@echo off
chcp 65001 >nul
setlocal

cd /d "%~dp0"

echo ==========================================
echo   SwitchSuite 一键构建（引擎侧车 + 桌面应用）
echo ==========================================
echo.

where python >nul 2>nul
if errorlevel 1 (
    echo [!] 没有找到 python，请先安装 Python 3.9+ 并加入 PATH
    pause
    exit /b 1
)
where node >nul 2>nul
if errorlevel 1 (
    echo [!] 没有找到 node，请先安装 Node.js 18+（桌面前端需要）
    pause
    exit /b 1
)

echo [*] 第 1/3 步：生成应用图标 ...
python tools\make_icon.py
if errorlevel 1 goto :fail

echo.
echo [*] 第 2/3 步：构建 WorkBuddy 引擎侧车（onedir）...
python -m PyInstaller build-sidecar.spec --noconfirm --clean
if errorlevel 1 goto :fail

if not exist "dist\wbswitch\wbswitch.exe" (
    echo [!] 侧车产物缺失：dist\wbswitch\wbswitch.exe
    goto :fail
)

echo.
echo [*] 第 3/3 步：构建 SwitchSuite 桌面应用（NSIS）...
if exist "desktop\src-tauri\sidecar\wbswitch" rmdir /s /q "desktop\src-tauri\sidecar\wbswitch"
mkdir "desktop\src-tauri\sidecar" 2>nul
xcopy "dist\wbswitch" "desktop\src-tauri\sidecar\wbswitch\" /E /I /Q /Y >nul
if errorlevel 1 goto :fail

pushd desktop
npm install || (popd & goto :fail)
npx tauri build || (popd & goto :fail)
popd

echo.
echo ==========================================
echo   完成：
echo     引擎独立版   dist\WorkBuddySwitch.exe（先跑 build.bat）
echo     桌面应用     desktop\src-tauri\target\release\bundle\nsis\*.exe
echo ==========================================
pause
exit /b 0

:fail
echo.
echo [!] 构建失败
pause
exit /b 1
