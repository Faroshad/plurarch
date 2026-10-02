"""Tiny client for the KenLP RevitMCPServer add-in (the HTTP endpoint the Revit MCP bridge uses).

    call(command, params, dry=False, timeout=20)   -> envelope {"ok": bool, "data": ..., "error": {...}}
    batch(steps, stop_on_error=True, dry=False)   -> one Revit transaction for all steps
    health(timeout=3)                              -> True if the add-in answers /health

Command names are the MCP tool names without the "revit_" prefix (e.g. "change_element_type").
The add-in listens on 127.0.0.1:<7891 + (version - 2026)> (Revit 2027 -> 7892) and needs the bearer
token Revit writes to %APPDATA%\\Autodesk\\Revit\\Addins\\<version>\\revit-mcp-token.txt (re-read on 401,
Revit makes a new one on restart). Every request has a hard timeout; nothing here can hang a caller.

Environment: PLURARCH_REVIT_VERSION (default 2027), PLURARCH_REVIT_PORT (default from the version).
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
import uuid

VERSION = os.environ.get("PLURARCH_REVIT_VERSION", "2027")
PORT = int(os.environ.get("PLURARCH_REVIT_PORT") or (7891 + int(VERSION) - 2026))
BASE = f"http://127.0.0.1:{PORT}"
MAX_TIMEOUT_S = 20.0


class RevitUnavailable(Exception):
    """The add-in did not answer (Revit closed, add-in not loaded, busy past the timeout)."""


def token_path() -> str:
    return os.path.join(os.environ.get("APPDATA", ""), "Autodesk", "Revit", "Addins", VERSION, "revit-mcp-token.txt")


def _token() -> str | None:
    try:
        with open(token_path(), "r", encoding="utf-8") as f:
            return f.read().strip() or None
    except OSError:
        return None


def _post(path: str, body: dict, timeout: float) -> dict:
    timeout = max(1.0, min(float(timeout), MAX_TIMEOUT_S))
    data = json.dumps(body).encode("utf-8")
    for attempt in (0, 1):
        tok = _token()
        headers = {"content-type": "application/json", "x-request-id": str(uuid.uuid4())}
        if tok:
            headers["authorization"] = "Bearer " + tok
        req = urllib.request.Request(BASE + path, data=data, method="POST", headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                env = json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            try:
                env = json.loads(e.read().decode("utf-8"))
            except Exception:  # noqa: BLE001
                env = {"ok": False, "error": {"code": f"http_{e.code}", "message": str(e)}}
            if e.code == 401 and attempt == 0:
                continue  # Revit restarted: re-read the token once
        except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as e:
            raise RevitUnavailable(f"Revit add-in not reachable at {BASE} ({getattr(e, 'reason', e)})") from e
        except ValueError as e:
            raise RevitUnavailable(f"Revit add-in sent a non-JSON answer ({e})") from e
        if isinstance(env, dict) and (env.get("error") or {}).get("code") == "unauthorized" and attempt == 0:
            continue
        return env if isinstance(env, dict) else {"ok": False, "error": {"code": "bad_response", "message": str(env)[:200]}}
    return env


def call(command: str, params: dict | None = None, dry: bool = False, timeout: float = MAX_TIMEOUT_S) -> dict:
    body = {"command": command, "params": params or {}}
    if dry:
        body["dryRun"] = True
    return _post("/mcp", body, timeout)


def batch(steps: list[dict], stop_on_error: bool = True, dry: bool = False, timeout: float = MAX_TIMEOUT_S) -> dict:
    body = {"stopOnError": stop_on_error, "steps": steps}
    if dry:
        body["dryRun"] = True
    return _post("/mcp/batch", body, timeout)


def health(timeout: float = 3.0) -> bool:
    try:
        with urllib.request.urlopen(BASE + "/health", timeout=timeout) as r:
            return r.status == 200
    except Exception:  # noqa: BLE001
        return False


def data(env: dict):
    """The payload of an envelope (some answers put it under "data", some at the top level)."""
    if not isinstance(env, dict):
        return None
    return env["data"] if "data" in env else env


def error_text(env: dict) -> str:
    err = (env or {}).get("error") or {}
    return f"{err.get('code', 'error')}: {err.get('message', '')}".strip(": ") if err else "unknown error"
