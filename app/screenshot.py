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

from PySide6.QtCore import (
    QLineF,
    QPointF,
    QPoint,
    QRectF,
    QRect,
    QSize,
    Qt,
    Signal,
)
from PySide6.QtGui import (
    QColor,
    QGuiApplication,
    QIcon,
    QImage,
    QCursor,
    QKeySequence,
    QPainter,
    QPen,
    QPixmap,
    QPolygonF,
    QFont,
)
from PySide6.QtWidgets import QFrame, QHBoxLayout, QPushButton, QWidget

# 选区手柄判定阈值（逻辑像素）
HANDLE = 8
MIN_SIZE = 6

TOOLBAR_GAP = 10        # 工具条与选区的间距
TOOLBAR_MARGIN = 6      # 工具条与屏幕边缘的最小间距

ANNO_WIDTH = 3.0        # 标注线宽（逻辑像素）
ANNO_COLORS = {
    "红色": QColor("#FF4D4F"),
    "黄色": QColor("#FFD60A"),
    "绿色": QColor("#34C759"),
    "蓝色": QColor("#2E6BFF"),
    "白色": QColor("#FFFFFF"),
}

_TOOL_KEYS = {          # 标注工具快捷键（选区存在时生效）
    Qt.Key.Key_S: "select",
    Qt.Key.Key_P: "pen",
    Qt.Key.Key_R: "rect",
    Qt.Key.Key_E: "ellipse",
    Qt.Key.Key_A: "arrow",
}


class _ToolbarFrame(QFrame):
    """工具条容器：吞掉鼠标事件，避免穿透到遮罩层误开新选区。"""

    def mousePressEvent(self, e):  # noqa: N802
        e.accept()

    def mouseReleaseEvent(self, e):  # noqa: N802
        e.accept()


def _paint_glyph(p: QPainter, kind: str, color: QColor, width: float = 2.6):
    """在 44x44 坐标系内画 kind 对应的图标图形（描边/文字用 color）。"""
    pen = QPen(color, width)
    pen.setCapStyle(Qt.PenCapStyle.RoundCap)
    pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
    p.setPen(pen)

    if kind == "fx":                       # 识别公式：ƒx
        f = QFont("Georgia", 19)
        f.setBold(True)
        p.setFont(f)
        p.drawText(QRectF(0, 0, 44, 44),
                   Qt.AlignmentFlag.AlignCenter, "ƒx")
    elif kind == "select":                 # 选择：鼠标箭头
        p.setBrush(QColor(255, 255, 255))
        p.drawPolygon(QPolygonF([
            QPointF(15, 9), QPointF(15, 33), QPointF(21, 27),
            QPointF(25, 35), QPointF(29, 33), QPointF(25, 26),
            QPointF(32, 26),
        ]))
    elif kind == "pen":                    # 画笔：斜置铅笔
        p.drawLine(13, 31, 29, 15)
        p.drawLine(29, 15, 33, 11)
        p.drawLine(13, 31, 10, 34)
        p.drawLine(10, 34, 11, 30)
    elif kind == "rect":                   # 矩形
        p.drawRect(10, 13, 24, 18)
    elif kind == "ellipse":                # 椭圆
        p.drawEllipse(10, 12, 24, 20)
    elif kind == "arrow":                  # 箭头
        p.drawLine(12, 32, 31, 13)
        p.drawLine(31, 13, 23, 13)
        p.drawLine(31, 13, 31, 21)
    elif kind == "undo":                   # 撤销：↶
        f = QFont("Segoe UI Symbol", 20)
        p.setFont(f)
        p.drawText(QRectF(0, 0, 44, 44), Qt.AlignmentFlag.AlignCenter, "\u21b6")
    elif kind == "clear":                  # 清空：垃圾桶
        p.drawLine(11, 14, 33, 14)
        p.drawLine(19, 14, 19, 10)
        p.drawLine(19, 10, 25, 10)
        p.drawLine(25, 10, 25, 14)
        p.drawRect(14, 17, 16, 18)
        p.drawLine(19, 21, 19, 31)
        p.drawLine(25, 21, 25, 31)
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


def make_toolbar_icon(kind: str) -> QIcon:
    """用 QPainter 画白色线性图标（无需外部资源）。

    kind: select/pen/rect/ellipse/arrow/undo/clear/fx/extract/copy/pin。
    """
    pm = QPixmap(44, 44)
    pm.fill(Qt.GlobalColor.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    _paint_glyph(p, kind, QColor(255, 255, 255))
    p.end()
    return QIcon(pm)


# 画笔光标热点（44 坐标系内的笔尖位置）；矩形/椭圆/箭头等形状工具用系统十字
_CURSOR_HOTSPOTS = {"pen": (10, 34)}
_tool_cursor_cache: dict[str, QCursor] = {}


def _tool_cursor(kind: str) -> QCursor:
    """标注工具自定义光标：工具图标缩小到 36px、加粗黑描边垫底（亮背景可见）。"""
    cur = _tool_cursor_cache.get(kind)
    if cur is None:
        size = 36
        scale = size / 44.0
        pm = QPixmap(size, size)
        pm.fill(Qt.GlobalColor.transparent)
        p = QPainter(pm)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.scale(scale, scale)
        _paint_glyph(p, kind, QColor(0, 0, 0, 200), width=4.6)  # 描边层
        _paint_glyph(p, kind, QColor(255, 255, 255))            # 白色主体
        p.end()
        hx, hy = _CURSOR_HOTSPOTS[kind]
        cur = QCursor(pm, round(hx * scale), round(hy * scale))
        _tool_cursor_cache[kind] = cur
    return cur

# 命中类型
HIT_NONE = 0
HIT_MOVE = -1
LEFT, TOP, RIGHT, BOTTOM = 1, 2, 4, 8

ACCENT = QColor(0x2E, 0x6B, 0xFF)      # 选区边框蓝
MASK = QColor(0, 0, 0, 132)            # 选区外遮罩


@dataclass
class AnnoShape:
    """一条标注。坐标记为选区归一化坐标（0..1），随选区移动/缩放自动跟随。"""

    kind: str                            # pen / rect / ellipse / arrow
    color: QColor
    width: float
    pts: list[tuple[float, float]]


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

    选区松手后出现 Snipaste 式工具条：标注工具（画笔/矩形/椭圆/箭头，可换颜色、
    撤销/清空）+ 功能按钮（识别公式 / 提取信息 / 复制 / 贴图 / 关闭）。
    标注按选区归一化坐标存储，确认时按 dpr 烧录进输出图片。
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
        self._tool = "select"              # 当前工具（select 或标注种类）
        self._color = "红色"               # 当前标注颜色（ANNO_COLORS 键）
        self._shapes: list[AnnoShape] = []  # 已确认的标注
        self._draw_shape: AnnoShape | None = None  # 正在绘制的标注
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
        """Snipaste 式选区工具条：标注工具 + 颜色 + 撤销清空 + 功能按钮。"""
        bar = _ToolbarFrame(self)
        bar.setObjectName("bar")
        bar.setStyleSheet(
            "QFrame#bar { background: rgba(28,28,30,235); border-radius: 8px; }"
            "QFrame#sep { background: rgba(255,255,255,45); border: none; }"
            "QPushButton { background: transparent; border: none;"
            "              padding: 4px; border-radius: 6px; }"
            "QPushButton:hover { background: rgba(255,255,255,36); }"
            "QPushButton:checked { background: rgba(46,107,255,190); }"
            "QPushButton#close { color: white; font-size: 15px; font-weight: bold;"
            "                    padding: 6px 10px; }")
        lay = QHBoxLayout(bar)
        lay.setContentsMargins(6, 3, 6, 3)
        lay.setSpacing(4)

        # 标注工具（互斥选中；S/P/R/E/A 快捷键切换）
        self._tool_buttons: dict[str, QPushButton] = {}
        for kind, tip in (
            ("select",  "选择 / 调整选区（S）"),
            ("pen",     "画笔：按住左键在选区内涂写（P）"),
            ("rect",    "矩形框选标注（R）"),
            ("ellipse", "椭圆标注（E）"),
            ("arrow",   "箭头指示（A）"),
        ):
            btn = self._make_button(kind, tip)
            btn.setCheckable(True)
            btn.clicked.connect(lambda _=False, k=kind: self._set_tool(k))
            lay.addWidget(btn)
            self._tool_buttons[kind] = btn
        self._tool_buttons["select"].setChecked(True)

        lay.addWidget(self._sep())

        # 标注颜色色块
        self._color_buttons: dict[str, QPushButton] = {}
        for name in ANNO_COLORS:
            btn = QPushButton(bar)
            btn.setFixedSize(18, 18)
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            btn.clicked.connect(lambda _=False, n=name: self._set_color(n))
            lay.addWidget(btn)
            self._color_buttons[name] = btn

        lay.addWidget(self._sep())

        # 撤销 / 清空标注
        self._undo_btn = self._make_button(
            "undo", "撤销上一步标注（Ctrl+Z）", self._undo)
        self._clear_btn = self._make_button(
            "clear", "清空全部标注", self._clear_annos)
        lay.addWidget(self._undo_btn)
        lay.addWidget(self._clear_btn)

        lay.addWidget(self._sep())

        for kind, tip, slot in (
            ("fx",      "识别公式（回车）", self._confirm),
            ("extract", "提取图片信息（供无视觉模型使用）", self._extract_info),
            ("copy",    "复制图片（含标注）", self._copy_clipboard),
            ("pin",     "贴图到桌面（含标注）", self._pin_to_desktop),
        ):
            lay.addWidget(self._make_button(kind, tip, slot))
        close_btn = QPushButton("✕")
        close_btn.setObjectName("close")
        close_btn.setToolTip("退出截图（Esc）")
        close_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        close_btn.clicked.connect(self._discard_selection)
        lay.addWidget(close_btn)
        self._set_color(self._color)
        self._update_anno_buttons()
        bar.hide()
        return bar

    def _make_button(self, kind: str, tip: str, slot=None) -> QPushButton:
        btn = QPushButton()
        btn.setIcon(make_toolbar_icon(kind))
        btn.setIconSize(QSize(22, 22))
        btn.setFixedSize(38, 36)
        btn.setToolTip(tip)
        btn.setCursor(Qt.CursorShape.PointingHandCursor)
        if slot is not None:
            btn.clicked.connect(slot)
        return btn

    @staticmethod
    def _sep() -> QFrame:
        line = QFrame()
        line.setObjectName("sep")
        line.setFixedSize(1, 22)
        return line

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

    # -------------------------------------------------------------- annotation
    def _cursor_for_tool(self, tool: str):
        """按工具返回光标：select 交给命中测试；画笔=笔形；形状工具=十字。"""
        if tool == "select":
            return None
        if tool == "pen":
            return _tool_cursor("pen")
        return Qt.CursorShape.CrossCursor

    def _set_tool(self, tool: str):
        """切换当前工具：select 恢复选区调整，其余为标注绘制（光标同步切换）。"""
        self._tool = tool
        self._draw_shape = None
        for name, btn in self._tool_buttons.items():
            btn.setChecked(name == tool)
        cur = self._cursor_for_tool(tool)
        self.setCursor(cur if cur is not None else Qt.CursorShape.CrossCursor)
        self.update()

    def _set_color(self, name: str):
        self._color = name
        for n, btn in self._color_buttons.items():
            border = "white" if n == name else "rgba(255,255,255,70)"
            btn.setStyleSheet(
                "QPushButton { background: %s; border: 2px solid %s;"
                " border-radius: 9px; }" % (ANNO_COLORS[n].name(), border))
            btn.setToolTip("标注颜色：" + n)

    def _undo(self):
        if self._shapes:
            self._shapes.pop()
            self._update_anno_buttons()
            self.update()

    def _clear_annos(self):
        if self._shapes:
            self._shapes.clear()
            self._update_anno_buttons()
            self.update()

    def _update_anno_buttons(self):
        has = bool(self._shapes)
        self._undo_btn.setEnabled(has)
        self._clear_btn.setEnabled(has)

    def _begin_shape(self, pos: QPoint, r: QRect):
        """在选区内开始一条标注（坐标记为选区归一化坐标）。"""
        f = self._frac(pos, r)
        pts = [f]
        if self._tool != "pen":
            pts.append(f)
        self._draw_shape = AnnoShape(
            self._tool, QColor(ANNO_COLORS[self._color]), ANNO_WIDTH, pts)

    @staticmethod
    def _frac(pos: QPoint, r: QRect) -> tuple[float, float]:
        fx = (pos.x() - r.left()) / max(1, r.width())
        fy = (pos.y() - r.top()) / max(1, r.height())
        return (min(1.0, max(0.0, fx)), min(1.0, max(0.0, fy)))

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
                if self._tool != "select":
                    self._set_tool("select")  # 标注中右键：先退回选择，防误触退出
                elif self.sel is not None:
                    self._discard_selection()
                else:
                    self.canceled.emit()
            return
        pos = e.position().toPoint()
        self._anchor = pos
        if self._tool != "select":
            # 标注工具：只允许在选区内落笔
            if self.sel is not None and self.sel.normalized().contains(pos):
                self._begin_shape(pos, self.sel.normalized())
                self.update()
            return
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
                # 点在选区外：清空并重新框选（旧选区上的标注一并作废）
                self.sel = QRect(pos, pos)
                self._mode = HIT_NONE  # 新框拖拽用独立模式
                self._shapes.clear()
                self._update_anno_buttons()
                self.setCursor(Qt.CursorShape.CrossCursor)
        else:
            self.sel = QRect(pos, pos)
            self._mode = HIT_NONE
            self.setCursor(Qt.CursorShape.CrossCursor)
        self.update()

    def mouseMoveEvent(self, e):  # noqa: N802
        pos = e.position().toPoint()
        # 标注绘制中：跟随鼠标更新当前标注
        if self._draw_shape is not None \
                and e.buttons() & Qt.MouseButton.LeftButton:
            r = self.sel.normalized()
            f = self._frac(pos, r)
            if self._draw_shape.kind == "pen":
                last = self._draw_shape.pts[-1]
                if abs(f[0] - last[0]) + abs(f[1] - last[1]) > 0.002:
                    self._draw_shape.pts.append(f)
            else:
                self._draw_shape.pts[-1] = f
            self.update()
            return
        if e.buttons() & Qt.MouseButton.LeftButton and self.sel is not None:
            # 拖拽全程按操作显式设光标：框选=十字线，移动/缩放=对应样式
            if self._mode == HIT_MOVE:
                delta = pos - self._anchor
                self.sel = (self._press_rect.translated(delta)
                            .intersected(self.rect()))
                self.setCursor(Qt.CursorShape.SizeAllCursor)
            elif self._mode == HIT_NONE:
                self.sel = QRect(self._anchor, pos).normalized()
                self.setCursor(Qt.CursorShape.CrossCursor)
            elif self._mode:
                self.sel = self._resize(self._mode, self._press_rect, pos)
                self.setCursor(self._cursor_for(self._mode))
            if self.sel is not None and (self.sel.width() < MIN_SIZE
                                         or self.sel.height() < MIN_SIZE):
                pass  # 过小选区仍保留，确认时再拦截
            self.update()
            return
        # 非拖拽：更新光标（标注工具用对应光标，选择模式按命中位置变缩放光标）
        cur = self._cursor_for_tool(self._tool)
        if cur is not None:
            self.setCursor(cur)
        elif self.sel is not None:
            self.setCursor(self._cursor_for(self._hit_test(pos)))
        else:
            self.setCursor(Qt.CursorShape.CrossCursor)
        self.update()

    def mouseReleaseEvent(self, e):  # noqa: N802
        if self._draw_shape is not None:
            shape = self._draw_shape
            self._draw_shape = None
            keep = True
            if shape.kind != "pen":
                r = self.sel.normalized()
                (x0, y0), (x1, y1) = shape.pts
                if (abs(x1 - x0) * r.width() < 3
                        and abs(y1 - y0) * r.height() < 3):
                    keep = False  # 几乎没拖动：丢弃空形状
            if keep:
                self._shapes.append(shape)
            self._update_anno_buttons()
            self.update()
            return                  # 标注后工具条保持可见，便于连续绘制
        self._mode = None
        self._press_rect = None
        if self._tool == "select":
            self._place_toolbar()   # 松手后显示工具条（Snipaste 交互）

    def mouseDoubleClickEvent(self, e):  # noqa: N802
        if (e.button() == Qt.MouseButton.LeftButton
                and self._tool == "select"
                and self.sel is not None
                and self.sel.contains(e.position().toPoint())):
            self._confirm()

    def keyPressEvent(self, e):  # noqa: N802
        key = e.key()
        if key == Qt.Key.Key_Escape:
            if self._tool != "select":
                self._set_tool("select")   # 先退出标注工具，再退出截图
                return
            if self.sel is not None:
                self._discard_selection()
            else:
                self.canceled.emit()
            return
        if e.matches(QKeySequence.StandardKey.Undo):
            self._undo()
            return
        if self.sel is not None and key in _TOOL_KEYS:
            self._set_tool(_TOOL_KEYS[key])
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
        """把本地逻辑选区换算成物理像素裁出，并把标注烧录进图片。

        标注按选区归一化坐标存储，映射到调用方传入的选区矩形（主控始终
        传 self.sel.normalized()），线宽按 dpr 放大保证 1:1 清晰。
        """
        dpr = self.shot.dpr
        phys = QRect(
            int(round(local_rect.x() * dpr)),
            int(round(local_rect.y() * dpr)),
            int(round(local_rect.width() * dpr)),
            int(round(local_rect.height() * dpr)),
        )
        phys = phys.intersected(self.shot.image.rect())
        image = self.shot.image.copy(phys)
        if self._shapes:
            p = QPainter(image)
            p.setRenderHint(QPainter.RenderHint.Antialiasing)
            self._render_shapes(p, self._shapes, QRectF(phys), dpr)
            p.end()
        return image

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
        # 2) 遮罩 + 选区 + 标注
        if self.sel is not None:
            r = self.sel.normalized().intersected(self.rect())
            self._paint_mask(p, r)
            self._paint_shapes(p, r)
            self._paint_frame(p, r)
            self._paint_size_chip(p, r)
        # 3) 底部操作提示
        self._paint_hint(p)
        # 4) 混合 DPI 提示
        if self.shot.mixed_dpi:
            p.setPen(QColor(255, 200, 60))
            p.drawText(16, 32, "检测到混合 DPI，本次仅截取主屏区域")
        p.end()

    def _paint_shapes(self, p: QPainter, r: QRect):
        """把标注画进选区（裁剪到选区内；含正在绘制的这条）。"""
        if not self._shapes and self._draw_shape is None:
            return
        pending = list(self._shapes)
        if self._draw_shape is not None:
            pending.append(self._draw_shape)
        p.save()
        p.setClipRect(r)
        self._render_shapes(p, pending, QRectF(r), 1.0)
        p.restore()

    @staticmethod
    def _render_shapes(p: QPainter, shapes, rect: QRectF, wscale: float):
        """把标注列表渲染到 rect（坐标归一化→rect 实际坐标，线宽乘 wscale）。"""
        for s in shapes:
            pen = QPen(s.color, s.width * wscale)
            pen.setCapStyle(Qt.PenCapStyle.RoundCap)
            pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
            p.setPen(pen)
            p.setBrush(Qt.BrushStyle.NoBrush)
            pts = [QPointF(rect.left() + fx * rect.width(),
                           rect.top() + fy * rect.height())
                   for fx, fy in s.pts]
            if s.kind == "pen":
                if len(pts) == 1:
                    p.drawLine(pts[0], pts[0])   # 圆头笔单击成点
                else:
                    p.drawPolyline(QPolygonF(pts))
            elif s.kind == "rect":
                p.drawRect(QRectF(pts[0], pts[1]).normalized())
            elif s.kind == "ellipse":
                p.drawEllipse(QRectF(pts[0], pts[1]).normalized())
            elif s.kind == "arrow":
                line = QLineF(pts[0], pts[1])
                p.drawLine(line)
                if line.length() > 1:
                    head = 12.0 * wscale
                    for da in (25.0, -25.0):
                        arm = QLineF.fromPolar(head, line.angle() + 180.0 + da)
                        arm.translate(line.p2())
                        p.drawLine(arm)

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
        elif self._tool != "select":
            name = {"pen": "画笔", "rect": "矩形", "ellipse": "椭圆",
                    "arrow": "箭头"}[self._tool]
            text = (f"{name}标注：按住左键在选区内拖动绘制 · 工具条可换颜色"
                    " · Ctrl+Z 撤销 · 右键/Esc 返回选择")
        else:
            text = ("工具条：画笔 / 矩形 / 椭圆 / 箭头标注 · 撤销    "
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
