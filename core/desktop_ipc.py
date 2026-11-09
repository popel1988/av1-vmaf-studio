"""Per-user single-instance lock and authenticated, bounded loopback IPC."""
from __future__ import annotations

import hmac
import json
import os
import secrets
import socket
import threading
import time
from pathlib import Path

MAX_MESSAGE = 262144


def _receive(conn: socket.socket) -> dict:
    data = bytearray()
    while b"\n" not in data:
        block = conn.recv(min(4096, MAX_MESSAGE + 1 - len(data)))
        if not block:
            raise ValueError("Unvollständige Desktop-Nachricht.")
        data.extend(block)
        if len(data) > MAX_MESSAGE:
            raise ValueError("Desktop-Nachricht zu groß.")
    value = json.loads(data.split(b"\n", 1)[0])
    if not isinstance(value, dict):
        raise ValueError("Ungültige Desktop-Nachricht.")
    return value


class SingleInstance:
    def __init__(self, directory: Path) -> None:
        self.directory = directory
        self.endpoint = directory / "desktop-instance.json"
        self._lock_file = None
        self._socket = None
        self._thread = None
        self._stop = threading.Event()
        self._token = secrets.token_hex(32)

    def acquire(self) -> bool:
        self.directory.mkdir(parents=True, exist_ok=True)
        handle = open(self.directory / "desktop-instance.lock", "a+b")
        # Reading byte zero on Windows itself fails while another process holds
        # the byte-range lock. Inspect length without touching the locked region.
        if not os.fstat(handle.fileno()).st_size:
            handle.write(b"0")
            handle.flush()
        handle.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            handle.close()
            return False
        self._lock_file = handle
        return True

    def listen(self, on_open) -> None:
        if self._lock_file is None:
            raise RuntimeError("Desktop-Instanz besitzt keine Sperre.")
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.bind(("127.0.0.1", 0))
        sock.listen(4)
        sock.settimeout(0.25)
        self._socket = sock
        # LOCALAPPDATA inherits the user's ACL; never publish token in app logs.
        fd = os.open(str(self.endpoint), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as output:
            json.dump({"port": sock.getsockname()[1], "token": self._token}, output)

        def run():
            while not self._stop.is_set():
                try:
                    conn, _ = sock.accept()
                except socket.timeout:
                    continue
                except OSError:
                    break
                with conn:
                    conn.settimeout(2)
                    try:
                        request = _receive(conn)
                        token = request.get("token")
                        if not isinstance(token, str) or not hmac.compare_digest(token, self._token):
                            raise ValueError("Authentifizierung fehlgeschlagen.")
                        if request.get("action") != "open":
                            raise ValueError("Unbekannte Aktion.")
                        # Callback validates paths before acknowledging receipt.
                        on_open(request.get("paths"))
                        response = {"ok": True}
                    except Exception:
                        response = {"ok": False, "error": "Dateien konnten nicht übernommen werden."}
                    try:
                        conn.sendall(json.dumps(response).encode() + b"\n")
                    except OSError:
                        pass

        self._thread = threading.Thread(target=run, name="desktop-ipc", daemon=True)
        self._thread.start()

    def forward(self, paths: list[str], timeout: float = 8) -> None:
        deadline = time.monotonic() + timeout
        while True:
            try:
                endpoint = json.loads(self.endpoint.read_text(encoding="utf-8"))
                port, token = endpoint["port"], endpoint["token"]
                if not isinstance(port, int) or not 1 <= port <= 65535:
                    raise ValueError("Ungültiger IPC-Port.")
                with socket.create_connection(("127.0.0.1", port), timeout=2) as conn:
                    conn.settimeout(3)
                    conn.sendall(json.dumps({
                        "action": "open", "paths": paths, "token": token,
                    }).encode() + b"\n")
                    response = _receive(conn)
                    if not response.get("ok"):
                        raise RuntimeError(response.get("error", "Desktop-IPC fehlgeschlagen."))
                    return
            except (OSError, ValueError, KeyError):
                if time.monotonic() >= deadline:
                    raise RuntimeError("Die laufende Desktop-Instanz antwortet nicht.") from None
                time.sleep(0.1)

    def close(self) -> None:
        if self._lock_file is None:
            return
        self._stop.set()
        if self._socket is not None:
            self._socket.close()
        if self._thread is not None:
            self._thread.join(3)
        try:
            self.endpoint.unlink(missing_ok=True)
        finally:
            if os.name == "nt":
                import msvcrt
                self._lock_file.seek(0)
                msvcrt.locking(self._lock_file.fileno(), msvcrt.LK_UNLCK, 1)
            self._lock_file.close()
            self._lock_file = None
