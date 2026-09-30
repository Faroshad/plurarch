"""Point Plurarch at a Supabase project (the public values only).

    .venv\\Scripts\\python.exe tools\\configure_supabase.py --url https://xxxx.supabase.co --anon-key <anon/publishable key> [--switch]

Writes:
    site/config.js     supabaseUrl + supabaseAnonKey (public by design; RLS is the security)
    .env               SUPABASE_URL + SUPABASE_ANON_KEY (keeps an existing SUPABASE_SERVICE_ROLE_KEY)
    --switch           also sets "backend": "supabase" in config/local.json

The service role / secret key is NOT taken here on purpose: paste it into .env yourself
(SUPABASE_SERVICE_ROLE_KEY=...). It must never reach site/ or git.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--url", required=True)
    ap.add_argument("--anon-key", required=True)
    ap.add_argument("--switch", action="store_true", help='set "backend": "supabase" in config/local.json')
    a = ap.parse_args()

    url = a.url.strip().rstrip("/")
    if not re.match(r"^https://[a-z0-9-]+\.supabase\.co$", url):
        sys.exit(f"That does not look like a Supabase project URL: {url!r} (expected https://<ref>.supabase.co)")
    key = a.anon_key.strip()
    if key.startswith("sb_secret_") or '"role":"service_role"' in key:
        sys.exit("That is the SECRET key. Use the anon / publishable key here; the secret key goes only into .env.")

    cfg = REPO / "site" / "config.js"
    text = cfg.read_text(encoding="utf-8")
    text = re.sub(r'supabaseUrl:\s*"[^"]*"', f'supabaseUrl: "{url}"', text)
    text = re.sub(r'supabaseAnonKey:\s*"[^"]*"', f'supabaseAnonKey: "{key}"', text)
    cfg.write_text(text, encoding="utf-8")

    env = REPO / ".env"
    lines = env.read_text(encoding="utf-8").splitlines() if env.exists() else \
        (REPO / ".env.example").read_text(encoding="utf-8").splitlines()
    out, seen = [], set()
    for line in lines:
        k = line.split("=", 1)[0].strip()
        if k == "SUPABASE_URL":
            line, _ = f"SUPABASE_URL={url}", seen.add(k)
        elif k == "SUPABASE_ANON_KEY":
            line, _ = f"SUPABASE_ANON_KEY={key}", seen.add(k)
        out.append(line)
    for k, v in (("SUPABASE_URL", url), ("SUPABASE_ANON_KEY", key)):
        if k not in seen:
            out.append(f"{k}={v}")
    env.write_text("\n".join(out) + "\n", encoding="utf-8")

    if a.switch:
        lp = REPO / "config" / "local.json"
        local = json.loads(lp.read_text(encoding="utf-8"))
        local["backend"] = "supabase"
        lp.write_text(json.dumps(local, indent=2) + "\n", encoding="utf-8")

    has_secret = any(l.startswith("SUPABASE_SERVICE_ROLE_KEY=") and len(l.split("=", 1)[1].strip()) > 20
                     for l in out)
    print(f"site/config.js and .env now point at {url}")
    print("backend switched to supabase" if a.switch else "backend unchanged (add --switch to use Supabase)")
    print("service key in .env: " + ("present" if has_secret else "MISSING: paste SUPABASE_SERVICE_ROLE_KEY=... into .env"))


if __name__ == "__main__":
    main()
