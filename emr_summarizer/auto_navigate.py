"""
NGTMediPlus EMR 챠트 자동 탐색 및 화면 수집 (pywin32 기반)

캡처: PrintWindow(PW_RENDERFULLCONTENT) → ImageGrab 폴백
문서 탐색: Claude Vision으로 왼쪽 메뉴/사이드바 항목 위치 파악 후 클릭
"""

from __future__ import annotations

import base64
import ctypes
import io
import json
import time

import os

import anthropic
import pyautogui
import win32con
import win32gui
import win32process
import win32ui
from PIL import Image, ImageGrab

_NGT_KEYWORDS = [
    "neomed", "ngt", "전자챠트", "전자차트", "emr",
    "차트", "medit", "문서 작성",
]
_WAIT_AFTER_CLICK = 2.0

# 우선 수집할 문서 목록 (없는 항목은 Vision이 자동 제외)
DEFAULT_TARGET_DOCS = [
    "응급실 진료기록지",
    "진료부-공통-입원경과기록지",
    "내과-외래경과기록지",
    "외과_외래경과기록지",
    "정형외과_외래경과기록지",
    "검사결과",
]


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
                continue  # 자기 자신(요약 앱) 제외
        except Exception:
            pass
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
    """
    PrintWindow(PW_RENDERFULLCONTENT=2)로 캡처.
    하드웨어 가속/DRM 창의 검은 화면 문제 해결.
    실패 시 ImageGrab 폴백.
    """
    try:
        left, top, right, bottom = win32gui.GetWindowRect(hwnd)
        w, h = right - left, bottom - top
        if w <= 0 or h <= 0:
            raise ValueError("zero-size window")

        hwnd_dc = win32gui.GetWindowDC(hwnd)
        mfc_dc  = win32ui.CreateDCFromHandle(hwnd_dc)
        save_dc = mfc_dc.CreateCompatibleDC()
        bmp     = win32ui.CreateBitmap()
        bmp.CreateCompatibleBitmap(mfc_dc, w, h)
        save_dc.SelectObject(bmp)

        ctypes.windll.user32.PrintWindow(hwnd, save_dc.GetSafeHdc(), 2)

        info = bmp.GetInfo()
        bits = bmp.GetBitmapBits(True)
        img  = Image.frombuffer(
            "RGB",
            (info["bmWidth"], info["bmHeight"]),
            bits, "raw", "BGRX", 0, 1,
        )

        win32gui.DeleteObject(bmp.GetHandle())
        save_dc.DeleteDC()
        mfc_dc.DeleteDC()
        win32gui.ReleaseDC(hwnd, hwnd_dc)
        return img

    except Exception:
        rect = win32gui.GetWindowRect(hwnd)
        return ImageGrab.grab(bbox=rect)


# ── Claude Vision: 왼쪽 메뉴 전체 항목 파악 ──────────────────────────────────

def find_all_menu_items_by_vision(
    screenshot: Image.Image,
    api_key: str,
    model: str,
) -> list[dict]:
    """
    EMR 화면 왼쪽 메뉴/사이드바의 클릭 가능한 모든 항목 위치 파악.
    반환: [{"name":..., "x_ratio":..., "y_ratio":...}, ...]  (위→아래 순)
    """
    buf = io.BytesIO()
    screenshot.save(buf, format="PNG")
    b64 = base64.standard_b64encode(buf.getvalue()).decode()

    prompt = (
        "이 EMR 화면의 왼쪽 사이드바 또는 메뉴 패널에서 "
        "클릭 가능한 모든 항목(진료기록지, 경과기록, 처방, 검사결과, 입원기록 등)을 찾아주세요.\n"
        "왼쪽 메뉴가 없으면 오른쪽 패널 또는 상단 탭도 확인하세요.\n"
        "각 항목의 이름과 클릭 위치를 x_ratio, y_ratio(이미지 크기 대비 0.0~1.0)로 반환하세요.\n"
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


def find_doc_items_by_vision(
    screenshot: Image.Image,
    target_docs: list[str],
    api_key: str,
    model: str,
) -> list[dict]:
    """
    특정 문서 목록 항목의 위치 파악 (이름 지정 방식).
    반환: [{"name":..., "x_ratio":..., "y_ratio":...}, ...] (target_docs 순서)
    """
    buf = io.BytesIO()
    screenshot.save(buf, format="PNG")
    b64 = base64.standard_b64encode(buf.getvalue()).decode()

    doc_list = "\n".join(f"- {d}" for d in target_docs)
    prompt = (
        "이 EMR 화면의 왼쪽 사이드바 또는 문서 목록에서 "
        "다음 항목들을 찾아주세요:\n"
        f"{doc_list}\n\n"
        "찾은 항목의 클릭 위치를 x_ratio, y_ratio (이미지 크기 대비 0.0~1.0)로 반환하세요.\n"
        "화면에 없는 항목은 제외하고, JSON 배열만 응답:\n"
        '[{"name":"항목이름","x_ratio":0.1,"y_ratio":0.3},...]'
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
        items = json.loads(text)
        items = [x for x in items
                 if "name" in x and "x_ratio" in x and "y_ratio" in x]
        order = {d: i for i, d in enumerate(target_docs)}
        items.sort(key=lambda x: order.get(x["name"], 999))
        return items
    except Exception:
        return []


# ── 메뉴 항목 자동 순회 ───────────────────────────────────────────────────────

def collect_menu_items(
    hwnd: int,
    items: list[dict],
    status_cb=None,
) -> list[tuple[str, Image.Image]]:
    """
    메뉴 항목 목록을 순서대로 클릭하며 화면 수집.
    반환: [(항목명, Image), ...]
    """
    rect = win32gui.GetWindowRect(hwnd)
    ox, oy = rect[0], rect[1]
    w = rect[2] - ox
    h = rect[3] - oy

    results = []
    for i, item in enumerate(items):
        name  = item.get("name", f"항목{i+1}")
        abs_x = int(ox + item["x_ratio"] * w)
        abs_y = int(oy + item["y_ratio"] * h)

        if status_cb:
            status_cb(f"[{i+1}/{len(items)}] {name} 캡처 중...")

        activate_window(hwnd)
        pyautogui.click(abs_x, abs_y)
        time.sleep(_WAIT_AFTER_CLICK)

        img = capture_window(hwnd)
        results.append((name, img))

    return results


# 하위 호환
def collect_all_tabs(hwnd, tabs, status_cb=None):
    return collect_menu_items(hwnd, tabs, status_cb)
