"""项目根目录启动入口；应用由 backend/api/server.py 提供。"""

from pathlib import Path
import sys

BACKEND_DIR = Path(__file__).resolve().parent / "backend"
sys.path.insert(0, str(BACKEND_DIR))

from api.port_cleanup import HOST, PORT, ensure_port_free  # noqa: E402
from api.server import app  # noqa: E402


if __name__ == "__main__":
    import uvicorn

    ensure_port_free()
    print(f"Starting server at http://{HOST}:{PORT}")
    uvicorn.run(app, host=HOST, port=PORT)
