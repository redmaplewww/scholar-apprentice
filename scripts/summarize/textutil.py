#!/usr/bin/env python3
"""轻量文本工具：不依赖外部模型，供聚类、去重、断言强度检查使用。

中文按二元字组切分，拉丁文按词切分；向量用 hashed TF-IDF，余弦相似度。
这样做的好处是离线、确定、快；代价是对同义词不敏感——所以归轴仍由 LLM
复核，这里只负责把候选缩小到可复核的规模。
"""
from __future__ import annotations

import hashlib
import math
import re
from collections import Counter

CJK = re.compile(r"[\u4e00-\u9fff]")
LATIN = re.compile(r"[a-z0-9][a-z0-9\-_+.]*")
WS = re.compile(r"\s+")

# 断言强度词：出现即要求证据规模支撑，否则 A2 降级
STRONG_CLAIM = [
    "普适", "普遍", "所有", "任意", "必然", "总是", "完全", "彻底", "证明",
    "最优", "显著优于", "universal", "always", "prove", "optimal", "for all",
]
HEDGE = ["在该体系下", "在所测算例中", "初步", "可能", "倾向于", "在本文范围内"]


def norm(s: str) -> str:
    return WS.sub(" ", (s or "").replace("\u3000", " ")).strip()


def flat(s: str) -> str:
    """用于逐字比对的归一化：去掉所有空白。"""
    return re.sub(r"\s+", "", (s or ""))


def _stem(w: str) -> str:
    """极轻量词干：只处理复数与常见派生后缀，够让 stencils/stencil 这类合并。"""
    for suf in ("ing", "ed"):
        if len(w) > 6 and w.endswith(suf):
            return w[: -len(suf)]
    if len(w) > 4 and w.endswith("es"):
        return w[:-2]
    if len(w) > 3 and w.endswith("s") and not w.endswith("ss"):
        return w[:-1]
    return w


def tokens(s: str) -> list[str]:
    s = (s or "").lower()
    out: list[str] = []
    for w in LATIN.findall(s):
        if len(w) > 1:
            out.append(w)
            st = _stem(w)
            if st != w:
                out.append(st)
    chars = [c for c in s if CJK.match(c)]
    for i in range(len(chars) - 1):
        out.append(chars[i] + chars[i + 1])
    for c in chars:            # 单字也留一份，短术语（如"势""熵"）才有信号
        out.append(c)
    return out


def tokens_latin(s: str) -> list[str]:
    """只取拉丁词：中英混排时用于跨语言对照的第二通道。"""
    return [w for w in LATIN.findall((s or "").lower()) if len(w) > 1]


def _h(tok: str) -> int:
    return int(hashlib.blake2b(tok.encode("utf-8"), digest_size=4).hexdigest(), 16)


class Vectorizer:
    """语料内 IDF + 哈希桶，避免持有全词表。"""

    BUCKETS = 4096

    def __init__(self):
        self.df: Counter[int] = Counter()
        self.n = 0

    def fit(self, docs: list[str]) -> "Vectorizer":
        self.df.clear()
        self.n = max(1, len(docs))
        for d in docs:
            for k in {_h(t) % self.BUCKETS for t in tokens(d)}:
                self.df[k] += 1
        return self

    def vec(self, text: str) -> dict[int, float]:
        tf = Counter(_h(t) % self.BUCKETS for t in tokens(text))
        if not tf:
            return {}
        out = {}
        for k, c in tf.items():
            idf = math.log((1 + self.n) / (1 + self.df.get(k, 0))) + 1.0
            out[k] = (1 + math.log(c)) * idf
        n = math.sqrt(sum(v * v for v in out.values())) or 1.0
        return {k: v / n for k, v in out.items()}


def cosine(a: dict[int, float], b: dict[int, float]) -> float:
    if not a or not b:
        return 0.0
    if len(a) > len(b):
        a, b = b, a
    return sum(v * b.get(k, 0.0) for k, v in a.items())


def centroid(vecs: list[dict[int, float]]) -> dict[int, float]:
    acc: Counter = Counter()
    for v in vecs:
        for k, x in v.items():
            acc[k] += x
    if not acc:
        return {}
    n = math.sqrt(sum(x * x for x in acc.values())) or 1.0
    return {k: x / n for k, x in acc.items()}


def top_terms(texts: list[str], k: int = 4) -> list[str]:
    """给出簇的高频术语，用于候选轴命名（人工或 LLM 再润色）。"""
    c: Counter = Counter()
    for t in texts:
        c.update(tokens(t))
    stop = {"的", "了", "与", "和", "在", "是", "对", "中", "为", "基于", "一种", "方法",
            "the", "of", "and", "for", "with", "using", "approach", "method"}
    out = []
    for tok, n in c.most_common(40):
        if tok in stop or n < 2:
            continue
        out.append(tok)
        if len(out) >= k:
            break
    return out


def has_strong_claim(text: str) -> bool:
    low = (text or "").lower()
    return any(w in low for w in STRONG_CLAIM)


def hedge(text: str) -> str:
    if any(h in text for h in HEDGE):
        return text
    return "在该体系与所测算例下，" + (text or "").lstrip("，, ")


def quote_in(quote: str, source: str) -> bool:
    """A1 的确定性判定：去掉空白后必须逐字命中。"""
    q, s = flat(quote), flat(source)
    return bool(q) and q in s


def snippet(source: str, quote: str, width: int = 90) -> str:
    """在原文里定位引文，返回上下文；定位不到返回空串。"""
    sq = flat(quote)
    fs = flat(source)
    i = fs.find(sq)
    if i < 0:
        return ""
    raw_idx = 0
    seen = 0
    for j, ch in enumerate(source):
        if not ch.isspace():
            if seen == i:
                raw_idx = j
                break
            seen += 1
    return norm(source[max(0, raw_idx - width): raw_idx + len(quote) + width])
