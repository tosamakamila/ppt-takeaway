@echo off
chcp 65001 >nul
start /B "" pythonw "%~dp0ppt_extractor.py"
echo PPT扒取器已后台启动！（完全无窗口）
echo 日志: D:\Work_Place\ppt-takeaway\ppt_slides\ppt_log.txt
echo.
echo 快捷键：
echo   F2 = 选区域
echo   Ctrl+Shift+S = 开始监测
echo   F12 = 手动截图
echo   Ctrl+Shift+Q = 退出
echo.
pause
