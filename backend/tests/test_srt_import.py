"""SRT 导入解析的容错，以及时间轴模式的 build_srt 还原。"""

import pytest

from app.srt import SrtParseError, build_srt, parse_srt, parse_ts

BASIC = """1
00:00:02,000 --> 00:00:05,000
你好世界

2
00:00:05,000 --> 00:00:08,500
欢迎回来
"""


def test_解析基本srt():
    out = parse_srt(BASIC)
    assert len(out) == 2
    assert out[0] == {"start_ms": 2000, "end_ms": 5000, "display_text": "你好世界"}
    assert out[1]["end_ms"] == 8500


def test_时间戳毫秒换算():
    assert parse_ts("00:00:01,500") == 1500
    assert parse_ts("01:01:01,234") == 3_661_234
    assert parse_ts("00:00:02.000") == 2000  # 点号也认


def test_缺序号也能解析():
    out = parse_srt("00:00:00,000 --> 00:00:01,000\n单条无序号")
    assert out[0]["display_text"] == "单条无序号"


def test_crlf和bom容错():
    out = parse_srt("﻿1\r\n00:00:00,000 --> 00:00:01,000\r\n带bom和crlf\r\n")
    assert out[0]["display_text"] == "带bom和crlf"


def test_多行字幕合并成一行():
    out = parse_srt("1\n00:00:00,000 --> 00:00:02,000\n第一行\n第二行")
    assert out[0]["display_text"] == "第一行 第二行"


def test_乱序按start排序():
    srt = ("1\n00:00:05,000 --> 00:00:06,000\n后\n\n"
           "2\n00:00:01,000 --> 00:00:02,000\n先")
    out = parse_srt(srt)
    assert [e["display_text"] for e in out] == ["先", "后"]


def test_空文本条目被剔除():
    srt = ("1\n00:00:00,000 --> 00:00:01,000\n\n\n"
           "2\n00:00:01,000 --> 00:00:02,000\n有内容")
    out = parse_srt(srt)
    assert len(out) == 1
    assert out[0]["display_text"] == "有内容"


def test_end早于start时钳到start():
    out = parse_srt("1\n00:00:05,000 --> 00:00:03,000\n倒挂")
    assert out[0]["end_ms"] == out[0]["start_ms"] == 5000


def test_空内容报错():
    with pytest.raises(SrtParseError):
        parse_srt("   \n\n ")


def test_无有效条目报错():
    with pytest.raises(SrtParseError):
        parse_srt("这不是字幕\n只是随便一行文字")


def test_坏时间戳报错():
    with pytest.raises(SrtParseError):
        parse_ts("不是时间")


# ---------- 时间轴模式导出 SRT ----------

def test_时间轴模式用原始时间戳而非累加():
    # 即使音频时长与窗口不符，导出 SRT 仍还原导入的原始时间轴
    segs = [
        {"display_text": "甲", "start_ms": 2000, "end_ms": 5000, "duration_ms": 1000},
        {"display_text": "乙", "start_ms": 5000, "end_ms": 8500, "duration_ms": 9000},
    ]
    out = build_srt(segs)
    assert "00:00:02,000 --> 00:00:05,000" in out
    assert "00:00:05,000 --> 00:00:08,500" in out


def test_时间轴模式end缺失时用duration补():
    segs = [{"display_text": "甲", "start_ms": 1000, "end_ms": None,
             "duration_ms": 2000}]
    out = build_srt(segs)
    assert "00:00:01,000 --> 00:00:03,000" in out
