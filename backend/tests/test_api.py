"""API 层集成测试：真实 SQLite、真实 ffmpeg，只 mock 外部 HTTP。"""

import json

import httpx
import pytest
import respx
from fastapi.testclient import TestClient

from app import db
from app.main import app
from helpers import llm_response, make_wav, tts_response

TTS_URL = "https://api.xiaomimimo.com/v1/chat/completions"


@pytest.fixture
def client():
    with TestClient(app) as c:
        yield c


def new_project(client, raw="他在2026年买了电脑。价格三万五。") -> int:
    r = client.post("/api/projects", json={"name": "解说稿", "raw_text": raw})
    assert r.status_code == 200
    return r.json()["id"]


# ---------- 元数据 ----------

def test_meta给出音色和语气清单(client):
    m = client.get("/api/meta").json()
    assert [v["id"] for v in m["voices"]] == ["苏打", "白桦", "冰糖", "茉莉"]
    assert "suspense" in [s["id"] for s in m["styles"]]
    assert m["tts_model"] == "mimo-v2.5-tts"


# ---------- 项目 CRUD ----------

def test_项目创建与查询(client):
    pid = new_project(client)
    d = client.get(f"/api/projects/{pid}").json()
    assert d["project"]["name"] == "解说稿"
    assert d["segments"] == []


def test_未知音色被拒(client):
    r = client.post("/api/projects", json={"name": "x", "default_voice": "不存在"})
    assert r.status_code == 400


def test_项目不存在返回404(client):
    assert client.get("/api/projects/99999").status_code == 404


def test_修改项目默认语气(client):
    pid = new_project(client)
    r = client.patch(f"/api/projects/{pid}", json={"default_style": "suspense"})
    assert r.json()["default_style"] == "suspense"


# ---------- 分段 ----------

def test_规则分段保真(client):
    raw = "第一句话在这里。第二句话稍微长一点点。\n第三句换行了。"
    pid = new_project(client, raw)
    segs = client.post(f"/api/projects/{pid}/split").json()["segments"]
    joined = "".join(s["display_text"] for s in segs)
    assert joined.replace(" ", "") == raw.replace("\n", "").replace(" ", "")
    assert all(s["status"] == "pending" for s in segs)


def test_原文为空时分段报错(client):
    pid = new_project(client, "")
    assert client.post(f"/api/projects/{pid}/split").status_code == 400


@respx.mock
def test_llm预处理成功落库(client):
    respx.post("https://api.xiaomimimo.com/v1/chat/completions").mock(
        return_value=httpx.Response(
            200,
            json=llm_response(
                {
                    "segments": [
                        {
                            "display_text": "他在2026年买了电脑。",
                            "synth_text": "他在二零二六年买了电脑。",
                            "style": "calm_narration",
                            "pause_after_ms": 300,
                        },
                        {
                            "display_text": "价格三万五。",
                            "synth_text": "价格三万五。[叹气]",
                            "style": "sarcastic",
                            "pause_after_ms": 0,
                        },
                    ]
                }
            ),
        )
    )
    pid = new_project(client)
    body = client.post(f"/api/projects/{pid}/preprocess").json()
    assert body["mode"] == "llm"
    assert body["warning"] is None
    segs = body["segments"]
    assert segs[0]["synth_text"] == "他在二零二六年买了电脑。"
    assert segs[0]["display_text"] == "他在2026年买了电脑。"
    assert segs[0]["pause_after_ms"] == 300
    assert segs[1]["style"] == "sarcastic"


@respx.mock
def test_llm改了原文时退回规则分段并警告(client):
    respx.post("https://api.xiaomimimo.com/v1/chat/completions").mock(
        return_value=httpx.Response(
            200,
            json=llm_response(
                {"segments": [{"display_text": "他买了一台崭新的电脑。", "synth_text": "x"}]}
            ),
        )
    )
    pid = new_project(client)
    body = client.post(f"/api/projects/{pid}/preprocess").json()
    assert body["mode"] == "rule"
    assert "LLM 未生效" in body["warning"]
    assert "改动了原文" in body["warning"]
    # 兜底结果必须保真
    joined = "".join(s["display_text"] for s in body["segments"])
    assert joined == "他在2026年买了电脑。价格三万五。"


@respx.mock
def test_llm挂了且禁用兜底时返回502(client):
    respx.post("https://api.xiaomimimo.com/v1/chat/completions").mock(
        return_value=httpx.Response(500, text="server down")
    )
    pid = new_project(client)
    r = client.post(f"/api/projects/{pid}/preprocess?fallback=false")
    assert r.status_code == 502


@respx.mock
def test_llm输出非json时兜底(client):
    respx.post("https://api.xiaomimimo.com/v1/chat/completions").mock(
        return_value=httpx.Response(
            200, json={"choices": [{"message": {"content": "这不是 JSON"}}]}
        )
    )
    pid = new_project(client)
    body = client.post(f"/api/projects/{pid}/preprocess").json()
    assert body["mode"] == "rule"


@respx.mock
def test_llm用代码围栏包裹也能解析(client):
    payload = json.dumps(
        {"segments": [{"display_text": "他在2026年买了电脑。价格三万五。", "synth_text": "读法"}]},
        ensure_ascii=False,
    )
    respx.post("https://api.xiaomimimo.com/v1/chat/completions").mock(
        return_value=httpx.Response(
            200, json={"choices": [{"message": {"content": f"```json\n{payload}\n```"}}]}
        )
    )
    pid = new_project(client)
    assert client.post(f"/api/projects/{pid}/preprocess").json()["mode"] == "llm"


# ---------- 段落编辑 ----------

def test_手工替换段落列表(client):
    pid = new_project(client)
    r = client.put(
        f"/api/projects/{pid}/segments",
        json={
            "segments": [
                {"display_text": "手写第一段", "voice": "白桦", "style": "suspense"},
                {"display_text": "手写第二段", "pause_after_ms": 400},
            ]
        },
    )
    segs = r.json()["segments"]
    assert segs[0]["voice"] == "白桦"
    assert segs[1]["synth_text"] == "手写第二段"  # 缺省时回退 display
    assert segs[1]["pause_after_ms"] == 400


def test_空段落列表被拒(client):
    pid = new_project(client)
    r = client.put(f"/api/projects/{pid}/segments", json={"segments": [{"display_text": "  "}]})
    assert r.status_code == 400


def test_改段落音色返回更新后的行(client):
    pid = new_project(client)
    client.post(f"/api/projects/{pid}/split")
    sid = client.get(f"/api/projects/{pid}").json()["segments"][0]["id"]
    r = client.patch(f"/api/segments/{sid}", json={"voice": "茉莉"})
    assert r.json()["voice"] == "茉莉"


def test_段落停顿超范围被拒(client):
    pid = new_project(client)
    client.post(f"/api/projects/{pid}/split")
    sid = client.get(f"/api/projects/{pid}").json()["segments"][0]["id"]
    assert client.patch(f"/api/segments/{sid}", json={"pause_after_ms": 99999}).status_code == 422


# ---------- 合成与导出 ----------

@respx.mock
def test_合成SSE推进度(client):
    respx.post(TTS_URL).mock(return_value=httpx.Response(200, json=tts_response(make_wav(1000))))
    pid = new_project(client)
    client.post(f"/api/projects/{pid}/split")

    with client.stream("POST", f"/api/projects/{pid}/synthesize") as r:
        assert r.status_code == 200
        events = [
            json.loads(line[6:])
            for line in r.iter_lines()
            if line.startswith("data: ")
        ]
    assert events[0]["type"] == "start"
    assert events[-1]["type"] == "summary"
    assert events[-1]["failed"] == 0


@respx.mock
def test_导出mp3和srt(client):
    respx.post(TTS_URL).mock(return_value=httpx.Response(200, json=tts_response(make_wav(1500))))
    pid = new_project(client)
    client.post(f"/api/projects/{pid}/split")
    with client.stream("POST", f"/api/projects/{pid}/synthesize") as r:
        list(r.iter_lines())

    mp3 = client.get(f"/api/projects/{pid}/export?fmt=mp3")
    assert mp3.status_code == 200
    assert mp3.headers["content-type"] == "audio/mpeg"
    assert len(mp3.content) > 500

    wav = client.get(f"/api/projects/{pid}/export?fmt=wav")
    assert wav.content[:4] == b"RIFF"

    srt = client.get(f"/api/projects/{pid}/srt")
    assert srt.status_code == 200
    assert "00:00:00,000 --> 00:00:01,500" in srt.text
    assert "%E8%A7%A3%E8%AF%B4%E7%A8%BF.srt" in srt.headers["content-disposition"]


def test_未合成时导出报错(client):
    pid = new_project(client)
    client.post(f"/api/projects/{pid}/split")
    assert client.get(f"/api/projects/{pid}/export").status_code == 400
    assert client.get(f"/api/projects/{pid}/srt").status_code == 400


def test_非法导出格式被拒(client):
    pid = new_project(client)
    assert client.get(f"/api/projects/{pid}/export?fmt=flac").status_code == 400


@respx.mock
def test_单段试听(client):
    respx.post(TTS_URL).mock(return_value=httpx.Response(200, json=tts_response(make_wav(700))))
    r = client.post("/api/preview", json={"text": "试听一下", "voice": "冰糖", "style": "lively"})
    assert r.status_code == 200
    assert r.headers["x-duration-ms"] == "700"
    assert r.content[:4] == b"RIFF"


@respx.mock
def test_试听失败返回502(client):
    respx.post(TTS_URL).mock(return_value=httpx.Response(400, text="bad"))
    r = client.post("/api/preview", json={"text": "试听", "voice": "冰糖"})
    assert r.status_code == 502


@respx.mock
def test_取单段音频(client):
    respx.post(TTS_URL).mock(return_value=httpx.Response(200, json=tts_response(make_wav(900))))
    pid = new_project(client)
    client.post(f"/api/projects/{pid}/split")
    with client.stream("POST", f"/api/projects/{pid}/synthesize") as r:
        list(r.iter_lines())
    sid = client.get(f"/api/projects/{pid}").json()["segments"][0]["id"]
    assert client.get(f"/api/segments/{sid}/audio").content[:4] == b"RIFF"


def test_段落无音频时取音频404(client):
    pid = new_project(client)
    client.post(f"/api/projects/{pid}/split")
    sid = client.get(f"/api/projects/{pid}").json()["segments"][0]["id"]
    assert client.get(f"/api/segments/{sid}/audio").status_code == 404


@respx.mock
def test_删项目清理孤儿音频(client):
    respx.post(TTS_URL).mock(return_value=httpx.Response(200, json=tts_response(make_wav(600))))
    pid = new_project(client)
    client.post(f"/api/projects/{pid}/split")
    with client.stream("POST", f"/api/projects/{pid}/synthesize") as r:
        list(r.iter_lines())
    body = client.delete(f"/api/projects/{pid}").json()
    assert body["deleted"] is True
    assert body["audio_files_purged"] >= 1
    assert db.get_project(pid) is None
