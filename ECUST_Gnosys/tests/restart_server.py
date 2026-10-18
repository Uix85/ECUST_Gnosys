"""Disposable process used to verify persisted chat recovery across real restarts."""
import os
from pathlib import Path
import sys

root = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(root), str(root / 'backend'), str(root / 'tests')]
os.environ['GNOSYS_BOOKS_DIR'] = sys.argv[1]
os.environ['AGENT_API_KEY'] = 'offline-test-key'

from server import app
from agent.runs import chat
from fake_model import ReadingModel

chat.build_model = lambda _config: ReadingModel(delay=0.04)

if __name__ == '__main__':
    import uvicorn
    uvicorn.run(app, host='127.0.0.1', port=int(sys.argv[2]), log_level='error')
