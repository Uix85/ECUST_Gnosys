"""验证 Agent 端点：能否对话、能否 tool calling、工具结果能否回填。

用途：换基座/换模型前先跑一遍。smolagents 的 ToolCallingAgent 与 agent_core
都依赖 Chat Completions 的函数调用，端点不通就没必要往下做。

用法：
    uv run python tools/check_agent_endpoint.py                      # 用 .env 里的模型
    uv run python tools/check_agent_endpoint.py qwen3.8-flash gpt-5.4-mini
"""

import json
import sys
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
BROWSER_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")

TOOLS = [{"type": "function", "function": {
    "name": "get_book_outline", "description": "查看当前书的逻辑章节目录。",
    "parameters": {"type": "object", "properties": {}, "required": [],
                   "additionalProperties": False}}}]

PLAIN = [{"role": "user", "content": "只回答两个字：收到"}]
TOOL_PROMPT = [
    {"role": "system", "content": "要回答问题必须先调用工具，然后按工具结果回答。"},
    {"role": "user", "content": "这本书有哪些章？必须调用 get_book_outline。"}]


def session_header(env):
    """opencode.ai 的 Go 订阅路由要求 x-opencode-session，缺了会 MissingSessionID。"""
    return {"x-opencode-session": env["AGENT_SESSION_ID"]} if env.get("AGENT_SESSION_ID") else {}


def read_env():
    values = {}
    for name in (".env", ".env.secrets"):
        path = ROOT / name
        if not path.is_file():
            continue
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                values[k.strip()] = v.strip().strip('"').strip("'")
    return values


def probe(client, url, headers, label, payload):
    started = time.monotonic()
    try:
        response = client.post(url, headers=headers, json=payload)
    except Exception as exc:
        return {"label": label, "ok": False, "detail": f"{type(exc).__name__}: {exc}"}
    elapsed = time.monotonic() - started
    if response.is_error:
        return {"label": label, "ok": False, "detail": f"HTTP {response.status_code} {response.text[:200]}"}
    return {"label": label, "ok": True, "detail": response.json(), "seconds": elapsed}


def check_model(client, base_url, headers, model):
    url = f"{base_url}/chat/completions"
    result = {"model": model}

    plain = probe(client, url, headers, "plain", {"model": model, "messages": PLAIN})
    if not plain["ok"]:
        result["verdict"] = "FAIL 对话失败"
        result["why"] = plain["detail"]
        return result
    result["plain_seconds"] = round(plain.get("seconds", 0), 2)

    call = probe(client, url, headers, "tools", {"model": model, "messages": TOOL_PROMPT,
                                                "tools": TOOLS, "tool_choice": "auto"})
    if not call["ok"]:
        result["verdict"] = "FAIL 带 tools 的请求被拒"
        result["why"] = call["detail"]
        return result
    calls = (call["detail"].get("choices") or [{}])[0].get("message", {}).get("tool_calls") or []
    if not calls:
        content = (call["detail"].get("choices") or [{}])[0].get("message", {}).get("content")
        result["verdict"] = "FAIL 不支持 tool calling（模型只回文本）"
        result["why"] = f"content={str(content)[:120]!r}"
        return result

    result["tool_call_seconds"] = round(call.get("seconds", 0), 2)
    call_id = calls[0].get("id", "")
    args = calls[0].get("function", {}).get("arguments")
    result["tool_name"] = calls[0].get("function", {}).get("name")
    result["arguments_is_json"] = _is_json(args)
    result["has_call_id"] = bool(call_id)

    messages = TOOL_PROMPT + [
        {"role": "assistant", "content": None, "tool_calls": calls},
        {"role": "tool", "tool_call_id": call_id,
         "content": json.dumps([{"chapter_idx": 0, "title": "序言"}, {"chapter_idx": 1, "title": "第一章"}],
                               ensure_ascii=False)}]
    back = probe(client, url, headers, "roundtrip", {"model": model, "messages": messages, "tools": TOOLS})
    if not back["ok"]:
        result["verdict"] = "FAIL 工具结果回填失败"
        result["why"] = back["detail"]
        return result
    final = (back["detail"].get("choices") or [{}])[0].get("message", {}).get("content")
    result["final_seconds"] = round(back.get("seconds", 0), 2)
    if not final:
        result["verdict"] = "FAIL 回填后没有最终回答"
        return result
    result["final_answer"] = str(final)[:80]
    result["verdict"] = "PASS 支持 tool calling"
    return result


def _is_json(value):
    if isinstance(value, dict):
        return True
    try:
        json.loads(value or "")
        return True
    except Exception:
        return False


def main():
    env = read_env()
    base_url = env.get("AGENT_API_BASE_URL", "").rstrip("/")
    api_key = env.get("AGENT_API_KEY", "")
    models = sys.argv[1:] or [env.get("AGENT_MODEL", "")]
    models = [m for m in models if m]
    if not base_url or not api_key:
        print("请先在 .env / .env.secrets 里配好 AGENT_API_BASE_URL 和 AGENT_API_KEY")
        return 1
    print(f"端点: {base_url}   模型: {', '.join(models)}\n")

    for ua_label, headers in (("带浏览器 UA", {"Authorization": f"Bearer {api_key}",
                                            "User-Agent": BROWSER_UA, "Accept": "application/json",
                                            **session_header(env)}),
                              ("默认 UA(app 当前行为)", {"Authorization": f"Bearer {api_key}",
                                                    "Content-Type": "application/json",
                                                    **session_header(env)})):
        print(f"════ {ua_label} ════")
        with httpx.Client(timeout=90) as client:
            for model in models:
                r = check_model(client, base_url, headers, model)
                line = f"  {r['model']:<24} {r['verdict']}"
                print(line)
                if r.get("plain_seconds") is not None:
                    print(f"      纯对话 {r['plain_seconds']}s", end="")
                if r.get("tool_call_seconds") is not None:
                    print(f" · 工具调用 {r['tool_call_seconds']}s", end="")
                if r.get("final_seconds") is not None:
                    print(f" · 回填 {r['final_seconds']}s", end="")
                print()
                for k in ("tool_name", "arguments_is_json", "has_call_id", "final_answer", "why"):
                    if k in r:
                        print(f"      {k}: {r[k]}")
        print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
