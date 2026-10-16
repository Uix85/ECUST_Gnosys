"""【备份，未接入生产】OpenAI 兼容的 httpx 客户端。生产走 smolagents 的 OpenAIModel。

带重试与退避：批量跑（一本书上千段）时 429 / 5xx / 超时是常态，
一次抖动不该废掉一整段。重试等待期间响应取消。
"""

import asyncio

import httpx

from agent_config import AgentConfig
from agent_core import AgentCancelled, AgentError


# 可重试的 HTTP 状态：限流、上游抖动、网关错误
RETRY_STATUS = frozenset({408, 409, 425, 429, 500, 502, 503, 504})
# 重试过程中要还原成异常的类型
RETRY_EXCEPTIONS = (httpx.TimeoutException, httpx.TransportError)


class ChatCompletionsClient:
    def __init__(self, config: AgentConfig, transport: httpx.AsyncBaseTransport | None = None):
        self.config = config
        self._transport = transport          # 仅测试注入用
        self.usage = {"requests": 0, "input_tokens": 0, "output_tokens": 0, "total_tokens": 0}

    # ── 内部工具 ──────────────────────────────────────────
    def _delay(self, attempt: int) -> float:
        return max(0.0, self.config.retry_base_delay) * (2 ** (attempt - 1))

    async def _pause(self, seconds: float, cancelled: asyncio.Event) -> None:
        """可取消的等待。"""
        if seconds <= 0:
            if cancelled.is_set():
                raise AgentCancelled("任务已取消")
            return
        try:
            await asyncio.wait_for(cancelled.wait(), timeout=seconds)
        except asyncio.TimeoutError:
            return
        raise AgentCancelled("任务已取消")

    def _record_usage(self, data: dict) -> None:
        usage = data.get("usage") or {}
        self.usage["requests"] += 1
        self.usage["input_tokens"] += int(usage.get("prompt_tokens") or usage.get("input_tokens") or 0)
        self.usage["output_tokens"] += int(usage.get("completion_tokens") or usage.get("output_tokens") or 0)
        self.usage["total_tokens"] = self.usage["input_tokens"] + self.usage["output_tokens"]

    async def _post(self, client: httpx.AsyncClient, url: str, headers: dict, payload: dict,
                    cancelled: asyncio.Event) -> httpx.Response:
        request = asyncio.create_task(client.post(url, headers=headers, json=payload))
        abort = asyncio.create_task(cancelled.wait())
        try:
            done, _ = await asyncio.wait((request, abort), return_when=asyncio.FIRST_COMPLETED)
            if abort in done and cancelled.is_set():
                raise AgentCancelled("任务已取消")
            return await request
        finally:
            for pending in (request, abort):
                if not pending.done():
                    pending.cancel()
            await asyncio.gather(request, abort, return_exceptions=True)

    # ── 对外接口 ──────────────────────────────────────────
    async def complete(self, messages: list[dict], tools: list[dict], cancelled: asyncio.Event) -> dict:
        if not self.config.api_key and "localhost" not in self.config.base_url and "127.0.0.1" not in self.config.base_url:
            raise AgentError("缺少 AGENT_API_KEY，请在 .env.secrets 中配置")
        headers = {"Content-Type": "application/json"}
        if self.config.api_key:
            headers["Authorization"] = f"Bearer {self.config.api_key}"
        if self.config.session_id:
            headers["x-opencode-session"] = self.config.session_id
        payload = {"model": self.config.model, "messages": messages, "tools": tools, "tool_choice": "auto"}
        if self.config.disable_thinking:
            payload["enable_thinking"] = False      # 逐段解释不需要长思考，能省一半 token 与时间
        url = f"{self.config.base_url}/chat/completions"
        attempts = max(1, self.config.max_retries + 1)

        kwargs: dict = {"timeout": self.config.request_timeout}
        if self._transport is not None:
            kwargs["transport"] = self._transport
        async with httpx.AsyncClient(**kwargs) as client:
            last_error: AgentError | None = None
            for attempt in range(1, attempts + 1):
                if cancelled.is_set():
                    raise AgentCancelled("任务已取消")
                retryable = True
                try:
                    response = await self._post(client, url, headers, payload, cancelled)
                except AgentCancelled:
                    raise
                except httpx.TimeoutException:
                    last_error = AgentError(f"模型请求超时（{self.config.request_timeout:g} 秒）")
                except httpx.HTTPError as exc:
                    last_error = AgentError(f"模型连接失败：{type(exc).__name__}")
                else:
                    if response.status_code in RETRY_STATUS:
                        last_error = AgentError(f"模型接口返回 HTTP {response.status_code}")
                    elif response.is_error:
                        retryable = False        # 4xx 是我们的请求问题，重试没意义
                        raise AgentError(
                            f"模型接口返回 HTTP {response.status_code}：{response.text[:200]}")
                    else:
                        try:
                            data = response.json()
                        except ValueError as exc:
                            retryable = False
                            raise AgentError("模型接口返回了无效 JSON") from exc
                        self._record_usage(data)
                        return data
                if not retryable or attempt >= attempts:
                    raise last_error or AgentError("模型请求失败")
                await self._pause(self._delay(attempt), cancelled)
            raise last_error or AgentError("模型请求失败")
