"""Errors for workflow NL clarification sessions (checkpoint-backed)."""


class ClarificationSessionNotFoundError(Exception):
    """Checkpoint missing or TTL-expired clarification session."""

    def __init__(self, session_id: str | None = None) -> None:
        self.session_id = session_id
        super().__init__(session_id or "unknown")


class ClarificationAccessDeniedError(Exception):
    """Session does not belong to the authenticated user."""
