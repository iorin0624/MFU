#!/bin/sh
set -eu

# Gunicorn's LAN listener exists only for the reverse proxy on 192.168.103.15.
# Direct LAN access would bypass Apache's trusted X-Forwarded-For rewrite and
# its TLS/security policy, so reject every other source on TCP/8080.
iptables -C INPUT -i lo -p tcp --dport 8080 -j ACCEPT 2>/dev/null || \
  iptables -I INPUT 1 -i lo -p tcp --dport 8080 -j ACCEPT
iptables -C INPUT -s 192.168.103.15/32 -p tcp --dport 8080 -j ACCEPT 2>/dev/null || \
  iptables -I INPUT 1 -s 192.168.103.15/32 -p tcp --dport 8080 -j ACCEPT
iptables -C INPUT -p tcp --dport 8080 -j REJECT --reject-with tcp-reset 2>/dev/null || \
  iptables -A INPUT -p tcp --dport 8080 -j REJECT --reject-with tcp-reset

# Dedicated desktop uploader API. Only the reserved main-PC address may use
# this listener; every other LAN or WAN source is rejected.
iptables -C INPUT -s 192.168.103.43/32 -p tcp --dport 8081 -j ACCEPT 2>/dev/null || \
  iptables -I INPUT 1 -s 192.168.103.43/32 -p tcp --dport 8081 -j ACCEPT
iptables -C INPUT -p tcp --dport 8081 -j REJECT --reject-with tcp-reset 2>/dev/null || \
  iptables -A INPUT -p tcp --dport 8081 -j REJECT --reject-with tcp-reset
