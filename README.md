# pepperoniPaint

A simple Paint application for Linux reminiscent of Windows 7 era MS Paint.

## Install

```sh
curl -fsSL https://raw.githubusercontent.com/spacepepperoni/pepperoniPaint/main/get.sh | bash
```

### Uninstall

```sh
systemctl --user disable --now pepperonipaint-update.timer
rm -rf ~/.local/share/pepperoniPaint ~/.local/bin/pepperonipaint \
  ~/.local/share/applications/pepperonipaint.desktop \
  ~/.local/share/icons/hicolor/scalable/apps/pepperonipaint.svg \
  ~/.config/systemd/user/pepperonipaint-update.*
```

## Development

Work in a clone and run `./install.sh` after each change (`--venv` if PyQt6 isn't
installed system-wide). Pushes to `main` reach every auto-updating install.

## License

MIT
