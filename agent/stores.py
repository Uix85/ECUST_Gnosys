"""SQLite-backed goal and context storage for the terminal agent."""

import agent.models
import datetime
import json
import pathlib
import sqlite3

SCHEMA = """
CREATE TABLE IF NOT EXISTS goals (
    id TEXT PRIMARY KEY,
    objective TEXT NOT NULL,
    acceptance_json TEXT NOT NULL,
    input_json TEXT NOT NULL,
    status TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS contexts (
    key TEXT PRIMARY KEY,
    context_json TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
"""


def _now() -> str:
    return datetime.datetime.now().isoformat(timespec="seconds")


class SQLiteStore:
    """One SQLite file for goals, contexts and SDK chat sessions."""

    def __init__(self, path: str | pathlib.Path):
        self.path = pathlib.Path(path).expanduser()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(self.path), check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript(SCHEMA)
        self._migrate_goals()
        self.db.commit()

    def _migrate_goals(self) -> None:
        """Drop old goal columns (source/allowed_tools/output_contract)."""
        columns = {
            row["name"] for row in self.db.execute("PRAGMA table_info(goals)")
        }
        if "source" not in columns:
            return
        with self.db:
            self.db.execute("ALTER TABLE goals RENAME TO goals_old")
            self.db.execute(
                "CREATE TABLE goals ("
                "id TEXT PRIMARY KEY, objective TEXT NOT NULL, "
                "acceptance_json TEXT NOT NULL, input_json TEXT NOT NULL, "
                "status TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL)"
            )
            self.db.execute(
                "INSERT INTO goals(id,objective,acceptance_json,input_json,status,"
                "created_at,updated_at) "
                "SELECT id,objective,acceptance_json,input_json,status,"
                "created_at,updated_at FROM goals_old"
            )
            self.db.execute("DROP TABLE goals_old")

    # ── context ──────────────────────────────────────────

    async def load_context(self, key: str) -> dict:
        row = self.db.execute(
            "SELECT context_json FROM contexts WHERE key=?", (key,)
        ).fetchone()
        if not row:
            return {}
        try:
            value = json.loads(row["context_json"])
        except (TypeError, json.JSONDecodeError):
            return {}
        return value if isinstance(value, dict) else {}

    async def save_context(self, key: str, context: dict) -> None:
        with self.db:
            self.db.execute(
                "INSERT INTO contexts(key,context_json,updated_at) VALUES(?,?,?) "
                "ON CONFLICT(key) DO UPDATE SET "
                "context_json=excluded.context_json, updated_at=excluded.updated_at",
                (key, json.dumps(context, ensure_ascii=False), _now()),
            )

    # ── goals ────────────────────────────────────────────

    def _row_to_goal(self, row: sqlite3.Row) -> agent.models.Goal:
        return agent.models.Goal(
            id=row["id"],
            objective=row["objective"],
            acceptance=json.loads(row["acceptance_json"]),
            input=json.loads(row["input_json"]),
            status=row["status"],
            store=self,
        )

    async def add_goal(self, goal: agent.models.Goal) -> None:
        timestamp = _now()
        with self.db:
            self.db.execute(
                "INSERT INTO goals(id,objective,acceptance_json,input_json,status,"
                "created_at,updated_at) VALUES(?,?,?,?,?,?,?) "
                "ON CONFLICT(id) DO UPDATE SET "
                "objective=excluded.objective, "
                "acceptance_json=excluded.acceptance_json, input_json=excluded.input_json, "
                "status=excluded.status, updated_at=excluded.updated_at",
                (
                    goal.id,
                    goal.objective,
                    json.dumps(goal.acceptance, ensure_ascii=False),
                    json.dumps(goal.input, ensure_ascii=False),
                    goal.status,
                    timestamp,
                    timestamp,
                ),
            )

    async def next_unfinished_goal(self) -> agent.models.Goal | None:
        row = self.db.execute(
            "SELECT * FROM goals WHERE status != 'completed' "
            "ORDER BY created_at, rowid LIMIT 1"
        ).fetchone()
        return self._row_to_goal(row) if row else None

    async def complete_goal(self, goal_id: str, result: agent.models.AgentResult) -> None:
        context = await self.load_context(goal_id)
        context.update(result.context_updates)
        if result.output is not None:
            context["last_output"] = result.output
        await self.save_context(goal_id, context)
        if context.get("satisfied"):
            with self.db:
                self.db.execute(
                    "UPDATE goals SET status='completed', updated_at=? WHERE id=?",
                    (_now(), goal_id),
                )

    def close(self) -> None:
        self.db.close()
