"""Backend interface used by the orchestrator.

Two implementations share it:
- backend_local.LocalBackend: SQLite in the shared state folder (local mode, no accounts)
- backend_supabase.SupabaseBackend: Supabase PostgREST with the service role key

Rows are plain dicts with the column names from docs/ARCHITECTURE.md. Timestamps are ISO 8601
strings in UTC. JSON columns come back as Python objects (dict / list), not strings.
"""
from __future__ import annotations


class BackendError(Exception):
    """A backend call failed. `code` is a short machine-readable reason."""

    def __init__(self, code: str, detail: str = ""):
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code = code
        self.detail = detail


class Backend:
    name = "base"

    # --- health -----------------------------------------------------------
    def ping(self) -> None:
        """Raise BackendError if the backend is unreachable (or a Supabase project is paused)."""
        raise NotImplementedError

    # --- sessions ---------------------------------------------------------
    def get_active_session(self) -> dict | None:
        """The newest session with status 'active', or None."""
        raise NotImplementedError

    def create_session(self, title: str, use_case: str, questions: list[dict]) -> dict:
        """Create an active session and seed its questions.

        `questions` rows: {key, type, options (list | None), min, max, step, position}.
        """
        raise NotImplementedError

    def end_session(self, session_id: str) -> None:
        raise NotImplementedError

    def get_questions(self, session_id: str) -> list[dict]:
        raise NotImplementedError

    # --- rounds -----------------------------------------------------------
    def list_rounds(self, session_id: str) -> list[dict]:
        """All rounds of the session, ordered by number."""
        raise NotImplementedError

    def open_round(self, session_id: str) -> dict:
        """Open the next round. BackendError('round_already_open') if one is open."""
        raise NotImplementedError

    def close_round(self, session_id: str) -> dict:
        """Close the open round. BackendError('no_open_round') if none is open."""
        raise NotImplementedError

    def get_closed_unprocessed_rounds(self, session_id: str) -> list[dict]:
        """Closed rounds with processed_at null, ordered by number."""
        raise NotImplementedError

    def mark_round_processed(self, round_id: str) -> None:
        raise NotImplementedError

    def update_round_participants(self, round_id: str, participants: int) -> None:
        """Live aggregate count for phones and the stage (session_status.round_participants)."""
        raise NotImplementedError

    # --- votes ------------------------------------------------------------
    def get_votes(self, round_id: str) -> list[dict]:
        raise NotImplementedError

    def insert_votes(self, rows: list[dict]) -> int:
        """Insert votes, ignoring duplicates. Rows: {round_id, participant_id, question_key,
        value (str), is_simulated (bool)}. Returns the number of rows inserted (best effort)."""
        raise NotImplementedError

    def delete_simulated_votes(self, session_id: str | None = None) -> int:
        """Delete simulated votes (of one session, or all). Returns the count (best effort)."""
        raise NotImplementedError

    # --- decisions and agent events ----------------------------------------
    def list_decisions(self, session_id: str) -> list[dict]:
        """All decisions of the session, oldest first."""
        raise NotImplementedError

    def get_decision_for_round(self, round_id: str) -> dict | None:
        raise NotImplementedError

    def insert_decision(self, row: dict) -> dict:
        """Insert a decision. decisions.round_id is unique: if a decision for the round already
        exists, return the existing row instead of failing."""
        raise NotImplementedError

    def insert_agent_event(self, row: dict) -> None:
        """Row: {round_id, seq, step, tool, summary}."""
        raise NotImplementedError

    def delete_agent_events(self, round_id: str) -> None:
        raise NotImplementedError
