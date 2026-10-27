@echo off
rem Codex 5h quota watchdog - single entry (guard mode)
rem 双击启动看门狗；本窗口可最小化，看门狗意外退出会在 30 秒内自动重启。
cd /d "%~dp0"
title Codex 5h 限额看门狗 - 守护
:loop
netstat -ano | findstr ":27689" | findstr "LISTENING" >nul
if errorlevel 1 (
  start "" /b "D:\anaconda3\pythonw.exe" "%~dp0codex-watch.py"
)
timeout /t 30 /nobreak >nul
goto loop
