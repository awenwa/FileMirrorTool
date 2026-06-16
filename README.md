# FileMirrorTool - 文件镜像同步工具

[![Version](https://img.shields.io/badge/version-v1.3-blue.svg)](https://github.com/awenwa/FileMirrorTool/releases)

网络文件夹到本地文件夹的符号链接镜像同步工具，可视化 GUI 界面。

## 功能特性

- 📁 **符号链接镜像**：将网络文件夹映射为本地符号链接，节省本地空间
- 🔄 **多种同步方式**：镜像复制 / 符号链接 / 智能同步
- 💾 **进度保存**：中断后可恢复，避免重复复制
- 📝 **日志自动保存**：每次同步日志持久化到本地
- 🔀 **文件夹互换**：一键交换源/目标目录
- ✏️ **方案管理**：保存/加载/重命名同步方案
- 🖥️ **GUI 界面**：tkinter 原生界面，无需额外依赖

## 截图

![UI](screenshot_ui.bmp)

## 快速开始

### 从源码运行

```bash
python file_mirror_tool.py
```

需要 Python 3.8+，仅依赖 tkinter（Python 标准库）。

### 直接运行 exe

下载 [Releases](https://github.com/awenwa/FileMirrorTool/releases) 中的 `文件镜像同步工具.exe`，双击即可运行。

## 使用说明

1. 设置源目录（网络文件夹）和目标目录（本地目录）
2. 选择同步方式（推荐：符号链接）
3. 点击「保存方案」保存配置
4. 点击「开始同步」执行
5. 支持右键菜单：重命名、删除、交换源/目标

## 更新日志

详见 [CHANGELOG.md](CHANGELOG.md)

## 技术栈

- Python 3.12 + tkinter GUI
- PyInstaller 单文件打包
- 跨平台（Windows / macOS / Linux）

## License

MIT License
