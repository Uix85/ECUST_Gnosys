/* Four-layer notes and note list. */
'use strict';
async function openNote(layer, target = {}) {
    if (!canLeaveNote()) return null;
    const id = ++noteRequest;
    const data = await api(base + '/note/read', {layer, target});
    if (id !== noteRequest) return null;
    state.note = data; state.dirty = false; state.editing = false; setNoteLayerUI(layer); $('note-editor').value = data.content;
    const targetLabel = layer === 'L1' ? cfg.title : layer === 'chat' ? '对话摘记' : (state.nodes.find(n => n.id === 'c' + target.chapter_idx)?.title || '') + (target.paragraph_index !== undefined ? ' · 段落 ' + (target.paragraph_index + 1) : '') + (target.term ? ' · ' + target.term : '');
    $('note-target').textContent = targetLabel; $('note-target').title = targetLabel;
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
}
async function openNoteLayer(layer) {
    if (layer === 'L1') return openNote(layer);
    if (layer === 'L2') return openNote(layer, {chapter_idx:state.chapter});
    const ref = state.selected[0] || currentPosition(); if (!ref) return;
    const target = {chapter_idx:ref.chapter_idx, paragraph_index:ref.paragraph_index, fingerprint:ref.fingerprint};
    if (layer === 'L4') { const term = state.selected[0]?.quote || prompt('输入当前位置要解释的术语'); if (!term) return; target.term = term; }
    await openNote(layer, target);
}
function splitL3Note(content) {
    const lines = String(content || '').split(/\r?\n/), short = [], body = [];
    let inShort = false;
    for (const line of lines) {
        const trimmed = line.trim();
        if (/^##\s*简短解释\s*$/.test(trimmed)) { inShort = true; continue; }
        if (inShort && /^##\s+/.test(trimmed)) inShort = false;
        (inShort ? short : body).push(line);
    }
    return {short: short.join('\n').trim(), body: body.join('\n').trim()};
}
function applyL3Short(content) {
    if (state.note?.layer !== 'L3') return;
    const target = state.note.target || {};
    if (Number(target.chapter_idx) !== state.chapter) return;
    const paragraph = state.paragraphs.find(p => p.paragraph_index === target.paragraph_index);
    if (!paragraph) return;
    const short = splitL3Note(content).short;
    paragraph.note = {...(paragraph.note || {}), short};
    const source = $('paragraph-' + target.paragraph_index);
    const side = source?.closest('.paragraph-row')?.querySelector('.semantic-note');
    if (!side) return;
    let node = side.querySelector('.short');
    if (!node) { node = el('div', 'short'); side.prepend(node); }
    node.textContent = short || '查看解释';
}
function renderNote() {
    const editing = state.editing;
    $('note-editor').hidden = !editing; $('note-preview').hidden = editing;
    $('edit-note').hidden = editing; $('save-note').hidden = !editing; $('cancel-note').hidden = !editing;
    const preview = $('note-preview'), content = $('note-editor').value;
    preview.innerHTML = renderMarkdown(state.note?.layer === 'L3' ? splitL3Note(content).body : content);
}
async function saveNote() {
    if (!state.note || noteSaving) return;
    noteSaving = true; $('save-note').disabled = true;
    try {
        const data = await api(base + '/note', {layer:state.note.layer, target:state.note.target, content:$('note-editor').value, baseline:baseline(state.note)}, 'PUT');
        state.note = data; state.dirty = false; state.editing = false; $('note-save-state').textContent = '已保存'; applyL3Short(data.content); delete $('note-list').dataset.loaded; toast('笔记已保存'); renderNote();
    } catch (e) {
        if (e.status === 409) { $('note-save-state').textContent = '保存冲突 · 输入已保留'; download($('note-editor').value, '冲突笔记备份.md'); }
        throw e;
    } finally {
        noteSaving = false; $('save-note').disabled = false;
    }
}

function noteListLabel(note) {
    const content = note.content || '';
    let label = note.target.term || note.key;
    if (note.layer === 'L3') {
        const parts = splitL3Note(content);
        label = note.target.term || parts.short || parts.body || note.key;
    } else if (content) {
        label = note.target.term || content;
    }
    const layerName = note.layer === 'chat' ? '对话摘记' : note.layer;
    return layerName + ' · ' + ((label.split(/\r?\n/).find(Boolean) || note.key).slice(0,45));
}
async function toggleNoteList() {
    const box = $('note-list');
    box.hidden = !box.hidden;
    if (box.hidden) return;
    if (box.dataset.loaded === '1') return;
    box.textContent = '正在载入笔记…'; box.setAttribute('aria-busy', 'true');
    try {
        const [saved, legacy] = await Promise.all([api(base + '/notes?summary=1'), api('/api/v1/layer/concept_list/' + book)]);
        const fragment = document.createDocumentFragment();
        (saved.items || []).forEach(n => fragment.append(button(noteListLabel(n), () => openNote(n.layer, n.target))));
        (legacy.items || []).forEach(n => { const d = el('details'); d.append(el('summary','','原有概念 · ' + n.term), mdBox(n.content)); fragment.append(d); });
        if (!fragment.children.length) fragment.append(el('p','muted','尚无笔记'));
        box.replaceChildren(fragment); box.dataset.loaded = '1';
    } catch (error) {
        box.textContent = '载入失败：' + error.message; throw error;
    } finally {
        box.removeAttribute('aria-busy');
    }
}
