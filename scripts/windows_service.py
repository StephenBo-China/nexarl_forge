"""User-owned Windows Python background process; no scheduler or shell."""
from __future__ import annotations
import hmac
import json
import os
import pathlib
import secrets
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request

import runtime_platform as storage


def _record(paths):
    return pathlib.Path(paths.install_root) / "state/service.json"


def _read(paths):
    try:
        value = json.loads(storage.read_regular(_record(paths)).decode("utf-8"))
    except FileNotFoundError:
        return None
    if not isinstance(value, dict) or not isinstance(value.get("port"), int) or not 1 <= value["port"] <= 65535:
        raise ValueError("invalid service state")
    for name in ("instance", "secret"):
        if not isinstance(value.get(name), str) or len(value[name]) != 64:
            raise ValueError("invalid service identity")
    return value


def _request(value, stop=False):
    url = f"http://127.0.0.1:{value['port']}" + ("/internal/native-stop" if stop else "/health")
    request = urllib.request.Request(url, data=b"{}" if stop else None,
        headers={"Authorization": "Bearer " + value["secret"]} if stop else {})
    # Do not let machine proxy settings send the local control secret elsewhere.
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())
    with opener.open(request, timeout=2 if stop else 0.5) as response:
        if response.geturl() != url:
            raise ValueError("unexpected service redirect")
        raw = response.read(4097)
        if len(raw) > 4096:
            raise ValueError("oversized service response")
        return json.loads(raw.decode("utf-8"))


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def status(paths):
    value = _read(paths)
    if value is None:
        return {"ok": False, "status": "stopped"}
    try:
        health = _request(value)
        if health.get("service") != "vibe-memory" or health.get("instance") != value["instance"]:
            return {"ok": False, "status": "foreign_service"}
    except (OSError, ValueError, urllib.error.URLError):
        return {"ok": False, "status": "stopped"}
    return {"ok": True, "status": "running", "instance": value["instance"], "port": value["port"]}


def start(paths, *, port=8897):
    if not storage.IS_WINDOWS:
        raise RuntimeError("Windows service backend requires Windows")
    if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
        raise ValueError("invalid service port")
    storage.private_directory(pathlib.Path(paths.install_root))
    storage.private_directory(pathlib.Path(paths.install_root) / "state")
    with storage.file_lock(pathlib.Path(paths.install_root) / "state/service.lock"):
        existing = status(paths)
        if existing["ok"]:
            if existing["port"] != port:
                raise ValueError("stop the current service before changing its port")
            return existing
        if existing["status"] == "foreign_service":
            raise ValueError("another process owns the configured port")
        value = {"port": port, "instance": secrets.token_hex(32), "secret": secrets.token_hex(32)}
        storage.atomic_write(_record(paths), json.dumps(value).encode("utf-8"))
        home = pathlib.Path(paths.personal_memory).parents[1]
        environment = os.environ.copy()
        environment.update(USERPROFILE=str(home), HOME=str(home),
            LOCALAPPDATA=str(pathlib.Path(paths.install_root).parent),
            APPDATA=str(pathlib.Path(paths.launch_agent).parents[5]),
            MEMORY_REVIEW_HOST="127.0.0.1", MEMORY_REVIEW_PORT=str(port),
            MEMORY_REVIEW_PROJECT_REGISTRY=str(paths.project_registry))
        environment.pop("MEMORY_REVIEW_PROJECT_ROOT", None)
        environment["PYTHONUTF8"] = "1"
        environment["PYTHONIOENCODING"] = "utf-8"
        log = pathlib.Path(paths.install_root) / "logs/service.log"
        log.parent.mkdir(parents=True, exist_ok=True)
        runner = pathlib.Path(paths.install_root) / "current/scripts/windows_service.py"
        if not runner.is_file():
            runner = pathlib.Path(__file__).resolve()
        with log.open("ab") as output:
            process = subprocess.Popen([sys.executable, str(runner), "--serve", str(_record(paths))],
                stdin=subprocess.DEVNULL, stdout=output, stderr=output, env=environment,
                close_fds=True, creationflags=subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP)
        # Retain/reap the Popen object while the intentionally detached child
        # lives; avoid leaking its process handle or reporting ResourceWarning.
        value["pid"] = process.pid
        storage.atomic_write(_record(paths), json.dumps(value).encode("utf-8"))
        threading.Thread(target=process.wait, daemon=True).start()
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            observed = status(paths)
            if observed["ok"]:
                return observed
            if process.poll() is not None:
                raise RuntimeError(f"background process exited; see {log}")
            time.sleep(0.05)
        # Never terminate an unverified PID. The child validates its identity.
        raise TimeoutError(f"service readiness timed out; see {log}")


def stop(paths):
    with storage.file_lock(pathlib.Path(paths.install_root) / "state/service.lock"):
        current = status(paths)
        if not current["ok"]:
            if current["status"] == "foreign_service":
                raise ValueError("refusing to stop a foreign service")
            return {"ok": True, "status": "stopped"}
        value = _read(paths)
        for attempt in range(3):
            try:
                reply = _request(value, stop=True)
                if reply.get("instance") != value["instance"]:
                    raise ValueError("service identity changed during stop")
                break
            except urllib.error.HTTPError:
                raise
            except (OSError, urllib.error.URLError):
                # A request may reach shutdown before its response arrives.
                # Recheck only this instance; never signal a PID on failure.
                if not status(paths)["ok"]:
                    break
                if attempt == 2:
                    raise
                time.sleep(0.1)
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if not status(paths)["ok"]:
                pid = value.get("pid")
                if pid is None or pid == os.getpid() or not storage.pid_is_alive(pid):
                    return {"ok": True, "status": "stopped"}
            time.sleep(0.05)
        raise TimeoutError("service did not stop")


def serve(record):
    value = json.loads(storage.read_regular(pathlib.Path(record)).decode("utf-8"))
    import memory_review_server as app
    class Handler(app.Handler):
        def do_GET(self):
            if self.path == "/health":
                self.send_json({**app.health_payload(), "instance": value["instance"]})
                return
            super().do_GET()

        def do_POST(self):
            if self.path == "/internal/native-stop":
                if not hmac.compare_digest(self.headers.get("Authorization", ""), "Bearer " + value["secret"]):
                    self.send_json({"error": "unauthorized"}, status=403)
                    return
                self.send_json({"ok": True, "instance": value["instance"]})
                threading.Thread(target=self.server.shutdown, daemon=True).start()
                return
            super().do_POST()
    app.review.build_queue()
    server = app.ThreadingHTTPServer(("127.0.0.1", value["port"]), Handler)
    try:
        server.serve_forever(poll_interval=0.05)
    finally:
        server.server_close()


if __name__ == "__main__":
    if len(sys.argv) != 3 or sys.argv[1] != "--serve":
        raise SystemExit(2)
    serve(sys.argv[2])
