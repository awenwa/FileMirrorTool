#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
文件镜像同步工具 v1.2
GitHub: https://github.com/awenwa/FileMirrorTool
修复：条件同步载入丢失、同步方式覆盖问题
调整：UI布局、名称、右键菜单、帮助菜单位置
"""

import os
import sys
import json
import re
import ctypes
import webbrowser
import urllib.request
import urllib.parse
from pathlib import Path
import tkinter as tk
from tkinter import ttk, filedialog, messagebox, Menu
from threading import Thread
import time

# ── 常量 ─────────────────────────────────────────────────────────────────

VERSION = "v1.2"
CONFIG_FILE = Path(__file__).parent / "file_mirror_config.json"
GITHUB_API = "https://api.github.com/repos/awenwa/FileMirrorTool/releases/latest"
BUG_EMAIL = "afane@qq.com"

# ── 权限工具 ────────────────────────────────────────────────────────────────

def is_admin():
    try:
        return ctypes.windll.shell32.IsUserAnAdmin()
    except Exception:
        return False


def run_as_admin():
    exe_path = os.path.abspath(sys.argv[0])
    try:
        ret = ctypes.windll.shell32.ShellExecuteW(
            None, "runas", exe_path, None, None, 1
        )
        return ret > 32
    except Exception as e:
        print(f"提权失败: {e}")
        return False


# ── 配置持久化 ────────────────────────────────────────────────────────────────

DEFAULT_CONFIG = {
    "last_src": "",
    "last_dst": "",
    "sync_type": "symlink",
}

def load_config():
    if CONFIG_FILE.exists():
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                cfg = json.load(f)
            for k, v in DEFAULT_CONFIG.items():
                cfg.setdefault(k, v)
            return cfg
        except Exception:
            pass
    return dict(DEFAULT_CONFIG)


def save_config(cfg):
    try:
        with open(CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"保存配置失败: {e}")


# ── 方案目录工具 ────────────────────────────────────────────────────────────

def get_scheme_dir():
    if getattr(sys, 'frozen', False):
        base_dir = Path(sys.executable).parent
    else:
        base_dir = Path(__file__).parent
    scheme_dir = base_dir / "config"
    scheme_dir.mkdir(parents=True, exist_ok=True)
    return scheme_dir


def load_scheme_file(filepath):
    try:
        with open(filepath, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        print(f"加载方案失败: {e}")
        return None


# ── 核心同步逻辑 ──────────────────────────────────────────────────────────

class FileMirrorSync:
    SYNC_SYMLINK = "symlink"
    SYNC_MIRROR = "mirror"
    SYNC_MIX = "mix"

    def __init__(self, src, dst, sync_type="symlink", rules=None,
                 progress_callback=None, log_callback=None, cancel_callback=None):
        self.src = Path(src)
        self.dst = Path(dst)
        self.sync_type = sync_type
        self.rules = rules or []
        self.progress_callback = progress_callback
        self.log_callback = log_callback
        self.cancel_callback = cancel_callback
        self.stats = {
            "dirs_created": 0,
            "symlinks_created": 0,
            "copies_created": 0,
            "skipped": 0,
            "errors": 0,
            "replaced": 0,
        }

    def _log(self, msg, level="INFO"):
        if self.log_callback:
            self.log_callback(f"[{level}] {msg}")

    def _progress(self, current, total, msg=""):
        if self.progress_callback:
            self.progress_callback(current, total, msg)

    def _should_cancel(self):
        if self.cancel_callback is not None:
            try:
                return self.cancel_callback()
            except Exception:
                pass
        return False

    def _should_copy(self, filepath: Path) -> bool:
        """判断文件是否需要复制（而非符号链接）"""
        if not self.rules or self.sync_type != self.SYNC_MIX:
            return self.sync_type == self.SYNC_MIRROR

        and_results, or_results, not_results = [], [], []
        ext_lower = filepath.suffix.lower()
        try:
            size_mb = filepath.stat().st_size / (1024 * 1024)
        except Exception:
            size_mb = 0

        for rule in self.rules:
            rtype, rvalue, rlogic = rule.get("type", ""), rule.get("value", ""), rule.get("logic", "and")
            matched = False

            if rtype == "ext":
                exts = set(e.strip().lower() for e in str(rvalue).split(",") if e.strip())
                matched = ext_lower in exts
            elif rtype == "size_range":
                try:
                    # Bug1修复：读取原始值和单位，转换为MB进行比较
                    min_raw = rvalue.get("min_raw")
                    max_raw = rvalue.get("max_raw")
                    unit = rvalue.get("unit", "MB").upper()
                    unit_map = {"B": 1/(1024*1024), "KB": 1/1024, "MB": 1.0, "GB": 1024}
                    unit_factor = unit_map.get(unit, 1.0)
                    
                    in_range = True
                    if min_raw is not None:
                        min_mb = float(min_raw) * unit_factor
                        in_range = in_range and size_mb >= min_mb
                    if max_raw is not None:
                        max_mb = float(max_raw) * unit_factor
                        in_range = in_range and size_mb <= max_mb
                    matched = in_range
                except Exception as e:
                    print(f"[WARN] size_range 规则解析失败: {e}")
                    matched = False

            if rlogic == "and": and_results.append(matched)
            elif rlogic == "or": or_results.append(matched)
            elif rlogic == "not": not_results.append(matched)

        if any(not_results): return False
        if any(or_results): return True
        if and_results: return all(and_results)
        return False

    def _link_type_for(self, filepath: Path) -> str:
        return "copy" if self._should_copy(filepath) else "symlink"

    def _create_symlink(self, src_file: Path, dst_file: Path) -> str:
        try:
            dst_file.parent.mkdir(parents=True, exist_ok=True)
            # BUG修复：强制删除已存在的符号链接/文件，重新创建
            if dst_file.exists() or dst_file.is_symlink():
                try:
                    if dst_file.is_symlink():
                        os.unlink(str(dst_file))
                    else:
                        os.remove(str(dst_file))
                    self.stats["replaced"] += 1
                except Exception:
                    pass
            os.symlink(str(src_file), str(dst_file))
            return "symlink"
        except OSError as e:
            if getattr(e, "winerror", None) == 1314:
                return self._copy_file(src_file, dst_file, fallback=True)
            return "error"
        except Exception:
            return "error"

    def _copy_file(self, src_file: Path, dst_file: Path, fallback=False) -> str:
        try:
            dst_file.parent.mkdir(parents=True, exist_ok=True)
            # BUG修复：强制覆盖已存在的文件
            if dst_file.exists():
                try:
                    os.remove(str(dst_file))
                    self.stats["replaced"] += 1
                except Exception:
                    pass
            import shutil
            shutil.copy2(str(src_file), str(dst_file))
            return "copy"
        except Exception:
            return "error"

    def _process_file(self, src_file: Path, dst_file: Path):
        """处理单个文件 - 智能同步：检查目标状态后决定操作"""
        link_type = self._link_type_for(src_file)
        should_be_symlink = (link_type == "symlink")
        
        # 智能同步：检查目标文件当前状态
        dst_is_symlink = dst_file.is_symlink()
        dst_exists = dst_file.exists()
        
        # 判断目标文件是否已经是期望的状态
        if dst_exists and dst_is_symlink == should_be_symlink:
            # 目标文件已经是期望的状态，跳过
            self.stats["skipped"] += 1
            return
        
        # 需要创建或替换
        result = self._copy_file(src_file, dst_file) if link_type == "copy" else self._create_symlink(src_file, dst_file)

        if result == "symlink": self.stats["symlinks_created"] += 1
        elif result == "copy": self.stats["copies_created"] += 1
        elif result == "skipped": self.stats["skipped"] += 1
        else: self.stats["errors"] += 1

    def _cleanup(self, src_files_set):
        removed = 0
        for root, _dirs, files in os.walk(str(self.dst), topdown=False):
            for fname in files:
                if self._should_cancel(): return removed
                dst_path = Path(root) / fname
                try:
                    rel = dst_path.relative_to(self.dst)
                    if str(rel).lower() not in src_files_set:
                        if dst_path.is_symlink():
                            os.unlink(str(dst_path))
                        else:
                            os.remove(str(dst_path))
                        removed += 1
                except Exception:
                    self.stats["errors"] += 1
        for root, dirs, _files in os.walk(str(self.dst), topdown=False):
            for d in dirs:
                dpath = Path(root) / d
                try:
                    if dpath.exists() and not any(dpath.iterdir()):
                        dpath.rmdir()
                except Exception:
                    pass
        return removed

    def run(self):
        start_time = time.time()
        self._log("=" * 60)
        self._log(f"源文件夹: {self.src}")
        self._log(f"目标文件夹: {self.dst}")
        type_names = {"symlink": "符号链接", "mirror": "实际复制", "mix": "混合同步"}
        self._log(f"同步类型: {type_names.get(self.sync_type, self.sync_type)}")
        if self.rules: self._log(f"筛选规则数: {len(self.rules)}")
        self._log("=" * 60)

        if not self.src.exists():
            self._log(f"源文件夹不存在: {self.src}", "ERROR")
            return False

        self.dst.mkdir(parents=True, exist_ok=True)

        self._log("正在统计文件总数...")
        total = 0
        scanned_dirs = 0
        for _root, _dirs, files in os.walk(self.src):
            total += len(files)
            scanned_dirs += 1
            if scanned_dirs % 50 == 0:
                self._log(f"  已扫描 {scanned_dirs} 个目录，发现 {total} 个文件...")
                self._progress(0, total, f"扫描中... 已发现 {total} 个文件")
        self._log(f"共发现 {total} 个文件，开始同步...")

        processed = 0
        src_files_rel = set()
        last_update = start_time

        for root, dirs, files in os.walk(self.src):
            if self._should_cancel():
                self._log("用户取消操作", "WARN")
                break

            rel_root = Path(root).relative_to(self.src)
            dst_root = self.dst / rel_root
            for d in dirs:
                dpath = dst_root / d
                if not dpath.exists():
                    try:
                        dpath.mkdir(parents=True, exist_ok=True)
                        self.stats["dirs_created"] += 1
                    except Exception as e:
                        self._log(f"创建目录失败: {dpath} - {e}", "WARN")

            for fname in files:
                if self._should_cancel(): break
                src_file = Path(root) / fname
                rel_path = src_file.relative_to(self.src)
                src_files_rel.add(str(rel_path).lower())
                dst_file = self.dst / rel_path
                self._process_file(src_file, dst_file)
                processed += 1

                now = time.time()
                if (now - last_update > 0.1) or (processed == total):
                    elapsed = now - start_time
                    speed = processed / elapsed if elapsed > 0 else 0
                    if speed > 0:
                        eta = (total - processed) / speed
                        if eta > 3600: eta_str = f"{int(eta//3600)}h {int(eta%3600//60)}m"
                        elif eta > 60: eta_str = f"{int(eta//60)}m {int(eta%60)}s"
                        else: eta_str = f"{int(eta)}s"
                    else: eta_str = "..."
                    msg = f"{rel_path.name} | 剩余: {eta_str}"
                    self._progress(processed, total, msg)
                    last_update = now

        if not self._should_cancel():
            self._log("清理目标中多余文件...")
            removed = self._cleanup(src_files_rel)
            if removed: self._log(f"删除了 {removed} 个多余文件")

        elapsed = time.time() - start_time
        self._log("=" * 60)
        self._log("同步完成！")
        self._log(f"目录创建: {self.stats['dirs_created']}")
        self._log(f"符号链接: {self.stats['symlinks_created']}")
        self._log(f"文件复制: {self.stats['copies_created']}")
        self._log(f"已覆盖: {self.stats['replaced']}")
        self._log(f"已跳过: {self.stats['skipped']}")
        self._log(f"错误数量: {self.stats['errors']}")
        self._log(f"耗时: {elapsed:.1f} 秒")
        self._log("=" * 60)
        self._progress(total, total, "完成")
        return self.stats["errors"] == 0


# ── GUI ────────────────────────────────────────────────────────────────────

class FileMirrorApp:

    SYNC_SYMLINK = "symlink"
    SYNC_MIRROR = "mirror"
    SYNC_MIX = "mix"

    SYNC_TYPE_OPTIONS = [
        ("Link", "右侧链接左侧", "symlink"),
        ("Mirror", "右侧镜像左侧", "mirror"),
        ("Mix", "混合同步", "mix"),
    ]

    def __init__(self, root):
        self.root = root
        self.root.title(f"文件镜像同步工具 {VERSION}")
        self.root.geometry("1050x720")
        self.root.minsize(900, 600)
        self._center_window()

        self.cfg = load_config()
        self.sync_thread = None
        self.sync_obj = None
        self.cancelled_flag = False
        self.is_admin = is_admin()
        self.current_scheme_path = None

        self._create_ui()

        if not self.is_admin:
            self._request_elevation()

    def _center_window(self):
        self.root.update_idletasks()
        w, h = 1050, 720
        x = (self.root.winfo_screenwidth() // 2) - (w // 2)
        y = (self.root.winfo_screenheight() // 2) - (h // 2)
        self.root.geometry(f"{w}x{h}+{x}+{y}")

    def _request_elevation(self):
        if run_as_admin():
            os._exit(0)
        else:
            messagebox.showerror("提权失败", "无法自动获取管理员权限。\n\n请手动以管理员身份运行此程序：\n右键 → 以管理员身份运行")

    # ── 构建 UI ──────────────────────────────────────────��─��────────────

    def _create_ui(self):
        # 自定义标题栏按钮（帮助）
        self._create_custom_titlebar()
        
        main = ttk.Frame(self.root, padding="10")
        main.pack(fill=tk.BOTH, expand=True)

        # 使用 Panedwindow 实现可调整大小的左右分割
        paned = ttk.PanedWindow(main, orient=tk.HORIZONTAL)
        paned.pack(fill=tk.BOTH, expand=True)

        # 左侧方案列表
        left_fr = ttk.Frame(paned)
        paned.add(left_fr, weight=0)  # 默认不随窗口调整宽度

        # 方案列表容器
        list_fr = ttk.LabelFrame(left_fr, text="已保存方案", padding="8")
        list_fr.pack(fill=tk.BOTH, expand=True, padx=5, pady=5)
        list_fr.rowconfigure(0, weight=1)
        list_fr.columnconfigure(0, weight=1)

        # 方案列表
        columns = ("name", "last_run")
        self.scheme_tree = ttk.Treeview(list_fr, columns=columns, show="headings", height=18, selectmode="browse")
        self.scheme_tree.heading("name", text="方案名称")
        self.scheme_tree.heading("last_run", text="最后执行")
        self.scheme_tree.column("name", width=110, anchor="w")
        self.scheme_tree.column("last_run", width=85, anchor="center")
        self.scheme_tree.grid(row=0, column=0, sticky="nswe")

        sb = ttk.Scrollbar(list_fr, orient=tk.VERTICAL, command=self.scheme_tree.yview)
        self.scheme_tree.configure(yscrollcommand=sb.set)
        sb.grid(row=0, column=1, sticky="ns")

        # 右键菜单
        self.scheme_menu = Menu(self.root, tearoff=0)
        self.scheme_menu.add_command(label="载入方案", command=self._load_scheme_from_list)
        self.scheme_menu.add_command(label="再次同步", command=self._run_scheme_from_list)
        self.scheme_menu.add_separator()
        self.scheme_menu.add_command(label="删除方案", command=self._delete_scheme)
        self.scheme_tree.bind("<Button-3>", self._show_scheme_menu)
        self.scheme_tree.bind("<<TreeviewSelect>>", self._on_scheme_select)

        # 右侧主内容
        right_fr = ttk.Frame(paned)
        paned.add(right_fr, weight=1)  # 右侧随窗口调整宽度
        right_fr.columnconfigure(0, weight=1)
        right_fr.rowconfigure(7, weight=1)

        self._build_right_panel(right_fr)
        self._refresh_scheme_list()

    def _create_custom_titlebar(self):
        """自定义标题栏：右上角帮助按钮"""
        titlebar = tk.Frame(self.root, bg="#f0f0f0", height=30)
        titlebar.pack(fill=tk.X)
        titlebar.pack_propagate(False)

        # 空白左侧区域（让出系统按钮位置）
        tk.Label(titlebar, text="", bg="#f0f0f0", width=30).pack(side=tk.LEFT)

        # 右侧帮助按钮
        help_btn = tk.Label(titlebar, text="帮助  ▼", bg="#f0f0f0", fg="#333", font=("Microsoft YaHei UI", 9), cursor="hand2")
        help_btn.pack(side=tk.RIGHT, padx=(0, 5))
        help_btn.bind("<Button-1>", self._show_help_menu)

        self.help_menu = Menu(self.root, tearoff=0)
        self.help_menu.add_command(label="检查更新", command=self._check_update)
        self.help_menu.add_command(label="Bug 反馈", command=self._show_bug_report)
        self.help_menu.add_separator()
        self.help_menu.add_command(label="关于", command=self._show_about)

    def _show_help_menu(self, event=None):
        # 在帮助按钮下方显示菜单
        self.help_menu.tk_popup(event.widget.winfo_rootx(), event.widget.winfo_rooty() + event.widget.winfo_height())

    def _build_right_panel(self, parent):
        parent.columnconfigure(0, weight=1)

        # 源/目标文件夹
        folder_fr = ttk.Frame(parent)
        folder_fr.grid(row=0, column=0, sticky="we", pady=(0, 8))
        folder_fr.columnconfigure(0, weight=1)
        folder_fr.columnconfigure(1, weight=1)

        left_fr = ttk.LabelFrame(folder_fr, text="左侧文件夹：", padding="5")
        left_fr.grid(row=0, column=0, sticky="we", padx=(0, 8))
        left_fr.columnconfigure(0, weight=1)
        self.src_var = tk.StringVar(value=self.cfg.get("last_src", ""))
        ttk.Entry(left_fr, textvariable=self.src_var, font=("Microsoft YaHei UI", 10)).grid(row=0, column=0, sticky="we", padx=(0, 5))
        ttk.Button(left_fr, text="浏览...", command=self._browse_src, width=8).grid(row=0, column=1)

        right_fr = ttk.LabelFrame(folder_fr, text="右侧文件夹：", padding="5")
        right_fr.grid(row=0, column=1, sticky="we")
        right_fr.columnconfigure(0, weight=1)
        self.dst_var = tk.StringVar(value=self.cfg.get("last_dst", ""))
        ttk.Entry(right_fr, textvariable=self.dst_var, font=("Microsoft YaHei UI", 10)).grid(row=0, column=0, sticky="we", padx=(0, 5))
        ttk.Button(right_fr, text="浏览...", command=self._browse_dst, width=8).grid(row=0, column=1)

        # 同步方式
        type_fr = ttk.LabelFrame(parent, text="同步方式", padding="8")
        type_fr.grid(row=1, column=0, sticky="we", pady=(0, 6))

        self.sync_type_var = tk.StringVar()
        last_st = self.cfg.get("sync_type", "symlink")

        # Link 选项
        opt1 = ttk.Frame(type_fr)
        opt1.pack(fill=tk.X, pady=2)
        rb1 = ttk.Radiobutton(opt1, text="🔗 Link：右侧链接左侧",
                          value="symlink", variable=self.sync_type_var, command=self._on_sync_type_change)
        rb1.pack(side=tk.LEFT)
        opt1.bind('<Button-1>', lambda e: (self.sync_type_var.set("symlink"), self._on_sync_type_change()))
        if last_st == "symlink": self.sync_type_var.set("symlink")

        # Mirror 选项
        opt2 = ttk.Frame(type_fr)
        opt2.pack(fill=tk.X, pady=2)
        rb2 = ttk.Radiobutton(opt2, text="💾 Mirror：右侧镜像左侧",
                          value="mirror", variable=self.sync_type_var, command=self._on_sync_type_change)
        rb2.pack(side=tk.LEFT)
        opt2.bind('<Button-1>', lambda e: (self.sync_type_var.set("mirror"), self._on_sync_type_change()))
        if last_st == "mirror": self.sync_type_var.set("mirror")

        # Mix 选项
        opt3 = ttk.Frame(type_fr)
        opt3.pack(fill=tk.X, pady=2)
        rb3 = ttk.Radiobutton(opt3, text="⚡ Mix：混合同步",
                          value="mix", variable=self.sync_type_var, command=self._on_sync_type_change)
        rb3.pack(side=tk.LEFT)
        desc_label = ttk.Label(opt3, text="（符合下述条件的文件镜像，其他文件链接）",
                            font=("Microsoft YaHei UI", 8), foreground="gray")
        desc_label.pack(side=tk.LEFT, padx=(5, 0))
        def on_opt3_click(event):
            self.sync_type_var.set("mix")
            self._on_sync_type_change()
        opt3.bind('<Button-1>', on_opt3_click)
        desc_label.bind('<Button-1>', on_opt3_click)
        if last_st == "mix": self.sync_type_var.set("mix")

        # 条件区域（紧凑布局）
        self.filter_frame = ttk.Frame(type_fr, padding=(20, 5, 0, 0))

        cond_row = ttk.Frame(self.filter_frame)
        cond_row.pack(fill=tk.X, pady=2)

        # 文件大小 - 紧凑
        ttk.Label(cond_row, text="大小：", font=("Microsoft YaHei UI", 9)).pack(side=tk.LEFT)
        self.size_min_var = tk.StringVar()
        self.size_min_entry = ttk.Entry(cond_row, textvariable=self.size_min_var, font=("Microsoft YaHei UI", 9), width=6)
        self.size_min_entry.pack(side=tk.LEFT, padx=(2, 2))
        self._size_min_placeholder = "最小"
        self._insert_placeholder(self.size_min_entry, self._size_min_placeholder)
        self.size_min_entry.bind('<FocusIn>', lambda e: self._clear_placeholder(self.size_min_entry, self._size_min_placeholder))
        self.size_min_entry.bind('<FocusOut>', lambda e: self._restore_placeholder(self.size_min_entry, self._size_min_placeholder))

        ttk.Label(cond_row, text="—", font=("Microsoft YaHei UI", 9)).pack(side=tk.LEFT)

        self.size_max_var = tk.StringVar()
        self.size_max_entry = ttk.Entry(cond_row, textvariable=self.size_max_var, font=("Microsoft YaHei UI", 9), width=6)
        self.size_max_entry.pack(side=tk.LEFT, padx=(2, 2))
        self._size_max_placeholder = "最大"
        self._insert_placeholder(self.size_max_entry, self._size_max_placeholder)
        self.size_max_entry.bind('<FocusIn>', lambda e: self._clear_placeholder(self.size_max_entry, self._size_max_placeholder))
        self.size_max_entry.bind('<FocusOut>', lambda e: self._restore_placeholder(self.size_max_entry, self._size_max_placeholder))

        self.size_unit_var = tk.StringVar(value="KB")
        unit_combo = ttk.Combobox(cond_row, textvariable=self.size_unit_var,
                               values=["B", "KB", "MB", "GB"], state="readonly",
                               font=("Microsoft YaHei UI", 9), width=4)
        unit_combo.pack(side=tk.LEFT, padx=(2, 8))

        # 逻辑运算符
        self.logic_op_var = tk.StringVar(value="AND")
        logic_combo = ttk.Combobox(cond_row, textvariable=self.logic_op_var,
                                 values=["AND", "OR", "NOT"], state="readonly",
                                 font=("Microsoft YaHei UI", 9), width=4)
        logic_combo.pack(side=tk.LEFT, padx=(0, 8))

        # 文件类型 - 紧凑
        ttk.Label(cond_row, text="类型：", font=("Microsoft YaHei UI", 9)).pack(side=tk.LEFT)
        self.ext_var = tk.StringVar()
        self.ext_entry = ttk.Entry(cond_row, textvariable=self.ext_var, font=("Microsoft YaHei UI", 9), width=18)
        self.ext_entry.pack(side=tk.LEFT, padx=(2, 8))
        self._ext_placeholder = ".txt .doc 等"
        self._insert_placeholder(self.ext_entry, self._ext_placeholder)
        self.ext_entry.bind('<FocusIn>', lambda e: self._clear_placeholder(self.ext_entry, self._ext_placeholder))
        self.ext_entry.bind('<FocusOut>', lambda e: self._restore_placeholder(self.ext_entry, self._ext_placeholder))

        self._on_sync_type_change()

        # 保存方案按钮（放在同步方式下方）
        save_btn = ttk.Button(parent, text="保存方案", command=self._save_scheme, width=12)
        save_btn.grid(row=2, column=0, sticky="w", pady=(6, 0))

        # 进度条
        prog_fr = ttk.LabelFrame(parent, text="同步进度", padding="8")
        prog_fr.grid(row=3, column=0, sticky="we", pady=(6, 6))

        self.progress_var = tk.DoubleVar()
        self.progress_bar = ttk.Progressbar(prog_fr, variable=self.progress_var, maximum=100, mode="determinate")
        self.progress_bar.pack(fill=tk.X, pady=(0, 4))

        self.status_var = tk.StringVar(value="就绪 - 请选择源和目标文件夹")
        ttk.Label(prog_fr, textvariable=self.status_var, font=("Microsoft YaHei UI", 9)).pack(fill=tk.X)

        # 按钮区域
        btn_fr = ttk.Frame(parent)
        btn_fr.grid(row=4, column=0, sticky="we", pady=(0, 6))

        self.start_btn = ttk.Button(btn_fr, text="开始同步", command=self._start_sync, width=12)
        self.start_btn.pack(side=tk.LEFT, padx=(0, 10))

        self.cancel_btn = ttk.Button(btn_fr, text="取消", command=self._cancel_sync, state=tk.DISABLED, width=12)
        self.cancel_btn.pack(side=tk.LEFT, padx=(0, 10))

        self.clear_btn = ttk.Button(btn_fr, text="清空日志", command=self._clear_log, width=12)
        self.clear_btn.pack(side=tk.LEFT)

        info_color = "green" if self.is_admin else "red"
        info_text = "✓ 管理员权限已获取" if self.is_admin else "✗ 需要管理员权限"
        ttk.Label(btn_fr, text=info_text, font=("Microsoft YaHei UI", 9, "bold"), foreground=info_color).pack(side=tk.RIGHT)

        # 日志区域
        log_fr = ttk.LabelFrame(parent, text="执行日志", padding="8")
        log_fr.grid(row=7, column=0, sticky="nswe")
        log_fr.columnconfigure(0, weight=1)
        log_fr.rowconfigure(0, weight=1)

        self.log_text = tk.Text(log_fr, font=("Consolas", 9), wrap=tk.WORD, state=tk.DISABLED, height=8)
        sb = ttk.Scrollbar(log_fr, orient=tk.VERTICAL, command=self.log_text.yview)
        self.log_text.configure(yscrollcommand=sb.set)
        self.log_text.grid(row=0, column=0, sticky="nswe")
        sb.grid(row=0, column=1, sticky="ns")

    # ── placeholder 辅助方法 ───────────────────────────────────────

    def _insert_placeholder(self, entry, text):
        entry.insert(0, text)
        entry.config(foreground="gray")
        entry._has_placeholder = True

    def _clear_placeholder(self, entry, text):
        if getattr(entry, '_has_placeholder', False):
            entry.delete(0, tk.END)
            entry.config(foreground="black")
            entry._has_placeholder = False

    def _restore_placeholder(self, entry, text):
        if not getattr(entry, '_has_placeholder', False) and not entry.get().strip():
            self._insert_placeholder(entry, text)

    # ── 方案列表相关 ───────────────────────────────────────────────────

    def _refresh_scheme_list(self):
        self.scheme_tree.delete(*self.scheme_tree.get_children())
        scheme_dir = get_scheme_dir()
        if not scheme_dir.exists(): return
        for fpath in sorted(scheme_dir.glob("*.json")):
            data = load_scheme_file(fpath)
            if not data: continue
            name = data.get("name", fpath.stem)
            last_run = data.get("last_run_time") or "-"
            if last_run and last_run != "-":
                try: last_run = str(last_run)[:16]
                except: pass
            self.scheme_tree.insert("", tk.END, iid=str(fpath), values=(name, last_run))

    def _on_scheme_select(self, event=None):
        pass  # 按钮已改为右键菜单

    def _get_selected_scheme_path(self):
        sel = self.scheme_tree.selection()
        return sel[0] if sel else None

    def _show_scheme_menu(self, event):
        """右键菜单"""
        # 选中点击的行
        item = self.scheme_tree.identify_row(event.y)
        if item:
            self.scheme_tree.selection_set(item)
            self.scheme_menu.tk_popup(event.x_root, event.y_root)

    def _load_scheme_from_list(self):
        fpath = self._get_selected_scheme_path()
        if not fpath:
            messagebox.showwarning("提示", "请先选择一个方案")
            return
        data = load_scheme_file(fpath)
        if not data:
            messagebox.showerror("载入失败", f"无法读取方案文件：\n{fpath}")
            return
        self._apply_scheme_data(data, fpath)

    def _run_scheme_from_list(self):
        fpath = self._get_selected_scheme_path()
        if not fpath:
            messagebox.showwarning("提示", "请先选择一个方案")
            return
        data = load_scheme_file(fpath)
        if not data:
            messagebox.showerror("载入失败", f"无法读取方案文件：\n{fpath}")
            return
        self._apply_scheme_data(data, fpath)
        self._start_sync()

    def _delete_scheme(self):
        """删除选中的方案"""
        fpath = self._get_selected_scheme_path()
        if not fpath:
            messagebox.showwarning("提示", "请先选择一个方案")
            return
        name = Path(fpath).stem
        if messagebox.askyesno("确认删除", f"确定要删除方案「{name}」吗？"):
            try:
                os.remove(fpath)
                self._refresh_scheme_list()
                messagebox.showinfo("删除成功", f"方案「{name}」已删除")
            except Exception as e:
                messagebox.showerror("删除失败", f"无法删除文件：\n{e}")

    def _apply_scheme_data(self, data, fpath):
        """应用方案数据到 UI"""
        self.src_var.set(data.get("src", "").replace('/', '\\'))
        self.dst_var.set(data.get("dst", "").replace('/', '\\'))

        st = data.get("sync_type", "symlink")
        matched = False
        for _title, _desc, val in self.SYNC_TYPE_OPTIONS:
            if st in (val, _title):
                self.sync_type_var.set(val)
                matched = True
                break
        if not matched:
            self.sync_type_var.set("symlink")

        self._on_sync_type_change()

        self.logic_op_var.set(data.get("logic_op", "AND"))
        self.size_unit_var.set(data.get("size_unit", "KB"))

        # 重置输入栏
        self.ext_var.set("")
        self._insert_placeholder(self.ext_entry, self._ext_placeholder)
        self.size_min_var.set("")
        self._insert_placeholder(self.size_min_entry, self._size_min_placeholder)
        self.size_max_var.set("")
        self._insert_placeholder(self.size_max_entry, self._size_max_placeholder)

        rules = data.get("rules", [])
        for rule in rules:
            rtype, rvalue = rule.get("type", ""), rule.get("value", "")
            if rtype == "ext" and rvalue:
                exts = rvalue.replace(",", " ")
                if getattr(self.ext_entry, '_has_placeholder', False):
                    self.ext_entry.delete(0, tk.END)
                    self.ext_entry.config(foreground="black")
                    self.ext_entry._has_placeholder = False
                self.ext_var.set(exts)
            elif rtype == "size_range" and isinstance(rvalue, dict):
                # Bug1修复：直接恢复原始值和单位
                min_raw = rvalue.get("min_raw")
                max_raw = rvalue.get("max_raw")
                unit = rvalue.get("unit", "KB").upper()
                self.size_unit_var.set(unit)
                # 先清空 Entry（无论是否有 placeholder 状态）
                self.size_min_entry.delete(0, tk.END)
                self.size_min_entry.config(foreground="black")
                self.size_min_entry._has_placeholder = False
                self.size_max_entry.delete(0, tk.END)
                self.size_max_entry.config(foreground="black")
                self.size_max_entry._has_placeholder = False
                # 设置新值
                if min_raw is not None:
                    self.size_min_var.set(min_raw)
                if max_raw is not None:
                    self.size_max_var.set(max_raw)

        self._on_sync_type_change()
        self.current_scheme_path = str(fpath)
        self._log(f"[INFO] 已载入方案：{data.get('name', Path(fpath).stem)}")

    # ── 规则收集 ─────────────────────────────────────────────────────

    def _collect_rules(self):
        """收集筛选规则 - 保存原始值和单位"""
        rules = []
        logic_op = self.logic_op_var.get().strip().lower()

        # 文件类型规则
        ext_val = self.ext_var.get().strip()
        if ext_val and ext_val != self._ext_placeholder:
            exts = []
            for e in ext_val.replace(",", " ").split():
                e = e.strip()
                if e:
                    if not e.startswith("."): e = "." + e
                    exts.append(e.lower())
            if exts:
                rules.append({"type": "ext", "value": ",".join(exts), "logic": logic_op})

        # 文件大小规则 - Bug1修复：保存原始字符串和单位
        ph_min = getattr(self, '_size_min_placeholder', '')
        ph_max = getattr(self, '_size_max_placeholder', '')
        size_min_val = self.size_min_var.get().strip()
        size_max_val = self.size_max_var.get().strip()
        has_size_rule = False
        size_data = {}

        if size_min_val and size_min_val != ph_min:
            size_data["min_raw"] = size_min_val  # 保存原始字符串
            has_size_rule = True

        if size_max_val and size_max_val != ph_max:
            size_data["max_raw"] = size_max_val  # 保存原始字符串
            has_size_rule = True

        if has_size_rule:
            size_data["unit"] = self.size_unit_var.get().upper()  # 保存单位
            rules.append({"type": "size_range", "value": size_data, "logic": logic_op})

        return rules

    # ── 同步类型切换 ───────────────────────────────────────────────────

    def _on_sync_type_change(self):
        sync_type = self.sync_type_var.get()
        if sync_type == "mix":
            try: self.filter_frame.pack(fill=tk.X, pady=(4, 0))
            except Exception as e: print(f"[ERROR] pack failed: {e}")
        else:
            self.filter_frame.pack_forget()

    # ── 文件浏览 ───────────────────────────────────────────────────────

    def _browse_src(self):
        p = filedialog.askdirectory(title="选择源文件夹", initialdir=self.src_var.get() or None)
        if p: self.src_var.set(p.replace('/', '\\'))

    def _browse_dst(self):
        p = filedialog.askdirectory(title="选择目标文件夹", initialdir=self.dst_var.get() or None)
        if p: self.dst_var.set(p.replace('/', '\\'))

    # ── 方案保存 ─────────────────────────────────────────────────────

    def _save_scheme(self):
        from tkinter import simpledialog
        scheme_name = simpledialog.askstring("保存方案", "请输入方案名称：", parent=self.root)
        if not scheme_name: return

        safe_name = re.sub(r'[<>:"/\\|?*]', '_', scheme_name.strip())
        if not safe_name:
            messagebox.showerror("错误", "方案名称无效")
            return

        filepath = get_scheme_dir() / f"{safe_name}.json"

        if filepath.exists():
            if not messagebox.askyesno("确认覆盖", f"方案「{scheme_name}」已存在，是否覆盖？"):
                return

        scheme_data = {
            "version": VERSION,
            "name": scheme_name,
            "src": self.src_var.get(),
            "dst": self.dst_var.get(),
            "sync_type": self.sync_type_var.get(),
            "logic_op": self.logic_op_var.get(),
            "size_unit": self.size_unit_var.get(),
            "rules": self._collect_rules(),
            "created_time": time.strftime('%Y-%m-%d %H:%M:%S'),
            "last_run_time": None,
        }

        try:
            with open(filepath, "w", encoding="utf-8") as f:
                json.dump(scheme_data, f, ensure_ascii=False, indent=2)
            self.current_scheme_path = str(filepath)
            messagebox.showinfo("保存成功", f"方案「{scheme_name}」已保存")
            self._refresh_scheme_list()
            self.scheme_tree.selection_set(str(filepath))
        except Exception as e:
            messagebox.showerror("保存失败", f"无法写入文件：\n{e}")

    # ── 日志 ────────────────────────────────────────────────────────────

    def _log(self, msg):
        self.log_text.configure(state=tk.NORMAL)
        self.log_text.insert(tk.END, msg + "\n")
        self.log_text.see(tk.END)
        self.log_text.configure(state=tk.DISABLED)

    def _clear_log(self):
        self.log_text.configure(state=tk.NORMAL)
        self.log_text.delete(1.0, tk.END)
        self.log_text.configure(state=tk.DISABLED)

    def _update_progress(self, current, total, msg):
        pct = (current / total * 100) if total > 0 else 0
        self.progress_var.set(pct)
        self.status_var.set(f"{current}/{total} ({pct:.1f}%) | {msg}")

    # ── 开始同步 ─────────────────────────────────────────────────────

    def _start_sync(self):
        src = self.src_var.get().strip()
        dst = self.dst_var.get().strip()

        if not src or not os.path.exists(src):
            messagebox.showerror("错误", f"源文件夹不存在：\n{src}")
            return
        if not dst:
            messagebox.showerror("错误", "请选择目标文件夹")
            return

        sync_type = self.sync_type_var.get()
        if sync_type not in ("symlink", "mirror", "mix"):
            for _title, _desc, val in self.SYNC_TYPE_OPTIONS:
                if sync_type == _title:
                    sync_type = val
                    break

        if sync_type == "symlink" and not self.is_admin:
            messagebox.showerror("权限不足", "创建符号链接需要管理员权限。\n\n请右键程序 → 以管理员身份运行")
            return

        # 保存配置
        self.cfg["last_src"] = src
        self.cfg["last_dst"] = dst
        self.cfg["sync_type"] = sync_type
        save_config(self.cfg)

        type_names = {"symlink": "符号链接", "mirror": "实际复制", "mix": "混合同步"}
        if not messagebox.askyesno("确认",
            f"即将同步文件：\n\n源：{src}\n目标：{dst}\n同步类型：{type_names.get(sync_type, sync_type)}\n\n是否继续？"):
            return

        self.cancelled_flag = False
        self.start_btn.configure(state=tk.DISABLED)
        self.cancel_btn.configure(state=tk.NORMAL)

        self.sync_obj = FileMirrorSync(
            src=src, dst=dst,
            sync_type=sync_type,
            rules=self._collect_rules(),
            progress_callback=self._update_progress,
            log_callback=self._log,
            cancel_callback=lambda: self.cancelled_flag,
        )

        t = Thread(target=self._run_sync, daemon=True)
        t.start()

    def _run_sync(self):
        try:
            success = self.sync_obj.run()
            self.root.after(0, lambda s=success: self._sync_complete(s, None))
        except Exception as e:
            import traceback
            self._log(traceback.format_exc())
            err_msg = str(e)
            self.root.after(0, lambda e=err_msg: self._sync_complete(False, e))

    def _sync_complete(self, success, error):
        self.start_btn.configure(state=tk.NORMAL)
        self.cancel_btn.configure(state=tk.DISABLED)
        self.progress_var.set(100 if success else 0)

        if success and self.current_scheme_path:
            self._update_scheme_run_time(self.current_scheme_path)

        if error:
            messagebox.showerror("同步失败", error)
        elif success:
            messagebox.showinfo("同步完成", "文件镜像同步成功完成！")
        else:
            messagebox.showwarning("同步完成", "同步完成，但有错误发生，请查看日志")

    def _update_scheme_run_time(self, fpath):
        try:
            data = load_scheme_file(fpath)
            if data is None: return
            now_str = time.strftime('%Y-%m-%d %H:%M:%S')
            data["last_run_time"] = now_str
            with open(fpath, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            self._refresh_scheme_list()
            if self.current_scheme_path:
                try: self.scheme_tree.selection_set(self.current_scheme_path)
                except: pass
        except Exception as e:
            print(f"更新方案执行时间失败: {e}")

    def _cancel_sync(self):
        self.cancelled_flag = True
        self._log("[WARN] 正在取消...")
        self.cancel_btn.configure(state=tk.DISABLED)

    # ── 关于 / 更新 / Bug反馈 ─────────────────────────────────────────

    def _show_about(self):
        dlg = tk.Toplevel(self.root)
        dlg.title("关于")
        dlg.geometry("360x280")
        dlg.resizable(False, False)
        dlg.transient(self.root)
        dlg.grab_set()
        # Bug2修复：关于窗口居中显示
        dlg.update_idletasks()
        root_x = self.root.winfo_x()
        root_y = self.root.winfo_y()
        root_w = self.root.winfo_width()
        root_h = self.root.winfo_height()
        x = root_x + (root_w - 360) // 2
        y = root_y + (root_h - 280) // 2
        dlg.geometry(f"360x280+{x}+{y}")

        main = ttk.Frame(dlg, padding="20")
        main.pack(fill=tk.BOTH, expand=True)

        ttk.Label(main, text="文件镜像同步工具", font=("Microsoft YaHei UI", 14, "bold")).pack(pady=(0, 5))
        ttk.Label(main, text=f"版本：{VERSION}", font=("Microsoft YaHei UI", 10)).pack()
        ttk.Label(main, text="作者：awen", font=("Microsoft YaHei UI", 10)).pack(pady=(0, 20))

        ttk.Button(main, text="检查更新", command=self._check_update, width=18).pack(pady=(0, 8))
        ttk.Button(main, text="Bug 反馈", command=self._show_bug_report, width=18).pack(pady=(0, 20))

        ttk.Label(main, text="GitHub：github.com/awenwa/FileMirrorTool", font=("Microsoft YaHei UI", 8), foreground="gray").pack()

    def _check_update(self):
        try:
            req = urllib.request.Request(GITHUB_API, headers={"User-Agent": "FileMirrorTool"})
            with urllib.request.urlopen(req, timeout=8) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            latest = data.get("tag_name", "unknown")
            if latest == VERSION:
                messagebox.showinfo("检查更新", f"当前已是最新版本 {VERSION}")
            else:
                if messagebox.askyesno("检查更新", f"发现新版本：{latest}\n当前版本：{VERSION}\n\n是否打开下载页面？"):
                    webbrowser.open(data.get("html_url", "https://github.com/awenwa/FileMirrorTool/releases"))
        except Exception as e:
            messagebox.showerror("检查更新失败", f"无法连接至 GitHub API：\n{e}")

    def _show_bug_report(self):
        dlg = tk.Toplevel(self.root)
        dlg.title("Bug 反馈")
        dlg.geometry("420x360")
        dlg.transient(self.root)
        dlg.grab_set()

        main = ttk.Frame(dlg, padding="15")
        main.pack(fill=tk.BOTH, expand=True)

        ttk.Label(main, text="姓名 / 昵称：", font=("Microsoft YaHei UI", 9)).grid(row=0, column=0, sticky="w", pady=(0, 4))
        self.name_var = tk.StringVar()
        ttk.Entry(main, textvariable=self.name_var, font=("Microsoft YaHei UI", 10)).grid(row=1, column=0, sticky="we", pady=(0, 10))

        ttk.Label(main, text="邮箱（选填）：", font=("Microsoft YaHei UI", 9)).grid(row=2, column=0, sticky="w", pady=(0, 4))
        self.email_var = tk.StringVar()
        ttk.Entry(main, textvariable=self.email_var, font=("Microsoft YaHei UI", 10)).grid(row=3, column=0, sticky="we", pady=(0, 10))

        ttk.Label(main, text="问题描述（必填）：", font=("Microsoft YaHei UI", 9)).grid(row=4, column=0, sticky="w", pady=(0, 4))
        self.desc_text = tk.Text(main, font=("Microsoft YaHei UI", 10), height=7, wrap=tk.WORD)
        self.desc_text.grid(row=5, column=0, sticky="wens", pady=(0, 15))

        main.columnconfigure(0, weight=1)
        main.rowconfigure(5, weight=1)

        btn_fr = ttk.Frame(main)
        btn_fr.grid(row=6, column=0, sticky="e")

        ttk.Button(btn_fr, text="发送", command=self._send_bug_report, width=12).pack(side=tk.LEFT, padx=(0, 10))
        ttk.Button(btn_fr, text="取消", command=dlg.destroy, width=12).pack(side=tk.LEFT)

        self.bug_dlg = dlg

    def _send_bug_report(self):
        desc = self.desc_text.get("1.0", tk.END).strip()
        if not desc:
            messagebox.showerror("错误", "请填写问题描述")
            return
        name = self.name_var.get().strip() or "匿名"
        email = self.email_var.get().strip()

        body = f"姓名：{name}\n邮箱：{email}\n版本：{VERSION}\n时间：{time.strftime('%Y-%m-%d %H:%M:%S')}\n\n问题描述：\n{desc}"

        try:
            subject = urllib.parse.quote(f"[Bug反馈] 文件镜像同步工具 {VERSION}")
            body_enc = urllib.parse.quote(body)
            webbrowser.open(f"mailto:{BUG_EMAIL}?subject={subject}&body={body_enc}")
            messagebox.showinfo("发送反馈", f"已调用系统邮件客户端。\n\n如果未弹出邮件窗口，请手动发送邮件至：\n{BUG_EMAIL}")
            self.bug_dlg.destroy()
        except Exception as e:
            messagebox.showerror("错误", f"无法打开邮件客户端：\n{e}")


# ── 主入口 ─────────────────────────────────────────────────────────────────

def main():
    if sys.platform != "win32":
        print("此工具仅支持 Windows 系统")
        sys.exit(1)

    root = tk.Tk()
    try:
        from ctypes import windll
        windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        pass

    app = FileMirrorApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()