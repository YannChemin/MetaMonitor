"""Desktop applications reporting progress through the Unity LauncherEntry API.

Many GTK/Qt/Electron/SDL applications (Firefox and Chromium downloads,
Nautilus/Files copies via Dash-to-Dock-aware code, Transmission, Steam,
KDE apps...) broadcast ``com.canonical.Unity.LauncherEntry.Update(s app_uri,
a{sv} props)`` on the session bus with ``progress`` (0..1) and
``progress-visible``.  It is a broadcast signal, so listening is enough.

SPDX-License-Identifier: Unlicense
"""

import time

from gi.repository import Gio

from ..model import Task

EXPIRE = 30  # Seconds without update before an app entry is considered gone.
FORGET = 3600  # Seconds before such an entry is dropped from memory.


class UnityCollector:
    source = "unity"

    def __init__(self):
        self.entries = {}
        try:
            self.bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
        except Exception:
            self.bus = None
            return
        self.bus.signal_subscribe(
            None, "com.canonical.Unity.LauncherEntry", "Update", None, None,
            Gio.DBusSignalFlags.NONE, self._on_update,
        )

    def _on_update(self, conn, sender, path, iface, signal, params):
        uri, props = params.unpack()
        desktop_id = uri.removeprefix("application://")
        entry = self.entries.setdefault(
            desktop_id, {"started": time.time(), "progress": None, "visible": False}
        )
        if "progress" in props:
            entry["progress"] = float(props["progress"])
        if "progress-visible" in props:
            entry["visible"] = bool(props["progress-visible"])
        if "count-visible" in props:
            entry["count"] = props.get("count") if props["count-visible"] else None
        entry["updated"] = time.time()

    def poll(self):
        now = time.time()
        out = []
        for desktop_id, e in list(self.entries.items()):
            age = now - e.get("updated", 0)
            if not e["visible"] or age > EXPIRE:
                # An app that quit or crashed mid-progress never sends the
                # update that hides its bar: forget it after FORGET.
                if not e["visible"] or age > FORGET:
                    del self.entries[desktop_id]
                continue
            name, icon = desktop_id, "application-x-executable-symbolic"
            try:
                info = Gio.DesktopAppInfo.new(desktop_id)
            except TypeError:
                info = None
            if info:
                name = info.get_display_name()
                gicon = info.get_icon()
                if gicon is not None:
                    icon = gicon.to_string()
            pct = None if e["progress"] is None else 100.0 * e["progress"]
            count = e.get("count")
            out.append(
                Task(
                    id=f"unity:{desktop_id}",
                    group="apps",
                    title=name,
                    subtitle=desktop_id + (f" · {count} items" if count else ""),
                    percent=pct,
                    state="running",
                    started=e["started"],
                    icon=icon,
                )
            )
        return out
