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

-- 3b. book_nodes（定位层·标题树：章与节/子节都是它，章就是一级标题）
--    node_idx 全书文档序；chapter_idx 仅 kind='chapter' 的节点计数（对外唯一章坐标）
CREATE TABLE IF NOT EXISTS book_nodes (
    book_id       INTEGER NOT NULL,
    node_idx      INTEGER NOT NULL,   -- 全书节点序（文档序）
    kind          TEXT    NOT NULL,   -- chapter / section
    level         INTEGER NOT NULL,   -- 相对深度：章=0
    parent_idx    INTEGER,            -- 父节点；章节点为 NULL
    chapter_idx   INTEGER NOT NULL,   -- 所属章的逻辑索引
    title         TEXT    NOT NULL,
    title_fp      TEXT,               -- 标题指纹（稳定身份）
    head_para     INTEGER,            -- 标题行所在段落；NULL = 原文未标（合成节点）
    para_from     INTEGER NOT NULL,   -- 该节点覆盖的段落区间（含子树）
    para_to       INTEGER NOT NULL,
    n_paragraphs  INTEGER NOT NULL,
    synthetic     INTEGER NOT NULL DEFAULT 0,  -- 1 = 章节点由章标题合成（原文未标）
    built_at      TEXT,
    PRIMARY KEY (book_id, node_idx)
);
CREATE INDEX IF NOT EXISTS idx_nodes_chapter ON book_nodes(book_id, chapter_idx, level);

-- 3c. book_paragraphs（定位层·唯一段落真相）
--    node_idx = 就近的标题祖先（"所属区"）；章首无标题的段落 node_idx 就是章节点
CREATE TABLE IF NOT EXISTS book_paragraphs (
    book_id     INTEGER NOT NULL,
    chapter_idx INTEGER NOT NULL,
    para_idx    INTEGER NOT NULL,
    node_idx    INTEGER NOT NULL,
    seg_in_node INTEGER NOT NULL,   -- 在所属节点内第几段（含标题行为 0）
    text        TEXT    NOT NULL,
    fingerprint TEXT    NOT NULL,
    char_count  INTEGER NOT NULL,
    built_at    TEXT,
    PRIMARY KEY (book_id, chapter_idx, para_idx)
);
CREATE INDEX IF NOT EXISTS idx_bp_fp ON book_paragraphs(book_id, fingerprint);
CREATE INDEX IF NOT EXISTS idx_bp_node ON book_paragraphs(book_id, node_idx);

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
    chapter_id    INTEGER UNIQUE REFERENCES chapters(id),   -- 内部外键（不再作存储键）
    book_id       INTEGER NOT NULL REFERENCES books(id),
    chapter_idx   INTEGER,          -- 逻辑章索引：对外唯一章坐标
    blocks_json   TEXT,             -- 结构化产出（单一真源，Markdown 由它渲染）
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

-- 8. concept_sense (L4 概念层·义项：一个词可以有一条或多条)
-- 键 = (book_id, term_norm, sense_idx)。term 存「以原文为准」的词形，term_norm 是缓存键。
-- 旧表 concept_occurrences 指向物理段落坐标系，与 L3/前端用的逻辑坐标对不上
-- （实测 1/38 逐字匹配），已在 _drop_unusable_tables 里丢弃，重建为下面的逻辑坐标表。
CREATE TABLE IF NOT EXISTS concept_sense (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    book_id          INTEGER NOT NULL REFERENCES books(id),
    term             TEXT    NOT NULL,
    term_norm        TEXT    NOT NULL,
    sense_idx        INTEGER NOT NULL,               -- 按首现位置排序，稳定不随重跑变
    has_definition   TEXT    NOT NULL,               -- explicit / inferred / external
    definition_note  TEXT,
    confusion_types  TEXT,                           -- JSON 数组
    reading          TEXT    NOT NULL,               -- 当前位置怎么读
    common_reading   TEXT,                           -- 日常读法（日常义≠文本义时必填）
    scope            TEXT    NOT NULL,               -- book / chapter / paragraph
    range_note       TEXT,                           -- scope≠book 时说明它管到哪
    chapter_range    TEXT,                           -- JSON [起, 止]
    evolves_to_next  INTEGER NOT NULL DEFAULT 0,     -- 演化链
    distinction_note TEXT,                           -- 多义时：为何是不同含义而非不同维度
    dimensions_json  TEXT,                           -- JSON 数组（同一义项内部的面）
    aliases_json     TEXT,                           -- JSON 数组（同义词形：自在存在↔自在自为）
    emphasis_json    TEXT,                           -- JSON 对象；默认沉默，非空即有侧重
    stance           TEXT NOT NULL DEFAULT 'normal', -- normal / boundary（处于义项过渡）
    revision         INTEGER NOT NULL DEFAULT 1,
    state            TEXT NOT NULL DEFAULT 'published',  -- published / pending（待核稿）
    model            TEXT,
    created_at       TEXT NOT NULL,
    updated_at       TEXT NOT NULL,
    UNIQUE(book_id, term_norm, sense_idx)
);
CREATE INDEX IF NOT EXISTS idx_concept_sense_term ON concept_sense(book_id, term_norm);

-- 9. concept_occurrence (义项成立的位置·逻辑坐标，与 agent_notes / L3 一致)
CREATE TABLE IF NOT EXISTS concept_occurrence (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    sense_id        INTEGER NOT NULL REFERENCES concept_sense(id) ON DELETE CASCADE,
    chapter_idx     INTEGER NOT NULL,
    paragraph_index INTEGER NOT NULL,
    sentence_index  INTEGER,
    fingerprint     TEXT NOT NULL,
    quote           TEXT,
    role            TEXT NOT NULL DEFAULT 'evidence',   -- evidence / emphasis
    UNIQUE(sense_id, chapter_idx, paragraph_index, sentence_index)
);
CREATE INDEX IF NOT EXISTS idx_concept_occ_sense ON concept_occurrence(sense_id);
CREATE INDEX IF NOT EXISTS idx_concept_occ_pos ON concept_occurrence(chapter_idx, paragraph_index);

-- 10. term_candidate (L0 录入期提名：统计提名 + 人工确认)
CREATE TABLE IF NOT EXISTS term_candidate (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    book_id       INTEGER NOT NULL REFERENCES books(id),
    term          TEXT NOT NULL,
    term_norm     TEXT NOT NULL,
    chapter_count INTEGER NOT NULL,                  -- 覆盖章数（判核心概念的主信号）
    total         INTEGER NOT NULL,                  -- 总出现次数
    first_chapter INTEGER NOT NULL,
    signals       TEXT NOT NULL,                     -- JSON：["glossary","title","coverage"]
    state         TEXT NOT NULL DEFAULT 'nominated',  -- nominated / confirmed / rejected
    rank          INTEGER,
    updated_at    TEXT NOT NULL,
    UNIQUE(book_id, term_norm)
);

-- 11. term_scan (按需扫描缓存：term → 全书分布，避免每次重扫 1.1s)
CREATE TABLE IF NOT EXISTS term_scan (
    book_id           INTEGER NOT NULL REFERENCES books(id),
    term_norm         TEXT NOT NULL,
    total             INTEGER NOT NULL,
    distribution_json TEXT NOT NULL,
    built_at          TEXT NOT NULL,
    PRIMARY KEY (book_id, term_norm)
);

-- 12. Agent runs and paragraph-level generated notes
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


# ── 不可用旧表的丢弃 ────────────────────────────
# concept_occurrences 指向物理段落坐标系（paragraphs.id），而 L3 / agent_notes / 前端高亮
# 全部用逻辑切分坐标（chapter_idx + paragraph_index + fingerprint）。实测两套切分对不上：
# 抽一章 38 个逻辑段，能在物理表逐字找到的只有 1 段。所以这张表不能用，直接丢
# （它从未写入过任何数据）。重建后的表见 SCHEMA_SQL 里的 concept_occurrence。
_DROPPED_TABLES = ("concept_occurrences",)


def _drop_unusable_tables(conn: sqlite3.Connection) -> None:
    for table in _DROPPED_TABLES:
        exists = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone()
        if not exists:
            continue
        conn.execute(f"DROP TABLE {table}")
        conn.commit()
        print(f"[schema] 丢弃不可用的旧表：{table}（坐标系与逻辑段落不一致）")


def _ensure_columns(conn: sqlite3.Connection) -> None:
    """给已存在的表补上后加的列（SQLite 不支持加列时带约束，这里只需可空列）。"""
    wanted = {("concept_sense", "aliases_json", "TEXT"),
              ("chapter_layer", "blocks_json", "TEXT"),      # L2 结构化产出（单一真源）
              ("chapter_layer", "chapter_idx", "INTEGER")}    # 逻辑章索引（对外唯一章坐标）
    for table, column, decl in wanted:
        cols = [r[1] for r in conn.execute(f"PRAGMA table_info({table})")]
        if not cols or column in cols:
            continue
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")
        conn.commit()
        print(f"[schema] 补列：{table}.{column}")


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
            from db.workspace_schema import backup_before_upgrade, migrate
            backup_before_upgrade(conn, key)
            conn.executescript(SCHEMA_SQL)
            _migrate_layer_tables(conn)   # 旧骨架库（层表字段不同）按当前定义重建
            _drop_unusable_tables(conn)   # 坐标系不对的旧表（concept_occurrences）
            _ensure_columns(conn)         # 后加的列（concept_sense.aliases_json）
            conn.commit()
            migrate(conn)
            _connections[key] = conn
        return _connections[key]
