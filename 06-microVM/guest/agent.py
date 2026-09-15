#!/usr/bin/env python3
"""An intentionally small, local-only guest application, also used as readiness probe."""
import hashlib
import json
import os
from pathlib import Path
import random
import time
from http.server import BaseHTTPRequestHandler, HTTPServer


class Application:
    def __init__(self, config, state_dir):
        self.state_dir = Path(state_dir)
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.boot_token = os.urandom(16).hex()
        self.initialized_at = time.time()
        self.instance_id = None
        self.counter = 0
        # Touch every byte: a visible, tunable initialization workload.
        self.payload = bytearray(b"x" * (config["warmup_mib"] * 1024 * 1024))
        self.payload_sha256 = hashlib.sha256(self.payload).hexdigest()
        time.sleep(config["warmup_seconds"])
        self.init_duration_ms = (time.time() - self.initialized_at) * 1000

    def health(self):
        marker = self.state_dir / "marker"
        return {
            "ready": True, "boot_token": self.boot_token,
            "initialized_at": self.initialized_at,
            "init_duration_ms": self.init_duration_ms,
            "instance_id": self.instance_id, "counter": self.counter,
            "disk_marker": marker.read_text() if marker.exists() else "",
            "payload_sha256": self.payload_sha256, "pid": os.getpid(),
        }


def handler_for(app):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.0"

        def log_message(self, fmt, *args):
            print(fmt % args, flush=True)

        def reply(self, data, status=200):
            body = json.dumps(data).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            if self.path == "/health":
                self.reply(app.health())
            else:
                self.reply({"error": "not found"}, 404)

        def do_POST(self):
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if not 0 <= length <= 8192:
                    raise ValueError("invalid request size")
                data = json.loads(self.rfile.read(length) or b"{}")
                if self.path == "/init":
                    # Host assigns per-instance identity AFTER restore; boot_token is
                    # intentionally retained as evidence that initialization was skipped.
                    app.instance_id = str(data["instance_id"])
                    random.seed(bytes.fromhex(data["seed"]))
                    time.clock_settime(time.CLOCK_REALTIME, float(data["host_time"]))
                    self.reply(app.health())
                elif self.path == "/mutate":
                    app.counter += 1
                    (app.state_dir / "marker").write_text(str(data["marker"]))
                    os.sync()
                    self.reply(app.health())
                elif self.path == "/prepare-snapshot":
                    # No background writers in this demo. Flush before pausing the VM.
                    os.sync()
                    self.reply(app.health())
                else:
                    self.reply({"error": "not found"}, 404)
            except (ValueError, KeyError, OSError) as exc:
                self.reply({"error": str(exc)}, 400)
    return Handler


if __name__ == "__main__":
    cfg = json.loads(Path("/etc/microvm-lab.json").read_text())
    application = Application(cfg, "/var/lib/microvm")
    print("MICROVM_APPLICATION_READY " + json.dumps(application.health()), flush=True)
    HTTPServer(("0.0.0.0", 8000), handler_for(application)).serve_forever()
