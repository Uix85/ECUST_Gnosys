"""Environment settings for the terminal agent."""

import dataclasses
import os
import pathlib

DEFAULT_MODEL = "gpt-4.1-mini"
DEFAULT_DB = pathlib.Path.home() / ".terminal-agent" / "agent.db"


def _env(*names: str) -> str:
    for name in names:
        value = os.getenv(name)
        if value:
            return value
    return ""


@dataclasses.dataclass(frozen=True)
class Settings:
    api_key: str
    base_url: str | None
    model: str
    db_path: pathlib.Path
    session_id: str
    request_timeout: float = 90.0


def load_settings(
    *,
    session_id: str | None = None,
    db_path: str | None = None,
    model: str | None = None,
    base_url: str | None = None,
) -> Settings:
    resolved_base = base_url or _env(
        "AGENT_API_BASE_URL", "OPENAI_BASE_URL"
    ) or None
    if resolved_base:
        resolved_base = resolved_base.rstrip("/")

    resolved_db = pathlib.Path(
        db_path or _env("AGENT_DB_PATH") or DEFAULT_DB
    ).expanduser()
    resolved_db.parent.mkdir(parents=True, exist_ok=True)

    return Settings(
        api_key=_env("AGENT_API_KEY", "OPENAI_API_KEY"),
        base_url=resolved_base,
        model=model or _env("AGENT_MODEL", "OPENAI_MODEL") or DEFAULT_MODEL,
        db_path=resolved_db,
        session_id=session_id or _env("AGENT_SESSION_ID") or "default",
        request_timeout=float(_env("AGENT_REQUEST_TIMEOUT") or "90"),
    )
