"""SQLite 访问层。单用户本地工具，直接用同步 sqlite3 足够。"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from .config import settings

SCHEMA = Path(__file__).with_name("schema.sql")


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def connect() -> sqlite3.Connection:
    settings.db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(settings.db_path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db() -> None:
    with connect() as conn:
        conn.executescript(SCHEMA.read_text(encoding="utf-8"))


# ---------- project ----------

def create_project(name: str, raw_text: str, voice: str, style: str) -> int:
    ts = now_iso()
    with connect() as conn:
        cur = conn.execute(
            "INSERT INTO project(name, raw_text, default_voice, default_style,"
            " created_at, updated_at) VALUES (?,?,?,?,?,?)",
            (name, raw_text, voice, style, ts, ts),
        )
        return int(cur.lastrowid)


def get_project(pid: int) -> sqlite3.Row | None:
    with connect() as conn:
        return conn.execute("SELECT * FROM project WHERE id=?", (pid,)).fetchone()


def list_projects() -> list[sqlite3.Row]:
    with connect() as conn:
        return conn.execute(
            "SELECT id, name, default_voice, default_style, created_at, updated_at,"
            " (SELECT COUNT(*) FROM segment s WHERE s.project_id=p.id) AS seg_count"
            " FROM project p ORDER BY updated_at DESC"
        ).fetchall()


def update_project(pid: int, **fields) -> None:
    allowed = {"name", "raw_text", "default_voice", "default_style"}
    sets = {k: v for k, v in fields.items() if k in allowed and v is not None}
    if not sets:
        return
    sets["updated_at"] = now_iso()
    clause = ", ".join(f"{k}=?" for k in sets)
    with connect() as conn:
        conn.execute(f"UPDATE project SET {clause} WHERE id=?", (*sets.values(), pid))


def delete_project(pid: int) -> None:
    with connect() as conn:
        conn.execute("DELETE FROM project WHERE id=?", (pid,))


# ---------- segment ----------

def replace_segments(pid: int, segments: list[dict]) -> None:
    """整体替换某项目的段落（预处理/重新分段后调用）。"""
    with connect() as conn:
        conn.execute("DELETE FROM segment WHERE project_id=?", (pid,))
        conn.executemany(
            "INSERT INTO segment(project_id, seq, display_text, synth_text, voice,"
            " style, pause_after_ms, status) VALUES (?,?,?,?,?,?,?, 'pending')",
            [
                (
                    pid,
                    i,
                    s["display_text"],
                    s["synth_text"],
                    s.get("voice"),
                    s.get("style"),
                    s.get("pause_after_ms", 0),
                )
                for i, s in enumerate(segments)
            ],
        )
        conn.execute("UPDATE project SET updated_at=? WHERE id=?", (now_iso(), pid))


def list_segments(pid: int) -> list[sqlite3.Row]:
    with connect() as conn:
        return conn.execute(
            "SELECT * FROM segment WHERE project_id=? ORDER BY seq", (pid,)
        ).fetchall()


def get_segment(sid: int) -> sqlite3.Row | None:
    with connect() as conn:
        return conn.execute("SELECT * FROM segment WHERE id=?", (sid,)).fetchone()


def update_segment(sid: int, **fields) -> None:
    """改 segment。改动合成因子时清掉音频引用，强制下次重合成。"""
    allowed = {
        "display_text", "synth_text", "voice", "style", "pause_after_ms",
        "audio_hash", "duration_ms", "status", "error_msg",
    }
    sets = {k: v for k, v in fields.items() if k in allowed}
    if not sets:
        return
    if {"synth_text", "voice", "style"} & sets.keys():
        sets.setdefault("audio_hash", None)
        sets.setdefault("duration_ms", None)
        sets.setdefault("status", "pending")
        sets.setdefault("error_msg", None)
    clause = ", ".join(f"{k}=?" for k in sets)
    with connect() as conn:
        conn.execute(f"UPDATE segment SET {clause} WHERE id=?", (*sets.values(), sid))


def mark_synth_ok(sid: int, h: str, duration_ms: int) -> None:
    with connect() as conn:
        conn.execute(
            "UPDATE segment SET audio_hash=?, duration_ms=?, status='ok',"
            " error_msg=NULL WHERE id=?",
            (h, duration_ms, sid),
        )


def mark_synth_failed(sid: int, msg: str) -> None:
    with connect() as conn:
        conn.execute(
            "UPDATE segment SET status='failed', error_msg=? WHERE id=?",
            (msg[:500], sid),
        )


# ---------- audio ----------

def record_audio(h: str, duration_ms: int, byte_size: int) -> None:
    with connect() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO audio(hash, duration_ms, byte_size, created_at)"
            " VALUES (?,?,?,?)",
            (h, duration_ms, byte_size, now_iso()),
        )


def get_audio(h: str) -> sqlite3.Row | None:
    with connect() as conn:
        return conn.execute("SELECT * FROM audio WHERE hash=?", (h,)).fetchone()


def referenced_hashes() -> set[str]:
    with connect() as conn:
        rows = conn.execute(
            "SELECT DISTINCT audio_hash FROM segment WHERE audio_hash IS NOT NULL"
        ).fetchall()
    return {r["audio_hash"] for r in rows}
