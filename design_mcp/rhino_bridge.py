"""Run a script inside Rhino 8 through the RhinoMCP plugin's socket (127.0.0.1:1999; start it in Rhino with
the `mcpstart` command). Standard library only.

Protocol (observed with RhinoMCP for Rhino 8): send one JSON object
{"type": "execute_rhinoscript_python_code", "params": {"code": "..."}}, read until the reply parses as JSON:
{"status": "success", "result": {"success": bool, "output": "<everything the script printed>"}}.
Results are passed back between @@PLX@@ and @@END@@ markers in the printed output.
"""
from __future__ import annotations

import json
import re
import socket

HOST, PORT = "127.0.0.1", 1999


class RhinoError(RuntimeError):
    pass


def available(timeout: float = 1.0) -> bool:
    try:
        with socket.create_connection((HOST, PORT), timeout=timeout):
            return True
    except OSError:
        return False


def run_code(code: str, timeout: float = 240.0) -> str:
    try:
        s = socket.create_connection((HOST, PORT), timeout=timeout)
    except OSError as e:
        raise RhinoError(f"Rhino is not reachable on {HOST}:{PORT} (open Rhino 8 and run mcpstart): {e}")
    with s:
        s.settimeout(timeout)
        s.sendall(json.dumps({"type": "execute_rhinoscript_python_code", "params": {"code": code}}).encode("utf-8"))
        buf = b""
        while True:
            chunk = s.recv(1 << 20)
            if not chunk:
                break
            buf += chunk
            try:
                reply = json.loads(buf.decode("utf-8"))
                break
            except ValueError:
                continue
        else:
            reply = None
    if not isinstance(reply, dict):
        raise RhinoError("no reply from Rhino")
    res = reply.get("result") or {}
    if reply.get("status") != "success" or not res.get("success", True):
        raise RhinoError(str(res.get("message") or res.get("output") or reply)[:600])
    return str(res.get("output", ""))


def run_file(path: str, args: dict, timeout: float = 240.0) -> dict:
    """exec a Rhino-side script with PLX=args. The script writes its JSON result to args["out"] (a temp file),
    so Rhino's command line only shows a short line; older scripts print it between the markers instead."""
    import os
    import tempfile
    fd, out_path = tempfile.mkstemp(prefix="plurarch-rhino-", suffix=".json")
    os.close(fd)
    args = {**args, "out": out_path}
    code = ("import io\n"
            f"__plx_ns = {{'PLX': {args!r}, '__name__': 'plurarch'}}\n"
            f"exec(io.open(r'{path}', encoding='utf-8').read(), __plx_ns)\n")
    try:
        out = run_code(code, timeout)
        try:
            text = open(out_path, encoding="utf-8").read().strip()
        except OSError:
            text = ""
        if text:
            return json.loads(text)
        m = re.search(r"@@PLX@@(.*?)@@END@@", out, re.S)
        if not m:
            raise RhinoError("the Rhino script returned no result: " + out[-600:])
        return json.loads(m.group(1))
    finally:
        try:
            os.unlink(out_path)
        except OSError:
            pass
