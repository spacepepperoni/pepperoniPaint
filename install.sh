#!/usr/bin/env bash
# Installs pepperoniPaint for the current user (no root needed for the app
# itself). Re-run any time to update.
#   ./install.sh          use the system PyQt6 (install it with your package manager)
#   ./install.sh --venv   no root: put PyQt6 in a private venv (Steam Deck etc.)
#   --auto-update         also install a systemd user timer that runs update.sh
#                         (checks GitHub ~10 min after login, then every 6 hours)
set -euo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
share="${XDG_DATA_HOME:-$HOME/.local/share}"
dest="$share/pepperoniPaint"
bin="$HOME/.local/bin"
use_venv=0
auto_update=0
for arg in "$@"; do
  case "$arg" in
    --venv) use_venv=1 ;;
    --auto-update) auto_update=1 ;;
    *) echo "unknown option: $arg" >&2; exit 2 ;;
  esac
done
[[ -x "$dest/venv/bin/python" ]] && use_venv=1   # once a venv, always (update.sh passes no flags)
. /etc/os-release 2>/dev/null || true
[[ "${ID:-}" == "steamos" ]] && use_venv=1     # read-only root: venv is the only way

py=python3
if (( ! use_venv )); then
  if ! python3 -c 'import PyQt6.QtWidgets' 2>/dev/null; then
    echo "PyQt6 isn't installed for the system Python. Either install it:"
    if command -v apt >/dev/null; then echo "    sudo apt install python3-pyqt6"
    elif command -v dnf >/dev/null; then echo "    sudo dnf install python3-pyqt6"
    elif command -v pacman >/dev/null; then echo "    sudo pacman -S python-pyqt6"
    fi
    echo "…and re-run ./install.sh, or run ./install.sh --venv (no root, ~100 MB download)."
    exit 1
  fi
else
  venv="$dest/venv"
  if [[ ! -x "$venv/bin/python" ]] || ! "$venv/bin/python" -c 'import PyQt6.QtWidgets' 2>/dev/null; then
    echo "Creating venv in $venv …"
    rm -rf "$venv"
    if ! python3 -m venv "$venv" &>/dev/null; then
      # Debian without python3-venv has no ensurepip: bootstrap pip by hand
      rm -rf "$venv"
      python3 -m venv --without-pip "$venv"
      curl -fsSL https://bootstrap.pypa.io/get-pip.py -o "$venv/get-pip.py"
      "$venv/bin/python" "$venv/get-pip.py" -q
      rm -f "$venv/get-pip.py"
    fi
    "$venv/bin/python" -m pip install -q --upgrade PyQt6
  fi
  py="$venv/bin/python"
fi

mkdir -p "$dest" "$bin" "$share/applications" "$share/icons/hicolor/scalable/apps"
install -m 644 "$here/pepperoni_paint.py" "$dest/pepperoni_paint.py"
install -m 644 "$here/pepperonipaint.svg" "$share/icons/hicolor/scalable/apps/pepperonipaint.svg"
cat > "$bin/pepperonipaint" <<LAUNCH
#!/bin/sh
exec "$py" "$dest/pepperoni_paint.py" "\$@"
LAUNCH
chmod 755 "$bin/pepperonipaint"
cat > "$share/applications/pepperonipaint.desktop" <<DESK
[Desktop Entry]
Type=Application
Name=pepperoniPaint
GenericName=Paint
Comment=Paste, select, move and draw — a Windows 7-style Paint
Exec="$bin/pepperonipaint" %f
Icon=pepperonipaint
Terminal=false
Categories=Graphics;2DGraphics;RasterGraphics;
MimeType=image/png;image/jpeg;image/bmp;image/webp;image/gif;
StartupWMClass=pepperoniPaint
DESK
command -v update-desktop-database >/dev/null && update-desktop-database -q "$share/applications" || true
command -v kbuildsycoca6 >/dev/null && kbuildsycoca6 >/dev/null 2>&1 || true
if (( auto_update )); then
  units="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
  mkdir -p "$units"
  cat > "$units/pepperonipaint-update.service" <<UNIT
[Unit]
Description=Update pepperoniPaint from GitHub
Wants=network-online.target
After=network-online.target

[Service]
Type=oneshot
ExecStart="$here/update.sh"
UNIT
  cat > "$units/pepperonipaint-update.timer" <<UNIT
[Unit]
Description=Check for pepperoniPaint updates

[Timer]
OnStartupSec=10min
OnUnitActiveSec=6h
Persistent=true

[Install]
WantedBy=timers.target
UNIT
  if systemctl --user daemon-reload && systemctl --user enable --now pepperonipaint-update.timer; then
    echo "Auto-update on: checks ~10 min after login, then every 6 h (journalctl --user -u pepperonipaint-update)"
  else
    echo "Couldn't turn on auto-update (no systemd user session?). Update by hand with: $here/update.sh"
  fi
fi
echo "Installed. Launch \"pepperoniPaint\" from the app menu, or run: $bin/pepperonipaint [file]"
