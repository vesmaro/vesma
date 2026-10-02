"""Unit tests for the native mesh serve wiring (W2, ROADMAP-v2 go-live).

``vesmaro serve`` must, when ``mesh.enabled`` is true:

* start the :class:`vesmaro.mesh_server.MeshServer` on the configured
  Unix socket (creating the parent dir) BEFORE uvicorn runs,
* seed the HTTP-app manager singleton from serve's own ``--config``
  (same manager for MeshServer and the in-process app),
* log exactly one startup line: ``mesh server listening on <path>``,
* stop the MeshServer and remove its socket when uvicorn returns —
  including when uvicorn raises (startup failure).

With ``mesh.enabled`` false (the default, including explicit legacy
``mesh:`` sections) the command must behave exactly as before: uvicorn
only, no MeshServer construction.

The gRPC layer is real (tmp Unix socket); only the manager singleton
and ``uvicorn.run`` are mocked — same mock boundary as
``tests/test_mesh_server.py``.
"""

from __future__ import annotations

import logging
import os
import stat
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
import yaml

from vesmaro.cli.main import serve

#: Bind used for every test — pinned so uvicorn.run assertions are exact.
_HOST = "127.0.0.1"
_PORT = 18787


class _ListHandler(logging.Handler):
    """Collect formatted messages (survives setup_logging's handler reset)."""

    def __init__(self) -> None:
        super().__init__()
        self.messages: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.messages.append(record.getMessage())


def _write_config(tmp_path: Path, mesh_section: dict[str, Any] | None) -> Path:
    """Write a minimal serve config with isolated storage paths."""
    cfg: dict[str, Any] = {
        "mnemos": {
            "data_dir": str(tmp_path / "data"),
            "vault_path": str(tmp_path / "vault"),
        },
    }
    if mesh_section is not None:
        cfg["mesh"] = mesh_section
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(cfg))
    return path


@pytest.fixture(autouse=True)
def _serve_env() -> Any:
    """Snapshot/restore the env vars serve() exports and root logging.

    serve() calls setup_logging() which clears root handlers — restore
    them so these tests do not break caplog in unrelated tests.
    """
    # serve() exports BOTH spellings (canonical VESMA_API__* since 5.3,
    # deprecated VESMARO_API__*): snapshot both or the canonical leak
    # outranks a later test's VESMARO_API__HOST under the dual-prefix
    # precedence and flips its expected bind host.
    keys = (
        "VESMA_API__HOST",
        "VESMA_API__PORT",
        "VESMARO_API__HOST",
        "VESMARO_API__PORT",
    )
    saved_env = {k: os.environ.get(k) for k in keys}
    root = logging.getLogger()
    saved_handlers = list(root.handlers)
    saved_level = root.level
    yield
    for k, v in saved_env.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v
    root.handlers = saved_handlers
    root.setLevel(saved_level)


class TestServeMeshDisabled:
    def test_mesh_disabled_starts_uvicorn_only(self, tmp_path: Path) -> None:
        """Explicit legacy ``mesh:`` section with enabled:false → no MeshServer."""
        cfg = _write_config(tmp_path, {"enabled": False})

        with (
            patch("uvicorn.run") as run,
            patch("vesmaro.mesh_server.MeshServer") as mesh_cls,
            patch("vesmaro.api.main.get_manager") as get_mgr,
        ):
            serve(host=_HOST, port=_PORT, log_file=None, config=str(cfg))

        mesh_cls.assert_not_called()
        get_mgr.assert_not_called()
        run.assert_called_once_with(
            "vesmaro.api.main:app",
            host=_HOST,
            port=_PORT,
            workers=1,
        )


class TestServeMeshEnabled:
    def test_starts_mesh_server_and_drains_on_return(self, tmp_path: Path) -> None:
        """enabled → real socket bound (0660 with group access), one log line,
        manager seeded from serve's --config, clean shutdown."""
        socket_path = str(tmp_path / "run" / "mnemos" / "core.sock")
        cfg = _write_config(
            tmp_path,
            {"enabled": True, "socket_path": socket_path, "socket_group_access": True},
        )
        mesh_logger = logging.getLogger("vesmaro.mesh_server")
        handler = _ListHandler()
        mesh_logger.setLevel(logging.INFO)
        mesh_logger.addHandler(handler)

        try:
            with (
                patch("uvicorn.run") as run,
                patch("vesmaro.api.main.get_manager") as get_mgr,
            ):
                get_mgr.return_value = MagicMock(name="manager")
                serve(host=_HOST, port=_PORT, log_file=None, config=str(cfg))
        finally:
            mesh_logger.removeHandler(handler)

        # Manager seeded from serve's own config path (same singleton the
        # in-process HTTP app would use).
        get_mgr.assert_called_once_with(str(cfg))
        # uvicorn still ran with the exact pre-wiring arguments.
        run.assert_called_once_with(
            "vesmaro.api.main:app",
            host=_HOST,
            port=_PORT,
            workers=1,
        )
        # Exactly one startup line, exact phrase, while the server lived —
        # and the socket drained (removed) after serve returned.
        listening = [m for m in handler.messages if "listening" in m]
        assert listening == [f"mesh server listening on {socket_path}"]
        assert not Path(socket_path).exists()

    def test_socket_created_with_group_access_modes(self, tmp_path: Path) -> None:
        """Boundary: socket path in a non-existent nested dir is created
        with 0660 socket / 0770 dir when socket_group_access is set."""
        socket_path = str(tmp_path / "deep" / "nest" / "core.sock")
        cfg = _write_config(
            tmp_path,
            {"enabled": True, "socket_path": socket_path, "socket_group_access": True},
        )
        seen: dict[str, int] = {}

        def _capture_run(*_args: Any, **_kwargs: Any) -> None:
            # Inside uvicorn.run: server is up, socket must exist with perms.
            st = Path(socket_path).stat()
            seen["socket"] = stat.S_IMODE(st.st_mode)
            seen["dir"] = stat.S_IMODE(Path(socket_path).parent.stat().st_mode)

        with (
            patch("uvicorn.run", side_effect=_capture_run),
            patch("vesmaro.api.main.get_manager") as get_mgr,
        ):
            get_mgr.return_value = MagicMock(name="manager")
            serve(host=_HOST, port=_PORT, log_file=None, config=str(cfg))

        assert not Path(socket_path).is_socket()  # removed after shutdown
        assert seen == {"socket": 0o660, "dir": 0o770}

    def test_default_socket_modes_are_owner_only(self, tmp_path: Path) -> None:
        """Without socket_group_access the wiring keeps 0600/0700."""
        socket_path = str(tmp_path / "run2" / "core.sock")
        cfg = _write_config(tmp_path, {"enabled": True, "socket_path": socket_path})
        seen: dict[str, int] = {}

        def _capture_run(*_args: Any, **_kwargs: Any) -> None:
            seen["socket"] = stat.S_IMODE(Path(socket_path).stat().st_mode)
            seen["dir"] = stat.S_IMODE(Path(socket_path).parent.stat().st_mode)

        with (
            patch("uvicorn.run", side_effect=_capture_run),
            patch("vesmaro.api.main.get_manager") as get_mgr,
        ):
            get_mgr.return_value = MagicMock(name="manager")
            serve(host=_HOST, port=_PORT, log_file=None, config=str(cfg))

        assert seen == {"socket": 0o600, "dir": 0o700}
        assert not Path(socket_path).exists()

    def test_uvicorn_failure_still_stops_mesh(self, tmp_path: Path) -> None:
        """Failure mode: uvicorn.run raising must not leak a live socket."""
        socket_path = str(tmp_path / "run3" / "core.sock")
        cfg = _write_config(tmp_path, {"enabled": True, "socket_path": socket_path})

        with (
            patch("uvicorn.run", side_effect=RuntimeError("bind failed")),
            patch("vesmaro.api.main.get_manager") as get_mgr,
        ):
            get_mgr.return_value = MagicMock(name="manager")
            with pytest.raises(RuntimeError, match="bind failed"):
                serve(host=_HOST, port=_PORT, log_file=None, config=str(cfg))

        assert not Path(socket_path).exists()
