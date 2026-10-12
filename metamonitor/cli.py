"""Command-line front ends: ``mmctl`` (manage slots) and ``mmrun`` (wrap a CLI).

SPDX-License-Identifier: Unlicense
"""

import argparse
import errno
import fcntl
import os
import pty
import select
import signal
import sys
import termios
import time
import tty

from . import spool
from .parse import ProgressParser


class SlotWriter:
    """Push parser results into a slot, at most a few times per second."""

    def __init__(self, sid, name, source, pid=None, interval=0.25):
        self.sid = sid
        self.interval = interval
        self.last = 0.0
        self.sent = None
        spool.write_slot(
            sid, name=name, source=source, pid=pid, state="running", percent=None,
            detail="", started=time.time(),
        )

    def push(self, progress, force=False):
        now = time.time()
        key = (progress.percent, progress.state, progress.detail)
        if key == self.sent:
            return
        if not force and now - self.last < self.interval and progress.state == self.sent[1]:
            return
        spool.write_slot(
            self.sid, percent=progress.percent, state=progress.state,
            detail=progress.detail,
        )
        self.sent = key
        self.last = now

    def finish(self, code):
        state = "done" if code == 0 else "failed"
        fields = {"state": state, "exit_code": code}
        if code == 0:
            fields["percent"] = 100.0
        spool.write_slot(self.sid, **fields)


def _default_id(name):
    return spool.slot_id(f"{name}-{os.getpid()}")


# mmrun -----------------------------------------------------------------------


def _copy_winsize(src_fd, dst_fd):
    try:
        size = fcntl.ioctl(src_fd, termios.TIOCGWINSZ, b"\0" * 8)
        fcntl.ioctl(dst_fd, termios.TIOCSWINSZ, size)
    except OSError:
        pass


def run_in_pty(argv, on_output):
    """Run ``argv`` on a pseudo-terminal, mirroring it to our terminal.

    The child believes it talks to a real terminal, so it keeps printing its
    usual in-place progress bars; every byte is shown to the user unchanged
    and also handed to ``on_output``.

    :return: exit code of the child (128 + signal when killed)
    """
    pid, master = pty.fork()
    if pid == 0:
        try:
            os.execvp(argv[0], argv)
        except OSError as e:
            sys.stderr.write(f"mmrun: {argv[0]}: {e.strerror}\n")
            os._exit(127)

    stdin_tty = os.isatty(0)
    if stdin_tty:
        _copy_winsize(0, master)
        signal.signal(signal.SIGWINCH, lambda *_: _copy_winsize(0, master))
        saved = termios.tcgetattr(0)
        tty.setraw(0)
    fds = [master] + ([0] if stdin_tty else [])
    try:
        while True:
            try:
                ready, _, _ = select.select(fds, [], [])
            except InterruptedError:
                continue
            if master in ready:
                try:
                    data = os.read(master, 65536)
                except OSError as e:
                    if e.errno != errno.EIO:
                        raise
                    data = b""
                if not data:
                    break
                os.write(1, data)
                on_output(data)
            if 0 in ready:
                data = os.read(0, 4096)
                if not data:
                    fds.remove(0)
                else:
                    os.write(master, data)
    finally:
        if stdin_tty:
            termios.tcsetattr(0, termios.TCSAFLUSH, saved)
    _, status = os.waitpid(pid, 0)
    return os.waitstatus_to_exitcode(status) if status else 0


def mmrun(argv=None):
    ap = argparse.ArgumentParser(
        prog="mmrun",
        description="Run a command and publish the progress it prints to MetaMonitor.",
        usage="mmrun [-n NAME] [--id ID] [--] COMMAND [ARGS...]",
    )
    ap.add_argument("-n", "--name", help="label shown in MetaMonitor (default: the command)")
    ap.add_argument("--id", help="slot identifier (default: name-pid)")
    ap.add_argument("--keep", action="store_true", help="leave the slot after exit")
    ap.add_argument("command", nargs=argparse.REMAINDER)
    args = ap.parse_args(argv)
    cmd = args.command[1:] if args.command[:1] == ["--"] else args.command
    if not cmd:
        ap.error("no command given")
    name = args.name or " ".join(cmd)[:80]
    sid = args.id or _default_id(os.path.basename(cmd[0]))

    parser = ProgressParser()
    writer = SlotWriter(sid, name, "mmrun", pid=os.getpid())

    def on_output(data):
        if parser.feed(data):
            writer.push(parser.progress)

    code = run_in_pty(cmd, on_output)
    writer.push(parser.progress, force=True)
    writer.finish(code)
    return code


# mmctl -----------------------------------------------------------------------

WRAP_DEFAULT = [
    "rsync", "wget", "gdal_translate", "gdalwarp", "gdaladdo", "gdal_rasterize",
    "ogr2ogr", "pip", "pip3", "uv", "huggingface-cli", "rclone", "aria2c",
]


def shell_init(commands):
    """Shell functions that run ``commands`` through mmrun on a terminal.

    Wrapping only happens when stdout is a terminal, so pipes, command
    substitutions and scripts keep the original command untouched.
    """
    out = ["# MetaMonitor auto-enrolment; generated by `mmctl shell-init`."]
    for cmd in commands:
        if not all(c.isalnum() or c in "._-" for c in cmd):
            continue
        out.append(
            f'{cmd}() {{ if [ -t 1 ] && command -v mmrun >/dev/null; then '
            f'mmrun -n "{cmd} $*" -- {cmd} "$@"; else command {cmd} "$@"; fi; }}'
        )
    return "\n".join(out)


def _follow(fd_or_path, parser, writer, tee, pid=None, from_start=False):
    if isinstance(fd_or_path, int):
        while True:
            data = os.read(fd_or_path, 65536)
            if not data:
                return 0
            if tee:
                os.write(1, data)
            if parser.feed(data):
                writer.push(parser.progress)
    with open(fd_or_path, "rb") as fh:
        if not from_start:
            fh.seek(0, os.SEEK_END)
        while True:
            data = fh.read(65536)
            if data:
                if tee:
                    os.write(1, data)
                if parser.feed(data):
                    writer.push(parser.progress)
                continue
            if pid and not spool.pid_alive(pid):
                return 0
            time.sleep(0.3)


def _print_slots():
    rows = spool.list_slots()
    if not rows:
        print("No slots.")
        return
    for s in rows:
        pct = "  ?  " if s.get("percent") is None else f"{s['percent']:5.1f}%"
        print(f"{s['id']:<32} {pct} {s['state']:<8} {s.get('name', '')}  {s.get('detail', '')}")


def mmctl(argv=None):
    ap = argparse.ArgumentParser(
        prog="mmctl", description="Publish progress slots read by MetaMonitor."
    )
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("set", help="create or update a slot")
    p.add_argument("id")
    p.add_argument("percent", nargs="?", type=float, help="0-100; omit for indeterminate")
    p.add_argument("-n", "--name")
    p.add_argument("-d", "--detail")
    p.add_argument("-s", "--state", choices=spool.STATES)
    p.add_argument("--pid", type=int, help="process to watch; the slot fails if it dies")
    p.add_argument("--of", type=float, metavar="TOTAL",
                   help="treat PERCENT as a count out of TOTAL")

    for name, state in (("done", "done"), ("fail", "failed"), ("pause", "paused")):
        p = sub.add_parser(name, help=f"mark a slot {state}")
        p.add_argument("id")
        p.add_argument("-d", "--detail")

    p = sub.add_parser("rm", help="remove slots")
    p.add_argument("ids", nargs="+")

    sub.add_parser("clear", help="remove finished slots")
    sub.add_parser("list", help="list slots")
    sub.add_parser("path", help="print the spool directory")
    p = sub.add_parser("bash", help="shell hook in ~/.bashrc wrapping progress-printing CLIs")
    p.add_argument("action", choices=("status", "enable", "disable"))
    p = sub.add_parser("grass", help="GRASS GIS progress hook (~/.grass8/bashrc)")
    p.add_argument("action", choices=("status", "enable", "disable"))
    p = sub.add_parser(
        "shell-init",
        help="print bash/zsh functions wrapping progress-printing commands with mmrun",
    )
    p.add_argument("commands", nargs="*", help=f"default: $METAMONITOR_WRAP or {' '.join(WRAP_DEFAULT)}")

    p = sub.add_parser("pipe", help="parse progress from stdin (cmd 2>&1 | mmctl pipe ID)")
    p.add_argument("id")
    p.add_argument("-n", "--name")
    p.add_argument("-q", "--quiet", action="store_true", help="do not echo stdin")
    p.add_argument("-N", "--numeric", action="store_true",
                   help="lines holding only a number are percentages (pv -n, gauges)")

    p = sub.add_parser("tail", help="follow a log file and parse its progress")
    p.add_argument("id")
    p.add_argument("file")
    p.add_argument("-n", "--name")
    p.add_argument("--pid", type=int, help="stop when this process exits")
    p.add_argument("--from-start", action="store_true")
    p.add_argument("-q", "--quiet", action="store_true")

    a = ap.parse_args(argv)

    if a.cmd == "set":
        pct = a.percent
        if pct is not None and a.of:
            pct = 100.0 * pct / a.of
        spool.write_slot(
            a.id, name=a.name, detail=a.detail, pid=a.pid,
            state=a.state or ("done" if pct is not None and pct >= 100 else "running"),
            percent=pct, source="mmctl",
        )
    elif a.cmd in ("done", "fail", "pause"):
        state = {"done": "done", "fail": "failed", "pause": "paused"}[a.cmd]
        fields = {"state": state, "detail": a.detail}
        if state == "done":
            fields["percent"] = 100.0
        spool.write_slot(a.id, **fields)
    elif a.cmd == "rm":
        for sid in a.ids:
            spool.remove_slot(sid)
    elif a.cmd == "clear":
        for s in spool.list_slots():
            if s["state"] in ("done", "failed"):
                spool.remove_slot(s["id"])
    elif a.cmd == "list":
        _print_slots()
    elif a.cmd == "path":
        print(spool.spool_dir())
    elif a.cmd == "grass":
        return _grass(a.action)
    elif a.cmd == "bash":
        from . import bashrc

        if a.action != "status":
            for f in bashrc.set_enabled(a.action == "enable"):
                print(f"{'hooked' if a.action == 'enable' else 'unhooked'}: {f}")
        for f, on in bashrc.status():
            print(f"{f}: {'hooked' if on else 'not hooked'}")
    elif a.cmd == "shell-init":
        print(shell_init(a.commands or os.environ.get("METAMONITOR_WRAP", "").split()
                         or WRAP_DEFAULT))
    elif a.cmd in ("pipe", "tail"):
        parser = ProgressParser(numeric=getattr(a, "numeric", False))
        source = "pipe" if a.cmd == "pipe" else "tail"
        name = a.name or (a.id if a.cmd == "pipe" else os.path.basename(a.file))
        writer = SlotWriter(a.id, name, source, pid=getattr(a, "pid", None) or os.getpid())
        try:
            if a.cmd == "pipe":
                _follow(0, parser, writer, not a.quiet)
            else:
                _follow(a.file, parser, writer, not a.quiet, a.pid, a.from_start)
        except KeyboardInterrupt:
            pass
        writer.push(parser.progress, force=True)
        writer.finish(0 if (parser.progress.state != "failed") else 1)
    return 0


def _grass(action):
    from . import grass

    if action != "status":
        settings = grass.load_settings()
        settings["grass_hook"] = action == "enable"
        grass.save_settings(settings)
        try:
            files = grass.enable_hook() if action == "enable" else grass.disable_hook()
        except FileNotFoundError as e:
            sys.stderr.write(f"mmctl: {e}\n")
            return 1
        for f in files:
            print(f"{'hooked' if action == 'enable' else 'unhooked'}: {f}")
    print(f"shim: {grass.shim_path() or 'not found'}")
    for f, shim in grass.hook_status():
        print(f"{f}: {'LD_PRELOAD=' + shim if shim else 'not hooked'}")
    return 0


def main_mmctl():
    sys.exit(mmctl())


def main_mmrun():
    sys.exit(mmrun())
