"""WAV 解析、静音生成与拼接。

MiMo TTS 返回 24kHz / PCM16LE / 单声道。同格式的 WAV 直接在 PCM 层面
拼接即可，不必过 ffmpeg —— 少一个子进程，也更好测。ffmpeg 只在导出
mp3 时用到（见 export.py）。
"""

from __future__ import annotations

import struct
from dataclasses import dataclass

SAMPLE_RATE = 24000
CHANNELS = 1
SAMPLE_WIDTH = 2  # PCM16
BYTES_PER_SEC = SAMPLE_RATE * CHANNELS * SAMPLE_WIDTH


class WavFormatError(ValueError):
    pass


@dataclass(frozen=True)
class WavInfo:
    sample_rate: int
    channels: int
    sample_width: int
    pcm: bytes

    @property
    def duration_ms(self) -> int:
        bps = self.sample_rate * self.channels * self.sample_width
        if bps == 0:
            raise WavFormatError("非法的 WAV 参数，无法计算时长")
        return round(len(self.pcm) * 1000 / bps)


def parse_wav(data: bytes) -> WavInfo:
    """解析 WAV，返回格式参数与 PCM 数据体。

    只支持 PCM(fmt=1)。遍历 chunk 而不是假定 44 字节固定头，因为部分
    编码器会插入 LIST / fact 之类的额外 chunk。
    """
    if len(data) < 12 or data[:4] != b"RIFF" or data[8:12] != b"WAVE":
        raise WavFormatError("不是合法的 RIFF/WAVE 数据")

    pos = 12
    fmt: tuple[int, int, int, int] | None = None
    pcm: bytes | None = None

    while pos + 8 <= len(data):
        chunk_id = data[pos : pos + 4]
        (chunk_size,) = struct.unpack_from("<I", data, pos + 4)
        body = pos + 8
        if chunk_id == b"fmt ":
            if chunk_size < 16:
                raise WavFormatError("fmt chunk 过短")
            audio_format, channels, sample_rate, _byte_rate, _align, bits = (
                struct.unpack_from("<HHIIHH", data, body)
            )
            if audio_format != 1:
                raise WavFormatError(f"只支持 PCM，收到 format={audio_format}")
            fmt = (sample_rate, channels, bits // 8, bits)
        elif chunk_id == b"data":
            # 有些流式落盘的文件 data size 写 0 或 0xFFFFFFFF，此时取到结尾
            if chunk_size == 0 or body + chunk_size > len(data):
                pcm = data[body:]
            else:
                pcm = data[body : body + chunk_size]
        pos = body + chunk_size + (chunk_size & 1)  # chunk 按偶数字节对齐

    if fmt is None:
        raise WavFormatError("缺少 fmt chunk")
    if pcm is None:
        raise WavFormatError("缺少 data chunk")

    sample_rate, channels, sample_width, bits = fmt
    if bits != 16:
        raise WavFormatError(f"只支持 16bit，收到 {bits}bit")
    return WavInfo(sample_rate, channels, sample_width, pcm)


def build_wav(
    pcm: bytes,
    sample_rate: int = SAMPLE_RATE,
    channels: int = CHANNELS,
    sample_width: int = SAMPLE_WIDTH,
) -> bytes:
    """给 PCM 数据套上 WAV 头。"""
    byte_rate = sample_rate * channels * sample_width
    header = (
        b"RIFF"
        + struct.pack("<I", 36 + len(pcm))
        + b"WAVE"
        + b"fmt "
        + struct.pack(
            "<IHHIIHH",
            16,
            1,
            channels,
            sample_rate,
            byte_rate,
            channels * sample_width,
            sample_width * 8,
        )
        + b"data"
        + struct.pack("<I", len(pcm))
    )
    return header + pcm


def silence_pcm(duration_ms: int, sample_rate: int = SAMPLE_RATE) -> bytes:
    """生成指定时长的静音 PCM。"""
    if duration_ms <= 0:
        return b""
    n = int(sample_rate * CHANNELS * SAMPLE_WIDTH * duration_ms / 1000)
    n -= n % SAMPLE_WIDTH  # 对齐到采样点边界
    return b"\x00" * n


def duration_ms_of(data: bytes) -> int:
    """WAV 字节 → 时长毫秒。"""
    return parse_wav(data).duration_ms
