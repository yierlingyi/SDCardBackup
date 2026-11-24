@echo off
chcp 65001 >nul
cd /d "%~dp0"
set "RUN="
where pyw >nul 2>nul && set "RUN=pyw -3.14"
if not defined RUN ( where pythonw >nul 2>nul && set "RUN=pythonw" )
if not defined RUN ( where py >nul 2>nul && set "RUN=py -3.14" )
if not defined RUN ( where python >nul 2>nul && set "RUN=python" )
if not defined RUN (
    echo [错误] 未检测到 Python，请安装 Python 3.9 及以上版本并勾选 Add to PATH
    pause
    exit /b 1
)
start "" %RUN% app.py
exit /b 0
