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
import pyautogui
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
    """발견된 EMR 탭을 클릭으로 선택·해제하고 선택된 항목만 수집하는 다이얼로그."""

    def __init__(self, parent, tabs: list[dict], on_confirm, title: str = "수집 항목 선택"):
        super().__init__(parent)
        self.title(title)
        self.geometry("440x480")
        self.resizable(True, True)
        self.grab_set()
        self._tabs: list[dict] = list(tabs)
        self._on_confirm = on_confirm
        self._lb: tk.Listbox | None = None
        self._sel_lbl: ttk.Label | None = None
        self._build()

    # ── UI 구성 ──────────────────────────────────────────────────────────────

    def _build(self):
        ttk.Label(self,
                  text="스캔할 항목을 확인하세요 (선택된 항목만 스캔됩니다):",
                  font=("맑은 고딕", 10, "bold")).pack(pady=(12, 4), padx=16, anchor="w")
        ttk.Label(self,
                  text="기본적으로 전체 선택됨 · 제외할 항목은 클릭해서 해제",
                  font=("맑은 고딕", 8), foreground="gray").pack(padx=16, anchor="w")

        # 전체 선택/해제 + 카운터
        ctrl = ttk.Frame(self)
        ctrl.pack(fill="x", padx=16, pady=(8, 4))
        ttk.Button(ctrl, text="전체 선택", width=10,
                   command=self._select_all).pack(side="left", padx=(0, 4))
        ttk.Button(ctrl, text="전체 해제", width=10,
                   command=self._deselect_all).pack(side="left")
        self._sel_lbl = ttk.Label(ctrl, text="0 / 0 선택", foreground="gray")
        self._sel_lbl.pack(side="right")

        # 리스트박스 (MULTIPLE: 클릭만으로 토글)
        lf = ttk.Frame(self)
        lf.pack(fill="both", expand=True, padx=16, pady=(0, 6))

        sb = ttk.Scrollbar(lf)
        sb.pack(side="right", fill="y")

        self._lb = tk.Listbox(
            lf,
            selectmode=tk.MULTIPLE,
            yscrollcommand=sb.set,
            font=("맑은 고딕", 10),
            activestyle="none",
            selectbackground="#1a7abf",
            selectforeground="white",
            relief="flat",
            bd=1,
            highlightthickness=1,
            highlightcolor="#aaaaaa",
        )
        self._lb.pack(fill="both", expand=True)
        sb.config(command=self._lb.yview)

        for tab in self._tabs:
            self._lb.insert(tk.END, f"  {tab.get('name', '')}")

        # 기본값: 전체 선택 (스캔 확인 게이트 — 빼고 싶은 것만 해제)
        self._lb.select_set(0, tk.END)

        # 마우스 휠
        self._lb.bind("<MouseWheel>",
                      lambda e: self._lb.yview_scroll(-1 * (e.delta // 120), "units"))
        # 선택 변경 감지
        self._lb.bind("<<ListboxSelect>>", lambda e: self._update_count())

        self._update_count()

        # 하단 버튼
        btn_bar = ttk.Frame(self)
        btn_bar.pack(fill="x", padx=16, pady=10, side="bottom")
        ttk.Button(btn_bar, text="선택 항목 스캔 시작",
                   command=self._confirm).pack(side="right", padx=4)
        ttk.Button(btn_bar, text="취소",
                   command=self.destroy).pack(side="right")

    # ── 전체 선택/해제 ───────────────────────────────────────────────────────

    def _select_all(self):
        self._lb.select_set(0, tk.END)
        self._update_count()

    def _deselect_all(self):
        self._lb.select_clear(0, tk.END)
        self._update_count()

    # ── 카운터 갱신 ──────────────────────────────────────────────────────────

    def _update_count(self):
        sel   = len(self._lb.curselection())
        total = self._lb.size()
        self._sel_lbl.config(text=f"{sel} / {total} 선택")

    # ── 확인 ─────────────────────────────────────────────────────────────────

    def _confirm(self):
        indices = self._lb.curselection()
        if not indices:
            messagebox.showwarning("선택 없음", "하나 이상의 항목을 선택하세요.", parent=self)
            return
        selected = [self._tabs[i] for i in indices]
        self.destroy()
        self._on_confirm(selected)


# ══════════════════════════════════════════════════════════════════════════════
# 플로팅 캡처 바 (항상 위에 표시 — 사용자가 메뉴 클릭 후 누름)
# ══════════════════════════════════════════════════════════════════════════════

class FloatingCaptureBar(tk.Toplevel):
    """
    EMR 창 위에 항상 떠 있는 작은 캡처 바.
    사용자가 NGTMediPlus 메뉴 항목을 직접 클릭한 뒤
    [📷 캡처] 버튼을 눌러 화면을 수집한다.
    """
    def __init__(self, parent, on_capture, on_done):
        super().__init__(parent)
        self.overrideredirect(True)      # 제목 표시줄 없음
        self.attributes("-topmost", True)
        self.attributes("-alpha", 0.92)
        self._on_capture = on_capture
        self._on_done    = on_done
        self._count      = 0
        self._drag_x     = 0
        self._drag_y     = 0
        self._build()
        # 화면 우측 상단에 배치
        self.update_idletasks()
        sw = self.winfo_screenwidth()
        self.geometry(f"+{sw - 280}+20")

    def _build(self):
        bg = "#1e3a5f"
        self.configure(bg=bg)

        header = tk.Frame(self, bg=bg, cursor="fleur")
        header.pack(fill="x", padx=2, pady=(4, 0))
        tk.Label(header, text="📋 EMR 캡처 바  (드래그로 이동)",
                 bg=bg, fg="white", font=("맑은 고딕", 8)).pack(side="left", padx=6)
        header.bind("<ButtonPress-1>",   self._drag_start)
        header.bind("<B1-Motion>",       self._drag_move)

        body = tk.Frame(self, bg=bg)
        body.pack(padx=6, pady=6)

        self._count_lbl = tk.Label(body, text="캡처: 0장",
                                   bg=bg, fg="#7dd3fc",
                                   font=("맑은 고딕", 9, "bold"))
        self._count_lbl.pack(pady=(0, 6))

        tk.Label(body, text="① NGTMediPlus에서 메뉴 항목 클릭\n② 내용이 바뀌면 아래 버튼 클릭",
                 bg=bg, fg="#cbd5e1", font=("맑은 고딕", 8),
                 justify="left").pack(pady=(0, 8))

        btn_f = tk.Frame(body, bg=bg)
        btn_f.pack(fill="x")

        cap_btn = tk.Button(btn_f, text="📷  지금 캡처",
                            bg="#2563eb", fg="white",
                            font=("맑은 고딕", 10, "bold"),
                            relief="flat", padx=10, pady=6,
                            command=self._capture)
        cap_btn.pack(fill="x", pady=(0, 4))

        done_btn = tk.Button(btn_f, text="✅  완료 → AI 요약",
                             bg="#16a34a", fg="white",
                             font=("맑은 고딕", 9, "bold"),
                             relief="flat", padx=10, pady=5,
                             command=self._done)
        done_btn.pack(fill="x")

    def _drag_start(self, e):
        self._drag_x = e.x_root - self.winfo_x()
        self._drag_y = e.y_root - self.winfo_y()

    def _drag_move(self, e):
        self.geometry(f"+{e.x_root - self._drag_x}+{e.y_root - self._drag_y}")

    def _capture(self):
        self._on_capture()
        self._count += 1
        self._count_lbl.config(text=f"캡처: {self._count}장")

    def _done(self):
        self.destroy()
        self._on_done()


# ══════════════════════════════════════════════════════════════════════════════
# 수집 진행 다이얼로그
# ══════════════════════════════════════════════════════════════════════════════

class CollectProgressDialog(tk.Toplevel):
    THUMB_W, THUMB_H = 320, 200

    def __init__(self, parent, total: int):
        super().__init__(parent)
        self.title("자동 수집 진행 중")
        self.geometry("400x380")
        self.resizable(False, False)
        self.protocol("WM_DELETE_WINDOW", lambda: None)  # 닫기 버튼 비활성
        self._total = total
        self._photo = None
        self._build()

    def _build(self):
        ttk.Label(self, text="EMR 화면 자동 수집 중입니다...",
                  font=("맑은 고딕", 10, "bold")).pack(pady=(14, 4))

        # 프로그레스 바
        bar_frame = ttk.Frame(self)
        bar_frame.pack(fill="x", padx=20, pady=4)
        self._pbar = ttk.Progressbar(bar_frame, maximum=self._total,
                                     mode="determinate", length=340)
        self._pbar.pack(side="left")

        self._count_var = tk.StringVar(value=f"0 / {self._total}")
        ttk.Label(bar_frame, textvariable=self._count_var,
                  width=8).pack(side="left", padx=6)

        # 현재 항목명
        self._item_var = tk.StringVar(value="준비 중...")
        ttk.Label(self, textvariable=self._item_var,
                  font=("맑은 고딕", 9), foreground="#1d4ed8",
                  wraplength=360).pack(pady=(2, 8))

        # 캡처 미리보기
        preview_frame = ttk.LabelFrame(self, text="최근 캡처 화면")
        preview_frame.pack(padx=16, fill="x")
        self._preview_lbl = ttk.Label(preview_frame,
                                      text="(캡처 대기 중)",
                                      foreground="gray",
                                      font=("맑은 고딕", 9))
        self._preview_lbl.pack(padx=8, pady=8)

        # 상태 텍스트
        self._status_var = tk.StringVar(value="")
        ttk.Label(self, textvariable=self._status_var,
                  foreground="gray", font=("맑은 고딕", 8),
                  wraplength=360).pack(pady=4)

    def update_item(self, idx: int, name: str, img: Image.Image):
        """캡처 완료 시 UI 갱신 (메인 스레드에서 호출)"""
        self._pbar["value"] = idx
        self._count_var.set(f"{idx} / {self._total}")
        self._item_var.set(f"완료: {name}")
        self._status_var.set(f"다음 항목 이동 중...")

        # 썸네일 갱신
        thumb = img.copy()
        thumb.thumbnail((self.THUMB_W, self.THUMB_H))
        self._photo = ImageTk.PhotoImage(thumb)
        self._preview_lbl.config(image=self._photo, text="")

    def set_status(self, msg: str):
        self._status_var.set(msg)
        self._item_var.set(msg)


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
# CT / X-ray 영상 임상 리딩 창
# ══════════════════════════════════════════════════════════════════════════════

class ImageReadingDialog(tk.Toplevel):
    """CT·X-ray·일반 영상 파일을 불러와 AI 임상 리딩을 수행하는 독립 창."""

    _SUPPORTED = (
        ("이미지 파일",
         "*.png *.jpg *.jpeg *.bmp *.tif *.tiff *.webp"),
        ("모든 파일", "*.*"),
    )

    def __init__(self, parent, settings: dict):
        super().__init__(parent)
        self.title("🩻  영상 임상 리딩 (CT / X-ray / 일반 영상)")
        self.geometry("960x760")
        self.minsize(700, 560)
        self.grab_set()  # 모달

        self._settings = settings
        self._images: list[tuple[str, Image.Image]] = []   # (파일명, PIL Image)
        self._thumb_imgs: list[ImageTk.PhotoImage] = []    # GC 방지용 참조

        self._build()

    # ── UI ───────────────────────────────────────────────────────────────────

    def _build(self):
        # 상단 안내 + 버튼
        top = ttk.Frame(self)
        top.pack(fill="x", padx=12, pady=8)

        ttk.Label(top, text="CT · X-ray · 일반 영상 파일을 불러와 AI 임상 리딩",
                  font=("맑은 고딕", 10, "bold")).pack(side="left")

        self._read_btn = ttk.Button(
            top, text="🔬  AI 임상 리딩 시작", width=20,
            command=self._run_reading)
        self._read_btn.pack(side="right", ipady=5)

        # ── 파일 추가 버튼 영역 ──
        file_row = ttk.Frame(self)
        file_row.pack(fill="x", padx=12, pady=(0, 4))

        ttk.Button(file_row, text="📂  파일 추가", width=14,
                   command=self._add_files).pack(side="left", ipady=4)
        ttk.Button(file_row, text="🗑  목록 지우기", width=14,
                   command=self._clear_images).pack(side="left", padx=6, ipady=4)
        self._file_count_lbl = ttk.Label(
            file_row, text="파일 없음", foreground="#6b7280")
        self._file_count_lbl.pack(side="left", padx=8)

        # ── 썸네일 갤러리 ──
        gal_frame = ttk.LabelFrame(self, text="불러온 영상")
        gal_frame.pack(fill="x", padx=12, pady=(0, 6))

        self._gal_canvas = tk.Canvas(
            gal_frame, height=130, highlightthickness=0)
        gal_sx = ttk.Scrollbar(gal_frame, orient="horizontal",
                               command=self._gal_canvas.xview)
        self._gal_canvas.configure(xscrollcommand=gal_sx.set)
        gal_sx.pack(side="bottom", fill="x")
        self._gal_canvas.pack(fill="x", padx=4, pady=4)
        self._gal_inner = ttk.Frame(self._gal_canvas)
        self._gal_canvas.create_window((0, 0), window=self._gal_inner, anchor="nw")
        self._gal_inner.bind(
            "<Configure>",
            lambda e: self._gal_canvas.configure(
                scrollregion=self._gal_canvas.bbox("all")))

        self._gal_empty = ttk.Label(
            self._gal_inner,
            text="📂 파일 추가 버튼으로 CT / X-ray 이미지를 불러오세요.",
            foreground="gray", font=("맑은 고딕", 9))

        # ── 임상 정보 입력 ──
        clin_frame = ttk.LabelFrame(self, text="임상 정보 입력 (선택 — 입력할수록 리딩 정확도 향상)")
        clin_frame.pack(fill="x", padx=12, pady=(0, 6))

        hint = ("예) 환자 60세 남성, 당뇨·고혈압 병력.  주호소: 호흡 곤란 3일.  "
                "혈압 150/90, SpO₂ 92%.  WBC 12,000.  흉부 PA 및 좌측 Lat 촬영.")
        self._clin_hint = hint
        self._clin_txt = tk.Text(
            clin_frame, height=4, font=("맑은 고딕", 10),
            wrap="word", relief="flat",
            background="#f8fafc", foreground="#374151")
        self._clin_txt.insert("1.0", hint)
        self._clin_txt.config(foreground="#9ca3af")   # placeholder 색
        self._clin_txt.pack(fill="x", padx=6, pady=6)

        # placeholder 동작
        self._clin_txt.bind("<FocusIn>",  self._clin_focus_in)
        self._clin_txt.bind("<FocusOut>", self._clin_focus_out)

        # ── 리딩 결과 ──
        res_frame = ttk.LabelFrame(self, text="AI 임상 리딩 결과")
        res_frame.pack(fill="both", expand=True, padx=12, pady=(0, 8))

        res_btn_row = ttk.Frame(res_frame)
        res_btn_row.pack(fill="x", padx=6, pady=(4, 0))
        self._res_copy_btn = ttk.Button(
            res_btn_row, text="📋  전체 복사", width=14,
            command=self._copy_result)
        self._res_copy_btn.pack(side="right", padx=4)
        ttk.Label(res_btn_row, text="드래그 후 Ctrl+C 로 부분 복사",
                  foreground="#6b7280").pack(side="right", padx=8)
        ttk.Button(res_btn_row, text="🗑  지우기",
                   command=lambda: self._set_result("")).pack(side="right", padx=4)

        self._res_txt = scrolledtext.ScrolledText(
            res_frame, font=("맑은 고딕", 10), wrap="word")
        self._res_txt.pack(fill="both", expand=True, padx=6, pady=6)
        self._res_txt.tag_config(
            "heading", font=("맑은 고딕", 11, "bold"), foreground="#1d4ed8")

        # 편집 차단 (선택·복사는 허용)
        _NAV = {"Up","Down","Left","Right","Home","End","Prior","Next",
                "Shift_L","Shift_R","Control_L","Control_R",
                "Alt_L","Alt_R","Caps_Lock","Escape"}

        def _block(ev):
            if ev.state & 0x4 and ev.keysym.lower() in ("c","a"):
                return
            if ev.keysym in _NAV:
                return
            return "break"

        self._res_txt.bind("<Key>", _block)

        def _smart_copy(ev):
            try:
                sel = self._res_txt.get("sel.first", "sel.last")
                if sel.strip():
                    self.clipboard_clear(); self.clipboard_append(sel)
                    self.update()
                    self._status(f"선택 복사 ({len(sel):,}자)")
                    return "break"
            except tk.TclError:
                pass
            self._copy_result()
            return "break"

        self._res_txt.bind("<Control-c>", _smart_copy)
        self._res_txt.bind("<Control-C>", _smart_copy)

        # 상태바
        sbar = ttk.Frame(self, relief="sunken")
        sbar.pack(fill="x", side="bottom")
        self._status_var = tk.StringVar(value="파일을 불러온 뒤 [AI 임상 리딩 시작]을 눌러주세요.")
        ttk.Label(sbar, textvariable=self._status_var,
                  anchor="w").pack(fill="x", padx=8, pady=2)

        self._refresh_gallery()

    # ── placeholder 처리 ────────────────────────────────────────────────────

    def _clin_focus_in(self, _):
        if self._clin_txt.get("1.0", "end").strip() == self._clin_hint:
            self._clin_txt.delete("1.0", "end")
            self._clin_txt.config(foreground="#374151")

    def _clin_focus_out(self, _):
        if not self._clin_txt.get("1.0", "end").strip():
            self._clin_txt.insert("1.0", self._clin_hint)
            self._clin_txt.config(foreground="#9ca3af")

    def _get_clinical_info(self) -> str:
        txt = self._clin_txt.get("1.0", "end").strip()
        return "" if txt == self._clin_hint else txt

    # ── 파일 관리 ───────────────────────────────────────────────────────────

    def _add_files(self):
        from tkinter import filedialog
        paths = filedialog.askopenfilenames(
            title="CT / X-ray 영상 파일 선택",
            filetypes=self._SUPPORTED,
            parent=self,
        )
        added = 0
        for p in paths:
            try:
                img = Image.open(p).convert("RGB")
                name = os.path.basename(p)
                self._images.append((name, img))
                added += 1
            except Exception as e:
                messagebox.showwarning("파일 오류", f"{os.path.basename(p)}\n{e}",
                                       parent=self)
        if added:
            self._refresh_gallery()
            self._status(f"파일 {added}개 추가 (총 {len(self._images)}개)")

    def _clear_images(self):
        self._images.clear()
        self._thumb_imgs.clear()
        self._refresh_gallery()
        self._status("목록 초기화")

    def _refresh_gallery(self):
        for w in self._gal_inner.winfo_children():
            w.destroy()
        self._thumb_imgs.clear()

        if not self._images:
            self._gal_empty = ttk.Label(
                self._gal_inner,
                text="📂 파일 추가 버튼으로 CT / X-ray 이미지를 불러오세요.",
                foreground="gray", font=("맑은 고딕", 9))
            self._gal_empty.pack(padx=20, pady=40)
            self._file_count_lbl.config(text="파일 없음")
            return

        self._file_count_lbl.config(text=f"영상 {len(self._images)}개 로드됨")
        for idx, (name, img) in enumerate(self._images):
            card = ttk.Frame(self._gal_inner)
            card.pack(side="left", padx=4, pady=4)

            thumb = img.copy()
            thumb.thumbnail((100, 100))
            ph = ImageTk.PhotoImage(thumb)
            self._thumb_imgs.append(ph)

            tk.Label(card, image=ph, relief="ridge", bd=1).pack()
            ttk.Label(card, text=name[:14] + ("…" if len(name) > 14 else ""),
                      font=("맑은 고딕", 7), foreground="#374151").pack()

            # X 삭제 버튼
            ttk.Button(card, text="✕", width=3,
                       command=lambda i=idx: self._remove_image(i)).pack()

        self._gal_canvas.configure(
            scrollregion=self._gal_canvas.bbox("all"))

    def _remove_image(self, idx: int):
        if 0 <= idx < len(self._images):
            self._images.pop(idx)
            self._refresh_gallery()

    # ── AI 리딩 ─────────────────────────────────────────────────────────────

    def _run_reading(self):
        if not self._images:
            messagebox.showwarning("영상 없음",
                                   "먼저 [📂 파일 추가]로 영상을 불러오세요.",
                                   parent=self)
            return
        api_key = self._settings.get("claude_api_key", "")
        if not api_key:
            messagebox.showerror("API 키 없음",
                                 "메인 창 [⚙ 설정]에서 Claude API 키를 입력하세요.",
                                 parent=self)
            return

        self._read_btn.config(state="disabled")
        clin = self._get_clinical_info()
        n = len(self._images)
        self._set_result(
            f"영상 {n}장을 AI가 임상 분석 중입니다...\n\n잠시 기다려 주세요.")
        self._status(f"영상 {n}장 분석 중...")

        def _worker():
            try:
                result = _read_images(
                    self._images,
                    clinical_info=clin,
                    api_key=api_key,
                    model=self._settings.get(
                        "claude_model", "claude-sonnet-4-6"),
                )
                self.after(0, lambda r=result: (
                    self._set_result(r),
                    self._status(f"임상 리딩 완료 — 영상 {n}장"),
                ))
            except Exception as e:
                self.after(0, lambda err=e: (
                    self._status(f"오류: {err}"),
                    messagebox.showerror("리딩 오류", str(err), parent=self),
                    self._set_result(""),
                ))
            finally:
                self.after(0, lambda: self._read_btn.config(state="normal"))

        threading.Thread(target=_worker, daemon=True).start()

    # ── 결과 표시 ───────────────────────────────────────────────────────────

    def _set_result(self, text: str):
        self._res_txt.delete("1.0", "end")
        for line in text.splitlines(keepends=True):
            tag = "heading" if line.startswith("## ") else ""
            self._res_txt.insert("end", line, tag)

    def _copy_result(self):
        text = self._res_txt.get("1.0", "end").strip()
        if not text:
            self._status("복사할 내용이 없습니다.")
            return
        self.clipboard_clear()
        self.clipboard_append(text)
        self.update()
        self._status(f"전체 복사 완료 ({len(text):,}자)")
        self._res_copy_btn.config(text="✅  복사됨")
        self.after(1500, lambda: self._res_copy_btn.config(text="📋  전체 복사"))

    def _status(self, msg: str):
        self._status_var.set(msg)


# ── 영상 임상 리딩 AI 함수 ────────────────────────────────────────────────────

def _read_images(
    images: list[tuple[str, Image.Image]],
    clinical_info: str,
    api_key: str,
    model: str,
) -> str:
    """CT / X-ray 영상을 Claude Vision으로 임상 분석."""
    import base64, io as _io
    import anthropic as _ant

    def _b64(img: Image.Image) -> str:
        buf = _io.BytesIO()
        resized = img.copy()
        resized.thumbnail((1280, 1280), Image.LANCZOS)
        resized.save(buf, format="PNG")
        return base64.standard_b64encode(buf.getvalue()).decode()

    content: list[dict] = []

    if clinical_info:
        content.append({
            "type": "text",
            "text": f"## 임상 정보 (판독 참고)\n{clinical_info}\n",
        })

    for fname, img in images:
        content.append({"type": "text", "text": f"[영상: {fname}]"})
        content.append({
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": "image/png",
                "data": _b64(img),
            },
        })

    content.append({"type": "text", "text": _READING_PROMPT})

    client = _ant.Anthropic(api_key=api_key)
    msg = client.messages.create(
        model=model,
        max_tokens=2500,
        system=_READING_SYSTEM,
        messages=[{"role": "user", "content": content}],
    )
    return msg.content[0].text


_READING_SYSTEM = """당신은 경험 많은 영상의학과 전문의입니다.
CT, X-ray, MRI, 초음파 등 의료 영상을 임상적으로 판독하고
한국어로 구조화된 판독 소견을 작성합니다.
영상의학적으로 정확한 용어를 사용하되, 임상의가 바로 활용할 수 있도록
명확하고 간결하게 기술합니다."""

_READING_PROMPT = """위 의료 영상(들)을 임상적으로 판독해 주세요.
임상 정보가 제공된 경우 함께 고려하여 판독하세요.

다음 형식으로 작성하세요:

## 영상 종류 및 촬영 부위
(CT/X-ray/MRI 여부, 촬영 부위, 방향)

## 주요 소견
(영상에서 관찰되는 이상 소견 — 위치, 크기, 성상 포함)

## 정상 소견
(주요 해부학적 구조물 중 정상인 항목)

## 감별 진단
(소견에 근거한 가능성 높은 진단 순서대로)

## 임상적 권고
(추가 검사, 추적 관찰, 치료 방향 제안)

## 종합 판독 소견
(핵심 소견 2~3줄 요약)

※ 이 판독은 AI에 의한 참고용이며, 최종 판단은 반드시 전문의가 확인해야 합니다."""


# ══════════════════════════════════════════════════════════════════════════════
# 메인 앱
# ══════════════════════════════════════════════════════════════════════════════

class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("AI 진료기록 요약")
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

        self._seq_btn = ttk.Button(
            top, text="🖱  순차 캡처 모드",
            command=self._start_sequential, width=16)
        self._seq_btn.pack(side="left", padx=(4, 0), ipady=6)

        self._summary_btn = ttk.Button(
            top, text="🤖  요약",
            command=self._run_summary, width=10)
        self._summary_btn.pack(side="left", padx=6, ipady=6)

        self._brief_btn = ttk.Button(
            top, text="⚡  초간단 요약",
            command=self._run_brief_summary, width=14)
        self._brief_btn.pack(side="left", padx=(0, 6), ipady=6)

        ttk.Separator(top, orient="vertical").pack(
            side="left", fill="y", padx=8, pady=4)

        ttk.Button(top, text="🩻  영상 리딩",
                   command=self._open_image_reading, width=13).pack(
            side="left", ipady=6)

        ttk.Button(top, text="🗑  초기화",
                   command=self._reset, width=10).pack(side="left", padx=6, ipady=6)

        ttk.Button(top, text="⚙  설정",
                   command=self._open_settings, width=8).pack(
            side="left", ipady=6)

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
        self._copy_btn = ttk.Button(
            btn_row, text="📋  전체 복사", width=14, command=self._copy)
        self._copy_btn.pack(side="right", padx=4)
        ttk.Label(btn_row, text="드래그로 부분 선택 후 Ctrl+C",
                  foreground="#6b7280").pack(side="right", padx=8)
        ttk.Button(btn_row, text="🗑  지우기",
                   command=self._clear_result).pack(side="right", padx=4)

        self._result_txt = scrolledtext.ScrolledText(
            result_frame, font=("맑은 고딕", 10), wrap="word")
        self._result_txt.pack(fill="both", expand=True, padx=6, pady=6)
        self._result_txt.tag_config(
            "heading", font=("맑은 고딕", 11, "bold"), foreground="#1d4ed8")

        # 편집 방지: 내비게이션·선택·복사 키만 허용, 나머지는 차단
        _ALLOW_KEYS = {
            "Up", "Down", "Left", "Right", "Home", "End",
            "Prior", "Next",          # Page Up / Page Down
            "Shift_L", "Shift_R",
            "Control_L", "Control_R",
            "Alt_L", "Alt_R",
            "Caps_Lock", "Escape", "F1", "F2", "F3", "F4",
        }

        def _block_edit(event):
            ctrl = event.state & 0x4
            if ctrl and event.keysym.lower() in ("c", "a"):
                return          # Ctrl+C / Ctrl+A 허용 (기본 처리로 넘김)
            if event.keysym in _ALLOW_KEYS:
                return          # 방향키·페이지키 허용
            return "break"      # 그 외 모든 키 입력 차단

        self._result_txt.bind("<Key>", _block_edit)

        # Ctrl+C: 선택 영역이 있으면 선택만, 없으면 전체 복사
        def _smart_copy(event):
            try:
                sel = self._result_txt.get("sel.first", "sel.last")
                if sel.strip():
                    self.clipboard_clear()
                    self.clipboard_append(sel)
                    self.update()
                    self._status(f"선택 텍스트 복사 ({len(sel):,}자)")
                    return "break"
            except tk.TclError:
                pass
            self._copy()
            return "break"

        self._result_txt.bind("<Control-c>", _smart_copy)
        self._result_txt.bind("<Control-C>", _smart_copy)

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
                if found:
                    hwnd, title = found
                    # 자동 발견됐어도 확인 후 진행
                    self.after(0, lambda h=hwnd, t=title: self._confirm_window_and_auto(h, t))
                else:
                    self.after(0, self._pick_window_and_auto)
                    self.after(0, self._enable_auto_btn)
            except Exception as e:
                self.after(0, lambda err=e: (
                    self._status(f"창 탐색 오류: {err}"),
                    messagebox.showerror("오류", f"창 탐색 중 오류:\n{err}"),
                    self._enable_auto_btn(),
                ))

        threading.Thread(target=_run, daemon=True).start()

    def _confirm_window_and_auto(self, hwnd: int, title: str):
        from tkinter import messagebox as mb
        ok = mb.askyesno(
            "창 확인",
            f"아래 창에서 EMR 정보를 수집합니다.\n\n"
            f"  {title}\n\n"
            f"맞으면 [예], 다른 창을 선택하려면 [아니오]를 클릭하세요.")
        if ok:
            self._run_auto_with_hwnd(hwnd)
        else:
            self._pick_window_and_auto()
            self._enable_auto_btn()

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
        self._status("EMR 화면 분석 시작...")

        api_key = self._settings["claude_api_key"]
        model   = self._settings.get("claude_model", "claude-sonnet-4-6")

        def _analyze():
            try:
                # 앱 최소화 → EMR 창 전면
                self.after(0, self.iconify)
                import time as _t; _t.sleep(0.3)

                def _status_cb(msg):
                    self.after(0, lambda m=msg: self._status(m))

                # 빠른 단일 패스 탐색: Vision 1회로 왼쪽 메뉴 항목 추출
                items = auto_navigate.discover_items_fast(
                    hwnd, api_key=api_key, model=model,
                    status_cb=_status_cb)

                self.after(0, self.deiconify)

                if not items:
                    fallback = auto_navigate.capture_screen(hwnd)
                    self.after(0, lambda s=fallback: (
                        self._captures.append(("현재 화면", s)),
                        self._refresh_gallery(),
                        self._status("서브메뉴 미발견 — 현재 화면으로 요약합니다."),
                        self._enable_auto_btn(),
                        self._run_summary(),
                    ))
                    return

                self.after(0, lambda it=items, h=hwnd:
                           self._confirm_and_collect(it, h))

            except Exception as e:
                self.after(0, lambda err=e: (
                    self.deiconify(),
                    self._status(f"분석 오류: {err}"),
                    messagebox.showerror("오류", f"화면 분석 중 오류:\n{err}"),
                    self._enable_auto_btn(),
                ))

        threading.Thread(target=_analyze, daemon=True).start()

    def _confirm_and_collect(self, tabs: list[dict], hwnd: int, method: str = ""):
        label = f"수집 항목 선택 ({method})" if method else "수집 항목 선택"
        TabConfirmDialog(
            self, tabs, title=label,
            on_confirm=lambda selected: self._do_collect(selected, hwnd))

    def _do_collect(self, items: list[dict], hwnd: int):
        self._auto_btn.config(state="disabled")
        self._add_btn.config(state="disabled")

        prog_dlg = CollectProgressDialog(self, total=len(items))
        api_key = self._settings["claude_api_key"]
        model   = self._settings.get("claude_model", "claude-sonnet-4-6")

        def _run():
            import time as _t
            try:
                # 모든 창 숨기기 — EMR 창만 화면에 남음
                self.after(0, self.iconify)
                self.after(0, prog_dlg.withdraw)
                _t.sleep(0.3)

                results = auto_navigate.collect_menu_items(
                    hwnd, items,
                    api_key=api_key,
                    model=model,
                    status_cb=lambda m: self.after(0, lambda msg=m: self._status(msg)),
                    item_cb=lambda idx, total, name, img: (
                        self.after(0, lambda i=idx, n=name, im=img: (
                            self._captures.append((n, im)),
                            self._refresh_gallery(),
                            prog_dlg.deiconify(),
                            prog_dlg.update_item(i, n, im),
                        )),
                        _t.sleep(0.2),
                        self.after(0, prog_dlg.withdraw),
                    ),
                )

                self.after(0, lambda: (
                    prog_dlg.destroy(),
                    self.deiconify(),
                    self._on_collected_noduplicate(results),
                ))
            except Exception as e:
                import traceback as _tb
                detail = _tb.format_exc()
                self.after(0, lambda err=e, d=detail: (
                    prog_dlg.destroy(),
                    self.deiconify(),
                    self._status(f"수집 오류: {err}"),
                    messagebox.showerror(
                        "수집 오류 상세",
                        f"{err}\n\n── 상세 ──\n{d[-800:]}"),
                    self._enable_auto_btn(),
                    self._add_btn.config(state="normal"),
                ))

        threading.Thread(target=_run, daemon=True).start()

    def _on_collected(self, results: list[tuple[str, Image.Image]]):
        self._captures.extend(results)
        self._refresh_gallery()
        self._auto_btn.config(state="normal")
        self._add_btn.config(state="normal")
        self._status(f"자동 수집 완료 ({len(results)}개) — [요약] 버튼을 클릭하세요.")
        self._run_summary()

    def _on_collected_noduplicate(self, results: list[tuple[str, Image.Image]]):
        """_do_collect에서 실시간으로 이미 _captures에 추가됐으므로 중복 방지"""
        self._auto_btn.config(state="normal")
        self._add_btn.config(state="normal")
        self._refresh_gallery()
        self._status(f"자동 수집 완료 ({len(results)}개) — AI 요약 시작 중...")
        self._run_summary()

    # ── 순차 캡처 모드 (사용자가 메뉴 클릭 → 캡처 버튼) ─────────────────────────

    def _start_sequential(self):
        """
        플로팅 캡처 바를 띄운다.
        사용자가 NGTMediPlus 메뉴 항목을 직접 클릭 후 [📷 지금 캡처] 누름.
        """
        self._seq_btn.config(state="disabled")

        def _do_capture():
            import time as _t
            import win32gui as _wg
            from PIL import ImageGrab

            # 현재 포그라운드 창 제목으로 제외 여부 확인
            try:
                fg = _wg.GetForegroundWindow()
                title = _wg.GetWindowText(fg)
            except Exception:
                title = ""

            if auto_navigate.is_excluded(title):
                self._status(f"제외 항목 건너뜀: {title}")
                bar.deiconify()
                return

            bar.withdraw()
            _t.sleep(0.1)
            img = ImageGrab.grab()
            bar.deiconify()
            n = len(self._captures) + 1
            self._captures.append((f"캡처 {n}", img))
            self._refresh_gallery()
            self._status(f"{n}장 캡처 완료 — 다음 항목 클릭 후 다시 [📷 지금 캡처]")

        def _do_done():
            self._seq_btn.config(state="normal")
            if self._captures:
                self._status(f"총 {len(self._captures)}장 수집 완료 — AI 요약 시작")
                self._run_summary()
            else:
                self._status("캡처된 화면이 없습니다.")

        bar = FloatingCaptureBar(self, on_capture=_do_capture, on_done=_do_done)
        self._status("순차 캡처 모드: NGTMediPlus 메뉴 클릭 → [📷 지금 캡처] 반복 → [✅ 완료]")

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

    def _run_brief_summary(self):
        """A4 2/3 분량 초간단 요약본 생성."""
        self._run_summary(brief=True)

    def _run_summary(self, brief: bool = False):
        if not self._captures:
            messagebox.showwarning("수집 없음", "먼저 화면을 수집해주세요.")
            return

        self._auto_btn.config(state="disabled")
        self._add_btn.config(state="disabled")
        self._summary_btn.config(state="disabled")
        self._brief_btn.config(state="disabled")
        n = len(self._captures)
        kind = "초간단 요약" if brief else "종합 요약"
        self._set_result(
            f"화면 {n}장으로 {kind}을 AI가 작성 중입니다...\n\n잠시 기다려 주세요.")
        self._status(f"화면 {n}장 {kind} 작성 중...")

        def _run():
            try:
                result = summarizer.summarize_images(
                    self._captures,
                    api_key=self._settings["claude_api_key"],
                    model=self._settings.get("claude_model", "claude-sonnet-4-6"),
                    brief=brief,
                )
                self.after(0, lambda r=result: self._display_result(r, brief))
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
                    self._brief_btn.config(state="normal"),
                ))

        threading.Thread(target=_run, daemon=True).start()

    def _display_result(self, text: str, brief: bool = False):
        self._set_result(text)
        kind = "초간단 요약" if brief else "요약"
        self._status(f"{kind} 완료 — 화면 {len(self._captures)}장 분석")

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

    def _open_image_reading(self):
        ImageReadingDialog(self, self._settings)

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
        self._result_txt.delete("1.0", "end")
        for line in text.splitlines(keepends=True):
            tag = "heading" if line.startswith("## ") else ""
            self._result_txt.insert("end", line, tag)

    def _copy(self):
        """전체 요약 클립보드 복사."""
        text = self._result_txt.get("1.0", "end").strip()
        if not text:
            self._status("복사할 요약 내용이 없습니다.")
            return
        self.clipboard_clear()
        self.clipboard_append(text)
        self.update()   # 클립보드 내용 확정 (창 닫혀도 유지)
        self._status(f"전체 복사 완료 ({len(text):,}자). Ctrl+V로 붙여넣기.")

        # 버튼에 잠시 확인 표시 후 원래대로 복원
        self._copy_btn.config(text="✅  복사됨")
        self.after(1500, lambda: self._copy_btn.config(text="📋  전체 복사"))

    def _clear_result(self):
        self._set_result("")


def _check_admin():
    """관리자 권한 없으면 경고 (클릭이 안 될 수 있음)"""
    import ctypes as _ct
    try:
        if not _ct.windll.shell32.IsUserAnAdmin():
            import tkinter as _tk
            from tkinter import messagebox as _mb
            _root = _tk.Tk(); _root.withdraw()
            _mb.showwarning(
                "관리자 권한 필요",
                "현재 일반 권한으로 실행 중입니다.\n\n"
                "NGTMediPlus가 관리자 권한으로 실행 중이면\n"
                "자동 클릭이 차단될 수 있습니다.\n\n"
                "해결: C:\\emr\\emr_summarizer\\실행.bat 으로 실행하세요.\n"
                "(UAC 창에서 [예] 클릭 필요)",
                parent=_root,
            )
            _root.destroy()
    except Exception:
        pass


if __name__ == "__main__":
    _check_admin()
    app = App()
    app.mainloop()
