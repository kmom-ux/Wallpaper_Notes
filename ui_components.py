"""UI 组件工厂与主题引擎。

职责：
1. 根据 theme.json 生成 QSS 样式表字符串
2. 提供预配置的 UI 组件实例（已应用主题的 QTabWidget、QTextBrowser 等）
3. 作为「自定义组件的隔离层」——未来超出 QTabWidget 能力的改造点

MVP 行为：
  仅生成 QSS 字符串并应用到标准 QWidgets。
  未来标准控件不够用时，隔离层保障改造不影响 window.py 的布局逻辑。
"""

from __future__ import annotations

from typing import Any

from PySide6.QtWidgets import QTabWidget

# ── QSS 生成 ──────────────────────────────────────────────────

_TAB_QSS = """
QTabBar {{
    background-color: {bg};
    border-top-left-radius: {radius_top}px;
    border-top-right-radius: {radius_top}px;
}}
QTabWidget::pane {{
    background-color: transparent;
}}
QTabBar::tab {{
    background: transparent;
    color: {text_color};
    font-family: "{font_family}";
    font-size: {font_size}px;
    padding: {pv}px {ph}px;
    border: none;
    border-bottom: 2px solid transparent;
}}
QTabBar::tab:selected {{
    color: {active_color};
    border-bottom: 2px solid {indicator};
}}
QTabBar::tab:hover {{
    color: {active_color};
}}
QTabBar QPushButton {{
    background: transparent;
    border: none;
    color: {text_color};
}}
QTabBar QPushButton:hover {{
    color: {active_color};
}}
"""

_CONTENT_QSS = """
QTextBrowser {{
    background-color: {bg};
    color: {text_color};
    font-family: "{font_family}";
    font-size: {font_size}px;
    border: none;
    border-bottom-left-radius: {pane_br}px;
    border-bottom-right-radius: {pane_br}px;
    padding: {pv}px {ph}px;
}}
"""

_EDITOR_QSS = """
QPlainTextEdit {{
    background-color: {bg};
    color: {text_color};
    font-family: "{font_family}";
    font-size: {font_size}px;
    border: none;
    selection-background-color: {selection};  /* padding+border-radius 已移除——由容器 margin + radius 提供 */
}}
"""

_SCROLLBAR_QSS = """
QScrollBar:vertical {{
    background: {track};
    width: {width}px;
    margin: 0;
}}
QScrollBar::handle:vertical {{
    background: {handle};
    border-radius: {width}px;
    min-height: 20px;
}}
QScrollBar::handle:vertical:hover {{
    background: {hover};
}}
QScrollBar::add-line:vertical,
QScrollBar::sub-line:vertical {{
    height: 0;
}}
QScrollBar::add-page:vertical,
QScrollBar::sub-page:vertical {{
    background: none;
}}
QScrollBar:horizontal {{
    height: {width}px;
    background: {track};
}}
QScrollBar::handle:horizontal {{
    background: {handle};
    border-radius: {width}px;
    min-width: 20px;
}}
QScrollBar::handle:horizontal:hover {{
    background: {hover};
}}
QScrollBar::add-line:horizontal,
QScrollBar::sub-line:horizontal {{
    width: 0;
}}
"""


def generate_tab_bar_qss(theme: dict[str, Any]) -> str:
    """标签栏样式。"""
    tb = theme.get("tab_bar", {})
    return _TAB_QSS.format(
        bg=_s(tb, "background_color", "rgba(245,245,245,0.9)"),
        radius_top=_i(tb, "radius_top", 0),
        text_color=_s(tb, "text_color", "#666666"),
        font_family=_s(tb, "font_family", "Microsoft YaHei"),
        font_size=_i(tb, "font_size", 14),
        ph=_i(tb, "padding_h", 12),
        pv=_i(tb, "padding_v", 8),
        active_color=_s(tb, "active_text_color", "#333333"),
        indicator=_s(tb, "active_indicator_color", "#4A90D9"),
    )


def generate_content_qss(theme: dict[str, Any], pane_br: int | None = None) -> str:
    """内容区显示模式样式。"""
    c = theme.get("content", {})
    if pane_br is None:
        pane_br = c.get("pane_border_radius", 4)
    return _CONTENT_QSS.format(
        bg=_s(c, "background_color", "transparent"),
        text_color=_s(c, "text_color", "#333333"),
        font_family=_s(c, "font_family", "Microsoft YaHei"),
        font_size=_i(c, "font_size", 16),
        ph=_i(c, "padding_h", 16),
        pv=_i(c, "padding_v", 16),
        pane_br=pane_br,
    )


def generate_editor_qss(theme: dict[str, Any], pane_br: int | None = None) -> str:
    """编辑模式样式。"""
    e = theme.get("editor", {})
    if pane_br is None:
        pane_br = theme.get("content", {}).get("pane_border_radius", 4)
    sel = _s(e, "caret_color", "#4A90D9")
    return _EDITOR_QSS.format(
        bg=_s(e, "background_color", "#FAFAFA"),
        text_color=_s(e, "text_color", "#333333"),
        font_family=_s(e, "font_family", "Cascadia Code, Consolas, monospace"),
        font_size=_i(e, "font_size", 15),
        selection=sel,
    )  # ph/pv 不再出现在 QSS 模板——由容器布局 margins 提供


def generate_scrollbar_qss(theme: dict[str, Any]) -> str:
    """滚动条样式。"""
    sb = theme.get("scrollbar", {})
    return _SCROLLBAR_QSS.format(
        width=_i(sb, "width", 6),
        track=_s(sb, "track_color", "transparent"),
        handle=_s(sb, "handle_color", "rgba(0,0,0,0.2)"),
        hover=_s(sb, "handle_hover_color", "rgba(0,0,0,0.4)"),
    )


# ── 合并样式表 ────────────────────────────────────────────────

def build_global_qss(theme: dict[str, Any]) -> str:
    """一次性生成窗口级和容器级 QSS（子控件样式由局部覆盖）。"""
    return (
        generate_tab_bar_qss(theme)
        + generate_scrollbar_qss(theme)
    )


# ── 组件工厂 ──────────────────────────────────────────────────

def create_tab_bar(parent, theme: dict[str, Any]) -> QTabWidget:
    """创建已应用 theme 样式的 QTabWidget。

    - 标签可关闭（右键菜单由 window.py 处理）
    - 标签不可拖拽重排（MVP）
    - '+' 按钮由 window.py 在右侧追加
    """
    tab = QTabWidget(parent)
    tab.setDocumentMode(True)  # 更扁平的外观
    tab.setMovable(False)
    tab.setTabsClosable(False)  # 删除通过右键菜单，不用关闭按钮
    return tab


# ── 内部工具 ──────────────────────────────────────────────────

def _s(d: dict[str, Any], key: str, default: str) -> str:
    return str(d.get(key, default))


def _i(d: dict[str, Any], key: str, default: int) -> int:
    return int(d.get(key, default))
