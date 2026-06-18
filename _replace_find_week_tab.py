"""替换 _find_week_tab 函数的脚本"""
import re

with open("ppt_extractor.py", "r", encoding="utf-8") as f:
    content = f.read()

# 找到 _find_week_tab 的开始和结束位置
start_marker = "def _find_week_tab(week, tries=OCR_SCROLL_TRIES):"
end_marker = "def _select_week_tab(week):"

start_idx = content.index(start_marker)
end_idx = content.index(end_marker)

old_code = content[start_idx:end_idx]

new_code = '''def _find_week_tab(week, tries=OCR_SCROLL_TRIES):
    """切换到目标周。策略：标题确认 + 逐步点击，不依赖OCR识别周按钮文字。
    1. 标题已是目标周 → 直接返回
    2. 弹窗全图OCR找"第N周" → 直接点击
    3. 逐步在弹窗中点击，每次点击后读标题确认
    4. 都失败 → 返回None，跳过本节
    """
    # ---- 保存调试截图 ----
    try:
        _save_debug_screenshot("week_tab_enter")
    except Exception:
        pass

    # ---- 第0步：标题已是目标周 → 直接返回 ----
    current_week = _current_switch_week()
    if current_week == week:
        log(f"  当前标题已为第{week}周，跳过点击")
        win = _locked_window_region()
        if win:
            x, y, w, h = win
            return (x + w // 2, y + int(h * 0.45))
        return None

    log(f"  需要从第{current_week}周切换到第{week}周")

    # ---- 第1步：弹窗全图OCR找"第N周" ----
    win = _locked_window_region()
    if win:
        wx, wy, ww, wh = win
        full_top_region = (wx + int(ww * 0.02), wy + int(wh * 0.20), int(ww * 0.96), int(wh * 0.40))
        for psm in [11, 6, 3]:
            items = _ocr_items(full_top_region, psm=psm)
            week_hits = {}
            for it in items:
                m = re.search(r"第?0?(\d{1,2})\\s*周", it["text"])
                if m:
                    w_num = int(m.group(1))
                    if w_num not in week_hits or it["conf"] > week_hits[w_num]["conf"]:
                        week_hits[w_num] = it
            if week_hits:
                visible = sorted(week_hits.keys())
                log(f"  弹窗全文扫描(psm={psm}): 识别到周={visible}")
                if week in week_hits:
                    target = week_hits[week]
                    log(f"  全文扫描命中第{week}周 -> ({target[chr(99)+chr(120)]},{target[chr(99)+chr(121)]}) 置信度={target[chr(99)+chr(111)+chr(110)+chr(102)]:.0f}")
                    _strong_click(target["cx"], target["cy"])
                    time.sleep(1.0)
                    new_week = _current_switch_week()
                    if new_week == week:
                        log(f"  全文扫描点击成功: 标题变为第{week}周")
                        return (target["cx"], target["cy"])
                    log(f"  点击后标题={new_week}(目标第{week}周)")
                break
        log(f"  弹窗全文扫描未找到第{week}周")

    # ---- 第2步：逐步点击策略 ----
    # 利用标题确认当前位置，在弹窗中逐步点击直到标题变为目标周
    if not win:
        log(f"  未锁定窗口，无法逐步点击")
        return None

    wx, wy, ww, wh = win
    current = _current_switch_week()

    # 确定点击的 y 坐标（标题下方约25-35px）
    title_region = _switch_title_region()
    title_items = _ocr_items(title_region) if title_region else []
    title_with_week = [it for it in title_items if "周" in it["text"]]
    if title_with_week:
        title_bottom_y = int(max(it["cy"] + it["h"] // 2 for it in title_with_week))
        click_y = title_bottom_y + 25
        log(f"  标题底部y={title_bottom_y}, 周按钮行y={click_y}")
    else:
        click_y = wy + int(wh * 0.40)
        log(f"  标题未识别，使用固定比例y={click_y}")

    # 弹窗水平范围
    tabs_left = wx + int(ww * 0.03)
    tabs_right = wx + int(ww * 0.97)
    tabs_width = tabs_right - tabs_left
    num_slots = 10

    log(f"  开始逐步点击: 从第{current}周到第{week}周, y={click_y}")

    for attempt in range(8):
        # 每次先检查标题
        current = _current_switch_week()
        if current == week:
            log(f"  逐步点击成功: 已切换到第{week}周 (第{attempt+1}次尝试)")
            return (wx + ww // 2, click_y)

        if current is None:
            log(f"  标题未识别, 第{attempt+1}次尝试")
            time.sleep(0.5)
            continue

        # 计算点击位置：根据当前周和目标周的距离
        diff = week - current
        # 每个"槽位"约代表1.5个周（12周÷8个可见≈1.5）
        slot = num_slots // 2 + int(diff / 1.5)
        slot = max(0, min(num_slots - 1, slot))
        click_x = int(tabs_left + (slot + 0.5) * tabs_width / num_slots)

        log(f"  逐步{attempt+1}: 第{current}周→第{week}周, 槽位{slot+1}/{num_slots} -> ({click_x},{click_y})")
        _strong_click(click_x, click_y)
        time.sleep(1.0)

        new_week = _current_switch_week()
        if new_week == week:
            log(f"  逐步点击成功: 已切换到第{week}周")
            return (click_x, click_y)
        if new_week is not None and new_week != current:
            log(f"  点击后标题从第{current}周变为第{new_week}周, 继续调整")
            current = new_week
            continue

        # 点击没效果，尝试从左到右扫描
        log(f"  点击后标题未变(={new_week}), 开始扫描")
        for scan_slot in range(num_slots):
            scan_x = int(tabs_left + (scan_slot + 0.5) * tabs_width / num_slots)
            if scan_slot == slot:
                continue  # 跳过已经点过的位置
            _strong_click(scan_x, click_y)
            time.sleep(0.5)
            scan_week = _current_switch_week()
            if scan_week == week:
                log(f"  扫描命中: 槽位{scan_slot+1} -> 第{week}周")
                return (scan_x, click_y)
            if scan_week is not None and scan_week != current:
                log(f"  扫描: 槽位{scan_slot+1}切到第{scan_week}周")
                current = scan_week
                break
        continue

    # 最终检查
    final_week = _current_switch_week()
    if final_week == week:
        log(f"  最终标题确认: 第{week}周")
        return (wx + ww // 2, click_y)

    log(f"  逐步点击未能切换到第{week}周(标题={final_week}), 跳过本节")
    return None


'''

content = content[:start_idx] + new_code + content[end_idx:]

with open("ppt_extractor.py", "w", encoding="utf-8") as f:
    f.write(content)

print("Done! _find_week_tab replaced successfully.")