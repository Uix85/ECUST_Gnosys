"""Compatibility entry point for the FastAPI application."""

from api.port_cleanup import HOST, PORT, ensure_port_free
from api.server import app


if __name__ == "__main__":
    import uvicorn

    ensure_port_free()
    print(f"Starting server at http://{HOST}:{PORT}")
    uvicorn.run(app, host=HOST, port=PORT)
