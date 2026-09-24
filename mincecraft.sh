#!/usr/bin/env bash
# MINCECRAFT weighing station — what the desktop icon runs.
#
#   ./mincecraft.sh            real scale on /dev/ttyUSB0, fullscreen
#   ./mincecraft.sh --demo     no hardware, windowed, scratch logs
#
# Works from wherever this folder lives. Anything station.py accepts can be
# passed through (--port /dev/ttyUSB1, --windowed, ...).

HERE="$(cd "$(dirname "$(readlink -f "$0")")" && pwd)"
cd "$HERE" || exit 1

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

if ! python3 -c "import PyQt5, serial" 2>/dev/null; then
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

{
    echo
    echo "=== $(date '+%F %T')  started: station.py $* ==="
} >> "$LOG"

# -u: unbuffered. Redirected to a file, Python holds output back in a buffer,
# and a power cut or a kill loses exactly the lines that explain what happened.
python3 -u "$HERE/station.py" "$@" >> "$LOG" 2>&1
status=$?

# 143 is SIGTERM and 130 SIGINT — logout, shutdown, Ctrl-C. Those are the
# station being told to stop, not failing, and an error box at every shutdown
# would teach people to ignore the error box.
case "$status" in
    0|130|143) ;;
    *)  complain "The station stopped with an error (exit $status).

Last lines of $LOG:

$(tail -n 12 "$LOG")" ;;
esac
exit "$status"
