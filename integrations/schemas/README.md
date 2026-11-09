# Vendored canon JSON Schemas — provenance

Vendored copies of the canon record schemas, shipped by the Vesma
integration pack so consumers (e.g. the vesma-eyes Go validator) can
receive pinned schemas natively from the engine deploy
(`vesma integration setup`, kind `schemas`).

- Source of truth: `github.com/vesmaro/vesma-canon`
- Pin tag: `canon-v1.0.1` (commit `4484685f93079a2d3b42925ff207c3b1870ab5c9`)
- **Do not edit.** JSON cannot carry comments, so provenance lives here
  instead of a header inside the files. The deployed copies are
  byte-identical to the pin tag; the engine deploy stamps ownership via a
  sidecar manifest (`vesma-schemas.manifest.json`), never inline.

## Provenance per file (sha256 at the pin tag)

| File | sha256 |
|---|---|
| `envelope.schema.json` | `81a7cc6e5edee88cabd8d45c270595e379e7fd078e6b165bbf4833541ca6b7e8` |
| `checkpoint.schema.json` | `5fb40028e59d556e79b155240d9b606004627903da699a7593c6d4cb74353db1` |
| `task.schema.json` | `c7528cc0e465e82cd1359f69635aa0ed624e81d4b2fd2c28d49ab76cf16087fc` |
| `decision.schema.json` | `3ac9bde8353bce0d955c0ff2c8edd1bb7962767d1859b863c508890209499943` |
| `report.schema.json` | `084e03a3efb0ea7cf0fa9d6c5aa0474603030d72fbd05b2bb1e2225abe2297df` |

## How these copies were produced

```
git -C <vesma-canon> show canon-v1.0.1:schemas/<name>.schema.json \
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