"""Claude Code sessions, background jobs and their background shell tasks.

Sources, all written by Claude Code itself (nothing to install):

* ``~/.claude/sessions/<pid>.json``: one per live CLI session, with its
  name, working directory and ``status`` (busy, idle, ...);
* ``~/.claude/projects/*/<sessionId>.jsonl``: the transcript, read
  incrementally, for the current tool call, the last prompt, the session
  title and the task list (``task_reminder`` attachments);
* ``~/.claude/jobs/<id>/state.json``: background/daemon sessions, alive only
  while ``~/.claude/daemon/roster.json`` lists a running worker for them
  (``state.json`` keeps its last state when the daemon dies);
* ``/tmp/claude-<uid>/*/<sessionId>/tasks/*.output``: output of
  ``run_in_background`` shell commands, parsed for percentages like any CLI.

SPDX-License-Identifier: Unlicense
"""

import glob
import json
import os
import re
import time
from datetime import datetime
from pathlib import Path

from .. import spool
from ..model import Task
from ..parse import ProgressParser

CLAUDE_DIR = Path(os.environ.get("CLAUDE_CONFIG_DIR", Path.home() / ".claude"))
TMP_ROOT = Path(f"/tmp/claude-{os.getuid()}")
EXIT_RE = re.compile(rb"\[(?:exited with code (-?\d+)|(killed))\]\s*$")
TASK_LINGER = 120
TASK_MAX_AGE = 6 * 3600
JOB_LINGER = 1800
JOB_GRACE = 60  # A new job may appear before the daemon lists its worker.
TOOL_MEMORY = 512  # Tool calls remembered per transcript, to name bg shells.

RUNNING = {"busy", "running", "shell", "compacting", "thinking", "working", "tool"}
BLOCKED = {"blocked", "permission", "waiting_permission", "needs_input", "awaiting_input"}


def _home(path):
    home = str(Path.home())
    return "~" + path[len(home):] if path and path.startswith(home) else (path or "")


def _ts(value):
    """Accept epoch ms, epoch s or ISO 8601; return epoch seconds."""
    if isinstance(value, (int, float)):
        return value / 1000.0 if value > 1e11 else float(value)
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
        except ValueError:
            pass
    return time.time()


def _describe_tool(name, inp):
    if not isinstance(inp, dict):
        return name
    for key in ("description", "prompt", "command", "file_path", "pattern", "query",
                "url", "skill", "path"):
        val = inp.get(key)
        if isinstance(val, str) and val.strip():
            val = val.strip().splitlines()[0]
            if key == "file_path":
                val = os.path.basename(val)
            return f"{name}: {val[:110]}"
    return name


class Transcript:
    """Incremental reader of one session transcript (JSON lines)."""

    def __init__(self, path):
        self.path = path
        self.offset = 0
        self.title = ""
        self.prompt = ""
        self.activity = ""
        self.activity_at = 0.0
        self.tools = {}  # tool_use_id -> description
        self.bg_names = {}  # background task id -> description
        self.todo = None  # (completed, total, current)
        self.out_tokens = 0
        self.turn_tools = 0

    def update(self):
        try:
            size = os.path.getsize(self.path)
        except OSError:
            return
        if size < self.offset:
            self.offset = 0
        if size == self.offset:
            return
        if self.offset == 0 and size > 4_000_000:
            self.offset = size - 4_000_000
        with open(self.path, "rb") as fh:
            fh.seek(self.offset)
            data = fh.read()
        end = data.rfind(b"\n")
        if end < 0:
            return
        self.offset += end + 1
        for raw in data[: end + 1].splitlines():
            try:
                rec = json.loads(raw)
            except ValueError:
                continue
            self._record(rec)

    def _record(self, rec):
        kind = rec.get("type")
        if kind == "ai-title":
            self.title = rec.get("aiTitle") or self.title
        elif kind == "last-prompt":
            self.prompt = rec.get("lastPrompt") or self.prompt
        elif kind == "attachment":
            att = rec.get("attachment") or {}
            if att.get("type") == "task_reminder" and isinstance(att.get("content"), list):
                self._todos(att["content"])
        elif kind == "assistant":
            msg = rec.get("message") or {}
            usage = msg.get("usage") or {}
            self.out_tokens += usage.get("output_tokens", 0) or 0
            for block in msg.get("content") or []:
                if isinstance(block, dict) and block.get("type") == "tool_use":
                    desc = _describe_tool(block.get("name", "tool"), block.get("input"))
                    self.tools[block.get("id")] = desc
                    if len(self.tools) > TOOL_MEMORY:
                        # Dicts keep insertion order: drop the oldest calls.
                        for key in list(self.tools)[: len(self.tools) - TOOL_MEMORY]:
                            del self.tools[key]
                    self.activity = desc
                    self.activity_at = _ts(rec.get("timestamp"))
                    self.turn_tools += 1
                    if block.get("name") in ("TodoWrite",):
                        self._todos((block.get("input") or {}).get("todos") or [])
        elif kind == "user":
            msg = rec.get("message") or {}
            content = msg.get("content")
            if isinstance(content, str) or (
                isinstance(content, list)
                and any(isinstance(b, dict) and b.get("type") == "text" for b in content)
                and not rec.get("toolUseResult")
            ):
                if not rec.get("isMeta"):
                    self.turn_tools = 0
            res = rec.get("toolUseResult")
            if isinstance(res, dict) and isinstance(content, list):
                tuid = next(
                    (b.get("tool_use_id") for b in content
                     if isinstance(b, dict) and b.get("type") == "tool_result"),
                    None,
                )
                for key, val in res.items():
                    if key.lower().endswith("id") and isinstance(val, str) and len(val) < 40:
                        self.bg_names.setdefault(val, self.tools.get(tuid, ""))
                if len(self.bg_names) > TOOL_MEMORY:
                    for key in list(self.bg_names)[: len(self.bg_names) - TOOL_MEMORY]:
                        del self.bg_names[key]

    def _todos(self, items):
        items = [i for i in items if isinstance(i, dict)]
        if not items:
            self.todo = None
            return
        done = sum(1 for i in items if i.get("status") == "completed")
        cur = next(
            (i.get("activeForm") or i.get("subject") or i.get("content") or ""
             for i in items if i.get("status") == "in_progress"),
            "",
        )
        self.todo = (done, len(items), cur)


class ClaudeCollector:
    source = "claude"

    def __init__(self):
        self.transcripts = {}
        self.paths = {}

    def _transcript(self, sid):
        if sid not in self.paths:
            hits = glob.glob(str(CLAUDE_DIR / "projects" / "*" / f"{sid}.jsonl"))
            if not hits:
                return None
            self.paths[sid] = hits[0]
        path = self.paths[sid]
        tr = self.transcripts.get(path)
        if tr is None:
            tr = self.transcripts[path] = Transcript(path)
        tr.update()
        return tr

    def poll(self):
        now = time.time()
        out = []
        seen = set()
        for f in glob.glob(str(CLAUDE_DIR / "sessions" / "*.json")):
            try:
                s = json.loads(Path(f).read_text())
            except (OSError, ValueError):
                continue
            pid, sid = s.get("pid"), s.get("sessionId")
            # procStart guards against a dead session's PID now used by
            # another process.
            if not sid or not pid or not spool.same_process(pid, s.get("procStart")):
                continue
            seen.add(sid)
            out.extend(self._session(s, now))
        workers = _live_workers()
        for f in glob.glob(str(CLAUDE_DIR / "jobs" / "*" / "state.json")):
            try:
                j = json.loads(Path(f).read_text())
            except (OSError, ValueError):
                continue
            if j.get("sessionId") in seen:
                continue
            out.extend(self._job(j, now, workers))
        self._prune(seen)
        return out

    def _prune(self, seen):
        """Forget the transcripts of sessions that ended (long runs)."""
        for sid in [sid for sid in self.paths if sid not in seen]:
            self.transcripts.pop(self.paths.pop(sid), None)

    def _session(self, s, now):
        sid = s["sessionId"]
        status = str(s.get("status", "")).lower()
        if status in RUNNING:
            state = "running"
        elif status in BLOCKED or "permission" in status:
            state = "blocked"
        else:
            state = "idle"
        tr = self._transcript(sid)
        since = _ts(s.get("statusUpdatedAt") or s.get("updatedAt") or now)
        elapsed = _fmt_dur(now - since)
        title = s.get("name") or (tr.title if tr else "") or sid[:8]
        sub = f"{_home(s.get('cwd'))} · {status or 'unknown'} {elapsed}"
        detail = ""
        percent = None
        if tr:
            if tr.title and tr.title != title:
                sub = f"{tr.title} · {sub}"
            if state == "running":
                detail = tr.activity or "thinking…"
                if tr.turn_tools:
                    detail = f"{detail}  ·  step {tr.turn_tools}"
            else:
                detail = f"“{tr.prompt[:120]}”" if tr.prompt else ""
            if tr.todo:
                done, total, cur = tr.todo
                percent = 100.0 * done / total
                detail = f"{done}/{total} tasks · {cur or detail}"
        task = Task(
            id=f"claude:{sid}",
            group="claude",
            title=title,
            subtitle=sub,
            detail=detail,
            percent=percent,
            state=state,
            started=_ts(s.get("startedAt")),
            updated=since,
            icon="mm-agent-symbolic",
        )
        return [task] + self._bg_tasks(sid, task.id, tr, now)

    def _bg_tasks(self, sid, parent, tr, now):
        out = []
        for f in glob.glob(str(TMP_ROOT / "*" / sid / "tasks" / "*.output")):
            try:
                st = os.stat(f)
            except OSError:
                continue
            age = now - st.st_mtime
            if age > TASK_MAX_AGE:
                continue
            with open(f, "rb") as fh:
                fh.seek(max(0, st.st_size - 16384))
                tail = fh.read()
            m = EXIT_RE.search(tail)
            tid = Path(f).stem
            if m:
                if age > TASK_LINGER:
                    continue
                code = int(m.group(1)) if m.group(1) else -9
                state = "done" if code == 0 else "failed"
                tail = tail[: m.start()]
            else:
                state = "running"
            parser = ProgressParser()
            parser.feed(tail)
            parser.feed(b"\n")
            pct = parser.progress.percent
            if state == "done":
                pct = 100.0
            name = tr.bg_names.get(tid) if tr else ""
            if not name and tr and state == "running" and age < 60:
                name = tr.activity  # Started by the latest tool call, not yet answered.
            name = name or f"background task {tid}"
            out.append(
                Task(
                    id=f"claude-bg:{tid}",
                    group="claude",
                    title=name.removeprefix("Bash: "),
                    subtitle=f"background shell · {tid}"
                    + (f" · exit {code}" if m else f" · {_fmt_dur(age)} since output"),
                    detail=parser.progress.detail,
                    percent=pct,
                    state=state,
                    started=st.st_ctime,
                    updated=st.st_mtime,
                    parent=parent,
                    icon="mm-terminal-symbolic",
                )
            )
        return out

    def _job(self, j, now, workers):
        state = str(j.get("state", "")).lower()
        updated = _ts(j.get("updatedAt"))
        detail = j.get("detail") or ""
        ended = state in ("done", "stopped", "failed", "error", "killed")
        keys = {j.get("daemonShort"), j.get("sessionId")} - {None, ""}
        if not ended and not keys & workers and now - updated > JOB_GRACE:
            # The daemon died (crash, logout, reboot) without updating
            # state.json, which still says "working": the job is over.
            state, ended = "gone", True
            detail = f"daemon worker gone. {detail}".strip()
        if ended:
            if now - updated > JOB_LINGER:
                return []
            mapped = "done" if state in ("done", "stopped") else "failed"
        elif state in ("blocked", "waiting", "needs_input"):
            mapped = "blocked"
        else:
            mapped = "running"
        return [
            Task(
                id=f"claude-job:{j.get('daemonShort') or j.get('sessionId')}",
                group="claude",
                title=j.get("name") or j.get("intent", "")[:60] or "background job",
                subtitle=f"background job · {_home(j.get('cwd'))} · {state}",
                detail=detail,
                percent=100.0 if mapped == "done" else None,
                state=mapped,
                started=_ts(j.get("createdAt")),
                updated=updated,
                icon="mm-agent-symbolic",
            )
        ]


def _live_workers():
    """Short ids and session ids of the jobs the Claude daemon runs now."""
    try:
        roster = json.loads((CLAUDE_DIR / "daemon" / "roster.json").read_text())
        workers = roster.get("workers") or {}
    except (OSError, ValueError, AttributeError):
        return set()
    live = set()
    for short, w in workers.items():
        if isinstance(w, dict) and w.get("pid") and spool.same_process(
            w["pid"], w.get("procStart")
        ):
            live.update(k for k in (short, w.get("sessionId")) if k)
    return live


def _fmt_dur(sec):
    sec = max(0, int(sec))
    if sec < 60:
        return f"{sec}s"
    if sec < 3600:
        return f"{sec // 60}m{sec % 60:02d}s"
    if sec < 86400:
        return f"{sec // 3600}h{sec % 3600 // 60:02d}m"
    return f"{sec // 86400}d{sec % 86400 // 3600}h"
