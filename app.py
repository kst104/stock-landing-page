"""
주식 스크리너 허브  –  http://localhost:8888

구조:
  /              → 메인 허브 (모든 스크리너 목록)
  /screener/N    → 개별 스크리너 페이지
  /api/N/start   → 스크리닝 시작
  /api/N/progress→ SSE 진행 상태
  /api/N/result  → 결과 JSON
  /api/N/download→ CSV 다운로드

데이터 소스:
  - FinanceDataReader  : 과거 OHLCV (primary)
  - 한국투자증권 KIS API : 당일 실시간 overlay + FDR 실패 시 fallback
"""

import html as _html
import io, json, os, time, threading, requests as _req, base64 as _base64, secrets
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta
from pathlib import Path

try:
    from PIL import Image as _PIL_Image
    _PIL_AVAILABLE = True
except ImportError:
    _PIL_AVAILABLE = False

import numpy as np
import pandas as pd
import FinanceDataReader as fdr
from flask import Flask, Response, jsonify, redirect, request, session, url_for
from werkzeug.security import check_password_hash, generate_password_hash

app = Flask(__name__)

# ══════════════════════════════════════════════════════════════════════════════
# 한국투자증권 KIS OpenAPI 클라이언트
# ══════════════════════════════════════════════════════════════════════════════

KIS_BASE   = os.environ.get("KIS_BASE", "https://openapi.koreainvestment.com:9443")
KIS_KEY    = os.environ.get("KIS_KEY", "")
KIS_SECRET = os.environ.get("KIS_SECRET", "")
_TOKEN_FILE  = Path(__file__).parent / "kis_token.json"
_EMAIL_FILE  = Path(__file__).parent / "email_config.json"   # 이메일 설정 영구 저장
_AUTH_FILE   = Path(__file__).parent / "authorized_users.json"
_AUTH_SECRET_FILE = Path(__file__).parent / ".auth_secret"
AUTH_ADMIN_EMAIL = os.environ.get("AUTH_ADMIN_EMAIL", "promokorea@gmail.com")
AUTH_ADMIN_PASSWORD = os.environ.get("AUTH_ADMIN_PASSWORD", "")

# ── 이메일 알림 설정 (파일에서 로드, 없으면 빈 값) ──────────────────────────
EMAIL_SMTP    = "smtp.gmail.com"
EMAIL_PORT    = 587
EMAIL_FROM    = ""
EMAIL_PASS    = ""
EMAIL_TO_LIST: list[str] = []   # 수신자 최대 5명

MAX_RECIPIENTS = 5


def _load_or_create_auth_secret() -> str:
    try:
        if _AUTH_SECRET_FILE.exists():
            secret = _AUTH_SECRET_FILE.read_text(encoding="utf-8").strip()
            if secret:
                return secret
        secret = secrets.token_urlsafe(48)
        _AUTH_SECRET_FILE.write_text(secret, encoding="utf-8")
        return secret
    except Exception:
        return secrets.token_urlsafe(48)


app.secret_key = _load_or_create_auth_secret()
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
)


def _normalize_login_id(value: str) -> str:
    return (value or "").strip().lower()


def _load_auth_users() -> list[dict]:
    try:
        if not _AUTH_FILE.exists():
            return []
        data = json.loads(_AUTH_FILE.read_text(encoding="utf-8"))
        users = data.get("users", data if isinstance(data, list) else [])
        return [u for u in users if isinstance(u, dict) and u.get("email") and u.get("password_hash")]
    except Exception as e:
        print(f"[AUTH] 사용자 목록 로드 실패: {e}", flush=True)
        return []


def _save_auth_users(users: list[dict]):
    _AUTH_FILE.write_text(
        json.dumps({"users": users}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def _is_admin_login(email: str, password: str) -> bool:
    return _normalize_login_id(email) == AUTH_ADMIN_EMAIL and password == AUTH_ADMIN_PASSWORD


def _is_allowed_user(email: str, password: str) -> bool:
    email = _normalize_login_id(email)
    if _is_admin_login(email, password):
        return True
    for user in _load_auth_users():
        if _normalize_login_id(user.get("email", "")) == email:
            try:
                return check_password_hash(user.get("password_hash", ""), password or "")
            except Exception:
                return False
    return False


def _wants_json_response() -> bool:
    return request.path.startswith("/api/") or "application/json" in request.headers.get("Accept", "")


def _load_email_config():
    global EMAIL_FROM, EMAIL_PASS, EMAIL_TO_LIST
    try:
        if _EMAIL_FILE.exists():
            d = json.loads(_EMAIL_FILE.read_text(encoding="utf-8"))
            EMAIL_FROM = d.get("from", "")
            EMAIL_PASS = d.get("pass", "")
            # 구버전 호환: "to" 문자열 → 리스트 변환
            raw = d.get("to_list", d.get("to", ""))
            if isinstance(raw, list):
                EMAIL_TO_LIST = [x.strip() for x in raw if x.strip()]
            else:
                EMAIL_TO_LIST = [x.strip() for x in str(raw).split(",") if x.strip()]
            if EMAIL_FROM:
                print(f"[EMAIL] 설정 로드: {EMAIL_FROM} → {EMAIL_TO_LIST}", flush=True)
    except Exception as e:
        print(f"[EMAIL] 설정 로드 실패: {e}", flush=True)


def _save_email_config():
    try:
        _EMAIL_FILE.write_text(
            json.dumps({"from": EMAIL_FROM, "pass": EMAIL_PASS,
                        "to_list": EMAIL_TO_LIST},
                       ensure_ascii=False),
            encoding="utf-8"
        )
    except Exception as e:
        print(f"[EMAIL] 설정 저장 실패: {e}", flush=True)


_load_email_config()   # 서버 시작 시 즉시 로드


def _plog(msg: str):
    """인코딩 안전 print (Windows CP949 환경에서 특수문자 오류 방지)"""
    try:
        print(msg, flush=True)
    except UnicodeEncodeError:
        print(msg.encode("utf-8", errors="replace").decode("ascii", errors="replace"), flush=True)


def _send_email_alert(subject: str, body: str):
    """
    이메일 발송 (수신자 최대 5명).
    반환: (성공여부:bool, 오류메시지:str)
    """
    import smtplib, base64

    if not EMAIL_FROM:
        _plog("[EMAIL] 발신 주소 미설정"); return False, "발신 주소 미설정"
    if not EMAIL_PASS:
        _plog("[EMAIL] 앱 비밀번호 미설정"); return False, "앱 비밀번호 미설정"
    recipients = [e for e in EMAIL_TO_LIST if e]
    if not recipients:
        _plog("[EMAIL] 수신 주소 미설정"); return False, "수신 주소 미설정"

    try:
        to_str = ", ".join(recipients)
        _plog(f"[EMAIL] 발송 시도: {EMAIL_FROM} -> {to_str}")

        # 제목·본문 모두 base64로 인코딩 → 순수 ASCII raw 메일
        enc_subj = "=?utf-8?b?" + base64.b64encode(subject.encode("utf-8")).decode() + "?="
        enc_body = base64.b64encode(body.encode("utf-8")).decode()
        raw = "\r\n".join([
            f"From: {EMAIL_FROM}",
            f"To: {to_str}",
            f"Subject: {enc_subj}",
            "MIME-Version: 1.0",
            "Content-Type: text/plain; charset=utf-8",
            "Content-Transfer-Encoding: base64",
            "",
            enc_body,
        ])
        raw_bytes = raw.encode("ascii")

        with smtplib.SMTP(EMAIL_SMTP, EMAIL_PORT, timeout=15) as srv:
            srv.ehlo("localhost")
            srv.starttls()
            srv.ehlo("localhost")
            srv.login(EMAIL_FROM, EMAIL_PASS)
            srv.sendmail(EMAIL_FROM, recipients, raw_bytes)
        _plog(f"[EMAIL] 발송 완료 -> {to_str}")
        return True, "발송 완료"

    except smtplib.SMTPAuthenticationError:
        msg = ("인증 실패 - Gmail 앱 비밀번호 필요. "
               "Google 계정 > 보안 > 2단계 인증 > 앱 비밀번호 생성 (16자리)")
        _plog(f"[EMAIL] {msg}"); return False, msg
    except smtplib.SMTPConnectError as e:
        msg = f"SMTP 연결 실패 (포트 587): {e}"
        _plog(f"[EMAIL] {msg}"); return False, msg
    except smtplib.SMTPException as e:
        msg = f"SMTP 오류: {type(e).__name__}: {e}"
        _plog(f"[EMAIL] {msg}"); return False, msg
    except Exception as e:
        msg = f"예외: {type(e).__name__}: {e}"
        _plog(f"[EMAIL] {msg}"); return False, msg


class KISClient:
    """KIS OpenAPI 토큰 자동 관리 + 현재가 조회"""

    # 토큰 만료 응답 msg_cd (KIS 공식 코드)
    _TOKEN_EXPIRED_CODES = {"EGW00121", "EGW00123", "EGW00201"}

    def __init__(self):
        self._token      = None
        self._expires_at = datetime.min
        self._lock       = threading.Lock()   # 토큰 갱신 동시성 보호
        self._load_token()

    # ── 토큰 관리 ─────────────────────────────────────────────────────────────

    def _load_token(self):
        """파일 캐시 → 유효하면 재사용, 만료/없으면 신규 발급"""
        try:
            if _TOKEN_FILE.exists():
                d   = json.loads(_TOKEN_FILE.read_text(encoding="utf-8"))
                exp = datetime.fromisoformat(d["expires_at"])
                if exp > datetime.now() + timedelta(minutes=10):
                    self._token      = d["access_token"]
                    self._expires_at = exp
                    print(f"[KIS] 캐시 토큰 사용 (만료: {exp:%Y-%m-%d %H:%M})")
                    return
        except Exception:
            pass
        self._issue_token()

    def _issue_token(self):
        """KIS OAuth2 토큰 신규 발급 및 파일 저장"""
        try:
            r = _req.post(
                f"{KIS_BASE}/oauth2/tokenP",
                json={"grant_type": "client_credentials",
                      "appkey": KIS_KEY, "appsecret": KIS_SECRET},
                timeout=10,
            )
            d = r.json()
            if "access_token" not in d:
                raise ValueError(d)
            self._token      = d["access_token"]
            self._expires_at = datetime.now() + timedelta(
                seconds=int(d.get("expires_in", 86400))
            )
            _TOKEN_FILE.write_text(
                json.dumps({"access_token": self._token,
                            "expires_at":   self._expires_at.isoformat()}),
                encoding="utf-8",
            )
            print(f"[KIS] 새 토큰 발급 완료 (만료: {self._expires_at:%Y-%m-%d %H:%M})")
        except Exception as e:
            print(f"[KIS] 토큰 발급 실패: {e}")

    def _ensure_token(self):
        """
        만료 10분 전이면 Lock 보호 하에 갱신.
        여러 스레드가 동시에 갱신 시도하는 것을 방지.
        """
        if datetime.now() >= self._expires_at - timedelta(minutes=10):
            with self._lock:
                # Lock 획득 후 재확인 (다른 스레드가 이미 갱신했을 수 있음)
                if datetime.now() >= self._expires_at - timedelta(minutes=10):
                    self._issue_token()

    def _hdrs(self, tr_id: str) -> dict:
        self._ensure_token()
        return {
            "Authorization": f"Bearer {self._token}",
            "appkey":        KIS_KEY,
            "appsecret":     KIS_SECRET,
            "tr_id":         tr_id,
            "Content-Type":  "application/json; charset=utf-8",
        }

    def _is_token_error(self, d: dict) -> bool:
        """API 응답이 토큰 만료/인증 오류인지 확인"""
        return (d.get("rt_cd") != "0" and
                d.get("msg_cd", "") in self._TOKEN_EXPIRED_CODES)

    # ── 현재가 조회 ───────────────────────────────────────────────────────────

    def current_price(self, code: str) -> dict | None:
        """
        현재가 조회 (장중: 실시간, 장후: 당일 종가)
        반환: {open, high, low, close, volume, prev_close, marcap_won}
        토큰 만료 응답 수신 시 1회 자동 갱신 후 재시도.
        """
        for attempt in range(2):
            try:
                r = _req.get(
                    f"{KIS_BASE}/uapi/domestic-stock/v1/quotations/inquire-price",
                    headers=self._hdrs("FHKST01010100"),
                    params={"fid_cond_mrkt_div_code": "J", "fid_input_iscd": code},
                    timeout=5,
                )
                d = r.json()
                if self._is_token_error(d):
                    print(f"[KIS] current_price 토큰 만료 → 갱신 후 재시도 (attempt {attempt+1})")
                    with self._lock:
                        self._issue_token()
                    continue
                if d.get("rt_cd") != "0":
                    return None
                o = d["output"]
                try:
                    marcap_won = int(str(o.get("hts_avls", "0")).replace(",", "")) * 100_000_000
                except Exception:
                    marcap_won = 0
                return {
                    "open":       int(o["stck_oprc"]),
                    "high":       int(o["stck_hgpr"]),
                    "low":        int(o["stck_lwpr"]),
                    "close":      int(o["stck_prpr"]),
                    "volume":     int(o["acml_vol"]),
                    "prev_close": int(o["stck_sdpr"]),
                    "marcap_won": marcap_won,
                }
            except Exception:
                return None
        return None

    # ── 일봉 OHLCV 조회 ──────────────────────────────────────────────────────

    def _fetch_window(self, code: str, call_start: datetime,
                      call_end: datetime) -> list[dict]:
        """단일 날짜 구간 100봉 조회 (토큰 만료 시 1회 재시도)"""
        url    = (f"{KIS_BASE}/uapi/domestic-stock/v1/quotations"
                  f"/inquire-daily-itemchartprice")
        params = {
            "FID_COND_MRKT_DIV_CODE": "J",
            "FID_INPUT_ISCD":         code,
            "FID_INPUT_DATE_1":       call_start.strftime("%Y%m%d"),
            "FID_INPUT_DATE_2":       call_end.strftime("%Y%m%d"),
            "FID_PERIOD_DIV_CODE":    "D",
            "FID_ORG_ADJ_PRC":        "0",
        }
        for attempt in range(2):
            try:
                r = _req.get(url, headers=self._hdrs("FHKST03010100"),
                             params=params, timeout=10)
                d = r.json()
                if self._is_token_error(d):
                    print(f"[KIS] _fetch_window({code}) 토큰 만료 → 갱신")
                    with self._lock:
                        self._issue_token()
                    continue
                if d.get("rt_cd") != "0":
                    return []
                rows = []
                for o in d.get("output2", []):
                    try:
                        close = int(o.get("stck_clpr", 0) or 0)
                        if close <= 0:
                            continue
                        rows.append({
                            "Date":   pd.Timestamp(o["stck_bsop_date"]),
                            "Open":   int(o.get("stck_oprc",  close) or close),
                            "High":   int(o.get("stck_hgpr",  close) or close),
                            "Low":    int(o.get("stck_lwpr",  close) or close),
                            "Close":  close,
                            "Volume": int(o.get("acml_vol", 0) or 0),
                        })
                    except Exception:
                        continue
                return rows
            except Exception as e:
                _plog(f"[KIS] _fetch_window({code}) 예외: {e}")
                break
        return []

    def daily_ohlcv(self, code: str, n_bars: int = 200,
                    end_date: "datetime | None" = None) -> "pd.DataFrame | None":
        """
        KIS API FHKST03010100 으로 일별 OHLCV 조회.
        100봉/call 한도 → 구간을 병렬(ThreadPoolExecutor) 호출해 속도 최적화.
        step_cal=140 (≈100 거래일) 으로 호출 간 빈틈 최소화.
        반환: DatetimeIndex DataFrame (Open/High/Low/Close/Volume)
        """
        end_dt   = end_date if end_date else datetime.now()
        step_cal = 140          # 140 calendar days ≈ 100 trading days
        calls    = max(1, (n_bars + 99) // 100)

        # 구간 목록 생성
        windows = []
        for i in range(calls):
            ce = end_dt - timedelta(days=i * step_cal)
            cs = ce     - timedelta(days=step_cal)
            windows.append((cs, ce))

        # 구간별 병렬 호출 (최대 6 스레드)
        all_rows: list[dict] = []
        max_workers = min(calls, 6)
        with ThreadPoolExecutor(max_workers=max_workers) as ex:
            futures = {ex.submit(self._fetch_window, code, cs, ce): i
                       for i, (cs, ce) in enumerate(windows)}
            for fut in as_completed(futures):
                try:
                    all_rows.extend(fut.result())
                except Exception:
                    pass

        if not all_rows:
            return None

        df = (pd.DataFrame(all_rows)
              .drop_duplicates(subset=["Date"])
              .set_index("Date")
              .sort_index())
        return df if len(df) >= 30 else None

    def get_price_detail(self, code: str) -> dict | None:
        """
        주식현재가시세 상세 조회 (FHKST01010100)
        업종명, 52주 고저, PER/PBR, 시총, 시가/현재가/고저 반환
        """
        for attempt in range(2):
            try:
                r = _req.get(
                    f"{KIS_BASE}/uapi/domestic-stock/v1/quotations/inquire-price",
                    headers=self._hdrs("FHKST01010100"),
                    params={"fid_cond_mrkt_div_code": "J", "fid_input_iscd": code},
                    timeout=6,
                )
                d = r.json()
                if self._is_token_error(d):
                    with self._lock:
                        self._issue_token()
                    continue
                if d.get("rt_cd") != "0":
                    return None
                o = d["output"]
                def _i(k):
                    return int(str(o.get(k, 0) or 0).replace(",", "") or 0)
                def _f(k):
                    try:
                        return float(str(o.get(k, 0) or 0).replace(",", "") or 0)
                    except Exception:
                        return 0.0
                return {
                    "업종명":   str(o.get("bstp_kor_isnm", "") or "").strip(),
                    "현재가":   _i("stck_prpr"),
                    "시가":     _i("stck_oprc"),
                    "고가":     _i("stck_hgpr"),
                    "저가":     _i("stck_lwpr"),
                    "w52_high": _i("w52_hgpr"),
                    "w52_low":  _i("w52_lwpr"),
                    "per":      _f("per"),
                    "pbr":      _f("pbr"),
                    "시총억":   _f("hts_avls"),
                    "기관순매수":   _i("pgtr_ntby_qty"),   # 프로그램 순매수로 대체
                    "외국인순매수": _i("frgn_ntby_qty"),
                }
            except Exception:
                return None
        return None

    def trade_value_ranking(self, date_str: str = "", top_n: int = 100,
                            market: str = "J") -> list:
        """
        거래대금 순위 조회 (FHPST01720000)
        date_str: YYYYMMDD  top_n: 최대 반환 수
        market: "J"=전체(코스피+코스닥), "Q"=코스닥 전용
        반환 dict keys: rank, code, name, price, open, volume, prev_vol, day_chg, tr_value
        """
        url = f"{KIS_BASE}/uapi/domestic-stock/v1/quotations/volume-rank"
        scr_code = "20172" if market == "Q" else "20171"
        params = {
            "FID_COND_MRKT_DIV_CODE":  market,
            "FID_COND_SCR_DIV_CODE":   scr_code,
            "FID_INPUT_ISCD":          "0000",
            "FID_DIV_CLS_CODE":        "0",
            "FID_BLNG_CLS_CODE":       "0",
            "FID_TRGT_CLS_CODE":       "111111111",
            "FID_TRGT_EXLS_CLS_CODE":  "000000",
            "FID_INPUT_PRICE_1":       "",
            "FID_INPUT_PRICE_2":       "",
            "FID_VOL_CNT":             "",
            "FID_INPUT_DATE_1":        date_str,
            "FID_RANK_SORT_CLS_CODE":  "0",
        }
        for attempt in range(2):
            try:
                r = _req.get(url, headers=self._hdrs("FHPST01720000"),
                             params=params, timeout=10)
                d = r.json()
                if self._is_token_error(d):
                    with self._lock:
                        self._issue_token()
                    continue
                if d.get("rt_cd") != "0":
                    print(f"[KIS] trade_value_ranking({date_str}) 오류: {d.get('msg1','')}")
                    return []
                rows = []
                for i, o in enumerate(d.get("output", []), 1):
                    try:
                        code = str(o.get("stck_shrn_iscd", "")).strip().zfill(6)
                        if not code or code == "000000":
                            continue
                        def _int(key):
                            return int(str(o.get(key, "0")).replace(",", "") or 0)
                        rows.append({
                            "rank":     int(str(o.get("data_rank", i)).replace(",", "") or i),
                            "code":     code,
                            "name":     str(o.get("hts_kor_isnm", "")),
                            "price":    _int("stck_prpr"),
                            "open":     _int("stck_oprc"),
                            "volume":   _int("acml_vol"),
                            "prev_vol": _int("prdy_vol"),
                            "day_chg":  float(str(o.get("prdy_ctrt", "0")).replace(",", "") or 0),
                            "tr_value": _int("acml_tr_pbmn"),
                        })
                    except Exception:
                        continue
                return rows[:top_n]
            except Exception as e:
                print(f"[KIS] trade_value_ranking 예외: {e}")
                break
        return []


# 전역 KIS 클라이언트 (서버 시작 시 1회 초기화)
try:
    _kis = KISClient()
except Exception as e:
    print(f"[KIS] 초기화 실패 (FDR 단독 모드로 동작): {e}")
    _kis = None


# ── 공통 OHLCV 캐시 (당일 재요청 방지) ─────────────────────────────────────
_ohlcv_cache: dict = {}
_ohlcv_cache_lock  = threading.Lock()
_ohlcv_cache_day   = ""

_FDR_TIMEOUT = 8   # FDR 요청 1건당 최대 대기 시간 (초)


def _fdr_safe(code: str, start: str, end: str):
    """fdr.DataReader를 daemon thread + timeout으로 실행.
    Naver rate-limit 시 무한 대기를 방지한다.
    """
    _result = [None]
    _done   = threading.Event()

    def _run():
        try:
            _result[0] = fdr.DataReader(code, start, end)
        except Exception:
            pass
        finally:
            _done.set()

    threading.Thread(target=_run, daemon=True).start()
    _done.wait(timeout=_FDR_TIMEOUT)
    return _result[0]   # timeout이면 None 반환


# ── 공통 OHLCV 조회 (FDR primary / KIS fallback) ────────────────────────────

def fetch_ohlcv(code: str, start: str, end: str) -> pd.DataFrame:
    """
    Primary : FinanceDataReader  (timeout 보호 적용)
    Fallback : KIS API daily_ohlcv  (FDR 실패 · timeout 시)

    start/end : YYYYMMDD 문자열
    반환      : DatetimeIndex DataFrame (Open/High/Low/Close/Volume)
    """
    global _ohlcv_cache, _ohlcv_cache_day

    today_str = datetime.now().strftime("%Y%m%d")

    # 날짜가 바뀌면 캐시 초기화
    if _ohlcv_cache_day != today_str:
        with _ohlcv_cache_lock:
            if _ohlcv_cache_day != today_str:
                _ohlcv_cache.clear()
                _ohlcv_cache_day = today_str

    key = (code, start, end)
    with _ohlcv_cache_lock:
        if key in _ohlcv_cache:
            return _ohlcv_cache[key]

    # ── Primary: FDR (timeout 8 초) ──────────────────────────────────────────
    df = pd.DataFrame()
    try:
        tmp = _fdr_safe(code, start, end)
        if tmp is not None and not tmp.empty:
            df = tmp
    except Exception:
        pass

    # ── Fallback: KIS API ────────────────────────────────────────────────────
    if df.empty and _kis:
        try:
            start_dt = datetime.strptime(start, "%Y%m%d")
            end_dt   = datetime.strptime(end,   "%Y%m%d")
            cal_days = max((end_dt - start_dt).days, 1)
            n_bars   = int(cal_days * 5 / 7) + 30
            df_kis = _kis.daily_ohlcv(code, n_bars, end_date=end_dt)
            if df_kis is not None and not df_kis.empty:
                start_ts = pd.Timestamp(start_dt.date())
                end_ts   = pd.Timestamp(end_dt.date())
                df_kis = df_kis[(df_kis.index >= start_ts) & (df_kis.index <= end_ts)]
                if not df_kis.empty:
                    df = df_kis
        except Exception as e:
            _plog(f"[fetch_ohlcv] KIS fallback 실패({code}): {e}")

    with _ohlcv_cache_lock:
        _ohlcv_cache[key] = df
    return df

# ══════════════════════════════════════════════════════════════════════════════
# 종목 리스트 조회 (병렬 네이버 스크래핑 + 디스크 캐시)
# ══════════════════════════════════════════════════════════════════════════════

import re as _re

_listing_cache: pd.DataFrame = pd.DataFrame()
_listing_cache_date: str     = ""
_LISTING_CACHE_VERSION = "naver-krx-kis-v2"
_LISTING_DISK_CACHE = Path(__file__).parent / ".listing_cache.json"

# ── 네이버 페이지 1장 가져오기 (병렬 worker 함수) ──────────────────────────
def _naver_fetch_page(args):
    sosok, page, mkt = args
    try:
        from bs4 import BeautifulSoup as _BS4
        url  = (f"https://finance.naver.com/sise/sise_market_sum.naver"
                f"?sosok={sosok}&page={page}")
        hdrs = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
        r    = _req.get(url, headers=hdrs, timeout=15)
        r.encoding = "euc-kr"

        soup  = _BS4(r.text, "lxml")
        tbody = soup.find("table", class_="type_2")
        if tbody is None:
            return None

        # 행별로 종목코드 추출
        row_codes = []
        for tr in tbody.find_all("tr"):
            a = tr.find("a", href=_re.compile(r"/item/main\.naver\?code=\d{6}"))
            if a:
                m = _re.search(r"code=(\d{6})", a["href"])
                if m:
                    row_codes.append(m.group(1))

        if not row_codes:
            return None   # 빈 페이지 (마지막 페이지 초과)

        # 시가총액 테이블 파싱
        tables = pd.read_html(io.StringIO(r.text), flavor="lxml")
        tbl    = None
        for t in tables:
            if "종목명" in t.columns and "시가총액" in t.columns:
                tbl = t.dropna(subset=["종목명"]).copy()
                tbl = tbl[tbl["종목명"].astype(str) != "종목명"]
                break

        if tbl is None or len(tbl) == 0:
            return None

        n      = min(len(row_codes), len(tbl))
        tbl    = tbl.iloc[:n].copy()
        tbl["Code"]   = row_codes[:n]
        tbl["Market"] = mkt
        tbl["Marcap"] = (
            tbl["시가총액"]
            .apply(lambda x: _safe_int(str(x).replace(",", "")))
            .mul(100_000_000)
        )
        return (
            tbl.rename(columns={"종목명": "Name"})
               [["Code", "Name", "Market", "Marcap"]]
        )
    except Exception:
        return None


def _get_listing() -> pd.DataFrame:
    """
    Return listing rows with columns: Code, Name, Market, Marcap.

    Fresh lookup order:
      1. Naver Finance market-cap pages
      2. KRX listing via FinanceDataReader
      3. KIS trade-value ranking fallback

    Same-day memory/disk caches are reused before a fresh network lookup.
    """
    global _listing_cache, _listing_cache_date
    today = datetime.now().strftime("%Y%m%d")

    # ── 1. 메모리 캐시 ────────────────────────────────────────────────────────
    if not _listing_cache.empty and _listing_cache_date == today:
        return _listing_cache.copy()

    # ── 2. 디스크 캐시 ────────────────────────────────────────────────────────
    try:
        if _LISTING_DISK_CACHE.exists():
            cached = json.loads(_LISTING_DISK_CACHE.read_text(encoding="utf-8"))
            if (cached.get("date") == today and
                    cached.get("version") == _LISTING_CACHE_VERSION and
                    cached.get("rows")):
                df = pd.DataFrame(cached["rows"])
                _listing_cache      = df.reset_index(drop=True)
                _listing_cache_date = today
                print(f"[LISTING] 디스크 캐시 로드 → {len(df)}종목")
                return _listing_cache.copy()
    except Exception as e:
        print(f"[LISTING] 디스크 캐시 로드 실패: {e}")

    # ── 3. Naver first ───────────────────────────────────────────────────────
    try:
        # KOSPI ~55 페이지, KOSDAQ ~50 페이지 (넉넉하게)
        tasks = ([(0, p, "KOSPI")  for p in range(1, 65)] +
                 [(1, p, "KOSDAQ") for p in range(1, 55)])
        all_rows = []
        with ThreadPoolExecutor(max_workers=20) as ex:
            for result in ex.map(_naver_fetch_page, tasks):
                if result is not None and not result.empty:
                    all_rows.append(result)

        if all_rows:
            df = pd.concat(all_rows, ignore_index=True)
            df = df[df["Code"].notna() & (df["Marcap"] > 0)]
            df["Code"] = df["Code"].astype(str).str.zfill(6)
            df = df.drop_duplicates(subset="Code").reset_index(drop=True)
            _listing_cache      = df
            _listing_cache_date = today
            _save_listing_cache(df, today)
            print(f"[LISTING] Naver success → {len(df)}종목")
            return _listing_cache.copy()
    except Exception as e:
        print(f"[LISTING] Naver 실패: {e}  → KRX/FDR")

    # ── 4. KRX/FDR fallback ─────────────────────────────────────────────────
    try:
        df = fdr.StockListing("KRX")
        if df is not None and not df.empty and "Marcap" in df.columns:
            df = df[["Code", "Name", "Market", "Marcap"]].copy()
            df["Code"] = df["Code"].astype(str).str.zfill(6)
            df = df.reset_index(drop=True)
            _listing_cache      = df
            _listing_cache_date = today
            _save_listing_cache(df, today)
            print(f"[LISTING] KRX/FDR 성공 → {len(df)}종목")
            return _listing_cache.copy()
    except Exception as e:
        print(f"[LISTING] KRX/FDR 실패: {e}  → KIS")

    # ── 5. KIS final fallback ───────────────────────────────────────────────
    try:
        rows = []
        if _kis:
            for market, market_name in [("J", "KOSPI/KOSDAQ"), ("Q", "KOSDAQ")]:
                for item in _kis.trade_value_ranking(top_n=500, market=market):
                    rows.append({
                        "Code": str(item.get("code", "")).zfill(6),
                        "Name": str(item.get("name", "")),
                        "Market": market_name,
                        "Marcap": 0,
                    })
        if rows:
            df = pd.DataFrame(rows)
            df = df[df["Code"].str.match(r"^\d{6}$", na=False)]
            df = df.drop_duplicates(subset="Code").reset_index(drop=True)
            _listing_cache      = df
            _listing_cache_date = today
            _save_listing_cache(df, today)
            print(f"[LISTING] KIS fallback 성공 → {len(df)}종목")
            return _listing_cache.copy()
    except Exception as e:
        print(f"[LISTING] KIS fallback 실패: {e}")

    # 모두 실패
    print("[LISTING] 모든 방법 실패 → 빈 리스트 반환")
    return pd.DataFrame(columns=["Code", "Name", "Market", "Marcap"])


def _get_listing_with_progress(prog: dict | None = None) -> pd.DataFrame:
    """Load listing and expose the lookup time in the progress payload."""
    if prog is not None:
        prog["listing_status"] = "loading"
        prog["listing_started_at"] = time.time()
        prog.pop("listing_elapsed", None)
        prog.pop("listing_count", None)
    started = time.perf_counter()
    df = _get_listing()
    elapsed = round(time.perf_counter() - started, 1)
    if prog is not None:
        prog["listing_status"] = "done"
        prog["listing_elapsed"] = elapsed
        prog["listing_count"] = int(len(df))
        prog.pop("listing_started_at", None)
    return df


def _save_listing_cache(df: pd.DataFrame, date_str: str):
    """listing을 디스크에 JSON으로 저장 (서버 재시작 후 재사용)."""
    try:
        data = {
            "date": date_str,
            "version": _LISTING_CACHE_VERSION,
            "rows": df.to_dict(orient="records"),
        }
        _LISTING_DISK_CACHE.write_text(
            json.dumps(data, ensure_ascii=False), encoding="utf-8"
        )
    except Exception as e:
        print(f"[LISTING] 디스크 캐시 저장 실패: {e}")


def _safe_int(s):
    try:
        return int(float(s))
    except Exception:
        return 0


# ── 종목 병렬 스크리닝 헬퍼 ──────────────────────────────────────────────────
_SCREEN_WORKERS = 20   # 동시 FDR 요청 수

def _run_screen_parallel(valid_df, ticker_fn, start, end, prog):
    """valid_df 종목들을 ticker_fn(code, start, end)으로 병렬 처리.
    결과 dict에 종목명/시총/시장 자동 추가 후 리스트 반환.
    """
    rows = []
    lock = threading.Lock()
    cnt  = [0]

    def _job(row):
        code = str(row["Code"]).zfill(6)
        res  = ticker_fn(code, start, end)
        with lock:
            cnt[0] += 1
            prog["current"] = cnt[0]
            if res is not None:
                res["종목명"]   = row["Name"]
                res["시총(억)"] = int(row["Marcap"]) // 100_000_000
                res["시장"]     = row["Market"]
                rows.append(res)

    with ThreadPoolExecutor(max_workers=_SCREEN_WORKERS) as ex:
        list(ex.map(_job, (row for _, row in valid_df.iterrows())))

    return rows


# ══════════════════════════════════════════════════════════════════════════════
# 공통 지표 함수
# ══════════════════════════════════════════════════════════════════════════════

def linreg(series, period):
    n = period; x = np.arange(n, dtype=float); xm = x.mean()
    xv = np.sum((x - xm) ** 2); v = series.values.astype(float)
    out = np.full(len(v), np.nan)
    for i in range(n - 1, len(v)):
        y = v[i-n+1:i+1]
        if np.any(np.isnan(y)): continue
        ym = y.mean(); sl = np.sum((x-xm)*(y-ym))/xv
        out[i] = sl*(n-1)+(ym-sl*xm)
    return pd.Series(out, index=series.index)

def ema(s, p):
    return s.ewm(span=p, adjust=False).mean()


def _calc_adx(df: pd.DataFrame, period: int = 14) -> pd.Series:
    """ADX 계산 (Wilder 평활).
    df에 High·Low·Close 컬럼 필요.
    반환: ADX pd.Series (초기 2*period-1 봉까지 NaN)
    """
    hi  = df["High"].astype(float).values
    lo  = df["Low"].astype(float).values
    cl  = df["Close"].astype(float).values
    n   = len(hi)

    tr_arr  = np.full(n, np.nan)
    pdm_arr = np.full(n, np.nan)
    ndm_arr = np.full(n, np.nan)

    for i in range(1, n):
        tr_arr[i]  = max(hi[i] - lo[i],
                         abs(hi[i] - cl[i-1]),
                         abs(lo[i] - cl[i-1]))
        up          = hi[i] - hi[i-1]
        dn          = lo[i-1] - lo[i]
        pdm_arr[i]  = up if (up > dn  and up  > 0) else 0.0
        ndm_arr[i]  = dn if (dn > up  and dn  > 0) else 0.0

    # Wilder 평활: 첫값=합계, 이후=이전−이전/p+현재
    str_arr  = np.full(n, np.nan)
    spdm_arr = np.full(n, np.nan)
    sndm_arr = np.full(n, np.nan)

    if n > period:
        str_arr[period]  = float(np.nansum(tr_arr[1:period + 1]))
        spdm_arr[period] = float(np.nansum(pdm_arr[1:period + 1]))
        sndm_arr[period] = float(np.nansum(ndm_arr[1:period + 1]))
        for i in range(period + 1, n):
            str_arr[i]  = str_arr[i-1]  - str_arr[i-1]  / period + tr_arr[i]
            spdm_arr[i] = spdm_arr[i-1] - spdm_arr[i-1] / period + pdm_arr[i]
            sndm_arr[i] = sndm_arr[i-1] - sndm_arr[i-1] / period + ndm_arr[i]

    # +DI, −DI
    with np.errstate(invalid="ignore", divide="ignore"):
        pdi = np.where(str_arr > 0, 100.0 * spdm_arr / str_arr, 0.0)
        ndi = np.where(str_arr > 0, 100.0 * sndm_arr / str_arr, 0.0)

    # DX
    di_sum  = pdi + ndi
    di_diff = np.abs(pdi - ndi)
    dx = np.where((di_sum > 0) & ~np.isnan(str_arr),
                  100.0 * di_diff / di_sum, np.nan)

    # ADX = Wilder 평활 of DX (첫값 = period개 DX 평균)
    adx       = np.full(n, np.nan)
    first_idx = 2 * period - 1      # ADX 최초 유효 인덱스 (0-based)
    if n > first_idx:
        adx[first_idx] = float(np.nanmean(dx[period: first_idx + 1]))
        for i in range(first_idx + 1, n):
            adx[i] = (adx[i-1] * (period - 1) + dx[i]) / period

    return pd.Series(adx, index=df.index)


# ── MACD Reloaded용 MA 구현 헬퍼 ──────────────────────────────────────────────

def _wma(series: pd.Series, period: int) -> pd.Series:
    """Weighted Moving Average (선형 가중)"""
    w = np.arange(1, period + 1, dtype=np.float64)
    w = w / w.sum()
    vals = series.values.astype(np.float64)
    out  = np.full(len(vals), np.nan)
    for i in range(period - 1, len(vals)):
        seg = vals[i - period + 1 : i + 1]
        if not np.any(np.isnan(seg)):
            out[i] = np.dot(seg, w)
    return pd.Series(out, index=series.index)


def _linreg_slope(series: pd.Series, period: int) -> pd.Series:
    """선형회귀 기울기 (LinRegSlope)"""
    n  = period
    x  = np.arange(n, dtype=np.float64)
    xm = x.mean(); xv = np.sum((x - xm) ** 2)
    vals = series.values.astype(np.float64)
    out  = np.full(len(vals), np.nan)
    for i in range(n - 1, len(vals)):
        y = vals[i - n + 1 : i + 1]
        if np.any(np.isnan(y)): continue
        ym = y.mean()
        out[i] = np.sum((x - xm) * (y - ym)) / xv
    return pd.Series(out, index=series.index)


def _var_ma(series: pd.Series, period: int, cmo_period: int = 9) -> pd.Series:
    """Variable Adaptive MA (Chande)"""
    alpha = 2.0 / (period + 1)
    delta = series.diff()
    vUD   = delta.clip(lower=0).rolling(cmo_period).sum()
    vDD   = (-delta).clip(lower=0).rolling(cmo_period).sum()
    tot   = vUD + vDD
    cmo_a = np.where(tot.values != 0,
                     np.abs((vUD.values - vDD.values) / tot.values), 0.0)
    vals = series.values.astype(np.float64)
    out  = np.full(len(vals), np.nan)
    fi   = int(np.argmax(~np.isnan(vals) & ~np.isnan(cmo_a)))
    if fi < len(vals):
        out[fi] = vals[fi]
    for i in range(fi + 1, len(vals)):
        if np.isnan(vals[i]): continue
        prev = out[i-1] if not np.isnan(out[i-1]) else vals[i]
        k    = alpha * cmo_a[i]
        out[i] = k * vals[i] + (1.0 - k) * prev
    return pd.Series(out, index=series.index)


def _hull_ma(series: pd.Series, period: int) -> pd.Series:
    """Hull Moving Average = WMA(2·WMA(C,n//2)−WMA(C,n), round(√n))"""
    half   = int(period / 2)
    sqrt_p = int(round(np.sqrt(period)))
    return _wma(2.0 * _wma(series, half) - _wma(series, period), sqrt_p)


def _tillson_t3(series: pd.Series, period: int, t3a1: float = 0.7) -> pd.Series:
    """Tillson T3"""
    c1 = -(t3a1 ** 3)
    c2 =  3*t3a1**2 + 3*t3a1**3
    c3 = -6*t3a1**2 - 3*t3a1 - 3*t3a1**3
    c4 =  1 + 3*t3a1 + t3a1**3 + 3*t3a1**2
    e1 = series.ewm(span=period, adjust=False).mean()
    e2 = e1.ewm(span=period, adjust=False).mean()
    e3 = e2.ewm(span=period, adjust=False).mean()
    e4 = e3.ewm(span=period, adjust=False).mean()
    e5 = e4.ewm(span=period, adjust=False).mean()
    e6 = e5.ewm(span=period, adjust=False).mean()
    return c1*e6 + c2*e5 + c3*e4 + c4*e3


def _ma_type(series: pd.Series, period: int,
             ma_type: int, t3a1: float = 0.7) -> pd.Series:
    """MA 타입 선택기 (1~11)"""
    import math
    if   ma_type == 1:  return series.rolling(period).mean()
    elif ma_type == 2:  return series.ewm(span=period, adjust=False).mean()
    elif ma_type == 3:  return _wma(series, period)
    elif ma_type == 4:
        e1 = series.ewm(span=period, adjust=False).mean()
        return 2*e1 - e1.ewm(span=period, adjust=False).mean()
    elif ma_type == 5:
        h1 = math.ceil(period / 2); h2 = period // 2 + 1
        return series.rolling(h1).mean().rolling(h2).mean()
    elif ma_type == 6:  return _var_ma(series, period)
    elif ma_type == 7:  return series.ewm(alpha=1.0/period, adjust=False).mean()
    elif ma_type == 8:
        lag = int(period / 2)
        return (series + (series - series.shift(lag))).ewm(span=period, adjust=False).mean()
    elif ma_type == 9:  return linreg(series, period) + _linreg_slope(series, period)
    elif ma_type == 10: return _hull_ma(series, period)
    elif ma_type == 11: return _tillson_t3(series, period, t3a1)
    else:               return series.ewm(span=period, adjust=False).mean()


# ══════════════════════════════════════════════════════════════════════════════
# 스크리너 정의 목록  (새 스크리너 추가 시 여기에만 등록)
# ══════════════════════════════════════════════════════════════════════════════

SCREENERS = {
    1: {
        "title":  "조건1 VL스크리너",
        "desc":   "VL = linreg(C,50)×2 − linreg(linreg(C,50),50) | 시총 1500억↑ · EMA200↑ · 종가>EMA60 · VL↑ · 종가 VL 대비 −5~−15%",
        "color":  "#4f8ef7",
        "icon":   "📈",
    },
    2: {
        "title":  "조건2 금일급락",
        "desc":   "조건1 전체 충족 + 전일 대비 −5% 초과 하락 종목",
        "color":  "#ef4444",
        "icon":   "🔻",
    },
    3: {
        "title":  "조건3 역사적스퀴즈",
        "desc":   "주봉 거래량 볼린저밴드(20,2) 대역폭이 52주 최솟값 달성 + 일봉 EMA200 상승",
        "color":  "#a78bfa",
        "icon":   "🔬",
    },
    4: {
        "title":  "조건4 거래량점수",
        "desc":   "20일 거래량MA 3일↑ + EMA200 3일↑ · 가격·거래량 관계 점수화 (최대 3.5점)",
        "color":  "#34d399",
        "icon":   "📊",
    },
    5: {
        "title":  "조건5 ASGMA",
        "desc":   "5일 평균거래량 30만↑ · VR14×ATR비율 ≥ 3 · 최근 15일 내 기준봉(몸통 12%↑ 또는 진폭 18%↑) 존재",
        "color":  "#f59e0b",
        "icon":   "⚡",
    },
    6: {
        "title":  "조건6 폭발직전",
        "desc":   "조건5 ASGMA점수 ≥ 3.0 + 최근 3일 모두 |등락폭| ≤ ATR(14) × 70%",
        "color":  "#ff6b6b",
        "icon":   "🚀",
    },
    7: {
        "title":  "조건7 폭발준비",
        "desc":   "5일 평균거래량 30만↑ · ASGMA점수 ≤ 1(눌림) · 기준봉 존재 · SMA5 또는 SMA20 상향 돌파",
        "color":  "#818cf8",
        "icon":   "🎯",
    },
    8: {
        "title":  "조건8 하이킨아시 주봉",
        "desc":   "5일 평균거래량 10만↑ · 주봉 양봉 · 하이킨아시 주봉 음봉 시가 저항선 신규 돌파",
        "color":  "#06b6d4",
        "icon":   "🕯️",
    },
    9: {
        "title":  "조건9 비율골든크로스",
        "desc":   "5일 평균거래량 10만↑ · 최근 20일 내 기준봉 · EMA20 이격 20%이내 · 누적 양음 거래비율 90일선 골든크로스",
        "color":  "#10b981",
        "icon":   "✨",
    },
    10: {
        "title":  "조건10 엔벨로프눌림",
        "desc":   "시총 1500억↑ · 30봉 내 시가→고가 15%↑ 캔들 · 7일 내 Envelope(20,40%) 상단 터치 · 현재가 VL ±3% 이내 눌림",
        "color":  "#f472b6",
        "icon":   "📉",
    },
    11: {
        "title":  "조건11 VL매수타점",
        "desc":   "시총 1500억↑ · 60일 내 고점/종가 Envelope(20,40%) 상단 돌파 이력 · (VL4|5)>(VL2|3)<(VL1|0) 확장 V자 반전 · 종가 VL 대비 10%↑ · 5일변동 ±10% 이내",
        "color":  "#fb923c",
        "icon":   "🎯",
    },
    12: {
        "title":  "조건12 Harvard RSI",
        "desc":   "시총 1500억↑ · RSI(21) 기반 A=SMA(2), B=SMA(34)−1.6185σ · (A4>B4 or A5>B5) → B(1)>A(1) → A>B 패턴 (눌림 후 재골든크로스)",
        "color":  "#e879f9",
        "icon":   "🔭",
    },
    13: {
        "title":  "조건13 이제출발",
        "desc":   "시총 3000억↑ · Envelope(20,40%) 상단이 30일 전 대비 5% 이하 상승 (횡보·하락) · 최근 10일 내 고가가 상단 3% 이내 접근 또는 돌파",
        "color":  "#34d399",
        "icon":   "🚦",
    },
    14: {
        "title":  "조건14 세력20평단 돌파",
        "desc":   "시총 3000억↑ · 세력20평단(거래량폭발 양봉 중값 EMA20) 신규 돌파 · 전일 동시간대 대비 거래량 200%↑",
        "color":  "#f59e0b",
        "icon":   "💥",
    },
    15: {
        "title":  "조건15 세력20평단임박",
        "desc":   "시총 3000억↑ · 종가 < 세력20평단(5% 이내) · 종가 > VL(변동회귀선) · VL 전일 대비 3%↑",
        "color":  "#a78bfa",
        "icon":   "🎯",
    },
    16: {
        "title":  "조건16 RSI밴드 압축돌파",
        "desc":   "시총 1500억↑ · UPPER=SMA(RSI21,34)+1.6185σ · LOWER=SMA−1.6185σ · 3일연속 밴드폭 축소 · S_RSI(SMA2) UPPER 상향돌파",
        "color":  "#34d399",
        "icon":   "🔔",
    },
    17: {
        "title":  "조건17 MACD Reloaded",
        "desc":   "시총 1500억↑ · HULL MA(12,26,9) MACD · src2=MA12−MA26 · Signal=MA(src2,9) · CrossUp(src2,Signal) · 양봉(C>O)",
        "color":  "#f472b6",
        "icon":   "🚀",
    },
    18: {
        "title":  "조건18 MACD Reloaded2",
        "desc":   "시총 1500억↑ · 거래대금 1600억↑ or 거래량 200만주↑ · HULL MACD hist≥0 & hist>hist[1] (라임그린) · 양봉(C>O)",
        "color":  "#22d3ee",
        "icon":   "📊",
    },
    19: {
        "title":  "조건19 급락회귀선",
        "desc":   "시총 1500억↑ · VL=linreg(C,50)+(A−A1) · 최근 10거래일 중 (C−VL)/VL≥15% 5일 이상 · 현재 종가>VL · 주봉 저점 2주 연속 상승",
        "color":  "#fb923c",
        "icon":   "🔄",
    },
    20: {
        "title":  "조건20 전고점돌파",
        "desc":   "시총 3000억↑ · 최근 60일 음봉 중 최고 시가(전고점 저항) 돌파 · 동시간대 거래량 전일대비 250%↑",
        "color":  "#a78bfa",
        "icon":   "🔝",
    },
    21: {
        "title":  "조건21 테마상한가",
        "desc":   "네이버 금융 테마 상승률 상위 5개 · 테마별 양봉(시가<종가) 구성종목 전체 표시",
        "color":  "#34d399",
        "icon":   "🚀",
    },
    22: {
        "title":  "조건22 캔들볼륨",
        "desc":   "시총 1500억↑ · 최근 26주 음봉 중 거래량 20주 평균 1.5배↑(캔들볼륨) 시가 최댓값을 저항선으로 정의 · 현재 주봉 종가 신규 돌파 · 종가 VL(변동회귀선) 대비 5%↑ · 5일 평균거래량 10만↑",
        "color":  "#f97316",
        "icon":   "🕯️",
    },
    23: {
        "title":  "조건23 세력20평단돌파",
        "desc":   "시총 3000억↑ · 5일 평균거래량 30만주↑ · 세력20평단(거래량폭발 양봉 중값 EMA20)과 종가의 가격차가 5거래일 연속 10% 이내 (평단 근접 횡보)",
        "color":  "#a78bfa",
        "icon":   "⚡",
    },
    24: {
        "title":  "조건24 RSI다이버전스",
        "desc":   "시총 3000억↑ · RSI(30) 매수 다이버전스: 최근 6봉 저점 < 과거 저점(가격 Lower Low) & 최근 6봉 RSI저점 > 과거 RSI저점(RSI Higher Low) · lookback 60봉",
        "color":  "#38bdf8",
        "icon":   "📡",
    },
    25: {
        "title":  "조건25 거래대금순위",
        "desc":   "금일 거래대금 순위(KIS API) · 전일 순위 CSV 업로드 비교 · 전일 1~10위 제외 · 순위 30위↑ 상승 · 양봉(C>O) · 전일동시간대 거래량 200%↑ · 상위 50위",
        "color":  "#f59e0b",
        "icon":   "💰",
    },
    26: {
        "title":  "조건26 20일선전고점돌파",
        "desc":   "시가총액 1500억↑ · ETF/ETN 제외 · SMA(20)이 60봉 이내 전고점을 오늘 신규 돌파하거나 같아지는 시점",
        "color":  "#10b981",
        "icon":   "📈",
    },
    27: {
        "title":  "조건27 최강종목",
        "desc":   "시총 1500억↑ · ETF/ETN 제외 · BB(200,3) 상한선 이상 종가 + Envelope(20,40%) 상단선 이상 종가 OR 오늘 고점 돌파",
        "color":  "#f43f5e",
        "icon":   "🔥",
    },
    28: {
        "title":  "조건28 RSI밴드돌파",
        "desc":   "시총 1500억↑ · RSI(21) 밴드 SMA(34)±1.6185σ · 3일 밴드압축 조건 없음 · S_RSI(SMA2)가 UPPER 상향돌파",
        "color":  "#a78bfa",
        "icon":   "📊",
    },
    29: {
        "title":  "조건29 양음양",
        "desc":   "시총 1500억↑ · ETF/ETN 제외 · 2봉전 강한 양봉(시가대비+8%↑) → 1봉전 거래량감소 음봉(≤50%) → 오늘 양봉 · 종가>SMA20·SMA200 · 전일동시간대 거래량100%↑",
        "color":  "#f59e0b",
        "icon":   "🕯️",
    },
    30: {
        "title":  "조건30 VL급반등",
        "desc":   "시총 1500억↑ · 지난 30거래일간 VL 피크→트로프 낙폭 40%↑ · 최근 3거래일 연속 VL 일간 4%↑씩 반등",
        "color":  "#34d399",
        "icon":   "📈",
    },
    31: {
        "title":  "조건31 pro RSI",
        "desc":   "시총 1500억↑ · ETF/ETN 제외 · expoLen=2×LenRSI−1 EMA로 RSI 밴드 역산 · 9가지 FindMode 선택 (기본: 하단돌파 OR 중심선돌파)",
        "color":  "#818cf8",
        "icon":   "📡",
    },
    32: {
        "title":  "조건32 캔들볼륨저항",
        "desc":   "시총 1500억↑ · ETF/ETN 제외 · 최근 90봉 음봉 중 (O+C+H+L)/4×거래량 최대인 봉의 시가 = 저항선 · 금일 종가가 저항선 상향돌파",
        "color":  "#f97316",
        "icon":   "🕯",
    },
    33: {
        "title":  "조건33 과열스코어",
        "desc":   "시총 1500억↑ · ATR(5w)/ATR(20w) × MA(V,5w)/MA(V,20w) × 이격도(20w)/100 > 3 · 주봉 양봉 · 전주 대비 거래량 증가 · 이번 주 첫 충족",
        "color":  "#f43f5e",
        "icon":   "🌡",
    },
    34: {
        "title":  "조건34 과열스코어순위",
        "desc":   "시총 1500억↑ · ETF/ETN 제외 · 일봉 ATR(5)/ATR(20) × MA(V,5)/MA(V,20) × 이격도(20)/100 > 3 · 양봉 · 거래량증가 · 전일 대비 스코어 상승폭 상위 10종목",
        "color":  "#fb923c",
        "icon":   "🏆",
    },
    35: {
        "title":  "조건35 거래대금순위",
        "desc":   "시총 3000억↑~20조 미만 · ETF/ETN 제외 · 전일 거래대금 1~3위 제외 · 거래대금=거래량×(O+H+L+C)/4 기준 당일 TOP10 · 실시간 스캔 + 이메일 알림",
        "color":  "#06b6d4",
        "icon":   "💰",
    },
    36: {
        "title":  "조건36 이제진짜출발",
        "desc":   "시총 1500억↑ · ETF/ETN 제외 · 조건23(세력20평단 ±10% 5일+ADX>25) AND 조건34(일봉 ATR(5)/ATR(20)×MA(V,5)/MA(V,20)×이격도(20)/100 ≥ 3) 동시 충족",
        "color":  "#fbbf24",
        "icon":   "🚀",
    },
    37: {
        "title":  "조건37 거래대금RSI",
        "desc":   "시총 3000억↑~20조 미만 · ETF/ETN 제외 · 거래대금(거래량×(O+H+L+C)/4) TOP100 종목 중 RSI 밴드 상단선 또는 중심선을 상향 돌파한 종목",
        "color":  "#a855f7",
        "icon":   "📡",
    },
    38: {
        "title":  "조건38 패턴검색",
        "desc":   "시총 1500억↑ · ETF/ETN 제외 · 90일 일봉 차트 이미지 업로드 → VL·세력평단·TDI·EMA 복합 지표 패턴 유사도 비교 → 상위 5종목 + 유사율(%) 표시",
        "color":  "#10b981",
        "icon":   "🔍",
    },
    39: {
        "title":  "조건39 VL이격시작",
        "desc":   "시총 1500억↑ · ETF/ETN 제외 · VL 대비 종가 30%↑ · VL 상승(전일VL<금일VL) · 60일 신고가 제외 · 3일 연속 거래없음 제외 · VL이격(%) 높은 순",
        "color":  "#f59e0b",
        "icon":   "📐",
    },
    40: {
        "title":  "조건40 파워맵우수",
        "desc":   "KIS API · 코스닥 거래대금 TOP20 실시간 조회 · 10분 간격 자동 스캔 · 순위 변동(신규진입·이탈·3↑이상 이동) 이메일 알림",
        "color":  "#06b6d4",
        "icon":   "🗺️",
    },
    41: {
        "title":  "조건41 파워맵최고",
        "desc":   "KIS API · 전일 코스닥 거래대금 10위 밖 · 금일 TOP20 진입 · 전일동시간대 거래량 200%↑ · 양봉 · 실시간 스캔 + 이메일 알림",
        "color":  "#f97316",
        "icon":   "🏆",
    },
    43: {
        "title":  "조건43 proRSI2",
        "desc":   "시총 1500억↑ · ETF/ETN 제외 · LenRSI=30 · expoLen=59 EMA로 RSI 밴드 역산 · 9가지 FindMode 선택 (기본: 하단돌파 OR 중심선돌파)",
        "color":  "#6366f1",
        "icon":   "🔷",
    },
    44: {
        "title":  "조건44 Market shift levels",
        "desc":   "시총 5,000억↑ · ETF/ETN 제외 · HMA(55) 기반 지지·저항 레벨 자동 갱신 · 6가지 FindMode (기본=6: 종가 레벨 상향돌파+양봉) · 실시간 스캔 + 이메일 알림",
        "color":  "#10b981",
        "icon":   "🌊",
    },
    42: {
        "title":  "조건42 파워수급분석",
        "desc":   "KIS API · 코스닥 거래대금 TOP20 · 업종·상승이유·하락이유 자동분석 · 연관종목 3개 연결 · 실시간 스캔 + 이메일 알림",
        "color":  "#8b5cf6",
        "icon":   "🔬",
    },
    45: {
        "title":  "조건45 QQE",
        "desc":   "시총 1,500억↑ · ETF/ETN 제외 · QQEF=EMA(RSI(14),5) · QQES=QQE 슬로우 라인 · QQEF·QQES 모두 50↓ 구간에서 전일 QQEF<QQES → 금일 QQEF>QQES (골든크로스) + VL금일↑",
        "color":  "#ec4899",
        "icon":   "📈",
    },
    46: {
        "title":  "조건46 MSL2",
        "desc":   "시총 3,000억↑ · ETF/ETN 제외 · HMA(55) 기반 지지·저항 레벨 자동 갱신 · 종가가 레벨 상향돌파 + 양봉 · 실시간 스캔 + 이메일 알림",
        "color":  "#f59e0b",
        "icon":   "🎯",
    },
    47: {
        "title":  "과열스코어(월)",
        "desc":   "시총 3,000억↑ · ETF/ETN 제외 · ATR(5m)/ATR(20m)×MA(V,5m)/MA(V,20m)×이격도(20m)/100 > 3 · 이번 달 월봉에서 처음 충족",
        "color":  "#e11d48",
        "icon":   "🔥",
    },
    48: {
        "title":  "칼만트렌드라인",
        "desc":   "시총 3,000억↑ · ETF/ETN 제외 · Short Kalman(50) / Long Kalman(150) 교차 및 추세 상태 검색",
        "color":  "#14b8a6",
        "icon":   "📈",
    },
    49: {
        "title":  "조건49 박문환원인점",
        "desc":   "시총 3,000억↑ · ETF/ETN 제외 · 5일선 이탈 원인점(시가·종가 모두 MA5 하방 첫 봉의 저가) 탐색 · 전일 종가≤원인점 → 금일 종가 재돌파",
        "color":  "#f472b6",
        "icon":   "📍",
    },
    50: {
        "title":  "조건50 김승태타점",
        "desc":   "시총 3,000억↑ · ETF/ETN 제외 · A:120봉신고가 20봉이내 · B:양봉 · C:전일종가<SMA5 · D:전일시가<SMA5 · E:종가 SMA5 골든크로스 · G:전일저가대비종가 5%↑ · H:5봉평균거래량 30만↑ · J:SMA200 2봉연속상승",
        "color":  "#0ea5e9",
        "icon":   "🎯",
    },
    51: {
        "title":  "조건51 김승태타점4",
        "desc":   "시총 3,000억↑ · ETF/ETN 제외 · A~C:3일연속 종가<SMA5 · D:금일시가<SMA5 · E:금일종가<SMA5 · F:양봉 · G:SMA20 2봉연속상승 · H:SMA60 2봉연속상승 · J:5봉평균거래량 10만↑",
        "color":  "#8b5cf6",
        "icon":   "🎯",
    },
}


# ══════════════════════════════════════════════════════════════════════════════
# 조건1: VL 스크리너 로직
# ══════════════════════════════════════════════════════════════════════════════

MIN_CAP   = 150_000_000_000
LR_PERIOD = 50
EMA200    = 200
EMA60     = 60
LOOKBACK  = 800
ZONES     = [-5, -10, -15]

def _screen1_ticker(code, start, end):
    try:
        df = fetch_ohlcv(code, start, end)
        if df is None or len(df) < EMA200 + LR_PERIOD*2 + 5: return None
        c_s = df["Close"].astype(float)
        e60s = ema(c_s, EMA60); e200s = ema(c_s, EMA200)
        A = linreg(c_s, LR_PERIOD); vl = A + (A - linreg(A, LR_PERIOD))
        c  = c_s.iloc[-1];  c1 = c_s.iloc[-2]
        v  = vl.iloc[-1];   v1 = vl.iloc[-2]
        if pd.isna(v) or pd.isna(v1): return None
        if e200s.iloc[-2] >= e200s.iloc[-1]: return None  # EMA200 비상승
        if c <= e60s.iloc[-1]: return None                # 종가 < EMA60
        if v1 >= v: return None                           # VL 비상승
        day_chg = (c - c1) / c1 * 100
        if v <= c: return None                            # VL < 종가
        drop = (c - v) / v * 100
        if drop > ZONES[0]: return None
        zone = (f"{ZONES[0]}%~{ZONES[1]}%" if drop > ZONES[1] else
                f"{ZONES[1]}%~{ZONES[2]}%" if drop > ZONES[2] else
                f"{ZONES[2]}% 이하")
        return {"종목코드": code, "종가": int(c), "VL": round(v,2),
                "VL대비(%)": round(drop,2), "전일대비(%)": round(day_chg,2),
                "EMA60": round(e60s.iloc[-1],2), "EMA200": round(e200s.iloc[-1],2),
                "구간": zone}
    except Exception:
        return None

def run_screen1(date_str, prog):
    prog.update({"current":0,"total":0,"status":"loading"})
    start = (datetime.strptime(date_str,"%Y%m%d")-timedelta(days=LOOKBACK)).strftime("%Y%m%d")
    listing = _get_listing_with_progress(prog)
    valid   = listing[listing["Marcap"] >= MIN_CAP].copy()
    prog["total"] = len(valid); prog["status"] = "running"
    rows = _run_screen_parallel(valid, _screen1_ticker, start, date_str, prog)
    prog["status"] = "done"
    if not rows: return pd.DataFrame()
    return (pd.DataFrame(rows)
            [["시장","종목코드","종목명","시총(억)","종가","전일대비(%)","VL","VL대비(%)","EMA60","EMA200","구간"]]
            .sort_values("VL대비(%)")
            .reset_index(drop=True))

# ══════════════════════════════════════════════════════════════════════════════
# 조건2: 금일급락 (조건1 + 전일대비 -5% 초과 하락)
# ══════════════════════════════════════════════════════════════════════════════

def _screen2_ticker(code, start, end):
    try:
        df = fetch_ohlcv(code, start, end)
        if df is None or len(df) < EMA200 + LR_PERIOD*2 + 5: return None
        c_s = df["Close"].astype(float)
        e60s = ema(c_s, EMA60); e200s = ema(c_s, EMA200)
        A = linreg(c_s, LR_PERIOD); vl = A + (A - linreg(A, LR_PERIOD))
        c  = c_s.iloc[-1];  c1 = c_s.iloc[-2]
        v  = vl.iloc[-1];   v1 = vl.iloc[-2]
        if pd.isna(v) or pd.isna(v1): return None
        if e200s.iloc[-2] >= e200s.iloc[-1]: return None  # 필터2: EMA200 비상승
        if c <= e60s.iloc[-1]: return None                # 필터3: 종가 < EMA60
        if v1 >= v: return None                           # 필터5: VL 비상승
        day_chg = (c - c1) / c1 * 100
        if day_chg > -5: return None                      # 필터6: 전일대비 -5% 미달
        if v <= c: return None                            # 필터4: VL < 종가
        drop = (c - v) / v * 100
        if drop > ZONES[0]: return None
        zone = (f"{ZONES[0]}%~{ZONES[1]}%" if drop > ZONES[1] else
                f"{ZONES[1]}%~{ZONES[2]}%" if drop > ZONES[2] else
                f"{ZONES[2]}% 이하")
        return {"종목코드": code, "종가": int(c), "VL": round(v,2),
                "VL대비(%)": round(drop,2), "전일대비(%)": round(day_chg,2),
                "EMA60": round(e60s.iloc[-1],2), "EMA200": round(e200s.iloc[-1],2),
                "구간": zone}
    except Exception:
        return None

def run_screen2(date_str, prog):
    prog.update({"current":0,"total":0,"status":"loading"})
    start = (datetime.strptime(date_str,"%Y%m%d")-timedelta(days=LOOKBACK)).strftime("%Y%m%d")
    listing = _get_listing_with_progress(prog)
    valid   = listing[listing["Marcap"] >= MIN_CAP].copy()
    prog["total"] = len(valid); prog["status"] = "running"
    rows = _run_screen_parallel(valid, _screen2_ticker, start, date_str, prog)
    prog["status"] = "done"
    if not rows: return pd.DataFrame()
    return (pd.DataFrame(rows)
            [["시장","종목코드","종목명","시총(억)","종가","전일대비(%)","VL","VL대비(%)","EMA60","EMA200","구간"]]
            .sort_values("전일대비(%)")
            .reset_index(drop=True))

# ══════════════════════════════════════════════════════════════════════════════
# 조건3: 역사적스퀴즈 로직
# ══════════════════════════════════════════════════════════════════════════════

def _resample_weekly(df: pd.DataFrame) -> pd.DataFrame:
    """일봉 DataFrame → 주봉 OHLCV (월요일 기준)"""
    df_w = df.resample("W-MON", label="left", closed="left").agg({
        "Open":   "first",
        "High":   "max",
        "Low":    "min",
        "Close":  "last",
        "Volume": "sum",
    }).dropna()
    return df_w


def _resample_monthly(df: pd.DataFrame) -> pd.DataFrame:
    """일봉 DataFrame → 월봉 OHLCV (월 시작일 기준, 현재 진행 중인 달 포함)"""
    df_m = df.resample("MS").agg({
        "Open":   "first",
        "High":   "max",
        "Low":    "min",
        "Close":  "last",
        "Volume": "sum",
    }).dropna(subset=["Close"])
    return df_m


def _screen3_ticker(code, start, end):
    try:
        df = fetch_ohlcv(code, start, end)
        if df is None or len(df) < 300:
            return None

        close_d = df["Close"].astype(float)

        # 일봉 EMA200 상승 확인
        ema200d = close_d.ewm(span=200, adjust=False).mean()
        if len(ema200d) < 2:
            return None
        if ema200d.iloc[-1] <= ema200d.iloc[-2]:
            return None

        # 주봉 변환
        df_w = _resample_weekly(df)
        if len(df_w) < 72:   # 52주 관찰 + 20주 이평 여유
            return None

        vol_w     = df_w["Volume"].astype(float)
        vol_ma20  = vol_w.rolling(window=20).mean()
        vol_std20 = vol_w.rolling(window=20).std()
        vol_bw    = (vol_ma20 + vol_std20 * 2) - (vol_ma20 - vol_std20 * 2)  # = 4 * std

        cur_bw      = vol_bw.iloc[-1]
        lookback_bw = vol_bw.iloc[-52:]

        if pd.isna(cur_bw) or lookback_bw.isna().all():
            return None

        min_bw = lookback_bw.min()
        if cur_bw > min_bw + 1e-9:   # 52주 최솟값이 아니면 제외
            return None

        avg_bw        = lookback_bw.mean()
        squeeze_ratio = round((cur_bw / avg_bw) * 100, 1) if avg_bw != 0 else 0.0

        c    = int(close_d.iloc[-1])
        e200 = round(ema200d.iloc[-1], 2)
        return {
            "종목코드":     code,
            "종가":        c,
            "EMA200":     e200,
            "스퀴즈비율(%)": squeeze_ratio,
        }
    except Exception:
        return None


def run_screen3(date_str, prog):
    prog.update({"current": 0, "total": 0, "status": "loading"})
    start   = (datetime.strptime(date_str, "%Y%m%d") - timedelta(days=LOOKBACK)).strftime("%Y%m%d")
    listing = _get_listing_with_progress(prog)
    valid   = listing[listing["Marcap"] >= MIN_CAP].copy()
    prog["total"] = len(valid); prog["status"] = "running"
    rows = _run_screen_parallel(valid, _screen3_ticker, start, date_str, prog)
    prog["status"] = "done"
    if not rows:
        return pd.DataFrame()
    return (pd.DataFrame(rows)
            [["시장", "종목코드", "종목명", "시총(억)", "종가", "EMA200", "스퀴즈비율(%)"]]
            .sort_values("스퀴즈비율(%)")   # 낮을수록 스퀴즈 강도 강함
            .reset_index(drop=True))


# 스크리너 ID → 실행 함수 매핑
# ══════════════════════════════════════════════════════════════════════════════
# 조건4: 거래량점수 로직
# ══════════════════════════════════════════════════════════════════════════════

def _score4(df) -> float | None:
    """가격·거래량 관계 점수 반환 (0이면 제외)"""
    if df is None or len(df) < 201:
        return None

    vol_s   = df["Volume"].astype(float)
    close_s = df["Close"].astype(float)

    # 조건1: 20일 거래량 MA 3일 연속 상승
    v_ma20 = vol_s.rolling(window=20).mean()
    if len(v_ma20) < 4:
        return None
    va = v_ma20.iloc[-4:].values          # [v_a0_oldest … v_a3_latest]
    if not (va[1] > va[0] and va[2] > va[1] and va[3] > va[2]):
        return None

    # 조건2: EMA200 3일 연속 상승
    ema200 = close_s.ewm(span=200, adjust=False).mean()
    if len(ema200) < 4:
        return None
    ea = ema200.iloc[-4:].values
    if not (ea[1] > ea[0] and ea[2] > ea[1] and ea[3] > ea[2]):
        return None

    c       = close_s.iloc[-1]
    c_prev  = close_s.iloc[-2]
    v       = vol_s.iloc[-1]
    v_prev  = vol_s.iloc[-2]
    v_ref   = vol_s.iloc[-21:-1].mean()   # 오늘 제외 20일 평균

    score = 0.0
    if   v > v_prev and c_prev <= c <= c_prev * 1.01:  score =  2.0
    elif v > v_prev and c < c_prev:                     score = -2.0
    elif c >= c_prev * 1.03 and v >= v_ref * 2.0:       score =  1.5
    elif abs(v - v_ref) / max(1, v_ref) < 0.2 and c > c_prev: score = 1.0
    elif v < v_ref * 0.8 and c > c_prev:                score =  0.5

    # 가점: 최근 20일 내 거래량 폭발 이력
    if len(vol_s) >= 20:
        recent_vol = vol_s.iloc[-20:]
        recent_ma  = v_ma20.iloc[-20:]
        if (recent_vol >= 2 * recent_ma).any():
            score += 1.0

    return score if score != 0 else None


def _screen4_ticker(code, start, end):
    try:
        df    = fetch_ohlcv(code, start, end)
        score = _score4(df)
        if score is None:
            return None

        close_s = df["Close"].astype(float)
        vol_s   = df["Volume"].astype(float)
        v_ref   = vol_s.iloc[-21:-1].mean()
        c       = int(close_s.iloc[-1])
        c_prev  = close_s.iloc[-2]
        day_chg = round((c - c_prev) / c_prev * 100, 2)

        return {
            "종목코드":    code,
            "종가":       c,
            "전일대비(%)": day_chg,
            "거래량":     int(vol_s.iloc[-1]),
            "20일평균":   int(v_ref),
            "점수":       round(score, 1),
        }
    except Exception:
        return None


def run_screen4(date_str, prog):
    prog.update({"current": 0, "total": 0, "status": "loading"})
    start   = (datetime.strptime(date_str, "%Y%m%d") - timedelta(days=LOOKBACK)).strftime("%Y%m%d")
    listing = _get_listing_with_progress(prog)
    valid   = listing[listing["Marcap"] >= MIN_CAP].copy()
    prog["total"] = len(valid); prog["status"] = "running"
    rows = _run_screen_parallel(valid, _screen4_ticker, start, date_str, prog)
    prog["status"] = "done"
    if not rows:
        return pd.DataFrame()
    return (pd.DataFrame(rows)
            [["시장", "종목코드", "종목명", "시총(억)", "종가", "전일대비(%)", "거래량", "20일평균", "점수"]]
            .sort_values("점수", ascending=False)
            .reset_index(drop=True))


# ══════════════════════════════════════════════════════════════════════════════
# 조건5: ASGMA 로직
# ══════════════════════════════════════════════════════════════════════════════

def _screen5_ticker(code, start, end):
    try:
        df = fetch_ohlcv(code, start, end)
        if df is None or len(df) < 50:
            return None

        # [0] 5일 평균 거래량 30만주 이상 (금일 제외)
        avg_vol_5d = df["Volume"].iloc[-6:-1].mean()
        if avg_vol_5d < 300_000:
            return None

        # [1] Volume Ratio (14일)
        d14 = df.tail(14)
        up_vol   = d14[d14["Close"] > d14["Open"]]["Volume"].sum()
        down_vol = d14[d14["Close"] < d14["Open"]]["Volume"].sum()
        total_vol = d14["Volume"].sum()
        vr14 = (up_vol - down_vol) / total_vol if total_vol != 0 else 0

        # [2] ATR(14) ratio
        hi, lo, pc = df["High"], df["Low"], df["Close"].shift(1)
        tr    = pd.concat([hi - lo, (hi - pc).abs(), (lo - pc).abs()], axis=1).max(axis=1)
        atr14 = tr.rolling(window=14).mean().iloc[-1]
        c     = df["Close"].iloc[-1]
        if c == 0:
            return None
        atr_ratio = atr14 / c * 100

        # [3] ASGMA 점수 ≥ 3
        score = vr14 * atr_ratio
        if score < 3:
            return None

        # [4] 기준봉 체크 (최근 15일 이내)
        d15 = df.tail(15).copy()
        d15["co_rate"] = (d15["Close"] - d15["Open"]) / d15["Open"] * 100   # 몸통
        d15["ch_rate"] = (d15["Close"] - d15["High"]) / d15["High"] * 100   # 윗꼬리
        d15["lh_rate"] = (d15["High"]  - d15["Low"])  / d15["Low"]  * 100   # 전체 진폭

        stand_bar = (d15["Close"] > d15["Open"]) & (
            (d15["co_rate"] >= 12) |
            ((d15["ch_rate"] >= -4) & (d15["lh_rate"] >= 18))
        )
        if not stand_bar.any():
            return None

        c_prev  = df["Close"].iloc[-2]
        day_chg = round((c - c_prev) / c_prev * 100, 2)

        return {
            "종목코드":    code,
            "종가":       int(c),
            "전일대비(%)": day_chg,
            "VR14":      round(vr14, 4),
            "ATR비율(%)": round(atr_ratio, 2),
            "ASGMA점수":  round(score, 4),
        }
    except Exception:
        return None


def run_screen5(date_str, prog):
    prog.update({"current": 0, "total": 0, "status": "loading"})
    start   = (datetime.strptime(date_str, "%Y%m%d") - timedelta(days=LOOKBACK)).strftime("%Y%m%d")
    listing = _get_listing_with_progress(prog)
    valid   = listing[listing["Marcap"] >= MIN_CAP].copy()
    prog["total"] = len(valid); prog["status"] = "running"
    rows = _run_screen_parallel(valid, _screen5_ticker, start, date_str, prog)
    prog["status"] = "done"
    if not rows:
        return pd.DataFrame()
    return (pd.DataFrame(rows)
            [["시장", "종목코드", "종목명", "시총(억)", "종가", "전일대비(%)", "VR14", "ATR비율(%)", "ASGMA점수"]]
            .sort_values("ASGMA점수", ascending=False)
            .reset_index(drop=True))


# ══════════════════════════════════════════════════════════════════════════════
# 조건6: 폭발직전 (ASGMA ≥ 3.0 + 최근 3일 |등락폭| ≤ ATR14 × 70%)
# ══════════════════════════════════════════════════════════════════════════════

def _screen6_ticker(code, start, end):
    try:
        df = fetch_ohlcv(code, start, end)
        if df is None or len(df) < 50:
            return None

        # ── 조건5 ASGMA 점수 계산 (재사용) ───────────────────────────────────
        avg_vol_5d = df["Volume"].iloc[-6:-1].mean()
        if avg_vol_5d < 300_000:
            return None

        d14 = df.tail(14)
        up_vol   = d14[d14["Close"] > d14["Open"]]["Volume"].sum()
        down_vol = d14[d14["Close"] < d14["Open"]]["Volume"].sum()
        total_vol = d14["Volume"].sum()
        vr14 = (up_vol - down_vol) / total_vol if total_vol != 0 else 0

        hi, lo, pc = df["High"], df["Low"], df["Close"].shift(1)
        tr    = pd.concat([hi - lo, (hi - pc).abs(), (lo - pc).abs()], axis=1).max(axis=1)
        atr14 = tr.rolling(window=14).mean().iloc[-1]
        c     = df["Close"].iloc[-1]
        if c == 0 or pd.isna(atr14) or atr14 == 0:
            return None
        atr_ratio = atr14 / c * 100
        score     = vr14 * atr_ratio
        if score < 3.0:
            return None

        d15 = df.tail(15).copy()
        d15["co_rate"] = (d15["Close"] - d15["Open"]) / d15["Open"] * 100
        d15["ch_rate"] = (d15["Close"] - d15["High"]) / d15["High"] * 100
        d15["lh_rate"] = (d15["High"]  - d15["Low"])  / d15["Low"]  * 100
        stand_bar = (d15["Close"] > d15["Open"]) & (
            (d15["co_rate"] >= 12) |
            ((d15["ch_rate"] >= -4) & (d15["lh_rate"] >= 18))
        )
        if not stand_bar.any():
            return None

        # ── 조건6 추가 필터: 최근 3일 |등락폭| ≤ ATR14 × 70% ────────────────
        closes = df["Close"].values.astype(float)
        threshold = atr14 * 0.70
        for i in range(-1, -4, -1):               # 최근 3일
            day_move = abs(closes[i] - closes[i - 1])
            if day_move > threshold:
                return None

        c_prev  = df["Close"].iloc[-2]
        day_chg = round((c - c_prev) / c_prev * 100, 2)

        # 최근 3일 등락폭 / ATR 비율 (참고용)
        ratios = [
            round(abs(closes[i] - closes[i-1]) / atr14 * 100, 1)
            for i in range(-1, -4, -1)
        ]

        return {
            "종목코드":      code,
            "종가":         int(c),
            "전일대비(%)":  day_chg,
            "ASGMA점수":   round(score, 4),
            "ATR14":       round(atr14, 2),
            "3일등락/ATR(%)": f"{ratios[0]} / {ratios[1]} / {ratios[2]}",
        }
    except Exception:
        return None


def run_screen6(date_str, prog):
    prog.update({"current": 0, "total": 0, "status": "loading"})
    start   = (datetime.strptime(date_str, "%Y%m%d") - timedelta(days=LOOKBACK)).strftime("%Y%m%d")
    listing = _get_listing_with_progress(prog)
    valid   = listing[listing["Marcap"] >= MIN_CAP].copy()
    prog["total"] = len(valid); prog["status"] = "running"
    rows = _run_screen_parallel(valid, _screen6_ticker, start, date_str, prog)
    prog["status"] = "done"
    if not rows:
        return pd.DataFrame()
    return (pd.DataFrame(rows)
            [["시장", "종목코드", "종목명", "시총(억)", "종가", "전일대비(%)",
              "ASGMA점수", "ATR14", "3일등락/ATR(%)"]]
            .sort_values("ASGMA점수", ascending=False)
            .reset_index(drop=True))


# ══════════════════════════════════════════════════════════════════════════════
# 조건7: 폭발준비 (ASGMA ≤ 1 눌림 + 기준봉 + SMA 상향돌파)
# ══════════════════════════════════════════════════════════════════════════════

def _screen7_ticker(code, start, end):
    try:
        df = fetch_ohlcv(code, start, end)
        if df is None or len(df) < 50:
            return None

        close = df["Close"].astype(float)

        # [0] 5일 평균 거래량 30만주 이상 (금일 제외)
        avg_vol_5d = df["Volume"].iloc[-6:-1].mean()
        if avg_vol_5d < 300_000:
            return None

        # [1] ASGMA Score 계산 → ≤ 1 (눌림 구간)
        d14 = df.tail(14)
        up_vol   = d14[d14["Close"] > d14["Open"]]["Volume"].sum()
        down_vol = d14[d14["Close"] < d14["Open"]]["Volume"].sum()
        total_vol = d14["Volume"].sum()
        vr14 = (up_vol - down_vol) / total_vol if total_vol != 0 else 0

        hi, lo, pc = df["High"], df["Low"], close.shift(1)
        tr    = pd.concat([hi - lo, (hi - pc).abs(), (lo - pc).abs()], axis=1).max(axis=1)
        atr14 = tr.rolling(window=14).mean().iloc[-1]
        c     = close.iloc[-1]
        if c == 0 or pd.isna(atr14):
            return None
        atr_ratio = atr14 / c * 100
        score     = vr14 * atr_ratio

        if score > 1:
            return None

        # [2] 기준봉 체크 (최근 15일 이내)
        d15 = df.tail(15).copy()
        d15["co_rate"] = (d15["Close"] - d15["Open"]) / d15["Open"] * 100
        d15["ch_rate"] = (d15["Close"] - d15["High"]) / d15["High"] * 100
        d15["lh_rate"] = (d15["High"]  - d15["Low"])  / d15["Low"]  * 100
        stand_bar = (d15["Close"] > d15["Open"]) & (
            (d15["co_rate"] >= 12) |
            ((d15["ch_rate"] >= -4) & (d15["lh_rate"] >= 18))
        )
        if not stand_bar.any():
            return None

        # [3] SMA5 / SMA20 상향 돌파 체크
        sma5  = close.rolling(window=5).mean()
        sma20 = close.rolling(window=20).mean()

        cross5  = (close.iloc[-2] < sma5.iloc[-2])  and (close.iloc[-1] >= sma5.iloc[-1])
        cross20 = (close.iloc[-2] < sma20.iloc[-2]) and (close.iloc[-1] >= sma20.iloc[-1])

        if not (cross5 or cross20):
            return None

        breakthrough = ("SMA5+SMA20" if (cross5 and cross20)
                        else "SMA5"  if cross5 else "SMA20")

        c_prev  = close.iloc[-2]
        day_chg = round((c - c_prev) / c_prev * 100, 2)

        return {
            "종목코드":    code,
            "종가":       int(c),
            "전일대비(%)": day_chg,
            "ASGMA점수":  round(score, 4),
            "돌파":       breakthrough,
            "SMA5":       round(sma5.iloc[-1], 0),
            "SMA20":      round(sma20.iloc[-1], 0),
        }
    except Exception:
        return None


def run_screen7(date_str, prog):
    prog.update({"current": 0, "total": 0, "status": "loading"})
    start   = (datetime.strptime(date_str, "%Y%m%d") - timedelta(days=LOOKBACK)).strftime("%Y%m%d")
    listing = _get_listing_with_progress(prog)
    valid   = listing[listing["Marcap"] >= MIN_CAP].copy()
    prog["total"] = len(valid); prog["status"] = "running"
    rows = _run_screen_parallel(valid, _screen7_ticker, start, date_str, prog)
    prog["status"] = "done"
    if not rows:
        return pd.DataFrame()
    return (pd.DataFrame(rows)
            [["시장", "종목코드", "종목명", "시총(억)", "종가", "전일대비(%)",
              "ASGMA점수", "돌파", "SMA5", "SMA20"]]
            .sort_values("전일대비(%)", ascending=False)
            .reset_index(drop=True))


# ══════════════════════════════════════════════════════════════════════════════
# 조건8: 하이킨아시 주봉 신규 돌파
# ══════════════════════════════════════════════════════════════════════════════

def _calc_heikin_ashi(df_w: pd.DataFrame) -> pd.DataFrame:
    """주봉 OHLCV → 하이킨아시 OHLC"""
    ha = pd.DataFrame(index=df_w.index)
    op = df_w["Open"].values.astype(float)
    hi = df_w["High"].values.astype(float)
    lo = df_w["Low"].values.astype(float)
    cl = df_w["Close"].values.astype(float)

    ha_close = (op + hi + lo + cl) / 4
    ha_open  = np.empty(len(df_w))
    ha_open[0] = (op[0] + cl[0]) / 2
    for i in range(1, len(df_w)):
        ha_open[i] = (ha_open[i-1] + ha_close[i-1]) / 2

    ha["Open"]  = ha_open
    ha["Close"] = ha_close
    ha["High"]  = np.maximum.reduce([hi, ha_open, ha_close])
    ha["Low"]   = np.minimum.reduce([lo, ha_open, ha_close])
    return ha


def _eval_ha_breakout(df_daily: pd.DataFrame) -> tuple[bool, float]:
    """
    하이킨아시 주봉 돌파 판별
    반환: (돌파여부, 저항선가격)
    """
    df_w = _resample_weekly(df_daily)
    if len(df_w) < 11:
        return False, 0.0

    # 현재 주봉 양봉 확인
    if df_w["Close"].iloc[-1] <= df_w["Open"].iloc[-1]:
        return False, 0.0

    # 하이킨아시 계산
    ha_w       = _calc_heikin_ashi(df_w)
    last10_ha  = ha_w.iloc[-11:-1]                              # 이전 10주
    bearish_ha = last10_ha[last10_ha["Close"] < last10_ha["Open"]]

    if bearish_ha.empty:
        return False, 0.0

    resistance = bearish_ha["Open"].max()                       # 음봉 시가 최대값 = 저항선
    if df_w["Close"].iloc[-1] > resistance:
        return True, float(resistance)
    return False, 0.0


def _screen8_ticker(code, start, end):
    try:
        df = fetch_ohlcv(code, start, end)
        if df is None or len(df) < 15:
            return None

        # [0] 5일 평균 거래량 10만주 이상 (금일 제외)
        avg_vol_5d = df["Volume"].iloc[-6:-1].mean()
        if avg_vol_5d < 100_000:
            return None

        # [1] 오늘 기준 돌파 여부
        is_today, resistance = _eval_ha_breakout(df)
        if not is_today:
            return None

        # [2] 신규 돌파 확인 (어제 기준으론 미충족)
        is_yesterday, _ = _eval_ha_breakout(df.iloc[:-1])
        if is_yesterday:
            return None   # 이미 전일에도 충족 → 신규 아님

        c      = float(df["Close"].iloc[-1])
        c_prev = float(df["Close"].iloc[-2])
        day_chg = round((c - c_prev) / c_prev * 100, 2)
        res_gap = round((c - resistance) / resistance * 100, 2)  # 저항선 대비 종가 위치

        return {
            "종목코드":      code,
            "종가":         int(c),
            "전일대비(%)":  day_chg,
            "저항선":       int(resistance),
            "저항선대비(%)": res_gap,
        }
    except Exception:
        return None


def run_screen8(date_str, prog):
    prog.update({"current": 0, "total": 0, "status": "loading"})
    start   = (datetime.strptime(date_str, "%Y%m%d") - timedelta(days=LOOKBACK)).strftime("%Y%m%d")
    listing = _get_listing_with_progress(prog)
    valid   = listing[listing["Marcap"] >= MIN_CAP].copy()
    prog["total"] = len(valid); prog["status"] = "running"
    rows = _run_screen_parallel(valid, _screen8_ticker, start, date_str, prog)
    prog["status"] = "done"
    if not rows:
        return pd.DataFrame()
    return (pd.DataFrame(rows)
            [["시장", "종목코드", "종목명", "시총(억)", "종가", "전일대비(%)", "저항선", "저항선대비(%)"]]
            .sort_values("저항선대비(%)", ascending=False)
            .reset_index(drop=True))


# ══════════════════════════════════════════════════════════════════════════════
# 조건9: 비율골든크로스
# ══════════════════════════════════════════════════════════════════════════════

def _screen9_ticker(code, start, end):
    try:
        df = fetch_ohlcv(code, start, end)
        if df is None or len(df) < 111:
            return None

        close_s = df["Close"].astype(float)
        open_s  = df["Open"].astype(float)
        high_s  = df["High"].astype(float)
        low_s   = df["Low"].astype(float)
        vol_s   = df["Volume"].astype(float)
        n = len(df)

        # [1] 5일 평균 거래량 10만주 이상 (금일 제외)
        if vol_s.iloc[-6:-1].mean() < 100_000:
            return None

        # [2] 최근 20일 내 기준봉 찾기 (진폭 12%↑, 윗꼬리 6%미만 양봉)
        base_idx = None
        for i in range(n - 1, max(0, n - 21), -1):
            if low_s.iloc[i] == 0:
                continue
            is_yang = close_s.iloc[i] > open_s.iloc[i]
            v_ratio = (high_s.iloc[i] - low_s.iloc[i]) / low_s.iloc[i] * 100.0
            t_ratio = ((high_s.iloc[i] - close_s.iloc[i]) / close_s.iloc[i] * 100.0
                       if close_s.iloc[i] != 0 else 999.0)
            if is_yang and v_ratio >= 12.0 and t_ratio < 6.0:
                base_idx = i
                break

        if base_idx is None or (n - 1) < base_idx + 5:
            return None

        # [3] EMA20 이격 확인 (과열 제외)
        ema20 = close_s.ewm(span=20, adjust=False).mean()
        if low_s.iloc[-1] > ema20.iloc[-1] * 1.20:
            return None

        # [4] 양음 거래대금 비율 계산
        tv     = ((open_s + high_s + low_s + close_s) / 4.0) * vol_s
        yang_m = (close_s > open_s).astype(float)
        eum_m  = (close_s < open_s).astype(float)

        # 90일 롤링 비율 (기준선)
        b_90 = (tv * yang_m).rolling(window=90).sum()
        e_90 = (tv * eum_m).rolling(window=90).sum()
        r_90 = b_90 / e_90.replace(0, 1)

        if pd.isna(r_90.iloc[-1]) or pd.isna(r_90.iloc[-2]):
            return None

        # 기준봉 이후 현재까지 누적 비율 (오늘)
        yang_cur  = (tv.iloc[base_idx:] * yang_m.iloc[base_idx:]).sum()
        eum_cur   = (tv.iloc[base_idx:] * eum_m.iloc[base_idx:]).sum()
        r_cum_cur = yang_cur / (eum_cur if eum_cur > 0 else 1.0)

        # 기준봉 이후 어제까지 누적 비율
        yang_prev  = (tv.iloc[base_idx:-1] * yang_m.iloc[base_idx:-1]).sum()
        eum_prev   = (tv.iloc[base_idx:-1] * eum_m.iloc[base_idx:-1]).sum()
        r_cum_prev = yang_prev / (eum_prev if eum_prev > 0 else 1.0)

        # 골든크로스 조건: 어제 < 90일선, 오늘 >= 90일선
        if not (r_cum_prev < r_90.iloc[-2] and r_cum_cur >= r_90.iloc[-1]):
            return None

        c       = float(close_s.iloc[-1])
        c_prev  = float(close_s.iloc[-2])
        day_chg = round((c - c_prev) / c_prev * 100, 2)

        # 기준봉 날짜 (MM/DD)
        bidx_ts = df.index[base_idx]
        base_date = (bidx_ts.strftime("%m/%d")
                     if hasattr(bidx_ts, "strftime") else str(bidx_ts)[:10])

        return {
            "종목코드":    code,
            "종가":       int(c),
            "전일대비(%)": day_chg,
            "누적비율":   round(float(r_cum_cur), 4),
            "90일비율":   round(float(r_90.iloc[-1]), 4),
            "기준봉":     base_date,
        }
    except Exception:
        return None


def run_screen9(date_str, prog):
    prog.update({"current": 0, "total": 0, "status": "loading"})
    start   = (datetime.strptime(date_str, "%Y%m%d") - timedelta(days=LOOKBACK)).strftime("%Y%m%d")
    listing = _get_listing_with_progress(prog)
    valid   = listing[listing["Marcap"] >= MIN_CAP].copy()
    prog["total"] = len(valid); prog["status"] = "running"
    rows = _run_screen_parallel(valid, _screen9_ticker, start, date_str, prog)
    prog["status"] = "done"
    if not rows:
        return pd.DataFrame()
    return (pd.DataFrame(rows)
            [["시장", "종목코드", "종목명", "시총(억)", "종가", "전일대비(%)",
              "누적비율", "90일비율", "기준봉"]]
            .sort_values("누적비율", ascending=False)
            .reset_index(drop=True))


# ══════════════════════════════════════════════════════════════════════════════
# 조건10: 엔벨로프눌림
# ══════════════════════════════════════════════════════════════════════════════

def _screen10_ticker(code, start, end):
    try:
        df = fetch_ohlcv(code, start, end)
        min_len = LR_PERIOD * 2 + 20 + 30 + 10   # linreg×2 + sma20 + 30봉 + 여유
        if df is None or len(df) < min_len:
            return None

        close_s = df["Close"].astype(float)
        open_s  = df["Open"].astype(float)
        high_s  = df["High"].astype(float)

        # [1] 30봉 내 (시가→고가) 등락률 15%↑ 캔들 존재
        d30     = df.tail(30)
        up_rate = (d30["High"].astype(float) - d30["Open"].astype(float)) / d30["Open"].astype(float) * 100
        if not (up_rate >= 15.0).any():
            return None

        # [2] 최근 7거래일 이내 Envelope(20, 40%) 상단 터치/돌파
        sma20     = close_s.rolling(window=20).mean()
        env_upper = sma20 * 1.40
        if len(high_s) < 8 or pd.isna(env_upper.iloc[-1]):
            return None
        if not any(high_s.iloc[i] >= env_upper.iloc[i] for i in range(-1, -8, -1)):
            return None

        # [3] VL 계산 (A=linreg(C,50), A1=linreg(A,50), VL=A+(A-A1))
        A  = linreg(close_s, LR_PERIOD)
        A1 = linreg(A,       LR_PERIOD)
        vl = A + (A - A1)
        v  = vl.iloc[-1]
        if pd.isna(v) or v == 0:
            return None

        # [4] 금일 종가가 VL ±3% 이내
        c      = float(close_s.iloc[-1])
        vl_gap = (c - v) / v * 100          # 음수 = VL 아래, 양수 = VL 위
        if abs(vl_gap) > 3.0:
            return None

        c_prev  = float(close_s.iloc[-2])
        day_chg = round((c - c_prev) / c_prev * 100, 2)

        # 엔벨로프 상단 (오늘 기준)
        env_top = round(float(env_upper.iloc[-1]), 0)

        # 엔벨로프상단이 VL 기준 몇 % 위인지
        env_vl_gap = round((env_top - v) / v * 100, 2)

        return {
            "종목코드":         code,
            "종가":            int(c),
            "전일대비(%)":     day_chg,
            "VL":              round(v, 2),
            "VL대비(%)":       round(vl_gap, 2),
            "엔벨로프상단":     int(env_top),
            "엔벨로프VL차(%)": env_vl_gap,
        }
    except Exception:
        return None


def run_screen10(date_str, prog):
    prog.update({"current": 0, "total": 0, "status": "loading"})
    start   = (datetime.strptime(date_str, "%Y%m%d") - timedelta(days=LOOKBACK)).strftime("%Y%m%d")
    listing = _get_listing_with_progress(prog)
    valid   = listing[listing["Marcap"] >= MIN_CAP].copy()
    prog["total"] = len(valid); prog["status"] = "running"
    rows = _run_screen_parallel(valid, _screen10_ticker, start, date_str, prog)
    prog["status"] = "done"
    if not rows:
        return pd.DataFrame()
    df_res = pd.DataFrame(rows)[["시장", "종목코드", "종목명", "시총(억)", "종가",
                                  "전일대비(%)", "VL", "VL대비(%)", "엔벨로프상단", "엔벨로프VL차(%)"]]
    # VL에 가장 근접한 순서로 정렬 (|VL대비(%)| 오름차순)
    df_res["_abs"] = df_res["VL대비(%)"].abs()
    df_res = df_res.sort_values("_abs").drop(columns="_abs").reset_index(drop=True)
    return df_res


# ══════════════════════════════════════════════════════════════════════════════
# 조건11: VL매수타점
# ══════════════════════════════════════════════════════════════════════════════

def _calc_세력평단(df: pd.DataFrame) -> pd.Series:
    """
    세력평단 = EMA(캔들, 20)
    캔들: V > 1.5×MA(V,60) and C>O 일 때 (C+O)/2, 아니면 마지막 유효값 유지 (valuewhen)
    """
    close_s  = df["Close"].astype(float)
    open_s   = df["Open"].astype(float)
    vol_s    = df["Volume"].astype(float)

    vol_ma60 = vol_s.rolling(window=60).mean()
    cond     = (vol_s > 1.5 * vol_ma60) & (close_s > open_s)
    mid      = (close_s + open_s) / 2.0

    # valuewhen(1, cond, mid) → 조건 충족 시 mid 값을 앞으로 carry-forward
    candle_vals = np.where(cond, mid, np.nan)
    candle = pd.Series(candle_vals, index=df.index).ffill()   # forward-fill

    return candle.ewm(span=20, adjust=False).mean()


def _screen11_ticker(code, start, end):
    try:
        df = fetch_ohlcv(code, start, end)
        min_len = LR_PERIOD * 2 + 20 + 30 + 10
        if df is None or len(df) < min_len:
            return None

        close_s = df["Close"].astype(float)
        high_s  = df["High"].astype(float)

        # Envelope(20, 40%) 상단
        sma20     = close_s.rolling(window=20).mean()
        env_upper = sma20 * 1.40
        if pd.isna(env_upper.iloc[-1]):
            return None

        # [1] 최근 60일 내 고점 또는 종가가 Envelope 상단을 돌파한 적 1회 이상
        d60_hi  = high_s.iloc[-60:]
        d60_cl  = close_s.iloc[-60:]
        d60_env = env_upper.iloc[-60:]
        if not ((d60_hi >= d60_env) | (d60_cl >= d60_env)).any():
            return None

        # VL 계산 (A=linreg(C,50), A1=linreg(A,50), VL=A+(A-A1))
        A  = linreg(close_s, LR_PERIOD)
        A1 = linreg(A,       LR_PERIOD)
        vl = A + (A - A1)

        v  = vl.iloc[-1]    # VL   (오늘)
        v1 = vl.iloc[-2]    # VL(1) (1일 전)
        v2 = vl.iloc[-3]    # VL(2) (2일 전)
        v3 = vl.iloc[-4]    # VL(3) (3일 전)
        v4 = vl.iloc[-5]    # VL(4) (4일 전)
        v5 = vl.iloc[-6]    # VL(5) (5일 전)
        if any(pd.isna(x) for x in [v, v1, v2, v3, v4, v5]) or v == 0:
            return None

        # [2] (VL(4) or VL(5)) > (VL(2) or VL(3)) < (VL(1) or VL)
        #   → 중간 구간(2~3일 전)이 골짜기(저점), 양쪽이 높은 확장형 V자 반전
        bottom     = min(v2, v3)          # 중간 저점
        left_peak  = max(v4, v5)          # 왼쪽 고점
        right_peak = max(v1, v)           # 오른쪽 고점 (오늘·어제)
        if not (left_peak > bottom and right_peak > bottom):
            return None

        # [2-1] VL ~ VL(5) 6일간 변동폭 최소 1.5% 이상
        vl_6 = [v, v1, v2, v3, v4, v5]
        vl_hi  = max(vl_6)
        vl_lo  = min(vl_6)
        vl_range = (vl_hi - vl_lo) / vl_lo * 100 if vl_lo != 0 else 0.0
        if vl_range < 1.5:
            return None

        c = float(close_s.iloc[-1])

        # [3] 종가 > VL × 1.10 (VL 대비 10% 이상 위)
        if c <= v * 1.10:
            return None

        # [4] 최근 5일간 종가 변동 -10% ~ +10% 이내 (횡보 눌림 확인)
        if len(close_s) < 6:
            return None
        c_5d_ago = float(close_s.iloc[-6])   # 5거래일 전 종가 (오늘 제외 기준)
        if c_5d_ago == 0:
            return None
        chg_5d = (c - c_5d_ago) / c_5d_ago * 100
        if not (-10.0 <= chg_5d <= 10.0):
            return None

        # [5] 세력평단 계산 및 전일 대비 하락 확인
        sp = _calc_세력평단(df)
        sp_today = sp.iloc[-1]
        sp_prev  = sp.iloc[-2]
        if pd.isna(sp_today) or pd.isna(sp_prev):
            return None
        if sp_today >= sp_prev:          # 오늘 세력평단 >= 어제 → 통과 안 됨
            return None

        c_prev       = float(close_s.iloc[-2])
        day_chg      = round((c - c_prev) / c_prev * 100, 2)
        vl_gap       = round((c - v) / v * 100, 2)          # 종가/VL 괴리 (%)
        env_top      = float(env_upper.iloc[-1])
        env_vl_gap   = round((env_top - v) / v * 100, 2)    # 엔벨로프상단/VL 괴리 (%)
        c_to_env     = round((env_top - c) / c * 100, 2)    # 종가→엔벨로프상단 여력 (%)

        sp_chg = round((float(sp_today) - float(sp_prev)) / float(sp_prev) * 100, 2)

        return {
            "종목코드":         code,
            "종가":            int(c),
            "전일대비(%)":     day_chg,
            "5일변동(%)":      round(chg_5d, 2),
            "VL":              round(v, 2),
            "VL6일변동(%)":    round(vl_range, 2),
            "종가VL갭(%)":     vl_gap,        # 유일 식별 컬럼 (조건10과 구분)
            "엔벨로프상단":     int(env_top),
            "엔벨로프VL차(%)": env_vl_gap,
            "상단여력(%)":     c_to_env,
            "세력평단":        round(float(sp_today), 0),
            "세력평단변화(%)": sp_chg,
        }
    except Exception:
        return None


def run_screen11(date_str, prog):
    prog.update({"current": 0, "total": 0, "status": "loading"})
    start   = (datetime.strptime(date_str, "%Y%m%d") - timedelta(days=LOOKBACK)).strftime("%Y%m%d")
    listing = _get_listing_with_progress(prog)
    valid   = listing[listing["Marcap"] >= MIN_CAP].copy()
    prog["total"] = len(valid); prog["status"] = "running"
    rows = _run_screen_parallel(valid, _screen11_ticker, start, date_str, prog)
    prog["status"] = "done"
    if not rows:
        return pd.DataFrame()
    return (pd.DataFrame(rows)
            [["시장", "종목코드", "종목명", "시총(억)", "종가", "전일대비(%)",
              "5일변동(%)", "VL", "VL6일변동(%)", "종가VL갭(%)",
              "엔벨로프상단", "엔벨로프VL차(%)", "상단여력(%)",
              "세력평단", "세력평단변화(%)"]]
            .sort_values("종가VL갭(%)")      # VL 대비 갭 작은 순 (매수타점 가까운 순)
            .reset_index(drop=True))


# ══════════════════════════════════════════════════════════════════════════════
# 조건12: Harvard RSI
# ══════════════════════════════════════════════════════════════════════════════

_HR_RSI_PERIOD  = 21
_HR_BAND_LEN    = 34
_HR_TSL         = 2       # lengthtradesl (A의 평균 기간)
_HR_COEF        = 1.6185  # 하단밴드 계수


def _calc_rsi(close_s: pd.Series, period: int) -> pd.Series:
    """Wilder RSI"""
    delta    = close_s.diff()
    gain     = delta.clip(lower=0)
    loss     = (-delta).clip(lower=0)
    avg_gain = gain.ewm(com=period - 1, adjust=False).mean()
    avg_loss = loss.ewm(com=period - 1, adjust=False).mean()
    rs = avg_gain / avg_loss.replace(0, 1e-10)
    return 100 - (100 / (1 + rs))


def _screen12_ticker(code, start, end):
    try:
        df = fetch_ohlcv(code, start, end)
        min_len = _HR_RSI_PERIOD + _HR_BAND_LEN + _HR_TSL + 15
        if df is None or len(df) < min_len:
            return None

        close_s = df["Close"].astype(float)

        # RSI(21)
        rsi_s = _calc_rsi(close_s, _HR_RSI_PERIOD)

        # A = avg(RSI, lengthtradesl=2)  →  SMA(RSI, 2)
        A = rsi_s.rolling(window=_HR_TSL).mean()

        # B = avg(RSI, bandLength=34) − 1.6185 × stdev(RSI, bandLength=34)
        rsi_ma  = rsi_s.rolling(window=_HR_BAND_LEN).mean()
        rsi_std = rsi_s.rolling(window=_HR_BAND_LEN).std(ddof=1)
        B = rsi_ma - _HR_COEF * rsi_std

        a0, a1 = A.iloc[-1], A.iloc[-2]
        a4, a5 = A.iloc[-5], A.iloc[-6]
        b0, b1 = B.iloc[-1], B.iloc[-2]
        b4, b5 = B.iloc[-5], B.iloc[-6]

        if any(pd.isna(x) for x in [a0, a1, a4, a5, b0, b1, b4, b5]):
            return None

        # (A(4)>B(4) or A(5)>B(5))  →  B(1)>A(1)  →  A>B
        if not ((a4 > b4 or a5 > b5) and b1 > a1 and a0 > b0):
            return None

        c       = float(close_s.iloc[-1])
        c_prev  = float(close_s.iloc[-2])
        day_chg = round((c - c_prev) / c_prev * 100, 2)

        return {
            "종목코드":    code,
            "종가":       int(c),
            "전일대비(%)": day_chg,
            "RSI(21)":   round(float(rsi_s.iloc[-1]), 2),
            "A":          round(float(a0), 2),
            "B":          round(float(b0), 2),
            "A-B":        round(float(a0 - b0), 2),
        }
    except Exception:
        return None


def run_screen12(date_str, prog):
    prog.update({"current": 0, "total": 0, "status": "loading"})
    start   = (datetime.strptime(date_str, "%Y%m%d") - timedelta(days=LOOKBACK)).strftime("%Y%m%d")
    listing = _get_listing_with_progress(prog)
    valid   = listing[listing["Marcap"] >= MIN_CAP].copy()
    prog["total"] = len(valid); prog["status"] = "running"
    rows = _run_screen_parallel(valid, _screen12_ticker, start, date_str, prog)
    prog["status"] = "done"
    if not rows:
        return pd.DataFrame()
    return (pd.DataFrame(rows)
            [["시장", "종목코드", "종목명", "시총(억)", "종가", "전일대비(%)",
              "RSI(21)", "A", "B", "A-B"]]
            .sort_values("A-B", ascending=False)
            .reset_index(drop=True))


# ══════════════════════════════════════════════════════════════════════════════
# 조건13: 이제출발
# ══════════════════════════════════════════════════════════════════════════════

MIN_CAP13 = 300_000_000_000   # 3000억


def _screen13_ticker(code, start, end):
    try:
        df = fetch_ohlcv(code, start, end)
        # sma20 + 30일 전고점 확인용 여유
        if df is None or len(df) < 20 + 30 + 10 + 5:
            return None

        close_s = df["Close"].astype(float)
        high_s  = df["High"].astype(float)

        # Envelope(20, 40%) 상단
        sma20     = close_s.rolling(window=20).mean()
        env_upper = sma20 * 1.40
        if pd.isna(env_upper.iloc[-1]):
            return None

        # [1] 30일 전 envelope 상단 대비 현재 상단의 상승폭이 5% 이하
        if len(env_upper) < 31:
            return None
        env_30d_ago = float(env_upper.iloc[-30])   # 30일 전 값
        env_cur     = float(env_upper.iloc[-1])
        if env_30d_ago == 0:
            return None
        env_rise = (env_cur - env_30d_ago) / env_30d_ago * 100   # 양수=상승, 음수=하락
        if env_rise > 5.0:         # 5% 초과 상승이면 제외
            return None

        # [2] 최근 10일 내 고가가 envelope 상단의 3% 이내 접근 or 돌파
        d10_high = high_s.iloc[-10:]
        d10_env  = env_upper.iloc[-10:]
        # 고가 / 상단 >= 0.97  →  상단 3% 아래까지 접근했거나 돌파
        approach = (d10_high / d10_env.replace(0, np.nan)).dropna()
        if not (approach >= 0.97).any():
            return None

        # 최근 10일 중 가장 가까이 접근했던 비율 (1.0 이상 = 돌파)
        best_ratio = float(approach.max())
        best_gap   = round((best_ratio - 1.0) * 100, 2)   # 양수=돌파, 음수=미돌파

        c       = float(close_s.iloc[-1])
        c_prev  = float(close_s.iloc[-2])
        day_chg = round((c - c_prev) / c_prev * 100, 2)

        return {
            "종목코드":       code,
            "종가":          int(c),
            "전일대비(%)":   day_chg,
            "엔벨로프상단":     int(env_cur),
            "엔벨로프30일상승(%)": round(env_rise, 2),   # 유일 식별 컬럼
            "최근접근(%)":     best_gap,               # 양수=돌파, 음수=접근
        }
    except Exception:
        return None


def run_screen13(date_str, prog):
    prog.update({"current": 0, "total": 0, "status": "loading"})
    start   = (datetime.strptime(date_str, "%Y%m%d") - timedelta(days=LOOKBACK)).strftime("%Y%m%d")
    listing = _get_listing_with_progress(prog)
    valid   = listing[listing["Marcap"] >= MIN_CAP13].copy()   # 3000억 이상
    prog["total"] = len(valid); prog["status"] = "running"
    rows = _run_screen_parallel(valid, _screen13_ticker, start, date_str, prog)
    prog["status"] = "done"
    if not rows:
        return pd.DataFrame()
    return (pd.DataFrame(rows)
            [["시장", "종목코드", "종목명", "시총(억)", "종가", "전일대비(%)",
              "엔벨로프상단", "엔벨로프30일상승(%)", "최근접근(%)"]]
            .sort_values("최근접근(%)", ascending=False)   # 가장 강하게 접근/돌파한 순
            .reset_index(drop=True))


# ══════════════════════════════════════════════════════════════════════════════
# 조건14: 세력20평단 돌파
# ══════════════════════════════════════════════════════════════════════════════

def _screen14_ticker(code, start, end):
    try:
        # KIS API 우선 사용, FDR 폴백
        df = None
        if _kis:
            try:
                df = _kis.daily_ohlcv(code, n_bars=250)
                if df is not None and len(df) >= 60 + 20 + 5:
                    # 오늘 실시간 현재가 overlay (마지막 바 갱신)
                    try:
                        rt = _kis.current_price(code)
                        if rt and rt.get("close", 0) > 0:
                            today_ts = pd.Timestamp(datetime.now().date())
                            df.loc[today_ts, "Open"]   = rt["open"]
                            df.loc[today_ts, "High"]   = rt["high"]
                            df.loc[today_ts, "Low"]    = rt["low"]
                            df.loc[today_ts, "Close"]  = rt["close"]
                            df.loc[today_ts, "Volume"] = rt["volume"]
                            df = df.sort_index()
                    except Exception:
                        pass
                else:
                    df = None
            except Exception as e:
                _plog(f"[KIS] _screen14_ticker({code}) 예외: {e}")
                df = None
        if df is None:
            df = fetch_ohlcv(code, start, end)
        if df is None or len(df) < 60 + 20 + 5:
            return None

        close_s = df["Close"].astype(float)
        vol_s   = df["Volume"].astype(float)

        # 세력20평단 (조건11과 동일 로직 재사용)
        sp      = _calc_세력평단(df)
        sp_now  = sp.iloc[-1]
        sp_prev = sp.iloc[-2]
        if pd.isna(sp_now) or pd.isna(sp_prev) or sp_now == 0:
            return None

        c      = float(close_s.iloc[-1])
        c_prev = float(close_s.iloc[-2])

        # [1] 세력20평단 신규 돌파: 어제 종가 ≤ 어제 평단, 오늘 종가 > 오늘 평단
        if not (c_prev <= float(sp_prev) and c > float(sp_now)):
            return None

        # [2] 전일 동시간대 대비 거래량 200% 이상
        v_today = float(vol_s.iloc[-1])
        v_prev  = float(vol_s.iloc[-2])
        if v_prev == 0:
            return None
        now       = datetime.now()
        mkt_open  = now.replace(hour=9,  minute=0,  second=0, microsecond=0)
        mkt_close = now.replace(hour=15, minute=30, second=0, microsecond=0)
        total_sec = (mkt_close - mkt_open).total_seconds()
        elapsed   = (now - mkt_open).total_seconds()
        scale     = max(min(elapsed / total_sec, 1.0), 0.01)
        vol_ratio = v_today / (v_prev * scale) * 100
        if vol_ratio < 200.0:
            return None

        day_chg  = round((c - c_prev) / c_prev * 100, 2)
        sp_gap   = round((c - float(sp_now)) / float(sp_now) * 100, 2)

        return {
            "종목코드":      code,
            "종가":         int(c),
            "전일대비(%)":  day_chg,
            "세력20평단":   int(round(float(sp_now), 0)),   # 유일 식별 컬럼
            "평단대비(%)":  sp_gap,
            "거래량비율(%)": round(vol_ratio, 1),
        }
    except Exception:
        return None


def run_screen14(date_str, prog):
    t_scan_start = datetime.now()
    prog.update({"current": 0, "total": 0, "status": "loading"})
    start   = (datetime.strptime(date_str, "%Y%m%d") - timedelta(days=LOOKBACK)).strftime("%Y%m%d")
    listing = _get_listing_with_progress(prog)
    valid   = listing[listing["Marcap"] >= MIN_CAP13].copy()   # 3000억 이상
    prog["total"] = len(valid); prog["status"] = "running"
    rows = _run_screen_parallel(valid, _screen14_ticker, start, date_str, prog)
    t_scan_end = datetime.now()
    elapsed    = int((t_scan_end - t_scan_start).total_seconds())
    prog["status"] = "done"
    if not rows:
        return pd.DataFrame()
    df = (pd.DataFrame(rows)
          [["시장", "종목코드", "종목명", "시총(억)", "종가", "전일대비(%)",
            "세력20평단", "평단대비(%)", "거래량비율(%)"]]
          .sort_values("거래량비율(%)", ascending=False)
          .reset_index(drop=True))
    # 검색 시각 / 소요 시간 열 추가
    df["검색시각"] = t_scan_end.strftime("%Y-%m-%d %H:%M:%S")
    df["소요(초)"] = elapsed
    return df


SCAN_INTERVAL_SEC = 180   # 실시간 재스캔 주기 (초) — 기본 3분


def _run_realtime14():
    """조건14 실시간 반복 스캔 루프"""
    global _rt14_scan_no, _rt14_scan_start, _rt14_last_scan, _rt14_next_scan, _rt14_scan_elapsed
    st = _state[14]
    print("[REALTIME14] 실시간 스캔 시작")
    while st["realtime"]:
        _rt14_scan_no += 1
        t_start   = datetime.now()
        today_str = t_start.strftime("%Y%m%d")
        _rt14_scan_start = t_start.strftime("%H:%M:%S")
        print(f"[REALTIME14] #{_rt14_scan_no} 스캔 시작 ({today_str}  {_rt14_scan_start})")
        try:
            st["progress"] = {"current": 0, "total": 0, "status": "loading"}
            df = run_screen14(today_str, st["progress"])

            t_end = datetime.now()
            _rt14_last_scan    = t_end.strftime("%H:%M:%S")
            _rt14_scan_elapsed = int((t_end - t_start).total_seconds())

            if not df.empty:
                cur_codes = set(df["종목코드"].tolist())
                new       = cur_codes - st["known_codes"]
                st["new_codes"] = new
                st["known_codes"].update(cur_codes)

                # 기존 종목의 검색시각(최초 발견 시각) 유지 ──────────────────
                prev_df = st["result_df"]
                if not prev_df.empty and "검색시각" in prev_df.columns:
                    # 이전 결과에서 종목코드→검색시각 매핑
                    prev_ts = prev_df.set_index("종목코드")["검색시각"].to_dict()
                    # 신규 종목은 이번 스캔 시각, 기존 종목은 원래 시각 유지
                    df["검색시각"] = df["종목코드"].map(
                        lambda c: prev_ts.get(c, df.loc[df["종목코드"] == c, "검색시각"].iloc[0])
                    )

                st["result_df"]   = df
                st["result_date"] = today_str

                if new:
                    new_df = df[df["종목코드"].isin(new)]
                    print(f"[REALTIME14] 신규 {len(new)}개: {list(new)}")
                    # ── 이메일 발송 ──────────────────────────────────────────
                    alert_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                    lines = [
                        f"알림 시간: {alert_time}",
                        f"기준일:   {today_str}",
                        f"스캔 횟수: {_rt14_scan_no}회차",
                        ""
                    ]
                    for _, r in new_df.iterrows():
                        # 검색시각 컬럼이 있으면 사용, 없으면 현재 시각
                        found_at = r.get("검색시각", alert_time) if hasattr(r, "get") else alert_time
                        try:
                            found_at = str(r["검색시각"])
                        except (KeyError, TypeError):
                            found_at = alert_time
                        lines += [
                            f"▶ {r['종목명']} ({r['종목코드']}) [{r['시장']}]",
                            f"   검색시각: {found_at}",
                            f"   종가: {int(r['종가']):,}원 | 전일대비: {r['전일대비(%)']}%",
                            f"   세력20평단: {int(r['세력20평단']):,}원 | 평단대비: +{r['평단대비(%)']}%",
                            f"   거래량비율: {r['거래량비율(%)']}%", ""
                        ]
                    ok, err = _send_email_alert(
                        f"[세력20평단 돌파] 신규 {len(new)}개 종목 ({datetime.now().strftime('%H:%M')})",
                        "\n".join(lines)
                    )
                    if not ok:
                        print(f"[REALTIME14] 이메일 발송 실패: {err}", flush=True)
            else:
                st["new_codes"] = set()

        except Exception as e:
            print(f"[REALTIME14] 오류: {e}")
            st["progress"]["status"] = "done"

        # 다음 스캔까지 대기 (중지 신호 즉시 반응)
        next_dt = datetime.now() + timedelta(seconds=SCAN_INTERVAL_SEC)
        _rt14_next_scan = next_dt.strftime("%H:%M:%S")
        for _ in range(SCAN_INTERVAL_SEC):
            if not st["realtime"]:
                break
            time.sleep(1)

    st["progress"]["status"] = "idle"
    _rt14_next_scan = ""
    print("[REALTIME14] 실시간 스캔 종료")


# ══════════════════════════════════════════════════════════════════════════════
# 조건15: 세력20평단임박
# ══════════════════════════════════════════════════════════════════════════════

def _screen15_ticker(code, start, end):
    try:
        # KIS API 우선, FDR 폴백 (조건14와 동일 패턴)
        df = None
        if _kis:
            try:
                df = _kis.daily_ohlcv(code, n_bars=250)
                if df is not None and len(df) >= LR_PERIOD * 2 + 60 + 5:
                    try:
                        rt = _kis.current_price(code)
                        if rt and rt.get("close", 0) > 0:
                            today_ts = pd.Timestamp(datetime.now().date())
                            df.loc[today_ts, "Open"]   = rt["open"]
                            df.loc[today_ts, "High"]   = rt["high"]
                            df.loc[today_ts, "Low"]    = rt["low"]
                            df.loc[today_ts, "Close"]  = rt["close"]
                            df.loc[today_ts, "Volume"] = rt["volume"]
                            df = df.sort_index()
                    except Exception:
                        pass
                else:
                    df = None
            except Exception:
                df = None
        if df is None:
            df = fetch_ohlcv(code, start, end)
        if df is None or len(df) < LR_PERIOD * 2 + 60 + 5:
            return None

        close_s = df["Close"].astype(float)

        # ── 세력20평단 ─────────────────────────────────────────────────────
        sp      = _calc_세력평단(df)
        sp_now  = sp.iloc[-1]
        if pd.isna(sp_now) or sp_now == 0:
            return None

        c = float(close_s.iloc[-1])

        # [1] 종가 < 세력20평단 (아직 돌파 안 함)
        if c >= float(sp_now):
            return None

        # [2] (세력20평단 - 종가) / 세력20평단 < 5% → 5% 이내 임박
        gap_pct = (float(sp_now) - c) / float(sp_now) * 100
        if gap_pct >= 5.0:
            return None

        # ── VL (변동회귀선) ────────────────────────────────────────────────
        A   = linreg(close_s, LR_PERIOD)
        A1  = linreg(A,       LR_PERIOD)
        vl  = A + (A - A1)
        v   = vl.iloc[-1]
        v1  = vl.iloc[-2]

        if pd.isna(v) or pd.isna(v1) or v1 == 0:
            return None

        # [3] 종가 > VL
        if c <= v:
            return None

        # [4] VL 전일 대비 3% 이상 상승
        vl_chg_pct = (v - v1) / abs(v1) * 100
        if vl_chg_pct < 3.0:
            return None

        c_prev = float(close_s.iloc[-2])
        day_chg = round((c - c_prev) / c_prev * 100, 2) if c_prev else 0.0

        return {
            "종목코드":       code,
            "종가":          int(c),
            "전일대비(%)":   day_chg,
            "세력20평단":    int(round(float(sp_now), 0)),
            "평단까지(%)":   round(gap_pct, 2),       # 평단까지 남은 %
            "VL":            round(v, 2),
            "VL상승(%)":     round(vl_chg_pct, 2),    # VL 전일 대비 상승률
        }
    except Exception:
        return None


def run_screen15(date_str, prog):
    t_scan_start = datetime.now()
    prog.update({"current": 0, "total": 0, "status": "loading"})
    start   = (datetime.strptime(date_str, "%Y%m%d") - timedelta(days=LOOKBACK)).strftime("%Y%m%d")
    listing = _get_listing_with_progress(prog)
    valid   = listing[listing["Marcap"] >= MIN_CAP13].copy()   # 3000억 이상
    prog["total"] = len(valid); prog["status"] = "running"
    rows = _run_screen_parallel(valid, _screen15_ticker, start, date_str, prog)
    t_scan_end = datetime.now()
    elapsed    = int((t_scan_end - t_scan_start).total_seconds())
    prog["status"] = "done"
    if not rows:
        return pd.DataFrame()
    df = (pd.DataFrame(rows)
          [["시장", "종목코드", "종목명", "시총(억)", "종가", "전일대비(%)",
            "세력20평단", "평단까지(%)", "VL", "VL상승(%)"]]
          .sort_values("평단까지(%)")          # 평단과 가까울수록 위로
          .reset_index(drop=True))
    df["검색시각"] = t_scan_end.strftime("%Y-%m-%d %H:%M:%S")
    df["소요(초)"] = elapsed
    return df


# ══════════════════════════════════════════════════════════════════════════════
# 조건16: RSI밴드 압축돌파
# UPPER = SMA(RSI(21),34) + 1.6185×stdev(RSI(21),34)
# LOWER = SMA(RSI(21),34) − 1.6185×stdev(RSI(21),34)
# S_RSI = SMA(RSI(21), 2)
# 조건: ① 3일 연속 밴드폭(UPPER-LOWER) 감소
#       ② 당일 S_RSI > UPPER (전일 S_RSI ≤ 전일 UPPER)  ← 상향돌파
# ══════════════════════════════════════════════════════════════════════════════

def _screen16_ticker(code, start, end):
    try:
        df = fetch_ohlcv(code, start, end)
        min_len = _HR_RSI_PERIOD + _HR_BAND_LEN + _HR_TSL + 10
        if df is None or len(df) < min_len:
            return None

        close_s = df["Close"].astype(float)

        # RSI(21)
        rsi_s = _calc_rsi(close_s, _HR_RSI_PERIOD)

        # S_RSI = SMA(RSI, 2)
        s_rsi = rsi_s.rolling(window=_HR_TSL).mean()

        # 밴드: SMA(RSI, 34) ± 1.6185 × stdev(RSI, 34)
        rsi_ma  = rsi_s.rolling(window=_HR_BAND_LEN).mean()
        rsi_std = rsi_s.rolling(window=_HR_BAND_LEN).std(ddof=1)
        upper   = rsi_ma + _HR_COEF * rsi_std
        lower   = rsi_ma - _HR_COEF * rsi_std
        bw      = upper - lower   # 밴드폭 = 2 × 1.6185 × stdev

        # 필요한 값 추출 (0=당일, 1=전일, 2=2일전, 3=3일전)
        u0, u1       = upper.iloc[-1],  upper.iloc[-2]
        s0, s1       = s_rsi.iloc[-1],  s_rsi.iloc[-2]
        bw0, bw1, bw2, bw3 = bw.iloc[-1], bw.iloc[-2], bw.iloc[-3], bw.iloc[-4]

        if any(pd.isna(x) for x in [u0, u1, s0, s1, bw0, bw1, bw2, bw3]):
            return None

        # [1] 전전전일 > 전전일 > 전일 (금일은 축소 여부 무관)
        if not (bw3 > bw2 > bw1):
            return None

        # [2] S_RSI 상향돌파: 전일 S_RSI ≤ 전일 UPPER, 당일 S_RSI > 당일 UPPER
        if not (s1 <= u1 and s0 > u0):
            return None

        c      = float(close_s.iloc[-1])
        c_prev = float(close_s.iloc[-2])
        day_chg = round((c - c_prev) / c_prev * 100, 2)

        return {
            "종목코드":     code,
            "종가":        int(c),
            "전일대비(%)": day_chg,
            "RSI(21)":    round(float(rsi_s.iloc[-1]), 2),
            "S_RSI":      round(float(s0), 2),
            "UPPER":      round(float(u0), 2),
            "LOWER":      round(float(lower.iloc[-1]), 2),
            "밴드폭":      round(float(bw0), 2),
            "밴드폭3일감소": round(float(bw3 - bw1), 2),  # 전전전일→전일 감소량 (금일 제외)
        }
    except Exception:
        return None


def run_screen16(date_str, prog):
    prog.update({"current": 0, "total": 0, "status": "loading"})
    start   = (datetime.strptime(date_str, "%Y%m%d") - timedelta(days=LOOKBACK)).strftime("%Y%m%d")
    listing = _get_listing_with_progress(prog)
    valid   = listing[listing["Marcap"] >= MIN_CAP].copy()   # 1500억 이상
    prog["total"] = len(valid); prog["status"] = "running"
    rows = _run_screen_parallel(valid, _screen16_ticker, start, date_str, prog)
    prog["status"] = "done"
    if not rows:
        return pd.DataFrame()
    return (pd.DataFrame(rows)
            [["시장", "종목코드", "종목명", "시총(억)", "종가", "전일대비(%)",
              "RSI(21)", "S_RSI", "UPPER", "LOWER", "밴드폭", "밴드폭3일감소"]]
            .sort_values("밴드폭3일감소", ascending=False)   # 가장 많이 압축된 순
            .reset_index(drop=True))


# ══════════════════════════════════════════════════════════════════════════════
# 조건17: MACD Reloaded
# MA12 = MA(C, 12, MAType)   MA26 = MA(C, 26, MAType)
# src2 = MA12 − MA26         MATR = MA(src2, 9, MAType)
# 기본 MAType = 10 (Hull MA),  T3a1 = 0.7
# 조건: CrossUp(src2, MATR)  AND  C > O  (양봉)
# ══════════════════════════════════════════════════════════════════════════════

_MR_LEN   = 12
_MR_LEN1  = 26
_MR_LEN2  = 9
_MR_T3A1  = 0.7
_MR_MTYPE = 10   # 기본값: HULL MA


def _screen17_ticker(code, start, end):
    try:
        df = fetch_ohlcv(code, start, end)
        # HULL(26)+HULL(9) 최소 워밍업: 26 + sqrt(26)≈5 + 9 + sqrt(9)=3 + 여유
        min_len = _MR_LEN1 + _MR_LEN2 + int(round(np.sqrt(_MR_LEN1))) + 30
        if df is None or len(df) < min_len:
            return None

        close  = df["Close"].astype(float)
        open_s = df["Open"].astype(float)

        ma12   = _ma_type(close, _MR_LEN,  _MR_MTYPE, _MR_T3A1)
        ma26   = _ma_type(close, _MR_LEN1, _MR_MTYPE, _MR_T3A1)
        src2   = ma12 - ma26
        signal = _ma_type(src2,  _MR_LEN2, _MR_MTYPE, _MR_T3A1)

        s0, s1       = float(src2.iloc[-1]),   float(src2.iloc[-2])
        sig0, sig1   = float(signal.iloc[-1]), float(signal.iloc[-2])

        if any(pd.isna(x) for x in [s0, s1, sig0, sig1]):
            return None

        # CrossUp(src2, MATR): 전일 src2 ≤ 전일 signal  AND  당일 src2 > 당일 signal
        if not (s1 <= sig1 and s0 > sig0):
            return None

        # 당일 양봉 (C > O)
        c = float(close.iloc[-1])
        o = float(open_s.iloc[-1])
        if c <= o:
            return None

        # 거래대금 1600억 이상 OR 거래량 200만주 이상
        vol     = float(df["Volume"].iloc[-1])
        tv      = c * vol                          # 거래대금 (원)
        if not (tv >= 160_000_000_000 or vol >= 2_000_000):
            return None

        c_prev  = float(close.iloc[-2])
        day_chg = round((c - c_prev) / c_prev * 100, 2) if c_prev else 0.0

        return {
            "종목코드":    code,
            "종가":       int(c),
            "전일대비(%)": day_chg,
            "MACD":      round(s0,   4),
            "Signal":    round(sig0, 4),
            "히스토그램":  round(s0  - sig0, 4),
            "전일히스토":  round(s1  - sig1, 4),
            "거래대금(억)": int(tv // 100_000_000),
            "거래량(만주)": round(vol / 10_000, 1),
        }
    except Exception:
        return None


def run_screen17(date_str, prog):
    prog.update({"current": 0, "total": 0, "status": "loading"})
    start   = (datetime.strptime(date_str, "%Y%m%d") - timedelta(days=LOOKBACK)).strftime("%Y%m%d")
    listing = _get_listing_with_progress(prog)
    valid   = listing[listing["Marcap"] >= MIN_CAP].copy()   # 1500억 이상
    prog["total"] = len(valid); prog["status"] = "running"
    rows = _run_screen_parallel(valid, _screen17_ticker, start, date_str, prog)
    prog["status"] = "done"
    if not rows:
        return pd.DataFrame()
    return (pd.DataFrame(rows)
            [["시장", "종목코드", "종목명", "시총(억)", "종가", "전일대비(%)",
              "거래대금(억)", "거래량(만주)", "MACD", "Signal", "히스토그램", "전일히스토"]]
            .sort_values("히스토그램", ascending=False)
            .reset_index(drop=True))


# ══════════════════════════════════════════════════════════════════════════════
# 조건18: MACD Reloaded2
# 조건17과 동일 MACD 계산 (HULL MA 12/26/9)
# 신호: hist = src2 - MATR_val
#       hist >= 0  AND  hist > hist[1]  (라임 그린 = 히스토그램 양수·증가 중)
#       AND C > O (양봉)
#       AND (거래대금 ≥ 1600억 OR 거래량 ≥ 200만주)
# ══════════════════════════════════════════════════════════════════════════════

def _screen18_ticker(code, start, end):
    try:
        df = fetch_ohlcv(code, start, end)
        min_len = _MR_LEN1 + _MR_LEN2 + int(round(np.sqrt(_MR_LEN1))) + 30
        if df is None or len(df) < min_len:
            return None

        close  = df["Close"].astype(float)
        open_s = df["Open"].astype(float)

        ma12   = _ma_type(close, _MR_LEN,  _MR_MTYPE, _MR_T3A1)
        ma26   = _ma_type(close, _MR_LEN1, _MR_MTYPE, _MR_T3A1)
        src2   = ma12 - ma26
        signal = _ma_type(src2,  _MR_LEN2, _MR_MTYPE, _MR_T3A1)
        hist   = src2 - signal

        h0, h1 = float(hist.iloc[-1]), float(hist.iloc[-2])
        s0      = float(src2.iloc[-1])
        sig0    = float(signal.iloc[-1])

        if any(pd.isna(x) for x in [h0, h1, s0, sig0]):
            return None

        # 라임 그린 조건: hist >= 0  AND  hist > hist[1]
        if not (h0 >= 0 and h0 > h1):
            return None

        # 당일 양봉 (C > O)
        c = float(close.iloc[-1])
        o = float(open_s.iloc[-1])
        if c <= o:
            return None

        # 거래대금 1600억 이상 OR 거래량 200만주 이상
        vol = float(df["Volume"].iloc[-1])
        tv  = c * vol
        if not (tv >= 160_000_000_000 or vol >= 2_000_000):
            return None

        c_prev  = float(close.iloc[-2])
        day_chg = round((c - c_prev) / c_prev * 100, 2) if c_prev else 0.0

        # 히스토그램 컬러 판별
        if h0 >= 0:
            hist_color = "라임" if h0 > h1 else "청록"
        else:
            hist_color = "연분홍" if h0 > h1 else "진빨강"

        return {
            "종목코드":    code,
            "종가":       int(c),
            "전일대비(%)": day_chg,
            "MACD":      round(s0,   4),
            "Signal":    round(sig0, 4),
            "히스토그램":  round(h0,  4),
            "전일히스토":  round(h1,  4),
            "히스트색":   hist_color,
            "거래대금(억)": int(tv // 100_000_000),
            "거래량(만주)": round(vol / 10_000, 1),
        }
    except Exception:
        return None


def run_screen18(date_str, prog):
    prog.update({"current": 0, "total": 0, "status": "loading"})
    start   = (datetime.strptime(date_str, "%Y%m%d") - timedelta(days=LOOKBACK)).strftime("%Y%m%d")
    listing = _get_listing_with_progress(prog)
    valid   = listing[listing["Marcap"] >= MIN_CAP].copy()   # 1500억 이상
    prog["total"] = len(valid); prog["status"] = "running"
    rows = _run_screen_parallel(valid, _screen18_ticker, start, date_str, prog)
    prog["status"] = "done"
    if not rows:
        return pd.DataFrame()
    return (pd.DataFrame(rows)
            [["시장", "종목코드", "종목명", "시총(억)", "종가", "전일대비(%)",
              "거래대금(억)", "거래량(만주)",
              "MACD", "Signal", "히스토그램", "전일히스토", "히스트색"]]
            .sort_values("히스토그램", ascending=False)
            .reset_index(drop=True))


# ══════════════════════════════════════════════════════════════════════════════
# 조건19: 급락회귀선
# VL = A + (A - A1),  A = linreg(C,50),  A1 = linreg(A,50)
# 조건①: 최근 10거래일 중 (C-VL)/VL ≥ 10% 인 날이 4거래일 이상  ← 고점 이력
# 조건②: 현재 종가 > VL                                   ← 회귀선 회복
# 조건③: 이번 주 저점(최근 5일) > 지난 주 저점(6~10일 전)  ← 주봉 저점 상승
# 조건④: VL 최근 6일 중 앞선 3일 하락/횡보, 최근 3일 상승  ← VL 반등 전환
# ══════════════════════════════════════════════════════════════════════════════

def _screen19_ticker(code, start, end):
    try:
        df = fetch_ohlcv(code, start, end)
        min_len = LR_PERIOD * 2 + 15    # linreg×2 + 2주 여유
        if df is None or len(df) < min_len:
            return None

        close_s = df["Close"].astype(float)
        low_s   = df["Low"].astype(float)

        # VL 계산
        A  = linreg(close_s, LR_PERIOD)
        A1 = linreg(A,       LR_PERIOD)
        vl = A + (A - A1)

        v0 = float(vl.iloc[-1])
        c0 = float(close_s.iloc[-1])
        if pd.isna(v0) or v0 == 0:
            return None

        # [조건②] 현재 종가 > VL (회귀선 위에서 거래)
        if c0 <= v0:
            return None

        # [조건①] 최근 10거래일 중 (C-VL)/VL ≥ 10% 인 거래일이 4일 이상
        n_bars  = min(10, len(close_s))
        close_w = close_s.iloc[-n_bars:]
        vl_w    = vl.iloc[-n_bars:]
        if (vl_w == 0).all():
            return None
        gap_pct  = (close_w - vl_w) / vl_w * 100   # 양수 = C가 VL 위
        above_cnt = int((gap_pct >= 10.0).sum())
        if above_cnt < 4:
            return None
        max_gap = float(gap_pct.max())   # 결과 표시용

        # [조건③] 주봉 저점 상승: 최근 5일 저점 > 이전 5일 저점
        if len(low_s) < 10:
            return None
        lw_this = float(low_s.iloc[-5:].min())
        lw_prev = float(low_s.iloc[-10:-5].min())
        if lw_this <= lw_prev:
            return None

        # [조건④] VL 최근 6일: 앞선 3일 하락/횡보 → 최근 3일 상승
        vl_early = vl.iloc[-6:-3]   # 앞선 3일
        vl_late  = vl.iloc[-3:]     # 최근 3일
        if float(vl_early.iloc[-1]) > float(vl_early.iloc[0]):   # 앞선 3일이 상승이면 제외
            return None
        if float(vl_late.iloc[-1]) <= float(vl_late.iloc[0]):    # 최근 3일이 상승이 아니면 제외
            return None

        c_prev  = float(close_s.iloc[-2])
        day_chg = round((c0 - c_prev) / c_prev * 100, 2) if c_prev else 0.0
        vl_gap  = round((c0 - v0) / v0 * 100, 2)   # 현재 종가가 VL 대비 몇 % 위

        return {
            "종목코드":      code,
            "종가":         int(c0),
            "전일대비(%)":  day_chg,
            "VL":           round(v0, 2),
            "종가vsVL(%)":  vl_gap,
            "최대이탈(%)":  round(max_gap, 2),   # 2주내 VL 대비 최대 하락폭
            "이번주저점":    int(lw_this),
            "지난주저점":    int(lw_prev),
        }
    except Exception:
        return None


def run_screen19(date_str, prog):
    prog.update({"current": 0, "total": 0, "status": "loading"})
    start   = (datetime.strptime(date_str, "%Y%m%d") - timedelta(days=LOOKBACK)).strftime("%Y%m%d")
    listing = _get_listing_with_progress(prog)
    valid   = listing[listing["Marcap"] >= MIN_CAP].copy()   # 1500억 이상
    prog["total"] = len(valid); prog["status"] = "running"
    rows = _run_screen_parallel(valid, _screen19_ticker, start, date_str, prog)
    prog["status"] = "done"
    if not rows:
        return pd.DataFrame()
    return (pd.DataFrame(rows)
            [["시장", "종목코드", "종목명", "시총(억)", "종가", "전일대비(%)",
              "VL", "종가vsVL(%)", "최대이탈(%)", "이번주저점", "지난주저점"]]
            .sort_values("최대이탈(%)", ascending=False)   # 급락 폭 큰 순
            .reset_index(drop=True))


def _run_realtime19():
    """조건19 실시간 반복 스캔 루프"""
    global _rt19_scan_no, _rt19_scan_start, _rt19_last_scan, _rt19_next_scan, _rt19_scan_elapsed
    st = _state[19]
    print("[REALTIME19] 실시간 스캔 시작")
    while st["realtime"]:
        _rt19_scan_no += 1
        t_start   = datetime.now()
        today_str = t_start.strftime("%Y%m%d")
        _rt19_scan_start = t_start.strftime("%H:%M:%S")
        print(f"[REALTIME19] #{_rt19_scan_no} 스캔 시작 ({today_str}  {_rt19_scan_start})")
        try:
            st["progress"] = {"current": 0, "total": 0, "status": "loading"}
            df = run_screen19(today_str, st["progress"])

            t_end = datetime.now()
            _rt19_last_scan    = t_end.strftime("%H:%M:%S")
            _rt19_scan_elapsed = int((t_end - t_start).total_seconds())

            if not df.empty:
                cur_codes = set(df["종목코드"].tolist())
                new       = cur_codes - st["known_codes"]
                st["new_codes"] = new
                st["known_codes"].update(cur_codes)

                prev_df = st["result_df"]
                if not prev_df.empty and "검색시각" in prev_df.columns:
                    prev_ts = prev_df.set_index("종목코드")["검색시각"].to_dict()
                    df["검색시각"] = df["종목코드"].map(
                        lambda c: prev_ts.get(c, df.loc[df["종목코드"] == c, "검색시각"].iloc[0])
                    )

                st["result_df"]   = df
                st["result_date"] = today_str

                if new:
                    new_df = df[df["종목코드"].isin(new)]
                    print(f"[REALTIME19] 신규 {len(new)}개: {list(new)}")
                    alert_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                    lines = [
                        f"알림 시간: {alert_time}",
                        f"기준일:   {today_str}",
                        f"스캔 횟수: {_rt19_scan_no}회차",
                        ""
                    ]
                    for _, r in new_df.iterrows():
                        try:
                            found_at = str(r["검색시각"])
                        except (KeyError, TypeError):
                            found_at = alert_time
                        lines += [
                            f"▶ {r['종목명']} ({r['종목코드']}) [{r['시장']}]",
                            f"   검색시각: {found_at}",
                            f"   종가: {int(r['종가']):,}원 | 전일대비: {r['전일대비(%)']}%",
                            f"   VL: {round(r['VL'],0):,.0f}원 | 종가vsVL: +{r['종가vsVL(%)']}%",
                            f"   최대이탈: {r['최대이탈(%)']}%", ""
                        ]
                    ok, err = _send_email_alert(
                        f"[급락회귀선] 신규 {len(new)}개 종목 ({datetime.now().strftime('%H:%M')})",
                        "\n".join(lines)
                    )
                    if not ok:
                        print(f"[REALTIME19] 이메일 발송 실패: {err}", flush=True)
            else:
                st["new_codes"] = set()

        except Exception as e:
            print(f"[REALTIME19] 오류: {e}")
            st["progress"]["status"] = "done"

        next_dt = datetime.now() + timedelta(seconds=SCAN_INTERVAL_SEC)
        _rt19_next_scan = next_dt.strftime("%H:%M:%S")
        for _ in range(SCAN_INTERVAL_SEC):
            if not st["realtime"]:
                break
            time.sleep(1)

    st["progress"]["status"] = "idle"
    _rt19_next_scan = ""
    print("[REALTIME19] 실시간 스캔 종료")


# ══════════════════════════════════════════════════════════════════════════════
# 조건20: 전고점돌파
# 최근 60일 음봉(C<O) 중 시가(Open)가 가장 높은 가격 = 전고점 저항선
# 조건①: 금일 종가 > 전고점 저항선 (돌파)
# 조건②: 동시간대 거래량 전일대비 150% 이상 (= 전일 동시간 대비 1.5배)
# ══════════════════════════════════════════════════════════════════════════════

def _screen20_ticker(code, start, end):
    try:
        df = None
        if _kis:
            try:
                df = _kis.daily_ohlcv(code, n_bars=80)
            except Exception as e:
                _plog(f"[KIS] _screen20_ticker({code}) 예외: {e}")
                df = None
        if df is None:
            df = fetch_ohlcv(code, start, end)
        if df is None or len(df) < 62:
            return None

        # 당일 실시간 오버레이
        if _kis:
            try:
                cp = _kis.current_price(code)
                if cp:
                    end_dt = pd.Timestamp(end)
                    df.loc[end_dt] = [cp["open"], cp["high"], cp["low"],
                                      cp["close"], cp["volume"]]
                    df = df[~df.index.duplicated(keep="last")].sort_index()
            except Exception:
                pass

        close_s = df["Close"].astype(float)
        open_s  = df["Open"].astype(float)
        vol_s   = df["Volume"].astype(float)

        c0      = float(close_s.iloc[-1])
        v_today = float(vol_s.iloc[-1])
        v_prev  = float(vol_s.iloc[-2])
        if v_prev <= 0:
            return None

        # [조건①] 최근 60일 음봉 중 최고 시가 = 전고점 저항선
        hist         = df.iloc[-61:-1]
        bearish_mask = hist["Close"].astype(float) < hist["Open"].astype(float)
        bearish      = hist[bearish_mask]
        if bearish.empty:
            return None
        resistance = float(bearish["Open"].astype(float).max())
        if c0 <= resistance:
            return None

        # [조건②] 동시간대 거래량 전일대비 150% 이상
        now            = datetime.now()
        mkt_open       = now.replace(hour=9,  minute=0,  second=0, microsecond=0)
        mkt_close      = now.replace(hour=15, minute=30, second=0, microsecond=0)
        total_sec      = (mkt_close - mkt_open).total_seconds()
        elapsed        = (now - mkt_open).total_seconds()
        scale          = max(min(elapsed / total_sec, 1.0), 0.01) if total_sec > 0 else 1.0
        vol_ratio      = v_today / (v_prev * scale) * 100
        if vol_ratio < 250.0:
            return None

        c_prev  = float(close_s.iloc[-2])
        day_chg = round((c0 - c_prev) / c_prev * 100, 2) if c_prev else 0.0

        return {
            "종목코드":      code,
            "종가":         int(c0),
            "전일대비(%)":  day_chg,
            "전고점저항":   int(round(resistance, 0)),
            "거래량":       int(v_today),
            "거래량비율(%)": round(vol_ratio, 1),
        }
    except Exception:
        return None


def run_screen20(date_str, prog):
    prog.update({"current": 0, "total": 0, "status": "loading"})
    start   = (datetime.strptime(date_str, "%Y%m%d") - timedelta(days=LOOKBACK)).strftime("%Y%m%d")
    listing = _get_listing_with_progress(prog)
    valid   = listing[listing["Marcap"] >= MIN_CAP13].copy()   # 3000억 이상
    prog["total"] = len(valid); prog["status"] = "running"
    t_start = datetime.now()
    rows = _run_screen_parallel(valid, _screen20_ticker, start, date_str, prog)
    scan_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    for r in rows:
        r["검색시각"] = scan_time
    prog["status"] = "done"
    if not rows:
        return pd.DataFrame()
    return (pd.DataFrame(rows)
            [["시장", "종목코드", "종목명", "시총(억)", "종가", "전일대비(%)",
              "전고점저항", "거래량비율(%)", "거래량", "검색시각"]]
            .sort_values("거래량비율(%)", ascending=False)
            .reset_index(drop=True))


def _run_realtime20():
    """조건20 실시간 반복 스캔 루프"""
    global _rt20_scan_no, _rt20_scan_start, _rt20_last_scan, _rt20_next_scan, _rt20_scan_elapsed
    st = _state[20]
    print("[REALTIME20] 실시간 스캔 시작")
    while st["realtime"]:
        _rt20_scan_no += 1
        t_start   = datetime.now()
        today_str = t_start.strftime("%Y%m%d")
        _rt20_scan_start = t_start.strftime("%H:%M:%S")
        print(f"[REALTIME20] #{_rt20_scan_no} 스캔 시작 ({today_str}  {_rt20_scan_start})")
        try:
            st["progress"] = {"current": 0, "total": 0, "status": "loading"}
            df = run_screen20(today_str, st["progress"])

            t_end = datetime.now()
            _rt20_last_scan    = t_end.strftime("%H:%M:%S")
            _rt20_scan_elapsed = int((t_end - t_start).total_seconds())

            if not df.empty:
                cur_codes = set(df["종목코드"].tolist())
                new       = cur_codes - st["known_codes"]
                st["new_codes"] = new
                st["known_codes"].update(cur_codes)

                prev_df = st["result_df"]
                if not prev_df.empty and "검색시각" in prev_df.columns:
                    prev_ts = prev_df.set_index("종목코드")["검색시각"].to_dict()
                    df["검색시각"] = df["종목코드"].map(
                        lambda c: prev_ts.get(c, df.loc[df["종목코드"] == c, "검색시각"].iloc[0])
                    )

                st["result_df"]   = df
                st["result_date"] = today_str

                if new:
                    new_df = df[df["종목코드"].isin(new)]
                    print(f"[REALTIME20] 신규 {len(new)}개: {list(new)}")
                    alert_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                    lines = [
                        f"알림 시간: {alert_time}",
                        f"기준일:   {today_str}",
                        f"스캔 횟수: {_rt20_scan_no}회차",
                        ""
                    ]
                    for _, r in new_df.iterrows():
                        try:
                            found_at = str(r["검색시각"])
                        except (KeyError, TypeError):
                            found_at = alert_time
                        lines += [
                            f"▶ {r['종목명']} ({r['종목코드']}) [{r['시장']}]",
                            f"   검색시각: {found_at}",
                            f"   종가: {int(r['종가']):,}원 | 전일대비: {r['전일대비(%)']}%",
                            f"   전고점저항: {int(r['전고점저항']):,}원 | 거래량비율: {r['거래량비율(%)']}%", ""
                        ]
                    ok, err = _send_email_alert(
                        f"[전고점돌파] 신규 {len(new)}개 종목 ({datetime.now().strftime('%H:%M')})",
                        "\n".join(lines)
                    )
                    if not ok:
                        print(f"[REALTIME20] 이메일 발송 실패: {err}", flush=True)
            else:
                st["new_codes"] = set()

        except Exception as e:
            print(f"[REALTIME20] 오류: {e}")
            st["progress"]["status"] = "done"

        next_dt = datetime.now() + timedelta(seconds=SCAN_INTERVAL_SEC)
        _rt20_next_scan = next_dt.strftime("%H:%M:%S")
        for _ in range(SCAN_INTERVAL_SEC):
            if not st["realtime"]:
                break
            time.sleep(1)

    st["progress"]["status"] = "idle"
    _rt20_next_scan = ""
    print("[REALTIME20] 실시간 스캔 종료")


# ══════════════════════════════════════════════════════════════════════════════
# 조건22: 캔들볼륨
# 주봉 거래량 폭증(20주 거래량 MA × 1.5↑) 음봉을 저항선으로 정의
# 저항선 = 최근 26주 내 자격을 갖춘 음봉들의 시가 최댓값
# 조건①: 현재 주봉 종가 > 저항선 (신규 돌파)
# 조건②: 직전 주봉 종가 ≤ 저항선 (= 신규 돌파)
# 조건③: 현재 주봉 양봉(C>O) · 5일 평균거래량 10만↑
# ══════════════════════════════════════════════════════════════════════════════

def _eval_candle_volume_breakout(df_daily: pd.DataFrame) -> tuple[bool, float, float, int]:
    """
    캔들볼륨(거래량 폭증 음봉) 저항선 돌파 판별
    반환: (돌파여부, 저항선가격, 저항음봉 거래량비율, 저항음봉 시점인덱스)
    """
    df_w = _resample_weekly(df_daily)
    if len(df_w) < 27:
        return False, 0.0, 0.0, 0

    # 현재 주봉 양봉 확인
    cur_close = float(df_w["Close"].iloc[-1])
    cur_open  = float(df_w["Open"].iloc[-1])
    if cur_close <= cur_open:
        return False, 0.0, 0.0, 0

    vol_w     = df_w["Volume"].astype(float)
    vol_ma20  = vol_w.rolling(window=20).mean()

    # 최근 26주 (현재 주봉 제외) 영역에서 음봉 후보 탐색
    look_start = max(len(df_w) - 27, 20)        # 20주 MA 가용 시점부터
    look_end   = len(df_w) - 1                  # 직전 주봉까지

    open_w  = df_w["Open"].astype(float)
    close_w = df_w["Close"].astype(float)

    cand_open = []
    cand_ratio = []
    cand_idx   = []
    for i in range(look_start, look_end):
        ma = vol_ma20.iloc[i]
        if pd.isna(ma) or ma <= 0:
            continue
        ratio = vol_w.iloc[i] / ma
        if ratio < 1.5:
            continue
        if close_w.iloc[i] >= open_w.iloc[i]:
            continue                            # 음봉 아님
        cand_open.append(float(open_w.iloc[i]))
        cand_ratio.append(float(ratio))
        cand_idx.append(i)

    if not cand_open:
        return False, 0.0, 0.0, 0

    # 저항선 = 자격 음봉의 시가 최댓값
    res_idx_local = int(np.argmax(cand_open))
    resistance    = cand_open[res_idx_local]
    res_ratio     = cand_ratio[res_idx_local]
    res_idx       = cand_idx[res_idx_local]

    # 신규 돌파: 직전 주봉 종가 ≤ 저항선 < 현재 주봉 종가
    if cur_close <= resistance:
        return False, float(resistance), float(res_ratio), res_idx
    prev_close = float(close_w.iloc[-2])
    if prev_close > resistance:
        return False, float(resistance), float(res_ratio), res_idx

    return True, float(resistance), float(res_ratio), res_idx


def _screen22_ticker(code, start, end):
    try:
        df = fetch_ohlcv(code, start, end)
        if df is None or len(df) < 200:
            return None

        # [0] 5일 평균 거래량 10만주 이상 (금일 제외)
        avg_vol_5d = df["Volume"].iloc[-6:-1].mean()
        if avg_vol_5d < 100_000:
            return None

        # [1] 캔들볼륨 저항선 신규 돌파
        ok, resistance, vol_ratio, _res_idx = _eval_candle_volume_breakout(df)
        if not ok:
            return None

        c       = float(df["Close"].iloc[-1])
        c_prev  = float(df["Close"].iloc[-2])
        day_chg = round((c - c_prev) / c_prev * 100, 2) if c_prev else 0.0
        res_gap = round((c - resistance) / resistance * 100, 2)

        # [2] VL(변동회귀선) 대비 종가 5%↑
        close_s = df["Close"].astype(float)
        A   = linreg(close_s, LR_PERIOD)
        A1  = linreg(A,       LR_PERIOD)
        vl  = A + (A - A1)
        v   = float(vl.iloc[-1])
        if v <= 0:
            return None
        vl_gap = (c - v) / v * 100
        if vl_gap < 5.0:
            return None

        return {
            "종목코드":         code,
            "종가":            int(c),
            "전일대비(%)":     day_chg,
            "저항선":          int(round(resistance, 0)),
            "저항선대비(%)":   res_gap,
            "음봉거래량배수":  round(vol_ratio, 2),
            "VL":              round(v, 2),
            "VL대비(%)":       round(vl_gap, 2),
        }
    except Exception:
        return None


def run_screen22(date_str, prog):
    prog.update({"current": 0, "total": 0, "status": "loading"})
    start   = (datetime.strptime(date_str, "%Y%m%d") - timedelta(days=LOOKBACK)).strftime("%Y%m%d")
    listing = _get_listing_with_progress(prog)
    valid   = listing[listing["Marcap"] >= MIN_CAP].copy()
    prog["total"] = len(valid); prog["status"] = "running"
    rows = _run_screen_parallel(valid, _screen22_ticker, start, date_str, prog)
    prog["status"] = "done"
    if not rows:
        return pd.DataFrame()
    return (pd.DataFrame(rows)
            [["시장", "종목코드", "종목명", "시총(억)", "종가", "전일대비(%)",
              "저항선", "저항선대비(%)", "음봉거래량배수", "VL", "VL대비(%)"]]
            .sort_values("저항선대비(%)", ascending=False)
            .reset_index(drop=True))


# ══════════════════════════════════════════════════════════════════════════════
# 조건23: 세력20평단돌파
# 세력20평단 = EMA(캔들, 20), 캔들 = V>1.5×MA(V,60) and C>O 일 때 (C+O)/2, 유지
# 조건: 최근 5거래일 연속으로 |종가 - 세력20평단| / 세력20평단 ≤ 10%
# ══════════════════════════════════════════════════════════════════════════════

def _screen23_ticker(code, start, end):
    try:
        df = fetch_ohlcv(code, start, end)
        min_len = 60 + 20 + 5 + 10   # vol_ma60 + EMA20 + 5일 판정 + 여유
        if df is None or len(df) < min_len:
            return None

        # [0] 5일 평균 거래량 30만주 이상 (금일 제외)
        if df["Volume"].iloc[-6:-1].astype(float).mean() < 300_000:
            return None

        close_s = df["Close"].astype(float)

        # 세력20평단 계산
        sp = _calc_세력평단(df)
        if sp.iloc[-5:].isna().any():
            return None

        # 최근 5거래일 연속 |종가 - 평단| / 평단 ≤ 10%
        for i in range(-5, 0):
            c_i  = float(close_s.iloc[i])
            sp_i = float(sp.iloc[i])
            if sp_i <= 0:
                return None
            if abs(c_i - sp_i) / sp_i * 100 > 10.0:
                return None

        # ADX(11) > 25 — 추세 강도 필터
        adx_s = _calc_adx(df, period=11)
        adx_now = float(adx_s.iloc[-1])
        if np.isnan(adx_now) or adx_now <= 25.0:
            return None

        c       = float(close_s.iloc[-1])
        c_prev  = float(close_s.iloc[-2])
        sp_now  = float(sp.iloc[-1])
        day_chg = round((c - c_prev) / c_prev * 100, 2) if c_prev else 0.0
        gap_pct = round((c - sp_now) / sp_now * 100, 2)

        return {
            "종목코드":     code,
            "종가":         int(c),
            "전일대비(%)":  day_chg,
            "세력20평단":   int(round(sp_now, 0)),
            "평단대비(%)":  gap_pct,
            "ADX(11)":      round(adx_now, 1),
        }
    except Exception:
        return None


def run_screen23(date_str, prog):
    prog.update({"current": 0, "total": 0, "status": "loading"})
    start   = (datetime.strptime(date_str, "%Y%m%d") - timedelta(days=LOOKBACK)).strftime("%Y%m%d")
    listing = _get_listing_with_progress(prog)
    valid   = listing[
        (listing["Marcap"] >= MIN_CAP13) &
        (~listing["Market"].isin(["ETF", "ETN"]))
    ].copy()
    prog["total"] = len(valid); prog["status"] = "running"
    rows = _run_screen_parallel(valid, _screen23_ticker, start, date_str, prog)
    prog["status"] = "done"
    if not rows:
        return pd.DataFrame()
    return (pd.DataFrame(rows)
            [["시장", "종목코드", "종목명", "시총(억)", "종가", "전일대비(%)",
              "세력20평단", "평단대비(%)", "ADX(11)"]]
            .sort_values("평단대비(%)", key=lambda x: x.abs())   # 평단 근접 순 정렬
            .reset_index(drop=True))


# ══════════════════════════════════════════════════════════════════════════════
# 조건24: RSI다이버전스
# RSI(30) 매수 다이버전스: 가격 Lower Low + RSI Higher Low
# 최근 영역: 최근 margin+1(6)봉  /  과거 영역: lookback(60)~margin+1(6)봉 이전
# 시총 3000억↑
# ══════════════════════════════════════════════════════════════════════════════

_DIV_RSI_PERIOD = 30
_DIV_LOOKBACK   = 60
_DIV_MARGIN     = 5


def _detect_bullish_divergence(close_s: pd.Series, low_s: pd.Series) -> tuple[bool, float, float, float, float]:
    """
    매수 다이버전스 판별
    반환: (감지여부, 현재저점가격, 과거저점가격, 현재저점RSI, 과거저점RSI)
    """
    rsi = _calc_rsi(close_s, _DIV_RSI_PERIOD)

    recent_c   = low_s.iloc[-(_DIV_MARGIN + 1):]
    recent_rsi = rsi.iloc[-(_DIV_MARGIN + 1):]
    past_c     = low_s.iloc[-_DIV_LOOKBACK:-(_DIV_MARGIN + 1)]
    past_rsi   = rsi.iloc[-_DIV_LOOKBACK:-(_DIV_MARGIN + 1)]

    if len(past_c) < _DIV_MARGIN:
        return False, 0.0, 0.0, 0.0, 0.0

    cur_price_low  = float(recent_c.min())
    past_price_low = float(past_c.min())
    cur_rsi_low    = float(recent_rsi.min())
    past_rsi_low   = float(past_rsi.min())

    ok = (cur_price_low < past_price_low) and (cur_rsi_low > past_rsi_low)
    return ok, cur_price_low, past_price_low, cur_rsi_low, past_rsi_low


def _screen24_ticker(code, start, end):
    try:
        df = fetch_ohlcv(code, start, end)
        min_len = _DIV_LOOKBACK + _DIV_RSI_PERIOD + 10
        if df is None or len(df) < min_len:
            return None

        close_s = df["Close"].astype(float)
        low_s   = df["Low"].astype(float)

        ok, cur_low, past_low, cur_rsi, past_rsi = _detect_bullish_divergence(close_s, low_s)
        if not ok:
            return None

        c       = float(close_s.iloc[-1])
        c_prev  = float(close_s.iloc[-2])
        day_chg = round((c - c_prev) / c_prev * 100, 2) if c_prev else 0.0
        rsi_now = round(float(_calc_rsi(close_s, _DIV_RSI_PERIOD).iloc[-1]), 2)

        return {
            "종목코드":       code,
            "종가":          int(c),
            "전일대비(%)":   day_chg,
            "RSI(30)":       rsi_now,
            "현재저점":      int(round(cur_low, 0)),
            "과거저점":      int(round(past_low, 0)),
            "현재RSI저점":   round(cur_rsi, 2),
            "과거RSI저점":   round(past_rsi, 2),
        }
    except Exception:
        return None


def run_screen24(date_str, prog):
    prog.update({"current": 0, "total": 0, "status": "loading"})
    start   = (datetime.strptime(date_str, "%Y%m%d") - timedelta(days=LOOKBACK)).strftime("%Y%m%d")
    listing = _get_listing_with_progress(prog)
    valid   = listing[
        (listing["Marcap"] >= MIN_CAP13) &
        (~listing["Market"].isin(["ETF", "ETN"]))
    ].copy()
    prog["total"] = len(valid); prog["status"] = "running"
    rows = _run_screen_parallel(valid, _screen24_ticker, start, date_str, prog)
    prog["status"] = "done"
    if not rows:
        return pd.DataFrame()
    return (pd.DataFrame(rows)
            [["시장", "종목코드", "종목명", "시총(억)", "종가", "전일대비(%)",
              "RSI(30)", "현재저점", "과거저점", "현재RSI저점", "과거RSI저점"]]
            .sort_values("RSI(30)")
            .reset_index(drop=True))


# ══════════════════════════════════════════════════════════════════════════════
# 조건25: 거래대금순위
# 금일 순위(KIS API) vs 전일 순위(CSV 업로드)
# 조건①: 양봉(C>O) ②: 전일 1-10위 제외 ③: 순위 30위↑ ④: 전일동시간대 거래량 200%↑
# 상위 50위 반환
# ══════════════════════════════════════════════════════════════════════════════

_rt25_prev_rank_map  = {}   # {code(6자리): rank(int)}  — CSV 업로드 데이터
_rt25_prev_file_name = ""
_rt25_prev_row_count = 0


def _parse_prev_ranking_csv(content_bytes: bytes) -> dict:
    """전일 거래대금 순위 CSV 파싱 → {종목코드(6자리): 순위}"""
    text = None
    for enc in ("utf-8-sig", "utf-8", "cp949", "euc-kr"):
        try:
            text = content_bytes.decode(enc)
            break
        except Exception:
            continue
    if text is None:
        return {}
    try:
        df_csv = pd.read_csv(io.StringIO(text))
    except Exception:
        return {}

    rank_col = code_col = None
    for col in df_csv.columns:
        c = col.strip().lower()
        if rank_col is None and any(k in c for k in ["순위", "rank"]):
            rank_col = col
        if code_col is None and any(k in c for k in ["종목코드", "code", "코드", "ticker"]):
            code_col = col
    # fallback: 첫 열=순위, 둘째 열=종목코드
    cols = list(df_csv.columns)
    if rank_col is None and len(cols) >= 1:
        rank_col = cols[0]
    if code_col is None and len(cols) >= 2:
        code_col = cols[1]
    if rank_col is None or code_col is None:
        return {}

    result = {}
    for _, row in df_csv.iterrows():
        try:
            code = str(row[code_col]).strip().split(".")[0].zfill(6)
            rank = int(str(row[rank_col]).replace(",", "").strip())
            if code and rank > 0:
                result[code] = rank
        except Exception:
            continue
    return result


def run_screen25(date_str, prog):
    prog.update({"current": 0, "total": 3, "status": "loading"})

    if not _kis:
        prog["status"] = "done"
        return pd.DataFrame()
    if not _rt25_prev_rank_map:
        prog["status"] = "done"
        return pd.DataFrame()

    prog["status"] = "running"

    # [1] 금일 거래대금 순위 (KIS API)
    prog["current"] = 1
    today_list = _kis.trade_value_ranking(date_str, top_n=100)
    if not today_list:
        prog["status"] = "done"
        return pd.DataFrame()

    # [2] 전일 동시간대 시간비율
    prog["current"] = 2
    now       = datetime.now()
    mkt_open  = now.replace(hour=9,  minute=0,  second=0, microsecond=0)
    mkt_close = now.replace(hour=15, minute=30, second=0, microsecond=0)
    total_sec = (mkt_close - mkt_open).total_seconds()
    elapsed   = (now - mkt_open).total_seconds()
    scale     = max(min(elapsed / total_sec, 1.0), 0.01) if total_sec > 0 else 1.0

    # [3] 필터링
    prog["current"] = 3
    rows = []
    for r in today_list:
        code       = r["code"]
        today_rank = r["rank"]
        prev_rank  = _rt25_prev_rank_map.get(code)

        if prev_rank is None:
            continue
        if prev_rank <= 10:                  # 전일 1-10위 제외
            continue
        rank_up = prev_rank - today_rank
        if rank_up < 30:                     # 30위↑ 상승
            continue

        price = r["price"]
        open_ = r["open"]
        if price <= open_ or open_ <= 0:     # 양봉
            continue

        v_today   = r["volume"]
        v_prev    = r["prev_vol"]
        if v_prev <= 0:
            continue
        vol_ratio = v_today / (v_prev * scale) * 100
        if vol_ratio < 200.0:                # 전일동시간대 거래량 200%↑
            continue

        rows.append({
            "종목코드":       code,
            "종목명":        r["name"],
            "종가":          price,
            "시가":          open_,
            "금일순위":      today_rank,
            "전일순위":      prev_rank,
            "순위상승":      rank_up,
            "전일대비(%)":   r["day_chg"],
            "거래량비율(%)": round(vol_ratio, 1),
            "거래량":        v_today,
            "거래대금(억)":  r["tr_value"] // 100_000_000,
        })

    prog["status"] = "done"
    if not rows:
        return pd.DataFrame()
    return (pd.DataFrame(rows)
            [["종목코드", "종목명", "종가", "시가",
              "금일순위", "전일순위", "순위상승", "전일대비(%)",
              "거래량비율(%)", "거래량", "거래대금(억)"]]
            .sort_values("금일순위")
            .head(50)
            .reset_index(drop=True))


def _run_realtime25():
    """조건25 실시간 반복 스캔 루프"""
    global _rt25_scan_no, _rt25_scan_start, _rt25_last_scan, _rt25_next_scan, _rt25_scan_elapsed
    st = _state[25]
    print("[REALTIME25] 실시간 스캔 시작")
    while st["realtime"]:
        _rt25_scan_no += 1
        t_start   = datetime.now()
        today_str = t_start.strftime("%Y%m%d")
        _rt25_scan_start = t_start.strftime("%H:%M:%S")
        print(f"[REALTIME25] #{_rt25_scan_no} 스캔 시작 ({today_str} {_rt25_scan_start})")
        try:
            st["progress"] = {"current": 0, "total": 0, "status": "loading"}
            df = run_screen25(today_str, st["progress"])
            t_end = datetime.now()
            _rt25_last_scan    = t_end.strftime("%H:%M:%S")
            _rt25_scan_elapsed = int((t_end - t_start).total_seconds())

            if not df.empty:
                cur_codes = set(df["종목코드"].tolist())
                new       = cur_codes - st["known_codes"]
                st["new_codes"] = new
                st["known_codes"].update(cur_codes)
                st["result_df"]   = df
                st["result_date"] = today_str

                if new:
                    new_df = df[df["종목코드"].isin(new)]
                    print(f"[REALTIME25] 신규 {len(new)}개: {list(new)}")
                    alert_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                    lines = [f"알림 시간: {alert_time}", f"기준일: {today_str}",
                             f"스캔 횟수: {_rt25_scan_no}회차", ""]
                    for _, r in new_df.iterrows():
                        lines += [
                            f"▶ {r['종목명']} ({r['종목코드']})",
                            f"   종가: {int(r['종가']):,}원 | 전일대비: {r['전일대비(%)']}%",
                            f"   금일순위: {r['금일순위']}위 ← 전일: {r['전일순위']}위 (↑{r['순위상승']})",
                            f"   거래량비율: {r['거래량비율(%)']}% | 거래대금: {r['거래대금(억)']}억원", ""
                        ]
                    ok, err = _send_email_alert(
                        f"[거래대금순위] 신규 {len(new)}개 종목 ({datetime.now().strftime('%H:%M')})",
                        "\n".join(lines)
                    )
                    if not ok:
                        print(f"[REALTIME25] 이메일 발송 실패: {err}", flush=True)
            else:
                st["new_codes"] = set()

        except Exception as e:
            print(f"[REALTIME25] 오류: {e}")
            st["progress"]["status"] = "done"

        next_dt = datetime.now() + timedelta(seconds=SCAN_INTERVAL_SEC)
        _rt25_next_scan = next_dt.strftime("%H:%M:%S")
        for _ in range(SCAN_INTERVAL_SEC):
            if not st["realtime"]:
                break
            time.sleep(1)

    st["progress"]["status"] = "idle"
    _rt25_next_scan = ""
    print("[REALTIME25] 실시간 스캔 종료")


# ══════════════════════════════════════════════════════════════════════════════
# 조건26: 20일선전고점돌파
# SMA(Close, 20) 이 60봉 이내 전고점을 오늘 돌파하거나 같아지는 신규 시점
# 시가총액 3000억↑, ETF/ETN 제외
# ══════════════════════════════════════════════════════════════════════════════

_S26_PERIOD   = 20   # SMA 기간
_S26_LOOKBACK = 60   # 전고점 탐색 봉 수


def _screen26_ticker(code, start, end):
    df = fetch_ohlcv(code,
                     pd.Timestamp(start).strftime("%Y%m%d"),
                     pd.Timestamp(end).strftime("%Y%m%d"))
    if df is None or len(df) < _S26_PERIOD + _S26_LOOKBACK + 5:
        return None

    close = df["Close"]
    sma   = close.rolling(_S26_PERIOD).mean()

    valid = sma.dropna()
    if len(valid) < _S26_LOOKBACK + 2:
        return None

    cur_sma  = sma.iloc[-1]
    prev_sma = sma.iloc[-2]

    # 전일 기준 60봉의 SMA20 최대값 = 20일선 전고점
    prev_max = sma.iloc[-(_S26_LOOKBACK + 1):-1].max()

    if pd.isna(cur_sma) or pd.isna(prev_max):
        return None

    # 신규 돌파 시점: 오늘 SMA20 ≥ 전고점, 전일 SMA20 < 전고점
    if cur_sma < prev_max:     # 아직 미달
        return None
    if prev_sma >= prev_max:   # 이미 전일에 돌파 → 신규 아님
        return None

    cur_close  = close.iloc[-1]
    prev_close = close.iloc[-2]
    day_chg    = (cur_close / prev_close - 1) * 100
    gap_pct    = (cur_sma  / prev_max   - 1) * 100   # 0% = 정확히 전고점, 양수 = 돌파

    return (code,
            int(cur_close),
            round(day_chg, 2),
            int(prev_max),
            round(gap_pct, 2),
            int(cur_sma))


def run_screen26(date_str, prog):
    listing = _get_listing_with_progress(prog)
    valid   = listing[
        (listing["Marcap"] >= MIN_CAP) &
        (~listing["Market"].isin(["ETF", "ETN"]))
    ].copy()
    valid["Code"] = valid["Code"].astype(str).str.zfill(6)
    codes    = valid["Code"].tolist()
    name_map = dict(zip(valid["Code"], valid["Name"]))

    end   = pd.Timestamp(date_str)
    start = end - pd.Timedelta(days=400)

    prog.update({"current": 0, "total": len(codes), "status": "running"})

    rows = []
    lock = threading.Lock()
    cnt  = [0]

    def _job26(code):
        r = _screen26_ticker(code, start, end)
        with lock:
            cnt[0] += 1
            prog["current"] = cnt[0]
            if r is not None:
                rows.append({
                    "종목코드":      r[0],
                    "종목명":        name_map.get(r[0], ""),
                    "종가":          r[1],
                    "전일대비(%)":   r[2],
                    "20일선전고점":  r[3],
                    "전고점대비(%)": r[4],
                    "SMA20":         r[5],
                })

    with ThreadPoolExecutor(max_workers=_SCREEN_WORKERS) as ex:
        list(ex.map(_job26, codes))

    prog["status"] = "done"
    if not rows:
        return pd.DataFrame()
    return (pd.DataFrame(rows)
            [["종목코드", "종목명", "종가", "전일대비(%)",
              "20일선전고점", "전고점대비(%)", "SMA20"]]
            .sort_values("전고점대비(%)", ascending=False)
            .reset_index(drop=True))


# ══════════════════════════════════════════════════════════════════════════════
# 조건27: 최강종목
# ① BB(200,2) 상한선 ≤ 오늘 종가
# ② Envelope(20,40%) 상단선 ≤ 오늘 종가  OR  오늘 고점 > 상단선
# 시가총액 1500억↑, ETF/ETN 제외
# ══════════════════════════════════════════════════════════════════════════════

def _screen27_ticker(code, start, end):
    df = fetch_ohlcv(code,
                     pd.Timestamp(start).strftime("%Y%m%d"),
                     pd.Timestamp(end).strftime("%Y%m%d"))
    if df is None or len(df) < 210:
        return None

    close = df["Close"].astype(float)
    high  = df["High"].astype(float)

    # ── BB(200, 2) ──────────────────────────────────────────────────────────
    bb_sma = close.rolling(200).mean()
    bb_std = close.rolling(200).std(ddof=0)
    bb_upper = bb_sma + 3 * bb_std

    # ── Envelope(20, 40%) ───────────────────────────────────────────────────
    env_sma   = close.rolling(20).mean()
    env_upper = env_sma * 1.40

    cur_close    = close.iloc[-1]
    cur_high     = high.iloc[-1]
    cur_bb_upper = bb_upper.iloc[-1]
    cur_env_upper= env_upper.iloc[-1]
    prev_close   = close.iloc[-2]

    if any(pd.isna(v) for v in [cur_bb_upper, cur_env_upper]):
        return None

    # ① 종가 ≥ BB(200,2) 상한선
    if cur_close < cur_bb_upper:
        return None

    # ② 종가 ≥ Envelope 상단선  OR  오늘 고점 > Envelope 상단선
    if cur_close < cur_env_upper and cur_high <= cur_env_upper:
        return None

    day_chg     = (cur_close / prev_close - 1) * 100
    bb_gap      = (cur_close / cur_bb_upper  - 1) * 100
    env_gap     = (cur_close / cur_env_upper - 1) * 100

    return {
        "종목코드":       code,
        "종가":          int(cur_close),
        "전일대비(%)":   round(day_chg, 2),
        "BB상한선":      int(cur_bb_upper),
        "BB대비(%)":     round(bb_gap, 2),
        "ENV상단선":     int(cur_env_upper),
        "ENV대비(%)":    round(env_gap, 2),
    }


def run_screen27(date_str, prog):
    t_start = datetime.now()
    prog.update({"current": 0, "total": 0, "status": "loading"})
    listing = _get_listing_with_progress(prog)
    valid   = listing[
        (listing["Marcap"] >= MIN_CAP) &
        (~listing["Market"].isin(["ETF", "ETN"]))
    ].copy()
    prog["total"]  = len(valid)
    prog["status"] = "running"

    end   = pd.Timestamp(date_str)
    start = end - pd.Timedelta(days=500)

    rows = _run_screen_parallel(valid, _screen27_ticker, start, end, prog)
    t_end   = datetime.now()
    elapsed = int((t_end - t_start).total_seconds())
    prog["status"] = "done"
    if not rows:
        return pd.DataFrame()
    df = (pd.DataFrame(rows)
          [["시장", "종목코드", "종목명", "시총(억)", "종가", "전일대비(%)",
            "BB상한선", "BB대비(%)", "ENV상단선", "ENV대비(%)"]]
          .sort_values("BB대비(%)", ascending=False)
          .reset_index(drop=True))
    df["검색시각"] = t_end.strftime("%Y-%m-%d %H:%M:%S")
    df["소요(초)"] = elapsed
    return df


_rt27_scan_no      = 0
_rt27_scan_start   = ""
_rt27_last_scan    = ""
_rt27_next_scan    = ""
_rt27_scan_elapsed = 0


def _run_realtime27():
    """조건27 실시간 반복 스캔 루프"""
    global _rt27_scan_no, _rt27_scan_start, _rt27_last_scan, _rt27_next_scan, _rt27_scan_elapsed
    st = _state[27]
    print("[REALTIME27] 실시간 스캔 시작")
    while st["realtime"]:
        _rt27_scan_no += 1
        t_start   = datetime.now()
        today_str = t_start.strftime("%Y%m%d")
        _rt27_scan_start = t_start.strftime("%H:%M:%S")
        print(f"[REALTIME27] #{_rt27_scan_no} 스캔 시작 ({today_str} {_rt27_scan_start})")
        try:
            st["progress"] = {"current": 0, "total": 0, "status": "loading"}
            df = run_screen27(today_str, st["progress"])

            t_end = datetime.now()
            _rt27_last_scan    = t_end.strftime("%H:%M:%S")
            _rt27_scan_elapsed = int((t_end - t_start).total_seconds())

            if not df.empty:
                cur_codes = set(df["종목코드"].tolist())
                new       = cur_codes - st["known_codes"]
                st["new_codes"]    = new
                st["known_codes"].update(cur_codes)

                prev_df = st["result_df"]
                if not prev_df.empty and "검색시각" in prev_df.columns:
                    prev_ts = prev_df.set_index("종목코드")["검색시각"].to_dict()
                    df["검색시각"] = df["종목코드"].map(
                        lambda c: prev_ts.get(c, df.loc[df["종목코드"] == c, "검색시각"].iloc[0])
                    )

                st["result_df"]   = df
                st["result_date"] = today_str

                if new:
                    new_df = df[df["종목코드"].isin(new)]
                    print(f"[REALTIME27] 신규 {len(new)}개: {list(new)}")
                    alert_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                    lines = [f"[조건27 최강종목] 신규 {len(new)}개 종목 발견 — {alert_time}", ""]
                    for _, r2 in new_df.iterrows():
                        lines.append(
                            f"  {r2['종목명']}({r2['종목코드']})  "
                            f"종가 {r2['종가']:,}  BB대비 {r2['BB대비(%)']}%  ENV대비 {r2['ENV대비(%)']}%"
                        )
                    lines += ["", f"스캔 횟수: {_rt27_scan_no}회차"]
                    _send_email_alert(
                        f"[최강종목] 신규 {len(new)}개 발견 — {alert_time}",
                        "\n".join(lines)
                    )
            else:
                st["result_df"]   = df
                st["result_date"] = today_str
                st["new_codes"]   = set()

        except Exception as e:
            print(f"[REALTIME27] 오류: {e}")

        next_dt = datetime.now() + timedelta(seconds=SCAN_INTERVAL_SEC)
        _rt27_next_scan = next_dt.strftime("%H:%M:%S")
        for _ in range(SCAN_INTERVAL_SEC):
            if not st["realtime"]:
                break
            time.sleep(1)

    st["progress"]["status"] = "idle"
    _rt27_next_scan = ""
    print("[REALTIME27] 실시간 스캔 종료")


# ══════════════════════════════════════════════════════════════════════════════
# 조건28: RSI밴드돌파
# 조건16과 동일하나 3일 밴드압축 조건 제외
# RSI(21) 밴드: SMA(34) ± 1.6185σ
# S_RSI = SMA(RSI,2)  →  전일 S_RSI ≤ 전일 UPPER, 당일 S_RSI > 당일 UPPER
# 시가총액 1500억↑
# ══════════════════════════════════════════════════════════════════════════════

def _screen28_ticker(code, start, end):
    try:
        df = fetch_ohlcv(code, start, end)
        min_len = _HR_RSI_PERIOD + _HR_BAND_LEN + _HR_TSL + 10
        if df is None or len(df) < min_len:
            return None

        close_s = df["Close"].astype(float)

        rsi_s   = _calc_rsi(close_s, _HR_RSI_PERIOD)
        s_rsi   = rsi_s.rolling(window=_HR_TSL).mean()
        rsi_ma  = rsi_s.rolling(window=_HR_BAND_LEN).mean()
        rsi_std = rsi_s.rolling(window=_HR_BAND_LEN).std(ddof=1)
        upper   = rsi_ma + _HR_COEF * rsi_std
        lower   = rsi_ma - _HR_COEF * rsi_std
        bw      = upper - lower

        u0, u1 = upper.iloc[-1], upper.iloc[-2]
        s0, s1 = s_rsi.iloc[-1], s_rsi.iloc[-2]

        if any(pd.isna(x) for x in [u0, u1, s0, s1]):
            return None

        # 밴드압축 조건 없음 — S_RSI 상향돌파만 확인
        if not (s1 <= u1 and s0 > u0):
            return None

        # ADX(11) > 25 — 추세 강도 필터
        adx_s   = _calc_adx(df, period=11)
        adx_now = float(adx_s.iloc[-1])
        if np.isnan(adx_now) or adx_now <= 25.0:
            return None

        c       = float(close_s.iloc[-1])
        c_prev  = float(close_s.iloc[-2])
        day_chg = round((c - c_prev) / c_prev * 100, 2)

        return {
            "종목코드":     code,
            "종가":        int(c),
            "전일대비(%)": day_chg,
            "RSI(21)":    round(float(rsi_s.iloc[-1]), 2),
            "S_RSI":      round(float(s0), 2),
            "UPPER":      round(float(u0), 2),
            "LOWER":      round(float(lower.iloc[-1]), 2),
            "밴드폭":      round(float(bw.iloc[-1]), 2),
            "ADX(11)":    round(adx_now, 1),
        }
    except Exception:
        return None


def run_screen28(date_str, prog):
    t_start = datetime.now()
    prog.update({"current": 0, "total": 0, "status": "loading"})
    start   = (datetime.strptime(date_str, "%Y%m%d") - timedelta(days=LOOKBACK)).strftime("%Y%m%d")
    listing = _get_listing_with_progress(prog)
    valid   = listing[listing["Marcap"] >= MIN_CAP].copy()
    prog["total"]  = len(valid)
    prog["status"] = "running"
    rows = _run_screen_parallel(valid, _screen28_ticker, start, date_str, prog)
    t_end   = datetime.now()
    elapsed = int((t_end - t_start).total_seconds())
    prog["status"] = "done"
    if not rows:
        return pd.DataFrame()
    df = (pd.DataFrame(rows)
          [["시장", "종목코드", "종목명", "시총(억)", "종가", "전일대비(%)",
            "RSI(21)", "S_RSI", "UPPER", "LOWER", "밴드폭", "ADX(11)"]]
          .sort_values("S_RSI", ascending=False)
          .reset_index(drop=True))
    df["검색시각"] = t_end.strftime("%Y-%m-%d %H:%M:%S")
    df["소요(초)"] = elapsed
    return df


# ══════════════════════════════════════════════════════════════════════════════
# 조건29: 양음양
# A : 2봉전 시가 대비 종가 등락률 ≥ 8%  (그저께 강한 양봉)
# B : 1봉전 종가 < 시가               (어제 음봉)
# C : 오늘 종가 > 시가                (오늘 양봉)
# D : 어제 거래량 ≤ 그저께 거래량×50% (음봉 시 거래량 감소)
# E : 그저께 시가 < 오늘 종가
# F : 전일 동시간대 대비 거래량 ≥ 100%
# G : 오늘 종가 > SMA(200)
# H : 오늘 종가 > SMA(20)
# 시가총액 1500억↑, ETF/ETN 제외
# ══════════════════════════════════════════════════════════════════════════════

def _screen29_ticker(code, start, end):
    try:
        df = fetch_ohlcv(code,
                         pd.Timestamp(start).strftime("%Y%m%d"),
                         pd.Timestamp(end).strftime("%Y%m%d"))
        if df is None or len(df) < 205:
            return None

        close  = df["Close"].astype(float)
        open_  = df["Open"].astype(float)
        volume = df["Volume"].astype(float)

        c0, c1, c2 = close.iloc[-1],  close.iloc[-2],  close.iloc[-3]
        o0, o1, o2 = open_.iloc[-1],  open_.iloc[-2],  open_.iloc[-3]
        v0, v1, v2 = volume.iloc[-1], volume.iloc[-2], volume.iloc[-3]

        # A: 2봉전(그저께) 시가 대비 종가 등락률 ≥ 8%
        if o2 == 0:
            return None
        if (c2 - o2) / o2 * 100 < 8.0:
            return None

        # B: 어제 음봉 (종가 < 시가)
        if c1 >= o1:
            return None

        # C: 오늘 양봉 (종가 > 시가)
        if c0 <= o0:
            return None

        # D: 어제 거래량 ≤ 그저께의 50%
        if v2 == 0 or v1 > v2 * 0.5:
            return None

        # E: 그저께 시가 < 오늘 종가
        if o2 >= c0:
            return None

        # F: 전일 동시간대 대비 거래량 ≥ 100%
        if v1 == 0:
            return None
        now       = datetime.now()
        mkt_open  = now.replace(hour=9,  minute=0,  second=0, microsecond=0)
        mkt_close = now.replace(hour=15, minute=30, second=0, microsecond=0)
        total_sec = (mkt_close - mkt_open).total_seconds()
        elapsed_s = (now - mkt_open).total_seconds()
        scale     = max(min(elapsed_s / total_sec, 1.0), 0.01)
        vol_ratio = v0 / (v1 * scale) * 100
        if vol_ratio < 100.0:
            return None

        # G: 오늘 종가 > SMA(200)
        sma200 = close.rolling(200).mean().iloc[-1]
        if pd.isna(sma200) or c0 <= sma200:
            return None

        # H: 오늘 종가 > SMA(20)
        sma20 = close.rolling(20).mean().iloc[-1]
        if pd.isna(sma20) or c0 <= sma20:
            return None

        day_chg  = round((c0 / c1 - 1) * 100, 2) if c1 > 0 else 0.0
        chg2     = round((c2 - o2) / o2 * 100, 2)

        return {
            "종목코드":      code,
            "종가":          int(c0),
            "전일대비(%)":   day_chg,
            "2봉전등락(%)":  chg2,
            "거래량비율(%)": round(vol_ratio, 1),
            "SMA20":         int(sma20),
            "SMA200":        int(sma200),
        }
    except Exception:
        return None


def run_screen29(date_str, prog):
    t_start = datetime.now()
    prog.update({"current": 0, "total": 0, "status": "loading"})
    end     = pd.Timestamp(date_str)
    start   = end - pd.Timedelta(days=400)
    listing = _get_listing_with_progress(prog)
    valid   = listing[
        (listing["Marcap"] >= MIN_CAP) &
        (~listing["Market"].isin(["ETF", "ETN"]))
    ].copy()
    prog["total"]  = len(valid)
    prog["status"] = "running"
    rows    = _run_screen_parallel(valid, _screen29_ticker, start, end, prog)
    t_end   = datetime.now()
    elapsed = int((t_end - t_start).total_seconds())
    prog["status"] = "done"
    if not rows:
        return pd.DataFrame()
    df = (pd.DataFrame(rows)
          [["시장", "종목코드", "종목명", "시총(억)", "종가", "전일대비(%)",
            "2봉전등락(%)", "거래량비율(%)", "SMA20", "SMA200"]]
          .sort_values("거래량비율(%)", ascending=False)
          .reset_index(drop=True))
    df["검색시각"] = t_end.strftime("%Y-%m-%d %H:%M:%S")
    df["소요(초)"] = elapsed
    return df


# ══════════════════════════════════════════════════════════════════════════════
# 조건30: VL급반등
# ── 지난 30거래일 VL 최대낙폭 ≥ 40% AND 최근 3거래일 연속 VL 일간 +4%↑
# ══════════════════════════════════════════════════════════════════════════════

def _screen30_ticker(code, start, end):
    try:
        df = fetch_ohlcv(code,
                         pd.Timestamp(start).strftime("%Y%m%d"),
                         pd.Timestamp(end).strftime("%Y%m%d"))
        min_len = LR_PERIOD * 2 + 34 + 5
        if df is None or len(df) < min_len:
            return None

        close_s = df["Close"].astype(float)

        # VL = 2·linreg(Close,50) − linreg(linreg(Close,50), 50)
        A  = linreg(close_s, LR_PERIOD)
        vl = A + (A - linreg(A, LR_PERIOD))

        vl_arr = vl.dropna().values
        if len(vl_arr) < 34:
            return None

        vl_30   = vl_arr[-30:]   # 최근 30봉 (낙폭 검사)
        vl_rise = vl_arr[-4:]    # 최근 4봉: D-3, D-2, D-1, D0 (반등 검사)

        # ── 반등 조건: 최근 3거래일 연속 VL 일간 ≥ 4% 상승 ──
        for i in range(1, 4):
            prev, curr = vl_rise[i - 1], vl_rise[i]
            if prev <= 0 or (curr - prev) / prev * 100 < 4.0:
                return None

        # ── 낙폭 조건: 30거래일 내 피크→트로프 최대낙폭 ≥ 40% ──
        peak     = vl_30[0]
        max_drop = 0.0
        for v in vl_30:
            if v > peak:
                peak = v
            elif peak > 0:
                drop = (peak - v) / peak * 100
                if drop > max_drop:
                    max_drop = drop

        if max_drop < 40.0:
            return None

        # ── 부가 정보 ──
        c0 = float(close_s.iloc[-1])
        c1 = float(close_s.iloc[-2])
        day_chg    = round((c0 / c1 - 1) * 100, 2) if c1 > 0 else 0.0
        vl0, vl1, vl3 = float(vl_rise[-1]), float(vl_rise[-2]), float(vl_rise[0])
        vl_day_chg = round((vl0 / vl1 - 1) * 100, 2) if vl1 > 0 else 0.0
        vl_3d_chg  = round((vl0 / vl3 - 1) * 100, 2) if vl3 > 0 else 0.0

        return {
            "종목코드":       code,
            "종가":           int(c0),
            "전일대비(%)":    day_chg,
            "VL":             round(vl0, 2),
            "VL일간상승(%)":  vl_day_chg,
            "3일누적상승(%)": vl_3d_chg,
            "VL낙폭(%)":      round(max_drop, 2),
        }
    except Exception:
        return None


def run_screen30(date_str, prog):
    t_start = datetime.now()
    prog.update({"current": 0, "total": 0, "status": "loading"})
    end     = pd.Timestamp(date_str)
    start   = end - pd.Timedelta(days=LOOKBACK)
    listing = _get_listing_with_progress(prog)
    valid   = listing[
        (listing["Marcap"] >= MIN_CAP) &
        (~listing["Market"].isin(["ETF", "ETN"]))
    ].copy()
    prog["total"]  = len(valid)
    prog["status"] = "running"
    rows    = _run_screen_parallel(valid, _screen30_ticker, start, end, prog)
    t_end   = datetime.now()
    elapsed = int((t_end - t_start).total_seconds())
    prog["status"] = "done"
    if not rows:
        return pd.DataFrame()
    df = (pd.DataFrame(rows)
          [["시장", "종목코드", "종목명", "시총(억)", "종가", "전일대비(%)",
            "VL", "VL일간상승(%)", "3일누적상승(%)", "VL낙폭(%)"]]
          .sort_values("VL낙폭(%)", ascending=False)
          .reset_index(drop=True))
    df["검색시각"] = t_end.strftime("%Y-%m-%d %H:%M:%S")
    df["소요(초)"] = elapsed
    return df


# ══════════════════════════════════════════════════════════════════════════════
# 조건31: pro RSI
# ── expoLen = 2*LenRSI-1 EMA 기반 RSI 밴드 역산 · 9가지 FindMode
# ══════════════════════════════════════════════════════════════════════════════

_S31_LEN_RSI   = 14
_S31_UPPER_PCT = 70.0
_S31_LOWER_PCT = 30.0


def _calc_rsi_bands(close: pd.Series,
                    len_rsi:   int   = 14,
                    upper_pct: float = 70.0,
                    lower_pct: float = 30.0):
    """RSI 밴드 역산 (벡터화).
    expoLen = 2*LenRSI - 1 의 EMA로 상승분/하락분 평활 → 밴드 역산.
    반환: (band_low, band_high, band_mid, band_mid_high, band_mid_low) — 모두 pd.Series
    """
    expo_len = 2 * len_rsi - 1
    diff     = close.diff()
    gain_raw = diff.clip(lower=0)
    loss_raw = (-diff).clip(lower=0)
    g_ema    = gain_raw.ewm(span=expo_len, adjust=False).mean()
    l_ema    = loss_raw.ewm(span=expo_len, adjust=False).mean()

    c = close.values.astype(float)
    g = g_ema.values
    l = l_ema.values

    # 하단밴드 (bandLow)
    x1 = (len_rsi - 1) * (l * lower_pct / (100.0 - lower_pct) - g)
    bl = np.where(x1 >= 0, c + x1, c + x1 * (100.0 - lower_pct) / lower_pct)

    # 상단밴드 (bandHigh)
    x2 = (len_rsi - 1) * (l * upper_pct / (100.0 - upper_pct) - g)
    bh = np.where(x2 >= 0, c + x2, c + x2 * (100.0 - upper_pct) / upper_pct)

    bm  = (bh + bl) / 2       # 중심선
    bmh = (bh + bm) / 2       # 상단 1/2선
    bml = (bm + bl) / 2       # 하단 1/2선

    idx = close.index
    return (pd.Series(bl,  index=idx),
            pd.Series(bh,  index=idx),
            pd.Series(bm,  index=idx),
            pd.Series(bmh, index=idx),
            pd.Series(bml, index=idx))


def _screen31_ticker(code, start, end):
    try:
        find_mode = _state[31].get("find_mode", 9)
        df = fetch_ohlcv(code,
                         pd.Timestamp(start).strftime("%Y%m%d"),
                         pd.Timestamp(end).strftime("%Y%m%d"))
        # expo_len=27 → EMA 충분한 안정화에 200봉 이상 필요
        if df is None or len(df) < 200:
            return None

        close = df["Close"].astype(float)
        bl, bh, bm, bmh, bml = _calc_rsi_bands(
            close, _S31_LEN_RSI, _S31_UPPER_PCT, _S31_LOWER_PCT)

        c0   = float(close.iloc[-1]);  c1   = float(close.iloc[-2])
        bl0  = float(bl.iloc[-1]);     bl1  = float(bl.iloc[-2])
        bh0  = float(bh.iloc[-1]);     bh1  = float(bh.iloc[-2])
        bm0  = float(bm.iloc[-1]);     bm1  = float(bm.iloc[-2])
        bmh0 = float(bmh.iloc[-1])
        bml0 = float(bml.iloc[-1])

        cross_up_low  = (c1 < bl1) and (c0 >= bl0)
        cross_dn_high = (c1 > bh1) and (c0 <= bh0)
        cross_up_mid  = (c1 < bm1) and (c0 >= bm0)
        cross_dn_mid  = (c1 > bm1) and (c0 <= bm0)
        cross_up_high = (c1 < bh1) and (c0 >= bh0)   # 모드10: 상단밴드 상향 돌파

        ok = False
        if   find_mode == 1:  ok = c0 < bl0
        elif find_mode == 2:  ok = c0 > bh0
        elif find_mode == 3:  ok = cross_up_low
        elif find_mode == 4:  ok = cross_dn_high
        elif find_mode == 5:  ok = cross_up_mid
        elif find_mode == 6:  ok = cross_dn_mid
        elif find_mode == 7:  ok = c0 < bml0
        elif find_mode == 8:  ok = c0 > bmh0
        elif find_mode == 9:  ok = cross_up_low or cross_up_mid
        elif find_mode == 10: ok = cross_up_high

        if not ok:
            return None

        day_chg = round((c0 / c1 - 1) * 100, 2) if c1 > 0 else 0.0

        sigs = []
        if cross_up_high: sigs.append("상단돌파↑")
        if cross_up_low:  sigs.append("하단돌파↑")
        if cross_up_mid:  sigs.append("중심선돌파↑")
        if cross_dn_high: sigs.append("상단이탈↓")
        if cross_dn_mid:  sigs.append("중심선이탈↓")
        if not sigs:
            if   c0 < bl0:  sigs.append("과매도")
            elif c0 > bh0:  sigs.append("과매수")
            elif c0 < bml0: sigs.append("약세구간")
            elif c0 > bmh0: sigs.append("강세구간")

        return {
            "종목코드":    code,
            "종가":        int(c0),
            "전일대비(%)": day_chg,
            "RSI밴드하단": round(bl0),
            "RSI중심선":   round(bm0),
            "RSI밴드상단": round(bh0),
            "신호":        ", ".join(sigs) if sigs else "-",
        }
    except Exception:
        return None


def run_screen31(date_str, prog):
    t_start = datetime.now()
    prog.update({"current": 0, "total": 0, "status": "loading"})
    end     = pd.Timestamp(date_str)
    start   = end - pd.Timedelta(days=LOOKBACK)
    listing = _get_listing_with_progress(prog)
    valid   = listing[
        (listing["Marcap"] >= MIN_CAP) &
        (~listing["Market"].isin(["ETF", "ETN"]))
    ].copy()
    prog["total"]  = len(valid)
    prog["status"] = "running"
    rows    = _run_screen_parallel(valid, _screen31_ticker, start, end, prog)
    t_end   = datetime.now()
    elapsed = int((t_end - t_start).total_seconds())
    prog["status"] = "done"
    if not rows:
        return pd.DataFrame()
    df = (pd.DataFrame(rows)
          [["시장", "종목코드", "종목명", "시총(억)", "종가", "전일대비(%)",
            "RSI밴드하단", "RSI중심선", "RSI밴드상단", "신호"]]
          .sort_values("전일대비(%)", ascending=False)
          .reset_index(drop=True))
    df["검색시각"] = t_end.strftime("%Y-%m-%d %H:%M:%S")
    df["소요(초)"] = elapsed
    return df


# ══════════════════════════════════════════════════════════════════════════════
# 조건43: proRSI2 — 조건31과 동일, RSI 기간만 30으로 변경 (expoLen=59)
# ══════════════════════════════════════════════════════════════════════════════

_S43_LEN_RSI   = 30
_S43_UPPER_PCT = 70.0
_S43_LOWER_PCT = 30.0


def _screen43_ticker(code, start, end):
    try:
        find_mode = _state[43].get("find_mode", 9)
        df = fetch_ohlcv(code,
                         pd.Timestamp(start).strftime("%Y%m%d"),
                         pd.Timestamp(end).strftime("%Y%m%d"))
        # expo_len=59 → EMA 충분한 안정화에 200봉 이상 필요
        if df is None or len(df) < 200:
            return None

        close = df["Close"].astype(float)
        bl, bh, bm, bmh, bml = _calc_rsi_bands(
            close, _S43_LEN_RSI, _S43_UPPER_PCT, _S43_LOWER_PCT)

        c0   = float(close.iloc[-1]);  c1   = float(close.iloc[-2])
        bl0  = float(bl.iloc[-1]);     bl1  = float(bl.iloc[-2])
        bh0  = float(bh.iloc[-1]);     bh1  = float(bh.iloc[-2])
        bm0  = float(bm.iloc[-1]);     bm1  = float(bm.iloc[-2])
        bmh0 = float(bmh.iloc[-1])
        bml0 = float(bml.iloc[-1])

        cross_up_low  = (c1 < bl1) and (c0 >= bl0)
        cross_dn_high = (c1 > bh1) and (c0 <= bh0)
        cross_up_mid  = (c1 < bm1) and (c0 >= bm0)
        cross_dn_mid  = (c1 > bm1) and (c0 <= bm0)
        cross_up_high = (c1 < bh1) and (c0 >= bh0)

        ok = False
        if   find_mode == 1:  ok = c0 < bl0
        elif find_mode == 2:  ok = c0 > bh0
        elif find_mode == 3:  ok = cross_up_low
        elif find_mode == 4:  ok = cross_dn_high
        elif find_mode == 5:  ok = cross_up_mid
        elif find_mode == 6:  ok = cross_dn_mid
        elif find_mode == 7:  ok = c0 < bml0
        elif find_mode == 8:  ok = c0 > bmh0
        elif find_mode == 9:  ok = cross_up_low or cross_up_mid
        elif find_mode == 10: ok = cross_up_high

        if not ok:
            return None

        day_chg = round((c0 / c1 - 1) * 100, 2) if c1 > 0 else 0.0

        sigs = []
        if cross_up_high: sigs.append("상단돌파↑")
        if cross_up_low:  sigs.append("하단돌파↑")
        if cross_up_mid:  sigs.append("중심선돌파↑")
        if cross_dn_high: sigs.append("상단이탈↓")
        if cross_dn_mid:  sigs.append("중심선이탈↓")
        if not sigs:
            if   c0 < bl0:  sigs.append("과매도")
            elif c0 > bh0:  sigs.append("과매수")
            elif c0 < bml0: sigs.append("약세구간")
            elif c0 > bmh0: sigs.append("강세구간")

        return {
            "종목코드":    code,
            "종가":        int(c0),
            "전일대비(%)": day_chg,
            "RSI밴드하단": round(bl0),
            "RSI중심선":   round(bm0),
            "RSI밴드상단": round(bh0),
            "신호":        ", ".join(sigs) if sigs else "-",
        }
    except Exception:
        return None


def run_screen43(date_str, prog):
    t_start = datetime.now()
    prog.update({"current": 0, "total": 0, "status": "loading"})
    end     = pd.Timestamp(date_str)
    start   = end - pd.Timedelta(days=LOOKBACK)
    listing = _get_listing_with_progress(prog)
    valid   = listing[
        (listing["Marcap"] >= MIN_CAP) &
        (~listing["Market"].isin(["ETF", "ETN"]))
    ].copy()
    prog["total"]  = len(valid)
    prog["status"] = "running"
    rows    = _run_screen_parallel(valid, _screen43_ticker, start, end, prog)
    t_end   = datetime.now()
    elapsed = int((t_end - t_start).total_seconds())
    prog["status"] = "done"
    if not rows:
        return pd.DataFrame()
    df = (pd.DataFrame(rows)
          [["시장", "종목코드", "종목명", "시총(억)", "종가", "전일대비(%)",
            "RSI밴드하단", "RSI중심선", "RSI밴드상단", "신호"]]
          .sort_values("전일대비(%)", ascending=False)
          .reset_index(drop=True))
    df["검색시각"] = t_end.strftime("%Y-%m-%d %H:%M:%S")
    df["소요(초)"] = elapsed
    return df


# ══════════════════════════════════════════════════════════════════════════════
# 조건44: Market shift levels — HMA(55) 지지/저항 레벨 기반 돌파 검색
# HMA(n) = WMA( 2*WMA(C, n/2) - WMA(C, n), sqrt(n) )
# CrossUp  → myLevel = 현재봉 저가 (지지선)
# CrossDown → myLevel = 현재봉 고가 (저항선)
# ══════════════════════════════════════════════════════════════════════════════

_S44_LEN     = 55
_S44_MIN_CAP = 500_000_000_000   # 시총 5,000억 이상


def _calc_hma44(close: pd.Series, length: int = 55) -> pd.Series:
    """
    HMA(n) = WMA( 2*WMA(C, n/2) - WMA(C, n), sqrt(n) )
    _wma()는 기존 공용 함수 재사용
    """
    h2     = length // 2
    sq     = int(np.sqrt(length))
    wma_h  = _wma(close, h2)
    wma_f  = _wma(close, length)
    hma_in = 2 * wma_h - wma_f
    return _wma(hma_in, sq)


def _calc_qqe(close: pd.Series, length: int = 14, ssf: int = 5,
              multiplier: float = 4.236):
    """
    QQE (Quantitative Qualitative Estimation)
    ① QQEF = EMA(RSI(close, length), ssf)          ← 패스트 라인
    ② TR   = |QQEF - QQEF[1]|
       ATRRSI = EMA(EMA(TR, length), length)        ← 이중 평활 ATR
    ③ QUP = QQEF + ATRRSI × multiplier             ← 상단 밴드
       QDN = QQEF − ATRRSI × multiplier             ← 하단 밴드
    ④ QQES: 트레일링 스탑 raw → EMA(ssf) 최종 평활  ← 슬로우 라인
    반환: (qqef, qqes)
    """
    rsi    = _calc_rsi(close, length)
    qqef   = rsi.ewm(span=ssf, adjust=False).mean()

    tr     = qqef.diff().abs()
    atr1   = tr.ewm(span=length, adjust=False).mean()
    atrrsi = atr1.ewm(span=length, adjust=False).mean()

    qup = qqef + multiplier * atrrsi
    qdn = qqef - multiplier * atrrsi

    # 트레일링 스탑 raw 계산
    ef  = qqef.values
    up  = qup.values
    dn  = qdn.values
    raw = np.full(len(close), np.nan)
    for i in range(len(close)):
        if np.isnan(ef[i]) or np.isnan(up[i]):
            continue
        if i == 0 or np.isnan(raw[i - 1]):
            raw[i] = (up[i] + dn[i]) / 2.0
            continue
        prev    = raw[i - 1]
        raw[i]  = max(dn[i], prev) if ef[i] > prev else min(up[i], prev)

    qqes = pd.Series(raw, index=close.index).ewm(span=ssf, adjust=False).mean()
    return qqef, qqes


def _screen44_ticker(code, start, end):
    try:
        find_mode = _state[44].get("find_mode", 6)
        df = fetch_ohlcv(code,
                         pd.Timestamp(start).strftime("%Y%m%d"),
                         pd.Timestamp(end).strftime("%Y%m%d"))
        if df is None or len(df) < 200:
            return None

        close = df["Close"].astype(float)
        high  = df["High"].astype(float)
        low   = df["Low"].astype(float)
        open_ = df["Open"].astype(float)

        hma1 = _calc_hma44(close, _S44_LEN)
        hma2 = hma1.shift(5)   # hma1[5] — 5봉 전 값

        h1 = hma1.values
        h2 = hma2.values
        lo = low.values
        hi = high.values
        cl = close.values
        op = open_.values
        n  = len(h1)

        # ── myLevel 계산: CrossUp → Low, CrossDown → High (persistent) ─────
        level     = np.zeros(n)
        cur_lv    = 0.0
        for i in range(1, n):
            h1i = h1[i];  h2i = h2[i]
            h1p = h1[i-1]; h2p = h2[i-1]
            if not (np.isnan(h1i) or np.isnan(h2i) or np.isnan(h1p) or np.isnan(h2p)):
                if h1p <= h2p and h1i > h2i:   # CrossUp → 지지선 = 당일 저가
                    cur_lv = lo[i]
                elif h1p >= h2p and h1i < h2i: # CrossDown → 저항선 = 당일 고가
                    cur_lv = hi[i]
            level[i] = cur_lv

        i = n - 1
        if i < 3 or level[i] == 0:
            return None

        lv  = level[i]   # 현재 myLevel
        c0  = cl[i];  c1  = cl[i-1]
        o0  = op[i]
        lo0 = lo[i];  lo1 = lo[i-1];  lo2 = lo[i-2]
        hi0 = hi[i];  hi1 = hi[i-1];  hi2 = hi[i-2]

        # ── 크로스 판별 ──────────────────────────────────────────────────────
        def _cross_up_hma():
            return (not np.isnan(h1[i]) and not np.isnan(h2[i]) and
                    not np.isnan(h1[i-1]) and not np.isnan(h2[i-1]) and
                    h1[i-1] <= h2[i-1] and h1[i] > h2[i])
        def _cross_dn_hma():
            return (not np.isnan(h1[i]) and not np.isnan(h2[i]) and
                    not np.isnan(h1[i-1]) and not np.isnan(h2[i-1]) and
                    h1[i-1] >= h2[i-1] and h1[i] < h2[i])

        # ── 6가지 조건 ──────────────────────────────────────────────────────
        cond_bull_rev    = lo2 > lv and lo1 < lv and lo0 > lv
        cond_bear_rev    = hi2 < lv and hi0 < lv and hi1 > lv
        cond_hma_crossup = _cross_up_hma()
        cond_hma_crossdn = _cross_dn_hma()
        cond_above_lv    = c0 > lv
        cond_cross_lv    = (c1 <= lv) and (c0 > lv) and (c0 > o0)

        ok = False
        if   find_mode == 1: ok = cond_bull_rev
        elif find_mode == 2: ok = cond_bear_rev
        elif find_mode == 3: ok = cond_hma_crossup
        elif find_mode == 4: ok = cond_hma_crossdn
        elif find_mode == 5: ok = cond_above_lv
        elif find_mode == 6: ok = cond_cross_lv

        if not ok:
            return None

        day_chg = round((c0 / c1 - 1) * 100, 2) if c1 > 0 else 0.0

        sigs = []
        if cond_bull_rev:    sigs.append("상승반전")
        if cond_bear_rev:    sigs.append("하락반전")
        if cond_hma_crossup: sigs.append("HMA상향돌파")
        if cond_hma_crossdn: sigs.append("HMA하향이탈")
        if cond_cross_lv:    sigs.append("레벨돌파+양봉")
        elif cond_above_lv:  sigs.append("레벨위유지")

        hma_dir = "↑" if (not np.isnan(h1[i]) and not np.isnan(h1[i-1]) and h1[i] > h1[i-1]) else "↓"

        return {
            "종목코드":    code,
            "종가":        int(c0),
            "전일대비(%)": day_chg,
            "HMA":         round(float(h1[i])) if not np.isnan(h1[i]) else 0,
            "HMA레벨":     round(lv),
            "HMA방향":     hma_dir,
            "신호":        ", ".join(sigs) if sigs else "-",
        }
    except Exception:
        return None


def run_screen44(date_str, prog):
    t_start = datetime.now()
    prog.update({"current": 0, "total": 0, "status": "loading"})
    end     = pd.Timestamp(date_str)
    start   = end - pd.Timedelta(days=LOOKBACK)
    listing = _get_listing_with_progress(prog)
    valid   = listing[
        (listing["Marcap"] >= _S44_MIN_CAP) &
        (~listing["Market"].isin(["ETF", "ETN"]))
    ].copy()
    prog["total"]  = len(valid)
    prog["status"] = "running"
    rows    = _run_screen_parallel(valid, _screen44_ticker, start, end, prog)
    t_end   = datetime.now()
    elapsed = int((t_end - t_start).total_seconds())
    prog["status"] = "done"
    if not rows:
        return pd.DataFrame()
    df = (pd.DataFrame(rows)
          [["시장", "종목코드", "종목명", "시총(억)", "종가", "전일대비(%)",
            "HMA", "HMA레벨", "HMA방향", "신호"]]
          .sort_values("전일대비(%)", ascending=False)
          .reset_index(drop=True))
    df["검색시각"] = t_end.strftime("%Y-%m-%d %H:%M:%S")
    df["소요(초)"] = elapsed
    return df


_rt44_scan_no      = 0
_rt44_scan_start   = ""
_rt44_last_scan    = ""
_rt44_next_scan    = ""
_rt44_scan_elapsed = 0

_RT44_INTERVAL_SEC = 600   # 조건44 실시간 재스캔 주기: 10분


def _run_realtime44():
    """조건44 Market shift levels 실시간 반복 스캔 루프 (10분 간격)"""
    global _rt44_scan_no, _rt44_scan_start, _rt44_last_scan, _rt44_next_scan, _rt44_scan_elapsed
    st = _state[44]
    print("[REALTIME44] 실시간 스캔 시작")

    _MODE_NAMES = {1:"상승반전", 2:"하락반전", 3:"HMA상향돌파",
                   4:"HMA하향이탈", 5:"레벨위유지", 6:"레벨돌파+양봉"}

    while st["realtime"]:
        _rt44_scan_no   += 1
        t_start          = datetime.now()
        today_str        = t_start.strftime("%Y%m%d")
        _rt44_scan_start = t_start.strftime("%H:%M:%S")
        find_mode        = st.get("find_mode", 6)
        print(f"[REALTIME44] #{_rt44_scan_no} 스캔 시작 ({today_str} {_rt44_scan_start}) Mode{find_mode}")

        try:
            st["progress"] = {"current": 0, "total": 0, "status": "loading"}
            result_df = run_screen44(today_str, st["progress"])
            t_end              = datetime.now()
            _rt44_last_scan    = t_end.strftime("%H:%M:%S")
            _rt44_scan_elapsed = int((t_end - t_start).total_seconds())
            st["progress"].update({"status": "done"})

            if not result_df.empty:
                result_df["검색시각"] = _rt44_last_scan
                result_df["소요(초)"] = _rt44_scan_elapsed
                st["result_df"] = result_df

                prev_codes = st.get("known_codes", set())
                cur_codes  = set(result_df["종목코드"].astype(str).tolist())
                new_codes  = cur_codes - prev_codes
                st["new_codes"]   = new_codes
                st["known_codes"] = cur_codes

                if new_codes and prev_codes:
                    mode_name  = _MODE_NAMES.get(find_mode, f"Mode{find_mode}")
                    alert_time = t_end.strftime("%Y-%m-%d %H:%M:%S")
                    lines = [
                        f"[Market shift levels] #{_rt44_scan_no}회차 신규 {len(new_codes)}종목 — {alert_time}",
                        f"FindMode: {find_mode} ({mode_name})", ""
                    ]
                    lines.append("■ 신규 신호 종목")
                    for _, row in result_df[result_df["종목코드"].isin(new_codes)].iterrows():
                        lines.append(
                            f"  {row['종목명']} ({row['종목코드']}) "
                            f"| 시장: {row['시장']} "
                            f"| {row['전일대비(%)']}% "
                            f"| HMA레벨: {row['HMA레벨']:,} "
                            f"| 신호: {row['신호']}"
                        )
                    lines += ["", f"스캔 #{_rt44_scan_no}회차 완료"]
                    _send_email_alert(
                        f"[Market shift levels] #{_rt44_scan_no}회차 신규 {len(new_codes)}종목 — {_rt44_last_scan}",
                        "\n".join(lines)
                    )
                    print(f"[REALTIME44] 신규 {len(new_codes)}개 → 이메일 발송")
                else:
                    print(f"[REALTIME44] #{_rt44_scan_no} {len(result_df)}종목 완료, 신규 없음")
            else:
                st["result_df"]  = pd.DataFrame()
                st["new_codes"]  = set()
                st["progress"]["status"] = "done"
                print(f"[REALTIME44] #{_rt44_scan_no} 결과 없음")

        except Exception as e:
            print(f"[REALTIME44] 오류: {e}")
            st["progress"]["status"] = "done"

        next_dt          = datetime.now() + timedelta(seconds=_RT44_INTERVAL_SEC)
        _rt44_next_scan  = next_dt.strftime("%H:%M:%S")
        for _ in range(_RT44_INTERVAL_SEC):
            if not st["realtime"]:
                break
            time.sleep(1)

    st["progress"]["status"] = "idle"
    _rt44_next_scan = ""
    print("[REALTIME44] 실시간 스캔 종료")


# ══════════════════════════════════════════════════════════════════════════════
# 조건45: QQE (Quantitative Qualitative Estimation)
# QQEF = EMA(RSI(14), 5)   ← 패스트 라인
# QQES = 트레일링 스탑 EMA(5) ← 슬로우 라인
# 신호: FAST·SLOW 모두 ≤50, 전일 FAST<SLOW → 금일 FAST>SLOW (골든크로스) AND VL↑
# 시총 1,500억↑, ETF/ETN 제외 · 데이터: KIS API 우선(400봉), FDR 폴백
# ══════════════════════════════════════════════════════════════════════════════

_S45_LEN     = 14
_S45_SSF     = 5
_S45_MULT    = 4.236
_S45_MIN_CAP = 150_000_000_000   # 시총 1,500억 이상


def _screen45_ticker(code, start, end):
    try:
        # ── KIS API 우선 (수정주가 기준 실데이터) ────────────────────────────
        df = None
        end_dt = pd.Timestamp(end).to_pydatetime()
        if _kis:
            try:
                df = _kis.daily_ohlcv(code, n_bars=400, end_date=end_dt)
                if df is not None and not df.empty:
                    # 검색 기준일 이후 데이터 제거
                    df = df[df.index <= pd.Timestamp(end_dt.date())]
            except Exception:
                df = None

        # ── KIS 실패 시 FDR 폴백 ────────────────────────────────────────────
        if df is None or df.empty:
            df = fetch_ohlcv(code,
                             pd.Timestamp(start).strftime("%Y%m%d"),
                             pd.Timestamp(end).strftime("%Y%m%d"))

        if df is None or len(df) < 60:
            return None

        close = df["Close"].astype(float)

        qqef, qqes = _calc_qqe(close, _S45_LEN, _S45_SSF, _S45_MULT)
        vl         = _calc_vl_s38(close)

        ef = qqef.values
        es = qqes.values
        vv = vl.values
        n  = len(ef)
        i  = n - 1

        if i < 2:
            return None
        if any(np.isnan(v) for v in [ef[i], es[i], ef[i-1], es[i-1]]):
            return None
        if np.isnan(vv[i]) or np.isnan(vv[i-1]):
            return None

        # ① CrossUp(QQEF, QQES): 전일 QQEF < QQES, 금일 QQEF > QQES
        cross_up  = (ef[i-1] < es[i-1]) and (ef[i] > es[i])
        # ② FAST·SLOW 모두 50 이하
        both_below_50 = (ef[i] <= 50.0) and (es[i] <= 50.0)
        # ③ 변동회귀선(VL) 금일 > 전일
        vl_rising = vv[i] > vv[i-1]

        if not (cross_up and both_below_50 and vl_rising):
            return None

        cl      = close.values
        c0, c1  = cl[i], cl[i-1]
        day_chg = round((c0 / c1 - 1) * 100, 2) if c1 > 0 else 0.0

        return {
            "종목코드":    code,
            "종가":        int(c0),
            "전일대비(%)": day_chg,
            "QQEF":        round(float(ef[i]), 2),
            "QQES":        round(float(es[i]), 2),
            "VL":          round(float(vv[i]), 2),
            "신호":        "QQE돌파(FAST·SLOW≤50)+VL↑",
        }
    except Exception:
        return None


def run_screen45(date_str, prog):
    t_start = datetime.now()
    prog.update({"current": 0, "total": 0, "status": "loading"})
    end     = pd.Timestamp(date_str)
    start   = end - pd.Timedelta(days=LOOKBACK)
    listing = _get_listing_with_progress(prog)
    valid   = listing[
        (listing["Marcap"] >= _S45_MIN_CAP) &
        (~listing["Market"].isin(["ETF", "ETN"]))
    ].copy()
    prog["total"]  = len(valid)
    prog["status"] = "running"
    rows    = _run_screen_parallel(valid, _screen45_ticker, start, end, prog)
    t_end   = datetime.now()
    elapsed = int((t_end - t_start).total_seconds())
    prog["status"] = "done"
    if not rows:
        return pd.DataFrame()
    df = (pd.DataFrame(rows)
          [["시장", "종목코드", "종목명", "시총(억)", "종가", "전일대비(%)",
            "QQEF", "QQES", "VL", "신호"]]
          .sort_values("QQEF", ascending=True)
          .reset_index(drop=True))
    df["검색시각"] = t_end.strftime("%Y-%m-%d %H:%M:%S")
    df["소요(초)"] = elapsed
    return df


# ══════════════════════════════════════════════════════════════════════════════
# 조건46: MSL2 — HMA(55) 레벨 돌파 + 양봉 (Mode6 고정)
# W1  = WMA(C, 27)   hI = 2*W1 - WMA(C, 55)   hma = WMA(hI, 7)
# CrossUp  → Lv = 당일 저가 (지지선)
# CrossDown → Lv = 당일 고가 (저항선)
# 신호: crossup(Close, Lv) AND Close > Open   시총 3,000억↑, ETF/ETN 제외
# ══════════════════════════════════════════════════════════════════════════════

_S46_MIN_CAP = 300_000_000_000   # 시총 3,000억 이상


def _screen46_ticker(code, start, end):
    try:
        df = fetch_ohlcv(code,
                         pd.Timestamp(start).strftime("%Y%m%d"),
                         pd.Timestamp(end).strftime("%Y%m%d"))
        if df is None or len(df) < 200:
            return None

        close = df["Close"].astype(float)
        high  = df["High"].astype(float)
        low   = df["Low"].astype(float)
        open_ = df["Open"].astype(float)

        # HMA(55) — _calc_hma44 재사용 (Len=55, hL=27, sL=7)
        hma1 = _calc_hma44(close, 55)
        hma2 = hma1.shift(5)   # hma[5]

        # VL = A + (A - A1)  where A=LinReg(C,50), A1=LinReg(A,50)
        vl = _calc_vl_s38(close)

        h1 = hma1.values
        h2 = hma2.values
        lo = low.values
        hi = high.values
        cl = close.values
        op = open_.values
        vv = vl.values
        n  = len(h1)

        # ── Lv 계산: CrossUp→Low(지지), CrossDown→High(저항), persistent ──────
        level  = np.zeros(n)
        cur_lv = 0.0
        for i in range(1, n):
            h1i = h1[i];  h2i = h2[i]
            h1p = h1[i-1]; h2p = h2[i-1]
            if not (np.isnan(h1i) or np.isnan(h2i) or np.isnan(h1p) or np.isnan(h2p)):
                if h1p <= h2p and h1i > h2i:    # CrossUp → 지지선
                    cur_lv = lo[i]
                elif h1p >= h2p and h1i < h2i:  # CrossDown → 저항선
                    cur_lv = hi[i]
            level[i] = cur_lv

        i = n - 1
        if i < 3 or level[i] == 0:
            return None

        # VL 유효성 확인
        if np.isnan(vv[i]) or np.isnan(vv[i-1]):
            return None

        lv = level[i]
        c0 = cl[i]; c1 = cl[i-1]; o0 = op[i]

        # 신호①: crossup(C, Lv) AND C > O (양봉)
        cond_lv = (c1 <= lv) and (c0 > lv) and (c0 > o0)
        # 신호②: VL(1) < VL  (변동회귀선 상승)
        cond_vl = vv[i-1] < vv[i]

        if not (cond_lv and cond_vl):
            return None

        day_chg = round((c0 / c1 - 1) * 100, 2) if c1 > 0 else 0.0
        hma_dir = "↑" if (not np.isnan(h1[i]) and not np.isnan(h1[i-1]) and h1[i] > h1[i-1]) else "↓"

        return {
            "종목코드":    code,
            "종가":        int(c0),
            "전일대비(%)": day_chg,
            "HMA":         round(float(h1[i])) if not np.isnan(h1[i]) else 0,
            "MSL2레벨":    round(lv),
            "HMA방향":     hma_dir,
            "VL":          round(float(vv[i]), 2),
            "신호":        "레벨돌파+양봉+VL↑",
        }
    except Exception:
        return None


def run_screen46(date_str, prog):
    t_start = datetime.now()
    prog.update({"current": 0, "total": 0, "status": "loading"})
    end     = pd.Timestamp(date_str)
    start   = end - pd.Timedelta(days=LOOKBACK)
    listing = _get_listing_with_progress(prog)
    valid   = listing[
        (listing["Marcap"] >= _S46_MIN_CAP) &
        (~listing["Market"].isin(["ETF", "ETN"]))
    ].copy()
    prog["total"]  = len(valid)
    prog["status"] = "running"
    rows    = _run_screen_parallel(valid, _screen46_ticker, start, end, prog)
    t_end   = datetime.now()
    elapsed = int((t_end - t_start).total_seconds())
    prog["status"] = "done"
    if not rows:
        return pd.DataFrame()
    df = (pd.DataFrame(rows)
          [["시장", "종목코드", "종목명", "시총(억)", "종가", "전일대비(%)",
            "HMA", "MSL2레벨", "HMA방향", "VL", "신호"]]
          .sort_values("전일대비(%)", ascending=False)
          .reset_index(drop=True))
    df["검색시각"] = t_end.strftime("%Y-%m-%d %H:%M:%S")
    df["소요(초)"] = elapsed
    return df


# ══════════════════════════════════════════════════════════════════════════════
# 조건47: 과열스코어(월) — 시총 3,000억↑, 월봉 ATR(5)/ATR(20)×MA(V,5)/MA(V,20)×이격도(20)/100
#         이번 달(진행 중 포함) 처음으로 3 초과 (전월 ≤ 3, 이번달 > 3)
# ══════════════════════════════════════════════════════════════════════════════

_S47_MIN_CAP = 300_000_000_000   # 시총 3,000억 이상


def _screen47_ticker(code, start, end):
    try:
        df = _fdr_safe(code, start, end)
        if df is None or len(df) < 60:
            return None
        df = df.copy()
        df = df[df["Close"] > 0].dropna(subset=["Open", "High", "Low", "Close", "Volume"])

        # 일봉 → 월봉
        dfm = _resample_monthly(df)
        if len(dfm) < 23:   # ATR(20) 계산 + 비교 여유
            return None

        hi = dfm["High"].astype(float)
        lo = dfm["Low"].astype(float)
        cl = dfm["Close"].astype(float)
        vo = dfm["Volume"].astype(float)

        # True Range (월봉)
        prev_cl = cl.shift(1)
        tr = pd.concat([hi - lo,
                         (hi - prev_cl).abs(),
                         (lo - prev_cl).abs()], axis=1).max(axis=1)

        # ATR(5m), ATR(20m) — SMA of TR
        atr5  = tr.rolling(5).mean()
        atr20 = tr.rolling(20).mean()

        # MA(V,5m), MA(V,20m)
        mav5  = vo.rolling(5).mean()
        mav20 = vo.rolling(20).mean()

        # 이격도(20m) = Close / SMA(20m) × 100
        sma20  = cl.rolling(20).mean()
        disp20 = cl / sma20 * 100.0

        # 과열스코어
        score = (atr5 / atr20) * (mav5 / mav20) * (disp20 / 100.0)

        cur_score  = float(score.iloc[-1])
        prev_score = float(score.iloc[-2])
        if np.isnan(cur_score) or np.isnan(prev_score):
            return None

        # 이번 달 처음 충족: 이번 > 3, 지난달 ≤ 3
        if cur_score <= 3.0:
            return None
        if prev_score > 3.0:   # 이미 지난달에 충족 → 제외
            return None

        # 전일대비(%) — 일봉 최근 2일
        today_close = float(df["Close"].iloc[-1])
        prev_daily  = float(df["Close"].iloc[-2]) if len(df) >= 2 else today_close
        pct_chg     = round((today_close / prev_daily - 1) * 100, 2) if prev_daily > 0 else 0.0

        return {
            "종목코드":      code,
            "종가":          int(today_close),
            "전일대비(%)":   pct_chg,
            "과열스코어(월)": round(cur_score, 2),
            "전월스코어":    round(prev_score, 2),
            "ATR비율(월)":   round(float(atr5.iloc[-1] / atr20.iloc[-1]), 2),
            "거래량비율(월)": round(float(mav5.iloc[-1] / mav20.iloc[-1]), 2),
            "이격도(20m)":   round(float(disp20.iloc[-1]), 1),
        }
    except Exception:
        return None


def run_screen47(date_str, prog):
    t_start = datetime.now()
    prog.update({"current": 0, "total": 0, "status": "loading"})
    end     = pd.Timestamp(date_str)
    start   = end - pd.Timedelta(days=LOOKBACK * 3)   # 월봉 충분히 확보 (약 6년)
    listing = _get_listing_with_progress(prog)
    valid   = listing[
        (listing["Marcap"] >= _S47_MIN_CAP) &
        (~listing["Market"].isin(["ETF", "ETN"]))
    ].copy()
    prog["total"]  = len(valid)
    prog["status"] = "running"
    rows    = _run_screen_parallel(valid, _screen47_ticker, start, end, prog)
    t_end   = datetime.now()
    elapsed = int((t_end - t_start).total_seconds())
    prog["status"] = "done"
    if not rows:
        return pd.DataFrame()
    df = (pd.DataFrame(rows)
          [["시장", "종목코드", "종목명", "시총(억)", "종가", "전일대비(%)",
            "과열스코어(월)", "전월스코어", "ATR비율(월)", "거래량비율(월)", "이격도(20m)"]]
          .sort_values("과열스코어(월)", ascending=False)
          .reset_index(drop=True))
    df["검색시각"] = t_end.strftime("%Y-%m-%d %H:%M:%S")
    df["소요(초)"] = elapsed
    return df


# 조건48: 칼만트렌드라인 — 시총 3,000억↑, Short Kalman(50) / Long Kalman(150)
_S48_MIN_CAP = 300_000_000_000
_S48_SHORT_LEN = 50
_S48_LONG_LEN = 150


def _kalman_filter_series(close: pd.Series, length: int,
                          r: float = 0.01, q: float = 0.1) -> pd.Series:
    error_est = 1.0
    error_meas = r * length
    estimate = np.nan
    vals = []
    close_vals = close.astype(float).to_numpy()

    for i, price in enumerate(close_vals):
        if np.isnan(price):
            vals.append(np.nan)
            continue
        if np.isnan(estimate):
            estimate = close_vals[i - 1] if i > 0 and not np.isnan(close_vals[i - 1]) else price
        prediction = estimate
        kalman_gain = error_est / (error_est + error_meas)
        estimate = prediction + kalman_gain * (price - prediction)
        error_est = (1 - kalman_gain) * error_est + q / length
        vals.append(estimate)

    return pd.Series(vals, index=close.index)


def _screen48_ticker(code, start, end):
    try:
        find_mode = int(_state[48].get("find_mode", 0))
        df = fetch_ohlcv(code, start, end)
        if df is None or len(df) < _S48_LONG_LEN + 5:
            return None
        df = df.copy().dropna(subset=["Open", "High", "Low", "Close"])
        if len(df) < _S48_LONG_LEN + 5:
            return None

        op = df["Open"].astype(float)
        cl = df["Close"].astype(float)
        short_k = _kalman_filter_series(cl, _S48_SHORT_LEN)
        long_k = _kalman_filter_series(cl, _S48_LONG_LEN)
        if pd.isna(short_k.iloc[-1]) or pd.isna(long_k.iloc[-1]):
            return None

        cross_up = short_k.iloc[-2] <= long_k.iloc[-2] and short_k.iloc[-1] > long_k.iloc[-1]
        cross_down = short_k.iloc[-2] >= long_k.iloc[-2] and short_k.iloc[-1] < long_k.iloc[-1]
        trend_up = bool(short_k.iloc[-1] > long_k.iloc[-1])
        candle_up = bool(cl.iloc[-1] > op.iloc[-1])

        if find_mode == 0:
            ok = cross_up and candle_up
            signal = "골든크로스"
        elif find_mode == 1:
            ok = cross_down
            signal = "데드크로스"
        elif find_mode == 2:
            ok = cross_up or cross_down
            signal = "골든크로스" if cross_up else "데드크로스"
        elif find_mode == 3:
            ok = trend_up
            signal = "상승추세"
        elif find_mode == 4:
            ok = not trend_up
            signal = "하락추세"
        else:
            ok = cross_up and candle_up
            signal = "골든크로스"

        if not ok:
            return None

        prev_close = float(cl.iloc[-2]) if len(cl) >= 2 else float(cl.iloc[-1])
        day_chg = (float(cl.iloc[-1]) / prev_close - 1) * 100 if prev_close > 0 else 0.0
        gap = (float(short_k.iloc[-1]) / float(long_k.iloc[-1]) - 1) * 100 if long_k.iloc[-1] else 0.0
        slope = float(short_k.iloc[-1] - short_k.iloc[-3]) if len(short_k) >= 3 else 0.0

        return {
            "종목코드": code,
            "종가": int(cl.iloc[-1]),
            "전일대비(%)": round(day_chg, 2),
            "신호": signal,
            "추세": "상승" if trend_up else "하락",
            "양봉": "Y" if candle_up else "N",
            "ShortKalman": round(float(short_k.iloc[-1]), 2),
            "LongKalman": round(float(long_k.iloc[-1]), 2),
            "이격(%)": round(gap, 2),
            "Short기울기": round(slope, 2),
        }
    except Exception:
        return None


def run_screen48(date_str, prog):
    t_start = datetime.now()
    prog.update({"current": 0, "total": 0, "status": "loading"})
    end = date_str
    start = (datetime.strptime(date_str, "%Y%m%d") - timedelta(days=LOOKBACK)).strftime("%Y%m%d")
    listing = _get_listing_with_progress(prog)
    valid = listing[
        (listing["Marcap"] >= _S48_MIN_CAP) &
        (~listing["Market"].isin(["ETF", "ETN"])) &
        (~listing["Name"].str.match(_ETF_NAME_RE, na=False))
    ].copy()
    prog["total"] = len(valid)
    prog["status"] = "running"
    rows = _run_screen_parallel(valid, _screen48_ticker, start, end, prog)
    t_end = datetime.now()
    elapsed = int((t_end - t_start).total_seconds())
    prog["status"] = "done"
    if not rows:
        return pd.DataFrame()
    df = (pd.DataFrame(rows)
          [["시장", "종목코드", "종목명", "시총(억)", "종가", "전일대비(%)",
            "신호", "추세", "양봉", "ShortKalman", "LongKalman", "이격(%)", "Short기울기"]]
          .sort_values(["신호", "이격(%)"], ascending=[True, False])
          .reset_index(drop=True))
    df["검색시각"] = t_end.strftime("%Y-%m-%d %H:%M:%S")
    df["소요(초)"] = elapsed
    return df


# ══════════════════════════════════════════════════════════════════════════════
# 조건49: 박문환원인점
# [원인점 정의] 시가·종가 모두 5일선 아래인 '첫 번째' 봉의 저가
#   - 역방향 탐색: 전일봉은 5일선 위(두 조건 중 하나 이상 충족) → 금일봉은 시가+종가 모두 하방
# [재돌파 신호] 전일 종가 ≤ 원인점 저가 → 금일 종가 > 원인점 저가
# 시총 3,000억↑, ETF/ETN 제외
# ══════════════════════════════════════════════════════════════════════════════

_S49_MIN_CAP = 300_000_000_000   # 시총 3,000억 이상
_S49_MA_PERIOD = 5               # 5일 이동평균


def _screen49_ticker(code, start, end):
    try:
        df = fetch_ohlcv(code,
                         pd.Timestamp(start).strftime("%Y%m%d"),
                         pd.Timestamp(end).strftime("%Y%m%d"))
        if df is None or len(df) < _S49_MA_PERIOD + 5:
            return None

        close  = df["Close"].astype(float)
        open_  = df["Open"].astype(float)
        low    = df["Low"].astype(float)
        volume = df["Volume"].astype(float)

        ma5 = close.rolling(_S49_MA_PERIOD).mean()

        cl  = close.values
        op  = open_.values
        lo  = low.values
        ma  = ma5.values
        vol = volume.values
        n   = len(cl)

        if n < 8:   # MA5(5) + 탐색 여유 + 거래량5일평균(1일 추가)
            return None

        # ── 거래량 조건 사전 체크 (빠른 기각) ────────────────────────────
        vol_today = vol[n - 1]
        vol_prev  = vol[n - 2]
        # 금일 제외 최근 5거래일 평균 거래량 (n-6 ~ n-2)
        if n < 7:
            return None
        vol5_avg = float(np.mean(vol[max(0, n - 6):n - 1]))

        # ① 금일 제외 5일 평균 거래량 ≥ 30만주
        if vol5_avg < 300_000:
            return None
        # ② 금일 거래량 ≥ 전일 거래량 × 200%
        if vol_prev <= 0 or vol_today < vol_prev * 2.0:
            return None

        # ── 원인점 탐색: 오늘(n-1) 제외, 역방향 ────────────────────────────
        # 조건: i번째 봉은 시가·종가 모두 MA5 하방,
        #        i-1번째 봉은 MA5 하방이 아님 (첫 이탈)
        cause_low  = None
        cause_date = None
        cause_idx  = None

        for i in range(n - 2, 0, -1):   # 전일(n-2)부터 역방향
            if np.isnan(ma[i]) or np.isnan(ma[i - 1]):
                continue
            below_now  = (op[i] < ma[i])  and (cl[i] < ma[i])
            below_prev = (op[i-1] < ma[i-1]) and (cl[i-1] < ma[i-1])
            if below_now and not below_prev:
                cause_low  = lo[i]
                cause_date = df.index[i]
                cause_idx  = i
                break

        if cause_low is None:
            return None

        # ── 재돌파 신호 ────────────────────────────────────────────────────
        today_close = cl[n - 1]
        prev_close  = cl[n - 2]

        if not ((today_close > cause_low) and (prev_close <= cause_low)):
            return None

        # ── 보조 지표 ──────────────────────────────────────────────────────
        day_chg      = round((today_close / prev_close - 1) * 100, 2) if prev_close > 0 else 0.0
        ma5_today    = round(float(ma[n - 1]), 2) if not np.isnan(ma[n - 1]) else 0.0
        break_pct    = round((today_close - cause_low) / cause_low * 100, 2) if cause_low > 0 else 0.0
        days_since   = (n - 1) - cause_idx
        cause_dt_str = pd.Timestamp(cause_date).strftime("%Y-%m-%d")
        vol_ratio    = round(vol_today / vol_prev * 100, 1) if vol_prev > 0 else 0.0

        return {
            "종목코드":       code,
            "종가":           int(today_close),
            "전일대비(%)":    day_chg,
            "원인점":         int(cause_low),
            "원인점날짜":     cause_dt_str,
            "5일선":          ma5_today,
            "돌파율(%)":      break_pct,
            "경과일":         days_since,
            "거래량비율(%)":  vol_ratio,
            "5일평균거래량":  int(vol5_avg),
            "신호":           "원인점재돌파",
        }
    except Exception:
        return None


def run_screen49(date_str, prog):
    t_start = datetime.now()
    prog.update({"current": 0, "total": 0, "status": "loading"})
    end     = pd.Timestamp(date_str)
    start   = end - pd.Timedelta(days=LOOKBACK)
    listing = _get_listing()
    valid   = listing[
        (listing["Marcap"] >= _S49_MIN_CAP) &
        (~listing["Market"].isin(["ETF", "ETN"])) &
        (~listing["Name"].str.match(_ETF_NAME_RE, na=False))
    ].copy()
    prog["total"]  = len(valid)
    prog["status"] = "running"
    rows    = _run_screen_parallel(valid, _screen49_ticker, start, end, prog)
    t_end   = datetime.now()
    elapsed = int((t_end - t_start).total_seconds())
    prog["status"] = "done"
    if not rows:
        return pd.DataFrame()
    df = (pd.DataFrame(rows)
          [["시장", "종목코드", "종목명", "시총(억)", "종가", "전일대비(%)",
            "원인점", "원인점날짜", "5일선", "돌파율(%)",
            "경과일", "거래량비율(%)", "5일평균거래량", "신호"]]
          .sort_values("돌파율(%)", ascending=True)
          .reset_index(drop=True))
    df["검색시각"] = t_end.strftime("%Y-%m-%d %H:%M:%S")
    df["소요(초)"] = elapsed
    return df


# ══════════════════════════════════════════════════════════════════════════════
# 조건50: 김승태타점
# A: 최근 120봉 중 종가 신고가가 최근 20봉 이내에 발생
# B: 양봉 (시가 < 종가)
# C: 전일 종가 < 전일 SMA5
# D: 전일 시가 < 전일 SMA5
# E: 종가 SMA5 골든크로스 (전일≤SMA5, 금일>SMA5)
# G: 전일 저가 대비 종가 등락률 ≥ 5%
# H: 전일 기준 5봉 평균거래량 ≥ 30만주
# J: SMA200 2봉 연속 상승
# (F 거래대금 순위 상위100은 조건식에서 제외)
# ══════════════════════════════════════════════════════════════════════════════

_S50_MIN_BARS = 210   # 조건A(120봉)+조건J(SMA200) 계산에 필요한 최소 봉 수


def _screen50_ticker(code, start, end):
    try:
        df = fetch_ohlcv(code,
                         pd.Timestamp(start).strftime("%Y%m%d"),
                         pd.Timestamp(end).strftime("%Y%m%d"))
        if df is None or len(df) < _S50_MIN_BARS:
            return None

        close  = df["Close"].astype(float)
        open_  = df["Open"].astype(float)
        low    = df["Low"].astype(float)
        volume = df["Volume"].astype(float)

        ma5   = close.rolling(5).mean()
        ma200 = close.rolling(200).mean()

        cl   = close.values
        op   = open_.values
        lo   = low.values
        vol  = volume.values
        m5   = ma5.values
        m200 = ma200.values
        n    = len(cl)
        i    = n - 1

        if i < 5:
            return None

        # NaN 체크 (필수 값)
        need = [m5[i], m5[i-1], m200[i], m200[i-1], m200[i-2]]
        if any(np.isnan(v) for v in need):
            return None

        # ── A: 120봉 신고가가 최근 20봉 이내에 발생 ────────────────────
        window_120  = cl[n - 120:n]                # 오늘 포함 최근 120봉
        max_pos     = np.argmax(window_120)         # window 내 최고가 위치
        bars_ago    = 119 - int(max_pos)            # 오늘=0, 어제=1, ...
        cond_A      = bars_ago < 20

        # ── B: 양봉 ─────────────────────────────────────────────────────
        cond_B = op[i] < cl[i]

        # ── C: 전일 종가 < 전일 MA5 ──────────────────────────────────────
        cond_C = m5[i-1] > cl[i-1]

        # ── D: 전일 시가 < 전일 MA5 ──────────────────────────────────────
        cond_D = m5[i-1] > op[i-1]

        # ── E: 종가 MA5 골든크로스 (전일≤MA5 → 금일>MA5) ────────────────
        cond_E = (cl[i-1] <= m5[i-1]) and (cl[i] > m5[i])

        # ── G: 전일 저가 대비 종가 등락률 ≥ 5% ──────────────────────────
        cond_G = (lo[i-1] > 0) and ((cl[i-1] - lo[i-1]) / lo[i-1] >= 0.05)

        # ── H: 전일 기준 5봉 평균거래량 ≥ 30만주 ────────────────────────
        vol5_avg = float(np.mean(vol[n-6:n-1]))    # n-6~n-2 (5봉, 전일 포함)
        cond_H   = vol5_avg >= 300_000

        # ── J: MA200 2봉 연속 상승 ────────────────────────────────────
        cond_J = (m200[i] > m200[i-1]) and (m200[i-1] > m200[i-2])

        if not (cond_A and cond_B and cond_C and cond_D and
                cond_E and cond_G and cond_H and cond_J):
            return None

        day_chg        = round((cl[i] / cl[i-1] - 1) * 100, 2) if cl[i-1] > 0 else 0.0
        prev_low_pct   = round((cl[i-1] - lo[i-1]) / lo[i-1] * 100, 2) if lo[i-1] > 0 else 0.0

        return {
            "종목코드":         code,
            "종가":             int(cl[i]),
            "전일대비(%)":      day_chg,
            "SMA5":             round(float(m5[i]), 2),
            "SMA200":           round(float(m200[i]), 2),
            "120봉신고가(봉전)": bars_ago,
            "전일저가대비(%)":   prev_low_pct,
            "5일평균거래량":     int(vol5_avg),
            "신호":             "김승태타점",
        }
    except Exception:
        return None


def run_screen50(date_str, prog):
    t_start = datetime.now()
    prog.update({"current": 0, "total": 0, "status": "loading"})
    end     = pd.Timestamp(date_str)
    start   = end - pd.Timedelta(days=LOOKBACK)
    listing = _get_listing()
    valid   = listing[
        (listing["Marcap"] >= 300_000_000_000) &
        ~listing["Market"].isin(["ETF", "ETN"]) &
        ~listing["Name"].str.match(_ETF_NAME_RE, na=False)
    ].copy()
    prog["total"]  = len(valid)
    prog["status"] = "running"
    rows    = _run_screen_parallel(valid, _screen50_ticker, start, end, prog)
    t_end   = datetime.now()
    elapsed = int((t_end - t_start).total_seconds())
    prog["status"] = "done"
    if not rows:
        return pd.DataFrame()
    df = (pd.DataFrame(rows)
          [["시장", "종목코드", "종목명", "시총(억)", "종가", "전일대비(%)",
            "SMA5", "SMA200", "120봉신고가(봉전)", "전일저가대비(%)",
            "5일평균거래량", "신호"]]
          .sort_values("전일대비(%)", ascending=False)
          .reset_index(drop=True))
    df["검색시각"] = t_end.strftime("%Y-%m-%d %H:%M:%S")
    df["소요(초)"] = elapsed
    return df


# ══════════════════════════════════════════════════════════════════════════════
# 조건51: 김승태타점4
# A: 3봉전 종가 < SMA5
# B: 2봉전 종가 < SMA5
# C: 1봉전 종가 < SMA5
# D: 금일 시가 < SMA5
# E: 금일 종가 < SMA5 (5일선 아래에서 양봉)
# F: 양봉 (시가 < 종가)
# G: SMA20 2봉 연속 상승
# H: SMA60 2봉 연속 상승
# I: 시총 3,000억↑ (run_screen51 필터)
# J: 전일 기준 5봉 평균거래량 ≥ 10만주
# ══════════════════════════════════════════════════════════════════════════════

_S51_MIN_BARS = 65   # SMA60 계산 + 이전 2봉 여유분


def _screen51_ticker(code, start, end):
    try:
        df = fetch_ohlcv(code,
                         pd.Timestamp(start).strftime("%Y%m%d"),
                         pd.Timestamp(end).strftime("%Y%m%d"))
        if df is None or len(df) < _S51_MIN_BARS:
            return None

        close  = df["Close"].astype(float)
        open_  = df["Open"].astype(float)
        volume = df["Volume"].astype(float)

        ma5  = close.rolling(5).mean()
        ma20 = close.rolling(20).mean()
        ma60 = close.rolling(60).mean()

        cl   = close.values
        op   = open_.values
        vol  = volume.values
        m5   = ma5.values
        m20  = ma20.values
        m60  = ma60.values
        n    = len(cl)
        i    = n - 1

        if i < 5:
            return None

        # NaN 체크 (필수 값)
        need = [m5[i], m5[i-1], m5[i-2], m5[i-3],
                m20[i], m20[i-1], m20[i-2],
                m60[i], m60[i-1], m60[i-2]]
        if any(np.isnan(v) for v in need):
            return None

        # ── A: 3봉전 종가 < SMA5 ─────────────────────────────────────
        cond_A = cl[i-3] < m5[i-3]

        # ── B: 2봉전 종가 < SMA5 ─────────────────────────────────────
        cond_B = cl[i-2] < m5[i-2]

        # ── C: 1봉전 종가 < SMA5 ─────────────────────────────────────
        cond_C = cl[i-1] < m5[i-1]

        # ── D: 금일 시가 < SMA5 ───────────────────────────────────────
        cond_D = op[i] < m5[i]

        # ── E: 금일 종가 < SMA5 ───────────────────────────────────────
        cond_E = cl[i] < m5[i]

        # ── F: 양봉 (시가 < 종가) ─────────────────────────────────────
        cond_F = op[i] < cl[i]

        # ── G: SMA20 2봉 연속 상승 ────────────────────────────────────
        cond_G = (m20[i] > m20[i-1]) and (m20[i-1] > m20[i-2])

        # ── H: SMA60 2봉 연속 상승 ────────────────────────────────────
        cond_H = (m60[i] > m60[i-1]) and (m60[i-1] > m60[i-2])

        # ── J: 전일 기준 5봉 평균거래량 ≥ 10만주 ─────────────────────
        vol5_avg = float(np.mean(vol[n-6:n-1]))
        cond_J   = vol5_avg >= 100_000

        if not (cond_A and cond_B and cond_C and cond_D and
                cond_E and cond_F and cond_G and cond_H and cond_J):
            return None

        day_chg = round((cl[i] / cl[i-1] - 1) * 100, 2) if cl[i-1] > 0 else 0.0

        return {
            "종목코드":      code,
            "종가":          int(cl[i]),
            "전일대비(%)":   day_chg,
            "SMA5":          round(float(m5[i]), 2),
            "SMA20":         round(float(m20[i]), 2),
            "SMA60":         round(float(m60[i]), 2),
            "5일평균거래량":  int(vol5_avg),
            "신호":          "김승태타점4",
        }
    except Exception:
        return None


def run_screen51(date_str, prog):
    t_start = datetime.now()
    prog.update({"current": 0, "total": 0, "status": "loading"})
    end     = pd.Timestamp(date_str)
    start   = end - pd.Timedelta(days=LOOKBACK)
    listing = _get_listing()
    valid   = listing[
        (listing["Marcap"] >= 300_000_000_000) &
        ~listing["Market"].isin(["ETF", "ETN"]) &
        ~listing["Name"].str.match(_ETF_NAME_RE, na=False)
    ].copy()
    prog["total"]  = len(valid)
    prog["status"] = "running"
    rows    = _run_screen_parallel(valid, _screen51_ticker, start, end, prog)
    t_end   = datetime.now()
    elapsed = int((t_end - t_start).total_seconds())
    prog["status"] = "done"
    if not rows:
        return pd.DataFrame()
    df = (pd.DataFrame(rows)
          [["시장", "종목코드", "종목명", "시총(억)", "종가", "전일대비(%)",
            "SMA5", "SMA20", "SMA60", "5일평균거래량", "신호"]]
          .sort_values("전일대비(%)", ascending=False)
          .reset_index(drop=True))
    df["검색시각"] = t_end.strftime("%Y-%m-%d %H:%M:%S")
    df["소요(초)"] = elapsed
    return df


_rt46_scan_no      = 0
_rt46_scan_start   = ""
_rt46_last_scan    = ""
_rt46_next_scan    = ""
_rt46_scan_elapsed = 0

_RT46_INTERVAL_SEC = 600   # 조건46 실시간 재스캔 주기: 10분


def _run_realtime46():
    """조건46 MSL2 실시간 반복 스캔 루프 (10분 간격)"""
    global _rt46_scan_no, _rt46_scan_start, _rt46_last_scan, _rt46_next_scan, _rt46_scan_elapsed
    st = _state[46]
    print("[REALTIME46] 실시간 스캔 시작")

    while st["realtime"]:
        _rt46_scan_no   += 1
        t_start          = datetime.now()
        today_str        = t_start.strftime("%Y%m%d")
        _rt46_scan_start = t_start.strftime("%H:%M:%S")
        print(f"[REALTIME46] #{_rt46_scan_no} 스캔 시작 ({today_str} {_rt46_scan_start})")

        try:
            st["progress"] = {"current": 0, "total": 0, "status": "loading"}
            result_df = run_screen46(today_str, st["progress"])
            t_end              = datetime.now()
            _rt46_last_scan    = t_end.strftime("%H:%M:%S")
            _rt46_scan_elapsed = int((t_end - t_start).total_seconds())
            st["progress"].update({"status": "done"})

            if not result_df.empty:
                result_df["검색시각"] = _rt46_last_scan
                result_df["소요(초)"] = _rt46_scan_elapsed
                st["result_df"] = result_df

                prev_codes = st.get("known_codes", set())
                cur_codes  = set(result_df["종목코드"].astype(str).tolist())
                new_codes  = cur_codes - prev_codes
                st["new_codes"]   = new_codes
                st["known_codes"] = cur_codes

                if new_codes and prev_codes:
                    alert_time = t_end.strftime("%Y-%m-%d %H:%M:%S")
                    lines = [
                        f"[MSL2] #{_rt46_scan_no}회차 신규 {len(new_codes)}종목 — {alert_time}", ""
                    ]
                    lines.append("■ 신규 레벨돌파 종목")
                    for _, row in result_df[result_df["종목코드"].isin(new_codes)].iterrows():
                        lines.append(
                            f"  {row['종목명']} ({row['종목코드']}) "
                            f"| 시장: {row['시장']} "
                            f"| {row['전일대비(%)']}% "
                            f"| MSL2레벨: {row['MSL2레벨']:,} "
                            f"| HMA방향: {row['HMA방향']}"
                        )
                    lines += ["", f"스캔 #{_rt46_scan_no}회차 완료"]
                    _send_email_alert(
                        f"[MSL2] #{_rt46_scan_no}회차 신규 {len(new_codes)}종목 — {_rt46_last_scan}",
                        "\n".join(lines)
                    )
                    print(f"[REALTIME46] 신규 {len(new_codes)}개 → 이메일 발송")
                else:
                    print(f"[REALTIME46] #{_rt46_scan_no} {len(result_df)}종목 완료, 신규 없음")
            else:
                st["result_df"]  = pd.DataFrame()
                st["new_codes"]  = set()
                st["progress"]["status"] = "done"
                print(f"[REALTIME46] #{_rt46_scan_no} 결과 없음")

        except Exception as e:
            print(f"[REALTIME46] 오류: {e}")
            st["progress"]["status"] = "done"

        next_dt          = datetime.now() + timedelta(seconds=_RT46_INTERVAL_SEC)
        _rt46_next_scan  = next_dt.strftime("%H:%M:%S")
        for _ in range(_RT46_INTERVAL_SEC):
            if not st["realtime"]:
                break
            time.sleep(1)

    st["progress"]["status"] = "idle"
    _rt46_next_scan = ""
    print("[REALTIME46] 실시간 스캔 종료")


_rt28_scan_no      = 0
_rt28_scan_start   = ""
_rt28_last_scan    = ""
_rt28_next_scan    = ""
_rt28_scan_elapsed = 0


def _run_realtime28():
    """조건28 실시간 반복 스캔 루프"""
    global _rt28_scan_no, _rt28_scan_start, _rt28_last_scan, _rt28_next_scan, _rt28_scan_elapsed
    st = _state[28]
    print("[REALTIME28] 실시간 스캔 시작")
    while st["realtime"]:
        _rt28_scan_no += 1
        t_start   = datetime.now()
        today_str = t_start.strftime("%Y%m%d")
        _rt28_scan_start = t_start.strftime("%H:%M:%S")
        print(f"[REALTIME28] #{_rt28_scan_no} 스캔 시작 ({today_str} {_rt28_scan_start})")
        try:
            st["progress"] = {"current": 0, "total": 0, "status": "loading"}
            df = run_screen28(today_str, st["progress"])

            t_end = datetime.now()
            _rt28_last_scan    = t_end.strftime("%H:%M:%S")
            _rt28_scan_elapsed = int((t_end - t_start).total_seconds())

            if not df.empty:
                cur_codes = set(df["종목코드"].tolist())
                new       = cur_codes - st["known_codes"]
                st["new_codes"]    = new
                st["known_codes"].update(cur_codes)

                prev_df = st["result_df"]
                if not prev_df.empty and "검색시각" in prev_df.columns:
                    prev_ts = prev_df.set_index("종목코드")["검색시각"].to_dict()
                    df["검색시각"] = df["종목코드"].map(
                        lambda c: prev_ts.get(c, df.loc[df["종목코드"] == c, "검색시각"].iloc[0])
                    )

                st["result_df"]   = df
                st["result_date"] = today_str

                if new:
                    new_df = df[df["종목코드"].isin(new)]
                    print(f"[REALTIME28] 신규 {len(new)}개: {list(new)}")
                    alert_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                    lines = [f"[조건28 RSI밴드돌파] 신규 {len(new)}개 종목 발견 — {alert_time}", ""]
                    for _, r2 in new_df.iterrows():
                        lines.append(
                            f"  {r2['종목명']}({r2['종목코드']})  "
                            f"종가 {r2['종가']:,}  S_RSI {r2['S_RSI']}  UPPER {r2['UPPER']}  ADX(11) {r2['ADX(11)']}"
                        )
                    lines += ["", f"스캔 횟수: {_rt28_scan_no}회차"]
                    _send_email_alert(
                        f"[RSI밴드돌파] 신규 {len(new)}개 발견 — {alert_time}",
                        "\n".join(lines)
                    )
            else:
                st["result_df"]   = df
                st["result_date"] = today_str
                st["new_codes"]   = set()

        except Exception as e:
            print(f"[REALTIME28] 오류: {e}")

        next_dt = datetime.now() + timedelta(seconds=SCAN_INTERVAL_SEC)
        _rt28_next_scan = next_dt.strftime("%H:%M:%S")
        for _ in range(SCAN_INTERVAL_SEC):
            if not st["realtime"]:
                break
            time.sleep(1)

    st["progress"]["status"] = "idle"
    _rt28_next_scan = ""
    print("[REALTIME28] 실시간 스캔 종료")


_rt30_scan_no      = 0
_rt30_scan_start   = ""
_rt30_last_scan    = ""
_rt30_next_scan    = ""
_rt30_scan_elapsed = 0


def _run_realtime30():
    """조건30 실시간 반복 스캔 루프"""
    global _rt30_scan_no, _rt30_scan_start, _rt30_last_scan, _rt30_next_scan, _rt30_scan_elapsed
    st = _state[30]
    print("[REALTIME30] 실시간 스캔 시작")
    while st["realtime"]:
        _rt30_scan_no += 1
        t_start   = datetime.now()
        today_str = t_start.strftime("%Y%m%d")
        _rt30_scan_start = t_start.strftime("%H:%M:%S")
        print(f"[REALTIME30] #{_rt30_scan_no} 스캔 시작 ({today_str} {_rt30_scan_start})")
        try:
            st["progress"] = {"current": 0, "total": 0, "status": "loading"}
            df = run_screen30(today_str, st["progress"])

            t_end = datetime.now()
            _rt30_last_scan    = t_end.strftime("%H:%M:%S")
            _rt30_scan_elapsed = int((t_end - t_start).total_seconds())

            if not df.empty:
                cur_codes = set(df["종목코드"].tolist())
                new       = cur_codes - st["known_codes"]
                st["new_codes"]    = new
                st["known_codes"].update(cur_codes)

                prev_df = st["result_df"]
                if not prev_df.empty and "검색시각" in prev_df.columns:
                    prev_ts = prev_df.set_index("종목코드")["검색시각"].to_dict()
                    df["검색시각"] = df["종목코드"].map(
                        lambda c: prev_ts.get(c, df.loc[df["종목코드"] == c, "검색시각"].iloc[0])
                    )

                st["result_df"]   = df
                st["result_date"] = today_str

                if new:
                    new_df = df[df["종목코드"].isin(new)]
                    print(f"[REALTIME30] 신규 {len(new)}개: {list(new)}")
                    alert_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                    lines = [f"[조건30 VL급반등] 신규 {len(new)}개 종목 발견 — {alert_time}", ""]
                    for _, r2 in new_df.iterrows():
                        lines.append(
                            f"  {r2['종목명']}({r2['종목코드']})  "
                            f"종가 {r2['종가']:,}  VL일간 +{r2['VL일간상승(%)']}%  "
                            f"3일누적 +{r2['3일누적상승(%)']}%  낙폭 {r2['VL낙폭(%)']}%↓"
                        )
                    lines += ["", f"스캔 횟수: {_rt30_scan_no}회차"]
                    _send_email_alert(
                        f"[VL급반등] 신규 {len(new)}개 발견 — {alert_time}",
                        "\n".join(lines)
                    )
            else:
                st["result_df"]   = df
                st["result_date"] = today_str
                st["new_codes"]   = set()

        except Exception as e:
            print(f"[REALTIME30] 오류: {e}")

        next_dt = datetime.now() + timedelta(seconds=SCAN_INTERVAL_SEC)
        _rt30_next_scan = next_dt.strftime("%H:%M:%S")
        for _ in range(SCAN_INTERVAL_SEC):
            if not st["realtime"]:
                break
            time.sleep(1)

    st["progress"]["status"] = "idle"
    _rt30_next_scan = ""
    print("[REALTIME30] 실시간 스캔 종료")


_rt33_scan_no      = 0
_rt33_scan_start   = ""
_rt33_last_scan    = ""
_rt33_next_scan    = ""
_rt33_scan_elapsed = 0

_rt35_scan_no      = 0
_rt35_scan_start   = ""
_rt35_last_scan    = ""
_rt35_next_scan    = ""
_rt35_scan_elapsed = 0

_rt37_scan_no      = 0
_rt37_scan_start   = ""
_rt37_last_scan    = ""
_rt37_next_scan    = ""
_rt37_scan_elapsed = 0

_RT37_INTERVAL_SEC = 600   # 조건37 실시간 재스캔 주기: 10분

_rt39_scan_no      = 0
_rt39_scan_start   = ""
_rt39_last_scan    = ""
_rt39_next_scan    = ""
_rt39_scan_elapsed = 0

_RT39_INTERVAL_SEC = 600   # 조건39 실시간 재스캔 주기: 10분

_rt40_scan_no      = 0
_rt40_scan_start   = ""
_rt40_last_scan    = ""
_rt40_next_scan    = ""
_rt40_scan_elapsed = 0

_RT40_INTERVAL_SEC = 600   # 조건40 실시간 재스캔 주기: 10분

_rt41_scan_no      = 0
_rt41_scan_start   = ""
_rt41_last_scan    = ""
_rt41_next_scan    = ""
_rt41_scan_elapsed = 0

_RT41_INTERVAL_SEC = 600   # 조건41 실시간 재스캔 주기: 10분

_rt42_scan_no      = 0
_rt42_scan_start   = ""
_rt42_last_scan    = ""
_rt42_next_scan    = ""
_rt42_scan_elapsed = 0

_RT42_INTERVAL_SEC = 600   # 조건42 실시간 재스캔 주기: 10분


def _run_realtime33():
    """조건33 실시간 반복 스캔 루프"""
    global _rt33_scan_no, _rt33_scan_start, _rt33_last_scan, _rt33_next_scan, _rt33_scan_elapsed
    st = _state[33]
    print("[REALTIME33] 실시간 스캔 시작")
    while st["realtime"]:
        _rt33_scan_no += 1
        t_start   = datetime.now()
        today_str = t_start.strftime("%Y%m%d")
        _rt33_scan_start = t_start.strftime("%H:%M:%S")
        print(f"[REALTIME33] #{_rt33_scan_no} 스캔 시작 ({today_str} {_rt33_scan_start})")
        try:
            st["progress"] = {"current": 0, "total": 0, "status": "loading"}
            df = run_screen33(today_str, st["progress"])

            t_end = datetime.now()
            _rt33_last_scan    = t_end.strftime("%H:%M:%S")
            _rt33_scan_elapsed = int((t_end - t_start).total_seconds())

            if not df.empty:
                cur_codes = set(df["종목코드"].tolist())
                new       = cur_codes - st["known_codes"]
                st["new_codes"]    = new
                st["known_codes"].update(cur_codes)

                prev_df = st["result_df"]
                if not prev_df.empty and "검색시각" in prev_df.columns:
                    prev_ts = prev_df.set_index("종목코드")["검색시각"].to_dict()
                    df["검색시각"] = df["종목코드"].map(
                        lambda c: prev_ts.get(c, df.loc[df["종목코드"] == c, "검색시각"].iloc[0])
                    )

                st["result_df"]   = df
                st["result_date"] = today_str

                if new:
                    new_df = df[df["종목코드"].isin(new)]
                    print(f"[REALTIME33] 신규 {len(new)}개: {list(new)}")
                    alert_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                    lines = [f"[조건33 과열스코어] 신규 {len(new)}개 종목 발견 — {alert_time}", ""]
                    for _, r2 in new_df.iterrows():
                        lines.append(
                            f"  {r2['종목명']}({r2['종목코드']})  "
                            f"종가 {r2['종가']:,}  과열스코어 {r2['과열스코어']}  "
                            f"전주스코어 {r2['전주스코어']}  이격도 {r2['이격도(20w)']}%"
                        )
                    lines += ["", f"스캔 횟수: {_rt33_scan_no}회차"]
                    _send_email_alert(
                        f"[과열스코어] 신규 {len(new)}개 발견 — {alert_time}",
                        "\n".join(lines)
                    )
            else:
                st["result_df"]   = df
                st["result_date"] = today_str
                st["new_codes"]   = set()

        except Exception as e:
            print(f"[REALTIME33] 오류: {e}")

        next_dt = datetime.now() + timedelta(seconds=SCAN_INTERVAL_SEC)
        _rt33_next_scan = next_dt.strftime("%H:%M:%S")
        for _ in range(SCAN_INTERVAL_SEC):
            if not st["realtime"]:
                break
            time.sleep(1)

    st["progress"]["status"] = "idle"
    _rt33_next_scan = ""
    print("[REALTIME33] 실시간 스캔 종료")


def _run_realtime35():
    """조건35 실시간 반복 스캔 루프"""
    global _rt35_scan_no, _rt35_scan_start, _rt35_last_scan, _rt35_next_scan, _rt35_scan_elapsed
    st = _state[35]
    print("[REALTIME35] 실시간 스캔 시작")
    while st["realtime"]:
        _rt35_scan_no += 1
        t_start   = datetime.now()
        today_str = t_start.strftime("%Y%m%d")
        _rt35_scan_start = t_start.strftime("%H:%M:%S")
        print(f"[REALTIME35] #{_rt35_scan_no} 스캔 시작 ({today_str} {_rt35_scan_start})")
        try:
            st["progress"] = {"current": 0, "total": 0, "status": "loading"}
            df = run_screen35(today_str, st["progress"])

            t_end = datetime.now()
            _rt35_last_scan    = t_end.strftime("%H:%M:%S")
            _rt35_scan_elapsed = int((t_end - t_start).total_seconds())

            if not df.empty:
                cur_codes = set(df["종목코드"].tolist())
                new       = cur_codes - st["known_codes"]
                st["new_codes"]    = new
                st["known_codes"].update(cur_codes)

                prev_df = st["result_df"]
                if not prev_df.empty and "검색시각" in prev_df.columns:
                    prev_ts = prev_df.set_index("종목코드")["검색시각"].to_dict()
                    df["검색시각"] = df["종목코드"].map(
                        lambda c: prev_ts.get(c, df.loc[df["종목코드"] == c, "검색시각"].iloc[0])
                    )

                st["result_df"]   = df
                st["result_date"] = today_str

                if new:
                    new_df = df[df["종목코드"].isin(new)]
                    print(f"[REALTIME35] 신규 {len(new)}개: {list(new)}")
                    alert_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                    lines = [f"[조건35 거래대금순위] TOP10 변경 — {alert_time}", ""]
                    for _, r2 in new_df.iterrows():
                        lines.append(
                            f"  {r2['종목명']}({r2['종목코드']})  "
                            f"종가 {r2['종가']:,}  거래대금 {r2['거래대금(억)']:,.0f}억  "
                            f"전일대비 {r2['전일대비(%)']}%"
                        )
                    lines += ["", f"스캔 횟수: {_rt35_scan_no}회차"]
                    _send_email_alert(
                        f"[거래대금순위] TOP10 신규 {len(new)}개 — {alert_time}",
                        "\n".join(lines)
                    )
            else:
                st["result_df"]   = df
                st["result_date"] = today_str
                st["new_codes"]   = set()

        except Exception as e:
            print(f"[REALTIME35] 오류: {e}")

        next_dt = datetime.now() + timedelta(seconds=SCAN_INTERVAL_SEC)
        _rt35_next_scan = next_dt.strftime("%H:%M:%S")
        for _ in range(SCAN_INTERVAL_SEC):
            if not st["realtime"]:
                break
            time.sleep(1)

    st["progress"]["status"] = "idle"
    _rt35_next_scan = ""
    print("[REALTIME35] 실시간 스캔 종료")


def _run_realtime37():
    """조건37 거래대금RSI 실시간 반복 스캔 루프 (10분 간격)"""
    global _rt37_scan_no, _rt37_scan_start, _rt37_last_scan, _rt37_next_scan, _rt37_scan_elapsed
    st = _state[37]
    print("[REALTIME37] 실시간 스캔 시작")
    while st["realtime"]:
        _rt37_scan_no += 1
        t_start   = datetime.now()
        today_str = t_start.strftime("%Y%m%d")
        _rt37_scan_start = t_start.strftime("%H:%M:%S")
        print(f"[REALTIME37] #{_rt37_scan_no} 스캔 시작 ({today_str} {_rt37_scan_start})")
        try:
            st["progress"] = {"current": 0, "total": 0, "status": "loading"}
            df = run_screen37(today_str, st["progress"])

            t_end = datetime.now()
            _rt37_last_scan    = t_end.strftime("%H:%M:%S")
            _rt37_scan_elapsed = int((t_end - t_start).total_seconds())

            if not df.empty:
                cur_codes = set(df["종목코드"].tolist())
                new       = cur_codes - st["known_codes"]
                st["new_codes"]    = new
                st["known_codes"].update(cur_codes)

                prev_df = st["result_df"]
                if not prev_df.empty and "검색시각" in prev_df.columns:
                    prev_ts = prev_df.set_index("종목코드")["검색시각"].to_dict()
                    df["검색시각"] = df["종목코드"].map(
                        lambda c: prev_ts.get(c, df.loc[df["종목코드"] == c, "검색시각"].iloc[0])
                    )

                st["result_df"]   = df
                st["result_date"] = today_str

                if new:
                    new_df = df[df["종목코드"].isin(new)]
                    print(f"[REALTIME37] 신규 {len(new)}개: {list(new)}")
                    alert_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                    lines = [f"[조건37 거래대금RSI] 신규 {len(new)}개 종목 발견 — {alert_time}", ""]
                    for _, r2 in new_df.iterrows():
                        lines.append(
                            f"  {r2['종목명']}({r2['종목코드']})  "
                            f"종가 {r2['종가']:,}  거래대금 {r2['거래대금(억)']:,.1f}억  "
                            f"RSI중심선 {r2['RSI중심선']:,}  RSI상단 {r2['RSI밴드상단']:,}  "
                            f"신호: {r2['신호(RSI)']}"
                        )
                    lines += ["", f"스캔 횟수: {_rt37_scan_no}회차"]
                    _send_email_alert(
                        f"[거래대금RSI] 신규 {len(new)}개 발견 — {alert_time}",
                        "\n".join(lines)
                    )
            else:
                st["result_df"]   = df
                st["result_date"] = today_str
                st["new_codes"]   = set()

        except Exception as e:
            print(f"[REALTIME37] 오류: {e}")

        next_dt = datetime.now() + timedelta(seconds=_RT37_INTERVAL_SEC)
        _rt37_next_scan = next_dt.strftime("%H:%M:%S")
        for _ in range(_RT37_INTERVAL_SEC):
            if not st["realtime"]:
                break
            time.sleep(1)

    st["progress"]["status"] = "idle"
    _rt37_next_scan = ""
    print("[REALTIME37] 실시간 스캔 종료")


def _run_realtime39():
    """조건39 VL이격시작 실시간 반복 스캔 루프 (10분 간격)"""
    global _rt39_scan_no, _rt39_scan_start, _rt39_last_scan, _rt39_next_scan, _rt39_scan_elapsed
    st = _state[39]
    print("[REALTIME39] 실시간 스캔 시작")
    while st["realtime"]:
        _rt39_scan_no += 1
        t_start   = datetime.now()
        today_str = t_start.strftime("%Y%m%d")
        _rt39_scan_start = t_start.strftime("%H:%M:%S")
        print(f"[REALTIME39] #{_rt39_scan_no} 스캔 시작 ({today_str} {_rt39_scan_start})")
        try:
            st["progress"] = {"current": 0, "total": 0, "status": "loading"}
            df = run_screen39(today_str, st["progress"])

            t_end = datetime.now()
            _rt39_last_scan    = t_end.strftime("%H:%M:%S")
            _rt39_scan_elapsed = int((t_end - t_start).total_seconds())

            if not df.empty:
                cur_codes = set(df["종목코드"].tolist())
                new       = cur_codes - st["known_codes"]
                st["new_codes"]    = new
                st["known_codes"].update(cur_codes)

                prev_df = st["result_df"]
                if not prev_df.empty and "검색시각" in prev_df.columns:
                    prev_ts = prev_df.set_index("종목코드")["검색시각"].to_dict()
                    df["검색시각"] = df["종목코드"].map(
                        lambda c: prev_ts.get(c, df.loc[df["종목코드"] == c, "검색시각"].iloc[0])
                    )

                st["result_df"]   = df
                st["result_date"] = today_str

                if new:
                    new_df = df[df["종목코드"].isin(new)]
                    print(f"[REALTIME39] 신규 {len(new)}개: {list(new)}")
                    alert_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                    lines = [f"[조건39 VL이격시작] 신규 {len(new)}개 종목 발견 — {alert_time}", ""]
                    for _, r2 in new_df.iterrows():
                        lines.append(
                            f"  {r2['종목명']}({r2['종목코드']})  "
                            f"종가 {int(r2['종가']):,}  전일대비 {r2['전일대비(%)']}%  "
                            f"VL값 {int(r2['VL값']):,}  VL이격 {r2['VL이격(%)']}%  "
                            f"60일고가 {int(r2['60일고가']):,}"
                        )
                    lines += ["", f"스캔 횟수: {_rt39_scan_no}회차"]
                    _send_email_alert(
                        f"[VL이격시작] 신규 {len(new)}개 발견 — {alert_time}",
                        "\n".join(lines)
                    )
            else:
                st["result_df"]   = df
                st["result_date"] = today_str
                st["new_codes"]   = set()

        except Exception as e:
            print(f"[REALTIME39] 오류: {e}")

        next_dt = datetime.now() + timedelta(seconds=_RT39_INTERVAL_SEC)
        _rt39_next_scan = next_dt.strftime("%H:%M:%S")
        for _ in range(_RT39_INTERVAL_SEC):
            if not st["realtime"]:
                break
            time.sleep(1)

    st["progress"]["status"] = "idle"
    _rt39_next_scan = ""
    print("[REALTIME39] 실시간 스캔 종료")


def _run_realtime22():
    """조건22 실시간 반복 스캔 루프"""
    global _rt22_scan_no, _rt22_scan_start, _rt22_last_scan, _rt22_next_scan, _rt22_scan_elapsed
    st = _state[22]
    print("[REALTIME22] 실시간 스캔 시작")
    while st["realtime"]:
        _rt22_scan_no += 1
        t_start   = datetime.now()
        today_str = t_start.strftime("%Y%m%d")
        _rt22_scan_start = t_start.strftime("%H:%M:%S")
        print(f"[REALTIME22] #{_rt22_scan_no} 스캔 시작 ({today_str}  {_rt22_scan_start})")
        try:
            st["progress"] = {"current": 0, "total": 0, "status": "loading"}
            df = run_screen22(today_str, st["progress"])

            t_end = datetime.now()
            _rt22_last_scan    = t_end.strftime("%H:%M:%S")
            _rt22_scan_elapsed = int((t_end - t_start).total_seconds())

            if not df.empty:
                cur_codes = set(df["종목코드"].tolist())
                new       = cur_codes - st["known_codes"]
                st["new_codes"] = new
                st["known_codes"].update(cur_codes)

                st["result_df"]   = df
                st["result_date"] = today_str

                if new:
                    new_df = df[df["종목코드"].isin(new)]
                    print(f"[REALTIME22] 신규 {len(new)}개: {list(new)}")
                    alert_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                    lines = [
                        f"알림 시간: {alert_time}",
                        f"기준일:   {today_str}",
                        f"스캔 횟수: {_rt22_scan_no}회차",
                        ""
                    ]
                    for _, r in new_df.iterrows():
                        lines += [
                            f"▶ {r['종목명']} ({r['종목코드']}) [{r['시장']}]",
                            f"   종가: {int(r['종가']):,}원 | 전일대비: {r['전일대비(%)']}%",
                            f"   저항선: {int(r['저항선']):,}원 | 저항선대비: {r['저항선대비(%)']}%",
                            f"   음봉거래량배수: {r['음봉거래량배수']}×", ""
                        ]
                    ok, err = _send_email_alert(
                        f"[캔들볼륨] 신규 {len(new)}개 종목 ({datetime.now().strftime('%H:%M')})",
                        "\n".join(lines)
                    )
                    if not ok:
                        print(f"[REALTIME22] 이메일 발송 실패: {err}", flush=True)
            else:
                st["new_codes"] = set()

        except Exception as e:
            print(f"[REALTIME22] 오류: {e}")
            st["progress"]["status"] = "done"

        next_dt = datetime.now() + timedelta(seconds=SCAN_INTERVAL_SEC)
        _rt22_next_scan = next_dt.strftime("%H:%M:%S")
        for _ in range(SCAN_INTERVAL_SEC):
            if not st["realtime"]:
                break
            time.sleep(1)

    st["progress"]["status"] = "idle"
    _rt22_next_scan = ""
    print("[REALTIME22] 실시간 스캔 종료")


# ══════════════════════════════════════════════════════════════════════════════
# 조건21: 테마 상승률 상위 5개 · 테마별 양봉(시가<종가) 종목 전체 표시
# ══════════════════════════════════════════════════════════════════════════════

_NAVER_HDR = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) "
                   "Chrome/120.0.0.0 Safari/537.36"),
    "Accept-Language": "ko-KR,ko;q=0.9",
    "Referer": "https://finance.naver.com/",
}
_NAVER_BASE    = "https://finance.naver.com"
_TOP_N_THEMES  = 5   # 상위 테마 개수


def _naver_fetch(url: str) -> str:
    """네이버 금융 페이지 EUC-KR 디코딩"""
    resp = _req.get(url, headers=_NAVER_HDR, timeout=15)
    resp.encoding = "euc-kr"
    return resp.text


def _naver_theme_list() -> list:
    """테마 목록 → [{code, name, rate}, ...] 등락률 내림차순
    URL: /sise/sise_group.naver?type=theme
    """
    html = _naver_fetch(f"{_NAVER_BASE}/sise/sise_group.naver?type=theme")
    matches = _re.findall(
        r'sise_group_detail\.naver\?type=theme&no=(\d+)"[^>]*>([^<]+)</a>'
        r'</td>\s*<td[^>]*>\s*<span[^>]*>\s*([+-]?\d+\.?\d*)%',
        html,
    )
    seen, themes = set(), []
    for code, name, rate_str in matches:
        if code not in seen:
            seen.add(code)
            try:
                rate = float(rate_str)
            except ValueError:
                rate = 0.0
            themes.append({"code": code, "name": name.strip(), "rate": rate})
    # 내림차순 정렬 (페이지가 이미 정렬돼 있지만 보장)
    themes.sort(key=lambda x: x["rate"], reverse=True)
    return themes


def _naver_theme_stocks(theme_code: str, theme_name: str, theme_rate: float) -> list:
    """테마 구성종목 전체 → [{code, name, current, open_p, change_rate, theme, theme_rate}, ...]
    URL: /sise/sise_group_detail.naver?type=theme&no=XXX
    """
    url = f"{_NAVER_BASE}/sise/sise_group_detail.naver?type=theme&no={theme_code}"
    try:
        html = _naver_fetch(url)
    except Exception:
        return []

    stocks = []
    for row in _re.split(r'<tr[^>]*>', html):
        code_m = _re.search(r'item/main\.naver\?code=(\d{6})"[^>]*>([^<]+)</a>', row)
        if not code_m:
            continue
        code = code_m.group(1)
        name = code_m.group(2).strip()
        rate_m = _re.search(r'tah p11 (?:red|blue)\d+">\s*([+-]?\d+\.?\d*)\s*%', row)
        rate = float(rate_m.group(1)) if rate_m else 0.0
        nums = _re.findall(r'<td class="number"[^>]*>\s*([0-9,]+)\s*</td>', row)
        current = int(nums[0].replace(',', '')) if nums else 0
        open_p  = int(nums[1].replace(',', '')) if len(nums) > 1 else 0
        stocks.append({
            'code': code, 'name': name,
            'current': current, 'open_p': open_p,
            'change_rate': rate,
            'theme': theme_name, 'theme_rate': theme_rate,
        })
    return stocks


def run_screen21(date_str, prog):
    """조건21: 테마 상승률 상위 5개 · 양봉(시가<종가) 구성종목 전체"""
    prog.update({"current": 0, "total": 0, "status": "loading"})
    try:
        all_themes = _naver_theme_list()
    except Exception as e:
        _plog(f"[C21] 테마목록 오류: {e}")
        prog["status"] = "done"
        return pd.DataFrame()

    top_themes = all_themes[:_TOP_N_THEMES]
    if not top_themes:
        prog["status"] = "done"
        return pd.DataFrame()

    _plog(f"[C21] 상위 {len(top_themes)}개 테마: " +
          ", ".join(f"{t['name']}({t['rate']:+.2f}%)" for t in top_themes))

    prog["total"] = len(top_themes)
    prog["status"] = "running"

    rows = []
    for i, theme in enumerate(top_themes, 1):
        prog["current"] = i
        stocks = _naver_theme_stocks(theme["code"], theme["name"], theme["rate"])
        _plog(f"[C21] '{theme['name']}': 전체 {len(stocks)}종목")
        for s in stocks:
            if s["current"] <= s["open_p"] or s["open_p"] == 0:
                continue   # 양봉(시가<종가) 아닌 종목 제외
            rows.append({
                "테마":          s["theme"],
                "테마등락률(%)": round(s["theme_rate"], 2),
                "종목코드":      s["code"],
                "종목명":        s["name"],
                "현재가":        s["current"],
                "시가":          s["open_p"],
                "등락률(%)":     round(s["change_rate"], 2),
                "검색시각":      datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            })

    prog["status"] = "done"
    if not rows:
        return pd.DataFrame()

    return (pd.DataFrame(rows)
            .sort_values(["테마등락률(%)", "등락률(%)"], ascending=[False, False])
            .reset_index(drop=True))


def _run_realtime21():
    """조건21 실시간 반복 스캔 루프"""
    global _rt21_scan_no, _rt21_scan_start, _rt21_last_scan, _rt21_next_scan, _rt21_scan_elapsed
    st = _state[21]
    print("[REALTIME21] 실시간 스캔 시작")
    while st["realtime"]:
        _rt21_scan_no += 1
        t_start   = datetime.now()
        today_str = t_start.strftime("%Y%m%d")
        _rt21_scan_start = t_start.strftime("%H:%M:%S")
        print(f"[REALTIME21] #{_rt21_scan_no} 스캔 시작 ({today_str}  {_rt21_scan_start})")
        try:
            st["progress"] = {"current": 0, "total": 0, "status": "loading"}
            df = run_screen21(today_str, st["progress"])

            t_end = datetime.now()
            _rt21_last_scan    = t_end.strftime("%H:%M:%S")
            _rt21_scan_elapsed = int((t_end - t_start).total_seconds())

            if not df.empty:
                cur_codes = set(df["종목코드"].tolist())
                new       = cur_codes - st["known_codes"]
                st["new_codes"] = new
                st["known_codes"].update(cur_codes)

                prev_df = st["result_df"]
                if not prev_df.empty and "검색시각" in prev_df.columns:
                    prev_ts = prev_df.set_index("종목코드")["검색시각"].to_dict()
                    df["검색시각"] = df["종목코드"].map(
                        lambda c: prev_ts.get(c, df.loc[df["종목코드"] == c, "검색시각"].iloc[0])
                    )

                st["result_df"]   = df
                st["result_date"] = today_str

                if new:
                    new_df = df[df["종목코드"].isin(new)]
                    print(f"[REALTIME21] 신규 {len(new)}개: {list(new)}")
                    alert_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                    lines = [
                        f"알림 시간: {alert_time}",
                        f"기준일:   {today_str}",
                        f"스캔 횟수: {_rt21_scan_no}회차",
                        ""
                    ]
                    for _, r in new_df.iterrows():
                        try:
                            found_at = str(r["검색시각"])
                        except (KeyError, TypeError):
                            found_at = alert_time
                        lines += [
                            f"▶ {r['종목명']} ({r['종목코드']})",
                            f"   테마: {r['테마']} (테마등락률: {r['테마등락률(%)']}%)",
                            f"   검색시각: {found_at}",
                            f"   현재가: {int(r['현재가']):,}원 | 시가: {int(r['시가']):,}원 | 등락률: {r['등락률(%)']}%",
                            ""
                        ]
                    ok, err = _send_email_alert(
                        f"[테마상위3] 신규 {len(new)}개 종목 ({datetime.now().strftime('%H:%M')})",
                        "\n".join(lines)
                    )
                    if not ok:
                        print(f"[REALTIME21] 이메일 발송 실패: {err}", flush=True)
            else:
                st["new_codes"] = set()

        except Exception as e:
            print(f"[REALTIME21] 오류: {e}")
            st["progress"]["status"] = "done"

        next_dt = datetime.now() + timedelta(seconds=SCAN_INTERVAL_SEC)
        _rt21_next_scan = next_dt.strftime("%H:%M:%S")
        for _ in range(SCAN_INTERVAL_SEC):
            if not st["realtime"]:
                break
            time.sleep(1)

    st["progress"]["status"] = "idle"
    _rt21_next_scan = ""
    print("[REALTIME21] 실시간 스캔 종료")


def _screen32_ticker(code, start, end):
    """조건32 캔들볼륨저항: 최근 90봉 음봉 중 (O+C+H+L)/4×거래량 최대인 봉의 시가 = 저항선, 금일 종가 > 저항선"""
    try:
        df = _fdr_safe(code, start, end)
        if df is None or len(df) < 92:
            return None
        df = df.copy()
        df = df[df["Close"] > 0].dropna(subset=["Open", "High", "Low", "Close", "Volume"])
        if len(df) < 92:
            return None

        # 최근 90봉 (금일 포함)
        recent = df.iloc[-90:].copy()

        # 음봉 필터: Close < Open
        bearish = recent[recent["Close"] < recent["Open"]].copy()
        if bearish.empty:
            return None

        # (O+C+H+L)/4 × 거래량 계산
        bearish = bearish.copy()
        bearish["_score"] = ((bearish["Open"] + bearish["Close"] +
                              bearish["High"] + bearish["Low"]) / 4.0) * bearish["Volume"].astype(float)

        # 최대값 봉 → 시가 = 저항선
        max_idx   = bearish["_score"].idxmax()
        resistance = float(bearish.loc[max_idx, "Open"])
        resist_date = str(max_idx)[:10]

        # 신규 돌파: 오늘 종가 > 저항선 AND 어제 종가 <= 저항선
        today_close = float(df["Close"].iloc[-1])
        prev_close  = float(df["Close"].iloc[-2])
        if today_close <= resistance:
            return None
        if prev_close > resistance:          # 어제 이미 돌파 → 제외
            return None

        # ADX(11) > 25 — 추세 강도 필터
        adx_s   = _calc_adx(df, period=11)
        adx_now = float(adx_s.iloc[-1])
        if np.isnan(adx_now) or adx_now <= 25.0:
            return None

        pct_chg    = round((today_close / prev_close - 1) * 100, 2) if prev_close > 0 else 0.0
        resist_pct = round((today_close / resistance - 1) * 100, 2)

        return {
            "종목코드":     code,
            "종가":         int(today_close),
            "전일대비(%)":  pct_chg,
            "캔들저항선":   int(resistance),
            "저항대비(%)":  resist_pct,
            "저항봉날짜":   resist_date,
            "ADX(11)":      round(adx_now, 1),
        }
    except Exception:
        return None


def run_screen32(date_str, prog):
    t_start = datetime.now()
    prog.update({"current": 0, "total": 0, "status": "loading"})
    end     = pd.Timestamp(date_str)
    start   = end - pd.Timedelta(days=200)
    listing = _get_listing_with_progress(prog)
    valid   = listing[
        (listing["Marcap"] >= MIN_CAP) &
        (~listing["Market"].isin(["ETF", "ETN"]))
    ].copy()
    prog["total"]  = len(valid)
    prog["status"] = "running"
    rows    = _run_screen_parallel(valid, _screen32_ticker, start, end, prog)
    t_end   = datetime.now()
    elapsed = int((t_end - t_start).total_seconds())
    prog["status"] = "done"
    if not rows:
        return pd.DataFrame()
    df = (pd.DataFrame(rows)
          [["시장", "종목코드", "종목명", "시총(억)", "종가", "전일대비(%)",
            "캔들저항선", "저항대비(%)", "저항봉날짜", "ADX(11)"]]
          .sort_values("저항대비(%)", ascending=True)
          .reset_index(drop=True))
    df["검색시각"] = t_end.strftime("%Y-%m-%d %H:%M:%S")
    df["소요(초)"] = elapsed
    return df


def _screen33_ticker(code, start, end):
    """조건33 과열스코어(주봉): ATR(5w)/ATR(20w)×MA(V5w)/MA(V20w)×이격도(20w)/100 > 3, 이번 주 첫 충족"""
    try:
        df = _fdr_safe(code, start, end)
        if df is None or len(df) < 30:
            return None
        df = df.copy()
        df = df[df["Close"] > 0].dropna(subset=["Open", "High", "Low", "Close", "Volume"])

        # 일봉 → 주봉
        dfw = _resample_weekly(df)
        if len(dfw) < 23:   # 20주 롤링 + 비교 2개 + 여유
            return None

        hi = dfw["High"].astype(float)
        lo = dfw["Low"].astype(float)
        cl = dfw["Close"].astype(float)
        op = dfw["Open"].astype(float)
        vo = dfw["Volume"].astype(float)

        # True Range (주봉)
        prev_cl = cl.shift(1)
        tr = pd.concat([hi - lo,
                         (hi - prev_cl).abs(),
                         (lo - prev_cl).abs()], axis=1).max(axis=1)

        # ATR(5w), ATR(20w) — SMA of TR
        atr5  = tr.rolling(5).mean()
        atr20 = tr.rolling(20).mean()

        # MA(V,5w), MA(V,20w)
        mav5  = vo.rolling(5).mean()
        mav20 = vo.rolling(20).mean()

        # 이격도(20w) = Close / SMA(20w) × 100
        sma20  = cl.rolling(20).mean()
        disp20 = cl / sma20 * 100.0

        # 과열스코어
        score = (atr5 / atr20) * (mav5 / mav20) * (disp20 / 100.0)

        cur_score  = float(score.iloc[-1])
        prev_score = float(score.iloc[-2])
        if np.isnan(cur_score) or np.isnan(prev_score):
            return None

        # 이번 주 첫 충족: 이번 > 3, 지난주 ≤ 3
        if cur_score <= 3.0:
            return None
        if prev_score > 3.0:   # 지난주 이미 충족 → 제외
            return None

        # 이번 주 양봉
        if float(cl.iloc[-1]) <= float(op.iloc[-1]):
            return None

        # 이번 주 거래량 > 지난 주 거래량
        if float(vo.iloc[-1]) <= float(vo.iloc[-2]):
            return None

        # 전일대비(%) — 일봉 최근 2일 기준
        today_close = float(df["Close"].iloc[-1])
        prev_daily  = float(df["Close"].iloc[-2])
        pct_chg     = round((today_close / prev_daily - 1) * 100, 2) if prev_daily > 0 else 0.0

        return {
            "종목코드":     code,
            "종가":         int(today_close),
            "전일대비(%)":  pct_chg,
            "과열스코어":   round(cur_score, 2),
            "전주스코어":   round(prev_score, 2),
            "ATR비율(주)":  round(float(atr5.iloc[-1] / atr20.iloc[-1]), 2),
            "거래량비율(주)": round(float(mav5.iloc[-1] / mav20.iloc[-1]), 2),
            "이격도(20w)":  round(float(disp20.iloc[-1]), 1),
        }
    except Exception:
        return None


def run_screen33(date_str, prog):
    t_start = datetime.now()
    prog.update({"current": 0, "total": 0, "status": "loading"})
    end     = pd.Timestamp(date_str)
    start   = end - pd.Timedelta(days=LOOKBACK)
    listing = _get_listing_with_progress(prog)
    valid   = listing[
        (listing["Marcap"] >= MIN_CAP) &
        (~listing["Market"].isin(["ETF", "ETN"]))
    ].copy()
    prog["total"]  = len(valid)
    prog["status"] = "running"
    rows    = _run_screen_parallel(valid, _screen33_ticker, start, end, prog)
    t_end   = datetime.now()
    elapsed = int((t_end - t_start).total_seconds())
    prog["status"] = "done"
    if not rows:
        return pd.DataFrame()
    df = (pd.DataFrame(rows)
          [["시장", "종목코드", "종목명", "시총(억)", "종가", "전일대비(%)",
            "과열스코어", "전주스코어", "ATR비율(주)", "거래량비율(주)", "이격도(20w)"]]
          .sort_values("과열스코어", ascending=False)
          .reset_index(drop=True))
    df["검색시각"] = t_end.strftime("%Y-%m-%d %H:%M:%S")
    df["소요(초)"] = elapsed
    return df


def _screen34_ticker(code, start, end):
    """조건34 과열스코어순위(일봉): 스코어>3 + 양봉 + 거래량증가, 전일/2일전 대비 상승점수 반환"""
    try:
        df = _fdr_safe(code, start, end)
        if df is None or len(df) < 26:
            return None
        df = df.copy()
        df = df[df["Close"] > 0].dropna(subset=["Open", "High", "Low", "Close", "Volume"])
        if len(df) < 26:
            return None

        hi = df["High"].astype(float)
        lo = df["Low"].astype(float)
        cl = df["Close"].astype(float)
        vo = df["Volume"].astype(float)

        prev_cl = cl.shift(1)
        tr = pd.concat([hi - lo,
                         (hi - prev_cl).abs(),
                         (lo - prev_cl).abs()], axis=1).max(axis=1)

        atr5  = tr.rolling(5).mean()
        atr20 = tr.rolling(20).mean()
        mav5  = vo.rolling(5).mean()
        mav20 = vo.rolling(20).mean()
        sma20  = cl.rolling(20).mean()
        disp20 = cl / sma20 * 100.0
        score  = (atr5 / atr20) * (mav5 / mav20) * (disp20 / 100.0)

        today_score = float(score.iloc[-1])
        score_1d    = float(score.iloc[-2])
        score_2d    = float(score.iloc[-3])

        if np.isnan(today_score) or np.isnan(score_1d) or np.isnan(score_2d):
            return None

        # 스코어 > 3
        if today_score <= 3.0:
            return None

        # 금일 양봉
        if float(cl.iloc[-1]) <= float(df["Open"].astype(float).iloc[-1]):
            return None

        # 전일보다 거래량 높음
        if float(vo.iloc[-1]) <= float(vo.iloc[-2]):
            return None

        today_close = float(cl.iloc[-1])
        today_high  = float(hi.iloc[-1])
        prev_close  = float(cl.iloc[-2])
        pct_chg     = round((today_close / prev_close - 1) * 100, 2) if prev_close > 0 else 0.0

        # Envelope(20, 40%) 상단 = SMA(20) × 1.40
        env_upper     = float(sma20.iloc[-1]) * 1.40
        env_not_break = (today_high <= env_upper) and (today_close <= env_upper)

        return {
            "종목코드":      code,
            "종가":          int(today_close),
            "전일대비(%)":   pct_chg,
            "과열스코어":    round(today_score, 3),
            "전일스코어":    round(score_1d,    3),
            "2일전스코어":   round(score_2d,    3),
            "상승점수(1일)": round(today_score - score_1d, 3),
            "상승점수(2일)": round(today_score - score_2d, 3),
            "ATR비율":       round(float(atr5.iloc[-1] / atr20.iloc[-1]), 2),
            "거래량비율":    round(float(mav5.iloc[-1] / mav20.iloc[-1]), 2),
            "이격도(20)":    round(float(disp20.iloc[-1]), 1),
            "엔벨상단":      round(env_upper, 0),
            "엔벨미초과":    env_not_break,
        }
    except Exception:
        return None


def run_screen34(date_str, prog):
    t_start = datetime.now()
    prog.update({"current": 0, "total": 0, "status": "loading"})
    end     = pd.Timestamp(date_str)
    start   = end - pd.Timedelta(days=LOOKBACK)
    listing = _get_listing_with_progress(prog)
    valid   = listing[
        (listing["Marcap"] >= MIN_CAP) &
        (~listing["Market"].isin(["ETF", "ETN"]))
    ].copy()
    prog["total"]  = len(valid)
    prog["status"] = "running"
    rows    = _run_screen_parallel(valid, _screen34_ticker, start, end, prog)
    t_end   = datetime.now()
    elapsed = int((t_end - t_start).total_seconds())
    prog["status"] = "done"
    if not rows:
        return pd.DataFrame()

    base_cols = ["시장", "종목코드", "종목명", "시총(억)", "종가", "전일대비(%)",
                 "과열스코어", "전일스코어", "2일전스코어",
                 "상승점수(1일)", "상승점수(2일)", "ATR비율", "거래량비율", "이격도(20)",
                 "엔벨상단", "엔벨미초과"]
    df_pool = pd.DataFrame(rows)[base_cols]

    # 결과1: 전일 대비 상승점수 상위 20
    df1 = (df_pool.nlargest(20, "상승점수(1일)")
           .reset_index(drop=True)
           .assign(구분="전일비교"))

    # 결과2: 2일전 대비 상승점수 상위 20
    df2 = (df_pool.nlargest(20, "상승점수(2일)")
           .reset_index(drop=True)
           .assign(구분="2일전비교"))

    # 결과3: 결과1 OR 결과2 종목 중 엔벨로프 미초과
    union_codes = set(df1["종목코드"]) | set(df2["종목코드"])
    r1_codes    = set(df1["종목코드"])
    r2_codes    = set(df2["종목코드"])

    df3_base = df_pool[
        df_pool["종목코드"].isin(union_codes) &
        (df_pool["엔벨미초과"] == True)
    ].copy()

    def _result_label(code):
        in1, in2 = code in r1_codes, code in r2_codes
        return "R1+R2" if (in1 and in2) else ("R1" if in1 else "R2")

    df3_base["포함결과"] = df3_base["종목코드"].map(_result_label)
    df3 = (df3_base
           .sort_values("과열스코어", ascending=False)
           .reset_index(drop=True)
           .assign(구분="엔벨미초과"))

    df = pd.concat([df1, df2, df3], ignore_index=True)
    # NaN → "" (포함결과 컬럼은 df3에만 존재 → df1/df2 rows는 NaN → JSON 직렬화 오류 방지)
    df["포함결과"] = df["포함결과"].fillna("")
    # 불리언 → 문자열 변환 (JSON NaN 방지)
    df["엔벨미초과"] = df["엔벨미초과"].astype(str)
    df["검색시각"] = t_end.strftime("%Y-%m-%d %H:%M:%S")
    df["소요(초)"] = elapsed
    return df


MIN_CAP_35 = 300_000_000_000        # 조건35 시총 하한: 3000억
MAX_CAP_35 = 20_000_000_000_000     # 조건35 시총 상한: 20조 미만

# FDR listing 에서 ETF가 Market='KOSPI'로 들어오는 경우 이름 패턴으로 추가 제외
_ETF_NAME_RE = _re.compile(
    r'^(TIGER|KODEX|KBSTAR|HANARO|KOSEF|ARIRANG|ACE|SOL|TIMEFOLIO|SMART|MASTER|'
    r'PLUS|TREX|GIANT|PIONEER|KTOP|KINDEX|파워|KoAct|WON|FOCUS|BOOKOO|LAVIE)',
    _re.IGNORECASE
)


def _screen35_ticker(code, start, end):
    """조건35 거래대금순위: 거래량 × (O+H+L+C)/4 (당일 + 전일 동시 계산)"""
    try:
        df = _fdr_safe(code, start, end)
        if df is None or len(df) < 3:
            return None
        df = df.copy()
        # 당일
        last = df.iloc[-1]
        O = float(last["Open"]); H = float(last["High"])
        L = float(last["Low"]);  C = float(last["Close"]); V = float(last["Volume"])
        if C <= 0 or V <= 0:
            return None
        typical_today = (O + H + L + C) / 4.0
        tv_today      = V * typical_today / 100_000_000     # 억
        # 전일
        prev = df.iloc[-2]
        O2 = float(prev["Open"]); H2 = float(prev["High"])
        L2 = float(prev["Low"]);  C2 = float(prev["Close"]); V2 = float(prev["Volume"])
        typical_prev = (O2 + H2 + L2 + C2) / 4.0
        tv_prev      = V2 * typical_prev / 100_000_000       # 억
        pct_chg = round((C / C2 - 1) * 100, 2) if C2 > 0 else 0.0
        return {
            "종목코드":       code,
            "종가":           int(C),
            "전일대비(%)":    pct_chg,
            "거래대금(억)":   round(tv_today, 1),
            "전일거래대금(억)": round(tv_prev, 1),   # 전일 TOP3 판별용 (출력 제외)
            "거래량":         int(V),
            "평균단가":       round(typical_today, 0),
        }
    except Exception:
        return None


def run_screen35(date_str, prog):
    t_start = datetime.now()
    prog.update({"current": 0, "total": 0, "status": "loading"})
    end     = pd.Timestamp(date_str)
    start   = end - pd.Timedelta(days=LOOKBACK)
    listing = _get_listing_with_progress(prog)
    valid   = listing[
        (listing["Marcap"] >= MIN_CAP_35) &
        (listing["Marcap"] <  MAX_CAP_35) &                         # 20조 미만
        (~listing["Market"].isin(["ETF", "ETN"])) &                 # Market 기반 제외
        (~listing["Name"].str.match(_ETF_NAME_RE, na=False))         # 이름 패턴 기반 ETF 추가 제외
    ].copy()
    prog["total"]  = len(valid)
    prog["status"] = "running"
    rows    = _run_screen_parallel(valid, _screen35_ticker, start, end, prog)
    t_end   = datetime.now()
    elapsed = int((t_end - t_start).total_seconds())
    prog["status"] = "done"
    if not rows:
        return pd.DataFrame()

    all_cols = ["시장", "종목코드", "종목명", "시총(억)", "종가", "전일대비(%)",
                "거래대금(억)", "전일거래대금(억)", "거래량", "평균단가"]
    df_pool = pd.DataFrame(rows)[all_cols]

    # 전일 거래대금 1~3위 종목 제외
    prev_top3 = set(df_pool.nlargest(3, "전일거래대금(억)")["종목코드"].tolist())

    df_filtered = df_pool[~df_pool["종목코드"].isin(prev_top3)].copy()

    out_cols = ["시장", "종목코드", "종목명", "시총(억)", "종가", "전일대비(%)",
                "거래대금(억)", "거래량", "평균단가"]
    df = (df_filtered.nlargest(10, "거래대금(억)")[out_cols]
          .reset_index(drop=True))
    df["검색시각"] = t_end.strftime("%Y-%m-%d %H:%M:%S")
    df["소요(초)"] = elapsed
    return df


def _screen36_ticker(code, start, end):
    """조건36 이제진짜출발: 조건23(세력20평단 ±10% 5일 + ADX>25) AND 조건34(일봉 과열스코어≥3) 동시 충족"""
    try:
        df = _fdr_safe(code, start, end)
        if df is None:
            return None
        df = df.copy()
        df = df[df["Close"] > 0].dropna(subset=["Open", "High", "Low", "Close", "Volume"])

        # ── 공통 최소 길이 (조건23 min_len이 binding) ─────────────────
        min_len = 60 + 20 + 5 + 10   # 95봉
        if len(df) < min_len:
            return None

        close_s = df["Close"].astype(float)
        hi_d    = df["High"].astype(float)
        lo_d    = df["Low"].astype(float)
        vo_d    = df["Volume"].astype(float)

        # ── 조건23 체크 (일봉) ────────────────────────────────────────
        # 5일 평균 거래량 30만주 이상 (금일 제외)
        if vo_d.iloc[-6:-1].mean() < 300_000:
            return None

        # 세력20평단
        sp = _calc_세력평단(df)
        if sp.iloc[-5:].isna().any():
            return None

        # 최근 5거래일 연속 |종가 - 평단| / 평단 ≤ 10%
        for i in range(-5, 0):
            c_i  = float(close_s.iloc[i])
            sp_i = float(sp.iloc[i])
            if sp_i <= 0:
                return None
            if abs(c_i - sp_i) / sp_i * 100 > 10.0:
                return None

        # ADX(11) > 25
        adx_s   = _calc_adx(df, period=11)
        adx_now = float(adx_s.iloc[-1])
        if np.isnan(adx_now) or adx_now <= 25.0:
            return None

        # ── 조건34 체크 (일봉 과열스코어) ────────────────────────────
        prev_c = close_s.shift(1)
        tr_d   = pd.concat([hi_d - lo_d,
                             (hi_d - prev_c).abs(),
                             (lo_d - prev_c).abs()], axis=1).max(axis=1)

        atr5    = tr_d.rolling(5).mean()
        atr20   = tr_d.rolling(20).mean()
        mav5    = vo_d.rolling(5).mean()
        mav20   = vo_d.rolling(20).mean()
        sma20   = close_s.rolling(20).mean()
        disp20  = close_s / sma20 * 100.0
        score_d = (atr5 / atr20) * (mav5 / mav20) * (disp20 / 100.0)

        cur_score = float(score_d.iloc[-1])
        if np.isnan(cur_score) or cur_score < 3.0:
            return None

        # ── 결과 수집 ─────────────────────────────────────────────────
        today_close = float(close_s.iloc[-1])
        prev_close  = float(close_s.iloc[-2])
        pct_chg     = round((today_close / prev_close - 1) * 100, 2) if prev_close > 0 else 0.0
        sp_now      = float(sp.iloc[-1])
        gap_pct     = round((today_close - sp_now) / sp_now * 100, 2)

        return {
            "종목코드":    code,
            "종가":        int(today_close),
            "전일대비(%)": pct_chg,
            "세력20평단":  int(round(sp_now, 0)),
            "평단대비(%)": gap_pct,
            "ADX(11)":     round(adx_now, 1),
            "과열스코어":  round(cur_score, 2),
            "이격도(20)":  round(float(disp20.iloc[-1]), 1),
        }
    except Exception:
        return None


def run_screen36(date_str, prog):
    t_start = datetime.now()
    prog.update({"current": 0, "total": 0, "status": "loading"})
    end     = pd.Timestamp(date_str)
    start   = end - pd.Timedelta(days=LOOKBACK)
    listing = _get_listing_with_progress(prog)
    valid   = listing[
        (listing["Marcap"] >= MIN_CAP) &
        (~listing["Market"].isin(["ETF", "ETN"]))
    ].copy()
    prog["total"]  = len(valid)
    prog["status"] = "running"
    rows    = _run_screen_parallel(valid, _screen36_ticker, start, end, prog)
    t_end   = datetime.now()
    elapsed = int((t_end - t_start).total_seconds())
    prog["status"] = "done"
    if not rows:
        return pd.DataFrame()
    df = (pd.DataFrame(rows)
          [["시장", "종목코드", "종목명", "시총(억)", "종가", "전일대비(%)",
            "세력20평단", "평단대비(%)", "ADX(11)", "과열스코어", "이격도(20)"]]
          .sort_values("과열스코어", ascending=False)
          .reset_index(drop=True))
    df["검색시각"] = t_end.strftime("%Y-%m-%d %H:%M:%S")
    df["소요(초)"] = elapsed
    return df


def _screen37_ticker(code, start, end):
    """조건37 거래대금RSI: 거래대금 계산 + RSI 상단선/중심선 상향 돌파 동시 체크"""
    try:
        df = _fdr_safe(code, start, end)
        if df is None or len(df) < 3:
            return None
        df = df.copy()

        # ── 거래대금 계산 (조건35와 동일) ────────────────────────────
        last = df.iloc[-1]
        O = float(last["Open"]); H = float(last["High"])
        L = float(last["Low"]);  C = float(last["Close"]); V = float(last["Volume"])
        if C <= 0 or V <= 0:
            return None
        typical_today = (O + H + L + C) / 4.0
        tv_today      = V * typical_today / 100_000_000   # 억

        prev = df.iloc[-2]
        C2   = float(prev["Close"])
        pct_chg = round((C / C2 - 1) * 100, 2) if C2 > 0 else 0.0

        # ── RSI 밴드 계산 (조건31과 동일, 200봉 이상 필요) ───────────
        bh0_val = 0; bm0_val = 0; sig = ""
        if len(df) >= 200:
            close  = df["Close"].astype(float)
            _, bh, bm, _, _ = _calc_rsi_bands(
                close, _S31_LEN_RSI, _S31_UPPER_PCT, _S31_LOWER_PCT)
            c0  = float(close.iloc[-1]); c1 = float(close.iloc[-2])
            bh0 = float(bh.iloc[-1]);    bh1 = float(bh.iloc[-2])
            bm0 = float(bm.iloc[-1]);    bm1 = float(bm.iloc[-2])
            if not (np.isnan(bh0) or np.isnan(bm0)):
                bh0_val = int(round(bh0))
                bm0_val = int(round(bm0))
                cross_up_high = (c1 < bh1) and (c0 >= bh0)
                cross_up_mid  = (c1 < bm1) and (c0 >= bm0)
                sigs = []
                if cross_up_high: sigs.append("상단돌파↑")
                if cross_up_mid:  sigs.append("중심선돌파↑")
                sig = ", ".join(sigs)

        return {
            "종목코드":    code,
            "종가":        int(C),
            "전일대비(%)": pct_chg,
            "거래대금(억)": round(tv_today, 1),
            "RSI밴드상단": bh0_val,
            "RSI중심선":   bm0_val,
            "신호(RSI)":  sig,
        }
    except Exception:
        return None


def run_screen37(date_str, prog):
    t_start = datetime.now()
    prog.update({"current": 0, "total": 0, "status": "loading"})
    end     = pd.Timestamp(date_str)
    start   = end - pd.Timedelta(days=LOOKBACK)
    listing = _get_listing_with_progress(prog)
    valid   = listing[
        (listing["Marcap"] >= MIN_CAP_35) &
        (listing["Marcap"] <  MAX_CAP_35) &
        (~listing["Market"].isin(["ETF", "ETN"])) &
        (~listing["Name"].str.match(_ETF_NAME_RE, na=False))
    ].copy()
    prog["total"]  = len(valid)
    prog["status"] = "running"
    rows    = _run_screen_parallel(valid, _screen37_ticker, start, end, prog)
    t_end   = datetime.now()
    elapsed = int((t_end - t_start).total_seconds())
    prog["status"] = "done"
    if not rows:
        return pd.DataFrame()

    df_pool = pd.DataFrame(rows)
    for col in ["RSI밴드상단", "RSI중심선", "신호(RSI)"]:
        if col not in df_pool.columns:
            df_pool[col] = ""
        else:
            df_pool[col] = df_pool[col].fillna("")

    # 거래대금 TOP 100 필터
    top100_codes = set(df_pool.nlargest(100, "거래대금(억)")["종목코드"].tolist())
    df_top = df_pool[df_pool["종목코드"].isin(top100_codes)].copy()

    # RSI 신호 있는 종목만 (상단돌파↑ 또는 중심선돌파↑)
    df_final = df_top[df_top["신호(RSI)"] != ""].copy()
    if df_final.empty:
        return pd.DataFrame()

    out_cols = ["시장", "종목코드", "종목명", "시총(억)", "종가", "전일대비(%)",
                "거래대금(억)", "RSI밴드상단", "RSI중심선", "신호(RSI)"]
    df = (df_final.sort_values("거래대금(억)", ascending=False)[out_cols]
          .reset_index(drop=True))
    df["검색시각"] = t_end.strftime("%Y-%m-%d %H:%M:%S")
    df["소요(초)"] = elapsed
    return df


# ══════════════════════════════════════════════════════════════════════════════
# 조건38: 패턴검색 — 이미지 업로드 → 다중지표 패턴 유사도 검색
# ══════════════════════════════════════════════════════════════════════════════

_p38_image_b64   = ""    # 업로드 이미지 base64 (미리보기용)
_p38_ref_pattern = []    # 이미지에서 추출한 90포인트 정규화 패턴


def _normalize_s38(arr):
    """0~1 정규화. NaN → 0.5. 실패시 None."""
    a = np.array(arr, dtype=float)
    valid = a[~np.isnan(a)]
    if len(valid) == 0:
        return None
    mn, mx = valid.min(), valid.max()
    if mx - mn < 1e-8:
        return np.full(len(a), 0.5)
    result = (a - mn) / (mx - mn)
    result[np.isnan(a)] = 0.5
    return result


def _pearson_s38(a, b):
    """Pearson 상관계수 → 0~100 유사도. 음수는 0으로 클리핑."""
    a = np.array(a, dtype=float)
    b = np.array(b, dtype=float)
    n = min(len(a), len(b))
    if n < 10:
        return 0.0
    a, b = a[:n], b[:n]
    if a.std() < 1e-8 or b.std() < 1e-8:
        return 0.0
    r = np.corrcoef(a, b)[0, 1]
    return max(0.0, float(r) if not np.isnan(r) else 0.0) * 100.0


def _extract_chart_pattern_38(image_bytes, n_points=90):
    """
    차트 이미지 → 정규화 90포인트 패턴.
    상하 각 10%, 좌우 각 5% 크롭 후 배경과의 차이를 이용해 가격선 추출.
    다크/라이트 테마 공통 지원.
    """
    if not _PIL_AVAILABLE:
        return None
    try:
        img = _PIL_Image.open(io.BytesIO(image_bytes)).convert('RGB')
        w, h = img.size
        # 차트 영역 크롭 (헤더·푸터·축 제외)
        l, r = int(w * 0.05), int(w * 0.95)
        t, b = int(h * 0.08), int(h * 0.88)
        img  = img.crop((l, t, r, b))
        arr  = np.array(img.convert('L'), dtype=float)
        ch, cw = arr.shape

        # 배경 밝기: 네 코너 중앙값
        corners       = [arr[0, 0], arr[0, -1], arr[-1, 0], arr[-1, -1]]
        bg_brightness = float(np.median(corners))

        pattern  = np.zeros(n_points)
        col_w    = cw / n_points

        for i in range(n_points):
            x0 = int(i * col_w)
            x1 = min(int((i + 1) * col_w), cw - 1)
            if x0 >= x1:
                x1 = x0 + 1
            col_slice = arr[:, x0:x1]
            diff      = np.abs(col_slice - bg_brightness).max(axis=1)
            thr       = np.percentile(diff, 75)     # 상위 25% 픽셀 = 차트 요소
            rows      = np.where(diff >= thr)[0]
            if len(rows) > 3:
                # 고가(rows.min=화면 상단) + 저가(rows.max) 중간 → 종가 근사
                pattern[i] = (rows.min() + rows.max()) / 2.0
            else:
                pattern[i] = ch * 0.5

        # y축 반전(위=높은가격) + 0~1 정규화 + 3포인트 스무딩
        pattern = ch - pattern
        mn, mx  = pattern.min(), pattern.max()
        if mx - mn > 1e-6:
            pattern = (pattern - mn) / (mx - mn)
        else:
            pattern[:] = 0.5
        # 스무딩
        sm = np.convolve(pattern, np.ones(3) / 3, mode='same')
        sm[0] = pattern[0]; sm[-1] = pattern[-1]
        return sm.tolist()
    except Exception as e:
        print(f"[PATTERN38] 이미지 추출 오류: {e}")
        return None


def _calc_vl_s38(close_s):
    """VL = LinReg(C,50)*2 − LinReg(LinReg(C,50),50)"""
    A  = linreg(close_s, 50)
    A1 = linreg(A, 50)
    return A + (A - A1)


def _calc_srsi_s38(close_s, rsi_p=14, trade_sl=7):
    """TDI S_RSI = SMA(RSI(rsi_p), trade_sl)"""
    delta = close_s.diff()
    gain  = delta.clip(lower=0)
    loss  = (-delta).clip(lower=0)
    ag    = gain.ewm(span=rsi_p, adjust=False).mean()
    al    = loss.ewm(span=rsi_p, adjust=False).mean()
    rs    = ag / (al + 1e-10)
    rsi   = 100.0 - 100.0 / (1.0 + rs)
    return rsi.rolling(trade_sl).mean()


def _screen38_ticker(code, start, end, ref_pattern):
    """조건38: 다중지표 패턴 유사도 계산 후 dict 반환
    ※ 비교 기준: '검색시점(end)' 이전 최근 90봉 — 과거 특정 시점 패턴 검색이 아님
    """
    try:
        df = _fdr_safe(code, start, end)
        if df is None or len(df) < 110:
            return None

        # ── 검색시점(end) 기준 엄격 필터 ── FDR이 end 이후 데이터를 포함할 경우 제거
        end_ts = pd.Timestamp(end)
        df = df[df.index <= end_ts]

        close = df["Close"].astype(float)
        N     = 90
        if len(close) < N:
            return None

        # 검색시점 기준 최근 N봉 (= 지금과 가장 유사한 최신 패턴 비교)
        c90 = close.iloc[-N:].values

        # ① 종가 정규화
        c_n = _normalize_s38(c90)
        if c_n is None:
            return None

        # ② VL 정규화
        vl    = _calc_vl_s38(close)
        vl_n  = _normalize_s38(vl.iloc[-N:].values)

        # ③ 세력평단 정규화
        sp    = _calc_세력평단(df)
        sp_n  = _normalize_s38(sp.iloc[-N:].values)

        # ④ TDI S_RSI 정규화
        srsi  = _calc_srsi_s38(close)
        sr_n  = _normalize_s38(srsi.iloc[-N:].values)

        # ⑤ EMA9 / EMA20 이격도 정규화
        e9    = close.ewm(span=9,   adjust=False).mean().iloc[-N:].values
        e20   = close.ewm(span=20,  adjust=False).mean().iloc[-N:].values
        e200  = close.ewm(span=200, adjust=False).mean().iloc[-N:].values
        e9_n  = _normalize_s38(e9  / (c90 + 1e-8))
        e20_n = _normalize_s38(e20 / (c90 + 1e-8))

        # 가중 유사도:  종가 70%  VL 12.5%  세력평단 5%  S_RSI 5%  EMA9 4%  EMA20 3.5%
        ref = ref_pattern
        def safe_sim(n):
            return _pearson_s38(ref, n) if n is not None else _pearson_s38(ref, c_n)

        total = (
            _pearson_s38(ref, c_n)  * 0.700 +
            safe_sim(vl_n)          * 0.125 +
            safe_sim(sp_n)          * 0.050 +
            safe_sim(sr_n)          * 0.050 +
            safe_sim(e9_n)          * 0.040 +
            safe_sim(e20_n)         * 0.035
        )

        today_c = float(close.iloc[-1])
        prev_c  = float(close.iloc[-2])
        pct     = round((today_c / prev_c - 1) * 100, 2) if prev_c > 0 else 0.0

        # EMA200 방향 (참고용)
        # EMA200 상승 필터 (필수 조건)
        if e200[-1] <= e200[-5]:
            return None
        e200_dir = "↑"

        return {
            "종목코드":    code,
            "종가":        int(today_c),
            "전일대비(%)": pct,
            "유사율(%)":   round(total, 1),
            "VL방향":      "↑" if float(vl.iloc[-1]) > float(vl.iloc[-5]) else "↓",
            "EMA200":      e200_dir,
        }
    except Exception:
        return None


def run_screen38(date_str, prog):
    global _p38_ref_pattern
    t_start = datetime.now()

    if not _p38_ref_pattern:
        prog.update({"current": 0, "total": 0, "status": "done"})
        return pd.DataFrame()

    prog.update({"current": 0, "total": 0, "status": "loading"})
    end     = pd.Timestamp(date_str)
    start   = end - pd.Timedelta(days=LOOKBACK)
    listing = _get_listing_with_progress(prog)
    valid   = listing[
        (listing["Marcap"] >= MIN_CAP) &
        (~listing["Market"].isin(["ETF", "ETN"])) &
        (~listing["Name"].str.match(_ETF_NAME_RE, na=False))
    ].copy()

    prog["total"]  = len(valid)
    prog["status"] = "running"

    ref  = list(_p38_ref_pattern)   # 로컬 복사
    rows = []
    lock = threading.Lock()
    cnt  = [0]

    def _job(row):
        code = str(row["Code"]).zfill(6)
        res  = _screen38_ticker(code, start, end, ref)
        with lock:
            cnt[0] += 1
            prog["current"] = cnt[0]
            if res is not None:
                res["종목명"]   = row["Name"]
                res["시총(억)"] = int(row["Marcap"]) // 100_000_000
                res["시장"]     = row["Market"]
                rows.append(res)

    with ThreadPoolExecutor(max_workers=_SCREEN_WORKERS) as ex:
        list(ex.map(_job, (row for _, row in valid.iterrows())))

    t_end   = datetime.now()
    elapsed = int((t_end - t_start).total_seconds())
    prog["status"] = "done"

    if not rows:
        return pd.DataFrame()

    out_cols = ["시장", "종목코드", "종목명", "시총(억)", "종가",
                "전일대비(%)", "유사율(%)", "VL방향", "EMA200"]
    df = (pd.DataFrame(rows)
          .nlargest(5, "유사율(%)")
          [out_cols]
          .reset_index(drop=True))
    df["검색시각"] = t_end.strftime("%Y-%m-%d %H:%M:%S")
    df["소요(초)"] = elapsed
    return df


# ══════════════════════════════════════════════════════════════════════════════
# 조건39: VL이격시작 — 종가 ≥ VL × 1.5 (VL 대비 50% 이상 이격)
# ══════════════════════════════════════════════════════════════════════════════

def _screen39_ticker(code, start, end):
    """조건39: VL 대비 30% 이상 이격 · VL 상승 · 60일 신고가 아닌 종목 · 3일 연속 거래 없는 종목 제외"""
    try:
        df = _fdr_safe(code, start, end)
        if df is None or len(df) < 110:
            return None
        close = df["Close"].astype(float)
        if len(close) < 61:
            return None

        # VL = LinReg(C,50)*2 − LinReg(LinReg(C,50),50)  (조건38 공용 함수 재사용)
        vl   = _calc_vl_s38(close)
        vl_v = float(vl.iloc[-1])
        c_v  = float(close.iloc[-1])

        # VL이 음수(또는 0)이면 이격도 계산 불가 → 제외
        if vl_v <= 0:
            return None

        # 조건1: VL 대비 30% 이상 이격
        gap = c_v / vl_v - 1.0   # 0.3 = 30%
        if gap < 0.30:
            return None

        # 조건2: 금일 종가가 최근 60일 최고 종가가 아닌 종목
        high60 = float(close.iloc[-60:].max())
        if c_v >= high60:   # 금일이 60일 최고가이면 제외
            return None

        # 조건3: VL 상승 — 전일 VL < 금일 VL
        if len(vl.dropna()) < 2 or float(vl.iloc[-2]) >= vl_v:
            return None

        # 조건4: 금일 포함 3거래일 연속 거래량 0인 종목 제외
        vol = df["Volume"].astype(float)
        if len(vol) >= 3 and vol.iloc[-1] == 0 and vol.iloc[-2] == 0 and vol.iloc[-3] == 0:
            return None

        prev_c = float(close.iloc[-2]) if len(close) >= 2 else c_v
        pct    = round((c_v / prev_c - 1) * 100, 2) if prev_c > 0 else 0.0

        return {
            "종목코드":      code,
            "종가":          int(c_v),
            "전일대비(%)":   pct,
            "VL값":          round(vl_v, 0),
            "VL이격(%)":     round(gap * 100, 1),
            "60일고가":      int(high60),
        }
    except Exception:
        return None


def run_screen39(date_str, prog):
    t_start = datetime.now()
    prog.update({"current": 0, "total": 0, "status": "loading"})
    end   = date_str
    start = (datetime.strptime(date_str, "%Y%m%d") - timedelta(days=LOOKBACK)).strftime("%Y%m%d")

    listing = _get_listing_with_progress(prog)
    valid   = listing[
        (listing["Marcap"] >= MIN_CAP) &
        (~listing["Market"].isin(["ETF", "ETN"])) &
        (~listing["Name"].str.match(_ETF_NAME_RE, na=False))
    ].copy()

    prog["total"]  = len(valid)
    prog["status"] = "running"
    rows = _run_screen_parallel(valid, _screen39_ticker, start, end, prog)

    t_end   = datetime.now()
    elapsed = int((t_end - t_start).total_seconds())
    prog["status"] = "done"

    if not rows:
        return pd.DataFrame()

    out_cols = ["시장", "종목코드", "종목명", "시총(억)", "종가", "전일대비(%)",
                "VL값", "VL이격(%)", "60일고가"]
    df = (pd.DataFrame(rows)
          .sort_values("VL이격(%)", ascending=False)
          [out_cols]
          .reset_index(drop=True))
    df["검색시각"] = t_end.strftime("%Y-%m-%d %H:%M:%S")
    df["소요(초)"] = elapsed
    return df


# ══════════════════════════════════════════════════════════════════════════════
# 조건40: 파워맵우수 — KIS API 코스닥 거래대금 TOP20 실시간 스캔
# ══════════════════════════════════════════════════════════════════════════════

def _kis40_fetch(top_n: int = 20) -> list[dict]:
    """KIS API → 코스닥 거래대금 상위 top_n 반환 (dict list)"""
    if _kis is None:
        return []
    raw = _kis.trade_value_ranking(date_str="", top_n=top_n, market="Q")
    rows = []
    for r in raw:
        price   = int(r.get("price", 0))
        prev_p  = price - int(r.get("day_chg", 0)) if r.get("day_chg") else price
        pct     = round(r.get("day_chg", 0.0), 2)
        tv_eok  = round(int(r.get("tr_value", 0)) / 100_000_000, 1)
        rows.append({
            "순위":         int(r.get("rank", len(rows)+1)),
            "종목코드":     str(r.get("code", "")).zfill(6),
            "종목명":       str(r.get("name", "")),
            "현재가":       price,
            "전일대비(%)":  pct,
            "거래대금(억)": tv_eok,
            "거래량":       int(r.get("volume", 0)),
            "변동":         "—",          # placeholder — 실시간 비교 시 덮어씀
        })
    return rows


def run_screen40(date_str, prog):
    """조건40: KIS API 코스닥 거래대금 TOP20 (date_str 무관, 항상 현재 데이터)"""
    t_start = datetime.now()
    prog.update({"current": 0, "total": 1, "status": "loading"})

    rows = _kis40_fetch(top_n=20)

    t_end   = datetime.now()
    elapsed = int((t_end - t_start).total_seconds())
    prog.update({"current": 1, "total": 1, "status": "done"})

    if not rows:
        return pd.DataFrame()

    out_cols = ["순위", "종목코드", "종목명", "현재가", "전일대비(%)",
                "거래대금(억)", "거래량", "변동"]
    df = pd.DataFrame(rows)[out_cols]
    df["검색시각"] = t_end.strftime("%Y-%m-%d %H:%M:%S")
    df["소요(초)"] = elapsed
    return df


def _run_realtime40():
    """조건40 코스닥 거래대금 TOP20 실시간 반복 스캔 루프 (10분 간격)"""
    global _rt40_scan_no, _rt40_scan_start, _rt40_last_scan, _rt40_next_scan, _rt40_scan_elapsed
    st = _state[40]
    prev_rows: list[dict] = []   # 이전 스캔 결과 (비교용)
    print("[REALTIME40] 실시간 스캔 시작")

    while st["realtime"]:
        _rt40_scan_no += 1
        t_start   = datetime.now()
        today_str = t_start.strftime("%Y%m%d")
        _rt40_scan_start = t_start.strftime("%H:%M:%S")
        print(f"[REALTIME40] #{_rt40_scan_no} 스캔 시작 ({today_str} {_rt40_scan_start})")

        try:
            st["progress"] = {"current": 0, "total": 1, "status": "loading"}
            cur_rows = _kis40_fetch(top_n=20)
            t_end = datetime.now()
            _rt40_last_scan    = t_end.strftime("%H:%M:%S")
            _rt40_scan_elapsed = int((t_end - t_start).total_seconds())
            st["progress"].update({"current": 1, "total": 1, "status": "done"})

            if cur_rows:
                prev_map = {r["종목코드"]: r["순위"] for r in prev_rows}
                cur_map  = {r["종목코드"]: r["순위"] for r in cur_rows}

                new_codes    = [r["종목코드"] for r in cur_rows if r["종목코드"] not in prev_map]
                exited_codes = [r["종목코드"] for r in prev_rows if r["종목코드"] not in cur_map]

                # 변동 annotation
                for r in cur_rows:
                    code = r["종목코드"]
                    if code not in prev_map:
                        r["변동"] = "NEW"
                    else:
                        delta = prev_map[code] - r["순위"]   # 양수 = 순위 상승
                        r["변동"] = (f"↑{delta}" if delta > 0
                                     else f"↓{abs(delta)}" if delta < 0 else "—")

                out_cols = ["순위", "종목코드", "종목명", "현재가", "전일대비(%)",
                            "거래대금(억)", "거래량", "변동"]
                df = pd.DataFrame(cur_rows)[out_cols]
                df["검색시각"] = _rt40_last_scan
                df["소요(초)"] = _rt40_scan_elapsed

                st["result_df"]    = df
                st["result_date"]  = today_str
                st["new_codes"]    = set(new_codes)
                st["known_codes"].update(cur_map.keys())

                # 변동 여부 판단 (신규진입·이탈·3위 이상 이동)
                has_change = bool(new_codes or exited_codes)
                if not has_change:
                    for r in cur_rows:
                        if r["종목코드"] in prev_map:
                            if abs(prev_map[r["종목코드"]] - r["순위"]) >= 3:
                                has_change = True
                                break

                # 이메일 발송 (첫 스캔 이후, 변동 있을 때)
                if prev_rows and has_change:
                    alert_time = t_end.strftime("%Y-%m-%d %H:%M:%S")
                    lines = [
                        f"[코스닥 거래대금 TOP20] #{_rt40_scan_no}회차 — {alert_time}", ""
                    ]

                    # 전체 순위표
                    lines.append("■ 현재 코스닥 거래대금 TOP20")
                    lines.append(f"{'순위':>4}  {'종목명':<14}  {'현재가':>8}  "
                                 f"{'거래대금(억)':>10}  {'전일대비':>8}  변동")
                    lines.append("─" * 62)
                    for r in cur_rows:
                        lines.append(
                            f"{r['순위']:>4}  {r['종목명']:<14}  {int(r['현재가']):>8,}  "
                            f"{r['거래대금(억)']:>10.1f}  {r['전일대비(%)']:>+7.2f}%  {r['변동']}"
                        )

                    # 신규 진입
                    if new_codes:
                        new_names = [r["종목명"] for r in cur_rows if r["종목코드"] in new_codes]
                        lines.append("")
                        lines.append(f"■ 신규 진입 ({len(new_codes)}종목): " +
                                     ", ".join(f"{nm}({cd})"
                                               for nm, cd in zip(new_names, new_codes)))

                    # TOP20 이탈
                    if exited_codes:
                        ex_names = [r["종목명"] for r in prev_rows if r["종목코드"] in exited_codes]
                        lines.append(f"■ TOP20 이탈 ({len(exited_codes)}종목): " +
                                     ", ".join(f"{nm}({cd})"
                                               for nm, cd in zip(ex_names, exited_codes)))

                    # 3위 이상 순위 변동
                    big = []
                    for r in cur_rows:
                        cd = r["종목코드"]
                        if cd in prev_map:
                            d = prev_map[cd] - r["순위"]
                            if abs(d) >= 3:
                                arr = "↑" if d > 0 else "↓"
                                big.append(f"  {r['종목명']}({cd}): "
                                           f"{prev_map[cd]}위 → {r['순위']}위  {arr}{abs(d)}")
                    if big:
                        lines.append("")
                        lines.append("■ 3위↑ 이상 변동:")
                        lines.extend(big)

                    lines += ["", f"스캔 #{_rt40_scan_no}회차 완료"]
                    _send_email_alert(
                        f"[코스닥TOP20] #{_rt40_scan_no}회차 "
                        f"진입{len(new_codes)} 이탈{len(exited_codes)} — {_rt40_last_scan}",
                        "\n".join(lines)
                    )
                    print(f"[REALTIME40] 변동 감지 → 이메일 발송 완료")

                prev_rows = cur_rows

            else:
                print(f"[REALTIME40] #{_rt40_scan_no} KIS API 결과 없음 (장 마감 또는 API 오류)")

        except Exception as e:
            print(f"[REALTIME40] 오류: {e}")

        next_dt = datetime.now() + timedelta(seconds=_RT40_INTERVAL_SEC)
        _rt40_next_scan = next_dt.strftime("%H:%M:%S")
        for _ in range(_RT40_INTERVAL_SEC):
            if not st["realtime"]:
                break
            time.sleep(1)

    st["progress"]["status"] = "idle"
    _rt40_next_scan = ""
    print("[REALTIME40] 실시간 스캔 종료")


# ══════════════════════════════════════════════════════════════════════════════
# 조건41: 파워맵최고 — 전일 TOP10 밖 · 금일 TOP20 진입 · 거래량비율≥200% · 양봉
# ══════════════════════════════════════════════════════════════════════════════

def _prev_trading_day(ref: datetime) -> str:
    """ref 기준 직전 영업일 (YYYYMMDD)"""
    d = ref - timedelta(days=1)
    while d.weekday() >= 5:   # 토(5)·일(6) 건너뜀
        d -= timedelta(days=1)
    return d.strftime("%Y%m%d")


def _market_time_ratio(ref: datetime) -> float:
    """장중 경과 비율: 9:00 기준 현재까지 / 전체(9:00~15:30)"""
    open_t  = ref.replace(hour=9,  minute=0, second=0, microsecond=0)
    close_t = ref.replace(hour=15, minute=30, second=0, microsecond=0)
    elapsed  = (ref - open_t).total_seconds()
    duration = (close_t - open_t).total_seconds()   # 23400 초 (6.5시간)
    return min(1.0, max(0.005, elapsed / duration))


def _run_screen41_core(prog=None) -> list[dict]:
    """
    조건41 핵심 로직:
      ① 전일 코스닥 거래대금 TOP10 조회
      ② 금일 코스닥 거래대금 TOP20 조회
      ③ ①에 없는 종목만 남기고
         - 양봉 (현재가 > 시가)
         - 전일동시간대 거래량비율 ≥ 200%
    """
    if _kis is None:
        return []

    now      = datetime.now()
    yest_str = _prev_trading_day(now)
    t_ratio  = _market_time_ratio(now)

    if prog:
        prog.update({"current": 0, "total": 3, "status": "loading"})

    # ① 전일 TOP10
    yest_raw = _kis.trade_value_ranking(date_str=yest_str, top_n=10, market="Q")
    yest_top10 = {str(r["code"]).zfill(6) for r in yest_raw}
    if prog:
        prog["current"] = 1

    # ② 금일 TOP20
    today_raw = _kis.trade_value_ranking(date_str="", top_n=20, market="Q")
    if prog:
        prog["current"] = 2

    results = []
    for r in today_raw:
        code = str(r["code"]).zfill(6)

        # 필터1: 전일 TOP10 제외
        if code in yest_top10:
            continue

        price    = r["price"]
        open_p   = r["open"]
        vol_t    = r["volume"]      # 금일 누적 거래량
        vol_p    = r["prev_vol"]    # 전일 총 거래량

        # 필터2: 양봉 (현재가 > 시가)
        if price <= open_p or open_p <= 0:
            continue

        # 필터3: 전일동시간대 거래량비율 ≥ 200%
        # 전일동시간대 추정량 = 전일총거래량 × 경과비율
        if vol_p <= 0:
            continue
        vol_ratio = vol_t / (vol_p * t_ratio)
        if vol_ratio < 2.0:
            continue

        pct    = round(r["day_chg"], 2)
        tv_eok = round(r["tr_value"] / 100_000_000, 1)
        results.append({
            "순위":          r["rank"],
            "종목코드":      code,
            "종목명":        r["name"],
            "현재가":        price,
            "전일대비(%)":   pct,
            "거래대금(억)":  tv_eok,
            "거래량비율(%)": round(vol_ratio * 100, 1),
            "변동":          "—",
        })

    if prog:
        prog["current"] = 3
    return results


def run_screen41(date_str, prog):
    """조건41: KIS API 코스닥 파워맵최고 (date_str 무관, 항상 현재 데이터)"""
    t_start = datetime.now()
    prog.update({"current": 0, "total": 3, "status": "loading"})

    rows = _run_screen41_core(prog)

    t_end   = datetime.now()
    elapsed = int((t_end - t_start).total_seconds())
    prog.update({"current": 3, "total": 3, "status": "done"})

    if not rows:
        return pd.DataFrame()

    out_cols = ["순위", "종목코드", "종목명", "현재가", "전일대비(%)",
                "거래대금(억)", "거래량비율(%)", "변동"]
    df = pd.DataFrame(rows)[out_cols]
    df["검색시각"] = t_end.strftime("%Y-%m-%d %H:%M:%S")
    df["소요(초)"] = elapsed
    return df


def _run_realtime41():
    """조건41 파워맵최고 실시간 반복 스캔 루프 (10분 간격)"""
    global _rt41_scan_no, _rt41_scan_start, _rt41_last_scan, _rt41_next_scan, _rt41_scan_elapsed
    st = _state[41]
    print("[REALTIME41] 실시간 스캔 시작")

    while st["realtime"]:
        _rt41_scan_no += 1
        t_start   = datetime.now()
        today_str = t_start.strftime("%Y%m%d")
        _rt41_scan_start = t_start.strftime("%H:%M:%S")
        print(f"[REALTIME41] #{_rt41_scan_no} 스캔 시작 ({today_str} {_rt41_scan_start})")

        try:
            st["progress"] = {"current": 0, "total": 3, "status": "loading"}
            cur_rows = _run_screen41_core(st["progress"])
            t_end = datetime.now()
            _rt41_last_scan    = t_end.strftime("%H:%M:%S")
            _rt41_scan_elapsed = int((t_end - t_start).total_seconds())
            st["progress"].update({"current": 3, "total": 3, "status": "done"})

            if cur_rows:
                cur_codes = {r["종목코드"] for r in cur_rows}
                new_codes = cur_codes - st["known_codes"]

                # 변동 annotation
                for r in cur_rows:
                    r["변동"] = "NEW" if r["종목코드"] in new_codes else "—"

                out_cols = ["순위", "종목코드", "종목명", "현재가", "전일대비(%)",
                            "거래대금(억)", "거래량비율(%)", "변동"]
                df = pd.DataFrame(cur_rows)[out_cols]
                df["검색시각"] = _rt41_last_scan
                df["소요(초)"] = _rt41_scan_elapsed

                st["result_df"]   = df
                st["result_date"] = today_str
                st["new_codes"]   = new_codes
                st["known_codes"].update(cur_codes)

                # 이메일: 신규 종목 있을 때
                if new_codes:
                    new_list  = [r for r in cur_rows if r["종목코드"] in new_codes]
                    alert_time = t_end.strftime("%Y-%m-%d %H:%M:%S")
                    lines = [
                        f"[코스닥 파워맵최고] #{_rt41_scan_no}회차 신규 {len(new_codes)}종목 — {alert_time}", ""
                    ]
                    lines.append("■ 신규 진입 종목")
                    lines.append(f"{'순위':>4}  {'종목명':<14}  {'현재가':>8}  "
                                 f"{'거래대금(억)':>10}  {'전일대비':>8}  거래량비율")
                    lines.append("─" * 65)
                    for r in new_list:
                        lines.append(
                            f"{r['순위']:>4}  {r['종목명']:<14}  {int(r['현재가']):>8,}  "
                            f"{r['거래대금(억)']:>10.1f}  {r['전일대비(%)']:>+7.2f}%  "
                            f"{r['거래량비율(%)']}%"
                        )
                    if len(cur_rows) > len(new_list):
                        lines.append("")
                        lines.append("■ 전체 조건 충족 종목")
                        for r in cur_rows:
                            if r["종목코드"] not in new_codes:
                                lines.append(
                                    f"  {r['순위']}위  {r['종목명']}({r['종목코드']})  "
                                    f"거래량비율 {r['거래량비율(%)']}%"
                                )
                    lines += ["", f"스캔 #{_rt41_scan_no}회차 완료"]
                    _send_email_alert(
                        f"[파워맵최고] #{_rt41_scan_no}회차 신규 {len(new_codes)}종목 — {_rt41_last_scan}",
                        "\n".join(lines)
                    )
                    print(f"[REALTIME41] 신규 {len(new_codes)}개 → 이메일 발송")
                else:
                    print(f"[REALTIME41] #{_rt41_scan_no} 충족 {len(cur_rows)}종목, 신규 없음")

            else:
                st["result_df"]   = pd.DataFrame()
                st["result_date"] = today_str
                st["new_codes"]   = set()
                st["progress"]["status"] = "done"
                print(f"[REALTIME41] #{_rt41_scan_no} 조건 충족 종목 없음")

        except Exception as e:
            print(f"[REALTIME41] 오류: {e}")
            st["progress"]["status"] = "done"

        next_dt = datetime.now() + timedelta(seconds=_RT41_INTERVAL_SEC)
        _rt41_next_scan = next_dt.strftime("%H:%M:%S")
        for _ in range(_RT41_INTERVAL_SEC):
            if not st["realtime"]:
                break
            time.sleep(1)

    st["progress"]["status"] = "idle"
    _rt41_next_scan = ""
    print("[REALTIME41] 실시간 스캔 종료")


# ══════════════════════════════════════════════════════════════════════════════
# 조건42: 파워수급분석 — KIS API 코스닥 TOP20 업종·이유·연관종목 분석
# ══════════════════════════════════════════════════════════════════════════════

def _analyze_42_reasons(row: dict, detail: dict) -> tuple:
    """
    상승이유 / 하락이유 규칙 기반 분석
    row   : trade_value_ranking 결과 (rank, code, name, price, open, volume, prev_vol, day_chg, tr_value)
    detail: get_price_detail 결과 (업종명, w52_high, w52_low, per, pbr, 시총억, ...)
    반환  : (reasons_up: list[str], reasons_dn: list[str])
    """
    reasons_up: list[str] = []
    reasons_dn: list[str] = []

    pct   = float(row.get("day_chg",  0) or 0)
    price = int(row.get("price", 0) or 0)
    open_ = int(row.get("open",  price) or price)
    vol   = int(row.get("volume",   0) or 0)
    pvol  = int(row.get("prev_vol", 0) or 0)

    w52h  = int(detail.get("w52_high", 0) or 0)
    w52l  = int(detail.get("w52_low",  0) or 0)
    per   = float(detail.get("per", 0) or 0)
    pbr   = float(detail.get("pbr", 0) or 0)
    frgn  = int(detail.get("외국인순매수", 0) or 0)

    # ── 거래량 분석 ─────────────────────────────────────────────────────────
    if pvol > 0:
        vr = vol / pvol
        if vr >= 5.0:
            reasons_up.append(f"거래량 {vr:.1f}배 폭증 🚀")
        elif vr >= 2.0:
            reasons_up.append(f"거래량 {vr:.1f}배 증가")
        elif vr >= 1.3:
            reasons_up.append(f"거래량 {vr:.1f}배 소폭 증가")
        elif vr < 0.5:
            reasons_dn.append(f"거래량 급감 (전일 {vr:.1f}배)")

    # ── 등락률 ──────────────────────────────────────────────────────────────
    if pct >= 15:
        reasons_up.append(f"상한가 근접 ({pct:.1f}%↑) 🔥")
    elif pct >= 7:
        reasons_up.append(f"강세 급등 ({pct:.1f}%↑)")
    elif pct >= 3:
        reasons_up.append(f"상승세 ({pct:.1f}%↑)")
    elif pct >= 0.5:
        reasons_up.append(f"소폭 상승 ({pct:.1f}%↑)")
    elif pct <= -15:
        reasons_dn.append(f"하한가 근접 ({pct:.1f}%↓) 💥")
    elif pct <= -7:
        reasons_dn.append(f"급락 ({pct:.1f}%↓)")
    elif pct <= -3:
        reasons_dn.append(f"하락세 ({pct:.1f}%↓)")

    # ── 양봉/음봉 ────────────────────────────────────────────────────────────
    if price > 0 and open_ > 0:
        body_pct = (price - open_) / open_ * 100
        if body_pct >= 3:
            reasons_up.append(f"강한 양봉 (+{body_pct:.1f}%)")
        elif body_pct > 0:
            reasons_up.append("양봉 마감 (시가 대비 상승)")
        elif body_pct <= -3:
            reasons_dn.append(f"강한 음봉 ({body_pct:.1f}%)")
        else:
            reasons_dn.append("음봉 (시가 대비 하락)")

    # ── 52주 가격 위치 ───────────────────────────────────────────────────────
    if w52h > 0 and price >= w52h * 0.95:
        reasons_up.append("52주 신고가 근접 (상단 5% 이내)")
    elif w52h > 0 and price >= w52h * 0.80:
        reasons_up.append(f"52주 고가 대비 {price/w52h*100:.0f}% 수준")
    if w52l > 0 and price <= w52l * 1.10:
        reasons_dn.append("52주 신저가 근접 ⚠")

    # ── 밸류에이션 ───────────────────────────────────────────────────────────
    if 0 < per < 10:
        reasons_up.append(f"저PER 저평가 ({per:.1f}배)")
    elif per > 60:
        reasons_dn.append(f"고PER 고평가 ({per:.1f}배)")
    if 0 < pbr < 0.8:
        reasons_up.append(f"저PBR (순자산 대비 저평가 {pbr:.2f}배)")

    # ── 외국인 수급 ──────────────────────────────────────────────────────────
    if frgn > 0:
        reasons_up.append(f"외국인 순매수")
    elif frgn < 0:
        reasons_dn.append(f"외국인 순매도")

    # ── 거래대금 집중 ────────────────────────────────────────────────────────
    reasons_up.append("코스닥 거래대금 상위권 수급 집중")

    # ── 기본 하락이유 ────────────────────────────────────────────────────────
    if not reasons_dn:
        if pct < 0:
            reasons_dn.append("차익실현 매물 출회")
        else:
            reasons_dn.append("추가 상승 시 단기 저항 가능")

    return reasons_up[:5], reasons_dn[:4]


# fdr StockListing 캐시 (당일 1회만 조회)
_kosdaq_listing_cache: dict = {}   # code → {name, sector, industry}
_kosdaq_listing_date:  str  = ""


def _get_kosdaq_listing() -> dict:
    """fdr.StockListing("KOSDAQ") 캐시 반환 (당일 1회)"""
    global _kosdaq_listing_cache, _kosdaq_listing_date
    today = datetime.now().strftime("%Y%m%d")
    if _kosdaq_listing_cache and _kosdaq_listing_date == today:
        return _kosdaq_listing_cache
    try:
        df = fdr.StockListing("KOSDAQ")
        if df is None or df.empty:
            return {}
        # 컬럼명 정규화 (대소문자 혼용 가능)
        df.columns = [c.strip() for c in df.columns]
        col_code    = next((c for c in df.columns if c.lower() in ("code", "symbol")), None)
        col_name    = next((c for c in df.columns if c.lower() == "name"), None)
        col_sector  = next((c for c in df.columns if c.lower() == "sector"), None)
        col_industry= next((c for c in df.columns if c.lower() == "industry"), None)
        if not col_code:
            return {}
        result = {}
        for _, row in df.iterrows():
            code = str(row[col_code]).strip().zfill(6)
            result[code] = {
                "name":     str(row[col_name])     if col_name     else "",
                "sector":   str(row[col_sector])   if col_sector   else "",
                "industry": str(row[col_industry]) if col_industry else "",
            }
        _kosdaq_listing_cache = result
        _kosdaq_listing_date  = today
        print(f"[C42] KOSDAQ 종목 리스트 갱신: {len(result)}종목")
        return result
    except Exception as e:
        print(f"[C42] StockListing 오류: {e}")
        return {}


def _run_screen42_core(prog=None) -> list[dict]:
    """조건42 파워수급분석 핵심 로직"""

    # Step 1: 코스닥 거래대금 TOP20
    if prog: prog.update({"current": 1, "total": 5, "status": "loading"})
    top20 = _kis.trade_value_ranking(date_str="", top_n=20, market="Q") if _kis else []
    if not top20:
        print("[C42] KIS API 데이터 없음")
        return []
    top20_codes = {str(r["code"]).zfill(6) for r in top20}

    # Step 2: 각 종목 상세 정보 (업종명, 52주 고저, PER 등) 병렬 조회
    if prog: prog.update({"current": 2, "total": 5, "status": "running"})
    details: dict[str, dict] = {}

    def _fetch_one(row):
        code = str(row["code"]).zfill(6)
        det  = _kis.get_price_detail(code) if _kis else None
        return code, det or {}

    with ThreadPoolExecutor(max_workers=10) as ex:
        futs = {ex.submit(_fetch_one, r): r for r in top20}
        for fut in as_completed(futs):
            code, det = fut.result()
            details[code] = det

    # Step 3: KOSDAQ 전체 종목 리스트 (연관종목 탐색용)
    if prog: prog.update({"current": 3, "total": 5, "status": "running"})
    listing = _get_kosdaq_listing()   # code → {name, sector, industry}

    # Step 4: 업종 맵 구성 (KIS 업종명 우선, 없으면 fdr sector)
    if prog: prog.update({"current": 4, "total": 5, "status": "running"})
    sector_of: dict[str, str] = {}
    for code, det in details.items():
        kis_sector = det.get("업종명", "").strip()
        fdr_sector = listing.get(code, {}).get("sector", "").strip()
        fdr_indust = listing.get(code, {}).get("industry", "").strip()
        sector_of[code] = kis_sector or fdr_sector or fdr_indust or "기타"

    # Step 5: 연관종목 탐색 (같은 업종 KOSDAQ 종목 중 TOP20 제외 최대 3개)
    if prog: prog.update({"current": 5, "total": 5, "status": "done"})
    related_of: dict[str, list] = {}
    for code in top20_codes:
        my_sector = sector_of.get(code, "")
        if not my_sector or my_sector in ("기타", "—"):
            related_of[code] = []
            continue
        bucket = []
        for c2, info in listing.items():
            if c2 in top20_codes:
                continue
            c2_sector = info.get("sector", "").strip() or info.get("industry", "").strip()
            if c2_sector == my_sector:
                bucket.append({"code": c2, "name": info.get("name", c2)})
            if len(bucket) >= 3:
                break
        related_of[code] = bucket

    # Step 6: 결과 조합
    now_ts = datetime.now().strftime("%H:%M:%S")
    result = []
    for row in top20:
        code   = str(row["code"]).zfill(6)
        det    = details.get(code, {})
        ru, rd = _analyze_42_reasons(row, det)
        result.append({
            "순위":         row["rank"],
            "종목코드":     code,
            "종목명":       row["name"],
            "업종":         sector_of.get(code, "—"),
            "현재가":       row["price"],
            "전일대비(%)":  round(float(row.get("day_chg", 0) or 0), 2),
            "거래대금(억)": round(row["tr_value"] / 100_000_000, 1),
            "거래량":       row["volume"],
            "52주고":       det.get("w52_high", 0),
            "52주저":       det.get("w52_low",  0),
            "PER":          det.get("per", 0),
            "PBR":          det.get("pbr", 0),
            "시총(억)":     round(det.get("시총억", 0), 0),
            "상승이유":     ru,    # list[str]
            "하락이유":     rd,    # list[str]
            "연관종목":     related_of.get(code, []),  # list[{code, name}]
            "검색시각":     now_ts,
        })
    return result


def run_screen42(date_str, prog):
    """조건42: KIS API 코스닥 파워수급분석 (date_str 무관, 항상 현재 데이터)"""
    t_start = datetime.now()
    prog.update({"current": 0, "total": 5, "status": "loading"})

    rows = _run_screen42_core(prog)

    t_end = datetime.now()
    prog.update({"current": 5, "total": 5, "status": "done"})

    if not rows:
        return pd.DataFrame()

    # DataFrame 직렬화: list 컬럼은 JSON 문자열로 저장
    import json as _json
    df = pd.DataFrame(rows)
    df["상승이유"]  = df["상승이유"].apply(_json.dumps)
    df["하락이유"]  = df["하락이유"].apply(_json.dumps)
    df["연관종목"]  = df["연관종목"].apply(_json.dumps)

    elapsed = (t_end - t_start).total_seconds()
    print(f"[C42] 분석 완료: {len(rows)}종목  소요 {elapsed:.1f}초")
    return df


def _run_realtime42():
    """조건42 파워수급분석 실시간 반복 스캔 루프 (10분 간격)"""
    global _rt42_scan_no, _rt42_scan_start, _rt42_last_scan, _rt42_next_scan, _rt42_scan_elapsed
    st = _state[42]
    print("[REALTIME42] 실시간 스캔 시작")

    while st["realtime"]:
        _rt42_scan_no   += 1
        t_start          = datetime.now()
        _rt42_scan_start = t_start.strftime("%H:%M:%S")
        print(f"[REALTIME42] #{_rt42_scan_no} 스캔 시작 ({_rt42_scan_start})")

        try:
            st["progress"] = {"current": 0, "total": 5, "status": "loading"}
            cur_rows = _run_screen42_core(st["progress"])
            t_end            = datetime.now()
            _rt42_last_scan    = t_end.strftime("%H:%M:%S")
            _rt42_scan_elapsed = int((t_end - t_start).total_seconds())
            st["progress"].update({"current": 5, "total": 5, "status": "done"})

            if cur_rows:
                import json as _json
                df = pd.DataFrame(cur_rows)
                df["상승이유"] = df["상승이유"].apply(_json.dumps)
                df["하락이유"] = df["하락이유"].apply(_json.dumps)
                df["연관종목"] = df["연관종목"].apply(_json.dumps)
                df["검색시각"] = _rt42_last_scan
                df["소요(초)"] = _rt42_scan_elapsed
                st["result_df"] = df

                # 이전 대비 신규 종목 감지
                prev_codes = st.get("known_codes", set())
                cur_codes  = {str(r["종목코드"]) for r in cur_rows}
                new_codes  = cur_codes - prev_codes
                st["new_codes"]   = new_codes
                st["known_codes"] = cur_codes

                # 이메일: 신규 종목 감지 시
                if new_codes and prev_codes:
                    alert_time = t_end.strftime("%Y-%m-%d %H:%M:%S")
                    lines = [
                        f"[코스닥 파워수급분석] #{_rt42_scan_no}회차 신규 {len(new_codes)}종목 — {alert_time}", ""
                    ]
                    lines.append("■ 신규 진입 종목")
                    for r in cur_rows:
                        if str(r["종목코드"]) in new_codes:
                            ru = r["상승이유"]
                            if isinstance(ru, list):
                                ru_str = " / ".join(ru)
                            else:
                                ru_str = str(ru)
                            lines.append(
                                f"  [{r['순위']}위] {r['종목명']} ({r['종목코드']}) "
                                f"| 업종: {r['업종']} "
                                f"| {r['전일대비(%)']}% "
                                f"| 거래대금 {r['거래대금(억)']}억 "
                                f"| 상승이유: {ru_str}"
                            )
                    lines += ["", f"스캔 #{_rt42_scan_no}회차 완료"]
                    _send_email_alert(
                        f"[파워수급분석] #{_rt42_scan_no}회차 신규 {len(new_codes)}종목 — {_rt42_last_scan}",
                        "\n".join(lines)
                    )
                    print(f"[REALTIME42] 신규 {len(new_codes)}개 → 이메일 발송")
                else:
                    print(f"[REALTIME42] #{_rt42_scan_no} {len(cur_rows)}종목 완료, 신규 없음")
            else:
                st["result_df"]  = pd.DataFrame()
                st["new_codes"]  = set()
                st["progress"]["status"] = "done"
                print(f"[REALTIME42] #{_rt42_scan_no} 결과 없음")

        except Exception as e:
            print(f"[REALTIME42] 오류: {e}")
            st["progress"]["status"] = "done"

        next_dt          = datetime.now() + timedelta(seconds=_RT42_INTERVAL_SEC)
        _rt42_next_scan  = next_dt.strftime("%H:%M:%S")
        for _ in range(_RT42_INTERVAL_SEC):
            if not st["realtime"]:
                break
            time.sleep(1)

    st["progress"]["status"] = "idle"
    _rt42_next_scan = ""
    print("[REALTIME42] 실시간 스캔 종료")


RUNNER = {1: run_screen1,  2: run_screen2,  3: run_screen3,
          4: run_screen4,  5: run_screen5,  6: run_screen6,
          7: run_screen7,  8: run_screen8,  9: run_screen9,
          10: run_screen10, 11: run_screen11, 12: run_screen12,
          13: run_screen13, 14: run_screen14, 15: run_screen15,
          16: run_screen16, 17: run_screen17, 18: run_screen18,
          19: run_screen19, 20: run_screen20, 21: run_screen21,
          22: run_screen22, 23: run_screen23, 24: run_screen24,
          25: run_screen25, 26: run_screen26, 27: run_screen27,
          28: run_screen28, 29: run_screen29, 30: run_screen30,
          31: run_screen31, 32: run_screen32, 33: run_screen33,
          34: run_screen34, 35: run_screen35, 36: run_screen36,
          37: run_screen37, 38: run_screen38, 39: run_screen39,
          40: run_screen40, 41: run_screen41, 42: run_screen42,
          43: run_screen43, 44: run_screen44, 45: run_screen45,
          46: run_screen46, 47: run_screen47, 48: run_screen48,
          49: run_screen49, 50: run_screen50, 51: run_screen51}


# ══════════════════════════════════════════════════════════════════════════════
# 전역 상태 (스크리너별로 독립)
# ══════════════════════════════════════════════════════════════════════════════

_state = {
    sid: {"progress": {"current":0,"total":0,"status":"idle"},
          "result_df": pd.DataFrame(), "result_date": "", "worker": None,
          "realtime": False, "known_codes": set(), "new_codes": set()}
    for sid in SCREENERS
}


# ══════════════════════════════════════════════════════════════════════════════
# 공통 CSS / JS 조각
# ══════════════════════════════════════════════════════════════════════════════

BASE_CSS = """
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:'Segoe UI',sans-serif;background:#0f1117;color:#e0e0e0;min-height:100vh}
a{color:inherit;text-decoration:none}
.topbar{background:#1a1d27;padding:16px 32px;border-bottom:1px solid #2a2d3a;
        display:flex;align-items:center;gap:16px}
.topbar-logo{font-size:1.2rem;font-weight:800;color:#fff;letter-spacing:-.5px}
.topbar-sub{font-size:.8rem;color:#8b8fa8}
.topbar-back{margin-right:auto;background:#12151f;border:1px solid #2a2d3a;
             border-radius:8px;padding:6px 14px;font-size:.85rem;color:#8b8fa8;cursor:pointer}
.topbar-back:hover{color:#e0e0e0;border-color:#4f8ef7}
.container{max-width:1400px;margin:0 auto;padding:28px 32px}
.card{background:#1a1d27;border:1px solid #2a2d3a;border-radius:12px;
      padding:24px;margin-bottom:20px}
.btn{padding:10px 22px;border-radius:8px;border:none;cursor:pointer;
     font-size:.9rem;font-weight:600;transition:.15s}
.btn-primary{background:#4f8ef7;color:#fff}
.btn-primary:hover{background:#3a7de6}
.btn-primary:disabled{background:#1e2a4a;color:#445;cursor:not-allowed}
.btn-dl{background:#0f2a1e;color:#4ade80;border:1px solid #1a4a30}
.btn-dl:hover{background:#1a3a28}
.hidden{display:none!important}
@keyframes blink-alert{0%,100%{background:#2a1800}50%{background:#5a3500;box-shadow:0 0 10px #f59e0b55}}
.row-new td{animation:blink-alert 1.2s ease-in-out infinite}
.rt-badge{display:inline-flex;align-items:center;gap:6px;padding:5px 12px;border-radius:6px;
          font-size:.8rem;font-weight:700;background:#1a3a00;color:#4ade80;border:1px solid #2a5a10}
.rt-badge.inactive{background:#1e1e2a;color:#8b8fa8;border-color:#2a2d3a}
"""


# ══════════════════════════════════════════════════════════════════════════════
# 메인 허브 페이지
# ══════════════════════════════════════════════════════════════════════════════

HUB_HTML = """
<!doctype html><html lang="ko"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>주식 스크리너 허브</title>
<style>
""" + BASE_CSS + """
.hub-title{font-size:1.8rem;font-weight:800;color:#fff;margin-bottom:6px}
.hub-sub{font-size:.9rem;color:#8b8fa8;margin-bottom:32px}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(320px,1fr));gap:16px}
.sc-card{background:#1a1d27;border:1px solid #2a2d3a;border-radius:14px;
          padding:24px;cursor:pointer;transition:.2s;position:relative;overflow:hidden}
.sc-card:hover{border-color:#4f8ef7;transform:translateY(-2px);box-shadow:0 8px 24px #0005}
.sc-card.disabled{opacity:.4;cursor:not-allowed;pointer-events:none}
.sc-num{font-size:.75rem;font-weight:700;color:#8b8fa8;letter-spacing:1px;
        text-transform:uppercase;margin-bottom:10px}
.sc-icon{font-size:2rem;margin-bottom:10px}
.sc-title{font-size:1.1rem;font-weight:700;color:#fff;margin-bottom:8px}
.sc-desc{font-size:.8rem;color:#8b8fa8;line-height:1.6}
.sc-arrow{position:absolute;right:20px;top:50%;transform:translateY(-50%);
           font-size:1.3rem;color:#2a2d3a;transition:.2s}
.sc-card:hover .sc-arrow{color:#4f8ef7;right:16px}
.sc-badge{display:inline-block;padding:3px 10px;border-radius:99px;
           font-size:.72rem;font-weight:700;margin-top:12px}
.badge-live{background:#1e3a2a;color:#4ade80;border:1px solid #2a5a3a}
.badge-soon{background:#1e1e2a;color:#8b8fa8;border:1px solid #2a2a3a}
.divider{border:none;border-top:1px solid #2a2d3a;margin:28px 0}
</style></head><body>
<div class="topbar">
  <span class="topbar-logo">📊 주식 스크리너</span>
  <span class="topbar-sub">Korea Stock Screener Hub</span>
</div>
<div class="container">
  <div class="hub-title">스크리너 목록</div>
  <div class="hub-sub">조건을 선택해 종목을 검색하세요.</div>
  <div class="grid" id="grid"></div>
</div>
<script>
const screeners = SCREENER_LIST;

screeners.forEach(s => {
  const card = document.createElement('div');
  card.className = 'sc-card' + (s.live ? '' : ' disabled');
  card.innerHTML = `
    <div class="sc-num">조건 ${s.id}</div>
    <div class="sc-icon">${s.icon}</div>
    <div class="sc-title">${s.title}</div>
    <div class="sc-desc">${s.desc}</div>
    <div><span class="sc-badge ${s.live ? 'badge-live':'badge-soon'}">${s.live?'● 사용 가능':'준비 중'}</span></div>
    <div class="sc-arrow">→</div>`;
  if (s.live) card.onclick = () => location.href = '/screener/' + s.id;
  document.getElementById('grid').appendChild(card);
});
</script>
</body></html>
"""


# ══════════════════════════════════════════════════════════════════════════════
# 개별 스크리너 페이지
# ══════════════════════════════════════════════════════════════════════════════

SCREENER_HTML = """
<!doctype html><html lang="ko"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{{TITLE}}</title>
<style>
""" + BASE_CSS + """
.sc-header{margin-bottom:20px}
.sc-header h2{font-size:1.4rem;font-weight:700;color:#fff}
.sc-header p{font-size:.82rem;color:#8b8fa8;margin-top:4px;line-height:1.6}
.form-row{display:flex;gap:12px;align-items:flex-end;flex-wrap:wrap}
label{font-size:.82rem;color:#8b8fa8;display:block;margin-bottom:5px}
input[type=date]{background:#0f1117;border:1px solid #2a2d3a;border-radius:8px;
                 color:#e0e0e0;padding:9px 13px;font-size:.9rem;outline:none}
input[type=date]:focus{border-color:#4f8ef7}
.prog-wrap{margin-top:16px;display:none}
.prog-bg{background:#0f1117;border-radius:999px;height:8px;overflow:hidden}
.prog-bar{background:linear-gradient(90deg,#4f8ef7,#7b5ff7);height:100%;
           width:0%;transition:width .3s;border-radius:999px}
.prog-bar.loading{width:16%!important;
           background:linear-gradient(90deg,rgba(79,142,247,.12),#4f8ef7,#7b5ff7,rgba(123,95,247,.12));
           animation:progLoading 1.05s ease-in-out infinite}
@keyframes progLoading{
  0%{transform:translateX(-120%)}
  55%{transform:translateX(260%)}
  100%{transform:translateX(260%)}
}
.prog-txt{font-size:.78rem;color:#8b8fa8;margin-top:6px}
.summary{display:flex;gap:12px;flex-wrap:wrap;margin-bottom:18px}
.badge{padding:7px 16px;border-radius:8px;font-size:.85rem;font-weight:600}
.b1{background:#1e2d4a;color:#4f8ef7}
.b2{background:#1a2e1a;color:#4ade80}
.b3{background:#2a1e1e;color:#f87171}
.zone-title{font-size:.95rem;font-weight:700;margin:20px 0 8px;color:#c0c4d0;
             display:flex;align-items:center;gap:8px}
.zone-title::before{content:'';display:inline-block;width:4px;height:16px;
                     border-radius:2px;background:currentColor}
table{width:100%;border-collapse:collapse;font-size:.86rem}
th{background:#12151f;color:#8b8fa8;font-weight:600;padding:9px 11px;
   text-align:left;border-bottom:1px solid #2a2d3a;position:sticky;top:0;z-index:1}
td{padding:8px 11px;border-bottom:1px solid #1e2130}
tr:hover td{background:#1e2235}
.mkt-kospi{color:#60a5fa}.mkt-kosdaq{color:#a78bfa}.mkt-konex{color:#fb923c}
.neg5{color:#fbbf24}.neg10{color:#f97316}.neg15{color:#ef4444}
.pos{color:#4ade80}
.tbl-wrap{overflow-x:auto;border-radius:8px}
#msg{font-size:.88rem;color:#8b8fa8;margin-top:10px}
.cond-list{display:flex;flex-wrap:wrap;gap:8px;margin-top:12px}
.cond-tag{background:#12151f;border:1px solid #2a2d3a;border-radius:6px;
           padding:4px 12px;font-size:.78rem;color:#8b8fa8}
</style></head><body>
<div class="topbar">
  <button class="topbar-back" onclick="location.href='/'">← 목록으로</button>
  <span class="topbar-logo">{{ICON}} {{TITLE}}</span>
</div>
<div class="container">
  <div class="card">
    <div class="sc-header">
      <h2>{{TITLE}}</h2>
      <p>{{DESC}}</p>
      <div class="cond-list">{{COND_TAGS}}</div>
    </div>
    <div class="form-row">
      <div>
        <label>기준일</label>
        <input type="date" id="dateInput">
      </div>
      <button class="btn btn-primary" id="runBtn" onclick="startScan()">스크리닝 시작</button>
      <button class="btn btn-dl hidden" id="dlBtn" onclick="dlCsv()">CSV 다운로드</button>
    </div>
    <div class="prog-wrap" id="progWrap">
      <div class="prog-bg"><div class="prog-bar" id="progBar"></div></div>
      <div class="prog-txt" id="progTxt">준비 중...</div>
    </div>
    <div id="msg"></div>
    {{EXTRA_SECTION}}
  </div>

  <div class="card hidden" id="resultCard">
    <div class="summary" id="summary"></div>
    <div id="tables"></div>
  </div>
</div>

<script>
const SID = {{SID}};
document.getElementById('dateInput').value = new Date().toISOString().slice(0,10);
let evtSrc = null;

function startScan() {
  const d = document.getElementById('dateInput').value.replace(/-/g,'');
  if (!d) return;
  document.getElementById('runBtn').disabled = true;
  document.getElementById('dlBtn').classList.add('hidden');
  document.getElementById('resultCard').classList.add('hidden');
  document.getElementById('progWrap').style.display = 'block';
  document.getElementById('msg').textContent = '';
  setP(0,'종목 리스트 조회 중...');
  fetch(`/api/${SID}/start?date=${d}`).then(r=>r.json()).then(r=>{
    if(r.error){showMsg(r.error);resetBtn();return;}
    listenProg();
  });
}

function listenProg() {
  if(evtSrc) evtSrc.close();
  evtSrc = new EventSource(`/api/${SID}/progress`);
  evtSrc.onmessage = e => {
    const d = JSON.parse(e.data);
    if(d.status==='loading'){
      if(Number(d.total)>0){
        const pct = scanProgressPct(d.current, d.total);
        setP(pct, `${scanProgressText(d.current, d.total, '조회대상 준비 중')}${listingDoneText(d)}`);
      } else {
        setP(0, listingProgressText(d));
      }
    } else if(d.status==='running'){
      const pct = scanProgressPct(d.current, d.total);
      setP(pct, `${scanProgressText(d.current, d.total, '분석 중')}${listingDoneText(d)}`);
    } else if(d.status==='done'){
      evtSrc.close(); setP(100,`완료!${listingDoneText(d)}`); setTimeout(loadResult,400);
    }
  };
}

function fmtSec(sec){
  if(sec===undefined || sec===null || sec==='') return '';
  const n = Number(sec);
  if(!Number.isFinite(n)) return '';
  return n >= 60 ? `${Math.floor(n/60)}분 ${Math.round(n%60)}초` : `${n.toFixed(1)}초`;
}

function listingProgressText(d){
  if(d.listing_status === 'loading' && d.listing_started_at){
    const sec = Math.max(0, Date.now()/1000 - Number(d.listing_started_at));
    return `종목 리스트 조회 중... ${fmtSec(sec)} 경과`;
  }
  return `종목 리스트 조회 중...`;
}

function listingDoneText(d){
  if(d.listing_elapsed === undefined || d.listing_elapsed === null) return '';
  const count = d.listing_count ? ` / ${Number(d.listing_count).toLocaleString()}종목` : '';
  return `  |  종목조회 ${fmtSec(d.listing_elapsed)}${count}`;
}

function scanProgressPct(current,total){
  const c = Number(current) || 0;
  const t = Number(total) || 0;
  if(t <= 0) return 0;
  if(c <= 0) return 0;
  if(c >= t) return 100;
  return Math.max(1, Math.min(99, Math.round(c / t * 100)));
}

function scanProgressText(current,total,label='스캔 중',unit='종목'){
  const c = Number(current) || 0;
  const t = Number(total) || 0;
  if(t <= 0) return '조회대상 산정 중...';
  const pct = scanProgressPct(c, t);
  return `${label}... ${c.toLocaleString()} / ${t.toLocaleString()} ${unit} (${pct}%)`;
}

function setP(pct,txt){
  const bar = document.getElementById('progBar');
  const n = Number(pct) || 0;
  const clamped = Math.max(0, Math.min(100, n));
  const waiting = clamped <= 0 && String(txt || '').trim().length > 0;
  bar.classList.toggle('loading', waiting);
  bar.style.width = waiting ? '16%' : clamped + '%';
  document.getElementById('progTxt').textContent=txt;
}

function loadResult(){
  fetch(`/api/${SID}/result`).then(r=>r.json()).then(d=>{
    resetBtn();
    if(!d.rows||d.rows.length===0){showMsg('조건에 맞는 종목이 없습니다.');return;}
    document.getElementById('dlBtn').classList.remove('hidden');
    renderResult(d.date,d.rows);
    if(typeof highlightNewRows==='function') highlightNewRows(d.new_codes||[]);
    if(typeof onResultLoaded==='function') onResultLoaded(d);
  });
}

function renderResult(date,rows){
  document.getElementById('resultCard').classList.remove('hidden');
  document.getElementById('summary').innerHTML=
    `<span class="badge b1">기준일: ${date}</span>`+
    `<span class="badge b2">총 ${rows.length}개</span>`;

  // 조건38 패턴검색 (유사율(%) 컬럼 존재 여부로 판별 — 가장 먼저)
  if(rows.length && '유사율(%)' in rows[0]){
    let html=`<div class="tbl-wrap"><table>
    <tr><th>순위</th><th>시장</th><th>종목코드</th><th>종목명</th><th>시총(억)</th>
        <th>종가</th><th>전일대비(%)</th><th>유사율(%)</th><th>VL방향</th><th>EMA200</th></tr>`;
    rows.forEach((r,idx)=>{
      const mc=r['시장'].includes('KOSDAQ')?'mkt-kosdaq':r['시장'].includes('KONEX')?'mkt-konex':'mkt-kospi';
      const dc=r['전일대비(%)']<0?'neg10':'pos';
      const sim=r['유사율(%)'];
      const sc=sim>=90?'neg15':sim>=80?'neg10':sim>=70?'neg5':'';
      const rankStyle=idx===0?'style="color:#fbbf24;font-weight:800"':idx===1?'style="color:#d1d5db;font-weight:700"':idx===2?'style="color:#cd7f32;font-weight:700"':'';
      const vld=r['VL방향']==='↑'?'style="color:#4ade80"':'style="color:#f87171"';
      const e2d=r['EMA200']==='↑'?'style="color:#4ade80"':'style="color:#f87171"';
      html+=`<tr>
        <td ${rankStyle}>${['🥇','🥈','🥉','4위','5위'][idx]||idx+1+'위'}</td>
        <td class="${mc}">${r['시장']}</td><td>${r['종목코드']}</td>
        <td><b>${r['종목명']}</b></td>
        <td>${Number(r['시총(억)']).toLocaleString()}</td>
        <td>${Number(r['종가']).toLocaleString()}</td>
        <td class="${dc}">${r['전일대비(%)']}%</td>
        <td class="${sc}" style="font-weight:800">${sim}%</td>
        <td ${vld}>${r['VL방향']}</td>
        <td ${e2d}>${r['EMA200']}</td>
      </tr>`;
    });
    html+='</table></div>';
    document.getElementById('tables').innerHTML=html;
    return;
  }

  // 조건42 파워수급분석 — '상승이유' 컬럼 존재 여부로 판별 (최우선 체크)
  if(rows.length && '상승이유' in rows[0]){
    const ts=rows[0]['검색시각']||'';
    let html=`<div style="font-size:.78rem;color:#8b5cf6;margin-bottom:14px">
      🔬 KIS API 코스닥 거래대금 TOP20 파워수급분석 &nbsp;·&nbsp; 조회시각: ${ts}
    </div><div style="display:grid;grid-template-columns:repeat(auto-fill,minmax(480px,1fr));gap:14px">`;
    rows.forEach((r)=>{
      const pct=parseFloat(r['전일대비(%)']||0);
      const pctCls=pct>0?'pos':pct<0?'neg10':'';
      const pctSign=pct>0?'+':'';
      // 상승/하락이유 파싱
      let ru=[],rd=[],rel=[];
      try{ ru=JSON.parse(r['상승이유']); }catch(e){ ru=[String(r['상승이유']||'')]; }
      try{ rd=JSON.parse(r['하락이유']); }catch(e){ rd=[String(r['하락이유']||'')]; }
      try{ rel=JSON.parse(r['연관종목']); }catch(e){ rel=[]; }
      const ruHtml=ru.map(s=>`<li style="color:#4ade80">▲ ${s}</li>`).join('');
      const rdHtml=rd.map(s=>`<li style="color:#f87171">▼ ${s}</li>`).join('');
      const relHtml=rel.length
        ? rel.map(x=>`<span style="background:#1e1a35;border:1px solid #4a3a8a;border-radius:12px;padding:2px 9px;font-size:.72rem;color:#c084fc;margin:2px 2px 0 0;display:inline-block">${x.name}<span style="color:#6b5fa8;margin-left:4px">${x.code}</span></span>`).join('')
        : `<span style="color:#4a4d5e;font-size:.72rem">—</span>`;
      const per=parseFloat(r['PER']||0);
      const perStr=per>0?`PER ${per.toFixed(1)}배`:'';
      const w52h=parseInt(r['52주고']||0);
      const w52l=parseInt(r['52주저']||0);
      const w52Str=(w52h>0&&w52l>0)?`52주: ${w52l.toLocaleString()}~${w52h.toLocaleString()}`:'';
      html+=`<div style="background:#13161f;border:1px solid #2a1a4a;border-radius:12px;padding:14px 16px">
        <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:8px">
          <div>
            <span style="background:#2a1a4a;color:#c084fc;border-radius:4px;padding:1px 7px;font-size:.7rem;margin-right:6px">${r['순위']}위</span>
            <b style="font-size:1rem;color:#e0e0e0">${r['종목명']}</b>
            <span style="color:#4a4d5e;font-size:.75rem;margin-left:6px">${r['종목코드']}</span>
          </div>
          <div style="text-align:right">
            <div style="font-size:1.05rem;font-weight:800;color:#e0e0e0">${Number(r['현재가']).toLocaleString()}원</div>
            <div class="${pctCls}" style="font-size:.85rem;font-weight:700">${pctSign}${pct.toFixed(2)}%</div>
          </div>
        </div>
        <div style="display:flex;gap:6px;flex-wrap:wrap;margin-bottom:8px">
          <span style="background:#1a1a2e;border:1px solid #3a2a6a;border-radius:10px;padding:2px 9px;font-size:.72rem;color:#a78bfa">🏭 ${r['업종']||'—'}</span>
          <span style="background:#0a1a0a;border:1px solid #1a3a1a;border-radius:10px;padding:2px 9px;font-size:.72rem;color:#6b8b6b">거래대금 ${Number(r['거래대금(억)']).toLocaleString()}억</span>
          ${perStr?`<span style="background:#1a1000;border:1px solid #3a2a00;border-radius:10px;padding:2px 9px;font-size:.72rem;color:#a16207">${perStr}</span>`:''}
          ${w52Str?`<span style="background:#0a0a1a;border:1px solid #2a2a4a;border-radius:10px;padding:2px 9px;font-size:.72rem;color:#6b7280">${w52Str}</span>`:''}
        </div>
        <div style="display:grid;grid-template-columns:1fr 1fr;gap:8px;margin-bottom:8px">
          <div style="background:#0a1a0a;border-radius:8px;padding:8px 10px">
            <div style="font-size:.7rem;color:#4ade80;margin-bottom:4px;font-weight:600">📈 상승이유</div>
            <ul style="list-style:none;padding:0;margin:0;font-size:.75rem;line-height:1.6">${ruHtml}</ul>
          </div>
          <div style="background:#1a0a0a;border-radius:8px;padding:8px 10px">
            <div style="font-size:.7rem;color:#f87171;margin-bottom:4px;font-weight:600">📉 하락이유</div>
            <ul style="list-style:none;padding:0;margin:0;font-size:.75rem;line-height:1.6">${rdHtml}</ul>
          </div>
        </div>
        <div style="border-top:1px solid #2a1a4a;padding-top:8px">
          <span style="font-size:.7rem;color:#6b5fa8;margin-right:6px">🔗 연관종목</span>
          ${relHtml}
        </div>
      </div>`;
    });
    html+='</div>';
    document.getElementById('tables').innerHTML=html;
    return;
  }

  // 조건41 파워맵최고 — 거래량비율(%) 컬럼 존재 여부로 판별 (조건40보다 먼저 체크)
  if(rows.length && '거래량비율(%)' in rows[0]){
    let html=`<div style="font-size:.78rem;color:#f97316;margin-bottom:8px">
      🏆 KIS API 코스닥 파워맵최고 — 전일TOP10 外 + 금일TOP20 진입 + 거래량폭증 + 양봉 &nbsp;·&nbsp; 조회시각: ${rows[0]['검색시각']||''}
    </div>
    <div class="tbl-wrap"><table>
    <tr><th>순위</th><th>종목코드</th><th>종목명</th><th>현재가</th>
        <th>전일대비(%)</th><th>거래대금(억)</th><th>거래량비율(%)</th><th>변동</th></tr>`;
    rows.forEach((r)=>{
      const dc=r['전일대비(%)']>0?'pos':r['전일대비(%)']<0?'neg10':'';
      const vrPct=parseFloat(r['거래량비율(%)'])||0;
      const vrc=vrPct>=500?'neg15':vrPct>=300?'neg10':vrPct>=200?'neg5':'';
      const vd=String(r['변동']||'—');
      let vtd='';
      if(vd==='NEW') vtd='style="color:#fbbf24;font-weight:800;font-size:.95rem"';
      html+=`<tr>
        <td style="font-weight:700;color:#f97316">${r['순위']}</td>
        <td>${r['종목코드']}</td>
        <td><b>${r['종목명']}</b></td>
        <td>${Number(r['현재가']).toLocaleString()}</td>
        <td class="${dc}">${r['전일대비(%)']}%</td>
        <td style="font-weight:700">${Number(r['거래대금(억)']).toLocaleString()}</td>
        <td class="${vrc}" style="font-weight:800">${r['거래량비율(%)']}%</td>
        <td ${vtd}>${vd}</td>
      </tr>`;
    });
    html+='</table></div>';
    document.getElementById('tables').innerHTML=html;
    return;
  }

  // 조건40 파워맵우수 — 코스닥 거래대금 TOP20 (변동 컬럼 존재 여부로 판별)
  if(rows.length && '변동' in rows[0]){
    let html=`<div style="font-size:.78rem;color:#8b8fa8;margin-bottom:8px">
      📡 KIS API 코스닥 거래대금 실시간 TOP20 &nbsp;·&nbsp; 조회시각: ${rows[0]['검색시각']||''}
    </div>
    <div class="tbl-wrap"><table>
    <tr><th>순위</th><th>종목코드</th><th>종목명</th><th>현재가</th>
        <th>전일대비(%)</th><th>거래대금(억)</th><th>거래량</th><th>변동</th></tr>`;
    rows.forEach((r)=>{
      const dc=r['전일대비(%)']>0?'pos':r['전일대비(%)']<0?'neg10':'';
      const vd=String(r['변동']||'—');
      let vtd='';
      if(vd==='NEW')         vtd='style="color:#fbbf24;font-weight:800;font-size:.95rem"';
      else if(vd.startsWith('↑')) vtd='style="color:#4ade80;font-weight:700"';
      else if(vd.startsWith('↓')) vtd='style="color:#f87171;font-weight:700"';
      html+=`<tr>
        <td style="font-weight:700;color:#06b6d4">${r['순위']}</td>
        <td>${r['종목코드']}</td>
        <td><b>${r['종목명']}</b></td>
        <td>${Number(r['현재가']).toLocaleString()}</td>
        <td class="${dc}">${r['전일대비(%)']}%</td>
        <td style="font-weight:700">${r['거래대금(억)'].toLocaleString()}</td>
        <td>${Number(r['거래량']).toLocaleString()}</td>
        <td ${vtd}>${vd}</td>
      </tr>`;
    });
    html+='</table></div>';
    document.getElementById('tables').innerHTML=html;
    return;
  }

  // 조건39 VL이격시작 (VL이격(%) 컬럼 존재 여부로 판별)
  if(rows.length && 'VL이격(%)' in rows[0]){
    let html=`<div class="tbl-wrap"><table>
    <tr><th>순위</th><th>시장</th><th>종목코드</th><th>종목명</th><th>시총(억)</th>
        <th>종가</th><th>전일대비(%)</th><th>VL값</th><th>VL이격(%)</th><th>60일고가</th></tr>`;
    rows.forEach((r,idx)=>{
      const mc=r['시장'].includes('KOSDAQ')?'mkt-kosdaq':r['시장'].includes('KONEX')?'mkt-konex':'mkt-kospi';
      const dc=r['전일대비(%)']<0?'neg10':'pos';
      const gap=r['VL이격(%)'];
      const gc=gap>=300?'neg15':gap>=200?'neg10':gap>=100?'neg5':'';
      html+=`<tr>
        <td style="font-weight:700">${idx+1}</td>
        <td class="${mc}">${r['시장']}</td><td>${r['종목코드']}</td>
        <td><b>${r['종목명']}</b></td>
        <td>${Number(r['시총(억)']).toLocaleString()}</td>
        <td>${Number(r['종가']).toLocaleString()}</td>
        <td class="${dc}">${r['전일대비(%)']}%</td>
        <td>${Number(r['VL값']).toLocaleString()}</td>
        <td class="${gc}" style="font-weight:800">${gap}%</td>
        <td style="color:#8b8fa8">${Number(r['60일고가']).toLocaleString()}</td>
      </tr>`;
    });
    html+='</table></div>';
    document.getElementById('tables').innerHTML=html;
    return;
  }

  // 조건3 (스퀴즈비율 컬럼 존재 여부로 판별)
  if(rows.length && '스퀴즈비율(%)' in rows[0]){
    let html=`<div class="tbl-wrap"><table>
    <tr><th>시장</th><th>종목코드</th><th>종목명</th><th>시총(억)</th>
        <th>종가</th><th>EMA200</th><th>스퀴즈비율(%)</th></tr>`;
    rows.forEach(r=>{
      const mc=r['시장'].includes('KOSDAQ')?'mkt-kosdaq':r['시장'].includes('KONEX')?'mkt-konex':'mkt-kospi';
      const sq=r['스퀴즈비율(%)'];
      const sc=sq<=20?'neg15':sq<=40?'neg10':sq<=60?'neg5':'';
      html+=`<tr>
        <td class="${mc}">${r['시장']}</td><td>${r['종목코드']}</td>
        <td><b>${r['종목명']}</b></td>
        <td>${Number(r['시총(억)']).toLocaleString()}</td>
        <td>${Number(r['종가']).toLocaleString()}</td>
        <td>${Number(r['EMA200']).toLocaleString()}</td>
        <td class="${sc}">${sq}%</td>
      </tr>`;
    });
    html+='</table></div>';
    document.getElementById('tables').innerHTML=html;
    return;
  }

  // 조건7 폭발준비 (돌파 컬럼 존재 여부로 판별)
  if(rows.length && '돌파' in rows[0]){
    let html=`<div class="tbl-wrap"><table>
    <tr><th>시장</th><th>종목코드</th><th>종목명</th><th>시총(억)</th>
        <th>종가</th><th>전일대비(%)</th><th>ASGMA점수</th><th>돌파</th><th>SMA5</th><th>SMA20</th></tr>`;
    rows.forEach(r=>{
      const mc=r['시장'].includes('KOSDAQ')?'mkt-kosdaq':r['시장'].includes('KONEX')?'mkt-konex':'mkt-kospi';
      const dc=r['전일대비(%)']<0?'neg10':'pos';
      const bc=r['돌파']==='SMA5+SMA20'?'neg15':r['돌파']==='SMA20'?'neg10':'neg5';
      html+=`<tr>
        <td class="${mc}">${r['시장']}</td><td>${r['종목코드']}</td>
        <td><b>${r['종목명']}</b></td>
        <td>${Number(r['시총(억)']).toLocaleString()}</td>
        <td>${Number(r['종가']).toLocaleString()}</td>
        <td class="${dc}">${r['전일대비(%)']}%</td>
        <td>${r['ASGMA점수']}</td>
        <td class="${bc}" style="font-weight:700">${r['돌파']}</td>
        <td>${Number(r['SMA5']).toLocaleString()}</td>
        <td>${Number(r['SMA20']).toLocaleString()}</td>
      </tr>`;
    });
    html+='</table></div>';
    document.getElementById('tables').innerHTML=html;
    return;
  }

  // 조건6 폭발직전 (ATR14 컬럼 존재 여부로 판별 — 조건5보다 먼저)
  if(rows.length && 'ATR14' in rows[0]){
    let html=`<div class="tbl-wrap"><table>
    <tr><th>시장</th><th>종목코드</th><th>종목명</th><th>시총(억)</th>
        <th>종가</th><th>전일대비(%)</th><th>ASGMA점수</th><th>ATR14</th><th>3일등락/ATR(%)</th></tr>`;
    rows.forEach(r=>{
      const mc=r['시장'].includes('KOSDAQ')?'mkt-kosdaq':r['시장'].includes('KONEX')?'mkt-konex':'mkt-kospi';
      const dc=r['전일대비(%)']<0?'neg10':'pos';
      const sc=r['ASGMA점수']>=10?'neg15':r['ASGMA점수']>=6?'neg10':'neg5';
      html+=`<tr>
        <td class="${mc}">${r['시장']}</td><td>${r['종목코드']}</td>
        <td><b>${r['종목명']}</b></td>
        <td>${Number(r['시총(억)']).toLocaleString()}</td>
        <td>${Number(r['종가']).toLocaleString()}</td>
        <td class="${dc}">${r['전일대비(%)']}%</td>
        <td class="${sc}" style="font-weight:700">${r['ASGMA점수']}</td>
        <td>${r['ATR14']}</td>
        <td style="color:#f59e0b">${r['3일등락/ATR(%)']}</td>
      </tr>`;
    });
    html+='</table></div>';
    document.getElementById('tables').innerHTML=html;
    return;
  }

  // 조건5 ASGMA (ASGMA점수 컬럼 존재 여부로 판별)
  if(rows.length && 'ASGMA점수' in rows[0]){
    let html=`<div class="tbl-wrap"><table>
    <tr><th>시장</th><th>종목코드</th><th>종목명</th><th>시총(억)</th>
        <th>종가</th><th>전일대비(%)</th><th>VR14</th><th>ATR비율(%)</th><th>ASGMA점수</th></tr>`;
    rows.forEach(r=>{
      const mc=r['시장'].includes('KOSDAQ')?'mkt-kosdaq':r['시장'].includes('KONEX')?'mkt-konex':'mkt-kospi';
      const dc=r['전일대비(%)']<0?'neg10':'pos';
      const sc=r['ASGMA점수']>=10?'neg15':r['ASGMA점수']>=6?'neg10':r['ASGMA점수']>=3?'neg5':'';
      html+=`<tr>
        <td class="${mc}">${r['시장']}</td><td>${r['종목코드']}</td>
        <td><b>${r['종목명']}</b></td>
        <td>${Number(r['시총(억)']).toLocaleString()}</td>
        <td>${Number(r['종가']).toLocaleString()}</td>
        <td class="${dc}">${r['전일대비(%)']}%</td>
        <td>${r['VR14']}</td>
        <td>${r['ATR비율(%)']}%</td>
        <td class="${sc}" style="font-weight:700">${r['ASGMA점수']}</td>
      </tr>`;
    });
    html+='</table></div>';
    document.getElementById('tables').innerHTML=html;
    return;
  }

  // 조건4 (점수 컬럼 존재 여부로 판별)
  if(rows.length && '점수' in rows[0]){
    let html=`<div class="tbl-wrap"><table>
    <tr><th>시장</th><th>종목코드</th><th>종목명</th><th>시총(억)</th>
        <th>종가</th><th>전일대비(%)</th><th>거래량</th><th>20일평균</th><th>점수</th></tr>`;
    rows.forEach(r=>{
      const mc=r['시장'].includes('KOSDAQ')?'mkt-kosdaq':r['시장'].includes('KONEX')?'mkt-konex':'mkt-kospi';
      const dc=r['전일대비(%)']<0?'neg10':'pos';
      const sc=r['점수']>=3?'neg15':r['점수']>=2?'neg10':r['점수']>=1?'neg5':r['점수']<0?'mkt-kospi':'';
      const volRatio=r['20일평균']>0?Math.round(r['거래량']/r['20일평균']*100)+'%':'-';
      html+=`<tr>
        <td class="${mc}">${r['시장']}</td><td>${r['종목코드']}</td>
        <td><b>${r['종목명']}</b></td>
        <td>${Number(r['시총(억)']).toLocaleString()}</td>
        <td>${Number(r['종가']).toLocaleString()}</td>
        <td class="${dc}">${r['전일대비(%)']}%</td>
        <td>${Number(r['거래량']).toLocaleString()}</td>
        <td>${Number(r['20일평균']).toLocaleString()} <small style="color:#8b8fa8">(${volRatio})</small></td>
        <td class="${sc}" style="font-weight:700">${r['점수']}</td>
      </tr>`;
    });
    html+='</table></div>';
    document.getElementById('tables').innerHTML=html;
    return;
  }

  // 조건36 이제진짜출발 (세력20평단 && 과열스코어 && 이격도(20) 동시 존재로 판별 — 조건23·33보다 먼저)
  if(rows.length && '세력20평단' in rows[0] && '과열스코어' in rows[0] && '이격도(20)' in rows[0]){
    let html=`<div class="tbl-wrap"><table>
    <tr><th>시장</th><th>종목코드</th><th>종목명</th><th>시총(억)</th>
        <th>종가</th><th>전일대비(%)</th>
        <th>세력20평단</th><th>평단대비(%)</th><th>ADX(11)</th>
        <th>과열스코어</th><th>이격도(20)</th></tr>`;
    rows.forEach(r=>{
      const mc=r['시장'].includes('KOSDAQ')?'mkt-kosdaq':r['시장'].includes('KONEX')?'mkt-konex':'mkt-kospi';
      const dc=r['전일대비(%)']<0?'neg10':'pos';
      const pg=r['평단대비(%)'];
      const pc=pg>=5?'neg15':pg>=2?'neg10':'pos';
      const adx=r['ADX(11)'];
      const ac=adx>=40?'neg15':adx>=30?'neg10':'neg5';
      const sc=r['과열스코어'];
      const scc=sc>=8?'neg15':sc>=5?'neg10':'neg5';
      const dp=r['이격도(20)'];
      const dpc=dp>=130?'neg15':dp>=115?'neg10':'neg5';
      html+=`<tr>
        <td class="${mc}">${r['시장']}</td><td>${r['종목코드']}</td>
        <td><b>${r['종목명']}</b></td>
        <td>${Number(r['시총(억)']).toLocaleString()}</td>
        <td>${Number(r['종가']).toLocaleString()}</td>
        <td class="${dc}">${r['전일대비(%)']}%</td>
        <td style="color:#f59e0b;font-weight:700">${Number(r['세력20평단']).toLocaleString()}</td>
        <td class="${pc}" style="font-weight:700">${pg>=0?'+':''}${pg}%</td>
        <td class="${ac}" style="font-weight:700">${adx}</td>
        <td class="${scc}" style="font-weight:700">${sc}</td>
        <td class="${dpc}">${dp}%</td>
      </tr>`;
    });
    html+='</table></div>';
    document.getElementById('tables').innerHTML=html;
    return;
  }

  // 조건23 세력20평단돌파+ADX (ADX(11) && 세력20평단 컬럼 동시 존재로 판별 — 조건14보다 먼저)
  if(rows.length && 'ADX(11)' in rows[0] && '세력20평단' in rows[0]){
    let html=`<div class="tbl-wrap"><table>
    <tr><th>시장</th><th>종목코드</th><th>종목명</th><th>시총(억)</th>
        <th>종가</th><th>전일대비(%)</th>
        <th>세력20평단</th><th>평단대비(%)</th><th>ADX(11)</th></tr>`;
    rows.forEach(r=>{
      const mc=r['시장'].includes('KOSDAQ')?'mkt-kosdaq':r['시장'].includes('KONEX')?'mkt-konex':'mkt-kospi';
      const dc=r['전일대비(%)']<0?'neg10':'pos';
      const pg=r['평단대비(%)'];
      const pc=pg>=5?'neg15':pg>=2?'neg10':'pos';
      const adx=r['ADX(11)'];
      const ac=adx>=40?'neg15':adx>=30?'neg10':'neg5';
      html+=`<tr>
        <td class="${mc}">${r['시장']}</td><td>${r['종목코드']}</td>
        <td><b>${r['종목명']}</b></td>
        <td>${Number(r['시총(억)']).toLocaleString()}</td>
        <td>${Number(r['종가']).toLocaleString()}</td>
        <td class="${dc}">${r['전일대비(%)']}%</td>
        <td style="color:#f59e0b">${Number(r['세력20평단']).toLocaleString()}</td>
        <td class="${pc}" style="font-weight:700">${pg>=0?'+':''}${pg}%</td>
        <td class="${ac}" style="font-weight:700">${adx}</td>
      </tr>`;
    });
    html+='</table></div>';
    document.getElementById('tables').innerHTML=html;
    return;
  }

  // 조건14 세력20평단 돌파 (세력20평단 컬럼 존재 여부로 판별)
  if(rows.length && '세력20평단' in rows[0]){
    // 검색시각 / 소요시간 배지 추가
    const scanTs  = rows[0]['검색시각'] || '';
    const elapsed = rows[0]['소요(초)'] || 0;
    function fmtSec(sec){ const m=Math.floor(sec/60),s2=sec%60; return m>0?`${m}분 ${s2}초`:`${s2}초`; }
    if(scanTs){
      document.getElementById('summary').innerHTML +=
        `<span class="badge" style="background:#1e2a1e;color:#4ade80;border:1px solid #2a5a2a">` +
        `🕐 검색시각: ${scanTs}</span>` +
        (elapsed ? `<span class="badge" style="background:#1e1e2e;color:#818cf8;border:1px solid #2a2a5a">` +
        `⏱ 소요: ${fmtSec(elapsed)}</span>` : '');
    }
    let html=`<div class="tbl-wrap"><table>
    <tr><th>시장</th><th>종목코드</th><th>종목명</th><th>시총(억)</th>
        <th>종가</th><th>전일대비(%)</th>
        <th>세력20평단</th><th>평단대비(%)</th><th>거래량비율(%)</th><th>검색시각</th></tr>`;
    rows.forEach(r=>{
      const mc=r['시장'].includes('KOSDAQ')?'mkt-kosdaq':r['시장'].includes('KONEX')?'mkt-konex':'mkt-kospi';
      const dc=r['전일대비(%)']<0?'neg10':'pos';
      // 평단대비: 종가가 평단보다 얼마나 위인지
      const pg=r['평단대비(%)'];
      const pc=pg>=5?'neg15':pg>=2?'neg10':'pos';
      // 거래량비율: 높을수록 강한 돌파
      const vr=r['거래량비율(%)'];
      const vc=vr>=500?'neg15':vr>=300?'neg10':'neg5';
      html+=`<tr>
        <td class="${mc}">${r['시장']}</td><td>${r['종목코드']}</td>
        <td><b>${r['종목명']}</b></td>
        <td>${Number(r['시총(억)']).toLocaleString()}</td>
        <td>${Number(r['종가']).toLocaleString()}</td>
        <td class="${dc}">${r['전일대비(%)']}%</td>
        <td style="color:#f59e0b">${Number(r['세력20평단']).toLocaleString()}</td>
        <td class="${pc}" style="font-weight:700">+${pg}%</td>
        <td class="${vc}" style="font-weight:700">${vr}%</td>
        <td style="color:#8b8fa8;font-size:.78rem">${(r['검색시각']||'').slice(11)}</td>
      </tr>`;
    });
    html+='</table></div>';
    document.getElementById('tables').innerHTML=html;
    return;
  }

  // 조건15 세력20평단임박 (평단까지(%) 컬럼으로 판별)
  if(rows.length && '평단까지(%)' in rows[0]){
    const scanTs  = rows[0]['검색시각'] || '';
    const elapsed = rows[0]['소요(초)'] || 0;
    function fmtSec15(sec){ const m=Math.floor(sec/60),s2=sec%60; return m>0?`${m}분 ${s2}초`:`${s2}초`; }
    if(scanTs){
      document.getElementById('summary').innerHTML +=
        `<span class="badge" style="background:#1e2a1e;color:#4ade80;border:1px solid #2a5a2a">` +
        `🕐 검색시각: ${scanTs}</span>` +
        (elapsed ? `<span class="badge" style="background:#1e1e2e;color:#818cf8;border:1px solid #2a2a5a">` +
        `⏱ 소요: ${fmtSec15(elapsed)}</span>` : '');
    }
    let html=`<div class="tbl-wrap"><table>
    <tr><th>시장</th><th>종목코드</th><th>종목명</th><th>시총(억)</th>
        <th>종가</th><th>전일대비(%)</th>
        <th>세력20평단</th><th>평단까지(%)</th><th>VL</th><th>VL상승(%)</th></tr>`;
    rows.forEach(r=>{
      const mc=r['시장'].includes('KOSDAQ')?'mkt-kosdaq':r['시장'].includes('KONEX')?'mkt-konex':'mkt-kospi';
      const dc=r['전일대비(%)']<0?'neg10':'pos';
      // 평단까지: 작을수록(임박) 강조
      const pg=r['평단까지(%)'];
      const pc=pg<=1?'neg15':pg<=3?'neg10':'neg5';
      // VL 상승률: 클수록 강조
      const vg=r['VL상승(%)'];
      const vc=vg>=10?'neg15':vg>=5?'neg10':'pos';
      html+=`<tr>
        <td class="${mc}">${r['시장']}</td><td>${r['종목코드']}</td>
        <td><b>${r['종목명']}</b></td>
        <td>${Number(r['시총(억)']).toLocaleString()}</td>
        <td>${Number(r['종가']).toLocaleString()}</td>
        <td class="${dc}">${r['전일대비(%)']}%</td>
        <td style="color:#a78bfa">${Number(r['세력20평단']).toLocaleString()}</td>
        <td class="${pc}" style="font-weight:700">-${pg}%</td>
        <td style="color:#60a5fa">${Number(r['VL']).toLocaleString()}</td>
        <td class="${vc}" style="font-weight:700">+${vg}%</td>
      </tr>`;
    });
    html+='</table></div>';
    document.getElementById('tables').innerHTML=html;
    return;
  }

  // 조건19 급락회귀선 (최대이탈(%) 컬럼 존재 여부로 판별)
  if(rows.length && '최대이탈(%)' in rows[0]){
    let html=`<div class="tbl-wrap"><table>
    <tr><th>시장</th><th>종목코드</th><th>종목명</th><th>시총(억)</th>
        <th>종가</th><th>전일대비(%)</th>
        <th>VL</th><th>종가vsVL(%)</th><th>최대이탈(%)</th>
        <th>이번주저점</th><th>지난주저점</th></tr>`;
    rows.forEach(r=>{
      const mc=r['시장'].includes('KOSDAQ')?'mkt-kosdaq':r['시장'].includes('KONEX')?'mkt-konex':'mkt-kospi';
      const dc=r['전일대비(%)']<0?'neg10':'pos';
      // 종가vsVL: 양수(종가>VL) → 초록
      const vg=r['종가vsVL(%)'];
      const vc=vg>=5?'neg15':vg>=2?'pos':'pos';
      // 최대이탈: 클수록 더 큰 급락 → 강조
      const mg=r['최대이탈(%)'];
      const mmc=mg>=30?'neg15':mg>=20?'neg10':'neg5';
      // 이번주저점 > 지난주저점 → 저점 상승 확인
      const lwThis=r['이번주저점'];
      const lwPrev=r['지난주저점'];
      const lwUp=lwThis>lwPrev;
      html+=`<tr>
        <td class="${mc}">${r['시장']}</td><td>${r['종목코드']}</td>
        <td><b>${r['종목명']}</b></td>
        <td>${Number(r['시총(억)']).toLocaleString()}</td>
        <td>${Number(r['종가']).toLocaleString()}</td>
        <td class="${dc}">${r['전일대비(%)']}%</td>
        <td style="color:#fb923c">${Number(r['VL']).toLocaleString()}</td>
        <td class="${vc}" style="font-weight:700">+${vg}%</td>
        <td class="${mmc}" style="font-weight:700">${mg}%</td>
        <td style="color:${lwUp?'#4ade80':'#f87171'};font-weight:700">${Number(lwThis).toLocaleString()}</td>
        <td style="color:#8b8fa8">${Number(lwPrev).toLocaleString()}</td>
      </tr>`;
    });
    html+='</table></div>';
    document.getElementById('tables').innerHTML=html;
    return;
  }

  // 조건16 RSI밴드 압축돌파 (UPPER 컬럼 + 밴드폭3일감소 컬럼으로 판별)
  if(rows.length && 'UPPER' in rows[0] && '밴드폭3일감소' in rows[0]){
    let html=`<div class="tbl-wrap"><table>
    <tr><th>시장</th><th>종목코드</th><th>종목명</th><th>시총(억)</th>
        <th>종가</th><th>전일대비(%)</th>
        <th>RSI(21)</th><th>S_RSI</th><th>UPPER</th><th>LOWER</th>
        <th>밴드폭</th><th>3일감소</th></tr>`;
    rows.forEach(r=>{
      const mc=r['시장'].includes('KOSDAQ')?'mkt-kosdaq':r['시장'].includes('KONEX')?'mkt-konex':'mkt-kospi';
      const dc=r['전일대비(%)']<0?'neg10':'pos';
      // S_RSI vs UPPER: 상향돌파 → 초록 강조
      const gap = r['S_RSI'] - r['UPPER'];
      const sc  = gap>2?'neg15':gap>0?'pos':'neg5';
      // 3일감소: 클수록 더 압축됨
      const brc = r['밴드폭3일감소'];
      const bc  = brc>=5?'neg15':brc>=2?'neg10':'pos';
      html+=`<tr>
        <td class="${mc}">${r['시장']}</td><td>${r['종목코드']}</td>
        <td><b>${r['종목명']}</b></td>
        <td>${Number(r['시총(억)']).toLocaleString()}</td>
        <td>${Number(r['종가']).toLocaleString()}</td>
        <td class="${dc}">${r['전일대비(%)']}%</td>
        <td style="color:#e0e0e0">${r['RSI(21)']}</td>
        <td class="${sc}" style="font-weight:700">${r['S_RSI']}</td>
        <td style="color:#f87171">${r['UPPER']}</td>
        <td style="color:#60a5fa">${r['LOWER']}</td>
        <td style="color:#8b8fa8">${r['밴드폭']}</td>
        <td class="${bc}" style="font-weight:700">▼${brc}</td>
      </tr>`;
    });
    html+='</table></div>';
    document.getElementById('tables').innerHTML=html;
    return;
  }

  // 조건18 MACD Reloaded2 (히스트색 컬럼 존재로 판별 — 조건17보다 먼저 체크)
  if(rows.length && '히스트색' in rows[0]){
    // 히스트색 → 배경색 매핑
    const histBg = {'라임':'#00ff0033','청록':'#b2dfdb33','연분홍':'#ffcdd233','진빨강':'#ef535033'};
    const histFg = {'라임':'#4ade80',  '청록':'#5eead4',  '연분홍':'#fca5a5',  '진빨강':'#f87171'};
    let html=`<div class="tbl-wrap"><table>
    <tr><th>시장</th><th>종목코드</th><th>종목명</th><th>시총(억)</th>
        <th>종가</th><th>전일대비(%)</th>
        <th>거래대금(억)</th><th>거래량(만주)</th>
        <th>MACD</th><th>Signal</th><th>히스토그램</th><th>전일히스토</th><th>색상</th></tr>`;
    rows.forEach(r=>{
      const mc  = r['시장'].includes('KOSDAQ')?'mkt-kosdaq':r['시장'].includes('KONEX')?'mkt-konex':'mkt-kospi';
      const dc  = r['전일대비(%)']<0?'neg10':'pos';
      const tv  = r['거래대금(억)'];
      const tvc = tv>=1600?'neg15':tv>=800?'neg10':'pos';
      const vv  = r['거래량(만주)'];
      const vvc = vv>=200?'neg15':vv>=100?'neg10':'pos';
      const hv  = r['히스토그램'];
      const phv = r['전일히스토'];
      const phc = phv<0?'neg5':'neg15';
      const hcolor = r['히스트색']||'라임';
      const hbg  = histBg[hcolor]||'';
      const hfg  = histFg[hcolor]||'#4ade80';
      const macdAbove = r['MACD'] > r['Signal'];
      html+=`<tr style="background:${hbg}">
        <td class="${mc}">${r['시장']}</td><td>${r['종목코드']}</td>
        <td><b>${r['종목명']}</b></td>
        <td>${Number(r['시총(억)']).toLocaleString()}</td>
        <td>${Number(r['종가']).toLocaleString()}</td>
        <td class="${dc}">${r['전일대비(%)']}%</td>
        <td class="${tvc}" style="font-weight:700">${Number(tv).toLocaleString()}</td>
        <td class="${vvc}">${vv}</td>
        <td style="color:${macdAbove?'#4ade80':'#f87171'};font-weight:700">${r['MACD']}</td>
        <td style="color:#8b8fa8">${r['Signal']}</td>
        <td style="color:${hfg};font-weight:700">${hv>0?'+':''}${hv}</td>
        <td class="${phc}">${phv>0?'+':''}${phv}</td>
        <td style="color:${hfg};font-size:.75rem">■ ${hcolor}</td>
      </tr>`;
    });
    html+='</table></div>';
    document.getElementById('tables').innerHTML=html;
    return;
  }

  // 조건17 MACD Reloaded (히스토그램 + 전일히스토 컬럼으로 판별)
  if(rows.length && '히스토그램' in rows[0] && '전일히스토' in rows[0]){
    let html=`<div class="tbl-wrap"><table>
    <tr><th>시장</th><th>종목코드</th><th>종목명</th><th>시총(억)</th>
        <th>종가</th><th>전일대비(%)</th>
        <th>거래대금(억)</th><th>거래량(만주)</th>
        <th>MACD</th><th>Signal</th><th>히스토그램</th><th>전일히스토</th></tr>`;
    rows.forEach(r=>{
      const mc  = r['시장'].includes('KOSDAQ')?'mkt-kosdaq':r['시장'].includes('KONEX')?'mkt-konex':'mkt-kospi';
      const dc  = r['전일대비(%)']<0?'neg10':'pos';
      // 거래대금 1600억 / 거래량 200만주 기준 강조
      const tv  = r['거래대금(억)'];
      const tvc = tv>=1600?'neg15':tv>=800?'neg10':'pos';
      const vv  = r['거래량(만주)'];
      const vvc = vv>=200?'neg15':vv>=100?'neg10':'pos';
      // 히스토그램
      const hv  = r['히스토그램'];
      const hc  = hv>0?'pos':'neg10';
      const phv = r['전일히스토'];
      const phc = phv<0?'neg5':'neg15';
      const macdAbove = r['MACD'] > r['Signal'];
      html+=`<tr>
        <td class="${mc}">${r['시장']}</td><td>${r['종목코드']}</td>
        <td><b>${r['종목명']}</b></td>
        <td>${Number(r['시총(억)']).toLocaleString()}</td>
        <td>${Number(r['종가']).toLocaleString()}</td>
        <td class="${dc}">${r['전일대비(%)']}%</td>
        <td class="${tvc}" style="font-weight:700">${Number(tv).toLocaleString()}</td>
        <td class="${vvc}">${vv}</td>
        <td style="color:${macdAbove?'#4ade80':'#f87171'};font-weight:700">${r['MACD']}</td>
        <td style="color:#8b8fa8">${r['Signal']}</td>
        <td class="${hc}" style="font-weight:700">${hv>0?'+':''}${hv}</td>
        <td class="${phc}">${phv>0?'+':''}${phv}</td>
      </tr>`;
    });
    html+='</table></div>';
    document.getElementById('tables').innerHTML=html;
    return;
  }

  // 조건13 이제출발 (엔벨로프30일상승(%) 컬럼 존재 여부로 판별)
  if(rows.length && '엔벨로프30일상승(%)' in rows[0]){
    let html=`<div class="tbl-wrap"><table>
    <tr><th>시장</th><th>종목코드</th><th>종목명</th><th>시총(억)</th>
        <th>종가</th><th>전일대비(%)</th>
        <th>엔벨로프상단</th><th>30일상승(%)</th><th>최근접근(%)</th></tr>`;
    rows.forEach(r=>{
      const mc=r['시장'].includes('KOSDAQ')?'mkt-kosdaq':r['시장'].includes('KONEX')?'mkt-konex':'mkt-kospi';
      const dc=r['전일대비(%)']<0?'neg10':'pos';
      // 30일상승: 5% 이하 상승 또는 하락 → 횡보/하락 구간
      const ed=r['엔벨로프30일상승(%)'];
      const ec=ed<=0?'neg5':ed<=3?'pos':'neg5';
      const esign=ed>=0?'+':'';
      // 최근접근: 양수=돌파(초록), 음수=접근(노랑/주황)
      const ag=r['최근접근(%)'];
      const ac=ag>=0?'pos':ag>=-1?'neg5':'neg10';
      const asign=ag>=0?'+':'';
      html+=`<tr>
        <td class="${mc}">${r['시장']}</td><td>${r['종목코드']}</td>
        <td><b>${r['종목명']}</b></td>
        <td>${Number(r['시총(억)']).toLocaleString()}</td>
        <td>${Number(r['종가']).toLocaleString()}</td>
        <td class="${dc}">${r['전일대비(%)']}%</td>
        <td style="color:#34d399">${Number(r['엔벨로프상단']).toLocaleString()}</td>
        <td class="${ec}">${esign}${ed}%</td>
        <td class="${ac}" style="font-weight:700">${asign}${ag}%</td>
      </tr>`;
    });
    html+='</table></div>';
    document.getElementById('tables').innerHTML=html;
    return;
  }

  // 조건28 RSI밴드돌파+ADX (S_RSI 컬럼 존재로 판별 — 조건12보다 먼저)
  if(rows.length && 'S_RSI' in rows[0]){
    let html=`<div class="tbl-wrap"><table>
    <tr><th>시장</th><th>종목코드</th><th>종목명</th><th>시총(억)</th>
        <th>종가</th><th>전일대비(%)</th>
        <th>RSI(21)</th><th>S_RSI</th><th>UPPER</th><th>LOWER</th>
        <th>밴드폭</th><th>ADX(11)</th></tr>`;
    rows.forEach(r=>{
      const mc=r['시장'].includes('KOSDAQ')?'mkt-kosdaq':r['시장'].includes('KONEX')?'mkt-konex':'mkt-kospi';
      const dc=r['전일대비(%)']<0?'neg10':'pos';
      const gap=r['S_RSI']-r['UPPER'];
      const sc=gap>2?'neg15':gap>0?'pos':'neg5';
      const adx=r['ADX(11)'];
      const ac=adx>=40?'neg15':adx>=30?'neg10':'neg5';
      html+=`<tr>
        <td class="${mc}">${r['시장']}</td><td>${r['종목코드']}</td>
        <td><b>${r['종목명']}</b></td>
        <td>${Number(r['시총(억)']).toLocaleString()}</td>
        <td>${Number(r['종가']).toLocaleString()}</td>
        <td class="${dc}">${r['전일대비(%)']}%</td>
        <td style="color:#e0e0e0">${r['RSI(21)']}</td>
        <td class="${sc}" style="font-weight:700">${r['S_RSI']}</td>
        <td style="color:#f87171">${r['UPPER']}</td>
        <td style="color:#60a5fa">${r['LOWER']}</td>
        <td style="color:#8b8fa8">${r['밴드폭']}</td>
        <td class="${ac}" style="font-weight:700">${adx}</td>
      </tr>`;
    });
    html+='</table></div>';
    document.getElementById('tables').innerHTML=html;
    return;
  }

  // 조건12 Harvard RSI (RSI(21) 컬럼 존재 여부로 판별)
  if(rows.length && 'RSI(21)' in rows[0]){
    let html=`<div class="tbl-wrap"><table>
    <tr><th>시장</th><th>종목코드</th><th>종목명</th><th>시총(억)</th>
        <th>종가</th><th>전일대비(%)</th>
        <th>RSI(21)</th><th>A</th><th>B</th><th>A−B</th></tr>`;
    rows.forEach(r=>{
      const mc=r['시장'].includes('KOSDAQ')?'mkt-kosdaq':r['시장'].includes('KONEX')?'mkt-konex':'mkt-kospi';
      const dc=r['전일대비(%)']<0?'neg10':'pos';
      const ri=r['RSI(21)'];
      // RSI 색상: 30↓ 과매도(빨강) / 50~70 중립(초록) / 70↑ 과매수(노랑)
      const rc=ri>=70?'neg5':ri>=50?'pos':ri>=30?'neg10':'neg15';
      const ab=r['A-B'];
      const ac=ab>=2?'neg15':ab>=1?'neg10':ab>=0.5?'neg5':'pos';
      html+=`<tr>
        <td class="${mc}">${r['시장']}</td><td>${r['종목코드']}</td>
        <td><b>${r['종목명']}</b></td>
        <td>${Number(r['시총(억)']).toLocaleString()}</td>
        <td>${Number(r['종가']).toLocaleString()}</td>
        <td class="${dc}">${r['전일대비(%)']}%</td>
        <td class="${rc}" style="font-weight:700">${ri}</td>
        <td style="color:#e879f9">${r['A']}</td>
        <td style="color:#8b8fa8">${r['B']}</td>
        <td class="${ac}" style="font-weight:700">+${ab}</td>
      </tr>`;
    });
    html+='</table></div>';
    document.getElementById('tables').innerHTML=html;
    return;
  }

  // 조건11 VL매수타점 (종가VL갭(%) 컬럼 존재 여부로 판별 — 조건10보다 먼저 검사)
  if(rows.length && '종가VL갭(%)' in rows[0]){
    let html=`<div class="tbl-wrap"><table>
    <tr><th>시장</th><th>종목코드</th><th>종목명</th><th>시총(억)</th>
        <th>종가</th><th>전일대비(%)</th><th>5일변동(%)</th>
        <th>VL</th><th>VL6일변동(%)</th><th>종가↑VL(%)</th>
        <th>엔벨로프상단</th><th>엔벨로프↑VL(%)</th><th>상단여력(%)</th>
        <th>세력평단</th><th>세력평단변화(%)</th></tr>`;
    rows.forEach(r=>{
      const mc=r['시장'].includes('KOSDAQ')?'mkt-kosdaq':r['시장'].includes('KONEX')?'mkt-konex':'mkt-kospi';
      const dc=r['전일대비(%)']<0?'neg10':'pos';
      // 5일변동: 횡보(-10~+10%)
      const fd=r['5일변동(%)'];
      const fc=fd<0?'neg5':'pos';
      const fsign=fd>=0?'+':'';
      // VL6일변동: 클수록 V자 진폭 뚜렷
      const rv=r['VL6일변동(%)'];
      const rc=rv>=5?'neg15':rv>=3?'neg10':rv>=1.5?'neg5':'';
      // 종가VL갭: 10~15% 노랑, 15~25% 주황, 25%↑ 빨강
      const vg=r['종가VL갭(%)'];
      const vc=vg>=25?'neg15':vg>=15?'neg10':'neg5';
      // 엔벨로프VL차: 클수록 상승 여력 큼
      const eg=r['엔벨로프VL차(%)'];
      const ec=eg>=40?'neg15':eg>=25?'neg10':eg>=15?'neg5':'pos';
      // 상단여력: 현재가→엔벨로프까지 남은 %
      const sg=r['상단여력(%)'];
      const sc=sg>=30?'neg15':sg>=20?'neg10':sg>=10?'neg5':'pos';
      html+=`<tr>
        <td class="${mc}">${r['시장']}</td><td>${r['종목코드']}</td>
        <td><b>${r['종목명']}</b></td>
        <td>${Number(r['시총(억)']).toLocaleString()}</td>
        <td>${Number(r['종가']).toLocaleString()}</td>
        <td class="${dc}">${r['전일대비(%)']}%</td>
        <td class="${fc}">${fsign}${fd}%</td>
        <td style="color:#fb923c">${Number(r['VL']).toLocaleString()}</td>
        <td class="${rc}" style="font-weight:700">${rv}%</td>
        <td class="${vc}" style="font-weight:700">+${vg}%</td>
        <td style="color:#8b8fa8">${Number(r['엔벨로프상단']).toLocaleString()}</td>
        <td class="${ec}" style="font-weight:700">+${eg}%</td>
        <td class="${sc}">+${sg}%</td>
        <td style="color:#c084fc">${Number(r['세력평단']).toLocaleString()}</td>
        <td style="color:#f87171;font-weight:700">${r['세력평단변화(%)']}%</td>
      </tr>`;
    });
    html+='</table></div>';
    document.getElementById('tables').innerHTML=html;
    return;
  }

  // 조건10 엔벨로프눌림 (엔벨로프상단 컬럼 존재 여부로 판별)
  if(rows.length && '엔벨로프상단' in rows[0]){
    let html=`<div class="tbl-wrap"><table>
    <tr><th>시장</th><th>종목코드</th><th>종목명</th><th>시총(억)</th>
        <th>종가</th><th>전일대비(%)</th><th>VL</th><th>VL대비(%)</th>
        <th>엔벨로프상단</th><th>엔벨로프↑VL(%)</th></tr>`;
    rows.forEach(r=>{
      const mc=r['시장'].includes('KOSDAQ')?'mkt-kosdaq':r['시장'].includes('KONEX')?'mkt-konex':'mkt-kospi';
      const dc=r['전일대비(%)']<0?'neg10':'pos';
      const vg=r['VL대비(%)'];
      const vc=vg>=0?'pos':'neg5';
      const vsign=vg>=0?'+':'';
      const eg=r['엔벨로프VL차(%)'];
      // 엔벨로프가 VL보다 많이 위일수록 강세 여력 큼
      const ec=eg>=30?'neg15':eg>=20?'neg10':eg>=10?'neg5':'pos';
      html+=`<tr>
        <td class="${mc}">${r['시장']}</td><td>${r['종목코드']}</td>
        <td><b>${r['종목명']}</b></td>
        <td>${Number(r['시총(억)']).toLocaleString()}</td>
        <td>${Number(r['종가']).toLocaleString()}</td>
        <td class="${dc}">${r['전일대비(%)']}%</td>
        <td style="color:#f472b6">${Number(r['VL']).toLocaleString()}</td>
        <td class="${vc}" style="font-weight:700">${vsign}${vg}%</td>
        <td style="color:#8b8fa8">${Number(r['엔벨로프상단']).toLocaleString()}</td>
        <td class="${ec}" style="font-weight:700">+${eg}%</td>
      </tr>`;
    });
    html+='</table></div>';
    document.getElementById('tables').innerHTML=html;
    return;
  }

  // 조건9 비율골든크로스 (누적비율 컬럼 존재 여부로 판별)
  if(rows.length && '누적비율' in rows[0]){
    let html=`<div class="tbl-wrap"><table>
    <tr><th>시장</th><th>종목코드</th><th>종목명</th><th>시총(억)</th>
        <th>종가</th><th>전일대비(%)</th><th>누적비율</th><th>90일비율</th><th>기준봉</th></tr>`;
    rows.forEach(r=>{
      const mc=r['시장'].includes('KOSDAQ')?'mkt-kosdaq':r['시장'].includes('KONEX')?'mkt-konex':'mkt-kospi';
      const dc=r['전일대비(%)']<0?'neg10':'pos';
      const rc=r['누적비율']>=3?'neg15':r['누적비율']>=2?'neg10':r['누적비율']>=1.5?'neg5':'pos';
      const diff=round2(r['누적비율']-r['90일비율']);
      html+=`<tr>
        <td class="${mc}">${r['시장']}</td><td>${r['종목코드']}</td>
        <td><b>${r['종목명']}</b></td>
        <td>${Number(r['시총(억)']).toLocaleString()}</td>
        <td>${Number(r['종가']).toLocaleString()}</td>
        <td class="${dc}">${r['전일대비(%)']}%</td>
        <td class="${rc}" style="font-weight:700">${r['누적비율']} <small style="color:#8b8fa8">(+${diff})</small></td>
        <td style="color:#8b8fa8">${r['90일비율']}</td>
        <td style="color:#10b981">${r['기준봉']}</td>
      </tr>`;
    });
    html+='</table></div>';
    document.getElementById('tables').innerHTML=html;
    return;
  }

  // 조건8 하이킨아시 주봉 (저항선대비(%) 컬럼 존재 여부로 판별)
  if(rows.length && '저항선대비(%)' in rows[0]){
    let html=`<div class="tbl-wrap"><table>
    <tr><th>시장</th><th>종목코드</th><th>종목명</th><th>시총(억)</th>
        <th>종가</th><th>전일대비(%)</th><th>저항선</th><th>저항선대비(%)</th></tr>`;
    rows.forEach(r=>{
      const mc=r['시장'].includes('KOSDAQ')?'mkt-kosdaq':r['시장'].includes('KONEX')?'mkt-konex':'mkt-kospi';
      const dc=r['전일대비(%)']<0?'neg10':'pos';
      const rg=r['저항선대비(%)'];
      const rc=rg>=10?'neg15':rg>=5?'neg10':rg>=2?'neg5':'pos';
      html+=`<tr>
        <td class="${mc}">${r['시장']}</td><td>${r['종목코드']}</td>
        <td><b>${r['종목명']}</b></td>
        <td>${Number(r['시총(억)']).toLocaleString()}</td>
        <td>${Number(r['종가']).toLocaleString()}</td>
        <td class="${dc}">${r['전일대비(%)']}%</td>
        <td style="color:#06b6d4">${Number(r['저항선']).toLocaleString()}</td>
        <td class="${rc}" style="font-weight:700">+${rg}%</td>
      </tr>`;
    });
    html+='</table></div>';
    document.getElementById('tables').innerHTML=html;
    return;
  }

  // 조건37 거래대금RSI (신호(RSI) 컬럼 존재 여부로 판별 — 조건31보다 먼저)
  if(rows.length && '신호(RSI)' in rows[0]){
    let html=`<div class="tbl-wrap"><table>
    <tr><th>시장</th><th>종목코드</th><th>종목명</th><th>시총(억)</th>
        <th>종가</th><th>전일대비(%)</th>
        <th>거래대금(억)</th><th>RSI중심선</th><th>RSI밴드상단</th><th>신호(RSI)</th></tr>`;
    rows.forEach(r=>{
      const mc=r['시장'].includes('KOSDAQ')?'mkt-kosdaq':r['시장'].includes('KONEX')?'mkt-konex':'mkt-kospi';
      const dc=r['전일대비(%)']<0?'neg10':'pos';
      const sig=r['신호(RSI)'];
      const sc=sig.includes('상단돌파')&&sig.includes('중심선')? 'neg15':
               sig.includes('상단돌파')? 'neg15':
               sig.includes('중심선')? 'neg10': '';
      const tv=r['거래대금(억)'];
      const tc=tv>=5000?'neg15':tv>=1000?'neg10':tv>=300?'neg5':'';
      html+=`<tr>
        <td class="${mc}">${r['시장']}</td><td>${r['종목코드']}</td>
        <td><b>${r['종목명']}</b></td>
        <td>${Number(r['시총(억)']).toLocaleString()}</td>
        <td>${Number(r['종가']).toLocaleString()}</td>
        <td class="${dc}">${r['전일대비(%)']}%</td>
        <td class="${tc}" style="font-weight:700">${tv.toLocaleString()}</td>
        <td style="color:#8b8fa8">${Number(r['RSI중심선']).toLocaleString()}</td>
        <td style="color:#34d399">${Number(r['RSI밴드상단']).toLocaleString()}</td>
        <td class="${sc}" style="font-weight:700">${sig}</td>
      </tr>`;
    });
    html+='</table></div>';
    document.getElementById('tables').innerHTML=html;
    return;
  }

  // 조건44 Market shift levels (HMA레벨 컬럼 존재 여부로 판별)
  if(rows.length && 'HMA레벨' in rows[0]){
    let html=`<div class="tbl-wrap"><table>
    <tr><th>시장</th><th>종목코드</th><th>종목명</th><th>시총(억)</th>
        <th>종가</th><th>전일대비(%)</th>
        <th>HMA</th><th>HMA레벨</th><th>HMA방향</th><th>신호</th></tr>`;
    rows.forEach(r=>{
      const mc=r['시장'].includes('KOSDAQ')?'mkt-kosdaq':r['시장'].includes('KONEX')?'mkt-konex':'mkt-kospi';
      const dc=r['전일대비(%)']<0?'neg10':'pos';
      const sig=r['신호'];
      const sc=sig.includes('레벨돌파')||sig.includes('상향돌파')||sig.includes('상승반전')?'neg15':
               sig.includes('하락반전')||sig.includes('하향이탈')?'neg10':
               sig.includes('레벨위')?'pos':'';
      const hd=r['HMA방향'];
      const hdStyle=hd==='↑'?'style="color:#4ade80;font-weight:700"':'style="color:#f87171;font-weight:700"';
      html+=`<tr>
        <td class="${mc}">${r['시장']}</td><td>${r['종목코드']}</td>
        <td><b>${r['종목명']}</b></td>
        <td>${Number(r['시총(억)']).toLocaleString()}</td>
        <td>${Number(r['종가']).toLocaleString()}</td>
        <td class="${dc}">${r['전일대비(%)']}%</td>
        <td style="color:#8b8fa8">${Number(r['HMA']).toLocaleString()}</td>
        <td style="color:#10b981;font-weight:700">${Number(r['HMA레벨']).toLocaleString()}</td>
        <td ${hdStyle}>${hd}</td>
        <td class="${sc}" style="font-weight:700">${sig}</td>
      </tr>`;
    });
    html+='</table></div>';
    document.getElementById('tables').innerHTML=html;
    return;
  }

  // 조건46 MSL2 (MSL2레벨 컬럼 존재 여부로 판별)
  if(rows.length && 'MSL2레벨' in rows[0]){
    let html=`<div class="tbl-wrap"><table>
    <tr><th>시장</th><th>종목코드</th><th>종목명</th><th>시총(억)</th>
        <th>종가</th><th>전일대비(%)</th>
        <th>HMA</th><th>MSL2레벨</th><th>HMA방향</th><th>VL</th><th>신호</th></tr>`;
    rows.forEach(r=>{
      const mc=r['시장'].includes('KOSDAQ')?'mkt-kosdaq':r['시장'].includes('KONEX')?'mkt-konex':'mkt-kospi';
      const dc=r['전일대비(%)']<0?'neg10':'pos';
      const hd=r['HMA방향'];
      const hdStyle=hd==='↑'?'style="color:#4ade80;font-weight:700"':'style="color:#f87171;font-weight:700"';
      html+=`<tr>
        <td class="${mc}">${r['시장']}</td><td>${r['종목코드']}</td>
        <td><b>${r['종목명']}</b></td>
        <td>${Number(r['시총(억)']).toLocaleString()}</td>
        <td>${Number(r['종가']).toLocaleString()}</td>
        <td class="${dc}">${r['전일대비(%)']}%</td>
        <td style="color:#8b8fa8">${Number(r['HMA']).toLocaleString()}</td>
        <td style="color:#f59e0b;font-weight:700">${Number(r['MSL2레벨']).toLocaleString()}</td>
        <td ${hdStyle}>${hd}</td>
        <td style="color:#a78bfa">${Number(r['VL']).toFixed(2)}</td>
        <td class="neg15" style="font-weight:700">${r['신호']}</td>
      </tr>`;
    });
    html+='</table></div>';
    document.getElementById('tables').innerHTML=html;
    return;
  }

  // 조건45 QQE (QQEF 컬럼 존재 여부로 판별)
  if(rows.length && 'QQEF' in rows[0]){
    let html=`<div class="tbl-wrap"><table>
    <tr><th>시장</th><th>종목코드</th><th>종목명</th><th>시총(억)</th>
        <th>종가</th><th>전일대비(%)</th>
        <th>QQEF</th><th>QQES</th><th>VL</th><th>신호</th></tr>`;
    rows.forEach(r=>{
      const mc=r['시장'].includes('KOSDAQ')?'mkt-kosdaq':r['시장'].includes('KONEX')?'mkt-konex':'mkt-kospi';
      const dc=r['전일대비(%)']<0?'neg10':'pos';
      const qqef=Number(r['QQEF']);
      const qqes=Number(r['QQES']);
      const aboveQqes=qqef>qqes;
      const qqefStyle=aboveQqes?'style="color:#4ade80;font-weight:700"':'style="color:#f87171;font-weight:700"';
      const vlVal=Number(r['VL']);
      html+=`<tr>
        <td class="${mc}">${r['시장']}</td><td>${r['종목코드']}</td>
        <td><b>${r['종목명']}</b></td>
        <td>${Number(r['시총(억)']).toLocaleString()}</td>
        <td>${Number(r['종가']).toLocaleString()}</td>
        <td class="${dc}">${r['전일대비(%)']}%</td>
        <td ${qqefStyle}>${qqef.toFixed(2)}</td>
        <td style="color:#8b8fa8">${qqes.toFixed(2)}</td>
        <td style="color:#a78bfa">${vlVal.toFixed(2)}</td>
        <td class="neg15" style="font-weight:700">${r['신호']}</td>
      </tr>`;
    });
    html+='</table></div>';
    document.getElementById('tables').innerHTML=html;
    return;
  }

  // 조건31 pro RSI (RSI밴드하단 컬럼 존재 여부로 판별)
  if(rows.length && 'RSI밴드하단' in rows[0]){
    let html=`<div class="tbl-wrap"><table>
    <tr><th>시장</th><th>종목코드</th><th>종목명</th><th>시총(억)</th>
        <th>종가</th><th>전일대비(%)</th>
        <th>RSI밴드하단</th><th>RSI중심선</th><th>RSI밴드상단</th><th>신호</th></tr>`;
    rows.forEach(r=>{
      const mc=r['시장'].includes('KOSDAQ')?'mkt-kosdaq':r['시장'].includes('KONEX')?'mkt-konex':'mkt-kospi';
      const dc=r['전일대비(%)']<0?'neg10':'pos';
      const sig=r['신호'];
      const sc=sig.includes('돌파↑')?'neg15':sig.includes('이탈↓')?'neg10':
               sig.includes('과매도')||sig.includes('약세')?'neg5':
               sig.includes('과매수')||sig.includes('강세')?'pos':'';
      html+=`<tr>
        <td class="${mc}">${r['시장']}</td><td>${r['종목코드']}</td>
        <td><b>${r['종목명']}</b></td>
        <td>${Number(r['시총(억)']).toLocaleString()}</td>
        <td>${Number(r['종가']).toLocaleString()}</td>
        <td class="${dc}">${r['전일대비(%)']}%</td>
        <td style="color:#f87171">${Number(r['RSI밴드하단']).toLocaleString()}</td>
        <td style="color:#8b8fa8">${Number(r['RSI중심선']).toLocaleString()}</td>
        <td style="color:#34d399">${Number(r['RSI밴드상단']).toLocaleString()}</td>
        <td class="${sc}" style="font-weight:700">${sig}</td>
      </tr>`;
    });
    html+='</table></div>';
    document.getElementById('tables').innerHTML=html;
    return;
  }

  // 조건34 과열스코어순위(일봉) (구분 컬럼 존재 여부로 판별)
  if(rows.length && '구분' in rows[0]){
    const rows1=rows.filter(r=>r['구분']==='전일비교');
    const rows2=rows.filter(r=>r['구분']==='2일전비교');
    const makeTable34 = (title, subRows, scoreKey, prevKey) => {
      let t=`<div style="font-size:.9rem;color:#fb923c;font-weight:700;margin:18px 0 8px">
        🏆 ${title} — 상위 ${subRows.length}종목</div>
      <div class="tbl-wrap"><table>
      <tr><th>순위</th><th>시장</th><th>종목코드</th><th>종목명</th><th>시총(억)</th>
          <th>종가</th><th>전일대비(%)</th>
          <th>과열스코어</th><th>${prevKey}</th><th>상승점수</th>
          <th>ATR비율</th><th>거래량비율</th><th>이격도(20)</th></tr>`;
      subRows.forEach((r,i)=>{
        const mc=r['시장'].includes('KOSDAQ')?'mkt-kosdaq':r['시장'].includes('KONEX')?'mkt-konex':'mkt-kospi';
        const dc=r['전일대비(%)']<0?'neg10':'pos';
        const sc=r['과열스코어'];
        const scc=sc>=8?'neg15':sc>=5?'neg10':'neg5';
        const ds=r[scoreKey];
        const dsc=ds>=3?'neg15':ds>=1.5?'neg10':'neg5';
        const rank=i===0?'🥇':i===1?'🥈':i===2?'🥉':`${i+1}위`;
        t+=`<tr>
          <td style="font-weight:700;text-align:center">${rank}</td>
          <td class="${mc}">${r['시장']}</td><td>${r['종목코드']}</td>
          <td><b>${r['종목명']}</b></td>
          <td>${Number(r['시총(억)']).toLocaleString()}</td>
          <td>${Number(r['종가']).toLocaleString()}</td>
          <td class="${dc}">${r['전일대비(%)']}%</td>
          <td class="${scc}" style="font-weight:700">${sc}</td>
          <td style="color:#8b8fa8">${r[prevKey]}</td>
          <td class="${dsc}" style="font-weight:700">▲${ds}</td>
          <td style="color:#94a3b8">${r['ATR비율']}</td>
          <td style="color:#94a3b8">${r['거래량비율']}</td>
          <td style="color:#94a3b8">${r['이격도(20)']}%</td>
        </tr>`;
      });
      t+='</table></div>';
      return t;
    };
    const rows3=rows.filter(r=>r['구분']==='엔벨미초과');
    let html='';
    html+=makeTable34('결과1 — 전일 대비 스코어 상승폭', rows1, '상승점수(1일)', '전일스코어');
    html+=`<div style="border-top:1px solid #2a2d3a;margin:24px 0"></div>`;
    html+=makeTable34('결과2 — 2일전 대비 스코어 상승폭', rows2, '상승점수(2일)', '2일전스코어');
    if(rows3.length){
      html+=`<div style="border-top:1px solid #2a2d3a;margin:24px 0"></div>`;
      html+=`<div style="font-size:.9rem;color:#34d399;font-weight:700;margin:18px 0 8px">
        🟢 결과3 — Envelope(20,40%) 미초과 종목 (결과1·2 교집합 후보) ${rows3.length}종목</div>
      <div class="tbl-wrap"><table>
      <tr><th>순위</th><th>포함</th><th>시장</th><th>종목코드</th><th>종목명</th><th>시총(억)</th>
          <th>종가</th><th>전일대비(%)</th>
          <th>과열스코어</th><th>상승점수(1일)</th><th>상승점수(2일)</th>
          <th>엔벨상단</th><th>이격도(20)</th></tr>`;
      rows3.forEach((r,i)=>{
        const mc=r['시장'].includes('KOSDAQ')?'mkt-kosdaq':r['시장'].includes('KONEX')?'mkt-konex':'mkt-kospi';
        const dc=r['전일대비(%)']<0?'neg10':'pos';
        const sc=r['과열스코어'];
        const scc=sc>=8?'neg15':sc>=5?'neg10':'neg5';
        const rank=i===0?'🥇':i===1?'🥈':i===2?'🥉':`${i+1}위`;
        const lbl=r['포함결과']||'';
        const lblc=lbl==='R1+R2'?'color:#f43f5e':lbl==='R1'?'color:#fb923c':'color:#fbbf24';
        html+=`<tr>
          <td style="font-weight:700;text-align:center">${rank}</td>
          <td style="${lblc};font-weight:700;text-align:center">${lbl}</td>
          <td class="${mc}">${r['시장']}</td><td>${r['종목코드']}</td>
          <td><b>${r['종목명']}</b></td>
          <td>${Number(r['시총(억)']).toLocaleString()}</td>
          <td>${Number(r['종가']).toLocaleString()}</td>
          <td class="${dc}">${r['전일대비(%)']}%</td>
          <td class="${scc}" style="font-weight:700">${sc}</td>
          <td style="color:#fb923c">${r['상승점수(1일)']}</td>
          <td style="color:#fbbf24">${r['상승점수(2일)']}</td>
          <td style="color:#94a3b8">${Number(r['엔벨상단']).toLocaleString()}</td>
          <td style="color:#94a3b8">${r['이격도(20)']}%</td>
        </tr>`;
      });
      html+='</table></div>';
    }
    document.getElementById('tables').innerHTML=html;
    return;
  }

  // 조건33 과열스코어(주봉) (과열스코어 컬럼 존재 여부로 판별)
  if(rows.length && '과열스코어' in rows[0]){
    let html=`<div class="tbl-wrap"><table>
    <tr><th>시장</th><th>종목코드</th><th>종목명</th><th>시총(억)</th>
        <th>종가</th><th>전일대비(%)</th>
        <th>과열스코어</th><th>전주스코어</th><th>ATR비율(주)</th><th>거래량비율(주)</th><th>이격도(20w)</th></tr>`;
    rows.forEach(r=>{
      const mc=r['시장'].includes('KOSDAQ')?'mkt-kosdaq':r['시장'].includes('KONEX')?'mkt-konex':'mkt-kospi';
      const dc=r['전일대비(%)']<0?'neg10':'pos';
      const sc=r['과열스코어'];
      const scc=sc>=8?'neg15':sc>=5?'neg10':'neg5';
      const dp=r['이격도(20w)'];
      const dpc=dp>=120?'neg15':dp>=110?'neg10':'neg5';
      html+=`<tr>
        <td class="${mc}">${r['시장']}</td><td>${r['종목코드']}</td>
        <td><b>${r['종목명']}</b></td>
        <td>${Number(r['시총(억)']).toLocaleString()}</td>
        <td>${Number(r['종가']).toLocaleString()}</td>
        <td class="${dc}">${r['전일대비(%)']}%</td>
        <td class="${scc}" style="font-weight:700">${sc}</td>
        <td style="color:#8b8fa8">${r['전주스코어']}</td>
        <td style="color:#94a3b8">${r['ATR비율(주)']}</td>
        <td style="color:#94a3b8">${r['거래량비율(주)']}</td>
        <td class="${dpc}">${dp}%</td>
      </tr>`;
    });
    html+='</table></div>';
    document.getElementById('tables').innerHTML=html;
    return;
  }

  // 조건35 거래대금순위 (거래대금(억) 컬럼 존재 여부로 판별)
  if(rows.length && '거래대금(억)' in rows[0]){
    let html=`<div style="font-size:.85rem;color:#06b6d4;font-weight:700;margin-bottom:10px">
      💰 거래대금 TOP10 — 거래량×(O+H+L+C)/4 기준</div>
    <div class="tbl-wrap"><table>
    <tr><th>순위</th><th>시장</th><th>종목코드</th><th>종목명</th><th>시총(억)</th>
        <th>종가</th><th>전일대비(%)</th>
        <th>거래대금(억)</th><th>거래량</th><th>평균단가</th></tr>`;
    rows.forEach((r,i)=>{
      const mc=r['시장'].includes('KOSDAQ')?'mkt-kosdaq':r['시장'].includes('KONEX')?'mkt-konex':'mkt-kospi';
      const dc=r['전일대비(%)']<0?'neg10':'pos';
      const tv=r['거래대금(억)'];
      const tvc=tv>=5000?'neg15':tv>=2000?'neg10':'neg5';
      const rank=i===0?'🥇':i===1?'🥈':i===2?'🥉':`${i+1}위`;
      html+=`<tr>
        <td style="font-weight:700;text-align:center">${rank}</td>
        <td class="${mc}">${r['시장']}</td><td>${r['종목코드']}</td>
        <td><b>${r['종목명']}</b></td>
        <td>${Number(r['시총(억)']).toLocaleString()}</td>
        <td>${Number(r['종가']).toLocaleString()}</td>
        <td class="${dc}">${r['전일대비(%)']}%</td>
        <td class="${tvc}" style="font-weight:700">${Number(r['거래대금(억)']).toLocaleString()}억</td>
        <td style="color:#94a3b8">${Number(r['거래량']).toLocaleString()}</td>
        <td style="color:#8b8fa8">${Number(r['평균단가']).toLocaleString()}</td>
      </tr>`;
    });
    html+='</table></div>';
    document.getElementById('tables').innerHTML=html;
    return;
  }

  // 조건32 캔들볼륨저항 (캔들저항선 컬럼 존재 여부로 판별)
  if(rows.length && '캔들저항선' in rows[0]){
    let html=`<div class="tbl-wrap"><table>
    <tr><th>시장</th><th>종목코드</th><th>종목명</th><th>시총(억)</th>
        <th>종가</th><th>전일대비(%)</th>
        <th>캔들저항선</th><th>저항대비(%)</th><th>저항봉날짜</th><th>ADX(11)</th></tr>`;
    rows.forEach(r=>{
      const mc=r['시장'].includes('KOSDAQ')?'mkt-kosdaq':r['시장'].includes('KONEX')?'mkt-konex':'mkt-kospi';
      const dc=r['전일대비(%)']<0?'neg10':'pos';
      const rp=r['저항대비(%)'];
      const rpc=rp>=10?'neg15':rp>=5?'neg10':'pos';
      const adx=r['ADX(11)'];
      const adxc=adx>=40?'neg15':adx>=30?'neg10':'pos';
      html+=`<tr>
        <td class="${mc}">${r['시장']}</td><td>${r['종목코드']}</td>
        <td><b>${r['종목명']}</b></td>
        <td>${Number(r['시총(억)']).toLocaleString()}</td>
        <td>${Number(r['종가']).toLocaleString()}</td>
        <td class="${dc}">${r['전일대비(%)']}%</td>
        <td style="color:#f97316;font-weight:700">${Number(r['캔들저항선']).toLocaleString()}</td>
        <td class="${rpc}" style="font-weight:700">+${rp}%</td>
        <td style="color:#94a3b8">${r['저항봉날짜']}</td>
        <td class="${adxc}" style="font-weight:700">${adx}</td>
      </tr>`;
    });
    html+='</table></div>';
    document.getElementById('tables').innerHTML=html;
    return;
  }

  // 조건30 VL급반등 (VL낙폭(%) 컬럼 존재 여부로 판별)
  if(rows.length && 'VL낙폭(%)' in rows[0]){
    let html=`<div class="tbl-wrap"><table>
    <tr><th>시장</th><th>종목코드</th><th>종목명</th><th>시총(억)</th>
        <th>종가</th><th>전일대비(%)</th><th>VL</th>
        <th>VL일간상승(%)</th><th>3일누적상승(%)</th><th>VL낙폭(%)</th></tr>`;
    rows.forEach(r=>{
      const mc=r['시장'].includes('KOSDAQ')?'mkt-kosdaq':r['시장'].includes('KONEX')?'mkt-konex':'mkt-kospi';
      const dc=r['전일대비(%)']<0?'neg10':'pos';
      const rd=r['VL낙폭(%)'];
      const rc=rd>=70?'neg15':rd>=55?'neg10':'neg5';
      const ri=r['VL일간상승(%)'];
      const ric=ri>=8?'neg15':ri>=6?'neg10':'neg5';
      const rt=r['3일누적상승(%)'];
      const rtc=rt>=20?'neg15':rt>=14?'neg10':'neg5';
      html+=`<tr>
        <td class="${mc}">${r['시장']}</td><td>${r['종목코드']}</td>
        <td><b>${r['종목명']}</b></td>
        <td>${Number(r['시총(억)']).toLocaleString()}</td>
        <td>${Number(r['종가']).toLocaleString()}</td>
        <td class="${dc}">${r['전일대비(%)']}%</td>
        <td style="color:#fb923c">${Number(r['VL']).toLocaleString()}</td>
        <td class="${ric}" style="font-weight:700">+${ri}%</td>
        <td class="${rtc}" style="font-weight:700">+${rt}%</td>
        <td class="${rc}" style="font-weight:700">${rd}%↓</td>
      </tr>`;
    });
    html+='</table></div>';
    document.getElementById('tables').innerHTML=html;
    return;
  }

  // 조건51 김승태타점4 (SMA60 컬럼 존재 여부로 판별)
  if(rows.length && 'SMA60' in rows[0]){
    let html=`<div class="tbl-wrap"><table>
    <tr><th>시장</th><th>종목코드</th><th>종목명</th><th>시총(억)</th>
        <th>종가</th><th>전일대비(%)</th>
        <th>SMA5</th><th>SMA20</th><th>SMA60</th>
        <th>5일평균거래량</th><th>신호</th></tr>`;
    rows.forEach(r=>{
      const mc=r['시장'].includes('KOSDAQ')?'mkt-kosdaq':r['시장'].includes('KONEX')?'mkt-konex':'mkt-kospi';
      const dc=r['전일대비(%)']<0?'neg10':'pos';
      html+=`<tr>
        <td class="${mc}">${r['시장']}</td><td>${r['종목코드']}</td>
        <td><b>${r['종목명']}</b></td>
        <td>${Number(r['시총(억)']).toLocaleString()}</td>
        <td>${Number(r['종가']).toLocaleString()}</td>
        <td class="${dc}">${r['전일대비(%)']}%</td>
        <td style="color:#60a5fa">${Number(r['SMA5']).toLocaleString()}</td>
        <td style="color:#a78bfa">${Number(r['SMA20']).toLocaleString()}</td>
        <td style="color:#34d399">${Number(r['SMA60']).toLocaleString()}</td>
        <td style="color:#8b8fa8">${Number(r['5일평균거래량']).toLocaleString()}주</td>
        <td class="neg15" style="font-weight:700">${r['신호']}</td>
      </tr>`;
    });
    html+='</table></div>';
    document.getElementById('tables').innerHTML=html;
    return;
  }

  // 조건50 김승태타점 (120봉신고가(봉전) 컬럼 존재 여부로 판별)
  if(rows.length && '120봉신고가(봉전)' in rows[0]){
    let html=`<div class="tbl-wrap"><table>
    <tr><th>시장</th><th>종목코드</th><th>종목명</th><th>시총(억)</th>
        <th>종가</th><th>전일대비(%)</th>
        <th>SMA5</th><th>SMA200</th>
        <th>120봉신고가(봉전)</th><th>전일저가대비(%)</th>
        <th>5일평균거래량</th><th>신호</th></tr>`;
    rows.forEach(r=>{
      const mc=r['시장'].includes('KOSDAQ')?'mkt-kosdaq':r['시장'].includes('KONEX')?'mkt-konex':'mkt-kospi';
      const dc=r['전일대비(%)']<0?'neg10':'pos';
      const ba=Number(r['120봉신고가(봉전)']);
      const bac=ba<=5?'neg15':ba<=10?'neg10':'neg5';
      const lp=Number(r['전일저가대비(%)']);
      const lpc=lp>=10?'neg15':lp>=7?'neg10':'neg5';
      html+=`<tr>
        <td class="${mc}">${r['시장']}</td><td>${r['종목코드']}</td>
        <td><b>${r['종목명']}</b></td>
        <td>${Number(r['시총(억)']).toLocaleString()}</td>
        <td>${Number(r['종가']).toLocaleString()}</td>
        <td class="${dc}">${r['전일대비(%)']}%</td>
        <td style="color:#60a5fa">${Number(r['SMA5']).toLocaleString()}</td>
        <td style="color:#34d399">${Number(r['SMA200']).toLocaleString()}</td>
        <td class="${bac}" style="font-weight:700">${ba}봉전</td>
        <td class="${lpc}" style="font-weight:700">+${lp}%</td>
        <td style="color:#8b8fa8">${Number(r['5일평균거래량']).toLocaleString()}주</td>
        <td class="neg15" style="font-weight:700">${r['신호']}</td>
      </tr>`;
    });
    html+='</table></div>';
    document.getElementById('tables').innerHTML=html;
    return;
  }

  // 조건49 박문환원인점 (원인점날짜 컬럼 존재 여부로 판별)
  if(rows.length && '원인점날짜' in rows[0]){
    let html=`<div class="tbl-wrap"><table>
    <tr><th>시장</th><th>종목코드</th><th>종목명</th><th>시총(억)</th>
        <th>종가</th><th>전일대비(%)</th>
        <th>원인점(원)</th><th>원인점날짜</th><th>5일선</th>
        <th>돌파율(%)</th><th>경과일</th>
        <th>거래량비율(%)</th><th>5일평균거래량</th><th>신호</th></tr>`;
    rows.forEach(r=>{
      const mc=r['시장'].includes('KOSDAQ')?'mkt-kosdaq':r['시장'].includes('KONEX')?'mkt-konex':'mkt-kospi';
      const dc=r['전일대비(%)']<0?'neg10':'pos';
      const bp=Number(r['돌파율(%)']);
      const bpc=bp<=1?'neg15':bp<=3?'neg10':'neg5';
      const vr=Number(r['거래량비율(%)']);
      const vrc=vr>=500?'neg15':vr>=300?'neg10':'neg5';
      html+=`<tr>
        <td class="${mc}">${r['시장']}</td><td>${r['종목코드']}</td>
        <td><b>${r['종목명']}</b></td>
        <td>${Number(r['시총(억)']).toLocaleString()}</td>
        <td>${Number(r['종가']).toLocaleString()}</td>
        <td class="${dc}">${r['전일대비(%)']}%</td>
        <td style="color:#f472b6;font-weight:700">${Number(r['원인점']).toLocaleString()}</td>
        <td style="color:#8b8fa8">${r['원인점날짜']}</td>
        <td style="color:#60a5fa">${Number(r['5일선']).toLocaleString()}</td>
        <td class="${bpc}" style="font-weight:700">+${bp}%</td>
        <td style="color:#8b8fa8">${r['경과일']}일</td>
        <td class="${vrc}" style="font-weight:700">${vr}%</td>
        <td style="color:#8b8fa8">${Number(r['5일평균거래량']).toLocaleString()}주</td>
        <td class="neg15" style="font-weight:700">${r['신호']}</td>
      </tr>`;
    });
    html+='</table></div>';
    document.getElementById('tables').innerHTML=html;
    return;
  }

  // 조건1·2 공통 렌더러 (구간별 테이블)
  const zones=['-5%~-10%','-10%~-15%','-15% 이하'];
  const zColors=['neg5','neg10','neg15'];
  const counts=zones.map(z=>rows.filter(r=>r['구간']===z).length);
  document.getElementById('summary').innerHTML+=
    zones.map((z,i)=>counts[i]>0?`<span class="badge b3">${z}: ${counts[i]}개</span>`:'').join('');

  let html='';
  zones.forEach((zone,zi)=>{
    const sub=rows.filter(r=>r['구간']===zone);
    if(!sub.length) return;
    const zoneColor=['#fbbf24','#f97316','#ef4444'][zi];
    html+=`<div class="zone-title" style="color:${zoneColor}">${zone} (${sub.length}개)</div>
    <div class="tbl-wrap"><table>
    <tr><th>시장</th><th>종목코드</th><th>종목명</th><th>시총(억)</th>
        <th>종가</th><th>전일대비(%)</th><th>VL</th><th>VL대비(%)</th><th>EMA60</th><th>EMA200</th></tr>`;
    sub.forEach(r=>{
      const mc=r['시장'].includes('KOSDAQ')?'mkt-kosdaq':r['시장'].includes('KONEX')?'mkt-konex':'mkt-kospi';
      const dc=r['전일대비(%)']<=-8?'neg15':r['전일대비(%)']<0?'neg10':'pos';
      html+=`<tr>
        <td class="${mc}">${r['시장']}</td><td>${r['종목코드']}</td>
        <td><b>${r['종목명']}</b></td>
        <td>${Number(r['시총(억)']).toLocaleString()}</td>
        <td>${Number(r['종가']).toLocaleString()}</td>
        <td class="${dc}">${r['전일대비(%)']}%</td>
        <td>${Number(r['VL']).toLocaleString()}</td>
        <td class="${zColors[zi]}">${r['VL대비(%)']}%</td>
        <td>${Number(r['EMA60']).toLocaleString()}</td>
        <td>${Number(r['EMA200']).toLocaleString()}</td>
      </tr>`;
    });
    html+='</table></div>';
  });
  document.getElementById('tables').innerHTML=html;
}

function dlCsv(){window.location.href=`/api/${SID}/download`;}
function showMsg(m){document.getElementById('msg').textContent=m;}
function resetBtn(){document.getElementById('runBtn').disabled=false;}
function round2(v){return Math.round(v*100)/100;}
{{EXTRA_JS}}
</script>
</body></html>
"""


# ══════════════════════════════════════════════════════════════════════════════
# Flask 라우트
# ══════════════════════════════════════════════════════════════════════════════

@app.before_request
def require_login():
    # 로그인 인증 비활성화 — 로컬 전용 모드
    return None


def _login_page(error: str = "") -> str:
    error_html = f'<div class="error">{_html.escape(error)}</div>' if error else ""
    next_url = _html.escape(request.args.get("next", "/"), quote=True)
    return f"""<!doctype html>
<html lang="ko">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <title>로그인</title>
  <style>
    body{{margin:0;min-height:100vh;display:grid;place-items:center;background:#101418;color:#e5e7eb;font-family:Arial,'Noto Sans KR',sans-serif}}
    .box{{width:min(420px,calc(100vw - 32px));background:#171d24;border:1px solid #2d3744;border-radius:8px;padding:28px;box-shadow:0 16px 48px rgba(0,0,0,.35)}}
    h1{{margin:0 0 18px;font-size:24px;letter-spacing:0}}
    label{{display:block;margin:14px 0 6px;color:#aab4c0;font-size:14px}}
    input{{width:100%;box-sizing:border-box;border:1px solid #344151;background:#0f141a;color:#fff;border-radius:6px;padding:12px;font-size:15px}}
    button{{width:100%;margin-top:18px;border:0;border-radius:6px;background:#2563eb;color:white;padding:12px 14px;font-weight:700;cursor:pointer}}
    .error{{margin-bottom:12px;color:#fecaca;background:#431b1b;border:1px solid #7f1d1d;border-radius:6px;padding:10px;font-size:14px}}
  </style>
</head>
<body>
  <form class="box" method="post" action="/login">
    <h1>권한관리자 로그인</h1>
    {error_html}
    <input type="hidden" name="next" value="{next_url}">
    <label for="email">아이디(이메일)</label>
    <input id="email" name="email" type="email" autocomplete="username" required autofocus>
    <label for="password">비밀번호</label>
    <input id="password" name="password" type="password" autocomplete="current-password" required>
    <button type="submit">로그인</button>
  </form>
</body>
</html>"""


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "GET":
        return Response(_login_page(), mimetype="text/html")

    email = request.form.get("email", "")
    password = request.form.get("password", "")
    if not _is_allowed_user(email, password):
        return Response(_login_page("허락된 아이디와 비밀번호만 로그인할 수 있습니다."), mimetype="text/html", status=401)

    session.clear()
    session["logged_in"] = True
    session["email"] = _normalize_login_id(email)
    session["is_admin"] = _is_admin_login(email, password)
    if session["is_admin"]:
        return redirect(url_for("auth_manage"))
    next_url = request.form.get("next") or "/"
    if not next_url.startswith("/") or next_url.startswith("//"):
        next_url = "/"
    return redirect(next_url)


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.route("/auth/manage", methods=["GET", "POST"])
def auth_manage():
    if not session.get("is_admin"):
        return redirect(url_for("login"))

    message = ""
    error = ""
    if request.method == "POST":
        email = _normalize_login_id(request.form.get("email", ""))
        password = request.form.get("password", "")
        if not email or not password:
            error = "아이디와 비밀번호를 모두 입력하세요."
        elif email == AUTH_ADMIN_EMAIL:
            error = "관리자 계정은 여기서 덮어쓸 수 없습니다."
        else:
            users = [u for u in _load_auth_users() if _normalize_login_id(u.get("email", "")) != email]
            users.append({"email": email, "password_hash": generate_password_hash(password)})
            _save_auth_users(users)
            message = f"{email} 로그인을 허락했습니다."

    rows = "".join(
        f"<tr><td>{_html.escape(u.get('email',''))}</td><td>허락됨</td></tr>"
        for u in sorted(_load_auth_users(), key=lambda x: x.get("email", ""))
    )
    if not rows:
        rows = '<tr><td colspan="2">아직 저장된 허락 계정이 없습니다.</td></tr>'
    message_html = f'<div class="ok">{_html.escape(message)}</div>' if message else ""
    error_html = f'<div class="error">{_html.escape(error)}</div>' if error else ""
    return Response(f"""<!doctype html>
<html lang="ko">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <title>권한관리자</title>
  <style>
    body{{margin:0;min-height:100vh;background:#101418;color:#e5e7eb;font-family:Arial,'Noto Sans KR',sans-serif}}
    main{{max-width:720px;margin:40px auto;padding:0 18px}}
    .panel{{background:#171d24;border:1px solid #2d3744;border-radius:8px;padding:24px;margin-bottom:18px}}
    h1{{font-size:24px;margin:0 0 16px;letter-spacing:0}}
    h2{{font-size:18px;margin:0 0 12px;letter-spacing:0}}
    label{{display:block;margin:14px 0 6px;color:#aab4c0;font-size:14px}}
    input{{width:100%;box-sizing:border-box;border:1px solid #344151;background:#0f141a;color:#fff;border-radius:6px;padding:12px;font-size:15px}}
    button,.link{{display:inline-flex;align-items:center;justify-content:center;border:0;border-radius:6px;background:#2563eb;color:white;padding:11px 14px;font-weight:700;text-decoration:none;cursor:pointer}}
    button{{width:100%;margin-top:18px}}
    .top{{display:flex;gap:10px;justify-content:flex-end;margin-bottom:14px}}
    table{{width:100%;border-collapse:collapse;margin-top:10px}}
    th,td{{border-bottom:1px solid #2d3744;text-align:left;padding:10px}}
    th{{color:#aab4c0;font-weight:600}}
    .ok{{margin-bottom:12px;color:#bbf7d0;background:#12331f;border:1px solid #166534;border-radius:6px;padding:10px}}
    .error{{margin-bottom:12px;color:#fecaca;background:#431b1b;border:1px solid #7f1d1d;border-radius:6px;padding:10px}}
  </style>
</head>
<body>
  <main>
    <div class="top">
      <a class="link" href="/">앱으로 이동</a>
      <a class="link" href="/logout">로그아웃</a>
    </div>
    <section class="panel">
      <h1>권한관리자</h1>
      {message_html}
      {error_html}
      <form method="post" action="/auth/manage">
        <label for="email">허락할 아이디(이메일)</label>
        <input id="email" name="email" type="email" autocomplete="username" required>
        <label for="password">허락할 비밀번호</label>
        <input id="password" name="password" type="password" autocomplete="new-password" required>
        <button type="submit">저장하기</button>
      </form>
    </section>
    <section class="panel">
      <h2>허락된 로그인</h2>
      <table>
        <thead><tr><th>아이디</th><th>상태</th></tr></thead>
        <tbody>{rows}</tbody>
      </table>
    </section>
  </main>
</body>
</html>""", mimetype="text/html")


@app.route("/")
def hub():
    items = []
    for sid, info in SCREENERS.items():
        items.append({"id": sid, "live": True,
                      "title": info["title"], "desc": info["desc"],
                      "icon": info["icon"]})
    for i in range(len(SCREENERS)+1, len(SCREENERS)+4):
        items.append({"id": i, "live": False, "title": f"조건{i} 준비 중",
                      "desc": "곧 추가될 예정입니다.", "icon": "🔒"})
    html = HUB_HTML.replace("SCREENER_LIST", json.dumps(items, ensure_ascii=False))
    return Response(html, mimetype="text/html")


COND_TAGS = {
    1: """
        <span class="cond-tag">필터1: 시총 1,500억↑</span>
        <span class="cond-tag">필터2: EMA(200) 상승</span>
        <span class="cond-tag">필터3: 종가 &gt; EMA(60)</span>
        <span class="cond-tag">필터4: VL &gt; 종가 (-5~-15%)</span>
        <span class="cond-tag">필터5: VL(1) &lt; VL</span>
    """,
    2: """
        <span class="cond-tag">조건1 필터 전체 포함</span>
        <span class="cond-tag">필터1: 시총 1,500억↑</span>
        <span class="cond-tag">필터2: EMA(200) 상승</span>
        <span class="cond-tag">필터3: 종가 &gt; EMA(60)</span>
        <span class="cond-tag">필터4: VL &gt; 종가 (-5~-15%)</span>
        <span class="cond-tag">필터5: VL(1) &lt; VL</span>
        <span class="cond-tag" style="color:#ef4444;border-color:#5a2a2a">필터6: 전일대비 -5% 초과 하락</span>
    """,
    3: """
        <span class="cond-tag">필터1: 시총 1,500억↑</span>
        <span class="cond-tag">필터2: 일봉 EMA(200) 상승</span>
        <span class="cond-tag" style="color:#a78bfa;border-color:#3a2a5a">필터3: 주봉 거래량 BB(20,2) 대역폭 = 52주 최솟값</span>
    """,
    4: """
        <span class="cond-tag">필터1: 시총 1,500억↑</span>
        <span class="cond-tag">필터2: 20일 거래량MA 3일 연속↑</span>
        <span class="cond-tag">필터3: EMA(200) 3일 연속↑</span>
        <span class="cond-tag" style="color:#34d399;border-color:#1a4a3a">점수: 가격·거래량 관계 점수화 (최대 3.5점)</span>
    """,
    5: """
        <span class="cond-tag">필터1: 5일 평균거래량 30만주↑ (금일 제외)</span>
        <span class="cond-tag">필터2: VR14 × ATR비율 ≥ 3 (ASGMA점수)</span>
        <span class="cond-tag" style="color:#f59e0b;border-color:#5a3a10">필터3: 최근 15일 내 기준봉 존재 (몸통 12%↑ 또는 진폭 18%↑)</span>
    """,
    6: """
        <span class="cond-tag">조건5 ASGMA 전체 포함</span>
        <span class="cond-tag">필터+: ASGMA점수 ≥ 3.0</span>
        <span class="cond-tag" style="color:#ff6b6b;border-color:#5a2020">필터+: 최근 3일 모두 |등락폭| ≤ ATR(14) × 70%</span>
    """,
    7: """
        <span class="cond-tag">필터1: 5일 평균거래량 30만주↑ (금일 제외)</span>
        <span class="cond-tag">필터2: ASGMA점수 ≤ 1 (눌림 구간)</span>
        <span class="cond-tag">필터3: 최근 15일 내 기준봉 존재</span>
        <span class="cond-tag" style="color:#818cf8;border-color:#2a2a5a">필터4: SMA5 또는 SMA20 상향 돌파</span>
    """,
    8: """
        <span class="cond-tag">필터1: 5일 평균거래량 10만주↑ (금일 제외)</span>
        <span class="cond-tag">필터2: 현재 주봉 양봉 (종가 &gt; 시가)</span>
        <span class="cond-tag" style="color:#06b6d4;border-color:#0a3a4a">필터3: 이전 10주 하이킨아시 음봉 시가 저항선 신규 돌파</span>
        <span class="cond-tag">필터4: 전일 기준 미충족 (신규 돌파만)</span>
    """,
    9: """
        <span class="cond-tag">필터1: 5일 평균거래량 10만주↑ (금일 제외)</span>
        <span class="cond-tag">필터2: 최근 20일 내 기준봉 (진폭 12%↑ · 윗꼬리 6%미만 양봉)</span>
        <span class="cond-tag">필터3: EMA(20) 이격 20% 이내 (과열 제외)</span>
        <span class="cond-tag" style="color:#10b981;border-color:#0a3a20">필터4: 기준봉 이후 누적 양음 거래비율이 90일 평균선 상향 돌파 (골든크로스)</span>
    """,
    10: """
        <span class="cond-tag">필터1: 시총 1,500억↑</span>
        <span class="cond-tag">필터2: 30봉 내 시가→고가 등락률 15%↑ 캔들 존재</span>
        <span class="cond-tag" style="color:#f472b6;border-color:#5a1a3a">필터3: 최근 7거래일 내 Envelope(20, 40%) 상단 터치 / 돌파</span>
        <span class="cond-tag" style="color:#f472b6;border-color:#5a1a3a">필터4: 현재가 VL ±3% 이내 (변동회귀선 근접 눌림)</span>
    """,
    11: """
        <span class="cond-tag">필터1: 시총 1,500억↑</span>
        <span class="cond-tag">필터2: 최근 60일 내 고점 또는 종가가 Envelope(20, 40%) 상단 돌파 이력</span>
        <span class="cond-tag" style="color:#fb923c;border-color:#5a2a10">필터3: (VL4|VL5) &gt; (VL2|VL3) &lt; (VL1|VL) — 확장형 V자 반전 (2~3일 전 저점, 양쪽 고점)</span>
        <span class="cond-tag" style="color:#fb923c;border-color:#5a2a10">필터4: 종가 &gt; VL × 1.10 (VL 대비 10%↑)</span>
        <span class="cond-tag" style="color:#fb923c;border-color:#5a2a10">필터5: 최근 5거래일 종가 변동 −10% ~ +10% 이내 (횡보 눌림)</span>
        <span class="cond-tag" style="color:#fb923c;border-color:#5a2a10">필터6: VL ~ VL(5) 6일간 변동폭 1.5%↑ (V자 진폭 확인)</span>
        <span class="cond-tag" style="color:#c084fc;border-color:#3a1a5a">필터7: 세력평단 전일 대비 하락 (EMA20 of 거래량폭발 양봉 중값)</span>
    """,
    12: """
        <span class="cond-tag">필터1: 시총 1,500억↑</span>
        <span class="cond-tag">A = SMA(RSI(21), 2) · B = SMA(RSI(21), 34) − 1.6185 × σ(34)</span>
        <span class="cond-tag" style="color:#e879f9;border-color:#4a1a5a">필터2: A(4) &gt; B(4) OR A(5) &gt; B(5) — 4~5일 전 A가 B 위</span>
        <span class="cond-tag" style="color:#e879f9;border-color:#4a1a5a">필터3: B(1) &gt; A(1) — 어제 B가 A 위 (일시 눌림)</span>
        <span class="cond-tag" style="color:#e879f9;border-color:#4a1a5a">필터4: A &gt; B — 오늘 A가 B 위 재돌파 (재골든크로스)</span>
    """,
    13: """
        <span class="cond-tag">필터1: 시총 3,000억↑</span>
        <span class="cond-tag" style="color:#34d399;border-color:#0a3a20">필터2: Envelope(20,40%) 상단이 30일 전 대비 5% 이하 상승 (횡보 또는 하락 구간)</span>
        <span class="cond-tag" style="color:#34d399;border-color:#0a3a20">필터3: 최근 10일 내 고가가 Envelope 상단 3% 이내 접근 또는 돌파</span>
    """,
    14: """
        <span class="cond-tag">필터1: 시총 3,000억↑</span>
        <span class="cond-tag">세력20평단 = EMA(캔들, 20) · 캔들 = V &gt; 1.5×MA(V,60) and C&gt;O 일 때 (C+O)/2, 아니면 마지막 유효값 유지</span>
        <span class="cond-tag" style="color:#f59e0b;border-color:#5a3a00">필터2: 오늘 종가 &gt; 세력20평단 (신규 돌파 — 어제는 이하)</span>
        <span class="cond-tag" style="color:#f59e0b;border-color:#5a3a00">필터3: 전일 대비 거래량 200%↑ (2배 이상)</span>
    """,
    30: """
        <span class="cond-tag">필터1: 시총 1,500억↑ · ETF/ETN 제외</span>
        <span class="cond-tag">VL = 2×linreg(종가,50) − linreg(linreg(종가,50),50)</span>
        <span class="cond-tag" style="color:#f87171;border-color:#5a2020">필터2: 최근 30거래일 VL 피크→트로프 낙폭 ≥ 40%</span>
        <span class="cond-tag" style="color:#34d399;border-color:#0a3a20">필터3: 최근 3거래일 연속 VL 일간 ≥ 4% 상승</span>
    """,
    31: """
        <span class="cond-tag">필터1: 시총 1,500억↑ · ETF/ETN 제외</span>
        <span class="cond-tag">expoLen = 2×LenRSI−1 = 27 · EMA(상승분, 27) / EMA(하락분, 27)로 RSI 밴드 역산</span>
        <span class="cond-tag">하단밴드(30%) · 중심선 · 상단밴드(70%) · 상하단 1/2선 계산</span>
        <span class="cond-tag" style="color:#818cf8;border-color:#2a2a6a">FindMode 선택: 1~9 (기본=9: 하단돌파 OR 중심선돌파)</span>
    """,
    43: """
        <span class="cond-tag">필터1: 시총 1,500억↑ · ETF/ETN 제외</span>
        <span class="cond-tag" style="color:#6366f1;border-color:#2a2a6a">LenRSI = 30 · expoLen = 2×30−1 = 59 (조건31의 14→30으로 변경)</span>
        <span class="cond-tag">EMA(상승분, 59) / EMA(하락분, 59)로 RSI 밴드 역산</span>
        <span class="cond-tag">하단밴드(30%) · 중심선 · 상단밴드(70%) · 상하단 1/2선 계산</span>
        <span class="cond-tag" style="color:#6366f1;border-color:#2a2a6a">FindMode 선택: 1~9 (기본=9: 하단돌파 OR 중심선돌파)</span>
    """,
    44: """
        <span class="cond-tag">필터1: 시총 5,000억↑ · ETF/ETN 제외</span>
        <span class="cond-tag" style="color:#10b981;border-color:#064e3b">HMA(55) = WMA( 2×WMA(C,27) − WMA(C,55) , 7 ) · hma2 = hma1[5]</span>
        <span class="cond-tag">CrossUp(hma1,hma2) → myLevel = 당일 저가 (지지선) · CrossDown → 당일 고가 (저항선)</span>
        <span class="cond-tag" style="color:#fbbf24;border-color:#5a4a00">Mode6(기본): 종가가 레벨 상향돌파 + 양봉 (C[1]≤레벨, C>레벨, C>O)</span>
        <span class="cond-tag">Mode1 상승반전 · Mode2 하락반전 · Mode3 HMA상향돌파 · Mode4 HMA하향이탈 · Mode5 레벨위유지</span>
        <span class="cond-tag">이메일 알림: 신규 신호 종목 발견 시 자동 발송 (10분 간격 실시간 스캔)</span>
    """,
    46: """
        <span class="cond-tag">필터1: 시총 3,000억↑ · ETF/ETN 제외</span>
        <span class="cond-tag" style="color:#f59e0b;border-color:#5a3a00">HMA(55) = WMA( 2×WMA(C,27) − WMA(C,55) , 7 ) · hma2 = hma[5]</span>
        <span class="cond-tag">CrossUp(hma,hma2) → Lv = 당일 저가 (지지선) · CrossDown → 당일 고가 (저항선)</span>
        <span class="cond-tag" style="color:#fcd34d;border-color:#5a3a00">신호①: 종가가 Lv 상향돌파 (전일종가≤Lv, 금일종가&gt;Lv) + 양봉 (종가&gt;시가)</span>
        <span class="cond-tag" style="color:#a78bfa;border-color:#3a2a6a">신호②: VL(1) &lt; VL — VL = A+(A−A1) · A=LinReg(C,50) · A1=LinReg(A,50)</span>
        <span class="cond-tag">이메일 알림: 신규 신호 종목 발견 시 자동 발송 (10분 간격 실시간 스캔)</span>
    """,
    49: """
        <span class="cond-tag">필터1: 시총 3,000억↑ · ETF/ETN 제외 (시장구분 + 종목명 패턴)</span>
        <span class="cond-tag" style="color:#f472b6;border-color:#5a1a3a">원인점 = 시가·종가 모두 MA5 하방인 첫 번째 봉의 저가 (전봉은 MA5 상방)</span>
        <span class="cond-tag">역방향 탐색: 최근봉부터 소급하여 5일선 첫 이탈 봉 탐지</span>
        <span class="cond-tag" style="color:#fda4af;border-color:#5a1a3a">신호: 전일 종가 ≤ 원인점 → 금일 종가 &gt; 원인점 (재돌파)</span>
        <span class="cond-tag" style="color:#fb923c;border-color:#5a2a00">거래량①: 금일 제외 5일 평균 거래량 ≥ 30만주</span>
        <span class="cond-tag" style="color:#fb923c;border-color:#5a2a00">거래량②: 금일 거래량 ≥ 전일 거래량 × 200% (전일동시간대 대비)</span>
        <span class="cond-tag">결과 정렬: 돌파율(%) 낮은 순 (원인점 바로 위에서 막 돌파한 종목 우선)</span>
    """,
    50: """
        <span class="cond-tag">필터1: 시총 3,000억↑ · ETF/ETN 제외 (시장구분 + 종목명 패턴)</span>
        <span class="cond-tag" style="color:#fbbf24;border-color:#5a3a00">A: 120봉 신고가가 최근 20봉 이내에 발생 (고점 근접 구간)</span>
        <span class="cond-tag" style="color:#4ade80;border-color:#0a3a20">B: 금일 양봉 (종가 &gt; 시가)</span>
        <span class="cond-tag" style="color:#f87171;border-color:#5a1a1a">C: 전일 종가 &lt; 전일 SMA5 (단순이동평균5 하방)</span>
        <span class="cond-tag" style="color:#f87171;border-color:#5a1a1a">D: 전일 시가 &lt; 전일 SMA5 (단순이동평균5 하방 출발)</span>
        <span class="cond-tag" style="color:#0ea5e9;border-color:#0a2a5a">E: 금일 종가 &gt; 금일 SMA5 · 전일 종가 ≤ 전일 SMA5 (종가 SMA5 골든크로스)</span>
        <span class="cond-tag" style="color:#a78bfa;border-color:#3a2a6a">G: 전일 저가 대비 전일 종가 등락률 ≥ 5% (저가→종가 5%↑ 이상 회복)</span>
        <span class="cond-tag" style="color:#fb923c;border-color:#5a2a00">H: 금일 제외 5봉 평균 거래량 ≥ 30만주</span>
        <span class="cond-tag" style="color:#34d399;border-color:#0a4a20">J: SMA200 2봉 연속 상승 (단순이동평균200 · rolling mean)</span>
    """,
    51: """
        <span class="cond-tag">필터1: 시총 3,000억↑ · ETF/ETN 제외</span>
        <span class="cond-tag" style="color:#f87171;border-color:#5a1a1a">A: 3봉전 종가 &lt; SMA5</span>
        <span class="cond-tag" style="color:#f87171;border-color:#5a1a1a">B: 2봉전 종가 &lt; SMA5</span>
        <span class="cond-tag" style="color:#f87171;border-color:#5a1a1a">C: 1봉전 종가 &lt; SMA5 (3일 연속 5일선 하방)</span>
        <span class="cond-tag" style="color:#fda4af;border-color:#5a1a1a">D: 금일 시가 &lt; SMA5</span>
        <span class="cond-tag" style="color:#fda4af;border-color:#5a1a1a">E: 금일 종가 &lt; SMA5 (5일선 아래에서 양봉 형성)</span>
        <span class="cond-tag" style="color:#4ade80;border-color:#0a3a20">F: 금일 양봉 (시가 &lt; 종가)</span>
        <span class="cond-tag" style="color:#60a5fa;border-color:#0a2a5a">G: SMA20 2봉 연속 상승</span>
        <span class="cond-tag" style="color:#34d399;border-color:#0a4a20">H: SMA60 2봉 연속 상승</span>
        <span class="cond-tag" style="color:#fb923c;border-color:#5a2a00">J: 전일 기준 5봉 평균 거래량 ≥ 10만주</span>
    """,
    45: """
        <span class="cond-tag">필터1: 시총 1,500억↑ · ETF/ETN 제외</span>
        <span class="cond-tag" style="color:#ec4899;border-color:#5a1a3a">QQEF = EMA(RSI(14), 5) — 패스트 라인</span>
        <span class="cond-tag">ATRRSI = EMA(EMA(|QQEF−QQEF[1]|, 14), 14) · QUP = QQEF + ATRRSI×4.236 · QDN = QQEF − ATRRSI×4.236</span>
        <span class="cond-tag" style="color:#f9a8d4;border-color:#5a1a3a">QQES = 트레일링 스탑(QDN 지지·QUP 저항) → EMA(5) 최종 평활 — 슬로우 라인</span>
        <span class="cond-tag" style="color:#ec4899;border-color:#5a1a3a">신호: QQEF·QQES 모두 ≤ 50 AND 전일 QQEF &lt; QQES → 금일 QQEF &gt; QQES (50 이하 골든크로스) AND VL금일 &gt; VL전일</span>
        <span class="cond-tag">VL = LinReg(C,50)×2 − LinReg(LinReg(C,50),50) (변동회귀선)</span>
    """,
    32: """
        <span class="cond-tag">필터1: 시총 1,500억↑ · ETF/ETN 제외</span>
        <span class="cond-tag">최근 90봉 중 음봉(종가&lt;시가) 선별 → (O+C+H+L)/4 × 거래량 최대인 봉의 시가 = 저항선</span>
        <span class="cond-tag" style="color:#f97316;border-color:#5a2a00">필터2: 금일 종가 &gt; 저항선 · 전일 종가 ≤ 저항선 (오늘 첫 돌파)</span>
        <span class="cond-tag" style="color:#34d399;border-color:#0a3a20">필터3: ADX(11) &gt; 25 (추세 강도 확인)</span>
        <span class="cond-tag">결과 정렬: 저항대비(%) 낮은 순 (최근 돌파 종목 상위)</span>
    """,
    33: """
        <span class="cond-tag">필터1: 시총 1,500억↑ · ETF/ETN 제외 · 주봉 기준</span>
        <span class="cond-tag">과열스코어 = ATR(5w)/ATR(20w) × MA(V,5w)/MA(V,20w) × 이격도(20w)/100</span>
        <span class="cond-tag" style="color:#f43f5e;border-color:#5a1a2a">필터2: 이번 주 과열스코어 &gt; 3 · 지난 주 스코어 ≤ 3 (첫 충족)</span>
        <span class="cond-tag" style="color:#34d399;border-color:#0a3a20">필터3: 이번 주 양봉 (주봉 종가 &gt; 시가)</span>
        <span class="cond-tag" style="color:#34d399;border-color:#0a3a20">필터4: 이번 주 거래량 &gt; 지난 주 거래량</span>
        <span class="cond-tag">결과 정렬: 과열스코어 높은 순</span>
    """,
    34: """
        <span class="cond-tag">필터1: 시총 1,500억↑ · ETF/ETN 제외 · 일봉 기준</span>
        <span class="cond-tag">과열스코어 = ATR(5)/ATR(20) × MA(V,5)/MA(V,20) × 이격도(20)/100</span>
        <span class="cond-tag" style="color:#fb923c;border-color:#5a2a00">필터2: 과열스코어 &gt; 3 · 양봉 · 거래량 &gt; 전일</span>
        <span class="cond-tag" style="color:#fbbf24;border-color:#5a4a00">결과1: 전일 대비 상승점수 상위 20종목</span>
        <span class="cond-tag" style="color:#fbbf24;border-color:#5a4a00">결과2: 2일전 대비 상승점수 상위 20종목</span>
        <span class="cond-tag" style="color:#34d399;border-color:#0a3a20">결과3: 결과1·2 종목 중 Envelope(20,40%) 상단 미초과 (고가·종가 모두 SMA20×1.4 이하)</span>
    """,
    37: """
        <span class="cond-tag">필터1: 시총 3,000억↑~20조 미만 · ETF/ETN 제외</span>
        <span class="cond-tag">거래대금 = 거래량 × (O+H+L+C)/4</span>
        <span class="cond-tag" style="color:#a855f7;border-color:#4a1a6a">필터2: 당일 거래대금 상위 100종목</span>
        <span class="cond-tag" style="color:#a855f7;border-color:#4a1a6a">필터3: RSI 밴드 상단선(70%) 또는 중심선 상향 돌파</span>
        <span class="cond-tag">결과 정렬: 거래대금(억) 높은 순</span>
    """,
    38: """
        <span class="cond-tag">필터1: 시총 1,500억↑ · ETF/ETN 제외</span>
        <span class="cond-tag">90일 일봉 차트 이미지 업로드 → 90포인트 정규화 패턴 추출</span>
        <span class="cond-tag" style="color:#10b981;border-color:#0a4a30">종가 70% · VL 12.5% · 세력평단 5% · TDI S_RSI 5% · EMA9 4% · EMA20 3.5% 가중 Pearson 유사도</span>
        <span class="cond-tag" style="color:#10b981;border-color:#0a4a30">EMA200 상승 필수 조건 (EMA200[-1] &gt; EMA200[-5])</span>
        <span class="cond-tag">비교 기준: 검색기준일 기준 최근 90봉 · 상위 5종목 + 유사율(%) 표시</span>
    """,
    39: """
        <span class="cond-tag">필터1: 시총 1,500억↑ · ETF/ETN 제외</span>
        <span class="cond-tag">VL = 2×linreg(종가,50) − linreg(linreg(종가,50),50)</span>
        <span class="cond-tag" style="color:#f59e0b;border-color:#5a3a00">필터2: 금일 종가 ≥ VL × 1.3 (VL 대비 30% 이상 이격)</span>
        <span class="cond-tag" style="color:#f87171;border-color:#5a1a1a">필터3: 금일 종가 &lt; 최근 60일 최고 종가 (60일 신고가 종목 제외)</span>
        <span class="cond-tag" style="color:#34d399;border-color:#0a3a20">필터4: VL 상승 — 전일 VL &lt; 금일 VL (VL(1) &lt; VL)</span>
        <span class="cond-tag" style="color:#f87171;border-color:#5a1a1a">필터5: 금일 포함 3거래일 연속 거래량 0 종목 제외</span>
        <span class="cond-tag">결과: 조건 충족 전체 종목 · VL이격(%) 높은 순 정렬 · 60일고가 함께 표시</span>
    """,
    40: """
        <span class="cond-tag" style="color:#06b6d4;border-color:#0a4a5a">데이터 소스: 한국투자증권 KIS OpenAPI (FHPST01720000)</span>
        <span class="cond-tag">시장: 코스닥(Q) · 거래대금순위 TOP20 실시간 조회</span>
        <span class="cond-tag" style="color:#fbbf24;border-color:#5a4a00">신규 진입: 직전 스캔에 없던 종목 (NEW 표시)</span>
        <span class="cond-tag" style="color:#4ade80;border-color:#0a3a20">순위 상승: ↑N 표시 (녹색)</span>
        <span class="cond-tag" style="color:#f87171;border-color:#5a1a1a">순위 하락: ↓N 표시 (빨간색)</span>
        <span class="cond-tag">이메일 알림: 신규진입·TOP20 이탈·3위↑이상 변동 시 자동 발송 (10분 간격)</span>
    """,
    41: """
        <span class="cond-tag" style="color:#f97316;border-color:#5a2a00">데이터 소스: 한국투자증권 KIS OpenAPI (FHPST01720000)</span>
        <span class="cond-tag">시장: 코스닥(Q) · 거래대금순위 실시간 조회</span>
        <span class="cond-tag" style="color:#f87171;border-color:#5a1a1a">필터1: 전일 코스닥 거래대금 TOP10 밖에 있던 종목</span>
        <span class="cond-tag" style="color:#f97316;border-color:#5a2a00">필터2: 금일 코스닥 거래대금 TOP20 진입 종목</span>
        <span class="cond-tag" style="color:#4ade80;border-color:#0a3a20">필터3: 전일동시간대 거래량 대비 200% 이상 (거래량 폭증)</span>
        <span class="cond-tag" style="color:#fbbf24;border-color:#5a4a00">필터4: 양봉 (현재가 &gt; 시가)</span>
        <span class="cond-tag">이메일 알림: 조건 충족 신규 종목 발견 시 자동 발송 (10분 간격)</span>
    """,
    42: """
        <span class="cond-tag" style="color:#8b5cf6;border-color:#3a1a6a">데이터 소스: 한국투자증권 KIS OpenAPI</span>
        <span class="cond-tag">시장: 코스닥(Q) · 당일 거래대금 TOP20 실시간 조회</span>
        <span class="cond-tag" style="color:#c084fc;border-color:#5a2a8a">분석항목: 업종명 · 52주 고저가 · PER/PBR · 등락률 · 거래량비율</span>
        <span class="cond-tag" style="color:#4ade80;border-color:#0a3a20">상승이유: 거래량 증가·양봉·52주 신고가·저PER·외국인 매수 등 규칙 기반 자동 분석</span>
        <span class="cond-tag" style="color:#f87171;border-color:#5a1a1a">하락이유: 거래량 감소·음봉·52주 신저가·고PER·외국인 매도 등 규칙 기반 자동 분석</span>
        <span class="cond-tag" style="color:#fbbf24;border-color:#5a4a00">연관종목: 동일 업종 코스닥 종목 중 3개 자동 연결 (FinanceDataReader 활용)</span>
        <span class="cond-tag">이메일 알림: TOP20 신규 진입 종목 감지 시 자동 발송 (10분 간격)</span>
    """,
}

_S16_EXTRA_SECTION = """
<div style="margin-top:18px;border-top:1px solid #2a2d3a;padding-top:16px">
  <!-- 결과 발송 버튼 (스캔 완료 후 표시) -->
  <div style="display:flex;gap:8px;flex-wrap:wrap;align-items:center;margin-bottom:14px">
    <button class="btn hidden" id="s16SendBtn" onclick="s16SendEmail()"
      style="background:#1a2a1a;color:#4ade80;border:1px solid #2a4a2a;padding:7px 18px">
      📧 결과 이메일 발송</button>
    <span id="s16SendMsg" style="font-size:.8rem"></span>
  </div>
  <!-- 이메일 설정 패널 (조건14와 공유) -->
  <div style="background:#13161f;border:1px solid #2a2d3a;border-radius:10px;padding:14px 16px">
    <div style="font-size:.8rem;color:#8b8fa8;margin-bottom:10px">📧 이메일 설정 (조건14·16 공용)</div>
    <div style="display:flex;gap:8px;flex-wrap:wrap;align-items:center;margin-bottom:8px">
      <input type="email" id="emailFrom" placeholder="발신 Gmail 주소" autocomplete="off"
        style="background:#0f1117;border:1px solid #2a2d3a;border-radius:6px;
               color:#e0e0e0;padding:7px 11px;font-size:.8rem;width:220px">
      <input type="password" id="emailPass" placeholder="Gmail 앱 비밀번호 (16자리)" autocomplete="new-password"
        style="background:#0f1117;border:1px solid #2a2d3a;border-radius:6px;
               color:#e0e0e0;padding:7px 11px;font-size:.8rem;width:200px">
    </div>
    <div style="display:flex;gap:8px;flex-wrap:wrap;align-items:center;margin-bottom:10px">
      <span style="font-size:.78rem;color:#8b8fa8;white-space:nowrap">수신자 (최대 5명):</span>
      <input type="email" id="emailTo1" placeholder="수신자 1" autocomplete="off"
        style="background:#0f1117;border:1px solid #2a2d3a;border-radius:6px;
               color:#e0e0e0;padding:7px 10px;font-size:.8rem;width:190px">
      <input type="email" id="emailTo2" placeholder="수신자 2" autocomplete="off"
        style="background:#0f1117;border:1px solid #2a2d3a;border-radius:6px;
               color:#e0e0e0;padding:7px 10px;font-size:.8rem;width:190px">
      <input type="email" id="emailTo3" placeholder="수신자 3 (선택)" autocomplete="off"
        style="background:#0f1117;border:1px solid #2a2d3a;border-radius:6px;
               color:#e0e0e0;padding:7px 10px;font-size:.8rem;width:190px">
      <input type="email" id="emailTo4" placeholder="수신자 4 (선택)" autocomplete="off"
        style="background:#0f1117;border:1px solid #2a2d3a;border-radius:6px;
               color:#e0e0e0;padding:7px 10px;font-size:.8rem;width:190px">
      <input type="email" id="emailTo5" placeholder="수신자 5 (선택)" autocomplete="off"
        style="background:#0f1117;border:1px solid #2a2d3a;border-radius:6px;
               color:#e0e0e0;padding:7px 10px;font-size:.8rem;width:190px">
    </div>
    <div style="display:flex;gap:8px;flex-wrap:wrap;align-items:center">
      <button class="btn" onclick="saveEmail()"
        style="background:#1e2a4a;color:#4f8ef7;border:1px solid #2a3a6a;padding:7px 18px">
        💾 저장</button>
      <button class="btn" id="testEmailBtn" onclick="testEmail()"
        style="background:#1a2a1a;color:#4ade80;border:1px solid #2a4a2a;padding:7px 18px">
        📨 테스트 발송</button>
    </div>
    <div id="emailStatus" style="margin-top:8px;font-size:.78rem;color:#8b8fa8"></div>
    <div id="emailMsg"    style="margin-top:4px;font-size:.78rem"></div>
  </div>
</div>
"""

_S16_EXTRA_JS = r"""
/* ── 조건16 이메일 설정 저장 ── */
function saveEmail(){
  const from = document.getElementById('emailFrom').value.trim();
  const pass = document.getElementById('emailPass').value.trim();
  const msgEl = document.getElementById('emailMsg');
  const toVals = [];
  for(let i=1;i<=5;i++){
    const v = (document.getElementById('emailTo'+i)||{}).value||'';
    if(v.trim()) toVals.push(v.trim());
  }
  if(!from){ msgEl.style.color='#f87171'; msgEl.textContent='✗ 발신 주소를 입력하세요.'; return; }
  if(!toVals.length){ msgEl.style.color='#f87171'; msgEl.textContent='✗ 수신자를 1명 이상 입력하세요.'; return; }
  msgEl.style.color='#8b8fa8'; msgEl.textContent='저장 중...';
  const passParam = pass || '__keep__';
  let url = '/api/14/email/save?from='+encodeURIComponent(from)+'&pass='+encodeURIComponent(passParam);
  toVals.forEach((v,i)=>{ url+='&to'+(i+1)+'='+encodeURIComponent(v); });
  fetch(url).then(r=>r.json()).then(d=>{
    msgEl.style.color = d.ok?'#4ade80':'#f87171';
    msgEl.textContent = d.ok
      ? `✓ 저장 완료 — 수신자 ${toVals.length}명 · 서버 재시작 후에도 유지됩니다.`
      : ('✗ '+(d.msg||'오류'));
    if(d.ok) loadEmailStatus();
  });
}

/* ── 테스트 이메일 ── */
function testEmail(){
  const btn=document.getElementById('testEmailBtn');
  const msgEl=document.getElementById('emailMsg');
  btn.disabled=true; msgEl.style.color='#8b8fa8'; msgEl.textContent='📨 테스트 발송 중...';
  fetch('/api/14/email/test').then(r=>r.json()).then(d=>{
    btn.disabled=false;
    msgEl.style.color=d.ok?'#4ade80':'#f87171';
    msgEl.textContent=d.ok?'✓ 테스트 이메일 발송 완료! 수신함을 확인하세요.':'✗ 발송 실패: '+(d.msg||'알 수 없는 오류');
  }).catch(()=>{ btn.disabled=false; msgEl.textContent='네트워크 오류'; });
}

/* ── 이메일 설정 복원 ── */
function loadEmailStatus(){
  fetch('/api/14/email/status').then(r=>r.json()).then(s=>{
    const el=document.getElementById('emailStatus');
    if(s.from) document.getElementById('emailFrom').value=s.from;
    if(s.pass) document.getElementById('emailPass').value=s.pass;
    const toList=s.to_list||[];
    for(let i=1;i<=5;i++){
      const inp=document.getElementById('emailTo'+i);
      if(inp) inp.value=toList[i-1]||'';
    }
    if(s.configured){
      const toSummary=toList.map(v=>`<b>${v}</b>`).join(', ');
      el.innerHTML=`<span style="color:#4ade80">● 이메일 설정 완료</span>`+
        `  발신: <b>${s.from}</b>  |  수신자 ${toList.length}명: ${toSummary}  |  앱비번: ${s.pass_hint}`;
    } else {
      el.innerHTML=`<span style="color:#f59e0b">⚠ 이메일 미설정</span>`+
        `  — 발신 Gmail, 앱 비밀번호, 수신자를 입력하고 저장하세요.`;
    }
  });
}

/* ── 결과 이메일 발송 ── */
function s16SendEmail(){
  const btn=document.getElementById('s16SendBtn');
  const msgEl=document.getElementById('s16SendMsg');
  btn.disabled=true; msgEl.style.color='#8b8fa8'; msgEl.textContent='발송 중...';
  fetch('/api/16/email/send').then(r=>r.json()).then(d=>{
    btn.disabled=false;
    msgEl.style.color=d.ok?'#4ade80':'#f87171';
    msgEl.textContent=d.ok?('✓ '+d.msg):('✗ '+(d.msg||'오류'));
  }).catch(()=>{ btn.disabled=false; msgEl.textContent='네트워크 오류'; });
}

/* ── 스캔 완료 후 발송 버튼 표시 (loadResult 훅) ── */
function onResultLoaded(d){
  const btn=document.getElementById('s16SendBtn');
  if(btn) btn.classList.toggle('hidden', !(d.rows && d.rows.length>0));
}

/* ── 페이지 로드 시 이메일 복원 + 이전 결과 있으면 버튼 표시 ── */
loadEmailStatus();
fetch('/api/16/result').then(r=>r.json()).then(d=>{
  if(d.rows && d.rows.length>0){
    document.getElementById('dlBtn').classList.remove('hidden');
    renderResult(d.date, d.rows);
    const btn=document.getElementById('s16SendBtn');
    if(btn) btn.classList.remove('hidden');
  }
});
"""

_RT14_EXTRA_SECTION = """
<div style="margin-top:18px;border-top:1px solid #2a2d3a;padding-top:16px">
  <div style="display:flex;gap:10px;flex-wrap:wrap;align-items:center;margin-bottom:12px">
    <button class="btn" id="rtStartBtn"
      style="background:#1a3a00;color:#4ade80;border:1px solid #2a5a10"
      onclick="rtStart()">▶ 실시간 스캔 시작</button>
    <button class="btn hidden" id="rtStopBtn"
      style="background:#3a1a00;color:#f87171;border:1px solid #5a2010"
      onclick="rtStop()">■ 중지</button>
    <span id="rtBadge" class="rt-badge inactive">● 대기 중</span>
    <span id="rtScanInfo" style="font-size:.78rem;color:#8b8fa8"></span>
  </div>
  <!-- 이메일 설정 패널 -->
  <div style="background:#13161f;border:1px solid #2a2d3a;border-radius:10px;padding:14px 16px;margin-top:4px">
    <div style="font-size:.8rem;color:#8b8fa8;margin-bottom:10px">📧 이메일 알림 설정</div>

    <!-- 발신 계정 행 -->
    <div style="display:flex;gap:8px;flex-wrap:wrap;align-items:center;margin-bottom:8px">
      <input type="email" id="emailFrom" placeholder="발신 Gmail 주소" autocomplete="off"
        style="background:#0f1117;border:1px solid #2a2d3a;border-radius:6px;
               color:#e0e0e0;padding:7px 11px;font-size:.8rem;width:220px">
      <input type="password" id="emailPass" placeholder="Gmail 앱 비밀번호 (16자리)" autocomplete="new-password"
        style="background:#0f1117;border:1px solid #2a2d3a;border-radius:6px;
               color:#e0e0e0;padding:7px 11px;font-size:.8rem;width:200px">
    </div>

    <!-- 수신자 5명 행 -->
    <div style="display:flex;gap:8px;flex-wrap:wrap;align-items:center;margin-bottom:10px">
      <span style="font-size:.78rem;color:#8b8fa8;white-space:nowrap">수신자 (최대 5명):</span>
      <input type="email" id="emailTo1" placeholder="수신자 1" autocomplete="off"
        style="background:#0f1117;border:1px solid #2a2d3a;border-radius:6px;
               color:#e0e0e0;padding:7px 10px;font-size:.8rem;width:190px">
      <input type="email" id="emailTo2" placeholder="수신자 2" autocomplete="off"
        style="background:#0f1117;border:1px solid #2a2d3a;border-radius:6px;
               color:#e0e0e0;padding:7px 10px;font-size:.8rem;width:190px">
      <input type="email" id="emailTo3" placeholder="수신자 3 (선택)" autocomplete="off"
        style="background:#0f1117;border:1px solid #2a2d3a;border-radius:6px;
               color:#e0e0e0;padding:7px 10px;font-size:.8rem;width:190px">
      <input type="email" id="emailTo4" placeholder="수신자 4 (선택)" autocomplete="off"
        style="background:#0f1117;border:1px solid #2a2d3a;border-radius:6px;
               color:#e0e0e0;padding:7px 10px;font-size:.8rem;width:190px">
      <input type="email" id="emailTo5" placeholder="수신자 5 (선택)" autocomplete="off"
        style="background:#0f1117;border:1px solid #2a2d3a;border-radius:6px;
               color:#e0e0e0;padding:7px 10px;font-size:.8rem;width:190px">
    </div>

    <!-- 버튼 행 -->
    <div style="display:flex;gap:8px;flex-wrap:wrap;align-items:center">
      <button class="btn" onclick="saveEmail()"
        style="background:#1e2a4a;color:#4f8ef7;border:1px solid #2a3a6a;padding:7px 18px">
        💾 저장</button>
      <button class="btn" id="testEmailBtn" onclick="testEmail()"
        style="background:#1a2a1a;color:#4ade80;border:1px solid #2a4a2a;padding:7px 18px">
        📨 테스트 발송</button>
    </div>
    <div id="emailStatus" style="margin-top:8px;font-size:.78rem;color:#8b8fa8"></div>
    <div id="emailMsg"    style="margin-top:4px;font-size:.78rem"></div>
  </div>
</div>
"""

_RT14_EXTRA_JS = r"""
/* ── 조건14 실시간 스캔 컨트롤러 ── */
let rtPollTimer  = null;
let rtActive     = false;
let rtLastScanTs = '';   // 마지막으로 결과를 렌더링한 스캔 완료 시각

/* ── 시작 / 중지 ── */
function rtStart(){
  fetch('/api/14/realtime/start').then(r=>r.json()).then(d=>{
    if(!d.ok){ showMsg(d.msg||'오류 발생'); return; }
    setRtActive(true);
    document.getElementById('progWrap').style.display = 'block';
    setP(0, '첫 번째 스캔 준비 중...');
    showMsg('실시간 스캔 시작됨 — 3분 간격으로 자동 반복됩니다.');
    rtLastScanTs = '';
    rtPoll();
  });
}
function rtStop(){
  fetch('/api/14/realtime/stop').then(r=>r.json()).then(()=>{
    setRtActive(false);
    if(rtPollTimer){ clearInterval(rtPollTimer); rtPollTimer = null; }
    showMsg('실시간 스캔 중지됨');
    setP(0,'');
    document.getElementById('progWrap').style.display = 'none';
  });
}

/* ── 배지 / 버튼 상태 ── */
function setRtActive(v){
  rtActive = v;
  document.getElementById('rtStartBtn').classList.toggle('hidden', v);
  document.getElementById('rtStopBtn').classList.toggle('hidden', !v);
  const badge = document.getElementById('rtBadge');
  if(v){ badge.textContent='● 실시간 스캔 중'; badge.classList.remove('inactive'); }
  else { badge.textContent='● 대기 중';         badge.classList.add('inactive'); }
}

/* ── 5초 폴링: 진행률 + 결과 동기화 ── */
function rtPoll(){
  if(rtPollTimer) clearInterval(rtPollTimer);
  rtPollTimer = setInterval(()=>{
    if(!rtActive){ clearInterval(rtPollTimer); return; }

    fetch('/api/14/realtime/status').then(r=>r.json()).then(s=>{
      /* 실시간이 서버측에서 중단됐으면 UI 반영 */
      if(!s.running && rtActive){ setRtActive(false); clearInterval(rtPollTimer); return; }

      /* 소요 시간 포맷 (초 → "X분 Y초") */
      function fmtElapsed(sec){
        if(!sec) return '';
        const m = Math.floor(sec/60), s2 = sec%60;
        return m > 0 ? `${m}분 ${s2}초` : `${s2}초`;
      }

      /* 스캔 정보 표시 */
      const infoEl = document.getElementById('rtScanInfo');
      if(s.scan_no > 0){
        const pst = s.prog_status;
        let info = `스캔 #${s.scan_no}`;
        if(pst === 'loading' || pst === 'running'){
          info += `  |  시작: ${s.scan_start||'—'}  |  진행 중...`;
        } else {
          const el = s.scan_elapsed ? `  소요: ${fmtElapsed(s.scan_elapsed)}` : '';
          info += `  |  시작: ${s.scan_start||'—'}  →  완료: ${s.last_scan||'—'}${el}`;
          if(s.next_scan) info += `  |  다음 스캔: ${s.next_scan}`;
        }
        infoEl.textContent = info;
      }

      /* 진행 바 업데이트 */
      const pst = s.prog_status;
      if(pst === 'loading'){
        setP(0, '종목 리스트 불러오는 중...');
      } else if(pst === 'running'){
        const pct = s.prog_total > 0
          ? Math.round(s.prog_current / s.prog_total * 100) : 0;
        setP(pct, scanProgressText(s.prog_current, s.prog_total, '스캔 중'));
      } else if(pst === 'done'){
        const el = s.scan_elapsed ? `  소요 ${fmtElapsed(s.scan_elapsed)}` : '';
        setP(100, `스캔 완료 ✓  ${s.result_count}개 종목 발견${el}`);
      }

      /* 스캔이 새로 완료됐을 때만 결과 로드 (last_scan 변경 감지) */
      const justDone = (pst === 'done') && s.last_scan && (s.last_scan !== rtLastScanTs);
      if(justDone || (pst === 'done' && s.result_count > 0 && !rtLastScanTs)){
        rtLastScanTs = s.last_scan || '_init_';
        rtLoadResult(s.new_count);
      }
    });
  }, 5000);   /* 5초마다 */
}

/* ── 결과 fetch & 렌더링 ── */
function rtLoadResult(newCount){
  fetch('/api/14/result').then(r=>r.json()).then(d=>{
    if(!d.rows || d.rows.length === 0){
      showMsg(`✓ 스캔 완료 — 조건 충족 종목 없음 (${new Date().toLocaleTimeString()})`);
      return;
    }
    /* 결과 카드 표시 */
    document.getElementById('resultCard').classList.remove('hidden');
    document.getElementById('dlBtn').classList.remove('hidden');
    renderResult(d.date, d.rows);
    highlightNewRows(d.new_codes || []);

    /* 메시지 */
    if(d.new_codes && d.new_codes.length > 0)
      showMsg(`🔔 신규 ${d.new_codes.length}개 종목 발견!  총 ${d.rows.length}개  (${new Date().toLocaleTimeString()})`);
    else
      showMsg(`✓ 스캔 완료 — ${d.rows.length}개 종목 (${new Date().toLocaleTimeString()})`);
  });
}

/* ── 신규 종목 행 깜빡임 ── */
function highlightNewRows(newCodes){
  document.querySelectorAll('#tables tr').forEach(tr=>{
    if(!tr.cells || tr.cells.length < 2) return;
    const code = tr.cells[1].textContent.trim();
    if(newCodes.includes(code)) tr.classList.add('row-new');
    else tr.classList.remove('row-new');
  });
}

/* ── 이메일 설정 저장 ── */
function saveEmail(){
  const from = document.getElementById('emailFrom').value.trim();
  const pass = document.getElementById('emailPass').value.trim();
  const msgEl = document.getElementById('emailMsg');
  // 수신자 1~5 수집 (빈 값 제외)
  const toVals = [];
  for(let i=1;i<=5;i++){
    const v = (document.getElementById('emailTo'+i)||{}).value||'';
    if(v.trim()) toVals.push(v.trim());
  }
  if(!from){ msgEl.style.color='#f87171'; msgEl.textContent='✗ 발신 주소를 입력하세요.'; return; }
  if(!toVals.length){ msgEl.style.color='#f87171'; msgEl.textContent='✗ 수신자를 1명 이상 입력하세요.'; return; }
  msgEl.style.color = '#8b8fa8';
  msgEl.textContent = '저장 중...';
  const passParam = pass || '__keep__';
  let url = '/api/14/email/save?from='+encodeURIComponent(from)+'&pass='+encodeURIComponent(passParam);
  toVals.forEach((v,i)=>{ url += '&to'+(i+1)+'='+encodeURIComponent(v); });
  fetch(url).then(r=>r.json()).then(d=>{
    msgEl.style.color = d.ok ? '#4ade80' : '#f87171';
    msgEl.textContent = d.ok
      ? `✓ 저장 완료 — 수신자 ${toVals.length}명 · 서버·페이지 재시작 후에도 자동 복원됩니다.`
      : ('✗ ' + (d.msg||'오류'));
    if(d.ok) loadEmailStatus();
  });
}

/* ── 테스트 이메일 발송 ── */
function testEmail(){
  const btn   = document.getElementById('testEmailBtn');
  const msgEl = document.getElementById('emailMsg');
  btn.disabled = true;
  msgEl.style.color   = '#8b8fa8';
  msgEl.textContent   = '📨 테스트 발송 중...';
  fetch('/api/14/email/test')
    .then(r=>r.json()).then(d=>{
      btn.disabled = false;
      msgEl.style.color = d.ok ? '#4ade80' : '#f87171';
      msgEl.textContent = d.ok
        ? '✓ 테스트 이메일 발송 완료! 수신함을 확인하세요.'
        : '✗ 발송 실패: ' + (d.msg||'알 수 없는 오류');
    }).catch(()=>{ btn.disabled=false; msgEl.textContent='네트워크 오류'; });
}

/* ── 이메일 설정 상태 표시 + 입력 필드 자동 복원 ── */
function loadEmailStatus(){
  fetch('/api/14/email/status').then(r=>r.json()).then(s=>{
    const el  = document.getElementById('emailStatus');
    /* 발신 계정 복원 */
    if(s.from) document.getElementById('emailFrom').value = s.from;
    if(s.pass) document.getElementById('emailPass').value = s.pass;
    /* 수신자 최대 5명 복원 */
    const toList = s.to_list || [];
    for(let i=1;i<=5;i++){
      const inp = document.getElementById('emailTo'+i);
      if(inp) inp.value = toList[i-1] || '';
    }
    if(s.configured){
      const toSummary = toList.map((v,i)=>`<b>${v}</b>`).join(', ');
      el.innerHTML = `<span style="color:#4ade80">● 이메일 설정 완료</span>` +
        `  발신: <b>${s.from}</b>  |  수신자 ${toList.length}명: ${toSummary}  |  앱비번: ${s.pass_hint}`;
    } else {
      el.innerHTML = `<span style="color:#f59e0b">⚠ 이메일 미설정</span>` +
        `  — 발신 Gmail, 앱 비밀번호, 수신자를 입력하고 저장하세요.`;
    }
  });
}

/* ── 페이지 로드 시: 이미 실행 중이면 즉시 재연결 ── */
fetch('/api/14/realtime/status').then(r=>r.json()).then(s=>{
  if(s.running){
    setRtActive(true);
    document.getElementById('progWrap').style.display = 'block';
    rtPoll();
    if(s.result_count > 0) rtLoadResult(s.new_count);
  }
});
loadEmailStatus();   /* 이메일 설정 상태 표시 */
"""


_RT19_EXTRA_SECTION = """
<div style="margin-top:18px;border-top:1px solid #2a2d3a;padding-top:16px">
  <div style="display:flex;gap:10px;flex-wrap:wrap;align-items:center;margin-bottom:12px">
    <button class="btn" id="rtStartBtn"
      style="background:#1a3a00;color:#4ade80;border:1px solid #2a5a10"
      onclick="rtStart()">▶ 실시간 스캔 시작</button>
    <button class="btn hidden" id="rtStopBtn"
      style="background:#3a1a00;color:#f87171;border:1px solid #5a2010"
      onclick="rtStop()">■ 중지</button>
    <span id="rtBadge" class="rt-badge inactive">● 대기 중</span>
    <span id="rtScanInfo" style="font-size:.78rem;color:#8b8fa8"></span>
  </div>
  <!-- 이메일 설정 패널 -->
  <div style="background:#13161f;border:1px solid #2a2d3a;border-radius:10px;padding:14px 16px;margin-top:4px">
    <div style="font-size:.8rem;color:#8b8fa8;margin-bottom:10px">📧 이메일 알림 설정</div>

    <!-- 발신 계정 행 -->
    <div style="display:flex;gap:8px;flex-wrap:wrap;align-items:center;margin-bottom:8px">
      <input type="email" id="emailFrom" placeholder="발신 Gmail 주소" autocomplete="off"
        style="background:#0f1117;border:1px solid #2a2d3a;border-radius:6px;
               color:#e0e0e0;padding:7px 11px;font-size:.8rem;width:220px">
      <input type="password" id="emailPass" placeholder="Gmail 앱 비밀번호 (16자리)" autocomplete="new-password"
        style="background:#0f1117;border:1px solid #2a2d3a;border-radius:6px;
               color:#e0e0e0;padding:7px 11px;font-size:.8rem;width:200px">
    </div>

    <!-- 수신자 5명 행 -->
    <div style="display:flex;gap:8px;flex-wrap:wrap;align-items:center;margin-bottom:10px">
      <span style="font-size:.78rem;color:#8b8fa8;white-space:nowrap">수신자 (최대 5명):</span>
      <input type="email" id="emailTo1" placeholder="수신자 1" autocomplete="off"
        style="background:#0f1117;border:1px solid #2a2d3a;border-radius:6px;
               color:#e0e0e0;padding:7px 10px;font-size:.8rem;width:190px">
      <input type="email" id="emailTo2" placeholder="수신자 2" autocomplete="off"
        style="background:#0f1117;border:1px solid #2a2d3a;border-radius:6px;
               color:#e0e0e0;padding:7px 10px;font-size:.8rem;width:190px">
      <input type="email" id="emailTo3" placeholder="수신자 3 (선택)" autocomplete="off"
        style="background:#0f1117;border:1px solid #2a2d3a;border-radius:6px;
               color:#e0e0e0;padding:7px 10px;font-size:.8rem;width:190px">
      <input type="email" id="emailTo4" placeholder="수신자 4 (선택)" autocomplete="off"
        style="background:#0f1117;border:1px solid #2a2d3a;border-radius:6px;
               color:#e0e0e0;padding:7px 10px;font-size:.8rem;width:190px">
      <input type="email" id="emailTo5" placeholder="수신자 5 (선택)" autocomplete="off"
        style="background:#0f1117;border:1px solid #2a2d3a;border-radius:6px;
               color:#e0e0e0;padding:7px 10px;font-size:.8rem;width:190px">
    </div>

    <!-- 버튼 행 -->
    <div style="display:flex;gap:8px;flex-wrap:wrap;align-items:center">
      <button class="btn" onclick="saveEmail()"
        style="background:#1e2a4a;color:#4f8ef7;border:1px solid #2a3a6a;padding:7px 18px">
        💾 저장</button>
      <button class="btn" id="testEmailBtn" onclick="testEmail()"
        style="background:#1a2a1a;color:#4ade80;border:1px solid #2a4a2a;padding:7px 18px">
        📨 테스트 발송</button>
    </div>
    <div id="emailStatus" style="margin-top:8px;font-size:.78rem;color:#8b8fa8"></div>
    <div id="emailMsg"    style="margin-top:4px;font-size:.78rem"></div>
  </div>
</div>
"""

_RT19_EXTRA_JS = r"""
/* ── 조건19 실시간 스캔 컨트롤러 ── */
let rtPollTimer  = null;
let rtActive     = false;
let rtLastScanTs = '';

function rtStart(){
  fetch('/api/19/realtime/start').then(r=>r.json()).then(d=>{
    if(!d.ok){ showMsg(d.msg||'오류 발생'); return; }
    setRtActive(true);
    document.getElementById('progWrap').style.display = 'block';
    setP(0, '첫 번째 스캔 준비 중...');
    showMsg('실시간 스캔 시작됨 — 3분 간격으로 자동 반복됩니다.');
    rtLastScanTs = '';
    rtPoll();
  });
}
function rtStop(){
  fetch('/api/19/realtime/stop').then(r=>r.json()).then(()=>{
    setRtActive(false);
    if(rtPollTimer){ clearInterval(rtPollTimer); rtPollTimer = null; }
    showMsg('실시간 스캔 중지됨');
    setP(0,'');
    document.getElementById('progWrap').style.display = 'none';
  });
}

function setRtActive(v){
  rtActive = v;
  document.getElementById('rtStartBtn').classList.toggle('hidden', v);
  document.getElementById('rtStopBtn').classList.toggle('hidden', !v);
  const badge = document.getElementById('rtBadge');
  if(v){ badge.textContent='● 실시간 스캔 중'; badge.classList.remove('inactive'); }
  else { badge.textContent='● 대기 중';         badge.classList.add('inactive'); }
}

function rtPoll(){
  if(rtPollTimer) clearInterval(rtPollTimer);
  rtPollTimer = setInterval(()=>{
    if(!rtActive){ clearInterval(rtPollTimer); return; }

    fetch('/api/19/realtime/status').then(r=>r.json()).then(s=>{
      if(!s.running && rtActive){ setRtActive(false); clearInterval(rtPollTimer); return; }

      function fmtElapsed(sec){
        if(!sec) return '';
        const m = Math.floor(sec/60), s2 = sec%60;
        return m > 0 ? `${m}분 ${s2}초` : `${s2}초`;
      }

      const infoEl = document.getElementById('rtScanInfo');
      if(s.scan_no > 0){
        const pst = s.prog_status;
        let info = `스캔 #${s.scan_no}`;
        if(pst === 'loading' || pst === 'running'){
          info += `  |  시작: ${s.scan_start||'—'}  |  진행 중...`;
        } else {
          const el = s.scan_elapsed ? `  소요: ${fmtElapsed(s.scan_elapsed)}` : '';
          info += `  |  시작: ${s.scan_start||'—'}  →  완료: ${s.last_scan||'—'}${el}`;
          if(s.next_scan) info += `  |  다음 스캔: ${s.next_scan}`;
        }
        infoEl.textContent = info;
      }

      const pst = s.prog_status;
      if(pst === 'loading'){
        setP(0, '종목 리스트 불러오는 중...');
      } else if(pst === 'running'){
        const pct = s.prog_total > 0
          ? Math.round(s.prog_current / s.prog_total * 100) : 0;
        setP(pct, scanProgressText(s.prog_current, s.prog_total, '스캔 중'));
      } else if(pst === 'done'){
        const el = s.scan_elapsed ? `  소요 ${fmtElapsed(s.scan_elapsed)}` : '';
        setP(100, `스캔 완료 ✓  ${s.result_count}개 종목 발견${el}`);
      }

      const justDone = (pst === 'done') && s.last_scan && (s.last_scan !== rtLastScanTs);
      if(justDone || (pst === 'done' && s.result_count > 0 && !rtLastScanTs)){
        rtLastScanTs = s.last_scan || '_init_';
        rtLoadResult(s.new_count);
      }
    });
  }, 5000);
}

function rtLoadResult(newCount){
  fetch('/api/19/result').then(r=>r.json()).then(d=>{
    if(!d.rows || d.rows.length === 0){
      showMsg(`✓ 스캔 완료 — 조건 충족 종목 없음 (${new Date().toLocaleTimeString()})`);
      return;
    }
    document.getElementById('resultCard').classList.remove('hidden');
    document.getElementById('dlBtn').classList.remove('hidden');
    renderResult(d.date, d.rows);
    highlightNewRows(d.new_codes || []);
    if(d.new_codes && d.new_codes.length > 0)
      showMsg(`🔔 신규 ${d.new_codes.length}개 종목 발견!  총 ${d.rows.length}개  (${new Date().toLocaleTimeString()})`);
    else
      showMsg(`✓ 스캔 완료 — ${d.rows.length}개 종목 (${new Date().toLocaleTimeString()})`);
  });
}

function highlightNewRows(newCodes){
  document.querySelectorAll('#tables tr').forEach(tr=>{
    if(!tr.cells || tr.cells.length < 2) return;
    const code = tr.cells[1].textContent.trim();
    if(newCodes.includes(code)) tr.classList.add('row-new');
    else tr.classList.remove('row-new');
  });
}

function saveEmail(){
  const from = document.getElementById('emailFrom').value.trim();
  const pass = document.getElementById('emailPass').value.trim();
  const msgEl = document.getElementById('emailMsg');
  const toVals = [];
  for(let i=1;i<=5;i++){
    const v = (document.getElementById('emailTo'+i)||{}).value||'';
    if(v.trim()) toVals.push(v.trim());
  }
  if(!from){ msgEl.style.color='#f87171'; msgEl.textContent='✗ 발신 주소를 입력하세요.'; return; }
  if(!toVals.length){ msgEl.style.color='#f87171'; msgEl.textContent='✗ 수신자를 1명 이상 입력하세요.'; return; }
  msgEl.style.color = '#8b8fa8';
  msgEl.textContent = '저장 중...';
  const passParam = pass || '__keep__';
  let url = '/api/19/email/save?from='+encodeURIComponent(from)+'&pass='+encodeURIComponent(passParam);
  toVals.forEach((v,i)=>{ url += '&to'+(i+1)+'='+encodeURIComponent(v); });
  fetch(url).then(r=>r.json()).then(d=>{
    msgEl.style.color = d.ok ? '#4ade80' : '#f87171';
    msgEl.textContent = d.ok
      ? `✓ 저장 완료 — 수신자 ${toVals.length}명 · 서버·페이지 재시작 후에도 자동 복원됩니다.`
      : ('✗ ' + (d.msg||'오류'));
    if(d.ok) loadEmailStatus();
  });
}

function testEmail(){
  const btn   = document.getElementById('testEmailBtn');
  const msgEl = document.getElementById('emailMsg');
  btn.disabled = true;
  msgEl.style.color   = '#8b8fa8';
  msgEl.textContent   = '📨 테스트 발송 중...';
  fetch('/api/19/email/test')
    .then(r=>r.json()).then(d=>{
      btn.disabled = false;
      msgEl.style.color = d.ok ? '#4ade80' : '#f87171';
      msgEl.textContent = d.ok
        ? '✓ 테스트 이메일 발송 완료! 수신함을 확인하세요.'
        : '✗ 발송 실패: ' + (d.msg||'알 수 없는 오류');
    }).catch(()=>{ btn.disabled=false; msgEl.textContent='네트워크 오류'; });
}

function loadEmailStatus(){
  fetch('/api/19/email/status').then(r=>r.json()).then(s=>{
    const el  = document.getElementById('emailStatus');
    if(s.from) document.getElementById('emailFrom').value = s.from;
    if(s.pass) document.getElementById('emailPass').value = s.pass;
    const toList = s.to_list || [];
    for(let i=1;i<=5;i++){
      const inp = document.getElementById('emailTo'+i);
      if(inp) inp.value = toList[i-1] || '';
    }
    if(s.configured){
      const toSummary = toList.map((v,i)=>`<b>${v}</b>`).join(', ');
      el.innerHTML = `<span style="color:#4ade80">● 이메일 설정 완료</span>` +
        `  발신: <b>${s.from}</b>  |  수신자 ${toList.length}명: ${toSummary}  |  앱비번: ${s.pass_hint}`;
    } else {
      el.innerHTML = `<span style="color:#f59e0b">⚠ 이메일 미설정</span>` +
        `  — 발신 Gmail, 앱 비밀번호, 수신자를 입력하고 저장하세요.`;
    }
  });
}

/* ── 페이지 로드 시: 이미 실행 중이면 즉시 재연결 ── */
fetch('/api/19/realtime/status').then(r=>r.json()).then(s=>{
  if(s.running){
    setRtActive(true);
    document.getElementById('progWrap').style.display = 'block';
    rtPoll();
    if(s.result_count > 0) rtLoadResult(s.new_count);
  }
});
loadEmailStatus();
"""


_RT20_EXTRA_SECTION = """
<div style="margin-top:18px;border-top:1px solid #2a2d3a;padding-top:16px">
  <div style="display:flex;gap:10px;flex-wrap:wrap;align-items:center;margin-bottom:12px">
    <button class="btn" id="rtStartBtn"
      style="background:#1a3a00;color:#4ade80;border:1px solid #2a5a10"
      onclick="rtStart()">▶ 실시간 스캔 시작</button>
    <button class="btn hidden" id="rtStopBtn"
      style="background:#3a1a00;color:#f87171;border:1px solid #5a2010"
      onclick="rtStop()">■ 중지</button>
    <span id="rtBadge" class="rt-badge inactive">● 대기 중</span>
    <span id="rtScanInfo" style="font-size:.78rem;color:#8b8fa8"></span>
  </div>
  <div style="background:#13161f;border:1px solid #2a2d3a;border-radius:10px;padding:14px 16px;margin-top:4px">
    <div style="font-size:.8rem;color:#8b8fa8;margin-bottom:10px">📧 이메일 알림 설정</div>
    <div style="display:flex;gap:8px;flex-wrap:wrap;align-items:center;margin-bottom:8px">
      <input type="email" id="emailFrom" placeholder="발신 Gmail 주소" autocomplete="off"
        style="background:#0f1117;border:1px solid #2a2d3a;border-radius:6px;
               color:#e0e0e0;padding:7px 11px;font-size:.8rem;width:220px">
      <input type="password" id="emailPass" placeholder="Gmail 앱 비밀번호 (16자리)" autocomplete="new-password"
        style="background:#0f1117;border:1px solid #2a2d3a;border-radius:6px;
               color:#e0e0e0;padding:7px 11px;font-size:.8rem;width:200px">
    </div>
    <div style="display:flex;gap:8px;flex-wrap:wrap;align-items:center;margin-bottom:10px">
      <span style="font-size:.78rem;color:#8b8fa8;white-space:nowrap">수신자 (최대 5명):</span>
      <input type="email" id="emailTo1" placeholder="수신자 1" autocomplete="off"
        style="background:#0f1117;border:1px solid #2a2d3a;border-radius:6px;
               color:#e0e0e0;padding:7px 10px;font-size:.8rem;width:190px">
      <input type="email" id="emailTo2" placeholder="수신자 2" autocomplete="off"
        style="background:#0f1117;border:1px solid #2a2d3a;border-radius:6px;
               color:#e0e0e0;padding:7px 10px;font-size:.8rem;width:190px">
      <input type="email" id="emailTo3" placeholder="수신자 3 (선택)" autocomplete="off"
        style="background:#0f1117;border:1px solid #2a2d3a;border-radius:6px;
               color:#e0e0e0;padding:7px 10px;font-size:.8rem;width:190px">
      <input type="email" id="emailTo4" placeholder="수신자 4 (선택)" autocomplete="off"
        style="background:#0f1117;border:1px solid #2a2d3a;border-radius:6px;
               color:#e0e0e0;padding:7px 10px;font-size:.8rem;width:190px">
      <input type="email" id="emailTo5" placeholder="수신자 5 (선택)" autocomplete="off"
        style="background:#0f1117;border:1px solid #2a2d3a;border-radius:6px;
               color:#e0e0e0;padding:7px 10px;font-size:.8rem;width:190px">
    </div>
    <div style="display:flex;gap:8px;flex-wrap:wrap;align-items:center">
      <button class="btn" onclick="saveEmail()"
        style="background:#1e2a4a;color:#4f8ef7;border:1px solid #2a3a6a;padding:7px 18px">
        💾 저장</button>
      <button class="btn" id="testEmailBtn" onclick="testEmail()"
        style="background:#1a2a1a;color:#4ade80;border:1px solid #2a4a2a;padding:7px 18px">
        📨 테스트 발송</button>
    </div>
    <div id="emailStatus" style="margin-top:8px;font-size:.78rem;color:#8b8fa8"></div>
    <div id="emailMsg"    style="margin-top:4px;font-size:.78rem"></div>
  </div>
</div>
"""

_RT20_EXTRA_JS = r"""
/* ── 조건20 실시간 스캔 컨트롤러 ── */
let rtPollTimer  = null;
let rtActive     = false;
let rtLastScanTs = '';

function rtStart(){
  fetch('/api/20/realtime/start').then(r=>r.json()).then(d=>{
    if(!d.ok){ showMsg(d.msg||'오류 발생'); return; }
    setRtActive(true);
    document.getElementById('progWrap').style.display = 'block';
    setP(0, '첫 번째 스캔 준비 중...');
    showMsg('실시간 스캔 시작됨 — 3분 간격으로 자동 반복됩니다.');
    rtLastScanTs = '';
    rtPoll();
  });
}
function rtStop(){
  fetch('/api/20/realtime/stop').then(r=>r.json()).then(()=>{
    setRtActive(false);
    if(rtPollTimer){ clearInterval(rtPollTimer); rtPollTimer = null; }
    showMsg('실시간 스캔 중지됨');
    setP(0,'');
    document.getElementById('progWrap').style.display = 'none';
  });
}

function setRtActive(v){
  rtActive = v;
  document.getElementById('rtStartBtn').classList.toggle('hidden', v);
  document.getElementById('rtStopBtn').classList.toggle('hidden', !v);
  const badge = document.getElementById('rtBadge');
  if(v){ badge.textContent='● 실시간 스캔 중'; badge.classList.remove('inactive'); }
  else { badge.textContent='● 대기 중';         badge.classList.add('inactive'); }
}

function rtPoll(){
  if(rtPollTimer) clearInterval(rtPollTimer);
  rtPollTimer = setInterval(()=>{
    if(!rtActive){ clearInterval(rtPollTimer); return; }
    fetch('/api/20/realtime/status').then(r=>r.json()).then(s=>{
      if(!s.running && rtActive){ setRtActive(false); clearInterval(rtPollTimer); return; }
      function fmtElapsed(sec){
        if(!sec) return '';
        const m=Math.floor(sec/60), s2=sec%60;
        return m>0?`${m}분 ${s2}초`:`${s2}초`;
      }
      const infoEl=document.getElementById('rtScanInfo');
      if(s.scan_no>0){
        const pst=s.prog_status;
        let info=`스캔 #${s.scan_no}`;
        if(pst==='loading'||pst==='running'){
          info+=`  |  시작: ${s.scan_start||'—'}  |  진행 중...`;
        } else {
          const el=s.scan_elapsed?`  소요: ${fmtElapsed(s.scan_elapsed)}`:'';
          info+=`  |  시작: ${s.scan_start||'—'}  →  완료: ${s.last_scan||'—'}${el}`;
          if(s.next_scan) info+=`  |  다음 스캔: ${s.next_scan}`;
        }
        infoEl.textContent=info;
      }
      const pst=s.prog_status;
      if(pst==='loading') setP(0,'종목 리스트 불러오는 중...');
      else if(pst==='running'){
        const pct=s.prog_total>0?Math.round(s.prog_current/s.prog_total*100):0;
        setP(pct, scanProgressText(s.prog_current, s.prog_total, '스캔 중'));
      } else if(pst==='done'){
        const el=s.scan_elapsed?`  소요 ${fmtElapsed(s.scan_elapsed)}`:'';
        setP(100,`스캔 완료 ✓  ${s.result_count}개 종목 발견${el}`);
      }
      const justDone=(pst==='done')&&s.last_scan&&(s.last_scan!==rtLastScanTs);
      if(justDone||(pst==='done'&&s.result_count>0&&!rtLastScanTs)){
        rtLastScanTs=s.last_scan||'_init_';
        rtLoadResult(s.new_count);
      }
    });
  },5000);
}

function rtLoadResult(newCount){
  fetch('/api/20/result').then(r=>r.json()).then(d=>{
    if(!d.rows||d.rows.length===0){
      showMsg(`✓ 스캔 완료 — 조건 충족 종목 없음 (${new Date().toLocaleTimeString()})`);
      return;
    }
    document.getElementById('resultCard').classList.remove('hidden');
    document.getElementById('dlBtn').classList.remove('hidden');
    renderResult(d.date,d.rows);
    highlightNewRows(d.new_codes||[]);
    if(d.new_codes&&d.new_codes.length>0)
      showMsg(`🔔 신규 ${d.new_codes.length}개 종목 발견!  총 ${d.rows.length}개  (${new Date().toLocaleTimeString()})`);
    else
      showMsg(`✓ 스캔 완료 — ${d.rows.length}개 종목 (${new Date().toLocaleTimeString()})`);
  });
}

function highlightNewRows(newCodes){
  document.querySelectorAll('#tables tr').forEach(tr=>{
    if(!tr.cells||tr.cells.length<2) return;
    const code=tr.cells[1].textContent.trim();
    if(newCodes.includes(code)) tr.classList.add('row-new');
    else tr.classList.remove('row-new');
  });
}

function saveEmail(){
  const from=document.getElementById('emailFrom').value.trim();
  const pass=document.getElementById('emailPass').value.trim();
  const msgEl=document.getElementById('emailMsg');
  const toVals=[];
  for(let i=1;i<=5;i++){
    const v=(document.getElementById('emailTo'+i)||{}).value||'';
    if(v.trim()) toVals.push(v.trim());
  }
  if(!from){msgEl.style.color='#f87171';msgEl.textContent='✗ 발신 주소를 입력하세요.';return;}
  if(!toVals.length){msgEl.style.color='#f87171';msgEl.textContent='✗ 수신자를 1명 이상 입력하세요.';return;}
  msgEl.style.color='#8b8fa8'; msgEl.textContent='저장 중...';
  const passParam=pass||'__keep__';
  let url='/api/20/email/save?from='+encodeURIComponent(from)+'&pass='+encodeURIComponent(passParam);
  toVals.forEach((v,i)=>{url+='&to'+(i+1)+'='+encodeURIComponent(v);});
  fetch(url).then(r=>r.json()).then(d=>{
    msgEl.style.color=d.ok?'#4ade80':'#f87171';
    msgEl.textContent=d.ok
      ?`✓ 저장 완료 — 수신자 ${toVals.length}명 · 서버·페이지 재시작 후에도 자동 복원됩니다.`
      :('✗ '+(d.msg||'오류'));
    if(d.ok) loadEmailStatus();
  });
}

function testEmail(){
  const btn=document.getElementById('testEmailBtn');
  const msgEl=document.getElementById('emailMsg');
  btn.disabled=true; msgEl.style.color='#8b8fa8'; msgEl.textContent='📨 테스트 발송 중...';
  fetch('/api/20/email/test').then(r=>r.json()).then(d=>{
    btn.disabled=false;
    msgEl.style.color=d.ok?'#4ade80':'#f87171';
    msgEl.textContent=d.ok?'✓ 테스트 이메일 발송 완료! 수신함을 확인하세요.':'✗ 발송 실패: '+(d.msg||'알 수 없는 오류');
  }).catch(()=>{btn.disabled=false;msgEl.textContent='네트워크 오류';});
}

function loadEmailStatus(){
  fetch('/api/20/email/status').then(r=>r.json()).then(s=>{
    const el=document.getElementById('emailStatus');
    if(s.from) document.getElementById('emailFrom').value=s.from;
    if(s.pass) document.getElementById('emailPass').value=s.pass;
    const toList=s.to_list||[];
    for(let i=1;i<=5;i++){
      const inp=document.getElementById('emailTo'+i);
      if(inp) inp.value=toList[i-1]||'';
    }
    if(s.configured){
      const toSummary=toList.map(v=>`<b>${v}</b>`).join(', ');
      el.innerHTML=`<span style="color:#4ade80">● 이메일 설정 완료</span>`+
        `  발신: <b>${s.from}</b>  |  수신자 ${toList.length}명: ${toSummary}  |  앱비번: ${s.pass_hint}`;
    } else {
      el.innerHTML=`<span style="color:#f59e0b">⚠ 이메일 미설정</span>`+
        `  — 발신 Gmail, 앱 비밀번호, 수신자를 입력하고 저장하세요.`;
    }
  });
}

fetch('/api/20/realtime/status').then(r=>r.json()).then(s=>{
  if(s.running){
    setRtActive(true);
    document.getElementById('progWrap').style.display='block';
    rtPoll();
    if(s.result_count>0) rtLoadResult(s.new_count);
  }
});
loadEmailStatus();
"""


_RT21_EXTRA_SECTION = """
<div style="margin-top:18px;border-top:1px solid #2a2d3a;padding-top:16px">
  <div style="display:flex;gap:10px;flex-wrap:wrap;align-items:center;margin-bottom:12px">
    <button class="btn" id="rtStartBtn"
      style="background:#1a3a00;color:#4ade80;border:1px solid #2a5a10"
      onclick="rtStart()">▶ 실시간 스캔 시작</button>
    <button class="btn hidden" id="rtStopBtn"
      style="background:#3a1a00;color:#f87171;border:1px solid #5a2010"
      onclick="rtStop()">■ 중지</button>
    <span id="rtBadge" class="rt-badge inactive">● 대기 중</span>
    <span id="rtScanInfo" style="font-size:.78rem;color:#8b8fa8"></span>
  </div>
  <div style="background:#13161f;border:1px solid #2a2d3a;border-radius:10px;padding:14px 16px;margin-top:4px">
    <div style="font-size:.8rem;color:#8b8fa8;margin-bottom:10px">📧 이메일 알림 설정</div>
    <div style="display:flex;gap:8px;flex-wrap:wrap;align-items:center;margin-bottom:8px">
      <input type="email" id="emailFrom" placeholder="발신 Gmail 주소" autocomplete="off"
        style="background:#0f1117;border:1px solid #2a2d3a;border-radius:6px;
               color:#e0e0e0;padding:7px 11px;font-size:.8rem;width:220px">
      <input type="password" id="emailPass" placeholder="Gmail 앱 비밀번호 (16자리)" autocomplete="new-password"
        style="background:#0f1117;border:1px solid #2a2d3a;border-radius:6px;
               color:#e0e0e0;padding:7px 11px;font-size:.8rem;width:200px">
    </div>
    <div style="display:flex;gap:8px;flex-wrap:wrap;align-items:center;margin-bottom:10px">
      <span style="font-size:.78rem;color:#8b8fa8;white-space:nowrap">수신자 (최대 5명):</span>
      <input type="email" id="emailTo1" placeholder="수신자 1" autocomplete="off"
        style="background:#0f1117;border:1px solid #2a2d3a;border-radius:6px;
               color:#e0e0e0;padding:7px 10px;font-size:.8rem;width:190px">
      <input type="email" id="emailTo2" placeholder="수신자 2" autocomplete="off"
        style="background:#0f1117;border:1px solid #2a2d3a;border-radius:6px;
               color:#e0e0e0;padding:7px 10px;font-size:.8rem;width:190px">
      <input type="email" id="emailTo3" placeholder="수신자 3 (선택)" autocomplete="off"
        style="background:#0f1117;border:1px solid #2a2d3a;border-radius:6px;
               color:#e0e0e0;padding:7px 10px;font-size:.8rem;width:190px">
      <input type="email" id="emailTo4" placeholder="수신자 4 (선택)" autocomplete="off"
        style="background:#0f1117;border:1px solid #2a2d3a;border-radius:6px;
               color:#e0e0e0;padding:7px 10px;font-size:.8rem;width:190px">
      <input type="email" id="emailTo5" placeholder="수신자 5 (선택)" autocomplete="off"
        style="background:#0f1117;border:1px solid #2a2d3a;border-radius:6px;
               color:#e0e0e0;padding:7px 10px;font-size:.8rem;width:190px">
    </div>
    <div style="display:flex;gap:8px;flex-wrap:wrap;align-items:center">
      <button class="btn" onclick="saveEmail()"
        style="background:#1e2a4a;color:#4f8ef7;border:1px solid #2a3a6a;padding:7px 18px">
        💾 저장</button>
      <button class="btn" id="testEmailBtn" onclick="testEmail()"
        style="background:#1a2a1a;color:#4ade80;border:1px solid #2a4a2a;padding:7px 18px">
        📨 테스트 발송</button>
    </div>
    <div id="emailStatus" style="margin-top:8px;font-size:.78rem;color:#8b8fa8"></div>
    <div id="emailMsg"    style="margin-top:4px;font-size:.78rem"></div>
  </div>
</div>
"""

_RT21_EXTRA_JS = r"""
/* ── 조건21 실시간 스캔 컨트롤러 ── */
let rtPollTimer  = null;
let rtActive     = false;
let rtLastScanTs = '';

function rtStart(){
  fetch('/api/21/realtime/start').then(r=>r.json()).then(d=>{
    if(!d.ok){ showMsg(d.msg||'오류 발생'); return; }
    setRtActive(true);
    document.getElementById('progWrap').style.display = 'block';
    setP(0, '첫 번째 스캔 준비 중...');
    showMsg('실시간 스캔 시작됨 — 3분 간격으로 자동 반복됩니다.');
    rtLastScanTs = '';
    rtPoll();
  });
}
function rtStop(){
  fetch('/api/21/realtime/stop').then(r=>r.json()).then(()=>{
    setRtActive(false);
    if(rtPollTimer){ clearInterval(rtPollTimer); rtPollTimer = null; }
    showMsg('실시간 스캔 중지됨');
    setP(0,'');
    document.getElementById('progWrap').style.display = 'none';
  });
}

function setRtActive(v){
  rtActive = v;
  document.getElementById('rtStartBtn').classList.toggle('hidden', v);
  document.getElementById('rtStopBtn').classList.toggle('hidden', !v);
  const badge = document.getElementById('rtBadge');
  if(v){ badge.textContent='● 실시간 스캔 중'; badge.classList.remove('inactive'); }
  else { badge.textContent='● 대기 중';         badge.classList.add('inactive'); }
}

function rtPoll(){
  if(rtPollTimer) clearInterval(rtPollTimer);
  rtPollTimer = setInterval(()=>{
    if(!rtActive){ clearInterval(rtPollTimer); return; }
    fetch('/api/21/realtime/status').then(r=>r.json()).then(s=>{
      if(!s.running && rtActive){ setRtActive(false); clearInterval(rtPollTimer); return; }
      function fmtElapsed(sec){
        if(!sec) return '';
        const m=Math.floor(sec/60), s2=sec%60;
        return m>0?`${m}분 ${s2}초`:`${s2}초`;
      }
      const infoEl=document.getElementById('rtScanInfo');
      if(s.scan_no>0){
        const pst=s.prog_status;
        let info=`스캔 #${s.scan_no}`;
        if(pst==='loading'||pst==='running'){
          info+=`  |  시작: ${s.scan_start||'—'}  |  진행 중...`;
        } else {
          const el=s.scan_elapsed?`  소요: ${fmtElapsed(s.scan_elapsed)}`:'';
          info+=`  |  시작: ${s.scan_start||'—'}  →  완료: ${s.last_scan||'—'}${el}`;
          if(s.next_scan) info+=`  |  다음 스캔: ${s.next_scan}`;
        }
        infoEl.textContent=info;
      }
      const pst=s.prog_status;
      if(pst==='loading') setP(0,'테마 목록 불러오는 중...');
      else if(pst==='running'){
        const pct=s.prog_total>0?Math.round(s.prog_current/s.prog_total*100):0;
        setP(pct, scanProgressText(s.prog_current, s.prog_total, '스캔 중', '테마'));
      } else if(pst==='done'){
        const el=s.scan_elapsed?`  소요 ${fmtElapsed(s.scan_elapsed)}`:'';
        setP(100,`스캔 완료 ✓  ${s.result_count}개 종목 발견${el}`);
      }
      const justDone=(pst==='done')&&s.last_scan&&(s.last_scan!==rtLastScanTs);
      if(justDone||(pst==='done'&&s.result_count>0&&!rtLastScanTs)){
        rtLastScanTs=s.last_scan||'_init_';
        rtLoadResult(s.new_count);
      }
    });
  },5000);
}

function rtLoadResult(newCount){
  fetch('/api/21/result').then(r=>r.json()).then(d=>{
    if(!d.rows||d.rows.length===0){
      showMsg(`✓ 스캔 완료 — 조건 충족 종목 없음 (${new Date().toLocaleTimeString()})`);
      return;
    }
    document.getElementById('resultCard').classList.remove('hidden');
    document.getElementById('dlBtn').classList.remove('hidden');
    renderResult(d.date,d.rows);
    highlightNewRows(d.new_codes||[]);
    if(d.new_codes&&d.new_codes.length>0)
      showMsg(`🔔 신규 ${d.new_codes.length}개 종목 발견!  총 ${d.rows.length}개  (${new Date().toLocaleTimeString()})`);
    else
      showMsg(`✓ 스캔 완료 — ${d.rows.length}개 종목 (${new Date().toLocaleTimeString()})`);
  });
}

function highlightNewRows(newCodes){
  document.querySelectorAll('#tables tr').forEach(tr=>{
    if(!tr.cells||tr.cells.length<2) return;
    const code=tr.cells[1].textContent.trim();
    if(newCodes.includes(code)) tr.classList.add('row-new');
    else tr.classList.remove('row-new');
  });
}

function saveEmail(){
  const from=document.getElementById('emailFrom').value.trim();
  const pass=document.getElementById('emailPass').value.trim();
  const msgEl=document.getElementById('emailMsg');
  const toVals=[];
  for(let i=1;i<=5;i++){
    const v=(document.getElementById('emailTo'+i)||{}).value||'';
    if(v.trim()) toVals.push(v.trim());
  }
  if(!from){msgEl.style.color='#f87171';msgEl.textContent='✗ 발신 주소를 입력하세요.';return;}
  if(!toVals.length){msgEl.style.color='#f87171';msgEl.textContent='✗ 수신자를 1명 이상 입력하세요.';return;}
  msgEl.style.color='#8b8fa8'; msgEl.textContent='저장 중...';
  const passParam=pass||'__keep__';
  let url='/api/21/email/save?from='+encodeURIComponent(from)+'&pass='+encodeURIComponent(passParam);
  toVals.forEach((v,i)=>{url+='&to'+(i+1)+'='+encodeURIComponent(v);});
  fetch(url).then(r=>r.json()).then(d=>{
    msgEl.style.color=d.ok?'#4ade80':'#f87171';
    msgEl.textContent=d.ok
      ?`✓ 저장 완료 — 수신자 ${toVals.length}명 · 서버·페이지 재시작 후에도 자동 복원됩니다.`
      :('✗ '+(d.msg||'오류'));
    if(d.ok) loadEmailStatus();
  });
}

function testEmail(){
  const btn=document.getElementById('testEmailBtn');
  const msgEl=document.getElementById('emailMsg');
  btn.disabled=true; msgEl.style.color='#8b8fa8'; msgEl.textContent='📨 테스트 발송 중...';
  fetch('/api/21/email/test').then(r=>r.json()).then(d=>{
    btn.disabled=false;
    msgEl.style.color=d.ok?'#4ade80':'#f87171';
    msgEl.textContent=d.ok?'✓ 테스트 이메일 발송 완료! 수신함을 확인하세요.':'✗ 발송 실패: '+(d.msg||'알 수 없는 오류');
  }).catch(()=>{btn.disabled=false;msgEl.textContent='네트워크 오류';});
}

function loadEmailStatus(){
  fetch('/api/21/email/status').then(r=>r.json()).then(s=>{
    const el=document.getElementById('emailStatus');
    if(s.from) document.getElementById('emailFrom').value=s.from;
    if(s.pass) document.getElementById('emailPass').value=s.pass;
    const toList=s.to_list||[];
    for(let i=1;i<=5;i++){
      const inp=document.getElementById('emailTo'+i);
      if(inp) inp.value=toList[i-1]||'';
    }
    if(s.configured){
      const toSummary=toList.map(v=>`<b>${v}</b>`).join(', ');
      el.innerHTML=`<span style="color:#4ade80">● 이메일 설정 완료</span>`+
        `  발신: <b>${s.from}</b>  |  수신자 ${toList.length}명: ${toSummary}  |  앱비번: ${s.pass_hint}`;
    } else {
      el.innerHTML=`<span style="color:#f59e0b">⚠ 이메일 미설정</span>`+
        `  — 발신 Gmail, 앱 비밀번호, 수신자를 입력하고 저장하세요.`;
    }
  });
}

fetch('/api/21/realtime/status').then(r=>r.json()).then(s=>{
  if(s.running){
    setRtActive(true);
    document.getElementById('progWrap').style.display='block';
    rtPoll();
    if(s.result_count>0) rtLoadResult(s.new_count);
  }
});
loadEmailStatus();
"""


_RT22_EXTRA_SECTION = """
<div style="margin-top:18px;border-top:1px solid #2a2d3a;padding-top:16px">
  <div style="display:flex;gap:10px;flex-wrap:wrap;align-items:center;margin-bottom:12px">
    <button class="btn" id="rtStartBtn"
      style="background:#2a1a00;color:#fb923c;border:1px solid #5a3010"
      onclick="rtStart()">▶ 실시간 스캔 시작</button>
    <button class="btn hidden" id="rtStopBtn"
      style="background:#3a1a00;color:#f87171;border:1px solid #5a2010"
      onclick="rtStop()">■ 중지</button>
    <span id="rtBadge" class="rt-badge inactive">● 대기 중</span>
    <span id="rtScanInfo" style="font-size:.78rem;color:#8b8fa8"></span>
  </div>
  <div style="background:#13161f;border:1px solid #2a2d3a;border-radius:10px;padding:14px 16px;margin-top:4px">
    <div style="font-size:.8rem;color:#8b8fa8;margin-bottom:10px">📧 이메일 알림 설정</div>
    <div style="display:flex;gap:8px;flex-wrap:wrap;align-items:center;margin-bottom:8px">
      <input type="email" id="emailFrom" placeholder="발신 Gmail 주소" autocomplete="off"
        style="background:#0f1117;border:1px solid #2a2d3a;border-radius:6px;
               color:#e0e0e0;padding:7px 11px;font-size:.8rem;width:220px">
      <input type="password" id="emailPass" placeholder="Gmail 앱 비밀번호 (16자리)" autocomplete="new-password"
        style="background:#0f1117;border:1px solid #2a2d3a;border-radius:6px;
               color:#e0e0e0;padding:7px 11px;font-size:.8rem;width:200px">
    </div>
    <div style="display:flex;gap:8px;flex-wrap:wrap;align-items:center;margin-bottom:10px">
      <span style="font-size:.78rem;color:#8b8fa8;white-space:nowrap">수신자 (최대 5명):</span>
      <input type="email" id="emailTo1" placeholder="수신자 1" autocomplete="off"
        style="background:#0f1117;border:1px solid #2a2d3a;border-radius:6px;
               color:#e0e0e0;padding:7px 10px;font-size:.8rem;width:190px">
      <input type="email" id="emailTo2" placeholder="수신자 2" autocomplete="off"
        style="background:#0f1117;border:1px solid #2a2d3a;border-radius:6px;
               color:#e0e0e0;padding:7px 10px;font-size:.8rem;width:190px">
      <input type="email" id="emailTo3" placeholder="수신자 3 (선택)" autocomplete="off"
        style="background:#0f1117;border:1px solid #2a2d3a;border-radius:6px;
               color:#e0e0e0;padding:7px 10px;font-size:.8rem;width:190px">
      <input type="email" id="emailTo4" placeholder="수신자 4 (선택)" autocomplete="off"
        style="background:#0f1117;border:1px solid #2a2d3a;border-radius:6px;
               color:#e0e0e0;padding:7px 10px;font-size:.8rem;width:190px">
      <input type="email" id="emailTo5" placeholder="수신자 5 (선택)" autocomplete="off"
        style="background:#0f1117;border:1px solid #2a2d3a;border-radius:6px;
               color:#e0e0e0;padding:7px 10px;font-size:.8rem;width:190px">
    </div>
    <div style="display:flex;gap:8px;flex-wrap:wrap;align-items:center">
      <button class="btn" onclick="saveEmail()"
        style="background:#1e2a4a;color:#4f8ef7;border:1px solid #2a3a6a;padding:7px 18px">
        💾 저장</button>
      <button class="btn" id="testEmailBtn" onclick="testEmail()"
        style="background:#1a2a1a;color:#4ade80;border:1px solid #2a4a2a;padding:7px 18px">
        📨 테스트 발송</button>
    </div>
    <div id="emailStatus" style="margin-top:8px;font-size:.78rem;color:#8b8fa8"></div>
    <div id="emailMsg"    style="margin-top:4px;font-size:.78rem"></div>
  </div>
</div>
"""

_RT22_EXTRA_JS = r"""
/* ── 조건22 실시간 스캔 컨트롤러 ── */
let rtPollTimer  = null;
let rtActive     = false;
let rtLastScanTs = '';

function rtStart(){
  fetch('/api/22/realtime/start').then(r=>r.json()).then(d=>{
    if(!d.ok){ showMsg(d.msg||'오류 발생'); return; }
    setRtActive(true);
    document.getElementById('progWrap').style.display = 'block';
    setP(0, '첫 번째 스캔 준비 중...');
    showMsg('실시간 스캔 시작됨 — 3분 간격으로 자동 반복됩니다.');
    rtLastScanTs = '';
    rtPoll();
  });
}
function rtStop(){
  fetch('/api/22/realtime/stop').then(r=>r.json()).then(()=>{
    setRtActive(false);
    if(rtPollTimer){ clearInterval(rtPollTimer); rtPollTimer = null; }
    showMsg('실시간 스캔 중지됨');
    setP(0,'');
    document.getElementById('progWrap').style.display = 'none';
  });
}

function setRtActive(v){
  rtActive = v;
  document.getElementById('rtStartBtn').classList.toggle('hidden', v);
  document.getElementById('rtStopBtn').classList.toggle('hidden', !v);
  const badge = document.getElementById('rtBadge');
  if(v){ badge.textContent='● 실시간 스캔 중'; badge.classList.remove('inactive'); }
  else { badge.textContent='● 대기 중';         badge.classList.add('inactive'); }
}

function rtPoll(){
  if(rtPollTimer) clearInterval(rtPollTimer);
  rtPollTimer = setInterval(()=>{
    if(!rtActive){ clearInterval(rtPollTimer); return; }
    fetch('/api/22/realtime/status').then(r=>r.json()).then(s=>{
      if(!s.running && rtActive){ setRtActive(false); clearInterval(rtPollTimer); return; }
      function fmtElapsed(sec){
        if(!sec) return '';
        const m=Math.floor(sec/60), s2=sec%60;
        return m>0?`${m}분 ${s2}초`:`${s2}초`;
      }
      const infoEl=document.getElementById('rtScanInfo');
      if(s.scan_no>0){
        const pst=s.prog_status;
        let info=`스캔 #${s.scan_no}`;
        if(pst==='loading'||pst==='running'){
          info+=`  |  시작: ${s.scan_start||'—'}  |  진행 중...`;
        } else {
          const el=s.scan_elapsed?`  소요: ${fmtElapsed(s.scan_elapsed)}`:'';
          info+=`  |  시작: ${s.scan_start||'—'}  →  완료: ${s.last_scan||'—'}${el}`;
          if(s.next_scan) info+=`  |  다음 스캔: ${s.next_scan}`;
        }
        infoEl.textContent=info;
      }
      const pst=s.prog_status;
      if(pst==='loading') setP(0,'종목 리스트 불러오는 중...');
      else if(pst==='running'){
        const pct=s.prog_total>0?Math.round(s.prog_current/s.prog_total*100):0;
        setP(pct, scanProgressText(s.prog_current, s.prog_total, '스캔 중'));
      } else if(pst==='done'){
        const el=s.scan_elapsed?`  소요 ${fmtElapsed(s.scan_elapsed)}`:'';
        setP(100,`스캔 완료 ✓  ${s.result_count}개 종목 발견${el}`);
      }
      const justDone=(pst==='done')&&s.last_scan&&(s.last_scan!==rtLastScanTs);
      if(justDone||(pst==='done'&&s.result_count>0&&!rtLastScanTs)){
        rtLastScanTs=s.last_scan||'_init_';
        rtLoadResult(s.new_count);
      }
    });
  },5000);
}

function rtLoadResult(newCount){
  fetch('/api/22/result').then(r=>r.json()).then(d=>{
    if(!d.rows||d.rows.length===0){
      showMsg(`✓ 스캔 완료 — 조건 충족 종목 없음 (${new Date().toLocaleTimeString()})`);
      return;
    }
    document.getElementById('resultCard').classList.remove('hidden');
    document.getElementById('dlBtn').classList.remove('hidden');
    renderResult(d.date,d.rows);
    highlightNewRows(d.new_codes||[]);
    if(d.new_codes&&d.new_codes.length>0)
      showMsg(`🔔 신규 ${d.new_codes.length}개 종목 발견!  총 ${d.rows.length}개  (${new Date().toLocaleTimeString()})`);
    else
      showMsg(`✓ 스캔 완료 — ${d.rows.length}개 종목 (${new Date().toLocaleTimeString()})`);
  });
}

function highlightNewRows(newCodes){
  document.querySelectorAll('#tables tr').forEach(tr=>{
    if(!tr.cells||tr.cells.length<2) return;
    const code=tr.cells[1].textContent.trim();
    if(newCodes.includes(code)) tr.classList.add('row-new');
    else tr.classList.remove('row-new');
  });
}

function saveEmail(){
  const from=document.getElementById('emailFrom').value.trim();
  const pass=document.getElementById('emailPass').value.trim();
  const msgEl=document.getElementById('emailMsg');
  const toVals=[];
  for(let i=1;i<=5;i++){
    const v=(document.getElementById('emailTo'+i)||{}).value||'';
    if(v.trim()) toVals.push(v.trim());
  }
  if(!from){msgEl.style.color='#f87171';msgEl.textContent='✗ 발신 주소를 입력하세요.';return;}
  if(!toVals.length){msgEl.style.color='#f87171';msgEl.textContent='✗ 수신자를 1명 이상 입력하세요.';return;}
  msgEl.style.color='#8b8fa8'; msgEl.textContent='저장 중...';
  const passParam=pass||'__keep__';
  let url='/api/22/email/save?from='+encodeURIComponent(from)+'&pass='+encodeURIComponent(passParam);
  toVals.forEach((v,i)=>{url+='&to'+(i+1)+'='+encodeURIComponent(v);});
  fetch(url).then(r=>r.json()).then(d=>{
    msgEl.style.color=d.ok?'#4ade80':'#f87171';
    msgEl.textContent=d.ok
      ?`✓ 저장 완료 — 수신자 ${toVals.length}명 · 서버·페이지 재시작 후에도 자동 복원됩니다.`
      :('✗ '+(d.msg||'오류'));
    if(d.ok) loadEmailStatus();
  });
}

function testEmail(){
  const btn=document.getElementById('testEmailBtn');
  const msgEl=document.getElementById('emailMsg');
  btn.disabled=true; msgEl.style.color='#8b8fa8'; msgEl.textContent='📨 테스트 발송 중...';
  fetch('/api/22/email/test').then(r=>r.json()).then(d=>{
    btn.disabled=false;
    msgEl.style.color=d.ok?'#4ade80':'#f87171';
    msgEl.textContent=d.ok?'✓ 테스트 이메일 발송 완료! 수신함을 확인하세요.':'✗ 발송 실패: '+(d.msg||'알 수 없는 오류');
  }).catch(()=>{btn.disabled=false;msgEl.textContent='네트워크 오류';});
}

function loadEmailStatus(){
  fetch('/api/22/email/status').then(r=>r.json()).then(s=>{
    const el=document.getElementById('emailStatus');
    if(s.from) document.getElementById('emailFrom').value=s.from;
    if(s.pass) document.getElementById('emailPass').value=s.pass;
    const toList=s.to_list||[];
    for(let i=1;i<=5;i++){
      const inp=document.getElementById('emailTo'+i);
      if(inp) inp.value=toList[i-1]||'';
    }
    if(s.configured){
      const toSummary=toList.map(v=>`<b>${v}</b>`).join(', ');
      el.innerHTML=`<span style="color:#4ade80">● 이메일 설정 완료</span>`+
        `  발신: <b>${s.from}</b>  |  수신자 ${toList.length}명: ${toSummary}  |  앱비번: ${s.pass_hint}`;
    } else {
      el.innerHTML=`<span style="color:#f59e0b">⚠ 이메일 미설정</span>`+
        `  — 발신 Gmail, 앱 비밀번호, 수신자를 입력하고 저장하세요.`;
    }
  });
}

fetch('/api/22/realtime/status').then(r=>r.json()).then(s=>{
  if(s.running){
    setRtActive(true);
    document.getElementById('progWrap').style.display='block';
    rtPoll();
    if(s.result_count>0) rtLoadResult(s.new_count);
  }
});
loadEmailStatus();
"""


_RT25_EXTRA_SECTION = """
<div style="margin-top:18px;border-top:1px solid #2a2d3a;padding-top:16px">
  <div style="background:#13161f;border:1px solid #2a2d3a;border-radius:10px;padding:14px 16px;margin-bottom:14px">
    <div style="font-size:.8rem;color:#8b8fa8;margin-bottom:6px">📂 전일 거래대금 순위 CSV 업로드</div>
    <div style="font-size:.75rem;color:#6b7280;margin-bottom:10px">
      형식: <b>순위</b>, <b>종목코드</b> 컬럼 포함 (헤더 1행). 컬럼명 자동 감지.
    </div>
    <div style="display:flex;gap:8px;flex-wrap:wrap;align-items:center">
      <input type="file" id="csvFile" accept=".csv"
        style="background:#0f1117;border:1px solid #2a2d3a;border-radius:6px;
               color:#e0e0e0;padding:6px 10px;font-size:.8rem;max-width:340px">
      <button class="btn" onclick="uploadCsv()"
        style="background:#2a1a00;color:#f59e0b;border:1px solid #5a3a00;padding:7px 18px">
        📤 업로드</button>
    </div>
    <div id="csvStatus" style="margin-top:8px;font-size:.78rem;color:#8b8fa8">— 전일 순위 미업로드</div>
  </div>
  <div style="display:flex;gap:10px;flex-wrap:wrap;align-items:center;margin-bottom:12px">
    <button class="btn" id="rtStartBtn"
      style="background:#2a1a00;color:#f59e0b;border:1px solid #5a3a00"
      onclick="rtStart()">▶ 실시간 스캔 시작</button>
    <button class="btn hidden" id="rtStopBtn"
      style="background:#3a1a00;color:#f87171;border:1px solid #5a2010"
      onclick="rtStop()">■ 중지</button>
    <span id="rtBadge" class="rt-badge inactive">● 대기 중</span>
    <span id="rtScanInfo" style="font-size:.78rem;color:#8b8fa8"></span>
  </div>
  <div style="background:#13161f;border:1px solid #2a2d3a;border-radius:10px;padding:14px 16px;margin-top:4px">
    <div style="font-size:.8rem;color:#8b8fa8;margin-bottom:10px">📧 이메일 알림 설정</div>
    <div style="display:flex;gap:8px;flex-wrap:wrap;align-items:center;margin-bottom:8px">
      <input type="email" id="emailFrom" placeholder="발신 Gmail 주소" autocomplete="off"
        style="background:#0f1117;border:1px solid #2a2d3a;border-radius:6px;
               color:#e0e0e0;padding:7px 11px;font-size:.8rem;width:220px">
      <input type="password" id="emailPass" placeholder="Gmail 앱 비밀번호 (16자리)" autocomplete="new-password"
        style="background:#0f1117;border:1px solid #2a2d3a;border-radius:6px;
               color:#e0e0e0;padding:7px 11px;font-size:.8rem;width:200px">
    </div>
    <div style="display:flex;gap:8px;flex-wrap:wrap;align-items:center;margin-bottom:10px">
      <span style="font-size:.78rem;color:#8b8fa8;white-space:nowrap">수신자 (최대 5명):</span>
      <input type="email" id="emailTo1" placeholder="수신자 1" autocomplete="off"
        style="background:#0f1117;border:1px solid #2a2d3a;border-radius:6px;
               color:#e0e0e0;padding:7px 10px;font-size:.8rem;width:190px">
      <input type="email" id="emailTo2" placeholder="수신자 2" autocomplete="off"
        style="background:#0f1117;border:1px solid #2a2d3a;border-radius:6px;
               color:#e0e0e0;padding:7px 10px;font-size:.8rem;width:190px">
      <input type="email" id="emailTo3" placeholder="수신자 3 (선택)" autocomplete="off"
        style="background:#0f1117;border:1px solid #2a2d3a;border-radius:6px;
               color:#e0e0e0;padding:7px 10px;font-size:.8rem;width:190px">
      <input type="email" id="emailTo4" placeholder="수신자 4 (선택)" autocomplete="off"
        style="background:#0f1117;border:1px solid #2a2d3a;border-radius:6px;
               color:#e0e0e0;padding:7px 10px;font-size:.8rem;width:190px">
      <input type="email" id="emailTo5" placeholder="수신자 5 (선택)" autocomplete="off"
        style="background:#0f1117;border:1px solid #2a2d3a;border-radius:6px;
               color:#e0e0e0;padding:7px 10px;font-size:.8rem;width:190px">
    </div>
    <div style="display:flex;gap:8px;flex-wrap:wrap;align-items:center">
      <button class="btn" onclick="saveEmail()"
        style="background:#1e2a4a;color:#4f8ef7;border:1px solid #2a3a6a;padding:7px 18px">
        💾 저장</button>
      <button class="btn" id="testEmailBtn" onclick="testEmail()"
        style="background:#1a2a1a;color:#4ade80;border:1px solid #2a4a2a;padding:7px 18px">
        📨 테스트 발송</button>
    </div>
    <div id="emailStatus" style="margin-top:8px;font-size:.78rem;color:#8b8fa8"></div>
    <div id="emailMsg"    style="margin-top:4px;font-size:.78rem"></div>
  </div>
</div>
"""

_RT25_EXTRA_JS = r"""
/* ── 조건25 CSV 업로드 ── */
function uploadCsv(){
  const f = document.getElementById('csvFile').files[0];
  if(!f){ document.getElementById('csvStatus').textContent='✗ 파일을 선택하세요.'; return; }
  const fd = new FormData(); fd.append('file', f);
  document.getElementById('csvStatus').textContent = '업로드 중...';
  fetch('/api/25/upload_prev', {method:'POST', body:fd}).then(r=>r.json()).then(d=>{
    const el=document.getElementById('csvStatus');
    if(d.ok){ el.style.color='#4ade80'; el.textContent=`✓ ${d.filename} — ${d.count}개 종목 로드 완료`; }
    else     { el.style.color='#f87171'; el.textContent='✗ '+(d.msg||'파싱 실패'); }
  }).catch(()=>{ document.getElementById('csvStatus').textContent='네트워크 오류'; });
}

/* ── 조건25 실시간 스캔 컨트롤러 ── */
let rtPollTimer=null, rtActive=false, rtLastScanTs='';

function rtStart(){
  fetch('/api/25/realtime/start').then(r=>r.json()).then(d=>{
    if(!d.ok){ showMsg(d.msg||'오류 발생'); return; }
    setRtActive(true);
    document.getElementById('progWrap').style.display='block';
    setP(0,'첫 번째 스캔 준비 중...');
    showMsg('실시간 스캔 시작됨 — 3분 간격 자동 반복');
    rtLastScanTs=''; rtPoll();
  });
}
function rtStop(){
  fetch('/api/25/realtime/stop').then(r=>r.json()).then(()=>{
    setRtActive(false);
    if(rtPollTimer){clearInterval(rtPollTimer);rtPollTimer=null;}
    showMsg('실시간 스캔 중지됨'); setP(0,'');
    document.getElementById('progWrap').style.display='none';
  });
}
function setRtActive(v){
  rtActive=v;
  document.getElementById('rtStartBtn').classList.toggle('hidden',v);
  document.getElementById('rtStopBtn').classList.toggle('hidden',!v);
  const b=document.getElementById('rtBadge');
  if(v){b.textContent='● 실시간 스캔 중';b.classList.remove('inactive');}
  else {b.textContent='● 대기 중';b.classList.add('inactive');}
}
function rtPoll(){
  if(rtPollTimer) clearInterval(rtPollTimer);
  rtPollTimer=setInterval(()=>{
    if(!rtActive){clearInterval(rtPollTimer);return;}
    fetch('/api/25/realtime/status').then(r=>r.json()).then(s=>{
      if(!s.running&&rtActive){setRtActive(false);clearInterval(rtPollTimer);return;}
      function fmt(sec){const m=Math.floor(sec/60),s2=sec%60;return m>0?`${m}분 ${s2}초`:`${s2}초`;}
      const el=document.getElementById('rtScanInfo');
      if(s.scan_no>0){
        const pst=s.prog_status; let info=`스캔 #${s.scan_no}`;
        if(pst==='loading'||pst==='running') info+=`  |  시작: ${s.scan_start||'—'}  |  진행 중...`;
        else{
          info+=`  |  ${s.scan_start||'—'} → ${s.last_scan||'—'}${s.scan_elapsed?' 소요: '+fmt(s.scan_elapsed):''}`;
          if(s.next_scan) info+=`  |  다음: ${s.next_scan}`;
        }
        el.textContent=info;
      }
      const pst=s.prog_status;
      if(pst==='loading') setP(0,'KIS 거래대금 순위 조회 중...');
      else if(pst==='running'){
        const pct = scanProgressPct(s.prog_current, s.prog_total);
        setP(pct, s.prog_total>0 ? scanProgressText(s.prog_current, s.prog_total, '전일 순위 비교 중', '단계') : '전일 순위 비교 중...');
      }
      else if(pst==='done') setP(100,`스캔 완료 ✓  ${s.result_count}개 종목`);
      const justDone=(pst==='done')&&s.last_scan&&(s.last_scan!==rtLastScanTs);
      if(justDone||(pst==='done'&&s.result_count>0&&!rtLastScanTs)){
        rtLastScanTs=s.last_scan||'_init_'; rtLoadResult(s.new_count);
      }
    });
  },5000);
}
function rtLoadResult(newCount){
  fetch('/api/25/result').then(r=>r.json()).then(d=>{
    if(!d.rows||!d.rows.length){showMsg(`✓ 스캔 완료 — 조건 충족 종목 없음 (${new Date().toLocaleTimeString()})`);return;}
    document.getElementById('resultCard').classList.remove('hidden');
    document.getElementById('dlBtn').classList.remove('hidden');
    renderResult(d.date,d.rows);
    highlightNewRows(d.new_codes||[]);
    if(d.new_codes&&d.new_codes.length>0)
      showMsg(`🔔 신규 ${d.new_codes.length}개!  총 ${d.rows.length}개  (${new Date().toLocaleTimeString()})`);
    else showMsg(`✓ 스캔 완료 — ${d.rows.length}개 (${new Date().toLocaleTimeString()})`);
  });
}
function highlightNewRows(newCodes){
  document.querySelectorAll('#tables tr').forEach(tr=>{
    if(!tr.cells||tr.cells.length<2) return;
    const code=tr.cells[0].textContent.trim();
    if(newCodes.includes(code)) tr.classList.add('row-new');
    else tr.classList.remove('row-new');
  });
}
function saveEmail(){
  const from=document.getElementById('emailFrom').value.trim();
  const pass=document.getElementById('emailPass').value.trim();
  const msgEl=document.getElementById('emailMsg');
  const toVals=[];
  for(let i=1;i<=5;i++){const v=(document.getElementById('emailTo'+i)||{}).value||'';if(v.trim())toVals.push(v.trim());}
  if(!from){msgEl.style.color='#f87171';msgEl.textContent='✗ 발신 주소를 입력하세요.';return;}
  if(!toVals.length){msgEl.style.color='#f87171';msgEl.textContent='✗ 수신자를 1명 이상 입력하세요.';return;}
  msgEl.style.color='#8b8fa8';msgEl.textContent='저장 중...';
  const passParam=pass||'__keep__';
  let url='/api/25/email/save?from='+encodeURIComponent(from)+'&pass='+encodeURIComponent(passParam);
  toVals.forEach((v,i)=>{url+='&to'+(i+1)+'='+encodeURIComponent(v);});
  fetch(url).then(r=>r.json()).then(d=>{
    msgEl.style.color=d.ok?'#4ade80':'#f87171';
    msgEl.textContent=d.ok?`✓ 저장 완료 — 수신자 ${toVals.length}명`:('✗ '+(d.msg||'오류'));
    if(d.ok) loadEmailStatus();
  });
}
function testEmail(){
  const btn=document.getElementById('testEmailBtn');
  const msgEl=document.getElementById('emailMsg');
  btn.disabled=true;msgEl.style.color='#8b8fa8';msgEl.textContent='📨 테스트 발송 중...';
  fetch('/api/25/email/test').then(r=>r.json()).then(d=>{
    btn.disabled=false;msgEl.style.color=d.ok?'#4ade80':'#f87171';
    msgEl.textContent=d.ok?'✓ 테스트 이메일 발송 완료!':'✗ 발송 실패: '+(d.msg||'');
  }).catch(()=>{btn.disabled=false;msgEl.textContent='네트워크 오류';});
}
function loadEmailStatus(){
  fetch('/api/25/email/status').then(r=>r.json()).then(s=>{
    const el=document.getElementById('emailStatus');
    if(s.from) document.getElementById('emailFrom').value=s.from;
    if(s.pass) document.getElementById('emailPass').value=s.pass;
    const toList=s.to_list||[];
    for(let i=1;i<=5;i++){const inp=document.getElementById('emailTo'+i);if(inp)inp.value=toList[i-1]||'';}
    if(s.configured){
      const toSummary=toList.map(v=>`<b>${v}</b>`).join(', ');
      el.innerHTML=`<span style="color:#4ade80">● 이메일 설정 완료</span>  발신: <b>${s.from}</b>  |  수신자 ${toList.length}명: ${toSummary}`;
    } else {
      el.innerHTML=`<span style="color:#f59e0b">⚠ 이메일 미설정</span>`;
    }
  });
}
fetch('/api/25/prev_status').then(r=>r.json()).then(s=>{
  if(s.loaded){
    const el=document.getElementById('csvStatus');
    el.style.color='#4ade80';
    el.textContent=`✓ ${s.filename} — ${s.count}개 종목 (서버 메모리 유지 중)`;
  }
});
fetch('/api/25/realtime/status').then(r=>r.json()).then(s=>{
  if(s.running){setRtActive(true);document.getElementById('progWrap').style.display='block';rtPoll();if(s.result_count>0)rtLoadResult(s.new_count);}
});
loadEmailStatus();
"""


_RT28_EXTRA_SECTION = """
<div style="margin-top:18px;border-top:1px solid #2a2d3a;padding-top:16px">
  <div style="display:flex;gap:10px;flex-wrap:wrap;align-items:center;margin-bottom:12px">
    <button class="btn" id="rtStartBtn"
      style="background:#1a1030;color:#a78bfa;border:1px solid #4a2a8a"
      onclick="rtStart()">▶ 실시간 스캔 시작</button>
    <button class="btn hidden" id="rtStopBtn"
      style="background:#3a1040;color:#f87171;border:1px solid #5a2050"
      onclick="rtStop()">■ 중지</button>
    <span id="rtBadge" class="rt-badge inactive">● 대기 중</span>
    <span id="rtScanInfo" style="font-size:.78rem;color:#8b8fa8"></span>
  </div>
  <div style="background:#13161f;border:1px solid #2a2d3a;border-radius:10px;padding:14px 16px;margin-top:4px">
    <div style="font-size:.8rem;color:#8b8fa8;margin-bottom:10px">📧 이메일 알림 설정</div>
    <div style="display:flex;gap:8px;flex-wrap:wrap;align-items:center;margin-bottom:8px">
      <input type="email" id="emailFrom" placeholder="발신 Gmail 주소" autocomplete="off"
        style="background:#0f1117;border:1px solid #2a2d3a;border-radius:6px;
               color:#e0e0e0;padding:7px 11px;font-size:.8rem;width:220px">
      <input type="password" id="emailPass" placeholder="Gmail 앱 비밀번호 (16자리)" autocomplete="new-password"
        style="background:#0f1117;border:1px solid #2a2d3a;border-radius:6px;
               color:#e0e0e0;padding:7px 11px;font-size:.8rem;width:200px">
    </div>
    <div style="display:flex;gap:8px;flex-wrap:wrap;align-items:center;margin-bottom:10px">
      <span style="font-size:.78rem;color:#8b8fa8;white-space:nowrap">수신자 (최대 5명):</span>
      <input type="email" id="emailTo1" placeholder="수신자 1" autocomplete="off"
        style="background:#0f1117;border:1px solid #2a2d3a;border-radius:6px;
               color:#e0e0e0;padding:7px 10px;font-size:.8rem;width:190px">
      <input type="email" id="emailTo2" placeholder="수신자 2" autocomplete="off"
        style="background:#0f1117;border:1px solid #2a2d3a;border-radius:6px;
               color:#e0e0e0;padding:7px 10px;font-size:.8rem;width:190px">
      <input type="email" id="emailTo3" placeholder="수신자 3 (선택)" autocomplete="off"
        style="background:#0f1117;border:1px solid #2a2d3a;border-radius:6px;
               color:#e0e0e0;padding:7px 10px;font-size:.8rem;width:190px">
      <input type="email" id="emailTo4" placeholder="수신자 4 (선택)" autocomplete="off"
        style="background:#0f1117;border:1px solid #2a2d3a;border-radius:6px;
               color:#e0e0e0;padding:7px 10px;font-size:.8rem;width:190px">
      <input type="email" id="emailTo5" placeholder="수신자 5 (선택)" autocomplete="off"
        style="background:#0f1117;border:1px solid #2a2d3a;border-radius:6px;
               color:#e0e0e0;padding:7px 10px;font-size:.8rem;width:190px">
    </div>
    <div style="display:flex;gap:8px;flex-wrap:wrap;align-items:center">
      <button class="btn" onclick="saveEmail()"
        style="background:#1e2a4a;color:#4f8ef7;border:1px solid #2a3a6a;padding:7px 18px">
        💾 저장</button>
      <button class="btn" id="testEmailBtn" onclick="testEmail()"
        style="background:#1a2a1a;color:#4ade80;border:1px solid #2a4a2a;padding:7px 18px">
        📨 테스트 발송</button>
    </div>
    <div id="emailStatus" style="margin-top:8px;font-size:.78rem;color:#8b8fa8"></div>
    <div id="emailMsg"    style="margin-top:4px;font-size:.78rem"></div>
  </div>
</div>
"""

_RT28_EXTRA_JS = r"""
/* ── 조건28 실시간 스캔 컨트롤러 ── */
let rtPollTimer  = null;
let rtActive     = false;
let rtLastScanTs = '';

function rtStart(){
  fetch('/api/28/realtime/start').then(r=>r.json()).then(d=>{
    if(!d.ok){ showMsg(d.msg||'오류 발생'); return; }
    setRtActive(true);
    document.getElementById('progWrap').style.display = 'block';
    setP(0, '첫 번째 스캔 준비 중...');
    showMsg('실시간 스캔 시작됨 — 3분 간격으로 자동 반복됩니다.');
    rtLastScanTs = '';
    rtPoll();
  });
}
function rtStop(){
  fetch('/api/28/realtime/stop').then(r=>r.json()).then(()=>{
    setRtActive(false);
    if(rtPollTimer){ clearInterval(rtPollTimer); rtPollTimer = null; }
    showMsg('실시간 스캔 중지됨');
    setP(0,'');
    document.getElementById('progWrap').style.display = 'none';
  });
}
function setRtActive(v){
  rtActive = v;
  document.getElementById('rtStartBtn').classList.toggle('hidden', v);
  document.getElementById('rtStopBtn').classList.toggle('hidden', !v);
  const badge = document.getElementById('rtBadge');
  if(v){ badge.textContent='● 실시간 스캔 중'; badge.classList.remove('inactive'); }
  else { badge.textContent='● 대기 중';         badge.classList.add('inactive'); }
}
function rtPoll(){
  if(rtPollTimer) clearInterval(rtPollTimer);
  rtPollTimer = setInterval(()=>{
    if(!rtActive){ clearInterval(rtPollTimer); return; }
    fetch('/api/28/realtime/status').then(r=>r.json()).then(s=>{
      if(!s.running && rtActive){ setRtActive(false); clearInterval(rtPollTimer); return; }
      function fmtElapsed(sec){ if(!sec) return ''; const m=Math.floor(sec/60),s2=sec%60; return m>0?`${m}분 ${s2}초`:`${s2}초`; }
      const infoEl=document.getElementById('rtScanInfo');
      if(s.scan_no>0){
        const pst=s.prog_status; let info=`스캔 #${s.scan_no}`;
        if(pst==='loading'||pst==='running'){ info+=`  |  시작: ${s.scan_start||'—'}  |  진행 중...`; }
        else{ const el=s.scan_elapsed?`  소요: ${fmtElapsed(s.scan_elapsed)}`:''; info+=`  |  시작: ${s.scan_start||'—'}  →  완료: ${s.last_scan||'—'}${el}`; if(s.next_scan) info+=`  |  다음 스캔: ${s.next_scan}`; }
        infoEl.textContent=info;
      }
      const pst=s.prog_status;
      if(pst==='loading') setP(0,'종목 리스트 불러오는 중...');
      else if(pst==='running'){ const pct=s.prog_total>0?Math.round(s.prog_current/s.prog_total*100):0; setP(pct, scanProgressText(s.prog_current, s.prog_total, '스캔 중')); }
      else if(pst==='done'){ const el=s.scan_elapsed?`  소요 ${fmtElapsed(s.scan_elapsed)}`:''; setP(100,`스캔 완료 ✓  ${s.result_count}개 종목 발견${el}`); }
      const justDone=(pst==='done')&&s.last_scan&&(s.last_scan!==rtLastScanTs);
      if(justDone||(pst==='done'&&s.result_count>0&&!rtLastScanTs)){ rtLastScanTs=s.last_scan||'_init_'; rtLoadResult(s.new_count); }
    });
  },5000);
}
function rtLoadResult(newCount){
  fetch('/api/28/result').then(r=>r.json()).then(d=>{
    if(!d.rows||d.rows.length===0){ showMsg(`✓ 스캔 완료 — 조건 충족 종목 없음 (${new Date().toLocaleTimeString()})`); return; }
    document.getElementById('resultCard').classList.remove('hidden');
    document.getElementById('dlBtn').classList.remove('hidden');
    renderResult(d.date,d.rows);
    highlightNewRows(d.new_codes||[]);
    if(d.new_codes&&d.new_codes.length>0) showMsg(`🔔 신규 ${d.new_codes.length}개 종목 발견!  총 ${d.rows.length}개  (${new Date().toLocaleTimeString()})`);
    else showMsg(`✓ 스캔 완료 — ${d.rows.length}개 종목 (${new Date().toLocaleTimeString()})`);
  });
}
function highlightNewRows(newCodes){
  document.querySelectorAll('#tables tr').forEach(tr=>{ if(!tr.cells||tr.cells.length<2) return; const code=tr.cells[1].textContent.trim(); if(newCodes.includes(code)) tr.classList.add('row-new'); else tr.classList.remove('row-new'); });
}
function saveEmail(){
  const from=document.getElementById('emailFrom').value.trim();
  const pass=document.getElementById('emailPass').value.trim();
  const msgEl=document.getElementById('emailMsg'); const toVals=[];
  for(let i=1;i<=5;i++){ const v=(document.getElementById('emailTo'+i)||{}).value||''; if(v.trim()) toVals.push(v.trim()); }
  if(!from){msgEl.style.color='#f87171';msgEl.textContent='✗ 발신 주소를 입력하세요.';return;}
  if(!toVals.length){msgEl.style.color='#f87171';msgEl.textContent='✗ 수신자를 1명 이상 입력하세요.';return;}
  msgEl.style.color='#8b8fa8'; msgEl.textContent='저장 중...';
  const passParam=pass||'__keep__';
  let url='/api/28/email/save?from='+encodeURIComponent(from)+'&pass='+encodeURIComponent(passParam);
  toVals.forEach((v,i)=>{url+='&to'+(i+1)+'='+encodeURIComponent(v);});
  fetch(url).then(r=>r.json()).then(d=>{ msgEl.style.color=d.ok?'#4ade80':'#f87171'; msgEl.textContent=d.ok?`✓ 저장 완료 — 수신자 ${toVals.length}명`:('✗ '+(d.msg||'오류')); if(d.ok) loadEmailStatus(); });
}
function testEmail(){
  const btn=document.getElementById('testEmailBtn'); const msgEl=document.getElementById('emailMsg');
  btn.disabled=true; msgEl.style.color='#8b8fa8'; msgEl.textContent='📨 테스트 발송 중...';
  fetch('/api/28/email/test').then(r=>r.json()).then(d=>{ btn.disabled=false; msgEl.style.color=d.ok?'#4ade80':'#f87171'; msgEl.textContent=d.ok?'✓ 테스트 이메일 발송 완료!':'✗ 발송 실패: '+(d.msg||'알 수 없는 오류'); }).catch(()=>{btn.disabled=false;msgEl.textContent='네트워크 오류';});
}
function loadEmailStatus(){
  fetch('/api/28/email/status').then(r=>r.json()).then(s=>{
    const el=document.getElementById('emailStatus');
    if(s.from) document.getElementById('emailFrom').value=s.from;
    if(s.pass) document.getElementById('emailPass').value=s.pass;
    const toList=s.to_list||[];
    for(let i=1;i<=5;i++){ const inp=document.getElementById('emailTo'+i); if(inp) inp.value=toList[i-1]||''; }
    if(s.configured){ const toSummary=toList.map(v=>`<b>${v}</b>`).join(', '); el.innerHTML=`<span style="color:#4ade80">● 이메일 설정 완료</span>  발신: <b>${s.from}</b>  |  수신자 ${toList.length}명: ${toSummary}  |  앱비번: ${s.pass_hint}`; }
    else { el.innerHTML=`<span style="color:#f59e0b">⚠ 이메일 미설정</span>  — 발신 Gmail, 앱 비밀번호, 수신자를 입력하고 저장하세요.`; }
  });
}
fetch('/api/28/realtime/status').then(r=>r.json()).then(s=>{
  if(s.running){ setRtActive(true); document.getElementById('progWrap').style.display='block'; rtPoll(); if(s.result_count>0) rtLoadResult(s.new_count); }
});
loadEmailStatus();
"""


_S31_EXTRA_SECTION = """
<div style="margin-top:14px">
  <label style="font-size:.82rem;color:#8b8fa8;display:block;margin-bottom:6px">🔍 검색 모드 (FindMode)</label>
  <select id="findModeSelect"
    style="background:#13161f;border:1px solid #2a2d3a;border-radius:6px;color:#e0e0e0;
           padding:9px 14px;font-size:.86rem;outline:none;max-width:520px;width:100%">
    <option value="9">Mode 9 — 하단돌파 OR 중심선돌파 (복합 매수 신호)</option>
    <option value="10">Mode 10 — 상단밴드 상향돌파 (강세 돌입)</option>
    <option value="3">Mode 3 — 하단밴드 상향돌파 (과매도 탈출)</option>
    <option value="5">Mode 5 — 중심선 상향돌파 (상승 전환)</option>
    <option value="1">Mode 1 — 종가 &lt; 하단밴드 (과매도 체류)</option>
    <option value="7">Mode 7 — 종가 &lt; 하단 1/2선 (약세 체류)</option>
    <option value="8">Mode 8 — 종가 &gt; 상단 1/2선 (강세 체류)</option>
    <option value="2">Mode 2 — 종가 &gt; 상단밴드 (과매수 체류)</option>
    <option value="4">Mode 4 — 상단밴드 하향이탈 (과매수 탈출)</option>
    <option value="6">Mode 6 — 중심선 하향이탈 (하락 전환)</option>
  </select>
  <div style="margin-top:8px;font-size:.76rem;color:#4a4d5e">
    LenRSI=14 · UpperPct=70 · LowerPct=30 · expoLen=27 (EMA 기반 RSI 밴드 역산)
  </div>
</div>
"""

_S31_EXTRA_JS = r"""
/* ── 조건31 pro RSI: FindMode 파라미터 포함하여 스크리닝 시작 ── */
function startScan(){
  const d = document.getElementById('dateInput').value.replace(/-/g,'');
  if(!d) return;
  const fm = document.getElementById('findModeSelect').value;
  document.getElementById('runBtn').disabled = true;
  document.getElementById('dlBtn').classList.add('hidden');
  document.getElementById('resultCard').classList.add('hidden');
  document.getElementById('progWrap').style.display = 'block';
  document.getElementById('msg').textContent = '';
  setP(0,'종목 리스트 조회 중...');
  fetch(`/api/${SID}/start?date=${d}&find_mode=${fm}`).then(r=>r.json()).then(r=>{
    if(r.error){showMsg(r.error);resetBtn();return;}
    listenProg();
  });
}
"""


# ── 조건43 proRSI2: 조건31과 동일하나 LenRSI=30 (expoLen=59) ───────────────
_S43_EXTRA_SECTION = """
<div style="margin-top:14px">
  <label style="font-size:.82rem;color:#8b8fa8;display:block;margin-bottom:6px">🔍 검색 모드 (FindMode)</label>
  <select id="findModeSelect"
    style="background:#13161f;border:1px solid #2a2d3a;border-radius:6px;color:#e0e0e0;
           padding:9px 14px;font-size:.86rem;outline:none;max-width:520px;width:100%">
    <option value="9">Mode 9 — 하단돌파 OR 중심선돌파 (복합 매수 신호)</option>
    <option value="10">Mode 10 — 상단밴드 상향돌파 (강세 돌입)</option>
    <option value="3">Mode 3 — 하단밴드 상향돌파 (과매도 탈출)</option>
    <option value="5">Mode 5 — 중심선 상향돌파 (상승 전환)</option>
    <option value="1">Mode 1 — 종가 &lt; 하단밴드 (과매도 체류)</option>
    <option value="7">Mode 7 — 종가 &lt; 하단 1/2선 (약세 체류)</option>
    <option value="8">Mode 8 — 종가 &gt; 상단 1/2선 (강세 체류)</option>
    <option value="2">Mode 2 — 종가 &gt; 상단밴드 (과매수 체류)</option>
    <option value="4">Mode 4 — 상단밴드 하향이탈 (과매수 탈출)</option>
    <option value="6">Mode 6 — 중심선 하향이탈 (하락 전환)</option>
  </select>
  <div style="margin-top:8px;font-size:.76rem;color:#4a4d5e">
    LenRSI=30 · UpperPct=70 · LowerPct=30 · expoLen=59 (EMA 기반 RSI 밴드 역산)
  </div>
</div>
"""

_S43_EXTRA_JS = r"""
/* ── 조건43 proRSI2: FindMode 파라미터 포함하여 스크리닝 시작 ── */
function startScan(){
  const d = document.getElementById('dateInput').value.replace(/-/g,'');
  if(!d) return;
  const fm = document.getElementById('findModeSelect').value;
  document.getElementById('runBtn').disabled = true;
  document.getElementById('dlBtn').classList.add('hidden');
  document.getElementById('resultCard').classList.add('hidden');
  document.getElementById('progWrap').style.display = 'block';
  document.getElementById('msg').textContent = '';
  setP(0,'종목 리스트 조회 중...');
  fetch(`/api/${SID}/start?date=${d}&find_mode=${fm}`).then(r=>r.json()).then(r=>{
    if(r.error){showMsg(r.error);resetBtn();return;}
    listenProg();
  });
}
"""


_S48_EXTRA_SECTION = """
<div style="margin-top:14px">
  <label style="font-size:.82rem;color:#8b8fa8;display:block;margin-bottom:6px">검색 모드 (FindMode)</label>
  <select id="findModeSelect"
    style="background:#13161f;border:1px solid #2a2d3a;border-radius:6px;color:#e0e0e0;
           padding:9px 14px;font-size:.86rem;outline:none;max-width:520px;width:100%">
    <option value="0">Mode 0 - 골든크로스 + 양봉</option>
    <option value="1">Mode 1 - 데드크로스</option>
    <option value="2">Mode 2 - 골든/데드 양방향</option>
    <option value="3">Mode 3 - 현재 상승 추세</option>
    <option value="4">Mode 4 - 현재 하락 추세</option>
  </select>
  <div style="margin-top:8px;font-size:.76rem;color:#4a4d5e">
    Short Kalman=50 · Long Kalman=150 · 기본값은 골든크로스 + 양봉입니다.
  </div>
</div>
"""

_S48_EXTRA_JS = r"""
/* ── 조건48 칼만트렌드라인: FindMode 파라미터 포함하여 스크리닝 시작 ── */
function startScan(){
  const d = document.getElementById('dateInput').value.replace(/-/g,'');
  if(!d) return;
  const fm = document.getElementById('findModeSelect').value;
  document.getElementById('runBtn').disabled = true;
  document.getElementById('dlBtn').classList.add('hidden');
  document.getElementById('resultCard').classList.add('hidden');
  document.getElementById('progWrap').style.display = 'block';
  document.getElementById('msg').textContent = '';
  setP(0,'종목 리스트 조회 중...');
  fetch(`/api/${SID}/start?date=${d}&find_mode=${fm}`).then(r=>r.json()).then(r=>{
    if(r.error){showMsg(r.error);resetBtn();return;}
    listenProg();
  });
}
"""


# ── 조건44 Market shift levels: FindMode + 실시간 스캔 + 이메일 통합 UI ──────
_RT44_EXTRA_SECTION = """
<div style="margin-top:14px">
  <label style="font-size:.82rem;color:#8b8fa8;display:block;margin-bottom:6px">🌊 검색 모드 (FindMode)</label>
  <select id="findModeSelect"
    style="background:#13161f;border:1px solid #2a2d3a;border-radius:6px;color:#e0e0e0;
           padding:9px 14px;font-size:.86rem;outline:none;max-width:520px;width:100%">
    <option value="6">Mode 6 — 종가 레벨 상향돌파 + 양봉 (기본 · 가장 강한 매수 신호)</option>
    <option value="1">Mode 1 — 상승 반전 (저가가 레벨 아래 이탈 후 복귀)</option>
    <option value="3">Mode 3 — HMA 상향돌파 (새 지지 레벨 형성)</option>
    <option value="5">Mode 5 — 종가 레벨 위 (상승 추세 유지 종목 전체)</option>
    <option value="2">Mode 2 — 하락 반전 (고가가 레벨 위 돌파 후 복귀)</option>
    <option value="4">Mode 4 — HMA 하향이탈 (새 저항 레벨 형성)</option>
  </select>
  <div style="margin-top:6px;font-size:.75rem;color:#4a4d5e">
    HMA(55) = WMA( 2×WMA(C,27) − WMA(C,55) , 7 ) · myLevel: CrossUp→저가(지지), CrossDown→고가(저항) · 시총 5,000억↑ · ETF/ETN 제외
  </div>
</div>
<div style="margin-top:18px;border-top:1px solid #2a2d3a;padding-top:16px">
  <div style="font-size:.78rem;color:#10b981;margin-bottom:10px">
    🌊 Market shift levels 실시간 스캔 — 10분 간격 자동 반복 (선택한 FindMode 적용)
  </div>
  <div style="display:flex;gap:10px;flex-wrap:wrap;align-items:center;margin-bottom:12px">
    <button class="btn" id="rtStartBtn"
      style="background:#022c22;color:#10b981;border:1px solid #065f46"
      onclick="rtStart()">▶ 실시간 스캔 시작</button>
    <button class="btn hidden" id="rtStopBtn"
      style="background:#3a1010;color:#f87171;border:1px solid #5a2020"
      onclick="rtStop()">■ 중지</button>
    <span id="rtBadge" class="rt-badge inactive">● 대기 중</span>
    <span id="rtScanInfo" style="font-size:.78rem;color:#8b8fa8"></span>
  </div>
  <div style="background:#13161f;border:1px solid #2a2d3a;border-radius:10px;padding:14px 16px;margin-top:4px">
    <div style="font-size:.8rem;color:#8b8fa8;margin-bottom:10px">📧 이메일 알림 설정</div>
    <div style="display:flex;gap:8px;flex-wrap:wrap;align-items:center;margin-bottom:8px">
      <input type="email" id="emailFrom" placeholder="발신 Gmail 주소" autocomplete="off"
        style="background:#0f1117;border:1px solid #2a2d3a;border-radius:6px;
               color:#e0e0e0;padding:7px 11px;font-size:.8rem;width:220px">
      <input type="password" id="emailPass" placeholder="Gmail 앱 비밀번호 (16자리)" autocomplete="new-password"
        style="background:#0f1117;border:1px solid #2a2d3a;border-radius:6px;
               color:#e0e0e0;padding:7px 11px;font-size:.8rem;width:200px">
    </div>
    <div style="display:flex;gap:8px;flex-wrap:wrap;align-items:center;margin-bottom:10px">
      <span style="font-size:.78rem;color:#8b8fa8;white-space:nowrap">수신자 (최대 5명):</span>
      <input type="email" id="emailTo1" placeholder="수신자 1" autocomplete="off"
        style="background:#0f1117;border:1px solid #2a2d3a;border-radius:6px;
               color:#e0e0e0;padding:7px 10px;font-size:.8rem;width:190px">
      <input type="email" id="emailTo2" placeholder="수신자 2" autocomplete="off"
        style="background:#0f1117;border:1px solid #2a2d3a;border-radius:6px;
               color:#e0e0e0;padding:7px 10px;font-size:.8rem;width:190px">
      <input type="email" id="emailTo3" placeholder="수신자 3 (선택)" autocomplete="off"
        style="background:#0f1117;border:1px solid #2a2d3a;border-radius:6px;
               color:#e0e0e0;padding:7px 10px;font-size:.8rem;width:190px">
      <input type="email" id="emailTo4" placeholder="수신자 4 (선택)" autocomplete="off"
        style="background:#0f1117;border:1px solid #2a2d3a;border-radius:6px;
               color:#e0e0e0;padding:7px 10px;font-size:.8rem;width:190px">
      <input type="email" id="emailTo5" placeholder="수신자 5 (선택)" autocomplete="off"
        style="background:#0f1117;border:1px solid #2a2d3a;border-radius:6px;
               color:#e0e0e0;padding:7px 10px;font-size:.8rem;width:190px">
    </div>
    <div style="display:flex;gap:8px;flex-wrap:wrap;align-items:center">
      <button class="btn" onclick="saveEmail()"
        style="background:#022c22;color:#10b981;border:1px solid #065f46;padding:7px 18px">
        💾 저장</button>
      <button class="btn" id="testEmailBtn" onclick="testEmail()"
        style="background:#022c22;color:#10b981;border:1px solid #065f46;padding:7px 18px">
        📨 테스트 발송</button>
    </div>
    <div id="emailStatus" style="margin-top:8px;font-size:.78rem;color:#8b8fa8"></div>
    <div id="emailMsg"    style="margin-top:4px;font-size:.78rem"></div>
  </div>
  <div style="margin-top:8px;font-size:.72rem;color:#4a4d5e;line-height:1.7">
    ※ 스크리닝 시작 버튼: 일회성 조회 (날짜 + FindMode 기준)<br>
    ※ 실시간 스캔: 10분 간격, 오늘 날짜 기준, 선택된 FindMode 자동 적용<br>
    ※ 이메일: 이전 스캔 대비 신규 신호 종목 발견 시 자동 발송
  </div>
</div>
"""

_RT44_EXTRA_JS = r"""
/* ── 조건44 Market shift levels: FindMode + 실시간 스캔 컨트롤러 ── */
let rtPollTimer  = null;
let rtActive     = false;
let rtLastScanTs = '';

function startScan(){
  const d = document.getElementById('dateInput').value.replace(/-/g,'');
  if(!d) return;
  const fm = document.getElementById('findModeSelect').value;
  document.getElementById('runBtn').disabled = true;
  document.getElementById('dlBtn').classList.add('hidden');
  document.getElementById('resultCard').classList.add('hidden');
  document.getElementById('progWrap').style.display = 'block';
  document.getElementById('msg').textContent = '';
  setP(0,'종목 리스트 조회 중...');
  fetch(`/api/${SID}/start?date=${d}&find_mode=${fm}`).then(r=>r.json()).then(r=>{
    if(r.error){showMsg(r.error);resetBtn();return;}
    listenProg();
  });
}
function rtStart(){
  const fm = document.getElementById('findModeSelect').value;
  fetch(`/api/44/realtime/start?find_mode=${fm}`).then(r=>r.json()).then(d=>{
    if(!d.ok){ showMsg(d.msg||'오류 발생'); return; }
    setRtActive(true);
    document.getElementById('progWrap').style.display = 'block';
    setP(0, '첫 번째 스캔 준비 중...');
    showMsg('실시간 스캔 시작됨 — 10분 간격으로 자동 반복됩니다.');
    rtLastScanTs = '';
    rtPoll();
  });
}
function rtStop(){
  fetch('/api/44/realtime/stop').then(r=>r.json()).then(()=>{
    setRtActive(false);
    if(rtPollTimer){ clearInterval(rtPollTimer); rtPollTimer = null; }
    showMsg('실시간 스캔 중지됨');
    setP(0,'');
    document.getElementById('progWrap').style.display = 'none';
  });
}
function setRtActive(v){
  rtActive = v;
  document.getElementById('rtStartBtn').classList.toggle('hidden', v);
  document.getElementById('rtStopBtn').classList.toggle('hidden', !v);
  const badge = document.getElementById('rtBadge');
  if(v){ badge.textContent='● 실시간 스캔 중'; badge.classList.remove('inactive'); }
  else { badge.textContent='● 대기 중';         badge.classList.add('inactive'); }
}
function rtPoll(){
  if(rtPollTimer) clearInterval(rtPollTimer);
  rtPollTimer = setInterval(()=>{
    if(!rtActive){ clearInterval(rtPollTimer); return; }
    fetch('/api/44/realtime/status').then(r=>r.json()).then(s=>{
      if(!s.running && rtActive){ setRtActive(false); clearInterval(rtPollTimer); return; }
      function fmtElapsed(sec){ if(!sec) return ''; const m=Math.floor(sec/60),s2=sec%60; return m>0?`${m}분 ${s2}초`:`${s2}초`; }
      const infoEl=document.getElementById('rtScanInfo');
      if(s.scan_no>0){
        const pst=s.prog_status; let info=`스캔 #${s.scan_no}`;
        if(pst==='loading'||pst==='running'){ info+=`  |  시작: ${s.scan_start||'—'}  |  조회 중... (${s.prog_current}/${s.prog_total})`; }
        else{ const el=s.scan_elapsed?`  소요: ${fmtElapsed(s.scan_elapsed)}`:''; info+=`  |  완료: ${s.last_scan||'—'}${el}`; if(s.next_scan) info+=`  |  다음: ${s.next_scan}`; }
        infoEl.textContent=info;
      }
      const pst=s.prog_status;
      if(pst==='loading'||pst==='running'){
        const pct = scanProgressPct(s.prog_current, s.prog_total);
        setP(pct, scanProgressText(s.prog_current, s.prog_total, '스캔 중'));
      }
      else if(pst==='done'){ const el=s.scan_elapsed?`  소요 ${fmtElapsed(s.scan_elapsed)}`:''; setP(100,`완료 ✓  ${s.result_count}종목${el}`); }
      const justDone=(pst==='done')&&s.last_scan&&(s.last_scan!==rtLastScanTs);
      if(justDone||(pst==='done'&&s.result_count>=0&&!rtLastScanTs)){ rtLastScanTs=s.last_scan||'_init_'; rtLoadResult(s.new_count); }
    });
  },3000);
}
function rtLoadResult(newCount){
  fetch('/api/44/result').then(r=>r.json()).then(d=>{
    if(!d.rows||d.rows.length===0){ showMsg(`✓ 스캔 완료 — 조건 충족 종목 없음`); return; }
    document.getElementById('resultCard').classList.remove('hidden');
    document.getElementById('dlBtn').classList.remove('hidden');
    renderResult(d.date,d.rows);
    highlightNewRows(d.new_codes||[]);
    const nc=d.new_codes&&d.new_codes.length;
    if(nc) showMsg(`🔔 신규 신호 ${nc}종목!  총 ${d.rows.length}종목  (${new Date().toLocaleTimeString()})`);
    else showMsg(`✓ ${d.rows.length}종목 스캔 완료  (${new Date().toLocaleTimeString()})`);
  });
}
function highlightNewRows(newCodes){
  document.querySelectorAll('#tables tr').forEach(tr=>{
    if(!tr.cells||tr.cells.length<2) return;
    const code=tr.cells[1].textContent.trim();
    if(newCodes.includes(code)) tr.classList.add('row-new');
    else tr.classList.remove('row-new');
  });
}
function saveEmail(){
  const from=document.getElementById('emailFrom').value.trim();
  const pass=document.getElementById('emailPass').value.trim();
  const msgEl=document.getElementById('emailMsg'); const toVals=[];
  for(let i=1;i<=5;i++){ const v=(document.getElementById('emailTo'+i)||{}).value||''; if(v.trim()) toVals.push(v.trim()); }
  if(!from){msgEl.style.color='#f87171';msgEl.textContent='✗ 발신 주소를 입력하세요.';return;}
  if(!toVals.length){msgEl.style.color='#f87171';msgEl.textContent='✗ 수신자를 1명 이상 입력하세요.';return;}
  msgEl.style.color='#8b8fa8'; msgEl.textContent='저장 중...';
  const passParam=pass||'__keep__';
  let url='/api/44/email/save?from='+encodeURIComponent(from)+'&pass='+encodeURIComponent(passParam);
  toVals.forEach((v,i)=>{url+='&to'+(i+1)+'='+encodeURIComponent(v);});
  fetch(url).then(r=>r.json()).then(d=>{ msgEl.style.color=d.ok?'#4ade80':'#f87171'; msgEl.textContent=d.ok?`✓ 저장 완료 — 수신자 ${toVals.length}명`:('✗ '+(d.msg||'오류')); if(d.ok) loadEmailStatus(); });
}
function testEmail(){
  const btn=document.getElementById('testEmailBtn'); const msgEl=document.getElementById('emailMsg');
  btn.disabled=true; msgEl.style.color='#8b8fa8'; msgEl.textContent='📨 테스트 발송 중...';
  fetch('/api/44/email/test').then(r=>r.json()).then(d=>{ btn.disabled=false; msgEl.style.color=d.ok?'#4ade80':'#f87171'; msgEl.textContent=d.ok?'✓ 테스트 이메일 발송 완료!':'✗ 발송 실패: '+(d.msg||'알 수 없는 오류'); }).catch(()=>{btn.disabled=false;msgEl.textContent='네트워크 오류';});
}
function loadEmailStatus(){
  fetch('/api/44/email/status').then(r=>r.json()).then(s=>{
    const el=document.getElementById('emailStatus');
    if(s.from) document.getElementById('emailFrom').value=s.from;
    if(s.pass) document.getElementById('emailPass').value=s.pass;
    const toList=s.to_list||[];
    for(let i=1;i<=5;i++){ const inp=document.getElementById('emailTo'+i); if(inp) inp.value=toList[i-1]||''; }
    if(s.configured){ const toSummary=toList.map(v=>`<b>${v}</b>`).join(', '); el.innerHTML=`<span style="color:#4ade80">● 이메일 설정 완료</span>  발신: <b>${s.from}</b>  |  수신자 ${toList.length}명: ${toSummary}  |  앱비번: ${s.pass_hint}`; }
    else { el.innerHTML=`<span style="color:#10b981">⚠ 이메일 미설정</span>  — 발신 Gmail, 앱 비밀번호, 수신자를 입력하고 저장하세요.`; }
  });
}
fetch('/api/44/realtime/status').then(r=>r.json()).then(s=>{
  if(s.running){ setRtActive(true); document.getElementById('progWrap').style.display='block'; rtPoll(); if(s.result_count>0) rtLoadResult(s.new_count); }
});
loadEmailStatus();
"""


# ── 조건46 MSL2: 실시간 스캔 + 이메일 통합 UI ────────────────────────────────
_RT46_EXTRA_SECTION = """
<div style="margin-top:18px;border-top:1px solid #2a2d3a;padding-top:16px">
  <div style="display:flex;gap:10px;flex-wrap:wrap;align-items:center;margin-bottom:12px">
    <button class="btn" id="rtStartBtn"
      style="background:#1a1200;color:#f59e0b;border:1px solid #5a3a00"
      onclick="rtStart()">▶ 실시간 스캔 시작</button>
    <button class="btn hidden" id="rtStopBtn"
      style="background:#3a1a00;color:#f87171;border:1px solid #5a2010"
      onclick="rtStop()">■ 중지</button>
    <span id="rtBadge" class="rt-badge inactive">● 대기 중</span>
    <span id="rtScanInfo" style="font-size:.78rem;color:#8b8fa8"></span>
  </div>
  <div style="background:#13161f;border:1px solid #2a2d3a;border-radius:10px;padding:14px 16px;margin-top:4px">
    <div style="font-size:.8rem;color:#8b8fa8;margin-bottom:10px">📧 이메일 알림 설정</div>
    <div style="display:flex;gap:8px;flex-wrap:wrap;align-items:center;margin-bottom:8px">
      <input type="email" id="emailFrom" placeholder="발신 Gmail 주소" autocomplete="off"
        style="background:#0f1117;border:1px solid #2a2d3a;border-radius:6px;
               color:#e0e0e0;padding:7px 11px;font-size:.8rem;width:220px">
      <input type="password" id="emailPass" placeholder="Gmail 앱 비밀번호 (16자리)" autocomplete="new-password"
        style="background:#0f1117;border:1px solid #2a2d3a;border-radius:6px;
               color:#e0e0e0;padding:7px 11px;font-size:.8rem;width:200px">
    </div>
    <div style="display:flex;gap:8px;flex-wrap:wrap;align-items:center;margin-bottom:10px">
      <span style="font-size:.78rem;color:#8b8fa8;white-space:nowrap">수신자 (최대 5명):</span>
      <input type="email" id="emailTo1" placeholder="수신자 1" autocomplete="off"
        style="background:#0f1117;border:1px solid #2a2d3a;border-radius:6px;
               color:#e0e0e0;padding:7px 10px;font-size:.8rem;width:190px">
      <input type="email" id="emailTo2" placeholder="수신자 2" autocomplete="off"
        style="background:#0f1117;border:1px solid #2a2d3a;border-radius:6px;
               color:#e0e0e0;padding:7px 10px;font-size:.8rem;width:190px">
      <input type="email" id="emailTo3" placeholder="수신자 3 (선택)" autocomplete="off"
        style="background:#0f1117;border:1px solid #2a2d3a;border-radius:6px;
               color:#e0e0e0;padding:7px 10px;font-size:.8rem;width:190px">
      <input type="email" id="emailTo4" placeholder="수신자 4 (선택)" autocomplete="off"
        style="background:#0f1117;border:1px solid #2a2d3a;border-radius:6px;
               color:#e0e0e0;padding:7px 10px;font-size:.8rem;width:190px">
      <input type="email" id="emailTo5" placeholder="수신자 5 (선택)" autocomplete="off"
        style="background:#0f1117;border:1px solid #2a2d3a;border-radius:6px;
               color:#e0e0e0;padding:7px 10px;font-size:.8rem;width:190px">
    </div>
    <div style="display:flex;gap:8px;flex-wrap:wrap;align-items:center">
      <button class="btn" onclick="saveEmail()"
        style="background:#1e1200;color:#f59e0b;border:1px solid #5a3a00;padding:7px 18px">
        💾 저장</button>
      <button class="btn" id="testEmailBtn" onclick="testEmail()"
        style="background:#0a1e18;color:#34d399;border:1px solid #1a5a40;padding:7px 18px">
        📨 테스트 발송</button>
    </div>
    <div id="emailStatus" style="margin-top:8px;font-size:.78rem;color:#8b8fa8"></div>
    <div id="emailMsg"    style="margin-top:4px;font-size:.78rem"></div>
  </div>
</div>
"""

_RT46_EXTRA_JS = r"""
/* ── 조건46 MSL2: 실시간 스캔 컨트롤러 ── */
let rtPollTimer  = null;
let rtActive     = false;
let rtLastScanTs = '';

function rtStart(){
  fetch('/api/46/realtime/start').then(r=>r.json()).then(d=>{
    if(!d.ok){ showMsg(d.msg||'오류 발생'); return; }
    setRtActive(true);
    document.getElementById('progWrap').style.display = 'block';
    setP(0, '첫 번째 스캔 준비 중...');
    showMsg('실시간 스캔 시작됨 — 10분 간격으로 자동 반복됩니다.');
    rtLastScanTs = '';
    rtPoll();
  });
}
function rtStop(){
  fetch('/api/46/realtime/stop').then(r=>r.json()).then(()=>{
    setRtActive(false);
    if(rtPollTimer){ clearInterval(rtPollTimer); rtPollTimer = null; }
    showMsg('실시간 스캔 중지됨');
    setP(0,'');
    document.getElementById('progWrap').style.display = 'none';
  });
}
function setRtActive(v){
  rtActive = v;
  document.getElementById('rtStartBtn').classList.toggle('hidden', v);
  document.getElementById('rtStopBtn').classList.toggle('hidden', !v);
  const badge = document.getElementById('rtBadge');
  if(v){ badge.textContent='● 실시간 스캔 중'; badge.classList.remove('inactive'); }
  else { badge.textContent='● 대기 중';         badge.classList.add('inactive'); }
}
function rtPoll(){
  if(rtPollTimer) clearInterval(rtPollTimer);
  rtPollTimer = setInterval(()=>{
    if(!rtActive){ clearInterval(rtPollTimer); return; }
    fetch('/api/46/realtime/status').then(r=>r.json()).then(s=>{
      if(!s.running && rtActive){ setRtActive(false); clearInterval(rtPollTimer); return; }
      function fmtElapsed(sec){ if(!sec) return ''; const m=Math.floor(sec/60),s2=sec%60; return m>0?`${m}분 ${s2}초`:`${s2}초`; }
      const infoEl=document.getElementById('rtScanInfo');
      if(s.scan_no>0){
        const pst=s.prog_status; let info=`스캔 #${s.scan_no}`;
        if(pst==='loading'||pst==='running'){ info+=`  |  시작: ${s.scan_start||'—'}  |  조회 중... (${s.prog_current}/${s.prog_total})`; }
        else{ const el=s.scan_elapsed?`  소요: ${fmtElapsed(s.scan_elapsed)}`:''; info+=`  |  완료: ${s.last_scan||'—'}${el}`; if(s.next_scan) info+=`  |  다음: ${s.next_scan}`; }
        infoEl.textContent=info;
      }
      const pst=s.prog_status;
      if(pst==='loading'||pst==='running'){
        const pct = scanProgressPct(s.prog_current, s.prog_total);
        setP(pct, scanProgressText(s.prog_current, s.prog_total, '스캔 중'));
      }
      else if(pst==='done'){ const el=s.scan_elapsed?`  소요 ${fmtElapsed(s.scan_elapsed)}`:''; setP(100,`완료 ✓  ${s.result_count}종목${el}`); }
      const justDone=(pst==='done')&&s.last_scan&&(s.last_scan!==rtLastScanTs);
      if(justDone||(pst==='done'&&s.result_count>=0&&!rtLastScanTs)){ rtLastScanTs=s.last_scan||'_init_'; rtLoadResult(s.new_count); }
    });
  },3000);
}
function rtLoadResult(newCount){
  fetch('/api/46/result').then(r=>r.json()).then(d=>{
    if(!d.rows||d.rows.length===0){ showMsg(`✓ 스캔 완료 — 조건 충족 종목 없음`); return; }
    document.getElementById('resultCard').classList.remove('hidden');
    document.getElementById('dlBtn').classList.remove('hidden');
    renderResult(d.date,d.rows);
    highlightNewRows(d.new_codes||[]);
    const nc=d.new_codes&&d.new_codes.length;
    if(nc) showMsg(`🔔 신규 레벨돌파 ${nc}종목!  총 ${d.rows.length}종목  (${new Date().toLocaleTimeString()})`);
    else showMsg(`✓ ${d.rows.length}종목 스캔 완료  (${new Date().toLocaleTimeString()})`);
  });
}
function highlightNewRows(newCodes){
  document.querySelectorAll('#tables tr').forEach(tr=>{
    if(!tr.cells||tr.cells.length<2) return;
    const code=tr.cells[1].textContent.trim();
    if(newCodes.includes(code)) tr.classList.add('row-new');
    else tr.classList.remove('row-new');
  });
}
function saveEmail(){
  const from=document.getElementById('emailFrom').value.trim();
  const pass=document.getElementById('emailPass').value.trim();
  const msgEl=document.getElementById('emailMsg'); const toVals=[];
  for(let i=1;i<=5;i++){ const v=(document.getElementById('emailTo'+i)||{}).value||''; if(v.trim()) toVals.push(v.trim()); }
  if(!from){msgEl.style.color='#f87171';msgEl.textContent='✗ 발신 주소를 입력하세요.';return;}
  if(!toVals.length){msgEl.style.color='#f87171';msgEl.textContent='✗ 수신자를 1명 이상 입력하세요.';return;}
  msgEl.style.color='#8b8fa8'; msgEl.textContent='저장 중...';
  const passParam=pass||'__keep__';
  let url='/api/46/email/save?from='+encodeURIComponent(from)+'&pass='+encodeURIComponent(passParam);
  toVals.forEach((v,i)=>{url+='&to'+(i+1)+'='+encodeURIComponent(v);});
  fetch(url).then(r=>r.json()).then(d=>{ msgEl.style.color=d.ok?'#4ade80':'#f87171'; msgEl.textContent=d.ok?`✓ 저장 완료 — 수신자 ${toVals.length}명`:('✗ '+(d.msg||'오류')); if(d.ok) loadEmailStatus(); });
}
function testEmail(){
  const btn=document.getElementById('testEmailBtn'); const msgEl=document.getElementById('emailMsg');
  btn.disabled=true; msgEl.style.color='#8b8fa8'; msgEl.textContent='📨 테스트 발송 중...';
  fetch('/api/46/email/test').then(r=>r.json()).then(d=>{ btn.disabled=false; msgEl.style.color=d.ok?'#4ade80':'#f87171'; msgEl.textContent=d.ok?'✓ 테스트 이메일 발송 완료!':'✗ 발송 실패: '+(d.msg||'알 수 없는 오류'); }).catch(()=>{btn.disabled=false;msgEl.textContent='네트워크 오류';});
}
function loadEmailStatus(){
  fetch('/api/46/email/status').then(r=>r.json()).then(s=>{
    const el=document.getElementById('emailStatus');
    if(s.from) document.getElementById('emailFrom').value=s.from;
    if(s.pass) document.getElementById('emailPass').value=s.pass;
    const toList=s.to_list||[];
    for(let i=1;i<=5;i++){ const inp=document.getElementById('emailTo'+i); if(inp) inp.value=toList[i-1]||''; }
    if(s.configured){ const toSummary=toList.map(v=>`<b>${v}</b>`).join(', '); el.innerHTML=`<span style="color:#4ade80">● 이메일 설정 완료</span>  발신: <b>${s.from}</b>  |  수신자 ${toList.length}명: ${toSummary}  |  앱비번: ${s.pass_hint}`; }
    else { el.innerHTML=`<span style="color:#f59e0b">⚠ 이메일 미설정</span>  — 발신 Gmail, 앱 비밀번호, 수신자를 입력하고 저장하세요.`; }
  });
}
fetch('/api/46/realtime/status').then(r=>r.json()).then(s=>{
  if(s.running){ setRtActive(true); document.getElementById('progWrap').style.display='block'; rtPoll(); if(s.result_count>0) rtLoadResult(s.new_count); }
});
loadEmailStatus();
"""


_RT30_EXTRA_SECTION = """
<div style="margin-top:18px;border-top:1px solid #2a2d3a;padding-top:16px">
  <div style="display:flex;gap:10px;flex-wrap:wrap;align-items:center;margin-bottom:12px">
    <button class="btn" id="rtStartBtn"
      style="background:#0a1e18;color:#34d399;border:1px solid #1a5a40"
      onclick="rtStart()">▶ 실시간 스캔 시작</button>
    <button class="btn hidden" id="rtStopBtn"
      style="background:#3a1a00;color:#f87171;border:1px solid #5a2010"
      onclick="rtStop()">■ 중지</button>
    <span id="rtBadge" class="rt-badge inactive">● 대기 중</span>
    <span id="rtScanInfo" style="font-size:.78rem;color:#8b8fa8"></span>
  </div>
  <div style="background:#13161f;border:1px solid #2a2d3a;border-radius:10px;padding:14px 16px;margin-top:4px">
    <div style="font-size:.8rem;color:#8b8fa8;margin-bottom:10px">📧 이메일 알림 설정</div>
    <div style="display:flex;gap:8px;flex-wrap:wrap;align-items:center;margin-bottom:8px">
      <input type="email" id="emailFrom" placeholder="발신 Gmail 주소" autocomplete="off"
        style="background:#0f1117;border:1px solid #2a2d3a;border-radius:6px;
               color:#e0e0e0;padding:7px 11px;font-size:.8rem;width:220px">
      <input type="password" id="emailPass" placeholder="Gmail 앱 비밀번호 (16자리)" autocomplete="new-password"
        style="background:#0f1117;border:1px solid #2a2d3a;border-radius:6px;
               color:#e0e0e0;padding:7px 11px;font-size:.8rem;width:200px">
    </div>
    <div style="display:flex;gap:8px;flex-wrap:wrap;align-items:center;margin-bottom:10px">
      <span style="font-size:.78rem;color:#8b8fa8;white-space:nowrap">수신자 (최대 5명):</span>
      <input type="email" id="emailTo1" placeholder="수신자 1" autocomplete="off"
        style="background:#0f1117;border:1px solid #2a2d3a;border-radius:6px;
               color:#e0e0e0;padding:7px 10px;font-size:.8rem;width:190px">
      <input type="email" id="emailTo2" placeholder="수신자 2" autocomplete="off"
        style="background:#0f1117;border:1px solid #2a2d3a;border-radius:6px;
               color:#e0e0e0;padding:7px 10px;font-size:.8rem;width:190px">
      <input type="email" id="emailTo3" placeholder="수신자 3 (선택)" autocomplete="off"
        style="background:#0f1117;border:1px solid #2a2d3a;border-radius:6px;
               color:#e0e0e0;padding:7px 10px;font-size:.8rem;width:190px">
      <input type="email" id="emailTo4" placeholder="수신자 4 (선택)" autocomplete="off"
        style="background:#0f1117;border:1px solid #2a2d3a;border-radius:6px;
               color:#e0e0e0;padding:7px 10px;font-size:.8rem;width:190px">
      <input type="email" id="emailTo5" placeholder="수신자 5 (선택)" autocomplete="off"
        style="background:#0f1117;border:1px solid #2a2d3a;border-radius:6px;
               color:#e0e0e0;padding:7px 10px;font-size:.8rem;width:190px">
    </div>
    <div style="display:flex;gap:8px;flex-wrap:wrap;align-items:center">
      <button class="btn" onclick="saveEmail()"
        style="background:#1e2a4a;color:#4f8ef7;border:1px solid #2a3a6a;padding:7px 18px">
        💾 저장</button>
      <button class="btn" id="testEmailBtn" onclick="testEmail()"
        style="background:#0a1e18;color:#34d399;border:1px solid #1a5a40;padding:7px 18px">
        📨 테스트 발송</button>
    </div>
    <div id="emailStatus" style="margin-top:8px;font-size:.78rem;color:#8b8fa8"></div>
    <div id="emailMsg"    style="margin-top:4px;font-size:.78rem"></div>
  </div>
</div>
"""

_RT30_EXTRA_JS = r"""
/* ── 조건30 실시간 스캔 컨트롤러 ── */
let rtPollTimer  = null;
let rtActive     = false;
let rtLastScanTs = '';

function rtStart(){
  fetch('/api/30/realtime/start').then(r=>r.json()).then(d=>{
    if(!d.ok){ showMsg(d.msg||'오류 발생'); return; }
    setRtActive(true);
    document.getElementById('progWrap').style.display = 'block';
    setP(0, '첫 번째 스캔 준비 중...');
    showMsg('실시간 스캔 시작됨 — 3분 간격으로 자동 반복됩니다.');
    rtLastScanTs = '';
    rtPoll();
  });
}
function rtStop(){
  fetch('/api/30/realtime/stop').then(r=>r.json()).then(()=>{
    setRtActive(false);
    if(rtPollTimer){ clearInterval(rtPollTimer); rtPollTimer = null; }
    showMsg('실시간 스캔 중지됨');
    setP(0,'');
    document.getElementById('progWrap').style.display = 'none';
  });
}
function setRtActive(v){
  rtActive = v;
  document.getElementById('rtStartBtn').classList.toggle('hidden', v);
  document.getElementById('rtStopBtn').classList.toggle('hidden', !v);
  const badge = document.getElementById('rtBadge');
  if(v){ badge.textContent='● 실시간 스캔 중'; badge.classList.remove('inactive'); }
  else { badge.textContent='● 대기 중';         badge.classList.add('inactive'); }
}
function rtPoll(){
  if(rtPollTimer) clearInterval(rtPollTimer);
  rtPollTimer = setInterval(()=>{
    if(!rtActive){ clearInterval(rtPollTimer); return; }
    fetch('/api/30/realtime/status').then(r=>r.json()).then(s=>{
      if(!s.running && rtActive){ setRtActive(false); clearInterval(rtPollTimer); return; }
      function fmtElapsed(sec){ if(!sec) return ''; const m=Math.floor(sec/60),s2=sec%60; return m>0?`${m}분 ${s2}초`:`${s2}초`; }
      const infoEl=document.getElementById('rtScanInfo');
      if(s.scan_no>0){
        const pst=s.prog_status; let info=`스캔 #${s.scan_no}`;
        if(pst==='loading'||pst==='running'){ info+=`  |  시작: ${s.scan_start||'—'}  |  진행 중...`; }
        else{ const el=s.scan_elapsed?`  소요: ${fmtElapsed(s.scan_elapsed)}`:''; info+=`  |  시작: ${s.scan_start||'—'}  →  완료: ${s.last_scan||'—'}${el}`; if(s.next_scan) info+=`  |  다음 스캔: ${s.next_scan}`; }
        infoEl.textContent=info;
      }
      const pst=s.prog_status;
      if(pst==='loading') setP(0,'종목 리스트 불러오는 중...');
      else if(pst==='running'){ const pct=s.prog_total>0?Math.round(s.prog_current/s.prog_total*100):0; setP(pct, scanProgressText(s.prog_current, s.prog_total, '스캔 중')); }
      else if(pst==='done'){ const el=s.scan_elapsed?`  소요 ${fmtElapsed(s.scan_elapsed)}`:''; setP(100,`스캔 완료 ✓  ${s.result_count}개 종목 발견${el}`); }
      const justDone=(pst==='done')&&s.last_scan&&(s.last_scan!==rtLastScanTs);
      if(justDone||(pst==='done'&&s.result_count>0&&!rtLastScanTs)){ rtLastScanTs=s.last_scan||'_init_'; rtLoadResult(s.new_count); }
    });
  },5000);
}
function rtLoadResult(newCount){
  fetch('/api/30/result').then(r=>r.json()).then(d=>{
    if(!d.rows||d.rows.length===0){ showMsg(`✓ 스캔 완료 — 조건 충족 종목 없음 (${new Date().toLocaleTimeString()})`); return; }
    document.getElementById('resultCard').classList.remove('hidden');
    document.getElementById('dlBtn').classList.remove('hidden');
    renderResult(d.date,d.rows);
    highlightNewRows(d.new_codes||[]);
    if(d.new_codes&&d.new_codes.length>0) showMsg(`🔔 신규 ${d.new_codes.length}개 종목 발견!  총 ${d.rows.length}개  (${new Date().toLocaleTimeString()})`);
    else showMsg(`✓ 스캔 완료 — ${d.rows.length}개 종목 (${new Date().toLocaleTimeString()})`);
  });
}
function highlightNewRows(newCodes){
  document.querySelectorAll('#tables tr').forEach(tr=>{ if(!tr.cells||tr.cells.length<2) return; const code=tr.cells[1].textContent.trim(); if(newCodes.includes(code)) tr.classList.add('row-new'); else tr.classList.remove('row-new'); });
}
function saveEmail(){
  const from=document.getElementById('emailFrom').value.trim();
  const pass=document.getElementById('emailPass').value.trim();
  const msgEl=document.getElementById('emailMsg'); const toVals=[];
  for(let i=1;i<=5;i++){ const v=(document.getElementById('emailTo'+i)||{}).value||''; if(v.trim()) toVals.push(v.trim()); }
  if(!from){msgEl.style.color='#f87171';msgEl.textContent='✗ 발신 주소를 입력하세요.';return;}
  if(!toVals.length){msgEl.style.color='#f87171';msgEl.textContent='✗ 수신자를 1명 이상 입력하세요.';return;}
  msgEl.style.color='#8b8fa8'; msgEl.textContent='저장 중...';
  const passParam=pass||'__keep__';
  let url='/api/30/email/save?from='+encodeURIComponent(from)+'&pass='+encodeURIComponent(passParam);
  toVals.forEach((v,i)=>{url+='&to'+(i+1)+'='+encodeURIComponent(v);});
  fetch(url).then(r=>r.json()).then(d=>{ msgEl.style.color=d.ok?'#4ade80':'#f87171'; msgEl.textContent=d.ok?`✓ 저장 완료 — 수신자 ${toVals.length}명`:('✗ '+(d.msg||'오류')); if(d.ok) loadEmailStatus(); });
}
function testEmail(){
  const btn=document.getElementById('testEmailBtn'); const msgEl=document.getElementById('emailMsg');
  btn.disabled=true; msgEl.style.color='#8b8fa8'; msgEl.textContent='📨 테스트 발송 중...';
  fetch('/api/30/email/test').then(r=>r.json()).then(d=>{ btn.disabled=false; msgEl.style.color=d.ok?'#4ade80':'#f87171'; msgEl.textContent=d.ok?'✓ 테스트 이메일 발송 완료!':'✗ 발송 실패: '+(d.msg||'알 수 없는 오류'); }).catch(()=>{btn.disabled=false;msgEl.textContent='네트워크 오류';});
}
function loadEmailStatus(){
  fetch('/api/30/email/status').then(r=>r.json()).then(s=>{
    const el=document.getElementById('emailStatus');
    if(s.from) document.getElementById('emailFrom').value=s.from;
    if(s.pass) document.getElementById('emailPass').value=s.pass;
    const toList=s.to_list||[];
    for(let i=1;i<=5;i++){ const inp=document.getElementById('emailTo'+i); if(inp) inp.value=toList[i-1]||''; }
    if(s.configured){ const toSummary=toList.map(v=>`<b>${v}</b>`).join(', '); el.innerHTML=`<span style="color:#4ade80">● 이메일 설정 완료</span>  발신: <b>${s.from}</b>  |  수신자 ${toList.length}명: ${toSummary}  |  앱비번: ${s.pass_hint}`; }
    else { el.innerHTML=`<span style="color:#f59e0b">⚠ 이메일 미설정</span>  — 발신 Gmail, 앱 비밀번호, 수신자를 입력하고 저장하세요.`; }
  });
}
fetch('/api/30/realtime/status').then(r=>r.json()).then(s=>{
  if(s.running){ setRtActive(true); document.getElementById('progWrap').style.display='block'; rtPoll(); if(s.result_count>0) rtLoadResult(s.new_count); }
});
loadEmailStatus();
"""


_RT33_EXTRA_SECTION = """
<div style="margin-top:18px;border-top:1px solid #2a2d3a;padding-top:16px">
  <div style="display:flex;gap:10px;flex-wrap:wrap;align-items:center;margin-bottom:12px">
    <button class="btn" id="rtStartBtn"
      style="background:#2a0a12;color:#fb7185;border:1px solid #6a1a2a"
      onclick="rtStart()">▶ 실시간 스캔 시작</button>
    <button class="btn hidden" id="rtStopBtn"
      style="background:#3a1020;color:#f87171;border:1px solid #5a2050"
      onclick="rtStop()">■ 중지</button>
    <span id="rtBadge" class="rt-badge inactive">● 대기 중</span>
    <span id="rtScanInfo" style="font-size:.78rem;color:#8b8fa8"></span>
  </div>
  <div style="background:#13161f;border:1px solid #2a2d3a;border-radius:10px;padding:14px 16px;margin-top:4px">
    <div style="font-size:.8rem;color:#8b8fa8;margin-bottom:10px">📧 이메일 알림 설정</div>
    <div style="display:flex;gap:8px;flex-wrap:wrap;align-items:center;margin-bottom:8px">
      <input type="email" id="emailFrom" placeholder="발신 Gmail 주소" autocomplete="off"
        style="background:#0f1117;border:1px solid #2a2d3a;border-radius:6px;
               color:#e0e0e0;padding:7px 11px;font-size:.8rem;width:220px">
      <input type="password" id="emailPass" placeholder="Gmail 앱 비밀번호 (16자리)" autocomplete="new-password"
        style="background:#0f1117;border:1px solid #2a2d3a;border-radius:6px;
               color:#e0e0e0;padding:7px 11px;font-size:.8rem;width:200px">
    </div>
    <div style="display:flex;gap:8px;flex-wrap:wrap;align-items:center;margin-bottom:10px">
      <span style="font-size:.78rem;color:#8b8fa8;white-space:nowrap">수신자 (최대 5명):</span>
      <input type="email" id="emailTo1" placeholder="수신자 1" autocomplete="off"
        style="background:#0f1117;border:1px solid #2a2d3a;border-radius:6px;
               color:#e0e0e0;padding:7px 10px;font-size:.8rem;width:190px">
      <input type="email" id="emailTo2" placeholder="수신자 2" autocomplete="off"
        style="background:#0f1117;border:1px solid #2a2d3a;border-radius:6px;
               color:#e0e0e0;padding:7px 10px;font-size:.8rem;width:190px">
      <input type="email" id="emailTo3" placeholder="수신자 3 (선택)" autocomplete="off"
        style="background:#0f1117;border:1px solid #2a2d3a;border-radius:6px;
               color:#e0e0e0;padding:7px 10px;font-size:.8rem;width:190px">
      <input type="email" id="emailTo4" placeholder="수신자 4 (선택)" autocomplete="off"
        style="background:#0f1117;border:1px solid #2a2d3a;border-radius:6px;
               color:#e0e0e0;padding:7px 10px;font-size:.8rem;width:190px">
      <input type="email" id="emailTo5" placeholder="수신자 5 (선택)" autocomplete="off"
        style="background:#0f1117;border:1px solid #2a2d3a;border-radius:6px;
               color:#e0e0e0;padding:7px 10px;font-size:.8rem;width:190px">
    </div>
    <div style="display:flex;gap:8px;flex-wrap:wrap;align-items:center">
      <button class="btn" onclick="saveEmail()"
        style="background:#1e2a4a;color:#4f8ef7;border:1px solid #2a3a6a;padding:7px 18px">
        💾 저장</button>
      <button class="btn" id="testEmailBtn" onclick="testEmail()"
        style="background:#1a2a1a;color:#4ade80;border:1px solid #2a4a2a;padding:7px 18px">
        📨 테스트 발송</button>
    </div>
    <div id="emailStatus" style="margin-top:8px;font-size:.78rem;color:#8b8fa8"></div>
    <div id="emailMsg"    style="margin-top:4px;font-size:.78rem"></div>
  </div>
</div>
"""

_RT33_EXTRA_JS = r"""
/* ── 조건33 실시간 스캔 컨트롤러 ── */
let rtPollTimer  = null;
let rtActive     = false;
let rtLastScanTs = '';

function rtStart(){
  fetch('/api/33/realtime/start').then(r=>r.json()).then(d=>{
    if(!d.ok){ showMsg(d.msg||'오류 발생'); return; }
    setRtActive(true);
    document.getElementById('progWrap').style.display = 'block';
    setP(0, '첫 번째 스캔 준비 중...');
    showMsg('실시간 스캔 시작됨 — 3분 간격으로 자동 반복됩니다.');
    rtLastScanTs = '';
    rtPoll();
  });
}
function rtStop(){
  fetch('/api/33/realtime/stop').then(r=>r.json()).then(()=>{
    setRtActive(false);
    if(rtPollTimer){ clearInterval(rtPollTimer); rtPollTimer = null; }
    showMsg('실시간 스캔 중지됨');
    setP(0,'');
    document.getElementById('progWrap').style.display = 'none';
  });
}
function setRtActive(v){
  rtActive = v;
  document.getElementById('rtStartBtn').classList.toggle('hidden', v);
  document.getElementById('rtStopBtn').classList.toggle('hidden', !v);
  const badge = document.getElementById('rtBadge');
  if(v){ badge.textContent='● 실시간 스캔 중'; badge.classList.remove('inactive'); }
  else { badge.textContent='● 대기 중';         badge.classList.add('inactive'); }
}
function rtPoll(){
  if(rtPollTimer) clearInterval(rtPollTimer);
  rtPollTimer = setInterval(()=>{
    if(!rtActive){ clearInterval(rtPollTimer); return; }
    fetch('/api/33/realtime/status').then(r=>r.json()).then(s=>{
      if(!s.running && rtActive){ setRtActive(false); clearInterval(rtPollTimer); return; }
      function fmtElapsed(sec){ if(!sec) return ''; const m=Math.floor(sec/60),s2=sec%60; return m>0?`${m}분 ${s2}초`:`${s2}초`; }
      const infoEl=document.getElementById('rtScanInfo');
      if(s.scan_no>0){
        const pst=s.prog_status; let info=`스캔 #${s.scan_no}`;
        if(pst==='loading'||pst==='running'){ info+=`  |  시작: ${s.scan_start||'—'}  |  진행 중...`; }
        else{ const el=s.scan_elapsed?`  소요: ${fmtElapsed(s.scan_elapsed)}`:''; info+=`  |  시작: ${s.scan_start||'—'}  →  완료: ${s.last_scan||'—'}${el}`; if(s.next_scan) info+=`  |  다음 스캔: ${s.next_scan}`; }
        infoEl.textContent=info;
      }
      const pst=s.prog_status;
      if(pst==='loading') setP(0,'종목 리스트 불러오는 중...');
      else if(pst==='running'){ const pct=s.prog_total>0?Math.round(s.prog_current/s.prog_total*100):0; setP(pct, scanProgressText(s.prog_current, s.prog_total, '스캔 중')); }
      else if(pst==='done'){ const el=s.scan_elapsed?`  소요 ${fmtElapsed(s.scan_elapsed)}`:''; setP(100,`스캔 완료 ✓  ${s.result_count}개 종목 발견${el}`); }
      const justDone=(pst==='done')&&s.last_scan&&(s.last_scan!==rtLastScanTs);
      if(justDone||(pst==='done'&&s.result_count>0&&!rtLastScanTs)){ rtLastScanTs=s.last_scan||'_init_'; rtLoadResult(s.new_count); }
    });
  },5000);
}
function rtLoadResult(newCount){
  fetch('/api/33/result').then(r=>r.json()).then(d=>{
    if(!d.rows||d.rows.length===0){ showMsg(`✓ 스캔 완료 — 조건 충족 종목 없음 (${new Date().toLocaleTimeString()})`); return; }
    document.getElementById('resultCard').classList.remove('hidden');
    document.getElementById('dlBtn').classList.remove('hidden');
    renderResult(d.date,d.rows);
    highlightNewRows(d.new_codes||[]);
    if(d.new_codes&&d.new_codes.length>0) showMsg(`🔔 신규 ${d.new_codes.length}개 종목 발견!  총 ${d.rows.length}개  (${new Date().toLocaleTimeString()})`);
    else showMsg(`✓ 스캔 완료 — ${d.rows.length}개 종목 (${new Date().toLocaleTimeString()})`);
  });
}
function highlightNewRows(newCodes){
  document.querySelectorAll('#tables tr').forEach(tr=>{ if(!tr.cells||tr.cells.length<2) return; const code=tr.cells[1].textContent.trim(); if(newCodes.includes(code)) tr.classList.add('row-new'); else tr.classList.remove('row-new'); });
}
function saveEmail(){
  const from=document.getElementById('emailFrom').value.trim();
  const pass=document.getElementById('emailPass').value.trim();
  const msgEl=document.getElementById('emailMsg'); const toVals=[];
  for(let i=1;i<=5;i++){ const v=(document.getElementById('emailTo'+i)||{}).value||''; if(v.trim()) toVals.push(v.trim()); }
  if(!from){msgEl.style.color='#f87171';msgEl.textContent='✗ 발신 주소를 입력하세요.';return;}
  if(!toVals.length){msgEl.style.color='#f87171';msgEl.textContent='✗ 수신자를 1명 이상 입력하세요.';return;}
  msgEl.style.color='#8b8fa8'; msgEl.textContent='저장 중...';
  const passParam=pass||'__keep__';
  let url='/api/33/email/save?from='+encodeURIComponent(from)+'&pass='+encodeURIComponent(passParam);
  toVals.forEach((v,i)=>{url+='&to'+(i+1)+'='+encodeURIComponent(v);});
  fetch(url).then(r=>r.json()).then(d=>{ msgEl.style.color=d.ok?'#4ade80':'#f87171'; msgEl.textContent=d.ok?`✓ 저장 완료 — 수신자 ${toVals.length}명`:('✗ '+(d.msg||'오류')); if(d.ok) loadEmailStatus(); });
}
function testEmail(){
  const btn=document.getElementById('testEmailBtn'); const msgEl=document.getElementById('emailMsg');
  btn.disabled=true; msgEl.style.color='#8b8fa8'; msgEl.textContent='📨 테스트 발송 중...';
  fetch('/api/33/email/test').then(r=>r.json()).then(d=>{ btn.disabled=false; msgEl.style.color=d.ok?'#4ade80':'#f87171'; msgEl.textContent=d.ok?'✓ 테스트 이메일 발송 완료!':'✗ 발송 실패: '+(d.msg||'알 수 없는 오류'); }).catch(()=>{btn.disabled=false;msgEl.textContent='네트워크 오류';});
}
function loadEmailStatus(){
  fetch('/api/33/email/status').then(r=>r.json()).then(s=>{
    const el=document.getElementById('emailStatus');
    if(s.from) document.getElementById('emailFrom').value=s.from;
    if(s.pass) document.getElementById('emailPass').value=s.pass;
    const toList=s.to_list||[];
    for(let i=1;i<=5;i++){ const inp=document.getElementById('emailTo'+i); if(inp) inp.value=toList[i-1]||''; }
    if(s.configured){ const toSummary=toList.map(v=>`<b>${v}</b>`).join(', '); el.innerHTML=`<span style="color:#4ade80">● 이메일 설정 완료</span>  발신: <b>${s.from}</b>  |  수신자 ${toList.length}명: ${toSummary}  |  앱비번: ${s.pass_hint}`; }
    else { el.innerHTML=`<span style="color:#f59e0b">⚠ 이메일 미설정</span>  — 발신 Gmail, 앱 비밀번호, 수신자를 입력하고 저장하세요.`; }
  });
}
fetch('/api/33/realtime/status').then(r=>r.json()).then(s=>{
  if(s.running){ setRtActive(true); document.getElementById('progWrap').style.display='block'; rtPoll(); if(s.result_count>0) rtLoadResult(s.new_count); }
});
loadEmailStatus();
"""


_RT35_EXTRA_SECTION = """
<div style="margin-top:18px;border-top:1px solid #2a2d3a;padding-top:16px">
  <div style="display:flex;gap:10px;flex-wrap:wrap;align-items:center;margin-bottom:12px">
    <button class="btn" id="rtStartBtn"
      style="background:#042a2e;color:#06b6d4;border:1px solid #0e5a65"
      onclick="rtStart()">▶ 실시간 스캔 시작</button>
    <button class="btn hidden" id="rtStopBtn"
      style="background:#0a2a30;color:#22d3ee;border:1px solid #145a65"
      onclick="rtStop()">■ 중지</button>
    <span id="rtBadge" class="rt-badge inactive">● 대기 중</span>
    <span id="rtScanInfo" style="font-size:.78rem;color:#8b8fa8"></span>
  </div>
  <div style="background:#13161f;border:1px solid #2a2d3a;border-radius:10px;padding:14px 16px;margin-top:4px">
    <div style="font-size:.8rem;color:#8b8fa8;margin-bottom:10px">📧 이메일 알림 설정</div>
    <div style="display:flex;gap:8px;flex-wrap:wrap;align-items:center;margin-bottom:8px">
      <input type="email" id="emailFrom" placeholder="발신 Gmail 주소" autocomplete="off"
        style="background:#0f1117;border:1px solid #2a2d3a;border-radius:6px;
               color:#e0e0e0;padding:7px 11px;font-size:.8rem;width:220px">
      <input type="password" id="emailPass" placeholder="Gmail 앱 비밀번호 (16자리)" autocomplete="new-password"
        style="background:#0f1117;border:1px solid #2a2d3a;border-radius:6px;
               color:#e0e0e0;padding:7px 11px;font-size:.8rem;width:200px">
    </div>
    <div style="display:flex;gap:8px;flex-wrap:wrap;align-items:center;margin-bottom:10px">
      <span style="font-size:.78rem;color:#8b8fa8;white-space:nowrap">수신자 (최대 5명):</span>
      <input type="email" id="emailTo1" placeholder="수신자 1" autocomplete="off"
        style="background:#0f1117;border:1px solid #2a2d3a;border-radius:6px;
               color:#e0e0e0;padding:7px 10px;font-size:.8rem;width:190px">
      <input type="email" id="emailTo2" placeholder="수신자 2" autocomplete="off"
        style="background:#0f1117;border:1px solid #2a2d3a;border-radius:6px;
               color:#e0e0e0;padding:7px 10px;font-size:.8rem;width:190px">
      <input type="email" id="emailTo3" placeholder="수신자 3 (선택)" autocomplete="off"
        style="background:#0f1117;border:1px solid #2a2d3a;border-radius:6px;
               color:#e0e0e0;padding:7px 10px;font-size:.8rem;width:190px">
      <input type="email" id="emailTo4" placeholder="수신자 4 (선택)" autocomplete="off"
        style="background:#0f1117;border:1px solid #2a2d3a;border-radius:6px;
               color:#e0e0e0;padding:7px 10px;font-size:.8rem;width:190px">
      <input type="email" id="emailTo5" placeholder="수신자 5 (선택)" autocomplete="off"
        style="background:#0f1117;border:1px solid #2a2d3a;border-radius:6px;
               color:#e0e0e0;padding:7px 10px;font-size:.8rem;width:190px">
    </div>
    <div style="display:flex;gap:8px;flex-wrap:wrap;align-items:center">
      <button class="btn" onclick="saveEmail()"
        style="background:#1e2a4a;color:#4f8ef7;border:1px solid #2a3a6a;padding:7px 18px">
        💾 저장</button>
      <button class="btn" id="testEmailBtn" onclick="testEmail()"
        style="background:#1a2a1a;color:#4ade80;border:1px solid #2a4a2a;padding:7px 18px">
        📨 테스트 발송</button>
    </div>
    <div id="emailStatus" style="margin-top:8px;font-size:.78rem;color:#8b8fa8"></div>
    <div id="emailMsg"    style="margin-top:4px;font-size:.78rem"></div>
  </div>
</div>
"""

_RT35_EXTRA_JS = r"""
/* ── 조건35 실시간 스캔 컨트롤러 ── */
let rtPollTimer  = null;
let rtActive     = false;
let rtLastScanTs = '';

function rtStart(){
  fetch('/api/35/realtime/start').then(r=>r.json()).then(d=>{
    if(!d.ok){ showMsg(d.msg||'오류 발생'); return; }
    setRtActive(true);
    document.getElementById('progWrap').style.display = 'block';
    setP(0, '첫 번째 스캔 준비 중...');
    showMsg('실시간 스캔 시작됨 — 3분 간격으로 자동 반복됩니다.');
    rtLastScanTs = '';
    rtPoll();
  });
}
function rtStop(){
  fetch('/api/35/realtime/stop').then(r=>r.json()).then(()=>{
    setRtActive(false);
    if(rtPollTimer){ clearInterval(rtPollTimer); rtPollTimer = null; }
    showMsg('실시간 스캔 중지됨');
    setP(0,'');
    document.getElementById('progWrap').style.display = 'none';
  });
}
function setRtActive(v){
  rtActive = v;
  document.getElementById('rtStartBtn').classList.toggle('hidden', v);
  document.getElementById('rtStopBtn').classList.toggle('hidden', !v);
  const badge = document.getElementById('rtBadge');
  if(v){ badge.textContent='● 실시간 스캔 중'; badge.classList.remove('inactive'); }
  else { badge.textContent='● 대기 중';         badge.classList.add('inactive'); }
}
function rtPoll(){
  if(rtPollTimer) clearInterval(rtPollTimer);
  rtPollTimer = setInterval(()=>{
    if(!rtActive){ clearInterval(rtPollTimer); return; }
    fetch('/api/35/realtime/status').then(r=>r.json()).then(s=>{
      if(!s.running && rtActive){ setRtActive(false); clearInterval(rtPollTimer); return; }
      function fmtElapsed(sec){ if(!sec) return ''; const m=Math.floor(sec/60),s2=sec%60; return m>0?`${m}분 ${s2}초`:`${s2}초`; }
      const infoEl=document.getElementById('rtScanInfo');
      if(s.scan_no>0){
        const pst=s.prog_status; let info=`스캔 #${s.scan_no}`;
        if(pst==='loading'||pst==='running'){ info+=`  |  시작: ${s.scan_start||'—'}  |  진행 중...`; }
        else{ const el=s.scan_elapsed?`  소요: ${fmtElapsed(s.scan_elapsed)}`:''; info+=`  |  시작: ${s.scan_start||'—'}  →  완료: ${s.last_scan||'—'}${el}`; if(s.next_scan) info+=`  |  다음 스캔: ${s.next_scan}`; }
        infoEl.textContent=info;
      }
      const pst=s.prog_status;
      if(pst==='loading') setP(0,'종목 리스트 불러오는 중...');
      else if(pst==='running'){ const pct=s.prog_total>0?Math.round(s.prog_current/s.prog_total*100):0; setP(pct, scanProgressText(s.prog_current, s.prog_total, '스캔 중')); }
      else if(pst==='done'){ const el=s.scan_elapsed?`  소요 ${fmtElapsed(s.scan_elapsed)}`:''; setP(100,`스캔 완료 ✓  TOP10 ${s.result_count}개${el}`); }
      const justDone=(pst==='done')&&s.last_scan&&(s.last_scan!==rtLastScanTs);
      if(justDone||(pst==='done'&&s.result_count>0&&!rtLastScanTs)){ rtLastScanTs=s.last_scan||'_init_'; rtLoadResult(s.new_count); }
    });
  },5000);
}
function rtLoadResult(newCount){
  fetch('/api/35/result').then(r=>r.json()).then(d=>{
    if(!d.rows||d.rows.length===0){ showMsg(`✓ 스캔 완료 — 결과 없음 (${new Date().toLocaleTimeString()})`); return; }
    document.getElementById('resultCard').classList.remove('hidden');
    document.getElementById('dlBtn').classList.remove('hidden');
    renderResult(d.date,d.rows);
    highlightNewRows(d.new_codes||[]);
    if(d.new_codes&&d.new_codes.length>0) showMsg(`🔔 TOP10 변경! 신규 ${d.new_codes.length}개  (${new Date().toLocaleTimeString()})`);
    else showMsg(`✓ 스캔 완료 — TOP10 ${d.rows.length}개  (${new Date().toLocaleTimeString()})`);
  });
}
function highlightNewRows(newCodes){
  document.querySelectorAll('#tables tr').forEach(tr=>{ if(!tr.cells||tr.cells.length<2) return; const code=tr.cells[1].textContent.trim(); if(newCodes.includes(code)) tr.classList.add('row-new'); else tr.classList.remove('row-new'); });
}
function saveEmail(){
  const from=document.getElementById('emailFrom').value.trim();
  const pass=document.getElementById('emailPass').value.trim();
  const msgEl=document.getElementById('emailMsg'); const toVals=[];
  for(let i=1;i<=5;i++){ const v=(document.getElementById('emailTo'+i)||{}).value||''; if(v.trim()) toVals.push(v.trim()); }
  if(!from){msgEl.style.color='#f87171';msgEl.textContent='✗ 발신 주소를 입력하세요.';return;}
  if(!toVals.length){msgEl.style.color='#f87171';msgEl.textContent='✗ 수신자를 1명 이상 입력하세요.';return;}
  msgEl.style.color='#8b8fa8'; msgEl.textContent='저장 중...';
  const passParam=pass||'__keep__';
  let url='/api/35/email/save?from='+encodeURIComponent(from)+'&pass='+encodeURIComponent(passParam);
  toVals.forEach((v,i)=>{url+='&to'+(i+1)+'='+encodeURIComponent(v);});
  fetch(url).then(r=>r.json()).then(d=>{ msgEl.style.color=d.ok?'#4ade80':'#f87171'; msgEl.textContent=d.ok?`✓ 저장 완료 — 수신자 ${toVals.length}명`:('✗ '+(d.msg||'오류')); if(d.ok) loadEmailStatus(); });
}
function testEmail(){
  const btn=document.getElementById('testEmailBtn'); const msgEl=document.getElementById('emailMsg');
  btn.disabled=true; msgEl.style.color='#8b8fa8'; msgEl.textContent='📨 테스트 발송 중...';
  fetch('/api/35/email/test').then(r=>r.json()).then(d=>{ btn.disabled=false; msgEl.style.color=d.ok?'#4ade80':'#f87171'; msgEl.textContent=d.ok?'✓ 테스트 이메일 발송 완료!':'✗ 발송 실패: '+(d.msg||'알 수 없는 오류'); }).catch(()=>{btn.disabled=false;msgEl.textContent='네트워크 오류';});
}
function loadEmailStatus(){
  fetch('/api/35/email/status').then(r=>r.json()).then(s=>{
    const el=document.getElementById('emailStatus');
    if(s.from) document.getElementById('emailFrom').value=s.from;
    if(s.pass) document.getElementById('emailPass').value=s.pass;
    const toList=s.to_list||[];
    for(let i=1;i<=5;i++){ const inp=document.getElementById('emailTo'+i); if(inp) inp.value=toList[i-1]||''; }
    if(s.configured){ const toSummary=toList.map(v=>`<b>${v}</b>`).join(', '); el.innerHTML=`<span style="color:#4ade80">● 이메일 설정 완료</span>  발신: <b>${s.from}</b>  |  수신자 ${toList.length}명: ${toSummary}  |  앱비번: ${s.pass_hint}`; }
    else { el.innerHTML=`<span style="color:#f59e0b">⚠ 이메일 미설정</span>  — 발신 Gmail, 앱 비밀번호, 수신자를 입력하고 저장하세요.`; }
  });
}
fetch('/api/35/realtime/status').then(r=>r.json()).then(s=>{
  if(s.running){ setRtActive(true); document.getElementById('progWrap').style.display='block'; rtPoll(); if(s.result_count>0) rtLoadResult(s.new_count); }
});
loadEmailStatus();
"""

_RT37_EXTRA_SECTION = """
<div style="margin-top:18px;border-top:1px solid #2a2d3a;padding-top:16px">
  <div style="display:flex;gap:10px;flex-wrap:wrap;align-items:center;margin-bottom:12px">
    <button class="btn" id="rtStartBtn"
      style="background:#1a0a30;color:#a855f7;border:1px solid #5a1a9a"
      onclick="rtStart()">▶ 실시간 스캔 시작</button>
    <button class="btn hidden" id="rtStopBtn"
      style="background:#3a1040;color:#f87171;border:1px solid #5a2050"
      onclick="rtStop()">■ 중지</button>
    <span id="rtBadge" class="rt-badge inactive">● 대기 중</span>
    <span id="rtScanInfo" style="font-size:.78rem;color:#8b8fa8"></span>
  </div>
  <div style="background:#13161f;border:1px solid #2a2d3a;border-radius:10px;padding:14px 16px;margin-top:4px">
    <div style="font-size:.8rem;color:#8b8fa8;margin-bottom:10px">📧 이메일 알림 설정</div>
    <div style="display:flex;gap:8px;flex-wrap:wrap;align-items:center;margin-bottom:8px">
      <input type="email" id="emailFrom" placeholder="발신 Gmail 주소" autocomplete="off"
        style="background:#0f1117;border:1px solid #2a2d3a;border-radius:6px;
               color:#e0e0e0;padding:7px 11px;font-size:.8rem;width:220px">
      <input type="password" id="emailPass" placeholder="Gmail 앱 비밀번호 (16자리)" autocomplete="new-password"
        style="background:#0f1117;border:1px solid #2a2d3a;border-radius:6px;
               color:#e0e0e0;padding:7px 11px;font-size:.8rem;width:200px">
    </div>
    <div style="display:flex;gap:8px;flex-wrap:wrap;align-items:center;margin-bottom:10px">
      <span style="font-size:.78rem;color:#8b8fa8;white-space:nowrap">수신자 (최대 5명):</span>
      <input type="email" id="emailTo1" placeholder="수신자 1" autocomplete="off"
        style="background:#0f1117;border:1px solid #2a2d3a;border-radius:6px;
               color:#e0e0e0;padding:7px 10px;font-size:.8rem;width:190px">
      <input type="email" id="emailTo2" placeholder="수신자 2" autocomplete="off"
        style="background:#0f1117;border:1px solid #2a2d3a;border-radius:6px;
               color:#e0e0e0;padding:7px 10px;font-size:.8rem;width:190px">
      <input type="email" id="emailTo3" placeholder="수신자 3 (선택)" autocomplete="off"
        style="background:#0f1117;border:1px solid #2a2d3a;border-radius:6px;
               color:#e0e0e0;padding:7px 10px;font-size:.8rem;width:190px">
      <input type="email" id="emailTo4" placeholder="수신자 4 (선택)" autocomplete="off"
        style="background:#0f1117;border:1px solid #2a2d3a;border-radius:6px;
               color:#e0e0e0;padding:7px 10px;font-size:.8rem;width:190px">
      <input type="email" id="emailTo5" placeholder="수신자 5 (선택)" autocomplete="off"
        style="background:#0f1117;border:1px solid #2a2d3a;border-radius:6px;
               color:#e0e0e0;padding:7px 10px;font-size:.8rem;width:190px">
    </div>
    <div style="display:flex;gap:8px;flex-wrap:wrap;align-items:center">
      <button class="btn" onclick="saveEmail()"
        style="background:#1e2a4a;color:#4f8ef7;border:1px solid #2a3a6a;padding:7px 18px">
        💾 저장</button>
      <button class="btn" id="testEmailBtn" onclick="testEmail()"
        style="background:#1a2a1a;color:#4ade80;border:1px solid #2a4a2a;padding:7px 18px">
        📨 테스트 발송</button>
    </div>
    <div id="emailStatus" style="margin-top:8px;font-size:.78rem;color:#8b8fa8"></div>
    <div id="emailMsg"    style="margin-top:4px;font-size:.78rem"></div>
  </div>
</div>
"""

_RT37_EXTRA_JS = r"""
/* ── 조건37 거래대금RSI 실시간 스캔 컨트롤러 (10분 간격) ── */
let rtPollTimer  = null;
let rtActive     = false;
let rtLastScanTs = '';

function rtStart(){
  fetch('/api/37/realtime/start').then(r=>r.json()).then(d=>{
    if(!d.ok){ showMsg(d.msg||'오류 발생'); return; }
    setRtActive(true);
    document.getElementById('progWrap').style.display = 'block';
    setP(0, '첫 번째 스캔 준비 중...');
    showMsg('실시간 스캔 시작됨 — 10분 간격으로 자동 반복됩니다.');
    rtLastScanTs = '';
    rtPoll();
  });
}
function rtStop(){
  fetch('/api/37/realtime/stop').then(r=>r.json()).then(()=>{
    setRtActive(false);
    if(rtPollTimer){ clearInterval(rtPollTimer); rtPollTimer = null; }
    showMsg('실시간 스캔 중지됨');
    setP(0,'');
    document.getElementById('progWrap').style.display = 'none';
  });
}
function setRtActive(v){
  rtActive = v;
  document.getElementById('rtStartBtn').classList.toggle('hidden', v);
  document.getElementById('rtStopBtn').classList.toggle('hidden', !v);
  const badge = document.getElementById('rtBadge');
  if(v){ badge.textContent='● 실시간 스캔 중'; badge.classList.remove('inactive'); }
  else { badge.textContent='● 대기 중';         badge.classList.add('inactive'); }
}
function rtPoll(){
  if(rtPollTimer) clearInterval(rtPollTimer);
  rtPollTimer = setInterval(()=>{
    if(!rtActive){ clearInterval(rtPollTimer); return; }
    fetch('/api/37/realtime/status').then(r=>r.json()).then(s=>{
      if(!s.running && rtActive){ setRtActive(false); clearInterval(rtPollTimer); return; }
      function fmtElapsed(sec){ if(!sec) return ''; const m=Math.floor(sec/60),s2=sec%60; return m>0?`${m}분 ${s2}초`:`${s2}초`; }
      const infoEl=document.getElementById('rtScanInfo');
      if(s.scan_no>0){
        const pst=s.prog_status; let info=`스캔 #${s.scan_no}`;
        if(pst==='loading'||pst==='running'){ info+=`  |  시작: ${s.scan_start||'—'}  |  진행 중...`; }
        else{ const el=s.scan_elapsed?`  소요: ${fmtElapsed(s.scan_elapsed)}`:''; info+=`  |  시작: ${s.scan_start||'—'}  →  완료: ${s.last_scan||'—'}${el}`; if(s.next_scan) info+=`  |  다음 스캔: ${s.next_scan}`; }
        infoEl.textContent=info;
      }
      const pst=s.prog_status;
      if(pst==='loading') setP(0,'종목 리스트 불러오는 중...');
      else if(pst==='running'){ const pct=s.prog_total>0?Math.round(s.prog_current/s.prog_total*100):0; setP(pct, scanProgressText(s.prog_current, s.prog_total, '스캔 중')); }
      else if(pst==='done'){ const el=s.scan_elapsed?`  소요 ${fmtElapsed(s.scan_elapsed)}`:''; setP(100,`스캔 완료 ✓  ${s.result_count}개 종목 발견${el}`); }
      const justDone=(pst==='done')&&s.last_scan&&(s.last_scan!==rtLastScanTs);
      if(justDone||(pst==='done'&&s.result_count>0&&!rtLastScanTs)){ rtLastScanTs=s.last_scan||'_init_'; rtLoadResult(s.new_count); }
    });
  },5000);
}
function rtLoadResult(newCount){
  fetch('/api/37/result').then(r=>r.json()).then(d=>{
    if(!d.rows||d.rows.length===0){ showMsg(`✓ 스캔 완료 — 조건 충족 종목 없음 (${new Date().toLocaleTimeString()})`); return; }
    document.getElementById('resultCard').classList.remove('hidden');
    document.getElementById('dlBtn').classList.remove('hidden');
    renderResult(d.date,d.rows);
    highlightNewRows(d.new_codes||[]);
    if(d.new_codes&&d.new_codes.length>0) showMsg(`🔔 신규 ${d.new_codes.length}개 종목 발견!  총 ${d.rows.length}개  (${new Date().toLocaleTimeString()})`);
    else showMsg(`✓ 스캔 완료 — ${d.rows.length}개 종목 (${new Date().toLocaleTimeString()})`);
  });
}
function highlightNewRows(newCodes){
  document.querySelectorAll('#tables tr').forEach(tr=>{ if(!tr.cells||tr.cells.length<2) return; const code=tr.cells[1].textContent.trim(); if(newCodes.includes(code)) tr.classList.add('row-new'); else tr.classList.remove('row-new'); });
}
function saveEmail(){
  const from=document.getElementById('emailFrom').value.trim();
  const pass=document.getElementById('emailPass').value.trim();
  const msgEl=document.getElementById('emailMsg'); const toVals=[];
  for(let i=1;i<=5;i++){ const v=(document.getElementById('emailTo'+i)||{}).value||''; if(v.trim()) toVals.push(v.trim()); }
  if(!from){msgEl.style.color='#f87171';msgEl.textContent='✗ 발신 주소를 입력하세요.';return;}
  if(!toVals.length){msgEl.style.color='#f87171';msgEl.textContent='✗ 수신자를 1명 이상 입력하세요.';return;}
  msgEl.style.color='#8b8fa8'; msgEl.textContent='저장 중...';
  const passParam=pass||'__keep__';
  let url='/api/37/email/save?from='+encodeURIComponent(from)+'&pass='+encodeURIComponent(passParam);
  toVals.forEach((v,i)=>{url+='&to'+(i+1)+'='+encodeURIComponent(v);});
  fetch(url).then(r=>r.json()).then(d=>{ msgEl.style.color=d.ok?'#4ade80':'#f87171'; msgEl.textContent=d.ok?`✓ 저장 완료 — 수신자 ${toVals.length}명`:('✗ '+(d.msg||'오류')); if(d.ok) loadEmailStatus(); });
}
function testEmail(){
  const btn=document.getElementById('testEmailBtn'); const msgEl=document.getElementById('emailMsg');
  btn.disabled=true; msgEl.style.color='#8b8fa8'; msgEl.textContent='📨 테스트 발송 중...';
  fetch('/api/37/email/test').then(r=>r.json()).then(d=>{ btn.disabled=false; msgEl.style.color=d.ok?'#4ade80':'#f87171'; msgEl.textContent=d.ok?'✓ 테스트 이메일 발송 완료!':'✗ 발송 실패: '+(d.msg||'알 수 없는 오류'); }).catch(()=>{btn.disabled=false;msgEl.textContent='네트워크 오류';});
}
function loadEmailStatus(){
  fetch('/api/37/email/status').then(r=>r.json()).then(s=>{
    const el=document.getElementById('emailStatus');
    if(s.from) document.getElementById('emailFrom').value=s.from;
    if(s.pass) document.getElementById('emailPass').value=s.pass;
    const toList=s.to_list||[];
    for(let i=1;i<=5;i++){ const inp=document.getElementById('emailTo'+i); if(inp) inp.value=toList[i-1]||''; }
    if(s.configured){ const toSummary=toList.map(v=>`<b>${v}</b>`).join(', '); el.innerHTML=`<span style="color:#4ade80">● 이메일 설정 완료</span>  발신: <b>${s.from}</b>  |  수신자 ${toList.length}명: ${toSummary}  |  앱비번: ${s.pass_hint}`; }
    else { el.innerHTML=`<span style="color:#f59e0b">⚠ 이메일 미설정</span>  — 발신 Gmail, 앱 비밀번호, 수신자를 입력하고 저장하세요.`; }
  });
}
fetch('/api/37/realtime/status').then(r=>r.json()).then(s=>{
  if(s.running){ setRtActive(true); document.getElementById('progWrap').style.display='block'; rtPoll(); if(s.result_count>0) rtLoadResult(s.new_count); }
});
loadEmailStatus();
"""

_S38_EXTRA_SECTION = """
<div style="margin-top:14px">
  <label style="font-size:.82rem;color:#8b8fa8;display:block;margin-bottom:8px">
    📷 90일 일봉 차트 이미지 업로드
  </label>
  <div id="uploadArea"
    style="border:2px dashed #2a2d3a;border-radius:10px;padding:28px 16px;
           text-align:center;cursor:pointer;transition:.2s;position:relative"
    onclick="document.getElementById('chartFile').click()"
    ondragover="event.preventDefault();this.style.borderColor='#10b981'"
    ondragleave="this.style.borderColor='#2a2d3a'"
    ondrop="handleDrop38(event)">
    <div id="uploadHint38" style="color:#8b8fa8;font-size:.9rem;pointer-events:none">
      🖼️ 여기를 클릭하거나 이미지를 드래그하세요<br>
      <small style="font-size:.75rem;color:#4a4d5e">PNG / JPG / JPEG · 최대 20MB · TradingView·HTS 차트 모두 지원</small>
    </div>
    <img id="chartPreview38" src="" alt=""
      style="display:none;max-width:100%;max-height:320px;border-radius:8px;
             margin-top:12px;pointer-events:none">
  </div>
  <input type="file" id="chartFile" accept="image/*" style="display:none"
    onchange="handleFileSelect38(this)">
  <div id="patternStatus38"
    style="font-size:.78rem;color:#8b8fa8;margin-top:8px;min-height:1.2em"></div>
  <div style="margin-top:8px;font-size:.73rem;color:#4a4d5e;line-height:1.6">
    ※ 비교 방식: 업로드 이미지 패턴 ↔ <span style="color:#a0a4b8">검색기준일 기준 최근 90봉</span> 패턴 유사도 (과거 임의 시점 검색 아님)<br>
    ※ 사용된 지표: 종가 70% · VL(변동회귀선50) 12.5% · 세력평단 5% · TDI S_RSI 5% · EMA9 4% · EMA20 3.5%<br>
    ※ EMA200 상승 조건(필수) 미충족 종목은 자동 제외됩니다.<br>
    ※ 이미지 패턴 추출은 근사값이므로 결과는 참고용으로만 활용하세요.
  </div>
</div>
"""

_S38_EXTRA_JS = r"""
/* ── 조건38 패턴검색 이미지 업로드 컨트롤러 ── */

function handleDrop38(e){
  e.preventDefault();
  document.getElementById('uploadArea').style.borderColor='#2a2d3a';
  if(e.dataTransfer.files.length > 0) uploadChart38(e.dataTransfer.files[0]);
}
function handleFileSelect38(input){
  if(input.files.length > 0) uploadChart38(input.files[0]);
}
function uploadChart38(file){
  if(!file.type.startsWith('image/')){
    setStatus38('✗ 이미지 파일만 업로드 가능합니다.', '#f87171'); return;
  }
  if(file.size > 20 * 1024 * 1024){
    setStatus38('✗ 파일 크기가 20MB를 초과합니다.', '#f87171'); return;
  }
  setStatus38('📊 이미지 분석 중 — 패턴 추출 중...', '#8b8fa8');
  const fd = new FormData();
  fd.append('chart', file);
  fetch('/api/38/upload', {method:'POST', body:fd})
    .then(r=>r.json())
    .then(d=>{
      if(!d.ok){ setStatus38('✗ '+d.msg, '#f87171'); return; }
      const prev = document.getElementById('chartPreview38');
      prev.src   = d.image_b64;
      prev.style.display = 'block';
      document.getElementById('uploadHint38').style.display = 'none';
      setStatus38('✓ 패턴 추출 완료 (90포인트) — 기준일 선택 후 스크리닝 시작을 누르세요.', '#4ade80');
    })
    .catch(()=> setStatus38('✗ 업로드 실패 — 네트워크 오류', '#f87171'));
}
function setStatus38(msg, color){
  const el = document.getElementById('patternStatus38');
  el.style.color = color;
  el.textContent  = msg;
}
/* 기본 startScan 오버라이드: 이미지 미업로드 시 경고 */
function startScan(){
  const prev = document.getElementById('chartPreview38');
  if(!prev || prev.style.display==='none'){
    showMsg('⚠ 먼저 90일 일봉 차트 이미지를 업로드해주세요.');
    return;
  }
  const d = document.getElementById('dateInput').value.replace(/-/g,'');
  if(!d) return;
  document.getElementById('runBtn').disabled = true;
  document.getElementById('dlBtn').classList.add('hidden');
  document.getElementById('resultCard').classList.add('hidden');
  document.getElementById('progWrap').style.display = 'block';
  document.getElementById('msg').textContent = '';
  setP(0,'종목 리스트 조회 중...');
  fetch(`/api/38/start?date=${d}`).then(r=>r.json()).then(r=>{
    if(r.error){showMsg(r.error);resetBtn();return;}
    listenProg();
  });
}
"""

# ── 조건39 실시간 스캔 + 이메일 UI ─────────────────────────────────────────
_RT39_EXTRA_SECTION = """
<div style="margin-top:18px;border-top:1px solid #2a2d3a;padding-top:16px">
  <div style="display:flex;gap:10px;flex-wrap:wrap;align-items:center;margin-bottom:12px">
    <button class="btn" id="rtStartBtn"
      style="background:#2a1800;color:#f59e0b;border:1px solid #7a4a00"
      onclick="rtStart()">▶ 실시간 스캔 시작</button>
    <button class="btn hidden" id="rtStopBtn"
      style="background:#3a1800;color:#f87171;border:1px solid #5a2010"
      onclick="rtStop()">■ 중지</button>
    <span id="rtBadge" class="rt-badge inactive">● 대기 중</span>
    <span id="rtScanInfo" style="font-size:.78rem;color:#8b8fa8"></span>
  </div>
  <div style="background:#13161f;border:1px solid #2a2d3a;border-radius:10px;padding:14px 16px;margin-top:4px">
    <div style="font-size:.8rem;color:#8b8fa8;margin-bottom:10px">📧 이메일 알림 설정</div>
    <div style="display:flex;gap:8px;flex-wrap:wrap;align-items:center;margin-bottom:8px">
      <input type="email" id="emailFrom" placeholder="발신 Gmail 주소" autocomplete="off"
        style="background:#0f1117;border:1px solid #2a2d3a;border-radius:6px;
               color:#e0e0e0;padding:7px 11px;font-size:.8rem;width:220px">
      <input type="password" id="emailPass" placeholder="Gmail 앱 비밀번호 (16자리)" autocomplete="new-password"
        style="background:#0f1117;border:1px solid #2a2d3a;border-radius:6px;
               color:#e0e0e0;padding:7px 11px;font-size:.8rem;width:200px">
    </div>
    <div style="display:flex;gap:8px;flex-wrap:wrap;align-items:center;margin-bottom:10px">
      <span style="font-size:.78rem;color:#8b8fa8;white-space:nowrap">수신자 (최대 5명):</span>
      <input type="email" id="emailTo1" placeholder="수신자 1" autocomplete="off"
        style="background:#0f1117;border:1px solid #2a2d3a;border-radius:6px;
               color:#e0e0e0;padding:7px 10px;font-size:.8rem;width:190px">
      <input type="email" id="emailTo2" placeholder="수신자 2" autocomplete="off"
        style="background:#0f1117;border:1px solid #2a2d3a;border-radius:6px;
               color:#e0e0e0;padding:7px 10px;font-size:.8rem;width:190px">
      <input type="email" id="emailTo3" placeholder="수신자 3 (선택)" autocomplete="off"
        style="background:#0f1117;border:1px solid #2a2d3a;border-radius:6px;
               color:#e0e0e0;padding:7px 10px;font-size:.8rem;width:190px">
      <input type="email" id="emailTo4" placeholder="수신자 4 (선택)" autocomplete="off"
        style="background:#0f1117;border:1px solid #2a2d3a;border-radius:6px;
               color:#e0e0e0;padding:7px 10px;font-size:.8rem;width:190px">
      <input type="email" id="emailTo5" placeholder="수신자 5 (선택)" autocomplete="off"
        style="background:#0f1117;border:1px solid #2a2d3a;border-radius:6px;
               color:#e0e0e0;padding:7px 10px;font-size:.8rem;width:190px">
    </div>
    <div style="display:flex;gap:8px;flex-wrap:wrap;align-items:center">
      <button class="btn" onclick="saveEmail()"
        style="background:#1e2a4a;color:#4f8ef7;border:1px solid #2a3a6a;padding:7px 18px">
        💾 저장</button>
      <button class="btn" id="testEmailBtn" onclick="testEmail()"
        style="background:#2a1800;color:#f59e0b;border:1px solid #7a4a00;padding:7px 18px">
        📨 테스트 발송</button>
    </div>
    <div id="emailStatus" style="margin-top:8px;font-size:.78rem;color:#8b8fa8"></div>
    <div id="emailMsg"    style="margin-top:4px;font-size:.78rem"></div>
  </div>
</div>
"""

_RT39_EXTRA_JS = r"""
/* ── 조건39 VL이격시작 실시간 스캔 컨트롤러 (10분 간격) ── */
let rtPollTimer  = null;
let rtActive     = false;
let rtLastScanTs = '';

function rtStart(){
  fetch('/api/39/realtime/start').then(r=>r.json()).then(d=>{
    if(!d.ok){ showMsg(d.msg||'오류 발생'); return; }
    setRtActive(true);
    document.getElementById('progWrap').style.display = 'block';
    setP(0, '첫 번째 스캔 준비 중...');
    showMsg('실시간 스캔 시작됨 — 10분 간격으로 자동 반복됩니다.');
    rtLastScanTs = '';
    rtPoll();
  });
}
function rtStop(){
  fetch('/api/39/realtime/stop').then(r=>r.json()).then(()=>{
    setRtActive(false);
    if(rtPollTimer){ clearInterval(rtPollTimer); rtPollTimer = null; }
    showMsg('실시간 스캔 중지됨');
    setP(0,'');
    document.getElementById('progWrap').style.display = 'none';
  });
}
function setRtActive(v){
  rtActive = v;
  document.getElementById('rtStartBtn').classList.toggle('hidden', v);
  document.getElementById('rtStopBtn').classList.toggle('hidden', !v);
  const badge = document.getElementById('rtBadge');
  if(v){ badge.textContent='● 실시간 스캔 중'; badge.classList.remove('inactive'); }
  else { badge.textContent='● 대기 중';         badge.classList.add('inactive'); }
}
function rtPoll(){
  if(rtPollTimer) clearInterval(rtPollTimer);
  rtPollTimer = setInterval(()=>{
    if(!rtActive){ clearInterval(rtPollTimer); return; }
    fetch('/api/39/realtime/status').then(r=>r.json()).then(s=>{
      if(!s.running && rtActive){ setRtActive(false); clearInterval(rtPollTimer); return; }
      function fmtElapsed(sec){ if(!sec) return ''; const m=Math.floor(sec/60),s2=sec%60; return m>0?`${m}분 ${s2}초`:`${s2}초`; }
      const infoEl=document.getElementById('rtScanInfo');
      if(s.scan_no>0){
        const pst=s.prog_status; let info=`스캔 #${s.scan_no}`;
        if(pst==='loading'||pst==='running'){ info+=`  |  시작: ${s.scan_start||'—'}  |  진행 중...`; }
        else{ const el=s.scan_elapsed?`  소요: ${fmtElapsed(s.scan_elapsed)}`:''; info+=`  |  시작: ${s.scan_start||'—'}  →  완료: ${s.last_scan||'—'}${el}`; if(s.next_scan) info+=`  |  다음 스캔: ${s.next_scan}`; }
        infoEl.textContent=info;
      }
      const pst=s.prog_status;
      if(pst==='loading') setP(0,'종목 리스트 불러오는 중...');
      else if(pst==='running'){ const pct=s.prog_total>0?Math.round(s.prog_current/s.prog_total*100):0; setP(pct, scanProgressText(s.prog_current, s.prog_total, '스캔 중')); }
      else if(pst==='done'){ const el=s.scan_elapsed?`  소요 ${fmtElapsed(s.scan_elapsed)}`:''; setP(100,`스캔 완료 ✓  ${s.result_count}개 종목 발견${el}`); }
      const justDone=(pst==='done')&&s.last_scan&&(s.last_scan!==rtLastScanTs);
      if(justDone||(pst==='done'&&s.result_count>0&&!rtLastScanTs)){ rtLastScanTs=s.last_scan||'_init_'; rtLoadResult(s.new_count); }
    });
  },5000);
}
function rtLoadResult(newCount){
  fetch('/api/39/result').then(r=>r.json()).then(d=>{
    if(!d.rows||d.rows.length===0){ showMsg(`✓ 스캔 완료 — 조건 충족 종목 없음 (${new Date().toLocaleTimeString()})`); return; }
    document.getElementById('resultCard').classList.remove('hidden');
    document.getElementById('dlBtn').classList.remove('hidden');
    renderResult(d.date,d.rows);
    highlightNewRows(d.new_codes||[]);
    if(d.new_codes&&d.new_codes.length>0) showMsg(`🔔 신규 ${d.new_codes.length}개 종목 발견!  총 ${d.rows.length}개  (${new Date().toLocaleTimeString()})`);
    else showMsg(`✓ 스캔 완료 — ${d.rows.length}개 종목 (${new Date().toLocaleTimeString()})`);
  });
}
function highlightNewRows(newCodes){
  document.querySelectorAll('#tables tr').forEach(tr=>{ if(!tr.cells||tr.cells.length<2) return; const code=tr.cells[2].textContent.trim(); if(newCodes.includes(code)) tr.classList.add('row-new'); else tr.classList.remove('row-new'); });
}
function saveEmail(){
  const from=document.getElementById('emailFrom').value.trim();
  const pass=document.getElementById('emailPass').value.trim();
  const msgEl=document.getElementById('emailMsg'); const toVals=[];
  for(let i=1;i<=5;i++){ const v=(document.getElementById('emailTo'+i)||{}).value||''; if(v.trim()) toVals.push(v.trim()); }
  if(!from){msgEl.style.color='#f87171';msgEl.textContent='✗ 발신 주소를 입력하세요.';return;}
  if(!toVals.length){msgEl.style.color='#f87171';msgEl.textContent='✗ 수신자를 1명 이상 입력하세요.';return;}
  msgEl.style.color='#8b8fa8'; msgEl.textContent='저장 중...';
  const passParam=pass||'__keep__';
  let url='/api/39/email/save?from='+encodeURIComponent(from)+'&pass='+encodeURIComponent(passParam);
  toVals.forEach((v,i)=>{url+='&to'+(i+1)+'='+encodeURIComponent(v);});
  fetch(url).then(r=>r.json()).then(d=>{ msgEl.style.color=d.ok?'#4ade80':'#f87171'; msgEl.textContent=d.ok?`✓ 저장 완료 — 수신자 ${toVals.length}명`:('✗ '+(d.msg||'오류')); if(d.ok) loadEmailStatus(); });
}
function testEmail(){
  const btn=document.getElementById('testEmailBtn'); const msgEl=document.getElementById('emailMsg');
  btn.disabled=true; msgEl.style.color='#8b8fa8'; msgEl.textContent='📨 테스트 발송 중...';
  fetch('/api/39/email/test').then(r=>r.json()).then(d=>{ btn.disabled=false; msgEl.style.color=d.ok?'#4ade80':'#f87171'; msgEl.textContent=d.ok?'✓ 테스트 이메일 발송 완료!':'✗ 발송 실패: '+(d.msg||'알 수 없는 오류'); }).catch(()=>{btn.disabled=false;msgEl.textContent='네트워크 오류';});
}
function loadEmailStatus(){
  fetch('/api/39/email/status').then(r=>r.json()).then(s=>{
    const el=document.getElementById('emailStatus');
    if(s.from) document.getElementById('emailFrom').value=s.from;
    if(s.pass) document.getElementById('emailPass').value=s.pass;
    const toList=s.to_list||[];
    for(let i=1;i<=5;i++){ const inp=document.getElementById('emailTo'+i); if(inp) inp.value=toList[i-1]||''; }
    if(s.configured){ const toSummary=toList.map(v=>`<b>${v}</b>`).join(', '); el.innerHTML=`<span style="color:#4ade80">● 이메일 설정 완료</span>  발신: <b>${s.from}</b>  |  수신자 ${toList.length}명: ${toSummary}  |  앱비번: ${s.pass_hint}`; }
    else { el.innerHTML=`<span style="color:#f59e0b">⚠ 이메일 미설정</span>  — 발신 Gmail, 앱 비밀번호, 수신자를 입력하고 저장하세요.`; }
  });
}
fetch('/api/39/realtime/status').then(r=>r.json()).then(s=>{
  if(s.running){ setRtActive(true); document.getElementById('progWrap').style.display='block'; rtPoll(); if(s.result_count>0) rtLoadResult(s.new_count); }
});
loadEmailStatus();
"""

# ── 조건40 파워맵우수 실시간 스캔 + 이메일 UI ───────────────────────────────
_RT40_EXTRA_SECTION = """
<div style="margin-top:18px;border-top:1px solid #2a2d3a;padding-top:16px">
  <div style="font-size:.78rem;color:#06b6d4;margin-bottom:10px">
    📡 KIS OpenAPI 코스닥 거래대금 TOP20 실시간 조회 — 10분 간격 자동 스캔
  </div>
  <div style="display:flex;gap:10px;flex-wrap:wrap;align-items:center;margin-bottom:12px">
    <button class="btn" id="rtStartBtn"
      style="background:#001a20;color:#06b6d4;border:1px solid #0a5a6a"
      onclick="rtStart()">▶ 실시간 스캔 시작</button>
    <button class="btn hidden" id="rtStopBtn"
      style="background:#3a1010;color:#f87171;border:1px solid #5a2020"
      onclick="rtStop()">■ 중지</button>
    <span id="rtBadge" class="rt-badge inactive">● 대기 중</span>
    <span id="rtScanInfo" style="font-size:.78rem;color:#8b8fa8"></span>
  </div>
  <div style="background:#13161f;border:1px solid #2a2d3a;border-radius:10px;padding:14px 16px;margin-top:4px">
    <div style="font-size:.8rem;color:#8b8fa8;margin-bottom:10px">📧 이메일 알림 설정</div>
    <div style="display:flex;gap:8px;flex-wrap:wrap;align-items:center;margin-bottom:8px">
      <input type="email" id="emailFrom" placeholder="발신 Gmail 주소" autocomplete="off"
        style="background:#0f1117;border:1px solid #2a2d3a;border-radius:6px;
               color:#e0e0e0;padding:7px 11px;font-size:.8rem;width:220px">
      <input type="password" id="emailPass" placeholder="Gmail 앱 비밀번호 (16자리)" autocomplete="new-password"
        style="background:#0f1117;border:1px solid #2a2d3a;border-radius:6px;
               color:#e0e0e0;padding:7px 11px;font-size:.8rem;width:200px">
    </div>
    <div style="display:flex;gap:8px;flex-wrap:wrap;align-items:center;margin-bottom:10px">
      <span style="font-size:.78rem;color:#8b8fa8;white-space:nowrap">수신자 (최대 5명):</span>
      <input type="email" id="emailTo1" placeholder="수신자 1" autocomplete="off"
        style="background:#0f1117;border:1px solid #2a2d3a;border-radius:6px;
               color:#e0e0e0;padding:7px 10px;font-size:.8rem;width:190px">
      <input type="email" id="emailTo2" placeholder="수신자 2" autocomplete="off"
        style="background:#0f1117;border:1px solid #2a2d3a;border-radius:6px;
               color:#e0e0e0;padding:7px 10px;font-size:.8rem;width:190px">
      <input type="email" id="emailTo3" placeholder="수신자 3 (선택)" autocomplete="off"
        style="background:#0f1117;border:1px solid #2a2d3a;border-radius:6px;
               color:#e0e0e0;padding:7px 10px;font-size:.8rem;width:190px">
      <input type="email" id="emailTo4" placeholder="수신자 4 (선택)" autocomplete="off"
        style="background:#0f1117;border:1px solid #2a2d3a;border-radius:6px;
               color:#e0e0e0;padding:7px 10px;font-size:.8rem;width:190px">
      <input type="email" id="emailTo5" placeholder="수신자 5 (선택)" autocomplete="off"
        style="background:#0f1117;border:1px solid #2a2d3a;border-radius:6px;
               color:#e0e0e0;padding:7px 10px;font-size:.8rem;width:190px">
    </div>
    <div style="display:flex;gap:8px;flex-wrap:wrap;align-items:center">
      <button class="btn" onclick="saveEmail()"
        style="background:#1e2a4a;color:#4f8ef7;border:1px solid #2a3a6a;padding:7px 18px">
        💾 저장</button>
      <button class="btn" id="testEmailBtn" onclick="testEmail()"
        style="background:#001a20;color:#06b6d4;border:1px solid #0a5a6a;padding:7px 18px">
        📨 테스트 발송</button>
    </div>
    <div id="emailStatus" style="margin-top:8px;font-size:.78rem;color:#8b8fa8"></div>
    <div id="emailMsg"    style="margin-top:4px;font-size:.78rem"></div>
  </div>
  <div style="margin-top:8px;font-size:.72rem;color:#4a4d5e;line-height:1.7">
    ※ 스크리닝 시작 버튼으로 즉시 1회 조회 가능 (날짜 무관, 항상 현재 데이터)<br>
    ※ 이메일: 신규진입·TOP20 이탈·3위↑이상 순위 변동 감지 시 자동 발송
  </div>
</div>
"""

_RT40_EXTRA_JS = r"""
/* ── 조건40 파워맵우수 실시간 스캔 컨트롤러 (10분 간격) ── */
let rtPollTimer  = null;
let rtActive     = false;
let rtLastScanTs = '';

function startScan(){
  /* 날짜 무관 — 항상 오늘 날짜로 KIS API 현재 데이터 조회 */
  const today = new Date().toISOString().slice(0,10).replace(/-/g,'');
  document.getElementById('runBtn').disabled = true;
  document.getElementById('dlBtn').classList.add('hidden');
  document.getElementById('resultCard').classList.add('hidden');
  document.getElementById('progWrap').style.display = 'block';
  document.getElementById('msg').textContent = '';
  setP(0,'KIS API 코스닥 거래대금 TOP20 조회 중...');
  fetch(`/api/40/start?date=${today}`).then(r=>r.json()).then(r=>{
    if(r.error){showMsg(r.error);resetBtn();return;}
    listenProg();
  });
}
function rtStart(){
  fetch('/api/40/realtime/start').then(r=>r.json()).then(d=>{
    if(!d.ok){ showMsg(d.msg||'오류 발생'); return; }
    setRtActive(true);
    document.getElementById('progWrap').style.display = 'block';
    setP(0, '첫 번째 스캔 준비 중...');
    showMsg('실시간 스캔 시작됨 — 10분 간격으로 자동 반복됩니다.');
    rtLastScanTs = '';
    rtPoll();
  });
}
function rtStop(){
  fetch('/api/40/realtime/stop').then(r=>r.json()).then(()=>{
    setRtActive(false);
    if(rtPollTimer){ clearInterval(rtPollTimer); rtPollTimer = null; }
    showMsg('실시간 스캔 중지됨');
    setP(0,'');
    document.getElementById('progWrap').style.display = 'none';
  });
}
function setRtActive(v){
  rtActive = v;
  document.getElementById('rtStartBtn').classList.toggle('hidden', v);
  document.getElementById('rtStopBtn').classList.toggle('hidden', !v);
  const badge = document.getElementById('rtBadge');
  if(v){ badge.textContent='● 실시간 스캔 중'; badge.classList.remove('inactive'); }
  else { badge.textContent='● 대기 중';         badge.classList.add('inactive'); }
}
function rtPoll(){
  if(rtPollTimer) clearInterval(rtPollTimer);
  rtPollTimer = setInterval(()=>{
    if(!rtActive){ clearInterval(rtPollTimer); return; }
    fetch('/api/40/realtime/status').then(r=>r.json()).then(s=>{
      if(!s.running && rtActive){ setRtActive(false); clearInterval(rtPollTimer); return; }
      function fmtElapsed(sec){ if(!sec) return ''; const m=Math.floor(sec/60),s2=sec%60; return m>0?`${m}분 ${s2}초`:`${s2}초`; }
      const infoEl=document.getElementById('rtScanInfo');
      if(s.scan_no>0){
        const pst=s.prog_status; let info=`스캔 #${s.scan_no}`;
        if(pst==='loading'||pst==='running'){ info+=`  |  시작: ${s.scan_start||'—'}  |  조회 중...`; }
        else{ const el=s.scan_elapsed?`  소요: ${fmtElapsed(s.scan_elapsed)}`:''; info+=`  |  완료: ${s.last_scan||'—'}${el}`; if(s.next_scan) info+=`  |  다음: ${s.next_scan}`; }
        infoEl.textContent=info;
      }
      const pst=s.prog_status;
      if(pst==='loading'||pst==='running'){
        const pct = scanProgressPct(s.prog_current, s.prog_total);
        setP(pct, s.prog_total>0 ? scanProgressText(s.prog_current, s.prog_total, 'KIS 조회 중') : 'KIS API 조회 중...');
      }
      else if(pst==='done'){ const el=s.scan_elapsed?`  소요 ${fmtElapsed(s.scan_elapsed)}`:''; setP(100,`조회 완료 ✓  TOP${s.result_count}${el}`); }
      const justDone=(pst==='done')&&s.last_scan&&(s.last_scan!==rtLastScanTs);
      if(justDone||(pst==='done'&&s.result_count>0&&!rtLastScanTs)){ rtLastScanTs=s.last_scan||'_init_'; rtLoadResult(s.new_count); }
    });
  },4000);
}
function rtLoadResult(newCount){
  fetch('/api/40/result').then(r=>r.json()).then(d=>{
    if(!d.rows||d.rows.length===0){ showMsg(`✓ TOP20 조회 완료 — 결과 없음 (장 마감 가능성)`); return; }
    document.getElementById('resultCard').classList.remove('hidden');
    document.getElementById('dlBtn').classList.remove('hidden');
    renderResult(d.date,d.rows);
    highlightNewRows(d.new_codes||[]);
    const nc=d.new_codes&&d.new_codes.length;
    if(nc) showMsg(`🔔 신규 진입 ${nc}종목!  TOP${d.rows.length}  (${new Date().toLocaleTimeString()})`);
    else showMsg(`✓ TOP${d.rows.length} 조회 완료  (${new Date().toLocaleTimeString()})`);
  });
}
function highlightNewRows(newCodes){
  document.querySelectorAll('#tables tr').forEach(tr=>{
    if(!tr.cells||tr.cells.length<2) return;
    const code=tr.cells[1].textContent.trim();
    if(newCodes.includes(code)) tr.classList.add('row-new');
    else tr.classList.remove('row-new');
  });
}
function saveEmail(){
  const from=document.getElementById('emailFrom').value.trim();
  const pass=document.getElementById('emailPass').value.trim();
  const msgEl=document.getElementById('emailMsg'); const toVals=[];
  for(let i=1;i<=5;i++){ const v=(document.getElementById('emailTo'+i)||{}).value||''; if(v.trim()) toVals.push(v.trim()); }
  if(!from){msgEl.style.color='#f87171';msgEl.textContent='✗ 발신 주소를 입력하세요.';return;}
  if(!toVals.length){msgEl.style.color='#f87171';msgEl.textContent='✗ 수신자를 1명 이상 입력하세요.';return;}
  msgEl.style.color='#8b8fa8'; msgEl.textContent='저장 중...';
  const passParam=pass||'__keep__';
  let url='/api/40/email/save?from='+encodeURIComponent(from)+'&pass='+encodeURIComponent(passParam);
  toVals.forEach((v,i)=>{url+='&to'+(i+1)+'='+encodeURIComponent(v);});
  fetch(url).then(r=>r.json()).then(d=>{ msgEl.style.color=d.ok?'#4ade80':'#f87171'; msgEl.textContent=d.ok?`✓ 저장 완료 — 수신자 ${toVals.length}명`:('✗ '+(d.msg||'오류')); if(d.ok) loadEmailStatus(); });
}
function testEmail(){
  const btn=document.getElementById('testEmailBtn'); const msgEl=document.getElementById('emailMsg');
  btn.disabled=true; msgEl.style.color='#8b8fa8'; msgEl.textContent='📨 테스트 발송 중...';
  fetch('/api/40/email/test').then(r=>r.json()).then(d=>{ btn.disabled=false; msgEl.style.color=d.ok?'#4ade80':'#f87171'; msgEl.textContent=d.ok?'✓ 테스트 이메일 발송 완료!':'✗ 발송 실패: '+(d.msg||'알 수 없는 오류'); }).catch(()=>{btn.disabled=false;msgEl.textContent='네트워크 오류';});
}
function loadEmailStatus(){
  fetch('/api/40/email/status').then(r=>r.json()).then(s=>{
    const el=document.getElementById('emailStatus');
    if(s.from) document.getElementById('emailFrom').value=s.from;
    if(s.pass) document.getElementById('emailPass').value=s.pass;
    const toList=s.to_list||[];
    for(let i=1;i<=5;i++){ const inp=document.getElementById('emailTo'+i); if(inp) inp.value=toList[i-1]||''; }
    if(s.configured){ const toSummary=toList.map(v=>`<b>${v}</b>`).join(', '); el.innerHTML=`<span style="color:#4ade80">● 이메일 설정 완료</span>  발신: <b>${s.from}</b>  |  수신자 ${toList.length}명: ${toSummary}  |  앱비번: ${s.pass_hint}`; }
    else { el.innerHTML=`<span style="color:#06b6d4">⚠ 이메일 미설정</span>  — 발신 Gmail, 앱 비밀번호, 수신자를 입력하고 저장하세요.`; }
  });
}
fetch('/api/40/realtime/status').then(r=>r.json()).then(s=>{
  if(s.running){ setRtActive(true); document.getElementById('progWrap').style.display='block'; rtPoll(); if(s.result_count>0) rtLoadResult(s.new_count); }
});
loadEmailStatus();
"""

# ── 조건41 파워맵최고 실시간 스캔 + 이메일 UI ───────────────────────────────
_RT41_EXTRA_SECTION = """
<div style="margin-top:18px;border-top:1px solid #2a2d3a;padding-top:16px">
  <div style="font-size:.78rem;color:#f97316;margin-bottom:10px">
    🏆 KIS OpenAPI 코스닥 파워맵최고 — 전일TOP10 外 + 금일TOP20 진입 + 거래량폭증(200%↑) + 양봉 실시간 스캔 (10분 간격)
  </div>
  <div style="display:flex;gap:10px;flex-wrap:wrap;align-items:center;margin-bottom:12px">
    <button class="btn" id="rtStartBtn"
      style="background:#2a1000;color:#f97316;border:1px solid #6a3000"
      onclick="rtStart()">▶ 실시간 스캔 시작</button>
    <button class="btn hidden" id="rtStopBtn"
      style="background:#3a1010;color:#f87171;border:1px solid #5a2020"
      onclick="rtStop()">■ 중지</button>
    <span id="rtBadge" class="rt-badge inactive">● 대기 중</span>
    <span id="rtScanInfo" style="font-size:.78rem;color:#8b8fa8"></span>
  </div>
  <div style="background:#13161f;border:1px solid #2a2d3a;border-radius:10px;padding:14px 16px;margin-top:4px">
    <div style="font-size:.8rem;color:#8b8fa8;margin-bottom:10px">📧 이메일 알림 설정</div>
    <div style="display:flex;gap:8px;flex-wrap:wrap;align-items:center;margin-bottom:8px">
      <input type="email" id="emailFrom" placeholder="발신 Gmail 주소" autocomplete="off"
        style="background:#0f1117;border:1px solid #2a2d3a;border-radius:6px;
               color:#e0e0e0;padding:7px 11px;font-size:.8rem;width:220px">
      <input type="password" id="emailPass" placeholder="Gmail 앱 비밀번호 (16자리)" autocomplete="new-password"
        style="background:#0f1117;border:1px solid #2a2d3a;border-radius:6px;
               color:#e0e0e0;padding:7px 11px;font-size:.8rem;width:200px">
    </div>
    <div style="display:flex;gap:8px;flex-wrap:wrap;align-items:center;margin-bottom:10px">
      <span style="font-size:.78rem;color:#8b8fa8;white-space:nowrap">수신자 (최대 5명):</span>
      <input type="email" id="emailTo1" placeholder="수신자 1" autocomplete="off"
        style="background:#0f1117;border:1px solid #2a2d3a;border-radius:6px;
               color:#e0e0e0;padding:7px 10px;font-size:.8rem;width:190px">
      <input type="email" id="emailTo2" placeholder="수신자 2" autocomplete="off"
        style="background:#0f1117;border:1px solid #2a2d3a;border-radius:6px;
               color:#e0e0e0;padding:7px 10px;font-size:.8rem;width:190px">
      <input type="email" id="emailTo3" placeholder="수신자 3 (선택)" autocomplete="off"
        style="background:#0f1117;border:1px solid #2a2d3a;border-radius:6px;
               color:#e0e0e0;padding:7px 10px;font-size:.8rem;width:190px">
      <input type="email" id="emailTo4" placeholder="수신자 4 (선택)" autocomplete="off"
        style="background:#0f1117;border:1px solid #2a2d3a;border-radius:6px;
               color:#e0e0e0;padding:7px 10px;font-size:.8rem;width:190px">
      <input type="email" id="emailTo5" placeholder="수신자 5 (선택)" autocomplete="off"
        style="background:#0f1117;border:1px solid #2a2d3a;border-radius:6px;
               color:#e0e0e0;padding:7px 10px;font-size:.8rem;width:190px">
    </div>
    <div style="display:flex;gap:8px;flex-wrap:wrap;align-items:center">
      <button class="btn" onclick="saveEmail()"
        style="background:#2a1000;color:#f97316;border:1px solid #6a3000;padding:7px 18px">
        💾 저장</button>
      <button class="btn" id="testEmailBtn" onclick="testEmail()"
        style="background:#2a1000;color:#f97316;border:1px solid #6a3000;padding:7px 18px">
        📨 테스트 발송</button>
    </div>
    <div id="emailStatus" style="margin-top:8px;font-size:.78rem;color:#8b8fa8"></div>
    <div id="emailMsg"    style="margin-top:4px;font-size:.78rem"></div>
  </div>
  <div style="margin-top:8px;font-size:.72rem;color:#4a4d5e;line-height:1.7">
    ※ 스크리닝 시작 버튼으로 즉시 1회 조회 가능 (날짜 무관, 항상 현재 데이터)<br>
    ※ 조건: 전일 거래대금 TOP10 밖 + 금일 TOP20 진입 + 전일동시간대 대비 거래량 200%↑ + 양봉<br>
    ※ 이메일: 조건 충족 신규 종목 감지 시 자동 발송
  </div>
</div>
"""

_RT41_EXTRA_JS = r"""
/* ── 조건41 파워맵최고 실시간 스캔 컨트롤러 (10분 간격) ── */
let rtPollTimer  = null;
let rtActive     = false;
let rtLastScanTs = '';

function startScan(){
  const today = new Date().toISOString().slice(0,10).replace(/-/g,'');
  document.getElementById('runBtn').disabled = true;
  document.getElementById('dlBtn').classList.add('hidden');
  document.getElementById('resultCard').classList.add('hidden');
  document.getElementById('progWrap').style.display = 'block';
  document.getElementById('msg').textContent = '';
  setP(0,'KIS API 코스닥 파워맵최고 조회 중...');
  fetch(`/api/41/start?date=${today}`).then(r=>r.json()).then(r=>{
    if(r.error){showMsg(r.error);resetBtn();return;}
    listenProg();
  });
}
function rtStart(){
  fetch('/api/41/realtime/start').then(r=>r.json()).then(d=>{
    if(!d.ok){ showMsg(d.msg||'오류 발생'); return; }
    setRtActive(true);
    document.getElementById('progWrap').style.display = 'block';
    setP(0, '첫 번째 스캔 준비 중...');
    showMsg('실시간 스캔 시작됨 — 10분 간격으로 자동 반복됩니다.');
    rtLastScanTs = '';
    rtPoll();
  });
}
function rtStop(){
  fetch('/api/41/realtime/stop').then(r=>r.json()).then(()=>{
    setRtActive(false);
    if(rtPollTimer){ clearInterval(rtPollTimer); rtPollTimer = null; }
    showMsg('실시간 스캔 중지됨');
    setP(0,'');
    document.getElementById('progWrap').style.display = 'none';
  });
}
function setRtActive(v){
  rtActive = v;
  document.getElementById('rtStartBtn').classList.toggle('hidden', v);
  document.getElementById('rtStopBtn').classList.toggle('hidden', !v);
  const badge = document.getElementById('rtBadge');
  if(v){ badge.textContent='● 실시간 스캔 중'; badge.classList.remove('inactive'); }
  else { badge.textContent='● 대기 중';         badge.classList.add('inactive'); }
}
function rtPoll(){
  if(rtPollTimer) clearInterval(rtPollTimer);
  rtPollTimer = setInterval(()=>{
    if(!rtActive){ clearInterval(rtPollTimer); return; }
    fetch('/api/41/realtime/status').then(r=>r.json()).then(s=>{
      if(!s.running && rtActive){ setRtActive(false); clearInterval(rtPollTimer); return; }
      function fmtElapsed(sec){ if(!sec) return ''; const m=Math.floor(sec/60),s2=sec%60; return m>0?`${m}분 ${s2}초`:`${s2}초`; }
      const infoEl=document.getElementById('rtScanInfo');
      if(s.scan_no>0){
        const pst=s.prog_status; let info=`스캔 #${s.scan_no}`;
        if(pst==='loading'||pst==='running'){ info+=`  |  시작: ${s.scan_start||'—'}  |  조회 중...`; }
        else{ const el=s.scan_elapsed?`  소요: ${fmtElapsed(s.scan_elapsed)}`:''; info+=`  |  완료: ${s.last_scan||'—'}${el}`; if(s.next_scan) info+=`  |  다음: ${s.next_scan}`; }
        infoEl.textContent=info;
      }
      const pst=s.prog_status;
      if(pst==='loading'||pst==='running'){
        const pct = scanProgressPct(s.prog_current, s.prog_total);
        setP(pct, s.prog_total>0 ? scanProgressText(s.prog_current, s.prog_total, 'KIS 조회 중') : 'KIS API 조회 중...');
      }
      else if(pst==='done'){ const el=s.scan_elapsed?`  소요 ${fmtElapsed(s.scan_elapsed)}`:''; setP(100,`조회 완료 ✓  ${s.result_count}종목${el}`); }
      const justDone=(pst==='done')&&s.last_scan&&(s.last_scan!==rtLastScanTs);
      if(justDone||(pst==='done'&&s.result_count>0&&!rtLastScanTs)){ rtLastScanTs=s.last_scan||'_init_'; rtLoadResult(s.new_count); }
    });
  },4000);
}
function rtLoadResult(newCount){
  fetch('/api/41/result').then(r=>r.json()).then(d=>{
    if(!d.rows||d.rows.length===0){ showMsg(`✓ 파워맵최고 조회 완료 — 조건 충족 종목 없음`); return; }
    document.getElementById('resultCard').classList.remove('hidden');
    document.getElementById('dlBtn').classList.remove('hidden');
    renderResult(d.date,d.rows);
    highlightNewRows(d.new_codes||[]);
    const nc=d.new_codes&&d.new_codes.length;
    if(nc) showMsg(`🔔 파워맵최고 신규 ${nc}종목!  (${new Date().toLocaleTimeString()})`);
    else showMsg(`✓ 파워맵최고 ${d.rows.length}종목 조회 완료  (${new Date().toLocaleTimeString()})`);
  });
}
function highlightNewRows(newCodes){
  document.querySelectorAll('#tables tr').forEach(tr=>{
    if(!tr.cells||tr.cells.length<2) return;
    const code=tr.cells[1].textContent.trim();
    if(newCodes.includes(code)) tr.classList.add('row-new');
    else tr.classList.remove('row-new');
  });
}
function saveEmail(){
  const from=document.getElementById('emailFrom').value.trim();
  const pass=document.getElementById('emailPass').value.trim();
  const msgEl=document.getElementById('emailMsg'); const toVals=[];
  for(let i=1;i<=5;i++){ const v=(document.getElementById('emailTo'+i)||{}).value||''; if(v.trim()) toVals.push(v.trim()); }
  if(!from){msgEl.style.color='#f87171';msgEl.textContent='✗ 발신 주소를 입력하세요.';return;}
  if(!toVals.length){msgEl.style.color='#f87171';msgEl.textContent='✗ 수신자를 1명 이상 입력하세요.';return;}
  msgEl.style.color='#8b8fa8'; msgEl.textContent='저장 중...';
  const passParam=pass||'__keep__';
  let url='/api/41/email/save?from='+encodeURIComponent(from)+'&pass='+encodeURIComponent(passParam);
  toVals.forEach((v,i)=>{url+='&to'+(i+1)+'='+encodeURIComponent(v);});
  fetch(url).then(r=>r.json()).then(d=>{ msgEl.style.color=d.ok?'#4ade80':'#f87171'; msgEl.textContent=d.ok?`✓ 저장 완료 — 수신자 ${toVals.length}명`:('✗ '+(d.msg||'오류')); if(d.ok) loadEmailStatus(); });
}
function testEmail(){
  const btn=document.getElementById('testEmailBtn'); const msgEl=document.getElementById('emailMsg');
  btn.disabled=true; msgEl.style.color='#8b8fa8'; msgEl.textContent='📨 테스트 발송 중...';
  fetch('/api/41/email/test').then(r=>r.json()).then(d=>{ btn.disabled=false; msgEl.style.color=d.ok?'#4ade80':'#f87171'; msgEl.textContent=d.ok?'✓ 테스트 이메일 발송 완료!':'✗ 발송 실패: '+(d.msg||'알 수 없는 오류'); }).catch(()=>{btn.disabled=false;msgEl.textContent='네트워크 오류';});
}
function loadEmailStatus(){
  fetch('/api/41/email/status').then(r=>r.json()).then(s=>{
    const el=document.getElementById('emailStatus');
    if(s.from) document.getElementById('emailFrom').value=s.from;
    if(s.pass) document.getElementById('emailPass').value=s.pass;
    const toList=s.to_list||[];
    for(let i=1;i<=5;i++){ const inp=document.getElementById('emailTo'+i); if(inp) inp.value=toList[i-1]||''; }
    if(s.configured){ const toSummary=toList.map(v=>`<b>${v}</b>`).join(', '); el.innerHTML=`<span style="color:#4ade80">● 이메일 설정 완료</span>  발신: <b>${s.from}</b>  |  수신자 ${toList.length}명: ${toSummary}  |  앱비번: ${s.pass_hint}`; }
    else { el.innerHTML=`<span style="color:#f97316">⚠ 이메일 미설정</span>  — 발신 Gmail, 앱 비밀번호, 수신자를 입력하고 저장하세요.`; }
  });
}
fetch('/api/41/realtime/status').then(r=>r.json()).then(s=>{
  if(s.running){ setRtActive(true); document.getElementById('progWrap').style.display='block'; rtPoll(); if(s.result_count>0) rtLoadResult(s.new_count); }
});
loadEmailStatus();
"""

# ── 조건42 파워수급분석 실시간 스캔 + 이메일 UI ─────────────────────────────
_RT42_EXTRA_SECTION = """
<div style="margin-top:18px;border-top:1px solid #2a2d3a;padding-top:16px">
  <div style="font-size:.78rem;color:#8b5cf6;margin-bottom:10px">
    🔬 KIS OpenAPI 코스닥 TOP20 파워수급분석 — 업종·상승이유·하락이유·연관종목 자동분석 (10분 간격)
  </div>
  <div style="display:flex;gap:10px;flex-wrap:wrap;align-items:center;margin-bottom:12px">
    <button class="btn" id="rtStartBtn"
      style="background:#1a0a30;color:#8b5cf6;border:1px solid #4a1a8a"
      onclick="rtStart()">▶ 실시간 스캔 시작</button>
    <button class="btn hidden" id="rtStopBtn"
      style="background:#3a1010;color:#f87171;border:1px solid #5a2020"
      onclick="rtStop()">■ 중지</button>
    <span id="rtBadge" class="rt-badge inactive">● 대기 중</span>
    <span id="rtScanInfo" style="font-size:.78rem;color:#8b8fa8"></span>
  </div>
  <div style="background:#13161f;border:1px solid #2a2d3a;border-radius:10px;padding:14px 16px;margin-top:4px">
    <div style="font-size:.8rem;color:#8b8fa8;margin-bottom:10px">📧 이메일 알림 설정</div>
    <div style="display:flex;gap:8px;flex-wrap:wrap;align-items:center;margin-bottom:8px">
      <input type="email" id="emailFrom" placeholder="발신 Gmail 주소" autocomplete="off"
        style="background:#0f1117;border:1px solid #2a2d3a;border-radius:6px;
               color:#e0e0e0;padding:7px 11px;font-size:.8rem;width:220px">
      <input type="password" id="emailPass" placeholder="Gmail 앱 비밀번호 (16자리)" autocomplete="new-password"
        style="background:#0f1117;border:1px solid #2a2d3a;border-radius:6px;
               color:#e0e0e0;padding:7px 11px;font-size:.8rem;width:200px">
    </div>
    <div style="display:flex;gap:8px;flex-wrap:wrap;align-items:center;margin-bottom:10px">
      <span style="font-size:.78rem;color:#8b8fa8;white-space:nowrap">수신자 (최대 5명):</span>
      <input type="email" id="emailTo1" placeholder="수신자 1" autocomplete="off"
        style="background:#0f1117;border:1px solid #2a2d3a;border-radius:6px;
               color:#e0e0e0;padding:7px 10px;font-size:.8rem;width:190px">
      <input type="email" id="emailTo2" placeholder="수신자 2" autocomplete="off"
        style="background:#0f1117;border:1px solid #2a2d3a;border-radius:6px;
               color:#e0e0e0;padding:7px 10px;font-size:.8rem;width:190px">
      <input type="email" id="emailTo3" placeholder="수신자 3 (선택)" autocomplete="off"
        style="background:#0f1117;border:1px solid #2a2d3a;border-radius:6px;
               color:#e0e0e0;padding:7px 10px;font-size:.8rem;width:190px">
      <input type="email" id="emailTo4" placeholder="수신자 4 (선택)" autocomplete="off"
        style="background:#0f1117;border:1px solid #2a2d3a;border-radius:6px;
               color:#e0e0e0;padding:7px 10px;font-size:.8rem;width:190px">
      <input type="email" id="emailTo5" placeholder="수신자 5 (선택)" autocomplete="off"
        style="background:#0f1117;border:1px solid #2a2d3a;border-radius:6px;
               color:#e0e0e0;padding:7px 10px;font-size:.8rem;width:190px">
    </div>
    <div style="display:flex;gap:8px;flex-wrap:wrap;align-items:center">
      <button class="btn" onclick="saveEmail()"
        style="background:#1a0a30;color:#8b5cf6;border:1px solid #4a1a8a;padding:7px 18px">
        💾 저장</button>
      <button class="btn" id="testEmailBtn" onclick="testEmail()"
        style="background:#1a0a30;color:#8b5cf6;border:1px solid #4a1a8a;padding:7px 18px">
        📨 테스트 발송</button>
    </div>
    <div id="emailStatus" style="margin-top:8px;font-size:.78rem;color:#8b8fa8"></div>
    <div id="emailMsg"    style="margin-top:4px;font-size:.78rem"></div>
  </div>
  <div style="margin-top:8px;font-size:.72rem;color:#4a4d5e;line-height:1.7">
    ※ 스크리닝 시작 버튼으로 즉시 1회 분석 가능 (날짜 무관, 항상 현재 데이터)<br>
    ※ 분석: 업종·52주 고저·PER·거래량·양봉/음봉·외국인수급 기반 규칙 엔진<br>
    ※ 이메일: TOP20 신규 진입 종목 감지 시 자동 발송
  </div>
</div>
"""

_RT42_EXTRA_JS = r"""
/* ── 조건42 파워수급분석 실시간 스캔 컨트롤러 (10분 간격) ── */
let rtPollTimer  = null;
let rtActive     = false;
let rtLastScanTs = '';

function startScan(){
  const today = new Date().toISOString().slice(0,10).replace(/-/g,'');
  document.getElementById('runBtn').disabled = true;
  document.getElementById('dlBtn').classList.add('hidden');
  document.getElementById('resultCard').classList.add('hidden');
  document.getElementById('progWrap').style.display = 'block';
  document.getElementById('msg').textContent = '';
  setP(0,'KIS API 코스닥 TOP20 파워수급분석 중...');
  fetch(`/api/42/start?date=${today}`).then(r=>r.json()).then(r=>{
    if(r.error){showMsg(r.error);resetBtn();return;}
    listenProg();
  });
}
function rtStart(){
  fetch('/api/42/realtime/start').then(r=>r.json()).then(d=>{
    if(!d.ok){ showMsg(d.msg||'오류 발생'); return; }
    setRtActive(true);
    document.getElementById('progWrap').style.display = 'block';
    setP(0, '첫 번째 분석 준비 중...');
    showMsg('실시간 스캔 시작됨 — 10분 간격으로 자동 반복됩니다.');
    rtLastScanTs = '';
    rtPoll();
  });
}
function rtStop(){
  fetch('/api/42/realtime/stop').then(r=>r.json()).then(()=>{
    setRtActive(false);
    if(rtPollTimer){ clearInterval(rtPollTimer); rtPollTimer = null; }
    showMsg('실시간 스캔 중지됨');
    setP(0,'');
    document.getElementById('progWrap').style.display = 'none';
  });
}
function setRtActive(v){
  rtActive = v;
  document.getElementById('rtStartBtn').classList.toggle('hidden', v);
  document.getElementById('rtStopBtn').classList.toggle('hidden', !v);
  const badge = document.getElementById('rtBadge');
  if(v){ badge.textContent='● 분석 중'; badge.classList.remove('inactive'); }
  else { badge.textContent='● 대기 중'; badge.classList.add('inactive'); }
}
function rtPoll(){
  if(rtPollTimer) clearInterval(rtPollTimer);
  rtPollTimer = setInterval(()=>{
    if(!rtActive){ clearInterval(rtPollTimer); return; }
    fetch('/api/42/realtime/status').then(r=>r.json()).then(s=>{
      if(!s.running && rtActive){ setRtActive(false); clearInterval(rtPollTimer); return; }
      function fmtElapsed(sec){ if(!sec) return ''; const m=Math.floor(sec/60),s2=sec%60; return m>0?`${m}분 ${s2}초`:`${s2}초`; }
      const infoEl=document.getElementById('rtScanInfo');
      if(s.scan_no>0){
        const pst=s.prog_status; let info=`분석 #${s.scan_no}`;
        if(pst==='loading'||pst==='running'){ info+=`  |  시작: ${s.scan_start||'—'}  |  분석 중...`; }
        else{ const el=s.scan_elapsed?`  소요: ${fmtElapsed(s.scan_elapsed)}`:''; info+=`  |  완료: ${s.last_scan||'—'}${el}`; if(s.next_scan) info+=`  |  다음: ${s.next_scan}`; }
        infoEl.textContent=info;
      }
      const pst=s.prog_status;
      if(pst==='loading'||pst==='running'){
        const pct = scanProgressPct(s.prog_current, s.prog_total);
        setP(pct, s.prog_total>0 ? scanProgressText(s.prog_current, s.prog_total, 'KIS 분석 중') : 'KIS API 조회 및 분석 중...');
      }
      else if(pst==='done'){ const el=s.scan_elapsed?`  소요 ${fmtElapsed(s.scan_elapsed)}`:''; setP(100,`분석 완료 ✓  TOP${s.result_count}종목${el}`); }
      const justDone=(pst==='done')&&s.last_scan&&(s.last_scan!==rtLastScanTs);
      if(justDone||(pst==='done'&&s.result_count>0&&!rtLastScanTs)){ rtLastScanTs=s.last_scan||'_init_'; rtLoadResult(s.new_count); }
    });
  },4000);
}
function rtLoadResult(newCount){
  fetch('/api/42/result').then(r=>r.json()).then(d=>{
    if(!d.rows||d.rows.length===0){ showMsg(`✓ 파워수급분석 완료 — 결과 없음`); return; }
    document.getElementById('resultCard').classList.remove('hidden');
    document.getElementById('dlBtn').classList.remove('hidden');
    renderResult(d.date,d.rows);
    highlightNewRows(d.new_codes||[]);
    const nc=d.new_codes&&d.new_codes.length;
    if(nc) showMsg(`🔔 신규 진입 ${nc}종목!  TOP${d.rows.length}  (${new Date().toLocaleTimeString()})`);
    else showMsg(`✓ TOP${d.rows.length} 분석 완료  (${new Date().toLocaleTimeString()})`);
  });
}
function highlightNewRows(newCodes){
  document.querySelectorAll('.row-new').forEach(el=>el.classList.remove('row-new'));
}
function saveEmail(){
  const from=document.getElementById('emailFrom').value.trim();
  const pass=document.getElementById('emailPass').value.trim();
  const msgEl=document.getElementById('emailMsg'); const toVals=[];
  for(let i=1;i<=5;i++){ const v=(document.getElementById('emailTo'+i)||{}).value||''; if(v.trim()) toVals.push(v.trim()); }
  if(!from){msgEl.style.color='#f87171';msgEl.textContent='✗ 발신 주소를 입력하세요.';return;}
  if(!toVals.length){msgEl.style.color='#f87171';msgEl.textContent='✗ 수신자를 1명 이상 입력하세요.';return;}
  msgEl.style.color='#8b8fa8'; msgEl.textContent='저장 중...';
  const passParam=pass||'__keep__';
  let url='/api/42/email/save?from='+encodeURIComponent(from)+'&pass='+encodeURIComponent(passParam);
  toVals.forEach((v,i)=>{url+='&to'+(i+1)+'='+encodeURIComponent(v);});
  fetch(url).then(r=>r.json()).then(d=>{ msgEl.style.color=d.ok?'#4ade80':'#f87171'; msgEl.textContent=d.ok?`✓ 저장 완료 — 수신자 ${toVals.length}명`:('✗ '+(d.msg||'오류')); if(d.ok) loadEmailStatus(); });
}
function testEmail(){
  const btn=document.getElementById('testEmailBtn'); const msgEl=document.getElementById('emailMsg');
  btn.disabled=true; msgEl.style.color='#8b8fa8'; msgEl.textContent='📨 테스트 발송 중...';
  fetch('/api/42/email/test').then(r=>r.json()).then(d=>{ btn.disabled=false; msgEl.style.color=d.ok?'#4ade80':'#f87171'; msgEl.textContent=d.ok?'✓ 테스트 이메일 발송 완료!':'✗ 발송 실패: '+(d.msg||'알 수 없는 오류'); }).catch(()=>{btn.disabled=false;msgEl.textContent='네트워크 오류';});
}
function loadEmailStatus(){
  fetch('/api/42/email/status').then(r=>r.json()).then(s=>{
    const el=document.getElementById('emailStatus');
    if(s.from) document.getElementById('emailFrom').value=s.from;
    if(s.pass) document.getElementById('emailPass').value=s.pass;
    const toList=s.to_list||[];
    for(let i=1;i<=5;i++){ const inp=document.getElementById('emailTo'+i); if(inp) inp.value=toList[i-1]||''; }
    if(s.configured){ const toSummary=toList.map(v=>`<b>${v}</b>`).join(', '); el.innerHTML=`<span style="color:#4ade80">● 이메일 설정 완료</span>  발신: <b>${s.from}</b>  |  수신자 ${toList.length}명: ${toSummary}  |  앱비번: ${s.pass_hint}`; }
    else { el.innerHTML=`<span style="color:#8b5cf6">⚠ 이메일 미설정</span>  — 발신 Gmail, 앱 비밀번호, 수신자를 입력하고 저장하세요.`; }
  });
}
fetch('/api/42/realtime/status').then(r=>r.json()).then(s=>{
  if(s.running){ setRtActive(true); document.getElementById('progWrap').style.display='block'; rtPoll(); if(s.result_count>0) rtLoadResult(s.new_count); }
});
loadEmailStatus();
"""

_RT27_EXTRA_SECTION = """
<div style="margin-top:18px;border-top:1px solid #2a2d3a;padding-top:16px">
  <div style="display:flex;gap:10px;flex-wrap:wrap;align-items:center;margin-bottom:12px">
    <button class="btn" id="rtStartBtn"
      style="background:#2a0a10;color:#f43f5e;border:1px solid #6a1a25"
      onclick="rtStart()">▶ 실시간 스캔 시작</button>
    <button class="btn hidden" id="rtStopBtn"
      style="background:#3a1010;color:#f87171;border:1px solid #5a2020"
      onclick="rtStop()">■ 중지</button>
    <span id="rtBadge" class="rt-badge inactive">● 대기 중</span>
    <span id="rtScanInfo" style="font-size:.78rem;color:#8b8fa8"></span>
  </div>
  <div style="background:#13161f;border:1px solid #2a2d3a;border-radius:10px;padding:14px 16px;margin-top:4px">
    <div style="font-size:.8rem;color:#8b8fa8;margin-bottom:10px">📧 이메일 알림 설정</div>
    <div style="display:flex;gap:8px;flex-wrap:wrap;align-items:center;margin-bottom:8px">
      <input type="email" id="emailFrom" placeholder="발신 Gmail 주소" autocomplete="off"
        style="background:#0f1117;border:1px solid #2a2d3a;border-radius:6px;
               color:#e0e0e0;padding:7px 11px;font-size:.8rem;width:220px">
      <input type="password" id="emailPass" placeholder="Gmail 앱 비밀번호 (16자리)" autocomplete="new-password"
        style="background:#0f1117;border:1px solid #2a2d3a;border-radius:6px;
               color:#e0e0e0;padding:7px 11px;font-size:.8rem;width:200px">
    </div>
    <div style="display:flex;gap:8px;flex-wrap:wrap;align-items:center;margin-bottom:10px">
      <span style="font-size:.78rem;color:#8b8fa8;white-space:nowrap">수신자 (최대 5명):</span>
      <input type="email" id="emailTo1" placeholder="수신자 1" autocomplete="off"
        style="background:#0f1117;border:1px solid #2a2d3a;border-radius:6px;
               color:#e0e0e0;padding:7px 10px;font-size:.8rem;width:190px">
      <input type="email" id="emailTo2" placeholder="수신자 2" autocomplete="off"
        style="background:#0f1117;border:1px solid #2a2d3a;border-radius:6px;
               color:#e0e0e0;padding:7px 10px;font-size:.8rem;width:190px">
      <input type="email" id="emailTo3" placeholder="수신자 3 (선택)" autocomplete="off"
        style="background:#0f1117;border:1px solid #2a2d3a;border-radius:6px;
               color:#e0e0e0;padding:7px 10px;font-size:.8rem;width:190px">
      <input type="email" id="emailTo4" placeholder="수신자 4 (선택)" autocomplete="off"
        style="background:#0f1117;border:1px solid #2a2d3a;border-radius:6px;
               color:#e0e0e0;padding:7px 10px;font-size:.8rem;width:190px">
      <input type="email" id="emailTo5" placeholder="수신자 5 (선택)" autocomplete="off"
        style="background:#0f1117;border:1px solid #2a2d3a;border-radius:6px;
               color:#e0e0e0;padding:7px 10px;font-size:.8rem;width:190px">
    </div>
    <div style="display:flex;gap:8px;flex-wrap:wrap;align-items:center">
      <button class="btn" onclick="saveEmail()"
        style="background:#1e2a4a;color:#4f8ef7;border:1px solid #2a3a6a;padding:7px 18px">
        💾 저장</button>
      <button class="btn" id="testEmailBtn" onclick="testEmail()"
        style="background:#1a2a1a;color:#4ade80;border:1px solid #2a4a2a;padding:7px 18px">
        📨 테스트 발송</button>
    </div>
    <div id="emailStatus" style="margin-top:8px;font-size:.78rem;color:#8b8fa8"></div>
    <div id="emailMsg"    style="margin-top:4px;font-size:.78rem"></div>
  </div>
</div>
"""

_RT27_EXTRA_JS = r"""
/* ── 조건27 실시간 스캔 컨트롤러 ── */
let rtPollTimer  = null;
let rtActive     = false;
let rtLastScanTs = '';

function rtStart(){
  fetch('/api/27/realtime/start').then(r=>r.json()).then(d=>{
    if(!d.ok){ showMsg(d.msg||'오류 발생'); return; }
    setRtActive(true);
    document.getElementById('progWrap').style.display = 'block';
    setP(0, '첫 번째 스캔 준비 중...');
    showMsg('실시간 스캔 시작됨 — 3분 간격으로 자동 반복됩니다.');
    rtLastScanTs = '';
    rtPoll();
  });
}
function rtStop(){
  fetch('/api/27/realtime/stop').then(r=>r.json()).then(()=>{
    setRtActive(false);
    if(rtPollTimer){ clearInterval(rtPollTimer); rtPollTimer = null; }
    showMsg('실시간 스캔 중지됨');
    setP(0,'');
    document.getElementById('progWrap').style.display = 'none';
  });
}

function setRtActive(v){
  rtActive = v;
  document.getElementById('rtStartBtn').classList.toggle('hidden', v);
  document.getElementById('rtStopBtn').classList.toggle('hidden', !v);
  const badge = document.getElementById('rtBadge');
  if(v){ badge.textContent='● 실시간 스캔 중'; badge.classList.remove('inactive'); }
  else { badge.textContent='● 대기 중';         badge.classList.add('inactive'); }
}

function rtPoll(){
  if(rtPollTimer) clearInterval(rtPollTimer);
  rtPollTimer = setInterval(()=>{
    if(!rtActive){ clearInterval(rtPollTimer); return; }
    fetch('/api/27/realtime/status').then(r=>r.json()).then(s=>{
      if(!s.running && rtActive){ setRtActive(false); clearInterval(rtPollTimer); return; }
      function fmtElapsed(sec){
        if(!sec) return '';
        const m=Math.floor(sec/60), s2=sec%60;
        return m>0?`${m}분 ${s2}초`:`${s2}초`;
      }
      const infoEl=document.getElementById('rtScanInfo');
      if(s.scan_no>0){
        const pst=s.prog_status;
        let info=`스캔 #${s.scan_no}`;
        if(pst==='loading'||pst==='running'){
          info+=`  |  시작: ${s.scan_start||'—'}  |  진행 중...`;
        } else {
          const el=s.scan_elapsed?`  소요: ${fmtElapsed(s.scan_elapsed)}`:'';
          info+=`  |  시작: ${s.scan_start||'—'}  →  완료: ${s.last_scan||'—'}${el}`;
          if(s.next_scan) info+=`  |  다음 스캔: ${s.next_scan}`;
        }
        infoEl.textContent=info;
      }
      const pst=s.prog_status;
      if(pst==='loading') setP(0,'종목 리스트 불러오는 중...');
      else if(pst==='running'){
        const pct=s.prog_total>0?Math.round(s.prog_current/s.prog_total*100):0;
        setP(pct, scanProgressText(s.prog_current, s.prog_total, '스캔 중'));
      } else if(pst==='done'){
        const el=s.scan_elapsed?`  소요 ${fmtElapsed(s.scan_elapsed)}`:'';
        setP(100,`스캔 완료 ✓  ${s.result_count}개 종목 발견${el}`);
      }
      const justDone=(pst==='done')&&s.last_scan&&(s.last_scan!==rtLastScanTs);
      if(justDone||(pst==='done'&&s.result_count>0&&!rtLastScanTs)){
        rtLastScanTs=s.last_scan||'_init_';
        rtLoadResult(s.new_count);
      }
    });
  },5000);
}

function rtLoadResult(newCount){
  fetch('/api/27/result').then(r=>r.json()).then(d=>{
    if(!d.rows||d.rows.length===0){
      showMsg(`✓ 스캔 완료 — 조건 충족 종목 없음 (${new Date().toLocaleTimeString()})`);
      return;
    }
    document.getElementById('resultCard').classList.remove('hidden');
    document.getElementById('dlBtn').classList.remove('hidden');
    renderResult(d.date,d.rows);
    highlightNewRows(d.new_codes||[]);
    if(d.new_codes&&d.new_codes.length>0)
      showMsg(`🔔 신규 ${d.new_codes.length}개 종목 발견!  총 ${d.rows.length}개  (${new Date().toLocaleTimeString()})`);
    else
      showMsg(`✓ 스캔 완료 — ${d.rows.length}개 종목 (${new Date().toLocaleTimeString()})`);
  });
}

function highlightNewRows(newCodes){
  document.querySelectorAll('#tables tr').forEach(tr=>{
    if(!tr.cells||tr.cells.length<2) return;
    const code=tr.cells[1].textContent.trim();
    if(newCodes.includes(code)) tr.classList.add('row-new');
    else tr.classList.remove('row-new');
  });
}

function saveEmail(){
  const from=document.getElementById('emailFrom').value.trim();
  const pass=document.getElementById('emailPass').value.trim();
  const msgEl=document.getElementById('emailMsg');
  const toVals=[];
  for(let i=1;i<=5;i++){
    const v=(document.getElementById('emailTo'+i)||{}).value||'';
    if(v.trim()) toVals.push(v.trim());
  }
  if(!from){msgEl.style.color='#f87171';msgEl.textContent='✗ 발신 주소를 입력하세요.';return;}
  if(!toVals.length){msgEl.style.color='#f87171';msgEl.textContent='✗ 수신자를 1명 이상 입력하세요.';return;}
  msgEl.style.color='#8b8fa8'; msgEl.textContent='저장 중...';
  const passParam=pass||'__keep__';
  let url='/api/27/email/save?from='+encodeURIComponent(from)+'&pass='+encodeURIComponent(passParam);
  toVals.forEach((v,i)=>{url+='&to'+(i+1)+'='+encodeURIComponent(v);});
  fetch(url).then(r=>r.json()).then(d=>{
    msgEl.style.color=d.ok?'#4ade80':'#f87171';
    msgEl.textContent=d.ok
      ?`✓ 저장 완료 — 수신자 ${toVals.length}명 · 서버·페이지 재시작 후에도 자동 복원됩니다.`
      :('✗ '+(d.msg||'오류'));
    if(d.ok) loadEmailStatus();
  });
}

function testEmail(){
  const btn=document.getElementById('testEmailBtn');
  const msgEl=document.getElementById('emailMsg');
  btn.disabled=true; msgEl.style.color='#8b8fa8'; msgEl.textContent='📨 테스트 발송 중...';
  fetch('/api/27/email/test').then(r=>r.json()).then(d=>{
    btn.disabled=false;
    msgEl.style.color=d.ok?'#4ade80':'#f87171';
    msgEl.textContent=d.ok?'✓ 테스트 이메일 발송 완료! 수신함을 확인하세요.':'✗ 발송 실패: '+(d.msg||'알 수 없는 오류');
  }).catch(()=>{btn.disabled=false;msgEl.textContent='네트워크 오류';});
}

function loadEmailStatus(){
  fetch('/api/27/email/status').then(r=>r.json()).then(s=>{
    const el=document.getElementById('emailStatus');
    if(s.from) document.getElementById('emailFrom').value=s.from;
    if(s.pass) document.getElementById('emailPass').value=s.pass;
    const toList=s.to_list||[];
    for(let i=1;i<=5;i++){
      const inp=document.getElementById('emailTo'+i);
      if(inp) inp.value=toList[i-1]||'';
    }
    if(s.configured){
      const toSummary=toList.map(v=>`<b>${v}</b>`).join(', ');
      el.innerHTML=`<span style="color:#4ade80">● 이메일 설정 완료</span>`+
        `  발신: <b>${s.from}</b>  |  수신자 ${toList.length}명: ${toSummary}  |  앱비번: ${s.pass_hint}`;
    } else {
      el.innerHTML=`<span style="color:#f59e0b">⚠ 이메일 미설정</span>`+
        `  — 발신 Gmail, 앱 비밀번호, 수신자를 입력하고 저장하세요.`;
    }
  });
}

fetch('/api/27/realtime/status').then(r=>r.json()).then(s=>{
  if(s.running){
    setRtActive(true);
    document.getElementById('progWrap').style.display='block';
    rtPoll();
    if(s.result_count>0) rtLoadResult(s.new_count);
  }
});
loadEmailStatus();
"""


@app.route("/screener/<int:sid>")
def screener_page(sid):
    if sid not in SCREENERS:
        return "스크리너를 찾을 수 없습니다.", 404
    info = SCREENERS[sid]
    extra_section = (_RT14_EXTRA_SECTION if sid == 14 else
                     _RT19_EXTRA_SECTION if sid == 19 else
                     _RT20_EXTRA_SECTION if sid == 20 else
                     _RT21_EXTRA_SECTION if sid == 21 else
                     _RT22_EXTRA_SECTION if sid == 22 else
                     _RT25_EXTRA_SECTION if sid == 25 else
                     _RT27_EXTRA_SECTION if sid == 27 else
                     _RT28_EXTRA_SECTION if sid == 28 else
                     _RT30_EXTRA_SECTION if sid == 30 else
                     _S31_EXTRA_SECTION  if sid == 31 else
                     _S43_EXTRA_SECTION  if sid == 43 else
                     _RT44_EXTRA_SECTION if sid == 44 else
                     _RT46_EXTRA_SECTION if sid == 46 else
                     _RT33_EXTRA_SECTION if sid == 33 else
                     _RT35_EXTRA_SECTION if sid == 35 else
                     _RT37_EXTRA_SECTION if sid == 37 else
                     _S38_EXTRA_SECTION  if sid == 38 else
                     _RT39_EXTRA_SECTION if sid == 39 else
                     _RT40_EXTRA_SECTION if sid == 40 else
                     _RT41_EXTRA_SECTION if sid == 41 else
                     _RT42_EXTRA_SECTION if sid == 42 else
                     _S48_EXTRA_SECTION  if sid == 48 else
                     _S16_EXTRA_SECTION  if sid == 16 else "")
    extra_js      = (_RT14_EXTRA_JS      if sid == 14 else
                     _RT19_EXTRA_JS      if sid == 19 else
                     _RT20_EXTRA_JS      if sid == 20 else
                     _RT21_EXTRA_JS      if sid == 21 else
                     _RT22_EXTRA_JS      if sid == 22 else
                     _RT25_EXTRA_JS      if sid == 25 else
                     _RT27_EXTRA_JS      if sid == 27 else
                     _RT28_EXTRA_JS      if sid == 28 else
                     _RT30_EXTRA_JS      if sid == 30 else
                     _S31_EXTRA_JS       if sid == 31 else
                     _S43_EXTRA_JS       if sid == 43 else
                     _RT44_EXTRA_JS      if sid == 44 else
                     _RT46_EXTRA_JS      if sid == 46 else
                     _RT33_EXTRA_JS      if sid == 33 else
                     _RT35_EXTRA_JS      if sid == 35 else
                     _RT37_EXTRA_JS      if sid == 37 else
                     _S38_EXTRA_JS       if sid == 38 else
                     _RT39_EXTRA_JS      if sid == 39 else
                     _RT40_EXTRA_JS      if sid == 40 else
                     _RT41_EXTRA_JS      if sid == 41 else
                     _RT42_EXTRA_JS      if sid == 42 else
                     _S48_EXTRA_JS       if sid == 48 else
                     _S16_EXTRA_JS       if sid == 16 else "")
    html = (SCREENER_HTML
            .replace("{{TITLE}}",          info["title"])
            .replace("{{DESC}}",           info["desc"])
            .replace("{{ICON}}",           info["icon"])
            .replace("{{SID}}",            str(sid))
            .replace("{{COND_TAGS}}",      COND_TAGS.get(sid, ""))
            .replace("{{EXTRA_SECTION}}", extra_section)
            .replace("{{EXTRA_JS}}",      extra_js))
    return Response(html, mimetype="text/html")


# ── 조건31 전용 start 라우트 (find_mode 파라미터 처리) ──────────────────────
@app.route("/api/31/start")
def api_s31_start():
    st = _state[31]
    date_str  = request.args.get("date", datetime.now().strftime("%Y%m%d"))
    find_mode = int(request.args.get("find_mode", 9))
    st["find_mode"] = find_mode

    def _run():
        try:
            st["result_df"]   = RUNNER[31](date_str, st["progress"])
            st["result_date"] = date_str
        except Exception as e:
            print(f"[ERROR sid=31] {e}")
            st["progress"]["status"] = "done"

    st["progress"] = {"current": 0, "total": 0, "status": "loading"}
    st["worker"]   = threading.Thread(target=_run, daemon=True)
    st["worker"].start()
    return jsonify({"ok": True})


# ── 조건44 전용 start 라우트 (find_mode 파라미터 처리) ──────────────────────
@app.route("/api/44/start")
def api_s44_start():
    st = _state[44]
    date_str  = request.args.get("date", datetime.now().strftime("%Y%m%d"))
    find_mode = int(request.args.get("find_mode", 6))
    st["find_mode"] = find_mode

    def _run():
        try:
            st["result_df"]   = RUNNER[44](date_str, st["progress"])
            st["result_date"] = date_str
        except Exception as e:
            print(f"[ERROR sid=44] {e}")
            st["progress"]["status"] = "done"

    st["progress"] = {"current": 0, "total": 0, "status": "loading"}
    st["worker"]   = threading.Thread(target=_run, daemon=True)
    st["worker"].start()
    return jsonify({"ok": True})


@app.route("/api/44/realtime/start")
def api_rt44_start():
    global _rt44_scan_no, _rt44_scan_start, _rt44_last_scan, _rt44_next_scan, _rt44_scan_elapsed
    st = _state[44]
    if st["realtime"]:
        return jsonify({"ok": True, "msg": "이미 실행 중"})
    find_mode = int(request.args.get("find_mode", 6))
    st["find_mode"]   = find_mode
    st["realtime"]    = True
    st["known_codes"] = set()
    st["new_codes"]   = set()
    _rt44_scan_no      = 0
    _rt44_scan_start   = ""
    _rt44_last_scan    = ""
    _rt44_next_scan    = ""
    _rt44_scan_elapsed = 0
    t = threading.Thread(target=_run_realtime44, daemon=True)
    t.start()
    st["worker"] = t
    return jsonify({"ok": True})


@app.route("/api/44/realtime/stop")
def api_rt44_stop():
    _state[44]["realtime"] = False
    return jsonify({"ok": True})


@app.route("/api/44/realtime/status")
def api_rt44_status():
    st   = _state[44]
    prog = st["progress"]
    return jsonify({
        "running":       st["realtime"],
        "scan_no":       _rt44_scan_no,
        "scan_start":    _rt44_scan_start,
        "last_scan":     _rt44_last_scan,
        "next_scan":     _rt44_next_scan,
        "scan_elapsed":  _rt44_scan_elapsed,
        "new_count":     len(st["new_codes"]),
        "result_count":  0 if st["result_df"].empty else len(st["result_df"]),
        "prog_status":   prog.get("status",  "idle"),
        "prog_current":  prog.get("current", 0),
        "prog_total":    prog.get("total",   0),
    })


@app.route("/api/44/email/save")
def api_rt44_email_save():
    return api_email_save()


@app.route("/api/44/email/status")
def api_rt44_email_status():
    return api_email_status()


@app.route("/api/44/email/test")
def api_rt44_email_test():
    return api_email_test()


# ── 조건46 MSL2 실시간/이메일 라우트 ─────────────────────────────────────────
@app.route("/api/46/realtime/start")
def api_rt46_start():
    global _rt46_scan_no, _rt46_scan_start, _rt46_last_scan, _rt46_next_scan, _rt46_scan_elapsed
    st = _state[46]
    if st["realtime"]:
        return jsonify({"ok": True, "msg": "이미 실행 중"})
    st["realtime"]    = True
    st["known_codes"] = set()
    st["new_codes"]   = set()
    _rt46_scan_no      = 0
    _rt46_scan_start   = ""
    _rt46_last_scan    = ""
    _rt46_next_scan    = ""
    _rt46_scan_elapsed = 0
    t = threading.Thread(target=_run_realtime46, daemon=True)
    t.start()
    st["worker"] = t
    return jsonify({"ok": True})


@app.route("/api/46/realtime/stop")
def api_rt46_stop():
    _state[46]["realtime"] = False
    return jsonify({"ok": True})


@app.route("/api/46/realtime/status")
def api_rt46_status():
    st   = _state[46]
    prog = st["progress"]
    return jsonify({
        "running":       st["realtime"],
        "scan_no":       _rt46_scan_no,
        "scan_start":    _rt46_scan_start,
        "last_scan":     _rt46_last_scan,
        "next_scan":     _rt46_next_scan,
        "scan_elapsed":  _rt46_scan_elapsed,
        "new_count":     len(st["new_codes"]),
        "result_count":  0 if st["result_df"].empty else len(st["result_df"]),
        "prog_status":   prog.get("status",  "idle"),
        "prog_current":  prog.get("current", 0),
        "prog_total":    prog.get("total",   0),
    })


@app.route("/api/46/email/save")
def api_rt46_email_save():
    return api_email_save()


@app.route("/api/46/email/status")
def api_rt46_email_status():
    return api_email_status()


@app.route("/api/46/email/test")
def api_rt46_email_test():
    return api_email_test()


# ── 조건43 전용 start 라우트 (find_mode 파라미터 처리) ──────────────────────
@app.route("/api/43/start")
def api_s43_start():
    st = _state[43]
    date_str  = request.args.get("date", datetime.now().strftime("%Y%m%d"))
    find_mode = int(request.args.get("find_mode", 9))
    st["find_mode"] = find_mode

    def _run():
        try:
            st["result_df"]   = RUNNER[43](date_str, st["progress"])
            st["result_date"] = date_str
        except Exception as e:
            print(f"[ERROR sid=43] {e}")
            st["progress"]["status"] = "done"

    st["progress"] = {"current": 0, "total": 0, "status": "loading"}
    st["worker"]   = threading.Thread(target=_run, daemon=True)
    st["worker"].start()
    return jsonify({"ok": True})


# ── 조건48 전용 start 라우트 (find_mode 파라미터 처리) ──────────────────────
@app.route("/api/48/start")
def api_s48_start():
    st = _state[48]
    date_str = request.args.get("date", datetime.now().strftime("%Y%m%d"))
    find_mode = int(request.args.get("find_mode", 0))
    st["find_mode"] = find_mode

    def _run():
        try:
            st["result_df"] = RUNNER[48](date_str, st["progress"])
            st["result_date"] = date_str
        except Exception as e:
            print(f"[ERROR sid=48] {e}")
            st["progress"]["status"] = "done"

    st["progress"] = {"current": 0, "total": 0, "status": "loading"}
    st["worker"] = threading.Thread(target=_run, daemon=True)
    st["worker"].start()
    return jsonify({"ok": True})


@app.route("/api/<int:sid>/start")
def api_start(sid):
    if sid not in SCREENERS: return jsonify({"error": "없는 스크리너"}), 404
    st = _state[sid]
    date_str = request.args.get("date", datetime.now().strftime("%Y%m%d"))

    def _run():
        try:
            st["result_df"]   = RUNNER[sid](date_str, st["progress"])
            st["result_date"] = date_str
        except Exception as e:
            print(f"[ERROR sid={sid}] {e}")
            st["progress"]["status"] = "done"

    st["progress"] = {"current":0,"total":0,"status":"loading"}
    st["worker"]   = threading.Thread(target=_run, daemon=True)
    st["worker"].start()
    return jsonify({"ok": True})


@app.route("/api/<int:sid>/progress")
def api_progress(sid):
    if sid not in SCREENERS: return jsonify({"error": "없는 스크리너"}), 404
    def stream():
        while True:
            yield f"data: {json.dumps(_state[sid]['progress'])}\n\n"
            if _state[sid]["progress"]["status"] == "done": break
            time.sleep(0.5)
    return Response(stream(), mimetype="text/event-stream",
                    headers={"Cache-Control":"no-cache","X-Accel-Buffering":"no"})


@app.route("/api/<int:sid>/result")
def api_result(sid):
    if sid not in SCREENERS: return jsonify({"error": "없는 스크리너"}), 404
    st   = _state[sid]
    rows = [] if st["result_df"].empty else st["result_df"].to_dict(orient="records")
    resp = {"rows": rows, "date": st["result_date"]}
    if sid in (14, 19, 20, 21, 22, 27, 28, 30, 33, 35, 37, 39, 40, 41, 42, 44, 46):
        resp["new_codes"] = list(st.get("new_codes", set()))
    return jsonify(resp)


@app.route("/api/<int:sid>/download")
def api_download(sid):
    if sid not in SCREENERS: return "없는 스크리너", 404
    st = _state[sid]
    if st["result_df"].empty: return "결과 없음", 404
    buf = io.StringIO()
    st["result_df"].to_csv(buf, index=False, encoding="utf-8-sig")
    fname = f"screener{sid}_result_{st['result_date']}.csv"
    return Response(
        "\ufeff" + buf.getvalue(),
        mimetype="text/csv",
        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{fname}"}
    )


# ── 조건14 실시간 제어 라우트 ──────────────────────────────────────────────────

_rt14_scan_no       = 0   # 현재 스캔 회차
_rt14_scan_start    = ""  # 스캔 시작 시각  (HH:MM:SS)
_rt14_last_scan     = ""  # 마지막 스캔 완료 시각 (HH:MM:SS)
_rt14_next_scan     = ""  # 다음 스캔 예정 시각 (HH:MM:SS)
_rt14_scan_elapsed  = 0   # 마지막 스캔 소요 시간 (초)

@app.route("/api/14/realtime/start")
def api_rt14_start():
    global _rt14_scan_no, _rt14_scan_start, _rt14_last_scan, _rt14_next_scan, _rt14_scan_elapsed
    st = _state[14]
    if st["realtime"]:
        return jsonify({"ok": True, "msg": "이미 실행 중"})
    st["realtime"]      = True
    st["known_codes"]   = set()
    st["new_codes"]     = set()
    _rt14_scan_no       = 0
    _rt14_scan_start    = ""
    _rt14_last_scan     = ""
    _rt14_next_scan     = ""
    _rt14_scan_elapsed  = 0
    t = threading.Thread(target=_run_realtime14, daemon=True)
    t.start()
    st["worker"] = t
    return jsonify({"ok": True})


@app.route("/api/14/realtime/stop")
def api_rt14_stop():
    _state[14]["realtime"] = False
    return jsonify({"ok": True})


@app.route("/api/14/realtime/status")
def api_rt14_status():
    st   = _state[14]
    prog = st["progress"]
    return jsonify({
        "running":        st["realtime"],
        "scan_no":        _rt14_scan_no,
        "scan_start":     _rt14_scan_start,
        "last_scan":      _rt14_last_scan,
        "next_scan":      _rt14_next_scan,
        "scan_elapsed":   _rt14_scan_elapsed,
        "new_count":      len(st["new_codes"]),
        "result_count":   0 if st["result_df"].empty else len(st["result_df"]),
        "prog_status":    prog.get("status",  "idle"),
        "prog_current":   prog.get("current", 0),
        "prog_total":     prog.get("total",   0),
    })


@app.route("/api/14/email/save")
def api_email_save():
    """이메일 설정 저장 (파일 영구 보존). pass=__keep__ 이면 기존 비밀번호 유지."""
    global EMAIL_FROM, EMAIL_PASS, EMAIL_TO_LIST
    f = request.args.get("from", "").strip()
    p = request.args.get("pass", "").strip()
    if not f:
        return jsonify({"ok": False, "msg": "발신 주소를 입력하세요."})
    # 수신자: to1~to5 파라미터 수집 (빈 값 제외)
    to_list = []
    for i in range(1, MAX_RECIPIENTS + 1):
        v = request.args.get(f"to{i}", "").strip()
        if v:
            to_list.append(v)
    if not to_list:
        return jsonify({"ok": False, "msg": "수신자를 1명 이상 입력하세요."})
    EMAIL_FROM = f
    if p and p != "__keep__":
        EMAIL_PASS = p
    if not EMAIL_PASS:
        return jsonify({"ok": False, "msg": "앱 비밀번호를 입력하세요. (Gmail 앱 비밀번호 16자리)"})
    EMAIL_TO_LIST = to_list
    _save_email_config()
    print(f"[EMAIL] 설정 저장 → 발신:{f}  수신:{to_list}", flush=True)
    return jsonify({"ok": True})


# 구 경로 하위 호환 유지
@app.route("/api/14/email")
def api_rt14_email():
    return api_email_save()


@app.route("/api/14/email/status")
def api_email_status():
    """현재 이메일 설정 상태 반환 (로컬 앱 → 실제 값 반환, 페이지 자동 복원용)"""
    recipients = [e for e in EMAIL_TO_LIST if e]
    return jsonify({
        "from":      EMAIL_FROM,
        "to_list":   recipients,          # 수신자 리스트 (최대 5명)
        "pass":      EMAIL_PASS,
        "pass_set":  bool(EMAIL_PASS),
        "pass_hint": ("*" * (len(EMAIL_PASS) - 2) + EMAIL_PASS[-2:]) if len(EMAIL_PASS) > 2 else ("설정됨" if EMAIL_PASS else ""),
        "configured": bool(EMAIL_FROM and EMAIL_PASS and recipients),
    })


@app.route("/api/14/email/test")
def api_email_test():
    """테스트 이메일 즉시 발송"""
    try:
        recipients = [e for e in EMAIL_TO_LIST if e]
        if not (EMAIL_FROM and EMAIL_PASS and recipients):
            missing = []
            if not EMAIL_FROM:   missing.append("발신주소")
            if not EMAIL_PASS:   missing.append("앱비밀번호")
            if not recipients:   missing.append("수신주소")
            return jsonify({"ok": False, "msg": f"미설정 항목: {', '.join(missing)}"})
        to_str = ", ".join(recipients)
        body = (
            f"테스트 발송 시각: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
            f"발신: {EMAIL_FROM}\n"
            f"수신: {to_str}\n\n"
            f"이 메일이 도착했다면 이메일 설정이 정상입니다.\n"
            f"앱 비밀번호 길이: {len(EMAIL_PASS)}자리"
        )
        ok, err = _send_email_alert("[세력20평단] 이메일 테스트", body)
        return jsonify({"ok": ok, "msg": err})
    except Exception as e:
        return jsonify({"ok": False, "msg": f"서버 오류: {type(e).__name__}: {e}"})


_rt19_scan_no       = 0
_rt19_scan_start    = ""
_rt19_last_scan     = ""
_rt19_next_scan     = ""
_rt19_scan_elapsed  = 0

@app.route("/api/19/realtime/start")
def api_rt19_start():
    global _rt19_scan_no, _rt19_scan_start, _rt19_last_scan, _rt19_next_scan, _rt19_scan_elapsed
    st = _state[19]
    if st["realtime"]:
        return jsonify({"ok": True, "msg": "이미 실행 중"})
    st["realtime"]      = True
    st["known_codes"]   = set()
    st["new_codes"]     = set()
    _rt19_scan_no       = 0
    _rt19_scan_start    = ""
    _rt19_last_scan     = ""
    _rt19_next_scan     = ""
    _rt19_scan_elapsed  = 0
    t = threading.Thread(target=_run_realtime19, daemon=True)
    t.start()
    st["worker"] = t
    return jsonify({"ok": True})


@app.route("/api/19/realtime/stop")
def api_rt19_stop():
    _state[19]["realtime"] = False
    return jsonify({"ok": True})


@app.route("/api/19/realtime/status")
def api_rt19_status():
    st   = _state[19]
    prog = st["progress"]
    return jsonify({
        "running":        st["realtime"],
        "scan_no":        _rt19_scan_no,
        "scan_start":     _rt19_scan_start,
        "last_scan":      _rt19_last_scan,
        "next_scan":      _rt19_next_scan,
        "scan_elapsed":   _rt19_scan_elapsed,
        "new_count":      len(st["new_codes"]),
        "result_count":   0 if st["result_df"].empty else len(st["result_df"]),
        "prog_status":    prog.get("status",  "idle"),
        "prog_current":   prog.get("current", 0),
        "prog_total":     prog.get("total",   0),
    })


@app.route("/api/19/email/save")
def api_rt19_email_save():
    return api_email_save()


@app.route("/api/19/email/status")
def api_rt19_email_status():
    return api_email_status()


@app.route("/api/19/email/test")
def api_rt19_email_test():
    return api_email_test()


_rt20_scan_no      = 0
_rt20_scan_start   = ""
_rt20_last_scan    = ""
_rt20_next_scan    = ""
_rt20_scan_elapsed = 0

@app.route("/api/20/realtime/start")
def api_rt20_start():
    global _rt20_scan_no, _rt20_scan_start, _rt20_last_scan, _rt20_next_scan, _rt20_scan_elapsed
    st = _state[20]
    if st["realtime"]:
        return jsonify({"ok": True, "msg": "이미 실행 중"})
    st["realtime"]     = True
    st["known_codes"]  = set()
    st["new_codes"]    = set()
    _rt20_scan_no      = 0
    _rt20_scan_start   = ""
    _rt20_last_scan    = ""
    _rt20_next_scan    = ""
    _rt20_scan_elapsed = 0
    t = threading.Thread(target=_run_realtime20, daemon=True)
    t.start()
    st["worker"] = t
    return jsonify({"ok": True})


@app.route("/api/20/realtime/stop")
def api_rt20_stop():
    _state[20]["realtime"] = False
    return jsonify({"ok": True})


@app.route("/api/20/realtime/status")
def api_rt20_status():
    st   = _state[20]
    prog = st["progress"]
    return jsonify({
        "running":      st["realtime"],
        "scan_no":      _rt20_scan_no,
        "scan_start":   _rt20_scan_start,
        "last_scan":    _rt20_last_scan,
        "next_scan":    _rt20_next_scan,
        "scan_elapsed": _rt20_scan_elapsed,
        "new_count":    len(st["new_codes"]),
        "result_count": 0 if st["result_df"].empty else len(st["result_df"]),
        "prog_status":  prog.get("status",  "idle"),
        "prog_current": prog.get("current", 0),
        "prog_total":   prog.get("total",   0),
    })


@app.route("/api/20/email/save")
def api_rt20_email_save():
    return api_email_save()


@app.route("/api/20/email/status")
def api_rt20_email_status():
    return api_email_status()


@app.route("/api/20/email/test")
def api_rt20_email_test():
    return api_email_test()


@app.route("/api/16/email/send")
def api_16_email_send():
    """조건16 현재 스캔 결과를 이메일 발송"""
    try:
        recipients = [e for e in EMAIL_TO_LIST if e]
        if not (EMAIL_FROM and EMAIL_PASS and recipients):
            missing = []
            if not EMAIL_FROM: missing.append("발신주소")
            if not EMAIL_PASS: missing.append("앱비밀번호")
            if not recipients: missing.append("수신주소")
            return jsonify({"ok": False, "msg": f"이메일 미설정: {', '.join(missing)}"})

        st = _state[16]
        df = st["result_df"]
        if df.empty:
            return jsonify({"ok": False, "msg": "발송할 결과가 없습니다. 먼저 검색을 실행하세요."})

        now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        lines = [
            f"발송 시각: {now_str}",
            f"기준일:   {st['result_date']}",
            f"검색 종목 수: {len(df)}개",
            ""
        ]
        for _, r in df.iterrows():
            lines += [
                f"▶ {r['종목명']} ({r['종목코드']}) [{r['시장']}]",
                f"   종가: {int(r['종가']):,}원 | 전일대비: {r['전일대비(%)']}%",
                f"   RSI(21): {r['RSI(21)']} | S_RSI: {r['S_RSI']} | UPPER: {r['UPPER']}",
                f"   LOWER: {r['LOWER']} | 밴드폭: {r['밴드폭']} | 3일감소: {r['밴드폭3일감소']}",
                ""
            ]
        subject = f"[RSI밴드 압축돌파] {len(df)}개 종목 ({datetime.now().strftime('%m/%d %H:%M')})"
        ok, err = _send_email_alert(subject, "\n".join(lines))
        return jsonify({"ok": ok, "msg": err if not ok else f"✓ {len(recipients)}명에게 발송 완료"})
    except Exception as e:
        return jsonify({"ok": False, "msg": f"서버 오류: {type(e).__name__}: {e}"})


# ── 조건21 실시간 제어 라우트 ──────────────────────────────────────────────────

_rt21_scan_no      = 0
_rt21_scan_start   = ""
_rt21_last_scan    = ""
_rt21_next_scan    = ""
_rt21_scan_elapsed = 0


@app.route("/api/21/realtime/start")
def api_rt21_start():
    global _rt21_scan_no, _rt21_scan_start, _rt21_last_scan, _rt21_next_scan, _rt21_scan_elapsed
    st = _state[21]
    if st["realtime"]:
        return jsonify({"ok": True, "msg": "이미 실행 중"})
    st["realtime"]     = True
    st["known_codes"]  = set()
    st["new_codes"]    = set()
    _rt21_scan_no      = 0
    _rt21_scan_start   = ""
    _rt21_last_scan    = ""
    _rt21_next_scan    = ""
    _rt21_scan_elapsed = 0
    t = threading.Thread(target=_run_realtime21, daemon=True)
    t.start()
    st["worker"] = t
    return jsonify({"ok": True})


@app.route("/api/21/realtime/stop")
def api_rt21_stop():
    _state[21]["realtime"] = False
    return jsonify({"ok": True})


@app.route("/api/21/realtime/status")
def api_rt21_status():
    st   = _state[21]
    prog = st["progress"]
    return jsonify({
        "running":      st["realtime"],
        "scan_no":      _rt21_scan_no,
        "scan_start":   _rt21_scan_start,
        "last_scan":    _rt21_last_scan,
        "next_scan":    _rt21_next_scan,
        "scan_elapsed": _rt21_scan_elapsed,
        "new_count":    len(st["new_codes"]),
        "result_count": 0 if st["result_df"].empty else len(st["result_df"]),
        "prog_status":  prog.get("status",  "idle"),
        "prog_current": prog.get("current", 0),
        "prog_total":   prog.get("total",   0),
    })


@app.route("/api/21/email/save")
def api_rt21_email_save():
    return api_email_save()


@app.route("/api/21/email/status")
def api_rt21_email_status():
    return api_email_status()


@app.route("/api/21/email/test")
def api_rt21_email_test():
    return api_email_test()


# ── 조건22 실시간 제어 라우트 ──────────────────────────────────────────────────

_rt22_scan_no      = 0
_rt22_scan_start   = ""
_rt22_last_scan    = ""
_rt22_next_scan    = ""
_rt22_scan_elapsed = 0

@app.route("/api/22/realtime/start")
def api_rt22_start():
    global _rt22_scan_no, _rt22_scan_start, _rt22_last_scan, _rt22_next_scan, _rt22_scan_elapsed
    st = _state[22]
    if st["realtime"]:
        return jsonify({"ok": True, "msg": "이미 실행 중"})
    st["realtime"]     = True
    st["known_codes"]  = set()
    st["new_codes"]    = set()
    _rt22_scan_no      = 0
    _rt22_scan_start   = ""
    _rt22_last_scan    = ""
    _rt22_next_scan    = ""
    _rt22_scan_elapsed = 0
    t = threading.Thread(target=_run_realtime22, daemon=True)
    t.start()
    st["worker"] = t
    return jsonify({"ok": True})


@app.route("/api/22/realtime/stop")
def api_rt22_stop():
    _state[22]["realtime"] = False
    return jsonify({"ok": True})


@app.route("/api/22/realtime/status")
def api_rt22_status():
    st   = _state[22]
    prog = st["progress"]
    return jsonify({
        "running":      st["realtime"],
        "scan_no":      _rt22_scan_no,
        "scan_start":   _rt22_scan_start,
        "last_scan":    _rt22_last_scan,
        "next_scan":    _rt22_next_scan,
        "scan_elapsed": _rt22_scan_elapsed,
        "new_count":    len(st["new_codes"]),
        "result_count": 0 if st["result_df"].empty else len(st["result_df"]),
        "prog_status":  prog.get("status",  "idle"),
        "prog_current": prog.get("current", 0),
        "prog_total":   prog.get("total",   0),
    })


@app.route("/api/22/email/save")
def api_rt22_email_save():
    return api_email_save()


@app.route("/api/22/email/status")
def api_rt22_email_status():
    return api_email_status()


@app.route("/api/22/email/test")
def api_rt22_email_test():
    return api_email_test()


# ── 조건25 실시간 제어 라우트 ──────────────────────────────────────────────────

_rt25_scan_no      = 0
_rt25_scan_start   = ""
_rt25_last_scan    = ""
_rt25_next_scan    = ""
_rt25_scan_elapsed = 0


@app.route("/api/25/upload_prev", methods=["POST"])
def api_25_upload_prev():
    global _rt25_prev_rank_map, _rt25_prev_file_name, _rt25_prev_row_count
    if "file" not in request.files:
        return jsonify({"ok": False, "msg": "파일이 없습니다"})
    f = request.files["file"]
    content = f.read()
    rank_map = _parse_prev_ranking_csv(content)
    if not rank_map:
        return jsonify({"ok": False, "msg": "CSV 파싱 실패 — 순위·종목코드 컬럼 확인 필요"})
    _rt25_prev_rank_map  = rank_map
    _rt25_prev_file_name = f.filename
    _rt25_prev_row_count = len(rank_map)
    return jsonify({"ok": True, "count": _rt25_prev_row_count, "filename": f.filename})


@app.route("/api/25/prev_status")
def api_25_prev_status():
    return jsonify({"loaded":   bool(_rt25_prev_rank_map),
                    "filename": _rt25_prev_file_name,
                    "count":    _rt25_prev_row_count})


@app.route("/api/25/realtime/start")
def api_rt25_start():
    global _rt25_scan_no, _rt25_scan_start, _rt25_last_scan, _rt25_next_scan, _rt25_scan_elapsed
    st = _state[25]
    if st["realtime"]:
        return jsonify({"ok": True, "msg": "이미 실행 중"})
    st["realtime"]     = True
    st["known_codes"]  = set()
    st["new_codes"]    = set()
    _rt25_scan_no      = 0
    _rt25_scan_start   = ""
    _rt25_last_scan    = ""
    _rt25_next_scan    = ""
    _rt25_scan_elapsed = 0
    t = threading.Thread(target=_run_realtime25, daemon=True)
    t.start()
    st["worker"] = t
    return jsonify({"ok": True})


@app.route("/api/25/realtime/stop")
def api_rt25_stop():
    _state[25]["realtime"] = False
    return jsonify({"ok": True})


@app.route("/api/25/realtime/status")
def api_rt25_status():
    st   = _state[25]
    prog = st["progress"]
    return jsonify({
        "running":      st["realtime"],
        "scan_no":      _rt25_scan_no,
        "scan_start":   _rt25_scan_start,
        "last_scan":    _rt25_last_scan,
        "next_scan":    _rt25_next_scan,
        "scan_elapsed": _rt25_scan_elapsed,
        "new_count":    len(st["new_codes"]),
        "result_count": 0 if st["result_df"].empty else len(st["result_df"]),
        "prog_status":  prog.get("status",  "idle"),
        "prog_current": prog.get("current", 0),
        "prog_total":   prog.get("total",   0),
    })


@app.route("/api/25/email/save")
def api_rt25_email_save():
    return api_email_save()


@app.route("/api/25/email/status")
def api_rt25_email_status():
    return api_email_status()


@app.route("/api/25/email/test")
def api_rt25_email_test():
    return api_email_test()


# ── 조건27 실시간 제어 라우트 ──────────────────────────────────────────────────

@app.route("/api/27/realtime/start")
def api_rt27_start():
    global _rt27_scan_no, _rt27_scan_start, _rt27_last_scan, _rt27_next_scan, _rt27_scan_elapsed
    st = _state[27]
    if st["realtime"]:
        return jsonify({"ok": True, "msg": "이미 실행 중"})
    st["realtime"]     = True
    st["known_codes"]  = set()
    st["new_codes"]    = set()
    _rt27_scan_no      = 0
    _rt27_scan_start   = ""
    _rt27_last_scan    = ""
    _rt27_next_scan    = ""
    _rt27_scan_elapsed = 0
    t = threading.Thread(target=_run_realtime27, daemon=True)
    t.start()
    st["worker"] = t
    return jsonify({"ok": True})


@app.route("/api/27/realtime/stop")
def api_rt27_stop():
    _state[27]["realtime"] = False
    return jsonify({"ok": True})


@app.route("/api/27/realtime/status")
def api_rt27_status():
    st   = _state[27]
    prog = st["progress"]
    return jsonify({
        "running":      st["realtime"],
        "scan_no":      _rt27_scan_no,
        "scan_start":   _rt27_scan_start,
        "last_scan":    _rt27_last_scan,
        "next_scan":    _rt27_next_scan,
        "scan_elapsed": _rt27_scan_elapsed,
        "new_count":    len(st["new_codes"]),
        "result_count": 0 if st["result_df"].empty else len(st["result_df"]),
        "prog_status":  prog.get("status",  "idle"),
        "prog_current": prog.get("current", 0),
        "prog_total":   prog.get("total",   0),
    })


@app.route("/api/27/email/save")
def api_rt27_email_save():
    return api_email_save()


@app.route("/api/27/email/status")
def api_rt27_email_status():
    return api_email_status()


@app.route("/api/27/email/test")
def api_rt27_email_test():
    return api_email_test()


# ── 조건28 실시간 라우트 ──────────────────────────────────────
@app.route("/api/28/realtime/start")
def api_rt28_start():
    global _rt28_scan_no, _rt28_scan_start, _rt28_last_scan, _rt28_next_scan, _rt28_scan_elapsed
    st = _state[28]
    if st["realtime"]:
        return jsonify({"ok": True, "msg": "이미 실행 중"})
    st["realtime"] = True
    st["known_codes"] = set()
    st["new_codes"]   = set()
    _rt28_scan_no      = 0
    _rt28_scan_start   = ""
    _rt28_last_scan    = ""
    _rt28_next_scan    = ""
    _rt28_scan_elapsed = 0
    t = threading.Thread(target=_run_realtime28, daemon=True)
    t.start()
    st["worker"] = t
    return jsonify({"ok": True})


@app.route("/api/28/realtime/stop")
def api_rt28_stop():
    _state[28]["realtime"] = False
    return jsonify({"ok": True})


@app.route("/api/28/realtime/status")
def api_rt28_status():
    st   = _state[28]
    prog = st["progress"]
    return jsonify({
        "running":       st["realtime"],
        "scan_no":       _rt28_scan_no,
        "scan_start":    _rt28_scan_start,
        "last_scan":     _rt28_last_scan,
        "next_scan":     _rt28_next_scan,
        "scan_elapsed":  _rt28_scan_elapsed,
        "new_count":     len(st["new_codes"]),
        "result_count":  0 if st["result_df"].empty else len(st["result_df"]),
        "prog_status":   prog.get("status",  "idle"),
        "prog_current":  prog.get("current", 0),
        "prog_total":    prog.get("total",   0),
    })


@app.route("/api/28/email/save")
def api_rt28_email_save():
    return api_email_save()


@app.route("/api/28/email/status")
def api_rt28_email_status():
    return api_email_status()


@app.route("/api/28/email/test")
def api_rt28_email_test():
    return api_email_test()


# ── 조건30 실시간 라우트 ──────────────────────────────────────
@app.route("/api/30/realtime/start")
def api_rt30_start():
    global _rt30_scan_no, _rt30_scan_start, _rt30_last_scan, _rt30_next_scan, _rt30_scan_elapsed
    st = _state[30]
    if st["realtime"]:
        return jsonify({"ok": True, "msg": "이미 실행 중"})
    st["realtime"] = True
    st["known_codes"] = set()
    st["new_codes"]   = set()
    _rt30_scan_no      = 0
    _rt30_scan_start   = ""
    _rt30_last_scan    = ""
    _rt30_next_scan    = ""
    _rt30_scan_elapsed = 0
    t = threading.Thread(target=_run_realtime30, daemon=True)
    t.start()
    st["worker"] = t
    return jsonify({"ok": True})


@app.route("/api/30/realtime/stop")
def api_rt30_stop():
    _state[30]["realtime"] = False
    return jsonify({"ok": True})


@app.route("/api/30/realtime/status")
def api_rt30_status():
    st   = _state[30]
    prog = st["progress"]
    return jsonify({
        "running":       st["realtime"],
        "scan_no":       _rt30_scan_no,
        "scan_start":    _rt30_scan_start,
        "last_scan":     _rt30_last_scan,
        "next_scan":     _rt30_next_scan,
        "scan_elapsed":  _rt30_scan_elapsed,
        "new_count":     len(st["new_codes"]),
        "result_count":  0 if st["result_df"].empty else len(st["result_df"]),
        "prog_status":   prog.get("status",  "idle"),
        "prog_current":  prog.get("current", 0),
        "prog_total":    prog.get("total",   0),
    })


@app.route("/api/30/email/save")
def api_rt30_email_save():
    return api_email_save()


@app.route("/api/30/email/status")
def api_rt30_email_status():
    return api_email_status()


@app.route("/api/30/email/test")
def api_rt30_email_test():
    return api_email_test()


# ── 조건33 실시간 라우트 ──────────────────────────────────────
@app.route("/api/33/realtime/start")
def api_rt33_start():
    global _rt33_scan_no, _rt33_scan_start, _rt33_last_scan, _rt33_next_scan, _rt33_scan_elapsed
    st = _state[33]
    if st["realtime"]:
        return jsonify({"ok": True, "msg": "이미 실행 중"})
    st["realtime"] = True
    st["known_codes"] = set()
    st["new_codes"]   = set()
    _rt33_scan_no      = 0
    _rt33_scan_start   = ""
    _rt33_last_scan    = ""
    _rt33_next_scan    = ""
    _rt33_scan_elapsed = 0
    t = threading.Thread(target=_run_realtime33, daemon=True)
    t.start()
    st["worker"] = t
    return jsonify({"ok": True})


@app.route("/api/33/realtime/stop")
def api_rt33_stop():
    _state[33]["realtime"] = False
    return jsonify({"ok": True})


@app.route("/api/33/realtime/status")
def api_rt33_status():
    st   = _state[33]
    prog = st["progress"]
    return jsonify({
        "running":       st["realtime"],
        "scan_no":       _rt33_scan_no,
        "scan_start":    _rt33_scan_start,
        "last_scan":     _rt33_last_scan,
        "next_scan":     _rt33_next_scan,
        "scan_elapsed":  _rt33_scan_elapsed,
        "new_count":     len(st["new_codes"]),
        "result_count":  0 if st["result_df"].empty else len(st["result_df"]),
        "prog_status":   prog.get("status",  "idle"),
        "prog_current":  prog.get("current", 0),
        "prog_total":    prog.get("total",   0),
    })


@app.route("/api/33/email/save")
def api_rt33_email_save():
    return api_email_save()


@app.route("/api/33/email/status")
def api_rt33_email_status():
    return api_email_status()


@app.route("/api/33/email/test")
def api_rt33_email_test():
    return api_email_test()


# ── 조건35 실시간 라우트 ──────────────────────────────────────
@app.route("/api/35/realtime/start")
def api_rt35_start():
    global _rt35_scan_no, _rt35_scan_start, _rt35_last_scan, _rt35_next_scan, _rt35_scan_elapsed
    st = _state[35]
    if st["realtime"]:
        return jsonify({"ok": True, "msg": "이미 실행 중"})
    st["realtime"]    = True
    st["known_codes"] = set()
    st["new_codes"]   = set()
    _rt35_scan_no      = 0
    _rt35_scan_start   = ""
    _rt35_last_scan    = ""
    _rt35_next_scan    = ""
    _rt35_scan_elapsed = 0
    t = threading.Thread(target=_run_realtime35, daemon=True)
    t.start()
    st["worker"] = t
    return jsonify({"ok": True})


@app.route("/api/35/realtime/stop")
def api_rt35_stop():
    _state[35]["realtime"] = False
    return jsonify({"ok": True})


@app.route("/api/35/realtime/status")
def api_rt35_status():
    st   = _state[35]
    prog = st["progress"]
    return jsonify({
        "running":       st["realtime"],
        "scan_no":       _rt35_scan_no,
        "scan_start":    _rt35_scan_start,
        "last_scan":     _rt35_last_scan,
        "next_scan":     _rt35_next_scan,
        "scan_elapsed":  _rt35_scan_elapsed,
        "new_count":     len(st["new_codes"]),
        "result_count":  0 if st["result_df"].empty else len(st["result_df"]),
        "prog_status":   prog.get("status",  "idle"),
        "prog_current":  prog.get("current", 0),
        "prog_total":    prog.get("total",   0),
    })


@app.route("/api/35/email/save")
def api_rt35_email_save():
    return api_email_save()


@app.route("/api/35/email/status")
def api_rt35_email_status():
    return api_email_status()


@app.route("/api/35/email/test")
def api_rt35_email_test():
    return api_email_test()


@app.route("/api/37/realtime/start")
def api_rt37_start():
    global _rt37_scan_no, _rt37_scan_start, _rt37_last_scan, _rt37_next_scan, _rt37_scan_elapsed
    st = _state[37]
    if st["realtime"]:
        return jsonify({"ok": True, "msg": "이미 실행 중"})
    st["realtime"]    = True
    st["known_codes"] = set()
    st["new_codes"]   = set()
    _rt37_scan_no      = 0
    _rt37_scan_start   = ""
    _rt37_last_scan    = ""
    _rt37_next_scan    = ""
    _rt37_scan_elapsed = 0
    t = threading.Thread(target=_run_realtime37, daemon=True)
    t.start()
    st["worker"] = t
    return jsonify({"ok": True})


@app.route("/api/37/realtime/stop")
def api_rt37_stop():
    _state[37]["realtime"] = False
    return jsonify({"ok": True})


@app.route("/api/37/realtime/status")
def api_rt37_status():
    st   = _state[37]
    prog = st["progress"]
    return jsonify({
        "running":       st["realtime"],
        "scan_no":       _rt37_scan_no,
        "scan_start":    _rt37_scan_start,
        "last_scan":     _rt37_last_scan,
        "next_scan":     _rt37_next_scan,
        "scan_elapsed":  _rt37_scan_elapsed,
        "new_count":     len(st["new_codes"]),
        "result_count":  0 if st["result_df"].empty else len(st["result_df"]),
        "prog_status":   prog.get("status",  "idle"),
        "prog_current":  prog.get("current", 0),
        "prog_total":    prog.get("total",   0),
    })


@app.route("/api/37/email/save")
def api_rt37_email_save():
    return api_email_save()


@app.route("/api/37/email/status")
def api_rt37_email_status():
    return api_email_status()


@app.route("/api/37/email/test")
def api_rt37_email_test():
    return api_email_test()


@app.route("/api/39/realtime/start")
def api_rt39_start():
    global _rt39_scan_no, _rt39_scan_start, _rt39_last_scan, _rt39_next_scan, _rt39_scan_elapsed
    st = _state[39]
    if st["realtime"]:
        return jsonify({"ok": True, "msg": "이미 실행 중"})
    st["realtime"]    = True
    st["known_codes"] = set()
    st["new_codes"]   = set()
    _rt39_scan_no      = 0
    _rt39_scan_start   = ""
    _rt39_last_scan    = ""
    _rt39_next_scan    = ""
    _rt39_scan_elapsed = 0
    t = threading.Thread(target=_run_realtime39, daemon=True)
    t.start()
    st["worker"] = t
    return jsonify({"ok": True})


@app.route("/api/39/realtime/stop")
def api_rt39_stop():
    _state[39]["realtime"] = False
    return jsonify({"ok": True})


@app.route("/api/39/realtime/status")
def api_rt39_status():
    st   = _state[39]
    prog = st["progress"]
    return jsonify({
        "running":       st["realtime"],
        "scan_no":       _rt39_scan_no,
        "scan_start":    _rt39_scan_start,
        "last_scan":     _rt39_last_scan,
        "next_scan":     _rt39_next_scan,
        "scan_elapsed":  _rt39_scan_elapsed,
        "new_count":     len(st["new_codes"]),
        "result_count":  0 if st["result_df"].empty else len(st["result_df"]),
        "prog_status":   prog.get("status",  "idle"),
        "prog_current":  prog.get("current", 0),
        "prog_total":    prog.get("total",   0),
    })


@app.route("/api/39/email/save")
def api_rt39_email_save():
    return api_email_save()


@app.route("/api/39/email/status")
def api_rt39_email_status():
    return api_email_status()


@app.route("/api/39/email/test")
def api_rt39_email_test():
    return api_email_test()


@app.route("/api/40/realtime/start")
def api_rt40_start():
    global _rt40_scan_no, _rt40_scan_start, _rt40_last_scan, _rt40_next_scan, _rt40_scan_elapsed
    st = _state[40]
    if st["realtime"]:
        return jsonify({"ok": True, "msg": "이미 실행 중"})
    st["realtime"]    = True
    st["known_codes"] = set()
    st["new_codes"]   = set()
    _rt40_scan_no      = 0
    _rt40_scan_start   = ""
    _rt40_last_scan    = ""
    _rt40_next_scan    = ""
    _rt40_scan_elapsed = 0
    t = threading.Thread(target=_run_realtime40, daemon=True)
    t.start()
    st["worker"] = t
    return jsonify({"ok": True})


@app.route("/api/40/realtime/stop")
def api_rt40_stop():
    _state[40]["realtime"] = False
    return jsonify({"ok": True})


@app.route("/api/40/realtime/status")
def api_rt40_status():
    st   = _state[40]
    prog = st["progress"]
    return jsonify({
        "running":       st["realtime"],
        "scan_no":       _rt40_scan_no,
        "scan_start":    _rt40_scan_start,
        "last_scan":     _rt40_last_scan,
        "next_scan":     _rt40_next_scan,
        "scan_elapsed":  _rt40_scan_elapsed,
        "new_count":     len(st["new_codes"]),
        "result_count":  0 if st["result_df"].empty else len(st["result_df"]),
        "prog_status":   prog.get("status",  "idle"),
        "prog_current":  prog.get("current", 0),
        "prog_total":    prog.get("total",   0),
    })


@app.route("/api/40/email/save")
def api_rt40_email_save():
    return api_email_save()


@app.route("/api/40/email/status")
def api_rt40_email_status():
    return api_email_status()


@app.route("/api/40/email/test")
def api_rt40_email_test():
    return api_email_test()


@app.route("/api/41/realtime/start")
def api_rt41_start():
    global _rt41_scan_no, _rt41_scan_start, _rt41_last_scan, _rt41_next_scan, _rt41_scan_elapsed
    st = _state[41]
    if st["realtime"]:
        return jsonify({"ok": True, "msg": "이미 실행 중"})
    st["realtime"]    = True
    st["known_codes"] = set()
    st["new_codes"]   = set()
    _rt41_scan_no      = 0
    _rt41_scan_start   = ""
    _rt41_last_scan    = ""
    _rt41_next_scan    = ""
    _rt41_scan_elapsed = 0
    t = threading.Thread(target=_run_realtime41, daemon=True)
    t.start()
    st["worker"] = t
    return jsonify({"ok": True})


@app.route("/api/41/realtime/stop")
def api_rt41_stop():
    _state[41]["realtime"] = False
    return jsonify({"ok": True})


@app.route("/api/41/realtime/status")
def api_rt41_status():
    st   = _state[41]
    prog = st["progress"]
    return jsonify({
        "running":       st["realtime"],
        "scan_no":       _rt41_scan_no,
        "scan_start":    _rt41_scan_start,
        "last_scan":     _rt41_last_scan,
        "next_scan":     _rt41_next_scan,
        "scan_elapsed":  _rt41_scan_elapsed,
        "new_count":     len(st["new_codes"]),
        "result_count":  0 if st["result_df"].empty else len(st["result_df"]),
        "prog_status":   prog.get("status",  "idle"),
        "prog_current":  prog.get("current", 0),
        "prog_total":    prog.get("total",   0),
    })


@app.route("/api/41/email/save")
def api_rt41_email_save():
    return api_email_save()


@app.route("/api/41/email/status")
def api_rt41_email_status():
    return api_email_status()


@app.route("/api/41/email/test")
def api_rt41_email_test():
    return api_email_test()


@app.route("/api/42/realtime/start")
def api_rt42_start():
    global _rt42_scan_no, _rt42_scan_start, _rt42_last_scan, _rt42_next_scan, _rt42_scan_elapsed
    st = _state[42]
    if st["realtime"]:
        return jsonify({"ok": True, "msg": "이미 실행 중"})
    st["realtime"]    = True
    st["known_codes"] = set()
    st["new_codes"]   = set()
    _rt42_scan_no      = 0
    _rt42_scan_start   = ""
    _rt42_last_scan    = ""
    _rt42_next_scan    = ""
    _rt42_scan_elapsed = 0
    t = threading.Thread(target=_run_realtime42, daemon=True)
    t.start()
    st["worker"] = t
    return jsonify({"ok": True})


@app.route("/api/42/realtime/stop")
def api_rt42_stop():
    _state[42]["realtime"] = False
    return jsonify({"ok": True})


@app.route("/api/42/realtime/status")
def api_rt42_status():
    st   = _state[42]
    prog = st["progress"]
    return jsonify({
        "running":       st["realtime"],
        "scan_no":       _rt42_scan_no,
        "scan_start":    _rt42_scan_start,
        "last_scan":     _rt42_last_scan,
        "next_scan":     _rt42_next_scan,
        "scan_elapsed":  _rt42_scan_elapsed,
        "new_count":     len(st["new_codes"]),
        "result_count":  0 if st["result_df"].empty else len(st["result_df"]),
        "prog_status":   prog.get("status",  "idle"),
        "prog_current":  prog.get("current", 0),
        "prog_total":    prog.get("total",   0),
    })


@app.route("/api/42/email/save")
def api_rt42_email_save():
    return api_email_save()


@app.route("/api/42/email/status")
def api_rt42_email_status():
    return api_email_status()


@app.route("/api/42/email/test")
def api_rt42_email_test():
    return api_email_test()


@app.route("/api/38/upload", methods=["POST"])
def api_38_upload():
    """조건38: 차트 이미지 업로드 → 패턴 추출"""
    global _p38_image_b64, _p38_ref_pattern
    if "chart" not in request.files:
        return jsonify({"ok": False, "msg": "파일이 없습니다."})
    f = request.files["chart"]
    if not f.filename:
        return jsonify({"ok": False, "msg": "파일명이 없습니다."})
    image_bytes = f.read()
    if len(image_bytes) > 20 * 1024 * 1024:
        return jsonify({"ok": False, "msg": "파일 크기가 너무 큽니다 (최대 20MB)."})
    if not _PIL_AVAILABLE:
        return jsonify({"ok": False, "msg": "서버에 Pillow 라이브러리가 없습니다. pip install Pillow"})
    pattern = _extract_chart_pattern_38(image_bytes)
    if pattern is None or len(pattern) < 10:
        return jsonify({"ok": False, "msg": "이미지에서 패턴을 추출하지 못했습니다. 다른 이미지를 시도해보세요."})
    mime = f.mimetype or "image/jpeg"
    _p38_image_b64   = f"data:{mime};base64," + _base64.b64encode(image_bytes).decode()
    _p38_ref_pattern = pattern
    return jsonify({"ok": True, "pattern": pattern, "image_b64": _p38_image_b64,
                    "points": len(pattern)})


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8888))
    print(f"주식 스크리너 허브 시작: http://localhost:{port}")
    app.run(host="0.0.0.0", port=port, debug=False)
