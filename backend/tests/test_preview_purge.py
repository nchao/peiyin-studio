"""全篇试听端点，以及合成收尾的孤儿音频清理。"""

import json

import httpx
import pytest
import respx
from fastapi.testclient import TestClient

from app import audio_store, db
from app.main import app
from helpers import make_wav, tts_response

TTS_URL = "https://api.xiaomimimo.com/v1/chat/completions"


@pytest.fixture
def client():
    with TestClient(app) as c:
        yield c


def prepared(client, texts=("第一段。", "第二段。")) -> int:
    """建一个已合成完的项目。"""
    r = client.post("/api/projects", json={"name": "试听项目", "raw_text": "".join(texts)})
    pid = r.json()["id"]
    client.put(
        f"/api/projects/{pid}/segments",
        json={"segments": [{"display_text": t} for t in texts]},
    )
    with client.stream("POST", f"/api/projects/{pid}/synthesize") as s:
        list(s.iter_lines())
    return pid


# ---------- 全篇试听 ----------

@respx.mock
def test_全篇试听内联返回而非下载(client):
    respx.post(TTS_URL).mock(return_value=httpx.Response(200, json=tts_response(make_wav(1000))))
    pid = prepared(client)
    r = client.get(f"/api/projects/{pid}/preview")
    assert r.status_code == 200
    assert r.headers["content-type"] == "audio/wav"
    # 关键区别：不能带 attachment，否则浏览器会下载而不是播放
    assert "content-disposition" not in {k.lower() for k in r.headers}
    assert r.content[:4] == b"RIFF"


@respx.mock
def test_全篇试听时长等于各段之和(client):
    respx.post(TTS_URL).mock(return_value=httpx.Response(200, json=tts_response(make_wav(1000))))
    pid = prepared(client, ("甲。", "乙。", "丙。"))
    from app.wavutil import duration_ms_of

    assert duration_ms_of(client.get(f"/api/projects/{pid}/preview").content) == 3000


@respx.mock
def test_全篇试听含段间停顿(client):
    respx.post(TTS_URL).mock(return_value=httpx.Response(200, json=tts_response(make_wav(1000))))
    pid = prepared(client)
    sid = client.get(f"/api/projects/{pid}").json()["segments"][0]["id"]
    client.patch(f"/api/segments/{sid}", json={"pause_after_ms": 500})
    from app.wavutil import duration_ms_of

    assert duration_ms_of(client.get(f"/api/projects/{pid}/preview").content) == 2500


def test_未合成时试听报错(client):
    r = client.post("/api/projects", json={"name": "空", "raw_text": "还没合成。"})
    pid = r.json()["id"]
    client.post(f"/api/projects/{pid}/split")
    assert client.get(f"/api/projects/{pid}/preview").status_code == 400


def test_项目不存在时试听404(client):
    assert client.get("/api/projects/99999/preview").status_code == 404


# ---------- 孤儿清理 ----------

@respx.mock
def test_合成收尾清理孤儿音频(client):
    respx.post(TTS_URL).mock(return_value=httpx.Response(200, json=tts_response(make_wav(800))))
    pid = prepared(client)
    before = len(list(audio_store.settings.audio_dir.glob("*.wav")))

    # 改文本 → 旧音频失去引用，成为孤儿
    sid = client.get(f"/api/projects/{pid}").json()["segments"][0]["id"]
    client.patch(f"/api/segments/{sid}", json={"synth_text": "改成别的话。"})

    with client.stream("POST", f"/api/projects/{pid}/synthesize") as s:
        events = [
            json.loads(l[6:]) for l in s.iter_lines() if l.startswith("data: ")
        ]

    purged = [e for e in events if e["type"] == "purged"]
    assert purged and purged[0]["files"] >= 1, "改文本后旧音频应被清掉"
    after = len(list(audio_store.settings.audio_dir.glob("*.wav")))
    assert after <= before, f"清理后文件数不该增加: {before} → {after}"


@respx.mock
def test_没有孤儿时不发purged事件(client):
    respx.post(TTS_URL).mock(return_value=httpx.Response(200, json=tts_response(make_wav(800))))
    pid = prepared(client)
    with client.stream("POST", f"/api/projects/{pid}/synthesize") as s:
        events = [
            json.loads(l[6:]) for l in s.iter_lines() if l.startswith("data: ")
        ]
    assert not [e for e in events if e["type"] == "purged"]


@respx.mock
def test_清理不误删在用音频(client):
    respx.post(TTS_URL).mock(return_value=httpx.Response(200, json=tts_response(make_wav(800))))
    pid_a = prepared(client, ("甲项目的话。",))
    pid_b = prepared(client, ("乙项目的话。",))

    hash_a = db.list_segments(pid_a)[0]["audio_hash"]
    with client.stream("POST", f"/api/projects/{pid_b}/synthesize") as s:
        list(s.iter_lines())

    assert audio_store.exists(hash_a), "清理不该动别的项目的音频"
