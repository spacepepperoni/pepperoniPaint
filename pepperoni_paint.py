#!/usr/bin/env python3
"""pepperoniPaint: a small Windows 7-style Paint.

Paste, select, move, draw. The image is always an opaque RGB32 QImage; the
view only ever reads it, so display scaling can never alter the pixels.

Zoom is measured in DEVICE pixels: 100% puts one image pixel on one screen
pixel even under fractional scaling (e.g. 2.15x), so a pasted screenshot
shows at exactly the size it was captured.
"""
import math
import os
import re
import subprocess
import sys
import threading

from PyQt6.QtCore import (QPoint, QPointF, QProcess, QRect, QRectF, QSettings, QSize,
                          QObject, Qt, QTimer, pyqtSignal)
from PyQt6.QtGui import (QAbstractTextDocumentLayout, QAction, QActionGroup,
                         QColor, QCursor, QFont, QFontDatabase, QIcon, QImage,
                         QImageReader, QImageWriter, QIntValidator, QKeySequence,
                         QPainter, QPalette, QPen, QPixmap, QPolygonF, QTextCharFormat,
                         QTextCursor, QTextDocument, QTransform)
from PyQt6.QtWidgets import (QApplication, QColorDialog, QComboBox, QDialog,
                             QDialogButtonBox, QFileDialog, QFormLayout, QGridLayout,
                             QHBoxLayout, QLabel, QMainWindow, QMenu,
                             QMessageBox, QProgressDialog, QScrollArea, QSpinBox, QToolBar,
                             QToolButton, QVBoxLayout, QWidget, QWidgetAction)

APP = "pepperoniPaint"
__version__ = "0.3.0"       # bump to release: installs only offer updates when this goes up
REPO_URL = "https://github.com/spacepepperoni/pepperoniPaint"
INSTALL_DIR = os.path.join(os.environ.get("XDG_DATA_HOME") or os.path.expanduser("~/.local/share"), APP)
UPDATE_EVERY_MS = 6 * 60 * 60 * 1000
RGB32 = QImage.Format.Format_RGB32
MARGIN = 14         # logical px of workspace around the canvas (room for its handles)
HANDLE = 6          # logical px, selection / text box handle squares
CANVAS_HANDLE = 9   # logical px, canvas resize handles
ROT_OFFSET = 24     # logical px from a selection's edge to its rotation knob
UNDO_BYTES = 600 * 1024 * 1024
UNDO_STEPS = 100
ZOOMS = [0.125, 0.25, 0.5, 1, 2, 3, 4, 5, 6, 7, 8]
DEFAULT_SIZE = QSize(800, 600)

PALETTE = [
    "#000000", "#7f7f7f", "#880015", "#ed1c24", "#ff7f27",
    "#fff200", "#22b14c", "#00a2e8", "#3f48cc", "#a349a4",
    "#ffffff", "#c3c3c3", "#b97a57", "#ffaec9", "#ffc90e",
    "#efe4b0", "#b5e61d", "#99d9ea", "#7092be", "#c8bfe7",
]

TOOLS = {   # id: (label, theme icons, key, default size, sizes)
    "select": ("Select", ["select-rectangular", "edit-select"], "S", 0, []),
    "pencil": ("Pencil", ["draw-freehand", "draw-pencil"], "P", 1, [1, 2, 3, 4]),
    "brush": ("Brush", ["draw-brush", "brush"], "B", 5, [1, 3, 5, 8, 12, 20]),
    "fill": ("Fill", ["fill-color", "color-fill"], "F", 0, []),
    "picker": ("Picker", ["color-picker", "color-select"], "K", 0, []),
    "eraser": ("Eraser", ["draw-eraser", "edit-clear"], "E", 8, [4, 6, 8, 10, 16, 24]),
    "line": ("Line", ["draw-line"], "L", 3, [1, 2, 3, 5, 8]),
    "rect": ("Rectangle", ["draw-rectangle"], "R", 3, [1, 2, 3, 5, 8]),
    "text": ("Text", ["draw-text", "insert-text"], "T", 0, []),
}

# Offered in the Text group when installed; the generic names always resolve.
COMMON_FONTS = ["Sans Serif", "Serif", "Monospace",
                "Noto Sans", "DejaVu Sans", "Liberation Sans", "Arial", "Ubuntu", "Cantarell",
                "Noto Serif", "DejaVu Serif", "Liberation Serif", "Times New Roman", "Georgia",
                "DejaVu Sans Mono", "Liberation Mono", "Courier New", "Hack",
                "Comic Neue", "Comic Sans MS", "Impact"]
FONT_SIZES = [8, 9, 10, 11, 12, 14, 16, 18, 20, 24, 28, 36, 48, 72]


def icon(names):
    for n in names:
        i = QIcon.fromTheme(n)
        if not i.isNull():
            return i
    return QIcon()


def swatch_icon(color, size=22):
    pm = QPixmap(size, size)
    pm.fill(QColor(color))
    p = QPainter(pm)
    p.setPen(QColor("#808080"))
    p.drawRect(0, 0, size - 1, size - 1)
    p.end()
    return QIcon(pm)


def size_icon(size, square, w, h, color):
    """A dot (or square, for the eraser) as big as the brush, like Paint's size list."""
    dpr = QApplication.instance().devicePixelRatio()
    pm = QPixmap(round(w * dpr), round(h * dpr))
    pm.setDevicePixelRatio(dpr)
    pm.fill(Qt.GlobalColor.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(color)
    d = max(1.5, min(float(size), h - 4.0))
    r = QRectF((w - d) / 2, (h - d) / 2, d, d)
    p.drawRect(r) if square else p.drawEllipse(r)
    p.end()
    return QIcon(pm)


def available_fonts():
    have = {f.lower(): f for f in QFontDatabase.families()}
    out = []
    for f in COMMON_FONTS:
        if f in ("Sans Serif", "Serif", "Monospace"):
            out.append(f)
        elif f.lower() in have and have[f.lower()] not in out:
            out.append(have[f.lower()])
    return out


def workspace_color(pal):
    """The grey area around the canvas. Like Win7 Paint's app-workspace color, it
    follows the system theme: a shade darker than the window, so pure-black
    themes stay black and light themes get a soft grey."""
    w = pal.color(QPalette.ColorRole.Window)
    return w.darker(140) if w.lightness() < 128 else w.darker(112)


def opaque(img):
    """Flatten any image onto white as RGB32 (Paint has no transparency)."""
    img.setDevicePixelRatio(1)
    if img.format() == RGB32:
        return QImage(img)
    if not img.hasAlphaChannel():
        return img.convertToFormat(RGB32)
    out = QImage(img.size(), RGB32)
    out.fill(Qt.GlobalColor.white)
    p = QPainter(out)
    p.drawImage(0, 0, img)
    p.end()
    return out


def flip_image(img, horizontal):
    if hasattr(img, "flipped"):
        o = Qt.Orientation.Horizontal if horizontal else Qt.Orientation.Vertical
        return img.flipped(o)
    return img.mirrored(horizontal, not horizontal)


def flood_fill(img, x, y, color):
    """Scanline fill on a raw RGB32 buffer. Runs are measured with C-speed
    slice compares, so a full-screen fill takes milliseconds, not seconds."""
    w, h = img.width(), img.height()
    bpl = img.bytesPerLine()
    ptr = img.constBits()
    ptr.setsize(img.sizeInBytes())
    b = bytearray(ptr.asstring(img.sizeInBytes()))
    off = y * bpl + 4 * x
    t = bytes(b[off:off + 4])
    f = (QColor(color).rgb() | 0xFF000000).to_bytes(4, "little")
    if t == f:
        return None
    T = t * w

    def gallop(good, maxn):        # largest n in [1, maxn] with good(n)
        lo, n = 1, 2
        while True:
            n = min(n, maxn)
            if n <= lo:
                return lo
            if good(n):
                lo, n = n, n * 2
                continue
            hi = n
            break
        while hi - lo > 1:
            mid = (lo + hi) // 2
            if good(mid):
                lo = mid
            else:
                hi = mid
        return lo

    def run_right(rs, x):
        return x + gallop(lambda n: b[rs + 4 * x:rs + 4 * (x + n)] == T[:4 * n], w - x)

    def run_left(rs, x):
        return x + 1 - gallop(lambda n: b[rs + 4 * (x + 1 - n):rs + 4 * (x + 1)] == T[:4 * n], x + 1)

    stack = [(x, y)]
    while stack:
        x, y = stack.pop()
        rs = y * bpl
        if b[rs + 4 * x:rs + 4 * x + 4] != t:
            continue
        x1, x2 = run_left(rs, x), run_right(rs, x)
        b[rs + 4 * x1:rs + 4 * x2] = f * (x2 - x1)
        for ny in (y - 1, y + 1):
            if not 0 <= ny < h:
                continue
            nrs = ny * bpl
            pos = x1
            while pos < x2:
                i = b.find(t, nrs + 4 * pos, nrs + 4 * x2)
                if i < 0:
                    break
                o = i - nrs
                if o % 4:
                    pos = o // 4 + 1
                    continue
                stack.append((o // 4, ny))
                pos = run_right(nrs, o // 4)
    return QImage(bytes(b), w, h, bpl, RGB32).copy()


def version_tuple(v):
    return tuple(int(x) for x in re.findall(r"\d+", v)[:3]) or (0,)


def source_dir():
    """The git clone this copy was installed from (install.sh records it)."""
    try:
        with open(os.path.join(INSTALL_DIR, "source-path")) as f:
            p = f.read().strip()
    except OSError:
        return None
    return p if os.path.isdir(os.path.join(p, ".git")) else None


class Updater(QObject):
    """Asks GitHub (through the install's git clone) whether a newer version
    exists, and installs it with the clone's update.sh. All git work happens on
    a worker thread; results come back as queued signals."""
    found = pyqtSignal(str, str, bool)      # version, what's new, manual
    current = pyqtSignal(bool)              # manual
    failed = pyqtSignal(str, bool)          # message, manual
    installed = pyqtSignal(bool, str)       # ok, output

    def __init__(self):
        super().__init__()
        self.busy = False

    def _git(self, src, *args):
        env = dict(os.environ, GIT_TERMINAL_PROMPT="0", LC_ALL="C")
        return subprocess.run(["git", "-C", src, *args], capture_output=True, text=True,
                              timeout=90, env=env)

    def check(self, manual):
        if self.busy:
            return
        src = source_dir()
        if not src:
            self.failed.emit("This copy of pepperoniPaint wasn't installed with the installer, "
                             "so it can't update itself.", manual)
            return
        self.busy = True
        threading.Thread(target=self._check, args=(src, manual), daemon=True).start()

    def _check(self, src, manual):
        try:
            r = self._git(src, "fetch", "-q")
            if r.returncode:
                raise RuntimeError(r.stderr.strip() or "git fetch failed")
            r = self._git(src, "show", "@{u}:pepperoni_paint.py")
            m = re.search(r'^__version__ = "([^"]+)"', r.stdout, re.M)
            latest = m.group(1) if m else "0"
            if version_tuple(latest) > version_tuple(__version__):
                log = self._git(src, "log", "--format=%s", "HEAD..@{u}").stdout.split("\n")
                notes = [l for l in log if l.strip()][:10]
                self.found.emit(latest, "\n".join(notes), manual)
            else:
                self.current.emit(manual)
        except Exception as e:      # offline, git missing, timeout...
            self.failed.emit(f"Couldn't check for updates:\n{e}", manual)
        finally:
            self.busy = False

    def install(self):
        src = source_dir()
        self.busy = True

        def run():
            try:
                r = subprocess.run([os.path.join(src, "update.sh")], capture_output=True, text=True,
                                   timeout=900, env=dict(os.environ, GIT_TERMINAL_PROMPT="0"))
                self.installed.emit(r.returncode == 0, (r.stdout + r.stderr).strip())
            except Exception as e:
                self.installed.emit(False, str(e))
            finally:
                self.busy = False
        threading.Thread(target=run, daemon=True).start()


class TextBox:
    """A text box being typed into. Laid out in IMAGE pixels against a QImage
    paint device, and previewed by rendering exactly what will be stamped."""
    _metrics = QImage(1, 1, QImage.Format.Format_ARGB32_Premultiplied)

    def __init__(self, rect):
        self.rect = QRect(rect)
        self.min_h = rect.height()
        self.doc = QTextDocument()
        self.doc.setDocumentMargin(2)
        self.doc.documentLayout().setPaintDevice(self._metrics)
        self.cursor = QTextCursor(self.doc)

    def layout(self, font):
        self.doc.setDefaultFont(font)
        self.doc.setTextWidth(self.rect.width())
        self.rect.setHeight(max(self.min_h, math.ceil(self.doc.size().height())))

    def hit(self, local):
        pos = self.doc.documentLayout().hitTest(QPointF(local), Qt.HitTestAccuracy.FuzzyHit)
        return pos if pos >= 0 else self.doc.characterCount() - 1

    def render(self, fg, bg, caret=False, highlight=None):
        img = QImage(self.rect.size(), QImage.Format.Format_ARGB32_Premultiplied)
        img.fill(bg if bg is not None else Qt.GlobalColor.transparent)
        p = QPainter(img)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setRenderHint(QPainter.RenderHint.TextAntialiasing)
        ctx = QAbstractTextDocumentLayout.PaintContext()
        pal = QPalette(ctx.palette)
        pal.setColor(QPalette.ColorRole.Text, fg)
        ctx.palette = pal
        if caret:
            ctx.cursorPosition = self.cursor.position()
            if self.cursor.hasSelection() and highlight is not None:
                sel = QAbstractTextDocumentLayout.Selection()
                sel.cursor = QTextCursor(self.cursor)
                fmt = QTextCharFormat()
                fmt.setBackground(highlight[0])
                fmt.setForeground(highlight[1])
                sel.format = fmt
                ctx.selections = [sel]
        self.doc.documentLayout().draw(p, ctx)
        p.end()
        return img


class Canvas(QWidget):
    docChanged = pyqtSignal()
    status = pyqtSignal(str, str)          # cursor position, selection size
    colorsChanged = pyqtSignal()
    toolChanged = pyqtSignal(str)

    def __init__(self, scroll):
        super().__init__()
        self.scroll = scroll
        self.image = QImage(DEFAULT_SIZE, RGB32)
        self.image.fill(Qt.GlobalColor.white)
        self.zoom = 1.0
        self.sel = None            # QRect, image coords
        self.floating = None       # lifted/pasted pixels (unscaled original)
        self.transparent = False   # Color 2 pixels in a selection are see-through
        self._disp_key = None
        self._disp = None
        self.drag = None
        self.undo_stack, self.redo_stack = [], []
        self.modified = False
        self.pristine = True       # untouched new canvas: first paste fits to it
        self.tool, self.prev_tool = "pencil", "pencil"
        self.sizes = {k: v[3] for k, v in TOOLS.items()}
        self.color1, self.color2 = QColor("#000000"), QColor("#ffffff")
        self._size_dpr = None
        self._wheel = 0
        self.workspace = QColor("#808080")
        self.rot = None             # {"src", "angle", "key"}: free-rotation state of the selection
        self.text = None            # TextBox being typed into
        self.text_style = {"family": "Sans Serif", "pt": 12, "bold": False, "italic": False,
                           "underline": False, "opaque": False}
        self.setMouseTracking(True)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setAttribute(Qt.WidgetAttribute.WA_OpaquePaintEvent)
        self.update_size()
        self.update_cursor()

    # ---- geometry ---------------------------------------------------
    def dpr(self):
        return self.devicePixelRatioF() or 1.0

    def scale(self):
        return self.zoom / self.dpr()

    def to_img(self, pos):
        s = self.scale()
        return QPointF((pos.x() - MARGIN) / s, (pos.y() - MARGIN) / s)

    def to_widget(self, r):
        s = self.scale()
        r = QRectF(r)
        return QRectF(MARGIN + r.x() * s, MARGIN + r.y() * s, r.width() * s, r.height() * s)

    def update_size(self):
        s = self.scale()
        w, h = self.image.width(), self.image.height()
        if self.drag and self.drag["kind"] == "canvas":
            w, h = max(w, self.drag["size"].width()), max(h, self.drag["size"].height())
        self.resize(math.ceil(2 * MARGIN + w * s), math.ceil(2 * MARGIN + h * s))
        self._size_dpr = self.dpr()
        self.update()

    def visible_origin(self):
        p = self.to_img(QPointF(self.scroll.horizontalScrollBar().value(),
                                self.scroll.verticalScrollBar().value()))
        return QPoint(max(0, int(p.x())), max(0, int(p.y())))

    def set_zoom(self, z, anchor=None):
        z = max(ZOOMS[0], min(ZOOMS[-1], z))
        vp = self.scroll.viewport()
        if anchor is None:
            anchor = QPointF(vp.width() / 2, vp.height() / 2)
        hb, vb = self.scroll.horizontalScrollBar(), self.scroll.verticalScrollBar()
        ip = self.to_img(QPointF(anchor.x() + hb.value(), anchor.y() + vb.value()))
        self.zoom = z
        self.update_size()
        s = self.scale()
        hb.setValue(round(MARGIN + ip.x() * s - anchor.x()))
        vb.setValue(round(MARGIN + ip.y() * s - anchor.y()))
        self.docChanged.emit()

    def zoom_step(self, d, anchor=None):
        if d > 0:
            nxt = [z for z in ZOOMS if z > self.zoom + 1e-9]
            self.set_zoom(nxt[0] if nxt else ZOOMS[-1], anchor)
        else:
            nxt = [z for z in ZOOMS if z < self.zoom - 1e-9]
            self.set_zoom(nxt[-1] if nxt else ZOOMS[0], anchor)

    def zoom_fit(self):
        vp = self.scroll.viewport()
        fit = min((vp.width() - 2 * MARGIN) / self.image.width(),
                  (vp.height() - 2 * MARGIN) / self.image.height()) * self.dpr()
        self.set_zoom(min(1.0, fit))

    # ---- undo -------------------------------------------------------
    def push_undo(self):
        self.undo_stack.append(QImage(self.image))     # shallow; detaches on write
        self.redo_stack.clear()
        total = 0
        for i in range(len(self.undo_stack) - 1, -1, -1):
            total += self.undo_stack[i].sizeInBytes()
            if total > UNDO_BYTES or len(self.undo_stack) - i > UNDO_STEPS:
                del self.undo_stack[:i + 1]
                break
        self.touch()

    def touch(self):
        self.modified = True
        self.pristine = False
        self.docChanged.emit()

    def undo(self):
        if self.drag:
            return
        self.commit()
        if not self.undo_stack:
            return
        self.redo_stack.append(self.image)
        self.image = self.undo_stack.pop()
        self.after_history()

    def redo(self):
        if self.drag:
            return
        self.commit()
        if not self.redo_stack:
            return
        self.undo_stack.append(self.image)
        self.image = self.redo_stack.pop()
        self.after_history()

    def after_history(self):
        self.sel = self.floating = None
        self.modified = True
        self.update_size()
        self.docChanged.emit()

    def set_image(self, img):
        self.image = opaque(img)
        self.sel = self.floating = self.drag = None
        self.undo_stack.clear()
        self.redo_stack.clear()
        self.modified = False
        self.pristine = False
        self.update_size()
        self.docChanged.emit()

    # ---- selection --------------------------------------------------
    def masked(self, f):
        """f with Color 2 see-through when Transparent selection is on (cached)."""
        if not self.transparent:
            return f
        key = (f.cacheKey(), self.color2.rgb())
        if key != self._disp_key:
            self._disp_key = key
            img = f.convertToFormat(QImage.Format.Format_ARGB32_Premultiplied)
            mask = f.convertToFormat(QImage.Format.Format_ARGB32).createMaskFromColor(
                self.color2.rgb(), Qt.MaskMode.MaskOutColor)
            mask.setColorTable([0x00000000, 0xFFFFFFFF])
            p = QPainter(img)
            p.setCompositionMode(QPainter.CompositionMode.CompositionMode_DestinationIn)
            p.drawImage(0, 0, mask.convertToFormat(QImage.Format.Format_ARGB32))
            p.end()
            self._disp = img
        return self._disp

    def draw_floating(self, p):
        p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform,
                        self.sel.size() != self.floating.size())
        p.drawImage(QRectF(self.sel), self.masked(self.floating), QRectF(self.floating.rect()))

    # ---- free rotation ---------------------------------------------
    def rot_knob(self):
        """Widget position of the selection's rotation knob (above it, or below
        when the selection hugs the top of the view)."""
        r = self.to_widget(self.sel)
        y = r.top() - ROT_OFFSET
        if y < HANDLE:
            y = r.bottom() + ROT_OFFSET
        return QPointF(r.center().x(), y)

    def begin_rotate(self, ip):
        self.lift()
        if (not self.rot or self.rot["key"] != self.floating.cacheKey()
                or self.rot["size"] != self.sel.size()):
            # rotate from an unrotated source each time, so repeated turns don't blur
            src = self.floating
            if src.size() != self.sel.size():
                src = src.scaled(self.sel.size(), Qt.AspectRatioMode.IgnoreAspectRatio,
                                 Qt.TransformationMode.SmoothTransformation)
            self.rot = {"src": src.convertToFormat(QImage.Format.Format_ARGB32_Premultiplied),
                        "angle": 0.0, "key": None, "size": None}
        c = QRectF(self.sel).center()
        return {"kind": "rotate_sel", "center": c, "base": self.rot["angle"], "angle": self.rot["angle"],
                "start": math.degrees(math.atan2(ip.y() - c.y(), ip.x() - c.x()))}

    def draw_rotating(self, p):
        d, src = self.drag, self.rot["src"]
        p.save()
        p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        p.translate(d["center"])
        p.rotate(d["angle"])
        p.drawImage(QPointF(-src.width() / 2, -src.height() / 2), self.masked(src))
        p.restore()

    def end_rotate(self, d):
        src, ang = self.rot["src"], d["angle"]
        out = src if ang % 360 == 0 else src.transformed(QTransform().rotate(ang),
                                                          Qt.TransformationMode.SmoothTransformation)
        c = d["center"]
        self.sel = QRect(round(c.x() - out.width() / 2), round(c.y() - out.height() / 2),
                         out.width(), out.height())
        self.floating = out
        self.rot.update(angle=ang, key=out.cacheKey(), size=self.sel.size())
        self.emit_sel()

    # ---- text --------------------------------------------------------
    def text_font(self):
        st = self.text_style
        f = QFont(st["family"])
        f.setPixelSize(max(1, round(st["pt"] * 96 / 72)))     # Paint's points at 96 dpi
        f.setBold(st["bold"])
        f.setItalic(st["italic"])
        f.setUnderline(st["underline"])
        return f

    def set_text_style(self, **kw):
        self.text_style.update(kw)
        if self.text:
            self.text.layout(self.text_font())
            self.update()
            self.setFocus()

    def text_image(self, caret):
        pal = self.palette()
        hl = (pal.color(QPalette.ColorRole.Highlight), pal.color(QPalette.ColorRole.HighlightedText))
        self.text.layout(self.text_font())
        return self.text.render(self.color1, self.color2 if self.text_style["opaque"] else None,
                                caret, hl)

    def new_text(self, rect):
        tb = TextBox(rect)
        tb.min_h = 0
        tb.layout(self.text_font())                 # height of one empty line
        tb.min_h = max(rect.height(), tb.rect.height())
        tb.layout(self.text_font())
        self.text = tb
        self.setFocus()
        self.update()

    def commit_text(self):
        if not self.text:
            return
        if self.text.doc.toPlainText().strip():
            img = self.text_image(caret=False)
            self.push_undo()
            p = QPainter(self.image)
            p.drawImage(self.text.rect.topLeft(), img)
            p.end()
        self.text = None
        self.update()
        self.update_cursor()

    def text_key(self, e):
        """Typing into the active text box. Returns False if the key isn't ours."""
        cur, k, mods = self.text.cursor, e.key(), e.modifiers()
        ctrl = bool(mods & Qt.KeyboardModifier.ControlModifier)
        mode = (QTextCursor.MoveMode.KeepAnchor if mods & Qt.KeyboardModifier.ShiftModifier
                else QTextCursor.MoveMode.MoveAnchor)
        Op = QTextCursor.MoveOperation
        moves = {Qt.Key.Key_Left: Op.WordLeft if ctrl else Op.Left,
                 Qt.Key.Key_Right: Op.WordRight if ctrl else Op.Right,
                 Qt.Key.Key_Up: Op.Up, Qt.Key.Key_Down: Op.Down,
                 Qt.Key.Key_Home: Op.Start if ctrl else Op.StartOfLine,
                 Qt.Key.Key_End: Op.End if ctrl else Op.EndOfLine}
        cb = QApplication.clipboard()
        if k in moves:
            cur.movePosition(moves[k], mode)
        elif k == Qt.Key.Key_Backspace:
            cur.deletePreviousChar()
        elif k == Qt.Key.Key_Delete:
            cur.deleteChar()
        elif k in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            cur.insertBlock()
        elif k == Qt.Key.Key_Escape:
            self.commit_text()
            return True
        elif ctrl and k == Qt.Key.Key_A:
            cur.select(QTextCursor.SelectionType.Document)
        elif ctrl and k in (Qt.Key.Key_C, Qt.Key.Key_X):
            if cur.hasSelection():
                cb.setText(cur.selectedText().replace("\u2029", "\n"))
                if k == Qt.Key.Key_X:
                    cur.removeSelectedText()
        elif ctrl and k == Qt.Key.Key_V:
            cur.insertText(cb.text())
        elif ctrl and k == Qt.Key.Key_Z:
            self.text.doc.undo(cur)
        elif ctrl and k == Qt.Key.Key_Y:
            self.text.doc.redo(cur)
        elif e.text() and e.text().isprintable() and not ctrl:
            cur.insertText(e.text())
        else:
            return False
        self.update()
        return True

    def event(self, e):
        # While typing, keys like S/P/Del/Ctrl+V belong to the text box, not to
        # the window's tool and edit shortcuts.
        if e.type() == e.Type.ShortcutOverride and self.text is not None:
            mods = e.modifiers() & ~Qt.KeyboardModifier.ShiftModifier & ~Qt.KeyboardModifier.KeypadModifier
            plain = mods == Qt.KeyboardModifier.NoModifier
            ctrl_keys = (Qt.Key.Key_A, Qt.Key.Key_C, Qt.Key.Key_X, Qt.Key.Key_V, Qt.Key.Key_Z,
                         Qt.Key.Key_Y, Qt.Key.Key_Left, Qt.Key.Key_Right, Qt.Key.Key_Home, Qt.Key.Key_End)
            if (plain and (e.text().isprintable() and e.text()
                           or e.key() in (Qt.Key.Key_Backspace, Qt.Key.Key_Delete, Qt.Key.Key_Escape,
                                          Qt.Key.Key_Return, Qt.Key.Key_Enter, Qt.Key.Key_Left,
                                          Qt.Key.Key_Right, Qt.Key.Key_Up, Qt.Key.Key_Down,
                                          Qt.Key.Key_Home, Qt.Key.Key_End))
                    or (mods == Qt.KeyboardModifier.ControlModifier and e.key() in ctrl_keys)):
                e.accept()
                return True
        return super().event(e)

    def lift(self, leave_hole=True):
        if not self.sel or self.floating is not None:
            return
        self.push_undo()
        self.sel = self.sel.intersected(self.image.rect())
        self.floating = self.image.copy(self.sel)
        if leave_hole:
            p = QPainter(self.image)
            p.fillRect(self.sel, self.color2)
            p.end()

    def stamp(self):
        p = QPainter(self.image)
        self.draw_floating(p)
        p.end()

    def commit(self):
        self.commit_text()
        if self.drag and self.drag["kind"] in ("move_sel", "resize_sel", "select_new", "rotate_sel"):
            self.drag = None
        self.rot = None
        if self.floating is not None:
            self.stamp()
            self.touch()
        self.sel = self.floating = None
        self.update()
        self.emit_sel()

    def select_all(self):
        self.commit()
        self.set_tool("select")
        self.sel = QRect(self.image.rect())
        self.update()
        self.emit_sel()

    def selection_image(self):
        if self.floating is not None:
            if self.sel.size() == self.floating.size() and not self.transparent:
                return QImage(self.floating)
            img = QImage(self.sel.size(), QImage.Format.Format_ARGB32_Premultiplied)
            img.fill(Qt.GlobalColor.transparent)
            p = QPainter(img)
            p.translate(-self.sel.x(), -self.sel.y())
            self.draw_floating(p)
            p.end()
            return img
        if self.sel:
            return self.image.copy(self.sel)
        return None

    def delete_selection(self):
        if self.floating is not None:
            self.floating = self.sel = None
            self.touch()
        elif self.sel:
            self.push_undo()
            p = QPainter(self.image)
            p.fillRect(self.sel, self.color2)
            p.end()
            self.sel = None
        self.update()
        self.emit_sel()

    def paste_image(self, img):
        img = QImage(img)
        img.setDevicePixelRatio(1)
        if img.hasAlphaChannel():
            img = img.convertToFormat(QImage.Format.Format_ARGB32_Premultiplied)
        else:
            img = img.convertToFormat(RGB32)
        self.commit()
        fresh = self.pristine and not self.undo_stack
        self.push_undo()
        W, H = self.image.width(), self.image.height()
        if fresh:
            nw, nh, pos = img.width(), img.height(), QPoint(0, 0)
        else:
            pos = self.visible_origin()
            nw, nh = W, H
            if img.width() > W:
                pos.setX(0)
                nw = img.width()
            else:
                pos.setX(min(pos.x(), W - img.width()))
            if img.height() > H:
                pos.setY(0)
                nh = img.height()
            else:
                pos.setY(min(pos.y(), H - img.height()))
        if (nw, nh) != (W, H):
            self.image = self.grown(nw, nh)
        self.set_tool("select")
        self.floating = img
        self.sel = QRect(pos, img.size())
        self.update_size()
        self.emit_sel()
        self.setFocus()

    def crop(self):
        img = self.selection_image()
        if img is None:
            return
        if self.floating is None:
            self.push_undo()
        self.image = opaque(img)
        self.sel = self.floating = None
        self.touch()
        self.update_size()
        self.emit_sel()

    def transform(self, rotate=0, flip=None):
        def apply(img):
            if rotate:
                return img.transformed(QTransform().rotate(rotate))
            return flip_image(img, flip == "h")
        if self.sel:
            self.lift()
            c = QRectF(self.sel).center()
            self.floating = apply(self.floating)
            if rotate % 180:
                sz = QSize(self.sel.height(), self.sel.width())
                self.sel = QRect(QPoint(round(c.x() - sz.width() / 2), round(c.y() - sz.height() / 2)), sz)
            self.emit_sel()
        else:
            self.commit()
            self.push_undo()
            self.image = opaque(apply(self.image))
            self.update_size()
        self.update()

    def grown(self, w, h):
        out = QImage(max(1, w), max(1, h), RGB32)
        out.fill(self.color2)
        p = QPainter(out)
        p.drawImage(0, 0, self.image)
        p.end()
        return out

    def resize_canvas(self, w, h):
        self.commit()
        if (w, h) == (self.image.width(), self.image.height()):
            return
        self.push_undo()
        self.image = self.grown(w, h)
        self.update_size()

    def nudge(self, dx, dy):
        if not self.sel:
            return False
        self.lift()
        self.sel.translate(dx, dy)
        self.update()
        return True

    def emit_sel(self):
        s = f"{self.sel.width()} × {self.sel.height()}px" if self.sel else ""
        self.status.emit("", s)

    # ---- tools ------------------------------------------------------
    def set_tool(self, t):
        if t == self.tool:
            return
        self.commit_text()
        if t != "select":
            self.commit()
        if t == "picker":
            self.prev_tool = self.tool
        self.tool = t
        self.update_cursor()
        self.toolChanged.emit(t)

    RESIZE_CURSORS = {"n": Qt.CursorShape.SizeVerCursor, "s": Qt.CursorShape.SizeVerCursor,
                      "e": Qt.CursorShape.SizeHorCursor, "w": Qt.CursorShape.SizeHorCursor,
                      "nw": Qt.CursorShape.SizeFDiagCursor, "se": Qt.CursorShape.SizeFDiagCursor,
                      "ne": Qt.CursorShape.SizeBDiagCursor, "sw": Qt.CursorShape.SizeBDiagCursor}

    def update_cursor(self, pos=None):
        c = Qt.CursorShape.IBeamCursor if self.tool == "text" else Qt.CursorShape.CrossCursor
        if pos is not None:
            h = self.canvas_handle_at(pos)
            if h:
                c = self.RESIZE_CURSORS[h]
            elif self.tool == "select" and self.sel and self.on_rot_knob(pos):
                c = Qt.CursorShape.PointingHandCursor
            elif self.tool == "select" and self.sel and self.handle_at(self.sel, pos):
                c = self.RESIZE_CURSORS[self.handle_at(self.sel, pos)]
            elif self.tool == "select" and self.sel and QRectF(self.sel).contains(self.to_img(pos)):
                c = Qt.CursorShape.SizeAllCursor
            elif self.text:
                h = self.handle_at(self.text.rect, pos)
                if h:
                    c = self.RESIZE_CURSORS[h]
                elif self.on_text_border(pos):
                    c = Qt.CursorShape.SizeAllCursor
        self.setCursor(c)

    def canvas_handles(self):
        """Win7 Paint's three canvas handles: right edge, bottom edge, corner.
        Each sits just outside the image, centred on its edge."""
        r = self.to_widget(self.image.rect())
        k = CANVAS_HANDLE
        return {"e": QRectF(r.right() + 1, r.center().y() - k / 2, k, k),
                "s": QRectF(r.center().x() - k / 2, r.bottom() + 1, k, k),
                "se": QRectF(r.right() + 1, r.bottom() + 1, k, k)}

    def canvas_handle_at(self, pos):
        for name, box in self.canvas_handles().items():
            if box.adjusted(-3, -3, 3, 3).contains(pos):
                return name
        return None

    def handles(self, rect):
        r = self.to_widget(rect)
        cx, cy = r.center().x(), r.center().y()
        return {"nw": QPointF(r.left(), r.top()), "n": QPointF(cx, r.top()),
                "ne": QPointF(r.right(), r.top()), "e": QPointF(r.right(), cy),
                "se": QPointF(r.right(), r.bottom()), "s": QPointF(cx, r.bottom()),
                "sw": QPointF(r.left(), r.bottom()), "w": QPointF(r.left(), cy)}

    def handle_at(self, rect, pos):
        for name, pt in self.handles(rect).items():
            if abs(pos.x() - pt.x()) <= HANDLE and abs(pos.y() - pt.y()) <= HANDLE:
                return name
        return None

    def sel_handle_at(self, pos):
        return self.handle_at(self.sel, pos) if self.sel else None

    def on_rot_knob(self, pos):
        k = self.rot_knob()
        return math.hypot(pos.x() - k.x(), pos.y() - k.y()) <= HANDLE + 2

    def on_text_border(self, pos):
        """The text box's dashed edge is its move grip (inside is for the caret)."""
        r = self.to_widget(self.text.rect)
        return r.adjusted(-5, -5, 5, 5).contains(pos) and not r.adjusted(3, 3, -3, -3).contains(pos)

    def resized_rect(self, o, h, ip, keep_aspect=False):
        """o with the edges named by handle h dragged to image point ip."""
        l, t, r, b = o.left(), o.top(), o.left() + o.width(), o.top() + o.height()
        if "w" in h:
            l = min(round(ip.x()), r - 1)
        if "e" in h:
            r = max(round(ip.x()), l + 1)
        if "n" in h:
            t = min(round(ip.y()), b - 1)
        if "s" in h:
            b = max(round(ip.y()), t + 1)
        if keep_aspect and len(h) == 2:
            k_ = max((r - l) / o.width(), (b - t) / o.height())
            nw, nh = max(1, round(o.width() * k_)), max(1, round(o.height() * k_))
            if "w" in h:
                l = r - nw
            else:
                r = l + nw
            if "n" in h:
                t = b - nh
            else:
                b = t + nh
        return QRect(l, t, r - l, b - t)

    def pen_for(self, color, tool):
        size = self.sizes[tool]
        pen = QPen(color, size)
        if tool in ("brush", "line"):
            pen.setCapStyle(Qt.PenCapStyle.RoundCap)
            pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
        else:
            pen.setCapStyle(Qt.PenCapStyle.SquareCap)
            pen.setJoinStyle(Qt.PenJoinStyle.MiterJoin)
        return pen

    def draw_segment(self, a, b):
        t = self.drag["tool"]
        p = QPainter(self.image)
        p.setPen(self.pen_for(self.drag["color"], t))
        if t == "brush":
            p.setRenderHint(QPainter.RenderHint.Antialiasing)
            pa, pb = QPointF(a.x(), a.y()), QPointF(b.x(), b.y())
            if pa == pb:
                p.drawPoint(pa)
            else:
                p.drawLine(pa, pb)
        else:
            ia = QPointF(math.floor(a.x()) + 0.5, math.floor(a.y()) + 0.5)
            ib = QPointF(math.floor(b.x()) + 0.5, math.floor(b.y()) + 0.5)
            if t == "eraser":
                # square eraser, pixel-aligned like Paint's
                sz = self.sizes["eraser"]
                p.setPen(Qt.PenStyle.NoPen)
                steps = max(1, int(max(abs(ib.x() - ia.x()), abs(ib.y() - ia.y()))))
                for i in range(steps + 1):
                    k = i / steps
                    x = math.floor(ia.x() + (ib.x() - ia.x()) * k) - sz // 2
                    y = math.floor(ia.y() + (ib.y() - ia.y()) * k) - sz // 2
                    p.fillRect(QRect(x, y, sz, sz), self.drag["color"])
            elif ia == ib:
                p.drawPoint(ia)
            else:
                p.drawLine(ia, ib)
        p.end()
        sz = self.sizes[t] + 2
        dirty = QRectF(a, b).normalized().adjusted(-sz, -sz, sz, sz)
        self.update(self.to_widget(dirty).toAlignedRect().adjusted(-2, -2, 2, 2))

    def shape_points(self):
        d = self.drag
        a, b = d["start"], d["end"]
        if d["shift"]:
            dx, dy = b.x() - a.x(), b.y() - a.y()
            if d["tool"] == "rect":
                m = max(abs(dx), abs(dy))
                b = QPointF(a.x() + math.copysign(m, dx), a.y() + math.copysign(m, dy))
            else:
                ang = round(math.atan2(dy, dx) / (math.pi / 4)) * (math.pi / 4)
                ln = math.hypot(dx, dy)
                b = QPointF(a.x() + ln * math.cos(ang), a.y() + ln * math.sin(ang))
        snap = lambda q: QPointF(math.floor(q.x()) + 0.5, math.floor(q.y()) + 0.5)
        return snap(a), snap(b)

    def draw_shape(self, p):
        d = self.drag
        a, b = self.shape_points()
        p.setPen(self.pen_for(d["color"], d["tool"]))
        p.setBrush(Qt.BrushStyle.NoBrush)
        if d["tool"] == "line":
            p.setRenderHint(QPainter.RenderHint.Antialiasing)
            p.drawLine(a, b)
        else:
            p.setRenderHint(QPainter.RenderHint.Antialiasing, False)
            p.drawRect(QRectF(a, b).normalized())

    # ---- events -----------------------------------------------------
    def mousePressEvent(self, e):
        pos, btn = e.position(), e.button()
        if self.drag:
            return
        if btn == Qt.MouseButton.MiddleButton:
            self.drag = {"kind": "pan", "start": e.globalPosition(),
                         "h": self.scroll.horizontalScrollBar().value(),
                         "v": self.scroll.verticalScrollBar().value()}
            self.setCursor(Qt.CursorShape.ClosedHandCursor)
            return
        if btn not in (Qt.MouseButton.LeftButton, Qt.MouseButton.RightButton):
            return
        ip = self.to_img(pos)
        color = self.color1 if btn == Qt.MouseButton.LeftButton else self.color2
        ctrl = bool(e.modifiers() & Qt.KeyboardModifier.ControlModifier)
        if btn == Qt.MouseButton.LeftButton and self.canvas_handle_at(pos):
            self.commit()
            self.drag = {"kind": "canvas", "which": self.canvas_handle_at(pos),
                         "size": self.image.size()}
            return
        t = self.tool
        if t == "select":
            if btn == Qt.MouseButton.RightButton:
                self.window().context_menu(e.globalPosition().toPoint())
                return
            h = self.sel_handle_at(pos)
            if self.sel and self.on_rot_knob(pos):
                self.drag = self.begin_rotate(ip)
            elif h:
                self.drag = {"kind": "resize_sel", "h": h, "orig": QRect(self.sel), "moved": False}
            elif self.sel and QRectF(self.sel).contains(ip):
                self.drag = {"kind": "move_sel", "start": ip, "orig": QRect(self.sel),
                             "moved": False, "ctrl": ctrl}
            else:
                self.commit()
                self.drag = {"kind": "select_new", "start": ip}
        elif t in ("pencil", "brush", "eraser"):
            self.push_undo()
            self.drag = {"kind": "stroke", "tool": t, "color": color if t != "eraser" else self.color2,
                         "last": ip}
            self.draw_segment(ip, ip)
        elif t == "fill":
            x, y = int(math.floor(ip.x())), int(math.floor(ip.y()))
            if self.image.rect().contains(x, y):
                out = flood_fill(self.image, x, y, color)
                if out is not None:
                    self.push_undo()
                    self.image = out
                    self.update()
        elif t == "picker":
            x, y = int(math.floor(ip.x())), int(math.floor(ip.y()))
            if self.image.rect().contains(x, y):
                c = self.image.pixelColor(x, y)
                if btn == Qt.MouseButton.LeftButton:
                    self.color1 = c
                else:
                    self.color2 = c
                self.colorsChanged.emit()
            self.set_tool(self.prev_tool if self.prev_tool != "picker" else "pencil")
        elif t in ("line", "rect"):
            self.drag = {"kind": "shape", "tool": t, "color": color, "start": ip, "end": ip,
                         "shift": bool(e.modifiers() & Qt.KeyboardModifier.ShiftModifier)}
        elif t == "text" and btn == Qt.MouseButton.LeftButton:
            tb = self.text
            h = self.handle_at(tb.rect, pos) if tb else None
            if h:
                self.drag = {"kind": "resize_text", "h": h, "orig": QRect(tb.rect)}
            elif tb and self.on_text_border(pos):
                self.drag = {"kind": "move_text", "start": ip, "orig": QRect(tb.rect)}
            elif tb and QRectF(tb.rect).contains(ip):
                mode = (QTextCursor.MoveMode.KeepAnchor if e.modifiers() & Qt.KeyboardModifier.ShiftModifier
                        else QTextCursor.MoveMode.MoveAnchor)
                tb.cursor.setPosition(tb.hit(ip - QPointF(tb.rect.topLeft())), mode)
                self.drag = {"kind": "text_select"}
                self.update()
            else:
                self.commit_text()
                self.drag = {"kind": "text_new", "start": ip, "rect": None}

    def mouseMoveEvent(self, e):
        pos = e.position()
        ip = self.to_img(pos)
        x, y = math.floor(ip.x()), math.floor(ip.y())
        inside = self.image.rect().contains(x, y)
        self.status.emit(f"{x}, {y}px" if inside else "", None)
        d = self.drag
        if not d:
            self.update_cursor(pos)
            return
        k = d["kind"]
        if k == "pan":
            delta = e.globalPosition() - d["start"]
            self.scroll.horizontalScrollBar().setValue(round(d["h"] - delta.x()))
            self.scroll.verticalScrollBar().setValue(round(d["v"] - delta.y()))
        elif k == "stroke":
            self.draw_segment(d["last"], ip)
            d["last"] = ip
        elif k == "shape":
            d["end"] = ip
            d["shift"] = bool(e.modifiers() & Qt.KeyboardModifier.ShiftModifier)
            self.update()
        elif k == "select_new":
            W, H = self.image.width(), self.image.height()
            a, b = d["start"], ip
            x0 = max(0, min(W, round(min(a.x(), b.x()))))
            x1 = max(0, min(W, round(max(a.x(), b.x()))))
            y0 = max(0, min(H, round(min(a.y(), b.y()))))
            y1 = max(0, min(H, round(max(a.y(), b.y()))))
            self.sel = QRect(x0, y0, x1 - x0, y1 - y0) if x1 > x0 and y1 > y0 else None
            self.update()
            self.emit_sel()
        elif k == "move_sel":
            dx, dy = round(ip.x() - d["start"].x()), round(ip.y() - d["start"].y())
            if not d["moved"] and (dx or dy):
                d["moved"] = True
                if d["ctrl"] and self.floating is not None:
                    self.stamp()            # Ctrl+drag leaves a copy behind
                elif d["ctrl"]:
                    self.push_undo()
                    self.floating = self.image.copy(self.sel)
                else:
                    self.lift()
            if d["moved"]:
                self.sel = d["orig"].translated(dx, dy)
                self.update()
        elif k == "resize_sel":
            if not d["moved"]:
                d["moved"] = True
                self.lift()
            self.sel = self.resized_rect(d["orig"], d["h"], ip,
                                         bool(e.modifiers() & Qt.KeyboardModifier.ShiftModifier))
            self.update()
            self.emit_sel()
        elif k == "rotate_sel":
            c = d["center"]
            a = d["base"] + math.degrees(math.atan2(ip.y() - c.y(), ip.x() - c.x())) - d["start"]
            if e.modifiers() & Qt.KeyboardModifier.ShiftModifier:
                a = round(a / 15) * 15
            d["angle"] = (a + 180) % 360 - 180
            self.status.emit("", f"Rotate {d['angle']:.0f}°")
            self.update()
        elif k == "text_new":
            a = d["start"]
            d["rect"] = QRect(QPoint(round(min(a.x(), ip.x())), round(min(a.y(), ip.y()))),
                              QPoint(round(max(a.x(), ip.x())) - 1, round(max(a.y(), ip.y())) - 1))
            self.update()
        elif k == "resize_text":
            r = self.resized_rect(d["orig"], d["h"], ip)
            self.text.rect = QRect(r.topLeft(), QSize(max(12, r.width()), r.height()))
            self.text.min_h = max(1, r.height())
            self.text.layout(self.text_font())
            self.update()
        elif k == "move_text":
            self.text.rect.moveTopLeft(d["orig"].topLeft() + QPoint(round(ip.x() - d["start"].x()),
                                                                    round(ip.y() - d["start"].y())))
            self.update()
        elif k == "text_select":
            tb = self.text
            tb.cursor.setPosition(tb.hit(ip - QPointF(tb.rect.topLeft())), QTextCursor.MoveMode.KeepAnchor)
            self.update()
        elif k == "canvas":
            w, h = self.image.width(), self.image.height()
            if "e" in d["which"]:
                w = max(1, round(ip.x()))
            if "s" in d["which"]:
                h = max(1, round(ip.y()))
            d["size"] = QSize(w, h)
            self.status.emit("", f"Canvas {w} × {h}px")
            self.update_size()

    def mouseReleaseEvent(self, e):
        d = self.drag
        if not d:
            return
        if d["kind"] == "pan" and e.button() != Qt.MouseButton.MiddleButton:
            return
        if d["kind"] != "pan" and e.button() == Qt.MouseButton.MiddleButton:
            return
        self.drag = None
        k = d["kind"]
        if k == "shape":
            self.push_undo()
            p = QPainter(self.image)
            self.drag = d
            self.draw_shape(p)
            self.drag = None
            p.end()
        elif k == "canvas":
            self.resize_canvas(d["size"].width(), d["size"].height())
            self.update_size()
            self.emit_sel()
        elif k == "stroke":
            self.docChanged.emit()
        elif k == "rotate_sel":
            self.end_rotate(d)
        elif k == "text_new":
            r = d["rect"]
            if r is None or r.width() < 8:          # a click: a default-width box at that spot
                r = QRect(QPoint(round(d["start"].x()), round(d["start"].y())), QSize(240, 1))
            self.new_text(r)
        self.update()
        self.update_cursor(e.position())

    def contextMenuEvent(self, e):
        e.accept()          # handled on press for the Select tool

    def wheelEvent(self, e):
        if e.modifiers() & Qt.KeyboardModifier.ControlModifier:
            self._wheel += e.angleDelta().y()
            vp_pos = self.mapTo(self.scroll.viewport(), e.position().toPoint())
            while abs(self._wheel) >= 120:
                step = 1 if self._wheel > 0 else -1
                self._wheel -= 120 * step
                self.zoom_step(step, QPointF(vp_pos))
            e.accept()
        else:
            e.ignore()

    def keyPressEvent(self, e):
        if self.text is not None and self.text_key(e):
            return
        k = e.key()
        moves = {Qt.Key.Key_Left: (-1, 0), Qt.Key.Key_Right: (1, 0),
                 Qt.Key.Key_Up: (0, -1), Qt.Key.Key_Down: (0, 1)}
        if k in moves and self.sel and not self.drag:
            self.nudge(*moves[k])
            return
        if k in (Qt.Key.Key_Escape, Qt.Key.Key_Return, Qt.Key.Key_Enter):
            if self.drag and self.drag["kind"] in ("stroke", "shape"):
                if self.drag["kind"] == "stroke" and self.undo_stack:
                    self.image = self.undo_stack.pop()     # cancel the stroke
                self.drag = None
                self.update()
                return
            self.commit()
            return
        super().keyPressEvent(e)

    def paintEvent(self, e):
        if self._size_dpr is not None and abs(self._size_dpr - self.dpr()) > 1e-6:
            QTimer.singleShot(0, self.update_size)      # moved to a screen with other scaling
        p = QPainter(self)
        p.fillRect(e.rect(), self.workspace)
        s = self.scale()
        W, H = self.image.width(), self.image.height()
        d = self.drag
        p.save()
        p.translate(MARGIN, MARGIN)
        p.scale(s, s)
        # Always draw the WHOLE image (clipped by Qt to the exposed area) so the
        # pixel mapping is identical on every repaint: no seams at odd zooms.
        p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, self.zoom < 1)
        p.drawImage(QRectF(0, 0, W, H), self.image)
        p.setClipRect(QRectF(0, 0, W, H))
        if d and d["kind"] == "rotate_sel":
            self.draw_rotating(p)
        elif self.floating is not None:
            self.draw_floating(p)
        if d and d["kind"] == "shape":
            self.draw_shape(p)
        if self.text:
            p.drawImage(QPointF(self.text.rect.topLeft()), self.text_image(caret=self.hasFocus()))
        p.restore()

        # ---- overlays, in widget coordinates
        p.setRenderHint(QPainter.RenderHint.Antialiasing, False)
        blue = QColor("#1e5fd8")
        r = self.to_widget(self.image.rect())
        p.setPen(QPen(QColor(128, 128, 128, 160), 0))    # canvas edge, visible on any theme
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawRect(r.adjusted(-0.5, -0.5, 0.5, 0.5))     # just outside: never covers a pixel

        def dashed(rect):
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.setPen(QPen(QColor("white"), 0))
            p.drawRect(rect)
            p.setPen(QPen(blue, 0, Qt.PenStyle.DashLine))
            p.drawRect(rect)

        def grips(rect):
            p.setPen(QPen(blue, 0))
            p.setBrush(QColor("white"))
            for pt in self.handles(rect).values():
                p.drawRect(QRectF(pt.x() - HANDLE / 2, pt.y() - HANDLE / 2, HANDLE, HANDLE))

        if d and d["kind"] == "rotate_sel":
            src = self.rot["src"]
            t = QTransform().translate(d["center"].x(), d["center"].y()).rotate(d["angle"])
            poly = t.map(QPolygonF(QRectF(-src.width() / 2, -src.height() / 2, src.width(), src.height())))
            poly = QTransform().translate(MARGIN, MARGIN).scale(s, s).map(poly)
            p.setRenderHint(QPainter.RenderHint.Antialiasing)
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.setPen(QPen(QColor("white"), 0))
            p.drawPolygon(poly)
            p.setPen(QPen(blue, 0, Qt.PenStyle.DashLine))
            p.drawPolygon(poly)
            p.setRenderHint(QPainter.RenderHint.Antialiasing, False)
        elif self.sel:
            dashed(self.to_widget(self.sel).adjusted(-0.5, -0.5, 0.5, 0.5))
            if not (d and d["kind"] == "select_new"):
                grips(self.sel)
                if self.tool == "select":                  # free-rotation knob on a stem
                    k = self.rot_knob()
                    sr = self.to_widget(self.sel)
                    edge = sr.top() if k.y() < sr.top() else sr.bottom()
                    p.setRenderHint(QPainter.RenderHint.Antialiasing)
                    p.setPen(QPen(blue, 0))
                    p.drawLine(QPointF(k.x(), edge), k)
                    p.setBrush(QColor("white"))
                    p.drawEllipse(k, HANDLE - 1, HANDLE - 1)
                    p.setBrush(blue)
                    p.drawEllipse(k, 1.5, 1.5)
                    p.setRenderHint(QPainter.RenderHint.Antialiasing, False)
        if self.text:
            dashed(self.to_widget(self.text.rect).adjusted(-0.5, -0.5, 0.5, 0.5))
            grips(self.text.rect)
        if d and d["kind"] == "text_new" and d["rect"] is not None:
            dashed(self.to_widget(d["rect"]))

        # canvas resize handles: big, light, outlined, so they read on dark themes too
        p.setPen(QPen(QColor("#2b4f8c"), 0))
        p.setBrush(QColor("white"))
        for box in self.canvas_handles().values():
            p.drawRect(box)
        if d and d["kind"] == "canvas":
            sz = d["size"]
            dashed(self.to_widget(QRect(QPoint(0, 0), sz)))
        p.end()


class PaletteGrid(QWidget):
    picked = pyqtSignal(QColor, bool)       # color, is_right_button
    CELL, GAP = 18, 3

    def __init__(self):
        super().__init__()
        self.custom = [None] * 10
        n = self.CELL + self.GAP
        self.setFixedSize(10 * n, 3 * n)
        self.setToolTip("Left click: set the selected color slot\nRight click: set Color 2")

    def colors(self):
        return [QColor(c) for c in PALETTE] + self.custom

    def paintEvent(self, e):
        p = QPainter(self)
        n = self.CELL + self.GAP
        for i, c in enumerate(self.colors()):
            r = QRect((i % 10) * n + 1, (i // 10) * n + 1, self.CELL, self.CELL)
            p.fillRect(r, c if c is not None else self.palette().color(self.palette().ColorRole.Base))
            p.setPen(QColor("#808080"))
            p.drawRect(r.adjusted(0, 0, -1, -1))
        p.end()

    def mousePressEvent(self, e):
        n = self.CELL + self.GAP
        i = int(e.position().y() // n) * 10 + int(e.position().x() // n)
        cs = self.colors()
        if 0 <= i < len(cs) and cs[i] is not None:
            self.picked.emit(cs[i], e.button() == Qt.MouseButton.RightButton)


class SizePicker(QToolButton):
    """Paint's size dropdown: each size is shown as a dot that big, not a number."""
    picked = pyqtSignal(int)

    def __init__(self):
        super().__init__()
        self.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        self.setAutoRaise(True)
        self.setIconSize(QSize(40, 30))
        self.popup = QMenu(self)
        self.box = QWidget()
        self.lay = QVBoxLayout(self.box)
        self.lay.setContentsMargins(2, 2, 2, 2)
        self.lay.setSpacing(0)
        wa = QWidgetAction(self.popup)
        wa.setDefaultWidget(self.box)
        self.popup.addAction(wa)
        self.setMenu(self.popup)
        self.buttons = []

    def show_sizes(self, sizes, current, square, color):
        for b in self.buttons:
            b.deleteLater()
        self.buttons = []
        for sz in sizes:
            b = QToolButton()
            b.setAutoRaise(True)
            b.setCheckable(True)
            b.setChecked(sz == current)
            b.setIconSize(QSize(64, 30))
            b.setIcon(size_icon(sz, square, 64, 30, color))
            b.setToolTip(f"{sz} px")
            b.clicked.connect(lambda _=False, sz=sz: self.choose(sz))
            self.lay.addWidget(b)
            self.buttons.append(b)
        self.setEnabled(bool(sizes))
        self.setIcon(size_icon(current, square, 40, 30, color) if sizes else QIcon())
        self.setToolTip(f"Size: {current} px" if sizes else "")

    def choose(self, sz):
        self.popup.close()
        self.picked.emit(sz)


class CanvasSizeDialog(QDialog):
    def __init__(self, parent, size):
        super().__init__(parent)
        self.setWindowTitle("Canvas size")
        f = QFormLayout(self)
        self.w, self.h = QSpinBox(), QSpinBox()
        for sb, v in ((self.w, size.width()), (self.h, size.height())):
            sb.setRange(1, 30000)
            sb.setValue(v)
            sb.setSuffix(" px")
        f.addRow("Width", self.w)
        f.addRow("Height", self.h)
        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        f.addRow(bb)


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.settings = QSettings(APP, APP)
        self.path = None
        self.active_slot = 1
        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(False)
        self.scroll.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)
        self.canvas = Canvas(self.scroll)
        self.scroll.setWidget(self.canvas)
        self.apply_workspace_color()
        self.setCentralWidget(self.scroll)
        self.setAcceptDrops(True)
        self.build_actions()
        self.build_menus()
        self.build_toolbar()
        self.build_status()
        self.canvas.docChanged.connect(self.refresh)
        self.canvas.status.connect(self.on_status)
        self.canvas.colorsChanged.connect(self.refresh_colors)
        self.canvas.toolChanged.connect(self.on_tool_changed)
        custom = self.settings.value("custom_colors", [], type=list)
        for i, c in enumerate(custom[:10]):
            self.grid.custom[i] = QColor(c) if c else None
        geo = self.settings.value("geometry")
        if geo is not None:
            self.restoreGeometry(geo)
        else:
            self.resize(1100, 760)
        self.on_tool_changed(self.canvas.tool)
        self.refresh_colors()
        self.refresh()
        self.updater = Updater()
        self.updater.found.connect(self.on_update_found)
        self.updater.current.connect(self.on_up_to_date)
        self.updater.failed.connect(self.on_update_failed)
        self.updater.installed.connect(self.on_update_installed)
        self.skipped_version = None
        self.update_timer = QTimer(self)
        self.update_timer.timeout.connect(lambda: self.updater.check(False))
        self.set_auto_update(self.a_autoupd.isChecked())
        if self.a_autoupd.isChecked():
            QTimer.singleShot(5000, lambda: self.updater.check(False))

    # ---- construction ----------------------------------------------
    def act(self, text, slot, shortcut=None, icons=None, checkable=False):
        a = QAction(text, self)
        if icons:
            a.setIcon(icon(icons))
        if shortcut:
            a.setShortcuts([QKeySequence(s) for s in (shortcut if isinstance(shortcut, list) else [shortcut])])
        a.setCheckable(checkable)
        a.triggered.connect(slot)
        self.addAction(a)
        return a

    def build_actions(self):
        c = self.canvas
        self.a_new = self.act("&New", self.new_file, "Ctrl+N", ["document-new"])
        self.a_open = self.act("&Open…", self.open_file, "Ctrl+O", ["document-open"])
        self.a_save = self.act("&Save", self.save, "Ctrl+S", ["document-save"])
        self.a_saveas = self.act("Save &as…", self.save_as, "Ctrl+Shift+S", ["document-save-as"])
        self.a_quit = self.act("E&xit", self.close, "Ctrl+Q", ["application-exit"])
        self.a_checkupd = self.act("Check for &updates…", lambda: self.updater.check(True), None,
                                   ["update-none", "system-software-update"])
        self.a_autoupd = self.act("&Automatic updates", self.set_auto_update, None, None, True)
        self.a_autoupd.setChecked(self.settings.value("auto_update", True, type=bool))
        self.a_autoupd.setToolTip("Check for new versions at startup and every 6 hours, and ask before installing")
        self.a_about = self.act(f"&About {APP}", self.about, None, ["help-about"])
        self.a_undo = self.act("&Undo", c.undo, "Ctrl+Z", ["edit-undo"])
        self.a_redo = self.act("&Redo", c.redo, ["Ctrl+Y", "Ctrl+Shift+Z"], ["edit-redo"])
        self.a_cut = self.act("Cu&t", self.cut, "Ctrl+X", ["edit-cut"])
        self.a_copy = self.act("&Copy", self.copy, "Ctrl+C", ["edit-copy"])
        self.a_paste = self.act("&Paste", self.paste, "Ctrl+V", ["edit-paste"])
        self.a_paste_file = self.act("Paste &from file…", self.paste_from_file, None, ["document-import"])
        self.a_delete = self.act("&Delete selection", c.delete_selection, "Del", ["edit-delete"])
        self.a_selall = self.act("Select &all", c.select_all, "Ctrl+A", ["edit-select-all"])
        self.a_crop = self.act("C&rop to selection", c.crop, "Ctrl+Shift+X", ["transform-crop", "image-crop"])
        self.a_rot_r = self.act("Rotate right 90°", lambda: c.transform(rotate=90), "Ctrl+R",
                                ["object-rotate-right"])
        self.a_rot_l = self.act("Rotate left 90°", lambda: c.transform(rotate=-90), None, ["object-rotate-left"])
        self.a_rot_180 = self.act("Rotate 180°", lambda: c.transform(rotate=180))
        self.a_flip_h = self.act("Flip horizontal", lambda: c.transform(flip="h"), None, ["object-flip-horizontal"])
        self.a_flip_v = self.act("Flip vertical", lambda: c.transform(flip="v"), None, ["object-flip-vertical"])
        self.a_canvas = self.act("Canvas &size…", self.canvas_size, "Ctrl+E", ["transform-scale", "zoom-fit-best"])
        self.a_crop_short = QAction(self.a_crop.icon(), "Crop", self)
        self.a_crop_short.triggered.connect(c.crop)
        self.a_canvas_short = QAction(self.a_canvas.icon(), "Resize", self)
        self.a_canvas_short.triggered.connect(self.canvas_size)
        self.a_transp = self.act("&Transparent selection", self.toggle_transparent, None, None, True)
        self.a_transp.setToolTip("Color 2 pixels in a pasted/moved selection become see-through")
        self.a_zin = self.act("Zoom &in", lambda: c.zoom_step(1), ["Ctrl++", "Ctrl+=", "Ctrl+PgUp"], ["zoom-in"])
        self.a_zout = self.act("Zoom &out", lambda: c.zoom_step(-1), ["Ctrl+-", "Ctrl+PgDown"], ["zoom-out"])
        self.a_z100 = self.act("&100%", lambda: c.set_zoom(1), "Ctrl+1", ["zoom-original"])
        self.a_zfit = self.act("&Fit to window", c.zoom_fit, "Ctrl+0", ["zoom-fit-best"])
        self.a_swap = self.act("Swap colors", self.swap_colors, "X")
        self.tool_group = QActionGroup(self)
        self.tool_actions = {}
        for tid, (label, icons, key, _, _) in TOOLS.items():
            a = self.act(label, lambda _=False, t=tid: self.canvas.set_tool(t), key, icons, True)
            a.setToolTip(f"{label} ({key})")
            self.tool_group.addAction(a)
            self.tool_actions[tid] = a

    def build_menus(self):
        mb = self.menuBar()
        m = mb.addMenu("&File")
        for a in (self.a_new, self.a_open, None, self.a_save, self.a_saveas, None,
                  self.a_checkupd, self.a_autoupd, None, self.a_quit):
            m.addSeparator() if a is None else m.addAction(a)
        m = mb.addMenu("&Edit")
        for a in (self.a_undo, self.a_redo, None, self.a_cut, self.a_copy, self.a_paste, self.a_paste_file,
                  None, self.a_delete, self.a_selall):
            m.addSeparator() if a is None else m.addAction(a)
        m = mb.addMenu("&Image")
        for a in (self.a_crop, self.a_canvas, self.a_transp, None, self.a_rot_r, self.a_rot_l, self.a_rot_180,
                  self.a_flip_h, self.a_flip_v):
            m.addSeparator() if a is None else m.addAction(a)
        m = mb.addMenu("&View")
        for a in (self.a_zin, self.a_zout, self.a_z100, self.a_zfit):
            m.addAction(a)
        mb.addMenu("&Help").addAction(self.a_about)

    def tool_button(self, action=None, text=None, icons=None, big=False, menu=None):
        b = QToolButton()
        if action is not None:
            b.setDefaultAction(action)
        else:
            b.setText(text)
            b.setIcon(icon(icons or []))
        b.setAutoRaise(True)
        b.setIconSize(QSize(32, 32) if big else QSize(20, 20))
        if b.icon().isNull():
            b.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextOnly)
        elif big:
            b.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextUnderIcon)
        elif action is not None and action.isCheckable():
            b.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonIconOnly)
        else:
            b.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        if menu:
            m = QMenu(b)
            for a in menu:
                m.addSeparator() if a is None else m.addAction(a)
            b.setMenu(m)
            b.setPopupMode(QToolButton.ToolButtonPopupMode.MenuButtonPopup if action is not None
                           else QToolButton.ToolButtonPopupMode.InstantPopup)
        return b

    def ribbon_group(self, tb, caption, body):
        """A Win7-style ribbon group: content on top, a caption underneath."""
        w = QWidget()
        v = QVBoxLayout(w)
        v.setContentsMargins(6, 2, 6, 0)
        v.setSpacing(2)
        v.addWidget(body, 1)
        lab = QLabel(caption)
        lab.setAlignment(Qt.AlignmentFlag.AlignHCenter)
        lab.setEnabled(False)
        v.addWidget(lab)
        return [tb.addWidget(w), tb.addSeparator()]

    def row(self, *widgets, vertical=False, grid_cols=0):
        w = QWidget()
        if grid_cols:
            lay = QGridLayout(w)
            for i, x in enumerate(widgets):
                lay.addWidget(x, i // grid_cols, i % grid_cols)
        else:
            lay = QVBoxLayout(w) if vertical else QHBoxLayout(w)
            for x in widgets:
                lay.addWidget(x)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(1)
        return w

    def build_toolbar(self):
        tb = QToolBar("Ribbon")
        tb.setMovable(False)
        self.addToolBar(tb)
        b = self.tool_button
        self.ribbon_group(tb, "Clipboard", self.row(
            b(self.a_paste, big=True),
            self.row(b(self.a_cut), b(self.a_copy), vertical=True)))
        self.ribbon_group(tb, "Image", self.row(
            b(self.tool_actions["select"], big=True,
              menu=[self.a_selall, self.a_delete, None, self.a_transp]),
            self.row(b(self.a_crop_short), b(self.a_canvas_short),
                     b(text="Rotate", icons=["object-rotate-right"],
                       menu=[self.a_rot_r, self.a_rot_l, self.a_rot_180, self.a_flip_h, self.a_flip_v]),
                     vertical=True)))
        self.ribbon_group(tb, "Tools", self.row(
            *[b(self.tool_actions[t]) for t in ("pencil", "fill", "picker", "text",
                                                 "brush", "eraser", "line", "rect")],
            grid_cols=4))
        self.size_picker = SizePicker()
        self.size_picker.picked.connect(self.on_size)
        self.ribbon_group(tb, "Size", self.row(self.size_picker, vertical=True))
        self.slot_btns = {}
        for slot in (1, 2):
            sb = QToolButton()
            sb.setText(f"Color\n{slot}")
            sb.setCheckable(True)
            sb.setAutoRaise(True)
            sb.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextUnderIcon)
            sb.setIconSize(QSize(30, 30) if slot == 1 else QSize(22, 22))
            sb.clicked.connect(lambda _=False, s=slot: self.set_slot(s))
            self.slot_btns[slot] = sb
        self.grid = PaletteGrid()
        self.grid.picked.connect(self.on_palette)
        edit = b(text="Edit\ncolors", icons=["color-management", "preferences-desktop-color"], big=True)
        edit.clicked.connect(self.edit_colors)
        self.ribbon_group(tb, "Colors", self.row(self.slot_btns[1], self.slot_btns[2], self.grid, edit))
        # Text formatting gets its own row, shown only with the Text tool (like
        # Win7's contextual Text tab), so it never pushes Colors off-screen.
        self.addToolBarBreak()
        self.text_bar = QToolBar("Text")
        self.text_bar.setMovable(False)
        self.addToolBar(self.text_bar)
        self.ribbon_group(self.text_bar, "Text", self.build_text_controls())

    def createPopupMenu(self):
        return None             # no right-click menu for hiding the ribbon rows

    def build_text_controls(self):
        """Win7's contextual Text tab, as a ribbon group shown with the Text tool."""
        st = self.canvas.text_style
        fonts = available_fonts()
        saved = self.settings.value("text_font", "")
        st["family"] = saved if saved in fonts else next(
            (f for f in ("Noto Sans", "DejaVu Sans", "Liberation Sans") if f in fonts), fonts[0])
        st["pt"] = self.settings.value("text_pt", 12, type=int)
        self.font_box = QComboBox()
        self.font_box.addItems(fonts)
        self.font_box.setCurrentText(st["family"])
        for i, f in enumerate(fonts):
            self.font_box.setItemData(i, QFont(f), Qt.ItemDataRole.FontRole)   # preview each font
        self.font_box.currentTextChanged.connect(lambda f: self.text_style(family=f))
        self.pt_box = QComboBox()
        self.pt_box.setEditable(True)
        self.pt_box.setValidator(QIntValidator(4, 500, self))
        self.pt_box.addItems([str(n) for n in FONT_SIZES])
        self.pt_box.setCurrentText(str(st["pt"]))
        self.pt_box.setMaximumWidth(64)
        self.pt_box.currentTextChanged.connect(
            lambda t: t.isdigit() and int(t) >= 4 and self.text_style(pt=int(t)))

        def toggle(key, label, icons, style=None):
            btn = QToolButton()
            btn.setCheckable(True)
            btn.setAutoRaise(True)
            btn.setToolTip(label)
            ic = icon(icons)
            if ic.isNull():
                btn.setText(label[0] if style else label)
                if style:
                    f = btn.font()
                    style(f)
                    btn.setFont(f)
            else:
                btn.setIcon(ic)
            btn.toggled.connect(lambda on: self.text_style(**{key: on}))
            return btn
        bold = toggle("bold", "Bold", ["format-text-bold"], lambda f: f.setBold(True))
        italic = toggle("italic", "Italic", ["format-text-italic"], lambda f: f.setItalic(True))
        under = toggle("underline", "Underline", ["format-text-underline"], lambda f: f.setUnderline(True))
        opaque = toggle("opaque", "Opaque background (Color 2)", ["format-fill-color", "fill-color"])
        opaque.setText("Opaque")
        opaque.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        return self.row(self.font_box, self.pt_box, bold, italic, under, opaque)

    def text_style(self, **kw):
        self.canvas.set_text_style(**kw)
        if "family" in kw:
            self.settings.setValue("text_font", kw["family"])
        if "pt" in kw:
            self.settings.setValue("text_pt", kw["pt"])

    def build_status(self):
        sb = self.statusBar()
        self.l_pos, self.l_sel, self.l_size = QLabel(), QLabel(), QLabel()
        for l in (self.l_pos, self.l_sel, self.l_size):
            l.setMinimumWidth(130)
            sb.addWidget(l)
        self.l_zoom = QLabel()
        self.l_zoom.setMinimumWidth(48)
        self.l_zoom.setAlignment(Qt.AlignmentFlag.AlignCenter)
        for a in (self.a_zout, None, self.a_zin):
            if a is None:
                sb.addPermanentWidget(self.l_zoom)
                continue
            b = QToolButton()
            b.setDefaultAction(a)
            b.setAutoRaise(True)
            sb.addPermanentWidget(b)

    # ---- state sync ------------------------------------------------
    def refresh(self):
        c = self.canvas
        name = os.path.basename(self.path) if self.path else "Untitled"
        self.setWindowTitle(f"{'*' if c.modified else ''}{name} - {APP}")
        self.a_undo.setEnabled(bool(c.undo_stack) or c.floating is not None)
        self.a_redo.setEnabled(bool(c.redo_stack))
        self.l_size.setText(f"{c.image.width()} × {c.image.height()}px")
        self.l_zoom.setText(f"{c.zoom * 100:g}%")

    def on_status(self, pos, sel):
        if pos is not None:
            self.l_pos.setText(pos)
        if sel is not None:
            self.l_sel.setText(sel)
        self.refresh()

    def on_tool_changed(self, t):
        self.tool_actions[t].setChecked(True)
        self.size_picker.show_sizes(TOOLS[t][4], self.canvas.sizes[t], t == "eraser",
                                    self.palette().color(QPalette.ColorRole.WindowText))
        self.text_bar.setVisible(t == "text")

    def on_size(self, sz):
        self.canvas.sizes[self.canvas.tool] = sz
        self.on_tool_changed(self.canvas.tool)
        self.canvas.setFocus()

    def refresh_colors(self):
        c = self.canvas
        self.slot_btns[1].setIcon(swatch_icon(c.color1, 28))
        self.slot_btns[2].setIcon(swatch_icon(c.color2, 22))
        for s, b in self.slot_btns.items():
            b.setChecked(s == self.active_slot)
        c._disp_key = None
        c.update()

    def set_slot(self, s):
        self.active_slot = s
        self.refresh_colors()

    def on_palette(self, color, right):
        if right or self.active_slot == 2:
            self.canvas.color2 = color
        else:
            self.canvas.color1 = color
        self.refresh_colors()

    def swap_colors(self):
        c = self.canvas
        c.color1, c.color2 = c.color2, c.color1
        self.refresh_colors()

    def edit_colors(self):
        c = self.canvas
        cur = c.color1 if self.active_slot == 1 else c.color2
        col = QColorDialog.getColor(cur, self, "Edit colors")
        if not col.isValid():
            return
        if self.active_slot == 1:
            c.color1 = col
        else:
            c.color2 = col
        if col not in self.grid.custom:
            self.grid.custom = [col] + self.grid.custom[:9]
            self.grid.update()
            self.settings.setValue("custom_colors", [x.name() if x else "" for x in self.grid.custom])
        self.refresh_colors()

    def toggle_transparent(self, on):
        self.canvas.transparent = on
        self.canvas._disp_key = None
        self.canvas.update()

    def context_menu(self, gpos):
        m = QMenu(self)
        for a in (self.a_cut, self.a_copy, self.a_paste, None, self.a_selall, self.a_crop, self.a_delete,
                  None, self.a_rot_r, self.a_rot_l, self.a_flip_h, self.a_flip_v, None, self.a_transp):
            m.addSeparator() if a is None else m.addAction(a)
        m.exec(gpos)

    # ---- clipboard -------------------------------------------------
    def copy(self):
        img = self.canvas.selection_image()
        if img is None:
            img = self.canvas.image
        QApplication.clipboard().setImage(QImage(img))

    def cut(self):
        if self.canvas.sel:
            self.copy()
            self.canvas.delete_selection()

    def paste(self):
        md = QApplication.clipboard().mimeData()
        img = None
        if md is not None and md.hasImage():
            img = QImage(md.imageData())
        if (img is None or img.isNull()) and md is not None and md.hasUrls():
            img = self.load_first_image([u.toLocalFile() for u in md.urls() if u.isLocalFile()])
        if img is None or img.isNull():
            self.statusBar().showMessage("No image on the clipboard", 3000)
            return
        self.canvas.paste_image(img)

    def load_first_image(self, paths):
        for p in paths:
            r = QImageReader(p)
            r.setAutoTransform(True)
            img = r.read()
            if not img.isNull():
                return img
        return None

    def paste_from_file(self):
        p, _ = QFileDialog.getOpenFileName(self, "Paste from", self.last_dir(), self.read_filter())
        if p:
            img = self.load_first_image([p])
            if img is not None:
                self.canvas.paste_image(img)

    def dragEnterEvent(self, e):
        if e.mimeData().hasUrls() or e.mimeData().hasImage():
            e.acceptProposedAction()

    def dropEvent(self, e):
        md = e.mimeData()
        img = QImage(md.imageData()) if md.hasImage() else None
        if img is None or img.isNull():
            img = self.load_first_image([u.toLocalFile() for u in md.urls() if u.isLocalFile()])
        if img is not None and not img.isNull():
            self.canvas.paste_image(img)
            e.acceptProposedAction()

    # ---- files -----------------------------------------------------
    def last_dir(self):
        return self.settings.value("last_dir", os.path.expanduser("~/Pictures"))

    def read_filter(self):
        fmts = sorted({bytes(f).decode() for f in QImageReader.supportedImageFormats()})
        return f"Images ({' '.join('*.' + f for f in fmts)});;All files (*)"

    def maybe_save(self):
        if not self.canvas.modified:
            return True
        name = os.path.basename(self.path) if self.path else "Untitled"
        r = QMessageBox.question(
            self, APP, f"Do you want to save changes to {name}?",
            QMessageBox.StandardButton.Save | QMessageBox.StandardButton.Discard | QMessageBox.StandardButton.Cancel)
        if r == QMessageBox.StandardButton.Save:
            return self.save()
        return r == QMessageBox.StandardButton.Discard

    def new_file(self):
        if not self.maybe_save():
            return
        img = QImage(DEFAULT_SIZE, RGB32)
        img.fill(Qt.GlobalColor.white)
        self.canvas.set_image(img)
        self.canvas.pristine = True
        self.path = None
        self.refresh()

    def open_file(self):
        if not self.maybe_save():
            return
        p, _ = QFileDialog.getOpenFileName(self, "Open", self.last_dir(), self.read_filter())
        if p:
            self.open_path(p)

    def open_path(self, p):
        img = self.load_first_image([p])
        if img is None:
            QMessageBox.warning(self, APP, f"Couldn't open {p}")
            return
        self.settings.setValue("last_dir", os.path.dirname(os.path.abspath(p)))
        self.path = os.path.abspath(p)
        self.canvas.set_image(img)
        self.canvas.set_zoom(1)
        self.refresh()

    def save(self):
        if not self.path:
            return self.save_as()
        return self.write(self.path)

    def save_as(self):
        start = self.path or os.path.join(self.last_dir(), "Untitled.png")
        writable = {bytes(f).decode() for f in QImageWriter.supportedImageFormats()}
        filters = [f for f in ("PNG (*.png)", "JPEG (*.jpg *.jpeg)", "BMP (*.bmp)", "WebP (*.webp)")
                   if f.split("*.")[1].split()[0].rstrip(")") in writable]
        p, flt = QFileDialog.getSaveFileName(self, "Save as", start, ";;".join(filters))
        if not p:
            return False
        if not os.path.splitext(p)[1]:
            p += "." + flt.split("*.")[1].split()[0].rstrip(")")
        if self.write(p):
            self.path = p
            self.settings.setValue("last_dir", os.path.dirname(p))
            self.refresh()
            return True
        return False

    def write(self, p):
        self.canvas.commit()
        ext = os.path.splitext(p)[1].lower()
        ok = self.canvas.image.save(p, None, 95 if ext in (".jpg", ".jpeg", ".webp") else -1)
        if not ok:
            QMessageBox.warning(self, APP, f"Couldn't save {p}")
            return False
        self.canvas.modified = False
        self.refresh()
        self.statusBar().showMessage(f"Saved {p}", 3000)
        return True

    def canvas_size(self):
        d = CanvasSizeDialog(self, self.canvas.image.size())
        if d.exec():
            self.canvas.resize_canvas(d.w.value(), d.h.value())

    # ---- theme -----------------------------------------------------
    def apply_workspace_color(self):
        ws = workspace_color(self.palette())
        self.canvas.workspace = ws
        vp = self.scroll.viewport()
        pal = vp.palette()
        pal.setColor(QPalette.ColorRole.Window, ws)
        vp.setPalette(pal)
        vp.setAutoFillBackground(True)
        self.canvas.update()

    def changeEvent(self, e):
        if e.type() in (e.Type.PaletteChange, e.Type.ApplicationPaletteChange) and hasattr(self, "canvas"):
            self.apply_workspace_color()
            if hasattr(self, "size_picker"):
                self.on_tool_changed(self.canvas.tool)
        super().changeEvent(e)

    # ---- updates ---------------------------------------------------
    def set_auto_update(self, on):
        self.settings.setValue("auto_update", bool(on))
        if on:
            self.update_timer.start(UPDATE_EVERY_MS)
        else:
            self.update_timer.stop()

    def on_update_found(self, version, notes, manual):
        if not manual and version == self.skipped_version:
            return          # "Later" was clicked this session; don't nag
        box = QMessageBox(self)
        box.setWindowTitle("Update available")
        box.setIcon(QMessageBox.Icon.Information)
        box.setText(f"<b>{APP} {version}</b> is available.<br>You have {__version__}.")
        if notes:
            box.setInformativeText("What's new:\n" + "\n".join("• " + n for n in notes.split("\n")))
        upd = box.addButton("Update now", QMessageBox.ButtonRole.AcceptRole)
        box.addButton("Later", QMessageBox.ButtonRole.RejectRole)
        box.setDefaultButton(upd)
        box.exec()
        if box.clickedButton() is not upd:
            self.skipped_version = version
            return
        self.progress = QProgressDialog(f"Installing {APP} {version}…", None, 0, 0, self)
        self.progress.setWindowTitle("Updating")
        self.progress.setMinimumDuration(0)
        self.progress.setWindowModality(Qt.WindowModality.WindowModal)
        self.progress.show()
        self.updating_to = version
        self.updater.install()

    def on_update_installed(self, ok, output):
        self.progress.close()
        if not ok:
            QMessageBox.warning(self, "Update failed", f"The update didn't install:\n\n{output[-1500:]}")
            return
        r = QMessageBox.question(self, "Update installed",
                                 f"{APP} {self.updating_to} is installed.\nRestart now to use it?",
                                 QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
        if r == QMessageBox.StandardButton.Yes:
            self.restart()

    def restart(self):
        if not self.maybe_save():
            return
        self.canvas.modified = False            # already asked; don't ask again on close
        script = os.path.join(INSTALL_DIR, "pepperoni_paint.py")
        if not os.path.isfile(script):
            script = os.path.abspath(__file__)
        args = [script] + ([self.path] if self.path else [])
        if QProcess.startDetached(sys.executable, args)[0]:
            self.close()

    def on_up_to_date(self, manual):
        if manual:
            QMessageBox.information(self, "No updates", f"You're up to date ({APP} {__version__}).")

    def on_update_failed(self, msg, manual):
        if manual:
            QMessageBox.warning(self, "Check for updates", msg)

    def about(self):
        QMessageBox.about(self, f"About {APP}",
                          f"<b>{APP} {__version__}</b><br>A simple Paint for Linux, "
                          f"reminiscent of Windows 7 Paint.<br><br>"
                          f'<a href="{REPO_URL}">{REPO_URL}</a><br>MIT License')

    def closeEvent(self, e):
        if not self.maybe_save():
            e.ignore()
            return
        self.settings.setValue("geometry", self.saveGeometry())
        e.accept()


def main():
    app = QApplication(sys.argv)
    app.setApplicationName(APP)
    app.setDesktopFileName("pepperonipaint")
    for ico in (os.path.join(INSTALL_DIR, "pepperonipaint.svg"),
                os.path.join(os.path.dirname(os.path.abspath(__file__)), "pepperonipaint.svg")):
        if os.path.isfile(ico):
            app.setWindowIcon(QIcon(ico))
            break
    w = MainWindow()
    w.show()
    w.canvas.setFocus()
    if len(sys.argv) > 1:
        w.open_path(sys.argv[1])
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
