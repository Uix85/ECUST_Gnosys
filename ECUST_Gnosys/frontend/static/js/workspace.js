(function () {
'use strict';
const cfg = window.GNOSYS, book = encodeURIComponent(cfg.bookId);
const base = '/api/workspace/' + book, agentBase = '/api/agent/' + book + '/runs', chatBase = '/api/chat/' + book;
const $ = id => document.getElementById(id);
const state = {chapter: cfg.chapter, nodes: [], paragraphs: [], references: [], session: '', messages: [], activeMessage: '',
    note: null, dirty: false, editing: false, selected: [], nav: 'outline', panel: 'chat', generation: 0,
    noteSentences: [],
    tocPinned: new Set(), tocClosed: new Set(), tocActive: new Set(), chapterNavLock: false};
let chatStream, toastTimer, progressTimer, tocTimer, runStream, mention = null, noteRequest = 0;
function toast(text) { $('toast').textContent = text; $('toast').hidden = false; clearTimeout(toastTimer); toastTimer = setTimeout(() => $('toast').hidden = true, 5500); }
async function api(url, body, method) {
    const options = body === undefined ? {} : {method: method || 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body)};
    if (method && body === undefined) options.method = method;
    const r = await fetch(url, options), data = await r.json();
    if (!r.ok) { const detail = data.detail; const error = new Error(typeof detail === 'string' ? detail : detail?.message || JSON.stringify(detail)); error.detail = detail; error.status = r.status; throw error; }
    return data;
}
function on(id, type, fn) { $(id).addEventListener(type, e => Promise.resolve(fn(e)).catch(error => toast(error.message))); }
function el(tag, cls, text) { const n = document.createElement(tag); if (cls) n.className = cls; if (text !== undefined) n.textContent = text; return n; }
function button(text, action, cls) { const n = el('button', cls, text); n.type = 'button'; n.onclick = e => Promise.resolve(action(e)).catch(error => toast(error.message)); return n; }
function mdBox(text) { const n = el('div', 'markdown'); n.innerHTML = renderMarkdown(text); return n; }
function download(text, name) { const a = document.createElement('a'); const u = URL.createObjectURL(new Blob([text], {type: 'text/markdown;charset=utf-8'})); a.href = u; a.download = name; a.click(); setTimeout(() => URL.revokeObjectURL(u), 1000); }
function baseline(note) { return {revision: note.revision, content: note.content, legacy_version: note.legacy_version || ''}; }
function canLeaveNote() { return !state.dirty || confirm('笔记有未保存修改。确定放弃这些修改吗？'); }
function panel(name) {
    state.panel = name; document.body.classList.remove('no-inspector'); $('toggle-panel').setAttribute('aria-expanded', 'true');
    document.querySelectorAll('[data-panel]').forEach(b => b.setAttribute('aria-selected', String(b.dataset.panel === name)));
    ['chat', 'note', 'runs'].forEach(p => $(p + '-panel').hidden = p !== name);
    if (name === 'runs') refreshRuns().catch(e => toast(e.message));
}
function tocNode(id) { return state.nodes.find(n => n.id === id); }
function tocHasChildren(id) { return state.nodes.some(n => n.parent_id === id); }
function tocExpanded(id) {
    if (state.tocClosed.has(id)) return false;
    if (state.tocPinned.has(id)) return true;
    return state.tocActive.has(id);
}
function tocVisible(node) {
    let parentId = node.parent_id;
    while (parentId) {
        const parent = tocNode(parentId);
        if (!parent || !tocExpanded(parentId)) return false;
        parentId = parent.parent_id;
    }
    return true;
}
function nodeButton(node) {
    const row = el('div', 'toc-row' + (state.tocActive.has(node.id) ? ' active' : '')); row.dataset.depth = node.depth; row.dataset.id = node.id;
    row.style.paddingLeft = Math.min(node.depth, 5) * 12 + 'px';
    const level = Math.min(node.depth, 4);
    row.style.setProperty('--toc-title-size', (13.5 - level * 0.5) + 'px');
    row.style.setProperty('--toc-fold-size', (15.5 - level * 0.5) + 'px');
    row.hidden = !tocVisible(node);
    if (tocHasChildren(node.id)) {
        const expanded = tocExpanded(node.id);
        const fold = button('', () => toggleToc(node.id), 'toc-fold');
        fold.setAttribute('aria-label', '折叠或展开 ' + node.title);
        fold.setAttribute('aria-expanded', String(expanded));
        fold.innerHTML = `<svg viewBox="0 0 24 24" aria-hidden="true"><path d="${expanded ? 'M6 9l6 6 6-6' : 'M9 6l6 6-6 6'}"/></svg>`;
        row.append(fold);
    } else row.append(el('span', 'toc-fold', ''));
    const link = button(node.title, () => goto(node.chapter_idx, node.paragraph_index)); link.title = node.path.join(' - '); row.append(link); return row;
}
function renderOutline() {
    const box = $('nav-results'); if (!box) return;
    const top = box.scrollTop, fragment = document.createDocumentFragment();
    state.nodes.forEach(n => fragment.append(nodeButton(n)));
    box.replaceChildren(fragment); box.scrollTop = top;
}
function toggleToc(id) {
    if (tocExpanded(id)) { state.tocPinned.delete(id); state.tocClosed.add(id); }
    else { state.tocClosed.delete(id); state.tocPinned.add(id); }
    renderOutline();
}
function updateTocActive(pi) {
    if (!Number.isInteger(pi)) return;
    const next = new Set(state.nodes.filter(n => n.chapter_idx === state.chapter && n.paragraph_index <= pi && pi < n.end).map(n => n.id));
    let same = next.size === state.tocActive.size;
    if (same) for (const id of next) { if (!state.tocActive.has(id)) { same = false; break; } }
    if (same) return;
    // 进入新的章/标题区间时，清除该区间的手动固定状态，恢复自动收展。
    for (const id of next) if (!state.tocActive.has(id)) { state.tocPinned.delete(id); state.tocClosed.delete(id); }
    state.tocActive = next;
    if (state.nav === 'outline') renderOutline();
}
function updateTocFromScroll() {
    const ref = currentPosition();
    if (ref) updateTocActive(ref.paragraph_index);
}
async function nav(name) {
    state.nav = name; $('search-form').hidden = name !== 'search';
    document.querySelectorAll('[data-nav]').forEach(b => b.setAttribute('aria-selected', String(b.dataset.nav === name)));
    $('nav-results').replaceChildren();
    if (name === 'outline') { updateTocFromScroll(); renderOutline(); }
    if (name === 'search') $('search-input').focus();
    if (name === 'bookmarks') {
        const data = await api(base + '/bookmarks');
        if (state.nav !== name) return;
        if (!data.items.length) $('nav-results').append(el('p', 'muted', '在阅读位置点击“添加书签”。'));
        data.items.forEach(item => {
            const row = el('div', 'bookmark-row');
            row.append(button((item.valid ? '' : '引用失效 · ') + item.label, () => jumpReference(item.reference)),
                button('删除', async () => { await api(base + '/bookmarks/' + item.id, undefined, 'DELETE'); nav('bookmarks'); }));
            $('nav-results').append(row);
        });
    }
}
function currentPosition() {
    const rect = $('reader-scroll').getBoundingClientRect();
    const visible = [...document.querySelectorAll('.source[data-pi]')].find(n => n.getBoundingClientRect().bottom > rect.top + 30);
    const p = state.paragraphs[visible ? Number(visible.dataset.pi) : 0];
    return p ? {chapter_idx: state.chapter, paragraph_index: p.paragraph_index, fingerprint: p.fingerprint, quote: p.text} : null;
}
// 与 backend/reader/auxiliary_reading.needs_semantic 保持同一判据：
// 太短、没有句末标点、或像标题/目录行的段落不显示“生成段落解释”。
function needsSemanticNote(text) {
    const raw = (text || '').trim();
    const normalized = raw.normalize('NFKC').replace(/\s+/g, '').toLowerCase();
    if (normalized.length < 25) return false;
    const term = /[\u4e00-\u9fff]/.test(raw) ? /[。！？]/ : /[.!?]/;
    if (!term.test(raw)) return false;
    const heading = /^(?:[\[［【(（]?第[一二三四五六七八九十百零两0-9]+[章节篇部分卷]|[一二三四五六七八九十]+[、．.]|[0-9]+[、．.)）])/;
    if (normalized.length <= 60 && heading.test(normalized)) return false;
    return true;
}
function jumpReaderScroll(top) {
    const scroller = $('reader-scroll'), previous = scroller.style.scrollBehavior;
    scroller.style.scrollBehavior = 'auto';
    scroller.scrollTop = top;
    scroller.style.scrollBehavior = previous;
}
async function goto(ci, pi = 0, updateHistory = true, edge = 'top') {
    if (ci < 0 || ci >= cfg.total) return;
    const chapterChanged = ci !== state.chapter;
    const version = ++state.generation;
    $('chapter-content').setAttribute('aria-busy', 'true');
    const data = await api(base + '/chapter/' + ci);
    if (version !== state.generation) return;
    state.chapter = ci; state.paragraphs = data.paragraphs;
    $('chapter-title').textContent = data.title; $('chapter-counter').textContent = (ci + 1) + ' / ' + data.total + ' 章';
    $('previous').disabled = ci === 0; $('next').disabled = ci + 1 === data.total;
    const fragment = document.createDocumentFragment(), titleKey = data.title.replace(/\s+/g, '');
    data.paragraphs.forEach(p => {
        const row = el('section', 'paragraph-row'), source = el('div', 'source');
        source.dataset.pi = p.paragraph_index; source.id = 'paragraph-' + p.paragraph_index; source.tabIndex = -1;
        if (p.paragraph_index === 0 && p.text.replace(/\s+/g, '') === titleKey) row.classList.add('chapter-heading-row');
        const tag = /^h[1-6]$/.test(p.tag) ? p.tag : p.tag === 'blockquote' ? 'blockquote' : 'p';
        const inner = el(tag); inner.innerHTML = DOMPurify.sanitize(p.html); source.append(inner);
        source.querySelectorAll('img').forEach(image => { const src = image.getAttribute('src'); if (src && !/^(https?:|\/)/i.test(src)) image.src = '/read/' + book + '/' + src; });
        const side = el('aside', 'semantic-note'); side.setAttribute('aria-label', '段落解释');
        if (p.note) {
            side.append(el('div', 'short', p.note.short || '查看解释'));
            side.append(button('详细 / 逐句', () => openSemantic(p)));
        } else if (needsSemanticNote(p.text)) side.append(button('生成段落解释', () => generate({layer:'L3', paragraph_ref:{chapter_idx:ci, paragraph_index:p.paragraph_index, fingerprint:p.fingerprint}, spec_type:$('agent-spec').value})));
        row.append(source, side); fragment.append(row);
    });
    data.images.forEach(markup => { const holder = el('div'); holder.innerHTML = DOMPurify.sanitize(markup); holder.querySelectorAll('img').forEach(img => { img.classList.add('book-image'); const src = img.getAttribute('src'); if (src && !/^(https?:|\/)/i.test(src)) img.src = '/read/' + book + '/' + src; }); fragment.append(holder); });
    $('chapter-content').replaceChildren(fragment); $('chapter-content').setAttribute('aria-busy', 'false');
    const landingAtBottom = chapterChanged && pi === 0 && edge === 'bottom';
    if (chapterChanged) jumpReaderScroll(landingAtBottom ? $('reader-scroll').scrollHeight : 0);
    else $('reader-scroll').scrollTop = 0;
    const landingPi = landingAtBottom ? (currentPosition()?.paragraph_index ?? 0) : pi;
    updateTocActive(landingPi);
    if (pi > 0 && state.paragraphs[pi]) locate(pi);
    if (updateHistory && !cfg.noteMode) history.replaceState({}, '', '/read/' + book + '/' + ci + '#source=' + ci + ':' + landingPi);
    clearTimeout(progressTimer); const ref = currentPosition(); if (ref) api(base + '/state', ref, 'PUT').catch(e => toast(e.message));
}
function locate(pi) {
    const target = $('paragraph-' + pi), scroller = $('reader-scroll');
    if (!target) return;
    const top = scroller.scrollTop + target.getBoundingClientRect().top - scroller.getBoundingClientRect().top;
    scroller.scrollTo({top, behavior: 'smooth'});
    document.querySelectorAll('.located').forEach(n => n.classList.remove('located')); target.classList.add('located'); setTimeout(() => target.classList.remove('located'), 2600);
}
async function jumpReference(ref) { const r = await api(base + '/reference/check', {references:[ref]}); const valid = r.items[0]; await goto(valid.chapter_idx, valid.paragraph_index); }
async function openNote(layer, target = {}) {
    if (!canLeaveNote()) return null;
    const id = ++noteRequest;
    const data = await api(base + '/note/read', {layer, target});
    if (id !== noteRequest) return null;
    state.note = data; state.dirty = false; state.editing = false; state.noteSentences = []; setNoteLayerUI(layer); $('note-editor').value = data.content;
    $('note-target').textContent = layer === 'L1' ? cfg.title : layer === 'chat' ? '对话摘记' : (state.nodes.find(n => n.id === 'c' + target.chapter_idx)?.title || '') + (target.paragraph_index !== undefined ? ' · 段落 ' + (target.paragraph_index + 1) : '') + (target.term ? ' · ' + target.term : '');
    $('note-save-state').textContent = ''; $('generate-note').disabled = layer === 'chat'; renderNote(); panel('note');
    return data;
}
function setNoteLayerUI(layer) {
    document.querySelectorAll('[data-note-layer]').forEach(b => b.setAttribute('aria-selected', String(b.dataset.noteLayer === layer)));
}
async function openSemantic(p) {
    const target = {chapter_idx:state.chapter, paragraph_index:p.paragraph_index, fingerprint:p.fingerprint};
    const data = await openNote('L3', target);
    if (!data || data.target.chapter_idx !== target.chapter_idx || data.target.paragraph_index !== target.paragraph_index) return;
    state.noteSentences = p.note?.sentences || [];
    renderNote();
}
async function openNoteLayer(layer) {
    if (layer === 'L1') return openNote(layer);
    if (layer === 'L2') return openNote(layer, {chapter_idx:state.chapter});
    const ref = state.selected[0] || currentPosition(); if (!ref) return;
    const target = {chapter_idx:ref.chapter_idx, paragraph_index:ref.paragraph_index, fingerprint:ref.fingerprint};
    if (layer === 'L4') { const term = state.selected[0]?.quote || prompt('输入当前位置要解释的术语'); if (!term) return; target.term = term; }
    const data = await openNote(layer, target);
    if (layer === 'L3' && data) {
        const p = state.paragraphs.find(x => x.paragraph_index === target.paragraph_index);
        state.noteSentences = p?.note?.sentences || [];
        renderNote();
    }
}
function renderNote() {
    const editing = state.editing;
    $('note-editor').hidden = !editing; $('note-preview').hidden = editing;
    $('edit-note').hidden = editing; $('save-note').hidden = !editing;
    const preview = $('note-preview');
    preview.innerHTML = renderMarkdown($('note-editor').value);
    if (state.note?.layer === 'L3' && state.noteSentences?.length) {
        preview.append(el('h4', '', '逐句解释'));
        state.noteSentences.forEach((s, index) => preview.append(mdBox((index + 1) + '. ' + s)));
    }
}
async function saveNote() {
    if (!state.note) return;
    try {
        const data = await api(base + '/note', {layer:state.note.layer, target:state.note.target, content:$('note-editor').value, baseline:baseline(state.note)}, 'PUT');
        state.note = data; state.dirty = false; state.editing = false; $('note-save-state').textContent = '已保存'; toast('笔记已保存'); renderNote();
    } catch (e) {
        if (e.status === 409) { $('note-save-state').textContent = '保存冲突 · 输入已保留'; download($('note-editor').value, '冲突笔记备份.md'); }
        throw e;
    }
}
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
    runStream.addEventListener('end', () => { runStream.close(); refreshRuns().catch(e => toast(e.message)); if (!state.dirty && state.note) openNote(state.note.layer, state.note.target).then(() => panel('runs')).catch(e => toast(e.message)); goto(state.chapter, currentPosition()?.paragraph_index || 0, false).catch(e => toast(e.message)); });
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
        card.append(button('采纳', async () => { await api(agentBase + '/' + draft.run_id + '/adopt', {draft_id:draft.id}); refreshRuns(); toast('已采纳'); }), button('导出候选稿', () => download(draft.result.content,'候选稿.md'))); $('pending-list').append(card);
    }
    for (const draft of pending.items || []) {
        const card = el('div','run-card'); card.append(el('header','','语义层待核稿 · 第 ' + (draft.chapter_idx+1) + ' 章'), mdBox(draft.detail || draft.short));
        card.append(button('原文', () => goto(draft.chapter_idx,draft.paragraph_index)), button('采纳', async () => { await api(agentBase + '/' + draft.run_id + '/adopt',{task_id:draft.task_id}); refreshRuns(); })); $('pending-list').append(card);
    }
}
function renderChips() {
    delete $('chat-input').dataset.requestId;
    $('reference-chips').replaceChildren(); state.references.forEach((ref,index) => $('reference-chips').append(refChip(ref, () => { state.references.splice(index,1); renderChips(); })));
}
function refChip(ref, remove) {
    const n = el('span','reference-chip' + (ref.valid === false ? ' invalid' : ''));
    const label = (ref.valid === false ? '已失效 · ' : '') + (ref.label || ref.quote || ref.path?.join(' - ') || '原文');
    n.append(button(label, () => jumpReference(ref))); n.title = label;
    if (remove) { const b = button('×', remove); b.setAttribute('aria-label','移除引用 ' + label); n.append(b); } return n;
}
function addReferences(refs) { for (const ref of refs) { if (!state.references.some(x => JSON.stringify(x) === JSON.stringify(ref))) state.references.push(ref); } state.references = state.references.slice(0,20); renderChips(); panel('chat'); $('chat-input').focus(); }
function closeMentions() { mention = null; $('mention-list').hidden = true; $('chat-input').setAttribute('aria-expanded','false'); $('chat-input').removeAttribute('aria-activedescendant'); }
function updateMentions() {
    const input = $('chat-input'), before = input.value.slice(0,input.selectionStart), match = before.match(/(?:^|\s)@([^@\n]*)$/);
    if (!match) { closeMentions(); return; }
    const query = match[1].toLowerCase(), start = input.selectionStart - query.length - 1;
    const matches = state.nodes.filter(n => n.path.join(' - ').toLowerCase().includes(query)).slice(0,60);
    mention = {start, end:input.selectionStart, matches, index:0}; renderMentions();
}
function renderMentions() {
    const box = $('mention-list'); box.replaceChildren(); box.hidden = false; $('chat-input').setAttribute('aria-expanded','true');
    if (!mention.matches.length) box.append(el('p','muted','没有匹配目录'));
    mention.matches.forEach((n,index) => { const option = el('div','',n.path.join(' - ')); option.role='option'; option.id='mention-'+index; option.setAttribute('aria-selected',String(index===mention.index)); option.onmousedown=e=>e.preventDefault(); option.onclick=()=>chooseMention(index); box.append(option); });
    $('chat-input').setAttribute('aria-activedescendant','mention-'+mention.index); $('mention-'+mention.index)?.scrollIntoView({block:'nearest'});
}
function chooseMention(index) {
    const node = mention.matches[index]; if (!node) return;
    const input = $('chat-input'), position = mention.start;
    input.value = input.value.slice(0,position) + input.value.slice(mention.end); input.setSelectionRange(position,position);
    addReferences([{kind:'node',node_id:node.id,chapter_idx:node.chapter_idx,paragraph_index:node.paragraph_index,fingerprint:node.fingerprint,label:node.path.join(' - ')}]); closeMentions();
}
async function sessions(select) {
    const data = await api(chatBase + '/sessions'); $('sessions').replaceChildren(new Option('选择或新建对话',''));
    data.items.forEach(s => $('sessions').append(new Option(s.title,s.id)));
    if (select !== undefined) state.session = select;
    if (!state.session && data.items.length) state.session=data.items[0].id;
    $('sessions').value=state.session;
    if (state.session) await loadSession();
}
async function newSession() {
    const r = await api(chatBase + '/sessions',{}); state.session=r.id; await sessions(r.id); panel('chat'); $('chat-input').focus(); return r.id;
}
async function loadSession() {
    if (chatStream) chatStream.close(); state.activeMessage='';
    if (!state.session) { state.messages=[]; renderMessages(); return; }
    const sid=state.session, data=await api(chatBase + '/sessions/' + sid); if(sid!==state.session)return;
    state.messages=data.messages; renderMessages();
    const active=data.messages.find(m=>m.role==='assistant' && ['queued','running'].includes(m.status));
    if(active) streamAnswer(active.id,active.event_cursor || 0); else setChatActive(false);
}
function setChatActive(active) { $('stop-chat').hidden=!active; $('send-chat').disabled=active; if(!active){state.activeMessage='';$('chat-status').textContent='';} }
function traceArgs(args) {
    if(!args||typeof args!=='object')return '';
    return Object.entries(args).map(([key,value])=>{let text=typeof value==='string'?value:JSON.stringify(value);if(text===undefined||text===null)text='';text=String(text).replace(/\s+/g,' ').trim();if(text.length>90)text=text.slice(0,87)+'…';return key+'='+text;}).filter(item=>!item.endsWith('=')).join(', ');
}
function traceToolRow(tool) {
    const item=el('li','chat-trace-tool '+(tool.status||'running'));if(tool.id!==undefined&&tool.id!==null)item.dataset.toolId=String(tool.id);
    item.append(el('span','chat-trace-tool-name',tool.tool||'工具'));
    const args=traceArgs(tool.args);if(args)item.append(el('span','chat-trace-tool-args',args));
    item.append(el('span','chat-trace-tool-status',{running:'进行中',ok:'完成',error:'失败',blocked:'已拦截'}[tool.status]||''));
    if(tool.error)item.append(el('span','chat-trace-tool-error',tool.error));
    return item;
}
function traceLabel(details) {
    const hasReasoning=!!details.querySelector('.chat-trace-reasoning')?.textContent.trim(),count=details.querySelectorAll('.chat-trace-tool').length;
    if(hasReasoning&&count)return `思考过程与工具调用（${count} 次）`;
    if(hasReasoning)return '思考过程';if(count)return `工具调用（${count} 次）`;return '执行过程';
}
function refreshTraceLabel(details) { const summary=details.querySelector('summary');if(summary)summary.textContent=traceLabel(details); }
function traceBox(trace,open) {
    const details=el('details','chat-trace');details.open=!!open;details.append(el('summary','chat-trace-summary'));
    const body=el('div','chat-trace-body');
    if(trace?.reasoning){const pre=el('pre','chat-trace-reasoning');pre.textContent=trace.reasoning;body.append(pre);}
    const list=el('ol','chat-trace-tools');(trace?.tools||[]).forEach(tool=>list.append(traceToolRow(tool)));if(list.children.length)body.append(list);
    details.append(body);refreshTraceLabel(details);return details;
}
function ensureTrace(messageId,open=true) {
    const row=$('message-'+messageId);if(!row)return null;let details=row.querySelector('.chat-trace');
    if(!details){details=traceBox({reasoning:'',tools:[]},open);row.insertBefore(details,row.querySelector('.message-content'));}
    if(open)details.open=true;return details;
}
function ensureTraceReasoning(details) {
    let pre=details.querySelector('.chat-trace-reasoning');if(!pre){pre=el('pre','chat-trace-reasoning');const body=details.querySelector('.chat-trace-body');body.insertBefore(pre,body.querySelector('.chat-trace-tools'));}return pre;
}
function ensureTraceTools(details) {
    let list=details.querySelector('.chat-trace-tools');if(!list){list=el('ol','chat-trace-tools');details.querySelector('.chat-trace-body').append(list);}return list;
}
function keepChatBottom() { const box=$('messages');if(box.scrollHeight-box.scrollTop-box.clientHeight<200)box.scrollTop=box.scrollHeight; }
function renderMessages() {
    const box=$('messages'); box.replaceChildren();
    if(!state.messages.length) { const empty=el('div','empty-state'); empty.append(el('span','empty-mark','G'),el('h2','','从原文开始思考'),el('p','','划选原文，或用 @ 引用章、节、子节。对话随书保存。'));box.append(empty); }
    for(const m of state.messages) {
        const row=el('section','message '+m.role); row.id='message-'+m.id;
        row.append(el('div','message-role',m.role==='user'?'读者':'Gnosys Agent · '+m.status));
        if(m.role==='assistant'&&m.trace&&(m.trace.reasoning||(m.trace.tools||[]).length))row.append(traceBox(m.trace,['queued','running'].includes(m.status)));
        const content=mdBox(m.content);content.classList.add('message-content');row.append(content);
        const refs=el('div','reference-chips');(m.references||[]).forEach(ref=>refs.append(refChip(ref)));row.append(refs);
        if(m.error)row.append(el('p','message-error',m.error));
        if(m.role==='assistant'){
            const actions=el('div','message-actions');actions.append(button('复制',()=>navigator.clipboard.writeText(m.content)),button('保存为笔记',async()=>{if(!canLeaveNote())return;state.dirty=false;await openNote('chat',{message_id:m.id});$('note-editor').value=m.content;state.dirty=true;state.editing=true;renderNote();await saveNote();}));
            if(['failed','canceled','interrupted'].includes(m.status))actions.append(button('重试',async()=>{const r=await api(chatBase+'/sessions/'+state.session+'/messages/'+m.id+'/retry',{request_id:crypto.randomUUID()});await loadSession();}));row.append(actions);
        }
        box.append(row);
    }
    box.scrollTop=box.scrollHeight;
}
function streamAnswer(id, cursor=0) {
    if(chatStream)chatStream.close(); state.activeMessage=id;setChatActive(true);
    const sid=state.session;chatStream=new EventSource(chatBase+'/sessions/'+sid+'/messages/'+id+'/events?after='+cursor);
    chatStream.addEventListener('update',e=>{
        if(sid!==state.session)return;const x=JSON.parse(e.data),m=state.messages.find(m=>m.id===id);
        if(x.kind==='delta'&&m){m.content+=x.data.text;const content=$('message-'+id)?.querySelector('.message-content');if(content)content.innerHTML=renderMarkdown(m.content);keepChatBottom();}
        if(x.kind==='reasoning'&&m){const details=ensureTrace(id,true);if(details){ensureTraceReasoning(details).append(document.createTextNode(x.data.text||''));refreshTraceLabel(details);keepChatBottom();}}
        if(x.kind==='tool'&&m){const details=ensureTrace(id,true);if(details){const list=ensureTraceTools(details),row=traceToolRow(x.data),old=[...list.children].find(n=>n.dataset.toolId===String(x.data.id));if(old)old.replaceWith(row);else list.append(row);refreshTraceLabel(details);keepChatBottom();}}
        if(x.kind==='status')$('chat-status').textContent=x.data.message+(x.data.tool?' · '+x.data.tool:'');
        if(x.kind==='completed'){const details=$('message-'+id)?.querySelector('.chat-trace');if(details)details.open=false;setChatActive(false);}
    });
    chatStream.addEventListener('end',()=>{chatStream.close();setChatActive(false);loadSession().catch(e=>toast(e.message));});
    chatStream.onerror=()=>{$('chat-status').textContent='连接中断，正在重接；回答会继续保存在本机';};
}
async function send() {
    const content=$('chat-input').value;if(!content.trim()&&!state.references.length)return;
    if(!state.session)await newSession();
    const requestId=$('chat-input').dataset.requestId||crypto.randomUUID();$('chat-input').dataset.requestId=requestId;
    const receipt=await api(chatBase+'/sessions/'+state.session+'/messages',{content,references:state.references,request_id:requestId});
    $('chat-input').value='';delete $('chat-input').dataset.requestId;state.references=[];renderChips();closeMentions();await loadSession();

}
function captureSelection() {
    const selection=window.getSelection();if(!selection.rangeCount||selection.isCollapsed){$('selection-actions').hidden=true;return;}
    const range=selection.getRangeAt(0),refs=[];
    for(const source of document.querySelectorAll('.source[data-pi]')){
        if(!range.intersectsNode(source))continue;
        const local=document.createRange();local.selectNodeContents(source);
        if(source.contains(range.startContainer))local.setStart(range.startContainer,range.startOffset);
        if(source.contains(range.endContainer))local.setEnd(range.endContainer,range.endOffset);
        const quote=local.toString();const p=state.paragraphs[Number(source.dataset.pi)];
        if(quote.trim()&&p.text.includes(quote))refs.push({kind:'selection',chapter_idx:state.chapter,paragraph_index:p.paragraph_index,fingerprint:p.fingerprint,quote,label:quote.slice(0,60)});
    }
    if(!refs.length){$('selection-actions').hidden=true;return;}
    state.selected=refs;const rect=range.getBoundingClientRect(),box=$('selection-actions');box.hidden=false;box.style.top=Math.min(window.innerHeight-50,Math.max(70,rect.bottom+8))+'px';box.style.left=Math.min(window.innerWidth-300,Math.max(8,rect.left))+'px';
}
const SIDEBARS = {
    nav: {panel:'navigation',handle:'nav-resize',css:'--nav-width',store:'gnosys-navigation-width',cls:'no-navigation',toggle:'toggle-nav'},
    inspector: {panel:'inspector',handle:'panel-resize',css:'--panel-width',store:'gnosys-inspector-width',cls:'no-inspector',toggle:'toggle-panel'}
};
const READING_MIN_WIDTH = 320;
const sidebarWidths = {nav:0, inspector:0};
function pageWidth() { return document.documentElement.clientWidth || window.innerWidth; }
function sidebarMinWidth() { return Math.max(1,Math.ceil(pageWidth()/50)); }
function sidebarMaxWidth() {
    const page=pageWidth();
    // 硬上限：页宽 1/3；同时给中间阅读区留出最小宽度，避免三栏挤爆。
    return Math.max(0,Math.min(Math.floor(page/3),Math.floor((page-READING_MIN_WIDTH)/2)));
}
function applySidebarWidth(kind,width,persist) {
    const cfg=SIDEBARS[kind],panel=$(cfg.panel);if(!panel)return 0;
    const min=sidebarMinWidth(),max=sidebarMaxWidth();if(max<=min)return 0;
    const value=Math.max(min,Math.min(max,Math.round(width)));
    document.documentElement.style.setProperty(cfg.css,value+'px');sidebarWidths[kind]=value;
    const handle=$(cfg.handle);
    if(handle){handle.setAttribute('aria-valuemin',String(min));handle.setAttribute('aria-valuemax',String(max));handle.setAttribute('aria-valuenow',String(value));}
    if(persist)localStorage.setItem(cfg.store,String(value));
    return value;
}
function syncSidebarHandles() {
    const min=sidebarMinWidth(),max=sidebarMaxWidth(),mobile=pageWidth()<=800;
    for(const kind of Object.keys(SIDEBARS)){
        const cfg=SIDEBARS[kind],handle=$(cfg.handle),panel=$(cfg.panel);if(!handle||!panel)continue;
        const enabled=!mobile&&max>min;handle.hidden=!enabled;if(!enabled)continue;
        if(!document.body.classList.contains(cfg.cls)){
            const width=sidebarWidths[kind]||panel.getBoundingClientRect().width;
            applySidebarWidth(kind,width,false);
        }
    }
}
function setSidebarVisible(kind,visible) {
    const cfg=SIDEBARS[kind],panel=$(cfg.panel),toggle=$(cfg.toggle);if(!panel)return;
    if(!visible){
        const width=panel.getBoundingClientRect().width;if(width>0)sidebarWidths[kind]=Math.round(width);
        document.body.classList.add(cfg.cls);
    }else{
        document.body.classList.remove(cfg.cls);
        const stored=parseFloat(localStorage.getItem(cfg.store));
        const width=Number.isFinite(stored)?stored:(sidebarWidths[kind]||panel.getBoundingClientRect().width);
        applySidebarWidth(kind,width,false);
    }
    if(toggle)toggle.setAttribute('aria-expanded',String(visible));
    syncSidebarHandles();
}
function toggleSidebar(kind) { const cfg=SIDEBARS[kind];setSidebarVisible(kind,document.body.classList.contains(cfg.cls)); }
function startSidebarDrag(kind,event) {
    const cfg=SIDEBARS[kind],panel=$(cfg.panel);if(!panel||event.button!==0||pageWidth()<=800)return;
    const min=sidebarMinWidth(),max=sidebarMaxWidth();if(max<=min)return;
    event.preventDefault();
    const startX=event.clientX,startWidth=panel.getBoundingClientRect().width,direction=kind==='nav'?1:-1;
    let done=false;
    const finish=save=>{
        if(done)return;done=true;
        document.body.classList.remove('panel-resizing');
        document.removeEventListener('pointermove',move);document.removeEventListener('pointerup',up);document.removeEventListener('pointercancel',up);window.removeEventListener('blur',up);
        if(save)applySidebarWidth(kind,panel.getBoundingClientRect().width,true);
    };
    const move=ev=>{
        if(done)return;
        const width=startWidth+(ev.clientX-startX)*direction;
        if(width<min){finish(false);setSidebarVisible(kind,false);return;}
        applySidebarWidth(kind,width,false);
    };
    const up=()=>finish(true);
    document.body.classList.add('panel-resizing');
    document.addEventListener('pointermove',move);document.addEventListener('pointerup',up);document.addEventListener('pointercancel',up);window.addEventListener('blur',up);
}
function initSidebars() {
    for(const kind of Object.keys(SIDEBARS)){
        const cfg=SIDEBARS[kind],panel=$(cfg.panel),handle=$(cfg.handle);if(!panel||!handle)continue;
        sidebarWidths[kind]=Math.round(panel.getBoundingClientRect().width);
        const stored=parseFloat(localStorage.getItem(cfg.store));
        if(Number.isFinite(stored))applySidebarWidth(kind,stored,false);
        handle.addEventListener('pointerdown',e=>startSidebarDrag(kind,e));
        handle.addEventListener('keydown',e=>{
            if(!['ArrowLeft','ArrowRight'].includes(e.key))return;
            e.preventDefault();
            const current=panel.getBoundingClientRect().width,min=sidebarMinWidth();
            const grow=kind==='nav'?(e.key==='ArrowRight'?1:-1):(e.key==='ArrowLeft'?1:-1);
            const next=current+grow*16;
            if(next<min){setSidebarVisible(kind,false);return;}
            applySidebarWidth(kind,next,true);
        });
    }
    window.addEventListener('resize',syncSidebarHandles);
    syncSidebarHandles();
}
// Shared navigation for evidence links emitted by the model and generated notes.
document.addEventListener('click',e=>{const a=e.target.closest('a');if(!a)return;const match=(a.getAttribute('href')||'').match(/^#source=(\d+):(\d+)$/);if(match){e.preventDefault();goto(Number(match[1]),Number(match[2])).catch(err=>toast(err.message));}});
document.querySelectorAll('[data-panel]').forEach(b=>b.onclick=()=>panel(b.dataset.panel));document.querySelectorAll('[data-nav]').forEach(b=>b.onclick=()=>nav(b.dataset.nav).catch(e=>toast(e.message)));
on('toggle-nav','click',()=>toggleSidebar('nav'));
on('toggle-panel','click',()=>toggleSidebar('inspector'));
on('previous','click',()=>goto(state.chapter-1));on('next','click',()=>goto(state.chapter+1));
on('search-form','submit',async e=>{e.preventDefault();const q=$('search-input').value.trim();if(!q)return;const data=await api(base+'/search?q='+encodeURIComponent(q));$('nav-results').replaceChildren();data.items.forEach(item=>{const row=el('div','search-result');row.append(button(item.title,()=>goto(item.chapter_idx,item.paragraph_index)),el('small','',item.excerpt));$('nav-results').append(row);});if(!data.items.length)$('nav-results').append(el('p','muted','没有找到匹配文字'));if(data.truncated)$('nav-results').append(el('p','muted','结果较多，请缩小搜索范围'));});
on('add-bookmark','click',async()=>{const ref=currentPosition();if(ref){await api(base+'/bookmarks',{reference:ref,label:ref.quote.slice(0,50)});toast('已添加书签');}});
on('reader-scroll','scroll',()=>{
    clearTimeout(tocTimer);tocTimer=setTimeout(updateTocFromScroll,80);
    clearTimeout(progressTimer);progressTimer=setTimeout(()=>{const ref=currentPosition();if(ref)api(base+'/state',ref,'PUT').catch(e=>toast(e.message));},500);
});
{
    const scroller=$('reader-scroll');
    scroller.addEventListener('wheel',e=>{
        if(state.chapterNavLock)return;
        const atTop=scroller.scrollTop<=4,atBottom=scroller.scrollTop+scroller.clientHeight>=scroller.scrollHeight-4;
        const target=e.deltaY>0?(atBottom&&state.chapter<cfg.total-1?state.chapter+1:null):(atTop&&state.chapter>0?state.chapter-1:null);
        if(target===null)return;
        state.chapterNavLock=true;
        goto(target,0,true,e.deltaY<0?'bottom':'top').catch(err=>toast(err.message)).finally(()=>setTimeout(()=>state.chapterNavLock=false,400));
    },{passive:true});
}
on('generate-semantic','click',()=>generate({layer:'L3',chapter_idx:state.chapter,mode:'fill_missing',spec_type:$('agent-spec').value}));
on('toggle-semantic-rail','click',()=>setSemanticVisible(document.body.classList.contains('hide-semantic')));
document.querySelectorAll('[data-note-layer]').forEach(b => b.onclick = () => openNoteLayer(b.dataset.noteLayer).catch(e => toast(e.message)));
on('edit-note','click',()=>{state.editing=true;renderNote();$('note-editor').focus();});
on('note-editor','input',()=>{state.dirty=true;$('note-save-state').textContent='未保存';});on('save-note','click',saveNote);
on('export-note','click',()=>download($('note-editor').value,(state.note?.key||'笔记')+'.md'));
on('generate-note','click',async()=>{if(!state.note)return;if(state.dirty){toast('请先保存当前笔记，再生成 AI 候选稿');return;}const n=state.note;if(n.layer==='L3')await generate({layer:'L3',paragraph_ref:n.target,mode:'overwrite',spec_type:$('agent-spec').value});else await generate({layer:n.layer,target:n.target,spec_type:$('agent-spec').value});});
on('note-list-toggle','click',async()=>{const box=$('note-list');box.hidden=!box.hidden;if(box.hidden)return;const [saved,legacy]=await Promise.all([api(base+'/notes'),api('/api/layer/concept_list/'+book)]);box.replaceChildren();saved.items.forEach(n=>box.append(button(n.layer+' · '+(n.target.term||n.content.slice(0,45)||n.key),()=>openNote(n.layer,n.target))));legacy.items.forEach(n=>{const d=el('details');d.append(el('summary','','原有概念 · '+n.term),mdBox(n.content));box.append(d);});if(!box.children.length)box.append(el('p','muted','尚无笔记'));});
on('refresh-runs','click',refreshRuns);on('new-session','click',newSession);on('sessions','change',()=>{state.session=$('sessions').value;return loadSession();});
on('rename-session','click',async()=>{if(!state.session)return;const title=prompt('对话名称',$('sessions').selectedOptions[0].text);if(title){await api(chatBase+'/sessions/'+state.session,{title},'PATCH');await sessions(state.session);}});
on('delete-session','click',async()=>{if(state.session&&confirm('删除此对话及其历史？')){await api(chatBase+'/sessions/'+state.session,undefined,'DELETE');state.session='';await sessions('');if(!state.session){state.messages=[];renderMessages();}}});
on('export-chat','click',()=>{if(state.session)location.href=chatBase+'/sessions/'+state.session+'/export';});on('send-chat','click',send);
on('stop-chat','click',async()=>{if(state.activeMessage)await api(chatBase+'/sessions/'+state.session+'/messages/'+state.activeMessage+'/cancel',{});});
on('chat-input','input',()=>{delete $('chat-input').dataset.requestId;updateMentions();});
on('chat-input','keydown',e=>{if(e.isComposing)return;if(mention){if(['ArrowDown','ArrowUp'].includes(e.key)){e.preventDefault();mention.index=(mention.index+(e.key==='ArrowDown'?1:-1)+mention.matches.length)%Math.max(1,mention.matches.length);renderMentions();return;}if(e.key==='Escape'){e.preventDefault();closeMentions();return;}if(e.key==='Enter'&&mention.matches.length){e.preventDefault();chooseMention(mention.index);return;}}if(e.key==='Enter'&&!e.shiftKey){e.preventDefault();if(!state.activeMessage)return send();}});
on('chapter-content','mouseup',captureSelection);on('chapter-content','keyup',captureSelection);
$('selection-actions').addEventListener('mousedown',e=>e.preventDefault());
on('selection-chat','click',()=>{addReferences(state.selected);$('selection-actions').hidden=true;});
on('selection-semantic','click',()=>{const r=state.selected[0];$('selection-actions').hidden=true;if(r)return openNote('L3',{chapter_idx:r.chapter_idx,paragraph_index:r.paragraph_index,fingerprint:r.fingerprint});});
on('selection-concept','click',async()=>{
    if(state.selected.length!==1){toast('解释概念时请只选一个段落内的术语');return;}
    const r=state.selected[0],target={chapter_idx:r.chapter_idx,paragraph_index:r.paragraph_index,fingerprint:r.fingerprint,term:r.quote};
    $('selection-actions').hidden=true;await openNote('L4',target);
    try { await generate({layer:'L4',target,spec_type:$('agent-spec').value}); }
    catch(error) {
        const decision=error.detail;
        if(decision?.action==='confirm'&&decision.candidates?.length){
            const term=prompt(decision.message+'\n'+decision.candidates.join(' / '),decision.candidates[0]);
            if(term){await openNote('L4',{...target,term});await generate({layer:'L4',target:{...target,term},spec_type:$('agent-spec').value});}
        } else throw error;
    }
});
window.addEventListener('beforeunload',e=>{if(state.dirty){e.preventDefault();e.returnValue='';}});
function initReadingCycles(){
    const closeMenus=()=>document.querySelectorAll('.reading-pref.open').forEach(wrap=>{
        wrap.classList.remove('open');
        wrap.querySelector('.reading-pref-button')?.setAttribute('aria-expanded','false');
    });
    const controls=[
        {id:'font-size-cycle',key:'font-size',css:'--reading-size',values:['16','18','20','22','24'],fallback:'18',unit:'px',label:'字号',format:v=>v+'px'},
        {id:'line-height-cycle',key:'line-height',css:'--reading-line',values:['1.6','1.9','2.2'],fallback:'1.9',unit:'',label:'行距',format:v=>v}
    ];
    controls.forEach(cfg=>{
        const btn=$(cfg.id);if(!btn)return;
        const wrap=btn.closest('.reading-pref'),menu=wrap?.querySelector('.reading-pref-menu');
        if(!wrap||!menu)return;
        let value=localStorage.getItem('gnosys-'+cfg.key)||cfg.fallback;
        if(!cfg.values.includes(value))value=cfg.fallback;
        const options=new Map();
        const apply=v=>{
            value=v;localStorage.setItem('gnosys-'+cfg.key,v);
            document.documentElement.style.setProperty(cfg.css,v+cfg.unit);
            btn.textContent=cfg.label+' '+cfg.format(v);
            btn.title=cfg.label+'：'+cfg.format(v);
            btn.setAttribute('aria-label',cfg.label+'：'+cfg.format(v));
            options.forEach((opt,val)=>opt.setAttribute('aria-selected',String(val===v)));
        };
        cfg.values.forEach(v=>{
            const opt=el('button','reading-pref-option',cfg.format(v));opt.type='button';
            opt.setAttribute('role','option');opt.dataset.value=v;
            opt.onclick=e=>{e.stopPropagation();apply(v);closeMenus();btn.focus();};
            options.set(v,opt);menu.append(opt);
        });
        apply(value);
        btn.onclick=e=>{
            e.stopPropagation();
            const wasOpen=wrap.classList.contains('open');
            closeMenus();
            if(!wasOpen){wrap.classList.add('open');btn.setAttribute('aria-expanded','true');}
        };
    });
    document.addEventListener('click',closeMenus);
    document.addEventListener('keydown',e=>{if(e.key==='Escape')closeMenus();});
}
function setSemanticVisible(visible){
    document.body.classList.toggle('hide-semantic',!visible);
    const btn=$('toggle-semantic-rail');
    if(btn){btn.setAttribute('aria-pressed',String(visible));btn.title='段落旁注：'+(visible?'开':'关');btn.setAttribute('aria-label','段落旁注：'+(visible?'开':'关'));}
}
initReadingCycles();
setSemanticVisible(true);
async function init(){const settingsLink=$('open-settings');if(settingsLink)settingsLink.href='/settings?from='+encodeURIComponent(location.pathname+location.search+location.hash);initSidebars();if(innerWidth<=800){document.body.classList.add('no-navigation','no-inspector');$('toggle-nav').setAttribute('aria-expanded','false');$('toggle-panel').setAttribute('aria-expanded','false');}const data=await api(base+'/outline');state.nodes=data.items;await nav('outline');const match=location.hash.match(/source=(\d+):(\d+)/);await goto(match?Number(match[1]):state.chapter,match?Number(match[2]):0,false);await sessions();if(cfg.noteMode)await openNote('L1');window.GNOSYS.ready=true;}
init().catch(e=>toast(e.message));
})();
