@echo off
rem 手动刷新入口（Windows）。逻辑全在 src\refresh.py。
rem 双击即可；配计划任务请直接指向 python.exe src\refresh.py，不经过 cmd.exe。
setlocal
chcp 65001 >nul 2>&1
set "PYEXE=%DEVTOOLRADAR_PYTHON%"
if not defined PYEXE set "PYEXE=python"
"%PYEXE%" "%~dp0src\refresh.py" %*
set RC=%ERRORLEVEL%
endlocal & exit /b %RC%
