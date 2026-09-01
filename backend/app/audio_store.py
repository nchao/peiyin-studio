"""音频缓存：按内容哈希落盘，改一段只重合成那一段。"""

from __future__ import annotations

import hashlib
from pathlib import Path

from .config import settings


def audio_hash(synth_text: str, voice: str, style: str | None, model: str,
               speed: float = 1.0) -> str:
    """内容哈希。任一因子变化都应失效（含语速 —— 变速后落盘的音频不同）。

    用 \\x00 分隔，避免 ("ab","c") 与 ("a","bc") 撞哈希。speed 归一化成
    定点字符串，1.0 与 1 视为同一，避免浮点表示差异导致的缓存不命中。
    """
    raw = "\x00".join([synth_text, voice, style or "", model, f"{speed:.3f}"])
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def path_of(h: str) -> Path:
    return settings.audio_dir / f"{h}.wav"


def exists(h: str) -> bool:
    return path_of(h).is_file()


def save(h: str, data: bytes) -> Path:
    settings.audio_dir.mkdir(parents=True, exist_ok=True)
    p = path_of(h)
    # 先写临时文件再 rename，避免并发或中断留下半截文件被当成有效缓存
    tmp = p.with_suffix(".wav.tmp")
    tmp.write_bytes(data)
    tmp.replace(p)
    return p


def load(h: str) -> bytes:
    return path_of(h).read_bytes()


def purge_orphans(referenced: set[str]) -> int:
    """删掉没有任何 segment 引用的音频文件，返回删除数量。"""
    if not settings.audio_dir.is_dir():
        return 0
    removed = 0
    for f in settings.audio_dir.glob("*.wav"):
        if f.stem not in referenced:
            f.unlink()
            removed += 1
    return removed
