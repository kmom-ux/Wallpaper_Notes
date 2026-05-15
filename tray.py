"""系统托盘控制器。

职责：
1. 创建 QSystemTrayIcon 并显示在系统托盘区
2. 管理右键菜单（显示/隐藏窗口、退出）
3. 发射信号供 app.py 绑定

边界：
- 仅发射信号，不控制窗口显示逻辑
- 不持有应用状态
"""

from __future__ import annotations

from PySide6.QtCore import QObject, Signal
from PySide6.QtGui import QAction, QIcon
from PySide6.QtWidgets import QApplication, QMenu, QSystemTrayIcon, QWidget


class TrayManager(QObject):
    """系统托盘图标与右键菜单。"""

    # ── 信号 ────────────────────────────────────────────────────

    show_requested = Signal()
    exit_requested = Signal()
    settings_requested = Signal()
    edit_requested = Signal()

    # ── 生命周期 ────────────────────────────────────────────────

    def __init__(self, parent_widget: QWidget, icon: QIcon | None = None) -> None:
        super().__init__(parent_widget)
        self._parent = parent_widget

        self._tray = QSystemTrayIcon(parent_widget)
        self._tray.setToolTip("Wallpaper_Notes")

        if icon is not None:
            self._tray.setIcon(icon)
        else:
            # 内置图标回退（Apple 风格 emoticon 或系统默认）
            self._tray.setIcon(
                QApplication.style().standardIcon(
                    QApplication.style().StandardPixmap.SP_FileDialogInfoView
                )
            )

        self._build_menu()
        self._tray.activated.connect(self._on_tray_activated)

    # ── 公开 ────────────────────────────────────────────────────

    def show(self) -> None:
        """显示托盘图标。"""
        self._tray.show()

    def hide(self) -> None:
        """隐藏托盘图标。"""
        self._tray.hide()

    def show_message(self, title: str, message: str) -> None:
        """显示气泡通知。"""
        self._tray.showMessage(
            title,
            message,
            QSystemTrayIcon.MessageIcon.Information,
            3000,  # ms
        )

    # ── 内部 ────────────────────────────────────────────────────

    def _build_menu(self) -> None:
        """构建右键菜单项并连接信号。"""
        menu = QMenu()

        action_show = QAction("显示/隐藏 (&S)", menu)
        action_show.triggered.connect(self._on_toggle_show)
        menu.addAction(action_show)

        action_edit = QAction("进入编辑 (&E)", menu)
        action_edit.triggered.connect(self.edit_requested.emit)
        menu.addAction(action_edit)

        menu.addSeparator()

        action_settings = QAction("设置 (&C)", menu)
        action_settings.triggered.connect(self.settings_requested.emit)
        menu.addAction(action_settings)

        menu.addSeparator()

        action_exit = QAction("退出 (&X)", menu)
        action_exit.triggered.connect(self.exit_requested.emit)
        menu.addAction(action_exit)

        self._tray.setContextMenu(menu)

    def _on_tray_activated(self, reason: QSystemTrayIcon.ActivationReason) -> None:
        """双击托盘图标时显示/隐藏窗口。"""
        if reason == QSystemTrayIcon.ActivationReason.DoubleClick:
            self._on_toggle_show()

    def _on_toggle_show(self) -> None:
        """切换窗口可见性。"""
        if self._parent.isVisible():
            self._parent.hide()
        else:
            self._parent.show()
            self._parent.raise_()
