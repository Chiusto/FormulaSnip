"""识别结果窗口：缩略图 + 原文 + LaTeX 提取/复制 + 可选渲染预览。"""
from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QImage, QPixmap
from PySide6.QtWidgets import (
    QDialog,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from app import APP_NAME
from app.config import DEFAULT_EXTRACT_PROMPT, AppConfig
from app.latex_preview import preview_available, render_latex
from app.pin import PinWidget, open_pin
from app.recognizer import RecognizeTask, extract_latex, strip_latex_delims, track_task

STYLE_STATUS_OK = "color:#2e7d32;"
STYLE_STATUS_ERR = "color:#c62828;"
STYLE_STATUS_BUSY = "color:#555555;"


class ResultDialog(QDialog):
    """识别结果。打开后自动开始后台识别；窗口可随时关闭，任务会安全收尾。"""

    recapture_requested = Signal()   # “再截一张”
    settings_requested = Signal()    # “设置…”
    copied_automatically = Signal(str)  # 窗口已关闭时识别完成并自动复制了结果
    task_started = Signal()          # 窗口内发起了识别/提取（重试、提取按钮）
    task_finished = Signal(bool, str)  # 窗口内识别结束 (是否成功, 文本或错误)

    def __init__(self, image: QImage, cfg: AppConfig,
                 parent: QWidget | None = None, mode: str = "formula",
                 autostart: bool = True):
        super().__init__(parent)
        self._image = image
        self._cfg = cfg
        self._task: RecognizeTask | None = None
        self._task_mode = mode           # formula=识别公式 | extract=提取图片信息
        self._preview_img: QImage | None = None
        self._preview_shown = False
        self._has_matplotlib = preview_available()

        self.setWindowTitle(f"识别结果 - {APP_NAME}")
        self.resize(800, 580)
        self.setMinimumSize(560, 420)
        self.setWindowFlag(Qt.WindowType.WindowContextHelpButtonHint, False)

        root = QVBoxLayout(self)

        # 顶部：缩略图（可缩放/平移）+ 文本
        top = QHBoxLayout()
        self.img_view = PinWidget(image)
        self.img_view.setFixedSize(220, 170)
        self.img_view.setToolTip("滚轮缩放 · 拖动平移 · 双击在 100% 与适应间切换")
        top.addWidget(self.img_view)
        self.img_view.refit_now()

        self.text_edit = QPlainTextEdit()
        self.text_edit.setPlaceholderText("识别结果将显示在这里…（可手动编辑后再复制）")
        self.text_edit.setSizePolicy(QSizePolicy.Policy.Expanding,
                                     QSizePolicy.Policy.Expanding)
        top.addWidget(self.text_edit, 1)
        root.addLayout(top, 1)

        # 预览区
        self.preview_label = QLabel("公式渲染预览")
        self.preview_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.preview_label.setStyleSheet(
            "background:#ffffff;border:1px solid #dddddd;border-radius:6px;")
        self.preview_label.setMinimumHeight(96)
        self.preview_label.setVisible(False)
        root.addWidget(self.preview_label)

        # 状态
        self.status_label = QLabel()
        root.addWidget(self.status_label)

        # 按钮行
        btns = QHBoxLayout()
        self.preview_btn = QPushButton("渲染预览")
        self.retry_btn = QPushButton("重试")
        self.retry_btn.setToolTip("用当前设置重新识别这张截图（改完配置/提示词后可直接重试）")
        self.copy_latex_btn = QPushButton("复制 LaTeX")
        self.copy_full_btn = QPushButton("复制全文")
        self.extract_btn = QPushButton("提取图片信息")
        self.extract_btn.setToolTip(
            "用视觉模型把图中全部信息（文字/公式/表格/图表/界面）提取为结构化\n"
            "Markdown 文本并复制，供没有视觉能力的模型作为“视觉输入”使用")
        self.pin_btn = QPushButton("贴到桌面")
        self.pin_btn.setToolTip("把截图贴到屏幕上：可拖动、滚轮缩放、双击复制并关闭")
        btns.addWidget(self.preview_btn)
        btns.addWidget(self.retry_btn)
        btns.addWidget(self.copy_latex_btn)
        btns.addWidget(self.copy_full_btn)
        btns.addWidget(self.extract_btn)
        btns.addWidget(self.pin_btn)
        btns.addStretch(1)
        for text, sig in (("设置…", self.settings_requested),
                          ("再截一张", self.recapture_requested),
                          ("关闭", None)):
            b = QPushButton(text)
            if sig is not None:
                b.clicked.connect(sig.emit)
            else:
                b.clicked.connect(self.close)
            btns.addWidget(b)
        root.addLayout(btns)

        self.preview_btn.clicked.connect(self._toggle_preview)
        self.retry_btn.clicked.connect(self._start_recognition)
        self.copy_latex_btn.clicked.connect(self._copy_latex)
        self.copy_full_btn.clicked.connect(self._copy_full)
        self.extract_btn.clicked.connect(self._start_extract)
        self.pin_btn.clicked.connect(lambda: open_pin(self._image))

        self._set_actions_enabled(False)
        if not self._has_matplotlib:
            self.preview_btn.setEnabled(False)
            self.preview_btn.setToolTip("未安装 matplotlib，无法渲染预览")
        if autostart:
            if mode == "extract":
                self._start_extract()
            else:
                self._start_recognition()

    # ------------------------------------------------------------ recognition
    def _spawn_task(self, prompt: str):
        """启动一个后台识别任务并接线（公式识别与信息提取共用）。"""
        task = RecognizeTask(self._cfg, self._image, prompt)
        task.succeeded.connect(self._on_done)
        task.failed.connect(self._on_fail)
        track_task(task)
        self._task = task
        task.start()

    def _start_recognition(self):
        self._task_mode = "formula"
        self.retry_btn.setEnabled(False)
        self._set_actions_enabled(False)
        self._set_status("识别中，请稍候…（最长约 {} 秒）".format(self._cfg.timeout),
                         STYLE_STATUS_BUSY)
        self.task_started.emit()
        self._spawn_task(self._cfg.effective_prompt())

    def _start_extract(self):
        """提取图片信息：把图中全部内容转成结构化文本，供无视觉模型使用。"""
        self._task_mode = "extract"
        self.retry_btn.setEnabled(False)
        self._set_actions_enabled(False)
        self._set_status("正在提取图片信息，请稍候…（最长约 {} 秒）".format(self._cfg.timeout),
                         STYLE_STATUS_BUSY)
        self.task_started.emit()
        self._spawn_task(DEFAULT_EXTRACT_PROMPT)

    def _on_done(self, text: str):
        self.task_finished.emit(True, text)   # 供胶囊同步状态
        self._set_actions_enabled(True)
        self.retry_btn.setEnabled(True)
        self.text_edit.setPlainText(text)
        if self._task_mode == "extract":
            # 提取模式：输出即"视觉输入"文本，整体复制才有意义
            full = text.strip()
            if full:
                self._copy_to_clipboard(full)
                self._set_status("图片信息已提取并自动复制到剪贴板，"
                                 "可直接粘贴给无视觉能力的模型（可编辑后再复制）",
                                 STYLE_STATUS_OK)
            else:
                self._set_status("提取完成，但结果为空", STYLE_STATUS_ERR)
            if not self.isVisible() and full:
                self.copied_automatically.emit(full)
            return
        # 公式识别模式：核心用途是“粘贴使用”，识别完成即自动复制 LaTeX。
        # 携带原文模式：模型输出即"文字公式混排"的完整原文，整体复制才有意义
        if self._cfg.latex_with_text:
            full = text.strip()
            if full:
                self._copy_to_clipboard(full)
                self._set_status("识别完成（含原文），已自动复制到剪贴板（可直接 "
                                 "Ctrl+V；编辑后可再点“复制 LaTeX”）", STYLE_STATUS_OK)
            else:
                self._set_status("识别完成，但结果为空", STYLE_STATUS_ERR)
            # 预览仍取文中第一个公式渲染
            latex = extract_latex(text)
            if latex and self._has_matplotlib and not self._preview_shown:
                self._render_latex(strip_latex_delims(latex))
            if not self.isVisible() and full:
                self.copied_automatically.emit(full)
            return
        # 核心用途是“粘贴使用”：识别完成即自动复制 LaTeX，省一次点击。
        # latex_keep_delim=True 时按所选格式保留定界符（如 \(E=mc^2\)）。
        latex = extract_latex(text, keep_delims=self._cfg.latex_keep_delim)
        if latex:
            self._copy_to_clipboard(latex)
            self._set_status("识别完成，LaTeX 已自动复制到剪贴板（可直接 Ctrl+V；"
                             "编辑后可再点“复制 LaTeX”）", STYLE_STATUS_OK)
        else:
            self._set_status("识别完成。未找到 ```latex 代码块，可点“复制全文”",
                             STYLE_STATUS_OK)
        if latex and self._has_matplotlib and not self._preview_shown:
            self._render_latex(strip_latex_delims(latex))
        if not self.isVisible() and (latex or text):
            # 窗口已被用户关掉：结果不能凭空消失，托盘气泡会告知
            self._copy_to_clipboard(latex or text)
            self.copied_automatically.emit(latex or text)

    def _on_fail(self, err: str):
        self.task_finished.emit(False, err)   # 供胶囊同步状态
        self._set_actions_enabled(False)
        self.retry_btn.setEnabled(True)   # 失败后允许直接重试
        self.extract_btn.setEnabled(True)  # 信息提取也允许重新发起
        self._set_status(f"识别失败：{err}", STYLE_STATUS_ERR)
        if not self.isVisible():
            return
        box = QMessageBox(
            QMessageBox.Icon.Warning, "识别失败", err,
            QMessageBox.StandardButton.Ok, self)
        box.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, True)
        box.open()  # 非模态，不阻塞界面

    # --------------------------------------------- 外部回填（迷你窗流程点开后）
    def show_running(self, status: str):
        """外部识别仍在进行：窗口进入忙碌态，完成后由外部回填结果。"""
        self._set_actions_enabled(False)
        self.retry_btn.setEnabled(False)
        self._set_status(status, STYLE_STATUS_BUSY)

    def show_result(self, text: str, status: str, ok: bool = True):
        """外部识别成功：回填文本、启用编辑/复制，公式模式按需渲染预览。"""
        self._set_actions_enabled(True)
        self.retry_btn.setEnabled(True)
        self.text_edit.setPlainText(text)
        self._set_status(status, STYLE_STATUS_OK if ok else STYLE_STATUS_ERR)
        if (self._task_mode != "extract" and ok and not self._preview_shown
                and self._has_matplotlib):
            latex = extract_latex(text)
            if latex:
                self._render_latex(strip_latex_delims(latex))

    def show_error(self, err: str):
        """外部识别失败：显示错误并允许重试（按钮状态同 _on_fail）。"""
        self._set_actions_enabled(False)
        self.retry_btn.setEnabled(True)
        self.extract_btn.setEnabled(True)
        self._set_status(f"识别失败：{err}", STYLE_STATUS_ERR)

    # ---------------------------------------------------------------- actions
    def _copy_latex(self):
        text = self.text_edit.toPlainText()
        if self._cfg.latex_with_text:
            # 携带原文模式：复制完整混排原文（文字 + 按格式包裹的公式）
            if text.strip():
                self._copy_to_clipboard(text)
                self._set_status("原文（含公式）已复制到剪贴板", STYLE_STATUS_OK)
            else:
                self._set_status("没有可复制的内容", STYLE_STATUS_ERR)
            return
        latex = extract_latex(text, keep_delims=self._cfg.latex_keep_delim)
        if latex:
            self._copy_to_clipboard(latex)
            self._set_status("LaTeX 已复制到剪贴板", STYLE_STATUS_OK)
            if not self._preview_shown and self._has_matplotlib:
                self._render_latex(strip_latex_delims(latex))
        else:
            self._copy_to_clipboard(text)
            self._set_status("未找到 LaTeX 代码块（```latex 或 $..$），已复制全文",
                             STYLE_STATUS_ERR)

    def _copy_full(self):
        text = self.text_edit.toPlainText()
        if not text:
            return
        self._copy_to_clipboard(text)
        self._set_status("全文已复制到剪贴板", STYLE_STATUS_OK)

    @staticmethod
    def _copy_to_clipboard(text: str):
        from PySide6.QtWidgets import QApplication

        QApplication.clipboard().setText(text)

    def _toggle_preview(self):
        text = self.text_edit.toPlainText()
        latex = extract_latex(text, keep_delims=self._cfg.latex_keep_delim)
        if not latex:
            self._set_status("未找到可渲染的 LaTeX 片段", STYLE_STATUS_ERR)
            return
        if self._preview_shown:
            self.preview_label.setVisible(False)
            self.preview_btn.setText("渲染预览")
            self._preview_shown = False
            return
        self._render_latex(strip_latex_delims(latex))

    def _render_latex(self, latex: str):
        from PySide6.QtWidgets import QApplication

        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            img = render_latex(latex)
        finally:
            QApplication.restoreOverrideCursor()
        if img is None:
            self._set_status("预览渲染失败：该公式可能包含不常用宏（mathtext 限制），"
                             "LaTeX 文本不受影响", STYLE_STATUS_ERR)
            return
        self._preview_img = img
        pm = QPixmap.fromImage(img)
        max_w = max(self.width() - 80, 200)
        if pm.width() > max_w:
            pm = pm.scaledToWidth(max_w, Qt.TransformationMode.SmoothTransformation)
        if pm.height() > 260:
            pm = pm.scaledToHeight(260, Qt.TransformationMode.SmoothTransformation)
        self.preview_label.setPixmap(pm)
        self.preview_label.setVisible(True)
        self.preview_btn.setText("隐藏预览")
        self._preview_shown = True

    # ------------------------------------------------------------------ util
    def update_config(self, cfg: AppConfig):
        """设置保存后同步最新配置（重试时即用新配置/新提示词）。"""
        self._cfg = cfg

    def _set_actions_enabled(self, on: bool):
        for b in (self.copy_latex_btn, self.copy_full_btn,
                  self.preview_btn, self.extract_btn):
            b.setEnabled(on if b is not self.preview_btn or self._has_matplotlib else False)

    def _set_status(self, msg: str, style: str = ""):
        self.status_label.setText(msg)
        self.status_label.setStyleSheet(style)

    def shutdown(self, timeout_ms: int | None = None):
        """退出前调用：等待本窗口的后台识别线程收尾，避免线程仍在运行时对象被销毁。"""
        if self._task is not None and self._task.isRunning():
            self._task.wait(timeout_ms)
