"""语气继承：段级留空 → 用项目默认值；LLM 需要知道整篇基调。"""

import httpx
import respx

from app import db, llm_preprocess
from app.synth import effective
from app.voices import build_synth_payload_text
from helpers import llm_response

LLM_URL = "https://api.xiaomimimo.com/v1/chat/completions"


def test_段级留空继承项目语气和音色():
    project = {"default_voice": "白桦", "default_style": "suspense", "default_speed": 1.0}
    seg = {"voice": None, "style": None, "speed": None}
    assert effective(seg, project) == ("白桦", "suspense", 1.0)


def test_段级覆盖优先():
    project = {"default_voice": "白桦", "default_style": "suspense", "default_speed": 1.0}
    seg = {"voice": "茉莉", "style": "lively", "speed": None}
    assert effective(seg, project) == ("茉莉", "lively", 1.0)


def test_段级空串style表示不加风格标签():
    """style="" 与 None 语义不同：空串是显式的「不要风格标签」。"""
    project = {"default_voice": "白桦", "default_style": "suspense", "default_speed": 1.0}
    _, style, _ = effective({"voice": None, "style": "", "speed": None}, project)
    assert style == ""
    assert build_synth_payload_text("正文", style) == "正文"


def test_段级语速覆盖项目默认():
    project = {"default_voice": "白桦", "default_style": "suspense", "default_speed": 1.2}
    # 段级留空 → 继承 1.2；段级指定 → 用段级
    assert effective({"voice": None, "style": None, "speed": None}, project)[2] == 1.2
    assert effective({"voice": None, "style": None, "speed": 0.8}, project)[2] == 0.8


def test_规则兜底不写死项目语气():
    """兜底分段的 style 必须留空，否则改项目语气后旧段不跟着变。"""
    segs = llm_preprocess.fallback_split("第一句。第二句。")
    assert all(s["style"] is None for s in segs)


@respx.mock
async def test_base_style以中文标签进入prompt():
    route = respx.post(LLM_URL).mock(
        return_value=httpx.Response(
            200, json=llm_response({"segments": [{"display_text": "原文。", "synth_text": "原文。"}]})
        )
    )
    await llm_preprocess.preprocess("原文。", base_style="suspense")
    sent = route.calls[0].request.read().decode("utf-8")
    assert "悬疑深沉" in sent


@respx.mock
async def test_不传base_style时prompt不含基调句():
    route = respx.post(LLM_URL).mock(
        return_value=httpx.Response(
            200, json=llm_response({"segments": [{"display_text": "原文。", "synth_text": "原文。"}]})
        )
    )
    await llm_preprocess.preprocess("原文。")
    assert "以此为基调" not in route.calls[0].request.read().decode("utf-8")


@respx.mock
async def test_自定义语气词原样进prompt():
    route = respx.post(LLM_URL).mock(
        return_value=httpx.Response(
            200, json=llm_response({"segments": [{"display_text": "原文。", "synth_text": "原文。"}]})
        )
    )
    await llm_preprocess.preprocess("原文。", base_style="东北话 俏皮")
    assert "东北话 俏皮" in route.calls[0].request.read().decode("utf-8")


def test_改项目语气后留空的段跟着变():
    """这是留空继承的意义：改一次项目设置，所有未单独指定的段都跟着变。"""
    pid = db.create_project("t", "x", "苏打", "calm_narration")
    db.replace_segments(
        pid,
        [
            {"display_text": "第一段", "synth_text": "第一段", "style": None},
            {"display_text": "第二段", "synth_text": "第二段", "style": None},
        ],
    )
    segs = db.list_segments(pid)
    db.update_segment(segs[0]["id"], style="hype")  # 第一段单独指定

    db.update_project(pid, default_style="suspense")
    project = db.get_project(pid)
    rows = db.list_segments(pid)
    assert effective(rows[0], project)[1] == "hype"       # 指定过的不变
    assert effective(rows[1], project)[1] == "suspense"   # 留空的跟着变
