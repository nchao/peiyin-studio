"""LLM 文本预处理：语义分段 + 语气标签 + 读法规范化。

走 OpenAI 兼容的 /chat/completions。核心约束是 display_text 拼起来必须
逐字等于原文 —— LLM 漏字/改写/润色是高频故障，静默接受会导出一条和稿
子不一样的音频，所以校验失败就报错并给 diff，绝不落库。
"""

from __future__ import annotations

import asyncio
import json
import re

import httpx

from .config import settings
from .segmenter import chunk_for_llm, normalize_for_compare, rule_split
from .voices import STYLE_PRESETS

_STYLE_LIST = "、".join(f'{s["id"]}({s["label"]})' for s in STYLE_PRESETS)

SYSTEM_PROMPT = f"""你是视频解说配音的文本预处理器。把用户给的解说稿切成段，每段给出两份文本。

# 最重要的规则：display_text 必须是原文的逐字拷贝

display_text 是**字幕**，用于显示。它只能是从原文里剪下来的连续片段，等同于复制粘贴。
禁止在 display_text 里做任何改动，包括但不限于：
- 禁止把阿拉伯数字改成中文（原文 "3000条" → display_text 必须还是 "3000条"，不能写 "三千条"）
- 禁止改写英文（原文 "JSON" → display_text 必须还是 "JSON"）
- 禁止增删或替换标点、禁止润色、禁止纠错、禁止简化
所有段的 display_text 顺序拼接后，必须与原文逐字一致。这一条会被程序自动校验，不一致则整批结果作废。

读法改写只能写进 synth_text，绝不能出现在 display_text 里。

# 字段说明

- display_text: 字幕文本。见上，原文逐字拷贝。
- synth_text: 送语音合成的文本。以 display_text 为基础，只允许这三类改动：
  1) 数字、英文、符号改成中文读法（"3000条"→"三千条"，"3.5倍"→"三点五倍"，"CPU"→"C P U"，"JSON"→"J S O N"）
  2) 多音字歧义纠正，用同音字替换（只在确有歧义时用，能不改就不改）
  3) 插入行内音频标签: [吸气] [深呼吸] [叹气] [轻笑] [笑] [冷笑] [紧张] [激动] [疲惫] [不耐烦] [震惊] [气声] [颤抖]
     标签要克制，整篇不超过段数的三分之一，只在情绪转折处加。
  没有需要改的地方时，synth_text 与 display_text 相同。
- style: 这一段的语气。**默认留空（null）表示跟随用户选的整篇语气** —— 绝大多数段都该留空。只有当某段情绪明显偏离全篇基调时（比如通篇平静叙述中突然一句激动的反问），才从这些预设里挑一个: {_STYLE_LIST}。不要给每段都填值。
- pause_after_ms: 段后停顿毫秒。一般 0；句群之间 200-400；话题切换 500-800。

# 分段规则

每段 15-25 个字，按语义完整性切，不要切断固定短语。遇到换行优先在换行处切。

# 示例

原文：一个返回3000条记录的接口，光是转成JSON就要200毫秒。
正确输出：
{{"segments":[
  {{"display_text":"一个返回3000条记录的接口，","synth_text":"一个返回三千条记录的接口，","style":null,"pause_after_ms":0}},
  {{"display_text":"光是转成JSON就要200毫秒。","synth_text":"光是转成J S O N就要二百毫秒。","style":null,"pause_after_ms":0}}
]}}
注意 display_text 里 "3000" 和 "JSON" 原样保留，只有 synth_text 改成了读法。

只输出 JSON，不要任何解释文字。"""


class PreprocessError(RuntimeError):
    """LLM 预处理失败。detail 里带可展示给用户的诊断信息。"""

    def __init__(self, message: str, *, detail: str = ""):
        super().__init__(message)
        self.detail = detail


def _strip_code_fence(s: str) -> str:
    s = s.strip()
    m = re.match(r"^```(?:json)?\s*(.*?)\s*```$", s, re.S)
    return m.group(1) if m else s


def verify_fidelity(raw_text: str, segments: list[dict]) -> None:
    """校验 display_text 拼接后与原文一致。不一致抛 PreprocessError。"""
    want = normalize_for_compare(raw_text)
    got = normalize_for_compare("".join(s["display_text"] for s in segments))
    if want == got:
        return

    # 找到第一个分歧位置，给出上下文，便于定位是漏字还是改写
    i = 0
    while i < min(len(want), len(got)) and want[i] == got[i]:
        i += 1
    lo = max(0, i - 15)
    raise PreprocessError(
        "LLM 改动了原文，已拒绝本次分段结果",
        detail=(
            f"首个分歧在第 {i} 字（忽略空白后）\n"
            f"原文: …{want[lo:i + 25]}…\n"
            f"LLM: …{got[lo:i + 25]}…\n"
            f"长度 原文={len(want)} LLM={len(got)}"
        ),
    )


def _validate_shape(data: object) -> list[dict]:
    if not isinstance(data, dict) or not isinstance(data.get("segments"), list):
        raise PreprocessError("LLM 输出不含 segments 数组")
    segs = data["segments"]
    if not segs:
        raise PreprocessError("LLM 返回了空的 segments")

    out: list[dict] = []
    for idx, s in enumerate(segs):
        if not isinstance(s, dict):
            raise PreprocessError(f"第 {idx} 段不是对象")
        display = s.get("display_text")
        if not isinstance(display, str) or not display.strip():
            raise PreprocessError(f"第 {idx} 段的 display_text 缺失或为空")
        synth = s.get("synth_text")
        if not isinstance(synth, str) or not synth.strip():
            synth = display
        # synth_text 只该插标签和改读法，长度不该翻倍 —— 那通常意味着它在扩写
        if len(synth) > max(40, len(display) * 3):
            raise PreprocessError(
                f"第 {idx} 段的 synth_text 长度异常（{len(synth)} vs {len(display)}），疑似扩写"
            )
        pause = s.get("pause_after_ms", 0)
        if not isinstance(pause, int) or not 0 <= pause <= 5000:
            pause = 0
        style = s.get("style")
        out.append(
            {
                "display_text": display,
                "synth_text": synth,
                "style": style if isinstance(style, str) and style else None,
                "pause_after_ms": pause,
            }
        )
    return out


def _build_system(base_style: str | None) -> str:
    system = SYSTEM_PROMPT
    if base_style:
        label = next(
            (s["label"] for s in STYLE_PRESETS if s["id"] == base_style), base_style
        )
        system += f"\n\n用户为整篇选的语气是「{label}」。以此为基调判断哪些段落需要单独指定 style。"
    return system


async def _preprocess_chunk(
    chunk: str, system: str, http: httpx.AsyncClient
) -> list[dict]:
    """处理一块文本。失败抛 PreprocessError。"""
    url = f"{settings.llm_base_url.rstrip('/')}/chat/completions"
    key = settings.llm_api_key or settings.mimo_api_key

    payload: dict = {
        "model": settings.llm_model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": chunk},
        ],
        "temperature": 0.2,
        "response_format": {"type": "json_object"},
    }
    # 关掉推理：分段是结构化任务，思考 token 占输出 90%+ 且拖慢 30 倍。
    # 两种写法都发，网关忽略不认识的那个。
    if settings.llm_disable_thinking:
        payload["thinking"] = {"type": "disabled"}
        payload["reasoning_effort"] = "none"

    headers = {
        "api-key": key,
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
    }

    try:
        resp = await http.post(url, json=payload, headers=headers)
    except httpx.HTTPError as exc:
        raise PreprocessError(f"请求 LLM 失败: {exc}") from exc
    if resp.status_code != 200:
        raise PreprocessError(f"LLM 返回 {resp.status_code}", detail=resp.text[:500])

    try:
        choice = resp.json()["choices"][0]
        content = choice["message"]["content"]
    except (KeyError, IndexError, TypeError) as exc:
        raise PreprocessError("LLM 响应结构异常") from exc

    # 输出被 token 上限截断时 JSON 必然不完整，明确报出来而不是让 JSON 解析报错
    if choice.get("finish_reason") == "length":
        raise PreprocessError(
            "LLM 输出被长度上限截断，这一块文本太长",
            detail=f"块长 {len(chunk)} 字，建议减小 LLM_CHUNK_CHARS",
        )

    try:
        data = json.loads(_strip_code_fence(content))
    except json.JSONDecodeError as exc:
        raise PreprocessError("LLM 输出不是合法 JSON", detail=content[:500]) from exc

    segments = _validate_shape(data)
    verify_fidelity(chunk, segments)
    return segments


async def preprocess(
    raw_text: str,
    *,
    base_style: str | None = None,
    client: httpx.AsyncClient | None = None,
) -> list[dict]:
    """调 LLM 做预处理。失败抛 PreprocessError，由调用方决定是否兜底。

    长稿按句末标点切块并行送 LLM —— 单次调用有输出 token 上限（实测
    1700 字就被 4096 token 截断），且分块后一块失败只影响那一块。
    base_style 是用户在界面上选的整篇语气，告知 LLM 以便判断哪段算
    「偏离基调」。
    """
    if not raw_text.strip():
        raise PreprocessError("原文为空")

    chunks = chunk_for_llm(raw_text, settings.llm_chunk_chars)
    system = _build_system(base_style)

    own = client is None
    http = client or httpx.AsyncClient(timeout=httpx.Timeout(180.0, connect=15.0))
    sem = asyncio.Semaphore(max(1, settings.llm_concurrency))

    async def one(chunk: str) -> list[dict]:
        async with sem:
            return await _preprocess_chunk(chunk, system, http)

    try:
        results = await asyncio.gather(
            *(one(c) for c in chunks), return_exceptions=True
        )
    finally:
        if own:
            await http.aclose()

    # 任一块失败就整体失败 —— 部分成功的结果拼不出完整原文，交给调用方兜底
    failures = [
        (i, r) for i, r in enumerate(results) if isinstance(r, BaseException)
    ]
    if failures:
        i, exc = failures[0]
        detail = getattr(exc, "detail", "") or ""
        raise PreprocessError(
            f"第 {i + 1}/{len(chunks)} 块处理失败: {exc}",
            detail=detail,
        )

    segments: list[dict] = []
    for r in results:
        segments.extend(r)  # type: ignore[arg-type]

    # 各块独立校验过，再校验一次全篇，防止切块本身丢字
    verify_fidelity(raw_text, segments)
    return segments


def fallback_split(raw_text: str) -> list[dict]:
    """规则兜底分段。不插标签、不改读法，style 留空继承项目设置。"""
    return [
        {"display_text": t, "synth_text": t, "style": None, "pause_after_ms": 0}
        for t in rule_split(raw_text)
    ]
