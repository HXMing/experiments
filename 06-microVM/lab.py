#!/usr/bin/env python3
"""Single-host aarch64 Firecracker lab. Run --help for the command sequence."""
import argparse
from concurrent.futures import ThreadPoolExecutor
import fcntl
import hashlib
import http.client
import json
import math
import os
from pathlib import Path
import platform
import re
import secrets
import shutil
import signal
import socket
import subprocess
import sys
import time
import uuid

ROOT = Path(__file__).resolve().parent
STATE = ROOT / "artifacts"
GUEST_URL = "http://172.30.0.2:8000"
VERSION = "Firecracker v1.12.1"


def run(argv, **kwargs):
    return subprocess.run([str(x) for x in argv], check=True, **kwargs)


def write_json(path, value):
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n")
    tmp.replace(path)


def read_json(path):
    return json.loads(path.read_text())


def sha256(path):
    h = hashlib.sha256()
    with path.open("rb") as f:
        while block := f.read(1024 * 1024):
            h.update(block)
    return h.hexdigest()


def valid_name(value):
    if not isinstance(value, str) or not re.fullmatch(r"[a-z][a-z0-9-]{0,31}", value):
        raise ValueError("name must match [a-z][a-z0-9-]{0,31}")
    return value


def validate_config(cfg):
    valid_name(cfg["name"])
    if not isinstance(cfg["from_image"], str) or not cfg["from_image"] or cfg["from_image"].startswith("-"):
        raise ValueError("from_image must be an OCI image reference")
    for key, low, high in (("vcpus", 1, 32), ("memory_mib", 256, 65536),
                           ("rootfs_mib", 1024, 65536), ("warmup_mib", 0, 32768)):
        if type(cfg[key]) is not int or not low <= cfg[key] <= high:
            raise ValueError(f"{key} must be an integer in [{low}, {high}]")
    seconds = cfg["warmup_seconds"]
    if type(seconds) not in (int, float) or not math.isfinite(seconds) or not 0 <= seconds <= 60:
        raise ValueError("warmup_seconds must be in [0, 60]")
    # Payload construction temporarily needs roughly twice the configured size.
    if cfg["warmup_mib"] * 2 + 128 > cfg["memory_mib"]:
        raise ValueError("memory_mib must leave room for 2 * warmup_mib + 128 MiB")
    return cfg


class UnixHTTP(http.client.HTTPConnection):
    def __init__(self, path, timeout=120):
        super().__init__("localhost", timeout=timeout)
        self.path = str(path)

    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout)
        self.sock.connect(self.path)


def api(vm_dir, method, path, payload=None):
    conn = UnixHTTP(vm_dir / "api.sock")
    try:
        body = json.dumps(payload) if payload is not None else None
        conn.request(method, path, body, {"Content-Type": "application/json"})
        res = conn.getresponse()
        raw = res.read()
        if not 200 <= res.status < 300:
            raise RuntimeError(f"Firecracker {method} {path}: HTTP {res.status}: {raw.decode()}")
        return json.loads(raw) if raw else None
    finally:
        conn.close()


def guest(ns, path="/health", body=None, timeout=2):
    cmd = ["ip", "netns", "exec", ns, "curl", "--noproxy", "*", "-fsS",
           "--connect-timeout", "0.5", "--max-time", str(timeout), "-H", "Connection: close"]
    if body is not None:
        cmd += ["-H", "Content-Type: application/json", "-d", json.dumps(body)]
    cmd += [GUEST_URL + path]
    return json.loads(run(cmd, capture_output=True, text=True, timeout=timeout + 2).stdout)


def proc_start(pid):
    try:
        return Path(f"/proc/{pid}/stat").read_text().rsplit(") ", 1)[1].split()[19]
    except (FileNotFoundError, ProcessLookupError):
        return None


def alive(meta):
    pid = meta.get("pid")
    if not pid or not meta.get("proc_start") or proc_start(pid) != meta["proc_start"]:
        return False
    try:
        return Path(f"/proc/{pid}/stat").read_text().rsplit(") ", 1)[1].split()[0] != "Z"
    except FileNotFoundError:
        return False


def clone_disk(source, target):
    # Never hardlink a writable disk. Try reflink, then an explicit full/sparse copy.
    result = subprocess.run(["cp", "--reflink=always", "--sparse=always", str(source), str(target)],
                            capture_output=True, text=True)
    mode = "reflink"
    if result.returncode:
        target.unlink(missing_ok=True)
        run(["cp", "--reflink=never", "--sparse=always", source, target])
        mode = "copy"
    target.chmod(0o600)
    if source.stat().st_ino == target.stat().st_ino and source.stat().st_dev == target.stat().st_dev:
        raise RuntimeError("Writable rootfs must have its own inode")
    return mode


def stop_vm(vm_dir):
    meta_path = vm_dir / "instance.json"
    if not meta_path.exists():
        return
    meta = read_json(meta_path)
    if alive(meta):
        os.kill(meta["pid"], signal.SIGTERM)
        deadline = time.monotonic() + 3
        while alive(meta) and time.monotonic() < deadline:
            time.sleep(0.05)
        if alive(meta):
            os.kill(meta["pid"], signal.SIGKILL)
            deadline = time.monotonic() + 3
            while alive(meta) and time.monotonic() < deadline:
                time.sleep(0.05)
        if alive(meta):
            raise RuntimeError(f"Process {meta['pid']} did not exit; resources retained")
    # Only delete the namespace created and recorded by this lab invocation.
    if meta.get("network_created"):
        result = subprocess.run(["ip", "netns", "del", meta["namespace"]], capture_output=True, text=True)
        if result.returncode and "No such file" not in result.stderr:
            raise RuntimeError(result.stderr)
        meta["network_created"] = False
    meta["status"] = "stopped"
    write_json(meta_path, meta)


def doctor():
    if platform.system() != "Linux" or platform.machine() != "aarch64":
        raise RuntimeError("Execution requires the user's aarch64 Linux KVM server; this machine is not supported")
    if os.geteuid() != 0:
        raise RuntimeError("Run with sudo: the lab creates network namespaces and TAP interfaces")
    for cmd in ("ip", "curl", "cp", "docker", "tar", "truncate", "mkfs.ext4", "e2fsck"):
        if not shutil.which(cmd):
            raise RuntimeError(f"Missing dependency: {cmd}; see README")
    if not Path("/dev/net/tun").exists():
        raise RuntimeError("Missing /dev/net/tun; load the tun module")
    with open("/dev/kvm", "rb+", buffering=0) as kvm:
        if fcntl.ioctl(kvm, 0xAE00, 0) != 12:  # KVM_GET_API_VERSION
            raise RuntimeError("Unsupported KVM API")
        vmfd = fcntl.ioctl(kvm, 0xAE01, 0)  # KVM_CREATE_VM
        os.close(vmfd)
    binary = STATE / "assets/firecracker"
    if not binary.exists() or not (STATE / "assets/Image").exists():
        raise RuntimeError("Missing assets; run python3 scripts/fetch-assets.py first")
    actual = run([binary, "--version"], capture_output=True, text=True).stdout.strip()
    if actual != VERSION:
        raise RuntimeError(f"This lab pins {VERSION}; found {actual}")
    # Probe Docker without modifying the daemon or installing anything.
    run(["docker", "info"], stdout=subprocess.DEVNULL)
    return {"host": platform.platform(), "architecture": platform.machine(), "kvm_api": 12,
            "firecracker": actual, "page_size": os.sysconf("SC_PAGE_SIZE")}


def load_template(name):
    path = STATE / "templates" / valid_name(name)
    manifest = read_json(path / "template.json")
    if manifest["host_machine_id"] != Path("/etc/machine-id").read_text().strip():
        raise RuntimeError("This lab restricts restore to the same physical host used to build the template")
    return path, manifest


def launch(template, cfg, vm_id, mode, initialize=True, record=True):
    valid_name(vm_id)
    vm_dir = STATE / "instances" / vm_id
    if len(str(vm_dir / "api.sock").encode()) >= 104:
        raise RuntimeError("Unix socket path too long; move microVM to a shorter path, e.g. /opt/microVM")
    t0 = time.perf_counter()
    vm_dir.mkdir(parents=True, exist_ok=False)
    ns = "mvl-" + uuid.uuid4().hex[:12]
    meta = {"id": vm_id, "namespace": ns, "template": template.name, "mode": mode,
            "status": "starting", "network_created": False}
    write_json(vm_dir / "instance.json", meta)
    process = None
    timings = {}
    try:
        source = template / ("snapshot/rootfs.ext4" if mode == "warm" else "rootfs.ext4")
        timings["disk_mode"] = clone_disk(source, vm_dir / "rootfs.ext4")
        t1 = time.perf_counter()
        timings["disk_prepare_ms"] = (t1 - t0) * 1000
        # Record immediately after creation so a subsequent TAP failure is recoverable.
        run(["ip", "netns", "add", ns])
        meta["network_created"] = True
        write_json(vm_dir / "instance.json", meta)
        setup_network(ns)
        t2 = time.perf_counter()
        timings["network_prepare_ms"] = (t2 - t1) * 1000
        with (vm_dir / "console.log").open("wb") as log:
            process = subprocess.Popen(["ip", "netns", "exec", ns, str(template / "firecracker"),
                                        "--api-sock", "api.sock"], cwd=vm_dir,
                                       stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                                       start_new_session=True)
        meta.update(pid=process.pid, proc_start=proc_start(process.pid))
        write_json(vm_dir / "instance.json", meta)
        deadline = time.monotonic() + 10
        while True:
            if process.poll() is not None:
                raise RuntimeError(f"Firecracker exited; inspect {vm_dir / 'console.log'}")
            try:
                api(vm_dir, "GET", "/")
                break
            except (OSError, http.client.HTTPException):
                if time.monotonic() > deadline:
                    raise RuntimeError("Timed out waiting for Firecracker API")
                time.sleep(0.01)
        t3 = time.perf_counter()
        timings["api_ready_ms"] = (t3 - t2) * 1000
        if mode == "warm":
            api(vm_dir, "PUT", "/snapshot/load", {
                "snapshot_path": str(template / "snapshot/vmstate"),
                "mem_backend": {"backend_type": "File", "backend_path": str(template / "snapshot/memory")},
                "enable_diff_snapshots": False, "resume_vm": False})
            api(vm_dir, "PATCH", "/vm", {"state": "Resumed"})
        else:
            api(vm_dir, "PUT", "/machine-config", {"vcpu_count": cfg["vcpus"], "mem_size_mib": cfg["memory_mib"],
                                                   "smt": False, "track_dirty_pages": False})
            api(vm_dir, "PUT", "/boot-source", {
                "kernel_image_path": str(template / "Image"),
                "boot_args": "console=ttyS0 keep_bootcon reboot=k panic=1 pci=off root=/dev/vda rw rootwait init=/sbin/lab-init"})
            # Relative path is intentional: a restored VMM resolves this within its own cwd.
            api(vm_dir, "PUT", "/drives/rootfs", {"drive_id": "rootfs", "path_on_host": "rootfs.ext4",
                                                 "is_root_device": True, "is_read_only": False})
            api(vm_dir, "PUT", "/network-interfaces/eth0", {"iface_id": "eth0", "host_dev_name": "tap0",
                                                           "guest_mac": "02:fc:00:00:00:02"})
            api(vm_dir, "PUT", "/entropy", {})
            api(vm_dir, "PUT", "/actions", {"action_type": "InstanceStart"})
        t4 = time.perf_counter()
        timings["boot_or_restore_api_ms"] = (t4 - t3) * 1000
        deadline = time.monotonic() + cfg["warmup_seconds"] + 90
        last_error = ""
        while True:
            if process.poll() is not None:
                raise RuntimeError(f"Firecracker exited; inspect {vm_dir / 'console.log'}")
            try:
                health = guest(ns)
                if health.get("ready"):
                    break
            except (subprocess.SubprocessError, ValueError) as exc:
                last_error = str(exc)
            if time.monotonic() > deadline:
                raise RuntimeError(f"Guest readiness timeout: {last_error}; inspect {vm_dir / 'console.log'}")
            time.sleep(0.02)
        t5 = time.perf_counter()
        timings["guest_ready_ms"] = (t5 - t4) * 1000
        if initialize:
            health = guest(ns, "/init", {"instance_id": vm_id, "seed": secrets.token_hex(32), "host_time": time.time()})
        t6 = time.perf_counter()
        timings.update(instance_init_ms=(t6 - t5) * 1000, total_ms=(t6 - t0) * 1000,
                       vmm_to_ready_ms=(t5 - t2) * 1000)
        meta.update(status="running", health=health, timings=timings)
        write_json(vm_dir / "instance.json", meta)
        if record:
            print(json.dumps({"id": vm_id, "namespace": ns, "mode": mode, "timings": timings}, ensure_ascii=False), flush=True)
        return vm_dir, meta
    except BaseException:
        if process and process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=3)
        stop_vm(vm_dir)
        raise


def setup_network(ns):
    prefix = ["ip", "netns", "exec", ns, "ip"]
    for args in (["link", "set", "lo", "up"], ["tuntap", "add", "dev", "tap0", "mode", "tap"],
                 ["link", "set", "tap0", "address", "02:fc:00:00:00:01"],
                 ["addr", "add", "172.30.0.1/30", "dev", "tap0"], ["link", "set", "tap0", "up"],
                 ["neigh", "replace", "172.30.0.2", "lladdr", "02:fc:00:00:00:02", "dev", "tap0", "nud", "permanent"]):
        run(prefix + args)


def build(config_path):
    host = doctor()
    cfg = validate_config(read_json(config_path))
    template = STATE / "templates" / cfg["name"]
    template.mkdir(parents=True, exist_ok=False)
    write_json(template / "config.json", cfg)
    for name in ("firecracker", "Image"):
        shutil.copy2(STATE / "assets" / name, template / name)
    # Keep image build output for diagnosing package/export/filesystem failures.
    with (template / "from-image.log").open("w") as log:
        print(f"Building {cfg['from_image']}; log: {log.name}", flush=True)
        run(["bash", ROOT / "scripts/from-image.sh", cfg["from_image"], template, cfg["rootfs_mib"]],
            stdout=log, stderr=subprocess.STDOUT)
    vm_dir, meta = launch(template, cfg, "builder-" + uuid.uuid4().hex[:10], "cold", initialize=False)
    snapshot = template / "snapshot"
    snapshot.mkdir()
    try:
        health = guest(meta["namespace"], "/prepare-snapshot", {}, timeout=30)
        api(vm_dir, "PATCH", "/vm", {"state": "Paused"})
        api(vm_dir, "PUT", "/snapshot/create", {"snapshot_type": "Full",
            "snapshot_path": str(snapshot / "vmstate"), "mem_file_path": str(snapshot / "memory")})
        # Keep the source paused until it has terminated; disk and memory must agree.
        stop_vm(vm_dir)
        process_disk_mode = clone_disk(vm_dir / "rootfs.ext4", snapshot / "rootfs.ext4")
        for f in snapshot.iterdir():
            f.chmod(0o444)
        files = ["firecracker", "Image", "rootfs.ext4", "snapshot/rootfs.ext4", "snapshot/vmstate", "snapshot/memory"]
        manifest = {"schema": 1, "config": cfg, "host": host,
                    "host_machine_id": Path("/etc/machine-id").read_text().strip(),
                    "created_at": time.time(), "snapshot_health": health,
                    "snapshot_disk_mode": process_disk_mode,
                    "sha256": {name: sha256(template / name) for name in files}}
        write_json(template / "template.json", manifest)  # Publication marker, written last.
        print(f"Template ready: {template}", flush=True)
    finally:
        stop_vm(vm_dir)


def start(args):
    template, manifest = load_template(args.template)
    if args.count < 1 or args.count > 64 or not 1 <= args.parallel <= 16:
        raise ValueError("count must be 1..64; parallel must be 1..16")
    if args.id and args.count != 1:
        raise ValueError("--id requires --count 1")
    ids = [args.id] if args.id else [f"{args.mode}-{uuid.uuid4().hex[:10]}" for _ in range(args.count)]
    # Wait for all submitted launches. Each failed launch cleans up its own resources.
    errors = []
    batch_start = time.perf_counter()
    with ThreadPoolExecutor(max_workers=args.parallel) as pool:
        futures = [pool.submit(launch, template, manifest["config"], vm_id, args.mode) for vm_id in ids]
        for future in futures:
            try:
                future.result()
            except Exception as exc:
                errors.append(str(exc))
    report = STATE / "reports" / f"launch-{time.time_ns()}.json"
    report.parent.mkdir(exist_ok=True)
    write_json(report, {"ids": ids, "mode": args.mode, "parallel": args.parallel,
                        "batch_wall_ms": (time.perf_counter() - batch_start) * 1000, "errors": errors})
    print(f"Report: {report}")
    if errors:
        raise RuntimeError("Some launches failed; successful VMs remain available. " + "; ".join(errors))


def percentile(values, p):
    ordered = sorted(values)
    return ordered[max(0, math.ceil(len(ordered) * p) - 1)]


def benchmark(args):
    if not 1 <= args.rounds <= 100:
        raise ValueError("rounds must be 1..100")
    template, manifest = load_template(args.template)
    rows = []
    for i in range(args.rounds):
        # Alternate order to reduce systematic cache/order bias. No host drop_caches.
        modes = ("cold", "warm") if i % 2 == 0 else ("warm", "cold")
        for mode in modes:
            vm_dir, meta = launch(template, manifest["config"], f"bench-{uuid.uuid4().hex[:10]}", mode)
            rows.append({"round": i + 1, "mode": mode, **meta["timings"]})
            stop_vm(vm_dir)
    result = {"cache_policy": "uncontrolled OS page cache; no cache eviction", "rows": rows,
              "warmup_seconds": manifest["config"]["warmup_seconds"], "summary": {}}
    for mode in ("cold", "warm"):
        result["summary"][mode] = {}
        for metric in ("total_ms", "vmm_to_ready_ms", "disk_prepare_ms"):
            values = [row[metric] for row in rows if row["mode"] == mode]
            result["summary"][mode][metric] = {"p50": percentile(values, .5), "p95": percentile(values, .95),
                                                "min": min(values), "max": max(values)}
    report = STATE / "reports" / f"benchmark-{time.time_ns()}.json"
    report.parent.mkdir(exist_ok=True)
    write_json(report, result)
    print(json.dumps(result["summary"], indent=2))
    print(f"Report: {report}")


def require(condition, message):
    if not condition:
        raise RuntimeError("Verification failed: " + message)


def verify(args):
    template, manifest = load_template(args.template)
    instances = []
    for path in sorted((STATE / "instances").glob("*/instance.json")):
        meta = read_json(path)
        if meta["template"] == template.name and meta["mode"] == "warm" and alive(meta):
            instances.append((path.parent, meta))
    require(len(instances) >= 2, "start at least two warm instances")
    health = [guest(meta["namespace"]) for _, meta in instances]
    expected = manifest["snapshot_health"]
    require(all(h["boot_token"] == expected["boot_token"] for h in health), "initialization token was not restored")
    require(all(h["initialized_at"] == expected["initialized_at"] for h in health), "application initialization reran")
    require(len({h["instance_id"] for h in health}) == len(health), "instance IDs are not unique")
    a, b = instances[:2]
    before_a, before_b = guest(a[1]["namespace"]), guest(b[1]["namespace"])
    marker = uuid.uuid4().hex
    after_a = guest(a[1]["namespace"], "/mutate", {"marker": marker})
    after_b = guest(b[1]["namespace"])
    require(after_a["counter"] == before_a["counter"] + 1, "memory update failed")
    require(after_a["disk_marker"] == marker, "disk write failed")
    require(after_b == before_b, "another VM observed the memory/disk mutation")
    inodes = {(path.joinpath("rootfs.ext4").stat().st_dev, path.joinpath("rootfs.ext4").stat().st_ino)
              for path, _ in instances}
    require(len(inodes) == len(instances), "writable disk inode is shared")
    probe_dir, probe = launch(template, manifest["config"], "probe-" + uuid.uuid4().hex[:10], "warm")
    try:
        require(probe["health"]["counter"] == expected["counter"], "template memory was modified")
        require(probe["health"]["disk_marker"] == expected["disk_marker"], "template disk was modified")
    finally:
        stop_vm(probe_dir)
    for name in ("snapshot/rootfs.ext4", "snapshot/memory", "snapshot/vmstate"):
        require(sha256(template / name) == manifest["sha256"][name], f"template checksum changed: {name}")
    result = {"passed": True, "instances": [m["id"] for _, m in instances],
              "checks": ["restored initialization state", "unique instance identity", "independent writable disks",
                         "isolated memory mutation", "isolated disk mutation", "fresh clone unchanged", "immutable snapshot checksums"],
              "modified_instance": a[1]["id"], "marker": marker}
    report = STATE / "reports" / f"verify-{time.time_ns()}.json"
    report.parent.mkdir(exist_ok=True)
    write_json(report, result)
    print(json.dumps(result, indent=2))
    print(f"Report: {report}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("doctor", help="check Linux/aarch64/KVM and dependencies")
    b = sub.add_parser("build", help="fromImage -> rootfs -> boot -> snapshot template")
    b.add_argument("config", type=Path)
    s = sub.add_parser("start", help="launch cold or snapshot-restored VMs")
    s.add_argument("template")
    s.add_argument("--mode", choices=("cold", "warm"), default="warm")
    s.add_argument("--count", type=int, default=1)
    s.add_argument("--parallel", type=int, default=1)
    s.add_argument("--id")
    sub.add_parser("list", help="show instances and last recorded launch timings")
    h = sub.add_parser("health", help="query a guest through its network namespace")
    h.add_argument("id")
    v = sub.add_parser("verify", help="check restored state and memory/disk isolation; mutates one VM")
    v.add_argument("template")
    bm = sub.add_parser("bench", help="alternating cold/warm end-to-end benchmark")
    bm.add_argument("template")
    bm.add_argument("--rounds", type=int, default=5)
    stop = sub.add_parser("stop", help="terminate only recorded instances; preserve logs and disks")
    group = stop.add_mutually_exclusive_group(required=True)
    group.add_argument("--id")
    group.add_argument("--all", action="store_true")
    args = parser.parse_args()
    if args.command == "doctor":
        print(json.dumps(doctor(), indent=2))
        return
    if platform.system() != "Linux" or platform.machine() != "aarch64" or os.geteuid() != 0:
        parser.error("Run on the aarch64 Linux server with sudo; --help and local tests work on other hosts")
    STATE.mkdir(parents=True, exist_ok=True)
    os.chmod(STATE, 0o700)
    # One controller at a time; --parallel handles intra-command concurrency safely.
    with (STATE / ".controller.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if args.command == "build":
            build(args.config)
        elif args.command == "start":
            start(args)
        elif args.command == "bench":
            benchmark(args)
        elif args.command == "verify":
            verify(args)
        elif args.command == "health":
            meta = read_json(STATE / "instances" / valid_name(args.id) / "instance.json")
            require(alive(meta), "instance process is not running")
            print(json.dumps(guest(meta["namespace"]), indent=2))
        elif args.command == "list":
            for path in sorted((STATE / "instances").glob("*/instance.json")):
                meta = read_json(path)
                print(json.dumps({"id": meta["id"], "mode": meta["mode"], "alive": alive(meta),
                                  "namespace": meta["namespace"], "timings": meta.get("timings")}))
        elif args.command == "stop":
            dirs = [STATE / "instances" / valid_name(args.id)] if args.id else sorted((STATE / "instances").glob("*"))
            for path in dirs:
                stop_vm(path)


if __name__ == "__main__":
    try:
        main()
    except (Exception, KeyboardInterrupt) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)
