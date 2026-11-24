"""电源动作单元测试：全部使用替身函数，不会真正休眠/关机。

覆盖：动作解析、跳过规则、演练模式、真实调用路径。
用法：py -3.14 tests/test_power.py
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sdbackup import config as cfgmod  # noqa: E402
from sdbackup import power, winapi  # noqa: E402


def make_cfg(**kwargs) -> cfgmod.AppConfig:
    cfg = cfgmod.AppConfig()
    cfg.update(unattended=False, finish_action="none",
               unattended_action="hibernate", run_action_on_failure=True,
               power_dry_run=False)
    cfg.update(**kwargs)
    return cfg


def test_resolve_action() -> None:
    assert power.resolve_action(make_cfg(finish_action="shutdown")) == "shutdown"
    assert power.resolve_action(make_cfg(unattended=True)) == "hibernate"
    assert power.resolve_action(make_cfg(finish_action="none")) == "none"
    assert power.resolve_action(make_cfg(finish_action="whatever")) == "none"
    print("✓ 动作解析：普通模式取 finish_action，无人值守取 unattended_action")


def test_should_execute() -> None:
    cfg = make_cfg()
    ok, _ = power.should_execute("hibernate", {"cancelled": False, "exists_failure": False}, cfg)
    assert ok
    ok, reason = power.should_execute("hibernate", {"cancelled": True}, cfg)
    assert not ok and "取消" in reason
    ok, reason = power.should_execute(
        "hibernate", {"exists_failure": True}, make_cfg(run_action_on_failure=False))
    assert not ok and "失败项" in reason
    ok, _ = power.should_execute(
        "hibernate", {"exists_failure": True}, make_cfg(run_action_on_failure=True))
    assert ok
    ok, _ = power.should_execute("none", {}, cfg)
    assert not ok
    print("✓ 跳过规则：取消/失败项开关/未设置动作")


def test_dry_run_never_touches_system() -> None:
    calls: list[str] = []

    def boom(*_a, **_k):
        calls.append("real")
        raise AssertionError("演练模式不应调用系统电源接口")

    origin_suspend, origin_shutdown = winapi.suspend, winapi.shutdown_system
    winapi.suspend, winapi.shutdown_system = boom, boom
    try:
        for action in ("hibernate", "shutdown"):
            result = power.execute(action, dry_run=True)
            assert result.dry_run and not result.executed
            assert "演练模式" in result.message
            result2 = power.execute(action, dry_run=False) if False else None  # noqa: F841
    finally:
        winapi.suspend, winapi.shutdown_system = origin_suspend, origin_shutdown
    assert not calls, "演练模式仍然调用了真实电源接口"
    print("✓ 演练模式：只返回计划，不调用 suspend/shutdown")


def test_real_path_uses_winapi() -> None:
    calls: list[tuple] = []
    origin_suspend, origin_shutdown = winapi.suspend, winapi.shutdown_system
    winapi.suspend = lambda hibernate=True: (calls.append(("suspend", hibernate)) or (True, "已执行休眠"))
    winapi.shutdown_system = lambda delay=0: (calls.append(("shutdown", delay)) or (True, "已提交关机"))
    try:
        r1 = power.execute("hibernate", dry_run=False)
        r2 = power.execute("shutdown", dry_run=False)
    finally:
        winapi.suspend, winapi.shutdown_system = origin_suspend, origin_shutdown
    assert r1.executed and r2.executed
    assert calls == [("suspend", True), ("shutdown", 0)], calls
    print("✓ 真实路径：hibernate→suspend(True)，shutdown→shutdown_system(0)（已用替身拦截）")


def test_env_dry_run() -> None:
    cfg = make_cfg(power_dry_run=False)
    os.environ.pop("SDBAK_DRY_RUN", None)
    assert not power.dry_run_enabled(cfg)
    cfg["power_dry_run"] = True
    assert power.dry_run_enabled(cfg)
    cfg["power_dry_run"] = False
    os.environ["SDBAK_DRY_RUN"] = "1"
    try:
        assert power.dry_run_enabled(cfg)
    finally:
        os.environ.pop("SDBAK_DRY_RUN", None)
    print("✓ 演练开关：环境变量 SDBAK_DRY_RUN 优先生效")


def test_eject_guards() -> None:
    assert winapi.is_system_drive("C:\\")
    assert not winapi.is_removable_drive("C:\\")
    print("✓ 弹出保护：系统盘不弹出，固定磁盘不弹出")


def main() -> int:
    test_resolve_action()
    test_should_execute()
    test_dry_run_never_touches_system()
    test_real_path_uses_winapi()
    test_env_dry_run()
    test_eject_guards()
    print("\n电源模块测试全部通过（未触发任何真实休眠/关机）✅")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
