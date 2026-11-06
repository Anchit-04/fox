import hmac
import json
import os
import socket
import subprocess
import sys
import threading
from typing import Callable

HELLO = "hello"
TOKEN_ENV = "FOX_WORKER_TOKEN"
ROOT_ENV = "FOX_WORKER_ROOT"
STILL_ACTIVE = 259


def detached() -> dict:
    if sys.platform == "win32":
        return {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS}
    return {"start_new_session": True}


def pid_alive(pid: int | None) -> bool:
    if not pid:
        return False
    if sys.platform == "win32":
        import ctypes
        from ctypes import wintypes
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.OpenProcess.restype = wintypes.HANDLE
        handle = kernel.OpenProcess(0x1000, False, pid)
        if not handle:
            return False
        try:
            code = wintypes.DWORD()
            if not kernel.GetExitCodeProcess(handle, ctypes.byref(code)):
                return False
            return code.value == STILL_ACTIVE
        finally:
            kernel.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def terminate(pid: int | None) -> None:
    if not pid_alive(pid):
        return
    if sys.platform == "win32":
        subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], stdout=subprocess.DEVNULL,
                       stderr=subprocess.DEVNULL)
    else:
        import signal
        os.kill(pid, signal.SIGKILL)


def encode(message: dict) -> bytes:
    return (json.dumps(message, ensure_ascii=False, default=str) + "\n").encode("utf-8")


class WorkerChannel:
    def __init__(self, token: str):
        self.token = token
        self.server = socket.create_server(("127.0.0.1", 0))
        self.port = self.server.getsockname()[1]
        self._lock = threading.Lock()
        self._conn: socket.socket | None = None
        self._on_line: Callable[[str], None] | None = None
        self._hello: Callable[[], dict] | None = None

    @property
    def connected(self) -> bool:
        return self._conn is not None

    def start(self, on_line: Callable[[str], None], hello: Callable[[], dict]) -> None:
        self._on_line = on_line
        self._hello = hello
        threading.Thread(target=self._accept, name="fox-channel", daemon=True).start()

    def send(self, message: dict) -> None:
        self.send_raw(encode(message))

    def send_raw(self, data: bytes) -> None:
        with self._lock:
            conn = self._conn
            if conn is None:
                return
            try:
                conn.sendall(data)
            except OSError:
                self._conn = None

    def close(self) -> None:
        try:
            self.server.close()
        except OSError:
            pass
        with self._lock:
            conn, self._conn = self._conn, None
        if conn is not None:
            _close(conn)

    def _accept(self) -> None:
        while True:
            try:
                conn, _ = self.server.accept()
            except OSError:
                return
            threading.Thread(target=self._serve, args=(conn,), daemon=True).start()

    def _serve(self, conn: socket.socket) -> None:
        reader = conn.makefile("rb")
        try:
            first = json.loads(reader.readline() or b"{}")
        except (ValueError, OSError):
            _close(conn)
            return
        if not isinstance(first, dict) or first.get("kind") != HELLO or \
                not hmac.compare_digest(str(first.get("token", "")), self.token):
            _close(conn)
            return
        with self._lock:
            old, self._conn = self._conn, conn
        if old is not None:
            _close(old)
        self.send(self._hello())
        try:
            for raw in reader:
                if raw.strip():
                    self._on_line(raw.decode("utf-8", errors="replace"))
        except OSError:
            pass
        with self._lock:
            if self._conn is conn:
                self._conn = None
        _close(conn)


def _close(conn: socket.socket) -> None:
    try:
        conn.shutdown(socket.SHUT_RDWR)
    except OSError:
        pass
    try:
        conn.close()
    except OSError:
        pass
