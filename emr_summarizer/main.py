"""
NGTMediPlus EMR 챠트 자동 요약
실행: python main.py
"""

from __future__ import annotations

import sys
import os

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

# 필수 패키지 확인
_MISSING = []
for _pkg, _imp in [
    ("anthropic",      "anthropic"),
    ("Pillow",         "PIL"),
    ("python-dotenv",  "dotenv"),
    ("pyautogui",      "pyautogui"),
]:
    try:
        __import__(_imp)
    except ImportError:
        _MISSING.append(_pkg)

if _MISSING:
    print("=" * 60)
    print("[오류] 다음 패키지가 설치되어 있지 않습니다:")
    for p in _MISSING:
        print(f"  - {p}")
    print()
    print(f"  pip install -r \"{os.path.join(_HERE, 'requirements.txt')}\"")
    print("=" * 60)
    input("\nEnter 키를 누르면 종료합니다...")
    sys.exit(1)

import threading
import tkinter as tk
from tkinter import messagebox, scrolledtext, ttk

from PIL import Image, ImageEnhance, ImageTk

import config
import summarizer
import auto_navigate


# ══════════════════════════════════════════════════════════════════════════════
# 화면 영역 선택기 (수동 캡처용)
# ══════════════════════════════════════════════════════════════════════════════

class RegionSelector(tk.Toplevel):
    def __init__(self, parent, screenshot: Image.Image, on_selected):
        super().__init__(parent)
        self._screenshot = screenshot
        self._on_selected = on_selected
        self._start = None
        self._rect_id = None

        sw, sh = screenshot.width, screenshot.height
        self.overrideredirect(True)
        self.attributes("-topmost", True)
        self.geometry(f"{sw}x{sh}+0+0")

        dimmed = ImageEnhance.Brightness(screenshot).enhance(0.4)
        self._photo = ImageTk.PhotoImage(dimmed)

        self._canvas = tk.Canvas(self, cursor="crosshair", highlightthickness=0)
        self._canvas.pack(fill="both", expand=True)
        self._canvas.create_image(0, 0, anchor="nw", image=self._photo)
        self._canvas.create_text(
            sw // 2, 40,
            text="드래그로 챠트 영역 선택    |    ESC: 취소",
            fill="white", font=("맑은 고딕", 15, "bold"),
        )
        self._canvas.bind("<ButtonPress-1>",   self._on_press)
        self._canvas.bind("<B1-Motion>",       self._on_drag)
        self._canvas.bind("<ButtonRelease-1>", self._on_release)
        self.bind("<Escape>", lambda _: self.destroy())

    def _on_press(self, e):
        self._start = (e.x, e.y)
        if self._rect_id:
            self._canvas.delete(self._rect_id)

    def _on_drag(self, e):
        if not self._start:
            return
        if self._rect_id:
            self._canvas.delete(self._rect_id)
        self._rect_id = self._canvas.create_rectangle(
            *self._start, e.x, e.y,
            outline="#00d4ff", width=2, fill="#00d4ff", stipple="gray12")

    def _on_release(self, e):
        if not self._start:
            return
        x0, y0 = self._start
        x1, y1 = e.x, e.y
        if abs(x1 - x0) < 20 or abs(y1 - y0) < 20:
            messagebox.showwarning("선택 오류", "더 넓은 영역을 드래그하세요.", parent=self)
            return
        region = self._screenshot.crop((
            min(x0, x1), min(y0, y1),
            max(x0, x1), max(y0, y1)))
        self.destroy()
        self._on_selected(region)


# ══════════════════════════════════════════════════════════════════════════════
# 창 선택 다이얼로그 (자동 수집 시 NGT 창 못 찾았을 때)
# ══════════════════════════════════════════════════════════════════════════════

class WindowPickerDialog(tk.Toplevel):
    def __init__(self, parent, on_selected):
        super().__init__(parent)
        self.title("창 선택")
        self.geometry("420x300")
        self.resizable(False, False)
        self.grab_set()
        self._on_selected = on_selected
        self._build()

    def _build(self):
        ttk.Label(self, text="NGTMediPlus 창을 목록에서 선택하세요:",
                  font=("맑은 고딕", 10)).pack(pady=10)

        frame = ttk.Frame(self)
        frame.pack(fill="both", expand=True, padx=12)

        sb = ttk.Scrollbar(frame)
        sb.pack(side="right", fill="y")
        self._lb = tk.Listbox(frame, yscrollcommand=sb.set, font=("", 9))
        self._lb.pack(fill="both", expand=True)
        sb.config(command=self._lb.yview)

        titles = auto_navigate.get_all_windows()
        for t in titles:
            self._lb.insert("end", t)

        btn = ttk.Frame(self)
        btn.pack(fill="x", padx=12, pady=8)
        ttk.Button(btn, text="선택", command=self._select).pack(side="right", padx=4)
        ttk.Button(btn, text="취소", command=self.destroy).pack(side="right")

    def _select(self):
        sel = self._lb.curselection()
        if not sel:
            messagebox.showwarning("선택 오류", "창을 선택하세요.", parent=self)
            return
        title = self._lb.get(sel[0])
        self.destroy()
        self._on_selected(title)


# ══════════════════════════════════════════════════════════════════════════════
# 탭 확인 다이얼로그 (자동 클릭 전 확인)
# ══════════════════════════════════════════════════════════════════════════════

class TabConfirmDialog(tk.Toplevel):
    def __init__(self, parent, tabs: list[dict], on_confirm, title: str = "발견된 탭 확인"):
        super().__init__(parent)
        self.title(title)
        self.geometry("360x280")
        self.resizable(False, False)
        self.grab_set()
        self._tabs = tabs
        self._on_confirm = on_confirm
        self._build()

    def _build(self):
        ttk.Label(self,
                  text=f"아래 {len(self._tabs)}개 탭을 자동으로 순회합니다.\n진행할까요?",
                  font=("맑은 고딕", 10)).pack(pady=10)

        frame = ttk.Frame(self)
        frame.pack(fill="both", expand=True, padx=16)

        for i, tab in enumerate(self._tabs, 1):
            ttk.Label(frame,
                      text=f"  {i}. {tab['name']}",
                      font=("맑은 고딕", 10)).pack(anchor="w", pady=2)

        btn = ttk.Frame(self)
        btn.pack(fill="x", padx=16, pady=10)
        ttk.Button(btn, text="자동 수집 시작",
                   command=self._confirm).pack(side="right", padx=4)
        ttk.Button(btn, text="취소",
                   command=self.destroy).pack(side="right")

    def _confirm(self):
        self.destroy()
        self._on_confirm()


# ══════════════════════════════════════════════════════════════════════════════
# 썸네일 카드
# ══════════════════════════════════════════════════════════════════════════════

class ThumbCard(ttk.Frame):
    THUMB_W, THUMB_H = 160, 100

    def __init__(self, parent, label: str, image: Image.Image, on_delete):
        super().__init__(parent, relief="ridge", padding=4)
        thumb = image.copy()
        thumb.thumbnail((self.THUMB_W, self.THUMB_H))
        self._photo = ImageTk.PhotoImage(thumb)
        ttk.Label(self, text=label, font=("", 8, "bold")).pack()
        ttk.Label(self, image=self._photo).pack()
        ttk.Button(self, text="✕ 삭제", width=8,
                   command=on_delete).pack(pady=(4, 0))


# ══════════════════════════════════════════════════════════════════════════════
# 설정 다이얼로그
# ══════════════════════════════════════════════════════════════════════════════

class SettingsDialog(tk.Toplevel):
    def __init__(self, parent, settings: dict, on_save):
        super().__init__(parent)
        self.title("설정")
        self.resizable(False, False)
        self.grab_set()
        self._settings = dict(settings)
        self._on_save = on_save
        self._vars: dict[str, tk.StringVar] = {}
        self._build()

    def _build(self):
        frame = ttk.LabelFrame(self, text="Claude API 설정")
        frame.pack(padx=16, pady=16, fill="x")
        for i, (label, key, secret) in enumerate([
            ("API 키", "claude_api_key", True),
            ("모델",   "claude_model",   False),
        ]):
            ttk.Label(frame, text=label + ":").grid(
                row=i, column=0, sticky="w", padx=10, pady=8)
            var = tk.StringVar(value=self._settings.get(key, ""))
            ttk.Entry(frame, textvariable=var, width=52,
                      show=("*" if secret else "")).grid(
                row=i, column=1, padx=8, pady=6)
            self._vars[key] = var
        ttk.Label(frame, text="API 키 발급: https://console.anthropic.com",
                  foreground="gray").grid(
            row=2, column=0, columnspan=2, padx=10, pady=(0, 8), sticky="w")

        btn = ttk.Frame(self)
        btn.pack(fill="x", padx=16, pady=(0, 16))
        ttk.Button(btn, text="저장", command=self._save).pack(side="right", padx=4)
        ttk.Button(btn, text="취소", command=self.destroy).pack(side="right")

    def _save(self):
        s = dict(self._settings)
        for key, var in self._vars.items():
            s[key] = var.get().strip()
        config.save(s)
        self._on_save(s)
        self.destroy()
        messagebox.showinfo("저장 완료", "설정이 저장되었습니다.")


# ══════════════════════════════════════════════════════════════════════════════
# 메인 앱
# ══════════════════════════════════════════════════════════════════════════════

class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("NGTMediPlus EMR 자동 요약")
        self.geometry("900x720")
        self.minsize(750, 550)

        self._settings = config.load()
        self._captures: list[tuple[str, Image.Image]] = []
        self._thumb_cards: list[ThumbCard] = []

        self._build()
        self._check_api_key()
        self._refresh_gallery()

    # ── UI 구성 ───────────────────────────────────────────────────────────────

    def _build(self):
        # 버튼 바
        top = ttk.Frame(self)
        top.pack(fill="x", padx=12, pady=10)

        self._auto_btn = ttk.Button(
            top, text="🤖  자동 수집 + 요약",
            command=self._start_auto, width=20)
        self._auto_btn.pack(side="left", ipady=6)

        ttk.Separator(top, orient="vertical").pack(
            side="left", fill="y", padx=10, pady=4)

        self._add_btn = ttk.Button(
            top, text="📷  수동 캡처 추가",
            command=self._start_manual_capture, width=18)
        self._add_btn.pack(side="left", ipady=6)

        self._summary_btn = ttk.Button(
            top, text="🤖  요약",
            command=self._run_summary, width=10)
        self._summary_btn.pack(side="left", padx=6, ipady=6)

        ttk.Button(top, text="🗑  초기화",
                   command=self._reset, width=10).pack(side="left", ipady=6)

        ttk.Button(top, text="⚙  설정",
                   command=self._open_settings, width=8).pack(
            side="left", padx=8, ipady=6)

        self._api_status = ttk.Label(top, text="", foreground="gray")
        self._api_status.pack(side="right")

        # 캡처 갤러리
        gallery_outer = ttk.LabelFrame(
            self, text="수집된 화면")
        gallery_outer.pack(fill="x", padx=12, pady=(0, 6))

        self._gallery_canvas = tk.Canvas(
            gallery_outer, height=160, highlightthickness=0)
        sx = ttk.Scrollbar(gallery_outer, orient="horizontal",
                           command=self._gallery_canvas.xview)
        self._gallery_canvas.configure(xscrollcommand=sx.set)
        sx.pack(side="bottom", fill="x")
        self._gallery_canvas.pack(fill="x", padx=4, pady=4)

        self._gallery_frame = ttk.Frame(self._gallery_canvas)
        self._gallery_canvas.create_window(
            (0, 0), window=self._gallery_frame, anchor="nw")
        self._gallery_frame.bind("<Configure>", self._on_gallery_resize)

        self._empty_label = ttk.Label(
            self._gallery_frame,
            text="[🤖 자동 수집] 버튼을 클릭하면 NGTMediPlus 창을 자동으로 탐색합니다.\n"
                 "수동으로 추가하려면 [📷 수동 캡처 추가]를 이용하세요.",
            foreground="gray", font=("맑은 고딕", 10))

        # 요약 결과
        result_frame = ttk.LabelFrame(self, text="AI 종합 요약 결과")
        result_frame.pack(fill="both", expand=True, padx=12, pady=(0, 6))

        btn_row = ttk.Frame(result_frame)
        btn_row.pack(fill="x", padx=6, pady=(4, 0))
        ttk.Button(btn_row, text="복사",   command=self._copy).pack(side="right", padx=4)
        ttk.Button(btn_row, text="지우기", command=self._clear_result).pack(side="right")

        self._result_txt = scrolledtext.ScrolledText(
            result_frame, font=("맑은 고딕", 10), wrap="word", state="disabled")
        self._result_txt.pack(fill="both", expand=True, padx=6, pady=6)
        self._result_txt.tag_config(
            "heading", font=("맑은 고딕", 11, "bold"), foreground="#1d4ed8")

        # 상태바
        bar = ttk.Frame(self, relief="sunken")
        bar.pack(fill="x", side="bottom")
        self._status_var = tk.StringVar(value="준비")
        ttk.Label(bar, textvariable=self._status_var,
                  anchor="w").pack(fill="x", padx=8, pady=2)

    # ── 갤러리 ────────────────────────────────────────────────────────────────

    def _on_gallery_resize(self, _):
        self._gallery_canvas.configure(
            scrollregion=self._gallery_canvas.bbox("all"))

    def _refresh_gallery(self):
        for card in self._thumb_cards:
            card.destroy()
        self._thumb_cards.clear()

        if not self._captures:
            self._empty_label.pack(pady=20)
            self._summary_btn.config(state="disabled")
            return

        self._empty_label.pack_forget()
        self._summary_btn.config(state="normal")

        for i, (label, img) in enumerate(self._captures):
            idx = i
            card = ThumbCard(
                self._gallery_frame, label, img,
                on_delete=lambda i=idx: self._delete_capture(i))
            card.pack(side="left", padx=6, pady=4)
            self._thumb_cards.append(card)

        self._status(f"수집된 화면 {len(self._captures)}장 — [요약] 버튼으로 분석하세요.")

    def _delete_capture(self, idx: int):
        if 0 <= idx < len(self._captures):
            self._captures.pop(idx)
            self._refresh_gallery()

    # ── 자동 수집 ─────────────────────────────────────────────────────────────

    def _enable_auto_btn(self):
        self._auto_btn.config(state="normal")

    def _start_auto(self):
        if not self._check_api_key_prompt():
            return

        self._auto_btn.config(state="disabled")
        self._status("NGTMediPlus 창 탐색 중...")

        def _run():
            try:
                found = auto_navigate.find_ngt_window()
                if not found:
                    self.after(0, self._pick_window_and_auto)
                    self.after(0, self._enable_auto_btn)
                    return
                hwnd, title = found
                self.after(0, lambda h=hwnd: self._run_auto_with_hwnd(h))
            except Exception as e:
                self.after(0, lambda err=e: (
                    self._status(f"창 탐색 오류: {err}"),
                    messagebox.showerror("오류", f"창 탐색 중 오류:\n{err}"),
                    self._enable_auto_btn(),
                ))

        threading.Thread(target=_run, daemon=True).start()

    def _pick_window_and_auto(self):
        WindowPickerDialog(self, on_selected=self._run_auto_with_title)

    def _run_auto_with_title(self, title: str):
        hwnd = auto_navigate.get_hwnd_by_title(title)
        if not hwnd:
            messagebox.showerror("오류", f"'{title}' 창을 찾을 수 없습니다.")
            self._enable_auto_btn()
            return
        self._run_auto_with_hwnd(hwnd)

    def _run_auto_with_hwnd(self, hwnd: int):
        self._auto_btn.config(state="disabled")
        self._status("EMR 문서 목록 분석 중...")

        api_key = self._settings["claude_api_key"]
        model   = self._settings.get("claude_model", "claude-sonnet-4-6")

        def _analyze():
            try:
                auto_navigate.activate_window(hwnd)
                self.after(0, lambda: self._status(
                    "Claude Vision으로 문서 목록 항목 위치 파악 중..."))

                results, found_items = auto_navigate.collect_doc_list(
                    hwnd,
                    target_docs=auto_navigate.DEFAULT_TARGET_DOCS,
                    api_key=api_key,
                    model=model,
                    status_cb=lambda m: self.after(
                        0, lambda msg=m: self._status(msg)),
                )

                if not results:
                    # Vision으로 문서 못 찾음 — 현재 화면 캡처 후 요약
                    screenshot = auto_navigate.capture_window(hwnd)
                    self.after(0, lambda s=screenshot: (
                        self._captures.append(("현재 화면", s)),
                        self._refresh_gallery(),
                        self._status("문서 항목 미발견 — 현재 화면으로 요약합니다."),
                        self._enable_auto_btn(),
                        self._run_summary(),
                    ))
                    return

                self.after(0, lambda r=results: self._on_collected(r))

            except Exception as e:
                self.after(0, lambda err=e: (
                    self._status(f"분석 오류: {err}"),
                    messagebox.showerror("오류", f"화면 분석 중 오류:\n{err}"),
                    self._enable_auto_btn(),
                ))

        threading.Thread(target=_analyze, daemon=True).start()

    def _confirm_and_collect(self, tabs: list[dict], hwnd: int, method: str = ""):
        label = f"발견된 탭 확인 ({method})" if method else "발견된 탭 확인"
        TabConfirmDialog(
            self, tabs, title=label,
            on_confirm=lambda: self._do_collect(tabs, hwnd))

    def _do_collect(self, tabs: list[dict], hwnd: int):
        self._auto_btn.config(state="disabled")
        self._add_btn.config(state="disabled")

        def _run():
            try:
                results = auto_navigate.collect_all_tabs(
                    hwnd, tabs,
                    status_cb=lambda m: self.after(0, lambda msg=m: self._status(msg)))
                self.after(0, lambda r=results: self._on_collected(r))
            except Exception as e:
                self.after(0, lambda err=e: (
                    self._status(f"수집 오류: {err}"),
                    messagebox.showerror("수집 오류", str(err)),
                    self._enable_auto_btn(),
                    self._add_btn.config(state="normal"),
                ))

        threading.Thread(target=_run, daemon=True).start()

    def _on_collected(self, results: list[tuple[str, Image.Image]]):
        self._captures.extend(results)
        self._refresh_gallery()
        self._auto_btn.config(state="normal")
        self._add_btn.config(state="normal")
        self._status(f"자동 수집 완료 ({len(results)}개 탭) — [요약] 버튼을 클릭하세요.")
        # 바로 요약 시작
        self._run_summary()

    # ── 수동 캡처 ─────────────────────────────────────────────────────────────

    def _start_manual_capture(self):
        if not self._check_api_key_prompt():
            return

        self._add_btn.config(state="disabled")
        self._status("3초 후 캡처합니다. NGTMediPlus 화면으로 이동하세요...")

        def _run():
            import time
            from PIL import ImageGrab
            for i in (3, 2, 1):
                self.after(0, lambda n=i: self._status(f"{n}초 후 캡처합니다..."))
                time.sleep(1)
            self.after(0, self.withdraw)
            time.sleep(0.3)
            screenshot = ImageGrab.grab(all_screens=True)
            self.after(0, lambda s=screenshot: self._show_selector(s))

        threading.Thread(target=_run, daemon=True).start()

    def _show_selector(self, screenshot: Image.Image):
        self.deiconify()
        RegionSelector(self, screenshot, self._on_manual_region)

    def _on_manual_region(self, region: Image.Image):
        n = len(self._captures) + 1
        self._captures.append((f"수동 캡처 {n}", region))
        self._add_btn.config(state="normal")
        self._refresh_gallery()

    # ── 요약 ─────────────────────────────────────────────────────────────────

    def _run_summary(self):
        if not self._captures:
            messagebox.showwarning("수집 없음", "먼저 화면을 수집해주세요.")
            return

        self._auto_btn.config(state="disabled")
        self._add_btn.config(state="disabled")
        self._summary_btn.config(state="disabled")
        n = len(self._captures)
        self._set_result(f"화면 {n}장을 AI가 분석 중입니다...\n\n잠시 기다려 주세요.")
        self._status(f"화면 {n}장 종합 분석 중...")

        def _run():
            try:
                result = summarizer.summarize_images(
                    self._captures,
                    api_key=self._settings["claude_api_key"],
                    model=self._settings.get("claude_model", "claude-sonnet-4-6"),
                )
                self.after(0, lambda r=result: self._display_result(r))
            except Exception as e:
                self.after(0, lambda err=e: (
                    self._status(f"오류: {err}"),
                    messagebox.showerror("요약 오류", str(err)),
                    self._set_result(""),
                ))
            finally:
                self.after(0, lambda: (
                    self._auto_btn.config(state="normal"),
                    self._add_btn.config(state="normal"),
                    self._summary_btn.config(state="normal"),
                ))

        threading.Thread(target=_run, daemon=True).start()

    def _display_result(self, text: str):
        self._set_result(text)
        self._status(f"요약 완료 — 화면 {len(self._captures)}장 분석")

    # ── 유틸 ─────────────────────────────────────────────────────────────────

    def _check_api_key_prompt(self) -> bool:
        if not self._settings.get("claude_api_key"):
            messagebox.showerror("API 키 없음",
                                 "먼저 [⚙ 설정]에서 Claude API 키를 입력하세요.")
            return False
        return True

    def _reset(self):
        if self._captures and not messagebox.askyesno(
                "초기화", f"수집된 화면 {len(self._captures)}장을 모두 삭제할까요?"):
            return
        self._captures.clear()
        self._refresh_gallery()
        self._clear_result()
        self._status("초기화 완료")

    def _open_settings(self):
        SettingsDialog(self, self._settings, self._on_settings_saved)

    def _on_settings_saved(self, s: dict):
        self._settings = s
        self._check_api_key()

    def _check_api_key(self):
        if self._settings.get("claude_api_key"):
            self._api_status.config(text="● API 키 설정됨", foreground="#16a34a")
        else:
            self._api_status.config(text="● API 키 없음", foreground="#dc2626")

    def _status(self, msg: str):
        self._status_var.set(msg)

    def _set_result(self, text: str):
        self._result_txt.config(state="normal")
        self._result_txt.delete("1.0", "end")
        for line in text.splitlines(keepends=True):
            tag = "heading" if line.startswith("## ") else ""
            self._result_txt.insert("end", line, tag)
        self._result_txt.config(state="disabled")

    def _copy(self):
        text = self._result_txt.get("1.0", "end").strip()
        if text:
            self.clipboard_clear()
            self.clipboard_append(text)
            self._status("클립보드에 복사되었습니다.")

    def _clear_result(self):
        self._set_result("")


if __name__ == "__main__":
    app = App()
    app.mainloop()
