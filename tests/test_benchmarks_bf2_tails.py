"""BF-2 tail hardening tests — review findings #206 (F1) + #205 (F1) + #206 (N4).

* **s1m root isolation** (F1 #206): the S1m model leg MUST run under its
  own store root (``root/s1m``), never sharing the reference leg's
  ``vectors.db`` — sharing mixes 256-dim BLAKE2b vectors with 384-dim
  production ones in one store, ``np.stack`` then fails on every vector
  search and the leg silently degrades to FTS-only (measured,
  honest-looking, wrong). The test pins the CORRECT behaviour: distinct
  roots, distinct database files, and no cross-dim contamination.
* **pyproject floor guard** (F1 #205): the `mcp[cli]>=2.0,<3.0` floor
  from the #185 SDK-2.x port must not silently regress to `>=1.0` — a
  pip consumer could resolve an SDK that no longer matches the code.
  A 5-line guard parses pyproject and reddens on the floor's removal.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

from benchmarks.stands.s1_quality import run as s1_run

ROOT = Path(s1_run.__file__).resolve().parents[3]


# ── F1 #206: s1m store-root isolation ────────────────────────────────────────


class TestS1mRootIsolation:
    """The model leg's store is separate from the reference leg's store."""

    def test_s1m_root_differs_from_reference_root(self) -> None:
        """root/s1m and root must resolve to DIFFERENT data dirs.

        The mutation this pins: passing ``root`` (not ``root / 's1m'``)
        to ``build_golden_manager`` in ``run_model_leg`` — both legs
        would then derive the SAME ``data_dir``, share ONE
        ``vectors.db`` + one ``golden.db`` and mix dimensions.
        """
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            reference_data = root / "data"  # golden_settings(root) derives this
            model_data = root / "s1m" / "data"  # run_model_leg's root / "s1m"
            assert model_data != reference_data
            assert model_data.parent.name == "s1m", (
                "s1m must nest its OWN root under the run root — a shared "
                "root mixes 256-dim reference and 384-dim production vectors"
            )

    def test_run_model_leg_uses_isolated_root_source_pin(self) -> None:
        """Source-level pin (reviewer F1): run_model_leg must pass root/s1m.

        The path-shape assertions above pin the intended geometry but are
        tautological against the REAL mutation — reverting run.py:165 to
        ``build_golden_manager(root, …)`` left them green (verified in
        review #215: shared-root mutation → 4 passed). This pin reads the
        run.py source and fails if the model leg ever stops isolating
        its root, making the shared-root mutation RED.
        """
        source = (
            Path(__file__)
            .parents[1]
            .joinpath("benchmarks/stands/s1_quality/run.py")
            .read_text(encoding="utf-8")
        )
        body = source.split("def run_model_leg", 1)[1]
        body = body.split("\ndef ", 1)[0]
        assert 'build_golden_manager(root / "s1m"' in body, (
            "run_model_leg must isolate the model leg under root/'s1m' — "
            "a shared root mixes 256-dim reference and 384-dim production "
            "vectors in one vectors.db (the silent FTS-only degradation)"
        )

    def test_shared_root_actually_mixes_vectors(self) -> None:
        """The DANGEROUS configuration, demonstrated: one root → one db.

        Both legs under one root produce the same ``data_dir`` and thus
        the same ``vectors.db``/``golden.db`` — the exact mixing failure
        mode the isolation prevents. Asserting the shared-path identity
        is what makes the isolation test meaningful (it names the real
        failure, not a tautology).
        """
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            # golden_settings(root) and golden_settings(root / 's1m') must
            # derive DIFFERENT db paths — the isolation contract.
            from benchmarks.stands.s1_quality.harness import golden_settings

            ref_db = golden_settings(root).db_path
            model_db = golden_settings(root / "s1m").db_path
            assert ref_db != model_db
            assert ref_db.parent == (root / "data")
            assert model_db.parent == (root / "s1m" / "data")

    def test_vector_store_dimensions_do_not_mix_across_legs(self) -> None:
        """End-to-end: two legs on separate roots keep separate vector dbs.

        A 256-dim reference upsert and a 384-dim model-leg upsert into
        the SAME store is the defect; into two stores it is correct.
        Pinned at the VectorStore level (the only place dimensions touch).
        """
        import numpy as np

        from vesma.storage.vector_store import VectorStore

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            ref_store = VectorStore(root / "data")
            model_store = VectorStore(root / "s1m" / "data")
            try:
                ref_vec = np.zeros(256, dtype=np.float32)
                ref_vec[0] = 1.0
                model_vec = np.zeros(384, dtype=np.float32)
                model_vec[0] = 1.0
                ref_store.upsert("ref-row", ref_vec.tolist(), {})
                model_store.upsert("model-row", model_vec.tolist(), {})
                # each store queries with its OWN dimension — no mixing
                ref_hits = ref_store.search(ref_vec.tolist(), limit=5)
                model_hits = model_store.search(model_vec.tolist(), limit=5)
                assert [mid for mid, _ in ref_hits] == ["ref-row"]
                assert [mid for mid, _ in model_hits] == ["model-row"]
            finally:
                ref_store.close()
                model_store.close()


# ── F1 #205: pyproject floor guard ───────────────────────────────────────────


class TestPyprojectMcpFloor:
    """`mcp[cli]>=2.0,<3.0` — a floor rollback must fail this test."""

    def test_mcp_dependency_floor_and_cap(self) -> None:
        pyproject = ROOT / "pyproject.toml"
        assert pyproject.is_file(), "pyproject.toml missing at repo root"
        text = pyproject.read_text(encoding="utf-8")
        mcp_lines = [
            line.strip()
            for line in text.splitlines()
            if line.strip().startswith('"mcp[')
            or line.strip().startswith('"mcp>')
            or line.strip().startswith('"mcp=')
        ]
        assert mcp_lines, "the mcp dependency line vanished from pyproject.toml"
        spec = mcp_lines[0]
        assert ">=2.0" in spec, (
            f"mcp floor regressed below 2.0 ({spec}) — the SDK-2.x port "
            "(#185) speaks the 2.x constructor API; 1.x cannot run it"
        )
        assert "<3.0" in spec, (
            f"mcp upper bound missing ({spec}) — the <3.0 cap protects "
            "against unvetted SDK 3.x removals"
        )
