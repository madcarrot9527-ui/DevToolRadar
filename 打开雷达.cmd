@echo off
rem Double-click this file to open the search page in your browser.
rem No Python, no install, no network. The page carries its own data.
if not exist "%~dp0site\index.html" (
  echo site\index.html not found. Run: python src\refresh.py
  pause
  exit /b 1
)
start "" "%~dp0site\index.html"
