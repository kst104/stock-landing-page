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
    sw = ctypes.windll.user32.GetSystemMetrics(0)
    sh = ctypes.windll.user32.GetSystemMetrics(1)
    if not (0 <= x < sw and 0 <= y < sh):
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


# 항상 전체 페이지 수집을 시도할 문서 키워드 (이름에 포함되면 적용)
PAGINATED_KEYWORDS = [
    "간호기록지", "응급실 진료기록지", "응급실진료기록지",
    "진료기록지", "경과기록지", "수술기록지", "투약기록지",
    "VS기록지", "BST기록지", "I&O기록지",
]


def _is_paginated_doc(name: str) -> bool:
    """이 문서는 항상 이전 버튼으로 전체 페이지를 수집해야 하는 타입인지 확인."""
    return any(kw in name for kw in PAGINATED_KEYWORDS)


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


def _find_prev_button(img: Image.Image, api_key: str, model: str) -> dict | None:
    """
    화면에서 '이전(◀)' 버튼 위치를 찾는다.
    페이지 총 개수를 알 필요 없이 버튼 좌표만 반환.
    반환: {"prev_x":0.65, "prev_y":0.08}  (전체 이미지 비율) 또는 None
    """
    prompt = (
        "이 EMR 화면에서 문서 페이지를 이전으로 이동하는 버튼을 찾아주세요.\n"
        "버튼 텍스트: '이전', '◀', '<', '←', 또는 이전 방향 화살표.\n"
        "페이지 번호(예: 15/15, 3/5)가 있으면 같이 반환하세요.\n\n"
        "반환 형식 (이미지 전체 크기 기준 비율 0.0~1.0):\n"
        '{"prev_x":0.65,"prev_y":0.08,"current":15,"total":15}\n'
        "'이전' 버튼이 전혀 없으면: null"
    )
    client = anthropic.Anthropic(api_key=api_key)
    text = _vision_call(client, model, img, prompt, max_tokens=120)
    try:
        if "null" in text.lower():
            return None
        data = _parse_json(text)
        if isinstance(data, dict) and "prev_x" in data and "prev_y" in data:
            return data
    except Exception:
        pass
    return None


def _collect_pages(
    hwnd: int,
    rect: tuple,
    name: str,
    first_img: Image.Image,
    api_key: str,
    model: str,
    menu_idx: int,
    menu_total: int,
    status_cb=None,
    item_cb=None,
) -> list[tuple[str, Image.Image]]:
    """
    '이전(◀)' 버튼을 반복 클릭해 모든 페이지 수집.
    페이지가 바뀌지 않을 때까지 계속 → 총 페이지 수를 몰라도 동작.
    간호기록지·진료기록지 등 다중 페이지 문서는 항상 전체 수집 시도.
    """
    if not api_key:
        if item_cb:
            item_cb(menu_idx, menu_total, name, first_img)
        return [(name, first_img)]

    # 이전 버튼 위치 찾기
    nav = _find_prev_button(first_img, api_key, model)

    # 이전 버튼을 못 찾았고, 다중 페이지 문서 키워드도 아니면 단일 페이지로 처리
    if not nav and not _is_paginated_doc(name):
        if item_cb:
            item_cb(menu_idx, menu_total, name, first_img)
        return [(name, first_img)]

    log_w = rect[2] - rect[0]
    log_h = rect[3] - rect[1]

    if nav:
        prev_x = rect[0] + int(nav["prev_x"] * log_w)
        prev_y = rect[1] + int(nav["prev_y"] * log_h)
        total_hint = int(nav.get("total", 0))   # Vision이 알려준 총 페이지(참고용)
        current    = int(nav.get("current", total_hint or 1))
    else:
        # 버튼 위치 미확인 — 이전 버튼이 보통 있는 위치(상단 중앙~우측)를 추정
        prev_x = rect[0] + int(log_w * 0.60)
        prev_y = rect[1] + int(log_h * 0.08)
        total_hint = 0
        current    = 1

    label0 = f"{name} p{current}" if total_hint else f"{name} (최신)"
    pages: list[tuple[str, Image.Image]] = [(label0, first_img)]
    if item_cb:
        item_cb(menu_idx, menu_total, label0, first_img)

    prev_content = _content_region(first_img)
    pg_offset = 1
    MAX_PAGES = 60   # 안전 상한

    while pg_offset <= MAX_PAGES:
        if status_cb:
            hint = f"/{total_hint}" if total_hint else ""
            status_cb(f"  [{menu_idx}/{menu_total}] {name} — 이전 {pg_offset}번째{hint}...")

        activate_window(hwnd)
        _safe_click(prev_x, prev_y)
        time.sleep(_WAIT_AFTER_CLICK)

        img = capture_screen(hwnd)
        content = _content_region(img)

        # 화면이 바뀌지 않으면 더 이상 이전 페이지 없음
        if not _images_differ(prev_content, content):
            break

        pg_num = current - pg_offset if total_hint else pg_offset
        lbl = (f"{name} p{pg_num}" if total_hint
               else f"{name} (이전{pg_offset})")
        pages.append((lbl, img))
        if item_cb:
            item_cb(menu_idx, menu_total, lbl, img)

        prev_content = content
        pg_offset += 1

    # 오래된→최신 순으로 정렬 후 1/N, 2/N ... 으로 재레이블
    pages.reverse()
    total = len(pages)
    if total > 1:
        pages = [(f"{name} ({i+1}/{total})", img)
                 for i, (_, img) in enumerate(pages)]

    return pages


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
        abs_y = item["abs_y"]
        menu_idx = i + 1

        if status_cb:
            status_cb(f"[{menu_idx}/{menu_total}] {name} 여는 중...")

        # 창 활성화 → 단일 클릭
        activate_window(hwnd)
        _safe_click(abs_x, abs_y)
        time.sleep(_WAIT_AFTER_CLICK)
        img = capture_screen(hwnd)

        # 오른쪽 콘텐츠 영역이 안 바뀌면 더블클릭+Enter 재시도
        changed = _images_differ(_content_region(prev_img), _content_region(img))
        if not changed:
            if status_cb:
                status_cb(f"[{menu_idx}/{menu_total}] {name} — 재시도...")
            activate_window(hwnd)
            _safe_click(abs_x, abs_y, double=True)
            _send_key(0x0D)
            time.sleep(_WAIT_AFTER_CLICK)
            img = capture_screen(hwnd)
            changed = _images_differ(_content_region(prev_img), _content_region(img))

        label = name if changed else f"{name} (확인필요)"

        # 페이지네이션 확인 및 전 페이지 수집
        pages = _collect_pages(
            hwnd, rect, label, img,
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
