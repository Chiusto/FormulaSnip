"""截屏核心：抓取桌面 + 仿 Snipaste 的选区遮罩层。

设计说明
--------
- 抓屏：主屏 100% 可靠；多屏在“各屏 DPI 一致”时逐屏抓取拼接（Windows/X11 上
  grabWindow 的 x,y 相对各屏原点，故每屏传屏内坐标再按虚拟桌面位置拼接），
  若检测到混合 DPI 则自动回退为仅截主屏（避免跨屏缩放错位）。
- 遮罩层用一张覆盖整个虚拟桌面的无边框置顶窗口实现，交互全部在本地逻辑坐标，
  确认后按 dpr 换算回物理像素裁剪，保证 1:1 清晰。
"""
from __future__ import annotations

from dataclasses import dataclass

from PySide6.QtCore import QPoint, QRect, QSize, Qt, Signal
from PySide6.QtGui import (
    QColor,
    QGuiApplication,
    QIcon,
    QImage,
    QPainter,
    QPen,
    QPixmap,
    QFont,
)
from PySide6.QtWidgets import QFrame, QHBoxLayout, QPushButton, QWidget

# 选区手柄判定阈值（逻辑像素）
HANDLE = 8
MIN_SIZE = 6

TOOLBAR_GAP = 10        # 工具条与选区的间距
TOOLBAR_MARGIN = 6      # 工具条与屏幕边缘的最小间距


class _ToolbarFrame(QFrame):
    """工具条容器：吞掉鼠标事件，避免穿透到遮罩层误开新选区。"""

    def mousePressEvent(self, e):  # noqa: N802
        e.accept()

    def mouseReleaseEvent(self, e):  # noqa: N802
        e.accept()


def make_toolbar_icon(kind: str) -> QIcon:
    """用 QPainter 画白色线性图标（无需外部资源）。kind: fx/extract/copy/pin。"""
    pm = QPixmap(44, 44)
    pm.fill(Qt.GlobalColor.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    pen = QPen(QColor(255, 255, 255), 2.6)
    pen.setCapStyle(Qt.PenCapStyle.RoundCap)
    pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
    p.setPen(pen)

    if kind == "fx":                       # 识别公式：ƒx
        f = QFont("Georgia", 19)
        f.setBold(True)
        p.setFont(f)
        p.drawText(pm.rect(), Qt.AlignmentFlag.AlignCenter, "ƒx")
    elif kind == "extract":                # 提取信息：文档 + 内容行
        p.drawRoundedRect(11, 7, 22, 30, 3, 3)
        for y in (15, 21, 27):
            p.drawLine(16, y, 28, y)
    elif kind == "copy":                   # 复制：双层矩形
        p.drawRoundedRect(13, 7, 20, 24, 3, 3)
        p.drawRoundedRect(9, 13, 20, 24, 3, 3)
    elif kind == "pin":                    # 贴图：图钉
        p.setBrush(QColor(255, 255, 255))
        p.drawEllipse(14, 8, 16, 16)
        p.drawLine(22, 24, 22, 30)
        p.drawLine(22, 30, 15, 37)
    p.end()
    return QIcon(pm)

# 命中类型
HIT_NONE = 0
HIT_MOVE = -1
LEFT, TOP, RIGHT, BOTTOM = 1, 2, 4, 8

ACCENT = QColor(0x2E, 0x6B, 0xFF)      # 选区边框蓝
MASK = QColor(0, 0, 0, 132)            # 选区外遮罩


@dataclass
class DesktopShot:
    """一次桌面抓取结果。image 为 dpr=1 的物理像素位图，geometry 为其虚拟桌面逻辑范围。"""

    image: QImage
    geometry: QRect
    dpr: float
    mixed_dpi: bool = False


def capture_desktop() -> DesktopShot:
    """抓取整个虚拟桌面（多屏同 DPI 时），混合 DPI 时回退到主屏。"""
    gui = QGuiApplication.instance()
    screens = gui.screens()
    dprs = {round(s.devicePixelRatio(), 4) for s in screens}
    mixed = len(dprs) > 1

    if mixed:
        # 混合 DPI：只截主屏，overlay 提示
        s = gui.primaryScreen()
        dpr = s.devicePixelRatio()
        geo = s.geometry()
        sub = s.grabWindow(0, 0, 0, geo.width(), geo.height()).toImage()
        sub.setDevicePixelRatio(1.0)
        return DesktopShot(sub, geo, dpr, True)

    dpr = next(iter(dprs)) if dprs else 1.0
    geo = screens[0].geometry()
    for s in screens[1:]:
        geo = geo.united(s.geometry())

    canvas = QImage(
        int(geo.width() * dpr), int(geo.height() * dpr),
        QImage.Format.Format_ARGB32_Premultiplied,
    )
    canvas.fill(Qt.GlobalColor.transparent)
    p = QPainter(canvas)
    for s in screens:
        g = s.geometry()
        # 参数为逻辑坐标（相对该屏左上角），Qt 内部按该屏 dpr 换算像素
        sub = s.grabWindow(0, 0, 0, g.width(), g.height()).toImage()
        sub.setDevicePixelRatio(1.0)
        dst = QRect(
            int(round((g.x() - geo.x()) * dpr)),
            int(round((g.y() - geo.y()) * dpr)),
            int(round(g.width() * dpr)),
            int(round(g.height() * dpr)),
        )
        p.drawImage(dst, sub)
    p.end()
    return DesktopShot(canvas, geo, dpr, False)


class CaptureOverlay(QWidget):
    """全屏选区遮罩：拖拽画框、边缘/四角缩放、整框移动、方向键微调、右下角工具条。

    选区松手后出现 Snipaste 式工具条（识别公式 / 复制 / 贴图 / 关闭）。
    """

    accepted = Signal(QRect)   # 本地逻辑坐标的选区（识别公式）
    extract_requested = Signal(QRect)  # 提取图片信息（供无视觉模型使用）
    copy_requested = Signal(QRect)   # 复制选区图片到剪贴板
    pin_requested = Signal(QRect)    # 把选区图片贴到桌面
    canceled = Signal()

    def __init__(self, shot: DesktopShot, parent=None):
        super().__init__(parent)
        self.shot = shot
        self.sel: QRect | None = None      # 当前选区（本地坐标）
        self._mode: int | None = None      # HIT_* 当前操作
        self._anchor = QPoint()            # 按下时鼠标位置
        self._press_rect: QRect | None = None
        self._bg_cache: QPixmap | None = None
        self._toolbar = self._build_toolbar()

        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool
        )
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, True)
        self.setCursor(Qt.CursorShape.CrossCursor)
        self.setMouseTracking(True)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setGeometry(shot.geometry)
        self.setWindowTitle("公式快截 - 选择区域")

    # ------------------------------------------------------------------ UI
    def show_and_activate(self):
        self.show()
        self.raise_()
        self.activateWindow()
        self.setFocus()

    def _build_toolbar(self) -> QFrame:
        """Snipaste 式选区工具条：图标按钮（悬停显示文字提示）。"""
        bar = _ToolbarFrame(self)
        bar.setStyleSheet(
            "QFrame { background: rgba(28,28,30,235); border-radius: 8px; }"
            "QPushButton { background: transparent; border: none;"
            "              padding: 4px; border-radius: 6px; }"
            "QPushButton:hover { background: rgba(255,255,255,36); }"
            "QPushButton#close { color: white; font-size: 15px; font-weight: bold;"
            "                    padding: 6px 10px; }")
        lay = QHBoxLayout(bar)
        lay.setContentsMargins(6, 3, 6, 3)
        lay.setSpacing(4)
        for kind, tip, slot in (
            ("fx",      "识别公式（回车）", self._confirm),
            ("extract", "提取图片信息（供无视觉模型使用）", self._extract_info),
            ("copy",    "复制图片", self._copy_clipboard),
            ("pin",     "贴图到桌面", self._pin_to_desktop),
        ):
            btn = QPushButton()
            btn.setIcon(make_toolbar_icon(kind))
            btn.setIconSize(QSize(22, 22))
            btn.setFixedSize(38, 36)
            btn.setToolTip(tip)
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            btn.clicked.connect(slot)
            lay.addWidget(btn)
        close_btn = QPushButton("✕")
        close_btn.setObjectName("close")
        close_btn.setToolTip("退出截图（Esc）")
        close_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        close_btn.clicked.connect(self._discard_selection)
        lay.addWidget(close_btn)
        bar.hide()
        return bar

    def _place_toolbar(self):
        """工具条放到选区右下角；放不下时翻到选区内侧，整体不超出屏幕。"""
        if self.sel is None:
            self._toolbar.hide()
            return
        r = self.sel.normalized()
        tb = self._toolbar
        tb.adjustSize()
        x = r.right() - tb.width()
        y = r.bottom() + TOOLBAR_GAP
        if y + tb.height() > self.height() - TOOLBAR_MARGIN:
            y = r.bottom() - tb.height() - TOOLBAR_GAP   # 翻到选区上方
        if y < TOOLBAR_MARGIN:
            y = max(TOOLBAR_MARGIN, r.bottom() - tb.height() - TOOLBAR_GAP)
        x = max(TOOLBAR_MARGIN, min(x, self.width() - tb.width() - TOOLBAR_MARGIN))
        y = max(TOOLBAR_MARGIN, min(y, self.height() - tb.height() - TOOLBAR_MARGIN))
        tb.move(x, y)
        tb.show()
        tb.raise_()

    # ------------------------------------------------------------ toolbar act
    def _copy_clipboard(self):
        if self.sel is not None:
            self.copy_requested.emit(self.sel.normalized())
            self.close()

    def _extract_info(self):
        """工具条「提取信息」：提取选区全部多模态信息，供无视觉模型使用。"""
        if self.sel is None:
            return
        r = self.sel.normalized()
        if r.width() < MIN_SIZE or r.height() < MIN_SIZE:
            return
        self._toolbar.hide()
        self.extract_requested.emit(r)
        self.close()

    def _pin_to_desktop(self):
        if self.sel is not None:
            self.pin_requested.emit(self.sel.normalized())
            self.close()

    def _discard_selection(self):
        """工具条 ✕：退出本次截图（关闭遮罩，等同无选区时按 Esc）。"""
        self._toolbar.hide()
        self.canceled.emit()
        self.close()

    def _build_cache(self):
        """把抓屏位图按窗口尺寸预渲染成缓存，避免每次 paint 全图重采样。"""
        dpr = self.devicePixelRatioF() or 1.0
        pm = QPixmap(max(1, int(self.width() * dpr)),
                     max(1, int(self.height() * dpr)))
        pm.setDevicePixelRatio(dpr)
        p = QPainter(pm)
        p.drawImage(pm.rect(), self.shot.image)
        p.end()
        self._bg_cache = pm

    def showEvent(self, e):  # noqa: N802
        super().showEvent(e)
        self._build_cache()

    def resizeEvent(self, e):  # noqa: N802
        super().resizeEvent(e)
        self._build_cache()

    # ------------------------------------------------------------ interaction
    def mousePressEvent(self, e):  # noqa: N802
        if e.button() != Qt.MouseButton.LeftButton:
            if e.button() == Qt.MouseButton.RightButton:
                if self.sel is not None:
                    self._discard_selection()
                else:
                    self.canceled.emit()
            return
        pos = e.position().toPoint()
        self._anchor = pos
        self._toolbar.hide()
        if self.sel is not None:
            hit = self._hit_test(pos)
            if hit == HIT_MOVE:
                self._mode = HIT_MOVE
                self._press_rect = QRect(self.sel)
            elif hit != HIT_NONE:
                self._mode = hit
                self._press_rect = QRect(self.sel)
            else:
                # 点在选区外：清空并重新框选
                self.sel = QRect(pos, pos)
                self._mode = HIT_NONE  # 新框拖拽用独立模式
        else:
            self.sel = QRect(pos, pos)
            self._mode = HIT_NONE
        self.update()

    def mouseMoveEvent(self, e):  # noqa: N802
        pos = e.position().toPoint()
        if e.buttons() & Qt.MouseButton.LeftButton and self.sel is not None:
            if self._mode == HIT_MOVE:
                delta = pos - self._anchor
                self.sel = (self._press_rect.translated(delta)
                            .intersected(self.rect()))
            elif self._mode == HIT_NONE:
                self.sel = QRect(self._anchor, pos).normalized()
            elif self._mode:
                self.sel = self._resize(self._mode, self._press_rect, pos)
            if self.sel is not None and (self.sel.width() < MIN_SIZE
                                         or self.sel.height() < MIN_SIZE):
                pass  # 过小选区仍保留，确认时再拦截
            self.update()
            return
        # 非拖拽：更新光标
        if self.sel is not None:
            self.setCursor(self._cursor_for(self._hit_test(pos)))
        else:
            self.setCursor(Qt.CursorShape.CrossCursor)
        self.update()

    def mouseReleaseEvent(self, e):  # noqa: N802
        self._mode = None
        self._press_rect = None
        self._place_toolbar()   # 松手后显示工具条（Snipaste 交互）

    def mouseDoubleClickEvent(self, e):  # noqa: N802
        if (e.button() == Qt.MouseButton.LeftButton
                and self.sel is not None
                and self.sel.contains(e.position().toPoint())):
            self._confirm()

    def keyPressEvent(self, e):  # noqa: N802
        key = e.key()
        if key == Qt.Key.Key_Escape:
            if self.sel is not None:
                self._discard_selection()
            else:
                self.canceled.emit()
            return
        if key in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            self._confirm()
            return
        if self.sel is not None and key in (
            Qt.Key.Key_Left, Qt.Key.Key_Right,
            Qt.Key.Key_Up, Qt.Key.Key_Down,
        ):
            step = 10 if e.modifiers() & Qt.KeyboardModifier.ShiftModifier else 1
            dx = dy = 0
            if key == Qt.Key.Key_Left:
                dx = -step
            elif key == Qt.Key.Key_Right:
                dx = step
            elif key == Qt.Key.Key_Up:
                dy = -step
            else:
                dy = step
            moved = self.sel.translated(dx, dy).intersected(self.rect())
            if moved.width() >= MIN_SIZE and moved.height() >= MIN_SIZE:
                self.sel = moved
                self._place_toolbar()
                self.update()

    # ---------------------------------------------------------------- helpers
    def _confirm(self):
        if self.sel is None:
            return
        r = self.sel.normalized()
        if r.width() < MIN_SIZE or r.height() < MIN_SIZE:
            return
        self._toolbar.hide()
        self.accepted.emit(r)
        self.close()

    def crop_image(self, local_rect: QRect) -> QImage:
        """把本地逻辑选区换算成物理像素并从抓屏位图中裁出。"""
        dpr = self.shot.dpr
        phys = QRect(
            int(round(local_rect.x() * dpr)),
            int(round(local_rect.y() * dpr)),
            int(round(local_rect.width() * dpr)),
            int(round(local_rect.height() * dpr)),
        )
        phys = phys.intersected(self.shot.image.rect())
        return self.shot.image.copy(phys)

    def _hit_test(self, pos: QPoint) -> int:
        """返回 HIT_NONE / HIT_MOVE / 边缘位组合。"""
        r = self.sel
        if r is None:
            return HIT_NONE
        outer = r.adjusted(-HANDLE, -HANDLE, HANDLE, HANDLE)
        if not outer.contains(pos):
            return HIT_NONE
        bits = 0
        if abs(pos.x() - r.left()) <= HANDLE:
            bits |= LEFT
        if abs(pos.x() - r.right()) <= HANDLE:
            bits |= RIGHT
        if abs(pos.y() - r.top()) <= HANDLE:
            bits |= TOP
        if abs(pos.y() - r.bottom()) <= HANDLE:
            bits |= BOTTOM
        return bits if bits else (HIT_MOVE if r.contains(pos) else HIT_NONE)

    @staticmethod
    def _cursor_for(hit: int) -> Qt.CursorShape:
        if hit == HIT_MOVE:
            return Qt.CursorShape.SizeAllCursor
        if hit == HIT_NONE:
            return Qt.CursorShape.CrossCursor
        horiz = bool(hit & (LEFT | RIGHT))
        vert = bool(hit & (TOP | BOTTOM))
        if horiz and vert:
            diag = (hit & LEFT and hit & TOP) or (hit & RIGHT and hit & BOTTOM)
            return (Qt.CursorShape.SizeFDiagCursor if diag
                    else Qt.CursorShape.SizeBDiagCursor)
        if horiz:
            return Qt.CursorShape.SizeHorCursor
        if vert:
            return Qt.CursorShape.SizeVerCursor
        return Qt.CursorShape.CrossCursor

    def _resize(self, edges: int, press: QRect, pos: QPoint) -> QRect:
        """按拖拽位置重算选区矩形，保证最小尺寸。"""
        left, top = press.left(), press.top()
        right, bottom = press.right(), press.bottom()
        dx = pos.x() - self._anchor.x()
        dy = pos.y() - self._anchor.y()
        w0, h0 = press.width(), press.height()
        if edges & LEFT:
            left = min(press.left() + dx, right - MIN_SIZE)
        if edges & RIGHT:
            right = max(press.right() + dx, left + MIN_SIZE)
        if edges & TOP:
            top = min(press.top() + dy, bottom - MIN_SIZE)
        if edges & BOTTOM:
            bottom = max(press.bottom() + dy, top + MIN_SIZE)
        _ = w0, h0
        r = QRect(QPoint(left, top), QPoint(right, bottom)).normalized()
        return r.intersected(self.rect())

    # ------------------------------------------------------------------ paint
    def paintEvent(self, e):  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        # 1) 背景 = 抓屏位图（预渲染缓存；未就绪时直接绘制）
        if self._bg_cache is not None and not self._bg_cache.isNull():
            p.drawPixmap(0, 0, self._bg_cache)
        else:
            p.drawImage(self.rect(), self.shot.image)
        # 2) 遮罩 + 选区
        if self.sel is not None:
            r = self.sel.normalized().intersected(self.rect())
            self._paint_mask(p, r)
            self._paint_frame(p, r)
            self._paint_size_chip(p, r)
        # 3) 底部操作提示
        self._paint_hint(p)
        # 4) 混合 DPI 提示
        if self.shot.mixed_dpi:
            p.setPen(QColor(255, 200, 60))
            p.drawText(16, 32, "检测到混合 DPI，本次仅截取主屏区域")
        p.end()

    def _paint_mask(self, p: QPainter, r: QRect):
        if r.isEmpty():
            return
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(MASK)
        w, h = self.width(), self.height()
        # 上 下 左 右 四条带
        p.drawRect(0, 0, w, r.top())                    # noqa: E225
        p.drawRect(0, r.bottom() + 1, w, h - r.bottom() - 1)
        p.drawRect(0, r.top(), r.left(), r.height())
        p.drawRect(r.right() + 1, r.top(), w - r.right() - 1, r.height())

    def _paint_frame(self, p: QPainter, r: QRect):
        pen = QPen(ACCENT, 2)
        p.setPen(pen)
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawRect(r.adjusted(1, 1, -1, -1))
        # 四角白色手柄
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor(255, 255, 255))
        s = 4
        for cx, cy in (
            (r.left(), r.top()), (r.right(), r.top()),
            (r.left(), r.bottom()), (r.right(), r.bottom()),
        ):
            p.drawRect(cx - s, cy - s, 2 * s, 2 * s)

    def _paint_size_chip(self, p: QPainter, r: QRect):
        text = f"{r.width()} x {r.height()}"
        p.setFont(self._mono(12))
        fm = p.fontMetrics()
        tw = fm.horizontalAdvance(text)
        cw = tw + 20
        ch = fm.height() + 10
        cx = r.left()
        cy = r.bottom() + 10
        if cy + ch > self.height() - 4:          # 贴底时放到选区内
            cy = r.bottom() - ch - 10
        if cx + cw > self.width() - 4:
            cx = self.width() - cw - 4
        chip = QRect(cx, cy, cw, ch)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor(0, 0, 0, 190))
        p.drawRoundedRect(chip, 5, 5)
        p.setPen(QColor(255, 255, 255))
        p.drawText(chip, Qt.AlignmentFlag.AlignCenter, text)

    def _paint_hint(self, p: QPainter):
        if self.sel is None:
            text = "按住鼠标左键拖动，框选要识别的公式区域"
        else:
            text = ("工具条：识别公式 / 提取信息 / 复制 / 贴图    "
                    "回车 = 识别    Esc 取消    拖边框缩放 · 方向键微调")
        p.setFont(self._font(13))
        fm = p.fontMetrics()
        tw = fm.horizontalAdvance(text)
        cw = tw + 36
        ch = fm.height() + 16
        cx = (self.width() - cw) // 2
        cy = self.height() - ch - 24
        bar = QRect(cx, cy, cw, ch)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor(0, 0, 0, 200))
        p.drawRoundedRect(bar, 8, 8)
        p.setPen(QColor(255, 255, 255))
        p.drawText(bar, Qt.AlignmentFlag.AlignCenter, text)

    def _font(self, px: int):
        from PySide6.QtGui import QFont

        f = QFont("Microsoft YaHei UI")
        f.setPixelSize(px)
        return f

    def _mono(self, px: int):
        from PySide6.QtGui import QFont

        f = QFont("Consolas")
        f.setPixelSize(px)
        f.setBold(True)
        return f
