"""Observe HDMI-CEC routing evidence without selecting an input.

Physical addresses describe HDMI topology, not the currently displayed input:
https://docs.kernel.org/userspace-api/media/cec/cec-ioc-adap-g-phys-addr.html
Monitor modes and their privilege requirements:
https://docs.kernel.org/userspace-api/media/cec/cec-ioc-g-mode.html

CEC is event based. A TV that silently switches to its tuner or an internal app
cannot be detected with certainty; positive evidence expires instead of keeping
IR control enabled indefinitely. A manual override belongs above this module.
"""

from __future__ import annotations

from datetime import datetime, timezone
import math
import os
import re
import select
import signal
import struct
import subprocess
import threading
import time

PRIVILEGE_HELP = (
    "CEC monitor mode needs root: reading HDMI messages requires CAP_NET_ADMIN, "
    "which owning /dev/cec0 through the video group does not grant. Allow just "
    "the monitor command without a password, or use the manual confirmation in "
    "the interface instead."
)

ACTIVE_SOURCE = 0x82
ROUTING_CHANGE = 0x80
ROUTING_INFORMATION = 0x81
SET_STREAM_PATH = 0x86
INACTIVE_SOURCE = 0x9D
STANDBY = 0x36
REPORT_POWER_STATUS = 0x90


def physical_address(value: str | int | None) -> int | None:
    """Validate a CEC address; None/FFFF means the HDMI address is unknown."""
    if value is None:
        return None
    if isinstance(value, str):
        if re.fullmatch(r"[0-9a-fA-F](?:\.[0-9a-fA-F]){3}", value):
            value = int(value.replace(".", ""), 16)
        elif re.fullmatch(r"(?:0x)?[0-9a-fA-F]{4}", value):
            value = int(value, 16)
        else:
            raise ValueError("CEC physical address must look like 1.0.0.0 or 1000.")
    if type(value) is not int or not 0 <= value <= 0xFFFF:
        raise ValueError("Invalid CEC physical address.")
    if value == 0xFFFF:
        return None
    zero_seen = False
    for shift in (12, 8, 4, 0):
        digit = (value >> shift) & 15
        if digit == 0:
            zero_seen = True
        elif zero_seen:
            raise ValueError("Invalid CEC topology address.")
    return value


def format_address(value: int | None) -> str | None:
    return None if value is None else ".".join(f"{value:04x}")


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


class CecState:
    """Pure, thread-safe reducer for received CEC frames and connection events."""

    def __init__(self, physical_address: str | int | None = None,
                 stale_after_s: float = 120, *, clock=time.monotonic):
        if (type(stale_after_s) not in (int, float) or not math.isfinite(stale_after_s)
                or not 1 <= stale_after_s <= 3600):
            raise ValueError("CEC evidence lifetime must be between 1 and 3600 seconds.")
        self.stale_after_s = float(stale_after_s)
        self._clock = clock
        self._lock = threading.RLock()
        self._physical_address = None
        self._state = "unknown"
        self._reason = "Waiting for the TV to report its selected input."
        self._evidence_at = None
        self._last_seen = None
        self._event = None
        self._source_address = None
        # Consumers poll snapshots, so a quick away/back transition must remain
        # distinguishable from another report during the same selected visit.
        self._selection_revision = 0
        self._evidence_revision = 0
        self._address_revision = 0
        self.set_physical_address(physical_address)

    def set_physical_address(self, value: str | int | None) -> None:
        address = physical_address(value)
        if address == 0:
            address = None  # The Pi is a source, not the root television.
        with self._lock:
            if address != self._physical_address:
                self._physical_address = address
                self._address_revision += 1
                self.unavailable("HDMI address changed; waiting for a new TV input report.")
            if address is None:
                self.unavailable("The Pi's HDMI physical address is unknown.")

    def unavailable(self, reason: str) -> None:
        with self._lock:
            if self._state != "unknown":
                self._selection_revision += 1
            self._state = "unknown"
            self._reason = reason
            self._event = None
            self._evidence_at = None
            self._last_seen = None
            self._source_address = None

    def _expire(self) -> None:
        if (self._state == "active" and self._evidence_at is not None
                and self._clock() - self._evidence_at >= self.stale_after_s):
            self._state = "unknown"
            self._reason = "The last HDMI selection report expired; confirm the input or switch to the Pi again."
            self._selection_revision += 1

    def _evidence(self, state: str, reason: str, event: str, address: int | None = None) -> None:
        self._expire()
        if state != self._state:
            self._selection_revision += 1
        self._evidence_revision += 1
        self._state = state
        self._reason = reason
        self._event = event
        self._evidence_at = self._clock()
        self._last_seen = _utc_now()
        self._source_address = address

    def observe(self, frame: bytes, *, received: bool = True) -> bool:
        """Apply one complete received CEC frame; return whether it is evidence.

        Sent frames must be identified by the transport and are always ignored.
        The TV is logical address 0. Routing reports from other HDMI branches do
        not establish that the TV is displaying this branch, so only TV routing
        messages can enable this directly-connected Pi.
        """
        if not received or not isinstance(frame, bytes) or not 2 <= len(frame) <= 16:
            return False
        initiator, destination = frame[0] >> 4, frame[0] & 15
        opcode = frame[1]
        with self._lock:
            if self._physical_address is None:
                return False
            if opcode in (ACTIVE_SOURCE, ROUTING_CHANGE, ROUTING_INFORMATION, SET_STREAM_PATH):
                expected = 6 if opcode == ROUTING_CHANGE else 4
                if len(frame) != expected or destination != 15 or initiator == 15:
                    return False
                if opcode != ACTIVE_SOURCE and initiator != 0:
                    return False
                try:
                    address = physical_address(int.from_bytes(frame[-2:], "big"))
                    if opcode == ROUTING_CHANGE:
                        if physical_address(int.from_bytes(frame[2:4], "big")) is None:
                            return False
                except ValueError:
                    return False
                if address is None:
                    return False
                event = {ACTIVE_SOURCE: "active_source", ROUTING_CHANGE: "routing_change",
                         ROUTING_INFORMATION: "routing_information", SET_STREAM_PATH: "set_stream_path"}[opcode]
                selected = address == self._physical_address
                self._evidence("active" if selected else "inactive",
                               "CEC reports the Pi's HDMI input selected." if selected else
                               "CEC reports another source selected.", event, address)
                return True
            if opcode == INACTIVE_SOURCE:
                if len(frame) != 4 or destination != 0 or initiator == 15:
                    return False
                if int.from_bytes(frame[2:], "big") == self._physical_address:
                    self._evidence("inactive", "The Pi's HDMI source was marked inactive.", "inactive_source")
                    return True
            elif opcode == STANDBY:
                if len(frame) == 2 and destination in (0, 15) and initiator != 15:
                    self._evidence("inactive", "CEC requested TV standby.", "standby")
                    return True
            elif opcode == REPORT_POWER_STATUS and initiator == 0 and len(frame) == 3:
                if frame[2] in (1, 3):
                    self._evidence("inactive", "The TV reports standby.", "tv_standby")
                    return True
                if frame[2] in (0, 2) and self._event in ("standby", "tv_standby"):
                    self.unavailable("The TV is on; its selected input is not yet known.")
            return False

    def snapshot(self) -> dict:
        with self._lock:
            self._expire()
            age = None if self._evidence_at is None else max(0.0, self._clock() - self._evidence_at)
            return {"state": self._state, "reason": self._reason,
                    "physical_address": format_address(self._physical_address),
                    "local_address": format_address(self._physical_address),
                    "source_address": format_address(self._source_address),
                    "last_seen": self._last_seen, "evidence_age_s": None if age is None else round(age, 2),
                    "stale_after_s": self.stale_after_s, "source": "cec", "event": self._event,
                    "selection_revision": self._selection_revision,
                    "evidence_revision": self._evidence_revision,
                    "address_revision": self._address_revision}


class CecCtlParser:
    """Parse cec-ctl --monitor --show-raw; never treat transmitted data as input.

    Output format is defined by show_msg/log_raw_msg/log_event in v4l-utils:
    https://github.com/gjasny/v4l-utils/blob/master/utils/cec-ctl/cec-ctl.cpp
    """

    _header = re.compile(r"^(Received from|Transmitted by) .+ \((\d+) to (\d+)\):")
    _raw = re.compile(r"^\s*Raw:\s*(0x[0-9a-fA-F]{2}(?:\s+0x[0-9a-fA-F]{2}){0,15})\s*(?:\([^\r\n]*\))?\s*$")
    _address = re.compile(r"Event: State Change: PA: ([0-9a-fA-F](?:\.[0-9a-fA-F]){3})(?:,|$)")

    def __init__(self, state: CecState):
        self.state = state
        self.ready = False
        self.failed = False
        self.needs_root = False
        self._pending = None

    def feed_line(self, line: str) -> bool:
        if len(line) > 8192:
            self.failed = True
            self.state.unavailable("CEC monitor produced an invalid oversized line.")
            return False
        stripped = line.strip()
        lower = stripped.lower()
        if ("events were lost" in lower or re.search(r"event: lost \d+ messages", lower)):
            self._pending = None
            self.state.unavailable("CEC messages were lost; waiting for a new TV input report.")
            return False
        address = self._address.search(stripped)
        if address:
            self._pending = None
            self.ready = True
            try:
                self.state.set_physical_address(address.group(1))
                if self.state.snapshot()["physical_address"] is not None:
                    # Even an unchanged address may follow a missed unplug/replug.
                    self.state.unavailable("CEC connected; switch away from the Pi input and back to report its selection.")
            except ValueError:
                self.state.set_physical_address(None)
            return False
        # cec-ctl indents a message's decoded payload, so a TV's own OSD name,
        # vendor text, or status string must never be read as a monitor failure.
        # cec-ctl and sudo report their own errors unindented, outside a message.
        errors = ("disconnected", "permission denied", "operation not permitted", "no such file",
                  "failed", "cannot open", "could not open", "error:", "password is required")
        if (line[:1] not in (" ", "\t") and self._pending is None
                and any(message in lower for message in errors)):
            self._pending = None
            self.ready = False
            self.failed = True
            # cec-ctl exits 0 after refusing monitor mode, so this line is the
            # only signal that the kernel turned the request down.
            if "monitor mode failed" in lower or "as root" in lower:
                self.needs_root = True
                self.state.unavailable(PRIVILEGE_HELP)
            else:
                self.state.unavailable("CEC monitor unavailable: " + stripped[:250])
            return False
        header = self._header.match(stripped)
        if header:
            initiator, destination = int(header.group(2)), int(header.group(3))
            self._pending = ((header.group(1) == "Received from", initiator << 4 | destination)
                             if 0 <= initiator <= 15 and 0 <= destination <= 15 else None)
            return False
        if stripped.startswith("Raw:"):
            pending, self._pending = self._pending, None
            raw = self._raw.match(stripped)
            if pending is None or raw is None:
                return False
            frame = bytes(int(value, 16) for value in raw.group(1).split())
            if frame[0] != pending[1]:
                return False
            self.ready = True
            return self.state.observe(frame, received=pending[0])
        if stripped.startswith(("Received from", "Transmitted by", "Event:", "Initial Event:")):
            self._pending = None
        return False


def read_physical_address(device: str) -> int | None:
    """Read the current EDID-derived address without claiming CEC identities."""
    import fcntl

    fd = os.open(device, os.O_RDONLY | os.O_NONBLOCK | os.O_CLOEXEC)
    try:
        value = bytearray(2)
        # _IOR('a', 1, __u16), Linux UAPI, identical on Pi ARM32/ARM64.
        fcntl.ioctl(fd, 0x80026101, value, True)  # CEC_ADAP_G_PHYS_ADDR
        return physical_address(struct.unpack("=H", value)[0])
    finally:
        os.close(fd)


class CecMonitor:
    """Supervise a passive cec-ctl child while the Flask process stays unprivileged.

    No transmission, identity configuration, topology scan, or source-activation
    commands are issued. A supplied command is an explicit local/testing
    override, never an HTTP input.

    Device access permits reading the physical address, but selecting Linux
    monitor mode additionally requires CAP_NET_ADMIN. privileged=True prepends
    sudo -n for an explicitly configured privileged monitor; it never prompts,
    so it fails at once where sudo wants a password.
    """

    def __init__(self, device: str = "/dev/cec0", stale_after_s: float = 120,
                 privileged: bool = False, command: list[str] | None = None):
        if not isinstance(device, str) or not re.fullmatch(r"/dev/cec\d+", device):
            raise ValueError("CEC device must look like /dev/cec0.")
        if command is not None and (not isinstance(command, (list, tuple)) or not command
                                    or not all(isinstance(arg, str) and arg for arg in command)):
            raise ValueError("CEC monitor command must be a list of arguments.")
        self.device = device
        self.privileged = privileged
        self.command = list(command) if command is not None else None
        self.state = CecState(stale_after_s=stale_after_s)
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._thread = None
        self._process = None
        self._ready = False

    def _command(self) -> list[str]:
        if self.command is not None:
            return self.command.copy()
        command = ["stdbuf", "-oL", "-eL", "cec-ctl", "-d", self.device,
                   "--monitor", "--show-raw", "--skip-info"]
        return ["sudo", "-n", *command] if self.privileged else command

    def start(self) -> None:
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._stop.clear()
            self._thread = threading.Thread(target=self._run, name="cec-monitor", daemon=True)
            self._thread.start()

    def snapshot(self) -> dict:
        with self._lock:
            result = self.state.snapshot()
            running = self._process is not None and self._process.poll() is None and self._ready
            result.update(monitor_running=running, device=self.device)
            if result["state"] == "active" and not running:
                result.update(state="unknown", reason="CEC monitoring stopped; HDMI selection is unknown.")
            return result

    def _stop_process(self, process) -> None:
        if process.poll() is not None:
            return
        for signum in (signal.SIGTERM, signal.SIGKILL):
            try:
                os.killpg(process.pid, signum)
            except ProcessLookupError:
                return
            except PermissionError:
                if not self.privileged or self.command is not None:
                    raise
                # sudo may have changed the child credentials. Address only the
                # process group created for this specific monitor subprocess.
                subprocess.run(["sudo", "-n", "kill", f"-{signum.name[3:]}", "--", f"-{process.pid}"],
                               stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                               stderr=subprocess.DEVNULL, timeout=1, check=False)
            try:
                process.wait(timeout=0.6)
                return
            except subprocess.TimeoutExpired:
                pass

    def _refresh_address(self) -> bool:
        """Read the adapter address only when this process is allowed to.

        If the Flask account lacks device access, a denied read must leave the
        address to cec-ctl's own State Change events instead of clearing it.
        """
        try:
            self.state.set_physical_address(read_physical_address(self.device))
        except PermissionError:
            return False
        return True

    def _run(self) -> None:
        while not self._stop.is_set():
            process = None
            parser = CecCtlParser(self.state)
            try:
                if self._refresh_address() and self.state.snapshot()["physical_address"] is not None:
                    self.state.unavailable("Waiting for the TV to report a source change; switch away from the Pi and back.")
                process = subprocess.Popen(self._command(), stdin=subprocess.DEVNULL,
                                           stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                           start_new_session=True, env={**os.environ, "LC_ALL": "C"})
                with self._lock:
                    self._process = process
                    self._ready = False
                pending = bytearray()
                next_address_check = time.monotonic() + 2
                while not self._stop.is_set():
                    now = time.monotonic()
                    if now >= next_address_check:
                        self._refresh_address()
                        next_address_check = now + 2
                    readable, _, _ = select.select([process.stdout], [], [], 0.2)
                    if readable:
                        chunk = os.read(process.stdout.fileno(), 16384)
                        if not chunk:
                            break
                        pending.extend(chunk)
                        while b"\n" in pending:
                            line, _, pending = pending.partition(b"\n")
                            parser.feed_line(line.decode("utf-8", "replace"))
                        if len(pending) > 8192:
                            raise RuntimeError("CEC monitor returned oversized output.")
                        with self._lock:
                            self._ready = parser.ready and not parser.failed
                        if parser.failed:
                            break
                    if process.poll() is not None:
                        break
                if not parser.failed and not self._stop.is_set():
                    self.state.unavailable("CEC monitor exited; reconnecting to the HDMI adapter.")
            except FileNotFoundError as exc:
                missing = self.device if exc.filename == self.device else "cec-ctl, stdbuf, or sudo"
                self.state.unavailable(f"CEC monitoring unavailable: {missing} was not found.")
            except PermissionError:
                self.state.unavailable("CEC access was denied. Check device permissions and passive-monitor privileges.")
            except (OSError, RuntimeError) as exc:
                self.state.unavailable("CEC monitoring unavailable: " + str(exc)[:250])
            finally:
                if process is not None:
                    try:
                        self._stop_process(process)
                    except (OSError, subprocess.SubprocessError):
                        self.state.unavailable("CEC monitor could not be stopped; HDMI selection is unknown.")
                    if process.stdout is not None:
                        process.stdout.close()
                with self._lock:
                    self._process = None
                    self._ready = False
            self._stop.wait(5)
        self.state.unavailable("CEC monitoring stopped.")

    def close(self) -> None:
        self._stop.set()
        with self._lock:
            thread = self._thread
        if thread is not None:
            thread.join(timeout=3)
        self.state.unavailable("CEC monitoring stopped.")
