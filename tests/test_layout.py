#!/usr/bin/env python3
"""The panel has to fit the screen it is given. Every screen, every size.

This exists because it did not. The panel was built at 1024x600 and the scale
factor was floored at 1.0, so on the 800x480 Pi touchscreen the window was
forced to 1024x600 and the compositor simply cropped it: 224 px of buttons off
the right, 120 px off the bottom, unreachable.

The old check asked whether `sizeHint()` fitted, which is the size the layout
would *like*. The size that cannot be negotiated away is `minimumSizeHint()`,
and that is what forces a window larger than the display. This checks the
minimum, on every screen, at every resolution the station might meet.

Run: python3 tests/test_layout.py
"""

import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from PyQt5.QtWidgets import QApplication            # noqa: E402

from scale import BatchLog, Config, DailyRatio, ScaleState, SimScale  # noqa: E402
from panel import DESIGN_H, DESIGN_W, Panel, Step, scale_for          # noqa: E402

# Every display the station plausibly meets, smallest first.
SIZES = [
    (800, 480, "official 7-inch Pi touchscreen"),
    (800, 450, "800x480 with a 30 px taskbar"),
    (1024, 600, "the design size"),
    (1024, 576, "1024x600 with a taskbar"),
    (1280, 800, "a small monitor"),
    (1920, 1080, "a full-HD monitor or TV"),
]

SCREENS = ["HOME", "CAPTURE", "PRODUCT", "REVIEW", "ADD", "MANUAL", "DONE",
           "MENU", "WATER"]

checks = []


def check(label, cond):
    checks.append((label, bool(cond)))
    print(("  PASS  " if cond else "  FAIL  ") + label)


def pump(app, n=6):
    for _ in range(n):
        app.processEvents()


def overflowing(win):
    """Visible children whose right or bottom edge is outside the window."""
    w, h = win.width(), win.height()
    out = []
    for child in win.findChildren(object):
        if not hasattr(child, "isVisible") or not hasattr(child, "mapTo"):
            continue
        if not child.isVisible() or child.window() is not win:
            continue
        if child.width() <= 0 or child.height() <= 0:
            continue
        pos = child.mapTo(win, child.rect().topLeft())
        if pos.x() + child.width() > w + 1 or pos.y() + child.height() > h + 1:
            out.append((type(child).__name__, child.objectName(),
                        pos.x() + child.width(), pos.y() + child.height()))
    return out


def load_a_batch(win, cfg):
    """A real recipe in progress, so every screen has something to draw.

    An empty batch would let the longest screens — the review table, the done
    table — render as a couple of rows and fit anywhere. The tallest content is
    the case that has to fit.
    """
    pid = max((p["id"] for p in cfg.products if not cfg.is_draft(p["id"])),
              key=lambda i: len(cfg.active_ingredients(i)))
    win.st.product = pid
    win.st.base_wt = 10000.0
    # assign_scale returns (key, reason); only the key belongs on the step.
    win.st.steps = []
    for name, pct in cfg.active_ingredients(pid):
        target = round(win.st.base_wt * pct / 100.0, 2)
        key, _why = cfg.assign_scale(target)
        win.st.steps.append(Step(name, pct, target, scale=key))
    for s in win.st.steps:
        s.actual = s.target
    win.st.idx = 0
    win.st.water_ratio = 0.55
    return pid


def main():
    app = QApplication.instance() or QApplication(sys.argv[:1])
    cfg = Config.load(os.path.join(ROOT, "recipes.json"))
    shots = os.path.join(ROOT, "screenshots")
    os.makedirs(shots, exist_ok=True)

    # ------------------------------------------------- the factor itself
    check("a screen smaller than the design size scales down",
          scale_for(800, 480) < 1.0)
    check("and by the dimension that binds, not the generous one",
          abs(scale_for(800, 480) - 800 / DESIGN_W) < 1e-9)
    check("the design size is exactly 1.0", scale_for(1024, 600) == 1.0)
    check("a full-HD monitor scales up", scale_for(1920, 1080) > 1.4)
    check("an absurd screen is still clamped", scale_for(7680, 4320) <= 2.0)
    check("a screen of nothing does not divide by zero",
          scale_for(0, 0) == 1.0)

    for w, h, why in SIZES:
        print(f"\n--- {w}x{h}  ({why}) ---")
        state = ScaleState(stable_band_g=2 * cfg.main.division_g)
        sim = SimScale(division_g=cfg.division_g)
        log = os.path.join(shots, f"_layout_{w}x{h}.jsonl")
        win = Panel(state, cfg, BatchLog(log), DailyRatio(log + ".daily", cfg),
                    sim=sim, scale=scale_for(w, h))
        win.resize(w, h)
        win.show()
        load_a_batch(win, cfg)
        pump(app)

        # The window must actually BE the size asked for. If it silently grew
        # to its minimum, every bound below is measured against the wrong
        # rectangle and the whole file passes while proving nothing.
        check(f"{w}x{h}: the window takes the size it is given "
              f"(got {win.width()}x{win.height()})",
              win.width() == w and win.height() == h)

        mw = win.minimumSizeHint().width()
        mh = win.minimumSizeHint().height()
        check(f"{w}x{h}: the window can shrink to the screen "
              f"(needs {mw}x{mh})", mw <= w and mh <= h)
        check(f"{w}x{h}: the hard minimum fits too "
              f"(min {win.minimumSize().width()}x{win.minimumSize().height()})",
              win.minimumSize().width() <= w and win.minimumSize().height() <= h)

        worst = []
        for key in SCREENS:
            # Reloaded before every screen: HOME clears the batch, so walking
            # the list in order would render the review and done tables empty —
            # two rows fit anywhere, and the check would prove nothing.
            load_a_batch(win, cfg)
            win.show_screen(key)
            pump(app)
            rows = getattr(getattr(win.screens[key], "table", None),
                           "rowCount", lambda: None)()
            if rows is not None and rows < 3:
                worst.append((key, [f"table rendered only {rows} rows"]))
            over = overflowing(win)
            if over:
                worst.append((key, over[:3]))
        if worst:
            for key, items in worst:
                print(f"        {key}: {items}")
        check(f"{w}x{h}: nothing hangs off the edge on any screen", not worst)

        if (w, h) == (800, 480):
            load_a_batch(win, cfg)
            win.show_screen("REVIEW")
            pump(app, 12)
            rev = win.screens["REVIEW"]
            named = [rev.table.item(r, 1).text()
                     for r in range(rev.table.rowCount())
                     if rev.table.item(r, 1)]
            check("800x480: the review list is actually populated "
                  f"({len(named)} rows)", len(named) >= len(win.st.steps))
            win.grab().save(os.path.join(shots, "23-800x480.png"))

        win.close()
        for f in (log, log + ".daily"):
            if os.path.exists(f):
                os.remove(f)

    # --------------------------------- the specific regression, stated plainly
    win = Panel(ScaleState(), cfg, BatchLog(os.path.join(shots, "_l.jsonl")),
                DailyRatio(os.path.join(shots, "_l.daily"), cfg),
                scale=scale_for(800, 480))
    win.resize(800, 480)
    win.show(); pump(app)
    check("the 800x480 regression: minimum width is not pinned to 1024",
          win.minimumSize().width() <= 800)
    check("and minimum height is not pinned to 600",
          win.minimumSize().height() <= 480)
    win.close()
    for f in (os.path.join(shots, "_l.jsonl"), os.path.join(shots, "_l.daily")):
        if os.path.exists(f):
            os.remove(f)

    failed = [c for c, ok in checks if not ok]
    print(f"\n{len(checks) - len(failed)}/{len(checks)} checks passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
