@echo off
chcp 65001 >nul 2>&1
title 智慧课堂PPT扒取器 v6.0
echo =============================================
echo      智慧课堂PPT扒取器 v6.0 - 多组模式
echo =============================================
echo.
echo   快捷键 (全局有效):
echo   -----------------------------
echo    F2    = 拖拽框选
echo    F3    = 开始/停止自动监测
echo    F4    = 手动截图
echo    F5    = 录制点击到当前组
echo    F6    = 执行当前组
echo    F7    = 停止执行
echo    F8    = 执行全部组（排队循环）
echo    ESC   = 取消框选
echo    F10   = 退出程序
echo   -----------------------------
echo    GUI窗口可编辑多组、设置循环次数和组间间隔
echo    数据文件: groups.json
echo    日志: D:\workspace\ppt_slides\ppt_log.txt
echo =============================================
echo.
"C:\Users\23811\.local\bin\python3.12.exe" D:\workspace\ppt_extractor.py
pause