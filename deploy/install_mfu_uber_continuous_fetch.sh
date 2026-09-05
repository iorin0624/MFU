#!/bin/sh
set -eu
install -m 0644 /mnt/mfu/app/deploy/mfu-uber-continuous-fetch.service /etc/systemd/system/mfu-uber-continuous-fetch.service
install -m 0644 /mnt/mfu/app/deploy/mfu-uber-continuous-fetch.timer /etc/systemd/system/mfu-uber-continuous-fetch.timer
systemctl daemon-reload
systemctl enable --now mfu-uber-continuous-fetch.timer
systemctl list-timers mfu-uber-continuous-fetch.timer --no-pager
