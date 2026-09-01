"""字幕导入、单段重合成、时间轴导出、克隆重命名的 API 层集成测试。"""

import json

import httpx
import respx
import pytest
from fastapi.testclient import TestClient

from app.main import app

TTS_URL = "https://api.xiaomimimo.com/v1/chat/completions"

from helpers import make_wav, tts_response  # noqa: E402


@pytest.fixture
def client():
    with TestClient(app) as c:
        yield c


def _new(client):
    return client.post("/api/projects", json={"name": "配音"}).json()["id"]


SRT = """1
00:00:01,000 --> 00:00:04,000
第一句台词

2
00:00:04,000 --> 00:00:07,500
第二句台词
"""


def test_导入srt进入时间轴模式(client):
    pid = _new(client)
    r = client.post(f"/api/projects/{pid}/import-srt", json={"content": SRT})
    assert r.status_code == 200
    body = r.json()
    assert body["mode"] == "srt" and body["count"] == 2
    segs = body["segments"]
    assert segs[0]["start_ms"] == 1000 and segs[0]["end_ms"] == 4000
    assert segs[0]["display_text"] == "第一句台词"
    # 详情接口标记 timeline=True
    d = client.get(f"/api/projects/{pid}").json()
    assert d["timeline"] is True


def test_导入坏srt报400(client):
    pid = _new(client)
    r = client.post(f"/api/projects/{pid}/import-srt", json={"content": "不是字幕"})
    assert r.status_code == 400


@respx.mock
def test_单段重合成(client):
    respx.post(TTS_URL).mock(return_value=httpx.Response(200, json=tts_response(make_wav(800))))
    pid = _new(client)
    client.post(f"/api/projects/{pid}/import-srt", json={"content": SRT})
    sid = client.get(f"/api/projects/{pid}").json()["segments"][0]["id"]

    r = client.post(f"/api/segments/{sid}/synthesize")
    assert r.status_code == 200
    body = r.json()
    assert body["result"]["status"] in ("ok", "cached")
    assert body["segment"]["status"] == "ok"
    assert body["segment"]["duration_ms"] == 800


def test_单段合成段不存在返回404(client):
    assert client.post("/api/segments/99999/synthesize").status_code == 404


@respx.mock
def test_时间轴模式导出用原始时间戳(client):
    respx.post(TTS_URL).mock(return_value=httpx.Response(200, json=tts_response(make_wav(1000))))
    pid = _new(client)
    client.post(f"/api/projects/{pid}/import-srt", json={"content": SRT})
    with client.stream("POST", f"/api/projects/{pid}/synthesize") as r:
        list(r.iter_lines())

    srt = client.get(f"/api/projects/{pid}/srt")
    assert srt.status_code == 200
    # 用导入的原始时间戳，不是按 1000ms 音频累加
    assert "00:00:01,000 --> 00:00:04,000" in srt.text
    assert "00:00:04,000 --> 00:00:07,500" in srt.text

    wav = client.get(f"/api/projects/{pid}/export?fmt=wav")
    assert wav.status_code == 200 and wav.content[:4] == b"RIFF"


@respx.mock
def test_溢出段在segments里带overflow(client):
    # 音频 5000ms，第一段窗口只有 3000ms → overflow 2000
    respx.post(TTS_URL).mock(return_value=httpx.Response(200, json=tts_response(make_wav(5000))))
    pid = _new(client)
    client.post(f"/api/projects/{pid}/import-srt", json={"content": SRT})
    with client.stream("POST", f"/api/projects/{pid}/synthesize") as r:
        list(r.iter_lines())
    segs = client.get(f"/api/projects/{pid}").json()["segments"]
    assert segs[0]["window_ms"] == 3000
    assert segs[0]["overflow_ms"] == 2000


def test_克隆重命名(client, monkeypatch):
    # 直接建一条克隆记录，不走上传（上传需真实音频探测）
    from app import db
    cid = db.create_voice_clone("旧名", "hash123", "wav", 5000)
    r = client.patch(f"/api/voice-clones/{cid}", json={"name": "新名"})
    assert r.status_code == 200
    assert r.json()["name"] == "新名"
    assert db.get_voice_clone(cid)["name"] == "新名"


def test_克隆重命名不存在返回404(client):
    assert client.patch("/api/voice-clones/99999", json={"name": "x"}).status_code == 404
