"""SRT 字幕生成。

时间轴由段时长累加得到，段后停顿计入间隔但不计入字幕显示时长。
MiMo 不返回字级时间戳，所以精度上限就是段级 —— 切段越细，字幕越准。
"""

from __future__ import annotations

import re


class SrtParseError(ValueError):
    pass


# 00:00:01,500 或 00:00:01.500（点号也认），允许时/分/秒位数松一点
_TS = re.compile(r"(\d{1,2}):(\d{2}):(\d{2})[,.](\d{1,3})")
_ARROW = re.compile(r"\s*-->\s*")


def parse_ts(s: str) -> int:
    """SRT 时间戳 → 毫秒。"""
    m = _TS.fullmatch(s.strip())
    if not m:
        raise SrtParseError(f"时间戳格式不对：{s!r}")
    h, mm, ss, ms = m.groups()
    return ((int(h) * 60 + int(mm)) * 60 + int(ss)) * 1000 + int(ms.ljust(3, "0"))


def parse_srt(content: str) -> list[dict]:
    """解析 SRT 文本 → [{start_ms, end_ms, display_text}]。

    容错：允许缺序号、CRLF、多空行、时间戳用点或逗号。文本多行的合并成
    一行（配音段落不需要保留字幕换行）。按 start_ms 排序，剔除空文本条目。
    """
    text = content.replace("\r\n", "\n").replace("\r", "\n").strip("﻿\n ")
    if not text:
        raise SrtParseError("字幕内容为空")

    entries: list[dict] = []
    for block in re.split(r"\n\s*\n", text):
        lines = [ln for ln in block.split("\n") if ln.strip() != ""]
        if not lines:
            continue
        # 首行可能是纯数字序号，跳过它找到含 --> 的行
        idx = 0
        if lines[0].strip().isdigit() and _ARROW.search(lines[0]) is None:
            idx = 1
        if idx >= len(lines) or _ARROW.search(lines[idx]) is None:
            continue  # 没有时间轴行，跳过这个块
        left, _, right = lines[idx].partition("-->")
        start_ms = parse_ts(left)
        end_ms = parse_ts(right)
        if end_ms < start_ms:
            end_ms = start_ms
        display = " ".join(ln.strip() for ln in lines[idx + 1:]).strip()
        if not display:
            continue
        entries.append({"start_ms": start_ms, "end_ms": end_ms,
                        "display_text": display})

    if not entries:
        raise SrtParseError("没有解析到任何字幕条目")
    entries.sort(key=lambda e: e["start_ms"])
    return entries


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

    时间轴模式（段带 start_ms）：直接用导入的原始时间戳，与视频画面严格
    对齐，不看音频是否合成。
    顺序模式：时间轴由段时长累加得到，duration_ms 为 None 的段跳过。
    """
    if any(s.get("start_ms") is not None for s in segments):
        return _build_srt_timeline(segments)

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


def _build_srt_timeline(segments: list[dict]) -> str:
    """时间轴模式：用导入的 start_ms/end_ms 原样输出，还原视频字幕。"""
    lines: list[str] = []
    index = 1
    for seg in segments:
        start = seg.get("start_ms")
        if start is None:
            continue
        end = seg.get("end_ms")
        if end is None or end < start:
            end = start + int(seg.get("duration_ms") or 0)
        text = seg["display_text"].strip()
        lines.append(f"{index}\n{format_ts(start)} --> {format_ts(end)}\n{text}\n")
        index += 1
    return "\n".join(lines)
