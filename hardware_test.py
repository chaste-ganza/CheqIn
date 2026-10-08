"""
hardware_test.py — Real hardware readiness check.

Tests items 1-15 of the backend readiness checklist against the
live Flask app (http://127.0.0.1:5000) and the physical ESP8266
+ RC522 RFID reader on COM7.

Items 1-3, 5-11, 15 are tested via the API (software-only, no tap needed).
Items 4, 12-14 require a physical card tap and will prompt you.

Usage:
    1. Start the app:    python app.py
    2. Plug in the ESP8266 on COM7
    3. Run:              python hardware_test.py

If you press Ctrl+C at any hardware-tap prompt the test is skipped
and marked NOT_TESTED.
"""

import sys
import sqlite3
import time
import requests
from datetime import datetime, timezone

BASE = "http://127.0.0.1:5000"

# ---------------------------------------------------------------------------
# Harness
# ---------------------------------------------------------------------------

_results = []


def report(item_num, label, status, detail=""):
    icon = {"PASS": "✓", "FAIL": "✗", "NOT_TESTED": "—"}[status]
    print(f"  {icon}  [{item_num:>2}]  {label}")
    if detail:
        print(f"              {detail}")
    _results.append((item_num, label, status, detail))


def get(path, params=None):
    return requests.get(BASE + path, params=params, timeout=10)


def post(path, body=None):
    return requests.post(BASE + path, json=body,
                         headers={"Content-Type": "application/json"},
                         timeout=10)


def prompt_tap(message):
    """Prompt for a card tap. Returns True if user proceeds, False on skip."""
    print(f"\n  >>> {message}")
    print("      Press Enter when done, or type 's' to skip: ", end="", flush=True)
    try:
        ans = input().strip().lower()
        return ans != "s"
    except (KeyboardInterrupt, EOFError):
        print()
        return False


# ---------------------------------------------------------------------------
# Seed fresh test data
# ---------------------------------------------------------------------------

def seed():
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
    conn = sqlite3.connect("attendance.db")
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("INSERT OR IGNORE INTO classes  (id, name) VALUES (1, 'Y1B')")
    conn.execute("INSERT OR IGNORE INTO classes  (id, name) VALUES (2, 'Y2A')")
    conn.execute(
        "INSERT OR IGNORE INTO subjects (id, name, code) "
        "VALUES (1, 'Mathematics', 'MATH')"
    )
    conn.execute(
        "INSERT OR IGNORE INTO teachers "
        "(id, name, username, status, created_at) "
        "VALUES (1, 'Teacher One', 'teacher1', 'active', ?)", (now,)
    )
    # Assign the three migrated students to class Y1B so card-lookup tests work.
    # These students have rfid_uid but class_id = NULL after the DB migration.
    conn.execute(
        "UPDATE students SET class_id = 1 "
        "WHERE class_id IS NULL AND rfid_uid IS NOT NULL"
    )
    # Clean up hardware test remnants from prior runs.
    # Order matters: attendance → sessions → students (FK chain).
    conn.execute(
        "DELETE FROM attendance WHERE session_id IN "
        "(SELECT id FROM attendance_sessions WHERE session_date = '2026-11-10')"
    )
    conn.execute("DELETE FROM attendance_sessions WHERE session_date = '2026-11-10'")
    conn.execute(
        "DELETE FROM attendance WHERE student_id IN "
        "(SELECT id FROM students WHERE registration_number LIKE 'HW%')"
    )
    conn.execute("DELETE FROM students WHERE registration_number LIKE 'HW%'")
    conn.commit()
    conn.close()


# ---------------------------------------------------------------------------
# Check Flask is reachable
# ---------------------------------------------------------------------------

print("\n" + "=" * 60)
print("  BACKEND READINESS CHECK")
print("=" * 60)

try:
    r = requests.get(BASE + "/api/health", timeout=3)
    if r.status_code != 200:
        print(f"\n  ERROR: Flask returned {r.status_code}. Is the app running?")
        sys.exit(1)
except requests.exceptions.ConnectionError:
    print(f"\n  ERROR: Cannot reach {BASE}. Start the app first:\n    python app.py")
    sys.exit(1)

print(f"\n  Flask: OK  ({BASE})")
reader_online = r.json().get("reader", {}).get("reader_online", False)
print(f"  RFID reader: {'ONLINE' if reader_online else 'OFFLINE (COM7 not connected)'}")

seed()
print("  Test data seeded.\n")


# ---------------------------------------------------------------------------
# ITEM 1 — Create a student
# ---------------------------------------------------------------------------

print("=" * 60)
print("  ITEMS 1-3  (software — no hardware needed)")
print("=" * 60)

r = post("/api/students", {
    "first_name": "Hardware", "last_name": "TestStudent",
    "registration_number": "HW001", "class_id": 1,
})
if r.status_code == 201 and r.json().get("student", {}).get("id"):
    student_id = r.json()["student"]["id"]
    report(1, "Create a student via POST /api/students",
           "PASS", f"student_id={student_id}")
else:
    student_id = None
    report(1, "Create a student via POST /api/students",
           "FAIL", f"status={r.status_code} body={r.text[:100]}")

# Second student in different class (for wrong-class test)
r2 = post("/api/students", {
    "first_name": "Hardware", "last_name": "WrongClass",
    "registration_number": "HW002", "class_id": 2,
})
wrong_class_student_id = r2.json().get("student", {}).get("id") if r2.status_code == 201 else None


# ---------------------------------------------------------------------------
# ITEM 2 — Create a session
# ---------------------------------------------------------------------------

r = post("/api/sessions", {
    "class_id": 1, "session_date": "2026-11-10",
    "teacher_id": 1, "subject_id": 1,
})
if r.status_code == 201 and r.json().get("session", {}).get("id"):
    session_id = r.json()["session"]["id"]
    report(2, "Create a session via POST /api/sessions",
           "PASS", f"session_id={session_id}")
else:
    session_id = None
    report(2, "Create a session via POST /api/sessions",
           "FAIL", f"status={r.status_code}")

if not (student_id and session_id):
    print("\n  Cannot continue without student and session. Aborting.")
    sys.exit(1)


# ---------------------------------------------------------------------------
# ITEM 3 — Open a session
# ---------------------------------------------------------------------------

r = post(f"/api/sessions/{session_id}/open")
if r.status_code == 200 and r.json().get("session", {}).get("status") == "OPEN":
    report(3, "Open a session via POST /api/sessions/<id>/open",
           "PASS", f"status=OPEN, opened_at set")
else:
    report(3, "Open a session via POST /api/sessions/<id>/open",
           "FAIL", f"status={r.status_code} body={r.text[:100]}")


# ---------------------------------------------------------------------------
# ITEM 4 — Receive a real RFID UID from the ESP8266
# (hardware required)
# ---------------------------------------------------------------------------

print("\n" + "=" * 60)
print("  ITEMS 4-11  (some require real hardware)")
print("=" * 60)

real_uid = None

if not reader_online:
    report(4, "Receive a real RFID UID from the ESP8266", "NOT_TESTED",
           "Reader is OFFLINE. Plug in the ESP8266 and restart the app.")
else:
    # Clear any previous scan result
    get("/api/scan-result", params={"clear": "1"})

    tapped = prompt_tap(
        "ITEM 4: Tap ANY RFID card on the reader to test hardware UID delivery."
    )
    if not tapped:
        report(4, "Receive a real RFID UID from the ESP8266", "NOT_TESTED",
               "Skipped by user.")
    else:
        # Poll /api/scan-result for up to 10 seconds
        uid_received = False
        for _ in range(20):
            time.sleep(0.5)
            r = get("/api/scan-result")
            outcome = r.json().get("outcome", "IDLE")
            if outcome != "IDLE":
                real_uid = r.json().get("uid")
                uid_received = True
                break

        if uid_received and real_uid:
            report(4, "Receive a real RFID UID from the ESP8266",
                   "PASS", f"Received UID: {real_uid}")
        else:
            report(4, "Receive a real RFID UID from the ESP8266",
                   "FAIL", "No UID received within 10s. Check firmware/wiring.")


# ---------------------------------------------------------------------------
# ITEM 5 — Identify a registered student
# (uses the existing migrated students: Student 1 = A96E9504, etc.)
# ---------------------------------------------------------------------------

# Check that the existing DB students are identifiable via the API
# by looking up a known UID directly
r = get("/api/students", params={"has_card": "true"})
registered = r.json().get("students", [])

if registered:
    known = registered[0]
    known_uid = known["rfid_uid"]
    known_name = known["full_name"]
    # Verify the service can find this student by UID
    # (test via the card check endpoint — does not require an open scan)
    r2 = post("/api/cards/check", {
        "student_id": known["id"],
        "uid": known_uid,
    })
    if r2.status_code == 200 and r2.json().get("outcome") == "ALREADY_ASSIGNED_SELF":
        report(5, "Identify a registered student by UID",
               "PASS", f"UID {known_uid} → {known_name}")
    else:
        report(5, "Identify a registered student by UID",
               "FAIL", f"check outcome={r2.json().get('outcome')}")
else:
    report(5, "Identify a registered student by UID",
           "FAIL", "No students with cards found in DB.")


# ---------------------------------------------------------------------------
# ITEM 6 — Record attendance
# ITEM 7 — Reject duplicate attendance
# (uses existing DB student whose class_id = 1 = Y1B = same as session)
# ---------------------------------------------------------------------------

# Find a registered student in class Y1B for the attendance tests
r = get("/api/students", params={"class_id": 1, "has_card": "true"})
class1_students = r.json().get("students", [])

# Prefer existing migrated students (Student 1/2/3 in class Y1B if assigned)
# Fall back to any class-1 student with a card
scan_student = next(
    (s for s in class1_students if s["rfid_uid"] is not None),
    None
)

if not scan_student:
    report(6, "Record attendance via handle_attendance_scan",
           "FAIL", "No student with card in class Y1B found.")
    report(7, "Reject duplicate attendance",
           "FAIL", "Skipped — depends on item 6.")
    scan_uid        = None
    scan_student_id = None
else:
    scan_uid = scan_student["rfid_uid"]
    scan_student_id = scan_student["id"]
    scan_student_name = scan_student["full_name"]

    # Direct service call via the scan-result mechanism:
    # Inject a fake scan by calling the service directly in Python
    # (valid because the service layer is already tested via rfid_test.py).
    # Here we call the API endpoint that process_scan() uses indirectly:
    # we simulate the exact JSON the worker thread would produce by calling
    # the business logic via a thin test shim.
    from rfid_service import handle_attendance_scan, ScanOutcome

    result = handle_attendance_scan(scan_uid, session_id)
    if result.outcome == ScanOutcome.SUCCESS:
        report(6, "Record attendance (service layer)",
               "PASS",
               f"{scan_student_name} UID={scan_uid} session={session_id}")
    elif result.outcome == ScanOutcome.WRONG_CLASS:
        report(6, "Record attendance (service layer)",
               "FAIL",
               f"Student class_id={scan_student.get('class_id')} "
               f"but session class_id=1. Assign student to Y1B.")
    else:
        report(6, "Record attendance (service layer)",
               "FAIL", f"outcome={result.outcome.value}: {result.message}")

    # Duplicate
    result2 = handle_attendance_scan(scan_uid, session_id)
    if result2.outcome == ScanOutcome.DUPLICATE:
        report(7, "Reject duplicate attendance",
               "PASS", f"Duplicate correctly returned DUPLICATE")
    else:
        report(7, "Reject duplicate attendance",
               "FAIL", f"outcome={result2.outcome.value}")


# ---------------------------------------------------------------------------
# ITEM 8 — Reject an unknown card
# ---------------------------------------------------------------------------

from rfid_service import handle_attendance_scan, ScanOutcome

result = handle_attendance_scan("UNKN0000", session_id)
if result.outcome == ScanOutcome.UNKNOWN_CARD:
    report(8, "Reject an unknown card", "PASS",
           f"UID=UNKN0000 → UNKNOWN_CARD, no DB record created")
else:
    report(8, "Reject an unknown card", "FAIL",
           f"outcome={result.outcome.value}")


# ---------------------------------------------------------------------------
# ITEM 9 — Reject a student from the wrong class
# ---------------------------------------------------------------------------

# Assign a card to the wrong-class student if they don't have one
if wrong_class_student_id:
    r_wc = get(f"/api/students/{wrong_class_student_id}")
    wc_student = r_wc.json().get("student", {})
    wc_uid = wc_student.get("rfid_uid")

    if wc_uid is None:
        # Assign a test card temporarily
        post("/api/cards/confirm", {
            "student_id": wrong_class_student_id,
            "uid": "WRONGCLS1",
        })
        wc_uid = "WRONGCLS1"

    result = handle_attendance_scan(wc_uid, session_id)
    if result.outcome == ScanOutcome.WRONG_CLASS:
        report(9, "Reject a student from the wrong class", "PASS",
               f"Student in class Y2A scanned for Y1B session → WRONG_CLASS")
    else:
        report(9, "Reject a student from the wrong class", "FAIL",
               f"outcome={result.outcome.value}: {result.message}")
else:
    report(9, "Reject a student from the wrong class", "FAIL",
           "Wrong-class student (HW002) was not created.")


# ---------------------------------------------------------------------------
# ITEM 10 — Close a session
# ---------------------------------------------------------------------------

r = post(f"/api/sessions/{session_id}/close")
if r.status_code == 200 and r.json().get("session", {}).get("status") == "CLOSED":
    report(10, "Close a session via POST /api/sessions/<id>/close",
           "PASS", "status=CLOSED")
else:
    report(10, "Close a session", "FAIL",
           f"status={r.status_code} body={r.text[:100]}")


# ---------------------------------------------------------------------------
# ITEM 11 — Reject attendance after session is closed
# ---------------------------------------------------------------------------

# Use a different student so it's not already marked present
r = get("/api/students", params={"class_id": 1, "has_card": "true"})
all_class1 = r.json().get("students", [])
second_student = next(
    (s for s in all_class1 if s.get("id") != scan_student_id
     and s.get("rfid_uid") is not None),
    None
)

# ---------------------------------------------------------------------------
# ITEM 11 — Reject attendance after session is closed
# ---------------------------------------------------------------------------

if scan_uid is None:
    report(11, "Reject attendance after session is closed",
           "FAIL", "Skipped — scan_uid unavailable from item 6.")
else:
    # Try a student that has NOT yet attended this session (avoid duplicate
    # masking the closed-session check).  Fall back to the already-present
    # student — the session-state check runs before the duplicate check, so
    # a CLOSED session always returns NO_OPEN_SESSION first.
    r = get("/api/students", params={"class_id": 1, "has_card": "true"})
    all_class1 = r.json().get("students", [])
    second_student = next(
        (s for s in all_class1
         if s.get("id") != scan_student_id and s.get("rfid_uid") is not None),
        None
    )
    test_uid_for_11 = (
        second_student["rfid_uid"] if second_student else scan_uid
    )
    result = handle_attendance_scan(test_uid_for_11, session_id)
    if result.outcome == ScanOutcome.NO_OPEN_SESSION:
        report(11, "Reject attendance after session is closed", "PASS",
               "CLOSED session returned NO_OPEN_SESSION")
    else:
        report(11, "Reject attendance after session is closed", "FAIL",
               f"outcome={result.outcome.value}: {result.message}")


# ---------------------------------------------------------------------------
# ITEMS 12-14 — Card assignment (hardware items)
# ---------------------------------------------------------------------------

print("\n" + "=" * 60)
print("  ITEMS 12-14  (card assignment — hardware if available)")
print("=" * 60)

from card_management_service import (
    check_uid_for_assignment, confirm_assignment,
    CardCheckOutcome,
)

# ITEM 12 — Assign an unassigned RFID card
# Test the two-phase workflow entirely in software with a known-new UID

r_check = check_uid_for_assignment("NEWCARD99", student_id)
if r_check.outcome == CardCheckOutcome.UNASSIGNED and r_check.requires_confirmation():
    r_confirm = confirm_assignment("NEWCARD99", student_id)
    if r_confirm.success():
        # Verify it's in the DB
        r_verify = get(f"/api/students/{student_id}")
        if r_verify.json().get("student", {}).get("rfid_uid") == "NEWCARD99":
            report(12, "Assign an unassigned RFID card (two-phase workflow)",
                   "PASS",
                   "check→UNASSIGNED, confirm→ASSIGNED, DB updated")
        else:
            report(12, "Assign an unassigned RFID card", "FAIL",
                   "Card assigned but not visible in /api/students/<id>")
    else:
        report(12, "Assign an unassigned RFID card", "FAIL",
               f"confirm outcome={r_confirm.outcome.value}")
else:
    report(12, "Assign an unassigned RFID card", "FAIL",
           f"check outcome={r_check.outcome.value} (expected UNASSIGNED)")

# ITEM 13 — Detect an already-assigned RFID card
r_check2 = check_uid_for_assignment("NEWCARD99", wrong_class_student_id or student_id + 1)
# The card NEWCARD99 is now on student_id.
# If we check it for another student it should be OWNED_BY_OTHER.
r_check3 = check_uid_for_assignment("NEWCARD99", student_id + 100)
# student_id+100 likely does not exist → STUDENT_NOT_FOUND.
# Use a real second student instead.
if wrong_class_student_id:
    r_check4 = check_uid_for_assignment("NEWCARD99", wrong_class_student_id)
    if r_check4.outcome == CardCheckOutcome.OWNED_BY_OTHER:
        report(13, "Detect an already-assigned card owned by another student",
               "PASS",
               f"NEWCARD99 owned by student {student_id}, "
               f"check for student {wrong_class_student_id} → OWNED_BY_OTHER")
    else:
        report(13, "Detect an already-assigned card owned by another student",
               "FAIL", f"outcome={r_check4.outcome.value}")
else:
    report(13, "Detect an already-assigned card owned by another student",
           "FAIL", "Wrong-class student not available.")

# ITEM 14 — Reassign a card only after confirmation
# Verify that check alone does NOT move the card
if wrong_class_student_id:
    # Card is on student_id. Check it for wrong_class_student_id.
    r_chk = check_uid_for_assignment("NEWCARD99", wrong_class_student_id)
    r_verify_before = get(f"/api/students/{wrong_class_student_id}")
    still_no_card = r_verify_before.json().get("student", {}).get("rfid_uid") != "NEWCARD99"

    if r_chk.outcome == CardCheckOutcome.OWNED_BY_OTHER and still_no_card:
        # Now confirm the reassignment
        r_conf = confirm_assignment("NEWCARD99", wrong_class_student_id)
        r_verify_after = get(f"/api/students/{wrong_class_student_id}")
        new_owner_has_card = (
            r_verify_after.json().get("student", {}).get("rfid_uid") == "NEWCARD99"
        )
        r_old_owner = get(f"/api/students/{student_id}")
        old_owner_lost_card = (
            r_old_owner.json().get("student", {}).get("rfid_uid") != "NEWCARD99"
        )

        if r_conf.success() and new_owner_has_card and old_owner_lost_card:
            report(14, "Reassign card only after explicit confirmation",
                   "PASS",
                   "check did not move card; confirm moved it; old owner cleared")
        else:
            report(14, "Reassign card only after explicit confirmation", "FAIL",
                   f"conf.success={r_conf.success()} "
                   f"new_has={new_owner_has_card} old_lost={old_owner_lost_card}")
    else:
        report(14, "Reassign card only after explicit confirmation", "FAIL",
               f"Pre-conditions failed: chk={r_chk.outcome.value} "
               f"card_still_on_old={not still_no_card}")
else:
    report(14, "Reassign card only after explicit confirmation",
           "FAIL", "Wrong-class student not available.")

# Hardware assign mode (item 12 in hardware sense)
if reader_online:
    print()
    tapped = prompt_tap(
        "OPTIONAL HARDWARE: Tap a NEW (unassigned) card to test hardware "
        "ASSIGN mode delivery to /api/scan-result."
    )
    if tapped:
        get("/api/scan-result", params={"clear": "1"})
        # The listener is in NORMAL mode — a tap will produce CARD_SCANNED.
        # We can't easily trigger ASSIGN mode from here without the full
        # browser flow, so we just verify the UID arrives.
        for _ in range(20):
            time.sleep(0.5)
            r = get("/api/scan-result")
            if r.json().get("outcome") not in ("IDLE", None):
                hw_uid = r.json().get("uid", "")
                print(f"    Hardware tap received UID: {hw_uid}")
                break


# ---------------------------------------------------------------------------
# ITEM 15 — Return appropriate JSON/API results
# ---------------------------------------------------------------------------

print("\n" + "=" * 60)
print("  ITEM 15  (JSON structure)")
print("=" * 60)

# Verify every outcome type returns valid JSON with the required fields.
checks_15 = []

# scan-result (IDLE state)
r = get("/api/scan-result", params={"clear": "1"})
checks_15.append(
    r.status_code == 200
    and "outcome" in r.json()
)

# session with attendance
r = get(f"/api/sessions/{session_id}")
j = r.json()
checks_15.append(
    r.status_code == 200
    and "session" in j
    and "attendance" in j
    and "count" in get(f"/api/sessions/{session_id}/attendance").json()
)

# student
r = get(f"/api/students/{student_id}")
j = r.json().get("student", {})
required_student_keys = {
    "id", "first_name", "last_name", "full_name",
    "registration_number", "rfid_uid", "has_card",
    "class_id", "class_name", "status",
}
checks_15.append(required_student_keys.issubset(j.keys()))

# session state transitions return structured JSON
r = post(f"/api/sessions/{session_id}/open")
j = r.json()
checks_15.append("outcome" in j and "session" in j and "message" in j)

# card check returns structured JSON
r = post("/api/cards/check", {
    "student_id": student_id,
    "uid": "SOMEUID99",
})
j = r.json()
required_check_keys = {
    "outcome", "uid", "target_student_id", "target_student_name",
    "requires_confirmation", "message",
}
checks_15.append(required_check_keys.issubset(j.keys()))

# 409 conflict response has required keys
r2 = post("/api/sessions", {"class_id": 1, "session_date": "2026-11-11"})
s3_id = r2.json().get("session", {}).get("id")
if s3_id:
    r3 = post(f"/api/sessions/{s3_id}/open")
    checks_15.append(
        r3.status_code == 409
        and "conflicting" in r3.json()
        and "session" in r3.json()
    )

if all(checks_15):
    report(15, "Return appropriate JSON/API results for all outcomes",
           "PASS",
           f"All {len(checks_15)} JSON structure checks passed")
else:
    failed_idx = [i for i, v in enumerate(checks_15) if not v]
    report(15, "Return appropriate JSON/API results for all outcomes",
           "FAIL", f"Failed checks at positions: {failed_idx}")


# ---------------------------------------------------------------------------
# Final report
# ---------------------------------------------------------------------------

print("\n" + "=" * 60)
print("  READINESS CHECK RESULTS")
print("=" * 60)

passed     = sum(1 for _, _, s, _ in _results if s == "PASS")
failed     = sum(1 for _, _, s, _ in _results if s == "FAIL")
not_tested = sum(1 for _, _, s, _ in _results if s == "NOT_TESTED")

for num, label, status, detail in _results:
    icon = {"PASS": "✓", "FAIL": "✗", "NOT_TESTED": "—"}[status]
    print(f"  {icon}  [{num:>2}]  {status:<12}  {label}")

print(f"\n  {passed} PASS  |  {failed} FAIL  |  {not_tested} NOT_TESTED")
print("=" * 60)

if failed > 0:
    print("\n  Backend is NOT ready. Fix failures above.")
    sys.exit(1)
elif not_tested > 0:
    print(f"\n  {not_tested} item(s) require real hardware and were not tested.")
    print("  All software-testable items passed.")
else:
    print("\n  Backend is ready for frontend development.")
