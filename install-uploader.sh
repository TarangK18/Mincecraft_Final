#!/usr/bin/env bash
# Set up the MINCECRAFT batch uploader on the Pi (systemd user service).
#
#   bash install-uploader.sh            install / update, start if a key is set
#   bash install-uploader.sh --remove   stop and remove the service (keeps
#                                       api.env, boot_id and the offset)
#
# Run as the station's user (doki-pi), NOT with sudo, and with `bash` — this
# script is deliberately not marked executable (see "file mode" below).
# Safe to run again.

set -e

if [ "$(id -u)" -eq 0 ]; then
    echo "Run this as your normal user, not with sudo."
    exit 1
fi

HERE="$(cd "$(dirname "$(readlink -f "$0")")" && pwd)"
CONF="${XDG_CONFIG_HOME:-$HOME/.config}/mincecraft"
STATE="${XDG_STATE_HOME:-$HOME/.local/state}/mincecraft"
UNITDIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
UNIT="mincecraft-uploader.service"
ENVF="$CONF/api.env"

if [ "${1:-}" = "--remove" ]; then
    systemctl --user disable --now "$UNIT" 2>/dev/null || true
    rm -f "$UNITDIR/$UNIT"
    systemctl --user daemon-reload
    echo "Uploader service removed. $ENVF and the upload position were kept."
    exit 0
fi

echo "Installing the uploader from: $HERE"

# ------------------------------------------------------------- file mode
# install-pi.sh once ran chmod +x on files in this clone. Git sees a mode
# change as a local edit, and `git pull` then refuses to update those files.
# Tell git to ignore modes in this clone; nothing here needs the x bit.
git -C "$HERE" config core.fileMode false 2>/dev/null && \
    echo "  git core.fileMode = false (git pull no longer trips on chmod)"

# --------------------------------------------------------------- key file
mkdir -p "$CONF" "$STATE" "$UNITDIR"
chmod 700 "$CONF"
if [ ! -f "$ENVF" ]; then
    cp "$HERE/api.env.example" "$ENVF"
    echo "  created $ENVF from the template"
fi
chmod 600 "$ENVF"

KEY_SET=1
if ! grep -q '^MINCECRAFT_API_KEY=.\+' "$ENVF" || \
   grep -q '^MINCECRAFT_API_KEY=PUT-THE-WRITE-KEY-HERE' "$ENVF"; then
    KEY_SET=0
fi

# ---------------------------------------------------------------- service
sed "s|@HERE@|$HERE|g" "$HERE/mincecraft-uploader.service" > "$UNITDIR/$UNIT"
systemctl --user daemon-reload
systemctl --user enable "$UNIT" >/dev/null
echo "  service        $UNITDIR/$UNIT (enabled)"

# Without lingering, a user service only runs while that user is logged in.
# The station's desktop login usually covers it; this makes it independent.
if command -v loginctl >/dev/null 2>&1 && \
   [ "$(loginctl show-user "$USER" -p Linger --value 2>/dev/null)" != "yes" ]; then
    echo "  enabling start at boot without a login (needs sudo)…"
    sudo loginctl enable-linger "$USER" || echo "  (linger not enabled — the uploader runs while $USER is logged in)"
fi

# ------------------------------------------------------------------ clock
SYNC="$(timedatectl show -p NTPSynchronized --value 2>/dev/null || echo unknown)"
echo "  clock synced   $SYNC   (batches finished while this is 'no' are sent without times)"

echo
if [ "$KEY_SET" -eq 0 ]; then
    systemctl --user stop "$UNIT" 2>/dev/null || true
    echo "** Not started yet: put the WRITE key in $ENVF"
    echo "     nano $ENVF"
    echo "   then:  systemctl --user restart $UNIT"
else
    systemctl --user restart "$UNIT"
    sleep 3
    systemctl --user --no-pager --lines=8 status "$UNIT" || true
    echo
    echo "Running. Follow it with:  journalctl --user -u $UNIT -f"
fi
