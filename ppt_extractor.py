"""
智慧课堂PPT扒取器 v6.0
多组点击序列 + 分组排队循环 + 可配延迟
"""

# ---- DPI 感知：必须在任何 GUI 库之前执行 ----
import ctypes
try:
    ctypes.windll.shcore.SetProcessDpiAwareness(2)
except Exception:
    try:
        ctypes.windll.user32.SetProcessDPIAware()
    except Exception:
        pass

import pyautogui
import cv2
import numpy as np
import os
import time
import sys
import json
import threading
import tkinter as tk
from datetime import datetime
from pynput import keyboard, mouse
from ctypes import wintypes

# DXGI Desktop Duplication - GPU级别截屏，硬件加速窗口也能截到
try:
    import dxcam
    _dxcam = dxcam.create(output_idx=0, max_buffer_len=1)
except Exception:
    _dxcam = None

# ---- Windows API 窗口捕获 ----
_user32 = ctypes.windll.user32
_gdi32 = ctypes.windll.gdi32

class _BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [
        ('biSize', wintypes.DWORD), ('biWidth', wintypes.LONG), ('biHeight', wintypes.LONG),
        ('biPlanes', wintypes.WORD), ('biBitCount', wintypes.WORD),
        ('biCompression', wintypes.DWORD), ('biSizeImage', wintypes.DWORD),
        ('biXPelsPerMeter', wintypes.LONG), ('biYPelsPerMeter', wintypes.LONG),
        ('biClrUsed', wintypes.DWORD), ('biClrImportant', wintypes.DWORD),
    ]

def _capture_to_array(memDC, hBmp, w, h):
    """从内存DC中读取位图数据为BGR numpy数组"""
    bmi = _BITMAPINFOHEADER()
    bmi.biSize = ctypes.sizeof(_BITMAPINFOHEADER)
    bmi.biWidth = w
    bmi.biHeight = -h  # top-down
    bmi.biPlanes = 1
    bmi.biBitCount = 32
    bmi.biCompression = 0
    buf = ctypes.create_string_buffer(w * h * 4)
    _gdi32.GetDIBits(memDC, hBmp, 0, h, buf, ctypes.byref(bmi), 0)
    img = np.frombuffer(buf, dtype=np.uint8).reshape((h, w, 4))
    return img[:, :, :3].copy()  # BGR

def capture_window(hwnd):
    """捕获窗口内容。PrintWindow黑屏则快速重试，最后用DXGI兜底。返回 BGR numpy 数组或 None。"""
    rect = wintypes.RECT()
    _user32.GetWindowRect(hwnd, ctypes.byref(rect))
    w = rect.right - rect.left
    h = rect.bottom - rect.top
    if w <= 0 or h <= 0:
        return None

    # PrintWindow快速重试（硬件加速窗口时好时坏，多试几次就能截到）
    for i in range(5):
        hwndDC = _user32.GetWindowDC(hwnd)
        if not hwndDC:
            time.sleep(0.02)
            continue
        memDC = _gdi32.CreateCompatibleDC(hwndDC)
        hBmp = _gdi32.CreateCompatibleBitmap(hwndDC, w, h)
        _gdi32.SelectObject(memDC, hBmp)

        ok = _user32.PrintWindow(hwnd, memDC, 2)
        if ok == 0:
            ok = _user32.PrintWindow(hwnd, memDC, 0)
        if ok == 0:
            _gdi32.BitBlt(memDC, 0, 0, w, h, hwndDC, 0, 0, 0x00CC0020)

        img = _capture_to_array(memDC, hBmp, w, h)
        _gdi32.DeleteObject(hBmp)
        _gdi32.DeleteDC(memDC)
        _user32.ReleaseDC(hwnd, hwndDC)

        if np.mean(img) >= 5:
            return img  # 截到了

        # 黑屏，短暂等待再试
        time.sleep(0.03)

    # PrintWindow 5次都黑屏，用DXGI兜底
    if _dxcam is not None:
        try:
            _dxcam.start(target_fps=30)
            time.sleep(0.05)
            frame = _dxcam.get_latest_frame()
            _dxcam.stop()
            if frame is not None:
                frame_bgr = cv2.cvtColor(frame, cv2.COLOR_RGB2BGR)
                _user32.GetWindowRect(hwnd, ctypes.byref(rect))
                fh, fw = frame_bgr.shape[:2]
                x1, y1 = max(0, rect.left), max(0, rect.top)
                x2, y2 = min(fw, rect.right), min(fh, rect.bottom)
                if x2 > x1 and y2 > y1:
                    crop = frame_bgr[y1:y2, x1:x2]
                    if np.mean(crop) >= 5:
                        return crop
        except Exception:
            try:
                _dxcam.stop()
            except Exception:
                pass

    return None

def get_window_title(hwnd):
    """获取窗口标题"""
    length = _user32.GetWindowTextLengthW(hwnd) + 1
    buf = ctypes.create_unicode_buffer(length)
    _user32.GetWindowTextW(hwnd, buf, length)
    return buf.value or "(无标题)"

def get_window_at_point(x, y):
    """获取屏幕坐标处的顶层窗口句柄"""
    hwnd = _user32.WindowFromPoint(wintypes.POINT(x, y))
    # 获取顶层父窗口
    while hwnd:
        parent = _user32.GetAncestor(hwnd, 2)  # GA_ROOT = 2
        if parent:
            hwnd = parent
            break
        parent = _user32.GetParent(hwnd)
        if not parent:
            break
        hwnd = parent
    return hwnd

def is_window_visible(hwnd):
    """检查窗口是否仍然存在且可见"""
    return _user32.IsWindow(hwnd) != 0

def cv2_imwrite(path, img):
    """cv2.imwrite 的中文路径兼容版本"""
    try:
        ext = os.path.splitext(path)[1]
        _, buf = cv2.imencode(ext, img)
        with open(path, 'wb') as f:
            f.write(buf.tobytes())
        return True
    except Exception:
        return False

OUTPUT_DIR = r"D:\Work_Place\ppt-takeaway\ppt_slides"
CHANGE_THRESHOLD = 8
CHECK_INTERVAL = 0.3
HASH_SIMILARITY = 0.95
STABLE_FRAMES = 1

GROUPS_FILE = os.path.join(OUTPUT_DIR, "groups.json")
CONFIG_FILE = os.path.join(OUTPUT_DIR, "config.json")
OLD_CLICK_FILE = os.path.join(OUTPUT_DIR, "click_sequence.json")

def log(msg):
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    t = datetime.now().strftime("%H:%M:%S")
    with open(os.path.join(OUTPUT_DIR, "ppt_log.txt"), "a", encoding="utf-8") as f:
        f.write(f"[{t}] {msg}\n")
    safe_msg = msg.encode("gbk", errors="replace").decode("gbk")
    print(f"[{t}] {safe_msg}")

# ---- 数据格式 & 迁移 ----
def load_groups():
    # 旧格式自动迁移
    if os.path.exists(OLD_CLICK_FILE) and not os.path.exists(GROUPS_FILE):
        try:
            with open(OLD_CLICK_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            clicks = []
            if data:
                if isinstance(data[0], list):
                    clicks = [{"x": item[0], "y": item[1], "delay": 1.0} for item in data]
                else:
                    clicks = data
            groups = [{"name": "默认组", "clicks": clicks, "repeat": 1, "next_delay": 600}]
            os.makedirs(OUTPUT_DIR, exist_ok=True)
            with open(GROUPS_FILE, "w", encoding="utf-8") as f:
                json.dump(groups, f, ensure_ascii=False, indent=2)
            os.rename(OLD_CLICK_FILE, OLD_CLICK_FILE + ".bak")
            log(f"已迁移旧数据 -> {len(clicks)} 步，组名: 默认组")
            return groups
        except Exception as e:
            log(f"数据迁移失败: {e}")

    if os.path.exists(GROUPS_FILE):
        try:
            with open(GROUPS_FILE, "r", encoding="utf-8") as f:
                groups = json.load(f)
            if groups:
                for g in groups:
                    g.setdefault("repeat", 1)
                    g.setdefault("next_delay", 600)
                    if "clicks" in g and g["clicks"] and isinstance(g["clicks"][0], list):
                        g["clicks"] = [{"x": c[0], "y": c[1], "delay": 1.0} for c in g["clicks"]]
                return groups
        except Exception:
            pass
    return [{"name": "默认组", "clicks": [], "repeat": 1, "next_delay": 600}]

def save_groups(groups):
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    with open(GROUPS_FILE, "w", encoding="utf-8") as f:
        json.dump(groups, f, ensure_ascii=False, indent=2)

def load_config():
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {"interval_minutes": 30, "default_delay": 1.0, "global_group_loop": True}

def save_config(cfg):
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)

_config = load_config()

state = {
    "running": True,
    "monitoring": False,
    "region": None,
    "picking": False,
    "pick_step": 0,
    "pick_p1": None,
    "recording": False,
    "groups": load_groups(),
    "current_group_index": 0,
    "countdown_seconds": _config["interval_minutes"] * 60,
    "interval_seconds": _config["interval_minutes"] * 60,
    "timer_active": False,
    "default_delay": _config.get("default_delay", 1.0),
    "global_group_loop": _config.get("global_group_loop", True),
    "playing": False,       # 是否正在执行（当前组 or 全部组）
    "playing_all": False,   # 是否正在执行全部组
    "session_dir": None,    # 当前监测会话的输出子目录（None 时用 OUTPUT_DIR）
    "target_hwnd": None,    # 锁定的窗口句柄（None=屏幕截取模式）
    "window_picking": False, # 是否正在选取窗口
    "window_title": "",     # 锁定窗口的标题
}
_lock = threading.Lock()

# 确保至少有一个组
if not state["groups"]:
    state["groups"] = [{"name": "默认组", "clicks": [], "repeat": 1, "next_delay": 600}]
    save_groups(state["groups"])

# ====================== 翻页检测 ======================
class ChangeDetector:
    def __init__(self, t=CHANGE_THRESHOLD):
        self.ref = None
        self.prev = None
        self.t = t
        self._stable_count = 0
        self._init_count = 0

    def detect(self, frame):
        g = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        g = cv2.GaussianBlur(g, (5, 5), 0)

        if self.ref is None:
            if self.prev is not None:
                d = cv2.absdiff(self.prev, g)
                _, th = cv2.threshold(d, 25, 255, cv2.THRESH_BINARY)
                s = np.sum(th > 0) / th.size * 100
                if s < 5:
                    self._init_count += 1
                else:
                    self._init_count = 0
            self.prev = g
            if self._init_count >= STABLE_FRAMES:
                self.ref = g
                log("参考帧已就绪（画面稳定）")
            else:
                # 超时保护：如果5秒还没稳定，强制用当前帧
                if self._init_count == 0 and self.prev is not None:
                    self._init_count = 1
                if self._init_count >= 1:
                    # 等了1帧还不稳定就直接用，避免视频永远卡在init
                    self.ref = g
                    log("参考帧已就绪（超时强制）")
            return False, 0, "init"

        d_ref = cv2.absdiff(self.ref, g)
        _, th_ref = cv2.threshold(d_ref, 25, 255, cv2.THRESH_BINARY)
        score_ref = np.sum(th_ref > 0) / th_ref.size * 100

        d_prev = cv2.absdiff(self.prev, g)
        _, th_prev = cv2.threshold(d_prev, 25, 255, cv2.THRESH_BINARY)
        score_prev = np.sum(th_prev > 0) / th_prev.size * 100

        self.prev = g

        if score_prev < 5:
            self._stable_count += 1
        else:
            self._stable_count = 0

        if score_ref > self.t:
            if self._stable_count >= STABLE_FRAMES:
                # 画面已稳定，确认翻页
                self.ref = g
                self._stable_count = 0
                return True, score_ref, "page_done"
            elif score_ref > self.t * 3:
                # 差异非常大（比如整页切换），不等稳定直接截图
                self.ref = g
                self._stable_count = 0
                return True, score_ref, "page_jump"
            return False, score_ref, "transitioning"

        if score_prev > 40:
            return False, score_prev, "fast_switch"

        return False, score_ref, "stable"

    def reset(self):
        self.ref = None
        self.prev = None
        self._stable_count = 0
        self._init_count = 0

class ImageDedup:
    def __init__(self, t=HASH_SIMILARITY):
        self.hashes = []; self.t = t
    def _phash(self, img):
        g = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
        g = cv2.resize(g, (32, 32))
        dct = cv2.dct(np.float32(g))
        low = dct[:8, :8]
        m = np.median(low)
        return "".join("1" if low[i, j] > m else "0" for i in range(8) for j in range(8))
    def dup(self, img):
        ph = self._phash(img)
        for h in self.hashes:
            if 1 - (sum(c1 != c2 for c1, c2 in zip(ph, h)) / 64) >= self.t:
                return True
        self.hashes.append(ph)
        return False

# ====================== 手动截图 ======================
def manual_capture():
    with _lock:
        region = state["region"]
        hwnd = state["target_hwnd"]
    if not region:
        log("请先按 F2 选择区域!")
        return
    x, y, w, h = region
    if w < 10 or h < 10:
        log("区域太小，请重选!")
        return
    try:
        ts = datetime.now().strftime("%H%M%S")
        fn = f"manual_{ts}.png"
        with _lock:
            out_dir = state.get("session_dir") or OUTPUT_DIR
        if hwnd and is_window_visible(hwnd):
            # 窗口捕获模式
            img = capture_window(hwnd)
            if img is not None:
                img = img[y:y+h, x:x+w]
                cv2_imwrite(os.path.join(out_dir, fn), img)
                log(f"手动截图(窗口) -> {fn} ({w}x{h})")
            else:
                log("窗口捕获失败，窗口可能已最小化")
        else:
            sc = pyautogui.screenshot(region=(x, y, w, h))
            sc.save(os.path.join(out_dir, fn))
            log(f"手动截图 -> {fn} ({w}x{h})")
    except Exception as e:
        log(f"截图失败: {e}")

# ====================== 监测会话管理 ======================
def _make_session_dir(label):
    """为监测会话创建独立子目录"""
    ts = datetime.now().strftime("%m%d_%H%M%S")
    safe = label.replace(" ", "_").replace("/", "_").replace("\\", "_").replace(":", "_")
    name = f"{safe}_{ts}"
    full = os.path.join(OUTPUT_DIR, name)
    os.makedirs(full, exist_ok=True)
    return full

def _start_monitoring(region, label="手动"):
    """创建会话目录并启动监测"""
    session_dir = _make_session_dir(label)
    with _lock:
        state["session_dir"] = session_dir
        state["monitoring"] = True
    threading.Thread(target=monitoring_loop, args=(region,), daemon=True).start()
    log(f"监测会话开始 -> {os.path.basename(session_dir)}/")

def _stop_monitoring():
    """停止当前监测会话"""
    with _lock:
        was = state["monitoring"]
        state["monitoring"] = False
    if was:
        time.sleep(CHECK_INTERVAL + 0.2)  # 等待监测循环退出
        log("监测会话结束")

# ====================== 点击执行引擎 ======================
def _do_sequence(clicks, label=""):
    """执行一组点击序列，返回是否被中断"""
    for i, step in enumerate(clicks):
        with _lock:
            if not state["running"] or not state["playing"]:
                log(f"[{label}] 点击序列中断 @ 第{i+1}步")
                return False
        cx, cy = step["x"], step["y"]
        delay = step.get("delay", 1.0)
        log(f"[{label}] 点击 {i+1}/{len(clicks)}: ({cx},{cy}) 延迟{delay:.1f}s")
        pyautogui.click(cx, cy)
        time.sleep(delay)
    return True

def play_current_group():
    """只执行当前选中组的点击序列（尊重 repeat），执行后启动监测捕获新画面"""
    with _lock:
        if state["playing"]:
            log("正在执行中，请先停止")
            return
        gidx = state["current_group_index"]
        if gidx < 0 or gidx >= len(state["groups"]):
            log("没有选中的组")
            return
        group = dict(state["groups"][gidx])
        region = state["region"]
        state["playing"] = True
        state["playing_all"] = False

    clicks = group.get("clicks", [])
    repeat = group.get("repeat", 1)
    name = group.get("name", f"组{gidx+1}")

    if not region:
        log("请先按 F2 选区域!")
        with _lock:
            state["playing"] = False
        return

    if not clicks:
        log(f"[{name}] 没有点击步骤")
        with _lock:
            state["playing"] = False
        return

    # 1. 停止当前监测，打包上一段截图
    _stop_monitoring()

    # 2. 执行点击（监测关闭，点击切换视频）
    log(f"执行组 [{name}]: {len(clicks)} 步 × {repeat} 次")
    for r in range(repeat):
        with _lock:
            if not state["playing"]:
                log(f"[{name}] 第{r+1}/{repeat}次循环前被中断")
                break
        if r > 0:
            log(f"[{name}] 第{r+1}/{repeat}次循环")
        ok = _do_sequence(clicks, label=f"{name}.{r+1}")
        if not ok:
            break

    with _lock:
        state["playing"] = False

    # 3. 启动新监测会话，捕获新视频画面
    _start_monitoring(region, name)

    log(f"[{name}] 执行完毕")
    with _lock:
        state["countdown_seconds"] = state["interval_seconds"]

def play_all_groups():
    """按顺序执行所有组，每组独立监测会话，支持全局循环"""
    with _lock:
        if state["playing"]:
            log("正在执行中，请先停止")
            return
        groups = list(state["groups"])
        loop = state["global_group_loop"]
        region = state["region"]
        state["playing"] = True
        state["playing_all"] = True

    if not region:
        log("请先按 F2 选区域!")
        with _lock:
            state["playing"] = False
            state["playing_all"] = False
        return

    if not groups:
        log("没有组可执行")
        with _lock:
            state["playing"] = False
            state["playing_all"] = False
        return

    # 先停止已有监测
    _stop_monitoring()

    log(f"执行全部组 ({len(groups)} 组) | 全局循环={'开' if loop else '关'}")
    round_num = 0
    while True:
        with _lock:
            if not state["playing"] or not state["playing_all"]:
                break
        round_num += 1
        if loop and len(groups) > 1:
            log(f"---- 第 {round_num} 轮 ----")

        for gi in range(len(groups)):
            with _lock:
                if not state["playing"] or not state["playing_all"]:
                    break
                grp = dict(state["groups"][gi])

            clicks = grp.get("clicks", [])
            repeat = grp.get("repeat", 1)
            next_delay = grp.get("next_delay", 600)
            name = grp.get("name", f"组{gi+1}")

            if not clicks:
                log(f"[{name}] 跳过（无点击步骤）")
                continue

            # 执行点击（监测关闭）
            log(f"[{name}] {len(clicks)} 步 × {repeat} 次")
            for r in range(repeat):
                with _lock:
                    if not state["playing"] or not state["playing_all"]:
                        break
                if r > 0:
                    log(f"[{name}] 第{r+1}/{repeat}次")
                ok = _do_sequence(clicks, label=f"{name}.{r+1}")
                if not ok:
                    break

            with _lock:
                if not state["playing"] or not state["playing_all"]:
                    break

            # 启动监测会话，捕获新视频画面
            _start_monitoring(region, name)

            # 组间等待（监测运行中，捕获视频画面）
            is_last = (gi == len(groups) - 1)
            if not is_last or loop:
                if next_delay > 0:
                    log(f"[{name}] 等待 {next_delay:.0f}s（监测中）...")
                    waited = 0
                    while waited < next_delay:
                        with _lock:
                            if not state["playing"] or not state["playing_all"]:
                                break
                        sleep_chunk = min(1, next_delay - waited)
                        time.sleep(sleep_chunk)
                        waited += sleep_chunk

            # 停止当前组监测，截图已隔离在该组目录中
            _stop_monitoring()

        if not loop:
            break

    # 安全停止：确保退出时监测已关闭
    _stop_monitoring()

    with _lock:
        state["playing"] = False
        state["playing_all"] = False

    log("全部组执行完毕")
    with _lock:
        state["countdown_seconds"] = state["interval_seconds"]

def stop_play():
    with _lock:
        if state["playing"]:
            state["playing"] = False
            state["playing_all"] = False
            log("正在停止执行...")

# ====================== 监测循环 ======================
def monitoring_loop(region):
    x, y, w, h = region
    det = ChangeDetector()
    dedup = ImageDedup()
    cd = 0
    cnt = 0
    start = time.time()

    with _lock:
        out_dir = state.get("session_dir") or OUTPUT_DIR
        hwnd = state["target_hwnd"]
        use_window = hwnd is not None and is_window_visible(hwnd)

    mode_str = "窗口捕获" if use_window else "屏幕截取"
    log(f"监测中 ({w}x{h}) @ ({x},{y}) [{mode_str}]")
    log(f"输出目录: {out_dir}")

    # 首帧测试截图，方便排查黑屏问题
    try:
        if use_window:
            test_img = capture_window(hwnd)
            if test_img is not None:
                test_crop = test_img[y:y+h, x:x+w]
                cv2_imwrite(os.path.join(out_dir, "_test_capture.png"), test_crop)
                # 检测是否黑屏
                mean_val = np.mean(test_crop)
                if mean_val < 5:
                    log("警告: 窗口捕获截图全黑！自动回退到屏幕截取模式")
                    log("提示: 微信小程序等硬件加速窗口不支持窗口捕获，请保持窗口可见")
                    use_window = False
                    with _lock:
                        state["target_hwnd"] = None
                        state["window_title"] = ""
                        if state["region"]:
                            rect = wintypes.RECT()
                            _user32.GetWindowRect(hwnd, ctypes.byref(rect))
                            rx2, ry2, rw2, rh2 = state["region"]
                            state["region"] = (rx2 + rect.left, ry2 + rect.top, rw2, rh2)
                    _run_on_main(_show_toast, "窗口捕获黑屏，已回退屏幕截取模式", '#f38ba8', 3000)
                    # 重新用屏幕截取模式截图
                    test_sc = pyautogui.screenshot(region=(x, y, w, h))
                    test_sc.save(os.path.join(out_dir, "_test_capture.png"))
                    log(f"屏幕截取测试截图已保存 -> _test_capture.png")
                else:
                    log(f"测试截图已保存 -> _test_capture.png (均值={mean_val:.1f})")
            else:
                log("警告: 窗口捕获返回空！自动回退到屏幕截取模式")
                use_window = False
                _run_on_main(_show_toast, "窗口捕获失败，已回退屏幕截取模式", '#f38ba8', 3000)
        else:
            test_sc = pyautogui.screenshot(region=(x, y, w, h))
            test_sc.save(os.path.join(out_dir, "_test_capture.png"))
            log(f"测试截图已保存 -> _test_capture.png")
    except Exception as e:
        log(f"测试截图失败: {e}")

    while True:
        with _lock:
            if not state["running"] or not state["monitoring"]:
                break
            hwnd = state["target_hwnd"]
            use_window = hwnd is not None and is_window_visible(hwnd)

        try:
            if use_window:
                img = capture_window(hwnd)
                if img is None:
                    log("窗口捕获失败，自动回退屏幕截取模式")
                    use_window = False
                    with _lock:
                        state["target_hwnd"] = None
                        state["window_title"] = ""
                        if state["region"]:
                            rect2 = wintypes.RECT()
                            _user32.GetWindowRect(hwnd, ctypes.byref(rect2))
                            rx2, ry2, rw2, rh2 = state["region"]
                            state["region"] = (rx2 + rect2.left, ry2 + rect2.top, rw2, rh2)
                    _run_on_main(_show_toast, "窗口捕获失败，已回退屏幕截取", '#f38ba8', 3000)
                    continue
                frame = img[y:y+h, x:x+w]
                # 检测黑屏（capture_window内部已重试3次，这里还黑屏说明真的截不到）
                if np.mean(frame) < 5:
                    log("窗口捕获持续黑屏，自动回退屏幕截取模式")
                    use_window = False
                    with _lock:
                        state["target_hwnd"] = None
                        state["window_title"] = ""
                        if state["region"]:
                            rect2 = wintypes.RECT()
                            _user32.GetWindowRect(hwnd, ctypes.byref(rect2))
                            rx2, ry2, rw2, rh2 = state["region"]
                            state["region"] = (rx2 + rect2.left, ry2 + rect2.top, rw2, rh2)
                    _run_on_main(_show_toast, "窗口持续黑屏，已回退屏幕截取", '#f38ba8', 3000)
                    continue
            else:
                sc = pyautogui.screenshot(region=(x, y, w, h))
                frame = cv2.cvtColor(np.array(sc), cv2.COLOR_RGB2BGR)
        except Exception as e:
            log(f"截图失败: {e}")
            time.sleep(1)
            continue

        changed, score, status = det.detect(frame)

        elapsed = time.time() - start
        if int(elapsed) % 20 == 0 and int(elapsed) > 0 and int((elapsed - 0.5)) % 20 != 0:
            log(f"[心跳] 运行中 {cnt}张 | 状态={status} | 差异={score:.1f}%")

        if cd > 0:
            cd -= 1

        if changed and cd == 0 and not dedup.dup(frame):
            cnt += 1
            ts = datetime.now().strftime("%H%M%S")
            fn = f"slide_{cnt:03d}_{ts}.png"
            cv2_imwrite(os.path.join(out_dir, fn), frame)
            log(f"[{cnt:03d}] 翻页 ({score:.0f}%) -> {fn}")
            cd = 3

        time.sleep(CHECK_INTERVAL)

    elapsed = time.time() - start
    log(f"监测结束 | {elapsed:.0f}s | {cnt}张 | 目录: {os.path.basename(out_dir)}/")
    # 清理空目录
    try:
        if os.path.isdir(out_dir) and not os.listdir(out_dir):
            os.rmdir(out_dir)
            log(f"已清理空目录: {os.path.basename(out_dir)}/")
    except Exception:
        pass
    with _lock:
        state["monitoring"] = False

# ====================== 可视化反馈 ======================
_sel_window = None
_highlight_window = None
_highlight_hwnd = None  # 当前高亮的窗口句柄，避免重复创建
_toast_window = None
_gui_root = None  # 主窗口引用，用于线程安全调度

def _set_gui_root(root):
    """设置主窗口引用（create_gui 时调用）"""
    global _gui_root
    _gui_root = root

def _run_on_main(func, *args):
    """在主线程中执行 UI 操作"""
    if _gui_root:
        try:
            _gui_root.after(0, func, *args)
        except Exception:
            pass

def _show_selection_rect(x1, y1, x2, y2):
    """显示半透明选区矩形（复用窗口，只更新位置）"""
    global _sel_window
    rx, ry = min(x1, x2), min(y1, y2)
    rw, rh = abs(x2 - x1), abs(y2 - y1)
    if rw < 5 or rh < 5:
        return
    if _sel_window:
        try:
            _sel_window.geometry(f"{rw}x{rh}+{rx}+{ry}")
            return
        except Exception:
            _sel_window = None
    _sel_window = tk.Toplevel()
    _sel_window.overrideredirect(True)
    _sel_window.attributes('-topmost', True)
    _sel_window.attributes('-alpha', 0.3)
    _sel_window.geometry(f"{rw}x{rh}+{rx}+{ry}")
    _sel_window.configure(bg='#89b4fa')

def _hide_selection_rect():
    """隐藏选区矩形"""
    global _sel_window
    if _sel_window:
        try:
            _sel_window.destroy()
        except Exception:
            pass
        _sel_window = None

def _flash_rect(x, y, w, h, color='#a6e3a1', duration=400):
    """闪一下矩形表示操作成功"""
    win = tk.Toplevel()
    win.overrideredirect(True)
    win.attributes('-topmost', True)
    win.attributes('-alpha', 0.5)
    win.geometry(f"{w}x{h}+{x}+{y}")
    win.configure(bg=color)
    win.after(duration, win.destroy)

def _highlight_window_rect(hwnd, color='#89b4fa'):
    """高亮显示窗口边框（复用窗口，只在 hwnd 变化时重建）"""
    global _highlight_window, _highlight_hwnd
    if not hwnd or not is_window_visible(hwnd):
        _hide_highlight()
        return
    # 同一个窗口，只更新位置
    if _highlight_window and _highlight_hwnd == hwnd:
        try:
            rect = wintypes.RECT()
            _user32.GetWindowRect(hwnd, ctypes.byref(rect))
            w = rect.right - rect.left
            h = rect.bottom - rect.top
            if w > 0 and h > 0:
                _highlight_window.geometry(f"{w}x{h}+{rect.left}+{rect.top}")
                return
        except Exception:
            pass
    # 不同窗口，重建
    _hide_highlight()
    rect = wintypes.RECT()
    _user32.GetWindowRect(hwnd, ctypes.byref(rect))
    w = rect.right - rect.left
    h = rect.bottom - rect.top
    if w <= 0 or h <= 0:
        return
    _highlight_hwnd = hwnd
    _highlight_window = tk.Toplevel()
    _highlight_window.overrideredirect(True)
    _highlight_window.attributes('-topmost', True)
    _highlight_window.attributes('-alpha', 0.25)
    _highlight_window.geometry(f"{w}x{h}+{rect.left}+{rect.top}")
    _highlight_window.configure(bg=color)

def _hide_highlight():
    """隐藏窗口高亮"""
    global _highlight_window, _highlight_hwnd
    if _highlight_window:
        try:
            _highlight_window.destroy()
        except Exception:
            pass
        _highlight_window = None
        _highlight_hwnd = None

def _show_toast(msg, color='#a6e3a1', duration=2000):
    """在屏幕顶部弹出提示条"""
    global _toast_window
    _hide_toast()
    _toast_window = tk.Toplevel()
    _toast_window.overrideredirect(True)
    _toast_window.attributes('-topmost', True)
    _toast_window.attributes('-alpha', 0.9)
    # 获取屏幕宽度
    sw = _toast_window.winfo_screenwidth()
    tw = max(len(msg) * 12, 300)
    th = 36
    tx = (sw - tw) // 2
    _toast_window.geometry(f"{tw}x{th}+{tx}+{60}")
    _toast_window.configure(bg=color)
    tk.Label(_toast_window, text=msg, fg='#1e1e2e', bg=color,
             font=("Microsoft YaHei UI", 11, "bold")).pack(expand=True)
    _toast_window.after(duration, _toast_window.destroy)

def _hide_toast():
    """隐藏提示条"""
    global _toast_window
    if _toast_window:
        try:
            _toast_window.destroy()
        except Exception:
            pass
        _toast_window = None

# ====================== 鼠标监听 ======================
def on_mouse_click(x, y, button, pressed):
    if button != mouse.Button.left:
        return
    with _lock:
        picking = state["picking"]
        recording = state["recording"]
        step = state["pick_step"]
        gidx = state["current_group_index"]
        window_picking = state["window_picking"]

    # 窗口选取模式：点击即选中窗口
    if window_picking and pressed:
        hwnd = get_window_at_point(x, y)
        title = get_window_title(hwnd)
        with _lock:
            state["target_hwnd"] = hwnd
            state["window_picking"] = False
            state["window_title"] = title
            # 如果之前有选区，转为窗口相对坐标
            if state["region"]:
                rect = wintypes.RECT()
                _user32.GetWindowRect(hwnd, ctypes.byref(rect))
                rx, ry, rw, rh = state["region"]
                state["region"] = (rx - rect.left, ry - rect.top, rw, rh)
        log(f"已锁定窗口: [{title}] (句柄={hwnd})")
        # 反馈：窗口闪绿 + 顶部提示
        rect = wintypes.RECT()
        _user32.GetWindowRect(hwnd, ctypes.byref(rect))
        _run_on_main(_hide_highlight)
        _run_on_main(_flash_rect, rect.left, rect.top, rect.right - rect.left, rect.bottom - rect.top, '#a6e3a1', 500)
        _run_on_main(_show_toast, f"已锁定窗口: {title}", '#a6e3a1')
        return

    if recording:
        if not pressed:
            return
        delay = state["default_delay"]
        with _lock:
            if gidx < len(state["groups"]):
                state["groups"][gidx]["clicks"].append({"x": x, "y": y, "delay": delay})
                save_groups(state["groups"])
                n = len(state["groups"][gidx]["clicks"])
                name = state["groups"][gidx]["name"]
        log(f"[{name}] 已记录 #{n}: ({x},{y}) 延迟{delay:.1f}s")
        return

    if not picking:
        return

    # 拖拽框选：按下记录起点，松开记录终点
    if pressed and step == 1:
        with _lock:
            state["pick_p1"] = (x, y)
            state["pick_step"] = 2
        log(f"选区起点: ({x},{y}) -> 拖拽到右下角松开")
    elif not pressed and step == 2:
        with _lock:
            p1 = state["pick_p1"]
            hwnd = state["target_hwnd"]
        if p1:
            x1, y1 = p1
            rx, ry = min(x1, x), min(y1, y)
            rw, rh = abs(x - x1), abs(y - y1)
            _run_on_main(_hide_selection_rect)
            # 如果锁定了窗口，转为窗口相对坐标
            if hwnd and is_window_visible(hwnd):
                rect = wintypes.RECT()
                _user32.GetWindowRect(hwnd, ctypes.byref(rect))
                rx -= rect.left
                ry -= rect.top
            with _lock:
                state["region"] = (rx, ry, rw, rh)
                state["picking"] = False
                state["pick_step"] = 0
                state["pick_p1"] = None
            log(f"选区: ({rx},{ry}) {rw}x{rh}")
            # 反馈：选区闪绿 + 顶部提示
            _run_on_main(_flash_rect, min(x1, x), min(y1, y), rw, rh, '#a6e3a1', 500)
            _run_on_main(_show_toast, f"选区完成: {rw}x{rh}", '#a6e3a1')

_last_move_time = 0
_move_pending = False
_move_data = None  # (x, y, picking, step, p1, window_picking)

def _process_move():
    """在主线程中处理鼠标移动 UI 更新"""
    global _move_pending, _move_data
    _move_pending = False
    data = _move_data
    if not data:
        return
    x, y, picking, step, p1, window_picking = data

    # 没有任何操作模式时，不做任何 UI 更新
    if not picking and not window_picking:
        _hide_highlight()
        _hide_selection_rect()
        return

    # F2 框选拖拽预览
    if picking and step == 2 and p1:
        _show_selection_rect(p1[0], p1[1], x, y)

    # F9 窗口选取悬停高亮
    if window_picking:
        hwnd = get_window_at_point(x, y)
        if hwnd:
            _highlight_window_rect(hwnd, '#89b4fa')
        else:
            _hide_highlight()

def on_mouse_move(x, y):
    """拖拽时实时显示选区矩形 / 窗口选取时高亮悬停窗口（节流+合并调度）"""
    global _last_move_time, _move_pending, _move_data
    now = time.time()
    if now - _last_move_time < 0.08:  # 80ms 节流
        return
    _last_move_time = now

    with _lock:
        picking = state["picking"]
        step = state["pick_step"]
        p1 = state["pick_p1"]
        window_picking = state["window_picking"]

    # 没有任何操作模式时，完全跳过，不往主线程塞任务
    if not picking and not window_picking:
        return

    _move_data = (x, y, picking, step, p1, window_picking)
    if not _move_pending:
        _move_pending = True
        _run_on_main(_process_move)

# ====================== 键盘监听 ======================
def start_keyboard():
    def on_press(key):
        try:
            k = key.char if hasattr(key, 'char') and key.char else key.name
        except:
            return
        if k == 'f2':
            with _lock:
                if state["monitoring"]:
                    log("监测中，请先按F3停止")
                    return
                state["picking"] = True
                state["pick_step"] = 1
                state["pick_p1"] = None
            log("选区域: 按住左键拖拽框选")
            _run_on_main(_show_toast, "按住左键拖拽框选区域", '#89b4fa')
        elif k == 'f3':
            with _lock:
                picking = state["picking"]
                monitoring = state["monitoring"]
                region = state["region"]
            if picking:
                log("选区域中，先完成或按ESC取消")
                return
            if not monitoring:
                if not region:
                    log("请先按 F2 选区域!")
                    _run_on_main(_show_toast, "请先按 F2 选区域!", '#f38ba8')
                    return
                r = region
                if r[2] < 10 or r[3] < 10:
                    log("区域太小，重选!")
                    _run_on_main(_show_toast, "区域太小，请重选!", '#f38ba8')
                    return
                _start_monitoring(r, "手动")
                _run_on_main(_show_toast, "监测已开始", '#a6e3a1')
            else:
                _stop_monitoring()
                _run_on_main(_show_toast, "监测已停止", '#fab387')
        elif k == 'f4':
            manual_capture()
            _run_on_main(_show_toast, "已截图", '#a6e3a1', 1000)
        elif k == 'f5':
            # 录制到当前组
            with _lock:
                recording = state["recording"]
                gidx = state["current_group_index"]
                if gidx >= len(state["groups"]):
                    state["current_group_index"] = 0
                    gidx = 0
                name = state["groups"][gidx]["name"]
            if not recording:
                with _lock:
                    state["recording"] = True
                log(f"录制模式开启 -> [{name}] 点击画面各位置（默认延迟 {state['default_delay']:.1f}s），按 F5 结束")
                _run_on_main(_show_toast, f"录制开始 -> [{name}]", '#89b4fa')
            else:
                with _lock:
                    state["recording"] = False
                    save_groups(state["groups"])
                    n = len(state["groups"][gidx]["clicks"])
                log(f"[{name}] 录制结束，共 {n} 步")
                _run_on_main(_show_toast, f"录制结束: {n} 步", '#a6e3a1')
        elif k == 'f6':
            threading.Thread(target=play_current_group, daemon=True).start()
        elif k == 'f7':
            stop_play()
        elif k == 'f8':
            threading.Thread(target=play_all_groups, daemon=True).start()
        elif k == 'esc':
            with _lock:
                if state["picking"]:
                    state["picking"] = False
                    state["pick_step"] = 0
                    state["pick_p1"] = None
                    _run_on_main(_hide_selection_rect)
                    log("取消选区域")
                    _run_on_main(_show_toast, "已取消选区域", '#fab387', 1000)
                elif state["window_picking"]:
                    state["window_picking"] = False
                    _run_on_main(_hide_highlight)
                    log("取消选取窗口")
                    _run_on_main(_show_toast, "已取消选取窗口", '#fab387', 1000)
        elif k == 'f9':
            with _lock:
                hwnd = state["target_hwnd"]
            if hwnd:
                # 已锁定，解锁
                with _lock:
                    state["target_hwnd"] = None
                    state["window_title"] = ""
                    # 将窗口相对坐标转回屏幕绝对坐标
                    if state["region"] and is_window_visible(hwnd):
                        rect = wintypes.RECT()
                        _user32.GetWindowRect(hwnd, ctypes.byref(rect))
                        rx, ry, rw, rh = state["region"]
                        state["region"] = (rx + rect.left, ry + rect.top, rw, rh)
                log("已解锁窗口，切换回屏幕截取模式")
                _run_on_main(_show_toast, "已解锁窗口", '#fab387')
            else:
                with _lock:
                    state["window_picking"] = True
                log("选取窗口: 点击目标窗口")
                _run_on_main(_show_toast, "请点击目标窗口", '#89b4fa')
        elif k == 'f10':
            log("退出中...")
            with _lock:
                state["running"] = False
            os._exit(0)
    with keyboard.Listener(on_press=on_press) as listener:
        listener.join()

# ====================== GUI 窗口 ======================
def create_gui():
    root = tk.Tk()
    root.title("PPT扒取器 v6.0")
    root.geometry("620x720")
    root.resizable(False, False)
    root.attributes('-topmost', True)
    _set_gui_root(root)

    # ---- 配色方案 ----
    bg = "#1e1e2e"
    fg = "#cdd6f4"
    accent = "#89b4fa"
    accent2 = "#a6e3a1"
    warn = "#f38ba8"
    orange = "#fab387"
    btn_bg = "#313244"
    btn_hover = "#45475a"
    entry_bg = "#181825"
    card_bg = "#252536"
    border_color = "#45475a"
    root.configure(bg=bg)

    # ---- 工具函数 ----
    def make_button(parent, text, command, color=btn_bg, fg_color=fg, width=None, font_size=9):
        b = tk.Button(parent, text=text, command=command, bg=color, fg=fg_color,
                      activebackground=btn_hover, activeforeground=fg_color,
                      relief="flat", font=("Microsoft YaHei UI", font_size),
                      cursor="hand2", bd=0, padx=10, pady=4)
        if width:
            b.configure(width=width)
        return b

    def make_section(parent, title):
        frame = tk.Frame(parent, bg=card_bg, highlightbackground=border_color,
                         highlightthickness=1, padx=12, pady=8)
        label = tk.Label(frame, text=title, fg=accent, bg=card_bg,
                         font=("Microsoft YaHei UI", 10, "bold"), anchor="w")
        label.pack(fill=tk.X, pady=(0, 6))
        return frame

    def separator(parent):
        tk.Frame(parent, bg=border_color, height=1).pack(fill=tk.X, padx=8, pady=4)

    # ==================== 顶部状态栏 ====================
    top_bar = tk.Frame(root, bg="#11111b", pady=6)
    top_bar.pack(fill=tk.X)

    status_var = tk.StringVar(value="就绪")
    tk.Label(top_bar, text="状态", fg="#6c7086", bg="#11111b",
             font=("Microsoft YaHei UI", 9)).pack(side=tk.LEFT, padx=(12, 4))
    tk.Label(top_bar, textvariable=status_var, fg=accent2, bg="#11111b",
             font=("Microsoft YaHei UI", 9, "bold")).pack(side=tk.LEFT)

    group_name_var = tk.StringVar(value="")
    tk.Label(top_bar, text="当前组", fg="#6c7086", bg="#11111b",
             font=("Microsoft YaHei UI", 9)).pack(side=tk.LEFT, padx=(24, 4))
    tk.Label(top_bar, textvariable=group_name_var, fg=orange, bg="#11111b",
             font=("Microsoft YaHei UI", 9, "bold")).pack(side=tk.LEFT)

    # ==================== 主内容区（可滚动） ====================
    canvas = tk.Canvas(root, bg=bg, highlightthickness=0)
    scrollbar = tk.Scrollbar(root, orient="vertical", command=canvas.yview)
    scroll_frame = tk.Frame(canvas, bg=bg)

    scroll_frame.bind("<Configure>", lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
    canvas.create_window((0, 0), window=scroll_frame, anchor="nw")
    canvas.configure(yscrollcommand=scrollbar.set)

    canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
    scrollbar.pack(side=tk.RIGHT, fill=tk.Y)

    # 鼠标滚轮支持
    def _on_mousewheel(event):
        canvas.yview_scroll(int(-1 * (event.delta / 120)), "units")
    canvas.bind_all("<MouseWheel>", _on_mousewheel)

    content = tk.Frame(scroll_frame, bg=bg, padx=8, pady=8)
    content.pack(fill=tk.BOTH, expand=True)

    # ==================== 组列表区 ====================
    group_section = make_section(content, "组列表")
    group_section.pack(fill=tk.X, pady=(0, 8))

    group_frame = tk.Frame(group_section, bg=card_bg)
    group_frame.pack(fill=tk.X)

    group_listbox = tk.Listbox(group_frame, bg=entry_bg, fg=fg, selectbackground=accent,
                               selectforeground="#1e1e2e", relief="flat", font=("Consolas", 9),
                               activestyle="none", highlightthickness=0, height=5,
                               selectmode=tk.SINGLE)
    group_listbox.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

    group_scrollbar = tk.Scrollbar(group_frame, command=group_listbox.yview,
                                   bg=card_bg, troughcolor=entry_bg)
    group_scrollbar.pack(side=tk.RIGHT, fill=tk.Y)
    group_listbox.config(yscrollcommand=group_scrollbar.set)

    def refresh_group_listbox():
        group_listbox.delete(0, tk.END)
        with _lock:
            groups = list(state["groups"])
            gidx = state["current_group_index"]
        for i, g in enumerate(groups):
            marker = "▸ " if i == gidx else "  "
            nclicks = len(g.get("clicks", []))
            repeat = g.get("repeat", 1)
            nd = g.get("next_delay", 600)
            group_listbox.insert(tk.END, f"{marker}[{i+1}] {g['name']}  |  {nclicks}步  |  循环{repeat}次  |  间隔{nd:.0f}s")

    def on_group_select(_evt=None):
        sel = group_listbox.curselection()
        if sel:
            with _lock:
                state["current_group_index"] = sel[0]
            refresh_group_listbox()
            refresh_click_listbox()
            refresh_group_settings()

    group_listbox.bind('<<ListboxSelect>>', on_group_select)

    # ---- 组操作按钮行 ----
    group_btn_row = tk.Frame(group_section, bg=card_bg)
    group_btn_row.pack(fill=tk.X, pady=(6, 0))

    def add_group():
        with _lock:
            name = f"组{len(state['groups']) + 1}"
            state["groups"].append({"name": name, "clicks": [], "repeat": 1, "next_delay": 600})
            state["current_group_index"] = len(state["groups"]) - 1
            save_groups(state["groups"])
        refresh_group_listbox()
        refresh_click_listbox()
        refresh_group_settings()
        log(f"新建组: {name}")

    def delete_group():
        with _lock:
            gidx = state["current_group_index"]
            if gidx < 0 or gidx >= len(state["groups"]):
                return
            if len(state["groups"]) <= 1:
                log("至少保留一个组")
                return
            name = state["groups"][gidx]["name"]
            del state["groups"][gidx]
            if gidx >= len(state["groups"]):
                state["current_group_index"] = len(state["groups"]) - 1
            save_groups(state["groups"])
        refresh_group_listbox()
        refresh_click_listbox()
        refresh_group_settings()
        log(f"已删除组: {name}")

    def rename_group():
        with _lock:
            gidx = state["current_group_index"]
            if gidx < 0 or gidx >= len(state["groups"]):
                return
            old_name = state["groups"][gidx]["name"]

        dlg = tk.Toplevel(root)
        dlg.title("重命名组")
        dlg.geometry("300x120")
        dlg.resizable(False, False)
        dlg.configure(bg=card_bg)
        dlg.attributes('-topmost', True)
        dlg.transient(root)
        dlg.grab_set()

        tk.Label(dlg, text="组名称:", fg=fg, bg=card_bg,
                 font=("Microsoft YaHei UI", 10)).pack(pady=(14, 6))
        var = tk.StringVar(value=old_name)
        entry = tk.Entry(dlg, textvariable=var, width=25, bg=entry_bg, fg=fg,
                         insertbackground=fg, relief="flat", font=("Microsoft YaHei UI", 10))
        entry.pack()
        entry.select_range(0, tk.END)
        entry.focus_set()

        def confirm():
            new_name = var.get().strip()
            if new_name:
                with _lock:
                    if gidx < len(state["groups"]):
                        state["groups"][gidx]["name"] = new_name
                        save_groups(state["groups"])
                refresh_group_listbox()
                refresh_click_listbox()
                log(f"组已重命名: {old_name} -> {new_name}")
            dlg.destroy()

        make_button(dlg, "确定", confirm, color=accent, fg_color="#1e1e2e").pack(pady=8)
        dlg.bind('<Return>', lambda _: confirm())

    def move_group_up():
        with _lock:
            gidx = state["current_group_index"]
            if gidx <= 0 or gidx >= len(state["groups"]):
                return
            state["groups"][gidx], state["groups"][gidx - 1] = state["groups"][gidx - 1], state["groups"][gidx]
            state["current_group_index"] = gidx - 1
            save_groups(state["groups"])
        refresh_group_listbox()
        log(f"组上移: {state['groups'][gidx-1]['name']}")

    def move_group_down():
        with _lock:
            gidx = state["current_group_index"]
            if gidx < 0 or gidx >= len(state["groups"]) - 1:
                return
            state["groups"][gidx], state["groups"][gidx + 1] = state["groups"][gidx + 1], state["groups"][gidx]
            state["current_group_index"] = gidx + 1
            save_groups(state["groups"])
        refresh_group_listbox()
        log(f"组下移: {state['groups'][gidx-1]['name']}")

    make_button(group_btn_row, "+ 新建组", add_group, color="#2d5a3d", fg_color=accent2).pack(side=tk.LEFT, padx=(0, 4))
    make_button(group_btn_row, "删除组", delete_group, color="#5a2d2d", fg_color=warn).pack(side=tk.LEFT, padx=4)
    make_button(group_btn_row, "重命名", rename_group).pack(side=tk.LEFT, padx=4)
    make_button(group_btn_row, "▲", move_group_up, width=3).pack(side=tk.LEFT, padx=4)
    make_button(group_btn_row, "▼", move_group_down, width=3).pack(side=tk.LEFT, padx=4)

    # ==================== 点击序列区 ====================
    click_section = make_section(content, "点击序列")
    click_section.pack(fill=tk.X, pady=(0, 8))

    click_frame = tk.Frame(click_section, bg=card_bg)
    click_frame.pack(fill=tk.X)

    click_listbox = tk.Listbox(click_frame, bg=entry_bg, fg=fg, selectbackground=accent,
                               selectforeground="#1e1e2e", relief="flat", font=("Consolas", 9),
                               activestyle="none", highlightthickness=0, height=5,
                               selectmode=tk.SINGLE)
    click_listbox.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

    click_scrollbar = tk.Scrollbar(click_frame, command=click_listbox.yview,
                                   bg=card_bg, troughcolor=entry_bg)
    click_scrollbar.pack(side=tk.RIGHT, fill=tk.Y)
    click_listbox.config(yscrollcommand=click_scrollbar.set)

    def refresh_click_listbox():
        click_listbox.delete(0, tk.END)
        with _lock:
            gidx = state["current_group_index"]
            if gidx < 0 or gidx >= len(state["groups"]):
                return
            clicks = list(state["groups"][gidx].get("clicks", []))
        for i, step in enumerate(clicks):
            delay = step.get("delay", 1.0)
            click_listbox.insert(tk.END, f"  #{i+1:>2}  ({step['x']:>4}, {step['y']:>4})  延迟 {delay:.1f}s")

    # ---- 点击编辑按钮行 ----
    click_btn_row = tk.Frame(click_section, bg=card_bg)
    click_btn_row.pack(fill=tk.X, pady=(6, 0))

    def delete_click():
        sel = click_listbox.curselection()
        if not sel:
            return
        idx = sel[0]
        with _lock:
            gidx = state["current_group_index"]
            if 0 <= gidx < len(state["groups"]) and 0 <= idx < len(state["groups"][gidx]["clicks"]):
                del state["groups"][gidx]["clicks"][idx]
                save_groups(state["groups"])
        refresh_click_listbox()
        refresh_group_listbox()
        log(f"已删除第 {idx+1} 步")

    def move_click_up():
        sel = click_listbox.curselection()
        if not sel or sel[0] == 0:
            return
        idx = sel[0]
        with _lock:
            gidx = state["current_group_index"]
            if gidx < len(state["groups"]):
                clicks = state["groups"][gidx]["clicks"]
                clicks[idx], clicks[idx-1] = clicks[idx-1], clicks[idx]
                save_groups(state["groups"])
        refresh_click_listbox()
        click_listbox.selection_set(idx - 1)
        log(f"第 {idx+1} 步上移")

    def move_click_down():
        sel = click_listbox.curselection()
        if not sel:
            return
        idx = sel[0]
        with _lock:
            gidx = state["current_group_index"]
            if gidx < len(state["groups"]) and idx < len(state["groups"][gidx]["clicks"]) - 1:
                clicks = state["groups"][gidx]["clicks"]
                clicks[idx], clicks[idx+1] = clicks[idx+1], clicks[idx]
                save_groups(state["groups"])
        refresh_click_listbox()
        click_listbox.selection_set(idx + 1)
        log(f"第 {idx+1} 步下移")

    def edit_click_delay():
        sel = click_listbox.curselection()
        if not sel:
            return
        idx = sel[0]
        with _lock:
            gidx = state["current_group_index"]
            if gidx >= len(state["groups"]) or idx >= len(state["groups"][gidx]["clicks"]):
                return
            current = state["groups"][gidx]["clicks"][idx].get("delay", 1.0)

        dlg = tk.Toplevel(root)
        dlg.title("编辑延迟")
        dlg.geometry("240x120")
        dlg.resizable(False, False)
        dlg.configure(bg=card_bg)
        dlg.attributes('-topmost', True)
        dlg.transient(root)
        dlg.grab_set()

        tk.Label(dlg, text=f"第 {idx+1} 步延迟 (秒):", fg=fg, bg=card_bg,
                 font=("Microsoft YaHei UI", 10)).pack(pady=(14, 6))
        var = tk.StringVar(value=str(current))
        entry = tk.Entry(dlg, textvariable=var, width=8, bg=entry_bg, fg=fg,
                         insertbackground=fg, relief="flat", font=("Microsoft YaHei UI", 10))
        entry.pack()
        entry.select_range(0, tk.END)
        entry.focus_set()

        def confirm():
            try:
                d = float(var.get())
                if d < 0.1:
                    d = 0.1
                with _lock:
                    gidx2 = state["current_group_index"]
                    if gidx2 < len(state["groups"]) and idx < len(state["groups"][gidx2]["clicks"]):
                        state["groups"][gidx2]["clicks"][idx]["delay"] = d
                        save_groups(state["groups"])
                refresh_click_listbox()
                log(f"第 {idx+1} 步延迟已改为 {d:.1f}s")
            except ValueError:
                pass
            dlg.destroy()

        make_button(dlg, "确定", confirm, color=accent, fg_color="#1e1e2e").pack(pady=8)
        dlg.bind('<Return>', lambda _: confirm())

    def clear_clicks():
        with _lock:
            gidx = state["current_group_index"]
            if gidx < len(state["groups"]):
                state["groups"][gidx]["clicks"] = []
                save_groups(state["groups"])
        refresh_click_listbox()
        refresh_group_listbox()
        log("已清空当前组点击序列")

    make_button(click_btn_row, "▲ 上移", move_click_up).pack(side=tk.LEFT, padx=(0, 4))
    make_button(click_btn_row, "▼ 下移", move_click_down).pack(side=tk.LEFT, padx=4)
    make_button(click_btn_row, "删除", delete_click, color="#5a2d2d", fg_color=warn).pack(side=tk.LEFT, padx=4)
    make_button(click_btn_row, "改延迟", edit_click_delay).pack(side=tk.LEFT, padx=4)
    make_button(click_btn_row, "清空", clear_clicks, color="#5a2d2d", fg_color=warn).pack(side=tk.LEFT, padx=4)

    # ==================== 设置区 ====================
    settings_section = make_section(content, "设置")
    settings_section.pack(fill=tk.X, pady=(0, 8))

    # ---- 默认延迟 ----
    delay_row = tk.Frame(settings_section, bg=card_bg)
    delay_row.pack(fill=tk.X, pady=(0, 6))

    tk.Label(delay_row, text="录制默认延迟 (秒):", fg=fg, bg=card_bg,
             font=("Microsoft YaHei UI", 9)).pack(side=tk.LEFT)
    delay_var = tk.StringVar(value=str(state["default_delay"]))
    delay_entry = tk.Entry(delay_row, textvariable=delay_var, width=5,
                           bg=entry_bg, fg=fg, insertbackground=fg,
                           relief="flat", font=("Consolas", 9))
    delay_entry.pack(side=tk.LEFT, padx=6)

    def apply_default_delay():
        try:
            d = float(delay_var.get())
            if d < 0.1:
                d = 0.1
            with _lock:
                state["default_delay"] = d
            _config["default_delay"] = d
            save_config(_config)
            log(f"默认延迟已设为 {d:.1f}s")
        except ValueError:
            pass

    make_button(delay_row, "应用", apply_default_delay, color=accent, fg_color="#1e1e2e",
                font_size=8).pack(side=tk.LEFT, padx=4)

    # ---- 当前组设置 ----
    group_settings_row = tk.Frame(settings_section, bg=card_bg)
    group_settings_row.pack(fill=tk.X, pady=(0, 6))

    tk.Label(group_settings_row, text="本组循环次数:", fg=fg, bg=card_bg,
             font=("Microsoft YaHei UI", 9)).pack(side=tk.LEFT)
    repeat_var = tk.StringVar(value="1")
    tk.Entry(group_settings_row, textvariable=repeat_var, width=4,
             bg=entry_bg, fg=fg, insertbackground=fg,
             relief="flat", font=("Consolas", 9)).pack(side=tk.LEFT, padx=4)

    tk.Label(group_settings_row, text="到下一组延迟 (秒):", fg=fg, bg=card_bg,
             font=("Microsoft YaHei UI", 9)).pack(side=tk.LEFT, padx=(12, 0))
    next_delay_var = tk.StringVar(value="600")
    tk.Entry(group_settings_row, textvariable=next_delay_var, width=6,
             bg=entry_bg, fg=fg, insertbackground=fg,
             relief="flat", font=("Consolas", 9)).pack(side=tk.LEFT, padx=4)

    def apply_group_settings():
        try:
            r = int(repeat_var.get())
            if r < 1:
                r = 1
        except ValueError:
            return
        try:
            nd = float(next_delay_var.get())
            if nd < 0:
                nd = 0
        except ValueError:
            return
        with _lock:
            gidx = state["current_group_index"]
            if gidx < len(state["groups"]):
                state["groups"][gidx]["repeat"] = r
                state["groups"][gidx]["next_delay"] = nd
                save_groups(state["groups"])
                name = state["groups"][gidx]["name"]
        refresh_group_listbox()
        log(f"[{name}] 循环{r}次 / 间隔{nd:.0f}s")

    make_button(group_settings_row, "应用", apply_group_settings, color=accent, fg_color="#1e1e2e",
                font_size=8).pack(side=tk.LEFT, padx=6)

    def refresh_group_settings():
        with _lock:
            gidx = state["current_group_index"]
            if gidx < 0 or gidx >= len(state["groups"]):
                return
            g = state["groups"][gidx]
        repeat_var.set(str(g.get("repeat", 1)))
        next_delay_var.set(str(g.get("next_delay", 600)))
        group_name_var.set(g.get("name", ""))

    # ---- 全局组循环 ----
    loop_row = tk.Frame(settings_section, bg=card_bg)
    loop_row.pack(fill=tk.X, pady=(0, 4))

    loop_var = tk.BooleanVar(value=state["global_group_loop"])

    def toggle_global_loop():
        v = loop_var.get()
        with _lock:
            state["global_group_loop"] = v
        _config["global_group_loop"] = v
        save_config(_config)
        log(f"全局组循环: {'开' if v else '关'}")

    tk.Checkbutton(loop_row, text="全局组循环 (A→B→C→A→B→C...)", variable=loop_var,
                   command=toggle_global_loop, fg=fg, bg=card_bg, selectcolor=entry_bg,
                   activebackground=card_bg, activeforeground=fg,
                   font=("Microsoft YaHei UI", 9)).pack(side=tk.LEFT)

    # ---- 定时间隔 ----
    timer_row = tk.Frame(settings_section, bg=card_bg)
    timer_row.pack(fill=tk.X, pady=(0, 4))

    tk.Label(timer_row, text="定时触发间隔 (分钟):", fg=fg, bg=card_bg,
             font=("Microsoft YaHei UI", 9)).pack(side=tk.LEFT)
    interval_var = tk.StringVar(value=str(state["interval_seconds"] // 60))
    tk.Entry(timer_row, textvariable=interval_var, width=4,
             bg=entry_bg, fg=fg, insertbackground=fg,
             relief="flat", font=("Consolas", 9)).pack(side=tk.LEFT, padx=6)

    def apply_interval():
        try:
            mins = int(interval_var.get())
            if mins < 1:
                mins = 1
            secs = mins * 60
            with _lock:
                state["interval_seconds"] = secs
                state["countdown_seconds"] = secs
            _config["interval_minutes"] = mins
            save_config(_config)
            log(f"定时间隔已设为 {mins} 分钟")
        except ValueError:
            pass

    make_button(timer_row, "应用", apply_interval, color=accent, fg_color="#1e1e2e",
                font_size=8).pack(side=tk.LEFT, padx=4)

    # ==================== 倒计时区 ====================
    timer_section = make_section(content, "倒计时")
    timer_section.pack(fill=tk.X, pady=(0, 8))

    countdown_var = tk.StringVar(value="--:--")
    cd_label = tk.Label(timer_section, textvariable=countdown_var, fg=accent, bg=card_bg,
                        font=("Consolas", 36, "bold"))
    cd_label.pack(pady=(4, 8))

    timer_btn_row = tk.Frame(timer_section, bg=card_bg)
    timer_btn_row.pack(fill=tk.X)

    def toggle_timer():
        with _lock:
            active = state["timer_active"]
        if active:
            with _lock:
                state["timer_active"] = False
            log("倒计时已暂停")
        else:
            with _lock:
                state["timer_active"] = True
            log("倒计时已启动")

    def exec_reset_timer():
        with _lock:
            state["countdown_seconds"] = state["interval_seconds"]
        log("倒计时已重置")

    make_button(timer_btn_row, "启/停定时", toggle_timer, width=10).pack(side=tk.LEFT, padx=(0, 6))
    make_button(timer_btn_row, "重置计时", exec_reset_timer, width=10).pack(side=tk.LEFT, padx=6)

    # ==================== 执行按钮区 ====================
    exec_section = make_section(content, "执行控制")
    exec_section.pack(fill=tk.X, pady=(0, 8))

    exec_btn_row1 = tk.Frame(exec_section, bg=card_bg)
    exec_btn_row1.pack(fill=tk.X, pady=(0, 6))

    make_button(exec_btn_row1, "▶ 执行当前组",
                lambda: threading.Thread(target=play_current_group, daemon=True).start(),
                color="#1e3a5f", fg_color=accent, width=14).pack(side=tk.LEFT, padx=(0, 8))
    make_button(exec_btn_row1, "▶▶ 执行全部组",
                lambda: threading.Thread(target=play_all_groups, daemon=True).start(),
                color="#1e4a2e", fg_color=accent2, width=14).pack(side=tk.LEFT, padx=8)
    make_button(exec_btn_row1, "■ 停止", stop_play,
                color="#5a2d2d", fg_color=warn, width=8).pack(side=tk.LEFT, padx=8)

    # ==================== 窗口锁定区 ====================
    window_section = make_section(content, "窗口锁定")
    window_section.pack(fill=tk.X, pady=(0, 8))

    window_info_var = tk.StringVar(value="未锁定 (屏幕截取模式)")

    window_info_label = tk.Label(window_section, textvariable=window_info_var, fg=fg, bg=card_bg,
                                  font=("Microsoft YaHei UI", 9), anchor="w", wraplength=500)
    window_info_label.pack(fill=tk.X, pady=(0, 6))

    window_btn_row = tk.Frame(window_section, bg=card_bg)
    window_btn_row.pack(fill=tk.X)

    def gui_pick_window():
        with _lock:
            state["window_picking"] = True
        log("选取窗口: 点击目标窗口")

    def gui_unlock_window():
        with _lock:
            hwnd = state["target_hwnd"]
            if hwnd:
                state["target_hwnd"] = None
                state["window_title"] = ""
                if state["region"] and is_window_visible(hwnd):
                    rect = wintypes.RECT()
                    _user32.GetWindowRect(hwnd, ctypes.byref(rect))
                    rx, ry, rw, rh = state["region"]
                    state["region"] = (rx + rect.left, ry + rect.top, rw, rh)
        log("已解锁窗口，切换回屏幕截取模式")

    make_button(window_btn_row, "锁定窗口 (F9)", gui_pick_window,
                color="#1e3a5f", fg_color=accent).pack(side=tk.LEFT, padx=(0, 6))
    make_button(window_btn_row, "解锁窗口", gui_unlock_window,
                color="#5a2d2d", fg_color=warn).pack(side=tk.LEFT, padx=6)

    # ==================== 快捷键提示 ====================
    tips_frame = tk.Frame(content, bg=bg)
    tips_frame.pack(fill=tk.X, pady=(4, 0))

    tips = "F2 框选  |  F3 监测  |  F4 截图  |  F5 录制  |  F6 当前组  |  F8 全部组  |  F7 停止  |  F9 锁定窗口  |  F10 退出"
    tk.Label(tips_frame, text=tips, fg="#6c7086", bg=bg,
             font=("Microsoft YaHei UI", 8)).pack()

    # ---- UI 刷新 ----
    def update_ui():
        with _lock:
            monitoring = state["monitoring"]
            recording = state["recording"]
            playing = state["playing"]
            playing_all = state["playing_all"]
            timer_active = state["timer_active"]
            cd = state["countdown_seconds"]
            gidx = state["current_group_index"]
            hwnd = state["target_hwnd"]
            win_title = state["window_title"]
            win_picking = state["window_picking"]
            if gidx < len(state["groups"]):
                cur_name = state["groups"][gidx]["name"]
            else:
                cur_name = "无"

        if recording:
            status_var.set(f"录制中 -> {cur_name}")
        elif playing_all:
            status_var.set("执行全部组中")
        elif playing:
            status_var.set(f"执行当前组 -> {cur_name}")
        elif monitoring:
            status_var.set("监测中")
        else:
            status_var.set("就绪")

        group_name_var.set(cur_name)

        # 更新窗口锁定状态
        if win_picking:
            window_info_var.set("请点击目标窗口...")
        elif hwnd and is_window_visible(hwnd):
            window_info_var.set(f"已锁定: [{win_title}] (窗口捕获模式，可遮挡)")
        elif hwnd:
            window_info_var.set("锁定窗口已关闭，请重新锁定")
        else:
            window_info_var.set("未锁定 (屏幕截取模式)")

        if timer_active:
            m = cd // 60
            s = cd % 60
            countdown_var.set(f"{m:02d}:{s:02d}")
        else:
            countdown_var.set("--:--")

        # 定期刷新列表
        if int(time.time() * 2) % 4 == 0:
            gcur = group_listbox.size()
            ccur = click_listbox.size()
            with _lock:
                gactual = len(state["groups"])
                gidx2 = state["current_group_index"]
                if gidx2 < len(state["groups"]):
                    cactual = len(state["groups"][gidx2].get("clicks", []))
                else:
                    cactual = 0
            if gcur != gactual:
                refresh_group_listbox()
            if ccur != cactual:
                refresh_click_listbox()

        root.after(500, update_ui)

    # ---- 倒计时 tick ----
    def countdown_tick():
        with _lock:
            if state["timer_active"] and state["running"] and not state["playing"]:
                state["countdown_seconds"] -= 1
                if state["countdown_seconds"] <= 0:
                    state["countdown_seconds"] = state["interval_seconds"]
                    threading.Thread(target=play_all_groups, daemon=True).start()
                    log(f"定时触发全部组 (间隔 {state['interval_seconds']//60} 分钟)")
        root.after(1000, countdown_tick)

    refresh_group_listbox()
    refresh_click_listbox()
    refresh_group_settings()
    update_ui()
    countdown_tick()

    def on_close():
        log("退出中...")
        with _lock:
            state["running"] = False
        root.destroy()
        os._exit(0)

    root.protocol("WM_DELETE_WINDOW", on_close)
    return root

# ====================== 主入口 ======================
def main():
    os.makedirs(OUTPUT_DIR, exist_ok=True)

    mouse_listener = mouse.Listener(on_click=on_mouse_click, on_move=on_mouse_move)
    mouse_listener.start()

    kb_thread = threading.Thread(target=start_keyboard, daemon=True)
    kb_thread.start()

    mins = state["interval_seconds"] // 60
    d = state["default_delay"]
    lp = "开" if state["global_group_loop"] else "关"
    log(f"已加载 {len(state['groups'])} 个组 | 默认延迟 {d:.1f}s | 全局循环={lp} | 定时间隔 {mins} 分钟")
    for i, g in enumerate(state["groups"]):
        log(f"  [{i+1}] {g['name']}: {len(g['clicks'])}步 循环{g['repeat']}次 间隔{g['next_delay']:.0f}s")
    log("F2=选区域 | F3=监测 | F4=截图 | F5=录制 | F6=执行当前组 | F8=执行全部组 | F7=停止 | F9=锁定窗口 | F10=退出")

    root = create_gui()
    root.mainloop()

if __name__ == "__main__":
    main()
