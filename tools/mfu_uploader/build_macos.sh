#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")"

python3 -m pip install -r requirements.txt
python3 -m PyInstaller \
  --noconfirm \
  --clean \
  --distpath dist_v10 \
  --workpath build_v10 \
  MFUUploader.spec

echo
echo "Built: $(pwd)/dist_v10/MFUUploader.app"
