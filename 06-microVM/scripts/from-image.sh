#!/usr/bin/env bash
# Called by lab.py; builds an OCI image, exports it and populates an ext4 image.
set -euo pipefail
image=$1
out=$2
size_mib=$3
source_dir=$(cd -- "$(dirname -- "$0")/.." && pwd)
work=$(mktemp -d "$out/.image-work.XXXXXX")
container_id=''
image_tag="microvm-lab-build:build-$(basename "$work" | tr '[:upper:]' '[:lower:]')"
cleanup() {
    if [[ -n "$container_id" ]]; then docker rm "$container_id" >/dev/null || true; fi
    docker image rm "$image_tag" >/dev/null 2>&1 || true
    rm -rf -- "$work"
}
trap cleanup EXIT
mkdir -p "$work/context" "$work/rootfs"
cp "$source_dir/guest/Dockerfile" "$source_dir/guest/init.sh" "$source_dir/guest/agent.py" "$work/context/"
cp "$out/config.json" "$work/context/guest-config.json"
docker pull --platform linux/arm64 "$image"
docker image inspect "$image" > "$out/source-image.json"
docker build --platform linux/arm64 --build-arg "BASE_IMAGE=$image" -t "$image_tag" "$work/context"
docker image inspect "$image_tag" > "$out/built-image.json"
container_id=$(docker create --platform linux/arm64 "$image_tag")
docker export -o "$work/rootfs.tar" "$container_id"
# Images are trusted experiment inputs; ownership and executable modes are preserved.
tar --numeric-owner -xpf "$work/rootfs.tar" -C "$work/rootfs"
# Kernel opens /dev/console before init can mount devtmpfs.
mkdir -p "$work/rootfs/dev"
[[ -e "$work/rootfs/dev/console" ]] || mknod -m 600 "$work/rootfs/dev/console" c 5 1
[[ -e "$work/rootfs/dev/null" ]] || mknod -m 666 "$work/rootfs/dev/null" c 1 3
truncate -s "${size_mib}M" "$out/rootfs.ext4"
# Disable recent optional ext4 features to support the pinned 6.1 guest kernel.
mkfs.ext4 -q -F -O '^metadata_csum_seed,^orphan_file' \
    -E lazy_itable_init=0,lazy_journal_init=0 -d "$work/rootfs" "$out/rootfs.ext4"
e2fsck -fn "$out/rootfs.ext4"
chmod 444 "$out/rootfs.ext4"
