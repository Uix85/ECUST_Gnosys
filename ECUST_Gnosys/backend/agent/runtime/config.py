"""Configuration for the local reading agent."""

import os
from dataclasses import dataclass
from pathlib import Path


ROOT_DIR = Path(__file__).resolve().parents[3]
ENV_FILE = ROOT_DIR / ".env"
SECRETS_FILE = ROOT_DIR / ".env.secrets"


def _read_env(path: Path) -> dict[str, str]:
    """解析 KEY=VALUE 形式的 env 文件；忽略空行与 # 注释，去掉包裹的引号。"""
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
    values = _read_env(ENV_FILE)
    values.update(_read_env(SECRETS_FILE))
    values.update(os.environ)
    return values


@dataclass(frozen=True)
class AgentConfig:
    """运行期只读配置；由 load_config 从合并后的环境值构造。"""

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
    """读取合并后的环境值并构造 AgentConfig；所有缺省值集中在这里。"""
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
        session_id=values.get("AGENT_SESSION_ID", ""),
        disable_thinking=values.get("AGENT_DISABLE_THINKING", "0").strip() in ("1", "true", "True"),
    )


# ── 设置页：可编辑的键（新增配置项只改这里）──────────────────

@dataclass(frozen=True)
class EnvField:
    """设置页可编辑项。

    `kind` 决定校验与前端控件类型（text / int / float / url / bool / secret）；
    `secret` 决定写入哪个文件（.env.secrets）以及快照中是否回显明文。
    """

    key: str
    label: str
    default: str
    kind: str = "text"          # text / int / float / url / bool / secret
    help: str = ""
    secret: bool = False


FIELDS: tuple[EnvField, ...] = (
    EnvField("AGENT_API_BASE_URL", "服务地址", "https://api.openai.com/v1", "url",
             "OpenAI 兼容的 Chat Completions 端点"),
    EnvField("AGENT_MODEL", "模型名称", "gpt-4.1-mini", "text", "服务商或中转的模型名"),
    EnvField("AGENT_API_KEY", "API 密钥", "", "secret", "只写入 .env.secrets；留空表示不修改", True),
    EnvField("AGENT_REQUEST_TIMEOUT", "单次请求超时（秒）", "90", "float"),
    EnvField("AGENT_MAX_REQUESTS", "单个任务请求上限", "12", "int", "模型往返次数"),
    EnvField("AGENT_MAX_TOOL_CALLS", "单个任务工具上限", "20", "int"),
    EnvField("AGENT_RUN_TIMEOUT", "单个任务最长运行（秒）", "300", "float"),
    EnvField("AGENT_MAX_RETRIES", "失败重试次数", "2", "int", "429 / 5xx / 超时"),
    EnvField("AGENT_MAX_PARALLEL", "同时运行的段落任务数", "2", "int"),
    EnvField("AGENT_SESSION_ID", "会话标识头", "", "text", "部分中转要求带 x-opencode-session；留空则不发"),
    EnvField("AGENT_DISABLE_THINKING", "关闭模型思考模式", "0", "bool", "省 token 与时间；部分模型不支持"),
)
FIELD_BY_KEY = {field.key: field for field in FIELDS}


def _file_for(field: EnvField) -> Path:
    """密钥类键写 .env.secrets，其余写 .env。"""
    return SECRETS_FILE if field.secret else ENV_FILE


def _source_of(key: str) -> str:
    """这个键的值实际来自哪里（进程环境优先级最高，与 _merged_values 一致）。"""
    if key in os.environ:
        return "env"
    if key in _read_env(SECRETS_FILE):
        return "secrets"
    if key in _read_env(ENV_FILE):
        return "dotenv"
    return "default"


def _normalize(field: EnvField, raw: str) -> str:
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


def _write_fields(values: dict[str, str]) -> list[str]:
    """把给定键写回对应文件。只动被管理的键，其它行（含注释）原样保留。"""
    written = []
    for path, group in ((ENV_FILE, [k for k in values if not FIELD_BY_KEY[k].secret]),
                        (SECRETS_FILE, [k for k in values if FIELD_BY_KEY[k].secret])):
        if not group:
            continue
        lines = path.read_text(encoding="utf-8").splitlines() if path.is_file() else []
        hit, out = set(), []
        for line in lines:
            stripped = line.strip()
            if stripped and not stripped.startswith("#") and "=" in stripped:
                key = stripped.split("=", 1)[0].strip()
                if key in group:
                    if key in hit:          # 重复键只保留一处
                        continue
                    hit.add(key)
                    out.append(f"{key}={values[key]}")
                    continue
            out.append(line)
        for key in group:
            if key in hit:
                continue
            field = FIELD_BY_KEY[key]
            if out and out[-1].strip():
                out.append("")
            if field.help:
                out.append(f"# {field.help}")
            out.append(f"{key}={values[key]}")
        path.write_text("\n".join(out).rstrip("\n") + "\n", encoding="utf-8")
        written.append(path.name)
    return written


def settings_snapshot() -> dict:
    """设置页用：可编辑项 + 生效值 + 来源 + 被进程环境覆盖的键。密钥不回传明文。

    来源优先级与 _merged_values 一致；`overridden` 列出被进程环境覆盖、因此
    在页面上改文件也不生效的键（API 密钥除外，避免泄露其来源）。
    """
    merged = _merged_values()
    items, overridden = [], []
    for field in FIELDS:
        source = _source_of(field.key)
        effective = merged.get(field.key, field.default)
        item = {"key": field.key, "label": field.label, "kind": field.kind,
                "help": field.help, "default": field.default, "source": source}
        if field.secret:
            item.update({"set": bool(effective), "hint": (
                f"已设置（{len(effective)} 字符）" if effective else "未设置")})
        else:
            item["value"] = effective
        if source == "env" and field.key != "AGENT_API_KEY":
            overridden.append(field.key)
        items.append(item)
    return {"items": items, "files": {"env": ENV_FILE.name, "secrets": SECRETS_FILE.name},
            "root": str(ROOT_DIR), "overridden": overridden,
            "ready": bool(merged.get("AGENT_API_KEY", "").strip()),
            "model": merged.get("AGENT_MODEL", ""), "base_url": merged.get("AGENT_API_BASE_URL", "")}


def save_settings(values: dict) -> dict:
    """校验并落盘。密钥留空 = 不改；其它键写 .env。返回新的快照。

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
        _write_fields(updates)
    return settings_snapshot()


def _check_writes() -> None:
    """写盘自检：只动被管理的键，注释与未知键原样保留，重复键去重。"""
    global ENV_FILE, SECRETS_FILE
    import tempfile
    lines = ["# 手写注释", "AGENT_MODEL=old", "OTHER_KEY=keep", "AGENT_MODEL=dup", ""]
    with tempfile.TemporaryDirectory() as tmp:
        ENV_FILE, SECRETS_FILE = Path(tmp) / ".env", Path(tmp) / ".env.secrets"
        ENV_FILE.write_text("\n".join(lines), encoding="utf-8")
        _write_fields({"AGENT_MODEL": "new", "AGENT_MAX_RETRIES": "3"})
        text = ENV_FILE.read_text(encoding="utf-8")
        assert "# 手写注释" in text and "OTHER_KEY=keep" in text
        assert text.count("AGENT_MODEL=") == 1 and "AGENT_MODEL=new" in text
        assert "AGENT_MAX_RETRIES=3" in text
        _write_fields({"AGENT_API_KEY": "sk-x"})
        assert SECRETS_FILE.read_text(encoding="utf-8").strip().endswith("AGENT_API_KEY=sk-x")


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
    _check_writes()
    print("agent.runtime.config 自检通过；当前模型：", snap["model"] or "(未配置)")
