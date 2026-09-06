"""离屏冒烟测试：验证导入、抓屏对象、选区/裁剪、LaTeX 提取、渲染与对话框构造。

用法：QT_QPA_PLATFORM=offscreen python smoke_test.py
"""
from __future__ import annotations

import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


def main() -> int:
    from PySide6.QtCore import QPoint, QRect
    from PySide6.QtGui import QColor, QImage, QPainter
    from PySide6.QtWidgets import QApplication, QPushButton

    app = QApplication([])

    # 1) 各模块导入（同时覆盖语法/符号错误）
    from app.config import AppConfig, ConfigStore  # noqa: F401
    from app.hotkey import GlobalHotkey  # noqa: F401
    from app.latex_preview import preview_available, render_latex
    from app.recognizer import (  # noqa: F401
        encode_image, extract_latex, strip_latex_delims)
    from app.screenshot import CaptureOverlay, DesktopShot, capture_desktop
    from app.settings_dialog import SettingsDialog
    print("[ok] imports")

    # 2) LaTeX 提取
    assert extract_latex("说明：\n```latex\n\\frac{a}{b}+\\sqrt{x}\n```\n完") == "\\frac{a}{b}+\\sqrt{x}"
    assert extract_latex("$$\\int_0^1 x dx$$") == "\\int_0^1 x dx"
    assert extract_latex("\\[ a^2 \\]") == "a^2"
    assert extract_latex("\\( a^2 \\)") == "a^2"
    assert extract_latex("说明 \\(E=mc^2\\) 结束") == "E=mc^2"
    assert extract_latex("公式 $E=mc^2$ 很好") == "E=mc^2"
    assert extract_latex("没有公式") is None
    # keep_delims=True：保留定界符原样输出（多公式逐行）
    assert extract_latex("\\( a^2 \\)", keep_delims=True) == "\\( a^2 \\)"
    assert extract_latex("说明 \\(E=mc^2\\) 结束", keep_delims=True) == "\\(E=mc^2\\)"
    assert extract_latex(
        "前 \\(a+b\\) 中 \\(c-d\\) 后", keep_delims=True) == "\\(a+b\\)\n\\(c-d\\)"
    assert extract_latex("$$x^2$$", keep_delims=True) == "$$x^2$$"
    # 代码块格式始终剥围栏
    assert extract_latex("```latex\n\\frac{a}{b}\n```", keep_delims=True) == "\\frac{a}{b}"
    # strip_latex_delims：剥掉定界符（供渲染预览用）
    assert strip_latex_delims("\\(a^2\\)") == "a^2"
    assert strip_latex_delims("$$\\int_0^1 x dx$$") == "\\int_0^1 x dx"
    print("[ok] extract_latex")

    # 3) 构造一张带图案的测试图并测试编码
    img = QImage(320, 200, QImage.Format.Format_ARGB32_Premultiplied)
    img.fill(QColor("white"))
    p = QPainter(img)
    p.setPen(QColor("black"))
    p.drawText(img.rect(), 1, "E = mc^2")
    p.end()
    mime, b64 = encode_image(img, 1600)
    assert mime.startswith("image/") and b64
    print("[ok] encode_image ->", mime, f"{len(b64) // 1024} KB base64")

    # 4) 抓屏（offscreen 平台可能返回空白，但流程不应抛异常）
    try:
        shot = capture_desktop()
        print("[ok] capture_desktop ->", shot.geometry, "dpr=", shot.dpr,
              "img=", shot.image.size())
        # overlay 构造 + 模拟裁剪
        ov = CaptureOverlay(shot)
        ov.resize(shot.geometry.size())
        r = QRect(10, 10, 120, 80)
        cropped = ov.crop_image(r)
        print("[ok] overlay crop ->", cropped.size())
        # 工具条：初始隐藏；有选区时出现且不超屏；信号连通
        assert ov._toolbar.isHidden()
        copy_sig, pin_sig = [], []
        ov.copy_requested.connect(lambda q: copy_sig.append(q))
        ov.pin_requested.connect(lambda q: pin_sig.append(q))
        ov.sel = QRect(r)
        ov._place_toolbar()
        assert not ov._toolbar.isHidden()
        tgeo = ov._toolbar.geometry()
        assert ov.rect().adjusted(0, 0, -1, -1).contains(tgeo), tgeo
        ov._copy_clipboard()
        assert copy_sig and copy_sig[0] == QRect(r), copy_sig
        ov.sel = QRect(r)
        ov._pin_to_desktop()
        assert pin_sig and pin_sig[0] == QRect(r), pin_sig
        # 提取信息：图标按钮存在（悬停提示含"提取"）且信号连通
        ext_sig = []
        ov.extract_requested.connect(lambda q: ext_sig.append(q))
        tips = [b.toolTip() for b in ov._toolbar.findChildren(QPushButton)]
        assert any("提取" in t for t in tips), tips
        ov.sel = QRect(r)
        ov._extract_info()
        assert ext_sig and ext_sig[0] == QRect(r), ext_sig
        # ✕：退出整个截图（应发出 canceled 信号并关闭遮罩）
        cancel_sig = []
        ov.canceled.connect(lambda: cancel_sig.append(True))
        ov._discard_selection()
        assert cancel_sig, "canceled not emitted"
        assert ov._toolbar.isHidden() and not ov.isVisible()
        print("[ok] selection toolbar show/copy/pin/exit")
        # 渲染路径抓帧（应不抛异常）
        ov.show()
        app.processEvents()
        frame = ov.grab()
        assert not frame.isNull()
        ov.close()
        app.processEvents()
        print("[ok] overlay render ->", frame.size().width(),
              "x", frame.size().height())
    except Exception as exc:  # noqa: BLE001
        import traceback
        traceback.print_exc()
        print("[warn] capture path skipped:", repr(exc))

    # 5) 全局热键（Windows）注册/注销；F3 贴图候选链存在
    from PySide6.QtWidgets import QWidget
    from app.hotkey import PIN_CANDIDATES, HOTKEY_ID_PIN, HOTKEY_ID_CAPTURE
    assert PIN_CANDIDATES[0][0] == "F3"
    assert HOTKEY_ID_PIN != HOTKEY_ID_CAPTURE

    host = QWidget()
    hk = GlobalHotkey(host)
    print("[ok] GlobalHotkey registered =", hk.registered)
    hk.unregister()
    host.close()

    # 6) 设置对话框构造/取值
    cfg = AppConfig()
    d = SettingsDialog(cfg)
    c = d.collect()
    assert isinstance(c, AppConfig)
    d.close()
    # 内置默认提示词不含格式相关描述（格式一律由设置注入，避免冲突）；恢复按钮存在
    from app.config import DEFAULT_PROMPT
    assert "```" not in DEFAULT_PROMPT
    assert "代码块" not in DEFAULT_PROMPT and "逐行输出" not in DEFAULT_PROMPT
    assert "任何解释" not in DEFAULT_PROMPT
    assert DEFAULT_PROMPT.strip() in AppConfig().effective_prompt()
    print("[ok] SettingsDialog collect")

    # 6.5) LaTeX 格式注入：四种包裹格式 + 非法值回落 + align 注入
    from app.config import LATEX_DELIM_INSTR, LATEX_MULTILINE_INSTR
    for dv in LATEX_DELIM_INSTR:
        cfg2 = AppConfig(latex_delim=dv)
        assert LATEX_DELIM_INSTR[dv] in cfg2.effective_prompt()
    assert "围栏代码块" in AppConfig(latex_delim="bogus").effective_prompt()
    assert "align" in AppConfig(latex_multiline="align").effective_prompt()
    # 携带原文模式：注入混排指令，且不再包含"公式外无文字"与 align 指令
    from app.config import LATEX_DELIM_INSTR_WITH_TEXT
    for dv in LATEX_DELIM_INSTR_WITH_TEXT:
        p = AppConfig(latex_delim=dv, latex_with_text=True).effective_prompt()
        assert LATEX_DELIM_INSTR_WITH_TEXT[dv] in p
        assert "完整原文" in p
    p_paren = AppConfig(latex_delim="paren", latex_with_text=True).effective_prompt()
    assert "\\(...\\)" in p_paren
    assert LATEX_DELIM_INSTR["paren"] not in p_paren      # 不再用"公式外无文字"版指令
    assert LATEX_MULTILINE_INSTR["lines"] not in p_paren  # 混排模式下不注入多行公式指令
    assert LATEX_MULTILINE_INSTR["lines"] in AppConfig(latex_delim="paren").effective_prompt()
    print("[ok] latex format prompt injection")

    # 6.55) 深度思考参数按协议注入
    from app.recognizer import _endpoint_headers_payload
    base = dict(base_url="https://x/v1", api_key="k", model="m")
    _, _, p1 = _endpoint_headers_payload(
        AppConfig(**base), "hi", None, 60.0)
    assert "enable_thinking" not in p1                    # 默认关闭不发送
    _, _, p2 = _endpoint_headers_payload(
        AppConfig(**base, enable_thinking=True), "hi", None, 60.0)
    assert p2.get("enable_thinking") is True
    _, _, p3 = _endpoint_headers_payload(
        AppConfig(**base, api_style="openai-responses", enable_thinking=True),
        "hi", None, 60.0)
    assert p3.get("reasoning") == {"effort": "medium"}
    _, _, p4 = _endpoint_headers_payload(
        AppConfig(**base, api_style="anthropic", enable_thinking=True, max_tokens=4096),
        "hi", None, 60.0)
    assert p4["thinking"]["type"] == "enabled"
    assert p4["thinking"]["budget_tokens"] == 2048
    assert p4["max_tokens"] >= 2048 + 1024 and p4["temperature"] == 1
    print("[ok] thinking params per protocol")

    # 6.6) list_models：mock 服务 + 对话框模型下拉框
    import json as _json
    import threading
    from http.server import BaseHTTPRequestHandler, HTTPServer

    class MH(BaseHTTPRequestHandler):
        def do_GET(self):
            body = _json.dumps({"data": [{"id": "m-b"}, {"id": "m-a"},
                                          {"id": "m-a"}]}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass

    srv = HTTPServer(("127.0.0.1", 18768), MH)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        from app.recognizer import list_models
        mc = AppConfig(base_url="http://127.0.0.1:18768/v1", api_key="k",
                       proxy="direct")
        ids = list_models(mc)
        assert ids == ["m-a", "m-b"], ids       # 去重 + 排序
        dlg2 = SettingsDialog(mc)
        assert dlg2.model_box.isEditable()
        dlg2.close()
        print("[ok] list_models mock ->", ids)
    finally:
        srv.shutdown()

    # 7) matplotlib 渲染预览
    ok = preview_available()
    qi = render_latex("\\frac{a}{b}+\\sqrt{x^2+y^2}") if ok else None
    print("[ok] matplotlib available =", ok,
          "| render null =", qi is None)
    if qi is not None:
        print("     preview size:", qi.size())

    # 7.5) 贴图 / 缩放视图（PinWidget / PinWindow）
    from app.pin import PinWidget, open_pin

    pw = PinWidget(img)
    pw.resize(300, 200)
    pw.refit_now()
    pw.show()
    app.processEvents()
    pw.close()
    pin_win = open_pin(img)
    app.processEvents()
    assert pin_win.isVisible()
    print("[ok] PinWidget + PinWindow open ->", pin_win.size().width(), "x",
          pin_win.size().height())
    pin_win.close()
    app.processEvents()

    # 7.5) F3 文字贴图：文字渲染成图片 + 复制回原文
    from app.pin import text_to_pin_image, open_pin_text
    long_text = ("这是一个文字贴图测试。\n" * 12) + "行" * 600
    timg = text_to_pin_image(long_text)
    assert not timg.isNull() and timg.width() >= 60 and timg.height() >= 60
    assert timg.width() <= 520
    short_img = text_to_pin_image("短文本")
    assert not short_img.isNull() and short_img.width() >= 60
    tw = open_pin_text(long_text)
    tw.show()
    app.processEvents()
    assert tw.isVisible() and tw._copy_text == long_text
    tw._copy_back()          # 模拟双击复制（不关窗口以便断言）
    assert QApplication.clipboard().text() == long_text, "文字贴图应复制回原文"
    tw.close()
    app.processEvents()
    print("[ok] text pin ->", timg.width(), "x", timg.height())

    # 7.6) 贴图右键识别：PIN_BUS 信号发射（贴图 → 识别公式 / 提取信息）
    from app.pin import PIN_BUS
    rec_sig, ext_sig = [], []
    PIN_BUS.recognize_requested.connect(lambda im: rec_sig.append(im))
    PIN_BUS.extract_requested.connect(lambda im: ext_sig.append(im))
    pin_win._request_recognize()
    pin_win._request_extract()
    app.processEvents()
    assert len(rec_sig) == 1 and not rec_sig[0].isNull()
    assert len(ext_sig) == 1 and not ext_sig[0].isNull()
    print("[ok] pin context-menu recognize/extract bus ->",
          rec_sig[0].width(), "x", rec_sig[0].height())

    # 8) 端到端：结果窗口自动识别 → 连接失败路径 → 线程收尾
    from PySide6.QtCore import QTimer

    from app.recognizer import wait_all_tasks
    from app.result_dialog import ResultDialog

    cfg2 = AppConfig()
    cfg2.base_url = "http://127.0.0.1:1/v1"   # 立即连接失败
    cfg2.api_key = "sk-test"
    cfg2.model = "test-vl"
    cfg2.timeout = 8
    img = QImage(320, 120, QImage.Format.Format_ARGB32_Premultiplied)
    img.fill(QColor("white"))

    dlg = ResultDialog(img, cfg2)
    dlg.show()
    QTimer.singleShot(2500, app.quit)          # 事件循环跑 2.5s 后退出
    app.exec()
    assert "识别失败" in dlg.status_label.text(), dlg.status_label.text()
    wait_all_tasks(timeout_ms=10000)
    # 提取图片信息：按钮存在 + 内置提取提示词要素齐全
    from app.config import DEFAULT_EXTRACT_PROMPT
    assert dlg.extract_btn.text() == "提取图片信息"
    assert "\\(...\\)" in DEFAULT_EXTRACT_PROMPT
    assert "无视觉能力" in DEFAULT_EXTRACT_PROMPT
    dlg._task_mode = "extract"
    dlg._on_done("## 提取结果\n文字 \\(a^2\\)")
    assert dlg.text_edit.toPlainText().startswith("## 提取结果")
    assert "已提取" in dlg.status_label.text(), dlg.status_label.text()
    print("[ok] ResultDialog end-to-end ->", dlg.status_label.text()[:40])
    dlg.close()

    # 8.5) 迷你识别窗流程：自动复制决策 + 胶囊状态/点击 + 结果窗外部回填
    from PySide6.QtCore import Qt
    from PySide6.QtTest import QTest

    from app.mini_result import MiniResultWindow
    from app.recognizer import plan_auto_copy

    ct, msg = plan_auto_copy(AppConfig(), "formula", "```latex\nE=mc^2\n```")
    assert ct == "E=mc^2" and "已自动复制" in msg, (ct, msg)
    ct, msg = plan_auto_copy(AppConfig(latex_with_text=True), "formula",
                             "原文 \\(a\\) 与 \\(b\\)")
    assert ct == "原文 \\(a\\) 与 \\(b\\)", ct
    ct, msg = plan_auto_copy(AppConfig(), "extract", "## 提取\n内容")
    assert ct == "## 提取\n内容" and "提取" in msg
    ct, msg = plan_auto_copy(AppConfig(), "formula", "没有公式标记")
    assert ct == "没有公式标记" and "全文" in msg
    ct, msg = plan_auto_copy(AppConfig(), "formula", "  \n  ")
    assert ct is None and "为空" in msg
    print("[ok] plan_auto_copy")

    mini = MiniResultWindow(img, "formula")
    mini.show_at_side()
    app.processEvents()
    assert mini.isVisible()
    opened, copies = [], []
    mini.open_requested.connect(lambda: opened.append(1))
    mini.copy_requested.connect(lambda: copies.append(1))
    QTest.mouseClick(mini, Qt.MouseButton.LeftButton)       # 运行中点胶囊本体
    app.processEvents()
    assert opened == [1], opened
    mini.set_success("E=mc^2\n第二行")
    assert mini._state == "success" and mini._title.startswith("完成")
    assert mini.height() == 92                              # 完成态加高一行按钮
    assert mini._btn_copy.isVisible() and mini._btn_edit.isVisible()
    assert mini._btn_close.isVisible()
    QTest.mouseClick(mini._btn_copy, Qt.MouseButton.LeftButton)
    app.processEvents()
    assert copies == [1], copies                            # 复制按钮发出请求
    mini.flash_copied()
    assert mini._btn_copy.text() == "已复制✓"
    QTest.mouseClick(mini._btn_edit, Qt.MouseButton.LeftButton)
    app.processEvents()
    assert len(opened) == 2, opened                         # 编辑按钮 = 点开
    mini.set_failed("HTTP 500: bad gateway")
    assert mini._state == "failed" and "500" in mini._subtitle
    assert not mini._btn_copy.isVisible() and mini._btn_close.isVisible()
    QTest.mouseClick(mini, Qt.MouseButton.RightButton)      # 右键不再执行任何操作
    app.processEvents()
    assert mini.isVisible()
    mini._show_preview()                                    # 悬停放大预览
    app.processEvents()
    pv = mini._preview
    assert pv.isVisible() and pv.width() > mini.width()
    on_left = pv.x() + pv.width() <= mini.x() + 4           # 预览贴在胶囊一侧
    on_right = pv.x() >= mini.x() + mini.width() - 4
    assert on_left or on_right, (pv.x(), mini.x())
    pv.hide()
    mini.set_running()                                      # 重试时回到识别中
    assert mini._state == "running" and not mini._btn_copy.isVisible()
    assert mini.height() == 60
    QTest.mouseClick(mini._btn_close, Qt.MouseButton.LeftButton)
    app.processEvents()
    assert not mini.isVisible()                             # × 关闭胶囊
    print("[ok] MiniResultWindow states + buttons + preview")

    dlg3 = ResultDialog(img, cfg2, autostart=False)
    dlg3.show_result("```latex\nE=mc^2\n```", "识别完成，LaTeX 已自动复制到剪贴板")
    assert dlg3.text_edit.toPlainText() == "```latex\nE=mc^2\n```"
    assert "已自动复制" in dlg3.status_label.text()
    assert dlg3.copy_latex_btn.isEnabled()
    dlg3.close()
    dlg4 = ResultDialog(img, cfg2, mode="extract", autostart=False)
    dlg4.show_error("HTTP 500: bad gateway")
    assert "识别失败" in dlg4.status_label.text()
    assert dlg4.retry_btn.isEnabled() and dlg4.extract_btn.isEnabled()
    dlg4.close()
    print("[ok] ResultDialog external fill (show_result/show_error)")

    # 8.6) 端到端：迷你窗识别流程（mock 服务）→ 自动复制 → 多胶囊按序 → 点开展开
    import json as _json2
    from http.server import BaseHTTPRequestHandler as _BRH2

    class _ChatMH(_BRH2):
        def do_POST(self):
            n = int(self.headers.get("Content-Length", 0))
            self.rfile.read(n)
            body = _json2.dumps(
                {"choices": [{"message": {"content": "```latex\nE=mc^2\n```"}}]}
            ).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass

    srv2 = HTTPServer(("127.0.0.1", 0), _ChatMH)
    threading.Thread(target=srv2.serve_forever, daemon=True).start()
    try:
        import main as _main
        from PySide6.QtCore import QObject as _QObject, QTimer as _QTimer
        from PySide6.QtWidgets import QSystemTrayIcon as _Tray

        class _FakeStore:
            def __init__(self, cfg):
                self._cfg = cfg

            def load(self):
                return self._cfg

        ctrl = _main.FormulaSnipController.__new__(_main.FormulaSnipController)
        _QObject.__init__(ctrl)
        cfg3 = AppConfig(base_url=f"http://127.0.0.1:{srv2.server_address[1]}/v1",
                         api_key="k", model="mock-vl", proxy="direct", timeout=5)
        ctrl.store = _FakeStore(cfg3)
        ctrl.tray = _Tray()          # 真托盘不可用/不显示：showMessage 安全
        ctrl._result = None
        ctrl._result_flow = None
        ctrl._flows = []
        ctrl._flow_seq = 0

        ctrl._start_recognize(img, "formula")
        flow_a = ctrl._flows[-1]
        assert flow_a.mini.isVisible() and flow_a.mini._state == "running"

        phase = {"n": 0, "fb": None}
        loop = _QTimer()
        loop.setSingleShot(True)
        loop.timeout.connect(app.quit)
        loop.start(8000)
        poll = _QTimer()
        poll.setInterval(50)

        def _check():
            if phase["n"] == 0:
                if flow_a.text is None:
                    return
                phase["n"] = 1
                assert QApplication.clipboard().text() == "E=mc^2"  # 完成即自动复制
                assert flow_a.mini._state == "success"
                QTest.mouseClick(flow_a.mini._btn_copy, Qt.MouseButton.LeftButton)
                app.processEvents()
                assert QApplication.clipboard().text() == "E=mc^2"  # 复制按钮再复制
                assert flow_a.mini._btn_copy.text() == "已复制✓"    # 就地反馈
                assert flow_a.mini.isVisible()                      # 复制不关闭胶囊
                ctrl._start_recognize(img, "formula")   # 第二个识别：按序往下排
                fb = ctrl._flows[-1]
                phase["fb"] = fb
                assert len(ctrl._flows) == 2
                assert fb.mini.y() >= flow_a.mini.y() + flow_a.mini.height()
                return
            fb = phase["fb"]
            if fb is None:
                return
            if phase["n"] == 1:
                if fb.text is None:
                    return
                phase["n"] = 2
                assert fb.mini._state == "success"
                ctrl._expand_result(fb.mini)        # 点开 B：展开完整结果窗
                assert ctrl._result.text_edit.toPlainText() == "```latex\nE=mc^2\n```"
                assert "已自动复制" in ctrl._result.status_label.text()
                assert ctrl._result_flow is fb
                assert fb in ctrl._flows and fb.mini.isVisible()   # 胶囊保持显示
                QTest.mouseClick(ctrl._result.retry_btn, Qt.MouseButton.LeftButton)
                app.processEvents()
                assert fb.mini._state == "running"  # 重试状态同步回胶囊
                return
            # phase 2：结果窗内重试完成后，状态同步回胶囊
            if fb.mini is not None and fb.mini._state == "success":
                poll.stop()
                assert QApplication.clipboard().text() == "E=mc^2"
                print("[ok] mini-window flow end-to-end (stacking + mock server)")
                app.quit()

        poll.timeout.connect(_check)
        poll.start()
        app.exec()
    finally:
        srv2.shutdown()

    print("SMOKE PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
