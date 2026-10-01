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

```sh
sudo apt install python3-pyqt6      # Debian      (Fedora: sudo dnf install python3-pyqt6)
./install.sh
```

No root, or Steam Deck: `./install.sh --venv` puts PyQt6 in a private venv.
Run `./install.sh` again after pulling updates.

## Other devices (pull-only, auto-updating)

Each device gets its own **read-only deploy key**: an SSH key that can pull this
one repo and nothing else, so no GitHub login is stored on the device.

1. Make the key and print it:
   ```sh
   ssh-keygen -t ed25519 -N "" -f ~/.ssh/pepperonipaint_deploy -C "pepperoniPaint@$(hostname)"
   cat ~/.ssh/pepperonipaint_deploy.pub
   ```
2. Add that line on GitHub: repo → **Settings → Deploy keys → Add deploy key**.
   Leave *Allow write access* unticked.
3. Clone and install with auto-update (replace `YOURNAME`):
   ```sh
   key='ssh -i ~/.ssh/pepperonipaint_deploy -o IdentitiesOnly=yes -o StrictHostKeyChecking=accept-new'
   GIT_SSH_COMMAND="$key" git clone git@github.com:YOURNAME/pepperoniPaint.git ~/pepperoniPaint
   git -C ~/pepperoniPaint config core.sshCommand "$key"
   cd ~/pepperoniPaint && ./install.sh --auto-update
   ```
   Install PyQt6 first (`sudo apt install python3-pyqt6` / `sudo dnf install python3-pyqt6`).
   On a Steam Deck, skip that: `install.sh` detects SteamOS and uses its own venv.

The `pepperonipaint-update` timer checks about 10 minutes after login and then every
6 hours. It pulls and reinstalls only when something changed. An already-open window
keeps the old version until you reopen it. To update right now, run `~/pepperoniPaint/update.sh`;
for its log, run `journalctl --user -u pepperonipaint-update`.
