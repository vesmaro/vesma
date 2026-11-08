"""Card cfe784f4 — the vendored canon schemas speak the 6.0 tag canon.

``integrations/schemas/*.schema.json`` still described the tag convention
with the LEGACY ``mnemos:<subtype>`` prefix (pre-rename drift) and taught
self-authoring the ``agent:`` tag — both retired. This suite pins:

1. no legacy ``mnemos:<subtype>`` prefix anywhere in the vendored schemas
   (``mnemos:no-federate`` is BYTE-STABLE FOREVER as the exclusion marker —
   its mentions, if any, are the exception and stay);
2. the ``vesma:<subtype>`` canon prefix is present per record type;
3. the two-layer convention: the agent-tag description says the tag is
   minted by the server from the token — never authored by the caller;
4. every file stays valid JSON (descriptions are prose, the documents are
   machine-read).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

SCHEMAS_DIR = Path(__file__).resolve().parent.parent / "integrations" / "schemas"

#: filename → the canon vesma:<subtype> its tags description must carry.
RECORD_SCHEMAS: dict[str, str] = {
    "checkpoint.schema.json": "vesma:checkpoint",
    "decision.schema.json": "vesma:decision",
    "report.schema.json": "vesma:session",
    "task.schema.json": "vesma:open-question",
}


def _load(name: str) -> dict[str, Any]:
    return json.loads((SCHEMAS_DIR / name).read_text(encoding="utf-8"))


def _tags_description(schema: dict[str, Any]) -> str:
    return schema["properties"]["tags"]["description"]


class TestVendoredCanonSchemas:
    @pytest.mark.parametrize("name", list(RECORD_SCHEMAS))
    def test_valid_json(self, name: str) -> None:
        assert _load(name)["title"]

    @pytest.mark.parametrize("name", list(RECORD_SCHEMAS))
    def test_no_legacy_subtype_prefix(self, name: str) -> None:
        """No ``mnemos:<subtype>`` left — except the byte-stable
        no-federate marker, which is not a subtype spelling."""
        text = json.dumps(_load(name), ensure_ascii=False)
        for token in text.split():
            assert token == "mnemos:no-federate" or "mnemos:" not in token, (
                f"{name}: legacy mnemos:* spelling survived: {token}"
            )

    @pytest.mark.parametrize("name,subtype", sorted(RECORD_SCHEMAS.items()))
    def test_canon_subtype_is_vesma_prefixed(self, name: str, subtype: str) -> None:
        assert subtype in _tags_description(_load(name))

    @pytest.mark.parametrize("name", list(RECORD_SCHEMAS))
    def test_agent_tag_is_server_minted(self, name: str) -> None:
        """The two-layer convention (ADR-0050 addendum): the agent: tag is
        minted by the server from the token — the caller never authors it."""
        desc = _tags_description(_load(name))
        assert "minted by the server from the token" in desc
        assert "do NOT author it yourself" in desc
