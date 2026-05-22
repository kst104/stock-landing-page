"""
NGTMediPlus SQL Server 연결 및 환자 데이터 조회
"""

from __future__ import annotations

import pyodbc

_conn: pyodbc.Connection | None = None
_settings: dict = {}


# ── 연결 ──────────────────────────────────────────────────────────────────────

def connect(settings: dict) -> tuple[bool, str]:
    """DB 연결 시도. (성공여부, 메시지) 반환"""
    global _conn, _settings
    try:
        conn_str = (
            f"DRIVER={{{settings['db_driver']}}};"
            f"SERVER={settings['db_server']};"
            f"DATABASE={settings['db_name']};"
            f"UID={settings['db_user']};"
            f"PWD={settings['db_password']};"
            "TrustServerCertificate=yes;"
        )
        if _conn:
            try:
                _conn.close()
            except Exception:
                pass
        _conn = pyodbc.connect(conn_str, timeout=10)
        _settings = settings
        return True, f"연결 성공: {settings['db_server']} / {settings['db_name']}"
    except pyodbc.Error as e:
        _conn = None
        return False, f"연결 실패: {e}"


def is_connected() -> bool:
    if not _conn:
        return False
    try:
        _conn.cursor().execute("SELECT 1")
        return True
    except Exception:
        return False


def disconnect():
    global _conn
    if _conn:
        try:
            _conn.close()
        except Exception:
            pass
        _conn = None


# ── 스키마 탐색 (설정 도움용) ─────────────────────────────────────────────────

def list_tables() -> list[str]:
    """DB 내 모든 테이블 목록 반환"""
    if not _conn:
        return []
    cur = _conn.cursor()
    cur.execute(
        "SELECT TABLE_NAME FROM INFORMATION_SCHEMA.TABLES "
        "WHERE TABLE_TYPE='BASE TABLE' ORDER BY TABLE_NAME"
    )
    return [r[0] for r in cur.fetchall()]


def list_columns(table: str) -> list[tuple[str, str]]:
    """테이블 컬럼 목록 반환 [(컬럼명, 타입), ...]"""
    if not _conn:
        return []
    cur = _conn.cursor()
    cur.execute(
        "SELECT COLUMN_NAME, DATA_TYPE FROM INFORMATION_SCHEMA.COLUMNS "
        "WHERE TABLE_NAME = ? ORDER BY ORDINAL_POSITION",
        table,
    )
    return [(r[0], r[1]) for r in cur.fetchall()]


def preview_table(table: str, limit: int = 5) -> tuple[list[str], list[tuple]]:
    """테이블 미리보기. (컬럼명 리스트, 행 리스트) 반환"""
    if not _conn:
        return [], []
    cur = _conn.cursor()
    cur.execute(f"SELECT TOP {limit} * FROM [{table}]")
    cols = [d[0] for d in cur.description]
    rows = cur.fetchall()
    return cols, [tuple(r) for r in rows]


# ── 환자 데이터 조회 ──────────────────────────────────────────────────────────

def _query(sql: str, params: tuple) -> list[dict]:
    """SQL 실행 → dict 리스트 반환"""
    if not _conn:
        raise RuntimeError("DB에 연결되어 있지 않습니다.")
    cur = _conn.cursor()
    cur.execute(sql, params)
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, row)) for row in cur.fetchall()]


def _safe_str(v) -> str:
    if v is None:
        return ""
    return str(v).strip()


def get_patient_info(patient_id: str) -> dict | None:
    rows = _query(_settings["sql_patient"], (patient_id,))
    if not rows:
        return None
    r = rows[0]
    return {k: _safe_str(v) for k, v in r.items()}


def get_visits(patient_id: str) -> list[dict]:
    rows = _query(_settings["sql_visits"], (patient_id,))
    return [{k: _safe_str(v) for k, v in r.items()} for r in rows]


def get_diagnoses(patient_id: str) -> list[dict]:
    rows = _query(_settings["sql_diagnoses"], (patient_id,))
    return [{k: _safe_str(v) for k, v in r.items()} for r in rows]


def get_prescriptions(patient_id: str) -> list[dict]:
    rows = _query(_settings["sql_prescriptions"], (patient_id,))
    return [{k: _safe_str(v) for k, v in r.items()} for r in rows]


def get_labs(patient_id: str) -> list[dict]:
    rows = _query(_settings["sql_labs"], (patient_id,))
    return [{k: _safe_str(v) for k, v in r.items()} for r in rows]


def get_all_chart_data(patient_id: str) -> dict:
    """환자 전체 챠트 데이터를 한 번에 조회"""
    return {
        "기본정보":  get_patient_info(patient_id),
        "진료기록":  get_visits(patient_id),
        "진단명":    get_diagnoses(patient_id),
        "처방":      get_prescriptions(patient_id),
        "검사결과":  get_labs(patient_id),
    }
