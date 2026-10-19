"""L1 full-book coverage and the shared legacy chapter summarizer."""

from agent.tools.book import logical_paragraphs
from agent.tools.context import chapter_outline
from reader.workspace import chapters
from agent.tasks.policy import SECTIONS


async def generate(run):
    """L1 入口：对全书每一章跑覆盖式总结。"""
    return await summarize_coverage(run, range(len(chapters(run.book_id))))


async def summarize_coverage(run, chapter_indices):
    """逐章分块覆盖 → 分块分析 → 多轮归并 → 全章/全书的最终成稿。

    分块不是抽样：每个字符恰好落进一块，超长段落按 8000 字切片，
    从而保证「全书分析」确实覆盖全部原文。L2 的旧版（API）章节总结也复用它。
    """
    summaries = []
    for ci in chapter_indices:
        chapter, paras, _ = logical_paragraphs(run.book_id, ci)
        # 按 8000 字上限装箱；段落先切片再装箱，单段超窗也不会漏字。
        chunks, current, size = [], [], 0
        for p in paras:
            # Every character is covered, including a paragraph larger than a model window.
            for start in range(0, len(p['text']), 8000):
                fragment = {**p, 'text': p['text'][start:start+8000], 'offset': start}
                if size + len(fragment['text']) > 8000 and current:
                    chunks.append(current)
                    current, size = [], 0
                current.append(fragment)
                size += len(fragment['text'])
        if current:
            chunks.append(current)
        for k, chunk in enumerate(chunks):
            run.budget.check_running(run.signal)
            # 先把这块原文记进账本：后续引文核验只认已读账本。
            run.remember(ci, chunk)
            await run.event('coverage', f"读取第 {ci+1} 章 · {k+1}/{len(chunks)} 块", {'chapter_idx': ci, 'chunk': k, 'total_chunks': len(chunks)})
            material = {'chapter_idx': ci, 'title': chapter['title'], 'outline': chapter_outline(run.book_id, ci),
                        'source': [{'paragraph_index': p['paragraph_index'], 'offset': p['offset'], 'text': p['text']} for p in chunk]}
            result = await run.execute('依据这块完整原文进行分析。必须用 Markdown 标题分别写出：' + '、'.join(SECTIONS[run.layer]) + '。标注证据位置，区分原文与解释。论证区块中的每个区块用 [原文](#source=章序:段序) 指向其范围起点，编号从0开始。\n' + run.spec, material, final=True)
            summaries.append({'chapter_idx': ci, 'chunk': k, 'result': result})
            run.coverage.append({'chapter_idx': ci, 'chunk': k, 'total_chunks': len(chunks),
                                  'chars': sum(len(p['text']) for p in chunk), 'references': [{'chapter_idx': ci, 'paragraph_index': p['paragraph_index'], 'fingerprint': p['fingerprint']} for p in chunk], 'result': result})
            run.candidate = {'content': '\n\n'.join(x['result']['content'] for x in summaries),
                              'sources': [s for x in summaries for s in x['result']['sources']]}
            # 每完成一块就落盘：中途失败也保留已覆盖部分的候选稿。
            run.persist()
    if len(summaries) == 1:
        return run.candidate
    # 归并到 ≤4 份再交给最终成稿，避免最后一次提示词里塞进几十块摘要。
    while len(summaries) > 4:
        reduced = []
        for start in range(0, len(summaries), 4):
            group = summaries[start:start+4]
            result = await run.execute('合并这些已覆盖的原文块分析，保留核心问题、论证、章节关系、差异及可核验来源。不得扩大覆盖范围。', {'covered_chunks': group})
            reduced.append({'result': result})
        summaries = reduced
    purpose = ('全书层：核心问题、思想地图、论证主线、章节关系。必须依据所有章的完整覆盖记录，不能用目录抽样冒充全书分析。'
               if run.layer == 'L1' else '章节层：章级总纲、按原文范围组织论证区块、关键转折及承上启下关系。每个区块给可定位的原文证据。')
    return await run.execute(purpose + '\n必须包含这些 Markdown 标题：' + '、'.join(SECTIONS[run.layer]) + '。论证区块逐个附上可跳转的原文位置链接。\n按块分析是参考，逐字引文必须回原文核对。输出 Markdown。\n' + run.spec, {'covered_chunks': summaries}, final=True)
