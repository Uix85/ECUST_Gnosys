"""启动前释放固定端口：定位监听进程并结束它（Windows / POSIX）。"""

import os
import signal
import socket
import subprocess
import sys
import time


HOST = "127.0.0.1"
PORT = 8123


def _windows_listening_pids(output: str, port: int) -> list[int]:
    """从 netstat -ano 输出里提取监听指定端口的 PID（仅 TCP LISTEN）。"""
    pids = []
    for line in output.splitlines():
        parts = line.split()
        if len(parts) < 5 or parts[0].upper() != "TCP":
            continue
        if "LISTEN" not in parts[3].upper():
            continue
        if parts[1].rsplit(":", 1)[-1] != str(port):
            continue
        try:
            pid = int(parts[4])
        except ValueError:
            continue
        if pid > 0 and pid not in pids:
            pids.append(pid)
    return pids


def _listening_pids(port: int) -> list[int]:
    """返回监听指定 TCP 端口的进程 PID；定位失败时返回空列表。"""
    if os.name == "nt":
        try:
            result = subprocess.run(
                ["netstat", "-ano", "-p", "tcp"],
                capture_output=True, text=True, errors="replace", timeout=10, check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            return []
        return _windows_listening_pids(result.stdout, port)

    # POSIX：优先 lsof，其次 fuser（两者都可能没装）。
    for command in (
        ["lsof", "-nP", f"-tiTCP:{port}", "-sTCP:LISTEN"],
        ["fuser", "-n", "tcp", str(port)],
    ):
        try:
            result = subprocess.run(
                command, capture_output=True, text=True, errors="replace", timeout=10, check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            continue
        pids = []
        for token in result.stdout.split():
            try:
                pid = int(token)
            except ValueError:
                continue
            if pid > 0 and pid not in pids:
                pids.append(pid)
        if pids:
            return pids
    return []


def _bind_blocked(host: str, port: int) -> bool:
    """netstat/lsof 都定位不到时，用绑定探测兜底。"""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        try:
            sock.bind((host, port))
        except OSError:
            return True
    return False


def _terminate(pids: list[int], *, force: bool = False) -> None:
    for pid in pids:
        if pid in (0, 4, os.getpid()):
            continue
        if os.name == "nt":
            # /T 连同子进程一起结束；/F 强制。开发服务器不存在需要优雅退出的数据写入。
            try:
                subprocess.run(
                    ["taskkill", "/PID", str(pid), "/F", "/T"],
                    capture_output=True, text=True, errors="replace", timeout=10, check=False,
                )
            except (OSError, subprocess.TimeoutExpired):
                pass
            continue
        try:
            os.kill(pid, getattr(signal, "SIGKILL", signal.SIGTERM) if force else signal.SIGTERM)
        except OSError:
            pass


def ensure_port_free(host: str = HOST, port: int = PORT, timeout: float = 10.0) -> None:
    """端口被占用时结束监听进程；无法释放则退出并给出 PID。"""
    pids = _listening_pids(port)
    if not pids:
        if not _bind_blocked(host, port):
            return
        print(f"端口 {port} 已被占用，但无法定位占用进程；请手动结束后重试。", file=sys.stderr)
        raise SystemExit(1)
    print(f"端口 {port} 被 PID {', '.join(map(str, pids))} 占用，正在自动结束…")
    _terminate(pids)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not _listening_pids(port):
            print(f"端口 {port} 已释放")
            return
        time.sleep(0.25)
    remaining = [pid for pid in _listening_pids(port) if pid not in (0, 4, os.getpid())]
    if remaining:
        print(f"端口 {port} 仍被 PID {', '.join(map(str, remaining))} 占用，再次强制结束…")
        _terminate(remaining, force=True)
        deadline = time.monotonic() + 3.0
        while time.monotonic() < deadline:
            if not _listening_pids(port):
                print(f"端口 {port} 已释放")
                return
            time.sleep(0.25)
    print(f"无法释放端口 {port}；请手动结束占用进程后重试。", file=sys.stderr)
    raise SystemExit(1)
