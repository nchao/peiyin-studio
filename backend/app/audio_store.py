"""音频缓存：按内容哈希落盘，改一段只重合成那一段。"""

from __future__ import annotations

import hashlib
from pathlib import Path

from .config import settings


def audio_hash(synth_text: str, voice: str, style: str | None, model: str) -> str:
    """内容哈希。四个因子任一变化都应失效。

    用 \\x00 分隔，避免 ("ab","c") 与 ("a","bc") 撞哈希。
    """
    raw = "\x00".join([synth_text, voice, style or "", model])
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
