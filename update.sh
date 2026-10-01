#!/usr/bin/env bash
# Pull the latest pepperoniPaint and reinstall, but only if something changed.
# Run by the pepperonipaint-update systemd user timer (./install.sh --auto-update),
# or by hand any time.
set -euo pipefail
cd "$(dirname "$0")"
git fetch -q
local_rev=$(git rev-parse HEAD)
remote_rev=$(git rev-parse '@{u}')
if [[ "$local_rev" == "$remote_rev" ]]; then
  echo "pepperoniPaint is up to date ($(git log -1 --format='%h %s'))"
  exit 0
fi
git merge -q --ff-only '@{u}'
echo "Updated pepperoniPaint ${local_rev:0:7} → $(git log -1 --format='%h %s')"
./install.sh
