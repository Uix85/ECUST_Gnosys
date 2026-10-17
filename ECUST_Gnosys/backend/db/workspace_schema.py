"""Additive, versioned workspace schema. Never rewrites reader content or old notes."""

import os
import sqlite3

VERSION = 1
DDL = """
CREATE TABLE IF NOT EXISTS workspace_migrations(version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE IF NOT EXISTS reader_state(key TEXT PRIMARY KEY, value_json TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS bookmarks(id TEXT PRIMARY KEY, reference_json TEXT NOT NULL, label TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS workspace_notes(key TEXT PRIMARY KEY, layer TEXT NOT NULL, target_json TEXT NOT NULL, content TEXT NOT NULL, revision INTEGER NOT NULL, updated_at TEXT NOT NULL, model TEXT NOT NULL DEFAULT 'manual');
CREATE TABLE IF NOT EXISTS layer_drafts(id TEXT PRIMARY KEY, run_id TEXT NOT NULL, note_key TEXT NOT NULL, layer TEXT NOT NULL, target_json TEXT NOT NULL, result_json TEXT NOT NULL, review_json TEXT NOT NULL, baseline_json TEXT NOT NULL, state TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS layer_results(run_id TEXT PRIMARY KEY REFERENCES agent_runs(id) ON DELETE CASCADE, candidate_json TEXT, review_json TEXT, baseline_json TEXT NOT NULL, coverage_json TEXT NOT NULL DEFAULT '[]');
CREATE TABLE IF NOT EXISTS chat_sessions(id TEXT PRIMARY KEY, title TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS chat_messages(id TEXT PRIMARY KEY, session_id TEXT NOT NULL REFERENCES chat_sessions(id) ON DELETE CASCADE, role TEXT NOT NULL, content TEXT NOT NULL, references_json TEXT NOT NULL DEFAULT '[]', status TEXT NOT NULL, error TEXT, request_id TEXT, created_at TEXT NOT NULL, UNIQUE(session_id,request_id));
CREATE TABLE IF NOT EXISTS chat_events(id INTEGER PRIMARY KEY AUTOINCREMENT, message_id TEXT NOT NULL REFERENCES chat_messages(id) ON DELETE CASCADE, kind TEXT NOT NULL, data_json TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS idx_chat_messages_session ON chat_messages(session_id,created_at);
CREATE INDEX IF NOT EXISTS idx_chat_events_message ON chat_events(message_id,id);
"""


def backup_before_upgrade(conn, path):
    """SQLite backup includes committed WAL pages; a backup failure aborts opening."""
    existing = conn.execute("SELECT 1 FROM sqlite_master WHERE name='books'").fetchone()
    migrated = conn.execute("SELECT 1 FROM sqlite_master WHERE name='workspace_migrations'").fetchone()
    if migrated and conn.execute("SELECT 1 FROM workspace_migrations WHERE version=?", (VERSION,)).fetchone():
        return
    if existing:
        backup_path = f"{path}.before-workspace-v{VERSION}.bak"
        if not os.path.exists(backup_path):
            partial = backup_path + '.tmp'
            try:
                target = sqlite3.connect(partial)
                try:
                    conn.backup(target)
                finally:
                    target.close()      # Windows：不关连接就无法 rename（WinError 32）
                os.replace(partial, backup_path)
            finally:
                if os.path.exists(partial):
                    os.unlink(partial)


def migrate(conn):
    with conn:
        conn.execute("BEGIN")
        for statement in DDL.split(';'):
            if statement.strip():
                conn.execute(statement)
        conn.execute("INSERT OR IGNORE INTO workspace_migrations(version) VALUES(?)", (VERSION,))
