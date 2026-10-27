#!/usr/bin/env bash
# MINCECRAFT weighing station — what the desktop icon and autostart run.
#
#   bash mincecraft.sh            update check, then the real scale, fullscreen
#   bash mincecraft.sh --demo     no hardware, windowed, scratch logs, no updates
#
# Works from wherever this folder lives. Anything station.py accepts can be
# passed through (--port /dev/ttyUSB1, --windowed, ...). Run it with `bash`:
# nothing in this repo needs the executable bit (see install-pi.sh).
#
# Before the station opens, updater.py --boot checks GitHub (at most ~15 s
# waiting for the network, ~30 s for the fetch): new code on the `stable`
# branch is switched to, and recipes from the owner's workbook are synced.
# No network or any error -> the station opens with what is already here.
# If the station then fails within the first 2 minutes after a code update,
# the update is rolled back and the station is started again, once.

HERE="$(cd "$(dirname "$(readlink -f "$0")")" && pwd)"
cd "$HERE" || exit 1

PY="${MINCECRAFT_PYTHON:-python3}"
HEALTHY_S="${MINCECRAFT_HEALTHY_S:-120}"     # running this long = update is good
NET_WAIT_S="${MINCECRAFT_NET_WAIT_S:-15}"    # how long to wait for DNS at boot

LOGDIR="${XDG_STATE_HOME:-$HOME/.local/state}/mincecraft"
LOG="$LOGDIR/station.log"
mkdir -p "$LOGDIR"

# Started from an icon there is no terminal, so a failure would otherwise be
# silent: the operator taps, nothing happens, they tap again. Say what broke.
complain() {
    local msg="$1"
    echo "$(date '+%F %T')  $msg" >> "$LOG"
    if command -v zenity >/dev/null 2>&1; then
        zenity --error --title="MINCECRAFT" --width=420 --text="$msg" 2>/dev/null
    elif command -v xmessage >/dev/null 2>&1; then
        xmessage -center "MINCECRAFT: $msg" 2>/dev/null
    else
        echo "$msg" >&2
    fi
}

if ! "$PY" -c "import PyQt5, serial" 2>/dev/null; then
    complain "Python packages are missing. Run install-pi.sh once:
  bash $HERE/install-pi.sh"
    exit 1
fi

# One station at a time. On a touchscreen a double tap on the icon is easy,
# and two copies would both open /dev/ttyUSB0 and fight over every frame.
exec 9>"$LOGDIR/station.lock"
if ! flock -n 9; then
    complain "The weighing station is already running."
    exit 0
fi

DEMO=0
for a in "$@"; do [ "$a" = "--demo" ] && DEMO=1; done

{
    echo
    echo "=== $(date '+%F %T')  started: station.py $* ==="
} >> "$LOG"

# ------------------------------------------------------------------ updates
if [ "$DEMO" -eq 0 ] && [ -z "${MINCECRAFT_UPDATED:-}" ] && [ -f "$HERE/updater.py" ]; then
    # Wi-Fi is often still coming up when the desktop autostarts us.
    for _ in $(seq 1 "$NET_WAIT_S"); do
        getent hosts github.com >/dev/null 2>&1 && break
        sleep 1
    done
    timeout 60 "$PY" -u "$HERE/updater.py" --boot >> "$LOG" 2>&1
    rc=$?
    if [ "$rc" -eq 10 ]; then
        echo "$(date '+%F %T')  code updated — restarting from the new files" >> "$LOG"
        exec 9>&-
        MINCECRAFT_UPDATED=1 exec bash "$HERE/mincecraft.sh" "$@"
    fi
fi

# ------------------------------------------------------------------ station
# -u: unbuffered. Redirected to a file, Python holds output back in a buffer,
# and a power cut or a kill loses exactly the lines that explain what happened.
"$PY" -u "$HERE/station.py" "$@" >> "$LOG" 2>&1 &
pid=$!
trap 'kill -TERM "$pid" 2>/dev/null' TERM INT HUP

# Still running after HEALTHY_S seconds: whatever code this is, it is good.
# 9>&- : the watcher must not hold the single-instance lock, or a restart
# after a rollback would find "already running".
watcher=""
if [ "$DEMO" -eq 0 ] && [ -f "$HERE/updater.py" ]; then
    (
        sleep "$HEALTHY_S"
        kill -0 "$pid" 2>/dev/null && "$PY" "$HERE/updater.py" --mark-good >> "$LOG" 2>&1
    ) 9>&- &
    watcher=$!
fi

wait "$pid"
status=$?
while kill -0 "$pid" 2>/dev/null; do     # woken by a signal: wait for the real exit
    wait "$pid"
    status=$?
done
[ -n "$watcher" ] && kill "$watcher" 2>/dev/null

# 143 is SIGTERM and 130 SIGINT — logout, shutdown, Ctrl-C. Those are the
# station being told to stop, not failing, and an error box at every shutdown
# would teach people to ignore the error box.
case "$status" in
    0|130|143) exit "$status" ;;
esac

# Failed. If that was a fresh code update, go back to the last good code and
# try once more.
if [ "$DEMO" -eq 0 ] && [ -z "${MINCECRAFT_ROLLED_BACK:-}" ] && [ -f "$HERE/updater.py" ]; then
    "$PY" -u "$HERE/updater.py" --rollback >> "$LOG" 2>&1
    if [ $? -eq 10 ]; then
        echo "$(date '+%F %T')  station failed after an update (exit $status) — rolled back, starting again" >> "$LOG"
        exec 9>&-
        MINCECRAFT_UPDATED=1 MINCECRAFT_ROLLED_BACK=1 exec bash "$HERE/mincecraft.sh" "$@"
    fi
fi

complain "The station stopped with an error (exit $status).

Last lines of $LOG:

$(tail -n 12 "$LOG")"
exit "$status"
