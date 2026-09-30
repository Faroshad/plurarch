"""Local backend: SQLite in the shared state folder. No accounts, works on one Wi-Fi network.

The same file is used by `orchestrator.py run` (which also serves the site and the local API) and by
the other CLI commands in separate processes (WAL mode + busy timeout make that safe).
"""
from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from pathlib import Path

from backend_base import Backend, BackendError

SCHEMA_SQL = """
create table if not exists sessions (
  id text primary key, title text not null, use_case text not null,
  status text not null default 'active' check (status in ('active','ended')),
  created_at text not null);
create table if not exists questions (
  session_id text not null references sessions(id) on delete cascade,
  key text not null, type text not null check (type in ('choice','slider')),
  options text, min real, max real, step real, position integer not null default 0,
  primary key (session_id, key));
create table if not exists rounds (
  id text primary key, session_id text not null references sessions(id) on delete cascade,
  number integer not null, status text not null check (status in ('open','closed')),
  opened_at text not null, closed_at text, processed_at text,
  participants integer not null default 0,
  unique (session_id, number));
create unique index if not exists one_open_round on rounds(session_id) where status = 'open';
create table if not exists votes (
  id integer primary key autoincrement,
  round_id text not null references rounds(id) on delete cascade,
  participant_id text not null, question_key text not null, value text not null,
  is_simulated integer not null default 0, created_at text not null,
  unique (round_id, participant_id, question_key));
create index if not exists votes_round on votes(round_id);
create table if not exists decisions (
  id text primary key, session_id text not null references sessions(id) on delete cascade,
  round_id text not null unique references rounds(id) on delete cascade, round_number integer,
  status text not null check (status in ('ok','failed','skipped')), verdict text,
  proposal text, applied_parameters text, evidence text, alternatives_considered text, changes text,
  rationale text, consistency_note text, message text, metrics text, duration_s real,
  created_at text not null);
create table if not exists agent_events (
  id integer primary key autoincrement,
  round_id text not null references rounds(id) on delete cascade,
  seq integer not null, step integer not null, tool text not null, summary text,
  created_at text not null, unique (round_id, seq));
"""

JSON_COLS = {
    "questions": ("options",),
    "decisions": ("proposal", "applied_parameters", "evidence", "alternatives_considered", "changes", "metrics"),
}


def _now() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


class LocalBackend(Backend):
    name = "local"

    def __init__(self, db_path: Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(self.db_path), check_same_thread=False, timeout=10)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.execute("pragma journal_mode=wal")
            self._conn.execute("pragma busy_timeout=10000")
            self._conn.execute("pragma foreign_keys=on")
            self._conn.executescript(SCHEMA_SQL)
            cols = {r[1] for r in self._conn.execute("pragma table_info(rounds)").fetchall()}
            if "participants" not in cols:  # databases created before the live count existed
                self._conn.execute("alter table rounds add column participants integer not null default 0")
            self._conn.commit()

    # --- helpers -------------------------------------------------------------
    def _rows(self, table: str, sql: str, args=()) -> list[dict]:
        with self._lock:
            cur = self._conn.execute(sql, args)
            out = [dict(r) for r in cur.fetchall()]
        for r in out:
            for c in JSON_COLS.get(table, ()):
                if c in r and isinstance(r[c], str):
                    try:
                        r[c] = json.loads(r[c])
                    except ValueError:
                        pass
            if table == "votes" and "is_simulated" in r:
                r["is_simulated"] = bool(r["is_simulated"])
        return out

    def _one(self, table: str, sql: str, args=()) -> dict | None:
        rows = self._rows(table, sql, args)
        return rows[0] if rows else None

    def _exec(self, sql: str, args=()) -> int:
        with self._lock:
            cur = self._conn.execute(sql, args)
            self._conn.commit()
            return cur.rowcount

    # --- health ---------------------------------------------------------------
    def ping(self) -> None:
        try:
            with self._lock:
                self._conn.execute("select 1").fetchone()
        except sqlite3.Error as e:
            raise BackendError("unreachable", str(e))

    # --- sessions -------------------------------------------------------------
    def get_active_session(self):
        return self._one("sessions", "select * from sessions where status='active' "
                                     "and use_case not in ('rls_test','health_check') order by created_at desc limit 1")

    def create_session(self, title, use_case, questions):
        sid = str(uuid.uuid4())
        with self._lock:
            self._conn.execute("insert into sessions(id,title,use_case,status,created_at) values (?,?,?,?,?)",
                               (sid, title, use_case, "active", _now()))
            for q in questions:
                self._conn.execute(
                    "insert into questions(session_id,key,type,options,min,max,step,position) values (?,?,?,?,?,?,?,?)",
                    (sid, q["key"], q["type"], json.dumps(q.get("options")) if q.get("options") is not None else None,
                     q.get("min"), q.get("max"), q.get("step"), q.get("position", 0)))
            self._conn.commit()
        return self._one("sessions", "select * from sessions where id=?", (sid,))

    def end_session(self, session_id):
        self._exec("update sessions set status='ended' where id=?", (session_id,))

    def get_questions(self, session_id):
        return self._rows("questions", "select * from questions where session_id=? order by position", (session_id,))

    # --- rounds ---------------------------------------------------------------
    def list_rounds(self, session_id):
        return self._rows("rounds", "select * from rounds where session_id=? order by number", (session_id,))

    def get_round(self, round_id):
        return self._one("rounds", "select * from rounds where id=?", (round_id,))

    def open_round(self, session_id):
        with self._lock:
            if self._one("rounds", "select id from rounds where session_id=? and status='open'", (session_id,)):
                raise BackendError("round_already_open", "close the open round first")
            n = self._conn.execute("select coalesce(max(number),0)+1 from rounds where session_id=?",
                                   (session_id,)).fetchone()[0]
            rid = str(uuid.uuid4())
            try:
                self._conn.execute("insert into rounds(id,session_id,number,status,opened_at) values (?,?,?,?,?)",
                                   (rid, session_id, n, "open", _now()))
                self._conn.commit()
            except sqlite3.IntegrityError as e:
                self._conn.rollback()
                raise BackendError("round_already_open", str(e))
        return self.get_round(rid)

    def close_round(self, session_id):
        with self._lock:
            r = self._one("rounds", "select * from rounds where session_id=? and status='open'", (session_id,))
            if not r:
                raise BackendError("no_open_round", "there is no open round")
            self._exec("update rounds set status='closed', closed_at=? where id=?", (_now(), r["id"]))
        return self.get_round(r["id"])

    def get_closed_unprocessed_rounds(self, session_id):
        return self._rows("rounds", "select * from rounds where session_id=? and status='closed' "
                                    "and processed_at is null order by number", (session_id,))

    def mark_round_processed(self, round_id):
        self._exec("update rounds set processed_at=? where id=? and processed_at is null", (_now(), round_id))

    def update_round_participants(self, round_id, participants):
        self._exec("update rounds set participants=? where id=?", (int(participants), round_id))

    # --- votes ----------------------------------------------------------------
    def get_votes(self, round_id):
        return self._rows("votes", "select * from votes where round_id=? order by id", (round_id,))

    def insert_votes(self, rows):
        inserted = 0
        with self._lock:
            for r in rows:
                cur = self._conn.execute(
                    "insert or ignore into votes(round_id,participant_id,question_key,value,is_simulated,created_at) "
                    "values (?,?,?,?,?,?)",
                    (r["round_id"], r["participant_id"], r["question_key"], str(r["value"]),
                     1 if r.get("is_simulated") else 0, r.get("created_at") or _now()))
                inserted += cur.rowcount
            self._conn.commit()
        return inserted

    def delete_simulated_votes(self, session_id=None):
        if session_id:
            return self._exec("delete from votes where is_simulated=1 and round_id in "
                              "(select id from rounds where session_id=?)", (session_id,))
        return self._exec("delete from votes where is_simulated=1")

    # --- decisions and events -------------------------------------------------
    def list_decisions(self, session_id):
        return self._rows("decisions", "select * from decisions where session_id=? order by created_at", (session_id,))

    def get_decision(self, decision_id):
        return self._one("decisions", "select * from decisions where id=?", (decision_id,))

    def get_decision_for_round(self, round_id):
        return self._one("decisions", "select * from decisions where round_id=?", (round_id,))

    def insert_decision(self, row):
        existing = self.get_decision_for_round(row["round_id"])
        if existing:
            return existing
        did = row.get("id") or str(uuid.uuid4())
        cols = ["id", "session_id", "round_id", "round_number", "status", "verdict", "proposal", "applied_parameters",
                "evidence", "alternatives_considered", "changes", "rationale", "consistency_note", "message",
                "metrics", "duration_s", "created_at"]
        vals = []
        for c in cols:
            v = {"id": did, "created_at": _now()}.get(c, row.get(c))
            if c in JSON_COLS["decisions"] and v is not None:
                v = json.dumps(v)
            vals.append(v)
        self._exec(f"insert or ignore into decisions({','.join(cols)}) values ({','.join('?' * len(cols))})", vals)
        return self.get_decision_for_round(row["round_id"])

    def insert_agent_event(self, row):
        self._exec("insert or ignore into agent_events(round_id,seq,step,tool,summary,created_at) values (?,?,?,?,?,?)",
                   (row["round_id"], row["seq"], row["step"], row["tool"], row.get("summary"), _now()))

    def list_agent_events(self, round_id):
        return self._rows("agent_events", "select * from agent_events where round_id=? order by seq", (round_id,))

    def delete_agent_events(self, round_id):
        self._exec("delete from agent_events where round_id=?", (round_id,))

    # --- local-only helpers for the local API ------------------------------------
    def session_status(self) -> dict | None:
        s = self.get_active_session()
        if not s:
            return None
        r = self._one("rounds", "select * from rounds where session_id=? order by number desc limit 1", (s["id"],))
        d = self._one("decisions", "select id, created_at from decisions where session_id=? "
                                   "order by created_at desc limit 1", (s["id"],))
        stamps = [x for x in (s["created_at"], r and r["opened_at"], r and r["closed_at"],
                              d and d["created_at"]) if x]
        return {
            "session_id": s["id"], "session_title": s["title"], "use_case": s["use_case"],
            "round_id": r["id"] if r else None, "round_number": r["number"] if r else None,
            "round_status": r["status"] if r else None,
            "round_opened_at": r["opened_at"] if r else None, "round_closed_at": r["closed_at"] if r else None,
            "round_participants": r["participants"] if r else 0,
            "latest_decision_id": d["id"] if d else None, "updated_at": max(stamps),
        }

    def count_participants(self, session_id) -> int:
        with self._lock:
            return self._conn.execute(
                "select count(distinct participant_id) from votes where round_id in "
                "(select id from rounds where session_id=?)", (session_id,)).fetchone()[0]
