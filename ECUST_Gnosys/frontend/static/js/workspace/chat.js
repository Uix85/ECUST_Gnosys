/* Chat sessions, composer drafts, streaming and selection actions. */
'use strict';
function renderChips() {
    delete $('chat-input').dataset.requestId;
    $('reference-chips').replaceChildren(); state.references.forEach((ref,index) => $('reference-chips').append(refChip(ref, () => { state.references.splice(index,1); renderChips(); })));
}
function stashDraft() {
    chatDrafts.set(state.session || '', {text:$('chat-input').value, references:state.references.slice(), requestId:$('chat-input').dataset.requestId || ''});
}
function restoreDraft(id) {
    const draft = chatDrafts.get(id || '');
    $('chat-input').value = draft?.text || '';
    state.references = (draft?.references || []).slice();
    renderChips(); closeMentions();
    if (draft?.requestId) $('chat-input').dataset.requestId = draft.requestId;
}
function refChip(ref, remove) {
    const n = el('span','reference-chip' + (ref.valid === false ? ' invalid' : ''));
    const label = (ref.valid === false ? '已失效 · ' : '') + (ref.label || ref.quote || ref.path?.join(' - ') || '原文');
    n.append(button(label, () => jumpReference(ref))); n.title = label;
    if (remove) { const b = button('×', remove, 'reference-remove'); b.setAttribute('aria-label','移除引用 ' + label); n.append(b); } return n;
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
function sessionTime(value) {
    const date = new Date(value); if (Number.isNaN(date.getTime())) return '';
    const now = new Date();
    if (date.toDateString() === now.toDateString()) return String(date.getHours()).padStart(2,'0') + ':' + String(date.getMinutes()).padStart(2,'0');
    if (date.getFullYear() === now.getFullYear()) return (date.getMonth()+1) + '月' + date.getDate() + '日';
    return date.getFullYear() + '年' + (date.getMonth()+1) + '月' + date.getDate() + '日';
}
function showSessionList() {
    closeSessionMenu();
    $('chat-sessions-view').hidden = false;
    $('chat-thread-view').hidden = true;
}
function showThreadView() {
    closeSessionMenu();
    $('chat-sessions-view').hidden = true;
    $('chat-thread-view').hidden = false;
}
function renderSessionList() {
    const list = $('session-list');
    list.replaceChildren();
    $('session-count').textContent = state.sessions.length ? state.sessions.length + ' 个对话 · 右键管理' : '尚无对话';
    if (!state.sessions.length) {
        list.append(el('p', 'session-list-state muted', '还没有对话，在下方输入并发送即可开始。'));
        return;
    }
    for (const session of state.sessions) {
        const row = el('div', 'session-item' + (session.id === state.session ? ' active' : ''));
        row.setAttribute('role', 'listitem'); row.tabIndex = 0; row.dataset.sessionId = session.id;
        row.title = '打开对话；右键可重命名或删除';
        const main = el('div', 'session-item-main');
        main.append(el('span', 'session-item-title', session.title || '新对话'), el('span', 'session-item-time', sessionTime(session.updated_at)));
        row.append(main);
        row.addEventListener('click', () => { closeSessionMenu(); openSession(session.id).catch(error => toast(error.message)); });
        row.addEventListener('contextmenu', event => { event.preventDefault(); event.stopPropagation(); openSessionMenu(event.clientX, event.clientY, session.id); });
        row.addEventListener('keydown', event => {
            if (event.key === 'Enter' || event.key === ' ') { event.preventDefault(); row.click(); return; }
            if (event.key === 'ContextMenu' || (event.shiftKey && event.key === 'F10')) {
                event.preventDefault(); const rect = row.getBoundingClientRect(); openSessionMenu(rect.right - 12, rect.top + 14, session.id);
            }
        });
        list.append(row);
    }
}
function closeSessionMenu() {
    const menu = $('session-menu'); if (!menu || menu.hidden) return;
    menu.hidden = true; menuSessionId = '';
}
function openSessionMenu(x, y, id) {
    menuSessionId = id;
    const menu = $('session-menu');
    menu.hidden = false; menu.style.left = '0px'; menu.style.top = '0px';
    const rect = menu.getBoundingClientRect();
    menu.style.left = Math.max(8, Math.min(x, window.innerWidth - rect.width - 8)) + 'px';
    menu.style.top = Math.max(8, Math.min(y, window.innerHeight - rect.height - 8)) + 'px';
    $('rename-session').focus();
}
async function sessions() {
    const data = await api(chatBase + '/sessions');
    state.sessions = data.items || [];
    renderSessionList();
}
async function renameSession(id) {
    closeSessionMenu();
    if (!id) return;
    const item = state.sessions.find(session => session.id === id);
    const title = prompt('对话名称', item?.title || '新对话');
    if (title === null) return;
    const value = title.trim();
    if (!value) { toast('对话名称不能为空'); return; }
    await api(chatBase + '/sessions/' + id, {title: value}, 'PATCH');
    await sessions();
    if (state.session === id) $('chat-session-title').textContent = value;
    toast('已重命名');
}
async function openSession(id) {
    stashDraft();
    state.session = id;
    restoreDraft(id);
    panel('chat'); showThreadView();
    const current = state.sessions.find(session => session.id === id);
    $('chat-session-title').textContent = current?.title || '对话';
    $('chat-session-meta').textContent = current ? '最后更新 ' + sessionTime(current.updated_at) : '';
    $('messages').replaceChildren(el('p', 'muted', '正在载入对话…'));
    await loadSession();
}
function newSession() {
    // 只开前端草稿；第一次发送成功后才创建数据库会话，避免留下空对话。
    stashDraft();
    if (chatStream) chatStream.close();
    state.session=''; state.messages=[]; state.activeMessage=''; restoreDraft('');
    panel('chat'); showThreadView();
    $('chat-session-title').textContent='新对话';
    $('chat-session-meta').textContent='尚未保存';
    $('chat-status').textContent=''; setChatActive(false); renderMessages(); $('chat-input').focus();
}
async function loadSession() {
    if (chatStream) chatStream.close(); state.activeMessage=''; setChatActive(false);
    if (!state.session) { state.messages=[]; renderMessages(); return; }
    const sid=state.session, data=await api(chatBase + '/sessions/' + sid); if(sid!==state.session)return;
    $('chat-session-title').textContent = data.title || '对话';
    $('chat-session-meta').textContent = '最后更新 ' + sessionTime(data.updated_at);
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
        if(m.role==='assistant'&&!['queued','running'].includes(m.status)){
            const actions=el('div','message-actions');
            if(m.content.trim())actions.append(button('复制',()=>navigator.clipboard.writeText(m.content)),button('保存为笔记',async()=>{if(!canLeaveNote())return;state.dirty=false;await openNote('chat',{message_id:m.id});$('note-editor').value=m.content;state.dirty=true;state.editing=true;renderNote();await saveNote();}));
            if(['failed','canceled','interrupted'].includes(m.status))actions.append(button('重试',async()=>{const r=await api(chatBase+'/sessions/'+state.session+'/messages/'+m.id+'/retry',{request_id:crypto.randomUUID()});await loadSession();}));
            if(actions.children.length)row.append(actions);
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
    if(chatSending)return;
    const content=$('chat-input').value;if(!content.trim()&&!state.references.length)return;
    chatSending=true;
    let createdHere=!state.session;
    let sid=state.session;
    try {
        if(createdHere){
            const created=await api(chatBase+'/sessions',{});
            sid=created.id; state.session=sid;
            $('chat-session-title').textContent=created.title||'新对话';
            $('chat-session-meta').textContent='刚刚';
        }
        const requestId=$('chat-input').dataset.requestId||crypto.randomUUID();$('chat-input').dataset.requestId=requestId;
        try {
            await api(chatBase+'/sessions/'+sid+'/messages',{content,references:state.references,request_id:requestId});
        } catch(error) {
            if(createdHere&&error.status){
                try{await api(chatBase+'/sessions/'+sid,undefined,'DELETE');}catch(_){}
                chatDrafts.delete(sid);
                await sessions().catch(()=>{});
                if(state.session===sid){
                    state.session='';state.messages=[];state.activeMessage='';
                    $('chat-session-title').textContent='新对话';$('chat-session-meta').textContent='尚未保存';renderMessages();setChatActive(false);
                }
            }
            throw error;
        }
        if(createdHere)chatDrafts.delete('');
        chatDrafts.delete(sid);
        if(state.session===sid){
            $('chat-input').value='';delete $('chat-input').dataset.requestId;state.references=[];renderChips();closeMentions();await loadSession();
        }
        await sessions();
    } finally { chatSending=false; }
}

let submittingFromList=false;
async function submitFromList(){
    if(submittingFromList)return;
    const value=$('session-new-input').value;
    if(!value.trim())return;
    submittingFromList=true;
    $('session-new-send').disabled=true;
    try{
        $('session-new-input').value='';
        newSession();
        const input=$('chat-input');
        input.value=value;
        input.dispatchEvent(new Event('input',{bubbles:true}));
        input.focus();
        input.setSelectionRange(value.length,value.length);
        await send();
    }finally{
        submittingFromList=false;
        $('session-new-send').disabled=!$('session-new-input').value.trim();
    }
}
