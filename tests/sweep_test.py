"""Tests that finished slots are cleared on close and during long sessions.

SPDX-License-Identifier: Unlicense
"""

import time
import types

import pytest

from metamonitor import spool
from metamonitor.collectors.slots import LINGER


@pytest.fixture(autouse=True)
def isolated_spool(tmp_path, monkeypatch):
    monkeypatch.setenv("METAMONITOR_SPOOL", str(tmp_path / "slots"))
    return tmp_path / "slots"


def test_sweep_removes_finished_and_keeps_live_work():
    spool.write_slot("built", name="Build", state="done", percent=100)
    spool.write_slot("broke", name="Upload", state="failed")
    spool.write_slot("busy", name="Resample", state="running", percent=40)
    spool.write_slot("stuck", name="Fetch", state="blocked")
    spool.write_slot("waiting", name="Session", state="idle")
    assert spool.sweep_finished() == 2
    assert sorted(s["id"] for s in spool.list_slots()) == ["busy", "stuck", "waiting"]


def test_sweep_on_close_takes_even_the_slots_still_on_screen():
    spool.write_slot("just-now", name="Export", state="done", percent=100)
    # Finished a moment ago, so still within its linger on the dashboard.
    assert spool.sweep_finished() == 1
    assert spool.list_slots() == []


def test_periodic_sweep_spares_what_is_still_shown():
    spool.write_slot("just-now", name="Export", state="done", percent=100)
    spool.write_slot("long-gone", name="Import", state="done", percent=100)
    spool.write_slot("long-gone", finished=time.time() - LINGER - 60)
    assert spool.sweep_finished(older_than=LINGER) == 1
    assert [s["id"] for s in spool.list_slots()] == ["just-now"]


def test_sweep_survives_a_slot_that_vanishes_under_it(monkeypatch):
    spool.write_slot("gone", name="Copy", state="done", percent=100)
    monkeypatch.setattr(spool, "remove_slot", lambda sid: False)
    assert spool.sweep_finished() == 0


def _session(ran_for, last_sweep=None):
    """An application stand-in for the pacing logic, with no GTK involved."""
    calls = []
    return types.SimpleNamespace(
        _session_start=time.monotonic() - ran_for,
        _last_sweep=last_sweep,
        collectors=[(object(), 1)],
        sweep_finished=lambda older_than=0.0: calls.append(older_than) or 0,
        _poll_one=lambda collector: None,
        calls=calls,
    )


def test_no_sweep_in_the_first_hour():
    from metamonitor.app import MetaMonitor

    app = _session(ran_for=600)
    MetaMonitor._sweep_periodically(app)
    assert app.calls == [] and app._last_sweep is None


def test_sweeps_once_the_session_passes_an_hour():
    from metamonitor.app import MetaMonitor

    app = _session(ran_for=3700)
    MetaMonitor._sweep_periodically(app)
    assert app.calls == [LINGER] and app._last_sweep is not None


def test_sweep_waits_its_interval_before_running_again():
    from metamonitor.app import MetaMonitor

    app = _session(ran_for=7200, last_sweep=time.monotonic() - 60)
    MetaMonitor._sweep_periodically(app)
    assert app.calls == []
    app._last_sweep = time.monotonic() - 1000
    MetaMonitor._sweep_periodically(app)
    assert app.calls == [LINGER]
