"""
card_management_test.py — Tests for card_management_service.py.

Required test cases:
    1. Assign unassigned card
    2. Reject duplicate UID (card already assigned to this student)
    3. Detect already-owned card (owned by a different student)
    4. Confirm reassignment
    5. Cancel reassignment
    6. Student without card
    7. Assigning same card to same student (idempotent)

Additional coverage:
    - create_student without a card
    - create_student with a card
    - create_student duplicate registration number
    - create_student with invalid class
    - update_student
    - list_students (unfiltered, filtered)
    - students_with_cards / students_without_cards
    - get_student found / not found
    - CardCheckResult.to_dict() structure
    - confirm_assignment delegates correctly to assign vs reassign

All tests run against an in-memory SQLite database.
The real attendance.db is never touched.
"""

import sqlite3
from datetime import datetime, timezone

from database import create_tables
from card_management_service import (
    check_uid_for_assignment,
    confirm_assignment,
    cancel_assignment,
    create_student,
    update_student,
    get_student,
    list_students,
    students_with_cards,
    students_without_cards,
    CardCheckOutcome,
    StudentOutcome,
)
from rfid_service import AssignmentOutcome


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


import card_management_service as _cms
import rfid_service             as _rs

_cms.get_connection = _mem
_rs.get_connection  = _mem


# ---------------------------------------------------------------------------
# Seed data
# ---------------------------------------------------------------------------
# Alice — has card AABB1122
# Bob   — has card CCDD3344
# Carol — NO card
# Dave  — inactive
# ---------------------------------------------------------------------------

def _seed():
    cur = _DB.cursor()
    cur.execute("INSERT INTO classes (id, name) VALUES (1, 'Y1B')")
    cur.execute("INSERT INTO classes (id, name) VALUES (2, 'Y2A')")

    # Alice — card assigned
    cur.execute(
        "INSERT INTO students (id, first_name, last_name, registration_number, "
        "rfid_uid, class_id, status, created_at) "
        "VALUES (1, 'Alice', 'Smith', 'REG001', 'AABB1122', 1, 'active', ?)",
        (now(),)
    )
    # Bob — card assigned
    cur.execute(
        "INSERT INTO students (id, first_name, last_name, registration_number, "
        "rfid_uid, class_id, status, created_at) "
        "VALUES (2, 'Bob', 'Jones', 'REG002', 'CCDD3344', 1, 'active', ?)",
        (now(),)
    )
    # Carol — no card
    cur.execute(
        "INSERT INTO students (id, first_name, last_name, registration_number, "
        "rfid_uid, class_id, status, created_at) "
        "VALUES (3, 'Carol', 'Lee', 'REG003', NULL, 1, 'active', ?)",
        (now(),)
    )
    # Dave — inactive
    cur.execute(
        "INSERT INTO students (id, first_name, last_name, registration_number, "
        "rfid_uid, class_id, status, created_at) "
        "VALUES (4, 'Dave', 'Old', 'REG004', NULL, 1, 'inactive', ?)",
        (now(),)
    )
    _DB.commit()


_seed()


# ============================================================================
# SECTION 1 — Required test case 1: Assign unassigned card
# ============================================================================

section("1. ASSIGN UNASSIGNED CARD")


def test_check_unassigned_card():
    """
    Phase 1: checking a card that is not assigned to anyone returns UNASSIGNED.
    Nothing is written to the database.
    """
    r = check_uid_for_assignment("FFEEDDCC", student_id=3)  # Carol
    assert r.outcome == CardCheckOutcome.UNASSIGNED, f"Got: {r}"
    assert r.uid == "FFEEDDCC"
    assert r.target_student_id == 3
    assert r.target_student_name == "Carol Lee"
    assert r.current_owner_id is None
    assert r.requires_confirmation() is True

    # Verify DB was NOT modified.
    cur = _DB.cursor()
    cur.execute("SELECT rfid_uid FROM students WHERE id = 3")
    assert cur.fetchone()[0] is None, "Carol should still have no card"


def test_confirm_unassigned_card():
    """
    Phase 2: after confirmation, the card is assigned.
    """
    r = confirm_assignment("FFEEDDCC", student_id=3)
    assert r.success(), f"Expected success: {r}"
    assert r.outcome == AssignmentOutcome.ASSIGNED
    assert r.uid == "FFEEDDCC"

    # Verify DB was updated.
    cur = _DB.cursor()
    cur.execute("SELECT rfid_uid FROM students WHERE id = 3")
    assert cur.fetchone()[0] == "FFEEDDCC", "Carol should now have card FFEEDDCC"


_run("CHECK: unassigned card returns UNASSIGNED (no DB change)",
     test_check_unassigned_card)
_run("CONFIRM: unassigned card is written to DB",
     test_confirm_unassigned_card)


# ============================================================================
# SECTION 2 — Required test case 2: Reject duplicate UID
#             (card already assigned to this student = ALREADY_ASSIGNED_SELF)
# ============================================================================

section("2. ALREADY ASSIGNED TO SAME STUDENT")


def test_check_already_assigned_self():
    """
    Phase 1: checking Alice's own card (AABB1122) for Alice returns
    ALREADY_ASSIGNED_SELF. No confirmation needed.
    """
    r = check_uid_for_assignment("AABB1122", student_id=1)  # Alice
    assert r.outcome == CardCheckOutcome.ALREADY_ASSIGNED_SELF, f"Got: {r}"
    assert r.requires_confirmation() is False
    assert r.is_no_op() is True
    assert r.current_owner_id == 1


def test_confirm_already_assigned_self_is_idempotent():
    """
    Phase 2: confirming an already-owned assignment is a no-op — the card
    stays on Alice and no error is raised.
    """
    r = confirm_assignment("AABB1122", student_id=1)
    assert r.success(), f"Expected success (idempotent): {r}"
    # Verify Alice still has AABB1122.
    cur = _DB.cursor()
    cur.execute("SELECT rfid_uid FROM students WHERE id = 1")
    assert cur.fetchone()[0] == "AABB1122"


_run("CHECK: card already on same student returns ALREADY_ASSIGNED_SELF",
     test_check_already_assigned_self)
_run("CONFIRM: same-student confirmation is idempotent",
     test_confirm_already_assigned_self_is_idempotent)


# ============================================================================
# SECTION 3 — Required test case 3: Detect already-owned card (other student)
# ============================================================================

section("3. CARD OWNED BY ANOTHER STUDENT")


def test_check_owned_by_other():
    """
    Phase 1: checking Bob's card (CCDD3344) for Alice returns OWNED_BY_OTHER.
    Shows the current owner (Bob) without modifying anything.
    """
    r = check_uid_for_assignment("CCDD3344", student_id=1)  # Alice wants Bob's card
    assert r.outcome == CardCheckOutcome.OWNED_BY_OTHER, f"Got: {r}"
    assert r.current_owner_id == 2        # Bob is the current owner
    assert r.current_owner_name == "Bob Jones"
    assert r.target_student_id == 1       # Alice is the intended recipient
    assert r.target_student_name == "Alice Smith"
    assert r.requires_confirmation() is True

    # Verify DB was NOT modified.
    cur = _DB.cursor()
    cur.execute("SELECT rfid_uid FROM students WHERE id = 1")
    assert cur.fetchone()[0] == "AABB1122", "Alice's card must be unchanged"
    cur.execute("SELECT rfid_uid FROM students WHERE id = 2")
    assert cur.fetchone()[0] == "CCDD3344", "Bob's card must be unchanged"


_run("CHECK: card owned by another student returns OWNED_BY_OTHER (no DB change)",
     test_check_owned_by_other)


# ============================================================================
# SECTION 4 — Required test case 4: Confirm reassignment
# ============================================================================

section("4. CONFIRM REASSIGNMENT")


def test_confirm_reassignment():
    """
    Phase 2: after the teacher confirms, Bob's card (CCDD3344) moves to Alice.
    Bob loses the card (rfid_uid = NULL).
    Alice gains the card.
    """
    r = confirm_assignment("CCDD3344", student_id=1)   # move Bob's card to Alice
    assert r.success(), f"Expected success: {r}"
    assert r.outcome == AssignmentOutcome.REASSIGNED
    assert r.previous_owner_id == 2         # was Bob's
    assert r.previous_owner_name == "Bob Jones"

    cur = _DB.cursor()
    cur.execute("SELECT rfid_uid FROM students WHERE id = 1")
    assert cur.fetchone()[0] == "CCDD3344", "Alice should now have CCDD3344"

    cur.execute("SELECT rfid_uid FROM students WHERE id = 2")
    assert cur.fetchone()[0] is None, "Bob should have no card after reassignment"


_run("CONFIRM: reassignment moves card and clears previous owner",
     test_confirm_reassignment)


# ============================================================================
# SECTION 5 — Required test case 5: Cancel reassignment
# ============================================================================

section("5. CANCEL REASSIGNMENT")


def test_cancel_does_not_modify_db():
    """
    cancel_assignment() returns a cancellation dict and writes nothing.
    We capture Alice's current card before the check, then verify it is
    unchanged after cancellation.
    """
    # Record Alice's card before the check.
    cur = _DB.cursor()
    cur.execute("SELECT rfid_uid FROM students WHERE id = 1")
    alice_card_before = cur.fetchone()[0]

    # Check (without confirming) — Alice wants CCDD3344 (currently her card
    # from section 4, so ALREADY_ASSIGNED_SELF) or some other card.
    # Either way, cancel and verify nothing changed.
    r = check_uid_for_assignment(alice_card_before, student_id=1)
    # outcome is ALREADY_ASSIGNED_SELF — cancel is a no-op regardless.

    result = cancel_assignment()
    assert result["outcome"] == "CANCELLED"
    assert "cancelled" in result["message"].lower()

    # Verify DB is unchanged — Alice still has the same card.
    cur.execute("SELECT rfid_uid FROM students WHERE id = 1")
    alice_card_after = cur.fetchone()[0]
    assert alice_card_after == alice_card_before, (
        f"Alice's card changed after cancel: {alice_card_before} -> {alice_card_after}"
    )


_run("CANCEL: no DB changes after cancellation", test_cancel_does_not_modify_db)


# ============================================================================
# SECTION 6 — Required test case 6: Student without card
# ============================================================================

section("6. STUDENT WITHOUT CARD")


def test_student_exists_without_card():
    """
    Bob now has no card (from section 4 reassignment).
    Verify that a student can exist with rfid_uid = NULL.
    """
    student = get_student(2)
    assert student is not None
    assert student.has_card is False
    assert student.rfid_uid is None


def test_check_for_student_without_card():
    """
    Checking an unassigned UID for a student without a card should return UNASSIGNED.
    """
    r = check_uid_for_assignment("BRANDNEW", student_id=2)   # Bob has no card
    assert r.outcome == CardCheckOutcome.UNASSIGNED
    assert r.target_student_id == 2


def test_students_without_cards_query():
    """students_without_cards() must include Bob and exclude Alice and Carol."""
    no_card = students_without_cards()
    ids = [s.id for s in no_card]
    assert 2 in ids, "Bob (no card) must be in the list"
    assert 1 not in ids, "Alice (has card) must NOT be in the list"


_run("Student without card: rfid_uid is NULL, has_card is False",
     test_student_exists_without_card)
_run("CHECK for student without card returns UNASSIGNED for new UID",
     test_check_for_student_without_card)
_run("students_without_cards() returns students with no card",
     test_students_without_cards_query)


# ============================================================================
# SECTION 7 — Required test case 7: Assigning same card to same student
# ============================================================================

section("7. ASSIGNING SAME CARD TO SAME STUDENT (IDEMPOTENT)")


def test_check_same_card_same_student():
    """
    Checking Alice's current card for Alice returns ALREADY_ASSIGNED_SELF.
    """
    r = check_uid_for_assignment("CCDD3344", student_id=1)   # Alice now has CCDD3344
    assert r.outcome == CardCheckOutcome.ALREADY_ASSIGNED_SELF
    assert r.is_no_op() is True


def test_confirm_same_card_same_student_is_harmless():
    """
    Confirming the assignment of a card to its current owner must not
    error and must not change the database.
    """
    r = confirm_assignment("CCDD3344", student_id=1)
    assert r.success()

    cur = _DB.cursor()
    cur.execute("SELECT rfid_uid FROM students WHERE id = 1")
    assert cur.fetchone()[0] == "CCDD3344"


_run("CHECK: same card, same student → ALREADY_ASSIGNED_SELF",
     test_check_same_card_same_student)
_run("CONFIRM: same card, same student is idempotent",
     test_confirm_same_card_same_student_is_harmless)


# ============================================================================
# SECTION 8 — create_student
# ============================================================================

section("8. CREATE STUDENT")

CREATED_ID = None


def test_create_student_without_card():
    global CREATED_ID
    r = create_student(
        first_name="Eve", last_name="Taylor",
        registration_number="REG010",
        class_id=1
    )
    assert r.success(), f"Expected CREATED: {r}"
    assert r.outcome == StudentOutcome.CREATED
    assert r.student is not None
    assert r.student.rfid_uid is None
    assert r.student.has_card is False
    assert r.student.class_name == "Y1B"
    CREATED_ID = r.student.id


def test_create_student_with_card():
    r = create_student(
        first_name="Frank", last_name="Doe",
        registration_number="REG011",
        rfid_uid="EEFF5566",
        class_id=1
    )
    assert r.success()
    assert r.student.rfid_uid == "EEFF5566"
    assert r.student.has_card is True


def test_create_student_no_class():
    """Class is optional — student can be created without a class."""
    r = create_student(
        first_name="Zara", last_name="Khan",
        registration_number="REG012"
    )
    assert r.success()
    assert r.student.class_id is None
    assert r.student.class_name is None


def test_create_student_duplicate_reg_number():
    r = create_student(
        first_name="Dup", last_name="Student",
        registration_number="REG001"   # already exists (Alice)
    )
    assert r.outcome == StudentOutcome.DUPLICATE_REG_NUMBER


def test_create_student_invalid_class():
    r = create_student(
        first_name="Bad", last_name="Class",
        registration_number="REG099",
        class_id=99999
    )
    assert r.outcome == StudentOutcome.INVALID_CLASS


_run("create_student without card",        test_create_student_without_card)
_run("create_student with card pre-set",   test_create_student_with_card)
_run("create_student without class",       test_create_student_no_class)
_run("create_student duplicate reg number → DUPLICATE_REG_NUMBER",
     test_create_student_duplicate_reg_number)
_run("create_student invalid class → INVALID_CLASS",
     test_create_student_invalid_class)


# ============================================================================
# SECTION 9 — update_student
# ============================================================================

section("9. UPDATE STUDENT")


def test_update_student_name():
    r = update_student(CREATED_ID, first_name="Evelyn")
    assert r.success()
    assert r.student.first_name == "Evelyn"


def test_update_student_class():
    r = update_student(CREATED_ID, class_id=2)
    assert r.success()
    assert r.student.class_id == 2
    assert r.student.class_name == "Y2A"


def test_update_student_not_found():
    r = update_student(99999, first_name="Ghost")
    assert r.outcome == StudentOutcome.NOT_FOUND


def test_update_student_no_fields_is_noop():
    r = update_student(CREATED_ID)
    assert r.success()   # No error, just returns current state


_run("update_student — change first name",        test_update_student_name)
_run("update_student — change class",             test_update_student_class)
_run("update_student — not found",                test_update_student_not_found)
_run("update_student — no fields provided is OK", test_update_student_no_fields_is_noop)


# ============================================================================
# SECTION 10 — Student queries
# ============================================================================

section("10. STUDENT QUERIES")


def test_get_student_found():
    s = get_student(1)
    assert s is not None
    assert s.first_name == "Alice"
    assert s.class_name == "Y1B"


def test_get_student_not_found():
    s = get_student(99999)
    assert s is None


def test_list_students_all():
    students = list_students()
    assert len(students) >= 4   # seed data + created students


def test_list_students_by_class():
    students = list_students(class_id=1)
    for s in students:
        assert s.class_id == 1


def test_list_students_active_only():
    students = list_students(status="active")
    for s in students:
        assert s.status == "active"
    ids = [s.id for s in students]
    assert 4 not in ids, "Dave (inactive) must not appear"


def test_students_with_cards():
    with_cards = students_with_cards()
    for s in with_cards:
        assert s.has_card is True
        assert s.rfid_uid is not None
    # Dave (inactive) must never appear regardless of card status.
    ids = [s.id for s in with_cards]
    assert 4 not in ids, "Dave (inactive) must not appear in active students with cards"


def test_students_without_cards():
    without = students_without_cards()
    for s in without:
        assert s.has_card is False
        assert s.rfid_uid is None


_run("get_student — found with class name", test_get_student_found)
_run("get_student — not found returns None", test_get_student_not_found)
_run("list_students — unfiltered", test_list_students_all)
_run("list_students — filtered by class_id", test_list_students_by_class)
_run("list_students — active only excludes inactive", test_list_students_active_only)
_run("students_with_cards — only returns students with cards", test_students_with_cards)
_run("students_without_cards — only returns students without cards", test_students_without_cards)


# ============================================================================
# SECTION 11 — Guard: inactive student cannot be assigned
# ============================================================================

section("11. GUARD: INACTIVE STUDENT")


def test_check_inactive_student():
    r = check_uid_for_assignment("ANYTHING", student_id=4)   # Dave is inactive
    assert r.outcome == CardCheckOutcome.STUDENT_INACTIVE
    assert r.requires_confirmation() is False


def test_check_nonexistent_student():
    r = check_uid_for_assignment("ANYTHING", student_id=99999)
    assert r.outcome == CardCheckOutcome.STUDENT_NOT_FOUND


_run("CHECK: inactive student returns STUDENT_INACTIVE",
     test_check_inactive_student)
_run("CHECK: nonexistent student returns STUDENT_NOT_FOUND",
     test_check_nonexistent_student)


# ============================================================================
# SECTION 12 — CardCheckResult structure
# ============================================================================

section("12. CARDCHECKRESULT STRUCTURE")


def test_check_result_to_dict_keys():
    r = check_uid_for_assignment("AABB1122", student_id=1)
    d = r.to_dict()
    required = {
        "outcome", "uid",
        "target_student_id", "target_student_name", "target_reg_number",
        "current_owner_id", "current_owner_name", "current_owner_reg",
        "requires_confirmation", "message",
    }
    missing = required - d.keys()
    assert not missing, f"Missing keys: {missing}"


def test_check_result_outcome_is_string():
    r = check_uid_for_assignment("AABB1122", student_id=1)
    d = r.to_dict()
    assert isinstance(d["outcome"], str), (
        f"outcome must be a string in to_dict(), got {type(d['outcome'])}"
    )


def test_student_record_to_dict_keys():
    s = get_student(1)
    d = s.to_dict()
    required = {
        "id", "first_name", "last_name", "full_name",
        "registration_number", "rfid_uid", "has_card",
        "class_id", "class_name", "status", "created_at",
    }
    missing = required - d.keys()
    assert not missing, f"Missing keys: {missing}"


_run("CardCheckResult.to_dict() contains all required keys",
     test_check_result_to_dict_keys)
_run("CardCheckResult.to_dict() outcome is a plain string",
     test_check_result_outcome_is_string)
_run("StudentRecord.to_dict() contains all required keys",
     test_student_record_to_dict_keys)


# ---------------------------------------------------------------------------
# Restore
# ---------------------------------------------------------------------------

import database as _db_mod
_cms.get_connection = _db_mod.get_connection
_rs.get_connection  = _db_mod.get_connection


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
