"""
주도주 조정 구간 탐색기
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
조건:
  1) 시총 3,000억 이상
  2) 최근 20거래일 상승률 30% 이상 (강한 주도주)
  3) 최근 5거래일: 5% 미만 상승 & 하루 5%+ 양봉 없음 (조정 상태)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
네이버 금융 API 사용 (pykrx 호환 문제 우회)
"""

import requests
import json
import time
import pandas as pd
from datetime import datetime
from tabulate import tabulate

HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
MIN_MARKET_CAP = 3000  # 억원


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 1) 시총 3000억 이상 종목 수집 (네이버 API)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
def fetch_market_cap_stocks(market, min_cap_억=3000):
    """네이버 시총 순위에서 시총 min_cap_억 이상 종목 가져오기"""
    stocks = []
    page = 1
    while True:
        url = f"https://m.stock.naver.com/api/stocks/marketValue/{market}?page={page}&pageSize=100"
        try:
            r = requests.get(url, headers=HEADERS, timeout=15)
            data = r.json()
        except Exception as e:
            print(f"  [오류] {market} page {page}: {e}")
            break

        items = data.get("stocks", [])
        if not items:
            break

        for item in items:
            code = item.get("itemCode", "")
            name = item.get("stockName", "")
            # 시총은 별도 필드가 없으므로 상세에서 가져와야 함
            # 대신 시총 순위로 정렬되어 있으므로 일정 페이지까지만 수집
            stock_type = item.get("stockEndType", "")
            if stock_type not in ("stock",):  # ETF 등 제외
                continue
            stocks.append({"code": code, "name": name})

        page += 1
        if page > 15:  # 시총 상위 1500개면 충분
            break
        time.sleep(0.1)

    return stocks


def get_market_cap(code):
    """개별 종목 시총 조회 (억원)"""
    url = f"https://m.stock.naver.com/api/stock/{code}/basic"
    try:
        r = requests.get(url, headers=HEADERS, timeout=10)
        data = r.json()
        # marketCapitalization이 원 단위
        cap = data.get("marketCapitalization")
        if cap:
            return int(cap) / 1e8  # 억원
    except Exception:
        pass
    return 0


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 2) 일봉 데이터 가져오기 (네이버 차트 API)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
def fetch_daily_prices(code, count=30):
    """
    네이버 일봉 데이터 (최근 count일)
    반환: list of dict [{date, open, high, low, close, volume}, ...]
    """
    url = f"https://m.stock.naver.com/api/stock/{code}/chart?chartType=day&count={count}&requestType=0"
    try:
        r = requests.get(url, headers=HEADERS, timeout=10)
        raw = r.json()
    except Exception:
        return []

    rows = []
    for item in raw:
        try:
            rows.append({
                "date": item.get("localTradedAt", ""),
                "close": int(str(item.get("closePrice", "0")).replace(",", "")),
                "open": int(str(item.get("openPrice", "0")).replace(",", "")),
                "high": int(str(item.get("highPrice", "0")).replace(",", "")),
                "low": int(str(item.get("lowPrice", "0")).replace(",", "")),
                "volume": int(str(item.get("accumulatedTradingVolume", "0")).replace(",", "")),
            })
        except (ValueError, TypeError):
            continue

    return rows


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 3) 조건 판별
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
def check_conditions(prices):
    """
    prices: 최신 → 과거순 or 과거 → 최신순 일봉 리스트
    반환: (통과여부, 분석결과 dict)
    """
    if len(prices) < 20:
        return False, {}

    # 날짜순 정렬 (과거 → 최신)
    prices = sorted(prices, key=lambda x: x["date"])

    # 최근 20거래일
    recent_20 = prices[-20:]
    # 최근 5거래일
    recent_5 = prices[-5:]

    # ── 조건2: 20거래일 상승률 30% 이상 ──
    start_price = recent_20[0]["close"]
    end_price = recent_20[-1]["close"]
    if start_price <= 0:
        return False, {}

    gain_20d = (end_price - start_price) / start_price * 100

    if gain_20d < 30:
        return False, {}

    # ── 조건3: 최근 5일 조정 상태 ──
    # 3a) 5일간 총 상승률 5% 미만
    start_5d = recent_5[0]["close"]
    end_5d = recent_5[-1]["close"]
    gain_5d = (end_5d - start_5d) / start_5d * 100 if start_5d > 0 else 0

    # 3b) 5일 중 하루라도 5% 이상 양봉이 없어야 함
    max_daily_gain = 0
    daily_gains = []
    for i in range(1, len(recent_5)):
        prev_close = recent_5[i - 1]["close"]
        curr_close = recent_5[i]["close"]
        if prev_close > 0:
            dg = (curr_close - prev_close) / prev_close * 100
            daily_gains.append(dg)
            if dg > max_daily_gain:
                max_daily_gain = dg

    # 첫날도 포함 (직전 거래일 대비)
    if len(prices) >= 21:
        day_before = prices[-6]["close"]
        first_5d = recent_5[0]["close"]
        if day_before > 0:
            dg0 = (first_5d - day_before) / day_before * 100
            daily_gains.insert(0, dg0)
            if dg0 > max_daily_gain:
                max_daily_gain = dg0

    is_consolidating = gain_5d < 5 and max_daily_gain < 5

    if not is_consolidating:
        return False, {}

    # ── 20일간 최고가 / 현재가 대비 ──
    high_20d = max(p["high"] for p in recent_20)
    drop_from_high = (high_20d - end_price) / high_20d * 100 if high_20d > 0 else 0

    # ── 거래량 분석 ──
    avg_vol_20 = sum(p["volume"] for p in recent_20) / 20
    avg_vol_5 = sum(p["volume"] for p in recent_5) / 5
    vol_ratio = avg_vol_5 / avg_vol_20 if avg_vol_20 > 0 else 0

    return True, {
        "20일상승률": round(gain_20d, 1),
        "5일상승률": round(gain_5d, 1),
        "5일최대일봉": round(max_daily_gain, 1),
        "일봉변동": [round(d, 1) for d in daily_gains],
        "20일고가": high_20d,
        "고점대비": round(drop_from_high, 1),
        "5일평균거래량": int(avg_vol_5),
        "20일평균거래량": int(avg_vol_20),
        "거래량비율": round(vol_ratio, 2),
        "시작가_20d": start_price,
        "현재가": end_price,
    }


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 메인
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
def main():
    print()
    print("=" * 72)
    print("  주도주 조정 구간 탐색기")
    print("  시총 3000억+ | 20일 상승률 30%+ | 최근 5일 조정 중")
    print("=" * 72)

    # 1) 코스피 + 코스닥 시총 상위 종목 수집
    print("\n[1/3] 종목 리스트 수집 중...")
    kospi_stocks = fetch_market_cap_stocks("KOSPI")
    kosdaq_stocks = fetch_market_cap_stocks("KOSDAQ")
    all_stocks = kospi_stocks + kosdaq_stocks
    print(f"  수집 완료: 코스피 {len(kospi_stocks)}개 + 코스닥 {len(kosdaq_stocks)}개 = {len(all_stocks)}개")

    # 2) 시총 필터 + 상승률/조정 분석
    print(f"\n[2/3] 시총 {MIN_MARKET_CAP}억+ 필터 & 가격 분석 중...")
    print("  (종목당 시총조회+일봉조회 → 약 3~5분 소요)")

    results = []
    checked = 0
    cap_passed = 0

    for i, stk in enumerate(all_stocks):
        code = stk["code"]
        name = stk["name"]

        if (i + 1) % 100 == 0:
            print(f"  진행: {i+1}/{len(all_stocks)} (시총통과: {cap_passed}, 조건충족: {len(results)})")

        # 시총 확인
        cap = get_market_cap(code)
        if cap < MIN_MARKET_CAP:
            # 시총 순위로 정렬되어 있어서, 일정 수 연속 미달이면 중단 가능
            # 하지만 안전하게 계속 진행
            continue

        cap_passed += 1

        # 일봉 가져오기
        prices = fetch_daily_prices(code, count=30)
        if not prices:
            continue

        passed, info = check_conditions(prices)
        if passed:
            results.append({
                "종목명": name,
                "코드": code,
                "시총(억)": int(cap),
                **info,
            })

        checked += 1
        time.sleep(0.05)

    # ── 결과 출력 ──────────────────────────────────────────────────────────────
    print(f"\n  분석 완료: 시총통과 {cap_passed}개 중 {len(results)}개 조건 충족")

    if not results:
        print("\n  조건에 맞는 종목이 없습니다.")
        print("  (20일 30%+ 상승 후 5일간 조정 중인 종목이 현재 없음)")
        print()

        # 조건 완화 결과도 참고로 출력
        print("  [참고] 20일 상승률 상위 종목 (조정 여부 무관):")
        # 다시 스캔하지 않고, 이미 수집한 시총통과 종목의 상승률만 표시
        return

    results.sort(key=lambda x: x["20일상승률"], reverse=True)

    print(f"\n{'=' * 72}")
    print(f"  조건 충족 종목: {len(results)}개")
    print(f"{'=' * 72}\n")

    # 요약 테이블
    summary = []
    for r in results:
        vol_status = ""
        vr = r["거래량비율"]
        if vr < 0.5:
            vol_status = "극감"
        elif vr < 0.8:
            vol_status = "감소"
        elif vr < 1.2:
            vol_status = "보합"
        else:
            vol_status = "증가"

        summary.append([
            r["종목명"],
            r["코드"],
            f"{r['시총(억)']:,}억",
            f"+{r['20일상승률']}%",
            f"{'+' if r['5일상승률']>=0 else ''}{r['5일상승률']}%",
            f"{r['5일최대일봉']}%",
            f"-{r['고점대비']}%",
            f"{r['거래량비율']}x ({vol_status})",
            f"{r['현재가']:,}",
        ])

    print(tabulate(
        summary,
        headers=["종목명", "코드", "시총", "20일상승", "5일변동",
                 "5일최대일봉", "고점대비", "거래량(5일/20일)", "현재가"],
        tablefmt="rounded_grid",
    ))

    # 상세 정보
    print(f"\n{'=' * 72}")
    print("  종목별 상세")
    print(f"{'=' * 72}")

    for r in results:
        print(f"\n  {'─' * 50}")
        print(f"  {r['종목명']} ({r['코드']}) | 시총 {r['시총(억)']:,}억")
        print(f"  {'─' * 50}")
        print(f"  20일 전 종가: {r['시작가_20d']:,}원 → 현재: {r['현재가']:,}원 (+{r['20일상승률']}%)")
        print(f"  20일 내 최고가: {r['20일고가']:,}원 (현재 대비 -{r['고점대비']}%)")
        print(f"  최근 5일 상승률: {'+' if r['5일상승률']>=0 else ''}{r['5일상승률']}%")
        print(f"  최근 5일 일봉 변동: {r['일봉변동']}")
        print(f"  거래량: 5일평균 {r['5일평균거래량']:,} / 20일평균 {r['20일평균거래량']:,} ({r['거래량비율']}x)")

        # 진단
        diag = []
        if r["고점대비"] <= 3:
            diag.append("고점 부근 횡보 (브레이크아웃 임박 가능)")
        elif r["고점대비"] <= 10:
            diag.append("건전한 조정 (눌림목)")
        else:
            diag.append("깊은 조정 중")

        if r["거래량비율"] < 0.6:
            diag.append("거래량 급감 → 매도 피로 신호")
        elif r["거래량비율"] < 0.8:
            diag.append("거래량 감소 → 조정 진행 중")

        for d in diag:
            print(f"  >> {d}")

    print(f"\n{'=' * 72}")
    print("  분석 완료")
    print(f"{'=' * 72}\n")


if __name__ == "__main__":
    main()
