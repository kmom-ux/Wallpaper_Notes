"""全局热键注册器。

职责：
1. 使用 Windows API RegisterHotKey 注册系统级热键
2. 注册在**窗口 HWND** 上（非线程队列），确保 WM_HOTKEY 经过
   窗口过程派发，使 SetForegroundWindow 获得完整前台权限

边界：
- 只做「注册/注销」这一件事
- WM_HOTKEY 的处理交由 WallpaperWindow.nativeEvent() 完成
"""

from __future__ import annotations

import ctypes
from ctypes import wintypes

from PySide6.QtCore import QObject

# ── Windows 常量 ──────────────────────────────────────────────

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
    """将键名字符串转为虚拟键码。"""
    lower = key.lower()
    if lower in _VK_MAP:
        return _VK_MAP[lower]
    return ord(key.upper())


# ═══════════════════════════════════════════════════════════════
# HotkeyManager
# ═══════════════════════════════════════════════════════════════

class HotkeyManager(QObject):
    """系统级全局热键管理。

    注册在全景窗口的 HWND 上，使 WM_HOTKEY 经过窗口过程派发，
    确保 SetForegroundWindow 在响应时拥有完整前台权限。
    """

    def __init__(
        self,
        modifiers: list[str],
        key: str,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._mod_flags = _modifiers_to_flags(modifiers)
        self._vk = _key_to_vk(key)
        self._hotkey_id = (id(self) % 0xBFFF) + 1
        self._registered = False
        self._hwnd: int | None = None  # 注册时所用的 HWND

    # ── 公开接口 ────────────────────────────────────────────────

    @property
    def hotkey_id(self) -> int:
        return self._hotkey_id

    def register(self, target_hwnd: int) -> bool:
        """注册全局热键在指定窗口 HWND 上。

        关键差异：HWND ≠ None → WM_HOTKEY 直接派发到窗口过程，
        非线程队列。SetForegroundWindow 在此期间获得前台权限。
        """
        if self._registered:
            return True

        user32 = ctypes.windll.user32
        result = user32.RegisterHotKey(
            target_hwnd,
            self._hotkey_id,
            self._mod_flags,
            self._vk,
        )
        if result == 0:
            return False

        self._hwnd = target_hwnd
        self._registered = True
        return True

    def unregister(self) -> None:
        """注销全局热键。"""
        if not self._registered:
            return
        user32 = ctypes.windll.user32
        user32.UnregisterHotKey(self._hwnd, self._hotkey_id)
        self._registered = False
        self._hwnd = None

    def rebind(self, modifiers: list[str], key: str, target_hwnd: int) -> bool:
        """重新绑定快捷键（先注销旧键，再注册新键）。"""
        was_registered = self._registered
        if self._registered:
            self.unregister()
        self._mod_flags = _modifiers_to_flags(modifiers)
        self._vk = _key_to_vk(key)
        if was_registered and target_hwnd:
            return self.register(target_hwnd)
        return True
