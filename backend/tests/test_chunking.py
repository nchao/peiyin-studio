"""预处理分块：切块保真、并行、部分失败、token 截断。"""

import httpx
import pytest
import respx

from app.config import settings
from app.llm_preprocess import PreprocessError, preprocess
from app.segmenter import chunk_for_llm, normalize_for_compare
from helpers import llm_response

URL = "https://api.xiaomimimo.com/v1/chat/completions"

PARA = [
    "今天我们聊一个被很多人忽略的问题。",
    "大部分人第一反应是数据库扛不住了，于是加索引、加缓存，折腾一大圈问题还在。",
    "真正的瓶颈往往藏在序列化这一步。",
    "而这段时间完全不会出现在慢查询日志里。",
]


# ---------- 切块 ----------

def test_短文本不切块():
    assert chunk_for_llm("一句话。", 400) == ["一句话。"]


def test_切块拼回原文逐字一致():
    text = "".join(PARA * 4)
    chunks = chunk_for_llm(text, 100)
    assert len(chunks) > 1
    assert "".join(chunks) == text


def test_每块不超过上限():
    text = "".join(PARA * 4)
    for c in chunk_for_llm(text, 100):
        assert len(c) <= 100, f"块超长: {len(c)} 字"


def test_只在句末切不切断句子():
    chunks = chunk_for_llm("".join(PARA * 3), 90)
    # 除最后一块，每块都该以句末标点或换行结尾
    for c in chunks[:-1]:
        assert c[-1] in "。！？!?；;…\n", f"切在了句子中间: …{c[-30:]!r}"


def test_换行保留():
    text = "第一段话在这里。\n第二段话在这里。\n第三段话在这里。"
    assert "".join(chunk_for_llm(text, 20)) == text


def test_超长单句被次级标点切开():
    text = "这是一个非常长的句子，里面有很多逗号分隔的部分，每个部分都不算短，" \
           "但整句话没有句号，所以只能靠逗号来切分它，否则一块就会撑爆输出上限。"
    chunks = chunk_for_llm(text, 30)
    assert all(len(c) <= 30 for c in chunks)
    assert "".join(chunks) == text


def test_无标点超长串也被硬切():
    text = "啊" * 250
    chunks = chunk_for_llm(text, 100)
    assert all(len(c) <= 100 for c in chunks)
    assert "".join(chunks) == text


def test_全空白返回空列表():
    assert chunk_for_llm("   \n  ", 400) == []


# ---------- 并行编排 ----------

def _echo(request):
    """把请求里的文本原样切成两段返回，模拟一个正常的 LLM。"""
    import json

    body = json.loads(request.read())
    text = body["messages"][-1]["content"]
    mid = len(text) // 2
    return httpx.Response(
        200,
        json=llm_response(
            {
                "segments": [
                    {"display_text": text[:mid], "synth_text": text[:mid]},
                    {"display_text": text[mid:], "synth_text": text[mid:]},
                ]
            }
        ),
    )


@respx.mock
async def test_长稿分块并行且拼回原文(monkeypatch):
    monkeypatch.setattr(settings, "llm_chunk_chars", 80)
    route = respx.post(URL).mock(side_effect=_echo)

    text = "".join(PARA * 4)
    segs = await preprocess(text)

    assert route.call_count > 1, "长稿应该切成多块"
    joined = "".join(s["display_text"] for s in segs)
    assert normalize_for_compare(joined) == normalize_for_compare(text)


@respx.mock
async def test_段落顺序与原文一致(monkeypatch):
    """并行返回顺序不确定，结果必须按块序拼接。"""
    monkeypatch.setattr(settings, "llm_chunk_chars", 60)
    respx.post(URL).mock(side_effect=_echo)

    text = "".join(PARA * 3)
    segs = await preprocess(text)
    assert "".join(s["display_text"] for s in segs) == text


@respx.mock
async def test_并发不超过配置(monkeypatch):
    import asyncio

    monkeypatch.setattr(settings, "llm_chunk_chars", 40)
    monkeypatch.setattr(settings, "llm_concurrency", 2)
    live = peak = 0

    async def handler(request):
        nonlocal live, peak
        live += 1
        peak = max(peak, live)
        await asyncio.sleep(0.02)
        live -= 1
        return _echo(request)

    respx.post(URL).mock(side_effect=handler)
    await preprocess("".join(PARA * 4))
    assert peak <= 2, f"并发峰值 {peak} 超过配置的 2"


@respx.mock
async def test_一块失败则整体失败(monkeypatch):
    """部分成功拼不出完整原文，必须整体失败让调用方兜底。"""
    monkeypatch.setattr(settings, "llm_chunk_chars", 60)
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        if calls["n"] == 2:
            return httpx.Response(500, text="boom")
        return _echo(request)

    respx.post(URL).mock(side_effect=handler)
    with pytest.raises(PreprocessError, match="块处理失败"):
        await preprocess("".join(PARA * 3))


@respx.mock
async def test_块内不保真则整体失败(monkeypatch):
    monkeypatch.setattr(settings, "llm_chunk_chars", 60)
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(
                200, json=llm_response({"segments": [{"display_text": "我改写了原文"}]})
            )
        return _echo(request)

    respx.post(URL).mock(side_effect=handler)
    with pytest.raises(PreprocessError):
        await preprocess("".join(PARA * 3))


@respx.mock
async def test_token截断被明确报出():
    """finish_reason=length 时 JSON 必然不完整，要报截断而不是 JSON 错误。"""
    import json as _j

    respx.post(URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {"content": _j.dumps({"segments": [{"display_text": "半"}]})},
                        "finish_reason": "length",
                    }
                ]
            },
        )
    )
    with pytest.raises(PreprocessError, match="长度上限截断"):
        await preprocess("一句话。")


@respx.mock
async def test_关推理参数被发出(monkeypatch):
    monkeypatch.setattr(settings, "llm_disable_thinking", True)
    route = respx.post(URL).mock(side_effect=_echo)
    await preprocess("一句话。")
    body = route.calls[0].request.read().decode("utf-8")
    assert '"thinking"' in body and '"disabled"' in body
    assert '"reasoning_effort":"none"' in body.replace(" ", "")


@respx.mock
async def test_可关闭关推理开关(monkeypatch):
    monkeypatch.setattr(settings, "llm_disable_thinking", False)
    route = respx.post(URL).mock(side_effect=_echo)
    await preprocess("一句话。")
    body = route.calls[0].request.read().decode("utf-8")
    assert "thinking" not in body and "reasoning_effort" not in body
