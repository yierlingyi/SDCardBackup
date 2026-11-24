"""备份引擎：扫描 → 复制 → 校验 → 失败分类（重传/源文件异常）→ 删除 → 报告。

引擎运行在独立线程中，全部状态通过 emit(kind, **payload) 回调上抛给界面。
"""
from __future__ import annotations

import hashlib
import os
import shutil
import threading
import time
from collections import deque
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable, Optional

from . import config as cfgmod
from . import winapi

CHUNK = 4 * 1024 * 1024          # 4 MB 分块
MTIME_TOLERANCE = 2.0            # FAT32/exFAT 时间精度 2 秒
DEVICE_CHECK_INTERVAL = 0.6      # 设备存在性检测间隔（秒）


# ---------------------------------------------------------------- 工具
def now_str() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def ts_str() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S")


def long_path(path: str) -> str:
    """超过 240 字符时加 \\\\?\\ 前缀，规避 MAX_PATH 限制。"""
    path = os.fspath(path)
    if os.name != "nt":
        return path
    if path.startswith("\\\\?\\"):
        return path
    if len(path) >= 240:
        if path.startswith("\\\\"):
            return "\\\\?\\UNC\\" + path[2:]
        return "\\\\?\\" + os.path.abspath(path)
    return path


def human_size(num: float) -> str:
    num = float(num or 0)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if num < 1024 or unit == "TB":
            return f"{int(num)} B" if unit == "B" else f"{num:.1f} {unit}"
        num /= 1024
    return f"{num:.1f} TB"


def human_time(seconds: Optional[float]) -> str:
    if seconds is None or seconds < 0 or seconds != seconds:
        return "--:--"
    seconds = int(seconds)
    hours, rem = divmod(seconds, 3600)
    minutes, secs = divmod(rem, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes:02d}:{secs:02d}"


# ---------------------------------------------------------------- 数据结构
class TaskCancelled(Exception):
    """用户取消任务。"""


class TaskAborted(Exception):
    """前置条件不满足，任务无法开始。"""


class DeviceLost(Exception):
    """源或目标设备在任务中消失。"""


@dataclass
class FileEntry:
    src: Path
    rel: str
    size: int
    mtime: float
    ext: str = ""


@dataclass
class FailureRecord:
    rel: str
    kind: str
    attempts: int
    reason: str
    timestamp: str
    src_path: str = ""
    dst_path: str = ""


# ---------------------------------------------------------------- 引擎
class BackupEngine:
    def __init__(self, cfg: cfgmod.AppConfig, ext_data: dict, src_root: str,
                 dst_root: str, emit: Callable[..., None], hooks: dict | None = None):
        self.cfg = cfg
        self.exts = cfgmod.camera_extension_set(ext_data)
        self.ext_groups = cfgmod.extension_groups(ext_data)
        self.src = os.path.abspath(src_root)
        self.dst = os.path.abspath(dst_root)
        self.emit = emit
        self.hooks = hooks or {}

        self.stop_flag = threading.Event()
        self.pause_flag = threading.Event()
        self._retry_evt = threading.Event()
        self._confirm_evt = threading.Event()
        self._confirm_ans = False
        self.device_lost = False
        self.phase = "准备"
        # 测试接缝：可替换为自定义探测函数以模拟设备拔出，正式运行保持 None
        self.device_probe = self.hooks.get("device_probe")

        self.entries: list[FileEntry] = []
        self.copied: dict[str, str] = {}
        self.copy_errors: dict[str, str] = {}
        self.verify_failed: dict[str, str] = {}
        self.verified_ok: set[str] = set()
        self.failures: list[FailureRecord] = []
        self.source_errors: list[FailureRecord] = []
        self.delete_failed: list[tuple[str, str]] = []
        self.deleted: list[str] = []

        self.total_bytes = 0
        self.bytes_done = 0
        self.files_total = 0
        self.files_done = 0
        self.phase_done = 0
        self.phase_total = 0
        self.file_done = 0
        self.file_total = 0
        self.speed = 0.0
        self.current = ""

        self.target_dir: Optional[Path] = None
        self._speed_pts: deque = deque()
        self._last_progress = 0.0
        self._last_dev_check = 0.0
        self._paused_announced = False
        self._copy_counts: dict[str, int] = {}
        self._log_file = None
        self._log_handle = None
        self.summary: dict = {}

    # ------------------------------------------------------------ 控制接口
    def cancel(self) -> None:
        self.stop_flag.set()
        self._confirm_evt.set()
        self._retry_evt.set()

    def toggle_pause(self) -> bool:
        if self.pause_flag.is_set():
            self.pause_flag.clear()
        else:
            self.pause_flag.set()
        return self.pause_flag.is_set()

    def resume(self) -> None:
        self.pause_flag.clear()

    def signal_retry(self) -> None:
        self._retry_evt.set()

    def confirm(self, answer: bool) -> None:
        self._confirm_ans = bool(answer)
        self._confirm_evt.set()

    @property
    def is_paused(self) -> bool:
        return self.pause_flag.is_set()

    # ------------------------------------------------------------ 主流程
    def run(self) -> dict:
        started = time.time()
        started_at = now_str()
        cfgmod.ensure_dirs()
        self._open_log(started_at)
        winapi.keep_awake()
        cfgmod.write_state({"started": started_at, "src": self.src, "dst": self.dst,
                            "mode": self.cfg["copy_mode"]})
        self._log("INFO", "=" * 62)
        self._log("INFO", f"任务开始：{self.src}  →  {self.dst}")
        self._log("INFO", f"复制模式：{'仅拍摄文件' if self.cfg['copy_mode'] == 'camera' else '全量复制'}"
                          f" ｜ 校验：{'大小+时间+SHA-256' if self.cfg['verify_hash'] else '大小+时间'}"
                          f" ｜ 删除源文件：{'是' if self.cfg['delete_source'] else '否'}"
                          f" ｜ 无人值守：{'是' if self.cfg['unattended'] else '否'}")
        cancelled = False
        error_text = ""
        try:
            self._validate()
            files = self._scan()
            self._prepare_target(files)
            if not self.cfg["unattended"]:
                if not self._ask_confirm(files):
                    raise TaskCancelled("用户取消")
            self._copy_all(files)
            self._verify_all()
            self._resolve_failures()
            self._delete_sources()
            self._write_report()
            self._finalize()
        except TaskCancelled as exc:
            cancelled = True
            self._log("WARN", f"任务已取消：{exc}")
        except TaskAborted as exc:
            cancelled = True
            error_text = str(exc)
            self._log("ERROR", f"任务中止：{exc}")
        except DeviceLost:
            cancelled = True
            self._log("ERROR", "设备丢失且未恢复，任务终止")
        except Exception as exc:  # 保底：不让线程静默死掉
            cancelled = True
            error_text = f"{type(exc).__name__}: {exc}"
            self._log("ERROR", f"任务异常终止：{error_text}")
        finally:
            winapi.allow_sleep()
            cfgmod.clear_state()
            self._close_log()

        exists_failure = bool(self.failures or self.source_errors or self.delete_failed)
        deleted_set = set(self.deleted)
        self.summary = {
            "cancelled": cancelled,
            "error": error_text,
            "started_at": started_at,
            "finished_at": now_str(),
            "duration": time.time() - started,
            "target_dir": str(self.target_dir) if self.target_dir else "",
            "files_total": self.files_total,
            "files_ok": len(self.verified_ok),
            "transfer_failed": len(self.failures),
            "source_errors": len(self.source_errors),
            "delete_failed": len(self.delete_failed),
            "bytes_total": self.total_bytes,
            "deleted": len(self.deleted),
            "deleted_bytes": sum(e.size for e in self.entries if e.rel in deleted_set),
            "delete_enabled": bool(self.cfg["delete_source"]),
            "delete_policy": self.cfg["delete_policy"],
            "report_path": str(getattr(self, "_report_path", "") or ""),
            "exists_failure": exists_failure,
            "mode": self.cfg["copy_mode"],
        }
        self._set_phase("已结束")
        self.emit("finished", summary=self.summary)
        self._log("INFO", f"任务结束：成功 {len(self.verified_ok)} 个，"
                          f"传输失败 {len(self.failures)} 个，源文件异常 {len(self.source_errors)} 个，"
                          f"耗时 {human_time(time.time() - started)}")
        return self.summary

    # ------------------------------------------------------------ 校验前置条件
    def _validate(self) -> None:
        if not os.path.exists(self.src):
            raise TaskAborted(f"源路径不存在：{self.src}")
        if not os.path.exists(self.dst):
            raise TaskAborted(f"目标路径不存在：{self.dst}")

        src_root = self.src.upper().rstrip("\\")
        dst_root = self.dst.upper().rstrip("\\")
        if src_root == dst_root:
            raise TaskAborted("源与目标是同一个位置，请重新选择")
        if dst_root.startswith(src_root + "\\"):
            raise TaskAborted("目标路径位于源设备内部，会导致自我复制")
        if src_root.startswith(dst_root + "\\"):
            raise TaskAborted("源路径位于目标设备内部，请重新选择")

        # 物理磁盘 / 卷序列号判重（仅盘符路径有效）
        if self._is_drive(self.src) and self._is_drive(self.dst):
            s_disk, s_part = winapi.physical_disk_number(self.src[:3])
            d_disk, d_part = winapi.physical_disk_number(self.dst[:3])
            if s_disk is not None and s_disk == d_disk and s_part == d_part:
                raise TaskAborted("源与目标是同一分区，请重新选择")
            if s_disk is not None and s_disk == d_disk:
                self._log("WARN", "检测到源与目标位于同一物理磁盘的不同分区，仍然允许继续")

    @staticmethod
    def _is_drive(path: str) -> bool:
        """判断是否为盘符根路径（如 E:\\ 或 E:），普通文件夹不算。"""
        if not path:
            return False
        p = path.rstrip("\\/")
        return len(p) == 2 and p[1] == ":"

    # ------------------------------------------------------------ 扫描
    def _scan(self) -> list[FileEntry]:
        self._set_phase("扫描")
        self._log("INFO", "正在扫描源设备文件…")
        mode = self.cfg["copy_mode"]
        skip = {str(s).lower() for s in self.cfg["skip_dirs"]}
        files: list[FileEntry] = []
        by_ext: dict[str, int] = {}
        total = 0
        stack = [self.src]
        while stack:
            cur = stack.pop()
            self._checkpoint()
            try:
                with os.scandir(cur) as it:
                    for entry in it:
                        try:
                            if entry.is_dir(follow_symlinks=False):
                                if entry.name.lower() in skip:
                                    self._log("INFO", f"跳过系统目录：{entry.path}")
                                    continue
                                stack.append(entry.path)
                            elif entry.is_file(follow_symlinks=False):
                                ext = os.path.splitext(entry.name)[1].upper()
                                if mode == "camera" and ext not in self.exts:
                                    continue
                                st = entry.stat(follow_symlinks=False)
                                rel = os.path.relpath(entry.path, self.src)
                                files.append(FileEntry(Path(entry.path), rel, st.st_size,
                                                       st.st_mtime, ext))
                                total += st.st_size
                                by_ext[ext] = by_ext.get(ext, 0) + 1
                        except OSError as exc:
                            self._log("WARN", f"无法读取条目 {entry.path}：{exc}")
            except OSError as exc:
                self._log("WARN", f"无法读取目录 {cur}：{exc}")
        files.sort(key=lambda f: f.rel.lower())

        self.entries = files
        self.total_bytes = total
        self.files_total = len(files)
        self.files_done = 0
        self.bytes_done = 0
        self.phase_done = 0
        self.phase_total = total
        self._log("INFO", f"扫描完成：{len(files)} 个文件，共 {human_size(total)}")
        if files:
            detail = "，".join(f"{k.lstrip('.') or '无后缀'}: {v}" for k, v in
                              sorted(by_ext.items(), key=lambda kv: -kv[1]))
            self._log("INFO", f"后缀统计：{detail}")
        self.emit("scan", count=len(files), bytes=total, by_ext=by_ext,
                  mode=mode, groups={k: v for k, v in self.ext_groups.items()})
        return files

    def _prepare_target(self, files: list[FileEntry]) -> None:
        if not files:
            raise TaskAborted("没有需要备份的文件（请检查复制模式或源设备内容）")
        need = int(self.total_bytes * 1.01) if self.total_bytes > 100 * 1024 * 1024 else \
            self.total_bytes + 100 * 1024 * 1024
        free = self._free_space(self.dst)
        self._log("INFO", f"空间检查：需要 {human_size(need)}（含余量），目标可用 {human_size(free)}")
        if free and free < need:
            raise TaskAborted(f"目标可用空间不足：需要 {human_size(need)}，仅剩 {human_size(free)}")

        stamp = ts_str()
        target = Path(self.dst) / f"SD_Backup_{stamp}"
        index = 1
        while target.exists():
            index += 1
            target = Path(self.dst) / f"SD_Backup_{stamp}_{index}"
        os.makedirs(target, exist_ok=True)
        self.target_dir = target
        self._log("OK", f"已创建备份目录：{target}")
        self.emit("target", path=str(target))

    @staticmethod
    def _free_space(root: str) -> int:
        try:
            return shutil.disk_usage(root).free
        except Exception:
            return 0

    # ------------------------------------------------------------ 复制
    def _copy_all(self, files: list[FileEntry]) -> None:
        self._set_phase("复制")
        self._t_phase = time.time()
        self._speed_pts.clear()
        self._log("INFO", "开始复制…")
        for index, fe in enumerate(files, start=1):
            self._checkpoint()
            self.files_done = index - 1
            self.file_total = fe.size
            self.file_done = 0
            self.current = fe.rel
            self._emit_progress(force=True)
            dst = Path(self.target_dir) / fe.rel
            try:
                digest = self._copy_with_recovery(fe, dst)
                self.copied[fe.rel] = digest
                self._log("OK", f"[{index}/{len(files)}] {fe.rel}  ({human_size(fe.size)})")
            except TaskCancelled:
                raise
            except DeviceLost:
                raise
            except OSError as exc:
                reason = self._err_text(exc)
                self.copy_errors[fe.rel] = reason
                self._log("ERROR", f"[{index}/{len(files)}] 复制失败 {fe.rel}：{reason}")
            finally:
                self.files_done = index
        self.bytes_done = self.total_bytes
        self._emit_progress(force=True)
        self._log("INFO", f"复制阶段完成：成功 {len(self.copied)} 个，失败 {len(self.copy_errors)} 个")

    def _copy_with_recovery(self, fe: FileEntry, dst: Path) -> str:
        for attempt in (1, 2):
            try:
                return self._copy_one(fe, dst)
            except DeviceLost:
                if attempt == 2:
                    raise
                self._handle_device_lost()
            except OSError as exc:
                src_ok, dst_ok = self._devices_present()
                if attempt == 1 and (not src_ok or not dst_ok):
                    self._handle_device_lost()
                    continue
                raise exc
        raise OSError("复制重试失败")

    def _copy_one(self, fe: FileEntry, dst: Path) -> str:
        os.makedirs(long_path(str(dst.parent)), exist_ok=True)
        hasher = hashlib.sha256()
        done = 0
        self.file_done = 0
        self.file_total = fe.size
        with open(long_path(str(fe.src)), "rb", buffering=0) as fin, \
                open(long_path(str(dst)), "wb", buffering=0) as fout:
            while True:
                self._checkpoint(writing=True)
                chunk = fin.read(CHUNK)
                if not chunk:
                    break
                fout.write(chunk)
                hasher.update(chunk)
                done += len(chunk)
                self.file_done = done
                self.bytes_done += len(chunk)
                self._emit_progress()
        os.utime(long_path(str(dst)), (fe.mtime, fe.mtime))
        if done != fe.size:
            raise OSError(f"源文件长度变化（已复制 {done}，预期 {fe.size}）")
        # 测试注入点（正式运行 hooks 为空）
        attempt = self._copy_counts.get(fe.rel, 0) + 1
        self._copy_counts[fe.rel] = attempt
        if "after_copy" in self.hooks:
            self.hooks["after_copy"](fe, dst, attempt)
        return hasher.hexdigest()

    # ------------------------------------------------------------ 校验
    def _verify_all(self) -> None:
        self._set_phase("校验")
        self.phase_done = 0
        self.phase_total = sum(fe.size for fe in self.entries if fe.rel in self.copied)
        self.bytes_done = 0
        self._speed_pts.clear()
        self._log("INFO", f"开始校验（{'大小+时间+SHA-256' if self.cfg['verify_hash'] else '大小+时间'}）…")
        for fe in self.entries:
            if fe.rel not in self.copied:
                continue
            self._checkpoint()
            self.current = fe.rel
            self.file_total = fe.size
            self.file_done = 0
            try:
                ok, reason = self._verify_one(fe)
            except DeviceLost:
                self._handle_device_lost()
                ok, reason = self._verify_one(fe)
            self.phase_done += fe.size
            self.bytes_done = self.phase_done
            if ok:
                self.verified_ok.add(fe.rel)
            else:
                self.verify_failed[fe.rel] = reason
                self._log("WARN", f"校验未通过 {fe.rel}：{reason}")
            self._emit_progress(force=True)
        self._log("INFO", f"校验阶段完成：通过 {len(self.verified_ok)} 个，"
                          f"未通过 {len(self.verify_failed)} 个，复制阶段失败 {len(self.copy_errors)} 个")

    def _verify_one(self, fe: FileEntry, src_hash: Optional[str] = None) -> tuple[bool, str]:
        dst = Path(self.target_dir) / fe.rel
        try:
            if not os.path.exists(long_path(str(dst))):
                return False, "目标文件缺失"
            size = os.path.getsize(long_path(str(dst)))
            if size != fe.size:
                return False, f"大小不一致（源 {fe.size} / 目标 {size}）"
            if self.cfg["verify_mtime"]:
                delta = abs(os.path.getmtime(long_path(str(dst))) - fe.mtime)
                if delta > MTIME_TOLERANCE:
                    return False, f"修改时间不一致（相差 {delta:.0f} 秒）"
            if self.cfg["verify_hash"]:
                expect = src_hash or self.copied.get(fe.rel)
                digest = self._hash_file(dst, fe.size)
                if digest is None:
                    return False, "目标文件读取失败"
                if expect and digest != expect:
                    return False, (f"SHA-256 不一致（源 {expect[:12]}… / 目标 {digest[:12]}…）")
            return True, ""
        except DeviceLost:
            raise
        except OSError as exc:
            src_ok, dst_ok = self._devices_present()
            if not src_ok or not dst_ok:
                raise DeviceLost()
            return False, self._err_text(exc)

    def _hash_file(self, path: Path, size: int = 0) -> Optional[str]:
        hasher = hashlib.sha256()
        read = 0
        try:
            with open(long_path(str(path)), "rb", buffering=0) as fh:
                while True:
                    self._checkpoint(writing=True)
                    chunk = fh.read(CHUNK)
                    if not chunk:
                        break
                    hasher.update(chunk)
                    read += len(chunk)
                    if self.phase == "校验":
                        self.file_done = read
                        self._emit_progress()
        except OSError as exc:
            src_ok, dst_ok = self._devices_present()
            if not src_ok or not dst_ok:
                raise DeviceLost()
            self._log("WARN", f"读取失败 {path}：{self._err_text(exc)}")
            return None
        return hasher.hexdigest()

    # ------------------------------------------------------------ 失败处理
    def _resolve_failures(self) -> None:
        pending = list(self.verify_failed.items()) + \
                  [(rel, reason) for rel, reason in self.copy_errors.items()
                   if rel not in self.verify_failed]
        if not pending:
            return
        self._set_phase("重传")
        self._log("WARN", f"存在 {len(pending)} 个未通过文件，开始判定与重传…")
        entry_map = {fe.rel: fe for fe in self.entries}
        max_retry = max(1, int(self.cfg["max_retry"]))
        for rel, reason in pending:
            self._checkpoint()
            fe = entry_map.get(rel)
            if fe is None:
                continue
            self.current = rel
            self.file_total = fe.size
            self.file_done = 0
            self._emit_progress(force=True)

            good, src_hash, why = self._source_self_check(fe)
            if not good:
                record = FailureRecord(rel, "源文件异常", 0, why, now_str(),
                                       str(fe.src), str(Path(self.target_dir) / rel))
                self.source_errors.append(record)
                self._log("ERROR", f"源文件异常，已跳过：{rel}（{why}）")
                continue

            self._log("WARN", f"判定为传输异常，开始重传：{rel}")
            attempts = 0
            solved = False
            last_reason = reason
            while attempts < max_retry:
                self._checkpoint()
                attempts += 1
                self.emit("retry", rel=rel, attempt=attempts, max_retry=max_retry)
                self._log("WARN", f"第 {attempts}/{max_retry} 次重传：{rel}")
                dst = Path(self.target_dir) / rel
                try:
                    if os.path.exists(long_path(str(dst))):
                        os.remove(long_path(str(dst)))
                    digest = self._copy_with_recovery(fe, dst)
                    self.copied[rel] = digest
                    ok, why = self._verify_one(fe, src_hash=digest)
                    if ok:
                        solved = True
                        self.verified_ok.add(rel)
                        self.verify_failed.pop(rel, None)
                        self._log("OK", f"重传成功（第 {attempts} 次）：{rel}")
                        break
                    last_reason = why
                except TaskCancelled:
                    raise
                except DeviceLost:
                    raise
                except OSError as exc:
                    last_reason = self._err_text(exc)
            if not solved:
                record = FailureRecord(rel, "传输失败", attempts, last_reason, now_str(),
                                       str(fe.src), str(Path(self.target_dir) / rel))
                self.failures.append(record)
                self._log("ERROR", f"重传 {attempts} 次仍失败，记入最终失败列表：{rel}（{last_reason}）")

    def _source_self_check(self, fe: FileEntry) -> tuple[bool, Optional[str], str]:
        """对 SD 卡源文件做可读性与哈希稳定性自检。"""
        self._log("INFO", f"源文件自检：{fe.rel}")
        first: Optional[str] = None
        second: Optional[str] = None
        for pass_no in (1, 2):
            try:
                digest = self._hash_source_raw(fe.src)
            except OSError as exc:
                return False, None, f"源文件读取失败（{self._err_text(exc)}）"
            if digest is None:
                return False, None, "源文件读取中断或存在坏块"
            if pass_no == 1:
                first = digest
                copy_hash = self.copied.get(fe.rel)
                if copy_hash and digest == copy_hash:
                    # 与复制时的源哈希一致：源文件稳定，问题在传输或目标端
                    return True, digest, ""
            else:
                second = digest
        if first != second:
            return False, None, "源文件两次读取哈希不一致（文件不稳定或存在坏块）"
        copy_hash = self.copied.get(fe.rel)
        if copy_hash and first != copy_hash:
            return False, None, "源文件内容与复制时不一致（复制后发生变化）"
        return True, first or "", ""

    def _hash_source_raw(self, path: Path) -> Optional[str]:
        hasher = hashlib.sha256()
        total = 0
        with open(long_path(str(path)), "rb", buffering=0) as fh:
            while True:
                self._checkpoint(writing=True)
                chunk = fh.read(CHUNK)
                if not chunk:
                    break
                hasher.update(chunk)
                total += len(chunk)
                self.file_done = total
                self._emit_progress()
        return hasher.hexdigest()

    # ------------------------------------------------------------ 删除源文件
    def _delete_sources(self) -> None:
        if not self.cfg["delete_source"]:
            self._log("INFO", "未启用『校验通过后删除源文件』，SD 卡内容保持不变")
            return
        self._set_phase("删除")
        policy = self.cfg["delete_policy"]
        ok_entries = [fe for fe in self.entries if fe.rel in self.verified_ok]
        has_failure = bool(self.failures or self.source_errors or self.copy_errors)
        if policy == "all_or_nothing" and has_failure:
            self._log("WARN", f"存在未成功文件（传输失败 {len(self.failures)}、"
                              f"源文件异常 {len(self.source_errors)}），"
                              f"按策略『全部成功才删除』保留 SD 卡全部内容")
            return
        if not ok_entries:
            self._log("WARN", "没有可删除的已校验文件")
            return
        self._log("INFO", f"开始删除 SD 卡已校验文件（策略：{policy}），共 {len(ok_entries)} 个")
        deleted_bytes = 0
        for index, fe in enumerate(ok_entries, start=1):
            self._checkpoint()
            self.current = fe.rel
            try:
                os.remove(long_path(str(fe.src)))
                self.deleted.append(fe.rel)
                deleted_bytes += fe.size
                self.file_done = index
                self.file_total = len(ok_entries)
                self._emit_progress(force=True)
            except OSError as exc:
                reason = self._err_text(exc)
                self.delete_failed.append((fe.rel, reason))
                self._log("ERROR", f"删除失败 {fe.rel}：{reason}（请手动处理）")
        self._log("OK", f"删除完成：成功 {len(self.deleted)} 个，释放 {human_size(deleted_bytes)}，"
                        f"失败 {len(self.delete_failed)} 个")
        if self.cfg["remove_empty_dirs"]:
            self._prune_empty_dirs()

    def _prune_empty_dirs(self) -> None:
        removed = 0
        skip = {str(s).lower() for s in self.cfg["skip_dirs"]}
        for cur, dirs, files in os.walk(self.src, topdown=False):
            if os.path.abspath(cur) == self.src:
                continue
            if os.path.basename(cur).lower() in skip:
                continue
            try:
                if not os.listdir(cur):
                    os.rmdir(cur)
                    removed += 1
            except OSError:
                pass
        if removed:
            self._log("INFO", f"已清理 {removed} 个空目录")

    # ------------------------------------------------------------ 报告
    def _write_report(self) -> None:
        self._report_path = None
        if not (self.failures or self.source_errors or self.delete_failed):
            self._log("OK", "无异常项，未生成报错文件")
            return
        lines = []
        lines.append("SD 卡自动备份与校验工具 —— 异常报告")
        lines.append(f"生成时间：{now_str()}")
        lines.append(f"源设备：{self.src}")
        lines.append(f"目标目录：{self.target_dir}")
        lines.append(f"复制模式：{'仅拍摄文件' if self.cfg['copy_mode'] == 'camera' else '全量复制'}")
        lines.append(f"校验方式：{'大小 + 修改时间 + SHA-256' if self.cfg['verify_hash'] else '大小 + 修改时间'}")
        lines.append("存在失败项：是")
        lines.append("")
        lines.append("[汇总]")
        lines.append(f"扫描文件：{self.files_total} 个 / {human_size(self.total_bytes)}")
        lines.append(f"校验通过：{len(self.verified_ok)} 个")
        lines.append(f"传输失败：{len(self.failures)} 个")
        lines.append(f"源文件异常：{len(self.source_errors)} 个")
        lines.append(f"SD 卡删除：成功 {len(self.deleted)} 个，失败 {len(self.delete_failed)} 个"
                     + ("（策略：全部成功才删除，已整体跳过）"
                        if self.cfg["delete_policy"] == "all_or_nothing"
                        and (self.failures or self.source_errors) else ""))
        lines.append("")
        if self.failures:
            lines.append(f"[传输失败] 重传 {self.cfg['max_retry']} 次后仍未通过")
            for i, rec in enumerate(self.failures, 1):
                lines.extend(self._record_block(i, rec))
        if self.source_errors:
            lines.append("[源文件异常] SD 卡源文件读取异常或不稳定，已跳过")
            for i, rec in enumerate(self.source_errors, 1):
                lines.extend(self._record_block(i, rec))
        if self.delete_failed:
            lines.append("[SD 卡删除失败] 校验通过但删除失败，请手动删除")
            for i, (rel, reason) in enumerate(self.delete_failed, 1):
                lines.append(f"{i}. 文件：{rel}")
                lines.append(f"   源路径：{os.path.join(self.src, rel)}")
                lines.append(f"   类型：删除失败")
                lines.append(f"   原因：{reason}")
                lines.append(f"   时间戳：{now_str()}")
                lines.append("")

        text = "\n".join(lines) + "\n"
        name = f"backup_errors_{ts_str()}.txt"
        target_path = Path(self.target_dir) / name
        try:
            with open(long_path(str(target_path)), "w", encoding="utf-8-sig") as fh:
                fh.write(text)
            self._report_path = target_path
            self._log("ERROR", f"已生成报错文件：{target_path}")
        except OSError as exc:
            self._log("ERROR", f"报错文件写入失败：{exc}")
        try:
            cfgmod.ensure_dirs()
            copy_path = cfgmod.LOG_DIR / name
            with open(copy_path, "w", encoding="utf-8-sig") as fh:
                fh.write(text)
        except OSError:
            pass
        self.emit("report", path=str(self._report_path or ""))

    @staticmethod
    def _record_block(index: int, rec: FailureRecord) -> list[str]:
        return [
            f"{index}. 文件：{rec.rel}",
            f"   源路径：{rec.src_path}",
            f"   目标路径：{rec.dst_path}",
            f"   类型：{rec.kind}",
            f"   重试次数：{rec.attempts}",
            f"   原因：{rec.reason}",
            f"   时间戳：{rec.timestamp}",
            "",
        ]

    # ------------------------------------------------------------ 收尾
    def _finalize(self) -> None:
        self._set_phase("收尾")
        if not self._is_drive(self.dst):
            self._set_phase("完成")
            return
        letter = self.dst[:3]
        ok, msg = winapi.flush_volume(letter)
        self._log("INFO" if ok else "WARN", f"目标设备缓存刷新：{msg}")
        if not self.cfg.get("eject_after_finish", True):
            self._log("INFO", "未启用安全弹出，跳过")
        elif winapi.is_system_drive(letter):
            self._log("WARN", "目标是系统盘，禁止弹出（仅刷新缓存）")
        elif not winapi.is_removable_drive(letter):
            self._log("INFO", "目标是固定磁盘，跳过安全弹出（仅刷新缓存）")
        else:
            ok, msg = winapi.eject_volume(letter)
            self._log("INFO" if ok else "WARN", f"目标设备安全弹出：{msg}")
        self._set_phase("完成")

    # ------------------------------------------------------------ 确认弹窗
    def _ask_confirm(self, files: list[FileEntry]) -> bool:
        by_ext: dict[str, int] = {}
        for fe in files:
            by_ext[fe.ext] = by_ext.get(fe.ext, 0) + 1
        free = self._free_space(self.dst)
        self._confirm_evt.clear()
        self._confirm_ans = False
        self.emit("confirm", count=len(files), bytes=self.total_bytes, by_ext=by_ext,
                  target_dir=str(self.target_dir), free=free,
                  delete_source=bool(self.cfg["delete_source"]),
                  mode=self.cfg["copy_mode"])
        while not self._confirm_evt.wait(0.2):
            if self.stop_flag.is_set():
                return False
        return self._confirm_ans

    # ------------------------------------------------------------ 设备丢失
    def _handle_device_lost(self) -> None:
        self.device_lost = True
        src_ok, dst_ok = self._devices_present()
        missing = []
        if not src_ok:
            missing.append("源设备")
        if not dst_ok:
            missing.append("目标设备")
        self._log("ERROR", f"{'与'.join(missing) or '设备'}已断开，任务暂停并禁止写入")
        self.emit("device_lost", missing=missing)
        self._retry_evt.clear()
        while True:
            if self.stop_flag.is_set():
                self.device_lost = False
                raise TaskCancelled("设备丢失，用户取消")
            if self._retry_evt.is_set():
                self._retry_evt.clear()
                src_ok, dst_ok = self._devices_present()
                if src_ok and dst_ok:
                    self.device_lost = False
                    self._log("OK", "设备已恢复，继续任务")
                    self.emit("device_lost_cleared")
                    return
                self._log("WARN", "设备仍未恢复，继续等待…")
                self.emit("device_lost", missing=missing, retry=True)
            time.sleep(0.2)

    # ------------------------------------------------------------ 内部工具
    def _devices_present(self) -> tuple[bool, bool]:
        """返回 (源存在, 目标存在)。"""
        src_ok = os.path.exists(self.src)
        dst_ok = os.path.exists(self.dst)
        if self.device_probe is not None:
            try:
                src_ok, dst_ok = self.device_probe(src_ok, dst_ok)
            except Exception:
                pass
        return bool(src_ok), bool(dst_ok)

    def _set_phase(self, name: str) -> None:
        self.phase = name
        self.emit("phase", value=name)

    def _checkpoint(self, writing: bool = False) -> None:
        if self.stop_flag.is_set():
            raise TaskCancelled("用户取消")
        if self.pause_flag.is_set():
            if not self._paused_announced:
                self._paused_announced = True
                self.emit("state", value="paused")
                self._log("WARN", "任务已暂停")
            while self.pause_flag.is_set():
                if self.stop_flag.is_set():
                    raise TaskCancelled("用户取消")
                time.sleep(0.15)
            self._paused_announced = False
            self.emit("state", value="running")
            self._log("INFO", "任务已继续")
        if writing:
            now = time.time()
            if now - self._last_dev_check >= DEVICE_CHECK_INTERVAL:
                self._last_dev_check = now
                src_ok, dst_ok = self._devices_present()
                if not src_ok or not dst_ok:
                    raise DeviceLost()

    def _emit_progress(self, force: bool = False) -> None:
        now = time.time()
        if not force and now - self._last_progress < 0.08:
            return
        self._last_progress = now
        total = max(1, self.phase_total or self.total_bytes)
        if self.phase == "复制":
            done = self.bytes_done
        elif self.phase == "删除":
            done = self.file_done
            total = max(1, self.file_total)
        else:
            done = min(total, self.phase_done + self.file_done)
        self._speed_pts.append((now, done))
        while len(self._speed_pts) > 2 and now - self._speed_pts[0][0] > 3.0:
            self._speed_pts.popleft()
        inst = 0.0
        if len(self._speed_pts) >= 2:
            dt = now - self._speed_pts[0][0]
            db = self._speed_pts[-1][1] - self._speed_pts[0][1]
            if dt > 0.05:
                inst = db / dt
        self.speed = inst if not self.speed else self.speed * 0.65 + inst * 0.35
        eta = (total - done) / self.speed if self.speed > 1024 else None
        self.emit("progress",
                  phase=self.phase,
                  percent=min(100.0, done / total * 100.0),
                  file_percent=(self.file_done / self.file_total * 100.0) if self.file_total else 0.0,
                  files_done=self.files_done,
                  files_total=self.files_total,
                  current=self.current,
                  speed=self.speed,
                  eta=eta,
                  done_text=human_size(done),
                  total_text=human_size(total),
                  file_done_text=human_size(self.file_done),
                  file_total_text=human_size(self.file_total))

    def _log(self, level: str, message: str) -> None:
        line = f"[{datetime.now().strftime('%H:%M:%S')}] [{level}] {message}"
        self.emit("log", level=level, msg=message, line=line)
        if self._log_handle:
            try:
                self._log_handle.write(line + "\n")
                self._log_handle.flush()
            except Exception:
                pass

    def _open_log(self, started_at: str) -> None:
        try:
            cfgmod.ensure_dirs()
            self._log_file = cfgmod.LOG_DIR / f"task_{ts_str()}.log"
            self._log_handle = open(self._log_file, "a", encoding="utf-8")
        except Exception:
            self._log_handle = None

    def _close_log(self) -> None:
        if self._log_handle:
            try:
                self._log_handle.close()
            except Exception:
                pass
            self._log_handle = None

    @property
    def log_path(self) -> str:
        return str(self._log_file) if self._log_file else ""

    @staticmethod
    def _err_text(exc: BaseException) -> str:
        if isinstance(exc, OSError):
            text = getattr(exc, "strerror", None) or str(exc)
            if getattr(exc, "winerror", None):
                text = f"{text}（WinError {exc.winerror}）"
            return text
        return str(exc)
