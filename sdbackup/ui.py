"""Tkinter 深色界面。"""
from __future__ import annotations

import ctypes
import os
import queue
import sys
import threading
import time
import tkinter as tk
from pathlib import Path
from tkinter import messagebox, ttk

from . import config as cfgmod
from . import devices as devmod
from . import engine as engmod
from . import power as powermod
from . import winapi

# ---------------------------------------------------------------- 配色
BG = "#14161b"
PANEL = "#1c2027"
PANEL2 = "#242a33"
PANEL3 = "#2c3440"
BORDER = "#2f3742"
TEXT = "#e6eaf2"
MUTED = "#8b95a7"
DIM = "#6b7484"
ACCENT = "#3b82f6"
ACCENT_HOVER = "#5b97ff"
GREEN = "#3ddc84"
AMBER = "#f0b429"
RED = "#ff5f57"

FONT_UI = ("Microsoft YaHei UI", 10)
FONT_BOLD = ("Microsoft YaHei UI", 10, "bold")
FONT_TITLE = ("Microsoft YaHei UI", 13, "bold")
FONT_SMALL = ("Microsoft YaHei UI", 9)
FONT_MONO = ("Consolas", 9)
FONT_BIG = ("Microsoft YaHei UI", 20, "bold")

ACTION_LABEL = {"hibernate": "休眠", "shutdown": "关机", "none": "不操作"}
ACTION_VALUE = {"休眠": "hibernate", "关机": "shutdown", "不操作": "none"}

LEVEL_COLOR = {"INFO": "#9aa7bd", "OK": GREEN, "WARN": AMBER, "ERROR": RED}


def ellipsize(text: str, limit: int = 96) -> str:
    if len(text) <= limit:
        return text
    head = limit * 2 // 3
    tail = limit - head - 1
    return text[:head] + "…" + text[-tail:]


class FlatButton(tk.Button):
    """扁平按钮，带悬停效果。"""

    def __init__(self, master, text, command, *, kind="default", width=None, **kw):
        palette = {
            "default": (PANEL2, TEXT, PANEL3),
            "primary": (ACCENT, "#ffffff", ACCENT_HOVER),
            "ghost": (PANEL, MUTED, PANEL2),
            "danger": ("#3a2226", "#ff9a94", "#4a2a2f"),
        }[kind]
        bg, fg, hover = palette
        kw.setdefault("padx", 14)
        kw.setdefault("pady", 6)
        super().__init__(master, text=text, command=command, bg=bg, fg=fg,
                         activebackground=hover, activeforeground=fg,
                         relief="flat", bd=0, highlightthickness=0,
                         font=FONT_UI, cursor="hand2", **kw)
        self._bg, self._hover = bg, hover
        self._fg = fg
        self._disabled_bg = PANEL2
        self.bind("<Enter>", self._on_enter)
        self.bind("<Leave>", self._on_leave)

    def _on_enter(self, _=None):
        if str(self["state"]) != "disabled":
            self.configure(bg=self._hover)

    def _on_leave(self, _=None):
        if str(self["state"]) != "disabled":
            self.configure(bg=self._bg)

    def set_enabled(self, enabled: bool) -> None:
        self.configure(state="normal" if enabled else "disabled",
                       bg=self._bg if enabled else self._disabled_bg,
                       fg=self._fg if enabled else DIM,
                       cursor="hand2" if enabled else "arrow")


class CountdownDialog(tk.Toplevel):
    """完成后动作倒计时。"""

    def __init__(self, master, action_text: str, seconds: int, note: str,
                 on_execute):
        super().__init__(master)
        self.on_execute = on_execute
        self.remaining = max(3, int(seconds))
        self.title("任务完成")
        self.configure(bg=PANEL)
        self.resizable(False, False)
        self.transient(master)
        self.attributes("-topmost", True)

        wrap = tk.Frame(self, bg=PANEL, padx=28, pady=22)
        wrap.pack(fill="both", expand=True)
        tk.Label(wrap, text="备份任务已完成", bg=PANEL, fg=TEXT,
                 font=FONT_TITLE).pack(anchor="w")
        self.num = tk.Label(wrap, text=str(self.remaining), bg=PANEL, fg=ACCENT,
                            font=("Microsoft YaHei UI", 34, "bold"))
        self.num.pack(pady=(10, 0))
        tk.Label(wrap, text=f"秒后自动{action_text}", bg=PANEL, fg=TEXT,
                 font=FONT_UI).pack()
        if note:
            tk.Label(wrap, text=note, bg=PANEL, fg=AMBER, font=FONT_SMALL,
                     wraplength=360, justify="left").pack(pady=(10, 0))
        row = tk.Frame(wrap, bg=PANEL)
        row.pack(pady=(18, 0), fill="x")
        FlatButton(row, f"立即{action_text}", self._execute, kind="primary").pack(side="right")
        FlatButton(row, "取消", self._cancel, kind="ghost").pack(side="right", padx=(0, 10))

        self._center(master)
        self._tick()

    def _center(self, master) -> None:
        self.update_idletasks()
        w, h = self.winfo_reqwidth(), self.winfo_reqheight()
        x = master.winfo_rootx() + (master.winfo_width() - w) // 2
        y = master.winfo_rooty() + (master.winfo_height() - h) // 3
        self.geometry(f"+{max(0, x)}+{max(0, y)}")

    def _tick(self) -> None:
        if not self.winfo_exists():
            return
        if self.remaining <= 0:
            self._execute()
            return
        self.num.configure(text=str(self.remaining))
        self.remaining -= 1
        self.after(1000, self._tick)

    def _execute(self) -> None:
        if not self.winfo_exists():
            return
        self.destroy()
        self.on_execute()

    def _cancel(self) -> None:
        self.destroy()
        self.on_execute = lambda: None
        master = self.master
        if isinstance(master, App):
            master.log("WARN", "已取消自动执行，请手动处理")
            master.set_status("已完成", GREEN)


class AdvancedDialog(tk.Toplevel):
    """高级设置：跳过目录、重传次数、空目录清理等。"""

    def __init__(self, master: "App"):
        super().__init__(master)
        self.app = master
        self.title("高级设置")
        self.configure(bg=PANEL)
        self.resizable(False, False)
        self.transient(master)
        self.grab_set()
        cfg = master.cfg

        wrap = tk.Frame(self, bg=PANEL, padx=24, pady=20)
        wrap.pack(fill="both", expand=True)
        tk.Label(wrap, text="高级设置", bg=PANEL, fg=TEXT, font=FONT_TITLE).pack(anchor="w")
        tk.Label(wrap, text="修改后立即生效并写入 settings.json", bg=PANEL, fg=MUTED,
                 font=FONT_SMALL).pack(anchor="w", pady=(2, 14))

        tk.Label(wrap, text="跳过的系统目录（每行一个，不区分大小写）", bg=PANEL, fg=TEXT,
                 font=FONT_UI).pack(anchor="w")
        self.skip_text = tk.Text(wrap, height=7, width=52, bg=PANEL2, fg=TEXT,
                                 insertbackground=TEXT, relief="flat", bd=0,
                                 font=FONT_MONO, padx=8, pady=6,
                                 highlightthickness=1, highlightbackground=BORDER,
                                 highlightcolor=ACCENT)
        self.skip_text.pack(fill="x", pady=(6, 14))
        self.skip_text.insert("1.0", "\n".join(cfg["skip_dirs"]))

        row = tk.Frame(wrap, bg=PANEL)
        row.pack(fill="x", pady=4)
        tk.Label(row, text="传输异常最多重传次数", bg=PANEL, fg=TEXT, font=FONT_UI).pack(side="left")
        self.retry_var = tk.StringVar(value=str(cfg["max_retry"]))
        tk.Spinbox(row, from_=1, to=10, textvariable=self.retry_var, width=4,
                   bg=PANEL2, fg=TEXT, buttonbackground=PANEL3, relief="flat",
                   insertbackground=TEXT, font=FONT_UI).pack(side="right")

        self.empty_var = tk.BooleanVar(value=bool(cfg["remove_empty_dirs"]))
        self.eject_var = tk.BooleanVar(value=bool(cfg.get("eject_after_finish", True)))
        self.dry_var = tk.BooleanVar(value=bool(cfg.get("power_dry_run", False)))
        for var, text in ((self.empty_var, "删除源文件后清理 SD 卡上的空目录"),
                          (self.eject_var, "任务结束前刷新缓存并尝试安全弹出目标设备"),
                          (self.dry_var, "演练模式：只记录休眠/关机，不真正执行（测试推荐）")):
            tk.Checkbutton(wrap, text=text, variable=var, bg=PANEL, fg=TEXT,
                           selectcolor=PANEL2, activebackground=PANEL,
                           activeforeground=TEXT, font=FONT_UI, bd=0,
                           highlightthickness=0, anchor="w").pack(fill="x", pady=(8, 0))

        tk.Label(wrap, text=f"拍摄文件后缀清单：{cfgmod.EXTENSIONS_PATH.name}"
                            "（可用记事本增删品牌后缀）", bg=PANEL, fg=MUTED,
                 font=FONT_SMALL).pack(anchor="w", pady=(14, 4))
        FlatButton(wrap, "打开后缀清单文件", self._open_ext, kind="ghost").pack(anchor="w")

        btns = tk.Frame(wrap, bg=PANEL)
        btns.pack(fill="x", pady=(18, 0))
        FlatButton(btns, "保存", self._save, kind="primary").pack(side="right")
        FlatButton(btns, "取消", self.destroy, kind="ghost").pack(side="right", padx=(0, 10))
        self._center(master)

    def _center(self, master) -> None:
        self.update_idletasks()
        w, h = self.winfo_reqwidth(), self.winfo_reqheight()
        x = master.winfo_rootx() + (master.winfo_width() - w) // 2
        y = master.winfo_rooty() + (master.winfo_height() - h) // 3
        self.geometry(f"+{max(0, x)}+{max(0, y)}")

    def _open_ext(self) -> None:
        try:
            cfgmod.load_extensions()
            os.startfile(str(cfgmod.EXTENSIONS_PATH))
        except Exception as exc:
            messagebox.showerror("打开失败", str(exc), parent=self)

    def _save(self) -> None:
        dirs = [line.strip() for line in self.skip_text.get("1.0", "end").splitlines()
                if line.strip()]
        self.app.cfg["skip_dirs"] = dirs
        try:
            self.app.cfg["max_retry"] = max(1, min(10, int(self.retry_var.get())))
        except ValueError:
            self.app.cfg["max_retry"] = 3
        self.app.cfg["remove_empty_dirs"] = bool(self.empty_var.get())
        self.app.cfg["eject_after_finish"] = bool(self.eject_var.get())
        self.app.cfg["power_dry_run"] = bool(self.dry_var.get())
        self.app.cfg.save()
        self.app._refresh_dry_badge()
        self.app.log("INFO", "高级设置已保存")
        self.destroy()


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title(f"{cfgmod.APP_NAME}  v{cfgmod.VERSION}")
        self.minsize(1040, 720)
        self.configure(bg=BG)
        self.cfg = cfgmod.AppConfig.load()
        self.cfgmod_ext = cfgmod.load_extensions()
        cfgmod.ensure_dirs()

        self.queue: queue.Queue = queue.Queue()
        self.devices: list[devmod.Device] = []
        self.engine: engmod.BackupEngine | None = None
        self.worker: threading.Thread | None = None
        self.running = False
        self.start_time = 0.0
        self.target_dir = ""
        self.report_path = ""
        self.log_path = ""
        self._in_dialog = False
        self._last_device_sig: list[tuple] = []
        self._confirm_open = False
        self._countdown: CountdownDialog | None = None
        self._progress_data: dict = {}
        self._closing = False

        self._init_style()
        self._build()
        self._place_window()
        self.after(60, self._dark_titlebar)
        self._set_icon()
        self._load_settings_to_ui()
        self._refresh_dry_badge()
        self._start_watcher()
        self.set_status("空闲", DIM)
        self.log("INFO", f"{cfgmod.APP_NAME} v{cfgmod.VERSION} 已启动，等待插入设备…")
        self.after(120, self._poll)
        self.after(500, self._tick_clock)
        self.after(600, self._warn_unfinished)
        self.protocol("WM_DELETE_WINDOW", self._on_close)

    def _place_window(self) -> None:
        """按屏幕可用区域确定窗口尺寸并居中（避免底部按钮被裁掉）。"""
        sw, sh = self.winfo_screenwidth(), self.winfo_screenheight()
        width = min(1240, sw - 120)
        height = min(860, sh - 150)
        x = max(0, (sw - width) // 2)
        y = max(0, (sh - height) // 2 - 24)
        self.geometry(f"{width}x{height}+{x}+{y}")

    # ------------------------------------------------------------ 窗口外观
    def _dark_titlebar(self) -> None:
        """让标题栏跟随深色主题（Win10 1809+）。"""
        try:
            hwnd = ctypes.windll.user32.GetParent(self.winfo_id()) or self.winfo_id()
            value = ctypes.c_int(1)
            for attr in (20, 19):
                if ctypes.windll.dwmapi.DwmSetWindowAttribute(
                        hwnd, attr, ctypes.byref(value), ctypes.sizeof(value)) == 0:
                    break
        except Exception:
            pass

    def _set_icon(self) -> None:
        """优先用打包内置资源，其次程序目录。"""
        candidates = []
        bundle = getattr(sys, "_MEIPASS", None)
        if bundle:
            candidates.append(Path(bundle) / "assets" / "app.ico")
        candidates.append(cfgmod.APP_DIR / "assets" / "app.ico")
        for icon in candidates:
            try:
                if icon.exists():
                    self.iconbitmap(default=str(icon))
                    return
            except Exception:
                continue

    # ------------------------------------------------------------ 样式
    def _init_style(self) -> None:
        style = ttk.Style(self)
        style.theme_use("clam")
        style.configure("TCombobox", fieldbackground=PANEL2, background=PANEL2,
                        foreground=TEXT, arrowcolor=MUTED, bordercolor=BORDER,
                        lightcolor=BORDER, darkcolor=BORDER, padding=5,
                        selectbackground=PANEL2, selectforeground=TEXT)
        style.map("TCombobox",
                  fieldbackground=[("readonly", PANEL2), ("disabled", PANEL)],
                  foreground=[("readonly", TEXT), ("disabled", DIM)],
                  background=[("readonly", PANEL2), ("active", PANEL3)],
                  bordercolor=[("focus", ACCENT)],
                  arrowcolor=[("active", TEXT)])
        style.configure("Accent.Horizontal.TProgressbar", troughcolor="#2b3340",
                        background=ACCENT, bordercolor=PANEL2, lightcolor=ACCENT,
                        darkcolor=ACCENT, thickness=10)
        style.configure("Thin.Horizontal.TProgressbar", troughcolor="#2b3340",
                        background="#5b97ff", bordercolor=PANEL2, lightcolor="#5b97ff",
                        darkcolor="#5b97ff", thickness=6)
        style.configure("Vertical.TScrollbar", background=PANEL3, troughcolor=PANEL,
                        bordercolor=PANEL, arrowcolor=MUTED, width=10)
        style.map("Vertical.TScrollbar", background=[("active", "#3a4553")])
        self.option_add("*TCombobox*Listbox.background", PANEL2)
        self.option_add("*TCombobox*Listbox.foreground", TEXT)
        self.option_add("*TCombobox*Listbox.selectBackground", ACCENT)
        self.option_add("*TCombobox*Listbox.selectForeground", "#ffffff")
        self.option_add("*TCombobox*Listbox.font", FONT_UI)

    # ------------------------------------------------------------ 布局
    def _build(self) -> None:
        self._build_bottom()          # 先占住底部按钮栏，避免被内容挤掉
        root = tk.Frame(self, bg=BG)
        root.pack(fill="both", expand=True, padx=16, pady=(12, 0))
        root.columnconfigure(0, weight=1)
        root.rowconfigure(4, weight=1)

        self._build_header(root)
        self._build_devices(root)
        self._build_options(root)
        self._build_progress(root)
        self._build_log(root)

    def _build_header(self, parent) -> None:
        head = tk.Frame(parent, bg=BG)
        head.grid(row=0, column=0, sticky="ew", pady=(0, 10))
        tk.Label(head, text="SD 卡自动备份与校验", bg=BG, fg=TEXT,
                 font=("Microsoft YaHei UI", 16, "bold")).pack(side="left")

        right = tk.Frame(head, bg=BG)
        right.pack(side="right")
        self.dry_lbl = tk.Label(right, text="", bg="#3a311c", fg=AMBER,
                                font=FONT_SMALL, padx=8, pady=2)
        self.status_dot = tk.Label(right, text="●", bg=BG, fg=DIM, font=("Segoe UI", 12))
        self.status_dot.pack(side="left", padx=(0, 4))
        self.status_lbl = tk.Label(right, text="空闲", bg=BG, fg=MUTED, font=FONT_BOLD)
        self.status_lbl.pack(side="left")
        self.elapsed_lbl = tk.Label(right, text="", bg=BG, fg=DIM, font=FONT_SMALL)
        self.elapsed_lbl.pack(side="left", padx=(12, 0))

    def _build_devices(self, parent) -> None:
        row = tk.Frame(parent, bg=BG)
        row.grid(row=1, column=0, sticky="ew", pady=(0, 10))
        row.columnconfigure(0, weight=1, uniform="dev")
        row.columnconfigure(1, weight=1, uniform="dev")

        self.src_card, src = self._card(row)
        self.src_card.grid(row=0, column=0, sticky="nsew", padx=(0, 5))
        self.src_combo, self.src_info, self.src_tag = self._device_card_body(src, "源设备")
        self.src_combo.bind("<<ComboboxSelected>>", lambda _e: self._on_device_select())

        self.dst_card, dst = self._card(row)
        self.dst_card.grid(row=0, column=1, sticky="nsew", padx=(5, 0))
        self.dst_combo, self.dst_info, self.dst_tag = self._device_card_body(dst, "目标设备")
        self.dst_combo.bind("<<ComboboxSelected>>", lambda _e: self._on_device_select())

    def _card(self, parent) -> tuple[tk.Frame, tk.Frame]:
        card = tk.Frame(parent, bg=PANEL, highlightbackground=BORDER, highlightthickness=1)
        inner = tk.Frame(card, bg=PANEL)
        inner.pack(fill="both", expand=True, padx=14, pady=10)
        return card, inner

    def _device_card_body(self, parent, title: str):
        head = tk.Frame(parent, bg=PANEL)
        head.pack(fill="x")
        tk.Label(head, text=title, bg=PANEL, fg=MUTED, font=FONT_SMALL).pack(side="left")
        tag = tk.Label(head, text="未检测", bg=PANEL2, fg=DIM, font=FONT_SMALL,
                       padx=8, pady=1)
        tag.pack(side="right")

        row = tk.Frame(parent, bg=PANEL)
        row.pack(fill="x", pady=(6, 4))
        combo = ttk.Combobox(row, state="readonly", font=FONT_UI,
                             values=["未检测到设备"], height=12)
        combo.pack(side="left", fill="x", expand=True)
        FlatButton(row, "刷新", lambda: self.refresh_devices(manual=True),
                   kind="ghost", padx=10, pady=4).pack(side="left", padx=(8, 0))
        combo.bind("<<ComboboxDropdown>>",
                   lambda _e, c=combo: self._style_popup(c), add="+")

        info = tk.Label(parent, text="未插入设备", bg=PANEL, fg=DIM,
                        font=FONT_SMALL, anchor="w")
        info.pack(fill="x")
        return combo, info, tag

    def _style_popup(self, combo) -> None:
        """给 Combobox 下拉列表套上深色皮肤。"""
        try:
            popdown = self.tk.call("ttk::combobox::PopdownWindow", str(combo))
            self.tk.call(f"{popdown}.f.l", "configure",
                         "-background", PANEL2, "-foreground", TEXT,
                         "-selectbackground", ACCENT, "-selectforeground", "#ffffff",
                         "-font", FONT_UI, "-borderwidth", "0",
                         "-highlightthickness", "0")
        except Exception:
            pass

    # ------------------------------------------------------------ 选项条
    def _group(self, parent, text: str) -> None:
        tk.Label(parent, text=text, bg=PANEL, fg=MUTED, font=FONT_SMALL).pack(
            side="left", padx=(0, 8))

    def _sep(self, parent) -> None:
        tk.Frame(parent, bg=BORDER, width=1, height=18).pack(side="left", padx=14)

    def _check(self, parent, text: str, var: tk.BooleanVar, command=None):
        return tk.Checkbutton(parent, text=text, variable=var, bg=PANEL, fg=TEXT,
                              selectcolor=PANEL2, activebackground=PANEL,
                              activeforeground=TEXT, font=FONT_UI, bd=0,
                              highlightthickness=0, cursor="hand2", command=command)

    def _build_options(self, parent) -> None:
        card, inner = self._card(parent)
        card.grid(row=2, column=0, sticky="ew", pady=(0, 10))

        row1 = tk.Frame(inner, bg=PANEL)
        row1.pack(fill="x")
        self._group(row1, "复制模式")
        self.mode_var = tk.StringVar(value="full")
        for value, text in (("full", "全量复制"), ("camera", "仅拍摄文件")):
            tk.Radiobutton(row1, text=text, value=value, variable=self.mode_var,
                           bg=PANEL, fg=TEXT, selectcolor=ACCENT, activebackground=PANEL,
                           activeforeground=TEXT, font=FONT_UI, bd=0,
                           highlightthickness=0, cursor="hand2",
                           command=self._on_mode_change).pack(side="left", padx=(0, 12))
        self._sep(row1)
        self._group(row1, "校验")
        tk.Label(row1, text="大小+时间", bg=PANEL, fg=TEXT, font=FONT_UI).pack(side="left")
        self.hash_var = tk.BooleanVar(value=True)
        self._check(row1, "SHA-256", self.hash_var).pack(side="left", padx=(12, 0))
        self._sep(row1)
        self._group(row1, "完成后")
        self.action_combo = ttk.Combobox(row1, state="readonly", width=7, font=FONT_UI,
                                         values=["不操作", "休眠", "关机"])
        self.action_combo.pack(side="left")
        tk.Label(row1, text="倒计时", bg=PANEL, fg=MUTED, font=FONT_SMALL).pack(
            side="left", padx=(12, 6))
        self.countdown_var = tk.StringVar(value="60")
        tk.Spinbox(row1, from_=5, to=600, increment=5, textvariable=self.countdown_var,
                   width=4, bg=PANEL2, fg=TEXT, buttonbackground=PANEL3, relief="flat",
                   insertbackground=TEXT, font=FONT_UI, highlightthickness=0).pack(side="left")
        tk.Label(row1, text="秒", bg=PANEL, fg=MUTED, font=FONT_SMALL).pack(
            side="left", padx=(4, 0))

        row2 = tk.Frame(inner, bg=PANEL)
        row2.pack(fill="x", pady=(10, 0))
        self._group(row2, "无人值守")
        self.unattended_var = tk.BooleanVar(value=False)
        self._check(row2, "启用", self.unattended_var,
                    command=self._on_unattended_change).pack(side="left")
        self.ua_action_combo = ttk.Combobox(row2, state="readonly", width=7, font=FONT_UI,
                                            values=["不操作", "休眠", "关机"])
        self.ua_action_combo.pack(side="left", padx=(8, 0))
        self._sep(row2)
        self.delete_var = tk.BooleanVar(value=True)
        self._check(row2, "校验后删除源文件", self.delete_var,
                    command=self._on_delete_change).pack(side="left")
        self.policy_combo = ttk.Combobox(row2, state="readonly", width=15, font=FONT_UI,
                                         values=["全部成功才删除", "按文件删除成功项"])
        self.policy_combo.pack(side="left", padx=(8, 0))
        self._sep(row2)
        self.run_on_fail_var = tk.BooleanVar(value=True)
        self._check(row2, "失败仍执行", self.run_on_fail_var).pack(side="left")
        FlatButton(row2, "高级…", self._open_advanced, kind="ghost",
                   padx=10, pady=1).pack(side="right")

    # ------------------------------------------------------------ 进度
    def _build_progress(self, parent) -> None:
        card, inner = self._card(parent)
        card.grid(row=3, column=0, sticky="ew", pady=(0, 10))

        top = tk.Frame(inner, bg=PANEL)
        top.pack(fill="x")
        tk.Label(top, text="阶段", bg=PANEL, fg=MUTED, font=FONT_SMALL).pack(side="left")
        self.phase_lbl = tk.Label(top, text="待开始", bg=PANEL, fg=TEXT, font=FONT_BOLD)
        self.phase_lbl.pack(side="left", padx=(8, 0))
        self.pct_lbl = tk.Label(top, text="0%", bg=PANEL, fg=ACCENT,
                                font=("Microsoft YaHei UI", 11, "bold"))
        self.pct_lbl.pack(side="right")

        self.overall_bar = ttk.Progressbar(inner, style="Accent.Horizontal.TProgressbar",
                                           maximum=100, value=0)
        self.overall_bar.pack(fill="x", pady=(8, 4))
        self.stats_lbl = tk.Label(inner, text="等待开始", bg=PANEL, fg=MUTED,
                                  font=FONT_SMALL, anchor="w")
        self.stats_lbl.pack(fill="x")
        self.file_bar = ttk.Progressbar(inner, style="Thin.Horizontal.TProgressbar",
                                        maximum=100, value=0)
        self.file_bar.pack(fill="x", pady=(8, 4))
        self.current_lbl = tk.Label(inner, text="当前文件：—", bg=PANEL, fg=TEXT,
                                    font=FONT_SMALL, anchor="w")
        self.current_lbl.pack(fill="x")

        meta = tk.Frame(inner, bg=PANEL)
        meta.pack(fill="x", pady=(4, 0))
        self.scan_lbl = tk.Label(meta, text="", bg=PANEL, fg=DIM,
                                 font=FONT_SMALL, anchor="w")
        self.scan_lbl.pack(side="left")
        self.target_lbl = tk.Label(meta, text="", bg=PANEL, fg=DIM,
                                   font=FONT_SMALL, anchor="e")
        self.target_lbl.pack(side="right")

    # ------------------------------------------------------------ 日志
    def _build_log(self, parent) -> None:
        card, inner = self._card(parent)
        card.grid(row=4, column=0, sticky="nsew")
        head = tk.Frame(inner, bg=PANEL)
        head.pack(fill="x", pady=(0, 8))
        tk.Label(head, text="运行日志", bg=PANEL, fg=TEXT, font=FONT_BOLD).pack(side="left")
        FlatButton(head, "清空", self._clear_log, kind="ghost", padx=8, pady=1).pack(side="right")

        wrap = tk.Frame(inner, bg=PANEL)
        wrap.pack(fill="both", expand=True)
        self.log_text = tk.Text(wrap, bg="#101318", fg=TEXT, insertbackground=TEXT,
                                relief="flat", bd=0, wrap="none", font=FONT_MONO,
                                padx=10, pady=8, height=8, highlightthickness=1,
                                highlightbackground=BORDER, selectbackground="#2f4a72")
        scroll = ttk.Scrollbar(wrap, orient="vertical", command=self.log_text.yview,
                               style="Vertical.TScrollbar")
        self.log_text.configure(yscrollcommand=scroll.set)
        scroll.pack(side="right", fill="y")
        self.log_text.pack(side="left", fill="both", expand=True)
        for level, color in LEVEL_COLOR.items():
            self.log_text.tag_configure(level, foreground=color)
        self.log_text.tag_configure("muted", foreground=DIM)
        self.log_text.tag_configure("title", foreground="#79b8ff")
        self.log_text.configure(state="disabled")

    def _build_bottom(self) -> None:
        bar = tk.Frame(self, bg=BG)
        bar.pack(side="bottom", fill="x", padx=18, pady=(10, 14))
        left = tk.Frame(bar, bg=BG)
        left.pack(side="left")
        self.start_btn = FlatButton(left, "开始备份", self._on_start, kind="primary")
        self.start_btn.pack(side="left")
        self.pause_btn = FlatButton(left, "暂停", self._on_pause)
        self.pause_btn.pack(side="left", padx=(10, 0))
        self.cancel_btn = FlatButton(left, "取消", self._on_cancel, kind="danger")
        self.cancel_btn.pack(side="left", padx=(10, 0))
        self.pause_btn.set_enabled(False)
        self.cancel_btn.set_enabled(False)

        right = tk.Frame(bar, bg=BG)
        right.pack(side="right")
        self.open_target_btn = FlatButton(right, "打开备份目录", self._open_target, kind="ghost")
        self.open_target_btn.pack(side="right")
        self.open_log_btn = FlatButton(right, "打开日志", self._open_log_folder, kind="ghost")
        self.open_log_btn.pack(side="right", padx=(0, 10))
        self.hint_lbl = tk.Label(right, text="", bg=BG, fg=DIM, font=FONT_SMALL)
        self.hint_lbl.pack(side="right", padx=(0, 14))

    # ------------------------------------------------------------ 设置读写
    def _refresh_dry_badge(self) -> None:
        """演练模式时在标题栏右侧给出显式标识。"""
        if powermod.dry_run_enabled(self.cfg):
            self.dry_lbl.configure(text="演练模式")
            self.dry_lbl.pack(side="left", padx=(0, 12), before=self.status_dot)
        else:
            self.dry_lbl.pack_forget()

    def _load_settings_to_ui(self) -> None:
        cfg = self.cfg
        self.mode_var.set(cfg["copy_mode"])
        self.hash_var.set(bool(cfg["verify_hash"]))
        self.action_combo.set(ACTION_LABEL.get(cfg["finish_action"], "不操作"))
        self.countdown_var.set(str(cfg["countdown_seconds"]))
        self.unattended_var.set(bool(cfg["unattended"]))
        self.ua_action_combo.set(ACTION_LABEL.get(cfg["unattended_action"], "休眠"))
        self.delete_var.set(bool(cfg["delete_source"]))
        self.policy_combo.set("按文件删除成功项" if cfg["delete_policy"] == "per_file"
                              else "全部成功才删除")
        self.run_on_fail_var.set(bool(cfg["run_action_on_failure"]))
        self._on_unattended_change()
        self._on_delete_change()

    def _collect_settings(self) -> None:
        cfg = self.cfg
        cfg["copy_mode"] = self.mode_var.get()
        cfg["verify_hash"] = bool(self.hash_var.get())
        cfg["finish_action"] = ACTION_VALUE.get(self.action_combo.get(), "none")
        try:
            cfg["countdown_seconds"] = max(5, int(self.countdown_var.get()))
        except ValueError:
            cfg["countdown_seconds"] = 60
        cfg["unattended"] = bool(self.unattended_var.get())
        cfg["unattended_action"] = ACTION_VALUE.get(self.ua_action_combo.get(), "hibernate")
        cfg["delete_source"] = bool(self.delete_var.get())
        cfg["delete_policy"] = "per_file" if self.policy_combo.get() == "按文件删除成功项" \
            else "all_or_nothing"
        cfg["run_action_on_failure"] = bool(self.run_on_fail_var.get())
        cfg.save()

    def _on_unattended_change(self) -> None:
        enabled = not self.unattended_var.get()
        self.action_combo.configure(state="readonly" if enabled else "disabled")
        self._save_quietly()

    def _on_delete_change(self) -> None:
        enabled = self.delete_var.get()
        self.policy_combo.configure(state="readonly" if enabled else "disabled")
        self._save_quietly()

    def _on_mode_change(self) -> None:
        mode = self.mode_var.get()
        hint = "复制 SD 卡全部非系统文件" if mode == "full" else "仅复制相机拍摄文件（后缀清单可配置）"
        self.hint_lbl.configure(text=hint)
        self._save_quietly()

    def _save_quietly(self) -> None:
        if not self.running:
            try:
                self._collect_settings()
            except Exception:
                pass

    # ------------------------------------------------------------ 设备
    def _start_watcher(self) -> None:
        self.watcher = devmod.DeviceWatcher(
            lambda devs: self.queue.put(("devices", devs)), interval=0.9)
        self.watcher.start()

    def refresh_devices(self, manual: bool = False) -> None:
        devs = devmod.list_devices()
        self._apply_devices(devs)
        if manual:
            self.log("INFO", f"已刷新设备列表（{len(devs)} 个可用设备）")

    def _apply_devices(self, devs: list[devmod.Device]) -> None:
        old_map = {d.root: d for d in self.devices}
        new_map = {d.root: d for d in devs}
        self._last_device_sig = [d.signature() for d in devs]
        self.devices = devs

        for root, dev in new_map.items():
            if root not in old_map:
                self.log("OK", f"检测到设备：{dev.title()}")
        for root, dev in old_map.items():
            if root not in new_map:
                self.log("WARN", f"设备已移除：{dev.title()}")

        for combo in (self.src_combo, self.dst_combo):
            current = combo.get()
            values = [d.title() for d in devs]
            combo.configure(values=values or ["未检测到设备"])
            if current in values:
                combo.set(current)
            elif values:
                combo.current(0)
            else:
                combo.set("未检测到设备")

        self._restore_selection()
        self._on_device_select()

    def _restore_selection(self) -> None:
        last_src = self.cfg["last_source"] or ""
        last_dst = self.cfg["last_target"] or ""
        for combo, last in ((self.src_combo, last_src), (self.dst_combo, last_dst)):
            for idx, dev in enumerate(self.devices):
                if dev.root.upper() == last.upper():
                    combo.current(idx)
                    break
        src = self._selected(self.src_combo)
        if src is None:
            sd = [d for d in self.devices if d.bus in (12, 13) or d.drive_type == 2]
            if sd:
                self.src_combo.set(sd[0].title())
                src = sd[0]
        dst = self._selected(self.dst_combo)
        if src and (dst is None or devmod.same_volume(src, dst)):
            others = [d for d in self.devices if not devmod.same_volume(d, src)]
            if others:
                best = max(others, key=lambda d: d.free)
                self.dst_combo.set(best.title())

    def _selected(self, combo) -> devmod.Device | None:
        title = combo.get()
        for dev in self.devices:
            if dev.title() == title:
                return dev
        return None

    def _on_device_select(self) -> None:
        src = self._selected(self.src_combo)
        dst = self._selected(self.dst_combo)
        for dev, info, tag in ((src, self.src_info, self.src_tag),
                               (dst, self.dst_info, self.dst_tag)):
            if dev is None:
                info.configure(text="未插入设备", fg=DIM)
                tag.configure(text="未检测", bg=PANEL2, fg=DIM)
                continue
            info.configure(text=f"{dev.fs or '未知'} · 总 {devmod.fmt_size(dev.total)}"
                                f" · 可用 {devmod.fmt_size(dev.free)}", fg=MUTED)
            color = GREEN if dev.bus in (12, 13) else (ACCENT if dev.bus == 7 else MUTED)
            tag.configure(text=dev.kind, bg=PANEL2, fg=color)
            color = GREEN if dev.bus in (12, 13) else (ACCENT if dev.bus == 7 else MUTED)
            tag.configure(text=dev.kind, bg=PANEL2, fg=color)

        if not self.running and src and dst:
            if devmod.same_volume(src, dst):
                self.hint_lbl.configure(text="⚠ 源与目标不能是同一分区", fg=RED)
            else:
                self.hint_lbl.configure(
                    text=f"目标可用 {devmod.fmt_size(dst.free)}", fg=DIM)
            self.cfg["last_source"] = src.root
            self.cfg["last_target"] = dst.root

    # ------------------------------------------------------------ 任务控制
    def _on_start(self) -> None:
        if self.running:
            return
        self._collect_settings()
        src = self._selected(self.src_combo)
        dst = self._selected(self.dst_combo)
        if src is None or dst is None:
            messagebox.showwarning("请选择设备", "请先选择源设备与目标设备。", parent=self)
            return
        if devmod.same_volume(src, dst):
            messagebox.showerror("非法组合", "源与目标是同一个分区，请重新选择。", parent=self)
            return

        self.target_dir = ""
        self.report_path = ""
        self.cfgmod_ext = cfgmod.load_extensions()   # 每次开始时重读后缀清单
        self.log("INFO", "─" * 40)
        self.engine = engmod.BackupEngine(
            self.cfg, self.cfgmod_ext, src.root, dst.root,
            lambda kind, **kw: self.queue.put(("engine", {"kind": kind, **kw})))
        self.worker = threading.Thread(target=self._run_engine, daemon=True)
        self._set_running(True)
        self.start_time = time.time()
        self.set_status("运行中", ACCENT)
        self.phase_lbl.configure(text="准备")
        self.overall_bar["value"] = 0
        self.file_bar["value"] = 0
        self.pct_lbl.configure(text="0%")
        self.scan_lbl.configure(text="扫描中…", fg=DIM)
        self.target_lbl.configure(text="")
        self.worker.start()

    def _run_engine(self) -> None:
        try:
            self.engine.run()
        except Exception as exc:  # 兜底
            self.queue.put(("engine", {"kind": "log", "level": "ERROR",
                                       "msg": f"引擎异常：{exc}", "line": ""}))
            self.queue.put(("engine", {"kind": "finished", "summary": {
                "cancelled": True, "error": str(exc), "exists_failure": False,
                "target_dir": "", "report_path": ""}}))

    def _on_pause(self) -> None:
        if not self.running or self.engine is None:
            return
        if self.engine.device_lost:
            self.engine.signal_retry()
            self.log("INFO", "正在重试设备连接…")
            return
        paused = self.engine.toggle_pause()
        self.pause_btn.configure(text="继续" if paused else "暂停")
        self.set_status("已暂停" if paused else "运行中", AMBER if paused else ACCENT)

    def _on_cancel(self) -> None:
        if not self.running or self.engine is None:
            return
        if not messagebox.askyesno("取消任务", "确定要取消当前备份任务吗？\n已复制的文件会保留。",
                                   parent=self):
            return
        self.engine.cancel()
        self.set_status("正在取消…", AMBER)
        self.log("WARN", "用户请求取消任务")

    def _set_running(self, running: bool) -> None:
        self.running = running
        self.start_btn.set_enabled(not running)
        self.pause_btn.set_enabled(running)
        self.cancel_btn.set_enabled(running)
        self.pause_btn.configure(text="暂停")
        state = "disabled" if running else "readonly"
        self.src_combo.configure(state=state)
        self.dst_combo.configure(state=state)
        if not running:
            self.elapsed_lbl.configure(text="")

    # ------------------------------------------------------------ 事件循环
    def _poll(self) -> None:
        if self._closing:
            return
        try:
            while True:
                kind, payload = self.queue.get_nowait()
                if kind == "devices":
                    self._apply_devices(payload)
                elif kind == "engine":
                    self._handle_engine_event(payload)
        except queue.Empty:
            pass
        except Exception as exc:  # 界面异常不应退出
            self.log("ERROR", f"界面事件处理异常：{exc}")
        if not self._closing:
            self.after(80, self._poll)

    def _handle_engine_event(self, ev: dict) -> None:
        kind = ev.get("kind")
        if kind == "log":
            self.log(ev.get("level", "INFO"), ev.get("msg", ""), ev.get("line"))
        elif kind == "phase":
            self.phase_lbl.configure(text=ev.get("value", ""))
            self.file_bar["value"] = 0
        elif kind == "scan":
            self._on_scan(ev)
        elif kind == "target":
            self.target_dir = ev.get("path", "")
            self.target_lbl.configure(text=ellipsize(self.target_dir, 62))
        elif kind == "progress":
            self._on_progress(ev)
        elif kind == "retry":
            self.set_status(f"重传 {ev.get('attempt')}/{ev.get('max_retry')}", AMBER)
        elif kind == "state":
            if ev.get("value") == "paused":
                self.set_status("已暂停", AMBER)
                self.pause_btn.configure(text="继续")
            else:
                self.set_status("运行中", ACCENT)
                self.pause_btn.configure(text="暂停")
        elif kind == "device_lost":
            missing = "与".join(ev.get("missing") or ["设备"])
            self.set_status(f"{missing}断开", RED)
            self.pause_btn.configure(text="继续（重试）")
            self.pause_btn.set_enabled(True)
            self.log("ERROR", f"⚠ {missing}已断开，任务已暂停并停止写入；恢复后点击『继续（重试）』")
        elif kind == "device_lost_cleared":
            self.set_status("运行中", ACCENT)
            self.pause_btn.configure(text="暂停")
        elif kind == "confirm":
            self._on_confirm(ev)
        elif kind == "report":
            self.report_path = ev.get("path", "")
        elif kind == "finished":
            self._on_finished(ev.get("summary") or {})

    def _on_scan(self, ev: dict) -> None:
        by_ext = ev.get("by_ext") or {}
        parts = " · ".join(f"{k.lstrip('.').upper() or '无后缀'} {v}"
                           for k, v in sorted(by_ext.items(), key=lambda kv: -kv[1]))
        head = "拍摄文件" if ev.get("mode") == "camera" else "文件"
        self.scan_lbl.configure(
            text=f"{head} {ev.get('count', 0)} 个 · {engmod.human_size(ev.get('bytes', 0))}"
                 + (f" · {parts}" if parts else ""), fg=MUTED)

    def _on_progress(self, ev: dict) -> None:
        self._progress_data = ev
        pct = ev.get("percent", 0)
        self.overall_bar["value"] = pct
        self.file_bar["value"] = ev.get("file_percent", 0)
        self.pct_lbl.configure(text=f"{pct:.1f}%")
        files = f"文件 {ev.get('files_done', 0)}/{ev.get('files_total', 0)}"
        size = f"{ev.get('done_text', '0 B')} / {ev.get('total_text', '0 B')}"
        speed = devmod.fmt_size(ev.get("speed", 0)) + "/s"
        eta = engmod.human_time(ev.get("eta"))
        self.stats_lbl.configure(text=f"{files} · {size} · {speed} · 剩余 {eta}")
        if ev.get("phase") == "复制":
            cur = f"当前文件：{ellipsize(ev.get('current', '—'))}   " \
                  f"({ev.get('file_done_text', '')}/{ev.get('file_total_text', '')})"
        else:
            cur = f"当前文件：{ellipsize(ev.get('current', '—'))}"
        self.current_lbl.configure(text=cur)

    def _on_confirm(self, ev: dict) -> None:
        if self._confirm_open:
            return
        self._confirm_open = True
        by_ext = ev.get("by_ext") or {}
        parts = " · ".join(f"{k.lstrip('.').upper() or '无后缀'} {v}"
                           for k, v in sorted(by_ext.items(), key=lambda kv: -kv[1]))
        lines = [
            f"源设备：{self.src_combo.get()}",
            f"目标设备：{self.dst_combo.get()}",
            f"备份目录：{ev.get('target_dir', '')}",
            "",
            f"待复制：{ev.get('count', 0)} 个文件，共 {engmod.human_size(ev.get('bytes', 0))}",
            f"后缀明细：{parts or '—'}",
            f"目标可用空间：{engmod.human_size(ev.get('free', 0))}",
            "",
            ("校验通过后将删除 SD 卡中对应的源文件！" if ev.get("delete_source")
             else "不会删除 SD 卡内容（仅备份）。"),
        ]
        ok = messagebox.askokcancel("确认开始备份", "\n".join(lines), parent=self,
                                    icon="warning")
        self._confirm_open = False
        if self.engine:
            self.engine.confirm(ok)
        if not ok:
            self.set_status("正在取消…", AMBER)

    def _on_finished(self, summary: dict) -> None:
        self._set_running(False)
        self.file_bar["value"] = 0
        if summary.get("target_dir"):
            self.target_dir = summary["target_dir"]
        if summary.get("report_path"):
            self.report_path = summary["report_path"]

        cancelled = summary.get("cancelled")
        if cancelled:
            self.phase_lbl.configure(text="已取消")
            self.set_status("已取消", AMBER)
            self.overall_bar["value"] = 0
            if summary.get("error"):
                self.stats_lbl.configure(text=f"已中止：{summary['error']}")
                messagebox.showerror("任务中止", summary["error"], parent=self)
            return

        ok = summary.get("files_ok", 0)
        fail = summary.get("transfer_failed", 0)
        src_err = summary.get("source_errors", 0)
        self.overall_bar["value"] = 100
        self.pct_lbl.configure(text="100%")
        self.phase_lbl.configure(text="已完成")
        self.stats_lbl.configure(
            text=f"成功 {ok} 个 · 传输失败 {fail} 个 · 源文件异常 {src_err} 个 · "
                 f"删除 {summary.get('deleted', 0)} 个 · 耗时 "
                 f"{engmod.human_time(summary.get('duration', 0))}")
        self.set_status("已完成" if not summary.get("exists_failure") else "完成（有异常）",
                        GREEN if not summary.get("exists_failure") else AMBER)
        if summary.get("exists_failure") and summary.get("report_path"):
            messagebox.showwarning(
                "存在未成功文件",
                f"已生成报错文件：\n{summary['report_path']}\n\n"
                f"传输失败 {fail} 个，源文件异常 {src_err} 个。\n"
                f"未成功文件的 SD 卡副本已保留。", parent=self)
        self._maybe_power_action(summary)

    def _maybe_power_action(self, summary: dict) -> None:
        action = powermod.resolve_action(self.cfg)
        allowed, reason = powermod.should_execute(action, summary, self.cfg)
        if not allowed:
            if action != "none":
                self.log("WARN", reason)
            return
        text = powermod.ACTION_TEXT.get(action, action)
        note = "存在失败项，详见报错文件" if summary.get("exists_failure") else ""
        if self.cfg["unattended"]:
            self.log("INFO", f"无人值守模式：任务结束，执行{text}")
            self._execute_action(action)
            return
        self.log("INFO", f"{self.cfg['countdown_seconds']} 秒后自动{text}，可取消")
        self._countdown = CountdownDialog(self, text, int(self.cfg["countdown_seconds"]),
                                          note, lambda: self._execute_action(action))

    def _execute_action(self, action: str) -> None:
        dry = powermod.dry_run_enabled(self.cfg)
        self.log("INFO", f"电源动作：{powermod.ACTION_TEXT.get(action, action)}"
                         f"{'（演练模式）' if dry else ''}，缓存已刷新")
        self.update_idletasks()
        result = powermod.execute(action, dry_run=dry)
        if result.dry_run:
            self.log("WARN", result.message)
            return
        self.log("INFO" if result.executed else "ERROR", result.message)
        if not result.executed:
            messagebox.showerror("电源操作失败", result.message, parent=self)

    # ------------------------------------------------------------ 辅助
    def set_status(self, text: str, color: str = MUTED) -> None:
        self.status_lbl.configure(text=text, fg=color)
        self.status_dot.configure(fg=color)

    def log(self, level: str, message: str, line: str | None = None) -> None:
        if self._closing:
            return
        stamp = time.strftime("%H:%M:%S")
        text = line or f"[{stamp}] [{level}] {message}"
        self.log_text.configure(state="normal")
        self.log_text.insert("end", text + "\n", level if level in LEVEL_COLOR else "muted")
        lines = int(self.log_text.index("end-1c").split(".")[0])
        if lines > 4000:
            self.log_text.delete("1.0", f"{lines - 3000}.0")
        self.log_text.see("end")
        self.log_text.configure(state="disabled")

    def _clear_log(self) -> None:
        self.log_text.configure(state="normal")
        self.log_text.delete("1.0", "end")
        self.log_text.configure(state="disabled")

    def _tick_clock(self) -> None:
        if self._closing:
            return
        if self.running and self.start_time:
            elapsed = time.time() - self.start_time
            self.elapsed_lbl.configure(text=f"已运行 {engmod.human_time(elapsed)}")
        self.after(1000, self._tick_clock)

    def _warn_unfinished(self) -> None:
        state = cfgmod.read_state()
        if state:
            started = state.get("started", "未知时间")
            if messagebox.askyesno(
                    "存在未完成任务",
                    f"检测到上次任务未正常结束：\n开始时间：{started}\n"
                    f"源：{state.get('src', '')}\n目标：{state.get('dst', '')}\n\n"
                    "是否打开日志目录查看？（不会自动续传）", parent=self):
                self._open_log_folder()

    def _open_target(self) -> None:
        path = self.target_dir
        if path and os.path.isdir(path):
            os.startfile(path)
            return
        dst = self._selected(self.dst_combo)
        if dst and os.path.isdir(dst.root):
            os.startfile(dst.root)
            return
        messagebox.showinfo("提示", "暂无可打开的备份目录。", parent=self)

    def _open_log_folder(self) -> None:
        cfgmod.ensure_dirs()
        os.startfile(str(cfgmod.LOG_DIR))

    def _open_advanced(self) -> None:
        if self.running:
            messagebox.showinfo("提示", "任务进行中，暂不能修改高级设置。", parent=self)
            return
        AdvancedDialog(self)

    def _on_close(self) -> None:
        if self.running:
            if not messagebox.askyesno("退出", "任务正在进行，确定要退出吗？\n已复制的文件会保留。",
                                       parent=self):
                return
            if self.engine:
                self.engine.cancel()
            time.sleep(0.2)
        self._closing = True
        try:
            self.watcher.stop()
        except Exception:
            pass
        self.destroy()


def main() -> None:
    app = App()
    app.mainloop()
