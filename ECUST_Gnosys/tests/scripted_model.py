"""脚本化的假模型：用 Agents SDK 的 Model 接口驱动测试，不需要 API key。

用法：给一个 holder（有 script 与 calls 两个 list）——
  holder["script"] 是待执行动作 [(工具名, 参数字典), ...]
  holder["calls"]  记录每次请求，供断言用
脚本用尽还要求继续就断言失败：那说明基座没有在应该停的时候停下。

与旧基座的两个假模型不同，这里**不需要改写提交工具名** ——
Agents SDK 用 tool_use_behavior 声明终止工具，不靠占 final_answer 这个名字。
"""

import json

from agents import ModelResponse, Usage
from agents.models.interface import Model
from openai.types.responses.response_function_tool_call import ResponseFunctionToolCall


class ScriptedModel(Model):
    def __init__(self, holder: dict, usage: dict | None = None):
        self.holder = holder
        self.usage = usage or {"prompt_tokens": 120, "completion_tokens": 30}

    async def get_response(self, system_instructions=None, input=None, model_settings=None,
                           tools=None, output_schema=None, handoffs=None, tracing=None, **kwargs):
        self.holder["calls"].append({
            "input": input, "instructions": system_instructions,
            "tools": [getattr(tool, "name", str(tool)) for tool in (tools or [])],
            "model_settings": model_settings,
        })
        if not self.holder["script"]:
            raise AssertionError("脚本用尽：模型被要求继续，但假模型没有后续动作")
        name, arguments = self.holder["script"].pop(0)
        index = len(self.holder["calls"])
        return ModelResponse(
            output=[ResponseFunctionToolCall(
                id=f"fc-{index}", call_id=f"call-{index}", name=name, type="function_call",
                arguments=arguments if isinstance(arguments, str)
                else json.dumps(arguments, ensure_ascii=False))],
            usage=Usage(requests=1, input_tokens=self.usage["prompt_tokens"],
                        output_tokens=self.usage["completion_tokens"]),
            response_id=None,
        )

    async def stream_response(self, *args, **kwargs):
        raise NotImplementedError("假模型不支持流式输出")


def new_script() -> dict:
    return {"script": [], "calls": []}


def request_text(record: dict) -> str:
    """把一次请求拍成文本，便于断言"材料/工具结果有没有进到模型看到的上下文里"。"""
    return json.dumps(record, ensure_ascii=False, default=str)
