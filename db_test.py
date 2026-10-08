"""
db_test.py — Database schema tests for the simplified 6-table prototype schema.

Tables covered:
    classes
    students          (rfid_uid nullable + unique on students directly)
    teachers
    subjects
    attendance_sessions
    attendance        (UNIQUE(session_id, student_id) — critical)

All tests run against an in-memory database.
The real attendance.db is never touched.

Run with:
    python db_test.py
"""

import sqlite3
from datetime import datetime, timezone
from database import create_tables


# ---------------------------------------------------------------------------
# Test harness
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


def expect_integrity_error(fn):
    """Assert that fn() raises sqlite3.IntegrityError."""
    try:
        fn()
        raise AssertionError("Expected IntegrityError but none was raised")
    except sqlite3.IntegrityError:
        pass


def section(title):
    print(f"\n{'=' * 60}")
    print(f"  {title}")
    print(f"{'=' * 60}")


def now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")


# ---------------------------------------------------------------------------
# In-memory DB fixture
# ---------------------------------------------------------------------------

def make_db():
    conn = sqlite3.connect(":memory:")
    conn.execute("PRAGMA foreign_keys = ON")
    conn.row_factory = sqlite3.Row
    create_tables(conn)
    conn.commit()
    return conn


DB  = make_db()
CUR = DB.cursor()


def insert(sql, params=()):
    CUR.execute(sql, params)
    return CUR.lastrowid


def query_one(sql, params=()):
    CUR.execute(sql, params)
    return CUR.fetchone()


def query_all(sql, params=()):
    CUR.execute(sql, params)
    return CUR.fetchall()


# ---------------------------------------------------------------------------
# SECTION 1 — Classes
# ---------------------------------------------------------------------------

section("1. CLASSES")

CLASS_ID = None
CLASS2_ID = None


def test_create_class():
    global CLASS_ID
    CLASS_ID = insert(
        "INSERT INTO classes (name, description) VALUES (?, ?)",
        ("Y1B", "Year 1 Section B")
    )
    row = query_one("SELECT * FROM classes WHERE id = ?", (CLASS_ID,))
    assert row is not None
    assert row["name"] == "Y1B"


def test_class_name_unique():
    expect_integrity_error(lambda: insert(
        "INSERT INTO classes (name) VALUES (?)", ("Y1B",)
    ))


def test_create_second_class():
    global CLASS2_ID
    CLASS2_ID = insert("INSERT INTO classes (name) VALUES (?)", ("Y2A",))
    assert CLASS2_ID is not None


_run("Create class", test_create_class)
_run("Class name must be unique", test_class_name_unique)
_run("Create second class", test_create_second_class)


# ---------------------------------------------------------------------------
# SECTION 2 — Subjects
# ---------------------------------------------------------------------------

section("2. SUBJECTS")

SUBJECT_ID = None


def test_create_subject():
    global SUBJECT_ID
    SUBJECT_ID = insert(
        "INSERT INTO subjects (name, code) VALUES (?, ?)",
        ("Mathematics", "MATH")
    )
    row = query_one("SELECT * FROM subjects WHERE id = ?", (SUBJECT_ID,))
    assert row is not None
    assert row["name"] == "Mathematics"


def test_subject_name_unique():
    expect_integrity_error(lambda: insert(
        "INSERT INTO subjects (name) VALUES (?)", ("Mathematics",)
    ))


_run("Create subject", test_create_subject)
_run("Subject name must be unique", test_subject_name_unique)


# ---------------------------------------------------------------------------
# SECTION 3 — Teachers
# ---------------------------------------------------------------------------

section("3. TEACHERS")

TEACHER_ID = None


def test_create_teacher():
    global TEACHER_ID
    TEACHER_ID = insert(
        "INSERT INTO teachers (name, username, status, created_at) "
        "VALUES (?, ?, ?, ?)",
        ("Teacher One", "teacher1", "active", now())
    )
    row = query_one("SELECT * FROM teachers WHERE id = ?", (TEACHER_ID,))
    assert row is not None
    assert row["username"] == "teacher1"
    assert row["password_hash"] is None  # nullable until auth is built


def test_teacher_username_unique():
    expect_integrity_error(lambda: insert(
        "INSERT INTO teachers (name, username, status, created_at) "
        "VALUES (?, ?, ?, ?)",
        ("Duplicate", "teacher1", "active", now())
    ))


def test_teacher_status_check():
    expect_integrity_error(lambda: insert(
        "INSERT INTO teachers (name, username, status, created_at) "
        "VALUES (?, ?, ?, ?)",
        ("Bad", "bad_teacher", "suspended", now())
    ))


_run("Create teacher (password_hash nullable)", test_create_teacher)
_run("Teacher username must be unique", test_teacher_username_unique)
_run("Teacher status CHECK (active/inactive only)", test_teacher_status_check)


# ---------------------------------------------------------------------------
# SECTION 4 — Students
# ---------------------------------------------------------------------------

section("4. STUDENTS")

STUDENT_ID  = None
STUDENT2_ID = None
STUDENT3_ID = None


def test_create_student_without_card():
    """A student can exist with rfid_uid = NULL."""
    global STUDENT_ID
    STUDENT_ID = insert(
        "INSERT INTO students "
        "(first_name, last_name, registration_number, rfid_uid, class_id, status, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        ("Alice", "Smith", "REG001", None, None, "active", now())
    )
    row = query_one("SELECT * FROM students WHERE id = ?", (STUDENT_ID,))
    assert row is not None
    assert row["rfid_uid"] is None, "rfid_uid should be NULL when not assigned"
    assert row["class_id"] is None, "class_id should be NULL when not assigned"
    # Confirm card_uid column does NOT exist on students.
    col_names = {desc[0] for desc in CUR.description}
    assert "card_uid" not in col_names, "card_uid must not exist on students table"


def test_create_student_with_card():
    """A student can be created with an rfid_uid assigned."""
    global STUDENT2_ID
    STUDENT2_ID = insert(
        "INSERT INTO students "
        "(first_name, last_name, registration_number, rfid_uid, class_id, status, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        ("Bob", "Jones", "REG002", "A96E9504", CLASS_ID, "active", now())
    )
    row = query_one("SELECT * FROM students WHERE id = ?", (STUDENT2_ID,))
    assert row["rfid_uid"] == "A96E9504"
    assert row["class_id"] == CLASS_ID


def test_create_student_in_class():
    """A student can reference a class."""
    global STUDENT3_ID
    STUDENT3_ID = insert(
        "INSERT INTO students "
        "(first_name, last_name, registration_number, rfid_uid, class_id, status, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        ("Carol", "Lee", "REG003", "AB529E04", CLASS_ID, "active", now())
    )
    row = query_one("SELECT * FROM students WHERE id = ?", (STUDENT3_ID,))
    assert row["class_id"] == CLASS_ID


def test_registration_number_unique():
    expect_integrity_error(lambda: insert(
        "INSERT INTO students "
        "(first_name, last_name, registration_number, status, created_at) "
        "VALUES (?, ?, ?, ?, ?)",
        ("Dup", "Student", "REG001", "active", now())
    ))


def test_rfid_uid_unique():
    """The same UID cannot be assigned to two students."""
    expect_integrity_error(lambda: insert(
        "INSERT INTO students "
        "(first_name, last_name, registration_number, rfid_uid, status, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        ("Eve", "Taylor", "REG004", "A96E9504", "active", now())
    ))


def test_rfid_uid_null_not_unique_violation():
    """NULL rfid_uid is allowed on multiple students (NULL != NULL in SQLite UNIQUE)."""
    id_a = insert(
        "INSERT INTO students "
        "(first_name, last_name, registration_number, rfid_uid, status, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        ("NoCard1", "Test", "REG005", None, "active", now())
    )
    id_b = insert(
        "INSERT INTO students "
        "(first_name, last_name, registration_number, rfid_uid, status, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        ("NoCard2", "Test", "REG006", None, "active", now())
    )
    assert id_a is not None and id_b is not None


def test_student_class_fk():
    """class_id must reference a valid classes.id."""
    expect_integrity_error(lambda: insert(
        "INSERT INTO students "
        "(first_name, last_name, registration_number, class_id, status, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        ("Bad", "Student", "REG099", 99999, "active", now())
    ))


def test_student_status_check():
    expect_integrity_error(lambda: insert(
        "INSERT INTO students "
        "(first_name, last_name, registration_number, status, created_at) "
        "VALUES (?, ?, ?, ?, ?)",
        ("Bad", "Status", "REG098", "expelled", now())
    ))


def test_assign_uid_to_existing_student():
    """A student without a card can have a UID assigned via UPDATE."""
    DB.execute(
        "UPDATE students SET rfid_uid = ? WHERE id = ?",
        ("594AB9D4", STUDENT_ID)
    )
    row = query_one("SELECT rfid_uid FROM students WHERE id = ?", (STUDENT_ID,))
    assert row["rfid_uid"] == "594AB9D4"


def test_reassign_uid_to_different_student_rejected():
    """Assigning a UID already on another student is rejected by UNIQUE constraint."""
    expect_integrity_error(lambda: DB.execute(
        "UPDATE students SET rfid_uid = ? WHERE id = ?",
        ("A96E9504", STUDENT_ID)   # A96E9504 is already on STUDENT2_ID
    ))


_run("Create student without RFID card (rfid_uid=NULL)", test_create_student_without_card)
_run("Create student with RFID card and class", test_create_student_with_card)
_run("Create student in class", test_create_student_in_class)
_run("Registration number must be unique", test_registration_number_unique)
_run("rfid_uid must be unique across students", test_rfid_uid_unique)
_run("Multiple students can have rfid_uid=NULL", test_rfid_uid_null_not_unique_violation)
_run("student.class_id FK enforced", test_student_class_fk)
_run("Student status CHECK constraint", test_student_status_check)
_run("Assign UID to student via UPDATE", test_assign_uid_to_existing_student)
_run("Reassigning an already-assigned UID is rejected", test_reassign_uid_to_different_student_rejected)


# ---------------------------------------------------------------------------
# SECTION 5 — Attendance sessions
# ---------------------------------------------------------------------------

section("5. ATTENDANCE SESSIONS")

SESSION_ID  = None
SESSION2_ID = None


def test_create_scheduled_session():
    global SESSION_ID
    SESSION_ID = insert(
        "INSERT INTO attendance_sessions "
        "(class_id, teacher_id, subject_id, session_date, status, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (CLASS_ID, TEACHER_ID, SUBJECT_ID, "2026-10-07", "SCHEDULED", now())
    )
    row = query_one("SELECT * FROM attendance_sessions WHERE id = ?", (SESSION_ID,))
    assert row is not None
    assert row["status"] == "SCHEDULED"
    assert row["opened_at"] is None
    assert row["closed_at"] is None
    assert row["completed_at"] is None


def test_session_requires_class():
    """class_id is NOT NULL — a session must have a class."""
    expect_integrity_error(lambda: insert(
        "INSERT INTO attendance_sessions (session_date, status, created_at) "
        "VALUES (?, ?, ?)",
        ("2026-10-08", "SCHEDULED", now())
    ))


def test_session_class_fk():
    expect_integrity_error(lambda: insert(
        "INSERT INTO attendance_sessions "
        "(class_id, session_date, status, created_at) VALUES (?, ?, ?, ?)",
        (99999, "2026-10-09", "SCHEDULED", now())
    ))


def test_session_teacher_fk():
    expect_integrity_error(lambda: insert(
        "INSERT INTO attendance_sessions "
        "(class_id, teacher_id, session_date, status, created_at) VALUES (?, ?, ?, ?, ?)",
        (CLASS_ID, 99999, "2026-10-10", "SCHEDULED", now())
    ))


def test_session_subject_fk():
    expect_integrity_error(lambda: insert(
        "INSERT INTO attendance_sessions "
        "(class_id, subject_id, session_date, status, created_at) VALUES (?, ?, ?, ?, ?)",
        (CLASS_ID, 99999, "2026-10-11", "SCHEDULED", now())
    ))


def test_session_status_check():
    expect_integrity_error(lambda: insert(
        "INSERT INTO attendance_sessions "
        "(class_id, session_date, status, created_at) VALUES (?, ?, ?, ?)",
        (CLASS_ID, "2026-10-12", "ACTIVE", now())
    ))


def test_session_teacher_and_subject_nullable():
    """teacher_id and subject_id are optional (nullable)."""
    sid = insert(
        "INSERT INTO attendance_sessions "
        "(class_id, teacher_id, subject_id, session_date, status, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (CLASS_ID, None, None, "2026-10-13", "SCHEDULED", now())
    )
    row = query_one("SELECT * FROM attendance_sessions WHERE id = ?", (sid,))
    assert row["teacher_id"] is None
    assert row["subject_id"] is None


def test_session_with_times():
    """start_time and end_time are optional TEXT fields."""
    sid = insert(
        "INSERT INTO attendance_sessions "
        "(class_id, session_date, start_time, end_time, status, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (CLASS_ID, "2026-10-14", "08:00", "10:30", "SCHEDULED", now())
    )
    row = query_one("SELECT * FROM attendance_sessions WHERE id = ?", (sid,))
    assert row["start_time"] == "08:00"
    assert row["end_time"] == "10:30"


# --- State machine transitions ---

def test_state_scheduled_to_open():
    opened = now()
    DB.execute(
        "UPDATE attendance_sessions SET status='OPEN', opened_at=? WHERE id=?",
        (opened, SESSION_ID)
    )
    row = query_one("SELECT status, opened_at FROM attendance_sessions WHERE id=?", (SESSION_ID,))
    assert row["status"] == "OPEN"
    assert row["opened_at"] is not None


def test_state_open_to_closed():
    closed = now()
    DB.execute(
        "UPDATE attendance_sessions SET status='CLOSED', closed_at=? WHERE id=?",
        (closed, SESSION_ID)
    )
    row = query_one("SELECT status FROM attendance_sessions WHERE id=?", (SESSION_ID,))
    assert row["status"] == "CLOSED"


def test_state_closed_to_open():
    """CLOSED can be reopened."""
    DB.execute(
        "UPDATE attendance_sessions SET status='OPEN' WHERE id=?", (SESSION_ID,)
    )
    row = query_one("SELECT status FROM attendance_sessions WHERE id=?", (SESSION_ID,))
    assert row["status"] == "OPEN"


def test_state_open_to_completed():
    completed = now()
    DB.execute(
        "UPDATE attendance_sessions SET status='COMPLETED', completed_at=? WHERE id=?",
        (completed, SESSION_ID)
    )
    row = query_one("SELECT status, completed_at FROM attendance_sessions WHERE id=?", (SESSION_ID,))
    assert row["status"] == "COMPLETED"
    assert row["completed_at"] is not None


def test_state_closed_to_completed():
    """A second session: CLOSED -> COMPLETED directly."""
    global SESSION2_ID
    SESSION2_ID = insert(
        "INSERT INTO attendance_sessions "
        "(class_id, session_date, status, created_at) VALUES (?, ?, ?, ?)",
        (CLASS_ID, "2026-10-15", "CLOSED", now())
    )
    DB.execute(
        "UPDATE attendance_sessions SET status='COMPLETED' WHERE id=?", (SESSION2_ID,)
    )
    row = query_one("SELECT status FROM attendance_sessions WHERE id=?", (SESSION2_ID,))
    assert row["status"] == "COMPLETED"


def test_completed_session_stays_completed():
    """
    COMPLETED is permanent — enforced by application logic.
    Document the enforcement boundary here:
    the DB CHECK allows any valid status string, so the application
    must refuse COMPLETED -> OPEN/CLOSED transitions.
    """
    row = query_one("SELECT status FROM attendance_sessions WHERE id=?", (SESSION_ID,))
    assert row["status"] == "COMPLETED", (
        "COMPLETED session must remain COMPLETED"
    )


_run("Create SCHEDULED session", test_create_scheduled_session)
_run("Session requires class_id (NOT NULL)", test_session_requires_class)
_run("Session class_id FK enforced", test_session_class_fk)
_run("Session teacher_id FK enforced", test_session_teacher_fk)
_run("Session subject_id FK enforced", test_session_subject_fk)
_run("Session status CHECK constraint", test_session_status_check)
_run("teacher_id and subject_id are nullable", test_session_teacher_and_subject_nullable)
_run("Session stores start_time and end_time", test_session_with_times)
_run("State: SCHEDULED -> OPEN", test_state_scheduled_to_open)
_run("State: OPEN -> CLOSED", test_state_open_to_closed)
_run("State: CLOSED -> OPEN (reopen)", test_state_closed_to_open)
_run("State: OPEN -> COMPLETED", test_state_open_to_completed)
_run("State: CLOSED -> COMPLETED", test_state_closed_to_completed)
_run("COMPLETED session stays COMPLETED (documented boundary)", test_completed_session_stays_completed)


# ---------------------------------------------------------------------------
# SECTION 6 — Attendance records
# ---------------------------------------------------------------------------

section("6. ATTENDANCE RECORDS")

# Create a fresh OPEN session for attendance tests.
ATT_SESSION_ID = insert(
    "INSERT INTO attendance_sessions "
    "(class_id, teacher_id, subject_id, session_date, status, opened_at, created_at) "
    "VALUES (?, ?, ?, ?, ?, ?, ?)",
    (CLASS_ID, TEACHER_ID, SUBJECT_ID, "2026-10-20", "OPEN", now(), now())
)

# A second OPEN session on a different date for cross-session tests.
ATT_SESSION2_ID = insert(
    "INSERT INTO attendance_sessions "
    "(class_id, session_date, status, opened_at, created_at) "
    "VALUES (?, ?, ?, ?, ?)",
    (CLASS_ID, "2026-10-21", "OPEN", now(), now())
)


def test_record_attendance():
    att_id = insert(
        "INSERT INTO attendance "
        "(session_id, student_id, attendance_time, status, created_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (ATT_SESSION_ID, STUDENT2_ID, now(), "present", now())
    )
    row = query_one("SELECT * FROM attendance WHERE id = ?", (att_id,))
    assert row is not None
    assert row["session_id"] == ATT_SESSION_ID
    assert row["student_id"] == STUDENT2_ID
    assert row["status"] == "present"


def test_duplicate_attendance_rejected():
    """
    The UNIQUE(session_id, student_id) constraint must prevent a student
    being recorded twice in the same session.
    This is the critical duplicate-prevention guarantee.
    """
    expect_integrity_error(lambda: insert(
        "INSERT INTO attendance "
        "(session_id, student_id, attendance_time, status, created_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (ATT_SESSION_ID, STUDENT2_ID, now(), "present", now())
    ))


def test_same_student_different_session():
    """The same student can attend a different session — this must succeed."""
    new_id = insert(
        "INSERT INTO attendance "
        "(session_id, student_id, attendance_time, status, created_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (ATT_SESSION2_ID, STUDENT2_ID, now(), "present", now())
    )
    assert new_id is not None
    rows = query_all(
        "SELECT * FROM attendance WHERE student_id = ?", (STUDENT2_ID,)
    )
    assert len(rows) == 2, f"Expected 2 attendance rows, got {len(rows)}"


def test_different_student_same_session():
    """A different student can attend the same session."""
    new_id = insert(
        "INSERT INTO attendance "
        "(session_id, student_id, attendance_time, status, created_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (ATT_SESSION_ID, STUDENT3_ID, now(), "present", now())
    )
    assert new_id is not None


def test_attendance_session_fk():
    expect_integrity_error(lambda: insert(
        "INSERT INTO attendance "
        "(session_id, student_id, attendance_time, status, created_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (99999, STUDENT2_ID, now(), "present", now())
    ))


def test_attendance_student_fk():
    expect_integrity_error(lambda: insert(
        "INSERT INTO attendance "
        "(session_id, student_id, attendance_time, status, created_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (ATT_SESSION_ID, 99999, now(), "present", now())
    ))


def test_attendance_has_attendance_time_not_scanned_at():
    """Column is named attendance_time, not scanned_at (schema change confirmed)."""
    row = query_one(
        "SELECT * FROM attendance WHERE session_id = ? AND student_id = ?",
        (ATT_SESSION_ID, STUDENT2_ID)
    )
    col_names = {desc[0] for desc in CUR.description}
    assert "attendance_time" in col_names, "attendance_time column must exist"
    assert "scanned_at" not in col_names, "scanned_at must NOT exist (old schema)"


def test_attendance_has_created_at():
    row = query_one(
        "SELECT * FROM attendance WHERE session_id = ? AND student_id = ?",
        (ATT_SESSION_ID, STUDENT2_ID)
    )
    assert row["created_at"] is not None


_run("Record student attendance", test_record_attendance)
_run("Duplicate attendance in same session rejected (UNIQUE constraint)", test_duplicate_attendance_rejected)
_run("Same student can attend different session", test_same_student_different_session)
_run("Different student can attend same session", test_different_student_same_session)
_run("Attendance session FK enforced", test_attendance_session_fk)
_run("Attendance student FK enforced", test_attendance_student_fk)
_run("Column is attendance_time (not scanned_at)", test_attendance_has_attendance_time_not_scanned_at)
_run("Attendance has created_at column", test_attendance_has_created_at)


# ---------------------------------------------------------------------------
# SECTION 7 — Removed tables must not exist
# ---------------------------------------------------------------------------

section("7. REMOVED TABLES MUST NOT EXIST")

REMOVED_TABLES = [
    "academic_years",
    "admins",
    "rfid_cards",
    "enrollments",
    "devices",
    "timetable_entries",
]


def make_removed_table_test(table_name):
    def test_fn():
        CUR.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
            (table_name,)
        )
        row = CUR.fetchone()
        assert row is None, (
            f"Table '{table_name}' still exists — it should have been removed."
        )
    return test_fn


for table in REMOVED_TABLES:
    _run(f"Table '{table}' does not exist", make_removed_table_test(table))


# ---------------------------------------------------------------------------
# SECTION 8 — Schema sanity: correct tables exist
# ---------------------------------------------------------------------------

section("8. EXPECTED TABLES EXIST")

EXPECTED_TABLES = {
    "classes",
    "students",
    "teachers",
    "subjects",
    "attendance_sessions",
    "attendance",
}


def test_expected_tables_present():
    CUR.execute("SELECT name FROM sqlite_master WHERE type='table'")
    found = {r[0] for r in CUR.fetchall()} - {"sqlite_sequence"}
    missing = EXPECTED_TABLES - found
    extra   = found - EXPECTED_TABLES
    assert not missing, f"Missing tables: {missing}"
    assert not extra,   f"Unexpected extra tables: {extra}"


_run("Exactly the 6 expected tables exist", test_expected_tables_present)


# ---------------------------------------------------------------------------
# Final report
# ---------------------------------------------------------------------------

DB.close()

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
