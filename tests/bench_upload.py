#!/usr/bin/env python3
"""Live bench test of the uploader against the REAL server.

Sends the three batches in tests/bench_batches.jsonl (written by the real
station code: a weighed Teriyaki/buffalo batch, a fixed Piri Piri batch with
one skipped step and an unsynced clock, a typed-weight Pepper/chicken
batch), then forgets its position and sends them again. The second pass
must come back as duplicates, adding no rows.

They land in the real database with boot_id BENCH_BOOT_ID, so their
batch_ids are always:

    bec40001-0   bec40001-1   bec40001-2

Delete those before go-live.

Uses throw-away config/state folders, never the Pi's real
~/.config/mincecraft (its boot_id and offset are untouched). The write key
comes from the environment (MINCECRAFT_API_KEY) or, failing that, from
~/.config/mincecraft/api.env. Nothing is written into this repo.

    MINCECRAFT_API_KEY=<WRITE KEY> python3 tests/bench_upload.py
"""

import json
import logging
import os
import shutil
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import uploader  # noqa: E402

BENCH_BOOT_ID = "bec40001"
LINES = os.path.join(ROOT, "tests", "bench_batches.jsonl")


def main():
    real_env = os.path.join(uploader.CONFIG_DIR, "api.env")
    settings = uploader.Settings(real_env)
    if settings.problem():
        print(f"no write key: set MINCECRAFT_API_KEY or fill in {real_env}")
        return 2

    work = tempfile.mkdtemp(prefix="mincecraft-bench-")
    cfg, state = os.path.join(work, "config"), os.path.join(work, "state")
    os.makedirs(cfg)
    log_path = os.path.join(work, "batches.jsonl")
    shutil.copy(LINES, log_path)

    seen = []

    class Grab(logging.Handler):
        def emit(self, r):
            msg = r.getMessage()
            if "line=" in msg:                  # the full lines are noise here
                msg = msg.split(" line=")[0]
            seen.append(f"{r.levelname} {msg}")
            print(f"  {r.levelname} {msg}")

    uploader.log.setLevel(logging.INFO)
    uploader.log.addHandler(Grab())
    names = uploader.product_names(os.path.join(ROOT, "recipes.json"))

    def one_pass(title):
        print(f"\n--- {title}")
        st = uploader.State(cfg, state, boot_id_hint=BENCH_BOOT_ID)
        settings.env_path = real_env              # key: env or the real file
        up = uploader.Uploader(log_path, settings, st, names)
        rc = up.run(once=True)
        print(f"  exit {rc}, position now line {st.line}")
        return rc

    try:
        rc1 = one_pass("first send (3 bench batches)")
        os.remove(os.path.join(state, "upload.offset"))
        rc2 = one_pass("resend the same 3 (must be duplicates, no new rows)")
    finally:
        shutil.rmtree(work, ignore_errors=True)

    ids = [f"{BENCH_BOOT_ID}-{i}" for i in range(3)]
    print("\nbench batch_ids (delete before go-live): " + ", ".join(ids))
    print(json.dumps({"first_exit": rc1, "resend_exit": rc2}))
    return 0 if rc1 == rc2 == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
