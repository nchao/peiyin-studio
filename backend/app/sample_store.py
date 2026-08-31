"""克隆音色的样本音频存储。

与合成产物缓存（audio_store / data/audio）分开：样本存 data/samples/，
purge_orphans 不会碰这里，避免克隆样本被当孤儿删掉。样本按内容哈希命名，
同一段音频多次上传只存一份。
"""

from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path

from .config import settings

ALLOWED_EXT = {"wav", "mp3"}


def probe_duration_ms(data: bytes, ext: str) -> int | None:
    """用 ffprobe 探测样本时长（毫秒）。样本可能是任意采样率的 mp3/wav，
    不能用只认 24k/16bit 的 wavutil。

    走临时文件而非 stdin 管道 —— wav 的总时长在文件尾，管道不可 seek 时
    ffprobe 拿不到 format.duration。探测失败返回 None（不阻断上传）。
    """
    import tempfile

    tmp = None
    try:
        with tempfile.NamedTemporaryFile(suffix=f".{ext}", delete=False) as f:
            f.write(data)
            tmp = f.name
        proc = subprocess.run(
            ["ffprobe", "-hide_banner", "-loglevel", "error",
             "-show_entries", "format=duration", "-of", "json", tmp],
            capture_output=True, timeout=30,
        )
        if proc.returncode != 0:
            return None
        dur = json.loads(proc.stdout)["format"]["duration"]
        return round(float(dur) * 1000)
    except (OSError, ValueError, KeyError, subprocess.TimeoutExpired):
        return None
    finally:
        if tmp:
            Path(tmp).unlink(missing_ok=True)


def sample_dir() -> Path:
    return settings.data_dir / "samples"


def sample_hash(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def path_of(h: str, ext: str) -> Path:
    return sample_dir() / f"{h}.{ext}"


def exists(h: str, ext: str) -> bool:
    return path_of(h, ext).is_file()


def save(data: bytes, ext: str) -> str:
    """存样本，返回内容哈希。已存在则跳过写入。"""
    if ext not in ALLOWED_EXT:
        raise ValueError(f"不支持的样本格式: {ext}")
    h = sample_hash(data)
    d = sample_dir()
    d.mkdir(parents=True, exist_ok=True)
    p = path_of(h, ext)
    if not p.is_file():
        tmp = p.with_suffix(p.suffix + ".tmp")
        tmp.write_bytes(data)
        tmp.replace(p)
    return h


def load(h: str, ext: str) -> bytes:
    return path_of(h, ext).read_bytes()


def delete(h: str, ext: str) -> None:
    """删样本文件。仅当没有别的克隆音色共享同一样本时才该调用。"""
    p = path_of(h, ext)
    if p.is_file():
        p.unlink()
