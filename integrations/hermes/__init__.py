"""Hermes Agent MemoryProvider plugin for Vesma — contract shim (#125 W5).

MIGRATED onto the ADR-0017 D1 provider contract (Wave 5): this plugin is
now a THIN Hermes-side shim. Every memory operation routes in-process
through :class:`mnemos.adapters.hermes.HermesMemoryAdapter` — the
``VesmaSDK`` facade + the W3 lifecycle hooks — and the ``MemoryManager``
 beneath them. The legacy bespoke path (raw urllib HTTP client, own
TOTP/login/session-auth flow, circuit breaker, sync/prefetch thread pool,
auto-publish bypass) is GONE; see the adapter module docstring for the
duty-by-duty migration map.

Architecture::

    Hermes MemoryManager
        ↓ MemoryProvider ABC (THIS shim, deploy-only)
    HermesMemoryAdapter (mnemos.adapters.hermes)
        ↓ VesmaSDK facade + mnemos.hooks (the D1 contract)
    MemoryManager → SQLite + vectors + Obsidian vault

Installation::

    # 1. vesma importable in the Hermes Python env (pip install mnemos-memory-server)
    #    — no separate ``vesma serve`` process is needed anymore
    # 2. Copy this plugin into the Hermes plugins dir
    cp -r integrations/hermes ~/.hermes/plugins/mnemos
    # 3. Activate via the interactive wizard (recommended)
    hermes memory setup
    # Select "vesma", configure project/agent slugs and store paths
    # OR: hermes config set memory.provider vesma

Config (in $HERMES_HOME/config.yaml under ``memory.vesma``)::

    memory:
      provider: vesma
      mnemos:
        data_dir: ""            # Vesma data dir ("" = vesma default)
        vault_path: ""          # Obsidian vault path ("" = vesma default)
        project: "hermes"       # project tag slug
        agent: "hermes-default" # agent tag slug
        auto_sync: true         # mirror builtin writes + sync significant turns
        publish_on_write: true  # promote writes to published (LLM-less posture)
        sync_interval: 10       # sync every Nth turn
        sync_min_user_chars: 50 # significance threshold for sync_turn

Env vars (config.yaml ``memory.vesma`` overrides these)::

    VESMA_DATA_DIR           — data dir ("" = default)
    VESMA_VAULT__VAULT_PATH  — vault path ("" = default)
    VESMA_PROJECT            — project slug (default: hermes)
    VESMA_AGENT              — agent slug (default: hermes-default)
    VESMA_AUTO_SYNC          — mirror + sync writes (default: true)
    VESMA_PUBLISH_ON_WRITE   — publish writes immediately (default: true)
    VESMA_SYNC_INTERVAL      — sync every Nth turn (default: 10)
    VESMA_SYNC_MIN_USER_CHARS — significance threshold (default: 50)

BREAKING vs the legacy HTTP plugin: ``base_url`` / ``api_key`` /
``totp_secret`` are gone — the plugin embeds the memory server in-process
(loopback by construction, ADR-0017 D6). Point ``data_dir`` /
``vault_path`` at the SAME store only if no other process owns it
(SQLite single-writer: pick one owner per data dir).

Tools exposed (all in-process over the contract; vesma_align_prefix
stays MCP-only — no manager verb):
    vesma_search / vesma_add / vesma_recall_context /
    vesma_save_context / vesma_agent_recall / vesma_list_recent /
    vesma_list_tags / vesma_stats / vesma_auto_collect_status /
    vesma_compress / vesma_retrieve / vesma_ingest_url /
    vesma_watch_start / vesma_watch_stop / vesma_watch_status
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from typing import Any

from agent.memory_provider import MemoryProvider
from mnemos.adapters.hermes import HermesMemoryAdapter
from mnemos.config import Settings
from mnemos.models import MemoryType
from mnemos.sdk import VesmaSDK
from tools.registry import tool_error

logger = logging.getLogger(__name__)

# ── Auto-collect reminder thresholds (shim-side signal vector) ────────────────
# The legacy plugin read the server's /auto-collect tracker over HTTP; the
# counter now lives WHERE the calls happen — in this process. Same shape,
# same thresholds as the server's defaults.
_REMIND_CALLS = 6
_REMIND_SECS = 480


# ── Config ────────────────────────────────────────────────────────────────────


def _load_config() -> dict:
    """Load config from env vars, with config.yaml ``memory.vesma`` overrides.

    Env vars provide defaults; the ``plugins.vesma`` (legacy) and
    ``memory.vesma`` (wizard) sections of ``config.yaml`` override —
    ``memory.vesma`` last (the wizard writes there).
    """
    config: dict[str, Any] = {
        "data_dir": os.environ.get("VESMA_DATA_DIR", ""),
        "vault_path": os.environ.get("VESMA_VAULT__VAULT_PATH", ""),
        "project": os.environ.get("VESMA_PROJECT", "hermes"),
        "agent": os.environ.get("VESMA_AGENT", "hermes-default"),
        "auto_sync": os.environ.get("VESMA_AUTO_SYNC", "true").lower()
        in ("true", "1", "yes", "on"),
        "publish_on_write": os.environ.get("VESMA_PUBLISH_ON_WRITE", "true").lower()
        in ("true", "1", "yes", "on"),
        "sync_interval": int(os.environ.get("VESMA_SYNC_INTERVAL", "10")),
        "sync_min_user_chars": int(os.environ.get("VESMA_SYNC_MIN_USER_CHARS", "50")),
    }

    try:
        from hermes_cli.config import cfg_get, load_config

        raw = load_config()
        merged: dict[str, Any] = {}
        for section in (cfg_get(raw, "plugins", "vesma"), cfg_get(raw, "memory", "vesma")):
            for k, v in (section or {}).items():
                if v is not None and v != "":
                    merged[k] = v
        for k, v in merged.items():
            if k in ("sync_interval", "sync_min_user_chars"):
                try:
                    v = int(v)
                except (TypeError, ValueError):
                    continue
            if k in ("auto_sync", "publish_on_write") and isinstance(v, str):
                v = v.lower() in ("true", "1", "yes", "on")
            config[k] = v
    except Exception:
        pass

    return config


# ── Tool schemas (OpenAI function-calling format — model-facing contract,
#    unchanged names/params from the legacy plugin) ────────────────────────────

MNEMOS_SEARCH_SCHEMA: dict[str, Any] = {
    "name": "vesma_search",
    "description": (
        "Search Vesma memory using hybrid vector + FTS5 search. Results are "
        "secret-scanned at issuance. Use before architectural decisions, "
        "before web searches, and when resuming a topic.\n\n"
        "Returns: list of {id, title, content, tags, status, score, "
        "search_type}."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "Natural language query."},
            "tags": {
                "type": "array",
                "items": {"type": "string"},
                "description": 'Tag filters, e.g. ["mnemos:decision"].',
            },
            "limit": {"type": "integer", "description": "Max results (default 10)."},
        },
        "required": ["query"],
    },
}

MNEMOS_ADD_SCHEMA: dict[str, Any] = {
    "name": "vesma_add",
    "description": (
        "Add a memory entry to Vesma. Tag contract is mandatory: "
        "exactly one project:<slug>, one agent:<slug>, and at least one "
        "mnemos:<subtype>. Write what you would want to read back in 30 days. "
        "One idea per entry.\n\n"
        "vesma subtypes: session, checkpoint, bug-pattern, learning, "
        "decision, rule, open-question, legacy."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "content": {"type": "string", "description": "Markdown body."},
            "tags": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    'Required: ["project:<slug>", "agent:<slug>", "mnemos:<subtype>"].'
                ),
            },
            "title": {"type": "string", "description": "Short title (optional)."},
            "memory_type": {
                "type": "string",
                "description": "note | fact | snippet | bookmark | conversation",
                "default": "note",
            },
        },
        "required": ["content", "tags"],
    },
}

MNEMOS_RECALL_CONTEXT_SCHEMA: dict[str, Any] = {
    "name": "vesma_recall_context",
    "description": (
        "Recall the most recent session checkpoints for the project — the "
        "saved context (goals, progress, decisions) from the last "
        "save_context call. Use at session start and after compression."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "Optional focus to rank checkpoints by relevance.",
            },
            "limit": {"type": "integer", "default": 5},
        },
        "required": [],
    },
}

MNEMOS_SAVE_CONTEXT_SCHEMA: dict[str, Any] = {
    "name": "vesma_save_context",
    "description": (
        "Save a session checkpoint — structured context capturing goals, "
        "completed work, in-progress items, decisions, free-form context. "
        "Tagged mnemos:checkpoint for recall via vesma_recall_context. "
        "Use at meaningful milestones; write sparingly."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "goals": {"type": "array", "items": {"type": "string"}},
            "completed": {"type": "array", "items": {"type": "string"}},
            "in_progress": {"type": "array", "items": {"type": "string"}},
            "decisions": {"type": "array", "items": {"type": "string"}},
            "context": {"type": "string"},
        },
        "required": [],
    },
}

MNEMOS_AGENT_RECALL_SCHEMA: dict[str, Any] = {
    "name": "vesma_agent_recall",
    "description": (
        "Recall agent-scoped context — entries authored by a specific "
        "agent (default: this deployment's agent slug). Use when resuming "
        "work to recover your own prior findings.\n\n"
        "Returns: list of {id, title, content, tags, created_at}."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "agent": {"type": "string", "description": "Agent slug (optional)."},
            "query": {"type": "string", "description": "Optional focus query."},
            "limit": {"type": "integer", "default": 20},
        },
        "required": [],
    },
}

MNEMOS_LIST_RECENT_SCHEMA: dict[str, Any] = {
    "name": "vesma_list_recent",
    "description": (
        "List recent memories, optionally filtered by status.\n\n"
        "Returns: list of {id, title, tags, status, created_at}."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "limit": {"type": "integer", "default": 20},
        },
        "required": [],
    },
}

MNEMOS_LIST_TAGS_SCHEMA: dict[str, Any] = {
    "name": "vesma_list_tags",
    "description": "List all tags with entry counts.",
    "parameters": {"type": "object", "properties": {}, "required": []},
}

MNEMOS_STATS_SCHEMA: dict[str, Any] = {
    "name": "vesma_stats",
    "description": "Store statistics — totals, status breakdown, project slice.",
    "parameters": {"type": "object", "properties": {}, "required": []},
}

MNEMOS_AUTO_COLLECT_STATUS_SCHEMA: dict[str, Any] = {
    "name": "vesma_auto_collect_status",
    "description": (
        "Compaction signal vector — tool calls since the last "
        "save_context, elapsed seconds, and a checkpoint recommendation. "
        "Tracked in-process (the calls happen here).\n\n"
        "Returns: {auto_collect_enabled, signals, recommendation}."
    ),
    "parameters": {"type": "object", "properties": {}, "required": []},
}

MNEMOS_COMPRESS_SCHEMA: dict[str, Any] = {
    "name": "vesma_compress",
    "description": (
        "Compress large content (tool output, logs, JSON) losslessly via "
        "CCR: the original is cached (SHA-256 keyed) and a short marker "
        "lets the model fetch it back with vesma_retrieve. 70-90% token "
        "reduction on typical logs; <500 chars returned as-is."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "text": {"type": "string", "description": "Content to compress."},
            "profile": {
                "type": "string",
                "description": "log | terminal | code | docs | web (auto if omitted).",
            },
        },
        "required": ["text"],
    },
}

MNEMOS_RETRIEVE_SCHEMA: dict[str, Any] = {
    "name": "vesma_retrieve",
    "description": (
        "Retrieve the original for a CCR [compressed: …] marker hash — "
        "full text, or FTS5-ranked snippets when query is given."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "hash": {"type": "string"},
            "query": {"type": "string"},
            "snippet_count": {"type": "integer", "default": 5},
        },
        "required": ["hash"],
    },
}

MNEMOS_INGEST_URL_SCHEMA: dict[str, Any] = {
    "name": "vesma_ingest_url",
    "description": (
        "Fetch a web page, extract main content, save as a memory. "
        "Credentials embedded in the URL are stripped before storage. "
        "Tags follow the vesma_add contract."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "url": {"type": "string"},
            "tags": {"type": "array", "items": {"type": "string"}},
        },
        "required": ["url", "tags"],
    },
}

MNEMOS_WATCH_START_SCHEMA: dict[str, Any] = {
    "name": "vesma_watch_start",
    "description": (
        "Start the background file watcher — new/modified files under the "
        "watched paths are auto-indexed. Empty paths watches the cwd."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "paths": {"type": "array", "items": {"type": "string"}},
            "scan": {"type": "boolean", "default": True},
            "include_rules": {"type": "boolean", "default": False},
        },
        "required": [],
    },
}

MNEMOS_WATCH_STOP_SCHEMA: dict[str, Any] = {
    "name": "vesma_watch_stop",
    "description": "Stop the file watcher. Idempotent.",
    "parameters": {"type": "object", "properties": {}, "required": []},
}

MNEMOS_WATCH_STATUS_SCHEMA: dict[str, Any] = {
    "name": "vesma_watch_status",
    "description": "Watcher state: {running, paths, counts}.",
    "parameters": {"type": "object", "properties": {}, "required": []},
}


# ── Provider ──────────────────────────────────────────────────────────────────


class VesmaMemoryProvider(MemoryProvider):
    """Hermes MemoryProvider shim over the Vesma contract adapter.

    All memory operations delegate to
    :class:`mnemos.adapters.hermes.HermesMemoryAdapter` (VesmaSDK facade
    + lifecycle hooks, in-process). This class owns ONLY the Hermes ABC
    glue: config loading, tool schemas/dispatch, the prefetch thread, and
    the harness-never-blocks error guard (memory failures degrade to
    logged no-ops / tool_error strings, never harness exceptions).
    """

    def __init__(self, config: dict | None = None):
        self._config = config or _load_config()
        self._sdk: VesmaSDK | None = None
        self._adapter: HermesMemoryAdapter | None = None

        self._session_id = ""
        self._platform = "cli"
        self._agent_context = "primary"

        # Prefetch (assemble_context does recall+filter+scan+align — run
        # it off the turn loop; the result is injected on the next turn).
        self._prefetch_thread: threading.Thread | None = None
        self._prefetch_lock = threading.Lock()
        self._prefetch_result = ""

        # Auto-collect signal vector (in-process now).
        self._tool_call_counter = 0
        self._last_checkpoint_time = time.time()

    @property
    def name(self) -> str:
        return "vesma"

    # -- Construction / availability -----------------------------------------

    def _ensure(self) -> HermesMemoryAdapter:
        """Construct the SDK + adapter once (idempotent)."""
        if self._adapter is not None:
            return self._adapter
        mnemos_cfg: dict[str, str] = {}
        if self._config.get("data_dir"):
            mnemos_cfg["data_dir"] = str(self._config["data_dir"])
        if self._config.get("vault_path"):
            mnemos_cfg["vault_path"] = str(self._config["vault_path"])
        settings = Settings(vesma=mnemos_cfg)
        settings.resolve_paths()
        self._sdk = VesmaSDK(settings)
        self._adapter = HermesMemoryAdapter(
            self._sdk,
            project=str(self._config.get("project", "hermes")),
            agent=str(self._config.get("agent", "hermes-default")),
            auto_sync=bool(self._config.get("auto_sync", True)),
            publish_on_write=bool(self._config.get("publish_on_write", True)),
            sync_interval=int(self._config.get("sync_interval", 10)),
            sync_min_user_chars=int(self._config.get("sync_min_user_chars", 50)),
        )
        return self._adapter

    def _rebind_adapter(self, session_id: str, *, reset: bool) -> None:
        """(Re)build the adapter preserving the SDK; bind the session.

        ``reset=True`` (Hermes /reset, /new) starts fresh turn counters;
        continuation (/resume, /branch, compression) keeps them.
        """
        if self._sdk is None:
            self._ensure()
        elif reset or self._adapter is None:
            cfg = self._config
            self._adapter = HermesMemoryAdapter(
                self._sdk,
                project=str(cfg.get("project", "hermes")),
                agent=str(cfg.get("agent", "hermes-default")),
                auto_sync=bool(cfg.get("auto_sync", True)),
                publish_on_write=bool(cfg.get("publish_on_write", True)),
                sync_interval=int(cfg.get("sync_interval", 10)),
                sync_min_user_chars=int(cfg.get("sync_min_user_chars", 50)),
            )
        if self._adapter is not None:
            self._adapter.bind_session(session_id)

    def is_available(self) -> bool:
        """The embedded memory server is available when it constructs."""
        try:
            self._ensure()
            return True
        except Exception as e:
            logger.warning("Vesma provider unavailable: %s", e)
            return False

    def initialize(self, session_id: str, **kwargs) -> None:
        self._session_id = session_id
        self._platform = kwargs.get("platform", "cli")
        self._agent_context = kwargs.get("agent_context", "primary")
        agent_identity = kwargs.get("agent_identity", "default")
        if agent_identity and agent_identity != "default":
            self._config["agent"] = agent_identity
        self._rebind_adapter(session_id, reset=True)
        logger.info(
            "Vesma provider initialized (contract): session=%s platform=%s agent=%s",
            self._session_id,
            self._platform,
            self._config.get("agent"),
        )

    def system_prompt_block(self) -> str:
        return (
            "# Vesma Memory\n"
            "Long-term memory (Vesma, in-process on the provider contract). "
            "Use vesma_search before architectural decisions and web "
            "searches. Use vesma_add to persist non-obvious learnings, "
            "decisions, and bug-patterns. Use vesma_recall_context at "
            "session start; vesma_save_context at milestones. Use "
            "vesma_agent_recall to recover your own prior findings. Use "
            "vesma_compress / vesma_retrieve to shrink and rehydrate "
            "large tool outputs losslessly.\n"
            "Tag contract: project:<slug> + agent:<slug> + mnemos:<subtype> "
            "(session|checkpoint|bug-pattern|learning|decision|rule|"
            "open-question|legacy). Search first, write sparingly, never "
            "block on memory failure."
        )

    # -- Prefetch (pre_llm_call → assemble_context, off the turn loop) ----

    def prefetch(self, query: str, *, session_id: str = "") -> str:
        """Return the cached prefetch result (filled by queue_prefetch)."""
        with self._prefetch_lock:
            result = self._prefetch_result
            self._prefetch_result = ""
            return result

    def queue_prefetch(self, query: str, *, session_id: str = "") -> None:
        """Assemble the injection block in the background for the next turn."""
        if not query:
            return

        def _run() -> None:
            try:
                adapter = self._ensure()
                result = adapter.pre_llm_call(query=query)
                lines = []
                for block in result.get("blocks", []):
                    title = block.get("provenance", "vesma")
                    excerpt = block.get("content", "")[:200]
                    lines.append(f"- {title}: {excerpt}")
                if lines:
                    with self._prefetch_lock:
                        self._prefetch_result = "\n".join(lines)
            except Exception as e:
                logger.debug("Vesma prefetch failed: %s", e)

        self._prefetch_thread = threading.Thread(target=_run, daemon=True, name="vesma-prefetch")
        self._prefetch_thread.start()

    # -- Lifecycle (all on the contract adapter; never block the harness) --

    def sync_turn(
        self,
        user_content: str,
        assistant_content: str,
        *,
        session_id: str = "",
        messages: list[dict[str, Any]] | None = None,
    ) -> None:
        if self._agent_context not in ("primary", ""):
            return
        try:
            self._ensure().sync_turn(user_content, assistant_content)
        except Exception as e:
            logger.debug("Vesma sync_turn failed: %s", e)

    def on_memory_write(
        self,
        action: str,
        target: str,
        content: str,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        try:
            self._ensure().mirror_memory_write(action, target, content, metadata)
        except Exception as e:
            logger.debug("Vesma memory_write mirror failed: %s", e)

    def on_pre_compress(self, messages: list[dict[str, Any]]) -> str:
        """ADR-0018 bridge: report what the compressor is about to discard.

        The to-be-discarded user messages become ONE on_context_rewrite
        event — the original lands in LTM losslessly (idempotent). Returns
        a short hint for the compression summary (legacy return contract).
        """
        hint = ""
        user_msgs = [
            m.get("content", "")
            for m in (messages or [])[-20:]
            if m.get("role") == "user" and isinstance(m.get("content"), str)
        ]
        if user_msgs:
            original = "\n".join(m[:500] for m in user_msgs[-10:])
            try:
                self._ensure().report_context_rewrite(original)
            except Exception as e:
                logger.debug("Vesma context_rewrite report failed: %s", e)
            hint = (
                "[Vesma] Discarded conversation preserved in LTM "
                "(on_context_rewrite); recall via vesma_search."
            )
        return hint

    def on_session_end(self, messages: list[dict[str, Any]]) -> None:
        if self._agent_context not in ("primary", ""):
            return
        try:
            self._ensure().session_end(messages or [])
        except Exception as e:
            logger.debug("Vesma on_session_end failed: %s", e)

    def on_session_switch(
        self,
        new_session_id: str,
        *,
        parent_session_id: str = "",
        reset: bool = False,
        rewound: bool = False,
        **kwargs,
    ) -> None:
        try:
            self._rebind_adapter(new_session_id, reset=reset)
        except Exception as e:
            logger.debug("Vesma on_session_switch failed: %s", e)

    # -- Tools ─────────────────────────────────────────────────────────────

    def get_tool_schemas(self) -> list[dict[str, Any]]:
        return [
            MNEMOS_SEARCH_SCHEMA,
            MNEMOS_ADD_SCHEMA,
            MNEMOS_RECALL_CONTEXT_SCHEMA,
            MNEMOS_SAVE_CONTEXT_SCHEMA,
            MNEMOS_AGENT_RECALL_SCHEMA,
            MNEMOS_LIST_RECENT_SCHEMA,
            MNEMOS_LIST_TAGS_SCHEMA,
            MNEMOS_STATS_SCHEMA,
            MNEMOS_AUTO_COLLECT_STATUS_SCHEMA,
            MNEMOS_COMPRESS_SCHEMA,
            MNEMOS_RETRIEVE_SCHEMA,
            MNEMOS_INGEST_URL_SCHEMA,
            MNEMOS_WATCH_START_SCHEMA,
            MNEMOS_WATCH_STOP_SCHEMA,
            MNEMOS_WATCH_STATUS_SCHEMA,
        ]

    def handle_tool_call(self, tool_name: str, args: dict, **kwargs) -> str:
        self._tool_call_counter += 1
        try:
            return self._dispatch_tool(tool_name, args)
        except Exception as e:
            logger.warning("Vesma tool %s failed: %s", tool_name, e)
            return tool_error(f"Vesma tool error: {e}")

    def _dispatch_tool(self, tool_name: str, args: dict) -> str:
        adapter = self._ensure()
        mgr = adapter.sdk.manager  # surfaced-operations escape hatch

        if tool_name == "vesma_search":
            items = adapter.search(
                args["query"], limit=int(args.get("limit", 10)), tags=args.get("tags")
            )
            if not items:
                return json.dumps({"result": "No relevant memories found."})
            return json.dumps({"results": items, "count": len(items)})

        if tool_name == "vesma_add":
            memory = adapter.add_memory(
                args["content"],
                args["tags"],
                title=args.get("title"),
                memory_type=MemoryType(args.get("memory_type", "note")),
            )
            return json.dumps(
                {
                    "result": "Memory stored.",
                    "id": memory.id,
                    "title": memory.title or memory.auto_title(),
                    "tags": memory.tags,
                    "status": memory.status,
                }
            )

        if tool_name == "vesma_recall_context":
            checkpoints = adapter.recall_checkpoints(
                query=args.get("query"),
                limit=min(int(args.get("limit", 5)), 10),
            )
            if not checkpoints:
                return json.dumps({"result": "No prior checkpoint found."})
            return json.dumps({"results": checkpoints, "count": len(checkpoints)})

        if tool_name == "vesma_save_context":
            memory = adapter.save_checkpoint(
                goals=args.get("goals"),
                completed=args.get("completed"),
                in_progress=args.get("in_progress"),
                decisions=args.get("decisions"),
                context=args.get("context"),
            )
            self._tool_call_counter = 0
            self._last_checkpoint_time = time.time()
            return json.dumps(
                {
                    "result": "Checkpoint saved.",
                    "id": memory.id,
                    "title": memory.title or memory.auto_title(),
                }
            )

        if tool_name == "vesma_agent_recall":
            items = adapter.agent_recall(
                args.get("agent"),
                query=args.get("query"),
                limit=min(int(args.get("limit", 20)), 100),
            )
            if not items:
                return json.dumps({"result": "No agent-scoped context found."})
            return json.dumps({"results": items, "count": len(items)})

        if tool_name == "vesma_list_recent":
            memories = mgr.list_recent(limit=int(args.get("limit", 20)))
            # Title-only echo — mirror the vesma_list_recent channel scan.
            items = []
            for m in memories:
                scan = mgr.scan_issuance_item(None, title=m.auto_title(), context=f"hermes:{m.id}")
                if scan.refused:
                    continue
                items.append(
                    {
                        "id": m.id,
                        "title": scan.title,
                        "tags": m.tags,
                        "status": m.status,
                        "created_at": m.created_at.isoformat(),
                    }
                )
            return json.dumps({"results": items, "count": len(items)})

        if tool_name == "vesma_list_tags":
            return json.dumps({"results": mgr.list_tags()})

        if tool_name == "vesma_stats":
            return json.dumps({"results": adapter.stats()})

        if tool_name == "vesma_auto_collect_status":
            elapsed = time.time() - self._last_checkpoint_time
            recommendation = (
                "save_context recommended"
                if self._tool_call_counter >= _REMIND_CALLS or elapsed >= _REMIND_SECS
                else "ok"
            )
            return json.dumps(
                {
                    "auto_collect_enabled": True,
                    "signals": {
                        "call_counter": {"calls_since_save": self._tool_call_counter},
                        "elapsed_secs": {"since_last_save": round(elapsed, 1)},
                    },
                    "recommendation": recommendation,
                }
            )

        if tool_name == "vesma_compress":
            # N2 identity mandate: agent+session threaded onto the cache
            # row INSIDE the post_tool_call hook — no identity-less mode.
            envelope = adapter.post_tool_call(
                tool_name="vesma_compress",
                output_text=args["text"],
                auto_compress=True,
                profile=args.get("profile"),
            )
            return json.dumps(envelope.get("ccr", {}))

        if tool_name == "vesma_retrieve":
            result = mgr.retrieve_content(
                args["hash"],
                query=args.get("query"),
                snippet_count=args.get("snippet_count"),
                project=adapter.project,
                agent=adapter.agent,
                session=adapter.session or None,
            )
            return json.dumps(result)

        if tool_name == "vesma_ingest_url":
            memory = mgr.ingest_url(
                args["url"],
                tags=args["tags"],
                project=adapter.project,
                agent=adapter.agent,
            )
            # m2 (final review): auto_title() derives from the fetched page
            # content — scan the echoed title at issuance; refuse mode drops
            # it (error shape, no echo), mirroring vesma_filter channels.
            title_scan = mgr.scan_issuance_item(
                None, title=memory.auto_title(), context=f"hermes:vesma_ingest_url:{memory.id}"
            )
            if title_scan.refused:
                return json.dumps({"error": f"issuance refused: {title_scan.reason}"})
            return json.dumps({"id": memory.id, "title": title_scan.title, "url": args["url"]})

        if tool_name == "vesma_watch_start":
            paths = args.get("paths") or []
            mgr.watch_start(
                paths=paths,
                scan=bool(args.get("scan", True)),
                include_rules=bool(args.get("include_rules", False)),
            )
            return json.dumps({"status": "started", "paths": paths})

        if tool_name == "vesma_watch_stop":
            mgr.watch_stop()
            return json.dumps({"status": "stopped"})

        if tool_name == "vesma_watch_status":
            return json.dumps(mgr.watch_status())

        return tool_error(f"Unknown tool: {tool_name}")

    # -- Config schema ─────────────────────────────────────────────────────

    def get_config_schema(self) -> list[dict[str, Any]]:
        return [
            {
                "key": "data_dir",
                "description": "Vesma data dir (empty = vesma default)",
                "default": "",
                "env_var": "VESMA_DATA_DIR",
            },
            {
                "key": "vault_path",
                "description": "Obsidian vault path (empty = vesma default)",
                "default": "",
                "env_var": "VESMA_VAULT__VAULT_PATH",
            },
            {
                "key": "project",
                "description": "Default project slug for tag contract",
                "default": "hermes",
                "env_var": "VESMA_PROJECT",
            },
            {
                "key": "agent",
                "description": "Default agent slug for tag contract",
                "default": "hermes-default",
                "env_var": "VESMA_AGENT",
            },
            {
                "key": "auto_sync",
                "description": "Mirror builtin writes and sync significant turns",
                "default": "true",
                "choices": ["true", "false"],
                "env_var": "VESMA_AUTO_SYNC",
            },
            {
                "key": "publish_on_write",
                "description": (
                    "Promote writes to published immediately (the LLM-less "
                    "posture; set false when the knowledge pipeline runs)"
                ),
                "default": "true",
                "choices": ["true", "false"],
                "env_var": "VESMA_PUBLISH_ON_WRITE",
            },
            {
                "key": "sync_interval",
                "description": "Sync every Nth turn",
                "default": "10",
                "env_var": "VESMA_SYNC_INTERVAL",
            },
            {
                "key": "sync_min_user_chars",
                "description": "Significance threshold: user-message chars",
                "default": "50",
                "env_var": "VESMA_SYNC_MIN_USER_CHARS",
            },
        ]

    def save_config(self, values: dict[str, Any], hermes_home: str) -> None:
        """Write config to config.yaml under ``memory.vesma`` (wizard path)."""
        from pathlib import Path

        config_path = Path(hermes_home) / "config.yaml"
        try:
            import yaml

            existing: dict[str, Any] = {}
            if config_path.exists():
                with open(config_path, encoding="utf-8-sig") as f:
                    existing = yaml.safe_load(f) or {}
            existing.setdefault("memory", {})
            existing["memory"]["vesma"] = values
            with open(config_path, "w", encoding="utf-8") as f:
                yaml.dump(existing, f, default_flow_style=False)
        except Exception as e:
            logger.warning("Failed to save Vesma config: %s", e)

    # -- Shutdown ──────────────────────────────────────────────────────────

    def shutdown(self) -> None:
        if self._prefetch_thread and self._prefetch_thread.is_alive():
            self._prefetch_thread.join(timeout=5.0)
        if self._sdk is not None:
            try:
                self._sdk.close()
            except Exception as e:
                logger.debug("Vesma SDK close failed: %s", e)
            self._sdk = None
            self._adapter = None


# ── Registration ──────────────────────────────────────────────────────────────


def register(ctx) -> None:
    """Register Vesma as a Hermes memory provider plugin."""
    ctx.register_memory_provider(VesmaMemoryProvider())
