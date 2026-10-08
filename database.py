"""
database.py — Database schema definition and connection management.

Prototype schema — 6 tables:

    classes
    students          (rfid_uid stored directly here, nullable)
    teachers
    subjects
    attendance_sessions
    attendance

Nothing runs on import. All initialization is explicit.
Call initialize_database() at application startup.
"""

import sqlite3
import os

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

DATABASE = "attendance.db"

# Valid session states.
SESSION_STATES = ("SCHEDULED", "OPEN", "CLOSED", "COMPLETED")


# ---------------------------------------------------------------------------
# Connection factory
# ---------------------------------------------------------------------------

def get_connection():
    """
    Return a new SQLite connection with:
      - foreign-key enforcement ON
      - row_factory = sqlite3.Row (columns accessible by name)

    The caller is responsible for closing the connection.
    Use as a context manager for automatic commit/rollback:

        with get_connection() as conn:
            conn.execute(...)
        conn.close()
    """
    conn = sqlite3.connect(DATABASE)
    conn.execute("PRAGMA foreign_keys = ON")
    conn.row_factory = sqlite3.Row
    return conn


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

def create_tables(conn):
    """
    Create all tables if they do not already exist.

    Accepts an open connection. Does not commit — the caller controls
    the transaction.

    Safe to call on an existing correct database (IF NOT EXISTS).
    """
    cur = conn.cursor()

    # ------------------------------------------------------------------
    # classes
    # ------------------------------------------------------------------
    # A school class or group, e.g. "Year 1B".
    # Class names are unique across the whole database.
    cur.execute("""
        CREATE TABLE IF NOT EXISTS classes (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            name        TEXT    NOT NULL UNIQUE,
            description TEXT
        )
    """)

    # ------------------------------------------------------------------
    # students
    # ------------------------------------------------------------------
    # Core student record.
    #
    # rfid_uid:
    #   - Nullable  → a student can exist before a card is assigned.
    #   - UNIQUE    → one UID cannot belong to two students.
    #   - Stored normalized (uppercase, no spaces), e.g. "A96E9504".
    #
    # class_id:
    #   - Nullable  → a student can exist before being assigned a class.
    #   - FK → classes.id
    #
    # registration_number:
    #   - Unique human-readable school ID.
    cur.execute("""
        CREATE TABLE IF NOT EXISTS students (
            id                  INTEGER PRIMARY KEY AUTOINCREMENT,
            first_name          TEXT    NOT NULL,
            last_name           TEXT    NOT NULL,
            registration_number TEXT    NOT NULL UNIQUE,
            rfid_uid            TEXT    UNIQUE,
            class_id            INTEGER REFERENCES classes(id),
            status              TEXT    NOT NULL DEFAULT 'active'
                                    CHECK (status IN ('active', 'inactive')),
            created_at          TEXT    NOT NULL
        )
    """)

    # ------------------------------------------------------------------
    # teachers
    # ------------------------------------------------------------------
    # Teacher identity. password_hash left nullable until auth is built.
    cur.execute("""
        CREATE TABLE IF NOT EXISTS teachers (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            name          TEXT    NOT NULL,
            username      TEXT    NOT NULL UNIQUE,
            password_hash TEXT,
            status        TEXT    NOT NULL DEFAULT 'active'
                              CHECK (status IN ('active', 'inactive')),
            created_at    TEXT    NOT NULL
        )
    """)

    # ------------------------------------------------------------------
    # subjects
    # ------------------------------------------------------------------
    # A school subject, e.g. "Mathematics". Reused across sessions.
    cur.execute("""
        CREATE TABLE IF NOT EXISTS subjects (
            id   INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT    NOT NULL UNIQUE,
            code TEXT
        )
    """)

    # ------------------------------------------------------------------
    # attendance_sessions
    # ------------------------------------------------------------------
    # One session = one teacher taking attendance for one class/subject
    # on one date.
    #
    # State machine:  SCHEDULED → OPEN → CLOSED → COMPLETED
    #                               ↑_______↓  (reopen allowed)
    #   COMPLETED is permanent — no transitions out of COMPLETED.
    #
    # Only OPEN sessions accept attendance scans.
    # State transitions are enforced in application logic (SQLite CHECK
    # cannot compare old vs new row values).
    cur.execute("""
        CREATE TABLE IF NOT EXISTS attendance_sessions (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            class_id     INTEGER NOT NULL REFERENCES classes(id),
            teacher_id   INTEGER REFERENCES teachers(id),
            subject_id   INTEGER REFERENCES subjects(id),
            session_date TEXT    NOT NULL,
            start_time   TEXT,
            end_time     TEXT,
            status       TEXT    NOT NULL DEFAULT 'SCHEDULED'
                             CHECK (status IN
                                 ('SCHEDULED', 'OPEN', 'CLOSED', 'COMPLETED')),
            opened_at    TEXT,
            closed_at    TEXT,
            completed_at TEXT,
            created_at   TEXT    NOT NULL
        )
    """)

    # ------------------------------------------------------------------
    # attendance
    # ------------------------------------------------------------------
    # One row = one student present in one session.
    #
    # CRITICAL: UNIQUE(session_id, student_id)
    #   The database enforces this — no student can appear twice in the
    #   same session regardless of application-layer checks.
    #
    # status: "present" is the only value needed for now. Reserved for
    #   future use (e.g. "late", "excused").
    cur.execute("""
        CREATE TABLE IF NOT EXISTS attendance (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id      INTEGER NOT NULL REFERENCES attendance_sessions(id),
            student_id      INTEGER NOT NULL REFERENCES students(id),
            attendance_time TEXT    NOT NULL,
            status          TEXT    NOT NULL DEFAULT 'present',
            created_at      TEXT    NOT NULL,
            UNIQUE (session_id, student_id)
        )
    """)


# ---------------------------------------------------------------------------
# Public initializer
# ---------------------------------------------------------------------------

def initialize_database():
    """
    Create all tables in attendance.db if they do not already exist.

    Safe to call multiple times — IF NOT EXISTS prevents data loss.
    Call explicitly at application startup; do NOT rely on import side effects.
    """
    conn = get_connection()
    try:
        with conn:
            create_tables(conn)
        print(f"Database initialized: {os.path.abspath(DATABASE)}")
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    initialize_database()
    print("Schema creation complete.")
