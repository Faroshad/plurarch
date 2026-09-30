"""Access (RLS) tests for the Supabase backend, plus smoke checks of the orchestrator backend.

Milestone 1 acceptance: "An anonymous client cannot read votes or agent_events" and "A signed-in
account that is not in facilitators cannot read votes" - plus the other rules in SPEC.md.

Run (from the project root):
    .venv\\Scripts\\python.exe tests\\test_rls.py            (add --force to run while another
                                                            session is active; see below)
Configuration (environment variables, or the project's .env file):
    SUPABASE_URL, SUPABASE_ANON_KEY, SUPABASE_SERVICE_ROLE_KEY          required
    TEST_NON_FACILITATOR_EMAIL, TEST_NON_FACILITATOR_PASSWORD           optional (else SKIP)
    TEST_FACILITATOR_EMAIL, TEST_FACILITATOR_PASSWORD                   optional (else SKIP)
SUPABASE_ANON_KEY is the publishable key (sb_publishable_...) or the legacy anon key.

All test data lives in one temporary session (use_case 'rls_test') that is deleted at the end,
even when a check fails or crashes. The temporary session is briefly *active*, so participant
devices polling session_status could see it: do not run this during a live session.

Output: one PASS / FAIL / WARN / SKIP line per check. Exit code 0 when nothing FAILed,
1 when a check failed, 2 for a configuration problem, 3 when stopped by the live-session guard.
No pytest needed.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import traceback
import uuid
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "orchestrator"))

from backend_base import BackendError  # noqa: E402
from backend_supabase import SupabaseBackend, anon_request  # noqa: E402

TEST_USE_CASE = "rls_test"
TEST_TITLE = "Plurarch RLS test (temporary, safe to delete)"


# ----------------------------------------------------------------------------- config

def load_dotenv(path: Path) -> None:
    """Minimal .env reader: KEY=VALUE lines, # comments, optional quotes, 'export ' prefix.
    Existing environment variables win."""
    if not path.is_file():
        return
    for raw in path.read_text(encoding="utf-8-sig").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):].lstrip()
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        key, value = key.strip(), value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        else:
            cut = value.find(" #")
            if cut != -1:
                value = value[:cut].rstrip()
        if key and key not in os.environ:
            os.environ[key] = value


def load_questions() -> list[dict]:
    """Question rows for create_session, built from config/parameters.json."""
    cfg = json.loads((ROOT / "config" / "parameters.json").read_text(encoding="utf-8"))
    rows = []
    for i, p in enumerate(cfg["parameters"]):
        if p["type"] == "choice":
            rows.append({"key": p["key"], "type": "choice",
                         "options": [o["value"] if isinstance(o, dict) else o for o in p["options"]],
                         "min": None, "max": None, "step": None, "position": i})
        else:
            rows.append({"key": p["key"], "type": "slider", "options": None,
                         "min": p["min"], "max": p["max"], "step": p["step"], "position": i})
    return rows


def dec(x) -> Decimal:
    return Decimal(str(x))


def fmt(d: Decimal) -> str:
    """Canonical text of a number, like the database trigger stores it ('25', '0.5')."""
    text = format(d.normalize(), "f")
    return "0" if text in ("-0", "") else text


# ----------------------------------------------------------------------------- report

class Report:
    def __init__(self) -> None:
        self.rows: list[tuple[str, str, str]] = []

    def add(self, status: str, name: str, detail: str = "") -> None:
        self.rows.append((status, name, detail))
        print(f"{status:<5} {name}", flush=True)
        for line in str(detail).splitlines():
            if line.strip():
                print(f"        {line}", flush=True)

    def check(self, name: str, fn) -> object:
        """fn() -> (True | False | 'WARN' | 'SKIP', detail[, value]). Exceptions count as FAIL."""
        try:
            result = fn()
        except Exception as exc:  # noqa: BLE001 - every crash is a failed check
            self.add("FAIL", name, f"{type(exc).__name__}: {exc}")
            return None
        ok, detail = result[0], result[1]
        status = ok if isinstance(ok, str) else ("PASS" if ok else "FAIL")
        self.add(status, name, detail)
        return result[2] if len(result) > 2 else None

    def count(self, status: str) -> int:
        return sum(1 for s, _, _ in self.rows if s == status)


def short(resp) -> str:
    try:
        data = resp.json()
        if isinstance(data, dict):
            bits = [str(data.get(k)) for k in ("code", "message", "details") if data.get(k)]
            if bits:
                return " | ".join(bits)[:240]
    except ValueError:
        pass
    return (resp.text or "").strip()[:240]


def rows_of(resp) -> list:
    try:
        data = resp.json()
    except ValueError:
        return []
    return data if isinstance(data, list) else []


def expect_no_rows(resp):
    """A read must return nothing: an HTTP 4xx error, or 200 with an empty list."""
    code = resp.status_code
    if 400 <= code < 500:
        return True, f"blocked: HTTP {code} {short(resp)}"
    if code in (200, 206):
        data = rows_of(resp)
        if not data and resp.text.strip() in ("[]", ""):
            return True, f"HTTP {code}, 0 rows"
        return False, f"LEAK: HTTP {code} returned {len(data)} row(s): {resp.text[:200]}"
    return False, f"inconclusive: HTTP {code} {short(resp)}"


def expect_rejected(resp):
    code = resp.status_code
    if 400 <= code < 500:
        return True, f"rejected: HTTP {code} {short(resp)}"
    return False, f"NOT rejected: HTTP {code} {short(resp)}"


# ----------------------------------------------------------------------------- tests

class RlsTest:
    def __init__(self, url: str, anon_key: str, service_key: str, force: bool) -> None:
        self.url, self.anon_key, self.service_key, self.force = url, anon_key, service_key, force
        self.backend = SupabaseBackend(url, service_key)
        self.report = Report()
        self.questions = load_questions()
        self.choice = next(q for q in self.questions if q["type"] == "choice")
        sliders = [q for q in self.questions if q["type"] == "slider"]
        self.slider = sliders[0]
        self.frac_slider = next((q for q in sliders if dec(q["step"]) % 1 != 0), sliders[0])
        self.session_id: str | None = None
        self.round_id: str | None = None
        self.decision_id: str | None = None
        self.tag = uuid.uuid4().hex[:6]

    # --- helpers --------------------------------------------------------------
    def anon(self, method: str, path: str, **kw):
        return anon_request(self.url, self.anon_key, method, path, **kw)

    def service(self, method: str, path: str, **kw):
        return anon_request(self.url, self.service_key, method, path, **kw)

    def pid(self, name: str) -> str:
        return f"rls-{self.tag}-{name}"

    def valid_value(self, q: dict) -> str:
        if q["type"] == "choice":
            return str(q["options"][0])
        lo, step, hi = dec(q["min"]), dec(q["step"]), dec(q["max"])
        return fmt(lo + step if lo + step <= hi else lo)

    def valid_votes(self, participant: str) -> list[dict]:
        return [{"round_id": self.round_id, "participant_id": participant,
                 "question_key": q["key"], "value": self.valid_value(q)} for q in self.questions]

    def votes_by(self, participant: str) -> list[dict]:
        return [v for v in self.backend.get_votes(self.round_id) if v["participant_id"] == participant]

    def snapshot(self) -> dict:
        return {(v["participant_id"], v["question_key"]): v["value"]
                for v in self.backend.get_votes(self.round_id)}

    # --- run --------------------------------------------------------------------
    def run(self) -> int:
        r = self.report
        print(f"Supabase project: {self.backend.host}")
        print(f"Questions from config/parameters.json: {', '.join(q['key'] for q in self.questions)}\n")

        ok = r.check("service role reaches the project (backend.ping)", self.t_ping_service)
        if ok is None and r.count("FAIL"):
            return self.finish()
        r.check("anon key reaches the project (GET session_status)", self.t_ping_anon)
        if r.count("FAIL"):
            return self.finish()

        self.remove_leftovers()
        if not self.live_session_guard():
            self.backend.close()
            return 3

        try:
            self.setup()
            self.reads_as_anon()
            self.writes_as_anon_open_round()
            self.closed_round()
            self.decisions()
            self.non_facilitator()
            self.facilitator()
        except KeyboardInterrupt:
            r.add("FAIL", "interrupted")
        except Exception as exc:  # noqa: BLE001
            r.add("FAIL", "test run crashed", f"{type(exc).__name__}: {exc}\n{traceback.format_exc()}")
        finally:
            self.cleanup()
        return self.finish()

    def finish(self) -> int:
        r = self.report
        print(f"\nSummary: {r.count('PASS')} passed, {r.count('FAIL')} failed, "
              f"{r.count('WARN')} warnings, {r.count('SKIP')} skipped")
        self.backend.close()
        return 1 if r.count("FAIL") else 0

    # --- preflight --------------------------------------------------------------
    def t_ping_service(self):
        self.backend.ping()
        return True, "", True

    def t_ping_anon(self):
        resp = self.anon("GET", "session_status", params={"select": "session_id", "limit": "1"})
        if resp.status_code == 200:
            return True, "HTTP 200"
        return False, f"HTTP {resp.status_code} {short(resp)} (wrong SUPABASE_ANON_KEY, or schema.sql not run?)"

    def remove_leftovers(self) -> None:
        resp = self.service("GET", "sessions", params={"select": "id", "use_case": f"eq.{TEST_USE_CASE}"})
        for row in rows_of(resp):
            self.service("DELETE", "sessions", params={"id": f"eq.{row['id']}"})
            print(f"(removed a leftover test session {row['id']})")

    def live_session_guard(self) -> bool:
        resp = self.service("GET", "sessions", params={"select": "id,title", "status": "eq.active",
                                                       "use_case": f"neq.{TEST_USE_CASE}"})
        live = rows_of(resp)
        if live and not self.force:
            print(f"\nSTOP  An active session exists ({live[0].get('title')!r}). Participant devices poll "
                  "session_status and could briefly see the temporary test session.\n"
                  "      Run again with --force if no live session is running right now.")
            return False
        return True

    def setup(self) -> None:
        b = self.backend
        s = b.create_session(TEST_TITLE, TEST_USE_CASE, self.questions)
        self.session_id = s["id"]
        rnd = b.open_round(self.session_id)
        self.round_id = rnd["id"]
        n = b.insert_votes([{"round_id": self.round_id, "participant_id": self.pid("svc"),
                             "question_key": self.choice["key"], "value": self.valid_value(self.choice),
                             "is_simulated": True}])
        b.insert_agent_event({"round_id": self.round_id, "seq": 1, "step": 1,
                              "tool": "get_project_brief", "summary": "RLS test event"})
        self.report.add("PASS" if n == 1 and rnd.get("number") == 1 else "FAIL",
                        "setup (service role): session, round 1 open, 1 simulated vote, 1 agent event",
                        f"session {self.session_id}, round {self.round_id}, votes inserted: {n}")

    # --- A: reads as anon -------------------------------------------------------
    def reads_as_anon(self) -> None:
        r, rid = self.report, self.round_id
        r.check("anon cannot read votes (select=*)",
                lambda: expect_no_rows(self.anon("GET", "votes", params={"round_id": f"eq.{rid}"})))
        r.check("anon cannot read votes (key columns only)",
                lambda: expect_no_rows(self.anon("GET", "votes", params={
                    "select": "round_id,participant_id,question_key", "round_id": f"eq.{rid}"})))

        def count_votes():
            resp = self.anon("GET", "votes", params={"select": "round_id", "round_id": f"eq.{rid}"},
                             prefer="count=exact")
            if 400 <= resp.status_code < 500:
                return True, f"blocked: HTTP {resp.status_code}"
            total = (resp.headers.get("content-range") or "").rsplit("/", 1)[-1]
            return total in ("0", "*", ""), f"Content-Range: {resp.headers.get('content-range')}"
        r.check("anon cannot count votes (count=exact)", count_votes)

        r.check("anon cannot read agent_events",
                lambda: expect_no_rows(self.anon("GET", "agent_events", params={"round_id": f"eq.{rid}"})))
        r.check("anon cannot read rounds directly (by design: participants use session_status)",
                lambda: expect_no_rows(self.anon("GET", "rounds", params={"id": f"eq.{rid}"})))
        r.check("anon cannot read sessions directly (by design)",
                lambda: expect_no_rows(self.anon("GET", "sessions", params={"id": f"eq.{self.session_id}"})))
        r.check("anon cannot read facilitators",
                lambda: expect_no_rows(self.anon("GET", "facilitators", params={"select": "*"})))

        def status_row():
            resp = self.anon("GET", "session_status", params={"session_id": f"eq.{self.session_id}"})
            rows = rows_of(resp)
            if resp.status_code != 200 or len(rows) != 1:
                return False, f"HTTP {resp.status_code}, {len(rows)} row(s): {resp.text[:200]}"
            row = rows[0]
            expected_cols = {"session_id", "session_title", "use_case", "round_id", "round_number",
                             "round_status", "round_opened_at", "round_closed_at",
                             "latest_decision_id", "updated_at"}
            ok = (row["round_id"] == rid and row["round_status"] == "open"
                  and row["round_number"] == 1 and set(row) == expected_cols)
            return ok, f"round {row['round_number']} {row['round_status']}, columns: {sorted(row)}"
        r.check("anon CAN read session_status (current round, open)", status_row)

        def questions():
            resp = self.anon("GET", "questions", params={"session_id": f"eq.{self.session_id}"})
            rows = rows_of(resp)
            return (resp.status_code == 200 and len(rows) == len(self.questions),
                    f"HTTP {resp.status_code}, {len(rows)} question(s)")
        r.check("anon CAN read questions", questions)

        def is_fac():
            resp = self.anon("POST", "rpc/is_facilitator", json={})
            if 400 <= resp.status_code < 500:
                return True, f"not callable by anon: HTTP {resp.status_code}"
            return resp.status_code == 200 and resp.json() is False, f"HTTP {resp.status_code}: {resp.text[:80]}"
        r.check("anon is not a facilitator (rpc is_facilitator)", is_fac)

    # --- B: writes as anon while the round is open ----------------------------------
    def writes_as_anon_open_round(self) -> None:
        r, rid, n_q = self.report, self.round_id, len(self.questions)

        p1 = self.pid("p1")

        def plain_insert():
            # what supabase-js sends for from('votes').insert(rows) (no .select())
            resp = self.anon("POST", "votes", json=self.valid_votes(p1),
                             params={"columns": '"round_id","participant_id","question_key","value"'})
            stored = self.votes_by(p1)
            ok = resp.status_code in (200, 201, 204) and len(stored) == n_q
            return ok, (f"HTTP {resp.status_code} {short(resp) if resp.status_code >= 400 else ''}".strip()
                        + f"; stored {len(stored)}/{n_q}: " + ", ".join(f"{v['question_key']}={v['value']}"
                                                                    for v in stored))
        r.check("anon CAN insert valid votes while the round is open (plain insert, like supabase-js insert())",
                plain_insert)

        def repeat_vote():
            options = self.choice["options"]
            other = options[1] if len(options) > 1 else options[0]
            resp = self.anon("POST", "votes", prefer="return=minimal", json=[{
                "round_id": rid, "participant_id": p1, "question_key": self.choice["key"], "value": other}])
            stored = [v for v in self.votes_by(p1) if v["question_key"] == self.choice["key"]]
            first = self.valid_value(self.choice)
            ok = (resp.status_code in (200, 201, 204) or
                  (resp.status_code == 409 and "23505" in resp.text)) and len(stored) == 1 \
                and stored[0]["value"] == first
            return ok, f"HTTP {resp.status_code}; stored value {stored[0]['value'] if stored else None!r} (first: {first!r})"
        r.check("a repeated vote is ignored silently; the first vote counts", repeat_vote)

        def forged_columns():
            p2 = self.pid("p2")
            resp = self.anon("POST", "votes", prefer="return=minimal", json=[{
                "round_id": rid, "participant_id": p2, "question_key": self.choice["key"],
                "value": self.valid_value(self.choice), "is_simulated": True,
                "created_at": "2000-01-01T00:00:00Z"}])
            stored = self.votes_by(p2)
            if resp.status_code >= 400:
                return True, f"rejected outright: HTTP {resp.status_code} {short(resp)}"
            ok = len(stored) == 1 and stored[0]["is_simulated"] is False \
                and not str(stored[0]["created_at"]).startswith("2000")
            return ok, (f"stored is_simulated={stored[0]['is_simulated']}, created_at={stored[0]['created_at']}"
                        if stored else "no row stored")
        r.check("anon cannot mark a vote as simulated or backdate it (trigger forces both)", forged_columns)

        def float_tolerance():
            q = self.frac_slider
            base = dec(q["min"]) + dec(q["step"])
            sent = fmt(base + Decimal("0.0000001"))
            p3 = self.pid("p3")
            resp = self.anon("POST", "votes", prefer="return=minimal", json=[{
                "round_id": rid, "participant_id": p3, "question_key": q["key"], "value": sent}])
            stored = self.votes_by(p3)
            ok = resp.status_code in (200, 201, 204) and len(stored) == 1 and stored[0]["value"] == fmt(base)
            return ok, (f"sent {q['key']}={sent!r}: HTTP {resp.status_code} {short(resp) if resp.status_code >= 400 else ''}"
                        f"; stored {stored[0]['value'] if stored else None!r} (expected {fmt(base)!r})")
        r.check("slider value with float noise is accepted and stored on the step grid", float_tolerance)

        def resend_whole_batch():
            # The participant app retries after a network error: re-sending the SAME batch with a
            # plain insert (what site/js/backend.js does) must succeed silently and keep the first votes.
            p4 = self.pid("p4")
            params = {"columns": '"round_id","participant_id","question_key","value"'}
            first = self.anon("POST", "votes", params=params, json=self.valid_votes(p4))
            changed = [dict(v, value=self.choice["options"][-1]) if v["question_key"] == self.choice["key"] else v
                       for v in self.valid_votes(p4)]
            again = self.anon("POST", "votes", params=params, json=changed)
            stored = {v["question_key"]: v["value"] for v in self.votes_by(p4)}
            expected = {v["question_key"]: v["value"] for v in self.valid_votes(p4)}
            ok = first.status_code in (200, 201, 204) and \
                (again.status_code in (200, 201, 204) or (again.status_code == 409 and "23505" in again.text)) \
                and stored == expected
            return ok, f"first HTTP {first.status_code}, resend HTTP {again.status_code}; stored {stored}"
        r.check("re-sending a whole vote batch (app retry) succeeds silently; the first votes count",
                resend_whole_batch)

        def representation_no_leak():
            p5 = self.pid("p5")
            resp = self.anon("POST", "votes", params={"select": "round_id,participant_id,question_key"},
                             prefer="return=representation", json=self.valid_votes(p5))
            if resp.status_code >= 400:
                return True, f"insert with a response body is refused (HTTP {resp.status_code} {short(resp)}); no data returned"
            rows = rows_of(resp)
            foreign = [x for x in rows if x.get("participant_id") != p5]
            return not foreign, f"returned {len(rows)} row(s), {len(foreign)} of other participants"
        r.check("anon insert with return=representation returns only its own rows", representation_no_leak)

        # invalid inputs: each must be rejected by the database and leave no row
        q_c, q_s = self.choice, self.slider
        lo, st, hi = dec(q_s["min"]), dec(q_s["step"]), dec(q_s["max"])
        cases = [
            ("choice value not among the options", "bad1", q_c["key"], "definitely_not_an_option", rid),
            ("slider value off the step grid", "bad2", q_s["key"], fmt(lo + st / 2), rid),
            ("slider value above max", "bad3", q_s["key"], fmt(hi + st), rid),
            ("slider value below min", "bad4", q_s["key"], fmt(lo - st), rid),
            ("slider value not a number", "bad5", q_s["key"], "abc", rid),
            ("slider value in exponent notation", "bad6", q_s["key"], "1e1", rid),
            ("unknown question key", "bad7", "not_a_question", "1", rid),
            ("value longer than 32 characters", "bad8", q_c["key"], "x" * 40, rid),
            ("unknown round id", "bad9", q_c["key"], self.valid_value(q_c), str(uuid.uuid4())),
        ]
        for label, name, key, value, round_id in cases:
            def one(name=name, key=key, value=value, round_id=round_id):
                p = self.pid(name)
                resp = self.anon("POST", "votes", prefer="return=minimal", json=[{
                    "round_id": round_id, "participant_id": p, "question_key": key, "value": value}])
                ok, detail = expect_rejected(resp)
                stored = self.votes_by(p)
                return ok and not stored, detail + (f"; {len(stored)} row(s) stored!" if stored else "")
            r.check(f"invalid vote rejected by the database: {label}", one)

        for label, participant in (("participant_id longer than 64 characters", "p" * 65),
                                   ("participant_id with spaces", "has space")):
            def bad_pid(participant=participant):
                resp = self.anon("POST", "votes", prefer="return=minimal", json=[{
                    "round_id": rid, "participant_id": participant, "question_key": q_c["key"],
                    "value": self.valid_value(q_c)}])
                ok, detail = expect_rejected(resp)
                stored = self.votes_by(participant)
                return ok and not stored, detail
            r.check(f"invalid vote rejected by the database: {label}", bad_pid)

        def one_open_round_index():
            resp = self.service("POST", "rounds", prefer="return=representation", json={
                "session_id": self.session_id, "number": 99, "status": "open"})
            if resp.status_code < 400:
                for row in rows_of(resp):
                    self.service("DELETE", "rounds", params={"id": f"eq.{row['id']}"})
                return False, "a second open round was accepted"
            return resp.status_code == 409 and "rounds_one_open_per_session" in resp.text, \
                f"HTTP {resp.status_code} {short(resp)}"
        r.check("database allows only ONE open round per session (partial unique index)", one_open_round_index)

        def backend_second_open():
            try:
                self.backend.open_round(self.session_id)
            except BackendError as exc:
                return exc.code == "round_already_open", f"BackendError({exc.code!r})"
            return False, "a second round was opened"
        r.check("backend.open_round refuses while a round is open ('round_already_open')", backend_second_open)

        def anon_rpcs():
            before = [(x["number"], x["status"]) for x in self.backend.list_rounds(self.session_id)]
            a = self.anon("POST", "rpc/open_round", json={"p_session_id": self.session_id})
            c = self.anon("POST", "rpc/close_round", json={"p_session_id": self.session_id})
            after = [(x["number"], x["status"]) for x in self.backend.list_rounds(self.session_id)]
            ok = 400 <= a.status_code < 500 and 400 <= c.status_code < 500 and before == after
            return ok, f"open_round HTTP {a.status_code}, close_round HTTP {c.status_code}; rounds {after}"
        r.check("anon cannot open or close rounds (RPC)", anon_rpcs)

        def anon_update():
            before = self.snapshot()
            resp = self.anon("PATCH", "votes", params={"round_id": f"eq.{rid}"},
                             json={"value": self.choice["options"][-1]}, prefer="return=minimal")
            after = self.snapshot()
            return before == after, f"HTTP {resp.status_code} {short(resp)}; votes unchanged: {before == after}"
        r.check("anon cannot update votes", anon_update)

        def anon_delete():
            before = self.snapshot()
            resp = self.anon("DELETE", "votes", params={"round_id": f"eq.{rid}"}, prefer="return=minimal")
            after = self.snapshot()
            return before == after, f"HTTP {resp.status_code} {short(resp)}; {len(after)}/{len(before)} votes remain"
        r.check("anon cannot delete votes", anon_delete)

        def anon_writes_elsewhere():
            attempts = {
                "decisions": {"round_id": rid, "session_id": self.session_id, "status": "ok",
                              "verdict": "ACCEPTED", "rationale": "forged"},
                "agent_events": {"round_id": rid, "seq": 999, "step": 1, "tool": "forged"},
                "sessions": {"title": "forged", "use_case": TEST_USE_CASE},
                "rounds": {"session_id": self.session_id, "number": 50, "status": "closed"},
                "questions": {"session_id": self.session_id, "key": "forged", "type": "choice",
                              "options": ["a"]},
            }
            codes = {t: self.anon("POST", t, json=body, prefer="return=minimal").status_code
                     for t, body in attempts.items()}
            leaked = self.backend.get_decision_for_round(rid) is not None
            ok = all(400 <= c < 500 for c in codes.values()) and not leaked
            return ok, ", ".join(f"{t}: HTTP {c}" for t, c in codes.items())
        r.check("anon cannot write decisions, agent_events, sessions, rounds or questions", anon_writes_elsewhere)

    # --- C: closed round ------------------------------------------------------------
    def closed_round(self) -> None:
        r, rid = self.report, self.round_id

        def close():
            row = self.backend.close_round(self.session_id)
            return (row["id"] == rid and row["status"] == "closed" and bool(row.get("closed_at")),
                    f"status {row['status']}, closed_at {row.get('closed_at')}")
        r.check("backend.close_round closes the round and sets closed_at", close)

        def vote_closed():
            p6 = self.pid("p6")
            resp = self.anon("POST", "votes", json=self.valid_votes(p6), prefer="return=minimal")
            ok, detail = expect_rejected(resp)
            stored = self.votes_by(p6)
            return ok and not stored, detail
        r.check("anon cannot insert votes into a closed round", vote_closed)

        def status_closed():
            rows = rows_of(self.anon("GET", "session_status", params={"session_id": f"eq.{self.session_id}"}))
            return (len(rows) == 1 and rows[0]["round_status"] == "closed"
                    and bool(rows[0]["round_closed_at"])), f"{rows[0] if rows else None}"
        r.check("session_status shows the round closed", status_closed)

        def unprocessed():
            ids = [x["id"] for x in self.backend.get_closed_unprocessed_rounds(self.session_id)]
            return ids == [rid], f"closed unprocessed rounds: {len(ids)}"
        r.check("backend.get_closed_unprocessed_rounds lists the closed round", unprocessed)

        def close_again():
            try:
                self.backend.close_round(self.session_id)
            except BackendError as exc:
                return exc.code == "no_open_round", f"BackendError({exc.code!r})"
            return False, "closed a round that was not open"
        r.check("backend.close_round with no open round raises 'no_open_round'", close_again)

    # --- D: decisions -----------------------------------------------------------------
    def decisions(self) -> None:
        r, rid = self.report, self.round_id
        applied = {q["key"]: self.valid_value(q) for q in self.questions}

        def insert():
            row = self.backend.insert_decision({
                "round_id": rid, "status": "ok", "verdict": "ACCEPTED",
                "proposal": {"parameters": applied}, "applied_parameters": applied,
                "evidence": {}, "alternatives_considered": [], "changes": None,
                "rationale": "RLS test decision.", "metrics": {"before": {}, "proposal": {}, "after": {}},
                "duration_s": 1.5, "not_a_column": "dropped by the backend"})
            self.decision_id = row["id"]
            ok = row["session_id"] == self.session_id and row["round_number"] == 1 and row["changes"] == []
            return ok, f"decision {row['id']}, session_id/round_number filled: {ok}"
        r.check("backend.insert_decision stores a decision (session_id, round_number filled by the DB)", insert)

        def insert_again():
            row = self.backend.insert_decision({"round_id": rid, "status": "failed", "message": "duplicate"})
            return row["id"] == self.decision_id and row["status"] == "ok", "returned the existing decision"
        r.check("backend.insert_decision is idempotent per round", insert_again)

        def anon_reads_decision():
            resp = self.anon("GET", "decisions", params={"id": f"eq.{self.decision_id}"})
            rows = rows_of(resp)
            return resp.status_code == 200 and len(rows) == 1 and rows[0]["verdict"] == "ACCEPTED", \
                f"HTTP {resp.status_code}, {len(rows)} row(s)"
        r.check("anon CAN read decisions", anon_reads_decision)

        def status_latest():
            rows = rows_of(self.anon("GET", "session_status", params={"session_id": f"eq.{self.session_id}"}))
            return (len(rows) == 1 and rows[0]["latest_decision_id"] == self.decision_id,
                    f"latest_decision_id {rows[0]['latest_decision_id'] if rows else None}")
        r.check("session_status.latest_decision_id points at the new decision", status_latest)

        def processed():
            self.backend.mark_round_processed(rid)
            left = self.backend.get_closed_unprocessed_rounds(self.session_id)
            return not left, f"closed unprocessed rounds after mark_round_processed: {len(left)}"
        r.check("backend.mark_round_processed removes the round from the queue", processed)

        def votes_and_simulated():
            votes = self.backend.get_votes(rid)
            anon_votes = [v for v in votes if v["participant_id"] != self.pid("svc")]
            sims = [v for v in votes if v["is_simulated"]]
            deleted = self.backend.delete_simulated_votes(self.session_id)
            remaining = self.backend.get_votes(rid)
            ok = (len(sims) == 1 and deleted == 1 and len(remaining) == len(votes) - 1
                  and all(not v["is_simulated"] for v in anon_votes))
            return ok, (f"{len(votes)} votes ({len(sims)} simulated); deleted {deleted}; "
                        f"{len(remaining)} remain")
        r.check("backend.get_votes / delete_simulated_votes", votes_and_simulated)

    # --- E: signed-in non-facilitator -----------------------------------------------------
    def sign_in(self, email: str, password: str) -> str:
        resp = self.anon("POST", "/auth/v1/token?grant_type=password", json={"email": email, "password": password})
        if resp.status_code != 200:
            raise RuntimeError(f"password sign-in for {email} failed: HTTP {resp.status_code} {short(resp)}")
        return resp.json()["access_token"]

    def non_facilitator(self) -> None:
        r, rid = self.report, self.round_id
        email = os.environ.get("TEST_NON_FACILITATOR_EMAIL", "").strip()
        password = os.environ.get("TEST_NON_FACILITATOR_PASSWORD", "")
        if not email or not password:
            r.add("SKIP", "signed-in non-facilitator cannot read votes (spec access test 2)",
                  "Set TEST_NON_FACILITATOR_EMAIL and TEST_NON_FACILITATOR_PASSWORD in .env to run it.\n"
                  "Sign-ups are disabled, so create the test user yourself: Supabase dashboard ->\n"
                  "Authentication -> Users -> Add user -> Create new user (any e-mail you control, e.g.\n"
                  "plurarch-test@example.com, a password, tick 'Auto Confirm User'). Do NOT add it to\n"
                  "public.facilitators.")
            return
        try:
            token = self.sign_in(email, password)
        except Exception as exc:  # noqa: BLE001
            r.add("FAIL", "non-facilitator sign-in", str(exc))
            return

        def user(method, path, **kw):
            return self.anon(method, path, access_token=token, **kw)

        r.check("signed-in non-facilitator cannot read votes",
                lambda: expect_no_rows(user("GET", "votes", params={"round_id": f"eq.{rid}"})))
        r.check("signed-in non-facilitator cannot read agent_events",
                lambda: expect_no_rows(user("GET", "agent_events", params={"round_id": f"eq.{rid}"})))
        r.check("signed-in non-facilitator cannot read rounds or sessions (no extra access)",
                lambda: (lambda a, b: (a[0] and b[0], f"rounds: {a[1]}; sessions: {b[1]}"))(
                    expect_no_rows(user("GET", "rounds", params={"id": f"eq.{rid}"})),
                    expect_no_rows(user("GET", "sessions", params={"id": f"eq.{self.session_id}"}))))

        def not_fac():
            resp = user("POST", "rpc/is_facilitator", json={})
            return resp.status_code == 200 and resp.json() is False, f"HTTP {resp.status_code}: {resp.text[:80]}"
        r.check("signed-in non-facilitator: is_facilitator() is false", not_fac)

        def cannot_open():
            before = len(self.backend.list_rounds(self.session_id))
            resp = user("POST", "rpc/open_round", json={"p_session_id": self.session_id})
            after = len(self.backend.list_rounds(self.session_id))
            ok, detail = expect_rejected(resp)
            return ok and before == after, detail
        r.check("signed-in non-facilitator cannot open rounds", cannot_open)

    # --- F: facilitator ------------------------------------------------------------------
    def facilitator(self) -> None:
        r, rid = self.report, self.round_id
        email = os.environ.get("TEST_FACILITATOR_EMAIL", "").strip()
        password = os.environ.get("TEST_FACILITATOR_PASSWORD", "")
        if not email or not password:
            r.add("SKIP", "facilitator can read votes / agent_events and open / close rounds",
                  "Set TEST_FACILITATOR_EMAIL and TEST_FACILITATOR_PASSWORD in .env (the console login,\n"
                  "whose e-mail is in public.facilitators) to run these checks.")
            return
        try:
            token = self.sign_in(email, password)
        except Exception as exc:  # noqa: BLE001
            r.add("FAIL", "facilitator sign-in", str(exc))
            return

        def user(method, path, **kw):
            return self.anon(method, path, access_token=token, **kw)

        def is_fac():
            resp = user("POST", "rpc/is_facilitator", json={})
            return (resp.status_code == 200 and resp.json() is True,
                    f"HTTP {resp.status_code}: {resp.text[:80]} (is the e-mail in public.facilitators?)")
        r.check("facilitator: is_facilitator() is true", is_fac)

        def reads():
            v = user("GET", "votes", params={"round_id": f"eq.{rid}"})
            e = user("GET", "agent_events", params={"round_id": f"eq.{rid}"})
            s = user("GET", "sessions", params={"id": f"eq.{self.session_id}"})
            ok = all(x.status_code == 200 for x in (v, e, s)) and rows_of(v) and rows_of(e) and rows_of(s)
            return bool(ok), f"votes {len(rows_of(v))}, agent_events {len(rows_of(e))}, sessions {len(rows_of(s))}"
        r.check("facilitator CAN read votes, agent_events and sessions", reads)

        def open_close():
            a = user("POST", "rpc/open_round", json={"p_session_id": self.session_id})
            opened = a.json() if a.status_code == 200 else {}
            b = user("POST", "rpc/open_round", json={"p_session_id": self.session_id})
            c = user("POST", "rpc/close_round", json={"p_session_id": self.session_id})
            closed = c.json() if c.status_code == 200 else {}
            d = user("POST", "rpc/close_round", json={"p_session_id": self.session_id})
            ok = (opened.get("number") == 2 and opened.get("status") == "open"
                  and b.status_code == 409 and "round_already_open" in b.text
                  and closed.get("status") == "closed" and bool(closed.get("closed_at"))
                  and d.status_code == 409 and "no_open_round" in d.text)
            return ok, (f"open HTTP {a.status_code} (round {opened.get('number')}), open again HTTP {b.status_code} "
                        f"{short(b)}, close HTTP {c.status_code}, close again HTTP {d.status_code} {short(d)}")
        r.check("facilitator CAN open and close rounds (RPC, with round_already_open / no_open_round)", open_close)

    # --- cleanup ---------------------------------------------------------------------------
    def cleanup(self) -> None:
        if not self.session_id:
            return
        try:
            self.service("DELETE", "sessions", params={"id": f"eq.{self.session_id}"})
            left = rows_of(self.service("GET", "sessions", params={"id": f"eq.{self.session_id}"}))
            self.report.add("PASS" if not left else "FAIL",
                            "cleanup: temporary session deleted (cascades to its rounds, votes, decisions, events)")
        except Exception as exc:  # noqa: BLE001
            self.report.add("FAIL", "cleanup", f"{exc}. Delete sessions with use_case = '{TEST_USE_CASE}' "
                                               "in the Table Editor.")


def main() -> int:
    parser = argparse.ArgumentParser(description="Supabase RLS / access tests for Plurarch")
    parser.add_argument("--force", action="store_true",
                        help="run even if another session is active (participants could see the test session)")
    args = parser.parse_args()

    load_dotenv(ROOT / ".env")
    url = os.environ.get("SUPABASE_URL", "").strip()
    anon_key = os.environ.get("SUPABASE_ANON_KEY", "").strip()
    service_key = os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "").strip()
    missing = [n for n, v in (("SUPABASE_URL", url), ("SUPABASE_ANON_KEY", anon_key),
                              ("SUPABASE_SERVICE_ROLE_KEY", service_key)) if not v]
    if missing:
        print("CONFIG  missing: " + ", ".join(missing))
        print("        Put them in the environment or in the project's .env file (see supabase/SETUP.md).")
        return 2
    try:
        test = RlsTest(url, anon_key, service_key, args.force)
    except BackendError as exc:
        print(f"CONFIG  {exc}")
        return 2
    return test.run()


if __name__ == "__main__":
    sys.exit(main())
