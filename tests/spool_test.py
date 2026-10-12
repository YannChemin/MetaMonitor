"""Tests for the slot spool, mmctl and mmrun.

SPDX-License-Identifier: Unlicense
"""

import json
import sys

import pytest

from metamonitor import cli, spool
from metamonitor.collectors.slots import SlotCollector


@pytest.fixture(autouse=True)
def isolated_spool(tmp_path, monkeypatch):
    monkeypatch.setenv("METAMONITOR_SPOOL", str(tmp_path / "slots"))
    return tmp_path / "slots"


def test_plain_number_file_is_a_slot(isolated_spool):
    (spool.spool_dir() / "backup").write_text("37.5\n")
    (slot,) = spool.list_slots()
    assert slot["name"] == "backup"
    assert slot["percent"] == 37.5
    assert slot["state"] == "running"


def test_number_with_detail(isolated_spool):
    (spool.spool_dir() / "job").write_text("12 % copying tiles")
    (slot,) = spool.list_slots()
    assert (slot["percent"], slot["detail"]) == (12.0, "copying tiles")


def test_mmctl_set_of_total_and_done():
    cli.mmctl(["set", "lut", "120", "--of", "400", "-n", "LUT build", "-d", "band 120"])
    (slot,) = spool.list_slots()
    assert slot["percent"] == pytest.approx(30.0)
    assert slot["name"] == "LUT build"
    cli.mmctl(["done", "lut"])
    (slot,) = spool.list_slots()
    assert (slot["state"], slot["percent"]) == ("done", 100.0)
    assert "finished" in slot


def test_dead_pid_marks_slot_failed():
    spool.write_slot("orphan", percent=10.0, state="running", pid=2**22 + 12345)
    (task,) = SlotCollector().poll()
    assert task.state == "failed"


def test_garbage_is_ignored(isolated_spool):
    (spool.spool_dir() / "junk").write_text("hello")
    (spool.spool_dir() / "badjson").write_text(json.dumps({"percent": "x"}))
    slots = {s["id"]: s for s in spool.list_slots()}
    assert "junk" not in slots
    assert slots["badjson"]["percent"] is None


def test_mmrun_parses_child_progress(capfd):
    code = cli.mmrun([
        "--id", "t", "--", sys.executable, "-c",
        "import sys,time\n"
        "for i in (10, 50, 90):\n"
        "    sys.stdout.write(f'\\rwork {i}%'); sys.stdout.flush(); time.sleep(0.3)\n"
        "sys.exit(3)",
    ])
    assert code == 3
    (slot,) = spool.list_slots()
    assert slot["state"] == "failed"
    assert slot["exit_code"] == 3
    assert slot["percent"] == 90.0
    assert "work 90%" in capfd.readouterr().out


def test_shell_init_skips_unsafe_names():
    text = cli.shell_init(["rsync", "rm -rf /;", "gdal_translate"])
    assert "rsync()" in text and "gdal_translate()" in text
    assert "rm -rf" not in text
