"""Agent 提示词：收尾规则 + 领域提示。

背景（为什么收尾规则要写这么硬）：换基座前，提示词里没有"够了就交"的硬约束，
模型会一直检索、核验同一句引文，直到步数用尽都不交卷 —— 4 段全部失败。
现在这套规则是把当时的实测症状逐条针对性地堵掉。

注意：这套规则是给**原生 function calling** 写的。smolagents 自带的
toolcalling_agent.yaml 教的是老式文本格式（`Action:` + JSON 块 + `Observation:`），
而实现走的是原生 function calling、历史消息又被文本化，两边对不上正是"不交卷"的根因。
所以生产基座（agent_engine/agents_sdk.py）直接用自己的提示词，不用框架的模板。
"""

# 收尾规则单独成段，位置在领域提示词之前。
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


def build_instructions(spec, *, tool_budget: int, submit_name: str | None = None) -> str:
    """领域提示词 + 收尾规则。

    提示词里的提交工具名默认就是真实名字。legacy 的 smolagents 基座要传
    submit_name="final_answer" —— 它只认这个名字当终止信号。
    """
    submit = submit_name or spec.submit.name
    domain = spec.prompt.replace(spec.submit.name, submit)
    return RULES.format(submit=submit, tool_budget=tool_budget) + "\n\n" + domain
