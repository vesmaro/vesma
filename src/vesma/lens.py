"""Code-defined lens presets — ADR-0027 Phase 0 (epic #308, invariant 6).

A lens is a deterministic **query-conditioned** function corpus →
projection that may only NARROW admissibility, never widen it. The shape
mirrors the context-filter profiles (``vesma.filter.pipeline``): an
enum in code, not a table, not a tag, not user configuration — the
definitions live here under review, because a named lens is an injection
vector (a tag lies about one record, a lens about the whole corpus;
ADR-0027 "Alternatives considered" rejects lenses-as-data for exactly
this reason).

Binding rules carried by this module (ADR-0027 invariants 5/6/9 + the
E3 class ban):

* **Query-conditioned, never query-blind.** The E3 falsification
  (``e3-lanes-56c568297ad6``: -61.5 pp) banned statically composed
  projections as a class. A lens therefore DECIDES from the effective
  recall query whether it activates at all; a query the lens does not
  match gets the identity projection (narrows by zero), not a forced
  narrowing.
* **Only narrows.** The projection is an order-preserving subset of the
  corpus — no reordering, no promotion, no pinning. A lens output is
  never pinned into the top-of-context (#248 is open; invariant 9).
* **Default = absent.** No lens selected → no projection runs, no stats
  key, byte-identical pipeline output (pinned by tests).
* **Uncalibrated zone.** Nobody ships strict lenses (ADR-0027
  competitive scan), so the activation heuristics are self-built and
  carry no value claim until an S5 PASS (invariant 11) — v1 is one
  built-in lens, deliberately minimal.
"""

from __future__ import annotations

import re
from enum import StrEnum
from typing import Final

__all__ = ["Lens", "lens_active", "lens_admits", "resolve_lens"]


class Lens(StrEnum):
    """The built-in lens presets (v1: exactly one).

    ``CODE`` — a code-focused lens: when the effective recall query is
    itself code-shaped, the projection keeps only ``content_type="code"``
    candidates (the ingest-captured binary partition the ``mode=code``
    contentType filter already uses — the lens differs from ``mode`` in
    that the QUERY decides, not the caller's assertion).
    """

    CODE = "code"


#: The content_type each lens narrows to when active. One entry per
#: lens member — a lens without a narrowing target would be dead code.
_LENS_CONTENT_TYPE: Final[dict[Lens, str]] = {Lens.CODE: "code"}

#: Query-level code signals for the CODE lens activation. Deliberately
#: TIGHT: syntactic shapes that do not occur in prose queries. A prose
#: question about code ("how does the retry loop work?", "import the
#: CSV data", "class attendance was low") must NOT activate the lens —
#: its recall corpus legitimately includes prose notes about code. Each
#: pattern is a documented, deterministic code signal:
#:   1. function definitions — keyword + identifier + ``(``;
#:   2. class definitions — keyword + identifier + ``:``/``{``/``(``;
#:   3. a call expression with code-typical ARGUMENTS — identifier
#:      DIRECTLY followed by ``(`` (no space: English typography mandates
#:      one before a parenthetical) whose group carries code evidence:
#:      a comma (``connect(host, port)``), ``=`` (``retry(backoff=5)``),
#:      an underscored/dotted token (``foo(max_retries)``, ``bar(x.y)``)
#:      or a comparison/bitwise operator. The #360 review false-fired
#:      the old word-group shape on prose parentheticals ("how does the
#:      retry loop work (with backoff)" activated the lens and stripped
#:      the prose rows), so empty groups and word-only groups are now
#:      REJECTED, and so are characters that occur in prose
#:      parentheticals: hyphens ("(step-by-step)"), slashes ("(and/or)"),
#:      percents ("(50% of runs)"), quotes ("(the server's config)") and
#:      bare digits ("(404 pages)"). An empty-arg call ("parse_args()")
#:      no longer activates either — with nothing inside the group the
#:      shape is indistinguishable from prose, and a MISSED activation is
#:      the safe direction (identity projection keeps every row); the
#:      no-space comma residual ("options(a, b, or c)") stays ADmissible
#:      on purpose — the typographic heuristic (prose parens take a
#:      leading space) is the discriminator, pinned as documented
#:      behavior (#368).
#:   4. a method call — receiver.identifier( — mirroring signal #3: the
#:      identifier is FLUSH against ``(`` (no space: English typography
#:      mandates one before a prose parenthetical, so "compare node.js
#:      (the runtime)" and the typo "e.g (note)" are prose, not calls)
#:      AND the group carries the same code evidence as #3 (comma, ``=``,
#:      underscored/dotted token, comparison/bitwise operator — "node.js
#:      (fs, cb)" vs "node.js (the runtime)"); the #368 review false-fired
#:      the old whitespace-tolerant shape on exactly those prose shapes.
#:      ACCEPTABLE LOSS (registered, #387 review): the group span
#:      ``[^()]*`` cannot see through INNER parentheses, so a nested-call
#:      argument like ``obj.method(f(x))`` never matches — the inner
#:      group terminates the span before the evidence check runs. All
#:      such losses are in the SAFE direction (ADR-0027 invariant 6): a
#:      missed activation yields the identity projection (every row
#:      kept), never a wrongly narrowed one; the outer call stays
#:      coverable through its own evidence-bearing phrasing.
#:   5. code-file paths in PATH-ISH CONTEXT ONLY (src/vesma/manager.py,
#:      `app.ts`, 'config.toml' — #388): an extension-only token carries
#:      a code signal only when the query marks it as a file reference,
#:      not as an English dotted word. The DISCRIMINATOR is the token's
#:      context — an extension match fires when the token contains a
#:      path separator (``/`` or ``\`` — prose never writes inside a
#:      word, so "src/vesma/lens.py" and "benchmarks/experiments/
#:      e3_lanes/runner.py" are paths, while "node.js" and "a.b.c" are
#:      dotted words) OR the token is wrapped in quotes/backticks (the
#:      author fenced it as a literal filename — "check `lens.py`"). The
#:      bare extension token ("node.js", "package.json", "some.sql")
#:      is PROSE-AMBIGUOUS — "compare node.js (the runtime)" reads
#:      ``node.js`` as the runtime's name, not a ``.js`` file — so it no
#:      longer activates on its own (#388); a missed path-shaped-like-
#:      prose query keeps the identity projection, the safe direction.
#:      The dotfile form (".rb files") was already inert (the pattern
#:      requires a word character before the dot) and stays so;
#:      an ``.md`` path never matched the extension lexicon either way.
#:   6. code-only operators (=> arrow, :: scope resolution).
#: An import statement is deliberately NOT a signal on its own — the
#: dotted-path form ("import vesma.manager") is already covered by the
#: file-path/call shapes, while the bare word form false-positives on
#: prose ("import the CSV data").
_QUERY_CODE_SIGNALS: Final[tuple[re.Pattern[str], ...]] = (
    re.compile(r"\b(?:def|function|fn)\s+[A-Za-z_]\w*\s*\("),
    re.compile(r"\bclass\s+[A-Za-z_]\w*\s*[:({]"),
    re.compile(r"\b[A-Za-z_]\w*\((?=[^()]*(?:[,=*<>&|]|\w[_.]\w))[^()]*\)"),
    re.compile(r"\b[A-Za-z_]\w*\.[A-Za-z_]\w*\((?=[^()]*(?:[,=*<>&|]|\w[_.]\w))[^()]*\)"),
    # Signal 5 (#388): the extension lexicon fires ONLY in path-ish
    # context — a path separator inside the token run (src/…, C:\…,
    # ./run.sh) or a quoted/backticked fence around it (`lens.py`).
    # The bare dotted token (node.js, a.b.c, package.json) is
    # prose-ambiguous and no longer activates on its own.
    re.compile(
        r"(?:[\w./\\-]*[/\\][\w./\\-]*"
        r"\.(?:py|pyi|ts|tsx|js|jsx|mjs|rs|go|c|h|cc|cpp|java|kt|rb|sh|sql|toml)\b"
        r"|[`'\"][\w./\\-]+"
        r"\.(?:py|pyi|ts|tsx|js|jsx|mjs|rs|go|c|h|cc|cpp|java|kt|rb|sh|sql|toml)\b[`'\"])"
    ),
    re.compile(r"=>|::"),
)


def resolve_lens(name: str | Lens | None) -> Lens | None:
    """Resolve a caller-supplied lens name to the code-defined enum.

    ``None`` (the default) resolves to ``None`` — no lens, the identity
    projection, byte-identical pipeline. Any other value MUST be a
    member of :class:`Lens` (the enum value string is accepted): lens
    definitions are code under review, never caller input, so an
    unknown name raises ``ValueError`` rather than being ignored — a
    typo'd lens must fail loudly, not silently assemble unlensed.
    """
    if name is None:
        return None
    if isinstance(name, Lens):
        return name
    try:
        return Lens(str(name))
    except ValueError:
        valid = ", ".join(m.value for m in Lens)
        raise ValueError(f"unknown lens {name!r}; valid lenses (code-defined): {valid}") from None


def lens_active(lens: Lens, *, query: str) -> bool:
    """True when the lens activates for this effective recall query.

    Deterministic pure function of the query text (the same effective
    query the recall stage used — explicit ``query`` if given, else the
    derived file-stem/project-slug term). The CODE lens activates only
    when the query ITSELF carries a code signal; a prose query gets the
    identity projection (query-conditioned, the E3 class ban honored).
    """
    if lens is not Lens.CODE:  # pragma: no cover — v1 has one member
        return False
    return any(sig.search(query) for sig in _QUERY_CODE_SIGNALS)


def lens_admits(lens: Lens, *, query: str, content_type: str) -> bool:
    """Per-candidate admissibility under the lens — never widens.

    Returns ``True`` (admissible) when the lens is inactive for this
    query (identity projection) or the candidate matches the lens's
    narrowing target; ``False`` only ever REMOVES a candidate the
    lens-less pipeline would have kept. The caller preserves order —
    this predicate is a pure filter, never a reordering.
    """
    if not lens_active(lens, query=query):
        return True
    return content_type == _LENS_CONTENT_TYPE[lens]
