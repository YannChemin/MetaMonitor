"""Help dialog: how to get progress into MetaMonitor, per environment.

SPDX-License-Identifier: Unlicense
"""

import gi

gi.require_version("Adw", "1")
from gi.repository import Adw, Gtk  # noqa: E402

# page name: (title, icon, [(heading, text, code or None)])
PAGES = {
    "bash": ("Bash", "mm-terminal-symbolic", [
        ("Already automatic",
         "The install adds one block to ~/.bashrc (and ~/.zshrc). In a terminal, rsync, wget, "
         "gdal_translate, gdalwarp, gdaladdo, gdal_rasterize, ogr2ogr, pip, uv, rclone and "
         "aria2c then run through mmrun on their own and show up here. Pipes and scripts "
         "are left untouched. Choose the list with METAMONITOR_WRAP.",
         "type rsync                     # rsync is a function: it is wrapped\n"
         "export METAMONITOR_WRAP=\"rsync wget ffmpeg-progress\"   # before the hook\n"
         "mmctl bash status | enable | disable"),
        ("Wrap any command",
         "mmrun runs the command on a pseudo-terminal, so it still draws its own bar. "
         "MetaMonitor parses what it prints and records success or failure from the exit "
         "status.",
         "mmrun -n \"Sync rasters\" -- rsync -a --info=progress2 src/ dst/\n"
         "mmrun -- grass ~/grassdata/emit_dubai/PERMANENT --exec r.slope.aspect ..."),
        ("Pipe it in",
         "When the output already goes through a pipe, send it to mmctl pipe. It echoes "
         "the input unless -q is given. Use -N for tools that print bare numbers.",
         "make -j8 2>&1 | mmctl pipe build -n \"Kernel build\"\n"
         "pv -n big.tar 2> >(mmctl pipe -N -q untar) | tar x\n"
         "dialog_script | mmctl pipe -N -q setup"),
        ("Follow a log file",
         "For jobs that write a log (nohup, cron, a cluster scheduler), follow the log; "
         "--pid stops following when the process exits.",
         "nohup ./process.sh > run.log 2>&1 &\n"
         "mmctl tail process run.log --pid $! &"),
        ("Report from your own loop",
         "mmctl set takes a percentage, or a count with --of TOTAL. done, fail and pause "
         "end or flag the slot. With --pid, a slot whose process dies turns red.",
         "files=(*.tif); n=${#files[@]}; i=0\n"
         "mmctl set batch 0 -n \"Reproject tiles\" --pid $$\n"
         "trap 'mmctl fail batch -d \"failed at $f\"' ERR\n"
         "for f in \"${files[@]}\"; do\n"
         "    gdalwarp -q -t_srs EPSG:32640 \"$f\" \"out/$f\"\n"
         "    mmctl set batch $((++i)) --of \"$n\" -d \"$f\"\n"
         "done\n"
         "mmctl done batch"),
        ("Without mmctl",
         "A slot is a plain file. A bare number is a percentage and the file name is the "
         "task name; JSON adds a name, detail, state and pid. Write it from anything.",
         "slots=\"$XDG_RUNTIME_DIR/metamonitor/slots\"; mkdir -p \"$slots\"\n"
         "echo 42 > \"$slots/my-job\"\n"
         "echo '{\"name\":\"LUT\",\"percent\":30,\"detail\":\"band 120/400\",\"state\":\"running\"}' "
         "> \"$slots/lut\"\n"
         "rm \"$slots/my-job\"                # remove it when done"),
    ]),
    "grass": ("GRASS GIS", "mm-grass-symbolic", [
        ("Sessions",
         "Every GRASS session appears as project / mapset, found from its mapset .gislock "
         "and from processes carrying GISRC. Locks whose process is gone are flagged, "
         "since they keep the mapset from opening.",
         None),
        ("Module progress, by default",
         "MetaMonitor adds a LD_PRELOAD line to ~/.grass8/bashrc, which GRASS loads for the "
         "shell, --exec and the GUI. A small library then forwards every G_percent() call "
         "unchanged and also reports it, for C modules and for Python scripts using "
         "gs.percent(). This works even with --quiet. Errors from G_fatal_error() show on "
         "the bar.",
         "mmctl grass status | enable | disable"),
        ("Python API sessions",
         "Sessions opened with grass.script.setup.init() or grass.jupyter do not read "
         "~/.grass8/bashrc. Their modules still appear, with progress estimated from how "
         "far they have read their input map. For exact progress, start Python with the "
         "library preloaded.",
         "LD_PRELOAD=/usr/lib/metamonitor/libmmgrass.so jupyter lab"),
    ]),
    "python": ("Python", "mm-module-symbolic", [
        ("tqdm and print()",
         "Anything that prints NN% (tqdm, rich, keras, pip) works through mmrun or "
         "mmctl pipe with no code change.",
         "mmrun -- python3 train.py"),
        ("Write the slot yourself",
         "No import needed: write JSON into the spool. Include the pid so a crash turns "
         "the bar red.",
         "import json, os, pathlib\n"
         "slot = pathlib.Path(os.environ[\"XDG_RUNTIME_DIR\"], \"metamonitor/slots/train\")\n"
         "slot.parent.mkdir(parents=True, exist_ok=True)\n"
         "def report(done, total, detail=\"\"):\n"
         "    tmp = slot.with_name(\".train.tmp\")\n"
         "    tmp.write_text(json.dumps({\"name\": \"Training\", \"pid\": os.getpid(),\n"
         "        \"percent\": 100 * done / total, \"detail\": detail}))\n"
         "    tmp.replace(slot)            # atomic: readers never see half a file"),
    ]),
    "claude": ("Claude Code", "mm-agent-symbolic", [
        ("Sessions",
         "Every live Claude Code session shows its status, current tool call, step number "
         "and last prompt, with no setup. Background shells and monitors appear under "
         "their session, with any percentage they print and their exit code.",
         None),
        ("Real percentages",
         "A session's bar becomes determinate when Claude uses its task list: completed "
         "tasks over total. For long loops, ask Claude to report progress itself, for "
         "example in a project's CLAUDE.md:",
         "For long jobs, report progress with\n"
         "`mmctl set <id> <done> --of <total> -n \"<name>\"` and `mmctl done <id>`."),
    ]),
    "formats": ("Formats", "mm-files-symbolic", [
        ("Recognised progress",
         "The same parser reads mmrun output, mmctl pipe/tail input and Claude background "
         "shells. It recognises:",
         "OSC 9;4 terminal progress   ESC ] 9 ; 4 ; state ; percent BEL\n"
         "percentages                 42%   42.5 %   (tqdm, rsync, wget, curl, pip, apt)\n"
         "GRASS                       G_percent \"  42%\" with backspaces,\n"
         "                            GRASS_INFO_PERCENT: 42\n"
         "GDAL                        0...10...20...  and  100 - done.\n"
         "counters                    [ 12/340]   Step 3/7   (not dates or paths)\n"
         "bare numbers                12\\n47\\n   with mmctl pipe -N"),
        ("Found without any setup",
         "cp, mv, dd, tar, rsync, gzip, xz, zstd, unzip, sha256sum, gdal_translate, ffmpeg "
         "and other file-crunching commands are read from /proc, as the offset into their "
         "largest input file. Add more names with METAMONITOR_PROC_COMMANDS. Applications "
         "that broadcast Unity LauncherEntry progress on D-Bus (browser downloads, "
         "Transmission, Steam) are picked up as well.",
         None),
    ]),
}


def _code_block(code):
    box = Gtk.Box(spacing=6)
    label = Gtk.Label(label=code, xalign=0, selectable=True, wrap=True, hexpand=True,
                      css_classes=["mm-help-code"])
    copy = Gtk.Button(icon_name="edit-copy-symbolic", valign=Gtk.Align.START,
                      tooltip_text="Copy", css_classes=["flat"])
    copy.connect("clicked", lambda b: b.get_clipboard().set(code))
    box.append(label)
    box.append(copy)
    return box


def help_dialog(page="bash"):
    dialog = Adw.PreferencesDialog(title="Feeding MetaMonitor", search_enabled=True)
    dialog.set_content_width(760)
    dialog.set_content_height(720)
    for name, (title, icon, sections) in PAGES.items():
        pref = Adw.PreferencesPage(name=name, title=title, icon_name=icon)
        for heading, text, code in sections:
            group = Adw.PreferencesGroup()
            box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
            box.append(Gtk.Label(label=heading.upper(), xalign=0,
                                 css_classes=["mm-help-heading"]))
            box.append(Gtk.Label(label=text, xalign=0, wrap=True,
                                 css_classes=["mm-help-text"]))
            if code:
                box.append(_code_block(code))
            group.add(box)
            pref.add(group)
        dialog.add(pref)
    dialog.set_visible_page_name(page)
    return dialog
