#!/usr/bin/env bash
# Installs pepperoniPaint for the current user (no root needed for the app
# itself). Re-run any time to reinstall.
#   ./install.sh              use the system PyQt6 (install it with your package manager)
#   ./install.sh --venv       no root: put PyQt6 in a private venv (Steam Deck etc.)
#   ./install.sh --uninstall  remove pepperoniPaint (keeps your settings)
# Updates are handled by the app itself (File → Check for updates).
set -euo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
share="${XDG_DATA_HOME:-$HOME/.local/share}"
dest="$share/pepperoniPaint"
bin="$HOME/.local/bin"
units="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
use_venv=0
uninstall=0
for arg in "$@"; do
  case "$arg" in
    --venv) use_venv=1 ;;
    --uninstall) uninstall=1 ;;
    --auto-update) ;;   # 0.1.0's background timer; the app checks for updates itself now
    *) echo "unknown option: $arg" >&2; exit 2 ;;
  esac
done

refresh_menus() {
  command -v update-desktop-database >/dev/null && update-desktop-database -q "$share/applications" || true
  command -v kbuildsycoca6 >/dev/null && kbuildsycoca6 >/dev/null 2>&1 || true
}

# 0.1.0 installed a systemd timer that updated silently. The app now asks before
# updating, so the timer goes away on every (re)install.
if [[ -e "$units/pepperonipaint-update.timer" ]]; then
  systemctl --user disable --now pepperonipaint-update.timer >/dev/null 2>&1 || true
  rm -f "$units/pepperonipaint-update.timer" "$units/pepperonipaint-update.service"
  systemctl --user daemon-reload >/dev/null 2>&1 || true
fi

if (( uninstall )); then
  rm -rf "$dest" "$bin/pepperonipaint" "$share/applications/pepperonipaint.desktop" \
         "$share/icons/hicolor/scalable/apps/pepperonipaint.svg"
  rm -f "$share"/icons/hicolor/*/apps/pepperonipaint.png
  refresh_menus
  echo "pepperoniPaint is uninstalled."
  exit 0
fi

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
install -m 644 "$here/pepperonipaint.svg" "$dest/pepperonipaint.svg"
install -m 644 "$here/pepperonipaint.svg" "$share/icons/hicolor/scalable/apps/pepperonipaint.svg"
# PNGs as well as the SVG: every desktop can show a PNG, with or without Qt's
# SVG plugin, and the window icon is built from these.
mkdir -p "$dest/icons"
for png in "$here"/icons/pepperonipaint-*.png; do
  size=$(basename "$png" .png); size=${size#pepperonipaint-}
  install -m 644 "$png" "$dest/icons/"
  install -Dm 644 "$png" "$share/icons/hicolor/${size}x${size}/apps/pepperonipaint.png"
done
printf '%s\n' "$here" > "$dest/source-path"     # where the app's updater pulls from
cat > "$bin/pepperonipaint" <<LAUNCH
#!/bin/sh
exec "$py" "$dest/pepperoni_paint.py" "\$@"
LAUNCH
chmod 755 "$bin/pepperonipaint"
# Icon is an absolute path: a theme name can stay invisible until Plasma's icon
# cache notices the new ~/.local/share/icons directory (often not until re-login).
cat > "$share/applications/pepperonipaint.desktop" <<DESK
[Desktop Entry]
Type=Application
Name=pepperoniPaint
GenericName=Paint
Comment=Paste, select, move and draw — a Windows 7-style Paint
Exec="$bin/pepperonipaint" %f
Icon=$dest/icons/pepperonipaint-256.png
Terminal=false
Categories=Graphics;2DGraphics;RasterGraphics;
MimeType=image/png;image/jpeg;image/bmp;image/webp;image/gif;
StartupWMClass=pepperoniPaint
DESK
refresh_menus
echo "Installed. Launch \"pepperoniPaint\" from the app menu, or run: $bin/pepperonipaint [file]"
