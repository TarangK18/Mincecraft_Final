#!/usr/bin/env python3
"""The batch uploader: mapping, identity, resends and every server answer.

Batch lines are written by the REAL station code (Panel.record_batch), with
the clock in IST, so the mapping is tested against what the Pi actually
writes. The server is tests/mock_ingest.py, a local stand-in that follows
the /ingest contract (stored / duplicate / rejected; 200 / 207 / 422; 401,
400, 413, 5xx). Nothing here touches the network beyond 127.0.0.1.

Run: QT_QPA_PLATFORM=offscreen python3 tests/test_uploader.py
"""

import hashlib
import json
import os
import shutil
import sys
import tempfile
import time
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ["TZ"] = "Asia/Kolkata"           # the Pi's zone: +0530
time.tzset()

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tests"))

import scale  # noqa: E402
import uploader  # noqa: E402
from mock_ingest import MockIngest  # noqa: E402

KEY = "test-write-key-0123456789"
RECIPES = os.path.join(ROOT, "recipes.json")


def quiet():
    uploader.log.handlers.clear()
    uploader.log.propagate = False


# ------------------------------------------------- lines from the station

_LINES = {}
_APP = None


def station_lines():
    """Three batches written by the real panel: weighed per_meat (Teriyaki,
    buffalo, with a small-scale step), fixed (Piri Piri), typed weight
    (Pepper, chicken). Plus one unsynced and one pre-flag legacy line."""
    if _LINES:
        return _LINES
    from PyQt5.QtWidgets import QApplication
    from scale import (MAIN, SMALL, BatchLog, Config, DailyRatio,
                       ScaleState)
    from panel import Panel, Step
    global _APP
    _APP = QApplication.instance() or QApplication(sys.argv[:1])
    tmp = tempfile.mkdtemp(prefix="_test_upload_station_")
    log_path = os.path.join(tmp, "batches.jsonl")
    cfg = Config.load(RECIPES)
    win = Panel(ScaleState(), cfg, BatchLog(log_path),
                DailyRatio(os.path.join(tmp, "daily.json"), cfg), version="v1.0")
    win.refresh_consumption = lambda: None
    win.clock_check = lambda: True

    def run(pid, base_g, typed=False, meat=None, clock=(True, True)):
        win.st.reset()
        seq = iter(clock)
        win.clock_check = lambda: next(seq)
        win.start_batch()
        win.st.product = pid
        fixed = cfg.is_fixed_batch(pid)
        win.st.base = None if fixed else cfg.meat_of(pid)
        win.st.base_wt = 0.0 if fixed else base_g
        win.st.base_typed = typed
        win.st.start_g = 0.0
        win.st.steps = []
        for n, t in cfg.targets_for(pid, None if fixed else base_g):
            sc = cfg.scale_for(t) or MAIN
            s = Step(n, None, t, scale=sc)
            s.actual = t if sc == SMALL else round(t + 0.4)
            s.assumed = sc == SMALL
            s.verified = None if sc == SMALL else True
            win.st.steps.append(s)
        if meat:
            win.st.meat_type = meat
            t = cfg.papain_target(pid, meat, base_g)
            if t:
                s = Step(cfg.papain_name, None, t, scale=cfg.scale_for(t) or MAIN)
                s.actual, s.verified = t, True
                win.st.steps.append(s)
        win.record_batch()
        with open(log_path, encoding="utf-8") as fh:
            return fh.read().splitlines()[-1]

    _LINES["per_meat"] = run("teriyaki_jerky", 3250, meat="buffalo")
    _LINES["fixed"] = run("piri_piri_masala", 0)
    _LINES["typed"] = run("pepper_jerky", 3250, typed=True, meat="chicken")
    _LINES["unsynced"] = run("pepper_jerky", 2000, meat="other", clock=(False, True))
    late = run("pepper_jerky", 2000, meat="other", clock=(True, False))
    _LINES["synced_late_only"] = late
    # As written by the station before this version: no clock_synced, no
    # base_weight_source, no batch key.
    legacy = json.loads(_LINES["per_meat"])
    for k in ("clock_synced", "base_weight_source", "batch", "station_version"):
        legacy.pop(k, None)
    _LINES["legacy"] = json.dumps(legacy)
    shutil.rmtree(tmp, ignore_errors=True)
    return _LINES


# ------------------------------------------------------------- mapping

class TestTime(unittest.TestCase):
    def test_ist_to_utc(self):
        self.assertEqual(uploader.to_utc("2026-10-20T13:46:30+0530"),
                         "2026-10-20T08:16:30Z")

    def test_colon_offset_and_z(self):
        self.assertEqual(uploader.to_utc("2026-10-20T13:46:30+05:30"),
                         "2026-10-20T08:16:30Z")
        self.assertEqual(uploader.to_utc("2026-10-20T08:16:30Z"),
                         "2026-10-20T08:16:30Z")
        self.assertEqual(uploader.to_utc("2026-10-20T08:16:30+0000"),
                         "2026-10-20T08:16:30Z")

    def test_day_rolls_back(self):
        self.assertEqual(uploader.to_utc("2026-10-21T02:00:00+0530"),
                         "2026-10-20T20:30:00Z")

    def test_naive_or_garbage_is_none_never_a_guess(self):
        for s in ("2026-10-20T13:46:30", "yesterday", "", None, 123):
            self.assertIsNone(uploader.to_utc(s), s)


class TestStationWritesTheFlag(unittest.TestCase):
    def test_new_lines_carry_clock_synced_and_version(self):
        L = station_lines()
        rec = json.loads(L["per_meat"])
        self.assertIs(rec["clock_synced"], True)
        self.assertEqual(rec["station_version"], "v1.0")
        self.assertTrue(rec["started_at"].endswith("+0530"))
        self.assertTrue(rec["logged_at"].endswith("+0530"))

    def test_unsynced_at_start_or_finish_is_false(self):
        L = station_lines()
        self.assertIs(json.loads(L["unsynced"])["clock_synced"], False)
        self.assertIs(json.loads(L["synced_late_only"])["clock_synced"], False)

    def test_clock_synced_reads_the_timesyncd_flag(self):
        tmp = tempfile.mkdtemp()
        flag = os.path.join(tmp, "synchronized")
        old = scale.TIMESYNC_FLAG
        try:
            scale.TIMESYNC_FLAG = flag
            open(flag, "w").close()
            self.assertTrue(scale.clock_synced())
        finally:
            scale.TIMESYNC_FLAG = old
            shutil.rmtree(tmp)

    def test_clock_synced_false_off_linux(self):
        old = sys.platform
        try:
            sys.platform = "win32"
            self.assertFalse(scale.clock_synced())
        finally:
            sys.platform = old


class TestMapping(unittest.TestCase):
    names = uploader.product_names(RECIPES)

    def m(self, which, seq=7):
        rec = json.loads(station_lines()[which])
        return rec, uploader.map_record(rec, seq, "0f3c9a21", "weigh-1", self.names)

    def test_envelope(self):
        rec, msg = self.m("per_meat", seq=57)
        self.assertEqual(set(msg), {"device", "boot_id", "seq", "ts", "type", "data"})
        self.assertEqual(msg["device"], "weigh-1")
        self.assertEqual(msg["boot_id"], "0f3c9a21")
        self.assertEqual(msg["seq"], 57)
        self.assertEqual(msg["type"], "batch")
        self.assertEqual(msg["data"]["batch_id"], "0f3c9a21-57")
        self.assertEqual(set(msg["data"]), {
            "batch_id", "product", "batch_type", "base_weight_g", "base_source",
            "meat_type", "started_at", "finished_at", "steps"})

    def test_per_meat_weighed(self):
        rec, msg = self.m("per_meat")
        d = msg["data"]
        self.assertEqual(d["product"], self.names["teriyaki_jerky"])
        self.assertEqual(d["batch_type"], "per_meat")
        self.assertEqual(d["base_weight_g"], 3250)
        self.assertIsInstance(d["base_weight_g"], int)
        self.assertEqual(d["base_source"], "scale")
        self.assertEqual(d["meat_type"], "buffalo")
        # IST in the line, UTC with Z on the wire; ts is the finish time.
        self.assertEqual(d["started_at"], uploader.to_utc(rec["started_at"]))
        self.assertEqual(d["finished_at"], uploader.to_utc(rec["logged_at"]))
        self.assertTrue(d["finished_at"].endswith("Z"))
        self.assertEqual(msg["ts"], d["finished_at"])

    def test_steps_in_order_with_measured(self):
        rec, msg = self.m("per_meat")
        steps = msg["data"]["steps"]
        self.assertEqual([s["step_no"] for s in steps], list(range(1, len(rec["steps"]) + 1)))
        self.assertEqual([s["ingredient"] for s in steps], [s["name"] for s in rec["steps"]])
        small = [s for s in steps if s["scale"] == "small"]
        self.assertTrue(small, "Teriyaki at 3.25 kg should have a small-scale step")
        self.assertTrue(all(s["measured"] is False for s in small))
        self.assertTrue(all(s["measured"] is True for s in steps if s["scale"] == "main"))
        self.assertEqual(steps[-1]["ingredient"], "Papain")
        self.assertAlmostEqual(steps[-1]["target_g"], 19.5)
        for s in steps:
            self.assertEqual(set(s), {"step_no", "ingredient", "scale", "target_g",
                                      "actual_g", "measured"})

    def test_fixed_batch(self):
        rec, msg = self.m("fixed")
        d = msg["data"]
        self.assertEqual(d["batch_type"], "fixed")
        self.assertIsNone(d["base_weight_g"])
        self.assertIsNone(d["base_source"])
        self.assertIsNone(d["meat_type"])
        self.assertEqual(d["product"], "Piri Piri Masala")
        self.assertEqual(len(d["steps"]), 10)

    def test_typed_weight(self):
        rec, msg = self.m("typed")
        d = msg["data"]
        self.assertEqual(d["base_source"], "typed")
        self.assertEqual(d["base_weight_g"], 3250)
        self.assertEqual(d["meat_type"], "chicken")
        self.assertAlmostEqual(d["steps"][-1]["target_g"], 6.5)

    def test_unsynced_sends_no_times(self):
        for which in ("unsynced", "synced_late_only"):
            rec, msg = self.m(which)
            self.assertIsNone(msg["ts"])
            self.assertIsNone(msg["data"]["started_at"])
            self.assertIsNone(msg["data"]["finished_at"])

    def test_legacy_line_without_flag(self):
        rec, msg = self.m("legacy")
        self.assertNotIn("clock_synced", rec)
        d = msg["data"]
        self.assertIsNone(msg["ts"])
        self.assertIsNone(d["started_at"])
        self.assertIsNone(d["finished_at"])
        self.assertEqual(d["batch_type"], "per_meat")    # inferred
        self.assertEqual(d["base_source"], "scale")      # typed did not exist

    def test_skipped_step_is_not_measured(self):
        rec = json.loads(station_lines()["per_meat"])
        rec["steps"][0]["skipped"] = True
        rec["steps"][0]["actual_g"] = None
        msg = uploader.map_record(rec, 1, "0f3c9a21", "weigh-1", self.names)
        s = msg["data"]["steps"][0]
        # The real server rejects a null actual_g ("must be a number").
        self.assertEqual(s["actual_g"], 0.0)
        self.assertFalse(s["measured"])

    def test_same_line_same_id(self):
        _, a = self.m("typed", seq=3)
        _, b = self.m("typed", seq=3)
        self.assertEqual(a, b)


# ----------------------------------------------------------- end to end

class Harness:
    def __init__(self, mock, lines=()):
        self.dir = tempfile.mkdtemp(prefix="_test_upload_")
        self.cfg = os.path.join(self.dir, "config")
        self.state = os.path.join(self.dir, "state")
        self.log = os.path.join(self.dir, "batches.jsonl")
        os.makedirs(self.cfg)
        with open(os.path.join(self.cfg, "api.env"), "w") as fh:
            fh.write(f"MINCECRAFT_API_URL={mock.url}\nMINCECRAFT_API_KEY={KEY}\n"
                     f"MINCECRAFT_DEVICE=weigh-1\n")
        with open(self.log, "w", encoding="utf-8") as fh:
            for ln in lines:
                fh.write(ln + "\n")
        self.mock = mock

    def uploader(self, boot_id=None):
        quiet()
        st = uploader.State(self.cfg, self.state, boot_id_hint=boot_id)
        up = uploader.Uploader(self.log, uploader.Settings(
            os.path.join(self.cfg, "api.env")), st, uploader.product_names(RECIPES))
        return up

    def run(self, boot_id=None):
        up = self.uploader(boot_id)
        return up.run(once=True), up

    def append(self, text):
        with open(self.log, "a", encoding="utf-8") as fh:
            fh.write(text)

    def offset(self):
        p = os.path.join(self.state, "upload.offset")
        return json.load(open(p)) if os.path.exists(p) else None

    def close(self):
        shutil.rmtree(self.dir, ignore_errors=True)


def three():
    L = station_lines()
    return [L["per_meat"], L["fixed"], L["typed"]]


class TestEndToEnd(unittest.TestCase):
    def setUp(self):
        station_lines()
        self.mock = MockIngest(key=KEY).__enter__()

    def tearDown(self):
        self.mock.__exit__()
        if getattr(self, "h", None):
            self.h.close()

    def test_sends_three_then_resend_is_all_duplicates(self):
        self.h = Harness(self.mock, three())
        before = hashlib.sha256(open(self.h.log, "rb").read()).hexdigest()
        rc, up = self.h.run(boot_id="0f3c9a21")
        self.assertEqual(rc, 0)
        self.assertEqual(sorted(b for _, b in self.mock.rows),
                         ["0f3c9a21-0", "0f3c9a21-1", "0f3c9a21-2"])
        self.assertEqual(self.h.offset()["line"], 3)
        self.assertEqual(self.h.offset()["offset"], os.path.getsize(self.h.log))
        # Lose the position: everything goes again, and nothing new is stored.
        os.remove(os.path.join(self.h.state, "upload.offset"))
        rc, _ = self.h.run()
        self.assertEqual(rc, 0)
        self.assertEqual(len(self.mock.rows), 3, "a resend must add no rows")
        self.assertEqual(self.mock.requests[-1], (200, 3))
        # Only ever read.
        after = hashlib.sha256(open(self.h.log, "rb").read()).hexdigest()
        self.assertEqual(before, after)

    def test_boot_id_made_once_and_kept(self):
        self.h = Harness(self.mock, three())
        _, up1 = self.h.run()
        bid = up1.state.boot_id
        self.assertRegex(bid, r"^[0-9a-f]{8}$")
        self.h.append(station_lines()["fixed"] + "\n")
        _, up2 = self.h.run()
        self.assertEqual(up2.state.boot_id, bid)
        self.assertIn(("weigh-1", f"{bid}-3"), self.mock.rows)
        ident = json.load(open(os.path.join(self.h.cfg, "uploader.json")))
        self.assertEqual(ident["boot_id"], bid)

    def test_losing_uploader_json_keeps_ids(self):
        self.h = Harness(self.mock, three())
        _, up1 = self.h.run()
        bid = up1.state.boot_id
        os.remove(os.path.join(self.h.cfg, "uploader.json"))
        _, up2 = self.h.run()
        self.assertEqual(up2.state.boot_id, bid)
        self.assertEqual(len(self.mock.rows), 3)

    def test_replaced_file_gets_new_boot_id(self):
        self.h = Harness(self.mock, three())
        _, up1 = self.h.run()
        old = up1.state.boot_id
        with open(self.h.log, "w") as fh:          # reset: a different history
            fh.write(station_lines()["typed"] + "\n")
        _, up2 = self.h.run()
        self.assertNotEqual(up2.state.boot_id, old)
        self.assertIn(("weigh-1", f"{up2.state.boot_id}-0"), self.mock.rows)
        self.assertEqual(len(self.mock.rows), 4)

    def test_truncated_file_gets_new_boot_id(self):
        self.h = Harness(self.mock, three())
        _, up1 = self.h.run()
        old = up1.state.boot_id
        with open(self.h.log, "w") as fh:          # same first line, shorter
            fh.write(station_lines()["per_meat"] + "\n")
        _, up2 = self.h.run()
        self.assertNotEqual(up2.state.boot_id, old)

    def test_partial_line_waits(self):
        self.h = Harness(self.mock, three()[:1])
        self.h.append(station_lines()["fixed"][:200])     # being written
        self.h.run()
        self.assertEqual(len(self.mock.rows), 1)
        self.h.append(station_lines()["fixed"][200:] + "\n")
        self.h.run()
        self.assertEqual(len(self.mock.rows), 2)

    def test_207_some_rejected_moves_on_and_logs(self):
        bad = json.loads(station_lines()["typed"])
        bad["meat_type"] = "goat"                    # the mock rejects it
        self.h = Harness(self.mock, [station_lines()["per_meat"], json.dumps(bad)])
        rc, up = self.h.run()
        self.assertEqual(self.mock.requests[-1][0], 207)
        self.assertEqual(rc, 0)
        self.assertEqual(self.h.offset()["line"], 2)
        self.assertEqual(len(self.mock.rows), 1)

    def test_rejection_is_logged_with_error_and_line(self):
        bad = json.loads(station_lines()["typed"])
        bad["meat_type"] = "goat"
        self.h = Harness(self.mock, [json.dumps(bad)])
        records = []

        class Grab(uploader.logging.Handler):
            def emit(self, r):
                records.append(r.getMessage())
        up = self.h.uploader()
        uploader.log.addHandler(Grab())
        up.run(once=True)
        rej = [r for r in records if "REJECTED" in r]
        self.assertEqual(len(rej), 1)
        self.assertIn("bad meat_type 'goat'", rej[0])
        self.assertIn('"meat_type": "goat"', rej[0])  # the original line
        self.assertEqual(self.mock.requests[-1][0], 422)
        self.assertEqual(self.h.offset()["line"], 1)  # 422 also moves on

    def test_5xx_keeps_offset_then_recovers(self):
        self.h = Harness(self.mock, three())
        self.mock.fail_next = [503]
        rc, _ = self.h.run()
        self.assertEqual(rc, 1)
        self.assertIsNone(self.h.offset())
        self.assertEqual(len(self.mock.rows), 0)
        rc, _ = self.h.run()
        self.assertEqual(rc, 0)
        self.assertEqual(len(self.mock.rows), 3)

    def test_network_down_keeps_offset(self):
        self.h = Harness(self.mock, three())
        self.mock.__exit__()                         # server gone
        rc, _ = self.h.run()
        self.assertEqual(rc, 1)
        self.assertIsNone(self.h.offset())
        self.mock = MockIngest(key=KEY).__enter__()  # for tearDown

    def test_timeout_keeps_offset(self):
        self.h = Harness(self.mock, three()[:1])
        self.mock.delay_s = 2
        old = uploader.TIMEOUT_S
        uploader.post.__defaults__ = (0.5,)
        try:
            rc, _ = self.h.run()
        finally:
            uploader.post.__defaults__ = (old,)
            self.mock.delay_s = 0
        self.assertEqual(rc, 1)
        self.assertIsNone(self.h.offset())

    def test_wrong_key_holds_and_nothing_skipped(self):
        self.h = Harness(self.mock, three())
        with open(os.path.join(self.h.cfg, "api.env"), "w") as fh:
            fh.write(f"MINCECRAFT_API_URL={self.mock.url}\nMINCECRAFT_API_KEY=wrong\n")
        rc, _ = self.h.run()
        self.assertEqual(rc, 2)
        self.assertEqual(self.mock.requests[-1][0], 401)
        self.assertIsNone(self.h.offset())
        # Fix the file, no restart needed: the next pass re-reads it.
        with open(os.path.join(self.h.cfg, "api.env"), "w") as fh:
            fh.write(f"MINCECRAFT_API_URL={self.mock.url}\nMINCECRAFT_API_KEY={KEY}\n")
        rc, _ = self.h.run()
        self.assertEqual(len(self.mock.rows), 3)

    def test_placeholder_key_sends_nothing(self):
        self.h = Harness(self.mock, three())
        with open(os.path.join(self.h.cfg, "api.env"), "w") as fh:
            fh.write("MINCECRAFT_API_KEY=PUT-THE-WRITE-KEY-HERE\n")
        rc, _ = self.h.run()
        self.assertEqual(rc, 2)
        self.assertEqual(self.mock.requests, [])

    def test_400_holds(self):
        self.h = Harness(self.mock, three())
        self.mock.fail_next = [400]
        rc, _ = self.h.run()
        self.assertEqual(rc, 2)
        self.assertIsNone(self.h.offset())

    def test_skipped_step_batch_is_stored_not_rejected(self):
        rec = json.loads(station_lines()["fixed"])
        rec["steps"][3]["skipped"] = True
        rec["steps"][3]["actual_g"] = None
        self.h = Harness(self.mock, [json.dumps(rec)])
        self.h.run()
        self.assertEqual(self.mock.requests[-1][0], 200)
        self.assertEqual(len(self.mock.rows), 1)

    def test_413_splits(self):
        L = station_lines()
        self.h = Harness(self.mock, [L["per_meat"], L["fixed"], L["typed"],
                                     L["fixed"], L["typed"]])
        self.mock.max_per_request = 2
        rc, _ = self.h.run()
        self.assertEqual(rc, 0)
        self.assertEqual(len(self.mock.rows), 5)
        self.assertIn(413, [s for s, _ in self.mock.requests])
        self.assertEqual(self.h.offset()["line"], 5)

    def test_413_single_batch_is_skipped_not_stuck(self):
        L = station_lines()
        self.h = Harness(self.mock, [L["per_meat"], L["fixed"], L["typed"]])
        up = self.h.uploader(boot_id="aaaaaaaa")
        self.mock.too_large_ids = {"aaaaaaaa-1"}
        rc = up.run(once=True)
        self.assertEqual(rc, 0)
        self.assertEqual(sorted(b for _, b in self.mock.rows),
                         ["aaaaaaaa-0", "aaaaaaaa-2"])
        self.assertEqual(self.h.offset()["line"], 3)

    def test_at_most_50_per_request(self):
        L = station_lines()
        self.h = Harness(self.mock, [L["fixed"]] * 120)
        rc, _ = self.h.run()
        self.assertEqual(rc, 0)
        self.assertEqual([n for _, n in self.mock.requests], [50, 50, 20])
        self.assertEqual(len(self.mock.rows), 120)

    def test_bad_json_line_skipped_seq_kept(self):
        L = station_lines()
        self.h = Harness(self.mock, [L["per_meat"], '{"batch_no": 2, "prod', L["typed"]])
        _, up = self.h.run()
        bid = up.state.boot_id
        self.assertEqual(sorted(b for _, b in self.mock.rows), [f"{bid}-0", f"{bid}-2"])
        self.assertEqual(self.h.offset()["line"], 3)

    def test_key_never_in_log(self):
        self.h = Harness(self.mock, three())
        uploader.setup_logging(self.h.state)
        try:
            up = self.h.uploader()
            uploader.setup_logging(self.h.state)
            up.run(once=True)
            self.mock.fail_next = [401]
            self.h.append(station_lines()["fixed"] + "\n")
            self.h.uploader().run(once=True)
        finally:
            for hd in list(uploader.log.handlers):
                hd.close()
            quiet()
        with open(os.path.join(self.h.state, "uploader.log")) as fh:
            text = fh.read()
        self.assertIn("sent seq=0", text)
        self.assertIn('"product": "teriyaki_jerky"', text)  # the full line is kept
        self.assertNotIn(KEY, text)

    def test_dry_run_sends_and_moves_nothing(self):
        self.h = Harness(self.mock, three())
        up = self.h.uploader()
        up.dry_run = True
        import io
        import contextlib
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            up.run()
        self.assertIn("3 batch(es) would be sent", buf.getvalue())
        self.assertEqual(self.mock.requests, [])
        self.assertIsNone(self.h.offset())


class TestStationStaysOffline(unittest.TestCase):
    def test_station_never_imports_network_code(self):
        for f in ("station.py", "panel.py", "scale.py"):
            with open(os.path.join(ROOT, f), encoding="utf-8") as fh:
                src = fh.read()
            for bad in ("import urllib", "import socket", "import requests",
                        "import http", "import uploader", "from uploader"):
                self.assertNotIn(bad, src, f"{f} must stay off the network")

    def test_no_key_in_tracked_files(self):
        """No real API key anywhere in the repo. Checked by hash, so this
        public file does not carry even part of a key."""
        import re
        known = {"09704a1dfc3cdbe69af2ecb891f1c79991910c31925150d0c199c0e17b14eac1",   # write
                 "3d95f321e9a204d3475cf8ccb99ab5d1e9d58abffa6e267812220dea49486503"}   # read
        hexrun = re.compile(r"[0-9a-fA-F]{64}")
        for dirpath, dirs, files in os.walk(ROOT):
            dirs[:] = [d for d in dirs if d not in (".git", "__pycache__")]
            for fn in files:
                if fn.endswith((".png", ".xlsx", ".pyc")):
                    continue
                with open(os.path.join(dirpath, fn), encoding="utf-8",
                          errors="ignore") as fh:
                    text = fh.read()
                for tok in hexrun.findall(text):
                    h = hashlib.sha256(tok.lower().encode()).hexdigest()
                    self.assertNotIn(h, known, f"{fn} contains an API key")

if __name__ == "__main__":
    unittest.main(verbosity=1)
