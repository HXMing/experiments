#!/bin/sh
set -eu
export PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
mount -t proc proc /proc
mount -t sysfs sysfs /sys
mount -t devtmpfs devtmpfs /dev
mkdir -p /dev/pts /run /tmp
mount -t devpts devpts /dev/pts
mount -t tmpfs -o mode=755 tmpfs /run
mount -t tmpfs -o mode=1777 tmpfs /tmp
exec </dev/console >/dev/console 2>&1
ip link set lo up
ip link set eth0 up
ip addr add 172.30.0.2/30 dev eth0
# Static neighbor removes stale ARP state as a variable in snapshot clones.
ip neigh replace 172.30.0.1 lladdr 02:fc:00:00:00:01 dev eth0 nud permanent
exec python3 -u /opt/microvm/agent.py
