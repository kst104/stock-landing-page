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
_WAIT_AFTER_CLICK = 2.5


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

_MOUSEEVENTF_MOVE     = 0x0001
_MOUSEEVENTF_LDOWN    = 0x0002
_MOUSEEVENTF_LUP      = 0x0004
_MOUSEEVENTF_ABSOLUTE = 0x8000
_INPUT_MOUSE          = 0


def _send_input_click(x: int, y: int, double: bool = False):
    """
    ctypes.SendInput으로 마우스 클릭.
    pyautogui가 UAC/권한 문제로 막힐 때 사용.
    """
    sw = ctypes.windll.user32.GetSystemMetrics(0)
    sh = ctypes.windll.user32.GetSystemMetrics(1)
    nx = int(x * 65535 / max(sw, 1))
    ny = int(y * 65535 / max(sh, 1))

    def _send(flags):
        inp = _INPUT()
        inp.type    = _INPUT_MOUSE
        inp.mi.dx   = nx
        inp.mi.dy   = ny
        inp.mi.dwFlags = flags
        ctypes.windll.user32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(inp))

    _send(_MOUSEEVENTF_MOVE | _MOUSEEVENTF_ABSOLUTE)
    time.sleep(0.05)
    _send(_MOUSEEVENTF_LDOWN | _MOUSEEVENTF_ABSOLUTE)
    time.sleep(0.05)
    _send(_MOUSEEVENTF_LUP | _MOUSEEVENTF_ABSOLUTE)

    if double:
        time.sleep(0.12)
        _send(_MOUSEEVENTF_LDOWN | _MOUSEEVENTF_ABSOLUTE)
        time.sleep(0.05)
        _send(_MOUSEEVENTF_LUP | _MOUSEEVENTF_ABSOLUTE)


def _safe_click(x: int, y: int, double: bool = False):
    """
    마우스를 target 위치로 이동 후 클릭.
    1) SetCursorPos로 이동
    2) mouse_event (구형 API, UIPI 우회 가능성 있음)
    3) SendInput 폴백
    클릭 후 Enter 키도 전송 (트리뷰/리스트 선택 항목 활성화).
    """
    sw = ctypes.windll.user32.GetSystemMetrics(0)
    sh = ctypes.windll.user32.GetSystemMetrics(1)
    if not (0 <= x < sw and 0 <= y < sh):
        return

    # 마우스 이동 (시각 확인용)
    ctypes.windll.user32.SetCursorPos(x, y)
    time.sleep(0.4)

    clicked = False

    # 방법 1: pyautogui (관리자 권한 시 정상 동작)
    try:
        pyautogui.click(x, y)
        if double:
            time.sleep(0.12)
            pyautogui.click(x, y)
        clicked = True
    except Exception:
        pass

    if not clicked:
        # 방법 2: mouse_event (구형 API, 일부 환경에서 UIPI 우회)
        try:
            MOUSEEVENTF_LEFTDOWN = 0x0002
            MOUSEEVENTF_LEFTUP   = 0x0004
            ctypes.windll.user32.mouse_event(MOUSEEVENTF_LEFTDOWN, x, y, 0, 0)
            time.sleep(0.05)
            ctypes.windll.user32.mouse_event(MOUSEEVENTF_LEFTUP, x, y, 0, 0)
            if double:
                time.sleep(0.12)
                ctypes.windll.user32.mouse_event(MOUSEEVENTF_LEFTDOWN, x, y, 0, 0)
                time.sleep(0.05)
                ctypes.windll.user32.mouse_event(MOUSEEVENTF_LEFTUP, x, y, 0, 0)
            clicked = True
        except Exception:
            pass

    if not clicked:
        # 방법 3: SendInput
        _send_input_click(x, y, double=double)

    # 클릭 후 Enter 키 — 트리뷰/리스트 항목 활성화
    time.sleep(0.1)
    try:
        pyautogui.press("enter")
    except Exception:
        KEYEVENTF_KEYUP = 0x0002
        VK_RETURN = 0x0D
        ctypes.windll.user32.keybd_event(VK_RETURN, 0, 0, 0)
        time.sleep(0.05)
        ctypes.windll.user32.keybd_event(VK_RETURN, 0, KEYEVENTF_KEYUP, 0)


# ── 창 활성화 + 캡처 (멀티모니터 대응) ───────────────────────────────────────

def activate_window(hwnd: int):
    placement = win32gui.GetWindowPlacement(hwnd)
    if placement[1] == win32con.SW_SHOWMINIMIZED:
        win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
    win32gui.SetForegroundWindow(hwnd)
    time.sleep(0.6)


def capture_screen(hwnd: int) -> Image.Image:
    """
    창을 활성화한 뒤 창 bbox로 캡처.
    bbox 지정 방식으로 보조모니터도 올바르게 캡처.
    """
    activate_window(hwnd)
    time.sleep(0.3)
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


# ── 2단계: 패널 내 서브메뉴 항목 추출 ────────────────────────────────────────

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
        "제외: 그룹/카테고리 제목(폴더처럼 하위 항목을 묶는 헤더)\n\n"
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


# ── 통합: 서브메뉴 항목 찾기 ─────────────────────────────────────────────────

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
        result.append({
            "name":  item["name"],
            "abs_x": ox + int(item["x_ratio"] * pw),
            "abs_y": oy + int(item["y_ratio"] * ph),
        })
    return result


# ── 수집: 항목 클릭 → 콘텐츠 캡처 ───────────────────────────────────────────

def _images_differ(img1: Image.Image, img2: Image.Image,
                   threshold: float = 0.01) -> bool:
    """두 이미지가 threshold 이상 다르면 True (콘텐츠 변경 감지)"""
    import struct
    try:
        a = img1.resize((64, 64)).tobytes()
        b = img2.resize((64, 64)).tobytes()
        diff = sum(abs(x - y) for x, y in zip(a, b))
        return diff / (len(a) * 255) > threshold
    except Exception:
        return True


def collect_menu_items(
    hwnd: int,
    items: list[dict],          # [{"name":..., "abs_x":..., "abs_y":...}]
    status_cb=None,
    item_cb=None,
) -> list[tuple[str, Image.Image]]:
    """
    서브메뉴 항목을 순서대로 클릭하며 콘텐츠 캡처.
    items는 find_all_menu_items_by_vision() 반환값.
    """
    total = len(items)
    results = []

    prev_img = capture_screen(hwnd)   # 클릭 전 기준 화면

    for i, item in enumerate(items):
        name  = item["name"]
        abs_x = item["abs_x"]
        abs_y = item["abs_y"]

        if status_cb:
            status_cb(f"[{i+1}/{total}] {name} 클릭 중...")

        # 클릭 (단일 + 더블 — 트리뷰/리스트 모두 대응)
        activate_window(hwnd)
        time.sleep(0.2)
        _safe_click(abs_x, abs_y)
        time.sleep(0.4)
        _safe_click(abs_x, abs_y, double=True)
        time.sleep(_WAIT_AFTER_CLICK)

        img = capture_screen(hwnd)

        # 화면이 바뀌지 않았으면 재시도
        if not _images_differ(prev_img, img):
            if status_cb:
                status_cb(f"[{i+1}/{total}] {name} — 재시도...")
            activate_window(hwnd)
            _safe_click(abs_x, abs_y)
            time.sleep(_WAIT_AFTER_CLICK)
            img = capture_screen(hwnd)

        prev_img = img
        results.append((name, img))

        if item_cb:
            item_cb(i + 1, total, name, img)

    return results


# 하위 호환
def collect_all_tabs(hwnd, tabs, status_cb=None):
    items = [{"name": t.get("name",""), "abs_x": 0, "abs_y": 0} for t in tabs]
    return collect_menu_items(hwnd, items, status_cb)
