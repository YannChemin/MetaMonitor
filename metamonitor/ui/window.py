"""Main window: overview strip plus one card per source group.

SPDX-License-Identifier: Unlicense
"""

import time

import gi

gi.require_version("Adw", "1")
from gi.repository import Adw, Gio, GLib, Gtk, Pango  # noqa: E402

from ..model import ACTIVE, GROUPS  # noqa: E402
from .widgets import GlowBar, HistoryChart, RingGauge  # noqa: E402

STATE_ORDER = {"running": 0, "blocked": 1, "paused": 2, "failed": 3, "done": 4, "idle": 5}


def fmt_eta(sec):
    if sec is None or sec > 86400 * 3:
        return ""
    sec = int(sec)
    if sec < 60:
        return f"{sec}s left"
    if sec < 3600:
        return f"{sec // 60}m{sec % 60:02d}s left"
    return f"{sec // 3600}h{sec % 3600 // 60:02d}m left"


class TaskRow(Gtk.Box):
    def __init__(self, task):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        self.task = task
        self.add_css_class("mm-row")
        if task.parent:
            self.add_css_class("child")
        _, _, c0, c1 = GROUPS[task.group]

        top = Gtk.Box(spacing=10)
        self.icon = Gtk.Image(pixel_size=18 if not task.parent else 14)
        texts = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, hexpand=True)
        self.title = Gtk.Label(xalign=0, css_classes=["mm-title"], max_width_chars=28,
                               ellipsize=Pango.EllipsizeMode.END)
        self.subtitle = Gtk.Label(xalign=0, css_classes=["mm-subtitle"], max_width_chars=40,
                                  ellipsize=Pango.EllipsizeMode.MIDDLE)
        texts.append(self.title)
        texts.append(self.subtitle)
        self.pill = Gtk.Label(css_classes=["mm-pill"], valign=Gtk.Align.CENTER)
        self.eta = Gtk.Label(xalign=1, css_classes=["mm-eta"], valign=Gtk.Align.CENTER)
        self.pct = Gtk.Label(xalign=1, css_classes=["mm-percent"], valign=Gtk.Align.CENTER)
        for w in (self.icon, texts, self.pill, self.eta, self.pct):
            top.append(w)

        self.bar = GlowBar(c0, c1, height=8 if task.parent else 10)
        self.detail = Gtk.Label(xalign=0, css_classes=["mm-detail"], max_width_chars=40,
                                ellipsize=Pango.EllipsizeMode.END)
        self.append(top)
        self.append(self.bar)
        self.append(self.detail)
        self.refresh()

    def refresh(self):
        t = self.task
        self.title.set_label(t.title)
        self.title.set_tooltip_text(t.title)
        self.subtitle.set_label(t.subtitle)
        self.detail.set_label(t.detail)
        self.detail.set_tooltip_text(t.detail or None)
        self.detail.set_visible(bool(t.detail))
        icon = t.icon or "content-loading-symbolic"
        if icon.startswith("/"):
            self.icon.set_from_file(icon)
        else:
            self.icon.set_from_icon_name(icon)
        for s in STATE_ORDER:
            self.remove_css_class(s)
            self.pill.remove_css_class(s)
        self.add_css_class(t.state)
        self.pill.add_css_class(t.state)
        self.pill.set_label(t.state.upper())
        self.pct.set_label("" if t.percent is None else f"{t.percent:5.1f}%")
        self.eta.set_label(fmt_eta(t.eta()))
        self.bar.set_progress(None if t.percent is None else t.percent / 100.0, t.state)


# Two card columns: the usually short job cards stacked on the left, the long
# Claude list on the right. A narrow window stacks the left column on top.
LEFT_CARDS = ["cli", "grass"] + [g for g in GROUPS if g not in ("cli", "grass", "claude")]
RIGHT_CARDS = ["claude"]


class GroupCard(Gtk.Box):
    def __init__(self, key):
        # Top-aligned so that a card keeps its own height instead of
        # stretching to fill its column.
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=2,
                         valign=Gtk.Align.START)
        self.add_css_class("mm-group")
        title, icon, c0, _ = GROUPS[key]
        head = Gtk.Box(spacing=8, margin_bottom=6)
        head.append(Gtk.Image(icon_name=icon, css_classes=["mm-group-icon"]))
        head.append(Gtk.Label(label=title.upper(), xalign=0, hexpand=True,
                              css_classes=["mm-group-title"]))
        self.count = Gtk.Label(css_classes=["mm-group-count"])
        head.append(self.count)
        self.append(head)
        self.rows = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self.append(self.rows)


class MainWindow(Adw.ApplicationWindow):
    def __init__(self, app, store):
        super().__init__(application=app, title="MetaMonitor")
        self.set_default_size(1180, 820)
        self.add_css_class("mm-window")
        self.store = store
        self.rows = {}
        self.cards = {}
        self.show_idle = True

        header = Adw.HeaderBar()
        title = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, valign=Gtk.Align.CENTER)
        wordmark = Gtk.Label(css_classes=["mm-wordmark"])
        wordmark.set_markup(
            '<span foreground="#00f0ff">META</span>'
            '<span foreground="#fcee0a">//</span>MONITOR'
        )
        self.window_title = Gtk.Label(css_classes=["mm-tagline"])
        title.append(wordmark)
        title.append(self.window_title)
        header.set_title_widget(title)
        header.pack_start(Gtk.Image(icon_name="io.github.yannchemin.MetaMonitor-symbolic"))

        menu = Gio.Menu()
        view_section = Gio.Menu()
        view_section.append("Show idle Claude sessions", "win.show-idle")
        view_section.append("Notify when tasks finish", "app.notify")
        view_section.append("Clear finished slots", "app.clear")
        view_section.append("Keep running when closed", "app.keep-running")
        menu.append_section(None, view_section)
        hooks = Gio.Menu()
        hooks.append("Wrap CLIs in ~/.bashrc", "app.bash-hook")
        hooks.append("Capture GRASS module progress", "app.grass-hook")
        menu.append_section("Enrolment", hooks)
        help_menu = Gio.Menu()
        for name, label in (("bash", "Bash"), ("grass", "GRASS GIS"), ("python", "Python"),
                            ("claude", "Claude Code"), ("formats", "Recognised formats")):
            help_menu.append(label, f"app.help::{name}")
        about = Gio.Menu()
        about.append_submenu("Help", help_menu)
        about.append("About MetaMonitor", "app.about")
        menu.append_section(None, about)
        header.pack_end(Gtk.MenuButton(icon_name="open-menu-symbolic", menu_model=menu))

        act = Gio.SimpleAction.new_stateful("show-idle", None, GLib.Variant.new_boolean(True))
        act.connect("change-state", self._on_show_idle)
        self.add_action(act)

        # Overview strip.
        self.g_active = RingGauge("ACTIVE", "#d97757", "#f4b183")
        self.g_mean = RingGauge("MEAN DONE", "#22d3ee", "#6366f1")
        self.g_done = RingGauge("FINISHED", "#22c55e", "#a3e635")
        self.chart = HistoryChart()
        legend = Gtk.Label(
            label="◼ active tasks   ◼ mean completion   · last 10 min",
            xalign=0, css_classes=["mm-chart-legend"],
        )
        chart_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4, hexpand=True)
        chart_box.append(self.chart)
        chart_box.append(legend)
        overview = Gtk.Box(spacing=18, css_classes=["mm-overview"])
        for w in (self.g_active, self.g_mean, self.g_done, chart_box):
            overview.append(w)

        self.left = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=14,
                            hexpand=True)
        self.right = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=14,
                             hexpand=True)
        # Homogeneous: equal column widths, and a lone visible column (the
        # other one hidden) takes the full width.
        self.columns = Gtk.Box(spacing=14, homogeneous=True, valign=Gtk.Align.START)
        self.columns.append(self.left)
        self.columns.append(self.right)
        self.empty = Adw.StatusPage(
            icon_name="emblem-synchronizing-symbolic",
            title="Nothing in progress",
            description=(
                "Claude Code sessions appear here on their own.\n"
                "Wrap any command:  <tt>mmrun -- rsync -a --info=progress2 src/ dst/</tt>\n"
                "Pipe its output:  <tt>cmd 2&gt;&amp;1 | mmctl pipe job</tt>\n"
                "Or push values:  <tt>mmctl set job 42 -n \"My job\"</tt>\n"
                "Menu → Help → Bash has the full guide."
            ),
        )
        self.empty.set_vexpand(True)

        body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=16,
                       margin_start=16, margin_end=16, margin_bottom=16)
        body.append(overview)
        body.append(self.columns)
        body.append(self.empty)
        clamp = Adw.Clamp(maximum_size=1600, tightening_threshold=1200, child=body)
        scroll = Gtk.ScrolledWindow(child=clamp, vexpand=True)
        scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)

        view = Adw.ToolbarView()
        view.add_top_bar(header)
        view.set_content(scroll)
        self.toasts = Adw.ToastOverlay(child=view)
        self.set_content(self.toasts)
        # Two card columns when wide, one when narrow (labels ellipsize).
        narrow = Adw.Breakpoint.new(Adw.BreakpointCondition.parse("max-width: 900sp"))
        narrow.add_setter(self.columns, "orientation", Gtk.Orientation.VERTICAL)
        narrow.add_setter(self.columns, "homogeneous", False)
        self.add_breakpoint(narrow)
        self.set_size_request(360, 400)

        store.connect(self._on_store)
        self.connect("close-request", self._on_close)
        self._finished = []
        GLib.timeout_add(1000, self._sample)

    def _on_close(self, *_):
        app = self.get_application()
        # Closing clears what has finished, here as well as in the
        # application's shutdown: with "Keep running when closed" the window
        # only hides and that shutdown never comes.
        sweep = getattr(app, "sweep_finished", None)
        if sweep is not None:
            sweep()
        # Kept running for the top-bar indicator: hide instead of quitting.
        if getattr(app, "_held", False):
            self.set_visible(False)
            return True
        return False

    def toast(self, text):
        t = Adw.Toast(title=text, timeout=8)
        t.set_use_markup(False)
        self.toasts.add_toast(t)

    # Store updates -----------------------------------------------------------

    def _visible(self, task):
        if not self.show_idle and task.group == "claude" and task.state == "idle":
            return False
        return True

    def _card(self, group):
        card = self.cards.get(group)
        if card is None:
            card = self.cards[group] = GroupCard(group)
            column, order = ((self.right, RIGHT_CARDS) if group in RIGHT_CARDS
                             else (self.left, LEFT_CARDS))
            prev = None
            for g in order[:order.index(group)]:
                prev = self.cards.get(g, prev)
            column.insert_child_after(card, prev)
        return card

    def _on_store(self, added, changed, removed):
        for tid in removed:
            row = self.rows.pop(tid, None)
            if row:
                row.get_parent().remove(row)
                # /proc and desktop-app tasks just vanish when they complete.
                if row.task.group in ("files", "apps") and row.task.state == "running":
                    self._finished.append(time.time())
        for t in added:
            row = self.rows[t.id] = TaskRow(t)
            self._card(t.group).rows.append(row)
        for t in changed:
            row = self.rows.get(t.id)
            if row:
                prev = row.pill.get_label().lower()
                row.refresh()
                if prev in ACTIVE and t.state in ("done", "failed", "idle"):
                    self._finished.append(time.time())
        self._layout()

    def _layout(self):
        tasks = self.store.tasks
        for group, card in list(self.cards.items()):
            members = [t for t in tasks.values() if t.group == group]
            parents = [t for t in members if not t.parent or t.parent not in tasks]
            parents.sort(key=lambda t: (STATE_ORDER.get(t.state, 9), -t.started))
            order = []
            for p in parents:
                order.append(p)
                kids = [t for t in members if t.parent == p.id]
                kids.sort(key=lambda t: (STATE_ORDER.get(t.state, 9), -t.started))
                order.extend(kids)
            prev = None
            shown = 0
            for t in order:
                row = self.rows[t.id]
                vis = self._visible(t) and (
                    not t.parent or self._visible(tasks.get(t.parent, t))
                )
                row.set_visible(vis)
                shown += vis
                card.rows.reorder_child_after(row, prev)
                prev = row
            active = sum(1 for t in members if t.state in ACTIVE)
            card.count.set_label(f"{active} active · {len(members)}")
            card.set_visible(shown > 0)
        for column, order in ((self.left, LEFT_CARDS), (self.right, RIGHT_CARDS)):
            column.set_visible(any(
                self.cards[g].get_visible() for g in order if g in self.cards
            ))
        any_visible = self.left.get_visible() or self.right.get_visible()
        self.columns.set_visible(any_visible)
        self.empty.set_visible(not any_visible)

    def _on_show_idle(self, action, value):
        action.set_state(value)
        self.show_idle = value.get_boolean()
        self._layout()

    # Overview ------------------------------------------------------------------

    def _sample(self):
        now = time.time()
        tasks = list(self.store.tasks.values())
        active = [t for t in tasks if t.state in ACTIVE]
        known = [t.percent for t in active if t.percent is not None]
        mean = sum(known) / len(known) if known else None
        self._finished = [x for x in self._finished if now - x < 3600]
        self.g_active.set_value(min(1.0, len(active) / 10), str(len(active)))
        self.g_mean.set_value((mean or 0) / 100, "—" if mean is None else f"{mean:.0f}%")
        self.g_done.set_value(min(1.0, len(self._finished) / 20), str(len(self._finished)))
        self.chart.add(now, len(active), mean)
        n_claude = sum(1 for t in active if t.group == "claude" and not t.parent)
        self.window_title.set_label(
            f"{len(active)} ACTIVE  ·  {n_claude} CLAUDE BUSY  ·  {len(tasks)} TRACKED"
        )
        for row in self.rows.values():
            if row.task.state == "running" and row.task.percent is not None:
                row.eta.set_label(fmt_eta(row.task.eta()))
        return True

