#!/usr/bin/env python3
"""
MINCECRAFT batch uploader — batches.jsonl -> the DOKI server.

THIS VERSION: v1.0 (2026-10-21)

What it does now, as a whole:
  - Runs as its own process (systemd user service mincecraft-uploader), never
    inside the station. The station does not know it exists.
  - Every 5 s, looks for new COMPLETE lines (ending in \\n) in batches.jsonl,
    which it only ever opens read-only. Each line is one finished batch.
  - Maps each line to one "batch" message and POSTs up to 50 at a time as a
    JSON array to $MINCECRAFT_API_URL (/ingest) with the X-API-Key header.
  - Its place in the file is an offset file it moves forward ONLY after the
    server has answered 200 / 207 / 422 (stored, partly rejected, all
    rejected — rejected batches are logged with the server's error and never
    retried, since they would never be accepted unchanged).
  - Identity makes resending safe: boot_id (8 hex, made once, kept in
    ~/.config/mincecraft/uploader.json), seq = 0-based line number,
    batch_id = "<boot_id>-<seq>". The server ignores duplicates.
  - A skipped step (no actual) is sent as actual_g 0, measured false: the
    server requires a number there.
  - Times are sent in UTC ("...Z") only when the line says clock_synced is
    true; otherwise ts / started_at / finished_at are null. Lines written
    before the flag existed count as not synced. Never a guessed time.
  - On failure: network down, timeout, 5xx or 429 -> offset untouched, retry
    with backoff (5 s doubling to 5 min). 413 -> split the request in half;
    a single batch still too large is logged and skipped. 400 (a bug here),
    401/403 (wrong key), 404 (wrong URL) -> offset untouched, logged, retried
    every 5 min with api.env re-read each time, so fixing the file is enough.
  - If batches.jsonl is reset or replaced (its first line changes, or it gets
    shorter than the offset), a new boot_id is made and it starts from line 0.
  - What it does NOT do: never writes batches.jsonl, never deletes anything,
    never blocks or slows the station, never reads the key from the repo
    (only ~/.config/mincecraft/api.env or the environment), never logs it.

  python3 uploader.py               run forever (what the service does)
  python3 uploader.py --once        send what is pending, then exit
  python3 uploader.py --dry-run     print the messages it would send; sends
                                    nothing, moves nothing

Logs: stdout (journalctl --user -u mincecraft-uploader) and
~/.local/state/mincecraft/uploader.log, which keeps the full original line
of every batch it sends or skips.

CHANGES (newest first)
  v1.0  2026-10-21  First version.
"""

import argparse
import hashlib
import json
import logging
import logging.handlers
import os
import random
import secrets
import socket
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone

FW_VERSION = "v1.0"

HERE = os.path.dirname(os.path.abspath(__file__))
HOME = os.path.expanduser("~")
CONFIG_DIR = os.path.join(os.environ.get("XDG_CONFIG_HOME") or
                          os.path.join(HOME, ".config"), "mincecraft")
STATE_DIR = os.path.join(os.environ.get("XDG_STATE_HOME") or
                         os.path.join(HOME, ".local", "state"), "mincecraft")

DEFAULT_URL = "https://api-production-2ef9.up.railway.app/ingest"
DEFAULT_DEVICE = "weigh-1"
KEY_PLACEHOLDER = "PUT-THE-WRITE-KEY-HERE"

MAX_PER_REQUEST = 50          # the server takes "at most ~50" per request
READ_CHUNK = 4 * 1024 * 1024  # bytes read from batches.jsonl per pass
POLL_S = 5.0                  # idle check interval
BACKOFF_MIN_S = 5.0
BACKOFF_MAX_S = 300.0
HOLD_S = 300.0                # wrong key / bug / wrong URL: retry this often
TIMEOUT_S = 20.0

STORED = (200, 207, 422)      # server has decided: move on
TRANSIENT = (408, 425, 429)   # plus every 5xx: try again later

log = logging.getLogger("uploader")


# ------------------------------------------------------------------ config

def read_env_file(path):
    """KEY=VALUE lines; # comments; optional quotes. Missing file -> {}."""
    out = {}
    try:
        with open(path, "r", encoding="utf-8") as fh:
            for raw in fh:
                line = raw.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                k = k.strip()
                if k.startswith("export "):
                    k = k[len("export "):].strip()
                v = v.strip()
                if len(v) >= 2 and v[0] == v[-1] and v[0] in "'\"":
                    v = v[1:-1]
                out[k] = v
    except FileNotFoundError:
        pass
    return out


class Settings:
    """Where to send, with what key, as which device. Re-read before every
    attempt, so a corrected api.env takes effect without a restart."""

    def __init__(self, env_path):
        self.env_path = env_path
        self.reload()

    def reload(self):
        vals = read_env_file(self.env_path)
        for k in ("MINCECRAFT_API_URL", "MINCECRAFT_API_KEY", "MINCECRAFT_DEVICE"):
            if os.environ.get(k):
                vals[k] = os.environ[k]
        self.url = vals.get("MINCECRAFT_API_URL") or DEFAULT_URL
        self.key = vals.get("MINCECRAFT_API_KEY") or ""
        self.device = vals.get("MINCECRAFT_DEVICE") or DEFAULT_DEVICE
        return self

    def problem(self):
        if not self.key or self.key == KEY_PLACEHOLDER:
            return (f"no write key — put MINCECRAFT_API_KEY in {self.env_path}")
        if not self.url.startswith(("https://", "http://")):
            return f"MINCECRAFT_API_URL is not a URL: {self.url!r}"
        return None


def product_names(recipes_path):
    """Product id -> display name from recipes.json. Any problem -> {} and the
    id is sent instead; the uploader must not stop over a recipe file."""
    try:
        with open(recipes_path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        return {p["id"]: p.get("name") or p["id"] for p in data.get("products", [])
                if isinstance(p, dict) and "id" in p}
    except (OSError, ValueError, TypeError, KeyError):
        return {}


# ----------------------------------------------------------------- mapping

def to_utc(stamp):
    """'2026-10-20T13:46:30+0530' -> '2026-10-20T08:16:30Z'.

    The station writes local time WITH its UTC offset (time.strftime %z), so
    the conversion is exact. A stamp with no offset cannot be placed in UTC
    without guessing the zone, so it becomes None — as does anything that
    does not parse.
    """
    if not isinstance(stamp, str) or not stamp:
        return None
    s = stamp.strip()
    if s.endswith("Z"):
        s = s[:-1] + "+0000"
    for fmt in ("%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%S.%f%z"):
        try:
            dt = datetime.strptime(s, fmt)
        except ValueError:
            continue
        if dt.tzinfo is None:
            return None
        return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    try:                                   # "+05:30" style offsets
        dt = datetime.fromisoformat(stamp.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        return None
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _num(v, nd=2):
    if v is None:
        return None
    try:
        return round(float(v), nd)
    except (TypeError, ValueError):
        return None


def batch_id(boot_id, seq):
    return f"{boot_id}-{seq}"


def map_record(rec, seq, boot_id, device, names=None):
    """One batches.jsonl record -> one server message."""
    names = names or {}
    synced = rec.get("clock_synced") is True   # absent (older lines) = not synced
    started = to_utc(rec.get("started_at")) if synced else None
    finished = to_utc(rec.get("logged_at")) if synced else None

    # Lines from before the "batch" key existed: a fixed batch is the one
    # with no meat weight.
    batch_type = rec.get("batch") or ("fixed" if rec.get("base_weight_g") is None
                                      else "per_meat")
    fixed = batch_type == "fixed"
    base = rec.get("base_weight_g")
    base = None if (fixed or base is None) else int(round(float(base)))
    # Lines from before typed weights existed were all weighed.
    source = None if fixed else (rec.get("base_weight_source") or "scale")

    steps = []
    for i, s in enumerate(rec.get("steps") or [], start=1):
        actual = _num(s.get("actual_g"))
        # A step skipped behind the PIN has no actual (null in the line). The
        # server requires a number — a null rejects the whole batch for good
        # (found in the live bench test, 2026-10-21) — so it goes as 0 g,
        # measured: false. Nothing was weighed in.
        skipped = bool(s.get("skipped")) or actual is None
        steps.append({
            "step_no": i,                         # the order they were added
            "ingredient": s.get("name"),
            "scale": s.get("weighed_on") or "main",
            "target_g": _num(s.get("target_g")) or 0.0,
            "actual_g": 0.0 if skipped else actual,
            # Small-scale steps record the target, not a reading ("assumed").
            "measured": not skipped and not s.get("assumed"),
        })

    product = rec.get("product")
    return {
        "device": device,
        "boot_id": boot_id,
        "seq": seq,
        "ts": finished,
        "type": "batch",
        "data": {
            "batch_id": batch_id(boot_id, seq),
            "product": names.get(product, product),
            "batch_type": batch_type,
            "base_weight_g": base,
            "base_source": source,
            "meat_type": rec.get("meat_type"),
            "started_at": started,
            "finished_at": finished,
            "steps": steps,
        },
    }


# ------------------------------------------------------------------- state

def _atomic_write_json(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = f"{path}.tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(obj, fh, indent=1)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)


def _read_json(path):
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None


def new_boot_id():
    return secrets.token_hex(4)               # 8 lowercase hex chars


class State:
    """boot_id + fingerprint of batches.jsonl (uploader.json), and the
    position reached in it (upload.offset): byte offset and line number."""

    def __init__(self, config_dir, state_dir, boot_id_hint=None):
        self.ident_path = os.path.join(config_dir, "uploader.json")
        self.offset_path = os.path.join(state_dir, "upload.offset")
        ident = _read_json(self.ident_path) or {}
        pos = _read_json(self.offset_path) or {}
        # The offset file also carries the boot_id, so losing uploader.json
        # does not mint a new id and resend every batch under new batch_ids.
        self.boot_id = ident.get("boot_id") or pos.get("boot_id")
        if not self.boot_id:
            self.boot_id = boot_id_hint or new_boot_id()
        self.fingerprint = ident.get("fingerprint")
        if pos.get("boot_id") == self.boot_id:
            self.offset = int(pos.get("offset", 0))
            self.line = int(pos.get("line", 0))
        else:
            self.offset, self.line = 0, 0
        self.save_ident()

    def save_ident(self):
        _atomic_write_json(self.ident_path, {
            "boot_id": self.boot_id, "fingerprint": self.fingerprint,
            "note": "boot_id is made once. A new one only if batches.jsonl "
                    "is reset or replaced."})

    def commit(self, offset, line):
        if offset <= self.offset:
            return
        self.offset, self.line = offset, line
        _atomic_write_json(self.offset_path, {
            "boot_id": self.boot_id, "offset": offset, "line": line})

    def reset(self, fingerprint, why):
        old = self.boot_id
        self.boot_id = new_boot_id()
        self.fingerprint = fingerprint
        self.offset, self.line = 0, 0
        self.save_ident()
        _atomic_write_json(self.offset_path,
                           {"boot_id": self.boot_id, "offset": 0, "line": 0})
        log.warning("batches.jsonl %s: new boot_id %s (was %s), starting from "
                    "line 0", why, self.boot_id, old)


def first_line(path):
    """First complete line of the file, or None."""
    with open(path, "rb") as fh:
        head = fh.readline(READ_CHUNK)
    return head if head.endswith(b"\n") else None


def check_identity(state, path):
    """Detect a reset / replaced batches.jsonl. Returns False if there is
    nothing to read yet."""
    if not os.path.exists(path):
        return False
    head = first_line(path)
    if head is None:
        return False
    fp = hashlib.sha256(head).hexdigest()
    size = os.path.getsize(path)
    if state.fingerprint is None:
        state.fingerprint = fp
        state.save_ident()
    elif fp != state.fingerprint:
        state.reset(fp, "was replaced (its first line changed)")
    elif size < state.offset:
        state.reset(fp, f"got shorter ({size} < offset {state.offset})")
    return True


def read_pending(path, offset, line, limit):
    """Complete lines after `offset`, at most `limit` that parse.

    Returns units: dicts with seq, end (byte offset after the line), raw
    (bytes), and rec (parsed dict, or None if the line is not valid JSON).
    """
    with open(path, "rb") as fh:
        fh.seek(offset)
        chunk = fh.read(READ_CHUNK)
    end = chunk.rfind(b"\n")
    if end < 0:
        return []
    units, pos, seq, parsed = [], 0, line, 0
    body = chunk[:end + 1]
    while pos < len(body) and parsed < limit:
        nl = body.index(b"\n", pos)
        raw = body[pos:nl]
        rec = None
        if raw.strip():
            try:
                rec = json.loads(raw.decode("utf-8"))
                if not isinstance(rec, dict):
                    rec = None
            except (UnicodeDecodeError, ValueError):
                rec = None
        units.append({"seq": seq, "end": offset + nl + 1, "raw": raw, "rec": rec})
        if rec is not None:
            parsed += 1
        pos, seq = nl + 1, seq + 1
    return units


# -------------------------------------------------------------------- HTTP

class Transient(Exception):
    """Network trouble: nothing was decided, try again later."""


def post(url, key, messages, timeout=TIMEOUT_S):
    body = json.dumps(messages, separators=(",", ":")).encode("utf-8")
    req = urllib.request.Request(url, data=body, method="POST", headers={
        "Content-Type": "application/json",
        "X-API-Key": key,
        "User-Agent": f"mincecraft-uploader/{FW_VERSION}",
    })
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        try:
            return e.code, e.read()
        except Exception:                      # body unreadable — still a status
            return e.code, b""
    except (urllib.error.URLError, socket.timeout, TimeoutError,
            ConnectionError, OSError) as e:
        raise Transient(f"{type(e).__name__}: {getattr(e, 'reason', e)}") from None


def _walk(node):
    if isinstance(node, dict):
        yield node
        for v in node.values():
            yield from _walk(v)
    elif isinstance(node, list):
        for v in node:
            yield from _walk(v)


def result_items(body):
    """Per-message results in a response, whatever the exact layout: every
    object that carries a status-like field."""
    try:
        doc = json.loads(body.decode("utf-8")) if body else None
    except (UnicodeDecodeError, ValueError):
        return None, []
    items = [d for d in _walk(doc)
             if isinstance(d, dict)
             and any(isinstance(d.get(k), str) for k in ("status", "result", "outcome"))
             and d is not doc]
    return doc, items


def _status_of(item):
    for k in ("status", "result", "outcome"):
        if isinstance(item.get(k), str):
            return item[k].lower()
    return "?"


# ------------------------------------------------------------------ uploader

class Uploader:
    def __init__(self, batch_log, settings, state, names, dry_run=False):
        self.batch_log = batch_log
        self.settings = settings
        self.state = state
        self.names = names
        self.dry_run = dry_run
        self.backoff = BACKOFF_MIN_S

    def has_pending(self):
        try:
            return os.path.getsize(self.batch_log) > self.state.offset
        except OSError:
            return False

    def build(self, units):
        """Messages for the units that parsed; log the ones that did not."""
        group = []
        for u in units:
            if u["rec"] is None:
                if u["raw"].strip():
                    log.error("line %d is not valid JSON — skipped, never sent: %s",
                              u["seq"], u["raw"].decode("utf-8", "replace"))
                continue
            msg = map_record(u["rec"], u["seq"], self.state.boot_id,
                             self.settings.device, self.names)
            group.append((u, msg))
        return group

    def log_results(self, status, body, group):
        doc, items = result_items(body)
        by_id = {msg["data"]["batch_id"]: (u, msg) for u, msg in group}
        by_seq = {msg["seq"]: (u, msg) for u, msg in group}
        counts = {}
        for it in items:
            st = _status_of(it)
            counts[st] = counts.get(st, 0) + 1
            if st == "rejected":
                key = it.get("batch_id") or (it.get("data") or {}).get("batch_id")
                hit = by_id.get(key) or by_seq.get(it.get("seq"))
                log.error("REJECTED by server: error=%r item=%s line=%s",
                          it.get("error"), json.dumps(it),
                          hit[0]["raw"].decode("utf-8", "replace") if hit else "?")
        if status == 422 and not any(_status_of(i) == "rejected" for i in items):
            # All rejected, but no per-item detail to log: log the body and
            # every line, so nothing disappears unexplained.
            log.error("422 all rejected: %s", body[:2000].decode("utf-8", "replace"))
            for u, _ in group:
                log.error("  rejected line %d: %s", u["seq"],
                          u["raw"].decode("utf-8", "replace"))
        summary = ", ".join(f"{k}={v}" for k, v in sorted(counts.items())) or \
            (body[:200].decode("utf-8", "replace") if body else "no body")
        ids = f"{group[0][1]['data']['batch_id']}..{group[-1][1]['data']['batch_id']}"
        log.info("HTTP %d for %d batch(es) %s: %s", status, len(group), ids, summary)
        return doc

    def deliver(self, group):
        """Send a group, splitting on 413. Returns 'ok', 'retry' or 'hold'.
        The offset is committed after each part the server has decided on."""
        queue = [group]
        while queue:
            part = queue.pop(0)
            msgs = [m for _, m in part]
            try:
                status, body = post(self.settings.url, self.settings.key, msgs)
            except Transient as e:
                log.warning("network: %s — %d batch(es) kept, will retry",
                            e, len(part))
                return "retry"
            if status in STORED:
                for u, msg in part:
                    log.info("sent seq=%d batch_id=%s line=%s", u["seq"],
                             msg["data"]["batch_id"],
                             u["raw"].decode("utf-8", "replace"))
                self.log_results(status, body, part)
                last = part[-1][0]
                self.state.commit(last["end"], last["seq"] + 1)
                continue
            text = body[:1000].decode("utf-8", "replace") if body else ""
            if status == 413:
                if len(part) > 1:
                    half = len(part) // 2
                    log.warning("413 for %d batches — splitting", len(part))
                    queue[0:0] = [part[:half], part[half:]]
                    continue
                u, msg = part[0]
                log.error("413 for a single batch %s — it can never be accepted "
                          "as it is; skipped. line=%s", msg["data"]["batch_id"],
                          u["raw"].decode("utf-8", "replace"))
                self.state.commit(u["end"], u["seq"] + 1)
                continue
            if status >= 500 or status in TRANSIENT:
                log.warning("server %d — kept, will retry: %s", status, text)
                return "retry"
            if status in (401, 403):
                log.error("server %d: the write key was refused — check "
                          "MINCECRAFT_API_KEY in %s. Nothing skipped. %s",
                          status, self.settings.env_path, text)
            elif status == 400:
                log.error("server 400 (a bug in this uploader — please report "
                          "with this line). Nothing skipped. %s", text)
            else:
                log.error("server %d — check MINCECRAFT_API_URL (%s). Nothing "
                          "skipped. %s", status, self.settings.url, text)
            return "hold"
        return "ok"

    def step(self):
        """One pass. Returns 'idle', 'ok', 'retry' or 'hold'."""
        self.settings.reload()
        if not check_identity(self.state, self.batch_log):
            return "idle"
        if not self.has_pending():
            return "idle"
        units = read_pending(self.batch_log, self.state.offset, self.state.line,
                             MAX_PER_REQUEST)
        if not units:
            return "idle"
        problem = self.settings.problem()
        if problem:
            log.error("%s — nothing sent, will look again", problem)
            return "hold"
        group = self.build(units)
        if not group:                          # only unparseable lines
            last = units[-1]
            self.state.commit(last["end"], last["seq"] + 1)
            return "ok"
        outcome = self.deliver(group)
        if outcome == "ok":
            # Unparseable lines after the last good one are behind us too.
            last = units[-1]
            if last["seq"] + 1 > self.state.line:
                self.state.commit(last["end"], last["seq"] + 1)
        return outcome

    def dry(self):
        """Print every pending message. Sends nothing, commits nothing."""
        if not os.path.exists(self.batch_log):
            print(f"no {self.batch_log} yet")
            return 0
        head = first_line(self.batch_log)
        if head and self.state.fingerprint and \
                hashlib.sha256(head).hexdigest() != self.state.fingerprint:
            print("note: batches.jsonl was replaced — a real run would start "
                  "again from line 0 with a new boot_id")
        offset, line, n = self.state.offset, self.state.line, 0
        while True:
            units = read_pending(self.batch_log, offset, line, MAX_PER_REQUEST)
            if not units:
                break
            for u, msg in self.build(units):
                print(json.dumps(msg, indent=1))
                n += 1
            offset, line = units[-1]["end"], units[-1]["seq"] + 1
        print(f"{n} batch(es) would be sent to {self.settings.url} "
              f"as device {self.settings.device}")
        return 0

    def run(self, once=False):
        if self.dry_run:
            return self.dry()
        while True:
            outcome = self.step()
            if outcome == "ok":
                self.backoff = BACKOFF_MIN_S
                if self.has_pending():
                    continue                    # more to send: no pause
                if once:
                    return 0
                time.sleep(POLL_S)
            elif outcome == "idle":
                if once:
                    return 0
                time.sleep(POLL_S)
            elif outcome == "retry":
                if once:
                    return 1
                wait = self.backoff * random.uniform(0.8, 1.2)
                log.info("retrying in %.0f s", wait)
                time.sleep(wait)
                self.backoff = min(BACKOFF_MAX_S, self.backoff * 2)
            else:                               # hold
                if once:
                    return 2
                time.sleep(HOLD_S)


# -------------------------------------------------------------------- main

def setup_logging(state_dir, verbose=False):
    os.makedirs(state_dir, exist_ok=True)
    log.setLevel(logging.DEBUG if verbose else logging.INFO)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
    out = logging.StreamHandler(sys.stdout)
    out.setFormatter(logging.Formatter("%(levelname)s %(message)s"))
    log.addHandler(out)
    fh = logging.handlers.RotatingFileHandler(
        os.path.join(state_dir, "uploader.log"),
        maxBytes=10 * 1024 * 1024, backupCount=20, encoding="utf-8")
    fh.setFormatter(fmt)
    log.addHandler(fh)


def main(argv=None):
    ap = argparse.ArgumentParser(description="MINCECRAFT batch uploader")
    ap.add_argument("--log", default=os.path.join(HERE, "batches.jsonl"),
                    help="the station's batch log (read-only)")
    ap.add_argument("--recipes", default=os.path.join(HERE, "recipes.json"))
    ap.add_argument("--config-dir", default=CONFIG_DIR)
    ap.add_argument("--state-dir", default=STATE_DIR)
    ap.add_argument("--boot-id", help="only used if no boot_id exists yet "
                                      "(bench tests); 8 lowercase hex chars")
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--version", action="version",
                    version=f"MINCECRAFT uploader {FW_VERSION}")
    args = ap.parse_args(argv)

    if args.boot_id and (len(args.boot_id) != 8 or
                         any(c not in "0123456789abcdef" for c in args.boot_id)):
        ap.error("--boot-id must be 8 lowercase hex characters")

    setup_logging(args.state_dir)
    settings = Settings(os.path.join(args.config_dir, "api.env"))
    state = State(args.config_dir, args.state_dir, boot_id_hint=args.boot_id)
    log.info("MINCECRAFT uploader %s — device %s, boot_id %s, %s, from line %d",
             FW_VERSION, settings.device, state.boot_id, args.log, state.line)
    up = Uploader(args.log, settings, state, product_names(args.recipes),
                  dry_run=args.dry_run)
    try:
        return up.run(once=args.once)
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
