"""
SQLite schema and connection management for the reader.

Database file: books/{book_folder}/book.db
基础数据表：books / chapters / paragraphs（正文 + 段落）
阅读笔记表：book_layer / chapter_layer / semantic_layer / concept_layer
Agent 表：agent_runs / agent_run_tasks / agent_run_events / agent_notes
"""

import sqlite3
import os
import re
import threading

# ── DDL ──────────────────────────────────────────────

SCHEMA_SQL = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

-- 1. books
CREATE TABLE IF NOT EXISTS books (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    title         TEXT    NOT NULL,
    language      TEXT    DEFAULT 'zh',
    authors       TEXT,              -- JSON array
    source_file   TEXT,
    processed_at  TEXT    NOT NULL,
    total_chaps   INTEGER NOT NULL,
    total_paras   INTEGER DEFAULT 0,
    toc_json      TEXT               -- 原始 TOC 结构 JSON
);

-- 2. chapters
CREATE TABLE IF NOT EXISTS chapters (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    book_id       INTEGER NOT NULL REFERENCES books(id),
    spine_order   INTEGER NOT NULL,
    href          TEXT    NOT NULL,
    title         TEXT    NOT NULL,
    content_html  TEXT    NOT NULL,
    content_text  TEXT    NOT NULL,
    heading_json  TEXT,              -- 子标题树 JSON
    para_count    INTEGER DEFAULT 0,
    UNIQUE(book_id, spine_order)
);
CREATE INDEX IF NOT EXISTS idx_chapters_book ON chapters(book_id, spine_order);

-- 3. paragraphs
CREATE TABLE IF NOT EXISTS paragraphs (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    chapter_id    INTEGER NOT NULL REFERENCES chapters(id),
    book_id       INTEGER NOT NULL REFERENCES books(id),
    seq           INTEGER NOT NULL,
    text          TEXT    NOT NULL,
    html          TEXT    NOT NULL,
    offset_start  INTEGER NOT NULL,
    offset_end    INTEGER NOT NULL,
    char_count    INTEGER NOT NULL,
    UNIQUE(chapter_id, seq)
);
CREATE INDEX IF NOT EXISTS idx_paras_chapter ON paragraphs(chapter_id, seq);
CREATE INDEX IF NOT EXISTS idx_paras_book    ON paragraphs(book_id);

-- 4. book_layer (L1 全书层·键=book_id)
CREATE TABLE IF NOT EXISTS book_layer (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    book_id       INTEGER NOT NULL UNIQUE REFERENCES books(id),
    content       TEXT,             -- 分析结果（Markdown）
    model_version TEXT,             -- 生成所用模型版本
    updated_at    TEXT
);

-- 5. chapter_layer (L2 章节层·键=chapter_id)
CREATE TABLE IF NOT EXISTS chapter_layer (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    chapter_id    INTEGER NOT NULL UNIQUE REFERENCES chapters(id),
    book_id       INTEGER NOT NULL REFERENCES books(id),
    content       TEXT,
    model_version TEXT,
    updated_at    TEXT
);
CREATE INDEX IF NOT EXISTS idx_ch_layer_book ON chapter_layer(book_id);

-- 6. semantic_layer (L3 语义层·键=MD5(规范化文本))
-- kind: 'para'=段落级预生成解释（content=简短解释 / detail=整段详细解释 /
--       sentences=逐句解释 JSON 数组）；其它/NULL=解释笔划词记忆
CREATE TABLE IF NOT EXISTS semantic_layer (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    book_id       INTEGER NOT NULL REFERENCES books(id),
    text_hash     TEXT    NOT NULL,
    content       TEXT,
    detail        TEXT,
    sentences     TEXT,
    kind          TEXT,
    model_version TEXT,
    updated_at    TEXT,
    source_text   TEXT,
    anchor_text   TEXT,
    UNIQUE(book_id, text_hash)
);
CREATE INDEX IF NOT EXISTS idx_semantic_layer_book ON semantic_layer(book_id);

-- 7. concept_layer (L4 概念层·键=规范化词条名)
CREATE TABLE IF NOT EXISTS concept_layer (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    book_id       INTEGER NOT NULL REFERENCES books(id),
    term          TEXT    NOT NULL,
    content       TEXT,
    model_version TEXT,
    updated_at    TEXT,
    UNIQUE(book_id, term)
);
CREATE INDEX IF NOT EXISTS idx_concept_layer_book ON concept_layer(book_id);

-- 8. concept_occurrences (概念→段落·预留)
CREATE TABLE IF NOT EXISTS concept_occurrences (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    concept_id   INTEGER NOT NULL REFERENCES concept_layer(id),
    paragraph_id INTEGER NOT NULL REFERENCES paragraphs(id)
);
CREATE INDEX IF NOT EXISTS idx_co_concept   ON concept_occurrences(concept_id);
CREATE INDEX IF NOT EXISTS idx_co_paragraph ON concept_occurrences(paragraph_id);

-- 9. Agent runs and paragraph-level generated notes
CREATE TABLE IF NOT EXISTS agent_runs (
    id             TEXT PRIMARY KEY,
    book_id        INTEGER NOT NULL REFERENCES books(id),
    status         TEXT NOT NULL,
    request_json   TEXT NOT NULL,
    model          TEXT NOT NULL,
    created_at     TEXT NOT NULL,
    updated_at     TEXT NOT NULL,
    error          TEXT
);
CREATE INDEX IF NOT EXISTS idx_agent_runs_book ON agent_runs(book_id, created_at);

CREATE TABLE IF NOT EXISTS agent_run_tasks (
    id               TEXT PRIMARY KEY,
    run_id           TEXT NOT NULL REFERENCES agent_runs(id) ON DELETE CASCADE,
    chapter_idx      INTEGER NOT NULL,
    paragraph_index  INTEGER NOT NULL,
    fingerprint      TEXT NOT NULL,
    source_text      TEXT NOT NULL,
    baseline_json    TEXT NOT NULL DEFAULT '{}',
    status           TEXT NOT NULL,
    candidate_json   TEXT,
    review_json      TEXT,
    error            TEXT,
    updated_at       TEXT NOT NULL,
    UNIQUE(run_id, chapter_idx, paragraph_index, fingerprint)
);
CREATE INDEX IF NOT EXISTS idx_agent_tasks_run ON agent_run_tasks(run_id, status);

CREATE TABLE IF NOT EXISTS agent_run_events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id      TEXT NOT NULL REFERENCES agent_runs(id) ON DELETE CASCADE,
    task_id     TEXT,
    stage       TEXT NOT NULL,
    message     TEXT NOT NULL,
    data_json   TEXT,
    created_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_agent_events_run ON agent_run_events(run_id, id);

CREATE TABLE IF NOT EXISTS agent_notes (
    id               TEXT PRIMARY KEY,
    chapter_idx      INTEGER NOT NULL,
    paragraph_index  INTEGER NOT NULL,
    fingerprint      TEXT NOT NULL,
    source_text      TEXT NOT NULL,
    short            TEXT NOT NULL,
    detail           TEXT NOT NULL,
    sentences        TEXT NOT NULL,
    review_json      TEXT NOT NULL,
    baseline_json    TEXT NOT NULL DEFAULT '{}',
    state            TEXT NOT NULL,
    model            TEXT NOT NULL,
    spec_version     TEXT NOT NULL,
    updated_at       TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_agent_notes_position ON agent_notes(chapter_idx, paragraph_index, state);
"""

# ── 旧骨架库升级 ────────────────────────────────
# 早期版本建的层表字段与现在不同，且带 NOT NULL 约束（如 semantic_layer.paragraph_id、
# concept_layer.name），按新定义写入会直接 500。首次连接时把这些表按当前定义重建，
# 只搬运交集列（concept_layer 的 name 归入 term），内容不丢；正常库零开销。
_LEGACY_LAYER = {
    "semantic_layer": ("paragraph_id",),
    "concept_layer": ("name",),
}
_LEGACY_RENAME = {"concept_layer": {"name": "term"}}


def _table_ddl(table: str):
    """从 SCHEMA_SQL 里抽出某张表的建表语句（避免 DDL 写两份）"""
    m = re.search(r'CREATE TABLE IF NOT EXISTS %s \(.*?\n\);' % table, SCHEMA_SQL, re.S)
    return m.group(0) if m else None


def _migrate_layer_tables(conn: sqlite3.Connection) -> None:
    for table, markers in _LEGACY_LAYER.items():
        old_cols = [r[1] for r in conn.execute(f"PRAGMA table_info({table})")]
        if not old_cols or not (set(markers) & set(old_cols)):
            continue   # 表不存在或已是当前定义
        ddl = _table_ddl(table)
        if not ddl:
            continue
        renamed = _LEGACY_RENAME.get(table, {})
        tmp = f"{table}__new"
        conn.execute("PRAGMA foreign_keys=OFF")
        try:
            conn.execute(ddl.replace(f"CREATE TABLE IF NOT EXISTS {table}", f"CREATE TABLE {tmp}"))
            new_cols = [r[1] for r in conn.execute(f"PRAGMA table_info({tmp})")]
            # 交集列搬运：旧列名 → 新列名（如 name → term）
            pairs = []
            for c in old_cols:
                tgt = c if c in new_cols else renamed.get(c)
                if tgt and tgt in new_cols:
                    pairs.append((c, tgt))
            if pairs:
                src = ", ".join(p[0] for p in pairs)
                dst = ", ".join(p[1] for p in pairs)
                conn.execute(f"INSERT INTO {tmp} ({dst}) SELECT {src} FROM {table}")
            conn.execute(f"DROP TABLE {table}")
            conn.execute(f"ALTER TABLE {tmp} RENAME TO {table}")
            conn.executescript(SCHEMA_SQL)   # 重建索引
            conn.commit()
            print(f"[schema] 层表已升级为当前定义：{table}（搬运列 {len(pairs)} 个）")
        finally:
            conn.execute("PRAGMA foreign_keys=ON")


# ── Connection pool (thread-local) ───────────────────

_connections: dict = {}
_lock = threading.Lock()

def close_db(db_path: str) -> None:
    """【关库】关闭并弹出某本书的常驻连接（Windows 上不关句柄则 book.db 文件删不掉）"""
    key = os.path.abspath(db_path)
    with _lock:
        conn = _connections.pop(key, None)
    if conn:
        conn.close()


def get_db(db_path: str) -> sqlite3.Connection:
    """Get or create a shared connection to a book database."""
    key = os.path.abspath(db_path)
    with _lock:
        if key not in _connections:
            os.makedirs(os.path.dirname(key), exist_ok=True)
            conn = sqlite3.connect(key, check_same_thread=False)
            conn.row_factory = sqlite3.Row
            conn.executescript(SCHEMA_SQL)
            _migrate_layer_tables(conn)   # 旧骨架库（层表字段不同）按当前定义重建
            conn.commit()
            _connections[key] = conn
        return _connections[key]
