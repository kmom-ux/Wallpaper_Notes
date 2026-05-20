"""主窗口 — 便签的视觉载体。

职责：
1. 管理 QTabWidget + QStackedWidget（显示/编辑双层）
2. 无边框窗口的拖拽移动与边缘缩放
3. 桌面层 ↔ 置顶层切换
4. 将 UI 事件翻译为回调，不直接操作文件系统

架构：
  WallpaperWindow(QWidget)
  └── QTabWidget（标签栏）
      └── 每个标签页 = QStackedWidget
          ├── [0] QTextBrowser   —— 显示模式
          └── [1] QPlainTextEdit —— 编辑模式

边界：
- 不直接操作文件（通过 callbacks）
- 不管理快捷键（WM_HOTKEY 由 window.nativeEvent 处理）
"""

from __future__ import annotations

import ctypes
import random
from ctypes import wintypes
from pathlib import Path
from typing import Any, Callable

from PySide6.QtCore import QEvent, QPoint, QRect, QSize, Qt, QTimer
from PySide6.QtGui import (
    QAction,
    QColor,
    QIcon,
    QImage,
    QPainter,
    QPainterPath,
    QPalette,
    QPixmap,
    QRegion,
)
from PySide6.QtWidgets import (
    QApplication,
    QGraphicsEffect,
    QHBoxLayout,
    QInputDialog,
    QMenu,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QStackedWidget,
    QTabWidget,
    QTextBrowser,
    QVBoxLayout,
    QWidget,
)

from config import Config
from renderer import build_css, render
from ui_components import (
    build_global_qss,
    generate_content_qss,
    generate_editor_qss,
)

# ── Windows 常量 ──────────────────────────────────────────────

_SWP_NOMOVE = 0x0002
_SWP_NOSIZE = 0x0001
_HWND_NOTOPMOST = -2
_HWND_BOTTOM = 1
_RESIZE_MARGIN = 8  # 边缘缩放触发距离 (px)

# WM_NCHITTEST 返回值
_HTCLIENT = 1
_HTLEFT = 10
_HTRIGHT = 11
_HTTOP = 12
_HTTOPLEFT = 13
_HTTOPRIGHT = 14
_HTBOTTOM = 15
_HTBOTTOMLEFT = 16
_HTBOTTOMRIGHT = 17

_WM_NCHITTEST = 0x84


class TextGlowEffect(QGraphicsEffect):
    """文字辉光效果：基于文字自身颜色，按距离变暗。

    原理：
    1. 把原始内容（文字）画到临时 QImage（背景透明）
    2. 用 box blur（缩小→放大）生成辉光层
    3. 先画辉光层，再画清晰文字在上面

    box blur 的天然特性：文字颜色向周围扩散，
    距离越远颜色越暗（强度越小），恰好实现
    「基于文字颜色 + 按距离变暗」的发光效果。
    """

    def __init__(self, blur_radius: int = 3, parent=None):
        super().__init__(parent)
        self._blur_radius = blur_radius

    def set_blur_radius(self, radius: int) -> None:
        self._blur_radius = radius
        self.update()

    def draw(self, painter: QPainter) -> None:
        rect = self.boundingRect()
        size = rect.size().toSize()
        if size.width() <= 0 or size.height() <= 0:
            self.drawSource(painter)
            return

        # 1. 画原始内容到临时 image（背景透明，只有文字）
        src = QImage(size, QImage.Format.Format_ARGB32_Premultiplied)
        src.fill(Qt.GlobalColor.transparent)
        src_painter = QPainter(src)
        src_painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        self.drawSource(src_painter)
        src_painter.end()

        # 2. box blur：缩小→放大模拟高斯模糊
        if self._blur_radius > 0:
            w, h = src.width(), src.height()
            ratio = max(1, self._blur_radius * 2 + 1)
            small_w = max(1, w // ratio)
            small_h = max(1, h // ratio)
            if small_w > 0 and small_h > 0:
                blurred = src.scaled(
                    small_w, small_h,
                    Qt.AspectRatioMode.IgnoreAspectRatio,
                    Qt.TransformationMode.SmoothTransformation,
                ).scaled(
                    w, h,
                    Qt.AspectRatioMode.IgnoreAspectRatio,
                    Qt.TransformationMode.SmoothTransformation,
                )
                # 3. 先画辉光层
                painter.setCompositionMode(
                    QPainter.CompositionMode.CompositionMode_SourceOver
                )
                painter.drawImage(rect.topLeft(), blurred)

        # 4. 再画原始清晰文字
        painter.drawImage(rect.topLeft(), src)


# 磨砂颗粒等级 → 实际像素大小映射
_FROST_GRAIN_MAP = {1: 1, 2: 2, 3: 4}

def _frost_grain_pixels(level: int) -> int:
    """将磨砂颗粒等级(1-3)映射为实际 grain 像素值。"""
    return _FROST_GRAIN_MAP.get(level, 1)


class FrostedNoiseWidget(QWidget):
    """磨砂噪点覆盖层——浮在 QStackedWidget 上的噪点纹理。

    intensity 控制灰度扩散范围（明暗对比），强度越大颗粒明暗差异越明显。
    鼠标事件穿透到下层 viewer/editor。

    性能优化：
    resize 时噪点重生成有 150ms 防抖，拖拽缩放期间只拉伸旧缓存，
    停止缩放后才按最终尺寸重新计算——避免每个像素变化都触发昂贵的像素级随机运算。
    """

    def __init__(
        self,
        parent: QWidget | None = None,
        intensity: float = 0.20,
        grain: int = 1,
    ) -> None:
        super().__init__(parent)
        self._intensity = max(0.0, min(intensity, 0.5))
        self._grain = max(1, grain)
        self._noise_cache: QImage | None = None
        self._cache_size: QSize = QSize(0, 0)
        self.setAttribute(Qt.WA_TransparentForMouseEvents, True)

        # ── resize 防抖：拖拽缩放时不立即重算，停稳后再算 ──
        self._debounce_timer = QTimer(self)
        self._debounce_timer.setSingleShot(True)
        self._debounce_timer.timeout.connect(self._regenerate_noise)
        self._pending_size: QSize = QSize(0, 0)

    def set_params(self, intensity: float, grain: int) -> None:
        """参数变化——立即重算，不走防抖（用户调整后希望立刻看到效果）。"""
        self._intensity = max(0.0, min(intensity, 0.5))
        self._grain = max(1, grain)
        self._noise_cache = None
        self._pending_size = QSize(0, 0)
        self._debounce_timer.stop()
        self.update()

    def paintEvent(self, event) -> None:  # type: ignore[override]
        if self._intensity <= 0:
            return
        sz = self.size()
        if sz.width() <= 0 or sz.height() <= 0:
            return

        if self._noise_cache is not None and self._cache_size == sz:
            # cache 命中：直接绘制
            self._draw_cache(sz)
            return

        # 尺寸变化：记录待算尺寸，启动/刷新防抖
        self._pending_size = sz
        if not self._debounce_timer.isActive():
            self._debounce_timer.start(150)  # 150ms 内稳定才重算

        if self._noise_cache is not None:
            # 有旧缓存：拉伸显示（噪点纹理拉伸基本看不出差别）
            self._draw_cache(sz)
        else:
            # 无缓存（首次绘制）：立即生成
            self._noise_cache = self._generate_noise(sz)
            self._cache_size = sz
            self._draw_cache(sz)

    def _regenerate_noise(self) -> None:
        """防抖到期：按最终待算尺寸重新生成噪点。"""
        if self._pending_size.width() <= 0 or self._pending_size.height() <= 0:
            return
        sz = self._pending_size
        self._noise_cache = self._generate_noise(sz)
        self._cache_size = sz
        self._pending_size = QSize(0, 0)
        self.update()

    def _draw_cache(self, sz: QSize) -> None:
        """绘制当前缓存的噪点图，必要时拉伸到控件当前尺寸。"""
        if self._noise_cache is None:
            return
        painter = QPainter(self)
        opacity = min(self._intensity * 1.2, 0.4)
        painter.setOpacity(opacity)
        # drawImage(dstRect, src, srcRect)：src→dst 自动拉伸
        painter.drawImage(
            QRect(0, 0, sz.width(), sz.height()),
            self._noise_cache,
            QRect(0, 0, self._cache_size.width(), self._cache_size.height()),
        )

    def _generate_noise(self, size: QSize) -> QImage:
        """生成磨砂噪点纹理。

        intensity 控制灰度扩散范围：
        - 低强度 → 灰度集中在 128 附近（对比度低，颗粒感弱）
        - 高强度 → 灰度分布在 0-255 全范围（对比度高，颗粒感强）
        """
        spread = int((self._intensity / 0.5) * 127)  # 0 → 0, 0.5 → 127
        lo = max(0, 128 - spread)
        hi = min(255, 128 + spread)

        img = QImage(size, QImage.Format_ARGB32_Premultiplied)
        img.fill(Qt.GlobalColor.transparent)
        p = QPainter(img)
        g = self._grain
        if hi <= lo:
            # 强度接近 0，全灰，快速填充
            p.fillRect(0, 0, size.width(), size.height(), QColor(128, 128, 128, 255))
        else:
            for y in range(0, size.height(), g):
                for x in range(0, size.width(), g):
                    v = random.randint(lo, hi)
                    p.fillRect(x, y, g, g, QColor(v, v, v, 255))
        p.end()
        return img


def _build_glass_qss(glass_opacity: float) -> str:
    """生成假毛玻璃背景的 QSS（半透对角渐变）。"""
    o = max(0.0, min(glass_opacity, 0.5))
    a_max = round(o * 255)
    a_mid = round(o * 0.6 * 255)
    a_min = round(o * 0.3 * 255)
    if a_max < 1:
        return ""
    return (
        "  background: qlineargradient(x1:0, y1:0, x2:1, y2:1,"
        f"    stop:0 rgba(255,255,255,{a_max}),"
        f"    stop:0.35 rgba(255,255,255,{a_mid}),"
        f"    stop:0.65 rgba(255,255,255,{a_min}),"
        f"    stop:1 rgba(255,255,255,{a_mid}));"
    )


# ═══════════════════════════════════════════════════════════════
# WallpaperWindow
# ═══════════════════════════════════════════════════════════════

class WallpaperWindow(QWidget):
    """无边框桌面便签主窗口。"""

    # 内部信号（非公开 API）


    def __init__(
        self,
        callbacks: dict[str, Callable],
        config: Config,
        theme: dict[str, Any],
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._config = config
        self._theme = theme
        self._callbacks = callbacks

        # 拖拽状态
        self._drag_offset: QPoint | None = None
        self._resize_edges: set[str] = set()  # {'top', 'left', ...}

        # 标签栏拖拽跟踪（仅在标签栏和 ⊕ 按钮区域生效）
        self._tab_drag_press_pos: QPoint | None = None
        self._tab_drag_engaged: bool = False

        # ⊕ 按钮引用（eventFilter 需要）
        self._btn_add: QPushButton | None = None

        # 磨砂噪点覆盖层（按 QStackedWidget id 索引）
        self._noise_widgets: dict[int, FrostedNoiseWidget] = {}



        # 窗口圆角半径（用于 setMask 剪辑，WA_TranslucentBackground 下 QSS 圆角不生效）
        self._border_radius: int = theme.get("window", {}).get("border_radius", 12)

        # 热键 ID（由 app.py 在 HotkeyManager 创建后设置）
        self._hotkey_id: int | None = None

        # 编辑模式三击退出追踪
        self._edit_dblclick_count: int = 0
        self._edit_dblclick_timer = QTimer(self)
        self._edit_dblclick_timer.setSingleShot(True)
        self._edit_dblclick_timer.timeout.connect(self._reset_edit_dblclick_count)

        # 标签 → 文件路径映射（QTabWidget 在 PySide6 中不暴露 tabData）
        self._fp_by_widget: dict[int, str] = {}  # id(QStackedWidget) → filepath

        # 窗口属性
        self.setWindowTitle("Wallpaper_Notes")
        self.setObjectName("WallpaperWindow")
        self.setMinimumSize(260, 200)

        # 去掉系统标题栏（最小化/最大化/关闭按钮）
        self.setWindowFlags(self.windowFlags() | Qt.FramelessWindowHint)
        # 透明底板：让 QSS 中 background_color 的 alpha 真正透出桌面壁纸
        self.setAttribute(Qt.WA_TranslucentBackground)

        # 应用窗口 QSS
        self.setStyleSheet(build_global_qss(theme))

        # 构建 UI
        self._build_ui()

        # 恢复窗口位置
        x, y, w, h = config.get_window_geometry()
        self.resize(w, h)
        if x is not None and y is not None:
            self.move(x, y)
        else:
            self._center_on_screen()

        # 初始为桌面层
        self._set_desktop_layer()
        # 圆角遮罩（QSS border-radius 在 WA_TranslucentBackground 下不生效）
        self._update_mask()

    # ── 公开接口 ────────────────────────────────────────────────

    def add_tab(self, filepath: str) -> None:
        """新增标签页。"""
        fp = str(filepath)
        # 重复检查（通过 widget 映射查找）
        existing = self._find_tab_index(fp)
        if existing >= 0:
            self._tabs.setCurrentIndex(existing)
            return

        content = self._read_file(fp)
        stack = self._create_tab_content(fp, content)
        stem = Path(fp).stem
        self._tabs.addTab(stack, stem)
        self._fp_by_widget[id(stack)] = fp
        self._tabs.setCurrentIndex(self._tabs.count() - 1)

    def remove_tab(self, filepath: str) -> None:
        """移除标签页。"""
        fp = str(filepath)
        for i in range(self._tabs.count()):
            stack = self._tabs.widget(i)
            if stack and self._fp_by_widget.get(id(stack)) == fp:
                del self._fp_by_widget[id(stack)]
                self._tabs.removeTab(i)
                return

    def refresh_current_tab(self, filepath: str) -> None:
        """文件内容被外部修改时刷新显示。

        仅在当前处于显示模式时才刷新——编辑模式下不覆盖用户正在编辑的内容。
        """
        fp = str(filepath)
        idx = self._find_tab_index(fp)
        if idx < 0:
            return
        stack: QStackedWidget = self._tabs.widget(idx)
        viewer: QTextBrowser = stack.widget(0)
        content = self._read_file(fp)
        viewer.setHtml(render(content, self._theme))

    def bring_to_front_and_edit(self) -> None:
        """快捷键唤出：恢复窗口 → 置前聚焦 → 编辑模式。

        多层防御策略：
        1. 检查/修复 WS_EX_NOACTIVATE（Qt FramelessWindow 可能设置了此标志）
        2. ShowWindow + HWND_TOPMOST（保障窗口可见和视觉层级）
        3. SwitchToThisWindow —— 模拟 Alt+Tab，绕过前台锁（核心方案）
        4. keybd_event Alt 键注入 —— 获得前台权限后 SetForegroundWindow
        5. AttachThreadInput —— 经典后备方案
        6. 500ms 后取消 TOPMOST
        """
        hwnd = int(self.winId())
        user32 = ctypes.windll.user32
        kernel32 = ctypes.windll.kernel32

        SWP_NOMOVE = 0x0002
        SWP_NOSIZE = 0x0001
        SWP_SHOWWINDOW = 0x0040
        HWND_TOPMOST = -1
        GWL_EXSTYLE = -20
        WS_EX_NOACTIVATE = 0x08000000

        # ── 1. 恢复窗口状态 ──
        if user32.IsIconic(hwnd):
            user32.ShowWindow(hwnd, 9)  # SW_RESTORE
        else:
            user32.ShowWindow(hwnd, 5)  # SW_SHOW
        self.show()  # 同步 Qt 内部状态

        # ── 2. 检查并移除 WS_EX_NOACTIVATE ──
        # Qt FramelessWindowHint 在某些 Windows 版本/主题下
        # 可能设置了此标志，阻止 SetForegroundWindow 激活窗口。
        ex_style = user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
        if ex_style & WS_EX_NOACTIVATE:
            user32.SetWindowLongW(hwnd, GWL_EXSTYLE, ex_style & ~WS_EX_NOACTIVATE)

        # ── 3. HWND_TOPMOST（临时置顶，不设 SWP_NOACTIVATE，让 Windows 一并处理激活） ──
        user32.SetWindowPos(
            hwnd, HWND_TOPMOST, 0, 0, 0, 0,
            SWP_NOMOVE | SWP_NOSIZE | SWP_SHOWWINDOW,
        )

        # ── 4. SwitchToThisWindow（核心方案） ──
        # Windows 用户32 API，模拟 Alt+Tab 切换行为。
        # deprecated 但 Win10/11 仍然有效，不依赖 SetForegroundWindow 的前台权限。
        user32.SwitchToThisWindow(hwnd, True)

        # ── 5. SetForegroundWindow ──
        # WM_HOTKEY 处理期间持有前台权限，通常能成功。
        if not user32.SetForegroundWindow(hwnd):
            # 5b. keybd_event：模拟 Alt 键按下/抬起
            #     注入键盘输入后调用线程获得系统前台权限。
            VK_MENU = 0x12
            KEYEVENTF_KEYUP = 0x0002
            user32.keybd_event(VK_MENU, 0, 0, 0)
            user32.keybd_event(VK_MENU, 0, KEYEVENTF_KEYUP, 0)
            user32.SetForegroundWindow(hwnd)

            # 5c. AttachThreadInput：关联前台线程输入状态
            if user32.GetForegroundWindow() != hwnd:
                fg_hwnd = user32.GetForegroundWindow()
                if fg_hwnd:
                    tid = kernel32.GetCurrentThreadId()
                    fg_tid = user32.GetWindowThreadProcessId(fg_hwnd, None)
                    if fg_tid and fg_tid != tid:
                        user32.AttachThreadInput(fg_tid, tid, True)
                        user32.SetForegroundWindow(hwnd)
                        user32.AttachThreadInput(fg_tid, tid, False)

        # ── 6. BringWindowToTop 兜底 ──
        user32.BringWindowToTop(hwnd)

        # ── 7. Qt 状态同步 ──
        self.activateWindow()
        self.raise_()

        # ── 8. 进入编辑模式 ──
        self._switch_edit()

        # ── 9. 延迟取消置顶 ──
        QTimer.singleShot(500, self._restore_zorder)

    def _restore_zorder(self) -> None:
        """取消置顶，回到正常 Z 序。"""
        hwnd = int(self.winId())
        ctypes.windll.user32.SetWindowPos(
            hwnd, _HWND_NOTOPMOST, 0, 0, 0, 0, _SWP_NOMOVE | _SWP_NOSIZE
        )

    def return_to_desktop_layer(self) -> None:
        """保存内容 → 返回显示模式 → 回到桌面层。"""
        self._save_and_switch_view()
        self._set_desktop_layer()

    def apply_theme(self, theme: dict[str, Any]) -> None:
        """应用新主题样式到窗口和所有标签页。

        由 app.py 在设置对话框确认后调用。
        """
        self._theme = theme
        # 重新生成全局 QSS
        self.setStyleSheet(build_global_qss(theme))
        # 更新圆角遮罩
        self._border_radius = theme.get("window", {}).get("border_radius", 12)
        self._update_mask()
        # 刷新所有标签页的内容区边框 + viewer + editor 样式
        ct = theme.get("content", {})
        bw = ct.get("pane_border_width", 1)
        bc = ct.get("pane_border_color", "rgba(200,200,200,0.5)")
        br = ct.get("pane_border_radius", 4)
        # 当边框宽度为 0 时取消圆角，防止玻璃渐变背景被 border-radius 裁切出视觉断层
        final_br = br if bw > 0 else 0
        win_r = theme.get("window", {}).get("border_radius", 12)
        self._main_layout.setContentsMargins(0, 0, 0, win_r)
        glass_qss = _build_glass_qss(
            ct.get("glass_opacity", 0.10)
            if ct.get("enable_glass", False) else 0.0
        )
        border_qss = (
            "QStackedWidget {"
            f"{glass_qss}"
            f"  border: {bw}px solid {bc};"
            f"  border-radius: 0px 0px {final_br}px {final_br}px;"
            "}"
        )
        enable_glow = ct.get("enable_glow", False)
        for i in range(self._tabs.count()):
            stack: QStackedWidget = self._tabs.widget(i)
            # 更新内容区边框 + 玻璃
            stack.setStyleSheet(border_qss)
            viewer: QTextBrowser = stack.widget(0)
            editor: QPlainTextEdit = stack.widget(1)
            viewer.viewport().setAutoFillBackground(False)
            editor.viewport().setAutoFillBackground(False)
            viewer.setStyleSheet(generate_content_qss(theme, final_br))
            editor.setStyleSheet(generate_editor_qss(theme, final_br))
            # 文字辉光（先移除旧的以免叠加）
            viewer.viewport().setGraphicsEffect(None)
            editor.viewport().setGraphicsEffect(None)
            if enable_glow:
                viewer.viewport().setGraphicsEffect(TextGlowEffect(blur_radius=3))
                editor.viewport().setGraphicsEffect(TextGlowEffect(blur_radius=3))
            # 重新渲染 Markdown（文字 CSS 可能变了）
            filepath = self._fp_by_widget.get(id(stack), "")
            if filepath:
                content = self._read_file(filepath)
                viewer.setHtml(render(content, theme))
            # 磨砂质感覆盖层（独立更新）
            noise_id = id(stack)
            enable_frost = ct.get("enable_frost", False)
            frost_intensity = ct.get("frost_intensity", 0.20) if enable_frost else 0.0
            grain_level = ct.get("frost_grain", 1)
            if frost_intensity > 0:
                if noise_id in self._noise_widgets:
                    self._noise_widgets[noise_id].set_params(
                        frost_intensity, _frost_grain_pixels(grain_level)
                    )
                else:
                    noise = FrostedNoiseWidget(
                        stack, intensity=frost_intensity,
                        grain=_frost_grain_pixels(grain_level)
                    )
                    noise.raise_()
                    noise.resize(stack.size())
                    self._noise_widgets[noise_id] = noise
                    stack.installEventFilter(self)
            else:
                if noise_id in self._noise_widgets:
                    self._noise_widgets[noise_id].deleteLater()
                    del self._noise_widgets[noise_id]

    # ── UI 构建 ─────────────────────────────────────────────────

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, self._border_radius)
        layout.setSpacing(0)
        self._main_layout = layout

        # ── 标签栏 ──
        self._tabs = QTabWidget()
        self._tabs.setDocumentMode(True)
        self._tabs.setMovable(False)
        self._tabs.setTabsClosable(False)

        # ⊕ 按钮
        btn_add = QPushButton("+")
        btn_add.setFixedSize(22, 22)
        btn_add.setFlat(True)
        btn_add.setToolTip("新建便签")
        btn_add.clicked.connect(self._on_add_clicked)
        self._btn_add = btn_add

        # 把 ⊕ 直接放进 QTabBar（同一容器），用 move() 定位到最右侧
        btn_add.setParent(self._tabs.tabBar())
        self._tabs.tabBar().setStyleSheet("QTabBar { padding-right: 30px; }")
        QTimer.singleShot(0, self._reposition_plus_button)

        # 右键菜单
        tab_bar = self._tabs.tabBar()
        tab_bar.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        tab_bar.customContextMenuRequested.connect(self._on_tab_context_menu)

        # 标签切换 → 回到显示模式
        self._tabs.currentChanged.connect(self._on_tab_changed)

        # 标签栏 + ⊕ 按钮支持拖拽窗口
        self._tabs.tabBar().installEventFilter(self)
        self._btn_add.installEventFilter(self)

        layout.addWidget(self._tabs)

        self.installEventFilter(self)

    def _reposition_plus_button(self) -> None:
        """将 ⊕ 按钮对齐到 QTabBar 最右侧（与标签文字同容器）。"""
        tb = self._tabs.tabBar()
        btn = self._btn_add
        if tb is None or btn is None:
            return
        x = tb.width() - btn.width() - 4  # 距右边缘 4px
        y = (tb.height() - btn.height()) // 2  # 垂直居中
        btn.move(max(0, x), max(0, y))
        btn.raise_()

    def _create_tab_content(self, filepath: str, content: str) -> QStackedWidget:
        """为指定文件构造 QStackedWidget（viewer + editor）。

        返回的 QStackedWidget：
          - index 0: QTextBrowser（显示模式，已加载 Markdown）
          - index 1: QPlainTextEdit（编辑模式，已加载原始文本）
        """
        stack = QStackedWidget()
        # 内容区边框 + 假毛玻璃背景
        # 上圆角：0（顶边贴标签栏）；下圆角：独立配置
        ct = self._theme.get("content", {})
        bw = ct.get("pane_border_width", 1)
        bc = ct.get("pane_border_color", "rgba(200,200,200,0.5)")
        br = ct.get("pane_border_radius", 4)
        # 当边框宽度为 0 时取消圆角，防止玻璃渐变背景被 border-radius 裁切出视觉断层
        final_br = br if bw > 0 else 0
        glass_qss = _build_glass_qss(
            ct.get("glass_opacity", 0.10)
            if ct.get("enable_glass", False) else 0.0
        )
        stack.setStyleSheet(
            "QStackedWidget {"
            f"{glass_qss}"
            f"  border: {bw}px solid {bc};"
            f"  border-radius: 0px 0px {final_br}px {final_br}px;"
            "}"
        )

        # ── 显示层 ──
        viewer = QTextBrowser()
        viewer.setOpenExternalLinks(False)
        viewer.setReadOnly(True)
        viewer.setFrameShape(QTextBrowser.Shape.NoFrame)
        viewer.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        viewer.setStyleSheet(generate_content_qss(self._theme, final_br))
        viewer.setHtml(render(content, self._theme))
        viewer.viewport().setAutoFillBackground(False)  # 透出 QStackedWidget 边框
        # 文字辉光（基于文字自身颜色，按距离变暗）
        if ct.get("enable_glow", False):
            viewer.viewport().setGraphicsEffect(TextGlowEffect(blur_radius=3))
        viewer.viewport().installEventFilter(self)  # 双击检测
        stack.addWidget(viewer)  # index 0

        # ── 编辑层 ──
        editor = QPlainTextEdit()
        editor.viewport().setAutoFillBackground(False)  # 透出 QStackedWidget 边框
        # 编辑层文字辉光
        if ct.get("enable_glow", False):
            editor.viewport().setGraphicsEffect(TextGlowEffect(blur_radius=3))
        editor.setFrameShape(QPlainTextEdit.Shape.NoFrame)
        editor.setTabStopDistance(32)
        editor.setPlainText(content)
        editor.setStyleSheet(generate_editor_qss(self._theme, final_br))
        editor.viewport().installEventFilter(self)  # Esc 检测
        stack.addWidget(editor)  # index 1

        # 初始：显示模式
        stack.setCurrentIndex(0)

        # 磨砂质感覆盖层（独立噪点纹理效果）
        if ct.get("enable_frost", False):
            intensity = ct.get("frost_intensity", 0.20)
            grain_level = ct.get("frost_grain", 1)
            if intensity > 0:
                noise = FrostedNoiseWidget(
                    stack, intensity=intensity, grain=_frost_grain_pixels(grain_level)
                )
                noise.raise_()
                self._noise_widgets[id(stack)] = noise
                stack.installEventFilter(self)

        return stack

    # ── 窗口层级与遮罩 ──────────────────────────────────────────

    def _update_mask(self) -> None:
        """用 setMask 实现窗口圆角（QSS border-radius 在 WA_TranslucentBackground
        下的 Windows 分层窗口中不生效）。"""
        r = self._border_radius
        if r > 0 and not self.isMinimized():
            path = QPainterPath()
            path.addRoundedRect(0, 0, self.width(), self.height(), r, r)
            self.setMask(QRegion(path.toFillPolygon().toPolygon()))
        else:
            self.clearMask()

    def resizeEvent(self, event) -> None:
        """窗口大小改变时更新圆角遮罩。"""
        super().resizeEvent(event)
        self._update_mask()

    def _set_desktop_layer(self) -> None:
        """窗口回到桌面层（压到所有普通窗口下面）。"""
        hwnd = int(self.winId())
        ctypes.windll.user32.SetWindowPos(
            hwnd, _HWND_BOTTOM, 0, 0, 0, 0, _SWP_NOMOVE | _SWP_NOSIZE
        )

    # ── Windows 原生消息 ────────────────────────────────────────

    def set_hotkey_id(self, hotkey_id: int) -> None:
        """由 app.py 在创建 HotkeyManager 后调用，记录热键 ID。

        nativeEvent 需要此 ID 来识别 WM_HOTKEY。
        """
        self._hotkey_id = hotkey_id

    def nativeEvent(self, event_type: bytes, message) -> tuple[bool, int]:
        """拦截原生 Windows 消息：WM_HOTKEY + WM_NCHITTEST。

        WM_HOTKEY：
        RegisterHotKey 注册在窗口 HWND 上，WM_HOTKEY 经 Qt 内部
        窗口过程派发到此方法。此时线程拥有完整前台权限，
        SetForegroundWindow 理应成功。

        WM_NCHITTEST：
        WA_TranslucentBackground 创建了 WS_EX_LAYERED 窗口，
        Windows 默认会透过透明像素的点击。此方法强制返回 HTCLIENT，
        让所有鼠标事件都路由到 Qt，不依赖背景透明度。
        """
        msg = ctypes.wintypes.MSG.from_address(message.__int__())

        # WM_HOTKEY —— 窗口过程内处理，前台权限充足
        if msg.message == 0x0312 and self._hotkey_id is not None:
            if msg.wParam == self._hotkey_id:
                self.bring_to_front_and_edit()
                return (True, 0)

        # WM_NCHITTEST
        if msg.message == _WM_NCHITTEST:
            return (True, _HTCLIENT)

        return super().nativeEvent(event_type, message)

    # ── 模式切换 ────────────────────────────────────────────────

    def _switch_edit(self) -> None:
        """切换到编辑模式（当前标签页）。"""
        if not self._has_tabs():
            return
        self._ensure_viewer_latest()  # 编辑前刷新显示层
        stack = self._current_stack()
        if stack is not None:
            stack.setCurrentIndex(1)
            editor: QPlainTextEdit = stack.widget(1)
            # 从文件重读内容到编辑器，避免显示层更新后编辑器仍持有旧内容
            fp = self._current_filepath()
            if fp:
                editor.setPlainText(self._read_file(fp))
            editor.setFocus()

    def _save_and_switch_view(self) -> None:
        """保存并切换到显示模式。"""
        if not self._has_tabs():
            return
        stack = self._current_stack()
        if stack is None or stack.currentIndex() != 1:
            return

        editor: QPlainTextEdit = stack.widget(1)
        content = editor.toPlainText()
        fp = self._current_filepath()

        # 保存回调
        save_cb = self._callbacks.get("on_save_content")
        if save_cb:
            save_cb(fp, content)

        # 刷新显示
        viewer: QTextBrowser = stack.widget(0)
        viewer.setHtml(render(content, self._theme))
        stack.setCurrentIndex(0)

    def _ensure_viewer_latest(self) -> None:
        """确保当前标签页的显示层是最新内容。"""
        if not self._has_tabs():
            return
        stack = self._current_stack()
        if stack is None:
            return
        fp = self._current_filepath()
        if fp:
            content = self._read_file(fp)
            viewer: QTextBrowser = stack.widget(0)
            viewer.setHtml(render(content, self._theme))

    # ── 便签操作（回调）─────────────────────────────────────────

    def _reset_edit_dblclick_count(self) -> None:
        """编辑模式下双击计数超时重置（三击退出的防误触）。"""
        self._edit_dblclick_count = 0

    def _on_add_clicked(self) -> None:
        cb = self._callbacks.get("on_create_note")
        if cb:
            cb()

    def _on_tab_context_menu(self, pos: QPoint) -> None:
        tab_bar = self._tabs.tabBar()
        idx = tab_bar.tabAt(pos)
        if idx < 0:
            return
        stack = self._tabs.widget(idx)
        filepath = self._fp_by_widget.get(id(stack), "") if stack else ""

        menu = QMenu(self)
        action_rename = QAction("重命名", menu)
        action_delete = QAction("删除", menu)
        menu.addAction(action_rename)
        menu.addAction(action_delete)

        action = menu.exec(tab_bar.mapToGlobal(pos))
        if action is action_rename:
            old_stem = Path(filepath).stem
            new_name, ok = QInputDialog.getText(
                self, "重命名便签", "新名称：", text=old_stem,
            )
            if ok and new_name.strip() and new_name.strip() != old_stem:
                cb = self._callbacks.get("on_rename_note")
                if cb:
                    cb(filepath, new_name.strip())
        elif action is action_delete:
            reply = QMessageBox.question(
                self,
                "删除便签",
                f"确定删除「{Path(filepath).stem}」？\n此操作不可恢复。",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if reply == QMessageBox.StandardButton.Yes:
                cb = self._callbacks.get("on_delete_note")
                if cb:
                    cb(filepath)

    def _on_tab_changed(self, index: int) -> None:
        """标签切换：保存上一个标签的编辑内容，刷新新标签。"""
        # 保存上一个标签的编辑内容
        prev = getattr(self, '_prev_tab_index', -1)
        if prev >= 0 and prev < self._tabs.count():
            prev_stack: QStackedWidget = self._tabs.widget(prev)
            if prev_stack is not None and prev_stack.currentIndex() == 1:
                editor: QPlainTextEdit = prev_stack.widget(1)
                content = editor.toPlainText()
                prev_stack_w = self._tabs.widget(prev)
                prev_fp = self._fp_by_widget.get(id(prev_stack_w), "") if prev_stack_w else ""
                save_cb = self._callbacks.get("on_save_content")
                if save_cb and prev_fp:
                    save_cb(prev_fp, content)
                # 切回显示模式
                viewer: QTextBrowser = prev_stack.widget(0)
                viewer.setHtml(render(content, self._theme))
                prev_stack.setCurrentIndex(0)

        self._prev_tab_index = index

        # 刷新新标签的显示内容
        if index >= 0:
            fp_w = self._tabs.widget(index)
            fp = self._fp_by_widget.get(id(fp_w), "") if fp_w else ""
            if fp:
                self.refresh_current_tab(fp)

        # 确保磨砂噪点层在最上层（QStackedWidget 切换会覆盖子widget）
        self._ensure_noise_ontop()

    # ── 磨砂噪点层保顶 ────────────────────────────────────────────

    def _ensure_noise_ontop(self) -> None:
        """确保所有磨砂噪点覆盖层在 QStackedWidget 最上层。"""
        for noise in self._noise_widgets.values():
            noise.raise_()
            noise.update()

    # ── 事件过滤器 ──────────────────────────────────────────────

    def eventFilter(self, obj: QObject, event: QEvent) -> bool:
        etype = event.type()

        # QStackedWidget 尺寸变化 → 同步磨砂噪点覆盖层
        if etype == QEvent.Type.Resize:
            noise_id = id(obj)
            if noise_id in self._noise_widgets:
                self._noise_widgets[noise_id].resize(event.size())
                # 不 return True——让 QStackedWidget 继续正常处理
        if obj is self._tabs.tabBar() and etype == QEvent.Type.Resize:
            self._reposition_plus_button()
            return False  # 不拦截，让 QTabBar 继续处理

        # ── 标签栏 / ⊕ 按钮拖拽窗口 ──
        # 点击标签是点击，长按拖拽是拖拽，通过距离阈值区分
        if obj in (self._tabs.tabBar(), self._btn_add):
            if etype == QEvent.Type.MouseButtonPress and event.button() == Qt.MouseButton.LeftButton:
                self._tab_drag_press_pos = event.globalPosition().toPoint()
                self._tab_drag_engaged = False
                return False  # 不拦截——让标签栏/按钮正常处理点击

            if etype == QEvent.Type.MouseMove and event.buttons() & Qt.MouseButton.LeftButton:
                if self._tab_drag_press_pos is not None:
                    gp = event.globalPosition().toPoint()
                    if not self._tab_drag_engaged:
                        dist = (gp - self._tab_drag_press_pos).manhattanLength()
                        if dist >= QApplication.startDragDistance():
                            self._tab_drag_engaged = True
                            self._drag_offset = self._tab_drag_press_pos - self.frameGeometry().topLeft()
                    if self._tab_drag_engaged:
                        self.move(gp - self._drag_offset)
                        self.setCursor(Qt.CursorShape.ClosedHandCursor)
                        return True  # 拦截——防止标签切换
                return False

            if etype == QEvent.Type.MouseButtonRelease:
                engaged = self._tab_drag_engaged
                self._tab_drag_press_pos = None
                self._tab_drag_engaged = False
                if engaged:
                    self.setCursor(Qt.CursorShape.ArrowCursor)
                    # 保存窗口位置
                    g = self.geometry()
                    self._config.set_window_geometry(g.x(), g.y(), g.width(), g.height())
                    return True  # 拦截释放事件，防止触发标签/按钮点击
                return False

            return super().eventFilter(obj, event)

        editing = self._is_editing()

        # 双击 → 进入编辑模式（仅在预览模式下触发）
        if etype == QEvent.Type.MouseButtonDblClick and not editing:
            self._switch_edit()
            return True

        # 编辑模式双击累计 → 三击退出预览模式
        if etype == QEvent.Type.MouseButtonDblClick and editing:
            self._edit_dblclick_count += 1
            self._edit_dblclick_timer.start(QApplication.doubleClickInterval())
            if self._edit_dblclick_count >= 1:  # 1 次 DblClick + 1 次物理点击 = 3 次实际点击
                self._edit_dblclick_count = 0
                self._edit_dblclick_timer.stop()
                self._save_and_switch_view()
            return True  # 阻止 text selection

        # Enter 键 → 编辑模式（仅在显示模式下触发，排除 Ctrl+Enter）
        if (
            etype == QEvent.Type.KeyPress
            and event.key() == Qt.Key.Key_Return
            and not editing
            and not (event.modifiers() & Qt.KeyboardModifier.ControlModifier)
        ):
            self._switch_edit()
            return True

        # Esc → 保存 + 回到显示模式（仅在编辑模式下触发）
        if (
            etype == QEvent.Type.KeyPress
            and event.key() == Qt.Key.Key_Escape
            and editing
        ):
            self._save_and_switch_view()
            self._set_desktop_layer()
            return True

        return super().eventFilter(obj, event)

    # ── 拖拽移动 / 边缘缩放 ─────────────────────────────────────

    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            pos = event.position().toPoint()
            self._resize_edges = self._detect_edges(pos)
            if self._resize_edges:
                self._drag_offset = event.globalPosition().toPoint()
            # 普通区域不再启动拖拽——拖拽仅在标签栏/⊕按钮区域生效
            # 普通区域的鼠标事件留给文本选择、双击编辑等交互
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event) -> None:
        pos = event.position().toPoint()
        edges = self._detect_edges(pos)
        self._update_cursor(edges)

        if event.buttons() & Qt.MouseButton.LeftButton and self._drag_offset is not None:
            gp = event.globalPosition().toPoint()
            if self._resize_edges:
                self._do_resize(gp)
            else:
                self.move(gp - self._drag_offset)

        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self._drag_offset = None
            self._resize_edges = set()
            # 保存窗口几何
            g = self.geometry()
            self._config.set_window_geometry(g.x(), g.y(), g.width(), g.height())
        super().mouseReleaseEvent(event)

    def wheelEvent(self, event) -> None:
        """将空白区域的滚轮事件转发到当前便签的 viewer/editor。

        由于 WA_TranslucentBackground 创建了 WS_EX_LAYERED 窗口，
        Windows 可能在透明像素区域拦截滚轮事件。此方法确保空白区域
        的滚动操作能作用到当前便签内容上。
        """
        stack = self._current_stack()
        if stack:
            active = stack.currentWidget()
            if active:
                target = (
                    active.viewport()
                    if hasattr(active, "viewport")
                    else active
                )
                QApplication.sendEvent(target, event)
                if event.isAccepted():
                    return
        super().wheelEvent(event)

    # ── 边缘检测 / 缩放 / 光标 ──────────────────────────────────

    def _detect_edges(self, pos: QPoint) -> set[str]:
        edges: set[str] = set()
        r = self.rect()
        if pos.x() <= _RESIZE_MARGIN:
            edges.add("left")
        elif pos.x() >= r.width() - _RESIZE_MARGIN:
            edges.add("right")
        if pos.y() <= _RESIZE_MARGIN:
            edges.add("top")
        elif pos.y() >= r.height() - _RESIZE_MARGIN:
            edges.add("bottom")
        return edges

    def _update_cursor(self, edges: set[str]) -> None:
        if not edges:
            self.setCursor(Qt.CursorShape.ArrowCursor)
        elif edges == {"left"} or edges == {"right"}:
            self.setCursor(Qt.CursorShape.SizeHorCursor)
        elif edges == {"top"} or edges == {"bottom"}:
            self.setCursor(Qt.CursorShape.SizeVerCursor)
        elif edges == {"top", "left"} or edges == {"bottom", "right"}:
            self.setCursor(Qt.CursorShape.SizeFDiagCursor)
        else:
            self.setCursor(Qt.CursorShape.SizeBDiagCursor)

    def _do_resize(self, global_pos: QPoint) -> None:
        """根据拖拽方向和全局坐标调整窗口大小。"""
        diff = global_pos - self._drag_offset
        geo = self.frameGeometry()
        edges = self._resize_edges

        if "left" in edges:
            geo.setLeft(geo.left() + diff.x())
        elif "right" in edges:
            geo.setRight(geo.right() + diff.x())

        if "top" in edges:
            geo.setTop(geo.top() + diff.y())
        elif "bottom" in edges:
            geo.setBottom(geo.bottom() + diff.y())

        min_w, min_h = self.minimumWidth(), self.minimumHeight()
        if geo.width() >= min_w and geo.height() >= min_h:
            self.setGeometry(geo)

        self._drag_offset = global_pos

    # ── 辅助 ────────────────────────────────────────────────────

    def _center_on_screen(self) -> None:
        scr = QApplication.primaryScreen()
        if scr is None:
            return
        center = scr.availableGeometry().center()
        fg = self.frameGeometry()
        fg.moveCenter(center)
        self.move(fg.topLeft())

    def _current_stack(self) -> QStackedWidget | None:
        return self._tabs.currentWidget()

    def _current_filepath(self) -> str:
        stack = self._current_stack()
        return self._fp_by_widget.get(id(stack), "") if stack else ""

    def _has_tabs(self) -> bool:
        return self._tabs.count() > 0

    def _is_editing(self) -> bool:
        """当前标签是否处于编辑模式。"""
        if not self._has_tabs():
            return False
        stack = self._current_stack()
        return stack is not None and stack.currentIndex() == 1

    def _find_tab_index(self, filepath: str) -> int:
        """按文件路径查找标签索引，未找到返回 -1。"""
        for i in range(self._tabs.count()):
            stack = self._tabs.widget(i)
            if stack and self._fp_by_widget.get(id(stack)) == filepath:
                return i
        return -1

    @staticmethod
    def _read_file(filepath: str) -> str:
        try:
            return Path(filepath).read_text(encoding="utf-8")
        except FileNotFoundError:
            return ""
