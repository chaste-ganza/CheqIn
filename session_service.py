"""
session_service.py — Attendance session management.

Prototype rule: only ONE session may be OPEN at any time because there
is only one physical RFID reader.

Public API:

    # Queries
    get_session(session_id)             -> SessionRow | None
    get_open_session()                  -> SessionRow | None
    list_sessions(filters)              -> list[SessionRow]
    get_session_attendance(session_id)  -> list[AttendanceRow]

    # Mutations (all return SessionResult)
    create_session(...)                 -> SessionResult
    open_session(session_id)            -> SessionResult
    close_session(session_id)           -> SessionResult
    complete_session(session_id)        -> SessionResult

State machine:
    SCHEDULED -> OPEN -> CLOSED -> COMPLETED
                   ^_______/   (reopen is allowed: CLOSED -> OPEN)
    OPEN -> COMPLETED  (direct complete without closing first)
    COMPLETED is permanent.

One-open-at-a-time:
    Attempting to OPEN a session while another is already OPEN returns
    SessionOutcome.ALREADY_OPEN_EXISTS with the conflicting session's details.
    The caller (teacher portal route) must close the existing session first.
"""

import sqlite3
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from enum import Enum
from typing import Optional

from database import get_connection


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")

def _today() -> str:
    return date.today().isoformat()


# ---------------------------------------------------------------------------
# Data transfer objects
# ---------------------------------------------------------------------------

@dataclass
class SessionRow:
    """A read-only view of one attendance_sessions row."""
    id           : int
    class_id     : int
    class_name   : Optional[str]
    teacher_id   : Optional[int]
    teacher_name : Optional[str]
    subject_id   : Optional[int]
    subject_name : Optional[str]
    session_date : str
    start_time   : Optional[str]
    end_time     : Optional[str]
    status       : str
    opened_at    : Optional[str]
    closed_at    : Optional[str]
    completed_at : Optional[str]
    created_at   : str

    @property
    def is_open(self) -> bool:
        return self.status == "OPEN"

    @property
    def is_closed(self) -> bool:
        return self.status == "CLOSED"

    @property
    def is_completed(self) -> bool:
        return self.status == "COMPLETED"

    @property
    def is_scheduled(self) -> bool:
        return self.status == "SCHEDULED"

    def to_dict(self) -> dict:
        return {
            "id"          : self.id,
            "class_id"    : self.class_id,
            "class_name"  : self.class_name,
            "teacher_id"  : self.teacher_id,
            "teacher_name": self.teacher_name,
            "subject_id"  : self.subject_id,
            "subject_name": self.subject_name,
            "session_date": self.session_date,
            "start_time"  : self.start_time,
            "end_time"    : self.end_time,
            "status"      : self.status,
            "opened_at"   : self.opened_at,
            "closed_at"   : self.closed_at,
            "completed_at": self.completed_at,
            "created_at"  : self.created_at,
        }


@dataclass
class AttendanceRow:
    """A read-only view of one attendance row with student details."""
    id                  : int
    session_id          : int
    student_id          : int
    first_name          : str
    last_name           : str
    registration_number : str
    rfid_uid            : Optional[str]
    attendance_time     : str
    status              : str
    created_at          : str

    @property
    def full_name(self) -> str:
        return f"{self.first_name} {self.last_name}".strip()

    def to_dict(self) -> dict:
        return {
            "id"                 : self.id,
            "session_id"         : self.session_id,
            "student_id"         : self.student_id,
            "first_name"         : self.first_name,
            "last_name"          : self.last_name,
            "full_name"          : self.full_name,
            "registration_number": self.registration_number,
            "rfid_uid"           : self.rfid_uid,
            "attendance_time"    : self.attendance_time,
            "status"             : self.status,
            "created_at"         : self.created_at,
        }


# ---------------------------------------------------------------------------
# Session operation result
# ---------------------------------------------------------------------------

class SessionOutcome(Enum):
    OK                   = "OK"
    CREATED              = "CREATED"
    OPENED               = "OPENED"
    CLOSED               = "CLOSED"
    COMPLETED            = "COMPLETED"
    ALREADY_OPEN_EXISTS  = "ALREADY_OPEN_EXISTS"  # one-open rule violated
    NOT_FOUND            = "NOT_FOUND"
    INVALID_TRANSITION   = "INVALID_TRANSITION"   # e.g. COMPLETED -> OPEN
    CLASS_NOT_FOUND      = "CLASS_NOT_FOUND"
    VALIDATION_ERROR     = "VALIDATION_ERROR"
    ERROR                = "ERROR"


@dataclass
class SessionResult:
    outcome          : SessionOutcome
    session          : Optional[SessionRow] = None
    conflicting      : Optional[SessionRow] = None   # set when ALREADY_OPEN_EXISTS
    message          : str = ""

    def success(self) -> bool:
        return self.outcome in (
            SessionOutcome.OK,
            SessionOutcome.CREATED,
            SessionOutcome.OPENED,
            SessionOutcome.CLOSED,
            SessionOutcome.COMPLETED,
        )

    def __str__(self):
        parts = [f"outcome={self.outcome.value}"]
        if self.session:
            parts.append(f"session_id={self.session.id}")
        parts.append(f"msg={self.message!r}")
        return " | ".join(parts)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _row_to_session(row: sqlite3.Row) -> SessionRow:
    return SessionRow(
        id           = row["id"],
        class_id     = row["class_id"],
        class_name   = row["class_name"],
        teacher_id   = row["teacher_id"],
        teacher_name = row["teacher_name"],
        subject_id   = row["subject_id"],
        subject_name = row["subject_name"],
        session_date = row["session_date"],
        start_time   = row["start_time"],
        end_time     = row["end_time"],
        status       = row["status"],
        opened_at    = row["opened_at"],
        closed_at    = row["closed_at"],
        completed_at = row["completed_at"],
        created_at   = row["created_at"],
    )


_SESSION_SELECT = """
    SELECT
        s.id,
        s.class_id,
        c.name          AS class_name,
        s.teacher_id,
        t.name          AS teacher_name,
        s.subject_id,
        sub.name        AS subject_name,
        s.session_date,
        s.start_time,
        s.end_time,
        s.status,
        s.opened_at,
        s.closed_at,
        s.completed_at,
        s.created_at
    FROM attendance_sessions s
    LEFT JOIN classes  c   ON c.id   = s.class_id
    LEFT JOIN teachers t   ON t.id   = s.teacher_id
    LEFT JOIN subjects sub ON sub.id = s.subject_id
"""


# ---------------------------------------------------------------------------
# Queries
# ---------------------------------------------------------------------------

def get_session(session_id: int) -> Optional[SessionRow]:
    """Return one session by id, or None if not found."""
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            _SESSION_SELECT + " WHERE s.id = ?",
            (session_id,)
        )
        row = cur.fetchone()
    finally:
        conn.close()
    return _row_to_session(row) if row else None


def get_open_session() -> Optional[SessionRow]:
    """
    Return the currently OPEN session, or None if no session is open.

    Because only one session may be OPEN at a time, this returns at most
    one result.
    """
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            _SESSION_SELECT + " WHERE s.status = 'OPEN' LIMIT 1"
        )
        row = cur.fetchone()
    finally:
        conn.close()
    return _row_to_session(row) if row else None


def list_sessions(
    class_id   : Optional[int] = None,
    teacher_id : Optional[int] = None,
    status     : Optional[str] = None,
    session_date: Optional[str] = None,
) -> list:
    """
    Return sessions matching optional filter criteria.
    Results are ordered by session_date DESC, created_at DESC.
    """
    filters = []
    params  = []

    if class_id is not None:
        filters.append("s.class_id = ?")
        params.append(class_id)
    if teacher_id is not None:
        filters.append("s.teacher_id = ?")
        params.append(teacher_id)
    if status is not None:
        filters.append("s.status = ?")
        params.append(status)
    if session_date is not None:
        filters.append("s.session_date = ?")
        params.append(session_date)

    where = ("WHERE " + " AND ".join(filters)) if filters else ""
    sql = _SESSION_SELECT + f" {where} ORDER BY s.session_date DESC, s.created_at DESC"

    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(sql, params)
        rows = cur.fetchall()
    finally:
        conn.close()

    return [_row_to_session(r) for r in rows]


def get_session_attendance(session_id: int) -> list:
    """
    Return all attendance records for a session, with student details.
    Ordered by attendance_time ASC.
    """
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT
                a.id,
                a.session_id,
                a.student_id,
                st.first_name,
                st.last_name,
                st.registration_number,
                st.rfid_uid,
                a.attendance_time,
                a.status,
                a.created_at
            FROM attendance a
            JOIN students st ON st.id = a.student_id
            WHERE a.session_id = ?
            ORDER BY a.attendance_time ASC
            """,
            (session_id,)
        )
        rows = cur.fetchall()
    finally:
        conn.close()

    return [
        AttendanceRow(
            id                  = r["id"],
            session_id          = r["session_id"],
            student_id          = r["student_id"],
            first_name          = r["first_name"],
            last_name           = r["last_name"],
            registration_number = r["registration_number"],
            rfid_uid            = r["rfid_uid"],
            attendance_time     = r["attendance_time"],
            status              = r["status"],
            created_at          = r["created_at"],
        )
        for r in rows
    ]


# ---------------------------------------------------------------------------
# Mutations
# ---------------------------------------------------------------------------

def create_session(
    class_id     : int,
    session_date : Optional[str] = None,
    teacher_id   : Optional[int] = None,
    subject_id   : Optional[int] = None,
    start_time   : Optional[str] = None,
    end_time     : Optional[str] = None,
) -> SessionResult:
    """
    Create a new attendance session with status SCHEDULED.

    class_id is required (NOT NULL in schema).
    session_date defaults to today if not provided.
    All other fields are optional.
    """
    if session_date is None:
        session_date = _today()

    conn = get_connection()
    try:
        cur = conn.cursor()

        # Validate class exists.
        cur.execute("SELECT id FROM classes WHERE id = ?", (class_id,))
        if cur.fetchone() is None:
            return SessionResult(
                outcome=SessionOutcome.CLASS_NOT_FOUND,
                message=f"Class id={class_id} not found."
            )

        ts = _now_iso()
        try:
            with conn:
                cur.execute(
                    """
                    INSERT INTO attendance_sessions
                        (class_id, teacher_id, subject_id, session_date,
                         start_time, end_time, status, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, 'SCHEDULED', ?)
                    """,
                    (class_id, teacher_id, subject_id, session_date,
                     start_time, end_time, ts)
                )
                new_id = cur.lastrowid
        except sqlite3.IntegrityError as exc:
            return SessionResult(
                outcome=SessionOutcome.ERROR,
                message=f"Database error creating session: {exc}"
            )

    finally:
        conn.close()

    session = get_session(new_id)
    return SessionResult(
        outcome=SessionOutcome.CREATED,
        session=session,
        message=f"Session created (id={new_id}, date={session_date})."
    )


def open_session(session_id: int) -> SessionResult:
    """
    Transition a session from SCHEDULED or CLOSED to OPEN.

    One-open-at-a-time rule:
        If any other session is already OPEN, return ALREADY_OPEN_EXISTS
        with the conflicting session. The caller must close it first.

    Allowed transitions:
        SCHEDULED -> OPEN
        CLOSED    -> OPEN  (reopen)

    Rejected transitions:
        OPEN      -> OPEN      (already open)
        COMPLETED -> OPEN      (permanent)
    """
    conn = get_connection()
    try:
        cur = conn.cursor()

        # Load the target session.
        cur.execute(
            _SESSION_SELECT + " WHERE s.id = ?", (session_id,)
        )
        row = cur.fetchone()
        if row is None:
            return SessionResult(
                outcome=SessionOutcome.NOT_FOUND,
                message=f"Session id={session_id} not found."
            )
        session = _row_to_session(row)

        # Guard: completed sessions cannot be reopened.
        if session.status == "COMPLETED":
            return SessionResult(
                outcome=SessionOutcome.INVALID_TRANSITION,
                session=session,
                message=(
                    f"Session id={session_id} is COMPLETED and cannot "
                    f"be reopened."
                )
            )

        # Guard: already open.
        if session.status == "OPEN":
            return SessionResult(
                outcome=SessionOutcome.ALREADY_OPEN_EXISTS,
                session=session,
                message=f"Session id={session_id} is already OPEN."
            )

        # One-open-at-a-time: check for any other open session.
        cur.execute(
            _SESSION_SELECT + " WHERE s.status = 'OPEN' AND s.id != ? LIMIT 1",
            (session_id,)
        )
        conflict_row = cur.fetchone()
        if conflict_row is not None:
            conflicting = _row_to_session(conflict_row)
            return SessionResult(
                outcome=SessionOutcome.ALREADY_OPEN_EXISTS,
                session=session,
                conflicting=conflicting,
                message=(
                    f"Session id={conflicting.id} is already OPEN "
                    f"({conflicting.class_name or 'unknown class'}). "
                    f"Close it before opening another session."
                )
            )

        # Transition to OPEN.
        # Clear closed_at so a reopened session does not retain a stale
        # closed timestamp. opened_at is only set on the first open —
        # preserve the original if already set, so we know when the session
        # was first opened.
        ts = _now_iso()
        with conn:
            conn.execute(
                "UPDATE attendance_sessions "
                "SET status = 'OPEN', "
                "    opened_at = COALESCE(opened_at, ?), "
                "    closed_at = NULL "
                "WHERE id = ?",
                (ts, session_id)
            )

    finally:
        conn.close()

    session = get_session(session_id)
    return SessionResult(
        outcome=SessionOutcome.OPENED,
        session=session,
        message=f"Session id={session_id} is now OPEN."
    )


def close_session(session_id: int) -> SessionResult:
    """
    Transition a session from OPEN to CLOSED.

    Allowed transitions:
        OPEN -> CLOSED

    Rejected:
        SCHEDULED  -> CLOSED  (never opened)
        CLOSED     -> CLOSED  (already closed)
        COMPLETED  -> CLOSED  (permanent)
    """
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            _SESSION_SELECT + " WHERE s.id = ?", (session_id,)
        )
        row = cur.fetchone()
        if row is None:
            return SessionResult(
                outcome=SessionOutcome.NOT_FOUND,
                message=f"Session id={session_id} not found."
            )
        session = _row_to_session(row)

        if session.status != "OPEN":
            return SessionResult(
                outcome=SessionOutcome.INVALID_TRANSITION,
                session=session,
                message=(
                    f"Session id={session_id} is {session.status}. "
                    f"Only OPEN sessions can be closed."
                )
            )

        ts = _now_iso()
        with conn:
            conn.execute(
                "UPDATE attendance_sessions "
                "SET status = 'CLOSED', closed_at = ? "
                "WHERE id = ?",
                (ts, session_id)
            )

    finally:
        conn.close()

    session = get_session(session_id)
    return SessionResult(
        outcome=SessionOutcome.CLOSED,
        session=session,
        message=f"Session id={session_id} is now CLOSED."
    )


def complete_session(session_id: int) -> SessionResult:
    """
    Permanently complete a session. No further attendance is accepted.

    Allowed transitions:
        OPEN   -> COMPLETED
        CLOSED -> COMPLETED

    Rejected:
        SCHEDULED  -> COMPLETED  (was never opened)
        COMPLETED  -> COMPLETED  (already done)
    """
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            _SESSION_SELECT + " WHERE s.id = ?", (session_id,)
        )
        row = cur.fetchone()
        if row is None:
            return SessionResult(
                outcome=SessionOutcome.NOT_FOUND,
                message=f"Session id={session_id} not found."
            )
        session = _row_to_session(row)

        if session.status == "COMPLETED":
            return SessionResult(
                outcome=SessionOutcome.INVALID_TRANSITION,
                session=session,
                message=f"Session id={session_id} is already COMPLETED."
            )

        if session.status == "SCHEDULED":
            return SessionResult(
                outcome=SessionOutcome.INVALID_TRANSITION,
                session=session,
                message=(
                    f"Session id={session_id} is SCHEDULED and has never "
                    f"been opened. Open it before completing."
                )
            )

        ts = _now_iso()
        with conn:
            conn.execute(
                "UPDATE attendance_sessions "
                "SET status = 'COMPLETED', completed_at = ? "
                "WHERE id = ?",
                (ts, session_id)
            )

    finally:
        conn.close()

    session = get_session(session_id)
    return SessionResult(
        outcome=SessionOutcome.COMPLETED,
        session=session,
        message=f"Session id={session_id} is now COMPLETED."
    )
