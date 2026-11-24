"""程序入口：双击运行或 `py app.py`。"""
from __future__ import annotations

import ctypes
import os
import sys

BASE = os.path.dirname(os.path.abspath(__file__))
if BASE not in sys.path:
    sys.path.insert(0, BASE)


def _enable_dpi_awareness() -> None:
    """让界面在高分屏上保持清晰。"""
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)  # System DPI aware
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass


def main() -> None:
    _enable_dpi_awareness()
    from sdbackup.ui import main as run_gui
    run_gui()


if __name__ == "__main__":
    main()
