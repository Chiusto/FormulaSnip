"""设置对话框：模型接入信息 + 自定义提示词 + 连通性测试。"""
from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QComboBox,
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from app.config import (
    DEFAULT_PROMPT,
    AppConfig,
    _LATEX_DELIM_LABELS,
    _LATEX_MULTILINE_LABELS,
)
from app.recognizer import ping_model


class SettingsDialog(QDialog):
    """编辑并保存 AppConfig。exec() 返回 Accepted 后取 result_config()。"""

    def __init__(self, cfg: AppConfig, parent: QWidget | None = None):
        super().__init__(parent)
        self._cfg = AppConfig.from_dict(cfg.to_dict())
        self.setWindowTitle("设置 - 模型接入")
        self.setMinimumWidth(560)
        self.setModal(True)

        root = QVBoxLayout(self)

        # ---- 接入表单
        form = QFormLayout()
        self.base_url_edit = QLineEdit(cfg.base_url)
        self.base_url_edit.setPlaceholderText("https://api.openai.com/v1")
        form.addRow("API 地址 base_url：", self.base_url_edit)

        self.api_key_edit = QLineEdit(cfg.api_key)
        self.api_key_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self.api_key_edit.setPlaceholderText("sk-...")
        form.addRow("API Key：", self.api_key_edit)

        # 模型：可编辑下拉框（可手填，也可点「获取模型列表」从服务端拉取）
        self.model_box = QComboBox()
        self.model_box.setEditable(True)
        self.model_box.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
        if cfg.model:
            self.model_box.addItem(cfg.model)
        self.model_box.setCurrentText(cfg.model)
        fetch_btn = QPushButton("获取模型列表")
        fetch_btn.setToolTip("从 API 地址拉取该服务可用的模型列表（GET /models）")
        fetch_btn.clicked.connect(self._on_fetch_models)
        self.fetch_btn = fetch_btn
        mrow = QHBoxLayout()
        mrow.addWidget(self.model_box, 1)
        mrow.addWidget(fetch_btn)
        form.addRow("模型 model：", mrow)

        self.style_box = QComboBox()
        self.style_box.addItem("OpenAI 兼容（/chat/completions）", "openai")
        self.style_box.addItem("OpenAI Responses（/responses）", "openai-responses")
        self.style_box.addItem("Anthropic（/v1/messages）", "anthropic")
        idx = max(0, self.style_box.findData((cfg.api_style or "openai").strip().lower()))
        self.style_box.setCurrentIndex(idx)
        form.addRow("API 协议：", self.style_box)

        self.proxy_edit = QLineEdit(cfg.proxy)
        self.proxy_edit.setPlaceholderText(
            "留空 = 跟随系统代理；direct = 强制直连；或填 http://127.0.0.1:7890")
        form.addRow("网络代理：", self.proxy_edit)
        root.addLayout(form)

        note = QLabel(
            "提示：模型必须支持图像输入（多模态视觉模型）。\n"
            "Key 会明文保存在 ~/.formulasnip/config.json，请注意保管。"
        )
        note.setStyleSheet("color:#8a8a8a;")
        root.addWidget(note)

        # ---- 高级参数（模型请求）
        adv = QGroupBox("高级参数")
        adv_form = QFormLayout(adv)
        self.max_tokens_spin = QSpinBox()
        self.max_tokens_spin.setRange(256, 32768)
        self.max_tokens_spin.setSingleStep(256)
        self.max_tokens_spin.setValue(int(cfg.max_tokens))
        self.max_tokens_spin.setToolTip("单次请求允许模型生成的最大 token 数")
        adv_form.addRow("最大输出 token：", self.max_tokens_spin)

        self.temp_spin = QDoubleSpinBox()
        self.temp_spin.setRange(0.0, 2.0)
        self.temp_spin.setSingleStep(0.1)
        self.temp_spin.setDecimals(2)
        self.temp_spin.setValue(float(cfg.temperature))
        self.temp_spin.setToolTip("越低越稳定（公式识别建议 0.0~0.2）")
        adv_form.addRow("temperature：", self.temp_spin)

        self.timeout_spin = QSpinBox()
        self.timeout_spin.setRange(10, 600)
        self.timeout_spin.setSuffix(" 秒")
        self.timeout_spin.setValue(int(cfg.timeout))
        self.timeout_spin.setToolTip("单次请求最长等待时间（读超时；连接超时固定 20 秒）")
        adv_form.addRow("请求超时：", self.timeout_spin)

        self.thinking_chk = QCheckBox("深度思考（推理模式，响应更慢、更费 token）")
        self.thinking_chk.setToolTip(
            "开启后按所选协议发送深度思考参数：\n"
            "· OpenAI 兼容：enable_thinking=true（通义 / vLLM / 硅基流动等）\n"
            "· OpenAI Responses：reasoning.effort=medium\n"
            "· Anthropic：thinking.type=enabled（预算为 max_tokens 的一半）\n"
            "识别数学公式建议保持关闭，除非使用的是推理类模型。")
        self.thinking_chk.setChecked(bool(cfg.enable_thinking))
        adv_form.addRow(self.thinking_chk)
        root.addWidget(adv)

        # ---- LaTeX 输出格式（自动注入提示词）
        fmt = QGroupBox("LaTeX 输出格式（自动注入提示词，优先于下方提示词中的格式描述）")
        fmt_form = QFormLayout(fmt)
        self.delim_box = QComboBox()
        for label, data in _LATEX_DELIM_LABELS:
            self.delim_box.addItem(label, data)
        di = max(0, self.delim_box.findData(cfg.latex_delim))
        self.delim_box.setCurrentIndex(di)
        fmt_form.addRow("公式包裹格式：", self.delim_box)

        self.multiline_box = QComboBox()
        for label, data in _LATEX_MULTILINE_LABELS:
            self.multiline_box.addItem(label, data)
        mi = max(0, self.multiline_box.findData(cfg.latex_multiline))
        self.multiline_box.setCurrentIndex(mi)
        fmt_form.addRow("多行公式处理：", self.multiline_box)

        self.keep_delim_chk = QCheckBox(
            "复制时保留定界符（如 \\(...\\)、$$...$$、\\[...\\]）")
        self.keep_delim_chk.setToolTip(
            "勾选：剪贴板里是带定界符的完整文本，如 \\(E=mc^2\\)；\n"
            "不勾选：只复制裸 LaTeX，如 E=mc^2。``` 代码块格式始终剥掉围栏。")
        self.keep_delim_chk.setChecked(bool(cfg.latex_keep_delim))
        fmt_form.addRow(self.keep_delim_chk)

        self.with_text_chk = QCheckBox(
            "携带原文输出（文字公式混排，公式按所选格式嵌在原位置）")
        self.with_text_chk.setToolTip(
            "勾选：输出截图中的完整原文，普通文字原样保留，数学公式按所选包裹格式\n"
            "（如 \\(...\\)）嵌在原位置，保持阅读顺序。适合识别整段文字笔记。\n"
            "不勾选：只输出公式本身。")
        self.with_text_chk.setChecked(bool(cfg.latex_with_text))
        fmt_form.addRow(self.with_text_chk)
        root.addWidget(fmt)

        # ---- 自定义提示词
        prompt_row = QHBoxLayout()
        prompt_label = QLabel("自定义提示词（输出格式由上方设置自动注入到末尾）：")
        prompt_row.addWidget(prompt_label, 1)
        reset_prompt_btn = QPushButton("恢复默认提示词")
        reset_prompt_btn.setToolTip("把提示词恢复为内置默认版本（格式规则仍由上方设置注入）")
        reset_prompt_btn.clicked.connect(
            lambda: self.prompt_edit.setPlainText(DEFAULT_PROMPT))
        prompt_row.addWidget(reset_prompt_btn)
        root.addLayout(prompt_row)
        self.prompt_edit = QPlainTextEdit(cfg.prompt)
        self.prompt_edit.setMinimumHeight(150)
        f = QFont("Microsoft YaHei UI")
        f.setPixelSize(13)
        self.prompt_edit.setFont(f)
        root.addWidget(self.prompt_edit)

        # ---- 按钮
        btns = QDialogButtonBox()
        self.test_btn = btns.addButton("测试连接", QDialogButtonBox.ButtonRole.ActionRole)
        btns.addButton("保存", QDialogButtonBox.ButtonRole.AcceptRole)
        btns.addButton("取消", QDialogButtonBox.ButtonRole.RejectRole)
        self.test_btn.clicked.connect(self._on_test)
        btns.accepted.connect(self._on_save)
        btns.rejected.connect(self.reject)
        root.addWidget(btns)

    # ------------------------------------------------------------- behaviors
    def _on_fetch_models(self):
        """从服务端拉取可用模型列表，填入模型下拉框。"""
        base = self.base_url_edit.text().strip()
        if not base:
            QMessageBox.warning(self, "提示", "请先填写 API 地址（base_url）。")
            return
        cfg = self.collect()
        self.fetch_btn.setEnabled(False)
        self.fetch_btn.setText("获取中…")
        try:
            from app.recognizer import list_models
            ids = list_models(cfg)
        except Exception as exc:  # noqa: BLE001
            ids = None
            QMessageBox.warning(
                self, "获取模型列表失败", str(exc))
        finally:
            self.fetch_btn.setEnabled(True)
            self.fetch_btn.setText("获取模型列表")
        if not ids:
            return
        current = self.model_box.currentText().strip()
        self.model_box.clear()
        self.model_box.addItems(ids)
        if current in ids:
            self.model_box.setCurrentText(current)
        QMessageBox.information(
            self, "获取模型列表",
            f"获取到 {len(ids)} 个模型，已在下拉框中列出。\n"
            "注意：请选择支持图像输入（多模态）的模型。")

    def collect(self) -> AppConfig:
        self._cfg.base_url = self.base_url_edit.text().strip()
        self._cfg.api_key = self.api_key_edit.text().strip()
        self._cfg.model = self.model_box.currentText().strip()
        self._cfg.api_style = self.style_box.currentData() or "openai"
        self._cfg.proxy = self.proxy_edit.text().strip()
        self._cfg.latex_delim = self.delim_box.currentData() or "codeblock"
        self._cfg.latex_multiline = self.multiline_box.currentData() or "lines"
        self._cfg.latex_keep_delim = self.keep_delim_chk.isChecked()
        self._cfg.latex_with_text = self.with_text_chk.isChecked()
        self._cfg.prompt = self.prompt_edit.toPlainText().strip()
        self._cfg.max_tokens = self.max_tokens_spin.value()
        self._cfg.temperature = self.temp_spin.value()
        self._cfg.timeout = self.timeout_spin.value()
        self._cfg.enable_thinking = self.thinking_chk.isChecked()
        return self._cfg

    def result_config(self) -> AppConfig:
        return self.collect()

    def _on_save(self):
        cfg = self.collect()
        if not cfg.base_url or not cfg.model:
            QMessageBox.warning(self, "提示", "请至少填写 API 地址与模型名称。")
            return
        if not cfg.api_key:
            QMessageBox.warning(self, "提示", "请填写 API Key。")
            return
        self.accept()

    def _on_test(self):
        cfg = self.collect()
        if not cfg.base_url or not cfg.api_key or not cfg.model:
            QMessageBox.warning(self, "提示", "请先填写完整的接入信息。")
            return
        self.test_btn.setEnabled(False)
        self.test_btn.setText("测试中…")
        ok, msg = ping_model(cfg)
        self.test_btn.setEnabled(True)
        self.test_btn.setText("测试连接")
        if ok:
            QMessageBox.information(self, "测试连接", msg)
        else:
            QMessageBox.warning(self, "测试连接失败", msg)
