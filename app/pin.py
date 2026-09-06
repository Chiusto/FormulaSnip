"""贴图与缩放查看：仿 Snipaste 的贴图窗口 + 可缩放平移的图片视图。

PinWidget：可在任意父容器里缩放/平移查看图片（结果窗口内嵌预览用）。
PinWindow：无边框置顶“贴图”——按住拖动、滚轮缩放（以光标为中心）、
          双击复制图片并关闭、右键菜单（复制/另存/重置/关闭）、Esc 关闭。
"""
from __future__ import annotations

from PySide6.QtCore import QObject, QPointF, QRectF, QSizeF, Qt, Signal
from PySide6.QtGui import QColor, QCursor, QGuiApplication, QImage, QPainter, QPen
from PySide6.QtWidgets import (
    QApplication,
    QFileDialog,
    QMenu,
    QWidget,
)

ACCENT = QColor("#185FA5")
BORDER = QColor("#B4B2A9")
CROSS = QColor(0xE2, 0x39, 0x46)

MIN_SCALE = 0.04
MAX_SCALE = 16.0


def _paste_image(image: QImage):
    QApplication.clipboard().setImage(image)


class _PinBus(QObject):
    """贴图动作总线：任意贴图窗口右键发起的识别/提取，由主控制器接收处理。"""

    recognize_requested = Signal(QImage)   # 对贴图执行「识别公式」
    extract_requested = Signal(QImage)     # 对贴图执行「提取图片信息」


PIN_BUS = _PinBus()


def text_to_pin_image(text: str) -> QImage:
    """把剪贴板文字渲染成贴图（Snipaste 式文字贴图）：
    白底黑字、自动换行、限宽 520px、限高约 900px，超长截断加省略号。"""
    from PySide6.QtCore import QRect
    from PySide6.QtGui import QFont, QFontMetrics

    font = QFont("Microsoft YaHei UI")
    font.setPixelSize(14)
    fm = QFontMetrics(font)
    max_w = 520
    pad = 14
    line_h = fm.height() + 4

    # 预拆行：显式换行 + 自动折行，统计总行数
    lines: list[str] = []
    for raw in text.replace("\t", "    ").splitlines() or [""]:
        line = raw
        while True:
            br = fm.boundingRect(QRect(0, 0, max_w - pad * 2, line_h),
                                 Qt.TextFlag.TextWordWrap, line or " ")
            if br.width() <= max_w - pad * 2 or len(line) <= 1:
                lines.append(line)
                break
            # 按字符逐步裁到能放进一行为止（对中英文均安全）
            lo, hi = 1, len(line)
            while lo < hi:
                mid = (lo + hi + 1) // 2
                if fm.boundingRect(QRect(0, 0, max_w - pad * 2, line_h),
                                   Qt.TextFlag.TextWordWrap,
                                   line[:mid]).width() <= max_w - pad * 2:
                    lo = mid
                else:
                    hi = mid - 1
            lines.append(line[:lo])
            line = line[lo:]
    max_lines = max(1, (900 - pad * 2) // line_h)
    truncated = len(lines) > max_lines
    if truncated:
        lines = lines[:max_lines]

    w = min(max_w,
            max(fm.horizontalAdvance(ln) for ln in lines) + pad * 2 + 4
            if lines else max_w)
    h = pad * 2 + line_h * len(lines)

    img = QImage(w, h, QImage.Format.Format_ARGB32_Premultiplied)
    img.fill(QColor("#FDFDFB"))
    p = QPainter(img)
    p.setFont(font)
    p.setPen(QColor("#1B1B1B"))
    y = pad
    for ln in lines:
        p.drawText(QRect(pad, y, w - pad * 2, line_h),
                   Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, ln)
        y += line_h
    if truncated:
        p.drawText(QRect(pad, y - line_h, w - pad * 2, line_h),
                   Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter,
                   "…（已截断，双击可复制全文）")
    p.end()
    return img


class PinWidget(QWidget):
    """可缩放平移的图片视图：滚轮缩放、左键拖动平移、双击在 100% 与适应之间切换。"""

    def __init__(self, image: QImage | None = None, parent: QWidget | None = None):
        super().__init__(parent)
        self._img: QImage | None = None
        self._scale = 1.0
        self._offset = QPointF()
        self._fit_mode = True
        self._drag_pos: QPointF | None = None
        self.setMouseTracking(True)
        self.setMinimumSize(60, 40)
        if image is not None:
            self.set_image(image)

    # ------------------------------------------------------------- public API
    def set_image(self, image: QImage):
        self._img = image.copy()
        self._fit_mode = True
        self._refit()
        self.update()

    def refit_now(self):
        """立即按当前尺寸重算“适应窗口”比例（供父布局定尺寸后调用）。"""
        self._fit_mode = True
        self._refit()
        self.update()

    def image(self) -> QImage | None:
        return None if self._img is None else QImage(self._img)

    def _refit(self):
        if self._img is None or self.width() < 10 or self.height() < 10:
            return
        iw, ih = self._img.width(), self._img.height()
        s = min((self.width() - 12) / iw, (self.height() - 12) / ih)
        self._scale = max(0.05, s)
        self._offset = QPointF((self.width() - iw * self._scale) / 2,
                               (self.height() - ih * self._scale) / 2)

    def _toggle_fit(self):
        if self._img is None:
            return
        if self._fit_mode:
            self._fit_mode = False
            self._scale = 1.0
            self._offset = QPointF(6, 6)
        else:
            self._fit_mode = True
            self._refit()
        self.update()

    # -------------------------------------------------------------- paint
    def paintEvent(self, e):  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, False)
        # 画布底 + 边框
        p.fillRect(self.rect(), QColor("#F4F4F4"))
        p.setPen(QPen(BORDER, 1))
        p.drawRect(self.rect().adjusted(0, 0, -1, -1))
        img = self._img
        if img is None:
            p.setPen(QColor("#8a8a8a"))
            p.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, "（无图片）")
            return
        target = QRectF(self._offset,
                        QSizeF(img.width() * self._scale,
                               img.height() * self._scale))
        p.drawImage(target, img, QRectF(img.rect()))
        p.end()

    # ------------------------------------------------------------ events
    def resizeEvent(self, e):  # noqa: N802
        super().resizeEvent(e)
        if self._fit_mode:
            self._refit()

    def wheelEvent(self, e):  # noqa: N802
        img = self._img
        if img is None:
            return
        factor = 1.25 if e.angleDelta().y() > 0 else 0.8
        new_s = max(MIN_SCALE, min(MAX_SCALE, self._scale * factor))
        if abs(new_s - self._scale) < 1e-6:
            return
        cursor = QPointF(e.position())
        # 保持光标下的图像点不动
        img_pt = (cursor - self._offset) / self._scale
        self._scale = new_s
        self._offset = cursor - img_pt * new_s
        self._fit_mode = False
        self.update()

    def mousePressEvent(self, e):  # noqa: N802
        if e.button() == Qt.MouseButton.LeftButton:
            self._drag_pos = e.position()
            self.setCursor(Qt.CursorShape.ClosedHandCursor)

    def mouseMoveEvent(self, e):  # noqa: N802
        if self._drag_pos is not None and (e.buttons() & Qt.MouseButton.LeftButton):
            delta = e.position() - self._drag_pos
            self._drag_pos = e.position()
            self._offset += delta
            self.update()

    def mouseReleaseEvent(self, e):  # noqa: N802
        self._drag_pos = None
        self.setCursor(Qt.CursorShape.OpenHandCursor)

    def mouseDoubleClickEvent(self, e):  # noqa: N802
        if e.button() == Qt.MouseButton.LeftButton:
            self._toggle_fit()


# ---------------------------------------------------------------- PinWindow
class PinWindow(QWidget):
    """Snipaste 式贴图：拖动移动整个窗口、滚轮以光标为中心缩放、双击复制并关闭。"""

    closed = Signal()

    def __init__(self, image: QImage, scale_initial: float = 1.0,
                 parent: QWidget | None = None, copy_text: str | None = None):
        super().__init__(parent)
        self._img = image.copy()
        self._copy_text = copy_text       # 文字贴图：双击/菜单复制原文
        self._scale = 1.0
        self._drag_global: QPointF | None = None

        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool
        )
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, True)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, False)
        self.setCursor(Qt.CursorShape.OpenHandCursor)
        self.setToolTip("贴图：拖动移动 · 滚轮缩放 · 双击复制并关闭 · 右键菜单")
        self._apply_geometry(scale_initial, anchor=None, place=True)

    # ------------------------------------------------------------- geometry
    def _img_size_at(self, s: float) -> tuple[int, int]:
        return int(self._img.width() * s), int(self._img.height() * s)

    def _apply_geometry(self, scale: float, anchor: QPointF | None,
                        place: bool = False):
        """以 anchor（窗口内坐标）为不动点缩放并重排窗口；place=True 时放到光标附近。"""
        w, h = self._img_size_at(scale)
        if anchor is None:
            if place:
                cur = QCursor.pos()
                scr = QGuiApplication.primaryScreen().availableGeometry()
                max_s = min(scr.width() * 0.7 / self._img.width(),
                            scr.height() * 0.7 / self._img.height(), 1.0)
                s0 = min(scale, max_s)
                w, h = self._img_size_at(s0)
                x = max(scr.left() + 8, min(cur.x() - w // 2,
                                             scr.right() - w - 8))
                y = max(scr.top() + 8, min(cur.y() - h // 2,
                                           scr.bottom() - h - 8))
                self._scale = s0
                self.setGeometry(x, y, w, h)
                return
            self._scale = scale
            self.setGeometry(self.x(), self.y(), w, h)
            return
        # 光标处图像点不动
        img_pt = (anchor / self._scale) if self._scale else QPointF()
        self._scale = scale
        g = self.mapToGlobal(anchor.toPoint())
        tl = QPointF(g) - QPointF(img_pt.x() * scale, img_pt.y() * scale)
        self.setGeometry(int(tl.x()), int(tl.y()), w, h)

    def zoom_by(self, factor: float, anchor: QPointF):
        new_s = max(MIN_SCALE, min(MAX_SCALE, self._scale * factor))
        if abs(new_s - self._scale) < 1e-6:
            return
        self._apply_geometry(new_s, anchor=anchor)

    # ---------------------------------------------------------------- paint
    def paintEvent(self, e):  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, False)
        p.fillRect(self.rect(), QColor("#ffffff"))
        p.drawImage(self.rect(), self._img)
        # 边框
        p.setPen(QPen(ACCENT, 2))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawRect(self.rect().adjusted(1, 1, -1, -1))
        p.end()

    # ---------------------------------------------------------------- events
    def mousePressEvent(self, e):  # noqa: N802
        if e.button() == Qt.MouseButton.LeftButton:
            self._drag_global = e.globalPosition() - QPointF(self.pos())
            self.setCursor(Qt.CursorShape.ClosedHandCursor)

    def mouseMoveEvent(self, e):  # noqa: N802
        if self._drag_global is not None and (e.buttons() & Qt.MouseButton.LeftButton):
            self.move((e.globalPosition() - self._drag_global).toPoint())

    def mouseReleaseEvent(self, e):  # noqa: N802
        self._drag_global = None
        self.setCursor(Qt.CursorShape.OpenHandCursor)

    def _copy_back(self):
        """双击/菜单复制：文字贴图复制回原文，图片贴图复制图片。"""
        if self._copy_text is not None:
            QApplication.clipboard().setText(self._copy_text)
        else:
            _paste_image(self._img)

    def mouseDoubleClickEvent(self, e):  # noqa: N802
        if e.button() == Qt.MouseButton.LeftButton:
            self._copy_back()
            self.close()

    def wheelEvent(self, e):  # noqa: N802
        factor = 1.25 if e.angleDelta().y() > 0 else 0.8
        self.zoom_by(factor, QPointF(e.position()))

    def keyPressEvent(self, e):  # noqa: N802
        if e.key() == Qt.Key.Key_Escape:
            self.close()
        else:
            super().keyPressEvent(e)

    def _request_recognize(self):
        """右键「识别公式」：把贴图发回主控制器打开识别结果窗口（贴图保留）。"""
        PIN_BUS.recognize_requested.emit(QImage(self._img))

    def _request_extract(self):
        """右键「提取图片信息」：同上，走多模态信息提取模式。"""
        PIN_BUS.extract_requested.emit(QImage(self._img))

    def contextMenuEvent(self, e):  # noqa: N802
        menu = QMenu(self)
        act_recognize = menu.addAction("识别公式")
        act_extract = menu.addAction("提取图片信息")
        menu.addSeparator()
        act_copy_text = (menu.addAction("复制文字")
                         if self._copy_text is not None else None)
        act_copy = menu.addAction("复制图片")
        act_save = menu.addAction("另存为 PNG…")
        menu.addSeparator()
        act_reset = menu.addAction("重置为 100%")
        act_close = menu.addAction("关闭贴图")
        act = menu.exec(e.globalPos())
        if act == act_recognize:
            self._request_recognize()
        elif act == act_extract:
            self._request_extract()
        elif act_copy_text is not None and act == act_copy_text:
            self._copy_back()
        elif act == act_copy:
            _paste_image(self._img)
        elif act == act_save:
            path, _ = QFileDialog.getSaveFileName(
                self, "保存贴图", "formula.png", "PNG 图片 (*.png)")
            if path:
                self._img.save(path, "PNG")
        elif act == act_reset:
            self._apply_geometry(1.0, anchor=None)
        elif act == act_close:
            self.close()

    def closeEvent(self, e):  # noqa: N802
        self.closed.emit()
        super().closeEvent(e)


# 模块级集合：托管所有贴图窗口，避免被 GC 提前回收
_PINS: set = set()


def open_pin(image: QImage) -> PinWindow:
    """在屏幕（尽量靠近光标）贴出一张图，返回窗口对象。"""
    return _spawn_pin(PinWindow(image))


def open_pin_text(text: str) -> PinWindow:
    """把文字渲染成 Snipaste 式文字贴图（双击/菜单复制回原文）。"""
    return _spawn_pin(PinWindow(text_to_pin_image(text), copy_text=text))


def _spawn_pin(win: PinWindow) -> PinWindow:
    _PINS.add(win)
    win.closed.connect(lambda w=win: _PINS.discard(w))
    win.show()
    win.raise_()
    win.activateWindow()
    return win
