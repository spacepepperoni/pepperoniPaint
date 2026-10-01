#!/usr/bin/env bash
# pepperoniPaint installer. Paste into a terminal:
#
#   curl -fsSL https://raw.githubusercontent.com/spacepepperoni/pepperoniPaint/main/get.sh | bash
#
# Installs for your user only (~/.local) and turns on automatic updates.
# The only thing that may ask for your password is installing PyQt6 (and git)
# from your distro, if they're missing. Safe to run again any time.
set -euo pipefail

main() {   # everything lives in a function so `curl | bash` reads the whole script first
  local repo="https://github.com/spacepepperoni/pepperoniPaint.git"
  local src="${XDG_DATA_HOME:-$HOME/.local/share}/pepperoniPaint/source"
  local flags=(--auto-update) need=() pm=()
  say() { printf '\n\033[1m%s\033[0m\n' "$*"; }

  . /etc/os-release 2>/dev/null || true
  command -v git >/dev/null || need+=(git)
  if [[ "${ID:-}" != steamos ]] && ! python3 -c 'import PyQt6.QtWidgets' 2>/dev/null; then
    need+=(pyqt6)
  fi
  if ((${#need[@]})); then
    if command -v apt-get >/dev/null; then pm=(apt-get install -y)
    elif command -v dnf >/dev/null; then pm=(dnf install -y)
    elif command -v pacman >/dev/null; then pm=(pacman -S --needed --noconfirm)
    elif command -v zypper >/dev/null; then pm=(zypper --non-interactive install)
    fi
    local pkgs=()
    for n in "${need[@]}"; do
      case "$n:${pm[0]:-}" in
        git:*) pkgs+=(git) ;;
        pyqt6:pacman) pkgs+=(python-pyqt6) ;;
        pyqt6:*) pkgs+=(python3-pyqt6) ;;
      esac
    done
    if ((${#pm[@]})); then
      say "Installing ${pkgs[*]} (sudo ${pm[*]} ${pkgs[*]}) — your password may be needed"
      sudo "${pm[@]}" "${pkgs[@]}" </dev/null || true
    fi
    command -v git >/dev/null || { echo "pepperoniPaint needs git. Install it and run this again."; exit 1; }
    if [[ "${ID:-}" != steamos ]] && ! python3 -c 'import PyQt6.QtWidgets' 2>/dev/null; then
      say "No PyQt6 from your distro — using a private copy instead (~100 MB download)"
      flags+=(--venv)
    fi
  fi

  if [[ -d "$src/.git" ]]; then
    say "Updating pepperoniPaint"
    git -C "$src" pull -q --ff-only
  else
    say "Downloading pepperoniPaint"
    mkdir -p "$(dirname "$src")"
    git clone -q --depth 1 "$repo" "$src"
  fi
  "$src/install.sh" "${flags[@]}"
  say "Done! Open pepperoniPaint from your app menu."
}

main "$@"
