"""Application entry point.

SPDX-License-Identifier: Unlicense
"""

import json
import os
import sys
import time
import traceback
from pathlib import Path

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gdk, Gio, GLib, Gtk  # noqa: E402

from . import APP_ID, __version__, bashrc, brand, grass, spool  # noqa: E402
from .collectors.claude import ClaudeCollector  # noqa: E402
from .collectors.grass import GrassCollector  # noqa: E402
from .collectors.procfs import ProcCollector  # noqa: E402
from .collectors.slots import SlotCollector  # noqa: E402
from .collectors.unity import UnityCollector  # noqa: E402
from .model import ACTIVE, GROUPS, Store  # noqa: E402

NOTIFY_MIN_RUNTIME = 60  # Only notify for tasks that ran at least this long.


class MetaMonitor(Adw.Application):
    def __init__(self):
        super().__init__(application_id=APP_ID, flags=Gio.ApplicationFlags.DEFAULT_FLAGS)
        self.add_main_option(
            "background", ord("b"), GLib.OptionFlags.NONE, GLib.OptionArg.NONE,
            "Collect progress without opening a window (for the top-bar indicator)", None,
        )
        self.add_main_option(
            "guide", ord("g"), GLib.OptionFlags.NONE, GLib.OptionArg.STRING,
            "Open the help on PAGE: bash, grass, python, claude or formats", "PAGE",
        )
        self._pending_help = None
        self.store = Store()
        self.store.connect(self._on_store)
        self._states = {}
        self._held = False
        self.collectors = []
        self.notify = True
        self.window = None
        self._tick = 0
        self._spool_monitor = None

    def do_startup(self):
        Adw.Application.do_startup(self)
        Adw.StyleManager.get_default().set_color_scheme(Adw.ColorScheme.FORCE_DARK)
        brand.add_icon_paths(Gdk.Display.get_default())
        Gtk.Window.set_default_icon_name(APP_ID)
        css = Gtk.CssProvider()
        css.load_from_path(str(Path(__file__).parent / "ui" / "style.css"))
        Gtk.StyleContext.add_provider_for_display(
            Gdk.Display.get_default(), css, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
        )
        for name, cb, accel in (
            ("quit", lambda *_: self.quit(), "<primary>q"),
            ("clear", self._clear, "<primary>l"),
            ("about", self._about, None),
        ):
            act = Gio.SimpleAction.new(name, None)
            act.connect("activate", cb)
            self.add_action(act)
            if accel:
                self.set_accels_for_action(f"app.{name}", [accel])
        act = Gio.SimpleAction.new_stateful("notify", None, GLib.Variant.new_boolean(True))
        act.connect("change-state", self._on_notify)
        self.add_action(act)
        hooked = grass.load_settings().get("grass_hook", True) and grass.shim_path() is not None
        act = Gio.SimpleAction.new_stateful("grass-hook", None, GLib.Variant.new_boolean(hooked))
        act.connect("change-state", self._on_grass_hook)
        self.add_action(act)
        on = bashrc.load_settings().get("bash_hook", True)
        act = Gio.SimpleAction.new_stateful("bash-hook", None, GLib.Variant.new_boolean(on))
        act.connect("change-state", self._on_bash_hook)
        self.add_action(act)
        keep = grass.load_settings().get("keep_running", False)
        act = Gio.SimpleAction.new_stateful("keep-running", None, GLib.Variant.new_boolean(keep))
        act.connect("change-state", self._on_keep_running)
        self.add_action(act)
        if keep:
            self._hold()
        act = Gio.SimpleAction.new("show", None)
        act.connect("activate", lambda *_: self.activate())
        self.add_action(act)
        act = Gio.SimpleAction.new("help", GLib.VariantType.new("s"))
        act.connect("activate", self._help)
        self.add_action(act)
        # Both enrolment hooks are on by default; each can be turned off.
        self.hook_changes = bashrc.ensure_default() + grass.ensure_default_hook()

        self.collectors = [
            (SlotCollector(), 1),
            (ClaudeCollector(), 2),
            (GrassCollector(), 2),
            (ProcCollector(), 2),
            (UnityCollector(), 2),
        ]
        # Slots also refresh instantly when a file changes in the spool.
        self._spool_monitor = Gio.File.new_for_path(str(spool.spool_dir())).monitor_directory(
            Gio.FileMonitorFlags.NONE, None
        )
        self._spool_monitor.set_rate_limit(100)
        self._spool_monitor.connect("changed", lambda *_: self._poll_one(self.collectors[0][0]))
        GLib.timeout_add(500, self._poll)

    def do_handle_local_options(self, options):
        guide = options.lookup_value("guide", GLib.VariantType.new("s"))
        if guide is not None:
            self.register(None)
            if self.get_is_remote():
                self.activate()
                self.activate_action("help", guide)
                return 0
            self._pending_help = guide.get_string()
            return -1
        if options.contains("background"):
            self.register(None)
            if self.get_is_remote():
                return 0  # Already running: nothing to do.
            # Keep running with no window; the activation that follows a
            # -1 return is swallowed in do_activate.
            self._hold()
            self._quiet_start = True
        return -1

    def _hold(self):
        if not self._held:
            self.hold()
            self._held = True

    def do_activate(self):
        if getattr(self, "_quiet_start", False):
            self._quiet_start = False
            return
        if self.window is None:
            from .ui.window import MainWindow

            self.window = MainWindow(self, self.store)
            self._poll(force=True)
            if self.hook_changes:
                self.window.toast(
                    "Enrolment hooks added to "
                    + ", ".join(str(f).replace(str(Path.home()), "~") for f in self.hook_changes)
                    + ". New shells and GRASS sessions report progress."
                )
        self.window.present()
        if self._pending_help:
            self._help(None, GLib.Variant.new_string(self._pending_help))
            self._pending_help = None

    def _poll_one(self, collector):
        try:
            self.store.replace(collector.source, collector.poll())
        except Exception:
            traceback.print_exc()

    def _poll(self, force=False):
        self._tick += 1
        for collector, every in self.collectors:
            if force or self._tick % every == 0:
                self._poll_one(collector)
        if self._tick % 2 == 0:
            self._write_summary()
        return True

    def _write_summary(self):
        """Publish a digest for the GNOME Shell top-bar indicator."""
        tasks = list(self.store.tasks.values())
        active = [t for t in tasks if t.state in ACTIVE]
        known = [t.percent for t in active if t.percent is not None]
        order = list(GROUPS)
        active.sort(key=lambda t: (t.parent is not None, order.index(t.group), -t.started))
        summary = {
            "updated": time.time(),
            "pid": os.getpid(),
            "active": len(active),
            "tracked": len(tasks),
            "mean": sum(known) / len(known) if known else None,
            "tasks": [
                {"title": t.title, "group": t.group, "state": t.state,
                 "percent": t.percent, "detail": t.detail[:80], "child": t.parent is not None}
                for t in active[:12]
            ],
        }
        path = spool.spool_dir().parent / "summary.json"
        tmp = path.with_name(".summary.json.tmp")
        try:
            tmp.write_text(json.dumps(summary))
            os.replace(tmp, path)
        except OSError:
            pass

    def _on_store(self, added, changed, removed):
        for t in changed:
            prev = self._states.get(t.id)
            if prev in ACTIVE and t.state in ("done", "failed", "idle"):
                self.task_finished(t, prev)
        for t in added + changed:
            self._states[t.id] = t.state
        for tid in removed:
            self._states.pop(tid, None)

    def _on_keep_running(self, action, value):
        action.set_state(value)
        settings = grass.load_settings()
        settings["keep_running"] = value.get_boolean()
        grass.save_settings(settings)
        if value.get_boolean():
            self._hold()
        elif self._held:
            self.release()
            self._held = False

    def task_finished(self, task, prev):
        if not self.notify or time.time() - task.started < NOTIFY_MIN_RUNTIME:
            return
        if task.group == "claude" and not task.parent and task.state == "idle":
            title = f"Claude is waiting: {task.title}"
        elif task.state == "idle":
            return
        else:
            title = f"{'Finished' if task.state == 'done' else 'Failed'}: {task.title}"
        n = Gio.Notification.new(title)
        n.set_icon(Gio.ThemedIcon.new(APP_ID))
        n.set_default_action("app.show")
        n.set_body(task.detail or task.subtitle)
        n.set_priority(
            Gio.NotificationPriority.HIGH if task.state == "failed"
            else Gio.NotificationPriority.NORMAL
        )
        self.send_notification(task.id, n)

    def _on_notify(self, action, value):
        action.set_state(value)
        self.notify = value.get_boolean()

    def _on_grass_hook(self, action, value):
        on = value.get_boolean()
        settings = grass.load_settings()
        settings["grass_hook"] = on
        grass.save_settings(settings)
        try:
            files = grass.enable_hook() if on else grass.disable_hook()
        except FileNotFoundError as e:
            self.window.toast(str(e))
            return
        action.set_state(value)
        if files and self.window:
            verb = "added to" if on else "removed from"
            self.window.toast(f"GRASS hook {verb} " + ", ".join(
                str(f).replace(str(Path.home()), "~") for f in files)
                + ". New GRASS sessions pick it up.")

    def _on_bash_hook(self, action, value):
        action.set_state(value)
        on = value.get_boolean()
        files = bashrc.set_enabled(on)
        if files and self.window:
            verb = "added to" if on else "removed from"
            self.window.toast(f"Shell hook {verb} " + ", ".join(
                str(f).replace(str(Path.home()), "~") for f in files)
                + ". Open a new terminal to use it.")

    def _help(self, action, param):
        from .ui.help import help_dialog

        help_dialog(param.get_string()).present(self.window)

    def _clear(self, *_):
        for s in spool.list_slots():
            if s["state"] in ("done", "failed"):
                spool.remove_slot(s["id"])
        self._poll(force=True)

    def _about(self, *_):
        about = Adw.AboutDialog(
            application_name="MetaMonitor",
            application_icon=APP_ID,
            version=__version__,
            developer_name="Yann Chemin",
            license_type=Gtk.License.CUSTOM,
            license="Released into the public domain (The Unlicense).",
            comments=(
                "Live progress for Claude Code sessions, wrapped CLIs, "
                "file copies and desktop applications."
            ),
        )
        about.present(self.window)


def main():
    brand.load_fonts()  # Before GTK creates its font map.
    return MetaMonitor().run(sys.argv)
