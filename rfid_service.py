"""
rfid_service.py — RFID card lookup, assignment, and attendance service.

Works against the simplified schema:
    students.rfid_uid  (nullable, UNIQUE)

Public API:

    # Primary scan entry point (used by the RFID listener integration)
    process_scan(uid)                 -> ScanResult
        Finds the open session automatically, validates the student,
        checks class membership, prevents duplicates, records attendance.

    # Card lookup
    find_student_by_uid(uid)          -> StudentLookupResult
    find_student_by_id(student_id)    -> StudentLookupResult | None

    # Attendance (when caller already knows the session)
    handle_attendance_scan(uid, session_id) -> ScanResult

    # Card management (require explicit teacher confirmation before calling)
    assign_card(uid, student_id)      -> AssignmentResult
    reassign_card(uid, student_id)    -> AssignmentResult
    unassign_card(student_id)         -> AssignmentResult

Scan result types (ScanOutcome):
    SUCCESS         attendance recorded
    DUPLICATE       student already marked present in this session
    UNKNOWN_CARD    UID not assigned to any student
    WRONG_CLASS     student is not in the session's class
    NO_OPEN_SESSION no session is currently OPEN
    ERROR           unexpected failure

Rules enforced here:
    - Unknown cards do NOT create students or card assignments.
    - A student from the wrong class is rejected.
    - Only OPEN sessions accept attendance.
    - UNIQUE(session_id, student_id) is enforced by the DB; the service
      also checks first and returns a clear DUPLICATE result.
    - A UID already owned by another student is NEVER silently moved.

This module does NOT:
    - Open serial ports.
    - Implement web/HTTP handling.
    - Implement authentication.
"""

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Optional

from database import get_connection
from rfid_listener import normalize_uid


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")


def _fetch_student_by_id(cur: sqlite3.Cursor,
                          student_id: int) -> Optional[sqlite3.Row]:
    cur.execute(
        "SELECT id, first_name, last_name, registration_number, "
        "       rfid_uid, class_id, status "
        "FROM students WHERE id = ?",
        (student_id,)
    )
    return cur.fetchone()


# ---------------------------------------------------------------------------
# Student lookup result
# ---------------------------------------------------------------------------

class LookupStatus(Enum):
    FOUND     = "FOUND"      # UID belongs to an active student
    NOT_FOUND = "NOT_FOUND"  # UID not assigned to any student
    INACTIVE  = "INACTIVE"   # UID belongs to an inactive student


@dataclass
class StudentLookupResult:
    uid                 : str
    status              : LookupStatus
    student_id          : Optional[int] = None
    first_name          : Optional[str] = None
    last_name           : Optional[str] = None
    registration_number : Optional[str] = None
    class_id            : Optional[int] = None
    message             : str = ""

    @property
    def full_name(self) -> Optional[str]:
        if self.first_name is None:
            return None
        return f"{self.first_name} {self.last_name}".strip()

    def found(self) -> bool:
        return self.status == LookupStatus.FOUND

    def __str__(self):
        parts = [f"uid={self.uid}", f"status={self.status.value}"]
        if self.full_name:
            parts.append(f"student={self.full_name!r}")
        parts.append(f"msg={self.message!r}")
        return " | ".join(parts)


# ---------------------------------------------------------------------------
# Scan result  (unified result for all RFID attendance scan paths)
# ---------------------------------------------------------------------------

class ScanOutcome(Enum):
    SUCCESS         = "SUCCESS"         # attendance recorded
    DUPLICATE       = "DUPLICATE"       # student already present this session
    UNKNOWN_CARD    = "UNKNOWN_CARD"    # UID not assigned to any student
    WRONG_CLASS     = "WRONG_CLASS"     # student not in the session's class
    NO_OPEN_SESSION = "NO_OPEN_SESSION" # no session is currently OPEN
    ERROR           = "ERROR"           # unexpected failure


@dataclass
class ScanResult:
    """
    Structured result returned for every RFID card scan attempt.

    Contains enough information for the frontend to display an immediate
    response without any additional queries.
    """
    outcome             : ScanOutcome
    uid                 : str
    # Session fields (populated whenever a session was found)
    session_id          : Optional[int] = None
    session_class_id    : Optional[int] = None
    session_class_name  : Optional[str] = None
    session_subject     : Optional[str] = None
    session_date        : Optional[str] = None
    # Student fields (populated when the card is recognised)
    student_id          : Optional[int] = None
    first_name          : Optional[str] = None
    last_name           : Optional[str] = None
    registration_number : Optional[str] = None
    student_class_id    : Optional[int] = None
    # Attendance fields (populated on SUCCESS)
    attendance_time     : Optional[str] = None
    # Human-readable explanation
    message             : str = ""

    @property
    def full_name(self) -> Optional[str]:
        if self.first_name is None:
            return None
        return f"{self.first_name} {self.last_name}".strip()

    def success(self) -> bool:
        return self.outcome == ScanOutcome.SUCCESS

    def to_dict(self) -> dict:
        return {
            "outcome"            : self.outcome.value,
            "uid"                : self.uid,
            "session_id"         : self.session_id,
            "session_class_id"   : self.session_class_id,
            "session_class_name" : self.session_class_name,
            "session_subject"    : self.session_subject,
            "session_date"       : self.session_date,
            "student_id"         : self.student_id,
            "first_name"         : self.first_name,
            "last_name"          : self.last_name,
            "full_name"          : self.full_name,
            "registration_number": self.registration_number,
            "attendance_time"    : self.attendance_time,
            "message"            : self.message,
        }

    def __str__(self):
        parts = [f"outcome={self.outcome.value}", f"uid={self.uid}"]
        if self.full_name:
            parts.append(f"student={self.full_name!r}")
        if self.session_id:
            parts.append(f"session={self.session_id}")
        parts.append(f"msg={self.message!r}")
        return " | ".join(parts)


# Keep the old name as an alias so existing rfid_test.py imports keep working.
AttendanceStatus = ScanOutcome
AttendanceScanResult = ScanResult


# ---------------------------------------------------------------------------
# Assignment result
# ---------------------------------------------------------------------------

class AssignmentOutcome(Enum):
    ASSIGNED          = "ASSIGNED"           # UID written to student
    REASSIGNED        = "REASSIGNED"         # UID moved from old student to new
    UNASSIGNED        = "UNASSIGNED"         # rfid_uid set to NULL
    UID_TAKEN         = "UID_TAKEN"          # UID owned by a different student
    STUDENT_NOT_FOUND = "STUDENT_NOT_FOUND"
    STUDENT_INACTIVE  = "STUDENT_INACTIVE"
    DB_CONFLICT       = "DB_CONFLICT"        # unexpected constraint violation
    ERROR             = "ERROR"


@dataclass
class AssignmentResult:
    outcome             : AssignmentOutcome
    uid                 : Optional[str]
    student_id          : Optional[int] = None
    student_name        : Optional[str] = None
    previous_owner_id   : Optional[int] = None
    previous_owner_name : Optional[str] = None
    message             : str = ""

    def success(self) -> bool:
        return self.outcome in (
            AssignmentOutcome.ASSIGNED,
            AssignmentOutcome.REASSIGNED,
            AssignmentOutcome.UNASSIGNED,
        )

    def __str__(self):
        parts = [f"outcome={self.outcome.value}"]
        if self.uid:
            parts.append(f"uid={self.uid}")
        if self.student_name:
            parts.append(f"student={self.student_name!r}")
        if self.previous_owner_name:
            parts.append(f"prev_owner={self.previous_owner_name!r}")
        parts.append(f"msg={self.message!r}")
        return " | ".join(parts)


# ---------------------------------------------------------------------------
# Card lookup
# ---------------------------------------------------------------------------

def find_student_by_uid(uid: str) -> StudentLookupResult:
    """
    Find the student who owns a given RFID UID.

    The UID is normalized before the query.

    Returns:
        FOUND     — an active student owns this UID.
        NOT_FOUND — no student has this UID (card is unassigned or unknown).
        INACTIVE  — a student owns it but their account is inactive.
    """
    uid = normalize_uid(uid)

    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT id, first_name, last_name, registration_number, "
            "       class_id, status "
            "FROM students WHERE rfid_uid = ?",
            (uid,)
        )
        row = cur.fetchone()
    finally:
        conn.close()

    if row is None:
        return StudentLookupResult(
            uid=uid, status=LookupStatus.NOT_FOUND,
            message=f"UID {uid} is not assigned to any student."
        )

    if row["status"] != "active":
        return StudentLookupResult(
            uid=uid, status=LookupStatus.INACTIVE,
            student_id=row["id"],
            first_name=row["first_name"], last_name=row["last_name"],
            registration_number=row["registration_number"],
            class_id=row["class_id"],
            message=(
                f"UID {uid} belongs to "
                f"{row['first_name']} {row['last_name']} "
                f"but that student is inactive."
            )
        )

    return StudentLookupResult(
        uid=uid, status=LookupStatus.FOUND,
        student_id=row["id"],
        first_name=row["first_name"], last_name=row["last_name"],
        registration_number=row["registration_number"],
        class_id=row["class_id"],
        message=(
            f"UID {uid} belongs to "
            f"{row['first_name']} {row['last_name']} "
            f"({row['registration_number']})."
        )
    )


def find_student_by_id(student_id: int) -> Optional[StudentLookupResult]:
    """
    Look up a student by their database id.

    Returns a StudentLookupResult if found, or None if no such student exists.
    The uid field on the result reflects the student's current rfid_uid.
    """
    conn = get_connection()
    try:
        cur = conn.cursor()
        row = _fetch_student_by_id(cur, student_id)
    finally:
        conn.close()

    if row is None:
        return None

    status = (LookupStatus.FOUND if row["status"] == "active"
              else LookupStatus.INACTIVE)

    return StudentLookupResult(
        uid=row["rfid_uid"] or "",
        status=status,
        student_id=row["id"],
        first_name=row["first_name"], last_name=row["last_name"],
        registration_number=row["registration_number"],
        class_id=row["class_id"],
        message=f"Student id={student_id}."
    )


# ---------------------------------------------------------------------------
# Primary scan entry point
# ---------------------------------------------------------------------------

def process_scan(uid: str) -> ScanResult:
    """
    Process an RFID card scan from the physical reader.

    This is the main entry point called by the RFID listener integration.
    It finds the currently open session automatically — the caller does
    not need to know which session is active.

    Validation sequence:
        1. Normalize UID.
        2. Find the currently OPEN session.
           - None found  -> NO_OPEN_SESSION
        3. Find the student who owns this UID.
           - Not found   -> UNKNOWN_CARD  (nothing written to DB)
        4. Check that the student's class matches the session's class.
           - Mismatch    -> WRONG_CLASS   (nothing written to DB)
        5. Check for duplicate attendance in this session.
           - Exists      -> DUPLICATE
        6. Insert attendance record.
        7. Return SUCCESS with full details.
    """
    from session_service import get_open_session

    uid = normalize_uid(uid)
    ts  = _now_iso()

    # Step 2 — find the open session.
    open_session = get_open_session()
    if open_session is None:
        return ScanResult(
            outcome=ScanOutcome.NO_OPEN_SESSION,
            uid=uid,
            message="No session is currently open. Ask the teacher to open a session."
        )

    return handle_attendance_scan(uid, open_session.id, _ts=ts)


# ---------------------------------------------------------------------------
# Attendance scan (explicit session_id)
# ---------------------------------------------------------------------------

def handle_attendance_scan(uid: str, session_id: int,
                           _ts: Optional[str] = None) -> ScanResult:
    """
    Process a card scan for a specific session.

    Used when the caller already knows which session to record against.
    process_scan() calls this internally after resolving the open session.

    Validation sequence:
        1. Normalize UID.
        2. Find the student who owns this UID.
           - Not found   -> UNKNOWN_CARD
        3. Verify the session exists and is OPEN.
        4. Check that the student's class matches the session's class.
           - Mismatch    -> WRONG_CLASS
        5. Check for duplicate.
           - Exists      -> DUPLICATE
        6. Insert attendance record.
        7. Return SUCCESS.
    """
    uid = normalize_uid(uid)
    ts  = _ts or _now_iso()

    # Step 1 — find student.
    lookup = find_student_by_uid(uid)

    if lookup.status != LookupStatus.FOUND:
        return ScanResult(
            outcome=ScanOutcome.UNKNOWN_CARD,
            uid=uid,
            session_id=session_id,
            message=(
                f"Unknown card {uid}. Not assigned to any active student."
                if lookup.status == LookupStatus.NOT_FOUND
                else f"Card {uid} belongs to an inactive student."
            )
        )

    student_id = lookup.student_id

    conn = get_connection()
    try:
        cur = conn.cursor()

        # Step 2 — verify session exists and is OPEN.
        cur.execute(
            """
            SELECT s.id, s.status, s.class_id, c.name AS class_name,
                   sub.name AS subject_name, s.session_date
            FROM attendance_sessions s
            LEFT JOIN classes  c   ON c.id   = s.class_id
            LEFT JOIN subjects sub ON sub.id = s.subject_id
            WHERE s.id = ?
            """,
            (session_id,)
        )
        session = cur.fetchone()

        if session is None:
            return ScanResult(
                outcome=ScanOutcome.ERROR,
                uid=uid, session_id=session_id,
                student_id=student_id,
                first_name=lookup.first_name, last_name=lookup.last_name,
                message=f"Session id={session_id} not found."
            )

        if session["status"] != "OPEN":
            return ScanResult(
                outcome=ScanOutcome.NO_OPEN_SESSION,
                uid=uid, session_id=session_id,
                session_class_id=session["class_id"],
                session_class_name=session["class_name"],
                session_subject=session["subject_name"],
                session_date=session["session_date"],
                student_id=student_id,
                first_name=lookup.first_name, last_name=lookup.last_name,
                message=(
                    f"Session id={session_id} is {session['status']}. "
                    f"Only OPEN sessions accept attendance."
                )
            )

        # Step 3 — class check.
        # A student must belong to the same class as the session.
        if lookup.class_id != session["class_id"]:
            return ScanResult(
                outcome=ScanOutcome.WRONG_CLASS,
                uid=uid, session_id=session_id,
                session_class_id=session["class_id"],
                session_class_name=session["class_name"],
                session_subject=session["subject_name"],
                session_date=session["session_date"],
                student_id=student_id,
                first_name=lookup.first_name, last_name=lookup.last_name,
                registration_number=lookup.registration_number,
                student_class_id=lookup.class_id,
                message=(
                    f"{lookup.full_name} is not enrolled in "
                    f"{session['class_name'] or 'this class'} "
                    f"(student class_id={lookup.class_id}, "
                    f"session class_id={session['class_id']})."
                )
            )

        # Step 4 — duplicate check.
        cur.execute(
            "SELECT id FROM attendance "
            "WHERE session_id = ? AND student_id = ?",
            (session_id, student_id)
        )
        if cur.fetchone() is not None:
            return ScanResult(
                outcome=ScanOutcome.DUPLICATE,
                uid=uid, session_id=session_id,
                session_class_id=session["class_id"],
                session_class_name=session["class_name"],
                session_subject=session["subject_name"],
                session_date=session["session_date"],
                student_id=student_id,
                first_name=lookup.first_name, last_name=lookup.last_name,
                registration_number=lookup.registration_number,
                message=(
                    f"{lookup.full_name} is already marked present "
                    f"in this session."
                )
            )

        # Step 5 — insert attendance.
        try:
            with conn:
                conn.execute(
                    "INSERT INTO attendance "
                    "(session_id, student_id, attendance_time, status, created_at) "
                    "VALUES (?, ?, ?, 'present', ?)",
                    (session_id, student_id, ts, ts)
                )
        except sqlite3.IntegrityError:
            # DB unique constraint fired — concurrent request.
            return ScanResult(
                outcome=ScanOutcome.DUPLICATE,
                uid=uid, session_id=session_id,
                session_class_id=session["class_id"],
                session_class_name=session["class_name"],
                student_id=student_id,
                first_name=lookup.first_name, last_name=lookup.last_name,
                registration_number=lookup.registration_number,
                message=f"{lookup.full_name} already recorded (DB constraint)."
            )

    finally:
        conn.close()

    return ScanResult(
        outcome=ScanOutcome.SUCCESS,
        uid=uid, session_id=session_id,
        session_class_id=session["class_id"],
        session_class_name=session["class_name"],
        session_subject=session["subject_name"],
        session_date=session["session_date"],
        student_id=student_id,
        first_name=lookup.first_name, last_name=lookup.last_name,
        registration_number=lookup.registration_number,
        attendance_time=ts,
        message=f"Attendance recorded for {lookup.full_name}."
    )


# ---------------------------------------------------------------------------
# Card assignment
# ---------------------------------------------------------------------------

def assign_card(uid: str, student_id: int) -> AssignmentResult:
    """
    Assign a UID to a student who currently has no card.

    Rules:
      - Student must exist and be active.
      - UID must NOT already be assigned to ANY other student.
        If it is, return UID_TAKEN with the current owner's details —
        the teacher must call reassign_card() explicitly.
      - If the student already has this exact UID, succeeds (idempotent).
      - If the student already has a DIFFERENT UID, return UID_TAKEN
        (use reassign_card to replace).

    The UID is normalized before writing.
    """
    uid = normalize_uid(uid)

    conn = get_connection()
    try:
        cur = conn.cursor()

        student = _fetch_student_by_id(cur, student_id)
        if student is None:
            return AssignmentResult(
                outcome=AssignmentOutcome.STUDENT_NOT_FOUND,
                uid=uid, student_id=student_id,
                message=f"Student id={student_id} not found."
            )
        if student["status"] != "active":
            return AssignmentResult(
                outcome=AssignmentOutcome.STUDENT_INACTIVE,
                uid=uid, student_id=student_id,
                student_name=f"{student['first_name']} {student['last_name']}",
                message=(
                    f"Student '{student['first_name']} {student['last_name']}' "
                    f"is inactive and cannot be assigned a card."
                )
            )

        # Idempotent: student already has exactly this UID.
        if student["rfid_uid"] == uid:
            return AssignmentResult(
                outcome=AssignmentOutcome.ASSIGNED,
                uid=uid, student_id=student_id,
                student_name=f"{student['first_name']} {student['last_name']}",
                message=f"UID {uid} already assigned to this student (no change)."
            )

        # Check if the UID is already owned by a different student.
        cur.execute(
            "SELECT id, first_name, last_name FROM students "
            "WHERE rfid_uid = ? AND id != ?",
            (uid, student_id)
        )
        owner = cur.fetchone()
        if owner is not None:
            return AssignmentResult(
                outcome=AssignmentOutcome.UID_TAKEN,
                uid=uid, student_id=student_id,
                student_name=f"{student['first_name']} {student['last_name']}",
                previous_owner_id=owner["id"],
                previous_owner_name=(
                    f"{owner['first_name']} {owner['last_name']}"
                ),
                message=(
                    f"UID {uid} is already assigned to "
                    f"'{owner['first_name']} {owner['last_name']}' "
                    f"(id={owner['id']}). "
                    f"Use reassign_card() to move it explicitly."
                )
            )

        # Check if the student already has a different card.
        if student["rfid_uid"] is not None:
            return AssignmentResult(
                outcome=AssignmentOutcome.UID_TAKEN,
                uid=uid, student_id=student_id,
                student_name=f"{student['first_name']} {student['last_name']}",
                message=(
                    f"Student '{student['first_name']} {student['last_name']}' "
                    f"already has card {student['rfid_uid']}. "
                    f"Use reassign_card() to replace it."
                )
            )

        # Assign.
        try:
            with conn:
                conn.execute(
                    "UPDATE students SET rfid_uid = ? WHERE id = ?",
                    (uid, student_id)
                )
        except sqlite3.IntegrityError as exc:
            return AssignmentResult(
                outcome=AssignmentOutcome.DB_CONFLICT,
                uid=uid, student_id=student_id,
                student_name=f"{student['first_name']} {student['last_name']}",
                message=f"Database constraint prevented assignment: {exc}"
            )

    finally:
        conn.close()

    return AssignmentResult(
        outcome=AssignmentOutcome.ASSIGNED,
        uid=uid, student_id=student_id,
        student_name=f"{student['first_name']} {student['last_name']}",
        message=(
            f"Card {uid} assigned to "
            f"'{student['first_name']} {student['last_name']}'."
        )
    )


def reassign_card(uid: str, student_id: int) -> AssignmentResult:
    """
    Move a UID to a student, clearing it from any previous owner first.

    Use this when:
      - The teacher explicitly confirms moving a card from student A to B.
      - A student is getting a replacement card (clearing their old one).

    Rules:
      - Target student must exist and be active.
      - If the UID is currently owned by another student, that student's
        rfid_uid is set to NULL before assigning to the new student.
        This never happens silently — the caller is responsible for
        confirming with the teacher before calling this function.
      - Target student's existing card (if any) is cleared first.
      - All changes run in one transaction.
    """
    uid = normalize_uid(uid)

    conn = get_connection()
    try:
        cur = conn.cursor()

        target = _fetch_student_by_id(cur, student_id)
        if target is None:
            return AssignmentResult(
                outcome=AssignmentOutcome.STUDENT_NOT_FOUND,
                uid=uid, student_id=student_id,
                message=f"Student id={student_id} not found."
            )
        if target["status"] != "active":
            return AssignmentResult(
                outcome=AssignmentOutcome.STUDENT_INACTIVE,
                uid=uid, student_id=student_id,
                student_name=f"{target['first_name']} {target['last_name']}",
                message=(
                    f"Student '{target['first_name']} {target['last_name']}' "
                    f"is inactive."
                )
            )

        # Find current owner of this UID (if any, and if not the target).
        cur.execute(
            "SELECT id, first_name, last_name FROM students "
            "WHERE rfid_uid = ? AND id != ?",
            (uid, student_id)
        )
        prev_owner = cur.fetchone()
        prev_owner_id   = prev_owner["id"]   if prev_owner else None
        prev_owner_name = (
            f"{prev_owner['first_name']} {prev_owner['last_name']}"
            if prev_owner else None
        )

        try:
            with conn:
                # Clear the UID from any previous owner.
                if prev_owner is not None:
                    conn.execute(
                        "UPDATE students SET rfid_uid = NULL WHERE id = ?",
                        (prev_owner["id"],)
                    )
                # Clear any different card the target already has.
                # (Target's existing card is released; new card is assigned.)
                conn.execute(
                    "UPDATE students SET rfid_uid = ? WHERE id = ?",
                    (uid, student_id)
                )
        except sqlite3.IntegrityError as exc:
            return AssignmentResult(
                outcome=AssignmentOutcome.DB_CONFLICT,
                uid=uid, student_id=student_id,
                student_name=f"{target['first_name']} {target['last_name']}",
                message=f"Database constraint prevented reassignment: {exc}"
            )

    finally:
        conn.close()

    return AssignmentResult(
        outcome=AssignmentOutcome.REASSIGNED,
        uid=uid, student_id=student_id,
        student_name=f"{target['first_name']} {target['last_name']}",
        previous_owner_id=prev_owner_id,
        previous_owner_name=prev_owner_name,
        message=(
            f"Card {uid} reassigned to "
            f"'{target['first_name']} {target['last_name']}'."
            + (f" Removed from '{prev_owner_name}'." if prev_owner_name else "")
        )
    )


def unassign_card(student_id: int) -> AssignmentResult:
    """
    Remove the RFID UID from a student (sets rfid_uid = NULL).

    Used when a card is lost or the teacher removes a card assignment.
    """
    conn = get_connection()
    try:
        cur = conn.cursor()
        student = _fetch_student_by_id(cur, student_id)
        if student is None:
            return AssignmentResult(
                outcome=AssignmentOutcome.STUDENT_NOT_FOUND,
                uid=None, student_id=student_id,
                message=f"Student id={student_id} not found."
            )
        old_uid = student["rfid_uid"]
        with conn:
            conn.execute(
                "UPDATE students SET rfid_uid = NULL WHERE id = ?",
                (student_id,)
            )
    finally:
        conn.close()

    return AssignmentResult(
        outcome=AssignmentOutcome.UNASSIGNED,
        uid=old_uid, student_id=student_id,
        student_name=f"{student['first_name']} {student['last_name']}",
        message=(
            f"Card {old_uid} removed from "
            f"'{student['first_name']} {student['last_name']}'."
        )
    )
