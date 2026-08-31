"""SRT 字幕生成。

时间轴由段时长累加得到，段后停顿计入间隔但不计入字幕显示时长。
MiMo 不返回字级时间戳，所以精度上限就是段级 —— 切段越细，字幕越准。
"""

from __future__ import annotations


def format_ts(ms: int) -> str:
    """毫秒 → SRT 时间戳 HH:MM:SS,mmm"""
    if ms < 0:
        ms = 0
    h, rem = divmod(ms, 3_600_000)
    m, rem = divmod(rem, 60_000)
    s, msec = divmod(rem, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{msec:03d}"


def build_srt(segments: list[dict]) -> str:
    """segments 需含 display_text / duration_ms / pause_after_ms。

    duration_ms 为 None 的段（未合成）跳过，不占时间轴 —— 与拼接逻辑一致。
    """
    lines: list[str] = []
    cursor = 0
    index = 1
    for seg in segments:
        dur = seg.get("duration_ms")
        if not dur:
            continue
        start, end = cursor, cursor + dur
        text = seg["display_text"].strip()
        lines.append(f"{index}\n{format_ts(start)} --> {format_ts(end)}\n{text}\n")
        index += 1
        cursor = end + int(seg.get("pause_after_ms") or 0)
    return "\n".join(lines)
