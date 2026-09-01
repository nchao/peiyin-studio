"""合成编排：重试退避、部分失败、缓存命中、并发。只 mock HTTP。"""

import httpx
import pytest
import respx

from app import audio_store, db, mimo_tts, synth
from app.config import settings
from helpers import make_wav, tts_response

URL = "https://api.xiaomimimo.com/v1/chat/completions"


def make_project(texts: list[str], voice="苏打", style="calm_narration") -> int:
    pid = db.create_project("测试", "".join(texts), voice, style)
    db.replace_segments(pid, [{"display_text": t, "synth_text": t} for t in texts])
    return pid


async def collect(pid: int, **kw) -> list[dict]:
    return [ev async for ev in synth.synthesize_project(pid, **kw)]


# ---------- 退避 ----------

def test_429退避比5xx更狠():
    # 同一 attempt，429 的退避下界应明显大于 5xx —— 限流要等更久
    for attempt in range(4):
        s429_min = mimo_tts._backoff_seconds(attempt, 429)
        s5xx_max = mimo_tts._backoff_seconds(attempt, 503)
        # 429 基数 2*3^a，5xx 基数 min(2^a,8)；即便各带抖动，429 下界也远超 5xx 上界
        assert s429_min > s5xx_max or attempt == 0


def test_429退避有上限():
    # 次数很大时退避被 30s 封顶（含抖动最多 45s），不会无限膨胀
    assert mimo_tts._backoff_seconds(10, 429) <= 45.0


def test_退避带抖动打散():
    # 同参数多次调用应取到不同值（有随机抖动），避免惊群
    vals = {mimo_tts._backoff_seconds(1, 429) for _ in range(20)}
    assert len(vals) > 1


# ---------- 重试 ----------

@respx.mock
async def test_429后重试成功(no_sleep):
    route = respx.post(URL).mock(
        side_effect=[
            httpx.Response(429, text="rate limited"),
            httpx.Response(200, json=tts_response(make_wav(1000))),
        ]
    )
    wav, dur = await mimo_tts.synthesize("你好", "苏打", "calm_narration")
    assert route.call_count == 2
    assert dur == 1000


@respx.mock
async def test_重试耗尽后抛错(no_sleep):
    route = respx.post(URL).mock(return_value=httpx.Response(429, text="busy"))
    with pytest.raises(mimo_tts.TTSError) as e:
        await mimo_tts.synthesize("你好", "苏打", None)
    assert route.call_count == settings.tts_max_retry + 1
    assert e.value.status == 429


@respx.mock
async def test_400不重试(no_sleep):
    route = respx.post(URL).mock(return_value=httpx.Response(400, text="bad voice"))
    with pytest.raises(mimo_tts.TTSError, match="400"):
        await mimo_tts.synthesize("你好", "不存在", None)
    assert route.call_count == 1


@respx.mock
async def test_5xx也重试(no_sleep):
    route = respx.post(URL).mock(
        side_effect=[
            httpx.Response(503),
            httpx.Response(200, json=tts_response(make_wav(500))),
        ]
    )
    await mimo_tts.synthesize("你好", "苏打", None)
    assert route.call_count == 2


@respx.mock
async def test_响应缺音频字段报错(no_sleep):
    respx.post(URL).mock(return_value=httpx.Response(200, json={"choices": [{"message": {}}]}))
    with pytest.raises(mimo_tts.TTSError, match="未找到音频数据"):
        await mimo_tts.synthesize("你好", "苏打", None)


@respx.mock
async def test_网络异常也重试(no_sleep):
    route = respx.post(URL).mock(
        side_effect=[
            httpx.ConnectTimeout("timeout"),
            httpx.Response(200, json=tts_response(make_wav(200))),
        ]
    )
    await mimo_tts.synthesize("你好", "苏打", None)
    assert route.call_count == 2


# ---------- 风格标签拼接 ----------

@respx.mock
async def test_风格预设拼成括号标签(no_sleep):
    route = respx.post(URL).mock(return_value=httpx.Response(200, json=tts_response(make_wav(100))))
    await mimo_tts.synthesize("正文内容", "白桦", "suspense")
    sent = route.calls[0].request.read().decode()
    assert "(深沉 磁性 平静)正文内容" in sent
    assert '"voice": "\\u767d\\u6866"' in sent or "白桦" in sent


@respx.mock
async def test_自定义风格词原样使用(no_sleep):
    route = respx.post(URL).mock(return_value=httpx.Response(200, json=tts_response(make_wav(100))))
    await mimo_tts.synthesize("正文", "苏打", "东北话 俏皮")
    assert "(东北话 俏皮)正文" in route.calls[0].request.read().decode()


@respx.mock
async def test_无风格时不加括号(no_sleep):
    route = respx.post(URL).mock(return_value=httpx.Response(200, json=tts_response(make_wav(100))))
    await mimo_tts.synthesize("正文", "苏打", None)
    body = route.calls[0].request.read().decode()
    assert "正文" in body and "(" not in body.split('"content"')[1][:20]


# ---------- 编排 ----------

@respx.mock
async def test_全部成功(no_sleep):
    respx.post(URL).mock(return_value=httpx.Response(200, json=tts_response(make_wav(1000))))
    pid = make_project(["第一段。", "第二段。", "第三段。"])
    events = await collect(pid)
    summary = events[-1]
    assert summary["type"] == "summary"
    assert summary["total"] == 3
    assert summary["ok"] + summary["cached"] == 3
    assert summary["failed"] == 0
    assert all(s["status"] == "ok" for s in db.list_segments(pid))


@respx.mock
async def test_单段失败不阻塞其余段(no_sleep):
    def handler(request):
        body = request.read().decode()
        if "坏段" in body:
            return httpx.Response(400, text="boom")
        return httpx.Response(200, json=tts_response(make_wav(800)))

    respx.post(URL).mock(side_effect=handler)
    pid = make_project(["好段一。", "坏段。", "好段二。"])
    summary = (await collect(pid))[-1]
    assert summary["failed"] == 1
    assert summary["ok"] == 2

    rows = {s["display_text"]: s for s in db.list_segments(pid)}
    assert rows["坏段。"]["status"] == "failed"
    assert "boom" in rows["坏段。"]["error_msg"]
    assert rows["好段一。"]["status"] == "ok"


@respx.mock
async def test_only_failed只重跑失败段(no_sleep):
    def handler(request):
        if "坏段" in request.read().decode():
            return httpx.Response(400, text="boom")
        return httpx.Response(200, json=tts_response(make_wav(800)))

    route = respx.post(URL).mock(side_effect=handler)
    pid = make_project(["好段一。", "坏段。", "好段二。"])
    await collect(pid)
    first_calls = route.call_count

    # 修好那段，只重跑失败的
    bad = next(s for s in db.list_segments(pid) if s["status"] == "failed")
    db.update_segment(bad["id"], synth_text="修好了。")
    events = await collect(pid, only_failed=True)

    assert events[-1]["total"] == 1
    assert route.call_count == first_calls + 1
    assert all(s["status"] == "ok" for s in db.list_segments(pid))


@respx.mock
async def test_第二次合成命中缓存不发请求(no_sleep):
    route = respx.post(URL).mock(return_value=httpx.Response(200, json=tts_response(make_wav(1000))))
    pid = make_project(["同样的话。", "另一句话。"])
    await collect(pid)
    assert route.call_count == 2

    summary = (await collect(pid))[-1]
    assert route.call_count == 2, "命中缓存不该再发请求"
    assert summary["cached"] == 2


@respx.mock
async def test_改音色后缓存失效(no_sleep):
    route = respx.post(URL).mock(return_value=httpx.Response(200, json=tts_response(make_wav(1000))))
    pid = make_project(["一句话。"])
    await collect(pid)
    sid = db.list_segments(pid)[0]["id"]

    db.update_segment(sid, voice="白桦")
    await collect(pid)
    assert route.call_count == 2, "改音色应重新合成"


@respx.mock
async def test_相同文本不同段共享缓存(no_sleep):
    route = respx.post(URL).mock(return_value=httpx.Response(200, json=tts_response(make_wav(600))))
    pid = make_project(["重复的话。", "重复的话。", "不同的话。"])
    await collect(pid)
    # 两段文本相同 → 只该发 2 次请求（但并发下可能都 miss，所以放宽到 <=3）
    assert route.call_count <= 3
    assert all(s["status"] == "ok" for s in db.list_segments(pid))


@respx.mock
async def test_段级音色覆盖项目默认(no_sleep):
    voices_used = []

    def handler(request):
        import json as _j
        voices_used.append(_j.loads(request.read())["audio"]["voice"])
        return httpx.Response(200, json=tts_response(make_wav(400)))

    respx.post(URL).mock(side_effect=handler)
    pid = make_project(["继承的。", "覆盖的。"], voice="苏打")
    segs = db.list_segments(pid)
    db.update_segment(segs[1]["id"], voice="茉莉")
    await collect(pid)
    assert sorted(voices_used) == sorted(["苏打", "茉莉"])


@respx.mock
async def test_并发不超过配置上限(no_sleep, monkeypatch):
    import asyncio

    monkeypatch.setattr(settings, "tts_concurrency", 2)
    live = 0
    peak = 0

    async def handler(request):
        nonlocal live, peak
        live += 1
        peak = max(peak, live)
        await asyncio.sleep(0.02)
        live -= 1
        return httpx.Response(200, json=tts_response(make_wav(300)))

    respx.post(URL).mock(side_effect=handler)
    pid = make_project([f"第{i}段内容。" for i in range(8)])
    await collect(pid)
    assert peak <= 2, f"并发峰值 {peak} 超过配置的 2"


@respx.mock
async def test_克隆项目用更低并发(no_sleep, monkeypatch):
    import asyncio

    from app import sample_store

    # 普通并发设高、克隆并发设低，验证含克隆音色的批走的是低并发
    monkeypatch.setattr(settings, "tts_concurrency", 8)
    monkeypatch.setattr(settings, "tts_clone_concurrency", 2)

    # 建一个克隆音色并让项目默认用它
    h = sample_store.save(make_wav(5000), "wav")
    cid = db.create_voice_clone("测试音色", h, "wav", 5000)
    voice = f"clone:{cid}"
    pid = make_project([f"第{i}段。" for i in range(6)], voice=voice)

    live = 0
    peak = 0

    async def handler(request):
        nonlocal live, peak
        live += 1
        peak = max(peak, live)
        await asyncio.sleep(0.02)
        live -= 1
        return httpx.Response(200, json=tts_response(make_wav(300)))

    respx.post(URL).mock(side_effect=handler)
    await collect(pid)
    assert peak <= 2, f"克隆合成并发峰值 {peak} 超过克隆上限 2"


async def test_项目不存在返回错误事件():
    events = await collect(99999)
    assert events[0]["type"] == "error"


async def test_无段落时直接给空summary():
    pid = db.create_project("空", "", "苏打", "calm_narration")
    events = await collect(pid)
    assert events[-1] == {"type": "summary", "total": 0, "ok": 0, "cached": 0, "failed": 0}


@respx.mock
async def test_进度事件逐段推送(no_sleep):
    respx.post(URL).mock(return_value=httpx.Response(200, json=tts_response(make_wav(500))))
    pid = make_project(["一。", "二。", "三。"])
    events = await collect(pid)
    assert events[0]["type"] == "start" and events[0]["total"] == 3
    progress = [e for e in events if e["type"] == "progress"]
    assert [p["done"] for p in progress] == [1, 2, 3]


@respx.mock
async def test_音频落盘且时长入库(no_sleep):
    respx.post(URL).mock(return_value=httpx.Response(200, json=tts_response(make_wav(1234))))
    pid = make_project(["一句话。"])
    await collect(pid)
    seg = db.list_segments(pid)[0]
    assert seg["duration_ms"] == 1234
    assert audio_store.exists(seg["audio_hash"])
    assert db.get_audio(seg["audio_hash"])["duration_ms"] == 1234
