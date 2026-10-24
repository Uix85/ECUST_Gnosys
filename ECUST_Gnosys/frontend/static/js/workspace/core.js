/* Shared configuration, state, API and DOM helpers. */
'use strict';
const cfg = window.GNOSYS, book = encodeURIComponent(cfg.bookId);
const base = '/api/v1/workspace/' + book, agentBase = '/api/v1/agent/' + book + '/runs', chatBase = '/api/v1/chat/' + book;
const $ = id => document.getElementById(id);
const state = {chapter: cfg.chapter, nodes: [], paragraphs: [], references: [], session: '', sessions: [], messages: [], activeMessage: '',
    note: null, dirty: false, editing: false, selected: [], nav: 'outline', panel: 'chat', generation: 0,
    tocPinned: new Set(), tocClosed: new Set(), tocActive: new Set(), chapterNavLock: false};
let chatStream, toastTimer, progressTimer, tocTimer, runStream, mention = null, noteRequest = 0, menuSessionId = '', chatSending = false, noteSaving = false;
const chatDrafts = new Map();

function toast(text) { $('toast').textContent = text; $('toast').hidden = false; clearTimeout(toastTimer); toastTimer = setTimeout(() => $('toast').hidden = true, 5500); }
async function api(url, body, method) {
    const options = body === undefined ? {} : {method: method || 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body)};
    if (method && body === undefined) options.method = method;
    const r = await fetch(url, options), data = await r.json();
    if (!r.ok) { const detail = data.error || data.detail; const error = new Error(typeof detail === 'string' ? detail : detail?.message || JSON.stringify(detail)); error.detail = detail; error.status = r.status; throw error; }
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
