# 智慧课堂PPT扒取器 v6.0

Windows 屏幕监控 + 鼠标点击自动化工具，用于捕获课堂/演示文稿幻灯片。

## 功能

1. **自动幻灯片截取** — 监控屏幕选定区域，检测画面变化（翻页），保存每张新幻灯片为 PNG
2. **多组点击序列** — 录制鼠标点击坐标，按组管理，定时/手动顺序执行，支持循环
3. **会话隔离** (v6.1) — 每组点击后自动创建独立子目录，不同视频/片段的截图互不混杂

## 技术栈

Python 3（3.10/3.13） | tkinter GUI | OpenCV 变化检测 + DCT 感知哈希去重 | pyautogui 截图/点击 | pynput 全局热键

## 目录结构

```
ppt_extractor.py          # 主程序，全部逻辑
run_ppt_extractor.bat     # 普通启动
run_silent.bat            # 静默启动（Conda ppt_env）
ppt_slides/               # 输出目录
  ├── config.json         # 配置
  ├── groups.json         # 点击组数据
  ├── ppt_log.txt         # 运行日志
  └── <组名>_MMDD_HHMMSS/ # 各监测会话的截图子目录
```

## 运行

```powershell
# 普通
C:\Users\23811\.local\bin\python3.12.exe ppt_extractor.py

# 静默
C:\Users\23811\.conda\envs\ppt_env\pythonw.exe ppt_extractor.py
```

依赖：`pyautogui opencv-python numpy pynput`

## 快捷键

| 键 | 功能 |
|---|---|
| F2 | 选择监控区域（两次点击左上→右下） |
| F3 | 启动/停止监测（截图存入独立子目录） |
| F4 | 手动截图 |
| F5 | 切换当前组的点击录制 |
| F6 | 执行当前组（执行后自动启动监测） |
| F7 | 停止执行 |
| F8 | 执行全部组（每组独立监测会话） |
| F10 | 退出 |

## 核心类/函数

- `ChangeDetector` — 多状态翻页检测（初始化→稳定→过渡→翻页完成），12% 像素差异阈值
- `ImageDedup` — DCT 感知哈希去重（95% 相似度阈值）
- `_start_monitoring(region, label)` / `_stop_monitoring()` — 监测会话生命周期管理
- `_make_session_dir(label)` — 创建 `标签_MMDD_HHMMSS/` 隔离目录
- `play_all_groups()` / `play_current_group()` — 点击执行，每组独立监测
- `monitoring_loop(region)` — 主监测循环，从 `state["session_dir"]` 读取输出路径

## 数据格式

`groups.json`：`[{name, clicks: [{x, y, delay}], repeat, next_delay}, ...]`
`config.json`：`{interval_minutes, click_loop}`
