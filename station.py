#!/usr/bin/env python3
"""
DOKI weighing station (MINCECRAFT) — entry point.

THIS VERSION: v1.2 (2026-10-27)

What it does now, as a whole:
  - Reads the main scale (RS232, 9600 8N1, /dev/ttyUSB0) on its own thread;
    the PyQt5 panel polls it every 100 ms and dims any reading older than 2 s.
  - Runs one batch at a time on the 7-inch touchscreen: pick the product,
    weigh the meat (or type its weight behind the supervisor PIN), swap to the
    empty ingredient container, review every target, weigh each ingredient
    with auto-advance in tolerance, papain last by meat type for jerky.
    Piri Piri Masala is a fixed batch with no meat.
  - Recipes: starts with the recipes synced from the owner's Google Sheet
    (~/.local/share/mincecraft/recipes.json, written by updater.py) if that
    file is valid, otherwise the repo's recipes.json. While on the HOME
    screen with no batch open it checks every 5 s for a newer synced file and
    switches to it — never mid-batch. HOME shows what the updater last did
    (recipes updated, a rejected workbook, a rolled-back software update).
  - Writes one line per finished batch to batches.jsonl (append-only),
    including "clock_synced" (NTP-synced at both start and finish),
    "recipe_version" (id of the exact recipe used), "recipes_commit" and
    "recipes_source" (google-sheet / workbook ref / repo), and
    rebuilds consumption.xlsx from it.
  - Starts by itself after a reboot (desktop auto-login + autostart, set up
    by install-pi.sh); mincecraft.sh runs updater.py first.
  - On failure: a dead or unplugged scale shows NO SCALE / STALE and disables
    weighing; the reader reconnects every 2 s. Nothing is lost from the log.
  - What it does NOT do: no network at all. Sending batches to the server is
    uploader.py, and fetching recipes / code is updater.py — separate
    processes. The station works the same with or without them.

  python3 station.py                    # real scale on /dev/ttyUSB0, fullscreen
  python3 station.py --sim              # simulated scale, on-screen +/- buttons
  python3 station.py --windowed         # windowed, for development
  python3 station.py --port /dev/ttyUSB1

CHANGES (newest first)
  v1.2  2026-10-27  Recipes come from the owner's Google Sheet (via
                    updater.py v1.1): HOME wording names the sheet, says
                    when it is unreachable, and lists products not taken
                    because of a mistake; batch lines gain recipes_source.
  v1.1  2026-10-27  Recipes follow the owner's workbook on GitHub: uses the
                    synced recipes file when valid, reloads it between
                    batches, shows update news on HOME, records
                    recipe_version / recipes_commit per batch. Opens at boot.
                    Why: the owner's repo becomes the master copy and his
                    recipe edits must reach the Pi without anyone touching it.
  v1.0  2026-10-21  First versioned release. Adds "clock_synced" and
                    "station_version" to each batch line so the uploader never
                    sends a guessed time (the Pi has no RTC). Version shown in
                    the status bar and printed at start. Includes everything
                    before versioning: typed meat weight (10-08), papain by
                    meat, container swap, 4 g routing, larger text (09-25),
                    Piri Piri Masala fixed batch, 800x480 scaling (09-24).
"""

import argparse
import os
import sys

from scale import (LOCAL_RECIPES, UPDATE_STATUS, BatchLog, Config, DailyRatio,
                   ScaleState, SimScale, pick_recipes, start_reader)

HERE = os.path.dirname(os.path.abspath(__file__))

FW_VERSION = "v1.2"


def main(argv=None):
    ap = argparse.ArgumentParser(description="DOKI weighing station")
    ap.add_argument("--port", default="/dev/ttyUSB0")
    ap.add_argument("--baud", type=int, default=9600)
    ap.add_argument("--sim", action="store_true", help="simulated scale, no hardware")
    ap.add_argument("--demo", action="store_true",
                    help="--sim, windowed, today's water ratio pre-set, and "
                         "scratch log files — for trying the flow end to end "
                         "with no hardware and nothing to clean up afterwards")
    ap.add_argument("--windowed", action="store_true", help="do not go fullscreen")
    ap.add_argument("--recipes", default=None,
                    help="recipes file (default: the synced one if valid, "
                         "else the repo's recipes.json)")
    ap.add_argument("--batch-log", default=os.path.join(HERE, "batches.jsonl"))
    ap.add_argument("--daily", default=os.path.join(HERE, "daily.json"))
    ap.add_argument("--version", action="version",
                    version=f"MINCECRAFT station {FW_VERSION}")
    args = ap.parse_args(argv)
    print(f"MINCECRAFT weighing station {FW_VERSION}")

    if args.demo:
        # A demo should not touch the real batch log, and should not stop at
        # the water gate before the operator has seen anything.
        import tempfile
        args.sim = args.windowed = True
        scratch = os.path.join(tempfile.gettempdir(), "doki-demo")
        os.makedirs(scratch, exist_ok=True)
        args.batch_log = os.path.join(scratch, "batches.jsonl")
        args.daily = os.path.join(scratch, "daily.json")

    start_note = None
    watch = None
    if args.recipes is None and not args.demo:
        args.recipes, why = pick_recipes(os.path.join(HERE, "recipes.json"),
                                         LOCAL_RECIPES)
        watch = LOCAL_RECIPES                  # pick up newer syncs later
        if why:
            print(why)
            start_note = ("warn", why[0].upper() + why[1:] + ".")
    elif args.recipes is None:
        args.recipes = os.path.join(HERE, "recipes.json")
    print(f"recipes: {args.recipes}")
    cfg = Config.load(args.recipes)
    state = ScaleState(stable_band_g=2 * cfg.main.division_g)
    batches = BatchLog(args.batch_log)
    daily = DailyRatio(args.daily, cfg)

    problems = cfg.validate_products()
    if problems:
        print("recipes.json is not usable:")
        for p in problems:
            print(f"  {p}")
        return 2
    if args.demo:
        # Only pre-set the ratio if some recipe actually derives water from
        # flour; otherwise there is nothing for it to unlock.
        if cfg.any_water_gated and not daily.is_set():
            gated = next(p["id"] for p in cfg.products if cfg.water_gated(p["id"]))
            daily.set(round(cfg.nominal_water_ratio(gated) or 0.55, 2))
            print(f"demo: today's water ratio pre-set to {daily.ratio()}")
        print(f"demo: logs in {os.path.dirname(args.batch_log)}")
        usable = [p["name"] for p in cfg.products if not cfg.is_draft(p["id"])]
        print(f"demo: recipes with ingredients — {', '.join(usable)}")

    sim = SimScale(division_g=cfg.division_g) if args.sim else None
    _thread, stop = start_reader(state, port=args.port, baud=args.baud, sim=sim)

    # Imported after the arg parse so --help works on a machine with no Qt.
    from PyQt5.QtCore import Qt
    from PyQt5.QtWidgets import QApplication
    from panel import DESIGN_H, DESIGN_W, Panel, scale_for

    app = QApplication(sys.argv[:1])
    app.setApplicationName("MINCECRAFT Weighing Station")

    # Measure the display before building anything. Every fixed height and
    # margin in the panel is computed once, at construction, from this factor,
    # so it has to be right before the first widget exists — and the panel is
    # only ever as large as the screen it is drawn on.
    screen = app.primaryScreen()
    full, avail = screen.geometry(), screen.availableGeometry()
    if args.windowed:
        # A window has to share the screen with the taskbar, so it gets the
        # usable area — capped at the design size so a development window does
        # not swallow a large monitor.
        w = min(DESIGN_W, avail.width())
        h = min(DESIGN_H, avail.height())
    else:
        # Fullscreen covers the taskbar, so the whole screen is the panel's.
        w, h = full.width(), full.height()
    factor = scale_for(w, h)
    print(f"display: {full.width()}x{full.height()} "
          f"(usable {avail.width()}x{avail.height()}) -> panel {w}x{h}, "
          f"scale {factor:.3f}")

    window = Panel(state, cfg, batches, daily, sim=sim, scale=factor,
                   version=FW_VERSION, recipes_path=watch,
                   status_path=None if args.demo else UPDATE_STATUS,
                   start_note=start_note)
    if args.windowed:
        window.resize(w, h)
        window.show()
    else:
        window.setCursor(Qt.BlankCursor)
        window.showFullScreen()

    source = "SIMULATED scale" if sim else f"{args.port} @ {args.baud} 8N1"
    print(f"weighing station: {source}")

    try:
        return app.exec_()
    finally:
        stop.set()


if __name__ == "__main__":
    sys.exit(main())
