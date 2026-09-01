"""HTTP 路由。薄层，业务逻辑都在各模块里。"""

from __future__ import annotations

import hmac
import io
import json
import re
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import quote

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import (
    audio_store, auth, db, export, llm_preprocess, mimo_tts,
    sample_store, srt, synth,
)
from .config import settings
from .voices import STYLE_PRESETS, VOICE_IDS, VOICES


@asynccontextmanager
async def lifespan(_app: FastAPI):
    settings.audio_dir.mkdir(parents=True, exist_ok=True)
    db.init_db()
    yield


app = FastAPI(title="配音工作台", version="1.0.0", lifespan=lifespan)
app.add_middleware(auth.AuthMiddleware)


def _attachment(filename: str) -> dict[str, str]:
    """RFC 5987 编码的 Content-Disposition，中文文件名不乱码。"""
    return {"Content-Disposition": f"attachment; filename*=UTF-8''{quote(filename)}"}


# ---------- 请求体 ----------

class ProjectCreate(BaseModel):
    name: str = Field(default="未命名", max_length=100)
    raw_text: str = ""
    default_voice: str = "苏打"
    default_style: str = "calm_narration"


class ProjectPatch(BaseModel):
    name: str | None = None
    raw_text: str | None = None
    default_voice: str | None = None
    default_style: str | None = None


class SegmentPatch(BaseModel):
    display_text: str | None = None
    synth_text: str | None = None
    voice: str | None = None
    style: str | None = None
    pause_after_ms: int | None = Field(default=None, ge=0, le=5000)


class SegmentsReplace(BaseModel):
    segments: list[dict]


class SrtImport(BaseModel):
    content: str = Field(min_length=1, max_length=2_000_000)


class PreviewReq(BaseModel):
    text: str = Field(min_length=1, max_length=500)
    voice: str = "苏打"
    style: str | None = "calm_narration"


# ---------- 鉴权 ----------

class LoginReq(BaseModel):
    password: str = Field(min_length=1, max_length=200)


@app.get("/api/auth-status")
def auth_status(request: Request):
    """前端开屏探测：是否需要登录、当前是否已登录。"""
    if not settings.app_password:
        return {"auth_required": False, "logged_in": True}
    return {
        "auth_required": True,
        "logged_in": auth.token_ok(request.cookies.get(auth.COOKIE_NAME)),
    }


@app.post("/api/login")
async def login(request: Request, body: LoginReq):
    if not settings.app_password:
        return {"logged_in": True}  # 没设密码，随便进
    if auth.too_many_fails(request):
        raise HTTPException(429, "尝试过于频繁，请稍后再试")
    if not hmac.compare_digest(body.password, settings.app_password):
        await auth.register_fail(request)
        raise HTTPException(401, "密码错误")

    auth.clear_fails(request)
    resp = JSONResponse({"logged_in": True})
    resp.set_cookie(
        auth.COOKIE_NAME,
        auth.expected_token(),
        max_age=auth.COOKIE_MAX_AGE,
        httponly=True,
        samesite="lax",
        secure=auth.is_secure(request),
    )
    return resp


@app.post("/api/logout")
def logout():
    resp = JSONResponse({"logged_in": False})
    resp.delete_cookie(auth.COOKIE_NAME)
    return resp


# ---------- 元数据 ----------

@app.get("/api/meta")
def meta():
    return {
        "voices": VOICES,
        "styles": STYLE_PRESETS,
        "tts_model": settings.mimo_tts_model,
        "llm_model": settings.llm_model,
        "key_configured": bool(settings.mimo_api_key),
        "concurrency": settings.tts_concurrency,
    }


# ---------- 克隆音色 ----------

MAX_SAMPLE_BYTES = 10 * 1024 * 1024
MIN_SAMPLE_MS = 3000
MAX_SAMPLE_MS = 15000  # 样本超过则从正中间截取这么长，克隆只需一小段代表性人声


def _clone_to_dict(row) -> dict:
    return {
        "id": row["id"],
        "name": row["name"],
        "duration_ms": row["duration_ms"],
        "created_at": row["created_at"],
        "voice": f"{synth.CLONE_PREFIX}{row['id']}",  # 前端直接拿去当音色值
    }


@app.get("/api/voice-clones")
def list_voice_clones():
    return [_clone_to_dict(r) for r in db.list_voice_clones()]


@app.post("/api/voice-clones")
async def create_voice_clone(file: UploadFile = File(...), name: str = Form(...)):
    name = name.strip()
    if not name:
        raise HTTPException(400, "音色名不能为空")
    if len(name) > 50:
        raise HTTPException(400, "音色名过长（≤50 字）")

    src_ext = (file.filename or "").rsplit(".", 1)[-1].lower()
    if src_ext not in sample_store.INPUT_EXT:
        allowed = "、".join(sorted(sample_store.INPUT_EXT))
        raise HTTPException(400, f"不支持的样本格式 {src_ext or '未知'}，支持：{allowed}")

    data = await file.read()
    if not data:
        raise HTTPException(400, "样本文件为空")
    if len(data) > MAX_SAMPLE_BYTES:
        raise HTTPException(400, "样本文件过大（≤10MB）")

    # 时长先按原始文件探测（ffprobe 认所有输入格式）。太短没法克隆直接拒；
    # 太长不再拒，从正中间截取 MAX_SAMPLE_MS —— 避开开头/结尾常见的静音换气。
    dur = sample_store.probe_duration_ms(data, src_ext)
    if dur is not None and dur < MIN_SAMPLE_MS:
        raise HTTPException(
            400, f"样本时长 {dur/1000:.1f}s 太短（至少 {MIN_SAMPLE_MS//1000}s），无法克隆")
    truncated = dur is not None and dur > MAX_SAMPLE_MS
    clip_ms = MAX_SAMPLE_MS if truncated else None
    start_ms = (dur - MAX_SAMPLE_MS) // 2 if truncated else 0  # 居中截取的起点

    # wav 且无需截断可直接存；否则统一过 ffmpeg 转 wav（并按需截断）——
    # MiMo voiceclone 只稳定接受 wav/mp3
    if src_ext == "wav" and not truncated:
        store_ext, store_data = "wav", data
    else:
        try:
            store_data = sample_store.transcode_to_wav(
                data, src_ext, max_ms=clip_ms, start_ms=start_ms)
        except sample_store.TranscodeError as exc:
            raise HTTPException(400, str(exc)) from exc
        store_ext = "wav"

    # 截断后落库的时长以实际截取值为准
    if truncated:
        dur = MAX_SAMPLE_MS

    h = sample_store.save(store_data, store_ext)
    cid = db.create_voice_clone(name, h, store_ext, dur)
    result = _clone_to_dict(db.get_voice_clone(cid))
    result["truncated"] = truncated  # 前端据此提示已自动从中间截取 15s
    return result


class CloneRename(BaseModel):
    name: str = Field(min_length=1, max_length=50)


@app.patch("/api/voice-clones/{cid}")
def rename_voice_clone(cid: int, body: CloneRename):
    if db.get_voice_clone(cid) is None:
        raise HTTPException(404, f"克隆音色 {cid} 不存在")
    db.rename_voice_clone(cid, body.name.strip())
    return _clone_to_dict(db.get_voice_clone(cid))


@app.delete("/api/voice-clones/{cid}")
def remove_voice_clone(cid: int, force: bool = False):
    row = db.get_voice_clone(cid)
    if row is None:
        raise HTTPException(404, f"克隆音色 {cid} 不存在")
    refs = db.count_clone_references(cid)
    if refs and not force:
        raise HTTPException(
            409, f"该音色仍被 {refs} 处引用（项目默认或段落），"
            f"确认删除会让这些位置的音色失效")
    db.delete_voice_clone(cid)
    # 没有别的克隆音色共享同一样本文件时才删样本
    if not db.sample_hash_shared(row["sample_hash"], cid):
        sample_store.delete(row["sample_hash"], row["sample_ext"])
    return {"deleted": True, "was_referenced": refs}


@app.get("/api/voice-clones/{cid}/sample")
def voice_clone_sample(cid: int):
    row = db.get_voice_clone(cid)
    if row is None:
        raise HTTPException(404, f"克隆音色 {cid} 不存在")
    if not sample_store.exists(row["sample_hash"], row["sample_ext"]):
        raise HTTPException(404, "样本文件缺失")
    media = "audio/mpeg" if row["sample_ext"] == "mp3" else "audio/wav"
    return FileResponse(
        sample_store.path_of(row["sample_hash"], row["sample_ext"]),
        media_type=media,
    )


# ---------- 项目 ----------

def _row_to_dict(row) -> dict:
    return dict(row) if row is not None else {}


def _require_project(pid: int) -> dict:
    p = db.get_project(pid)
    if p is None:
        raise HTTPException(404, f"项目 {pid} 不存在")
    return dict(p)


def _valid_voice(voice: str) -> bool:
    """预置音色名，或指向存在的克隆音色的 clone:<id>。"""
    cid = synth.parse_clone_id(voice)
    if cid is not None:
        return db.get_voice_clone(cid) is not None
    return voice in VOICE_IDS


def _fresh_segments(pid: int, project: dict) -> list[dict]:
    """导出/试听用的段落：过期音频一律不参与。

    宁可报错让用户重新合成，也不能悄悄导出旧音色的音频 —— 那和界面
    显示的不一致，用户发现不了。
    """
    segs = _segments_of(pid, project)
    stale = [s for s in segs if s["audio_hash"] and not s["fresh"]]
    if stale:
        raise HTTPException(
            409,
            f"有 {len(stale)} 段的音频与当前音色/语气不一致（改过设置），"
            f"请先点「合成全部」重新合成",
        )
    return segs


def _segments_of(pid: int, project: dict | None = None) -> list[dict]:
    """段落列表，附带 fresh 字段。

    fresh=False 表示这段的音频与当前音色/语气不一致（改项目级设置后
    status 仍是 ok，但音频已过期），前端据此提示需要重新合成。
    """
    p = project or _require_project(pid)
    out = []
    for row in db.list_segments(pid):
        d = _row_to_dict(row)
        d["fresh"] = synth.is_fresh(row, p)
        voice, style = synth.effective(row, p)
        d["effective_voice"] = voice
        d["effective_style"] = style
        # 时间轴模式：算出该段的字幕窗口与音频溢出，供前端标红
        if d.get("start_ms") is not None and d.get("end_ms") is not None:
            d["window_ms"] = int(d["end_ms"]) - int(d["start_ms"])
            dur = d.get("duration_ms")
            d["overflow_ms"] = max(0, int(dur) - d["window_ms"]) if dur else 0
        out.append(d)
    return out


@app.get("/api/projects")
def projects():
    return [_row_to_dict(r) for r in db.list_projects()]


@app.post("/api/projects")
def create_project(body: ProjectCreate):
    if not _valid_voice(body.default_voice):
        raise HTTPException(400, f"未知音色 {body.default_voice}")
    pid = db.create_project(
        body.name.strip() or "未命名",
        body.raw_text,
        body.default_voice,
        body.default_style,
    )
    return _require_project(pid)


@app.get("/api/projects/{pid}")
def project_detail(pid: int):
    p = _require_project(pid)
    return {
        "project": p,
        "segments": _segments_of(pid, p),
        "timeline": db.project_is_timeline(pid),
    }


@app.patch("/api/projects/{pid}")
def patch_project(pid: int, body: ProjectPatch):
    _require_project(pid)
    if body.default_voice is not None and not _valid_voice(body.default_voice):
        raise HTTPException(400, f"未知音色 {body.default_voice}")
    db.update_project(pid, **body.model_dump(exclude_none=True))
    return _require_project(pid)


@app.delete("/api/projects/{pid}")
def remove_project(pid: int):
    _require_project(pid)
    db.delete_project(pid)
    removed = audio_store.purge_orphans(db.referenced_hashes())
    return {"deleted": True, "audio_files_purged": removed}


# ---------- 分段 ----------

@app.post("/api/projects/{pid}/preprocess")
async def preprocess(pid: int, fallback: bool = True):
    """LLM 预处理。失败时按 fallback 决定是否退回规则分段。"""
    p = _require_project(pid)
    if not p["raw_text"].strip():
        raise HTTPException(400, "原文为空，先粘贴稿子")

    try:
        segments = await llm_preprocess.preprocess(
            p["raw_text"], base_style=p["default_style"]
        )
        mode = "llm"
        warning = None
    except llm_preprocess.PreprocessError as exc:
        if not fallback:
            raise HTTPException(502, {"message": str(exc), "detail": exc.detail}) from exc
        segments = llm_preprocess.fallback_split(p["raw_text"])
        mode = "rule"
        warning = f"LLM 未生效，已用规则分段。原因：{exc}"
        if exc.detail:
            warning += f"\n{exc.detail}"

    db.replace_segments(pid, segments)
    return {
        "mode": mode,
        "warning": warning,
        "segments": _segments_of(pid, p),
    }


@app.post("/api/projects/{pid}/split")
def split_by_rule(pid: int):
    """纯规则分段，不调 LLM。"""
    p = _require_project(pid)
    if not p["raw_text"].strip():
        raise HTTPException(400, "原文为空，先粘贴稿子")
    segments = llm_preprocess.fallback_split(p["raw_text"])
    db.replace_segments(pid, segments)
    return {"mode": "rule", "segments": _segments_of(pid, p)}


@app.post("/api/projects/{pid}/import-srt")
def import_srt(pid: int, body: SrtImport):
    """导入 SRT 字幕：每条字幕成一段，带上 start_ms/end_ms 进入时间轴模式。

    合成文本默认等于字幕文本，用户可在段落里单独改读法。原稿(raw_text)
    也一并填成字幕文本拼接，方便对照。
    """
    p = _require_project(pid)
    try:
        entries = srt.parse_srt(body.content)
    except srt.SrtParseError as exc:
        raise HTTPException(400, str(exc)) from exc

    segments = [
        {
            "display_text": e["display_text"],
            "synth_text": e["display_text"],
            "start_ms": e["start_ms"],
            "end_ms": e["end_ms"],
        }
        for e in entries
    ]
    db.replace_segments(pid, segments)
    # 原稿存一份纯文本，切回原稿视图时能看到
    db.update_project(pid, raw_text="\n".join(e["display_text"] for e in entries))
    return {
        "mode": "srt",
        "count": len(segments),
        "segments": _segments_of(pid, _require_project(pid)),
    }


@app.put("/api/projects/{pid}/segments")
def replace_segments(pid: int, body: SegmentsReplace):
    _require_project(pid)
    cleaned = []
    for s in body.segments:
        display = (s.get("display_text") or "").strip()
        if not display:
            continue
        cleaned.append(
            {
                "display_text": display,
                "synth_text": (s.get("synth_text") or display).strip(),
                "voice": s.get("voice") or None,
                "style": s.get("style") or None,
                "pause_after_ms": int(s.get("pause_after_ms") or 0),
            }
        )
    if not cleaned:
        raise HTTPException(400, "段落列表为空")
    db.replace_segments(pid, cleaned)
    return {"segments": _segments_of(pid)}


@app.patch("/api/segments/{sid}")
def patch_segment(sid: int, body: SegmentPatch):
    seg = db.get_segment(sid)
    if seg is None:
        raise HTTPException(404, f"段落 {sid} 不存在")
    if body.voice is not None and body.voice and not _valid_voice(body.voice):
        raise HTTPException(400, f"未知音色 {body.voice}")
    fields = body.model_dump(exclude_unset=True)
    db.update_segment(sid, **fields)
    return _row_to_dict(db.get_segment(sid))


# ---------- 合成 ----------

@app.post("/api/projects/{pid}/synthesize")
async def synthesize(pid: int, only_failed: bool = False):
    """SSE 推进度。前端用 fetch + ReadableStream 读。"""
    _require_project(pid)

    async def gen():
        async for ev in synth.synthesize_project(pid, only_failed=only_failed):
            yield f"data: {json.dumps(ev, ensure_ascii=False)}\n\n"
        # 反复改稿会攒下没人引用的音频，合成收尾时顺手清掉
        purged = audio_store.purge_orphans(db.referenced_hashes())
        if purged:
            yield f"data: {json.dumps({'type': 'purged', 'files': purged})}\n\n"

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.post("/api/segments/{sid}/synthesize")
async def synthesize_segment(sid: int):
    """只合成/重合成单段。用于失败重试、改完一句只重跑这一句。"""
    seg = db.get_segment(sid)
    if seg is None:
        raise HTTPException(404, f"段落 {sid} 不存在")
    r = await synth.synthesize_one(sid)
    if r.get("status") == "error":
        raise HTTPException(400, r.get("error", "合成失败"))
    p = _require_project(seg["project_id"])
    updated = next((s for s in _segments_of(seg["project_id"], p) if s["id"] == sid), None)
    return {"result": r, "segment": updated}


@app.post("/api/preview")
async def preview(body: PreviewReq):
    """单段试听，不落库不进缓存表 —— 用于试音色/语气。"""
    if not _valid_voice(body.voice):
        raise HTTPException(400, f"未知音色 {body.voice}")
    # 克隆音色：读样本走 voiceclone 分支
    clone_sample = None
    cid = synth.parse_clone_id(body.voice)
    if cid is not None:
        row = db.get_voice_clone(cid)
        if row is None or not sample_store.exists(row["sample_hash"], row["sample_ext"]):
            raise HTTPException(400, "克隆音色样本缺失")
        clone_sample = (sample_store.load(row["sample_hash"], row["sample_ext"]),
                        row["sample_ext"])
    try:
        wav, dur = await mimo_tts.synthesize(
            body.text, body.voice, body.style, clone_sample=clone_sample)
    except mimo_tts.TTSError as exc:
        raise HTTPException(502, str(exc)) from exc
    return Response(
        wav,
        media_type="audio/wav",
        headers={"X-Duration-Ms": str(dur)},
    )


@app.get("/api/segments/{sid}/audio")
def segment_audio(sid: int):
    seg = db.get_segment(sid)
    if seg is None:
        raise HTTPException(404, f"段落 {sid} 不存在")
    h = seg["audio_hash"]
    if not h or not audio_store.exists(h):
        raise HTTPException(404, "该段还没有音频")
    return FileResponse(audio_store.path_of(h), media_type="audio/wav")


# ---------- 导出 ----------

def _safe_filename(name: str) -> str:
    s = re.sub(r'[/\\:*?"<>|\x00-\x1f]', "_", name).strip() or "配音"
    return s[:60]


@app.get("/api/projects/{pid}/preview")
def preview_full(pid: int):
    """全篇试听：与导出同一份音频，但内联播放而非下载。

    改动前听一遍全篇是配音的常规动作，不该逼用户先下载到本地。
    """
    p = _require_project(pid)
    try:
        wav = _concat_for_export(pid, _fresh_segments(pid, p))
    except export.ExportError as exc:
        raise HTTPException(400, str(exc)) from exc
    return Response(
        wav,
        media_type="audio/wav",
        headers={"Content-Length": str(len(wav)), "Cache-Control": "no-store"},
    )


def _concat_for_export(pid: int, segs: list[dict]) -> bytes:
    """按项目模式拼接：时间轴模式贴 start_ms，顺序模式累加。"""
    if db.project_is_timeline(pid):
        wav, _places = export.concat_wav_timeline(segs)
    else:
        wav, _offsets = export.concat_wav(segs)
    return wav


@app.get("/api/projects/{pid}/export")
def export_audio(pid: int, fmt: str = "mp3"):
    p = _require_project(pid)
    if fmt not in {"mp3", "wav"}:
        raise HTTPException(400, "fmt 只支持 mp3 或 wav")
    try:
        wav = _concat_for_export(pid, _fresh_segments(pid, p))
        data = wav if fmt == "wav" else export.wav_to_mp3(wav)
    except export.ExportError as exc:
        raise HTTPException(400, str(exc)) from exc

    fname = f"{_safe_filename(p['name'])}.{fmt}"
    return StreamingResponse(
        io.BytesIO(data),
        media_type="audio/mpeg" if fmt == "mp3" else "audio/wav",
        headers={**_attachment(fname), "Content-Length": str(len(data))},
    )


@app.get("/api/projects/{pid}/srt")
def export_srt(pid: int):
    p = _require_project(pid)
    content = srt.build_srt(_fresh_segments(pid, p))
    if not content:
        raise HTTPException(400, "没有任何已合成的段落，无法生成字幕")
    fname = f"{_safe_filename(p['name'])}.srt"
    return Response(
        content.encode("utf-8-sig"),  # 带 BOM，剪辑软件识别中文更稳
        media_type="application/x-subrip",
        headers=_attachment(fname),
    )


# ---------- 静态前端（必须放最后，兜底路由）----------

_dist = Path(__file__).resolve().parents[2] / "frontend" / "dist"
if _dist.is_dir():
    app.mount("/", StaticFiles(directory=_dist, html=True), name="ui")
