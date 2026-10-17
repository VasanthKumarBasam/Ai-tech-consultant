"""Embedding back-ends.

* fastembed (default): BAAI/bge-small-en-v1.5, runs locally on CPU, no key, ~130 MB download once.
* voyage: Voyage AI API (Anthropic's recommended embeddings partner), needs VOYAGE_API_KEY.
* hash: dependency-free feature-hashing embedder for tests/offline. Lexical only.

All vectors are L2-normalised so a dot product is cosine similarity.
"""
from __future__ import annotations

import hashlib
import math
import os
import re
from typing import Protocol

import numpy as np

from ..config import Settings


class Embedder(Protocol):
    name: str
    dim: int

    def embed_documents(self, texts: list[str]) -> np.ndarray: ...
    def embed_query(self, text: str) -> np.ndarray: ...


def _normalize(m: np.ndarray) -> np.ndarray:
    m = np.asarray(m, dtype=np.float32)
    norms = np.linalg.norm(m, axis=-1, keepdims=True)
    norms[norms == 0] = 1.0
    return m / norms


class HashEmbedder:
    """Signed feature hashing of word unigrams + bigrams with log-tf weighting."""

    def __init__(self, dim: int = 768):
        self.dim = dim
        self.name = f"hash-{dim}"

    def _vec(self, text: str) -> np.ndarray:
        words = re.findall(r"[a-z0-9]+", text.lower())
        feats = words + [f"{a}_{b}" for a, b in zip(words, words[1:])]
        v = np.zeros(self.dim, dtype=np.float32)
        counts: dict[str, int] = {}
        for f in feats:
            counts[f] = counts.get(f, 0) + 1
        for f, c in counts.items():
            h = int(hashlib.md5(f.encode()).hexdigest(), 16)
            v[h % self.dim] += (1.0 if (h >> 64) & 1 else -1.0) * (1 + math.log(c))
        return v

    def embed_documents(self, texts: list[str]) -> np.ndarray:
        if not texts:
            return np.zeros((0, self.dim), dtype=np.float32)
        return _normalize(np.stack([self._vec(t) for t in texts]))

    def embed_query(self, text: str) -> np.ndarray:
        return self.embed_documents([text])[0]


class FastEmbedEmbedder:
    def __init__(self, model: str):
        try:
            from fastembed import TextEmbedding
        except ImportError as e:  # pragma: no cover
            raise RuntimeError("fastembed is not installed; pip install fastembed or set KB_EMBEDDER=hash") from e
        try:
            self.model = TextEmbedding(model)
        except Exception as e:  # pragma: no cover - network/model download problems
            raise RuntimeError(
                f"Could not load embedding model {model} ({e}). It downloads once from Hugging Face; "
                "check your network or set KB_EMBEDDER=hash for an offline fallback."
            ) from e
        self.name = f"fastembed:{model}"
        self.dim = len(next(iter(self.model.embed(["probe"]))))

    def embed_documents(self, texts: list[str]) -> np.ndarray:
        if not texts:
            return np.zeros((0, self.dim), dtype=np.float32)
        return _normalize(np.stack(list(self.model.embed(texts, batch_size=32))))

    def embed_query(self, text: str) -> np.ndarray:
        return _normalize(np.stack(list(self.model.query_embed([text]))))[0]


class VoyageEmbedder:
    URL = "https://api.voyageai.com/v1/embeddings"

    def __init__(self, model: str):
        import httpx

        self.key = os.getenv("VOYAGE_API_KEY")
        if not self.key:
            raise RuntimeError("VOYAGE_API_KEY not set")
        self.model = model
        self.name = f"voyage:{model}"
        self._http = httpx.Client(timeout=60)
        self.dim = len(self._embed(["probe"], "query")[0])

    def _embed(self, texts: list[str], input_type: str) -> np.ndarray:
        out = []
        for i in range(0, len(texts), 64):
            r = self._http.post(
                self.URL,
                headers={"Authorization": f"Bearer {self.key}"},
                json={"input": texts[i : i + 64], "model": self.model, "input_type": input_type},
            )
            r.raise_for_status()
            out.extend(d["embedding"] for d in sorted(r.json()["data"], key=lambda d: d["index"]))
        return _normalize(np.array(out))

    def embed_documents(self, texts: list[str]) -> np.ndarray:
        if not texts:
            return np.zeros((0, self.dim), dtype=np.float32)
        return self._embed(texts, "document")

    def embed_query(self, text: str) -> np.ndarray:
        return self._embed([text], "query")[0]


_cache: dict[str, Embedder] = {}


def get_embedder(settings: Settings) -> Embedder:
    key = f"{settings.embedder}:{settings.fastembed_model}:{settings.voyage_model}"
    if key not in _cache:
        if settings.embedder == "hash":
            _cache[key] = HashEmbedder()
        elif settings.embedder == "voyage":
            _cache[key] = VoyageEmbedder(settings.voyage_model)
        elif settings.embedder == "fastembed":
            _cache[key] = FastEmbedEmbedder(settings.fastembed_model)
        else:
            raise ValueError(f"Unknown KB_EMBEDDER={settings.embedder}")
    return _cache[key]
