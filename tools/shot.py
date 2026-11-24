"""启动界面并截图，用于开发期界面检查。"""
from __future__ import annotations

import ctypes
import os
import subprocess
import sys
import time
from ctypes import wintypes

from PIL import ImageGrab

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TITLE = "SD 卡自动备份与校验工具"


def find_window(substr: str):
    user32 = ctypes.windll.user32
    found = []

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def cb(hwnd, _):
        length = user32.GetWindowTextLengthW(hwnd)
        if length:
            buf = ctypes.create_unicode_buffer(length + 1)
            user32.GetWindowTextW(hwnd, buf, length + 1)
            if substr in buf.value and user32.IsWindowVisible(hwnd):
                found.append(hwnd)
        return True

    user32.EnumWindows(cb, 0)
    return found[0] if found else None


def main() -> int:
    dpi = 96
    try:
        dpi = ctypes.windll.user32.GetDpiForSystem()
    except Exception:
        pass
    print("system dpi:", dpi, "screen:", ctypes.windll.user32.GetSystemMetrics(0),
          "x", ctypes.windll.user32.GetSystemMetrics(1))
    ctypes.windll.user32.SetCursorPos(5, 1060)
    env = dict(os.environ)
    env["SDBAK_HOME"] = os.path.join(HERE, "tools", "_shot_home")
    env["SDBAK_DRY_RUN"] = "1"
    os.makedirs(env["SDBAK_HOME"], exist_ok=True)
    proc = subprocess.Popen([sys.executable, os.path.join(HERE, "app.py")], cwd=HERE, env=env)
    out = os.path.join(HERE, "tools", "shot.png")
    try:
        time.sleep(4.0)
        hwnd = find_window(TITLE)
        if not hwnd:
            print("未找到窗口")
            return 1
        user32 = ctypes.windll.user32
        user32.SetForegroundWindow(hwnd)
        time.sleep(0.6)
        rect = wintypes.RECT()
        user32.GetWindowRect(hwnd, ctypes.byref(rect))
        bbox = (rect.left, rect.top, rect.right, rect.bottom)
        print("window rect:", bbox)
        ImageGrab.grab(bbox=bbox, all_screens=True).save(out)
        print("saved:", out)
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except Exception:
            proc.kill()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
