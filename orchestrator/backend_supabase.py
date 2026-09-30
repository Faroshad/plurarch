"""Supabase backend for the orchestrator: PostgREST over HTTPS with the service role key.

Implements orchestrator/backend_base.Backend against the schema in supabase/schema.sql
(data contract: docs/ARCHITECTURE.md). Runs only on the facilitator's machine; the service
role key bypasses RLS and must never reach a browser.

Keys and headers
    Legacy keys (anon / service_role) are JWTs: they are sent as `apikey: <key>` and
    `Authorization: Bearer <key>`. New keys (sb_publishable_... / sb_secret_...) are not
    JWTs, and Supabase says to send them ONLY in the `apikey` header ("Send publishable and
    secret keys on the apikey header, not on Authorization: Bearer"), so for them no
    Authorization header is sent. Both kinds work with this module.

Errors
    Every failure raises BackendError(code, detail). The codes:
    - 'unreachable'  connection error or timeout (no internet, wrong URL, DNS failure)
    - 'paused'       the free-plan project is paused (HTTP 540, or the project host no
                     longer resolves in DNS while supabase.com does). Fix: dashboard -> Restore.
    - 'auth'         HTTP 401/403 (wrong or missing key, or a privilege problem)
    - app codes raised by the schema's triggers/RPCs, taken from the PostgREST error message:
                     round_closed, round_not_found, unknown_question, invalid_value, ...
    - 'duplicate'    unique violation (SQLSTATE 23505) not handled by the caller
    - 'http_<status>' anything else; detail carries the PostgREST code, message and hint.

Retries
    Reads (GET) get one quick retry (0.5 s) on connection errors and 5xx (not 540).
    Writes are never retried automatically.

Pagination
    Supabase caps every response at "Max rows" (default 1000). List reads page with
    limit/offset and `Prefer: count=exact`, so 300 participants x 4 questions = 1200 votes
    come back complete.
"""
from __future__ import annotations

import json
import math
import socket
import time
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlsplit

import httpx

from backend_base import Backend, BackendError

REST_PREFIX = "/rest/v1"
PAGE_SIZE = 1000
INSERT_CHUNK = 500
READ_RETRY_DELAY_S = 0.5
PING_TIMEOUT_S = 5.0

# Code words raised by supabase/schema.sql (RAISE EXCEPTION '<word>'), passed through as-is.
APP_ERROR_CODES = frozenset({
    "round_closed", "round_not_found", "unknown_question", "invalid_value",
    "invalid_participant", "invalid_question", "round_already_open", "no_open_round",
    "not_facilitator", "session_not_found",
})

DECISION_COLUMNS = (
    "id", "session_id", "round_id", "round_number", "status", "verdict", "proposal",
    "applied_parameters", "evidence", "alternatives_considered", "changes", "rationale",
    "consistency_note", "message", "metrics", "duration_s", "created_at",
)
AGENT_EVENT_COLUMNS = ("round_id", "seq", "step", "tool", "summary")
TOOL_MAX_CHARS = 200        # DB limit 200
SUMMARY_MAX_CHARS = 2000    # DB limit 4000; keep trace lines short


# --------------------------------------------------------------------------- helpers

def _is_jwt(key: str) -> bool:
    """Legacy Supabase keys and user access tokens are JWTs (three base64url parts)."""
    return key.startswith("eyJ") and key.count(".") == 2


def _auth_headers(api_key: str, access_token: str | None = None) -> dict[str, str]:
    headers = {"apikey": api_key}
    bearer = access_token or (api_key if _is_jwt(api_key) else None)
    if bearer:
        headers["Authorization"] = f"Bearer {bearer}"
    return headers


def _normalize_url(url: str) -> str:
    url = (url or "").strip().rstrip("/")
    if url.endswith(REST_PREFIX):
        url = url[: -len(REST_PREFIX)]
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.netloc:
        raise BackendError("config", f"SUPABASE_URL must look like https://<ref>.supabase.co (got {url!r})")
    return url


def _clean_json(obj: Any) -> Any:
    """Make a value JSON-safe for PostgREST: NaN/Infinity -> null, tuples -> lists."""
    if isinstance(obj, float):
        return obj if math.isfinite(obj) else None
    if isinstance(obj, dict):
        return {str(k): _clean_json(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_clean_json(v) for v in obj]
    return obj


def _dumps(obj: Any) -> bytes:
    return json.dumps(_clean_json(obj), default=str, ensure_ascii=False, allow_nan=False).encode("utf-8")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _json(resp: httpx.Response) -> Any:
    if not resp.content:
        return None
    try:
        return resp.json()
    except ValueError as exc:
        raise BackendError(f"http_{resp.status_code}", f"invalid JSON from Supabase: {resp.text[:200]!r}") from exc


def _total_from_content_range(value: str | None) -> int | None:
    """'0-999/1200' -> 1200, '*/0' -> 0, '0-9/*' -> None."""
    if not value or "/" not in value:
        return None
    total = value.rsplit("/", 1)[1].strip()
    return int(total) if total.isdigit() else None


def _in_list(values: list[str]) -> str:
    return "in.(" + ",".join(values) + ")"


def _error_from_response(resp: httpx.Response, context: str = "") -> BackendError:
    """Map an HTTP error response (PostgREST, gateway or auth) to a BackendError."""
    status = resp.status_code
    text = resp.text or ""
    info: dict = {}
    try:
        data = resp.json()
        if isinstance(data, dict):
            info = data
    except ValueError:
        pass
    pg_code = str(info.get("code") or "")
    message = str(info.get("message") or info.get("msg") or info.get("error_description")
                  or info.get("error") or "").strip()
    details = info.get("details")
    hint = info.get("hint")

    parts = [f"HTTP {status}"]
    if pg_code:
        parts.append(pg_code)
    if message:
        parts.append(message)
    if details:
        parts.append(f"details: {details}")
    if hint:
        parts.append(f"hint: {hint}")
    if not info and text:
        parts.append(text[:200].strip())
    detail = " | ".join(parts)
    if context:
        detail = f"{context}: {detail}"

    lowered = text.lower()
    if status == 540 or (status >= 500 and ("paused" in lowered or "restoring" in lowered)):
        return BackendError("paused", f"{detail}. The Supabase project is paused: open the dashboard "
                                      "and click 'Restore project', then wait until it is healthy.")
    if message in APP_ERROR_CODES:
        return BackendError(message, f"{context}: {details or message}" if context else str(details or message))
    if pg_code == "23505":
        return BackendError("duplicate", detail)
    if status in (401, 403):
        return BackendError("auth", f"{detail}. Check SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY in .env.")
    if status == 404 and pg_code in ("PGRST205", "42P01", "PGRST202", "42883"):
        return BackendError("http_404", f"{detail}. Has supabase/schema.sql been run in the SQL editor?")
    return BackendError(f"http_{status}", detail)


def _dns_diagnosis(host: str) -> tuple[str, str]:
    """After a connection failure: is the project paused/deleted, or is the internet down?"""
    def resolves(name: str) -> bool:
        try:
            socket.getaddrinfo(name, 443, proto=socket.IPPROTO_TCP)
            return True
        except OSError:
            return False

    if resolves(host):
        return "unreachable", (f"{host} resolves but the connection failed (network, firewall or proxy). "
                               "If this persists, check the project status in the Supabase dashboard.")
    if host.endswith(".supabase.co") and resolves("supabase.com"):
        return "paused", (f"{host} does not resolve in DNS while supabase.com does: the free-plan project is "
                          "most likely PAUSED (or SUPABASE_URL is wrong / the project was deleted). "
                          "Open the dashboard and click 'Restore project'.")
    return "unreachable", (f"Cannot resolve {host}: no internet connection or DNS failure "
                           "(or SUPABASE_URL is wrong).")


# ------------------------------------------------------------ module-level helper

def anon_request(url: str, anon_key: str, method: str, path: str, **kw: Any) -> httpx.Response:
    """One HTTP request to a Supabase project as a browser would make it (tests, health checks).

    `path`: "votes" or "session_status?..." means /rest/v1/<path>; a path starting with "/" is
    taken relative to the project URL (e.g. "/auth/v1/token?grant_type=password").
    Keyword options: params, json, headers, prefer (Prefer header), access_token (a user's
    JWT from a password sign-in; default: none, i.e. the anon role), timeout (seconds).
    Works with any key (anon/publishable or service/secret). Returns the httpx.Response
    without raising for HTTP errors; raises BackendError('unreachable') on connection errors.
    """
    base = _normalize_url(url)
    full = base + (path if path.startswith("/") else f"{REST_PREFIX}/{path}")
    headers = _auth_headers(anon_key, kw.pop("access_token", None))
    prefer = kw.pop("prefer", None)
    if prefer:
        headers["Prefer"] = prefer
    headers.update(kw.pop("headers", None) or {})
    timeout = kw.pop("timeout", 10.0)
    try:
        return httpx.request(method.upper(), full, headers=headers, timeout=timeout, **kw)
    except httpx.TransportError as exc:
        raise BackendError("unreachable", f"{method.upper()} {full}: {type(exc).__name__}: {exc}") from exc


# ------------------------------------------------------------------- the backend

class SupabaseBackend(Backend):
    name = "supabase"

    def __init__(self, url: str, service_key: str, timeout_s: float = 10.0):
        if not service_key:
            raise BackendError("config", "SUPABASE_SERVICE_ROLE_KEY is empty")
        self.url = _normalize_url(url)
        self.host = urlsplit(self.url).hostname or ""
        self.timeout_s = float(timeout_s)
        self._client = httpx.Client(
            base_url=self.url + REST_PREFIX,
            headers={**_auth_headers(service_key), "Accept": "application/json"},
            timeout=self.timeout_s,
        )

    # --- plumbing -----------------------------------------------------------
    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "SupabaseBackend":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    def _send(self, method: str, path: str, *, params: dict | None = None, body: Any = None,
              prefer: str | None = None, timeout: float | None = None,
              retry: bool | None = None) -> httpx.Response:
        method = method.upper()
        attempts = 2 if (retry if retry is not None else method == "GET") else 1
        headers: dict[str, str] = {}
        content = None
        if prefer:
            headers["Prefer"] = prefer
        if body is not None:
            headers["Content-Type"] = "application/json"
            try:
                content = _dumps(body)
            except (TypeError, ValueError) as exc:
                raise BackendError("invalid_payload", f"{method} {path}: {exc}") from exc
        context = f"{method} {path}"
        for attempt in range(attempts):
            try:
                resp = self._client.request(method, path, params=params, content=content,
                                            headers=headers, timeout=timeout or self.timeout_s)
            except httpx.TransportError as exc:
                if attempt + 1 < attempts:
                    time.sleep(READ_RETRY_DELAY_S)
                    continue
                code, why = _dns_diagnosis(self.host) if isinstance(exc, httpx.ConnectError) else (
                    "unreachable", "timeout or dropped connection")
                raise BackendError(code, f"{context}: {type(exc).__name__}: {exc}. {why}") from exc
            if resp.status_code >= 500 and resp.status_code != 540 and attempt + 1 < attempts:
                time.sleep(READ_RETRY_DELAY_S)
                continue
            if resp.status_code >= 400:
                raise _error_from_response(resp, context)
            return resp
        raise BackendError("unreachable", f"{context}: no response")  # not reached

    def _select(self, table: str, params: dict) -> list[dict]:
        rows = _json(self._send("GET", f"/{table}", params=params))
        return rows if isinstance(rows, list) else []

    def _select_all(self, table: str, params: dict) -> list[dict]:
        """GET every matching row, paging past the server's max-rows cap. `params` must
        contain an `order` for stable pages."""
        rows: list[dict] = []
        offset = 0
        while True:
            page_params = {**params, "limit": str(PAGE_SIZE), "offset": str(offset)}
            try:
                resp = self._send("GET", f"/{table}", params=page_params, prefer="count=exact")
            except BackendError as exc:
                if exc.code == "http_416" and rows:  # rows vanished between pages: done
                    break
                raise
            page = _json(resp) or []
            rows.extend(page)
            offset += len(page)
            total = _total_from_content_range(resp.headers.get("content-range"))
            if not page:
                break
            if total is not None and offset >= total:
                break
            if total is None and len(page) < PAGE_SIZE:
                break
        return rows

    def _first(self, table: str, params: dict) -> dict | None:
        rows = self._select(table, {**params, "limit": "1"})
        return rows[0] if rows else None

    # --- health -----------------------------------------------------------
    def ping(self) -> None:
        """Tiny query with a short timeout. Raises BackendError('unreachable' | 'paused' |
        'auth' | 'http_<code>'). A paused free-plan project shows up as HTTP 540, as a 5xx
        mentioning 'paused', or as a DNS failure of <ref>.supabase.co while supabase.com
        still resolves (diagnosed in _dns_diagnosis)."""
        self._send("GET", "/sessions", params={"select": "id", "limit": "1"},
                   timeout=min(PING_TIMEOUT_S, self.timeout_s))

    # --- sessions ---------------------------------------------------------
    def get_active_session(self) -> dict | None:
        # temporary sessions of tests/test_rls.py and health-check are never the live session
        return self._first("sessions", {"status": "eq.active", "use_case": "not.in.(rls_test,health_check)",
                                        "order": "created_at.desc"})

    def create_session(self, title: str, use_case: str, questions: list[dict]) -> dict:
        resp = self._send("POST", "/sessions",
                          body={"title": title, "use_case": use_case, "status": "active"},
                          prefer="return=representation")
        rows = _json(resp) or []
        if not rows:
            raise BackendError("insert_failed", "POST /sessions returned no row")
        session = rows[0]
        qrows = [self._question_row(session["id"], q, i) for i, q in enumerate(questions)]
        if qrows:
            try:
                self._send("POST", "/questions", body=qrows, prefer="return=minimal")
            except BackendError:
                # do not leave a half-created active session behind
                try:
                    self._send("DELETE", "/sessions", params={"id": f"eq.{session['id']}"},
                               prefer="return=minimal")
                except BackendError:
                    pass
                raise
        return session

    @staticmethod
    def _question_row(session_id: str, q: dict, index: int) -> dict:
        qtype = q.get("type")
        row = {
            "session_id": session_id,
            "key": q["key"],
            "type": qtype,
            "options": None,
            "min": None,
            "max": None,
            "step": None,
            "position": q.get("position", index),
        }
        if qtype == "choice":
            # accept either plain values or config objects {value, label, icon}
            row["options"] = [str(o["value"]) if isinstance(o, dict) else str(o)
                              for o in (q.get("options") or [])]
        else:
            row["min"], row["max"], row["step"] = q.get("min"), q.get("max"), q.get("step")
        return row

    def end_session(self, session_id: str) -> None:
        self._send("PATCH", "/sessions", params={"id": f"eq.{session_id}"},
                   body={"status": "ended"}, prefer="return=minimal")

    def delete_session(self, session_id: str) -> None:
        """Delete a session and (cascade) its rounds, votes, decisions and events. Test sessions only."""
        self._send("DELETE", "/sessions", params={"id": f"eq.{session_id}"}, prefer="return=minimal")

    def get_questions(self, session_id: str) -> list[dict]:
        return self._select_all("questions", {"session_id": f"eq.{session_id}",
                                              "order": "position.asc,key.asc"})

    # --- rounds -----------------------------------------------------------
    def list_rounds(self, session_id: str) -> list[dict]:
        return self._select_all("rounds", {"session_id": f"eq.{session_id}", "order": "number.asc"})

    def _open_round_row(self, session_id: str) -> dict | None:
        return self._first("rounds", {"session_id": f"eq.{session_id}", "status": "eq.open",
                                      "order": "number.desc"})

    def open_round(self, session_id: str) -> dict:
        """Open the next round with direct table writes (the open_round RPC needs a
        facilitator JWT). The partial unique index rounds_one_open_per_session is the
        final guard: its violation (HTTP 409 / 23505) becomes 'round_already_open'."""
        session = self._first("sessions", {"id": f"eq.{session_id}", "select": "id,status"})
        if session is None:
            raise BackendError("session_not_found", f"no session {session_id}")
        if session.get("status") != "active":
            raise BackendError("session_ended", f"session {session_id} is {session.get('status')}")
        existing = self._open_round_row(session_id)
        if existing:
            raise BackendError("round_already_open", f"round {existing.get('number')} is open")
        for _ in range(3):
            last = self._first("rounds", {"session_id": f"eq.{session_id}", "select": "number",
                                          "order": "number.desc"})
            next_number = int(last["number"]) + 1 if last else 1
            try:
                resp = self._send("POST", "/rounds",
                                  body={"session_id": session_id, "number": next_number, "status": "open"},
                                  prefer="return=representation")
            except BackendError as exc:
                if exc.code != "duplicate":
                    raise
                if "rounds_one_open_per_session" in exc.detail or self._open_round_row(session_id):
                    raise BackendError("round_already_open", exc.detail) from exc
                continue  # round number taken by a concurrent insert: try the next number
            rows = _json(resp) or []
            if rows:
                return rows[0]
            raise BackendError("insert_failed", "POST /rounds returned no row")
        raise BackendError("conflict", "could not allocate a round number (concurrent inserts)")

    def close_round(self, session_id: str) -> dict:
        resp = self._send("PATCH", "/rounds",
                          params={"session_id": f"eq.{session_id}", "status": "eq.open"},
                          body={"status": "closed"},        # closed_at = DB now() (rounds trigger)
                          prefer="return=representation")
        rows = _json(resp) or []
        if not rows:
            raise BackendError("no_open_round", f"session {session_id} has no open round")
        row = rows[0]
        if not row.get("closed_at"):  # trigger missing (older schema): fall back to this clock
            resp = self._send("PATCH", "/rounds", params={"id": f"eq.{row['id']}"},
                              body={"closed_at": _now_iso()}, prefer="return=representation")
            row = (_json(resp) or [row])[0]
        return row

    def get_closed_unprocessed_rounds(self, session_id: str) -> list[dict]:
        return self._select_all("rounds", {"session_id": f"eq.{session_id}", "status": "eq.closed",
                                           "processed_at": "is.null", "order": "number.asc"})

    def mark_round_processed(self, round_id: str) -> None:
        resp = self._send("PATCH", "/rounds", params={"id": f"eq.{round_id}", "select": "id"},
                          body={"processed_at": _now_iso()}, prefer="return=representation")
        if not (_json(resp) or []):
            raise BackendError("round_not_found", f"no round {round_id}")

    # --- votes ------------------------------------------------------------
    def get_votes(self, round_id: str) -> list[dict]:
        return self._select_all("votes", {"round_id": f"eq.{round_id}", "order": "id.asc"})

    def insert_votes(self, rows: list[dict]) -> int:
        """Bulk insert, duplicates ignored (ON CONFLICT DO NOTHING on the unique key; the
        vote trigger also skips repeats). Invalid values or a closed round raise the
        trigger's code (invalid_value, round_closed, ...) for the whole chunk."""
        clean = [{
            "round_id": r["round_id"],
            "participant_id": str(r["participant_id"]),
            "question_key": str(r["question_key"]),
            "value": str(r["value"]),
            "is_simulated": bool(r.get("is_simulated", False)),
        } for r in rows]
        inserted = 0
        for start in range(0, len(clean), INSERT_CHUNK):
            chunk = clean[start:start + INSERT_CHUNK]
            resp = self._send("POST", "/votes",
                              params={"on_conflict": "round_id,participant_id,question_key", "select": "id"},
                              body=chunk,
                              prefer="resolution=ignore-duplicates,return=representation")
            inserted += len(_json(resp) or [])
        return inserted

    def delete_simulated_votes(self, session_id: str | None = None) -> int:
        params = {"is_simulated": "is.true", "select": "id"}
        if session_id is not None:
            round_ids = [r["id"] for r in self.list_rounds(session_id)]
            if not round_ids:
                return 0
            params["round_id"] = _in_list(round_ids)
        resp = self._send("DELETE", "/votes", params=params, prefer="return=representation")
        return len(_json(resp) or [])

    # --- decisions and agent events ----------------------------------------
    def list_decisions(self, session_id: str) -> list[dict]:
        return self._select_all("decisions", {"session_id": f"eq.{session_id}",
                                              "order": "created_at.asc,id.asc"})

    def get_decision_for_round(self, round_id: str) -> dict | None:
        return self._first("decisions", {"round_id": f"eq.{round_id}"})

    def insert_decision(self, row: dict) -> dict:
        """Insert a decision; if the round already has one (decisions.round_id is unique),
        return the existing row. Keys that are not decision columns are dropped;
        session_id / round_number may be omitted (the DB copies them from the round)."""
        if not row.get("round_id"):
            raise BackendError("invalid_payload", "decision row needs round_id")
        body = {k: row[k] for k in DECISION_COLUMNS if k in row}
        resp = self._send("POST", "/decisions", params={"on_conflict": "round_id"}, body=body,
                          prefer="resolution=ignore-duplicates,return=representation")
        rows = _json(resp) or []
        if rows:
            return rows[0]
        existing = self.get_decision_for_round(row["round_id"])
        if existing is None:
            raise BackendError("insert_failed", f"decision for round {row['round_id']} was not stored")
        return existing

    def insert_agent_event(self, row: dict) -> None:
        """Insert one trace event; a repeated (round_id, seq) is ignored."""
        body = {k: row.get(k) for k in AGENT_EVENT_COLUMNS}
        body["tool"] = str(body.get("tool") or "event")[:TOOL_MAX_CHARS]
        if body.get("summary") is not None:
            body["summary"] = str(body["summary"])[:SUMMARY_MAX_CHARS]
        self._send("POST", "/agent_events", params={"on_conflict": "round_id,seq"}, body=body,
                   prefer="resolution=ignore-duplicates,return=minimal")

    def delete_agent_events(self, round_id: str) -> None:
        self._send("DELETE", "/agent_events", params={"round_id": f"eq.{round_id}"},
                   prefer="return=minimal")
