"""Start the application Server and Agent service as separate local processes."""

import os
import secrets
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import urlsplit


ROOT = Path(__file__).resolve().parent
agent_port = os.environ.get("GNOSYS_AGENT_PORT")
agent_url = os.environ.get("GNOSYS_AGENT_URL")
if not agent_port:
    agent_port = str(urlsplit(agent_url).port) if agent_url and urlsplit(agent_url).port else "8124"
agent_url = (agent_url or f"http://127.0.0.1:{agent_port}").rstrip("/")
server_port = os.environ.get("GNOSYS_SERVER_PORT", "8123")
token = os.environ.get("GNOSYS_AGENT_TOKEN") or secrets.token_urlsafe(32)
env = os.environ.copy()
env.update({
    "GNOSYS_AGENT_MODE": "http",
    "GNOSYS_AGENT_TOKEN": token,
    "GNOSYS_AGENT_URL": agent_url,
    "GNOSYS_AGENT_PORT": agent_port,
    "GNOSYS_SERVER_URL": os.environ.get("GNOSYS_SERVER_URL", f"http://127.0.0.1:{server_port}"),
})

processes = []
try:
    agent = subprocess.Popen([sys.executable, "agent_service.py"], cwd=ROOT, env=env)
    processes.append(agent)
    for _ in range(100):
        if agent.poll() is not None:
            raise RuntimeError("Agent service exited before becoming ready")
        try:
            with urllib.request.urlopen(agent_url + "/health", timeout=0.5) as response:
                if response.status == 200:
                    break
        except (OSError, urllib.error.URLError):
            time.sleep(0.1)
    else:
        raise RuntimeError(f"Agent service did not become ready at {agent_url}")

    server = subprocess.Popen([sys.executable, "server.py"], cwd=ROOT, env=env)
    processes.append(server)
    raise SystemExit(server.wait())
except KeyboardInterrupt:
    pass
finally:
    for process in reversed(processes):
        if process.poll() is None:
            process.terminate()
    for process in reversed(processes):
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
