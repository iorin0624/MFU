#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")"

flutter pub get
dart run flutter_launcher_icons
flutter analyze
flutter test
flutter build ipa --release

echo
echo "Built: $(pwd)/build/ios/ipa"
