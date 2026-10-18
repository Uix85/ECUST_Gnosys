"""SQLite storage for application settings."""

import os
import sqlite3

from db.paths import SETTINGS_DB


def _connect() -> sqlite3.Connection:
    SETTINGS_DB.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    conn = sqlite3.connect(SETTINGS_DB)
    conn.execute(
        "CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL)"
    )
    conn.commit()
    os.chmod(SETTINGS_DB, 0o600)
    return conn


def load() -> dict[str, str]:
    """Return settings stored in SQLite."""
    conn = _connect()
    try:
        return dict(conn.execute("SELECT key, value FROM settings"))
    finally:
        conn.close()


def save(values: dict[str, str]) -> None:
    conn = _connect()
    try:
        conn.executemany(
            "INSERT INTO settings(key, value) VALUES(?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            values.items(),
        )
        conn.commit()
        os.chmod(SETTINGS_DB, 0o600)
    finally:
        conn.close()
