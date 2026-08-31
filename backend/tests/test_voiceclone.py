"""声音克隆音色：上传、合成分叉、缓存、引用检查。"""

import base64
import json

import httpx
import pytest
import respx
from fastapi.testclient import TestClient

from app import db, sample_store, synth
from app.main import app
from helpers import make_wav, tts_response

TTS_URL = "https://api.xiaomimimo.com/v1/chat/completions"


@pytest.fixture
def client():
    with TestClient(app) as c:
        yield c


def upload_clone(client, name="孙悟空", ms=5000):
    """上传一个克隆音色，返回其 dict（含 voice=clone:<id>）。"""
    wav = make_wav(ms)
    r = client.post(
        "/api/voice-clones",
        files={"file": ("sample.wav", wav, "audio/wav")},
        data={"name": name},
    )
    assert r.status_code == 200, r.text
    return r.json()


# ---------- 上传 ----------

def test_上传wav建立克隆音色(client):
    c = upload_clone(client)
    assert c["name"] == "孙悟空"
    assert c["voice"].startswith("clone:")
    # 列表能查到
    lst = client.get("/api/voice-clones").json()
    assert any(x["id"] == c["id"] for x in lst)
    # 样本落盘到 samples 目录（与合成缓存隔离）
    row = db.get_voice_clone(c["id"])
    assert sample_store.exists(row["sample_hash"], row["sample_ext"])


def test_非法格式被拒(client):
    r = client.post(
        "/api/voice-clones",
        files={"file": ("bad.txt", b"not audio", "text/plain")},
        data={"name": "x"},
    )
    assert r.status_code == 400


def test_空名字被拒(client):
    wav = make_wav(5000)
    r = client.post(
        "/api/voice-clones",
        files={"file": ("s.wav", wav, "audio/wav")},
        data={"name": "  "},
    )
    assert r.status_code == 400


# ---------- 合成分叉 ----------

@respx.mock
def test_段落用克隆音色合成走voiceclone模型(client):
    route = respx.post(TTS_URL).mock(
        return_value=httpx.Response(200, json=tts_response(make_wav(1200))))
    clone = upload_clone(client)

    pid = client.post("/api/projects", json={
        "name": "西游解说", "raw_text": "话说那猴王出世。",
        "default_voice": clone["voice"], "default_style": "lively",
    }).json()["id"]
    client.post(f"/api/projects/{pid}/split")

    r = client.post(f"/api/projects/{pid}/synthesize?only_failed=false")
    assert r.status_code == 200
    assert "summary" in r.text

    # 校验发出去的请求体：voiceclone 模型 + DataURL
    sent = json.loads(route.calls.last.request.content)
    assert sent["model"] == "mimo-v2.5-tts-voiceclone"
    assert sent["audio"]["voice"].startswith("data:audio/wav;base64,")


@respx.mock
def test_克隆音色命中缓存不重复请求(client):
    route = respx.post(TTS_URL).mock(
        return_value=httpx.Response(200, json=tts_response(make_wav(1000))))
    clone = upload_clone(client)
    pid = client.post("/api/projects", json={
        "name": "p", "raw_text": "一句话测试。",
        "default_voice": clone["voice"], "default_style": "calm_narration",
    }).json()["id"]
    client.post(f"/api/projects/{pid}/split")

    client.post(f"/api/projects/{pid}/synthesize")
    n1 = route.call_count
    client.post(f"/api/projects/{pid}/synthesize")  # 再合成一次
    assert route.call_count == n1  # 命中缓存，没新增请求


@respx.mock
def test_删除克隆音色后合成报明确错误(client):
    respx.post(TTS_URL).mock(
        return_value=httpx.Response(200, json=tts_response(make_wav(1000))))
    clone = upload_clone(client)
    pid = client.post("/api/projects", json={
        "name": "p", "raw_text": "测试句子。",
        "default_voice": clone["voice"], "default_style": "calm_narration",
    }).json()["id"]
    client.post(f"/api/projects/{pid}/split")
    # 强删（无视引用）
    client.delete(f"/api/voice-clones/{clone['id']}?force=true")

    r = client.post(f"/api/projects/{pid}/synthesize")
    assert "已删除" in r.text  # 段落失败信息里带明确原因


# ---------- 缓存 key 用样本哈希 ----------

def test_缓存key用样本哈希而非id(client):
    """两个 id 不同但样本相同的克隆音色，缓存 key 应相同。"""
    c1 = upload_clone(client, name="A")
    c2 = upload_clone(client, name="B")  # 同样的 make_wav 内容 → 同样本哈希
    r1 = db.get_voice_clone(c1["id"])
    r2 = db.get_voice_clone(c2["id"])
    assert r1["sample_hash"] == r2["sample_hash"]

    k1 = synth._hash_factors(c1["voice"], "calm_narration")
    k2 = synth._hash_factors(c2["voice"], "calm_narration")
    assert k1 == k2  # voice_key 都是 clone:<同一样本哈希>
    assert k1[0].startswith("clone:")


# ---------- 引用检查与删除 ----------

def test_删除被引用的克隆音色需force(client):
    clone = upload_clone(client)
    client.post("/api/projects", json={
        "name": "p", "raw_text": "x", "default_voice": clone["voice"],
        "default_style": "calm_narration",
    })
    # 直接删被拒（409）
    r = client.delete(f"/api/voice-clones/{clone['id']}")
    assert r.status_code == 409
    # force 删成功
    r = client.delete(f"/api/voice-clones/{clone['id']}?force=true")
    assert r.status_code == 200


def test_删除克隆音色清理样本文件(client):
    clone = upload_clone(client, name="独一份")
    row = db.get_voice_clone(clone["id"])
    h, ext = row["sample_hash"], row["sample_ext"]
    assert sample_store.exists(h, ext)
    client.delete(f"/api/voice-clones/{clone['id']}")
    assert not sample_store.exists(h, ext)  # 没人共享，样本被清


def test_共享样本不误删(client):
    """两个克隆音色共享同一样本，删其一不该删样本文件。"""
    c1 = upload_clone(client, name="A")
    c2 = upload_clone(client, name="B")
    row = db.get_voice_clone(c1["id"])
    h, ext = row["sample_hash"], row["sample_ext"]
    client.delete(f"/api/voice-clones/{c1['id']}")
    assert sample_store.exists(h, ext)  # c2 还在用，样本保留
    client.delete(f"/api/voice-clones/{c2['id']}")
    assert not sample_store.exists(h, ext)  # 都删了才清


# ---------- 单元：DataURL 与前缀解析 ----------

def test_clone前缀解析():
    assert synth.parse_clone_id("clone:7") == 7
    assert synth.parse_clone_id("苏打") is None
    assert synth.parse_clone_id("clone:abc") is None
    assert synth.parse_clone_id(None) is None
