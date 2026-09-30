"""Supabase-mode checks for `orchestrator.py health-check`.

- RLS blocks anonymous reads of votes (also for a vote that certainly exists)
- a test vote round-trips (anon plain insert while a round is open, read back by the service role)
- the realtime channel delivers a votes INSERT event (the console relies on this)

All test data lives in a temporary session with use_case 'health_check', which the status view and
the orchestrator ignore, and which is deleted at the end (also on failure).

The realtime check uses a tiny standard-library websocket client (Phoenix protocol, vsn 1.0.0).
It subscribes with the service key, so it proves the publication and realtime work; the
facilitator-JWT side of RLS for realtime is exercised by the console itself (connection indicator).
"""
from __future__ import annotations

import base64
import json
import os
import socket
import ssl
import struct
import time
import uuid
from urllib.parse import urlparse

from backend_supabase import SupabaseBackend, anon_request

TEST_USE_CASE = "health_check"


# --- minimal websocket client ------------------------------------------------------------------

class MiniWebSocket:
    def __init__(self, url: str, timeout: float = 10.0):
        u = urlparse(url)
        port = u.port or (443 if u.scheme == "wss" else 80)
        raw = socket.create_connection((u.hostname, port), timeout=timeout)
        self.sock = ssl.create_default_context().wrap_socket(raw, server_hostname=u.hostname) \
            if u.scheme == "wss" else raw
        key = base64.b64encode(os.urandom(16)).decode()
        path = u.path + (f"?{u.query}" if u.query else "")
        req = (f"GET {path} HTTP/1.1\r\nHost: {u.hostname}\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
               f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n")
        self.sock.sendall(req.encode())
        head = b""
        while b"\r\n\r\n" not in head:
            chunk = self.sock.recv(4096)
            if not chunk:
                raise ConnectionError("websocket handshake: connection closed")
            head += chunk
        status = head.split(b"\r\n", 1)[0].decode(errors="replace")
        if " 101 " not in status + " ":
            raise ConnectionError(f"websocket handshake failed: {status}")
        self.buf = head.split(b"\r\n\r\n", 1)[1]

    def send(self, obj: dict) -> None:
        data = json.dumps(obj).encode()
        mask = os.urandom(4)
        n = len(data)
        header = bytes([0x81]) + (bytes([0x80 | n]) if n < 126 else
                                  bytes([0x80 | 126]) + struct.pack(">H", n) if n < 65536 else
                                  bytes([0x80 | 127]) + struct.pack(">Q", n))
        self.sock.sendall(header + mask + bytes(b ^ mask[i % 4] for i, b in enumerate(data)))

    def _read(self, n: int) -> bytes:
        while len(self.buf) < n:
            chunk = self.sock.recv(65536)
            if not chunk:
                raise ConnectionError("websocket closed")
            self.buf += chunk
        out, self.buf = self.buf[:n], self.buf[n:]
        return out

    def recv(self, timeout: float) -> dict | None:
        self.sock.settimeout(timeout)
        try:
            b1, b2 = self._read(2)
        except socket.timeout:
            return None
        n = b2 & 0x7F
        if n == 126:
            n = struct.unpack(">H", self._read(2))[0]
        elif n == 127:
            n = struct.unpack(">Q", self._read(8))[0]
        payload = self._read(n)
        opcode = b1 & 0x0F
        if opcode == 8:
            raise ConnectionError("websocket closed by server")
        if opcode != 1:
            return {}
        try:
            return json.loads(payload.decode())
        except ValueError:
            return {}

    def close(self) -> None:
        try:
            self.sock.close()
        except OSError:
            pass


# --- checks -----------------------------------------------------------------------------------

def run_all(ctx, check, env: dict) -> list[bool]:
    url, anon_key, service_key = env.get("SUPABASE_URL"), env.get("SUPABASE_ANON_KEY"), env.get("SUPABASE_SERVICE_ROLE_KEY")
    results = []
    if not anon_key:
        check("SUPABASE_ANON_KEY is set in .env", lambda: (_ for _ in ()).throw(RuntimeError("missing")))
        return [False]
    be = SupabaseBackend(url, service_key)
    state = {}

    def setup():
        s = be.create_session("health check (temporary)", TEST_USE_CASE, ctx.questions())
        state["session"] = s
        state["round"] = be.open_round(s["id"])
        return f"temporary session {s['id'][:8]}…, round open"

    def anon_blocked_empty():
        resp = anon_request(url, anon_key, "GET", "votes", params={"select": "id", "limit": "5"})
        rows = resp.json() if resp.status_code == 200 else []
        if rows:
            raise RuntimeError(f"anon read {len(rows)} vote(s)!")
        return f"HTTP {resp.status_code}, 0 rows"

    def roundtrip_and_realtime():
        rid = state["round"]["id"]
        ws_url = url.replace("https://", "wss://").rstrip("/") + \
            f"/realtime/v1/websocket?apikey={service_key}&vsn=1.0.0"
        ws = MiniWebSocket(ws_url)
        try:
            payload = {"config": {"broadcast": {"self": False}, "presence": {"key": ""},
                                  "postgres_changes": [{"event": "INSERT", "schema": "public", "table": "votes",
                                                        "filter": f"round_id=eq.{rid}"}]}}
            if service_key.startswith("eyJ"):
                payload["access_token"] = service_key
            ws.send({"topic": "realtime:plurarch-health", "event": "phx_join", "payload": payload,
                     "ref": "1", "join_ref": "1"})
            joined = subscribed = False
            t_end = time.monotonic() + 10
            while time.monotonic() < t_end and not subscribed:
                msg = ws.recv(1.0) or {}
                if msg.get("event") == "phx_reply" and msg.get("ref") == "1":
                    if (msg.get("payload") or {}).get("status") != "ok":
                        raise RuntimeError(f"realtime join refused: {msg.get('payload')}")
                    joined = True
                if msg.get("event") == "system" and "postgres" in json.dumps(msg).lower():
                    status = (msg.get("payload") or {}).get("status")
                    if status not in (None, "ok"):
                        raise RuntimeError(f"realtime subscription failed: {msg.get('payload')}")
                    subscribed = True
            if not joined:
                raise RuntimeError("realtime join timed out")
            pid = f"health-{uuid.uuid4().hex[:10]}"
            resp = anon_request(url, anon_key, "POST", "votes", prefer="return=minimal",
                                json=[{"round_id": rid, "participant_id": pid, "question_key": "window_ratio",
                                       "value": "45"}])
            if resp.status_code >= 300:
                raise RuntimeError(f"anon vote insert failed: HTTP {resp.status_code} {resp.text[:200]}")
            stored = [v for v in be.get_votes(rid) if v["participant_id"] == pid]
            if len(stored) != 1:
                raise RuntimeError(f"vote not stored (found {len(stored)})")
            leak = anon_request(url, anon_key, "GET", "votes", params={"round_id": f"eq.{rid}"})
            if leak.status_code == 200 and leak.json():
                raise RuntimeError("anon can read the test vote!")
            got = False
            t_end = time.monotonic() + 10
            while time.monotonic() < t_end and not got:
                msg = ws.recv(1.0) or {}
                got = msg.get("event") == "postgres_changes"
            state["realtime"] = got
            return "anon insert → stored, not readable by anon"
        finally:
            ws.close()

    def realtime_result():
        if state.get("realtime"):
            return "votes INSERT delivered over the websocket"
        raise RuntimeError("no realtime event within 10 s (check Database → Publications → supabase_realtime)")

    def cleanup():
        if state.get("session"):
            try:
                be.close_round(state["session"]["id"])
            except Exception:
                pass
            be.delete_session(state["session"]["id"])
        return "temporary session removed"

    try:
        results.append(check("RLS: anon cannot read votes", anon_blocked_empty))
        if check("setup: temporary test session", setup):
            results.append(check("a test vote round-trips (and stays private)", roundtrip_and_realtime))
            results.append(check("the realtime channel receives events", realtime_result))
        else:
            results.append(False)
    finally:
        check("cleanup", cleanup)
    return results
