"""配置、拍摄文件后缀清单、运行路径。"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

APP_NAME = "SD 卡自动备份与校验工具"
VERSION = "1.0.0"


def app_dir() -> Path:
    """程序所在目录（兼容 PyInstaller 打包后的 exe）。"""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


def base_dir() -> Path:
    """数据目录：默认与程序同目录，可用环境变量 SDBAK_HOME 重定向（测试隔离用）。"""
    override = os.environ.get("SDBAK_HOME", "").strip()
    if override:
        return Path(override).expanduser().resolve()
    return app_dir()


APP_DIR = app_dir()
BASE_DIR = base_dir()
SETTINGS_PATH = BASE_DIR / "settings.json"
EXTENSIONS_PATH = BASE_DIR / "camera_extensions.json"
LOG_DIR = BASE_DIR / "logs"
STATE_DIR = BASE_DIR / "state"
STATE_PATH = STATE_DIR / "current_task.json"

DEFAULT_SETTINGS = {
    "copy_mode": "full",                 # full | camera
    "verify_hash": True,                 # SHA-256
    "verify_mtime": True,                # 大小 + 修改时间
    "delete_source": True,               # 校验通过后删除 SD 卡文件
    "delete_policy": "all_or_nothing",   # all_or_nothing | per_file
    "max_retry": 3,                      # 传输异常最多重传次数
    "skip_dirs": [
        "System Volume Information",
        "$RECYCLE.BIN",
        ".Trashes",
        ".Spotlight-V100",
        ".fseventsd",
        "found.000",
        "LOST.DIR",
    ],
    "remove_empty_dirs": False,
    "unattended": False,
    "unattended_action": "hibernate",    # hibernate | shutdown | none
    "finish_action": "none",             # hibernate | shutdown | none
    "countdown_seconds": 60,
    "run_action_on_failure": True,
    "power_dry_run": False,              # 演练模式：只记录不真正休眠/关机
    "eject_after_finish": True,          # 结束前刷新缓存并尝试安全弹出（仅可移动设备）
    "last_source": "",
    "last_target": "",
}

DEFAULT_EXTENSIONS = {
    "_说明": "camera_extensions.json —— 仅『拍摄文件』模式使用的后缀清单，可自行增删。后缀不区分大小写。",
    "photo": [".ARW", ".JPG", ".JPEG", ".HIF", ".HEIF"],
    "video": [".MP4", ".MTS"],
    "meta": [".XML", ".CPI", ".THM"],
    "extra": [],
    "_其他品牌示例": {
        "canon": [".CR2", ".CR3", ".MOV"],
        "nikon": [".NEF", ".NRW"],
        "fuji": [".RAF"],
        "panasonic": [".RW2"],
    },
}


class AppConfig:
    """settings.json 的轻量封装。"""

    def __init__(self, data: dict | None = None):
        self.data = dict(DEFAULT_SETTINGS)
        if data:
            for key, value in data.items():
                if key in self.data:
                    self.data[key] = value

    def __getitem__(self, key):
        return self.data.get(key, DEFAULT_SETTINGS.get(key))

    def __setitem__(self, key, value):
        self.data[key] = value

    def get(self, key, default=None):
        return self.data.get(key, default)

    def update(self, **kwargs):
        self.data.update(kwargs)

    @classmethod
    def load(cls) -> "AppConfig":
        if SETTINGS_PATH.exists():
            try:
                with open(SETTINGS_PATH, "r", encoding="utf-8") as fh:
                    return cls(json.load(fh))
            except Exception:
                pass
        return cls()

    def save(self) -> None:
        try:
            with open(SETTINGS_PATH, "w", encoding="utf-8") as fh:
                json.dump(self.data, fh, ensure_ascii=False, indent=2)
        except Exception:
            pass


def load_extensions(path: Path | None = None) -> dict:
    """读取后缀清单，缺失时自动生成默认文件。"""
    path = path or EXTENSIONS_PATH
    if not path.exists():
        try:
            with open(path, "w", encoding="utf-8") as fh:
                json.dump(DEFAULT_EXTENSIONS, fh, ensure_ascii=False, indent=2)
        except Exception:
            pass
        return DEFAULT_EXTENSIONS
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        if not isinstance(data, dict):
            raise ValueError("格式错误")
        return data
    except Exception:
        return DEFAULT_EXTENSIONS


def camera_extension_set(ext_data: dict) -> set[str]:
    result: set[str] = set()
    for key in ("photo", "video", "meta", "extra"):
        for ext in ext_data.get(key, []) or []:
            ext = str(ext).strip()
            if not ext:
                continue
            if not ext.startswith("."):
                ext = "." + ext
            result.add(ext.upper())
    return result


def extension_groups(ext_data: dict) -> dict[str, list[str]]:
    return {
        "photo": [e.upper() for e in ext_data.get("photo", [])],
        "video": [e.upper() for e in ext_data.get("video", [])],
        "meta": [e.upper() for e in ext_data.get("meta", [])],
        "extra": [e.upper() for e in ext_data.get("extra", [])],
    }


def ensure_dirs() -> None:
    for path in (LOG_DIR, STATE_DIR):
        os.makedirs(path, exist_ok=True)


def read_state() -> dict | None:
    try:
        if STATE_PATH.exists():
            with open(STATE_PATH, "r", encoding="utf-8") as fh:
                return json.load(fh)
    except Exception:
        pass
    return None


def write_state(data: dict) -> None:
    ensure_dirs()
    try:
        with open(STATE_PATH, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False, indent=2)
    except Exception:
        pass


def clear_state() -> None:
    try:
        if STATE_PATH.exists():
            STATE_PATH.unlink()
    except Exception:
        pass
