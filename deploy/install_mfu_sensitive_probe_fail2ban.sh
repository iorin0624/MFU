#!/usr/bin/env bash
set -euo pipefail

SOURCE_ROOT="${1:-/tmp/mfu-fail2ban}"
STAMP="$(date +%Y%m%d_%H%M%S)"
BACKUP_DIR="/root/mfu-backups/fail2ban-mfu-$STAMP"

install -d -o root -g root -m 0700 "$BACKUP_DIR"
for target in \
  /etc/fail2ban/filter.d/mfu-sensitive-probe.conf \
  /etc/fail2ban/filter.d/recidive.local \
  /etc/fail2ban/jail.d/mfu-sensitive-probe.conf; do
  if [[ -e "$target" ]]; then
    cp -a "$target" "$BACKUP_DIR/"
  fi
done

install -o root -g root -m 0644 \
  "$SOURCE_ROOT/filter.d/mfu-sensitive-probe.conf" \
  /etc/fail2ban/filter.d/mfu-sensitive-probe.conf
install -o root -g root -m 0644 \
  "$SOURCE_ROOT/filter.d/recidive.local" \
  /etc/fail2ban/filter.d/recidive.local
install -o root -g root -m 0644 \
  "$SOURCE_ROOT/jail.d/mfu-sensitive-probe.conf" \
  /etc/fail2ban/jail.d/mfu-sensitive-probe.conf

fail2ban-client -t
systemctl restart fail2ban
fail2ban-client status mfu-sensitive-probe
echo "FAIL2BAN_BACKUP=$BACKUP_DIR"
