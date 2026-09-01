"""合成编排：并发跑各段，命中缓存跳过，单段失败不阻塞其余段。"""

from __future__ import annotations

import asyncio
from typing import AsyncIterator

import httpx

from . import audio_store, db, mimo_tts, sample_store
from .config import settings

CLONE_PREFIX = "clone:"


def parse_clone_id(voice: str | None) -> int | None:
    """voice 是 'clone:<id>' 则返回 id，否则 None（预置音色）。"""
    if voice and voice.startswith(CLONE_PREFIX):
        try:
            return int(voice[len(CLONE_PREFIX):])
        except ValueError:
            return None
    return None


def _hash_factors(voice: str, style: str | None) -> tuple[str, str]:
    """算出用于缓存 key 的 (voice_key, model)。

    克隆音色的 voice 是 'clone:<id>'，但真正决定音频的是样本内容，所以
    key 用 'clone:<sample_hash>' —— 换样本才失效，换 id 但同样本仍命中。
    找不到克隆音色时用原串兜底（合成会失败，key 不撞已有缓存即可）。
    """
    cid = parse_clone_id(voice)
    if cid is None:
        return voice, settings.mimo_tts_model
    row = db.get_voice_clone(cid)
    if row is None:
        return voice, settings.mimo_clone_model
    return f"{CLONE_PREFIX}{row['sample_hash']}", settings.mimo_clone_model


def expected_hash(seg, project) -> str:
    """这一段按当前设置**应该**对应的音频哈希。

    新鲜度由哈希算出，不看 status 字段 —— 改项目级音色/语气时，继承它的
    段落并不会被逐段更新，只靠 status 判断会把过期音频当成已合成。
    """
    voice, style = effective(seg, project)
    voice_key, model = _hash_factors(voice, style)
    return audio_store.audio_hash(seg["synth_text"], voice_key, style, model)


def is_fresh(seg, project) -> bool:
    """段落音频是否与当前设置一致（且文件确实存在）。"""
    h = seg["audio_hash"]
    return bool(h) and h == expected_hash(seg, project) and audio_store.exists(h)


def effective(seg, project) -> tuple[str, str | None]:
    """段落的有效音色与语气：段级为空则继承项目默认值。"""
    voice = seg["voice"] or project["default_voice"]
    style = seg["style"] if seg["style"] is not None else project["default_style"]
    return voice, style


async def _one(seg, project, sem: asyncio.Semaphore, client: httpx.AsyncClient) -> dict:
    sid = seg["id"]
    voice, style = effective(seg, project)
    h = expected_hash(seg, project)

    if audio_store.exists(h):
        info = db.get_audio(h)
        if info and info["duration_ms"]:
            db.mark_synth_ok(sid, h, int(info["duration_ms"]))
            return {"id": sid, "seq": seg["seq"], "status": "cached",
                    "duration_ms": int(info["duration_ms"])}
        # 文件在但 audio 表没记录（比如手工拷进来的），重算时长
        from .wavutil import duration_ms_of
        dur = duration_ms_of(audio_store.load(h))
        db.record_audio(h, dur, audio_store.path_of(h).stat().st_size)
        db.mark_synth_ok(sid, h, dur)
        return {"id": sid, "seq": seg["seq"], "status": "cached", "duration_ms": dur}

    # 克隆音色：读样本，合成走 voiceclone 分支
    clone_sample = None
    cid = parse_clone_id(voice)
    if cid is not None:
        row = db.get_voice_clone(cid)
        if row is None:
            msg = f"克隆音色已删除（clone:{cid}），请重新选择音色"
            db.mark_synth_failed(sid, msg)
            return {"id": sid, "seq": seg["seq"], "status": "failed", "error": msg}
        try:
            data = sample_store.load(row["sample_hash"], row["sample_ext"])
        except OSError:
            msg = f"克隆音色「{row['name']}」的样本文件缺失"
            db.mark_synth_failed(sid, msg)
            return {"id": sid, "seq": seg["seq"], "status": "failed", "error": msg}
        clone_sample = (data, row["sample_ext"])

    async with sem:
        try:
            wav, dur = await mimo_tts.synthesize(
                seg["synth_text"], voice, style,
                clone_sample=clone_sample, client=client,
            )
        except Exception as exc:  # noqa: BLE001 单段失败不该炸掉整批
            db.mark_synth_failed(sid, str(exc))
            return {"id": sid, "seq": seg["seq"], "status": "failed", "error": str(exc)}

    audio_store.save(h, wav)
    db.record_audio(h, dur, len(wav))
    db.mark_synth_ok(sid, h, dur)
    return {"id": sid, "seq": seg["seq"], "status": "ok", "duration_ms": dur}


async def synthesize_one(
    sid: int, *, client: httpx.AsyncClient | None = None
) -> dict:
    """只合成单段。命中缓存也会刷新 status，用于「重合成这一句」。

    返回 _one 的结果 dict（status: ok/cached/failed），段不存在返回 error。
    """
    seg = db.get_segment(sid)
    if seg is None:
        return {"id": sid, "status": "error", "error": f"段落 {sid} 不存在"}
    project = db.get_project(seg["project_id"])
    if project is None:
        return {"id": sid, "status": "error", "error": "所属项目不存在"}

    sem = asyncio.Semaphore(1)
    own = client is None
    http = client or httpx.AsyncClient(timeout=httpx.Timeout(180.0, connect=15.0))
    try:
        return await _one(seg, project, sem, http)
    finally:
        if own:
            await http.aclose()


async def synthesize_project(
    pid: int, *, only_failed: bool = False, client: httpx.AsyncClient | None = None
) -> AsyncIterator[dict]:
    """逐段产出进度事件。最后一条是 summary。"""
    project = db.get_project(pid)
    if project is None:
        yield {"type": "error", "message": f"项目 {pid} 不存在"}
        return

    segs = db.list_segments(pid)
    if only_failed:
        # 按新鲜度筛而非 status：改了项目音色的段 status 还是 ok，但音频已过期
        segs = [s for s in segs if not is_fresh(s, project)]
    if not segs:
        yield {"type": "summary", "total": 0, "ok": 0, "cached": 0, "failed": 0}
        return

    yield {"type": "start", "total": len(segs)}

    sem = asyncio.Semaphore(max(1, settings.tts_concurrency))
    own = client is None
    http = client or httpx.AsyncClient(timeout=httpx.Timeout(180.0, connect=15.0))
    counts = {"ok": 0, "cached": 0, "failed": 0}
    done = 0
    try:
        tasks = [asyncio.create_task(_one(s, project, sem, http)) for s in segs]
        for fut in asyncio.as_completed(tasks):
            r = await fut
            counts[r["status"]] = counts.get(r["status"], 0) + 1
            done += 1
            yield {"type": "progress", "done": done, "total": len(segs), **r}
    finally:
        if own:
            await http.aclose()

    yield {"type": "summary", "total": len(segs), **counts}
