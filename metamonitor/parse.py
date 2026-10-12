"""Extract a completion percentage from arbitrary CLI output.

The parser is fed raw bytes or text as they arrive (pty, pipe or log file)
and keeps the most recent progress it recognised.  Recognised forms, in order
of trust:

* ConEmu/Windows Terminal ``OSC 9;4`` progress sequences (emitted by systemd,
  cargo, pytest, winget, Ghostty-aware tools...);
* GRASS GIS ``GRASS_INFO_PERCENT: 42`` (``GRASS_MESSAGE_FORMAT=gui``);
* plain percentages: ``42%``, ``42.5 %`` (tqdm, G_percent, wget, curl, rsync,
  pip, apt, dd, ffmpeg-style wrappers...);
* GDAL-style dotted counters: ``0...10...20...30``;
* ``N/M`` counters (``[ 12/340]``, ``Step 3/7``), ignored when they look like
  a date or a path.

SPDX-License-Identifier: Unlicense
"""

import re
from dataclasses import dataclass

OSC94_RE = re.compile(r"\x1b\]9;4;(\d)(?:;(\d{1,3}))?(?:\x07|\x1b\\)")
ANSI_RE = re.compile(
    r"\x1b\[[0-?]*[ -/]*[@-~]"  # CSI
    r"|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)"  # OSC
    r"|\x1b[@-Z\\-_]"  # two-byte escapes
)
GRASS_RE = re.compile(r"GRASS_INFO_PERCENT:\s*(\d{1,3})")
PERCENT_RE = re.compile(r"(?<![\w.])(\d{1,3}(?:[.,]\d+)?)\s?%")
GDAL_RE = re.compile(r"\d{1,3}(?:\.{2,}\d{1,3})*\.{2,}\d{0,3}")
GDAL_DONE_RE = re.compile(r"\b100\s*-\s*done\b")
FRACTION_RE = re.compile(r"(?<![\d/.:-])(\d{1,9})\s*/\s*(\d{1,9})(?![\d/.:-])")

# OSC 9;4 states: 0 remove, 1 normal, 2 error, 3 indeterminate, 4 paused.
OSC_STATES = {0: "done", 1: "running", 2: "failed", 3: "running", 4: "paused"}


@dataclass
class Progress:
    percent: float | None = None
    state: str = "running"
    detail: str = ""


def _clean(segment):
    return ANSI_RE.sub("", segment).replace("\x00", "").strip()


def percent_in_line(line):
    """Return the percentage found in one output line, or None.

    :param line: one line of output, escape sequences already removed
    :return: float in [0, 100] or None
    """
    m = GRASS_RE.search(line)
    if m:
        return min(float(m.group(1)), 100.0)
    found = [
        float(v.replace(",", "."))
        for v in PERCENT_RE.findall(line)
        if float(v.replace(",", ".")) <= 100.0
    ]
    if found:
        # tqdm and friends print the overall figure first; a trailing
        # percentage is usually a rate or a sub-figure.
        return found[0]
    if GDAL_DONE_RE.search(line):
        return 100.0
    m = GDAL_RE.search(line)
    if m:
        nums = [int(v) for v in re.findall(r"\d+", m.group(0)) if int(v) <= 100]
        if nums:
            return float(nums[-1])
    for num, den in FRACTION_RE.findall(line):
        num, den = int(num), int(den)
        if 2 <= den and num <= den:
            return 100.0 * num / den
    return None


BARE_RE = re.compile(r"^\s*(\d{1,3}(?:\.\d+)?)\s*$")


class ProgressParser:
    """Incremental parser fed with chunks of terminal output.

    :param numeric: also take a line holding only a number as a percentage
                    (``pv -n``, dialog/whiptail gauges)
    """

    def __init__(self, numeric=False):
        self.numeric = numeric
        self.progress = Progress()
        self._partial = ""
        self.changed = False

    def feed(self, data):
        """Consume a chunk; return True when the progress changed.

        :param data: bytes or str
        """
        if isinstance(data, bytes):
            data = data.decode("utf-8", "replace")
        text = self._partial + data
        before = (self.progress.percent, self.progress.state, self.progress.detail)

        for m in OSC94_RE.finditer(text):
            st = int(m.group(1))
            self.progress.state = OSC_STATES.get(st, "running")
            if st in (1, 2, 4) and m.group(2) is not None:
                self.progress.percent = min(float(m.group(2)), 100.0)
            elif st == 3:
                self.progress.percent = None
            elif st == 0:
                self.progress.state = "running"
        text = OSC94_RE.sub("", text)

        # Progress lines are rewritten in place with \r or \b, so every one
        # of those is a segment boundary just like \n.
        segments = re.split(r"[\r\n\b]+", text)
        self._partial = segments.pop()[-4096:]
        for seg in segments:
            self._line(seg)
        # A progress line rewritten with \r has no terminator yet; peek at it.
        if self._partial:
            line = _clean(self._partial)
            if line and percent_in_line(line) is not None:
                self._line(self._partial)

        self.changed = before != (
            self.progress.percent,
            self.progress.state,
            self.progress.detail,
        )
        return self.changed

    def _line(self, seg):
        line = _clean(seg)
        if not line:
            return
        pct = percent_in_line(line)
        if pct is None and self.numeric:
            m = BARE_RE.match(line)
            if m and float(m.group(1)) <= 100:
                pct = float(m.group(1))
        if pct is not None:
            self.progress.percent = pct
        if len(line) > 2:
            self.progress.detail = line[:160]
