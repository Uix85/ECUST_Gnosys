"""Run the standalone model execution service."""

import os
import sys
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parent / "backend"
sys.path.insert(0, str(BACKEND_DIR))

import uvicorn
from agent.service import app


if __name__ == "__main__":
    uvicorn.run(app, host=os.environ.get("GNOSYS_AGENT_HOST", "127.0.0.1"),
                port=int(os.environ.get("GNOSYS_AGENT_PORT", "8124")))
