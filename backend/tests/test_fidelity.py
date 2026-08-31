"""LLM 输出的原文一致性校验 —— 这是整个预处理最关键的不变量。"""

import pytest

from app.llm_preprocess import PreprocessError, verify_fidelity, _validate_shape


def seg(display: str, synth: str | None = None) -> dict:
    return {"display_text": display, "synth_text": synth or display}


RAW = "他在2026年买了一台新电脑。价格是3.5万，性能翻倍。"


def test_逐字一致时通过():
    verify_fidelity(RAW, [seg("他在2026年买了一台新电脑。"), seg("价格是3.5万，性能翻倍。")])


def test_忽略空白差异():
    verify_fidelity(
        "他在 2026 年买了\n一台新电脑。",
        [seg("他在2026年买了"), seg("一台新电脑。")],
    )


def test_漏字被拒绝():
    with pytest.raises(PreprocessError) as e:
        verify_fidelity(RAW, [seg("他在2026年买了一台电脑。"), seg("价格是3.5万，性能翻倍。")])
    assert "改动了原文" in str(e.value)
    assert "首个分歧" in e.value.detail


def test_润色改写被拒绝():
    with pytest.raises(PreprocessError):
        verify_fidelity(RAW, [seg("他于2026年购入了一台新电脑。"), seg("价格是3.5万，性能翻倍。")])


def test_擅自增字被拒绝():
    with pytest.raises(PreprocessError):
        verify_fidelity(RAW, [seg("他在2026年买了一台崭新的电脑。"), seg("价格是3.5万，性能翻倍。")])


def test_标点被改也拒绝():
    """标点影响 TTS 停顿，不能放过。"""
    with pytest.raises(PreprocessError):
        verify_fidelity(RAW, [seg("他在2026年买了一台新电脑，"), seg("价格是3.5万，性能翻倍。")])


def test_段落顺序错乱被拒绝():
    with pytest.raises(PreprocessError):
        verify_fidelity(RAW, [seg("价格是3.5万，性能翻倍。"), seg("他在2026年买了一台新电脑。")])


def test_synth_text_可以改读法():
    """synth_text 允许改读法，只有 display_text 参与一致性校验。"""
    verify_fidelity(
        RAW,
        [
            seg("他在2026年买了一台新电脑。", "他在二零二六年买了一台新电脑。"),
            seg("价格是3.5万，性能翻倍。", "价格是三点五万，[停顿]性能翻倍。"),
        ],
    )


def test_synth_text_扩写被拒绝():
    data = {
        "segments": [
            {
                "display_text": "他买了电脑。",
                "synth_text": "他买了一台性能非常强劲的全新电脑，"
                "这台电脑的配置在同价位里相当出色，值得推荐给大家。",
            }
        ]
    }
    with pytest.raises(PreprocessError, match="疑似扩写"):
        _validate_shape(data)


def test_synth_text_缺失时回退为display_text():
    out = _validate_shape({"segments": [{"display_text": "他买了电脑。"}]})
    assert out[0]["synth_text"] == "他买了电脑。"


def test_非法停顿值被归零():
    out = _validate_shape(
        {"segments": [{"display_text": "喂", "pause_after_ms": 99999}]}
    )
    assert out[0]["pause_after_ms"] == 0


def test_空segments被拒绝():
    with pytest.raises(PreprocessError, match="空的 segments"):
        _validate_shape({"segments": []})


def test_缺segments字段被拒绝():
    with pytest.raises(PreprocessError, match="不含 segments"):
        _validate_shape({"data": []})
