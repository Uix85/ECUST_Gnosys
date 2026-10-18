// Run via ego-browser nodejs < tests/browser_e2e.mjs while browser_server.py is running.
const assert = (await import('node:assert/strict')).default;
const fs = await import('node:fs/promises');
const testConfig=globalThis.GNOSYS_TEST || {};
const task = testConfig.spaceId ? await taskSpace(testConfig.spaceId) : await taskSpace('Gnosys browser E2E');
const page = task.page('p1'), origin = testConfig.origin || 'http://127.0.0.1:8137';
const results = [], report = testConfig.report || '/tmp/gnosys-browser-e2e.json';
const uploadFixture='/tmp/端到端导入样书.txt';
await fs.writeFile(uploadFixture,'端到端导入样书\n第一章 临时测试\n这是只在浏览器验收中使用的本地样书。');
function pass(name, evidence) { results.push({name, status:'passed', evidence}); console.log('PASS ' + name); }
async function visible(selector) { await page.waitForSelector(selector, {state:'visible'}); }
async function ready() { await page.waitForFunction(() => window.GNOSYS?.ready && document.querySelector('#chapter-content[aria-busy=false]') && document.querySelectorAll('.source').length > 0); }
async function waitRun(layer, status='completed') { await page.waitForFunction(([layer,status]) => [...document.querySelectorAll('#runs-list .run-card header')].some(node=>node.textContent.includes(layer+' · '+status)),[layer,status],{timeout:15000}); }
async function viewport(width, height=980) {
    // Ego Lite can apply a profile zoom. Account for it, then assert the actual CSS viewport.
    let metricWidth=width,metricHeight=height;
    await page.cdp('Emulation.setDeviceMetricsOverride', {width:metricWidth,height:metricHeight,deviceScaleFactor:1,mobile:false});
    let actual = await page.evaluate(() => innerWidth);
    if (Math.abs(actual-width)>1) {
        const zoom=width/actual;
        metricWidth=Math.round(metricWidth*zoom);metricHeight=Math.round(metricHeight*zoom);
        await page.cdp('Emulation.setDeviceMetricsOverride', {width:metricWidth,height:metricHeight,deviceScaleFactor:1,mobile:false});
        actual=await page.evaluate(()=>innerWidth);
    }
    for(let tries=0;actual!==width&&tries<4;tries++){
        metricWidth+=actual<width?1:-1;
        await page.cdp('Emulation.setDeviceMetricsOverride',{width:metricWidth,height:metricHeight,deviceScaleFactor:1,mobile:false});
        actual=await page.evaluate(()=>innerWidth);
    }
    const dimensions=await page.evaluate(()=>({width:innerWidth,height:innerHeight,scroll:document.documentElement.scrollWidth}));
    assert.equal(dimensions.width,width,JSON.stringify(dimensions));
    return dimensions;
}
await page.cdp('Page.addScriptToEvaluateOnNewDocument',{source:'window.__qaErrors=[];addEventListener("error",e=>window.__qaErrors.push(e.message));addEventListener("unhandledrejection",e=>window.__qaErrors.push(String(e.reason)));'});
await page.goto(origin+'/');
await viewport(1440);
console.log(await page.snapshot());
await page.fill('#library-search','没有这本书');
assert.equal(await page.evaluate(()=>!document.querySelector('#no-results').hidden),true);
await page.fill('#library-search','社会');
await page.evaluate(()=>localStorage.setItem('gnosys-inspector-width','380'));
await page.click('text=继续阅读'); await ready();
pass('书库搜索与继续阅读',await page.url());
await page.click('#new-session'); await page.waitForFunction(()=>document.querySelectorAll('.message.assistant').length===0);
await page.evaluate(()=>window.prompt=()=> '浏览器验收对话');await page.click('#rename-session');await page.waitForFunction(()=>document.querySelector('#sessions').selectedOptions[0]?.textContent==='浏览器验收对话');

await page.fill('#chat-input','@实践');
assert.ok(await page.evaluate(()=>document.querySelectorAll('#mention-list [role=option]').length>=2));
await page.press('#chat-input','ArrowDown');await page.press('#chat-input','Enter');
await page.fill('#chat-input','@社会秩序');await page.press('#chat-input','Enter');
assert.equal(await page.evaluate(()=>document.querySelectorAll('#reference-chips .reference-chip').length),2);
await page.fill('#chat-input','@');await page.press('#chat-input','Escape');
assert.equal(await page.evaluate(()=>document.querySelector('#mention-list').hidden),true);
await page.fill('#chat-input','比较这几处原文');
await page.evaluate(()=>{const p=document.querySelector('#paragraph-3 p'),r=document.createRange();r.setStart(p.firstChild,0);r.setEnd(p.firstChild,4);const s=getSelection();s.removeAllRanges();s.addRange(r);p.dispatchEvent(new MouseEvent('mouseup',{bubbles:true}));});
await visible('#selection-actions');await page.click('#selection-chat');
assert.equal(await page.evaluate(()=>document.querySelectorAll('#reference-chips .reference-chip').length),3);
await page.click('#reference-chips .reference-chip button:first-child >> nth=0');
await page.waitForFunction(()=>location.hash.startsWith('#source=0:'));
await page.click('#reference-chips button[aria-label^="移除引用"] >> nth=0');
assert.equal(await page.evaluate(()=>document.querySelectorAll('#reference-chips .reference-chip').length),2);
pass('多层目录补全、引用跳转/移除、Esc 与划线引用',await page.evaluate(()=>({chips:document.querySelectorAll('#reference-chips .reference-chip').length,source:location.hash})));

await page.click('#send-chat');
await page.waitForFunction(()=>document.querySelector('.message.assistant .message-role')?.textContent.includes('completed'),undefined,{timeout:15000});
let rendered=await page.evaluate(()=>({table:!!document.querySelector('.message table'),nested:!!document.querySelector('.message ul ul'),math:!!document.querySelector('.message .katex'),highlight:!!document.querySelector('.message .hljs-number'),checked:document.querySelector('.message input[type=checkbox]')?.checked,source:!!document.querySelector('.message a[href="#source=0:3"]')}));
assert.ok(Object.values(rendered).every(Boolean),JSON.stringify(rendered));pass('流式对话及完整 Markdown',rendered);
await page.click('.message.assistant a[href="#source=0:3"]');await page.waitForFunction(()=>location.hash==='#source=0:3');
pass('对话来源可点击返回原文',await page.evaluate(()=>location.hash));
await page.evaluate(()=>{window.__copied='';Object.defineProperty(navigator,'clipboard',{configurable:true,value:{writeText:async text=>{window.__copied=text;}}});});
await page.click('.message.assistant .message-actions button >> nth=0');
await page.waitForFunction(()=>window.__copied.includes('阅读分析'));
await page.click('.message.assistant .message-actions button >> nth=1');
await page.waitForFunction(()=>document.querySelector('#note-layer').value==='chat'&&document.querySelector('#note-save-state').textContent==='已保存');
assert.ok(await page.evaluate(()=>document.querySelector('#note-editor').value.includes('公式：')));
pass('Agent 回答复制并保存为对话摘记',true);
await page.click('[data-panel=chat]');
await page.fill('#chat-input','继续说明');await page.press('#chat-input','Enter');
await page.waitForFunction(()=>document.querySelectorAll('.message.assistant').length===2&&!document.querySelector('#send-chat').disabled,undefined,{timeout:15000});
await page.reload();await ready();await page.waitForFunction(()=>document.querySelectorAll('.message.assistant').length===2);
pass('追问与刷新后的会话历史',2);
await page.fill('#chat-input','测试停止后刷新续接');await page.click('#send-chat');
await page.waitForFunction(()=>[...document.querySelectorAll('.message.assistant')].some(m=>{const status=m.querySelector('.message-role')?.textContent||'';return !status.includes('completed')&&!status.includes('failed')&&m.querySelector('.message-content')?.textContent.length>0}),undefined,{timeout:10000});
await page.reload();await ready();await page.waitForFunction(()=>{const rows=[...document.querySelectorAll('.message.assistant')];return rows.length===3&&rows.at(-1)?.querySelector('.message-role')?.textContent.includes('completed')},undefined,{timeout:15000});
const resumed=await page.evaluate(()=>{const rows=[...document.querySelectorAll('.message.assistant')];return {count:rows.length,analysis:(rows.at(-1)?.textContent.match(/阅读分析/g)||[]).length,formula:rows.at(-1)?.textContent.includes('公式')}});
assert.equal(resumed.count,3);assert.equal(resumed.analysis,1);assert.equal(resumed.formula,true);pass('流式中刷新后续接且内容不重复',resumed);

const security=await page.evaluate(()=>{const box=document.createElement('div');box.innerHTML=renderMarkdown('<img src=x onerror="window.__xss=1">\n\n[x](javascript:alert(1))\n\n<svg onload=alert(1)></svg>\n\n~~删除~~\n\n> 引用\n\n$$x^2$$\n\n```js\nconst x = 1\n');return {unsafe:!!box.querySelector('script,iframe,svg,[onerror],[onload],a[href^="javascript:"]'),strike:!!box.querySelector('s'),quote:!!box.querySelector('blockquote'),math:!!box.querySelector('.katex'),unfinishedCode:!!box.querySelector('pre code')};});
assert.equal(security.unsafe,false);assert.ok(security.strike&&security.quote&&security.math&&security.unfinishedCode);pass('危险 HTML/链接与未闭合代码块',security);

await page.click('[data-panel="note"]');await page.evaluate(()=>{const s=document.querySelector('#note-layer');s.value='L1';s.dispatchEvent(new Event('change',{bubbles:true}));});await page.waitForFunction(()=>document.querySelector('#note-layer').value==='L1');await page.click('#edit-note');await page.fill('#note-editor','## E2E 手工笔记\n\n- 原文依据\n- 读者记录');await page.click('#save-note');
await page.waitForFunction(()=>document.querySelector('#note-save-state').textContent==='已保存');
await page.click('#edit-note');assert.ok((await page.evaluate(()=>document.querySelector('#note-preview').textContent)).includes('E2E 手工笔记'));
const noteDownload=page.waitForEvent('download');await page.click('#export-note');const noteFile=await noteDownload;await noteFile.saveAs('/tmp/gnosys-e2e-note.md');assert.ok((await fs.readFile('/tmp/gnosys-e2e-note.md','utf8')).includes('E2E 手工笔记'));
pass('笔记编辑、预览、保存与 Markdown 导出','/tmp/gnosys-e2e-note.md');

await page.click('#generate-note');
await waitRun('L1');
pass('全书 AI 从页面触发至完整覆盖汇总','completed');
await page.click('[data-panel="note"]');await page.evaluate(()=>{const s=document.querySelector('#note-layer');s.value='L1';s.dispatchEvent(new Event('change',{bubbles:true}));});await page.waitForFunction(()=>document.querySelector('#note-layer').value==='L1');

// Actual concurrent save through another client: the UI must retain unsaved input.
await page.click('#edit-note');await page.fill('#note-editor','浏览器仍未保存的修改');
await page.evaluate(async()=>{const base='/api/workspace/'+encodeURIComponent(GNOSYS.bookId),n=await(await fetch(base+'/note/read',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({layer:'L1'})})).json();await fetch(base+'/note',{method:'PUT',headers:{'Content-Type':'application/json'},body:JSON.stringify({layer:'L1',content:'另一个窗口保存',baseline:{revision:n.revision,content:n.content,legacy_version:n.legacy_version||''}})});});
const conflictDownload=page.waitForEvent('download');await page.click('#save-note');const conflict=await conflictDownload;await conflict.saveAs('/tmp/gnosys-e2e-conflict.md');
assert.equal(await page.evaluate(()=>document.querySelector('#note-editor').value),'浏览器仍未保存的修改');
assert.ok((await page.evaluate(()=>document.querySelector('#note-save-state').textContent)).includes('冲突'));
pass('并发保存冲突保留输入并导出备份','/tmp/gnosys-e2e-conflict.md');
// Discard only the QA draft through the UI's explicit dialog.
await page.click('[data-panel="note"]');await page.evaluate(()=>{const s=document.querySelector('#note-layer');s.value='L2';s.dispatchEvent(new Event('change',{bubbles:true}));});
await page.waitForFunction(()=>document.querySelector('#note-layer').value==='L2');
await page.click('#generate-note');await waitRun('L2');
pass('章节 AI 从页面触发至完成','completed');
await page.click('#generate-semantic');await page.waitForFunction(()=>document.querySelector('.semantic-note .short'),undefined,{timeout:15000});
pass('语义 AI 与段落旁注发布',true);

await page.evaluate(()=>{const p=document.querySelector('#paragraph-3 p'),r=document.createRange();r.setStart(p.firstChild,0);r.setEnd(p.firstChild,4);const s=getSelection();s.removeAllRanges();s.addRange(r);p.dispatchEvent(new MouseEvent('mouseup',{bubbles:true}));});
await page.click('#selection-semantic');
await page.waitForFunction(()=>document.querySelector('#note-layer').value==='L3'&&document.querySelector('#note-preview').textContent.length>0);
pass('原文划线打开该段语义笔记',await page.evaluate(()=>document.querySelector('#note-target').textContent));
await page.evaluate(()=>{const p=document.querySelector('#paragraph-3 p'),r=document.createRange();r.setStart(p.firstChild,0);r.setEnd(p.firstChild,4);const s=getSelection();s.removeAllRanges();s.addRange(r);p.dispatchEvent(new MouseEvent('mouseup',{bubbles:true}));});
await page.click('#selection-concept');
await waitRun('L4');
pass('划词概念 AI、意义地图与当前位置解释','completed');

await page.click('#next');await page.waitForFunction(()=>document.querySelector('#chapter-title').textContent.includes('第二章'));
await page.click('#open-chapter-note');await page.waitForFunction(()=>document.querySelector('#note-layer').value==='L2');
await page.evaluate(async()=>fetch('/__qa/model',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({fail:true})}));
await page.click('#generate-note');await waitRun('L2','failed');
const runsBeforeRetry=await page.evaluate(async()=> (await (await fetch('/api/agent/'+encodeURIComponent(GNOSYS.bookId)+'/runs')).json()).items.map(x=>x.id));
await page.evaluate(async()=>fetch('/__qa/model',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({fail:false,approve:true})}));
await page.evaluate(()=>{const card=[...document.querySelectorAll('#runs-list .run-card')].find(x=>x.querySelector('header')?.textContent.includes('L2 · failed'));card.querySelector('button').click();});
await page.waitForFunction(async(before)=>{const items=(await(await fetch('/api/agent/'+encodeURIComponent(GNOSYS.bookId)+'/runs')).json()).items;return items.some(x=>!before.includes(x.id)&&x.layer==='L2'&&x.request.target.chapter_idx===1);},runsBeforeRetry,{timeout:15000});
const retriedRunId=await page.evaluate(async(before)=>{const items=(await(await fetch('/api/agent/'+encodeURIComponent(GNOSYS.bookId)+'/runs')).json()).items;return items.find(x=>!before.includes(x.id)&&x.layer==='L2'&&x.request.target.chapter_idx===1)?.id;},runsBeforeRetry);
assert.ok(retriedRunId);
await page.waitForFunction(id=>document.querySelector('#run-'+id)?.querySelector('header')?.textContent.includes('L2 · completed'),retriedRunId,{timeout:15000});
await page.waitForTimeout(300); // Let the completed-run handler finish reopening its note.
pass('章节任务失败后从任务面板重试','completed');
await page.evaluate(async()=>fetch('/__qa/model',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({delay:.4,approve:true,fail:false})}));
await page.click('[data-panel="note"]');await page.waitForFunction(()=>!document.querySelector('#note-panel').hidden);
const runsBeforeCancel=await page.evaluate(async()=> (await (await fetch('/api/agent/'+encodeURIComponent(GNOSYS.bookId)+'/runs')).json()).items.map(x=>x.id));
await page.click('#generate-note');
await page.waitForFunction(async(before)=>{const items=(await(await fetch('/api/agent/'+encodeURIComponent(GNOSYS.bookId)+'/runs')).json()).items;return items.some(x=>!before.includes(x.id)&&x.layer==='L2'&&x.status==='running');},runsBeforeCancel,{timeout:10000});
const cancelRunId=await page.evaluate(async(before)=>{const items=(await(await fetch('/api/agent/'+encodeURIComponent(GNOSYS.bookId)+'/runs')).json()).items;return items.find(x=>!before.includes(x.id)&&x.layer==='L2'&&x.status==='running')?.id;},runsBeforeCancel);
await page.evaluate(id=>document.querySelector('#run-'+id+' button')?.click(),cancelRunId);
await page.waitForFunction(async(id)=>{const item=await(await fetch('/api/agent/'+encodeURIComponent(GNOSYS.bookId)+'/runs/'+id)).json();return item.status==='canceled';},cancelRunId,{timeout:10000});
await page.evaluate(async()=>fetch('/__qa/model',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({delay:.015,approve:true,fail:false})}));
pass('运行中的章节任务可从页面取消',cancelRunId);
await page.evaluate(async()=>fetch('/__qa/model',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({approve:false})}));
await page.click('[data-panel="note"]');
await page.waitForFunction(()=>document.querySelector('[data-panel="note"]').getAttribute('aria-selected')==='true'&&!document.querySelector('#note-panel').hidden);
await page.waitForTimeout(300);
await page.click('#generate-note');await waitRun('L2','needs_review');
await page.waitForFunction(()=>document.querySelector('#pending-list .run-card button'));
await page.click('#pending-list .run-card button >> nth=0');
await page.waitForFunction(()=>document.querySelector('#pending-list').children.length===0);
await page.evaluate(async()=>fetch('/__qa/model',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({approve:true,fail:false})}));
pass('待核稿在页面预览后由读者采纳',await page.evaluate(()=>document.querySelector('#pending-list').children.length));

await page.click('[data-panel=chat]');const previous=await page.evaluate(()=>document.querySelectorAll('.message.assistant').length);
await page.fill('#chat-input','测试停止');await page.click('#send-chat');await visible('#stop-chat');await page.click('#stop-chat');
await page.waitForFunction(count=>document.querySelectorAll('.message.assistant').length>count&&[...document.querySelectorAll('.message-role')].some(n=>n.textContent.includes('canceled')),previous,{timeout:15000});
pass('停止回答并保留历史',true);
await page.click('.message.assistant:last-child .message-actions button >> nth=2');
await page.waitForFunction(()=>document.querySelector('.message.assistant:last-child .message-role')?.textContent.includes('completed'),undefined,{timeout:15000});
pass('停止的对话可从页面重试',true);
const exportPromise=page.waitForEvent('download');await page.click('#export-chat');const chatFile=await exportPromise;await chatFile.saveAs('/tmp/gnosys-e2e-chat.md');assert.ok((await fs.readFile('/tmp/gnosys-e2e-chat.md','utf8')).includes('比较这几处原文'));
pass('对话 Markdown 导出','/tmp/gnosys-e2e-chat.md');
await page.evaluate(()=>window.confirm=()=>true);await page.click('#delete-session');await page.waitForFunction(()=>document.querySelector('#sessions').value==='');pass('会话重命名与删除',true);

await page.click('[data-nav=search]');await page.fill('#search-input','社会关系');await page.press('#search-input','Enter');await visible('.search-result');
assert.ok(await page.evaluate(()=>document.querySelectorAll('.search-result').length>=2));
await page.click('.search-result button >> nth=0');await page.click('#add-bookmark');await page.click('[data-nav=bookmarks]');await visible('.bookmark-row');
pass('全文搜索、原文定位与书签',true);
for(let i=0;i<5;i++){if((await page.evaluate(()=>localStorage.getItem('gnosys-font-size')))==='22')break;await page.click('#font-size-cycle');}
for(let i=0;i<3;i++){if((await page.evaluate(()=>localStorage.getItem('gnosys-line-height')))==='2.2')break;await page.click('#line-height-cycle');}
await page.reload();await ready();
assert.equal(await page.evaluate(()=>localStorage.getItem('gnosys-font-size')),'22');assert.equal(await page.evaluate(()=>localStorage.getItem('gnosys-line-height')),'2.2');
pass('阅读字号与行距持久化',true);
await viewport(1440);
const beforeResize=await page.evaluate(()=>{const r=document.querySelector('#inspector').getBoundingClientRect();return {width:r.width,x:r.right-2,y:r.bottom-2}});
assert.ok(beforeResize.width>=360,beforeResize.width);
const dragBy=-35;
await page.cdp('Input.dispatchMouseEvent',{type:'mouseMoved',x:beforeResize.x,y:beforeResize.y});await page.cdp('Input.dispatchMouseEvent',{type:'mousePressed',x:beforeResize.x,y:beforeResize.y,button:'left',clickCount:1});
await page.cdp('Input.dispatchMouseEvent',{type:'mouseMoved',x:beforeResize.x+dragBy,y:beforeResize.y,button:'left'});await page.cdp('Input.dispatchMouseEvent',{type:'mouseReleased',x:beforeResize.x+dragBy,y:beforeResize.y,button:'left',clickCount:1});
const resized=await page.evaluate(()=>({width:document.querySelector('#inspector').getBoundingClientRect().width,saved:Number(localStorage.getItem('gnosys-inspector-width'))}));
assert.ok(Math.abs(resized.width-beforeResize.width)>20);assert.ok(Math.abs(resized.width-resized.saved)<1);
await page.reload();await ready();assert.ok(Math.abs(await page.evaluate(()=>document.querySelector('#inspector').getBoundingClientRect().width)-resized.saved)<1);pass('辅助面板拖动与宽度持久化',resized);

const layouts=[];
for(const width of [1440,1024]){
    const size=await viewport(width);const layout=await page.evaluate(()=>({viewport:innerWidth,scroll:document.documentElement.scrollWidth,reading:document.querySelector('#reading').getBoundingClientRect().width,panel:document.querySelector('#inspector').getBoundingClientRect().width,sendRight:document.querySelector('#send-chat').getBoundingClientRect().right,appbarRight:Math.max(...[...document.querySelectorAll('.appbar>*')].map(e=>e.getBoundingClientRect().right))}));
    assert.equal(layout.scroll,layout.viewport);assert.ok(layout.reading>=320);assert.ok(layout.sendRight<=layout.viewport+1,JSON.stringify(layout));assert.ok(layout.appbarRight<=layout.viewport+1,JSON.stringify(layout));layouts.push(layout);
    await page.screenshot({path:'/tmp/gnosys-e2e-'+width+'.png',fullPage:true,raw:true});
}
pass('1440 与 1024 桌面布局',layouts);
await page.cdp('Emulation.setEmulatedMedia',{features:[{name:'prefers-reduced-motion',value:'reduce'}]});
assert.equal(await page.evaluate(()=>getComputedStyle(document.querySelector('#reader-scroll')).scrollBehavior),'auto');pass('减少动态效果设置',true);
await viewport(390,844);await page.reload();await ready();
let mobile=await page.evaluate(()=>({viewport:innerWidth,scroll:document.documentElement.scrollWidth,navHidden:document.body.classList.contains('no-navigation'),panelHidden:document.body.classList.contains('no-inspector')}));
assert.equal(mobile.scroll,mobile.viewport);assert.ok(mobile.navHidden&&mobile.panelHidden);
await page.click('#toggle-nav');await page.click('#toggle-panel');
mobile=await page.evaluate(()=>({viewport:innerWidth,scroll:document.documentElement.scrollWidth,navOpen:document.querySelector('#toggle-nav').getAttribute('aria-expanded'),panelOpen:document.querySelector('#toggle-panel').getAttribute('aria-expanded'),panelRight:document.querySelector('#inspector').getBoundingClientRect().right}));
assert.equal(mobile.scroll,mobile.viewport);assert.equal(mobile.navOpen,'true');assert.equal(mobile.panelOpen,'true');assert.ok(mobile.panelRight<=mobile.viewport);
await page.screenshot({path:'/tmp/gnosys-e2e-mobile.png',fullPage:true,raw:true});pass('390px 手机视口及抽屉开合',mobile);

await page.goto(origin+'/');
await page.setInputFiles('#book-file',[uploadFixture]);
await page.waitForFunction(()=>[...document.querySelectorAll('.book-card h2')].some(n=>n.textContent==='端到端导入样书'),undefined,{timeout:15000});
const imported=await page.evaluate(()=>{const card=[...document.querySelectorAll('.book-card')].find(n=>n.querySelector('h2')?.textContent==='端到端导入样书');return {id:decodeURIComponent(new URL(card.querySelector('a[href^="/read/"]').href).pathname.split('/')[2]),title:card.querySelector('h2').textContent};});
await page.click('loc=href:/read/'+encodeURIComponent(imported.id));await ready();
assert.ok(await page.evaluate(()=>document.querySelector('#chapter-title').textContent.includes('第一章')));
await page.goto(origin+'/');await page.evaluate(()=>window.confirm=()=>true);
await page.click('.delete-book[data-book="'+imported.id+'"]');
await page.waitForFunction(()=>![...document.querySelectorAll('.book-card h2')].some(n=>n.textContent==='端到端导入样书'));
const missing=await page.fetch('/api/books/'+encodeURIComponent(imported.id),{method:'DELETE'});assert.equal(missing.status,404);
pass('书库导入、打开、删除与只读当前临时书籍',imported);
const errors=await page.evaluate(()=>window.__qaErrors||[]);assert.deepEqual(errors,[]);pass('浏览器未捕获异常',errors);
await fs.writeFile(report,JSON.stringify({origin,spaceId:task.spaceId,passed:results.length,results},null,2));console.log('REPORT '+report);
if(!testConfig.spaceId)await task.finish({keep:[]});
