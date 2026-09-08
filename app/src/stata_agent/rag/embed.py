"""Embedder：文本 → 向量。

先给确定性本地实现 HashEmbedder（无网络/依赖，中文友好：对 ascii 词/汉字串，
把每个 token 用 sha256 映射到桶并加权重）。真 bge-m3/API 后续按同接口替换。
"""

from __future__ import annotations

import hashlib
import math
from typing import Protocol

from .retriever import tokenize


class Embedder(Protocol):
    dim: int

    def embed_text(self, text: str) -> list[float]: ...


class HashEmbedder:
    """词袋哈希嵌入：确定性、本地、零依赖。仅用于检索排序，不做语义学习。"""

    def __init__(self, dim: int = 256):
        self.dim = dim

    def embed_text(self, text: str) -> list[float]:
        vec = [0.0] * self.dim
        for tok in tokenize(text):
            h = int(hashlib.sha256(tok.encode("utf-8")).hexdigest(), 16)
            idx = h % self.dim
            sign = 1.0 if (h >> 8) % 2 == 0 else -1.0
            vec[idx] += sign
        norm = math.sqrt(sum(v * v for v in vec)) or 1.0
        return [v / norm for v in vec]


def cosine(a: list[float], b: list[float]) -> float:
    return sum(x * y for x, y in zip(a, b, strict=False))
