"""规则分段：LLM 不可用时的兜底，也用于超长段的强制再切。

只按标点切，绝不改一个字 —— 保证 display_text 拼回来等于原文。
"""

from __future__ import annotations

import re

# 句末标点（切在它后面）
SENT_END = "。！？!?；;…"
# 句中标点（句子太长时的次级切点）
CLAUSE_END = "，,、：:"

TARGET = 22   # 目标段长
MAX_LEN = 40  # 超过就找次级切点


def _split_keep(text: str, puncts: str) -> list[str]:
    """按标点切，标点留在前一段末尾。"""
    out: list[str] = []
    buf = ""
    for ch in text:
        buf += ch
        if ch in puncts:
            out.append(buf)
            buf = ""
    if buf:
        out.append(buf)
    return out


def _merge_short(parts: list[str], target: int, hard_max: int) -> list[str]:
    """把过短的相邻片段合并到接近 target，但不越过 hard_max。"""
    merged: list[str] = []
    for p in parts:
        if merged and len(merged[-1]) + len(p) <= max(target, hard_max // 2):
            merged[-1] += p
        else:
            merged.append(p)
    return merged


def _hard_wrap(s: str, limit: int) -> list[str]:
    """连一个标点都没有的超长串，按长度硬切。"""
    return [s[i : i + limit] for i in range(0, len(s), limit)] or [s]


def rule_split(text: str, target: int = TARGET, max_len: int = MAX_LEN) -> list[str]:
    """把原文切成段列表。拼接结果逐字等于去掉首尾空白的原文。

    换行视为强制切点（解说稿常按镜头分行）。
    """
    result: list[str] = []
    for line in text.split("\n"):
        line = line.strip()
        if not line:
            continue
        sents = _merge_short(_split_keep(line, SENT_END), target, max_len)
        for s in sents:
            if len(s) <= max_len:
                result.append(s)
                continue
            clauses = _merge_short(_split_keep(s, CLAUSE_END), target, max_len)
            for c in clauses:
                if len(c) <= max_len:
                    result.append(c)
                else:
                    result.extend(_hard_wrap(c, max_len))
    return result


def chunk_for_llm(text: str, limit: int) -> list[str]:
    """把原文切成不超过 limit 字的块，供 LLM 分别处理。

    只在句末标点或换行处切，保证每块语义完整；拼接结果逐字等于原文
    （含换行），这样各块的分段结果直接顺序拼接就还原全篇。
    """
    if len(text) <= limit:
        return [text] if text.strip() else []

    # 先按句末标点切成最小单元，再贪心装箱到 limit
    units: list[str] = []
    buf = ""
    for ch in text:
        buf += ch
        if ch in SENT_END or ch == "\n":
            units.append(buf)
            buf = ""
    if buf:
        units.append(buf)

    chunks: list[str] = []
    cur = ""
    for u in units:
        if cur and len(cur) + len(u) > limit:
            chunks.append(cur)
            cur = u
        else:
            cur += u
        # 单个句子就超 limit：按次级标点再切，避免一块撑爆输出
        while len(cur) > limit:
            cut = max(
                (cur.rfind(p, 0, limit) for p in CLAUSE_END),
                default=-1,
            )
            cut = cut + 1 if cut > limit // 3 else limit
            chunks.append(cur[:cut])
            cur = cur[cut:]
    if cur:
        chunks.append(cur)
    return [c for c in chunks if c.strip()]


def normalize_for_compare(s: str) -> str:
    """比对用的归一化：去掉所有空白字符。

    只去空白，不去标点 —— 标点影响 TTS 停顿，LLM 擅自增删标点属于需要
    暴露的改动。
    """
    return re.sub(r"\s+", "", s)
