#!/bin/sh
# Deploy the image-viewer toolbar source and compiled assets on MFU SE02.
set -eu

APP=/mnt/mfu/app
STAGE=/tmp/mfu-imageviewer-toolbar-stage
BACKUP="/mnt/mfu/deploy-backups/image-viewer-toolbar-$(date +%Y%m%d%H%M%S)"
FILES='image_viewer/frontend/src/components/ExplorerWindow.vue image_viewer/frontend/src/styles.css image_viewer/template/image_viewer_vue.html static/image_viewer_vue/image-viewer.js static/image_viewer_vue/image-viewer.css'

for name in ExplorerWindow.vue styles.css image_viewer_vue.html image-viewer.js image-viewer.css; do
    test -s "$STAGE/$name"
done
install -d -o root -g root -m 0750 "$BACKUP"
(
    cd "$APP"
    for path in $FILES; do
        cp -a --parents "$path" "$BACKUP"
    done
)

rollback() {
    if [ "${complete:-0}" = 0 ]; then
        echo 'Toolbar validation failed; restoring prior files.' >&2
        (cd "$BACKUP" && for path in $FILES; do cp -a "$path" "$APP/$path"; done)
        systemctl restart mfu.service || true
    fi
}
trap rollback EXIT

install -o mfu -g mfu -m 0644 "$STAGE/ExplorerWindow.vue" "$APP/image_viewer/frontend/src/components/ExplorerWindow.vue"
install -o mfu -g mfu -m 0644 "$STAGE/styles.css" "$APP/image_viewer/frontend/src/styles.css"
install -o mfu -g mfu -m 0644 "$STAGE/image_viewer_vue.html" "$APP/image_viewer/template/image_viewer_vue.html"
install -o mfu -g mfu -m 0644 "$STAGE/image-viewer.js" "$APP/static/image_viewer_vue/image-viewer.js"
install -o mfu -g mfu -m 0644 "$STAGE/image-viewer.css" "$APP/static/image_viewer_vue/image-viewer.css"

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
test "$(curl -sS --max-time 5 -o /dev/null -w '%{http_code}' http://127.0.0.1:8080/static/image_viewer_vue/image-viewer.js)" = 200
cmp -s "$STAGE/image-viewer.js" "$APP/static/image_viewer_vue/image-viewer.js"
complete=1
echo "Image viewer toolbar deployed; rollback files: $BACKUP"
