#!/usr/bin/env python3
"""
MINCECRAFT updater — recipes from the owner's Google Sheet, code from `stable`.

THIS VERSION: v1.2 (2026-10-28)

What it does now, as a whole:
  - RECIPES FROM THE GOOGLE SHEET (when ~/.config/mincecraft/recipes.env
    holds RECIPES_SHEET_URL — the normal case): every 5 min (systemd user
    timer) and at every station start, downloads the whole sheet as .xlsx in
    one request through its "anyone with the link" view link, reads one tab
    per recipe (sheet_recipes.py), merges them with the
    settings in the repo's recipes.json (scales, tolerances, PIN, papain
    rates), checks the result with the station's own Config +
    validate_products(), and writes it atomically to
    ~/.local/share/mincecraft/recipes.json. A product with a mistake keeps
    its previous recipe and is named in the status and in the synced file —
    also when that leaves the recipes themselves unchanged; a sheet that is
    unusable as a whole (not shared, no recipe tabs, every recipe inactive)
    is rejected and the last good recipes stay. The station shows all of
    this on HOME and on its Recipes screen, whose SYNC NOW runs --recipes.
  - RECIPES FROM THE WORKBOOK ON GITHUB (only when no sheet link is set —
    before handover or for testing): fetches the repo's `origin`, reads DOKI-Recipes.xlsx and
    recipes.json from origin/main WITHOUT checking anything out, converts the
    workbook with xlsx_to_recipes.convert(), and checks the result with the
    station's own Config + validate_products(). Valid -> written atomically
    to ~/.local/share/mincecraft/recipes.json, which the station reads
    (between batches only). Invalid -> the last good recipes stay, and the
    reason is written to the status file the station shows on its HOME
    screen. A workbook sheet with a problem is skipped (that product keeps
    its previous recipe) and named in the status.
  - CODE (only at station start, from mincecraft.sh): if origin/stable has
    moved, the clone is switched to it (`git checkout -B stable <sha>`).
    The previous commit is kept as "good"; the new one is "pending" until the
    station has run for 2 minutes (mincecraft.sh then calls --mark-good). If
    the station fails before that, --rollback puts the good commit back and
    marks the new one "bad" so it is never tried again; only a newer stable
    commit is.
  - It restarts the uploader service if an update changed uploader.py.
  - On failure — no network, Google or GitHub down, git error, a bad
    sheet or workbook — it
    logs, records the reason in the status file and leaves everything as it
    was. It never stops the station from starting, never waits more than
    ~30 s for the network, and never runs while another copy runs (lock).
  - What it does NOT do: never pushes, never changes the station's batch log
    or daily ratio, never applies anything mid-batch (the station decides
    when to pick up recipes), never updates code from `main` (only
    `stable`), never installs packages, never discards local edits (a clone
    with modified tracked files is left alone and the status says so), never
    writes to the Google Sheet, never stores the sheet link in the repo.

  python3 updater.py --recipes      sync recipes now (what the timer runs)
  python3 updater.py --boot         code update (if any), then recipes
  python3 updater.py --mark-good    the running code is good
  python3 updater.py --rollback     the new code failed: go back
  python3 updater.py --status       print the status file

Exit codes: 0 ok / nothing to do, 10 = code changed (restart the station),
1 = nothing to roll back, 2 = bad arguments.

CHANGES (newest first)
  v1.2  2026-10-28  A tab with a mistake is reported even when keeping its
                    previous recipe leaves the recipes unchanged (the
                    problems are now part of the sync id). Before, the
                    station said "no change" and never named the tab — found
                    while testing SYNC NOW. Header text updated for the
                    one-tab-per-recipe layout.
  v1.1  2026-10-27  Recipes come from the owner's Google Sheet (one tab per
                    recipe) when recipes.env has its link;
                    the GitHub workbook is only used when no link is set.
                    Why: the owner edits recipes in Google Sheets, from any
                    device, without uploading files to GitHub.
  v1.0  2026-10-27  First version: workbook-driven recipe sync with
                    validation and last-good fallback; code from `stable` at
                    start with rollback.
"""

import argparse
import fcntl
import json
import logging
import logging.handlers
import os
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

FW_VERSION = "v1.2"

HERE = os.path.dirname(os.path.abspath(__file__))
HOME = os.path.expanduser("~")
STATE_DIR = os.path.join(os.environ.get("XDG_STATE_HOME") or
                         os.path.join(HOME, ".local", "state"), "mincecraft")
DATA_DIR = os.path.join(os.environ.get("XDG_DATA_HOME") or
                        os.path.join(HOME, ".local", "share"), "mincecraft")
CONFIG_DIR = os.path.join(os.environ.get("XDG_CONFIG_HOME") or
                          os.path.join(HOME, ".config"), "mincecraft")
SHEET_PLACEHOLDER = "PASTE-THE-GOOGLE-SHEET-LINK-HERE"
SHEET_TIMEOUT_S = 30

WORKBOOK = "DOKI-Recipes.xlsx"
RECIPES = "recipes.json"
CODE_BRANCH = "stable"
RECIPE_BRANCH = "main"
FETCH_TIMEOUT_S = 30
CHANGED = 10

log = logging.getLogger("updater")


def now_iso():
    return time.strftime("%Y-%m-%dT%H:%M:%S%z")


# ------------------------------------------------------------------ status

class Status:
    """update.json in the state dir: what the station shows on HOME."""

    def __init__(self, state_dir):
        self.path = os.path.join(state_dir, "update.json")
        try:
            with open(self.path, "r", encoding="utf-8") as fh:
                self.data = json.load(fh)
        except (OSError, ValueError):
            self.data = {}
        self.data.setdefault("code", {})
        self.data.setdefault("recipes", {})

    @property
    def code(self):
        return self.data["code"]

    @property
    def recipes(self):
        return self.data["recipes"]

    def save(self):
        self.data["updater_version"] = FW_VERSION
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(self.data, fh, indent=1)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, self.path)


# --------------------------------------------------------------------- git

class GitError(Exception):
    pass


class Repo:
    def __init__(self, path):
        self.path = path

    def run(self, *args, timeout=20, binary=False):
        env = dict(os.environ, GIT_TERMINAL_PROMPT="0", GIT_ASKPASS="true",
                   LC_ALL="C")
        try:
            out = subprocess.run(["git", *args], cwd=self.path, env=env,
                                 capture_output=True, timeout=timeout)
        except subprocess.TimeoutExpired:
            raise GitError(f"git {args[0]} timed out after {timeout} s") from None
        except OSError as e:
            raise GitError(f"git not runnable: {e}") from None
        if out.returncode != 0:
            msg = out.stderr.decode("utf-8", "replace").strip().splitlines()
            raise GitError(f"git {' '.join(args[:2])}: {msg[-1] if msg else out.returncode}")
        return out.stdout if binary else out.stdout.decode("utf-8", "replace").strip()

    def ok(self, *args):
        try:
            self.run(*args)
            return True
        except GitError:
            return False

    def fetch(self):
        self.run("fetch", "--quiet", "--prune", "origin", timeout=FETCH_TIMEOUT_S)

    def sha(self, ref):
        try:
            return self.run("rev-parse", "--verify", "-q", f"{ref}^{{commit}}")
        except GitError:
            return None

    def head(self):
        return self.sha("HEAD")

    def blob(self, ref, path):
        """(blob sha, bytes) of a file at a ref, or (None, None)."""
        try:
            sha = self.run("rev-parse", "--verify", "-q", f"{ref}:{path}")
            return sha, self.run("cat-file", "blob", sha, binary=True)
        except GitError:
            return None, None

    def dirty(self):
        return bool(self.run("status", "--porcelain", "--untracked-files=no"))

    def changed(self, a, b, path):
        return not self.ok("diff", "--quiet", a, b, "--", path)


# ----------------------------------------------------------------- recipes

def validate(cfg_dict):
    """Problems with a recipes dict, as the station would see them."""
    sys.path.insert(0, HERE)
    from scale import Config                    # station's own rules
    try:
        cfg = Config(cfg_dict)
    except Exception as e:                      # a missing key, a wrong type
        return [f"recipes.json is not usable: {type(e).__name__}: {e}"]
    problems = list(cfg.validate_products())
    usable = [p for p in cfg.products if not cfg.is_draft(p["id"])]
    if not usable:
        problems.append("no recipe has any ingredients")
    return problems


def write_json_atomic(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), prefix=".recipes-")
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(obj, fh, indent=2, ensure_ascii=False)
        fh.write("\n")
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)


def sync_recipes(repo, status, data_dir, fetch=True):
    """Bring ~/.local/share/mincecraft/recipes.json up to origin/main's
    workbook. Returns True if a new file was written."""
    rs = status.recipes
    rs["checked_at"] = now_iso()
    if fetch:
        try:
            repo.fetch()
            rs["online"] = True
        except GitError as e:
            rs["online"] = False
            rs["last_error"] = f"could not reach GitHub ({e}) — recipes unchanged"
            log.warning(rs["last_error"])
            status.save()
            return False

    ref = f"origin/{RECIPE_BRANCH}"
    commit = repo.sha(ref)
    wb_sha, wb_bytes = repo.blob(ref, WORKBOOK)
    rj_sha, rj_bytes = repo.blob(ref, RECIPES)
    if not commit or wb_sha is None:
        rs["last_error"] = f"{WORKBOOK} not found on {ref} — recipes unchanged"
        log.warning(rs["last_error"])
        status.save()
        return False
    source = f"{wb_sha}:{rj_sha}"
    if source == rs.get("applied_source"):
        rs.pop("last_error", None)
        status.save()
        return False                            # nothing new
    if source == rs.get("rejected_source"):
        status.save()                           # already reported
        return False

    try:
        base = json.loads(rj_bytes.decode("utf-8")) if rj_bytes else None
    except ValueError as e:
        base = None
        bad_base = f"{RECIPES} on {ref} is not valid JSON ({e})"
    else:
        bad_base = None if base is not None else f"{RECIPES} missing on {ref}"
    if base is None:
        return _reject(rs, status, source, commit, bad_base)

    sys.path.insert(0, HERE)
    import xlsx_to_recipes
    with tempfile.TemporaryDirectory() as tmp:
        wb_path = os.path.join(tmp, WORKBOOK)
        with open(wb_path, "wb") as fh:
            fh.write(wb_bytes)
        try:
            cfg, ready, skipped, added, updated = xlsx_to_recipes.convert(wb_path, base)
        except Exception as e:
            return _reject(rs, status, source, commit,
                           f"{WORKBOOK} could not be read ({type(e).__name__}: {e})")

    problems = validate(cfg)
    if problems:
        return _reject(rs, status, source, commit, "; ".join(problems[:3]))

    cfg["_sync"] = {
        "source": ref, "commit": commit, "workbook_blob": wb_sha,
        "recipes_blob": rj_sha, "applied_at": now_iso(),
        "updater_version": FW_VERSION,
        "skipped_sheets": [{"sheet": t, "problems": p} for t, p in skipped],
    }
    path = os.path.join(data_dir, RECIPES)
    write_json_atomic(path, cfg)
    rs.update({
        "ok": True, "applied_source": source, "commit": commit,
        "applied_at": cfg["_sync"]["applied_at"],
        "skipped_sheets": cfg["_sync"]["skipped_sheets"],
        "updated": updated, "added": added,
    })
    rs.pop("last_error", None)
    rs.pop("rejected_source", None)
    rs.pop("rejected", None)
    status.save()
    log.info("recipes from %s (%s): %d sheet(s) used, %d skipped -> %s",
             ref, commit[:8], len(ready), len(skipped), path)
    for title, probs in skipped:
        log.warning("sheet %r not used, previous recipe kept: %s",
                    title, "; ".join(probs))
    return True


def _reject(rs, status, source, commit, why):
    rs.update({"ok": False, "rejected_source": source,
               "rejected": {"commit": commit, "why": why, "at": now_iso()}})
    status.save()
    log.error("recipe update from %s REJECTED, last good recipes kept: %s",
              commit[:8] if commit else "?", why)
    return False


# ------------------------------------------------------------ google sheet

def read_env(path):
    out = {}
    try:
        with open(path, "r", encoding="utf-8") as fh:
            for raw in fh:
                line = raw.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    out[k.strip()] = v.strip().strip("'\"")
    except OSError:
        pass
    return out


def sheet_url(config_dir):
    """The owner's sheet link from recipes.env, or None if not set."""
    url = os.environ.get("RECIPES_SHEET_URL") or \
        read_env(os.path.join(config_dir, "recipes.env")).get("RECIPES_SHEET_URL", "")
    url = url.strip()
    return url if url and url != SHEET_PLACEHOLDER else None


class Offline(Exception):
    pass


def download(url, timeout=SHEET_TIMEOUT_S):
    """The sheet as .xlsx bytes. Offline -> Offline; anything that is not a
    spreadsheet comes back as bytes and sheet_recipes says why."""
    req = urllib.request.Request(url, headers={
        "User-Agent": f"mincecraft-updater/{FW_VERSION}"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.read()
    except urllib.error.HTTPError as e:
        if e.code in (401, 403, 404):
            return b""                       # not shared / wrong link
        raise Offline(f"HTTP {e.code}") from None
    except (urllib.error.URLError, OSError) as e:
        raise Offline(str(getattr(e, "reason", e))) from None


def sync_from_sheet(repo_path, status, data_dir, url):
    """Bring the local recipes up to the owner's sheet. Returns True if a new
    file was written."""
    sys.path.insert(0, HERE)
    import sheet_recipes as sr
    rs = status.recipes
    rs["checked_at"] = now_iso()
    rs["source"] = "google-sheet"
    try:
        data = download(sr.export_url(url))
        rs["online"] = True
    except Offline as e:
        rs["online"] = False
        rs["last_error"] = f"could not reach the Google Sheet ({e}) — recipes unchanged"
        log.warning(rs["last_error"])
        status.save()
        return False

    try:
        parsed = sr.read_sheet(data)
    except sr.SheetError as e:
        msg = str(e)
        if rs.get("rejected_source") == f"err:{msg}":
            status.save()                       # already reported
            return False
        return _reject(rs, status, f"err:{msg}", "sheet", msg[:1].upper() + msg[1:] + ".")

    try:
        with open(os.path.join(repo_path, RECIPES), "r", encoding="utf-8") as fh:
            base = json.load(fh)
    except (OSError, ValueError) as e:
        return _reject(rs, status, "err:base", "sheet",
                       f"the station's own {RECIPES} is unreadable ({e})")
    local = os.path.join(data_dir, RECIPES)
    try:
        with open(local, "r", encoding="utf-8") as fh:
            previous = json.load(fh)
    except (OSError, ValueError):
        previous = base

    cfg, kept, dropped = sr.merge(base, previous, parsed)
    # The problems are part of the id: a mistake that leaves the recipes
    # unchanged (its product keeps the previous version) must still reach
    # the station, and so must fixing it.
    source = "sheet-" + sr.digest(dict(cfg, _problems=parsed["problems"],
                                       _inactive=parsed["inactive"]))
    if source == rs.get("applied_source"):
        rs.pop("last_error", None)
        status.save()
        return False                            # nothing new
    if source == rs.get("rejected_source"):
        status.save()
        return False
    problems = validate(cfg)
    if problems:
        return _reject(rs, status, source, "sheet", "; ".join(problems[:3]))

    skipped = [{"sheet": name, "problems": parsed["problems"][name],
                "kept_previous": name in kept} for name in parsed["problems"]]
    cfg["_sync"] = {
        "source": "google-sheet", "commit": source,
        "sheet_id": sr.sheet_id(url), "applied_at": now_iso(),
        "updater_version": FW_VERSION, "skipped_sheets": skipped,
        "inactive": parsed["inactive"],
    }
    write_json_atomic(local, cfg)
    rs.update({"ok": True, "applied_source": source, "commit": source,
               "applied_at": cfg["_sync"]["applied_at"],
               "skipped_sheets": skipped, "dropped": dropped})
    for k in ("last_error", "rejected_source", "rejected", "updated", "added"):
        rs.pop(k, None)
    status.save()
    log.info("recipes from the Google Sheet (%s): %d product(s), %d with a "
             "problem -> %s", source, len(cfg["products"]), len(skipped), local)
    for item in skipped:
        log.warning("product %r not taken from the sheet (%s): %s", item["sheet"],
                    "previous recipe kept" if item["kept_previous"] else "left out",
                    "; ".join(item["problems"]))
    return True


# -------------------------------------------------------------------- code

def systemctl(*args):
    cmd = os.environ.get("MINCECRAFT_SYSTEMCTL", "systemctl")
    try:
        subprocess.run([cmd, "--user", *args], capture_output=True, timeout=20)
    except (OSError, subprocess.SubprocessError):
        pass


def update_code(repo, status, fetch=True):
    """Switch to origin/stable if it moved. Returns True if the code changed."""
    cs = status.code
    cs["checked_at"] = now_iso()
    head = repo.head()
    cs.setdefault("good", head)                 # first run: what runs now is good
    if fetch:
        try:
            repo.fetch()
        except GitError as e:
            cs["last_error"] = f"could not reach GitHub ({e}) — code unchanged"
            log.warning(cs["last_error"])
            status.save()
            return False
    target = repo.sha(f"origin/{CODE_BRANCH}")
    if not target:
        cs["last_error"] = (f"no '{CODE_BRANCH}' branch on GitHub — code updates "
                            f"are off until someone creates it")
        status.save()
        return False
    if target == head:
        cs.pop("last_error", None)
        status.save()
        return False
    if target in cs.get("bad", []):
        cs["last_error"] = (f"{CODE_BRANCH} is {target[:8]}, which failed to start "
                            f"before — waiting for a newer commit")
        status.save()
        return False
    if repo.dirty():
        cs["last_error"] = ("files in the Pi's copy were edited by hand — code "
                            "updates paused until that is cleaned up")
        log.error(cs["last_error"])
        status.save()
        return False
    try:
        repo.run("checkout", "-q", "-B", CODE_BRANCH, target)
    except GitError as e:
        cs["last_error"] = f"could not switch to {target[:8]}: {e}"
        log.error(cs["last_error"])
        status.save()
        return False
    if cs.get("pending") is None:
        cs["good"] = head                       # the code that ran fine until now
    cs.update({"pending": target, "updated_at": now_iso(),
               "from": head, "to": target})
    cs.pop("last_error", None)
    cs.pop("rolled_back", None)
    status.save()
    log.info("code updated %s -> %s (pending until it runs)", head[:8], target[:8])
    if repo.changed(head, target, "uploader.py"):
        systemctl("try-restart", "mincecraft-uploader.service")
        log.info("uploader.py changed — uploader service restarted")
    return True


def mark_good(repo, status):
    cs = status.code
    head = repo.head()
    cs["good"] = head
    cs["pending"] = None
    status.save()
    log.info("code %s marked good", head[:8])
    return 0


def rollback(repo, status):
    cs = status.code
    head = repo.head()
    pending, good = cs.get("pending"), cs.get("good")
    if not pending or pending != head or not good or good == head:
        log.info("nothing to roll back (head %s, pending %s)",
                 head[:8] if head else "?", (pending or "none")[:8])
        return 1
    try:
        repo.run("checkout", "-q", "-B", CODE_BRANCH, good)
    except GitError as e:
        cs["last_error"] = f"rollback to {good[:8]} failed: {e}"
        log.error(cs["last_error"])
        status.save()
        return 1
    bad = cs.setdefault("bad", [])
    if pending not in bad:
        bad.append(pending)
    cs["pending"] = None
    cs["rolled_back"] = {"from": pending, "to": good, "at": now_iso()}
    status.save()
    log.error("code %s failed to start — rolled back to %s", pending[:8], good[:8])
    if repo.changed(pending, good, "uploader.py"):
        systemctl("try-restart", "mincecraft-uploader.service")
    return CHANGED


# -------------------------------------------------------------------- main

def setup_logging(state_dir):
    os.makedirs(state_dir, exist_ok=True)
    log.setLevel(logging.INFO)
    if log.handlers:
        return
    out = logging.StreamHandler(sys.stdout)
    out.setFormatter(logging.Formatter("updater: %(levelname)s %(message)s"))
    log.addHandler(out)
    fh = logging.handlers.RotatingFileHandler(
        os.path.join(state_dir, "updater.log"), maxBytes=2_000_000,
        backupCount=5, encoding="utf-8")
    fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    log.addHandler(fh)


def main(argv=None):
    ap = argparse.ArgumentParser(description="MINCECRAFT updater")
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--recipes", action="store_true")
    g.add_argument("--boot", action="store_true")
    g.add_argument("--mark-good", action="store_true")
    g.add_argument("--rollback", action="store_true")
    g.add_argument("--status", action="store_true")
    ap.add_argument("--repo", default=HERE)
    ap.add_argument("--state-dir", default=STATE_DIR)
    ap.add_argument("--data-dir", default=DATA_DIR)
    ap.add_argument("--config-dir", default=CONFIG_DIR)
    ap.add_argument("--version", action="version",
                    version=f"MINCECRAFT updater {FW_VERSION}")
    args = ap.parse_args(argv)

    setup_logging(args.state_dir)
    status = Status(args.state_dir)
    if args.status:
        print(json.dumps(status.data, indent=1))
        return 0

    os.makedirs(args.state_dir, exist_ok=True)
    lock = open(os.path.join(args.state_dir, "update.lock"), "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        log.info("another update is running — skipped")
        return 0
    repo = Repo(args.repo)
    if not repo.head():
        log.error("%s is not a git clone — nothing to update", args.repo)
        return 0

    if args.mark_good:
        return mark_good(repo, status)
    if args.rollback:
        return rollback(repo, status)
    url = sheet_url(args.config_dir)
    if args.recipes:
        if url:
            sync_from_sheet(args.repo, status, args.data_dir, url)
        else:
            sync_recipes(repo, status, args.data_dir)
        return 0
    # --boot: one fetch for both. Offline is normal at boot (Wi-Fi still
    # coming up): start with what is there, the timer catches up later.
    changed = False
    try:
        repo.fetch()
        fetched = True
    except GitError as e:
        fetched = False
        why = f"could not reach GitHub ({e}) — code unchanged"
        status.code["last_error"] = why
        status.code["checked_at"] = now_iso()
        status.save()
        log.warning(why)
    if fetched:
        changed = update_code(repo, status, fetch=False)
    if url:
        # The sheet is a different site from GitHub: try it either way. After
        # a code update the merge uses the new code's recipes.json.
        sync_from_sheet(args.repo, status, args.data_dir, url)
    elif fetched:
        status.recipes["online"] = True
        sync_recipes(repo, status, args.data_dir, fetch=False)
    else:
        status.recipes.update({"online": False, "checked_at": now_iso(),
                               "last_error": status.code["last_error"]})
        status.save()
    return CHANGED if changed else 0


if __name__ == "__main__":
    sys.exit(main())
