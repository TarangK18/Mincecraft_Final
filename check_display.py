#!/usr/bin/env python3
"""Measure the display, rather than assuming things about it.

  python3 check_display.py

Run it **on the Pi's own screen**, not over SSH — an SSH session has no
display attached and Qt aborts with "could not connect to display". If you must
run it remotely, borrow the desktop session:

  DISPLAY=:0 python3 check_display.py                                  # X11
  XDG_RUNTIME_DIR=/run/user/$(id -u) WAYLAND_DISPLAY=wayland-0 \
      python3 check_display.py                                         # Wayland

The panel is drawn at 1024x600 and multiplied by a scale factor to fit the real
screen. Getting that factor wrong is not a cosmetic problem: too large and the
buttons sit off the edge of the display where nobody can press them. This
reports what the factor will actually be, and why.
"""

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

DESIGN_W, DESIGN_H = 1024, 600


def framebuffer():
    """The kernel's view, which works over SSH when Qt does not."""
    try:
        with open("/sys/class/graphics/fb0/virtual_size") as fh:
            w, h = fh.read().strip().split(",")
            return int(w), int(h)
    except Exception:
        return None


def main():
    fb = framebuffer()
    if fb:
        print(f"framebuffer (fb0): {fb[0]}x{fb[1]}")

    try:
        from PyQt5.QtWidgets import QApplication
    except ImportError:
        print("PyQt5 is not installed:  sudo apt install python3-pyqt5")
        return 2

    try:
        from panel import scale_for
    except ImportError:
        def scale_for(w, h):
            return max(0.55, min(2.0, min(w / DESIGN_W, h / DESIGN_H)))

    if not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")):
        print("\nNo DISPLAY or WAYLAND_DISPLAY is set — this looks like an SSH")
        print("session. Qt cannot measure a screen it cannot reach. Run this on")
        print("the Pi's own screen, or see the header of this file.")
        if not fb:
            return 1
        print("\nGoing on the framebuffer size alone:")
        w, h = fb
        print(f"  scale factor -> {scale_for(w, h):.3f}")
        return 0

    app = QApplication(sys.argv[:1])
    for s in app.screens():
        g, a = s.geometry(), s.availableGeometry()
        print(f"\n{s.name() or 'screen'}")
        print(f"  resolution      {g.width()}x{g.height()}")
        print(f"  usable          {a.width()}x{a.height()}")
        print(f"  device pixels   {s.devicePixelRatio():g}")
        print(f"  logical DPI     {s.logicalDotsPerInch():.0f}")

        taken = (g.width() - a.width(), g.height() - a.height())
        if any(taken):
            print(f"  -> a taskbar or dock is taking {taken[0]}x{taken[1]} px. "
                  f"The panel uses the usable area, not the full screen.")

        factor = scale_for(a.width(), a.height())
        print(f"\n  scale factor -> {factor:.3f}")
        print(f"  panel drawn at {int(DESIGN_W * factor)}x{int(DESIGN_H * factor)}"
              f" inside {a.width()}x{a.height()}")

        if factor < 1.0:
            print(f"  this screen is smaller than the {DESIGN_W}x{DESIGN_H} design"
                  f" size, so everything shrinks to fit.")
            base = round(15 * factor)
            print(f"  body text ends up about {base}px. Below ~11px it is hard to"
                  f" read at arm's length on a 7-inch panel.")
        elif factor > 1.0:
            print("  larger than the design size, so type and controls grow to "
                  "match rather than sitting small in one corner.")

        if fb and (fb[0] != g.width() or fb[1] != g.height()):
            print(f"\n  NOTE: the framebuffer is {fb[0]}x{fb[1]} but Qt sees "
                  f"{g.width()}x{g.height()}.")

    print("\nIf these numbers look right but the panel is still cropped on the")
    print("physical display, the cause is HDMI overscan — the Pi is drawing the")
    print("full picture and the monitor is cutting the edges off. Fix it in")
    print("/boot/firmware/config.txt with  disable_overscan=1  and reboot.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
