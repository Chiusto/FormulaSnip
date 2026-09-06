"""全局热键：Windows 上用 RegisterHotKey 注册截图/贴图热键。

两组热键（各自带备用键降级链，注册失败自动尝试下一个）：
- 截图：F1（Snipaste 习惯）→ F2 → Ctrl+Shift+X
- 贴图：F3 → F4 → Ctrl+Shift+V
非 Windows 平台由托盘菜单触发。
"""
from __future__ import annotations

import ctypes
import ctypes.wintypes
import sys

from PySide6.QtCore import QAbstractNativeEventFilter, QCoreApplication, QObject, Signal

WM_HOTKEY = 0x0312
HOTKEY_ID_CAPTURE = 0x5EED   # 截图热键 ID
HOTKEY_ID_PIN = 0x5EEF       # 贴图热键 ID

# 虚拟键码
VK_F1 = 0x70
VK_F2 = 0x71
VK_F3 = 0x72
VK_F4 = 0x73
VK_X = 0x58
VK_V = 0x56

# RegisterHotKey 修饰位
MOD_CONTROL = 0x0002
MOD_SHIFT = 0x0004
MOD_NOREPEAT = 0x4000

# 候选热键（按优先级）
DEFAULT_CANDIDATES = [          # 截图：F1 → F2 → Ctrl+Shift+X
    ("F1", 0, VK_F1),
    ("F2", 0, VK_F2),
    ("Ctrl+Shift+X", MOD_CONTROL | MOD_SHIFT, VK_X),
]
PIN_CANDIDATES = [              # 贴图：F3 → F4 → Ctrl+Shift+V
    ("F3", 0, VK_F3),
    ("F4", 0, VK_F4),
    ("Ctrl+Shift+V", MOD_CONTROL | MOD_SHIFT, VK_V),
]


class _HotkeyNativeFilter(QAbstractNativeEventFilter):
    """把 WM_HOTKEY 消息转成 Qt 信号（按 hotkey_id 区分多组热键）。"""

    def __init__(self, owner: "GlobalHotkey", hotkey_id: int):
        super().__init__()
        self._owner = owner
        self._hotkey_id = hotkey_id

    def nativeEventFilter(self, eventType, message):  # noqa: N802
        if eventType == b"windows_generic_MSG":
            try:
                msg = ctypes.wintypes.MSG.from_address(int(message))
            except (TypeError, ValueError):
                return False, 0
            if msg.message == WM_HOTKEY and msg.wParam == self._hotkey_id:
                self._owner.triggered.emit()
                return True, 0
        return False, 0


def _register(hwnd: int, hotkey_id: int, fs_modifiers: int, vk: int) -> bool:
    """尝试注册；个别环境不支持 MOD_NOREPEAT 时去掉该修饰位重试。"""
    user32 = ctypes.windll.user32
    if user32.RegisterHotKey(hwnd, hotkey_id, fs_modifiers | MOD_NOREPEAT, vk):
        return True
    if user32.RegisterHotKey(hwnd, hotkey_id, fs_modifiers, vk):
        return True
    return False


class GlobalHotkey(QObject):
    """全局热键注册器。注册成功后 registered=True、key_name 为实际生效键名。

    同一个 hwnd 上可创建多个实例，用 hotkey_id 区分（如截图 0x5EED / 贴图 0x5EEF）。
    """

    triggered = Signal()

    def __init__(self, host_widget, candidates=None, parent=None,
                 hotkey_id: int = HOTKEY_ID_CAPTURE):
        super().__init__(parent)
        self.registered = False
        self.key_name: str | None = None
        self._hotkey_id = hotkey_id
        self._filter = None
        self._hwnd = None
        if sys.platform != "win32":
            return  # macOS/Linux：走托盘菜单触发
        app = QCoreApplication.instance()
        if app is None:
            return
        try:
            hwnd = int(host_widget.winId())
        except Exception:  # noqa: BLE001
            hwnd = 0
        if not hwnd:
            return
        for name, mods, vk in (candidates if candidates is not None
                               else DEFAULT_CANDIDATES):
            if _register(hwnd, hotkey_id, mods, vk):
                self.registered = True
                self.key_name = name
                self._hwnd = hwnd
                self._filter = _HotkeyNativeFilter(self, hotkey_id)
                app.installNativeEventFilter(self._filter)
                return

    def unregister(self):
        if self.registered and self._hwnd is not None:
            try:
                ctypes.windll.user32.UnregisterHotKey(self._hwnd, self._hotkey_id)
            except Exception:  # noqa: BLE001
                pass
            self.registered = False
