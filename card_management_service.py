"""
card_management_service.py — RFID card management workflow.

This module implements the two-phase card assignment workflow:

    Phase 1 — CHECK
        check_uid_for_assignment(uid, student_id)
        Reads the database and returns a CardCheckResult describing what
        would happen. Nothing is written to the database.

    Phase 2 — CONFIRM or CANCEL
        confirm_assignment(uid, student_id)   -> writes to DB
        cancel_assignment()                   -> no-op, returns confirmation

The two-phase design ensures the teacher always sees who currently owns
a card before any reassignment happens.  Silent reassignment is impossible.

Student management:
    create_student(...)     -> StudentResult
    list_students(...)      -> list[StudentRecord]
    get_student(id)         -> StudentRecord | None
    update_student(...)     -> StudentResult

Public card-status queries:
    students_with_cards()   -> list[StudentRecord]
    students_without_cards() -> list[StudentRecord]

All card write operations ultimately delegate to rfid_service.assign_card()
or rfid_service.reassign_card(), which enforce the database constraints.
"""

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Optional

from database import get_connection
from rfid_listener import normalize_uid
from rfid_service import (
    find_student_by_uid,
    find_student_by_id,
    assign_card,
    reassign_card,
    unassign_card,
    LookupStatus,
    AssignmentOutcome,
    AssignmentResult,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")


# ---------------------------------------------------------------------------
# Student record (read-only DTO)
# ---------------------------------------------------------------------------

@dataclass
class StudentRecord:
    """A full student row with optional joined class name."""
    id                  : int
    first_name          : str
    last_name           : str
    registration_number : str
    rfid_uid            : Optional[str]
    class_id            : Optional[int]
    class_name          : Optional[str]
    status              : str
    created_at          : str

    @property
    def full_name(self) -> str:
        return f"{self.first_name} {self.last_name}".strip()

    @property
    def has_card(self) -> bool:
        return self.rfid_uid is not None

    def to_dict(self) -> dict:
        return {
            "id"                 : self.id,
            "first_name"         : self.first_name,
            "last_name"          : self.last_name,
            "full_name"          : self.full_name,
            "registration_number": self.registration_number,
            "rfid_uid"           : self.rfid_uid,
            "has_card"           : self.has_card,
            "class_id"           : self.class_id,
            "class_name"         : self.class_name,
            "status"             : self.status,
            "created_at"         : self.created_at,
        }


# ---------------------------------------------------------------------------
# Student CRUD results
# ---------------------------------------------------------------------------

class StudentOutcome(Enum):
    CREATED              = "CREATED"
    UPDATED              = "UPDATED"
    NOT_FOUND            = "NOT_FOUND"
    DUPLICATE_REG_NUMBER = "DUPLICATE_REG_NUMBER"
    INVALID_CLASS        = "INVALID_CLASS"
    ERROR                = "ERROR"


@dataclass
class StudentResult:
    outcome  : StudentOutcome
    student  : Optional[StudentRecord] = None
    message  : str = ""

    def success(self) -> bool:
        return self.outcome in (
            StudentOutcome.CREATED,
            StudentOutcome.UPDATED,
        )


# ---------------------------------------------------------------------------
# Card check result (Phase 1 — read-only)
# ---------------------------------------------------------------------------

class CardCheckOutcome(Enum):
    UNASSIGNED          = "UNASSIGNED"          # card is free; safe to assign
    ALREADY_ASSIGNED_SELF = "ALREADY_ASSIGNED_SELF" # card already belongs to this student
    OWNED_BY_OTHER      = "OWNED_BY_OTHER"      # card belongs to another student
    STUDENT_NOT_FOUND   = "STUDENT_NOT_FOUND"
    STUDENT_INACTIVE    = "STUDENT_INACTIVE"
    ERROR               = "ERROR"


@dataclass
class CardCheckResult:
    """
    Result of checking a UID before committing an assignment.

    Nothing is written to the database when this is returned.
    The teacher reads this, decides, then calls confirm_assignment() or
    cancel_assignment().
    """
    outcome              : CardCheckOutcome
    uid                  : str
    # The student the teacher selected.
    target_student_id    : Optional[int]  = None
    target_student_name  : Optional[str]  = None
    target_reg_number    : Optional[str]  = None
    # The student who currently owns the card (if any).
    current_owner_id     : Optional[int]  = None
    current_owner_name   : Optional[str]  = None
    current_owner_reg    : Optional[str]  = None
    message              : str = ""

    def requires_confirmation(self) -> bool:
        """True if the teacher must explicitly confirm before assigning."""
        return self.outcome in (
            CardCheckOutcome.UNASSIGNED,
            CardCheckOutcome.OWNED_BY_OTHER,
        )

    def is_no_op(self) -> bool:
        """True if assigning this card would change nothing."""
        return self.outcome == CardCheckOutcome.ALREADY_ASSIGNED_SELF

    def to_dict(self) -> dict:
        return {
            "outcome"             : self.outcome.value,
            "uid"                 : self.uid,
            "target_student_id"   : self.target_student_id,
            "target_student_name" : self.target_student_name,
            "target_reg_number"   : self.target_reg_number,
            "current_owner_id"    : self.current_owner_id,
            "current_owner_name"  : self.current_owner_name,
            "current_owner_reg"   : self.current_owner_reg,
            "requires_confirmation": self.requires_confirmation(),
            "message"             : self.message,
        }

    def __str__(self):
        parts = [f"outcome={self.outcome.value}", f"uid={self.uid}"]
        if self.target_student_name:
            parts.append(f"target={self.target_student_name!r}")
        if self.current_owner_name:
            parts.append(f"current_owner={self.current_owner_name!r}")
        parts.append(f"msg={self.message!r}")
        return " | ".join(parts)


# ---------------------------------------------------------------------------
# Phase 1 — Check
# ---------------------------------------------------------------------------

def check_uid_for_assignment(uid: str,
                              student_id: int) -> CardCheckResult:
    """
    Phase 1: inspect the current state of a UID without modifying anything.

    Returns a CardCheckResult describing one of four situations:

        UNASSIGNED
            The card is free. Safe to assign to the selected student.
            Teacher must still call confirm_assignment() to write the change.

        ALREADY_ASSIGNED_SELF
            The card already belongs to the selected student.
            No further action needed; confirm_assignment() would be a no-op.

        OWNED_BY_OTHER
            The card belongs to a different student.
            Shows the current owner so the teacher can decide.
            Only reassign by calling confirm_assignment() explicitly.

        STUDENT_NOT_FOUND / STUDENT_INACTIVE / ERROR
            Cannot proceed — explain why.
    """
    uid = normalize_uid(uid)

    # Validate the target student.
    target = find_student_by_id(student_id)

    if target is None:
        return CardCheckResult(
            outcome=CardCheckOutcome.STUDENT_NOT_FOUND,
            uid=uid, target_student_id=student_id,
            message=f"Student id={student_id} not found."
        )

    if target.status != LookupStatus.FOUND:
        return CardCheckResult(
            outcome=CardCheckOutcome.STUDENT_INACTIVE,
            uid=uid,
            target_student_id=student_id,
            target_student_name=target.full_name,
            target_reg_number=target.registration_number,
            message=(
                f"Student '{target.full_name}' is inactive "
                f"and cannot be assigned a card."
            )
        )

    # Check who currently owns this UID.
    existing = find_student_by_uid(uid)

    if existing.status == LookupStatus.NOT_FOUND:
        # Card is unassigned — clean assignment.
        return CardCheckResult(
            outcome=CardCheckOutcome.UNASSIGNED,
            uid=uid,
            target_student_id=student_id,
            target_student_name=target.full_name,
            target_reg_number=target.registration_number,
            message=(
                f"Card {uid} is not currently assigned to anyone. "
                f"Confirm to assign it to '{target.full_name}'."
            )
        )

    if existing.status == LookupStatus.FOUND:
        if existing.student_id == student_id:
            # Already belongs to this student — nothing to do.
            return CardCheckResult(
                outcome=CardCheckOutcome.ALREADY_ASSIGNED_SELF,
                uid=uid,
                target_student_id=student_id,
                target_student_name=target.full_name,
                target_reg_number=target.registration_number,
                current_owner_id=student_id,
                current_owner_name=target.full_name,
                current_owner_reg=target.registration_number,
                message=(
                    f"Card {uid} is already assigned to "
                    f"'{target.full_name}'. No change needed."
                )
            )
        else:
            # Belongs to someone else — requires explicit confirmation.
            return CardCheckResult(
                outcome=CardCheckOutcome.OWNED_BY_OTHER,
                uid=uid,
                target_student_id=student_id,
                target_student_name=target.full_name,
                target_reg_number=target.registration_number,
                current_owner_id=existing.student_id,
                current_owner_name=existing.full_name,
                current_owner_reg=existing.registration_number,
                message=(
                    f"Card {uid} is currently assigned to "
                    f"'{existing.full_name}' ({existing.registration_number}). "
                    f"Confirm to move it to '{target.full_name}'."
                )
            )

    # existing.status == INACTIVE (card owned by an inactive student)
    return CardCheckResult(
        outcome=CardCheckOutcome.OWNED_BY_OTHER,
        uid=uid,
        target_student_id=student_id,
        target_student_name=target.full_name,
        target_reg_number=target.registration_number,
        current_owner_id=existing.student_id,
        current_owner_name=existing.full_name,
        current_owner_reg=existing.registration_number,
        message=(
            f"Card {uid} is currently assigned to "
            f"'{existing.full_name}' (inactive student). "
            f"Confirm to move it to '{target.full_name}'."
        )
    )


# ---------------------------------------------------------------------------
# Phase 2 — Confirm
# ---------------------------------------------------------------------------

def confirm_assignment(uid: str, student_id: int) -> AssignmentResult:
    """
    Phase 2: execute the card assignment after the teacher has confirmed.

    This should only be called after check_uid_for_assignment() returned
    UNASSIGNED or OWNED_BY_OTHER and the teacher explicitly confirmed.

    Behaviour:
        - If the card is unassigned       → assign_card()
        - If the card is owned by another → reassign_card()
        - If the card is already on this  → assign_card() (idempotent, no-op)
        - If the student does not exist   → returns STUDENT_NOT_FOUND

    The low-level functions enforce all database constraints. This function
    does not bypass them.
    """
    uid = normalize_uid(uid)

    existing = find_student_by_uid(uid)

    if existing.status == LookupStatus.NOT_FOUND:
        # Unassigned card — fresh assignment.
        return assign_card(uid, student_id)

    if existing.student_id == student_id:
        # Already assigned to this student — idempotent.
        return assign_card(uid, student_id)

    # Owned by someone else — explicit reassignment.
    return reassign_card(uid, student_id)


def cancel_assignment() -> dict:
    """
    Phase 2 alternative: teacher cancelled the assignment.

    No database changes are made. Returns a simple confirmation dict.
    """
    return {
        "outcome" : "CANCELLED",
        "message" : "Card assignment cancelled. No changes were made.",
    }


# ---------------------------------------------------------------------------
# Student CRUD
# ---------------------------------------------------------------------------

def create_student(
    first_name          : str,
    last_name           : str,
    registration_number : str,
    class_id            : Optional[int] = None,
    rfid_uid            : Optional[str] = None,
) -> StudentResult:
    """
    Create a new student record.

    rfid_uid is optional — a student can exist without a card.
    class_id is optional — a student can be created before class assignment.

    Validates:
        - registration_number must be unique.
        - class_id must reference a valid class (if provided).
        - rfid_uid must be unique (if provided).
    """
    if rfid_uid is not None:
        rfid_uid = normalize_uid(rfid_uid)

    conn = get_connection()
    try:
        cur = conn.cursor()

        # Validate class if provided.
        if class_id is not None:
            cur.execute("SELECT id FROM classes WHERE id = ?", (class_id,))
            if cur.fetchone() is None:
                return StudentResult(
                    outcome=StudentOutcome.INVALID_CLASS,
                    message=f"Class id={class_id} not found."
                )

        ts = _now_iso()
        try:
            with conn:
                cur.execute(
                    """
                    INSERT INTO students
                        (first_name, last_name, registration_number,
                         rfid_uid, class_id, status, created_at)
                    VALUES (?, ?, ?, ?, ?, 'active', ?)
                    """,
                    (first_name, last_name, registration_number,
                     rfid_uid, class_id, ts)
                )
                new_id = cur.lastrowid
        except sqlite3.IntegrityError as exc:
            msg = str(exc)
            if "registration_number" in msg or "UNIQUE" in msg:
                # Could be rfid_uid unique violation too.
                if rfid_uid and "rfid_uid" in msg:
                    return StudentResult(
                        outcome=StudentOutcome.ERROR,
                        message=f"Card {rfid_uid} is already assigned to another student."
                    )
                return StudentResult(
                    outcome=StudentOutcome.DUPLICATE_REG_NUMBER,
                    message=(
                        f"Registration number '{registration_number}' "
                        f"is already in use."
                    )
                )
            return StudentResult(
                outcome=StudentOutcome.ERROR,
                message=f"Database error: {exc}"
            )

    finally:
        conn.close()

    student = get_student(new_id)
    return StudentResult(
        outcome=StudentOutcome.CREATED,
        student=student,
        message=f"Student '{first_name} {last_name}' created (id={new_id})."
    )


def update_student(
    student_id          : int,
    first_name          : Optional[str] = None,
    last_name           : Optional[str] = None,
    registration_number : Optional[str] = None,
    class_id            : Optional[int] = None,
    status              : Optional[str] = None,
) -> StudentResult:
    """
    Update mutable student fields. Only provided (non-None) fields are changed.

    Does NOT update rfid_uid here — card changes go through the
    check_uid_for_assignment / confirm_assignment workflow.
    """
    conn = get_connection()
    try:
        cur = conn.cursor()

        cur.execute("SELECT * FROM students WHERE id = ?", (student_id,))
        row = cur.fetchone()
        if row is None:
            return StudentResult(
                outcome=StudentOutcome.NOT_FOUND,
                message=f"Student id={student_id} not found."
            )

        # Build the update from only provided fields.
        updates = {}
        if first_name          is not None: updates["first_name"]          = first_name
        if last_name           is not None: updates["last_name"]           = last_name
        if registration_number is not None: updates["registration_number"] = registration_number
        if class_id            is not None: updates["class_id"]            = class_id
        if status              is not None: updates["status"]              = status

        if not updates:
            student = get_student(student_id)
            return StudentResult(
                outcome=StudentOutcome.UPDATED,
                student=student,
                message="No fields changed."
            )

        set_clause = ", ".join(f"{k} = ?" for k in updates)
        values = list(updates.values()) + [student_id]

        try:
            with conn:
                conn.execute(
                    f"UPDATE students SET {set_clause} WHERE id = ?",
                    values
                )
        except sqlite3.IntegrityError as exc:
            return StudentResult(
                outcome=StudentOutcome.DUPLICATE_REG_NUMBER,
                message=f"Update failed: {exc}"
            )

    finally:
        conn.close()

    student = get_student(student_id)
    return StudentResult(
        outcome=StudentOutcome.UPDATED,
        student=student,
        message=f"Student id={student_id} updated."
    )


# ---------------------------------------------------------------------------
# Student queries
# ---------------------------------------------------------------------------

_STUDENT_SELECT = """
    SELECT
        s.id,
        s.first_name,
        s.last_name,
        s.registration_number,
        s.rfid_uid,
        s.class_id,
        c.name AS class_name,
        s.status,
        s.created_at
    FROM students s
    LEFT JOIN classes c ON c.id = s.class_id
"""


def _row_to_student(row: sqlite3.Row) -> StudentRecord:
    return StudentRecord(
        id                  = row["id"],
        first_name          = row["first_name"],
        last_name           = row["last_name"],
        registration_number = row["registration_number"],
        rfid_uid            = row["rfid_uid"],
        class_id            = row["class_id"],
        class_name          = row["class_name"],
        status              = row["status"],
        created_at          = row["created_at"],
    )


def get_student(student_id: int) -> Optional[StudentRecord]:
    """Return one student with joined class name, or None."""
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(_STUDENT_SELECT + " WHERE s.id = ?", (student_id,))
        row = cur.fetchone()
    finally:
        conn.close()
    return _row_to_student(row) if row else None


def list_students(
    class_id       : Optional[int]  = None,
    status         : Optional[str]  = None,
    has_card       : Optional[bool] = None,
) -> list:
    """
    Return students matching optional filters.
    Ordered by last_name, first_name.

        has_card=True   → only students with rfid_uid IS NOT NULL
        has_card=False  → only students with rfid_uid IS NULL
        has_card=None   → all students
    """
    filters = []
    params  = []

    if class_id is not None:
        filters.append("s.class_id = ?")
        params.append(class_id)
    if status is not None:
        filters.append("s.status = ?")
        params.append(status)
    if has_card is True:
        filters.append("s.rfid_uid IS NOT NULL")
    elif has_card is False:
        filters.append("s.rfid_uid IS NULL")

    where = ("WHERE " + " AND ".join(filters)) if filters else ""
    sql = _STUDENT_SELECT + f" {where} ORDER BY s.last_name, s.first_name"

    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(sql, params)
        rows = cur.fetchall()
    finally:
        conn.close()

    return [_row_to_student(r) for r in rows]


def students_with_cards() -> list:
    """Return all active students who have an RFID card assigned."""
    return list_students(status="active", has_card=True)


def students_without_cards() -> list:
    """Return all active students who have no RFID card assigned."""
    return list_students(status="active", has_card=False)
