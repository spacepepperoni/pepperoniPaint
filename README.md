# pepperoniPaint

A simple Paint application for Linux reminiscent of Windows 7 era MS Paint.

## Install

```sh
curl -fsSL https://raw.githubusercontent.com/spacepepperoni/pepperoniPaint/main/get.sh | bash
```

pepperoniPaint checks for updates on its own and asks before installing them
(File → Check for updates / Automatic updates).

### Uninstall

```sh
~/.local/share/pepperoniPaint/source/install.sh --uninstall
```

## Development

Work in a clone and run `./install.sh` after each change (`--venv` if PyQt6 isn't
installed system-wide). To release, bump `__version__` in `pepperoni_paint.py`, push to
`main` and tag `vX.Y.Z`. Installs are only offered an update when the version goes up.

## License

MIT
