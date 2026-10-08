"""
app.py — Flask teacher portal for the RFID attendance prototype.

Architecture:
    One RFIDListener daemon thread reads the serial port.
    A separate rfid_worker thread pops events from the listener queue,
    calls the business logic (process_scan), and writes results into the
    shared rfid_state singleton.
    Flask routes read from rfid_state and call service functions.

Startup:
    python app.py

The RFID listener will attempt to connect on startup.
If the reader is not plugged in, the app still starts and serves the
portal — the dashboard shows READER OFFLINE and retries are not performed
automatically (the teacher refreshes or reconnects the device).

Configuration (environment variables or defaults):
    RFID_PORT     serial port for the RFID reader  (default: COM7)
    FLASK_HOST    host to bind                      (default: 127.0.0.1)
    FLASK_PORT    port to bind                      (default: 5000)
    FLASK_DEBUG   enable debug mode                 (default: 0)
"""

import os
import threading
from datetime import datetime, timezone

from flask import Flask, jsonify, request, abort

from database          import initialize_database
from rfid_state        import rfid_state
from rfid_listener     import RFIDListener, EventKind, create_listener
from rfid_service      import process_scan, ScanOutcome
from session_service   import (
    create_session, open_session, close_session, complete_session,
    get_session, get_open_session, list_sessions, get_session_attendance,
    SessionOutcome,
)
from card_management_service import (
    check_uid_for_assignment, confirm_assignment, cancel_assignment,
    create_student, update_student, get_student, list_students,
    students_with_cards, students_without_cards,
    CardCheckOutcome, StudentOutcome,
)

# ---------------------------------------------------------------------------
# App factory
# ---------------------------------------------------------------------------

app = Flask(__name__)
app.config["JSON_SORT_KEYS"] = False


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")


# ---------------------------------------------------------------------------
# RFID listener lifecycle
# ---------------------------------------------------------------------------

_listener: RFIDListener = None
_listener_lock = threading.Lock()
_worker_thread: threading.Thread = None


def _rfid_worker(listener: RFIDListener):
    """
    Worker thread: pops events from the listener queue and dispatches them.

    CARD_SCANNED  → process_scan(uid) → rfid_state.set_scan_result()
    ASSIGN_SCANNED → rfid_state.set_pending_assignment(uid)
    ERROR         → rfid_state.set_reader_offline() if serial error
    """
    for event in listener.events():
        if event.kind == EventKind.CARD_SCANNED:
            try:
                result = process_scan(event.uid)
                rfid_state.set_scan_result(result.to_dict())
            except Exception as exc:
                rfid_state.set_scan_result({
                    "outcome": "ERROR",
                    "uid": event.uid,
                    "message": f"Error processing scan: {exc}",
                })

        elif event.kind == EventKind.ASSIGN_SCANNED:
            rfid_state.set_pending_assignment(event.uid)

        elif event.kind == EventKind.ERROR:
            if "SerialException" in event.message:
                rfid_state.set_reader_offline()


def start_rfid_listener(port: str):
    """
    Create the RFIDListener, attempt to connect, start the worker thread.
    Called once at app startup. Safe to call when hardware is absent —
    errors are caught and rfid_state is set to offline.
    """
    global _listener, _worker_thread

    with _listener_lock:
        if _listener is not None:
            return  # already started

        listener = create_listener(port)
        try:
            listener.start()
            rfid_state.set_reader_online(True, port)
            print(f"[RFID] Listener started on {port}")
        except Exception as exc:
            rfid_state.set_reader_offline()
            print(f"[RFID] Could not connect to {port}: {exc}")
            print("[RFID] Portal running — reader OFFLINE")
            # Store the non-started listener so status can report the port.
            rfid_state.set_reader_online(False)
            rfid_state._reader_port = port  # record configured port
            _listener = listener
            return

        _listener = listener

        worker = threading.Thread(
            target=_rfid_worker,
            args=(listener,),
            name="rfid-worker",
            daemon=True,
        )
        worker.start()
        _worker_thread = worker


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _ok(data: dict, status: int = 200):
    return jsonify(data), status


def _err(message: str, status: int = 400):
    return jsonify({"error": message}), status


def _session_outcome_to_status(outcome: SessionOutcome) -> int:
    if outcome in (
        SessionOutcome.CREATED,
        SessionOutcome.OPENED,
        SessionOutcome.CLOSED,
        SessionOutcome.COMPLETED,
        SessionOutcome.OK,
    ):
        return 200
    if outcome == SessionOutcome.NOT_FOUND:
        return 404
    return 400


# ---------------------------------------------------------------------------
# Dashboard
# ---------------------------------------------------------------------------

@app.route("/")
def index():
    """
    Simple JSON status page.
    The frontend (to be built) will replace this with HTML.
    """
    open_sess = get_open_session()
    return _ok({
        "app"         : "RFID Attendance Portal",
        "reader"      : rfid_state.status(),
        "open_session": open_sess.to_dict() if open_sess else None,
    })


@app.route("/api/dashboard")
def api_dashboard():
    """Full dashboard snapshot: reader status + open session + today's sessions."""
    from datetime import date
    today = date.today().isoformat()
    today_sessions = list_sessions(session_date=today)
    open_sess      = get_open_session()

    return _ok({
        "reader"        : rfid_state.status(),
        "open_session"  : open_sess.to_dict() if open_sess else None,
        "today_sessions": [s.to_dict() for s in today_sessions],
        "timestamp"     : _now_iso(),
    })


@app.route("/api/scan-result")
def api_scan_result():
    """
    Return the most recent RFID scan result.

    The frontend polls this endpoint (or uses SSE — polling is simpler
    for the prototype).  Pass ?clear=1 to consume and clear the result.
    """
    result = rfid_state.get_scan_result()
    if request.args.get("clear") == "1":
        rfid_state.clear_scan_result()
    return _ok(result)


# ---------------------------------------------------------------------------
# Sessions
# ---------------------------------------------------------------------------

@app.route("/api/sessions", methods=["GET"])
def api_list_sessions():
    """
    List sessions.

    Query params:
        class_id    filter by class
        teacher_id  filter by teacher
        status      filter by status (SCHEDULED|OPEN|CLOSED|COMPLETED)
        date        filter by session_date (YYYY-MM-DD)
    """
    class_id     = request.args.get("class_id",    type=int)
    teacher_id   = request.args.get("teacher_id",  type=int)
    status       = request.args.get("status")
    session_date = request.args.get("date")

    sessions = list_sessions(
        class_id=class_id,
        teacher_id=teacher_id,
        status=status,
        session_date=session_date,
    )
    return _ok({"sessions": [s.to_dict() for s in sessions]})


@app.route("/api/sessions", methods=["POST"])
def api_create_session():
    """
    Create a new attendance session.

    Body (JSON):
        class_id      int  required
        session_date  str  optional (defaults to today)
        teacher_id    int  optional
        subject_id    int  optional
        start_time    str  optional  "HH:MM"
        end_time      str  optional  "HH:MM"
    """
    body = request.get_json(silent=True) or {}

    class_id = body.get("class_id")
    if not class_id:
        return _err("class_id is required", 400)

    result = create_session(
        class_id     = class_id,
        session_date = body.get("session_date"),
        teacher_id   = body.get("teacher_id"),
        subject_id   = body.get("subject_id"),
        start_time   = body.get("start_time"),
        end_time     = body.get("end_time"),
    )

    if not result.success():
        return _err(result.message, 400)

    return _ok({
        "session": result.session.to_dict(),
        "message": result.message,
    }, 201)


@app.route("/api/sessions/<int:session_id>", methods=["GET"])
def api_get_session(session_id: int):
    """Return one session by id, including its attendance records."""
    session = get_session(session_id)
    if session is None:
        return _err(f"Session {session_id} not found", 404)

    attendance = get_session_attendance(session_id)
    return _ok({
        "session"   : session.to_dict(),
        "attendance": [a.to_dict() for a in attendance],
    })


@app.route("/api/sessions/<int:session_id>/open", methods=["POST"])
def api_open_session(session_id: int):
    """
    Open a session (SCHEDULED|CLOSED -> OPEN).

    Returns 409 if another session is already OPEN, with details of
    the conflicting session so the caller can close it first.
    """
    result = open_session(session_id)

    if result.outcome == SessionOutcome.NOT_FOUND:
        return _err(result.message, 404)

    if result.outcome == SessionOutcome.ALREADY_OPEN_EXISTS:
        return _ok({
            "outcome"    : result.outcome.value,
            "message"    : result.message,
            "session"    : result.session.to_dict() if result.session else None,
            "conflicting": result.conflicting.to_dict() if result.conflicting else None,
        }, 409)

    if not result.success():
        return _err(result.message, 400)

    return _ok({
        "outcome": result.outcome.value,
        "session": result.session.to_dict(),
        "message": result.message,
    })


@app.route("/api/sessions/<int:session_id>/close", methods=["POST"])
def api_close_session(session_id: int):
    """Close an OPEN session (OPEN -> CLOSED)."""
    result = close_session(session_id)

    if result.outcome == SessionOutcome.NOT_FOUND:
        return _err(result.message, 404)
    if not result.success():
        return _err(result.message, 400)

    return _ok({
        "outcome": result.outcome.value,
        "session": result.session.to_dict(),
        "message": result.message,
    })


@app.route("/api/sessions/<int:session_id>/complete", methods=["POST"])
def api_complete_session(session_id: int):
    """Permanently complete a session (OPEN|CLOSED -> COMPLETED)."""
    result = complete_session(session_id)

    if result.outcome == SessionOutcome.NOT_FOUND:
        return _err(result.message, 404)
    if not result.success():
        return _err(result.message, 400)

    return _ok({
        "outcome": result.outcome.value,
        "session": result.session.to_dict(),
        "message": result.message,
    })


@app.route("/api/sessions/<int:session_id>/attendance", methods=["GET"])
def api_session_attendance(session_id: int):
    """Return all attendance records for a session."""
    session = get_session(session_id)
    if session is None:
        return _err(f"Session {session_id} not found", 404)

    rows = get_session_attendance(session_id)
    return _ok({
        "session_id" : session_id,
        "count"      : len(rows),
        "attendance" : [r.to_dict() for r in rows],
    })


# ---------------------------------------------------------------------------
# Students
# ---------------------------------------------------------------------------

@app.route("/api/students", methods=["GET"])
def api_list_students():
    """
    List students.

    Query params:
        class_id    filter by class
        status      filter by status (active|inactive)
        has_card    filter by card presence (true|false)
    """
    class_id_str = request.args.get("class_id", type=int)
    status       = request.args.get("status")
    has_card_str = request.args.get("has_card")

    has_card = None
    if has_card_str is not None:
        has_card = has_card_str.lower() in ("1", "true", "yes")

    students = list_students(
        class_id=class_id_str,
        status=status,
        has_card=has_card,
    )
    return _ok({"students": [s.to_dict() for s in students]})


@app.route("/api/students", methods=["POST"])
def api_create_student():
    """
    Create a student.

    Body (JSON):
        first_name           str  required
        last_name            str  required
        registration_number  str  required
        class_id             int  optional
        rfid_uid             str  optional
    """
    body = request.get_json(silent=True) or {}

    first_name = (body.get("first_name") or "").strip()
    last_name  = (body.get("last_name")  or "").strip()
    reg_num    = (body.get("registration_number") or "").strip()

    if not first_name:
        return _err("first_name is required", 400)
    if not last_name:
        return _err("last_name is required", 400)
    if not reg_num:
        return _err("registration_number is required", 400)

    result = create_student(
        first_name          = first_name,
        last_name           = last_name,
        registration_number = reg_num,
        class_id            = body.get("class_id"),
        rfid_uid            = body.get("rfid_uid"),
    )

    if not result.success():
        status_code = 409 if result.outcome == StudentOutcome.DUPLICATE_REG_NUMBER else 400
        return _err(result.message, status_code)

    return _ok({
        "student": result.student.to_dict(),
        "message": result.message,
    }, 201)


@app.route("/api/students/<int:student_id>", methods=["GET"])
def api_get_student(student_id: int):
    """Return one student by id."""
    student = get_student(student_id)
    if student is None:
        return _err(f"Student {student_id} not found", 404)
    return _ok({"student": student.to_dict()})


@app.route("/api/students/<int:student_id>", methods=["PATCH"])
def api_update_student(student_id: int):
    """
    Update mutable student fields.

    Body (JSON): any subset of first_name, last_name,
                 registration_number, class_id, status.
    Note: rfid_uid is managed through /api/cards endpoints only.
    """
    body = request.get_json(silent=True) or {}

    result = update_student(
        student_id          = student_id,
        first_name          = body.get("first_name"),
        last_name           = body.get("last_name"),
        registration_number = body.get("registration_number"),
        class_id            = body.get("class_id"),
        status              = body.get("status"),
    )

    if result.outcome == StudentOutcome.NOT_FOUND:
        return _err(result.message, 404)
    if not result.success():
        return _err(result.message, 400)

    return _ok({
        "student": result.student.to_dict(),
        "message": result.message,
    })


# ---------------------------------------------------------------------------
# RFID card management
# ---------------------------------------------------------------------------

@app.route("/api/cards", methods=["GET"])
def api_list_cards():
    """
    Return two lists: students with cards and students without cards.
    """
    with_cards    = students_with_cards()
    without_cards = students_without_cards()
    return _ok({
        "with_cards"   : [s.to_dict() for s in with_cards],
        "without_cards": [s.to_dict() for s in without_cards],
    })


@app.route("/api/cards/check", methods=["POST"])
def api_check_card():
    """
    Phase 1 of card assignment: inspect the current state of a UID
    without modifying anything.

    Body (JSON):
        student_id  int  required
        uid         str  required  (the scanned UID)

    Returns a CardCheckResult describing whether the card is unassigned,
    already assigned to this student, or owned by another student.
    The frontend shows this to the teacher before calling /api/cards/assign.
    """
    body       = request.get_json(silent=True) or {}
    student_id = body.get("student_id")
    uid        = body.get("uid", "").strip()

    if not student_id:
        return _err("student_id is required", 400)
    if not uid:
        return _err("uid is required", 400)

    result = check_uid_for_assignment(uid, student_id)
    http_status = 200

    if result.outcome in (
        CardCheckOutcome.STUDENT_NOT_FOUND,
        CardCheckOutcome.STUDENT_INACTIVE,
    ):
        http_status = 400

    return _ok(result.to_dict(), http_status)


@app.route("/api/cards/assign", methods=["POST"])
def api_assign_card():
    """
    Initiate hardware card assignment mode, wait for a card tap, then
    return the scanned UID so the frontend can show a confirmation prompt.

    This endpoint:
        1. Sends ASSIGN to the ESP8266 via the listener.
        2. Waits up to `timeout` seconds for the reader to scan a card.
        3. Returns the scanned UID.

    The frontend then calls POST /api/cards/confirm to write the assignment.

    Body (JSON):
        student_id  int    required
        timeout     float  optional  seconds to wait (default 30)

    If no reader is connected, returns 503.
    If timeout expires before a card is tapped, returns 408.
    """
    body       = request.get_json(silent=True) or {}
    student_id = body.get("student_id")
    timeout    = float(body.get("timeout", 30))

    if not student_id:
        return _err("student_id is required", 400)

    student = get_student(student_id)
    if student is None:
        return _err(f"Student {student_id} not found", 404)

    if not rfid_state.reader_online:
        return _err("RFID reader is offline. Check the USB connection.", 503)

    # Clear any stale pending assignment.
    rfid_state.pop_pending_assignment()
    rfid_state.assign_event.clear()

    # Tell the listener to enter ASSIGN mode.
    global _listener
    with _listener_lock:
        if _listener is None:
            return _err("RFID listener not started", 503)
        try:
            _listener.send_command("ASSIGN")
        except Exception as exc:
            return _err(f"Could not send ASSIGN command: {exc}", 503)

    # Wait for the worker thread to deposit the scanned UID.
    got_card = rfid_state.assign_event.wait(timeout=timeout)

    if not got_card:
        return _ok({
            "outcome": "TIMEOUT",
            "message": f"No card scanned within {timeout}s. Try again.",
            "student": student.to_dict(),
        }, 408)

    pending = rfid_state.pop_pending_assignment()
    if pending is None:
        return _err("Assignment event fired but no UID was captured.", 500)

    uid = pending["uid"]

    # Run phase-1 check so the frontend can show the confirmation screen.
    check = check_uid_for_assignment(uid, student_id)

    return _ok({
        "outcome"    : "CARD_SCANNED",
        "uid"        : uid,
        "scanned_at" : pending["scanned_at"],
        "student"    : student.to_dict(),
        "check"      : check.to_dict(),
    })


@app.route("/api/cards/confirm", methods=["POST"])
def api_confirm_card():
    """
    Phase 2 of card assignment: write the assignment to the database.

    Must only be called after /api/cards/assign returned a scanned UID
    and the teacher has reviewed the check result.

    Body (JSON):
        student_id  int  required
        uid         str  required  (the UID returned by /api/cards/assign)
    """
    body       = request.get_json(silent=True) or {}
    student_id = body.get("student_id")
    uid        = (body.get("uid") or "").strip()

    if not student_id:
        return _err("student_id is required", 400)
    if not uid:
        return _err("uid is required", 400)

    result = confirm_assignment(uid, student_id)

    if not result.success():
        return _err(result.message, 400)

    student = get_student(student_id)
    return _ok({
        "outcome"             : result.outcome.value,
        "uid"                 : result.uid,
        "student"             : student.to_dict() if student else None,
        "previous_owner_id"   : result.previous_owner_id,
        "previous_owner_name" : result.previous_owner_name,
        "message"             : result.message,
    })


@app.route("/api/cards/cancel", methods=["POST"])
def api_cancel_card():
    """Cancel the current card assignment workflow. No DB changes."""
    result = cancel_assignment()
    return _ok(result)


@app.route("/api/cards/unassign", methods=["POST"])
def api_unassign_card():
    """
    Remove the RFID card from a student (sets rfid_uid = NULL).

    Body (JSON):
        student_id  int  required
    """
    body       = request.get_json(silent=True) or {}
    student_id = body.get("student_id")

    if not student_id:
        return _err("student_id is required", 400)

    from rfid_service import unassign_card, AssignmentOutcome
    result = unassign_card(student_id)

    if result.outcome == AssignmentOutcome.STUDENT_NOT_FOUND:
        return _err(result.message, 404)
    if not result.success():
        return _err(result.message, 400)

    student = get_student(student_id)
    return _ok({
        "outcome": result.outcome.value,
        "student": student.to_dict() if student else None,
        "message": result.message,
    })


# ---------------------------------------------------------------------------
# Classes — full CRUD
# ---------------------------------------------------------------------------

@app.route("/api/classes", methods=["GET", "POST"])
def api_classes():
    """
    GET  — list all classes ordered by name.
    POST — create a class.  Body: {name (required), description (optional)}
    """
    if request.method == "GET":
        from database import get_connection
        conn = get_connection()
        try:
            cur = conn.cursor()
            cur.execute("SELECT id, name, description FROM classes ORDER BY name")
            rows = cur.fetchall()
        finally:
            conn.close()
        return _ok({"classes": [dict(r) for r in rows]})

    # POST
    from database import get_connection
    import sqlite3 as _sqlite3
    body = request.get_json(silent=True) or {}
    name = (body.get("name") or "").strip()
    if not name:
        return _err("name is required", 400)
    description = (body.get("description") or "").strip() or None

    conn = get_connection()
    try:
        with conn:
            conn.execute(
                "INSERT INTO classes (name, description) VALUES (?, ?)",
                (name, description)
            )
            new_id = conn.execute(
                "SELECT id FROM classes WHERE name = ?", (name,)
            ).fetchone()["id"]
    except _sqlite3.IntegrityError:
        conn.close()
        return _err(f"A class named '{name}' already exists.", 409)
    finally:
        conn.close()

    conn2 = get_connection()
    try:
        row = conn2.execute(
            "SELECT id, name, description FROM classes WHERE id = ?", (new_id,)
        ).fetchone()
        cls = dict(row)
    finally:
        conn2.close()

    return _ok({"class": cls, "message": f"Class '{name}' created."}, 201)


@app.route("/api/classes/<int:class_id>", methods=["GET"])
def api_get_class(class_id: int):
    """Return one class by id."""
    from database import get_connection
    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT id, name, description FROM classes WHERE id = ?", (class_id,)
        ).fetchone()
    finally:
        conn.close()
    if row is None:
        return _err(f"Class {class_id} not found.", 404)
    return _ok({"class": dict(row)})


@app.route("/api/classes/<int:class_id>", methods=["PATCH"])
def api_update_class(class_id: int):
    """
    Update a class.

    Body (JSON): any subset of name, description.
    """
    from database import get_connection
    import sqlite3 as _sqlite3
    body = request.get_json(silent=True) or {}

    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT id FROM classes WHERE id = ?", (class_id,)
        ).fetchone()
        if row is None:
            return _err(f"Class {class_id} not found.", 404)

        updates = {}
        if "name" in body and body["name"] is not None:
            updates["name"] = body["name"].strip()
        if "description" in body:
            updates["description"] = (body["description"] or "").strip() or None

        if not updates:
            row2 = conn.execute(
                "SELECT id, name, description FROM classes WHERE id = ?", (class_id,)
            ).fetchone()
            return _ok({"class": dict(row2), "message": "No fields changed."})

        set_clause = ", ".join(f"{k} = ?" for k in updates)
        values = list(updates.values()) + [class_id]
        try:
            with conn:
                conn.execute(
                    f"UPDATE classes SET {set_clause} WHERE id = ?", values
                )
        except _sqlite3.IntegrityError as exc:
            return _err(f"Update failed: {exc}", 409)

        row3 = conn.execute(
            "SELECT id, name, description FROM classes WHERE id = ?", (class_id,)
        ).fetchone()
    finally:
        conn.close()

    return _ok({"class": dict(row3), "message": f"Class {class_id} updated."})


@app.route("/api/classes/<int:class_id>", methods=["DELETE"])
def api_delete_class(class_id: int):
    """
    Delete a class.

    Refused if any students or attendance_sessions reference this class,
    because cascading those deletions would silently destroy data.
    """
    from database import get_connection
    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT id, name FROM classes WHERE id = ?", (class_id,)
        ).fetchone()
        if row is None:
            return _err(f"Class {class_id} not found.", 404)

        # Check for students in this class.
        student_count = conn.execute(
            "SELECT COUNT(*) FROM students WHERE class_id = ?", (class_id,)
        ).fetchone()[0]
        if student_count > 0:
            return _err(
                f"Cannot delete class '{row['name']}': "
                f"{student_count} student(s) are assigned to it. "
                f"Reassign or remove them first.",
                409
            )

        # Check for sessions referencing this class.
        session_count = conn.execute(
            "SELECT COUNT(*) FROM attendance_sessions WHERE class_id = ?",
            (class_id,)
        ).fetchone()[0]
        if session_count > 0:
            return _err(
                f"Cannot delete class '{row['name']}': "
                f"{session_count} attendance session(s) reference it. "
                f"Delete those sessions first.",
                409
            )

        with conn:
            conn.execute("DELETE FROM classes WHERE id = ?", (class_id,))
    finally:
        conn.close()

    return _ok({"message": f"Class '{row['name']}' (id={class_id}) deleted."})


# ---------------------------------------------------------------------------
# Subjects — full CRUD
# ---------------------------------------------------------------------------

@app.route("/api/subjects", methods=["GET", "POST"])
def api_subjects():
    """
    GET  — list all subjects ordered by name.
    POST — create a subject.  Body: {name (required), code (optional)}
    """
    if request.method == "GET":
        from database import get_connection
        conn = get_connection()
        try:
            cur = conn.cursor()
            cur.execute("SELECT id, name, code FROM subjects ORDER BY name")
            rows = cur.fetchall()
        finally:
            conn.close()
        return _ok({"subjects": [dict(r) for r in rows]})

    # POST
    from database import get_connection
    import sqlite3 as _sqlite3
    body = request.get_json(silent=True) or {}
    name = (body.get("name") or "").strip()
    if not name:
        return _err("name is required", 400)
    code = (body.get("code") or "").strip() or None

    conn = get_connection()
    try:
        with conn:
            conn.execute(
                "INSERT INTO subjects (name, code) VALUES (?, ?)", (name, code)
            )
            new_id = conn.execute(
                "SELECT id FROM subjects WHERE name = ?", (name,)
            ).fetchone()["id"]
    except _sqlite3.IntegrityError:
        conn.close()
        return _err(f"A subject named '{name}' already exists.", 409)
    finally:
        conn.close()

    conn2 = get_connection()
    try:
        row = conn2.execute(
            "SELECT id, name, code FROM subjects WHERE id = ?", (new_id,)
        ).fetchone()
        subj = dict(row)
    finally:
        conn2.close()

    return _ok({"subject": subj, "message": f"Subject '{name}' created."}, 201)


@app.route("/api/subjects/<int:subject_id>", methods=["GET"])
def api_get_subject(subject_id: int):
    """Return one subject by id."""
    from database import get_connection
    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT id, name, code FROM subjects WHERE id = ?", (subject_id,)
        ).fetchone()
    finally:
        conn.close()
    if row is None:
        return _err(f"Subject {subject_id} not found.", 404)
    return _ok({"subject": dict(row)})


@app.route("/api/subjects/<int:subject_id>", methods=["PATCH"])
def api_update_subject(subject_id: int):
    """
    Update a subject.

    Body (JSON): any subset of name, code.
    """
    from database import get_connection
    import sqlite3 as _sqlite3
    body = request.get_json(silent=True) or {}

    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT id FROM subjects WHERE id = ?", (subject_id,)
        ).fetchone()
        if row is None:
            return _err(f"Subject {subject_id} not found.", 404)

        updates = {}
        if "name" in body and body["name"] is not None:
            updates["name"] = body["name"].strip()
        if "code" in body:
            updates["code"] = (body["code"] or "").strip() or None

        if not updates:
            row2 = conn.execute(
                "SELECT id, name, code FROM subjects WHERE id = ?", (subject_id,)
            ).fetchone()
            return _ok({"subject": dict(row2), "message": "No fields changed."})

        set_clause = ", ".join(f"{k} = ?" for k in updates)
        values = list(updates.values()) + [subject_id]
        try:
            with conn:
                conn.execute(
                    f"UPDATE subjects SET {set_clause} WHERE id = ?", values
                )
        except _sqlite3.IntegrityError as exc:
            return _err(f"Update failed: {exc}", 409)

        row3 = conn.execute(
            "SELECT id, name, code FROM subjects WHERE id = ?", (subject_id,)
        ).fetchone()
    finally:
        conn.close()

    return _ok({"subject": dict(row3), "message": f"Subject {subject_id} updated."})


@app.route("/api/subjects/<int:subject_id>", methods=["DELETE"])
def api_delete_subject(subject_id: int):
    """
    Delete a subject.

    Refused if any attendance_sessions reference this subject.
    """
    from database import get_connection
    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT id, name FROM subjects WHERE id = ?", (subject_id,)
        ).fetchone()
        if row is None:
            return _err(f"Subject {subject_id} not found.", 404)

        session_count = conn.execute(
            "SELECT COUNT(*) FROM attendance_sessions WHERE subject_id = ?",
            (subject_id,)
        ).fetchone()[0]
        if session_count > 0:
            return _err(
                f"Cannot delete subject '{row['name']}': "
                f"{session_count} session(s) reference it.",
                409
            )

        with conn:
            conn.execute("DELETE FROM subjects WHERE id = ?", (subject_id,))
    finally:
        conn.close()

    return _ok({"message": f"Subject '{row['name']}' (id={subject_id}) deleted."})


# ---------------------------------------------------------------------------
# Teachers — full CRUD
# ---------------------------------------------------------------------------

@app.route("/api/teachers", methods=["GET", "POST"])
def api_teachers():
    """
    GET  — list all active teachers ordered by name.
    POST — create a teacher.  Body: {name (required), username (required)}
    """
    if request.method == "GET":
        from database import get_connection
        conn = get_connection()
        try:
            cur = conn.cursor()
            cur.execute(
                "SELECT id, name, username, status FROM teachers "
                "WHERE status = 'active' ORDER BY name"
            )
            rows = cur.fetchall()
        finally:
            conn.close()
        return _ok({"teachers": [dict(r) for r in rows]})

    # POST
    from database import get_connection
    import sqlite3 as _sqlite3
    body = request.get_json(silent=True) or {}
    name     = (body.get("name")     or "").strip()
    username = (body.get("username") or "").strip()

    if not name:
        return _err("name is required", 400)
    if not username:
        return _err("username is required", 400)

    conn = get_connection()
    try:
        ts = _now_iso()
        with conn:
            conn.execute(
                "INSERT INTO teachers (name, username, password_hash, status, created_at) "
                "VALUES (?, ?, NULL, 'active', ?)",
                (name, username, ts)
            )
            new_id = conn.execute(
                "SELECT id FROM teachers WHERE username = ?", (username,)
            ).fetchone()["id"]
    except _sqlite3.IntegrityError:
        conn.close()
        return _err(f"Username '{username}' is already taken.", 409)
    finally:
        conn.close()

    conn2 = get_connection()
    try:
        row = conn2.execute(
            "SELECT id, name, username, status, created_at FROM teachers WHERE id = ?",
            (new_id,)
        ).fetchone()
        teacher = dict(row)
    finally:
        conn2.close()

    return _ok({"teacher": teacher, "message": f"Teacher '{name}' created."}, 201)


@app.route("/api/teachers/<int:teacher_id>", methods=["GET"])
def api_get_teacher(teacher_id: int):
    """Return one teacher by id."""
    from database import get_connection
    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT id, name, username, status, created_at FROM teachers WHERE id = ?",
            (teacher_id,)
        ).fetchone()
    finally:
        conn.close()
    if row is None:
        return _err(f"Teacher {teacher_id} not found.", 404)
    return _ok({"teacher": dict(row)})


@app.route("/api/teachers/<int:teacher_id>", methods=["PATCH"])
def api_update_teacher(teacher_id: int):
    """
    Update a teacher.

    Body (JSON): any subset of name, username, status.
    password_hash is NOT updatable here (no authentication yet).
    """
    from database import get_connection
    import sqlite3 as _sqlite3
    body = request.get_json(silent=True) or {}

    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT id FROM teachers WHERE id = ?", (teacher_id,)
        ).fetchone()
        if row is None:
            return _err(f"Teacher {teacher_id} not found.", 404)

        updates = {}
        if "name"     in body and body["name"]     is not None:
            updates["name"]     = body["name"].strip()
        if "username" in body and body["username"] is not None:
            updates["username"] = body["username"].strip()
        if "status"   in body and body["status"]   is not None:
            updates["status"]   = body["status"]

        if not updates:
            row2 = conn.execute(
                "SELECT id, name, username, status, created_at FROM teachers WHERE id = ?",
                (teacher_id,)
            ).fetchone()
            return _ok({"teacher": dict(row2), "message": "No fields changed."})

        set_clause = ", ".join(f"{k} = ?" for k in updates)
        values = list(updates.values()) + [teacher_id]
        try:
            with conn:
                conn.execute(
                    f"UPDATE teachers SET {set_clause} WHERE id = ?", values
                )
        except _sqlite3.IntegrityError as exc:
            return _err(f"Update failed: {exc}", 409)

        row3 = conn.execute(
            "SELECT id, name, username, status, created_at FROM teachers WHERE id = ?",
            (teacher_id,)
        ).fetchone()
    finally:
        conn.close()

    return _ok({"teacher": dict(row3), "message": f"Teacher {teacher_id} updated."})


@app.route("/api/teachers/<int:teacher_id>", methods=["DELETE"])
def api_delete_teacher(teacher_id: int):
    """
    Delete a teacher.

    If the teacher is referenced by sessions, the sessions are not deleted —
    the teacher_id on those sessions is set to NULL instead, preserving the
    historical session records.  The teacher row is then deleted.

    This follows the prototype's existing design: teacher_id on sessions is
    nullable, so detaching is the safe option rather than blocking deletion
    or cascading.
    """
    from database import get_connection
    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT id, name FROM teachers WHERE id = ?", (teacher_id,)
        ).fetchone()
        if row is None:
            return _err(f"Teacher {teacher_id} not found.", 404)

        with conn:
            # Detach from sessions (teacher_id is nullable).
            conn.execute(
                "UPDATE attendance_sessions SET teacher_id = NULL WHERE teacher_id = ?",
                (teacher_id,)
            )
            conn.execute("DELETE FROM teachers WHERE id = ?", (teacher_id,))
    finally:
        conn.close()

    return _ok({"message": f"Teacher '{row['name']}' (id={teacher_id}) deleted."})


# ---------------------------------------------------------------------------
# Students — DELETE
# ---------------------------------------------------------------------------

@app.route("/api/students/<int:student_id>", methods=["DELETE"])
def api_delete_student(student_id: int):
    """
    Delete a student.

    Refused if the student has attendance records — deleting them would
    silently destroy historical data.  The caller must acknowledge this
    explicitly (no auto-cascade).
    """
    from database import get_connection
    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT id, first_name, last_name FROM students WHERE id = ?",
            (student_id,)
        ).fetchone()
        if row is None:
            return _err(f"Student {student_id} not found.", 404)

        attendance_count = conn.execute(
            "SELECT COUNT(*) FROM attendance WHERE student_id = ?",
            (student_id,)
        ).fetchone()[0]
        if attendance_count > 0:
            return _err(
                f"Cannot delete student '{row['first_name']} {row['last_name']}': "
                f"{attendance_count} attendance record(s) exist. "
                f"Historical attendance cannot be automatically removed.",
                409
            )

        with conn:
            conn.execute("DELETE FROM students WHERE id = ?", (student_id,))
    finally:
        conn.close()

    return _ok({
        "message": (
            f"Student '{row['first_name']} {row['last_name']}' "
            f"(id={student_id}) deleted."
        )
    })


# ---------------------------------------------------------------------------
# Sessions — PATCH and DELETE
# ---------------------------------------------------------------------------

@app.route("/api/sessions/<int:session_id>", methods=["PATCH"])
def api_update_session(session_id: int):
    """
    Update editable session fields.

    Allowed fields: session_date, start_time, end_time, teacher_id, subject_id.
    class_id may also be updated, but ONLY if the session is still SCHEDULED
    (no attendance can have been taken yet).

    Protected fields (status, opened_at, closed_at, completed_at) are never
    updated here — use the /open, /close, /complete endpoints instead.

    COMPLETED sessions cannot be edited.
    """
    from database import get_connection
    import sqlite3 as _sqlite3
    body = request.get_json(silent=True) or {}

    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT id, status FROM attendance_sessions WHERE id = ?",
            (session_id,)
        ).fetchone()
        if row is None:
            return _err(f"Session {session_id} not found.", 404)

        if row["status"] == "COMPLETED":
            return _err(
                f"Session {session_id} is COMPLETED and cannot be edited.",
                400
            )

        updates = {}

        if "session_date" in body and body["session_date"] is not None:
            updates["session_date"] = body["session_date"]
        if "start_time" in body:
            updates["start_time"] = body["start_time"] or None
        if "end_time" in body:
            updates["end_time"] = body["end_time"] or None
        if "teacher_id" in body:
            updates["teacher_id"] = body["teacher_id"]
        if "subject_id" in body:
            updates["subject_id"] = body["subject_id"]

        # class_id only allowed when SCHEDULED (no attendance exists yet).
        if "class_id" in body:
            if row["status"] != "SCHEDULED":
                return _err(
                    "class_id can only be changed while the session is SCHEDULED.",
                    400
                )
            updates["class_id"] = body["class_id"]

        # Silently ignore any attempt to set protected timestamp/status fields.
        for protected in ("status", "opened_at", "closed_at", "completed_at",
                          "created_at"):
            updates.pop(protected, None)

        if not updates:
            from session_service import get_session
            session = get_session(session_id)
            return _ok({
                "session": session.to_dict(),
                "message": "No editable fields provided."
            })

        set_clause = ", ".join(f"{k} = ?" for k in updates)
        values = list(updates.values()) + [session_id]
        try:
            with conn:
                conn.execute(
                    f"UPDATE attendance_sessions SET {set_clause} WHERE id = ?",
                    values
                )
        except _sqlite3.IntegrityError as exc:
            return _err(f"Update failed: {exc}", 400)

    finally:
        conn.close()

    from session_service import get_session
    session = get_session(session_id)
    return _ok({"session": session.to_dict(), "message": f"Session {session_id} updated."})


@app.route("/api/sessions/<int:session_id>", methods=["DELETE"])
def api_delete_session(session_id: int):
    """
    Delete a session.

    Rules:
      - COMPLETED sessions cannot be deleted (they are historical records).
      - Sessions with any attendance records cannot be deleted.
      - SCHEDULED, OPEN, or CLOSED sessions with no attendance can be deleted.
    """
    from database import get_connection
    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT id, status, session_date FROM attendance_sessions WHERE id = ?",
            (session_id,)
        ).fetchone()
        if row is None:
            return _err(f"Session {session_id} not found.", 404)

        if row["status"] == "COMPLETED":
            return _err(
                f"Session {session_id} is COMPLETED and cannot be deleted. "
                f"Completed sessions are permanent historical records.",
                409
            )

        attendance_count = conn.execute(
            "SELECT COUNT(*) FROM attendance WHERE session_id = ?",
            (session_id,)
        ).fetchone()[0]
        if attendance_count > 0:
            return _err(
                f"Cannot delete session {session_id}: "
                f"{attendance_count} attendance record(s) exist. "
                f"Attendance records cannot be automatically removed.",
                409
            )

        with conn:
            conn.execute(
                "DELETE FROM attendance_sessions WHERE id = ?", (session_id,)
            )
    finally:
        conn.close()

    return _ok({
        "message": (
            f"Session {session_id} ({row['session_date']}) deleted."
        )
    })


# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------

@app.route("/api/health", methods=["GET"])
def api_health():
    """Simple health check."""
    return _ok({
        "status" : "ok",
        "reader" : rfid_state.status(),
        "time"   : _now_iso(),
    })


# ---------------------------------------------------------------------------
# Error handlers
# ---------------------------------------------------------------------------

@app.errorhandler(404)
def not_found(e):
    return _err("Not found", 404)


@app.errorhandler(405)
def method_not_allowed(e):
    return _err("Method not allowed", 405)


@app.errorhandler(500)
def internal_error(e):
    return _err(f"Internal server error: {e}", 500)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    rfid_port  = os.environ.get("RFID_PORT",  "COM7")
    flask_host = os.environ.get("FLASK_HOST", "127.0.0.1")
    flask_port = int(os.environ.get("FLASK_PORT", "5000"))
    debug      = os.environ.get("FLASK_DEBUG", "0") == "1"

    # Initialise DB schema (safe to call repeatedly — IF NOT EXISTS).
    initialize_database()

    # Start RFID listener before Flask begins serving.
    start_rfid_listener(rfid_port)

    print(f"\nRFID Attendance Portal")
    print(f"  URL  : http://{flask_host}:{flask_port}")
    print(f"  RFID : {rfid_port}  ({'ONLINE' if rfid_state.reader_online else 'OFFLINE'})")
    print(f"  Debug: {debug}\n")

    # use_reloader=False prevents the listener thread being started twice
    # by Werkzeug's reloader process.
    app.run(
        host=flask_host,
        port=flask_port,
        debug=debug,
        use_reloader=False,
    )
