"""Small helpers shared by several modules."""

from __future__ import annotations

import os
import shutil
import signal
import time
from datetime import datetime, timezone


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def find_program(*names: str) -> str | None:
    """Full path of the first of these programs found on PATH."""
    for name in names:
        path = shutil.which(name)
        if path:
            return path
    return None


def find_processes(matches, procfs: str = "/proc") -> list[int]:
    """Pids whose command line (a list of arguments) satisfies matches()."""
    try:
        pids = sorted(int(name) for name in os.listdir(procfs) if name.isdigit())
    except OSError:
        return []
    found = []
    for pid in pids:
        try:
            with open(f"{procfs}/{pid}/cmdline", "rb") as handle:
                argv = [part.decode("utf-8", "replace") for part in handle.read().split(b"\0") if part]
        except OSError:
            continue  # exited while we were looking
        if argv and matches(argv):
            found.append(pid)
    return found


def is_running(pid: int, kill=os.kill, procfs: str = "/proc") -> bool:
    try:
        kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        pass  # someone else's process, but it exists
    except OSError:
        return False
    # A zombie still answers signal 0, but its cmdline is empty.
    try:
        with open(f"{procfs}/{pid}/cmdline", "rb") as handle:
            return bool(handle.read(1))
    except OSError:
        return False


def stop_process(pid: int, grace_s: float = 2.0, kill=os.kill, procfs: str = "/proc",
                 clock=time.monotonic, sleep=time.sleep) -> None:
    """SIGTERM, then SIGKILL if it is still running after grace_s. Raises OSError."""
    kill(pid, signal.SIGTERM)
    deadline = clock() + grace_s
    while is_running(pid, kill, procfs) and clock() < deadline:
        sleep(0.1)
    if is_running(pid, kill, procfs):
        kill(pid, signal.SIGKILL)
