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

OUTPUT_DIR = r"D:\workspace\ppt_slides"
CHANGE_THRESHOLD = 12
CHECK_INTERVAL = 1.0
HASH_SIMILARITY = 0.95
STABLE_FRAMES = 2

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
                self.ref = g
                self._stable_count = 0
                return True, score_ref, "page_done"
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
    if not region:
        log("请先按 F2 选择区域!")
        return
    x, y, w, h = region
    if w < 10 or h < 10:
        log("区域太小，请重选!")
        return
    try:
        sc = pyautogui.screenshot(region=(x, y, w, h))
        ts = datetime.now().strftime("%H%M%S")
        fn = f"manual_{ts}.png"
        with _lock:
            out_dir = state.get("session_dir") or OUTPUT_DIR
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

    log(f"监测中 ({w}x{h}) @ ({x},{y})")
    log(f"输出目录: {out_dir}")

    while True:
        with _lock:
            if not state["running"] or not state["monitoring"]:
                break
        try:
            sc = pyautogui.screenshot(region=(x, y, w, h))
        except Exception as e:
            log(f"截图失败: {e}")
            time.sleep(1)
            continue

        frame = cv2.cvtColor(np.array(sc), cv2.COLOR_RGB2BGR)
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
            sc.save(os.path.join(out_dir, fn))
            log(f"[{cnt:03d}] 翻页 ({score:.0f}%) -> {fn}")
            cd = 5

        time.sleep(CHECK_INTERVAL)

    elapsed = time.time() - start
    log(f"监测结束 | {elapsed:.0f}s | {cnt}张 | 目录: {os.path.basename(out_dir)}/")
    with _lock:
        state["monitoring"] = False

# ====================== 鼠标监听 ======================
def on_mouse_click(x, y, button, pressed):
    if not pressed or button != mouse.Button.left:
        return
    with _lock:
        picking = state["picking"]
        recording = state["recording"]
        step = state["pick_step"]
        gidx = state["current_group_index"]

    if recording:
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

    if step == 1:
        with _lock:
            state["pick_p1"] = (x, y)
            state["pick_step"] = 2
        log(f"左上: ({x},{y}) -> 请点击右下角")
    elif step == 2:
        with _lock:
            p1 = state["pick_p1"]
        if p1:
            x1, y1 = p1
            rx, ry = min(x1, x), min(y1, y)
            rw, rh = abs(x - x1), abs(y - y1)
            with _lock:
                state["region"] = (rx, ry, rw, rh)
                state["picking"] = False
                state["pick_step"] = 0
                state["pick_p1"] = None
            log(f"选区: ({rx},{ry}) {rw}x{rh}")

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
            log("选区域: 点击画面左上角")
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
                    return
                r = region
                if r[2] < 10 or r[3] < 10:
                    log("区域太小，重选!")
                    return
                _start_monitoring(r, "手动")
            else:
                _stop_monitoring()
        elif k == 'f4':
            manual_capture()
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
            else:
                with _lock:
                    state["recording"] = False
                    save_groups(state["groups"])
                    n = len(state["groups"][gidx]["clicks"])
                log(f"[{name}] 录制结束，共 {n} 步")
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
                    log("取消选区域")
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
    root.title("PPT扒取器 v6.0 - 多组模式")
    root.geometry("500x650")
    root.resizable(False, False)
    root.attributes('-topmost', True)

    bg = "#2b2b2b"
    fg = "#e0e0e0"
    accent = "#4a9eff"
    btn_bg = "#3c3c3c"
    entry_bg = "#1e1e1e"
    root.configure(bg=bg)

    # ---- 状态行 ----
    status_var = tk.StringVar(value="就绪")
    tk.Label(root, text="状态:", fg=fg, bg=bg, font=("", 9)).place(x=10, y=8)
    tk.Label(root, textvariable=status_var, fg=accent, bg=bg, font=("", 9, "bold")).place(x=50, y=8)

    # ---- 当前组名显示 ----
    group_name_var = tk.StringVar(value="")
    tk.Label(root, text="当前组:", fg=fg, bg=bg, font=("", 9)).place(x=220, y=8)
    tk.Label(root, textvariable=group_name_var, fg="#ff9944", bg=bg, font=("", 9, "bold")).place(x=278, y=8)

    # ==================== 组列表区 ====================
    tk.Label(root, text="组列表（每个组 = 一个独立个体）:", fg=fg, bg=bg, font=("", 9)).place(x=10, y=32)

    group_frame = tk.Frame(root, bg=bg)
    group_frame.place(x=10, y=54, width=480, height=120)

    group_listbox = tk.Listbox(group_frame, bg=entry_bg, fg=fg, selectbackground=accent,
                               selectforeground="#fff", relief="flat", font=("Consolas", 9),
                               activestyle="none", highlightthickness=0)
    group_listbox.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

    group_scrollbar = tk.Scrollbar(group_frame, command=group_listbox.yview)
    group_scrollbar.pack(side=tk.RIGHT, fill=tk.Y)
    group_listbox.config(yscrollcommand=group_scrollbar.set)

    def refresh_group_listbox():
        group_listbox.delete(0, tk.END)
        with _lock:
            groups = list(state["groups"])
            gidx = state["current_group_index"]
        for i, g in enumerate(groups):
            marker = "> " if i == gidx else "  "
            nclicks = len(g.get("clicks", []))
            repeat = g.get("repeat", 1)
            nd = g.get("next_delay", 600)
            group_listbox.insert(tk.END, f"{marker}[{i+1}] {g['name']} | {nclicks}步 | 循环{repeat}次 | 间隔{nd:.0f}s")

    def on_group_select(_evt=None):
        sel = group_listbox.curselection()
        if sel:
            with _lock:
                state["current_group_index"] = sel[0]
            refresh_group_listbox()
            refresh_click_listbox()
            refresh_group_settings()

    group_listbox.bind('<<ListboxSelect>>', on_group_select)

    # ---- 组操作按钮 ----
    group_btn_y = 178

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
        dlg.geometry("280x100")
        dlg.resizable(False, False)
        dlg.configure(bg=bg)
        dlg.attributes('-topmost', True)
        dlg.transient(root)
        dlg.grab_set()

        tk.Label(dlg, text="组名称:", fg=fg, bg=bg).pack(pady=(10, 5))
        var = tk.StringVar(value=old_name)
        entry = tk.Entry(dlg, textvariable=var, width=25, bg=entry_bg, fg=fg,
                         insertbackground=fg, relief="flat", font=("", 10))
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

        tk.Button(dlg, text="确定", command=confirm, bg=btn_bg, fg=fg, relief="flat").pack(pady=5)
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

    tk.Button(root, text="新建组", command=add_group, bg="#225522", fg=fg, relief="flat",
              font=("", 8), width=7).place(x=10, y=group_btn_y)
    tk.Button(root, text="删除组", command=delete_group, bg="#552222", fg=fg, relief="flat",
              font=("", 8), width=7).place(x=72, y=group_btn_y)
    tk.Button(root, text="重命名", command=rename_group, bg=btn_bg, fg=fg, relief="flat",
              font=("", 8), width=7).place(x=134, y=group_btn_y)
    tk.Button(root, text="上移", command=move_group_up, bg=btn_bg, fg=fg, relief="flat",
              font=("", 8), width=5).place(x=196, y=group_btn_y)
    tk.Button(root, text="下移", command=move_group_down, bg=btn_bg, fg=fg, relief="flat",
              font=("", 8), width=5).place(x=251, y=group_btn_y)

    # ==================== 点击序列区 ====================
    tk.Label(root, text="当前组的点击序列:", fg=fg, bg=bg, font=("", 9)).place(x=10, y=210)

    click_frame = tk.Frame(root, bg=bg)
    click_frame.place(x=10, y=232, width=480, height=130)

    click_listbox = tk.Listbox(click_frame, bg=entry_bg, fg=fg, selectbackground=accent,
                               selectforeground="#fff", relief="flat", font=("Consolas", 9),
                               activestyle="none", highlightthickness=0)
    click_listbox.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

    click_scrollbar = tk.Scrollbar(click_frame, command=click_listbox.yview)
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
            click_listbox.insert(tk.END, f"#{i+1}: ({step['x']:>4},{step['y']:>4})  延迟 {delay:.1f}s")

    # ---- 点击编辑按钮 ----
    click_btn_y = 368

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
        dlg.geometry("200x100")
        dlg.resizable(False, False)
        dlg.configure(bg=bg)
        dlg.attributes('-topmost', True)
        dlg.transient(root)
        dlg.grab_set()

        tk.Label(dlg, text=f"第 {idx+1} 步延迟 (秒):", fg=fg, bg=bg).pack(pady=(10, 5))
        var = tk.StringVar(value=str(current))
        entry = tk.Entry(dlg, textvariable=var, width=8, bg=entry_bg, fg=fg,
                         insertbackground=fg, relief="flat", font=("", 10))
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

        tk.Button(dlg, text="确定", command=confirm, bg=btn_bg, fg=fg, relief="flat").pack(pady=5)
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

    tk.Button(root, text="上移", command=move_click_up, bg=btn_bg, fg=fg, relief="flat",
              font=("", 8), width=5).place(x=10, y=click_btn_y)
    tk.Button(root, text="下移", command=move_click_down, bg=btn_bg, fg=fg, relief="flat",
              font=("", 8), width=5).place(x=60, y=click_btn_y)
    tk.Button(root, text="删步", command=delete_click, bg=btn_bg, fg=fg, relief="flat",
              font=("", 8), width=5).place(x=110, y=click_btn_y)
    tk.Button(root, text="改延迟", command=edit_click_delay, bg=btn_bg, fg=fg, relief="flat",
              font=("", 8), width=6).place(x=160, y=click_btn_y)
    tk.Button(root, text="清空", command=clear_clicks, bg="#552222", fg=fg, relief="flat",
              font=("", 8), width=5).place(x=215, y=click_btn_y)

    # ---- 默认延迟 ----
    tk.Label(root, text="新录制的默认延迟 (秒):", fg=fg, bg=bg, font=("", 9)).place(x=10, y=400)

    delay_var = tk.StringVar(value=str(state["default_delay"]))
    delay_entry = tk.Entry(root, textvariable=delay_var, width=5,
                           bg=entry_bg, fg=fg, insertbackground=fg,
                           relief="flat", font=("", 9))
    delay_entry.place(x=165, y=399)

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

    tk.Button(root, text="应用", command=apply_default_delay,
              bg=btn_bg, fg=fg, relief="flat", font=("", 8)).place(x=208, y=398)

    # ==================== 组级设置 ====================
    tk.Label(root, text="当前组设置:", fg=fg, bg=bg, font=("", 9, "bold")).place(x=10, y=428)

    # Repeat
    tk.Label(root, text="本组循环次数:", fg=fg, bg=bg, font=("", 9)).place(x=10, y=452)
    repeat_var = tk.StringVar(value="1")
    repeat_entry = tk.Entry(root, textvariable=repeat_var, width=5,
                            bg=entry_bg, fg=fg, insertbackground=fg,
                            relief="flat", font=("", 9))
    repeat_entry.place(x=110, y=451)

    # Next delay
    tk.Label(root, text="到下一组延迟 (秒):", fg=fg, bg=bg, font=("", 9)).place(x=170, y=452)
    next_delay_var = tk.StringVar(value="600")
    next_delay_entry = tk.Entry(root, textvariable=next_delay_var, width=6,
                                 bg=entry_bg, fg=fg, insertbackground=fg,
                                 relief="flat", font=("", 9))
    next_delay_entry.place(x=290, y=451)

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

    tk.Button(root, text="应用", command=apply_group_settings,
              bg=btn_bg, fg=fg, relief="flat", font=("", 8)).place(x=350, y=450)

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
    loop_var = tk.BooleanVar(value=state["global_group_loop"])

    def toggle_global_loop():
        v = loop_var.get()
        with _lock:
            state["global_group_loop"] = v
        _config["global_group_loop"] = v
        save_config(_config)
        log(f"全局组循环: {'开' if v else '关'}")

    tk.Checkbutton(root, text="全局组循环（A→B→C→A→B→C... 执行全部组时生效）",
                   variable=loop_var, command=toggle_global_loop,
                   fg=fg, bg=bg, selectcolor=bg, activebackground=bg,
                   font=("", 9)).place(x=10, y=480)

    # ---- 定时间隔 ----
    tk.Label(root, text="定时触发间隔 (分钟):", fg=fg, bg=bg, font=("", 9)).place(x=10, y=508)

    interval_var = tk.StringVar(value=str(state["interval_seconds"] // 60))
    interval_entry = tk.Entry(root, textvariable=interval_var, width=5,
                              bg=entry_bg, fg=fg, insertbackground=fg,
                              relief="flat", font=("", 9))
    interval_entry.place(x=150, y=507)

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

    tk.Button(root, text="应用", command=apply_interval,
              bg=btn_bg, fg=fg, relief="flat", font=("", 8)).place(x=195, y=506)

    # ---- 倒计时 ----
    tk.Label(root, text="下次执行倒计时:", fg=fg, bg=bg, font=("", 9)).place(x=10, y=535)

    countdown_var = tk.StringVar(value="--:--")
    cd_label = tk.Label(root, textvariable=countdown_var, fg=accent, bg=bg,
                        font=("Consolas", 26, "bold"))
    cd_label.place(x=10, y=555)

    # ==================== 底部按钮 ====================
    btn_y = 600
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

    tk.Button(root, text="启/停定时", command=toggle_timer,
              bg=btn_bg, fg=fg, relief="flat", font=("", 9), width=8).place(x=10, y=btn_y)

    tk.Button(root, text="执行当前组", command=lambda: threading.Thread(target=play_current_group, daemon=True).start(),
              bg="#224488", fg=fg, relief="flat", font=("", 9), width=9).place(x=85, y=btn_y)

    tk.Button(root, text="执行全部组", command=lambda: threading.Thread(target=play_all_groups, daemon=True).start(),
              bg="#225522", fg=fg, relief="flat", font=("", 9), width=9).place(x=168, y=btn_y)

    tk.Button(root, text="停止", command=stop_play,
              bg="#552222", fg=fg, relief="flat", font=("", 9), width=5).place(x=251, y=btn_y)

    tk.Button(root, text="重置计时", command=exec_reset_timer,
              bg=btn_bg, fg=fg, relief="flat", font=("", 9), width=7).place(x=305, y=btn_y)

    # ---- 快捷键提示 ----
    tips = "F2框选 | F3监测 | F4截图 | F5录制到当前组 | F6执行当前组 | F8执行全部组 | F7停止 | F10退出"
    tk.Label(root, text=tips, fg="#666", bg=bg, font=("", 7)).place(x=10, y=630)

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

    mouse_listener = mouse.Listener(on_click=on_mouse_click)
    mouse_listener.start()

    kb_thread = threading.Thread(target=start_keyboard, daemon=True)
    kb_thread.start()

    mins = state["interval_seconds"] // 60
    d = state["default_delay"]
    lp = "开" if state["global_group_loop"] else "关"
    log(f"已加载 {len(state['groups'])} 个组 | 默认延迟 {d:.1f}s | 全局循环={lp} | 定时间隔 {mins} 分钟")
    for i, g in enumerate(state["groups"]):
        log(f"  [{i+1}] {g['name']}: {len(g['clicks'])}步 循环{g['repeat']}次 间隔{g['next_delay']:.0f}s")
    log("F2=选区域 | F3=监测 | F4=截图 | F5=录制 | F6=执行当前组 | F8=执行全部组 | F7=停止 | F10=退出")

    root = create_gui()
    root.mainloop()

if __name__ == "__main__":
    main()
