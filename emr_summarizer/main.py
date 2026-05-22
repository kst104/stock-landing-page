"""
NGTMediPlus EMR 챠트 자동 요약 (화면 캡처 방식)
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
for _pkg, _imp in [("anthropic", "anthropic"), ("Pillow", "PIL"), ("python-dotenv", "dotenv")]:
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
    print("아래 명령어로 설치 후 다시 실행하세요:")
    print(f"  pip install -r \"{os.path.join(_HERE, 'requirements.txt')}\"")
    print("=" * 60)
    input("\nEnter 키를 누르면 종료합니다...")
    sys.exit(1)

import threading
import tkinter as tk
from tkinter import messagebox, scrolledtext, ttk

from PIL import Image, ImageDraw, ImageEnhance, ImageTk

import config
import summarizer


# ══════════════════════════════════════════════════════════════════════════════
# 화면 영역 선택기
# ══════════════════════════════════════════════════════════════════════════════

class RegionSelector(tk.Toplevel):
    """
    현재 화면을 배경으로 보여주고 마우스 드래그로 캡처 영역을 선택한다.
    선택 완료 시 on_selected(PIL.Image) 콜백 호출.
    """

    def __init__(self, parent, screenshot: Image.Image, on_selected):
        super().__init__(parent)
        self._screenshot = screenshot
        self._on_selected = on_selected
        self._start = None
        self._rect_id = None

        # 전체 화면 덮기
        sw = self.winfo_screenwidth()
        sh = self.winfo_screenheight()
        self.overrideredirect(True)
        self.attributes("-topmost", True)
        self.geometry(f"{sw}x{sh}+0+0")

        # 화면 어둡게 처리
        dimmed = ImageEnhance.Brightness(screenshot).enhance(0.45)
        self._photo = ImageTk.PhotoImage(dimmed)

        self._canvas = tk.Canvas(self, cursor="crosshair",
                                 highlightthickness=0)
        self._canvas.pack(fill="both", expand=True)
        self._canvas.create_image(0, 0, anchor="nw", image=self._photo)
        self._canvas.create_text(
            sw // 2, 36,
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
            outline="#00d4ff", width=2,
            fill="#00d4ff", stipple="gray12",
        )

    def _on_release(self, e):
        if not self._start:
            return
        x0, y0 = self._start
        x1, y1 = e.x, e.y
        # 최소 크기 보장
        if abs(x1 - x0) < 20 or abs(y1 - y0) < 20:
            messagebox.showwarning("선택 오류", "더 넓은 영역을 드래그해서 선택하세요.",
                                   parent=self)
            return
        region = self._screenshot.crop((
            min(x0, x1), min(y0, y1),
            max(x0, x1), max(y0, y1),
        ))
        self.destroy()
        self._on_selected(region)


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

        fields = [
            ("API 키",  "claude_api_key", True),
            ("모델",    "claude_model",   False),
        ]
        for i, (label, key, secret) in enumerate(fields):
            ttk.Label(frame, text=label + ":").grid(
                row=i, column=0, sticky="w", padx=10, pady=8)
            var = tk.StringVar(value=self._settings.get(key, ""))
            ttk.Entry(frame, textvariable=var, width=52,
                      show=("*" if secret else "")).grid(
                row=i, column=1, padx=8, pady=6)
            self._vars[key] = var

        ttk.Label(frame,
                  text="API 키 발급: https://console.anthropic.com",
                  foreground="gray").grid(
            row=len(fields), column=0, columnspan=2,
            padx=10, pady=(0, 8), sticky="w")

        btn_frame = ttk.Frame(self)
        btn_frame.pack(fill="x", padx=16, pady=(0, 16))
        ttk.Button(btn_frame, text="저장", command=self._save).pack(
            side="right", padx=4)
        ttk.Button(btn_frame, text="취소", command=self.destroy).pack(
            side="right")

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
        self.geometry("820x680")
        self.minsize(700, 500)

        self._settings = config.load()
        self._captured: Image.Image | None = None

        self._build()
        self._check_api_key()

    def _build(self):
        # ── 상단 버튼 바 ──
        top = ttk.Frame(self)
        top.pack(fill="x", padx=12, pady=10)

        self._capture_btn = ttk.Button(
            top, text="📷  챠트 캡처 후 요약",
            command=self._start_capture,
            width=22,
        )
        self._capture_btn.pack(side="left", ipady=6)

        ttk.Button(top, text="⚙  설정",
                   command=self._open_settings,
                   width=10).pack(side="left", padx=8, ipady=6)

        self._api_status = ttk.Label(top, text="", foreground="gray")
        self._api_status.pack(side="right")

        # ── 캡처 미리보기 ──
        preview_frame = ttk.LabelFrame(self, text="캡처된 화면")
        preview_frame.pack(fill="x", padx=12, pady=(0, 6))

        self._preview_lbl = ttk.Label(
            preview_frame,
            text="NGTMediPlus에서 환자 챠트를 열고\n[챠트 캡처 후 요약] 버튼을 클릭하세요.",
            foreground="gray", anchor="center",
        )
        self._preview_lbl.pack(fill="x", padx=8, pady=16)

        # ── 요약 결과 ──
        result_frame = ttk.LabelFrame(self, text="AI 요약 결과")
        result_frame.pack(fill="both", expand=True, padx=12, pady=(0, 6))

        btn_row = ttk.Frame(result_frame)
        btn_row.pack(fill="x", padx=6, pady=(4, 0))
        ttk.Button(btn_row, text="복사",   command=self._copy).pack(side="right", padx=4)
        ttk.Button(btn_row, text="지우기", command=self._clear).pack(side="right")

        self._result_txt = scrolledtext.ScrolledText(
            result_frame,
            font=("맑은 고딕", 10),
            wrap="word",
            state="disabled",
        )
        self._result_txt.pack(fill="both", expand=True, padx=6, pady=6)
        self._result_txt.tag_config(
            "heading", font=("맑은 고딕", 11, "bold"), foreground="#1d4ed8")

        # ── 상태바 ──
        bar = ttk.Frame(self, relief="sunken")
        bar.pack(fill="x", side="bottom")
        self._status_var = tk.StringVar(value="준비")
        ttk.Label(bar, textvariable=self._status_var,
                  anchor="w").pack(fill="x", padx=8, pady=2)

    # ── API 키 상태 표시 ──────────────────────────────────────────────────────

    def _check_api_key(self):
        if self._settings.get("claude_api_key"):
            self._api_status.config(text="● API 키 설정됨", foreground="#16a34a")
        else:
            self._api_status.config(text="● API 키 없음 — [설정] 필요", foreground="#dc2626")

    # ── 캡처 ─────────────────────────────────────────────────────────────────

    def _start_capture(self):
        if not self._settings.get("claude_api_key"):
            messagebox.showerror(
                "API 키 없음",
                "Claude API 키가 설정되어 있지 않습니다.\n[설정] 버튼을 클릭해서 API 키를 입력하세요.")
            return

        self._status("3초 후 화면을 캡처합니다. NGTMediPlus 챠트로 이동하세요...")
        self._capture_btn.config(state="disabled")

        def _do_capture():
            import time
            # 카운트다운 표시
            for i in (3, 2, 1):
                self.after(0, lambda n=i: self._status(f"{n}초 후 캡처합니다..."))
                time.sleep(1)

            # 앱 숨기기
            self.after(0, self.withdraw)
            time.sleep(0.3)

            # 전체 화면 캡처
            from PIL import ImageGrab
            screenshot = ImageGrab.grab(all_screens=True)

            # 앱 다시 보이기 + 선택기 표시
            self.after(0, lambda: self._show_selector(screenshot))

        threading.Thread(target=_do_capture, daemon=True).start()

    def _show_selector(self, screenshot: Image.Image):
        self.deiconify()
        RegionSelector(self, screenshot, self._on_region_selected)

    def _on_region_selected(self, region: Image.Image):
        self._captured = region
        self._capture_btn.config(state="normal")

        # 미리보기 표시 (최대 너비 맞게 축소)
        thumb = region.copy()
        thumb.thumbnail((760, 200))
        photo = ImageTk.PhotoImage(thumb)
        self._preview_lbl.config(image=photo, text="")
        self._preview_lbl.image = photo  # 참조 유지

        self._status("캡처 완료. AI 요약을 시작합니다...")
        self._run_summary()

    # ── 요약 ─────────────────────────────────────────────────────────────────

    def _run_summary(self):
        if not self._captured:
            return

        self._capture_btn.config(state="disabled")
        self._set_result("AI가 챠트를 분석 중입니다...\n\n잠시 기다려 주세요.")

        def _run():
            try:
                result = summarizer.summarize_image(
                    self._captured,
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
                self.after(0, lambda: self._capture_btn.config(state="normal"))

        threading.Thread(target=_run, daemon=True).start()

    def _display_result(self, text: str):
        self._set_result(text)
        self._status("요약 완료")

    # ── 유틸 ─────────────────────────────────────────────────────────────────

    def _open_settings(self):
        SettingsDialog(self, self._settings, self._on_settings_saved)

    def _on_settings_saved(self, s: dict):
        self._settings = s
        self._check_api_key()

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

    def _clear(self):
        self._set_result("")
        self._status("지웠습니다.")


# ── 진입점 ────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    app = App()
    app.mainloop()
