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


def concat_wav_timeline(segments: list[dict]) -> tuple[bytes, list[dict]]:
    """字幕时间轴模式拼接：每段音频放到它的 start_ms 锚点上。

    音频比字幕窗口短 → 后面补静音，对齐下一段的 start。
    音频比窗口长 → 如实放置、不截断，后续段被顺延；该段标记 overflow_ms，
    供前端提示用户精简文本或调语速。

    返回 (wav_bytes, placements)。placements 每项含
    {seq, start_ms, placed_ms, duration_ms, window_ms, overflow_ms}。
    """
    timed = [s for s in segments if s.get("start_ms") is not None]
    timed.sort(key=lambda s: s["start_ms"])

    chunks: list[bytes] = []
    placements: list[dict] = []
    cursor_ms = 0  # 已写出的音轨末尾（毫秒）
    rate = SAMPLE_RATE

    for seg in timed:
        h = seg.get("audio_hash")
        if not h or not audio_store.exists(h):
            continue
        info = parse_wav(audio_store.load(h))
        if info.sample_rate != rate and chunks:
            raise ExportError(
                f"段音频采样率不一致：{info.sample_rate} vs {rate}，无法直接拼接"
            )
        rate = info.sample_rate

        start = int(seg["start_ms"])
        # 锚点在光标之后 → 先补静音把空档填上；在光标之前（上一段溢出压过来）
        # → 只能顺延，从当前光标接着放
        placed = start
        if start > cursor_ms:
            chunks.append(silence_pcm(start - cursor_ms, rate))
            cursor_ms = start
        else:
            placed = cursor_ms

        chunks.append(info.pcm)
        dur = info.duration_ms
        cursor_ms = placed + dur

        end = seg.get("end_ms")
        window = (int(end) - start) if end is not None else None
        overflow = max(0, dur - window) if window is not None else 0
        placements.append({
            "seq": seg.get("seq"),
            "id": seg.get("id"),
            "start_ms": start,
            "placed_ms": placed,
            "duration_ms": dur,
            "window_ms": window,
            "overflow_ms": overflow,
        })

    if not chunks:
        raise ExportError("没有任何已合成的段落，无法导出")
    return build_wav(b"".join(chunks), sample_rate=rate), placements


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
