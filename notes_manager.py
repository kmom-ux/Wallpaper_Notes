"""文件系统到便签列表的映射器。

职责：
1. 维护 notes/ 目录的实时镜像——list[NoteInfo] 与 .md 文件保持最终一致
2. 提供 CRUD 方法，直接操作文件系统
3. 启动 watchdog 监听目录变化，变化时发射信号
4. 对上游屏蔽文件路径、监听器等实现细节

边界：
- 不关心文件内容格式（仅读写，不做解析）
- 不关心上游如何使用 NoteInfo
- 只操作 notes/ 目录
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

from PySide6.QtCore import QObject, Signal, QTimer
from watchdog.events import (
    FileCreatedEvent,
    FileDeletedEvent,
    FileModifiedEvent,
    FileSystemEventHandler,
)
from watchdog.observers.polling import PollingObserver as Observer

from models import NoteInfo

# ── 常量 ──────────────────────────────────────────────────────

_DEBOUNCE_MS = 300  # 同一文件连续事件的时间间隔阈值


class _NotesEventHandler(FileSystemEventHandler):
    """watchdog 事件 → NotesManager 信号的中转器。

    在独立线程中执行，通过 Qt 信号与主线程通信。
    内置简单去重：同一文件 300ms 内的重复事件只处理一次。
    """

    def __init__(self, manager: NotesManager) -> None:
        super().__init__()
        self._manager = manager

    def on_modified(self, event: FileSystemEventHandler) -> None:
        if isinstance(event, FileModifiedEvent):
            self._manager._on_watchdog_event("modified", event.src_path)

    def on_created(self, event: FileSystemEventHandler) -> None:
        if isinstance(event, FileCreatedEvent):
            self._manager._on_watchdog_event("created", event.src_path)

    def on_deleted(self, event: FileSystemEventHandler) -> None:
        if isinstance(event, FileDeletedEvent):
            self._manager._on_watchdog_event("deleted", event.src_path)


# ═══════════════════════════════════════════════════════════════
# NotesManager
# ═══════════════════════════════════════════════════════════════

class NotesManager(QObject):
    """便签数据管理器，维护 notes/ 目录的实时映射。"""

    # ── 信号 ────────────────────────────────────────────────────

    note_changed = Signal(str)      # filepath — 文件内容被修改
    note_added = Signal(str)        # filepath — 新文件被创建
    note_deleted = Signal(str)      # filepath — 文件被删除

    # ── 生命周期 ────────────────────────────────────────────────

    def __init__(self, notes_dir: str | Path, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._notes_dir = Path(notes_dir).resolve()
        self._observer: Observer | None = None
        # 防抖：{(event_type, filepath): last_timestamp}
        self._debounce: dict[tuple[str, str], float] = {}
        # 确保目录存在
        self._notes_dir.mkdir(parents=True, exist_ok=True)

    # ── 全量扫描 ────────────────────────────────────────────────

    def scan_all(self) -> list[NoteInfo]:
        """扫描 notes/ 下所有 .md 文件，返回 NoteInfo 列表。

        结果按文件名排序。
        """
        md_files = sorted(self._notes_dir.glob("*.md"), key=lambda p: p.name.lower())
        return [self._build_note_info(p) for p in md_files]

    # ── CRUD ────────────────────────────────────────────────────

    def create(self, filename: str) -> NoteInfo:
        """创建新的 .md 文件（内容为空），返回对应的 NoteInfo。

        filename 不含路径，不含 .md 后缀，如 '新便签'。
        """
        name = filename if filename.endswith(".md") else f"{filename}.md"
        filepath = (self._notes_dir / name).resolve()
        filepath.write_text("", encoding="utf-8")
        info = self._build_note_info(filepath)
        # 由于是自己创建的文件，watchdog 会检测到。
        # 但我们用防抖来避免重复通知。
        self.note_added.emit(str(filepath))
        return info

    def delete(self, filepath: str | Path) -> None:
        """删除指定的 .md 文件。"""
        path = Path(filepath).resolve()
        # 仅允许删除 notes/ 目录下的文件，防止误删
        if not str(path).startswith(str(self._notes_dir)):
            return
        if path.exists():
            path.unlink()
            self.note_deleted.emit(str(path))

    def rename(self, old_path: str | Path, new_name: str) -> str:
        """重命名 .md 文件，返回新路径。

        new_name 不含路径，不含 .md 后缀，如 '新便签'。
        """
        old = Path(old_path).resolve()
        nname = new_name if new_name.endswith(".md") else f"{new_name}.md"
        new = (old.parent / nname).resolve()
        old.rename(new)
        # 通知上游：旧标签消失，新标签出现
        self.note_deleted.emit(str(old))
        self.note_added.emit(str(new))
        return str(new)

    # ── 读写内容 ────────────────────────────────────────────────

    def save_content(self, filepath: str | Path, content: str) -> None:
        """写入文件内容。"""
        Path(filepath).write_text(content, encoding="utf-8")

    def read_content(self, filepath: str | Path) -> str:
        """读取文件内容。"""
        try:
            return Path(filepath).read_text(encoding="utf-8")
        except FileNotFoundError:
            return ""

    # ── 文件监听 ────────────────────────────────────────────────

    def start_watching(self) -> None:
        """启动 watchdog 轮询监听。"""
        if self._observer is not None:
            return
        handler = _NotesEventHandler(self)
        self._observer = Observer()
        self._observer.schedule(handler, str(self._notes_dir), recursive=False)
        self._observer.start()

    def stop_watching(self) -> None:
        """停止 watchdog 监听。"""
        if self._observer is None:
            return
        self._observer.stop()
        self._observer.join(timeout=3)
        self._observer = None

    # ── 内部 ────────────────────────────────────────────────────

    def _build_note_info(self, filepath: Path) -> NoteInfo:
        """根据文件路径构造 NoteInfo。"""
        return NoteInfo(
            filepath=str(filepath.resolve()),
            filename=filepath.stem,  # 不含 .md 后缀
            content=self.read_content(filepath),
            last_modified=filepath.stat().st_mtime if filepath.exists() else 0.0,
        )

    def _on_watchdog_event(self, event_type: str, src_path: str) -> None:
        """接收 watchdog 事件，防抖后发射对应信号。

        仅处理 .md 文件。在 watchdog 线程中调用 → 通过 QTimer
        异步发射信号确保线程安全（实际上 watchdog 事件的 handler
        在独立线程，但 Signal.emit() 在 Qt 5.15+ 已经是线程安全的）。
        """
        path_obj = Path(src_path)
        if path_obj.suffix.lower() != ".md":
            return

        # 防抖检查
        key = (event_type, str(path_obj.resolve()))
        now = time.time()
        last = self._debounce.get(key, 0)
        if now - last < (_DEBOUNCE_MS / 1000.0):
            return
        self._debounce[key] = now

        # 发射信号
        fp = str(path_obj.resolve())
        if event_type == "modified":
            self.note_changed.emit(fp)
        elif event_type == "created":
            self.note_added.emit(fp)
        elif event_type == "deleted":
            self.note_deleted.emit(fp)
