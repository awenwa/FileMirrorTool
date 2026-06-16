@echo off
chcp 65001 > nul
echo ===================================
echo FileMirrorTool v1.3 打包脚本
echo ===================================

REM 检查 Python 是否可用
python --version > nul 2>&1
if %errorlevel% neq 0 (
    echo [错误] 未找到 Python，请先安装 Python 并添加到 PATH
    pause
    exit /b 1
)

REM 安装 PyInstaller
echo [1/4] 检查/安装 PyInstaller...
pip show pyinstaller > nul 2>&1
if %errorlevel% neq 0 (
    echo 正在安装 PyInstaller...
    pip install pyinstaller -i https://pypi.tuna.tsinghua.edu.cn/simple
)

REM 切换到脚本目录
cd /d %~dp0

REM 执行打包
echo [2/4] 开始打包 exe...
pyinstaller --onefile --windowed --name "文件镜像同步工具" --uac-admin --hidden-import tkinter --hidden-import tkinter.ttk --hidden-import tkinter.filedialog --hidden-import tkinter.messagebox --hidden-import tkinter.scrolledtext file_mirror_tool.py

if %errorlevel% neq 0 (
    echo [错误] 打包失败，请检查错误信息
    pause
    exit /b 1
)

REM 复制 exe 到当前目录
echo [3/4] 复制 exe 文件...
if exist "dist\文件镜像同步工具.exe" (
    copy /Y "dist\文件镜像同步工具.exe" "."
    echo 已复制：文件镜像同步工具.exe
) else (
    echo [警告] 未找到打包后的 exe 文件
)

REM 清理临时文件
echo [4/4] 清理临时文件...
if exist "build" rmdir /s /q "build"
if exist "dist" rmdir /s /q "dist"
if exist "__pycache__" rmdir /s /q "__pycache__"
if exist "文件镜像同步工具.spec" del /q "文件镜像同步工具.spec"

echo ===================================
echo 打包完成！
echo exe 文件路径：%~dp0文件镜像同步工具.exe
echo ===================================
pause
