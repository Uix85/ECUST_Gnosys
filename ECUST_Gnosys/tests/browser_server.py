"""Local browser QA server with deterministic SDK model; only temporary books."""
import os
import sys
import tempfile
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT),str(ROOT/'backend'),str(ROOT/'tests')]
DATA=tempfile.TemporaryDirectory(prefix='gnosys-browser-')
os.environ['GNOSYS_BOOKS_DIR']=str(Path(DATA.name)/'books')
os.environ['AGENT_API_KEY']='offline-test-key'
from reader.import_document import import_document
from fake_model import ReadingModel,SOURCE
import fake_model
source=Path(DATA.name)/'社会关系与实践.txt'
source.write_text('第一章 社会关系\n第一节 实践\n一、观察\n'+SOURCE+'\n二、交往\n社会关系意味着实践中形成的联系。人们在交往中形成生活秩序。\n第二章 社会秩序\n第一节 实践\n社会关系在重复行动中逐渐稳定。我们由此观察秩序的形成。',encoding='utf-8')
folder=import_document(source,os.environ['GNOSYS_BOOKS_DIR']);fake_model.BOOK_ID=Path(folder).name
from server import app
from agent.runs import layer
from agent.layers import l3_paragraph
from agent import chat
model=ReadingModel(delay=float(os.environ.get('GNOSYS_QA_DELAY','.015')))
layer.build_model=lambda _:model
l3_paragraph.build_model=lambda _:model
chat.build_model=lambda _:model
from fastapi import Body
@app.post('/__qa/model')
async def configure_fake_model(body: dict = Body(...)):
 model.approve=body.get('approve',True)
 model.fail=body.get('fail',False)
 model.delay=body.get('delay',.015)
 return {'ok':True,'approve':model.approve}
print('QA_BOOK='+fake_model.BOOK_ID,flush=True)
if __name__=='__main__':
 import uvicorn
 uvicorn.run(app,host='127.0.0.1',port=8137)
