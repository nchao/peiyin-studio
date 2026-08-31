"""缓存失效条件、WAV 拼接时长、孤儿清理。用真实文件和真实 ffmpeg。"""

import pytest

from app import audio_store, db, export
from app.wavutil import duration_ms_of, parse_wav
from helpers import make_wav

M = "mimo-v2.5-tts"


def test_四个因子任一变化都失效():
    base = audio_store.audio_hash("你好世界", "苏打", "suspense", M)
    assert audio_store.audio_hash("你好世界", "苏打", "suspense", M) == base
    assert audio_store.audio_hash("你好，世界", "苏打", "suspense", M) != base
    assert audio_store.audio_hash("你好世界", "白桦", "suspense", M) != base
    assert audio_store.audio_hash("你好世界", "苏打", "lively", M) != base
    assert audio_store.audio_hash("你好世界", "苏打", "suspense", "other-model") != base


def test_哈希分隔避免拼接歧义():
    """("ab","c") 与 ("a","bc") 不能撞哈希。"""
    assert audio_store.audio_hash("ab", "c", None, M) != audio_store.audio_hash("a", "bc", None, M)


def test_空style与空串style等价():
    assert audio_store.audio_hash("x", "苏打", None, M) == audio_store.audio_hash("x", "苏打", "", M)


def test_存取往返():
    h = "a" * 64
    wav = make_wav(500)
    audio_store.save(h, wav)
    assert audio_store.exists(h)
    assert audio_store.load(h) == wav
    assert duration_ms_of(wav) == 500


def test_save不留临时文件():
    audio_store.save("b" * 64, make_wav(100))
    assert list(audio_store.settings.audio_dir.glob("*.tmp")) == []


def test_孤儿清理只删未引用的():
    keep, drop = "c" * 64, "d" * 64
    audio_store.save(keep, make_wav(100))
    audio_store.save(drop, make_wav(100))
    assert audio_store.purge_orphans({keep}) == 1
    assert audio_store.exists(keep)
    assert not audio_store.exists(drop)


# ---------- 拼接 ----------

def _seg(h: str, pause: int = 0) -> dict:
    return {"audio_hash": h, "pause_after_ms": pause}


def test_拼接总时长等于各段加停顿():
    h1, h2, h3 = "1" * 64, "2" * 64, "3" * 64
    audio_store.save(h1, make_wav(1000))
    audio_store.save(h2, make_wav(2000))
    audio_store.save(h3, make_wav(500))
    out, offsets = export.concat_wav([_seg(h1, 300), _seg(h2, 0), _seg(h3, 200)])
    # 1000+300+2000+0+500+200 = 4000（末尾停顿也计入）
    assert duration_ms_of(out) == 4000
    assert offsets == [0, 1300, 3300]


def test_拼接跳过缺音频的段():
    h1 = "4" * 64
    audio_store.save(h1, make_wav(1000))
    out, offsets = export.concat_wav(
        [_seg(h1), {"audio_hash": None, "pause_after_ms": 0}, _seg("missing" + "0" * 57)]
    )
    assert duration_ms_of(out) == 1000
    assert offsets == [0]


def test_没有任何音频时报错():
    with pytest.raises(export.ExportError, match="没有任何已合成"):
        export.concat_wav([{"audio_hash": None, "pause_after_ms": 0}])


def test_采样率不一致时报错():
    h1, h2 = "5" * 64, "6" * 64
    audio_store.save(h1, make_wav(500, sample_rate=24000))
    audio_store.save(h2, make_wav(500, sample_rate=16000))
    with pytest.raises(export.ExportError, match="采样率不一致"):
        export.concat_wav([_seg(h1), _seg(h2)])


def test_拼接后格式仍是24k单声道16bit():
    h = "7" * 64
    audio_store.save(h, make_wav(300))
    out, _ = export.concat_wav([_seg(h)])
    info = parse_wav(out)
    assert (info.sample_rate, info.channels, info.sample_width) == (24000, 1, 2)


def test_转mp3走真实ffmpeg():
    h = "8" * 64
    audio_store.save(h, make_wav(800))
    wav, _ = export.concat_wav([_seg(h)])
    mp3 = export.wav_to_mp3(wav)
    # ID3 头或 MPEG 帧同步字
    assert mp3[:3] == b"ID3" or mp3[:2] in (b"\xff\xfb", b"\xff\xf3", b"\xff\xfa")
    assert len(mp3) > 500


# ---------- DB 层的缓存失效联动 ----------

def test_改synth_text清掉音频引用():
    pid = db.create_project("t", "原文", "苏打", "calm_narration")
    db.replace_segments(pid, [{"display_text": "原文", "synth_text": "原文"}])
    sid = db.list_segments(pid)[0]["id"]
    db.mark_synth_ok(sid, "9" * 64, 1000)
    assert db.get_segment(sid)["status"] == "ok"

    db.update_segment(sid, synth_text="改了")
    row = db.get_segment(sid)
    assert row["audio_hash"] is None
    assert row["duration_ms"] is None
    assert row["status"] == "pending"


def test_只改停顿不清音频():
    pid = db.create_project("t", "原文", "苏打", "calm_narration")
    db.replace_segments(pid, [{"display_text": "原文", "synth_text": "原文"}])
    sid = db.list_segments(pid)[0]["id"]
    db.mark_synth_ok(sid, "a" * 64, 1000)

    db.update_segment(sid, pause_after_ms=500)
    row = db.get_segment(sid)
    assert row["audio_hash"] == "a" * 64
    assert row["status"] == "ok"


def test_删项目级联删段落():
    pid = db.create_project("t", "原文", "苏打", "calm_narration")
    db.replace_segments(pid, [{"display_text": "a", "synth_text": "a"}])
    db.delete_project(pid)
    assert db.list_segments(pid) == []
