@echo off
rem DevToolRadar CLI 入口（Windows）。
rem 把这个目录加进 PATH，就能在任意位置用 tool。
rem chcp 65001 切控制台到 UTF-8，否则中文输出乱码。
setlocal
chcp 65001 >nul 2>&1
rem 想指定解释器就设环境变量 DEVTOOLRADAR_PYTHON，否则用 PATH 上的 python。
set "PYEXE=%DEVTOOLRADAR_PYTHON%"
if not defined PYEXE set "PYEXE=python"
"%PYEXE%" "%~dp0src\cli.py" %*
endlocal
