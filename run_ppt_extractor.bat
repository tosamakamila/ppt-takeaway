@echo off
chcp 65001 >nul 2>&1
title PPT v6.0
echo =============================================
echo      PPT Extractor v6.0
echo =============================================
echo.
echo   Alt+Q = Select Area
echo   Alt+W = Start/Stop Monitor
echo   Alt+E = Manual Capture
echo   Alt+R = Record Clicks
echo   Alt+T = Play Current Group
echo   Alt+Y = Stop
echo   Alt+U = Play All Groups
echo   Alt+I = Lock Window
echo   Alt+O = Exit
echo   ESC   = Cancel
echo =============================================
echo.
set "UV_CACHE_DIR=%~dp0.uv-cache"
set "UV_PYTHON=D:\Software\anaconda\python.exe"
set "PATH=C:\Program Files\Tesseract-OCR;%PATH%"
uv run python "%~dp0ppt_extractor.py"
if errorlevel 1 (
    echo.
    echo uv run failed, fallback to python...
    python "%~dp0ppt_extractor.py"
)
pause
