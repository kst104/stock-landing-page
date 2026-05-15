"""
한국 주식(코스피/코스닥) 상위 100개 종목 차트 자동 분석
Gemini Vision API + yfinance + mplfinance
"""

import os
import asyncio
import json
import re
import base64
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.font_manager as fm

import yfinance as yf
import mplfinance as mpf
import pandas as pd
from dotenv import load_dotenv
from google import genai
from google.genai import types

# ── 환경 변수 로드 ─────────────────────────────────────────────────────────────
load_dotenv()
GOOGLE_API_KEY = os.getenv("GOOGLE_API_KEY")
GEMINI_MODEL   = os.getenv("GEMINI_MODEL", "gemini-2.0-flash-preview-image-generation")

if not GOOGLE_API_KEY:
    raise EnvironmentError(".env 파일에 GOOGLE_API_KEY가 없습니다.")

client = genai.Client(api_key=GOOGLE_API_KEY)

# ── 한글 폰트 설정 ─────────────────────────────────────────────────────────────
_FONT_CANDIDATES = [
    "/System/Library/Fonts/AppleSDGothicNeo.ttc",          # macOS
    "/usr/share/fonts/truetype/nanum/NanumGothic.ttf",     # Linux
    "C:/Windows/Fonts/malgun.ttf",                          # Windows
]
for _fp in _FONT_CANDIDATES:
    if Path(_fp).exists():
        fm.fontManager.addfont(_fp)
        plt.rcParams["font.family"] = fm.FontProperties(fname=_fp).get_name()
        break
plt.rcParams["axes.unicode_minus"] = False

# ── 종목 리스트 ────────────────────────────────────────────────────────────────
STOCKS: dict[str, str] = {
    "005930.KS": "삼성전자",       "000660.KS": "SK하이닉스",       "373220.KS": "LG에너지솔루션",
    "207940.KS": "삼성바이오로직스","005380.KS": "현대차",           "000270.KS": "기아",
    "006400.KS": "삼성SDI",        "051910.KS": "LG화학",           "035420.KS": "NAVER",
    "035720.KS": "카카오",         "005490.KS": "POSCO홀딩스",      "055550.KS": "신한지주",
    "105560.KS": "KB금융",         "003670.KS": "포스코퓨처엠",     "012330.KS": "현대모비스",
    "066570.KS": "LG전자",         "003550.KS": "LG",               "032830.KS": "삼성생명",
    "086790.KS": "하나금융지주",   "034730.KS": "SK",               "015760.KS": "한국전력",
    "096770.KS": "SK이노베이션",   "017670.KS": "SK텔레콤",         "030200.KS": "KT",
    "316140.KS": "우리금융지주",   "009150.KS": "삼성전기",         "010130.KS": "고려아연",
    "028260.KS": "삼성물산",       "034020.KS": "두산에너빌리티",   "011200.KS": "HMM",
    "018260.KS": "삼성에스디에스", "033780.KS": "KT&G",             "000810.KS": "삼성화재",
    "010950.KS": "S-Oil",          "009540.KS": "HD한국조선해양",   "267250.KS": "HD현대",
    "003490.KS": "대한항공",       "036570.KS": "엔씨소프트",       "011170.KS": "롯데케미칼",
    "024110.KS": "기업은행",       "000720.KS": "현대건설",         "010140.KS": "삼성중공업",
    "047050.KS": "포스코인터내셔널","009240.KS": "한샘",            "090430.KS": "아모레퍼시픽",
    "051900.KS": "LG생활건강",     "329180.KS": "HD현대중공업",     "004020.KS": "현대제철",
    "000100.KS": "유한양행",       "011780.KS": "금호석유",         "016360.KS": "삼성증권",
    "006800.KS": "미래에셋증권",   "138040.KS": "메리츠금융지주",   "003410.KS": "쌍용C&E",
    "069500.KS": "KODEX 200",      "352820.KS": "하이브",           "259960.KS": "크래프톤",
    "042660.KS": "한화오션",       "402340.KS": "SK스퀘어",         "361610.KS": "SK아이이테크놀로지",
    "001570.KS": "금양",           "271560.KS": "오리온",           "000080.KS": "하이트진로",
    "002790.KS": "아모레G",        "088350.KS": "한화생명",         "161390.KS": "한국타이어앤테크놀로지",
    "004170.KS": "신세계",         "021240.KS": "코웨이",           "006360.KS": "GS건설",
    "071050.KS": "한국금융지주",   "139480.KS": "이마트",           "326030.KS": "SK바이오팜",
    "180640.KS": "한진칼",         "032640.KS": "LG유플러스",       "078930.KS": "GS",
    "247540.KQ": "에코프로비엠",   "086520.KQ": "에코프로",         "377300.KQ": "카카오페이",
    "263750.KQ": "펄어비스",       "068270.KQ": "셀트리온",         "196170.KQ": "알테오젠",
    "145020.KQ": "휴젤",           "041510.KQ": "에스엠",           "293490.KQ": "카카오게임즈",
    "112040.KQ": "위메이드",       "035900.KQ": "JYP Ent.",         "357780.KQ": "솔브레인",
    "028300.KQ": "에이치엘비",     "095340.KQ": "ISC",              "039030.KQ": "이오테크닉스",
    "058470.KQ": "리노공업",       "005290.KQ": "동진쎄미켐",       "383220.KQ": "F&F",
    "454910.KQ": "에이피알",       "322510.KQ": "제이엘케이",       "236810.KQ": "엔비티",
    "403870.KQ": "HPSP",           "067310.KQ": "하나마이크론",     "218410.KQ": "RFHIC",
    "041920.KQ": "메디아나",
}

CHARTS_DIR = Path("charts_kr")
CHARTS_DIR.mkdir(exist_ok=True)


# ── Step 1: 차트 생성 ──────────────────────────────────────────────────────────
def download_and_plot(ticker: str, name: str) -> Path | None:
    """yfinance로 데이터 다운로드 후 mplfinance 캔들차트 저장."""
    out_path = CHARTS_DIR / f"{ticker}_{name}.png"
    if out_path.exists():
        print(f"  [SKIP] {name} 차트 이미 존재")
        return out_path

    try:
        raw = yf.download(ticker, period="1y", progress=False, auto_adjust=True)
        if raw.empty or len(raw) < 30:
            print(f"  [WARN] {name}({ticker}) 데이터 부족 — 건너뜀")
            return None

        # MultiIndex 컬럼 처리
        if isinstance(raw.columns, pd.MultiIndex):
            raw.columns = raw.columns.droplevel("Ticker")

        raw.index = pd.DatetimeIndex(raw.index)
        raw = raw[["Open", "High", "Low", "Close", "Volume"]].dropna()

        mc = mpf.make_marketcolors(up="r", down="b", inherit=True)
        style = mpf.make_mpf_style(marketcolors=mc, gridstyle="--", gridcolor="#e0e0e0")

        add_plots = [
            mpf.make_addplot(raw["Close"].rolling(20).mean(),  color="cyan",   width=1.2, label="MA20"),
            mpf.make_addplot(raw["Close"].rolling(50).mean(),  color="orange", width=1.2, label="MA50"),
            mpf.make_addplot(raw["Close"].rolling(200).mean(), color="red",    width=1.2, label="MA200"),
        ]

        mpf.plot(
            raw,
            type="candle",
            style=style,
            title=f"{name} ({ticker})",
            volume=True,
            addplot=add_plots,
            savefig=dict(fname=str(out_path), dpi=150, bbox_inches="tight"),
            figsize=(14, 8),
        )
        plt.close("all")
        print(f"  [OK] {name} 차트 저장 → {out_path.name}")
        return out_path

    except Exception as e:
        print(f"  [ERR] {name}({ticker}) 차트 생성 실패: {e}")
        plt.close("all")
        return None


def step1_generate_charts() -> dict[str, Path]:
    """모든 종목 차트 생성."""
    print("\n" + "=" * 60)
    print("Step 1: 차트 생성")
    print("=" * 60)
    chart_paths: dict[str, Path] = {}
    for ticker, name in STOCKS.items():
        path = download_and_plot(ticker, name)
        if path:
            chart_paths[ticker] = path
    print(f"\n차트 생성 완료: {len(chart_paths)}/{len(STOCKS)}개")
    return chart_paths


# ── Step 2: Gemini Vision 분석 ─────────────────────────────────────────────────
ANALYSIS_PROMPT = """\
당신은 25년 경력의 기술적 분석 전문가입니다.

이 {name}({ticker}) 한국 주식 차트를 분석해주세요.

다음 항목을 확인하세요:
1. 이동평균선(20/50/200) 배열 상태
2. RSI가 30 이하(과매도) 또는 70 이상(과매수)인지
3. 거래량이 최근 20일 평균 대비 증감
4. 볼린저밴드 상/하단 터치 여부

반드시 아래 JSON 형식으로만 응답하세요(마크다운 코드블록 없이):
{{
  "signal": "BUY|HOLD|SELL",
  "confidence": 0.0~1.0,
  "reasons": ["이유1", "이유2"],
  "ma_status": "정배열|역배열|혼조",
  "rsi_zone": "과매도|중립|과매수",
  "volume_trend": "증가|감소|보합"
}}
"""

def _parse_gemini_response(text: str) -> dict:
    """Gemini 응답에서 JSON 추출 (list 또는 dict 처리)."""
    # 코드블록 제거
    text = re.sub(r"```(?:json)?", "", text).strip().rstrip("`").strip()
    data = json.loads(text)
    if isinstance(data, list):
        data = data[0]
    return data


def _analyze_sync(ticker: str, name: str, img_path: Path) -> dict:
    """단일 종목 동기 분석."""
    with open(img_path, "rb") as f:
        img_bytes = f.read()

    prompt = ANALYSIS_PROMPT.format(name=name, ticker=ticker)

    response = client.models.generate_content(
        model=GEMINI_MODEL,
        contents=[
            types.Part.from_bytes(data=img_bytes, mime_type="image/png"),
            types.Part.from_text(text=prompt),
        ],
        config=types.GenerateContentConfig(
            response_mime_type="application/json",
        ),
    )

    raw_text = response.text or ""
    result = _parse_gemini_response(raw_text)
    result.update({"ticker": ticker, "name": name})
    return result


async def step2_analyze_charts(chart_paths: dict[str, Path]) -> list[dict]:
    """asyncio + Semaphore로 병렬 Gemini 분석."""
    print("\n" + "=" * 60)
    print("Step 2: Gemini Vision 분석")
    print("=" * 60)

    sem = asyncio.Semaphore(10)
    loop = asyncio.get_event_loop()
    executor = ThreadPoolExecutor(max_workers=10)
    results: list[dict] = []

    async def analyze_one(ticker: str, img_path: Path):
        name = STOCKS[ticker]
        async with sem:
            try:
                result = await loop.run_in_executor(
                    executor, _analyze_sync, ticker, name, img_path
                )
                print(f"  [OK] {name}({ticker}) → signal={result.get('signal')}, conf={result.get('confidence')}")
                results.append(result)
            except Exception as e:
                print(f"  [ERR] {name}({ticker}) 분석 실패: {e}")
                results.append({
                    "ticker": ticker, "name": name,
                    "signal": "HOLD", "confidence": 0.0,
                    "reasons": [str(e)], "ma_status": "N/A",
                    "rsi_zone": "N/A", "volume_trend": "N/A",
                })

    tasks = [analyze_one(ticker, path) for ticker, path in chart_paths.items()]
    await asyncio.gather(*tasks)
    executor.shutdown(wait=False)
    print(f"\n분석 완료: {len(results)}개")
    return results


# ── Step 3: 결과 종합 ──────────────────────────────────────────────────────────
def step3_summarize(results: list[dict]) -> pd.DataFrame:
    """DataFrame 변환, 정렬, 출력, CSV 저장."""
    print("\n" + "=" * 60)
    print("Step 3: 결과 종합")
    print("=" * 60)

    rows = []
    for r in results:
        ticker = r.get("ticker", "")
        market = "코스피" if ticker.endswith(".KS") else "코스닥"
        rows.append({
            "종목코드":     ticker,
            "종목명":       r.get("name", ""),
            "시장":         market,
            "signal":       r.get("signal", "HOLD"),
            "confidence":   float(r.get("confidence", 0.0)),
            "ma_status":    r.get("ma_status", "N/A"),
            "rsi_zone":     r.get("rsi_zone", "N/A"),
            "volume_trend": r.get("volume_trend", "N/A"),
            "reasons":      " | ".join(r.get("reasons", [])) if isinstance(r.get("reasons"), list) else str(r.get("reasons", "")),
        })

    df = pd.DataFrame(rows).sort_values("confidence", ascending=False).reset_index(drop=True)

    # 신호별 카운트
    counts = df["signal"].value_counts()
    print(f"\n[신호 요약]")
    for sig in ["BUY", "HOLD", "SELL"]:
        print(f"  {sig}: {counts.get(sig, 0)}개")

    # BUY 종목 상세
    buy_df = df[df["signal"] == "BUY"]
    if not buy_df.empty:
        print(f"\n[BUY 종목 상세 — {len(buy_df)}개]")
        print(buy_df[["종목명", "종목코드", "시장", "confidence", "ma_status", "rsi_zone"]].to_string(index=False))

    # CSV 저장
    csv_path = "gemini_chart_analysis_kr.csv"
    df.to_csv(csv_path, index=False, encoding="utf-8-sig")
    print(f"\n결과 저장 → {csv_path}")
    return df


# ── 메인 ───────────────────────────────────────────────────────────────────────
async def main():
    chart_paths = step1_generate_charts()
    if not chart_paths:
        print("차트가 생성되지 않았습니다. 프로그램을 종료합니다.")
        return

    results = await step2_analyze_charts(chart_paths)
    step3_summarize(results)


if __name__ == "__main__":
    asyncio.run(main())
