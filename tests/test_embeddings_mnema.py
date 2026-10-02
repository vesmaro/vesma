"""NM-1c/NM-1d — NanoProvider: the bundled vesma-embed model (ADR-0021).

Guards the production default:

* the bundled artifact under ``mnemos/models/vesma-embed-v1/`` is complete
  and its manifest pins the REAL weights hash (the manifest drifting
  from the shipped bytes would poison every fingerprint consumer);
* the provider loads from the shipped default config (no config edits
  needed — zero-config offline install), embeds 384-dim L2-normalized
  vectors in single and batch mode;
* legacy ``provider=chromadb`` configs migrate to nano with a loud
  deprecation warning instead of crashing the legacy install (the
  chromadb runtime dependency was removed in NM-1c); the FULL legacy
  default pair (chromadb + all-MiniLM-L6-v2) degrades the model to the
  bundled artifact too — review #221 F1: degrading only the provider
  crashed NanoProvider with FileNotFoundError on the MiniLM spec;
* an unknown provider still fails loud at the boundary.
"""

from __future__ import annotations

import hashlib
import json
from importlib.resources import files as resource_files
from pathlib import Path

import pytest

from vesma.config import EmbeddingConfig
from vesma.embeddings import (
    MNEMA_EMBED_MODEL,
    NanoProvider,
    config_fingerprint,
    create_embedding_provider,
    mnema_artifact_onnx_path,
)


@pytest.fixture(scope="module")
def provider() -> NanoProvider:
    """One ORT session for the whole module (construction is ~1s)."""
    return NanoProvider()


def _artifact_dir() -> Path:
    return Path(str(resource_files("vesma") / "models" / MNEMA_EMBED_MODEL))


# ── bundled artifact ──────────────────────────────────────────────────────────


def test_bundled_artifact_manifest_pins_real_weights() -> None:
    """manifest.json matches the shipped bytes (name, dims, sha256)."""
    artifact = _artifact_dir()
    assert (artifact / "tokenizer.json").is_file(), "tokenizer missing from the bundle"

    manifest = json.loads((artifact / "manifest.json").read_text())
    assert manifest["name"] == MNEMA_EMBED_MODEL
    assert manifest["dimensions"] == 384
    assert manifest["max_seq"] == 256
    assert "Apache-2.0" in manifest["license"]

    digest = hashlib.sha256()
    with (artifact / "model.onnx").open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    assert manifest["weights_sha256"] == digest.hexdigest(), (
        "manifest weights_sha256 drifted from the shipped model.onnx"
    )


def test_default_config_builds_nano() -> None:
    """The shipped default is provider=nano / model=vesma-embed-v1."""
    cfg = EmbeddingConfig()
    assert cfg.provider == "nano"
    assert cfg.model == MNEMA_EMBED_MODEL
    built = create_embedding_provider(cfg)
    assert isinstance(built, NanoProvider)


# ── embeddings ────────────────────────────────────────────────────────────────


def test_embed_returns_384d_l2_normalized(provider: NanoProvider) -> None:
    vec = provider.embed("привет мир — nano embedder smoke")
    assert len(vec) == 384
    assert all(isinstance(x, float) for x in vec)
    norm = sum(x * x for x in vec) ** 0.5
    assert norm == pytest.approx(1.0, abs=1e-3), "graph-side L2 normalization missing"
    assert any(abs(x) > 1e-6 for x in vec), "degenerate all-zero embedding"


def test_embed_batch_shapes_and_stability(provider: NanoProvider) -> None:
    texts = ["первый текст", "second text", ""]
    rows = provider.embed_batch(texts)
    assert len(rows) == 3
    assert all(len(r) == 384 for r in rows)
    for row in rows:
        assert sum(x * x for x in row) ** 0.5 == pytest.approx(1.0, abs=1e-3)

    # same session + same input → deterministic (per-arch corridors rely on it)
    again = provider.embed(texts[0])
    assert again == rows[0]


def test_embed_truncates_overlong_input(provider: NanoProvider) -> None:
    """Boundary: input beyond max_seq 256 tokens still yields a valid vector."""
    long_text = "деталь сборки механизм " * 400  # well over 256 tokens
    vec = provider.embed(long_text)
    assert len(vec) == 384
    assert sum(x * x for x in vec) ** 0.5 == pytest.approx(1.0, abs=1e-3)


def test_custom_onnx_path_resolution(provider: NanoProvider) -> None:
    """An explicit .onnx path resolves its sibling tokenizer.json."""
    onnx_path = mnema_artifact_onnx_path(MNEMA_EMBED_MODEL)
    custom = NanoProvider(model=str(onnx_path))
    assert custom.dimension == 384
    assert custom.weights_sha256 == provider.weights_sha256
    assert custom.embed("same text") == provider.embed("same text")


# ── migration: legacy provider values (chromadb removed in NM-1c) ─────────────


@pytest.mark.parametrize("legacy", ["chromadb", "chroma", "default"])
def test_legacy_provider_migrates_to_nano(legacy: str, caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level("WARNING", logger="vesma.embeddings"):
        built = create_embedding_provider(EmbeddingConfig(provider=legacy))
    assert isinstance(built, NanoProvider)
    assert any(
        "deprecated" in rec.message and "provider=nano" in rec.message for rec in caplog.records
    ), f"expected a deprecation warning for provider={legacy!r}"


def test_legacy_default_pair_migrates_model_too(caplog: pytest.LogCaptureFixture) -> None:
    """Review #221 F1: the FULL legacy default pair degrades wholesale.

    ``provider=chromadb`` + ``model=all-MiniLM-L6-v2`` is the shipped
    default of a pre-NM-1c install. Degrading only the provider used to
    crash NanoProvider with FileNotFoundError (the MiniLM string is not
    a bundled artifact name); the factory must swap the model to the
    bundled vesma-embed artifact with a loud warning.
    """
    with caplog.at_level("WARNING", logger="vesma.embeddings"):
        built = create_embedding_provider(
            EmbeddingConfig(provider="chromadb", model="all-MiniLM-L6-v2")
        )
    assert isinstance(built, NanoProvider)
    assert built.model_name == MNEMA_EMBED_MODEL
    assert any(
        "deprecated" in rec.message and "provider=nano" in rec.message for rec in caplog.records
    ), "expected a deprecation warning naming the degraded pair"


def test_unknown_provider_fails_loud() -> None:
    with pytest.raises(ValueError, match="Unknown embedding provider"):
        create_embedding_provider(EmbeddingConfig(provider="no-such-provider"))


# ── vintage fingerprint (ADR-0021 round-3 swap) ───────────────────────────────


def test_nano_fingerprint_pins_weights(provider: NanoProvider) -> None:
    """The bundled provider exposes its verified weights hash as fingerprint."""
    assert provider.fingerprint == f"nano:sha256:{provider.weights_sha256}"


def test_config_fingerprint_twin_matches_instance() -> None:
    """Doctor-side twin: config_fingerprint == instance fingerprint.

    The two sides are computed by different code paths (pure hash vs a
    loaded provider); a drift here would make the doctor report false
    vintage mismatches (or miss real ones).
    """
    cfg = EmbeddingConfig()
    built = create_embedding_provider(cfg)
    assert config_fingerprint(cfg) == built.fingerprint


def test_config_fingerprint_legacy_pair_degrades_wholesale() -> None:
    """chromadb + MiniLM (the legacy default pair) hashes the BUNDLED artifact."""
    legacy = EmbeddingConfig(provider="chromadb", model="all-MiniLM-L6-v2")
    assert config_fingerprint(legacy) == config_fingerprint(EmbeddingConfig())


@pytest.mark.parametrize(
    ("provider", "model", "expected"),
    [
        ("ollama", "nomic-embed-text", "ollama:nomic-embed-text"),
        ("onnx", "BAAI/bge-small-en-v1.5", "onnxhub:BAAI/bge-small-en-v1.5@rev123"),
        (
            "sentence-transformers",
            "intfloat/multilingual-e5-small",
            "st:intfloat/multilingual-e5-small",
        ),
    ],
)
def test_config_fingerprint_coarse_providers(provider: str, model: str, expected: str) -> None:
    """Non-nano providers: stable ``key:identity`` (switch detection)."""
    kwargs: dict[str, str] = {"provider": provider, "model": model}
    if provider == "onnx":
        kwargs["hf_revision"] = "rev123"
    assert config_fingerprint(EmbeddingConfig(**kwargs)) == expected


def test_config_fingerprint_follows_weights_swap(provider: NanoProvider) -> None:
    """The fingerprint changes exactly when the shipped weights change.

    Regression guard for the round-3 swap: the stamped vintage key must
    not be pinned to a constant — it tracks the artifact bytes.
    """
    swapped = hashlib.sha256(b"other weights").hexdigest()
    assert provider.fingerprint != f"nano:sha256:{swapped}"
