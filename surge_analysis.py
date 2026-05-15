"""
최근 1달간 50% 이상 상승 종목 분석
- 상승률을 5%씩 구간으로 나눠서 각 구간별 도달 소요일수 분석
- 가장 빠르게 상승하는 구간 & 가장 많이 꺾이는 구간 파악
- 시총 3000억 이상 종목 대상
"""

import warnings
warnings.filterwarnings("ignore")

from datetime import datetime, timedelta
from pykrx import stock
import pandas as pd
from tabulate import tabulate
import time

# ── 설정 ──────────────────────────────────────────────────────────────────────
TODAY = datetime.today()
START_DATE = (TODAY - timedelta(days=45)).strftime("%Y%m%d")  # 여유있게 45일
END_DATE = TODAY.strftime("%Y%m%d")
MIN_MARKET_CAP = 3000  # 억원 단위 (3000억)
MIN_SURGE_PCT = 50     # 최소 상승률 %
ZONE_SIZE = 5          # 구간 크기 %


def get_all_tickers():
    """코스피+코스닥 전 종목 티커 가져오기"""
    kospi = stock.get_market_ticker_list(END_DATE, market="KOSPI")
    kosdaq = stock.get_market_ticker_list(END_DATE, market="KOSDAQ")
    return kospi + kosdaq


def filter_by_market_cap(tickers):
    """시총 3000억 이상 필터"""
    print(f"시총 {MIN_MARKET_CAP}억 이상 종목 필터링 중...")

    # 코스피 시총
    try:
        cap_kospi = stock.get_market_cap(END_DATE, market="KOSPI")
    except Exception:
        cap_kospi = pd.DataFrame()

    # 코스닥 시총
    try:
        cap_kosdaq = stock.get_market_cap(END_DATE, market="KOSDAQ")
    except Exception:
        cap_kosdaq = pd.DataFrame()

    cap_all = pd.concat([cap_kospi, cap_kosdaq])

    if cap_all.empty:
        print("시총 데이터를 가져올 수 없습니다.")
        return tickers

    # 시가총액은 원 단위 -> 억 단위로 변환
    cap_all["시가총액_억"] = cap_all["시가총액"] / 1e8
    filtered = cap_all[cap_all["시가총액_억"] >= MIN_MARKET_CAP].index.tolist()

    result = [t for t in tickers if t in filtered]
    print(f"  전체 {len(tickers)}개 → 시총 필터 후 {len(result)}개")
    return result


def analyze_ticker(ticker):
    """
    개별 종목의 1달간 OHLCV를 가져와서
    최저점 대비 최고점 상승률이 50% 이상인지 확인하고,
    5%씩 구간별 도달 소요일수를 계산
    """
    try:
        df = stock.get_market_ohlcv(START_DATE, END_DATE, ticker)
    except Exception:
        return None

    if df.empty or len(df) < 5:
        return None

    # 최근 약 1달(22거래일) 데이터만 사용
    df = df.tail(22)

    if df.empty:
        return None

    closes = df["종가"].values
    dates = df.index.tolist()

    # 저점 찾기: 기간 내 최저 종가
    min_idx = 0
    min_price = closes[0]
    for i, p in enumerate(closes):
        if p <= min_price:
            min_price = p
            min_idx = i

    # 저점 이후 최고점 찾기
    if min_idx >= len(closes) - 1:
        return None

    max_idx = min_idx
    max_price = closes[min_idx]
    for i in range(min_idx, len(closes)):
        if closes[i] >= max_price:
            max_price = closes[i]
            max_idx = i

    # 상승률 계산
    if min_price <= 0:
        return None
    surge_pct = (max_price - min_price) / min_price * 100

    if surge_pct < MIN_SURGE_PCT:
        return None

    # 종목명
    try:
        name = stock.get_market_ticker_name(ticker)
    except Exception:
        name = ticker

    # 5%씩 구간별 도달 소요일수 계산
    zones = []
    num_zones = int(surge_pct // ZONE_SIZE) + 1

    for z in range(num_zones):
        target_pct = (z + 1) * ZONE_SIZE
        target_price = min_price * (1 + target_pct / 100)

        if target_price > max_price * 1.001:  # 최고가 초과 구간은 제외
            break

        # 해당 가격에 처음 도달한 날 찾기
        reached_day = None
        for i in range(min_idx, len(closes)):
            if closes[i] >= target_price:
                reached_day = i - min_idx  # 저점으로부터 소요일
                break

        if reached_day is not None:
            zones.append({
                "구간": f"{(z)*ZONE_SIZE}%→{target_pct}%",
                "목표가": int(target_price),
                "도달일수": reached_day,
            })

    # 각 구간별 소요일수 (이전 구간 도달일 빼기)
    for i in range(len(zones) - 1, 0, -1):
        zones[i]["구간소요일"] = zones[i]["도달일수"] - zones[i-1]["도달일수"]
    if zones:
        zones[0]["구간소요일"] = zones[0]["도달일수"]

    # 꺾임 분석: 최고점 이후 하락폭
    pullback_pct = 0
    if max_idx < len(closes) - 1:
        after_max_min = min(closes[max_idx:])
        pullback_pct = (max_price - after_max_min) / max_price * 100

    # 꺾이는 구간: 최고점이 어느 5% 구간에 있는지
    peak_zone_idx = int(surge_pct // ZONE_SIZE)

    return {
        "티커": ticker,
        "종목명": name,
        "저점일": dates[min_idx].strftime("%m/%d") if hasattr(dates[min_idx], "strftime") else str(dates[min_idx]),
        "고점일": dates[max_idx].strftime("%m/%d") if hasattr(dates[max_idx], "strftime") else str(dates[max_idx]),
        "저점가": int(min_price),
        "고점가": int(max_price),
        "상승률": round(surge_pct, 1),
        "상승소요일": max_idx - min_idx,
        "고점후하락률": round(pullback_pct, 1),
        "구간데이터": zones,
        "꺾임구간": f"{peak_zone_idx * ZONE_SIZE}%~{(peak_zone_idx+1) * ZONE_SIZE}%",
    }


def main():
    print("=" * 70)
    print("  최근 1달간 50%+ 상승 종목 구간별 속도 분석")
    print("=" * 70)
    print()

    # 1) 전 종목 가져오기
    all_tickers = get_all_tickers()
    print(f"전체 종목 수: {len(all_tickers)}")

    # 2) 시총 필터
    tickers = filter_by_market_cap(all_tickers)

    # 3) 개별 분석
    print(f"\n{len(tickers)}개 종목 상승률 분석 중... (약 2-5분 소요)")
    results = []
    for i, ticker in enumerate(tickers):
        if (i + 1) % 50 == 0:
            print(f"  진행: {i+1}/{len(tickers)}")

        result = analyze_ticker(ticker)
        if result:
            results.append(result)

        time.sleep(0.05)  # API 부하 방지

    if not results:
        print("\n조건에 맞는 종목이 없습니다.")
        return

    # ── 결과 출력 ──────────────────────────────────────────────────────────────
    results.sort(key=lambda x: x["상승률"], reverse=True)

    print(f"\n{'=' * 70}")
    print(f"  50% 이상 상승 종목: {len(results)}개 발견")
    print(f"{'=' * 70}\n")

    # 종목별 요약
    summary_rows = []
    for r in results:
        summary_rows.append([
            r["종목명"], r["티커"],
            f"{r['저점가']:,}", f"{r['고점가']:,}",
            f"{r['상승률']}%",
            f"{r['상승소요일']}일",
            r["저점일"], r["고점일"],
            f"-{r['고점후하락률']}%",
        ])

    print(tabulate(
        summary_rows,
        headers=["종목명", "티커", "저점가", "고점가", "상승률", "소요일",
                 "저점일", "고점일", "고점후하락"],
        tablefmt="rounded_grid",
    ))

    # ── 구간별 통합 분석 ────────────────────────────────────────────────────────
    print(f"\n{'=' * 70}")
    print("  구간별 평균 소요일수 (전체 종목 통합)")
    print(f"{'=' * 70}\n")

    # 모든 종목의 구간 데이터를 통합
    zone_stats = {}  # key: 구간명, value: list of 구간소요일

    for r in results:
        for z in r["구간데이터"]:
            zone_name = z["구간"]
            if zone_name not in zone_stats:
                zone_stats[zone_name] = []
            zone_stats[zone_name].append(z["구간소요일"])

    zone_rows = []
    fastest_zone = None
    fastest_speed = float("inf")

    for zone_name in sorted(zone_stats.keys(), key=lambda x: int(x.split("%")[0])):
        days_list = zone_stats[zone_name]
        avg_days = sum(days_list) / len(days_list)
        count = len(days_list)
        min_days = min(days_list)
        max_days = max(days_list)

        # 속도 = 5% / 평균소요일 (일당 상승률)
        speed = ZONE_SIZE / avg_days if avg_days > 0 else float("inf")

        bar = "█" * int(speed * 3) if speed != float("inf") else "█" * 20

        zone_rows.append([
            zone_name, count, f"{avg_days:.1f}일",
            f"{min_days}일", f"{max_days}일",
            f"{speed:.1f}%/일", bar
        ])

        if avg_days < fastest_speed and count >= 2:
            fastest_speed = avg_days
            fastest_zone = zone_name

    print(tabulate(
        zone_rows,
        headers=["구간", "종목수", "평균소요", "최단", "최장", "일당상승", "속도바"],
        tablefmt="rounded_grid",
    ))

    if fastest_zone:
        print(f"\n  ⚡ 가장 빠른 구간: {fastest_zone} (평균 {fastest_speed:.1f}일)")

    # ── 꺾임 구간 분석 ──────────────────────────────────────────────────────────
    print(f"\n{'=' * 70}")
    print("  꺾임(고점) 구간 분포")
    print(f"{'=' * 70}\n")

    peak_zones = {}
    for r in results:
        z = r["꺾임구간"]
        if z not in peak_zones:
            peak_zones[z] = {"종목": [], "하락률": []}
        peak_zones[z]["종목"].append(r["종목명"])
        peak_zones[z]["하락률"].append(r["고점후하락률"])

    peak_rows = []
    for zone_name in sorted(peak_zones.keys(), key=lambda x: int(x.split("%")[0])):
        info = peak_zones[zone_name]
        count = len(info["종목"])
        avg_drop = sum(info["하락률"]) / count
        names = ", ".join(info["종목"][:5])
        if len(info["종목"]) > 5:
            names += f" 외 {len(info['종목'])-5}개"
        bar = "▓" * count
        peak_rows.append([zone_name, count, bar, f"-{avg_drop:.1f}%", names])

    print(tabulate(
        peak_rows,
        headers=["꺾임구간", "종목수", "분포", "평균하락", "종목"],
        tablefmt="rounded_grid",
    ))

    most_peak_zone = max(peak_zones.items(), key=lambda x: len(x[1]["종목"]))
    print(f"\n  📉 가장 많이 꺾이는 구간: {most_peak_zone[0]} ({len(most_peak_zone[1]['종목'])}개 종목)")

    # ── 종목별 상세 구간 ────────────────────────────────────────────────────────
    print(f"\n{'=' * 70}")
    print("  종목별 상세 구간 분석")
    print(f"{'=' * 70}")

    for r in results:
        print(f"\n  ▸ {r['종목명']} ({r['티커']}) | {r['상승률']}% 상승 | {r['상승소요일']}일")
        if r["구간데이터"]:
            detail_rows = []
            for z in r["구간데이터"]:
                speed = ZONE_SIZE / z["구간소요일"] if z["구간소요일"] > 0 else float("inf")
                speed_str = f"{speed:.1f}%/일" if speed != float("inf") else "즉시"
                accel = "🔥" if (z["구간소요일"] == 0 or (z["구간소요일"] <= 1 and speed >= 5)) else ""
                detail_rows.append([
                    z["구간"], f"{z['구간소요일']}일",
                    f"누적 {z['도달일수']}일", speed_str, accel
                ])
            print(tabulate(detail_rows, headers=["구간", "소요일", "누적", "속도", ""], tablefmt="simple"))

        if r["고점후하락률"] > 0:
            print(f"    → 고점 후 하락: -{r['고점후하락률']}% (꺾임구간: {r['꺾임구간']})")

    print(f"\n{'=' * 70}")
    print("  분석 완료")
    print(f"{'=' * 70}")


if __name__ == "__main__":
    main()
