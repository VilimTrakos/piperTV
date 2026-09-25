"""The recordings library: a JSON file, validated on load and replaced atomically on save.

Every capture of a button is kept as its own sample. The same file also holds
the role bindings and the pointer and window settings.
"""

import copy
import fcntl
import hashlib
import json
import os
import tempfile
import threading
from contextlib import contextmanager
from pathlib import Path

from .buttons import BUTTONS, BUTTON_IDS
from .desktop import validate_pointer
from .roles import ROLES, validate_roles
from .util import utc_now
from .window import validate_window


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
    # lirc: the kernel receiver, gpio: a pin we read ourselves
    if signal.get("source") not in ("lirc", "gpio", "demo"):
        raise ValueError("The signal must identify its hardware or demo source.")
    if not isinstance(signal.get("captured_at"), str):
        raise ValueError("The signal has no capture timestamp.")
    if type(signal.get("trailing_gap_us")) is not int or signal["trailing_gap_us"] < 0:
        raise ValueError("The signal has an invalid trailing gap.")
    return copy.deepcopy(signal)


def validate_label(value):
    if not isinstance(value, str) or not value.strip() or len(value.strip()) > 80:
        raise ValueError("Button labels must contain 1–80 characters.")
    if any(ord(char) < 32 for char in value):
        raise ValueError("Button labels cannot contain control characters.")
    return value.strip()


def check_button(key):
    if key not in BUTTON_IDS:
        raise KeyError("Unknown remote button")


def _digest(raw):
    return None if raw is None else hashlib.sha256(raw).digest()


class RecordingStore:
    def __init__(self, path):
        self.path = Path(path).expanduser().resolve()
        self.lock = threading.RLock()
        raw = self._read_disk()
        # What we last saw on disk; a save refuses to go ahead if it changed.
        self._disk_digest = _digest(raw)
        if raw is None:
            now = utc_now()
            self.document = {
                "schema_version": 1,
                "remote_name": "One For All TV remote",
                "timing_unit": "microseconds",
                "timing_order": "pulse, space, pulse, …, pulse",
                "created_at": now,
                "updated_at": now,
                "recordings": {},
            }
            return
        try:
            self.document = json.loads(raw.decode("utf-8"))
            self._validate_document()
        except (ValueError, TypeError, KeyError) as exc:
            raise ValueError(f"Cannot load {self.path}: {exc}. Existing file was left untouched.") from exc

    def _read_disk(self):
        try:
            return self.path.read_bytes()
        except FileNotFoundError:
            return None

    def _check_disk(self):
        if _digest(self._read_disk()) != self._disk_digest:
            raise RuntimeError(
                "The recordings file changed or was deleted outside this app. "
                "Nothing was overwritten. Stop other apps using this file and "
                "restart PiperTV to load the current data before recording again.")

    def _validate_document(self):
        doc = self.document
        if (not isinstance(doc, dict) or doc.get("schema_version") != 1
                or not isinstance(doc.get("recordings"), dict)):
            raise ValueError("Unsupported recordings format")
        # Older libraries have no roles or pointer settings; that is fine.
        if "roles" in doc:
            validate_roles(doc["roles"])
        if "pointer" in doc:
            validate_pointer(doc["pointer"])
        for key, record in doc["recordings"].items():
            if key not in BUTTON_IDS or not isinstance(record, dict):
                raise ValueError("Unknown button in recordings")
            validate_label(record["label"])
            if not isinstance(record["samples"], list):
                raise ValueError("Samples must be a list")
            for signal in record["samples"]:
                validate_signal(signal)

    def _commit(self, document):
        document["updated_at"] = utc_now()
        encoded = (json.dumps(document, indent=2, ensure_ascii=False, allow_nan=False) + "\n").encode("utf-8")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # Lock a separate file: every save replaces the JSON file itself.
        lock_path = self.path.with_name(f".{self.path.name}.lock")
        with lock_path.open("a+b") as lock_handle:
            fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX)
            self._check_disk()
            temporary = None
            try:
                with tempfile.NamedTemporaryFile(mode="wb", dir=self.path.parent, prefix=f".{self.path.name}.",
                                                 suffix=".tmp", delete=False) as handle:
                    temporary = handle.name
                    handle.write(encoded)
                    handle.flush()
                    os.fsync(handle.fileno())
                self._check_disk()  # an editor may have saved it meanwhile
                os.replace(temporary, self.path)
                self._disk_digest = _digest(encoded)
                self.document = document
            finally:
                if temporary and os.path.exists(temporary):
                    os.unlink(temporary)

    @contextmanager
    def _editing(self):
        """Edit a copy of the document; it is saved, and becomes current, on exit."""
        with self.lock:
            document = copy.deepcopy(self.document)
            yield document
            self._commit(document)

    def snapshot(self):
        with self.lock:
            return copy.deepcopy(self.document)

    def buttons(self):
        with self.lock:
            recordings = self.document["recordings"]
            return [dict(button, label=recordings.get(button["id"], {}).get("label", button["label"]))
                    for button in BUTTONS]

    @staticmethod
    def _record(doc, key):
        default = next(button["label"] for button in BUTTONS if button["id"] == key)
        return doc["recordings"].setdefault(key, {"label": default, "samples": []})

    def append(self, key, signal):
        check_button(key)
        signal = validate_signal(signal)
        with self._editing() as doc:
            self._record(doc, key)["samples"].append(signal)

    def rename(self, key, label):
        check_button(key)
        label = validate_label(label)
        with self._editing() as doc:
            self._record(doc, key)["label"] = label

    def delete_sample(self, key, index):
        check_button(key)
        with self._editing() as doc:
            samples = doc["recordings"].get(key, {}).get("samples", [])
            if type(index) is not int or not 0 <= index < len(samples):
                raise KeyError("Unknown sample")
            del samples[index]

    def roles(self):
        with self.lock:
            return dict(self.document.get("roles", {}))

    def set_role(self, role, button):
        """Bind a role to a button, or give it back to its own key with None."""
        if role not in ROLES:
            raise ValueError(f"Unknown role {role!r}. Roles are: {', '.join(ROLES)}.")
        if button is not None:
            check_button(button)
        with self._editing() as doc:
            bindings = dict(doc.get("roles", {}))
            if button is None:
                bindings.pop(role, None)
            else:
                bindings[role] = button
            # Check the whole map: the new binding may collide with another.
            doc["roles"] = validate_roles(bindings)
        return dict(doc["roles"])

    def pointer(self):
        with self.lock:
            return dict(self.document.get("pointer", {}))

    def set_pointer(self, values):
        checked = validate_pointer(values)
        with self._editing() as doc:
            doc["pointer"] = checked
        return dict(checked)

    def window(self):
        with self.lock:
            return dict(self.document.get("window", {}))

    def set_window(self, values):
        checked = validate_window(values)
        with self._editing() as doc:
            doc["window"] = checked
        return dict(checked)

    def irctl(self, key, index=-1):
        """One sample in the text format `ir-ctl --send` reads."""
        check_button(key)
        with self.lock:
            record = self.document["recordings"].get(key)
            if not record or not record["samples"] or index < -1 or index >= len(record["samples"]):
                raise KeyError("Unknown sample")
            sample = record["samples"][index]
            lines = [f"# PiperTV: {record['label']}",
                     f"# Source: {sample['source']}; carrier {sample['carrier_source']}",
                     f"carrier {sample['carrier_hz']}"]
            lines += [f"{'pulse' if i % 2 == 0 else 'space'} {duration}"
                      for i, duration in enumerate(sample["durations_us"])]
            return "\n".join(lines) + "\n"
