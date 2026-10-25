@echo off
rem Codex 5h 限额看门狗 - 双击启动桌面看板
cd /d "%~dp0"
start "" "D:\anaconda3\pythonw.exe" "%~dp0codex-watch.py"
