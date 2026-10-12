"""Zero-setup progress for file-crunching commands, read from /proc.

Like Xfennec's ``progress`` tool: for a known command (cp, dd, tar, rsync,
gzip, sha256sum...) the file offset of its largest open input, divided by
that file's size, is how far it got.  Works for commands started without
any wrapper, in any terminal.

SPDX-License-Identifier: Unlicense
"""

import os
import time

from ..model import Task

COMMANDS = {
    "cp", "mv", "dd", "cat", "tar", "gzip", "gunzip", "pigz", "bzip2", "pbzip2",
    "xz", "unxz", "zstd", "lz4", "unzip", "7z", "7za", "zip", "rsync", "scp",
    "sftp", "md5sum", "sha1sum", "sha256sum", "sha512sum", "b2sum", "cksum",
    "pv", "split", "shred", "openssl", "gpg", "ffmpeg", "rclone", "aws",
    "gsutil", "split", "convert", "magick", "gdal_translate", "gdalwarp",
    "ogr2ogr", "nccopy", "h5repack", "sqlite3", "pg_restore", "psql",
}
MIN_SIZE = 8 * 1024 * 1024  # Ignore inputs below 8 MiB: too quick to matter.


def _extra_commands():
    env = os.environ.get("METAMONITOR_PROC_COMMANDS", "")
    return {c for c in env.replace(",", " ").split() if c}


def _comm(pid):
    try:
        with open(f"/proc/{pid}/comm") as fh:
            return fh.read().strip()
    except OSError:
        return None


def _best_input(pid):
    """Return (path, pos, size) of the largest regular file opened for reading."""
    best = None
    fd_dir = f"/proc/{pid}/fd"
    try:
        fds = os.listdir(fd_dir)
    except OSError:
        return None
    for fd in fds:
        try:
            target = os.readlink(f"{fd_dir}/{fd}")
            if not target.startswith("/") or target.startswith(("/dev/", "/proc/")):
                continue
            st = os.stat(f"{fd_dir}/{fd}")
            if not (st.st_mode & 0o170000 == 0o100000) or st.st_size < MIN_SIZE:
                continue
            with open(f"/proc/{pid}/fdinfo/{fd}") as fh:
                info = dict(
                    line.split(":", 1) for line in fh.read().splitlines() if ":" in line
                )
            flags = int(info.get("flags", "0").strip(), 8)
            if flags & 0o3 != 0:  # Inputs only: a file being written has pos == size.
                continue
            pos = int(info.get("pos", "0").strip())
        except (OSError, ValueError):
            continue
        if best is None or st.st_size > best[2]:
            best = (target, pos, st.st_size)
    return best


class ProcCollector:
    source = "procfs"

    def __init__(self):
        self.uid = os.getuid()
        self.commands = COMMANDS | _extra_commands()

    def poll(self):
        out = []
        me = os.getpid()
        for entry in os.scandir("/proc"):
            if not entry.name.isdigit() or int(entry.name) == me:
                continue
            try:
                if entry.stat().st_uid != self.uid:
                    continue
            except OSError:
                continue
            pid = int(entry.name)
            comm = _comm(pid)
            if comm not in self.commands:
                continue
            best = _best_input(pid)
            if not best:
                continue
            path, pos, size = best
            try:
                started = os.stat(f"/proc/{pid}").st_mtime
            except OSError:
                started = time.time()
            out.append(
                Task(
                    id=f"proc:{pid}",
                    group="files",
                    title=f"{comm} {os.path.basename(path)}",
                    subtitle=f"pid {pid} · {_human(pos)} of {_human(size)}",
                    detail=path,
                    percent=min(100.0, 100.0 * pos / size),
                    state="running",
                    started=started,
                    icon="mm-files-symbolic",
                )
            )
        return out


def _human(n):
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if n < 1024 or unit == "TiB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
