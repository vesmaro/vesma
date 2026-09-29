"""Configuration management for Mnemos."""

from __future__ import annotations

import logging
import os
import re
import shutil
from pathlib import Path
from typing import Any, Final, Literal

import yaml
from pydantic import BaseModel, Field, SecretStr, field_validator, model_validator
from pydantic.fields import FieldInfo
from pydantic_settings import BaseSettings, PydanticBaseSettingsSource

logger = logging.getLogger(__name__)


class MnemosConfig(BaseModel):
    # Consolidated layout (v2.1): everything lives under ~/.mnemos/.
    # Old scattered paths (~/mnemos-vault, ~/.mnemos as data_dir) are
    # auto-migrated by ``Settings.migrate_layout()`` on first load.
    vault_path: Path = Path("~/.mnemos/vault")
    data_dir: Path = Path("~/.mnemos/data")
    db_name: str = "mnemos.db"
    # M2: tag contract enforcement
    strict_tag_contract: bool = True
    # M10: auto-run the context filter on ingest (mnemos_add / manager.add).
    # When True, raw_content is preserved and clean_content is populated;
    # filter failures are non-fatal (memory is still saved with raw content).
    auto_filter: bool = True
    # ADR-0019 §2 (B2b) — entry visibility policy for records created
    # WITHOUT an explicit ``status`` (an explicit ``status=`` in
    # MemoryCreate always keeps the pre-B2b contract and routes through
    # the N1 direct-seed gate):
    #   * "immediate" (default, the owner's model) — the content passes
    #     the fail-closed ingest gate (danger detectors + secret scan,
    #     the SAME Phase A verdict helper): clean ⇒ stored PUBLISHED with
    #     ``pipeline_state='pending'`` and findable at once; a refusal or
    #     a scanner error ⇒ stored RAW, invisible, zero-loss.
    #   * "curated" — stored RAW with ``pipeline_state='pending'``
    #     (invisible); visibility is granted only when the refine cycle
    #     completes and the refined projection passes the publication
    #     gate (refusal at that point enters the lane-(b) quarantine).
    # Canonical env override: VESMARO_MNEMOS__VISIBILITY=curated.
    visibility: Literal["immediate", "curated"] = "immediate"
    # ADR-0030 A0 (issue #322) — deterministic relates_to auto-minting on
    # write: after every add, ONE synchronous hybrid search through the
    # EXISTING FTS+vector legs mints up to 3 `relates_to` edges to
    # near-duplicate candidates (provenance 'auto-dedupe', raised weight;
    # exclusions per ADR-0030 I4/§5/intra-project). DEFAULT ON (owner
    # decision 2026-09-28 «graphs on by default»): the A0-review density
    # gate was met on the live corpus (cohort ≥0.5 edges/write, 7-day
    # window, ADR-0030 addendum B.1) — minting is the fuel line the
    # ecosystem builds on. Minting is best-effort: a minting failure
    # never fails the write.
    # Canonical env override: VESMARO_MNEMOS__GRAPH_AUTO_MINT=false.
    graph_auto_mint: bool = True
    # ADR-0030 A0 (issue #324) — the 1-hop ``relates_to`` walk in the
    # search graph leg: the leg extends from ``supersedes`` (both
    # directions, unchanged) to also expand ``relates_to`` neighbours,
    # behind the SAME gates as the fused rows (status F1, project scope
    # F2, ADR-0019 §4/§5) and the existing decay rule. Invariants
    # I1-I3 are codified as mutation-verified contract tests
    # (tests/test_graph_walk_invariants.py) — the Security condition
    # for letting minted fuel reach search. DEFAULT ON (owner decision
    # 2026-09-28): S1 shipped with the reserved-quota discipline and
    # the dedicated guard floor recall@5 ≥ 0.9121 (ADR-0030 B.3).
    # Canonical env override: VESMARO_MNEMOS__GRAPH_WALK=false.
    graph_walk: bool = True
    # ADR-0030 A1-S2 (issue #325) — feedback APPLY: the edge_stats
    # `used` counters multiply walked-block edge weights by a
    # SATURATING, bounded factor (I6: rank-only, bounded Δ per
    # principal — never eligibility, never the fused block, never past
    # the quota). INDEPENDENT of `graph_walk` by committee ruling
    # (ArchCom 2026-09-27, verdict (c) Security residual: independent
    # flag per leg): graph_walk=OFF ⇒ apply is inert (the walk does
    # not run); graph_walk=ON + feedback_apply=OFF ⇒ A1-S1 behavior
    # byte-identical. DEFAULT ON (owner decision 2026-09-28); capture
    # telemetry validated in the A0 7-day window.
    # Canonical env override: VESMARO_MNEMOS__FEEDBACK_APPLY=false.
    feedback_apply: bool = True
    # mnemos #96: workflow lifecycle guardrails. Stale-lock threshold governs
    # how long a lock survives before a different actor can take it over
    # without ``force`` (guardrail 2). Rate limit caps transitions per memory
    # per minute to prevent churn (guardrail 5) — it is per-memory, NOT
    # per-actor: churn on a single memory is throttled regardless of which
    # actor drives the transitions.
    workflow_stale_lock_threshold_hours: int = Field(default=24, ge=1, le=720)
    workflow_rate_limit_per_minute: int = Field(default=30, ge=1, le=1000)
    # mnemos #125 W2 review F1: on_context_rewrite write-surface guardrails.
    # The rate limit counts STORED rewrite events per (project, session) per
    # minute — a deduplicated re-delivery performs no write and consumes no
    # quota, so at-least-once retry storms stay harmless. 0 disables the
    # limiter. Size caps reject oversized payloads at the boundary before
    # any write: content = the original block (default 1 MiB in chars),
    # diff = the advisory was→becomes payload (default 256 KiB in chars).
    context_rewrite_rate_limit_per_minute: int = Field(default=30, ge=0, le=10_000)
    # C10 (ArchCom 2026-08-27) — SECONDARY per-project aggregate ceiling:
    # total stored rewrite events per project per minute across ALL of
    # that project's sessions (the distinct-session count rides along in
    # the 429 message as the noisy-neighbor signal). Same 429/rate_limited
    # shape as the primary limiter; NULL-session events land in their own
    # bucket under the same knobs. 0 disables the aggregate ceiling (the
    # per-(project, session) limiter still applies). Default 300 = ten
    # sessions at full per-session burn; the residual noisy-neighbor risk
    # (one project starving its siblings on a shared node) is
    # ADR-0018-accepted (single-operator).
    context_rewrite_project_rate_limit_per_minute: int = Field(default=300, ge=0, le=100_000)
    context_rewrite_max_content_chars: int = Field(default=1_048_576, ge=1)
    context_rewrite_max_diff_chars: int = Field(default=262_144, ge=1)
    # C9 (ArchCom 2026-09-27, swarm v0a): per-(project, agent) cap on
    # awareness/operational-picture queries per minute. In-process
    # sliding window — the picture stores NOTHING (C2), so a SQL counter
    # over stored rows is structurally impossible; reads are not rows.
    # Over-limit degrades to a "rate-limited, retry later" line, never a
    # hard error (the composition contract must not break). Without the
    # cap a polling harness reconstructs a neighbor's timeline at
    # arbitrary resolution. 0 disables the limiter.
    # Canonical env override: VESMARO_MNEMOS__AWARENESS_PICTURE_RATE_LIMIT_PER_MINUTE=0.
    awareness_picture_rate_limit_per_minute: int = Field(default=30, ge=0, le=10_000)

    # vesmaro-canon v1.0.0 (ADR-0003 engine obligation 4) — the
    # server-minted canon envelope ``metadata.canon.language`` for
    # checkpoints written by ``save_checkpoint``. One language per record
    # (canon §6): the operator declares the deployment's checkpoint body
    # language ONCE; the ``save_checkpoint`` ``language=`` parameter
    # overrides per call, WITHOUT heuristics (a per-record language
    # guess is explicitly out of scope — verdicts A/G condition 4).
    # Canon enum: "ru" | "en" (schemas/envelope.schema.json). The
    # validator enforces the same set at the manager boundary
    # (``checkpoint_canon_envelope`` raises ValueError fail-loud), so a
    # mistyped config value surfaces on the first save, not silently.
    # Canonical env override: VESMARO_MNEMOS__CHECKPOINT_LANGUAGE=ru.
    checkpoint_language: Literal["ru", "en"] = "ru"
    # vesmaro-canon v1.0.0 (canon §9, ADR-0003 obligations 5-6) — the
    # write-path canon enforcement level. The validator
    # (``vesmaro.canon_validate``) runs on every record that CARRIES
    # ``metadata.canon`` at the write path (manager add/update incl. the
    # trusted ``save_checkpoint`` path); a record without the envelope is
    # OUT OF CANON SCOPE (canon §9 transitional rule — pre-canon and
    # imported rows are legacy, not violations) and is never validated.
    #   * "warn" (default) — violations are logged as machine-parseable
    #     warnings (code + memory id) and attached to the stored row as
    #     ``metadata["canon_warnings"]``; the write ALWAYS succeeds.
    #   * "strict" — violations REJECT the write on the CREATE path only
    #     (``manager.add``). Update paths never fail retroactively (canon
    #     §10 "new records only"): a legacy row's update must succeed even
    #     in strict mode.
    #   * "off" — no canon validation at all.
    # Strict stays default-off until vesmaro 6.0 (canon §9 freeze: the
    # warn telemetry decides the strict default, owner directive).
    # Canonical env override: VESMARO_MNEMOS__CANON_MODE=strict.
    canon_mode: Literal["off", "warn", "strict"] = "warn"


class LoggingConfig(BaseModel):
    """Logging configuration — file + console handlers with rotation.

    Set ``log_file`` to an empty path (``Path("")``) to disable file logging
    and emit to stderr only.
    """

    level: str = "INFO"  # DEBUG | INFO | WARNING | ERROR
    log_file: Path = Path("~/.mnemos/logs/mnemos.log")
    max_file_size_mb: int = Field(default=10, ge=1, le=1024)
    backup_count: int = Field(default=3, ge=0, le=100)
    format: str = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
    date_format: str = "%Y-%m-%d %H:%M:%S"


class EmbeddingConfig(BaseModel):
    # NM-1c (ADR-0021): the bundled mnema-embed model is the default.
    # Legacy values ("chromadb"/"chroma"/"default") migrate to nano with a
    # deprecation warning; quality-first operators can switch to "onnx".
    provider: str = "nano"  # nano | onnx | ollama | sentence-transformers
    # nano: bundled artifact name under mnemos/models/, or a filesystem path
    # to a .onnx file; onnx/st: HF model ID.
    model: str = "mnema-embed-v1"
    onnx_file: str = "onnx/model.onnx"  # ONNX filename within HF repo
    ollama_url: str = "http://localhost:11434"
    # M15.2: pin HF Hub downloads to a specific revision to mitigate supply-chain
    # risk (CWE-494 — download of code without integrity check). Override via
    # VESMARO_EMBEDDING__HF_REVISION env var or config.yaml. The default is
    # empty so the ``if not revision: raise`` guard in ONNXHubProvider fires
    # and forces operators to pin an explicit revision when using the ONNX
    # provider. When changing the ``model`` field, set ``hf_revision`` to a
    # matching pinned SHA/tag.
    hf_revision: str = ""


class SearchConfig(BaseModel):
    default_limit: int = 20
    # 0.5 balances the RRF legs (issue #300 probe): at alpha 0.7 the vector
    # leg structurally subordinates any FTS-only match (an FTS-rank-1 hit
    # scores 0.3/61 < a pure-vector rank-1 at 0.7/61); at alpha 0.5 they tie,
    # so FTS-rank-1 matches stop drowning. Measured on both embedder
    # regimes: governance +5.2pp (nano) / +1.04pp (lexical), knowledge
    # recall@5 +3.9pp (nano) / +3.7pp (lexical), zero G-neg displacement.
    hybrid_alpha: float = Field(default=0.5, ge=0.0, le=1.0)
    # ADR-0030 A0 (issue #323) — used/rejected feedback CAPTURE into the
    # append-only ``edge_stats`` table (I5). DEFAULT ON (owner decision
    # 2026-09-28 «graphs on by default»): capture is the fuel
    # ``feedback_apply`` consumes — an apply without capture is
    # decorative, so the whole loop ships enabled together (capture
    # telemetry was validated in the A0 7-day window; I5 caps volume,
    # capture-only has zero ranking influence). Flag off =
    # ``report_search_feedback`` performs no writes and no telemetry
    # (and ``feedback_apply`` stays inert on an empty counter set).
    # Env: ``VESMARO_SEARCH__FEEDBACK_CAPTURE_ENABLED``.
    feedback_capture_enabled: bool = True


class ApiConfig(BaseModel):
    host: str = "127.0.0.1"
    port: int = 8787
    # T-CORS: browser cross-origin allow-list for mnemos-eyes
    # Default is strict - CORS disabled, no origin permitted.
    cors_enabled: bool = False
    cors_allow_origins: list[str] = []
    cors_allow_credentials: bool = False
    cors_allow_methods: list[str] = ["GET", "POST", "DELETE"]
    cors_allow_headers: list[str] = ["Authorization", "Content-Type"]
    # T-AUTH additions (ADR-0014) ─────────────────────────────────────────────
    auth_enabled: bool = False  # default off — safe for loopback-only bind
    totp_enabled: bool = False  # default off — safe for loopback-only bind
    # env-only; never written to disk — VESMARO_API__TOTP_MASTER_KEY
    totp_master_key: SecretStr = SecretStr("")
    session_ttl_sec: int = Field(default=8 * 3600, ge=300, le=24 * 3600)
    session_pin_ip: bool = False  # bind session to creation IP
    behind_tls_proxy: bool = False  # operator-asserted TLS termination ahead
    trusted_proxies: list[str] = Field(default_factory=list)  # CIDRs for X-Forwarded-*


class McpConfig(BaseModel):
    transport: str = "stdio"


class WatcherConfig(BaseModel):
    paths: list[str] = []
    # M8: enable path-scoped rules ingest
    include_rules: bool = False
    ignore_dirs: list[str] = [
        ".git",
        "node_modules",
        "__pycache__",
        ".venv",
        "venv",
        "dist",
        "build",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
    ]
    extensions: list[str] = [
        ".md",
        ".py",
        ".js",
        ".ts",
        ".yaml",
        ".yml",
        ".toml",
        ".json",
        ".txt",
        ".rst",
        ".sh",
        ".css",
        ".html",
        ".sql",
    ]
    max_file_size_kb: int = 512
    auto_scan: bool = True
    auto_translate: bool = False


class LLMConfig(BaseModel):
    """Multi-provider LLM configuration (M4 synthesis workers, M10 context filter)."""

    provider: str = "ollama"  # anthropic | openai | azure_openai | ollama | gemini
    model: str = "qwen2.5:3b"
    # Ollama
    ollama_url: str = "http://localhost:11434"
    # OpenAI
    openai_api_key: SecretStr = SecretStr("")
    openai_base_url: str = ""
    # Azure OpenAI
    azure_endpoint: str = ""
    azure_api_version: str = "2024-02-01"
    azure_deployment: str = ""
    # Anthropic
    anthropic_api_key: SecretStr = SecretStr("")
    # Google Gemini
    gemini_api_key: SecretStr = SecretStr("")
    temperature: float = 0.3
    max_tokens: int = 4096


class AutomationConfig(BaseModel):
    """M5 — policy engine / scheduler configuration."""

    enabled: bool = True
    # APScheduler interval for periodic tasks
    scheduler_interval_sec: int = Field(default=300, ge=30, le=86400)
    # Debounce after vault write events
    event_debounce_sec: int = Field(default=45, ge=5, le=3600)
    # Minimum raw entries required before auto-clustering triggers
    min_raw_to_trigger: int = Field(default=3, ge=1, le=10000)
    # Cooldown between automated pipeline runs
    cooldown_sec: int = Field(default=180, ge=10, le=86400)


class RuntimeConfig(BaseModel):
    # Hard cap for CPU-bound thread pools (BLAS/OMP/ONNX/tokenizers)
    cpu_threads: int = Field(default=4, ge=1, le=64)
    # Uvicorn worker processes for `mnemos serve`
    uvicorn_workers: int = Field(default=1, ge=1, le=8)


class CCRConfig(BaseModel):
    """P1-4 — CCR (Compress-Cache-Retrieve) reversible compression.

    Inspired by headroom's CCR (https://github.com/headroomlabs-ai/headroom),
    Apache 2.0. We implement our own version integrated into the existing
    mnemos SQLite store (one DB, one backup) with FTS5 snippet retrieval
    and per-project scoping.
    """

    enabled: bool = True
    # Cache entries older than this are eligible for cleanup (days).
    ttl_days: int = Field(default=7, ge=1, le=365)
    # LRU eviction kicks in when the entry count exceeds this.
    max_entries: int = Field(default=10000, ge=100, le=1_000_000)
    # Content shorter than this (chars) is returned as-is — not cached,
    # not compressed (tiny content has no token savings).
    min_size_chars: int = Field(default=500, ge=50, le=100_000)
    # Number of snippet fragments returned by retrieve(query=...).
    snippet_count: int = Field(default=5, ge=1, le=50)
    # Token budget passed to apply_filter for the compress stage.
    filter_budget: int = Field(default=4096, ge=256, le=1_000_000)
    # ADR-0018 P0 — issuance guard. When True, retrieve refuses to issue
    # content whose scan detected a secret (refused=True, no content in
    # the response). Default False → redact-and-issue: matched spans are
    # replaced with <REDACTED:<pattern>> in the returned payload only;
    # the stored original is never mutated (zero-loss storage).
    retrieve_refuse_on_secret: bool = False
    # ADR-0018 P1-b (Security findings 1+4, CWE-668 ergonomics) — when
    # True, retrieving a project-scoped cache entry WITHOUT a matching
    # ``project`` argument is DENIED (refused=True, no content) instead
    # of merely logged as a WARNING. Default False preserves the legacy
    # unscoped behavior for callers without project context (the
    # WARNING still fires); flip to True on single-project deployments
    # to make the scope check enforceable.
    require_project_match: bool = False
    # A2 (ArchCom 2026-08-27) — strict marker validation on issuance.
    # When True, a MARKER-SHAPED retrieve (one carrying original_chars
    # and/or agent/session identity from a [compressed: ...] marker)
    # must pass existence (project-scoped, after A1), original_chars
    # integrity, and provenance (the cache row's issuer ledger must
    # match the caller's trusted issuer context) BEFORE any content is
    # issued; a failed check returns refused=True with no content
    # (fail-closed). Plain hash-only retrieves are unaffected. Default
    # False until W3 automation ships; flip to True in the automation
    # config so hooks redeem only markers minted in their own
    # agent/session context. Per-call ``validate_marker`` overrides.
    validate_markers: bool = False
    # P1-5/T3: background CCR cleanup interval (seconds). Default 1200s = 20 min.
    # The processor loop runs every `interval_sec` (default 120s); CCR cleanup runs
    # every `ccr_cleanup_interval_sec` to avoid scanning the cache table every cycle.
    ccr_cleanup_interval_sec: int = Field(default=1200, ge=60, le=86400)


class HooksConfig(BaseModel):
    """ADR-0017 D1 lifecycle hooks (mnemos #125, Wave 3).

    The hooks themselves (``pre_llm_call`` / ``on_session_start`` /
    ``post_tool_call``) are stateless thin wrappers over the manager's
    existing recall / assemble / compress paths — exposing them adds no
    capability the MCP/REST surfaces do not already have, so there is no
    master ``enabled`` switch (tool-surface exposure is a server concern,
    not per-hook config). The ONLY behavior knob is autocompression,
    which turns ``post_tool_call`` from a no-op envelope into a
    side-effecting CCR write.
    """

    # post_tool_call autocompression (ADR-0018 Phase 1): compress the
    # tool output via CCR and return the marker to substitute. Default
    # False — a side-effecting write is opt-in; automation deployments
    # (the W3+ harness configs) enable it. The per-call ``auto_compress``
    # argument overrides per invocation.
    auto_compress: bool = False
    # W3 review F3 — hard cap on ``post_tool_call`` ``output_text`` (in
    # characters, enforced at the hook boundary BEFORE any write: an
    # oversized payload is rejected with ValueError → 422 / MCP error
    # dict, nothing reaches ccr_store/FTS). Default matches the
    # ``mnemos.context_rewrite_max_content_chars`` caps convention
    # (1 MiB in chars). 0 disables the cap.
    max_output_chars: int = Field(default=1_048_576, ge=0, le=100_000_000)


class LanesConfig(BaseModel):
    """ADR-0025 E1 — deterministic retrieval lanes (mnemos #253).

    Lanes dispatch is a recall SUB-STAGE of ``assemble_context``
    (``mnemos/lanes.py``): rules/decisions ride deterministic SQL
    (``list_all(tags=...)``), knowledge keeps the hybrid RRF recall with
    governance rows excluded, checkpoints stay on the
    ``on_session_start`` bootstrap channel. ``STAGE_ORDER`` and
    ``hooks.py`` are unchanged.

    ONE switch, default OFF — there is no second enablement path. With
    ``enabled=False`` the assemble code path is identical to the
    pre-E1 pipeline: no lane queries run and the assembled output is
    byte-identical (the ``lane`` block field is omitted entirely when
    off, not rendered as a default value). Canonical env override:
    ``VESMARO_LANES__ENABLED=true``.

    ``type_boost`` is NOT a second lanes enablement path — it is the E3
    leg B0 treatment (E0 §1.1: "type-boost of rules/decisions at recall
    — one ranking line, zero meta-level", issue #277): governance rows
    keep arriving through the ordinary RRF recall only, but their
    scores are multiplied by ``lanes.B0_TYPE_BOOST_FACTOR`` and the
    candidate list is re-ranked by score. Zero meta-level: no lane
    queries, no lane ordering, no byte-stable pinned prefix. The two
    treatments are mutually exclusive (a leg is exactly one of
    A / B0 / B) — enabling both is a configuration bug, raised here.
    With both flags off the code path is byte-identical to the pre-E1
    pipeline. Canonical env override: ``VESMARO_LANES__TYPE_BOOST=true``.
    """

    enabled: bool = False
    type_boost: bool = False

    @model_validator(mode="after")
    def _treatments_are_exclusive(self) -> LanesConfig:
        """E0 §1.1 — ``enabled`` (leg B) and ``type_boost`` (leg B0) are
        alternative treatments of the SAME experiment; composing them is
        an unregistered fourth leg and is refused at the config boundary."""
        if self.enabled and self.type_boost:
            raise ValueError(
                "LanesConfig: 'enabled' (E0 §1.1 leg B) and 'type_boost' (leg B0) "
                "are mutually exclusive treatments — a leg is exactly one of A/B0/B"
            )
        return self


class CacheAlignerConfig(BaseModel):
    """P1-5 — CacheAligner prefix stabilization.

    Inspired by headroom's CacheAligner
    (https://github.com/headroomlabs-ai/headroom, Apache 2.0). Original
    implementation — no headroom code is imported.

    When enabled, dynamic content (timestamps, UUIDs, session ids, tokens)
    is relocated to the end of system-prompt-like text so the prefix stays
    byte-identical across requests and provider KV caches
    (Anthropic ``cache_control``, OpenAI prefix caching) hit.
    """

    enabled: bool = True
    # Toggle individual extractor kinds. Disabling a kind means those
    # spans stay in-place (not relocated). Useful for workloads where a
    # kind is known to be stable (e.g. a fixed session id per session).
    extract_timestamps: bool = True
    extract_uuids: bool = True
    extract_session_ids: bool = True
    extract_dates: bool = True
    extract_tokens: bool = True


class OutputStyleConfig(BaseModel):
    """P1-7 — Output token reduction via verbosity steering + effort routing.

    Inspired by headroom's output token reduction work. Original
    implementation. These are *hints* injected into tool result framing
    and passed through to the caller — they are not model config changes.
    """

    enabled: bool = True
    # Default verbosity when the caller does not specify one.
    #   "default" — preserve current behaviour (no steering injected).
    #   "terse"   — inject "be terse, no preambles, no restated context".
    #   "minimal" — inject "absolute minimum, facts only".
    default_verbosity: str = "default"
    # Default reasoning effort hint when the caller does not specify one.
    #   "low" / "medium" / "high" — dial thinking depth for routine steps.
    default_effort: str = "medium"


def _reject_degenerate_project_slugs(
    field_name: str,
    projects: list[str],
    *,
    allow_wildcard: bool,
) -> None:
    """Fail-closed config gate for federation project lists (review MAJOR).

    Blank slugs are refused everywhere: the memories table DEFAULTs
    ``project`` to ``''``, so an ``''`` entry in an allow-list matches
    every UNTAGGED record in SQL (``project IN ('')``) — a silent
    fail-open against the mesh-server untagged-record deny.
    ``'*'`` is refused in ``shared_projects``: the wildcard is a
    PER-PEER concept (:attr:`PeerConfig.allowed_projects`, documented);
    a ``'*'`` in the global shared list would flow through the
    effective-set resolution into ``_intersect_projects``, whose
    wildcard branch returns the requested list verbatim — handing a
    scoped read ANY project while the write path stays bounded
    (read/write asymmetry, vesmaro#371/#369 review).
    """
    for item in projects:
        if not item.strip():
            raise ValueError(
                f"{field_name}: blank project slug {item!r} is not allowed — "
                "it would match every untagged record (fail-closed)"
            )
        if item == "*" and not allow_wildcard:
            raise ValueError(
                f"{field_name}: '*' is not allowed here — the wildcard is a "
                "per-peer allowed_projects grant, not a shared_projects entry"
            )


class PeerConfig(BaseModel):
    """Per-peer federation ACL — Phase 1 prerequisite (contract §3.2, §6).

    ADR-0016 mandates per-peer bearer ``mnk_fed_<peer_id>_`` plus mTLS
    client cert pinned per peer, plus a per-peer ACL GATE. Each entry
    in :attr:`FederationConfig.peers` is one peer — keyed by the peer's
    A2A id (e.g. ``mnemos-A``).

    Fail-closed defaults: every list field defaults to empty, which
    means "none" — never implicit "allow all". ``["*"]`` is the explicit
    wildcard. This is the contract §3.2 / §6 rule: an operator who wants
    to open a peer must say so explicitly.

    Fields:
        bearer_token_env: NAME of the environment variable holding the
            per-peer bearer token (e.g. ``VESMARO_FED_PEER_A_TOKEN``).
            Per ``sensitive-data.instructions.md`` we store the NAME,
            never the value — the server reads the token from this env
            var at request time. The token format is
            ``mnk_fed_<peer_id>_<random>`` per ADR-0016.
        allowed_projects: Which projects this peer may pull. Subset
            filter applied on top of the global
            :attr:`FederationConfig.shared_projects` whitelist. Empty
            list = none (fail-closed). ``["*"]`` = all projects in
            ``shared_projects`` (explicit wildcard, not implicit).
            Blank slugs are rejected at the config boundary — an ``''``
            entry would match every untagged record in SQL.
        allowed_types: Which record types this peer may pull
            (``decision`` / ``learning`` / ``bug-pattern`` / ``rule`` /
            ``open-question`` / ``checkpoint`` / ``session``). Empty
            list = none. ``["*"]`` = all types.
        rate_limit_per_minute: Per-peer rate limit on pull requests
            (contract §8 — DDoS mitigation; slowapi is already used on
            the ``/auth/*`` surface). Default 30/min. Clamped to
            ``[1, 600]`` — below 1 is unusable, above 600 defeats the
            purpose.
        mtls_cert_fingerprint: Optional SHA-256 fingerprint of the
            peer's mTLS client cert, for pinning. If set, the server
            rejects connections whose client cert fingerprint does not
            match. If ``None``, mTLS pinning is not enforced for this
            peer (operator opts in). ADR-0016 recommends pinning for
            networked deployments.
    """

    bearer_token_env: str = Field(..., min_length=1, max_length=256)
    allowed_projects: list[str] = Field(default_factory=list)
    allowed_types: list[str] = Field(default_factory=list)
    rate_limit_per_minute: int = Field(default=30, ge=1, le=600)
    mtls_cert_fingerprint: str | None = Field(default=None, max_length=128)

    @field_validator("allowed_projects")
    @classmethod
    def _allowed_projects_no_degenerate_slugs(cls, value: list[str]) -> list[str]:
        """Reject blank slugs (``'*'`` stays legal here — documented grant)."""
        _reject_degenerate_project_slugs("PeerConfig.allowed_projects", value, allow_wildcard=True)
        return value


class MetaPollConfig(BaseModel):
    """S2 phase 2 meta-poller configuration (ADR-0021 Q10.2 poll-first).

    The poller lives in the mnemos process (ArchCom ruling Q10.1:
    orchestration in MNEMOS, the mesh is transport) and pulls each
    peer's ``federation_index`` pages by shelling out to the mesh CLI
    (``mnemos-mesh sync-meta --config <mesh.yaml> --peer <id> --json
    [--since <rev>]``), then imports the records in-process via
    :meth:`vesmaro.storage.sqlite_store.SQLiteStore.upsert_index_entries`.
    Metadata-only by construction: the poller never touches
    ``memories`` or the pipeline.

    Default OFF (additive): a config without the ``meta_poll`` section
    parses unchanged and the process behaves bit-for-bit as S2 phase 1.

    Fields:
        enabled: Master switch. Default ``False`` — the background
            poller task is only started (in ``vesmaro serve``) when
            this is explicitly set to ``true``.
        interval_seconds: Wall-clock seconds between background ticks.
            Default 300 (5 min). Clamped to ``[60, 86400]`` — below one
            minute a poller hammers the peers for no freshness gain,
            above a day it is not a poller any more.
        peers: Which peers to poll. ``"all"`` (default) = every key in
            :attr:`FederationConfig.peers`; an explicit list of peer
            A2A ids = only those (validated at the config boundary
            against the ``peers`` map — a typo'd id is a startup error,
            never a silently skipped peer). Blank ids are rejected.
        mesh_config_path: Path to the mesh ``yaml`` passed to the CLI
            via ``--config``. REQUIRED when ``enabled`` (the CLI cannot
            dial the peer leg without it) — enforced here, fail-fast at
            startup.
        mesh_bin: The mesh CLI binary to execute. Default
            ``mnemos-mesh`` (resolved via ``PATH``). An absolute path is
            the injection point used by tests to substitute a script
            double.
    """

    enabled: bool = False
    interval_seconds: int = 300
    peers: str | list[str] = "all"
    mesh_config_path: str = Field(default="", max_length=4096)
    mesh_bin: str = Field(default="mnemos-mesh", min_length=1, max_length=256)

    @field_validator("interval_seconds")
    @classmethod
    def _interval_clamped(cls, value: int) -> int:
        """Clamp the tick interval into ``[60, 86400]`` (clamp, not reject).

        An operator writing ``interval_seconds: 30`` wants a faster
        poller, not a crashed process — the value is silently clamped
        to the floor and the effective value is logged at poller start.
        """
        return max(60, min(86_400, value))

    @field_validator("peers")
    @classmethod
    def _peers_wellformed(cls, value: str | list[str]) -> str | list[str]:
        """Reject blank ids and any string form other than ``"all"``."""
        if isinstance(value, str):
            if value != "all":
                raise ValueError(
                    f"meta_poll.peers: string form must be 'all', got {value!r} "
                    "(list peer ids explicitly for a subset)"
                )
            return value
        for peer_id in value:
            if not peer_id.strip():
                raise ValueError("meta_poll.peers: blank peer id is not a valid A2A id")
        return value

    @model_validator(mode="after")
    def _enabled_requires_mesh_config(self) -> MetaPollConfig:
        """``enabled: true`` without ``mesh_config_path`` is a config error.

        The CLI invocation is built from this path; an empty value would
        surface as a per-tick subprocess failure loop instead of an
        operator-actionable message. Fail at the config boundary.
        """
        if self.enabled and not self.mesh_config_path.strip():
            raise ValueError(
                "meta_poll.enabled requires meta_poll.mesh_config_path — "
                "the mesh CLI needs the path to the peer-leg mesh yaml"
            )
        return self


class FetchConfig(BaseModel):
    """S2 lazy-fetch configuration (ADR-0021 Q10.3 chairman ruling).

    Lazy fetch is the EXPLICIT, operator-confirmed content fetch: the
    index mirror (``federation_index``, populated by the meta-poller)
    carries metadata-only rows; ``mnemos fetch --id <fed-id>`` resolves
    a row's origin peer and pulls the full :class:`CompactRecord` from
    it through the mesh CLI

        <mesh_bin> fetch --config <mesh.yaml> --peer <origin> --id <fed-id> ... --json

    (the Go-track subcommand; this side codes against its JSON
    contract), then imports in-process through the very same path
    :rpc:`WriteMemory` uses. There is no auto-fetch and no loop —
    every fetch is confirmed by a human at a TTY or an explicit
    ``--yes``.

    Keys mirror :class:`MetaPollConfig` (same defaults); there is NO
    ``enabled`` switch — the section only shapes the one-shot command,
    never a background task.

    Fields:
        mesh_config_path: Path to the mesh ``yaml`` passed to the CLI
            via ``--config``. REQUIRED to run ``mnemos fetch`` (the
            CLI cannot dial the peer leg without it) — enforced at the
            command boundary, not here (unlike ``meta_poll`` there is
            no ``enabled`` to gate at startup).
        mesh_bin: The mesh CLI binary to execute. Default
            ``mnemos-mesh`` (resolved via ``PATH``). An absolute path
            is the injection point used by tests to substitute a
            script double.
    """

    mesh_config_path: str = Field(default="", max_length=4096)
    mesh_bin: str = Field(default="mnemos-mesh", min_length=1, max_length=256)


class FederationConfig(BaseModel):
    """Federation (Phase 0 batch sync) configuration.

    ArchCom 2026-07-17 federation contract §3.1. This section governs
    operator-curated, offline, cron-triggered batch sync between two
    mnemos instances. It is NOT networked — transfer is out-of-band
    (rsync / scp / shared volume by the operator).

    Fields:
        shared_projects: Whitelist of project slugs eligible for sync.
            Blank slugs and ``'*'`` are rejected at the config boundary:
            ``''`` would match every untagged record in SQL, and ``'*'``
            is a per-peer ``allowed_projects`` concept — a wildcard here
            would bypass the shared-union bound on scoped reads.
            Only records whose ``project:`` tag matches a slug in this
            list are included in ``mnemos sync export``. Empty list =
            no projects are eligible (sync exports nothing). The
            receiving side re-applies the same filter on import.
        moderation_mapping_ttl_hours: TTL for the per-run moderation
            mapping table (contract §2.2). The mapping is in-memory only
            and NEVER replicated (it is a leak surface). Default 24h.
            After expiry, a fresh mapping is issued on the next
            moderation run.
        moderation_refuse_threshold: Fraction of content that must be
            redacted/anonymized to trigger a ``refuse`` verdict (contract
            §2.2). Default 0.8 = 80%. If >80% of content is redacted or
            anonymized, the record is refused (no useful remainder).
        peers: Per-peer ACL map — Phase 1 prerequisite (contract §3.2,
            §6, ADR-0016). Keyed by peer A2A id (e.g. ``mnemos-A``).
            Empty dict = no peers configured = the federation server
            refuses all pull requests (fail-closed). Each value is a
            :class:`PeerConfig` with the per-peer bearer token env name,
            allowed projects/types, rate limit, and optional mTLS cert
            fingerprint. ``shared_projects`` stays as the global filter;
            per-peer ``allowed_projects`` is a subset filter on top.
        access_log_path: Optional override for the B-side federation
            access log location (contract §10). When ``None`` (default)
            the log falls back to ``~/.mnemos/logs/federation-access.jsonl``
            (see :data:`vesmaro.federation_access_log.DEFAULT_LOG_PATH`).
            Set to an absolute path (e.g. ``/data/federation-access.jsonl``)
            to place the log on a persistent volume — useful for
            containerised deployments where ``~/.mnemos`` is ephemeral.
            The log is never replicated, never exported, never synced
            (leak surface, contract §10 "Где хранится").
        index_title_blocklist: Q10.9 title-regex patterns (Python ``re``,
            ``re.search`` semantics) applied to the S2 ``federation_index``
            at BOTH gates — metadata rows whose ``title`` matches any
            pattern are dropped from metadata-sync answers (export) and
            refused at upsert (import). Rationale: metadata distribution
            IS export (ADR-0021 ruling 9 — an index row is an inference
            surface). Empty list (default) = no title filtering. Each
            pattern must compile — invalid regex fails at the config
            boundary (startup fail-fast, never a silent skip).
        meta_poll: S2 phase 2 background meta-poller (ADR-0021 Q10.2
            poll-first, Q10.1 orchestration-in-mnemos ruling). Default
            OFF; see :class:`MetaPollConfig`. Additive: configs without
            the key parse unchanged (bit-for-bit S1/phase-1 behaviour).
        fetch: S2 lazy-fetch (Q10.3) — keys for the explicit
            ``mnemos fetch`` command only (mesh CLI binary + mesh yaml
            path); see :class:`FetchConfig`. Additive; no background
            behaviour.
        agent_token_key_path: Optional override for the W3 AgentGateway
            token signing key (ADR-0018-T §3). ``None`` (default) resolves
            to ``<mnemos.data_dir>/agent-token-signing.key`` — generated
            on first use with mode 0600, never leaves this host. Additive:
            configs without the key parse unchanged.
        agent_token_issuer: ``iss`` claim stamped into minted agent tokens
            (ADR-0018-T §3). Default ``"mnemos"``. Informational for
            routing/audit — validation relies on the Ed25519 signature,
            not on this string.
    """

    shared_projects: list[str] = Field(default_factory=list)
    moderation_mapping_ttl_hours: int = Field(default=24, ge=1, le=168)
    moderation_refuse_threshold: float = Field(default=0.8, ge=0.0, le=1.0)
    peers: dict[str, PeerConfig] = Field(default_factory=dict)
    access_log_path: str | None = Field(default=None, max_length=4096)
    index_title_blocklist: list[str] = Field(default_factory=list, max_length=256)
    meta_poll: MetaPollConfig = Field(default_factory=MetaPollConfig)
    agent_token_key_path: str | None = Field(default=None, max_length=4096)
    agent_token_issuer: str = Field(default="mnemos", min_length=1, max_length=64)
    fetch: FetchConfig = Field(default_factory=FetchConfig)

    @field_validator("shared_projects")
    @classmethod
    def _shared_projects_no_degenerate_slugs(cls, value: list[str]) -> list[str]:
        """Reject blank slugs and the ``'*'`` wildcard (per-peer-only grant).

        ``'*'`` here would bypass the shared-union bound on scoped reads
        (see :func:`_reject_degenerate_project_slugs`); blank slugs would
        match every untagged record in SQL.
        """
        _reject_degenerate_project_slugs(
            "FederationConfig.shared_projects", value, allow_wildcard=False
        )
        return value

    @field_validator("index_title_blocklist")
    @classmethod
    def _index_title_blocklist_compiles(cls, value: list[str]) -> list[str]:
        """Q10.9 title-regex gate — every pattern must be a valid regex.

        The patterns are applied at BOTH federation-index gates (serve
        and import) by :func:`vesmaro.compact.title_matches_blocklist`.
        A pattern that does not compile is a config error: failing at
        the config boundary (startup) instead of at sync time keeps the
        operator's blocklist intent fail-closed — a silently skipped
        pattern would quietly widen the export surface.
        """
        for pattern in value:
            if not pattern:
                raise ValueError("index_title_blocklist: blank pattern is not a valid regex")
            try:
                re.compile(pattern)
            except re.error as exc:
                raise ValueError(
                    f"index_title_blocklist: pattern {pattern!r} does not compile: {exc}"
                ) from exc
        return value

    @model_validator(mode="after")
    def _meta_poll_peers_known(self) -> FederationConfig:
        """Explicit ``meta_poll.peers`` ids must exist in the ``peers`` map.

        A typo'd peer id would otherwise be a silently never-polled
        entry (the poller resolves the list verbatim). The ``"all"``
        form tracks the map by construction and needs no check.
        """
        explicit = self.meta_poll.peers
        if isinstance(explicit, list):
            unknown = [p for p in explicit if p not in self.peers]
            if unknown:
                raise ValueError(
                    f"meta_poll.peers: unknown peer id(s) {unknown} — "
                    f"configured federation.peers keys: {sorted(self.peers)}"
                )
        return self


class ScannerConfig(BaseModel):
    """Background secrets scanner configuration (Layer 2 defence-in-depth).

    ArchCom 2026-07-17 federation contract §2.2.1 — the background
    scanner periodically re-scans the whole corpus for secrets missed
    by the write-path scanner (Layer 1) and auto-tags
    ``mnemos:no-federate`` so the record is excluded from all external
    exchange. It re-uses :func:`vesmaro.secrets_detector.detect_secrets`
    unchanged (DRY — one source of truth for patterns).

    Fields:
        enabled: When ``False``, ``BackgroundScanner.start()`` is a
            no-op and the scanner does not run on the configured
            interval. Defaults to ``True`` (defence-in-depth is on by
            default — operators who want to disable it must do so
            explicitly).
        interval_hours: Wall-clock interval between automatic scan
            passes. Default 6h per contract §2.2.1. Clamped to
            ``[1, 168]`` — anything below 1h is wasteful, anything
            above a week defeats the purpose of catching false
            negatives in a timely manner.
        incremental: When ``True`` (default), ``run_scan`` only scans
            records whose ``created_at`` OR ``updated_at`` is newer than
            the last successful scan timestamp. When ``False``, every
            scan is a full corpus scan. The CLI ``--full`` flag forces
            ``incremental=False`` for one run.
    """

    enabled: bool = True
    interval_hours: int = Field(default=6, ge=1, le=168)
    incremental: bool = True


class MeshTCPTLSConfig(BaseModel):
    """TLS material for the optional mesh TCP leg (ADR-0019 option 1).

    Chart-facing key alignment (ADR-0019 amendment 3d): the helm values
    are ``mesh.tcp.tls.existingSecret`` — the app-side snake_case name
    is :attr:`existing_secret`. The chart mounts that Secret
    (``mnemos-core-grpc-tls``, amendment 3e: the third server-identity
    leaf under the COMMON mesh CA) and renders the three file paths
    below; the app itself only ever reads files, never the cluster API.

    Fields:
        existing_secret: NAME of the Kubernetes Secret holding the
            core-identity leaf (chart-facing, amendment 3d/3e).
            Informational for core — the process consumes only the
            mounted file paths below. Kept so the app config mirrors
            the chart values 1:1 and operators can cross-check the
            render. Empty = not deployed via the chart (compose/bare).
        cert_file: PEM file with the mnemos-core server-identity leaf
            (from the mesh CA). Mounted from the Secret above — the
            path is a deployment concern, never hardcoded.
        key_file: PEM private key matching :attr:`cert_file`.
        ca_file: PEM bundle with the mesh CA — the trust root for
            CLIENT-certificate verification on the TCP leg
            (``RequireAndVerifyClientCert``: every caller must present
            a mesh-CA certificate; anonymous TLS is rejected at the
            handshake).
    """

    existing_secret: str = Field(default="", max_length=253)
    cert_file: str = Field(default="", max_length=4096)
    key_file: str = Field(default="", max_length=4096)
    ca_file: str = Field(default="", max_length=4096)


class MeshTCPConfig(BaseModel):
    """Optional networked TCP leg for the MnemosCore gRPC server (ADR-0019).

    W2.5 dual-mode mesh: alongside the Unix-socket leg (sidecar default,
    unchanged) the same grpcio server can expose ``MnemosCore`` over TCP
    with mesh-CA mTLS — for the standalone mesh Deployment (Phase 2).
    Default OFF (amendment 3d): with ``enabled: false`` (the default)
    no TCP port is opened at all and the process behaves exactly as
    before — byte-identical render when disabled.

    Explicit choice only — no auto-fallback (ADR-0019 rejects option 2):
    the mesh binary takes an explicit ``mnemos.transport``; a config
    error on either side must surface, never be papered over by
    silently switching transports. Startup fail-fast (amendment 3c):
    a failed ``add_secure_port`` raises and crashes the process —
    silent degradation is forbidden (there is deliberately no k8s probe
    on 8790; CrashLoop is the visibility mechanism).

    Fields:
        enabled: Master switch for the TCP leg. Default ``False``.
            Requires the parent ``mesh.enabled: true`` (the gRPC server
            itself is only constructed then — validated here, at the
            config boundary, so a ``tcp.enabled`` without ``mesh.enabled``
            is an immediate config error, not a silently missing leg).
        port: TCP port to listen on. Default ``8790`` (confirmed free at
            every layer, amendment 3a). ``0`` = bind an ephemeral port
            (tests/local diagnostics only — the actual bound port is
            logged at startup and exposed on the running server).
        bind: Bind address. Default ``127.0.0.1`` — Phase 1 (sidecar
            form) per amendment 3b: loopback is not policed by
            NetworkPolicy. Phase 2 (standalone mesh) opens
            ``0.0.0.0`` plus an ingress rule in the mesh-owned
            ``mnemos-mesh-peer-allow`` — an operator action, never a
            default.
        tls: TLS material (see :class:`MeshTCPTLSConfig`). Required in
            full (leaf cert + key + mesh-CA bundle) when ``enabled`` —
            there is no plaintext TCP mode on this leg.
    """

    enabled: bool = False
    port: int = Field(default=8790, ge=0, le=65535)
    bind: str = Field(default="127.0.0.1", max_length=253)
    tls: MeshTCPTLSConfig = Field(default_factory=MeshTCPTLSConfig)

    @model_validator(mode="after")
    def _enabled_requires_tls_material(self) -> MeshTCPConfig:
        """Fail-fast at the config boundary: no TLS material, no TCP leg.

        ``tcp.enabled: true`` without all three TLS file paths is a
        config error, not a runtime surprise at server start (and
        certainly not a fallback to plaintext — that mode does not
        exist on this leg).
        """
        if self.enabled:
            missing = [
                name for name in ("cert_file", "key_file", "ca_file") if not getattr(self.tls, name)
            ]
            if missing:
                raise ValueError(
                    "mesh.tcp.tls: "
                    + ", ".join(missing)
                    + " required when mesh.tcp.enabled is true "
                    "(no plaintext TCP on this leg — ADR-0019)"
                )
        return self


class CodeGraphConfig(BaseModel):
    """Project code graph indexer knobs (ADR-0032 PG-0, ArchCom 2026-09-28).

    The graph is MEMORY, not a per-process index: the sidecar
    ``code_graph.db`` (slice 1) is rebuilt from sources. PG7 fail-closed
    limits are DEFAULT ON — we learn from DeusData's default-off
    mistake (ADR-0032 §5): a breach fails the WHOLE index, no partial
    graph is ever published. The limits are ceilings the indexer checks
    BEFORE committing its transaction, so a hostile or misconfigured
    root cannot balloon the sidecar store.

    Canonical env override: ``VESMARO_CODE_GRAPH__INDEX_MAX_FILES`` /
    ``VESMARO_CODE_GRAPH__INDEX_MAX_SOURCE_MB`` / ``VESMARO_CODE_GRAPH__AUTO_INDEX`` /
    ``VESMARO_CODE_GRAPH__AUTO_REINDEX_MIN_INTERVAL_SEC`` /
    ``VESMARO_CODE_GRAPH__AUTO_REGISTER_MAX_PROJECTS``.

    Fields:
        enabled: Master flag for the project-graph tool surface (the
            10 graph MCP tools and the ``/graph/`` REST namespace,
            ADR-0032 §3.3). DEFAULT ON (owner decision 2026-09-28
            «graphs on by default»: ecosystem components build on the
            graphs, so they are first-class, not an opt-in). Set
            ``false`` to hide the graph from agents entirely.
        index_max_files: Hard cap on indexed files per project.
            Default 20000 (ADR-0032 §3.4).
        index_max_source_mb: Hard cap on total source bytes per
            project, MiB. Default 500 (ADR-0032 §3.4).
        beacon: The recall beacon v1 (ADR-0032 §3.5, slice 6): ONE tail
            line appended to the ``assemble_context`` output AFTER the
            budget stage — outside the budget blocks, never pinned,
            never a pipeline stage. Effective only when ``enabled`` is
            on. Default ON (it is the discovery surface for the ten
            tools; without it the graph is invisible to agents).
        watch: The ``watch_start`` poll registrar (contract §3.2
            trigger (b), slice 6): registers a light poll task INSIDE
            the server process (no daemon) that checks graph-file
            mtime+size on an adaptive interval and reindexes on actual
            changes (audit reason ``watch``). DEFAULT ON (owner
            decision 2026-09-28): the poll is inert until a project
            is EXPLICITLY registered via ``watch_start`` (it never
            seeds a first index and never runs uninvited), so the
            default costs nothing while keeping the surface usable
            without a second operator toggle.
        watch_max_registrations: Global cap on ACTIVE watch
            registrations per process (contract §3.2: per-session 1 /
            global 8 — an MCP call carries no session context, so the
            session cap is not enforceable; the global cap + manual
            ``watch_stop`` is the slice-6 binding). A registration
            beyond the cap is refused.
        watch_base_interval_sec / watch_interval_per_500_files /
        watch_max_interval_sec: The adaptive poll interval model
            (contract §3.2): ``base + (files // 500) * per_500``, capped
            — 5s base, +1s per 500 files, 60s cap. Exposed as config
            fields so tests can shrink them (injection) and operators
            can widen them; the DEFAULTS are the contract's numbers.
        auto_index: Native auto-indexing (PG-0.5, owner directive
            2026-09-29): the first contact with a project through the
            MCP dispatcher or the ``pre_llm_call`` hook emits an
            activity HINT that (a) auto-registers the project when its
            cwd carries a project marker (.git / pyproject.toml /
            package.json / go.mod / Cargo.toml) and (b) runs the
            first index / a stale reindex in the BACKGROUND on the
            shared scheduler thread — zero explicit calls, zero
            instructions. DEFAULT ON (the whole point of the
            directive); set ``false`` to fall back to the manual
            ``mnemos_index_project`` / ``watch_start`` triggers only.
        auto_reindex_min_interval_sec: Per-project throttle for the
            auto path: the sidecar ``graph_meta`` stamp
            ``last_auto_action`` gates consecutive auto actions on the
            same project. Default 300s; 0 disables the throttle (test
            injection — the same discipline as the watch interval
            fields).
        auto_register_max_projects: Global cap on AUTO-registered
            projects (PG-0.5 fix-slice, PR #443 review P2-1): counted
            through the ``auto-registered by`` description marker (the
            projects table has no ``registered_by`` column — the marker
            IS the provenance). A hint past the cap is a silent skip
            with an ``auto-register-capped`` audit row, never an error
            to the caller; operator-registered projects and root-reuse
            of an existing project never count against the cap.
    """

    enabled: bool = True
    index_max_files: int = Field(default=20_000, ge=1)
    index_max_source_mb: int = Field(default=500, ge=1)
    beacon: bool = True
    watch: bool = True
    auto_index: bool = True
    auto_reindex_min_interval_sec: float = Field(default=300.0, ge=0.0)
    auto_register_max_projects: int = Field(default=64, ge=1)
    watch_max_registrations: int = Field(default=8, ge=1)
    watch_base_interval_sec: float = Field(default=5.0, ge=0.01)
    watch_interval_per_500_files: float = Field(default=1.0, ge=0.0)
    watch_max_interval_sec: float = Field(default=60.0, ge=0.1)


class VitalsConfig(BaseModel):
    """Passive assemble metrics into the vitals sidecar (ADR-0026).

    Phase A of the memory-value observability track (ArchCom core
    ``7ec9dda3``; implementation developed in the mnemos-vitals repo,
    vendored under ``vesmaro.metrics``). Default-on in local-first: a
    local attacker holding the sidecar already holds the main store
    with full content, so collection adds no exposure.

    The collection boundaries are EXHAUSTIVE: the MCP
    ``mnemos_assemble_context`` handler and the ``pre_llm_call`` hook.
    REST ``POST /context/assemble`` is deliberately NOT a boundary —
    it is the multi-principal surface C3 exists to protect; do not add
    collection there without revisiting C3.
    """

    enabled: bool = True
    # C3 config-lint: per-request rows carry a session column; under
    # api.auth_enabled=true (multi-principal) collection refuses to
    # start without this explicit acknowledgement. The server keeps
    # running without metrics — refusal, never a crash.
    ack_session_collection: bool = False
    # C4: nightly DELETE + VACUUM cadence for the sidecar (TTLs are
    # born-final: verb 30d, hourly rollup 400d, assemble family 90d).
    retention_interval_sec: int = Field(default=24 * 3600, ge=3600)


class MeshConfig(BaseModel):
    """mnemos-mesh gRPC client configuration (Phase 3, issue #105 M3).

    ArchCom 2026-07-17 federation contract §3.1. This section governs the
    Python gRPC client (:class:`vesmaro.mesh_client.MeshClient`) that talks
    to the ``mnemos-mesh`` Go binary over a Unix socket. The mesh is a
    dumb transport (criterion 1); moderation and storage stay in Python
    (criterion 2/11). This section is OFF by default — an operator opts in
    by setting ``enabled: true`` after deploying the mesh binary.

    Fields:
        socket_path: Filesystem path to the ``mnemos-mesh`` Unix socket.
            The mesh binary creates the socket; mnemos connects to it.
            Default ``/run/mnemos/core.sock`` (systemd-tmpfiles convention
            for runtime sockets owned by the mnemos user).
        enabled: Master switch. When ``False`` (default), :class:`MeshClient`
            is not constructed and the MCP/HTTP paths do not attempt to
            talk to the mesh. Operators enable it after deploying the mesh.
        timeout_s: Per-RPC deadline in seconds. Short enough that a dead
            mesh is noticed quickly, long enough for a local Unix-socket
            round trip. Default 2.0s.
        socket_group_access: Grant group read/write on the Unix socket
            (and group rwx on its parent dir) so a mesh binary running as
            a DIFFERENT uid but the SAME gid can dial it. For deployments
            where the socket dir is a shared volume whose group ownership
            is already solved outside the process (Kubernetes fsGroup,
            compose ``user: <uid>:<gid>``). When ``False`` (default) the
            socket is ``0600`` and the dir ``0700`` — mnemos user only.
            Additive (W2 native serve wiring): existing configs behave
            exactly as before.
        tcp: Optional networked TCP leg on the SAME grpcio server
            (ADR-0019 option 1, W2.5). Default OFF — see
            :class:`MeshTCPConfig`. Additive: existing configs (no
            ``tcp:`` section) parse unchanged and open no TCP port.
    """

    socket_path: str = "/run/mnemos/core.sock"
    enabled: bool = False
    timeout_s: float = Field(default=2.0, gt=0.0, le=60.0)
    socket_group_access: bool = False
    tcp: MeshTCPConfig = Field(default_factory=MeshTCPConfig)

    @model_validator(mode="after")
    def _tcp_requires_master_switch(self) -> MeshConfig:
        """``mesh.tcp.enabled`` without ``mesh.enabled`` is a config error.

        The MnemosCore gRPC server (both legs) is only constructed when
        ``mesh.enabled`` is true; a TCP-only opt-in would therefore be a
        silently missing leg. Reject it at the config boundary with a
        message that names the fix.
        """
        if self.tcp.enabled and not self.enabled:
            raise ValueError(
                "mesh.tcp.enabled requires mesh.enabled: true — the gRPC server "
                "(both the Unix-socket and the TCP leg) only starts under the "
                "mesh master switch"
            )
        return self


# ── Issue #139: legacy short env-name compatibility ──────────────────────────
#
# ``Settings`` maps env vars with the ``VESMARO_`` prefix + ``__`` nesting, so
# the canonical names for the nested ``mnemos`` section fields are
# ``VESMARO_MNEMOS__DATA_DIR`` / ``VESMARO_MNEMOS__VAULT_PATH``. Historically the
# repo docs and ``scripts/mcp-setup.sh`` advertised the shorter
# ``VESMARO_DATA_DIR`` / ``VESMARO_VAULT__VAULT_PATH`` forms, which
# pydantic-settings silently ignores (no matching field). The mapping below
# restores those two short names as compatibility aliases. Scope is
# deliberately fixed to these two — this is NOT a general renaming engine.

_ENV_COMPAT_ALIASES: Final[dict[str, tuple[str, str]]] = {
    # legacy env name            -> (settings section, field)
    "VESMARO_DATA_DIR": ("mnemos", "data_dir"),
    "VESMARO_VAULT__VAULT_PATH": ("mnemos", "vault_path"),
}


class _EnvCompatAliasSettingsSource(PydanticBaseSettingsSource):
    """Settings source mapping legacy short env names (#139) to nested fields.

    Reads the process environment (NOT ``.env`` files — that is a separate,
    lower-priority source) on every call, so each ``Settings()`` construction
    observes the current ``os.environ``. Empty-string values are treated as
    unset.
    """

    def get_field_value(self, field: FieldInfo, field_name: str) -> tuple[Any, str, bool]:
        # Not consulted by the source-merge machinery (only __call__ is);
        # required by the PydanticBaseSettingsSource ABC.
        return None, field_name, False

    def __call__(self) -> dict[str, Any]:
        data: dict[str, dict[str, Any]] = {}
        for alias, (section, field_name) in _ENV_COMPAT_ALIASES.items():
            value = os.environ.get(alias, "")
            if value:
                data.setdefault(section, {})[field_name] = value
        return data


class Settings(BaseSettings):
    mnemos: MnemosConfig = MnemosConfig()
    embedding: EmbeddingConfig = EmbeddingConfig()
    search: SearchConfig = SearchConfig()
    api: ApiConfig = ApiConfig()
    mcp: McpConfig = McpConfig()
    watcher: WatcherConfig = WatcherConfig()
    llm: LLMConfig = LLMConfig()
    automation: AutomationConfig = AutomationConfig()
    runtime: RuntimeConfig = RuntimeConfig()
    ccr: CCRConfig = CCRConfig()
    hooks: HooksConfig = HooksConfig()
    lanes: LanesConfig = LanesConfig()
    cache_aligner: CacheAlignerConfig = CacheAlignerConfig()
    output_style: OutputStyleConfig = OutputStyleConfig()
    federation: FederationConfig = FederationConfig()
    scanner: ScannerConfig = ScannerConfig()
    mesh: MeshConfig = MeshConfig()
    code_graph: CodeGraphConfig = CodeGraphConfig()
    vitals: VitalsConfig = VitalsConfig()
    logging: LoggingConfig = LoggingConfig()
    # M5: declarative policy rules (loaded from YAML or set programmatically)
    policies: dict[str, Any] = Field(default_factory=dict)

    model_config = {
        "env_prefix": "VESMARO_",
        "env_nested_delimiter": "__",
        "env_file": ".env",
        "env_file_encoding": "utf-8",
    }

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        """Insert the #139 legacy alias source between env and dotenv sources.

        Resulting precedence for ``mnemos.data_dir`` / ``mnemos.vault_path``
        (high → low; sources deep-merge, so higher priority wins per field):

        1. Init kwargs — ``load_settings()`` passes the YAML config file here,
           so an explicit file value beats env vars. This mirrors the
           pre-existing pydantic-settings behaviour of canonical names
           (verified against pydantic-settings 2.14.2: init > env).
        2. Canonical env: ``VESMARO_MNEMOS__DATA_DIR`` /
           ``VESMARO_MNEMOS__VAULT_PATH``.
        3. Compat alias (this source): ``VESMARO_DATA_DIR`` /
           ``VESMARO_VAULT__VAULT_PATH`` — honoured only when neither the file
           nor the canonical name provides the field. A short alias therefore
           never overrides an explicit config-file value and never wins
           against the canonical name; it only fills the gap that previously
           fell through to the defaults.
        4. ``.env`` dotenv file.  5. Field defaults.
        """
        return (
            init_settings,
            env_settings,
            _EnvCompatAliasSettingsSource(settings_cls),
            dotenv_settings,
            file_secret_settings,
        )

    def resolve_paths(self) -> None:
        self.mnemos.vault_path = self.mnemos.vault_path.expanduser().resolve()
        self.mnemos.data_dir = self.mnemos.data_dir.expanduser().resolve()
        # Resolve log_file only if non-empty; an empty Path("") becomes "."
        # which means "stderr only" — leave it as an empty Path().
        log_str = str(self.logging.log_file).strip()
        if log_str and log_str != ".":
            self.logging.log_file = self.logging.log_file.expanduser().resolve()
        else:
            self.logging.log_file = Path()

    def apply_runtime_env(self) -> None:
        """Apply conservative thread caps unless explicitly overridden by user env."""
        threads = str(self.runtime.cpu_threads)
        defaults = {
            "OMP_NUM_THREADS": threads,
            "OPENBLAS_NUM_THREADS": threads,
            "MKL_NUM_THREADS": threads,
            "NUMEXPR_NUM_THREADS": threads,
            "VECLIB_MAXIMUM_THREADS": threads,
            "BLIS_NUM_THREADS": threads,
            "TOKENIZERS_PARALLELISM": "false",
        }
        for key, value in defaults.items():
            os.environ.setdefault(key, value)

    @property
    def db_path(self) -> Path:
        return self.mnemos.data_dir / self.mnemos.db_name

    def migrate_layout(self) -> list[str]:
        """Migrate scattered old paths to the consolidated ``~/.mnemos/`` layout.

        Detection rules (all idempotent — only moves if old exists AND new doesn't):

        * Old data dir ``~/.mnemos`` containing ``mnemos.db`` (and optionally
          ``vectors.db``) → moved to ``~/.mnemos/data/``.
        * Old vault ``~/mnemos-vault/`` → moved to ``~/.mnemos/vault/``.

        The config file ``~/.mnemos/config.yaml`` stays in place — it was
        already at the root. If the old ``~/.mnemos`` dir contained a
        ``config.yaml``, it is left in place (the new layout keeps config at
        the root, not under ``data/``).

        Returns a list of human-readable descriptions of what was moved
        (empty if nothing was migrated).
        """
        actions: list[str] = []
        home = Path.home()
        new_data = self.mnemos.data_dir
        new_vault = self.mnemos.vault_path

        # ── Data dir migration ────────────────────────────────────────────
        # Old layout: ~/.mnemos/mnemos.db (and vectors.db) directly under root.
        # New layout: ~/.mnemos/data/mnemos.db
        # Only migrate if the *default* data_dir is in use (i.e. the user
        # hasn't overridden it to a custom path). If data_dir was overridden
        # via config/env, we respect that and skip migration.
        old_data_root = home / ".mnemos"
        default_new_data = (home / ".mnemos" / "data").resolve()
        if (
            new_data == default_new_data
            and old_data_root.is_dir()
            and (old_data_root / "mnemos.db").exists()
            and not (new_data / "mnemos.db").exists()
        ):
            new_data.mkdir(parents=True, exist_ok=True)
            for item in old_data_root.iterdir():
                # Don't move config.yaml, data/, vault/, logs/, cache/ — those
                # are either already in the right place or belong at root.
                if item.name in ("config.yaml", "data", "vault", "logs", "cache"):
                    continue
                dest = new_data / item.name
                if not dest.exists():
                    shutil.move(str(item), str(dest))
                    actions.append(f"data: {item.name} → {dest}")
                    logger.info("migrate_layout: moved %s → %s", item, dest)

        # ── Vault migration ───────────────────────────────────────────────
        # Old layout: ~/mnemos-vault/
        # New layout: ~/.mnemos/vault/
        old_vault = home / "mnemos-vault"
        default_new_vault = (home / ".mnemos" / "vault").resolve()
        if new_vault == default_new_vault and old_vault.is_dir() and not new_vault.exists():
            new_vault.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(old_vault), str(new_vault))
            actions.append(f"vault: {old_vault} → {new_vault}")
            logger.info("migrate_layout: moved vault %s → %s", old_vault, new_vault)

        return actions


def find_config_file(config_path: str | Path | None = None) -> Path | None:
    """Resolve the config file :func:`load_settings` would load, or ``None``.

    Zero-config support (ADR-0017 Phase 0, D6): a ``None`` return means no
    user config exists anywhere on the search path, so ``load_settings``
    falls back to the built-in safe defaults (loopback bind, storage under
    ``~/.mnemos/``). Surfaces such as ``mnemos serve`` use this to tell the
    zero-config profile apart from an explicit config.

    Search order (identical to :func:`load_settings`):
      1. Explicit config_path argument
      2. VESMARO_CONFIG env var
      3. ./config.yaml in cwd
      4. ~/.mnemos/config.yaml

    Env handling for ``vesmaro.data_dir`` / ``vesmaro.vault_path`` (per field,
    high → low; full contract in ``Settings.settings_customise_sources``):
      config-file value > canonical env (``VESMARO_MNEMOS__DATA_DIR`` /
      ``VESMARO_MNEMOS__VAULT_PATH``) > legacy short alias (``VESMARO_DATA_DIR``
      / ``VESMARO_VAULT__VAULT_PATH``, issue #139 compatibility) > ``.env``
      file > defaults.
    """
    if config_path is None:
        env_config = os.environ.get("VESMARO_CONFIG", "")
        candidates: list[Path | None] = [
            Path(env_config) if env_config else None,
            Path.cwd() / "config.yaml",
            Path.home() / ".mnemos" / "config.yaml",
        ]
    else:
        candidates = [Path(config_path)]

    for candidate in candidates:
        if candidate and candidate.is_file():
            return candidate
    return None


def load_settings(config_path: str | Path | None = None) -> Settings:
    """Load settings from YAML config file with env var overrides.

    Search order:
      1. Explicit config_path argument
      2. VESMARO_CONFIG env var
      3. ./config.yaml in cwd
      4. ~/.mnemos/config.yaml

    When no config file is found (zero-config profile), the built-in
    defaults apply: loopback-only API bind (127.0.0.1:8787), storage
    auto-created under ``~/.mnemos/``, FTS5 lexical recall active with or
    without an embedding provider (vector leg degrades non-fatally).
    See :func:`find_config_file` for programmatic detection.
    """
    found = find_config_file(config_path)
    config_data: dict[str, Any] = {}
    if found is not None:
        with found.open() as fh:
            config_data = yaml.safe_load(fh) or {}

    settings = Settings(**config_data)
    settings.resolve_paths()
    settings.migrate_layout()
    settings.apply_runtime_env()
    _warn_federation_mtls_pinning_off(settings)
    return settings


def _warn_federation_mtls_pinning_off(settings: Settings) -> None:
    """Warn when federation is active but a peer has mTLS pinning off.

    Federation is considered active when at least one peer is configured
    (``settings.federation.peers`` is non-empty — there is no separate
    ``enabled`` flag; a non-empty peers map is the activation signal).
    For each such peer whose ``mtls_cert_fingerprint`` is ``None``, emit
    a warning so the operator knows pinning is OFF (operator opt-out).
    This is non-breaking — a warning only, not a refusal. ADR-0016
    recommends pinning for networked deployments; the warning makes the
    opt-out visible at config-load time rather than silently accepted.
    """
    fed = settings.federation
    if not fed.peers:
        return
    for peer_id, peer in fed.peers.items():
        if peer.mtls_cert_fingerprint is None:
            logger.warning(
                "Peer '%s' has no mTLS cert fingerprint — pinning OFF "
                "(operator opt-out). Set mtls_cert_fingerprint to "
                "SHA-256 hex to enable pinning.",
                peer_id,
            )
