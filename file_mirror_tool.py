#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
文件镜像同步工具 - 从网络文件夹向本地创建符号链接镜像
功能：
1. 复制完整文件夹结构到本地
2. 为所有文件创建符号链接（指向源文件，不占用实际空间）
3. 如果无法创建符号链接，自动尝试创建硬链接或复制文件
4. 显示进度条和 ETA
5. 支持中断恢复（跳过已创建的链接）
6. 错误计数和详细日志

Windows 权限说明：
- 符号链接（symlink）需要管理员权限或开发者模式
- 硬链接（hardlink）不需要管理员权限，但只能用于同一分区的文件
- 文件复制总是可用，但会占用磁盘空间
"""

import os
import sys
import ctypes
import shutil
import subprocess
import tkinter as tk
from tkinter import ttk, filedialog, messagebox
from pathlib import Path
from threading import Thread
import time


def is_admin():
    """检查是否有管理员权限"""
    try:
        return ctypes.windll.shell32.IsUserAnAdmin()
    except:
        return False


def run_as_admin():
    """以管理员权限重新运行程序（兼容 py 和 exe 模式）"""
    exe_path = os.path.abspath(sys.argv[0])
    
    try:
        # 使用 ShellExecute 提权，SW_SHOWNORMAL = 1 显示窗口
        # 打包成 exe 后 sys.executable 就是 exe 本身，无需 pythonw
        ctypes.windll.shell32.ShellExecuteW(
            None,           # hwnd
            "runas",        # operation (提权)
            exe_path,       # file (py 脚本或 exe)
            None,           # parameters
            None,           # directory
            1               # show_cmd (SW_SHOWNORMAL = 显示)
        )
        return True
    except Exception as e:
        print(f"提权失败: {e}")
        return False


class FileMirrorSync:
    """文件镜像同步核心逻辑"""
    
    # 链接类型
    LINK_SYMLINK = 'symlink'
    LINK_HARDLINK = 'hardlink'
    LINK_JUNCTION = 'junction'
    LINK_COPY = 'copy'
    
    def __init__(self, src: str, dst: str, link_type: str = None, 
                 progress_callback=None, log_callback=None):
        self.src = Path(src)
        self.dst = Path(dst)
        self.progress_callback = progress_callback
        self.log_callback = log_callback
        self.cancelled = False
        self.link_type = link_type  # None=自动选择
        self.stats = {
            'dirs_created': 0,
            'symlinks_created': 0,
            'hardlinks_created': 0,
            'junctions_created': 0,
            'copies_created': 0,
            'skipped': 0,
            'errors': 0
        }
        self._detected_link_type = None
    
    def log(self, message: str, level: str = 'INFO'):
        """日志输出"""
        if self.log_callback:
            self.log_callback(f"[{level}] {message}")
    
    def update_progress(self, current: int, total: int, message: str = ""):
        """更新进度"""
        if self.progress_callback:
            self.progress_callback(current, total, message)
    
    def scan_files(self):
        """扫描源文件夹，返回所有文件列表"""
        files = []
        self.log(f"正在扫描源文件夹: {self.src}")
        
        try:
            for root, dirs, filenames in os.walk(self.src):
                for filename in filenames:
                    if self.cancelled:
                        break
                    src_file = Path(root) / filename
                    files.append(src_file)
        except Exception as e:
            self.log(f"扫描文件夹失败: {e}", 'ERROR')
            return []
        
        self.log(f"扫描完成，共发现 {len(files)} 个文件")
        return files
    
    def detect_best_link_type(self):
        """检测最佳的链接类型"""
        # 1. 尝试创建符号链接（需要管理员权限或开发者模式）
        test_file = self.dst / ".symlink_test"
        try:
            self.dst.mkdir(parents=True, exist_ok=True)
            with open(self.src / ".test_src", 'w') as f:
                f.write("test")
            os.symlink(str(self.src / ".test_src"), str(test_file))
            os.unlink(str(test_file))
            os.remove(str(self.src / ".test_src"))
            self.log("符号链接可用")
            return self.LINK_SYMLINK
        except:
            pass
        
        try:
            if (self.src / ".test_src").exists():
                os.remove(str(self.src / ".test_src"))
        except:
            pass
        
        # 2. 检查硬链接是否可行（同分区）
        try:
            src_drive = os.path.splitdrive(str(self.src))[0]
            dst_drive = os.path.splitdrive(str(self.dst))[0]
            if src_drive == dst_drive:
                self.log(f"源和目标在同一分区 {src_drive}，硬链接可用")
                return self.LINK_HARDLINK
        except:
            pass
        
        # 3. 降级为复制
        self.log("无法创建链接，将复制文件（占用磁盘空间）")
        return self.LINK_COPY
    
    def create_link(self, src_file: Path, dst_file: Path, link_type: str):
        """创建链接或复制文件"""
        try:
            # 目标文件已存在
            if dst_file.exists() or dst_file.is_symlink():
                # 检查是否已正确链接
                try:
                    if dst_file.is_symlink():
                        existing_target = os.readlink(str(dst_file))
                        if existing_target == str(src_file):
                            self.stats['skipped'] += 1
                            return True
                    elif os.path.samefile(str(src_file), str(dst_file)):
                        self.stats['skipped'] += 1
                        return True
                except:
                    pass
                
                # 删除旧文件
                try:
                    if dst_file.is_symlink() or dst_file.is_junction():
                        os.unlink(str(dst_file))
                    else:
                        os.remove(str(dst_file))
                except Exception as e:
                    self.log(f"删除旧文件失败: {dst_file.name} - {e}", 'WARN')
                    self.stats['errors'] += 1
                    return False
            
            # 确保目标目录存在
            dst_dir = dst_file.parent
            if not dst_dir.exists():
                dst_dir.mkdir(parents=True, exist_ok=True)
                self.stats['dirs_created'] += 1
            
            # 根据类型创建链接
            if link_type == self.LINK_SYMLINK:
                try:
                    os.symlink(str(src_file), str(dst_file))
                    self.stats['symlinks_created'] += 1
                    return True
                except OSError as e:
                    if e.winerror == 1314:  # 权限不足
                        # 降级为硬链接或复制
                        return self._create_link_fallback(src_file, dst_file)
                    raise
                    
            elif link_type == self.LINK_HARDLINK:
                try:
                    os.link(str(src_file), str(dst_file))
                    self.stats['hardlinks_created'] += 1
                    return True
                except OSError as e:
                    # 硬链接失败（跨分区等），降级为复制
                    self.log(f"硬链接失败: {dst_file.name}，改为复制", 'WARN')
                    return self._create_link_fallback(src_file, dst_file)
                    
            elif link_type == self.LINK_COPY:
                shutil.copy2(str(src_file), str(dst_file))
                self.stats['copies_created'] += 1
                return True
                
        except Exception as e:
            self.log(f"创建链接失败: {dst_file.name} - {e}", 'ERROR')
            self.stats['errors'] += 1
            return False
    
    def _create_link_fallback(self, src_file: Path, dst_file: Path):
        """硬链接失败后的降级方案"""
        try:
            # 检查是否同分区
            src_drive = os.path.splitdrive(str(src_file))[0]
            dst_drive = os.path.splitdrive(str(dst_file))[0]
            
            if src_drive == dst_drive:
                # 同分区，尝试硬链接
                try:
                    os.link(str(src_file), str(dst_file))
                    self.stats['hardlinks_created'] += 1
                    return True
                except:
                    pass
            
            # 不同分区或硬链接失败，复制文件
            shutil.copy2(str(src_file), str(dst_file))
            self.stats['copies_created'] += 1
            self.log(f"使用文件复制: {dst_file.name}", 'INFO')
            return True
            
        except Exception as e:
            self.log(f"复制文件失败: {dst_file.name} - {e}", 'ERROR')
            self.stats['errors'] += 1
            return False
    
    def scan_dst_files(self):
        """扫描目标文件夹，返回所有文件和目录列表"""
        files = []
        dirs = []
        try:
            for root, dirnames, filenames in os.walk(self.dst):
                for dirname in dirnames:
                    dst_dir = Path(root) / dirname
                    dirs.append(dst_dir)
                for filename in filenames:
                    dst_file = Path(root) / filename
                    files.append(dst_file)
        except Exception as e:
            self.log(f"扫描目标文件夹失败: {e}", 'ERROR')
        return files, dirs
    
    def remove_orphaned_files(self, src_files, dst_files):
        """删除目标文件夹中源文件夹不存在的文件"""
        removed = 0
        src_rel_paths = set()
        
        # 获取源文件的相对路径集合
        for src_file in src_files:
            rel_path = src_file.relative_to(self.src)
            src_rel_paths.add(str(rel_path).lower())
        
        # 删除目标文件夹中多余的文件
        for dst_file in dst_files:
            if self.cancelled:
                break
            try:
                rel_path = dst_file.relative_to(self.dst)
                rel_path_lower = str(rel_path).lower()
                
                if rel_path_lower not in src_rel_paths:
                    if dst_file.is_symlink() or dst_file.is_junction():
                        os.unlink(str(dst_file))
                    else:
                        os.remove(str(dst_file))
                    self.log(f"删除多余文件: {rel_path}")
                    removed += 1
            except Exception as e:
                self.log(f"删除文件失败: {dst_file} - {e}", 'WARN')
        
        return removed
    
    def remove_empty_dirs(self):
        """删除空目录"""
        removed = 0
        try:
            # 从深层目录开始删除
            for root, dirs, files in os.walk(str(self.dst), topdown=False):
                for dirname in dirs:
                    dir_path = Path(root) / dirname
                    try:
                        # 检查目录是否为空
                        if dir_path.exists() and not any(dir_path.iterdir()):
                            dir_path.rmdir()
                            rel_path = dir_path.relative_to(self.dst)
                            self.log(f"删除空目录: {rel_path}")
                            removed += 1
                    except:
                        pass
        except Exception as e:
            self.log(f"删除空目录失败: {e}", 'WARN')
        return removed
    
    def run(self):
        """执行同步"""
        try:
            self.log("=" * 60)
            self.log(f"源文件夹: {self.src}")
            self.log(f"目标文件夹: {self.dst}")
            self.log("=" * 60)
            
            # 检查源文件夹
            if not self.src.exists():
                self.log(f"源文件夹不存在: {self.src}", 'ERROR')
                return False
            
            # 创建目标根目录
            if not self.dst.exists():
                self.dst.mkdir(parents=True, exist_ok=True)
                self.log(f"创建目标文件夹: {self.dst}")
            
            # 检测最佳链接类型
            if self.link_type is None:
                self._detected_link_type = self.detect_best_link_type()
            else:
                self._detected_link_type = self.link_type
            
            self.log(f"使用链接类型: {self._detected_link_type}")
            
            # 扫描源文件
            src_files = self.scan_files()
            
            # 扫描目标文件（用于后续删除）
            self.log("扫描目标文件夹...")
            dst_files, dst_dirs = self.scan_dst_files()
            
            if not src_files:
                self.log("源文件夹为空，清理目标文件夹...")
                # 删除所有目标文件
                for dst_file in dst_files:
                    try:
                        if dst_file.is_symlink() or dst_file.is_junction():
                            os.unlink(str(dst_file))
                        else:
                            os.remove(str(dst_file))
                        self.log(f"删除: {dst_file.relative_to(self.dst)}")
                    except Exception as e:
                        self.log(f"删除失败: {dst_file} - {e}", 'WARN')
                # 删除空目录
                self.remove_empty_dirs()
                self.log("清理完成")
                return True
            
            # 处理每个文件
            total = len(src_files)
            start_time = time.time()
            last_update_time = start_time
            
            for i, src_file in enumerate(src_files, 1):
                if self.cancelled:
                    self.log("用户取消操作", 'WARN')
                    break
                
                # 计算相对路径
                rel_path = src_file.relative_to(self.src)
                dst_file = self.dst / rel_path
                
                # 创建链接
                self.create_link(src_file, dst_file, self._detected_link_type)
                
                # 更新进度
                current_time = time.time()
                if (current_time - last_update_time > 0.1) or (i == total):
                    elapsed = current_time - start_time
                    speed = i / elapsed if elapsed > 0 else 0
                    eta = (total - i) / speed if speed > 0 else 0
                    
                    if eta > 3600:
                        eta_str = f"{int(eta//3600)}h {int(eta%3600//60)}m"
                    elif eta > 60:
                        eta_str = f"{int(eta//60)}m {int(eta%60)}s"
                    elif eta > 0:
                        eta_str = f"{int(eta)}s"
                    else:
                        eta_str = "计算中..."
                    
                    msg = f"{rel_path.name} | 剩余: {eta_str}"
                    self.update_progress(i, total, msg)
                    last_update_time = current_time
                
                if i % 100 == 0:
                    self.log(f"进度: {i}/{total} ({i*100//total}%)")
            
            # 删除目标文件夹中多余的文件
            if not self.cancelled:
                self.log("清理多余文件...")
                removed_files = self.remove_orphaned_files(src_files, dst_files)
                if removed_files > 0:
                    self.log(f"删除 {removed_files} 个多余文件")
                
                # 删除空目录
                removed_dirs = self.remove_empty_dirs()
                if removed_dirs > 0:
                    self.log(f"删除 {removed_dirs} 个空目录")
            
            # 输出最终统计
            elapsed = time.time() - start_time
            self.log("=" * 60)
            self.log("同步完成！")
            self.log(f"目录创建: {self.stats['dirs_created']}")
            self.log(f"符号链接: {self.stats['symlinks_created']}")
            self.log(f"硬链接: {self.stats['hardlinks_created']}")
            self.log(f"文件复制: {self.stats['copies_created']}")
            self.log(f"已跳过: {self.stats['skipped']}")
            self.log(f"错误数量: {self.stats['errors']}")
            self.log(f"耗时: {elapsed:.1f} 秒")
            self.log("=" * 60)
            
            return self.stats['errors'] == 0 or not self.cancelled
            
        except Exception as e:
            self.log(f"同步失败: {e}", 'ERROR')
            import traceback
            self.log(traceback.format_exc(), 'ERROR')
            return False


class FileMirrorApp:
    """GUI 应用"""
    
    def __init__(self, root):
        self.root = root
        self.root.title("文件镜像同步工具")
        self.root.geometry("900x600")
        self.root.minsize(800, 500)
        
        # 窗口居中
        self._center_window()
        
        self.sync_thread = None
        self.sync_task = None
        
        # 检查管理员权限
        self.is_admin = is_admin()
        
        self._create_ui()
        
        # 如果没有管理员权限，提示并自动提权
        if not self.is_admin:
            self._request_elevation()
    
    def _center_window(self):
        """窗口居中显示"""
        self.root.update_idletasks()
        width = 900
        height = 600
        x = (self.root.winfo_screenwidth() // 2) - (width // 2)
        y = (self.root.winfo_screenheight() // 2) - (height // 2)
        self.root.geometry(f'{width}x{height}+{x}+{y}')
    
    def _request_elevation(self):
        """请求管理员权限 - 直接提权，不弹确认框"""
        if run_as_admin():
            # 关闭当前实例（强制退出，避免残留进程）
            os._exit(0)
        else:
            messagebox.showerror(
                "提权失败",
                "无法自动获取管理员权限。\n\n"
                "请手动以管理员身份运行此程序：\n"
                "右键点击 → 以管理员身份运行"
            )
    
    def _create_ui(self):
        """创建界面"""
        # 主框架
        main_frame = ttk.Frame(self.root, padding="10")
        main_frame.pack(fill=tk.BOTH, expand=True)
        
        # 源文件夹
        src_frame = ttk.LabelFrame(main_frame, text="源文件夹（网络文件夹）", padding="10")
        src_frame.pack(fill=tk.X, pady=(0, 10))
        
        self.src_var = tk.StringVar(value=r"\\vmware-host\Shared Folders\Downloads\!暂存待处理")
        src_entry = ttk.Entry(src_frame, textvariable=self.src_var, font=("Microsoft YaHei UI", 10))
        src_entry.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(0, 10))
        
        src_btn = ttk.Button(src_frame, text="浏览...", command=self._browse_src)
        src_btn.pack(side=tk.RIGHT)
        
        # 目标文件夹
        dst_frame = ttk.LabelFrame(main_frame, text="目标文件夹（本地镜像）", padding="10")
        dst_frame.pack(fill=tk.X, pady=(0, 10))
        
        self.dst_var = tk.StringVar(value=r"D:\暂存待处理本地镜像")
        dst_entry = ttk.Entry(dst_frame, textvariable=self.dst_var, font=("Microsoft YaHei UI", 10))
        dst_entry.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(0, 10))
        
        dst_btn = ttk.Button(dst_frame, text="浏览...", command=self._browse_dst)
        dst_btn.pack(side=tk.RIGHT)
        
        # 链接类型选择（仅符号链接）
        type_frame = ttk.LabelFrame(main_frame, text="链接类型", padding="10")
        type_frame.pack(fill=tk.X, pady=(0, 10))
        
        type_label = ttk.Label(
            type_frame, 
            text="符号链接（不占用磁盘空间，需要管理员权限）",
            font=("Microsoft YaHei UI", 10)
        )
        type_label.pack(side=tk.LEFT, padx=(0, 20))
        
        # 管理员权限状态
        if self.is_admin:
            status_text = "✓ 已获取管理员权限"
            status_color = "green"
        else:
            status_text = "✗ 未获取管理员权限 - 无法创建符号链接"
            status_color = "red"
        
        self.admin_status_label = ttk.Label(
            type_frame,
            text=status_text,
            font=("Microsoft YaHei UI", 9, "bold"),
            foreground=status_color
        )
        self.admin_status_label.pack(side=tk.LEFT)
        
        # 进度条
        progress_frame = ttk.LabelFrame(main_frame, text="同步进度", padding="10")
        progress_frame.pack(fill=tk.X, pady=(0, 10))
        
        self.progress_var = tk.DoubleVar()
        self.progress_bar = ttk.Progressbar(
            progress_frame, 
            variable=self.progress_var,
            maximum=100,
            mode='determinate',
            length=400
        )
        self.progress_bar.pack(fill=tk.X, pady=(0, 5))
        
        self.status_var = tk.StringVar(value="就绪 - 请选择源和目标文件夹")
        status_label = ttk.Label(progress_frame, textvariable=self.status_var, 
                                font=("Microsoft YaHei UI", 9))
        status_label.pack(fill=tk.X)
        
        # 按钮区域（放在日志框上方）
        btn_frame = ttk.Frame(main_frame)
        btn_frame.pack(fill=tk.X, pady=(0, 10))
        
        self.start_btn = ttk.Button(btn_frame, text="开始同步", command=self._start_sync, width=15)
        self.start_btn.pack(side=tk.LEFT, padx=(0, 10))
        
        self.cancel_btn = ttk.Button(btn_frame, text="取消", command=self._cancel_sync, 
                                     state=tk.DISABLED, width=15)
        self.cancel_btn.pack(side=tk.LEFT, padx=(0, 10))
        
        self.clear_btn = ttk.Button(btn_frame, text="清空日志", command=self._clear_log, width=15)
        self.clear_btn.pack(side=tk.LEFT, padx=(0, 10))
        
        # 右侧权限状态
        if self.is_admin:
            info_text = "✓ 管理员权限已获取"
            info_color = "green"
        else:
            info_text = "✗ 需要管理员权限"
            info_color = "red"
        
        self.bottom_status_label = ttk.Label(
            btn_frame, 
            text=info_text,
            font=("Microsoft YaHei UI", 9, "bold"),
            foreground=info_color
        )
        self.bottom_status_label.pack(side=tk.RIGHT)
        
        # 日志区域（缩小高度）
        log_frame = ttk.LabelFrame(main_frame, text="执行日志", padding="10")
        log_frame.pack(fill=tk.BOTH, expand=True)
        
        # 创建 Text 和 Scrollbar
        log_container = ttk.Frame(log_frame)
        log_container.pack(fill=tk.BOTH, expand=True)
        
        self.log_text = tk.Text(
            log_container, 
            font=("Consolas", 9),
            wrap=tk.WORD,
            state=tk.DISABLED,
            height=8
        )
        scrollbar = ttk.Scrollbar(log_container, orient=tk.VERTICAL, command=self.log_text.yview)
        self.log_text.configure(yscrollcommand=scrollbar.set)
        
        self.log_text.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)
    
    def _browse_src(self):
        """浏览源文件夹"""
        path = filedialog.askdirectory(
            title="选择源文件夹",
            initialdir=self.src_var.get()
        )
        if path:
            self.src_var.set(path)
    
    def _browse_dst(self):
        """浏览目标文件夹"""
        path = filedialog.askdirectory(
            title="选择目标文件夹",
            initialdir=self.dst_var.get()
        )
        if path:
            self.dst_var.set(path)
    
    def _log(self, message: str):
        """添加日志"""
        self.log_text.configure(state=tk.NORMAL)
        self.log_text.insert(tk.END, message + "\n")
        self.log_text.see(tk.END)
        self.log_text.configure(state=tk.DISABLED)
    
    def _clear_log(self):
        """清空日志"""
        self.log_text.configure(state=tk.NORMAL)
        self.log_text.delete(1.0, tk.END)
        self.log_text.configure(state=tk.DISABLED)
    
    def _update_progress(self, current: int, total: int, message: str):
        """更新进度条"""
        progress = (current / total * 100) if total > 0 else 0
        self.progress_var.set(progress)
        self.status_var.set(f"{current}/{total} ({progress:.1f}%) | {message}")
    
    def _start_sync(self):
        """开始同步"""
        src = self.src_var.get().strip()
        dst = self.dst_var.get().strip()
        
        if not src:
            messagebox.showerror("错误", "请选择源文件夹")
            return
        
        if not dst:
            messagebox.showerror("错误", "请选择目标文件夹")
            return
        
        # 检查源文件夹是否存在
        if not os.path.exists(src):
            # 检查是否是网络驱动器路径
            if src[1:2] == ":":
                drive = src[0].upper()
                msg = f"""源文件夹不存在: {src}

检测到您使用了网络驱动器 {drive}: 盘。
在管理员权限下，网络驱动器映射可能不可见。

解决方案：
1. 使用 UNC 路径代替驱动器盘符，例如：
   \\\\server\\share\\folder
   
2. 或者在普通用户权限下运行（但无法创建符号链接）

当前路径: {src}"""
            else:
                msg = f"源文件夹不存在:\n{src}"
            messagebox.showerror("错误", msg)
            return
        
        # 确认对话框
        msg = f"即将同步文件：\n\n源：{src}\n目标：{dst}\n\n将创建符号链接，不占用磁盘空间。\n\n是否继续？"
        if not messagebox.askyesno("确认", msg):
            return
        
        # 禁用按钮
        self.start_btn.configure(state=tk.DISABLED)
        self.cancel_btn.configure(state=tk.NORMAL)
        
        # 创建同步任务（强制使用符号链接）
        self.sync_task = FileMirrorSync(
            src=src,
            dst=dst,
            link_type=FileMirrorSync.LINK_SYMLINK,
            progress_callback=self._update_progress,
            log_callback=self._log
        )
        
        # 启动线程
        self.sync_thread = Thread(target=self._run_sync, daemon=True)
        self.sync_thread.start()
    
    def _run_sync(self):
        """在线程中运行同步"""
        try:
            success = self.sync_task.run()
            # 切回主线程更新 UI
            self.root.after(0, lambda: self._sync_complete(success))
        except Exception as e:
            self.root.after(0, lambda: self._sync_complete(False, str(e)))
    
    def _sync_complete(self, success: bool, error: str = None):
        """同步完成"""
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
        """取消同步"""
        if self.sync_task:
            self.sync_task.cancelled = True
            self._log("[WARN] 正在取消...")
            self.cancel_btn.configure(state=tk.DISABLED)


def main():
    """主函数"""
    # 检查是否在 Windows 上
    if sys.platform != 'win32':
        print("此工具仅支持 Windows 系统")
        sys.exit(1)
    
    # 创建主窗口
    root = tk.Tk()
    
    # 设置 DPI 感知（Windows 高 DPI 支持）
    try:
        from ctypes import windll
        windll.shcore.SetProcessDpiAwareness(1)
    except:
        pass
    
    # 创建应用
    app = FileMirrorApp(root)
    
    # 运行主循环
    root.mainloop()


if __name__ == "__main__":
    main()
