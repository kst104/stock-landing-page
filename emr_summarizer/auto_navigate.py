"""
NGTMediPlus EMR 챠트 자동 탐색 및 화면 수집

캡처: 창 활성화 후 창 bbox ImageGrab (멀티모니터 대응)
탐색:
  1단계 Vision — 노란색 왼쪽 메뉴 패널 위치 파악
  2단계 Vision — 패널 안의 서브메뉴 항목 추출
"""

from __future__ import annotations

import base64
import io
import json
import os
import re
import time

import ctypes
import ctypes.wintypes

import anthropic
import pyautogui
import win32con
import win32gui
import win32process
from PIL import Image, ImageGrab

# FailSafe 비활성화 (좌표가 모서리여도 중단하지 않음)
pyautogui.FAILSAFE = False
pyautogui.PAUSE = 0.05

_NGT_KEYWORDS = [
    "neomed", "ngt", "전자챠트", "전자차트", "emr",
    "차트", "medit", "문서 작성",
]
_WAIT_AFTER_CLICK = 1.0   # 클릭 후 콘텐츠 로드 대기 (기존 2.5s)

# 캡처 제외 항목 (이름에 포함되면 건너뜀)
EXCLUDED_KEYWORDS = ["동의서", "욕창", "상처기록", "영양", "제증명", "보험종별"]


def is_excluded(name: str) -> bool:
    return any(kw in name for kw in EXCLUDED_KEYWORDS)


# ── 창 탐색 ───────────────────────────────────────────────────────────────────

def _enum_windows() -> list[tuple[int, str]]:
    result = []
    def cb(hwnd, _):
        if win32gui.IsWindowVisible(hwnd):
            title = win32gui.GetWindowText(hwnd)
            if title.strip():
                result.append((hwnd, title))
        return True
    win32gui.EnumWindows(cb, None)
    return result


def find_ngt_window() -> tuple[int, str] | None:
    my_pid = os.getpid()
    for hwnd, title in _enum_windows():
        try:
            _, pid = win32process.GetWindowThreadProcessId(hwnd)
            if pid == my_pid:
                continue
        except Exception:
            pass
        if any(kw in title.lower() for kw in _NGT_KEYWORDS):
            return hwnd, title
    return None


def get_all_windows() -> list[str]:
    my_pid = os.getpid()
    result = []
    for hwnd, title in _enum_windows():
        try:
            _, pid = win32process.GetWindowThreadProcessId(hwnd)
            if pid == my_pid:
                continue
        except Exception:
            pass
        result.append(title)
    return result


def get_hwnd_by_title(title: str) -> int | None:
    for hwnd, t in _enum_windows():
        if t == title:
            return hwnd
    return None


# ── 마우스 클릭 (권한 문제 대응) ─────────────────────────────────────────────

class _MOUSEINPUT(ctypes.Structure):
    _fields_ = [
        ("dx",          ctypes.wintypes.LONG),
        ("dy",          ctypes.wintypes.LONG),
        ("mouseData",   ctypes.wintypes.DWORD),
        ("dwFlags",     ctypes.wintypes.DWORD),
        ("time",        ctypes.wintypes.DWORD),
        ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong)),
    ]

class _INPUT(ctypes.Structure):
    class _U(ctypes.Union):
        _fields_ = [("mi", _MOUSEINPUT)]
    _anonymous_ = ("_u",)
    _fields_    = [("type", ctypes.wintypes.DWORD), ("_u", _U)]

_MOUSEEVENTF_MOVE        = 0x0001
_MOUSEEVENTF_LDOWN       = 0x0002
_MOUSEEVENTF_LUP         = 0x0004
_MOUSEEVENTF_ABSOLUTE    = 0x8000
_MOUSEEVENTF_VIRTUALDESK = 0x4000   # 전체 가상 데스크톱 기준 (멀티모니터 필수)
_INPUT_MOUSE             = 0

# 가상 데스크톱 전체 크기 (멀티모니터 합산)
_SM_XVIRTUALSCREEN  = 76
_SM_YVIRTUALSCREEN  = 77
_SM_CXVIRTUALSCREEN = 78
_SM_CYVIRTUALSCREEN = 79


def _send_input_click(x: int, y: int, double: bool = False):
    """
    ctypes.SendInput으로 마우스 클릭.
    가상 데스크톱 전체 기준 ABSOLUTE 좌표 사용 → 멀티모니터 + 보조모니터 정확.
    pyautogui가 UAC/권한 문제로 막힐 때 사용.
    """
    vx = ctypes.windll.user32.GetSystemMetrics(_SM_XVIRTUALSCREEN)
    vy = ctypes.windll.user32.GetSystemMetrics(_SM_YVIRTUALSCREEN)
    vw = ctypes.windll.user32.GetSystemMetrics(_SM_CXVIRTUALSCREEN)
    vh = ctypes.windll.user32.GetSystemMetrics(_SM_CYVIRTUALSCREEN)
    nx = int((x - vx) * 65535 / max(vw, 1))
    ny = int((y - vy) * 65535 / max(vh, 1))

    FLAGS = _MOUSEEVENTF_ABSOLUTE | _MOUSEEVENTF_VIRTUALDESK

    def _send(extra):
        inp = _INPUT()
        inp.type       = _INPUT_MOUSE
        inp.mi.dx      = nx
        inp.mi.dy      = ny
        inp.mi.dwFlags = FLAGS | extra
        ctypes.windll.user32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(inp))

    _send(_MOUSEEVENTF_MOVE)
    time.sleep(0.05)
    _send(_MOUSEEVENTF_LDOWN)
    time.sleep(0.05)
    _send(_MOUSEEVENTF_LUP)

    if double:
        time.sleep(0.12)
        _send(_MOUSEEVENTF_LDOWN)
        time.sleep(0.05)
        _send(_MOUSEEVENTF_LUP)


def _safe_click(x: int, y: int, double: bool = False):
    """
    다단계 클릭 시도:
    1) pyautogui  2) mouse_event  3) SendInput
    모두 실패해도 예외를 삼키고 계속 진행.
    """
    # 가상 데스크톱 전체 범위로 검사 — 보조 모니터(음수 x/y 포함) 대응
    vx = ctypes.windll.user32.GetSystemMetrics(_SM_XVIRTUALSCREEN)
    vy = ctypes.windll.user32.GetSystemMetrics(_SM_YVIRTUALSCREEN)
    vw = ctypes.windll.user32.GetSystemMetrics(_SM_CXVIRTUALSCREEN)
    vh = ctypes.windll.user32.GetSystemMetrics(_SM_CYVIRTUALSCREEN)
    if not (vx <= x < vx + vw and vy <= y < vy + vh):
        return

    # 커서 이동
    ctypes.windll.user32.SetCursorPos(x, y)
    time.sleep(0.1)

    def _do_click(dbl=False):
        # 시도 1: pyautogui
        try:
            if dbl:
                pyautogui.doubleClick(x, y)
            else:
                pyautogui.click(x, y)
            return
        except Exception:
            pass
        # 시도 2: mouse_event (구형 API)
        try:
            LD, LU = 0x0002, 0x0004
            ctypes.windll.user32.mouse_event(LD, 0, 0, 0, 0)
            time.sleep(0.05)
            ctypes.windll.user32.mouse_event(LU, 0, 0, 0, 0)
            if dbl:
                time.sleep(0.1)
                ctypes.windll.user32.mouse_event(LD, 0, 0, 0, 0)
                time.sleep(0.05)
                ctypes.windll.user32.mouse_event(LU, 0, 0, 0, 0)
            return
        except Exception:
            pass
        # 시도 3: SendInput
        try:
            _send_input_click(x, y, double=dbl)
        except Exception:
            pass

    _do_click()
    if double:
        time.sleep(0.15)
        _do_click()


def _send_key(vk: int):
    """가상 키 전송 (pyautogui 실패 시 keybd_event 폴백)"""
    try:
        key_map = {0x0D: "enter", 0x28: "down", 0x26: "up"}
        if vk in key_map:
            pyautogui.press(key_map[vk])
            return
    except Exception:
        pass
    try:
        KEYEVENTF_KEYUP = 0x0002
        ctypes.windll.user32.keybd_event(vk, 0, 0, 0)
        time.sleep(0.05)
        ctypes.windll.user32.keybd_event(vk, 0, KEYEVENTF_KEYUP, 0)
    except Exception:
        pass


# ── 창 활성화 + 캡처 (멀티모니터 대응) ───────────────────────────────────────

def activate_window(hwnd: int):
    placement = win32gui.GetWindowPlacement(hwnd)
    if placement[1] == win32con.SW_SHOWMINIMIZED:
        win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
    win32gui.SetForegroundWindow(hwnd)
    time.sleep(0.3)


def capture_screen(hwnd: int) -> Image.Image:
    """
    창을 활성화한 뒤 창 bbox로 캡처.
    bbox 지정 방식으로 보조모니터도 올바르게 캡처.
    """
    activate_window(hwnd)
    time.sleep(0.1)
    rect = win32gui.GetWindowRect(hwnd)
    return ImageGrab.grab(bbox=rect)


# ── Vision 유틸 ───────────────────────────────────────────────────────────────

def _img_to_b64(img: Image.Image) -> str:
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return base64.standard_b64encode(buf.getvalue()).decode()


def _vision_call(client: anthropic.Anthropic, model: str,
                 img: Image.Image, prompt: str, max_tokens: int = 1500) -> str:
    resp = client.messages.create(
        model=model,
        max_tokens=max_tokens,
        messages=[{"role": "user", "content": [
            {"type": "image", "source": {
                "type": "base64", "media_type": "image/png",
                "data": _img_to_b64(img)}},
            {"type": "text", "text": prompt},
        ]}],
    )
    return resp.content[0].text.strip()


def _parse_json(text: str):
    if "```" in text:
        s = text.find("[") if "[" in text else text.find("{")
        e = text.rfind("]") + 1 if "]" in text else text.rfind("}") + 1
        text = text[s:e]
    return json.loads(text)


# ── 1단계: 노란 메뉴 패널 bbox 파악 ─────────────────────────────────────────

def find_left_panel_bounds(screenshot: Image.Image,
                           api_key: str, model: str) -> dict | None:
    """
    노란색/황색 왼쪽 메뉴 패널의 경계를 Vision으로 파악.
    반환: {"x0":0.0,"y0":0.0,"x1":0.18,"y1":1.0} (비율) 또는 None
    """
    prompt = (
        "이 EMR 화면에서 왼쪽에 있는 노란색 또는 황색 배경의 메뉴/사이드바 패널을 찾아주세요.\n"
        "그 패널의 경계를 이미지 전체 크기 기준 비율(0.0~1.0)로 반환하세요.\n"
        "JSON 한 줄만 (다른 텍스트 없이):\n"
        '{"x0":0.0,"y0":0.05,"x1":0.18,"y1":0.98}\n'
        "노란 패널이 없으면: null"
    )
    client = anthropic.Anthropic(api_key=api_key)
    text = _vision_call(client, model, screenshot, prompt, max_tokens=200)
    try:
        if "null" in text.lower():
            return None
        data = _parse_json(text)
        if all(k in data for k in ("x0", "y0", "x1", "y1")):
            return data
    except Exception:
        pass
    return None


# ── 2단계: 패널 내 최상위 카테고리 항목 추출 ─────────────────────────────────

def find_top_level_menu_items(panel_img: Image.Image,
                              api_key: str, model: str) -> list[dict]:
    """
    크롭된 패널에서 클릭 시 하위 서브메뉴가 펼쳐지는 최상위 카테고리 탭/항목 추출.
    반환: [{"name":..., "x_ratio":..., "y_ratio":...}, ...]  (panel_img 기준 비율)
    """
    prompt = (
        "이 이미지는 EMR 프로그램의 왼쪽 메뉴 패널입니다.\n"
        "클릭하면 하위 서브메뉴가 펼쳐지는 최상위 카테고리 탭/항목을 모두 찾아주세요.\n\n"
        "포함: 굵게 표시된 카테고리 제목, 탭 버튼, 폴더 역할을 하는 헤더 항목\n"
        "제외: 이미 들여쓰기된 하위 서브메뉴 항목, '보험종별' 같은 필터·드롭다운, 날짜 입력 필드\n\n"
        "각 항목의 클릭 위치를 이 이미지 기준 비율(0.0~1.0)로 반환.\n"
        "위→아래 순서, JSON 배열만 응답:\n"
        '[{"name":"항목이름","x_ratio":0.5,"y_ratio":0.08},...]\n'
        "항목 없으면 [] 반환."
    )
    client = anthropic.Anthropic(api_key=api_key)
    text = _vision_call(client, model, panel_img, prompt)
    try:
        items = _parse_json(text)
        return [x for x in items
                if "name" in x and "x_ratio" in x and "y_ratio" in x]
    except Exception:
        return []


# ── 3단계: 패널 내 서브메뉴 항목 추출 ────────────────────────────────────────

def find_submenu_items(panel_img: Image.Image,
                       api_key: str, model: str) -> list[dict]:
    """
    크롭된 메뉴 패널 이미지에서 클릭 가능한 서브메뉴 항목 추출.
    반환: [{"name":..., "x_ratio":..., "y_ratio":...}, ...]
          좌표는 panel_img 기준 비율.
    """
    prompt = (
        "이 이미지는 EMR 프로그램의 왼쪽 메뉴 패널만 크롭한 것입니다.\n"
        "이 패널에서 클릭하면 오른쪽에 내용이 표시되는 서브메뉴 항목을 모두 찾아주세요.\n\n"
        "포함: 들여쓰기된 하위 항목, 문서명, 기록지명 등 실제 내용으로 연결되는 항목\n"
        "제외: 그룹/카테고리 제목(폴더처럼 하위 항목을 묶는 헤더), '보험종별' 필터, 날짜 입력\n\n"
        "각 항목의 클릭 위치를 이 이미지(패널) 크기 기준 비율(0.0~1.0)로 반환.\n"
        "위→아래 순서로, JSON 배열만 응답:\n"
        '[{"name":"항목이름","x_ratio":0.5,"y_ratio":0.12},...]\n'
        "항목 없으면 [] 반환."
    )
    client = anthropic.Anthropic(api_key=api_key)
    text = _vision_call(client, model, panel_img, prompt)
    try:
        items = _parse_json(text)
        return [x for x in items
                if "name" in x and "x_ratio" in x and "y_ratio" in x]
    except Exception:
        return []


# ── 빠른 단일 패스: 전체 이미지 기준 좌표로 왼쪽 메뉴 항목 추출 ──────────────

def find_left_menu_items_full(screenshot: Image.Image,
                              api_key: str, model: str) -> list[dict]:
    """
    전체 창 스크린샷에서 왼쪽 메뉴의 클릭 가능한 항목을 한 번에 추출.
    좌표는 '전체 이미지' 기준 비율 → 변환 단계가 없어 오차가 적음.
    반환: [{"name":..., "x_ratio":..., "y_ratio":...}, ...]
    """
    prompt = (
        "이 EMR 화면의 왼쪽 메뉴/사이드바에서, 클릭하면 오른쪽에 문서·기록 내용이 표시되는 "
        "항목을 모두 찾아주세요.\n\n"
        "포함: 문서명, 기록지명 등 실제 내용으로 연결되는 목록 항목\n"
        "제외: 그룹/카테고리 헤더, '보험종별' 같은 필터·드롭다운, 날짜 입력 필드, 버튼\n\n"
        "각 항목의 클릭 위치를 '전체 이미지' 크기 기준 비율(0.0~1.0)로,\n"
        "텍스트가 시작되는 부분 가까이(행의 세로 중앙)로 지정하세요.\n"
        "위→아래 순서, JSON 배열만 응답:\n"
        '[{"name":"항목이름","x_ratio":0.07,"y_ratio":0.12},...]\n'
        "항목 없으면 [] 반환."
    )
    client = anthropic.Anthropic(api_key=api_key)
    text = _vision_call(client, model, screenshot, prompt, max_tokens=1200)
    try:
        items = _parse_json(text)
        return [x for x in items
                if "name" in x and "x_ratio" in x and "y_ratio" in x]
    except Exception:
        return []


def _items_full_to_abs(items: list[dict], rect: tuple,
                       w: int, h: int) -> list[dict]:
    """
    전체 이미지 비율 좌표 → 화면 절대 좌표 (제외 항목 필터).

    Vision은 물리 픽셀 이미지를 보므로 비율은 맞지만,
    클릭 좌표는 GetWindowRect 기준 논리 픽셀이어야 한다.
    rect(논리) 기준 창 크기를 사용해 DPI 스케일 오차를 제거.
    """
    log_w = rect[2] - rect[0]   # 논리 픽셀 폭 (DPI 무관하게 정확)
    log_h = rect[3] - rect[1]   # 논리 픽셀 높이
    result = []
    seen = set()
    for item in items:
        name = item["name"]
        if is_excluded(name) or name in seen:
            continue
        seen.add(name)
        result.append({
            "name":  name,
            "abs_x": rect[0] + int(item["x_ratio"] * log_w),
            "abs_y": rect[1] + int(item["y_ratio"] * log_h),
        })
    return result


def discover_items_fast(hwnd: int, api_key: str, model: str,
                        status_cb=None) -> list[dict]:
    """
    메뉴 항목 발견 — 정확도 우선 순서로 시도:
      1) Windows UI Automation  : 항목 이름·좌표를 OS에서 직접 읽음 (가장 정확)
      2) Vision (폴백)           : UIA가 지원되지 않을 때 Claude Vision으로 추정
    반환: [{"name":..., "abs_x":..., "abs_y":...}, ...]
    """
    def _status(msg):
        if status_cb:
            status_cb(msg)

    # ── 1순위: Windows UI Automation ─────────────────────────────────────────
    _status("UI Automation으로 메뉴 탐색 중...")
    try:
        import uia_navigate
        uia_items = uia_navigate.find_menu_items_uia(hwnd)
        if len(uia_items) >= 2:
            _status(f"UI Automation: 항목 {len(uia_items)}개 발견 (좌표 정확)")
            return uia_items
        if uia_items:
            _status(f"UI Automation: 항목 {len(uia_items)}개만 발견 — Vision으로 보완")
    except Exception:
        pass

    # ── 2순위: Vision (폴백) ──────────────────────────────────────────────────
    _status("Vision으로 화면 분석 중...")
    screenshot = capture_screen(hwnd)
    rect = win32gui.GetWindowRect(hwnd)
    w, h = screenshot.size

    items = find_left_menu_items_full(screenshot, api_key, model)
    result = _items_full_to_abs(items, rect, w, h)

    # 목록이 거의 비어 있으면 메뉴가 접혀 있을 가능성 — 후보 1개 펼치고 재시도
    if len(result) < 2:
        _status("메뉴가 접혀 있음 — 카테고리 펼치는 중...")
        log_w = rect[2] - rect[0]
        log_h = rect[3] - rect[1]
        cx = rect[0] + int(log_w * 0.07)
        cy = rect[1] + int(log_h * 0.12)
        activate_window(hwnd)
        _safe_click(cx, cy)
        time.sleep(_WAIT_AFTER_CLICK)

        screenshot = capture_screen(hwnd)
        items = find_left_menu_items_full(screenshot, api_key, model)
        result = _items_full_to_abs(items, rect, w, h)

    _status(f"Vision: 항목 {len(result)}개 발견")
    return result


# ── (구) 계층형 탐색 (느림 — 폴백용) ─────────────────────────────────────────

def discover_items_hierarchical(
    hwnd: int,
    api_key: str,
    model: str,
    status_cb=None,
) -> list[dict]:
    """
    왼쪽 메뉴의 최상위 카테고리를 하나씩 클릭해 펼친 뒤
    그 아래 나타나는 서브메뉴 항목을 수집.
    반환: [{"name":..., "abs_x":..., "abs_y":...}, ...]
    """
    def _status(msg):
        if status_cb:
            status_cb(msg)

    _status("EMR 화면 캡처 중...")
    screenshot = capture_screen(hwnd)
    hwnd_rect = win32gui.GetWindowRect(hwnd)
    w, h = screenshot.size

    _status("왼쪽 메뉴 패널 위치 파악 중...")
    bounds = find_left_panel_bounds(screenshot, api_key, model)
    if bounds:
        px0 = int(bounds["x0"] * w)
        py0 = int(bounds["y0"] * h)
        px1 = int(bounds["x1"] * w)
        py1 = int(bounds["y1"] * h)
    else:
        px0, py0 = 0, 0
        px1, py1 = int(w * 0.20), h

    pw = px1 - px0
    ph = py1 - py0
    ox = hwnd_rect[0] + px0
    oy = hwnd_rect[1] + py0

    panel_img = screenshot.crop((px0, py0, px1, py1))

    _status("최상위 메뉴 카테고리 탐색 중...")
    top_items = find_top_level_menu_items(panel_img, api_key, model)

    # 최상위 항목이 없으면 서브메뉴 직접 추출 (폴백)
    if not top_items:
        _status("카테고리 미발견 — 서브메뉴 직접 추출 중...")
        return _panel_to_abs(
            find_submenu_items(panel_img, api_key, model),
            ox, oy, pw, ph)

    all_items: list[dict] = []
    seen: set[str] = set()

    for i, top in enumerate(top_items):
        name = top["name"]
        if is_excluded(name):
            continue

        abs_x = ox + int(top["x_ratio"] * pw)
        abs_y = oy + int(top["y_ratio"] * ph)

        _status(f"[{i+1}/{len(top_items)}] '{name}' 클릭 → 서브메뉴 탐색...")
        activate_window(hwnd)
        _safe_click(abs_x, abs_y)
        time.sleep(_WAIT_AFTER_CLICK)

        # 클릭 후 새 스크린샷 → 패널 재크롭 → 서브메뉴 추출
        new_shot = capture_screen(hwnd)
        new_panel = new_shot.crop((px0, py0, px1, py1))
        sub_items = find_submenu_items(new_panel, api_key, model)

        for sub in sub_items:
            sub_name = sub["name"]
            if is_excluded(sub_name) or sub_name in seen:
                continue
            seen.add(sub_name)
            all_items.append({
                "name":  sub_name,
                "abs_x": ox + int(sub["x_ratio"] * pw),
                "abs_y": oy + int(sub["y_ratio"] * ph),
            })

    return all_items


def _panel_to_abs(items: list[dict], ox: int, oy: int,
                  pw: int, ph: int) -> list[dict]:
    """패널 비율 좌표를 절대 좌표로 변환."""
    result = []
    for item in items:
        name = item["name"]
        if is_excluded(name):
            continue
        result.append({
            "name":  name,
            "abs_x": ox + int(item["x_ratio"] * pw),
            "abs_y": oy + int(item["y_ratio"] * ph),
        })
    return result


# ── (구) 단순 탐색: 서브메뉴 항목 한 번에 찾기 ───────────────────────────────

def find_all_menu_items_by_vision(screenshot: Image.Image,
                                  api_key: str, model: str,
                                  hwnd_rect: tuple | None = None,
                                  ) -> list[dict]:
    """
    두 단계 Vision으로 서브메뉴 항목 + 절대 클릭 좌표 반환.
    반환: [{"name":..., "abs_x":..., "abs_y":...}, ...]
    """
    w, h = screenshot.size

    # 1단계: 노란 패널 위치
    bounds = find_left_panel_bounds(screenshot, api_key, model)

    if bounds:
        px0 = int(bounds["x0"] * w)
        py0 = int(bounds["y0"] * h)
        px1 = int(bounds["x1"] * w)
        py1 = int(bounds["y1"] * h)
        panel_img = screenshot.crop((px0, py0, px1, py1))
    else:
        # 노란 패널 못 찾으면 왼쪽 20% 사용
        px0, py0 = 0, 0
        px1, py1 = int(w * 0.20), h
        panel_img = screenshot.crop((px0, py0, px1, py1))

    # 2단계: 패널 내 서브메뉴
    items = find_submenu_items(panel_img, api_key, model)

    # 패널 좌표 → 창/스크린 절대 좌표
    pw = px1 - px0
    ph = py1 - py0
    ox = (hwnd_rect[0] if hwnd_rect else 0) + px0
    oy = (hwnd_rect[1] if hwnd_rect else 0) + py0

    result = []
    for item in items:
        name = item["name"]
        if is_excluded(name):
            continue   # 제외 항목 건너뜀
        result.append({
            "name":  name,
            "abs_x": ox + int(item["x_ratio"] * pw),
            "abs_y": oy + int(item["y_ratio"] * ph),
        })
    return result


# ── 수집: 항목 클릭 → 콘텐츠 캡처 ───────────────────────────────────────────

def _content_region(img: Image.Image) -> Image.Image:
    """왼쪽 메뉴 패널(~22%)을 뺀 오른쪽 콘텐츠 영역만 잘라낸다."""
    w, h = img.size
    return img.crop((int(w * 0.22), 0, w, h))


# 괄호 숫자 없어도 항상 전체 페이지 수집을 강제하는 문서 키워드
ALWAYS_PAGINATE = ["간호기록지", "응급실 진료기록지", "응급실진료기록지", "응급실기록지"]

# 절대 클릭하면 안 되는 위험 버튼 — 페이지 이동(이전/다음)과 혼동 금지
DANGER_BUTTON_KEYWORDS = [
    "저장", "삭제", "복사", "새문서", "새 문서", "신규문서", "신규 문서",
    "수정", "등록", "확인", "닫기", "출력", "인쇄",
    "전송", "발송", "승인", "취소", "신규", "추가", "잠금",
    "save", "delete", "remove", "copy", "new", "submit",
    "print", "confirm", "close",
]


def is_danger_button(name: str) -> bool:
    """이름에 위험 버튼 키워드가 포함되면 True (이전/다음과 절대 혼동 금지)."""
    nl = (name or "").lower().strip()
    return any(kw.lower() in nl for kw in DANGER_BUTTON_KEYWORDS)


def _parse_item_count(raw_name: str) -> tuple[str, int]:
    """
    왼쪽 메뉴 항목 이름에서 괄호 안 장수를 추출한다.
    '간호기록지(15)'  → ('간호기록지', 15)
    '응급실 진료기록지(3)' → ('응급실 진료기록지', 3)
    '간호초기평가기록'      → ('간호초기평가기록', 1)
    """
    m = re.search(r'\((\d+)\)\s*$', raw_name.strip())
    if m:
        count = int(m.group(1))
        base  = raw_name[:m.start()].strip()
        return base, count
    return raw_name.strip(), 1


def _images_differ(img1: Image.Image, img2: Image.Image,
                   threshold: float = 0.01) -> bool:
    """두 이미지가 threshold 이상 다르면 True (콘텐츠 변경 감지)"""
    try:
        a = img1.resize((64, 64)).tobytes()
        b = img2.resize((64, 64)).tobytes()
        diff = sum(abs(x - y) for x, y in zip(a, b))
        return diff / (len(a) * 255) > threshold
    except Exception:
        return True


def _find_nav_buttons_abs(hwnd: int, rect: tuple,
                          img: Image.Image,
                          api_key: str, model: str) -> dict:
    """
    이전/다음 버튼의 절대 화면 좌표 + 툴바 페이지 표시(1/N)를 찾는다.
    반환: {"prev_abs_x":..., "prev_abs_y":...,
           "next_abs_x":..., "next_abs_y":...,
           "page_current":1, "page_total":15}   (찾은 것만 포함)

    탐색 순서:
      1) UIA (Windows 접근성 API) — 버튼 좌표
      2) Vision — 전체 폭 × 상단 30% 크롭 (버튼 좌표 + 1/N 페이지 표시)
    """
    result: dict = {}

    # ── 1단계: UIA — 버튼 좌표 ───────────────────────────────────────────
    try:
        import uia_navigate
        uia_nav = uia_navigate.find_nav_buttons_uia(hwnd)
        if uia_nav:
            result.update(uia_nav)
    except Exception:
        pass

    # ── 2단계: Vision — 버튼 좌표(없을 때) + 툴바 1/N 페이지 표시 ─────────
    # 버튼 좌표를 UIA로 이미 찾았더라도, 툴바의 1/N 숫자를 읽기 위해 Vision 호출.
    if not api_key:
        return result

    try:
        w, h = img.size
        HEADER_H_RATIO = 0.30   # 더 넓게 잡아 툴바 놓치지 않도록
        header = img.crop((0, 0, w, int(h * HEADER_H_RATIO)))

        prompt = (
            "이 이미지는 EMR 프로그램 화면의 상단(툴바·헤더) 부분입니다.\n\n"
            "1) 문서 페이지를 이동하는 버튼만 찾아주세요:\n"
            "   • '이전' 또는 '◀' 또는 '<' — 이전(왼쪽) 페이지 버튼\n"
            "   • '다음' 또는 '▶' 또는 '>' — 다음(오른쪽) 페이지 버튼\n"
            "2) 툴바에 'N/M' 또는 'N / M' 형태의 페이지 표시가 있으면\n"
            "   현재 페이지(N)와 전체 페이지(M)를 읽어주세요. 예: '1/15' → cur=1,total=15\n\n"
            "주의: '저장','삭제','복사','새문서','수정','등록','출력','인쇄','확인','닫기'\n"
            "같은 버튼은 페이지 이동 버튼이 아니므로 prev/next 로 절대 반환하지 마세요.\n"
            "이 버튼들의 위치는 danger 배열에 따로 담아주세요(혼동 방지용).\n\n"
            "이 이미지 전체 크기 기준 비율(0.0~1.0)로 반환:\n"
            '{"prev_x":0.3,"prev_y":0.5,"next_x":0.4,"next_y":0.5,'
            '"page_current":1,"page_total":15,'
            '"danger":[{"x":0.8,"y":0.5},{"x":0.9,"y":0.5}]}\n'
            "이동 버튼·페이지 표시를 모두 못 찾으면: null\n"
            "(일부만 있으면 있는 항목만 채워서 반환)"
        )
        client = anthropic.Anthropic(api_key=api_key)
        text = _vision_call(client, model, header, prompt, max_tokens=300)

        if "null" not in text.lower():
            data = _parse_json(text)
            if isinstance(data, dict):
                log_w = rect[2] - rect[0]
                log_h = rect[3] - rect[1]

                def _abs(rx, ry):
                    return (rect[0] + int(rx * log_w),
                            rect[1] + int(ry * HEADER_H_RATIO * log_h))

                # 툴바 1/N 페이지 표시 읽기
                try:
                    pt = int(data.get("page_total", 0))
                    if pt > 1:
                        result["page_total"] = pt
                    pc = int(data.get("page_current", 0))
                    if pc >= 1:
                        result["page_current"] = pc
                except Exception:
                    pass

                # 위험 버튼 절대 좌표 목록
                danger_pts: list[tuple[int, int]] = []
                for d in (data.get("danger") or []):
                    try:
                        danger_pts.append(_abs(d["x"], d["y"]))
                    except Exception:
                        pass

                # 이동 버튼이 위험 버튼과 너무 가까우면 오인식으로 보고 버림
                near_thresh = max(int(log_w * 0.03), 25)   # px

                def _too_close(px, py):
                    return any(abs(px - dx) <= near_thresh and
                               abs(py - dy) <= near_thresh
                               for dx, dy in danger_pts)

                # UIA가 좌표를 못 줬을 때만 Vision 좌표 사용
                if "prev_abs_x" not in result and \
                        "prev_x" in data and "prev_y" in data:
                    px, py = _abs(data["prev_x"], data["prev_y"])
                    if not _too_close(px, py):
                        result["prev_abs_x"], result["prev_abs_y"] = px, py
                if "next_abs_x" not in result and \
                        "next_x" in data and "next_y" in data:
                    nx, ny = _abs(data["next_x"], data["next_y"])
                    if not _too_close(nx, ny):
                        result["next_abs_x"], result["next_abs_y"] = nx, ny
    except Exception:
        pass

    return result


def _collect_pages(
    hwnd: int,
    rect: tuple,
    base_name: str,
    page_count: int,
    first_img: Image.Image,
    api_key: str,
    model: str,
    menu_idx: int,
    menu_total: int,
    status_cb=None,
    item_cb=None,
) -> list[tuple[str, Image.Image]]:
    """
    전체 페이지를 이전(◀)/다음(▶) 버튼으로 수집한다.

    페이지 수 결정 우선순위:
      1) 툴바의 'N/M' 표시 분모 M  (가장 신뢰도 높음 — 화면이 실제로 알려줌)
      2) 메뉴 이름 괄호 숫자        (예: '간호기록지(15)' → 15)
      3) ALWAYS_PAGINATE 키워드     (숫자 없어도 바뀌는 동안 계속)

    수집 방식:
      - 다음 버튼이 있으면: 이전으로 1페이지까지 되돌린 뒤 다음으로 M장 순서대로 수집
      - 다음 버튼이 없으면: 이전으로 바뀌는 동안 수집 후 순서 뒤집기
    저장/삭제/복사/새문서 버튼은 _find_nav_buttons_abs 에서 이미 배제됨.
    """
    force = any(kw in base_name for kw in ALWAYS_PAGINATE)

    # ── 버튼 좌표 + 툴바 N/M 페이지 표시 탐색 ──
    nav = _find_nav_buttons_abs(hwnd, rect, first_img, api_key, model)
    prev_x: int | None = nav.get("prev_abs_x")
    prev_y: int | None = nav.get("prev_abs_y")
    next_x: int | None = nav.get("next_abs_x")
    next_y: int | None = nav.get("next_abs_y")
    page_total = nav.get("page_total")   # 툴바 분모 M

    # 최종 목표 장수: 툴바 분모 > 메뉴 괄호 숫자
    target = page_count
    if isinstance(page_total, int) and page_total > 1:
        target = max(target, page_total)

    # 수집할 게 없으면 단일 페이지
    if target <= 1 and not force:
        if item_cb:
            item_cb(menu_idx, menu_total, base_name, first_img)
        return [(base_name, first_img)]

    HARD_CAP = max(target if target > 1 else 60, 60)   # 안전 상한

    def _press_key(vk: int):
        """Page Up(0x21)/Page Down(0x22) — 저장/삭제를 트리거하지 않는 안전 이동."""
        try:
            pyautogui.press("pageup" if vk == 0x21 else "pagedown")
        except Exception:
            ctypes.windll.user32.keybd_event(vk, 0, 0, 0)
            time.sleep(0.05)
            ctypes.windll.user32.keybd_event(vk, 0, 0x0002, 0)

    def _go_prev():
        activate_window(hwnd)
        if prev_x is not None and prev_y is not None:
            _safe_click(prev_x, prev_y)
        else:
            _press_key(0x21)   # Page Up
        time.sleep(_WAIT_AFTER_CLICK)

    def _go_next():
        activate_window(hwnd)
        if next_x is not None and next_y is not None:
            _safe_click(next_x, next_y)
        else:
            _press_key(0x22)   # Page Down
        time.sleep(_WAIT_AFTER_CLICK)

    has_next = (next_x is not None) or (prev_x is None)   # 다음 버튼 or 키보드 가능

    if status_cb:
        tgt_str = f"{target}장" if target > 1 else "전체"
        via = "이전/다음 버튼" if prev_x is not None else "Page Up/Down 키"
        status_cb(f"  [{menu_idx}/{menu_total}] {base_name} "
                  f"— {tgt_str} 수집 ({via})")

    pages: list[tuple[str, Image.Image]] = []

    if has_next:
        # ── 1) 이전으로 첫 페이지까지 되돌리기 ──
        cur = _content_region(first_img)
        for _ in range(HARD_CAP):
            _go_prev()
            cap = capture_screen(hwnd)
            cc  = _content_region(cap)
            if not _images_differ(cur, cc):
                break   # 더 못 감 = 첫 페이지 도달
            cur = cc

        # ── 2) 첫 페이지 캡처 후 다음으로 순서대로 수집 ──
        first_page = capture_screen(hwnd)
        pages.append(("_pg_", first_page))
        prev_content = _content_region(first_page)

        while len(pages) < HARD_CAP:
            if status_cb:
                status_cb(f"  [{menu_idx}/{menu_total}] {base_name} "
                          f"— 다음 이동 {len(pages)}/{target}...")
            _go_next()
            cap = capture_screen(hwnd)
            cc  = _content_region(cap)
            if not _images_differ(prev_content, cc):
                break   # 마지막 페이지
            pages.append(("_pg_", cap))
            prev_content = cc
            if target > 1 and len(pages) >= target:
                break
        # 이미 오래된→최신 순 → 뒤집지 않음
    else:
        # ── 다음 버튼 없음: 이전으로만 수집 후 순서 뒤집기 ──
        pages.append(("_latest_", first_img))
        prev_content = _content_region(first_img)
        while len(pages) < HARD_CAP:
            if status_cb:
                status_cb(f"  [{menu_idx}/{menu_total}] {base_name} "
                          f"— 이전 이동 {len(pages)}/{target}...")
            _go_prev()
            cap = capture_screen(hwnd)
            cc  = _content_region(cap)
            if not _images_differ(prev_content, cc):
                break
            pages.append(("_pg_", cap))
            prev_content = cc
            if target > 1 and len(pages) >= target:
                break
        pages.reverse()   # 오래된→최신 순으로

    if len(pages) <= 1 and status_cb:
        status_cb(f"  [{menu_idx}/{menu_total}] {base_name} "
                  "— 페이지 이동 안 됨, 1페이지만 수집")

    total   = len(pages)
    labeled = [(f"{base_name} ({i+1}/{total})", im)
               for i, (_, im) in enumerate(pages)]

    if item_cb:
        for lbl, im in labeled:
            item_cb(menu_idx, menu_total, lbl, im)

    return labeled


def collect_menu_items(
    hwnd: int,
    items: list[dict],
    api_key: str = "",
    model: str = "",
    status_cb=None,
    item_cb=None,
) -> list[tuple[str, Image.Image]]:
    """
    메뉴 항목을 순서대로 클릭하며 오른쪽 콘텐츠를 캡처.
    각 항목에서 이전(◀) 버튼을 클릭해 페이지가 바뀌는 동안 전 페이지 수집.
    간호기록지·진료기록지 등 PAGINATED_KEYWORDS 항목은 항상 전체 수집 시도.
    """
    menu_total = len(items)
    results: list[tuple[str, Image.Image]] = []
    rect = win32gui.GetWindowRect(hwnd)

    prev_img = capture_screen(hwnd)   # 클릭 전 기준 화면

    for i, item in enumerate(items):
        name  = item["name"]
        abs_x = item["abs_x"]
        abs_y    = item["abs_y"]
        menu_idx = i + 1

        # 메뉴 이름에서 장수 파싱: '간호기록지(15)' → base='간호기록지', count=15
        base_name, page_count = _parse_item_count(name)

        if status_cb:
            cnt_str = f" ({page_count}장)" if page_count > 1 else ""
            status_cb(f"[{menu_idx}/{menu_total}] {base_name}{cnt_str} 여는 중...")

        # 창 활성화 → 단일 클릭
        activate_window(hwnd)
        _safe_click(abs_x, abs_y)
        time.sleep(_WAIT_AFTER_CLICK)
        img = capture_screen(hwnd)

        # 오른쪽 콘텐츠 영역이 안 바뀌면 더블클릭+Enter 재시도
        changed = _images_differ(_content_region(prev_img), _content_region(img))
        if not changed:
            if status_cb:
                status_cb(f"[{menu_idx}/{menu_total}] {base_name} — 재시도...")
            activate_window(hwnd)
            _safe_click(abs_x, abs_y, double=True)
            _send_key(0x0D)
            time.sleep(_WAIT_AFTER_CLICK)
            img = capture_screen(hwnd)

        # 전체 페이지 수집 (page_count > 1 또는 ALWAYS_PAGINATE 키워드면 이전 반복)
        pages = _collect_pages(
            hwnd, rect, base_name, page_count, img,
            api_key, model,
            menu_idx, menu_total,
            status_cb=status_cb,
            item_cb=item_cb,
        )
        results.extend(pages)
        prev_img = img

    return results


# 하위 호환
def collect_all_tabs(hwnd, tabs, status_cb=None):
    items = [{"name": t.get("name",""), "abs_x": 0, "abs_y": 0} for t in tabs]
    return collect_menu_items(hwnd, items, status_cb)
