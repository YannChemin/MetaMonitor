"""The ``~/.bashrc`` hook: auto-enrol progress-printing commands.

The hook is a marked block that evaluates ``mmctl shell-init``, which
defines shell functions running rsync, wget, gdal_translate... through
``mmrun`` whenever their output is a terminal.  It is installed by
``make install`` and on MetaMonitor's first start, and can be turned off
from the menu or with ``mmctl bash disable``.

SPDX-License-Identifier: Unlicense
"""

import os
import re
from pathlib import Path

from .grass import load_settings, save_settings

BEGIN = "# >>> metamonitor >>>"
END = "# <<< metamonitor <<<"
BLOCK = (
    f"{BEGIN}\n"
    "# Commands that print progress report it to MetaMonitor (see `mmctl shell-init`).\n"
    'if command -v mmctl >/dev/null 2>&1; then eval "$(mmctl shell-init)"; fi\n'
    f"{END}\n"
)


def rc_files():
    files = [Path.home() / ".bashrc"]
    if os.environ.get("SHELL", "").endswith("zsh") or (Path.home() / ".zshrc").exists():
        files.append(Path.home() / ".zshrc")
    return files


def _strip(text):
    return re.sub(rf"\n?{re.escape(BEGIN)}.*?{re.escape(END)}\n?", "\n", text, flags=re.S)


def status():
    out = []
    for f in rc_files():
        try:
            out.append((f, BLOCK in f.read_text()))
        except OSError:
            out.append((f, False))
    return out


def enable():
    changed = []
    for f, hooked in status():
        if hooked:
            continue
        try:
            text = f.read_text()
        except OSError:
            text = ""
        text = _strip(text).rstrip("\n")
        f.write_text((text + "\n\n" if text else "") + BLOCK)
        changed.append(f)
    return changed


def disable():
    changed = []
    for f, hooked in status():
        try:
            text = f.read_text()
        except OSError:
            continue
        if BEGIN not in text:
            continue
        text = _strip(text).rstrip("\n")
        f.write_text(text + "\n" if text else "")
        changed.append(f)
    return changed


def ensure_default():
    """Install the hook unless the user turned it off; return changed files."""
    if not load_settings().get("bash_hook", True):
        return []
    try:
        return enable()
    except OSError:
        return []


def set_enabled(on):
    settings = load_settings()
    settings["bash_hook"] = on
    save_settings(settings)
    return enable() if on else disable()
