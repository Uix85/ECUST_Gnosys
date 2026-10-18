"""Isolated HTTP → SDK → SQLite integration tests; never opens the user's book directory."""
import asyncio
import io
import json
import os
from pathlib import Path
import sqlite3
import socket
import sys
import tempfile
import unittest
from contextlib import suppress
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / 'backend'), str(ROOT / 'tests')]
TEMP = tempfile.TemporaryDirectory(prefix='gnosys-test-')
os.environ['GNOSYS_BOOKS_DIR'] = str(Path(TEMP.name) / 'books')
from db import paths as db_paths, settings as settings_db
db_paths.SETTINGS_DB = Path(TEMP.name) / 'settings.db'
settings_db.SETTINGS_DB = db_paths.SETTINGS_DB
settings_db.save({'AGENT_API_KEY': 'offline-test-key'})
from httpx import AsyncClient, ASGITransport
from server import app
from agent.tools.book import book_db, get_paragraph, logical_paragraphs, split_sentences
from reader.workspace import outline_nodes, get_note, baseline, save_note, reference
from reader.import_document import import_document
from db.schema import get_db, close_db
from fake_model import ReadingModel, SOURCE
import fake_model

TEXT = '第一章 开始\n第一节 方法\n一、观察\n' + SOURCE + '\n二、讨论\n社会关系意味着人们在实践中结成的联系。这些联系也会影响我们的生活。\n第二章 延续\n第一节 方法\n社会关系在后续行动中逐渐稳定。人们不断修订彼此的理解。'


class WorkspaceTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.client = AsyncClient(transport=ASGITransport(app=app), base_url='http://test')
        r = await self.client.post('/upload', files={'file':('研究.txt', TEXT.encode(),'text/plain')})
        self.assertEqual(r.status_code,200,r.text)
        self.book = r.json()['folder']; fake_model.BOOK_ID=self.book
        self.base='/api/workspace/'+self.book; self.runs='/api/agent/'+self.book+'/runs'; self.chat='/api/chat/'+self.book
        self.model=ReadingModel()
        self.patches=[patch('agent.runs.layer.build_model',return_value=self.model),patch('agent.layers.l3_paragraph.build_model',return_value=self.model),patch('agent.chat.build_model',return_value=self.model)]
        for p in self.patches:p.start()

    async def asyncTearDown(self):
        from agent.runs.loop import _run_tasks, _cancel_signals
        from agent.chat import ACTIVE as CA,SIGNALS
        for signal in list(_cancel_signals.values())+list(SIGNALS.values()):signal.set()
        tasks=list(_run_tasks.values())+list(CA.values())
        if tasks:await asyncio.gather(*tasks,return_exceptions=True)
        for p in self.patches:p.stop()
        await self.client.aclose()

    async def wait_run(self, run_id):
        for _ in range(500):
            r=(await self.client.get(self.runs+'/'+run_id)).json()
            if r['status'] not in ('queued','running','canceling'):return r
            await asyncio.sleep(.01)
        self.fail('run timeout')

    async def send(self, session, text='解释原文', refs=None, req='request-1'):
        return await self.client.post(self.chat+'/sessions/'+session+'/messages',json={'content':text,'references':refs or [],'request_id':req})

    async def wait_chat(self, session):
        for _ in range(500):
            data=(await self.client.get(self.chat+'/sessions/'+session)).json()
            if data['messages'] and data['messages'][-1]['status'] not in ('queued','running'):return data
            await asyncio.sleep(.01)
        self.fail('chat timeout')

    async def test_import_formats_names_and_failure(self):
        r=await self.client.post('/upload',files={'file':('研究.txt',TEXT.encode('gb18030'),'text/plain')})
        self.assertEqual(r.status_code,200,r.text);self.assertNotEqual(r.json()['folder'],self.book)
        original=book_db(self.book).execute('SELECT source_file FROM books').fetchone()[0];self.assertEqual(original,'研究.txt')
        utf16=await self.client.post('/upload',files={'file':('UTF16.txt',TEXT.encode('utf-16'),'text/plain')})
        self.assertEqual(utf16.status_code,200,utf16.text)
        self.assertEqual(book_db(utf16.json()['folder']).execute('SELECT source_file FROM books').fetchone()[0],'UTF16.txt')
        for name,content in [('坏.epub',b'bad'),('空.txt',b''),('坏.txt',b'\x00\x01\x00'),('bad.pdf',b'bad')]:
            r=await self.client.post('/upload',files={'file':(name,content)});self.assertEqual(r.status_code,400,r.text)
        self.assertFalse(any(p.name.startswith('.import-') for p in Path(os.environ['GNOSYS_BOOKS_DIR']).iterdir()))
        r=await self.client.post('/upload',files={'file':('无标题.txt','纯文本没有标题。\n下一段内容。'.encode())});self.assertEqual(r.status_code,200,r.text)
        from ebooklib import epub
        with tempfile.TemporaryDirectory() as d:
            b=epub.EpubBook();b.set_identifier('fixture');b.set_title('EPUB 测试');b.set_language('zh')
            from PIL import Image
            pixels=Path(d)/'pixel.png';image_data=Image.new('RGB',(4,4));image_data.putdata([(220,40,80),(20,180,70),(30,60,220),(240,210,10)]*4);image_data.save(pixels)
            image=epub.EpubItem(uid='pixel',file_name='images/pixel.png',media_type='image/png',content=pixels.read_bytes())
            chapter=epub.EpubHtml(title='第一章',file_name='one.xhtml',lang='zh');chapter.content='<h1>第一章</h1><h2>第一节</h2><h3>子节</h3><p>'+SOURCE+'</p><img src="images/pixel.png" onerror="alert(1)">';b.add_item(chapter);b.add_item(image);b.spine=['nav',chapter];b.toc=(chapter,);b.add_item(epub.EpubNcx());b.add_item(epub.EpubNav());path=Path(d)/'书.epub';epub.write_epub(str(path),b)
            r=await self.client.post('/upload',files={'file':('书.epub',path.read_bytes(),'application/epub+zip')});self.assertEqual(r.status_code,200,r.text)
            folder=r.json()['folder'];nodes=outline_nodes(folder);self.assertTrue(any(n['depth']>=2 for n in nodes),nodes)
            chapter=await self.client.get(f'/api/full_chapter/{folder}/0');self.assertEqual(chapter.status_code,200,chapter.text)
            self.assertIn('images/pixel.png',chapter.json()['html']);self.assertNotIn('onerror',chapter.json()['html'])
            served=await self.client.get(f'/read/{folder}/images/pixel.png');self.assertEqual(served.status_code,200);self.assertEqual(served.content[:8],b'\x89PNG\r\n\x1a\n')
            self.assertEqual((await self.client.get(f'/api/full_chapter/{folder}/99')).status_code,404)

    async def test_outline_reference_search_state_bookmarks(self):
        data=(await self.client.get(self.base+'/outline')).json()['items'];self.assertEqual(len({n['id'] for n in data}),len(data));self.assertTrue(any(n['depth']==2 for n in data))
        self.assertGreaterEqual(sum(n['title']=='第一节 方法' for n in data),2)
        p=get_paragraph(self.book,0,3);ref={'chapter_idx':0,'paragraph_index':3,'fingerprint':p['fingerprint'],'quote':SOURCE[:12]}
        r=await self.client.post(self.base+'/reference/check',json={'references':[ref,{'node_id':'c0:p1'}]});self.assertEqual(r.status_code,200,r.text)
        r=await self.client.post(self.base+'/reference/check',json={'references':[{**ref,'fingerprint':'stale'}]});self.assertEqual(r.status_code,409)
        r=await self.client.put(self.base+'/state',json=ref);self.assertEqual(r.status_code,200);self.assertEqual((await self.client.get(self.base+'/state')).json()['paragraph_index'],3)
        r=await self.client.get('/read/'+self.book,follow_redirects=False);self.assertIn('#source=0:3',r.headers['location'])
        r=await self.client.get(self.base+'/search',params={'q':'社会关系'});self.assertEqual(r.status_code,200);self.assertGreater(len(r.json()['items']),1)
        mark=(await self.client.post(self.base+'/bookmarks',json={'reference':ref})).json()['id'];self.assertEqual(len((await self.client.get(self.base+'/bookmarks')).json()['items']),1)
        await self.client.delete(self.base+'/bookmarks/'+mark);self.assertFalse((await self.client.get(self.base+'/bookmarks')).json()['items'])
        r=await self.client.get(self.base+'/chapter/0');self.assertEqual(r.status_code,200);self.assertEqual(r.json()['paragraphs'][3]['text'],SOURCE)

    async def test_locator_mode_keeps_reader_payload(self):
        """建了定位表之后，/chapter 必须仍给前端 html+tag（定位表只存文本）。"""
        from reader import locator
        before=(await self.client.get(self.base+'/chapter/0')).json()['paragraphs']
        locator.build(self.book,apply=True)
        self.assertTrue(locator.has_locator(self.book))
        r=await self.client.get(self.base+'/chapter/0');self.assertEqual(r.status_code,200,r.text)
        after=r.json()['paragraphs']
        self.assertEqual([p['text'] for p in after],[p['text'] for p in before])   # 切段不变
        self.assertTrue(all(p['html'].strip() and p['tag'] for p in after))
        self.assertTrue(all(p['html'] for p in before))
        self.assertTrue((await self.client.get(self.base+'/outline')).json()['items'])
        self.assertEqual((await self.client.get(self.base+'/notes')).status_code,200)

    async def test_notes_conflict_and_legacy(self):
        db=book_db(self.book);db.execute("INSERT INTO book_layer(book_id,content) VALUES(1,'旧笔记')");db.commit()
        old=(await self.client.post(self.base+'/note/read',json={'layer':'L1'})).json();self.assertEqual(old['content'],'旧笔记')
        body={'layer':'L1','content':'## 手工内容','baseline':baseline(self.book,'L1',{})}
        r=await self.client.put(self.base+'/note',json=body);self.assertEqual(r.status_code,200,r.text)
        r=await self.client.put(self.base+'/note',json={**body,'content':'并发内容'});self.assertEqual(r.status_code,409,r.text)
        r=await self.client.post(self.base+'/note/export',json={'layer':'L1'});self.assertEqual(r.text,'## 手工内容')
        self.assertEqual(db.execute('SELECT content FROM book_layer').fetchone()[0],'旧笔记')
        r=await self.client.post('/api/layer/concept/'+self.book,json={'term':'关系'});self.assertEqual(r.status_code,422)
        self.assertEqual((await self.client.get('/api/layer/concept/'+self.book+'?term=不存在')).json()['content'],'')

    async def test_legacy_layer_crud_lists_and_paragraph_separation(self):
        layers='/api/layer'
        self.assertFalse((await self.client.get(layers+'/book/'+self.book)).json()['matched'])
        self.assertEqual((await self.client.post(layers+'/book/'+self.book,json={'content':'## 全书旧格式'})).status_code,200)
        book=(await self.client.get(layers+'/book/'+self.book)).json();self.assertTrue(book['matched']);self.assertEqual(book['content'],'## 全书旧格式')

        chapters=(await self.client.get(layers+'/chapter_list/'+self.book)).json()['items']
        self.assertTrue(chapters);self.assertFalse(chapters[0]['has_content'])
        self.assertFalse((await self.client.get(layers+'/chapter/'+self.book+'/0')).json()['matched'])
        self.assertEqual((await self.client.post(layers+'/chapter/'+self.book+'/0',json={'content':'## 章节旧格式'})).status_code,200)
        chapter=(await self.client.get(layers+'/chapter/'+self.book+'/0')).json();self.assertTrue(chapter['matched']);self.assertEqual(chapter['content'],'## 章节旧格式')
        self.assertTrue((await self.client.get(layers+'/chapter_list/'+self.book)).json()['items'][0]['has_content'])
        self.assertEqual((await self.client.get(layers+'/chapter/'+self.book+'/99')).status_code,404)

        text='  一段有意义的原文  '
        body={'text':text,'content':'划线语义','source_text':'一段有意义的原文','anchor_text':'段落前缀'}
        self.assertEqual((await self.client.post(layers+'/semantic/'+self.book,json=body)).status_code,200)
        self.assertEqual((await self.client.get(layers+'/semantic/'+self.book,params={'text':'一段有意义的原文'})).json()['content'],'划线语义')
        self.assertEqual((await self.client.get(layers+'/semantic_list/'+self.book)).json()['items'][0]['source_text'],'一段有意义的原文')
        self.assertEqual((await self.client.post(layers+'/semantic/'+self.book,json={**body,'content':'更新语义'})).status_code,200)
        self.assertEqual((await self.client.get(layers+'/semantic/'+self.book,params={'text':text})).json()['content'],'更新语义')
        self.assertTrue((await self.client.delete(layers+'/semantic/'+self.book,params={'text':text})).json()['deleted'])
        self.assertFalse((await self.client.get(layers+'/semantic/'+self.book,params={'text':text})).json()['matched'])

        concept={'term':'社会关系','content':'概念解释','source_text':'社会关系','anchor_text':'段落前缀'}
        self.assertEqual((await self.client.post(layers+'/concept/'+self.book,json=concept)).status_code,200)
        self.assertEqual((await self.client.get(layers+'/concept/'+self.book,params={'term':'社会关系'})).json()['content'],'概念解释')
        self.assertEqual((await self.client.get(layers+'/concept_list/'+self.book)).json()['items'][0]['term'],'社会关系')
        self.assertEqual((await self.client.post(layers+'/concept/'+self.book,json={**concept,'content':'更新概念'})).status_code,200)
        self.assertEqual((await self.client.get(layers+'/concept/'+self.book,params={'term':'社会关系'})).json()['content'],'更新概念')
        self.assertTrue((await self.client.delete(layers+'/concept/'+self.book,params={'term':'社会关系'})).json()['deleted'])
        self.assertFalse((await self.client.get(layers+'/concept/'+self.book,params={'term':'社会关系'})).json()['matched'])

        para={'text':SOURCE,'content':'简要解释','detail':'逐段解释','sentences':['第一句解释','第二句解释']}
        self.assertEqual((await self.client.post(layers+'/para_semantic/'+self.book,json=para)).status_code,200)
        paragraph=(await self.client.get(layers+'/para_semantic/'+self.book,params={'chapter':0})).json()['items']
        self.assertTrue(any(x['source_text']==SOURCE and x['sentences']==para['sentences'] for x in paragraph))
        self.assertEqual((await self.client.get(layers+'/para_semantic/'+self.book)).status_code,200)
        self.assertTrue((await self.client.get(self.base+'/notes')).json()['items'])
        self.assertEqual((await self.client.delete(layers+'/para_semantic/'+self.book,params={'text':SOURCE})).json()['deleted'],1)
        self.assertFalse(any(x['source_text']==SOURCE for x in (await self.client.get(layers+'/para_semantic/'+self.book,params={'chapter':0})).json()['items']))

        await self.client.post(layers+'/semantic/'+self.book,json={'text':'这条划线不应随着段落批量删除','content':'保留的选区解释'})
        await self.client.post(layers+'/para_semantic/'+self.book,json=para)
        cleared=(await self.client.delete(layers+'/para_semantic/'+self.book)).json();self.assertGreaterEqual(cleared['deleted'],1)
        self.assertTrue(any(x['content']=='保留的选区解释' for x in (await self.client.get(layers+'/semantic_list/'+self.book)).json()['items']))
        self.assertFalse((await self.client.get(layers+'/para_semantic/'+self.book,params={'chapter':0})).json()['items'])

    async def test_book_delete_cancels_active_work_and_removes_only_that_book(self):
        r=await self.client.post('/upload',files={'file':('待删除.txt',TEXT.encode(),'text/plain')});self.assertEqual(r.status_code,200,r.text)
        folder=r.json()['folder'];path=Path(os.environ['GNOSYS_BOOKS_DIR'])/folder
        self.model.delay=.2
        run=await self.client.post('/api/agent/'+folder+'/runs',json={'layer':'L2','target':{'chapter_idx':0}})
        self.assertEqual(run.status_code,200,run.text)
        deleted=await self.client.delete('/api/books/'+folder);self.assertEqual(deleted.status_code,200,deleted.text)
        self.assertFalse(path.exists())
        self.assertTrue(Path(os.environ['GNOSYS_BOOKS_DIR'],self.book).exists())
        self.assertEqual((await self.client.delete('/api/books/'+folder)).status_code,404)
        self.assertEqual((await self.client.delete('/api/books/not-a-book')).status_code,400)
        self.assertEqual((await self.client.get('/notes/'+folder)).status_code,404)

    async def test_four_layers_actual_sdk_and_events(self):
        for layer,target in [('L1',{}),('L2',{'chapter_idx':0}),('L4',{'chapter_idx':0,'paragraph_index':3,'term':'社会关系'})]:
            spec='hegel_helin' if layer=='L2' else 'general'
            r=await self.client.post(self.runs,json={'layer':layer,'target':target,'spec_type':spec});self.assertEqual(r.status_code,200,r.text)
            result=await self.wait_run(r.json()['id']);self.assertEqual(result['status'],'completed',result)
            self.assertEqual(result['request']['spec_type'],spec)
            note=get_note(self.book,layer,target);self.assertTrue(note['content']);self.assertIn('#source=',note['content'])
            events=await self.client.get(self.runs+'/'+result['id']+'/events');self.assertIn('event: end',events.text)
            if layer=='L1':
                covered={c['chapter_idx'] for c in result['coverage']};self.assertEqual(covered,set(range(len([n for n in outline_nodes(self.book) if n['parent_id'] is None]))))
                self.assertEqual(sum(x['chars'] for x in result['coverage']),sum(len(p['text']) for ci in covered for p in logical_paragraphs(self.book,ci)[1]))
            if layer=='L4':
                maps=self.model.map_calls
                other=next(p for p in logical_paragraphs(self.book,1)[1] if '社会关系' in p['text'])
                target={'chapter_idx':1,'paragraph_index':other['paragraph_index'],'term':'社会关系'}
                r=await self.client.post(self.runs,json={'layer':'L4','target':target});self.assertEqual(r.status_code,200,r.text)
                cached=await self.wait_run(r.json()['id']);self.assertEqual(cached['status'],'completed',cached)
                self.assertEqual(self.model.map_calls,maps,'同一术语在第二个位置应复用已核验意义地图')
        r=await self.client.post(self.runs,json={'paragraph_ref':{'chapter_idx':0,'paragraph_index':3},'spec_type':'general'});self.assertEqual(r.status_code,200,r.text)
        result=await self.wait_run(r.json()['id']);self.assertEqual(result['status'],'completed',result)
        notes=(await self.client.get('/api/layer/para_semantic/'+self.book+'?chapter=0')).json()['items'];self.assertTrue(notes);self.assertEqual(len(notes[0]['sentences']),len(split_sentences(SOURCE)))
        self.assertTrue(any('chapter_structure' in str(x) for x in self.model.inputs))

    async def test_review_revise_pending_adopt_and_conflict(self):
        self.model.approve=False
        r=await self.client.post(self.runs,json={'layer':'L2','target':{'chapter_idx':0}});result=await self.wait_run(r.json()['id']);self.assertEqual(result['status'],'needs_review',result);self.assertEqual(self.model.reviews,2)
        pending=(await self.client.get(self.runs+'/pending')).json();self.assertTrue(pending['drafts'])
        draft=pending['drafts'][0];r=await self.client.post(self.runs+'/'+result['id']+'/adopt',json={'draft_id':draft['id']});self.assertEqual(r.status_code,200,r.text)
        self.model.approve=True
        async def edit():
            b=baseline(self.book,'L1',{});save_note(self.book,'L1',{},'用户在生成期间编辑',b)
        self.model.before_review=edit
        r=await self.client.post(self.runs,json={'layer':'L1'});result=await self.wait_run(r.json()['id']);self.assertEqual(result['status'],'needs_review',result)
        self.assertEqual(get_note(self.book,'L1',{})['content'],'用户在生成期间编辑');self.assertEqual(result['draft']['state'],'conflict')
        r=await self.client.post(self.runs+'/'+result['id']+'/adopt',json={'draft_id':result['draft']['id']});self.assertEqual(r.status_code,409)

    async def test_cancel_timeout_failure_and_retry(self):
        self.model.delay=.2
        r=await self.client.post(self.runs,json={'layer':'L2','target':{'chapter_idx':0}});ident=r.json()['id']
        await self.client.post(self.runs+'/'+ident+'/cancel');result=await self.wait_run(ident);self.assertEqual(result['status'],'canceled',result)
        self.model.delay=0;self.model.fail=True
        r=await self.client.post(self.runs+'/'+ident+'/retry');result=await self.wait_run(r.json()['id']);self.assertEqual(result['status'],'failed',result)
        self.model.fail=False
        r=await self.client.post(self.runs+'/'+result['id']+'/retry');result=await self.wait_run(r.json()['id']);self.assertEqual(result['status'],'completed',result)
        settings_db.save({'AGENT_RUN_TIMEOUT':'0.005'})
        try:
            self.model.delay=.1;r=await self.client.post(self.runs,json={'layer':'L1'});result=await self.wait_run(r.json()['id']);self.assertEqual(result['status'],'failed',result)
        finally:
            settings_db.save({'AGENT_RUN_TIMEOUT':'300'})

    async def test_chat_stream_history_sources_idempotency_and_export(self):
        sid=(await self.client.post(self.chat+'/sessions',json={})).json()['id']
        p=get_paragraph(self.book,0,3);refs=[{'chapter_idx':0,'paragraph_index':3,'quote':SOURCE[:12],'fingerprint':p['fingerprint']},{'node_id':'c0:p1'}]
        r=await self.send(sid,refs=refs);self.assertEqual(r.status_code,200,r.text);mid=r.json()['message_id']
        duplicate=await self.send(sid,refs=refs);self.assertEqual(duplicate.json()['message_id'],mid)
        data=await self.wait_chat(sid);self.assertEqual(data['messages'][-1]['status'],'completed',data)
        self.assertEqual([m['role'] for m in data['messages']],['user','assistant'])
        self.assertIn('公式',data['messages'][-1]['content']);self.assertTrue(data['messages'][-1]['references'])
        assistant=next(m for m in data['messages'] if m['id']==mid)
        self.assertIn('先核对章节范围',assistant['trace']['reasoning'])
        self.assertTrue(any(t['tool']=='read_directory_node' and t['status']=='ok' for t in assistant['trace']['tools']))
        stream=await self.client.get(self.chat+f'/sessions/{sid}/messages/{mid}/events');self.assertIn('delta',stream.text);self.assertIn('read_directory_node',stream.text)
        cursor=data['messages'][-1]['event_cursor'];resume=await self.client.get(self.chat+f'/sessions/{sid}/messages/{mid}/events',headers={'Last-Event-ID':str(cursor)});self.assertNotIn('event: update',resume.text)
        r=await self.send(sid,'继续比较',req='request-2');self.assertEqual(r.status_code,200,r.text);await self.wait_chat(sid)
        from agent.chat import sdk_session
        session=sdk_session(self.book,sid)
        self.assertGreater(len(await session.get_items()),4);session.close()
        r=await self.client.get(self.chat+'/sessions/'+sid+'/export');self.assertIn('## Agent',r.text);self.assertIn('社会关系',r.text)
        await self.client.patch(self.chat+'/sessions/'+sid,json={'title':'新的名字'});self.assertEqual((await self.client.get(self.chat+'/sessions')).json()['items'][0]['title'],'新的名字')
        await self.client.delete(self.chat+'/sessions/'+sid);self.assertEqual((await self.client.get(self.chat+'/sessions/'+sid)).status_code,404)

    async def test_chat_order_survives_identical_timestamps(self):
        sid=(await self.client.post(self.chat+'/sessions',json={})).json()['id']
        db=book_db(self.book);ts='2026-01-01T00:00:00.000000+00:00'
        with db:
            db.execute("INSERT INTO chat_messages(id,session_id,role,content,references_json,status,request_id,created_at) VALUES(?,?,?,?,?,?,?,?)",
                       ('zzz-user',sid,'user','问','[]','completed','r1',ts))
            db.execute("INSERT INTO chat_messages(id,session_id,role,content,references_json,status,request_id,created_at) VALUES(?,?,?,?,?,?,?,?)",
                       ('aaa-assistant',sid,'assistant','答','[]','completed','r2',ts))
        data=(await self.client.get(self.chat+'/sessions/'+sid)).json()
        self.assertEqual([m['role'] for m in data['messages']],['user','assistant'])
        self.assertEqual([m['id'] for m in data['messages']],['zzz-user','aaa-assistant'])

    async def test_chat_stop_busy_failure_retry(self):
        self.model.delay=.1
        sid=(await self.client.post(self.chat+'/sessions',json={})).json()['id'];r=await self.send(sid);mid=r.json()['message_id']
        r=await self.send(sid,req='other');self.assertEqual(r.status_code,409)
        for _ in range(500):
            data=(await self.client.get(self.chat+'/sessions/'+sid)).json()
            partial=next(m for m in data['messages'] if m['id']==mid)
            if partial['content'] or partial['status'] not in ('queued','running'):break
            await asyncio.sleep(.01)
        self.assertTrue(partial['content'],partial)
        await self.client.post(self.chat+f'/sessions/{sid}/messages/{mid}/cancel');data=await self.wait_chat(sid);self.assertEqual(data['messages'][-1]['status'],'canceled',data);self.assertTrue(data['messages'][-1]['content'])
        self.model.delay=0
        r=await self.client.post(self.chat+f'/sessions/{sid}/messages/{mid}/retry',json={'request_id':'retry-1'});self.assertEqual(r.status_code,200,r.text);data=await self.wait_chat(sid);self.assertEqual(data['messages'][-1]['status'],'completed',data)
        self.model.fail=True;r=await self.send(sid,req='failing');self.assertEqual(r.status_code,200);data=await self.wait_chat(sid);self.assertEqual(data['messages'][-1]['status'],'failed',data)

    async def test_chat_tool_budget_exhaustion_still_answers(self):
        from agent.runtime.config import AgentConfig
        cfg = AgentConfig(base_url='http://offline', model='fake', api_key='offline-test-key',
                          request_timeout=5, max_requests=8, max_tool_calls=0,
                          run_timeout=30, max_parallel=1)
        sid = (await self.client.post(self.chat + '/sessions', json={})).json()['id']
        with patch('agent.chat.load_config', return_value=cfg):
            r = await self.send(sid, req='budget-0')
            self.assertEqual(r.status_code, 200, r.text)
            mid = r.json()['message_id']
            for _ in range(500):
                data = (await self.client.get(self.chat + '/sessions/' + sid)).json()
                msg = next(x for x in data['messages'] if x['id'] == mid)
                if msg['status'] not in ('queued', 'running'):
                    break
                await asyncio.sleep(.01)
            else:
                self.fail('chat timeout')
        self.assertEqual(msg['status'], 'completed', data)
        self.assertIn('阅读分析', msg['content'])
        self.assertTrue(msg['trace']['tools'])
        self.assertEqual(msg['trace']['tools'][0]['status'], 'blocked')
        events = (await self.client.get(self.chat + f'/sessions/{sid}/messages/{mid}/events')).text
        self.assertIn('工具预算已用完', events)

    async def test_restart_recovery_and_source_validation(self):
        db=book_db(self.book)
        sid=(await self.client.post(self.chat+'/sessions',json={})).json()['id']
        db.execute("INSERT INTO chat_messages(id,session_id,role,content,status,created_at) VALUES('interrupted',?,'assistant','partial','running','now')",(sid,));db.commit()
        from agent.chat import recover
        recover(self.book);row=db.execute("SELECT * FROM chat_messages WHERE id='interrupted'").fetchone();self.assertEqual(row['content'],'partial');self.assertEqual(row['status'],'interrupted')
        from agent.layers.policy import validate_result
        with self.assertRaises(ValueError):validate_result(self.book,{'content':'test','sources':[{'chapter_idx':1,'paragraph_index':0,'quote':SOURCE}]},{(0,3):[SOURCE]})
        r=await self.client.get('/read/'+self.book+'/0');self.assertEqual(r.status_code,200,r.text)
        r=await self.client.get('/notes/'+self.book);self.assertEqual(r.status_code,200,r.text)
        r=await self.client.get('/');self.assertEqual(r.status_code,200,r.text)

    async def test_long_book_complete_coverage_and_unheaded_context(self):
        long_text = ('社会关系在实践中不断变化。' * 800) + '\n' + ('交往形成生活秩序。' * 800)
        r = await self.client.post('/upload', files={'file': ('长文本.txt', long_text.encode())})
        book = r.json()['folder']
        from agent.tools.context import section_context
        context = section_context(book, 0, 0)
        self.assertFalse(context['is_heading']); self.assertGreater(context['section_size'], 0)
        r = await self.client.post('/api/agent/' + book + '/runs', json={'layer': 'L1'})
        ident = r.json()['id']
        for _ in range(500):
            result = (await self.client.get('/api/agent/' + book + '/runs/' + ident)).json()
            if result['status'] not in ('queued', 'running'): break
            await asyncio.sleep(.01)
        self.assertEqual(result['status'], 'completed', result)
        self.assertGreater(len(result['coverage']), 2)
        self.assertEqual(sum(x['chars'] for x in result['coverage']), len(long_text.replace('\n', '')))

    async def test_candidate_retained_on_cancel_and_semantic_manual_conflict(self):
        async def wait_review():
            await asyncio.sleep(.2)
        self.model.before_review = wait_review
        r = await self.client.post(self.runs, json={'layer':'L2','target':{'chapter_idx':0}})
        ident = r.json()['id']
        for _ in range(100):
            data = (await self.client.get(self.runs + '/' + ident)).json()
            if data['candidate']: break
            await asyncio.sleep(.01)
        await self.client.post(self.runs + '/' + ident + '/cancel')
        data = await self.wait_run(ident)
        self.assertEqual(data['status'], 'canceled'); self.assertTrue(data['draft'])
        async def manual_edit():
            target={'chapter_idx':0,'paragraph_index':3}
            save_note(self.book,'L3',target,'生成期间的手工解释',baseline(self.book,'L3',target))
        self.model.before_review=manual_edit
        r=await self.client.post(self.runs,json={'paragraph_ref':{'chapter_idx':0,'paragraph_index':3},'mode':'overwrite'})
        data=await self.wait_run(r.json()['id'])
        self.assertEqual(data['status'],'needs_review',data)
        self.assertEqual(get_note(self.book,'L3',{'chapter_idx':0,'paragraph_index':3})['content'],'生成期间的手工解释')
        self.model.before_review=None
        r=await self.client.post(self.runs,json={'paragraph_ref':{'chapter_idx':0,'paragraph_index':3},'mode':'overwrite'})
        data=await self.wait_run(r.json()['id']);self.assertEqual(data['status'],'completed',data)
        self.assertNotEqual(get_note(self.book,'L3',{'chapter_idx':0,'paragraph_index':3})['content'],'生成期间的手工解释')

    async def test_interrupted_layer_candidate_adopt_and_input_boundaries(self):
        db=book_db(self.book)
        from reader.workspace import dumps, now
        target={'chapter_idx':0}; request={'layer':'L2','target':target,'note_key':'L2:0'}
        p=get_paragraph(self.book,0,3)
        candidate={'content':'服务中断前的稿件','sources':[{'chapter_idx':0,'paragraph_index':3,'quote':p['text'][:12],'fingerprint':p['fingerprint']}]}
        with db:
            db.execute('INSERT INTO agent_runs VALUES(?,?,?,?,?,?,?,?)',('restart-run',1,'running',dumps(request),'offline',now(),now(),None))
            db.execute('INSERT INTO layer_results(run_id,candidate_json,baseline_json) VALUES(?,?,?)',('restart-run',dumps(candidate),dumps(baseline(self.book,'L2',target))))
        from api.server import recover_agent_tasks
        await recover_agent_tasks()
        data=(await self.client.get(self.runs+'/restart-run')).json();self.assertEqual(data['status'],'interrupted');self.assertTrue(data['draft'])
        r=await self.client.post(self.runs+'/restart-run/adopt',json={'draft_id':data['draft']['id']});self.assertEqual(r.status_code,200,r.text)
        for body in [{'layer':'unknown'}, {'layer':'L2','target':{'chapter_idx':-1}}, {'layer':'L4','target':{'chapter_idx':0,'paragraph_index':3,'term':'原文不存在'}}]:
            r=await self.client.post(self.runs,json=body);self.assertIn(r.status_code,(404,409,422),r.text)
        sid=(await self.client.post(self.chat+'/sessions',json={})).json()['id']
        r=await self.send(sid,refs=[{'node_id':'missing'}]);self.assertEqual(r.status_code,409)
        settings_db.save({'AGENT_API_KEY':''})
        try:
            r=await self.send(sid);self.assertEqual(r.status_code,422)
        finally:
            settings_db.save({'AGENT_API_KEY':'offline-test-key'})
        r=await self.client.get('/api/workspace/..%5Cother_data/outline');self.assertEqual(r.status_code,404)


class ProcessRecoveryTest(unittest.IsolatedAsyncioTestCase):
    async def test_chat_survives_real_server_restart(self):
        with tempfile.TemporaryDirectory(prefix='gnosys-process-restart-') as root:
            books = Path(root) / 'books'; books.mkdir()
            source = Path(root) / 'restart.txt'; source.write_text(TEXT, encoding='utf-8')
            folder = Path(import_document(source, books)).name
            with socket.socket() as probe:
                probe.bind(('127.0.0.1', 0)); port = probe.getsockname()[1]
            env = {**os.environ, 'GNOSYS_BOOKS_DIR': str(books)}
            helper = ROOT / 'tests' / 'restart_server.py'
            process = None

            async def start():
                child = await asyncio.create_subprocess_exec(sys.executable, str(helper), str(books), str(port), str(settings_db.SETTINGS_DB),
                    cwd=ROOT, env=env, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
                async with AsyncClient(timeout=2) as client:
                    for _ in range(100):
                        if child.returncode is not None:
                            self.fail('restart helper exited before serving')
                        try:
                            response = await client.get(f'http://127.0.0.1:{port}/')
                            if response.status_code == 200:
                                return child
                        except Exception:
                            pass
                        await asyncio.sleep(.05)
                child.kill(); await child.wait(); self.fail('restart helper did not become ready')

            try:
                process = await start()
                async with AsyncClient(base_url=f'http://127.0.0.1:{port}', timeout=3) as client:
                    chat_url = f'/api/chat/{folder}'
                    sid = (await client.post(chat_url + '/sessions', json={})).json()['id']
                    receipt = await client.post(chat_url + f'/sessions/{sid}/messages', json={'content':'测试停止后恢复','references':[],'request_id':'restart-test'})
                    self.assertEqual(receipt.status_code, 200, receipt.text); message_id = receipt.json()['message_id']
                    partial = None
                    for _ in range(200):
                        data = (await client.get(chat_url + f'/sessions/{sid}')).json()
                        partial = next(m for m in data['messages'] if m['id'] == message_id)
                        if len(partial['content']) >= 13: break
                        await asyncio.sleep(.03)
                    self.assertGreaterEqual(len(partial['content']), 13, partial)
                    process.kill(); await process.wait(); process = None
                    process = await start()
                    data = (await client.get(chat_url + f'/sessions/{sid}')).json()
                    recovered = next(m for m in data['messages'] if m['id'] == message_id)
                    self.assertEqual(recovered['status'], 'interrupted', recovered)
                    self.assertEqual(recovered['content'], partial['content'])
                    retry = await client.post(chat_url + f'/sessions/{sid}/messages/{message_id}/retry', json={'request_id':'restart-retry'})
                    self.assertEqual(retry.status_code, 200, retry.text)
                    for _ in range(400):
                        data = (await client.get(chat_url + f'/sessions/{sid}')).json()
                        latest = data['messages'][-1]
                        if latest['status'] not in ('queued','running'): break
                        await asyncio.sleep(.03)
                    self.assertEqual(latest['status'], 'completed', latest)
                    self.assertIn('公式', latest['content'])
            finally:
                if process is not None and process.returncode is None:
                    process.terminate()
                    with suppress(ProcessLookupError): await asyncio.wait_for(process.wait(), 5)

class MigrationTest(unittest.TestCase):
    def test_backup_wal_idempotent_and_rollback(self):
        with tempfile.TemporaryDirectory() as d:
            path=str(Path(d)/'book.db');conn=sqlite3.connect(path);conn.execute('PRAGMA journal_mode=WAL');conn.execute('CREATE TABLE books(id INTEGER PRIMARY KEY,title TEXT,language TEXT,authors TEXT,source_file TEXT,processed_at TEXT,total_chaps INTEGER,total_paras INTEGER,toc_json TEXT)');conn.execute("INSERT INTO books VALUES(1,'旧书','zh','[]','','',0,0,'[]')");conn.commit()
            db=get_db(path);backup=path+'.before-workspace-v1.bak';self.assertTrue(Path(backup).is_file())
            with sqlite3.connect(backup) as old:self.assertEqual(old.execute('SELECT title FROM books').fetchone()[0],'旧书');self.assertFalse(old.execute("SELECT 1 FROM sqlite_master WHERE name='workspace_notes'").fetchone())
            self.assertEqual(db.execute('SELECT COUNT(*) FROM workspace_migrations').fetchone()[0],1);close_db(path);db=get_db(path);self.assertEqual(db.execute('SELECT COUNT(*) FROM workspace_migrations').fetchone()[0],1);close_db(path);conn.close()
            from db import workspace_schema
            conn=sqlite3.connect(':memory:')
            with patch.object(workspace_schema,'DDL','CREATE TABLE first_table(id INTEGER); BAD SQL;'):
                with self.assertRaises(sqlite3.Error):workspace_schema.migrate(conn)
            self.assertFalse(conn.execute("SELECT 1 FROM sqlite_master WHERE name='first_table'").fetchone());conn.close()


if __name__=='__main__':unittest.main(verbosity=2)
