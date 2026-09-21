#!/bin/sh
set -eu

# Run only on SE02 (server-103-16) as root after the repository is deployed to
# /mnt/mfu/app/inpa. This prepares OS-level prerequisites; it never creates a
# database, writes secrets, runs migrations, or starts services.

if [ "$(id -u)" -ne 0 ]; then
  echo "run as root" >&2
  exit 1
fi

if [ "$(hostname)" != "SE02" ]; then
  echo "refusing to install outside SE02" >&2
  exit 1
fi

app_dir=/mnt/mfu/app/inpa
if [ ! -f "$app_dir/backend/pyproject.toml" ]; then
  echo "INPA checkout is missing at $app_dir" >&2
  exit 1
fi

getent group inpa >/dev/null || groupadd --system inpa
getent group inpa_admin >/dev/null || groupadd --system inpa_admin
id -u inpa >/dev/null 2>&1 || useradd --system --gid inpa --home-dir /nonexistent --shell /usr/sbin/nologin inpa
if id -u mfu >/dev/null 2>&1; then
  usermod --append --groups inpa_admin mfu
fi

install -d -o inpa -g inpa -m 0750 "$app_dir/var"
install -d -o root -g inpa -m 0750 /etc/inpa
install -m 0755 "$app_dir/deploy/inpa-app-firewall.sh" /usr/local/sbin/inpa-app-firewall
install -m 0644 "$app_dir/deploy/systemd/inpa-web.service" /etc/systemd/system/inpa-web.service
install -m 0644 "$app_dir/deploy/systemd/inpa-admin-api.service" /etc/systemd/system/inpa-admin-api.service
install -m 0644 "$app_dir/deploy/systemd/inpa-mail-worker.service" /etc/systemd/system/inpa-mail-worker.service
install -m 0644 "$app_dir/deploy/systemd/inpa-app-firewall.service" /etc/systemd/system/inpa-app-firewall.service

systemctl daemon-reload
echo "OS prerequisites installed. Create /etc/inpa/runtime.env and provision MySQL before enabling INPA."
