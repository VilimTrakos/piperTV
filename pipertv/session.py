"""Decide when the remote may drive this Pi's desktop, and in which mode.

Control is allowed only while three things hold at once: this Pi's HDMI input is
the one the TV is showing, a mode was chosen for that specific visit, and no
recording is in progress. Each of the three can end control on its own.

A mode choice belongs to one visit. Selecting the Pi again starts a new session
with a new identifier, so a choice made on an earlier visit can never be reused
and a browser holding a stale page cannot resurrect one.

Manual confirmation is a user assertion, not CEC evidence: it is only ever
opened explicitly and stays visible as manual. Fresh contrary evidence or a
known HDMI disconnection ends it; inconclusive monitoring alone does not.

See ../docs/hdmi-detection.md for the behaviour this implements.
"""

from __future__ import annotations

from datetime import datetime, timezone
import threading
import uuid

SOURCES = ("active", "inactive", "unknown")
# "piper" drives the interface on the TV itself; the other two move the Pi's
# desktop cursor. All three are gated identically.
MODES = ("pointer", "snapping", "piper")
DESKTOP_MODES = ("pointer", "snapping")
ORIGINS = ("cec", "manual", "served")


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


class ControlSession:
    """The gate between HDMI detection and anything that moves the cursor.

    Every method is safe to call from the CEC monitor thread, the IR reader
    thread, and request threads at once.
    """

    def __init__(self):
        self._lock = threading.RLock()
        self._source = "unknown"
        self._detail = "Waiting for the TV to report its selected input."
        self._session: dict | None = None
        self._hold: str | None = None
        self._stopped = False
        self._selection_revision = None
        self._evidence_revision = None
        self._address_revision = None
        self._address = None
        self._evidence_token = None
        self._manual_evidence = None
        self._manual_revision = None

    # --- detection -------------------------------------------------------

    def update(self, snapshot: dict) -> dict:
        """Fold in the detector's latest conclusion about the selected input."""
        if not isinstance(snapshot, dict):
            raise ValueError("A CEC snapshot must be a JSON object.")
        state = snapshot.get("state")
        if state not in SOURCES:
            state = "unknown"
        detail = snapshot.get("reason")
        with self._lock:
            if self._session is not None and self._session["origin"] == "served":
                # Nothing the set reports can end this visit, because Piper is
                # not on that screen: the interface is in the television's own
                # browser, and the only thing on this Pi's output is what Piper
                # itself was asked to open. What the TV is showing is then a
                # fact about the TV, not evidence about the remote.
                self._source = state
                if isinstance(detail, str) and detail:
                    self._detail = detail
                return self.snapshot()
            previous_source = self._source
            selection_revision = snapshot.get("selection_revision")
            evidence_revision = snapshot.get("evidence_revision")
            address_revision = snapshot.get("address_revision")
            revision_changed = (type(selection_revision) is int
                                and self._selection_revision is not None
                                and selection_revision != self._selection_revision)
            # A lost connection and return to the same HDMI socket may both
            # occur between polls; the address counter preserves that event.
            address_changed = False
            if "physical_address" in snapshot or "local_address" in snapshot:
                address = snapshot.get("physical_address", snapshot.get("local_address"))
                address_changed = self._address is not None and (
                    address != self._address or
                    (type(address_revision) is int and self._address_revision is not None
                     and address_revision != self._address_revision))
                self._address = address
                self._address_revision = address_revision if type(address_revision) is int else None
            self._selection_revision = selection_revision if type(selection_revision) is int else None
            self._evidence_revision = evidence_revision if type(evidence_revision) is int else None
            self._evidence_token = ("revision", evidence_revision) if type(evidence_revision) is int else (
                "report", state, snapshot.get("last_seen"), snapshot.get("source_address"), snapshot.get("event"))
            self._source = state
            if isinstance(detail, str) and detail:
                self._detail = detail
            if state != "active" or revision_changed:
                self._stopped = False
            if address_changed or (previous_source == state == "active" and revision_changed):
                self._end()
            if state == "active":
                # Only the transition into the Pi's input starts a visit; while
                # it stays selected the existing choice must survive.
                if self._session is None and not self._stopped:
                    self._start("cec")
                elif self._session is not None and self._session["origin"] == "manual":
                    # This report corroborates the user's confirmation. Keep
                    # its revision so a later monitoring failure alone cannot
                    # be mistaken for unseen contrary evidence.
                    self._manual_evidence = self._evidence_token
                    self._manual_revision = self._evidence_revision
            elif state == "inactive":
                # Explicit confirmation can supersede an old negative report
                # after a silent TV switch. A fresh report still outranks it.
                if (self._session is None or self._session["origin"] != "manual"
                        or self._evidence_token != self._manual_evidence):
                    self._end()
            elif self._session is not None and self._session["origin"] == "cec":
                # Inconclusive detection cannot keep automatic control alive.
                self._end()
            elif (self._session is not None and self._manual_revision is not None
                  and self._evidence_revision is not None
                  and self._evidence_revision != self._manual_revision):
                # A report followed by a reset may happen between polls. Its
                # conclusion is now unavailable, so require confirmation again.
                self._end()
            return self.snapshot()

    # --- sessions --------------------------------------------------------

    def _start(self, origin: str) -> None:
        self._session = {"id": uuid.uuid4().hex, "origin": origin,
                         "mode": None, "started_at": utc_now()}

    def _end(self) -> None:
        self._session = None
        self._manual_evidence = None
        self._manual_revision = None

    def choose(self, mode: str, session_id: str) -> dict:
        """Bind a mode to the visit the browser was actually looking at."""
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
        """Open a manual session from an explicit confirmation, never from doubt."""
        if confirmed is not True:
            raise ValueError("Confirm that the TV is showing the Pi before controlling it.")
        with self._lock:
            self._stopped = False
            self._start("manual")
            self._manual_evidence = self._evidence_token
            self._manual_revision = self._evidence_revision
            return self.snapshot()

    def start_served(self, mode: str = "piper") -> dict:
        """Open the visit that lasts, for when Piper draws nothing on this TV.

        The gate has one purpose: a press must not move this Pi's cursor while
        the television is showing something else. With the interface served to
        another browser that cannot happen -- the only thing ever on this
        screen is what Piper was asked to put there, by the remote that is now
        driving it. So the visit is the arrangement itself. It is recorded as
        "served" rather than dressed up as a confirmation nobody gave.
        """
        if mode not in MODES:
            raise ValueError("Choose the piper interface, pointer, or snapping mode.")
        with self._lock:
            self._stopped = False
            self._start("served")
            self._session["mode"] = mode
            return self.snapshot()

    def stop(self) -> dict:
        """The visible stop action, for either kind of session."""
        with self._lock:
            self._end()
            self._stopped = self._source == "active"
            return self.snapshot()

    # --- recording interlock ---------------------------------------------

    def hold(self, reason: str) -> dict:
        """Suspend control without ending the visit, so recording keeps the mode."""
        if not isinstance(reason, str) or not reason:
            raise ValueError("A hold must say why control is suspended.")
        with self._lock:
            self._hold = reason
            return self.snapshot()

    def release(self) -> dict:
        with self._lock:
            self._hold = None
            return self.snapshot()

    # --- reporting -------------------------------------------------------

    def enabled(self) -> bool:
        """The predicate the IR reader consults before acting on any button."""
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
