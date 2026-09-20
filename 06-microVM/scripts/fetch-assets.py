#!/usr/bin/env python3
"""Download the pinned official aarch64 binary and a compatible CI guest kernel."""
import argparse
import hashlib
import json
from pathlib import Path
import re
import tarfile
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
import sys

VERSION = "v1.12.1"
BUCKET = "https://s3.amazonaws.com/spec.ccfc.min"


def download(url, path):
    print(f"Downloading {url}", flush=True)
    tmp = path.with_suffix(path.suffix + ".part")
    try:
        with urllib.request.urlopen(url, timeout=120) as response, tmp.open("wb") as out:
            while block := response.read(1024 * 1024):
                out.write(block)
        tmp.replace(path)
    finally:
        tmp.unlink(missing_ok=True)


def digest(path):
    sha = hashlib.sha256()
    with path.open("rb") as f:
        while block := f.read(1024 * 1024):
            sha.update(block)
    return sha.hexdigest()


def kernel_url():
    prefix = "firecracker-ci/v1.12/aarch64/vmlinux-6.1."
    keys, token = [], None
    while True:
        query = {"list-type": "2", "prefix": prefix}
        if token:
            query["continuation-token"] = token
        with urllib.request.urlopen(BUCKET + "?" + urllib.parse.urlencode(query), timeout=60) as response:
            root = ET.fromstring(response.read())
        keys.extend(e.text for e in root.findall("{*}Contents/{*}Key")
                    if re.fullmatch(re.escape(prefix) + r"\d+", e.text or ""))
        token = root.findtext("{*}NextContinuationToken")
        if not token:
            break
    if not keys:
        raise RuntimeError("No 6.1 ARM kernel in official CI bucket; pass --kernel-url or provide assets/Image manually. See README.")
    key = max(keys, key=lambda value: int(value.rsplit(".", 1)[1]))
    return BUCKET + "/" + key

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, default=Path(__file__).resolve().parents[1] / "artifacts/assets")
    parser.add_argument("--kernel-url", help="Official/custom aarch64 uncompressed Image URL")
    parser.add_argument("--kernel-sha256")
    parser.add_argument("--archive-sha256")
    args = parser.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)
    if any((args.out / name).exists() for name in ("firecracker", "Image", "assets.json")):
        parser.error("assets already exist; use a fresh --out directory to avoid replacing pinned inputs")
    url = f"https://github.com/firecracker-microvm/firecracker/releases/download/{VERSION}/firecracker-{VERSION}-aarch64.tgz"
    archive = args.out / "firecracker.tgz"
    download(url, archive)
    if args.archive_sha256 and digest(archive) != args.archive_sha256.lower():
        raise RuntimeError("Firecracker archive SHA256 mismatch")
    with tarfile.open(archive) as tar:
        name = f"release-{VERSION}-aarch64/firecracker-{VERSION}-aarch64"
        member = tar.getmember(name)
        if not member.isfile():
            raise RuntimeError("Unexpected archive member")
        with tar.extractfile(member) as src, (args.out / "firecracker").open("wb") as dst:
            dst.write(src.read())
    (args.out / "firecracker").chmod(0o755)
    kurl = args.kernel_url or kernel_url()
    download(kurl, args.out / "Image")
    if args.kernel_sha256 and digest(args.out / "Image") != args.kernel_sha256.lower():
        raise RuntimeError("Guest kernel SHA256 mismatch")
    with (args.out / "Image").open("rb") as f:
        f.seek(56)
        if f.read(4) != b"ARM\x64":
            raise RuntimeError("Guest kernel lacks the ARM64 Image header; do not use x86 vmlinux or a compressed image")
    manifest = {"firecracker_version": VERSION, "archive_url": url, "kernel_url": kurl,
                "sha256": {name: digest(args.out / name) for name in ("firecracker.tgz", "firecracker", "Image")}}
    (args.out / "assets.json").write_text(json.dumps(manifest, indent=2) + "\n")
    print(json.dumps(manifest, indent=2))


if __name__ == "__main__":
    main()
