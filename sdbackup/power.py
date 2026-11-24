"""电源动作（休眠 / 关机）的独立封装。

与复制引擎解耦，任何调用方都必须显式经过这里，便于：
* 演练模式（dry-run）下只记录、不真正让机器休眠或关机；
* 单元测试替换底层实现，不会影响真实系统；
* 统一处理"任务取消""存在失败项"等跳过条件。
"""
from __future__ import annotations

import os
from dataclasses import dataclass

from . import winapi

ACTION_TEXT = {"hibernate": "休眠", "shutdown": "关机", "none": "不操作"}
VALID_ACTIONS = ("hibernate", "shutdown", "none")


@dataclass
class PowerResult:
    action: str
    executed: bool
    message: str
    dry_run: bool

    def __bool__(self) -> bool:  # 便于 if result: ...
        return self.executed


def dry_run_enabled(cfg) -> bool:
    """演练模式：环境变量优先，其次看配置。"""
    env = os.environ.get("SDBAK_DRY_RUN", "").strip().lower()
    if env in ("1", "true", "yes", "on"):
        return True
    try:
        return bool(cfg.get("power_dry_run", False))
    except Exception:
        return False


def resolve_action(cfg, unattended: bool | None = None) -> str:
    """按模式取出本次应执行的电源动作。"""
    if unattended is None:
        unattended = bool(cfg["unattended"])
    action = cfg["unattended_action"] if unattended else cfg["finish_action"]
    return action if action in VALID_ACTIONS else "none"


def should_execute(action: str, summary: dict, cfg) -> tuple[bool, str]:
    """判断是否应该执行电源动作，返回 (是否执行, 原因)。"""
    if action == "none":
        return False, "未设置完成动作，保持开机"
    if summary.get("cancelled"):
        return False, "任务被取消，不执行电源动作"
    if summary.get("exists_failure") and not cfg["run_action_on_failure"]:
        return False, "存在失败项且未启用『失败时仍执行』，跳过电源动作"
    return True, ""


def execute(action: str, *, dry_run: bool = False) -> PowerResult:
    """执行休眠/关机。dry_run=True 时只返回计划，不触碰系统电源。"""
    if action not in ("hibernate", "shutdown"):
        return PowerResult(action, False, "无电源动作", dry_run)
    text = ACTION_TEXT[action]
    if dry_run:
        return PowerResult(action, False, f"演练模式：本应执行{text}，未对系统执行", True)
    if action == "hibernate":
        ok, msg = winapi.suspend(hibernate=True)
    else:
        ok, msg = winapi.shutdown_system(0)
    return PowerResult(action, ok, msg, False)
