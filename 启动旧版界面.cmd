@echo off
chcp 65001 >nul
cd /d "%~dp0"
if not exist "%~dp0.runtime\Scripts\python.exe" (
  echo 请先运行同目录的“准备桌面环境.cmd”。
  pause
  exit /b 1
)
"%~dp0.runtime\Scripts\python.exe" -X utf8 "%~dp0app.py" %*
if errorlevel 1 pause
