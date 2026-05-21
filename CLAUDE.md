# CLAUDE.md — Stock Screener Hub

## Project Overview

A Flask-based Korean stock screener hub with 48 distinct screeners for KOSPI/KOSDAQ markets. The app provides a web UI where users trigger screener jobs, poll SSE progress streams, and download CSV results. Real-time screeners run on background threads and send email alerts. Deployed on Render.

---

## Architecture

### Files

| File | Purpose |
|------|---------|
| `app.py` | Monolithic Flask app (~15,400 lines): all screeners, routes, KIS API client, auth, email, data utilities |
| `index.html` | Static landing page (served separately, not through Flask) |
| `vl_screener.py` | Standalone CLI script for VL screener (mirrors screener 1 logic) |
| `surge_analysis.py` | Standalone CLI script for surge analysis (uses pykrx, not deployed) |
| `leader_consolidation.py` | Standalone CLI script for leader consolidation (uses Naver API, not deployed) |
| `main_kr.py` | Standalone CLI script for Gemini Vision chart analysis (not deployed) |
| `requirements.txt` | Python dependencies for the deployed Flask app only |
| `render.yaml` | Render deployment config |
| `.env.example` | Template for required environment variables |
| `SKILL.md` | Design conventions for landing page generation |

### Route Structure

```
/                    → Hub page (lists all screeners)
/login               → Login (POST: email + password)
/logout              → Clear session
/auth/manage         → Admin: add/delete users, manage email settings
/screener/<sid>      → Individual screener UI page
/api/<sid>/start     → POST: kick off a screening job
/api/<sid>/progress  → GET: SSE stream of progress (0–100%)
/api/<sid>/result    → GET: JSON result rows
/api/<sid>/download  → GET: CSV download

# Special screener routes
/api/14/realtime/*   → Screener 14 real-time polling (start/stop/status/email)
/api/19/realtime/*   → Screener 19 real-time
/api/20/realtime/*   → Screener 20 real-time
/api/21/realtime/*   → Screener 21 (Naver theme) real-time
/api/31/start        → Screener 31 (pro RSI)
/api/44/start        → Screener 44 (Market shift levels)
/api/44/realtime/*   → Screener 44 real-time
/api/46/realtime/*   → Screener 46 real-time
/api/43/start        → Screener 43
/api/48/start        → Screener 48
```

### Authentication

- Session-based (Flask sessions, secret stored in `.auth_secret`)
- Admin: `AUTH_ADMIN_EMAIL` + `AUTH_ADMIN_PASSWORD` env vars
- Additional users stored in `authorized_users.json` (gitignored) with bcrypt hashes
- `@app.before_request` guard redirects unauthenticated requests to `/login`
- API routes return JSON 401 when unauthenticated

---

## Data Sources

| Source | Use |
|--------|-----|
| `FinanceDataReader` (fdr) | Primary OHLCV history for all screeners |
| Korea Investment Securities (KIS) API | Real-time/intraday price overlay; fallback when FDR fails; trade-value rankings; intraday volume |
| Naver Finance API | Stock listing (alternative to FDR listing), theme rankings (screener 21), leader consolidation script |

### KIS API Client (`KISClient` class, `app.py:225`)

- OAuth2 token auto-managed; cached in `kis_token.json` (gitignored)
- Token refreshed 10 minutes before expiry; thread-safe via `threading.Lock`
- On token-expiry response codes (`EGW00121`, `EGW00123`, `EGW00201`), automatically re-issues token and retries once
- Key methods: `current_price`, `get_price_detail`, `trade_value_ranking`, `ohlcv_bars`
- `ohlcv_bars` parallelises 100-bar KIS windows via `ThreadPoolExecutor` (max 6 workers)

### OHLCV Data (`fetch_ohlcv`, `app.py:584`)

- Tries FDR first; falls back to KIS on failure
- Results cached in `_ohlcv_cache` (in-memory dict) with `threading.Lock`

### Stock Listing (`_get_listing`, `app.py:706`)

- Fetches KRX listing; cached in `.listing_cache.json` (gitignored)
- Progress-aware variant: `_get_listing_with_progress` (updates `prog` dict for SSE)

---

## Screener System

### SCREENERS dict (`app.py:1056`)

48 screeners defined as `{id: {title, desc, color, icon}}`. IDs are not fully contiguous (42 and 43 are swapped in order; 48 is the latest).

### Screener Pattern

Each screener `N` has:
- `_screenN_ticker(code, start, end) → dict | None` — per-ticker filter; returns result row or None
- `run_screenN(date_str, prog) → pd.DataFrame` — drives the full scan, updates `prog` dict for SSE
- Real-time screeners additionally have `_run_realtimeN()` running on a background thread

### Per-Job State

Each running screener job stores state in module-level dicts keyed by screener ID:
- `prog` dict with `pct` (0–100), `total`, `done`, `rows`, `error`, `start_time`
- Thread references for real-time screeners (start/stop flags)

### Progress SSE (`/api/<sid>/progress`)

Streams `data: <pct>\n\n` events. Frontend polls until `pct == 100`.

### Technical Indicators (all in `app.py`)

| Function | Location | Description |
|----------|----------|-------------|
| `linreg(series, period)` | `app.py:880` | Linear regression value (TradingView-compatible) |
| `ema(s, p)` | `app.py:891` | Exponential moving average |
| `_calc_adx(df, period)` | `app.py:895` | Average Directional Index |
| `_wma(series, period)` | `app.py:956` | Weighted moving average |
| `_linreg_slope(series, period)` | `app.py:969` | Linear regression slope |
| `_var_ma(series, period, ...)` | `app.py:984` | Variable MA (VIDYA) |
| `_hull_ma(series, period)` | `app.py:1006` | Hull moving average |
| `_tillson_t3(series, period, ...)` | `app.py:1013` | Tillson T3 |
| `_ma_type(series, period, ...)` | `app.py:1028` | Dispatcher for MA types |

VL (Virtual Line) formula: `VL = linreg(C, 50) + (linreg(C, 50) − linreg(linreg(C, 50), 50))`

---

## Screener Reference (IDs 1–48)

| ID | Name | Key Conditions |
|----|------|----------------|
| 1 | VL스크리너 | Marcap ≥ 1500억, EMA200 rising, Close > EMA60, VL > Close, VL drop −5~−15% |
| 2 | 금일급락 | Screener 1 conditions + today −5% drop |
| 3 | 역사적스퀴즈 | Weekly volume BB bandwidth at 52-week min + daily EMA200 rising |
| 4 | 거래량점수 | 20d vol MA 3d rising + EMA200 3d rising, price/volume score |
| 5 | ASGMA | Avg vol ≥ 300k, VR14×ATR ≥ 3, base candle in last 15d |
| 6 | 폭발직전 | ASGMA score ≥ 3.0 + last 3d range ≤ ATR×70% |
| 7 | 폭발준비 | Avg vol ≥ 300k, ASGMA ≤ 1, base candle, SMA5 or SMA20 breakout |
| 8 | 하이킨아시 주봉 | Avg vol ≥ 100k, weekly bullish, HA resistance breakout |
| 9 | 비율골든크로스 | Avg vol ≥ 100k, base candle in 20d, vol ratio 90d golden cross |
| 10 | 엔벨로프눌림 | Marcap ≥ 1500억, upper envelope touch in 7d, price near VL |
| 11 | VL매수타점 | Envelope breakout history, VL V-shaped reversal, price within 10% VL |
| 12 | Harvard RSI | RSI(21) band pattern: golden cross after pullback |
| 13 | 이제출발 | Marcap ≥ 3000억, envelope upper sideways, recent high near upper band |
| 14 | 세력20평단 돌파 | Marcap ≥ 3000억, 세력20평단 breakout, intraday vol ≥ 200% |
| 15 | 세력20평단임박 | Marcap ≥ 3000억, close near 세력20평단, close > VL, VL +3% |
| 16 | RSI밴드 압축돌파 | Marcap ≥ 1500억, RSI(21) band 3d compression + breakout |
| 17 | MACD Reloaded | Marcap ≥ 1500억, Hull MA MACD cross up + bullish candle |
| 18 | MACD Reloaded2 | Marcap ≥ 1500억, high volume, Hull MACD histogram rising |
| 19 | 급락회귀선 | Marcap ≥ 1500억, recent VL surge history, weekly low rising 2w |
| 20 | 전고점돌파 | Marcap ≥ 3000억, bearish high breakout, intraday vol ≥ 250% |
| 21 | 테마상한가 | Naver top-5 rising themes with bullish constituent stocks |
| 22 | 캔들볼륨 | Marcap ≥ 1500억, weekly candle-volume resistance breakout |
| 23 | 세력20평단돌파 | Marcap ≥ 3000억, avg vol ≥ 300k, 5d close within 10% of 세력평단 |
| 24 | RSI다이버전스 | Marcap ≥ 3000억, RSI(30) bullish divergence in 60 bars |
| 25 | 거래대금순위 | KIS trade-value rank rise ≥ 30, exclude prev top-10, vol ≥ 200% |
| 26 | 20일선전고점돌파 | Marcap ≥ 1500억, SMA20 newly breaks 60-bar prior high |
| 27 | 최강종목 | Marcap ≥ 1500억, close above BB(200,3) upper or Envelope(20,40%) upper |
| 28 | RSI밴드돌파 | Marcap ≥ 1500억, RSI(21) band breakout (no compression requirement) |
| 29 | 양음양 | Marcap ≥ 1500억, strong bullish → low-vol bearish → bullish candle pattern |
| 30 | VL급반등 | Marcap ≥ 1500억, VL peak→trough drop ≥ 40%, VL rebound ≥ 4%/d for 3d |
| 31 | pro RSI | Extended RSI strategy |
| 32 | 캔들볼륨저항 | Candle-volume resistance variant |
| 33 | 과열스코어 | Overheat score |
| 34 | 과열스코어순위 | Overheat score ranking |
| 35 | 거래대금순위 | Trade value ranking variant |
| 36 | 이제진짜출발 | Extended departure signal |
| 37 | 거래대금RSI | Trade value RSI |
| 38 | 패턴검색 | Chart pattern search (image upload → pattern match) |
| 39 | VL이격시작 | VL divergence start |
| 40 | 파워맵우수 | Power map — good |
| 41 | 파워맵최고 | Power map — best |
| 42 | 파워수급분석 | Power supply/demand analysis |
| 43 | proRSI2 | Pro RSI variant 2 |
| 44 | Market shift levels | Market shift level detection (real-time capable) |
| 45 | QQE | Quantitative Qualitative Estimation |
| 46 | MSL2 | Market shift level 2 (real-time capable) |
| 47 | 과열스코어(월) | Monthly overheat score |
| 48 | 칼만트렌드라인 | Kalman filter trendline screener |

---

## Email Notification System

- Config stored in `email_config.json` (gitignored); up to 5 recipients
- Gmail SMTP with app password; base64-encoded subject/body for Korean content
- Real-time screeners (14, 19, 20, 21, 44, 46) support email alerts
- `_send_email_alert(subject, body)` at `app.py:167`

---

## Environment Variables

| Variable | Required | Description |
|----------|----------|-------------|
| `KIS_KEY` | Yes | Korea Investment Securities API key |
| `KIS_SECRET` | Yes | Korea Investment Securities API secret |
| `KIS_BASE` | No | KIS API base URL (default: `https://openapi.koreainvestment.com:9443`) |
| `AUTH_ADMIN_EMAIL` | No | Admin login email (default: `promokorea@gmail.com`) |
| `AUTH_ADMIN_PASSWORD` | Yes | Admin login password |

---

## Local Development

```powershell
# Windows
python -m pip install -r requirements.txt
$env:KIS_KEY="your-kis-key"
$env:KIS_SECRET="your-kis-secret"
$env:AUTH_ADMIN_PASSWORD="your-admin-password"
python app.py
```

```bash
# Linux/Mac
pip install -r requirements.txt
export KIS_KEY="your-kis-key"
export KIS_SECRET="your-kis-secret"
export AUTH_ADMIN_PASSWORD="your-admin-password"
python app.py
```

App runs at `http://localhost:8888`.

---

## Deployment

Deployed on **Render** as a Python web service:
- Build: `pip install -r requirements.txt`
- Start: `python app.py`
- Config: `render.yaml`

The `index.html` is a static landing page and is not served by Flask directly.

---

## Gitignored Runtime Files

These files are created at runtime and must not be committed:

| File | Contents |
|------|----------|
| `.auth_secret` | Flask session signing secret |
| `authorized_users.json` | Additional user accounts (hashed passwords) |
| `email_config.json` | Email SMTP credentials and recipient list |
| `kis_token.json` | KIS OAuth2 access token cache |
| `.listing_cache.json` | KRX stock listing cache |
| `vl_result_*.csv` | Screener output CSVs |

---

## Key Conventions

### Adding a New Screener

1. Add an entry to the `SCREENERS` dict at `app.py:1056` with the next ID, title, desc, color, icon.
2. Implement `_screenN_ticker(code, start, end) → dict | None` — must be exception-safe and return `None` to exclude a ticker.
3. Implement `run_screenN(date_str, prog) → pd.DataFrame` following the pattern of existing runners: fetch listing, filter by market cap, call `_run_screen_parallel(valid_df, _screenN_ticker, start, end, prog)`.
4. Wire to the generic `/api/<sid>/start` handler — no new route needed for standard screeners.
5. For real-time screeners, add `/api/N/realtime/start|stop|status` routes and implement `_run_realtimeN()`.

### Screener Implementation Rules

- All ticker functions must catch all exceptions and return `None` on failure — a bad ticker must never crash the scan.
- Market cap filters use `Marcap` column from `_get_listing()` in Won units; convert to 억 when displaying (`// 100_000_000`).
- Use `fetch_ohlcv(code, start, end)` (not fdr directly) so the in-memory OHLCV cache is shared.
- `_run_screen_parallel` handles threading and `prog` updates — prefer it over custom threading.
- Indicators should match TradingView definitions exactly (the `linreg` function is TradingView-compatible).
- Stock codes are always zero-padded to 6 digits: `str(code).zfill(6)`.

### Thread Safety

- `_ohlcv_cache` protected by `_ohlcv_cache_lock`
- `KISClient._token` protected by `KISClient._lock`
- Real-time screeners use module-level `threading.Event` or boolean flags for stop signalling

### Korean String Handling

- Use `_plog(msg)` instead of `print(msg)` for any Korean text — handles Windows CP949 encoding errors.
- JSON files written with `ensure_ascii=False`.
- CSV downloads use `encoding="utf-8-sig"` (BOM) for Excel compatibility.

### Security

- All routes protected by `@app.before_request` session check at `app.py:9633`.
- HTML in dynamically generated pages escaped with `html.escape` / `_html.escape`.
- Session cookie: `HttpOnly=True`, `SameSite=Lax`.
- Admin password compared directly (env var, no hash); additional users use `werkzeug.security` bcrypt.

---

## Standalone Scripts (not part of Flask app)

These scripts have their own dependencies (pykrx, tabulate, yfinance, mplfinance, google-genai) not listed in `requirements.txt`:

- `vl_screener.py` — CLI VL screener, outputs CSV
- `surge_analysis.py` — Surge analysis using pykrx
- `leader_consolidation.py` — Leader stock consolidation using Naver API
- `main_kr.py` — Gemini Vision chart analysis for top 100 Korean stocks

Do not add these scripts' dependencies to `requirements.txt` unless they are needed by the Flask app.
