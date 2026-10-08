"""
reset_database.py — Wipe all data from the development database.

Targets:        attendance.db  (in the current working directory)
Tables cleared: attendance, attendance_sessions, students,
                teachers, subjects, classes
                (in FK-safe order — children before parents)
Schema:         NOT changed.  Tables are emptied, not dropped.
Seed data:      NONE inserted.  The database is left completely empty.
Autoincrement:  sqlite_sequence is cleared so IDs restart from 1.
Backup:         A timestamped copy is made before any deletion.

This script does NOT affect:
    - Test databases (tests use in-memory SQLite, not attendance.db)
    - The schema itself (tables remain, just empty)
    - The attendance.csv archive file

When to run:
    Once, before entering real school data for the first time.
    After this, add classes / subjects / teachers directly via SQL
    (or wait for the admin API endpoints to be implemented).

Run with:
    python reset_database.py
"""

import sqlite3
import os
import shutil
from datetime import datetime


DB = "attendance.db"


def confirm():
    print("\n  WARNING: This will permanently delete ALL data in attendance.db.")
    print("  A timestamped backup will be created first.")
    print()
    print("  Type 'yes' to continue: ", end="", flush=True)
    try:
        ans = input().strip().lower()
        return ans == "yes"
    except (KeyboardInterrupt, EOFError):
        print()
        return False


def reset():
    # ---------------------------------------------------------------------------
    # Backup
    # ---------------------------------------------------------------------------
    ts_str = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup = f"attendance.db.reset_backup_{ts_str}"
    shutil.copy2(DB, backup)
    print(f"\n  Backup created: {backup}")

    # ---------------------------------------------------------------------------
    # Delete all data in FK-safe order (children before parents)
    # ---------------------------------------------------------------------------
    conn = sqlite3.connect(DB)
    conn.execute("PRAGMA foreign_keys = OFF")

    deletion_order = [
        "attendance",           # references attendance_sessions, students
        "attendance_sessions",  # references classes, teachers, subjects
        "students",             # references classes
        "teachers",
        "subjects",
        "classes",
    ]

    for table in deletion_order:
        conn.execute(f"DELETE FROM {table}")
        print(f"  Cleared: {table}")

    # Reset autoincrement counters so IDs restart from 1.
    conn.execute("DELETE FROM sqlite_sequence")

    conn.execute("PRAGMA foreign_keys = ON")
    conn.commit()
    conn.execute("VACUUM")
    conn.close()

    # ---------------------------------------------------------------------------
    # Summary
    # ---------------------------------------------------------------------------
    print("\n  Database reset complete.")
    print("  All 6 tables are empty. IDs will start from 1.")
    print()
    print("  The schema is intact — no tables were dropped.")
    print("  No seed data was inserted.")
    print()
    print("  Next: add real school data before starting the application.")
    print("  Until admin API endpoints exist, insert directly via SQL:")
    print()
    print("    INSERT INTO classes  (name) VALUES ('...');")
    print("    INSERT INTO subjects (name, code) VALUES ('...', '...');")
    print("    INSERT INTO teachers (name, username, status, created_at)")
    print("                 VALUES ('...', '...', 'active', datetime('now'));")
    print()
    print("  Then add students and assign RFID cards through the API.")


if __name__ == "__main__":
    if not os.path.exists(DB):
        print(f"  Database not found: {DB}")
        print(f"  Run from the project root directory.")
        raise SystemExit(1)

    if confirm():
        reset()
    else:
        print("  Reset cancelled. No changes made.")
