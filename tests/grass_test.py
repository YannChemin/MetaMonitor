"""Tests for the GRASS GIS shim, hook and lock handling.

The shim tests build a stand-in for libgrass_gis, so GRASS itself is not
needed; the last test runs real modules when ``grass`` is installed.

SPDX-License-Identifier: Unlicense
"""

import json
import os
import shutil
import struct
import subprocess
from pathlib import Path

import pytest

from metamonitor import grass

ROOT = Path(__file__).resolve().parents[1]
CC = shutil.which("cc") or shutil.which("gcc")

FAKE_LIB = r"""
#include <stdarg.h>
#include <stdio.h>
#include <stdlib.h>
void G_percent(long n, long d, int s) { fprintf(stderr, "real %ld/%ld\n", n, d); }
void G_progress(long n, int s) { fprintf(stderr, "count %ld\n", n); }
void G_fatal_error(const char *msg, ...)
{
    va_list ap;
    va_start(ap, msg);
    fputs("REAL ERROR: ", stderr);
    vfprintf(stderr, msg, ap);
    va_end(ap);
    exit(1);
}
"""

FAKE_MODULE = r"""
#include <stdlib.h>
#include <string.h>
#include <unistd.h>
void G_percent(long, long, int);
void G_fatal_error(const char *, ...);
int main(int argc, char **argv)
{
    usleep(1100000);  /* Longer than the shim's first-slot delay. */
    for (long i = 0; i <= 40; i++) {
        G_percent(i, 40, 5);
        usleep(10000);
    }
    if (argc > 1 && strcmp(argv[1], "fail") == 0)
        G_fatal_error("Raster map <%s> not found", "dem");
    if (argc > 1 && strcmp(argv[1], "fork") == 0 && fork() == 0)
        return 0;  /* A forked child must not mark the slot done. */
    return 0;
}
"""


@pytest.fixture(scope="module")
def built(tmp_path_factory):
    if not CC:
        pytest.skip("no C compiler")
    d = tmp_path_factory.mktemp("build")
    shim = d / "libmmgrass.so"
    subprocess.run([CC, "-O2", "-fPIC", "-shared", "-o", shim, ROOT / "grass" / "mmgrass.c",
                    "-ldl", "-lpthread"], check=True)
    (d / "fake.c").write_text(FAKE_LIB)
    (d / "mod.c").write_text(FAKE_MODULE)
    subprocess.run([CC, "-fPIC", "-shared", "-o", d / "libgrass_gis.so", d / "fake.c"],
                   check=True)
    subprocess.run([CC, "-o", d / "r.fake", d / "mod.c", f"-L{d}", "-lgrass_gis",
                    f"-Wl,-rpath,{d}"], check=True)
    return d, shim


def _run(built, tmp_path, *args):
    d, shim = built
    spool = tmp_path / "spool"
    env = dict(os.environ, LD_PRELOAD=str(shim), METAMONITOR_SPOOL=str(spool),
               GIS_LOCK="4242")
    proc = subprocess.run([d / "r.fake", *args], env=env, capture_output=True, text=True)
    slots = [json.loads(p.read_text()) for p in spool.glob("grass-*")]
    return proc, slots


def test_shim_forwards_and_publishes(built, tmp_path):
    proc, slots = _run(built, tmp_path)
    assert proc.returncode == 0
    assert "real 40/40" in proc.stderr  # The real G_percent still runs.
    (slot,) = slots
    assert slot["name"] == "r.fake"
    assert (slot["state"], slot["percent"]) == ("done", 100)
    assert slot["parent"] == "grass:4242"
    assert slot["group"] == "grass"


def test_shim_reports_fatal_error(built, tmp_path):
    proc, slots = _run(built, tmp_path, "fail")
    assert proc.returncode == 1
    assert "REAL ERROR: Raster map <dem> not found" in proc.stderr
    (slot,) = slots
    assert slot["state"] == "failed"
    assert slot["detail"] == "ERROR: Raster map <dem> not found"


def test_forked_child_does_not_finish_slot(built, tmp_path):
    proc, slots = _run(built, tmp_path, "fork")
    assert proc.returncode == 0
    (slot,) = slots
    assert slot["state"] == "done"


def test_read_lock_binary_and_text(tmp_path):
    (tmp_path / "a").write_bytes(struct.pack("=i", 9684))
    (tmp_path / "b").write_text("1234\n")
    assert grass.read_lock(tmp_path / "a") == 9684
    assert grass.read_lock(tmp_path / "b") == 1234
    assert grass.read_lock(tmp_path / "missing") is None


def test_hook_enable_is_idempotent_and_reversible(tmp_path, monkeypatch, built):
    _, shim = built
    monkeypatch.setenv("GRASS_CONFIG_DIR", str(tmp_path))
    monkeypatch.setenv("METAMONITOR_GRASS_SHIM", str(shim))
    monkeypatch.setenv("SHELL", "/bin/bash")
    rc = tmp_path / ".grass8" / "bashrc"
    rc.parent.mkdir()
    rc.write_text("export GRASS_PAGER=less\n")
    assert grass.enable_hook() == [rc]
    assert grass.enable_hook() == []
    text = rc.read_text()
    assert text.startswith("export GRASS_PAGER=less\n")
    assert f"export LD_PRELOAD={shim}\n" in text
    assert grass.hook_status() == [(rc, str(shim))]
    assert grass.disable_hook() == [rc]
    assert rc.read_text() == "export GRASS_PAGER=less\n"


@pytest.mark.skipif(not shutil.which("grass"), reason="GRASS GIS not installed")
def test_real_grass_session(built, tmp_path):
    _, shim = built
    cfg = tmp_path / "cfg" / ".grass8"
    cfg.mkdir(parents=True)
    (cfg / "bashrc").write_text(f"export LD_PRELOAD={shim}\n")
    spool = tmp_path / "spool"
    env = dict(os.environ, GRASS_CONFIG_DIR=str(cfg.parent), METAMONITOR_SPOOL=str(spool))
    # Big enough to outlast the shim's 1 s first-slot delay on an idle
    # machine (6000 x 6000 took 0.8 s, 9000 x 9000 takes 1.7 s).
    subprocess.run(
        ["grass", "--tmp-project", "XY", "--exec", "bash", "-c",
         "g.region rows=9000 cols=9000 && r.surf.random output=r min=0 max=1 --quiet"],
        env=env, check=True, capture_output=True,
    )
    slots = [json.loads(p.read_text()) for p in spool.glob("grass-*")]
    assert any(s["name"] == "r.surf.random" and s["state"] == "done" for s in slots)
