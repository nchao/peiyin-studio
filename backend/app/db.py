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
        _migrate(conn)


def _migrate(conn: sqlite3.Connection) -> None:
    """轻量迁移：给老库补新列。CREATE TABLE IF NOT EXISTS 不会改已存在的表。"""
    seg_cols = {r["name"] for r in conn.execute("PRAGMA table_info(segment)")}
    for col in ("start_ms", "end_ms"):
        if col not in seg_cols:
            conn.execute(f"ALTER TABLE segment ADD COLUMN {col} INTEGER")
    if "speed" not in seg_cols:
        conn.execute("ALTER TABLE segment ADD COLUMN speed REAL")
    proj_cols = {r["name"] for r in conn.execute("PRAGMA table_info(project)")}
    if "default_speed" not in proj_cols:
        conn.execute("ALTER TABLE project ADD COLUMN default_speed REAL NOT NULL DEFAULT 1.0")


# ---------- project ----------

def create_project(name: str, raw_text: str, voice: str, style: str,
                   speed: float = 1.0) -> int:
    ts = now_iso()
    with connect() as conn:
        cur = conn.execute(
            "INSERT INTO project(name, raw_text, default_voice, default_style,"
            " default_speed, created_at, updated_at) VALUES (?,?,?,?,?,?,?)",
            (name, raw_text, voice, style, speed, ts, ts),
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
    allowed = {"name", "raw_text", "default_voice", "default_style", "default_speed"}
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
    """整体替换某项目的段落（预处理/重新分段/导入字幕后调用）。

    segments 可含 start_ms/end_ms（导入 SRT 时），普通分段不带则为 NULL。
    """
    with connect() as conn:
        conn.execute("DELETE FROM segment WHERE project_id=?", (pid,))
        conn.executemany(
            "INSERT INTO segment(project_id, seq, display_text, synth_text, voice,"
            " style, pause_after_ms, speed, start_ms, end_ms, status)"
            " VALUES (?,?,?,?,?,?,?,?,?,?, 'pending')",
            [
                (
                    pid,
                    i,
                    s["display_text"],
                    s["synth_text"],
                    s.get("voice"),
                    s.get("style"),
                    s.get("pause_after_ms", 0),
                    s.get("speed"),
                    s.get("start_ms"),
                    s.get("end_ms"),
                )
                for i, s in enumerate(segments)
            ],
        )
        conn.execute("UPDATE project SET updated_at=? WHERE id=?", (now_iso(), pid))


def project_is_timeline(pid: int) -> bool:
    """项目是否处于字幕时间轴模式（存在任一段带 start_ms）。"""
    with connect() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM segment"
            " WHERE project_id=? AND start_ms IS NOT NULL",
            (pid,),
        ).fetchone()
    return row["n"] > 0


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
        "display_text", "synth_text", "voice", "style", "pause_after_ms", "speed",
        "audio_hash", "duration_ms", "status", "error_msg",
    }
    sets = {k: v for k, v in fields.items() if k in allowed}
    if not sets:
        return
    if {"synth_text", "voice", "style", "speed"} & sets.keys():
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


# ---------- voice_clone ----------

def create_voice_clone(name: str, sample_hash: str, sample_ext: str,
                       duration_ms: int | None) -> int:
    with connect() as conn:
        cur = conn.execute(
            "INSERT INTO voice_clone(name, sample_hash, sample_ext, duration_ms,"
            " created_at) VALUES (?,?,?,?,?)",
            (name, sample_hash, sample_ext, duration_ms, now_iso()),
        )
        return int(cur.lastrowid)


def list_voice_clones() -> list[sqlite3.Row]:
    with connect() as conn:
        return conn.execute(
            "SELECT * FROM voice_clone ORDER BY created_at DESC"
        ).fetchall()


def get_voice_clone(cid: int) -> sqlite3.Row | None:
    with connect() as conn:
        return conn.execute("SELECT * FROM voice_clone WHERE id=?", (cid,)).fetchone()


def rename_voice_clone(cid: int, name: str) -> None:
    with connect() as conn:
        conn.execute("UPDATE voice_clone SET name=? WHERE id=?", (name, cid))


def delete_voice_clone(cid: int) -> None:
    with connect() as conn:
        conn.execute("DELETE FROM voice_clone WHERE id=?", (cid,))


def sample_hash_shared(sample_hash: str, exclude_id: int) -> bool:
    """除 exclude_id 外，是否还有别的克隆音色用同一样本文件。

    删除克隆音色时据此决定要不要删样本文件 —— 内容相同的样本共享一份。
    """
    with connect() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM voice_clone WHERE sample_hash=? AND id<>?",
            (sample_hash, exclude_id),
        ).fetchone()
    return row["n"] > 0


def count_clone_references(cid: int) -> int:
    """有多少段落或项目引用了这个克隆音色（voice = 'clone:<id>'）。"""
    token = f"clone:{cid}"
    with connect() as conn:
        seg = conn.execute(
            "SELECT COUNT(*) AS n FROM segment WHERE voice=?", (token,)
        ).fetchone()["n"]
        proj = conn.execute(
            "SELECT COUNT(*) AS n FROM project WHERE default_voice=?", (token,)
        ).fetchone()["n"]
    return int(seg) + int(proj)
