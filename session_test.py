"""
session_test.py — Tests for session_service.py and the full scan pipeline.

Coverage:
    SESSION MANAGEMENT
        create_session      — happy path, invalid class, defaults
        open_session        — SCHEDULED->OPEN, CLOSED->OPEN (reopen)
                              one-open-at-a-time rule, COMPLETED rejection
        close_session       — OPEN->CLOSED, invalid transitions
        complete_session    — OPEN->COMPLETED, CLOSED->COMPLETED
                              SCHEDULED rejection, COMPLETED idempotent rejection
        get_session         — found, not found
        get_open_session    — returns OPEN session or None
        list_sessions       — unfiltered, filtered by class, status, date
        get_session_attendance — attendance rows with student details

    SCAN PIPELINE (all 6 ScanOutcome values)
        SUCCESS         — registered student, correct class, open session
        DUPLICATE       — same student scans twice
        UNKNOWN_CARD    — UID not assigned to any student
        WRONG_CLASS     — student belongs to a different class
        NO_OPEN_SESSION — no session is currently OPEN
        ERROR           — nonexistent session (via handle_attendance_scan)

    process_scan(uid)
        — finds open session automatically
        — returns NO_OPEN_SESSION when nothing is open

All tests run against an in-memory SQLite database.
The real attendance.db is never touched.
"""

import sqlite3
from datetime import datetime, timezone

from database import create_tables
from session_service import (
    create_session, open_session, close_session, complete_session,
    get_session, get_open_session, list_sessions, get_session_attendance,
    SessionOutcome,
)
from rfid_service import (
    process_scan, handle_attendance_scan,
    ScanOutcome,
)


# ---------------------------------------------------------------------------
# Harness
# ---------------------------------------------------------------------------

_passed = 0
_failed = 0
_results = []


def _run(label, fn):
    global _passed, _failed
    try:
        fn()
        _passed += 1
        _results.append(("PASS", label, ""))
        print(f"  PASS  {label}")
    except AssertionError as e:
        _failed += 1
        _results.append(("FAIL", label, str(e)))
        print(f"  FAIL  {label}")
        if str(e):
            print(f"        {e}")
    except Exception as e:
        _failed += 1
        _results.append(("FAIL", label, f"{type(e).__name__}: {e}"))
        print(f"  FAIL  {label}")
        print(f"        {type(e).__name__}: {e}")


def section(title):
    print(f"\n{'=' * 60}")
    print(f"  {title}")
    print(f"{'=' * 60}")


def now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")


# ---------------------------------------------------------------------------
# In-memory DB with patch
# ---------------------------------------------------------------------------

def make_db():
    conn = sqlite3.connect(":memory:")
    conn.execute("PRAGMA foreign_keys = ON")
    conn.row_factory = sqlite3.Row
    create_tables(conn)
    conn.commit()
    return conn


_DB = make_db()


class _UnclosableConnection:
    def __init__(self, conn):
        self._conn = conn
    def close(self):
        pass
    def __getattr__(self, name):
        return getattr(self._conn, name)
    def __enter__(self):
        return self._conn.__enter__()
    def __exit__(self, *args):
        return self._conn.__exit__(*args)


def _mem():
    _DB.execute("PRAGMA foreign_keys = ON")
    _DB.row_factory = sqlite3.Row
    return _UnclosableConnection(_DB)


import session_service as _ss
import rfid_service    as _rs

_ss.get_connection = _mem
_rs.get_connection = _mem


# ---------------------------------------------------------------------------
# Fixture data
# ---------------------------------------------------------------------------
# Two classes so we can test WRONG_CLASS.
# Two students: Alice in Y1B, Bob in Y2A.
# Dave is inactive.
# Two teachers, two subjects.
# ---------------------------------------------------------------------------

def _seed():
    cur = _DB.cursor()

    cur.execute("INSERT INTO classes  (id, name) VALUES (1, 'Y1B')")
    cur.execute("INSERT INTO classes  (id, name) VALUES (2, 'Y2A')")
    cur.execute("INSERT INTO subjects (id, name) VALUES (1, 'Mathematics')")
    cur.execute("INSERT INTO subjects (id, name) VALUES (2, 'Physics')")
    cur.execute(
        "INSERT INTO teachers (id, name, username, status, created_at) "
        "VALUES (1, 'Teacher One', 'teacher1', 'active', ?)", (now(),)
    )
    cur.execute(
        "INSERT INTO teachers (id, name, username, status, created_at) "
        "VALUES (2, 'Teacher Two', 'teacher2', 'active', ?)", (now(),)
    )

    # Alice — Y1B, has card
    cur.execute(
        "INSERT INTO students (id, first_name, last_name, registration_number, "
        "rfid_uid, class_id, status, created_at) "
        "VALUES (1, 'Alice', 'Smith', 'REG001', 'AABBCCDD', 1, 'active', ?)",
        (now(),)
    )
    # Bob — Y2A, has card
    cur.execute(
        "INSERT INTO students (id, first_name, last_name, registration_number, "
        "rfid_uid, class_id, status, created_at) "
        "VALUES (2, 'Bob', 'Jones', 'REG002', '11223344', 2, 'active', ?)",
        (now(),)
    )
    # Carol — Y1B, no card
    cur.execute(
        "INSERT INTO students (id, first_name, last_name, registration_number, "
        "rfid_uid, class_id, status, created_at) "
        "VALUES (3, 'Carol', 'Lee', 'REG003', NULL, 1, 'active', ?)",
        (now(),)
    )
    # Dave — Y1B, inactive
    cur.execute(
        "INSERT INTO students (id, first_name, last_name, registration_number, "
        "rfid_uid, class_id, status, created_at) "
        "VALUES (4, 'Dave', 'Old', 'REG004', 'DEADBEEF', 1, 'inactive', ?)",
        (now(),)
    )

    _DB.commit()


_seed()


# ============================================================================
# SECTION 1 — create_session
# ============================================================================

section("1. CREATE SESSION")

SESS_ID = None


def test_create_basic():
    global SESS_ID
    r = create_session(class_id=1, session_date="2026-10-07",
                       teacher_id=1, subject_id=1,
                       start_time="08:00", end_time="09:30")
    assert r.success(), f"Expected success: {r}"
    assert r.outcome == SessionOutcome.CREATED
    assert r.session is not None
    assert r.session.status == "SCHEDULED"
    assert r.session.class_id == 1
    assert r.session.teacher_id == 1
    assert r.session.subject_id == 1
    assert r.session.start_time == "08:00"
    assert r.session.end_time == "09:30"
    SESS_ID = r.session.id


def test_create_defaults_to_today():
    r = create_session(class_id=1)
    assert r.success()
    from datetime import date
    assert r.session.session_date == date.today().isoformat()


def test_create_with_invalid_class():
    r = create_session(class_id=99999)
    assert r.outcome == SessionOutcome.CLASS_NOT_FOUND


def test_create_teacher_and_subject_optional():
    r = create_session(class_id=2, session_date="2026-10-08")
    assert r.success()
    assert r.session.teacher_id is None
    assert r.session.subject_id is None


def test_session_row_has_class_name():
    """SessionRow joins class name from classes table."""
    r = create_session(class_id=1, session_date="2026-10-09")
    assert r.session.class_name == "Y1B"


def test_session_row_has_subject_name():
    r = create_session(class_id=1, session_date="2026-10-10",
                       subject_id=1)
    assert r.session.subject_name == "Mathematics"


_run("Create session — happy path", test_create_basic)
_run("create_session defaults session_date to today", test_create_defaults_to_today)
_run("create_session with invalid class returns CLASS_NOT_FOUND", test_create_with_invalid_class)
_run("teacher_id and subject_id are optional", test_create_teacher_and_subject_optional)
_run("SessionRow includes class_name from JOIN", test_session_row_has_class_name)
_run("SessionRow includes subject_name from JOIN", test_session_row_has_subject_name)


# ============================================================================
# SECTION 2 — open_session
# ============================================================================

section("2. OPEN SESSION")


def test_open_scheduled_session():
    r = open_session(SESS_ID)
    assert r.success(), f"Expected OPENED: {r}"
    assert r.outcome == SessionOutcome.OPENED
    assert r.session.status == "OPEN"
    assert r.session.opened_at is not None


def test_open_already_open_returns_already_open_exists():
    """Opening an already-OPEN session returns ALREADY_OPEN_EXISTS."""
    r = open_session(SESS_ID)
    assert r.outcome == SessionOutcome.ALREADY_OPEN_EXISTS


def test_one_open_at_a_time():
    """
    Creating a second session and trying to open it while SESS_ID is OPEN
    must return ALREADY_OPEN_EXISTS with conflicting session details.
    """
    r2 = create_session(class_id=2, session_date="2026-10-07",
                        teacher_id=2, subject_id=2)
    sess2_id = r2.session.id

    result = open_session(sess2_id)
    assert result.outcome == SessionOutcome.ALREADY_OPEN_EXISTS, (
        f"Expected ALREADY_OPEN_EXISTS, got {result.outcome}"
    )
    assert result.conflicting is not None
    assert result.conflicting.id == SESS_ID


def test_open_nonexistent_session():
    r = open_session(99999)
    assert r.outcome == SessionOutcome.NOT_FOUND


_run("SCHEDULED -> OPEN transition", test_open_scheduled_session)
_run("Opening an already-OPEN session returns ALREADY_OPEN_EXISTS",
     test_open_already_open_returns_already_open_exists)
_run("One-open-at-a-time rule: second open rejected with conflicting session",
     test_one_open_at_a_time)
_run("Opening nonexistent session returns NOT_FOUND", test_open_nonexistent_session)


# ============================================================================
# SECTION 3 — close_session
# ============================================================================

section("3. CLOSE SESSION")


def test_close_open_session():
    r = close_session(SESS_ID)
    assert r.success()
    assert r.outcome == SessionOutcome.CLOSED
    assert r.session.status == "CLOSED"
    assert r.session.closed_at is not None


def test_close_already_closed():
    r = close_session(SESS_ID)
    assert r.outcome == SessionOutcome.INVALID_TRANSITION


def test_close_scheduled_session():
    r_new = create_session(class_id=1, session_date="2026-10-11")
    r = close_session(r_new.session.id)
    assert r.outcome == SessionOutcome.INVALID_TRANSITION


def test_close_nonexistent():
    r = close_session(99999)
    assert r.outcome == SessionOutcome.NOT_FOUND


_run("OPEN -> CLOSED transition", test_close_open_session)
_run("Closing already-CLOSED session returns INVALID_TRANSITION",
     test_close_already_closed)
_run("Closing a SCHEDULED session returns INVALID_TRANSITION",
     test_close_scheduled_session)
_run("Closing nonexistent session returns NOT_FOUND", test_close_nonexistent)


# ============================================================================
# SECTION 4 — reopen (CLOSED -> OPEN)
# ============================================================================

section("4. REOPEN SESSION (CLOSED -> OPEN)")


def test_reopen_closed_session():
    """A CLOSED session can be reopened."""
    r = open_session(SESS_ID)
    assert r.success(), f"Expected OPENED: {r}"
    assert r.outcome == SessionOutcome.OPENED
    assert r.session.status == "OPEN"


def test_reopen_clears_closed_at():
    """
    BUG GUARD: when a CLOSED session is reopened, closed_at must be
    cleared to NULL.  Leaving it populated would produce an inconsistent
    state (OPEN + closed_at set).
    """
    # SESS_ID was just reopened in the previous test — verify closed_at is NULL.
    s = get_session(SESS_ID)
    assert s.status == "OPEN"
    assert s.closed_at is None, (
        f"closed_at should be NULL after reopen, got {s.closed_at!r}"
    )


_run("CLOSED -> OPEN (reopen) is allowed", test_reopen_closed_session)
_run("Reopen clears closed_at to NULL (bug guard)", test_reopen_clears_closed_at)


# ============================================================================
# SECTION 5 — complete_session
# ============================================================================

section("5. COMPLETE SESSION")


def test_complete_open_session():
    """OPEN -> COMPLETED directly."""
    r = complete_session(SESS_ID)
    assert r.success()
    assert r.outcome == SessionOutcome.COMPLETED
    assert r.session.status == "COMPLETED"
    assert r.session.completed_at is not None


def test_complete_already_completed():
    r = complete_session(SESS_ID)
    assert r.outcome == SessionOutcome.INVALID_TRANSITION


def test_complete_closed_session():
    """CLOSED -> COMPLETED is allowed."""
    r_new = create_session(class_id=1, session_date="2026-10-12")
    open_session(r_new.session.id)
    close_session(r_new.session.id)
    r = complete_session(r_new.session.id)
    assert r.success()
    assert r.outcome == SessionOutcome.COMPLETED


def test_complete_scheduled_session():
    """SCHEDULED -> COMPLETED is rejected (never opened)."""
    r_new = create_session(class_id=1, session_date="2026-10-13")
    r = complete_session(r_new.session.id)
    assert r.outcome == SessionOutcome.INVALID_TRANSITION


def test_complete_nonexistent():
    r = complete_session(99999)
    assert r.outcome == SessionOutcome.NOT_FOUND


_run("OPEN -> COMPLETED", test_complete_open_session)
_run("Completing already-COMPLETED returns INVALID_TRANSITION",
     test_complete_already_completed)
_run("CLOSED -> COMPLETED is allowed", test_complete_closed_session)
_run("SCHEDULED -> COMPLETED is rejected", test_complete_scheduled_session)
_run("Completing nonexistent session returns NOT_FOUND", test_complete_nonexistent)


# ============================================================================
# SECTION 6 — COMPLETED is permanent
# ============================================================================

section("6. COMPLETED IS PERMANENT")


def test_completed_cannot_be_reopened():
    """open_session on a COMPLETED session must return INVALID_TRANSITION."""
    r = open_session(SESS_ID)
    assert r.outcome == SessionOutcome.INVALID_TRANSITION, (
        f"Expected INVALID_TRANSITION for COMPLETED->OPEN, got {r.outcome}"
    )


def test_completed_cannot_be_closed():
    """close_session on a COMPLETED session must return INVALID_TRANSITION."""
    r = close_session(SESS_ID)
    assert r.outcome == SessionOutcome.INVALID_TRANSITION


_run("COMPLETED -> OPEN is rejected (permanent)", test_completed_cannot_be_reopened)
_run("COMPLETED -> CLOSED is rejected (permanent)", test_completed_cannot_be_closed)


# ============================================================================
# SECTION 7 — Queries
# ============================================================================

section("7. QUERIES")


def test_get_session_found():
    r = get_session(SESS_ID)
    assert r is not None
    assert r.id == SESS_ID


def test_get_session_not_found():
    r = get_session(99999)
    assert r is None


def test_get_open_session_none_when_nothing_open():
    """After completing SESS_ID, no session should be OPEN."""
    r = get_open_session()
    assert r is None


def test_get_open_session_returns_open():
    """Create and open a fresh session, then get_open_session returns it."""
    r_new = create_session(class_id=1, session_date="2026-10-20")
    open_session(r_new.session.id)
    r = get_open_session()
    assert r is not None
    assert r.status == "OPEN"
    assert r.id == r_new.session.id
    # Clean up — close it so subsequent tests start clean.
    close_session(r_new.session.id)


def test_list_sessions_unfiltered():
    sessions = list_sessions()
    assert len(sessions) >= 1


def test_list_sessions_filter_by_class():
    sessions = list_sessions(class_id=1)
    for s in sessions:
        assert s.class_id == 1


def test_list_sessions_filter_by_status():
    sessions = list_sessions(status="COMPLETED")
    for s in sessions:
        assert s.status == "COMPLETED"


def test_list_sessions_filter_by_date():
    sessions = list_sessions(session_date="2026-10-07")
    for s in sessions:
        assert s.session_date == "2026-10-07"


_run("get_session — found", test_get_session_found)
_run("get_session — not found returns None", test_get_session_not_found)
_run("get_open_session returns None when no OPEN session", test_get_open_session_none_when_nothing_open)
_run("get_open_session returns the OPEN session", test_get_open_session_returns_open)
_run("list_sessions — unfiltered returns results", test_list_sessions_unfiltered)
_run("list_sessions — filtered by class_id", test_list_sessions_filter_by_class)
_run("list_sessions — filtered by status", test_list_sessions_filter_by_status)
_run("list_sessions — filtered by session_date", test_list_sessions_filter_by_date)


# ============================================================================
# SECTION 8 — All 6 scan outcomes via handle_attendance_scan
# ============================================================================
# Set up: create a fresh OPEN session for class Y1B (class_id=1).
# Alice (uid=AABBCCDD, class=Y1B) is the "correct class" student.
# Bob   (uid=11223344, class=Y2A) is the "wrong class" student.
# ============================================================================

section("8. SCAN OUTCOMES — handle_attendance_scan")

_scan_r = create_session(class_id=1, session_date="2026-10-21",
                         teacher_id=1, subject_id=1)
SCAN_SESSION_ID = _scan_r.session.id
open_session(SCAN_SESSION_ID)


def test_scan_success():
    """SUCCESS: Alice (Y1B) scans in a Y1B OPEN session."""
    r = handle_attendance_scan("AABBCCDD", SCAN_SESSION_ID)
    assert r.outcome == ScanOutcome.SUCCESS, f"Got: {r}"
    assert r.student_id == 1
    assert r.first_name == "Alice"
    assert r.attendance_time is not None
    assert r.session_class_id == 1
    assert r.session_class_name == "Y1B"
    assert r.session_subject == "Mathematics"


def test_scan_duplicate():
    """DUPLICATE: Alice scans again in the same session."""
    r = handle_attendance_scan("AABBCCDD", SCAN_SESSION_ID)
    assert r.outcome == ScanOutcome.DUPLICATE, f"Got: {r}"
    assert r.student_id == 1
    # Verify DB still has only one attendance row for Alice.
    cur = _DB.cursor()
    cur.execute(
        "SELECT COUNT(*) FROM attendance "
        "WHERE session_id = ? AND student_id = 1",
        (SCAN_SESSION_ID,)
    )
    assert cur.fetchone()[0] == 1


def test_scan_unknown_card():
    """UNKNOWN_CARD: a UID not assigned to any student."""
    r = handle_attendance_scan("FFFFFFFF", SCAN_SESSION_ID)
    assert r.outcome == ScanOutcome.UNKNOWN_CARD, f"Got: {r}"
    assert r.student_id is None
    # No attendance row should exist for this unknown card.
    cur = _DB.cursor()
    cur.execute(
        "SELECT COUNT(*) FROM attendance WHERE session_id = ?",
        (SCAN_SESSION_ID,)
    )
    assert cur.fetchone()[0] == 1  # only Alice from success test


def test_scan_wrong_class():
    """WRONG_CLASS: Bob (Y2A) scans in a Y1B session."""
    r = handle_attendance_scan("11223344", SCAN_SESSION_ID)
    assert r.outcome == ScanOutcome.WRONG_CLASS, f"Got: {r}"
    assert r.student_id == 2
    assert r.first_name == "Bob"
    assert r.student_class_id == 2    # Bob is in Y2A
    assert r.session_class_id == 1    # session is for Y1B
    # No attendance row for Bob.
    cur = _DB.cursor()
    cur.execute(
        "SELECT COUNT(*) FROM attendance "
        "WHERE session_id = ? AND student_id = 2",
        (SCAN_SESSION_ID,)
    )
    assert cur.fetchone()[0] == 0


def test_scan_no_open_session():
    """NO_OPEN_SESSION: session exists but is not OPEN."""
    # Close the session first.
    close_session(SCAN_SESSION_ID)
    r = handle_attendance_scan("AABBCCDD", SCAN_SESSION_ID)
    assert r.outcome == ScanOutcome.NO_OPEN_SESSION, f"Got: {r}"
    # Reopen for remaining tests.
    open_session(SCAN_SESSION_ID)


def test_scan_error_nonexistent_session():
    """ERROR: session id does not exist."""
    r = handle_attendance_scan("AABBCCDD", 99999)
    assert r.outcome == ScanOutcome.ERROR, f"Got: {r}"


_run("ScanOutcome.SUCCESS — correct class, OPEN session",
     test_scan_success)
_run("ScanOutcome.DUPLICATE — same student scans twice",
     test_scan_duplicate)
_run("ScanOutcome.UNKNOWN_CARD — UID not registered",
     test_scan_unknown_card)
_run("ScanOutcome.WRONG_CLASS — student in different class",
     test_scan_wrong_class)
_run("ScanOutcome.NO_OPEN_SESSION — session is CLOSED",
     test_scan_no_open_session)
_run("ScanOutcome.ERROR — nonexistent session id",
     test_scan_error_nonexistent_session)


# ============================================================================
# SECTION 9 — process_scan (auto-finds open session)
# ============================================================================

section("9. process_scan — AUTO SESSION DISCOVERY")


def test_process_scan_success():
    """process_scan finds the OPEN session automatically."""
    # SCAN_SESSION_ID is currently OPEN (reopened in test_scan_no_open_session).
    r = process_scan("AABBCCDD")
    # Alice already attended — should be DUPLICATE since section 8 recorded her.
    assert r.outcome in (ScanOutcome.SUCCESS, ScanOutcome.DUPLICATE), (
        f"Expected SUCCESS or DUPLICATE, got {r.outcome}"
    )
    assert r.session_id == SCAN_SESSION_ID


def test_process_scan_no_open_session():
    """process_scan returns NO_OPEN_SESSION when nothing is OPEN."""
    close_session(SCAN_SESSION_ID)
    r = process_scan("AABBCCDD")
    assert r.outcome == ScanOutcome.NO_OPEN_SESSION, f"Got: {r}"
    assert r.session_id is None


def test_process_scan_unknown_card():
    """process_scan returns UNKNOWN_CARD for an unregistered UID."""
    # Re-open session for this test.
    open_session(SCAN_SESSION_ID)
    r = process_scan("NOTACARD")
    assert r.outcome == ScanOutcome.UNKNOWN_CARD


def test_process_scan_wrong_class():
    """process_scan returns WRONG_CLASS for a student in a different class."""
    r = process_scan("11223344")   # Bob is Y2A, session is Y1B
    assert r.outcome == ScanOutcome.WRONG_CLASS


_run("process_scan finds open session automatically",
     test_process_scan_success)
_run("process_scan returns NO_OPEN_SESSION when nothing is open",
     test_process_scan_no_open_session)
_run("process_scan returns UNKNOWN_CARD for unregistered UID",
     test_process_scan_unknown_card)
_run("process_scan returns WRONG_CLASS for wrong-class student",
     test_process_scan_wrong_class)


# ============================================================================
# SECTION 10 — get_session_attendance
# ============================================================================

section("10. GET SESSION ATTENDANCE")


def test_get_session_attendance_returns_rows():
    rows = get_session_attendance(SCAN_SESSION_ID)
    # At least Alice should be present (recorded in section 8).
    assert len(rows) >= 1
    alice = next((r for r in rows if r.student_id == 1), None)
    assert alice is not None
    assert alice.first_name == "Alice"
    assert alice.registration_number == "REG001"
    assert alice.rfid_uid == "AABBCCDD"


def test_get_session_attendance_empty_session():
    r_new = create_session(class_id=2, session_date="2026-10-22")
    rows = get_session_attendance(r_new.session.id)
    assert rows == []


def test_attendance_row_to_dict():
    rows = get_session_attendance(SCAN_SESSION_ID)
    d = rows[0].to_dict()
    required_keys = {
        "id", "session_id", "student_id", "first_name", "last_name",
        "full_name", "registration_number", "rfid_uid",
        "attendance_time", "status", "created_at",
    }
    assert required_keys.issubset(d.keys()), (
        f"Missing keys: {required_keys - d.keys()}"
    )


_run("get_session_attendance returns rows with student details",
     test_get_session_attendance_returns_rows)
_run("get_session_attendance returns empty list for session with no attendance",
     test_get_session_attendance_empty_session)
_run("AttendanceRow.to_dict() contains all required keys",
     test_attendance_row_to_dict)


# ============================================================================
# SECTION 11 — ScanResult.to_dict()
# ============================================================================

section("11. SCAN RESULT STRUCTURE")


def test_scan_result_to_dict_keys():
    """ScanResult.to_dict() must contain all keys the frontend needs."""
    open_session(SCAN_SESSION_ID)
    r = handle_attendance_scan("AABBCCDD", SCAN_SESSION_ID)
    # Either SUCCESS (if attendance was cleared) or DUPLICATE — both work.
    d = r.to_dict()
    required = {
        "outcome", "uid",
        "session_id", "session_class_id", "session_class_name",
        "session_subject", "session_date",
        "student_id", "first_name", "last_name", "full_name",
        "registration_number", "attendance_time", "message",
    }
    missing = required - d.keys()
    assert not missing, f"Missing keys in ScanResult.to_dict(): {missing}"


def test_scan_result_outcome_is_string_in_dict():
    """outcome in to_dict() must be a string, not an enum object."""
    open_session(SCAN_SESSION_ID)
    r = handle_attendance_scan("AABBCCDD", SCAN_SESSION_ID)
    d = r.to_dict()
    assert isinstance(d["outcome"], str), (
        f"outcome should be a string, got {type(d['outcome'])}"
    )


_run("ScanResult.to_dict() contains all required frontend keys",
     test_scan_result_to_dict_keys)
_run("ScanResult.to_dict() outcome is a plain string",
     test_scan_result_outcome_is_string_in_dict)


# ---------------------------------------------------------------------------
# Restore get_connection
# ---------------------------------------------------------------------------

import database as _db_mod
_ss.get_connection = _db_mod.get_connection
_rs.get_connection = _db_mod.get_connection


# ---------------------------------------------------------------------------
# Final report
# ---------------------------------------------------------------------------

total = _passed + _failed
print(f"\n{'=' * 60}")
print(f"  RESULTS: {_passed} passed, {_failed} failed, {total} total")
print(f"{'=' * 60}")

if _failed > 0:
    print("\n  FAILURES:")
    for status, label, detail in _results:
        if status == "FAIL":
            print(f"    x  {label}")
            if detail:
                print(f"       {detail}")
    raise SystemExit(1)
else:
    print("\n  All tests passed.")
