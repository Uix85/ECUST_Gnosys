/* Agent run panel and note generation triggers. */
'use strict';
async function generate(body) {
    const response = await api(agentBase, body);
    if (!response.id) { toast('所选范围已有解释，无需补齐'); return; }
    if (response.layer === 'L4' && response.target && state.note?.layer === 'L4') {
        state.note = await api(base + '/note/read', {layer:'L4', target:response.target});
        $('note-editor').value = state.note.content;
    }
    panel('runs'); watchRun(response.id); await refreshRuns();
}
function watchRun(id) {
    if (runStream) runStream.close();
    runStream = new EventSource(agentBase + '/' + id + '/events');
    runStream.addEventListener('update', e => { const x = JSON.parse(e.data); const card = $('run-' + id); if (card) { let status = card.querySelector('.run-status'); if (status) status.textContent = x.message; } });
    runStream.addEventListener('end', () => { runStream.close(); refreshRuns().catch(e => toast(e.message)); if (!state.dirty && state.note) openNote(state.note.layer, state.note.target).then(() => panel('runs')).catch(e => toast(e.message)); refreshChapter().catch(e => toast(e.message)); });
}
async function refreshRuns() {
    const [data, pending] = await Promise.all([api(agentBase), api(agentBase + '/pending')]); $('runs-list').replaceChildren(); $('pending-list').replaceChildren();
    if (!data.items.length) $('runs-list').append(el('p', 'muted', '所有 AI 生成均由你主动触发。'));
    for (const run of data.items) {
        const card = el('div', 'run-card'); card.id = 'run-' + run.id;
        card.append(el('header', '', (run.layer || 'L3') + ' · ' + run.status), el('p', 'run-status', run.error || (run.coverage ? '已覆盖 ' + run.coverage.length + ' 个原文块' : JSON.stringify(run.counts))));
        if (run.layer && run.candidate) { const details = el('details'); details.append(el('summary', '', '查看候选稿')); details.append(mdBox(run.candidate.content)); card.append(details); }
        if (['queued','running','canceling'].includes(run.status)) { card.append(button('取消', async () => { await api(agentBase + '/' + run.id + '/cancel', {}); refreshRuns(); })); if (!runStream || runStream.readyState === 2) watchRun(run.id); }
        else if (run.status !== 'completed') card.append(button('重试', async () => { const next = await api(agentBase + '/' + run.id + '/retry', {}); if (next.id) watchRun(next.id); refreshRuns(); }));
        $('runs-list').append(card);
    }
    for (const draft of pending.drafts || []) {
        const card = el('div','run-card'); card.append(el('header','',draft.layer + ' 待核稿 · ' + draft.state), mdBox(draft.result.content));
        card.append(button('采纳', async () => { await api(agentBase + '/' + draft.run_id + '/adopt', {draft_id:draft.id}); await refreshRuns(); await refreshChapter(); toast('已采纳'); }), button('导出候选稿', () => download(draft.result.content,'候选稿.md'))); $('pending-list').append(card);
    }
    for (const draft of pending.items || []) {
        const card = el('div','run-card'); card.append(el('header','','语义层待核稿 · 第 ' + (draft.chapter_idx+1) + ' 章'), mdBox(draft.detail || draft.short));
        card.append(button('原文', () => goto(draft.chapter_idx,draft.paragraph_index)), button('采纳', async () => { await api(agentBase + '/' + draft.run_id + '/adopt',{task_id:draft.task_id}); await refreshRuns(); await refreshChapter(); })); $('pending-list').append(card);
    }
}
