#!/usr/bin/env bash
set -euo pipefail

install -o root -g root -m 0644 /mnt/mfu/app/deploy/mfu-tdr-refresh.service /etc/systemd/system/mfu-tdr-refresh.service
install -o root -g root -m 0644 /mnt/mfu/app/deploy/mfu-tdr-refresh.timer /etc/systemd/system/mfu-tdr-refresh.timer
systemctl daemon-reload
systemctl enable --now mfu-tdr-refresh.timer
systemctl list-timers mfu-tdr-refresh.timer --no-pager

