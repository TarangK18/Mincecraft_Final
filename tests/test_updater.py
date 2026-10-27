#!/usr/bin/env python3
"""Recipes from the owner's workbook, code from `stable`, opening at boot.

Builds a throw-away "GitHub": a bare origin repo made from this folder, a
"dev" clone the owner pushes from, and a "pi" clone the station runs from.
updater.py is driven through its command line exactly as the timer and
mincecraft.sh run it. The launcher test runs the real mincecraft.sh with a
stub station.py so a broken update and its rollback happen end to end.
The panel tests check that recipes only ever change on the HOME screen.

Run: QT_QPA_PLATFORM=offscreen python3 tests/test_updater.py
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import openpyxl  # noqa: E402

UPDATER = os.path.join(ROOT, "updater.py")
TRACKED_SKIP = {".git", "__pycache__", "screenshots", "_versions"}


def git(cwd, *args, check=True):
    out = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True,
                         env=dict(os.environ, GIT_TERMINAL_PROMPT="0",
                                  GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@t",
                                  GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@t"))
    if check and out.returncode:
        raise AssertionError(f"git {args}: {out.stderr}")
    return out.stdout.strip()


class World:
    """origin (bare) + dev clone + pi clone + the Pi's state/data dirs."""

    def __init__(self):
        self.dir = tempfile.mkdtemp(prefix="_test_update_")
        self.origin = os.path.join(self.dir, "origin.git")
        self.dev = os.path.join(self.dir, "dev")
        self.pi = os.path.join(self.dir, "pi")
        self.home = os.path.join(self.dir, "home")
        self.state = os.path.join(self.home, ".local", "state", "mincecraft")
        self.data = os.path.join(self.home, ".local", "share", "mincecraft")
        os.makedirs(self.home)
        shutil.copytree(ROOT, self.dev, ignore=lambda d, names: [
            n for n in names if n in TRACKED_SKIP or n.endswith(".pyc")
            or n.startswith("_test_")])
        git(self.dir, "init", "-q", "--bare", "-b", "main", self.origin)
        git(self.dev, "init", "-q", "-b", "main")
        git(self.dev, "add", "-A")
        git(self.dev, "commit", "-qm", "A")
        git(self.dev, "remote", "add", "origin", self.origin)
        git(self.dev, "push", "-q", "origin", "main")
        git(self.dir, "clone", "-q", self.origin, self.pi)
        self.A = git(self.pi, "rev-parse", "HEAD")

    # -- the owner -------------------------------------------------------
    def commit(self, msg, branch="main", stable=False):
        git(self.dev, "add", "-A")
        git(self.dev, "commit", "-qm", msg)
        sha = git(self.dev, "rev-parse", "HEAD")
        git(self.dev, "push", "-q", "origin", f"HEAD:{branch}")
        if stable:
            git(self.dev, "push", "-q", "-f", "origin", f"{sha}:refs/heads/stable")
        return sha

    def set_stable(self, sha):
        git(self.dev, "push", "-q", "-f", "origin", f"{sha}:refs/heads/stable")

    def edit_workbook(self, sheet, cell, value):
        p = os.path.join(self.dev, "DOKI-Recipes.xlsx")
        wb = openpyxl.load_workbook(p)
        wb[sheet][cell] = value
        wb.save(p)

    # -- the Pi ----------------------------------------------------------
    def updater(self, *args, env=None):
        e = dict(os.environ, HOME=self.home, **(env or {}))
        e.pop("XDG_STATE_HOME", None)
        e.pop("XDG_DATA_HOME", None)
        out = subprocess.run([sys.executable, UPDATER, "--repo", self.pi,
                              "--state-dir", self.state, "--data-dir", self.data,
                              *args], capture_output=True, text=True, env=e,
                             timeout=120)
        return out.returncode, out.stdout + out.stderr

    def status(self):
        with open(os.path.join(self.state, "update.json")) as fh:
            return json.load(fh)

    def local(self):
        with open(os.path.join(self.data, "recipes.json")) as fh:
            return json.load(fh)

    def head(self):
        return git(self.pi, "rev-parse", "HEAD")

    def clean(self):
        return git(self.pi, "status", "--porcelain") == ""

    def close(self):
        shutil.rmtree(self.dir, ignore_errors=True)


def pct(cfg, pid, name):
    p = next(p for p in cfg["products"] if p["id"] == pid)
    return dict(p["ingredients"])[name]


# --------------------------------------------------------------- recipes

class TestRecipeSync(unittest.TestCase):
    def setUp(self):
        self.w = World()

    def tearDown(self):
        self.w.close()

    def test_first_sync_matches_repo_and_tree_stays_clean(self):
        rc, out = self.w.updater("--recipes")
        self.assertEqual(rc, 0, out)
        local = self.w.local()
        with open(os.path.join(self.w.pi, "recipes.json")) as fh:
            repo = json.load(fh)
        sync = local.pop("_sync")
        self.assertEqual(local, repo, "an unchanged workbook gives the same recipes")
        self.assertEqual(sync["commit"], self.w.A)
        self.assertTrue(self.w.status()["recipes"]["ok"])
        self.assertTrue(self.w.clean(), "syncing must not touch the clone")

    def test_owner_edits_workbook_and_pi_follows(self):
        self.w.updater("--recipes")
        self.w.edit_workbook("Teriyaki Jerky", "C9", 80)     # teriyaki sauce 73 -> 80 g/kg
        sha = self.w.commit("more teriyaki sauce")
        rc, out = self.w.updater("--recipes")
        self.assertEqual(rc, 0, out)
        local = self.w.local()
        self.assertAlmostEqual(pct(local, "teriyaki_jerky", "teriyaki sauce"), 8.0)
        self.assertEqual(local["_sync"]["commit"], sha)
        self.assertTrue(self.w.clean())
        # Nothing new: nothing rewritten.
        m = os.stat(os.path.join(self.w.data, "recipes.json")).st_mtime_ns
        time.sleep(0.05)
        self.w.updater("--recipes")
        self.assertEqual(m, os.stat(os.path.join(self.w.data, "recipes.json")).st_mtime_ns)

    def test_bad_sheet_skipped_others_used(self):
        self.w.updater("--recipes")
        before = self.w.local()
        self.w.edit_workbook("Pepper Jerky", "C9", "abc")    # not a number
        self.w.edit_workbook("Teriyaki Jerky", "C9", 81)
        self.w.commit("one good, one bad")
        self.w.updater("--recipes")
        local = self.w.local()
        self.assertAlmostEqual(pct(local, "teriyaki_jerky", "teriyaki sauce"), 8.1)
        self.assertEqual(pct(local, "pepper_jerky", "onion powder"),
                         pct(before, "pepper_jerky", "onion powder"),
                         "the broken sheet keeps its previous recipe")
        st = self.w.status()["recipes"]
        self.assertTrue(st["ok"])
        self.assertEqual([x["sheet"] for x in st["skipped_sheets"]], ["Pepper Jerky"])
        self.assertIn("not a number", st["skipped_sheets"][0]["problems"][0])

    def test_invalid_recipes_rejected_last_good_kept(self):
        self.w.updater("--recipes")
        good = self.w.local()
        p = os.path.join(self.w.dev, "recipes.json")
        cfg = json.load(open(p))
        cfg["papain"]["applies_to"].append("no_such_product")
        json.dump(cfg, open(p, "w"), indent=2)
        self.w.edit_workbook("Teriyaki Jerky", "C9", 99)
        bad = self.w.commit("broken papain list")
        rc, out = self.w.updater("--recipes")
        self.assertEqual(rc, 0)
        self.assertEqual(self.w.local(), good, "last good recipes stay")
        st = self.w.status()["recipes"]
        self.assertIs(st["ok"], False)
        self.assertEqual(st["rejected"]["commit"], bad)
        self.assertIn("no_such_product", st["rejected"]["why"])
        self.assertIn("REJECTED", out)
        # Reported once, not every 5 minutes.
        rc, out = self.w.updater("--recipes")
        self.assertNotIn("REJECTED", out)

    def test_offline_changes_nothing(self):
        self.w.updater("--recipes")
        good = self.w.local()
        git(self.w.pi, "remote", "set-url", "origin", "/nonexistent/repo.git")
        rc, out = self.w.updater("--recipes")
        self.assertEqual(rc, 0)
        self.assertEqual(self.w.local(), good)
        self.assertIs(self.w.status()["recipes"]["online"], False)
        rc, out = self.w.updater("--boot")
        self.assertEqual(rc, 0, "boot with no network still lets the station start")

    def test_workbook_missing_on_main(self):
        os.remove(os.path.join(self.w.dev, "DOKI-Recipes.xlsx"))
        self.w.commit("removed workbook")
        rc, _ = self.w.updater("--recipes")
        self.assertEqual(rc, 0)
        self.assertFalse(os.path.exists(os.path.join(self.w.data, "recipes.json")))
        self.assertIn("not found", self.w.status()["recipes"]["last_error"])


# ------------------------------------------------------------------ code

class TestCodeUpdates(unittest.TestCase):
    def setUp(self):
        self.w = World()

    def tearDown(self):
        self.w.close()

    def touch(self, name, text):
        with open(os.path.join(self.w.dev, name), "w") as fh:
            fh.write(text)

    def test_no_stable_branch_no_code_change(self):
        self.touch("NOTE.txt", "main only\n")
        self.w.commit("main only")
        rc, _ = self.w.updater("--boot")
        self.assertEqual(rc, 0)
        self.assertEqual(self.w.head(), self.w.A, "main never reaches the Pi as code")
        self.assertIn("stable", self.w.status()["code"]["last_error"])

    def test_stable_update_then_rollback_then_newer(self):
        self.touch("NOTE.txt", "B\n")
        B = self.w.commit("B", stable=True)
        rc, _ = self.w.updater("--boot")
        self.assertEqual(rc, 10)
        self.assertEqual(self.w.head(), B)
        self.assertEqual(git(self.w.pi, "rev-parse", "--abbrev-ref", "HEAD"), "stable")
        cs = self.w.status()["code"]
        self.assertEqual((cs["good"], cs["pending"]), (self.w.A, B))
        self.assertEqual(self.w.updater("--mark-good")[0], 0)
        cs = self.w.status()["code"]
        self.assertEqual((cs["good"], cs["pending"]), (B, None))

        self.touch("NOTE.txt", "C — breaks the station\n")
        C = self.w.commit("C", stable=True)
        self.assertEqual(self.w.updater("--boot")[0], 10)
        self.assertEqual(self.w.head(), C)
        rc, _ = self.w.updater("--rollback")
        self.assertEqual(rc, 10)
        self.assertEqual(self.w.head(), B)
        cs = self.w.status()["code"]
        self.assertIn(C, cs["bad"])
        self.assertEqual(cs["rolled_back"]["from"], C)
        # The bad commit is never tried again...
        self.assertEqual(self.w.updater("--boot")[0], 0)
        self.assertEqual(self.w.head(), B)
        # ...but a newer one is.
        self.touch("NOTE.txt", "D — fixed\n")
        D = self.w.commit("D", stable=True)
        self.assertEqual(self.w.updater("--boot")[0], 10)
        self.assertEqual(self.w.head(), D)
        self.assertTrue(self.w.clean())

    def test_nothing_to_roll_back(self):
        self.assertEqual(self.w.updater("--rollback")[0], 1)
        self.assertEqual(self.w.head(), self.w.A)

    def test_hand_edited_clone_is_left_alone(self):
        with open(os.path.join(self.w.pi, "README.md"), "a") as fh:
            fh.write("edited on the Pi\n")
        self.touch("NOTE.txt", "E\n")
        self.w.commit("E", stable=True)
        rc, _ = self.w.updater("--boot")
        self.assertEqual(rc, 0)
        self.assertEqual(self.w.head(), self.w.A)
        self.assertIn("edited by hand", self.w.status()["code"]["last_error"])

    def test_uploader_restarted_only_when_it_changed(self):
        calls = os.path.join(self.w.dir, "calls.txt")
        stub = os.path.join(self.w.dir, "systemctl")
        with open(stub, "w") as fh:
            fh.write(f"#!/bin/sh\necho \"$@\" >> {calls}\n")
        os.chmod(stub, 0o755)
        env = {"MINCECRAFT_SYSTEMCTL": stub}
        self.touch("NOTE.txt", "no uploader change\n")
        self.w.commit("F", stable=True)
        self.w.updater("--boot", env=env)
        self.assertFalse(os.path.exists(calls))
        self.w.updater("--mark-good")
        with open(os.path.join(self.w.dev, "uploader.py"), "a") as fh:
            fh.write("\n# changed\n")
        self.w.commit("G", stable=True)
        self.w.updater("--boot", env=env)
        self.assertIn("try-restart mincecraft-uploader.service", open(calls).read())


# -------------------------------------------------------------- launcher

GOOD_STATION = """import sys, time
print("stub station GOOD", flush=True)
time.sleep(4)
sys.exit(0)
"""
BAD_STATION = """import sys
print("stub station BAD", flush=True)
sys.exit(1)
"""


class TestLauncherRollback(unittest.TestCase):
    def setUp(self):
        self.w = World()

    def tearDown(self):
        self.w.close()

    def run_launcher(self):
        env = dict(os.environ, HOME=self.w.home, MINCECRAFT_HEALTHY_S="2",
                   MINCECRAFT_NET_WAIT_S="0", PATH=os.environ["PATH"])
        for k in ("XDG_STATE_HOME", "XDG_DATA_HOME", "XDG_CONFIG_HOME",
                  "MINCECRAFT_UPDATED", "MINCECRAFT_ROLLED_BACK"):
            env.pop(k, None)
        env["PATH"] = "/nonexistent-zenity-free:" + env["PATH"]
        return subprocess.run(["bash", os.path.join(self.w.pi, "mincecraft.sh")],
                              capture_output=True, text=True, env=env, timeout=120)

    def test_broken_update_rolls_back_and_station_starts(self):
        with open(os.path.join(self.w.dev, "station.py"), "w") as fh:
            fh.write(GOOD_STATION)
        good = self.w.commit("good stub", stable=True)
        # First start: picks up the good stub, runs > 2 s, marked good.
        self.run_launcher()
        self.assertEqual(self.w.head(), good)
        cs = self.w.status()["code"]
        self.assertEqual((cs["good"], cs["pending"]), (good, None))

        with open(os.path.join(self.w.dev, "station.py"), "w") as fh:
            fh.write(BAD_STATION)
        bad = self.w.commit("bad stub", stable=True)
        out = self.run_launcher()
        log = open(os.path.join(self.w.state, "station.log")).read()
        self.assertIn("stub station BAD", log)
        self.assertIn("rolled back, starting again", log)
        self.assertIn("stub station GOOD", log.split("rolled back")[-1])
        self.assertEqual(out.returncode, 0)
        self.assertEqual(self.w.head(), good)
        cs = self.w.status()["code"]
        self.assertIn(bad, cs["bad"])
        self.assertIsNone(cs["pending"])
        self.assertTrue(self.w.clean())


# ----------------------------------------------------------------- panel

class TestStationPicksUpRecipes(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from PyQt5.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication(sys.argv[:1])

    def setUp(self):
        from scale import BatchLog, Config, DailyRatio, ScaleState
        from panel import Panel, scale_for
        self.dir = tempfile.mkdtemp(prefix="_test_update_panel_")
        self.local = os.path.join(self.dir, "recipes.json")
        self.status = os.path.join(self.dir, "update.json")
        with open(os.path.join(ROOT, "recipes.json")) as fh:
            self.base = json.load(fh)
        self.write(self.base, commit="aaaa1111")
        self.cfg = Config.load(self.local)
        self.log = os.path.join(self.dir, "b.jsonl")
        self.win = Panel(ScaleState(), self.cfg, BatchLog(self.log),
                         DailyRatio(os.path.join(self.dir, "d.json"), self.cfg),
                         scale=scale_for(800, 480), version="v1.1",
                         recipes_path=self.local, status_path=self.status)
        self.win.refresh_consumption = lambda: None
        self.win.resize(800, 480)
        self.win.show()
        self.app.processEvents()

    def tearDown(self):
        self.win.close()
        shutil.rmtree(self.dir, ignore_errors=True)

    def write(self, cfg, commit=None, teriyaki=None):
        cfg = json.loads(json.dumps(cfg))
        if teriyaki is not None:
            p = next(p for p in cfg["products"] if p["id"] == "teriyaki_jerky")
            p["ingredients"][0][1] = teriyaki
        if commit:
            cfg["_sync"] = {"commit": commit, "applied_at": "2026-10-27T10:00:00+0530"}
        time.sleep(0.02)
        with open(self.local, "w") as fh:
            json.dump(cfg, fh)

    def teriyaki(self):
        return dict(self.win.cfg.product("teriyaki_jerky")["ingredients"])["teriyaki sauce"]

    def test_reload_on_home(self):
        self.write(self.base, commit="bbbb2222", teriyaki=8.0)
        self.assertTrue(self.win.check_recipes())
        self.assertAlmostEqual(self.teriyaki(), 8.0)
        self.assertIs(self.win.screens["REVIEW"].cfg, self.win.cfg)
        self.assertEqual(self.win.cfg.synced_from, "bbbb2222")
        tone, text = self.win.update_notice()
        self.assertIn("Recipes updated", text)

    def test_never_mid_batch(self):
        self.win.choose_product("teriyaki_jerky")
        self.app.processEvents()
        self.assertEqual(self.win.current, "CAPTURE")
        self.write(self.base, commit="cccc3333", teriyaki=9.0)
        self.assertFalse(self.win.check_recipes())
        self.assertAlmostEqual(self.teriyaki(), 7.3)
        self.win.go_home()
        self.assertTrue(self.win.check_recipes())
        self.assertAlmostEqual(self.teriyaki(), 9.0)

    def test_invalid_file_not_loaded(self):
        bad = json.loads(json.dumps(self.base))
        bad["papain"]["applies_to"].append("nope")
        self.write(bad, commit="dddd4444")
        self.assertFalse(self.win.check_recipes())
        self.assertAlmostEqual(self.teriyaki(), 7.3)
        tone, text = self.win.update_notice()
        self.assertEqual(tone, "warn")
        self.assertIn("not loaded", text)

    def test_home_shows_rejection_and_rollback_and_fits(self):
        with open(self.status, "w") as fh:
            json.dump({"recipes": {"ok": False, "rejected": {
                "why": "papain applies_to names unknown product 'x' " * 4}}}, fh)
        self.win.check_recipes()
        self.win.show_screen("HOME")
        self.win.screens["HOME"].tick({"grams": None, "stable": False, "fresh": False}, False)
        self.app.processEvents()
        line = self.win.screens["HOME"].update_line
        self.assertTrue(line.isVisible())
        self.assertIn("Recipe update rejected", line.text())
        self.assertLess(len(line.text()), 200)
        btn = self.win.screens["HOME"].start_btn
        bottom = btn.mapTo(self.win, btn.rect().bottomLeft()).y()
        self.assertLessEqual(bottom, self.win.height(), "buttons still on screen")
        with open(self.status, "w") as fh:
            json.dump({"code": {"rolled_back": {"from": "abcdef123", "to": "1234567aa"}}}, fh)
        self.win.check_recipes()
        self.assertIn("failed to start", self.win.update_notice()[1])
        self.win.grab().save(os.path.join(ROOT, "screenshots", "34-home-update-notice.png"))

    def test_record_carries_recipe_version(self):
        from panel import Step
        st = self.win.st
        self.win.start_batch()
        st.product = "piri_piri_masala"
        st.steps = [Step(n, None, t) for n, t in self.win.cfg.targets_for("piri_piri_masala")]
        for s in st.steps:
            s.actual = s.target
        self.win.record_batch()
        rec = json.loads(open(self.log).read().splitlines()[-1])
        self.assertEqual(rec["recipe_version"], self.win.cfg.recipe_version("piri_piri_masala"))
        self.assertEqual(rec["recipes_commit"], "aaaa1111")
        self.assertRegex(rec["recipe_version"], r"^[0-9a-f]{12}$")

    def test_pick_recipes(self):
        from scale import pick_recipes
        repo = os.path.join(ROOT, "recipes.json")
        self.assertEqual(pick_recipes(repo, self.local), (self.local, None))
        self.assertEqual(pick_recipes(repo, os.path.join(self.dir, "none.json")),
                         (repo, None))
        with open(self.local, "w") as fh:
            fh.write("{ not json")
        path, why = pick_recipes(repo, self.local)
        self.assertEqual(path, repo)
        self.assertIn("unusable", why)


if __name__ == "__main__":
    os.makedirs(os.path.join(ROOT, "screenshots"), exist_ok=True)
    unittest.main(verbosity=1)
