"""
NGTMediPlus 창 자동 탐색 및 화면 수집 (pywin32 기반)

탭 탐색 우선순위:
  1. Windows 자식 컨트롤 열거 (가장 정확 — Delphi/WinForms 앱에 효과적)
  2. Claude Vision 분석 (폴백)
  3. 두 방법 모두 실패 시 → 현재 화면 그대로 반환
"""

from __future__ import annotations

import base64
import io
import json
import time

import anthropic
import pyautogui
import win32con
import win32gui
from PIL import Image, ImageGrab

_NGT_KEYWORDS = ["neomed", "ngt", "전자챠트", "전자차트", "emr", "차트", "medit"]
_WAIT_AFTER_CLICK = 2.0


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
    for hwnd, title in _enum_windows():
        if any(kw in title.lower() for kw in _NGT_KEYWORDS):
            return hwnd, title
    return None


def get_all_windows() -> list[str]:
    return [title for _, title in _enum_windows()]


def get_hwnd_by_title(title: str) -> int | None:
    for hwnd, t in _enum_windows():
        if t == title:
            return hwnd
    return None


# ── 창 제어 + 캡처 ────────────────────────────────────────────────────────────

def activate_window(hwnd: int):
    placement = win32gui.GetWindowPlacement(hwnd)
    if placement[1] == win32con.SW_SHOWMINIMIZED:
        win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
    win32gui.SetForegroundWindow(hwnd)
    time.sleep(0.5)


def capture_window(hwnd: int) -> Image.Image:
    rect = win32gui.GetWindowRect(hwnd)
    return ImageGrab.grab(bbox=rect)


# ── 방법 1: Windows 자식 컨트롤 열거 ─────────────────────────────────────────

# Delphi / WinForms / 일반 Win32 탭·버튼 클래스명
_TAB_CLASSES = {
    "TPageControl", "TTabSheet", "TTabControl",
    "SysTabControl32", "TabControl",
    "TButton", "TBitBtn", "TSpeedButton", "Button",
    "TPanel",
}


def find_tabs_by_enum(hwnd: int) -> list[dict]:
    """
    Windows 자식 컨트롤 직접 열거로 탭/버튼 위치 파악.
    반환: [{"name": ..., "x_ratio": ..., "y_ratio": ...}, ...]
    """
    parent_rect = win32gui.GetWindowRect(hwnd)
    px, py = parent_rect[0], parent_rect[1]
    pw = parent_rect[2] - px
    ph = parent_rect[3] - py
    if pw == 0 or ph == 0:
        return []

    controls: list[dict] = []

    def cb(child, _):
        try:
            if not win32gui.IsWindowVisible(child):
                return True
            cls  = win32gui.GetClassName(child)
            text = win32gui.GetWindowText(child).strip()
            if cls in _TAB_CLASSES and text:
                r = win32gui.GetWindowRect(child)
                w, h = r[2] - r[0], r[3] - r[1]
                if 15 < w < 400 and 10 < h < 80:
                    cx = r[0] + w // 2
                    cy = r[1] + h // 2
                    controls.append({
                        "name":    text,
                        "x_ratio": (cx - px) / pw,
                        "y_ratio": (cy - py) / ph,
                    })
        except Exception:
            pass
        return True

    try:
        win32gui.EnumChildWindows(hwnd, cb, None)
    except Exception:
        pass

    # 중복 제거 (같은 이름)
    seen, unique = set(), []
    for c in controls:
        if c["name"] not in seen:
            seen.add(c["name"])
            unique.append(c)
    return unique


# ── 방법 2: Claude Vision 분석 ────────────────────────────────────────────────

def find_tabs_by_vision(screenshot: Image.Image,
                        api_key: str, model: str) -> list[dict]:
    """
    Claude Vision으로 탭/메뉴 버튼 위치 파악.
    반환: [{"name": ..., "x_ratio": ..., "y_ratio": ...}, ...]
    """
    buf = io.BytesIO()
    screenshot.save(buf, format="PNG")
    b64 = base64.standard_b64encode(buf.getvalue()).decode()

    prompt = (
        "이 EMR 프로그램 화면에서 클릭할 수 있는 탭, 메뉴 버튼, 사이드바 항목을 모두 찾아주세요.\n"
        "각 항목의 이름과 위치를 이미지 너비/높이 대비 비율(0.0~1.0)로 반환하세요.\n"
        "JSON 배열만 응답 (다른 텍스트 없이):\n"
        '[{"name":"항목이름","x_ratio":0.1,"y_ratio":0.05},...]\n\n'
        "탭이나 버튼이 없으면 [] 반환."
    )

    client = anthropic.Anthropic(api_key=api_key)
    resp = client.messages.create(
        model=model,
        max_tokens=1000,
        messages=[{"role": "user", "content": [
            {"type": "image", "source": {
                "type": "base64", "media_type": "image/png", "data": b64}},
            {"type": "text", "text": prompt},
        ]}],
    )

    text = resp.content[0].text.strip()
    try:
        if "```" in text:
            start = text.find("[")
            end   = text.rfind("]") + 1
            text  = text[start:end]
        tabs = json.loads(text)
        return [t for t in tabs
                if "name" in t and "x_ratio" in t and "y_ratio" in t]
    except Exception:
        return []


# ── 통합: 탭 탐색 (열거 → Vision → 폴백) ─────────────────────────────────────

def identify_tabs(hwnd: int, screenshot: Image.Image,
                  api_key: str, model: str) -> tuple[list[dict], str]:
    """
    탭 탐색 통합 함수.
    반환: (tabs, method)
      method: "enum" | "vision" | "fallback"
    """
    # 1. Windows 컨트롤 열거
    tabs = find_tabs_by_enum(hwnd)
    if tabs:
        return tabs, "enum"

    # 2. Claude Vision
    tabs = find_tabs_by_vision(screenshot, api_key, model)
    if tabs:
        return tabs, "vision"

    # 3. 폴백: 현재 화면 그대로
    return [], "fallback"


# ── 탭 자동 순회 ──────────────────────────────────────────────────────────────

def collect_all_tabs(
    hwnd: int,
    tabs: list[dict],
    status_cb=None,
) -> list[tuple[str, Image.Image]]:
    """탭 목록을 순서대로 클릭하며 화면 수집"""
    rect = win32gui.GetWindowRect(hwnd)
    ox, oy = rect[0], rect[1]
    w = rect[2] - ox
    h = rect[3] - oy

    results = []
    for i, tab in enumerate(tabs):
        name  = tab.get("name", f"탭{i+1}")
        abs_x = int(ox + tab["x_ratio"] * w)
        abs_y = int(oy + tab["y_ratio"] * h)

        if status_cb:
            status_cb(f"[{i+1}/{len(tabs)}] {name} 이동 중...")

        activate_window(hwnd)
        pyautogui.click(abs_x, abs_y)
        time.sleep(_WAIT_AFTER_CLICK)

        img = capture_window(hwnd)
        results.append((name, img))

    return results
