"""
Windows UI Automation(UIA) 기반 EMR 메뉴 항목 탐색.

Vision/좌표 어림 방식 대신 Windows 접근성 API로
항목 이름과 정확한 클릭 좌표를 직접 읽어온다.
NGTMediPlus가 UIA를 노출하면 Vision 없이 100% 정확.
노출하지 않으면 빈 리스트를 반환 → Vision 폴백으로 자동 전환.
"""

from __future__ import annotations

import win32gui

from auto_navigate import EXCLUDED_KEYWORDS, is_excluded

# UIA 컨트롤 타입 — 왼쪽 메뉴에서 클릭 대상이 될 수 있는 유형
_CLICKABLE_TYPES = {
    "TreeItemControl",
    "ListItemControl",
    "ButtonControl",
    "HyperlinkControl",
    "CustomControl",
}

# UIA를 통해 읽어낸 항목의 최소 크기 (픽셀) — 아이콘·구분선 제외
_MIN_H = 8
_MIN_W = 20


def find_menu_items_uia(hwnd: int) -> list[dict]:
    """
    UIAutomation으로 EMR 왼쪽 메뉴의 항목을 찾는다.

    반환: [{"name":..., "abs_x":..., "abs_y":...}, ...]
          항목을 위→아래 순으로 정렬.
          UIA를 지원하지 않거나 항목이 없으면 빈 리스트.
    """
    try:
        import uiautomation as uia
    except ImportError:
        return []

    try:
        win_rect   = win32gui.GetWindowRect(hwnd)
        win_left   = win_rect[0]
        win_top    = win_rect[1]
        win_w      = win_rect[2] - win_left
        panel_right = win_left + int(win_w * 0.28)   # 왼쪽 28% = 메뉴 패널 영역

        root  = uia.ControlFromHandle(hwnd)
        items: list[dict] = []
        seen:  set[str]   = set()

        def _walk(ctrl: uia.Control, depth: int = 0):
            if depth > 10:
                return
            try:
                r    = ctrl.BoundingRectangle
                name = (ctrl.Name or "").strip()
                ctype = ctrl.ControlTypeName

                # 패널 범위 안 + 클릭 가능 타입 + 적당한 크기
                if (name
                        and r.right <= panel_right
                        and r.left  >= win_left
                        and (r.bottom - r.top)  >= _MIN_H
                        and (r.right  - r.left) >= _MIN_W
                        and ctype in _CLICKABLE_TYPES
                        and not is_excluded(name)
                        and name not in seen):
                    seen.add(name)
                    items.append({
                        "name":  name,
                        "abs_x": (r.left + r.right)  // 2,
                        "abs_y": (r.top  + r.bottom) // 2,
                    })
            except Exception:
                pass

            try:
                for child in ctrl.GetChildren():
                    _walk(child, depth + 1)
            except Exception:
                pass

        _walk(root)

        # 위→아래 순 정렬
        items.sort(key=lambda x: x["abs_y"])
        return items

    except Exception:
        return []


def find_nav_buttons_uia(hwnd: int) -> dict | None:
    """
    UIAutomation으로 '이전' / '다음' 버튼의 절대 좌표를 찾는다.
    반환: {"prev_abs_x":..., "prev_abs_y":..., "next_abs_x":..., "next_abs_y":...}
          찾지 못하면 None.
    """
    try:
        import uiautomation as uia
    except ImportError:
        return None

    _PREV_NAMES = {"이전", "◀", "prev", "<", "previous", "이전 페이지"}
    _NEXT_NAMES = {"다음", "▶", "next", ">", "forward",  "다음 페이지"}
    _BTN_TYPES  = {"ButtonControl", "CustomControl", "HyperlinkControl"}

    prev_pos: tuple | None = None
    next_pos: tuple | None = None

    try:
        root = uia.ControlFromHandle(hwnd)

        def _walk(ctrl, depth: int = 0):
            nonlocal prev_pos, next_pos
            if depth > 10:
                return
            try:
                name  = (ctrl.Name or "").strip()
                ctype = ctrl.ControlTypeName
                if ctype in _BTN_TYPES and name:
                    r  = ctrl.BoundingRectangle
                    cx = (r.left + r.right)  // 2
                    cy = (r.top  + r.bottom) // 2
                    nl = name.lower()
                    if nl in _PREV_NAMES and prev_pos is None:
                        prev_pos = (cx, cy)
                    elif nl in _NEXT_NAMES and next_pos is None:
                        next_pos = (cx, cy)
            except Exception:
                pass
            try:
                for child in ctrl.GetChildren():
                    _walk(child, depth + 1)
            except Exception:
                pass

        _walk(root)

        result: dict = {}
        if prev_pos:
            result["prev_abs_x"], result["prev_abs_y"] = prev_pos
        if next_pos:
            result["next_abs_x"], result["next_abs_y"] = next_pos
        return result if result else None

    except Exception:
        return None


def is_available() -> bool:
    """uiautomation 패키지가 설치되어 있으면 True."""
    try:
        import uiautomation  # noqa: F401
        return True
    except ImportError:
        return False
