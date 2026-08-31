"""测试辅助：造真实的 WAV 字节。"""

import base64

from app.wavutil import SAMPLE_RATE, build_wav


def make_wav(duration_ms: int, sample_rate: int = SAMPLE_RATE) -> bytes:
    """造一段指定时长的真实 WAV（内容是静音，格式合法）。"""
    n = int(sample_rate * 2 * duration_ms / 1000)
    n -= n % 2
    return build_wav(b"\x01\x00" * (n // 2), sample_rate=sample_rate)


def tts_response(wav: bytes) -> dict:
    """模拟 MiMo TTS 的 200 响应体。"""
    return {
        "choices": [
            {"message": {"audio": {"data": base64.b64encode(wav).decode()}}}
        ]
    }


def llm_response(payload: dict) -> dict:
    """模拟 OpenAI 兼容的 chat.completions 响应体。"""
    import json

    return {"choices": [{"message": {"content": json.dumps(payload, ensure_ascii=False)}}]}
