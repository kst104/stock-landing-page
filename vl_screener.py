"""
VL (Virtual Line) 스크리너
VL = A + (A - A1), A = linreg(C, 50), A1 = linreg(A, 50)

필터 조건:
  1. 시총 1500억 이상
  2. EMA(200) 상승 추세 (전일 EMA200 < 당일 EMA200)
  3. 종가 > EMA(60)
  4. 종가가 VL 대비 -5% / -10% / -15% 이하 하락 (VL > C)

데이터 소스: FinanceDataReader
실행: python vl_screener.py [YYYYMMDD]
"""

import sys
import warnings
from datetime import datetime, timedelta

import numpy as np
import pandas as pd
import FinanceDataReader as fdr

warnings.filterwarnings("ignore")

# ── 상수 ─────────────────────────────────────────────────────────────────────
MIN_MARKET_CAP = 150_000_000_000   # 1500억
LINREG_PERIOD  = 50
EMA_LONG       = 200
EMA_MID        = 60
LOOKBACK_DAYS  = 800               # linreg×2 + EMA200 확보에 필요한 역일 수
DROP_ZONES     = [-5, -10, -15]    # VL 대비 하락률 기준(%)


# ── 지표 계산 ─────────────────────────────────────────────────────────────────

def linreg(series: pd.Series, period: int) -> pd.Series:
    """각 바의 마지막 점에서 linear regression 예측값 (TradingView linreg 동일)"""
    n      = period
    x      = np.arange(n, dtype=float)
    x_mean = x.mean()
    x_var  = np.sum((x - x_mean) ** 2)
    values = series.values.astype(float)
    out    = np.full(len(values), np.nan)

    for i in range(n - 1, len(values)):
        y = values[i - n + 1 : i + 1]
        if np.any(np.isnan(y)):
            continue
        y_mean    = y.mean()
        slope     = np.sum((x - x_mean) * (y - y_mean)) / x_var
        intercept = y_mean - slope * x_mean
        out[i]    = slope * (n - 1) + intercept

    return pd.Series(out, index=series.index)


def ema(series: pd.Series, period: int) -> pd.Series:
    return series.ewm(span=period, adjust=False).mean()


def calc_vl(close: pd.Series) -> pd.Series:
    A  = linreg(close, LINREG_PERIOD)
    A1 = linreg(A,     LINREG_PERIOD)
    return A + (A - A1)


# ── 개별 종목 스크리닝 ────────────────────────────────────────────────────────

def screen_ticker(code: str, start: str, end: str) -> dict | None:
    try:
        df = fdr.DataReader(code, start, end)
        if df is None or len(df) < EMA_LONG + LINREG_PERIOD * 2 + 5:
            return None

        close = df["Close"].astype(float)

        ema60   = ema(close, EMA_MID)
        ema200  = ema(close, EMA_LONG)
        vl      = calc_vl(close)

        c       = close.iloc[-1]
        e60     = ema60.iloc[-1]
        e200    = ema200.iloc[-1]
        e200_1  = ema200.iloc[-2]   # 전일 EMA200
        v       = vl.iloc[-1]

        if pd.isna(v):
            return None

        # 필터 2: EMA200 상승 추세
        if e200_1 >= e200:
            return None

        # 필터 3: 종가 > EMA60
        if c <= e60:
            return None

        # 필터 4: VL > 종가 AND 하락률 ≤ -5%
        if v <= c:
            return None
        drop = (c - v) / v * 100   # 음수
        if drop > DROP_ZONES[0]:
            return None

        zone = (
            f"{DROP_ZONES[0]}%~{DROP_ZONES[1]}%"  if drop > DROP_ZONES[1] else
            f"{DROP_ZONES[1]}%~{DROP_ZONES[2]}%"  if drop > DROP_ZONES[2] else
            f"{DROP_ZONES[2]}% 이하"
        )

        return {
            "종목코드":   code,
            "종가":      int(c),
            "VL":        round(v, 2),
            "VL대비(%)": round(drop, 2),
            "EMA60":     round(e60, 2),
            "EMA200":    round(e200, 2),
            "구간":      zone,
        }
    except Exception:
        return None


# ── 메인 스크리너 ─────────────────────────────────────────────────────────────

def run(date_str: str | None = None):
    if date_str is None:
        date_str = datetime.now().strftime("%Y%m%d")

    start = (
        datetime.strptime(date_str, "%Y%m%d") - timedelta(days=LOOKBACK_DAYS)
    ).strftime("%Y%m%d")

    print(f"\n{'='*60}")
    print(f"  VL 스크리너  |  기준일: {date_str}")
    print(f"{'='*60}")

    # 전체 종목 리스트 + 시총
    print("\n종목 리스트 조회 중...")
    listing = fdr.StockListing("KRX")

    # 필터 1: 시총 1500억 이상
    valid = listing[listing["Marcap"] >= MIN_MARKET_CAP].copy()
    print(f"시총 1500억 이상: {len(valid)} / {len(listing)} 종목")

    all_rows = []
    total = len(valid)

    for i, (_, row) in enumerate(valid.iterrows(), 1):
        code   = str(row["Code"]).zfill(6)
        name   = row["Name"]
        market = row["Market"]
        marcap = int(row["Marcap"])

        if i % 50 == 0 or i == total:
            print(f"  분석 중... {i}/{total}", end="\r")

        result = screen_ticker(code, start, date_str)
        if result:
            result["종목명"]   = name
            result["시총(억)"] = marcap // 100_000_000
            result["시장"]     = market
            all_rows.append(result)

    print(f"\n분석 완료 → {len(all_rows)}개 조건 충족")

    if not all_rows:
        print("\n조건에 맞는 종목이 없습니다.")
        return pd.DataFrame()

    result_df = (
        pd.DataFrame(all_rows)
        [["시장", "종목코드", "종목명", "시총(억)", "종가", "VL",
          "VL대비(%)", "EMA60", "EMA200", "구간"]]
        .sort_values("VL대비(%)")
        .reset_index(drop=True)
    )

    # ── 결과 출력 ──
    print(f"\n{'─'*60}")
    for zone in [
        f"{DROP_ZONES[0]}%~{DROP_ZONES[1]}%",
        f"{DROP_ZONES[1]}%~{DROP_ZONES[2]}%",
        f"{DROP_ZONES[2]}% 이하",
    ]:
        sub = result_df[result_df["구간"] == zone]
        if sub.empty:
            continue
        print(f"\n▶ {zone}  ({len(sub)}개)")
        print(
            sub[["시장", "종목코드", "종목명", "시총(억)", "종가",
                 "VL", "VL대비(%)"]].to_string(index=False)
        )

    return result_df


# ── 엔트리 포인트 ─────────────────────────────────────────────────────────────

if __name__ == "__main__":
    date_arg = sys.argv[1] if len(sys.argv) > 1 else None
    df = run(date_arg)

    if not df.empty:
        ts  = datetime.now().strftime("%Y%m%d_%H%M")
        out = f"vl_result_{ts}.csv"
        df.to_csv(out, index=False, encoding="utf-8-sig")
        print(f"\n결과 저장: {out}")
