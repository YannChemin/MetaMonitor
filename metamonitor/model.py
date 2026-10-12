"""Task records shared by every collector and the UI.

SPDX-License-Identifier: Unlicense
"""

import time
from collections import deque
from dataclasses import dataclass, field

ACTIVE = ("running", "blocked", "paused")
GROUPS = {
    # group key: (title, icon, gradient start, gradient end)
    "claude": ("Claude Code", "mm-agent-symbolic", "#ff9f1c", "#fcee0a"),
    "cli": ("Command line", "mm-terminal-symbolic", "#00f0ff", "#2f6bff"),
    "grass": ("GRASS GIS", "mm-grass-symbolic", "#7cff4f", "#00e5c7"),
    "files": ("File transfers", "mm-files-symbolic", "#fcee0a", "#7cff4f"),
    "apps": ("Applications", "mm-apps-symbolic", "#ff2a6d", "#b026ff"),
}


@dataclass
class Task:
    id: str
    group: str
    title: str
    subtitle: str = ""
    detail: str = ""
    percent: float | None = None
    state: str = "running"
    started: float = field(default_factory=time.time)
    updated: float = field(default_factory=time.time)
    parent: str | None = None
    icon: str | None = None
    history: deque = field(default_factory=lambda: deque(maxlen=240))

    def same_as(self, other):
        keys = ("title", "subtitle", "detail", "percent", "state", "parent", "icon")
        return all(getattr(self, k) == getattr(other, k) for k in keys)

    def eta(self):
        """Seconds left from a least-squares fit of recent progress, or None."""
        pts = [(t, p) for t, p in self.history if p is not None]
        if self.state != "running" or self.percent is None or len(pts) < 3:
            return None
        horizon = pts[-1][0] - 120
        pts = [x for x in pts if x[0] >= horizon]
        if len(pts) < 3:
            return None
        n = len(pts)
        mt = sum(t for t, _ in pts) / n
        mp = sum(p for _, p in pts) / n
        var = sum((t - mt) ** 2 for t, _ in pts)
        if var <= 0:
            return None
        slope = sum((t - mt) * (p - mp) for t, p in pts) / var
        if slope <= 1e-4:
            return None
        return (100.0 - self.percent) / slope


class Store:
    """Holds the current tasks; collectors replace their own subset."""

    def __init__(self):
        self.tasks = {}
        self.listeners = []
        self._owner = {}

    def connect(self, fn):
        self.listeners.append(fn)

    def replace(self, source, tasks):
        """Make ``tasks`` the complete set owned by ``source``."""
        now = time.time()
        new_ids = set()
        added, changed = [], []
        for t in tasks:
            new_ids.add(t.id)
            old = self.tasks.get(t.id)
            if old is None:
                t.history.append((now, t.percent))
                self.tasks[t.id] = t
                self._owner[t.id] = source
                added.append(t)
            else:
                if not old.same_as(t):
                    for k in ("title", "subtitle", "detail", "percent", "state",
                              "parent", "icon", "updated"):
                        setattr(old, k, getattr(t, k))
                    changed.append(old)
                if old.percent is not None and old.state == "running":
                    old.history.append((now, old.percent))
        removed = [
            tid for tid, src in self._owner.items() if src == source and tid not in new_ids
        ]
        for tid in removed:
            del self.tasks[tid]
            del self._owner[tid]
        if added or changed or removed:
            for fn in self.listeners:
                fn(added, changed, removed)
