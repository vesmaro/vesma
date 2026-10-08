"""Embeddings layer for Vesma.

Uses local ONNX models by default (privacy + offline).

Providers:
  - NanoProvider            — the bundled vesma-embed model (default,
                              ADR-0021 NM-1: 384d, int8 ONNX, ships in the
                              wheel — zero downloads, zero network)
  - ONNXHubProvider         — any HuggingFace ONNX model
  - OllamaProvider          — local Ollama embeddings
  - SentenceTransformerProvider — via sentence-transformers (optional dep)
"""

from __future__ import annotations

import hashlib
import logging
import os
from abc import ABC, abstractmethod
from importlib.resources import files as resource_files
from pathlib import Path
from typing import Any, cast

import numpy as np

from vesma.config import EmbeddingConfig

logger = logging.getLogger(__name__)


# ── Abstract base ─────────────────────────────────────────────────────────────


class EmbeddingProvider(ABC):
    @abstractmethod
    def embed(self, text: str) -> list[float]: ...

    @abstractmethod
    def embed_batch(self, texts: list[str]) -> list[list[float]]: ...

    @property
    @abstractmethod
    def dimension(self) -> int: ...

    @property
    def fingerprint(self) -> str:
        """Stable identity of the embedding geometry this provider produces.

        The vector "vintage" key: stamped into vector-store metadata on
        every upsert and compared by ``heal_stale_embeddings`` — after an
        embedder swap every pre-swap vector carries a different
        fingerprint and is re-embedded (ADR-0021: full re-embed on every
        embedder swap). Concrete (non-abstract): subclasses without a
        verifiable identity keep this default, which is stable per class
        but coarse — it detects a provider SWITCH, not a silent weights
        update. Subclasses with stronger keys override it
        (NanoProvider pins its ``weights_sha256``).
        """
        return f"{type(self).__name__}"


# ── vesma-embed: the bundled distilled embedder (ADR-0021 NM-1) ───────────────

#: Bundled artifact directory name (src/vesma/models/<name>/ inside the
#: wheel, reachable via importlib.resources). NOTE: ``vesma/models/`` is a
#: DATA directory, deliberately NOT a Python package — ``vesma.models``
#: remains the ``models.py`` module; a directory without ``__init__.py``
#: never shadows it at import time.
#: Renamed from ``mnema-embed-v1`` in the vesma rebrand: the weights are
#: byte-identical (the store pin is ``nano:sha256:<onnx-hash>`` — a rename
#: cannot invalidate stored pins), and the old-name directory ships until
#: 6.0 for the deprecated mnemos-memory-server mirror.
MNEMA_EMBED_MODEL = "vesma-embed-v1"

#: Pre-6.0 rebrand fallback: bundled name → legacy artifact directory.
#: If the new-name directory is missing (e.g. a deploy where only the
#: deprecated mirror shipped the old directory), resolve the legacy
#: directory instead and warn — the weights there are byte-identical.
_LEGACY_ARTIFACT_DIRS: dict[str, str] = {"vesma-embed-v1": "mnema-embed-v1"}

#: Static sequence shape of the exported graph (batch 1 x 256 tokens).
MNEMA_MAX_SEQ = 256

#: Dtype-agnostic ndarray alias for ORT graph edges — numpy stubs are import-
#: skipped (pyproject mypy overrides), so a bare ``np.ndarray`` trips
#: strict ``disallow_any_generics``; the alias keeps the NanoProvider
#: annotations clean without pretending dtype precision we cannot check.
_OrtTensor = np.ndarray[Any, Any]


def _mnema_artifact_dir(model: str) -> Path:
    """Resolve the artifact directory for a vesma-embed model spec.

    ``model`` is either (a) a filesystem path to a ``.onnx`` file — the
    tokenizer is then expected as ``tokenizer.json`` next to it — or
    (b) a bundled artifact name resolved under ``vesma/models/``.

    Raises FileNotFoundError (fail-loud at the boundary) when neither
    resolves; callers that prefer degradation own the try/except.
    """
    spec = (model or "").strip()
    if spec.endswith(".onnx"):
        onnx_path = Path(spec).expanduser().resolve()
        if onnx_path.is_file():
            return onnx_path.parent
        raise FileNotFoundError(
            f"vesma-embed model not found at {onnx_path!s}; pass a path to "
            f"an existing .onnx file or a bundled name (default: {MNEMA_EMBED_MODEL!r})"
        )
    name = spec or MNEMA_EMBED_MODEL
    bundled = resource_files("vesma") / "models" / name
    if (bundled / "model.onnx").is_file():
        # onnxruntime needs a real file path, so the Traversable is
        # stringified — the wheel/source layouts are real directories.
        return Path(str(bundled))
    # Rebrand fallback (pre-6.0): the vesma-* directory is missing but the
    # deprecated mnema-* twin (byte-identical weights) is still bundled —
    # resolve it so upgraded-but-mixed installs keep working.
    legacy_name = _LEGACY_ARTIFACT_DIRS.get(name)
    if legacy_name:
        legacy = resource_files("vesma") / "models" / legacy_name
        if (legacy / "model.onnx").is_file():
            logger.warning(
                "bundled artifact %r not found, falling back to the deprecated "
                "legacy directory %r (byte-identical weights; the legacy name "
                "is removed in 6.0)",
                name,
                legacy_name,
            )
            return Path(str(legacy))
    raise FileNotFoundError(
        f"vesma-embed artifact {name!r} is not bundled (looked at {bundled!s}); "
        f"expected model.onnx + tokenizer.json inside it"
    )


def mnema_artifact_onnx_path(model: str = "") -> Path:
    """Path to the resolved vesma-embed ``model.onnx`` (never checks existence).

    Shared with the S1m model-contour fingerprint so the provider and the
    gate hash the SAME file for ``weights_sha256``.
    """
    return _mnema_artifact_dir(model) / "model.onnx"


def mnema_weights_sha256(model: str = "") -> str:
    """SHA-256 over the resolved vesma-embed ``model.onnx`` bytes."""
    digest = hashlib.sha256()
    with mnema_artifact_onnx_path(model).open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


class NanoProvider(EmbeddingProvider):
    """The bundled vesma-embed model (ADR-0021 NM-1; provider key ``nano``).

    Loads the int8-quantized ONNX artifact shipped inside the package
    (``vesma/models/<name>/``): 384-dim, multilingual (RU+EN), L2-
    normalized. Mean-pooling and L2 normalization are part of the ONNX
    graph — the provider must NOT re-pool or re-normalize the output.

    The exported graph has a STATIC batch-1 x 256 shape, so batches are
    embedded text-by-text (same contract as the training-side
    ``OnnxEmbedder`` in ``training/eval_distilled.py``).
    """

    def __init__(self, model: str = "") -> None:
        import onnxruntime as ort
        from tokenizers import Tokenizer

        artifact_dir = _mnema_artifact_dir(model)
        self.model_name = (model or "").strip() or MNEMA_EMBED_MODEL
        self.weights_sha256 = mnema_weights_sha256(model)

        tokenizer_path = artifact_dir / "tokenizer.json"
        self._tokenizer = Tokenizer.from_file(str(tokenizer_path))
        # Static 1x256 graph: pad every input to exactly MAX_SEQ tokens.
        # The tokenizer's attention_mask is used AS IS — a manual mask from
        # token count would be all-ones and pollute the graph-side
        # mean-pooling with pad embeddings (review F1, PR #218).
        self._tokenizer.enable_truncation(max_length=MNEMA_MAX_SEQ)
        self._tokenizer.enable_padding(length=MNEMA_MAX_SEQ)

        n_threads = max(
            1,
            int(os.environ.get("VESMA_ORT_THREADS") or os.environ.get("OMP_NUM_THREADS") or "4"),
        )
        sess_opts = ort.SessionOptions()
        sess_opts.intra_op_num_threads = n_threads
        sess_opts.inter_op_num_threads = 1
        self._session = ort.InferenceSession(
            str(artifact_dir / "model.onnx"),
            sess_options=sess_opts,
            providers=["CPUExecutionProvider"],
        )
        self._model_inputs = {inp.name for inp in self._session.get_inputs()}
        test = self._infer(["test"])
        self._dim = int(test.shape[-1])
        logger.info(
            "vesma-embed ready: %s (dim=%d, sha256=%s…)",
            self.model_name,
            self._dim,
            self.weights_sha256[:12],
        )

    def _infer(self, texts: list[str]) -> _OrtTensor:
        """Embed text-by-text (static batch-1 graph). Output is already pooled+normalized."""
        rows: list[_OrtTensor] = []
        for text in texts:
            e = self._tokenizer.encode_batch([text])[0]
            ids = np.array([e.ids], dtype=np.int64)
            mask = np.array([e.attention_mask], dtype=np.int64)
            inputs: dict[str, _OrtTensor] = {"input_ids": ids, "attention_mask": mask}
            if "token_type_ids" in self._model_inputs:
                inputs["token_type_ids"] = np.zeros_like(ids)
            # `ort.InferenceSession.run` is untyped in the onnxruntime
            # stubs; output[0] is the (1, dim) embedding tensor.
            outputs = cast(list[_OrtTensor], self._session.run(None, inputs))
            rows.append(outputs[0][0])
        stacked: _OrtTensor = np.stack(rows)
        return stacked

    def embed(self, text: str) -> list[float]:
        return [float(x) for x in self._infer([text])[0]]

    def embed_batch(self, texts: list[str]) -> list[list[float]]:
        return [[float(x) for x in row] for row in self._infer(texts)]

    @property
    def dimension(self) -> int:
        return self._dim

    @property
    def fingerprint(self) -> str:
        # Strongest available key: the verified weights hash changes
        # exactly when the embedding geometry changes (round-3 swap ⇒
        # full re-embed via the heal sweeper).
        return f"nano:sha256:{self.weights_sha256}"


# ── Ollama ────────────────────────────────────────────────────────────────────


class OllamaProvider(EmbeddingProvider):
    """Embeddings via a local Ollama instance."""

    def __init__(self, model_name: str, base_url: str) -> None:
        import ollama as _ollama

        logger.info("Using Ollama embeddings: %s @ %s", model_name, base_url)
        self._client = _ollama.Client(host=base_url)
        self._model = model_name
        self._dim: int | None = None

    def embed(self, text: str) -> list[float]:
        # The `response` payload is `Any` (ollama SDK stubs); the inner
        # `embeddings[0]` is always list[float] for our `input=str` call.
        response = self._client.embed(model=self._model, input=text)
        vec: list[float] = response["embeddings"][0]
        if self._dim is None:
            self._dim = len(vec)
        return vec

    def embed_batch(self, texts: list[str]) -> list[list[float]]:
        response = self._client.embed(model=self._model, input=texts)
        vecs: list[list[float]] = response["embeddings"]
        if self._dim is None and vecs:
            self._dim = len(vecs[0])
        return vecs

    @property
    def dimension(self) -> int:
        if self._dim is None:
            self.embed("test")
        return self._dim or 768

    @property
    def fingerprint(self) -> str:
        # No verifiable weights hash over a served Ollama model; the
        # coarse identity detects model switches, not Ollama-side weight
        # updates (documented limitation of the base contract).
        return f"ollama:{self._model}"


# ── ONNX Hub ──────────────────────────────────────────────────────────────────


class ONNXHubProvider(EmbeddingProvider):
    """Load any ONNX embedding model from HuggingFace Hub.

    Recommended models:
    - sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2  (384d, RU+EN)
    - BAAI/bge-small-en-v1.5                                       (384d, EN fast)
    - intfloat/multilingual-e5-small                                (384d, multilingual)
    """

    def __init__(
        self,
        model_id: str,
        onnx_file: str = "onnx/model.onnx",
        max_length: int = 512,
        *,
        revision: str | None = None,
    ) -> None:
        import onnxruntime as ort
        from huggingface_hub import hf_hub_download
        from tokenizers import Tokenizer

        # M15.2: pin HF Hub revision (commit SHA or tag) to mitigate CWE-494
        # (download of code without integrity check). B615 requires `revision=`
        # be passed to every `hf_hub_download()` call. Operators MUST set
        # `EmbeddingConfig.hf_revision` explicitly when changing `model_id`.
        if not revision:
            raise ValueError(
                "ONNXHubProvider requires an explicit `revision` "
                "(set EmbeddingConfig.hf_revision or pass `revision=` directly) "
                "to pin the HuggingFace Hub download. This mitigates supply-chain "
                "risk (CWE-494)."
            )
        logger.info("Loading ONNX model: %s (%s) @ revision=%s", model_id, onnx_file, revision)

        # Kept for the vintage fingerprint: model@revision is the pinned
        # identity (M15.2) — a revision bump is a weights change.
        self._model_id = model_id
        self._revision = revision

        try:
            model_path = hf_hub_download(model_id, onnx_file, revision=revision)
        except Exception as exc:
            logger.warning(
                "hf_hub_download(%s, %s) failed, falling back to model.onnx: %s",
                model_id,
                onnx_file,
                exc,
            )
            model_path = hf_hub_download(model_id, "model.onnx", revision=revision)

        tokenizer_path = hf_hub_download(model_id, "tokenizer.json", revision=revision)
        self._tokenizer = Tokenizer.from_file(tokenizer_path)
        self._tokenizer.enable_truncation(max_length=max_length)
        self._tokenizer.enable_padding(length=max_length)

        n_threads = max(
            1,
            int(os.environ.get("VESMA_ORT_THREADS") or os.environ.get("OMP_NUM_THREADS") or "4"),
        )
        sess_opts = ort.SessionOptions()
        sess_opts.intra_op_num_threads = n_threads
        sess_opts.inter_op_num_threads = 1
        self._session = ort.InferenceSession(
            model_path,
            sess_options=sess_opts,
            providers=["CPUExecutionProvider"],
        )
        self._model_inputs = {inp.name for inp in self._session.get_inputs()}
        self._max_length = max_length
        test = self._infer(["test"])
        self._dim = int(test.shape[-1])
        logger.info("ONNX model ready: %s (dim=%d)", model_id, self._dim)

    def _infer(self, texts: list[str]) -> np.ndarray:
        encodings = self._tokenizer.encode_batch(texts)
        input_ids = np.array([e.ids for e in encodings], dtype=np.int64)
        attention_mask = np.array([e.attention_mask for e in encodings], dtype=np.int64)
        # Explicit type arg: tokenizers.Tokenizer.encode_batch returns Any
        # per the (untyped) huggingface_hub/tokenizers stubs.
        inputs: dict[str, np.ndarray] = {
            "input_ids": input_ids,
            "attention_mask": attention_mask,
        }
        if "token_type_ids" in self._model_inputs:
            inputs["token_type_ids"] = np.zeros_like(input_ids)
        # `ort.InferenceSession.run` is untyped in the onnxruntime stubs;
        # the first output is the (batch, seq, dim) tensor by convention.
        outputs = cast(list[np.ndarray], self._session.run(None, inputs))
        token_embs = outputs[0]  # (batch, seq, dim)
        mask = attention_mask[:, :, np.newaxis].astype(np.float32)
        summed = np.sum(token_embs * mask, axis=1) / np.maximum(np.sum(mask, axis=1), 1e-9)
        norms = np.linalg.norm(summed, axis=1, keepdims=True)
        result: np.ndarray = summed / np.maximum(norms, 1e-9)
        return result

    def embed(self, text: str) -> list[float]:
        return [float(x) for x in self._infer([text])[0]]

    def embed_batch(self, texts: list[str]) -> list[list[float]]:
        return [[float(x) for x in row] for row in self._infer(texts)]

    @property
    def dimension(self) -> int:
        return self._dim

    @property
    def fingerprint(self) -> str:
        # The pinned model@revision pair (M15.2) is the strongest key a
        # Hub-served model has — it changes on any operator-pinned bump.
        return f"onnxhub:{self._model_id}@{self._revision}"


# ── sentence-transformers ─────────────────────────────────────────────────────


class SentenceTransformerProvider(EmbeddingProvider):
    """sentence-transformers backend (optional dependency)."""

    def __init__(self, model_name: str) -> None:
        from sentence_transformers import SentenceTransformer

        logger.info("Using sentence-transformers: %s", model_name)
        self._model_name = model_name
        self._model = SentenceTransformer(model_name)
        self._dim = self._model.get_sentence_embedding_dimension() or 384

    def embed(self, text: str) -> list[float]:
        # sentence-transformers `.encode()` returns `Any` (untyped lib);
        # the contract is `np.ndarray` of shape (dim,). tolist() yields
        # list[float] — cast keeps mypy from complaining about the Any.
        arr = cast(np.ndarray, self._model.encode(text))
        return cast(list[float], arr.tolist())

    def embed_batch(self, texts: list[str]) -> list[list[float]]:
        arr = cast(np.ndarray, self._model.encode(texts))
        return cast(list[list[float]], arr.tolist())

    @property
    def dimension(self) -> int:
        return self._dim

    @property
    def fingerprint(self) -> str:
        # Coarse identity: detects model switches, not upstream weight
        # refreshes inside the same model name (documented limitation).
        return f"st:{self._model_name}"


# ── factory ───────────────────────────────────────────────────────────────────


#: The pre-NM-1c legacy default pair: chromadb + its built-in MiniLM
#: model. A config carrying BOTH values is the shipped default of an old
#: install — the factory degrades it wholesale to the bundled vesma-embed
#: (review #221 F1: the MiniLM string alone is not a bundled artifact
#: name, so degrading only the provider crashed NanoProvider with
#: FileNotFoundError on the resolved artifact).
_LEGACY_CHROMADB_MODEL = "all-minilm-l6-v2"


def create_embedding_provider(cfg: EmbeddingConfig) -> EmbeddingProvider:
    """Instantiate the configured embedding provider."""
    provider = cfg.provider.lower()
    if provider == "nano":
        return NanoProvider(cfg.model)
    if provider in ("chromadb", "chroma", "default"):
        # NM-1c migration: chromadb was removed from the runtime (ADR-0021).
        # Legacy config values degrade to the bundled nano embedder with a
        # loud warning instead of crashing the legacy install.
        if cfg.model.strip().lower() == _LEGACY_CHROMADB_MODEL:
            logger.warning(
                "provider=%s + model=%s is the deprecated pre-NM-1c default "
                "pair, using provider=nano + %s; set provider=nano explicitly",
                provider,
                cfg.model,
                MNEMA_EMBED_MODEL,
            )
            return NanoProvider(MNEMA_EMBED_MODEL)
        logger.warning(
            "provider=%s is deprecated, using nano; set provider=nano explicitly",
            provider,
        )
        return NanoProvider(cfg.model)
    if provider == "ollama":
        return OllamaProvider(cfg.model, cfg.ollama_url)
    if provider in ("onnx", "onnxhub"):
        return ONNXHubProvider(
            cfg.model,
            onnx_file=cfg.onnx_file,
            revision=cfg.hf_revision,
        )
    if provider in ("sentence-transformers", "st"):
        return SentenceTransformerProvider(cfg.model)
    raise ValueError(
        f"Unknown embedding provider: {provider!r}. "
        "Valid: nano, ollama, onnx, sentence-transformers"
    )


def config_fingerprint(cfg: EmbeddingConfig) -> str:
    """Vintage fingerprint of the embedder ``cfg`` WILL build — no session.

    Doctor-side twin of ``EmbeddingProvider.fingerprint``: computes the
    same identity ``create_embedding_provider(cfg)`` would produce
    WITHOUT instantiating it (no ORT session, no network — the nano leg
    only hashes the artifact file). MUST stay in sync with the factory's
    provider resolution, including the legacy chromadb degradation
    (review #221 F1): a drifted twin would make the doctor report false
    vintage mismatches.
    """
    provider = cfg.provider.lower()
    if provider == "nano":
        return f"nano:sha256:{mnema_weights_sha256(cfg.model)}"
    if provider in ("chromadb", "chroma", "default"):
        model = cfg.model
        if model.strip().lower() == _LEGACY_CHROMADB_MODEL:
            model = MNEMA_EMBED_MODEL
        return f"nano:sha256:{mnema_weights_sha256(model)}"
    if provider == "ollama":
        return f"ollama:{cfg.model}"
    if provider in ("onnx", "onnxhub"):
        # Asymmetry (harmless): with an empty hf_revision this twin emits
        # "onnxhub:<model>@" while the factory raises — runtime fails loud
        # first, so the degenerate string never reaches stored metadata.
        return f"onnxhub:{cfg.model}@{cfg.hf_revision}"
    if provider in ("sentence-transformers", "st"):
        return f"st:{cfg.model}"
    # Unknown provider never instantiates (factory raises); report the
    # raw coarse identity so doctor can still surface the mismatch.
    return f"{provider}:{cfg.model}"
