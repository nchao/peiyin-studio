PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS project (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    name          TEXT    NOT NULL,
    raw_text      TEXT    NOT NULL DEFAULT '',
    default_voice TEXT    NOT NULL DEFAULT '苏打',
    default_style TEXT    NOT NULL DEFAULT 'calm_narration',
    default_speed REAL    NOT NULL DEFAULT 1.0,
    created_at    TEXT    NOT NULL,
    updated_at    TEXT    NOT NULL
);

-- voice / style 为 NULL 表示继承 project 的默认值
CREATE TABLE IF NOT EXISTS segment (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id     INTEGER NOT NULL REFERENCES project(id) ON DELETE CASCADE,
    seq            INTEGER NOT NULL,
    display_text   TEXT    NOT NULL,
    synth_text     TEXT    NOT NULL,
    voice          TEXT,
    style          TEXT,
    pause_after_ms INTEGER NOT NULL DEFAULT 0,
    speed          REAL,  -- NULL=继承项目 default_speed；否则段级语速倍率
    audio_hash     TEXT,
    duration_ms    INTEGER,
    status         TEXT    NOT NULL DEFAULT 'pending',
    error_msg      TEXT,
    -- 字幕时间轴对齐（导入 SRT 时填入）。NULL = 顺序拼接模式，
    -- 有值 = 该段音频要对齐到视频的 [start_ms, end_ms] 窗口
    start_ms       INTEGER,
    end_ms         INTEGER
);

CREATE INDEX IF NOT EXISTS idx_segment_project ON segment(project_id, seq);

CREATE TABLE IF NOT EXISTS audio (
    hash        TEXT PRIMARY KEY,
    duration_ms INTEGER NOT NULL,
    byte_size   INTEGER NOT NULL,
    created_at  TEXT    NOT NULL
);

-- 克隆音色（全局共享）。样本音频存 data/samples/<sample_hash>.<ext>，
-- 与合成产物缓存（audio 表 / data/audio）隔离，不被 purge_orphans 清理。
CREATE TABLE IF NOT EXISTS voice_clone (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    name         TEXT    NOT NULL,
    sample_hash  TEXT    NOT NULL,
    sample_ext   TEXT    NOT NULL DEFAULT 'wav',
    duration_ms  INTEGER,
    created_at   TEXT    NOT NULL
);
