r"""Screen-record the Plurarch showcase: real windows, the real mouse cursor, real time.

    C:\Python314\python.exe tools\showcase\record_screen.py              record (about 3-4 minutes)
    C:\Python314\python.exe tools\showcase\record_screen.py --rehearse   set up + check cursor mapping, no recording
    options: --keep-revit (leave the voted design in Revit), --n 46 (simulated voters)

It TAKES OVER THE SCREEN AND THE MOUSE while it runs: do not touch mouse or keyboard, and turn on
Windows "Do not disturb" so no notification lands in the video. Revit 2027 must have
revit/LangfordA_Plurarch.rvt open.

Layout (logical px on a 1920x1080 desktop):
  voting + result   Chrome "facilitator console" 0..1280  |  Edge app window "phone" 1280..1920
  review            Windows Terminal 0..960 (orchestrator above, live Claude Code stream below)  |  Revit 960..1920
The mouse moves with SetCursorPos and clicks/scrolls with SendInput, so what you see is what happens.
Element positions come from the pages over CDP (Chrome runs with its own blank profile and a debugging port).
ffmpeg records the desktop with Desktop Duplication (ddagrab, cursor included) and encodes on the GPU.
Only the other voters are simulated (orchestrator.py simulate --profile showcase).
"""
from __future__ import annotations

import argparse
import ctypes
import ctypes.wintypes as W
import json
import math
import os
import random
import shutil
import socket
import subprocess
import sys
import time
import urllib.request
from datetime import datetime
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "orchestrator"))
sys.path.insert(0, str(REPO))
from supabase_checks import MiniWebSocket  # noqa: E402
import showcase_feed  # noqa: E402

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

LOCAL = json.loads((REPO / "config" / "local.json").read_text(encoding="utf-8"))
VENV_PY = LOCAL.get("python") or sys.executable
STATE = REPO / "state" / "showcase"
OUT_DIR = REPO / "media"
PORT = 8790
CDP_PORT = 9352
CHROME = r"C:\Program Files\Google\Chrome\Application\chrome.exe"
# A blank, isolated profile: no sign-in, no sync, no bookmarks (Edge signs in with the Windows account).
BROWSER_PROFILE = STATE / "chrome-screen-profile"
VIEW_NAME = "Plurarch Showcase SE high"
ROOF_VIEW_NAME = "Plurarch Showcase roof"
RHINO_ENGINE = REPO / "rhino" / "plurarch_daylight.py"
FFMPEG = shutil.which("ffmpeg") or str(Path(os.environ.get("LOCALAPPDATA", "")) / "Microsoft/WinGet/Packages/"
                                       "Gyan.FFmpeg.Essentials_Microsoft.Winget.Source_8wekyb3d8bbwe/"
                                       "ffmpeg-8.1.1-essentials_build/bin/ffmpeg.exe")


def log(msg: str) -> None:
    print(f"[{datetime.now():%H:%M:%S}] {msg}", flush=True)


# ------------------------------------------------------------------ Win32
U = ctypes.windll.user32
U.SetWindowPos.argtypes = [W.HWND, W.HWND, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, W.UINT]
U.SetWindowPos.restype = W.BOOL
U.GetWindowThreadProcessId.argtypes = [W.HWND, ctypes.POINTER(W.DWORD)]
try:
    U.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))  # per-monitor v2: every coordinate is physical
except Exception:
    U.SetProcessDPIAware()
SCALE = U.GetDpiForSystem() / 96.0
SCREEN_W, SCREEN_H = U.GetSystemMetrics(0), U.GetSystemMetrics(1)
WORK = W.RECT()
U.SystemParametersInfoW(0x0030, 0, ctypes.byref(WORK), 0)  # SPI_GETWORKAREA (physical)

HWND_TOP, HWND_TOPMOST, HWND_NOTOPMOST = 0, -1, -2
SWP_NOMOVE, SWP_NOSIZE, SWP_SHOWWINDOW, SWP_NOACTIVATE = 0x2, 0x1, 0x40, 0x10
SW_RESTORE, SW_MAXIMIZE = 9, 3


class MOUSEINPUT(ctypes.Structure):
    _fields_ = [("dx", W.LONG), ("dy", W.LONG), ("mouseData", W.DWORD), ("dwFlags", W.DWORD),
                ("time", W.DWORD), ("dwExtraInfo", ctypes.c_size_t)]


class INPUT(ctypes.Structure):
    class _U(ctypes.Union):
        _fields_ = [("mi", MOUSEINPUT), ("pad", ctypes.c_byte * 32)]
    _anonymous_ = ("u",)
    _fields_ = [("type", W.DWORD), ("u", _U)]


def _mouse(flags: int, data: int = 0) -> None:
    inp = INPUT(type=0)
    inp.mi = MOUSEINPUT(0, 0, ctypes.c_ulong(data & 0xFFFFFFFF).value, flags, 0, 0)
    U.SendInput(1, ctypes.byref(inp), ctypes.sizeof(INPUT))


def cursor_pos() -> tuple[int, int]:
    p = W.POINT()
    U.GetCursorPos(ctypes.byref(p))
    return p.x, p.y


def move_to(x: float, y: float, dur: float | None = None) -> None:
    """Glide the real cursor to physical (x, y) on a gentle curve, like a hand on a mouse."""
    x0, y0 = cursor_pos()
    dist = math.hypot(x - x0, y - y0)
    if dur is None:
        dur = min(1.1, 0.35 + dist / (SCALE * 1400))
    steps = max(8, int(dur * 90))
    bend = random.uniform(-0.12, 0.12) * dist
    nx, ny = (-(y - y0) / dist, (x - x0) / dist) if dist else (0, 0)
    for i in range(1, steps + 1):
        t = i / steps
        e = t * t * (3 - 2 * t)
        arc = math.sin(math.pi * e) * bend
        U.SetCursorPos(int(x0 + (x - x0) * e + nx * arc), int(y0 + (y - y0) * e + ny * arc))
        time.sleep(dur / steps)
    U.SetCursorPos(int(x), int(y))


def click(x: float, y: float, dur: float | None = None, pause: float = 0.18) -> None:
    move_to(x, y, dur)
    time.sleep(pause)
    _mouse(0x0002)  # left down
    time.sleep(0.07)
    _mouse(0x0004)  # left up


def drag(x0, y0, x1, y1, dur=1.0) -> None:
    move_to(x0, y0)
    time.sleep(0.15)
    _mouse(0x0002)
    steps = int(dur * 90)
    for i in range(1, steps + 1):
        t = i / steps
        e = t * t * (3 - 2 * t)
        U.SetCursorPos(int(x0 + (x1 - x0) * e), int(y0 + (y1 - y0) * e))
        time.sleep(dur / steps)
    time.sleep(0.05)
    _mouse(0x0004)


def wheel(notches: int, gap: float = 0.16) -> None:
    for _ in range(abs(notches)):
        _mouse(0x0800, -120 if notches > 0 else 120)
        time.sleep(gap)


def windows() -> list[tuple[int, str, str]]:
    out = []
    proto = ctypes.WINFUNCTYPE(W.BOOL, W.HWND, W.LPARAM)

    def cb(h, _):
        if U.IsWindowVisible(h):
            n = U.GetWindowTextLengthW(h)
            buf = ctypes.create_unicode_buffer(n + 1)
            U.GetWindowTextW(h, buf, n + 1)
            cls = ctypes.create_unicode_buffer(256)
            U.GetClassNameW(h, cls, 256)
            out.append((h, buf.value, cls.value))
        return True
    U.EnumWindows(proto(cb), 0)
    return out


def find_window(title_part: str, cls: str | None = None, timeout: float = 20) -> int | None:
    t_end = time.time() + timeout
    while time.time() < t_end:
        for h, t, c in windows():
            if title_part in t and (cls is None or c == cls):
                return h
        time.sleep(0.3)
    return None


def _frame_margins(h) -> tuple[int, int, int, int]:
    """Invisible resize borders: window rect minus the visible (DWM) frame."""
    r, f = W.RECT(), W.RECT()
    U.GetWindowRect(h, ctypes.byref(r))
    ctypes.windll.dwmapi.DwmGetWindowAttribute(h, 9, ctypes.byref(f), ctypes.sizeof(f))  # EXTENDED_FRAME_BOUNDS
    return f.left - r.left, f.top - r.top, r.right - f.right, r.bottom - f.bottom


def place(h, x, y, w, hgt, top: bool = True) -> None:
    """Put a window's VISIBLE frame at logical (x, y, w, h) inside the work area."""
    if U.IsZoomed(h) or U.IsIconic(h):
        U.ShowWindow(h, SW_RESTORE)
        time.sleep(0.3)
    px, py, pw, ph = [int(round(v * SCALE)) for v in (x, y, w, hgt)]
    U.SetWindowPos(h, HWND_TOP, px, py, pw, ph, SWP_SHOWWINDOW | SWP_NOACTIVATE)
    time.sleep(0.15)
    ml, mt, mr, mb = _frame_margins(h)
    U.SetWindowPos(h, HWND_TOP, px - ml, py - mt, pw + ml + mr, ph + mt + mb, SWP_SHOWWINDOW | SWP_NOACTIVATE)
    if top:
        raise_window(h)


def raise_window(h) -> None:
    """Bring a window to the front, like clicking it in the taskbar (attach to the foreground thread's
    input so Windows lets this background process hand the focus over)."""
    flags = SWP_NOMOVE | SWP_NOSIZE | SWP_SHOWWINDOW | SWP_NOACTIVATE
    U.SetWindowPos(h, HWND_TOPMOST, 0, 0, 0, 0, flags)
    U.SetWindowPos(h, HWND_NOTOPMOST, 0, 0, 0, 0, flags)
    k = ctypes.windll.kernel32
    fg = U.GetForegroundWindow()
    fg_tid = U.GetWindowThreadProcessId(fg, None) if fg else 0
    me = k.GetCurrentThreadId()
    if fg_tid and fg_tid != me:
        U.AttachThreadInput(me, fg_tid, True)
    U.BringWindowToTop(h)
    U.SetForegroundWindow(h)
    if fg_tid and fg_tid != me:
        U.AttachThreadInput(me, fg_tid, False)


def window_pid(h) -> int:
    pid = W.DWORD()
    U.GetWindowThreadProcessId(h, ctypes.byref(pid))
    return pid.value


def pids_with(*parts: str) -> set[int]:
    cond = " -and ".join(f"$_.CommandLine.Contains('{p}')" for p in parts)
    r = subprocess.run(["powershell", "-NoProfile", "-Command",
                        f"Get-CimInstance Win32_Process | Where-Object {{ $_.CommandLine -and {cond} }} | "
                        "ForEach-Object { $_.ProcessId }"], capture_output=True, text=True)
    return {int(x) for x in r.stdout.split() if x.strip().isdigit()}


def find_own_window(title_part: str, pids: set[int], timeout: float = 20) -> int | None:
    t_end = time.time() + timeout
    while time.time() < t_end:
        for h, t, c in windows():
            if title_part in t and c == "Chrome_WidgetWin_1" and window_pid(h) in pids:
                return h
        time.sleep(0.3)
        pids = pids | pids_with("chrome.exe", str(BROWSER_PROFILE))
    return None


class Placement(ctypes.Structure):
    _fields_ = [("length", W.UINT), ("flags", W.UINT), ("showCmd", W.UINT), ("ptMin", W.POINT),
                ("ptMax", W.POINT), ("rc", W.RECT)]


def get_placement(h) -> Placement:
    p = Placement()
    p.length = ctypes.sizeof(p)
    U.GetWindowPlacement(h, ctypes.byref(p))
    return p


# ------------------------------------------------------------------ Edge pages over CDP
class Page:
    def __init__(self, ws_url: str):
        self.ws = MiniWebSocket(ws_url, timeout=30)
        self.mid = 0

    def js(self, expr: str, timeout: float = 20):
        self.mid += 1
        self.ws.send({"id": self.mid, "method": "Runtime.evaluate",
                      "params": {"expression": expr, "returnByValue": True, "awaitPromise": True}})
        t_end = time.time() + timeout
        while time.time() < t_end:
            m = self.ws.recv(timeout)
            if m is None:
                break
            if m.get("id") == self.mid:
                r = m.get("result", {})
                if r.get("exceptionDetails"):
                    return None
                return r.get("result", {}).get("value")
        raise TimeoutError(expr[:60])


def cdp_page(url_part: str, timeout: float = 30) -> Page:
    t_end = time.time() + timeout
    while time.time() < t_end:
        try:
            targets = json.loads(urllib.request.urlopen(f"http://127.0.0.1:{CDP_PORT}/json", timeout=2).read())
            for t in targets:
                if t.get("type") == "page" and url_part in t.get("url", ""):
                    return Page(t["webSocketDebuggerUrl"])
        except Exception:
            pass
        time.sleep(0.4)
    raise RuntimeError(f"no Edge page with {url_part!r}")


GEOM_JS = """(() => ({ sx: screenX, sy: screenY, ow: outerWidth, oh: outerHeight, iw: innerWidth, ih: innerHeight,
                     dpr: devicePixelRatio, mm: window.__mm || null }))()"""
HOOK_JS = "window.__mm = null; addEventListener('mousemove', e => { window.__mm = [e.clientX, e.clientY]; }, true); true"


class Win:
    """An Edge window: page coordinates → physical screen pixels, calibrated with a real mouse move."""

    def __init__(self, page: Page, hwnd: int, name: str):
        self.page, self.hwnd, self.name = page, hwnd, name
        self.fix = (0.0, 0.0)

    def origin(self) -> tuple[float, float, float]:
        g = self.page.js(GEOM_JS)
        border = (g["ow"] - g["iw"]) / 2
        ox = g["sx"] + border
        oy = g["sy"] + g["oh"] - g["ih"] - border
        return ox, oy, g["dpr"] / SCALE  # CSS px per DIP (page zoom)

    def to_screen(self, cx: float, cy: float) -> tuple[int, int]:
        ox, oy, zoom = self.origin()
        return (int((ox + cx * zoom + self.fix[0]) * SCALE), int((oy + cy * zoom + self.fix[1]) * SCALE))

    def calibrate(self, cx: float, cy: float) -> tuple[float, float]:
        self.page.js(HOOK_JS)
        self.fix = (0.0, 0.0)
        for _ in range(2):
            x, y = self.to_screen(cx, cy)
            move_to(x + 6, y + 6, 0.25)
            move_to(x, y, 0.15)
            time.sleep(0.25)
            mm = self.page.js("window.__mm")
            if not mm:
                break
            _, _, zoom = self.origin()
            self.fix = (self.fix[0] + (cx - mm[0]) * zoom, self.fix[1] + (cy - mm[1]) * zoom)
        return self.fix

    def rect(self, sel: str, frame: bool = False) -> dict | None:
        """Centre of an element in page coordinates (frame=True: inside the phone iframe)."""
        js = """((sel, inFrame) => {
          let doc = document, ox = 0, oy = 0;
          if (inFrame) { const f = document.querySelector('iframe'); if (!f) return null;
            const fr = f.getBoundingClientRect(); ox = fr.left; oy = fr.top; doc = f.contentDocument; }
          const n = doc && doc.querySelector(sel); if (!n) return null;
          const r = n.getBoundingClientRect(); if (!r.width || !r.height) return null;
          const cs = (inFrame ? document.querySelector('iframe').contentWindow : window).getComputedStyle(n);
          if (cs.visibility === 'hidden' || cs.display === 'none' || +cs.opacity < 0.2) return null;
          return { x: ox + r.left + r.width / 2, y: oy + r.top + r.height / 2, top: r.top, bottom: r.bottom, h: r.height };
        })(%s, %s)""" % (json.dumps(sel), "true" if frame else "false")
        return self.page.js(js)

    def click(self, sel: str, frame: bool = False, dur: float | None = None, after: float = 0.8) -> bool:
        r = self.rect(sel, frame)
        if not r:
            return False
        click(*self.to_screen(r["x"], r["y"]), dur=dur)
        time.sleep(after)
        return True

    def hover(self, sel: str, frame: bool = False, dur: float | None = None) -> bool:
        r = self.rect(sel, frame)
        if not r:
            return False
        move_to(*self.to_screen(r["x"], r["y"]), dur=dur)
        return True

    def scroll_to(self, sel: str, top_at: float = 90, max_notches: int = 30) -> None:
        """Real wheel notches over the page until `sel` sits near the top."""
        for _ in range(max_notches):
            r = self.rect(sel)
            if not r or r["top"] <= top_at + 40:
                break
            wheel(1, 0.0)
            time.sleep(0.2)
        time.sleep(0.4)


def phone_status(win: Win) -> dict:
    return win.page.js("""(() => { const f = document.querySelector('iframe'); const d = f && f.contentDocument;
      if (!d) return {};
      const go = d.getElementById('go-3d'); const sh = d.querySelector('.sheet.open');
      return { is3d: d.documentElement.classList.contains('is-3d'), pins: d.querySelectorAll('.v3d-pin').length,
               go: go ? go.textContent.trim() : null, sheet: sh ? sh.dataset.kind : null,
               reveal: !!d.querySelector('.reveal-card'), text: (d.body.innerText || '').slice(0, 100) }; })()""") or {}


def wait_for(fn, timeout: float, step: float = 0.4):
    t_end = time.time() + timeout
    while time.time() < t_end:
        try:
            v = fn()
        except Exception:
            v = None
        if v:
            return v
        time.sleep(step)
    return None


# ------------------------------------------------------------------ processes
def orch_env() -> dict:
    return {**os.environ, "PLURARCH_STATE_DIR": str(STATE), "PYTHONUTF8": "1"}


def orch(*args) -> str:
    r = subprocess.run([VENV_PY, str(REPO / "orchestrator" / "orchestrator.py"), "--backend", "local", *args],
                       env=orch_env(), capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=300)
    if r.returncode != 0:
        raise RuntimeError(f"orchestrator {' '.join(args)} failed:\n{r.stdout[-1200:]}\n{r.stderr[-1200:]}")
    return r.stdout


def write_cmd(name: str, body: str) -> Path:
    d = Path(os.environ.get("TEMP", str(STATE))) / "plurarch_showcase"   # no spaces: Windows Terminal re-quotes
    d.mkdir(parents=True, exist_ok=True)
    p = d / name
    p.write_text("@echo off\r\nchcp 65001 >nul\r\n"
                 f"set PLURARCH_STATE_DIR={STATE}\r\nset PYTHONUTF8=1\r\nset PLURARCH_HIDE_KEY=1\r\ncd /d \"{REPO}\"\r\n{body}\r\n", encoding="utf-8")
    return p


def kill_matching(*parts: str) -> None:
    cond = " -and ".join(f"$_.CommandLine.Contains('{p}')" for p in parts)
    subprocess.run(["powershell", "-NoProfile", "-Command",
                    f"Get-CimInstance Win32_Process | Where-Object {{ $_.CommandLine -and {cond} }} | "
                    "ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }"],
                   capture_output=True)


def server_up() -> bool:
    try:
        urllib.request.urlopen(f"http://127.0.0.1:{PORT}/api/status", timeout=1).read()
        return True
    except Exception:
        return False


def revit_hwnd() -> int | None:
    return find_window("Autodesk Revit 2027", timeout=3)


# ------------------------------------------------------------------ main
def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--rehearse", action="store_true")
    ap.add_argument("--keep-revit", action="store_true")
    ap.add_argument("--n", type=int, default=46)
    ap.add_argument("--scenario", choices=["facade", "skylights"], default="facade",
                    help="facade: fins rule (MODIFIED); skylights: Rhino daylight GA places the voted roof glass")
    ap.add_argument("--seed", type=int, default=11)
    args = ap.parse_args()
    random.seed(7)
    STATE.mkdir(parents=True, exist_ok=True)
    OUT_DIR.mkdir(exist_ok=True)
    log(f"screen {SCREEN_W}x{SCREEN_H} physical, scale {SCALE}, work area {WORK.right}x{WORK.bottom}")
    lw, lh = SCREEN_W / SCALE, WORK.bottom / SCALE  # logical work area (1920 x 1032)

    from design_mcp import revit_apply as ra, revit_client as rc
    if not rc.health():
        sys.exit("STOP: Revit is not reachable (open Revit 2027 with revit/LangfordA_Plurarch.rvt)")
    ok, _, why = ra.guard()
    if not ok:
        sys.exit(f"STOP: {why}")
    views = (rc.data(rc.call("get_views")) or {}).get("views") or []
    want_view = ROOF_VIEW_NAME if args.scenario == "skylights" else VIEW_NAME
    view_id = next((int(v["id"]) for v in views if v.get("name") == want_view), None)
    if not view_id:
        sys.exit(f"STOP: the Revit view '{want_view}' is missing")
    rh_h = None
    if args.scenario == "skylights":
        from design_mcp import rhino_bridge
        if not rhino_bridge.available():
            sys.exit("STOP: Rhino 8 is not reachable (open Rhino and run mcpstart)")
        rhino_bridge.run_file(str(RHINO_ENGINE), {"repo": str(REPO), "action": "preview", "budget": 112})
        rh_h = find_window("Rhino Viewport", "AfxFrameOrView140u", timeout=10)   # the study's floating viewport
        if not rh_h:
            sys.exit("STOP: the Rhino study viewport did not open")
    rv_h = revit_hwnd()
    if not rv_h:
        sys.exit("STOP: no Revit window")
    rv_place = get_placement(rv_h)

    log("fresh session; Revit back to as-built")
    orch("reset-session", "--yes", "--title", "Plurarch · live demo")
    rc.call("open_view", {"viewId": view_id})
    for f in (STATE / "agent_runs").glob("latest.json"):
        f.unlink()

    # terminal: orchestrator above, live Claude Code stream below
    kill_matching("orchestrator.py", f"--port {PORT}")
    run_cmd = write_cmd("run_orchestrator.cmd",
                        f'"{VENV_PY}" orchestrator\\orchestrator.py --backend local run --port {PORT}')
    feed_cmd = write_cmd("run_feed.cmd", f'"{VENV_PY}" orchestrator\\showcase_feed.py --follow "{STATE}"')
    subprocess.Popen(["wt.exe", "-w", "new", "new-tab", "--title", "Plurarch · orchestrator", "--suppressApplicationTitle",
                      "cmd", "/c", str(run_cmd), ";", "split-pane", "-H", "-s", "0.58", "--title",
                      "Plurarch · Claude Code reviewer", "--suppressApplicationTitle", "cmd", "/c", str(feed_cmd)])
    if not wait_for(server_up, 40):
        sys.exit("STOP: the orchestrator's local server did not start")
    wt_h = find_window("Plurarch ·", "CASCADIA_HOSTING_WINDOW_CLASS", 20)
    key = (STATE / "facilitator_key.txt").read_text(encoding="utf-8").strip()

    # Chrome: console (normal window with its address bar) + phone (app window), own blank profile
    kill_matching("chrome.exe", str(BROWSER_PROFILE))
    time.sleep(0.5)
    shutil.rmtree(BROWSER_PROFILE, ignore_errors=True)
    flags = [f"--user-data-dir={BROWSER_PROFILE}", f"--remote-debugging-port={CDP_PORT}", "--no-first-run",
             "--no-default-browser-check", "--disable-extensions", "--hide-crash-restore-bubble",
             "--disable-session-crashed-bubble", "--disable-features=msEdgeSidebarV2,msUndersideButton"]
    subprocess.Popen([CHROME, *flags, "--new-window", f"http://localhost:{PORT}/console.html#key={key}"])
    con_page = cdp_page("/console.html")
    subprocess.Popen([CHROME, *flags, f"--app=http://localhost:{PORT}/showcase/phone.html"])
    ph_page = cdp_page("/showcase/phone.html")
    own = pids_with("chrome.exe", str(BROWSER_PROFILE))
    con_h = find_own_window("Plurarch · facilitator console", own)
    ph_h = find_own_window("Plurarch · phone", own)
    if not (con_h and ph_h and wt_h):
        sys.exit(f"STOP: windows not found (console {con_h}, phone {ph_h}, terminal {wt_h})")

    # stack: terminal + Revit behind, console + phone in front; together they cover the whole work area
    split = 860  # review layout: terminal | Revit
    place(wt_h, 0, 0, split, lh)
    if rh_h:
        place(rh_h, split, 0, lw - split, lh)
    place(rv_h, split, 0, lw - split, lh)
    time.sleep(1.0)
    EL = json.loads((REPO / "config" / "langford" / "elements.json").read_text(encoding="utf-8"))
    if args.scenario == "skylights":
        zoom_ids = [i for l in EL["skylights_open"]["lanterns"] if 18 <= l["centroid"][0] <= 44 for i in l["glazing_ids"]]
    else:
        zoom_ids = [p["id"] for p in EL["se_glass_share"]["panels"] if 17 <= p["center"][0] <= 47]  # middle bays
    rc.call("zoom_to_elements", {"ids": zoom_ids})  # frame the changing elements in the resized window
    place(con_h, 0, 0, lw * 2 / 3, lh)
    place(ph_h, lw * 2 / 3, 0, lw / 3, lh)
    con, ph = Win(con_page, con_h, "console"), Win(ph_page, ph_h, "phone")
    wait_for(lambda: con_page.js("!document.getElementById('app').hidden"), 30)
    wait_for(lambda: (lambda s: s.get("is3d") and s.get("pins", 0) >= 4)(phone_status(ph)), 60)
    time.sleep(3)
    log(f"calibration: console {con.calibrate(400, 300)}, phone {ph.calibrate(30, 30)}")
    move_to(SCREEN_W * 0.45, SCREEN_H * 0.55, 0.4)

    if args.rehearse:
        for sel, frame, w in (("#round-btn", False, con), ("#info-btn", True, ph), ('.v3d-pin[data-q="fin_depth"]', True, ph)):
            ok = w.hover(sel, frame, 0.7)
            log(f"rehearse hover {sel}: {ok}")
            time.sleep(1.2)
        shot = STATE / "rehearsal_layout.png"
        subprocess.run([FFMPEG, "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi", "-i",
                        "ddagrab=output_idx=0:framerate=5:draw_mouse=1", "-frames:v", "1", "-vf",
                        "hwdownload,format=bgra,scale=1920:1080", str(shot)])
        raise_window(wt_h)
        raise_window(rv_h)
        time.sleep(1.5)
        subprocess.run([FFMPEG, "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi", "-i",
                        "ddagrab=output_idx=0:framerate=5:draw_mouse=1", "-frames:v", "1", "-vf",
                        "hwdownload,format=bgra,scale=1920:1080", str(STATE / "rehearsal_review.png")])
        log("rehearsal done (nothing recorded); layout frames saved")
        return_after = True
    else:
        return_after = False

    rec = None
    out4k = OUT_DIR / f"Plurarch_screen_{datetime.now():%Y%m%d_%H%M}_4k.mp4"
    try:
        if not return_after:
            rec = subprocess.Popen([FFMPEG, "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi", "-i",
                                    "ddagrab=output_idx=0:framerate=30:draw_mouse=1",
                                    "-c:v", "h264_nvenc", "-preset", "p6", "-cq", "19", "-b:v", "0",
                                    "-movflags", "+faststart", str(out4k)],
                                   stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=open(STATE / "ffmpeg.log", "w"))
            time.sleep(2.0)
            if args.scenario == "skylights":
                scenario_skylights(con, ph, wt_h, rv_h, rh_h, args)
            else:
                scenario(con, ph, wt_h, rv_h, args)
            time.sleep(1.0)
    finally:
        if rec:
            try:
                rec.stdin.write(b"q")
                rec.stdin.flush()
                rec.wait(timeout=30)
            except Exception:
                rec.kill()
        log("teardown")
        kill_matching("chrome.exe", str(BROWSER_PROFILE))
        kill_matching("showcase_feed.py", "--follow")
        kill_matching("orchestrator.py", f"--port {PORT}")
        time.sleep(1.0)
        if wt_h and U.IsWindow(wt_h):
            U.PostMessageW(wt_h, 0x0010, 0, 0)  # WM_CLOSE
        if not args.keep_revit:
            try:
                orch("reset-session", "--yes", "--title", "Plurarch · live demo")
                log("Revit back to as-built")
            except Exception as e:
                log(f"reset failed: {e}")
        U.SetWindowPlacement(rv_h, ctypes.byref(rv_place))
        if not args.rehearse:  # the recording showed this run's console; retire its facilitator key
            (STATE / "facilitator_key.txt").unlink(missing_ok=True)
    if rec and out4k.exists():
        out1080 = out4k.with_name(out4k.name.replace("_4k", "_1080p"))
        subprocess.run([FFMPEG, "-hide_banner", "-loglevel", "error", "-y", "-i", str(out4k), "-vf",
                        "scale=1920:1080:flags=lanczos", "-c:v", "h264_nvenc", "-preset", "p6", "-cq", "20", "-b:v", "0",
                        "-movflags", "+faststart", str(out1080)])
        log(f"VIDEO 4K: {out4k}")
        log(f"VIDEO 1080p: {out1080}")


def scenario(con: Win, ph: Win, wt_h: int, rv_h: int, args) -> None:
    t0 = time.time()
    log("recording")
    time.sleep(1.5)

    # the phone: what is this?
    ph.click("#info-btn", frame=True, after=4.5)
    ph.click(".sheet.open .sheet-done", frame=True, after=1.0)

    # facilitator opens the round
    con.click("#round-btn", after=0.5)
    wait_for(lambda: phone_status(ph).get("go"), 25)
    time.sleep(1.2)

    # this phone votes: turn the model a little, then answer at the pins
    r = ph.rect("canvas", frame=True)
    if r:
        x, y = ph.to_screen(r["x"] + 40, r["y"] + 60)
        drag(x, y, x - int(90 * SCALE), y + int(6 * SCALE), 1.2)
        time.sleep(1.0)

    def answer(key: str, action: str | None):
        if not (ph.click(f'.v3d-pin[data-q="{key}"]', frame=True, after=1.3) or ph.click("#go-3d", frame=True, after=1.3)):
            return
        if action == "choice":
            ph.click('.sheet.open label.tile:has(input[value="concrete"])', frame=True, after=1.0)
        elif action:
            ph.click(f'.sheet.open .round-btn[aria-label^="{action}"]', frame=True, after=1.1)
        else:
            time.sleep(1.4)
        ph.click(".sheet.open .sheet-done", frame=True, after=1.0)

    answer("infill_finish", "choice")
    answer("se_glass_share", "Less")
    answer("fin_depth", "More")
    answer("skylights_open", None)
    for _ in range(3):
        go = phone_status(ph).get("go") or ""
        if go.startswith("Submit"):
            ph.click("#go-3d", frame=True, after=2.0)
            break
        if go.startswith("Next"):
            ph.click("#go-3d", frame=True, after=1.3)
            ph.click(".sheet.open .sheet-done", frame=True, after=1.0)

    # the rest of the room votes (simulated); the facilitator looks through the questions
    sim = subprocess.Popen([VENV_PY, str(REPO / "orchestrator" / "orchestrator.py"), "--backend", "local", "simulate",
                            "--profile", "showcase", "--n", str(args.n), "--over", "30", "--seed", str(args.seed)],
                           env=orch_env(), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    time.sleep(3)
    for i in (2, 3, 4, 1):
        con.click(f"#seg-questions button:nth-child({i})", after=0.4)
        con.hover("#heatmap", dur=0.8)
        time.sleep(4.5)
    while sim.poll() is None:
        time.sleep(0.4)
    time.sleep(2.5)

    # close: the tally goes to the reviewer
    t_close = time.time()
    con.click("#round-btn", after=1.0)
    con.hover("#agent", dur=0.6)
    time.sleep(1.5)
    con.scroll_to("#trace", top_at=120)

    # switch to the review as soon as the agent is scoring (before it writes to Revit)
    def agent_lines():
        meta = showcase_feed.latest(STATE)
        if not meta or float(meta.get("started") or 0) < t_close - 1:
            return None
        lines, _, done = showcase_feed.lines_from(STATE / "agent_runs" / meta["stream"])
        return lines, done
    wait_for(lambda: (lambda r: r and (r[1] or any(L.get("tool") == "evaluate" for L in r[0])))(agent_lines()), 40, 0.25)
    raise_window(wt_h)
    raise_window(rv_h)
    move_to(SCREEN_W * 0.22, SCREEN_H * 0.70, 0.8)
    wait_for(lambda: (lambda r: r and r[1])(agent_lines()), 150, 0.4)
    time.sleep(2.0)
    move_to(SCREEN_W * 0.80, SCREEN_H * 0.50, 1.0)   # look at the new fins in Revit
    time.sleep(4.0)
    move_to(SCREEN_W * 0.22, SCREEN_H * 0.82, 0.9)   # and the verdict in the terminal
    time.sleep(4.0)

    # back to the console and the phone: everyone sees the result
    raise_window(con.hwnd)
    raise_window(ph.hwnd)
    time.sleep(0.8)
    move_to(*con.to_screen(640, 560), 0.7)
    wait_for(lambda: ph.rect(".reveal-card", frame=True), 12)
    con.scroll_to("#decision-card", top_at=110)
    time.sleep(4.0)
    ph.hover(".reveal-card", frame=True, dur=0.9)
    time.sleep(3.5)
    con.click("#stage-link", after=0.5)                # the projector view
    time.sleep(8.0)
    log(f"scenario took {time.time() - t0:.0f} s")



def vote_on_phone(ph: Win, answers: list) -> None:
    """answers: (question key, 'choice' / 'Less' / 'More' / None, how many taps)."""
    for key, action, taps in answers:
        if not (ph.click(f'.v3d-pin[data-q="{key}"]', frame=True, after=1.3) or ph.click("#go-3d", frame=True, after=1.3)):
            continue
        if action == "choice":
            ph.click('.sheet.open label.tile:has(input[value="concrete"])', frame=True, after=1.0)
        elif action:
            for _ in range(taps):
                ph.click(f'.sheet.open .round-btn[aria-label^="{action}"]', frame=True, after=0.7)
            time.sleep(0.4)
        else:
            time.sleep(1.2)
        ph.click(".sheet.open .sheet-done", frame=True, after=1.0)
    for _ in range(3):
        go = phone_status(ph).get("go") or ""
        if go.startswith("Submit"):
            ph.click("#go-3d", frame=True, after=2.0)
            return
        if go.startswith("Next"):
            ph.click("#go-3d", frame=True, after=1.3)
            ph.click(".sheet.open .sheet-done", frame=True, after=1.0)


def scenario_skylights(con: Win, ph: Win, wt_h: int, rv_h: int, rh_h, args) -> None:
    """Round: the room cuts roof glass to 8 lanterns' worth; the reviewer asks Rhino where that glass should go."""
    t0 = time.time()
    log("recording (skylights)")
    time.sleep(1.5)
    con.click("#round-btn", after=0.5)
    wait_for(lambda: phone_status(ph).get("go"), 25)
    time.sleep(1.0)
    r = ph.rect("canvas", frame=True)
    if r:
        x, y = ph.to_screen(r["x"] + 40, r["y"] + 60)
        drag(x, y, x - int(70 * SCALE), y + int(4 * SCALE), 1.0)
        time.sleep(0.8)
    vote_on_phone(ph, [("infill_finish", "choice", 1), ("se_glass_share", "Less", 1),
                       ("fin_depth", "More", 2), ("skylights_open", "Less", 2)])
    sim = subprocess.Popen([VENV_PY, str(REPO / "orchestrator" / "orchestrator.py"), "--backend", "local", "simulate",
                            "--profile", "skylight_cut", "--n", str(args.n), "--over", "24", "--seed", str(args.seed)],
                           env=orch_env(), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    time.sleep(2.5)
    for i in (4, 1):                                    # the roof question, then back to the finish
        con.click(f"#seg-questions button:nth-child({i})", after=0.4)
        con.hover("#heatmap", dur=0.8)
        time.sleep(6.0)
    while sim.poll() is None:
        time.sleep(0.4)
    con.click("#seg-questions button:nth-child(4)", after=0.4)
    time.sleep(3.0)

    t_close = time.time()
    con.click("#round-btn", after=1.0)
    con.hover("#agent", dur=0.6)

    def agent_lines():
        meta = showcase_feed.latest(STATE)
        if not meta or float(meta.get("started") or 0) < t_close - 1:
            return None
        lines, _, done = showcase_feed.lines_from(STATE / "agent_runs" / meta["stream"])
        return lines, done

    def saw(kind, tool):
        return lambda: (lambda r: r and (r[1] or any(L.get("k") == kind and L.get("tool") == tool for L in r[0])))(agent_lines())

    # the terminal + Rhino as soon as the agent is working; Rhino draws the study live when it is called
    wait_for(saw("call", "evaluate"), 40, 0.25)
    raise_window(wt_h)
    if rh_h:
        raise_window(rh_h)
    move_to(SCREEN_W * 0.22, SCREEN_H * 0.70, 0.8)
    wait_for(saw("call", "optimise_skylight_layout"), 60, 0.25)
    move_to(SCREEN_W * 0.70, SCREEN_H * 0.55, 1.0)      # watch the rays, the heat map and the GA in Rhino
    wait_for(saw("result", "optimise_skylight_layout"), 120, 0.3)
    time.sleep(0.8)
    raise_window(rv_h)                                  # Revit, before the agent writes the layout
    move_to(SCREEN_W * 0.72, SCREEN_H * 0.45, 0.9)
    wait_for(lambda: (lambda r: r and r[1])(agent_lines()), 120, 0.4)
    time.sleep(2.0)
    move_to(SCREEN_W * 0.22, SCREEN_H * 0.80, 0.9)      # the verdict and the cited simulation numbers
    time.sleep(5.0)
    if rh_h:                                            # one more look at the study's final readout
        raise_window(rh_h)
        time.sleep(3.0)
        raise_window(rv_h)
        time.sleep(2.0)

    raise_window(con.hwnd)
    raise_window(ph.hwnd)
    time.sleep(0.8)
    move_to(*con.to_screen(640, 560), 0.7)
    wait_for(lambda: ph.rect(".reveal-card", frame=True), 12)
    con.scroll_to("#decision-card", top_at=110)
    time.sleep(3.0)
    if ph.click('.v3d-pin[data-q="skylights_open"]', frame=True, after=4.5):   # the phone flies to the lanterns
        ph.click(".sheet.open .sheet-done", frame=True, after=1.0)
    time.sleep(1.5)
    con.click("#stage-link", after=0.5)
    time.sleep(8.0)
    log(f"scenario took {time.time() - t0:.0f} s")


if __name__ == "__main__":
    main()
