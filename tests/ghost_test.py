"""Tests that dead tasks do not linger as ghosts.

SPDX-License-Identifier: Unlicense
"""

import json
import os
import subprocess
import time

import pytest

from metamonitor import spool
from metamonitor.collectors import claude
from metamonitor.collectors.slots import SlotCollector


@pytest.fixture
def claude_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(claude, "CLAUDE_DIR", tmp_path)
    monkeypatch.setattr(claude, "TMP_ROOT", tmp_path / "tmp")
    monkeypatch.setenv("METAMONITOR_SPOOL", str(tmp_path / "slots"))
    (tmp_path / "jobs" / "abc12345").mkdir(parents=True)
    (tmp_path / "daemon").mkdir()
    (tmp_path / "sessions").mkdir()
    return tmp_path


def _job(root, updated):
    stamp = time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime(updated))
    (root / "jobs" / "abc12345" / "state.json").write_text(json.dumps({
        "state": "working", "name": "phase 2", "daemonShort": "abc12345",
        "sessionId": "abc12345-0000", "cwd": "/tmp", "createdAt": stamp,
        "updatedAt": stamp,
    }))


def _roster(root, pid, proc_start):
    (root / "daemon" / "roster.json").write_text(json.dumps({"workers": {
        "abc12345": {"pid": pid, "procStart": str(proc_start),
                     "sessionId": "abc12345-0000"},
    }}))


def test_proc_start_matches_own_process():
    assert spool.proc_start_ticks(os.getpid()) is not None
    assert spool.same_process(os.getpid(), spool.proc_start_ticks(os.getpid()))
    assert not spool.same_process(os.getpid(), "1")
    assert not spool.same_process(2**22 + 12345)


def test_job_with_live_worker_runs(claude_dir):
    _job(claude_dir, time.time() - 3600)
    _roster(claude_dir, os.getpid(), spool.proc_start_ticks(os.getpid()))
    (task,) = claude.ClaudeCollector().poll()
    assert task.state == "running"


def test_job_whose_daemon_died_is_dropped(claude_dir):
    # The ghost: state.json still says "working" two weeks after the daemon died.
    _job(claude_dir, time.time() - 14 * 86400)
    _roster(claude_dir, 2**22 + 12345, 1)
    assert claude.ClaudeCollector().poll() == []


def test_job_whose_worker_pid_was_reused_is_dropped(claude_dir):
    _job(claude_dir, time.time() - 14 * 86400)
    _roster(claude_dir, os.getpid(), 1)  # Live PID, but another process.
    assert claude.ClaudeCollector().poll() == []


def test_job_that_just_died_shows_failed(claude_dir):
    _job(claude_dir, time.time() - 300)
    (task,) = claude.ClaudeCollector().poll()
    assert task.state == "failed"
    assert "daemon worker gone" in task.detail


def test_new_job_before_roster_update_runs(claude_dir):
    _job(claude_dir, time.time())
    (task,) = claude.ClaudeCollector().poll()
    assert task.state == "running"


def test_session_with_reused_pid_is_dropped(claude_dir):
    (claude_dir / "sessions" / "1.json").write_text(json.dumps({
        "pid": os.getpid(), "sessionId": "s1", "procStart": "1", "status": "busy",
    }))
    assert claude.ClaudeCollector().poll() == []


def test_ended_session_transcript_is_forgotten(claude_dir):
    proj = claude_dir / "projects" / "p"
    proj.mkdir(parents=True)
    (proj / "s1.jsonl").write_text("{}\n")
    sess = claude_dir / "sessions" / "1.json"
    sess.write_text(json.dumps({
        "pid": os.getpid(), "sessionId": "s1",
        "procStart": str(spool.proc_start_ticks(os.getpid())), "status": "idle",
    }))
    col = claude.ClaudeCollector()
    col.poll()
    assert col.transcripts and col.paths
    sess.unlink()
    col.poll()
    assert not col.transcripts and not col.paths


def test_slot_with_reused_pid_fails(tmp_path, monkeypatch):
    monkeypatch.setenv("METAMONITOR_SPOOL", str(tmp_path / "slots"))
    child = subprocess.Popen(["sleep", "30"])
    try:
        # Written an hour ago for a PID that a new process now holds.
        spool.write_slot("old", percent=10.0, state="running", pid=child.pid)
        path = spool.spool_dir() / "old"
        slot = json.loads(path.read_text())
        slot["updated"] = time.time() - 3600
        path.write_text(json.dumps(slot))
        # Judged dead, and long past the finished-slot linger: not shown at
        # all, where a bare PID check kept it running.
        assert SlotCollector().poll() == []
    finally:
        child.kill()
        child.wait()


def test_relaunched_slot_keeps_running(tmp_path, monkeypatch):
    monkeypatch.setenv("METAMONITOR_SPOOL", str(tmp_path / "slots"))
    spool.write_slot("queue", percent=5.0, state="running", pid=2**22 + 12345)
    child = subprocess.Popen(["sleep", "30"])
    try:
        # Same slot id, new worker: the old "started" must not condemn it.
        spool.write_slot("queue", percent=6.0, state="running", pid=child.pid)
        (task,) = SlotCollector().poll()
        assert task.state == "running"
    finally:
        child.kill()
        child.wait()
