#!/bin/sh
# Run as root on MFU SE02 after copying the reviewed files into STAGE.
set -eu

APP=/mnt/mfu/app
STAGE=/tmp/mfu-imageviewer-stage
BACKUP="/mnt/mfu/deploy-backups/image-viewer-media-tools-$(date +%Y%m%d%H%M%S)"
OLD_FILES='image_viewer/routes.py image_viewer/catalog.py image_viewer/frontend/src/api/client.ts image_viewer/frontend/src/types.ts image_viewer/frontend/src/styles.css image_viewer/frontend/src/components/ExplorerWindow.vue image_viewer/frontend/src/components/ImageViewerWindow.vue static/image_viewer_vue/image-viewer.js static/image_viewer_vue/image-viewer.css'

for name in routes.py catalog.py image_edits.py smb_transfer.py client.ts types.ts styles.css ExplorerWindow.vue ImageViewerWindow.vue CropDialog.vue image-viewer.js image-viewer.css; do
    test -s "$STAGE/$name"
done
install -d -o root -g root -m 0750 "$BACKUP"
(
    cd "$APP"
    for path in $OLD_FILES; do
        cp -a --parents "$path" "$BACKUP"
    done
)

rollback() {
    if [ "${complete:-0}" = 0 ]; then
        echo 'Deployment validation failed; restoring prior image viewer files.' >&2
        (cd "$BACKUP" && for path in $OLD_FILES; do cp -a "$path" "$APP/$path"; done)
        systemctl restart mfu.service || true
    fi
}
trap rollback EXIT

install -o mfu -g mfu -m 0644 "$STAGE/routes.py" "$APP/image_viewer/routes.py"
install -o mfu -g mfu -m 0644 "$STAGE/catalog.py" "$APP/image_viewer/catalog.py"
install -o mfu -g mfu -m 0644 "$STAGE/image_edits.py" "$APP/image_viewer/image_edits.py"
install -o mfu -g mfu -m 0644 "$STAGE/smb_transfer.py" "$APP/image_viewer/smb_transfer.py"
install -o mfu -g mfu -m 0644 "$STAGE/client.ts" "$APP/image_viewer/frontend/src/api/client.ts"
install -o mfu -g mfu -m 0644 "$STAGE/types.ts" "$APP/image_viewer/frontend/src/types.ts"
install -o mfu -g mfu -m 0644 "$STAGE/styles.css" "$APP/image_viewer/frontend/src/styles.css"
install -o mfu -g mfu -m 0644 "$STAGE/ExplorerWindow.vue" "$APP/image_viewer/frontend/src/components/ExplorerWindow.vue"
install -o mfu -g mfu -m 0644 "$STAGE/ImageViewerWindow.vue" "$APP/image_viewer/frontend/src/components/ImageViewerWindow.vue"
install -o mfu -g mfu -m 0644 "$STAGE/CropDialog.vue" "$APP/image_viewer/frontend/src/components/CropDialog.vue"
install -o mfu -g mfu -m 0644 "$STAGE/image-viewer.js" "$APP/static/image_viewer_vue/image-viewer.js"
install -o mfu -g mfu -m 0644 "$STAGE/image-viewer.css" "$APP/static/image_viewer_vue/image-viewer.css"

# The service cannot create directories directly under /mnt/mfu.
# Retained originals must remain outside the public upload tree.
install -d -o mfu -g mfu -m 0700 /mnt/mfu/image_viewer_crop_originals

/mnt/mfu/venv/bin/python -m py_compile "$APP/image_viewer/routes.py" "$APP/image_viewer/catalog.py" "$APP/image_viewer/image_edits.py" "$APP/image_viewer/smb_transfer.py"
systemctl restart mfu.service
systemctl is-active --quiet mfu.service
ready=0
for attempt in $(seq 1 30); do
    if [ "$(curl -sS --max-time 3 -H 'Accept: text/html' -o /dev/null -w '%{http_code}' http://127.0.0.1:8080/image_viewer/ 2>/dev/null || true)" = 302 ]; then
        ready=1
        break
    fi
    sleep 2
done
test "$ready" = 1
test "$(curl -sS -H 'Accept: application/json' -o /dev/null -w '%{http_code}' http://127.0.0.1:8080/image_viewer/api/entries/crop)" = 401
complete=1
echo "Image viewer deployed; previous files: $BACKUP"
