"""Validated, atomic JSON storage. Every capture is retained as a separate sample."""

import copy
import hashlib
import json
import os
from pathlib import Path
import tempfile
import threading
from datetime import datetime, timezone

from .buttons import BUTTONS, BUTTON_IDS
from .desktop import validate_pointer
from .roles import ROLES, validate_roles
from .window import validate_window


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def validate_signal(signal):
    if not isinstance(signal, dict):
        raise ValueError("The receiver returned an invalid signal.")
    durations = signal.get("durations_us")
    if not isinstance(durations, list) or not 3 <= len(durations) <= 20000 or len(durations) % 2 != 1:
        raise ValueError("A signal must have an odd number of pulse/space durations (3–20000).")
    if any(type(value) is not int or not 1 <= value <= 0xFFFFFF for value in durations):
        raise ValueError("Pulse/space durations must be positive integer microseconds.")
    if type(signal.get("carrier_hz")) is not int or not 1000 <= signal["carrier_hz"] <= 1000000:
        raise ValueError("The signal has an invalid carrier frequency.")
    if signal.get("carrier_source") not in ("assumed", "measured"):
        raise ValueError("The signal must say whether its carrier was assumed or measured.")
    # lirc is the kernel's receiver, gpio a pin Piper reads itself: both are
    # the Pi's own hardware, timed by the kernel.
    if signal.get("source") not in ("lirc", "gpio", "demo"):
        raise ValueError("The signal must identify its hardware or demo source.")
    if not isinstance(signal.get("captured_at"), str):
        raise ValueError("The signal has no capture timestamp.")
    if type(signal.get("trailing_gap_us")) is not int or signal["trailing_gap_us"] < 0:
        raise ValueError("The signal has an invalid trailing gap.")
    return copy.deepcopy(signal)


class RecordingStore:
    def __init__(self, path):
        self.path = Path(path).expanduser().resolve()
        self.lock = threading.RLock()
        raw = self._read_disk()
        self._disk_digest = self._digest(raw)
        if raw is not None:
            try:
                self.document = json.loads(raw.decode("utf-8"))
                self._validate_document()
            except (ValueError, TypeError, KeyError) as exc:
                raise ValueError(f"Cannot load {self.path}: {exc}. Existing file was left untouched.") from exc
        else:
            self.document = {
                "schema_version": 1,
                "remote_name": "One For All TV remote",
                "timing_unit": "microseconds",
                "timing_order": "pulse, space, pulse, …, pulse",
                "created_at": utc_now(),
                "updated_at": utc_now(),
                "recordings": {},
            }

    def _read_disk(self):
        try:
            return self.path.read_bytes()
        except FileNotFoundError:
            return None

    @staticmethod
    def _digest(raw):
        # Absence differs from an empty file, including before the first save.
        return None if raw is None else hashlib.sha256(raw).digest()

    def _check_disk(self):
        if self._digest(self._read_disk()) != self._disk_digest:
            raise RuntimeError(
                "The recordings file changed or was deleted outside this app. "
                "Nothing was overwritten. Stop other apps using this file and "
                "restart PiperTV to load the current data before recording again."
            )

    def _validate_document(self):
        doc = self.document
        if not isinstance(doc, dict) or doc.get("schema_version") != 1 or not isinstance(doc.get("recordings"), dict):
            raise ValueError("Unsupported recordings format")
        # Absent in every library recorded before roles existed, which must
        # keep loading: no bindings simply means each key acts as itself.
        if "roles" in doc:
            validate_roles(doc["roles"])
        # Likewise absent from every library recorded before the cursor could
        # be tuned: no preference means the defaults.
        if "pointer" in doc:
            validate_pointer(doc["pointer"])
        for key, record in doc["recordings"].items():
            if key not in BUTTON_IDS or not isinstance(record, dict):
                raise ValueError("Unknown button in recordings")
            self._label(record["label"])
            if not isinstance(record["samples"], list):
                raise ValueError("Samples must be a list")
            for signal in record["samples"]:
                validate_signal(signal)

    @staticmethod
    def _label(value):
        if not isinstance(value, str) or not value.strip() or len(value.strip()) > 80:
            raise ValueError("Button labels must contain 1–80 characters.")
        if any(ord(char) < 32 for char in value):
            raise ValueError("Button labels cannot contain control characters.")
        return value.strip()

    @staticmethod
    def _check_button(key):
        if key not in BUTTON_IDS:
            raise KeyError("Unknown remote button")

    def snapshot(self):
        with self.lock:
            return copy.deepcopy(self.document)

    def buttons(self):
        with self.lock:
            return [dict(button, label=self.document["recordings"].get(button["id"], {}).get("label", button["label"])) for button in BUTTONS]

    def _record(self, doc, key):
        default = next(button["label"] for button in BUTTONS if button["id"] == key)
        return doc["recordings"].setdefault(key, {"label": default, "samples": []})

    def _commit(self, document):
        import fcntl

        document["updated_at"] = utc_now()
        encoded = (json.dumps(document, indent=2, ensure_ascii=False, allow_nan=False) + "\n").encode("utf-8")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # Lock a stable sidecar inode, because each save replaces the JSON inode.
        # Leave the sidecar in place so concurrent processes always lock the same file.
        lock_path = self.path.with_name(f".{self.path.name}.lock")
        with lock_path.open("a+b") as lock_handle:
            fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX)
            self._check_disk()
            temporary = None
            try:
                with tempfile.NamedTemporaryFile(mode="wb", dir=self.path.parent, prefix=f".{self.path.name}.", suffix=".tmp", delete=False) as handle:
                    temporary = handle.name
                    handle.write(encoded)
                    handle.flush()
                    os.fsync(handle.fileno())
                # Also notice ordinary editor changes while preparing the save.
                self._check_disk()
                os.replace(temporary, self.path)
                self._disk_digest = self._digest(encoded)
                self.document = document
            finally:
                if temporary and os.path.exists(temporary):
                    os.unlink(temporary)

    def append(self, key, signal):
        self._check_button(key)
        signal = validate_signal(signal)
        with self.lock:
            updated = copy.deepcopy(self.document)
            self._record(updated, key)["samples"].append(signal)
            self._commit(updated)

    def roles(self):
        with self.lock:
            return dict(self.document.get("roles", {}))

    def set_role(self, role, button):
        """Bind one navigation role to a button, or release it with None.

        Releasing restores the role to its own key rather than leaving it
        unreachable, so a mistaken binding can always be undone from the studio.
        """
        if role not in ROLES:
            raise ValueError(f"Unknown role {role!r}. Roles are: {', '.join(ROLES)}.")
        if button is not None:
            self._check_button(button)
        with self.lock:
            updated = copy.deepcopy(self.document)
            bindings = dict(updated.get("roles", {}))
            if button is None:
                bindings.pop(role, None)
            else:
                bindings[role] = button
            # Validate the whole map: a new binding can collide with an old one.
            updated["roles"] = validate_roles(bindings)
            self._commit(updated)
            return dict(updated["roles"])

    def pointer(self):
        with self.lock:
            return dict(self.document.get("pointer", {}))

    def set_pointer(self, values):
        """Keep how the cursor behaves beside the signals it is driven by."""
        checked = validate_pointer(values)
        with self.lock:
            updated = copy.deepcopy(self.document)
            updated["pointer"] = checked
            self._commit(updated)
            return dict(checked)

    def window(self):
        with self.lock:
            return dict(self.document.get("window", {}))

    def set_window(self, values):
        """Keep whether Piper fills the screen beside everything else it owns."""
        checked = validate_window(values)
        with self.lock:
            updated = copy.deepcopy(self.document)
            updated["window"] = checked
            self._commit(updated)
            return dict(checked)

    def rename(self, key, label):
        self._check_button(key)
        label = self._label(label)
        with self.lock:
            updated = copy.deepcopy(self.document)
            self._record(updated, key)["label"] = label
            self._commit(updated)

    def delete_sample(self, key, index):
        self._check_button(key)
        with self.lock:
            updated = copy.deepcopy(self.document)
            samples = updated["recordings"].get(key, {}).get("samples", [])
            if type(index) is not int or not 0 <= index < len(samples):
                raise KeyError("Unknown sample")
            del samples[index]
            self._commit(updated)

    def irctl(self, key, index=-1):
        self._check_button(key)
        with self.lock:
            record = self.document["recordings"].get(key)
            if not record or not record["samples"] or index < -1 or index >= len(record["samples"]):
                raise KeyError("Unknown sample")
            sample = record["samples"][index]
            lines = [f"# PiperTV: {record['label']}", f"# Source: {sample['source']}; carrier {sample['carrier_source']}", f"carrier {sample['carrier_hz']}"]
            lines += [f"{'pulse' if i % 2 == 0 else 'space'} {duration}" for i, duration in enumerate(sample["durations_us"])]
            return "\n".join(lines) + "\n"
