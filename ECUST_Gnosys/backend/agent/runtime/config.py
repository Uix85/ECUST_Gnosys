"""Configuration for the local reading agent."""

import sys
from dataclasses import dataclass
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[2]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from db import settings as settings_db
from db.paths import ROOT_DIR, SETTINGS_DB


@dataclass(frozen=True)
class AgentConfig:
    """运行期只读配置；由 load_config 从 SQLite 设置构造。"""

    base_url: str
    model: str
    api_key: str
    request_timeout: float
    max_requests: int
    max_tool_calls: int
    run_timeout: float
    max_parallel: int
    max_retries: int = 2            # 单次模型请求的重试次数（429/5xx/超时）
    session_id: str = ""            # 带 x-opencode-session 头（opencode.ai 的 Go 订阅路由强制要求）
    disable_thinking: bool = False  # 关掉模型的思考模式（省 token 与时间）


def load_config() -> AgentConfig:
    """读取 SQLite 设置并构造 AgentConfig；所有缺省值集中在这里。"""
    values = _database_values()
    return AgentConfig(
        base_url=values["AGENT_API_BASE_URL"].rstrip("/"),
        model=values["AGENT_MODEL"],
        api_key=values["AGENT_API_KEY"],
        request_timeout=float(values["AGENT_REQUEST_TIMEOUT"]),
        max_requests=int(values["AGENT_MAX_REQUESTS"]),
        max_tool_calls=int(values["AGENT_MAX_TOOL_CALLS"]),
        run_timeout=float(values["AGENT_RUN_TIMEOUT"]),
        max_parallel=int(values["AGENT_MAX_PARALLEL"]),
        max_retries=int(values["AGENT_MAX_RETRIES"]),
        session_id=values["AGENT_SESSION_ID"],
        disable_thinking=values["AGENT_DISABLE_THINKING"].strip() in ("1", "true", "True"),
    )


# ── 设置页：可编辑的键（新增配置项只改这里）──────────────────

@dataclass(frozen=True)
class SettingField:
    """设置页可编辑项。

    kind 决定校验与前端控件类型；secret 表示快照不回显明文。
    """

    key: str
    label: str
    default: str
    kind: str = "text"          # text / int / float / url / bool / secret
    help: str = ""
    secret: bool = False


FIELDS: tuple[SettingField, ...] = (
    SettingField("AGENT_API_BASE_URL", "服务地址", "https://api.openai.com/v1", "url",
             "OpenAI 兼容的 Chat Completions 端点"),
    SettingField("AGENT_MODEL", "模型名称", "gpt-4.1-mini", "text", "服务商或中转的模型名"),
    SettingField("AGENT_API_KEY", "API 密钥", "", "secret", "留空表示不修改", True),
    SettingField("AGENT_REQUEST_TIMEOUT", "单次请求超时（秒）", "90", "float"),
    SettingField("AGENT_MAX_REQUESTS", "单个任务请求上限", "12", "int", "模型往返次数"),
    SettingField("AGENT_MAX_TOOL_CALLS", "单个任务工具上限", "20", "int"),
    SettingField("AGENT_RUN_TIMEOUT", "单个任务最长运行（秒）", "300", "float"),
    SettingField("AGENT_MAX_RETRIES", "失败重试次数", "2", "int", "429 / 5xx / 超时"),
    SettingField("AGENT_MAX_PARALLEL", "同时运行的段落任务数", "2", "int"),
    SettingField("AGENT_SESSION_ID", "会话标识头", "", "text", "部分中转要求带 x-opencode-session；留空则不发"),
    SettingField("AGENT_DISABLE_THINKING", "关闭模型思考模式", "0", "bool", "省 token 与时间；部分模型不支持"),
)
FIELD_BY_KEY = {field.key: field for field in FIELDS}


def _database_values() -> dict[str, str]:
    values = settings_db.load()
    defaults = {field.key: field.default for field in FIELDS}
    missing = {key: value for key, value in defaults.items() if key not in values}
    if missing:
        settings_db.save(missing)
        values.update(missing)
    return values


def _normalize(field: SettingField, raw: str) -> str:
    """按 kind 校验并规范化输入值；非法值抛 ValueError（消息会直接展示给用户）。"""
    value = (raw or "").strip()
    if field.kind == "int":
        if not value.isdigit() or int(value) < 1:
            raise ValueError(f"{field.label}必须是不小于 1 的整数")
        return str(int(value))
    if field.kind == "float":
        try:
            number = float(value)
        except ValueError:
            raise ValueError(f"{field.label}必须是数字") from None
        if number <= 0:
            raise ValueError(f"{field.label}必须大于 0")
        return f"{number:g}"
    if field.kind == "url":
        if not value.startswith(("http://", "https://")):
            raise ValueError(f"{field.label}必须以 http:// 或 https:// 开头")
        return value.rstrip("/")
    if field.kind == "bool":
        return "1" if value.lower() in ("1", "true", "on", "yes") else "0"
    if field.key == "AGENT_MODEL" and not value:
        raise ValueError("模型名称不能为空")
    return value


def settings_snapshot() -> dict:
    """设置页用：可编辑项、生效值、来源；密钥不回传明文。"""
    database_values = _database_values()
    items = []
    for field in FIELDS:
        effective = database_values[field.key]
        item = {"key": field.key, "label": field.label, "kind": field.kind,
                "help": field.help, "default": field.default, "source": "database"}
        if field.secret:
            item.update({"set": bool(effective), "hint": (
                f"已设置（{len(effective)} 字符）" if effective else "未设置")})
        else:
            item["value"] = effective
        items.append(item)
    return {"items": items, "settings_db": str(SETTINGS_DB),
            "ready": bool(database_values["AGENT_API_KEY"].strip()),
            "model": database_values["AGENT_MODEL"],
            "base_url": database_values["AGENT_API_BASE_URL"]}


def save_settings(values: dict) -> dict:
    """校验并写入 SQLite。密钥留空 = 不改。

    未知键直接忽略；所有值先经 _normalize 校验，任一非法即整体失败、不写盘。
    """
    if not isinstance(values, dict):
        raise ValueError("values 必须是对象")
    updates = {}
    for key, raw in values.items():
        field = FIELD_BY_KEY.get(key)
        if field is None:
            continue
        text = "" if raw is None else str(raw)
        if field.secret and not text.strip():
            continue
        updates[key] = _normalize(field, text)
    if updates:
        settings_db.save(updates)
    return settings_snapshot()


def _check_database() -> None:
    """设置数据库自检，仅使用临时目录。"""
    import tempfile

    old_path = settings_db.SETTINGS_DB
    with tempfile.TemporaryDirectory() as tmp:
        settings_db.SETTINGS_DB = Path(tmp) / "settings.db"
        try:
            assert settings_db.load() == {}
            settings_db.save({"AGENT_MODEL": "new", "AGENT_API_KEY": "test-key"})
            assert settings_db.load() == {
                "AGENT_MODEL": "new", "AGENT_API_KEY": "test-key"
            }
        finally:
            settings_db.SETTINGS_DB = old_path


if __name__ == "__main__":                       # ponytail: 一条可运行的自检
    assert _normalize(FIELD_BY_KEY["AGENT_MAX_REQUESTS"], "3") == "3"
    assert _normalize(FIELD_BY_KEY["AGENT_DISABLE_THINKING"], "true") == "1"
    assert _normalize(FIELD_BY_KEY["AGENT_API_BASE_URL"], "https://x.test/v1/") == "https://x.test/v1"
    for key, bad in (("AGENT_MAX_REQUESTS", "0"), ("AGENT_REQUEST_TIMEOUT", "abc"),
                     ("AGENT_API_BASE_URL", "x.test"), ("AGENT_MODEL", "  ")):
        try:
            _normalize(FIELD_BY_KEY[key], bad)
        except ValueError:
            pass
        else:
            raise AssertionError(f"{key} 应该拒绝 {bad!r}")
    snap = settings_snapshot()
    assert {item["key"] for item in snap["items"]} == set(FIELD_BY_KEY)
    assert all("value" not in item for item in snap["items"] if item["kind"] == "secret")
    _check_database()
    print("agent.runtime.config 自检通过；当前模型：", snap["model"] or "(未配置)")
