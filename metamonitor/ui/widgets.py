"""Custom widgets drawn with GSK render nodes.

Everything here goes through ``Gtk.Snapshot`` (gradients, path clips,
blurs, filled and stroked paths), which GTK 4.14+ renders on the GPU with
its Vulkan or GL renderer.  No Cairo rasterisation, no shaders to maintain.

The look: btop's segmented gradient meters, cut into Cyberpunk 2077's
chamfered HUD shapes, with a neon bloom and a passing scan sheen.

SPDX-License-Identifier: Unlicense
"""

import math

import gi

gi.require_version("Gdk", "4.0")
gi.require_version("Gtk", "4.0")
gi.require_version("Gsk", "4.0")
gi.require_version("Graphene", "1.0")
from gi.repository import Gdk, Graphene, Gsk, Gtk  # noqa: E402

from .. import brand  # noqa: E402

STATE_COLORS = {
    "done": (brand.GREEN, brand.TEAL),
    "failed": (brand.RED, brand.MAGENTA),
    "paused": (brand.YELLOW, brand.ORANGE),
    "blocked": (brand.YELLOW, brand.MAGENTA),
    "idle": ("#2c3150", "#454c75"),
}
SEGMENT = 7.0  # btop-style block pitch, px.
GAP = 2.0


def rgba(spec, alpha=1.0):
    c = Gdk.RGBA()
    c.parse(spec)
    c.alpha *= alpha
    return c


def stops(*pairs):
    out = []
    for offset, color in pairs:
        s = Gsk.ColorStop()
        s.offset = offset
        s.color = color
        out.append(s)
    return out


def rect(x, y, w, h):
    return Graphene.Rect().init(x, y, w, h)


def point(x, y):
    return Graphene.Point().init(x, y)


def chamfer_path(x, y, w, h, slant):
    """Parallelogram with the HUD slant: ``/____/``.

    Built as a path because GskRoundedRect (an inline struct array) crashes
    PyGObject; paths go through the same GPU pipeline anyway.
    """
    slant = min(slant, w / 2)
    b = Gsk.PathBuilder.new()
    b.move_to(x + slant, y)
    b.line_to(x + w, y)
    b.line_to(x + w - slant, y + h)
    b.line_to(x, y + h)
    b.close()
    return b.to_path()


def cut_corner_path(x, y, w, h, cut):
    """Rectangle with the top-left and bottom-right corners cut off."""
    b = Gsk.PathBuilder.new()
    b.move_to(x + cut, y)
    b.line_to(x + w, y)
    b.line_to(x + w, y + h - cut)
    b.line_to(x + w - cut, y + h)
    b.line_to(x, y + h)
    b.line_to(x, y + cut)
    b.close()
    return b.to_path()


def push_clip(snap, path):
    snap.push_fill(path, Gsk.FillRule.WINDING)


class GlowBar(Gtk.Widget):
    """A segmented neon meter with a chamfered outline.

    ``fraction`` None means indeterminate: a block of segments sweeps the
    track like a scanner.
    """

    __gtype_name__ = "MMGlowBar"

    def __init__(self, c0=brand.CYAN, c1=brand.BLUE, height=10):
        super().__init__()
        self.set_size_request(60, height + 10)
        self.set_hexpand(True)
        self.colors = (c0, c1)
        self.bar_h = height
        self.target = None
        self.shown = 0.0
        self.state = "running"
        self.phase = 0.0
        self._last = None
        self._tick = None

    def set_progress(self, fraction, state):
        if fraction is not None:
            fraction = max(0.0, min(1.0, fraction))
            if self.target is None:
                self.shown = fraction if state != "running" else 0.0
        self.target, self.state = fraction, state
        self._ensure_tick()
        self.queue_draw()

    def _animating(self):
        if self.state in ("running", "blocked"):
            return True
        return self.target is not None and abs(self.target - self.shown) > 1e-3

    def _ensure_tick(self):
        if self._tick is None and self._animating():
            self._last = None
            self._tick = self.add_tick_callback(self._on_tick)

    def _on_tick(self, widget, clock):
        t = clock.get_frame_time() / 1e6
        dt = 0.016 if self._last is None else min(0.1, t - self._last)
        self._last = t
        self.phase = (self.phase + dt) % 1000.0
        if self.target is not None:
            # Critically damped approach: smooth but never overshoots.
            self.shown += (self.target - self.shown) * (1 - math.exp(-dt * 6))
        self.queue_draw()
        if not self._animating():
            self._tick = None
            return False
        return True

    def _segments(self, snap, x0, x1, y, h):
        """Cut the btop block gaps into whatever was drawn below."""
        gap = rgba(brand.VOID, 0.92)
        x = x0 + SEGMENT - GAP
        while x < x1:
            snap.append_color(gap, rect(x, y, GAP, h))
            x += SEGMENT

    def do_snapshot(self, snap):
        w, h = self.get_width(), self.get_height()
        bh = self.bar_h
        y = (h - bh) / 2
        slant = bh * 0.6
        c0, c1 = STATE_COLORS.get(self.state, self.colors)

        track = chamfer_path(0, y, w, bh, slant)
        snap.append_fill(track, Gsk.FillRule.WINDING, rgba("#ffffff", 0.05))
        # Faint rail markers every 10 %, like a HUD scale.
        for i in range(1, 10):
            snap.append_color(rgba(brand.CYAN, 0.10), rect(w * i / 10, y + bh + 2, 1, 2))

        if self.target is None:
            self._scanner(snap, w, y, bh, slant, c0, c1)
            return
        fw = w * self.shown
        if fw < 1:
            return
        fill = chamfer_path(0, y, max(fw, slant * 2), bh, slant)
        if self.state != "idle":
            snap.push_blur(7)
            snap.append_fill(fill, Gsk.FillRule.WINDING, rgba(c1, 0.6))
            snap.pop()
        push_clip(snap, fill)
        snap.append_linear_gradient(
            rect(0, y, fw, bh), point(0, 0), point(max(w, 1), 0),
            stops((0.0, rgba(c0)), (1.0, rgba(c1))),
        )
        snap.append_linear_gradient(
            rect(0, y, fw, bh), point(0, y), point(0, y + bh),
            stops((0.0, rgba("#ffffff", 0.30)), (0.45, rgba("#ffffff", 0.0)),
                  (1.0, rgba("#000000", 0.25))),
        )
        if self.state == "running":
            band = 120.0
            x = ((self.phase * 0.45) % 1.0) * (fw + 2 * band) - band
            snap.append_linear_gradient(
                rect(x, y, band, bh), point(x, 0), point(x + band, 0),
                stops((0.0, rgba("#ffffff", 0.0)), (0.5, rgba("#ffffff", 0.45)),
                      (1.0, rgba("#ffffff", 0.0))),
            )
        self._segments(snap, 0, fw, y, bh)
        snap.pop()
        # Bright leading edge: the "write head" of the meter.
        if self.state == "running" and fw < w - 1:
            snap.append_color(rgba("#ffffff", 0.85), rect(fw - slant - 1, y, 2, bh))

    def _scanner(self, snap, w, y, bh, slant, c0, c1):
        push_clip(snap, chamfer_path(0, y, w, bh, slant))
        if self.state not in ("running", "blocked"):
            snap.append_linear_gradient(
                rect(0, y, w, bh), point(0, 0), point(w, 0),
                stops((0.0, rgba(c0, 0.30)), (1.0, rgba(c1, 0.30))),
            )
        else:
            cw = max(70.0, w * 0.25)
            s = (math.sin(self.phase * 1.7) + 1) / 2
            x = s * (w + cw) - cw
            snap.append_linear_gradient(
                rect(x, y, cw, bh), point(x, 0), point(x + cw, 0),
                stops((0.0, rgba(c0, 0.0)), (0.55, rgba(c0, 0.95)), (0.7, rgba(c1, 0.95)),
                      (1.0, rgba(c1, 0.0))),
            )
        self._segments(snap, 0, w, y, bh)
        snap.pop()


class RingGauge(Gtk.Widget):
    """A HUD ring: dashed track, segmented conic-gradient arc, centred value."""

    __gtype_name__ = "MMRingGauge"

    def __init__(self, label, c0, c1, size=116):
        super().__init__()
        self.set_size_request(size, size)
        self.c0, self.c1 = c0, c1
        self.value = 0.0
        self.shown = 0.0
        self._tick = None
        self.set_layout_manager(Gtk.BinLayout())
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, valign=Gtk.Align.CENTER)
        self.big = Gtk.Label(label="0", css_classes=["mm-gauge-value"])
        self.small = Gtk.Label(label=label, css_classes=["mm-gauge-caption"])
        box.append(self.big)
        box.append(self.small)
        box.set_parent(self)

    def set_value(self, fraction, text):
        self.value = max(0.0, min(1.0, fraction))
        self.big.set_label(text)
        if self._tick is None:
            self._tick = self.add_tick_callback(self._on_tick)

    def _on_tick(self, *_):
        self.shown += (self.value - self.shown) * 0.12
        self.queue_draw()
        if abs(self.value - self.shown) < 1e-3:
            self.shown = self.value
            self._tick = None
            return False
        return True

    def do_snapshot(self, snap):
        w, h = self.get_width(), self.get_height()
        d = min(w, h)
        lw = 8.0
        r = d / 2 - lw - 3
        cx, cy = w / 2, h / 2

        # Outer tick ring.
        ticks = Gsk.Stroke.new(2.0)
        ticks.set_dash([1.5, 5.0])
        b = Gsk.PathBuilder.new()
        b.add_circle(point(cx, cy), r + lw / 2 + 3)
        snap.append_stroke(b.to_path(), ticks, rgba(brand.CYAN, 0.25))

        track = Gsk.Stroke.new(lw)
        track.set_dash([5.0, 2.0])
        b = Gsk.PathBuilder.new()
        b.add_circle(point(cx, cy), r)
        snap.append_stroke(b.to_path(), track, rgba("#ffffff", 0.07))

        f = self.shown
        if f > 0.002:
            a0 = -math.pi / 2
            a1 = a0 + 2 * math.pi * min(f, 0.9999)
            b = Gsk.PathBuilder.new()
            b.move_to(cx + r * math.cos(a0), cy + r * math.sin(a0))
            b.svg_arc_to(r, r, 0, f > 0.5, True, cx + r * math.cos(a1), cy + r * math.sin(a1))
            path = b.to_path()
            bounds = rect(cx - r - lw, cy - r - lw, 2 * (r + lw), 2 * (r + lw))
            snap.push_blur(7)
            snap.push_stroke(path, Gsk.Stroke.new(lw))
            snap.append_color(rgba(self.c1, 0.7), bounds)
            snap.pop()
            snap.pop()
            snap.push_stroke(path, track)
            snap.append_conic_gradient(
                bounds, point(cx, cy), 0,
                stops((0.0, rgba(self.c0)), (0.5, rgba(self.c1)), (1.0, rgba(self.c0))),
            )
            snap.pop()
        child = self.get_first_child()
        if child:
            self.snapshot_child(child, snap)


class HistoryChart(Gtk.Widget):
    """btop-style area chart: active task count and mean completion over time."""

    __gtype_name__ = "MMHistoryChart"

    def __init__(self, seconds=600):
        super().__init__()
        self.set_size_request(240, 116)
        self.set_hexpand(True)
        self.seconds = seconds
        self.samples = []  # (t, active, mean_percent or None)

    def add(self, t, active, mean):
        self.samples.append((t, active, mean))
        cutoff = t - self.seconds
        while self.samples and self.samples[0][0] < cutoff:
            self.samples.pop(0)
        self.queue_draw()

    def do_snapshot(self, snap):
        w, h = self.get_width(), self.get_height()
        pad = 6.0
        top, bot = pad, h - pad
        push_clip(snap, cut_corner_path(0, 0, w, h, 12))
        snap.append_color(rgba(brand.VOID, 0.55), rect(0, 0, w, h))
        grid = rgba(brand.CYAN, 0.07)
        for i in range(1, 4):
            snap.append_color(grid, rect(0, top + (bot - top) * i / 4, w, 1))
        for i in range(1, 12):
            snap.append_color(grid, rect(w * i / 12, 0, 1, h))
        if len(self.samples) >= 2:
            t_end = self.samples[-1][0]
            # The window grows with the data, up to ``seconds``, so the chart
            # is filled from the first minute on.
            span = min(self.seconds, max(60.0, t_end - self.samples[0][0]))
            t0 = t_end - span
            peak = max(2, max(s[1] for s in self.samples))

            def xy(t, v):
                return w * (t - t0) / span, bot - (bot - top) * v

            self._area(snap, [xy(t, a / peak) for t, a, _ in self.samples],
                       brand.MAGENTA, brand.YELLOW, bot)
            pts = [xy(t, m / 100) for t, _, m in self.samples if m is not None]
            if len(pts) >= 2:
                self._area(snap, pts, brand.BLUE, brand.CYAN, bot, fill_alpha=0.20)
        # btop draws its graphs in braille dots; a vertical raster of gaps
        # gives the same dotted texture.
        x = 3.0
        gap = rgba(brand.VOID, 0.5)
        while x < w:
            snap.append_color(gap, rect(x, 0, 1, h))
            x += 4.0
        snap.pop()
        snap.append_stroke(cut_corner_path(0.5, 0.5, w - 1, h - 1, 12),
                           Gsk.Stroke.new(1.0), rgba(brand.CYAN, 0.25))

    @staticmethod
    def _area(snap, pts, c0, c1, bottom, fill_alpha=0.35):
        line = Gsk.PathBuilder.new()
        area = Gsk.PathBuilder.new()
        area.move_to(pts[0][0], bottom)
        for i, (x, y) in enumerate(pts):
            (line.move_to if i == 0 else line.line_to)(x, y)
            area.line_to(x, y)
        area.line_to(pts[-1][0], bottom)
        area.close()
        xs = [p[0] for p in pts]
        ytop = min(p[1] for p in pts)
        bounds = rect(min(xs), ytop - 4, max(1, max(xs) - min(xs)), bottom - ytop + 4)
        snap.push_fill(area.to_path(), Gsk.FillRule.WINDING)
        snap.append_linear_gradient(
            bounds, point(0, ytop), point(0, bottom),
            stops((0.0, rgba(c1, fill_alpha)), (1.0, rgba(c0, 0.0))),
        )
        snap.pop()
        stroke = Gsk.Stroke.new(2.0)
        stroke.set_line_join(Gsk.LineJoin.ROUND)
        path = line.to_path()
        snap.push_blur(4)
        snap.append_stroke(path, stroke, rgba(c1, 0.6))
        snap.pop()
        snap.push_stroke(path, stroke)
        snap.append_linear_gradient(
            rect(bounds.get_x(), bounds.get_y() - 2, bounds.get_width(), bounds.get_height() + 4),
            point(bounds.get_x(), 0), point(bounds.get_x() + bounds.get_width(), 0),
            stops((0.0, rgba(c0)), (1.0, rgba(c1))),
        )
        snap.pop()
