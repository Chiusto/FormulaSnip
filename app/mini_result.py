"""屏幕侧边迷你识别窗：识别期间收纳为小胶囊，完成后提示并可点开查看。

设计动机：确认选区后不直接弹出大结果窗打断工作——屏幕右侧出现一个小胶囊
（截图缩略图 + 进度），识别在后台进行；完成后自动复制并提示，胶囊上直接
给出「复制」「编辑」按钮与右上角 × 关闭，点击复制/编辑均不关闭胶囊、由
用户手动 × 关闭。多个胶囊由主控在屏幕右侧按创建顺序纵向排列；悬停胶囊
可放大预览本次识别的输入截图。
"""
from __future__ import annotations

import time

from PySide6.QtCore import QPoint, QRect, QRectF, Qt, QTimer, Signal
from PySide6.QtGui import (
    QColor,
    QCursor,
    QFont,
    QFontMetrics,
    QGuiApplication,
    QImage,
    QPainter,
    QPainterPath,
    QPen,
    QPixmap,
)
from PySide6.QtWidgets import QPushButton, QWidget

PILL_WIDTH = 264
PILL_HEIGHT = 60
PILL_HEIGHT_SUCCESS = 92          # 完成态：多一行「复制 / 编辑」按钮
BTN_ROW_TOP = 58
BTN_HEIGHT = 24
CLOSE_SIZE = 20
EDGE_MARGIN = 14
COPY_FLASH_MS = 1200
PREVIEW_DELAY_MS = 350              # 悬停多久后弹出放大预览
HOVER_POLL_MS = 100                 # 悬停几何检测轮询间隔

_STATE_RUNNING = "running"
_STATE_SUCCESS = "success"
_STATE_FAILED = "failed"

_COLOR_BG = QColor(30, 32, 38, 235)
_COLOR_BG_HOVER = QColor(46, 50, 58, 242)
_COLOR_BORDER = QColor(255, 255, 255, 30)
_COLOR_TITLE = QColor(240, 242, 245)
_COLOR_SUB = QColor(154, 162, 172)
_COLOR_SPIN = QColor("#4C8DFF")
_COLOR_OK = QColor("#43a047")
_COLOR_ERR = QColor("#e53935")


class MiniResultWindow(QWidget):
    """识别中悬浮于屏幕侧边的迷你胶囊窗（无任务栏项、置顶、不主动抢焦点）。

    生命周期：出现后**保持显示，仅由用户点右上角 × 手动关闭**（识别中点 ×
    则该次识别转入后台，完成后仍自动复制并气泡提示）。左键点本体发出
    open_requested、按住拖动可移动；右键无操作（防误触）。完成（成功）态给
    出「复制」「编辑」按钮，点击均不关闭胶囊。悬停片刻显示放大预览浮窗
    （输入截图），悬停检测用几何轮询而非 enter/leave 事件，避免子控件或
    原生提示窗干扰。
    """

    open_requested = Signal()
    copy_requested = Signal()
    closed = Signal()

    def __init__(self, image: QImage, mode: str = "formula",
                 parent: QWidget | None = None):
        super().__init__(parent)
        self._mode = mode
        self._state = _STATE_RUNNING
        self._angle = 0
        self._elapsed = -1
        self._started = time.monotonic()
        self._hovered = False
        self._title = "识别中…" if mode == "formula" else "提取图片信息中…"
        self._subtitle = ""
        self._drag_offset: QPoint | None = None
        self._moved = False

        pm = QPixmap.fromImage(image)
        if not pm.isNull():
            pm = pm.scaled(40, 40, Qt.AspectRatioMode.KeepAspectRatio,
                           Qt.TransformationMode.SmoothTransformation)
        self._thumb = pm

        self.setWindowTitle("公式识别中" if mode == "formula" else "提取图片信息")
        self.setWindowFlags(Qt.WindowType.FramelessWindowHint
                            | Qt.WindowType.Tool
                            | Qt.WindowType.WindowStaysOnTopHint)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setFixedSize(PILL_WIDTH, PILL_HEIGHT)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setToolTip("识别中，完成后自动复制到剪贴板\n左键：打开结果窗口")

        # 完成态操作按钮：复制 / 编辑 / 右上角关闭
        self._btn_copy = QPushButton("复制", self)
        self._btn_edit = QPushButton("编辑", self)
        self._btn_close = QPushButton("×", self)
        for b in (self._btn_copy, self._btn_edit, self._btn_close):
            b.setObjectName("miniBtn" if b is not self._btn_close else "miniClose")
            b.setCursor(Qt.CursorShape.PointingHandCursor)
            b.hide()
        self._btn_copy.setToolTip("把识别结果复制到剪贴板")
        self._btn_edit.setToolTip("打开结果窗口查看 / 编辑后再复制")
        self._btn_close.setToolTip("收起胶囊（结果仍在剪贴板）")
        self._btn_copy.clicked.connect(self.copy_requested.emit)
        self._btn_edit.clicked.connect(self.open_requested.emit)
        self._btn_close.clicked.connect(self.close)
        self.setStyleSheet("""
            QPushButton#miniBtn {
                background: rgba(255,255,255,0.09);
                color: #f0f2f5;
                border: 1px solid rgba(255,255,255,0.16);
                border-radius: 5px;
                padding: 0 6px;
                font-size: 11px;
            }
            QPushButton#miniBtn:hover { background: rgba(255,255,255,0.18); }
            QPushButton#miniBtn:pressed { background: rgba(255,255,255,0.26); }
            QPushButton#miniClose {
                background: transparent;
                color: #9aa2ac;
                border: none;
                border-radius: 4px;
                font-size: 14px;
            }
            QPushButton#miniClose:hover { background: rgba(255,255,255,0.16); color: white; }
        """)

        self._anim = QTimer(self)
        self._anim.timeout.connect(self._tick)
        self._anim.start(50)

        self._copy_flash = QTimer(self)
        self._copy_flash.setSingleShot(True)
        self._copy_flash.timeout.connect(
            lambda: self._btn_copy.setText("复制"))

        # 悬停片刻弹出放大预览（本次识别的输入截图）
        self._preview = _PreviewPopup(image)
        self._preview_timer = QTimer(self)
        self._preview_timer.setSingleShot(True)
        self._preview_timer.timeout.connect(self._show_preview)

        # 悬停检测用几何轮询（enter/leave 在子控件/原生提示窗场景不可靠）
        self._hover_poll = QTimer(self)
        self._hover_poll.setInterval(HOVER_POLL_MS)
        self._hover_poll.timeout.connect(self._poll_hover)
        self._hover_poll.start()

    # ------------------------------------------------------------- 状态切换
    def set_running(self):
        """回到识别中状态（结果窗内点「重试 / 提取信息」时由主控调用）。"""
        self._state = _STATE_RUNNING
        self._title = "识别中…" if self._mode == "formula" else "提取图片信息中…"
        self._subtitle = ""
        self._started = time.monotonic()
        self._elapsed = -1
        self.setWindowTitle("公式识别中" if self._mode == "formula" else "提取图片信息")
        self.setToolTip("识别中，完成后自动复制到剪贴板\n左键：打开结果窗口")
        self._btn_copy.hide()
        self._btn_edit.hide()
        self._btn_close.hide()
        self.setFixedSize(PILL_WIDTH, PILL_HEIGHT)
        self._anim.start(50)
        self.update()

    def set_success(self, result_text: str):
        """识别完成：✓ + 结果首行预览 + 复制/编辑按钮 + 右上角关闭。

        胶囊保持显示，由用户点 × 手动关闭（复制/编辑点击不关闭）。
        """
        self._state = _STATE_SUCCESS
        self._title = "完成，已复制到剪贴板"
        lines = [ln.strip() for ln in (result_text or "").splitlines() if ln.strip()]
        self._subtitle = lines[0] if lines else "（结果为空）"
        self.setWindowTitle("识别完成")
        self.setToolTip("识别完成，结果已自动复制到剪贴板\n"
                        "「复制」再复制一次 · 「编辑」查看/编辑 · × 关闭")
        self._anim.stop()
        self.setFixedSize(PILL_WIDTH, PILL_HEIGHT_SUCCESS)
        self._btn_copy.setGeometry(62, BTN_ROW_TOP, 58, BTN_HEIGHT)
        self._btn_edit.setGeometry(128, BTN_ROW_TOP, 58, BTN_HEIGHT)
        self._btn_close.setGeometry(PILL_WIDTH - 27, 5, CLOSE_SIZE, CLOSE_SIZE)
        self._btn_copy.show()
        self._btn_edit.show()
        self._btn_close.show()
        self.update()
        # 加高后可能贴近屏幕下缘：夹回可视范围
        scr = self.screen()
        if scr is not None:
            geo = scr.availableGeometry()
            bottom = self.mapToGlobal(QPoint(0, self.height())).y()
            if bottom > geo.bottom() - EDGE_MARGIN:
                self.move(self.x(), geo.bottom() - EDGE_MARGIN - self.height())

    def set_failed(self, err: str):
        """识别失败：✕ + 错误首行 + 右上角关闭，保持显示等待用户处理。"""
        self._state = _STATE_FAILED
        self._title = "识别失败"
        lines = [ln.strip() for ln in (err or "").splitlines() if ln.strip()]
        self._subtitle = lines[0] if lines else "未知错误"
        self.setWindowTitle("识别失败")
        self.setToolTip("识别失败\n左键：打开结果窗口重试 · × 关闭")
        self._anim.stop()
        self._btn_copy.hide()
        self._btn_edit.hide()
        self._btn_close.setGeometry(PILL_WIDTH - 27, 5, CLOSE_SIZE, CLOSE_SIZE)
        self._btn_close.show()
        self.update()

    def flash_copied(self):
        """「复制」点击后的就地反馈：按钮短暂显示“已复制✓”。"""
        self._btn_copy.setText("已复制✓")
        self._copy_flash.start(COPY_FLASH_MS)

    def show_at_side(self):
        """显示在光标所在屏幕的右侧中部（避开托盘气泡所在的右下角）。"""
        screen = QGuiApplication.screenAt(QCursor.pos())
        if screen is None:
            screen = QGuiApplication.primaryScreen()
        if screen is not None:
            geo = screen.availableGeometry()
            x = geo.right() - self.width() - EDGE_MARGIN
            y = geo.top() + max(EDGE_MARGIN,
                                (geo.height() - self.height()) // 2)
            self.move(x, y)
        self.show()
        self.raise_()

    # ------------------------------------------------------------- 动画/绘制
    def _tick(self):
        self._angle = (self._angle + 9) % 360
        elapsed = int(time.monotonic() - self._started)
        if elapsed != self._elapsed:
            self._elapsed = elapsed
            self._subtitle = f"已进行 {elapsed} 秒"
        self.update()

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        path = QPainterPath()
        path.addRoundedRect(QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5),
                            14, 14)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(_COLOR_BG_HOVER if self._hovered else _COLOR_BG)
        p.drawPath(path)
        p.setPen(QPen(_COLOR_BORDER, 1))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawPath(path)

        if not self._thumb.isNull():
            trect = QRectF(12, 10, self._thumb.width(), self._thumb.height())
            clip = QPainterPath()
            clip.addRoundedRect(trect, 8, 8)
            p.setPen(Qt.PenStyle.NoPen)
            p.setClipPath(clip)
            p.drawPixmap(trect.topLeft(), self._thumb)
            p.setClipping(False)
            self._paint_badge(p, QRectF(trect.right() - 13, trect.bottom() - 13,
                                        16, 16))
            text_x = trect.right() + 10
        else:
            self._paint_badge(p, QRectF(16, (self.height() - 16) / 2, 16, 16))
            text_x = 42

        text_w = self.width() - text_x - 12
        fm_t = QFontMetrics(self._title_font())
        fm_s = QFontMetrics(self._sub_font())
        p.setFont(self._title_font())
        p.setPen(_COLOR_TITLE)
        p.drawText(QRectF(text_x, 9, text_w, 21),
                   Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                   fm_t.elidedText(self._title, Qt.TextElideMode.ElideRight,
                                   int(text_w)))
        p.setFont(self._sub_font())
        p.setPen(_COLOR_SUB)
        p.drawText(QRectF(text_x, 30, text_w, 21),
                   Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                   fm_s.elidedText(self._subtitle, Qt.TextElideMode.ElideRight,
                                   int(text_w)))
        p.end()

    def _paint_badge(self, p: QPainter, zone: QRectF):
        """缩略图角标：识别中为旋转圆弧，完成/失败为绿✓/红!圆点。"""
        if self._state == _STATE_RUNNING:
            pen = QPen(_COLOR_SPIN, 2.6)
            pen.setCapStyle(Qt.PenCapStyle.RoundCap)
            p.setPen(pen)
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawArc(zone, int(-self._angle * 16), 110 * 16)
            return
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(_COLOR_OK if self._state == _STATE_SUCCESS else _COLOR_ERR)
        p.drawEllipse(zone)
        p.setPen(QColor("white"))
        f = QFont()
        f.setPixelSize(11)
        f.setBold(True)
        p.setFont(f)
        p.drawText(zone, Qt.AlignmentFlag.AlignCenter,
                   "✓" if self._state == _STATE_SUCCESS else "!")

    @staticmethod
    def _title_font() -> QFont:
        f = QFont()
        f.setPixelSize(12)
        f.setBold(True)
        return f

    @staticmethod
    def _sub_font() -> QFont:
        f = QFont()
        f.setPixelSize(11)
        return f

    # -------------------------------------------------------------- 交互
    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self._drag_offset = (event.globalPosition().toPoint()
                                 - self.frameGeometry().topLeft())
            self._moved = False
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if (self._drag_offset is not None
                and event.buttons() & Qt.MouseButton.LeftButton):
            self._moved = True
            self.move(self._clamped(
                event.globalPosition().toPoint() - self._drag_offset))

    def mouseReleaseEvent(self, event):
        # 右键不执行任何操作（防误关）；关闭一律走 × / 完成自动收起
        if event.button() == Qt.MouseButton.LeftButton:
            self._drag_offset = None
            if not self._moved:
                self.open_requested.emit()
        super().mouseReleaseEvent(event)

    def _clamped(self, pos: QPoint) -> QPoint:
        """拖动时把胶囊限制在虚拟桌面范围内，避免丢到屏幕外找不到。"""
        screens = QGuiApplication.screens()
        if not screens:
            return pos
        bound = screens[0].geometry()
        for s in screens[1:]:
            bound = bound.united(s.geometry())
        max_x = max(bound.left(), bound.right() - self.width() + 1)
        max_y = max(bound.top(), bound.bottom() - self.height() + 1)
        return QPoint(min(max(pos.x(), bound.left()), max_x),
                      min(max(pos.y(), bound.top()), max_y))

    def _poll_hover(self):
        """悬停检测（几何轮询）：进入时准备弹出预览，离开时收起预览。"""
        inside = QRect(self.mapToGlobal(QPoint(0, 0)),
                       self.size()).contains(QCursor.pos())
        if inside == self._hovered:
            return
        self._hovered = inside
        if inside:
            if not self._thumb.isNull() and self.isVisible():
                self._preview_timer.start(PREVIEW_DELAY_MS)
        else:
            self._preview_timer.stop()
            self._preview.hide()
        self.update()

    def moveEvent(self, event):
        if self._preview.isVisible():
            self._place_preview()      # 拖动/重排时预览跟随
        super().moveEvent(event)

    def hideEvent(self, event):
        self._preview.hide()
        super().hideEvent(event)

    # ------------------------------------------------------------ 放大预览
    def _show_preview(self):
        """悬停足够久后，在胶囊左侧（放不下则右侧）弹出放大预览浮窗。"""
        if self._thumb.isNull() or not self.isVisible():
            return
        self._preview.prepare()
        self._place_preview()
        self._preview.show()
        self._preview.raise_()

    def _place_preview(self):
        pv = self._preview
        scr = self.screen()
        if scr is None:
            return
        geo = scr.availableGeometry()
        x = self.x() - pv.width() - 10
        if x < geo.left() + 4:         # 胶囊已贴左缘：改贴右侧
            x = self.x() + self.width() + 10
        y = self.y() + self.height() // 2 - pv.height() // 2
        y = max(geo.top() + 4, min(y, geo.bottom() - pv.height() - 4))
        pv.move(x, y)

    def closeEvent(self, event):
        self._preview_timer.stop()
        self._hover_poll.stop()
        self._preview.close()
        self._anim.stop()
        self.closed.emit()
        super().closeEvent(event)


class _PreviewPopup(QWidget):
    """悬停放大预览浮窗：展示本次识别的输入截图（只读、不接收鼠标事件）。"""

    PAD = 8
    MAX_SIDE = 360                      # 预览最长边（逻辑像素）

    def __init__(self, image: QImage, parent: QWidget | None = None):
        super().__init__(parent)
        self._img = image
        self._pm: QPixmap | None = None
        self.setWindowFlags(Qt.WindowType.FramelessWindowHint
                            | Qt.WindowType.Tool
                            | Qt.WindowType.WindowStaysOnTopHint)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, True)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)

    def prepare(self):
        """按当前 DPI 计算缩放图与窗口尺寸（等比缩到最长边 MAX_SIDE）。"""
        img = self._img
        scale = min(1.0, self.MAX_SIDE / max(1, max(img.width(), img.height())))
        w = max(1, int(img.width() * scale))
        h = max(1, int(img.height() * scale))
        dpr = self.devicePixelRatioF() or 1.0
        pm = QPixmap.fromImage(img.scaled(
            int(w * dpr), int(h * dpr),
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation))
        pm.setDevicePixelRatio(dpr)
        self._pm = pm
        self.setFixedSize(w + self.PAD * 2, h + self.PAD * 2)

    def paintEvent(self, event):
        if self._pm is None:
            return
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        path = QPainterPath()
        path.addRoundedRect(QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5),
                            10, 10)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor(18, 20, 24, 245))
        p.drawPath(path)
        p.setPen(QPen(QColor(255, 255, 255, 36), 1))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawPath(path)
        p.drawPixmap(self.PAD, self.PAD, self._pm)
        p.end()
