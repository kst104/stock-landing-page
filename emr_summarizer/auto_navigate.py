"""
NGTMediPlus 창 자동 탐색 및 화면 수집
"""

from __future__ import annotations

import base64
import io
import json
import time

import anthropic
import pyautogui
from PIL import Image, ImageGrab

# NGTMediPlus 창 제목 키워드
_NGT_KEYWORDS = ["neomed", "NGT", "전자챠트", "전자차트", "EMR", "차트", "medit"]

# 탭 클릭 후 로딩 대기 시간 (초)
_WAIT_AFTER_CLICK = 1.8


def find_ngt_window():
    """NGTMediPlus 창 찾기. 없으면 None."""
    for kw in _NGT_KEYWORDS:
        wins = pyautogui.getWindowsWithTitle(kw)
        if wins:
            return wins[0]
    return None


def get_all_windows() -> list[str]:
    """현재 열려있는 모든 창 제목 목록"""
    return [w.title for w in pyautogui.getAllWindows() if w.title.strip()]


def capture_window(window) -> Image.Image:
    """창 영역만 캡처"""
    try:
        left  = window.left
        top   = window.top
        right = window.right
        bot   = window.bottom
        return ImageGrab.grab(bbox=(left, top, right, bot))
    except Exception:
        return ImageGrab.grab()


def identify_tabs(screenshot: Image.Image,
                  api_key: str, model: str) -> list[dict]:
    """
    Claude Vision으로 탭/메뉴 버튼 위치 파악.
    반환: [{"name": "탭명", "x_ratio": 0.15, "y_ratio": 0.05}, ...]
    x_ratio, y_ratio 는 이미지 너비/높이 대비 0.0~1.0 비율.
    """
    buf = io.BytesIO()
    screenshot.save(buf, format="PNG")
    b64 = base64.standard_b64encode(buf.getvalue()).decode()

    prompt = (
        "이 NGTMediPlus EMR 화면에서 환자 정보를 탐색할 수 있는 "
        "탭, 메뉴 버튼, 사이드 항목을 모두 찾아주세요.\n\n"
        "각 항목의 이름과 위치를 이미지 크기 대비 비율(0.0~1.0)로 반환하세요.\n"
        "JSON 배열만 반환 (설명 없이):\n"
        '[{"name":"탭이름","x_ratio":0.15,"y_ratio":0.05}, ...]\n\n'
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
        # 마크다운 코드블록 제거
        if "```" in text:
            start = text.find("[")
            end   = text.rfind("]") + 1
            text  = text[start:end]
        tabs = json.loads(text)
        # 유효한 항목만
        return [t for t in tabs
                if "name" in t and "x_ratio" in t and "y_ratio" in t]
    except Exception:
        return []


def collect_all_tabs(
    window,
    tabs: list[dict],
    status_cb=None,
) -> list[Image.Image]:
    """탭 목록을 순서대로 클릭하며 화면 수집"""
    w = window.right  - window.left
    h = window.bottom - window.top
    ox = window.left
    oy = window.top

    images = []
    for i, tab in enumerate(tabs):
        name = tab.get("name", f"탭{i+1}")
        abs_x = int(ox + tab["x_ratio"] * w)
        abs_y = int(oy + tab["y_ratio"] * h)

        if status_cb:
            status_cb(f"[{i+1}/{len(tabs)}] {name} 이동 중...")

        window.activate()
        time.sleep(0.3)
        pyautogui.click(abs_x, abs_y)
        time.sleep(_WAIT_AFTER_CLICK)

        img = capture_window(window)
        images.append((name, img))

    return images  # [(탭명, Image), ...]


def run_auto_collect(
    api_key: str,
    model: str,
    window_title: str | None = None,
    status_cb=None,
) -> tuple[list[tuple[str, Image.Image]], str]:
    """
    전체 자동 수집 파이프라인.
    반환: ([(탭명, Image), ...], 상태메시지)
    """
    # 1. 창 찾기
    if status_cb:
        status_cb("NGTMediPlus 창 탐색 중...")

    if window_title:
        wins = pyautogui.getWindowsWithTitle(window_title)
        window = wins[0] if wins else None
    else:
        window = find_ngt_window()

    if not window:
        return [], "NGTMediPlus 창을 찾을 수 없습니다.\nNGTMediPlus에서 환자 챠트를 열고 다시 시도하세요."

    # 2. 창 활성화 + 초기 화면 캡처
    if status_cb:
        status_cb("NGTMediPlus 창 활성화 중...")
    window.activate()
    time.sleep(0.5)
    screenshot = capture_window(window)

    # 3. Claude Vision으로 탭 분석
    if status_cb:
        status_cb("Claude가 탭 구조 분석 중...")
    tabs = identify_tabs(screenshot, api_key, model)

    if not tabs:
        return [], "탭을 찾지 못했습니다. 환자 챠트가 열려 있는지 확인하세요."

    tab_names = [t["name"] for t in tabs]
    if status_cb:
        status_cb(f"{len(tabs)}개 탭 발견: {', '.join(tab_names)}")

    # 4. 탭 자동 순회
    results = collect_all_tabs(window, tabs, status_cb)

    return results, f"{len(results)}개 탭 자동 수집 완료"
