#!/usr/bin/env python3
"""Papain: the last step of a jerky batch, decided by the meat.

After every other ingredient the station asks Buffalo / Chicken / Something
else. Buffalo adds papain at 6 g per kg of the captured meat, chicken at 2 g
per kg, anything else adds nothing. The vinegar bath no longer has papain at
all. The earlier ingredients are marked done directly so each case gets
straight to the question; the papain itself is weighed on the simulated scale.

Run: QT_QPA_PLATFORM=offscreen python3 tests/test_papain.py
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
from panel import Panel, Step  # noqa: E402

SHOTS = os.path.join(ROOT, "screenshots")
LOG = os.path.join(ROOT, "_test_papain.jsonl")
DAILY = os.path.join(ROOT, "_test_papain_daily.json")
PID = "pepper_jerky"
MEAT_G = 3000.0

checks = []


def check(label, cond):
    checks.append((label, bool(cond)))
    print(("  PASS  " if cond else "  FAIL  ") + label)


def pump(app, seconds=0.3):
    end = time.time() + seconds
    while time.time() < end:
        app.processEvents()
        time.sleep(0.01)


def last_record():
    with open(LOG, encoding="utf-8") as fh:
        return json.loads(fh.read().strip().splitlines()[-1])


def all_but_papain_done(win, cfg, pid=PID, base=MEAT_G):
    """A batch whose every recipe ingredient is in, ready for the question."""
    win.st.reset()
    win.st.product = pid
    win.st.base = cfg.meat_of(pid)
    win.st.base_wt = base
    win.st.start_g = 0.0
    win.st.steps = []
    for n, t in cfg.targets_for(pid, base):
        s = Step(n, None, t, scale=cfg.scale_for(t) or MAIN)
        s.actual, s.assumed, s.verified = t, False, True
        win.st.steps.append(s)
    win.st.idx = len(win.st.steps) - 1


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
    pump(app, 1.0)

    # ---------------------------------------------- shown ahead, on review
    print("--- review ---")
    all_but_papain_done(win, cfg)
    for s in win.st.steps:
        s.actual = None
    win.st.meat_type = None
    win.show_screen("REVIEW")
    pump(app, 0.3)
    rows = [" | ".join(win.screens["REVIEW"].table.item(r, c).text()
                       for c in range(5) if win.screens["REVIEW"].table.item(r, c))
            for r in range(win.screens["REVIEW"].table.rowCount())]
    check("review warns papain comes last, asked by meat",
          any("ASKED AT THE END" in r for r in rows))
    check("with the buffalo amount (6 g/kg of 3 kg = 18 g)",
          any("if buffalo" in r and "18" in r for r in rows))
    check("and the chicken amount (2 g/kg of 3 kg = 6 g)",
          any("if chicken" in r and "6" in r for r in rows))
    check("but it is not a step, and not in the total",
          all(s.name != "Papain" for s in win.st.steps))
    win.grab().save(os.path.join(SHOTS, "30-review-papain.png"))

    # ---------------------------------------------------------- buffalo
    print("--- buffalo ---")
    sim.zero()
    pump(app, 0.8)
    all_but_papain_done(win, cfg)
    win.next_step()
    pump(app, 0.3)
    check("after the last ingredient: the meat question", win.current == "MEAT")
    win.grab().save(os.path.join(SHOTS, "31-meat-question.png"))
    win.screens["MEAT"].buttons["buffalo"].click()
    pump(app, 0.4)
    pap = win.st.steps[-1]
    check("buffalo adds papain as the very last step", pap.name == "Papain")
    check("at 6 g per kg: 18 g for 3 kg", abs(pap.target - 18.0) < 1e-6)
    check("and it is weighed like any other ingredient", win.current == "ADD")
    sim.add(18)
    end = time.time() + 15
    while time.time() < end and win.current == "ADD":
        pump(app, 0.1)
    check("then the batch is complete", win.current == "DONE")
    rec = last_record()
    check("logged: meat buffalo, papain last, measured",
          rec["meat_type"] == "buffalo" and rec["steps"][-1]["name"] == "Papain"
          and abs(rec["steps"][-1]["target_g"] - 18.0) < 1e-6
          and rec["steps"][-1]["assumed"] is False)

    # ---------------------------------------------------------- chicken
    print("--- chicken ---")
    all_but_papain_done(win, cfg)
    win.next_step()
    pump(app, 0.3)
    win.screens["MEAT"].buttons["chicken"].click()
    pump(app, 0.3)
    check("chicken adds papain at 2 g per kg: 6 g for 3 kg",
          win.st.steps[-1].name == "Papain"
          and abs(win.st.steps[-1].target - 6.0) < 1e-6)
    check("6 g is on the main scale (from 4 g up)", win.st.steps[-1].scale == MAIN)

    # --------------------------------------------------- something else
    print("--- something else ---")
    all_but_papain_done(win, cfg)
    n_before = len(win.st.steps)
    win.next_step()
    pump(app, 0.3)
    check("something else offers no papain",
          "no papain" in win.screens["MEAT"].buttons["other"].text())
    win.screens["MEAT"].buttons["other"].click()
    pump(app, 0.3)
    check("goes straight to done", win.current == "DONE")
    check("with no papain step added", len(win.st.steps) == n_before)
    rec = last_record()
    check("logged: meat 'other', no papain",
          rec["meat_type"] == "other"
          and all(s["name"] != "Papain" for s in rec["steps"]))

    # ------------------------------------ products without papain never ask
    print("--- no question where it does not apply ---")
    for pid, base in (("vinegar_bath", MEAT_G), ("piri_piri_masala", 0.0)):
        all_but_papain_done(win, cfg, pid, base)
        win.next_step()
        pump(app, 0.3)
        check(f"{pid}: finishes without asking", win.current == "DONE")
        check(f"{pid}: meat_type left empty", last_record()["meat_type"] is None)

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
