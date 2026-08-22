#!/usr/bin/env bash
set -euo pipefail

if [[ ${EUID} -ne 0 ]]; then
  echo "rootで実行してください" >&2
  exit 1
fi

PUBLIC_KEY_FILE=${1:-}
if [[ -z "${PUBLIC_KEY_FILE}" || ! -f "${PUBLIC_KEY_FILE}" ]]; then
  echo "使用方法: $0 /path/to/mfu_mail_filter.pub" >&2
  exit 1
fi

if ! id mfu-mail-filter >/dev/null 2>&1; then
  useradd --system --create-home --home-dir /var/lib/mfu-mail-filter --shell /bin/bash mfu-mail-filter
fi

install -o root -g root -m 0755 \
  "$(dirname "$0")/mfu_mail_filter_agent.py" \
  /usr/local/sbin/mfu-mail-filter-agent

cat >/etc/sudoers.d/mfu-mail-filter-agent <<'EOF'
mfu-mail-filter ALL=(root) NOPASSWD: /usr/local/sbin/mfu-mail-filter-agent
EOF
chmod 0440 /etc/sudoers.d/mfu-mail-filter-agent
visudo -cf /etc/sudoers.d/mfu-mail-filter-agent >/dev/null

install -d -o mfu-mail-filter -g mfu-mail-filter -m 0700 /var/lib/mfu-mail-filter/.ssh
key=$(tr -d '\r\n' <"${PUBLIC_KEY_FILE}")
case "${key}" in
  ssh-ed25519\ *) ;;
  *) echo "Ed25519公開鍵を指定してください" >&2; exit 1 ;;
esac
printf 'restrict,command="sudo -n /usr/local/sbin/mfu-mail-filter-agent" %s\n' "${key}" \
  >/var/lib/mfu-mail-filter/.ssh/authorized_keys
chown mfu-mail-filter:mfu-mail-filter /var/lib/mfu-mail-filter/.ssh/authorized_keys
chmod 0600 /var/lib/mfu-mail-filter/.ssh/authorized_keys

install -d -o root -g root -m 0700 /mnt/mfu/mail-filter-backups
echo "mfu-mail-filter agent installed"
