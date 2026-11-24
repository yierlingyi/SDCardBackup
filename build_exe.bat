@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo [1/3] 检查 PyInstaller ...
py -3.14 -m PyInstaller --version >nul 2>nul || py -3.14 -m pip install --upgrade pyinstaller || goto :err
echo [2/3] 清理旧构建 ...
if exist build rmdir /s /q build
if exist dist rmdir /s /q dist
echo [3/3] 打包为单文件 exe ...
py -3.14 -m PyInstaller --noconfirm --clean --onefile --windowed ^
    --name SDCardBackup ^
    --icon assets\app.ico ^
    --add-data "assets;assets" ^
    --add-data "camera_extensions.json;." ^
    app.py || goto :err
echo.
echo 完成：dist\SDCardBackup.exe
echo 提示：exe 首次运行时会在同目录生成 settings.json 与 logs\
pause
exit /b 0

:err
echo.
echo 打包失败，请检查上方错误信息
pause
exit /b 1
