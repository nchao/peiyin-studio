"""小米 MiMo TTS 客户端。

接口形态是 OpenAI chat.completions 的变体：待合成文本放 assistant 消息，
音色/格式放 audio 对象，风格靠文本开头的 `(风格词)` 标签。不支持 SSML。

鉴权头：官方文档的裸 HTTP 示例用 `api-key: xxx`，但平台又声明兼容
OpenAI 协议（标准 SDK 发 `Authorization: Bearer`）。两种写法哪个生效
未实测，这里同时发送 —— 多余的那个头服务端会忽略。
"""

from __future__ import annotations

import asyncio
import base64
import random

import httpx

from .config import settings
from .voices import build_synth_payload_text, resolve_style_tags
from .wavutil import duration_ms_of

RETRY_STATUS = {429, 500, 502, 503, 504}


def _backoff_seconds(attempt: int, status: int | None) -> float:
    """退避时长。429（限流）退避更狠、抖动更大 —— 多个并发请求同时被限时，
    大抖动能把重试错峰散开，避免退避后又一起重发（惊群）再次撞限。
    5xx 用较温和的指数退避即可。attempt 从 0 起。"""
    if status == 429:
        base = min(2.0 * (3 ** attempt), 30.0)   # 2,6,18,30,30...
        return base + random.uniform(0, base * 0.5)
    base = min(2 ** attempt, 8)                    # 1,2,4,8,8...
    return base + random.uniform(0, 0.5)


class TTSError(RuntimeError):
    """合成失败，且已耗尽重试。"""

    def __init__(self, message: str, *, status: int | None = None):
        super().__init__(message)
        self.status = status


def _headers() -> dict[str, str]:
    key = settings.mimo_api_key
    return {
        "api-key": key,
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
    }


def _payload(text: str, voice: str) -> dict:
    """预置音色请求体。voice 是音色名（如"苏打"）。"""
    return {
        "model": settings.mimo_tts_model,
        "messages": [{"role": "assistant", "content": text}],
        "audio": {"format": "wav", "voice": voice},
        "stream": False,
    }


def _clone_data_url(sample: bytes, ext: str) -> str:
    """样本字节 → DataURL。实测 voiceclone 的 audio.voice 要 DataURL，
    不是官方文档写的裸 base64。mime 随样本格式。"""
    mime = "audio/mpeg" if ext == "mp3" else "audio/wav"
    return f"data:{mime};base64,{base64.b64encode(sample).decode()}"


def _clone_payload(text: str, style_words: str, sample: bytes, ext: str) -> dict:
    """克隆音色请求体：voiceclone 模型 + 样本 DataURL。

    user 角色放风格描述词，assistant 角色放正文（与 voicedesign 一致）。
    """
    messages = []
    if style_words:
        messages.append({"role": "user", "content": style_words})
    messages.append({"role": "assistant", "content": text})
    return {
        "model": settings.mimo_clone_model,
        "messages": messages,
        "audio": {"format": "wav", "voice": _clone_data_url(sample, ext)},
        "stream": False,
    }


def _extract_audio(body: dict) -> bytes:
    try:
        b64 = body["choices"][0]["message"]["audio"]["data"]
    except (KeyError, IndexError, TypeError) as exc:
        raise TTSError(f"响应结构异常，未找到音频数据: {body!r}") from exc
    if not b64:
        raise TTSError("响应里的音频数据为空")
    return base64.b64decode(b64)


async def synthesize(
    synth_text: str,
    voice: str,
    style: str | None,
    *,
    clone_sample: tuple[bytes, str] | None = None,
    client: httpx.AsyncClient | None = None,
) -> tuple[bytes, int]:
    """合成一段音频，返回 (wav_bytes, duration_ms)。

    clone_sample 为 (样本字节, 扩展名) 时走声音克隆模型；否则用预置音色。
    重试覆盖 429 与 5xx，指数退避 + 抖动。4xx（除 429）直接抛，重试没意义。
    """
    if clone_sample is not None:
        # 克隆分支：风格走 user 消息的描述词，不拼进正文前缀
        sample, ext = clone_sample
        style_words = resolve_style_tags(style)
        payload = _clone_payload(synth_text, style_words, sample, ext)
    else:
        text = build_synth_payload_text(synth_text, style)
        payload = _payload(text, voice)
    url = f"{settings.mimo_base_url.rstrip('/')}/chat/completions"

    own_client = client is None
    http = client or httpx.AsyncClient(timeout=httpx.Timeout(180.0, connect=15.0))
    try:
        last_err: Exception | None = None
        last_status: int | None = None
        for attempt in range(settings.tts_max_retry + 1):
            try:
                resp = await http.post(url, json=payload, headers=_headers())
            except httpx.HTTPError as exc:
                last_err = TTSError(f"请求 MiMo TTS 失败: {exc}")
                last_status = None
            else:
                if resp.status_code == 200:
                    wav = _extract_audio(resp.json())
                    return wav, duration_ms_of(wav)
                detail = resp.text[:500]
                if resp.status_code not in RETRY_STATUS:
                    raise TTSError(
                        f"MiMo TTS 返回 {resp.status_code}: {detail}",
                        status=resp.status_code,
                    )
                last_err = TTSError(
                    f"MiMo TTS 返回 {resp.status_code}: {detail}",
                    status=resp.status_code,
                )
                last_status = resp.status_code

            if attempt < settings.tts_max_retry:
                await asyncio.sleep(_backoff_seconds(attempt, last_status))

        assert last_err is not None
        raise last_err
    finally:
        if own_client:
            await http.aclose()
