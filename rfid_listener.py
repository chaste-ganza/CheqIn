"""
rfid_listener.py — Serial listener for the ESP8266 + MFRC522 RFID reader.

Prototype: one physical reader connected via USB serial.

Usage:
    from rfid_listener import RFIDListener, RFIDEvent, EventKind, ListenerMode

    listener = RFIDListener(port="COM7")
    listener.start()

    for event in listener.events():
        if event.kind == EventKind.CARD_SCANNED:
            # normal attendance scan
            handle_attendance(event.uid)
        elif event.kind == EventKind.ASSIGN_SCANNED:
            # card management scan
            handle_assignment(event.uid)

    listener.stop()

Commands supported by the ESP8266 firmware:
    ON      enable scanning (sent automatically on start())
    OFF     disable scanning (sent automatically on stop())
    STATUS  report current reader state
    ASSIGN  read exactly one card for assignment, then return to normal

The listener tracks two modes:
    NORMAL  every card scan produces a CARD_SCANNED event
    ASSIGN  the next card scan produces an ASSIGN_SCANNED event,
            then the reader reverts to NORMAL automatically

The listener does NOT contain any business logic.
It answers only: "Which card was scanned, and when?"
"""

import serial
import threading
import queue
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Optional


# ---------------------------------------------------------------------------
# Configuration defaults
# ---------------------------------------------------------------------------

DEFAULT_PORT         = "COM7"
BAUD_RATE            = 115200
STARTUP_DELAY        = 2.0    # seconds after opening port (ESP8266 resets on connect)
ASSIGN_TIMEOUT       = 30.0   # seconds to wait for a card in ASSIGN mode
READLINE_TIMEOUT     = 1.0    # serial readline timeout in seconds


# ---------------------------------------------------------------------------
# Enumerations
# ---------------------------------------------------------------------------

class ListenerMode(Enum):
    """Current operating mode of the listener."""
    NORMAL = "NORMAL"   # ordinary attendance scanning
    ASSIGN = "ASSIGN"   # waiting for one card for card management


class EventKind(Enum):
    """Every event type the listener can produce."""
    CARD_SCANNED   = "CARD_SCANNED"   # card read in NORMAL mode
    ASSIGN_SCANNED = "ASSIGN_SCANNED" # card read in ASSIGN mode
    ASSIGN_TIMEOUT = "ASSIGN_TIMEOUT" # no card arrived before ASSIGN_TIMEOUT
    SYSTEM_ON      = "SYSTEM_ON"      # ESP8266 confirmed ON
    SYSTEM_OFF     = "SYSTEM_OFF"     # ESP8266 confirmed OFF
    STATUS         = "STATUS"         # response to STATUS command
    ASSIGN_WAITING = "ASSIGN_WAITING" # ESP8266 entered ASSIGN mode
    ASSIGN_DONE    = "ASSIGN_DONE"    # ESP8266 completed ASSIGN mode
    ERROR          = "ERROR"          # parse error or firmware error line
    BOOT           = "BOOT"           # ESP8266 startup message


# ---------------------------------------------------------------------------
# Event dataclass
# ---------------------------------------------------------------------------

@dataclass
class RFIDEvent:
    """
    A structured event produced by RFIDListener.

    Fields:
        kind        What type of event this is.
        uid         Normalized card UID, or None if not a card event.
        timestamp   UTC ISO-8601 string when the event was produced.
        raw_line    The original unmodified line from the serial port.
        mode        Listener mode at the time of the event.
        message     Additional text (errors, status replies, boot messages).
    """
    kind      : EventKind
    uid       : Optional[str] = None
    timestamp : str           = field(default_factory=lambda:
                                    datetime.now(timezone.utc)
                                    .strftime("%Y-%m-%dT%H:%M:%S"))
    raw_line  : str           = ""
    mode      : ListenerMode  = ListenerMode.NORMAL
    message   : str           = ""

    def __str__(self):
        parts = [f"[{self.timestamp}]", f"kind={self.kind.value}",
                 f"mode={self.mode.value}"]
        if self.uid:
            parts.append(f"uid={self.uid}")
        if self.message:
            parts.append(f"msg={self.message!r}")
        return " | ".join(parts)


# ---------------------------------------------------------------------------
# UID normalization
# ---------------------------------------------------------------------------

def normalize_uid(raw: str) -> str:
    """
    Normalize a raw UID string from the serial port.

      - strip leading/trailing whitespace
      - remove all internal spaces  ("A9 6E 95 04" -> "A96E9504")
      - uppercase

    Examples:
        "A96E9504"     -> "A96E9504"
        "a96e9504"     -> "A96E9504"
        "a9 6e 95 04"  -> "A96E9504"
        " A96E9504\\n" -> "A96E9504"
    """
    return raw.strip().replace(" ", "").upper()


# ---------------------------------------------------------------------------
# RFIDListener
# ---------------------------------------------------------------------------

class RFIDListener:
    """
    Manages the serial connection to the ESP8266 RFID reader.

    One instance = one physical reader on one serial port.

    Thread model:
        start() opens the port and launches a daemon background thread.
        The thread reads serial lines and places parsed RFIDEvent objects
        on an internal queue.
        The caller consumes events via events() or get_event().

    Assignment mode:
        Call request_assign() to put the reader into ASSIGN mode.
        It blocks until one card is scanned or the timeout expires.
        The reader automatically returns to NORMAL after one card.
    """

    def __init__(self, port: str = DEFAULT_PORT, baud: int = BAUD_RATE):
        """
        Parameters:
            port    Serial port, e.g. "COM7" or "/dev/ttyUSB0".
            baud    Baud rate (default 115200, must match firmware).
        """
        self.port  = port
        self.baud  = baud

        self._serial      : Optional[serial.Serial] = None
        self._thread      : Optional[threading.Thread] = None
        self._stop_event  = threading.Event()
        self._event_queue : queue.Queue = queue.Queue()
        self._mode        = ListenerMode.NORMAL
        self._mode_lock   = threading.Lock()

        # Used by request_assign() to block until a card arrives.
        self._assign_event  = threading.Event()
        self._assign_result : Optional[RFIDEvent] = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(self):
        """
        Open the serial port, wait for the ESP8266 to boot, then send ON.

        Raises:
            serial.SerialException  if the port cannot be opened.
        """
        self._serial = serial.Serial(self.port, self.baud,
                                     timeout=READLINE_TIMEOUT)
        time.sleep(STARTUP_DELAY)
        self._serial.reset_input_buffer()

        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._read_loop,
            name="rfid-listener",
            daemon=True
        )
        self._thread.start()
        self.send_command("ON")

    def stop(self):
        """Send OFF, stop the background thread, close the port."""
        try:
            self.send_command("OFF")
        except Exception:
            pass

        self._stop_event.set()

        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=3.0)

        if self._serial and self._serial.is_open:
            self._serial.close()

    @property
    def is_running(self) -> bool:
        """True if the background thread is alive and the port is open."""
        return (
            self._thread is not None
            and self._thread.is_alive()
            and self._serial is not None
            and self._serial.is_open
        )

    # ------------------------------------------------------------------
    # Commands
    # ------------------------------------------------------------------

    def send_command(self, cmd: str):
        """
        Write a newline-terminated command to the ESP8266.

        Valid commands: ON, OFF, STATUS, ASSIGN

        Raises:
            RuntimeError  if start() has not been called.
        """
        if not self._serial or not self._serial.is_open:
            raise RuntimeError(
                f"Serial port {self.port} is not open. Call start() first."
            )
        self._serial.write((cmd.strip().upper() + "\n").encode("utf-8"))

    # ------------------------------------------------------------------
    # Assignment mode
    # ------------------------------------------------------------------

    def request_assign(self, timeout: float = ASSIGN_TIMEOUT) -> Optional[RFIDEvent]:
        """
        Enter ASSIGN mode and block until one card is scanned.

        Used for card management: the teacher initiates this, one card
        is read, the reader returns to NORMAL automatically.

        Returns:
            RFIDEvent with kind=ASSIGN_SCANNED, or None on timeout.

        Raises:
            RuntimeError  if already in ASSIGN mode.
        """
        with self._mode_lock:
            if self._mode == ListenerMode.ASSIGN:
                raise RuntimeError(
                    "Already in ASSIGN mode. "
                    "Wait for the current scan to complete."
                )
            self._mode = ListenerMode.ASSIGN
            self._assign_event.clear()
            self._assign_result = None

        self.send_command("ASSIGN")

        received = self._assign_event.wait(timeout=timeout)

        if not received:
            with self._mode_lock:
                self._mode = ListenerMode.NORMAL
            self._event_queue.put(RFIDEvent(
                kind=EventKind.ASSIGN_TIMEOUT,
                mode=ListenerMode.ASSIGN,
                message=f"No card scanned within {timeout}s"
            ))
            return None

        return self._assign_result

    @property
    def mode(self) -> ListenerMode:
        """Current listener mode (thread-safe)."""
        with self._mode_lock:
            return self._mode

    # ------------------------------------------------------------------
    # Event consumption
    # ------------------------------------------------------------------

    def get_event(self, block: bool = True,
                  timeout: float = None) -> Optional[RFIDEvent]:
        """
        Get the next event from the queue.

        Returns None if the queue is empty and block=False or timeout expired.
        """
        try:
            return self._event_queue.get(block=block, timeout=timeout)
        except queue.Empty:
            return None

    def events(self):
        """
        Generator that yields events until the listener is stopped.

            for event in listener.events():
                handle(event)
        """
        while not self._stop_event.is_set():
            event = self.get_event(block=True, timeout=0.5)
            if event is not None:
                yield event

    # ------------------------------------------------------------------
    # Background reader thread
    # ------------------------------------------------------------------

    def _read_loop(self):
        while not self._stop_event.is_set():
            try:
                raw = self._serial.readline()
                if not raw:
                    continue
                line = raw.decode("utf-8", errors="ignore").strip()
                if not line:
                    continue

                event = self._parse_line(line)
                if event is None:
                    continue

                # ASSIGN_SCANNED: signal request_assign() before queuing.
                if event.kind == EventKind.ASSIGN_SCANNED:
                    with self._mode_lock:
                        self._assign_result = event
                        self._mode = ListenerMode.NORMAL
                    self._assign_event.set()

                self._event_queue.put(event)

            except serial.SerialException as exc:
                self._event_queue.put(RFIDEvent(
                    kind=EventKind.ERROR,
                    mode=self.mode,
                    message=f"SerialException: {exc}"
                ))
                self._stop_event.set()

            except Exception as exc:
                self._event_queue.put(RFIDEvent(
                    kind=EventKind.ERROR,
                    mode=self.mode,
                    message=f"Unexpected error in read loop: {exc}"
                ))

    # ------------------------------------------------------------------
    # Line parser
    # ------------------------------------------------------------------

    def _parse_line(self, line: str) -> Optional[RFIDEvent]:
        """
        Parse one line from the ESP8266 into an RFIDEvent.

        Returns None for lines that should be silently absorbed
        (e.g. the secondary confirmation lines of ON/OFF responses).
        """
        current_mode = self.mode
        upper = line.upper()

        # UID: XXXXXXXX  (produced in both NORMAL and ASSIGN mode)
        if upper.startswith("UID:"):
            uid = normalize_uid(line.split(":", 1)[1])
            if not uid:
                return RFIDEvent(
                    kind=EventKind.ERROR, raw_line=line, mode=current_mode,
                    message="UID line received but value is empty"
                )
            kind = (EventKind.ASSIGN_SCANNED
                    if current_mode == ListenerMode.ASSIGN
                    else EventKind.CARD_SCANNED)
            return RFIDEvent(kind=kind, uid=uid,
                             raw_line=line, mode=current_mode)

        if upper == "SYSTEM: ON":
            return RFIDEvent(kind=EventKind.SYSTEM_ON,
                             raw_line=line, mode=current_mode)

        if upper == "RFID READER: ACTIVE":
            return None  # absorbed — SYSTEM_ON is sufficient

        if upper == "SYSTEM: OFF":
            return RFIDEvent(kind=EventKind.SYSTEM_OFF,
                             raw_line=line, mode=current_mode)

        if upper == "RFID READER: INACTIVE":
            return None  # absorbed

        if upper.startswith("STATUS:"):
            return RFIDEvent(kind=EventKind.STATUS, raw_line=line,
                             mode=current_mode,
                             message=line.split(":", 1)[1].strip())

        if upper == "ASSIGN: WAITING":
            return RFIDEvent(kind=EventKind.ASSIGN_WAITING,
                             raw_line=line, mode=current_mode)

        if upper == "ASSIGN: DONE":
            return RFIDEvent(kind=EventKind.ASSIGN_DONE,
                             raw_line=line, mode=current_mode)

        if upper.startswith("BOOT:"):
            return RFIDEvent(kind=EventKind.BOOT, raw_line=line,
                             mode=current_mode,
                             message=line.split(":", 1)[1].strip()
                             if ":" in line else line)

        if upper.startswith("ERROR:"):
            return RFIDEvent(kind=EventKind.ERROR, raw_line=line,
                             mode=current_mode,
                             message=line.split(":", 1)[1].strip()
                             if ":" in line else line)

        # Unrecognised line — surface it as an error so nothing is lost.
        return RFIDEvent(kind=EventKind.ERROR, raw_line=line,
                         mode=current_mode,
                         message=f"Unrecognised serial line: {line!r}")


# ---------------------------------------------------------------------------
# Convenience factory
# ---------------------------------------------------------------------------

def create_listener(port: str = DEFAULT_PORT) -> RFIDListener:
    """
    Create an RFIDListener for the given serial port.

    Usage:
        listener = create_listener("COM7")
        listener.start()
    """
    return RFIDListener(port=port)


# ---------------------------------------------------------------------------
# Standalone entry point (manual hardware test)
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import sys

    port = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_PORT
    print(f"RFID Listener — connecting to {port}")
    print("Press Ctrl+C to stop.\n")

    listener = create_listener(port)
    try:
        listener.start()
        print(f"Connected. Listening on {port}\n")
        for event in listener.events():
            print(event)
    except serial.SerialException as e:
        print(f"\nSerial error: {e}")
        print("Check the port and USB connection.")
    except KeyboardInterrupt:
        print("\nStopping...")
    finally:
        listener.stop()
        print("Listener stopped.")
