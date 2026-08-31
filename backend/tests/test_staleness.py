"""音频过期检测：改项目级音色/语气后，继承它的段落音频失效。

这是曾经的 bug —— db.update_project 不像 update_segment 那样清状态，
导致改音色后段落 status 仍是 ok、界面显示已合成、导出的却是旧音色。
现在新鲜度由哈希算出，不依赖 status 字段。
"""

import httpx
import pytest
import respx
from fastapi.testclient import TestClient

from app import db
from app.main import app
from app.synth import is_fresh, expected_hash
from helpers import make_wav, tts_response

TTS_URL = "https://api.xiaomimimo.com/v1/chat/completions"


@pytest.fixture
def client():
    with TestClient(app) as c:
        yield c


def synthesized(client, voice="苏打", style="calm_narration", texts=("第一段。", "第二段。")):
    r = client.post(
        "/api/projects",
        json={"name": "过期测试", "raw_text": "".join(texts),
              "default_voice": voice, "default_style": style},
    )
    pid = r.json()["id"]
    client.put(
        f"/api/projects/{pid}/segments",
        json={"segments": [{"display_text": t} for t in texts]},
    )
    with client.stream("POST", f"/api/projects/{pid}/synthesize") as s:
        list(s.iter_lines())
    return pid


# ---------- 新鲜度判定（纯函数） ----------

def test_合成后段落新鲜():
    from app import audio_store
    project = {"default_voice": "苏打", "default_style": "calm_narration"}
    seg = {"synth_text": "你好", "voice": None, "style": None,
           "audio_hash": None, "status": "pending"}
    h = expected_hash(seg, project)
    audio_store.save(h, make_wav(500))
    seg["audio_hash"] = h
    assert is_fresh(seg, project)


def test_改项目音色后段落过期():
    from app import audio_store
    project = {"default_voice": "苏打", "default_style": "calm_narration"}
    seg = {"synth_text": "你好", "voice": None, "style": None, "audio_hash": None}
    h = expected_hash(seg, project)
    audio_store.save(h, make_wav(500))
    seg["audio_hash"] = h
    assert is_fresh(seg, project)

    project["default_voice"] = "白桦"  # 改音色
    assert not is_fresh(seg, project), "改音色后应过期"


def test_段级音色不受项目改动影响():
    from app import audio_store
    project = {"default_voice": "苏打", "default_style": "calm_narration"}
    seg = {"synth_text": "你好", "voice": "茉莉", "style": "lively", "audio_hash": None}
    h = expected_hash(seg, project)
    audio_store.save(h, make_wav(500))
    seg["audio_hash"] = h

    project["default_voice"] = "白桦"  # 改项目默认
    assert is_fresh(seg, project), "段落有自己的音色，不该受项目改动影响"


def test_文件丢失则不新鲜():
    project = {"default_voice": "苏打", "default_style": "calm_narration"}
    seg = {"synth_text": "你好", "voice": None, "style": None,
           "audio_hash": "deadbeef" * 8}
    assert not is_fresh(seg, project), "哈希对但文件不存在，也不算新鲜"


# ---------- API 层 ----------

@respx.mock
def test_改项目音色后段落标记为过期(client):
    respx.post(TTS_URL).mock(return_value=httpx.Response(200, json=tts_response(make_wav(1000))))
    pid = synthesized(client)

    before = client.get(f"/api/projects/{pid}").json()["segments"]
    assert all(s["fresh"] for s in before), "刚合成应全部新鲜"

    client.patch(f"/api/projects/{pid}", json={"default_voice": "白桦"})
    after = client.get(f"/api/projects/{pid}").json()["segments"]
    assert all(not s["fresh"] for s in after), "改音色后应全部过期"
    assert all(s["status"] == "ok" for s in after), "status 仍是 ok（音频还在，只是过期）"


@respx.mock
def test_有过期段时导出被拒(client):
    respx.post(TTS_URL).mock(return_value=httpx.Response(200, json=tts_response(make_wav(1000))))
    pid = synthesized(client)
    client.patch(f"/api/projects/{pid}", json={"default_style": "suspense"})

    r = client.get(f"/api/projects/{pid}/export?fmt=mp3")
    assert r.status_code == 409
    assert "不一致" in r.json()["detail"]

    assert client.get(f"/api/projects/{pid}/srt").status_code == 409
    assert client.get(f"/api/projects/{pid}/preview").status_code == 409


@respx.mock
def test_重新合成后过期段恢复新鲜可导出(client):
    respx.post(TTS_URL).mock(return_value=httpx.Response(200, json=tts_response(make_wav(1000))))
    pid = synthesized(client)
    client.patch(f"/api/projects/{pid}", json={"default_voice": "白桦"})
    assert client.get(f"/api/projects/{pid}/export?fmt=mp3").status_code == 409

    # 重新合成
    with client.stream("POST", f"/api/projects/{pid}/synthesize") as s:
        list(s.iter_lines())

    assert all(s["fresh"] for s in client.get(f"/api/projects/{pid}").json()["segments"])
    assert client.get(f"/api/projects/{pid}/export?fmt=mp3").status_code == 200


@respx.mock
def test_only_failed重合成覆盖过期段(client):
    """改音色后段落 status 是 ok 但过期，only_failed 也该把它们重跑。"""
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        return httpx.Response(200, json=tts_response(make_wav(1000)))

    respx.post(TTS_URL).mock(side_effect=handler)
    pid = synthesized(client)  # 2 段，calls=2
    client.patch(f"/api/projects/{pid}", json={"default_voice": "白桦"})

    before = calls["n"]
    with client.stream("POST", f"/api/projects/{pid}/synthesize?only_failed=true") as s:
        list(s.iter_lines())
    assert calls["n"] == before + 2, "两段过期段都该被重合成"


@respx.mock
def test_effective字段随项目改动更新(client):
    respx.post(TTS_URL).mock(return_value=httpx.Response(200, json=tts_response(make_wav(1000))))
    pid = synthesized(client, voice="苏打")
    segs = client.get(f"/api/projects/{pid}").json()["segments"]
    assert all(s["effective_voice"] == "苏打" for s in segs)

    client.patch(f"/api/projects/{pid}", json={"default_voice": "白桦"})
    segs = client.get(f"/api/projects/{pid}").json()["segments"]
    assert all(s["effective_voice"] == "白桦" for s in segs)
