@echo off
cd /d "%~dp0"
py -3 main.py gui
if errorlevel 1 pause
