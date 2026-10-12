"""Tests for progress extraction from CLI output.

SPDX-License-Identifier: Unlicense
"""

import pytest

from metamonitor.parse import ProgressParser, percent_in_line


@pytest.mark.parametrize(
    ("line", "expected"),
    [
        (" 42%|████▏     | 42/100 [00:01<00:01, 41.9it/s]", 42.0),  # tqdm
        ("     12,345,678  37%   11.73MB/s    0:00:05 (xfr#1)", 37.0),  # rsync
        ("GRASS_INFO_PERCENT: 73", 73.0),
        ("0...10...20...30...40", 40.0),  # GDAL
        ("0...10...20...30....", 30.0),
        ("Input file size is 1242, 1500", None),
        ("0...10...20...30...40...50...60...70...80...90...100 - done.", 100.0),
        ("[ 12/340] Compiling foo.c", pytest.approx(100 * 12 / 340)),
        ("Step 3/7 : RUN make", pytest.approx(100 * 3 / 7)),
        ("Get:5 http://deb.debian.org 45.2 %", 45.2),
        ("written 2026/10/12 in /home/x/y", None),  # Dates and paths are not progress.
        ("version 1.2/3", None),
        ("nothing here", None),
        ("rate 250% faster", None),
    ],
)
def test_percent_in_line(line, expected):
    assert percent_in_line(line) == expected


def test_carriage_return_progress_without_newline():
    p = ProgressParser()
    p.feed(b"Downloading  10%\rDownloading  55%")
    assert p.progress.percent == 55.0
    assert p.progress.detail == "Downloading  55%"


def test_grass_g_percent_backspaces():
    p = ProgressParser()
    p.feed("Reading raster map...\n   4%\b\b\b\b   8%\b\b\b\b  12%\b\b\b\b")
    assert p.progress.percent == 12.0


def test_osc_9_4_sequences():
    p = ProgressParser()
    p.feed("\x1b]9;4;1;30\x07building")
    assert (p.progress.percent, p.progress.state) == (30.0, "running")
    p.feed("\x1b]9;4;3\x1b\\")
    assert p.progress.percent is None
    p.feed("\x1b]9;4;2;80\x07")
    assert (p.progress.percent, p.progress.state) == (80.0, "failed")


def test_ansi_colours_are_stripped():
    p = ProgressParser()
    p.feed("\x1b[32m 67%\x1b[0m done so far\n")
    assert p.progress.percent == 67.0
    assert "\x1b" not in p.progress.detail


def test_split_utf8_and_chunks():
    p = ProgressParser()
    p.feed(b" 9")
    p.feed(b"1% \xe2\x96")
    p.feed(b"\x88\n")
    assert p.progress.percent == 91.0


def test_numeric_mode_for_pv_and_gauges():
    p = ProgressParser(numeric=True)
    p.feed("12\n47\n")
    assert p.progress.percent == 47.0
    q = ProgressParser()
    q.feed("12\n47\n")
    assert q.progress.percent is None
