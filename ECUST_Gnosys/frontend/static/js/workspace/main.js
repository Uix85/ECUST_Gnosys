/* DOM event wiring and application startup. */
'use strict';
document.addEventListener('click',e=>{
    const a=e.target.closest('a');if(!a)return;
    const href=a.getAttribute('href')||'';
    const match=href.match(/^#source=(\d+):(\d+)$/);
    if(match){e.preventDefault();goto(Number(match[1]),Number(match[2])).catch(err=>toast(err.message));return;}
    if(!a.closest('#chapter-content')||!href.includes('#'))return;
    const raw=href.slice(href.indexOf('#')+1);
    let anchor=raw;try{anchor=decodeURIComponent(raw);}catch(e){}
    const ci=cfg.anchorMap?.[anchor];
    if(!Number.isInteger(ci))return;
    e.preventDefault();
    jumpToAnchor(ci,anchor).catch(err=>toast(err.message));
});

document.querySelectorAll('[data-panel]').forEach(b=>b.onclick=()=>{const previous=state.panel;panel(b.dataset.panel);if(b.dataset.panel==='chat'&&previous!=='chat'){showSessionList();sessions().catch(e=>toast(e.message));}if(b.dataset.panel==='note'&&!state.note)openNoteLayer('L1').catch(e=>toast(e.message));});document.querySelectorAll('[data-nav]').forEach(b=>b.onclick=()=>nav(b.dataset.nav).catch(e=>toast(e.message)));
document.addEventListener('click',e=>{if(!e.target.closest('#session-menu'))closeSessionMenu();});
document.addEventListener('contextmenu',e=>{if(!e.target.closest('.session-item')&&!e.target.closest('#session-menu'))closeSessionMenu();});
document.addEventListener('keydown',e=>{if(e.key==='Escape')closeSessionMenu();});
window.addEventListener('resize',closeSessionMenu);$('session-list').addEventListener('scroll',closeSessionMenu,{passive:true});
on('toggle-nav','click',()=>toggleSidebar('nav'));
on('toggle-panel','click',()=>toggleSidebar('inspector'));
on('previous','click',()=>goto(state.chapter-1));on('next','click',()=>goto(state.chapter+1));
on('search-form','submit',async e=>{e.preventDefault();const q=$('search-input').value.trim();if(!q)return;const data=await api(base+'/search?q='+encodeURIComponent(q));const box=$('nav-results');box.replaceChildren();data.items.forEach(item=>{const row=el('div','search-result'),hit=button('',()=>goto(item.chapter_idx,item.paragraph_index,true,'top',false),'search-hit'),excerpt=el('span','search-excerpt'),start=Number.isInteger(item.match_start)?item.match_start:-1,end=start+(item.match_length||0);if(start>=0&&end>start){excerpt.append(document.createTextNode(item.excerpt.slice(0,start)),el('mark','',item.excerpt.slice(start,end)),document.createTextNode(item.excerpt.slice(end)));}else excerpt.textContent=item.excerpt;hit.append(excerpt);row.append(hit);box.append(row);});if(!data.items.length)box.append(el('p','muted','没有找到匹配文字'));if(data.truncated)box.append(el('p','muted','结果较多，请缩小搜索范围'));});
on('add-bookmark','click',async()=>{const ref=currentPosition();if(ref){await api(base+'/bookmarks',{reference:ref,label:ref.quote.slice(0,50)});toast('已添加书签');}});
on('reader-scroll','scroll',()=>{
    if(edgeBounce.direction||edgeBounce.armed){const s=$('reader-scroll');if(s.scrollTop>4&&s.scrollTop+s.clientHeight<s.scrollHeight-4){resetEdgeBounce(false);disarmEdgeBounce();}}
    clearTimeout(tocTimer);tocTimer=setTimeout(updateTocFromScroll,80);
    clearTimeout(progressTimer);progressTimer=setTimeout(()=>{const ref=currentPosition();if(ref)api(base+'/state',ref,'PUT').catch(e=>toast(e.message));},500);
});
{
    const scroller=$('reader-scroll');
    scroller.addEventListener('wheel',e=>{
        if(state.chapterNavLock||!e.deltaY)return;
        const atTop=scroller.scrollTop<=4,atBottom=scroller.scrollTop+scroller.clientHeight>=scroller.scrollHeight-4;
        const direction=e.deltaY>0?1:-1, atEdge=direction>0?atBottom:atTop;
        if(!atEdge){if(edgeBounce.direction)resetEdgeBounce(false);disarmEdgeBounce();return;}
        if(edgeBounce.direction&&edgeBounce.direction!==direction){resetEdgeBounce(false);disarmEdgeBounce();}
        const magnitude=Math.abs(e.deltaY)*(e.deltaMode===1?16:e.deltaMode===2?scroller.clientHeight:1);
        applyEdgePull(direction,magnitude);
        const target=direction>0?(state.chapter<cfg.total-1?state.chapter+1:null):(state.chapter>0?state.chapter-1:null);
        const secondPush=edgeBounce.armed&&edgeBounce.events>=2&&edgeBounce.pull>=EDGE_BOUNCE_TRIGGER;
        const hardPush=edgeBounce.events>=EDGE_HARD_EVENTS&&edgeBounce.pull>=EDGE_HARD_PULL;
        if(target===null||(!secondPush&&!hardPush))return;
        disarmEdgeBounce();resetEdgeBounce(false);
        state.chapterNavLock=true;
        goto(target,0,true,direction>0?'top':'bottom').catch(err=>toast(err.message)).finally(()=>setTimeout(()=>state.chapterNavLock=false,400));
    },{passive:true});
}
on('generate-semantic','click',()=>generate({layer:'L3',chapter_idx:state.chapter,mode:'fill_missing',spec_type:$('agent-spec').value}));
on('toggle-semantic-rail','click',()=>setSemanticVisible(document.body.classList.contains('hide-semantic')));
document.querySelectorAll('[data-note-layer]').forEach(b => b.onclick = () => { document.querySelectorAll('.tab-dropdown button').forEach(el => el.blur()); openNoteLayer(b.dataset.noteLayer).catch(e => toast(e.message)); });
on('edit-note','click',()=>{state.editing=true;renderNote();$('note-editor').focus();});
on('note-editor','input',()=>{state.dirty=true;$('note-save-state').textContent='未保存';});on('save-note','click',saveNote);
on('cancel-note','click',()=>{if(!state.note||!state.editing)return;if(state.dirty&&!confirm('放弃未保存的修改？'))return;$('note-editor').value=state.note.content||'';state.dirty=false;state.editing=false;$('note-save-state').textContent='';renderNote();});
on('export-note','click',()=>download($('note-editor').value,(state.note?.key||'笔记')+'.md'));
on('generate-note','click',async()=>{if(!state.note)return;if(state.dirty){toast('请先保存当前笔记，再生成 AI 候选稿');return;}const n=state.note;if(n.layer==='L3')await generate({layer:'L3',paragraph_ref:n.target,mode:'overwrite',spec_type:$('agent-spec').value});else await generate({layer:n.layer,target:n.target,spec_type:$('agent-spec').value});});

on('note-list-toggle','click',toggleNoteList);
on('refresh-runs','click',refreshRuns);on('back-to-sessions','click',async()=>{showSessionList();await sessions();});
on('rename-session','click',async()=>{const id=menuSessionId;closeSessionMenu();if(!id)return;const item=state.sessions.find(s=>s.id===id);if(!item)return;const title=prompt('对话名称',item.title||'新对话');if(title===null)return;const value=title.trim();if(!value){toast('对话名称不能为空');return;}await api(chatBase+'/sessions/'+id,{title:value},'PATCH');await sessions();if(state.session===id)$('chat-session-title').textContent=value;toast('已重命名');});
on('delete-session','click',async()=>{const id=menuSessionId;closeSessionMenu();if(!id||!confirm('删除此对话及其历史？'))return;if(state.session===id){if(chatStream)chatStream.close();state.session='';state.messages=[];state.activeMessage='';setChatActive(false);renderMessages();}await api(chatBase+'/sessions/'+id,undefined,'DELETE');await sessions();showSessionList();toast('对话已删除');});
on('export-chat','click',()=>{if(state.session)location.href=chatBase+'/sessions/'+state.session+'/export';});on('send-chat','click',send);
on('stop-chat','click',async()=>{if(state.activeMessage)await api(chatBase+'/sessions/'+state.session+'/messages/'+state.activeMessage+'/cancel',{});});

on('session-new-input','input',()=>{$('session-new-send').disabled=!$('session-new-input').value.trim();});
on('session-new-input','keydown',e=>{if(e.key==='Enter'&&!e.shiftKey&&!e.isComposing){e.preventDefault();submitFromList().catch(error=>toast(error.message));}});
on('session-new-send','click',submitFromList);
on('chat-input','input',()=>{delete $('chat-input').dataset.requestId;updateMentions();});
on('chat-input','keydown',e=>{if(e.isComposing)return;if(mention){if(['ArrowDown','ArrowUp'].includes(e.key)){e.preventDefault();mention.index=(mention.index+(e.key==='ArrowDown'?1:-1)+mention.matches.length)%Math.max(1,mention.matches.length);renderMentions();return;}if(e.key==='Escape'){e.preventDefault();closeMentions();return;}if(e.key==='Enter'&&mention.matches.length){e.preventDefault();chooseMention(mention.index);return;}}if(e.key==='Enter'&&!e.shiftKey){e.preventDefault();if(!state.activeMessage)return send();}});
on('chapter-content','mouseup',captureSelection);on('chapter-content','keyup',captureSelection);
$('selection-actions').addEventListener('mousedown',e=>e.preventDefault());
on('selection-chat','click',async()=>{
    const refs=state.selected.slice();$('selection-actions').hidden=true;
    if(!refs.length)return;
    if(!$('chat-thread-view').hidden){panel('chat');showThreadView();addReferences(refs);return;}
    // 列表页/无会话时另起新对话，避免把上一个会话未发送的引用带进来。
    state.references=[];renderChips();
    await newSession();
    addReferences(refs);
});
on('selection-semantic','click',()=>{const r=state.selected[0];$('selection-actions').hidden=true;if(r)return openNote('L3',{chapter_idx:r.chapter_idx,paragraph_index:r.paragraph_index,fingerprint:r.fingerprint});});
on('selection-concept','click',async()=>{
    if(state.selected.length!==1){toast('解释概念时请只选一个段落内的术语');return;}
    const r=state.selected[0],target={chapter_idx:r.chapter_idx,paragraph_index:r.paragraph_index,fingerprint:r.fingerprint,term:r.quote};
    $('selection-actions').hidden=true;await openNote('L4',target);
    try { await generate({layer:'L4',target,spec_type:$('agent-spec').value}); }
    catch(error) {
        const envelope=error.detail;
        const decision=envelope?.details&&!Array.isArray(envelope.details)?{...envelope.details,message:envelope.message}:envelope;
        if(decision?.action==='confirm'&&decision.candidates?.length){
            const term=prompt(decision.message+'\n'+decision.candidates.join(' / '),decision.candidates[0]);
            if(term){await openNote('L4',{...target,term});await generate({layer:'L4',target:{...target,term},spec_type:$('agent-spec').value});}
        } else throw error;
    }
});
window.addEventListener('beforeunload',e=>{if(state.dirty){e.preventDefault();e.returnValue='';}});

initReadingCycles();
setSemanticVisible(true);
async function init(){const settingsLink=$('open-settings');if(settingsLink)settingsLink.href='/settings?from='+encodeURIComponent(location.pathname+location.search+location.hash);initSidebars();applyResponsiveLayout(true);const bookInfo=await api('/api/v1/books/'+book);cfg.title=bookInfo.title;cfg.total=bookInfo.chapter_count;cfg.anchorMap=bookInfo.anchor_map||{};document.title=cfg.title+' · Gnosys';const data=await api(base+'/outline');state.nodes=data.items;await nav('outline');const match=location.hash.match(/source=(\d+):(\d+)/);await goto(match?Number(match[1]):state.chapter,match?Number(match[2]):0,false);await sessions();showSessionList();if(cfg.noteMode)await openNote('L1');window.GNOSYS.ready=true;}
init().catch(e=>toast(e.message));
