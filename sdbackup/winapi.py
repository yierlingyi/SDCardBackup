"""Windows 原生 API 封装（ctypes 实现，无第三方依赖）。

提供：逻辑驱动器枚举、卷信息/容量、物理磁盘号、总线类型与厂商型号、
刷写缓存、安全弹出、休眠/关机、阻止系统睡眠。
"""
from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import os
import struct
import subprocess

kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
try:
    powrprof = ctypes.WinDLL("powrprof")
except OSError:  # pragma: no cover
    powrprof = None

# ---------------------------------------------------------------- 常量
DRIVE_UNKNOWN = 0
DRIVE_NO_ROOT_DIR = 1
DRIVE_REMOVABLE = 2
DRIVE_FIXED = 3
DRIVE_REMOTE = 4
DRIVE_CDROM = 5
DRIVE_RAMDISK = 6

GENERIC_READ = 0x80000000
GENERIC_WRITE = 0x40000000
FILE_SHARE_READ = 0x00000001
FILE_SHARE_WRITE = 0x00000002
OPEN_EXISTING = 3
INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value

IOCTL_STORAGE_GET_DEVICE_NUMBER = 0x2D1080
IOCTL_STORAGE_QUERY_PROPERTY = 0x002D1400
IOCTL_STORAGE_MEDIA_REMOVAL = 0x002D4804
IOCTL_STORAGE_EJECT_MEDIA = 0x2D4808
FSCTL_LOCK_VOLUME = 0x00090018
FSCTL_DISMOUNT_VOLUME = 0x00090020

ES_CONTINUOUS = 0x80000000
ES_SYSTEM_REQUIRED = 0x00000001

BUS_TYPE_NAMES = {
    0: "未知", 1: "SCSI", 2: "ATAPI", 3: "ATA", 4: "1394", 5: "SSA", 6: "光纤",
    7: "USB", 8: "RAID", 9: "iSCSI", 10: "SAS", 11: "SATA", 12: "SD", 13: "MMC",
    14: "虚拟", 15: "文件虚拟", 16: "存储空间", 17: "NVMe", 18: "SCM", 19: "UFS",
}

# ---------------------------------------------------------------- 结构体声明
kernel32.CreateFileW.argtypes = [wt.LPCWSTR, wt.DWORD, wt.DWORD, ctypes.c_void_p,
                                 wt.DWORD, wt.DWORD, wt.HANDLE]
kernel32.CreateFileW.restype = ctypes.c_void_p
kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
kernel32.CloseHandle.restype = wt.BOOL
kernel32.DeviceIoControl.argtypes = [ctypes.c_void_p, wt.DWORD, ctypes.c_void_p, wt.DWORD,
                                     ctypes.c_void_p, wt.DWORD, ctypes.POINTER(wt.DWORD),
                                     ctypes.c_void_p]
kernel32.DeviceIoControl.restype = wt.BOOL
kernel32.GetLogicalDrives.restype = wt.DWORD
kernel32.GetDriveTypeW.argtypes = [wt.LPCWSTR]
kernel32.GetDriveTypeW.restype = wt.UINT
kernel32.GetVolumeInformationW.argtypes = [wt.LPCWSTR, wt.LPWSTR, wt.DWORD,
                                           ctypes.POINTER(wt.DWORD), ctypes.POINTER(wt.DWORD),
                                           ctypes.POINTER(wt.DWORD), wt.LPWSTR, wt.DWORD]
kernel32.GetVolumeInformationW.restype = wt.BOOL
kernel32.GetDiskFreeSpaceExW.argtypes = [wt.LPCWSTR, ctypes.POINTER(wt.ULARGE_INTEGER),
                                         ctypes.POINTER(wt.ULARGE_INTEGER),
                                         ctypes.POINTER(wt.ULARGE_INTEGER)]
kernel32.GetDiskFreeSpaceExW.restype = wt.BOOL
kernel32.FlushFileBuffers.argtypes = [ctypes.c_void_p]
kernel32.FlushFileBuffers.restype = wt.BOOL
kernel32.SetThreadExecutionState.argtypes = [wt.DWORD]
kernel32.SetThreadExecutionState.restype = wt.DWORD


class STORAGE_DEVICE_DESCRIPTOR(ctypes.Structure):
    _fields_ = [
        ("Version", wt.DWORD),
        ("Size", wt.DWORD),
        ("DeviceType", ctypes.c_ubyte),
        ("DeviceTypeModifier", ctypes.c_ubyte),
        ("RemovableMedia", ctypes.c_ubyte),
        ("CommandQueueing", ctypes.c_ubyte),
        ("VendorIdOffset", wt.DWORD),
        ("ProductIdOffset", wt.DWORD),
        ("ProductRevisionOffset", wt.DWORD),
        ("SerialNumberOffset", wt.DWORD),
        ("BusType", ctypes.c_int),
        ("RawPropertiesLength", wt.DWORD),
    ]


# ---------------------------------------------------------------- 基础工具
def _open_volume(root: str, access: int = 0):
    """打开卷句柄，例如 'E:\\' -> \\\\.\\E:"""
    letter = root.rstrip("\\/")
    path = "\\\\.\\" + letter
    handle = kernel32.CreateFileW(path, access, FILE_SHARE_READ | FILE_SHARE_WRITE,
                                  None, OPEN_EXISTING, 0, None)
    if not handle or handle == INVALID_HANDLE_VALUE:
        return None
    return handle


def _close(handle) -> None:
    try:
        kernel32.CloseHandle(handle)
    except Exception:  # pragma: no cover
        pass


def _ioctl(handle, code: int, in_buf=None, out_size: int = 0):
    ret = wt.DWORD(0)
    in_ptr = None
    in_size = 0
    if isinstance(in_buf, (bytes, bytearray)):
        in_buf = ctypes.create_string_buffer(bytes(in_buf), len(in_buf))
        in_ptr, in_size = in_buf, len(in_buf)
    out_buf = ctypes.create_string_buffer(out_size) if out_size else None
    ok = kernel32.DeviceIoControl(handle, code, in_ptr, in_size, out_buf, out_size,
                                  ctypes.byref(ret), None)
    return bool(ok), (out_buf.raw if out_buf else b""), ret.value


# ---------------------------------------------------------------- 驱动器信息
def logical_drives() -> list[str]:
    mask = kernel32.GetLogicalDrives()
    result = []
    for i in range(26):
        if mask & (1 << i):
            result.append(f"{chr(65 + i)}:\\")
    return result


def drive_type(root: str) -> int:
    try:
        return int(kernel32.GetDriveTypeW(root))
    except Exception:  # pragma: no cover
        return DRIVE_UNKNOWN


def volume_info(root: str) -> dict:
    label_buf = ctypes.create_unicode_buffer(261)
    fs_buf = ctypes.create_unicode_buffer(261)
    serial = wt.DWORD(0)
    max_len = wt.DWORD(0)
    flags = wt.DWORD(0)
    ok = kernel32.GetVolumeInformationW(root, label_buf, 261, ctypes.byref(serial),
                                       ctypes.byref(max_len), ctypes.byref(flags),
                                       fs_buf, 261)
    if not ok:
        return {"label": "", "fs": "", "serial": 0, "ready": False}
    return {"label": label_buf.value, "fs": fs_buf.value, "serial": serial.value,
            "max_component": max_len.value, "flags": flags.value, "ready": True}


def disk_space(root: str) -> tuple[int, int, int]:
    """返回 (总容量, 可用容量, 总空闲) 字节。"""
    free_avail = wt.ULARGE_INTEGER()
    total = wt.ULARGE_INTEGER()
    free_total = wt.ULARGE_INTEGER()
    ok = kernel32.GetDiskFreeSpaceExW(root, ctypes.byref(free_avail),
                                      ctypes.byref(total), ctypes.byref(free_total))
    if not ok:
        return 0, 0, 0
    return total.value, free_avail.value, free_total.value


def physical_disk_number(root: str):
    """返回 (物理磁盘号, 分区号)，失败返回 (None, None)。"""
    if not root or len(root) < 2 or root[1] != ":":
        return None, None
    handle = _open_volume(root)
    if not handle:
        return None, None
    try:
        ok, raw, _ = _ioctl(handle, IOCTL_STORAGE_GET_DEVICE_NUMBER, out_size=12)
        if not ok or len(raw) < 12:
            return None, None
        _, dev_no, part_no = struct.unpack("<III", raw[:12])
        return dev_no, part_no
    finally:
        _close(handle)


def storage_descriptor(root: str) -> dict:
    """查询总线类型与厂商/型号字符串。"""
    info = {"bus": -1, "bus_name": "", "vendor": "", "product": "",
            "removable_media": False}
    if not root or len(root) < 2 or root[1] != ":":
        return info
    handle = _open_volume(root)
    if not handle:
        return info
    try:
        query = struct.pack("<II", 0, 0) + b"\x00" * 4  # StorageDeviceProperty
        ok, raw, _ = _ioctl(handle, IOCTL_STORAGE_QUERY_PROPERTY, query, 1024)
        if not ok or len(raw) < ctypes.sizeof(STORAGE_DEVICE_DESCRIPTOR):
            return info
        desc = STORAGE_DEVICE_DESCRIPTOR.from_buffer_copy(raw[:ctypes.sizeof(STORAGE_DEVICE_DESCRIPTOR)])

        def _string(offset: int) -> str:
            if not offset or offset >= len(raw):
                return ""
            end = raw.find(b"\x00", offset)
            if end < 0:
                end = len(raw)
            return raw[offset:end].decode("ascii", "ignore").strip()

        info["bus"] = desc.BusType
        info["bus_name"] = BUS_TYPE_NAMES.get(desc.BusType, str(desc.BusType))
        info["vendor"] = _string(desc.VendorIdOffset)
        info["product"] = _string(desc.ProductIdOffset)
        info["removable_media"] = bool(desc.RemovableMedia)
        return info
    except Exception:  # pragma: no cover
        return info
    finally:
        _close(handle)


# ---------------------------------------------------------------- 缓存刷写 / 弹出
def flush_volume(root: str) -> tuple[bool, str]:
    """刷新卷写入缓存。无管理员权限时降级为尽力而为。"""
    handle = _open_volume(root, GENERIC_READ | GENERIC_WRITE)
    access_note = ""
    if not handle:
        handle = _open_volume(root, 0)
        access_note = "（无写权限，已尽力刷新）"
    if not handle:
        return False, "无法打开卷句柄，缓存刷新失败"
    try:
        ok = bool(kernel32.FlushFileBuffers(handle))
        return ok, ("缓存已刷新" + access_note) if ok else "FlushFileBuffers 调用失败" + access_note
    finally:
        _close(handle)


def eject_volume(root: str) -> tuple[bool, str]:
    """尝试安全弹出（锁定卷 -> 卸载卷 -> 允许移除 -> 弹出）。"""
    handle = _open_volume(root, GENERIC_READ | GENERIC_WRITE)
    if not handle:
        handle = _open_volume(root, 0)
    if not handle:
        return False, "无法打开卷（可能正被其他程序占用）"
    try:
        kernel32.FlushFileBuffers(handle)
        ok_lock, _, _ = _ioctl(handle, FSCTL_LOCK_VOLUME)
        if not ok_lock:
            return False, "卷锁定失败（有程序正在使用该磁盘）"
        _ioctl(handle, FSCTL_DISMOUNT_VOLUME)
        _ioctl(handle, IOCTL_STORAGE_MEDIA_REMOVAL, b"\x00\x00\x00\x00")
        ok_eject, _, _ = _ioctl(handle, IOCTL_STORAGE_EJECT_MEDIA)
        if ok_eject:
            return True, "已安全弹出"
        return False, "设备不支持弹出指令（数据已刷新，可手动拔出）"
    except Exception as exc:  # pragma: no cover
        return False, f"弹出异常: {exc}"
    finally:
        _close(handle)


def is_removable_drive(root: str) -> bool:
    """是否为可移动设备（SD 卡 / U 盘 / USB 硬盘）。固定硬盘不允许弹出。"""
    if not root or len(root) < 2 or root[1] != ":":
        return False
    if drive_type(root) == DRIVE_REMOVABLE:
        return True
    bus = storage_descriptor(root)["bus"]
    return bus in (7, 12, 13)   # USB / SD / MMC


def is_system_drive(root: str) -> bool:
    """是否系统盘（绝不允许弹出/卸载）。"""
    system = os.environ.get("SystemDrive", "C:").upper().rstrip("\\")
    return root[:2].upper() == system[:2]


# ---------------------------------------------------------------- 电源
def keep_awake() -> None:
    """阻止系统在长任务中自动睡眠（当前线程生效）。"""
    try:
        kernel32.SetThreadExecutionState(ES_CONTINUOUS | ES_SYSTEM_REQUIRED)
    except Exception:  # pragma: no cover
        pass


def allow_sleep() -> None:
    try:
        kernel32.SetThreadExecutionState(ES_CONTINUOUS)
    except Exception:  # pragma: no cover
        pass


def suspend(hibernate: bool = True) -> tuple[bool, str]:
    """休眠 / 睡眠。"""
    if powrprof is None:
        return False, "powrprof 不可用"
    try:
        ok = bool(powrprof.SetSuspendState(1 if hibernate else 0, 0, 0))
        if ok:
            return True, "已执行休眠" if hibernate else "已执行睡眠"
        if hibernate:
            ok2 = bool(powrprof.SetSuspendState(0, 0, 0))
            if ok2:
                return True, "休眠不可用（系统未启用休眠），已改为睡眠"
            return False, "休眠/睡眠调用失败（可能缺少权限）"
        return False, "睡眠调用失败（可能缺少权限）"
    except Exception as exc:  # pragma: no cover
        return False, f"电源调用异常: {exc}"


def shutdown_system(delay: int = 0) -> tuple[bool, str]:
    try:
        subprocess.run(["shutdown", "/s", "/f", "/t", str(max(0, delay))],
                       capture_output=True, text=True, timeout=20,
                       creationflags=0x08000000)
        return True, f"已提交关机命令（{delay} 秒后）"
    except Exception as exc:  # pragma: no cover
        return False, f"关机命令失败: {exc}"


def is_admin() -> bool:
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:  # pragma: no cover
        return False


if __name__ == "__main__":  # 简单自检
    for drv in logical_drives():
        dt = drive_type(drv)
        if dt in (DRIVE_UNKNOWN, DRIVE_NO_ROOT_DIR, DRIVE_CDROM):
            continue
        vi = volume_info(drv)
        total, free, _ = disk_space(drv)
        desc = storage_descriptor(drv)
        disk_no, part = physical_disk_number(drv)
        print(f"{drv} type={dt} label={vi['label']!r} fs={vi['fs']} "
              f"total={total/2**30:.1f}G free={free/2**30:.1f}G "
              f"bus={desc['bus_name']} disk={disk_no} part={part} "
              f"product={desc['product']!r}")
    print("admin:", is_admin())
