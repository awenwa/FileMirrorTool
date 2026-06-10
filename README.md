# File Mirror Tool (文件镜像同步工具)

将网络文件夹中的文件以符号链接形式镜像到本地，不占用实际磁盘空间。

## 功能

- 创建符号链接镜像（仅占用目录结构空间，文件内容指向源）
- 自动创建目录结构
- 同步时删除源中已删除的文件和空目录
- 实时进度显示
- 支持中断恢复（跳过已创建的链接）
- Windows 高 DPI 支持

## 使用

### 方式一：EXE（推荐）

直接双击 `FileMirrorTool.exe`，确认 UAC 对话框即可。

### 方式二：VBS 启动

双击 `run_as_admin.vbs`，无黑窗，确认 UAC 即可。

### 方式三：Python 源码

```bash
python file_mirror_tool.py
```

## 构建

```bash
pip install pyinstaller
pyinstaller --onefile --windowed --name FileMirrorTool --clean file_mirror_tool.py
```

构建产物在 `dist/FileMirrorTool.exe`。

## 注意事项

- 符号链接需要管理员权限，程序启动时会自动请求 UAC 提权
- 源文件夹支持 UNC 路径（如 `\\vmware-host\Shared Folders\Downloads\`）
- Windows UAC 无法通过代码自动跳过
