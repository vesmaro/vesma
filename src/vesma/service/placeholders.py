"""argv placeholder expansion (component-manifest v1 §2/§3.5 + layout v1 §3.4).

ONLY the four allowlist tokens are ever substituted — no environment
interpolation, no shell. Well-formed placeholders (``{[a-z_]+}``) outside
the allowlist are refused with PLACEHOLDER_UNKNOWN; a ``{venv_bin}``
reference for a component that has no venv is refused fail-closed with a
diagnostic (layout §3.4: "не расширяется" — the start is refused, not
silently passed through).
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence

from vesma.service.errors import PLACEHOLDER_UNKNOWN, ManifestError
from vesma.service.manifest import PLACEHOLDER_ALLOWLIST

_WELL_FORMED_RE = re.compile(r"^\{[a-z_]+\}$")
_ANY_BRACES_RE = re.compile(r"\{[^{}]*\}")


def expand(argv: Sequence[str], mapping: Mapping[str, str]) -> list[str]:
    """Expand allowlisted placeholders in every argv element.

    ``mapping`` carries the resolved layout targets (see
    ``vesma.service.layout.resolve_component_paths``). Raises
    ManifestError(PLACEHOLDER_UNKNOWN) when a well-formed placeholder is
    outside the allowlist or cannot be resolved from ``mapping`` — the
    venv-less ``{venv_bin}`` case is reported with its own diagnostic.
    """
    # Validate the whole argv first: refuse before returning anything.
    for position, element in enumerate(argv):
        for match in _ANY_BRACES_RE.finditer(element):
            token = match.group(0)
            if _WELL_FORMED_RE.match(token) is None:
                continue  # literal braces ({}, {a) are not placeholders
            name = token[1:-1]
            if name not in PLACEHOLDER_ALLOWLIST:
                raise ManifestError(
                    PLACEHOLDER_UNKNOWN,
                    f"argv[{position}]",
                    f"placeholder {{{name}}} is outside the allowlist "
                    f"{sorted(PLACEHOLDER_ALLOWLIST)}",
                )
            if name == "venv_bin" and name not in mapping:
                raise ManifestError(
                    PLACEHOLDER_UNKNOWN,
                    f"argv[{position}]",
                    "placeholder {venv_bin} referenced but the component has no "
                    "venv — refusing to spawn (fail-closed, specs/layout/v1 §3.4)",
                )
            if name not in mapping:
                raise ManifestError(
                    PLACEHOLDER_UNKNOWN,
                    f"argv[{position}]",
                    f"placeholder {{{name}}} cannot be resolved from the supplied layout mapping",
                )

    def replace(match: re.Match[str]) -> str:
        token = match.group(0)
        name = token[1:-1]
        if _WELL_FORMED_RE.match(token) is not None and name in mapping:
            return mapping[name]
        return token  # non-placeholder braces stay literal

    return [_ANY_BRACES_RE.sub(replace, element) for element in argv]
