"""Readable lines from the reviewer agent's raw event stream (state/agent_runs/<run_id>.jsonl).

Used by the local server's /api/showcase/agent endpoint, so the showcase page can show the real
headless Claude Code run as a live terminal: session start, every tool call with its key result,
the Revit report, and the final decision record.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path


def _short(obj, n: int = 120) -> str:
    s = obj if isinstance(obj, str) else json.dumps(obj, ensure_ascii=False, separators=(", ", ": "))
    return s if len(s) <= n else s[: n - 1] + "…"


def _params(p: dict | None) -> str:
    if not isinstance(p, dict):
        return ""
    order = ("infill_finish", "se_glass_share", "fin_depth", "skylights_open")
    units = {"se_glass_share": "%", "fin_depth": " m", "skylights_open": " lanterns"}
    return " · ".join(f"{p[k]}{units.get(k, '')}" for k in order if k in p)


def _result_text(block) -> dict | str:
    content = block.get("content")
    text = ""
    if isinstance(content, list):
        text = "".join(c.get("text", "") for c in content if isinstance(c, dict))
    elif isinstance(content, str):
        text = content
    try:
        return json.loads(text)
    except ValueError:
        return text


def _summarise_result(tool: str, out) -> str:
    if not isinstance(out, dict):
        return _short(out, 100)
    if tool == "get_project_brief":
        return f"{out.get('title', 'brief')} · {len(out.get('goals', []))} goals · {len(out.get('hard_rules', []))} hard rules"
    if tool == "get_schema":
        return f"{len(out.get('parameters', []))} questions"
    if tool == "evaluate":
        if not out.get("valid", True):
            return "invalid: " + _short(out.get("error") or out.get("errors"), 80)
        return out.get("summary") or _short(out.get("metrics"), 100)
    if tool == "set_parameters":
        if not out.get("applied"):
            return "refused: " + _short(out.get("detail") or out.get("error"), 100)
        rv = out.get("revit") or {}
        rv_txt = ""
        if isinstance(rv, dict) and rv:
            if rv.get("applied"):
                ch = rv.get("changes") or {}
                parts = []
                if ch.get("panels_retyped"):
                    parts.append(f"{ch['panels_retyped']} panels retyped")
                if ch.get("fins_created"):
                    parts.append(f"{ch['fins_created']} fins created")
                if ch.get("fins_deleted"):
                    parts.append(f"{ch['fins_deleted']} fins removed")
                if ch.get("material"):
                    parts.append("finish material set")
                rv_txt = (" · Revit: " + (", ".join(parts) or f"{rv.get('ops', 0)} operations") +
                          (" · verified ✓" if rv.get("verified") else ""))
            else:
                rv_txt = " · Revit: not applied (" + _short(rv.get("error") or rv.get("reason") or "", 60) + ")"
        return f"applied {_params(out.get('parameters'))}{rv_txt}"
    if tool == "get_parameters":
        return "model now: " + _params(out.get("parameters"))
    if tool == "get_revit_state":
        if out.get("available") is False or out.get("error"):
            return "Revit: " + _short(out.get("error") or out.get("reason") or "unavailable", 80)
        depths = out.get("fin_depths_m") or []
        depth = f" ({max(depths)} m)" if depths else ""
        bits = [f"SE glass {out.get('se_panels_glazed', '?')}/{out.get('se_panels_total', '?')}",
                f"lanterns open {out.get('lanterns_open', '?')}",
                f"fins {out.get('fins', 0)}{depth}",
                f"finish {out.get('solid_finish', '?')}"]
        match = out.get("matches_applied_parameters")
        tail = " · matches the decision ✓" if match else (" · MISMATCH" if match is False else "")
        return "Revit: " + " · ".join(bits) + tail
    return _short(out, 100)


def lines_from(stream_path: Path, start: int = 0) -> tuple[list[dict], int, bool]:
    """Parse the stream from line `start`. Returns (display lines, next start, finished)."""
    out: list[dict] = []
    done = False
    tool_names: dict[str, str] = {}
    try:
        raw_lines = stream_path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return out, start, False
    # tool ids are needed to label results: scan everything before `start` for tool_use names
    for i, raw in enumerate(raw_lines):
        try:
            ev = json.loads(raw)
        except ValueError:
            continue
        if ev.get("type") == "assistant":
            for c in ev.get("message", {}).get("content", []):
                if c.get("type") == "tool_use":
                    tool_names[c.get("id", "")] = c.get("name", "").replace("mcp__design__", "")
        if i < start:
            continue
        t = ev.get("type")
        if t == "system" and ev.get("subtype") == "init":
            tools = [x.replace("mcp__design__", "") for x in ev.get("tools", []) if x != "StructuredOutput"]
            out.append({"k": "sys", "t": f"session started · model {ev.get('model', '?')} · "
                                          f"{len(tools)} design-mcp tools ({', '.join(tools)}) "
                                          "+ the decision-record schema; no shell, files or web"})
        elif t == "assistant":
            for c in ev.get("message", {}).get("content", []):
                if c.get("type") == "thinking" and c.get("thinking"):
                    out.append({"k": "think", "t": _short(c["thinking"].strip().replace("\n", " "), 220)})
                elif c.get("type") == "text" and c.get("text", "").strip():
                    out.append({"k": "say", "t": _short(c["text"].strip().replace("\n", " "), 220)})
                elif c.get("type") == "tool_use":
                    name = c.get("name", "").replace("mcp__design__", "")
                    if name == "StructuredOutput":
                        continue
                    inp = c.get("input") or {}
                    arg = _params(inp.get("parameters")) if isinstance(inp, dict) and inp.get("parameters") else ""
                    if name == "set_parameters" and isinstance(inp, dict):
                        arg = f"{inp.get('verdict', '')} · {arg}"
                    out.append({"k": "call", "tool": name, "t": arg})
        elif t == "user":
            for c in ev.get("message", {}).get("content", []):
                if isinstance(c, dict) and c.get("type") == "tool_result":
                    name = tool_names.get(c.get("tool_use_id", ""), "")
                    if name in ("StructuredOutput", ""):
                        continue
                    out.append({"k": "result", "tool": name,
                                "t": _summarise_result(name, _result_text(c))})
        elif t == "result":
            done = True
            rec = ev.get("structured_output") or {}
            if isinstance(rec, dict) and rec.get("verdict"):
                out.append({"k": "verdict", "t": rec["verdict"], "applied": _params(rec.get("applied_parameters")),
                            "rationale": rec.get("rationale", "")})
            out.append({"k": "sys", "t": f"process exited · {round((ev.get('duration_ms') or 0) / 1000, 1)} s · "
                                          f"{ev.get('num_turns', '?')} turns"})
    return out, len(raw_lines), done


def latest(state_dir: Path) -> dict | None:
    try:
        return json.loads((state_dir / "agent_runs" / "latest.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


# ---------------------------------------------------------------- terminal follower
# python orchestrator/showcase_feed.py --follow <state_dir>
# Prints every new reviewer-agent run as it happens (the showcase screen recording runs this in a
# terminal pane next to the orchestrator). It only reads the run's stream file; it never talks to the agent.
_C = {"dim": "\033[90m", "sys": "\033[90m", "think": "\033[3;95m", "say": "\033[97m", "call": "\033[1;97m",
      "dot": "\033[92m", "args": "\033[37m", "result": "\033[37m", "rv": "\033[38;5;215m", "stdin": "\033[38;5;183m",
      "key": "\033[38;5;117m", "reset": "\033[0m", "ok": "\033[92m",
      "ACCEPTED": "\033[1;30;102m", "MODIFIED": "\033[1;30;103m", "REJECTED": "\033[1;97;101m"}


def _wrap(text: str, indent: str, width: int) -> str:
    import textwrap
    return textwrap.fill(text, width=width, initial_indent="", subsequent_indent=indent,
                         break_long_words=False, break_on_hyphens=False)


def _print_line(L: dict, width: int) -> None:
    c = _C
    k = L.get("k")
    if k == "sys":
        print(f"{c['sys']}● {_wrap(L['t'], '  ', width - 2)}{c['reset']}")
    elif k == "think":
        print(f"{c['think']}✻ {_wrap(L['t'], '  ', width - 2)}{c['reset']}")
    elif k == "say":
        print(f"{c['say']}{_wrap(L['t'], '', width)}{c['reset']}")
    elif k == "call":
        print()
        print(f"{c['dot']}⏺{c['reset']} {c['call']}{L.get('tool')}{c['reset']}{c['args']}({L.get('t', '')}){c['reset']}")
    elif k == "result":
        col = c["rv"] if L.get("tool") in ("set_parameters", "get_revit_state") else c["result"]
        print(f"  {c['dim']}⎿{c['reset']}  {col}{_wrap(L['t'], '     ', width - 5)}{c['reset']}")
    elif k == "verdict":
        print()
        print(f"{c.get(L['t'], '')} {L['t']} {c['reset']}  {c['say']}{L.get('applied', '')}{c['reset']}")
        print(f"{c['say']}{_wrap(L.get('rationale', ''), '', width)}{c['reset']}")
        print()
    sys.stdout.flush()


def follow(state_dir: Path, poll: float = 0.15) -> None:
    import shutil
    import time
    os.system("")  # enable ANSI on Windows consoles
    width = max(60, min(140, shutil.get_terminal_size((110, 30)).columns - 2))
    seen_run, t_launch = None, time.time()
    c = _C
    print(f"{c['dim']}Claude Code reviewer · live view of its stream-json output · {state_dir}{c['reset']}")
    print(f"{c['dim']}Waiting for a round to close…{c['reset']}")
    while True:
        meta = latest(state_dir)
        if not meta or meta.get("run_id") == seen_run or float(meta.get("started") or 0) < t_launch:
            time.sleep(poll)
            continue
        seen_run = meta["run_id"]
        part = meta.get("participation") or {}
        print()
        print(f"{c['ok']}PS state\agent_work>{c['reset']} {c['dim']}{_wrap(meta.get('command', 'claude -p'), '  ', width)}{c['reset']}")
        props = ", ".join(f"{c['key']}{k}{c['stdin']}: {json.dumps(v)}" for k, v in (meta.get("proposal") or {}).items())
        print(f"{c['stdin']}stdin ← Round {meta.get('round_number')} proposal from the vote tally (data, not instructions)")
        print(f"       {{ {props} }}")
        print(f"       {part.get('participants', '?')} participants · {part.get('votes', '?')} votes{c['reset']}")
        sys.stdout.flush()
        stream = state_dir / "agent_runs" / meta["stream"]
        nxt, done = 0, False
        while not done:
            lines, nxt, done = lines_from(stream, nxt)
            for L in lines:
                _print_line(L, width)
            if not done:
                time.sleep(poll)
                nmeta = latest(state_dir)
                if nmeta and nmeta.get("run_id") != seen_run:
                    break
        print(f"{c['dim']}Waiting for the next round…{c['reset']}")
        sys.stdout.flush()


if __name__ == "__main__":
    if len(sys.argv) >= 3 and sys.argv[1] == "--follow":
        follow(Path(sys.argv[2]))
    else:
        sys.exit("usage: showcase_feed.py --follow <state_dir>")
