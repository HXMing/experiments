ARG BASE_IMAGE=ubuntu:24.04
FROM ${BASE_IMAGE}
USER root
ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update && apt-get install -y --no-install-recommends \
    python3 iproute2 util-linux ca-certificates \
    && rm -rf /var/lib/apt/lists/*
COPY init.sh /sbin/lab-init
COPY mounts.sh /opt/microvm/mounts.sh
COPY agent.py /opt/microvm/agent.py
COPY guest-config.json /etc/microvm-lab.json
RUN chmod 755 /sbin/lab-init && mkdir -p /var/lib/microvm /run /tmp /proc /sys /dev/pts
ENTRYPOINT ["/sbin/lab-init"]
