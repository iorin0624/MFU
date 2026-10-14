#!/usr/bin/env bash
set -euo pipefail

install -o root -g root -m 0644 /mnt/mfu/app/deploy/mfu-media-fetch-worker.service /etc/systemd/system/mfu-media-fetch-worker.service
systemctl daemon-reload
systemctl enable --now mfu-media-fetch-worker.service
