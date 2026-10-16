"""Configuration for the local reading agent."""

import os
from dataclasses import dataclass
from pathlib import Path


ROOT_DIR = Path(__file__).resolve().parents[1]



def _read_env(path: Path) -> dict[str, str]:
    values = {}
    if not path.is_file():
        return values
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def _merged_values() -> dict[str, str]:
    """合并 .env / .env.secrets / 进程环境变量（后者优先）。"""
    values = _read_env(ROOT_DIR / ".env")
    values.update(_read_env(ROOT_DIR / ".env.secrets"))
    values.update(os.environ)
    return values


@dataclass(frozen=True)
class AgentConfig:
    base_url: str
    model: str
    api_key: str
    request_timeout: float
    max_requests: int
    max_tool_calls: int
    run_timeout: float
    max_parallel: int
    max_retries: int = 2            # 单次模型请求的重试次数（429/5xx/超时）
    retry_base_delay: float = 2.0   # 退避基数（秒），按 2 的幂增长
    session_id: str = ""            # 带 x-opencode-session 头（opencode.ai 的 Go 订阅路由强制要求）
    disable_thinking: bool = False  # 关掉模型的思考模式（省 token 与时间）


def load_config() -> AgentConfig:
    values = _merged_values()
    return AgentConfig(
        base_url=values.get("AGENT_API_BASE_URL", "https://api.openai.com/v1").rstrip("/"),
        model=values.get("AGENT_MODEL", "gpt-4.1-mini"),
        api_key=values.get("AGENT_API_KEY", ""),
        request_timeout=float(values.get("AGENT_REQUEST_TIMEOUT", "90")),
        max_requests=int(values.get("AGENT_MAX_REQUESTS", "12")),
        max_tool_calls=int(values.get("AGENT_MAX_TOOL_CALLS", "20")),
        run_timeout=float(values.get("AGENT_RUN_TIMEOUT", "300")),
        max_parallel=int(values.get("AGENT_MAX_PARALLEL", "2")),
        max_retries=int(values.get("AGENT_MAX_RETRIES", "2")),
        retry_base_delay=float(values.get("AGENT_RETRY_BASE_DELAY", "2")),
        session_id=values.get("AGENT_SESSION_ID", ""),
        disable_thinking=values.get("AGENT_DISABLE_THINKING", "0").strip() in ("1", "true", "True"),
    )
