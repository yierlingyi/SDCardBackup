"""设备丢失场景测试：复制中目标盘/源盘消失，应暂停写入、等待重试或取消。

全程使用临时目录，不涉及真实磁盘与电源动作。
用法：py -3.14 tests/test_device_lost.py
"""
from __future__ import annotations

import os
import queue
import shutil
import sys
import tempfile
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sdbackup import config as cfgmod  # noqa: E402
from sdbackup import engine as eng  # noqa: E402


def make_cfg() -> cfgmod.AppConfig:
    cfg = cfgmod.AppConfig()
    cfg.update(unattended=True, delete_source=False, copy_mode="full",
               verify_hash=False, max_retry=1)
    return cfg


def build(src: Path, size_mb: int) -> None:
    chunk = bytes(range(256)) * 4096          # 1 MB
    path = src / "DCIM" / "100MSDCF" / "BIG.MP4"
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "wb") as fh:
        for _ in range(size_mb):
            fh.write(chunk)


def wait_kind(events: queue.Queue, kind: str, timeout: float = 10.0) -> dict | None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            ev = events.get(timeout=0.2)
        except queue.Empty:
            continue
        if ev.get("kind") == kind:
            return ev
    return None


def case_target_removed_then_retry() -> None:
    print("=== 用例1：复制中目标盘消失 -> 暂停 -> 恢复后重试成功 ===")
    tmp = Path(tempfile.mkdtemp(prefix="sdbak_lost_"))
    src, dst = tmp / "SD", tmp / "HDD"
    src.mkdir(); dst.mkdir()
    build(src, 700)                            # 700MB，避免瞬间复制完

    q: queue.Queue = queue.Queue()

    def emit(kind, **kw):
        q.put({"kind": kind, **kw})

    flag = {"target": True}
    cfg = make_cfg()
    engine = eng.BackupEngine(cfg, cfgmod.load_extensions(), str(src), str(dst), emit,
                              hooks={"device_probe": lambda s, d: (s, d and flag["target"])})
    thread = threading.Thread(target=engine.run, daemon=True)
    thread.start()

    # 等待进入复制阶段，然后模拟"拔掉"目标盘
    assert wait_kind(q, "target", 10), "未创建备份目录"
    time.sleep(0.25)
    flag["target"] = False
    print("  已模拟拔出目标盘，等待引擎暂停…")
    lost = wait_kind(q, "device_lost", 15)
    assert lost is not None, "设备丢失未被检测到"
    assert engine.device_lost, "引擎未进入设备丢失暂停状态"
    print(f"  引擎已暂停：{lost.get('missing')}，写入已停止")

    # 停止期间不应产生新的成功日志
    before = len(engine.copied)
    time.sleep(0.8)
    assert len(engine.copied) == before, "暂停期间仍在写入"
    assert thread.is_alive(), "引擎不应直接退出"

    # "插回"磁盘并点重试
    flag["target"] = True
    engine.signal_retry()
    thread.join(timeout=120)
    assert not thread.is_alive(), "恢复后引擎未结束"
    s = engine.summary
    print(f"  恢复后结果：成功 {s['files_ok']}，失败 {s['transfer_failed']}，"
          f"取消 {s['cancelled']}")
    assert not s["cancelled"] and s["files_ok"] == 1
    shutil.rmtree(tmp, ignore_errors=True)


def case_target_removed_then_cancel() -> None:
    print("=== 用例2：设备丢失后选择取消 -> 立即结束，不崩溃 ===")
    tmp = Path(tempfile.mkdtemp(prefix="sdbak_lost2_"))
    src, dst = tmp / "SD", tmp / "HDD"
    src.mkdir(); dst.mkdir()
    build(src, 700)

    q: queue.Queue = queue.Queue()
    flag = {"target": True}
    cfg = make_cfg()
    engine = eng.BackupEngine(cfg, cfgmod.load_extensions(), str(src), str(dst),
                              lambda kind, **kw: q.put({"kind": kind, **kw}),
                              hooks={"device_probe": lambda s, d: (s, d and flag["target"])})
    thread = threading.Thread(target=engine.run, daemon=True)
    thread.start()
    assert wait_kind(q, "target", 10)
    time.sleep(0.25)
    flag["target"] = False
    assert wait_kind(q, "device_lost", 15) is not None
    engine.cancel()
    thread.join(timeout=20)
    assert not thread.is_alive(), "取消后引擎未退出"
    s = engine.summary
    print(f"  取消结果：cancelled={s['cancelled']}，已复制 {len(engine.copied)} 个")
    assert s["cancelled"]
    # 已复制的部分文件应保留在目标（若目录已被删则不存在，不做强制断言）
    shutil.rmtree(tmp, ignore_errors=True)


def case_source_removed_then_cancel() -> None:
    print("=== 用例3：源设备丢失 -> 暂停 -> 取消 ===")
    tmp = Path(tempfile.mkdtemp(prefix="sdbak_lost3_"))
    src, dst = tmp / "SD", tmp / "HDD"
    src.mkdir(); dst.mkdir()
    build(src, 700)
    q: queue.Queue = queue.Queue()
    flag = {"src": True}
    cfg = make_cfg()
    engine = eng.BackupEngine(cfg, cfgmod.load_extensions(), str(src), str(dst),
                              lambda kind, **kw: q.put({"kind": kind, **kw}),
                              hooks={"device_probe": lambda s, d: (s and flag["src"], d)})
    thread = threading.Thread(target=engine.run, daemon=True)
    thread.start()
    assert wait_kind(q, "target", 10)
    time.sleep(0.25)
    flag["src"] = False
    lost = wait_kind(q, "device_lost", 15)
    assert lost is not None and "源设备" in (lost.get("missing") or []), "未提示源设备丢失"
    engine.cancel()
    thread.join(timeout=20)
    assert not thread.is_alive()
    assert engine.summary["cancelled"]
    print("  源设备丢失被正确识别并允许取消")
    shutil.rmtree(tmp, ignore_errors=True)


def main() -> int:
    case_target_removed_then_retry()
    case_target_removed_then_cancel()
    case_source_removed_then_cancel()
    print("\n设备丢失测试全部通过 ✅")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
