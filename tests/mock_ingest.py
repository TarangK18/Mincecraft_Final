"""A local stand-in for the DOKI /ingest endpoint, for tests.

It follows the contract the uploader was written against:
  - X-API-Key must match, else 401.
  - The body must be a JSON array, else 400.
  - More than `max_per_request` messages, or a body larger than `max_bytes`,
    gives 413.
  - Each message is stored, a duplicate (same device + batch_id), or rejected
    with an "error". 200 if nothing was rejected, 207 if some were, 422 if
    all were.
  - `fail_next` queues forced statuses (500, 401, 400 ...) for the next
    requests, and `delay_s` makes it slow (for timeouts).

The real server's response body may be laid out differently; the uploader
only relies on the status code and on per-item "status"/"error" fields.
"""

import json
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HEX8 = re.compile(r"^[0-9a-f]{8}$")


class MockIngest:
    def __init__(self, key="test-write-key", devices=("weigh-1",),
                 max_per_request=50, max_bytes=10_000_000):
        self.key = key
        self.devices = set(devices)
        self.max_per_request = max_per_request
        self.max_bytes = max_bytes
        self.rows = {}              # (device, batch_id) -> message
        self.requests = []          # (status, n_messages)
        self.fail_next = []
        self.too_large_ids = set()  # any request containing these -> 413
        self.delay_s = 0
        self.lock = threading.Lock()
        mock = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _send(self, status, obj):
                body = json.dumps(obj).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_POST(self):
                n = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(n)
                if mock.delay_s:
                    import time
                    time.sleep(mock.delay_s)
                status, obj, count = mock.handle(self.path, self.headers, raw)
                with mock.lock:
                    mock.requests.append((status, count))
                self._send(status, obj)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.server.server_address[1]
        self.url = f"http://127.0.0.1:{self.port}/ingest"
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *a):
        self.server.shutdown()
        self.server.server_close()

    # -------------------------------------------------------------- rules
    def validate(self, m):
        if not isinstance(m, dict):
            return "message is not an object"
        if m.get("device") not in self.devices:
            return f"unknown device {m.get('device')!r}"
        if not isinstance(m.get("boot_id"), str) or not HEX8.match(m["boot_id"]):
            return "boot_id must be 8 lowercase hex"
        if not isinstance(m.get("seq"), int) or m["seq"] < 0:
            return "seq must be a non-negative integer"
        if m.get("type") != "batch":
            return "type must be 'batch'"
        d = m.get("data")
        if not isinstance(d, dict) or not d.get("batch_id"):
            return "data.batch_id missing"
        if d.get("batch_type") not in ("per_meat", "fixed"):
            return f"bad batch_type {d.get('batch_type')!r}"
        if d.get("base_source") not in ("scale", "typed", None):
            return f"bad base_source {d.get('base_source')!r}"
        if d.get("meat_type") not in ("buffalo", "chicken", "other", None):
            return f"bad meat_type {d.get('meat_type')!r}"
        for k in ("started_at", "finished_at"):
            v = d.get(k)
            if v is not None and not (isinstance(v, str) and v.endswith("Z")):
                return f"{k} must be UTC with Z or null"
        if not isinstance(d.get("steps"), list):
            return "steps must be a list"
        # As the real server answered in the live bench test (2026-10-21).
        for i, st in enumerate(d["steps"]):
            for k in ("target_g", "actual_g"):
                v = st.get(k) if isinstance(st, dict) else None
                if isinstance(v, bool) or not isinstance(v, (int, float)):
                    return f"steps[{i}].{k} must be a number"
        return None

    def handle(self, path, headers, raw):
        with self.lock:
            forced = self.fail_next.pop(0) if self.fail_next else None
        if forced:
            return forced, {"error": f"forced {forced}"}, 0
        if path != "/ingest":
            return 404, {"error": "not found"}, 0
        if headers.get("X-API-Key") != self.key:
            return 401, {"error": "bad key"}, 0
        if headers.get("Content-Type", "").split(";")[0] != "application/json":
            return 400, {"error": "content type"}, 0
        if len(raw) > self.max_bytes:
            return 413, {"error": "too large"}, 0
        try:
            msgs = json.loads(raw)
        except ValueError:
            return 400, {"error": "not JSON"}, 0
        if not isinstance(msgs, list):
            return 400, {"error": "body must be an array"}, 0
        if len(msgs) > self.max_per_request:
            return 413, {"error": "too many"}, len(msgs)
        ids = {(m.get("data") or {}).get("batch_id") for m in msgs if isinstance(m, dict)}
        if ids & self.too_large_ids:
            return 413, {"error": "too large"}, len(msgs)
        results = []
        with self.lock:
            for m in msgs:
                err = self.validate(m)
                bid = (m.get("data") or {}).get("batch_id") if isinstance(m, dict) else None
                seq = m.get("seq") if isinstance(m, dict) else None
                if err:
                    results.append({"seq": seq, "batch_id": bid,
                                    "status": "rejected", "error": err})
                    continue
                k = (m["device"], bid)
                if k in self.rows:
                    results.append({"seq": seq, "batch_id": bid, "status": "duplicate"})
                else:
                    self.rows[k] = m
                    results.append({"seq": seq, "batch_id": bid, "status": "stored"})
        rejected = sum(r["status"] == "rejected" for r in results)
        status = 200 if rejected == 0 else (422 if rejected == len(results) else 207)
        return status, {"results": results}, len(msgs)
