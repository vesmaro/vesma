"""Tests for the S2 lazy fetch (`vesmaro.lazy_fetch` + `mnemos fetch`).

Coverage map (task brief):

* resolution — remote origin → fetch item; unknown origin peer → honest
  error listing configured peers; ``self``/empty origin with a local
  body → "already local" skip; ``self`` without a body → index
  inconsistency error; id absent from the mirror → error; tombstoned →
  skip; duplicate ``--id`` values collapse;
* plan + confirmation — render, and the ``y/N`` gate (y/yes/n/EOF,
  non-TTY refusal with the ``--yes`` hint) mirroring ``mnemos-mesh
  pull``'s ``confirmPull``;
* mesh CLI leg — the Go-track ``fetch --json`` contract
  (``{"records": [...], "not_found": [...]}``) parsed through a REAL
  subprocess script double (injected via ``federation.fetch.mesh_bin``):
  argv shape, non-zero exit, broken JSON, noisy stdout;
* import — the in-process path reuses :rpc:`WriteMemory`'s core
  (``VesmaCoreServicer.import_compact_record``): written / duplicate
  (#359/#362 by fed_id) / gated (ACL/moderation) outcomes, all against
  the REAL MemoryManager (moderation + Layer 1 scanner run for real);
* CLI — ``mnemos fetch`` happy path, --yes requirement under CliRunner
  (non-TTY), already-local exit 0, unknown peer exit 1, failing mesh
  CLI exit 1, unconfigured ``mesh_config_path`` exit 1.
"""

from __future__ import annotations

import json
import logging
import os
import stat
from collections.abc import Generator
from io import StringIO
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest
import yaml
from typer.testing import CliRunner

from vesmaro.cli._manager import reset_manager
from vesmaro.cli.main import app
from vesmaro.compact import CompactRecord, FederationIndexEntry
from vesmaro.config import FederationConfig, PeerConfig, Settings
from vesmaro.lazy_fetch import (
    FETCH_TIMEOUT_S,
    SKIP_ALREADY_LOCAL,
    SKIP_TOMBSTONED,
    FetchEnvelope,
    FetchPeerError,
    FetchResolutionError,
    confirm_fetch,
    fetch_from_peer,
    resolve_fetch_plan,
    run_fetch,
)
from vesmaro.manager import MemoryManager
from vesmaro.mesh_server import VesmaCoreServicer
from vesmaro.models import MemoryCreate, MemorySource
from vesmaro.storage.sqlite_store import SQLiteStore

runner = CliRunner()

_PROJECT = "proj"
_PEER = "peer-a"
_TOKEN_ENV = "VESMARO_TEST_FETCH_TOKEN"
_AGENT = "agent-x"

#: The script double — installed as ``federation.fetch.mesh_bin`` so the
#: fetch leg spawns a REAL subprocess (same pattern as the meta-poller
#: tests). Behaviour is driven by the JSON state file
#: (``MNEMOS_TEST_FETCH_STATE``): ``records`` / ``not_found`` (the
#: envelope answer), ``fail`` / ``fail_code`` (non-zero exit + stderr),
#: ``bad_json`` (garbage stdout), ``noise`` (INFO lines on stdout
#: BEFORE the envelope — the noisy-wire hardening). Every invocation
#: records its argv into ``calls``.
FETCH_MESH_DOUBLE = '''#!/usr/bin/env python3
"""Script double for `mnemos-mesh fetch` (test fixture)."""
import json
import os
import sys


def main() -> int:
    argv = sys.argv[1:]
    state_path = os.environ["MNEMOS_TEST_FETCH_STATE"]
    with open(state_path) as f:
        state = json.load(f)
    state.setdefault("calls", []).append(argv)
    if state.get("fail"):
        with open(state_path, "w") as f:
            json.dump(state, f)
        sys.stderr.write(str(state["fail"]))
        return int(state.get("fail_code", 3))
    for i in range(int(state.get("noise") or 0)):
        sys.stdout.write(f"INFO 2026-09-21T10:00:00Z mesh leg chatter line {i}\\n")
    if state.get("bad_json"):
        with open(state_path, "w") as f:
            json.dump(state, f)
        sys.stdout.write("this is not json at all")
        return 0
    envelope = {
        "records": state.get("records", []),
        "not_found": state.get("not_found", []),
    }
    with open(state_path, "w") as f:
        json.dump(state, f)
    sys.stdout.write(json.dumps(envelope))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
'''


class MeshFetchDouble:
    """Installer/inspector for the fetch script double."""

    def __init__(self, tmp_path: Path) -> None:
        self.bin_path = tmp_path / "mesh-fetch-double"
        self.bin_path.write_text(FETCH_MESH_DOUBLE)
        self.bin_path.chmod(self.bin_path.stat().st_mode | stat.S_IEXEC)
        self.state_path = tmp_path / "fetch-state.json"
        self.state_path.write_text(json.dumps({}))

    def set(self, spec: dict[str, Any]) -> None:
        self.state_path.write_text(json.dumps(spec))

    def reload(self) -> dict[str, Any]:
        return json.loads(self.state_path.read_text())


@pytest.fixture
def mesh(tmp_path: Path) -> MeshFetchDouble:
    d = MeshFetchDouble(tmp_path)
    os.environ["MNEMOS_TEST_FETCH_STATE"] = str(d.state_path)
    return d


def _settings(
    tmp_path: Path,
    mesh: MeshFetchDouble,
    *,
    peers: dict[str, Any] | None = None,
    db_name: str = "lazy-fetch.db",
) -> Settings:
    if peers is None:
        peers = {_PEER: {"bearer_token_env": _TOKEN_ENV, "allowed_projects": [_PROJECT]}}
    settings = Settings(
        **{  # type: ignore[arg-type]  # pydantic dict→model coercion
            "mnemos": {
                "vault_path": str(tmp_path / "vault"),
                "data_dir": str(tmp_path / "data"),
                "db_name": db_name,
            },
            "embedding": {"provider": "nano"},
            "scanner": {"enabled": False},
            "federation": FederationConfig(
                shared_projects=[_PROJECT],
                peers={
                    pid: PeerConfig(
                        bearer_token_env=str(spec["bearer_token_env"]),
                        allowed_projects=list(spec.get("allowed_projects", [_PROJECT])),
                    )
                    for pid, spec in peers.items()
                },
                fetch={
                    "mesh_bin": str(mesh.bin_path),
                    "mesh_config_path": str(tmp_path / "mesh.yaml"),
                },
            ),
        }
    )
    settings.resolve_paths()
    return settings


@pytest.fixture
def manager(tmp_path: Path, mesh: MeshFetchDouble) -> Generator[MemoryManager, None, None]:
    mgr = MemoryManager(_settings(tmp_path, mesh))
    mock_embedder = MagicMock()
    mock_embedder.embed.return_value = [0.1] * 384
    mgr._embedder = mock_embedder
    yield mgr
    mgr.close()


def _entry(
    fed_id: str,
    *,
    origin_peer: str = _PEER,
    project: str = _PROJECT,
    title: str = "Remote decision",
    content_state: str = "available",
) -> FederationIndexEntry:
    return FederationIndexEntry(
        id=fed_id,
        type="decision",
        title=title,
        tags=[f"project:{project}", f"agent:{_AGENT}", "mnemos:decision"],
        project=project,
        source_agent=_AGENT,
        source_peer=origin_peer,
        origin_peer=origin_peer,
        content_state=content_state,
        timestamp="2026-09-21T09:00:00Z",
    )


def _seed_index(store: SQLiteStore, *entries: FederationIndexEntry) -> None:
    stats = store.upsert_index_entries(list(entries))
    assert stats.written == len(entries)


def _record(
    fed_id: str, *, project: str = _PROJECT, title: str = "Remote decision"
) -> CompactRecord:
    return CompactRecord(
        id=fed_id,
        type="decision",
        title=title,
        summary="A decision fetched lazily from the origin peer.",
        key_points=["point one"],
        tags=[f"project:{project}", f"agent:{_AGENT}", "mnemos:decision"],
        source_agent=_AGENT,
        timestamp="2026-09-21T09:00:00Z",
    )


def _yes_stdin(answer: str = "y\n") -> StringIO:
    return StringIO(answer)


def _always_tty() -> bool:
    return True


def _never_tty() -> bool:
    return False


# ── Resolution ────────────────────────────────────────────────────────────────


class TestResolveFetchPlan:
    def test_remote_origin_fetch_item(self, manager: MemoryManager) -> None:
        _seed_index(manager.sqlite, _entry("fed:agent-x:1"))
        plan = resolve_fetch_plan(manager.sqlite, manager.settings.federation, ["fed:agent-x:1"])
        assert len(plan.fetch_items) == 1
        item = plan.fetch_items[0]
        assert item.origin_peer == _PEER
        assert item.project == _PROJECT
        assert item.title == "Remote decision"
        assert plan.skipped_items == []

    def test_unknown_origin_peer_lists_configured(self, manager: MemoryManager) -> None:
        _seed_index(manager.sqlite, _entry("fed:agent-x:2", origin_peer="ghost-peer"))
        with pytest.raises(FetchResolutionError, match=r"ghost-peer.*peer-a"):
            resolve_fetch_plan(manager.sqlite, manager.settings.federation, ["fed:agent-x:2"])

    def test_self_origin_already_local(self, manager: MemoryManager) -> None:
        manager.add(
            MemoryCreate(
                content="Local body for a self-origin index row.",
                title="Local decision",
                tags=[f"project:{_PROJECT}", f"agent:{_AGENT}", "mnemos:decision"],
                source=MemorySource.MCP,
                metadata={"fed_id": "fed:agent-x:3"},
            ),
            project=_PROJECT,
            agent=_AGENT,
        )
        _seed_index(manager.sqlite, _entry("fed:agent-x:3", origin_peer="self"))
        plan = resolve_fetch_plan(manager.sqlite, manager.settings.federation, ["fed:agent-x:3"])
        assert plan.fetch_items == []
        assert [i.skip_reason for i in plan.skipped_items] == [SKIP_ALREADY_LOCAL]

    def test_self_origin_without_body_is_index_error(self, manager: MemoryManager) -> None:
        _seed_index(manager.sqlite, _entry("fed:agent-x:4", origin_peer="self"))
        with pytest.raises(FetchResolutionError, match="inconsistent"):
            resolve_fetch_plan(manager.sqlite, manager.settings.federation, ["fed:agent-x:4"])

    def test_empty_origin_treated_as_self(self, manager: MemoryManager) -> None:
        _seed_index(manager.sqlite, _entry("fed:agent-x:5", origin_peer=""))
        with pytest.raises(FetchResolutionError, match="inconsistent"):
            resolve_fetch_plan(manager.sqlite, manager.settings.federation, ["fed:agent-x:5"])

    def test_unknown_id_is_error(self, manager: MemoryManager) -> None:
        with pytest.raises(FetchResolutionError, match="not present in the local federation_index"):
            resolve_fetch_plan(manager.sqlite, manager.settings.federation, ["fed:agent-x:nope"])

    def test_tombstoned_skipped(self, manager: MemoryManager) -> None:
        _seed_index(manager.sqlite, _entry("fed:agent-x:6", content_state="tombstoned"))
        plan = resolve_fetch_plan(manager.sqlite, manager.settings.federation, ["fed:agent-x:6"])
        assert plan.fetch_items == []
        assert [i.skip_reason for i in plan.skipped_items] == [SKIP_TOMBSTONED]

    def test_duplicate_ids_collapse(self, manager: MemoryManager) -> None:
        _seed_index(manager.sqlite, _entry("fed:agent-x:7"))
        plan = resolve_fetch_plan(
            manager.sqlite, manager.settings.federation, ["fed:agent-x:7", "fed:agent-x:7"]
        )
        assert [i.fed_id for i in plan.fetch_items] == ["fed:agent-x:7"]

    def test_peer_groups_bucket_by_origin(
        self, manager: MemoryManager, tmp_path: Path, mesh: MeshFetchDouble
    ) -> None:
        two_peers = _settings(
            tmp_path,
            mesh,
            peers={
                _PEER: {"bearer_token_env": _TOKEN_ENV},
                "peer-b": {"bearer_token_env": _TOKEN_ENV},
            },
        )
        _seed_index(
            manager.sqlite,
            _entry("fed:agent-x:8", origin_peer=_PEER),
            _entry("fed:agent-x:9", origin_peer="peer-b"),
        )
        plan = resolve_fetch_plan(
            manager.sqlite, two_peers.federation, ["fed:agent-x:8", "fed:agent-x:9"]
        )
        assert plan.peer_groups() == {_PEER: ["fed:agent-x:8"], "peer-b": ["fed:agent-x:9"]}


# ── Confirmation ──────────────────────────────────────────────────────────────


class TestConfirmFetch:
    def _confirm(self, answer: str) -> bool:
        out = StringIO()
        ok = confirm_fetch(StringIO(answer), out, _always_tty)
        assert "Proceed with fetch? [y/N]:" in out.getvalue()
        return ok

    def test_yes_variants_accepted(self) -> None:
        assert self._confirm("y\n")
        assert self._confirm("Y\n")
        assert self._confirm("yes\n")
        assert self._confirm("  yes  \n")

    def test_declined_variants(self) -> None:
        assert not self._confirm("n\n")
        assert not self._confirm("N\n")
        assert not self._confirm("\n")
        assert not self._confirm("nope\n")

    def test_eof_refuses(self) -> None:
        assert not self._confirm("")

    def test_non_tty_refuses_with_hint(self) -> None:
        out = StringIO()
        assert not confirm_fetch(StringIO("y\n"), out, _never_tty)
        assert "--yes" in out.getvalue()
        assert "Proceed with fetch?" not in out.getvalue()


# ── Mesh CLI leg (real subprocess double) ────────────────────────────────────


class TestFetchFromPeer:
    def test_argv_shape_and_envelope(self, mesh: MeshFetchDouble, tmp_path: Path) -> None:
        mesh.set(
            {
                "records": [{"id": "fed:agent-x:1", "type": "decision", "title": "t"}],
                "not_found": ["fed:agent-x:2"],
            }
        )
        envelope = fetch_from_peer(
            str(mesh.bin_path),
            str(tmp_path / "mesh.yaml"),
            _PEER,
            ["fed:agent-x:1", "fed:agent-x:2"],
        )
        calls = mesh.reload()["calls"]
        # The double records sys.argv[1:] — the bin path itself is implicit
        # (it IS the script being executed).
        assert calls == [
            [
                "fetch",
                "--config",
                str(tmp_path / "mesh.yaml"),
                "--peer",
                _PEER,
                "--id",
                "fed:agent-x:1",
                "--id",
                "fed:agent-x:2",
                "--json",
            ]
        ]
        assert isinstance(envelope, FetchEnvelope)
        assert envelope.records[0]["id"] == "fed:agent-x:1"
        assert envelope.not_found == ["fed:agent-x:2"]

    def test_nonzero_exit(self, mesh: MeshFetchDouble, tmp_path: Path) -> None:
        mesh.set({"fail": "peer leg down", "fail_code": 4})
        with pytest.raises(FetchPeerError, match="exited 4: peer leg down"):
            fetch_from_peer(
                str(mesh.bin_path), str(tmp_path / "mesh.yaml"), _PEER, ["fed:agent-x:1"]
            )

    def test_bad_json(self, mesh: MeshFetchDouble, tmp_path: Path) -> None:
        mesh.set({"bad_json": True})
        with pytest.raises(FetchPeerError, match="not valid JSON"):
            fetch_from_peer(
                str(mesh.bin_path), str(tmp_path / "mesh.yaml"), _PEER, ["fed:agent-x:1"]
            )

    def test_noisy_stdout_prefix_chatter(self, mesh: MeshFetchDouble, tmp_path: Path) -> None:
        mesh.set(
            {"noise": 3, "records": [{"id": "fed:agent-x:1", "type": "decision", "title": "t"}]}
        )
        envelope = fetch_from_peer(
            str(mesh.bin_path), str(tmp_path / "mesh.yaml"), _PEER, ["fed:agent-x:1"]
        )
        assert len(envelope.records) == 1

    def test_invalid_envelope_rejected(self, mesh: MeshFetchDouble, tmp_path: Path) -> None:
        # not_found must be a list — an int violates the contract.
        mesh.set({"not_found": 7})
        with pytest.raises(FetchPeerError, match="envelope violates contract"):
            fetch_from_peer(
                str(mesh.bin_path), str(tmp_path / "mesh.yaml"), _PEER, ["fed:agent-x:1"]
            )

    def test_missing_binary(self, tmp_path: Path) -> None:
        with pytest.raises(FetchPeerError, match="cannot execute mesh bin"):
            fetch_from_peer(str(tmp_path / "does-not-exist"), "mesh.yaml", _PEER, ["fed:agent-x:1"])

    def test_timeout_constant_is_bounded(self) -> None:
        assert 0 < FETCH_TIMEOUT_S <= 120


# ── Orchestration: fetch → import (the WriteMemory path, in-process) ─────────


class TestRunFetch:
    def _prepare(
        self, manager: MemoryManager, mesh: MeshFetchDouble, fed_id: str = "fed:agent-x:1"
    ) -> None:
        _seed_index(manager.sqlite, _entry(fed_id))
        mesh.set({"records": [_record(fed_id).model_dump()], "not_found": []})

    def test_written(self, manager: MemoryManager, mesh: MeshFetchDouble) -> None:
        self._prepare(manager, mesh)
        stats = run_fetch(
            manager.sqlite,
            manager,
            manager.settings,
            ["fed:agent-x:1"],
            assume_yes=True,
            stdin=StringIO(),
            stdout=StringIO(),
        )
        assert (stats.fetched, stats.imported, stats.duplicates, stats.errors) == (1, 1, 0, 0)
        dup = manager.sqlite.find_federated_duplicate(fed_id="fed:agent-x:1")
        assert dup is not None
        assert dup.metadata.get("fed_id") == "fed:agent-x:1"
        assert "project:" in " ".join(dup.tags)

    def test_duplicate_on_replay(self, manager: MemoryManager, mesh: MeshFetchDouble) -> None:
        self._prepare(manager, mesh)
        first = run_fetch(
            manager.sqlite,
            manager,
            manager.settings,
            ["fed:agent-x:1"],
            assume_yes=True,
            stdin=StringIO(),
            stdout=StringIO(),
        )
        assert (first.imported, first.duplicates) == (1, 0)
        second = run_fetch(
            manager.sqlite,
            manager,
            manager.settings,
            ["fed:agent-x:1"],
            assume_yes=True,
            stdin=StringIO(),
            stdout=StringIO(),
        )
        assert (second.imported, second.duplicates, second.errors) == (0, 1, 0)
        # The duplicate gate (#359/#362 by fed_id) is the SHARED path's;
        # the existing row is reported, never re-written.
        assert manager.sqlite.find_federated_duplicate(fed_id="fed:agent-x:1") is not None

    def test_not_found(self, manager: MemoryManager, mesh: MeshFetchDouble) -> None:
        _seed_index(manager.sqlite, _entry("fed:agent-x:1"), _entry("fed:agent-x:2"))
        mesh.set(
            {"records": [_record("fed:agent-x:1").model_dump()], "not_found": ["fed:agent-x:2"]}
        )
        stats = run_fetch(
            manager.sqlite,
            manager,
            manager.settings,
            ["fed:agent-x:1", "fed:agent-x:2"],
            assume_yes=True,
            stdin=StringIO(),
            stdout=StringIO(),
        )
        assert (stats.imported, stats.not_found, stats.errors) == (1, 1, 0)

    def test_peer_failure_counts_errors(
        self, manager: MemoryManager, mesh: MeshFetchDouble
    ) -> None:
        _seed_index(manager.sqlite, _entry("fed:agent-x:1"))
        mesh.set({"fail": "mesh leg down"})
        stats = run_fetch(
            manager.sqlite,
            manager,
            manager.settings,
            ["fed:agent-x:1"],
            assume_yes=True,
            stdin=StringIO(),
            stdout=StringIO(),
        )
        assert (stats.fetched, stats.imported, stats.errors) == (0, 0, 1)

    def test_gated_acl_refusal_is_not_an_error(
        self, manager: MemoryManager, mesh: MeshFetchDouble
    ) -> None:
        # The record's project is outside peer-a's allowed set — the
        # WriteMemory ACL gate refuses it; policy, not transport.
        _seed_index(manager.sqlite, _entry("fed:agent-x:1", project=_PROJECT))
        mesh.set(
            {
                "records": [_record("fed:agent-x:1", project="other-project").model_dump()],
                "not_found": [],
            }
        )
        stats = run_fetch(
            manager.sqlite,
            manager,
            manager.settings,
            ["fed:agent-x:1"],
            assume_yes=True,
            stdin=StringIO(),
            stdout=StringIO(),
        )
        assert (stats.imported, stats.gated, stats.errors) == (0, 1, 0)

    def test_declined_confirmation_aborts(
        self, manager: MemoryManager, mesh: MeshFetchDouble
    ) -> None:
        self._prepare(manager, mesh)
        out = StringIO()
        stats = run_fetch(
            manager.sqlite,
            manager,
            manager.settings,
            ["fed:agent-x:1"],
            assume_yes=False,
            stdin=_yes_stdin("n\n"),
            stdout=out,
            is_tty=_always_tty,
        )
        assert stats.aborted is True
        assert stats.fetched == 0
        assert mesh.reload().get("calls", []) == []  # the CLI leg never ran
        assert manager.sqlite.find_federated_duplicate(fed_id="fed:agent-x:1") is None

    def test_non_tty_without_yes_aborts_with_hint(
        self, manager: MemoryManager, mesh: MeshFetchDouble
    ) -> None:
        self._prepare(manager, mesh)
        out = StringIO()
        stats = run_fetch(
            manager.sqlite,
            manager,
            manager.settings,
            ["fed:agent-x:1"],
            assume_yes=False,
            stdin=StringIO(),
            stdout=out,
            is_tty=_never_tty,
        )
        assert stats.aborted is True
        assert "--yes" in out.getvalue()

    def test_plan_rendered_before_confirmation(
        self, manager: MemoryManager, mesh: MeshFetchDouble
    ) -> None:
        self._prepare(manager, mesh)
        out = StringIO()
        run_fetch(
            manager.sqlite,
            manager,
            manager.settings,
            ["fed:agent-x:1"],
            assume_yes=False,
            stdin=_yes_stdin("y\n"),
            stdout=out,
            is_tty=_always_tty,
        )
        text = out.getvalue()
        assert "Fetch plan:" in text
        assert "fed:agent-x:1" in text
        assert f"origin={_PEER}" in text
        assert "Proceed with fetch? [y/N]:" in text

    def test_all_skipped_never_prompts(self, manager: MemoryManager, mesh: MeshFetchDouble) -> None:
        manager.add(
            MemoryCreate(
                content="Local body.",
                title="Local decision",
                tags=[f"project:{_PROJECT}", f"agent:{_AGENT}", "mnemos:decision"],
                source=MemorySource.MCP,
                metadata={"fed_id": "fed:agent-x:3"},
            ),
            project=_PROJECT,
            agent=_AGENT,
        )
        _seed_index(manager.sqlite, _entry("fed:agent-x:3", origin_peer="self"))
        out = StringIO()
        stats = run_fetch(
            manager.sqlite,
            manager,
            manager.settings,
            ["fed:agent-x:3"],
            assume_yes=False,
            stdin=StringIO(),
            stdout=out,
            is_tty=_never_tty,
        )
        assert stats.aborted is False
        assert stats.skipped == 1
        assert "Proceed" not in out.getvalue()


# ── In-process import path reuse (servicer, no gRPC) ─────────────────────────


class TestImportPathReuse:
    def test_servicer_import_matches_writememory_semantics(self, manager: MemoryManager) -> None:
        servicer = VesmaCoreServicer(manager, settings=manager.settings)
        result = servicer.import_compact_record(_record("fed:agent-x:imp"), peer_id=_PEER)
        assert result.status.value == "written"
        assert result.written_id
        replay = servicer.import_compact_record(_record("fed:agent-x:imp"), peer_id=_PEER)
        assert replay.status.value == "duplicate"
        assert replay.written_id == result.written_id
        refused = servicer.import_compact_record(
            _record("fed:agent-x:sec", project="not-allowed"), peer_id=_PEER
        )
        assert refused.status.value == "refused_acl"
        assert "not allowed" in refused.reason


# ── CLI ───────────────────────────────────────────────────────────────────────


def _cli_config(tmp_path: Path, mesh: MeshFetchDouble, *, with_fetch: bool = True) -> Path:
    cfg: dict[str, Any] = {
        "mnemos": {
            "data_dir": str(tmp_path / "data"),
            "vault_path": str(tmp_path / "vault"),
            "db_name": "lazy-fetch-cli.db",
        },
        "embedding": {"provider": "nano"},
        "scanner": {"enabled": False},
        "federation": {
            "shared_projects": [_PROJECT],
            "peers": {_PEER: {"bearer_token_env": _TOKEN_ENV, "allowed_projects": [_PROJECT]}},
            "fetch": {
                "mesh_bin": str(mesh.bin_path),
                "mesh_config_path": str(tmp_path / "mesh.yaml"),
            },
        },
    }
    if not with_fetch:
        del cfg["federation"]["fetch"]
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(cfg))
    return path


class TestFetchCLI:
    @pytest.fixture(autouse=True)
    def _reset(self) -> Generator[None, None, None]:
        root = logging.getLogger()
        saved_handlers = list(root.handlers)
        saved_level = root.level
        reset_manager()
        yield
        reset_manager()
        root.handlers = saved_handlers
        root.setLevel(saved_level)

    @pytest.fixture(autouse=True)
    def _index_seed(self, tmp_path: Path, mesh: MeshFetchDouble) -> Generator[None, None, None]:
        """Pre-seed the CLI DB's federation_index before the manager opens it."""
        db_path = tmp_path / "data" / "lazy-fetch-cli.db"
        db_path.parent.mkdir(parents=True, exist_ok=True)
        store = SQLiteStore(db_path)
        try:
            _seed_index(store, _entry("fed:agent-x:1"))
        finally:
            store.close()
        yield

    def test_happy_path(self, tmp_path: Path, mesh: MeshFetchDouble) -> None:
        mesh.set({"records": [_record("fed:agent-x:1").model_dump()], "not_found": []})
        cfg = _cli_config(tmp_path, mesh)
        result = runner.invoke(
            app, ["fetch", "--id", "fed:agent-x:1", "--yes", "--config", str(cfg)]
        )
        assert result.exit_code == 0, result.output
        assert "fetched=1 imported=1" in result.output
        assert "errors=0" in result.output
        store = SQLiteStore(tmp_path / "data" / "lazy-fetch-cli.db")
        try:
            assert store.find_federated_duplicate(fed_id="fed:agent-x:1") is not None
        finally:
            store.close()

    def test_non_tty_without_yes_exits_1(self, tmp_path: Path, mesh: MeshFetchDouble) -> None:
        mesh.set({"records": [_record("fed:agent-x:1").model_dump()], "not_found": []})
        cfg = _cli_config(tmp_path, mesh)
        result = runner.invoke(
            app, ["fetch", "--id", "fed:agent-x:1", "--config", str(cfg)], input=""
        )
        assert result.exit_code == 1
        assert "not interactive" in result.output
        # Nothing was fetched or written.
        store = SQLiteStore(tmp_path / "data" / "lazy-fetch-cli.db")
        try:
            assert store.find_federated_duplicate(fed_id="fed:agent-x:1") is None
        finally:
            store.close()

    def test_no_ids_exits_1(self, tmp_path: Path, mesh: MeshFetchDouble) -> None:
        cfg = _cli_config(tmp_path, mesh)
        result = runner.invoke(app, ["fetch", "--config", str(cfg)])
        assert result.exit_code == 1
        assert "no --id" in result.output

    def test_missing_mesh_config_exits_1(self, tmp_path: Path, mesh: MeshFetchDouble) -> None:
        cfg = _cli_config(tmp_path, mesh, with_fetch=False)
        result = runner.invoke(
            app, ["fetch", "--id", "fed:agent-x:1", "--yes", "--config", str(cfg)]
        )
        assert result.exit_code == 1
        assert "mesh_config_path" in result.output

    def test_unknown_origin_peer_exits_1(self, tmp_path: Path, mesh: MeshFetchDouble) -> None:
        db_path = tmp_path / "data" / "lazy-fetch-cli.db"
        store = SQLiteStore(db_path)
        try:
            _seed_index(store, _entry("fed:agent-x:ghost", origin_peer="ghost-peer"))
        finally:
            store.close()
        cfg = _cli_config(tmp_path, mesh)
        result = runner.invoke(
            app, ["fetch", "--id", "fed:agent-x:ghost", "--yes", "--config", str(cfg)]
        )
        assert result.exit_code == 1
        assert "ghost-peer" in result.output and "peer-a" in result.output

    def test_unknown_id_exits_1(self, tmp_path: Path, mesh: MeshFetchDouble) -> None:
        cfg = _cli_config(tmp_path, mesh)
        result = runner.invoke(
            app, ["fetch", "--id", "fed:agent-x:nope", "--yes", "--config", str(cfg)]
        )
        assert result.exit_code == 1
        assert "federation_index" in result.output

    def test_failing_mesh_cli_exits_1(self, tmp_path: Path, mesh: MeshFetchDouble) -> None:
        mesh.set({"fail": "mesh leg down"})
        cfg = _cli_config(tmp_path, mesh)
        result = runner.invoke(
            app, ["fetch", "--id", "fed:agent-x:1", "--yes", "--config", str(cfg)]
        )
        assert result.exit_code == 1
        assert "errors=1" in result.output

    def test_already_local_exits_0(self, tmp_path: Path, mesh: MeshFetchDouble) -> None:
        # A self-origin index row whose body IS in the local corpus:
        # seed the self row FIRST (INSERT — no origin conflict), then
        # import the record into memories through the real path, then
        # run the CLI against the same DB — it must report a clean skip
        # and never spawn the mesh CLI.
        settings = _settings(tmp_path, mesh, db_name="lazy-fetch-cli.db")
        store = SQLiteStore(settings.db_path)
        try:
            _seed_index(store, _entry("fed:agent-x:loc", origin_peer="self"))
        finally:
            store.close()
        mgr = MemoryManager(settings)
        mock_embedder = MagicMock()
        mock_embedder.embed.return_value = [0.1] * 384
        mgr._embedder = mock_embedder
        try:
            servicer = VesmaCoreServicer(mgr, settings=settings)
            servicer.import_compact_record(_record("fed:agent-x:loc"), peer_id=_PEER)
        finally:
            mgr.close()
        cfg = _cli_config(tmp_path, mesh)
        result = runner.invoke(app, ["fetch", "--id", "fed:agent-x:loc", "--config", str(cfg)])
        assert result.exit_code == 0, result.output
        assert "nothing to fetch" in result.output
        assert mesh.reload().get("calls", []) == []

    def test_all_not_found_reports_summary_not_skip(
        self, tmp_path: Path, mesh: MeshFetchDouble
    ) -> None:
        """Review F1 regression: when the peer returns not_found for EVERY
        planned id, the CLI must show the summary (not_found=2) — not the
        green 'nothing to fetch' skip message, which would hide a lost
        record from the operator."""
        store = SQLiteStore(tmp_path / "data" / "lazy-fetch-cli.db")
        try:
            _seed_index(
                store,
                _entry("fed:agent-x:gone1", origin_peer="peer-a"),
                _entry("fed:agent-x:gone2", origin_peer="peer-a"),
            )
        finally:
            store.close()
        mesh.set({"records": [], "not_found": ["fed:agent-x:gone1", "fed:agent-x:gone2"]})
        cfg = _cli_config(tmp_path, mesh)
        result = runner.invoke(
            app,
            [
                "fetch",
                "--id",
                "fed:agent-x:gone1",
                "--id",
                "fed:agent-x:gone2",
                "--config",
                str(cfg),
                "--yes",
            ],
        )
        assert result.exit_code == 0, result.output
        assert "nothing to fetch" not in result.output
        assert "not_found=2" in result.output
