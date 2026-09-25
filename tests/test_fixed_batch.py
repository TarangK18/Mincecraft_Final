#!/usr/bin/env python3
"""A fixed batch, driven through the real panel on the simulated scale.

Piri Piri Masala: no meat, no weighing step, exactly the grams in the recipe.
The whole batch is run the way an operator would — floor-scale ingredients
poured until the panel auto-accepts, citric acid confirmed from the bench —
and the batch record is read back from the log.

The tub is deliberately NOT zeroed (it reads 850 g) to prove the end-of-batch
reconciliation measures from where the scale started, not from zero.

Run: QT_QPA_PLATFORM=offscreen python3 tests/test_fixed_batch.py
"""

import json
import os
import sys
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from PyQt5.QtWidgets import QApplication, QPushButton  # noqa: E402

from scale import (MAIN, SMALL, BatchLog, Config, DailyRatio,  # noqa: E402
                   ScaleState, SimScale, start_reader)
from panel import Panel  # noqa: E402

SHOTS = os.path.join(ROOT, "screenshots")
LOG = os.path.join(ROOT, "_test_fixed.jsonl")
DAILY = os.path.join(ROOT, "_test_fixed_daily.json")
PID = "piri_piri_masala"
TUB_G = 850.0

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


def button_for(widget, text):
    return next((b for b in widget.findChildren(QPushButton)
                 if text in b.text()), None)


def main():
    os.makedirs(SHOTS, exist_ok=True)
    for f in (LOG, DAILY):
        if os.path.exists(f):
            os.remove(f)

    cfg = Config.load()
    state = ScaleState(stable_band_g=2 * cfg.main.division_g)
    sim = SimScale(division_g=cfg.division_g)
    _t, stop = start_reader(state, sim=sim)
    app = QApplication.instance() or QApplication(sys.argv[:1])
    win = Panel(state, cfg, BatchLog(LOG), DailyRatio(DAILY, cfg), sim=sim)
    win.resize(1024, 600)
    win.show()

    # An empty tub that nobody zeroed.
    sim.set(TUB_G)
    pump(app, 1.0)
    settle(app, state)

    # --------------------------------------------------------- choosing it
    win.screens["HOME"].start_btn.click()
    pump(app, 0.4)
    prod = win.screens["PRODUCT"]
    btn = button_for(prod, "Piri Piri Masala")
    check("Piri Piri Masala is on the product list", btn is not None)
    check("its button says fixed batch and the batch size",
          btn and "fixed batch" in btn.text() and "2.00 kg" in btn.text())
    check("and it can be started", btn and btn.isEnabled())
    win.grab().save(os.path.join(SHOTS, "25-products-with-piri-piri.png"))
    btn.click()
    pump(app, 0.5)

    # ------------------------------------------------ no weighing step at all
    check("choosing it goes straight to recipe review — no meat is weighed",
          win.current == "REVIEW")
    rev = win.screens["REVIEW"]
    check("the review says step 2 of 2 and no meat",
          "Step 2 of 2" in rev.crumb.text() and "no meat" in rev.crumb.text())
    check("the status bar says fixed batch 2.00 kg",
          "fixed batch 2.00 kg" in win.info_lbl.text())

    steps = win.st.steps
    got = {s.name: s.target for s in steps}
    want = dict(cfg.targets_for(PID))
    check("ten ingredients", len(steps) == 10)
    check("every target is exactly the recipe's grams", got == want)
    check("citric acid is weighed on the bench scale",
          next(s for s in steps if s.name == "citric acid").scale == SMALL)
    check("everything else on the floor scale",
          all(s.scale == MAIN for s in steps if s.name != "citric acid"))
    check("floor-scale ingredients come first, bench last",
          steps[-1].name == "citric acid")
    total_row = [rev.table.item(r, 1).text() for r in range(rev.table.rowCount())
                 if rev.table.item(r, 1)
                 and rev.table.item(r, 1).text().startswith("Total")]
    check("the total row says 'Total batch', not 'with meat'",
          total_row == ["Total batch"])
    check("START ADDING is armed", rev.start_btn.isEnabled())
    win.grab().save(os.path.join(SHOTS, "26-piri-piri-review.png"))

    # --------------------------------------------------------- run the batch
    rev.start_btn.click()
    pump(app, 0.5)
    check("the batch starts from where the scale stood (the tub)",
          abs((win.st.start_g or 0) - TUB_G) <= 2)
    add = win.screens["ADD"]
    check("the weighing screen says fixed batch, not meat",
          "Fixed batch" in add.base_line.text()
          and "Meat" not in add.base_line.text())

    for i, s in enumerate(list(steps)):
        if s.scale == SMALL:
            if win.current != "MANUAL":
                break
            sim.add(s.target)
            settle(app, state)
            win.screens["MANUAL"].confirm_btn.click()
            pump(app, 0.5)
            continue
        if win.current != "ADD":
            break
        sim.add(s.target)
        end = time.time() + 15
        while time.time() < end and win.current == "ADD" and win.st.idx == i:
            pump(app, 0.1)

    check("all ten ingredients done and the batch is complete",
          win.current == "DONE")
    win.grab().save(os.path.join(SHOTS, "27-piri-piri-done.png"))

    # --------------------------------------------------------- the record
    with open(LOG, encoding="utf-8") as fh:
        rec = json.loads(fh.read().strip().splitlines()[-1])
    check("the log says it was a fixed batch", rec.get("batch") == "fixed")
    check("with no base weight — None, not 0", rec.get("base_weight_g") is None
          and "base_weight_g" in rec)
    check("and no meat", rec.get("base") is None)
    check("the product is Piri Piri Masala", rec.get("product") == PID)
    check("every ingredient recorded with its exact target",
          {s["name"]: s["target_g"] for s in rec["steps"]}
          == {n: round(t, 2) for n, t in want.items()})
    cit = next(s for s in rec["steps"] if s["name"] == "citric acid")
    check("citric acid is logged as bench-weighed and assumed",
          cit["weighed_on"] == SMALL and cit["assumed"] is True)
    recon = rec["reconciliation"]
    check("the batch reconciles, measured from the tub, not from zero "
          f"(expected {recon.get('expected_g')}, saw {recon.get('observed_g')})",
          recon.get("available") and recon.get("ok"))

    # --------------------------------------- meat recipes are unchanged
    win.new_batch()
    pump(app, 0.4)
    win.screens["HOME"].start_btn.click()
    pump(app, 0.3)
    button_for(win.screens["PRODUCT"], "Masala Jerky").click()
    pump(app, 0.4)
    check("a meat recipe still goes to the weighing step first",
          win.current == "CAPTURE")

    stop.set()
    win.close()
    for f in (LOG, DAILY):
        if os.path.exists(f):
            os.remove(f)

    failed = [c for c, ok in checks if not ok]
    print(f"\n{len(checks) - len(failed)}/{len(checks)} checks passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
