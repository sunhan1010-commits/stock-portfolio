"""
종목 데이터 조회 모듈.
- 한국 종목: 네이버 금융 모바일 API (안정적인 JSON)
- 미국 종목: yfinance
결과는 공통 Quote(dict 형태)로 정규화한다.
지연 시세 기준(무료 데이터). 실시간이 아님에 유의.
"""
from __future__ import annotations
import certs  # noqa: F401  (SSL 환경변수 먼저 세팅)

import re
import time
import difflib
import requests

_HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}

# 간단한 메모리 캐시 (같은 종목 반복 조회 시 과도한 요청 방지)
_CACHE: dict[str, tuple[float, dict]] = {}
_CACHE_TTL = 60  # 초


# ----------------------------- 파싱 유틸 -----------------------------
def _to_float(s):
    """'17.78배', '46.54%', '12,372원' 등에서 숫자만 추출."""
    if s is None:
        return None
    if isinstance(s, (int, float)):
        return float(s)
    s = str(s).replace(",", "").strip()
    if s in ("", "-", "N/A"):
        return None
    m = re.search(r"-?\d+(?:\.\d+)?", s)
    return float(m.group()) if m else None


def _parse_kr_money(s):
    """'1조 6,830억', '15조 7,923억', '3,500억' -> 원 단위 정수."""
    if not s or s in ("-",):
        return None
    s = s.replace(",", "")
    total = 0
    m = re.search(r"(\d+(?:\.\d+)?)\s*조", s)
    if m:
        total += float(m.group(1)) * 1_0000_0000_0000
    m = re.search(r"(\d+(?:\.\d+)?)\s*억", s)
    if m:
        total += float(m.group(1)) * 1_0000_0000
    m = re.search(r"(\d+(?:\.\d+)?)\s*만", s)
    if m:
        total += float(m.group(1)) * 1_0000
    if total == 0:
        v = _to_float(s)
        return v
    return total


# ----------------------------- 한국 종목 목록(KRX) 인덱스 -----------------------------
_KR_INDEX = None  # [(code, name), ...]


def _fmt_code(c):
    c = str(c).strip()
    # 순수 숫자 코드는 6자리로 0채움. ETF 등 영숫자 코드(예 0072R0)는 그대로.
    return c.zfill(6) if c.isdigit() else c


def _kr_index():
    """국내 주식 + ETF 통합 목록 [(code, name), ...] (1회 로딩 후 캐시)."""
    global _KR_INDEX
    if _KR_INDEX is None:
        idx = []
        try:
            import FinanceDataReader as fdr
        except Exception:
            fdr = None
        if fdr is not None:
            # 주식(KRX)
            try:
                df = fdr.StockListing("KRX")
                for c, n in zip(df["Code"], df["Name"]):
                    if c is not None and n is not None:
                        idx.append((_fmt_code(c), str(n)))
            except Exception:
                pass
            # ETF (컬럼명이 Symbol)
            try:
                etf = fdr.StockListing("ETF/KR")
                code_col = "Symbol" if "Symbol" in etf.columns else "Code"
                for c, n in zip(etf[code_col], etf["Name"]):
                    if c is not None and n is not None:
                        idx.append((_fmt_code(c), str(n)))
            except Exception:
                pass
        # 코드 기준 중복 제거
        seen, out = set(), []
        for c, n in idx:
            if c not in seen:
                seen.add(c)
                out.append((c, n))
        _KR_INDEX = out
    return _KR_INDEX


def _norm(s: str) -> str:
    return re.sub(r"\s+", "", str(s)).lower()


# 미국 인기 종목/ETF — (티커, 표시명, [한글·영문 별칭]) : 한글 이름·자동완성 검색용
_US_STOCKS = [
    ("AAPL", "Apple 애플", ["애플", "apple"]),
    ("MSFT", "Microsoft 마이크로소프트", ["마이크로소프트", "마소", "microsoft"]),
    ("NVDA", "NVIDIA 엔비디아", ["엔비디아", "엔디비아", "nvidia"]),
    ("GOOGL", "Alphabet(Google) 알파벳/구글", ["구글", "알파벳", "google", "alphabet"]),
    ("AMZN", "Amazon 아마존", ["아마존", "amazon"]),
    ("META", "Meta 메타(페이스북)", ["메타", "페이스북", "meta", "facebook"]),
    ("TSLA", "Tesla 테슬라", ["테슬라", "tesla"]),
    ("AVGO", "Broadcom 브로드컴", ["브로드컴", "broadcom"]),
    ("NFLX", "Netflix 넷플릭스", ["넷플릭스", "netflix"]),
    ("AMD", "AMD 에이엠디", ["에이엠디", "amd"]),
    ("INTC", "Intel 인텔", ["인텔", "intel"]),
    ("QCOM", "Qualcomm 퀄컴", ["퀄컴", "qualcomm"]),
    ("PLTR", "Palantir 팔란티어", ["팔란티어", "palantir"]),
    ("COIN", "Coinbase 코인베이스", ["코인베이스", "coinbase"]),
    ("MSTR", "MicroStrategy 마이크로스트래티지", ["마이크로스트래티지", "microstrategy"]),
    ("KO", "Coca-Cola 코카콜라", ["코카콜라", "cocacola"]),
    ("PEP", "PepsiCo 펩시", ["펩시", "pepsi"]),
    ("MCD", "McDonald's 맥도날드", ["맥도날드", "mcdonald"]),
    ("SBUX", "Starbucks 스타벅스", ["스타벅스", "starbucks"]),
    ("NKE", "Nike 나이키", ["나이키", "nike"]),
    ("DIS", "Disney 디즈니", ["디즈니", "disney"]),
    ("V", "Visa 비자", ["비자", "visa"]),
    ("MA", "Mastercard 마스터카드", ["마스터카드", "mastercard"]),
    ("JPM", "JPMorgan 제이피모건", ["제이피모건", "jpmorgan", "jp모건"]),
    ("BAC", "Bank of America 뱅크오브아메리카", ["뱅크오브아메리카", "bofa"]),
    ("WMT", "Walmart 월마트", ["월마트", "walmart"]),
    ("COST", "Costco 코스트코", ["코스트코", "costco"]),
    ("PG", "Procter & Gamble P&G", ["피앤지", "p&g", "pg"]),
    ("JNJ", "Johnson & Johnson 존슨앤존슨", ["존슨앤존슨", "jnj"]),
    ("UNH", "UnitedHealth 유나이티드헬스", ["유나이티드헬스", "unitedhealth"]),
    ("XOM", "Exxon Mobil 엑슨모빌", ["엑슨모빌", "exxon"]),
    ("BRK-B", "Berkshire Hathaway 버크셔", ["버크셔", "berkshire"]),
    ("LLY", "Eli Lilly 일라이릴리", ["일라이릴리", "릴리", "lilly"]),
    ("ORCL", "Oracle 오라클", ["오라클", "oracle"]),
    ("CRM", "Salesforce 세일즈포스", ["세일즈포스", "salesforce"]),
    ("ADBE", "Adobe 어도비", ["어도비", "adobe"]),
    ("UBER", "Uber 우버", ["우버", "uber"]),
    ("BA", "Boeing 보잉", ["보잉", "boeing"]),
    # 인기 ETF
    ("SPY", "SPDR S&P500 ETF", ["s&p500", "spy", "에스앤피"]),
    ("VOO", "Vanguard S&P500 ETF", ["voo", "뱅가드s&p"]),
    ("QQQ", "Invesco 나스닥100 ETF", ["나스닥", "qqq", "나스닥100"]),
    ("SCHD", "Schwab 배당 ETF SCHD", ["슈드", "schd"]),
    ("DIA", "다우존스 ETF DIA", ["다우", "dia", "dow"]),
    ("VT", "Vanguard 전세계 ETF VT", ["vt", "전세계"]),
    ("JEPI", "JPMorgan 커버드콜 ETF JEPI", ["jepi"]),
]


def _us_index():
    return [(t, name) for t, name, _ in _US_STOCKS]


def _us_match(q):
    """한글/영문 이름·티커로 미국 종목 매칭 -> [(ticker, name), ...]."""
    ql = _norm(q)
    if not ql:
        return []
    exact = []
    for t, name, aliases in _US_STOCKS:
        keys = {t.lower()} | {_norm(a) for a in aliases}
        if ql in keys:
            exact.append((t, name))
    if exact:
        return exact
    part = []
    for t, name, aliases in _US_STOCKS:
        hay = " ".join([t.lower(), _norm(name)] + [_norm(a) for a in aliases])
        if ql in hay:
            part.append((t, name))
    part.sort(key=lambda x: len(x[1]))
    return part[:8]


def completer_index():
    """자동완성용 통합 목록(국내 + 미국 인기종목)."""
    return _kr_index() + _us_index()


def _kr_search_naver(q):
    """네이버 자동완성 -> [(code, name), ...] (국내 6자리 종목만)."""
    out = []
    try:
        r = requests.get("https://ac.stock.naver.com/ac",
                         params={"q": q, "target": "stock,index"},
                         headers=_HEADERS, timeout=6)
        for item in r.json().get("items", []):
            code, name = item.get("code"), item.get("name")
            if code and re.fullmatch(r"\d{6}", str(code)):
                out.append((str(code), name))
    except Exception:
        pass
    return out


def _kr_search_index(q):
    """KRX 전체목록에서 정확 -> 부분 -> 유사(퍼지) 매칭. (네이버가 못 찾을 때만 호출)"""
    idx = _kr_index()
    if not idx:
        return []
    qn = _norm(q)

    exact = [(c, n) for c, n in idx if _norm(n) == qn]
    if exact:
        return exact[:8]

    partial = [(c, n) for c, n in idx
               if qn and (qn in _norm(n) or _norm(n) in qn)]
    if partial:
        partial.sort(key=lambda x: len(x[1]))  # 짧은 이름(대표종목) 우선
        return partial[:8]

    # 유사(퍼지) 매칭 — 앞 두 글자를 공유하는 후보로 좁혀 빠르게
    key = q[:2]
    pool = [(c, n) for c, n in idx if key and key in n] or idx
    names = [n for _, n in pool]
    close = difflib.get_close_matches(q, names, n=5, cutoff=0.6)
    out, have = [], set()
    for cm in close:
        for c, n in pool:
            if n == cm and n not in have:
                out.append((c, n)); have.add(n); break
    return out[:8]


def search(query: str):
    """검색어 -> 후보 리스트 [(market, code, name), ...]."""
    q = (query or "").strip()
    if not q:
        return []
    if re.fullmatch(r"\d{6}", q):
        return [("KR", q, None)]

    def _dedup(pairs):
        seen, out = set(), []
        for code, name in pairs:
            if code not in seen:
                seen.add(code)
                out.append(("KR", code, name))
        return out

    # 1) 네이버 자동완성이 찾으면 즉시 반환(빠름) — KRX 전체 로딩 회피
    naver = _kr_search_naver(q)
    if naver:
        return _dedup(naver)

    # 2) 미국 인기종목(한글 이름·티커) 매칭 — 예: 애플/테슬라/엔비디아
    us = _us_match(q)
    if us:
        return [("US", t, name) for t, name in us]

    # 3) 네이버가 못 찾을 때만 KRX 전체목록 검색(정식명·오타 대응)
    idx_hits = _kr_search_index(q)
    if idx_hits:
        return _dedup(idx_hits)

    # 4) 한글이 포함됐는데 국내·미국목록에서 못 찾으면 미국행 금지(오조회 방지)
    if re.search(r"[가-힣]", q):
        return []
    return [("US", q.upper(), None)]


def resolve(query: str):
    """사용자 입력 -> (market, code, display_name). 없으면 None."""
    cands = search(query)
    return cands[0] if cands else None


# ----------------------------- 한국 종목 조회 -----------------------------
def _fetch_kr(code: str) -> dict:
    url = f"https://m.stock.naver.com/api/stock/{code}/integration"
    r = requests.get(url, headers=_HEADERS, timeout=8)
    r.raise_for_status()
    d = r.json()

    infos = {i.get("code"): i.get("value") for i in d.get("totalInfos", [])}

    price = _to_float(infos.get("lastClosePrice"))
    # 현재가: dealTrendInfos 최신 종가가 있으면 사용
    try:
        trend = d.get("dealTrendInfos") or []
        if trend:
            price = _to_float(trend[0].get("closePrice")) or price
    except Exception:
        pass

    eps = _to_float(infos.get("eps"))
    bps = _to_float(infos.get("bps"))
    roe = (eps / bps * 100.0) if (eps and bps) else None  # 추정 ROE = EPS/BPS

    cons = d.get("consensusInfo") or {}

    return {
        "market": "KR",
        "code": code,
        "name": d.get("stockName") or code,
        "currency": "KRW",
        "price": price,
        "prev_close": _to_float(infos.get("lastClosePrice")),
        "per": _to_float(infos.get("per")),
        "forward_per": _to_float(infos.get("cnsPer")),   # 추정 PER
        "eps": eps,
        "pbr": _to_float(infos.get("pbr")),
        "bps": bps,
        "roe": roe,
        "roe_estimated": True,
        "div_yield": _to_float(infos.get("dividendYieldRatio")),
        "market_cap": _parse_kr_money(infos.get("marketValue")),
        "wk52_high": _to_float(infos.get("highPriceOf52Weeks")),
        "wk52_low": _to_float(infos.get("lowPriceOf52Weeks")),
        "sector": None,
        # --- 상세 데이터셋(선택 표시) ---
        "open": _to_float(infos.get("openPrice")),
        "high": _to_float(infos.get("highPrice")),
        "low": _to_float(infos.get("lowPrice")),
        "volume": _to_float(infos.get("accumulatedTradingVolume")),
        "trading_value": infos.get("accumulatedTradingValue"),  # "12조 3,719억" 형태
        "foreign_rate": infos.get("foreignRate"),               # "46.61%"
        "cns_eps": _to_float(infos.get("cnsEps")),              # 추정 EPS
        "dps": _to_float(infos.get("dividend")),               # 주당배당금
        "target_price": _to_float(cons.get("priceTargetMean")),  # 목표주가 컨센서스
        "recomm_mean": _to_float(cons.get("recommMean")),      # 투자의견(1~5, 높을수록 매수)
        "recomm_scale": "kr",
    }


# ----------------------------- 미국 종목 조회 -----------------------------
def _fetch_us(ticker: str) -> dict:
    import yfinance as yf
    info = yf.Ticker(ticker).info or {}
    if not info.get("regularMarketPrice") and not info.get("currentPrice") \
            and not info.get("longName") and not info.get("shortName"):
        raise ValueError(f"'{ticker}' 종목 정보를 찾을 수 없습니다.")

    roe = info.get("returnOnEquity")
    roe = roe * 100.0 if roe is not None else None  # yfinance는 소수(0.14) -> %

    def _pct(v):
        return v * 100.0 if v is not None else None

    return {
        "market": "US",
        "code": ticker,
        "name": info.get("longName") or info.get("shortName") or ticker,
        "currency": info.get("currency") or "USD",
        "price": info.get("currentPrice") or info.get("regularMarketPrice"),
        "prev_close": info.get("previousClose"),
        "per": info.get("trailingPE"),
        "forward_per": info.get("forwardPE"),
        "eps": info.get("trailingEps"),
        "pbr": info.get("priceToBook"),
        "bps": info.get("bookValue"),
        "roe": roe,
        "roe_estimated": False,
        "div_yield": info.get("dividendYield"),  # 최신 yfinance는 이미 % 단위
        "market_cap": info.get("marketCap"),
        "wk52_high": info.get("fiftyTwoWeekHigh"),
        "wk52_low": info.get("fiftyTwoWeekLow"),
        "sector": info.get("sector"),
        # --- 상세 데이터셋(선택 표시) ---
        "open": info.get("open") or info.get("regularMarketOpen"),
        "high": info.get("dayHigh"),
        "low": info.get("dayLow"),
        "volume": info.get("volume") or info.get("regularMarketVolume"),
        "avg_volume": info.get("averageVolume"),
        "ma50": info.get("fiftyDayAverage"),
        "ma200": info.get("twoHundredDayAverage"),
        "target_price": info.get("targetMeanPrice"),
        "recomm_mean": info.get("recommendationMean"),  # 1~5, 낮을수록 매수
        "recomm_key": info.get("recommendationKey"),
        "recomm_scale": "us",
        "n_analysts": info.get("numberOfAnalystOpinions"),
        "profit_margin": _pct(info.get("profitMargins")),
        "operating_margin": _pct(info.get("operatingMargins")),
        "revenue_growth": _pct(info.get("revenueGrowth")),
        "earnings_growth": _pct(info.get("earningsGrowth")),
        "roa": _pct(info.get("returnOnAssets")),
        "debt_to_equity": info.get("debtToEquity"),
        "current_ratio": info.get("currentRatio"),
        "total_cash": info.get("totalCash"),
        "total_debt": info.get("totalDebt"),
        "fcf": info.get("freeCashflow"),
        "ebitda": info.get("ebitda"),
        "beta": info.get("beta"),
        "peg": info.get("trailingPegRatio"),
        "psr": info.get("priceToSalesTrailing12Months"),
        "payout_ratio": _pct(info.get("payoutRatio")),
        "inst_held": _pct(info.get("heldPercentInstitutions")),
        "shares_out": info.get("sharesOutstanding"),
        "industry": info.get("industry"),
        "employees": info.get("fullTimeEmployees"),
    }


# ----------------------------- 공개 API -----------------------------
def get_quote(query: str) -> dict:
    """종목명/코드/티커로 정규화된 시세+재무 dict 반환. 실패 시 예외."""
    resolved = resolve(query)
    if not resolved:
        raise ValueError(f"'{query}' 종목을 찾지 못했습니다. "
                         f"정확한 종목명이나 코드(예: 현대차, 005380)로 다시 시도하세요.")
    market, code, name = resolved

    key = f"{market}:{code}"
    now = time.time()
    if key in _CACHE and now - _CACHE[key][0] < _CACHE_TTL:
        return _CACHE[key][1]

    if market == "KR":
        q = _fetch_kr(code)
    else:
        q = _fetch_us(code)
    if name and (not q.get("name") or q["name"] == code):
        q["name"] = name

    _CACHE[key] = (now, q)
    return q


def quote_for(market: str, code: str) -> dict:
    """시장을 아는 경우 재추정 없이 바로 조회(영숫자 ETF코드 안전)."""
    return _fetch_kr(code) if market == "KR" else _fetch_us(code)


# ----------------------------- 스크리너용 종목 유니버스 -----------------------------
def kr_market_leaders(market: str = "ALL", n: int = 200):
    """시총 상위 종목 [(code, name), ...]. market: 'KOSPI'/'KOSDAQ'/'ALL'."""
    markets = ["KOSPI", "KOSDAQ"] if market == "ALL" else [market]
    out = []
    for m in markets:
        got, page = 0, 1
        while got < n and page <= 10:
            try:
                r = requests.get(
                    f"https://m.stock.naver.com/api/stocks/marketValue/{m}"
                    f"?page={page}&pageSize=100", headers=_HEADERS, timeout=8)
                stocks = r.json().get("stocks", [])
            except Exception:
                break
            if not stocks:
                break
            for s in stocks:
                code = s.get("itemCode")
                if code:
                    out.append((str(code), s.get("stockName") or code))
                    got += 1
                    if got >= n:
                        break
            page += 1
    return out


# 지수 심볼 맵: 표시이름 -> (FDR심볼, 통화)
_INDEX_SYMBOLS = {
    "KOSPI": ("KS11", "KRW"), "KOSDAQ": ("KQ11", "KRW"),
    "S&P500": ("US500", "USD"), "나스닥": ("IXIC", "USD"),
    "다우": ("DJI", "USD"),
}
_INDEX_NAMES = {
    "KOSPI": "코스피", "KOSDAQ": "코스닥",
    "S&P500": "S&P 500", "나스닥": "나스닥 종합", "다우": "다우존스 산업평균",
}


def index_history(index="KOSPI", period="1년"):
    """지수 종가 시계열 -> [(date, close), ...]. 국내(코스피/코스닥)+미국(S&P500/나스닥/다우). (FDR)"""
    import FinanceDataReader as fdr
    from datetime import datetime, timedelta
    days = {"3개월": 100, "1년": 400, "3년": 1150, "5년": 1900}.get(period, 400)
    symbol = _INDEX_SYMBOLS.get(index, ("KS11", "KRW"))[0]
    if period == "전체":
        df = fdr.DataReader(symbol)
    else:
        start = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
        df = fdr.DataReader(symbol, start)
    out = []
    for idx, row in df.iterrows():
        try:
            v = float(row["Close"])
            if v != v or v <= 0:   # NaN(오늘 미체결 등)·비정상 값 제외
                continue
            out.append((idx.strftime("%Y-%m-%d"), v))
        except Exception:
            pass
    return out


def index_valuation(index="KOSPI"):
    """지수 현재값 + PER/PBR/배당(스냅샷). EPS = 지수 ÷ PER 로 산출.
    미국 지수는 무료 PER/EPS 스냅샷이 없어 이름만 반환(추이는 index_history로)."""
    if index in ("S&P500", "나스닥", "다우"):
        return {"name": _INDEX_NAMES.get(index, index), "price": None,
                "per": None, "pbr": None, "div": None, "eps": None}
    code = "KOSPI" if index == "KOSPI" else "KOSDAQ"
    r = requests.get(f"https://m.stock.naver.com/api/index/{code}/integration",
                     headers=_HEADERS, timeout=8)
    d = r.json()
    infos = {i.get("code"): i.get("value") for i in d.get("totalInfos", [])}
    price = _to_float(infos.get("lastClosePrice")) or _to_float(infos.get("closePrice"))
    per = _to_float(infos.get("per"))
    pbr = _to_float(infos.get("pbr"))
    div = _to_float(infos.get("dividendYieldRatio"))
    eps = (price / per) if (price and per) else None
    return {"name": d.get("stockName") or code, "price": price,
            "per": per, "pbr": pbr, "div": div, "eps": eps}


def kr_sectors():
    """네이버 업종(섹터) 목록 [(no, name, count), ...]."""
    try:
        r = requests.get("https://m.stock.naver.com/api/stocks/industry",
                         headers=_HEADERS, timeout=8)
        return [(g["no"], g["name"], g.get("totalCount", 0))
                for g in r.json().get("groups", [])]
    except Exception:
        return []


def kr_sector_stocks(no, n: int = 200):
    """특정 업종의 종목 [(code, name), ...]."""
    try:
        r = requests.get(
            f"https://m.stock.naver.com/api/stocks/industry/{no}"
            f"?page=1&pageSize={n}", headers=_HEADERS, timeout=8)
        return [(str(s["itemCode"]), s.get("stockName") or s["itemCode"])
                for s in r.json().get("stocks", []) if s.get("itemCode")]
    except Exception:
        return []


def screen_fetch(codes, progress=None):
    """코드 리스트의 재무지표를 모아 반환. progress(done, total) 콜백 선택."""
    out = []
    total = len(codes)
    for i, code in enumerate(codes):
        try:
            q = _fetch_kr(code)
            out.append({
                "market": "KR", "code": code, "name": q.get("name") or code,
                "price": q.get("price"), "per": q.get("per"),
                "pbr": q.get("pbr"), "roe": q.get("roe"),
                "div": q.get("div_yield"), "market_cap": q.get("market_cap"),
            })
        except Exception:
            pass
        if progress:
            progress(i + 1, total)
    return out


# 미국 스크리너용 티커 분류(ETF는 재무지표가 없을 수 있음)
_US_ETF_TICKERS = {"SPY", "VOO", "QQQ", "SCHD", "DIA", "VT", "JEPI"}


def us_universe(kind="전체"):
    """미국 스크리너 유니버스 [(ticker, name, is_etf), ...]. kind: '전체'/'주식'/'ETF'."""
    out = []
    for t, name, _ in _US_STOCKS:
        is_etf = t in _US_ETF_TICKERS
        if kind == "주식" and is_etf:
            continue
        if kind == "ETF" and not is_etf:
            continue
        out.append((t, name, is_etf))
    return out


def screen_fetch_us(tickers, progress=None):
    """미국 티커 리스트의 재무지표를 모아 반환. 시총은 원화(KRW)로 환산해 국내와 동일 필터 사용."""
    try:
        usd = fx_to_krw("USD")
    except Exception:
        usd = 1300.0
    out = []
    total = len(tickers)
    for i, t in enumerate(tickers):
        try:
            q = _fetch_us(t)
            cap = q.get("market_cap")
            out.append({
                "market": "US", "code": t, "name": q.get("name") or t,
                "currency": q.get("currency") or "USD",
                "is_etf": t in _US_ETF_TICKERS,
                "price": q.get("price"), "per": q.get("per"),
                "pbr": q.get("pbr"), "roe": q.get("roe"),
                "div": q.get("div_yield"),
                "market_cap": (cap * usd) if cap else None,   # 원화 환산
            })
        except Exception:
            pass
        if progress:
            progress(i + 1, total)
    return out


def get_price(market: str, code: str):
    """포트폴리오 평가용 현재가만 빠르게."""
    try:
        q = get_quote(code if market == "US" else code)
        return q.get("price")
    except Exception:
        return None


def live_price(market: str, code: str) -> dict:
    """실시간(준실시간) 현재가/등락. KR=네이버 폴링(지연 0), US=yfinance(약 15분 지연)."""
    if market == "KR":
        r = requests.get(
            f"https://polling.finance.naver.com/api/realtime/domestic/stock/{code}",
            headers=_HEADERS, timeout=6)
        d = r.json()["datas"][0]
        price = _to_float(d.get("closePriceRaw")) or _to_float(d.get("closePrice"))
        change = _to_float(d.get("compareToPreviousClosePriceRaw")) or \
            _to_float(d.get("compareToPreviousClosePrice")) or 0.0
        pct = _to_float(d.get("fluctuationsRatioRaw")) or \
            _to_float(d.get("fluctuationsRatio")) or 0.0
        return {"name": d.get("stockName") or code, "price": price,
                "change": change, "change_pct": pct,
                "prev_close": (price - change) if price is not None else None,
                "currency": "KRW", "market_status": d.get("marketStatus")}
    else:
        import yfinance as yf
        fi = yf.Ticker(code).fast_info
        def g(*keys):
            for k in keys:
                try:
                    v = fi[k]
                except Exception:
                    v = None
                if v:
                    return float(v)
            return None
        price = g("lastPrice", "last_price")
        prev = g("previousClose", "previous_close", "regularMarketPreviousClose")
        change = (price - prev) if (price and prev) else 0.0
        pct = (change / prev * 100) if prev else 0.0
        return {"name": code, "price": price, "change": change, "change_pct": pct,
                "prev_close": prev, "currency": "USD", "market_status": None}


def intraday(market: str, code: str):
    """당일 분봉 시계열 -> [(ts 'YYYYMMDDHHMMSS', price), ...]."""
    pts = []
    if market == "KR":
        r = requests.get(
            f"https://api.stock.naver.com/chart/domestic/item/{code}/minute?minuteUnit=5",
            headers=_HEADERS, timeout=8)
        for it in r.json():
            ts, pr = it.get("localDateTime"), it.get("currentPrice")
            if ts and pr is not None:
                pts.append((str(ts), float(pr)))
    else:
        import yfinance as yf
        h = yf.Ticker(code).history(period="1d", interval="1m")
        for idx, row in h.iterrows():
            try:
                pts.append((idx.strftime("%Y%m%d%H%M%S"), float(row["Close"])))
            except Exception:
                pass
    return pts


# 미국 지수 yfinance 티커
_INDEX_YF = {"S&P500": "^GSPC", "나스닥": "^IXIC", "다우": "^DJI"}


def index_intraday(index="KOSPI"):
    """지수 당일 분봉 -> [(ts 'YYYYMMDDHHMMSS', value), ...]. KR=네이버 지수분봉, US=yfinance."""
    pts = []
    if index in ("KOSPI", "KOSDAQ"):
        r = requests.get(
            f"https://api.stock.naver.com/chart/domestic/index/{index}/minute?minuteUnit=5",
            headers=_HEADERS, timeout=8)
        for it in r.json():
            ts, pr = it.get("localDateTime"), it.get("currentPrice")
            if ts and pr is not None:
                pts.append((str(ts), float(pr)))
    else:
        import yfinance as yf
        h = yf.Ticker(_INDEX_YF.get(index, "^GSPC")).history(
            period="1d", interval="1m")
        for idx, row in h.iterrows():
            try:
                c = float(row["Close"])
                if c == c:
                    pts.append((idx.strftime("%Y%m%d%H%M%S"), c))
            except Exception:
                pass
    return pts


def index_live(index="KOSPI"):
    """지수 현재값/등락 -> {name, price, change, change_pct, prev_close}."""
    name = _INDEX_NAMES.get(index, index)
    if index in ("KOSPI", "KOSDAQ"):
        r = requests.get(
            f"https://m.stock.naver.com/api/index/{index}/price?pageSize=2",
            headers=_HEADERS, timeout=8)
        d = r.json()[0]
        price = _to_float(d.get("closePrice"))
        chg = abs(_to_float(d.get("compareToPreviousClosePrice")) or 0.0)
        pct = abs(_to_float(d.get("fluctuationsRatio")) or 0.0)
        if (d.get("compareToPreviousPrice") or {}).get("name") == "FALLING":
            chg, pct = -chg, -pct
        prev = (price - chg) if price is not None else None
        return {"name": name, "price": price, "change": chg,
                "change_pct": pct, "prev_close": prev}
    import yfinance as yf
    fi = yf.Ticker(_INDEX_YF.get(index, "^GSPC")).fast_info

    def g(*keys):
        for k in keys:
            try:
                v = fi[k]
            except Exception:
                v = None
            if v:
                return float(v)
        return None
    price = g("lastPrice", "last_price")
    prev = g("previousClose", "previous_close", "regularMarketPreviousClose")
    chg = (price - prev) if (price and prev) else 0.0
    pct = (chg / prev * 100) if prev else 0.0
    return {"name": name, "price": price, "change": chg,
            "change_pct": pct, "prev_close": prev}


# ----------------------------- 원자재 / 시장 대시보드 -----------------------------
# 주요 지수(그리드용). key 는 index_* 함수가 받는 이름.
MARKET_INDICES = [
    ("코스피", "KOSPI"), ("코스닥", "KOSDAQ"),
    ("S&P500", "S&P500"), ("나스닥", "나스닥"), ("다우", "다우"),
]
# 주요 원자재 선물(yfinance). 두바이유는 무료 피드가 없어 국제 벤치마크 브렌트로 대체.
COMMODITIES = [
    ("WTI 원유", "CL=F"), ("브렌트유", "BZ=F"), ("천연가스", "NG=F"),
    ("금", "GC=F"), ("은", "SI=F"), ("구리", "HG=F"),
    ("백금", "PL=F"), ("팔라듐", "PA=F"),
    ("옥수수", "ZC=F"), ("밀", "ZW=F"), ("대두", "ZS=F"),
    ("설탕", "SB=F"), ("커피", "KC=F"), ("코코아", "CC=F"), ("면화", "CT=F"),
]
_YF_PERIOD = {"1개월": "1mo", "3개월": "3mo", "1년": "1y",
              "3년": "3y", "5년": "5y", "전체": "max"}


def commodity_history(ticker, period="1년"):
    """원자재 종가 시계열 -> [(date, close), ...] (yfinance)."""
    import yfinance as yf
    h = yf.Ticker(ticker).history(period=_YF_PERIOD.get(period, "1y"),
                                  interval="1d")
    out = []
    for idx, row in h.iterrows():
        try:
            c = float(row["Close"])
            if c == c:
                out.append((idx.strftime("%Y-%m-%d"), c))
        except Exception:
            pass
    return out


def item_live(kind, key):
    """대시보드 아이템 현재값/등락. kind: 'index'|'commodity'."""
    if kind == "index":
        return index_live(key)
    return live_price("US", key)


def item_history(kind, key, period):
    """대시보드 아이템 기간 종가 시계열 -> [(date, close), ...]."""
    if kind == "index":
        return index_history(key, period)
    return commodity_history(key, period)


def item_intraday(kind, key):
    """대시보드 아이템 당일 분봉 -> [(ts, value), ...]."""
    if kind == "index":
        return index_intraday(key)
    return intraday("US", key)


def history(market: str, code: str, period: str):
    """일봉 OHLC 시계열. period: '1주'/'1개월'/'3개월'/'1년'/'3년'/'5년'/'전체'. KR·US 모두 FDR."""
    import FinanceDataReader as fdr
    from datetime import datetime, timedelta
    days = {"1주": 12, "1개월": 40, "3개월": 110, "1년": 400,
            "3년": 1150, "5년": 1900}.get(period, 40)
    if period == "전체":
        df = fdr.DataReader(code)        # 상장 이후 전체
    else:
        start = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
        df = fdr.DataReader(code, start)
    out = []
    for idx, row in df.iterrows():
        try:
            c = float(row["Close"])
            if c != c or c <= 0:          # NaN(오늘 미체결)·이상치 제외
                continue
            out.append({
                "date": idx.strftime("%Y-%m-%d"),
                "open": float(row["Open"]), "high": float(row["High"]),
                "low": float(row["Low"]), "close": c,
            })
        except Exception:
            pass
    return out


# 지원 통화(원화 환산) — UI 드롭다운과 동일 순서
CURRENCIES = ["KRW", "USD", "JPY", "EUR", "CNY", "GBP"]
_FX_FALLBACK = {"USD": 1400.0, "JPY": 9.0, "EUR": 1600.0, "CNY": 195.0, "GBP": 1800.0}
_FX_CACHE = {}   # cur -> (t, rate)


def fx_to_krw(cur: str) -> float:
    """1 <통화> = ? KRW. KRW=1. 실패 시 대략적 기본값."""
    cur = (cur or "KRW").upper()
    if cur == "KRW":
        return 1.0
    now = time.time()
    hit = _FX_CACHE.get(cur)
    if hit and now - hit[0] < 300:
        return hit[1]
    rate = None
    try:
        import yfinance as yf
        fi = yf.Ticker(f"{cur}KRW=X").fast_info
        for k in ("lastPrice", "last_price"):
            try:
                v = fi[k]
            except Exception:
                v = None
            if v:
                rate = float(v)
                break
    except Exception:
        pass
    if not rate:
        rate = _FX_FALLBACK.get(cur, 1.0)
    _FX_CACHE[cur] = (now, rate)
    return rate


def fx_rates(currencies) -> dict:
    """여러 통화의 원화 환율을 한 번에."""
    return {c: fx_to_krw(c) for c in set(currencies)}


def usdkrw() -> float:
    """USD -> KRW (하위 호환)."""
    return fx_to_krw("USD")


if __name__ == "__main__":
    for q in ("삼성전자", "005930", "AAPL", "카카오", "TSLA"):
        try:
            d = get_quote(q)
            print(f"{q:8} -> {d['name']} ({d['market']}) price={d['price']} "
                  f"PER={d['per']} PBR={d['pbr']} ROE={d['roe']} DIV={d['div_yield']}")
        except Exception as e:
            print(f"{q:8} -> ERROR {e!r}")
    print("USD/KRW:", usdkrw())
