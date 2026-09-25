"""The gate that decides when the remote may drive the Pi, and in which mode.

Control is on only while all three hold: the TV shows the Pi's HDMI input, a
mode was chosen for this visit, and no recording is in progress.

Each time the Pi's input is selected a new session (visit) starts with a new
id, so a mode chosen on an earlier visit, or sent by a stale page, never
applies to the current one.

A manual session comes from the user confirming that the TV shows the Pi; it
is not CEC evidence. Fresh contrary evidence or an HDMI address change ends
it, but a merely inconclusive detector does not.

See ../docs/hdmi-detection.md.
"""

from __future__ import annotations

import threading
import uuid

from .util import utc_now

SOURCES = ("active", "inactive", "unknown")
# "piper" drives the TV interface; the other two move the desktop cursor.
MODES = ("pointer", "snapping", "piper")
DESKTOP_MODES = ("pointer", "snapping")


def _revision(value):
    return value if type(value) is int else None


class ControlSession:
    """Thread-safe: used from the CEC, IR and request threads at once."""

    def __init__(self):
        self._lock = threading.RLock()
        self._source = "unknown"
        self._detail = "Waiting for the TV to report its selected input."
        self._session: dict | None = None
        self._hold: str | None = None
        # Set by stop() while the Pi's input stays selected, so the same
        # selection doesn't immediately start a new session.
        self._stopped = False
        self._selection_revision = None
        self._evidence_revision = None
        self._address_revision = None
        self._address = None
        self._evidence_token = None
        # The evidence a manual session was confirmed against.
        self._manual_evidence = None
        self._manual_revision = None

    def update(self, snapshot: dict) -> dict:
        """Apply the detector's latest snapshot (see CecMonitor.snapshot)."""
        if not isinstance(snapshot, dict):
            raise ValueError("A CEC snapshot must be a JSON object.")
        state = snapshot.get("state")
        if state not in SOURCES:
            state = "unknown"
        detail = snapshot.get("reason")
        selection = _revision(snapshot.get("selection_revision"))
        evidence = _revision(snapshot.get("evidence_revision"))
        address_revision = _revision(snapshot.get("address_revision"))
        with self._lock:
            previous = self._source
            reselected = (selection is not None and self._selection_revision is not None
                          and selection != self._selection_revision)
            # The address revision catches an unplug and replug to the same
            # socket that both happened between two polls.
            address_changed = False
            if "physical_address" in snapshot:
                address = snapshot["physical_address"]
                address_changed = self._address is not None and (
                    address != self._address
                    or (address_revision is not None and self._address_revision is not None
                        and address_revision != self._address_revision))
                self._address = address
                self._address_revision = address_revision
            self._selection_revision = selection
            self._evidence_revision = evidence
            self._evidence_token = (("revision", evidence) if evidence is not None else
                                    ("report", state, snapshot.get("last_seen"),
                                     snapshot.get("source_address"), snapshot.get("event")))
            self._source = state
            if isinstance(detail, str) and detail:
                self._detail = detail

            if state != "active" or reselected:
                self._stopped = False
            if address_changed or (previous == state == "active" and reselected):
                self._end()
            if state == "active":
                if self._session is None and not self._stopped:
                    self._start("cec")
                elif self._session is not None and self._session["origin"] == "manual":
                    # CEC now agrees with the manual confirmation.
                    self._manual_evidence = self._evidence_token
                    self._manual_revision = self._evidence_revision
            elif state == "inactive":
                # A manual confirmation outranks the negative report it was made
                # against (the TV may have switched without saying so), but not
                # a newer one.
                if (self._session is None or self._session["origin"] != "manual"
                        or self._evidence_token != self._manual_evidence):
                    self._end()
            elif self._session is not None and self._session["origin"] == "cec":
                self._end()  # unknown: automatic control needs positive evidence
            elif (self._session is not None and self._manual_revision is not None
                  and self._evidence_revision is not None
                  and self._evidence_revision != self._manual_revision):
                # Some report arrived and was lost again between polls; we can't
                # tell what it said, so ask for confirmation again.
                self._end()
            return self.snapshot()

    def _start(self, origin: str) -> None:
        self._session = {"id": uuid.uuid4().hex, "origin": origin,
                         "mode": None, "started_at": utc_now()}

    def _end(self) -> None:
        self._session = None
        self._manual_evidence = None
        self._manual_revision = None

    def choose(self, mode: str, session_id: str) -> dict:
        """Set the mode for the visit the caller was looking at."""
        if mode not in MODES:
            raise ValueError("Choose the piper interface, pointer, or snapping mode.")
        if not isinstance(session_id, str) or not session_id:
            raise ValueError("A mode choice must name the session it belongs to.")
        with self._lock:
            if self._session is None:
                raise RuntimeError("The TV is not showing the Pi, so there is nothing to control.")
            if session_id != self._session["id"]:
                raise RuntimeError("That choice belongs to an earlier visit to the Pi's input. "
                                   "Choose a mode again for the current one.")
            self._session["mode"] = mode
            return self.snapshot()

    def start_manual(self, confirmed: bool) -> dict:
        if confirmed is not True:
            raise ValueError("Confirm that the TV is showing the Pi before controlling it.")
        with self._lock:
            self._stopped = False
            self._start("manual")
            self._manual_evidence = self._evidence_token
            self._manual_revision = self._evidence_revision
            return self.snapshot()

    def stop(self) -> dict:
        with self._lock:
            self._end()
            self._stopped = self._source == "active"
            return self.snapshot()

    def hold(self, reason: str) -> dict:
        """Suspend control without ending the visit (used while recording)."""
        if not isinstance(reason, str) or not reason:
            raise ValueError("A hold must say why control is suspended.")
        with self._lock:
            self._hold = reason
            return self.snapshot()

    def release(self) -> dict:
        with self._lock:
            self._hold = None
            return self.snapshot()

    def enabled(self) -> bool:
        with self._lock:
            return bool(self._session and self._session["mode"] and not self._hold)

    def snapshot(self) -> dict:
        with self._lock:
            session = dict(self._session) if self._session else None
            return {"source": self._source, "detail": self._detail,
                    "session": session,
                    "origin": session["origin"] if session else None,
                    "mode": session["mode"] if session else None,
                    "needs_mode": bool(session and not session["mode"]),
                    "hold": self._hold,
                    "control": "on" if self.enabled() else "off"}
