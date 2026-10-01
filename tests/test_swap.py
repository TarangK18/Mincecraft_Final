#!/usr/bin/env python3
"""The container swap between weighing the meat and adding the ingredients.

The operator TAREs the scale with the empty meat container, weighs the meat,
captures it, then takes meat and container off and puts a different, empty
container on. Because of that TARE, the scale reads NEGATIVE once the meat's
container is off. These drive the swap screen with exact readings — including
the negative ones the simulated scale cannot produce — and then run a whole
batch through the swap on the simulator.

Run: QT_QPA_PLATFORM=offscreen python3 tests/test_swap.py
"""

import json
import os
import sys
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from PyQt5.QtWidgets import QApplication  # noqa: E402

from scale import (MAIN, BatchLog, Config, DailyRatio, ScaleState,  # noqa: E402
                   SimScale, start_reader)
from panel import Panel  # noqa: E402

SHOTS = os.path.join(ROOT, "screenshots")
LOG = os.path.join(ROOT, "_test_swap.jsonl")
DAILY = os.path.join(ROOT, "_test_swap_daily.json")

checks = []


def check(label, cond):
    checks.append((label, bool(cond)))
    print(("  PASS  " if cond else "  FAIL  ") + label)


def pump(app, seconds=0.3):
    end = time.time() + seconds
    while time.time() < end:
        app.processEvents()
        time.sleep(0.01)


def settle(app, state, seconds=8):
    end = time.time() + seconds
    run = 0
    while time.time() < end:
        run = run + 1 if state.snapshot()["stable"] else 0
        if run >= 3:
            pump(app, 0.4)
            return True
        pump(app, 0.1)
    return False


def snap(grams, stable=True, live=True):
    return {"grams": grams, "stable": stable, "fresh": live}


def cleanup():
    for f in (LOG, DAILY):
        if os.path.exists(f):
            os.remove(f)


def main():
    os.makedirs(SHOTS, exist_ok=True)
    cleanup()
    cfg = Config.load()
    app = QApplication.instance() or QApplication(sys.argv[:1])

    # ------------------------------------ the screen, with exact readings
    print("--- the swap screen, reading by reading ---")
    win = Panel(ScaleState(), cfg, BatchLog(LOG), DailyRatio(DAILY, cfg))
    win.resize(1024, 600)
    win.show()
    win.st.product = "masala_jerky"
    win.st.base = cfg.meat_of("masala_jerky")
    win.st.base_wt = 3200.0
    win.show_screen("SWAP")
    sw = win.screens["SWAP"]
    pump(app, 0.2)

    sw.tick(snap(3200), True)
    check("meat still on: CONTINUE locked", not sw.cont_btn.isEnabled())
    check("and the screen says to take it off",
          "Take the chicken off" in sw.step_off.text())
    sw.tick(snap(3100, stable=False), True)
    check("lifting it a little is not 'off'", not sw.meat_off)

    # Tared on the meat's 1,150 g container, so with it off the scale reads
    # -1,150 g. That is what a correct swap looks like.
    sw.tick(snap(-1150, stable=False), True)
    check("a negative reading after TARE counts as the meat being off",
          sw.meat_off)
    check("but CONTINUE waits for the reading to settle",
          not sw.cont_btn.isEnabled())
    sw.tick(snap(-250), True)          # lighter ingredient container on
    check("empty container on and steady: CONTINUE arms",
          sw.cont_btn.isEnabled())
    check("with both steps ticked off",
          "✓" in sw.step_off.text() and "✓" in sw.step_on.text())
    check("and no raw number on screen to alarm anyone",
          "-250" not in sw.step_on.text() and "−250" not in sw.step_on.text())
    sw.tick(snap(-250, stable=False), True)
    check("a knock while waiting locks it again", not sw.cont_btn.isEnabled())
    sw.tick(snap(4000), True)          # a heavy container, above the meat
    check("once the meat has been seen off, a heavy container still counts",
          sw.cont_btn.isEnabled())
    sw.tick(snap(None, live=False), False)
    check("scale gone: locked, and it says so",
          not sw.cont_btn.isEnabled() and "Waiting" in sw.step_off.text())

    # Re-entering starts over: a second visit must see the meat come off again.
    win.show_screen("SWAP")
    sw.tick(snap(3200), True)
    check("coming back to the screen starts the check over",
          not sw.meat_off and not sw.cont_btn.isEnabled())
    win.close()

    # --------------------------------------- a whole batch through the swap
    print("--- a whole batch, meat weighed then swapped out ---")
    state = ScaleState(stable_band_g=2 * cfg.main.division_g)
    sim = SimScale(division_g=cfg.division_g)
    _t, stop = start_reader(state, sim=sim)
    win = Panel(state, cfg, BatchLog(LOG), DailyRatio(DAILY, cfg), sim=sim)
    win.resize(1024, 600)
    win.show()
    pump(app, 1.0)

    pid = "vinegar_bath"               # short: one ingredient, no papain step
    win.screens["HOME"].start_btn.click()
    pump(app, 0.3)
    win.choose_product(pid)
    pump(app, 0.3)
    check("a meat recipe weighs the meat first", win.current == "CAPTURE")
    check("the capture screen tells them to TARE with the empty container",
          "TARE" in win.screens["CAPTURE"].hint.text())
    sim.set(5000)
    settle(app, state)
    win.screens["CAPTURE"].cap_btn.click()
    pump(app, 0.4)
    check("capture goes to the swap", win.current == "SWAP")

    sim.zero()
    pump(app, 0.6)
    sim.set(700)                       # the empty ingredient container
    settle(app, state)
    check("on the live scale, CONTINUE arms after the swap",
          win.screens["SWAP"].cont_btn.isEnabled())
    win.grab().save(os.path.join(SHOTS, "28-swap.png"))
    win.screens["SWAP"].cont_btn.click()
    pump(app, 0.4)
    check("then the review", win.current == "REVIEW")
    # From the weight actually captured (the sim dithers by a gram, so it may
    # be 4,999 or 5,001), and certainly not from the 700 g container now on.
    captured = win.st.base_wt
    want = dict(cfg.targets_for(pid, captured))
    check(f"targets come from the captured {captured:.0f} g meat, not from the "
          f"container on the scale",
          abs(captured - 5000) <= 2
          and {s.name: round(s.target, 2) for s in win.st.steps}
          == {n: round(t, 2) for n, t in want.items()})

    win.screens["REVIEW"].start_btn.click()
    pump(app, 0.5)
    add = win.screens["ADD"]
    shown = add.big.text()
    print(f"         first ingredient reads: {shown!r}")
    check("the first ingredient starts from zero in the new container "
          "(within the scale's 1 g dither)",
          abs(float(shown.split("/")[0])) <= 2 * cfg.main.division_g)
    check("the ingredients' zero is the empty container",
          abs((win.st.start_g or 0) - 700) <= 2)

    for i, s in enumerate(list(win.st.steps)):
        if win.current != "ADD":
            break
        sim.add(s.target)
        end = time.time() + 15
        while time.time() < end and win.current == "ADD" and win.st.idx == i:
            pump(app, 0.1)
    check("the batch completes", win.current == "DONE")
    check("with no container-removed alarm along the way",
          not win.dialog_open)

    with open(LOG, encoding="utf-8") as fh:
        rec = json.loads(fh.read().strip().splitlines()[-1])
    check("the log keeps the meat weight", abs(rec["base_weight_g"] - 5000) <= 2)
    check("and the container's zero", abs((rec.get("start_g") or 0) - 700) <= 2)
    check("all on the main scale", all(s["weighed_on"] == MAIN for s in rec["steps"]))
    r = rec["reconciliation"]
    check("the batch total reconciles against the ingredient container, "
          f"not the meat (expected {r.get('expected_g')}, saw {r.get('observed_g')})",
          r.get("available") and r.get("ok"))

    stop.set()
    win.close()
    cleanup()
    failed = [c for c, ok in checks if not ok]
    print(f"\n{len(checks) - len(failed)}/{len(checks)} checks passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
