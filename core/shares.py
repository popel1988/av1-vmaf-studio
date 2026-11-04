"""SMB-Freigaben, die das Studio selbst einhängt.

Die Zugangsdaten liegen in /data/shares.json und gehen nicht an den Browser
zurück. Eine Freigabe wird eine zusätzliche Medienwurzel, sobald sie hängt.
Die lokale Standard-Ausgabe bleibt davon unberührt.
"""
from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import threading
import uuid
from pathlib import Path
from typing import Optional

from . import config

logger = logging.getLogger("vcompress.shares")

_lock = threading.RLock()
_runtime: dict[str, dict] = {}
_VERSIONS = ("auto", "3.1.1", "3.0", "2.1", "2.0")
_MOUNT_HINTS = (
    ("error(13)", "Anmeldung abgelehnt"),
    ("Permission denied", "Anmeldung abgelehnt"),
    ("error(113)", "Server nicht erreichbar"),
    ("error(112)", "Server nicht erreichbar"),
    ("No route to host", "Server nicht erreichbar"),
    ("No such device", "Der Kernel stellt kein SMB (cifs) bereit"),
    ("unknown filesystem type", "Im Container fehlt cifs-utils. Das Image muss neu gebaut werden."),
    ("helper failed", "Im Container fehlt cifs-utils. Das Image muss neu gebaut werden."),
    ("Operation not permitted", "Der Container darf keine Freigabe einhängen."),
)


def _store_path() -> Path:
    return config.DATA_DIR / "shares.json"


def _mount_root() -> Path:
    return config.DATA_DIR / "share-mounts"


def _load() -> list[dict]:
    path = _store_path()
    if not path.exists():
        return []
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    items = raw.get("shares") if isinstance(raw, dict) else None
    return [item for item in items or [] if isinstance(item, dict) and item.get("id")]


def _save(items: list[dict]) -> None:
    path = _store_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps({"shares": items}, indent=2, ensure_ascii=False) + "\n"
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(payload, encoding="utf-8")
    try:
        os.chmod(tmp, 0o600)
    except OSError:
        pass
    tmp.replace(path)


def _clean(value, limit: int = 80) -> str:
    return str(value or "").replace("\x00", "").strip()[:limit]


def _split_location(raw_host: str, share: str, subpath: str) -> tuple[str, str, str]:
    """smb://, cifs:// und UNC auf Server, Freigabe und Unterordner verteilen.

    Ein extra eingetragener Freigabename bleibt. Die Adresse darf nur den
    Server nennen, zum Beispiel smb://server/.
    """
    raw = str(raw_host or "").replace("\x00", "").strip()
    lower = raw.lower()
    for prefix in ("smb://", "cifs://"):
        if lower.startswith(prefix):
            raw = raw[len(prefix):]
            break
    raw = raw.replace("\\", "/").split("?", 1)[0].split("#", 1)[0].strip()
    while raw.startswith("/"):
        raw = raw[1:]
    head, _, tail = raw.partition("/")
    if "@" in head:
        head = head.rsplit("@", 1)[-1]
    if head.count(":") == 1:
        server, port = head.split(":", 1)
        if port.isdigit():
            head = server
    parts = [part for part in ([head] + (tail.split("/") if tail else [])) if part]
    if not parts:
        return "", share, subpath
    host = parts[0]
    if not share and len(parts) > 1:
        share = parts[1]
        if not subpath and len(parts) > 2:
            subpath = "/".join(parts[2:])
    return host, share, subpath


def _valid_host(host: str) -> bool:
    if not host or len(host) > 253 or host.startswith("-") or ".." in host:
        return False
    return all(ch.isalnum() or ch in ".-" for ch in host)


def _valid_share(name: str) -> bool:
    if not name or name in (".", "..") or len(name) > 80:
        return False
    forbidden = set('\\/\x00\r\n\t;|$`"\'')
    return not any(ch in forbidden for ch in name)


def normalize(fields: dict, previous: Optional[dict] = None) -> tuple[dict, str]:
    """Formulardaten prüfen. Ein leeres Passwort behält das gespeicherte."""
    name = _clean(fields.get("name"))
    host, share, sub_in = _split_location(
        fields.get("host"), _clean(fields.get("share")), str(fields.get("subpath") or ""))
    host = host.strip()[:253]
    share = _clean(share)
    if not name:
        return {}, "Name fehlt"
    if not _valid_host(host):
        return {}, "Server fehlt oder enthält ungültige Zeichen"
    if not _valid_share(share):
        return {}, "Freigabename fehlt oder enthält ungültige Zeichen"
    username = _clean(fields.get("username"), 128)
    domain = _clean(fields.get("domain"), 128)
    password = str(fields.get("password") or "")
    if "\n" in password or "\r" in password or "\x00" in password or len(password) > 256:
        return {}, "Passwort enthält ungültige Zeichen"
    if "=" in username or "=" in domain:
        return {}, "Benutzername oder Domäne enthält ein Gleichheitszeichen"
    guest = bool(fields.get("guest"))
    if guest:
        username, domain, password = "", "", ""
    elif not password and previous and not previous.get("guest"):
        password = str(previous.get("password") or "")
    if not guest and not username:
        return {}, "Benutzer fehlt"
    vers = _clean(fields.get("vers") or "auto", 8) or "auto"
    if vers not in _VERSIONS:
        vers = "auto"
    subpath = config.safe_subdir(sub_in)
    spec = {
        "name": name,
        "host": host,
        "share": share,
        "subpath": subpath,
        "username": username,
        "password": password,
        "domain": domain,
        "guest": guest,
        "readonly": bool(fields.get("readonly", True)),
        "vers": vers,
    }
    return spec, ""


def _unique_root(name: str, items: list[dict], own_id: str = "") -> str:
    base = config._slug(name)[:40]
    used = {n for n, _ in config.MEDIA_ROOTS}
    for item in items:
        if item.get("id") != own_id and item.get("root"):
            used.add(item["root"])
    candidate, n = base, 2
    while candidate in used:
        candidate = f"{base}_{n}"
        n += 1
    return candidate


def _same_target(a: dict, b: dict) -> bool:
    return (a.get("host"), a.get("share"), a.get("subpath") or "") == (
        b.get("host"), b.get("share"), b.get("subpath") or "")


def public(spec: dict) -> dict:
    state = _runtime.get(spec.get("id") or "", {})
    return {
        "id": spec.get("id") or "",
        "name": spec.get("name") or "",
        "root": spec.get("root") or "",
        "host": spec.get("host") or "",
        "share": spec.get("share") or "",
        "subpath": spec.get("subpath") or "",
        "username": spec.get("username") or "",
        "domain": spec.get("domain") or "",
        "guest": bool(spec.get("guest")),
        "readonly": bool(spec.get("readonly", True)),
        "vers": spec.get("vers") or "auto",
        "password_set": bool(spec.get("password")) and not spec.get("guest"),
        "mounted": bool(state.get("mounted")),
        "busy": bool(state.get("busy")),
        "error": state.get("error") or "",
        "path": state.get("path") or "",
    }


def list_public() -> list[dict]:
    with _lock:
        return [public(item) for item in _load()]


def _publish_locked() -> None:
    roots = []
    for item in _load():
        state = _runtime.get(item.get("id") or "", {})
        if state.get("mounted") and state.get("path"):
            roots.append((item.get("root") or item["id"], Path(state["path"])))
    config.set_share_roots(roots)


def linux_options(spec: dict, cred_path: Optional[Path]) -> list[str]:
    """mount.cifs-Optionen. Das Passwort steht nur in der Credential-Datei."""
    opts = []
    if spec.get("guest") or cred_path is None:
        opts.append("guest")
    else:
        opts.append(f"credentials={cred_path}")
    uid = os.getuid() if hasattr(os, "getuid") else 0
    gid = os.getgid() if hasattr(os, "getgid") else 0
    opts.extend([
        f"uid={uid}", f"gid={gid}",
        "file_mode=0644", "dir_mode=0755",
        "iocharset=utf8", "noperm", "noserverino", "nounix", "soft",
    ])
    if spec.get("readonly", True):
        opts.append("ro")
    return opts


def linux_mount_command(source: str, point: Path, options: list[str]) -> list[str]:
    return ["mount", "-t", "cifs", source, str(point), "-o", ",".join(options)]


def _explain_mount(text: str) -> str:
    raw = (text or "").strip()
    for needle, message in _MOUNT_HINTS:
        if needle.lower() in raw.lower():
            return message
    line = raw.splitlines()[-1].strip() if raw else ""
    return line[:240] or "Freigabe konnte nicht eingehängt werden"


def _unescape_mount(value: str) -> str:
    return (value.replace("\\040", " ").replace("\\011", "\t")
            .replace("\\012", "\n").replace("\\134", "\\"))


def _is_mounted(point: Path) -> bool:
    target = str(point)
    try:
        lines = Path("/proc/mounts").read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return False
    for line in lines:
        parts = line.split()
        if len(parts) >= 2 and _unescape_mount(parts[1]) == target:
            return True
    return False


def _write_credentials(spec: dict, path: Path) -> None:
    lines = [f"username={spec.get('username') or ''}"]
    lines.append(f"password={spec.get('password') or ''}")
    if spec.get("domain"):
        lines.append(f"domain={spec['domain']}")
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write("\n".join(lines) + "\n")


def _umount(point: Path) -> None:
    if not _is_mounted(point):
        return
    for args in (["umount", str(point)], ["umount", "-l", str(point)]):
        try:
            subprocess.run(args, capture_output=True, text=True, timeout=12, check=False)
        except (OSError, subprocess.TimeoutExpired):
            continue
        if not _is_mounted(point):
            return


def _mount_linux(spec: dict, point: Path) -> str:
    if shutil.which("mount.cifs") is None and shutil.which("mount") is None:
        return "Im Container fehlt cifs-utils. Das Image muss neu gebaut werden."
    point.mkdir(parents=True, exist_ok=True)
    _umount(point)
    cred = None
    if not spec.get("guest"):
        cred = _mount_root() / f"{spec['id']}.cred"
        _write_credentials(spec, cred)
    elif cred_path_exists(spec):
        _remove_credentials(spec)
    versions = ["3.1.1", "3.0", "2.1"] if spec.get("vers") == "auto" else [spec.get("vers") or "3.0"]
    source = f"//{spec['host']}/{spec['share']}"
    last = "Freigabe konnte nicht eingehängt werden"
    for vers in versions:
        options = linux_options(spec, cred) + [f"vers={vers}"]
        cmd = linux_mount_command(source, point, options)
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=12, check=False)
        except subprocess.TimeoutExpired:
            _umount(point)
            last = "Server antwortet nicht"
            continue
        except OSError as exc:
            return _explain_mount(str(exc))
        if proc.returncode == 0 and _is_mounted(point):
            return ""
        _umount(point)
        last = _explain_mount((proc.stderr or "") + "\n" + (proc.stdout or ""))
    return last


def cred_path_exists(spec: dict) -> bool:
    return (_mount_root() / f"{spec.get('id')}.cred").exists()


def _remove_credentials(spec: dict) -> None:
    path = _mount_root() / f"{spec.get('id')}.cred"
    try:
        path.unlink(missing_ok=True)
    except OSError:
        pass


def _mount_windows(spec: dict) -> tuple[Optional[Path], str]:
    unc = Path(f"\\\\{spec['host']}\\{spec['share']}")
    if not spec.get("guest") and spec.get("username"):
        user = spec["username"]
        if spec.get("domain"):
            user = spec["domain"] + "\\" + user
        cmd = ["net", "use", str(unc), spec.get("password") or "",
               "/user:" + user, "/persistent:no"]
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=20, check=False)
        except (OSError, subprocess.TimeoutExpired) as exc:
            return None, _explain_mount(str(exc))
        if proc.returncode != 0:
            try:
                reachable = unc.exists()
            except OSError:
                reachable = False
            if not reachable:
                return None, _explain_mount((proc.stderr or "") + "\n" + (proc.stdout or ""))
    base: Path = unc
    if spec.get("subpath"):
        base = unc.joinpath(*str(spec["subpath"]).split("/"))
    try:
        ok = base.exists()
    except OSError as exc:
        return None, _explain_mount(str(exc))
    if not ok:
        return None, "Freigabe nicht erreichbar"
    return base, ""


def _activate(spec: dict) -> tuple[Optional[Path], str]:
    if os.name == "nt":
        return _mount_windows(spec)
    point = _mount_root() / spec["id"]
    err = _mount_linux(spec, point)
    if err:
        return None, err
    base = point
    if spec.get("subpath"):
        base = point.joinpath(*str(spec["subpath"]).split("/"))
        try:
            found = base.is_dir()
        except OSError:
            found = False
        if not found:
            return None, "Ordner in der Freigabe nicht gefunden"
    return base, ""


def _connect(spec: dict) -> str:
    base, err = _activate(spec)
    with _lock:
        _runtime[spec["id"]] = {
            "mounted": base is not None and not err,
            "error": err,
            "path": str(base) if base else "",
            "busy": False,
        }
        _publish_locked()
    if err:
        logger.info("SMB %s //%s/%s: %s", spec.get("name"), spec.get("host"), spec.get("share"), err)
    else:
        logger.info("SMB %s verbunden: //%s/%s", spec.get("name"), spec.get("host"), spec.get("share"))
    return err


def add(fields: dict) -> tuple[dict, str]:
    spec, err = normalize(fields)
    if err:
        return {}, err
    with _lock:
        items = _load()
        if any(_same_target(spec, item) for item in items):
            return {}, "Diese Freigabe ist schon eingetragen"
        spec["id"] = uuid.uuid4().hex[:12]
        spec["root"] = _unique_root(spec["name"], items)
        items.append(spec)
        _save(items)
        _runtime[spec["id"]] = {"mounted": False, "error": "", "path": "", "busy": True}
    err = _connect(spec)
    with _lock:
        stored = next((item for item in _load() if item.get("id") == spec["id"]), spec)
        return public(stored), err


def remove(share_id: str) -> str:
    with _lock:
        items = _load()
        spec = next((item for item in items if item.get("id") == share_id), None)
        if spec is None:
            return "Nicht gefunden"
        items = [item for item in items if item.get("id") != share_id]
        _save(items)
        _runtime.pop(share_id, None)
        _publish_locked()
    _detach(spec)
    return ""


def _detach(spec: dict) -> None:
    if os.name == "nt":
        unc = f"\\\\{spec.get('host')}\\{spec.get('share')}"
        try:
            subprocess.run(["net", "use", unc, "/delete", "/y"],
                           capture_output=True, text=True, timeout=12, check=False)
        except (OSError, subprocess.TimeoutExpired):
            pass
        return
    _umount(_mount_root() / spec["id"])
    _remove_credentials(spec)


def reconnect(share_id: str) -> str:
    with _lock:
        spec = next((item for item in _load() if item.get("id") == share_id), None)
        if spec is None:
            return "Nicht gefunden"
        _runtime[share_id] = {"mounted": False, "error": "", "path": "", "busy": True}
    return _connect(spec)


def restore_async() -> None:
    """Gespeicherte Freigaben nach dem Start einhängen, ohne den Start zu blockieren."""
    with _lock:
        items = _load()
        for item in items:
            _runtime[item["id"]] = {"mounted": False, "error": "", "path": "", "busy": True}

    def run() -> None:
        for item in items:
            try:
                _connect(item)
            except Exception:
                logger.exception("SMB %s konnte nicht verbunden werden", item.get("name"))

    threading.Thread(target=run, name="shares", daemon=True).start()
