#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
文件镜像同步工具 v1.3
GitHub: https://github.com/awenwa/FileMirrorTool
改进：字体放大、执行时间保存修复、进度保存、日志自动保存、文件夹互换、方案重命名
"""

import os
import sys
import json
import re
import uuid
import ctypes
import webbrowser
import urllib.request
import urllib.parse
from pathlib import Path
import tkinter as tk
from tkinter import ttk, filedialog, messagebox, Menu, simpledialog
from threading import Thread, Lock
import time
import traceback

# ── 常量 ─────────────────────────────────────────────────────────────────

VERSION = "v1.4"
# 数据目录：优先使用 exe 所在的 config 文件夹，兼容开发模式
if getattr(sys, 'frozen', False):
    _BASE_DIR = Path(sys.executable).parent / "config"
else:
    _BASE_DIR = Path(__file__).parent
SCHEME_FILE = _BASE_DIR / "schemes.json"        # 所有方案合并到一个文件
LOG_FILE = _BASE_DIR / "sync_log.txt"            # 所有日志合并到一个文件
CONFIG_FILE = _BASE_DIR / "file_mirror_config.json"
PROGRESS_FILE = _BASE_DIR / "sync_progress.json"
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
    "font_size": 10,  # 默认字体大小
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


# ── 方案存储（合并到单一文件） ──────────────────────────────────────────────

def load_schemes():
    """从单一文件加载所有方案，返回 list[dict]"""
    if not SCHEME_FILE.exists():
        return []
    try:
        with open(SCHEME_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        schemes = data.get("schemes", []) if isinstance(data, dict) else (data or [])
        return schemes
    except Exception as e:
        print(f"加载方案失败: {e}")
        return []


def save_schemes(schemes):
    """保存所有方案到单一文件"""
    try:
        SCHEME_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(SCHEME_FILE, "w", encoding="utf-8") as f:
            json.dump({"schemes": schemes}, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"保存方案失败: {e}")


def find_scheme(scheme_id):
    """按 id 查找方案"""
    for s in load_schemes():
        if s.get("id") == scheme_id:
            return s
    return None


def load_scheme_file(filepath):
    """兼容旧调用（按文件路径加载，已弃用）"""
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
                 progress_callback=None, log_callback=None, cancel_callback=None,
                 progress_file=None):
        self.src = Path(src)
        self.dst = Path(dst)
        self.sync_type = sync_type
        self.rules = rules or []
        self.progress_callback = progress_callback
        self.log_callback = log_callback
        self.cancel_callback = cancel_callback
        self.progress_file = progress_file
        self.progress_lock = Lock()
        self.stats = {
            "dirs_created": 0,
            "symlinks_created": 0,
            "copies_created": 0,
            "skipped": 0,
            "errors": 0,
            "replaced": 0,
            "cancelled": 0,
        }
        self.processed_files = set()  # 已处理的文件（相对路径）
        self.total_files = 0
        self.current_index = 0

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

    def _save_progress(self):
        """保存当前进度到文件"""
        if not self.progress_file:
            return
        try:
            with self.progress_lock:
                data = {
                    "src": str(self.src),
                    "dst": str(self.dst),
                    "sync_type": self.sync_type,
                    "rules": self.rules,
                    "processed_files": list(self.processed_files),
                    "stats": self.stats,
                    "current_index": self.current_index,
                    "total_files": self.total_files,
                    "save_time": time.strftime('%Y-%m-%d %H:%M:%S'),
                }
                with open(self.progress_file, "w", encoding="utf-8") as f:
                    json.dump(data, f, ensure_ascii=False, indent=2)
        except Exception as e:
            print(f"保存进度失败: {e}")

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
            if dst_file.exists():
                try:
                    os.remove(str(dst_file))
                    self.stats["replaced"] += 1
                except Exception:
                    pass
            # 分块复制，每写完一块检查一次取消标志，保证大文件也能及时响应取消
            BUF = 8 * 1024 * 1024  # 8MB
            with open(src_file, "rb") as fin, open(dst_file, "wb") as fout:
                while True:
                    if self._should_cancel():
                        # 取消：删除未完成的目标文件
                        try:
                            fout.close()
                            os.remove(str(dst_file))
                        except Exception:
                            pass
                        return "cancelled"
                    chunk = fin.read(BUF)
                    if not chunk:
                        break
                    fout.write(chunk)
            return "copy"
        except Exception:
            return "error"

    def _process_file(self, src_file: Path, dst_file: Path):
        """处理单个文件 - 智能同步：检查目标状态后决定操作"""
        link_type = self._link_type_for(src_file)
        should_be_symlink = (link_type == "symlink")
        
        dst_is_symlink = dst_file.is_symlink()
        dst_exists = dst_file.exists()
        
        if dst_exists and dst_is_symlink == should_be_symlink:
            self.stats["skipped"] += 1
            return
        
        result = self._copy_file(src_file, dst_file) if link_type == "copy" else self._create_symlink(src_file, dst_file)

        if result == "symlink": self.stats["symlinks_created"] += 1
        elif result == "copy": self.stats["copies_created"] += 1
        elif result == "skipped": self.stats["skipped"] += 1
        elif result == "cancelled": self.stats["cancelled"] = self.stats.get("cancelled", 0) + 1
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

    def run(self, resume=False):
        start_time = time.time()
        self._log("=" * 60)
        if resume:
            self._log("恢复同步进度...")
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

        # 统计文件总数
        if not resume or self.total_files == 0:
            self._log("正在统计文件总数...")
            total = 0
            scanned_dirs = 0
            for _root, _dirs, files in os.walk(self.src):
                if self._should_cancel():
                    self._log("用户取消操作（扫描阶段）", "WARN")
                    return False
                total += len(files)
                scanned_dirs += 1
                if scanned_dirs % 50 == 0:
                    self._log(f"  已扫描 {scanned_dirs} 个目录，发现 {total} 个文件...")
                    self._progress(0, total, f"扫描中... 已发现 {total} 个文件")
            self.total_files = total
            self._log(f"共发现 {total} 个文件，开始同步...")
        else:
            self._log(f"从进度文件恢复，共 {self.total_files} 个文件，已处理 {self.current_index} 个...")

        processed = self.current_index
        src_files_rel = set()
        last_update = start_time
        save_counter = 0  # 计数器，每处理100个文件保存一次进度

        for root, dirs, files in os.walk(self.src):
            if self._should_cancel():
                self._log("用户取消操作，进度已保存", "WARN")
                self._save_progress()
                return False

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
                if self._should_cancel():
                    self._log("用户取消操作，进度已保存", "WARN")
                    self._save_progress()
                    return False
                
                src_file = Path(root) / fname
                rel_path = str(src_file.relative_to(self.src))
                
                # 断点续传：跳过已处理的文件
                if resume and rel_path in self.processed_files:
                    processed += 1
                    continue
                
                dst_file = self.dst / rel_path
                self._process_file(src_file, dst_file)
                self.processed_files.add(rel_path)
                src_files_rel.add(rel_path.lower())  # 记录源文件相对路径，用于cleanup
                processed += 1
                self.current_index = processed
                save_counter += 1

                # 每处理100个文件保存一次进度
                if save_counter >= 100:
                    self._save_progress()
                    save_counter = 0

                now = time.time()
                if (now - last_update > 0.1) or (processed == self.total_files):
                    elapsed = now - start_time
                    speed = processed / elapsed if elapsed > 0 else 0
                    if speed > 0:
                        eta = (self.total_files - processed) / speed
                        if eta > 3600: eta_str = f"{int(eta//3600)}h {int(eta%3600//60)}m"
                        elif eta > 60: eta_str = f"{int(eta//60)}m {int(eta%60)}s"
                        else: eta_str = f"{int(eta)}s"
                    else: eta_str = "..."
                    msg = f"{src_file.name} | 剩余: {eta_str}"
                    self._progress(processed, self.total_files, msg)
                    last_update = now

        # 最终保存进度（标记为完成，删除进度文件）
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
        self._progress(self.total_files, self.total_files, "完成")
        
        # 同步完成后删除进度文件
        if self.progress_file and os.path.exists(self.progress_file):
            try:
                os.remove(self.progress_file)
            except Exception:
                pass
        return self.stats["errors"] == 0

    def load_progress(self, progress_file):
        """从进度文件恢复进度"""
        try:
            with open(progress_file, "r", encoding="utf-8") as f:
                data = json.load(f)
            self.processed_files = set(data.get("processed_files", []))
            self.stats = data.get("stats", self.stats)
            self.current_index = data.get("current_index", 0)
            self.total_files = data.get("total_files", 0)
            return True
        except Exception as e:
            print(f"恢复进度失败: {e}")
            return False


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
        self.root.title("文件镜像同步工具")
        self.root.geometry("1050x720")
        self.root.minsize(900, 600)
        self._center_window()

        self.cfg = load_config()
        self.sync_thread = None
        self.sync_obj = None
        self.cancelled_flag = False
        self.is_admin = is_admin()
        self.current_scheme_id = None
        self._last_saved_log_idx = "1.0"
        self.font_size = self.cfg.get("font_size", 10)  # 字体大小
        self._migrate_old_schemes()  # 兼容旧版：把分散的方案文件合并进 schemes.json
        
        # 初始化 Mix 相关属性（避免 _on_sync_type_change 访问时报错）
        self.filter_frame = None
        self._mix_diagram_canvas = None

        self._create_ui()

        if not self.is_admin:
            self._request_elevation()
        
        # 启动时检查是否有未完成的进度
        self._check_resume_progress()

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

    # ── 字体工具 ────────────────────────────────────────────────────────

    def _font(self, size=None, bold=False):
        """返回指定大小的字体元组"""
        fz = size or self.font_size
        weight = "bold" if bold else "normal"
        return ("Microsoft YaHei UI", fz, weight)

    def _font_mono(self, size=None):
        """等宽字体（日志区域）"""
        fz = size or self.font_size
        return ("Consolas", fz)

    # ── 构建 UI ────────────────────────────────────────────────────────────

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
        paned.add(left_fr, weight=1)

        # 方案列表容器
        list_fr = ttk.LabelFrame(left_fr, text="已保存方案", padding="8")
        list_fr.pack(fill=tk.BOTH, expand=True, padx=5, pady=5)
        list_fr.rowconfigure(0, weight=1)
        list_fr.columnconfigure(0, weight=1)
        # 让左侧 frame 宽度可随 paned sash 拉伸（配合 weight=1）
        left_fr.columnconfigure(0, weight=1)
        left_fr.rowconfigure(0, weight=1)

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

        # 右键菜单（增加重命名、修改执行时间）
        self.scheme_menu = Menu(self.root, tearoff=0)
        self.scheme_menu.add_command(label="载入方案", command=self._load_scheme_from_list)
        self.scheme_menu.add_command(label="再次同步", command=self._run_scheme_from_list)
        self.scheme_menu.add_separator()
        self.scheme_menu.add_command(label="重命名", command=self._rename_scheme)
        self.scheme_menu.add_command(label="修改执行时间", command=self._edit_scheme_runtime)
        self.scheme_menu.add_separator()
        self.scheme_menu.add_command(label="删除方案", command=self._delete_scheme)
        self.scheme_tree.bind("<Double-1>", self._load_scheme_from_list)  # 双击载入方案
        self.scheme_tree.bind("<Button-3>", self._show_scheme_menu)
        self.scheme_tree.bind("<<TreeviewSelect>>", self._on_scheme_select)

        # 右侧主内容
        right_fr = ttk.Frame(paned)
        paned.add(right_fr, weight=1)
        right_fr.columnconfigure(0, weight=1)
        right_fr.rowconfigure(7, weight=1)

        self._build_right_panel(right_fr)
        self._refresh_scheme_list()
        # 从配置中恢复最后选中的方案（如果有）
        last_sid = self.cfg.get("last_scheme_id")
        if last_sid:
            self.current_scheme_id = last_sid
            children = self.scheme_tree.get_children()
            if last_sid in children:
                self.scheme_tree.selection_set(last_sid)
        
        # 加载上次日志
        self._load_last_log()

    def _create_custom_titlebar(self):
        """自定义标题栏：右上角帮助按钮"""
        titlebar = tk.Frame(self.root, bg="#f0f0f0", height=30)
        titlebar.pack(fill=tk.X)
        titlebar.pack_propagate(False)

        tk.Label(titlebar, text="", bg="#f0f0f0", width=30).pack(side=tk.LEFT)

        help_btn = tk.Label(titlebar, text="帮助  ▼", bg="#f0f0f0", fg="#333", font=self._font(9), cursor="hand2")
        help_btn.pack(side=tk.RIGHT, padx=(0, 5))
        help_btn.bind("<Button-1>", self._show_help_menu)

        self.help_menu = Menu(self.root, tearoff=0)
        self.help_menu.add_command(label="检查更新", command=self._check_update)
        self.help_menu.add_command(label="Bug 反馈", command=self._show_bug_report)
        self.help_menu.add_separator()
        self.help_menu.add_command(label="关于", command=self._show_about)

    def _show_help_menu(self, event=None):
        self.help_menu.tk_popup(event.widget.winfo_rootx(), event.widget.winfo_rooty() + event.widget.winfo_height())

    def _build_right_panel(self, parent):
        parent.columnconfigure(0, weight=1)

        # 源/目标文件夹（增加互换按钮）
        folder_fr = ttk.Frame(parent)
        folder_fr.grid(row=0, column=0, sticky="we", pady=(0, 8))
        folder_fr.columnconfigure(0, weight=1)
        folder_fr.columnconfigure(1, weight=0)  # 互换按钮列
        folder_fr.columnconfigure(2, weight=1)

        left_fr = ttk.LabelFrame(folder_fr, text="源文件夹：", padding="5")
        left_fr.grid(row=0, column=0, sticky="we", padx=(0, 5))
        left_fr.columnconfigure(0, weight=1)
        self.src_var = tk.StringVar(value=self.cfg.get("last_src", ""))
        ttk.Entry(left_fr, textvariable=self.src_var, font=self._font()).grid(row=0, column=0, sticky="we", padx=(0, 5))
        ttk.Button(left_fr, text="浏览...", command=self._browse_src, width=8).grid(row=0, column=1)

        # 互换按钮
        swap_btn = ttk.Button(folder_fr, text="⇄", command=self._swap_folders, width=3)
        swap_btn.grid(row=0, column=1, padx=5)
        swap_btn.bind("<Enter>", lambda e: swap_btn.config(text="⇄ 互换"))
        swap_btn.bind("<Leave>", lambda e: swap_btn.config(text="⇄"))

        right_fr = ttk.LabelFrame(folder_fr, text="目标文件夹：", padding="5")
        right_fr.grid(row=0, column=2, sticky="we")
        right_fr.columnconfigure(0, weight=1)
        self.dst_var = tk.StringVar(value=self.cfg.get("last_dst", ""))
        ttk.Entry(right_fr, textvariable=self.dst_var, font=self._font()).grid(row=0, column=0, sticky="we", padx=(0, 5))
        ttk.Button(right_fr, text="浏览...", command=self._browse_dst, width=8).grid(row=0, column=1)

        # 同步方式（卡片式设计）
        type_fr = ttk.LabelFrame(parent, text="同步方式", padding="8")
        type_fr.grid(row=1, column=0, sticky="we", pady=(0, 6))
        type_fr.columnconfigure(0, weight=1)
        type_fr.columnconfigure(1, weight=1)
        type_fr.columnconfigure(2, weight=1)

        self.sync_type_var = tk.StringVar()
        last_st = self.cfg.get("sync_type", "symlink")

        # 三个选项卡片
        self._card_frames = {}  # 存储卡片引用，用于高亮选中状态
        
        # Link 卡片：三条虚线（代表软连接）
        self._card_frames["symlink"] = self._create_sync_card(
            type_fr, 0, "symlink", "🔗", "Link（链接）",
            "目标文件夹生成源文件夹的软链接",
            ["dashed", "dashed", "dashed"]
        )
        
        # Mix 卡片：上中下三条线（虚-实-虚），选中后被过滤条件替换
        self._card_frames["mix"] = self._create_sync_card(
            type_fr, 1, "mix", "⚡", "Mix（混合）",
            "符合条件的文件复制，其他生成软链接",
            ["dashed", "solid", "dashed"]
        )
        
        # Mirror 卡片：三条实线（代表复制）
        self._card_frames["mirror"] = self._create_sync_card(
            type_fr, 2, "mirror", "💾", "Mirror（镜像）",
            "将源文件夹复制到目标文件夹",
            ["solid", "solid", "solid"]
        )

        if last_st in ("symlink", "mirror", "mix"):
            self.sync_type_var.set(last_st)
        else:
            self.sync_type_var.set("symlink")
        
        # 初始高亮 + 同步切换（让 mix 选中时正确隐藏示意图、显示过滤条件）
        self._update_card_highlight()
        self._on_sync_type_change()

        # 按钮区域（在同步方式下方，同步进度上方）
        btn_fr = ttk.Frame(parent)
        btn_fr.grid(row=2, column=0, sticky="we", pady=(0, 6))

        self.start_btn = ttk.Button(btn_fr, text="开始执行", command=self._start_sync, width=14)
        self.start_btn.pack(side=tk.LEFT, padx=(0, 8))

        self.cancel_btn = ttk.Button(btn_fr, text="取消", command=self._cancel_sync, state=tk.DISABLED, width=12)
        self.cancel_btn.pack(side=tk.LEFT, padx=(0, 8))

        save_btn = ttk.Button(btn_fr, text="保存方案", command=self._save_scheme, width=12)
        save_btn.pack(side=tk.LEFT, padx=(0, 8))

        self.clear_btn = ttk.Button(btn_fr, text="清空日志", command=self._clear_log, width=12)
        self.clear_btn.pack(side=tk.LEFT, padx=(0, 8))

        info_color = "green" if self.is_admin else "red"
        info_text = "✓ 管理员权限已获取" if self.is_admin else "✗ 需要管理员权限"
        ttk.Label(btn_fr, text=info_text, font=self._font(9, True), foreground=info_color).pack(side=tk.RIGHT)

        # 进度条
        prog_fr = ttk.LabelFrame(parent, text="同步进度", padding="8")
        prog_fr.grid(row=3, column=0, sticky="we", pady=(6, 6))

        self.progress_var = tk.DoubleVar()
        self.progress_bar = ttk.Progressbar(prog_fr, variable=self.progress_var, maximum=100, mode="determinate")
        self.progress_bar.pack(fill=tk.X, pady=(0, 4))

        self.status_var = tk.StringVar(value="就绪 - 请选择源和目标文件夹")
        ttk.Label(prog_fr, textvariable=self.status_var, font=self._font(9)).pack(fill=tk.X)

        # 日志区域
        log_fr = ttk.LabelFrame(parent, text="执行日志", padding="8")
        log_fr.grid(row=7, column=0, sticky="nswe")
        log_fr.columnconfigure(0, weight=1)
        log_fr.rowconfigure(0, weight=1)

        self.log_text = tk.Text(log_fr, font=self._font(), wrap=tk.WORD, state=tk.DISABLED, height=8)
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

    def _migrate_old_schemes(self):
        """兼容旧版：把分散在 config 目录下的独立方案 json 合并到 schemes.json"""
        try:
            d = SCHEME_FILE.parent
            if not d.exists():
                return
            old_files = [f for f in d.glob("*.json")
                         if f.name not in ("file_mirror_config.json",
                                           "sync_progress.json",
                                           "schemes.json")]
            if not old_files:
                return
            existing = load_schemes()
            seen = {s.get("name") for s in existing}
            for f in old_files:
                data = load_scheme_file(f)
                if not data or "src" not in data:
                    continue
                if data.get("name") in seen:
                    continue
                data["id"] = uuid.uuid4().hex
                data.setdefault("last_run_time", None)
                existing.append(data)
                seen.add(data.get("name"))
            save_schemes(existing)
            # 迁移完成后删除旧文件
            for f in old_files:
                try:
                    f.unlink()
                except Exception:
                    pass
        except Exception as e:
            print(f"迁移旧方案失败: {e}")

    def _refresh_scheme_list(self):
        self.scheme_tree.delete(*self.scheme_tree.get_children())
        for s in sorted(load_schemes(), key=lambda x: x.get("name", "")):
            sid = s.get("id", "")
            name = s.get("name", sid)
            last_run = s.get("last_run_time") or "-"
            if last_run and last_run != "-":
                try: last_run = str(last_run)[:10]   # 只显示日期 YYYY-MM-DD
                except: pass
            self.scheme_tree.insert("", tk.END, iid=sid, values=(name, last_run))

    def _on_scheme_select(self, event=None):
        # 仅当用户手动点击时更新 current_scheme_id
        sel = self._get_selected_scheme_path()
        if sel:
            self.current_scheme_id = str(sel)

    def _get_selected_scheme_path(self):
        sel = self.scheme_tree.selection()
        return sel[0] if sel else None

    def _show_scheme_menu(self, event):
        item = self.scheme_tree.identify_row(event.y)
        if item:
            self.scheme_tree.selection_set(item)
            self.scheme_menu.tk_popup(event.x_root, event.y_root)

    def _load_scheme_from_list(self):
        sid = self._get_selected_scheme_path()
        if not sid:
            messagebox.showwarning("提示", "请先选择一个方案")
            return
        data = find_scheme(sid)
        if not data:
            messagebox.showerror("载入失败", "无法读取方案")
            return
        self._apply_scheme_data(data, sid)

    def _run_scheme_from_list(self):
        sid = self._get_selected_scheme_path()
        if not sid:
            messagebox.showwarning("提示", "请先选择一个方案")
            return
        data = find_scheme(sid)
        if not data:
            messagebox.showerror("载入失败", "无法读取方案")
            return
        self._apply_scheme_data(data, sid)
        self.current_scheme_id = sid  # 记录当前方案 id，同步完成后才能更新执行时间
        self._start_sync()

    def _delete_scheme(self):
        sid = self._get_selected_scheme_path()
        if not sid:
            messagebox.showwarning("提示", "请先选择一个方案")
            return
        schemes = load_schemes()
        target = next((s for s in schemes if s.get("id") == sid), None)
        if not target:
            return
        name = target.get("name", sid)
        if messagebox.askyesno("确认删除", f"确定要删除方案「{name}」吗？"):
            schemes = [s for s in schemes if s.get("id") != sid]
            save_schemes(schemes)
            if self.current_scheme_id == sid:
                self.current_scheme_id = None
            self._refresh_scheme_list()
            messagebox.showinfo("删除成功", f"方案「{name}」已删除")

    def _rename_scheme(self):
        """重命名方案"""
        sid = self._get_selected_scheme_path()
        if not sid:
            messagebox.showwarning("提示", "请先选择一个方案")
            return
        schemes = load_schemes()
        target = next((s for s in schemes if s.get("id") == sid), None)
        if not target:
            return
        old_name = target.get("name", sid)
        new_name = simpledialog.askstring("重命名方案", f"请输入新的方案名称：\n\n当前名称：{old_name}", initialvalue=old_name, parent=self.root)
        if not new_name or new_name == old_name:
            return
        safe_name = re.sub(r'[<>:"/\\|?*]', '_', new_name.strip())
        if not safe_name:
            messagebox.showerror("错误", "方案名称无效")
            return
        if any(s.get("name") == new_name and s.get("id") != sid for s in schemes):
            messagebox.showerror("错误", f"方案「{safe_name}」已存在")
            return
        try:
            target["name"] = new_name
            save_schemes(schemes)
            self.current_scheme_id = sid
            self._refresh_scheme_list()
            self.scheme_tree.selection_set(sid)
            messagebox.showinfo("重命名成功", f"方案已重命名为「{new_name}」")
        except Exception as e:
            messagebox.showerror("重命名失败", f"无法重命名方案：\n{e}")

    def _edit_scheme_runtime(self):
        """手动修改方案最后执行时间"""
        sid = self._get_selected_scheme_path()
        if not sid:
            messagebox.showwarning("提示", "请先选择一个方案")
            return
        schemes = load_schemes()
        target = next((s for s in schemes if s.get("id") == sid), None)
        if not target:
            messagebox.showerror("错误", "无法读取方案文件")
            return
        
        current_time = target.get("last_run_time") or time.strftime('%Y-%m-%d %H:%M:%S')
        
        new_time = simpledialog.askstring(
            "修改执行时间", 
            "请输入最后执行时间：\n\n格式：YYYY-MM-DD HH:MM:SS\n留空则清除",
            initialvalue=current_time,
            parent=self.root
        )
        
        if new_time is None:  # 用户取消
            return
        
        if new_time.strip():
            try:
                time.strptime(new_time.strip(), '%Y-%m-%d %H:%M:%S')
            except ValueError:
                messagebox.showerror("错误", "时间格式不正确\n请使用：YYYY-MM-DD HH:MM:SS")
                return
        
        target["last_run_time"] = new_time.strip() or None
        try:
            save_schemes(schemes)
            self._refresh_scheme_list()
            messagebox.showinfo("修改成功", "方案执行时间已更新")
        except Exception as e:
            messagebox.showerror("修改失败", f"无法保存方案：\n{e}")

    def _apply_scheme_data(self, data, sid):
        """应用方案数据到 UI（sid 为方案唯一 id）"""
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
                min_raw = rvalue.get("min_raw")
                max_raw = rvalue.get("max_raw")
                unit = rvalue.get("unit", "KB").upper()
                self.size_unit_var.set(unit)
                self.size_min_entry.delete(0, tk.END)
                self.size_min_entry.config(foreground="black")
                self.size_min_entry._has_placeholder = False
                self.size_max_entry.delete(0, tk.END)
                self.size_max_entry.config(foreground="black")
                self.size_max_entry._has_placeholder = False
                if min_raw is not None:
                    self.size_min_var.set(min_raw)
                if max_raw is not None:
                    self.size_max_var.set(max_raw)

        self._on_sync_type_change()
        self.current_scheme_id = str(sid)
        self._log(f"[INFO] 已载入方案：{data.get('name', sid)}")

        # 自动保存当前方案 id 到配置，确保同步完成后能更新时间
        self.cfg["last_scheme_id"] = str(sid)
        save_config(self.cfg)

    # ── 规则收集 ─────────────────────────────────────────────────────

    def _collect_rules(self):
        rules = []
        logic_op = self.logic_op_var.get().strip().lower()

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

        ph_min = getattr(self, '_size_min_placeholder', '')
        ph_max = getattr(self, '_size_max_placeholder', '')
        size_min_val = self.size_min_var.get().strip()
        size_max_val = self.size_max_var.get().strip()
        has_size_rule = False
        size_data = {}

        if size_min_val and size_min_val != ph_min:
            size_data["min_raw"] = size_min_val
            has_size_rule = True

        if size_max_val and size_max_val != ph_max:
            size_data["max_raw"] = size_max_val
            has_size_rule = True

        if has_size_rule:
            size_data["unit"] = self.size_unit_var.get().upper()
            rules.append({"type": "size_range", "value": size_data, "logic": logic_op})

        return rules

    # ── 同步类型切换 ───────────────────────────────────────────────────

    def _create_sync_card(self, parent, column, val, icon, title, desc, line_types):
        """创建同步方式卡片（使用 Canvas 绘制图形）
        line_types: 列表，每个元素是 'dashed' 或 'solid'，表示每条线的样式
        """
        # 使用 tk.Frame 实现边框效果
        card = tk.Frame(parent, bg="white", highlightbackground="#cccccc", highlightthickness=1, padx=8, pady=8)
        card.grid(row=0, column=column, padx=4, sticky="nsew")
        
        # 单选按钮 + 图标 + 标题（一行）
        header_fr = tk.Frame(card, bg="white")
        header_fr.pack(fill=tk.X)
        
        rb = tk.Radiobutton(header_fr, text="", value=val, variable=self.sync_type_var,
                           command=self._on_sync_type_change, bg="white")
        rb.pack(side=tk.LEFT)
        
        icon_lbl = tk.Label(header_fr, text=icon, font=self._font(14), bg="white")
        icon_lbl.pack(side=tk.LEFT, padx=(2, 4))
        
        title_lbl = tk.Label(header_fr, text=title, font=self._font(13, True), bg="white")
        title_lbl.pack(side=tk.LEFT)
        
        # 说明文字
        desc_lbl = tk.Label(card, text=desc, font=self._font(9), fg="gray", bg="white", wraplength=220, justify="left")
        desc_lbl.pack(anchor="w", pady=(4, 0))
        
        # 使用 Canvas 绘制示意图
        canvas = tk.Canvas(card, width=220, height=80, bg="white", highlightthickness=0)
        canvas.pack(anchor="w", pady=(8, 0))
        
        # 左右矩形（源、目）—— 加宽到 30px，文字移到矩形下方
        # 左矩形（源）：x=18~48
        canvas.create_rectangle(18, 12, 48, 52, outline="#0078d4", width=1.5, fill="#eaf4fc")
        canvas.create_text(33, 66, text="源文件夹", font=self._font(9), fill="#0078d4")
        
        # 右矩形（目）：x=172~202
        canvas.create_rectangle(172, 12, 202, 52, outline="#0078d4", width=1.5, fill="#eaf4fc")
        canvas.create_text(187, 66, text="目标文件夹", font=self._font(9), fill="#0078d4")
        
        # 绘制三条线和箭头
        y_positions = [20, 32, 44]  # 三条线的 y 坐标（均匀分布在矩形内）
        for i, line_type in enumerate(line_types):
            y = y_positions[i]
            dash = (5, 3) if line_type == "dashed" else None
            # 画线（从源矩形右边缘 48 到目矩形左边缘 172）
            canvas.create_line(50, y, 162, y, fill="#333333", width=1.8, dash=dash)
            # 画箭头（三角形，紧贴目矩形左边）
            canvas.create_polygon(162, y-4, 162, y+4, 170, y, fill="#333333", outline="#333333")
        
        # Mix 卡片：保存 canvas 引用
        if val == "mix":
            self._mix_diagram_canvas = canvas
        
        # 点击整卡片均可选中
        for w in (card, icon_lbl, title_lbl, desc_lbl, canvas):
            w.bind('<Button-1>', lambda e, v=val: (self.sync_type_var.set(v), self._on_sync_type_change()))
        
        # Mix 卡片：添加过滤条件区域（初始隐藏）
        if val == "mix":
            filter_fr = tk.Frame(card, bg="white")
            # 不立即 pack，由 _on_sync_type_change 控制显示
            
            # 文件大小行
            size_row = tk.Frame(filter_fr, bg="white")
            size_row.pack(fill=tk.X, pady=(6, 0))
            tk.Label(size_row, text="文件大小：", font=self._font(9), bg="white").pack(side=tk.LEFT)
            
            self.size_min_var = tk.StringVar()
            self.size_min_entry = tk.Entry(size_row, textvariable=self.size_min_var, font=self._font(9), width=6, relief="solid", bd=1)
            self.size_min_entry.pack(side=tk.LEFT, padx=(2, 2))
            self._size_min_placeholder = "最小"
            self._insert_placeholder(self.size_min_entry, self._size_min_placeholder)
            self.size_min_entry.bind('<FocusIn>', lambda e: self._clear_placeholder(self.size_min_entry, self._size_min_placeholder))
            self.size_min_entry.bind('<FocusOut>', lambda e: self._restore_placeholder(self.size_min_entry, self._size_min_placeholder))
            
            tk.Label(size_row, text="—", font=self._font(9), bg="white").pack(side=tk.LEFT)
            
            self.size_max_var = tk.StringVar()
            self.size_max_entry = tk.Entry(size_row, textvariable=self.size_max_var, font=self._font(9), width=6, relief="solid", bd=1)
            self.size_max_entry.pack(side=tk.LEFT, padx=(2, 2))
            self._size_max_placeholder = "最大"
            self._insert_placeholder(self.size_max_entry, self._size_max_placeholder)
            self.size_max_entry.bind('<FocusIn>', lambda e: self._clear_placeholder(self.size_max_entry, self._size_max_placeholder))
            self.size_max_entry.bind('<FocusOut>', lambda e: self._restore_placeholder(self.size_max_entry, self._size_max_placeholder))
            
            self.size_unit_var = tk.StringVar(value="KB")
            unit_combo = ttk.Combobox(size_row, textvariable=self.size_unit_var, values=["B", "KB", "MB", "GB"], 
                                     state="readonly", font=self._font(9), width=4)
            unit_combo.pack(side=tk.LEFT, padx=(2, 0))
            
            # 逻辑和类型行
            logic_row = tk.Frame(filter_fr, bg="white")
            logic_row.pack(fill=tk.X, pady=(4, 0))
            
            self.logic_op_var = tk.StringVar(value="AND")
            logic_combo = ttk.Combobox(logic_row, textvariable=self.logic_op_var, values=["AND", "OR", "NOT"],
                                      state="readonly", font=self._font(9), width=5)
            logic_combo.pack(side=tk.LEFT)
            
            tk.Label(logic_row, text="  类型：", font=self._font(9), bg="white").pack(side=tk.LEFT)
            
            self.ext_var = tk.StringVar()
            self.ext_entry = tk.Entry(logic_row, textvariable=self.ext_var, font=self._font(9), width=15, relief="solid", bd=1)
            self.ext_entry.pack(side=tk.LEFT, padx=(2, 0))
            self._ext_placeholder = ".txt .doc 等"
            self._insert_placeholder(self.ext_entry, self._ext_placeholder)
            self.ext_entry.bind('<FocusIn>', lambda e: self._clear_placeholder(self.ext_entry, self._ext_placeholder))
            self.ext_entry.bind('<FocusOut>', lambda e: self._restore_placeholder(self.ext_entry, self._ext_placeholder))
            
            self.filter_frame = filter_fr  # 保存引用
            self.filter_frame.pack_forget()  # 初始隐藏，由 _on_sync_type_change 控制
        
        return card

    def _update_card_highlight(self):
        """更新卡片选中状态的高亮边框"""
        selected = self.sync_type_var.get()
        for val, card in self._card_frames.items():
            if val == selected:
                card.config(highlightbackground="#0078d4", highlightthickness=2)  # 蓝色高亮
            else:
                card.config(highlightbackground="#cccccc", highlightthickness=1)  # 灰色边框

    def _on_sync_type_change(self):
        sync_type = self.sync_type_var.get()
        # 更新卡片高亮
        self._update_card_highlight()
        # Mix 选中时：隐藏示意图，显示过滤条件；否则反之
        if sync_type == "mix":
            if getattr(self, '_mix_diagram_canvas', None):
                self._mix_diagram_canvas.pack_forget()
            if getattr(self, 'filter_frame', None):
                self.filter_frame.pack(fill=tk.X, pady=(6, 0))
        else:
            if getattr(self, 'filter_frame', None):
                self.filter_frame.pack_forget()
            if getattr(self, '_mix_diagram_canvas', None):
                self._mix_diagram_canvas.pack(anchor="w", pady=(6, 0))

    # ── 文件浏览 ───────────────────────────────────────────────────────

    def _browse_src(self):
        p = filedialog.askdirectory(title="选择源文件夹", initialdir=self.src_var.get() or None)
        if p: self.src_var.set(p.replace('/', '\\'))

    def _browse_dst(self):
        p = filedialog.askdirectory(title="选择目标文件夹", initialdir=self.dst_var.get() or None)
        if p: self.dst_var.set(p.replace('/', '\\'))

    # ── 文件夹互换 ─────────────────────────────────────────────────────

    def _swap_folders(self):
        """交换源文件夹和目标文件夹"""
        src = self.src_var.get()
        dst = self.dst_var.get()
        self.src_var.set(dst)
        self.dst_var.set(src)
        self._log("[INFO] 已交换源文件夹和目标文件夹")

    # ── 方案保存 ─────────────────────────────────────────────────────

    def _save_scheme(self):
        scheme_name = simpledialog.askstring("保存方案", "请输入方案名称：", parent=self.root)
        if not scheme_name: return

        safe_name = re.sub(r'[<>:"/\\|?*]', '_', scheme_name.strip())
        if not safe_name:
            messagebox.showerror("错误", "方案名称无效")
            return

        schemes = load_schemes()
        # 同名方案视为覆盖更新，否则新建
        existing = next((s for s in schemes if s.get("name") == scheme_name), None)
        if existing is None and any(s.get("name") == scheme_name for s in schemes):
            if not messagebox.askyesno("确认覆盖", f"方案「{scheme_name}」已存在，是否覆盖？"):
                return

        # 同名方案视为覆盖更新，否则新建
        existing = next((s for s in schemes if s.get("name") == scheme_name), None)
        if existing is not None:
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

        if existing is not None:
            scheme_data["id"] = existing.get("id") or uuid.uuid4().hex
            scheme_data["created_time"] = existing.get("created_time", scheme_data["created_time"])
            scheme_data["last_run_time"] = existing.get("last_run_time")
            for i, s in enumerate(schemes):
                if s.get("name") == scheme_name:
                    schemes[i] = scheme_data
                    break
            new_id = scheme_data["id"]
        else:
            new_id = uuid.uuid4().hex
            scheme_data["id"] = new_id
            schemes.append(scheme_data)

        try:
            save_schemes(schemes)
            self.current_scheme_id = new_id
            messagebox.showinfo("保存成功", f"方案「{scheme_name}」已保存")
            self._refresh_scheme_list()
            self.scheme_tree.selection_set(new_id)
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
        self._last_saved_log_idx = "1.0"

    def _update_progress(self, current, total, msg):
        pct = (current / total * 100) if total > 0 else 0
        self.progress_var.set(pct)
        self.status_var.set(f"{current}/{total} ({pct:.1f}%) | {msg}")

    # ── 日志自动保存 ─────────────────────────────────────────────────

    def _save_log_to_file(self, scheme_name=None):
        """保存日志到单一文件（增量追加，避免重复）"""
        try:
            start = getattr(self, "_last_saved_log_idx", "1.0")
            log_content = self.log_text.get(start, tk.END).strip()
            if not log_content:
                return
            timestamp = time.strftime('%Y-%m-%d %H:%M:%S')
            header = (f"\n{'='*60}\n[{timestamp}]"
                      + (f" 方案：{scheme_name}" if scheme_name else "")
                      + f"\n{'='*60}\n")
            LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
            with open(LOG_FILE, "a", encoding="utf-8") as f:
                f.write(header + log_content + "\n")
            self._last_saved_log_idx = self.log_text.index(tk.END)
        except Exception as e:
            print(f"保存日志失败: {e}")

    def _load_last_log(self):
        """加载历史日志文件内容到日志区域"""
        try:
            if not LOG_FILE.exists():
                return
            with open(LOG_FILE, "r", encoding="utf-8") as f:
                content = f.read()
            if not content.strip():
                return
            self.log_text.configure(state=tk.NORMAL)
            self.log_text.insert(1.0, f"[INFO] 上次日志（{LOG_FILE.name}）：\n")
            self.log_text.insert(tk.END, content + "\n")
            self.log_text.see(tk.END)
            self.log_text.configure(state=tk.DISABLED)
            self._last_saved_log_idx = self.log_text.index(tk.END)
            self.log_text.configure(state=tk.DISABLED)
        except Exception as e:
            print(f"加载上次日志失败: {e}")

    # ── 进度保存与恢复 ─────────────────────────────────────────────

    def _check_resume_progress(self):
        """启动时检查是否有未完成的同步进度"""
        if PROGRESS_FILE.exists():
            try:
                with open(PROGRESS_FILE, "r", encoding="utf-8") as f:
                    data = json.load(f)
                src = data.get("src", "")
                dst = data.get("dst", "")
                save_time = data.get("save_time", "")
                processed = len(data.get("processed_files", []))
                total = data.get("total_files", 0)
                
                if messagebox.askyesno("发现未完成的同步",
                    f"发现未完成的同步任务：\n\n源：{src}\n目标：{dst}\n"
                    f"进度：{processed}/{total} 个文件\n保存时间：{save_time}\n\n"
                    f"是否恢复并继续同步？"):
                    
                    # 自动填充路径
                    self.src_var.set(src.replace('/', '\\'))
                    self.dst_var.set(dst.replace('/', '\\'))
                    self.sync_type_var.set(data.get("sync_type", "symlink"))
                    self._on_sync_type_change()
                    
                    # 标记需要恢复进度
                    self.resume_progress_data = data
                    self._log(f"[INFO] 将恢复未完成的同步（进度：{processed}/{total}）")
                    self.status_var.set(f"可恢复同步：{processed}/{total} 个文件已处理")
            except Exception as e:
                print(f"检查进度文件失败: {e}")
                try:
                    PROGRESS_FILE.unlink()
                except Exception:
                    pass

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

        # 自动匹配方案（如果当前 id 与某个方案匹配，自动设置 current_scheme_id）
        self._auto_match_scheme(src, dst)

        type_names = {"symlink": "符号链接", "mirror": "实际复制", "mix": "混合同步"}
        
        # 检查是否需要恢复进度
        resume = hasattr(self, 'resume_progress_data') and self.resume_progress_data
        
        if resume:
            if not messagebox.askyesno("恢复同步",
                f"即将恢复同步文件：\n\n源：{src}\n目标：{dst}\n同步类型：{type_names.get(sync_type, sync_type)}\n\n是否继续？"):
                return
        else:
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
            progress_file=PROGRESS_FILE if not resume else None,  # 恢复模式不需要新的进度文件
        )

        # 如果需要恢复进度
        if resume:
            self.sync_obj.load_progress(PROGRESS_FILE)
            self.sync_obj.progress_file = PROGRESS_FILE
            delattr(self, 'resume_progress_data')

        t = Thread(target=self._run_sync, daemon=True)
        t.start()

    def _auto_match_scheme(self, src, dst):
        """自动匹配方案：仅在 current_scheme_id 为空时，如果当前路径与某个方案匹配，自动设置"""
        if self.current_scheme_id:
            return  # 已有明确选中的方案，不自动匹配
        for data in load_schemes():
            if (data.get("src", "").replace('/', '\\') == src and
                data.get("dst", "").replace('/', '\\') == dst):
                self.current_scheme_id = data.get("id")
                self._log(f"[INFO] 自动匹配到方案：{data.get('name', data.get('id', ''))}")
                return

    def _run_sync(self):
        try:
            resume = hasattr(self.sync_obj, 'processed_files') and len(self.sync_obj.processed_files) > 0
            success = self.sync_obj.run(resume=resume)
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

        # 保存日志到文件
        scheme_name = None
        if self.current_scheme_id:
            data = find_scheme(self.current_scheme_id)
            if data:
                scheme_name = data.get("name", "")

        self._save_log_to_file(scheme_name)

        if success and self.current_scheme_id:
            self._update_scheme_run_time(self.current_scheme_id)

        if error:
            messagebox.showerror("同步失败", error)
        elif success:
            messagebox.showinfo("同步完成", "文件镜像同步成功完成！")
        else:
            messagebox.showwarning("同步完成", "同步完成，但有错误发生，请查看日志")

    def _update_scheme_run_time(self, scheme_id):
        """更新方案最后执行时间（参数为方案 id）"""
        try:
            schemes = load_schemes()
            target = next((s for s in schemes if s.get("id") == scheme_id), None)
            if not target:
                self._log(f"[WARN] 无法找到方案：{scheme_id}")
                return
            now_str = time.strftime('%Y-%m-%d %H:%M:%S')
            target["last_run_time"] = now_str
            for retry in range(3):
                try:
                    save_schemes(schemes)
                    break
                except Exception as e:
                    if retry == 2:
                        self._log(f"[ERROR] 更新方案执行时间失败（已重试3次）：{e}")
                        return
                    time.sleep(0.1)
            # 只更新当前条目在列表中的 last_run 列，不整体刷新（避免触发 <<TreeviewSelect>>）
            try:
                children = self.scheme_tree.get_children()
                if scheme_id in children:
                    name = target.get('name', scheme_id)
                    last_run = now_str[:10]   # 只显示日期 YYYY-MM-DD
                    self.scheme_tree.item(scheme_id, values=(name, last_run))
            except Exception as e:
                self._log(f"[WARN] 更新执行时间显示失败: {e}")
        except Exception as e:
            self._log(f"[ERROR] 更新方案执行时间失败: {e}")

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

        ttk.Label(main, text="文件镜像同步工具", font=self._font(14, True)).pack(pady=(0, 5))
        ttk.Label(main, text=f"版本：{VERSION}", font=self._font(10)).pack()
        ttk.Label(main, text="作者：awen", font=self._font(10)).pack(pady=(0, 20))

        ttk.Button(main, text="检查更新", command=self._check_update, width=18).pack(pady=(0, 8))
        ttk.Button(main, text="Bug 反馈", command=self._show_bug_report, width=18).pack(pady=(0, 20))

        ttk.Label(main, text="GitHub：github.com/awenwa/FileMirrorTool", font=self._font(8), foreground="gray").pack()

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

        ttk.Label(main, text="姓名 / 昵称：", font=self._font(9)).grid(row=0, column=0, sticky="w", pady=(0, 4))
        self.name_var = tk.StringVar()
        ttk.Entry(main, textvariable=self.name_var, font=self._font(10)).grid(row=1, column=0, sticky="we", pady=(0, 10))

        ttk.Label(main, text="邮箱（选填）：", font=self._font(9)).grid(row=2, column=0, sticky="w", pady=(0, 4))
        self.email_var = tk.StringVar()
        ttk.Entry(main, textvariable=self.email_var, font=self._font(10)).grid(row=3, column=0, sticky="we", pady=(0, 10))

        ttk.Label(main, text="问题描述（必填）：", font=self._font(9)).grid(row=4, column=0, sticky="w", pady=(0, 4))
        self.desc_text = tk.Text(main, font=self._font(10), height=7, wrap=tk.WORD)
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
