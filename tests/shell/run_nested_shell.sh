#!/usr/bin/env bash
# MODULE:    run_nested_shell.sh
# AUTHOR(S): Yann Chemin
# PURPOSE:   Smoke-test the MetaMonitor GNOME Shell extension in a throwaway
#            headless gnome-shell, without touching the desktop session and
#            without leaving any process behind.
# SPDX-License-Identifier: Unlicense
#
# Usage: tests/shell/run_nested_shell.sh [OUTDIR]
#
# Runs gnome-shell --headless on its own private D-Bus session bus, with
# throwaway XDG/HOME directories, enables only the extension from this source
# tree, checks that its indicator is in the top bar and that its menu opens,
# and saves a screenshot to OUTDIR (default: a temporary directory, printed
# at the end). Exit status 0 when every check passes.
#
# Input method: gnome-shell starts "ibus-daemon --panel disable" itself
# whenever the IBus systemd user unit is not reachable, which is always the
# case on a private bus. When gnome-shell then exits, that ibus-daemon is
# orphaned: it loses its bus, spins at about 35 % CPU and grows by gigabytes
# of RAM. The test needs no input method, so a no-op ibus-daemon is put first
# on PATH, and on exit every process still attached to the private bus is
# killed as a second safeguard.

set -uo pipefail

if [[ "${1:-}" == "--inner" ]]; then
    # Second stage, inside dbus-run-session: the private bus is up.
    shift
    WORK=$1
    OUT=$2
    EXT=$3
    HELPER=$4
    printf '%s\n' "$DBUS_SESSION_BUS_ADDRESS" > "$WORK/bus"
    export XDG_DATA_HOME=$WORK/data XDG_CONFIG_HOME=$WORK/config \
        XDG_CACHE_HOME=$WORK/cache HOME=$WORK/home GSETTINGS_BACKEND=keyfile
    export PATH="$WORK/stub:$PATH"
    gsettings set org.gnome.shell disable-user-extensions false
    gsettings set org.gnome.shell enabled-extensions "['$EXT', '$HELPER']"
    gnome-shell --headless --virtual-monitor 1400x900 > "$OUT/shell.log" 2>&1 &
    shell=$!
    trap 'kill "$shell" 2>/dev/null; wait "$shell" 2>/dev/null' EXIT

    eval_js() {
        gdbus call --session --dest org.gnome.Shell --object-path /org/gnome/Shell \
            --method org.gnome.Shell.Eval "$1" 2>&1
    }
    # Wait for the shell to answer on D-Bus (up to 60 s).
    for _ in $(seq 60); do
        eval_js "1" | grep -q "^(true" && break
        kill -0 "$shell" 2>/dev/null || { echo "gnome-shell exited, see $OUT/shell.log"; exit 1; }
        sleep 1
    done
    sleep 3
    fail=0
    check() {
        local name=$1 expect=$2 got
        got=$(eval_js "$3")
        echo "$name: $got" >> "$OUT/result.txt"
        if [[ "$got" == *"$expect"* ]]; then
            echo "ok   $name"
        else
            echo "FAIL $name: $got"
            fail=1
        fi
    }
    check "indicator" "present" \
        "Main.panel.statusArea['$EXT'] ? 'present' : 'missing'"
    check "menu" "opened" \
        "Main.panel.statusArea['$EXT'].menu.open(); 'opened'"
    sleep 2
    gdbus call --session --dest org.gnome.Shell --object-path /org/gnome/Shell/Screenshot \
        --method org.gnome.Shell.Screenshot.Screenshot false false "$OUT/shell.png" \
        >> "$OUT/result.txt" 2>&1
    gnome-extensions info "$EXT" >> "$OUT/result.txt" 2>&1
    check "state" "true" \
        "Main.extensionManager.lookup('$EXT')?.state === 1"
    exit $fail
fi

HERE=$(cd "$(dirname "$0")" && pwd)
TOP=$(cd "$HERE/../.." && pwd)
EXT=metamonitor@yannchemin.github.io
# Since GNOME 45, org.gnome.Shell.Eval only answers in unsafe mode; this
# helper extension, installed only in the throwaway data directory, turns it on.
HELPER=unsafe-eval@metamonitor.test
for cmd in gnome-shell dbus-run-session gdbus gsettings; do
    command -v "$cmd" >/dev/null || { echo "$cmd not found, skipping"; exit 77; }
done

WORK=$(mktemp -d "${TMPDIR:-/tmp}/mm-shelltest.XXXXXX")
OUT=${1:-$WORK/out}
mkdir -p "$OUT" "$WORK/data/gnome-shell/extensions" "$WORK/config" "$WORK/cache" \
    "$WORK/home" "$WORK/stub"
OUT=$(cd "$OUT" && pwd)
: > "$OUT/result.txt"
cp -r "$TOP/gnome-shell/$EXT" "$WORK/data/gnome-shell/extensions/"
mkdir -p "$WORK/data/gnome-shell/extensions/$HELPER"
cat > "$WORK/data/gnome-shell/extensions/$HELPER/metadata.json" <<META
{"uuid": "$HELPER", "name": "Test helper",
 "description": "Unsafe mode for the MetaMonitor shell test",
 "shell-version": ["45", "46", "47", "48", "49"]}
META
cat > "$WORK/data/gnome-shell/extensions/$HELPER/extension.js" <<'JS'
import {Extension} from 'resource:///org/gnome/shell/extensions/extension.js';

export default class UnsafeEval extends Extension {
    enable() {
        global.context.unsafe_mode = true;
    }

    disable() {
        global.context.unsafe_mode = false;
    }
}
JS
printf '#!/bin/sh\n# No input method in the test shell.\nexit 0\n' > "$WORK/stub/ibus-daemon"
chmod +x "$WORK/stub/ibus-daemon"

cleanup() {
    # Kill whatever is still attached to the test's private bus: gnome-shell
    # children re-parented to systemd --user survive dbus-run-session.
    if [[ -s "$WORK/bus" ]]; then
        local bus pid
        bus=$(<"$WORK/bus")
        for pid in $(pgrep -u "$(id -u)"); do
            if { tr '\0' '\n' < "/proc/$pid/environ"; } 2>/dev/null |
                grep -qxF "DBUS_SESSION_BUS_ADDRESS=$bus"; then
                kill -TERM "$pid" 2>/dev/null
            fi
        done
        sleep 1
        for pid in $(pgrep -u "$(id -u)"); do
            if { tr '\0' '\n' < "/proc/$pid/environ"; } 2>/dev/null |
                grep -qxF "DBUS_SESSION_BUS_ADDRESS=$bus"; then
                echo "killing leftover $(ps -o comm= -p "$pid") ($pid)"
                kill -KILL "$pid" 2>/dev/null
            fi
        done
    fi
    rm -rf "$WORK/data" "$WORK/config" "$WORK/cache" "$WORK/home" "$WORK/stub"
}
trap cleanup EXIT

# The private session's services log to stderr; keep that out of the way.
dbus-run-session -- "$0" --inner "$WORK" "$OUT" "$EXT" "$HELPER" 2> "$OUT/session.log"
status=$?
echo "Results and screenshot: $OUT"
exit $status
