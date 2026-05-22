"""
설정 관리 — .env 파일 또는 settings.json 에서 로드
"""

import json
import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

_SETTINGS_FILE = Path(__file__).parent / "settings.json"

_DEFAULTS = {
    # ── DB 연결 ────────────────────────────────────────────────────────────────
    "db_server":   os.getenv("DB_SERVER", "localhost"),
    "db_name":     os.getenv("DB_NAME", "NGTMEDI"),
    "db_user":     os.getenv("DB_USER", "sa"),
    "db_password": os.getenv("DB_PASSWORD", ""),
    "db_driver":   os.getenv("DB_DRIVER", "ODBC Driver 17 for SQL Server"),

    # ── Claude API ────────────────────────────────────────────────────────────
    "claude_api_key": os.getenv("CLAUDE_API_KEY", ""),
    "claude_model":   os.getenv("CLAUDE_MODEL", "claude-sonnet-4-6"),

    # ── SQL 쿼리 (NGTMediPlus 실제 테이블명에 맞게 수정 필요) ──────────────────
    "sql_patient": (
        "SELECT PT_NO, PT_NM, BIRTH_DT, SEX_CD, TEL_NO "
        "FROM TB_PT_INFO WHERE PT_NO = ?"
    ),
    "sql_visits": (
        "SELECT TOP 10 RCPT_DT, CHIEF_COMPLAINT, DOCTOR_NM, DEPT_NM "
        "FROM TB_VISIT_MST WHERE PT_NO = ? ORDER BY RCPT_DT DESC"
    ),
    "sql_diagnoses": (
        "SELECT TOP 20 DIAG_DT, DIAG_CD, DIAG_NM "
        "FROM TB_DIAGNOSIS WHERE PT_NO = ? ORDER BY DIAG_DT DESC"
    ),
    "sql_prescriptions": (
        "SELECT TOP 30 ORD_DT, DRUG_NM, DOSE_AMT, DOSE_UNIT, FREQ_CD, DAYS_CNT "
        "FROM TB_ORDER_DRUG WHERE PT_NO = ? ORDER BY ORD_DT DESC"
    ),
    "sql_labs": (
        "SELECT TOP 50 EXAM_DT, EXAM_NM, RESULT_VAL, UNIT, REF_RANGE, ABNORMAL_YN "
        "FROM TB_LAB_RESULT WHERE PT_NO = ? ORDER BY EXAM_DT DESC"
    ),
}


def load() -> dict:
    """settings.json → 없으면 기본값 반환"""
    if _SETTINGS_FILE.exists():
        try:
            saved = json.loads(_SETTINGS_FILE.read_text(encoding="utf-8"))
            merged = dict(_DEFAULTS)
            merged.update(saved)
            return merged
        except Exception:
            pass
    return dict(_DEFAULTS)


def save(settings: dict):
    """settings.json 에 저장 (비밀번호 포함 — 로컬 전용)"""
    _SETTINGS_FILE.write_text(
        json.dumps(settings, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
