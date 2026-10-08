"""
crud_test.py — Tests for new CRUD endpoints.

Covers:
    Classes   POST, GET one, PATCH, DELETE (safe + FK-blocked)
    Subjects  POST, GET one, PATCH, DELETE (safe + FK-blocked)
    Teachers  POST, GET one, PATCH, DELETE (detach from sessions)
    Students  DELETE (safe + blocked by attendance)
    Sessions  PATCH (editable fields, protected fields, COMPLETED guard)
              DELETE (safe + blocked by attendance + COMPLETED guard)

Requires:
    python app.py  (running on 127.0.0.1:5000)

Run with:
    python crud_test.py
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


def delete(path):
    return requests.delete(BASE + path, timeout=5)


# ---------------------------------------------------------------------------
# Seed: remove crud_test leftovers from prior runs
# ---------------------------------------------------------------------------

def seed():
    conn = sqlite3.connect("attendance.db")
    conn.execute("PRAGMA foreign_keys = OFF")
    # attendance first (FK child of sessions and students)
    conn.execute(
        "DELETE FROM attendance WHERE student_id IN "
        "(SELECT id FROM students WHERE registration_number LIKE 'CRUD%')"
    )
    conn.execute(
        "DELETE FROM attendance WHERE session_id IN "
        "(SELECT id FROM attendance_sessions WHERE session_date "
        " IN ('2026-12-01','2026-12-02','2026-12-03','2026-12-04','2026-12-05'))"
    )
    conn.execute(
        "DELETE FROM attendance_sessions WHERE session_date "
        "IN ('2026-12-01','2026-12-02','2026-12-03','2026-12-04','2026-12-05')"
    )
    conn.execute(
        "DELETE FROM students WHERE registration_number LIKE 'CRUD%'"
    )
    conn.execute("DELETE FROM teachers WHERE username LIKE 'crud%'")
    conn.execute("DELETE FROM subjects WHERE name  LIKE 'CRUDSubj%'")
    conn.execute("DELETE FROM classes  WHERE name  LIKE 'CRUDCls%'")
    conn.execute("PRAGMA foreign_keys = ON")
    conn.commit()
    conn.close()


seed()


# ===========================================================================
# SECTION 1 — Classes
# ===========================================================================

section("1. CLASSES — POST")

r = post("/api/classes", {"name": "CRUDClsA", "description": "Alpha"})
check("POST /api/classes  returns 201",
      r.status_code == 201, f"got {r.status_code}: {r.text[:100]}")
cls_a = r.json().get("class", {})
cls_a_id = cls_a.get("id")
check("POST /api/classes  returns id",          cls_a_id is not None)
check("POST /api/classes  name correct",        cls_a.get("name") == "CRUDClsA")
check("POST /api/classes  description correct", cls_a.get("description") == "Alpha")

r = post("/api/classes", {"name": "CRUDClsA"})   # duplicate
check("POST /api/classes  duplicate name -> 409", r.status_code == 409)

r = post("/api/classes", {"description": "no name"})
check("POST /api/classes  missing name -> 400",   r.status_code == 400)

section("1. CLASSES — GET one")

r = get(f"/api/classes/{cls_a_id}")
check("GET /api/classes/<id>  returns 200", r.status_code == 200)
check("GET /api/classes/<id>  correct id",  r.json()["class"]["id"] == cls_a_id)

r = get("/api/classes/99999")
check("GET /api/classes/99999  returns 404", r.status_code == 404)

section("1. CLASSES — PATCH")

r = patch(f"/api/classes/{cls_a_id}", {"name": "CRUDClsAUpdated"})
check("PATCH /api/classes/<id>  returns 200",
      r.status_code == 200, f"got {r.status_code}: {r.text[:100]}")
check("PATCH /api/classes/<id>  name updated",
      r.json()["class"]["name"] == "CRUDClsAUpdated")

r = patch("/api/classes/99999", {"name": "Ghost"})
check("PATCH /api/classes/99999  returns 404", r.status_code == 404)

# Conflict: rename to an existing name
r_b = post("/api/classes", {"name": "CRUDClsB"})
cls_b_id = r_b.json()["class"]["id"]
r = patch(f"/api/classes/{cls_b_id}", {"name": "CRUDClsAUpdated"})
check("PATCH /api/classes/<id>  duplicate name -> 409", r.status_code == 409)

section("1. CLASSES — DELETE")

# Safe delete (no students, no sessions)
r_c = post("/api/classes", {"name": "CRUDClsC"})
cls_c_id = r_c.json()["class"]["id"]
r = delete(f"/api/classes/{cls_c_id}")
check("DELETE /api/classes/<id>  no refs -> 200",
      r.status_code == 200, f"got {r.status_code}: {r.text[:100]}")

r = delete("/api/classes/99999")
check("DELETE /api/classes/99999  returns 404", r.status_code == 404)

# Blocked: class has a student
r_stu = post("/api/students", {
    "first_name": "CRUD", "last_name": "DelTest",
    "registration_number": "CRUD001",
    "class_id": cls_a_id,
})
check("Setup: student assigned to class",
      r_stu.status_code == 201, r_stu.text[:80])
r = delete(f"/api/classes/{cls_a_id}")
check("DELETE /api/classes/<id>  has students -> 409",
      r.status_code == 409, f"got {r.status_code}: {r.text[:100]}")

# Blocked: class has a session (use cls_b which has no students)
# Need a helper teacher to create the session
r_th = post("/api/teachers", {"name": "CRUD TH", "username": "crudth1"})
th_id = r_th.json()["teacher"]["id"]
r_sess = post("/api/sessions", {
    "class_id": cls_b_id, "session_date": "2026-12-01",
    "teacher_id": th_id,
})
check("Setup: session references class",
      r_sess.status_code == 201, r_sess.text[:80])
r = delete(f"/api/classes/{cls_b_id}")
check("DELETE /api/classes/<id>  has sessions -> 409",
      r.status_code == 409, f"got {r.status_code}: {r.text[:100]}")


# ===========================================================================
# SECTION 2 — Subjects
# ===========================================================================

section("2. SUBJECTS — POST")

r = post("/api/subjects", {"name": "CRUDSubjA", "code": "CSA"})
check("POST /api/subjects  returns 201",
      r.status_code == 201, f"got {r.status_code}: {r.text[:100]}")
subj_a_id = r.json().get("subject", {}).get("id")
check("POST /api/subjects  returns id",   subj_a_id is not None)
check("POST /api/subjects  code saved",   r.json()["subject"]["code"] == "CSA")

r = post("/api/subjects", {"name": "CRUDSubjA"})
check("POST /api/subjects  duplicate name -> 409", r.status_code == 409)

r = post("/api/subjects", {})
check("POST /api/subjects  missing name -> 400",   r.status_code == 400)

section("2. SUBJECTS — GET one")

r = get(f"/api/subjects/{subj_a_id}")
check("GET /api/subjects/<id>  returns 200", r.status_code == 200)

r = get("/api/subjects/99999")
check("GET /api/subjects/99999  returns 404", r.status_code == 404)

section("2. SUBJECTS — PATCH")

r = patch(f"/api/subjects/{subj_a_id}", {"name": "CRUDSubjAUpdated", "code": "CSAU"})
check("PATCH /api/subjects/<id>  returns 200",
      r.status_code == 200, f"got {r.status_code}: {r.text[:100]}")
check("PATCH /api/subjects/<id>  name updated",
      r.json()["subject"]["name"] == "CRUDSubjAUpdated")
check("PATCH /api/subjects/<id>  code updated",
      r.json()["subject"]["code"] == "CSAU")

r = patch("/api/subjects/99999", {"name": "Ghost"})
check("PATCH /api/subjects/99999  returns 404", r.status_code == 404)

section("2. SUBJECTS — DELETE")

r_b_subj = post("/api/subjects", {"name": "CRUDSubjB"})
subj_b_id = r_b_subj.json()["subject"]["id"]
r = delete(f"/api/subjects/{subj_b_id}")
check("DELETE /api/subjects/<id>  no refs -> 200",
      r.status_code == 200, f"got {r.status_code}: {r.text[:100]}")

r = delete("/api/subjects/99999")
check("DELETE /api/subjects/99999  returns 404", r.status_code == 404)

# Blocked: subject referenced by session
r_sess_s = post("/api/sessions", {
    "class_id"  : cls_a_id,
    "session_date": "2026-12-02",
    "subject_id": subj_a_id,
})
check("Setup: session references subject",
      r_sess_s.status_code == 201, r_sess_s.text[:80])
r = delete(f"/api/subjects/{subj_a_id}")
check("DELETE /api/subjects/<id>  referenced by session -> 409",
      r.status_code == 409, f"got {r.status_code}: {r.text[:100]}")


# ===========================================================================
# SECTION 3 — Teachers
# ===========================================================================

section("3. TEACHERS — POST")

r = post("/api/teachers", {"name": "CRUD Teacher A", "username": "crudteacha"})
check("POST /api/teachers  returns 201",
      r.status_code == 201, f"got {r.status_code}: {r.text[:100]}")
teach_a_id = r.json().get("teacher", {}).get("id")
check("POST /api/teachers  returns id",  teach_a_id is not None)
check("POST /api/teachers  status=active",
      r.json()["teacher"]["status"] == "active")

r = post("/api/teachers", {"name": "Dup", "username": "crudteacha"})
check("POST /api/teachers  duplicate username -> 409", r.status_code == 409)

r = post("/api/teachers", {"name": "No User"})
check("POST /api/teachers  missing username -> 400", r.status_code == 400)

r = post("/api/teachers", {"username": "noname"})
check("POST /api/teachers  missing name -> 400",    r.status_code == 400)

section("3. TEACHERS — GET one")

r = get(f"/api/teachers/{teach_a_id}")
check("GET /api/teachers/<id>  returns 200", r.status_code == 200)
check("GET /api/teachers/<id>  correct id",  r.json()["teacher"]["id"] == teach_a_id)

r = get("/api/teachers/99999")
check("GET /api/teachers/99999  returns 404", r.status_code == 404)

section("3. TEACHERS — PATCH")

r = patch(f"/api/teachers/{teach_a_id}", {"name": "CRUD Teacher A Updated"})
check("PATCH /api/teachers/<id>  returns 200",
      r.status_code == 200, f"got {r.status_code}: {r.text[:100]}")
check("PATCH /api/teachers/<id>  name updated",
      r.json()["teacher"]["name"] == "CRUD Teacher A Updated")

r = patch("/api/teachers/99999", {"name": "Ghost"})
check("PATCH /api/teachers/99999  returns 404", r.status_code == 404)

section("3. TEACHERS — DELETE")

# Safe delete: teacher with no sessions
r_t2 = post("/api/teachers", {"name": "CRUD Teacher B", "username": "crudteachb"})
teach_b_id = r_t2.json()["teacher"]["id"]
r = delete(f"/api/teachers/{teach_b_id}")
check("DELETE /api/teachers/<id>  no sessions -> 200",
      r.status_code == 200, f"got {r.status_code}: {r.text[:100]}")

r = delete("/api/teachers/99999")
check("DELETE /api/teachers/99999  returns 404", r.status_code == 404)

# Teacher referenced by a session -> detach (200, not 409)
r_sess_t = post("/api/sessions", {
    "class_id"   : cls_a_id,
    "session_date": "2026-12-03",
    "teacher_id" : teach_a_id,
})
check("Setup: session references teacher",
      r_sess_t.status_code == 201, r_sess_t.text[:80])
sess_t_id = r_sess_t.json()["session"]["id"]

r = delete(f"/api/teachers/{teach_a_id}")
check("DELETE /api/teachers/<id>  referenced by session -> 200 (detach)",
      r.status_code == 200, f"got {r.status_code}: {r.text[:100]}")

r_check = get(f"/api/sessions/{sess_t_id}")
check("Session still exists after teacher deleted",
      r_check.status_code == 200)
check("Session teacher_id is NULL after teacher deleted",
      r_check.json()["session"]["teacher_id"] is None)


# ===========================================================================
# SECTION 4 — Students DELETE
# ===========================================================================

section("4. STUDENTS — DELETE")

# Safe delete (no attendance)
r_s2 = post("/api/students", {
    "first_name": "CRUD", "last_name": "NoAtt",
    "registration_number": "CRUD002",
})
s2_id = r_s2.json()["student"]["id"]
r = delete(f"/api/students/{s2_id}")
check("DELETE /api/students/<id>  no attendance -> 200",
      r.status_code == 200, f"got {r.status_code}: {r.text[:100]}")

r = delete("/api/students/99999")
check("DELETE /api/students/99999  returns 404", r.status_code == 404)

# Blocked: student has attendance
# Create a clean class, student, session, then insert attendance directly.
r_cls_att = post("/api/classes", {"name": "CRUDClsAtt"})
cls_att_id = r_cls_att.json()["class"]["id"]
r_stu_att = post("/api/students", {
    "first_name": "CRUD", "last_name": "HasAtt",
    "registration_number": "CRUD003",
    "class_id": cls_att_id,
})
stu_att_id = r_stu_att.json()["student"]["id"]
r_sess_att = post("/api/sessions", {
    "class_id": cls_att_id, "session_date": "2026-12-04",
})
check("Setup: session for attendance test",
      r_sess_att.status_code == 201, r_sess_att.text[:80])
sess_att_id = r_sess_att.json()["session"]["id"]

now_ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
conn_att = sqlite3.connect("attendance.db")
conn_att.execute(
    "INSERT INTO attendance "
    "(session_id, student_id, attendance_time, status, created_at) "
    "VALUES (?, ?, ?, 'present', ?)",
    (sess_att_id, stu_att_id, now_ts, now_ts)
)
conn_att.commit()
conn_att.close()

r = delete(f"/api/students/{stu_att_id}")
check("DELETE /api/students/<id>  has attendance -> 409",
      r.status_code == 409, f"got {r.status_code}: {r.text[:100]}")


# ===========================================================================
# SECTION 5 — Sessions PATCH
# ===========================================================================

section("5. SESSIONS — PATCH")

# Fresh SCHEDULED session
r_cls_p = post("/api/classes", {"name": "CRUDClsPatch"})
cls_p_id = r_cls_p.json()["class"]["id"]
r_cls_p2 = post("/api/classes", {"name": "CRUDClsPatch2"})
cls_p2_id = r_cls_p2.json()["class"]["id"]
r_subj_p = post("/api/subjects", {"name": "CRUDSubjPatch"})
subj_p_id = r_subj_p.json()["subject"]["id"]
r_teach_p = post("/api/teachers", {"name": "CRUD PTeach", "username": "crudpteach"})
teach_p_id = r_teach_p.json()["teacher"]["id"]

r_sess_p = post("/api/sessions", {
    "class_id": cls_p_id, "session_date": "2026-12-01",
})
check("Setup: SCHEDULED session for PATCH", r_sess_p.status_code == 201)
sess_p_id = r_sess_p.json()["session"]["id"]

# Patch all editable fields
r = patch(f"/api/sessions/{sess_p_id}", {
    "session_date": "2026-12-02",
    "start_time"  : "09:00",
    "end_time"    : "10:30",
    "teacher_id"  : teach_p_id,
    "subject_id"  : subj_p_id,
})
check("PATCH /api/sessions/<id>  returns 200",
      r.status_code == 200, f"got {r.status_code}: {r.text[:100]}")
s = r.json().get("session", {})
check("PATCH session_date updated",  s.get("session_date") == "2026-12-02")
check("PATCH start_time updated",    s.get("start_time")   == "09:00")
check("PATCH end_time updated",      s.get("end_time")      == "10:30")
check("PATCH teacher_id updated",    s.get("teacher_id")   == teach_p_id)
check("PATCH subject_id updated",    s.get("subject_id")   == subj_p_id)

# class_id allowed while SCHEDULED
r = patch(f"/api/sessions/{sess_p_id}", {"class_id": cls_p2_id})
check("PATCH class_id on SCHEDULED session -> 200",
      r.status_code == 200, f"got {r.status_code}: {r.text[:100]}")

# Protected fields are silently ignored
r = patch(f"/api/sessions/{sess_p_id}", {
    "status"      : "OPEN",
    "opened_at"   : "2026-01-01T00:00:00",
    "closed_at"   : "2026-01-01T00:00:00",
    "completed_at": "2026-01-01T00:00:00",
    "session_date": "2026-12-03",            # editable — should change
})
check("PATCH with protected fields -> 200 (protected ignored)",
      r.status_code == 200)
s2 = r.json().get("session", {})
check("PATCH: status NOT changed",       s2.get("status")    == "SCHEDULED")
check("PATCH: opened_at still NULL",     s2.get("opened_at") is None)
check("PATCH: session_date WAS changed", s2.get("session_date") == "2026-12-03")

# Non-existent session
r = patch("/api/sessions/99999", {"session_date": "2026-12-04"})
check("PATCH /api/sessions/99999  returns 404", r.status_code == 404)

# class_id not allowed once OPEN
post(f"/api/sessions/{sess_p_id}/open")
r = patch(f"/api/sessions/{sess_p_id}", {"class_id": cls_p_id})
check("PATCH class_id on OPEN session -> 400",
      r.status_code == 400, f"got {r.status_code}: {r.text[:100]}")
post(f"/api/sessions/{sess_p_id}/close")

# COMPLETED sessions cannot be patched
post(f"/api/sessions/{sess_p_id}/complete")
r = patch(f"/api/sessions/{sess_p_id}", {"session_date": "2026-12-05"})
check("PATCH COMPLETED session -> 400",
      r.status_code == 400, f"got {r.status_code}: {r.text[:100]}")


# ===========================================================================
# SECTION 6 — Sessions DELETE
# ===========================================================================

section("6. SESSIONS — DELETE")

# Safe delete: SCHEDULED, no attendance
r_cls_d = post("/api/classes", {"name": "CRUDClsDel"})
cls_d_id = r_cls_d.json()["class"]["id"]
r_sess_d = post("/api/sessions", {
    "class_id": cls_d_id, "session_date": "2026-12-01",
})
check("Setup: SCHEDULED session for DELETE test",
      r_sess_d.status_code == 201, r_sess_d.text[:80])
sess_d_id = r_sess_d.json()["session"]["id"]

r = delete(f"/api/sessions/{sess_d_id}")
check("DELETE /api/sessions/<id>  SCHEDULED, no attendance -> 200",
      r.status_code == 200, f"got {r.status_code}: {r.text[:100]}")

r = delete("/api/sessions/99999")
check("DELETE /api/sessions/99999  returns 404", r.status_code == 404)

# Blocked: session has attendance
r = delete(f"/api/sessions/{sess_att_id}")
check("DELETE /api/sessions/<id>  has attendance -> 409",
      r.status_code == 409, f"got {r.status_code}: {r.text[:100]}")

# Blocked: COMPLETED session
r = delete(f"/api/sessions/{sess_p_id}")
check("DELETE COMPLETED session -> 409",
      r.status_code == 409, f"got {r.status_code}: {r.text[:100]}")

# Allowed: CLOSED session with no attendance
r_cls_cl = post("/api/classes", {"name": "CRUDClsClosed"})
cls_cl_id = r_cls_cl.json()["class"]["id"]
r_sess_cl = post("/api/sessions", {
    "class_id": cls_cl_id, "session_date": "2026-12-02",
})
sess_cl_id = r_sess_cl.json()["session"]["id"]
post(f"/api/sessions/{sess_cl_id}/open")
post(f"/api/sessions/{sess_cl_id}/close")
r = delete(f"/api/sessions/{sess_cl_id}")
check("DELETE CLOSED session, no attendance -> 200",
      r.status_code == 200, f"got {r.status_code}: {r.text[:100]}")


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
