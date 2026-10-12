"""Brand assets: palette, bundled fonts and icons.

The palette is the single source of truth for the neon colours used by the
widgets; ``ui/style.css`` mirrors it as ``@define-color`` tokens.

SPDX-License-Identifier: Unlicense
"""

import ctypes
import ctypes.util
from pathlib import Path

DATA = Path(__file__).resolve().parents[1] / "data"

# Night City on a terminal: acid yellow, cyan, hot magenta on near-black.
YELLOW = "#fcee0a"
CYAN = "#00f0ff"
MAGENTA = "#ff2a6d"
RED = "#ff003c"
VIOLET = "#b026ff"
BLUE = "#2f6bff"
ORANGE = "#ff9f1c"
GREEN = "#39ff88"
TEAL = "#00e5c7"
LIME = "#7cff4f"
VOID = "#07080e"
PANEL = "#10121c"
TEXT = "#e6f1ff"
DIM = "#7a86a8"


def load_fonts():
    """Make the bundled Orbitron and Rajdhani visible to this process only.

    Uses fontconfig's application font set, so nothing is installed system
    wide.  Silently does nothing if fontconfig cannot be loaded.
    """
    fonts = DATA / "fonts"
    lib = ctypes.util.find_library("fontconfig")
    if not lib or not fonts.is_dir():
        return False
    try:
        fc = ctypes.CDLL(lib)
        fc.FcConfigAppFontAddDir.argtypes = [ctypes.c_void_p, ctypes.c_char_p]
        return bool(fc.FcConfigAppFontAddDir(None, str(fonts).encode()))
    except (OSError, AttributeError):
        return False


def add_icon_paths(display):
    """Let GTK find the app's own icons when running from any location."""
    from gi.repository import Gtk

    theme = Gtk.IconTheme.get_for_display(display)
    for sub in ("icons", "icons/symbolic"):
        path = DATA / sub
        if path.is_dir():
            theme.add_search_path(str(path))
