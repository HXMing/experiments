"""Portable checks; hardware acceptance is performed by lab.py verify on ARM/KVM."""
import importlib.util
import json
from pathlib import Path
import socket
import tempfile
import threading
import unittest
from http.server import HTTPServer
from unittest.mock import patch
import urllib.request

ROOT = Path(__file__).resolve().parents[1]


def module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


lab = module("lab", ROOT / "lab.py")
agent = module("agent", ROOT / "guest/agent.py")


class ConfigTests(unittest.TestCase):
    def test_example_valid(self):
        cfg = lab.validate_config(lab.read_json(ROOT / "template.example.json"))
        self.assertEqual(cfg["name"], "python-demo")

    def test_no_traversal_or_shell_option_names(self):
        for name in ("../other", "-option", "x/y", "", "a" * 33):
            with self.assertRaises(ValueError):
                lab.valid_name(name)

    def test_memory_budget_prevents_guest_oom(self):
        cfg = lab.read_json(ROOT / "template.example.json")
        cfg["warmup_mib"] = cfg["memory_mib"]
        with self.assertRaises(ValueError):
            lab.validate_config(cfg)

    def test_nan_is_not_a_duration(self):
        cfg = lab.read_json(ROOT / "template.example.json")
        cfg["warmup_seconds"] = float("nan")
        with self.assertRaises(ValueError):
            lab.validate_config(cfg)


class UnixAPITests(unittest.TestCase):
    def respond_once(self, folder, status, body, observed):
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        listener.bind(str(folder / "api.sock"))
        listener.listen(1)

        def worker():
            try:
                with listener.accept()[0] as conn:
                    data = b""
                    while b"\r\n\r\n" not in data:
                        data += conn.recv(65536)
                    headers, payload = data.split(b"\r\n\r\n", 1)
                    lengths = [int(line.split(b":", 1)[1]) for line in headers.split(b"\r\n")
                               if line.lower().startswith(b"content-length:")]
                    while len(payload) < (lengths[0] if lengths else 0):
                        payload += conn.recv(65536)
                    observed.append((headers, payload))
                    raw = json.dumps(body).encode()
                    conn.sendall(f"HTTP/1.1 {status} Test\r\nContent-Length: {len(raw)}\r\nConnection: close\r\n\r\n".encode() + raw)
            finally:
                listener.close()

        thread = threading.Thread(target=worker, daemon=True)
        thread.start()
        return thread

    def test_unix_socket_roundtrip_with_json(self):
        with tempfile.TemporaryDirectory(dir="/tmp") as directory:
            folder, observed = Path(directory), []
            thread = self.respond_once(folder, 200, {"state": "Running"}, observed)
            res = lab.api(folder, "PUT", "/snapshot/load", {"resume_vm": False})
            thread.join(3)
            self.assertEqual(res, {"state": "Running"})
            self.assertEqual(json.loads(observed[0][1]), {"resume_vm": False})

    def test_api_failure_is_not_silently_accepted(self):
        with tempfile.TemporaryDirectory(dir="/tmp") as directory:
            folder = Path(directory)
            thread = self.respond_once(folder, 400, {"fault_message": "invalid snapshot"}, [])
            with self.assertRaisesRegex(RuntimeError, "invalid snapshot"):
                lab.api(folder, "PUT", "/snapshot/load", {})
            thread.join(3)


class GuestTests(unittest.TestCase):
    def test_http_mutation_and_state_isolation(self):
        with tempfile.TemporaryDirectory() as directory:
            app_a = agent.Application({"warmup_mib": 0, "warmup_seconds": 0}, Path(directory) / "a")
            app_b = agent.Application({"warmup_mib": 0, "warmup_seconds": 0}, Path(directory) / "b")
            server = HTTPServer(("127.0.0.1", 0), agent.handler_for(app_a))
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                request = urllib.request.Request(f"http://127.0.0.1:{server.server_port}/mutate",
                                                 data=b'{"marker":"only-a"}',
                                                 headers={"Content-Type": "application/json"})
                with patch.object(agent.os, "sync", create=True), urllib.request.urlopen(request) as response:
                    data = json.load(response)
                self.assertEqual(data["counter"], 1)
                self.assertEqual(data["disk_marker"], "only-a")
                self.assertEqual(app_b.health()["counter"], 0)
                self.assertEqual(app_b.health()["disk_marker"], "")
            finally:
                server.shutdown()
                thread.join(3)
                server.server_close()


class CleanupTests(unittest.TestCase):
    def test_stale_pid_never_kills_unrelated_process(self):
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            lab.write_json(folder / "instance.json", {"pid": 123, "proc_start": "old",
                           "namespace": "mvl-test", "network_created": False})
            with patch.object(lab, "proc_start", return_value="new"), patch.object(lab.os, "kill") as kill:
                lab.stop_vm(folder)
                kill.assert_not_called()
            self.assertEqual(lab.read_json(folder / "instance.json")["status"], "stopped")

    def test_failed_namespace_creation_does_not_delete_foreign_namespace(self):
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            lab.write_json(folder / "instance.json", {"namespace": "mvl-existing", "network_created": False})
            with patch.object(lab.subprocess, "run") as run:
                lab.stop_vm(folder)
                run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
