# -*- coding: utf-8 -*-
"""
조건64 — 한투 신호봇 스캐너 (standalone / 실주문 없음 · 신호계산 전용)
======================================================================
주식랜딩페이지 app.py 의 조건64 "1회 스캔"(_hantoo_candidates)과 동일 로직.

로직
  ① 급등테마 선정 (= 조건63)
       · 네이버 테마분류 전체 로드
       · 각 종목의 두 시점 "1개월 수익률"  (오늘 기준 / 2주=10거래일 전 기준)
       · 테마별 평균수익률로 오늘/2주전 순위 → 순위변동(=2주전순위-현재순위)
       · 순위변동 상위 10개 테마 = 급등테마
  ② 급등테마 구성종목 중 0봉전 볼린저밴드(20,2) 상단 상향돌파(오늘 첫 돌파) 종목
  ③ 같은 테마에서 0봉전 돌파가 3종목 이상이면 그 클러스터 중 '시가총액 최대' 종목 = ★진입대상
  ④ 각 후보에 대해  진입가=당일종가 · 손절=−1.5ATR(14) · 익절=+3ATR(14)
       매수 300만원 기준 수량 = 300만 ÷ 진입가

⚠️ 실주문 기능은 없습니다. 계산된 신호를 참고해 매매는 사용자가 직접 하세요.

설치:  pip install finance-datareader pandas numpy requests beautifulsoup4 lxml
실행:  python condition64_hantoo_scanner.py
"""

import json
import re
import warnings
from datetime import datetime, timedelta
from concurrent.futures import ThreadPoolExecutor

import numpy as np
import pandas as pd
import requests
import FinanceDataReader as fdr

warnings.filterwarnings("ignore")

# ══════════════════════════════════════════════════════════════════════════════
# 설정값 (app.py 조건63/64와 동일)
# ══════════════════════════════════════════════════════════════════════════════
TARGET_DATE     = datetime.now().strftime("%Y%m%d")  # 기준일 (예: "20260904")
TOP_N           = 10          # 급등테마 개수
TWO_WEEK_BARS   = 10          # 2주 = 10 거래일
THEME_MIN_MEMB  = 3           # 테마 순위 산정 최소 유효 구성종목 수
BB_LEN          = 20          # 볼린저밴드 기간
BB_K            = 2.0         # 볼린저밴드 표준편차 배수
BB_WINDOW       = 3           # 돌파 탐색 최근 봉수 (0봉전만 사용)
BB_LOOKBACK     = 120         # 일봉 조회 캘린더 일수
CLUSTER_MIN     = 3           # 같은 테마 0봉전 최소 종목수 (진입대상 판정)
BUY_KRW         = 3_000_000   # 매수 금액
ATR_STOP        = 1.5         # 손절 배수
ATR_TARGET      = 3.0         # 익절 배수
WORKERS         = 8           # 네이버/FDR 병렬 워커 수
HDRS            = {"User-Agent": "Mozilla/5.0"}


# ══════════════════════════════════════════════════════════════════════════════
# 데이터 수집 유틸
# ══════════════════════════════════════════════════════════════════════════════
def _parse_eok(s):
    """네이버 시총 문자열 → 억 단위 정수.  "1,575조 5,721억" → 15755721,  "5,721억" → 5721."""
    s = str(s).replace(",", "").strip()
    if not s or s == "-":
        return 0
    eok = 0
    m_jo  = re.search(r"(\d+)\s*조", s)
    m_eok = re.search(r"(\d+)\s*억", s)
    if m_jo:
        eok += int(m_jo.group(1)) * 10000
    if m_eok:
        eok += int(m_eok.group(1))
    return eok


def naver_stock_meta(code):
    """돌파 후보 1종목의 (시총[억], 시장명) 조회. FDR KRX 목록이 불안정하여 종목별 네이버 API 사용."""
    marcap_eok, market = 0, ""
    try:
        r = requests.get(f"https://m.stock.naver.com/api/stock/{code}/integration",
                         headers=HDRS, timeout=8)
        d = json.loads(r.text)
        for t in d.get("totalInfos", []):
            if t.get("code") == "marketValue":
                marcap_eok = _parse_eok(t.get("value", "-"))
                break
    except Exception:
        pass
    try:
        r = requests.get(f"https://m.stock.naver.com/api/stock/{code}/basic",
                         headers=HDRS, timeout=8)
        b = json.loads(r.text)
        market = b.get("stockExchangeName", "") or ""
    except Exception:
        pass
    return marcap_eok, market


def naver_theme_detail(args):
    """한 테마의 구성종목 [{code,name},...] 스크랩."""
    no, nm = args
    try:
        from bs4 import BeautifulSoup
        url = f"https://finance.naver.com/sise/sise_group_detail.naver?type=theme&no={no}"
        r = requests.get(url, headers=HDRS, timeout=12)
        r.encoding = "euc-kr"
        soup = BeautifulSoup(r.text, "lxml")
        out = []
        for a in soup.select("div.name_area a"):
            m = re.search(r"code=(\d{6})", a.get("href", ""))
            name = a.get_text(strip=True)
            if m and name:
                out.append({"code": m.group(1), "name": name})
        return nm, out
    except Exception:
        return nm, []


def load_theme_map():
    """네이버 테마분류 전체 → {테마명:[{code,name}]}."""
    r = requests.get("https://finance.naver.com/sise/sise_group.naver?type=theme",
                     headers=HDRS, timeout=15)
    r.encoding = "euc-kr"
    pairs, seen = [], set()
    for no, nm in re.findall(
            r'sise_group_detail\.naver\?type=theme&no=(\d+)"[^>]*>([^<]+)</a>', r.text):
        if no not in seen:
            seen.add(no); pairs.append((no, nm.strip()))
    groups = {}
    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        for nm, stocks in ex.map(naver_theme_detail, pairs):
            if stocks:
                groups[nm] = stocks
    return groups


def stock_two_returns(code, start_s, end_s):
    """네이버 siseJson 1회 조회 → (오늘기준 1개월, 2주전기준 1개월) 수익률(%)."""
    try:
        url = (f"https://api.finance.naver.com/siseJson.naver?symbol={code}"
               f"&requestType=1&startTime={start_s}&endTime={end_s}&timeframe=day")
        r = requests.get(url, headers=HDRS, timeout=5)
        rows = json.loads(r.text.replace("'", '"').replace("\n", "").replace("\t", ""))
        cl = [float(x[4]) for x in rows[1:] if x and len(x) > 4 and x[4]]
        cl = [c for c in cl if c > 0]
        if len(cl) < 22 + TWO_WEEK_BARS:
            return None
        now  = (cl[-1] / cl[-22] - 1.0) * 100.0
        prev = (cl[-1 - TWO_WEEK_BARS] / cl[-22 - TWO_WEEK_BARS] - 1.0) * 100.0
        return (now, prev)
    except Exception:
        return None


def compute_theme_surge(groups, date_str, n):
    """오늘/2주전 두 시점 월수익률로 테마 순위 → 순위 급상승 상위 n개."""
    codes = sorted({s["code"] for st in groups.values() for s in st})
    end = pd.Timestamp(date_str)
    start_s = (end - pd.Timedelta(days=70)).strftime("%Y%m%d")
    end_s   = end.strftime("%Y%m%d")

    now_r, prev_r = {}, {}
    def _job(code):
        r = stock_two_returns(code, start_s, end_s)
        if r is not None:
            now_r[code], prev_r[code] = r
    print(f"  · 테마 구성종목 {len(codes)}개 수익률 조회 중...")
    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        list(ex.map(_job, codes))

    theme_now, theme_prev, members = {}, {}, {}
    for nm, st in groups.items():
        rn = [now_r[s["code"]]  for s in st if s["code"] in now_r]
        rp = [prev_r[s["code"]] for s in st if s["code"] in prev_r]
        if len(rn) < THEME_MIN_MEMB or len(rp) < THEME_MIN_MEMB:
            continue
        theme_now[nm]  = sum(rn) / len(rn)
        theme_prev[nm] = sum(rp) / len(rp)
        members[nm]    = st
    rank_now  = {nm: i + 1 for i, nm in enumerate(sorted(theme_now,  key=lambda k: -theme_now[k]))}
    rank_prev = {nm: i + 1 for i, nm in enumerate(sorted(theme_prev, key=lambda k: -theme_prev[k]))}
    rows = []
    for nm in theme_now:
        if nm not in rank_prev:
            continue
        change = rank_prev[nm] - rank_now[nm]   # 양수 = 순위 상승
        rows.append({"테마": nm, "순위변동": change, "현재순위": rank_now[nm],
                     "현재월수익률": round(theme_now[nm], 2), "종목": members[nm]})
    rows.sort(key=lambda x: (-x["순위변동"], x["현재순위"]))
    return rows[:n]


def fetch_ohlcv(code, start, end):
    """FinanceDataReader 일봉 (YYYYMMDD)."""
    try:
        s = f"{start[:4]}-{start[4:6]}-{start[6:]}"
        e = f"{end[:4]}-{end[4:6]}-{end[6:]}"
        df = fdr.DataReader(code, s, e)
        if df is None or df.empty:
            return None
        return df
    except Exception:
        return None


def calc_atr14(df, period=14):
    hi = df["High"].astype(float).values
    lo = df["Low"].astype(float).values
    cl = df["Close"].astype(float).values
    n = len(cl)
    if n < period + 1:
        return None
    tr = np.zeros(n)
    for i in range(1, n):
        tr[i] = max(hi[i] - lo[i], abs(hi[i] - cl[i - 1]), abs(lo[i] - cl[i - 1]))
    atr = tr[1:period + 1].mean()
    for i in range(period + 1, n):
        atr = (atr * (period - 1) + tr[i]) / period
    return float(atr) if atr > 0 else None


def bb_breakout_0bar(code, date_str):
    """볼린저밴드(20,2) 상단을 '오늘' 첫 상향돌파(0봉전)했으면 True."""
    end   = pd.Timestamp(date_str)
    start = (end - pd.Timedelta(days=BB_LOOKBACK)).strftime("%Y%m%d")
    df = fetch_ohlcv(code, start, end.strftime("%Y%m%d"))
    need = BB_LEN + 3
    if df is None or len(df) < need:
        return None
    df = df[df["Close"] > 0].dropna(subset=["Open", "High", "Low", "Close", "Volume"])
    if len(df) < need:
        return None
    close = df["Close"].astype(float)
    sma   = close.rolling(BB_LEN).mean()
    std   = close.rolling(BB_LEN).std(ddof=0)
    upper = (sma + BB_K * std).values
    c     = close.values
    n     = len(c)
    # 0봉전(오늘) 크로스오버: 어제 종가 ≤ 어제 상단, 오늘 종가 > 오늘 상단
    i = n - 1
    if i < 1 or np.isnan(upper[i]) or np.isnan(upper[i - 1]):
        return None
    if c[i - 1] <= upper[i - 1] and c[i] > upper[i]:
        return df
    return None


# ══════════════════════════════════════════════════════════════════════════════
# 메인 스캔
# ══════════════════════════════════════════════════════════════════════════════
def run_scan(date_str=TARGET_DATE):
    print("=" * 70)
    print(" 조건64 — 한투 신호봇 스캐너 (실주문 없음 · 신호 계산 전용)")
    print(f" 기준일: {date_str}")
    print("=" * 70)

    print("[1/3] 네이버 테마분류 로드...")
    groups = load_theme_map()
    print(f"        테마 {len(groups)}개")

    print("[2/3] 급등테마 선정 (오늘 vs 2주전 순위변동 상위 10)...")
    top = compute_theme_surge(groups, date_str, TOP_N)
    if not top:
        print("급등테마를 선정하지 못했습니다.")
        return pd.DataFrame()
    print("        급등테마 TOP:")
    for t in top:
        print(f"          ▲{t['순위변동']:>3}  {t['테마']}  (현재순위 {t['현재순위']}, 월수익률 {t['현재월수익률']}%)")

    # 급등테마 구성종목 → 테마 역인덱스
    theme_of, tname_of = {}, {}
    for t in top:
        for s in t["종목"]:
            theme_of.setdefault(s["code"], []).append(t["테마"])
            tname_of[s["code"]] = s["name"]
    codes = list(theme_of.keys())

    print(f"[3/3] 급등테마 구성종목 {len(codes)}개 · 0봉전 BB({BB_LEN},{BB_K}) 돌파 탐지...")
    brk = {}
    def _job(code):
        df = bb_breakout_0bar(code, date_str)
        if df is not None:
            brk[code] = df
    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        list(ex.map(_job, codes))
    if not brk:
        print("\n0봉전 볼린저밴드 돌파 종목이 없습니다.")
        return pd.DataFrame()

    # 돌파 후보에 대해서만 시총·시장 조회 (네이버 종목별 API)
    print(f"        돌파 후보 {len(brk)}종목 · 시총/시장 조회...")
    cap_of, mkt_of = {}, {}
    def _meta(code):
        cap_of[code], mkt_of[code] = naver_stock_meta(code)
    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        list(ex.map(_meta, list(brk.keys())))

    # 테마별 0봉전 돌파 종목수 → 클러스터/진입대상
    theme_hits = {}
    for code in brk:
        for th in theme_of[code]:
            theme_hits.setdefault(th, []).append(code)
    cluster_codes = {c for th, cs in theme_hits.items() if len(cs) >= CLUSTER_MIN for c in cs}
    entry_code = max(cluster_codes, key=lambda c: cap_of.get(c, 0)) if cluster_codes else None

    rows = []
    for code, df in brk.items():
        atr = calc_atr14(df)
        if atr is None:
            continue
        entry = float(df["Close"].astype(float).iloc[-1])
        shares = int(BUY_KRW // entry)
        if shares < 1:
            continue
        ths = theme_of[code]
        cl_max = max(len(theme_hits.get(th, [])) for th in ths)
        rows.append({
            "구분":     "★진입" if code == entry_code else "후보",
            "종목코드":  code,
            "종목명":    tname_of.get(code, code),
            "시장":      mkt_of.get(code, ""),
            "시총(억)":  cap_of.get(code, 0),
            "테마":      " · ".join(ths),
            "클러스터":  cl_max,
            "진입가":    int(round(entry)),
            "ATR14":     int(round(atr)),
            "손절가":    int(round(entry - ATR_STOP * atr)),
            "익절가":    int(round(entry + ATR_TARGET * atr)),
            "수량":      shares,
            "매수금액":  shares * int(round(entry)),
        })
    if not rows:
        print("\n조건에 부합하는 종목이 없습니다.")
        return pd.DataFrame()

    df_res = pd.DataFrame(rows).sort_values(
        by=["구분", "클러스터", "시총(억)"], ascending=[True, False, False]).reset_index(drop=True)

    print("\n" + "=" * 70)
    n_pick = (df_res["구분"] == "★진입").sum()
    print(f"★ 0봉전 돌파 {len(df_res)}종목 · ★진입대상 {n_pick}건 "
          f"(같은 테마 {CLUSTER_MIN}종목↑ 클러스터 중 시총최대)")
    print("=" * 70)
    pd.set_option("display.unicode.east_asian_width", True)
    pd.set_option("display.max_rows", None)
    pd.set_option("display.width", 200)
    print(df_res.to_string(index=False))

    fname = f"condition64_hantoo_{date_str}.csv"
    df_res.to_csv(fname, index=False, encoding="utf-8-sig")
    print(f"\nCSV 저장: {fname}")
    print("⚠️ 실주문 없음 — 위 신호를 참고해 매매는 직접 하세요.")
    return df_res


if __name__ == "__main__":
    run_scan()
