# Vendored canon JSON Schemas — provenance

Vendored copies of the canon record schemas, shipped by the Vesma
integration pack so consumers (e.g. the vesma-eyes Go validator) can
receive pinned schemas natively from the engine deploy
(`vesma integration setup`, kind `schemas`).

- Source of truth: `github.com/vesmaro/vesma-canon`
- Pin tag: `canon-v1.0.0` (commit `d4e998089acde88a9d57551fa71cfa2ef3c23fdf`)
- **Do not edit.** JSON cannot carry comments, so provenance lives here
  instead of a header inside the files. The deployed copies are
  byte-identical to the pin tag; the engine deploy stamps ownership via a
  sidecar manifest (`mnemos-schemas.manifest.json`), never inline.

## Provenance per file (sha256 at the pin tag)

| File | sha256 |
|---|---|
| `envelope.schema.json` | `3b7a57bcda64eb2758460e025ef1756236a03a481d0c4ae8c39cf206ba583f8f` |
| `checkpoint.schema.json` | `e71ad139e4e21ef911c2ab3278df1b0ccbe6c94a9fea8c3db056803261808161` |
| `task.schema.json` | `a08ad9fc718e1be977bd337bbb681a87e11403df1ebe5b024f4a84857a834b0c` |
| `decision.schema.json` | `1a832e8c283c3e076b7f1252a28184544285aa5d32f0a872d30c313067047a3a` |
| `report.schema.json` | `0936b9d147dcb5c5cc1740a9ac32648dae4127b58e47d97fa273906e0595a51a` |

## How these copies were produced

```
git -C <vesma-canon> show canon-v1.0.0:schemas/<name>.schema.json \
  > integrations/schemas/<name>.schema.json
```

Drift is enforced by `tests/test_integration.py::TestSchemasPack` — it
compares every shipped file against the sibling canon repo checkout when
available and against the frozen sha256 table above otherwise. To bump
the pin: re-run the command above for the new tag, update this table and
`SCHEMAS_SOURCE_PIN` in `src/vesma/cli/integration.py` in the same
change.

Schema semantics (record model, envelope, per-type sections, strictness)
are documented upstream in the canon repo's `schemas/README.md`.