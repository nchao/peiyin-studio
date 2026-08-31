"""音频拼接与格式导出。

WAV 拼接在 PCM 层面做（同格式，无需重编码）；转 mp3 才调 ffmpeg。
"""

from __future__ import annotations

import subprocess

from . import audio_store
from .wavutil import SAMPLE_RATE, build_wav, parse_wav, silence_pcm


class ExportError(RuntimeError):
    pass


def concat_wav(segments: list[dict]) -> tuple[bytes, list[int]]:
    """按顺序拼接段音频，段间插静音。

    返回 (wav_bytes, 每段起始偏移毫秒)。缺音频的段跳过，与 SRT 逻辑一致。
    """
    chunks: list[bytes] = []
    offsets: list[int] = []
    cursor_bytes = 0
    rate = SAMPLE_RATE

    for seg in segments:
        h = seg.get("audio_hash")
        if not h or not audio_store.exists(h):
            continue
        info = parse_wav(audio_store.load(h))
        if info.sample_rate != rate and chunks:
            raise ExportError(
                f"段音频采样率不一致：{info.sample_rate} vs {rate}，无法直接拼接"
            )
        rate = info.sample_rate
        offsets.append(round(cursor_bytes * 1000 / (rate * 1 * 2)))
        chunks.append(info.pcm)
        cursor_bytes += len(info.pcm)

        pause = int(seg.get("pause_after_ms") or 0)
        if pause > 0:
            sil = silence_pcm(pause, rate)
            chunks.append(sil)
            cursor_bytes += len(sil)

    if not chunks:
        raise ExportError("没有任何已合成的段落，无法导出")
    return build_wav(b"".join(chunks), sample_rate=rate), offsets


def wav_to_mp3(wav_bytes: bytes, bitrate: str = "192k") -> bytes:
    """调 ffmpeg 转 mp3。走 stdin/stdout，不落临时文件。"""
    try:
        proc = subprocess.run(
            [
                "ffmpeg", "-hide_banner", "-loglevel", "error",
                "-f", "wav", "-i", "pipe:0",
                "-c:a", "libmp3lame", "-b:a", bitrate,
                "-f", "mp3", "pipe:1",
            ],
            input=wav_bytes,
            capture_output=True,
            timeout=300,
        )
    except FileNotFoundError as exc:
        raise ExportError("未找到 ffmpeg，无法导出 mp3") from exc
    except subprocess.TimeoutExpired as exc:
        raise ExportError("ffmpeg 转码超时") from exc
    if proc.returncode != 0:
        raise ExportError(f"ffmpeg 转码失败: {proc.stderr.decode(errors='replace')[:300]}")
    return proc.stdout
