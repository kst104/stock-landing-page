"""
NGTMediPlus 창 자동 탐색 및 화면 수집 (pywin32 기반)
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
    """보이는 모든 창 열거 → [(hwnd, title), ...]"""
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
    """NGTMediPlus 창 자동 탐색. (hwnd, title) 또는 None."""
    for hwnd, title in _enum_windows():
        if any(kw in title.lower() for kw in _NGT_KEYWORDS):
            return hwnd, title
    return None


def get_all_windows() -> list[str]:
    """현재 열린 모든 창 제목 목록"""
    return [title for _, title in _enum_windows()]


def get_hwnd_by_title(title: str) -> int | None:
    """제목으로 hwnd 조회"""
    for hwnd, t in _enum_windows():
        if t == title:
            return hwnd
    return None


# ── 창 제어 + 캡처 ────────────────────────────────────────────────────────────

def activate_window(hwnd: int):
    """창 활성화 (최소화된 경우 복원)"""
    placement = win32gui.GetWindowPlacement(hwnd)
    if placement[1] == win32con.SW_SHOWMINIMIZED:
        win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
    win32gui.SetForegroundWindow(hwnd)
    time.sleep(0.4)


def capture_window(hwnd: int) -> Image.Image:
    """창 영역만 캡처"""
    rect = win32gui.GetWindowRect(hwnd)
    return ImageGrab.grab(bbox=rect)


# ── Claude Vision으로 탭 위치 파악 ───────────────────────────────────────────

def identify_tabs(screenshot: Image.Image,
                  api_key: str, model: str) -> list[dict]:
    """
    Claude Vision으로 탭/메뉴 위치 파악.
    반환: [{"name": "탭명", "x_ratio": 0.15, "y_ratio": 0.05}, ...]
    """
    buf = io.BytesIO()
    screenshot.save(buf, format="PNG")
    b64 = base64.standard_b64encode(buf.getvalue()).decode()

    prompt = (
        "이 NGTMediPlus EMR 화면에서 클릭 가능한 탭, 메뉴, 버튼을 모두 찾아주세요.\n"
        "각 항목의 이름과 위치를 이미지 크기 대비 비율(0.0~1.0)로 반환하세요.\n"
        "JSON 배열만 반환 (다른 텍스트 없이):\n"
        '[{"name":"탭이름","x_ratio":0.15,"y_ratio":0.05},...]\n\n'
        "없으면 [] 반환."
    )

    client = anthropic.Anthropic(api_key=api_key)
    resp = client.messages.create(
        model=model,
        max_tokens=800,
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


# ── 탭 자동 순회 ──────────────────────────────────────────────────────────────

def collect_all_tabs(
    hwnd: int,
    tabs: list[dict],
    status_cb=None,
) -> list[tuple[str, Image.Image]]:
    """탭 목록을 순서대로 클릭하며 화면 수집"""
    rect  = win32gui.GetWindowRect(hwnd)
    ox, oy = rect[0], rect[1]
    w = rect[2] - rect[0]
    h = rect[3] - rect[1]

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
