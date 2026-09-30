"""Load test: N simulated participant devices polling the status and all voting within one round.

    .venv\\Scripts\\python.exe tools\\load_test.py [--devices 300] [--round-s 60] [--target local|supabase]
                                                   [--base-url http://127.0.0.1:8787]

Each device behaves like the participant app: it polls the status at the profile's interval with
random jitter, votes once at a random moment in the round (all four questions in one request), and
fetches the decision only when latest_decision_id changes. The test opens a round, runs, then closes
it. The running orchestrator reviews that round like any other: run reset-session afterwards.

Local target: the orchestrator `run` must be running (it serves the local API).
Supabase target: SUPABASE_URL, SUPABASE_ANON_KEY and SUPABASE_SERVICE_ROLE_KEY in .env.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import random
import statistics
import sys
import time
from collections import Counter, defaultdict
from pathlib import Path

import httpx

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "orchestrator"))

from design_mcp import core  # noqa: E402


def random_votes(schema: dict) -> list[dict]:
    out = []
    for p in schema["parameters"]:
        if p["type"] == "choice":
            v = random.choice([o["value"] for o in p["options"]])
        else:
            steps = int(round((p["max"] - p["min"]) / p["step"]))
            v = p["min"] + random.randint(0, steps) * p["step"]
            v = int(v) if float(v).is_integer() else round(v, 3)
        out.append({"question_key": p["key"], "value": str(v)})
    return out


class Target:
    """Wraps the two backends' participant-facing HTTP calls."""

    def __init__(self, args, env: dict):
        self.kind = args.target
        if self.kind == "local":
            self.base = args.base_url.rstrip("/")
            key_file = core.state_dir() / "facilitator_key.txt"
            self.key = key_file.read_text(encoding="utf-8").strip() if key_file.exists() else ""
        else:
            self.base = env["SUPABASE_URL"].rstrip("/")
            self.anon = env["SUPABASE_ANON_KEY"]
            self.service = env["SUPABASE_SERVICE_ROLE_KEY"]

    def anon_headers(self) -> dict:
        return {} if self.kind == "local" else {"apikey": self.anon, "Authorization": f"Bearer {self.anon}"}

    async def status(self, c: httpx.AsyncClient):
        if self.kind == "local":
            r = await c.get(f"{self.base}/api/status")
            return r, (r.json() if r.status_code == 200 else None)
        r = await c.get(f"{self.base}/rest/v1/session_status?select=*&limit=1", headers=self.anon_headers())
        rows = r.json() if r.status_code == 200 else []
        return r, (rows[0] if rows else None)

    async def vote(self, c: httpx.AsyncClient, round_id: str, pid: str, votes: list[dict]):
        if self.kind == "local":
            return await c.post(f"{self.base}/api/votes",
                                json={"round_id": round_id, "participant_id": pid, "votes": votes})
        rows = [{"round_id": round_id, "participant_id": pid, **v} for v in votes]
        # plain insert, exactly like the participant app (the trigger skips repeated votes)
        return await c.post(f"{self.base}/rest/v1/votes", json=rows,
                            headers={**self.anon_headers(), "Content-Type": "application/json",
                                     "Prefer": "return=minimal"})

    async def decision(self, c: httpx.AsyncClient, did: str):
        if self.kind == "local":
            return await c.get(f"{self.base}/api/decisions/{did}")
        return await c.get(f"{self.base}/rest/v1/decisions?id=eq.{did}&select=*", headers=self.anon_headers())

    def open_round(self):
        if self.kind == "local":
            r = httpx.post(f"{self.base}/api/rounds/open", headers={"Authorization": f"Bearer {self.key}"}, timeout=10)
            if r.status_code == 409:
                raise SystemExit(f"Could not open a round: {r.json()}. Close the open round first.")
            r.raise_for_status()
            return r.json()["round"]
        from backend_supabase import SupabaseBackend
        be = SupabaseBackend(self.base, self.service)
        s = be.get_active_session()
        if not s:
            raise SystemExit("No active session: start `orchestrator.py run` once first.")
        return be.open_round(s["id"])

    def close_round(self):
        if self.kind == "local":
            httpx.post(f"{self.base}/api/rounds/close", headers={"Authorization": f"Bearer {self.key}"}, timeout=10)
        else:
            from backend_supabase import SupabaseBackend
            be = SupabaseBackend(self.base, self.service)
            s = be.get_active_session()
            if s:
                be.close_round(s["id"])


async def device(i: int, target: Target, c: httpx.AsyncClient, schema: dict, round_id: str, t_end: float,
                 t_vote: float, interval: float, jitter: float, stats: dict) -> None:
    pid = f"load-{i:04d}-{random.getrandbits(24):06x}"
    voted = False
    last_decision = None
    await asyncio.sleep(random.uniform(0, interval))  # devices join at different moments
    while time.monotonic() < t_end:
        t0 = time.monotonic()
        try:
            r, st = await target.status(c)
            stats["lat"]["status"].append((time.monotonic() - t0) * 1000)
            stats["codes"][f"status {r.status_code}"] += 1
            if st and st.get("latest_decision_id") and st["latest_decision_id"] != last_decision:
                last_decision = st["latest_decision_id"]
                t1 = time.monotonic()
                r2 = await target.decision(c, last_decision)
                stats["lat"]["decision"].append((time.monotonic() - t1) * 1000)
                stats["codes"][f"decision {r2.status_code}"] += 1
            if not voted and time.monotonic() >= t_vote and st and st.get("round_status") == "open" \
                    and st.get("round_id") == round_id:
                for attempt in range(4):  # the app retries network errors with backoff
                    t2 = time.monotonic()
                    try:
                        r3 = await target.vote(c, round_id, pid, random_votes(schema))
                        stats["lat"]["vote"].append((time.monotonic() - t2) * 1000)
                        stats["codes"][f"vote {r3.status_code}"] += 1
                        if r3.status_code < 500 and r3.status_code != 429:
                            voted = r3.status_code in (200, 201, 204)
                            break
                    except httpx.HTTPError as e:
                        stats["codes"][f"vote {type(e).__name__}"] += 1
                    await asyncio.sleep(2 ** attempt)
                stats["voted"] += int(voted)
        except httpx.HTTPError as e:
            stats["codes"][f"status {type(e).__name__}"] += 1
        await asyncio.sleep(max(0.2, interval + random.uniform(-jitter, jitter)))


def pct(values: list[float], q: float) -> float:
    if not values:
        return 0.0
    values = sorted(values)
    return values[min(len(values) - 1, int(q * len(values)))]


async def main_async(args) -> None:
    schema = core.load_schema()
    profile = core.load_profile()
    env = {}
    envp = REPO / ".env"
    if envp.exists():
        for line in envp.read_text(encoding="utf-8").splitlines():
            if "=" in line and not line.strip().startswith("#"):
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip()
    target = Target(args, env)
    interval = args.interval or profile["poll_interval_s"]
    jitter = profile.get("poll_jitter_s", 1.5)
    rnd = target.open_round()
    print(f"Round {rnd['number']} open · {args.devices} devices · poll {interval}s ±{jitter}s · "
          f"voting window {args.round_s}s · target {target.kind} {target.base}")
    stats = {"lat": defaultdict(list), "codes": Counter(), "voted": 0}
    start = time.monotonic()
    t_end = start + args.round_s + 10
    limits = httpx.Limits(max_connections=args.devices, max_keepalive_connections=args.devices)
    async with httpx.AsyncClient(timeout=10, limits=limits) as c:
        tasks = [device(i, target, c, schema, rnd["id"], t_end, start + random.uniform(2, args.round_s - 2),
                        interval, jitter, stats) for i in range(args.devices)]
        await asyncio.gather(*tasks)
    target.close_round()
    elapsed = time.monotonic() - start

    total = sum(stats["codes"].values())
    ok = sum(n for k, n in stats["codes"].items() if k.split()[-1] in ("200", "201", "204"))
    print(f"\n{total} requests in {elapsed:.0f} s ({total / elapsed:.1f} req/s) · success {100 * ok / max(total, 1):.1f}%")
    for kind, lat in stats["lat"].items():
        print(f"  {kind:<9} n={len(lat):<5} p50 {statistics.median(lat):.0f} ms · p95 {pct(lat, .95):.0f} ms · "
              f"p99 {pct(lat, .99):.0f} ms · max {max(lat):.0f} ms")
    print(f"  devices that voted: {stats['voted']}/{args.devices}")
    print("  responses: " + ", ".join(f"{k}: {n}" for k, n in sorted(stats["codes"].items())))
    bad = {k: n for k, n in stats["codes"].items() if k.split()[-1] not in ("200", "201", "204")}
    p95 = pct(stats["lat"]["status"], .95)
    print("\nVerdict:")
    if not bad and p95 < 1000 and stats["voted"] == args.devices:
        print("  OK: this load is handled comfortably.")
    else:
        if any("429" in k for k in bad):
            print("  Rate limiting seen: raise poll_interval_s in the use-case profile (e.g. 6-8 s).")
        if any(k.split()[-1].startswith("5") or "Error" in k for k in bad):
            print("  Server errors / timeouts seen: raise poll_interval_s, and check the network or the plan.")
        if p95 >= 1000:
            print(f"  Slow status polls (p95 {p95:.0f} ms): raise poll_interval_s or reduce the status payload.")
        if stats["voted"] < args.devices:
            print(f"  {args.devices - stats['voted']} devices could not vote: see the response codes above.")
    print("\nRun `orchestrator.py reset-session` after a load test to start a clean session.")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--devices", type=int, default=300)
    ap.add_argument("--round-s", type=float, default=60)
    ap.add_argument("--interval", type=float, help="override the profile's poll interval")
    ap.add_argument("--target", choices=["local", "supabase"],
                    default=core.load_local().get("backend", "local"))
    ap.add_argument("--base-url", default=f"http://127.0.0.1:{core.load_local().get('local_port', 8787)}")
    asyncio.run(main_async(ap.parse_args()))


if __name__ == "__main__":
    main()
