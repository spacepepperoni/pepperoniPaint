# pepperoniPaint

A small Windows 7-style Paint for Linux (PyQt6). Built for pasting screenshots,
selecting and moving parts of them, and scribbling on top.

- **Paste** (Ctrl+V) from the clipboard, a copied file, or by dropping a file
  on the window. On a fresh canvas, the canvas snaps to the pasted image's size;
  otherwise the canvas grows if the paste is bigger.
- **Select** (S): drag a box, then drag inside it to move it. The 8 handles stretch it
  (Shift on a corner keeps the aspect ratio). Ctrl+drag moves a copy. Arrow keys nudge it.
  Enter/Esc drops it. *Transparent selection* (Select ▾) makes Color 2 see-through.
- **Pencil** (P, the default tool), **Brush** (B), **Fill** (F), **Eraser** (E),
  **Color picker** (K), **Line** (L), **Rectangle** (R). Shift snaps lines to 45° and makes rectangles square.
- Left button = Color 1, right button = Color 2; palette right-click sets Color 2; X swaps.
- Crop (Ctrl+Shift+X), Rotate/Flip, Canvas size (Ctrl+E), or drag the handles at the canvas edge.
- Ctrl+wheel zooms (Ctrl+1 = 100%, Ctrl+0 = fit); middle-drag pans.
- Ctrl+C with no selection copies the whole image.

100% zoom is one image pixel per *screen* pixel, even with fractional display
scaling (e.g. 215%), so screenshots show at their real size and are never resampled.

## Install

Paste this into a terminal:

```sh
curl -fsSL https://raw.githubusercontent.com/spacepepperoni/pepperoniPaint/main/get.sh | bash
```

Then open **pepperoniPaint** from your app menu.

- Installs for your user only. The only thing that may ask for your password is
  installing PyQt6 (and git) from your distro if they're missing. If that isn't possible
  (e.g. on a Steam Deck), it downloads a private copy of PyQt6 instead.
- **Updates are automatic.** A small background timer checks GitHub about 10 minutes
  after you log in and then every 6 hours, and reinstalls only when there's something new.
  An already-open window keeps the old version until you reopen it.
  To update right now, run the same command again.

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
