"""The slot spool: one small file per progress source.

Any program enrols by writing a file into the spool directory
(``$XDG_RUNTIME_DIR/metamonitor/slots`` by default, overridable with
``METAMONITOR_SPOOL``).  The file is either a bare number (the percentage,
the file name being the task name) or a JSON object::

    {"name": "LUT build", "percent": 42.5, "state": "running",
     "detail": "band 120/400", "pid": 12345}

Writes go through a temporary file and ``os.replace`` so readers never see
a half-written slot.  No daemon is needed: slots written while the
dashboard is closed appear as soon as it starts.

SPDX-License-Identifier: Unlicense
"""

import json
import os
import re
import time
from pathlib import Path

STATES = ("running", "paused", "blocked", "idle", "done", "failed")


def spool_dir():
    """Return the spool directory, creating it if needed."""
    env = os.environ.get("METAMONITOR_SPOOL")
    if env:
        path = Path(env)
    else:
        runtime = os.environ.get("XDG_RUNTIME_DIR") or f"/tmp/metamonitor-{os.getuid()}"
        path = Path(runtime) / "metamonitor" / "slots"
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    return path


def slot_id(text):
    """Turn arbitrary text into a safe slot file name."""
    sid = re.sub(r"[^A-Za-z0-9._-]+", "-", text).strip("-.")
    return sid[:96] or "task"


def write_slot(sid, **fields):
    """Create or update a slot, merging with what is already there.

    :param sid: slot identifier (file name)
    :param fields: any of name, percent, state, detail, pid, source
    :return: the merged slot dictionary
    """
    path = spool_dir() / slot_id(sid)
    slot = read_slot(path) or {}
    now = time.time()
    slot.setdefault("started", now)
    slot.update({k: v for k, v in fields.items() if v is not None})
    if "percent" in fields and fields["percent"] is None:
        slot["percent"] = None
    slot.setdefault("name", sid)
    slot["updated"] = now
    if slot.get("state") in ("done", "failed") and "finished" not in slot:
        slot["finished"] = now
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(slot))
    os.replace(tmp, path)
    return slot


def read_slot(path):
    """Read one slot file; return a dict or None if unreadable."""
    path = Path(path)
    try:
        raw = path.read_text().strip()
        st = path.stat()
    except OSError:
        return None
    if not raw:
        return None
    try:
        slot = json.loads(raw)
    except ValueError:
        slot = None
    if isinstance(slot, (int, float)):
        slot = {"percent": float(slot)}
    if not isinstance(slot, dict):
        m = re.match(r"\s*(\d+(?:\.\d+)?)\s*%?\s*(.*)", raw, re.S)
        if not m:
            return None
        slot = {"percent": float(m.group(1)), "detail": m.group(2).strip()[:160]}
    slot.setdefault("name", path.name)
    slot.setdefault("updated", st.st_mtime)
    slot.setdefault("started", st.st_mtime)
    p = slot.get("percent")
    if p is not None:
        try:
            slot["percent"] = max(0.0, min(100.0, float(p)))
        except (TypeError, ValueError):
            slot["percent"] = None
    slot.setdefault("state", "done" if (slot.get("percent") or 0) >= 100 else "running")
    if slot["state"] not in STATES:
        slot["state"] = "running"
    slot["id"] = path.name
    return slot


def list_slots():
    """Return every readable slot in the spool."""
    out = []
    for path in sorted(spool_dir().iterdir()):
        if path.name.startswith(".") or not path.is_file():
            continue
        slot = read_slot(path)
        if slot:
            out.append(slot)
    return out


def remove_slot(sid):
    try:
        (spool_dir() / slot_id(sid)).unlink()
        return True
    except FileNotFoundError:
        return False


def sweep_finished(older_than=0.0):
    """Delete the slot files of tasks that are done or failed.

    The dashboard sweeps them when it closes, and again every so often during
    a long session, so that a day of finished work does not sit in the spool
    waiting for a collector to notice it.

    :param older_than: keep a slot that finished less than this many seconds
        ago; 0 removes every finished slot, including those still on screen
    :return: the number of slots removed
    """
    now = time.time()
    removed = 0
    for slot in list_slots():
        if slot["state"] not in ("done", "failed"):
            continue
        if now - slot.get("finished", slot["updated"]) < older_than:
            continue
        if remove_slot(slot["id"]):
            removed += 1
    return removed


def pid_alive(pid):
    try:
        os.kill(int(pid), 0)
    except ProcessLookupError:
        return False
    except (PermissionError, TypeError, ValueError):
        return True
    return True


def proc_start_ticks(pid):
    """Start time of ``pid`` in clock ticks since boot (``/proc/<pid>/stat``
    field 22), or None when the process is gone or unreadable."""
    try:
        with open(f"/proc/{int(pid)}/stat", "rb") as fh:
            stat = fh.read()
        # The command name (field 2) may hold spaces and parentheses.
        return int(stat[stat.rindex(b")") + 2:].split()[19])
    except (OSError, ValueError, TypeError, IndexError):
        return None


def proc_started(pid):
    """Start time of ``pid`` in epoch seconds, or None when it is gone."""
    ticks = proc_start_ticks(pid)
    if ticks is None:
        return None
    try:
        with open("/proc/stat") as fh:
            btime = next(int(line.split()[1]) for line in fh if line.startswith("btime "))
    except (OSError, StopIteration, ValueError):
        return None
    return btime + ticks / os.sysconf("SC_CLK_TCK")


def same_process(pid, proc_start=None, seen_at=None):
    """True when ``pid`` still runs the process we were told about.

    A bare PID can be reused by an unrelated process after a crash, which
    would keep a dead task looking alive. ``proc_start`` (start ticks, as
    Claude Code records it) is checked exactly. ``seen_at`` is a time (epoch
    seconds) when the PID was known to be ours, such as the last write of the
    file that names it: a process that began after it is somebody else.
    """
    if not pid_alive(pid):
        return False
    if proc_start not in (None, ""):
        ticks = proc_start_ticks(pid)
        if ticks is not None and str(ticks) != str(proc_start):
            return False
    if seen_at:
        began = proc_started(pid)
        if began is not None and began > seen_at + 2:
            return False
    return True
