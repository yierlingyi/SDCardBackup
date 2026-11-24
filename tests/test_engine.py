"""引擎模拟测试：用临时目录模拟 SD 卡与目标硬盘。

覆盖：
1) 仅拍摄文件模式（复制、校验、删除、系统目录跳过）
2) 传输异常 -> 重传成功
3) 传输异常 -> 重传 3 次仍失败 -> 报错文件
4) 源文件异常 -> 跳过并继续
5) 全量模式
"""
from __future__ import annotations

import os
import random
import shutil
import sys
import tempfile
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sdbackup import config as cfgmod
from sdbackup import engine as eng


def build_fixture(root: Path) -> dict:
    files = {
        "DCIM/100MSDCF/DSC0001.ARW": 3 * 1024 * 1024 + 17,
        "DCIM/100MSDCF/DSC0001.JPG": 512 * 1024,
        "DCIM/100MSDCF/DSC0002.HIF": 256 * 1024,
        "PRIVATE/M4ROOT/CLIP/C0001.MP4": 2 * 1024 * 1024,
        "PRIVATE/AVCHD/BDMV/STREAM/00001.MTS": 1024 * 1024,
        "PRIVATE/M4ROOT/CLIP/C0001M01.XML": 8 * 1024,
        "PRIVATE/AVCHD/BDCHD/INDEX.CPI": 1024,
        "PRIVATE/M4ROOT/THMBNL/C0001.THM": 64 * 1024,
        "NOTES/readme.txt": 2048,
        "System Volume Information/tracking.log": 512,
        "$RECYCLE.BIN/junk.dat": 4096,
    }
    for rel, size in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(random.randbytes(size))
    return files


def run_engine(src: Path, dst: Path, cfg: cfgmod.AppConfig, hooks=None):
    events: list[dict] = []
    msgs: list[str] = []

    def emit(kind, **payload):
        payload["kind"] = kind
        events.append(payload)
        if kind == "log":
            msgs.append(payload["msg"])

    ext_data = cfgmod.load_extensions()
    engi = eng.BackupEngine(cfg, ext_data, str(src), str(dst), emit, hooks=hooks)
    thread = threading.Thread(target=engi.run, daemon=True)
    thread.start()
    thread.join(timeout=180)
    assert not thread.is_alive(), "引擎未在超时内结束"
    return engi, events, msgs


def make_cfg(**kwargs) -> cfgmod.AppConfig:
    cfg = cfgmod.AppConfig()
    cfg.update(unattended=True, delete_source=True, verify_hash=True,
               copy_mode="camera", max_retry=3)
    cfg.update(**kwargs)
    return cfg


def case_camera_ok(tmp: Path) -> None:
    print("\n=== 用例1：仅拍摄文件模式，全部成功 ===")
    src = tmp / "SD"; dst = tmp / "HDD"
    src.mkdir(); dst.mkdir()
    build_fixture(src)
    engi, events, msgs = run_engine(src, dst, make_cfg())
    target = Path(engi.summary["target_dir"])
    s = engi.summary
    print(f"扫描 {s['files_total']} 个 / {s['bytes_total']/2**20:.1f} MB，"
          f"通过 {s['files_ok']}，失败 {s['transfer_failed']}，源异常 {s['source_errors']}，"
          f"删除 {s['deleted']}")
    assert s["files_total"] == 8, f"拍摄文件识别数量错误: {s['files_total']}"
    assert s["files_ok"] == 8 and not s["exists_failure"]
    for rel in ["DCIM/100MSDCF/DSC0001.ARW", "PRIVATE/AVCHD/BDMV/STREAM/00001.MTS",
                "PRIVATE/M4ROOT/CLIP/C0001M01.XML"]:
        assert (target / rel).exists(), f"目标缺少 {rel}"
        assert not (src / rel).exists(), f"SD 卡未删除 {rel}"
    assert (src / "NOTES/readme.txt").exists(), "非拍摄文件被误删/误拷贝"
    assert not (target / "NOTES/readme.txt").exists(), "仅拍摄模式下复制了非拍摄文件"
    assert (src / "System Volume Information/tracking.log").exists(), "系统目录被误删"
    assert not list(target.rglob("backup_errors_*.txt")), "不应生成报错文件"
    scan = [e for e in events if e["kind"] == "scan"][0]
    print("后缀统计:", scan["by_ext"])


def case_retry_success(tmp: Path) -> None:
    print("\n=== 用例2：首次传输损坏 -> 重传成功 ===")
    src = tmp / "SD2"; dst = tmp / "HDD2"
    src.mkdir(); dst.mkdir()
    build_fixture(src)
    damaged = {"done": False}

    def after_copy(fe, dst_path: Path, attempt: int):
        if fe.rel.endswith(".ARW") and attempt == 1:   # 仅首次传输损坏
            damaged["done"] = True
            with open(dst_path, "r+b") as fh:
                fh.seek(0)
                fh.write(b"\xff")

    engi, events, msgs = run_engine(src, dst, make_cfg(), hooks={"after_copy": after_copy})
    s = engi.summary
    retries = [e for e in events if e["kind"] == "retry"]
    print(f"通过 {s['files_ok']}，失败 {s['transfer_failed']}，重传事件 {len(retries)}")
    assert s["files_ok"] == 8 and s["transfer_failed"] == 0
    assert len(retries) == 1, f"应有 1 次重传，实际 {len(retries)}"
    assert s["deleted"] == 8
    assert not s["exists_failure"]


def case_retry_exhausted(tmp: Path) -> None:
    print("\n=== 用例3：持续损坏 -> 重传 3 次仍失败 ===")
    src = tmp / "SD3"; dst = tmp / "HDD3"
    src.mkdir(); dst.mkdir()
    build_fixture(src)

    def after_copy(fe, dst_path: Path, attempt: int):
        if fe.rel.endswith(".MP4"):                     # 每次传输都损坏
            with open(dst_path, "r+b") as fh:
                fh.seek(0)
                fh.write(b"\xff")

    engi, events, msgs = run_engine(src, dst, make_cfg(), hooks={"after_copy": after_copy})
    s = engi.summary
    retries = [e for e in events if e["kind"] == "retry"]
    print(f"通过 {s['files_ok']}，传输失败 {s['transfer_failed']}，重传次数 {len(retries)}，"
          f"删除 {s['deleted']}，存在失败项 {s['exists_failure']}")
    assert s["transfer_failed"] == 1
    assert len(retries) == 3, f"应重传 3 次，实际 {len(retries)}"
    assert s["exists_failure"]
    # 默认策略：全部成功才删除 -> 一个都不删
    assert s["deleted"] == 0
    assert (src / "PRIVATE/M4ROOT/CLIP/C0001.MP4").exists(), "失败文件不应被删除"
    report = Path(s["report_path"])
    assert report.exists(), "未生成报错文件"
    text = report.read_text(encoding="utf-8-sig")
    for token in ("传输失败", "重试次数：3", "SHA-256 不一致", "存在失败项：是"):
        assert token in text, f"报错文件缺少：{token}"
    print("报错文件片段:")
    print("\n".join(text.splitlines()[:14]))


def case_source_error(tmp: Path) -> None:
    print("\n=== 用例4：源文件异常 -> 跳过并继续 ===")
    src = tmp / "SD4"; dst = tmp / "HDD4"
    src.mkdir(); dst.mkdir()
    build_fixture(src)

    def after_copy(fe, dst_path: Path, attempt: int):
        if fe.rel.endswith(".JPG"):
            with open(dst_path, "r+b") as fh:
                fh.seek(10)
                fh.write(b"\xee\xee")

    events: list[dict] = []

    def emit(kind, **payload):
        payload["kind"] = kind
        events.append(payload)

    cfg = make_cfg()
    engi = eng.BackupEngine(cfg, cfgmod.load_extensions(), str(src), str(dst), emit,
                            hooks={"after_copy": after_copy})
    # 模拟 SD 卡坏块：源文件自检返回不可读
    engi._source_self_check = lambda fe: (False, None, "源文件读取失败（模拟坏块 CRC 错误）")
    engi.run()
    s = engi.summary
    print(f"通过 {s['files_ok']}，源文件异常 {s['source_errors']}，删除 {s['deleted']}")
    assert s["source_errors"] == 1 and s["files_ok"] == 7
    assert (src / "DCIM/100MSDCF/DSC0001.JPG").exists(), "源异常文件不应删除"
    text = Path(s["report_path"]).read_text(encoding="utf-8-sig")
    assert "源文件异常" in text and "模拟坏块" in text
    assert s["deleted"] == 0, "存在失败项时不应删除"


def case_full_mode(tmp: Path) -> None:
    print("\n=== 用例5：全量复制模式 + 按文件删除策略 ===")
    src = tmp / "SD5"; dst = tmp / "HDD5"
    src.mkdir(); dst.mkdir()
    build_fixture(src)
    cfg = make_cfg(copy_mode="full", delete_policy="per_file")
    engi, events, msgs = run_engine(src, dst, cfg)
    s = engi.summary
    target = Path(s["target_dir"])
    print(f"扫描 {s['files_total']} 个，删除 {s['deleted']} 个")
    assert s["files_total"] == 9, f"全量模式应复制 9 个非系统文件，实际 {s['files_total']}"
    assert (target / "NOTES/readme.txt").exists()
    assert not (target / "System Volume Information").exists()
    assert s["deleted"] == 9


def main() -> int:
    random.seed(7)
    tmp = Path(tempfile.mkdtemp(prefix="sdbak_test_"))
    print(f"临时目录: {tmp}")
    try:
        case_camera_ok(tmp)
        case_retry_success(tmp)
        case_retry_exhausted(tmp)
        case_source_error(tmp)
        case_full_mode(tmp)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    print("\n全部用例通过 ✅")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
