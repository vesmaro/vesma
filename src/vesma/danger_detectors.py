"""Danger detectors — ADR-0019 Phase A: enumerated positive-signal set.

This module is the single source of truth for the **danger detector**
classes whose positive signal blocks publication (ADR-0019 lane (b):
"a positive danger-detector signal quarantines it terminally"). It is
consumed by the fail-closed publication gate
(:func:`vesma.pipeline.publish.publish_memory`); the secrets patterns
themselves are NOT duplicated here — the secret class **delegates** to
:func:`vesma.secrets_detector.detect_secrets` (the existing single
source of truth for secret patterns, ArchCom 2026-07-17 §2.2.1) and
keeps only the high-confidence subset as a publication-blocking signal.

Enumerated detector classes (the "enumerated set" of ADR-0019):

* ``prompt-injection`` — the patterns originally introduced by the
  import-validation screen (issue #86) as **log-only**; they moved here
  (single source of truth) so the publication gate can classify them
  into lane (b) while the import screen keeps its warn-only behaviour
  by importing the same table. Case-insensitive substring matching —
  deliberately not regex, to avoid ReDoS on untrusted content.
* ``secret`` — high-confidence secret patterns only: the discrete
  structural patterns of :mod:`vesma.secrets_detector` (aws-key,
  github-token, …). The ``high-entropy`` heuristic is deliberately
  excluded: it is a false-positive-tuned heuristic, not a
  high-confidence signal, and already drives the no-federate tag and
  the issuance redaction path.

Design constraints (mirroring :mod:`vesma.secrets_detector`)
-------------------------------------------------------------

* **Pure function contract** — :func:`detect` never mutates its inputs,
  never logs, never keeps state. Logging/audit is the caller's business.
* **Fail-closed as a value, not an exception** — a detector/scanner
  error is returned inside the :class:`DetectionResult` (``error``
  set); it never propagates out of :func:`detect`. Callers treat a
  non-``None`` ``error`` as a refusal signal (fail-closed).
* **No raw values** — findings carry pattern names and counts ONLY.
  The matched text is never stored on the finding, so a finding cannot
  leak a secret or an injection payload into logs or audit records.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

from vesma.secrets_detector import detect_secrets

__all__ = [
    "DETECTOR_CLASSES",
    "DETECTOR_CLASS_PROMPT_INJECTION",
    "DETECTOR_CLASS_SECRET",
    "HIGH_CONFIDENCE_SECRET_PATTERNS",
    "PROMPT_INJECTION_PATTERNS",
    "DangerFinding",
    "DetectionResult",
    "detect",
]

#: Detector class: prompt-injection payloads (chatml / llama control
#: tokens, instruction-override phrases, role-prefix spoofs).
DETECTOR_CLASS_PROMPT_INJECTION: Final[str] = "prompt-injection"

#: Detector class: high-confidence secret patterns (delegated to
#: :func:`vesma.secrets_detector.detect_secrets`).
DETECTOR_CLASS_SECRET: Final[str] = "secret"

#: The enumerated detector set (ADR-0019 lane (b)). A positive signal
#: from any class in this set is a publication-blocking danger signal.
DETECTOR_CLASSES: Final[frozenset[str]] = frozenset(
    {DETECTOR_CLASS_PROMPT_INJECTION, DETECTOR_CLASS_SECRET}
)

#: Prompt-injection patterns. Originally the log-only screen of the
#: import validation (issue #86, ``cli/import_.py``); moved here as the
#: single source of truth so the publication gate (ADR-0019 Phase A)
#: and the import screen share one table. Each entry:
#: ``(pattern_name, substring)``. Matching is case-insensitive substring
#: (not regex) to keep it fast and avoid ReDoS on untrusted content.
PROMPT_INJECTION_PATTERNS: Final[tuple[tuple[str, str], ...]] = (
    ("chatml-im-start", "<|im_start|>"),
    ("chatml-im-end", "<|im_end|>"),
    ("llama-inst", "[inst]"),
    ("llama-inst-close", "[/inst]"),
    ("ignore-previous", "ignore previous instructions"),
    ("system-prefix", "system:"),
    ("eos-token", "</s>"),
)

#: The high-confidence subset of the secrets-detector pattern catalogue
#: (ADR-0019 ingest gate: "high-confidence secrets mean a hard
#: publication refusal"). These are the discrete structural patterns
#: declared in ``vesma.secrets_detector._PATTERNS``; the
#: ``high-entropy`` heuristic is intentionally absent (it already drives
#: the no-federate tag and issuance redaction — weaker signals must not
#: hard-block publication). A unit test guards this set against pattern
#: renames in :mod:`vesma.secrets_detector`.
HIGH_CONFIDENCE_SECRET_PATTERNS: Final[frozenset[str]] = frozenset(
    {
        "aws-key",
        "github-token",
        "slack-token",
        "openai-key",
        "jwt",
        "pem-private-key",
        "connection-string",
    }
)


# ── Findings ──────────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class DangerFinding:
    """One positive detector signal, aggregated per pattern.

    Carries the detector class, the pattern name and the match count —
    **never the matched text**. This makes every finding safe to log,
    serialise into audit records, or return to callers by construction
    (same posture as ``SecretFinding.redacted_display``).
    """

    detector_class: str
    pattern_name: str
    count: int


@dataclass(frozen=True, slots=True)
class DetectionResult:
    """Outcome of :func:`detect` — the fail-closed gate input.

    * ``findings`` — positive signals from the enumerated classes
      (empty when nothing fired).
    * ``error`` — a detector/scanner error message when the scan could
      not be completed (fail-closed signal: the caller must refuse, the
      entry stays stored and unpublished — zero-loss).
    * ``positive`` — at least one enumerated detector fired.
    * ``clean`` — the gate-admissible verdict: no findings AND no error.
    """

    findings: tuple[DangerFinding, ...] = ()
    error: str | None = None

    @property
    def positive(self) -> bool:
        """True when at least one enumerated detector fired."""
        return bool(self.findings)

    @property
    def clean(self) -> bool:
        """True iff no detector fired and the scan completed (no error)."""
        return not self.findings and self.error is None

    def patterns_by_class(self) -> dict[str, dict[str, int]]:
        """Return ``{detector_class: {pattern_name: count}}`` for logging.

        Pattern names and counts only — by construction this carries no
        matched values, so it is safe for log lines and audit records.
        """
        out: dict[str, dict[str, int]] = {}
        for f in self.findings:
            by_pattern = out.setdefault(f.detector_class, {})
            by_pattern[f.pattern_name] = by_pattern.get(f.pattern_name, 0) + f.count
        return out


# ── Public API ────────────────────────────────────────────────────────────────


def detect(content: str, title: str | None = None) -> DetectionResult:
    """Run every enumerated danger detector over ``content`` and ``title``.

    Pure function: no logging, no state, no input mutation. A detector
    or scanner error is returned inside the result (``error`` set,
    fail-closed) — it never propagates as an exception.

    Args:
        content: The text to scan (the served projection at the
            publication gate). Empty/``None`` → skipped.
        title: Optional title scanned with the same detectors (a title
            can carry an injection payload or a secret exactly like
            content — mirrors ``scan_issuance_item`` which scans both
            echoed fields).

    Returns:
        :class:`DetectionResult` with aggregated findings per
        ``(detector_class, pattern_name)``. Findings from both fields
        are merged; counts add up.
    """
    findings: list[DangerFinding] = []
    try:
        for text in (content or "", title or ""):
            if not text:
                continue

            # ── prompt-injection: case-insensitive substring scan ────
            lower = text.lower()
            injection_counts: dict[str, int] = {}
            for pname, pat in PROMPT_INJECTION_PATTERNS:
                n = lower.count(pat.lower())
                if n:
                    injection_counts[pname] = n
            findings.extend(
                DangerFinding(DETECTOR_CLASS_PROMPT_INJECTION, pname, n)
                for pname, n in injection_counts.items()
            )

            # ── secret: delegate to the existing scanner, keep only ──
            # ── the high-confidence subset (enumerated set)          ──
            secret_counts: dict[str, int] = {}
            for f in detect_secrets(text):
                if f.pattern_name in HIGH_CONFIDENCE_SECRET_PATTERNS:
                    secret_counts[f.pattern_name] = secret_counts.get(f.pattern_name, 0) + 1
            findings.extend(
                DangerFinding(DETECTOR_CLASS_SECRET, pname, n) for pname, n in secret_counts.items()
            )
    except Exception as exc:  # fail-closed by contract: error → result
        # A detector/scanner error is a RESULT (fail-closed signal for
        # the gate), never an exception out of this function. Partial
        # findings collected before the error are preserved for
        # forensics; the caller refuses on ``error`` regardless.
        return DetectionResult(findings=tuple(findings), error=str(exc))
    return DetectionResult(findings=tuple(findings))
