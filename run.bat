@echo off
chcp 65001 >nul
setlocal

cd /d "%~dp0"

echo ==========================================
echo   WorkBuddy Switch
echo ==========================================
echo.

where python >nul 2>nul
if errorlevel 1 (
    echo [!] 没有找到 python，请先安装 Python 3.9+ 并加入 PATH
    echo     下载： https://www.python.org/downloads/
    echo.
    pause
    exit /b 1
)

python -c "import tkinter" >nul 2>nul
if errorlevel 1 (
    echo [!] 当前 Python 缺少 tkinter，无法启动图形界面。
    echo     请安装 python.org 官方安装包，并勾选 "tcl/tk and IDLE"。
    echo.
    echo     仍然可以使用命令行版本：
    echo       python -m wbswitch.cli diagnose
    echo.
    pause
    exit /b 1
)

python -m wbswitch.cli gui %*
if errorlevel 1 (
    echo.
    echo [!] 启动失败，退出码 %errorlevel%
    pause
)

endlocal
