#!/bin/sh
# Run as root on MFU SE02 after setup_image_viewer_smb_pi.sh on the Pi.
set -eu

if [ "$(id -u mfu)" != 997 ] || [ "$(id -g mfu)" != 985 ]; then
    echo 'mfu uid/gid differs from the mount unit; review before installing.' >&2
    exit 1
fi
apt-get install -y cifs-utils
install -d -o root -g root -m 0755 /etc/mfu
install -d -o root -g root -m 0755 /mnt/mfu/image_viewer_smb
if [ ! -e /etc/mfu/image_viewer_smb.credentials ]; then
    scp -q root@192.168.103.15:/root/mfu_imageviewer_smb.credentials /etc/mfu/image_viewer_smb.credentials
fi
chown root:root /etc/mfu/image_viewer_smb.credentials
chmod 0600 /etc/mfu/image_viewer_smb.credentials
if [ -e /etc/systemd/system/mnt-mfu-image_viewer_smb.mount ] &&
   ! cmp -s /mnt/mfu/app/deploy/systemd/mnt-mfu-image_viewer_smb.mount /etc/systemd/system/mnt-mfu-image_viewer_smb.mount; then
    echo 'An unrelated SMB mount unit exists; not overwriting.' >&2
    exit 1
fi
install -o root -g root -m 0644 /mnt/mfu/app/deploy/systemd/mnt-mfu-image_viewer_smb.mount /etc/systemd/system/
install -o root -g root -m 0644 /mnt/mfu/app/deploy/systemd/mnt-mfu-image_viewer_smb.automount /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now mnt-mfu-image_viewer_smb.automount
ls -A /mnt/mfu/image_viewer_smb >/dev/null
findmnt -rn -o FSTYPE -T /mnt/mfu/image_viewer_smb | grep -qx cifs
runuser -u mfu -- sh -c 'test -w /mnt/mfu/image_viewer_smb'
