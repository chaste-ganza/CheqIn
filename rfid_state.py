"""
rfid_state.py — Thread-safe shared state between the RFID listener thread
                and Flask route handlers.

The RFID listener runs in a daemon thread and produces scan results.
Flask routes run in the main thread (or Werkzeug worker threads).
Direct access between them without synchronisation would cause race
conditions.

This module provides a single module-level RFIDState instance that both
sides can safely read and write using a threading.Lock.

Architecture:

    RFIDListener (daemon thread)
          │
          ▼
    rfid_worker thread
    calls process_scan(uid) → ScanResult
    calls rfid_state.set_scan_result(result)
          │
          ▼
    RFIDState  <──────  Flask GET /api/scan-result
    (protected
     by Lock)  <──────  Flask POST /api/cards/assign
                             starts assign thread
                             sets pending_assignment
"""

import threading
from datetime import datetime, timezone
from typing import Optional


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")


class RFIDState:
    """
    Thread-safe container for the current RFID system state.

    Fields:
        reader_online       True if the serial listener is running.
        reader_port         The configured serial port string.
        last_scan           Dict of the most recent ScanResult, or None.
        last_scan_at        ISO timestamp of the last scan.
        pending_assignment  Dict with uid + timestamp when the reader just
                            scanned a card in ASSIGN mode, awaiting teacher
                            confirmation.  None when not in assign mode.
        assign_event        threading.Event set when a pending_assignment
                            arrives.  Used to block the assign route while
                            waiting for the hardware scan.
    """

    def __init__(self):
        self._lock              = threading.Lock()
        self._reader_online     = False
        self._reader_port       = "COM7"
        self._last_scan         : Optional[dict] = None
        self._last_scan_at      : Optional[str]  = None
        self._pending_assignment: Optional[dict] = None
        # Event set by the listener worker when an ASSIGN_SCANNED event
        # arrives. The /api/cards/assign route waits on this.
        self.assign_event       = threading.Event()

    # ------------------------------------------------------------------
    # Reader status
    # ------------------------------------------------------------------

    def set_reader_online(self, online: bool, port: str = "COM7"):
        with self._lock:
            self._reader_online = online
            self._reader_port   = port

    def set_reader_offline(self):
        with self._lock:
            self._reader_online = False

    @property
    def reader_online(self) -> bool:
        with self._lock:
            return self._reader_online

    @property
    def reader_port(self) -> str:
        with self._lock:
            return self._reader_port

    # ------------------------------------------------------------------
    # Scan results
    # ------------------------------------------------------------------

    def set_scan_result(self, result: dict):
        """Store the latest scan result. Called by the listener worker."""
        with self._lock:
            self._last_scan    = result
            self._last_scan_at = _now_iso()

    def get_scan_result(self) -> dict:
        """Return the latest scan result dict, or an idle placeholder."""
        with self._lock:
            if self._last_scan is None:
                return {
                    "outcome"    : "IDLE",
                    "message"    : "Waiting for card scan...",
                    "last_scan_at": None,
                }
            return {**self._last_scan, "last_scan_at": self._last_scan_at}

    def clear_scan_result(self):
        """Reset the scan result (e.g. after the frontend has consumed it)."""
        with self._lock:
            self._last_scan    = None
            self._last_scan_at = None

    # ------------------------------------------------------------------
    # Assignment mode
    # ------------------------------------------------------------------

    def set_pending_assignment(self, uid: str):
        """
        Called by the listener worker when an ASSIGN_SCANNED event arrives.
        Stores the UID and signals the assign route that a card is ready.
        """
        with self._lock:
            self._pending_assignment = {
                "uid"        : uid,
                "scanned_at" : _now_iso(),
            }
        self.assign_event.set()

    def pop_pending_assignment(self) -> Optional[dict]:
        """
        Return and clear the pending assignment.
        Returns None if no assignment is pending.
        """
        with self._lock:
            result = self._pending_assignment
            self._pending_assignment = None
        self.assign_event.clear()
        return result

    def get_pending_assignment(self) -> Optional[dict]:
        """Return the pending assignment without clearing it."""
        with self._lock:
            return self._pending_assignment

    # ------------------------------------------------------------------
    # Status snapshot (for dashboard / health endpoint)
    # ------------------------------------------------------------------

    def status(self) -> dict:
        with self._lock:
            return {
                "reader_online"      : self._reader_online,
                "reader_port"        : self._reader_port,
                "last_scan_at"       : self._last_scan_at,
                "has_pending_assign" : self._pending_assignment is not None,
            }


# ---------------------------------------------------------------------------
# Module-level singleton — import this in app.py and the listener worker
# ---------------------------------------------------------------------------

rfid_state = RFIDState()
