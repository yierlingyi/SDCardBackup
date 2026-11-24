"""设备枚举与热插拔监听。"""
from __future__ import annotations

import os
import threading
import time
from dataclasses import dataclass, field
from typing import Callable, Optional

from . import winapi


@dataclass
class Device:
    root: str                      # "E:\\"
    label: str = ""
    fs: str = ""
    total: int = 0
    free: int = 0
    drive_type: int = 0
    bus: int = -1
    bus_name: str = ""
    vendor: str = ""
    product: str = ""
    serial: int = 0
    disk_no: Optional[int] = None
    partition: Optional[int] = None

    # ---------------------------------------------------------- 展示
    @property
    def letter(self) -> str:
        return self.root.rstrip("\\/")

    @property
    def kind(self) -> str:
        if self.bus in (12, 13):
            return "SD 卡"
        if self.drive_type == winapi.DRIVE_REMOVABLE:
            return "USB 移动存储"
        if self.bus == 7:
            return "USB 外置硬盘"
        if self.bus in (17, 11, 10, 3):
            return "内部固定磁盘"
        if self.drive_type == winapi.DRIVE_FIXED:
            return "固定磁盘"
        return "存储设备"

    @property
    def is_removable(self) -> bool:
        return self.drive_type == winapi.DRIVE_REMOVABLE or self.bus in (7, 12, 13)

    def title(self) -> str:
        """"E:  SDHC (exFAT) · SD 卡" """
        text = f"{self.letter}"
        if self.label:
            text += f"  {self.label}"
        if self.fs:
            text += f"  ({self.fs})"
        return text

    def capacity_text(self) -> str:
        return (f"总容量 {fmt_size(self.total)} · 可用 {fmt_size(self.free)}"
                f" · {self.bus_name or '未知总线'}")

    def detail_text(self) -> str:
        bits = [self.kind, self.fs or "未知文件系统"]
        if self.product:
            bits.append(self.product)
        if self.disk_no is not None:
            bits.append(f"磁盘{self.disk_no}")
        return " · ".join(bits)

    def identity(self) -> tuple:
        """用于判断是否为同一物理设备/同一分区。"""
        return (self.disk_no, self.partition, self.serial, self.root.upper())

    def signature(self) -> tuple:
        return (self.root, self.label, self.total, self.fs)


def fmt_size(num: float) -> str:
    num = float(num or 0)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if num < 1024 or unit == "TB":
            if unit == "B":
                return f"{int(num)} B"
            return f"{num:.1f} {unit}"
        num /= 1024
    return f"{num:.1f} TB"


def list_devices(include_network: bool = False) -> list[Device]:
    devices: list[Device] = []
    for root in winapi.logical_drives():
        dt = winapi.drive_type(root)
        if dt in (winapi.DRIVE_UNKNOWN, winapi.DRIVE_NO_ROOT_DIR, winapi.DRIVE_CDROM):
            continue
        if dt == winapi.DRIVE_REMOTE and not include_network:
            continue
        vi = winapi.volume_info(root)
        if not vi.get("ready"):
            continue
        total, free, _ = winapi.disk_space(root)
        if total <= 0:
            continue
        desc = winapi.storage_descriptor(root)
        disk_no, part = winapi.physical_disk_number(root)
        devices.append(Device(
            root=root, label=vi.get("label", ""), fs=vi.get("fs", ""),
            total=total, free=free, drive_type=dt,
            bus=desc["bus"], bus_name=desc["bus_name"],
            vendor=desc["vendor"], product=desc["product"],
            serial=vi.get("serial", 0), disk_no=disk_no, partition=part,
        ))
    devices.sort(key=lambda d: d.root)
    return devices


def same_device(a: Device, b: Device) -> bool:
    if a.root.upper() == b.root.upper():
        return True
    if a.disk_no is not None and a.disk_no == b.disk_no:
        return True
    return False


def same_volume(a: Device, b: Device) -> bool:
    if a.root.upper() == b.root.upper():
        return True
    if a.serial and a.serial == b.serial:
        return True
    return False


class DeviceWatcher(threading.Thread):
    """轮询式热插拔监听（间隔 0.9 秒，满足 1-2 秒内刷新要求）。"""

    def __init__(self, on_change: Callable[[list[Device]], None], interval: float = 0.9):
        super().__init__(daemon=True, name="device-watcher")
        self.on_change = on_change
        self.interval = interval
        self._stop = threading.Event()
        self._last: Optional[list[tuple]] = None

    def run(self) -> None:
        while not self._stop.is_set():
            try:
                devices = list_devices()
                sig = [d.signature() for d in devices]
                if sig != self._last:
                    self._last = sig
                    self.on_change(devices)
            except Exception:
                pass
            self._stop.wait(self.interval)

    def stop(self) -> None:
        self._stop.set()


def wait_for_device(root: str, timeout: float) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if os.path.exists(root):
            return True
        time.sleep(0.2)
    return os.path.exists(root)
