"""GRASS GIS integration: the progress shim hook and mapset lock reading.

The hook is one ``export LD_PRELOAD=<libmmgrass.so>`` line in the GRASS
env file (``~/.grass8/bashrc``).  GRASS's start-up script loads that file
for every kind of session (interactive shell, ``--exec``, GUI), so every
module started from it reports its G_percent() progress to MetaMonitor.

SPDX-License-Identifier: Unlicense
"""

import json
import os
import re
import struct
from pathlib import Path

BEGIN = "# >>> metamonitor >>>"
END = "# <<< metamonitor <<<"
SHIM = "libmmgrass.so"
CONFIG = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "metamonitor"


def shim_path():
    """Return the installed or freshly built shim, or None."""
    env = os.environ.get("METAMONITOR_GRASS_SHIM")
    here = Path(__file__).resolve()
    candidates = [Path(env)] if env else []
    candidates += [
        here.parents[1] / "grass" / SHIM,  # Source tree.
        here.parents[3] / "lib" / "metamonitor" / SHIM,  # <prefix>/share/metamonitor/...
        Path("/usr/lib/metamonitor") / SHIM,
    ]
    return next((p for p in candidates if p.is_file()), None)


def config_dirs():
    """GRASS user configuration directories to hook (``~/.grass8``...)."""
    base = Path(os.environ.get("GRASS_CONFIG_DIR") or Path.home())
    found = sorted(
        p for p in base.glob(".grass*")
        if p.is_dir() and re.fullmatch(r"\.grass(\d+)", p.name)
        and int(p.name[6:]) >= 8
    )
    return found or [base / ".grass8"]


def _env_files():
    names = ["bashrc"]
    if os.environ.get("SHELL", "").endswith("zsh"):
        names.append("zshrc")
    return [d / n for d in config_dirs() for n in names]


def _strip_block(text):
    return re.sub(rf"\n?{re.escape(BEGIN)}.*?{re.escape(END)}\n?", "\n", text, flags=re.S)


def hook_status():
    """Return a list of (env file, hooked shim path or None)."""
    out = []
    for f in _env_files():
        try:
            text = f.read_text()
        except OSError:
            text = ""
        m = re.search(rf"{re.escape(BEGIN)}\nexport LD_PRELOAD=(\S+)\n{re.escape(END)}", text)
        out.append((f, m.group(1) if m else None))
    return out


def enable_hook():
    """Write (or refresh) the hook block in every GRASS env file.

    :return: list of files changed
    :raises FileNotFoundError: when the shim is not installed
    """
    shim = shim_path()
    if not shim:
        raise FileNotFoundError(f"{SHIM} not found; build it with `make`.")
    block = f"{BEGIN}\nexport LD_PRELOAD={shim}\n{END}\n"
    changed = []
    for f, current in hook_status():
        if current == str(shim):
            continue
        f.parent.mkdir(parents=True, exist_ok=True)
        try:
            text = f.read_text()
        except OSError:
            text = ""
        text = _strip_block(text).rstrip("\n")
        f.write_text((text + "\n\n" if text else "") + block)
        changed.append(f)
    return changed


def disable_hook():
    changed = []
    for f, current in hook_status():
        if current is None:
            continue
        text = _strip_block(f.read_text()).rstrip("\n")
        f.write_text(text + "\n" if text else "")
        changed.append(f)
    return changed


def load_settings():
    try:
        return json.loads((CONFIG / "settings.json").read_text())
    except (OSError, ValueError):
        return {}


def save_settings(settings):
    CONFIG.mkdir(parents=True, exist_ok=True)
    (CONFIG / "settings.json").write_text(json.dumps(settings, indent=2) + "\n")


def ensure_default_hook():
    """Enable the hook unless the user turned it off; return changed files."""
    if not load_settings().get("grass_hook", True) or not shim_path():
        return []
    try:
        return enable_hook()
    except OSError:
        return []


def read_lock(path):
    """Return the pid stored in a ``.gislock`` file, or None.

    GRASS's ``etc/lock`` writes the pid as a native ``int``; accept text too.
    """
    try:
        raw = Path(path).read_bytes()
    except OSError:
        return None
    if len(raw) == 4:
        return struct.unpack("=i", raw)[0]
    m = re.match(rb"\s*(\d+)", raw)
    return int(m.group(1)) if m else None


def read_gisrc(path):
    """Parse a GISRC file into a dict (GISDBASE, LOCATION_NAME, MAPSET...)."""
    out = {}
    try:
        for line in Path(path).read_text().splitlines():
            key, sep, val = line.partition(":")
            if sep:
                out[key.strip()] = val.strip()
    except OSError:
        pass
    return out


def gisdbases():
    """Database directories worth scanning for ``.gislock`` files."""
    dbs = {Path.home() / "grassdata"}
    for d in config_dirs():
        db = read_gisrc(d / "rc").get("GISDBASE")
        if db:
            dbs.add(Path(db))
    for extra in os.environ.get("METAMONITOR_GRASS_DBASES", "").split(":"):
        if extra:
            dbs.add(Path(extra))
    return {d for d in dbs if d.is_dir()}
