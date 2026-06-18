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
import re
from datetime import datetime
from pynput import keyboard, mouse
from ctypes import wintypes
from PIL import ImageGrab, Image

try:
    import pytesseract
except Exception:
    pytesseract = None

TESSERACT_CMD = r"C:\Program Files\Tesseract-OCR\tesseract.exe"
TESSDATA_DIR = r"C:\Program Files\Tesseract-OCR\tessdata"
if pytesseract is not None and os.path.exists(TESSERACT_CMD):
    pytesseract.pytesseract.tesseract_cmd = TESSERACT_CMD

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
    """PrintWindow 捕获窗口内容（可后台截图）。黑屏则快速重试，仍黑屏返回 None。"""
    rect = wintypes.RECT()
    _user32.GetWindowRect(hwnd, ctypes.byref(rect))
    w = rect.right - rect.left
    h = rect.bottom - rect.top
    if w <= 0 or h <= 0:
        return None

    # 预先获取 DC 和位图对象，避免每次重试都重建
    hwndDC = _user32.GetWindowDC(hwnd)
    if not hwndDC:
        return None
    memDC = _gdi32.CreateCompatibleDC(hwndDC)
    hBmp = _gdi32.CreateCompatibleBitmap(hwndDC, w, h)
    _gdi32.SelectObject(memDC, hBmp)

    try:
        for i in range(3):
            _user32.PrintWindow(hwnd, memDC, 2)
            img = _capture_to_array(memDC, hBmp, w, h)
            if np.mean(img) >= 5:
                return img
            # 黑屏再试一次 flag=0
            _user32.PrintWindow(hwnd, memDC, 0)
            img = _capture_to_array(memDC, hBmp, w, h)
            if np.mean(img) >= 5:
                return img
            time.sleep(0.03)
        return None
    finally:
        _gdi32.DeleteObject(hBmp)
        _gdi32.DeleteDC(memDC)
        _user32.ReleaseDC(hwnd, hwndDC)

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

def _sanitize_name(name):
    """清理为 Windows 可用的文件/目录名"""
    name = str(name or "").strip()
    name = re.sub(r'[\\/:*?"<>|]+', "-", name)
    name = re.sub(r"\s+", "_", name)
    return name.strip(" ._") or datetime.now().strftime("%Y%m%d_%H%M%S")

def _is_black_frame(img):
    if img is None or img.size == 0:
        return True
    return float(np.mean(img)) < BLACK_MEAN_THRESHOLD and float(np.std(img)) < BLACK_STD_THRESHOLD

def _infer_duration_from_name(name):
    """从组名里的 10-09-10-55 / 10:09-10:55 推断秒数"""
    text = str(name or "")
    m = re.search(r"(\d{1,2})[:：-](\d{2})\s*[-~至到]\s*(\d{1,2})[:：-](\d{2})", text)
    if not m:
        m = re.search(r"(\d{1,2})[:：-](\d{2})[-_](\d{1,2})[:：-](\d{2})", text)
    if not m:
        return None
    h1, m1, h2, m2 = map(int, m.groups())
    start = h1 * 60 + m1
    end = h2 * 60 + m2
    if end <= start:
        end += 24 * 60
    duration = (end - start) * 60
    return duration if duration > 0 else None

def _get_group_duration(group):
    name = group.get("name", "")
    inferred = _infer_duration_from_name(name)
    try:
        explicit = float(group.get("duration_seconds", 0))
        if explicit > 0 and (explicit != 600 or inferred is None):
            return explicit
    except (TypeError, ValueError):
        pass
    if inferred:
        return inferred
    for key in ("duration_seconds", "next_delay"):
        try:
            value = float(group.get(key, 0))
            if value > 0:
                return value
        except (TypeError, ValueError):
            pass
    return 600

def _export_session_pdf(out_dir, label=None):
    """把当前会话内的 slide_*.png 合成 PDF"""
    try:
        slides = sorted(
            os.path.join(out_dir, f)
            for f in os.listdir(out_dir)
            if f.lower().startswith("slide_") and f.lower().endswith(".png")
        )
        if not slides:
            return None
        base = _sanitize_name(label or os.path.basename(out_dir))
        pdf_path = os.path.join(out_dir, f"{base}.pdf")
        images = []
        for path in slides:
            img = Image.open(path).convert("RGB")
            images.append(img)
        first, rest = images[0], images[1:]
        first.save(pdf_path, save_all=True, append_images=rest)
        log(f"已导出 PDF -> {os.path.basename(pdf_path)} ({len(slides)}页)")
        for img in images:
            img.close()
        return pdf_path
    except Exception as e:
        log(f"PDF 导出失败: {e}")
        return None

OUTPUT_DIR = r"D:\Work_Place\ppt-takeaway\ppt_slides"
CHANGE_THRESHOLD = 8
CHECK_INTERVAL = 0.3
HASH_SIMILARITY = 0.95
STABLE_FRAMES = 1
TEMPLATE_SIZE = 120  # 模板图片边长（像素）
TEMPLATE_MATCH_THRESHOLD = 0.65  # 匹配置信度阈值
CLICK_RETRY_GAP = 0.25
CLICK_SETTLE_DELAY = 0.15
CLICK_HOLD_SECONDS = 0.06
def _available_ocr_langs():
    langs = set()
    if os.path.isdir(TESSDATA_DIR):
        for name in os.listdir(TESSDATA_DIR):
            if name.endswith(".traineddata"):
                langs.add(os.path.splitext(name)[0])
    return langs

def _preferred_ocr_lang():
    langs = _available_ocr_langs()
    if "chi_sim" in langs and "eng" in langs:
        return "chi_sim+eng"
    if "chi_sim" in langs:
        return "chi_sim"
    if "eng" in langs:
        return "eng"
    return "eng"

OCR_LANG = _preferred_ocr_lang()
SMART_CLICK_KEYWORDS_BY_STEP = [
    ["切换节次", "切换"],
    ["课表录制", "课堂回放", "回放", "录制"],
    ["进入回放", "回放", "播放"],
]
OCR_SCALE = 2.0
OCR_MIN_CONFIDENCE = 25
OCR_SCROLL_TRIES = 8
OCR_SCROLL_AMOUNT = -5
DEFAULT_SIMPLE_WEEKS = "1-16"
SIMPLE_LESSONS = (1, 2)

GROUPS_FILE = os.path.join(OUTPUT_DIR, "groups.json")
CONFIG_FILE = os.path.join(OUTPUT_DIR, "config.json")
OLD_CLICK_FILE = os.path.join(OUTPUT_DIR, "click_sequence.json")
TEMPLATE_DIR = os.path.join(OUTPUT_DIR, "templates")
BLACK_MEAN_THRESHOLD = 5
BLACK_STD_THRESHOLD = 3
MIN_VALID_CAPTURE_RETRIES = 3

def log(msg):
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    t = datetime.now().strftime("%H:%M:%S")
    with open(os.path.join(OUTPUT_DIR, "ppt_log.txt"), "a", encoding="utf-8") as f:
        f.write(f"[{t}] {msg}\n")
    safe_msg = msg.encode("gbk", errors="replace").decode("gbk")
    print(f"[{t}] {safe_msg}")

def _save_debug_screenshot(label="debug"):
    """保存当前锁定窗口截图到输出目录，用于排查定位问题。（已禁用）"""
    pass

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
                    g.setdefault("duration_seconds", _infer_duration_from_name(g.get("name", "")) or g.get("next_delay", 600))
                    if "clicks" in g and g["clicks"] and isinstance(g["clicks"][0], list):
                        g["clicks"] = [{"x": c[0], "y": c[1], "delay": 1.0} for c in g["clicks"]]
                return groups
        except Exception:
            pass
    return [{"name": "默认组", "clicks": [], "repeat": 1, "next_delay": 600, "duration_seconds": 600}]

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
    return {"interval_minutes": 30, "default_delay": 1.0, "global_group_loop": True, "simple_click_profile": []}

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
    "session_label": "",
    "target_hwnd": None,    # 锁定的窗口句柄（None=屏幕截取模式）
    "window_picking": False, # 是否正在选取窗口
    "window_title": "",     # 锁定窗口的标题
}
_lock = threading.Lock()

# 确保至少有一个组
if not state["groups"]:
    state["groups"] = [{"name": "默认组", "clicks": [], "repeat": 1, "next_delay": 600, "duration_seconds": 600}]
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
        log("请先按 Alt+Q 选择区域!")
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
                if _is_black_frame(img):
                    log("手动截图(窗口): 黑屏，未保存")
                    return
                cv2_imwrite(os.path.join(out_dir, fn), img)
                log(f"手动截图(窗口) -> {fn} ({w}x{h})")
            else:
                log("窗口捕获失败，窗口可能已最小化")
        else:
            frame = None
            for _ in range(MIN_VALID_CAPTURE_RETRIES):
                pil_img = ImageGrab.grab(bbox=(x, y, x+w, y+h))
                candidate = cv2.cvtColor(np.array(pil_img), cv2.COLOR_RGB2BGR)
                if not _is_black_frame(candidate):
                    frame = candidate
                    break
                time.sleep(0.05)
            if frame is None:
                log("手动截图: 黑屏，未保存")
                return
            cv2_imwrite(os.path.join(out_dir, fn), frame)
            log(f"手动截图 -> {fn} ({w}x{h})")
    except Exception as e:
        log(f"截图失败: {e}")

# ====================== 监测会话管理 ======================
def _make_session_dir(label):
    """为监测会话创建独立子目录，优先使用组名/课程名"""
    base = _sanitize_name(label)
    name = base
    idx = 2
    while os.path.exists(os.path.join(OUTPUT_DIR, name)):
        name = f"{base}_{idx:02d}"
        idx += 1
    full = os.path.join(OUTPUT_DIR, name)
    os.makedirs(full, exist_ok=True)
    return full

def _start_monitoring(region, label="手动"):
    """创建会话目录并启动监测"""
    session_dir = _make_session_dir(label)
    with _lock:
        state["session_dir"] = session_dir
        state["session_label"] = label
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

# ====================== 模板匹配定位 ======================
def _capture_template_at(x, y, size=TEMPLATE_SIZE):
    """在点击位置截取一小块图片作为模板，返回模板路径或 None"""
    try:
        os.makedirs(TEMPLATE_DIR, exist_ok=True)
        half = size // 2
        bbox = (x - half, y - half, x + half, y + half)
        pil_img = ImageGrab.grab(bbox=bbox)
        ts = datetime.now().strftime("%Y%m%d%H%M%S")
        fname = f"tpl_{ts}_{x}_{y}.png"
        fpath = os.path.join(TEMPLATE_DIR, fname)
        pil_img.save(fpath)
        return fname
    except Exception as e:
        log(f"模板截图失败: {e}")
        return None

def _locked_window_region():
    with _lock:
        hwnd = state.get("target_hwnd")
    if hwnd and is_window_visible(hwnd):
        rect = wintypes.RECT()
        _user32.GetWindowRect(hwnd, ctypes.byref(rect))
        w = rect.right - rect.left
        h = rect.bottom - rect.top
        if w > 0 and h > 0:
            return (rect.left, rect.top, w, h)
    return None

def _grab_ocr_image(search_region=None):
    """返回 (PIL图像, offset_x, offset_y)。无指定区域时优先读取锁定窗口。"""
    if search_region:
        sx, sy, sw, sh = search_region
        return ImageGrab.grab(bbox=(sx, sy, sx + sw, sy + sh)), sx, sy

    with _lock:
        hwnd = state.get("target_hwnd")
    if hwnd and is_window_visible(hwnd):
        rect = wintypes.RECT()
        _user32.GetWindowRect(hwnd, ctypes.byref(rect))
        img = capture_window(hwnd)
        if img is not None and not _is_black_frame(img):
            rgb = cv2.cvtColor(img, cv2.COLOR_BGR2RGB)
            return Image.fromarray(rgb), rect.left, rect.top

    return ImageGrab.grab(), 0, 0

def _find_target(template_fname, search_region=None, threshold=TEMPLATE_MATCH_THRESHOLD):
    """用模板匹配在屏幕上找到目标位置，返回 (x, y) 或 None"""
    tpl_path = os.path.join(TEMPLATE_DIR, template_fname)
    if not os.path.exists(tpl_path):
        return None
    try:
        template = cv2.imread(tpl_path, cv2.IMREAD_COLOR)
        if template is None:
            return None
        th, tw = template.shape[:2]

        screen_pil, offset_x, offset_y = _grab_ocr_image(search_region)
        screen = cv2.cvtColor(np.array(screen_pil), cv2.COLOR_RGB2BGR)

        result = cv2.matchTemplate(screen, template, cv2.TM_CCOEFF_NORMED)
        _, max_val, _, max_loc = cv2.minMaxLoc(result)

        if max_val >= threshold:
            cx = int(max_loc[0] + tw / 2) + offset_x
            cy = int(max_loc[1] + th / 2) + offset_y
            log(f"  模板匹配成功: 置信度={max_val:.2f} -> ({cx},{cy})")
            return (cx, cy)
        else:
            log(f"  模板匹配失败: 置信度={max_val:.2f} < {threshold}")
            return None
    except Exception as e:
        log(f"  模板匹配异常: {e}")
        return None

def _split_keywords(value):
    if not value:
        return []
    if isinstance(value, str):
        parts = re.split(r"[,，;；|/\s]+", value)
    else:
        parts = []
        for item in value:
            parts.extend(re.split(r"[,，;；|/\s]+", str(item)))
    return [p.strip() for p in parts if p and p.strip()]

def _default_target_text_for_step(index):
    if 0 <= index < len(SMART_CLICK_KEYWORDS_BY_STEP):
        return list(SMART_CLICK_KEYWORDS_BY_STEP[index])
    return []

def _keywords_from_group_name(name):
    text = str(name or "")
    keys = []
    date_match = re.search(r"(\d{1,2})[-_/\.](\d{1,2})", text)
    if date_match:
        mm, dd = date_match.groups()
        keys.extend([f"{int(mm):02d}-{int(dd):02d}", f"{int(mm)}-{int(dd)}"])
    time_match = re.search(r"(\d{1,2})[:：-](\d{2})\s*[-_~至到]\s*(\d{1,2})[:：-](\d{2})", text)
    if time_match:
        h1, m1, h2, m2 = map(int, time_match.groups())
        keys.extend([
            f"{h1:02d}:{m1:02d}", f"{h1:02d}-{m1:02d}",
            f"{h2:02d}:{m2:02d}", f"{h2:02d}-{m2:02d}",
        ])
    return list(dict.fromkeys(keys))

def _parse_week_spec(text):
    """解析 1-4,7,9-12 / 第1周-第4周 这类输入。"""
    raw = str(text or "").strip()
    if not raw:
        return []
    weeks = []
    for part in re.split(r"[,，;；\s]+", raw):
        if not part:
            continue
        nums = [int(n) for n in re.findall(r"\d+", part)]
        if not nums:
            continue
        if len(nums) >= 2 and any(sep in part for sep in ("-", "~", "至", "到")):
            a, b = nums[0], nums[1]
            if a <= b:
                weeks.extend(range(a, b + 1))
            else:
                weeks.extend(range(a, b - 1, -1))
        else:
            weeks.append(nums[0])
    return sorted(dict.fromkeys(w for w in weeks if w > 0))

def _scan_completed_week_lessons():
    """扫描输出目录，返回 {(week, lesson)} 和 {week}。"""
    completed_lessons = set()
    completed_weeks = set()
    if not os.path.isdir(OUTPUT_DIR):
        return completed_lessons, completed_weeks
    try:
        names = [
            name for name in os.listdir(OUTPUT_DIR)
            if os.path.isdir(os.path.join(OUTPUT_DIR, name)) or name.lower().endswith(".pdf")
        ]
    except Exception:
        return completed_lessons, completed_weeks
    for name in names:
        week_match = re.search(r"第?\s*(\d{1,2})\s*周", name)
        if not week_match:
            continue
        week = int(week_match.group(1))
        completed_weeks.add(week)
        lesson_match = re.search(r"第?\s*([一二12])\s*(?:节|课)", name)
        if lesson_match:
            token = lesson_match.group(1)
            lesson = 1 if token in ("一", "1") else 2
            completed_lessons.add((week, lesson))
    return completed_lessons, completed_weeks

def _fallback_step(clicks, index):
    if 0 <= index < len(clicks):
        src = clicks[index]
        return int(src.get("x", 0)), int(src.get("y", 0)), float(src.get("delay", 1.0)), src.get("template")
    return 0, 0, 1.0, None

def _has_template_profile(clicks):
    return any(step.get("template") for step in (clicks or []))

def _build_position_model(profile):
    if len(profile or []) < 6:
        return None
    try:
        switch = profile[0]
        week1 = profile[1]
        week2 = profile[2]
        lesson1 = profile[3]
        lesson2 = profile[4]
        replay = profile[5]
        row_gap = float(week2["y"] - week1["y"])
        if abs(row_gap) < 5:
            return None
        return {
            "switch": (int(switch["x"]), int(switch["y"])),
            "week_x": int(week1["x"]),
            "week1_y": int(week1["y"]),
            "week_row_gap": row_gap,
            "lesson1": (int(lesson1["x"]), int(lesson1["y"])),
            "lesson2": (int(lesson2["x"]), int(lesson2["y"])),
            "replay": (int(replay["x"]), int(replay["y"])),
            "delay": max(float(switch.get("delay", 1.0)), 1.0),
        }
    except Exception:
        return None

def _has_position_model(profile):
    return _build_position_model(profile) is not None

def _lesson_words(lesson):
    if lesson == 1:
        return ["1", "第1节", "第一节", "1节", "一节", "上节"]
    return ["2", "第2节", "第二节", "2节", "二节", "下节"]

def _course_card_point(lesson):
    """切换节次弹窗中，第1节通常是右侧 10:09 卡片，第2节通常是左侧 11:04 卡片。"""
    region = _locked_window_region()
    if not region:
        return None
    ocr_point = _course_card_point_by_time(lesson)
    if ocr_point:
        return ocr_point
    x, y, w, h = region
    rel_x = 0.49 if int(lesson) == 1 else 0.18
    rel_y = 0.50
    point = (x + int(w * rel_x), y + int(h * rel_y))
    log(f"  课表卡片固定兜底: 第{lesson}节 -> {point}")
    return point

def _course_card_point_by_time(lesson):
    """优先按卡片上的时间文字定位课表卡片。"""
    region = _course_cards_region()
    if not region:
        return None
    lesson = int(lesson)
    target_digits = "1009" if lesson == 1 else "1104"
    items = _ocr_items(region)
    for item in items:
        digits = re.sub(r"\D+", "", item["text"])
        if target_digits in digits:
            log(f"  课表卡片 OCR 定位: 第{lesson}节 命中 {item['raw']!r} -> ({item['cx']},{item['cy']})")
            return (item["cx"], item["cy"])
    log(f"  课表卡片 OCR 未命中第{lesson}节时间({target_digits})，使用固定兜底")
    return None

def _switch_dialog_close_point():
    """切换节次弹窗右上角关闭按钮。"""
    region = _locked_window_region()
    if not region:
        return None
    x, y, w, h = region
    return (x + int(w * 0.94), y + int(h * 0.31))

def _build_simple_course_groups(weeks, base_clicks=None, skip_completed=True, duration_seconds=1500):
    """为简单模式生成：OCR 查找切换节次 -> 第X周 -> 第1/2节 -> 回放。"""
    base_clicks = base_clicks or []
    completed_lessons, _ = _scan_completed_week_lessons()
    groups = []
    for week in weeks:
        for lesson in SIMPLE_LESSONS:
            if skip_completed and (week, lesson) in completed_lessons:
                continue
            name = f"第{week:02d}周_第{lesson}节"
            specs = [
                (["切换节次", "切换"], 0, True, False, "full", False, "vertical", ""),
                ([f"第{week}周", f"第{week:02d}周", f"{week}周", f"{week:02d}周"], 1, True, True, "week_tabs", False, "horizontal", "week_tab"),
                ([f"第{lesson}节课表卡片"], 2, False, False, "course_cards", False, "vertical", "course_card"),
                (["关闭切换节次弹窗"], 3, False, False, "full", False, "vertical", "close_switch_dialog"),
            ]
            clicks = []
            for words, fallback_idx, require_ocr, search_scroll, search_area, exact_match, scroll_axis, mode in specs:
                x, y, delay, tpl = _fallback_step(base_clicks, fallback_idx)
                step = {
                    "x": x,
                    "y": y,
                    "delay": max(delay, 1.0),
                    "target_text": words,
                    "require_ocr": require_ocr,
                    "search_scroll": search_scroll,
                    "search_area": search_area,
                    "exact_match": exact_match,
                    "scroll_axis": scroll_axis,
                    "mode": mode,
                    "lesson_slot": lesson,
                    "week_slot": week,
                }
                if tpl:
                    step["template"] = tpl
                clicks.append(step)
            groups.append({
                "name": name,
                "clicks": clicks,
                "repeat": 1,
                "next_delay": duration_seconds,
                "duration_seconds": duration_seconds,
                "auto_simple": True,
            })
    return groups

def _norm_ocr_text(text):
    return re.sub(r"\s+", "", str(text or "")).lower()

def _find_text_target(keywords, search_region=None, exact=False):
    """OCR 查找目标文字，返回屏幕坐标 (x, y) 或 None。"""
    keys = [_norm_ocr_text(k) for k in _split_keywords(keywords)]
    keys = [k for k in keys if k]
    if not keys:
        return None
    if pytesseract is None:
        log("  OCR 不可用：未安装 pytesseract，跳过文字定位")
        return None

    try:
        pil_img, offset_x, offset_y = _grab_ocr_image(search_region)

        if OCR_SCALE != 1:
            pil_img = pil_img.resize(
                (int(pil_img.width * OCR_SCALE), int(pil_img.height * OCR_SCALE)),
                Image.Resampling.LANCZOS,
            )

        data = pytesseract.image_to_data(
            pil_img,
            lang=OCR_LANG,
            config="--psm 11",
            output_type=pytesseract.Output.DICT,
        )
        best = None
        for idx, raw_text in enumerate(data.get("text", [])):
            text = _norm_ocr_text(raw_text)
            if not text:
                continue
            try:
                conf = float(data["conf"][idx])
            except Exception:
                conf = -1
            if conf < OCR_MIN_CONFIDENCE:
                continue
            if exact:
                text_digits = re.sub(r"\D+", "", text)
                matched_key = next(
                    (
                        k for k in keys
                        if text == k or (k.isdigit() and text_digits == k)
                    ),
                    None,
                )
            else:
                matched_key = next((k for k in keys if k in text or text in k), None)
            if not matched_key:
                continue
            x = data["left"][idx] / OCR_SCALE + offset_x
            y = data["top"][idx] / OCR_SCALE + offset_y
            w = data["width"][idx] / OCR_SCALE
            h = data["height"][idx] / OCR_SCALE
            item = (conf, int(x + w / 2), int(y + h / 2), raw_text, matched_key)
            if best is None or item[0] > best[0]:
                best = item

        if best:
            conf, cx, cy, raw_text, matched_key = best
            log(f"  OCR 命中: {raw_text!r}≈{matched_key} 置信度={conf:.0f} -> ({cx},{cy})")
            return (cx, cy)
        log(f"  OCR 未找到: {', '.join(_split_keywords(keywords))}")
        return None
    except Exception as e:
        log(f"  OCR 异常: {e}")
        return None

def _ocr_items(search_region=None, psm=None):
    """返回 OCR 词块列表：text/conf/cx/cy/w/h。psm 可覆盖默认 --psm 11。"""
    if pytesseract is None:
        return []
    try:
        pil_img, offset_x, offset_y = _grab_ocr_image(search_region)
        if OCR_SCALE != 1:
            pil_img = pil_img.resize(
                (int(pil_img.width * OCR_SCALE), int(pil_img.height * OCR_SCALE)),
                Image.Resampling.LANCZOS,
            )
        config_str = f"--psm {psm}" if psm else "--psm 11"
        data = pytesseract.image_to_data(
            pil_img,
            lang=OCR_LANG,
            config=config_str,
            output_type=pytesseract.Output.DICT,
        )
        items = []
        for idx, raw_text in enumerate(data.get("text", [])):
            text = _norm_ocr_text(raw_text)
            if not text:
                continue
            try:
                conf = float(data["conf"][idx])
            except Exception:
                conf = -1
            if conf < OCR_MIN_CONFIDENCE:
                continue
            x = data["left"][idx] / OCR_SCALE + offset_x
            y = data["top"][idx] / OCR_SCALE + offset_y
            w = data["width"][idx] / OCR_SCALE
            h = data["height"][idx] / OCR_SCALE
            items.append({
                "text": text,
                "raw": raw_text,
                "conf": conf,
                "cx": int(x + w / 2),
                "cy": int(y + h / 2),
                "w": int(w),
                "h": int(h),
            })
        return items
    except Exception as e:
        log(f"  OCR 词块读取异常: {e}")
        return []

def _switch_title_region():
    region = _locked_window_region()
    if not region:
        return None
    x, y, w, h = region
    return (
        x + int(w * 0.18),
        y + int(h * 0.29),
        int(w * 0.55),
        int(h * 0.08),
    )

def _current_switch_week():
    """读取切换节次弹窗标题里的当前周，例如【第3周】。"""
    region = _switch_title_region()
    if not region:
        return None
    items = _ocr_items(region)
    text = "".join(item["text"] for item in items)
    match = re.search(r"第?0?(\d{1,2})周", text)
    if match:
        week = int(match.group(1))
        log(f"  当前标题周: 第{week}周")
        return week
    log(f"  当前标题周: 未识别 ({text[:30] or '无文字'})")
    return None

def _switch_button_region():
    """回放页右侧“切换节次”按钮所在区域，避免 OCR 误读左侧“5次”。"""
    region = _locked_window_region()
    if not region:
        return None
    x, y, w, h = region
    return (
        x + int(w * 0.72),
        y + int(h * 0.49),
        int(w * 0.25),
        int(h * 0.12),
    )

def _switch_dialog_open():
    if _current_switch_week() is not None:
        return True
    region = _switch_title_region()
    if not region:
        return False
    items = _ocr_items(region)
    text = "".join(item["text"] for item in items)
    return "切换节次" in text or "切换" in text

def _open_switch_dialog():
    """打开切换节次弹窗：只在右侧按钮区域 OCR，避免全屏误点。"""
    if _switch_dialog_open():
        log("  切换节次弹窗已打开")
        return True

    search_region = _switch_button_region()
    if not search_region:
        log("  无法计算切换节次 OCR 区域")
        return False
    log(f"  OCR 在切换节次按钮区域查找: {search_region}")
    point = _find_text_target(["切换节次", "切换"], search_region=search_region)
    if point:
        log(f"  OCR 命中切换节次 -> {point}")
        _strong_click(*point)
        # 弹窗可能需要时间渲染，最多等2秒
        for _ in range(10):
            time.sleep(0.2)
            if _switch_dialog_open():
                log("  弹窗已确认打开")
                return True
        log("  点击后弹窗未确认打开，可能跳转页面需要更长时间")
        time.sleep(1.0)
        return _switch_dialog_open()

    # OCR未找到，用固定坐标兜底点击
    fallback = _switch_button_fallback_point()
    if fallback:
        log(f"  切换节次OCR未找到，用固定位置兜底点击 -> {fallback}")
        _strong_click(*fallback)
        for _ in range(10):
            time.sleep(0.2)
            if _switch_dialog_open():
                log("  弹窗已确认打开(兜底)")
                return True
        log("  兜底点击后弹窗未确认打开")
    else:
        log("  切换节次按钮区域 OCR 未找到，无兜底坐标，停止")
    return False

def _course_cards_region():
    region = _locked_window_region()
    if not region:
        return None
    x, y, w, h = region
    return (
        x + int(w * 0.02),
        y + int(h * 0.42),
        int(w * 0.65),
        int(h * 0.20),
    )

def _week_tabs_region(expanded=False):
    """返回周按钮行的OCR搜索区域。只扫描按钮行(y=37%-43%)，排除标题行。
    expanded=True时使用稍大的区域(y=35%-48%)兜底。
    """
    region = _locked_window_region()
    if not region:
        return None
    x, y, w, h = region
    if expanded:
        # 扩展区域：y=35%-48%，覆盖按钮行及上下留白
        return (
            x + int(w * 0.01),
            y + int(h * 0.35),
            int(w * 0.98),
            int(h * 0.13),
        )
    # 标准区域：精确的周按钮行 y=37%-43%
    return (
        x + int(w * 0.01),
        y + int(h * 0.37),
        int(w * 0.98),
        int(h * 0.06),
    )

def _week_tabs_region_from_title():
    """根据标题区 '第N周' 的 OCR 坐标，推算周按钮行的搜索区域。
    弹窗布局固定：
    - 标题行 y=29%-37%
    - 周按钮行 y=37%-43%
    以标题位置为锚点，但对齐到已知的37%-43%范围，加上小偏移量调整。
    """
    title_region = _switch_title_region()
    if not title_region:
        return None
    # 先 OCR 标题区域找 "第N周" 的位置
    items = _ocr_items(title_region)
    title_text = "".join(item["text"] for item in items)
    title_match = re.search(r"第?0?(\d{1,2})周", title_text)
    if not title_match:
        return _week_tabs_region(expanded=True)
    # 找标题文字的底部 y
    title_items = [it for it in items if "周" in it["text"] or re.search(r"\d+", it["text"])]
    if not title_items:
        return _week_tabs_region(expanded=True)

    win_region = _locked_window_region()
    if not win_region:
        return None
    wx, _, ww, wh = win_region

    # 标题底部y (用于计算偏移量)
    title_bottom_y = max(it["cy"] + it["h"] // 2 for it in title_items)
    # 标题底部正常运行在约35%位置
    expected_title_bottom_abs = win_region[1] + int(wh * 0.35)

    # 计算偏移并限制在±3%范围内
    y_offset = title_bottom_y - expected_title_bottom_abs
    max_offset = int(wh * 0.03)
    y_offset = max(-max_offset, min(max_offset, y_offset))

    # 标准按钮行区域: y=37%-43%，加上偏移量
    tabs_top = win_region[1] + int(wh * 0.37) + y_offset
    tabs_height = int(wh * 0.06)  # 窄区域，只覆盖按钮行
    tabs_left = wx + int(ww * 0.01)
    tabs_width = int(ww * 0.98)

    result = (tabs_left, tabs_top, tabs_width, tabs_height)
    log(f"  周按钮行区域(基于标题定位): title_bottom={title_bottom_y}, offset={y_offset} -> top={tabs_top}, h={tabs_height}")
    return result

def _switch_button_fallback_point():
    """回放页右侧"切换节次"按钮的固定兜底位置。"""
    region = _locked_window_region()
    if not region:
        return None
    x, y, w, h = region
    return (x + int(w * 0.82), y + int(h * 0.55))

def _click_week_scroll_arrow(direction):
    """弹窗没有翻页箭头，通过点击最边上的周按钮触发滚动翻页。
    direction='left' → 点击左侧第一个可见周按钮（看更早的周）
    direction='right' → 点击右侧最后一个可见周按钮（看更晚的周）
    """
    region = _locked_window_region()
    if not region:
        return False
    x, y, w, h = region
    click_y = y + int(h * 0.40)
    # 最左侧周按钮 ≈ 8-12%位置，最右侧 ≈ 82-88%位置
    if direction == "left":
        click_x = x + int(w * 0.10)
    else:
        click_x = x + int(w * 0.85)
    log(f"  翻页点击{'左' if direction=='left' else '右'}侧周按钮 -> ({click_x},{click_y})")
    _strong_click(click_x, click_y)
    time.sleep(0.8)
    # 点最边上后，弹窗可能滚动显示更多周。再OCR一次读标题确认变了没
    new_week = _current_switch_week()
    if new_week is not None:
        log(f"  翻页后标题=第{new_week}周")
    return True

def _click_week_edge(target_week, week_items, fallback_direction="right"):
    """目标周不在可见列表时，点击翻页箭头滚动到目标周所在范围。
    如果目标周 < 最小可见周，点左箭头（更早的周）
    如果目标周 > 最大可见周，点右箭头（更晚的周）
    如果目标周在可见范围之间但不在列表中，可能该周无课 → 返回skip
    """
    if not week_items:
        # 完全没识别到周按钮，按判断方向翻页
        log(f"  周按钮识别不足，翻页方向: {fallback_direction}")
        return _click_week_scroll_arrow(fallback_direction)
    visible = sorted(week_items)
    if target_week < visible[0]:
        # 需要向更早的周翻页
        log(f"  第{target_week}周 < 最小可见周{visible[0]}，点左箭头翻页")
        return _click_week_scroll_arrow("left")
    elif target_week > visible[-1]:
        # 需要向更晚的周翻页
        log(f"  第{target_week}周 > 最大可见周{visible[-1]}，点右箭头翻页")
        return _click_week_scroll_arrow("right")
    else:
        # 目标周在可见范围之间但不在列表中，该周可能无课
        log(f"  第{target_week}周在可见范围[{visible[0]}-{visible[-1]}]之间但不在列表中，跳过")
        return "skip"

def _parse_week_items(items):
    """提取OCR文本块中的数字作为周按钮位置。
    OCR常见"第3周"拆成"第"(26px)"3"(10px)"周"(18px)。
    不合并——每个数字单独处理，用块大小(h≤50,w≤80)过滤垃圾字符。"""
    if not items:
        return {}
    # 调试输出
    raw_debug = [(it["text"], it["cx"], it["cy"], f"{it['conf']:.0f}", it["w"], it["h"]) for it in items if it["conf"] > 30]
    if raw_debug:
        log(f"  _parse_week_items OCR({len(items)}块): {raw_debug[:25]}")

    weeks = {}
    for it in items:
        digits = re.sub(r"\D", "", it["text"])
        if not digits:
            continue
        # 数字块大小过滤：周按钮数字≈10-20px宽×15-40px高
        # 大块(>50px高或>80px宽)是垃圾文字或按钮背景，排除
        if it["h"] > 50 or it["w"] > 80:
            continue
        try:
            week = int(digits)
        except ValueError:
            continue
        if 1 <= week <= 16:
            if week not in weeks or it["conf"] > weeks[week]["conf"]:
                weeks[week] = {"text": digits, "raw": it["text"], "conf": it["conf"],
                                "cx": it["cx"], "cy": it["cy"], "w": it["w"], "h": it["h"]}

    if weeks:
        details = [(k, v['cx'], v['cy'], f"{v['conf']:.0f}", v['w'], v['h']) for k, v in sorted(weeks.items())]
        log(f"  _parse_week_items结果: 周={sorted(weeks.keys())}, {details}")
    return weeks

def _check_blue_at(cx, cy):
    """检测屏幕坐标(cx,cy)附近是否有蓝色像素（表示周按钮被选中）。"""
    try:
        bbox = (cx - 12, cy - 12, cx + 12, cy + 12)
        img = ImageGrab.grab(bbox=bbox)
        arr = np.array(img)
        if arr.shape[2] < 3:
            return False
        hsv = cv2.cvtColor(arr[:, :, :3], cv2.COLOR_RGB2HSV)
        blue_mask = cv2.inRange(hsv, (95, 40, 40), (135, 255, 255))
        blue_count = blue_mask.sum() / 255
        return blue_count > 20
    except Exception:
        return False

def _find_week_tab(week, tries=OCR_SCROLL_TRIES):
    """用OCR找数字定位周按钮 + 颜色检测蓝色选中验证。
    找到目标周 → 点击 → 检查该位置是否变蓝 → 变蓝说明点中了。
    """
    try:
        _save_debug_screenshot("week_tab_enter")
    except Exception:
        pass

    win = _locked_window_region()
    if not win:
        log(f"  未锁定窗口")
        return None
    wx, wy, ww, wh = win

    # ---- 只OCR按钮行(y=35%-48%)，排除标题和课程卡片 ----
    btn_y_min = wy + int(wh * 0.35)
    btn_y_max = wy + int(wh * 0.48)
    ocr_region = (wx + int(ww * 0.02), btn_y_min, int(ww * 0.96), int(wh * 0.13))

    for attempt in range(tries + 1):
        items = _ocr_items(ocr_region, psm=11)
        found = _parse_week_items(items)
        visible = {k: v for k, v in found.items() if btn_y_min <= v["cy"] <= btn_y_max}

        if not visible:
            log(f"  第{attempt+1}次: 按钮行未识别到数字")
            time.sleep(0.3)
            continue

        vis_sorted = sorted(visible.keys())
        log(f"  第{attempt+1}次: 可见周={vis_sorted}")

        if week in visible:
            target = visible[week]
            log(f"  找到第{week}周 -> ({target['cx']},{target['cy']}) 点击")
            _strong_click(target["cx"], target["cy"])
            time.sleep(0.5)
            # 用蓝色检测验证：目标位置变蓝说明点中了
            if _check_blue_at(target["cx"], target["cy"]):
                log(f"  蓝色确认: 第{week}周按钮已选中")
                return (target["cx"], target["cy"])
            # 点偏了？尝试偏移3个不同位置
            for dx, dy in [(5, 0), (-5, 0), (0, 8)]:
                _strong_click(target["cx"] + dx, target["cy"] + dy)
                time.sleep(0.3)
                if _check_blue_at(target["cx"], target["cy"]):
                    log(f"  偏移点击({dx},{dy})命中第{week}周")
                    return (target["cx"] + dx, target["cy"] + dy)
            log(f"  点击第{week}周但未出现蓝色选中，可能位置不准")

        # 目标周不在可见列表，点击最左/右侧按钮翻页
        if attempt < tries:
            if week < vis_sorted[0]:
                log(f"  第{week}周 < 最小{vis_sorted[0]}，点左侧周翻页")
                _click_week_scroll_arrow("left")
            else:
                log(f"  第{week}周 > 最大{vis_sorted[-1]}，点右侧周翻页")
                _click_week_scroll_arrow("right")

    log(f"  翻页{tries+1}次后未找到第{week}周，跳过")
    return None


def _select_week_tab(week):
    """选择目标周；如果标题已是目标周，直接认为成功。"""
    current = _current_switch_week()
    if current == week:
        log(f"  当前已是第{week}周，不重复点击周按钮")
        return True
    point = _find_week_tab(week)
    if point == "skip":
        return "skip"
    if not point:
        # 找不到按钮，再确认一次标题
        current = _current_switch_week()
        if current == week:
            log(f"  虽然找不到按钮，但标题已是第{week}周，继续")
            return True
        log(f"  未找到第{week}周按钮，标题={current}，跳过避免播放错误周内容")
        return "skip"  # 跳过而不是盲目继续
    _strong_click(*point)
    time.sleep(0.8)
    current = _current_switch_week()
    if current == week:
        log(f"  已确认切到第{week}周")
        return True
    log(f"  已点击第{week}周按钮，标题确认结果: {current if current is not None else '未识别'}")
    # 有些页面点击同一周不会改变标题，但下面卡片已经可见；允许继续。
    return True


def _ensure_week_selected(week):
    """确保弹窗处于目标周；缺课或无法切换返回 'skip'。
    如果标题周已经是目标周就直接成功；
    如果找不到目标周按钮且标题不匹配，跳过本节避免播放错误周内容。
    """
    current = _current_switch_week()
    if current == week:
        log(f"  已在第{week}周")
        return True
    point = _find_week_tab(week)
    if point == "skip":
        return "skip"
    if not point:
        # _find_week_tab 返回 None，但标题可能是目标周（OCR误识别）
        # 再确认一次标题
        current = _current_switch_week()
        if current == week:
            log(f"  虽然找不到按钮，但标题已是第{week}周，继续")
            return True
        log(f"  无法定位第{week}周按钮，标题={current}，跳过本节避免播放错误周内容")
        return "skip"
    _strong_click(*point)
    time.sleep(0.8)
    current = _current_switch_week()
    if current == week:
        log(f"  已切到第{week}周")
    else:
        log(f"  点击第{week}周后标题={current if current is not None else '未识别'}，继续尝试课表卡片")
    return True

def _has_course_cards():
    region = _course_cards_region()
    if not region:
        return False
    items = _ocr_items(region)
    text = "".join(item["text"] for item in items)
    has = any(key in text for key in ("课表录制", "录制", "回放")) or bool(re.search(r"\d{1,2}[-:：]\d{2}", text))
    log(f"  课表卡片检测: {'有' if has else '无'} ({text[:40] or '无文字'})")
    return has

def _find_text_target_with_scroll(keywords, search_region=None, tries=OCR_SCROLL_TRIES, exact=False, axis="vertical"):
    """OCR 查找文字，找不到时向下滚动列表继续找。"""
    for attempt in range(max(1, tries)):
        found = _find_text_target(keywords, search_region=search_region, exact=exact)
        if found:
            return found
        if attempt < tries - 1:
            log(f"  OCR 滚动搜索 {attempt + 1}/{tries - 1} ({axis})")
            if axis == "horizontal" and search_region:
                sx, sy, sw, sh = search_region
                pyautogui.moveTo(sx + int(sw * 0.82), sy + int(sh * 0.50), duration=0.05)
                pyautogui.dragRel(-int(sw * 0.45), 0, duration=0.25, button="left")
            else:
                pyautogui.scroll(OCR_SCROLL_AMOUNT)
            time.sleep(0.45)
    return None

def _ocr_ready():
    if pytesseract is None:
        return False, "未安装 pytesseract"
    try:
        _ = pytesseract.get_tesseract_version()
        return True, f"OCR 可用({OCR_LANG})"
    except Exception as e:
        return False, f"Tesseract 不可用: {e}"

def _strong_click(x, y):
    """比普通 click 稍稳的点击：先移动，再短按抬起。"""
    pyautogui.moveTo(x, y, duration=0.05)
    pyautogui.mouseDown(x, y)
    time.sleep(CLICK_HOLD_SECONDS)
    pyautogui.mouseUp(x, y)

def _step_search_region(step):
    area = step.get("search_area")
    if area == "week_tabs":
        region = _locked_window_region()
        if region:
            x, y, w, h = region
            return (
                x + int(w * 0.01),
                y + int(h * 0.37),
                int(w * 0.98),
                int(h * 0.06),
            )
    if area == "lesson_buttons":
        region = _locked_window_region()
        if region:
            x, y, w, h = region
            return (
                x,
                y + int(h * 0.50),
                int(w * 0.45),
                int(h * 0.24),
            )
    if area == "lower_window":
        region = _locked_window_region()
        if region:
            x, y, w, h = region
            top = y + int(h * 0.45)
            return (x, top, w, h - int(h * 0.45))
    return None

# ====================== 点击执行引擎 ======================
def _do_sequence(clicks, label="", group_name=""):
    """执行一组点击序列：OCR文字 -> 模板 -> 固定坐标。"""
    for i, step in enumerate(clicks):
        with _lock:
            if not state["running"] or not state["playing"]:
                log(f"[{label}] 点击序列中断 @ 第{i+1}步")
                return False
        cx, cy = step["x"], step["y"]
        delay = step.get("delay", 1.0)
        tpl = step.get("template")
        mode = step.get("mode", "")
        target_text = _split_keywords(step.get("target_text")) or _default_target_text_for_step(i)
        require_ocr = bool(step.get("require_ocr"))
        search_scroll = bool(step.get("search_scroll"))
        search_region = _step_search_region(step)
        exact_match = bool(step.get("exact_match"))
        scroll_axis = step.get("scroll_axis", "vertical")
        if i == 1:
            target_text = list(dict.fromkeys(target_text + _keywords_from_group_name(group_name)))

        actual_cx, actual_cy = cx, cy
        used_ocr = False
        used_template = False
        if mode == "coord":
            note = step.get("note", "坐标")
            log(f"[{label}] 点击 {i+1}/{len(clicks)}: {note} 坐标模型 -> ({cx},{cy})")
        elif mode == "week_tab":
            week_slot = int(step.get("week_slot", 0))
            log(f"[{label}] 点击 {i+1}/{len(clicks)}: 解析周按钮 -> 第{week_slot}周")
            point = _find_week_tab(week_slot)
            if point == "skip":
                log(f"[{label}] 第{week_slot}周不在课程周列表中，跳过本节")
                return "skip"
            if not point:
                # _find_week_tab 内部已有卡住检测，返回None说明确实无法定位
                current = _current_switch_week()
                if current == week_slot:
                    log(f"[{label}] 虽然找不到周按钮，标题已为第{week_slot}周，跳过点击继续")
                    actual_cx, actual_cy = cx, cy
                    log(f"[{label}] 跳过周按钮点击，尝试下一步")
                    time.sleep(delay)
                    continue
                else:
                    log(f"[{label}] 未找到第{week_slot}周按钮(标题={current})，跳过本节避免播放错误周内容")
                    return "skip"
            actual_cx, actual_cy = point
            used_ocr = True
            log(f"[{label}] 周按钮定位完成: 第{week_slot}周 -> ({actual_cx},{actual_cy})")
        elif mode == "course_card":
            lesson_slot = int(step.get("lesson_slot", 1))
            _has_course_cards()
            point = _course_card_point(lesson_slot)
            if not point:
                log(f"[{label}] 未锁定窗口，无法定位第{lesson_slot}节课表卡片")
                return False
            actual_cx, actual_cy = point
            used_ocr = True
            log(f"[{label}] 点击 {i+1}/{len(clicks)}: 第{lesson_slot}节课表卡片 -> ({actual_cx},{actual_cy})")
        elif mode == "close_switch_dialog":
            point = _switch_dialog_close_point()
            if not point:
                log(f"[{label}] 未锁定窗口，无法关闭切换节次弹窗")
                return False
            actual_cx, actual_cy = point
            used_ocr = True
            log(f"[{label}] 点击 {i+1}/{len(clicks)}: 关闭切换节次弹窗 -> ({actual_cx},{actual_cy})")
        elif target_text:
            log(f"[{label}] 点击 {i+1}/{len(clicks)}: OCR 查找 {'/'.join(target_text)}...")
            if search_region:
                sx, sy, sw, sh = search_region
                log(f"[{label}] OCR 搜索区域: ({sx},{sy},{sw},{sh})")
            time.sleep(CLICK_SETTLE_DELAY)
            if search_scroll:
                matched_text = _find_text_target_with_scroll(target_text, search_region=search_region, exact=exact_match, axis=scroll_axis)
            else:
                matched_text = _find_text_target(target_text, search_region=search_region, exact=exact_match)
            if matched_text:
                actual_cx, actual_cy = matched_text
                used_ocr = True
                log(f"[{label}] OCR 定位完成: 原始({cx},{cy}) -> 实际({actual_cx},{actual_cy})")
            elif require_ocr and not tpl:
                log(f"[{label}] 必须定位的目标未找到，停止本组，避免点错位置")
                return False
        if not used_ocr and tpl:
            log(f"[{label}] 点击 {i+1}/{len(clicks)}: 模板匹配定位中...")
            time.sleep(CLICK_SETTLE_DELAY)
            matched = _find_target(tpl, search_region=None)
            if not matched:
                time.sleep(CLICK_RETRY_GAP)
                matched = _find_target(tpl, search_region=None, threshold=max(0.55, TEMPLATE_MATCH_THRESHOLD - 0.08))
            if matched:
                actual_cx, actual_cy = matched
                used_template = True
                log(f"[{label}] 定位完成: 原始({cx},{cy}) -> 实际({actual_cx},{actual_cy})")
            else:
                if require_ocr:
                    log(f"[{label}] OCR 和模板都未定位到必须目标，停止本组，避免点错位置")
                    return False
                log(f"[{label}] 匹配失败，回退到原始坐标 ({cx},{cy})")
        if not used_ocr and not used_template and not tpl:
            log(f"[{label}] 点击 {i+1}/{len(clicks)}: 未绑定模板，使用固定坐标")
        if not used_ocr and not used_template and target_text:
            log(f"[{label}] 文字/模板未命中，本步使用固定坐标")

        log(f"[{label}] 点击 {i+1}/{len(clicks)}: ({actual_cx},{actual_cy}) 延迟{delay:.1f}s")
        _strong_click(actual_cx, actual_cy)
        if tpl and not used_template and not used_ocr:
            time.sleep(CLICK_RETRY_GAP)
            log(f"[{label}] 模板定位失败，本步已使用固定坐标")
        time.sleep(delay)
    return True

def play_current_group():
    """只执行当前选中组的点击序列（尊重 repeat），执行后启动监测捕获新画面"""
    try:
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
            log("请先按 Alt+Q 选区域!")
            with _lock:
                state["playing"] = False
            return

        if not clicks:
            log(f"[{name}] 没有点击步骤")
            with _lock:
                state["playing"] = False
            return
        if not any(s.get("template") for s in clicks):
            log(f"[{name}] 提示：本组没有模板定位，当前只能按固定坐标点击；点不上时建议重新录制该组")

        # 1. 停止当前监测，打包上一段截图
        _stop_monitoring()

        # 2. 执行点击（监测关闭，点击切换视频）
        log(f"=== 执行 [{name}]: {len(clicks)} 步 × {repeat} 次 ===")
        for r in range(repeat):
            with _lock:
                if not state["playing"]:
                    log(f"[{name}] 第{r+1}/{repeat}次循环前被中断")
                    break
            if r > 0:
                log(f"[{name}] 第{r+1}/{repeat}次循环")
            ok = _do_sequence(clicks, label=f"{name}.{r+1}", group_name=name)
            if not ok:
                break

        with _lock:
            state["playing"] = False

        # 3. 启动新监测会话，捕获新视频画面
        log(f"[{name}] <<< 开始监测...")
        _start_monitoring(region, name)

        log(f"=== [{name}] 执行完毕 ===")
        with _lock:
            state["countdown_seconds"] = state["interval_seconds"]

    except Exception as e:
        log(f"!!! 执行当前组异常: {e}")
        import traceback
        traceback.print_exc()
        with _lock:
            state["playing"] = False

def play_all_groups():
    """按顺序执行所有组，每组独立监测会话，支持全局循环"""
    try:
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
            log("请先按 Alt+Q 选区域!")
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

        log(f"=== 执行全部组 ({len(groups)} 组) | 全局循环={'开' if loop else '关'} ===")
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
                name = grp.get("name", f"组{gi+1}")
                course_duration = _get_group_duration(grp)

                if not clicks:
                    log(f"[{name}] 跳过（无点击步骤）")
                    continue
                if not any(s.get("template") for s in clicks):
                    log(f"[{name}] 提示：本组没有模板兜底，将主要依赖 OCR 文字定位")

                # 执行点击（监测关闭）
                log(f"[{name}] >>> 点击 {len(clicks)} 步 × {repeat} 次")
                group_click_ok = True
                group_skipped = False
                for r in range(repeat):
                    with _lock:
                        if not state["playing"] or not state["playing_all"]:
                            group_click_ok = False
                            break
                    if r > 0:
                        log(f"[{name}] 第{r+1}/{repeat}次循环")
                    ok = _do_sequence(clicks, label=f"{name}.{r+1}", group_name=name)
                    if ok == "skip":
                        group_skipped = True
                        break
                    if not ok:
                        group_click_ok = False
                        break

                with _lock:
                    if not state["playing"] or not state["playing_all"]:
                        break
                if group_skipped:
                    log(f"[{name}] 已跳过：当前周/节没有可用课表卡片")
                    continue
                if not group_click_ok:
                    log(f"[{name}] 点击未完成，未启动本节监测；请检查 OCR/目标文字/当前页面")
                    with _lock:
                        state["playing"] = False
                        state["playing_all"] = False
                    break

                # 启动监测会话，捕获新视频画面
                log(f"[{name}] <<< 开始监测...")
                _start_monitoring(region, name)

                # 等待本节课时长（最后一节也要完整监测）
                if course_duration > 0:
                    log(f"[{name}] 按课程时长监测 {course_duration:.0f}s...")
                    waited = 0
                    report_interval = max(60, int(course_duration / 10))  # 每60秒或总时间的10%报一次进度
                    while waited < course_duration:
                        with _lock:
                            if not state["playing"] or not state["playing_all"]:
                                break
                        sleep_chunk = min(1, course_duration - waited)
                        time.sleep(sleep_chunk)
                        waited += sleep_chunk
                        # 定期报告等待进度
                        if int(waited) % report_interval == 0 and waited > 0:
                            remain = course_duration - waited
                            log(f"[{name}] 监测中... 已运行 {waited:.0f}s / {course_duration:.0f}s，剩余 {remain:.0f}s")
                    with _lock:
                        still_playing = state["playing"] and state["playing_all"]
                    if still_playing:
                        is_last = (gi == len(groups) - 1)
                        next_msg = "全部任务即将结束" if is_last and not loop else "准备切换下一组"
                        log(f"[{name}] 课程时长结束 ({course_duration:.0f}s)，{next_msg}")

                # 停止当前组监测，截图已隔离在该组目录中
                log(f"[{name}] 停止监测")
                _stop_monitoring()

            if not loop:
                break

        # 安全停止：确保退出时监测已关闭
        _stop_monitoring()

        with _lock:
            state["playing"] = False
            state["playing_all"] = False

        log("=== 全部组执行完毕 ===")
        with _lock:
            state["countdown_seconds"] = state["interval_seconds"]

    except Exception as e:
        log(f"!!! 执行全部组异常: {e}")
        import traceback
        traceback.print_exc()
        try:
            _stop_monitoring()
        except Exception:
            pass
        with _lock:
            state["playing"] = False
            state["playing_all"] = False

def _simple_wait_course(name, duration):
    log(f"[{name}] 按课程时长监测 {duration:.0f}s...")
    waited = 0
    report_interval = max(60, int(duration / 10))
    while waited < duration:
        with _lock:
            if not state["playing"] or not state["playing_all"]:
                return False
        sleep_chunk = min(1, duration - waited)
        time.sleep(sleep_chunk)
        waited += sleep_chunk
        if int(waited) % report_interval == 0 and waited > 0:
            log(f"[{name}] 监测中... 已运行 {waited:.0f}s / {duration:.0f}s")
    return True

def _run_simple_course(week, lesson, region, duration):
    name = f"第{week:02d}周_第{lesson}节"
    log(f"[{name}] 1/4 打开切换节次")
    if not _open_switch_dialog():
        log(f"[{name}] 打不开切换节次弹窗，停止")
        return False
    if not _switch_dialog_open():
        log(f"[{name}] 已点击切换节次，但未确认弹窗打开，停止")
        return False

    log(f"[{name}] 2/4 选择第{week}周")
    selected = _ensure_week_selected(week)
    if selected == "skip":
        log(f"[{name}] 第{week}周不在课程列表中，跳过")
        return "skip"
    if not selected:
        log(f"[{name}] 找不到第{week}周，停止")
        return False

    log(f"[{name}] 3/4 点击第{lesson}节课表卡片")
    _run_on_main(_show_toast, f"{name}: 3/4 点课表卡片", '#89b4fa', 1800)
    _has_course_cards()
    point = _course_card_point(lesson)
    if not point:
        log(f"[{name}] 无法计算课表卡片位置")
        return False
    _strong_click(*point)
    time.sleep(1.0)

    log(f"[{name}] 4/4 关闭切换节次弹窗")
    point = _switch_dialog_close_point()
    if not point:
        log(f"[{name}] 无法计算关闭按钮位置")
        return False
    _strong_click(*point)
    time.sleep(1.0)

    # 鼠标移到监控区域下方，避免挡住PPT内容
    if region:
        rx, ry, rw, rh = region
        pyautogui.moveTo(rx + rw // 2, ry + rh + 10, duration=0.1)
        log(f"  鼠标移到监控区域下方: ({rx + rw // 2}, {ry + rh + 10})")

    log(f"[{name}] 开始监控 PPT 区域")
    _start_monitoring(region, name)
    _simple_wait_course(name, duration)
    log(f"[{name}] 停止监控")
    _stop_monitoring()
    return True

def run_simple_auto_courses(weeks, duration, skip_completed=True):
    with _lock:
        if state["playing"]:
            log("正在执行中，请先停止")
            return
        region = state["region"]
        state["playing"] = True
        state["playing_all"] = True
    _stop_monitoring()
    completed_lessons, _ = _scan_completed_week_lessons()
    try:
        for week in weeks:
            for lesson in SIMPLE_LESSONS:
                with _lock:
                    if not state["playing"] or not state["playing_all"]:
                        return
                if skip_completed and (week, lesson) in completed_lessons:
                    log(f"[第{week:02d}周_第{lesson}节] 已有输出，跳过")
                    continue
                result = _run_simple_course(week, lesson, region, duration)
                if result is False:
                    log("[简单模式] 自动流程中止")
                    return
        log("[简单模式] 自动流程完成")
    finally:
        _stop_monitoring()
        with _lock:
            state["playing"] = False
            state["playing_all"] = False

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
        session_label = state.get("session_label") or os.path.basename(out_dir)
        hwnd = state["target_hwnd"]
        use_window = hwnd is not None and is_window_visible(hwnd)

    mode_str = "窗口捕获" if use_window else "屏幕截取"
    log(f"监测中 ({w}x{h}) @ ({x},{y}) [{mode_str}]")
    log(f"输出目录: {out_dir}")

    def save_slide(frame, reason, score=0):
        nonlocal cnt, cd
        if _is_black_frame(frame):
            log(f"{reason}: 黑屏帧已跳过")
            return False
        if dedup.dup(frame):
            return False
        cnt += 1
        ts = datetime.now().strftime("%H%M%S")
        fn = f"slide_{cnt:03d}_{ts}.png"
        cv2_imwrite(os.path.join(out_dir, fn), frame)
        if score:
            log(f"[{cnt:03d}] {reason} ({score:.0f}%) -> {fn}")
        else:
            log(f"[{cnt:03d}] {reason} -> {fn}")
        cd = 3
        return True

    # 首帧测试截图
    try:
        if use_window:
            test_img = capture_window(hwnd)
            if test_img is not None:
                test_crop = test_img[y:y+h, x:x+w]
                if not _is_black_frame(test_crop):
                    cv2_imwrite(os.path.join(out_dir, "_test_capture.png"), test_crop)
                    log(f"测试截图已保存 -> _test_capture.png (均值={np.mean(test_crop):.1f})")
                else:
                    log("测试截图: 黑屏，未保存")
            else:
                log("测试截图: 窗口返回空（可能最小化）")
        else:
            test_sc = ImageGrab.grab(bbox=(x, y, x+w, y+h))
            test_frame = cv2.cvtColor(np.array(test_sc), cv2.COLOR_RGB2BGR)
            if not _is_black_frame(test_frame):
                cv2_imwrite(os.path.join(out_dir, "_test_capture.png"), test_frame)
                log(f"测试截图已保存 -> _test_capture.png")
            else:
                log("测试截图: 黑屏，未保存")
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
                    # 窗口可能最小化了，等下次循环
                    time.sleep(CHECK_INTERVAL)
                    continue
                frame = img[y:y+h, x:x+w]
                if _is_black_frame(frame):
                    # 黑屏，capture_window 已重试过，跳过这帧等下次
                    time.sleep(CHECK_INTERVAL)
                    continue
            else:
                frame = None
                for _ in range(MIN_VALID_CAPTURE_RETRIES):
                    pil_img = ImageGrab.grab(bbox=(x, y, x+w, y+h))
                    candidate = cv2.cvtColor(np.array(pil_img), cv2.COLOR_RGB2BGR)
                    if not _is_black_frame(candidate):
                        frame = candidate
                        break
                    time.sleep(0.05)
                if frame is None:
                    time.sleep(CHECK_INTERVAL)
                    continue
        except Exception as e:
            log(f"截图失败: {e}")
            time.sleep(1)
            continue

        if cnt == 0:
            save_slide(frame, "首张有效页")
            det.reset()

        changed, score, status = det.detect(frame)

        elapsed = time.time() - start
        if int(elapsed) % 30 == 0 and int(elapsed) > 0 and int((elapsed - 0.5)) % 30 != 0:
            log(f"[心跳] 运行中 {cnt}张 | 状态={status} | 差异={score:.1f}%")

        if cd > 0:
            cd -= 1

        if changed and cd == 0:
            save_slide(frame, "翻页", score)

        time.sleep(CHECK_INTERVAL)

    elapsed = time.time() - start
    log(f"监测结束 | {elapsed:.0f}s | {cnt}张 | 目录: {os.path.basename(out_dir)}/")
    if cnt > 0:
        _export_session_pdf(out_dir, session_label)
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
        name = "未知组"
        n = 0
        tpl_fname = None
        with _lock:
            if gidx < len(state["groups"]):
                clicks_list = state["groups"][gidx]["clicks"]
                n = len(clicks_list) + 1
                click_entry = {"x": x, "y": y, "delay": delay}
                default_target = _default_target_text_for_step(n - 1)
                if default_target:
                    click_entry["target_text"] = default_target
                tpl_fname = _capture_template_at(x, y)
                if tpl_fname:
                    click_entry["template"] = tpl_fname
                    log(f"[模板] 已保存: {tpl_fname}")
                clicks_list.append(click_entry)
                save_groups(state["groups"])
                name = state["groups"][gidx]["name"]
                if name.startswith("简单模式校准"):
                    _config["simple_click_profile"] = clicks_list[:4]
                    save_config(_config)
            else:
                log("录制失败：当前组不存在")
                return
        target_desc = f" 目标={','.join(default_target)}" if default_target else ""
        log(f"[{name}] 已记录 #{n}: ({x},{y}) 延迟{delay:.1f}s{target_desc}{' [带模板]' if tpl_fname else ''}")
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
            _run_on_main(_show_toast, f"选区完成: {rw}x{rh} | 按Alt+W开始监测", '#a6e3a1', 2500)

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

    # Alt+Q 框选拖拽预览
    if picking and step == 2 and p1:
        _show_selection_rect(p1[0], p1[1], x, y)

    # Alt+I 窗口选取悬停高亮
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
_alt_pressed = False

def start_keyboard():
    global _alt_pressed

    def on_press(key):
        global _alt_pressed
        # 追踪 Alt 键状态
        if key in (keyboard.Key.alt_l, keyboard.Key.alt_r, keyboard.Key.alt_gr):
            _alt_pressed = True
            return

        # 只在 Alt 按住时响应字母键
        if not _alt_pressed:
            # ESC 不需要 Alt
            if key == keyboard.Key.esc:
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
            return

        # 获取字母键
        try:
            k = key.char.lower() if hasattr(key, 'char') and key.char else None
        except:
            k = None
        if k not in ('q','w','e','r','t','y','u','i','o'):
            return

        if k == 'q':  # Alt+Q: 框选区域
            with _lock:
                if state["monitoring"]:
                    log("监测中，请先 Alt+W 停止")
                    _run_on_main(_show_toast, "请先 Alt+W 停止监测", '#f38ba8', 1500)
                    return
                state["picking"] = True
                state["pick_step"] = 1
                state["pick_p1"] = None
            log("选区域: 按住左键拖拽框选")
            _run_on_main(_show_toast, "按住左键拖拽框选区域", '#89b4fa')
        elif k == 'w':  # Alt+W: 开始/停止监测
            with _lock:
                picking = state["picking"]
                monitoring = state["monitoring"]
                region = state["region"]
            if picking:
                log("选区域中，先完成或按ESC取消")
                _run_on_main(_show_toast, "请先完成选区域或按ESC取消", '#f38ba8', 1500)
                return
            if not monitoring:
                if not region:
                    log("请先按 Alt+Q 选区域!")
                    _run_on_main(_show_toast, "请先按 Alt+Q 选区域!", '#f38ba8', 1500)
                    return
                r = region
                if r[2] < 10 or r[3] < 10:
                    log("区域太小，重选!")
                    _run_on_main(_show_toast, "区域太小，请重选!", '#f38ba8', 1500)
                    return
                _start_monitoring(r, "手动")
                _run_on_main(_show_toast, "监测已开始 - 翻页自动截图", '#a6e3a1', 2000)
            else:
                _stop_monitoring()
                _run_on_main(_show_toast, "监测已停止", '#fab387')
        elif k == 'e':  # Alt+E: 手动截图
            manual_capture()
            _run_on_main(_show_toast, "已截图", '#a6e3a1', 1000)
        elif k == 'r':  # Alt+R: 录制点击
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
                log(f"录制模式开启 -> [{name}] 点击画面各位置（默认延迟 {state['default_delay']:.1f}s），按 Alt+R 结束")
                _run_on_main(_show_toast, f"录制开始 -> [{name}]", '#89b4fa')
            else:
                with _lock:
                    state["recording"] = False
                    save_groups(state["groups"])
                    n = len(state["groups"][gidx]["clicks"])
                    if name.startswith("简单模式校准"):
                        _config["simple_click_profile"] = state["groups"][gidx]["clicks"][:4]
                        save_config(_config)
                log(f"[{name}] 录制结束，共 {n} 步")
                if name.startswith("简单模式校准"):
                    log(f"[简单模式] 校准完成：已保存 {min(n, 4)} 个模板/坐标")
                    _run_on_main(_show_toast, f"校准完成: {min(n, 4)} 步", '#a6e3a1')
                else:
                    _run_on_main(_show_toast, f"录制结束: {n} 步", '#a6e3a1')
        elif k == 't':  # Alt+T: 执行当前组
            threading.Thread(target=play_current_group, daemon=True).start()
        elif k == 'y':  # Alt+Y: 停止
            stop_play()
        elif k == 'u':  # Alt+U: 执行全部组
            log("Alt+U 旧全部组已停用，请使用绿色“一键自动转图”")
            _run_on_main(_show_toast, "请使用绿色“一键自动转图”", '#fab387', 1800)
        elif k == 'i':  # Alt+I: 锁定/解锁窗口
            with _lock:
                hwnd = state["target_hwnd"]
            if hwnd:
                with _lock:
                    state["target_hwnd"] = None
                    state["window_title"] = ""
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
        elif k == 'o':  # Alt+O: 退出
            log("退出中...")
            with _lock:
                state["running"] = False
            os._exit(0)

    def on_release(key):
        global _alt_pressed
        if key in (keyboard.Key.alt_l, keyboard.Key.alt_r, keyboard.Key.alt_gr):
            _alt_pressed = False

    with keyboard.Listener(on_press=on_press, on_release=on_release) as listener:
        listener.join()

# ====================== GUI 窗口 ======================
def create_gui():
    root = tk.Tk()
    root.title("PPT Extractor v7")
    root.geometry("1240x860")
    root.resizable(False, False)
    root.attributes('-topmost', True)
    _set_gui_root(root)

    # ---- 配色 ----
    bg = "#1a1b26"
    fg = "#a9b1d6"
    accent = "#7aa2f7"
    accent2 = "#9ece6a"
    warn = "#f7768e"
    orange = "#e0af68"
    btn_bg = "#24283b"
    btn_hover = "#3b4261"
    entry_bg = "#16161e"
    card_bg = "#1f2335"
    border = "#292e42"
    dim = "#565f89"
    root.configure(bg=bg)

    def make_btn(parent, text, cmd, c=btn_bg, fc=fg, w=None, fs=11):
        b = tk.Button(parent, text=text, command=cmd, bg=c, fg=fc,
                      activebackground=btn_hover, activeforeground=fc,
                      relief="flat", font=("Microsoft YaHei UI", fs),
                      cursor="hand2", bd=0, padx=10, pady=5)
        if w: b.configure(width=w)
        return b

    def card(parent, title):
        f = tk.Frame(parent, bg=card_bg, highlightbackground=border,
                     highlightthickness=1, padx=12, pady=8)
        tk.Label(f, text=title, fg=accent, bg=card_bg,
                 font=("Microsoft YaHei UI", 12, "bold"), anchor="w").pack(fill=tk.X, pady=(0, 6))
        return f

    # ---- 回调函数（GUI布局中引用，必须先定义）----
    def gui_pick_region():
        with _lock:
            if state["monitoring"]:
                log("监测中，请先停止监测")
                _run_on_main(_show_toast, "请先停止监测", '#f38ba8', 1500)
                return
            state["picking"] = True
            state["pick_step"] = 1
            state["pick_p1"] = None
        log("选区域: 按住左键拖拽框选")
        _run_on_main(_show_toast, "按住左键拖拽框选 PPT 区域", '#89b4fa')

    def gui_toggle_monitor():
        with _lock:
            picking = state["picking"]
            monitoring = state["monitoring"]
            region = state["region"]
        if picking:
            log("选区域中，先完成或按ESC取消")
            _run_on_main(_show_toast, "请先完成选区域或按ESC取消", '#f38ba8', 1500)
            return
        if not monitoring:
            if not region:
                log("请先选区域")
                _run_on_main(_show_toast, "请先选区域", '#f38ba8', 1500)
                return
            if region[2] < 10 or region[3] < 10:
                log("区域太小，重选")
                _run_on_main(_show_toast, "区域太小，请重选", '#f38ba8', 1500)
                return
            _start_monitoring(region, "手动")
            _run_on_main(_show_toast, "监测已开始 - 翻页自动截图", '#a6e3a1', 2000)
        else:
            _stop_monitoring()
            _run_on_main(_show_toast, "监测已停止", '#fab387')

    def add_group():
        with _lock:
            name = f"组{len(state['groups']) + 1}"
            state["groups"].append({"name": name, "clicks": [], "repeat": 1, "next_delay": 600, "duration_seconds": 600})
            state["current_group_index"] = len(state["groups"]) - 1
            save_groups(state["groups"])
        refresh_group_listbox(); refresh_click_listbox(); refresh_group_settings()
        log(f"新建组: {name}")

    def delete_group():
        with _lock:
            gidx = state["current_group_index"]
            if gidx < 0 or gidx >= len(state["groups"]) or len(state["groups"]) <= 1:
                if len(state["groups"]) <= 1: log("至少保留一个组"); return
                return
            name = state["groups"][gidx]["name"]
            del state["groups"][gidx]
            state["current_group_index"] = min(gidx, len(state["groups"])-1)
            save_groups(state["groups"])
        refresh_group_listbox(); refresh_click_listbox(); refresh_group_settings()
        log(f"已删除组: {name}")

    def rename_group():
        with _lock:
            gidx = state["current_group_index"]
            old_name = state["groups"][gidx]["name"] if gidx < len(state["groups"]) else ""
        dlg = tk.Toplevel(root); dlg.title("重命名组"); dlg.geometry("300x120")
        dlg.resizable(False, False); dlg.configure(bg=card_bg)
        dlg.attributes('-topmost', True); dlg.transient(root); dlg.grab_set()
        tk.Label(dlg, text="组名称:", fg=fg, bg=card_bg, font=("Microsoft YaHei UI", 10)).pack(pady=(14,6))
        var = tk.StringVar(value=old_name)
        e = tk.Entry(dlg, textvariable=var, width=25, bg=entry_bg, fg=fg, insertbackground=fg, relief="flat", font=("Microsoft YaHei UI", 10))
        e.pack(); e.select_range(0,tk.END); e.focus_set()
        def confirm():
            n = var.get().strip()
            if n:
                with _lock:
                    if state["current_group_index"] < len(state["groups"]):
                        state["groups"][state["current_group_index"]]["name"] = n; save_groups(state["groups"])
                refresh_group_listbox(); log(f"重命名: {old_name} -> {n}")
            dlg.destroy()
        make_btn(dlg, "确定", confirm).pack(pady=8); dlg.bind('<Return>', lambda _: confirm())

    def move_group_up():
        with _lock:
            gi = state["current_group_index"]
            if gi <= 0 or gi >= len(state["groups"]): return
            state["groups"][gi], state["groups"][gi-1] = state["groups"][gi-1], state["groups"][gi]; state["current_group_index"] = gi-1; save_groups(state["groups"])
        refresh_group_listbox()

    def move_group_down():
        with _lock:
            gi = state["current_group_index"]
            if gi < 0 or gi >= len(state["groups"])-1: return
            state["groups"][gi], state["groups"][gi+1] = state["groups"][gi+1], state["groups"][gi]; state["current_group_index"] = gi+1; save_groups(state["groups"])
        refresh_group_listbox()

    def refresh_group_listbox():
        group_listbox.delete(0, tk.END)
        with _lock:
            groups = list(state["groups"]); gi = state["current_group_index"]
        for i, g in enumerate(groups):
            m = "▸ " if i == gi else "  "
            nc = len(g.get("clicks", [])); rp = g.get("repeat", 1); dur = _get_group_duration(g)
            group_listbox.insert(tk.END, f"{m}[{i+1}] {g['name']}  |  {nc}步  |  循环{rp}次  |  时长{dur:.0f}s")

    def on_group_select(_evt=None):
        sel = group_listbox.curselection()
        if sel:
            with _lock: state["current_group_index"] = sel[0]
            refresh_group_listbox(); refresh_click_listbox(); refresh_group_settings()

    def refresh_click_listbox():
        click_listbox.delete(0, tk.END)
        with _lock:
            gi = state["current_group_index"]
            if gi < 0 or gi >= len(state["groups"]): return
            clicks = list(state["groups"][gi].get("clicks", []))
        for i, s in enumerate(clicks):
            d = s.get("delay", 1.0)
            words = _split_keywords(s.get("target_text")) or _default_target_text_for_step(i)
            word_label = "/".join(words[:3]) if words else "-"
            tpl_label = "模板" if s.get("template") else "无模板"
            if words and s.get("require_ocr") and int(s.get("x", 0)) == 0 and int(s.get("y", 0)) == 0:
                pos_label = "OCR定位"
            else:
                pos_label = f"坐标({s['x']:>4},{s['y']:>4})"
            click_listbox.insert(
                tk.END,
                f"  #{i+1:>2}  文字:{word_label:<12} | {tpl_label:<4} | {pos_label:<12} | 延迟{d:.1f}s"
            )

    def delete_click():
        sel = click_listbox.curselection()
        if not sel: return
        idx = sel[0]
        with _lock:
            gi = state["current_group_index"]
            if 0 <= gi < len(state["groups"]) and 0 <= idx < len(state["groups"][gi]["clicks"]):
                del state["groups"][gi]["clicks"][idx]; save_groups(state["groups"])
        refresh_click_listbox(); refresh_group_listbox(); log(f"已删除第{idx+1}步")

    def move_click_up():
        sel = click_listbox.curselection()
        if not sel or sel[0] == 0: return
        idx = sel[0]
        with _lock:
            gi = state["current_group_index"]
            if gi < len(state["groups"]):
                c = state["groups"][gi]["clicks"]; c[idx], c[idx-1] = c[idx-1], c[idx]; save_groups(state["groups"])
        refresh_click_listbox(); click_listbox.selection_set(idx-1)

    def move_click_down():
        sel = click_listbox.curselection()
        if not sel: return
        idx = sel[0]
        with _lock:
            gi = state["current_group_index"]
            if gi < len(state["groups"]) and idx < len(state["groups"][gi]["clicks"])-1:
                c = state["groups"][gi]["clicks"]; c[idx], c[idx+1] = c[idx+1], c[idx]; save_groups(state["groups"])
        refresh_click_listbox(); click_listbox.selection_set(idx+1)

    def edit_click_delay():
        sel = click_listbox.curselection()
        if not sel: return
        idx = sel[0]
        with _lock:
            gi = state["current_group_index"]
            cur = state["groups"][gi]["clicks"][idx].get("delay", 1.0) if gi < len(state["groups"]) and idx < len(state["groups"][gi].get("clicks",[])) else 1.0
        dlg = tk.Toplevel(root); dlg.title("编辑延迟"); dlg.geometry("240x120")
        dlg.resizable(False, False); dlg.configure(bg=card_bg)
        dlg.attributes('-topmost', True); dlg.transient(root); dlg.grab_set()
        tk.Label(dlg, text=f"第{idx+1}步延迟(秒):", fg=fg, bg=card_bg, font=("Microsoft YaHei UI", 10)).pack(pady=(14,6))
        var = tk.StringVar(value=str(cur))
        e = tk.Entry(dlg, textvariable=var, width=8, bg=entry_bg, fg=fg, insertbackground=fg, relief="flat", font=("Microsoft YaHei UI", 10))
        e.pack(); e.select_range(0,tk.END); e.focus_set()
        def confirm():
            try:
                d = max(0.1, float(var.get()))
                with _lock:
                    g2 = state["current_group_index"]
                    if g2 < len(state["groups"]) and idx < len(state["groups"][g2]["clicks"]):
                        state["groups"][g2]["clicks"][idx]["delay"] = d; save_groups(state["groups"])
                refresh_click_listbox()
            except ValueError: pass
            dlg.destroy()
        make_btn(dlg, "确定", confirm).pack(pady=8); dlg.bind('<Return>', lambda _: confirm())

    def edit_click_target():
        sel = click_listbox.curselection()
        if not sel: return
        idx = sel[0]
        with _lock:
            gi = state["current_group_index"]
            cur = ""
            if gi < len(state["groups"]) and idx < len(state["groups"][gi].get("clicks", [])):
                step = state["groups"][gi]["clicks"][idx]
                words = _split_keywords(step.get("target_text")) or _default_target_text_for_step(idx)
                cur = ",".join(words)
        dlg = tk.Toplevel(root); dlg.title("编辑目标文字"); dlg.geometry("420x150")
        dlg.resizable(False, False); dlg.configure(bg=card_bg)
        dlg.attributes('-topmost', True); dlg.transient(root); dlg.grab_set()
        tk.Label(dlg, text=f"第{idx+1}步目标文字（逗号分隔，留空则只用模板/坐标）:", fg=fg, bg=card_bg,
                 font=("Microsoft YaHei UI", 10)).pack(pady=(14,6))
        var = tk.StringVar(value=cur)
        e = tk.Entry(dlg, textvariable=var, width=44, bg=entry_bg, fg=fg,
                     insertbackground=fg, relief="flat", font=("Microsoft YaHei UI", 10))
        e.pack(); e.select_range(0, tk.END); e.focus_set()
        def confirm():
            words = _split_keywords(var.get())
            with _lock:
                g2 = state["current_group_index"]
                if g2 < len(state["groups"]) and idx < len(state["groups"][g2]["clicks"]):
                    if words:
                        state["groups"][g2]["clicks"][idx]["target_text"] = words
                    else:
                        state["groups"][g2]["clicks"][idx].pop("target_text", None)
                    save_groups(state["groups"])
            refresh_click_listbox()
            dlg.destroy()
        make_btn(dlg, "确定", confirm).pack(pady=10); dlg.bind('<Return>', lambda _: confirm())

    def clear_clicks():
        with _lock:
            gi = state["current_group_index"]
            if gi < len(state["groups"]): state["groups"][gi]["clicks"] = []; save_groups(state["groups"])
        refresh_click_listbox(); refresh_group_listbox(); log("已清空点击序列")

    def apply_group_settings():
        try:
            r = max(1, int(repeat_var.get())); nd = max(0, float(next_delay_var.get()))
        except ValueError: return
        nm = ""
        with _lock:
            gi = state["current_group_index"]
            if gi < len(state["groups"]):
                state["groups"][gi]["repeat"] = r
                state["groups"][gi]["next_delay"] = nd
                state["groups"][gi]["duration_seconds"] = nd
                save_groups(state["groups"])
                nm = state["groups"][gi]["name"]
        if nm:
            refresh_group_listbox(); log(f"[{nm}] 循环{r}次/课程时长{nd:.0f}s")

    def refresh_group_settings():
        with _lock:
            gi = state["current_group_index"]
            if gi < 0 or gi >= len(state["groups"]): return
            g = state["groups"][gi]
        repeat_var.set(str(g.get("repeat", 1))); next_delay_var.set(str(_get_group_duration(g))); group_name_var.set(g.get("name", ""))

    def toggle_global_loop():
        v = loop_var.get()
        with _lock: state["global_group_loop"] = v
        _config["global_group_loop"] = v; save_config(_config); log(f"全局循环:{'开' if v else '关'}")

    def apply_default_delay():
        try:
            d = max(0.1, float(delay_var.get()))
        except ValueError: return
        with _lock: state["default_delay"] = d
        _config["default_delay"] = d; save_config(_config); log(f"默认延迟:{d:.1f}s")

    def apply_interval():
        try:
            m = max(1, int(interval_var.get())); s = m * 60
        except ValueError: return
        with _lock: state["interval_seconds"] = s; state["countdown_seconds"] = s
        _config["interval_minutes"] = m; save_config(_config); log(f"定时间隔:{m}分钟")

    def gui_pick_window():
        with _lock: state["window_picking"] = True
        log("选取窗口: 点击目标窗口")

    def gui_unlock_window():
        with _lock:
            h = state["target_hwnd"]
            if h:
                state["target_hwnd"] = None; state["window_title"] = ""
                if state["region"] and is_window_visible(h):
                    rect = wintypes.RECT(); _user32.GetWindowRect(h, ctypes.byref(rect))
                    rx, ry, rw, rh = state["region"]; state["region"] = (rx+rect.left, ry+rect.top, rw, rh)
        log("已解锁窗口")

    def toggle_timer():
        with _lock: a = state["timer_active"]
        if a:
            with _lock: state["timer_active"] = False; log("倒计时暂停")
        else:
            with _lock: state["timer_active"] = True; log("倒计时启动")

    def exec_reset_timer():
        with _lock: state["countdown_seconds"] = state["interval_seconds"]
        log("倒计时重置")

    def simple_status_text():
        ocr_ok, ocr_msg = _ocr_ready()
        profile = _config.get("simple_click_profile", [])
        profile_msg = "有模板兜底" if _has_template_profile(profile) else "无模板兜底"
        return f"{ocr_msg} | {profile_msg} | 输入周范围后可直接点“一键开始”"

    def simple_scan_done():
        weeks = _parse_week_spec(simple_weeks_var.get())
        _, done_weeks = _scan_completed_week_lessons()
        ocr_ok, ocr_msg = _ocr_ready()
        profile = _config.get("simple_click_profile", [])
        if weeks:
            done = [w for w in weeks if w in done_weeks]
            todo = [w for w in weeks if w not in done_weeks]
        else:
            done = sorted(done_weeks)
            todo = []
        profile_msg = "有模板兜底" if _has_template_profile(profile) else "无模板兜底"
        simple_status_var.set(f"已有周: {done if done else '无'} | {ocr_msg} | {profile_msg}")
        log(f"[简单模式] 已扫描输出目录，已有周: {done if done else '无'}")

    def simple_calibrate_clicks():
        with _lock:
            state["groups"] = [{
                "name": "简单模式校准_切换-周-节-回放",
                "clicks": [],
                "repeat": 1,
                "next_delay": 600,
                "duration_seconds": 600,
            }]
            state["current_group_index"] = 0
            state["recording"] = True
            save_groups(state["groups"])
        refresh_group_listbox()
        refresh_click_listbox()
        refresh_group_settings()
        msg = "可选校准：依次点击4处作为模板兜底：切换节次、任意周、任意节、回放。点完按 Alt+R 结束。"
        simple_status_var.set(msg)
        log(f"[简单模式] {msg}")
        _run_on_main(_show_toast, "依次点4处，点完按 Alt+R 结束", '#89b4fa', 3000)

    def simple_generate_tasks(start_after=False):
        weeks = _parse_week_spec(simple_weeks_var.get())
        if not weeks:
            log("[简单模式] 请先输入周范围，例如 1-16")
            _run_on_main(_show_toast, "请输入周范围，例如 1-16", '#f38ba8', 1800)
            return
        try:
            duration = max(60, float(simple_duration_var.get()))
        except ValueError:
            duration = 1500
        _config["simple_weeks"] = simple_weeks_var.get().strip() or DEFAULT_SIMPLE_WEEKS
        _config["simple_duration_seconds"] = duration
        _config["simple_skip_done"] = simple_skip_done_var.get()
        _config["global_group_loop"] = False
        save_config(_config)
        with _lock:
            configured_profile = list(_config.get("simple_click_profile", []))
            if configured_profile:
                base_clicks = configured_profile
            else:
                base_clicks = list(state["groups"][0].get("clicks", [])) if state["groups"] else []
        ocr_ok, ocr_msg = _ocr_ready()
        if not ocr_ok:
            msg = f"不能启动：{ocr_msg}。请确认 Tesseract 和中文包可用。"
            simple_status_var.set(msg)
            log(f"[简单模式] {msg}")
            _run_on_main(_show_toast, "OCR 不可用，不能启动", '#f38ba8', 2500)
            return
        groups = _build_simple_course_groups(weeks, base_clicks=base_clicks, skip_completed=simple_skip_done_var.get(), duration_seconds=duration)
        if not groups:
            simple_status_var.set("没有新任务：这些周次可能已经有输出")
            log("[简单模式] 没有生成新任务：可能都已经完成")
            return
        with _lock:
            state["groups"] = groups
            state["current_group_index"] = 0
            state["global_group_loop"] = False
            save_groups(state["groups"])
        try:
            loop_var.set(False)
        except Exception:
            pass
        refresh_group_listbox()
        refresh_click_listbox()
        refresh_group_settings()
        simple_status_var.set(f"已生成 {len(groups)} 个任务：{weeks[0]}-{weeks[-1]}周，每周1/2节")
        log(f"[简单模式] 已生成 {len(groups)} 个任务（每周第1/2节）")
        if start_after:
            log("[简单模式] 生成任务后的旧全部组启动已停用，请使用绿色“一键自动转图”")

    def simple_start_auto():
        with _lock:
            region = state.get("region")
            hwnd = state.get("target_hwnd")
        if not hwnd or not is_window_visible(hwnd):
            msg = "请先点右侧“锁定窗口”，再点击智慧课堂完整窗口"
            simple_status_var.set(msg)
            log(f"[简单模式] {msg}")
            _run_on_main(_show_toast, msg, '#f38ba8', 2500)
            return
        if not region:
            msg = "请先按 Alt+Q 框选 PPT 播放区域"
            simple_status_var.set(msg)
            log(f"[简单模式] {msg}")
            _run_on_main(_show_toast, msg, '#f38ba8', 2500)
            return
        ocr_ok, ocr_msg = _ocr_ready()
        if not ocr_ok:
            msg = f"不能启动：{ocr_msg}"
            simple_status_var.set(msg)
            log(f"[简单模式] {msg}")
            _run_on_main(_show_toast, msg, '#f38ba8', 2500)
            return
        weeks = _parse_week_spec(simple_weeks_var.get())
        if not weeks:
            msg = "请输入周范围，例如 1-16"
            simple_status_var.set(msg)
            log(f"[简单模式] {msg}")
            _run_on_main(_show_toast, msg, '#f38ba8', 1800)
            return
        try:
            duration = max(60, float(simple_duration_var.get()))
        except ValueError:
            duration = 1500
        _config["simple_weeks"] = simple_weeks_var.get().strip() or DEFAULT_SIMPLE_WEEKS
        _config["simple_duration_seconds"] = duration
        _config["simple_skip_done"] = simple_skip_done_var.get()
        _config["global_group_loop"] = False
        save_config(_config)
        simple_status_var.set("自动流程启动：先切课，进入回放后开始监控")
        log(f"[简单模式] 专用自动流程启动：{weeks[0]}-{weeks[-1]}周，每周1/2节，时长{duration:.0f}s")
        threading.Thread(
            target=run_simple_auto_courses,
            args=(weeks, duration, simple_skip_done_var.get()),
            daemon=True,
        ).start()

    # ==================== 顶栏 ====================
    top = tk.Frame(root, bg="#13141f", height=42)
    top.pack(fill=tk.X)
    top.pack_propagate(False)

    status_var = tk.StringVar(value="就绪")
    tk.Label(top, text="●", fg=accent2, bg="#13141f",
             font=("Segoe UI", 13)).pack(side=tk.LEFT, padx=(12, 4))
    tk.Label(top, textvariable=status_var, fg=fg, bg="#13141f",
             font=("Microsoft YaHei UI", 12)).pack(side=tk.LEFT)

    group_name_var = tk.StringVar(value="")
    tk.Label(top, text="|", fg=dim, bg="#13141f",
             font=("Consolas", 11)).pack(side=tk.LEFT, padx=(14, 6))
    tk.Label(top, textvariable=group_name_var, fg=orange, bg="#13141f",
             font=("Microsoft YaHei UI", 12, "bold")).pack(side=tk.LEFT)

    # 右侧快捷操作
    quick_row = tk.Frame(top, bg="#13141f")
    quick_row.pack(side=tk.RIGHT, padx=(0, 12))

    make_btn(quick_row, "选区", gui_pick_region, c='#3d59a1', fc='#c0caf5', fs=10).pack(side=tk.LEFT, padx=3)
    make_btn(quick_row, "监测", gui_toggle_monitor, c='#2a5068', fc='#a9d4e0', fs=10).pack(side=tk.LEFT, padx=3)
    make_btn(quick_row, "截图", manual_capture, c='#4a3a2a', fc='#e0c898', fs=10).pack(side=tk.LEFT, padx=3)
    make_btn(quick_row, "停止", stop_play, c='#5a2838', fc='#e8a0b0', fs=10).pack(side=tk.LEFT, padx=3)

    # ==================== 主区域：左右分栏 ====================
    main = tk.Frame(root, bg=bg)
    main.pack(fill=tk.BOTH, expand=True, padx=12, pady=10)

    left = tk.Frame(main, bg=bg)
    left.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

    right = tk.Frame(main, bg=bg, width=430)
    right.pack(side=tk.RIGHT, fill=tk.Y, padx=(12, 0))
    right.pack_propagate(False)

    # ===== 左栏：组 + 点击序列 =====

    # --- 简单模式：只输入周范围 ---
    simple_sec = card(left, "简单模式：课堂回放转图片")
    simple_sec.pack(fill=tk.X, pady=(0, 10))

    simple_row1 = tk.Frame(simple_sec, bg=card_bg)
    simple_row1.pack(fill=tk.X, pady=(0, 6))
    tk.Label(simple_row1, text="周范围:", fg=dim, bg=card_bg,
             font=("Microsoft YaHei UI", 11)).pack(side=tk.LEFT)
    simple_weeks_var = tk.StringVar(value=_config.get("simple_weeks", DEFAULT_SIMPLE_WEEKS))
    tk.Entry(simple_row1, textvariable=simple_weeks_var, width=18, bg=entry_bg, fg=fg,
             insertbackground=fg, relief="flat", font=("Consolas", 12)).pack(side=tk.LEFT, padx=8)
    tk.Label(simple_row1, text="每节时长(s):", fg=dim, bg=card_bg,
             font=("Microsoft YaHei UI", 11)).pack(side=tk.LEFT, padx=(12, 0))
    simple_duration_var = tk.StringVar(value=str(_config.get("simple_duration_seconds", 1500)))
    tk.Entry(simple_row1, textvariable=simple_duration_var, width=7, bg=entry_bg, fg=fg,
             insertbackground=fg, relief="flat", font=("Consolas", 12)).pack(side=tk.LEFT, padx=8)
    simple_skip_done_var = tk.BooleanVar(value=_config.get("simple_skip_done", True))
    tk.Checkbutton(simple_row1, text="跳过已有", variable=simple_skip_done_var,
                   fg=fg, bg=card_bg, selectcolor=entry_bg,
                   activebackground=card_bg, activeforeground=fg,
                   font=("Microsoft YaHei UI", 10)).pack(side=tk.LEFT, padx=(8, 0))

    simple_row2 = tk.Frame(simple_sec, bg=card_bg)
    simple_row2.pack(fill=tk.X, pady=(0, 6))
    make_btn(simple_row2, "扫描已有周", simple_scan_done, fs=10).pack(side=tk.LEFT, padx=(0, 6))
    make_btn(simple_row2, "模板兜底(可选)", simple_calibrate_clicks,
             c='#4a3a2a', fc='#e0c898', fs=10).pack(side=tk.LEFT, padx=6)
    make_btn(simple_row2, "生成任务(预览)", lambda: simple_generate_tasks(False),
             c='#2a4a6a', fc='#a9d4e0', fs=10).pack(side=tk.LEFT, padx=6)

    simple_row3 = tk.Frame(simple_sec, bg=card_bg)
    simple_row3.pack(fill=tk.X, pady=(0, 6))
    make_btn(simple_row3, "一键自动转图：切课 → 监控", simple_start_auto,
             c='#1a472a', fc='#9ece6a', fs=13).pack(fill=tk.X)

    simple_status_var = tk.StringVar(value=simple_status_text())
    tk.Label(simple_sec, textvariable=simple_status_var, fg=dim, bg=card_bg,
             font=("Microsoft YaHei UI", 10), anchor="w").pack(fill=tk.X)

    # --- 组列表 ---
    g_sec = card(left, "组列表")
    g_sec.pack(fill=tk.X, pady=(0, 10))

    gf = tk.Frame(g_sec, bg=card_bg)
    gf.pack(fill=tk.X)

    group_listbox = tk.Listbox(gf, bg=entry_bg, fg=fg, selectbackground=accent,
                               selectforeground="#1a1b26", relief="flat",
                               font=("Consolas", 12), activestyle="none",
                               highlightthickness=0, height=6, selectmode=tk.SINGLE)
    group_listbox.pack(side=tk.LEFT, fill=tk.X, expand=True)
    gs = tk.Scrollbar(gf, command=group_listbox.yview, bg=card_bg, troughcolor=entry_bg)
    gs.pack(side=tk.RIGHT, fill=tk.Y)
    group_listbox.config(yscrollcommand=gs.set)

    gb = tk.Frame(g_sec, bg=card_bg)
    gb.pack(fill=tk.X, pady=(6, 0))
    make_btn(gb, "+ 新建", lambda: add_group(), c='#1a472a', fc='#9ece6a', fs=10).pack(side=tk.LEFT, padx=(0, 4))
    make_btn(gb, "删除", delete_group, c='#4a1a2a', fc='#f7768e', fs=10).pack(side=tk.LEFT, padx=4)
    make_btn(gb, "重命名", rename_group, fs=10).pack(side=tk.LEFT, padx=4)
    make_btn(gb, "▲", move_group_up, w=2, fs=10).pack(side=tk.LEFT, padx=4)
    make_btn(gb, "▼", move_group_down, w=2, fs=10).pack(side=tk.LEFT, padx=4)

    # --- 点击序列 ---
    c_sec = card(left, "点击序列")
    c_sec.pack(fill=tk.BOTH, expand=True, pady=(0, 0))

    cf = tk.Frame(c_sec, bg=card_bg)
    cf.pack(fill=tk.BOTH, expand=True)

    click_listbox = tk.Listbox(cf, bg=entry_bg, fg=fg, selectbackground=accent,
                               selectforeground="#1a1b26", relief="flat",
                               font=("Consolas", 12), activestyle="none",
                               highlightthickness=0, height=10, selectmode=tk.SINGLE)
    click_listbox.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
    cs = tk.Scrollbar(cf, command=click_listbox.yview, bg=card_bg, troughcolor=entry_bg)
    cs.pack(side=tk.RIGHT, fill=tk.Y)
    click_listbox.config(yscrollcommand=cs.set)

    cb = tk.Frame(c_sec, bg=card_bg)
    cb.pack(fill=tk.X, pady=(6, 0))
    make_btn(cb, "▲ 上移", move_click_up, fs=10).pack(side=tk.LEFT, padx=(0, 4))
    make_btn(cb, "▼ 下移", move_click_down, fs=10).pack(side=tk.LEFT, padx=4)
    make_btn(cb, "删除", delete_click, c='#4a1a2a', fc='#f7768e', fs=10).pack(side=tk.LEFT, padx=4)
    make_btn(cb, "改目标", edit_click_target, fs=10).pack(side=tk.LEFT, padx=4)
    make_btn(cb, "改延迟", edit_click_delay, fs=10).pack(side=tk.LEFT, padx=4)
    make_btn(cb, "清空", clear_clicks, c='#4a1a2a', fc='#f7768e', fs=10).pack(side=tk.LEFT, padx=4)

    # ===== 右栏：设置 + 执行 =====

    # --- 当前组设置 ---
    s_sec = card(right, "当前组设置")
    s_sec.pack(fill=tk.X, pady=(0, 10))

    r1 = tk.Frame(s_sec, bg=card_bg); r1.pack(fill=tk.X, pady=4)
    tk.Label(r1, text="循环:", fg=dim, bg=card_bg, font=("Microsoft YaHei UI", 11)).pack(side=tk.LEFT)
    repeat_var = tk.StringVar(value="1")
    tk.Entry(r1, textvariable=repeat_var, width=4, bg=entry_bg, fg=fg,
             insertbackground=fg, relief="flat", font=("Consolas", 12)).pack(side=tk.LEFT, padx=8)

    r1b = tk.Frame(s_sec, bg=card_bg); r1b.pack(fill=tk.X, pady=4)
    tk.Label(r1b, text="课程时长(s):", fg=dim, bg=card_bg, font=("Microsoft YaHei UI", 11)).pack(side=tk.LEFT)
    next_delay_var = tk.StringVar(value="600")
    tk.Entry(r1b, textvariable=next_delay_var, width=8, bg=entry_bg, fg=fg,
             insertbackground=fg, relief="flat", font=("Consolas", 12)).pack(side=tk.LEFT, padx=8)

    r1c = tk.Frame(s_sec, bg=card_bg); r1c.pack(fill=tk.X, pady=(4, 6))
    make_btn(r1c, "应用当前组设置", apply_group_settings, c=accent, fc=bg, fs=11).pack(fill=tk.X)

    r2 = tk.Frame(s_sec, bg=card_bg); r2.pack(fill=tk.X, pady=4)
    loop_var = tk.BooleanVar(value=state["global_group_loop"])
    tk.Checkbutton(r2, text="全局循环 A→B→C→A...", variable=loop_var,
                   command=toggle_global_loop, fg=fg, bg=card_bg, selectcolor=entry_bg,
                   activebackground=card_bg, activeforeground=fg,
                   font=("Microsoft YaHei UI", 10)).pack(side=tk.LEFT)

    r3 = tk.Frame(s_sec, bg=card_bg); r3.pack(fill=tk.X, pady=4)
    tk.Label(r3, text="录制延迟(s):", fg=dim, bg=card_bg, font=("Microsoft YaHei UI", 11)).pack(side=tk.LEFT)
    delay_var = tk.StringVar(value=str(state["default_delay"]))
    tk.Entry(r3, textvariable=delay_var, width=4, bg=entry_bg, fg=fg,
             insertbackground=fg, relief="flat", font=("Consolas", 12)).pack(side=tk.LEFT, padx=8)
    make_btn(r3, "应用", apply_default_delay, c=accent, fc=bg, fs=11).pack(side=tk.LEFT, padx=8)

    r4 = tk.Frame(s_sec, bg=card_bg); r4.pack(fill=tk.X, pady=4)
    tk.Label(r4, text="定时(分):", fg=dim, bg=card_bg, font=("Microsoft YaHei UI", 11)).pack(side=tk.LEFT)
    interval_var = tk.StringVar(value=str(state["interval_seconds"] // 60))
    tk.Entry(r4, textvariable=interval_var, width=4, bg=entry_bg, fg=fg,
             insertbackground=fg, relief="flat", font=("Consolas", 12)).pack(side=tk.LEFT, padx=8)
    make_btn(r4, "应用", apply_interval, c=accent, fc=bg, fs=11).pack(side=tk.LEFT, padx=8)

    # --- 窗口锁定 ---
    w_sec = card(right, "窗口锁定")
    w_sec.pack(fill=tk.X, pady=(0, 10))

    window_info_var = tk.StringVar(value="屏幕截取模式")
    tk.Label(w_sec, textvariable=window_info_var, fg=dim, bg=card_bg,
             font=("Microsoft YaHei UI", 10), anchor="w").pack(fill=tk.X)
    wb = tk.Frame(w_sec, bg=card_bg); wb.pack(fill=tk.X, pady=(6, 0))
    make_btn(wb, "锁定窗口", gui_pick_window, c='#2a4a6a', fc='#a9d4e0', fs=10).pack(side=tk.LEFT, padx=(0, 6))
    make_btn(wb, "解锁", gui_unlock_window, c='#4a2a3a', fc='#e8a0b0', fs=10).pack(side=tk.LEFT)

    # --- 倒计时 ---
    t_sec = card(right, "定时倒计时")
    t_sec.pack(fill=tk.X, pady=(0, 10))

    countdown_var = tk.StringVar(value="--:--")
    cd_label = tk.Label(t_sec, textvariable=countdown_var, fg=accent, bg=card_bg,
                        font=("Consolas", 34, "bold"))
    cd_label.pack(pady=4)
    tb = tk.Frame(t_sec, bg=card_bg); tb.pack(fill=tk.X)
    make_btn(tb, "启/停", toggle_timer, fs=10).pack(side=tk.LEFT, padx=(0, 6))
    make_btn(tb, "重置", exec_reset_timer, fs=10).pack(side=tk.LEFT)

    # --- 执行控制（大按钮）---
    e_sec = card(right, "执行控制")
    e_sec.pack(fill=tk.X, pady=(0, 10))

    eb1 = tk.Frame(e_sec, bg=card_bg); eb1.pack(fill=tk.X, pady=(0, 4))
    make_btn(eb1, "▶ 执行当前组",
             lambda: threading.Thread(target=play_current_group, daemon=True).start(),
             c='#1a472a', fc='#9ece6a', fs=11).pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(0, 4))
    make_btn(eb1, "旧全部组",
             lambda: threading.Thread(target=play_all_groups, daemon=True).start(),
             c='#2b2f44', fc=dim, fs=11).pack(side=tk.LEFT, fill=tk.X, expand=True)

    eb2 = tk.Frame(e_sec, bg=card_bg); eb2.pack(fill=tk.X)
    make_btn(eb2, "■ 停止", stop_play, c='#4a1a2a', fc='#f7768e',
             w=22, fs=11).pack(pady=(4, 0))

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
                    log("定时旧全部组已停用，请使用绿色“一键自动转图”")
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
        log(f"  [{i+1}] {g['name']}: {len(g.get('clicks', []))}步 循环{g.get('repeat', 1)}次 时长{_get_group_duration(g):.0f}s")
    log("Alt+Q=选区域 | Alt+W=监测 | Alt+E=截图 | Alt+R=录制 | Alt+T=执行当前组 | Alt+U=执行全部组 | Alt+Y=停止 | Alt+I=锁定窗口 | Alt+O=退出")

    root = create_gui()
    root.mainloop()

if __name__ == "__main__":
    main()
