# FileMirrorTool 更新日志

## v1.3 (2026-06-16)

### 新功能
- **UI 字体放大**：界面字体从默认 9pt 提升到 11pt，提升可读性
- **进度保存与恢复**：同步中断后可恢复进度，避免重复复制
- **日志自动保存**：每次同步日志自动写入 `config/logs/` 目录，重启不丢失
- **文件夹互换**：一键交换源目录和目标目录路径
- **方案重命名**：右键菜单支持重命名已保存的方案

### Bug 修复
- **执行时间更新错误**：修复 `_auto_match_scheme` 覆盖 `current_scheme_path` 导致执行时间更新到错误方案的 bug（#1）
- **高亮跳回第一个方案**：`_update_scheme_run_time` 改为只更新单行，不再整体刷新列表触发选择事件（#2）
- **配置文件出现在方案列表**：过滤 `file_mirror_config.json` 和 `sync_progress.json`（#3）
- **窗口宽度不可调整**：panedwindow 左右 weight 均改为 1（#4）
- **str 对象无 .name 属性**：修复 cleanup 中 `rel_path.name` → `src_file.name`（#5）
- **src_files_rel 集合未填充**：修复 os.walk 循环中未记录源文件导致 cleanup 误删刚复制的文件（#6）
- **重启后日志丢失**：PyInstaller 打包后 `__file__` 指向临时目录，改用 `sys.executable.parent / "config"` 作为持久化基础目录（#7）

### 技术改进
- 源码从 C# 重写为 Python + tkinter GUI
- PyInstaller 单文件打包，无需 Python 环境

---

## v1.2 (2026-06-10)
- 初始 Python 版本，功能对齐 v1.2 C# 版本
- 符号链接镜像同步、可视化 GUI 界面

---

## v1.1 ~ v1.0
- C# 原始版本
