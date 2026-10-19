/* Chapter loading, table of contents, search and reading position. */
'use strict';
const EDGE_BOUNCE_LIMIT = 34, EDGE_BOUNCE_TRIGGER = 180, EDGE_BOUNCE_MAX_PULL = 1200;
const EDGE_HARD_PULL = 720, EDGE_HARD_EVENTS = 5, EDGE_ARM_WINDOW = 1400;
const edgeBounce = {direction:0, pull:0, events:0, armed:false, idleTimer:null, cleanupTimer:null, armTimer:null};

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
function disarmEdgeBounce() {
    clearTimeout(edgeBounce.armTimer); edgeBounce.armTimer = null; edgeBounce.armed = false;
}
function resetEdgeBounce(animate = true) {
    clearTimeout(edgeBounce.idleTimer); edgeBounce.idleTimer = null;
    clearTimeout(edgeBounce.cleanupTimer); edgeBounce.cleanupTimer = null;
    edgeBounce.direction = 0; edgeBounce.pull = 0; edgeBounce.events = 0;
    const view = $('chapter-view'); if (!view) return;
    if (!animate) { view.style.transition = ''; view.style.transform = ''; return; }
    view.style.transition = 'transform 180ms cubic-bezier(.2,.7,.3,1)';
    view.style.transform = 'translateY(0)';
    edgeBounce.cleanupTimer = setTimeout(() => { view.style.transition = ''; view.style.transform = ''; edgeBounce.cleanupTimer = null; }, 190);
}
function armEdgeBounce() {
    edgeBounce.idleTimer = null;
    resetEdgeBounce(true);
    edgeBounce.armed = true;
    clearTimeout(edgeBounce.armTimer);
    edgeBounce.armTimer = setTimeout(disarmEdgeBounce, EDGE_ARM_WINDOW);
}
function applyEdgePull(direction, magnitude) {
    edgeBounce.direction = direction;
    edgeBounce.pull = Math.min(EDGE_BOUNCE_MAX_PULL, edgeBounce.pull + magnitude);
    edgeBounce.events += 1;
    const distance = Math.min(EDGE_BOUNCE_LIMIT, 4 + edgeBounce.pull * .12);
    const view = $('chapter-view');
    clearTimeout(edgeBounce.cleanupTimer); edgeBounce.cleanupTimer = null;
    view.style.transition = 'none';
    view.style.transform = 'translateY(' + (direction > 0 ? -distance : distance) + 'px)';
    clearTimeout(edgeBounce.idleTimer);
    edgeBounce.idleTimer = setTimeout(armEdgeBounce, 260);
}
async function goto(ci, pi = 0, updateHistory = true, edge = 'top', smooth = true, force = false) {
    ci = Number(ci); pi = Number(pi);
    if (!Number.isInteger(ci) || !Number.isInteger(pi) || ci < 0 || pi < 0) return;
    resetEdgeBounce(false); disarmEdgeBounce();
    state.selected = []; $('selection-actions').hidden = true;
    const chapterChanged = ci !== state.chapter;
    if (!chapterChanged && !force && state.paragraphs.some(p => p.paragraph_index === pi)) {
        state.generation++;
        $('chapter-content').setAttribute('aria-busy', 'false');
        updateTocActive(pi); locate(pi, smooth);
        if (updateHistory && !cfg.noteMode) history.replaceState({}, '', '/read/' + book + '/' + ci + '#source=' + ci + ':' + pi);
        return;
    }
    const version = ++state.generation;
    $('chapter-content').setAttribute('aria-busy', 'true');
    let data;
    try {
        data = await api(base + '/chapter/' + ci);
    } catch (error) {
        if (version === state.generation) $('chapter-content').setAttribute('aria-busy', 'false');
        throw error;
    }
    if (version !== state.generation) return;
    cfg.total = data.total;
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
        source.querySelectorAll('img').forEach(image => { const src = image.getAttribute('src'); if (src && !/^(https?:|\/)/i.test(src)) image.src = '/api/v1/books/' + book + '/images/' + encodeURIComponent(src.split('/').pop()); });
        const side = el('aside', 'semantic-note'); side.setAttribute('aria-label', '段落解释');
        if (p.note) {
            side.append(el('div', 'short', p.note.short || '查看解释'));
            side.append(button('详细 / 逐句', () => openSemantic(p)));
        } else if (needsSemanticNote(p.text)) side.append(button('生成段落解释', () => generate({layer:'L3', paragraph_ref:{chapter_idx:ci, paragraph_index:p.paragraph_index, fingerprint:p.fingerprint}, spec_type:$('agent-spec').value})));
        row.append(source, side); fragment.append(row);
    });
    data.images.forEach(markup => { const holder = el('div'); holder.innerHTML = DOMPurify.sanitize(markup); holder.querySelectorAll('img').forEach(img => { img.classList.add('book-image'); const src = img.getAttribute('src'); if (src && !/^(https?:|\/)/i.test(src)) img.src = '/api/v1/books/' + book + '/images/' + encodeURIComponent(src.split('/').pop()); }); fragment.append(holder); });
    $('chapter-content').replaceChildren(fragment); $('chapter-content').setAttribute('aria-busy', 'false');
    const landingAtBottom = chapterChanged && pi === 0 && edge === 'bottom';
    if (chapterChanged) jumpReaderScroll(landingAtBottom ? $('reader-scroll').scrollHeight : 0);
    else $('reader-scroll').scrollTop = 0;
    const landingPi = landingAtBottom ? (currentPosition()?.paragraph_index ?? 0) : pi;
    updateTocActive(landingPi);
    if (pi > 0 && state.paragraphs.some(p => p.paragraph_index === pi)) locate(pi, smooth);
    if (updateHistory && !cfg.noteMode) history.replaceState({}, '', '/read/' + book + '/' + ci + '#source=' + ci + ':' + landingPi);
    clearTimeout(progressTimer); const ref = currentPosition(); if (ref) api(base + '/state', ref, 'PUT').catch(e => toast(e.message));
}
function locate(pi, smooth = true) {
    const target = $('paragraph-' + pi), scroller = $('reader-scroll');
    if (!target) return;
    const top = scroller.scrollTop + target.getBoundingClientRect().top - scroller.getBoundingClientRect().top;
    scroller.scrollTo({top, behavior: smooth ? 'smooth' : 'auto'});
    document.querySelectorAll('.located').forEach(n => n.classList.remove('located')); target.classList.add('located'); setTimeout(() => target.classList.remove('located'), 2600);
}
function refreshChapter() {
    return goto(state.chapter, currentPosition()?.paragraph_index || 0, false, 'top', true, true);
}
async function jumpReference(ref) { const r = await api(base + '/reference/check', {references:[ref]}); const valid = r.items[0]; await goto(valid.chapter_idx, valid.paragraph_index); }

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

async function jumpToAnchor(ci,anchor){
    await goto(ci,0,true);
    const target=document.getElementById(anchor);if(!target)return;
    const source=target.closest('.source');
    if(source){source.classList.add('located');setTimeout(()=>source.classList.remove('located'),2600);}
    const scroller=$('reader-scroll');
    const top=scroller.scrollTop+target.getBoundingClientRect().top-scroller.getBoundingClientRect().top;
    scroller.scrollTo({top,behavior:'smooth'});
}
