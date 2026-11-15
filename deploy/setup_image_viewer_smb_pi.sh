#!/bin/sh
# Run as root on the Raspberry Pi. Creates a separate Samba credential without
# changing the password used by existing smb clients.
set -eu

USER_NAME=mfu_imageviewer
SECRET_FILE=/root/mfu_imageviewer_smb.credentials
CONF=/etc/samba/smb.conf

if ! getent passwd "$USER_NAME" >/dev/null; then
    useradd --system --no-create-home --gid smb --shell /usr/sbin/nologin "$USER_NAME"
fi
if [ ! -f "$SECRET_FILE" ]; then
    if pdbedit -L 2>/dev/null | cut -d: -f1 | grep -Fxq "$USER_NAME"; then
        echo 'Samba user exists without the expected protected credential file.' >&2
        exit 1
    fi
    password=$(openssl rand -hex 32)
    printf '%s\n%s\n' "$password" "$password" | smbpasswd -s -a "$USER_NAME" >/dev/null
    umask 077
    printf 'username=%s\npassword=%s\n' "$USER_NAME" "$password" > "$SECRET_FILE"
    unset password
    chmod 600 "$SECRET_FILE"
fi

if ! grep -Fxq 'valid users = smb mfu_imageviewer' "$CONF"; then
    if [ "$(grep -Fxc 'valid users = smb' "$CONF")" -ne 1 ]; then
        echo 'Unexpected Samba share configuration; not editing.' >&2
        exit 1
    fi
    cp -a "$CONF" "${CONF}.before-mfu-imageviewer-$(date +%Y%m%d%H%M%S)"
    sed -i 's/^valid users = smb$/valid users = smb mfu_imageviewer/' "$CONF"
fi
testparm -s >/dev/null 2>&1
systemctl reload smbd
systemctl is-active --quiet smbd
