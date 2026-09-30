"""Write the machine-specific files (all git-ignored) with absolute paths.

    .venv\\Scripts\\python.exe tools\\setup_local.py [--state-dir PATH] [--force]

Creates or updates:
    config/local.json   absolute paths: python, claude, node, state folder, the Grasshopper-watched file
    agent/mcp.json      MCP config for the headless reviewer agent: ONLY design-mcp
    .mcp.json           the same server for Claude Code in VS Code (build time)
    <state>/parameters.json   the default design, if missing
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from design_mcp import core  # noqa: E402


def find_claude() -> str:
    found = shutil.which("claude")
    if found:
        return str(Path(found).resolve())
    guess = Path.home() / ".local" / "bin" / "claude.exe"
    return str(guess) if guess.exists() else "claude"


NODE_DEFAULT = Path(r"C:\Program Files\nodejs\node.exe")


def find_node() -> str | None:
    """node.exe for the pavilion generator (Rhino runs tools/pavilion_cli.mjs with it)."""
    if NODE_DEFAULT.is_file():
        return str(NODE_DEFAULT)
    found = shutil.which("node")
    return str(Path(found).resolve()) if found else None


def mcp_config(python: str, state_dir: str) -> dict:
    return {
        "mcpServers": {
            "design": {
                "type": "stdio",
                "command": python,
                "args": [str(REPO / "design_mcp" / "server.py")],
                "env": {"PLURARCH_STATE_DIR": state_dir, "PYTHONUTF8": "1"},
            }
        }
    }


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--state-dir", default=None, help="shared state folder (default <repo>/state)")
    ap.add_argument("--force", action="store_true", help="overwrite config/local.json values")
    args = ap.parse_args()

    local_path = REPO / "config" / "local.json"
    local = json.loads(local_path.read_text(encoding="utf-8")) if local_path.exists() else {}
    python = str((REPO / ".venv" / "Scripts" / "python.exe").resolve())
    state_dir = str(Path(args.state_dir).resolve()) if args.state_dir else local.get("state_dir") or str(REPO / "state")

    defaults = {
        "python": python,
        "claude": find_claude(),
        "state_dir": state_dir,
        "grasshopper_file": str(Path(state_dir) / "parameters.json"),
        "backend": "local",
        "local_port": 8787,
        "use_case": "live_presentation",
    }
    node = find_node()
    if node:
        defaults["node"] = node
    else:
        print("warning: node not found; Rhino will show the simplified built-in model "
              "(install Node.js, then run this again)")
    for k, v in defaults.items():
        if args.force or k not in local:
            local[k] = v
    if args.state_dir:
        local["state_dir"] = state_dir
        local["grasshopper_file"] = str(Path(state_dir) / "parameters.json")
    local_path.write_text(json.dumps(local, indent=2) + "\n", encoding="utf-8")

    cfg = mcp_config(local["python"], local["state_dir"])
    (REPO / "agent").mkdir(exist_ok=True)
    (REPO / "agent" / "mcp.json").write_text(json.dumps(cfg, indent=2) + "\n", encoding="utf-8")
    (REPO / ".mcp.json").write_text(json.dumps(cfg, indent=2) + "\n", encoding="utf-8")

    sdir = Path(local["state_dir"])
    sdir.mkdir(parents=True, exist_ok=True)
    if not core.parameters_path(sdir).exists():
        core.write_state(core.default_parameters(), sdir, verdict="DEFAULT", round_id=None)

    print(json.dumps(local, indent=2))
    print("wrote config/local.json, agent/mcp.json, .mcp.json; state at", sdir)


if __name__ == "__main__":
    main()
