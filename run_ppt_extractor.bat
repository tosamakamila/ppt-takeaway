@echo off
chcp 65001 >nul 2>&1
title PPT v6.0
echo =============================================
echo      PPT Extractor v6.0
echo =============================================
echo.
echo   F2  = Select Area
echo   F3  = Start/Stop Monitor
echo   F4  = Manual Capture
echo   F5  = Record Clicks
echo   F6  = Play Current Group
echo   F7  = Stop
echo   F8  = Play All Groups
echo   F9  = Lock Window
echo   ESC = Cancel
echo   F10 = Exit
echo =============================================
echo.
python "%~dp0ppt_extractor.py"
pause
