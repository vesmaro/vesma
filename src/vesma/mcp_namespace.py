"""MCP-plane namespace contracts (ADR-0050 §4/§5, wave W3).

Harness isolation on the ``/mcp`` streamable-HTTP plane by the axes
**identity / data / resources** (never processes — the Ф3 supervisor-per-
harness shape was rejected by the ArchCom):

* **Identity threading** — the raw-ASGI auth gate
  (:mod:`vesma.api.mcp_endpoint`) validates the Bearer agent token on
  EVERY request; after a ``valid`` verdict it publishes the verdict's
  ``(agent_id, jti)`` as an :class:`McpIdentity` into a
  :class:`contextvars.ContextVar` for the duration of the SDK session
  manager's ``handle_request`` (set before the await, reset in a
  ``finally``). The MCP tool dispatch (:mod:`vesma.mcp_server`) reads the
  identity from the context. Because the gate and the dispatch run in the
  same process, the contextvar is the whole transport — anyio propagates
  the copied context into the worker tasks the SDK spawns, and stdio /
  local calls never set the var, so ``current_identity()`` stays ``None``
  and every helper below degrades to the byte-identical pre-W3 behavior
  (the local CLI / embedded store contract).
* **Enforced ``agent:<slug>`` tags (reject-loud)** — on the /mcp plane a
  client-authored ``agent:<slug>`` tag (or agent-identity argument) that
  does not match the request's identity agent is a
  :class:`NamespaceViolationError`: a loud, typed refusal. This is the
  extension of the server-minted-metadata strip class
  (``manager.INTERNAL_METADATA_KEYS`` / the #251/#432 mint-protection
  discipline, CWE-346) from metadata keys to the ownership tag: the
  identity is minted by the token validation, a client must never mint a
  foreign one. Non-``agent:`` tags (``project:*``, ``vesma:*``,
  ``task:*``, …) are NOT touched — only the ownership namespace is
  enforced.
* **Cross-read (`cross_read`)** — :confval:`mcp.namespace.cross_read`
  (``published_only`` default | ``own_only``) filters FOREIGN (not-this-
  identity) records on the read surfaces: ``published_only`` serves only
  ``published`` foreign rows; ``own_only`` serves none. The identity's own
  rows are always served in full. The filter is a dispatch-level
  post-filter over the manager result (owner = the record's
  ``agent:<slug>`` tag, status = the record's status) — the SQLite read
  paths are unchanged, so the local surfaces are byte-identical. Without
  identity the filter is a no-op. The config value is a ``Literal`` — an
  unknown value fails closed at Settings construction.
* **Per-token quota** — :confval:`mcp.quota.calls_per_minute` caps /mcp
  tool calls per agent token (key = the ``jti``; a rotated token gets a
  fresh window) on a sliding 60-second window: a plain in-process
  ``dict`` + lock (``TokenQuota``). The gate checks BEFORE handing the
  request to the session manager; over-limit answers 429 JSON with
  ``Retry-After``. ``None`` disables the cap. Process-local by design
  (Ф1: one core process; Ф2 fronts are stateless proxies in front of the
  same core).

Token hygiene (the W1 mesh data-gate convention) holds here too: the
identity carries ``jti``/``agent_id``, never the token value, and quota
logs stay at the same coarse granularity.
"""

from __future__ import annotations

import math
import threading
import time
from collections import deque
from collections.abc import Iterable
from contextvars import ContextVar, Token
from dataclasses import dataclass
from typing import Any, Final, Literal

#: The two ratified cross-read modes (ADR-0050 §5): the default mirrors
#: the awareness cross-silo cap, ``own_only`` is the strict deployment
#: posture. Anything else fails closed at config construction (the
#: ``Literal`` in :class:`vesma.config.McpNamespaceConfig`).
CrossReadMode = Literal["published_only", "own_only"]

CROSS_READ_MODES: Final[tuple[str, str]] = ("published_only", "own_only")

#: The ownership-tag prefix — the ONE namespace this wave enforces.
AGENT_TAG_PREFIX: Final[str] = "agent:"

#: Sliding-window length of the per-token quota (the ratified default
#: shape: N calls per minute, not a fixed-rate token bucket).
QUOTA_WINDOW_SECONDS: Final[float] = 60.0

#: Default per-token cap (ADR-0050 W3 brief: 120 calls/min; a harness
#: session peaks well under it, an abuse loop does not).
DEFAULT_CALLS_PER_MINUTE: Final[int] = 120


@dataclass(frozen=True, slots=True)
class McpIdentity:
    """The authenticated harness identity of ONE ``/mcp`` request.

    Minted by the gate from a ``valid`` :class:`AgentTokenVerdict` —
    ``agent_id`` is the token's ``sub`` (the enforced ``agent:<slug>``
    namespace owner), ``jti`` the token instance id (the quota key). The
    token VALUE never rides here — the identity is metadata, the same
    coarse granularity the gate logs at.
    """

    agent_id: str
    jti: str


_identity_var: ContextVar[McpIdentity | None] = ContextVar(
    "vesma_mcp_request_identity", default=None
)


def set_request_identity(identity: McpIdentity) -> Token[McpIdentity | None]:
    """Publish the request identity into the current context.

    The gate calls this right before ``manager.handle_request`` and MUST
    reset with the returned token in a ``finally`` — the var is
    per-request state, never a session or process default.
    """
    return _identity_var.set(identity)


def reset_request_identity(token: Token[McpIdentity | None]) -> None:
    """Reset the identity var to its pre-request value (gate ``finally``)."""
    _identity_var.reset(token)


def current_identity() -> McpIdentity | None:
    """The identity of the /mcp request being served, ``None`` off-plane.

    ``None`` everywhere except inside a gate-wrapped ``handle_request``:
    stdio MCP, the CLI, the embedded store and the REST API never set it,
    so every consumer keyed on it keeps its exact pre-W3 behavior.
    """
    return _identity_var.get()


def current_agent() -> str | None:
    """The identity agent slug, ``None`` off-plane (convenience reader)."""
    identity = _identity_var.get()
    return identity.agent_id if identity is not None else None


class NamespaceViolationError(Exception):
    """A client tried to mint a foreign namespace identity on /mcp.

    The reject-loud twin of the metadata strip class: instead of silently
    stripping (metadata stamps) the ownership claim is REFUSED with a
    typed, loud error — an enforced namespace that silently rewrote
    client tags would mask harness configuration bugs (the record would
    land in a namespace the harness never asked for and could not find
    again). The MCP dispatch renders it as a distinct ``❌ Namespace
    violation`` result; it is deliberately NOT a ``ValueError`` subclass
    so a future caller cannot accidentally funnel it into a generic
    validation path.
    """


def record_owner(tags: list[str] | tuple[str, ...] | None) -> str:
    """The ``agent:<slug>`` slug of a record's tags, ``""`` when unowned.

    The ONE owner convention for the cross-read filter: generic writes
    carry their agent tag from the tag contract, checkpoints mint it at
    the save boundary — every memory row answers through its tags, no
    extra schema column.
    """
    if not tags:
        return ""
    for tag in tags:
        if tag.startswith(AGENT_TAG_PREFIX):
            return tag[len(AGENT_TAG_PREFIX) :]
    return ""


def enforce_agent_tags(tags: list[str], identity: McpIdentity | None) -> list[str]:
    """Reject a foreign client-authored ``agent:<slug>`` tag on /mcp.

    Runs AFTER :func:`vesma.models.validate_tag_contract` (which
    guarantees at most one ``agent:`` tag and may have lax-patched a
    missing one to ``agent:unknown`` — an equally foreign mint under
    identity). ``identity=None`` (local/stdio call) returns the tags
    unchanged — the byte-identical pre-W3 contract. Only ``agent:*`` tags
    are judged; ``project:*`` / ``vesma:*`` / ``task:*`` / free tags are
    never touched.
    """
    if identity is None:
        return tags
    expected = f"{AGENT_TAG_PREFIX}{identity.agent_id}"
    for tag in tags:
        if tag.startswith(AGENT_TAG_PREFIX) and tag != expected:
            raise NamespaceViolationError(
                f"tag {tag!r} mints a foreign agent namespace; this request is "
                f"authenticated as agent {identity.agent_id!r} — write under "
                f"{expected!r} (enforced agent:<slug>, ADR-0050 §4)"
            )
    return tags


def assert_rows_owned(
    rows: Iterable[Any],
    identity: McpIdentity | None,
    *,
    action: str,
) -> None:
    """Refuse a bulk mutation whose affected population includes foreign rows.

    The row-ownership twin of :func:`enforce_agent_tags` (ADR-0050 §4, the
    same "identity is server-minted, a client never mints a foreign one"
    principle): a mutating surface (bulk tag operations, workflow
    transitions) may touch only rows the request identity owns. An
    UNOWNED row (``""`` owner) is foreign — the empty owner is not a
    wildcard, exactly like :func:`cross_read_visible`. Own rows keep full
    freedom (you may repair your own namespace tags). ``identity=None``
    (local call) is a no-op — the pre-W3 behavior.
    """
    if identity is None:
        return
    for mem in rows:
        owner = record_owner(getattr(mem, "tags", None))
        if owner != identity.agent_id:
            raise NamespaceViolationError(
                f"{action}: memory {getattr(mem, 'id', '?')[:8]}… is owned by "
                f"{owner!r}; this request is authenticated as agent "
                f"{identity.agent_id!r} — mutating another harness's rows is "
                f"refused (enforced row ownership, ADR-0050 §4)"
            )


def enforce_bulk_rename_prefixes(
    from_prefix: str,
    to_prefix: str,
    identity: McpIdentity | None,
) -> None:
    """Refuse a bulk rename touching the ``agent:`` namespace on /mcp.

    A bulk prefix rename spans every row in scope — foreign ones included
    — so rewriting FROM ``agent:`` (mass-rewrite of the ownership
    namespace) and renaming TO ``agent:`` (mass-MINT of the ownership
    namespace) are refused ENTIRELY on the plane, whatever the rows own.
    A prefix-scoped repair of a single own row has no bulk shape; off-
    plane (``identity=None``) the rename behaves exactly as before.
    """
    if identity is None:
        return
    if from_prefix == AGENT_TAG_PREFIX or to_prefix == AGENT_TAG_PREFIX:
        raise NamespaceViolationError(
            f"bulk rename {from_prefix}→{to_prefix} spans the agent: ownership "
            f"namespace; this request is authenticated as agent "
            f"{identity.agent_id!r} — bulk namespace rewrites are refused on "
            f"the /mcp plane (ADR-0050 §4)"
        )


def enforce_agent_arg(
    agent: str | None, identity: McpIdentity | None, *, surface: str
) -> str | None:
    """Reject an explicit foreign agent-identity argument on /mcp.

    The argument-shaped twin of :func:`enforce_agent_tags` for surfaces
    that take the record owner as an argument (the checkpoint channel,
    the rewrite channel, the CCR issuer ledger). ``identity=None`` (local
    call) returns the argument unchanged; ``None`` argument (caller
    omitted it) passes through — the surface's own default applies.
    """
    if identity is None or agent is None:
        return agent
    if agent != identity.agent_id:
        raise NamespaceViolationError(
            f"{surface}: agent={agent!r} mints a foreign agent namespace; this "
            f"request is authenticated as agent {identity.agent_id!r} "
            f"(enforced agent:<slug>, ADR-0050 §4)"
        )
    return agent


def resolve_identity_agent(agent: str | None, identity: McpIdentity | None) -> str | None:
    """Default an absent owner argument to the request identity.

    Used by the checkpoint channel on the /mcp plane: a harness that
    omits ``agent`` gets its checkpoints owned by its OWN identity (not
    the legacy ``"user"`` default — under isolation that would orphan
    every harness checkpoint into a namespace the harness cannot read
    back under ``own_only``). An explicit argument must first clear
    :func:`enforce_agent_arg`; ``identity=None`` keeps the argument as-is.
    """
    if identity is None:
        return agent
    return agent if agent is not None else identity.agent_id


def cross_read_visible(
    owner: str,
    status: str,
    identity: McpIdentity | None,
    mode: str,
) -> bool:
    """The cross-read predicate for one record (dispatch-level post-filter).

    Own records (owner == the identity agent) are always visible in full.
    Foreign records follow the mode: ``published_only`` serves the
    published projection only, ``own_only`` serves none. Records with no
    owner tag (``""``) are foreign by definition — the empty owner is not
    a wildcard. ``identity=None`` (local call) short-circuits to visible,
    and an unknown ``mode`` FAILS CLOSED (invisible) — a config regression
    must never widen the aperture.
    """
    if identity is None:
        return True
    if owner == identity.agent_id:
        return True
    if mode == "own_only":
        return False
    if mode == "published_only":
        return status == "published"
    return False  # unknown mode: fail closed


@dataclass(frozen=True, slots=True)
class QuotaDecision:
    """One quota check verdict (``retry_after`` seconds when denied)."""

    allowed: bool
    retry_after: int | None = None


class TokenQuota:
    """Sliding-window per-token call cap for the /mcp plane (ADR-0050 §4).

    The resource axis of harness isolation: one window per token
    instance (key = the ``jti``), a plain in-process ``dict`` of
    ``deque``s under one lock — no external store, no cross-process
    claim (Ф1 runs one core process; a multi-core fleet would need the
    W4 resilience plane). ``check`` prunes timestamps older than the
    window, compares the live count against the cap, and records the
    call only when admitted — denied calls do NOT extend the window
    (a caller hammering a 429 must not push its own recovery further
    away). ``calls_per_minute=None`` disables the cap entirely
    (``check`` becomes a constant allow, no bookkeeping).

    The clock is injectable (``now``) so tests pin the window arithmetic
    without sleeping.
    """

    def __init__(self, calls_per_minute: int | None = DEFAULT_CALLS_PER_MINUTE) -> None:
        if calls_per_minute is not None and calls_per_minute <= 0:
            raise ValueError("calls_per_minute must be a positive integer or None")
        self._limit = calls_per_minute
        self._lock = threading.Lock()
        self._windows: dict[str, deque[float]] = {}

    @property
    def calls_per_minute(self) -> int | None:
        """The configured cap (``None`` = disabled)."""
        return self._limit

    def check(self, key: str, *, now: float | None = None) -> QuotaDecision:
        """Admit or deny one call for ``key`` (the token ``jti``).

        Returns the verdict with ``retry_after`` seconds (ceil) when
        denied; the header value is always ≥ 1 so a client never sees a
        0-second retry hint.
        """
        if self._limit is None:
            return QuotaDecision(allowed=True)
        moment = time.monotonic() if now is None else now
        with self._lock:
            window = self._windows.get(key)
            if window is None:
                window = deque()
                self._windows[key] = window
            cutoff = moment - QUOTA_WINDOW_SECONDS
            while window and window[0] <= cutoff:
                window.popleft()
            if len(window) >= self._limit:
                oldest = window[0]
                retry_after = max(1, math.ceil(QUOTA_WINDOW_SECONDS - (moment - oldest)))
                return QuotaDecision(allowed=False, retry_after=retry_after)
            window.append(moment)
            return QuotaDecision(allowed=True)


__all__ = [
    "AGENT_TAG_PREFIX",
    "CROSS_READ_MODES",
    "DEFAULT_CALLS_PER_MINUTE",
    "QUOTA_WINDOW_SECONDS",
    "CrossReadMode",
    "McpIdentity",
    "NamespaceViolationError",
    "QuotaDecision",
    "TokenQuota",
    "assert_rows_owned",
    "cross_read_visible",
    "current_agent",
    "current_identity",
    "enforce_agent_arg",
    "enforce_agent_tags",
    "enforce_bulk_rename_prefixes",
    "record_owner",
    "reset_request_identity",
    "resolve_identity_agent",
    "set_request_identity",
]
