@echo off
rem setlog PC Web を起動する（ダブルクリック用）
pwsh -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\start-setlog-web.ps1" %*
