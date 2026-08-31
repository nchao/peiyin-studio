"""规则分段的保真性，以及 SRT 时间轴累加。"""

from app.segmenter import normalize_for_compare, rule_split
from app.srt import build_srt, format_ts

LONG = (
    "今天我们聊一个被低估的问题。很多人以为性能瓶颈在数据库，"
    "但实际上大部分请求的时间都花在了序列化上，这一点很反直觉。\n"
    "接下来我用三组数据说明。"
)


def test_规则分段不改一个字():
    parts = rule_split(LONG)
    assert normalize_for_compare("".join(parts)) == normalize_for_compare(LONG)


def test_换行是强制切点():
    parts = rule_split("第一句话。\n第二句话。")
    assert parts[0].endswith("。")
    assert "第二句话。" in parts


def test_无标点超长串被硬切():
    text = "啊" * 130
    parts = rule_split(text)
    assert all(len(p) <= 40 for p in parts)
    assert "".join(parts) == text


def test_空行被跳过():
    parts = rule_split("句一。\n\n\n句二。")
    assert len(parts) == 2


def test_单字符输入():
    assert rule_split("好") == ["好"]


def test_全空白输入返回空列表():
    assert rule_split("  \n \n ") == []


# ---------- SRT ----------

def test_时间戳格式():
    assert format_ts(0) == "00:00:00,000"
    assert format_ts(1500) == "00:00:01,500"
    assert format_ts(3_661_234) == "01:01:01,234"


def test_srt时间轴累加含段间停顿():
    segs = [
        {"display_text": "第一段", "duration_ms": 1000, "pause_after_ms": 0},
        {"display_text": "第二段", "duration_ms": 2000, "pause_after_ms": 500},
        {"display_text": "第三段", "duration_ms": 1500, "pause_after_ms": 0},
    ]
    out = build_srt(segs)
    # 第一段 0-1000，第二段 1000-3000，停顿 500，第三段 3500-5000
    assert "00:00:00,000 --> 00:00:01,000" in out
    assert "00:00:01,000 --> 00:00:03,000" in out
    assert "00:00:03,500 --> 00:00:05,000" in out


def test_srt跳过未合成段且序号连续():
    segs = [
        {"display_text": "有音频", "duration_ms": 1000, "pause_after_ms": 0},
        {"display_text": "没音频", "duration_ms": None, "pause_after_ms": 0},
        {"display_text": "也有", "duration_ms": 1000, "pause_after_ms": 0},
    ]
    out = build_srt(segs)
    assert "没音频" not in out
    assert out.startswith("1\n")
    assert "\n2\n" in out
    assert "\n3\n" not in out


def test_srt字幕文本用display不含标签():
    segs = [{"display_text": "他叹了口气", "duration_ms": 1000, "pause_after_ms": 0}]
    out = build_srt(segs)
    assert "他叹了口气" in out
    assert "[" not in out


def test_全部未合成时返回空串():
    assert build_srt([{"display_text": "x", "duration_ms": None}]) == ""
