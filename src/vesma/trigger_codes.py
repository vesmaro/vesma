"""Trigger codes for federation mediated pull (Phase 1 prerequisite).

ArchCom 2026-07-17 federation contract §9. Instead of a per-session
query budget, the B side (the federation server) returns an
exhaustive response plus a **trigger code** that tells the A side (the
federation client) what to do next. The codes are an enum, not prose
explanations — A sees the code and knows the action.

Phase 1 (this module) only defines the enum and two helpers. Phase 2
wires the codes into the federation server (B-side, returned in the
``share-finding`` A2A payload) and the federation client (A-side,
dispatched on receive). No transport, no server, no client lives here.

Reference:
    - Contract §9: ``.archcom/sessions/2026-07-17-federation-contract.md``
    - ADR-0016: ``docs/project/adr/0016-federation-threat-model.md``
"""

from __future__ import annotations

from enum import StrEnum


class TriggerCode(StrEnum):
    """Trigger code returned by the B-side federation server.

    Exactly five values per contract §9 — no extras. Each docstring
    explains when B returns the code and what A should do.

    Values:
        EXHAUSTIVE: B gave an exhaustive answer carrying the full gist
            in correctly sanitized form. A should use the answer and
            NOT repeat the request for the same topic — there will be
            no additions.
        ALREADY_EXHAUSTED: A repeated a request on a topic B already
            answered exhaustively (B checked
            :mod:`vesma.federation_access_log`). The previous answer
            was complete; there is nothing to add. A should NOT repeat
            the request and should reuse the previous answer.
        PARTIAL: The answer is partial — not all relevant records were
            found, or moderation redacted a portion. More relevant
            content exists but B cannot or will not ship it all. A may
            refine the request (a different topic / angle) but should
            NOT repeat the exact same request.
        REFUSED: B refused the request — moderation did not pass, the
            content cannot be shared even after redaction. A should NOT
            repeat the request and should fall back to local
            :func:`mnemos_search` per КП-2.
        OFFLINE_LITE: B is online but in a reduced mode (for example,
            the moderation pipeline is partially offline). A receives a
            partial result and may supplement it with local
            :func:`mnemos_search`.
    """

    EXHAUSTIVE = "EXHAUSTIVE"
    ALREADY_EXHAUSTED = "ALREADY_EXHAUSTED"
    PARTIAL = "PARTIAL"
    REFUSED = "REFUSED"
    OFFLINE_LITE = "OFFLINE_LITE"


def is_terminal(code: TriggerCode) -> bool:
    """Return ``True`` if A should not repeat the request.

    Terminal codes (contract §9): ``EXHAUSTIVE``, ``ALREADY_EXHAUSTED``,
    ``REFUSED`` — the answer is final; A does not re-query the same
    topic. Non-terminal: ``PARTIAL``, ``OFFLINE_LITE`` — A may refine
    or continue.
    """
    return code in (TriggerCode.EXHAUSTIVE, TriggerCode.ALREADY_EXHAUSTED, TriggerCode.REFUSED)


def should_fallback_to_local(code: TriggerCode) -> bool:
    """Return ``True`` if A should fall back to local ``mnemos_search``.

    Per КП-2 (contract §3.2): ``REFUSED`` (content cannot be shared) and
    ``OFFLINE_LITE`` (B in reduced mode) both signal that A should
    supplement the answer with a local search. The other codes do not.
    """
    return code in (TriggerCode.REFUSED, TriggerCode.OFFLINE_LITE)
