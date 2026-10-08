"""
api_test.py — API endpoint tests for the Flask teacher portal.

Requires the app to be running:
    python app.py

Run tests:
    python api_test.py
"""

import sys
import sqlite3
from datetime import datetime, timezone
import requests

BASE = "http://127.0.0.1:5000"

# ---------------------------------------------------------------------------
# Harness
# ---------------------------------------------------------------------------

_passed = 0
_failed = 0
_results = []


def check(label, condition, detail=""):
    global _passed, _failed
    if condition:
        _passed += 1
        _results.append(("PASS", label, ""))
        print(f"  PASS  {label}")
    else:
        _failed += 1
        _results.append(("FAIL", label, detail))
        print(f"  FAIL  {label}")
        if detail:
            print(f"        {detail}")


def section(title):
    print(f"\n{'=' * 60}")
    print(f"  {title}")
    print(f"{'=' * 60}")


def get(path, params=None):
    return requests.get(BASE + path, params=params, timeout=5)


def post(path, body=None):
    return requests.post(BASE + path, json=body,
                         headers={"Content-Type": "application/json"},
                         timeout=5)


def patch(path, body=None):
    return requests.patch(BASE + path, json=body,
                          headers={"Content-Type": "application/json"},
                          timeout=5)


# ---------------------------------------------------------------------------
# Seed reference data directly into SQLite so tests are self-contained
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
    # Remove any leftover test students/sessions from prior runs
    conn.execute(
        "DELETE FROM attendance WHERE session_id IN "
        "(SELECT id FROM attendance_sessions WHERE session_date IN "
        "('2026-10-30','2026-10-31','2026-11-01'))"
    )
    conn.execute(
        "DELETE FROM attendance_sessions "
        "WHERE session_date IN ('2026-10-30','2026-10-31','2026-11-01')"
    )
    conn.execute(
        "DELETE FROM students WHERE registration_number LIKE 'APITEST%' "
        "OR registration_number LIKE 'CARDTEST%'"
    )
    conn.commit()
    conn.close()


seed()


# ===========================================================================
# SECTION 1 — Core / health
# ===========================================================================

section("1. CORE ENDPOINTS")

r = get("/")
check("GET /  returns 200", r.status_code == 200,
      f"got {r.status_code}: {r.text[:100]}")
check("GET /  has 'app' key",    "app"    in r.json())
check("GET /  has 'reader' key", "reader" in r.json())

r = get("/api/health")
check("GET /api/health  returns 200",  r.status_code == 200)
check("GET /api/health  status = ok",  r.json().get("status") == "ok")
check("GET /api/health  has reader",   "reader" in r.json())

r = get("/api/dashboard")
check("GET /api/dashboard  returns 200",        r.status_code == 200)
check("GET /api/dashboard  has reader",         "reader"         in r.json())
check("GET /api/dashboard  has today_sessions", "today_sessions" in r.json())
check("GET /api/dashboard  has open_session",   "open_session"   in r.json())
check("GET /api/dashboard  has timestamp",      "timestamp"      in r.json())

r = get("/api/scan-result")
check("GET /api/scan-result  returns 200",  r.status_code == 200)
check("GET /api/scan-result  has outcome",  "outcome" in r.json())

r = get("/api/scan-result", params={"clear": "1"})
check("GET /api/scan-result?clear=1  returns 200", r.status_code == 200)


# ===========================================================================
# SECTION 2 — Reference data
# ===========================================================================

section("2. REFERENCE DATA")

r = get("/api/classes")
check("GET /api/classes  returns 200", r.status_code == 200)
classes = r.json().get("classes", [])
check("GET /api/classes  returns list",    isinstance(classes, list))
check("GET /api/classes  Y1B present",
      any(c["name"] == "Y1B" for c in classes))

r = get("/api/subjects")
check("GET /api/subjects  returns 200",  r.status_code == 200)
check("GET /api/subjects  returns list", isinstance(r.json().get("subjects"), list))

r = get("/api/teachers")
check("GET /api/teachers  returns 200",  r.status_code == 200)
check("GET /api/teachers  returns list", isinstance(r.json().get("teachers"), list))


# ===========================================================================
# SECTION 3 — Students
# ===========================================================================

section("3. STUDENTS")

# Create — happy path
r = post("/api/students", {
    "first_name": "Api", "last_name": "Tester",
    "registration_number": "APITEST001", "class_id": 1,
})
check("POST /api/students  returns 201",
      r.status_code == 201, f"got {r.status_code}: {r.text[:200]}")
student = r.json().get("student", {})
student_id = student.get("id")
check("POST /api/students  returns id",       student_id is not None)
check("POST /api/students  has_card = False", student.get("has_card") is False)
check("POST /api/students  class_name = Y1B", student.get("class_name") == "Y1B")

# Duplicate registration number -> 409
r = post("/api/students", {
    "first_name": "Dup", "last_name": "Student",
    "registration_number": "APITEST001",
})
check("POST /api/students  duplicate reg_number -> 409",
      r.status_code == 409, f"got {r.status_code}")

# Missing required fields -> 400
r = post("/api/students", {"last_name": "NoFirst"})
check("POST /api/students  missing first_name -> 400", r.status_code == 400)

r = post("/api/students", {"first_name": "No", "last_name": "Reg"})
check("POST /api/students  missing registration_number -> 400", r.status_code == 400)

# List all
r = get("/api/students")
check("GET /api/students  returns 200", r.status_code == 200)
all_students = r.json().get("students", [])
check("GET /api/students  list is a list",       isinstance(all_students, list))
check("GET /api/students  contains new student",
      any(s["id"] == student_id for s in all_students))

# Filter has_card=false
r = get("/api/students", params={"has_card": "false"})
check("GET /api/students?has_card=false  returns 200", r.status_code == 200)
no_card_list = r.json().get("students", [])
check("GET /api/students?has_card=false  all have no card",
      all(not s["has_card"] for s in no_card_list),
      str([s["id"] for s in no_card_list if s["has_card"]]))

# Filter has_card=true
r = get("/api/students", params={"has_card": "true"})
check("GET /api/students?has_card=true  returns 200", r.status_code == 200)
check("GET /api/students?has_card=true  all have a card",
      all(s["has_card"] for s in r.json().get("students", [])))

# Filter by class_id
r = get("/api/students", params={"class_id": 1})
check("GET /api/students?class_id=1  returns 200", r.status_code == 200)
check("GET /api/students?class_id=1  all in class 1",
      all(s["class_id"] == 1 for s in r.json().get("students", [])))

# Get one
r = get(f"/api/students/{student_id}")
check(f"GET /api/students/{student_id}  returns 200", r.status_code == 200)
check("GET /api/students/<id>  correct student",
      r.json().get("student", {}).get("id") == student_id)

# Get non-existent
r = get("/api/students/99999")
check("GET /api/students/99999  returns 404", r.status_code == 404)

# Update
r = patch(f"/api/students/{student_id}", {"first_name": "Updated"})
check("PATCH /api/students/<id>  returns 200", r.status_code == 200)
check("PATCH /api/students/<id>  name updated",
      r.json().get("student", {}).get("first_name") == "Updated")

# Update non-existent
r = patch("/api/students/99999", {"first_name": "Ghost"})
check("PATCH /api/students/99999  returns 404", r.status_code == 404)


# ===========================================================================
# SECTION 4 — Sessions
# ===========================================================================

section("4. SESSIONS")

# Create: missing class_id
r = post("/api/sessions", {})
check("POST /api/sessions  missing class_id -> 400", r.status_code == 400)

# Create: invalid class
r = post("/api/sessions", {"class_id": 99999})
check("POST /api/sessions  invalid class -> 400", r.status_code == 400)

# Create: valid
r = post("/api/sessions", {
    "class_id": 1, "session_date": "2026-10-30",
    "teacher_id": 1, "subject_id": 1,
    "start_time": "08:00", "end_time": "09:30",
})
check("POST /api/sessions  returns 201",
      r.status_code == 201, f"got {r.status_code}: {r.text[:200]}")
sess = r.json().get("session", {})
session_id = sess.get("id")
check("POST /api/sessions  returns session id",       session_id is not None)
check("POST /api/sessions  status = SCHEDULED",       sess.get("status") == "SCHEDULED")
check("POST /api/sessions  class_name = Y1B",         sess.get("class_name") == "Y1B")
check("POST /api/sessions  subject_name = Mathematics",
      sess.get("subject_name") == "Mathematics")

# List unfiltered
r = get("/api/sessions")
check("GET /api/sessions  returns 200", r.status_code == 200)
sessions_list = r.json().get("sessions", [])
check("GET /api/sessions  returns list", isinstance(sessions_list, list))
check("GET /api/sessions  contains new session",
      any(s["id"] == session_id for s in sessions_list))

# Filter by status
r = get("/api/sessions", params={"status": "SCHEDULED"})
check("GET /api/sessions?status=SCHEDULED  returns 200", r.status_code == 200)
check("GET /api/sessions?status=SCHEDULED  all SCHEDULED",
      all(s["status"] == "SCHEDULED" for s in r.json().get("sessions", [])))

# Filter by date
r = get("/api/sessions", params={"date": "2026-10-30"})
check("GET /api/sessions?date=2026-10-30  returns 200", r.status_code == 200)
check("GET /api/sessions?date=2026-10-30  all match date",
      all(s["session_date"] == "2026-10-30" for s in r.json().get("sessions", [])))

# Get one
r = get(f"/api/sessions/{session_id}")
check(f"GET /api/sessions/{session_id}  returns 200", r.status_code == 200)
check("GET /api/sessions/<id>  has session key",    "session"    in r.json())
check("GET /api/sessions/<id>  has attendance key", "attendance" in r.json())
check("GET /api/sessions/<id>  correct id",
      r.json()["session"]["id"] == session_id)

# Get non-existent
r = get("/api/sessions/99999")
check("GET /api/sessions/99999  returns 404", r.status_code == 404)

# Open
r = post(f"/api/sessions/{session_id}/open")
check(f"POST /api/sessions/{session_id}/open  returns 200",
      r.status_code == 200, f"got {r.status_code}: {r.text[:200]}")
check("Session status = OPEN after open",
      r.json().get("session", {}).get("status") == "OPEN")
check("opened_at is set",
      r.json().get("session", {}).get("opened_at") is not None)

# One-open-at-a-time: create second session and try to open it
r2 = post("/api/sessions", {"class_id": 1, "session_date": "2026-10-31"})
session2_id = r2.json().get("session", {}).get("id")
r3 = post(f"/api/sessions/{session2_id}/open")
check("Opening second session while one is OPEN -> 409",
      r3.status_code == 409, f"got {r3.status_code}: {r3.text[:200]}")
check("409 response has 'conflicting' key",
      "conflicting" in r3.json())
check("409 conflicting.id matches the open session",
      r3.json().get("conflicting", {}).get("id") == session_id)

# Attendance endpoint (empty for now)
r = get(f"/api/sessions/{session_id}/attendance")
check(f"GET /api/sessions/{session_id}/attendance  returns 200",
      r.status_code == 200)
check("attendance response has 'count' key", "count" in r.json())
check("attendance response has 'attendance' list",
      isinstance(r.json().get("attendance"), list))
check("count = 0 (no scans yet)", r.json()["count"] == 0)

# Close
r = post(f"/api/sessions/{session_id}/close")
check(f"POST /api/sessions/{session_id}/close  returns 200",
      r.status_code == 200, f"got {r.status_code}: {r.text[:200]}")
check("Session status = CLOSED after close",
      r.json().get("session", {}).get("status") == "CLOSED")
check("closed_at is set",
      r.json().get("session", {}).get("closed_at") is not None)

# Close again (invalid transition)
r = post(f"/api/sessions/{session_id}/close")
check("Closing already-CLOSED session -> 400", r.status_code == 400)

# Reopen (CLOSED -> OPEN)
r = post(f"/api/sessions/{session_id}/open")
check("Reopening CLOSED session returns 200",
      r.status_code == 200, f"got {r.status_code}: {r.text[:200]}")
check("Status = OPEN after reopen",
      r.json().get("session", {}).get("status") == "OPEN")

# Complete
r = post(f"/api/sessions/{session_id}/complete")
check(f"POST /api/sessions/{session_id}/complete  returns 200",
      r.status_code == 200, f"got {r.status_code}: {r.text[:200]}")
check("Session status = COMPLETED",
      r.json().get("session", {}).get("status") == "COMPLETED")
check("completed_at is set",
      r.json().get("session", {}).get("completed_at") is not None)

# Complete again (invalid)
r = post(f"/api/sessions/{session_id}/complete")
check("Completing already-COMPLETED session -> 400", r.status_code == 400)

# Open completed (invalid)
r = post(f"/api/sessions/{session_id}/open")
check("Opening COMPLETED session -> 400", r.status_code == 400)

# Close completed (invalid)
r = post(f"/api/sessions/{session_id}/close")
check("Closing COMPLETED session -> 400", r.status_code == 400)

# Open non-existent
r = post("/api/sessions/99999/open")
check("POST /api/sessions/99999/open  returns 404", r.status_code == 404)


# ===========================================================================
# SECTION 5 — Card management
# ===========================================================================

section("5. CARD MANAGEMENT")

# Seed two students: one without card, one with card
r = post("/api/students", {
    "first_name": "Card", "last_name": "NoCard",
    "registration_number": "CARDTEST001", "class_id": 1,
})
check("Created student without card", r.status_code == 201,
      f"got {r.status_code}: {r.text[:100]}")
no_card_student_id = r.json().get("student", {}).get("id")

r = post("/api/students", {
    "first_name": "Card", "last_name": "HasCard",
    "registration_number": "CARDTEST002", "class_id": 1,
    "rfid_uid": "TESTUID1",
})
check("Created student with card TESTUID1", r.status_code == 201,
      f"got {r.status_code}: {r.text[:100]}")
has_card_student_id = r.json().get("student", {}).get("id")

# GET /api/cards
r = get("/api/cards")
check("GET /api/cards  returns 200", r.status_code == 200)
check("GET /api/cards  has with_cards list",
      isinstance(r.json().get("with_cards"), list))
check("GET /api/cards  has without_cards list",
      isinstance(r.json().get("without_cards"), list))
check("GET /api/cards  HasCard student appears in with_cards",
      any(s["id"] == has_card_student_id
          for s in r.json().get("with_cards", [])))
check("GET /api/cards  NoCard student appears in without_cards",
      any(s["id"] == no_card_student_id
          for s in r.json().get("without_cards", [])))

# POST /api/cards/check — unassigned UID for no-card student
r = post("/api/cards/check", {
    "student_id": no_card_student_id,
    "uid": "BRANDNEWUID",
})
check("POST /api/cards/check  returns 200", r.status_code == 200)
check("POST /api/cards/check  outcome = UNASSIGNED",
      r.json().get("outcome") == "UNASSIGNED",
      f"got: {r.json().get('outcome')}")
check("POST /api/cards/check  requires_confirmation = True",
      r.json().get("requires_confirmation") is True)

# POST /api/cards/check — already assigned to same student
r = post("/api/cards/check", {
    "student_id": has_card_student_id,
    "uid": "TESTUID1",
})
check("POST /api/cards/check  own card -> ALREADY_ASSIGNED_SELF",
      r.json().get("outcome") == "ALREADY_ASSIGNED_SELF",
      f"got: {r.json().get('outcome')}")
check("POST /api/cards/check  requires_confirmation = False",
      r.json().get("requires_confirmation") is False)

# POST /api/cards/check — owned by another student
r = post("/api/cards/check", {
    "student_id": no_card_student_id,
    "uid": "TESTUID1",   # belongs to has_card_student
})
check("POST /api/cards/check  other owner -> OWNED_BY_OTHER",
      r.json().get("outcome") == "OWNED_BY_OTHER",
      f"got: {r.json().get('outcome')}")
check("POST /api/cards/check  current_owner_id is correct",
      r.json().get("current_owner_id") == has_card_student_id)
check("POST /api/cards/check  requires_confirmation = True",
      r.json().get("requires_confirmation") is True)

# POST /api/cards/check — missing fields
r = post("/api/cards/check", {"uid": "SOMEUID"})
check("POST /api/cards/check  missing student_id -> 400", r.status_code == 400)

r = post("/api/cards/check", {"student_id": no_card_student_id})
check("POST /api/cards/check  missing uid -> 400", r.status_code == 400)

# POST /api/cards/confirm — assign unassigned card
r = post("/api/cards/confirm", {
    "student_id": no_card_student_id,
    "uid": "BRANDNEWUID",
})
check("POST /api/cards/confirm  returns 200", r.status_code == 200,
      f"got {r.status_code}: {r.text[:200]}")
check("POST /api/cards/confirm  outcome = ASSIGNED",
      r.json().get("outcome") == "ASSIGNED",
      f"got: {r.json().get('outcome')}")
check("POST /api/cards/confirm  student now has card",
      r.json().get("student", {}).get("has_card") is True)
check("POST /api/cards/confirm  uid matches",
      r.json().get("uid") == "BRANDNEWUID")

# POST /api/cards/confirm — reassign card from one student to another
r = post("/api/cards/confirm", {
    "student_id": no_card_student_id,
    "uid": "TESTUID1",   # move from has_card_student to no_card_student
})
check("POST /api/cards/confirm  reassign returns 200",
      r.status_code == 200, f"got {r.status_code}: {r.text[:200]}")
check("POST /api/cards/confirm  outcome = REASSIGNED",
      r.json().get("outcome") == "REASSIGNED",
      f"got: {r.json().get('outcome')}")
check("POST /api/cards/confirm  previous_owner_id correct",
      r.json().get("previous_owner_id") == has_card_student_id)

# Verify old owner lost card
r = get(f"/api/students/{has_card_student_id}")
check("Old owner now has no card after reassignment",
      r.json().get("student", {}).get("has_card") is False,
      f"has_card = {r.json().get('student', {}).get('has_card')}")

# POST /api/cards/confirm — missing fields
r = post("/api/cards/confirm", {"uid": "SOMEUID"})
check("POST /api/cards/confirm  missing student_id -> 400", r.status_code == 400)

r = post("/api/cards/confirm", {"student_id": no_card_student_id})
check("POST /api/cards/confirm  missing uid -> 400", r.status_code == 400)

# POST /api/cards/cancel
r = post("/api/cards/cancel")
check("POST /api/cards/cancel  returns 200", r.status_code == 200)
check("POST /api/cards/cancel  outcome = CANCELLED",
      r.json().get("outcome") == "CANCELLED")

# POST /api/cards/assign — reader offline (expected 503)
r = post("/api/cards/assign", {"student_id": no_card_student_id})
check("POST /api/cards/assign  reader offline -> 503",
      r.status_code == 503, f"got {r.status_code}: {r.text[:200]}")

# POST /api/cards/unassign
r = post("/api/cards/unassign", {"student_id": no_card_student_id})
check("POST /api/cards/unassign  returns 200",
      r.status_code == 200, f"got {r.status_code}: {r.text[:200]}")
check("POST /api/cards/unassign  outcome = UNASSIGNED",
      r.json().get("outcome") == "UNASSIGNED",
      f"got: {r.json().get('outcome')}")
check("POST /api/cards/unassign  student has no card",
      r.json().get("student", {}).get("has_card") is False)

# POST /api/cards/unassign — missing student_id
r = post("/api/cards/unassign", {})
check("POST /api/cards/unassign  missing student_id -> 400", r.status_code == 400)


# ===========================================================================
# SECTION 6 — Error handling
# ===========================================================================

section("6. ERROR HANDLING")

r = get("/api/notaroute")
check("GET /api/notaroute  returns 404", r.status_code == 404)
check("404 response has 'error' key", "error" in r.json())

r = get("/api/sessions/abc")   # non-integer id
check("GET /api/sessions/abc  returns 404", r.status_code == 404)


# ===========================================================================
# Final report
# ===========================================================================

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
    sys.exit(1)
else:
    print("\n  All tests passed.")
