#!/usr/bin/env python3
"""Supervisor menu -> Recipes: a button per recipe, tap one to see it in
full; one line on where the recipes came from; SYNC NOW.

SYNC NOW is driven end to end: the panel starts the real updater.py as a
separate process, which downloads the sheet from a stand-in for Google on
127.0.0.1 (the same FakeGoogle the sheet tests use). The station must swap
recipes only with no batch open, never run two syncs at once, stay usable
while one runs, and say plainly what happened — including a sheet that is
not shared, no network, a hung updater, and an updater that cannot start.

Run: QT_QPA_PLATFORM=offscreen python3 tests/test_recipes_screen.py
"""

import os
import shutil
import sys
import tempfile
import time
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tests"))

from test_sheet_recipes import BASE, FakeGoogle, recipe_tabs, row_of, xlsx  # noqa: E402


class TestRecipesScreen(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from PyQt5.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication(sys.argv[:1])

    def setUp(self):
        from scale import BatchLog, Config, DailyRatio, ScaleState
        from panel import Panel, scale_for
        self.dir = tempfile.mkdtemp(prefix="_test_recipes_screen_")
        self.local = os.path.join(self.dir, "data", "recipes.json")
        self.status = os.path.join(self.dir, "state", "update.json")
        self.conf = os.path.join(self.dir, "config")
        for d in ("data", "state", "config"):
            os.makedirs(os.path.join(self.dir, d))
        self.g = FakeGoogle()
        self.set_link(self.g.url)
        # Start as a Pi that has never synced: the repo's recipes.
        self.cfg = Config.load(os.path.join(ROOT, "recipes.json"))
        self.log = os.path.join(self.dir, "b.jsonl")
        self.win = Panel(ScaleState(), self.cfg, BatchLog(self.log),
                         DailyRatio(os.path.join(self.dir, "d.json"), self.cfg),
                         scale=scale_for(800, 480), version="v1.3",
                         recipes_path=self.local, status_path=self.status)
        self.win.refresh_consumption = lambda: None
        # The real command, pointed at this test's link file.
        self.win.sync_argv = self.win.sync_argv + ["--config-dir", self.conf]
        self.win.resize(800, 480)
        self.win.show()
        self.app.processEvents()
        self.screen = self.win.screens["RECIPES"]

    def tearDown(self):
        if self.win._sync:
            self.win._sync["proc"].kill()
            self.win._sync["proc"].waitForFinished(5000)
        self.win.close()
        self.g.stop()
        shutil.rmtree(self.dir, ignore_errors=True)

    # -- helpers -----------------------------------------------------------

    def set_link(self, url):
        with open(os.path.join(self.conf, "recipes.env"), "w") as fh:
            fh.write(f"RECIPES_SHEET_URL={url}\n")

    def open_recipes(self):
        self.win.show_screen("MENU")
        btn = self.find_button(self.win.screens["MENU"], "Recipes")
        self.assertTrue(btn.isEnabled())
        btn.click()
        self.app.processEvents()
        self.assertEqual(self.win.current, "RECIPES")

    @staticmethod
    def find_button(screen, text):
        from PyQt5.QtWidgets import QPushButton
        return next(b for b in screen.findChildren(QPushButton) if b.text() == text)

    def wait_sync(self, limit=60):
        end = time.monotonic() + limit
        while self.win.syncing() and time.monotonic() < end:
            self.app.processEvents()
            time.sleep(0.02)
        self.app.processEvents()
        self.assertFalse(self.win.syncing(), "sync finished")

    def sync(self):
        self.screen.sync_btn.click()
        self.wait_sync()
        return self.screen.result.property("tone"), self.screen.result.text()

    def soy(self):
        return dict(self.win.cfg.product("teriyaki_jerky")["ingredients"])["soy sauce"]

    def cards(self):
        self.screen.paint()
        return self.screen.cards

    def open_card(self, name):
        self.cards()[name].click()
        self.app.processEvents()
        self.assertEqual(self.win.current, "RECIPE")
        return self.win.screens["RECIPE"]

    @staticmethod
    def rows(detail):
        t = detail.table
        return [[t.item(r, c).text() if t.item(r, c) else "" for c in range(3)]
                for r in range(t.rowCount())]

    def bottom_ok(self, w):
        y = w.mapTo(self.win, w.rect().bottomLeft()).y()
        self.assertLessEqual(y, self.win.height(), f"{w.text()!r} on screen")

    # -- the menu ----------------------------------------------------------

    def test_menu_has_recipes_not_a_dead_editor(self):
        from PyQt5.QtWidgets import QPushButton
        texts = [b.text() for b in self.win.screens["MENU"].findChildren(QPushButton)]
        self.assertNotIn("Recipe editor", texts)
        self.assertIn("Recipes", texts)
        self.open_recipes()
        self.assertIn("Built into the station", self.screen.status.text())
        self.assertEqual(list(self.cards()), [p["name"] for p in BASE["products"]])
        self.assertFalse(self.screen.result.isVisible(), "no clutter until a sync")
        self.find_button(self.screen, "◀ BACK").click()
        self.assertEqual(self.win.current, "MENU")

    # -- SYNC NOW, end to end ---------------------------------------------

    def test_sync_now_takes_the_sheet_then_says_no_change(self):
        self.open_recipes()
        # The sheet holds today's recipes: same recipes, now from the sheet.
        tone, text = self.sync()
        self.assertEqual(tone, "good", text)
        self.assertIn("No change", text)
        self.assertEqual(self.win.cfg.data["_sync"]["source"], "google-sheet")
        self.assertIn("From the Google Sheet", self.screen.status.text())
        self.assertIn("last change", self.screen.status.text())
        self.assertIs(self.win.screens["REVIEW"].cfg, self.win.cfg)

        tabs = recipe_tabs()
        tabs["Teriyaki Jerky"][row_of(tabs["Teriyaki Jerky"], "soy sauce")][1] = 50
        self.g.body = xlsx(tabs)
        tone, text = self.sync()
        self.assertIn("Recipes updated", text)
        self.assertAlmostEqual(self.soy(), 5.0)
        detail = self.open_card("Teriyaki Jerky")
        self.assertIn(["2", "soy sauce", "50 g"], self.rows(detail), "detail shows the new grams")
        self.find_button(detail, "◀ ALL RECIPES").click()

        tone, text = self.sync()
        self.assertEqual(tone, "good")
        self.assertIn("No change", text)
        self.win.go_home()
        self.assertIn("Recipes updated", self.win.update_notice()[1])

    def test_mistake_is_named_and_screen_fits_800x480(self):
        self.open_recipes()
        self.sync()
        tabs = recipe_tabs()
        for name in ("Teriyaki Jerky", "Pepper Jerky", "Mughlai Jerky", "Masala Jerky"):
            rows = tabs[name]
            rows[row_of(rows, rows[7][0])][1] = "about a spoon, roughly, give or take"
        self.g.body = xlsx(tabs)
        # Every mistake keeps its previous recipe, so the recipes themselves
        # do not change — the tabs must still be named (updater v1.2).
        tone, text = self.sync()
        self.assertEqual(tone, "over", text)
        self.assertIn("No recipe changed", text)
        cards = self.cards()
        for name in ("Teriyaki Jerky", "Pepper Jerky", "Mughlai Jerky", "Masala Jerky"):
            self.assertIn("⚠", cards[name].text(), name)
        self.assertNotIn("⚠", cards["Vinegar Bath"].text())
        self.bottom_ok(self.screen.sync_btn)
        self.bottom_ok(cards["Piri Piri Masala"])
        self.win.grab().save(os.path.join(ROOT, "screenshots", "35-recipes-list.png"))
        detail = self.open_card("Teriyaki Jerky")
        self.assertTrue(detail.problem.isVisible())
        self.assertIn("is not a number", detail.problem.text())
        self.assertIn("previous recipe", detail.problem.text())
        self.assertIn(["1", "teriyaki sauce", "73 g"], self.rows(detail), "previous recipe shown")
        self.win.grab().save(os.path.join(ROOT, "screenshots", "36-recipe-mistake.png"))
        self.win.go_home()
        self.assertIn("Teriyaki Jerky", self.win.update_notice()[1], "HOME too")
        self.win.show_screen("RECIPES")
        # Fixing the sheet clears it.
        self.g.body = xlsx(recipe_tabs())
        tone, text = self.sync()
        self.assertIn("No change", text)
        self.assertFalse(any("⚠" in b.text() for b in self.cards().values()))
        self.bottom_ok(self.screen.result)

    def test_not_shared(self):
        self.open_recipes()
        self.g.status, self.g.body = 401, b"<html>Sign in</html>"
        tone, text = self.sync()
        self.assertEqual(tone, "over")
        self.assertIn("not taken", text)
        self.assertIn("not shared", self.screen.status.text())
        self.assertNotIn("_sync", self.win.cfg.data, "recipes unchanged")

    def test_no_network(self):
        self.open_recipes()
        self.set_link("http://127.0.0.1:9/export.xlsx")     # nothing listens
        tone, text = self.sync()
        self.assertEqual(tone, "over")
        self.assertIn("Could not reach", text)
        self.assertIn("not reachable", self.screen.status.text())

    def test_hung_updater_is_stopped(self):
        self.open_recipes()
        self.win.sync_argv = [sys.executable, "-c", "import time; time.sleep(30)"]
        self.win.sync_timeout_s = 0.5
        t = time.monotonic()
        tone, text = self.sync()
        self.assertLess(time.monotonic() - t, 10)
        self.assertEqual(tone, "over")
        self.assertIn("stopped", text)

    def test_updater_cannot_start(self):
        self.open_recipes()
        self.win.sync_argv = [os.path.join(self.dir, "no-such-python")]
        tone, text = self.sync()
        self.assertEqual(tone, "over")
        self.assertIn("Could not run", text)
        self.assertTrue(self.screen.sync_btn.isEnabled(), "can try again")

    def test_another_sync_already_running(self):
        self.open_recipes()
        # A stand-in that does nothing, as the updater does when the timer's
        # copy holds the lock.
        self.win.sync_argv = [sys.executable, "-c", "pass"]
        tone, text = self.sync()
        self.assertIn("already running", text)

    def test_one_sync_at_a_time_and_screen_stays_live(self):
        self.open_recipes()
        self.win.sync_argv = [sys.executable, "-c", "import time; time.sleep(1.5)"]
        self.assertTrue(self.win.sync_recipes_now())
        self.app.processEvents()
        self.assertFalse(self.screen.sync_btn.isEnabled())
        self.assertEqual(self.screen.sync_btn.text(), "SYNCING…")
        self.assertIn("Checking", self.screen.result.text())
        self.assertFalse(self.win.sync_recipes_now(), "second press ignored")
        self.find_button(self.screen, "◀ BACK").click()   # not stuck waiting
        self.assertEqual(self.win.current, "MENU")
        self.wait_sync()
        self.win.show_screen("RECIPES")
        self.assertTrue(self.screen.sync_btn.isEnabled())
        self.assertEqual(self.screen.sync_btn.text(), "SYNC NOW")

    def test_never_swaps_recipes_mid_batch(self):
        tabs = recipe_tabs()
        tabs["Teriyaki Jerky"][row_of(tabs["Teriyaki Jerky"], "soy sauce")][1] = 50
        self.g.body = xlsx(tabs)
        self.open_recipes()
        self.assertTrue(self.win.sync_recipes_now())
        self.win.go_home()
        self.win.choose_product("teriyaki_jerky")        # batch opened meanwhile
        self.assertEqual(self.win.current, "CAPTURE")
        self.wait_sync()
        self.assertAlmostEqual(self.soy(), 4.64, msg="not swapped mid-batch")
        self.win.go_home()
        self.assertTrue(self.win.check_recipes())
        self.assertAlmostEqual(self.soy(), 5.0)

    # -- one recipe in full -------------------------------------------------

    def test_meat_recipe_in_full(self):
        self.open_recipes()
        d = self.open_card("Teriyaki Jerky")
        self.assertEqual(d.headline.text(), "Teriyaki Jerky")
        self.assertIn("Per 1 kg of meat", d.sub.text())
        self.assertIn("15 ingredients", d.sub.text())
        self.assertIn("papain last", d.sub.text())
        self.assertFalse(d.problem.isVisible())
        rows = self.rows(d)
        self.assertEqual(rows[0], ["1", "teriyaki sauce", "73 g"])
        self.assertIn(["2", "soy sauce", "46.4 g"], rows)
        self.assertIn(["15", "bhut jholokia", "0.25 g"], rows)
        self.assertIn(["", "liquid smoke", "not weighed"], rows)
        self.assertIn("Papain", rows[-1][1])
        self.assertEqual(rows[-1][2], "buffalo 6 g · chicken 2 g")
        self.assertEqual(len(rows), 17)                 # 16 listed + papain
        self.assertEqual(d.table.horizontalHeaderItem(2).text(), "Per kg meat")
        self.bottom_ok(self.find_button(d, "◀ ALL RECIPES"))
        self.win.grab().save(os.path.join(ROOT, "screenshots", "37-recipe-detail.png"))
        self.find_button(d, "◀ ALL RECIPES").click()
        self.assertEqual(self.win.current, "RECIPES")

    def test_open_recipe_follows_a_sync(self):
        # A supervisor looking at a recipe while it changes sees the change,
        # not the old numbers.
        self.open_recipes()
        d = self.open_card("Teriyaki Jerky")
        self.assertIn(["2", "soy sauce", "46.4 g"], self.rows(d))
        tabs = recipe_tabs()
        tabs["Teriyaki Jerky"][row_of(tabs["Teriyaki Jerky"], "soy sauce")][1] = 50
        self.g.body = xlsx(tabs)
        self.assertTrue(self.win.sync_recipes_now())
        self.wait_sync()
        d.tick(None, False)
        self.assertIn(["2", "soy sauce", "50 g"], self.rows(d))

    def test_fixed_batch_in_full(self):
        self.open_recipes()
        self.assertIn("fixed batch", self.cards()["Piri Piri Masala"].text())
        d = self.open_card("Piri Piri Masala")
        self.assertIn("Fixed batch", d.sub.text())
        self.assertIn("2.00 kg per batch", d.sub.text())
        rows = self.rows(d)
        self.assertEqual(rows[0], ["1", "chilli flakes", "300 g"])
        self.assertEqual(len(rows), 10, "no papain row")
        self.assertEqual(d.table.horizontalHeaderItem(2).text(), "Per batch")

    def test_demo_has_no_sync(self):
        from scale import BatchLog, DailyRatio, ScaleState
        from panel import Panel
        win = Panel(ScaleState(), self.cfg, BatchLog(self.log),
                    DailyRatio(os.path.join(self.dir, "d2.json"), self.cfg))
        self.assertIsNone(win.sync_argv)
        win.show_screen("RECIPES")
        scr = win.screens["RECIPES"]
        self.assertFalse(scr.sync_btn.isEnabled())
        self.assertIn("not set up", scr.result.text())
        self.assertFalse(win.sync_recipes_now())
        win.close()

    def test_default_command_is_the_updater_with_the_stations_paths(self):
        argv = self.win.sync_argv
        self.assertEqual(argv[0], sys.executable)
        self.assertEqual(argv[1], os.path.join(ROOT, "updater.py"))
        self.assertEqual(argv[2], "--recipes")
        self.assertEqual(argv[argv.index("--state-dir") + 1], os.path.dirname(self.status))
        self.assertEqual(argv[argv.index("--data-dir") + 1], os.path.dirname(self.local))


if __name__ == "__main__":
    unittest.main(verbosity=1)
