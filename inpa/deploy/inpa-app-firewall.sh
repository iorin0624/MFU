#!/bin/sh
set -eu

# The public listener is reachable only from the Apache reverse proxy on
# server-103-15. The Internal Admin API has no TCP listener.
iptables -C INPUT -i lo -p tcp --dport 8090 -j ACCEPT 2>/dev/null || \
  iptables -I INPUT 1 -i lo -p tcp --dport 8090 -j ACCEPT
iptables -C INPUT -s 192.168.103.15/32 -p tcp --dport 8090 -j ACCEPT 2>/dev/null || \
  iptables -I INPUT 1 -s 192.168.103.15/32 -p tcp --dport 8090 -j ACCEPT
iptables -C INPUT -p tcp --dport 8090 -j REJECT --reject-with tcp-reset 2>/dev/null || \
  iptables -A INPUT -p tcp --dport 8090 -j REJECT --reject-with tcp-reset
