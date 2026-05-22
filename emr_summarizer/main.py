"""
NGTMediPlus EMR 챠트 자동 요약 프로그램
실행: emr_summarizer 폴더 안에서 → python main.py
      또는 어느 위치에서든 → 실행.bat 더블클릭 (Windows)
"""

from __future__ import annotations

import sys
import os

# emr_summarizer 폴더를 import 경로에 추가 (어느 디렉터리에서 실행해도 동작)
_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

# 필수 패키지 확인
_MISSING = []
for _pkg, _import in [("anthropic", "anthropic"), ("pyodbc", "pyodbc"), ("python-dotenv", "dotenv")]:
    try:
        __import__(_import)
    except ImportError:
        _MISSING.append(_pkg)

if _MISSING:
    print("=" * 60)
    print("[오류] 다음 패키지가 설치되어 있지 않습니다:")
    for p in _MISSING:
        print(f"  - {p}")
    print()
    print("아래 명령어로 설치 후 다시 실행하세요:")
    print(f"  pip install -r {os.path.join(_HERE, 'requirements.txt')}")
    print("=" * 60)
    input("\nEnter 키를 누르면 종료합니다...")
    sys.exit(1)

import threading
import tkinter as tk
from tkinter import messagebox, scrolledtext, ttk

import config
import db
import summarizer


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
        self._db_vars: dict[str, tk.StringVar] = {}
        self._ai_vars: dict[str, tk.StringVar] = {}
        self._sql_vars: dict[str, tk.Text] = {}
        self._build()

    def _build(self):
        nb = ttk.Notebook(self)
        nb.pack(fill="both", expand=True, padx=10, pady=10)

        db_frame = ttk.Frame(nb)
        nb.add(db_frame, text="  데이터베이스  ")
        self._build_form(db_frame, self._db_vars, [
            ("서버 주소",     "db_server",    False),
            ("DB 이름",       "db_name",      False),
            ("사용자명",      "db_user",      False),
            ("비밀번호",      "db_password",  True),
            ("ODBC 드라이버", "db_driver",    False),
        ])

        ai_frame = ttk.Frame(nb)
        nb.add(ai_frame, text="  Claude API  ")
        self._build_form(ai_frame, self._ai_vars, [
            ("API 키", "claude_api_key", True),
            ("모델",   "claude_model",   False),
        ])

        sql_frame = ttk.Frame(nb)
        nb.add(sql_frame, text="  SQL 쿼리  ")
        self._build_sql_form(sql_frame)

        btn_frame = ttk.Frame(self)
        btn_frame.pack(fill="x", padx=10, pady=(0, 10))
        ttk.Button(btn_frame, text="저장", command=self._save).pack(side="right", padx=4)
        ttk.Button(btn_frame, text="취소", command=self.destroy).pack(side="right")

    def _build_form(self, parent, target_dict, fields):
        for i, (label, key, secret) in enumerate(fields):
            ttk.Label(parent, text=label + ":").grid(
                row=i, column=0, sticky="w", padx=12, pady=6)
            var = tk.StringVar(value=self._settings.get(key, ""))
            ttk.Entry(parent, textvariable=var, width=46,
                      show=("*" if secret else "")).grid(
                row=i, column=1, padx=8, pady=4, sticky="w")
            target_dict[key] = var

    def _build_sql_form(self, parent):
        fields = [
            ("환자 기본정보", "sql_patient"),
            ("진료기록",      "sql_visits"),
            ("진단명",        "sql_diagnoses"),
            ("처방약",        "sql_prescriptions"),
            ("검사결과",      "sql_labs"),
        ]
        for i, (label, key) in enumerate(fields):
            ttk.Label(parent, text=label + ":").grid(
                row=i * 2, column=0, sticky="w", padx=12, pady=(8, 0))
            txt = tk.Text(parent, height=3, width=72, wrap="none",
                          font=("Consolas", 9))
            txt.insert("1.0", self._settings.get(key, ""))
            txt.grid(row=i * 2 + 1, column=0, columnspan=2,
                     padx=12, pady=(0, 4), sticky="ew")
            self._sql_vars[key] = txt
        parent.columnconfigure(0, weight=1)

    def _save(self):
        s = dict(self._settings)
        for key, var in {**self._db_vars, **self._ai_vars}.items():
            s[key] = var.get().strip()
        for key, txt in self._sql_vars.items():
            s[key] = txt.get("1.0", "end").strip()
        config.save(s)
        self._on_save(s)
        self.destroy()
        messagebox.showinfo("저장 완료", "설정이 저장되었습니다.")


# ══════════════════════════════════════════════════════════════════════════════
# 스키마 탐색기 다이얼로그
# ══════════════════════════════════════════════════════════════════════════════

class SchemaDialog(tk.Toplevel):
    def __init__(self, parent):
        super().__init__(parent)
        self.title("테이블 탐색기")
        self.geometry("780x520")
        self.grab_set()
        self._build()
        self._load_tables()

    def _build(self):
        paned = ttk.PanedWindow(self, orient="horizontal")
        paned.pack(fill="both", expand=True, padx=8, pady=8)

        left = ttk.Frame(paned, width=220)
        paned.add(left, weight=1)
        ttk.Label(left, text="테이블 목록").pack(anchor="w", padx=4)
        self._table_lb = tk.Listbox(left, exportselection=False)
        self._table_lb.pack(fill="both", expand=True, padx=4, pady=4)
        self._table_lb.bind("<<ListboxSelect>>", self._on_select)

        right = ttk.Frame(paned)
        paned.add(right, weight=3)
        ttk.Label(right, text="컬럼 / 데이터 미리보기").pack(anchor="w", padx=4)
        self._preview = scrolledtext.ScrolledText(
            right, font=("Consolas", 9), state="disabled")
        self._preview.pack(fill="both", expand=True, padx=4, pady=4)

    def _load_tables(self):
        try:
            for t in db.list_tables():
                self._table_lb.insert("end", t)
        except Exception as e:
            messagebox.showerror("오류", str(e), parent=self)

    def _on_select(self, _):
        sel = self._table_lb.curselection()
        if not sel:
            return
        table = self._table_lb.get(sel[0])
        try:
            cols = db.list_columns(table)
            headers, rows = db.preview_table(table, limit=5)
        except Exception as e:
            self._set_preview(f"오류: {e}")
            return

        lines = [f"▶ {table}", "", "[ 컬럼 ]"]
        for name, dtype in cols:
            lines.append(f"  {name}  ({dtype})")
        lines += ["", "[ 데이터 미리보기 (상위 5행) ]"]
        if headers:
            lines.append("  " + " | ".join(str(h) for h in headers))
            lines.append("  " + "-" * 60)
            for row in rows:
                lines.append("  " + " | ".join(str(v)[:20] for v in row))
        self._set_preview("\n".join(lines))

    def _set_preview(self, text: str):
        self._preview.config(state="normal")
        self._preview.delete("1.0", "end")
        self._preview.insert("1.0", text)
        self._preview.config(state="disabled")


# ══════════════════════════════════════════════════════════════════════════════
# 메인 앱
# ══════════════════════════════════════════════════════════════════════════════

class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("NGTMediPlus EMR 자동 요약")
        self.geometry("950x720")
        self.minsize(800, 600)

        self._settings = config.load()
        self._chart_data: dict | None = None
        self._current_pt_id: str = ""

        self._build_menu()
        self._build_toolbar()
        self._build_body()
        self._build_statusbar()

        self._try_auto_connect()

    # ── 메뉴 ──────────────────────────────────────────────────────────────────

    def _build_menu(self):
        menubar = tk.Menu(self)
        self.config(menu=menubar)

        file_menu = tk.Menu(menubar, tearoff=0)
        menubar.add_cascade(label="파일", menu=file_menu)
        file_menu.add_command(label="설정", command=self._open_settings)
        file_menu.add_separator()
        file_menu.add_command(label="종료", command=self.destroy)

        db_menu = tk.Menu(menubar, tearoff=0)
        menubar.add_cascade(label="데이터베이스", menu=db_menu)
        db_menu.add_command(label="연결",       command=self._connect_db)
        db_menu.add_command(label="연결 해제",  command=self._disconnect_db)
        db_menu.add_separator()
        db_menu.add_command(label="테이블 탐색기", command=self._open_schema)

    # ── 툴바 ──────────────────────────────────────────────────────────────────

    def _build_toolbar(self):
        toolbar = ttk.Frame(self, relief="flat")
        toolbar.pack(fill="x", padx=8, pady=(8, 0))

        self._db_status_var = tk.StringVar(value="● DB 미연결")
        self._db_status_lbl = ttk.Label(
            toolbar, textvariable=self._db_status_var,
            foreground="gray", font=("", 9, "bold"))
        self._db_status_lbl.pack(side="left", padx=(0, 12))

        ttk.Button(toolbar, text="DB 연결",   command=self._connect_db,    width=10).pack(side="left", padx=2)
        ttk.Button(toolbar, text="설정",      command=self._open_settings, width=8).pack(side="left", padx=2)
        ttk.Button(toolbar, text="테이블 탐색기", command=self._open_schema).pack(side="left", padx=2)

        ttk.Separator(toolbar, orient="vertical").pack(side="left", fill="y", padx=12, pady=4)
        ttk.Label(toolbar, text="환자번호:").pack(side="left")
        self._pt_id_var = tk.StringVar()
        self._pt_entry = ttk.Entry(toolbar, textvariable=self._pt_id_var, width=16)
        self._pt_entry.pack(side="left", padx=6)
        self._pt_entry.bind("<Return>", lambda _: self._load_chart())

        ttk.Button(toolbar, text="챠트 조회", command=self._load_chart, width=10).pack(side="left", padx=2)

        ttk.Separator(toolbar, orient="vertical").pack(side="left", fill="y", padx=12, pady=4)
        self._summarize_btn = ttk.Button(
            toolbar, text="AI 요약", command=self._summarize, width=12)
        self._summarize_btn.pack(side="left", padx=2)

    # ── 본문 ──────────────────────────────────────────────────────────────────

    def _build_body(self):
        paned = ttk.PanedWindow(self, orient="vertical")
        paned.pack(fill="both", expand=True, padx=8, pady=8)

        # 위쪽: 챠트 탭
        chart_frame = ttk.LabelFrame(paned, text="챠트 데이터")
        paned.add(chart_frame, weight=1)

        self._nb = ttk.Notebook(chart_frame)
        self._nb.pack(fill="both", expand=True, padx=4, pady=4)

        self._tab_texts: dict[str, scrolledtext.ScrolledText] = {}
        for label, key in [
            ("기본정보", "info"),
            ("진료기록", "visits"),
            ("진단명",   "diagnoses"),
            ("처방약",   "rx"),
            ("검사결과", "labs"),
        ]:
            frame = ttk.Frame(self._nb)
            self._nb.add(frame, text=f"  {label}  ")
            txt = scrolledtext.ScrolledText(
                frame, font=("Consolas", 9), state="disabled", wrap="none")
            txt.pack(fill="both", expand=True)
            self._tab_texts[key] = txt

        # 아래쪽: 요약
        summary_frame = ttk.LabelFrame(paned, text="AI 요약 결과")
        paned.add(summary_frame, weight=1)

        btn_row = ttk.Frame(summary_frame)
        btn_row.pack(fill="x", padx=4, pady=(4, 0))
        ttk.Button(btn_row, text="복사",   command=self._copy_summary).pack(side="right", padx=4)
        ttk.Button(btn_row, text="지우기", command=self._clear_summary).pack(side="right")

        self._summary_txt = scrolledtext.ScrolledText(
            summary_frame, font=("맑은 고딕", 10), wrap="word", state="disabled")
        self._summary_txt.pack(fill="both", expand=True, padx=4, pady=4)
        self._summary_txt.tag_config(
            "heading", font=("맑은 고딕", 11, "bold"), foreground="#1d4ed8")

    # ── 상태바 ────────────────────────────────────────────────────────────────

    def _build_statusbar(self):
        bar = ttk.Frame(self, relief="sunken")
        bar.pack(fill="x", side="bottom")
        self._status_var = tk.StringVar(value="준비")
        ttk.Label(bar, textvariable=self._status_var,
                  anchor="w").pack(fill="x", padx=8, pady=2)

    # ── DB 연결 ───────────────────────────────────────────────────────────────

    def _try_auto_connect(self):
        if self._settings.get("db_server") and self._settings.get("db_password"):
            self._connect_db()

    def _connect_db(self):
        self._status("DB 연결 중...")
        self.update_idletasks()

        def _run():
            ok, msg = db.connect(self._settings)
            def _done():
                if ok:
                    self._db_status_var.set("● DB 연결됨")
                    self._db_status_lbl.config(foreground="#16a34a")
                    self._status(msg)
                else:
                    self._db_status_var.set("● DB 미연결")
                    self._db_status_lbl.config(foreground="#dc2626")
                    self._status(f"연결 실패: {msg}")
                    messagebox.showerror(
                        "DB 연결 실패",
                        f"{msg}\n\n"
                        "확인사항:\n"
                        "1. [설정]에서 서버 주소/DB명/비밀번호가 맞는지 확인\n"
                        "2. SQL Server가 실행 중인지 확인\n"
                        "3. SQL Server 인증 모드가 활성화됐는지 확인\n"
                        "4. ODBC Driver 17 for SQL Server 설치 여부 확인"
                    )
            self.after(0, _done)

        threading.Thread(target=_run, daemon=True).start()

    def _disconnect_db(self):
        db.disconnect()
        self._db_status_var.set("● DB 미연결")
        self._db_status_lbl.config(foreground="gray")
        self._status("DB 연결 해제")

    # ── 챠트 조회 ─────────────────────────────────────────────────────────────

    def _load_chart(self):
        pt_id = self._pt_id_var.get().strip()
        if not pt_id:
            messagebox.showwarning("입력 오류", "환자번호를 입력하세요.")
            return
        if not db.is_connected():
            messagebox.showerror("연결 오류",
                                 "DB에 연결되어 있지 않습니다.\n[DB 연결] 버튼을 먼저 클릭하세요.")
            return

        self._current_pt_id = pt_id
        self._status(f"환자 {pt_id} 챠트 조회 중...")
        self._clear_all_tabs()
        self._chart_data = None

        def _run():
            try:
                data = db.get_all_chart_data(pt_id)
                self.after(0, lambda: self._display_chart(data))
            except Exception as e:
                self.after(0, lambda err=e: (
                    self._status(f"오류: {err}"),
                    messagebox.showerror("조회 오류", str(err))
                ))

        threading.Thread(target=_run, daemon=True).start()

    def _display_chart(self, data: dict):
        self._chart_data = data
        info = data.get("기본정보") or {}

        if not info:
            self._status("환자를 찾을 수 없습니다.")
            messagebox.showinfo("결과", "해당 환자번호의 데이터가 없습니다.")
            return

        self._set_tab("info",      self._fmt_dict(info))
        self._set_tab("visits",    self._fmt_list(data.get("진료기록", [])))
        self._set_tab("diagnoses", self._fmt_list(data.get("진단명", [])))
        self._set_tab("rx",        self._fmt_list(data.get("처방", [])))
        self._set_tab("labs",      self._fmt_list(data.get("검사결과", [])))

        pt_name = info.get("PT_NM", info.get("이름", self._current_pt_id))
        self._status(f"챠트 로드 완료 — {pt_name} ({self._current_pt_id})")

    # ── AI 요약 ───────────────────────────────────────────────────────────────

    def _summarize(self):
        if not self._chart_data or not self._chart_data.get("기본정보"):
            messagebox.showwarning("데이터 없음", "먼저 챠트를 조회하세요.")
            return
        api_key = self._settings.get("claude_api_key", "")
        if not api_key:
            messagebox.showerror(
                "설정 오류",
                "Claude API 키가 설정되어 있지 않습니다.\n[설정] 메뉴에서 API 키를 입력하세요.")
            return

        self._summarize_btn.config(state="disabled")
        self._status("AI 요약 생성 중...")
        self._set_summary("요약 생성 중...\n\n잠시 기다려 주세요.")

        def _run():
            try:
                result = summarizer.summarize(
                    self._chart_data,
                    api_key=api_key,
                    model=self._settings.get("claude_model", "claude-sonnet-4-6"),
                )
                self.after(0, lambda r=result: self._display_summary(r))
            except Exception as e:
                self.after(0, lambda err=e: (
                    self._status(f"요약 오류: {err}"),
                    messagebox.showerror("요약 오류", str(err)),
                    self._set_summary("")
                ))
            finally:
                self.after(0, lambda: self._summarize_btn.config(state="normal"))

        threading.Thread(target=_run, daemon=True).start()

    def _display_summary(self, text: str):
        self._set_summary(text)
        self._status("AI 요약 완료")

    # ── 유틸 ──────────────────────────────────────────────────────────────────

    def _open_settings(self):
        SettingsDialog(self, self._settings, self._on_settings_saved)

    def _on_settings_saved(self, new_settings: dict):
        self._settings = new_settings

    def _open_schema(self):
        if not db.is_connected():
            messagebox.showerror("연결 오류", "DB에 먼저 연결하세요.")
            return
        SchemaDialog(self)

    def _status(self, msg: str):
        self._status_var.set(msg)

    def _fmt_dict(self, d: dict) -> str:
        return "\n".join(f"{k}: {v}" for k, v in d.items() if v)

    def _fmt_list(self, rows: list[dict]) -> str:
        if not rows:
            return "(데이터 없음)"
        lines = []
        for i, row in enumerate(rows, 1):
            lines.append(f"[{i}]")
            for k, v in row.items():
                if v:
                    lines.append(f"  {k}: {v}")
            lines.append("")
        return "\n".join(lines)

    def _set_tab(self, key: str, text: str):
        txt = self._tab_texts[key]
        txt.config(state="normal")
        txt.delete("1.0", "end")
        txt.insert("1.0", text)
        txt.config(state="disabled")

    def _clear_all_tabs(self):
        for key in self._tab_texts:
            self._set_tab(key, "")

    def _set_summary(self, text: str):
        self._summary_txt.config(state="normal")
        self._summary_txt.delete("1.0", "end")
        for line in text.splitlines(keepends=True):
            tag = "heading" if line.startswith("## ") else ""
            self._summary_txt.insert("end", line, tag)
        self._summary_txt.config(state="disabled")

    def _copy_summary(self):
        text = self._summary_txt.get("1.0", "end").strip()
        if text:
            self.clipboard_clear()
            self.clipboard_append(text)
            self._status("요약 내용이 클립보드에 복사되었습니다.")

    def _clear_summary(self):
        self._set_summary("")


# ── 진입점 ────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    app = App()
    app.mainloop()
