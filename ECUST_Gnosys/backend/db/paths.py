"""Application data paths."""

import os
from pathlib import Path


ROOT_DIR = Path(__file__).resolve().parents[2]
DATABASE_DIR = ROOT_DIR / "database"
BOOKS_DIR = os.environ.get("GNOSYS_BOOKS_DIR", str(DATABASE_DIR / "books"))
SETTINGS_DB = DATABASE_DIR / "settings.db"
