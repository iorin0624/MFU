#!/bin/sh
set -eu

install -o root -g root -m 0755 /mnt/mfu/app/deploy/mfu-app-firewall.sh /usr/local/sbin/mfu-app-firewall
install -o root -g root -m 0644 /mnt/mfu/app/deploy/mfu-app-firewall.service /etc/systemd/system/mfu-app-firewall.service
install -o root -g root -m 0644 /mnt/mfu/app/deploy/mfu-upload-lan.service /etc/systemd/system/mfu-upload-lan.service

systemctl daemon-reload
systemctl enable mfu-app-firewall.service mfu-upload-lan.service
systemctl restart mfu-app-firewall.service
systemctl restart mfu-upload-lan.service

