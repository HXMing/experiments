#!/bin/sh
# Some kernels mount devtmpfs before executing init. Preserve existing mounts;
# actual mount failures must still stop initialization rather than being hidden.
mount_if_needed() {
    mount_target=$1
    shift
    if ! mountpoint -q "$mount_target"; then
        mount "$@" "$mount_target"
    fi
}

mount_runtime_filesystems() {
    mount_if_needed /proc -t proc proc
    mount_if_needed /sys -t sysfs sysfs
    mount_if_needed /dev -t devtmpfs devtmpfs
    mkdir -p /dev/pts /run /tmp
    mount_if_needed /dev/pts -t devpts devpts
    mount_if_needed /run -t tmpfs -o mode=755 tmpfs
    mount_if_needed /tmp -t tmpfs -o mode=1777 tmpfs
}
