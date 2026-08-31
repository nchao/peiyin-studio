"""音色与语气预设。

MiMo TTS 把两者天然分离：voice 走 audio.voice 参数，语气走文本开头的
`(风格)` 标签。这里只暴露中文音色和视频解说常用的语气预设。
"""

VOICES = [
    {"id": "苏打", "label": "苏打", "gender": "male", "hint": "男声，清亮"},
    {"id": "白桦", "label": "白桦", "gender": "male", "hint": "男声，沉稳"},
    {"id": "冰糖", "label": "冰糖", "gender": "female", "hint": "女声，甜美"},
    {"id": "茉莉", "label": "茉莉", "gender": "female", "hint": "女声，温柔"},
]

VOICE_IDS = {v["id"] for v in VOICES}

# key -> 拼进文本开头括号里的风格词
STYLE_PRESETS = [
    {"id": "suspense", "label": "悬疑深沉", "tags": "深沉 磁性 平静"},
    {"id": "calm_narration", "label": "平静客观", "tags": "平静 干练"},
    {"id": "lively", "label": "活泼轻快", "tags": "活泼 俏皮"},
    {"id": "hype", "label": "激动高燃", "tags": "兴奋 激动 凌厉"},
    {"id": "sarcastic", "label": "冷静吐槽", "tags": "冷漠 无奈"},
    {"id": "warm", "label": "温情旁白", "tags": "温柔 醇厚"},
]

STYLE_MAP = {s["id"]: s["tags"] for s in STYLE_PRESETS}


def resolve_style_tags(style: str | None) -> str:
    """把 style 解析成括号内的风格词。

    预设 id 走映射表；非预设值当作用户自定义风格词原样使用（MiMo 接受
    列表外的自定义描述）。空值返回空串，表示不加风格标签。
    """
    if not style:
        return ""
    return STYLE_MAP.get(style, style.strip())


def build_synth_payload_text(synth_text: str, style: str | None) -> str:
    """拼出最终送给 TTS 的文本：`(风格词)正文`。"""
    tags = resolve_style_tags(style)
    if not tags:
        return synth_text
    return f"({tags}){synth_text}"
