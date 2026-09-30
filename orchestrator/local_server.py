"""Local mode web server: the participant app, the console and the local API (docs/ARCHITECTURE.md).

Runs inside `orchestrator.py run` when config/local.json has "backend": "local". Phones on the same
Wi-Fi open http://<laptop-ip>:<port>/. Standard library only (ThreadingHTTPServer).
"""
from __future__ import annotations

import json
import mimetypes
import secrets
import socket
import threading
import time
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from backend_base import BackendError
from backend_local import LocalBackend

REPO = Path(__file__).resolve().parent.parent
SITE = REPO / "site"
CONFIG = REPO / "config"
PUBLIC_CONFIG_EXCLUDE = {"local.json"}
MAX_BODY = 16 * 1024

mimetypes.add_type("text/javascript", ".js")
mimetypes.add_type("image/svg+xml", ".svg")


def lan_ip() -> str:
    """The laptop's address on the current network (no packets are sent)."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("10.255.255.255", 1))
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()


def facilitator_key(state_dir: Path) -> str:
    path = state_dir / "facilitator_key.txt"
    if path.exists():
        key = path.read_text(encoding="utf-8").strip()
        if key:
            return key
    key = secrets.token_urlsafe(18)
    path.write_text(key, encoding="utf-8")
    return key


class _StatusCache:
    """Hundreds of phones poll the status: serve it from a 0.5 s cache."""

    def __init__(self, backend: LocalBackend, ttl: float = 0.5):
        self.backend, self.ttl = backend, ttl
        self._value, self._at, self._lock = None, 0.0, threading.Lock()

    def get(self):
        with self._lock:
            if time.monotonic() - self._at > self.ttl:
                self._value = self.backend.session_status()
                self._at = time.monotonic()
            return self._value

    def invalidate(self):
        with self._lock:
            self._at = 0.0


def validate_vote_value(question: dict, value) -> str | None:
    """Return the normalized value as text, or None if invalid (mirrors the Supabase trigger)."""
    if not isinstance(value, (str, int, float)) or isinstance(value, bool):
        return None
    text = str(value).strip()
    if not text or len(text) > 32:
        return None
    if question["type"] == "choice":
        return text if text in (question.get("options") or []) else None
    try:
        f = float(text)
    except ValueError:
        return None
    lo, hi, step = question["min"], question["max"], question["step"]
    if not (lo - 1e-9 <= f <= hi + 1e-9):
        return None
    k = (f - lo) / step
    if abs(k - round(k)) > 1e-6:
        return None
    return str(int(f)) if f.is_integer() else str(round(f, 3))


class _QuietServer(ThreadingHTTPServer):
    """Phones drop connections all the time (screen off, Wi-Fi hand-over): never print tracebacks
    for that on the orchestrator's on-screen console."""
    daemon_threads = True
    log = None

    def handle_error(self, request, client_address):
        import sys
        exc = sys.exc_info()[1]
        if isinstance(exc, (ConnectionError, TimeoutError, OSError)):
            return
        if self.log:
            self.log(f"server error from {client_address[0]}: {exc!r}", "err")


class LocalServer:
    def __init__(self, backend: LocalBackend, state_dir: Path, port: int, use_case: str, log=print):
        self.backend, self.state_dir, self.port, self.use_case, self.log = backend, state_dir, port, use_case, log
        self.key = facilitator_key(state_dir)
        self.ip = lan_ip()
        self.join_url = f"http://{self.ip}:{port}/"
        self.console_url = f"http://localhost:{port}/console.html#key={self.key}"
        self.status = _StatusCache(backend)
        self.stats = {"requests": 0, "votes_ok": 0, "votes_rejected": 0}
        self._seen: set[str] = set()
        self.httpd = _QuietServer(("0.0.0.0", port), self._handler())
        self.httpd.log = log

    def note_device(self, ip: str, user_agent: str, path: str) -> None:
        """Print one line the first time a device loads the participant page, so the facilitator
        (and troubleshooting) can see that phones really reach the laptop."""
        if path not in ("/", "/index.html") or ip in self._seen:
            return
        self._seen.add(ip)
        ua = user_agent or ""
        kind = ("iPhone" if "iPhone" in ua else "iPad" if "iPad" in ua else "Android" if "Android" in ua
                else "this laptop" if ip.startswith("127.") else "computer")
        browser = ("Chrome" if ("Chrome" in ua or "CriOS" in ua) and "Edg" not in ua else "Edge" if "Edg" in ua
                   else "Firefox" if "Firefox" in ua or "FxiOS" in ua else "Safari" if "Safari" in ua else "browser")
        self.log(f"Device connected: {ip} ({kind}, {browser}) · {len(self._seen)} so far", "accent")

    def start(self) -> threading.Thread:
        t = threading.Thread(target=self.httpd.serve_forever, name="local-server", daemon=True)
        t.start()
        return t

    def stop(self) -> None:
        self.httpd.shutdown()

    # --- request handling -----------------------------------------------------------
    def _handler(self):
        srv = self

        class Handler(BaseHTTPRequestHandler):
            server_version = "Plurarch"
            protocol_version = "HTTP/1.1"

            def log_message(self, fmt, *args):  # keep the orchestrator console readable
                pass

            # helpers
            def _send(self, code: int, body: bytes, ctype: str, cache: str = "no-store"):
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", cache)
                self.send_header("X-Content-Type-Options", "nosniff")
                self.end_headers()
                if self.command != "HEAD":
                    self.wfile.write(body)

            def _json(self, code: int, obj):
                self._send(code, json.dumps(obj, ensure_ascii=False, default=str).encode("utf-8"),
                           "application/json; charset=utf-8")

            def _file(self, path: Path, cache: str = "no-cache"):
                try:
                    data = path.read_bytes()
                except OSError:
                    return self._json(404, {"error": "not_found"})
                ctype = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
                if ctype.startswith("text/") or ctype in ("application/json", "image/svg+xml"):
                    ctype += "; charset=utf-8"
                self._send(200, data, ctype, cache)

            def _safe(self, root: Path, rel: str) -> Path | None:
                target = (root / rel.lstrip("/")).resolve()
                try:
                    target.relative_to(root.resolve())
                except ValueError:
                    return None
                return target if target.is_file() else None

            def _authorized(self) -> bool:
                auth = self.headers.get("Authorization", "")
                return secrets.compare_digest(auth, f"Bearer {srv.key}")

            def _body(self):
                n = int(self.headers.get("Content-Length") or 0)
                if n > MAX_BODY:
                    raise ValueError("body too large")
                raw = self.rfile.read(n) if n else b""
                return json.loads(raw.decode("utf-8")) if raw else {}

            # routes
            def do_HEAD(self):
                self.do_GET()

            def do_GET(self):
                srv.stats["requests"] += 1
                url = urlparse(self.path)
                path = url.path
                srv.note_device(self.client_address[0], self.headers.get("User-Agent", ""), path)
                try:
                    if path == "/config.js":
                        cfg = {"backend": "local", "useCase": srv.use_case, "apiBase": ""}
                        body = f"window.PLURARCH_CONFIG = {json.dumps(cfg)};\n".encode("utf-8")
                        return self._send(200, body, "text/javascript; charset=utf-8")
                    if path == "/api/status":
                        return self._json(200, srv.status.get())
                    if path.startswith("/api/decisions/"):
                        d = srv.backend.get_decision(path.rsplit("/", 1)[-1])
                        return self._json(200 if d else 404, d or {"error": "not_found"})
                    if path == "/api/console/state":
                        if not self._authorized():
                            return self._json(401, {"error": "unauthorized"})
                        q = parse_qs(url.query)
                        return self._json(200, srv.console_state((q.get("round_id") or [None])[0]))
                    if path.startswith("/api/"):
                        return self._json(404, {"error": "not_found"})
                    if path in ("/qr.png", "/qr_plain.png"):
                        p = srv.state_dir / path.lstrip("/")
                        return self._file(p) if p.exists() else self._json(404, {"error": "not_found"})
                    if path.startswith("/config/"):
                        rel = path[len("/config/"):]
                        if Path(rel).name in PUBLIC_CONFIG_EXCLUDE:
                            return self._json(404, {"error": "not_found"})
                        p = self._safe(CONFIG, rel)
                        return self._file(p) if p else self._json(404, {"error": "not_found"})
                    rel = "index.html" if path in ("", "/") else path
                    p = self._safe(SITE, rel)
                    return self._file(p) if p else self._json(404, {"error": "not_found"})
                except (ConnectionError, TimeoutError):
                    return  # the phone went away; nothing to answer
                except Exception as e:  # never kill the server thread
                    srv.log(f"server error on GET {path}: {e}", "err")
                    return self._json(500, {"error": "server_error"})

            def do_POST(self):
                srv.stats["requests"] += 1
                path = urlparse(self.path).path
                try:
                    if path == "/api/votes":
                        return self._json(*srv.submit_votes(self._body()))
                    if path in ("/api/rounds/open", "/api/rounds/close"):
                        if not self._authorized():
                            return self._json(401, {"error": "unauthorized"})
                        return self._json(*srv.round_action(path.endswith("open")))
                    return self._json(404, {"error": "not_found"})
                except ValueError as e:
                    return self._json(400, {"error": "bad_request", "detail": str(e)})
                except (ConnectionError, TimeoutError):
                    return
                except Exception as e:
                    srv.log(f"server error on POST {path}: {e}", "err")
                    return self._json(500, {"error": "server_error"})

        return Handler

    # --- API logic --------------------------------------------------------------------
    def submit_votes(self, body: dict):
        round_id = body.get("round_id")
        pid = body.get("participant_id")
        votes = body.get("votes")
        if not isinstance(round_id, str) or not isinstance(pid, str) or not (1 <= len(pid) <= 64) \
                or not isinstance(votes, list) or not (1 <= len(votes) <= 20):
            return 400, {"error": "bad_request", "detail": "round_id, participant_id and votes are required"}
        rnd = self.backend.get_round(round_id)
        if not rnd:
            return 400, {"error": "unknown_round"}
        if rnd["status"] != "open":
            return 409, {"error": "round_closed"}
        questions = {q["key"]: q for q in self.backend.get_questions(rnd["session_id"])}
        rows = []
        for v in votes:
            q = questions.get(v.get("question_key")) if isinstance(v, dict) else None
            value = validate_vote_value(q, v.get("value")) if q else None
            if value is None:
                self.stats["votes_rejected"] += 1
                return 400, {"error": "invalid_value", "detail": f"invalid vote {v!r}"}
            rows.append({"round_id": round_id, "participant_id": pid, "question_key": q["key"],
                         "value": value, "is_simulated": False})
        inserted = self.backend.insert_votes(rows)
        self.stats["votes_ok"] += inserted
        return 200, {"inserted": inserted, "duplicates": len(rows) - inserted}

    def round_action(self, open_: bool):
        s = self.backend.get_active_session()
        if not s:
            return 409, {"error": "no_active_session"}
        try:
            r = self.backend.open_round(s["id"]) if open_ else self.backend.close_round(s["id"])
        except BackendError as e:
            return 409, {"error": e.code, "detail": e.detail}
        self.status.invalidate()
        self.log(f"Round {r['number']} {'opened' if open_ else 'closed'} from the console", "ok")
        return 200, {"round": r}

    def console_state(self, round_id: str | None):
        s = self.backend.get_active_session()
        if not s:
            return {"session": None, "questions": [], "rounds": [], "round": None, "votes": [],
                    "agent_events": [], "decisions": [], "participants_total": 0,
                    "join_url": self.join_url, "server_time": _now()}
        rounds = self.backend.list_rounds(s["id"])
        rnd = next((r for r in rounds if r["id"] == round_id), None) or (rounds[-1] if rounds else None)
        return {
            "session": s,
            "questions": self.backend.get_questions(s["id"]),
            "rounds": rounds,
            "round": rnd,
            "votes": self.backend.get_votes(rnd["id"]) if rnd else [],
            "agent_events": self.backend.list_agent_events(rnd["id"]) if rnd else [],
            "decisions": self.backend.list_decisions(s["id"]),
            "participants_total": self.backend.count_participants(s["id"]),
            "join_url": self.join_url,
            "server_time": _now(),
        }


def _now() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
