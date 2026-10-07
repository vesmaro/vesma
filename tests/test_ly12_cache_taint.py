"""Cache taint + write-API tests (specs/layout/v1 §3.9, checklist LY-12).

Contract legs under test:

- env-file values carry the secret taint AT LOAD TIME (TaintedValue /
  TaintedValues — transparent str/dict subclasses, non-breaking);
- the engine cache-write API (:func:`put_under_cache`) REFUSES a tainted
  value — declared via ``tainted=True`` or carried by the type — with a
  typed error that never echoes the value;
- an untainted value is accepted, the directory is 0750 umask-independent,
  and deleting the cache directory never breaks operation (regenerable:
  the next write recreates it).

Honest scope (documented in the conformance report): the engine today has
NO feature writing env-file values into caches — the LY-12 refusal leg is
therefore proven against the API itself (the guard exists before the
first writer), per the checklist's probe-write formulation.
"""

from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

from vesma.service.cache import CACHE_DIR_MODE, CacheWriteRefusedError, put_under_cache
from vesma.service.envfile import TaintedValue, TaintedValues, load_env_file


def _write_env(path: Path, body: str = "ALPHA=one\nTOKEN=super-secret-value\n") -> Path:
    path.write_text(body, encoding="utf-8")
    os.chmod(path, 0o600)
    return path


class TestTaintAtLoad:
    def test_env_file_values_carry_taint(self, tmp_path: Path) -> None:
        values = load_env_file(_write_env(tmp_path / "comp.env"))
        assert isinstance(values, TaintedValues)
        assert isinstance(values["TOKEN"], TaintedValue)
        assert isinstance(values["ALPHA"], TaintedValue)

    def test_taint_is_nonbreaking_for_existing_consumers(self, tmp_path: Path) -> None:
        values = load_env_file(_write_env(tmp_path / "comp.env"))
        # dict/str semantics every existing caller relies on:
        assert values == {"ALPHA": "one", "TOKEN": "super-secret-value"}
        assert values["TOKEN"] == "super-secret-value"
        env: dict[str, str] = {"PATH": "/bin"}
        env.update(values)  # supervisor._construct_env pattern
        assert env["TOKEN"] == "super-secret-value"
        assert isinstance(env["TOKEN"], TaintedValue)  # taint survives update()


class TestWriteApiRefusal:
    def test_probe_write_of_env_sourced_value_refused(self, tmp_path: Path) -> None:
        values = load_env_file(_write_env(tmp_path / "comp.env"))
        with pytest.raises(CacheWriteRefusedError) as exc:
            put_under_cache("mycomp", "probe", values["TOKEN"])
        # The refusal names component/key, never the VALUE.
        assert "mycomp" in str(exc.value)
        assert "super-secret-value" not in str(exc.value)
        assert "LY-12" in str(exc.value)

    def test_declared_taint_refused_even_for_plain_str(self, tmp_path: Path) -> None:
        with pytest.raises(CacheWriteRefusedError):
            put_under_cache("mycomp", "probe", "innocent-looking", tainted=True)
        assert not (tmp_path / "cache").exists()  # nothing written on refusal

    def test_every_mapping_value_refused(self, tmp_path: Path) -> None:
        values = load_env_file(_write_env(tmp_path / "comp.env"))
        for _key, value in values.items():
            with pytest.raises(CacheWriteRefusedError):
                put_under_cache("mycomp", "probe", value)

    def test_tainted_bytes_refused(self, tmp_path: Path) -> None:
        with pytest.raises(CacheWriteRefusedError):
            put_under_cache("mycomp", "probe", b"\x00\x01", tainted=True)


class TestUntaintedAccepted:
    def test_untainted_value_accepted_with_dir_mode(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
        old_umask = os.umask(0o077)  # LY-01 posture: modes are explicit, not umask-derived
        try:
            path = put_under_cache("mycomp", "embeddings.bin", "payload")
        finally:
            os.umask(old_umask)
        assert path.read_text(encoding="utf-8") == "payload"
        cache_root = tmp_path / "cache" / "vesma" / "mycomp"
        assert stat.S_IMODE(cache_root.stat().st_mode) == CACHE_DIR_MODE == 0o750

    def test_bytes_value_accepted(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
        path = put_under_cache("mycomp", "blob", b"\x00\x01\x02")
        assert path.read_bytes() == b"\x00\x01\x02"

    def test_overwrite_is_atomic_replacement(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
        put_under_cache("mycomp", "state", "v1")
        path = put_under_cache("mycomp", "state", "v2")
        assert path.read_text(encoding="utf-8") == "v2"
        assert not [p for p in path.parent.iterdir() if p.name.startswith(".tmp-")]

    def test_taint_types_are_distinct_from_plain_str(self) -> None:
        assert isinstance(TaintedValue("x"), str)
        assert not isinstance("x", TaintedValue)


class TestBoundaryValidation:
    @pytest.mark.parametrize("name", ["../evil", "Upper", "has space", "", "a" * 64])
    def test_bad_component_name_refused(self, name: str) -> None:
        with pytest.raises(CacheWriteRefusedError):
            put_under_cache(name, "key", "v")

    @pytest.mark.parametrize("key", ["../x", "/abs", "a/b", ".hidden", "", "k" * 129])
    def test_bad_key_refused(self, key: str) -> None:
        with pytest.raises(CacheWriteRefusedError):
            put_under_cache("mycomp", key, "v")


class TestRegenerable:
    def test_rm_rf_cache_does_not_break_operation(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
        first = put_under_cache("mycomp", "state", "v1")
        assert first.exists()
        # rm -rf the whole cache root — the LY-12 checklist leg.
        cache_root = tmp_path / "cache" / "vesma"
        for child in sorted(cache_root.rglob("*"), reverse=True):
            if child.is_dir():
                child.rmdir()
            else:
                child.unlink()
        cache_root.rmdir()
        # The next write regenerates the directory: operation unchanged.
        second = put_under_cache("mycomp", "state", "v2")
        assert second.exists()
        assert second.read_text(encoding="utf-8") == "v2"
        assert stat.S_IMODE(second.parent.stat().st_mode) == 0o750
