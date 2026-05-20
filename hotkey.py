"""全局热键注册器。

职责：
1. 使用 Windows API（RegisterHotKey + WM_HOTKEY）注册系统级热键
2. 热键触发时发射 activated 信号
3. 零额外依赖，仅依赖 ctypes

边界：
- 只做「检测到快捷键 → 发信号」这一件事
- 不关心快捷键触发后做什么
"""

from __future__ import annotations

import ctypes
from ctypes import wintypes

from PySide6.QtCore import QAbstractNativeEventFilter, QObject, Signal
from PySide6.QtWidgets import QApplication

# ── Windows 常量 ──────────────────────────────────────────────

WM_HOTKEY = 0x0312

MOD_ALT = 0x0001
MOD_CONTROL = 0x0002
MOD_SHIFT = 0x0004
MOD_WIN = 0x0008
MOD_NOREPEAT = 0x4000

# 虚拟键码映射（字母/数字/功能键）
_VK_MAP: dict[str, int] = {
    "f1": 0x70, "f2": 0x71, "f3": 0x72, "f4": 0x73,
    "f5": 0x74, "f6": 0x75, "f7": 0x76, "f8": 0x77,
    "f9": 0x78, "f10": 0x79, "f11": 0x7A, "f12": 0x7B,
    "space": 0x20,
    "tab": 0x09,
    "enter": 0x0D,
    "escape": 0x1B,
    "backspace": 0x08,
    "delete": 0x2E,
    "up": 0x26, "down": 0x28, "left": 0x25, "right": 0x27,
    "home": 0x24, "end": 0x23,
    "pageup": 0x21, "pagedown": 0x22,
}

# MSG 结构体（用于解析 nativeEventFilter 中的消息）
class _MSG(ctypes.Structure):
    _fields_ = [
        ("hwnd", wintypes.HWND),
        ("message", wintypes.UINT),
        ("wParam", wintypes.WPARAM),
        ("lParam", wintypes.LPARAM),
        ("time", wintypes.DWORD),
        ("pt", wintypes.POINT),
    ]


def _modifiers_to_flags(modifiers: list[str]) -> int:
    """将 ['ctrl', 'shift'] 转为 Windows modifier flag。"""
    flags = MOD_NOREPEAT
    for mod in modifiers:
        m = mod.lower()
        if m == "ctrl" or m == "control":
            flags |= MOD_CONTROL
        elif m == "shift":
            flags |= MOD_SHIFT
        elif m == "alt":
            flags |= MOD_ALT
        elif m == "win" or m == "meta":
            flags |= MOD_WIN
    return flags


def _key_to_vk(key: str) -> int:
    """将键名字符串转为虚拟键码。

    字母 'a'..'z'、数字 '0'..'9'、功能键 'f1'..'f12' 等。
    """
    lower = key.lower()
    if lower in _VK_MAP:
        return _VK_MAP[lower]
    if len(key) == 1:
        # 单字符：直接取大写 ASCII
        return ord(key.upper())
    return ord(key.upper())  # 回退


# ═══════════════════════════════════════════════════════════════
# HotkeyManager
# ═══════════════════════════════════════════════════════════════

class HotkeyManager(QObject, QAbstractNativeEventFilter):
    """系统级全局热键管理。"""

    activated = Signal()  # 快捷键被按下

    def __init__(
        self,
        modifiers: list[str],
        key: str,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._mod_flags = _modifiers_to_flags(modifiers)
        self._vk = _key_to_vk(key)
        self._hotkey_id = id(self) & 0xFFFF  # 唯一 ID
        self._registered = False

    # ── 公开接口 ────────────────────────────────────────────────

    def register(self, target_hwnd: int | None = None) -> bool:
        """注册全局热键。

        target_hwnd: 接收 WM_HOTKEY 的窗口句柄。
        如果不传，自动从当前应用获取。
        """
        if self._registered:
            return True

        if target_hwnd is not None:
            hwnd = target_hwnd
        else:
            hwnd = self._get_hwnd()
        if hwnd is None:
            return False

        user32 = ctypes.windll.user32
        result = user32.RegisterHotKey(
            wintypes.HWND(hwnd),
            self._hotkey_id,
            self._mod_flags,
            self._vk,
        )
        if result == 0:
            return False

        QApplication.instance().installNativeEventFilter(self)
        self._registered = True
        return True

    def unregister(self) -> None:
        """注销全局热键。"""
        if not self._registered:
            return
        user32 = ctypes.windll.user32
        user32.UnregisterHotKey(None, self._hotkey_id)
        QApplication.instance().removeNativeEventFilter(self)
        self._registered = False

    def rebind(self, modifiers: list[str], key: str) -> bool:
        """重新绑定快捷键（先注销旧键，再注册新键）。"""
        was_registered = self._registered
        if self._registered:
            self.unregister()
        self._mod_flags = _modifiers_to_flags(modifiers)
        self._vk = _key_to_vk(key)
        if was_registered:
            return self.register()
        return True

    # ── nativeEventFilter ───────────────────────────────────────

    def nativeEventFilter(self, event_type: bytes, message) -> tuple[bool, int]:
        """捕获 WM_HOTKEY 消息，发射 activated 信号。

        message 类型：PySide2 传 int，PySide6 传 sip.voidptr。
        统一通过 int() 转为地址值再构造 MSG 指针。
        """
        msg_addr = int(message)  # 兼容两种类型
        msg = ctypes.cast(
            ctypes.c_void_p(msg_addr), ctypes.POINTER(_MSG)
        ).contents
        if msg.message == WM_HOTKEY and msg.wParam == self._hotkey_id:
            self.activated.emit()
        return False, 0

    # ── 内部 ────────────────────────────────────────────────────

    @staticmethod
    def _get_hwnd() -> int | None:
        """尝试从当前应用获取有效的 HWND。"""
        app = QApplication.instance()
        if app is None:
            return None
        # 优先取活动窗口
        w = app.activeWindow()
        if w is not None:
            return int(w.winId())
        # 回退：第一个顶层窗口
        widgets = app.topLevelWidgets()
        if widgets:
            return int(widgets[0].winId())
        return None
