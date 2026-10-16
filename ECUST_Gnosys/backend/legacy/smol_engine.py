"""smolagents 基座（已下线，备份）。生产路径现在是 agent_engine/agents_sdk.py。

下面是 smolagents 专属的提示词模板与占名常量，随本文件一起归档。

smolagents 基座：同一份 TaskSpec，改由 smolagents.ToolCallingAgent 执行。

映射关系（与 agent_core 对齐）：
  prompt              -> agent 的 instructions
  materials           -> agent.run(task) 的 task 字符串（JSON）
  工具（含技能的）      -> smolagents Tool 子类；async handler 跨线程桥回主事件循环
  submit_*  + validator -> submit 工具内直接校验（失败=抛错回给模型，与 agent_core 一致）
                          + final_answer_checks 兜底（没交过合规结果就不许结束）
  event               -> step_callbacks
  LoopBudget          -> max_steps（步数）+ step_callback 里查时限 + 工具计数器
  cancelled           -> agent.interrupt() + 抛 AgentCancelled
"""

import asyncio
import json
import time
from typing import Any, Callable

# smolagents 只把名为 final_answer 的工具当作终止信号
# （agents.py: `is_final_answer = tool_name == "final_answer"`），且 agent.tools 是 setdefault，
# 所以让提交工具占用这个名字 = “校验通过就立刻结束”。
SUBMIT_ALIAS = "final_answer"

SYSTEM_PROMPT = """你是一个中文人文社科学术文本的阅读辅助 Agent，为书稿段落撰写语义旁注。
你的产出必须通过调用提交工具交出去；没交出去的任务算失败。

可用工具（每一轮必须且只能调用其中一个）：
{%- for tool in tools.values() %}
- {{ tool.to_tool_calling_prompt() }}
{%- endfor %}

{{custom_instructions}}"""


# 收尾规则单独成段，位置在领域提示词之前、由 build_instructions 拼装。
# 这套规则针对实测症状：模型反复检索/核验同一句引文，直到步数用尽都不交卷。
RULES = """## 收尾规则（按此执行，不要自己发明流程）
1. 每一轮只调用一个工具，不要在一轮里同时调多个。
2. 先读后写。读原文用 read_book_paragraph，检索用 search_book_text，读已有笔记用 read_existing_note。
3. 引文必须先用 verify_source_quote 核验过一次才写进结果。核验不通过就改用自己的话复述，
   不要反复重试同一句引文。
4. 同一工具 + 同一组参数不要重复调用第二次；已经有结论就往前走。
5. 整个任务最多 {tool_budget} 次工具调用，超了直接失败。用掉一半还没开始动笔，就直接动笔：
   宁可写得保守一些，也不要因为“材料读得不够”而交不出稿。
6. 只要你能写出一份符合规范的结果，立刻调用 {submit} 交卷 —— 交卷即任务结束，
   不要再补做一次核验或补充阅读。
7. {submit} 返回错误说明时，含义是“改一处再交一次”：按错误信息修改后重新提交，
   不要重新从头探索。"""



def prompt_templates() -> dict:
    """三份 yaml 都能替，这里只替 toolcalling_agent 的系统提示词。

    planning / managed_agent / final_answer 目前用不到，但 MultiStepAgent.__init__
    会断言键齐全（agents.py:316），所以照原样留着。final_answer 那段只在
    步数用尽的兜底路径里用到（agents.py:827），而那条路径我们会直接判失败。
    """
    return {
        "system_prompt": SYSTEM_PROMPT,
        "planning": {"initial_plan": "", "update_plan_pre_messages": "", "update_plan_post_messages": ""},
        "managed_agent": {"task": "", "report": ""},
        "final_answer": {
            "pre_messages": "Agent 未能自行交卷，以下是它的全部交互记录：",
            "post_messages": "基于以上记录，回答下面这个任务：\n{{task}}",
        },
    }


from smolagents import LogLevel, Tool, ToolCallingAgent   # noqa: E402
from smolagents import AgentError as SmolAgentError
from smolagents.memory import ActionStep, PlanningStep

from agent_core import AgentCancelled, AgentError, BudgetExceeded, LoopBudget
from agent_engine.prompts import build_instructions
from agent_engine.spec import EventWriter, TaskSpec, ToolSpec


def build_model(config):
    """用 .env 里的配置造一个 smolagents 模型（OpenAI 兼容端点）。"""
    from smolagents import OpenAIServerModel
    # smolagents 用自己的 openai SDK 客户端，不走 agent_provider；
    # 所以请求超时与重试必须在这里补齐，否则 .env 里那两项在 smolagents 下是失效的。
    client_kwargs = {"timeout": config.request_timeout, "max_retries": config.max_retries}
    if config.session_id:      # opencode.ai 的 Go 路由要求带这个头，否则 MissingSessionID
        client_kwargs["default_headers"] = {"x-opencode-session": config.session_id}
    # tool_choice 策略：required 会强制每一步都调工具 —— 实测模型有时改用散文回答，
    # smolagents 解析不出 tool call 就报 "does not contain any JSON blob"，然后空转到步数用尽。
    # 但 required 与 thinking 模式冲突（Qwen 直接 400），所以只在关掉 thinking 时启用。
    kwargs = {}
    if config.disable_thinking:
        # openai SDK 不接受未知的 completion 参数，自定义字段要走 extra_body
        kwargs["extra_body"] = {"enable_thinking": False}
    tool_choice = "required" if config.disable_thinking else "auto"
    return OpenAIServerModel(model_id=config.model, api_base=config.base_url,
                             api_key=config.api_key, client_kwargs=client_kwargs,
                             tool_choice=tool_choice, **kwargs)


class _BridgedTool(Tool):
    """把 ToolSpec 的 async handler 包成 smolagents 能同步调用的 Tool。"""

    name = "bridged"
    description = ""
    inputs: dict = {}
    output_type = "object"
    skip_forward_signature_validation = True

    def __init__(self, spec: ToolSpec, call: Callable[[dict], Any]):
        super().__init__()
        self.name = spec.name
        self.description = spec.description
        self.inputs = spec.smol_inputs()
        self._call = call

    def forward(self, **kwargs):        # noqa: D102
        return self._call(kwargs)


async def run(
    spec: TaskSpec,
    *,
    model: Any,
    event: EventWriter,
    cancelled: Any,
    budget: LoopBudget | None = None,
    session: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """跑一个阶段。传 session 则跨阶段复用同一个 agent（记忆保留）。

    复合循环：smolagents 只有扁平单循环（交答案就结束），没有“生成→校验→复核→修订”
    这种编排。编排仍由 agent_runs 的状态机驱动，但**生成与修订共用同一个 agent**——
    `agent.run(reset=False)` 保留 memory（agents.py:468 起），于是材料只进 prompt 一次，
    修订时模型带着自己的探索记忆改稿，不用重新热身、也不必重读原文。
    复核阶段故意不传 session：独立上下文是刻意的质量设计，同一记忆里自我复核会盲从。
    """
    if model is None:
        raise ValueError("smolagents 基座需要传入 model")
    loop = asyncio.get_running_loop()
    if budget is None:
        budget = LoopBudget(spec.budget.max_requests, spec.budget.max_tool_calls,
                            time.monotonic() + spec.budget.timeout_seconds)
    cfg = spec.budget

    # 复用时 agent 与它的闭包都活着，所以闭包不能直接捕获 spec/state ——
    # 否则切到修订阶段时它们还指着生成阶段的校验器和提交结果。统一从 ctx 读。
    ctx: dict[str, Any] = session.get("ctx") if session and "ctx" in session \
        else {"spec": spec, "state": {}}
    ctx["spec"] = spec
    ctx["state"] = {}
    if session is not None:
        session["ctx"] = ctx

    def call_handler(tool_spec: ToolSpec, handler, args: dict):
        """在 worker 线程里调用 async handler：桥回主事件循环。

        与 legacy 的 custom 循环对齐：工具执行前后都查一次取消/时限（
        search_book_text 这类工具会扫全书，跑很久，不能只在“步”边界上才发现超时）；
        并把工具名与被读到的材料位置写进事件，前端/统计才看得到。
        """
        budget.check_running(cancelled)
        budget.tool_calls += 1
        if budget.tool_calls > budget.max_tool_calls:
            raise BudgetExceeded("工具调用次数已达上限")
        emit("tool", f"执行工具：{tool_spec.name}", {"tool": tool_spec.name})
        result = asyncio.run_coroutine_threadsafe(handler(args), loop).result()
        emit("material", "读取了带位置的书内材料", _material_refs(result))
        budget.check_running(cancelled)
        return result

    def make_tool(tspec: ToolSpec) -> _BridgedTool:
        return _BridgedTool(tspec, lambda args: call_handler(tspec, tspec.handler, args))

    def submit_call(args: dict):
        budget.check_running(cancelled)
        budget.tool_calls += 1
        if budget.tool_calls > budget.max_tool_calls:
            raise BudgetExceeded("工具调用次数已达上限")
        target = ctx["spec"].submit
        ctx["state"]["submitted"] = target.validator(args)   # 抛 ValueError = 反馈给模型重来
        return {"ok": True, "accepted": True}

    def _material_refs(result):
        refs, material_type = [], None
        if isinstance(result, dict):
            material_type = result.get("material_type")
            if result.get("reference"):
                refs.append(result["reference"])
        elif isinstance(result, list):
            refs = [item["reference"] for item in result
                    if isinstance(item, dict) and item.get("reference")]
            if refs:
                material_type = "original_text"
        return {"material_type": material_type, "references": refs} if refs else None

    def load_skill_call(args: dict):
        current = ctx["spec"]
        name = args.get("name")
        if name not in current.allowed_skills:
            raise ValueError("该技能不在本任务允许列表中")
        skill = current.skill(name)
        agent = agent_ref["agent"]
        added = []
        for tool_spec in skill.tools:
            if tool_spec.name not in agent.tools:
                agent.tools[tool_spec.name] = make_tool(tool_spec)
                added.append(tool_spec.name)
        return {"instructions": skill.instructions, "tools": added}

    def build_tools() -> list[Tool]:
        current = ctx["spec"]
        built: list[Tool] = [make_tool(t) for t in current.tools]
        built.append(_BridgedTool(
            ToolSpec(SUBMIT_ALIAS, current.submit.description, current.submit.parameters, None,
                     current.submit.required),
            submit_call))
        built.append(_BridgedTool(
            ToolSpec("load_skill", "读取一个允许使用的技能规程，并启用该技能提供的工具。",
                     {"name": {"type": "string", "description": "技能名"}}, load_skill_call, ["name"]),
            load_skill_call))
        return built

    def emit(stage: str, message: str, data: dict | None = None) -> None:
        asyncio.run_coroutine_threadsafe(event(stage, message, data), loop)
    def on_step(step, agent=None):          # step_callbacks 的签名是 (memory_step, agent=...)
        if cancelled.is_set():
            if agent is not None:
                agent.interrupt()
            raise AgentCancelled("任务已取消")
        if time.monotonic() >= budget.deadline:
            raise BudgetExceeded("任务超过总时限")
        if isinstance(step, (ActionStep, PlanningStep)):
            budget.requests += 1                 # 一个动作步 ≈ 一次模型请求
            usage = getattr(step, "token_usage", None)
            if usage is not None:
                budget.record_usage({"input_tokens": getattr(usage, "input_tokens", 0),
                                     "output_tokens": getattr(usage, "output_tokens", 0)})
            # 与 custom 基座对齐：同样的 model_response 事件与同样的用量字段
            emit("model_response", f"模型返回（累计 {budget.total_tokens} tokens）",
                 {"request": budget.requests, "input_tokens": budget.input_tokens,
                  "output_tokens": budget.output_tokens, "total_tokens": budget.total_tokens})
        emit("step", f"第 {getattr(step, 'step_number', '?')} 步",
             {"step": getattr(step, "step_number", None), "kind": type(step).__name__})
        # 与 legacy 对齐：把校验器退回的错误单独记一条，否则统计里看不到
        # “模型交了几次不合格的稿”（smolagents 把它藏在 action_step.error 里）
        error = getattr(step, "error", None)
        if error is not None:
            emit("tool_error", f"步骤被退回：{str(error)[:200]}", {"error": str(error)[:200]})

    def check_final(final_answer, memory, agent=None):
        # 兜底：若走了 max_steps 的兜底路径，这里没有交过合规结果
        if ctx["state"].get("submitted") is None:
            raise AssertionError("必须先调用提交工具交出通过校验的结果")
        return True

    remaining = max(0, budget.max_requests - budget.requests)
    if cfg.request_limit:
        remaining = min(remaining, cfg.request_limit)

    agent_ref: dict[str, Any] = {}
    agent = session.get("agent") if session else None
    reused = agent is not None
    if reused:
        # 换掉工具集与系统提示词（system_prompt 是只读 property，run() 每轮
        # 用它重建 SystemPromptStep（agents.py:477），所以改完下一轮就生效）。
        agent.tools = {tool.name: tool for tool in build_tools()}
        agent.instructions = build_instructions(spec, tool_budget=cfg.max_tool_calls, submit_name=SUBMIT_ALIAS)
        agent.prompt_templates = prompt_templates()
    else:
        agent = ToolCallingAgent(
            tools=build_tools(),
            model=model,
            prompt_templates=prompt_templates(),
            instructions=build_instructions(spec, tool_budget=cfg.max_tool_calls, submit_name=SUBMIT_ALIAS),
            max_steps=max(1, remaining),
            step_callbacks=[on_step],
            final_answer_checks=[check_final],
            return_full_result=True,
            verbosity_level=LogLevel.ERROR,
        )
        if session is not None:
            session["agent"] = agent
    agent_ref["agent"] = agent

    task = json.dumps(spec.materials, ensure_ascii=False)
    emit("run", ("复用同一 Agent 继续执行" if reused else "smolagents 基座开始执行"),
         {"layer": spec.layer, "reused": reused,
          "memory_steps": len(agent.memory.steps) if reused else 0})
    try:
        # reset=False 是复合循环的关键：不调 self.memory.reset()，历史工具调用与
        # 上一稿都留着，模型接着改稿而不是重新热身。
        result = await asyncio.to_thread(agent.run, task, reset=not reused, max_steps=max(1, remaining))
    except (AgentError, BudgetExceeded, AgentCancelled):
        raise
    except SmolAgentError as exc:
        if cancelled.is_set() or "interrupt" in str(exc).lower():
            raise AgentCancelled("任务已取消") from exc
        raise AgentError(str(exc)) from exc
    except Exception as exc:                                    # 步骤回调里抛出的预算/取消
        if cancelled.is_set():
            raise AgentCancelled("任务已取消") from exc
        if isinstance(exc, BudgetExceeded) or isinstance(exc.__cause__, BudgetExceeded):
            raise BudgetExceeded(str(exc)) from exc
        raise AgentError(f"smolagents 基座执行失败：{type(exc).__name__}: {exc}") from exc

    if getattr(result, "state", "success") == "max_steps_error":
        raise BudgetExceeded("步数用尽仍未交出合规结果")
    if ctx["state"].get("submitted") is None:
        raise AgentError("模型未调用提交结果工具")
    return ctx["state"]["submitted"]
