"""
NGTMediPlus EMR 챠트 자동 탐색 및 화면 수집 (pywin32 기반)

캡처: PrintWindow(PW_RENDERFULLCONTENT) → ImageGrab 폴백
문서 탐색: Claude Vision으로 작성문서 목록 항목 위치 파악 후 클릭
"""

from __future__ import annotations

import base64
import ctypes
import io
import json
import time

import anthropic
import pyautogui
import win32con
import win32gui
import win32ui
from PIL import Image, ImageGrab

_NGT_KEYWORDS = [
    "neomed", "ngt", "전자챠트", "전자차트", "emr",
    "차트", "medit", "문서 작성",
]
_WAIT_AFTER_CLICK = 2.0

# 기본 수집 문서 목록 (없는 항목은 Vision이 자동 제외)
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


# ── Claude Vision: 문서 목록 항목 위치 파악 ──────────────────────────────────

def find_doc_items_by_vision(
    screenshot: Image.Image,
    target_docs: list[str],
    api_key: str,
    model: str,
) -> list[dict]:
    """
    EMR 화면에서 작성문서 목록 항목의 위치를 Vision으로 파악.
    반환: [{"name":..., "x_ratio":..., "y_ratio":...}, ...] (target_docs 순서)
    """
    buf = io.BytesIO()
    screenshot.save(buf, format="PNG")
    b64 = base64.standard_b64encode(buf.getvalue()).decode()

    doc_list = "\n".join(f"- {d}" for d in target_docs)
    prompt = (
        "이 EMR 화면의 좌측 또는 우측 문서 목록(트리뷰/리스트/사이드바)에서 "
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
        # target_docs 순서로 정렬
        order = {d: i for i, d in enumerate(target_docs)}
        items.sort(key=lambda x: order.get(x["name"], 999))
        return items
    except Exception:
        return []


# ── 문서 목록 자동 순회 ───────────────────────────────────────────────────────

def collect_doc_list(
    hwnd: int,
    target_docs: list[str],
    api_key: str,
    model: str,
    status_cb=None,
) -> tuple[list[tuple[str, Image.Image]], list[dict]]:
    """
    작성문서 목록에서 target_docs 항목을 순서대로 클릭하며 캡처.
    반환: ([(탭명, Image), ...], found_items)
    """
    if status_cb:
        status_cb("문서 목록 항목 위치 분석 중 (Vision)...")

    screenshot = capture_window(hwnd)
    items = find_doc_items_by_vision(screenshot, target_docs, api_key, model)

    if not items:
        return [], []

    rect = win32gui.GetWindowRect(hwnd)
    ox, oy = rect[0], rect[1]
    w = rect[2] - ox
    h = rect[3] - oy

    results = []
    for i, item in enumerate(items):
        name  = item.get("name", f"문서{i+1}")
        abs_x = int(ox + item["x_ratio"] * w)
        abs_y = int(oy + item["y_ratio"] * h)

        if status_cb:
            status_cb(f"[{i+1}/{len(items)}] {name} 캡처 중...")

        activate_window(hwnd)
        pyautogui.click(abs_x, abs_y)
        time.sleep(_WAIT_AFTER_CLICK)

        img = capture_window(hwnd)
        results.append((name, img))

    return results, items


# ── 하위 호환: 탭 기반 수집 (수동 지정 탭 목록용) ────────────────────────────

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
