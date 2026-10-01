#!/usr/bin/env bash
# Pull the latest pepperoniPaint and reinstall it.
# Run by the app (File → Check for updates), or by hand any time.
set -euo pipefail
cd "$(dirname "$0")"
git fetch -q
before=$(git rev-parse --short HEAD)
git merge -q --ff-only '@{u}'
after=$(git rev-parse --short HEAD)
if [[ "$before" == "$after" ]]; then
  echo "Already at $(git log -1 --format='%h %s'); reinstalling"
else
  echo "Updated pepperoniPaint $before → $(git log -1 --format='%h %s')"
fi
./install.sh
