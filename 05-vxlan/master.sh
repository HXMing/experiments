#!/bin/bash

function exc_node1() {
  echo "1.创建vxlan"
  ip link add vxlan100 type vxlan id 100 local 10.211.55.42 remote 10.211.55.43 dev enp0s5 dstport 4790

  echo "2.配置Overlay IP"
  ip addr add 172.30.100.1/24 dev vxlan100

  echo "3.启动"
  ip link set vxlan100 up
  ip link set vxlan100 mtu 1450

  echo "4.查看"
  ip -d link show vxlan100
}

function exc_node2() {
  echo "1.创建vxlan"
  ip link add vxlan100 type vxlan id 100 local 10.211.55.43 remote 10.211.55.42 dev enp0s5 dstport 4790

  echo "2.配置Overlay IP"
  ip addr add 172.30.100.2/24 dev vxlan100

  echo "3.启动"
  ip link set vxlan100 up
  ip link set vxlan100 mtu 1450

  echo "4.查看"
  ip -d link show vxlan100
}

NODE="${NODE:-}"

main() {
  if [[ $NODE == 1 ]]; then
    exc_node1
  elif [[ $NODE == 2 ]]; then
    exc_node2
  else
    echo "error:node idx"
  fi
}

main
