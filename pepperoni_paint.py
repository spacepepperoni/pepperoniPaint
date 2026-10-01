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
import sys

from PyQt6.QtCore import (QPoint, QPointF, QRect, QRectF, QSettings, QSize,
                          Qt, QTimer, pyqtSignal)
from PyQt6.QtGui import (QAction, QActionGroup, QColor, QCursor, QIcon,
                         QImage, QImageReader, QImageWriter, QKeySequence,
                         QPainter, QPen, QPixmap, QTransform)
from PyQt6.QtWidgets import (QApplication, QColorDialog, QComboBox, QDialog,
                             QDialogButtonBox, QFileDialog, QFormLayout, QGridLayout,
                             QHBoxLayout, QLabel, QMainWindow, QMenu,
                             QMessageBox, QScrollArea, QSpinBox, QToolBar,
                             QToolButton, QVBoxLayout, QWidget)

APP = "pepperoniPaint"
RGB32 = QImage.Format.Format_RGB32
MARGIN = 8          # logical px of workspace around the canvas
HANDLE = 6          # logical px, resize handle squares
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
}


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
        self._dpr = None
        self._wheel = 0
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
    def float_display(self):
        f = self.floating
        key = (f.cacheKey(), self.transparent, self.color2.rgb())
        if key != self._disp_key:
            self._disp_key = key
            if self.transparent:
                img = f.convertToFormat(QImage.Format.Format_ARGB32_Premultiplied)
                mask = f.convertToFormat(QImage.Format.Format_ARGB32).createMaskFromColor(
                    self.color2.rgb(), Qt.MaskMode.MaskOutColor)
                mask.setColorTable([0x00000000, 0xFFFFFFFF])
                p = QPainter(img)
                p.setCompositionMode(QPainter.CompositionMode.CompositionMode_DestinationIn)
                p.drawImage(0, 0, mask.convertToFormat(QImage.Format.Format_ARGB32))
                p.end()
                self._disp = img
            else:
                self._disp = f
        return self._disp

    def draw_floating(self, p):
        p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform,
                        self.sel.size() != self.floating.size())
        p.drawImage(QRectF(self.sel), self.float_display(), QRectF(self.floating.rect()))

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
        if self.drag and self.drag["kind"] in ("move_sel", "resize_sel", "select_new"):
            self.drag = None
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
        if t != "select":
            self.commit()
        if t == "picker":
            self.prev_tool = self.tool
        self.tool = t
        self.update_cursor()
        self.toolChanged.emit(t)

    def update_cursor(self, pos=None):
        c = Qt.CursorShape.CrossCursor
        if pos is not None:
            h = self.canvas_handle_at(pos)
            if h:
                c = {"e": Qt.CursorShape.SizeHorCursor, "s": Qt.CursorShape.SizeVerCursor,
                     "se": Qt.CursorShape.SizeFDiagCursor}[h]
            elif self.tool == "select":
                h = self.sel_handle_at(pos)
                if h:
                    c = {"n": Qt.CursorShape.SizeVerCursor, "s": Qt.CursorShape.SizeVerCursor,
                         "e": Qt.CursorShape.SizeHorCursor, "w": Qt.CursorShape.SizeHorCursor,
                         "nw": Qt.CursorShape.SizeFDiagCursor, "se": Qt.CursorShape.SizeFDiagCursor,
                         "ne": Qt.CursorShape.SizeBDiagCursor, "sw": Qt.CursorShape.SizeBDiagCursor}[h]
                elif self.sel and QRectF(self.sel).contains(self.to_img(pos)):
                    c = Qt.CursorShape.SizeAllCursor
        self.setCursor(c)

    def canvas_handle_at(self, pos):
        r = self.to_widget(self.image.rect())
        for name, pt in (("se", r.bottomRight()), ("e", QPointF(r.right(), r.center().y())),
                         ("s", QPointF(r.center().x(), r.bottom()))):
            if QRectF(pt.x() - 1, pt.y() - 1, HANDLE + 2, HANDLE + 2).contains(pos):
                return name
        return None

    def sel_handles(self):
        r = self.to_widget(self.sel)
        cx, cy = r.center().x(), r.center().y()
        return {"nw": QPointF(r.left(), r.top()), "n": QPointF(cx, r.top()),
                "ne": QPointF(r.right(), r.top()), "e": QPointF(r.right(), cy),
                "se": QPointF(r.right(), r.bottom()), "s": QPointF(cx, r.bottom()),
                "sw": QPointF(r.left(), r.bottom()), "w": QPointF(r.left(), cy)}

    def sel_handle_at(self, pos):
        if not self.sel:
            return None
        for name, pt in self.sel_handles().items():
            if abs(pos.x() - pt.x()) <= HANDLE and abs(pos.y() - pt.y()) <= HANDLE:
                return name
        return None

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
            if h:
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
            o, h = d["orig"], d["h"]
            l, t, r, b = o.left(), o.top(), o.left() + o.width(), o.top() + o.height()
            if "w" in h:
                l = min(round(ip.x()), r - 1)
            if "e" in h:
                r = max(round(ip.x()), l + 1)
            if "n" in h:
                t = min(round(ip.y()), b - 1)
            if "s" in h:
                b = max(round(ip.y()), t + 1)
            if len(h) == 2 and e.modifiers() & Qt.KeyboardModifier.ShiftModifier:
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
            self.sel = QRect(l, t, r - l, b - t)
            self.update()
            self.emit_sel()
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
        if self._dpr != self.dpr():
            first = self._dpr is None
            self._dpr = self.dpr()
            if not first:
                QTimer.singleShot(0, self.update_size)
        p = QPainter(self)
        p.fillRect(e.rect(), self.palette().color(self.palette().ColorRole.Dark))
        s = self.scale()
        W, H = self.image.width(), self.image.height()
        p.save()
        p.translate(MARGIN, MARGIN)
        p.scale(s, s)
        # Always draw the WHOLE image (clipped by Qt to the exposed area) so the
        # pixel mapping is identical on every repaint: no seams at odd zooms.
        p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, self.zoom < 1)
        p.drawImage(QRectF(0, 0, W, H), self.image)
        p.setClipRect(QRectF(0, 0, W, H))
        if self.floating is not None:
            self.draw_floating(p)
        if self.drag and self.drag["kind"] == "shape":
            self.draw_shape(p)
        p.restore()
        # overlays, in widget coordinates
        p.setRenderHint(QPainter.RenderHint.Antialiasing, False)
        if self.sel:
            r = self.to_widget(self.sel).adjusted(-0.5, -0.5, 0.5, 0.5)
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.setPen(QPen(QColor("white"), 0))
            p.drawRect(r)
            dash = QPen(QColor("#1e5fd8"), 0, Qt.PenStyle.DashLine)
            p.setPen(dash)
            p.drawRect(r)
            if not (self.drag and self.drag["kind"] == "select_new"):
                p.setPen(QPen(QColor("#1e5fd8"), 0))
                p.setBrush(QColor("white"))
                for pt in self.sel_handles().values():
                    p.drawRect(QRectF(pt.x() - HANDLE / 2, pt.y() - HANDLE / 2, HANDLE, HANDLE))
        r = self.to_widget(self.image.rect())
        p.setPen(QPen(QColor("#5a6f8f"), 0))
        p.setBrush(QColor("white"))
        for pt in (r.bottomRight(), QPointF(r.right(), r.center().y()), QPointF(r.center().x(), r.bottom())):
            p.drawRect(QRectF(pt.x(), pt.y(), HANDLE - 1, HANDLE - 1))
        if self.drag and self.drag["kind"] == "canvas":
            sz = self.drag["size"]
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.setPen(QPen(QColor("white"), 0))
            p.drawRect(self.to_widget(QRect(QPoint(0, 0), sz)))
            p.setPen(QPen(QColor("black"), 0, Qt.PenStyle.DashLine))
            p.drawRect(self.to_widget(QRect(QPoint(0, 0), sz)))
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
        self.scroll.setBackgroundRole(self.palette().ColorRole.Dark)
        self.canvas = Canvas(self.scroll)
        self.scroll.setWidget(self.canvas)
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
        for a in (self.a_new, self.a_open, None, self.a_save, self.a_saveas, None, self.a_quit):
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
        tb.addWidget(w)
        tb.addSeparator()

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
            *[b(self.tool_actions[t]) for t in ("pencil", "fill", "picker", "line",
                                                 "brush", "eraser", "rect")],
            grid_cols=4))
        self.size_combo = QComboBox()
        self.size_combo.activated.connect(self.on_size)
        size_w = self.row(self.size_combo, vertical=True)
        self.ribbon_group(tb, "Size", size_w)
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
        sizes = TOOLS[t][4]
        self.size_combo.clear()
        self.size_combo.setEnabled(bool(sizes))
        for s in sizes:
            self.size_combo.addItem(f"{s} px", s)
        if sizes:
            self.size_combo.setCurrentIndex(sizes.index(self.canvas.sizes[t]))

    def on_size(self, i):
        self.canvas.sizes[self.canvas.tool] = self.size_combo.itemData(i)
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
    w = MainWindow()
    w.show()
    w.canvas.setFocus()
    if len(sys.argv) > 1:
        w.open_path(sys.argv[1])
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
