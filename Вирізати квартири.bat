@echo off
chcp 65001 >nul
cd /d "%~dp0"
python cut_apartments.py "%~1" -o output
echo.
pause
