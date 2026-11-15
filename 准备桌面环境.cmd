@echo off
chcp 65001 >nul
cd /d "%~dp0"
setlocal
if exist "%~dp0.runtime\Scripts\python.exe" goto install
if defined MEETING_PYTHON (
  "%MEETING_PYTHON%" -X utf8 -c "import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)"
  if errorlevel 1 goto missing
  "%MEETING_PYTHON%" -m venv "%~dp0.runtime"
  if errorlevel 1 goto failed
  goto install
)
where py >nul 2>nul
if not errorlevel 1 (
  py -3.11 -m venv "%~dp0.runtime"
  if not errorlevel 1 goto install
)
where python >nul 2>nul
if errorlevel 1 goto missing
python -X utf8 -c "import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)"
if errorlevel 1 goto missing
python -m venv "%~dp0.runtime"
if errorlevel 1 goto failed
:install
"%~dp0.runtime\Scripts\python.exe" -X utf8 -m pip install -r "%~dp0requirements_desktop.txt"
if errorlevel 1 goto failed
echo 桌面环境已准备完成，可以双击“启动工具.vbs”。
pause
exit /b 0
:missing
echo 请先安装 Python 3.11，或将 MEETING_PYTHON 设置为现有 Python 解释器的完整路径。
pause
exit /b 1
:failed
echo 安装未完成，请保留上方错误提示。
pause
exit /b 1
