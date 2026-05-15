"""Wallpaper_Notes — 桌面便签。

进程入口：创建 QApplication，启动 App 主控。
不包含任何业务逻辑。
"""

import sys
from pathlib import Path

from PySide6.QtWidgets import QApplication

from app import App

# 项目根目录（main.py 所在目录）
_APP_ROOT = Path(__file__).resolve().parent


def main() -> None:
    app = QApplication(sys.argv)
    app.setApplicationName("Wallpaper_Notes")

    controller = App(
        notes_dir=_APP_ROOT / "notes",
        config_path=_APP_ROOT / "config.json",
        theme_path=_APP_ROOT / "theme.json",
    )
    controller.run()


if __name__ == "__main__":
    main()
