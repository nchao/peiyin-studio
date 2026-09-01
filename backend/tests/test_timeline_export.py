"""字幕时间轴模式拼接：补静音对齐、溢出顺延、placements 汇报。"""

import pytest

from app import audio_store, export
from app.wavutil import duration_ms_of
from helpers import make_wav


def _seg(h, start, end, seq=0, sid=None):
    return {"audio_hash": h, "start_ms": start, "end_ms": end,
            "seq": seq, "id": sid}


def test_音频短于窗口时补静音对齐下一段起点():
    h1, h2 = "a1" + "0" * 62, "a2" + "0" * 62
    audio_store.save(h1, make_wav(1000))   # 窗口 3000，音频 1000
    audio_store.save(h2, make_wav(1000))   # 窗口从 5000 开始
    out, places = export.concat_wav_timeline([
        _seg(h1, 2000, 5000, seq=0),
        _seg(h2, 5000, 8000, seq=1),
    ])
    # 前导静音 2000 + 段1音频 1000 + 补静音到 5000（2000）+ 段2音频 1000 = 6000
    assert duration_ms_of(out) == 6000
    assert places[0]["placed_ms"] == 2000
    assert places[0]["overflow_ms"] == 0
    assert places[1]["placed_ms"] == 5000


def test_音频超窗口时不截断而顺延后续段():
    h1, h2 = "b1" + "0" * 62, "b2" + "0" * 62
    audio_store.save(h1, make_wav(5000))   # 窗口只有 3000，音频 5000，溢出 2000
    audio_store.save(h2, make_wav(1000))
    out, places = export.concat_wav_timeline([
        _seg(h1, 0, 3000, seq=0),
        _seg(h2, 3000, 6000, seq=1),
    ])
    assert places[0]["overflow_ms"] == 2000
    # 段1从0放5000，段2的锚点3000已被压过 → 顺延从5000接着放
    assert places[1]["placed_ms"] == 5000
    # 总时长 = 5000 + 1000（无空档可补）
    assert duration_ms_of(out) == 6000


def test_placements汇报窗口与时长():
    h = "c1" + "0" * 62
    audio_store.save(h, make_wav(2500))
    _, places = export.concat_wav_timeline([_seg(h, 1000, 4000, seq=3, sid=99)])
    p = places[0]
    assert p["seq"] == 3 and p["id"] == 99
    assert p["window_ms"] == 3000
    assert p["duration_ms"] == 2500
    assert p["overflow_ms"] == 0


def test_跳过未合成的段():
    h = "d1" + "0" * 62
    audio_store.save(h, make_wav(1000))
    out, places = export.concat_wav_timeline([
        _seg(None, 0, 2000, seq=0),
        _seg(h, 2000, 4000, seq=1),
    ])
    assert len(places) == 1
    assert places[0]["seq"] == 1


def test_忽略无start_ms的段():
    h = "e1" + "0" * 62
    audio_store.save(h, make_wav(1000))
    out, places = export.concat_wav_timeline([
        {"audio_hash": h, "start_ms": None, "end_ms": None, "seq": 0},
        _seg(h, 1000, 3000, seq=1),
    ])
    assert len(places) == 1


def test_无任何音频时报错():
    with pytest.raises(export.ExportError, match="没有任何已合成"):
        export.concat_wav_timeline([_seg(None, 0, 1000)])
