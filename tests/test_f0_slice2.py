"""ADR-0027 Phase 0, slice 2 (epic #308, checklist items 3-5 + review notes).

Covers:

1. **Code lens preset** (``vesma.lens`` — a code-defined, query-
   conditioned projection that only narrows): resolve/activate/admit
   unit pins, the assembly-level narrowing pin (subset + order), the
   inactive-lens identity projection, and the DEFAULT-ABSENT byte
   identity pin (no lens → no ``lens`` keys anywhere).
2. **Optional ``task`` parameter on ``assemble_context``** (tail-only,
   ADR-0027 invariant 1): boundary validation (bare slug), the
   intersection-doctrine narrowing through the whole six-stage pipeline
   (task rows only — never foreign tasks, never task-less rows, never
   foreign projects), the echo/stats additive-only discipline, the
   provenance/prefix stability across task and default assemblies of
   one session, the governance-lanes composition, and the harness
   carriers (``pre_llm_call`` hook, ``dispatch_hook``, REST
   ``/hooks/pre_llm_call``).
3. **Write-side doc-grouping validation** (slice-1 review item 1): the
   DTO boundary (``MemoryCreate``/``MemoryUpdate`` field validators — a
   partial ``{doc_id, chunk_idx, heading_path}`` triple fails
   construction, REST gets a 422) and the store-boundary belt-and-
   suspenders in ``MemoryManager.add``/``update`` (a validator-bypassed
   ``model_construct`` DTO still cannot persist a partial triple).

Item 4 of the slice-2 scope (the vector-leg agent predicate) was an
ANALYSIS deliverable of slice 2; the Ф1-PREP wave (#360 review item 5)
has since landed it as manager-side resolve-time guards (vector leg +
graph leg mirror) — pinned in ``tests/test_a9_agent_predicate.py``.

Ф1-PREP WAVE additions (#360 review, before any lens default-enablement
or Ф1 runs): the lens signal-#3 tightening pins (negative pins WITH
parentheses; the empty-group/word-only prose shapes no longer activate),
the ``task="t1\\n"`` boundary pin (``\\Z`` anchor), the lens x lanes
composition contract (§2e — governance rows ARE lens-stripped before the
applyTo partition; symmetric with the contentType mode filter), and the
graph-edge strict-subset fixture (both edge kinds).

#368 WAVE additions (pre lens default-enablement gate): the signal-#4
flush-paren + evidence tightening (the dotted-receiver twin of the #3
rule — "compare node.js (the runtime)" / "e.g (note)" no longer match the
pattern), the signal-#3 no-space comma residual pinned as documented
behavior ("options(a, b, or c)"), and the ``\\Z`` anchor sweep across
``_PROJECT_RE``/``_AGENT_RE``/``_VESMA_TAG_RE`` (newline pins in
``tests/test_f0_task_scope.py``).

Test embedder: ``_HashEmbedder`` (deterministic hashed bag-of-tokens) —
same rationale as the slice-1 suite: a MagicMock embedder cannot
discriminate and would fake the ranking/gate semantics under test.

MUTATION PROTOCOL — the load-bearing pins are mutation-verified
(mutant → failing test, documented in the report; unmutated code GREEN):

* ``test_lens_absent_default_is_byte_identical`` — M1 changes the
  ``lens`` default in the ``MemoryManager.assemble_context`` wrapper
  (the effective call boundary — the core function receives the
  wrapper's explicit ``None``) to a member value; the additive-only
  echo keys appear on the default path.
* ``test_task_scoped_assembly_intersects_every_scope`` — M2 drops the
  ``tags=search_tags`` threading in ``_recall_stage`` (foreign-task and
  task-less rows surface in a task-scoped assembly).
* ``test_lens_projection_is_subset_and_order_preserving`` — M3 makes
  the lens block reorder survivors (``candidates = kept[::-1]``) — the
  order-preservation assertion fails (the fixture keeps TWO code
  survivors so the pin is non-vacuous).
* ``test_manager_add_rejects_bypassed_partial_triple`` — M4 removes the
  ``doc_grouping_from_metadata`` call from ``MemoryManager.add`` (a
  ``model_construct`` partial triple persists).
"""

from __future__ import annotations

import hashlib
import math
import re
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import ValidationError

from vesma.api import main as api_main
from vesma.api.main import app as real_app
from vesma.api.main import lifespan
from vesma.config import Settings
from vesma.hooks import dispatch_hook, pre_llm_call
from vesma.lens import _QUERY_CODE_SIGNALS as _LENS_SIGNALS
from vesma.lens import Lens, lens_active, lens_admits, resolve_lens
from vesma.manager import MemoryManager
from vesma.models import (
    Memory,
    MemoryCreate,
    MemorySource,
    MemoryStatus,
    MemoryUpdate,
    build_doc_grouping_metadata,
)

PROJECT = "s2-proj"
PROJECT_B = "s2-other"
AGENT = "s2-agent"
SESSION = "s2-session"

#: Content the ingest classifier stamps ``content_type="code"`` on
#: (three code-pattern lines for ``detect_profile``).
CODE_CONTENT = (
    "def load_settings():\n    import json\n    const cache = {}\n    return json.loads(raw)"
)
#: Prose counterpart — classified "prose" by the binary partition.
PROSE_CONTENT = (
    "The deploy pipeline uses hybrid search with rank fusion and "
    "provenance wrappers for every recalled block."
)

#: A query that lexically hits both rows AND carries a code signal:
#: ``load_settings(path, env)`` — a call expression with code-typical
#: arguments (comma list, Ф1-PREP tightening — the old empty-group shape
#: ``load_settings()`` no longer activates the lens).
CODE_QUERY = "how does load_settings(path, env) parse the config"


class _HashEmbedder:
    """Deterministic test embedder: hashed bag-of-tokens vectors (slice-1 twin)."""

    DIM = 256

    def embed(self, text: str) -> list[float]:
        vec = [0.0] * self.DIM
        for tok in re.findall(r"[a-z0-9]+", text.lower()):
            h = int(hashlib.md5(tok.encode()).hexdigest(), 16)
            vec[h % self.DIM] += 1.0
        norm = math.sqrt(sum(v * v for v in vec)) or 1.0
        return [v / norm for v in vec]


def _settings(tmp: Path, *, lanes_enabled: bool = False, graph_walk: bool = False) -> Settings:
    settings = Settings(
        vesma={
            "vault_path": str(tmp / "vault"),
            "data_dir": str(tmp / "data"),
            "db_name": "test.db",
            "graph_walk": graph_walk,
        },
        scanner={"enabled": False},  # type: ignore[arg-type]
        lanes={"enabled": lanes_enabled},  # type: ignore[arg-type]
    )
    settings.resolve_paths()
    return settings


@pytest.fixture
def mgr(tmp_path: Path) -> Iterator[MemoryManager]:
    manager = MemoryManager(_settings(tmp_path))
    manager._embedder = _HashEmbedder()
    yield manager
    manager.close()


@pytest.fixture
def lanes_mgr(tmp_path: Path) -> Iterator[MemoryManager]:
    manager = MemoryManager(_settings(tmp_path, lanes_enabled=True))
    manager._embedder = _HashEmbedder()
    yield manager
    manager.close()


def _row(
    manager: MemoryManager,
    content: str,
    *,
    project: str = PROJECT,
    agent: str = AGENT,
    task: str | None = None,
    subtype: str = "mnemos:learning",
    title: str | None = None,
) -> Memory:
    """Add one published row with an explicit scope triple (+ task)."""
    tags = [f"project:{project}", f"agent:{agent}", subtype]
    if task is not None:
        tags.append(f"task:{task}")
    return manager.add(
        MemoryCreate(
            content=content,
            title=title,
            tags=tags,
            source=MemorySource.MCP,
            status=MemoryStatus.PUBLISHED,
        ),
        project=project,
        agent=agent,
    )


# ---------------------------------------------------------------------------
# 1a. Lens — unit semantics (resolve / activate / admit)
# ---------------------------------------------------------------------------


class TestLensUnit:
    def test_resolve_none_is_none(self) -> None:
        """Default = absent: None resolves to None (no lens, no projection)."""
        assert resolve_lens(None) is None

    @pytest.mark.parametrize("value", ["code", Lens.CODE])
    def test_resolve_members(self, value: str | Lens) -> None:
        assert resolve_lens(value) is Lens.CODE

    @pytest.mark.parametrize("bad", ["wide", "", "CODE", "prose", "code-lens"])
    def test_resolve_unknown_raises(self, bad: str) -> None:
        """Lens definitions are CODE under review — any other name is a
        boundary error (a named lens is an injection vector; fail loud)."""
        with pytest.raises(ValueError, match="unknown lens"):
            resolve_lens(bad)

    @pytest.mark.parametrize(
        "query",
        [
            "def run(self):",
            "call parse_args(verbose=True) first",
            "what does connect(host, port) do",
            "how do I call foo(max_retries)",
            "what does retry(backoff=5) do",
            "check src/vesma/manager.py search",
            "rewrite fetch(url) => Result",
            "class SettingsLoader: what fields",
            "obj.method(x.y) returns what",  # dotted ARG = evidence (#368)
            "what does std::vector hold",
        ],
    )
    def test_code_shaped_queries_activate(self, query: str) -> None:
        assert lens_active(Lens.CODE, query=query) is True

    @pytest.mark.parametrize(
        "query",
        [
            "how does the retry loop work",
            "deploy notes for the pipeline",
            "parse",
            "remember the quokka habitat survey",
            "class attendance was low",  # 'class' without definition syntax
            "import the CSV data",  # bare-word import is prose-ambiguous
            "import vesma.manager",  # dotted import: no code-extension path
            "define the function area for review",
            # Ф1-PREP item 1 — negative pins WITH parentheses (the #360
            # review false-fire class): prose parentheticals must not
            # activate the lens, whatever sits inside the group.
            "how does the retry loop work (with backoff)",  # THE review case
            "how does the retry loop work (backoff)",  # word-only group
            "how does parse () work",  # empty group (space = typography)
            "pick a color (red, green, blue)",  # prose list, spaced paren
            "the retry policy (and/or fallback)",  # slash is not evidence
            "see the notes (step-by-step guide)",  # hyphen is not evidence
            "retries happen (50% of runs)",  # percent is not evidence
            "the handler (the server's config)",  # quote/apostrophe neither
            # #368 item 1 — signal #4 negative pins (the review false-fire
            # class): a SPACE before the paren is English prose typography
            # (a parenthetical, not a call), and the old
            # receiver.identifier\s*\(\) shape admitted it.
            "e.g (note)",  # THE #368 review typo case
            "e.g (note the caveat)",  # spaced word-only group, dotted trigger
            "what does v2.build (the new builder) do",  # spaced prose group
            "check config.get (the getter)",  # spaced prose group
            # #388 item 3 — precision on WHICH shapes the S4 tightening
            # kills: the DOTTED-RECEIVER members (v2.build / config.get /
            # node.js / e.g) are S4-killable — their receiver.identifier
            # shape reaches the signal. The BARE-receiver shape below is
            # NOT S4-killable: "handle_request" matches no dotted
            # receiver, so signal #4 never sees it — its inertness here
            # rests on signal #3 alone (a SPACE before the group means
            # prose typography, and the word-only group carries no code
            # evidence either way). Kept as a full-lens negative.
            "compare handle_request (the slow path)",  # bare receiver — S3 inert
        ],
    )
    def test_prose_queries_do_not_activate(self, query: str) -> None:
        """Query-conditioned (E3 class ban): a prose question about code
        keeps the identity projection — its corpus legitimately includes
        prose notes about code. The parenthesized entries pin the Ф1-PREP
        signal-#3 tightening: only code-typical content INSIDE the parens
        (comma list, ``=``, underscored/dotted token, operator) activates;
        empty and word-only groups are prose parentheticals. The spaced
        entries pin the #368 signal-#4 tightening: the same flush-paren +
        evidence rule applies to the dotted receiver shape."""
        assert lens_active(Lens.CODE, query=query) is False

    @pytest.mark.parametrize(
        "query",
        [
            # #368 item 1 — the two VERBATIM review false-fire shapes,
            # pinned against the signal-#4 pattern itself: after the fix
            # the shape no longer matches (flush paren + evidence).
            # Since #388 the first shape is ALSO a full-lens negative —
            # the signal-#5 path-context requirement retired the last
            # lexicon overlap ("node.js" reads as ``.js``).
            "compare node.js (the runtime)",
            "e.g (note)",
        ],
    )
    def test_signal4_pattern_rejects_review_false_fires(self, query: str) -> None:
        """Unit pin on the hardened signal-#4 regex: no space-gap before
        ``(``, and the two review prose shapes no longer match it."""
        assert _LENS_SIGNALS[3].search(query) is None

    @pytest.mark.parametrize(
        "query",
        [
            # #368 acceptance — genuine code shapes activate (signal #4
            # where the dotted receiver is the trigger, signal #3 for the
            # bare-identifier calls; every group carries code evidence).
            "node.js(fs, cb) callback pattern",
            "trace handle_request(req, ctx)",
            "v2.build(cfg=2) fails",
            "run parse_csv(rows, opts)",
            "config.get(app_name) returns",
            "obj.method(x.y) returns what",  # dotted ARG = evidence
        ],
    )
    def test_code_queries_still_activate_after_signal4_tightening(self, query: str) -> None:
        """The #368 tightening only NARROWS: genuine flush-paren calls
        with code evidence keep activating (ADR-0027 invariant 6)."""
        assert lens_active(Lens.CODE, query=query) is True

    @pytest.mark.parametrize(
        "query",
        [
            # #368 item 2 — the signal-#3 no-space comma RESIDUAL is
            # DOCUMENTED behavior (the typographic heuristic: prose
            # parentheticals take a leading space, so a flush group is
            # read as a call). Pin it so future hardening cannot silently
            # regress these shapes.
            "options(a, b, or c)",
            "color(red, green, blue)",
        ],
    )
    def test_signal3_no_space_comma_residual_activates(self, query: str) -> None:
        """Pinned residual: a FLUSH group with a comma list activates even
        when its content reads as prose words — the leading space is the
        discriminator, and its absence is code typography."""
        assert lens_active(Lens.CODE, query=query) is True

    @pytest.mark.parametrize(
        "query",
        [
            # #387 rider (#388 item 2) — the signal-#4 NESTED-PAREN
            # acceptable loss, registered in the lens.py doctrine: the
            # group span ``[^()]*`` cannot see through inner parens, so
            # a nested-call argument never reaches the evidence check.
            # All losses safe-direction (ADR-0027 invariant 6): a missed
            # activation is the identity projection, never a narrowing.
            "obj.method(f(x)) returns what",
            "trace obj.method(f(x), 2) closely",
        ],
    )
    def test_signal4_nested_paren_loss_is_safe_direction(self, query: str) -> None:
        """ACCEPTABLE LOSS (registered): a method call whose argument
        carries an inner group does not activate the lens — the group
        span cannot see through the inner parens. Safe per ADR-0027
        invariant 6 (identity projection keeps every row)."""
        assert lens_active(Lens.CODE, query=query) is False

    @pytest.mark.parametrize(
        "query",
        [
            # The same nested-paren shape IS covered the moment another
            # signal fires — the loss is per-signal, not per-query.
            "obj.method(f(x)) — see src/vesma/lens.py for the span note",
            "how does def run(self): handle obj.method(f(x))",
        ],
    )
    def test_nested_paren_shape_rescued_by_other_signals(self, query: str) -> None:
        """The registered loss is bounded: a query carrying the nested
        shape AND a second code signal (a path-with-separator here, a
        def there) still activates."""
        assert lens_active(Lens.CODE, query=query) is True

    @pytest.mark.parametrize(
        "query",
        [
            # #388 — the accidental-rescue shape. The old lexicon read
            # the ".c" of "a.b.c" as the C extension; the path-context
            # requirement killed that read. The shape is INERT through
            # every signal: no separator, no fence, and the word-only
            # group carries no call evidence (S3/S4 need a comma, ``=``,
            # an underscored/dotted token, or an operator).
            "a.b.c(x)",
            "the a.b.c(x) shape from the issue",
        ],
    )
    def test_abcx_accidental_rescue_is_inert(self, query: str) -> None:
        """PINNED TRUTH (#388): "a.b.c(x)" does not activate the lens —
        neither the lexicon (no path context) nor the call shapes (the
        group "x" has no code evidence) fire. Safe direction."""
        assert lens_active(Lens.CODE, query=query) is False

    @pytest.mark.parametrize(
        "query",
        [
            # #388 main — the TWO verbatim issue prose shapes plus the
            # wider dotted-token prose class: signal #5 no longer fires
            # on an extension-only token. Prose dotted words ("node.js"
            # the runtime, "e.g" the abbreviation) are not file paths.
            "compare node.js (the runtime)",  # THE issue shape, verbatim
            "see node.js (v2) docs",  # THE issue shape, verbatim
            "the team upgraded node.js yesterday",
            "the config.toml mention in the meeting notes",
            "they pinned the gem version (see the note about .rb files)",
            "writing a lexer in go (the language, not the game)",
        ],
    )
    def test_prose_dotted_tokens_do_not_activate(self, query: str) -> None:
        """#388 main pin: an extension-only token in a prose query keeps
        the identity projection — "node.js" is the runtime's name, not a
        ``.js`` file reference. The signal-#5 lexicon requires path-ish
        context (a separator or a quote fence) before it fires."""
        assert lens_active(Lens.CODE, query=query) is False

    @pytest.mark.parametrize(
        "query",
        [
            # #388 main — genuine paths KEEP activating: a separator
            # inside the token run (slash — POSIX, backslash — Windows,
            # or a leading ./) or a quote/backtick fence around the token.
            "check src/vesma/manager.py search",
            "open benchmarks/experiments/e3_lanes/runner.py",
            "look at scripts/gen-proto.sh output",
            "run ./app.ts in dev mode",
            "review `src/vesma/lens.py` against the doctrine",
            "open 'config.toml' in the editor",
            "the file at src\\vesma\\lens.py on windows",
        ],
    )
    def test_path_context_queries_activate(self, query: str) -> None:
        """#388 main pin: the narrowing keeps its true-path recall —
        slash-component paths (the registered non-extension shapes that
        MUST keep activating) and quoted/backticked filenames fire."""
        assert lens_active(Lens.CODE, query=query) is True

    @pytest.mark.parametrize(
        "query",
        [
            # Signal-#5 unit pin on the DISCRIMINATOR itself: the bare
            # extension token is inert, the same token with a separator
            # or a fence is a signal.
            "node.js",
            "node.js (the runtime)",
            "app.ts",
            "some.sql scripts they said",
        ],
    )
    def test_signal5_pattern_requires_path_context(self, query: str) -> None:
        """Unit pin on the hardened signal-#5 regex: an extension match
        without path context (separator or quote fence) does not match."""
        assert _LENS_SIGNALS[4].search(query) is None

    @pytest.mark.parametrize(
        "query",
        [
            "src/vesma/lens.py",
            "node.js/fs/index.js",
            "./run.sh",
            "`lens.py`",
        ],
    )
    def test_signal5_pattern_fires_on_path_context(self, query: str) -> None:
        """Unit twin of the negative pin: the same tokens WITH path
        context match the signal-#5 pattern."""
        assert _LENS_SIGNALS[4].search(query) is not None

    def test_admit_identity_when_inactive(self) -> None:
        assert lens_admits(Lens.CODE, query="deploy notes", content_type="prose") is True
        assert lens_admits(Lens.CODE, query="deploy notes", content_type="code") is True

    def test_admit_narrows_when_active(self) -> None:
        assert lens_admits(Lens.CODE, query="def f():", content_type="code") is True
        assert lens_admits(Lens.CODE, query="def f():", content_type="prose") is False


# ---------------------------------------------------------------------------
# 1b. Lens — assembly-level semantics
# ---------------------------------------------------------------------------


class TestLensAssembly:
    def _seed(self, manager: MemoryManager) -> dict[str, Memory]:
        return {
            "code": _row(manager, CODE_CONTENT, title="loader"),
            "code_b": _row(manager, CODE_CONTENT + "\n# second loader", title="loader b"),
            "prose": _row(manager, PROSE_CONTENT, title="deploy notes"),
        }

    def test_lens_projection_is_subset_and_order_preserving(self, mgr: MemoryManager) -> None:
        """ONLY NARROWS: the lens output is an ORDER-PRESERVING SUBSET of
        the lens-less corpus — no reordering, no promotion (M3 pin; the
        two code survivors make the order assertion non-vacuous)."""
        rows = self._seed(mgr)
        unlensed = mgr.assemble_context(session=SESSION, project=PROJECT, query=CODE_QUERY)
        lensed = mgr.assemble_context(
            session=SESSION, project=PROJECT, query=CODE_QUERY, lens="code"
        )
        unlensed_ids = [b["memory_id"] for b in unlensed["blocks"]]
        lensed_ids = [b["memory_id"] for b in lensed["blocks"]]
        # Subset: every lensed block was in the unlensed output.
        assert set(lensed_ids) <= set(unlensed_ids)
        # Order-preserving: the kept blocks keep their unlensed relative order.
        assert lensed_ids == [mid for mid in unlensed_ids if mid in set(lensed_ids)]
        # The narrowing actually fired on this fixture (not a vacuous pin):
        # BOTH code rows survive (order pin has two survivors), prose drops.
        assert rows["prose"].id in set(unlensed_ids)
        assert rows["prose"].id not in set(lensed_ids)
        assert {rows["code"].id, rows["code_b"].id} <= set(lensed_ids)
        # Telemetry: additive-only, present because the lens was selected.
        assert lensed["lens"] == "code"
        assert lensed["stats"]["recall"]["lens"] == {
            "name": "code",
            "active": True,
            "filtered": 1,
        }

    def test_inactive_lens_is_identity_projection(self, mgr: MemoryManager) -> None:
        """A prose query under the CODE lens narrows nothing: same blocks
        as the lens-less assembly (query-conditioned, E3 class ban)."""
        self._seed(mgr)
        unlensed = mgr.assemble_context(
            session=SESSION, project=PROJECT, query="deploy pipeline notes"
        )
        lensed = mgr.assemble_context(
            session=SESSION, project=PROJECT, query="deploy pipeline notes", lens="code"
        )
        assert [b["memory_id"] for b in lensed["blocks"]] == [
            b["memory_id"] for b in unlensed["blocks"]
        ]
        assert lensed["lens"] == "code"  # echoed — the caller sees what they asked
        assert lensed["stats"]["recall"]["lens"] == {
            "name": "code",
            "active": False,
            "filtered": 0,
        }

    def test_lens_absent_default_is_byte_identical(self, mgr: MemoryManager) -> None:
        """DEFAULT = ABSENT (M1 pin): without the parameter no ``lens``
        key exists anywhere in the result — the default result dict shape
        is the pre-Phase-0 shape, byte-identical."""
        self._seed(mgr)
        out = mgr.assemble_context(session=SESSION, project=PROJECT, query=CODE_QUERY)
        assert "lens" not in out
        assert "lens" not in out["stats"]["recall"]
        # Explicit None is the same default.
        out_none = mgr.assemble_context(
            session=SESSION, project=PROJECT, query=CODE_QUERY, lens=None
        )
        assert out_none == out

    def test_unknown_lens_rejected_at_boundary(self, mgr: MemoryManager) -> None:
        with pytest.raises(ValueError, match="unknown lens"):
            mgr.assemble_context(session=SESSION, project=PROJECT, lens="wide")


# ---------------------------------------------------------------------------
# 2a. task parameter — boundary validation
# ---------------------------------------------------------------------------


class TestTaskParamValidation:
    @pytest.mark.parametrize(
        "bad", ["Bad!", "", "  ", "task:t1", "t1 t2", "x" * 65, "рефакторинг", "t1\n"]
    )
    def test_invalid_task_rejected(self, mgr: MemoryManager, bad: str) -> None:
        with pytest.raises(ValueError, match="task must"):
            mgr.assemble_context(session=SESSION, project=PROJECT, task=bad)

    def test_trailing_newline_rejected_not_dead_tag(self, mgr: MemoryManager) -> None:
        """Ф1-PREP item 2 — the ``$``-anchor flaw: ``task="t1\\n"`` used to
        validate (``$`` also matches just before a trailing newline) and
        mint ``task:t1\\n`` — a tag the exact-membership search filter can
        never hit (a dead scope). The ``\\Z`` anchor rejects it at the
        boundary instead."""
        with pytest.raises(ValueError, match="task must match"):
            mgr.assemble_context(session=SESSION, project=PROJECT, task="t1\n")

    def test_prefixed_task_gets_actionable_error(self, mgr: MemoryManager) -> None:
        """The prefix is the boundary's job — point the caller at the slug."""
        with pytest.raises(ValueError, match="pass 't1'"):
            mgr.assemble_context(session=SESSION, project=PROJECT, task="task:t1")

    def test_valid_slugs_accepted(self, mgr: MemoryManager) -> None:
        """Boundary alphabet == the tag contract's (TASK_SLUG_RE shares the
        task: tag pattern): 1..64 chars of [a-z0-9_-]."""
        _row(mgr, "boundary slug probe content", task="a")
        out = mgr.assemble_context(session=SESSION, project=PROJECT, task="a", query="boundary")
        assert out["task"] == "a"


# ---------------------------------------------------------------------------
# 2b. task parameter — assembly semantics (intersection doctrine)
# ---------------------------------------------------------------------------


class TestTaskScopedAssembly:
    def _seed(self, manager: MemoryManager) -> dict[str, Memory]:
        rows = {
            "in_task": _row(manager, "quokka habitat survey field notes", task="t1"),
            "other_task": _row(manager, "quokka habitat survey field notes", task="t2"),
            "no_task": _row(manager, "quokka habitat survey field notes"),
            "foreign_project": _row(
                manager, "quokka habitat survey field notes", project=PROJECT_B, task="t1"
            ),
        }
        manager.vectors.wipe()  # FTS-only: the scope gates are the decider
        return rows

    def test_task_scoped_assembly_intersects_every_scope(self, mgr: MemoryManager) -> None:
        """M2 pin: the task tail narrows through the WHOLE pipeline — only
        same-project rows carrying the task tag surface (never the other
        task, never task-less rows, never foreign projects)."""
        rows = self._seed(mgr)
        out = mgr.assemble_context(
            session=SESSION, project=PROJECT, task="t1", query="quokka habitat"
        )
        ids = {b["memory_id"] for b in out["blocks"]}
        assert ids == {rows["in_task"].id}
        # Echo + telemetry: additive-only, present because the task was given.
        assert out["task"] == "t1"
        assert out["stats"]["recall"]["task_scoped"] is True

    def test_task_absent_default_is_byte_identical(self, mgr: MemoryManager) -> None:
        """Same discipline as the lens (M1 twin): no task → no ``task``
        keys anywhere; explicit None == omitted."""
        self._seed(mgr)
        out = mgr.assemble_context(session=SESSION, project=PROJECT, query="quokka habitat")
        assert "task" not in out
        assert "task_scoped" not in out["stats"]["recall"]
        out_none = mgr.assemble_context(
            session=SESSION, project=PROJECT, query="quokka habitat", task=None
        )
        assert out_none == out

    def test_task_tail_only_prefixes_untouched(self, mgr: MemoryManager) -> None:
        """ADR-0027 invariant 1 (tail-only): the task parameter changes
        WHICH blocks enter the per-call tail and NOTHING about the prefix
        contract — every block keeps the canonical provenance shape, and
        the session-scoped ``retrieved=`` stamp is IDENTICAL across the
        task and default assemblies of one session (byte-stable prefixes
        for harness-side KV caching)."""
        self._seed(mgr)
        default_out = mgr.assemble_context(session=SESSION, project=PROJECT, query="quokka habitat")
        task_out = mgr.assemble_context(
            session=SESSION, project=PROJECT, task="t1", query="quokka habitat"
        )
        prov_re = re.compile(
            r"^\[mnemos:[0-9a-f-]{36} project=\S+ status=\S+ origin=\S+ "
            r"(pipeline=\S+ )?v=\d+ retrieved=(\S+)\]$"
        )
        stamps: set[str] = set()
        for out in (default_out, task_out):
            for line in out["text"].splitlines():
                if line.startswith("[mnemos:"):
                    m = prov_re.match(line)
                    assert m is not None, f"provenance shape drift: {line}"
                    stamps.add(m.group(2))
        assert len(stamps) == 1  # one session → one first-assembly stamp

    def test_task_scoped_assembly_stable_across_calls(self, mgr: MemoryManager) -> None:
        """Per-call tail determinism: two identical task-scoped assemblies
        of one session produce byte-identical text (retrieved= is
        session-scoped, uuids fixed by the seeded store)."""
        self._seed(mgr)
        t1 = mgr.assemble_context(
            session=SESSION, project=PROJECT, task="t1", query="quokka habitat"
        )["text"]
        t2 = mgr.assemble_context(
            session=SESSION, project=PROJECT, task="t1", query="quokka habitat"
        )["text"]
        assert t1 == t2


class TestTaskGraphEdgeStrictSubset:
    """Ф1-PREP item 4 (#360 review) — the strict-subset pin on the GRAPH path.

    Slice-2's subset pin (``test_task_scoped_assembly_intersects_every_scope``)
    covers the FUSED leg only (the vector store is wiped, FTS decides). The
    graph edge leg is a second surfacing path with its own gate sequence, so
    the subset property is pinned here separately: an edge-sourced row
    surfaces in the DEFAULT assembly via the graph leg, and the task-scoped
    assembly stays a strict, order-preserving SUBSET of it — the tags gate
    binds to the graph leg (slice-1 M2), so a task assembly never surfaces
    an edge row the task-less assembly would not. Both edge kinds: the
    unconditional ``supersedes`` leg and the flag-gated ``relates_to`` walk
    (``mnemos.graph_walk``) share the gate sequence under test.
    """

    @pytest.mark.parametrize("kind", ["supersedes", "relates_to"])
    def test_task_assembly_stays_subset_on_graph_path(self, tmp_path: Path, kind: str) -> None:
        walk = kind == "relates_to"
        manager = MemoryManager(_settings(tmp_path, graph_walk=walk))
        manager._embedder = _HashEmbedder()
        try:
            anchor = _row(manager, "anchor tide schedule notes", task="t1")
            # Lexically disjoint from the query: the neighbour can surface
            # ONLY through the edge, never through the fused legs.
            neighbour = _row(manager, "dormant ledger reconciliation figures")
            manager.add_memory_edge(anchor.id, neighbour.id, kind=kind)
            manager.vectors.wipe()  # FTS + graph legs decide

            default_out = manager.assemble_context(
                session=SESSION, project=PROJECT, query="anchor tide"
            )
            default_ids = [b["memory_id"] for b in default_out["blocks"]]
            # Fixture sanity: the graph leg DID surface the neighbour in
            # the task-less assembly (the subset pin is not vacuous).
            assert neighbour.id in default_ids
            assert anchor.id in default_ids

            task_out = manager.assemble_context(
                session=SESSION, project=PROJECT, task="t1", query="anchor tide"
            )
            task_ids = [b["memory_id"] for b in task_out["blocks"]]
            # STRICT SUBSET, order-preserving (tail-only narrowing).
            assert set(task_ids) <= set(default_ids)
            assert task_ids == [mid for mid in default_ids if mid in set(task_ids)]
            assert task_ids == [anchor.id]
            assert task_out["task"] == "t1"
        finally:
            manager.close()


# ---------------------------------------------------------------------------
# 2c. task x lanes composition (intersection binds the governance leg)
# ---------------------------------------------------------------------------


class TestTaskLanesComposition:
    def test_task_narrows_governance_lanes_too(self, lanes_mgr: MemoryManager) -> None:
        """Symmetric narrowing: with lanes ON, a governance row without
        the assembly's task tag is OUTSIDE the task's admissibility set
        (intersection doctrine — a task-scoped assembly never surfaces a
        row the task-less assembly would not)."""
        tagged_rule = _row(
            lanes_mgr,
            "Tagged rule: run the migration linter before merge",
            task="t1",
            subtype="mnemos:rule",
        )
        untagged_rule = _row(
            lanes_mgr,
            "Untagged rule: keep CHANGELOG entries imperative",
            subtype="mnemos:rule",
        )
        out = lanes_mgr.assemble_context(
            session=SESSION, project=PROJECT, task="t1", query="migration linter"
        )
        ids = {b["memory_id"] for b in out["blocks"]}
        assert tagged_rule.id in ids
        assert untagged_rule.id not in ids
        # Control: task-less assembly surfaces BOTH rules (the narrowing
        # is the task gate, not a broken lane).
        control = lanes_mgr.assemble_context(
            session=SESSION, project=PROJECT, query="migration linter"
        )
        control_ids = {b["memory_id"] for b in control["blocks"]}
        assert {tagged_rule.id, untagged_rule.id} <= control_ids
        # Lane telemetry carries the task filter count (additive key).
        assert out["stats"]["recall"]["lanes"]["task_filtered"] >= 1


# ---------------------------------------------------------------------------
# 2e. lens x lanes composition (Ф1-PREP item 3, #360 review)
# ---------------------------------------------------------------------------


class TestLensLanesComposition:
    """Pin the TRUE lens x lanes ordering contract (Ф1-PREP item 3).

    In ``_recall_stage`` the lens projection runs over the COMBINED
    candidate list (lanes leg + knowledge leg) BEFORE the applyTo
    partition. The #360 review flagged governance rows as "possibly
    stripped before the partition" — investigated: they ARE, and that is
    the intended contract, symmetric with the pre-existing contentType
    mode filter (``mode=code`` strips prose governance rows inside the
    lanes loop the same way). An ACTIVE lens strips inadmissible
    governance rows before the partition; an INACTIVE lens (prose query)
    is the identity projection and governance rows survive to the
    partition and surface with their lane keys.
    """

    def _seed(self, manager: MemoryManager) -> dict[str, Memory]:
        return {
            "rule": _row(
                manager,
                "Rule: run the migration linter before merge",
                subtype="mnemos:rule",
            ),
            "code": _row(manager, CODE_CONTENT, title="loader"),
            "prose": _row(manager, PROSE_CONTENT, title="deploy notes"),
        }

    def test_active_lens_strips_governance_before_partition(self, lanes_mgr: MemoryManager) -> None:
        """The stripping contract: a code-shaped query under lanes narrows
        the WHOLE candidate list — the prose rule (lane) and the prose
        knowledge row drop before the applyTo partition ever runs; only
        the code row survives."""
        rows = self._seed(lanes_mgr)
        out = lanes_mgr.assemble_context(
            session=SESSION, project=PROJECT, query=CODE_QUERY, lens="code"
        )
        ids = {b["memory_id"] for b in out["blocks"]}
        assert ids == {rows["code"].id}
        assert all(b["lane"] == "knowledge" for b in out["blocks"])
        # Lens telemetry: the rule (lane) and prose rows were filtered.
        assert out["stats"]["recall"]["lens"] == {
            "name": "code",
            "active": True,
            "filtered": 2,
        }
        # Lane telemetry asymmetry is part of the contract: rules/decisions
        # carry the RAW lane query counts, knowledge counts post-lens
        # survivors (the raw lane counts were never post-filtered — same
        # as the pre-lens contentType filter before this wave).
        lanes_stats = out["stats"]["recall"]["lanes"]
        assert lanes_stats["rules"] == 1
        assert lanes_stats["knowledge"] == 1

    def test_inactive_lens_keeps_governance_rows(self, lanes_mgr: MemoryManager) -> None:
        """Identity projection: a prose query under the CODE lens narrows
        nothing — the governance row survives to the partition and
        surfaces as a rules-lane block."""
        rows = self._seed(lanes_mgr)
        out = lanes_mgr.assemble_context(
            session=SESSION, project=PROJECT, query="migration linter", lens="code"
        )
        ids = {b["memory_id"] for b in out["blocks"]}
        assert rows["rule"].id in ids
        rule_block = next(b for b in out["blocks"] if b["memory_id"] == rows["rule"].id)
        assert rule_block["lane"] == "rules"
        assert out["stats"]["recall"]["lens"] == {
            "name": "code",
            "active": False,
            "filtered": 0,
        }

    def test_no_lens_governance_rows_surface_control(self, lanes_mgr: MemoryManager) -> None:
        """Control: same code-shaped query, NO lens — the lanes behavior
        stands untouched (the stripping in the first test is the lens
        gate, not a broken lane)."""
        rows = self._seed(lanes_mgr)
        out = lanes_mgr.assemble_context(session=SESSION, project=PROJECT, query=CODE_QUERY)
        ids = {b["memory_id"] for b in out["blocks"]}
        assert rows["rule"].id in ids
        assert "lens" not in out["stats"]["recall"]


# ---------------------------------------------------------------------------
# 2d. task — harness carriers (hooks / dispatch / REST)
# ---------------------------------------------------------------------------


class TestTaskHarnessCarriers:
    def test_pre_llm_call_threads_task(self, mgr: MemoryManager) -> None:
        """ADR-0027: 'the harness passes the task identifier' — the
        pre_llm_call hook is that carrier."""
        rows = {
            "in": _row(mgr, "carrier probe content about zebras", task="t1"),
            "out": _row(mgr, "carrier probe content about zebras", task="t2"),
        }
        mgr.vectors.wipe()
        out = pre_llm_call(
            mgr,
            session=SESSION,
            project=PROJECT,
            agent=AGENT,
            context_hint="carrier probe zebras",
            task="t1",
        )
        ids = {b["memory_id"] for b in out["blocks"]}
        assert ids == {rows["in"].id}
        assert out["task"] == "t1"

    def test_pre_llm_call_without_task_unchanged(self, mgr: MemoryManager) -> None:
        _row(mgr, "carrier probe content about zebras", task="t1")
        out = pre_llm_call(
            mgr, session=SESSION, project=PROJECT, agent=AGENT, context_hint="zebras"
        )
        assert "task" not in out

    def test_dispatch_hook_routes_task(self, mgr: MemoryManager) -> None:
        out = dispatch_hook(
            mgr,
            action="pre_llm_call",
            session=SESSION,
            project=PROJECT,
            agent=AGENT,
            context_hint="zebras",
            task="t1",
        )
        assert out["task"] == "t1"

    def test_rest_hooks_pre_llm_call_accepts_task(self, tmp_path: Path) -> None:
        """The HTTP harness twin: POST /hooks/pre_llm_call with ``task``
        narrows the assembly; an invalid slug maps to 422."""
        manager = MemoryManager(_settings(tmp_path))
        manager._embedder = _HashEmbedder()
        try:
            in_row = _row(manager, "rest carrier content about zebras", task="t1")
            out_row = _row(manager, "rest carrier content about zebras", task="t2")
            manager.vectors.wipe()
            api_main._manager = manager
            test_app = FastAPI(title="s2-test", version="0.1.0", lifespan=lifespan)
            for route in real_app.routes:
                test_app.routes.append(route)
            with TestClient(test_app) as client:
                body: dict[str, Any] = {
                    "session": SESSION,
                    "project": PROJECT,
                    "agent": AGENT,
                    "context_hint": "rest carrier zebras",
                    "task": "t1",
                }
                resp = client.post("/hooks/pre_llm_call", json=body)
                assert resp.status_code == 200
                payload = resp.json()
                assert payload["task"] == "t1"
                ids = {b["memory_id"] for b in payload["blocks"]}
                assert in_row.id in ids
                assert out_row.id not in ids
                bad = dict(body, task="Bad!")
                bad_resp = client.post("/hooks/pre_llm_call", json=bad)
                assert bad_resp.status_code == 422
        finally:
            api_main._manager = None
            manager.close()


# ---------------------------------------------------------------------------
# 3. Write-side doc-grouping validation (slice-1 review item 1)
# ---------------------------------------------------------------------------


PARTIAL_TRIPLES: list[dict[str, Any]] = [
    {"doc_id": "d"},
    {"doc_id": "d", "chunk_idx": 0},
    {"chunk_idx": 0, "heading_path": []},
    {"doc_id": "d", "heading_path": []},
    {"heading_path": ["A"]},
]
MALFORMED_FULL: list[dict[str, Any]] = [
    {"doc_id": "", "chunk_idx": 0, "heading_path": []},
    {"doc_id": "d", "chunk_idx": -1, "heading_path": []},
    {"doc_id": "d", "chunk_idx": "3", "heading_path": []},
    {"doc_id": "d", "chunk_idx": 0, "heading_path": "Context"},
]


class TestDocMetadataCreateValidation:
    @pytest.mark.parametrize("partial", PARTIAL_TRIPLES)
    def test_memory_create_rejects_partial_triple(self, partial: dict[str, Any]) -> None:
        """DTO boundary: construction fails — the convention is
        all-or-nothing, a persisted half-triple would split a document
        silently once the Phase-3 reader groups by doc_id."""
        with pytest.raises(ValidationError, match="doc grouping"):
            MemoryCreate(content="x", metadata=partial)

    @pytest.mark.parametrize("malformed", MALFORMED_FULL)
    def test_memory_create_rejects_malformed_triple(self, malformed: dict[str, Any]) -> None:
        """Full but ill-shaped triples fail with the builder's own shape
        error (raw values are never silently coerced)."""
        with pytest.raises(ValidationError):
            MemoryCreate(content="x", metadata=malformed)

    def test_memory_create_accepts_full_triple_and_plain_dicts(self) -> None:
        triple = build_doc_grouping_metadata("doc-1", 0, ["A", "B"])
        MemoryCreate(content="x", metadata=dict(triple))
        MemoryCreate(content="x", metadata={"unrelated": "value"})
        MemoryCreate(content="x")  # default empty dict


class TestDocMetadataManagerGate:
    def test_manager_add_rejects_bypassed_partial_triple(self, mgr: MemoryManager) -> None:
        """M4 pin — belt-and-suspenders: ``model_construct`` bypasses the
        pydantic validators, the MANAGER gate is the store-boundary
        defence every write path (MCP/REST/SDK/CLI) funnels through."""
        bypassed = MemoryCreate.model_construct(
            content="bypassed",
            tags=["project:p", "agent:a", "mnemos:rule"],
            metadata={"doc_id": "d"},
        )
        with pytest.raises(ValueError, match="all-or-nothing"):
            mgr.add(bypassed, project="p", agent="a")

    def test_manager_add_accepts_full_triple(self, mgr: MemoryManager) -> None:
        triple = build_doc_grouping_metadata("doc-2", 4, ["Install"])
        memory = mgr.add(
            MemoryCreate(
                content="full triple",
                tags=["project:p", "agent:a", "mnemos:rule"],
                metadata=dict(triple),
            ),
            project="p",
            agent="a",
        )
        stored = mgr.sqlite.get(memory.id)
        assert stored is not None
        assert stored.metadata["doc_id"] == "doc-2"

    def test_manager_update_rejects_bypassed_partial_triple(self, mgr: MemoryManager) -> None:
        memory = _row(mgr, "update gate probe")
        bypassed = MemoryUpdate.model_construct(metadata={"doc_id": "d", "chunk_idx": 7})
        with pytest.raises(ValueError, match="all-or-nothing"):
            mgr.update(memory.id, bypassed)
        # Fail-loud BEFORE any write: the row's metadata is unchanged.
        stored = mgr.sqlite.get(memory.id)
        assert stored is not None
        assert "doc_id" not in stored.metadata

    def test_manager_update_accepts_full_triple_replacement(self, mgr: MemoryManager) -> None:
        memory = _row(mgr, "update gate probe")
        triple = build_doc_grouping_metadata("doc-3", 1, ["Run"])
        updated = mgr.update(memory.id, MemoryUpdate(metadata=dict(triple)))
        assert updated is not None
        assert updated.metadata["doc_id"] == "doc-3"

    def test_update_without_metadata_leaves_legacy_rows_alone(self, mgr: MemoryManager) -> None:
        """Write-side validation only: an update that does not touch
        metadata never re-validates historical rows (zero migration, no
        backfill semantics change)."""
        memory = _row(mgr, "legacy row")
        updated = mgr.update(memory.id, MemoryUpdate(title="retitled"))
        assert updated is not None
        assert updated.title == "retitled"

    def test_rest_create_maps_partial_triple_to_422(self, tmp_path: Path) -> None:
        """REST /memories parses MemoryCreate — the DTO validator makes a
        partial triple a 422 (client error), not a 500 or a silent write."""
        manager = MemoryManager(_settings(tmp_path))
        manager._embedder = _HashEmbedder()
        try:
            api_main._manager = manager
            test_app = FastAPI(title="s2-test", version="0.1.0", lifespan=lifespan)
            for route in real_app.routes:
                test_app.routes.append(route)
            with TestClient(test_app) as client:
                body = {
                    "content": "chunk text",
                    "tags": ["project:s2-proj", "agent:s2-agent", "mnemos:learning"],
                    "metadata": {"doc_id": "d"},
                }
                resp = client.post("/memories", json=body)
                assert resp.status_code == 422
                assert "doc grouping" in resp.text
        finally:
            api_main._manager = None
            manager.close()
