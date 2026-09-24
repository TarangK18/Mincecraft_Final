#!/usr/bin/env bash
# One-time setup of the MINCECRAFT weighing station on a Raspberry Pi.
#
#   bash install-pi.sh               icon on the desktop and in the menu
#   bash install-pi.sh --autostart   ...and start the station at login
#   bash install-pi.sh --no-apt      skip package install (offline re-run)
#
# Run it as the user who will operate the station, NOT with sudo — it asks
# for sudo itself where it needs it. Safe to run again: every file it writes
# is overwritten, nothing is appended twice.

set -e

AUTOSTART=0
APT=1
for arg in "$@"; do
    case "$arg" in
        --autostart) AUTOSTART=1 ;;
        --no-apt)    APT=0 ;;
        -h|--help)   sed -n '2,12p' "$0"; exit 0 ;;
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
chmod +x "$HERE/mincecraft.sh" "$HERE/station.py" "$HERE/install-pi.sh" 2>/dev/null || true

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
Exec="$HERE/mincecraft.sh"${4:+ $4}
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
    echo "  autostart      on — the station opens at login"
elif [ -f "$AUTO" ]; then
    echo "  autostart      left as it was (remove $AUTO to turn it off)"
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
