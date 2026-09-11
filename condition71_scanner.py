# -*- coding: utf-8 -*-
"""
조건71 — 세력평균20돌파 스캐너 (standalone)
====================================================
주식랜딩페이지 app.py 의 조건71과 동일 로직 (일봉).

  세력캔들  : 거래량 > 1.5×MA(V,60) AND 양봉(C>O) 인 봉의 (시가+종가)/2
  세력평단  : 세력캔들 값 (세력캔들이 없는 날은 직전 세력캔들 값 유지 = valuewhen)
  세력20평균: 지수평균(세력평단, 20)

  선정 조건 (시가총액 3,000억↑ · ETF/ETN 제외):
    ① 세력20평균이 '오늘까지' 5일 이상 연속 하락
    ② 종가가 세력20평균을 '오늘' 상향 돌파
       (전일 종가 ≤ 전일 평균  →  금일 종가 > 금일 평균)

설치:  pip install finance-datareader pandas numpy requests beautifulsoup4 lxml
실행:  python condition71_scanner.py
"""

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
# 설정값 (app.py 조건71과 동일)
# ══════════════════════════════════════════════════════════════════════════════
TARGET_DATE   = datetime.now().strftime("%Y%m%d")   # 기준일 (예: "20260904")
MIN_CAP_EOK   = 3000        # 시가총액 하한 (억원) = 3천억
VOL_MULT      = 1.5         # 세력캔들 거래량 배수 (V > 1.5×MA(V,60))
VOL_MA        = 60          # 거래량 이동평균 기간
EMA_LEN       = 20          # 세력20평균 지수평균 기간
DROP_DAYS     = 5           # 세력20평균 최소 연속 하락일수
LOOKBACK_DAYS = 800         # 일봉 조회 캘린더 일수
WORKERS       = 10          # 병렬 워커 수
HDRS          = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}

# ETF/ETN 이름 접두사 (제외용)
_ETF_RE = re.compile(
    r'^(TIGER|KODEX|KBSTAR|HANARO|KOSEF|ARIRANG|ACE|SOL|TIMEFOLIO|SMART|MASTER|'
    r'PLUS|TREX|GIANT|PIONEER|KTOP|KINDEX|파워|KoAct|WON|FOCUS|BOOKOO|LAVIE|'
    r'RISE|히어로즈|마이티|UNICORN|1Q|VITA|KIWOOM|키움)', re.IGNORECASE)


def _safe_int(s):
    try:
        return int(float(str(s).replace(",", "")))
    except Exception:
        return 0


# ══════════════════════════════════════════════════════════════════════════════
# 종목 리스트 (네이버 시가총액 페이지 — FDR StockListing이 불안정하여 자체 스크랩)
# ══════════════════════════════════════════════════════════════════════════════
def _naver_fetch_page(args):
    from bs4 import BeautifulSoup
    sosok, page, mkt = args
    try:
        url = (f"https://finance.naver.com/sise/sise_market_sum.naver"
               f"?sosok={sosok}&page={page}")
        r = requests.get(url, headers=HDRS, timeout=15)
        r.encoding = "euc-kr"
        soup  = BeautifulSoup(r.text, "lxml")
        tbody = soup.find("table", class_="type_2")
        if tbody is None:
            return None
        rows = []
        for tr in tbody.find_all("tr"):
            a = tr.find("a", href=re.compile(r"/item/main\.naver\?code=\d{6}"))
            if not a:
                continue
            m = re.search(r"code=(\d{6})", a["href"])
            if not m:
                continue
            code = m.group(1)
            name = a.get_text(strip=True)
            tds  = tr.find_all("td")
            if not name or len(tds) < 7:
                continue
            marcap_eok = _safe_int(tds[6].get_text(strip=True).replace(",", ""))
            if marcap_eok <= 0:
                continue
            rows.append({"Code": code, "Name": name, "Market": mkt, "Marcap_eok": marcap_eok})
        return rows or None
    except Exception:
        return None


def load_listing():
    """네이버 시가총액 페이지 전체 스크랩 → DataFrame(Code, Name, Market, Marcap_eok)."""
    tasks = []
    for sosok, mkt in ((0, "KOSPI"), (1, "KOSDAQ")):
        for page in range(1, 45):          # 시장별 최대 ~44페이지
            tasks.append((sosok, page, mkt))
    all_rows = []
    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        for res in ex.map(_naver_fetch_page, tasks):
            if res:
                all_rows.extend(res)
    df = pd.DataFrame(all_rows).drop_duplicates(subset=["Code"]).reset_index(drop=True)
    return df


# ══════════════════════════════════════════════════════════════════════════════
# 시세 조회 + 조건71 판정
# ══════════════════════════════════════════════════════════════════════════════
def fetch_ohlcv(code, start, end):
    try:
        s = f"{start[:4]}-{start[4:6]}-{start[6:]}"
        e = f"{end[:4]}-{end[4:6]}-{end[6:]}"
        df = fdr.DataReader(code, s, e)
        return df if (df is not None and not df.empty) else None
    except Exception:
        return None


def check_condition71(code, start, end):
    """조건71 판정 → dict | None."""
    df = fetch_ohlcv(code, start, end)
    if df is None or len(df) < 90:
        return None
    df = df[df["Close"] > 0].dropna(subset=["Open", "High", "Low", "Close", "Volume"])
    if len(df) < 90:
        return None
    c = df["Close"].astype(float)
    o = df["Open"].astype(float)
    v = df["Volume"].astype(float)
    volma = v.rolling(VOL_MA).mean()

    # 세력캔들 = (V > 1.5×MA(V,60)) AND 양봉 → (시가+종가)/2, 아니면 직전값 유지
    mask   = (v > VOL_MULT * volma) & (c > o)
    half   = (c + o) / 2.0
    kandle = half.where(mask).ffill()                      # valuewhen(1, mask, (C+O)/2)
    avg    = kandle.ewm(span=EMA_LEN, adjust=False).mean() # 세력20평균

    if pd.isna(avg.iloc[-1]) or pd.isna(avg.iloc[-2]):
        return None

    # ① 세력20평균이 오늘까지 5일 이상 연속 하락
    drop = 0
    for i in range(len(avg) - 1, 0, -1):
        a0, a1 = avg.iloc[i], avg.iloc[i - 1]
        if pd.notna(a0) and pd.notna(a1) and float(a0) < float(a1):
            drop += 1
        else:
            break
    if drop < DROP_DAYS:
        return None

    # ② 종가가 세력20평균을 오늘 상향돌파
    c0, c1   = float(c.iloc[-1]), float(c.iloc[-2])
    a0v, a1v = float(avg.iloc[-1]), float(avg.iloc[-2])
    if not (c1 <= a1v and c0 > a0v):
        return None

    return {
        "종가":       int(round(c0)),
        "전일대비(%)": round((c0 / c1 - 1) * 100, 2) if c1 > 0 else 0.0,
        "세력평단":   int(round(float(kandle.iloc[-1]))),
        "세력20선":   int(round(a0v)),
        "평균대비(%)": round((c0 / a0v - 1) * 100, 2),
        "하락일수":   drop,
    }


# ══════════════════════════════════════════════════════════════════════════════
# 메인 스캔
# ══════════════════════════════════════════════════════════════════════════════
def run_scan(date_str=TARGET_DATE):
    print("=" * 70)
    print(" 조건71 — 세력평균20돌파 스캐너")
    print(f" 기준일: {date_str}")
    print(f" 조건: 시총≥{MIN_CAP_EOK}억 · 세력20평균 {DROP_DAYS}일↑ 하락 중 종가 상향돌파 · ETF/ETN 제외")
    print("=" * 70)

    print("[1/3] 네이버 시가총액 목록 로드...")
    listing = load_listing()
    print(f"        전체 {len(listing)}종목")

    valid = listing[
        (listing["Marcap_eok"] >= MIN_CAP_EOK) &
        (~listing["Name"].str.match(_ETF_RE, na=False)) &
        (~listing["Name"].str.contains(r"ETN|ETF", case=False, na=False, regex=True))
    ].reset_index(drop=True)
    print(f"        시총 {MIN_CAP_EOK}억↑ · ETF/ETN 제외: {len(valid)}종목")

    start = (datetime.strptime(date_str, "%Y%m%d") - timedelta(days=LOOKBACK_DAYS)).strftime("%Y%m%d")
    cap_of  = dict(zip(valid["Code"], valid["Marcap_eok"]))
    name_of = dict(zip(valid["Code"], valid["Name"]))
    mkt_of  = dict(zip(valid["Code"], valid["Market"]))

    print(f"[2/3] 일봉 스캔 ({len(valid)}종목)...")
    rows = []
    done = [0]
    def _job(code):
        res = check_condition71(code, start, date_str)
        done[0] += 1
        if done[0] % 100 == 0:
            print(f"        {done[0]}/{len(valid)}")
        if res:
            res["종목코드"] = code
            res["종목명"]   = name_of.get(code, code)
            res["시장"]     = mkt_of.get(code, "")
            res["시총(억)"] = cap_of.get(code, 0)
            rows.append(res)
    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        list(ex.map(_job, valid["Code"].tolist()))

    print("[3/3] 결과 정리")
    print("=" * 70)
    if not rows:
        print("조건에 부합하는 종목이 없습니다.")
        return pd.DataFrame()

    cols = ["시장", "종목코드", "종목명", "시총(억)", "종가", "전일대비(%)",
            "세력평단", "세력20선", "평균대비(%)", "하락일수"]
    df_res = (pd.DataFrame(rows)[cols]
              .sort_values(["하락일수", "평균대비(%)"], ascending=[False, False])
              .reset_index(drop=True))

    print(f"\n★ 총 {len(df_res)}종목\n")
    pd.set_option("display.unicode.east_asian_width", True)
    pd.set_option("display.max_rows", None)
    pd.set_option("display.width", 200)
    print(df_res.to_string(index=False))

    fname = f"condition71_result_{date_str}.csv"
    df_res.to_csv(fname, index=False, encoding="utf-8-sig")
    print(f"\nCSV 저장: {fname}")
    return df_res


if __name__ == "__main__":
    run_scan()
