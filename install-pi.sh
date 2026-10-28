#!/usr/bin/env bash
# One-time setup of the MINCECRAFT weighing station on a Raspberry Pi.
#
#   bash install-pi.sh                 everything: icons, opens at boot,
#                                      recipe sync every 5 min
#   bash install-pi.sh --no-autostart  ...but don't open at boot
#   bash install-pi.sh --no-apt        skip package install (offline re-run)
#
# Opening at boot needs two things, and this sets both: the desktop logs in
# by itself (raspi-config B4), and an autostart entry starts the station in
# that session. Screen blanking is turned off so the tap that would wake the
# screen can't press a button.
#
# Run it as the user who will operate the station, NOT with sudo — it asks
# for sudo itself where it needs it. Safe to run again: every file it writes
# is overwritten, nothing is appended twice. Run it with `bash`: nothing in
# this repo is marked executable, and git is told to ignore file modes, so
# `git pull` never refuses over a chmod.

set -e

AUTOSTART=1
APT=1
for arg in "$@"; do
    case "$arg" in
        --autostart)    AUTOSTART=1 ;;   # the default now; kept for old notes
        --no-autostart) AUTOSTART=0 ;;
        --no-apt)       APT=0 ;;
        -h|--help)      sed -n '2,19p' "$0"; exit 0 ;;
        *) echo "unknown option: $arg"; exit 2 ;;
    esac
done

if [ "$(id -u)" -eq 0 ]; then
    echo "Run this as your normal user, not with sudo."
    echo "Under sudo the icons would land in root's desktop, where nobody sees them."
    exit 1
fi

HERE="$(cd "$(dirname "$(readlink -f "$0")")" && pwd)"
echo "Installing from: $HERE"

# ------------------------------------------------------------- file modes
# Older versions of this script ran chmod +x inside the clone. Git counts that
# as a local edit and `git pull` (and the auto-updater) then refuse to touch
# those files. Ignore modes in this clone; nothing here needs the x bit.
git -C "$HERE" config core.fileMode false 2>/dev/null || true

# ---------------------------------------------------------------- packages
if [ "$APT" -eq 1 ]; then
    echo
    echo "Installing packages (needs sudo)…"
    sudo apt-get update -qq
    sudo apt-get install -y -qq python3-pyqt5 python3-serial python3-openpyxl zenity
fi

# ------------------------------------------------------------ serial access
# Without this the station cannot open /dev/ttyUSB0 at all and just shows the
# scale as disconnected — which looks like a cable fault, not a permission one.
NEED_RELOGIN=0
if ! id -nG "$USER" | tr ' ' '\n' | grep -qx dialout; then
    echo
    echo "Adding $USER to the dialout group so the app can open the scale's port…"
    sudo usermod -aG dialout "$USER"
    NEED_RELOGIN=1
fi

# ----------------------------------------------------------------- launchers
APPS="${XDG_DATA_HOME:-$HOME/.local/share}/applications"
DESKTOP="$(xdg-user-dir DESKTOP 2>/dev/null || echo "$HOME/Desktop")"
mkdir -p "$APPS" "$DESKTOP"

write_entry() {   # file  name  comment  args
    cat > "$1" <<EOF
[Desktop Entry]
Type=Application
Version=1.0
Name=$2
Comment=$3
Exec=bash "$HERE/mincecraft.sh"${4:+ $4}
Path=$HERE
Icon=$HERE/mincecraft.png
Terminal=false
Categories=Utility;
StartupNotify=false
EOF
    chmod +x "$1"
    # Some file managers refuse to launch an untrusted .desktop file from the
    # desktop until it is marked trusted. Harmless where it is not needed.
    command -v gio >/dev/null 2>&1 && gio set "$1" metadata::trusted true 2>/dev/null || true
}

write_entry "$APPS/mincecraft.desktop" \
    "MINCECRAFT Station" "DOKI weighing station — real scale" ""
write_entry "$APPS/mincecraft-demo.desktop" \
    "MINCECRAFT Demo" "Weighing station with a simulated scale — no hardware" "--demo"

cp "$APPS/mincecraft.desktop" "$DESKTOP/mincecraft.desktop"
chmod +x "$DESKTOP/mincecraft.desktop"
command -v gio >/dev/null 2>&1 && gio set "$DESKTOP/mincecraft.desktop" metadata::trusted true 2>/dev/null || true

echo
echo "  desktop icon   $DESKTOP/mincecraft.desktop"
echo "  menu entries   MINCECRAFT Station, MINCECRAFT Demo  (Accessories)"

# ---------------------------------------------------------------- autostart
AUTO="$HOME/.config/autostart/mincecraft.desktop"
if [ "$AUTOSTART" -eq 1 ]; then
    mkdir -p "$(dirname "$AUTO")"
    cp "$APPS/mincecraft.desktop" "$AUTO"
    echo "  autostart      on — the station opens when the desktop starts"
    if command -v raspi-config >/dev/null 2>&1; then
        # B4 = boot to desktop, logged in automatically. Without it the
        # autostart entry never runs after a reboot: nobody logs in.
        if sudo raspi-config nonint do_boot_behaviour B4; then
            echo "  auto-login     on — the Pi logs in to the desktop by itself"
        else
            echo "  auto-login     could not be set — use raspi-config > System > Boot / Auto Login"
        fi
        if sudo raspi-config nonint do_blanking 1; then
            echo "  blanking       off — the screen stays on (takes effect after a reboot)"
        fi
    else
        echo "  auto-login     raspi-config not found — set desktop auto-login by hand"
    fi
else
    rm -f "$AUTO"
    echo "  autostart      off"
fi

# ------------------------------------------------------------- recipe sync
# Every 5 minutes, and at every station start, updater.py reads the owner's
# Google Sheet (link in ~/.config/mincecraft/recipes.env), checks it, and
# hands valid recipes to the station. With no link set it falls back to
# DOKI-Recipes.xlsx on GitHub (main). Code only changes from the `stable`
# branch, at station start (see README, "Updates from GitHub").
CONF="${XDG_CONFIG_HOME:-$HOME/.config}/mincecraft"
mkdir -p "$CONF"
if [ ! -f "$CONF/recipes.env" ]; then
    cp "$HERE/recipes.env.example" "$CONF/recipes.env"
    echo "  recipe sheet   created $CONF/recipes.env — paste the sheet link into it"
elif grep -q '^RECIPES_SHEET_URL=PASTE-THE' "$CONF/recipes.env"; then
    echo "  recipe sheet   link not set yet — edit $CONF/recipes.env"
else
    echo "  recipe sheet   link set in $CONF/recipes.env"
fi
UNITDIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
mkdir -p "$UNITDIR"
for u in mincecraft-recipes.service mincecraft-recipes.timer; do
    sed "s|@HERE@|$HERE|g" "$HERE/$u" > "$UNITDIR/$u"
done
if systemctl --user daemon-reload 2>/dev/null && \
   systemctl --user enable --now mincecraft-recipes.timer >/dev/null 2>&1; then
    echo "  recipe sync    on — every 5 min"
else
    echo "  recipe sync    timer not started (no user systemd session?) — re-run after logging in"
fi
if git -C "$HERE" rev-parse -q --verify origin/stable >/dev/null 2>&1; then
    echo "  code updates   from branch 'stable', at station start"
else
    echo "  code updates   off until a 'stable' branch exists on GitHub"
fi

# ------------------------------------------------------------------- check
echo
if python3 -c "import PyQt5, serial, openpyxl" 2>/dev/null; then
    echo "Packages: OK"
else
    echo "Packages: something is still missing — re-run without --no-apt."
fi
if ls /dev/ttyUSB* >/dev/null 2>&1; then
    echo "Scale port: $(ls /dev/ttyUSB* | tr '\n' ' ')"
else
    echo "Scale port: no /dev/ttyUSB* yet — plug in the USB-RS232 lead."
fi

echo
if [ "$NEED_RELOGIN" -eq 1 ]; then
    echo "** Log out and back in (or reboot) once before using the real scale."
    echo "   The dialout group only takes effect in a new login session."
else
    echo "Done. Double-click MINCECRAFT Station on the desktop."
fi
