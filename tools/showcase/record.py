r"""Record the Plurarch showcase video: one real round, end to end, in one 1920x1080 frame.

    C:\Python314\python.exe tools\showcase\record.py            (needs Pillow + numpy for the Revit highlight)
    options: --no-revit (skip Revit), --keep-revit (leave the voted design in Revit), --n 46 (simulated crowd)

What is real: the participant page (index.html), the facilitator console and stage, the orchestrator in
local mode, the headless Claude Code reviewer and its stream, the design-mcp tools and the Revit model
(revit/LangfordA_Plurarch.rvt must be the ACTIVE document in Revit 2027). What is simulated: the other
voters (`orchestrator.py simulate --profile showcase`). The page tools/showcase/web/showcase.html lays it
out; this script drives it over CDP in its own headless Edge, records it with Page.startScreencast,
and builds the MP4 with ffmpeg (waiting stretches are sped up, and the page says so on screen).

Everything runs against a separate state folder (state/showcase), so the online setup is not touched.
Afterwards the session is reset and Revit goes back to the as-built design (unless --keep-revit).
"""
from __future__ import annotations

import argparse
import base64
import itertools
import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
import urllib.request
from datetime import datetime
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "orchestrator"))
sys.path.insert(0, str(REPO))
from supabase_checks import MiniWebSocket  # noqa: E402

LOCAL = json.loads((REPO / "config" / "local.json").read_text(encoding="utf-8"))
VENV_PY = LOCAL.get("python") or sys.executable
STATE = REPO / "state" / "showcase"
IMG = STATE / "showcase"            # served at /showcase/revit/<name>.png by the local server
FRAMES = STATE / "frames"
OUT_DIR = REPO / "media"
PORT = 8790
CDP_PORT = 9351
EDGE = r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"
VIEW_NAME = "Plurarch Showcase SE high"
FFMPEG = shutil.which("ffmpeg") or str(Path(os.environ.get("LOCALAPPDATA", "")) / "Microsoft/WinGet/Packages/"
                                       "Gyan.FFmpeg.Essentials_Microsoft.Winget.Source_8wekyb3d8bbwe/"
                                       "ffmpeg-8.1.1-essentials_build/bin/ffmpeg.exe")


try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass


def log(msg: str) -> None:
    print(f"[{datetime.now():%H:%M:%S}] {msg}", flush=True)


# ------------------------------------------------------------------ CDP with a reader thread
class CDP:
    """One page target. A reader thread answers commands and saves screencast frames (acking each one,
    so Chrome keeps sending while the main thread sleeps)."""

    def __init__(self, port: int, profile: Path):
        with socket.socket() as probe:
            if probe.connect_ex(("127.0.0.1", port)) == 0:
                raise RuntimeError(f"port {port} is already in use")
        shutil.rmtree(profile, ignore_errors=True)
        self.profile = profile
        self.proc = subprocess.Popen([
            EDGE, "--headless=new", "--use-angle=swiftshader", "--enable-unsafe-swiftshader", "--enable-webgl",
            "--ignore-gpu-blocklist", "--no-first-run", "--no-default-browser-check", "--disable-extensions",
            "--hide-scrollbars", "--force-device-scale-factor=1", "--window-size=1920,1080",
            f"--user-data-dir={profile}", f"--remote-debugging-port={port}", "about:blank"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        page = None
        for _ in range(120):
            try:
                targets = json.loads(urllib.request.urlopen(f"http://127.0.0.1:{port}/json", timeout=1).read())
                page = next(t for t in targets if t.get("type") == "page")
                break
            except Exception:
                time.sleep(0.25)
        if not page:
            self.close()
            raise RuntimeError("Edge did not expose a page target")
        self.ws = MiniWebSocket(page["webSocketDebuggerUrl"], timeout=30)
        self.lock = threading.Lock()
        self.mid = 0
        self.waiting: dict[int, list] = {}
        self.frames: list[tuple[float, str]] = []
        self.recording = False
        self.logs: list[str] = []
        self.alive = True
        self.ack_ids = itertools.count(10_000_000)
        self.reader = threading.Thread(target=self._read, daemon=True)
        self.reader.start()

    def _send(self, obj: dict) -> None:
        with self.lock:
            self.ws.send(obj)

    def _read(self) -> None:
        while self.alive:
            try:
                m = self.ws.recv(None)
            except Exception:
                break
            if not m:
                continue
            if "id" in m and m["id"] in self.waiting:
                slot = self.waiting[m["id"]]
                slot[1] = m
                slot[0].set()
                continue
            meth = m.get("method")
            if meth == "Page.screencastFrame":
                p = m["params"]
                t = time.time()
                if self.recording:
                    path = FRAMES / f"f_{len(self.frames):06d}.jpg"
                    try:
                        path.write_bytes(base64.b64decode(p["data"]))
                        self.frames.append((t, path.name))
                    except OSError:
                        pass
                try:
                    self._send({"id": next(self.ack_ids), "method": "Page.screencastFrameAck",
                                "params": {"sessionId": p["sessionId"]}})
                except Exception:
                    break
            elif meth == "Runtime.exceptionThrown":
                d = m["params"]["exceptionDetails"]
                self.logs.append("EXCEPTION " + str(d.get("exception", {}).get("description", d.get("text")))[:300])

    def cmd(self, method: str, timeout: float = 60, **params):
        with self.lock:
            self.mid += 1
            mid = self.mid
        ev = threading.Event()
        self.waiting[mid] = [ev, None]
        self._send({"id": mid, "method": method, "params": params})
        if not ev.wait(timeout):
            self.waiting.pop(mid, None)
            raise TimeoutError(method)
        m = self.waiting.pop(mid)[1]
        if "error" in m:
            raise RuntimeError(f"{method}: {m['error']}")
        return m.get("result", {})

    def js(self, expr: str, timeout: float = 30):
        r = self.cmd("Runtime.evaluate", timeout=timeout, expression=expr, returnByValue=True, awaitPromise=True)
        if r.get("exceptionDetails"):
            raise RuntimeError("JS: " + str(r["exceptionDetails"].get("exception", {}).get("description",
                                                                                         r["exceptionDetails"].get("text")))[:400])
        return r.get("result", {}).get("value")

    def close(self) -> None:
        self.alive = False
        try:
            self.ws.sock.close()
        except Exception:
            pass
        subprocess.run(["taskkill", "/PID", str(self.proc.pid), "/T", "/F"], capture_output=True)
        marker = str(self.profile).replace("'", "''")
        subprocess.run(["powershell", "-NoProfile", "-Command",
                        "Get-CimInstance Win32_Process -Filter \"name='msedge.exe'\" | Where-Object { $_.CommandLine -and "
                        f"$_.CommandLine.Contains('{marker}') }} | ForEach-Object {{ Stop-Process -Id $_.ProcessId -Force "
                        "-ErrorAction SilentlyContinue }"], capture_output=True)


# ------------------------------------------------------------------ the recording surface
class Show:
    def __init__(self, cdp: CDP):
        self.c = cdp
        self.speed_marks: list[tuple[float, float]] = []

    def sc(self, call: str):
        return self.c.js(f"(() => {{ const r = SC.{call}; return r === undefined ? null : r; }})()")

    def status(self) -> dict:
        return self.c.js("typeof SC === 'undefined' ? {} : SC.status()") or {}

    def rect(self, frame: str, sel: str, **opts):
        return self.c.js(f"SC.rect({json.dumps(frame)}, {json.dumps(sel)}, {json.dumps(opts)})")

    def speed(self, f: float) -> None:
        self.speed_marks.append((time.time(), f))
        self.sc(f"speed({f})")

    def caption(self, n: int, title: str, text: str) -> None:
        self.sc(f"caption({n}, {json.dumps(title)}, {json.dumps(text)})")

    def mode(self, m: str) -> None:
        self.sc(f"mode({json.dumps(m)})")

    # phone: a real touch at the element, with a visible fingertip
    def tap(self, sel: str, wait: float = 0.9, **opts) -> bool:
        r = self.rect("phone", sel, **opts)
        if not r:
            return False
        self.sc(f"finger({r['x']}, {r['y']})")
        time.sleep(0.12)
        self.c.cmd("Input.dispatchTouchEvent", type="touchStart", touchPoints=[{"x": r["x"], "y": r["y"]}])
        time.sleep(0.06)
        self.c.cmd("Input.dispatchTouchEvent", type="touchEnd", touchPoints=[])
        time.sleep(wait)
        return True

    def drag_phone(self, x0, y0, dx, dy, steps=24):
        self.c.cmd("Input.dispatchTouchEvent", type="touchStart", touchPoints=[{"x": x0, "y": y0}])
        for i in range(1, steps + 1):
            self.c.cmd("Input.dispatchTouchEvent", type="touchMove",
                       touchPoints=[{"x": x0 + dx * i / steps, "y": y0 + dy * i / steps}])
            time.sleep(0.03)
        self.c.cmd("Input.dispatchTouchEvent", type="touchEnd", touchPoints=[])

    # console: a visible pointer moves to the element, then a real mouse click
    def click(self, frame: str, sel: str, travel: float = 1.0, after: float = 0.8) -> bool:
        r = self.rect(frame, sel)
        if not r:
            return False
        self.sc(f"cursor({r['x']}, {r['y']}, true)")
        time.sleep(travel)
        self.sc("press()")
        for typ in ("mouseMoved", "mousePressed", "mouseReleased"):
            self.c.cmd("Input.dispatchMouseEvent", type=typ, x=r["x"], y=r["y"], button="left", clickCount=1)
        time.sleep(after)
        return True

    def wait_for(self, fn, timeout: float, step: float = 0.4):
        t_end = time.time() + timeout
        while time.time() < t_end:
            v = fn()
            if v:
                return v
            time.sleep(step)
        return None


# ------------------------------------------------------------------ orchestrator + Revit helpers
def orch(*args, wait=True, log_to=None):
    env = {**os.environ, "PLURARCH_STATE_DIR": str(STATE), "PYTHONUTF8": "1"}
    cmd = [VENV_PY, str(REPO / "orchestrator" / "orchestrator.py"), "--backend", "local", *args]
    if wait:
        r = subprocess.run(cmd, env=env, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=300)
        if r.returncode != 0:
            raise RuntimeError(f"orchestrator {' '.join(args)} failed:\n{r.stdout[-1500:]}\n{r.stderr[-1500:]}")
        return r.stdout
    out = open(log_to, "a", encoding="utf-8") if log_to else subprocess.DEVNULL
    return subprocess.Popen(cmd, env=env, stdout=out, stderr=subprocess.STDOUT,
                            creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0)


def revit_ready() -> tuple[bool, str]:
    from design_mcp import revit_apply as ra, revit_client as rc
    if not rc.health():
        return False, "the Revit add-in is not reachable (open Revit 2027 with revit/LangfordA_Plurarch.rvt)"
    ok, info, reason = ra.guard()
    return ok, reason or str(info)


def revit_view_id() -> int | None:
    from design_mcp import revit_client as rc
    views = rc.data(rc.call("get_views")) or {}
    for v in (views.get("views") if isinstance(views, dict) else views) or []:
        if v.get("name") == VIEW_NAME:
            return int(v["id"])
    return None


def revit_image(view_id: int, name: str) -> Path:
    from design_mcp import revit_client as rc
    env = rc.call("get_view_image", {"viewId": view_id, "pixelSize": 1800}, timeout=30)
    d = rc.data(env) or {}
    if not d.get("imageBase64"):
        raise RuntimeError(f"Revit returned no image: {str(env)[:300]}")
    p = IMG / f"{name}.png"
    p.write_bytes(base64.b64decode(d["imageBase64"]))
    return p


def revit_diff(before: Path, after: Path, out: Path) -> float:
    """Orange where the two Revit renders differ (dilated a little), transparent elsewhere."""
    import numpy as np
    from PIL import Image, ImageFilter
    a = np.asarray(Image.open(before).convert("RGB")).astype(int)
    b = np.asarray(Image.open(after).convert("RGB")).astype(int)
    if a.shape != b.shape:
        return 0.0
    m = (np.abs(a - b).sum(2) > 40)
    core = Image.fromarray((m * 255).astype("uint8")).filter(ImageFilter.MaxFilter(5))
    glow = core.filter(ImageFilter.MaxFilter(13)).filter(ImageFilter.GaussianBlur(6))
    alpha = np.maximum(np.asarray(core).astype(float) * 0.82, np.asarray(glow).astype(float) * 0.35).astype("uint8")
    rgba = np.zeros((*m.shape, 4), dtype="uint8")
    rgba[..., 0], rgba[..., 1], rgba[..., 2], rgba[..., 3] = 255, 159, 67, alpha
    Image.fromarray(rgba, "RGBA").save(out)
    return float(m.mean())


def raw_tool_result(stream: Path, tool: str) -> dict | None:
    """The parsed JSON result of the last call of `tool` in an agent stream."""
    names, found = {}, None
    for raw in stream.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            ev = json.loads(raw)
        except ValueError:
            continue
        for c in (ev.get("message") or {}).get("content", []) if isinstance(ev.get("message"), dict) else []:
            if not isinstance(c, dict):
                continue
            if c.get("type") == "tool_use":
                names[c.get("id")] = c.get("name", "").replace("mcp__design__", "")
            elif c.get("type") == "tool_result" and names.get(c.get("tool_use_id")) == tool:
                content = c.get("content")
                text = "".join(x.get("text", "") for x in content if isinstance(x, dict)) if isinstance(content, list) else str(content)
                try:
                    found = json.loads(text)
                except ValueError:
                    pass
    return found


def revit_items(rv: dict, as_built_glazed: int = 142) -> list[dict]:
    plan, ch = rv.get("plan") or {}, rv.get("changes") or {}
    items = []
    if ch.get("panels_retyped"):
        to = "solid panels" if (plan.get("se_glazed") or as_built_glazed) < as_built_glazed else "glass"
        items.append({"text": f"SE studio façade: {ch['panels_retyped']} lites switched to {to} "
                              f"({plan.get('se_glazed')} of {plan.get('se_n')} stay glazed)"})
    if ch.get("fins_created"):
        items.append({"text": f"{ch['fins_created']} concrete sunshade fins created, {plan.get('fin_depth')} m deep"})
    if ch.get("fins_deleted"):
        items.append({"text": f"{ch['fins_deleted']} old fins removed"})
    if plan.get("lanterns_open") is not None and plan.get("lanterns_open") != 12:
        items.append({"text": f"Roof: {plan['lanterns_open']} of 12 skylight lanterns stay open"})
    if ch.get("material"):
        items.append({"text": f"Infill finish: {plan.get('material')} (the solid panel type; the penthouse louvres share it)"})
    if rv.get("applied"):
        items.append({"text": f"{rv.get('ops')} edits in one Revit transaction, read back and "
                              f"{'verified ✓' if rv.get('verified') else 'NOT verified'}", "check": True})
    return items


# ------------------------------------------------------------------ video
def build_video(frames: list[tuple[float, str]], marks: list[tuple[float, float]], out: Path) -> None:
    def vt_factory():
        segs = sorted(marks)

        def speed_at(t):
            f = 1.0
            for ts, sf in segs:
                if ts <= t:
                    f = sf
            return f
        return speed_at
    speed_at = vt_factory()
    lines, vt_total = [], 0.0
    for i, (t, name) in enumerate(frames):
        if i + 1 < len(frames):
            real = max(0.0, frames[i + 1][0] - t)
        else:
            real = 1.0
        d = real / speed_at(t)
        vt_total += d
        lines.append(f"file '{(FRAMES / name).as_posix()}'\nduration {d:.4f}")
    lines.append(f"file '{(FRAMES / frames[-1][1]).as_posix()}'")
    lst = STATE / "frames.txt"
    lst.write_text("\n".join(lines) + "\n", encoding="utf-8")
    log(f"encoding {len(frames)} frames → {vt_total:.0f} s of video")
    r = subprocess.run([FFMPEG, "-y", "-hide_banner", "-loglevel", "error", "-f", "concat", "-safe", "0", "-i", str(lst),
                        "-vf", "fps=30,format=yuv420p", "-c:v", "libx264", "-preset", "medium", "-crf", "18",
                        "-movflags", "+faststart", str(out)], capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError("ffmpeg failed: " + r.stderr[-1500:])


# ------------------------------------------------------------------ the scenario
def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-revit", action="store_true")
    ap.add_argument("--keep-revit", action="store_true")
    ap.add_argument("--n", type=int, default=46)
    ap.add_argument("--seed", type=int, default=11)
    args = ap.parse_args()

    STATE.mkdir(parents=True, exist_ok=True)
    IMG.mkdir(parents=True, exist_ok=True)
    shutil.rmtree(FRAMES, ignore_errors=True)
    FRAMES.mkdir(parents=True)
    OUT_DIR.mkdir(exist_ok=True)
    for old in IMG.glob("*.png"):
        old.unlink()

    use_revit = not args.no_revit
    view_id = None
    if use_revit:
        ok, why = revit_ready()
        if not ok:
            sys.exit(f"STOP: Revit is not ready: {why}  (or run with --no-revit)")
        view_id = revit_view_id()
        if not view_id:
            sys.exit(f"STOP: the Revit view '{VIEW_NAME}' is missing")
    else:
        os.environ["PLURARCH_REVIT"] = "off"

    log("fresh session (and Revit back to as-built)")
    print(orch("reset-session", "--yes", "--title", "Plurarch · live demo"))
    orch_log = STATE / "orchestrator.log"
    orch_log.write_text("", encoding="utf-8")
    runner = orch("run", "--port", str(PORT), wait=False, log_to=orch_log)
    cdp = None
    try:
        for _ in range(60):
            try:
                urllib.request.urlopen(f"http://127.0.0.1:{PORT}/api/status", timeout=1).read()
                break
            except Exception:
                time.sleep(0.5)
        else:
            raise RuntimeError("the local server did not start; see " + str(orch_log))
        key = (STATE / "facilitator_key.txt").read_text(encoding="utf-8").strip()
        if use_revit:
            revit_image(view_id, "revit_before")
            log("Revit before image exported")

        cdp = CDP(CDP_PORT, STATE / "edge-profile")
        cdp.cmd("Page.enable")
        cdp.cmd("Runtime.enable")
        cdp.cmd("Emulation.setDeviceMetricsOverride", width=1920, height=1080, deviceScaleFactor=1, mobile=False)
        cdp.cmd("Emulation.setTouchEmulationEnabled", enabled=True, maxTouchPoints=5)
        cdp.cmd("Page.navigate", url=f"http://localhost:{PORT}/showcase/showcase.html#key={key}")
        show = Show(cdp)
        time.sleep(2)
        st = show.wait_for(lambda: (lambda s: s if s.get("console") and s.get("phone3d") and s.get("pins", 0) >= 1 else None)(show.status()), 90)
        if not st:
            st = show.status()
            log(f"warning: page not fully ready: {st}")
        if use_revit:
            show.sc("revitBefore('/showcase/revit/revit_before.png')")
        time.sleep(3)  # let the 3D model and fonts settle

        # ---------------------------------------------------------------- recording starts
        cdp.recording = True
        cdp.cmd("Page.startScreencast", format="jpeg", quality=86, maxWidth=1920, maxHeight=1080, everyNthFrame=1)
        t_start = time.time()
        show.speed(1)
        when = datetime.now().strftime("%d %b %Y")
        show.sc("card('title', true, " + json.dumps({"fine": f"Recorded live on {when}. Real phone page, real dashboard, real Claude Code run, "
                                                             f"real Revit model. Only the other {args.n} voters are simulated."}) + ")")
        time.sleep(7.5)
        show.sc("card('title', false)")
        time.sleep(1.0)

        # 1 · the facilitator opens a round; the phone shows the purpose first
        show.caption(1, "Phones join with a QR code",
                     "No app and no account. The page explains the purpose in one paragraph, then waits for the facilitator.")
        time.sleep(1.5)
        if show.tap("#info-btn", wait=4.5):
            show.tap(".sheet.open .sheet-done", wait=1.0)
        show.caption(1, "The facilitator opens a round",
                     "One click on the dashboard. Every phone in the room switches to the vote within a few seconds.")
        time.sleep(1.2)
        show.click("console", "#round-btn", travel=1.2)
        show.sc("clock(Date.now())")
        show.sc("cursor(null, null, false)")
        show.wait_for(lambda: (show.status().get("go") or None), 20)
        time.sleep(1.5)

        # 1 · this phone votes
        show.caption(1, "Each phone votes on four changes, pinned to the building",
                     "Tap a pin, pick an answer. This person keeps the concrete finish, trims the SE glass to 90 % "
                     "and asks for 0.3 m sunshade fins.")
        r = show.rect("phone", "canvas")
        if r:
            show.drag_phone(r["x"] + 60, r["y"] + 40, -110, 0)
            time.sleep(1.2)

        def answer(key: str, action: str | None):
            opened = show.tap(f'.v3d-pin[data-q="{key}"]', wait=1.3) or show.tap("#go-3d", wait=1.3)
            if not opened:
                return
            if action == "choice":
                show.tap('.sheet.open label.tile:has(input[value="concrete"])', wait=1.0)
            elif action in ("Less", "More"):
                show.tap(f'.sheet.open .round-btn[aria-label^="{action}"]', wait=1.1)
            else:
                time.sleep(1.2)
            show.tap(".sheet.open .sheet-done", wait=1.0)

        answer("infill_finish", "choice")
        answer("se_glass_share", "Less")
        answer("fin_depth", "More")
        answer("skylights_open", None)
        for _ in range(3):  # anything left unanswered, then "Submit vote"
            go = (show.status().get("go") or "")
            if go.startswith("Submit"):
                show.tap("#go-3d", wait=2.0)
                break
            if go.startswith("Next"):
                show.tap("#go-3d", wait=1.3)
                show.tap(".sheet.open .sheet-done", wait=1.0)
        time.sleep(1.5)

        # 2 · the rest of the room
        show.caption(2, "The rest of the room votes at the same time",
                     f"{args.n} more phones (simulated for this recording). The dashboard fills in live: "
                     "how many took part and how the answers spread for each question.")
        show.speed(3)
        sim = orch("simulate", "--profile", "showcase", "--n", str(args.n), "--over", "36", "--seed", str(args.seed),
                   wait=False, log_to=STATE / "simulate.log")
        for i in range(2, 5):
            time.sleep(6.5)
            show.click("console", f"#seg-questions button:nth-child({i})", travel=0.9, after=0.3)
        while sim.poll() is None:
            time.sleep(0.5)
        show.click("console", "#seg-questions button:nth-child(1)", travel=0.9, after=0.3)
        time.sleep(4)
        show.speed(1)

        # 3 · close
        show.caption(3, "Voting closes: the tally becomes one proposal",
                     "The winner of each choice and the median of each slider. The orchestrator sends that proposal "
                     "to the reviewer agent.")
        time.sleep(1.5)
        t_close = time.time()
        show.click("console", "#round-btn", travel=1.2)
        show.sc("cursor(null, null, false)")
        show.sc(f"termStart({t_close - 2:.1f})")
        show.sc("clock(Date.now(), 'since voting closed')")
        time.sleep(2.5)

        # 4 · the dashboard shows the agent working
        show.caption(4, "Claude Code wakes up as the reviewer",
                     "A headless Claude Code process starts for this round. The dashboard streams every tool call it makes.")
        show.sc("scrollTo('console', '#agent', 'center')")
        time.sleep(9)
        show.sc("scrollTo('console', '#trace', 'start')")
        time.sleep(7)

        # 5 · the terminal: the real process
        show.mode("terminal")
        show.sc("preloadStage()")
        show.caption(5, "Inside the reviewer: the real Claude Code run",
                     "It reads the brief, scores the proposal, checks the hard rules and tests alternatives. "
                     "No shell, no files, no web: only the six design tools.")
        time.sleep(4)
        show.speed(2)
        agent_timeout = float(json.loads((REPO / "config" / "use_cases" / "live_presentation.json").read_text(encoding="utf-8"))
                              .get("agent_timeout_s", 120)) + 30
        saw_set = show.wait_for(lambda: show.c.js("SC.term.sawSet || SC.term.done"), agent_timeout, step=0.5)
        show.speed(1)
        time.sleep(4)
        meta = json.loads((STATE / "agent_runs" / "latest.json").read_text(encoding="utf-8"))
        stream = STATE / "agent_runs" / meta["stream"]

        # 6 · Revit
        set_out = raw_tool_result(stream, "set_parameters") or {}
        rv = set_out.get("revit") if isinstance(set_out, dict) else None
        if use_revit and saw_set and isinstance(rv, dict) and rv.get("applied"):
            show.mode("revit")
            show.caption(6, "The decision is written into the real Revit model",
                         "set_parameters turns the decision into element edits on Langford A: panel types, new fin walls "
                         "and the finish material, then reads them back to verify.")
            show.sc("revitState('As built · before the vote', '')")
            time.sleep(3.5)
            show.sc("revitState('Applying the decision through the Revit API…', 'busy')")
            show.sc("revitList(" + json.dumps(revit_items(rv)) + ", 'Written to the Revit model')")
            after = revit_image(view_id, "revit_after")
            changed = revit_diff(IMG / "revit_before.png", after, IMG / "revit_diff.png")
            log(f"Revit after image exported; {changed:.1%} of pixels changed")
            time.sleep(2.5)
            show.sc("revitAfter('/showcase/revit/revit_after.png', '/showcase/revit/revit_diff.png')")
            time.sleep(3.2)
            show.sc("revitState('After the vote · read back from Revit', 'done')")
            time.sleep(1.5)
            show.sc("revitDiff(true)")
            time.sleep(8)
        elif use_revit:
            log(f"no Revit apply to show (saw_set={saw_set}, revit={str(rv)[:200]})")

        # back to the terminal for the verdict
        show.mode("terminal")
        show.caption(5, "The reviewer explains its decision",
                     "Kept, adjusted or rejected, always with a reason the room can read. The record goes back to the orchestrator.")
        show.speed(2)
        show.wait_for(lambda: show.c.js("SC.term.done && SC.term.queue.length === 0"), 90, step=0.5)
        show.speed(1)
        time.sleep(5)

        # 7 · result everywhere
        show.mode("console")
        show.caption(7, "Everyone sees the verdict and the reason",
                     "The dashboard, the projector and every phone show what changed and why, in plain words.")
        show.wait_for(lambda: show.rect("console", "#decision-card h2, #decision-card .verdict, #decision-card"), 20)
        show.sc("scrollTo('console', '#decision-card', 'start')")
        time.sleep(7)
        show.mode("stage")
        show.caption(7, "On the projector",
                     "The stage view shows the decision large for the room, next to the live model.")
        time.sleep(8)

        # end card
        dec = None
        try:
            st = json.loads(urllib.request.urlopen(urllib.request.Request(
                f"http://127.0.0.1:{PORT}/api/console/state", headers={"Authorization": f"Bearer {key}"}), timeout=5).read())
            dec = (st.get("decisions") or [None])[-1] if st.get("decisions") else None
            part = st.get("participants_total")
        except Exception:
            part = None
        stats = [[str(part or (meta.get("participation") or {}).get("participants", "?")), "people voted (1 real phone in this recording)"],
                 [str((meta.get("participation") or {}).get("votes", "?")), "votes on 4 questions"],
                 [str((dec or {}).get("verdict") or "?"), "the reviewer’s verdict, with its reason"],
                 [f"{(dec or {}).get('duration_s', '?')} s", "Claude Code review, including the Revit write"]]
        line = (f"Revit: {rv.get('ops')} element edits in {rv.get('s', '?')} s, verified by reading the model back."
                if isinstance(rv, dict) and rv.get("applied") else "")
        show.sc("card('end', true, " + json.dumps({"stats": stats, "line": line,
                                                   "fine": "Plurarch · faroshad.github.io/plurarch · the design metrics are indicative proxies"}) + ")")
        time.sleep(9)
        cdp.cmd("Page.stopScreencast")
        cdp.recording = False
        log(f"recorded {len(cdp.frames)} frames in {time.time() - t_start:.0f} s")
        if cdp.logs:
            log("page exceptions: " + " | ".join(cdp.logs[:5]))
        (STATE / "timeline.json").write_text(json.dumps({"frames": cdp.frames, "speed": show.speed_marks}), encoding="utf-8")
        out = OUT_DIR / f"Plurarch_showcase_{datetime.now():%Y%m%d_%H%M}.mp4"
        build_video(cdp.frames, show.speed_marks, out)
        log(f"VIDEO: {out}")
    finally:
        if cdp:
            cdp.close()
        try:
            runner.send_signal(signal.CTRL_BREAK_EVENT) if os.name == "nt" else runner.terminate()
            runner.wait(timeout=10)
        except Exception:
            subprocess.run(["taskkill", "/PID", str(runner.pid), "/T", "/F"], capture_output=True)
        if not args.keep_revit:
            log("reset: session closed, Revit back to as-built")
            try:
                print(orch("reset-session", "--yes", "--title", "Plurarch · live demo"))
            except Exception as e:
                log(f"reset failed: {e}")


if __name__ == "__main__":
    main()
