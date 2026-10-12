"""Tests for the ~/.bashrc enrolment hook.

SPDX-License-Identifier: Unlicense
"""

import subprocess
from pathlib import Path

import pytest

from metamonitor import bashrc

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("SHELL", "/bin/bash")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / ".config"))
    monkeypatch.setattr("metamonitor.grass.CONFIG", tmp_path / ".config" / "metamonitor")
    (tmp_path / ".bashrc").write_text("alias ll='ls -l'\n")
    return tmp_path


def test_enable_disable_roundtrip(home):
    rc = home / ".bashrc"
    assert bashrc.enable() == [rc]
    assert bashrc.enable() == []
    assert rc.read_text().startswith("alias ll='ls -l'\n\n# >>> metamonitor >>>")
    assert bashrc.status() == [(rc, True)]
    assert bashrc.disable() == [rc]
    assert rc.read_text() == "alias ll='ls -l'\n"


def test_turning_off_is_remembered(home):
    bashrc.set_enabled(False)
    assert bashrc.ensure_default() == []
    bashrc.set_enabled(True)
    assert bashrc.status()[0][1]


def test_hook_defines_wrappers_in_bash(home):
    bashrc.enable()
    out = subprocess.run(
        ["bash", "-c", f"PATH={ROOT / 'bin'}:$PATH; source ~/.bashrc; type -t rsync"],
        capture_output=True, text=True, env={"HOME": str(home), "PATH": "/usr/bin:/bin"},
    )
    assert out.stdout.strip() == "function", out.stderr
