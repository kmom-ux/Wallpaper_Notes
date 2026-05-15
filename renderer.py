"""Markdown 渲染管道。

职责：
1. 接收 Markdown 文本，返回 HTML 供 QTextBrowser 显示
2. 结合 theme.json 生成 CSS 样式表
3. 作为渲染管道的隔离层——未来替换渲染库只需改此文件

策略：
  自行将 Markdown 转为 HTML（处理标题、段落、列表等基本结构），
  并把 CSS 内联到 HTML 中，避免依赖 QTextBrowser 内置 Markdown 解析器
  在空行处理上的不一致。

边界：
- 不做文件 IO
- 不感知 UI 层
"""

from __future__ import annotations

import html as _html
import re
from typing import Any

# ── CSS 模板 ──────────────────────────────────────────────────

# QTextBrowser 支持的 CSS 子集（Qt Rich Text）
_CONTENT_CSS_TEMPLATE = """
body {{
    font-family: "{font_family}", "Segoe UI Emoji", sans-serif;
    font-size: {font_size}px;
    color: {text_color};
    line-height: {line_height};
    padding: {padding_v}px {padding_h}px;
    margin: 0;
}}
h1 {{ font-family: "{heading_font_family}", "Segoe UI Emoji", sans-serif; font-size: {h1_size}px; color: {heading_color}; margin: 8px 0 4px 0; }}
h2 {{ font-family: "{heading_font_family}", "Segoe UI Emoji", sans-serif; font-size: {h2_size}px; color: {heading_color}; margin: 6px 0 3px 0; }}
h3 {{ font-family: "{heading_font_family}", "Segoe UI Emoji", sans-serif; font-size: {h3_size}px; color: {heading_color}; margin: 4px 0 2px 0; }}
p  {{ margin: 4px 0; }}
ul, ol {{ margin: 4px 0; padding-left: 20px; }}
blockquote {{
    border-left: 3px solid {quote_border};
    padding: 4px 12px;
    margin: 6px 0;
    color: {quote_color};
}}
code {{
    font-family: "{mono_family}";
    background: {code_bg};
    padding: 1px 4px;
    border-radius: 3px;
}}
pre {{
    font-family: "{mono_family}";
    background: {code_bg};
    padding: 8px 12px;
    border-radius: 4px;
    margin: 6px 0;
}}
"""


# ── 公开接口 ──────────────────────────────────────────────────

def render(md_text: str, theme: dict[str, Any]) -> str:
    """将 Markdown 文本转换为完整 HTML（含内联 CSS），供 QTextBrowser 显示。

    解析标题、段落、列表、引用等基本 Markdown 结构，生成带样式的 HTML。
    不再依赖 QTextBrowser.setMarkdown() 的内置解析器。
    """
    css = build_css(theme)
    body = _md_to_html(md_text)
    return f"""<html><head><meta charset="utf-8"><style>{css}</style></head><body>{body}</body></html>"""


def build_css(theme: dict[str, Any]) -> str:
    """根据 theme 生成 QTextBrowser 用的 CSS 样式表。

    读取路径：
      theme.content → 正文样式
      theme.editor  → monospace 字体系列（代码块用）
    """
    content = theme.get("content", {})
    editor = theme.get("editor", {})

    font_family = str(content.get("font_family", "Microsoft YaHei"))
    font_size = int(content.get("font_size", 16))
    text_color = str(content.get("text_color", "#333333"))
    heading_color = str(content.get("heading_color", "#aaddff"))
    heading_font_family = str(content.get("heading_font_family", font_family))
    line_height = float(content.get("line_height", 1.6))
    padding_h = int(content.get("padding_h", 16))
    padding_v = int(content.get("padding_v", 16))

    mono_family = str(editor.get("font_family", "Cascadia Code, Consolas, monospace"))

    bg_raw = str(content.get("background_color", "transparent"))
    # 从 "transparent" 或 "rgba(...)" 中提取 alpha 用于引文
    quote_color = text_color
    quote_border = _derive_color(text_color, 0.2)

    css = _CONTENT_CSS_TEMPLATE.format(
        font_family=font_family,
        font_size=font_size,
        text_color=text_color,
        heading_color=heading_color,
        heading_font_family=heading_font_family,
        line_height=line_height,
        padding_h=padding_h,
        padding_v=padding_v,
        h1_size=int(font_size * 1.5),
        h2_size=int(font_size * 1.25),
        h3_size=int(font_size * 1.1),
        mono_family=mono_family,
        quote_color=quote_color,
        quote_border=quote_border,
        code_bg="rgba(0,0,0,0.05)",
    )
    return css


# ── Markdown → HTML 转换 ──────────────────────────────────────


def _md_to_html(text: str) -> str:
    """极简 Markdown → HTML 转换器。

    支持：标题（# ~ ######）、段落（空行分隔）、无序列表（-）、
    引用（>）、行内粗体/斜体/代码。
    """
    text = text.replace("\r\n", "\n")

    # 用空行分割为块
    blocks = re.split(r"\n\n+", text)
    blocks = [b.strip() for b in blocks if b.strip()]

    html_parts: list[str] = []
    for block in blocks:
        lines = block.split("\n")

        # ── 标题 ──
        if re.match(r"^#{1,6}\s", block):
            m = re.match(r"^(#{1,6})\s+(.+)$", block)
            if m:
                level = len(m.group(1))
                content = _inline_to_html(m.group(2).strip())
                html_parts.append(f"<h{level}>{content}</h{level}>")
                continue

        # ── 无序列表（所有非空行以 - 或 * 开头）──
        if all(
            re.match(r"^\s*[-*]\s", line) for line in lines if line.strip()
        ):
            items = []
            for line in lines:
                line = line.strip()
                if not line:
                    continue
                text_content = re.sub(r"^[-*]\s+", "", line, count=1)
                items.append(f"<li>{_inline_to_html(text_content)}</li>")
            html_parts.append("<ul>" + "".join(items) + "</ul>")
            continue

        # ── 引用 ──
        if all(line.strip().startswith(">") for line in lines if line.strip()):
            quote_lines = []
            for line in lines:
                stripped = re.sub(r"^>\s?", "", line)
                quote_lines.append(stripped)
            html_parts.append(
                f"<blockquote>{_inline_to_html(' '.join(quote_lines))}</blockquote>"
            )
            continue

        # ── 段落（含换行）──
        para_lines = []
        for line in lines:
            stripped = line.strip()
            if stripped:
                para_lines.append(_inline_to_html(stripped))
        html_parts.append("<p>" + "<br>".join(para_lines) + "</p>")

    return "\n".join(html_parts)


def _inline_to_html(text: str) -> str:
    """处理行内 Markdown：**粗体**、*斜体*、`代码`。"""
    text = _html.escape(text)
    text = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", text)
    text = re.sub(r"\*(.+?)\*", r"<i>\1</i>", text)
    text = re.sub(r"`(.+?)`", r"<code>\1</code>", text)
    return text


# ── 内部 ──────────────────────────────────────────────────────


def _derive_color(base: str, alpha: float) -> str:
    """从十六进制颜色值生成 rgba 变体。"""
    base = base.lstrip("#")
    if len(base) != 6:
        return f"rgba(0,0,0,{alpha})"
    try:
        r = int(base[0:2], 16)
        g = int(base[2:4], 16)
        b = int(base[4:6], 16)
        return f"rgba({r},{g},{b},{alpha})"
    except ValueError:
        return f"rgba(0,0,0,{alpha})"
