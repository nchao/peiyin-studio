"""合成编排：并发跑各段，命中缓存跳过，单段失败不阻塞其余段。"""

from __future__ import annotations

import asyncio
from typing import AsyncIterator

import httpx

from . import audio_store, db, mimo_tts
from .config import settings


def expected_hash(seg, project) -> str:
    """这一段按当前设置**应该**对应的音频哈希。

    新鲜度由哈希算出，不看 status 字段 —— 改项目级音色/语气时，继承它的
    段落并不会被逐段更新，只靠 status 判断会把过期音频当成已合成。
    """
    voice, style = effective(seg, project)
    return audio_store.audio_hash(
        seg["synth_text"], voice, style, settings.mimo_tts_model
    )


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

    async with sem:
        try:
            wav, dur = await mimo_tts.synthesize(
                seg["synth_text"], voice, style, client=client
            )
        except Exception as exc:  # noqa: BLE001 单段失败不该炸掉整批
            db.mark_synth_failed(sid, str(exc))
            return {"id": sid, "seq": seg["seq"], "status": "failed", "error": str(exc)}

    audio_store.save(h, wav)
    db.record_audio(h, dur, len(wav))
    db.mark_synth_ok(sid, h, dur)
    return {"id": sid, "seq": seg["seq"], "status": "ok", "duration_ms": dur}


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
