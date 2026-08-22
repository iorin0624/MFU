#!/usr/bin/env bash
set -euo pipefail

FW_HOST="${FW_BAN_HOST:-192.168.103.15}"
KEY_PATH="${FW_BAN_IDENTITY_FILE:-/mnt/mfu/ssh/fw_ban_ed25519}"
KNOWN_HOSTS="${FW_BAN_KNOWN_HOSTS:-/mnt/mfu/ssh/known_hosts}"
HELPER_SOURCE="${1:-/tmp/mfu_fw_ban_20260716/mfu-fw-ban-ssh}"
STAMP="$(date +%Y%m%d_%H%M%S)"

install -d -o mfu -g mfu -m 0700 /mnt/mfu/ssh
if [[ ! -f "$KEY_PATH" ]]; then
  ssh-keygen -q -t ed25519 -N '' -C 'mfu-fw-ban@SE02' -f "$KEY_PATH"
fi
chown mfu:mfu "$KEY_PATH" "$KEY_PATH.pub"
chmod 0600 "$KEY_PATH"
chmod 0644 "$KEY_PATH.pub"

if ! ssh-keygen -F "$FW_HOST" -f "$KNOWN_HOSTS" >/dev/null 2>&1; then
  trusted_line="$(ssh-keygen -F "$FW_HOST" -f /root/.ssh/known_hosts | grep -v '^#' | head -n 1)"
  if [[ -z "$trusted_line" ]]; then
    echo "Trusted host key was not found for $FW_HOST" >&2
    exit 1
  fi
  printf '%s\n' "$trusted_line" >> "$KNOWN_HOSTS"
fi
chown mfu:mfu "$KNOWN_HOSTS"
chmod 0644 "$KNOWN_HOSTS"

backup_dir="/root/mfu-backups/fw-ban-$STAMP"
ssh root@"$FW_HOST" "mkdir -p '$backup_dir'; cp -a /root/.ssh/authorized_keys '$backup_dir/authorized_keys'; if [[ -e /usr/local/sbin/mfu-fw-ban-ssh ]]; then cp -a /usr/local/sbin/mfu-fw-ban-ssh '$backup_dir/mfu-fw-ban-ssh'; fi"
scp -q "$HELPER_SOURCE" root@"$FW_HOST":/tmp/mfu-fw-ban-ssh
ssh root@"$FW_HOST" "install -o root -g root -m 0755 /tmp/mfu-fw-ban-ssh /usr/local/sbin/mfu-fw-ban-ssh; rm -f /tmp/mfu-fw-ban-ssh; python3 -m py_compile /usr/local/sbin/mfu-fw-ban-ssh"

public_key="$(cat "$KEY_PATH.pub")"
if ! ssh root@"$FW_HOST" "grep -q 'mfu-fw-ban@SE02' /root/.ssh/authorized_keys"; then
  printf 'restrict,command="/usr/local/sbin/mfu-fw-ban-ssh" %s\n' "$public_key" |
    ssh root@"$FW_HOST" 'umask 077; cat >> /root/.ssh/authorized_keys'
fi
ssh root@"$FW_HOST" 'chmod 0600 /root/.ssh/authorized_keys'

echo "FW_BACKUP=$backup_dir"
ssh-keygen -lf "$KEY_PATH.pub"
