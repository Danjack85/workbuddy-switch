@echo off
chcp 65001 >nul
setlocal

cd /d "%~dp0"

echo ==========================================
echo   WorkBuddy Switch - 构建 exe
echo ==========================================
echo.

where python >nul 2>nul
if errorlevel 1 (
    echo [!] 没有找到 python，请先安装 Python 3.9+ 并加入 PATH
    pause
    exit /b 1
)

python -c "import PyInstaller" >nul 2>nul
if errorlevel 1 (
    echo [*] 正在安装 PyInstaller ...
    python -m pip install --upgrade pyinstaller
    if errorlevel 1 (
        echo [!] PyInstaller 安装失败
        pause
        exit /b 1
    )
)

echo [*] 生成图标 ...
python tools\make_icon.py

echo.
echo [*] 开始打包（首次约 1-2 分钟）...
echo.
python -m PyInstaller build.spec --noconfirm --clean
if errorlevel 1 (
    echo.
    echo [!] 打包失败
    pause
    exit /b 1
)

echo.
echo ==========================================
echo   完成： dist\WorkBuddySwitch.exe
echo ==========================================
dir /b dist
echo.
echo   双击 exe 打开图形界面；
echo   命令行用法： dist\WorkBuddySwitch.exe state
echo.

endlocal
