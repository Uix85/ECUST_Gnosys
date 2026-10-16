"""项目根目录启动入口；应用由 backend/api/server.py 提供。"""

from pathlib import Path
import sys

BACKEND_DIR = Path(__file__).resolve().parent / "backend"
sys.path.insert(0, str(BACKEND_DIR))

from api.server import app  # noqa: E402


if __name__ == "__main__":
    import uvicorn

    print("Starting server at http://127.0.0.1:8123")
    uvicorn.run(app, host="127.0.0.1", port=8123)
