"""Tests for /api/v1/export and /api/v1/import endpoints (M17)."""

from __future__ import annotations

import gzip
import io
import json
import tempfile
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from vesma.api import main as api_main
from vesma.api.main import app, lifespan
from vesma.config import Settings
from vesma.manager import MemoryManager
from vesma.models import MemoryCreate, MemorySource


@pytest.fixture
def tmp_settings():
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp = Path(tmpdir)
        settings = Settings(
            vesma={
                "vault_path": str(tmp / "vault"),
                "data_dir": str(tmp / "data"),
                "db_name": "test-api-export.db",
                "auto_filter": False,
            },
            embedding={"provider": "onnx"},
            # Scanner is exercised in test_scanner.py — disable it here so
            # the API lifespan does not spawn a daemon thread per test
            # (defence-in-depth against the singleton thread leak).
            scanner={"enabled": False},
        )
        settings.resolve_paths()
        yield settings


@pytest.fixture
def client(tmp_settings):
    mgr = MemoryManager(tmp_settings)
    mock_embedder = MagicMock()
    mock_embedder.embed.return_value = [0.1] * 384
    mgr._embedder = mock_embedder

    test_app = FastAPI(title="Mnemos-Test", version="0.1.0", lifespan=lifespan)
    for route in app.routes:
        test_app.routes.append(route)
    api_main._manager = mgr
    with TestClient(test_app) as tc:
        yield tc
    mgr.close()
    api_main._manager = None


def _add(client, content: str = "test") -> dict:
    resp = client.post(
        "/memories",
        json={
            "content": content,
            "tags": ["project:mnemos", "agent:reviewer", "mnemos:learning"],
        },
    )
    assert resp.status_code == 201
    return resp.json()


class TestApiExport:
    def test_export_json_returns_download(self, client):
        _add(client, "export me")
        resp = client.post("/api/v1/export", json={"format": "json"})
        assert resp.status_code == 200
        assert "attachment" in resp.headers.get("content-disposition", "")
        payload = json.loads(resp.content)
        assert payload["format_version"] == "1.0"
        assert len(payload["memories"]) == 1

    def test_export_json_with_filter(self, client):
        _add(client, "a")
        # Add a second memory with a different project via direct manager access.
        mgr = api_main._manager
        mgr.add(
            MemoryCreate(
                content="b",
                tags=["project:other", "agent:x", "mnemos:learning"],
                source=MemorySource.CLI,
            ),
            project="other",
            agent="x",
        )
        resp = client.post("/api/v1/export", json={"format": "json", "project": "mnemos"})
        assert resp.status_code == 200
        payload = json.loads(resp.content)
        assert all(m["project"] == "mnemos" for m in payload["memories"])

    def test_export_gzip_compression(self, client):
        _add(client, "compress me")
        resp = client.post("/api/v1/export", json={"format": "json", "compress": "gzip"})
        assert resp.status_code == 200
        raw = gzip.decompress(resp.content)
        payload = json.loads(raw)
        assert len(payload["memories"]) == 1

    def test_export_sqlite_returns_tar_gz(self, client):
        _add(client, "snapshot")
        resp = client.post("/api/v1/export", json={"format": "sqlite"})
        assert resp.status_code == 200
        # The body is a gzip tar archive.
        import tarfile

        with tarfile.open(fileobj=io.BytesIO(resp.content), mode="r:gz") as tar:
            assert "mnemos.db" in tar.getnames()

    def test_export_encrypt_requires_passphrase_header(self, client):
        _add(client, "secret")
        resp = client.post("/api/v1/export", json={"format": "json", "encrypt": True})
        assert resp.status_code == 400
        assert "passphrase" in resp.json()["detail"].lower()

    def test_export_encrypt_with_passphrase_header(self, client):
        _add(client, "secret")
        resp = client.post(
            "/api/v1/export",
            json={"format": "json", "encrypt": True},
            headers={"X-Mnemos-Passphrase": "test-pw"},
        )
        assert resp.status_code == 200
        assert resp.content.startswith(b"MNEMOS1")

    def test_export_invalid_format(self, client):
        resp = client.post("/api/v1/export", json={"format": "xml"})
        assert resp.status_code == 400


class TestApiImport:
    def test_import_merge_inserts(self, client):
        # Export from the current DB.
        _add(client, "x")
        resp = client.post("/api/v1/export", json={"format": "json"})
        export_bytes = resp.content

        # Wipe the DB via a fresh manager, then import.
        mgr = api_main._manager
        mgr.sqlite.wipe_all()
        mgr.vectors.wipe()
        assert mgr.sqlite.count() == 0

        import_resp = client.post(
            "/api/v1/import",
            files={"file": ("backup.json", export_bytes, "application/json")},
            params={"mode": "merge"},
        )
        assert import_resp.status_code == 200
        data = import_resp.json()
        assert data["imported"] == 1
        assert data["errors"] == []

    def test_import_restore_requires_confirm(self, client):
        _add(client, "x")
        resp = client.post("/api/v1/export", json={"format": "json"})
        export_bytes = resp.content

        import_resp = client.post(
            "/api/v1/import",
            files={"file": ("backup.json", export_bytes, "application/json")},
            params={"mode": "restore", "confirm": "false"},
        )
        # Restore without confirm → errors in the summary, status 200.
        assert import_resp.status_code == 200
        data = import_resp.json()
        assert data["errors"]
        assert any("confirm" in e for e in data["errors"])

    def test_import_dry_run(self, client):
        _add(client, "x")
        resp = client.post("/api/v1/export", json={"format": "json"})
        export_bytes = resp.content

        mgr = api_main._manager
        mgr.sqlite.wipe_all()
        before = mgr.sqlite.count()

        import_resp = client.post(
            "/api/v1/import",
            files={"file": ("backup.json", export_bytes, "application/json")},
            params={"mode": "merge", "dry_run": "true"},
        )
        assert import_resp.status_code == 200
        data = import_resp.json()
        assert data["dry_run"] is True
        assert data["imported"] == 1
        assert mgr.sqlite.count() == before  # nothing written

    def test_import_no_file_returns_400(self, client):
        resp = client.post("/api/v1/import", params={"mode": "merge"})
        # FastAPI returns 422 when a required File field is missing.
        assert resp.status_code in (400, 422)
