"""GRASS GIS sessions, found from mapset ``.gislock`` files and process envs.

A session appears as a parent row (project/mapset).  Its modules appear as
children: exact progress comes from slots written by the libmmgrass shim
(see ``grass/mmgrass.c``); a module without such a slot (shim not hooked,
Python API session...) gets an estimate from how far it has read its
largest input map, or an indeterminate bar.

Locks whose process is gone are reported, since a stale ``.gislock``
keeps the mapset from opening.

SPDX-License-Identifier: Unlicense
"""

import os
import re
import time
from pathlib import Path

from .. import grass, spool
from ..model import Task
from .procfs import _human

MODULE_RE = re.compile(r"^(?:d|db|g|i|m|ps|r|r3|t|v)\.[\w.]+$")
LOCK_SCAN_EVERY = 10.0
STALE_RECENT = 86400  # Stale locks younger than this get their own row.
QUIET_MODULES = {"g.message", "g.gisenv", "g.region", "g.findfile", "g.proj", "g.list"}


def _environ(pid):
    try:
        with open(f"/proc/{pid}/environ", "rb") as fh:
            raw = fh.read()
    except OSError:
        return None
    env = {}
    for item in raw.split(b"\0"):
        k, sep, v = item.partition(b"=")
        if sep and k in (b"GIS_LOCK", b"GISRC"):
            env[k.decode()] = v.decode(errors="replace")
    return env


def _cmdline(pid):
    try:
        with open(f"/proc/{pid}/cmdline", "rb") as fh:
            return [a.decode(errors="replace") for a in fh.read().split(b"\0") if a]
    except OSError:
        return []


def _module_name(argv):
    """Return the GRASS module a command line runs, or None."""
    if not argv:
        return None
    name = os.path.basename(argv[0])
    if name.startswith("python") and len(argv) > 1 and not argv[1].startswith("-"):
        name = os.path.basename(argv[1]).removesuffix(".py")
    return name if MODULE_RE.match(name) else None


def _input_progress(pid, dbase):
    """(fraction, path) of the largest map file opened for reading."""
    best = None
    fd_dir = f"/proc/{pid}/fd"
    try:
        fds = os.listdir(fd_dir)
    except OSError:
        return None
    for fd in fds:
        try:
            target = os.readlink(f"{fd_dir}/{fd}")
            if not target.startswith(dbase):
                continue
            st = os.stat(f"{fd_dir}/{fd}")
            if st.st_size < 1 << 20:
                continue
            with open(f"/proc/{pid}/fdinfo/{fd}") as fh:
                info = dict(l.split(":", 1) for l in fh.read().splitlines() if ":" in l)
            if int(info["flags"].strip(), 8) & 0o3:
                continue
            pos = int(info["pos"].strip())
        except (OSError, KeyError, ValueError):
            continue
        if best is None or st.st_size > best[2]:
            best = (pos, target, st.st_size)
    if not best:
        return None
    pos, target, size = best
    return min(1.0, pos / size), target, size


class GrassCollector:
    source = "grass"

    def __init__(self):
        self.uid = os.getuid()
        self.locks = []  # (lock path, pid, mtime)
        self._last_scan = 0.0
        self._dbases = set()

    def _scan_locks(self, extra_dbases):
        now = time.time()
        if now - self._last_scan < LOCK_SCAN_EVERY and not extra_dbases - self._dbases:
            return
        self._last_scan = now
        self._dbases = grass.gisdbases() | extra_dbases
        locks = []
        for db in self._dbases:
            for lock in db.glob("*/*/.gislock"):
                pid = grass.read_lock(lock)
                try:
                    mtime = lock.stat().st_mtime
                except OSError:
                    continue
                locks.append((lock, pid, mtime))
        self.locks = locks

    def poll(self):
        now = time.time()
        sessions = {}  # lock pid -> dict
        modules = []
        for entry in os.scandir("/proc"):
            if not entry.name.isdigit():
                continue
            try:
                if entry.stat().st_uid != self.uid:
                    continue
            except OSError:
                continue
            env = _environ(entry.name)
            if not env or "GIS_LOCK" not in env:
                continue
            lock_pid = env["GIS_LOCK"]
            if lock_pid not in sessions:
                rc = grass.read_gisrc(env.get("GISRC", ""))
                sessions[lock_pid] = rc
            argv = _cmdline(entry.name)
            name = _module_name(argv)
            if name and name not in QUIET_MODULES:
                modules.append((int(entry.name), lock_pid, name, argv))

        extra = {Path(rc["GISDBASE"]) for rc in sessions.values() if rc.get("GISDBASE")}
        self._scan_locks({d for d in extra if d.is_dir()})

        out = []
        live_locks = {}
        stale = []
        for lock, pid, mtime in self.locks:
            # The owner wrote the lock, so it already ran at the lock's mtime;
            # a later process with that PID means the owner died long ago.
            if pid and spool.same_process(pid, seen_at=mtime):
                live_locks[str(pid)] = lock
            else:
                stale.append((lock, pid, mtime))

        for lock_pid in set(sessions) | set(live_locks):
            rc = sessions.get(lock_pid, {})
            lock = live_locks.get(lock_pid)
            if lock:
                mapset, project, dbase = lock.parent.name, lock.parent.parent.name, str(
                    lock.parent.parent.parent)
            else:
                mapset = rc.get("MAPSET", "?")
                project = rc.get("LOCATION_NAME", "?")
                dbase = rc.get("GISDBASE", "")
            mods = [m for m in modules if m[1] == lock_pid]
            try:
                started = os.stat(f"/proc/{lock_pid}").st_mtime
            except OSError:
                started = now
            out.append(Task(
                id=f"grass:{lock_pid}",
                group="grass",
                title=f"{project} / {mapset}",
                subtitle=f"GRASS session · pid {lock_pid} · {_short(dbase)}"
                + ("" if lock else " · no .gislock"),
                detail=" ".join(mods[-1][3])[:160] if mods else "",
                percent=None,
                state="running" if mods else "idle",
                started=started,
                icon="mm-grass-symbolic",
            ))
            for pid, _, name, argv in mods:
                if (spool.spool_dir() / f"grass-{pid}").exists():
                    continue  # The shim reports this one exactly.
                est = _input_progress(pid, dbase) if dbase else None
                try:
                    mstart = os.stat(f"/proc/{pid}").st_mtime
                except OSError:
                    mstart = now
                out.append(Task(
                    id=f"grass-proc:{pid}",
                    group="grass",
                    title=name,
                    subtitle=(f"pid {pid} · estimated from input read, "
                              f"{_human(est[2])} map" if est else f"pid {pid} · no progress hook"),
                    detail=" ".join(argv)[:160],
                    percent=100.0 * est[0] if est else None,
                    state="running",
                    started=mstart,
                    parent=f"grass:{lock_pid}",
                    icon="mm-module-symbolic",
                ))

        recent = [s for s in stale if now - s[2] < STALE_RECENT]
        old = [s for s in stale if now - s[2] >= STALE_RECENT]
        for lock, pid, mtime in recent:
            out.append(Task(
                id=f"grass-stale:{lock}",
                group="grass",
                title=f"{lock.parent.parent.name} / {lock.parent.name}",
                subtitle=f"stale .gislock · pid {pid} is gone",
                detail=f"rm '{lock}'   # or start grass with -f",
                state="blocked",
                started=mtime,
                updated=mtime,
                icon="mm-lock-symbolic",
            ))
        if old:
            out.append(Task(
                id="grass-stale:old",
                group="grass",
                title=f"{len(old)} older stale .gislock file{'s' if len(old) > 1 else ''}",
                subtitle="mapsets left locked by sessions that died",
                detail="  ".join(
                    f"{l.parent.parent.name}/{l.parent.name}" for l, _, _ in old[:8]
                ) + (" …" if len(old) > 8 else ""),
                state="idle",
                started=min(s[2] for s in old),
                icon="mm-lock-symbolic",
            ))
        return out


def _short(path):
    home = str(Path.home())
    return "~" + path[len(home):] if path.startswith(home) else path
