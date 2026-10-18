"""Mesh socket platform default + unwritable-parent refusal (portability F4).

Linux keeps the historical ``/run/mnemos/core.sock`` default byte-for-byte
(prod behavior unchanged). Every other unix falls back into the user-owned
``~/.vesma/run/`` — macOS/BSD have no ``/run/mnemos``, so the old default
guaranteed a PermissionError on the very first bind. When an operator
configures a genuinely unwritable parent, :meth:`MeshServer.start` must
refuse with :class:`MeshSocketPathError` naming the ``mesh.socket_path``
override — never a raw traceback.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

from vesma.config import MeshConfig, Settings, _default_mesh_socket_path
from vesma.mesh_server import MeshServer, MeshSocketPathError


class TestPlatformDefault:
    def test_linux_default_is_unchanged(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Linux keeps ``/run/mnemos/core.sock`` — prod behavior identical."""
        monkeypatch.setattr(sys, "platform", "linux")
        assert _default_mesh_socket_path() == "/run/mnemos/core.sock"
        assert MeshConfig().socket_path == "/run/mnemos/core.sock"

    @pytest.mark.parametrize("platform", ["darwin", "freebsd", "openbsd"])
    def test_non_linux_default_falls_into_user_home(
        self, monkeypatch: pytest.MonkeyPatch, platform: str
    ) -> None:
        """Non-Linux unix binds under ``~/.vesma/run/`` instead of ``/run/mnemos``."""
        monkeypatch.setattr(sys, "platform", platform)
        expected = str(Path.home() / ".vesma" / "run" / "core.sock")
        assert _default_mesh_socket_path() == expected
        assert MeshConfig().socket_path == expected


class TestUnwritableParentRefusal:
    @pytest.mark.skipif(
        hasattr(os, "geteuid") and os.geteuid() == 0,
        reason="root ignores directory permission bits",
    )
    def test_start_refuses_with_override_hint(self, tmp_path: Path) -> None:
        """An unwritable parent yields MeshSocketPathError naming the fix."""
        ro = tmp_path / "readonly"
        ro.mkdir()
        ro.chmod(0o555)
        sock = ro / "sub" / "core.sock"  # mkdir must CREATE 'sub' → EACCES
        settings = Settings(mesh=MeshConfig(enabled=True, socket_path=str(sock)))
        server = MeshServer(str(sock), manager=None, settings=settings)  # type: ignore[arg-type]
        try:
            with pytest.raises(MeshSocketPathError) as excinfo:
                server.start()
        finally:
            ro.chmod(0o700)
        message = str(excinfo.value)
        assert "mesh.socket_path" in message, message
        assert "override" in message, message
