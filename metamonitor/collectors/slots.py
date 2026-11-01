"""Slots written by mmctl, mmrun, scripts or anything else into the spool.

SPDX-License-Identifier: Unlicense
"""

import time

from .. import spool
from ..model import GROUPS, Task

LINGER = 120  # Seconds a finished slot stays visible.
STALE = 600  # A running slot with no pid and no update for this long is stale.
PURGE = 86400  # Finished slot files older than this are deleted.


class SlotCollector:
    source = "slots"

    def poll(self):
        now = time.time()
        out = []
        for s in spool.list_slots():
            state = s["state"]
            pid = s.get("pid")
            detail = s.get("detail") or ""
            # The PID was ours when the slot was last written; one that
            # started later was reused by another process after ours died.
            if state in ("running", "paused", "blocked") and pid and not spool.same_process(
                pid, seen_at=s["updated"]
            ):
                if (s.get("percent") or 0) >= 100:
                    # Reached the end without saying so (gs.percent scripts).
                    state = "done"
                    s.setdefault("finished", s["updated"])
                else:
                    state, detail = "failed", f"process {pid} vanished. {detail}".strip()
            elif state == "running" and not pid and now - s["updated"] > STALE:
                state = "paused"
                detail = f"no update for {int((now - s['updated']) // 60)} min. {detail}"
            if state in ("done", "failed"):
                age = now - s.get("finished", s["updated"])
                if age > PURGE:
                    spool.remove_slot(s["id"])
                if age > LINGER:
                    continue
            src = s.get("source", "slot")
            out.append(
                Task(
                    id=f"slot:{s['id']}",
                    group=s.get("group") if s.get("group") in GROUPS else "cli",
                    title=str(s.get("name", s["id"])),
                    subtitle=f"{src} · pid {pid}" if pid else src,
                    detail=detail,
                    percent=s.get("percent"),
                    state=state,
                    started=s.get("started", s["updated"]),
                    updated=s["updated"],
                    parent=s.get("parent"),
                    icon="mm-module-symbolic" if src == "grass" else "mm-terminal-symbolic",
                )
            )
        return out
