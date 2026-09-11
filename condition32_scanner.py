"""
조건32 — 캔들볼륨 저항선 돌파 스캐너 (standalone)
====================================================
로직 (주식랜딩페이지 app.py 의 조건32와 동일):
  · 시가총액 1,500억원 이상 · ETF/ETN 제외 (KOSPI+KOSDAQ 일반주)
  · 최근 90봉 중 음봉(종가<시가)만 선별
  · 각 음봉의 (시가+종가+고가+저가)/4 × 거래량 = 점수
  · 점수 최대인 음봉의 '시가' = 캔들 저항선
  · 금일 종가 > 저항선  AND  전일 종가 ≤ 저항선   (오늘 첫 돌파)
  · ADX(11) > 25   (추세 강도 확인, Wilder 평활)
  · 결과: 저항대비(%) 낮은 순 (막 돌파한 종목 우선)

설치: pip install pykrx pandas numpy
실행: python condition32_scanner.py
"""

import numpy as np
import pandas as pd
from pykrx import stock
from datetime import datetime, timedelta
import warnings
warnings.filterwarnings("ignore")

# ==========================================
# 설정값
# ==========================================
TARGET_DATE   = "20260717"   # 기준일 (휴장이면 자동으로 직전 거래일 탐색)
LOOKBACK_DAYS = 200          # OHLCV 조회 캘린더 일수 (90봉 확보용)
RECENT_BARS   = 90           # 저항선 탐색 구간 (봉)
ADX_PERIOD    = 11           # ADX 기간
ADX_MIN       = 25.0         # ADX 하한
MIN_MKT_CAP   = 1500         # 최소 시가총액 (억원)


# ==========================================
# 유틸 함수
# ==========================================
def find_last_trading_date(date_str: str) -> str:
    """해당일이 휴장이면 직전 거래일 반환"""
    d = datetime.strptime(date_str, "%Y%m%d")
    for _ in range(10):
        try:
            t = stock.get_market_ticker_list(d.strftime("%Y%m%d"), market="KOSPI")
            if t:
                return d.strftime("%Y%m%d")
        except Exception:
            pass
        d -= timedelta(days=1)
    raise ValueError("유효한 거래일을 찾지 못했습니다.")


def calc_adx(high, low, close, period=14):
    """ADX (Wilder 평활). 반환: numpy array (초기 2*period-1 봉까지 NaN)"""
    hi = np.asarray(high, dtype=float)
    lo = np.asarray(low, dtype=float)
    cl = np.asarray(close, dtype=float)
    n = len(hi)

    tr  = np.full(n, np.nan)
    pdm = np.full(n, np.nan)
    ndm = np.full(n, np.nan)
    for i in range(1, n):
        tr[i] = max(hi[i] - lo[i], abs(hi[i] - cl[i-1]), abs(lo[i] - cl[i-1]))
        up = hi[i] - hi[i-1]
        dn = lo[i-1] - lo[i]
        pdm[i] = up if (up > dn and up > 0) else 0.0
        ndm[i] = dn if (dn > up and dn > 0) else 0.0

    s_tr  = np.full(n, np.nan)
    s_pdm = np.full(n, np.nan)
    s_ndm = np.full(n, np.nan)
    if n > period:
        s_tr[period]  = np.nansum(tr[1:period+1])
        s_pdm[period] = np.nansum(pdm[1:period+1])
        s_ndm[period] = np.nansum(ndm[1:period+1])
        for i in range(period+1, n):
            s_tr[i]  = s_tr[i-1]  - s_tr[i-1]  / period + tr[i]
            s_pdm[i] = s_pdm[i-1] - s_pdm[i-1] / period + pdm[i]
            s_ndm[i] = s_ndm[i-1] - s_ndm[i-1] / period + ndm[i]

    with np.errstate(invalid="ignore", divide="ignore"):
        pdi = np.where(s_tr > 0, 100.0 * s_pdm / s_tr, 0.0)
        ndi = np.where(s_tr > 0, 100.0 * s_ndm / s_tr, 0.0)

    di_sum  = pdi + ndi
    di_diff = np.abs(pdi - ndi)
    dx = np.where((di_sum > 0) & ~np.isnan(s_tr), 100.0 * di_diff / di_sum, np.nan)

    adx = np.full(n, np.nan)
    first = 2 * period - 1
    if n > first:
        adx[first] = np.nanmean(dx[period:first+1])
        for i in range(first+1, n):
            adx[i] = (adx[i-1] * (period-1) + dx[i]) / period
    return adx


def check_candle_volume_resistance(df: pd.DataFrame):
    """
    조건32 판정. df: 시가/고가/저가/종가/거래량 컬럼(pykrx 형식).
    반환: None(미충족) 또는 dict.
    """
    if df is None or len(df) < RECENT_BARS + 2:
        return None
    df = df[df["종가"] > 0].dropna(subset=["시가", "고가", "저가", "종가", "거래량"])
    if len(df) < RECENT_BARS + 2:
        return None

    # 최근 90봉 (금일 포함) 중 음봉만
    recent  = df.iloc[-RECENT_BARS:]
    bearish = recent[recent["종가"] < recent["시가"]]
    if bearish.empty:
        return None

    # (O+C+H+L)/4 × 거래량 → 최대 봉의 시가 = 저항선
    score = ((bearish["시가"] + bearish["종가"] +
              bearish["고가"] + bearish["저가"]) / 4.0) * bearish["거래량"].astype(float)
    max_idx     = score.idxmax()
    resistance  = float(bearish.loc[max_idx, "시가"])
    resist_date = str(max_idx)[:10]

    # 오늘 첫 돌파: 금일 종가 > 저항선 AND 전일 종가 ≤ 저항선
    today_close = float(df["종가"].iloc[-1])
    prev_close  = float(df["종가"].iloc[-2])
    if today_close <= resistance:
        return None
    if prev_close > resistance:
        return None

    # ADX(11) > 25
    adx = calc_adx(df["고가"].values, df["저가"].values, df["종가"].values, ADX_PERIOD)
    adx_now = float(adx[-1])
    if np.isnan(adx_now) or adx_now <= ADX_MIN:
        return None

    return {
        "종가":         int(today_close),
        "전일대비(%)":  round((today_close / prev_close - 1) * 100, 2) if prev_close > 0 else 0.0,
        "캔들저항선":   int(resistance),
        "저항대비(%)":  round((today_close / resistance - 1) * 100, 2),
        "저항봉날짜":   resist_date,
        "ADX(11)":      round(adx_now, 1),
    }


# ==========================================
# 메인 스캔
# ==========================================
def run_scan():
    print("=" * 66)
    print(" 조건32 — 캔들볼륨 저항선 돌파 스캐너")
    print("=" * 66)

    trading_date = find_last_trading_date(TARGET_DATE)
    start_date   = (datetime.strptime(trading_date, "%Y%m%d")
                    - timedelta(days=LOOKBACK_DAYS)).strftime("%Y%m%d")
    print(f"기준 거래일 : {trading_date[:4]}.{trading_date[4:6]}.{trading_date[6:]}")
    print(f"조건        : 시총≥{MIN_MKT_CAP}억 | 90봉 음봉 저항 돌파 | ADX({ADX_PERIOD})>{ADX_MIN:.0f}\n")

    print("[1/3] 종목·시가총액 조회...")
    frames = []
    for mkt in ("KOSPI", "KOSDAQ"):
        cap = stock.get_market_cap(trading_date, market=mkt)
        cap["시장"] = mkt
        frames.append(cap)
    cap_all = pd.concat(frames)
    cap_all["시가총액_억"] = cap_all["시가총액"] / 1e8
    target = cap_all[cap_all["시가총액_억"] >= MIN_MKT_CAP]
    print(f"        {MIN_MKT_CAP}억 이상: {len(target)}종목 (ETF/ETN 제외 — 일반주만)")

    print(f"[2/3] OHLCV 스캔 ({len(target)}종목)...")
    results = []
    tickers = target.index.tolist()
    for i, code in enumerate(tickers, 1):
        if i % 100 == 0:
            print(f"        {i}/{len(tickers)} ({i/len(tickers)*100:.0f}%)")
        try:
            df = stock.get_market_ohlcv(start_date, trading_date, code)
            res = check_candle_volume_resistance(df)
            if res:
                res["종목코드"] = code
                res["종목명"]   = stock.get_market_ticker_name(code)
                res["시장"]     = target.loc[code, "시장"]
                res["시총(억)"] = int(target.loc[code, "시가총액_억"])
                results.append(res)
        except Exception:
            continue

    print("\n[3/3] 결과 정리")
    print("=" * 66)
    if not results:
        print("조건에 부합하는 종목이 없습니다.")
        return pd.DataFrame()

    cols = ["시장", "종목코드", "종목명", "시총(억)", "종가", "전일대비(%)",
            "캔들저항선", "저항대비(%)", "저항봉날짜", f"ADX({ADX_PERIOD})"]
    df_res = (pd.DataFrame(results)[cols]
              .sort_values("저항대비(%)", ascending=True)   # 막 돌파한 종목 우선
              .reset_index(drop=True))

    print(f"\n★ 총 {len(df_res)}종목 (저항대비 낮은 순)\n")
    print(df_res.to_string(index=False))

    fname = f"condition32_result_{trading_date}.csv"
    df_res.to_csv(fname, index=False, encoding="utf-8-sig")
    print(f"\nCSV 저장: {fname}")
    return df_res


if __name__ == "__main__":
    run_scan()
