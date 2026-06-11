#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
文件镜像同步工具 v1.1
GitHub: https://github.com/awenwa/FileMirrorTool
"""

import os
import sys
import json
import ctypes
import webbrowser
import urllib.request
import urllib.parse
from pathlib import Path
import tkinter as tk
from tkinter import ttk, filedialog, messagebox, Menu, simpledialog
from threading import Thread
import time

# ── 常量 ─────────────────────────────────────────────────────────────────

VERSION = "v1.1"
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


# ── 配置持久化（JSON 文件） ───────────────────────────────────────────────

DEFAULT_CONFIG = {
    "last_src": "",
    "last_dst": "",
    "sync_type": "all_symlink",
    "partial_rules": [],
    "schemes": [],
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


# ── 方案文件（独立 JSON，用户可选择保存位置） ────────────────────────────

def save_scheme_file(filepath, scheme_data):
    """保存方案到用户选择的路径"""
    try:
        with open(filepath, "w", encoding="utf-8") as f:
            json.dump(scheme_data, f, ensure_ascii=False, indent=2)
        return True
    except Exception as e:
        print(f"保存方案失败: {e}")
        return False


def load_scheme_file(filepath):
    """从用户选择的路径加载方案"""
    try:
        with open(filepath, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        print(f"加载方案失败: {e}")
        return None


# ── 核心同步逻辑 ──────────────────────────────────────────────────────────

class FileMirrorSync:
    SYNC_ALL_SYMLINK = "all_symlink"
    SYNC_PARTIAL    = "partial_symlink"
    SYNC_ALL_COPY   = "all_copy"

    def __init__(self, src, dst,
                 sync_type="all_symlink",
                 rules=None,
                 progress_callback=None,
                 log_callback=None,
                 cancel_callback=None):
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

    # ── 规则引擎：判断文件是否应该复制（而非符号链接） ─────────────────
    # rules 格式: [{"type": "ext", "value": ".mp4,.mkv", "logic": "or"},
    #              {"type": "size_min", "value": 100, "logic": "and"},
    #              {"type": "size_max", "value": 5000, "logic": "and"},
    #              {"type": "ext", "value": ".iso", "logic": "not"}]
    # logic: "and"=且(必须满足) / "or"=或(任一满足即复制) / "not"=非(排除)

    def _should_copy(self, filepath: Path) -> bool:
        """
        返回 True 表示该文件应该复制实际文件（而不是创建符号链接）。
        规则逻辑：
          - 所有 logic="and" 的规则必须全部满足
          - 任一 logic="or" 的规则满足即可
          - 任一 logic="not" 的规则满足则直接排除（复制）
        """
        if not self.rules or self.sync_type != self.SYNC_PARTIAL:
            return self.sync_type == self.SYNC_ALL_COPY

        and_results = []
        or_results = []
        not_results = []

        ext_lower = filepath.suffix.lower()
        try:
            size_mb = filepath.stat().st_size / (1024 * 1024)
        except Exception:
            size_mb = 0

        for rule in self.rules:
            rtype = rule.get("type", "")
            rvalue = rule.get("value", "")
            rlogic = rule.get("logic", "and")
            matched = False

            if rtype == "ext":
                exts = set(e.strip().lower() for e in str(rvalue).split(",") if e.strip())
                matched = ext_lower in exts
            elif rtype == "size_range":
                # 文件大小范围：同时检查 min 和 max
                try:
                    min_val = rvalue.get("min")
                    max_val = rvalue.get("max")
                    in_range = True
                    if min_val is not None:
                        in_range = in_range and (size_mb >= float(min_val))
                    if max_val is not None:
                        in_range = in_range and (size_mb <= float(max_val))
                    matched = in_range
                except (ValueError, TypeError, AttributeError):
                    matched = False
            # 兼容旧版规则格式
            elif rtype == "size_min":
                try:
                    matched = size_mb >= float(rvalue)
                except (ValueError, TypeError):
                    matched = False
            elif rtype == "size_max":
                try:
                    matched = size_mb <= float(rvalue)
                except (ValueError, TypeError):
                    matched = False

            if rlogic == "and":
                and_results.append(matched)
            elif rlogic == "or":
                or_results.append(matched)
            elif rlogic == "not":
                not_results.append(matched)

        # 非：任一命中则直接判定为"不复制"（即走符号链接）
        if any(not_results):
            return False

        # 或：任一命中就复制
        if any(or_results):
            return True

        # 且：全部必须命中才复制
        if and_results:
            return all(and_results)

        # 边界情况：如果只有 NOT 规则，未命中的文件应该复制（默认行为）
        if not_results:
            return True

        return False

    def _link_type_for(self, filepath: Path) -> str:
        if self._should_copy(filepath):
            return "copy"
        return "symlink"

    def _create_symlink(self, src_file: Path, dst_file: Path) -> str:
        try:
            dst_file.parent.mkdir(parents=True, exist_ok=True)
            if dst_file.exists() or dst_file.is_symlink():
                if dst_file.is_symlink():
                    try:
                        if os.readlink(str(dst_file)) == str(src_file):
                            return "skipped"
                    except Exception:
                        pass
                try:
                    (os.unlink if dst_file.is_symlink() else os.remove)(str(dst_file))
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
                return "skipped"
            import shutil
            shutil.copy2(str(src_file), str(dst_file))
            return "copy"
        except Exception:
            return "error"

    def _process_file(self, src_file: Path, dst_file: Path):
        link_type = self._link_type_for(src_file)
        if link_type == "copy":
            result = self._copy_file(src_file, dst_file)
        else:
            result = self._create_symlink(src_file, dst_file)

        if result == "symlink":
            self.stats["symlinks_created"] += 1
        elif result == "copy":
            self.stats["copies_created"] += 1
        elif result == "skipped":
            self.stats["skipped"] += 1
        else:
            self.stats["errors"] += 1

    def _cleanup(self, src_files_set):
        removed = 0
        for root, _dirs, files in os.walk(str(self.dst), topdown=False):
            for fname in files:
                if self._should_cancel():
                    return removed
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
        type_names = {"all_symlink": "全部符号链接", "partial_symlink": "部分符号链接", "all_copy": "全部实际文件"}
        self._log(f"同步类型: {type_names.get(self.sync_type, self.sync_type)}")
        if self.rules:
            self._log(f"筛选规则数: {len(self.rules)}")
        self._log("=" * 60)

        if not self.src.exists():
            self._log(f"源文件夹不存在: {self.src}", "ERROR")
            return False

        self.dst.mkdir(parents=True, exist_ok=True)

        self._log("正在统计文件总数...")
        total = 0
        for _root, _dirs, files in os.walk(self.src):
            total += len(files)
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
                if self._should_cancel():
                    break
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
                        if eta > 3600:
                            eta_str = f"{int(eta//3600)}h {int(eta%3600//60)}m"
                        elif eta > 60:
                            eta_str = f"{int(eta//60)}m {int(eta%60)}s"
                        else:
                            eta_str = f"{int(eta)}s"
                    else:
                        eta_str = "..."
                    msg = f"{rel_path.name} | 剩余: {eta_str}"
                    self._progress(processed, total, msg)
                    last_update = now

        if not self._should_cancel():
            self._log("清理目标中多余文件...")
            removed = self._cleanup(src_files_rel)
            if removed:
                self._log(f"删除了 {removed} 个多余文件")

        elapsed = time.time() - start_time
        self._log("=" * 60)
        self._log("同步完成！")
        self._log(f"目录创建: {self.stats['dirs_created']}")
        self._log(f"符号链接: {self.stats['symlinks_created']}")
        self._log(f"文件复制: {self.stats['copies_created']}")
        self._log(f"已跳过:   {self.stats['skipped']}")
        self._log(f"错误数量: {self.stats['errors']}")
        self._log(f"耗时: {elapsed:.1f} 秒")
        self._log("=" * 60)
        self._progress(total, total, "完成")
        return self.stats["errors"] == 0


# ── GUI ────────────────────────────────────────────────────────────────────

class FileMirrorApp:

    # 同步类型内部值
    SYNC_ALL_SYMLINK = "all_symlink"
    SYNC_PARTIAL = "partial_symlink"
    SYNC_ALL_COPY = "all_copy"

    # 同步类型选项定义 (标题, 描述, 内部值)
    SYNC_TYPE_OPTIONS = [
        ("全部符号链接", "在右侧建立左侧的快捷方式", "all_symlink"),
        ("全部实际文件", "将左侧文件同步到右侧", "all_copy"),
        ("条件同步", "符合下述条件的文件同步到右侧，其他文件建立快捷方式", "partial_symlink"),
    ]

    def __init__(self, root):
        self.root = root
        self.root.title(f"文件镜像同步工具 {VERSION}")
        self.root.geometry("950x750")
        self.root.minsize(800, 600)
        self._center_window()

        self.cfg = load_config()
        self.sync_thread = None
        self.sync_obj = None
        self.cancelled_flag = False
        self.is_admin = is_admin()

        self._create_ui()

        if not self.is_admin:
            self._request_elevation()

    def _center_window(self):
        self.root.update_idletasks()
        w, h = 950, 750
        x = (self.root.winfo_screenwidth() // 2) - (w // 2)
        y = (self.root.winfo_screenheight() // 2) - (h // 2)
        self.root.geometry(f"{w}x{h}+{x}+{y}")

    def _request_elevation(self):
        if run_as_admin():
            os._exit(0)
        else:
            messagebox.showerror(
                "提权失败",
                "无法自动获取管理员权限。\n\n请手动以管理员身份运行此程序：\n右键 → 以管理员身份运行"
            )

    # ── 构建 UI ─────────────────────────────────────────────────────────

    def _create_ui(self):
        self._create_menu()
        main = ttk.Frame(self.root, padding="10")
        main.pack(fill=tk.BOTH, expand=True)

        # ── 源/目标文件夹（并排）─────────────────────────────────────────
        folder_fr = ttk.Frame(main)
        folder_fr.pack(fill=tk.X, pady=(0, 8))

        # 左侧文件夹
        left_fr = ttk.LabelFrame(folder_fr, text="左侧文件夹：", padding="5")
        left_fr.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(0, 8))
        self.src_var = tk.StringVar(value=self.cfg.get("last_src", ""))
        ttk.Entry(left_fr, textvariable=self.src_var, font=("Microsoft YaHei UI", 10), width=35) \
            .pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(0, 5))
        ttk.Button(left_fr, text="浏览...", command=self._browse_src, width=8) \
            .pack(side=tk.RIGHT)

        # 右侧文件夹
        right_fr = ttk.LabelFrame(folder_fr, text="右侧文件夹：", padding="5")
        right_fr.pack(side=tk.RIGHT, fill=tk.X, expand=True)
        self.dst_var = tk.StringVar(value=self.cfg.get("last_dst", ""))
        ttk.Entry(right_fr, textvariable=self.dst_var, font=("Microsoft YaHei UI", 10), width=35) \
            .pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(0, 5))
        ttk.Button(right_fr, text="浏览...", command=self._browse_dst, width=8) \
            .pack(side=tk.RIGHT)

        # ── 同步方式（紧凑横向排列）─────────────────────────────────────
        type_fr = ttk.LabelFrame(main, text="同步方式", padding="8")
        type_fr.pack(fill=tk.X, pady=(0, 6))

        self.sync_type_var = tk.StringVar()
        last_st = self.cfg.get("sync_type", "all_symlink")

        # 选项 1：符号链接
        opt1 = ttk.Frame(type_fr)
        opt1.pack(fill=tk.X, pady=2)
        rb1 = ttk.Radiobutton(opt1, text="🔗 在右侧建立左侧的快捷方式",
                              value="all_symlink", variable=self.sync_type_var,
                              command=self._on_sync_type_change)
        rb1.pack(side=tk.LEFT)
        opt1.bind('<Button-1>', lambda e: (self.sync_type_var.set("all_symlink"), self._on_sync_type_change()))
        if last_st == "all_symlink":
            self.sync_type_var.set("all_symlink")

        # 选项 2：全部复制
        opt2 = ttk.Frame(type_fr)
        opt2.pack(fill=tk.X, pady=2)
        rb2 = ttk.Radiobutton(opt2, text="💾 将左侧文件同步到右侧",
                              value="all_copy", variable=self.sync_type_var,
                              command=self._on_sync_type_change)
        rb2.pack(side=tk.LEFT)
        opt2.bind('<Button-1>', lambda e: (self.sync_type_var.set("all_copy"), self._on_sync_type_change()))
        if last_st == "all_copy":
            self.sync_type_var.set("all_copy")

        # 选项 3：条件同步（带内嵌条件）
        opt3 = ttk.Frame(type_fr)
        opt3.pack(fill=tk.X, pady=2)
        rb3 = ttk.Radiobutton(opt3, text="⚡ 条件同步",
                              value="partial_symlink", variable=self.sync_type_var,
                              command=self._on_sync_type_change)
        rb3.pack(side=tk.LEFT)
        desc_label = ttk.Label(opt3, text="（符合下述条件的文件同步到右侧，其他文件建立快捷方式）",
                  font=("Microsoft YaHei UI", 8), foreground="gray")
        desc_label.pack(side=tk.LEFT, padx=(5, 0))
        # 绑定整个 opt3 区域的点击事件，确保点击文字也能触发选中
        def on_opt3_click(event):
            self.sync_type_var.set("partial_symlink")
            self._on_sync_type_change()
        opt3.bind('<Button-1>', on_opt3_click)
        desc_label.bind('<Button-1>', on_opt3_click)
        if last_st == "partial_symlink":
            self.sync_type_var.set("partial_symlink")

        # 条件同步的条件区域（内嵌在type_fr中，位于选项3之后）
        self.filter_frame = ttk.Frame(type_fr, padding=(20, 5, 0, 0))

        # 条件行：文件大小 [逻辑运算符] 文件类型
        cond_row = ttk.Frame(self.filter_frame)
        cond_row.pack(fill=tk.X, pady=2)

        # ── 文件大小范围 + 单位下拉框（左侧）──
        ttk.Label(cond_row, text="文件大小：", font=("Microsoft YaHei UI", 9)).pack(side=tk.LEFT)
        self.size_min_var = tk.StringVar()
        self.size_min_entry = ttk.Entry(cond_row, textvariable=self.size_min_var,
                                        font=("Microsoft YaHei UI", 9), width=8)
        self.size_min_entry.pack(side=tk.LEFT, padx=(2, 2))
        self._size_min_placeholder = "最小值"
        self._insert_placeholder(self.size_min_entry, self._size_min_placeholder)
        self.size_min_entry.bind('<FocusIn>', lambda e: self._clear_placeholder(self.size_min_entry, self._size_min_placeholder))
        self.size_min_entry.bind('<FocusOut>', lambda e: self._restore_placeholder(self.size_min_entry, self._size_min_placeholder))
        
        ttk.Label(cond_row, text="—", font=("Microsoft YaHei UI", 9)).pack(side=tk.LEFT)
        
        self.size_max_var = tk.StringVar()
        self.size_max_entry = ttk.Entry(cond_row, textvariable=self.size_max_var,
                                        font=("Microsoft YaHei UI", 9), width=8)
        self.size_max_entry.pack(side=tk.LEFT, padx=(2, 2))
        self._size_max_placeholder = "最大值"
        self._insert_placeholder(self.size_max_entry, self._size_max_placeholder)
        self.size_max_entry.bind('<FocusIn>', lambda e: self._clear_placeholder(self.size_max_entry, self._size_max_placeholder))
        self.size_max_entry.bind('<FocusOut>', lambda e: self._restore_placeholder(self.size_max_entry, self._size_max_placeholder))

        # 单位下拉框（B / KB / MB / GB）
        self.size_unit_var = tk.StringVar(value="KB")
        unit_combo = ttk.Combobox(cond_row, textvariable=self.size_unit_var,
                                  values=["B", "KB", "MB", "GB"], state="readonly",
                                  font=("Microsoft YaHei UI", 9), width=4)
        unit_combo.pack(side=tk.LEFT, padx=(2, 8))

        # ── 逻辑运算符下拉框（AND / OR / NOT）──
        self.logic_op_var = tk.StringVar(value="AND")
        logic_combo = ttk.Combobox(cond_row, textvariable=self.logic_op_var,
                                   values=["AND", "OR", "NOT"], state="readonly",
                                   font=("Microsoft YaHei UI", 9), width=5)
        logic_combo.pack(side=tk.LEFT, padx=(0, 8))

        # ── 文件类型（带 placeholder，右侧）──
        ttk.Label(cond_row, text="文件类型：", font=("Microsoft YaHei UI", 9)).pack(side=tk.LEFT)
        self.ext_var = tk.StringVar()
        self.ext_entry = ttk.Entry(cond_row, textvariable=self.ext_var,
                                   font=("Microsoft YaHei UI", 9), width=28)
        self.ext_entry.pack(side=tk.LEFT, padx=(2, 8))
        self._ext_placeholder = ".txt, .doc, .html 等，用空格分隔"
        self._insert_placeholder(self.ext_entry, self._ext_placeholder)
        self.ext_entry.bind('<FocusIn>', lambda e: self._clear_placeholder(self.ext_entry, self._ext_placeholder))
        self.ext_entry.bind('<FocusOut>', lambda e: self._restore_placeholder(self.ext_entry, self._ext_placeholder))

        # 根据当前同步类型显示/隐藏条件区域
        self._on_sync_type_change()

        # ── 方案操作 ───────────────────────────────────────────────────
        scheme_fr = ttk.Frame(main)
        scheme_fr.pack(fill=tk.X, pady=(6, 6))

        ttk.Button(scheme_fr, text="保存方案...", command=self._save_scheme, width=12) \
            .pack(side=tk.LEFT, padx=(0, 8))
        ttk.Button(scheme_fr, text="载入方案...", command=self._load_scheme, width=12) \
            .pack(side=tk.LEFT)

        # ── 进度条 ───────────────────────────────────────────────────────
        prog_fr = ttk.LabelFrame(main, text="同步进度", padding="8")
        prog_fr.pack(fill=tk.X, pady=(0, 6))

        self.progress_var = tk.DoubleVar()
        self.progress_bar = ttk.Progressbar(
            prog_fr, variable=self.progress_var, maximum=100, mode="determinate"
        )
        self.progress_bar.pack(fill=tk.X, pady=(0, 4))

        self.status_var = tk.StringVar(value="就绪 - 请选择源和目标文件夹")
        ttk.Label(prog_fr, textvariable=self.status_var, font=("Microsoft YaHei UI", 9)) \
            .pack(fill=tk.X)

        # ── 按钮区域 ────────────────────────────────────────────────────
        btn_fr = ttk.Frame(main)
        btn_fr.pack(fill=tk.X, pady=(0, 6))

        self.start_btn = ttk.Button(btn_fr, text="开始同步", command=self._start_sync, width=12)
        self.start_btn.pack(side=tk.LEFT, padx=(0, 10))

        self.cancel_btn = ttk.Button(btn_fr, text="取消", command=self._cancel_sync,
                                    state=tk.DISABLED, width=12)
        self.cancel_btn.pack(side=tk.LEFT, padx=(0, 10))

        self.clear_btn = ttk.Button(btn_fr, text="清空日志", command=self._clear_log, width=12)
        self.clear_btn.pack(side=tk.LEFT)

        info_color = "green" if self.is_admin else "red"
        info_text = "✓ 管理员权限已获取" if self.is_admin else "✗ 需要管理员权限"
        ttk.Label(btn_fr, text=info_text,
                   font=("Microsoft YaHei UI", 9, "bold"), foreground=info_color) \
            .pack(side=tk.RIGHT)

        # ── 日志区域 ────────────────────────────────────────────────────
        log_fr = ttk.LabelFrame(main, text="执行日志", padding="8")
        log_fr.pack(fill=tk.BOTH, expand=True)

        self.log_text = tk.Text(
            log_fr, font=("Consolas", 9), wrap=tk.WORD, state=tk.DISABLED, height=8
        )
        sb = ttk.Scrollbar(log_fr, orient=tk.VERTICAL, command=self.log_text.yview)
        self.log_text.configure(yscrollcommand=sb.set)
        self.log_text.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        sb.pack(side=tk.RIGHT, fill=tk.Y)

    # ── placeholder 辅助方法 ───────────────────────────────────────
    def _insert_placeholder(self, entry, text):
        """插入灰色占位文字"""
        entry.insert(0, text)
        entry.config(foreground="gray")
        entry._has_placeholder = True

    def _clear_placeholder(self, entry, text):
        """获得焦点时清除占位文字"""
        if getattr(entry, '_has_placeholder', False):
            entry.delete(0, tk.END)
            entry.config(foreground="black")
            entry._has_placeholder = False

    def _restore_placeholder(self, entry, text):
        """失去焦点且为空时恢复占位文字"""
        if not getattr(entry, '_has_placeholder', False) and not entry.get().strip():
            self._insert_placeholder(entry, text)

    def _create_menu(self):
        menubar = Menu(self.root)
        self.root.config(menu=menubar)
        help_menu = Menu(menubar, tearoff=0)
        menubar.add_cascade(label="帮助", menu=help_menu)
        help_menu.add_command(label="关于", command=self._show_about)

    # ── 规则收集 ─────────────────────────────────────────────────────

    def _collect_rules(self):
        """从 UI 收集当前所有规则（简化版：文件类型 + 文件大小）"""
        rules = []

        # 逻辑运算符（从下拉框读取）
        logic_op = self.logic_op_var.get().strip().lower()  # and / or / not

        # 文件类型规则
        ext_val = self.ext_var.get().strip()
        if ext_val and ext_val != self._ext_placeholder:
            # 解析扩展名（支持空格、逗号分隔）
            exts = []
            for e in ext_val.replace(",", " ").split():
                e = e.strip()
                if e:
                    if not e.startswith("."):
                        e = "." + e
                    exts.append(e.lower())
            if exts:
                rules.append({
                    "type": "ext",
                    "value": ",".join(exts),
                    "logic": logic_op,
                })

        # 单位换算系数（统一转为 MB）
        unit = self.size_unit_var.get().upper()
        unit_map = {"B": 1/(1024*1024), "KB": 1/1024, "MB": 1.0, "GB": 1024}
        unit_factor = unit_map.get(unit, 1/1024)

        # 文件大小范围（作为整体规则，使用同一个逻辑运算符）
        size_min_val = self.size_min_var.get().strip()
        size_max_val = self.size_max_var.get().strip()
        has_size_rule = False
        size_data = {}

        if size_min_val and size_min_val != self._size_min_placeholder:
            try:
                size_data["min"] = float(size_min_val) * unit_factor
                has_size_rule = True
            except ValueError:
                pass

        if size_max_val and size_max_val != self._size_max_placeholder:
            try:
                size_data["max"] = float(size_max_val) * unit_factor
                has_size_rule = True
            except ValueError:
                pass

        if has_size_rule:
            rules.append({
                "type": "size_range",
                "value": size_data,
                "logic": logic_op,
            })

        return rules

    # ── 同步类型切换 ───────────────────────────────────────────────────

    def _on_sync_type_change(self):
        sync_type = self.sync_type_var.get()
        if sync_type == "partial_symlink":
            # 确保filter_frame在type_fr的pack列表中
            try:
                self.filter_frame.pack(fill=tk.X, pady=(4, 0))
            except Exception as e:
                print(f"[ERROR] pack failed: {e}")
        else:
            self.filter_frame.pack_forget()

    # ── 文件浏览 ───────────────────────────────────────────────────────

    def _browse_src(self):
        p = filedialog.askdirectory(title="选择源文件夹", initialdir=self.src_var.get() or None)
        if p:
            # 统一使用 Windows 反斜杠格式
            self.src_var.set(p.replace('/', '\\'))

    def _browse_dst(self):
        p = filedialog.askdirectory(title="选择目标文件夹", initialdir=self.dst_var.get() or None)
        if p:
            # 统一使用 Windows 反斜杠格式
            self.dst_var.set(p.replace('/', '\\'))

    # ── 方案保存 / 加载（用户选择路径） ───────────────────────────────

    def _save_scheme(self):
        """保存方案到用户选择的路径（默认目标文件夹）"""
        dst = self.dst_var.get().strip()
        default_dir = dst if os.path.isdir(dst) else str(Path.home())
        default_name = "file_mirror_scheme.json"

        filepath = filedialog.asksaveasfilename(
            title="保存方案",
            initialdir=default_dir,
            initialfile=default_name,
            defaultextension=".json",
            filetypes=[("JSON 方案文件", "*.json"), ("所有文件", "*.*")]
        )
        if not filepath:
            return

        scheme_data = {
            "version": VERSION,
            "src": self.src_var.get(),
            "dst": self.dst_var.get(),
            "sync_type": self.sync_type_var.get(),
            "logic_op": self.logic_op_var.get(),
            "size_unit": self.size_unit_var.get(),
            "rules": self._collect_rules(),
        }

        if save_scheme_file(filepath, scheme_data):
            # 同时记录最近使用的方案路径到配置
            self.cfg["last_scheme_path"] = filepath
            save_config(self.cfg)
            messagebox.showinfo("保存成功", f"方案已保存至：\n{filepath}")
        else:
            messagebox.showerror("保存失败", f"无法写入文件：\n{filepath}")

    def _load_scheme(self):
        """从用户选择的路径加载方案"""
        last_path = self.cfg.get("last_scheme_path", "")
        init_dir = str(Path(last_path).parent) if last_path and os.path.isfile(last_path) else str(Path.home())

        filepath = filedialog.askopenfilename(
            title="载入方案",
            initialdir=init_dir,
            filetypes=[("JSON 方案文件", "*.json"), ("所有文件", "*.*")]
        )
        if not filepath:
            return

        data = load_scheme_file(filepath)
        if not data:
            messagebox.showerror("载入失败", f"无法读取方案文件：\n{filepath}")
            return

        # 应用方案到 UI（统一使用 Windows 反斜杠格式）
        self.src_var.set(data.get("src", "").replace('/', '\\'))
        self.dst_var.set(data.get("dst", "").replace('/', '\\'))

        st = data.get("sync_type", "all_symlink")
        # 兼容：st 可能是内部值或标题
        matched = False
        for _title, _desc, val in self.SYNC_TYPE_OPTIONS:
            if st in (val, _title):
                self.sync_type_var.set(val)
                matched = True
                break
        if not matched:
            self.sync_type_var.set("all_symlink")
        
        # 触发同步类型变更，更新 UI 显示
        self._on_sync_type_change()

        # 加载逻辑运算符和单位
        self.logic_op_var.set(data.get("logic_op", "AND"))
        self.size_unit_var.set(data.get("size_unit", "KB"))

        # 加载规则到新 UI
        rules = data.get("rules", [])
        # 重置默认值
        self.ext_var.set("")
        self._insert_placeholder(self.ext_entry, self._ext_placeholder)
        self.size_min_var.set("")
        self._insert_placeholder(self.size_min_entry, self._size_min_placeholder)
        self.size_max_var.set("")
        self._insert_placeholder(self.size_max_entry, self._size_max_placeholder)

        for rule in rules:
            rtype = rule.get("type", "")
            rvalue = rule.get("value", "")
            if rtype == "ext" and rvalue:
                exts = rvalue.replace(",", " ")
                self.ext_var.set(exts)
                if getattr(self.ext_entry, '_has_placeholder', False):
                    self.ext_entry.delete(0, tk.END)
                    self.ext_entry.config(foreground="black")
                    self.ext_entry._has_placeholder = False
                self.ext_entry.insert(0, exts)
            elif rtype == "size_min" and rvalue:
                self.size_min_var.set(str(rvalue))
                if getattr(self.size_min_entry, '_has_placeholder', False):
                    self.size_min_entry.delete(0, tk.END)
                    self.size_min_entry.config(foreground="black")
                    self.size_min_entry._has_placeholder = False
            elif rtype == "size_max" and rvalue:
                self.size_max_var.set(str(rvalue))
                if getattr(self.size_max_entry, '_has_placeholder', False):
                    self.size_max_entry.delete(0, tk.END)
                    self.size_max_entry.config(foreground="black")
                    self.size_max_entry._has_placeholder = False

        self._on_sync_type_change()

        # 记录路径
        self.cfg["last_scheme_path"] = filepath
        save_config(self.cfg)

        messagebox.showinfo("载入成功", f"方案已从以下文件载入：\n{filepath}")

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

    # ── 开始同步 ───────────────────────────────────────────────────────

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
        # 如果是旧格式标题，查找内部值
        if sync_type not in ("all_symlink", "partial_symlink", "all_copy"):
            for _title, _desc, val in self.SYNC_TYPE_OPTIONS:
                if sync_type == _title:
                    sync_type = val
                    break

        if sync_type in ("all_symlink", "partial_symlink") and not self.is_admin:
            messagebox.showerror(
                "权限不足",
                "创建符号链接需要管理员权限。\n\n请右键程序 → 以管理员身份运行"
            )
            return

        # 保存配置（含路径和规则）——每次都写，确保重启后能恢复
        self.cfg["last_src"] = src
        self.cfg["last_dst"] = dst
        self.cfg["sync_type"] = sync_type
        self.cfg["partial_rules"] = self._collect_rules()
        save_config(self.cfg)

        type_names = {"all_symlink": "全部符号链接", "partial_symlink": "条件同步", "all_copy": "全部实际文件"}
        if not messagebox.askyesno("确认",
            f"即将同步文件：\n\n"
            f"源：{src}\n"
            f"目标：{dst}\n"
            f"同步类型：{type_names.get(sync_type, sync_type)}\n\n"
            f"是否继续？"):
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
            self._log(traceback.format_exc(), "ERROR")
            err_msg = str(e)
            self.root.after(0, lambda e=err_msg: self._sync_complete(False, e))

    def _sync_complete(self, success, error):
        self.start_btn.configure(state=tk.NORMAL)
        self.cancel_btn.configure(state=tk.DISABLED)
        self.progress_var.set(100 if success else 0)
        if error:
            messagebox.showerror("同步失败", error)
        elif success:
            messagebox.showinfo("同步完成", "文件镜像同步成功完成！")
        else:
            messagebox.showwarning("同步完成", "同步完成，但有错误发生，请查看日志")

    def _cancel_sync(self):
        self.cancelled_flag = True
        self._log("[WARN] 正在取消...")
        self.cancel_btn.configure(state=tk.DISABLED)

    def _show_about(self):
        dlg = AboutDialog(self.root)
        self.root.wait_window(dlg)


# ── 关于对话框 ─────────────────────────────────────────────────────────────

class AboutDialog(tk.Toplevel):
    def __init__(self, parent):
        super().__init__(parent)
        self.title("关于 - 文件镜像同步工具")
        self.geometry("400x340")
        self.resizable(False, False)
        self.transient(parent)
        self.grab_set()

        main = ttk.Frame(self, padding="20")
        main.pack(fill=tk.BOTH, expand=True)

        ttk.Label(main, text="文件镜像同步工具",
                  font=("Microsoft YaHei UI", 14, "bold")).pack(pady=(0, 5))
        ttk.Label(main, text=f"版本：{VERSION}",
                  font=("Microsoft YaHei UI", 10)).pack()
        ttk.Label(main, text="作者：QClaw",
                  font=("Microsoft YaHei UI", 10)).pack(pady=(0, 20))

        ttk.Button(main, text="检查更新", command=self._check_update, width=20) \
            .pack(pady=(0, 10))
        ttk.Button(main, text="Bug 反馈", command=self._show_bug_report, width=20) \
            .pack(pady=(0, 20))

        ttk.Label(main, text="GitHub：github.com/awenwa/FileMirrorTool",
                  font=("Microsoft YaHei UI", 8), foreground="gray").pack()
        ttk.Label(main, text=f"配置文件：{CONFIG_FILE}",
                  font=("Microsoft YaHei UI", 8), foreground="gray").pack(pady=(5, 0))

    def _check_update(self):
        try:
            req = urllib.request.Request(GITHUB_API, headers={"User-Agent": "FileMirrorTool"})
            with urllib.request.urlopen(req, timeout=8) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            latest = data.get("tag_name", "unknown")
            if latest == VERSION:
                messagebox.showinfo("检查更新", f"当前已是最新版本 {VERSION}")
            else:
                if messagebox.askyesno("检查更新",
                                        f"发现新版本：{latest}\n当前版本：{VERSION}\n\n是否打开下载页面？"):
                    webbrowser.open(data.get("html_url", "https://github.com/awenwa/FileMirrorTool/releases"))
        except Exception as e:
            messagebox.showerror("检查更新失败", f"无法连接至 GitHub API：\n{e}")

    def _show_bug_report(self):
        dlg = BugReportDialog(self)
        self.wait_window(dlg)


# ── Bug 反馈对话框 ────────────────────────────────────────────────────────

class BugReportDialog(tk.Toplevel):
    def __init__(self, parent):
        super().__init__(parent)
        self.title("Bug 反馈")
        self.geometry("440x380")
        self.transient(parent)
        self.grab_set()

        main = ttk.Frame(self, padding="15")
        main.pack(fill=tk.BOTH, expand=True)

        ttk.Label(main, text="姓名 / 昵称：", font=("Microsoft YaHei UI", 9)) \
            .grid(row=0, column=0, sticky="w", pady=(0, 4))
        self.name_var = tk.StringVar()
        ttk.Entry(main, textvariable=self.name_var, font=("Microsoft YaHei UI", 10)) \
            .grid(row=1, column=0, sticky="we", pady=(0, 10))

        ttk.Label(main, text="邮箱（选填）：", font=("Microsoft YaHei UI", 9)) \
            .grid(row=2, column=0, sticky="w", pady=(0, 4))
        self.email_var = tk.StringVar()
        ttk.Entry(main, textvariable=self.email_var, font=("Microsoft YaHei UI", 10)) \
            .grid(row=3, column=0, sticky="we", pady=(0, 10))

        ttk.Label(main, text="问题描述（必填）：", font=("Microsoft YaHei UI", 9)) \
            .grid(row=4, column=0, sticky="w", pady=(0, 4))
        self.desc_text = tk.Text(main, font=("Microsoft YaHei UI", 10), height=7, wrap=tk.WORD)
        self.desc_text.grid(row=5, column=0, sticky="wens", pady=(0, 15))

        main.columnconfigure(0, weight=1)
        main.rowconfigure(5, weight=1)

        btn_fr = ttk.Frame(main)
        btn_fr.grid(row=6, column=0, sticky="e")

        ttk.Button(btn_fr, text="发送", command=self._send, width=12) \
            .pack(side=tk.LEFT, padx=(0, 10))
        ttk.Button(btn_fr, text="取消", command=self.destroy, width=12) \
            .pack(side=tk.LEFT)

    def _send(self):
        desc = self.desc_text.get("1.0", tk.END).strip()
        if not desc:
            messagebox.showerror("错误", "请填写问题描述")
            return
        name = self.name_var.get().strip() or "匿名"
        email = self.email_var.get().strip()

        body = (
            f"姓名：{name}\n"
            f"邮箱：{email}\n"
            f"版本：{VERSION}\n"
            f"时间：{time.strftime('%Y-%m-%d %H:%M:%S')}\n"
            f"\n问题描述：\n{desc}"
        )

        try:
            subject = urllib.parse.quote(f"[Bug反馈] 文件镜像同步工具 {VERSION}")
            body_enc = urllib.parse.quote(body)
            webbrowser.open(f"mailto:{BUG_EMAIL}?subject={subject}&body={body_enc}")
            messagebox.showinfo(
                "发送反馈",
                f"已调用系统邮件客户端。\n\n如果未弹出邮件窗口，请手动发送邮件至：\n{BUG_EMAIL}"
            )
            self.clipboard_clear()
            self.clipboard_append(f"收件人：{BUG_EMAIL}\n主题：[Bug反馈] 文件镜像同步工具 {VERSION}\n\n{body}")
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
