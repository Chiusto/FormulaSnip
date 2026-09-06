"""公式快截 FormulaSnip —— 按 F1 截屏，交给“你自己的模型”识别并输出公式。

用法
----
1. python -m pip install -r requirements.txt
2. python main.py            # 常驻系统托盘
3. 按 F1 框选屏幕上的公式区域 → 屏幕侧边迷你窗后台识别，完成后自动复制
   并气泡提示；左键点开迷你窗可查看/编辑结果后再复制。

借鉴 Snipaste 的交互：F1 触发 → 全屏选区遮罩（拖拽/缩放/微调）→ 确认后识别。
模型接入完全由用户自定义（OpenAI 兼容接口）。
"""
from __future__ import annotations

import logging
import sys
import traceback

from PySide6.QtCore import QLockFile, QObject, QRect, Qt, QTimer
from PySide6.QtGui import QColor, QFont, QGuiApplication, QIcon, QImage, QPainter, QPixmap
from PySide6.QtWidgets import (
    QApplication,
    QDialog,
    QMenu,
    QMessageBox,
    QSystemTrayIcon,
    QWidget,
)

from app import APP_NAME, __version__
from app.config import (
    DEFAULT_EXTRACT_PROMPT,
    AppConfig,
    ConfigStore,
    LOG_PATH,
    init_logging,
)
from app.hotkey import PIN_CANDIDATES, GlobalHotkey, HOTKEY_ID_PIN
from app.mini_result import EDGE_MARGIN, MiniResultWindow
from app.pin import PIN_BUS, open_pin, open_pin_text
from app.recognizer import (
    RecognizeTask,
    plan_auto_copy,
    track_task,
    wait_all_tasks,
)
from app.result_dialog import ResultDialog
from app.screenshot import CaptureOverlay, capture_desktop
from app.settings_dialog import SettingsDialog

LOG = logging.getLogger("formulasnip.main")


def make_app_icon() -> QIcon:
    pm = QPixmap(128, 128)
    pm.fill(Qt.GlobalColor.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(QColor("#2E6BFF"))
    p.drawRoundedRect(10, 10, 108, 108, 24, 24)
    f = QFont("Segoe UI Symbol")
    f.setPixelSize(74)
    f.setBold(True)
    p.setFont(f)
    p.setPen(QColor("white"))
    p.drawText(pm.rect(), Qt.AlignmentFlag.AlignCenter, "\u222b")  # ∫
    p.end()
    return QIcon(pm)


class _Flow:
    """一次识别的完整状态：胶囊 + 后台任务 + 结果。

    主控支持多个流程同时在屏：每个胶囊对应一条记录，完成后各自更新、
    互不干扰；胶囊点「编辑」不关闭（结果窗与胶囊同步状态），仅 × 关闭后
    该记录才移除（任务回调仍持有引用收尾）。
    """

    def __init__(self, seq: int, image: QImage, mode: str, cfg: AppConfig):
        self.seq = seq
        self.image = image
        self.mode = mode
        self.cfg = cfg
        self.text: str | None = None
        self.status = ""
        self.ok = True
        self.error: str | None = None
        self.copy_text: str | None = None   # 完成后自动复制的内容（复制按钮复用）
        self.mini: MiniResultWindow | None = None
        self.task: RecognizeTask | None = None


class FormulaSnipController(QObject):
    """应用主控：托盘常驻 + F1 全局热键，串联 截图 → 识别 → 结果。"""

    def __init__(self, app: QApplication):
        super().__init__()
        self._app = app
        self.store = ConfigStore()
        self._busy = False
        self._overlay: CaptureOverlay | None = None
        self._result: ResultDialog | None = None
        self._result_flow: _Flow | None = None      # 结果窗对应的识别流程
        # 识别胶囊：可多个同时在屏，屏幕右侧按创建顺序纵向排列
        self._flows: list[_Flow] = []
        self._flow_seq = 0
        self._pending_image: QImage | None = None   # 未配置模型时暂存的截图
        self._pins: list = []                       # 持有贴图窗口引用，防 GC

        # 隐藏宿主窗口：为全局热键提供原生句柄
        self.host = QWidget()
        self.host.setWindowTitle(APP_NAME)

        icon = make_app_icon()
        app.setWindowIcon(icon)
        self.host.setWindowIcon(icon)

        # 托盘
        self.tray = QSystemTrayIcon(icon, self.host)
        menu = QMenu()
        self.act_capture = menu.addAction("截图识别…")
        self.act_capture.triggered.connect(self.start_capture)
        menu.addAction("设置…", self.open_settings)
        menu.addSeparator()
        menu.addAction("退出", self._quit)
        self.tray.setContextMenu(menu)
        self.tray.activated.connect(self._on_tray_activated)
        self.tray.show()
        LOG.info("system tray shown")

        # 全局热键（Windows；被占用时自动改用备用键）
        self.hotkey = GlobalHotkey(self.host)
        self.hotkey.triggered.connect(self.start_capture)
        hk_name = self.hotkey.key_name or "F1"
        LOG.info("global hotkey registered = %s (%s)",
                 self.hotkey.registered, hk_name)
        self.act_capture.setText(f"截图识别（{hk_name}）")

        # F3 贴图热键：剪贴板有图贴图、有文字贴文字
        self.pin_hotkey = GlobalHotkey(self.host, PIN_CANDIDATES,
                                       hotkey_id=HOTKEY_ID_PIN)
        self.pin_hotkey.triggered.connect(self._on_pin_hotkey)
        pin_name = self.pin_hotkey.key_name or "F3"
        LOG.info("pin hotkey registered = %s (%s)",
                 self.pin_hotkey.registered, pin_name)

        menu.addAction("贴图（%s）" % pin_name, self._on_pin_hotkey)
        self.tray.setToolTip(
            f"{APP_NAME} v{__version__} —— {hk_name} 截屏识别 · {pin_name} 贴图")

        # 贴图右键「识别公式 / 提取图片信息」→ 走侧边迷你窗识别流程
        # （贴图保留在桌面；完成后自动复制，点开迷你窗查看/编辑）
        PIN_BUS.recognize_requested.connect(
            lambda img: self._start_recognize(img, mode="formula"))
        PIN_BUS.extract_requested.connect(
            lambda img: self._start_recognize(img, mode="extract"))

        # 启动气泡：告知生效热键 / 托盘位置 / 冲突提示
        if self.hotkey.registered:
            if hk_name == "F1" and pin_name == "F3":
                msg = ("已在后台运行：F1 框选截图识别公式，F3 把剪贴板"
                       "（图片或文字）贴到桌面。\n"
                       "若托盘图标在折叠区，点系统托盘 ^ 展开。")
            else:
                msg = (f"截图热键 {hk_name}、贴图热键 {pin_name}（有键被占用时"
                       "已自动换备用键）。仍想用 F1/F3，请关闭占用软件后重启。")
            self.tray.showMessage(APP_NAME, msg,
                                  QSystemTrayIcon.MessageIcon.Information,
                                  6000)

        # 首次运行引导
        QTimer.singleShot(500, self._first_run_check)

    # ------------------------------------------------------------- hot paths
    def _on_tray_activated(self, reason):
        if reason in (QSystemTrayIcon.ActivationReason.Trigger,
                      QSystemTrayIcon.ActivationReason.DoubleClick):
            self.start_capture()

    def start_capture(self):
        """按热键 / 托盘触发：无论是否已配置模型，都直接进入截图选区（Snipaste 手感）。"""
        if self._busy:
            return
        self._busy = True
        self._hide_minis()   # 胶囊不遮挡选区；只隐藏不关闭，截图结束后恢复
        try:
            shot = capture_desktop()
        except Exception as exc:  # noqa: BLE001
            self._busy = False
            self._restore_minis()
            QMessageBox.warning(None, "截图失败", str(exc))
            return
        overlay = CaptureOverlay(shot)
        overlay.accepted.connect(self._on_region_accepted)
        overlay.extract_requested.connect(self._on_extract_requested)
        overlay.copy_requested.connect(self._on_copy_requested)
        overlay.pin_requested.connect(self._on_pin_requested)
        overlay.canceled.connect(self._on_capture_canceled)
        self._overlay = overlay
        overlay.show_and_activate()

    def _on_region_accepted(self, local_rect: QRect):
        image = self._finish_overlay(local_rect)
        if image is None:
            return
        # 还没配置模型：截图先保留，配置完成后自动继续识别（不让用户白框选）
        if not self.store.load().is_ready():
            self._pending_image = image
            self._notify("请先配置模型接口",
                         "截图已保留，完成配置后将自动开始识别。")
            self.open_settings()
            return
        self._start_recognize(image)

    def _on_extract_requested(self, local_rect: QRect):
        """工具条「提取信息」：确认选区并直接开始提取图片信息。"""
        image = self._finish_overlay(local_rect)
        if image is None:
            return
        if not self.store.load().is_ready():
            self._pending_image = image
            self._notify("请先配置模型接口",
                         "截图已保留，完成配置后将自动开始识别。")
            self.open_settings()
            return
        self._start_recognize(image, mode="extract")

    # ------------------------------------------------------------------ F3 贴图
    def _on_pin_hotkey(self):
        """F3：剪贴板有图片贴图片，有文字渲染成文字贴图；都无则气泡提示。"""
        cb = self._app.clipboard()
        md = cb.mimeData()
        if md.hasImage():
            img = cb.image()
            if img is not None and not img.isNull():
                self._add_pin(open_pin(img))
                return
        text = (cb.text() or "").strip()
        if text:
            self._add_pin(open_pin_text(text))
            return
        self._notify("剪贴板为空",
                     "F3 贴图需要剪贴板里先有图片或文字（截图/复制后再按 F3）。")

    def _add_pin(self, win):
        self._pins.append(win)
        self._pins = [p for p in self._pins if p.isVisible()]

    def _on_capture_canceled(self):
        overlay, self._overlay = self._overlay, None
        if overlay is not None:
            overlay.close()
        self._busy = False
        self._restore_minis()

    # --------------------------------------------------- toolbar: copy / pin
    def _finish_overlay(self, local_rect: QRect) -> QImage | None:
        """关闭遮罩并裁出选区图片；返回 None 表示选区无效。"""
        overlay, self._overlay = self._overlay, None
        if overlay is None:
            self._busy = False
            self._restore_minis()
            return None
        image = overlay.crop_image(local_rect)
        overlay.close()
        self._busy = False
        self._restore_minis()
        if image.isNull() or image.width() < 2 or image.height() < 2:
            return None
        return image

    def _on_copy_requested(self, local_rect: QRect):
        """工具条「复制」：选区图片进剪贴板，贴图窗/识别流程均不启动。"""
        image = self._finish_overlay(local_rect)
        if image is None:
            return
        QGuiApplication.clipboard().setImage(image)
        self.tray.showMessage(
            "已复制图片",
            "截图已复制到剪贴板，可直接 Ctrl+V 粘贴。",
            QSystemTrayIcon.MessageIcon.Information, 4000)

    def _on_pin_requested(self, local_rect: QRect):
        """工具条「贴图」：选区图片直接贴到桌面（不识别）。"""
        image = self._finish_overlay(local_rect)
        if image is None:
            return
        self._pins.append(open_pin(image))
        self._pins = [p for p in self._pins if p.isVisible()]

    # ------------------------------------------------ 识别流程（侧边迷你窗）
    def _start_recognize(self, image: QImage, mode: str = "formula"):
        """识别入口：不直接弹大窗——收纳为屏幕侧边迷你胶囊并后台识别。

        可多个识别同时在屏：新胶囊在右侧按创建顺序往下排，互不打断；完成后
        各自自动复制并气泡提示；点胶囊「编辑」（或本体）展开完整结果窗查看/
        编辑，点「复制」把该胶囊的结果再复制一次。
        """
        self._flow_seq += 1
        cfg = self.store.load()
        flow = _Flow(self._flow_seq, image, mode, cfg)
        self._flows.append(flow)

        mini = MiniResultWindow(image, mode)
        mini.open_requested.connect(lambda m=mini: self._expand_result(m))
        mini.copy_requested.connect(lambda m=mini: self._copy_flow_result(m))
        mini.closed.connect(lambda m=mini: self._on_mini_closed(m))
        flow.mini = mini
        mini.show_at_side()
        self._relayout_minis()               # 整列重新排位（新胶囊排到最后）

        prompt = (DEFAULT_EXTRACT_PROMPT if mode == "extract"
                  else cfg.effective_prompt())
        task = RecognizeTask(cfg, image, prompt)
        task.succeeded.connect(lambda text, f=flow: self._on_flow_done(f, text))
        task.failed.connect(lambda err, f=flow: self._on_flow_fail(f, err))
        track_task(task)
        flow.task = task
        task.start()

    def _find_flow(self, mini: MiniResultWindow) -> _Flow | None:
        for f in self._flows:
            if f.mini is mini:
                return f
        return None

    def _on_flow_done(self, flow: _Flow, text: str):
        """识别成功回调：按配置自动复制，并同步胶囊 / 已展开的结果窗 / 气泡。"""
        copy_text, msg = plan_auto_copy(flow.cfg, flow.mode, text)
        if copy_text:
            QGuiApplication.clipboard().setText(copy_text)
        flow.copy_text = copy_text
        flow.text = text
        flow.status = msg
        flow.ok = copy_text is not None
        if flow.mini is not None:        # 截图期间被临时隐藏的胶囊也照常更新
            flow.mini.set_success(copy_text or text)
            self._relayout_minis()       # 完成态加高后整列重新排位
        if self._result_flow is flow and self._result is not None \
                and self._result.isVisible():
            self._result.show_result(text, msg, ok=flow.ok)
            return                       # 用户正看着该结果窗，不再重复气泡
        if not (copy_text or text).strip():
            self._notify("识别完成", msg)
            return
        preview = (copy_text or text).strip()
        preview = preview if len(preview) <= 80 else preview[:77] + "…"
        self.tray.showMessage("识别完成", f"已复制到剪贴板：\n{preview}",
                              QSystemTrayIcon.MessageIcon.Information, 6000)

    def _on_flow_fail(self, flow: _Flow, err: str):
        """识别失败回调：对应胶囊标红并允许重试，气泡给出完整错误。"""
        flow.error = err
        if flow.mini is not None:
            flow.mini.set_failed(err)
        if self._result_flow is flow and self._result is not None \
                and self._result.isVisible():
            self._result.show_error(err)
            return
        self.tray.showMessage("识别失败", err,
                              QSystemTrayIcon.MessageIcon.Warning, 6000)

    def _on_dialog_task_started(self, flow: _Flow):
        """结果窗内点「重试 / 提取信息」：对应胶囊回到识别中状态。"""
        if flow.mini is not None:
            flow.mini.set_running()
            self._relayout_minis()

    def _on_dialog_task_finished(self, flow: _Flow, ok: bool, payload: str):
        """结果窗自己发起的识别结束：同步胶囊状态（剪贴板由结果窗逻辑负责）。"""
        if ok:
            copy_text, msg = plan_auto_copy(flow.cfg, flow.mode, payload)
            flow.copy_text = copy_text
            flow.text = payload
            flow.status = msg
            flow.ok = copy_text is not None
            if flow.mini is not None:
                flow.mini.set_success(copy_text or payload)
                self._relayout_minis()
        else:
            flow.error = payload
            if flow.mini is not None:
                flow.mini.set_failed(payload)

    def _copy_flow_result(self, mini: MiniResultWindow):
        """胶囊「复制」按钮：把该次识别结果再次复制到剪贴板并就地反馈。"""
        flow = self._find_flow(mini)
        if flow is None or not flow.copy_text:
            return
        QGuiApplication.clipboard().setText(flow.copy_text)
        if mini.isVisible():
            mini.flash_copied()

    def _expand_result(self, mini: MiniResultWindow):
        """点胶囊「编辑」/本体：展开该次识别的完整结果窗（胶囊保持不关闭）。"""
        flow = self._find_flow(mini)
        if flow is None:
            return
        if self._result_flow is flow and self._result is not None \
                and self._result.isVisible():
            self._result.raise_()        # 已是该结果窗：只抬升
            self._result.activateWindow()
            return
        self._close_result_window()      # 结果窗一次只保留一个
        dlg = ResultDialog(flow.image, self.store.load(),
                           mode=flow.mode, autostart=False)
        dlg.recapture_requested.connect(self.start_capture)
        dlg.settings_requested.connect(self.open_settings)
        dlg.copied_automatically.connect(self._on_auto_copied)
        # 结果窗内点「重试 / 提取」时，把状态同步回该胶囊
        dlg.task_started.connect(lambda f=flow: self._on_dialog_task_started(f))
        dlg.task_finished.connect(
            lambda ok, payload, f=flow: self._on_dialog_task_finished(f, ok, payload))
        self._result = dlg
        self._result_flow = flow
        if flow.error is not None:
            dlg.show_error(flow.error)
        elif flow.text is not None:
            dlg.show_result(flow.text, flow.status, ok=flow.ok)
        elif flow.mode == "extract":
            dlg.show_running("正在提取图片信息，请稍候…（完成后自动复制，"
                             "本窗口会同步显示结果）")
        else:
            dlg.show_running("识别中，请稍候…（完成后自动复制，"
                             "本窗口会同步显示结果）")
        dlg.show()
        dlg.raise_()
        dlg.activateWindow()

    def _close_result_window(self):
        dlg, self._result = self._result, None
        self._result_flow = None
        if dlg is not None:
            dlg.close()
            dlg.deleteLater()

    def _on_mini_closed(self, mini: MiniResultWindow):
        """胶囊自行关闭（完成自动收起 / 点 × / 点开转入结果窗）后移出队列。"""
        for f in self._flows:
            if f.mini is mini:
                f.mini = None
                self._flows.remove(f)
                break
        self._relayout_minis()           # 其余胶囊自动补位

    def _relayout_minis(self):
        """把可见胶囊在屏幕右侧按创建顺序纵向居中排列，互不重叠。"""
        minis = [f.mini for f in self._flows
                 if f.mini is not None and f.mini.isVisible()]
        if not minis:
            return
        screen = (QGuiApplication.screenAt(minis[0].pos())
                  or QGuiApplication.primaryScreen())
        if screen is None:
            return
        geo = screen.availableGeometry()
        gap = 10
        total = sum(m.height() for m in minis) + gap * (len(minis) - 1)
        y = geo.top() + max(EDGE_MARGIN, (geo.height() - total) // 2)
        x = geo.right() - minis[0].width() - EDGE_MARGIN
        for m in minis:
            m.move(x, y)
            y += m.height() + gap

    def _hide_minis(self):
        """截图选区期间临时隐藏全部胶囊（不关闭，结束后恢复并重排）。"""
        for f in self._flows:
            if f.mini is not None and f.mini.isVisible():
                f.mini.hide()

    def _restore_minis(self):
        """截图结束后恢复被临时隐藏的胶囊并重新排列。"""
        changed = False
        for f in self._flows:
            if f.mini is not None and not f.mini.isVisible():
                f.mini.show()
                changed = True
        if changed:
            self._relayout_minis()

    def _on_auto_copied(self, text: str):
        """结果窗被关闭后识别才完成：托盘气泡告知结果已进剪贴板。"""
        preview = text if len(text) <= 80 else text[:77] + "…"
        self.tray.showMessage(
            "识别完成",
            f"结果已自动复制到剪贴板：\n{preview}",
            QSystemTrayIcon.MessageIcon.Information, 6000)

    # -------------------------------------------------------------- settings
    def open_settings(self):
        dlg = SettingsDialog(self.store.load(), self.host)
        if dlg.exec() == QDialog.DialogCode.Accepted:
            cfg = dlg.result_config()
            self.store.save(cfg)
            LOG.info("settings saved (ready=%s)", cfg.is_ready())
            if self._result is not None:
                self._result.update_config(cfg)     # 已打开的结果窗用上新配置
            self._resume_pending(cfg)               # 配置完成 → 继续识别暂存截图
        elif self._pending_image is not None:
            # 用户取消配置：放弃暂存截图并明确告知，不留“隐形状态”
            self._pending_image = None
            self._notify("已取消配置",
                         "本次截图未识别；可随时从托盘菜单打开设置。")

    def _resume_pending(self, cfg):
        """配置就绪后，继续识别此前因未配置而暂存的截图。"""
        img, self._pending_image = self._pending_image, None
        if img is not None and cfg.is_ready():
            self._start_recognize(img)

    def _first_run_check(self):
        cfg = self.store.load()
        if not cfg.is_ready():
            # 启动不弹任何窗口（同 Snipaste 的安静常驻）：仅托盘气泡引导
            LOG.info("first run: model not configured (silent start, tray only)")
            self._notify(
                "欢迎使用公式快截",
                "已在后台运行。右键托盘图标打开“设置”配置模型后，"
                "即可按热键截图识别公式。")
        elif sys.platform == "win32" and not self.hotkey.registered:
            LOG.warning("all hotkey candidates registration failed")
            self._notify("截图热键注册失败",
                         "F1 / F2 / Ctrl+Shift+X 均注册失败，可能被其他软件占用。"
                         "仍可通过托盘图标“截图识别”触发。")

    def _notify(self, title: str, msg: str):
        self.tray.showMessage(title, msg,
                              QSystemTrayIcon.MessageIcon.Information, 6000)

    # ------------------------------------------------------------------ quit
    def _quit(self):
        self._app.quit()

    def shutdown(self):
        """退出前收尾：等待后台识别线程结束、注销全局热键。"""
        wait_all_tasks(timeout_ms=None)
        if self._result is not None:
            self._result.shutdown()
        self.hotkey.unregister()
        self.pin_hotkey.unregister()


def _install_excepthook():
    """未捕获异常写入日志，避免 pythonw 后台启动时“静默死亡”。"""
    def hook(exc_type, exc, tb):
        LOG.critical("unhandled exception",
                     exc_info=(exc_type, exc, tb))
    sys.excepthook = hook


def main() -> int:
    init_logging()
    _install_excepthook()
    LOG.info("=== %s v%s starting (python %s) ===",
             APP_NAME, __version__, sys.version.split()[0])
    try:
        QApplication.setHighDpiScaleFactorRoundingPolicy(
            Qt.HighDpiScaleFactorRoundingPolicy.PassThrough)
        app = QApplication(sys.argv)
        app.setQuitOnLastWindowClosed(False)
        app.setApplicationName("formulasnip")
        app.setApplicationDisplayName(APP_NAME)

        # 单实例锁：已有实例在跑时直接退出，避免多实例抢热键/托盘混乱
        lock = QLockFile(str(LOG_PATH.parent / "app.lock"))
        if not lock.tryLock(0):
            LOG.warning("another instance is running, exit")
            QMessageBox.information(
                None, APP_NAME,
                "公式快截已在运行中（请查看系统托盘图标）。\n"
                "如需重启，请先在托盘菜单中退出旧实例。")
            return 0

        if not QSystemTrayIcon.isSystemTrayAvailable():
            LOG.error("system tray NOT available")
            QMessageBox.critical(None, APP_NAME,
                                 "系统托盘不可用，本工具需要托盘常驻运行。\n"
                                 f"日志：{LOG_PATH}")
            return 1

        controller = FormulaSnipController(app)
        app.aboutToQuit.connect(controller.shutdown)
        LOG.info("entering event loop")
        rc = app.exec()
        LOG.info("event loop exited, rc=%s", rc)
        return rc
    except Exception:  # noqa: BLE001
        LOG.exception("fatal startup error")
        try:
            QMessageBox.critical(
                None, APP_NAME,
                "公式快截启动失败，详情见日志：\n" + str(LOG_PATH))
        except Exception:  # noqa: BLE001
            pass
        return 1


if __name__ == "__main__":
    sys.exit(main())
