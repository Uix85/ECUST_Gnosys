"""Deterministic model adapter: exercises the real SDK loop, tools and sessions offline."""
import asyncio
import json
import uuid
from agents.models.interface import Model
from agents.items import ModelResponse
from agents.usage import Usage
from openai.types.responses import ResponseFunctionToolCall, ResponseOutputMessage, ResponseOutputText, ResponseTextDeltaEvent, ResponseReasoningSummaryTextDeltaEvent, ResponseCompletedEvent, Response


def call(name, args):
    return ResponseFunctionToolCall(id='fc_' + uuid.uuid4().hex, call_id=uuid.uuid4().hex, name=name, arguments=json.dumps(args, ensure_ascii=False), type='function_call', status='completed')


class ReadingModel(Model):
    def __init__(self, *, approve=True, delay=0, fail=False, before_review=None):
        self.approve, self.delay, self.fail, self.before_review = approve, delay, fail, before_review
        self.inputs = []
        self.reviews = 0
        self.map_calls = 0

    async def get_response(self, system_instructions, input, model_settings, tools, *args, **kwargs):
        await asyncio.sleep(self.delay)
        if self.fail:
            raise RuntimeError('offline provider failure')
        self.inputs.append(input)
        names = {t.name for t in tools}
        items = [{'role':'user','content':input}] if isinstance(input,str) else input
        materials = None
        for item in reversed(items):
            if isinstance(item, dict) and item.get('role') == 'user':
                try:
                    materials = json.loads(item['content'])
                    break
                except (TypeError, json.JSONDecodeError):
                    pass
        materials = materials or {}
        if 'submit_review' in names:
            self.reviews += 1
            if self.before_review:
                await self.before_review()
            name, value = 'submit_review', {'approved':self.approve,'issues':[] if self.approve else ['请核对论证范围'],'alternatives':[]}
        elif 'submit_concept_map' in names:
            self.map_calls += 1
            position = materials['position']; ci, pi = position['chapter_idx'], position['paragraph_index']
            # Call a real read tool before submitting: its result must update the validator ledger.
            if not any(isinstance(x,dict) and x.get('type')=='function_call_output' for x in items):
                if 'read_book_paragraph' not in names:
                    return ModelResponse([call('load_skill',{'name':'concept-mapping'})], Usage(), None)
                return ModelResponse([call('read_book_paragraph',{'chapter_idx':ci,'paragraph_index':pi})], Usage(), None)
            from agent.book import get_paragraph
            text = get_paragraph(BOOK_ID,ci,pi)['text']
            value = {'has_definition':'inferred','definition_note':'从本章使用中归纳','confusion_types':['idiom_shift'],
                     'senses':[{'reading':'实践中形成的社会联系','common_reading':'人与事物之间的联系','scope':'chapter','range_note':'本章',
                                'evidence':[{'chapter_idx':ci,'paragraph_index':pi,'quote':text[:12]}], 'evolves_to_next':False,'distinction_note':'','dimensions':[],'emphasis_here':{}}], 'quotes':[]}
            name = 'submit_concept_map'
        elif 'submit_concept_reading' in names:
            name = 'submit_concept_reading'
            value = {'reading':'这里指实践中形成的社会联系','common_reading':'一般的联系','emphasis_here':{},'stance':'normal','quotes':[],'needs_mapping':False}
        else:
            name = 'submit_result'
            if 'short' in next(t for t in tools if t.name==name).params_json_schema['properties']:
                from agent.book import split_sentences
                source = materials.get('target',{}).get('text',SOURCE)
                value = {'short':'社会关系在实践中形成。','detail':'这段文字说明交往与实践相互联系，并由此形成生活秩序。','sentences':['说明实践的作用。']*len(split_sentences(source))}
            else:
                if 'source' in materials:
                    p=materials['source'][0];ci=materials['chapter_idx']; pi=p['paragraph_index']; text=p['text']
                elif 'covered_chunks' in materials:
                    ref=materials['covered_chunks'][0]['result']['sources'][0]; ci,pi,text=ref['chapter_idx'],ref['paragraph_index'],ref['quote']
                elif 'candidate' in materials:
                    ref=materials['candidate']['sources'][0];ci,pi,text=ref['chapter_idx'],ref['paragraph_index'],ref['quote']
                else:
                    ci,pi,text=0,3,SOURCE
                value={'content':'## 核心问题\n\n社会关系怎样在实践中形成？\n\n## 思想地图\n\n实践 → 交往 → 秩序。\n\n## 论证主线\n\n从经验观察到生活秩序。\n\n## 章节关系\n\n第二章继承第一章。\n\n## 章级总纲\n\n解释社会关系与实践。\n\n## 论证区块\n\n从观察到交往，见[原文](#source=0:3)。\n\n## 关键转折\n\n转向生活秩序。\n\n## 承上启下\n\n为后续讨论奠定基础。','sources':[{'chapter_idx':ci,'paragraph_index':pi,'quote':text[:12]}]}
        return ModelResponse([call(name,value)], Usage(requests=1,input_tokens=20,output_tokens=20,total_tokens=40), None)

    async def stream_response(self, system_instructions, input, model_settings, tools, *args, **kwargs):
        self.inputs.append(input)
        if self.fail:
            raise RuntimeError('offline streaming failure')
        # First streamed turn invokes an actual directory tool, subsequent turn emits text.
        if not any(isinstance(x,dict) and x.get('type')=='function_call_output' for x in input):
            yield ResponseReasoningSummaryTextDeltaEvent(
                type='response.reasoning_summary_text.delta',
                delta='先核对章节范围和原文位置。',
                item_id='rs_test', output_index=0, summary_index=0,
                sequence_number=0,
            )
            output=[call('read_directory_node',{'node_id':'c0','offset':0})]
            response=Response.model_construct(id='resp_'+uuid.uuid4().hex, output=output, usage=None)
            yield ResponseCompletedEvent(type='response.completed',response=response,sequence_number=1)
            return
        text='## 阅读分析\n\n社会关系与实践有关。见[原文](#source=0:3)。\n\n| 概念 | 说明 |\n| --- | --- |\n| 关系 | 联系 |\n\n- [x] 已读原文\n  - 子项\n\n```python\nx = 1\n```\n\n公式：$x^2 + y^2$。'
        for i in range(0,len(text),13):
            await asyncio.sleep(.15 if '测试停止' in str(input) else self.delay or .002)
            yield ResponseTextDeltaEvent(type='response.output_text.delta',delta=text[i:i+13],content_index=0,item_id='msg_1',output_index=0,logprobs=[],sequence_number=i+1)
        output=[ResponseOutputMessage(id='msg_'+uuid.uuid4().hex,role='assistant',status='completed',type='message',content=[ResponseOutputText(type='output_text',text=text,annotations=[],logprobs=[])])]
        response=Response.model_construct(id='resp_'+uuid.uuid4().hex, output=output, usage=None)
        yield ResponseCompletedEvent(type='response.completed',response=response,sequence_number=len(text)+2)


BOOK_ID = ''
SOURCE = '社会关系构成了研究的基础。我们从实践出发理解人与人的交往。'
