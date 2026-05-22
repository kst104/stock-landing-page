"""
NGTMediPlus EMR 챠트 자동 탐색 및 화면 수집 (pywin32 기반)

캡처: 창을 포그라운드로 올린 뒤 전체 화면 캡처 (검은 화면 방지)
문서 탐색: Claude Vision으로 왼쪽 메뉴 항목 위치 파악 후 클릭
"""

from __future__ import annotations

import base64
import io
import json
import os
import time

import anthropic
import pyautogui
import win32con
import win32gui
import win32process
from PIL import Image, ImageGrab

_NGT_KEYWORDS = [
    "neomed", "ngt", "전자챠트", "전자차트", "emr",
    "차트", "medit", "문서 작성",
]
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
    my_pid = os.getpid()
    for hwnd, title in _enum_windows():
        try:
            _, pid = win32process.GetWindowThreadProcessId(hwnd)
            if pid == my_pid:
                continue  # 요약 앱 자신 제외
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


# ── 창 활성화 + 전체화면 캡처 ─────────────────────────────────────────────────

def activate_window(hwnd: int):
    placement = win32gui.GetWindowPlacement(hwnd)
    if placement[1] == win32con.SW_SHOWMINIMIZED:
        win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
    win32gui.SetForegroundWindow(hwnd)
    time.sleep(0.6)


def capture_screen(hwnd: int) -> Image.Image:
    """
    hwnd 창을 포그라운드로 올린 뒤 전체 화면 캡처.
    PrintWindow 방식을 쓰지 않으므로 어떤 앱이든 검은 화면 없음.
    """
    activate_window(hwnd)
    time.sleep(0.3)                  # 창이 완전히 그려질 때까지 대기
    return ImageGrab.grab()          # 주 모니터 전체 캡처


# ── Claude Vision: 왼쪽 메뉴 전체 항목 파악 ──────────────────────────────────

def find_all_menu_items_by_vision(
    screenshot: Image.Image,
    api_key: str,
    model: str,
) -> list[dict]:
    """
    EMR 화면 왼쪽 메뉴/사이드바의 클릭 가능한 모든 항목 위치 파악.
    반환: [{"name":..., "x_ratio":..., "y_ratio":...}, ...]  (위→아래 순)
    좌표는 스크린샷 전체 크기 기준 비율.
    """
    buf = io.BytesIO()
    screenshot.save(buf, format="PNG")
    b64 = base64.standard_b64encode(buf.getvalue()).decode()

    prompt = (
        "이 EMR 화면의 왼쪽 사이드바 또는 메뉴 패널에서 "
        "클릭 가능한 모든 항목(진료기록지, 경과기록, 처방, 검사결과, 입원기록 등)을 찾아주세요.\n"
        "왼쪽 메뉴가 없으면 오른쪽 패널 또는 상단 탭도 확인하세요.\n"
        "각 항목의 이름과 클릭 위치를 x_ratio, y_ratio "
        "(이미지 전체 너비/높이 대비 0.0~1.0)로 반환하세요.\n"
        "위에서 아래 순서로, JSON 배열만 응답 (다른 텍스트 없이):\n"
        '[{"name":"항목이름","x_ratio":0.08,"y_ratio":0.15},...]\n\n'
        "클릭 가능한 항목이 없으면 [] 반환."
    )

    client = anthropic.Anthropic(api_key=api_key)
    resp = client.messages.create(
        model=model,
        max_tokens=1500,
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
        items = json.loads(text)
        return [x for x in items
                if "name" in x and "x_ratio" in x and "y_ratio" in x]
    except Exception:
        return []


# ── 메뉴 항목 자동 순회 ───────────────────────────────────────────────────────

def collect_menu_items(
    hwnd: int,
    items: list[dict],
    status_cb=None,
    item_cb=None,
) -> list[tuple[str, Image.Image]]:
    """
    메뉴 항목 목록을 순서대로 클릭하며 전체화면 캡처.

    좌표 기준: ImageGrab.grab() 캡처 크기 (pyautogui.size() 사용 시
    멀티모니터 환경에서 좌표 불일치 발생 → 실제 캡처 이미지 크기 사용).

    status_cb(msg)                 — 상태 문자열 콜백
    item_cb(idx, total, name, img) — 항목 캡처 완료 콜백
    """
    # Vision이 분석한 스크린샷과 동일한 기준으로 좌표 계산
    ref = ImageGrab.grab()
    sw, sh = ref.size
    total  = len(items)

    results = []
    for i, item in enumerate(items):
        name  = item.get("name", f"항목{i+1}")
        abs_x = int(item["x_ratio"] * sw)
        abs_y = int(item["y_ratio"] * sh)

        if status_cb:
            status_cb(f"[{i+1}/{total}] {name} 클릭 중...")

        # 창 활성화 후 단일 클릭 → 더블클릭 (트리뷰/리스트 호환)
        activate_window(hwnd)
        time.sleep(0.2)
        pyautogui.click(abs_x, abs_y)
        time.sleep(0.3)
        pyautogui.doubleClick(abs_x, abs_y)
        time.sleep(_WAIT_AFTER_CLICK)

        img = capture_screen(hwnd)
        results.append((name, img))

        if item_cb:
            item_cb(i + 1, total, name, img)

    return results


# 하위 호환
def collect_all_tabs(hwnd, tabs, status_cb=None):
    return collect_menu_items(hwnd, tabs, status_cb)
