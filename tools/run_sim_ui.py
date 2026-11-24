"""界面端到端模拟：用临时目录伪装成 SD 卡与移动硬盘，跑一次真实任务并截图。

安全措施：整个进程内把设备枚举替换为模拟设备，绝不会碰到真实磁盘。
用法：py -3.14 tools/run_sim_ui.py
产物：tools/run_copy.png（复制/校验中）、tools/run_done.png（完成）
"""
from __future__ import annotations

import os
import shutil
import sys
import tempfile
from pathlib import Path

from PIL import ImageGrab

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE / "tools"))

# 测试隔离：配置/日志/状态写到临时目录，并强制演练模式（绝不真的休眠/关机）
_SIM_HOME = Path(tempfile.mkdtemp(prefix="sdbak_home_"))
os.environ["SDBAK_HOME"] = str(_SIM_HOME)
os.environ["SDBAK_DRY_RUN"] = "1"

from sdbackup import config as cfgmod  # noqa: E402
from sdbackup import devices as devmod  # noqa: E402
from sdbackup import ui  # noqa: E402
import win_capture  # noqa: E402


class FakeDevice(devmod.Device):
    def __init__(self, root: str, letter: str, display: str, kind: str,
                 total: int, free: int):
        super().__init__(root=root, label=display, fs="exFAT", total=total, free=free,
                         drive_type=2, bus=7, bus_name="USB", product="Card Reader")
        self._letter = letter
        self._kind = kind

    @property
    def letter(self) -> str:  # type: ignore[override]
        return self._letter

    @property
    def kind(self) -> str:  # type: ignore[override]
        return self._kind


def build_fixture(root: Path) -> None:
    big = 8 * 1024 * 1024
    chunk = bytes(range(256)) * (big // 256)
    plan = {
        "DCIM/100MSDCF/DSC0100.ARW": 40 * big,
        "DCIM/100MSDCF/DSC0101.ARW": 24 * big,
        "DCIM/100MSDCF/DSC0102.JPG": 8 * big,
        "DCIM/100MSDCF/DSC0103.JPG": 4 * big,
        "PRIVATE/M4ROOT/CLIP/C0100.MP4": 56 * big,
        "PRIVATE/AVCHD/BDMV/STREAM/00000.MTS": 16 * big,
        "PRIVATE/M4ROOT/CLIP/C0100M01.XML": 16 * 1024,
        "PRIVATE/AVCHD/BDCMF/INDEX.CPI": 2048,
        "NOTES/shooting-notes.txt": 1024,
    }
    for rel, size in plan.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "wb") as fh:
            written = 0
            while written < size:
                n = min(len(chunk), size - written)
                fh.write(chunk[:n])
                written += n


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="sdbak_ui_"))
    src = tmp / "SD"; dst = tmp / "HDD"
    src.mkdir(); dst.mkdir()
    build_fixture(src)
    print(f"测试数据：{tmp}")

    fake_src = FakeDevice(str(src), "E:", "SONY SDXC", "SD 卡", 64 * 2**30, 55 * 2**30)
    fake_dst = FakeDevice(str(dst), "F:", "Elements", "USB 外置硬盘", 2 * 2**30, 1800 * 2**30)
    devmod.list_devices = lambda include_network=False: [fake_src, fake_dst]  # type: ignore

    cfgmod.clear_state()
    app = ui.App()
    app._warn_unfinished = lambda: None          # 跳过"未完成任务"提示
    app.watcher.stop()
    while not app.queue.empty():                 # 丢掉启动时的真实设备事件
        app.queue.get()
    app._apply_devices([fake_src, fake_dst])
    app.src_combo.set(fake_src.title())
    app.dst_combo.set(fake_dst.title())
    app._on_device_select()
    app.cfg["unattended"] = True                 # 跳过确认框
    app.cfg["delete_source"] = False             # 保留样本文件，便于重复测试
    app.cfg["finish_action"] = "none"
    app.cfg["unattended_action"] = "hibernate"   # 演练模式下验证：只记录，不真执行
    app.unattended_var.set(True)                 # 界面开关需同步，否则会弹确认框
    app.delete_var.set(False)
    app.mode_var.set("camera")

    def grab(name: str) -> None:
        app.update_idletasks()
        print("  geometry:", app.winfo_width(), "x", app.winfo_height(),
              "| bottom bar y:", app.start_btn.winfo_rooty() - app.winfo_rooty(),
              "| log h:", app.log_text.winfo_height())
        path = str(HERE / "tools" / name)
        try:
            win_capture.capture_window(win_capture.hwnd_of(app), path)
            print("grab ok (PrintWindow)", name)
            return
        except Exception as exc:
            print("PrintWindow failed:", exc)
        x, y = app.winfo_rootx(), app.winfo_rooty()
        w, h = app.winfo_width(), app.winfo_height()
        ImageGrab.grab(bbox=(x, y, x + w, y + h)).save(path)

    def start() -> None:
        chosen = app._selected(app.src_combo)
        assert chosen is not None and chosen.root == str(src), "源设备选择异常，已中止"
        app._on_start()

    app.after(300, start)
    app.after(1500, lambda: grab("run_copy.png"))
    app.after(16000, lambda: grab("run_done.png"))
    app.after(16800, lambda: (setattr(app, "_closing", True), app.destroy()))
    app.mainloop()

    summary = app.engine.summary if app.engine else {}
    print("summary:", {k: summary.get(k) for k in
                       ("files_total", "files_ok", "transfer_failed", "source_errors",
                        "exists_failure", "target_dir", "cancelled")})
    shutil.rmtree(tmp, ignore_errors=True)
    shutil.rmtree(_SIM_HOME, ignore_errors=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
