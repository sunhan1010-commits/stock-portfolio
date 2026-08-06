"""포트폴리오 분석용 순수 계산 함수(네트워크·GUI 없음).

값 리스트/시계열을 받아 배분·집중도·리스크·상관 등을 계산한다.
초보자 참고용 지표이며 투자권유가 아님.
"""
import math


# ----------------------------- 배분 / 집중도 -----------------------------
def weights(values):
    tot = sum(v for v in values if v) or 0.0
    if tot <= 0:
        return [0.0] * len(values)
    return [(v or 0.0) / tot for v in values]


def hhi(vals):
    """허핀달지수(집중도). sum(비중^2). 1=한 종목 몰빵, 낮을수록 분산."""
    w = weights(vals)
    return sum(x * x for x in w)


def effective_holdings(vals):
    """유효 종목 수 = 1/HHI (분산 정도를 '실질 종목 개수'로)."""
    h = hhi(vals)
    return (1.0 / h) if h > 0 else 0.0


def top_concentration(vals, n=3):
    """상위 n개 비중 합(%)."""
    w = sorted(weights(vals), reverse=True)
    return sum(w[:n]) * 100.0


def weighted_average(pairs):
    """[(weight_value, metric)] -> 가중평균(metric None·<=0 은 제외).
    weight_value 는 평가액 등 양수 가중치."""
    num = den = 0.0
    for wv, m in pairs:
        if m is None or wv is None or wv <= 0:
            continue
        num += wv * m
        den += wv
    return (num / den) if den > 0 else None


def group_sum(items, keyfn, valfn):
    """items 를 key 별로 valfn 합계 -> dict{key: sum} (내림차순 정렬된 list of (key,sum))."""
    acc = {}
    for it in items:
        k = keyfn(it)
        acc[k] = acc.get(k, 0.0) + (valfn(it) or 0.0)
    return sorted(acc.items(), key=lambda kv: kv[1], reverse=True)


# ----------------------------- 시계열 / 리스크 -----------------------------
def daily_returns(closes):
    """종가 리스트 -> 일간 수익률 리스트(길이 n-1)."""
    out = []
    for i in range(1, len(closes)):
        p0, p1 = closes[i - 1], closes[i]
        if p0:
            out.append(p1 / p0 - 1.0)
    return out


def _mean(xs):
    return sum(xs) / len(xs) if xs else 0.0


def _std(xs):
    if len(xs) < 2:
        return 0.0
    m = _mean(xs)
    return math.sqrt(sum((x - m) ** 2 for x in xs) / (len(xs) - 1))


def volatility(returns, periods=252):
    """연율화 변동성(%). returns=일간수익률."""
    return _std(returns) * math.sqrt(periods) * 100.0


def max_drawdown(series):
    """최대낙폭(%). series=가치(또는 가격) 시계열. 음수로 반환."""
    if not series:
        return 0.0
    peak = series[0]
    mdd = 0.0
    for v in series:
        if v > peak:
            peak = v
        if peak > 0:
            dd = v / peak - 1.0
            if dd < mdd:
                mdd = dd
    return mdd * 100.0


def sharpe(returns, rf_annual=0.0, periods=252):
    """샤프지수(연율화). 무위험수익률 기본 0."""
    if len(returns) < 2:
        return None
    rf_daily = rf_annual / periods
    ex = [r - rf_daily for r in returns]
    sd = _std(ex)
    if sd == 0:
        return None
    return (_mean(ex) / sd) * math.sqrt(periods)


def beta(asset_returns, market_returns):
    """벤치마크 대비 베타. 두 수익률은 같은 날짜로 정렬돼 있어야 함."""
    n = min(len(asset_returns), len(market_returns))
    if n < 2:
        return None
    a = asset_returns[-n:]
    m = market_returns[-n:]
    ma, mm = _mean(a), _mean(m)
    cov = sum((a[i] - ma) * (m[i] - mm) for i in range(n)) / (n - 1)
    var = sum((m[i] - mm) ** 2 for i in range(n)) / (n - 1)
    return (cov / var) if var else None


def correlation(a, b):
    """두 수익률 시계열의 피어슨 상관계수(-1~1)."""
    n = min(len(a), len(b))
    if n < 2:
        return None
    a = a[-n:]; b = b[-n:]
    ma, mb = _mean(a), _mean(b)
    sa = math.sqrt(sum((x - ma) ** 2 for x in a))
    sb = math.sqrt(sum((x - mb) ** 2 for x in b))
    if sa == 0 or sb == 0:
        return None
    cov = sum((a[i] - ma) * (b[i] - mb) for i in range(n))
    return cov / (sa * sb)


def align_by_date(series_map):
    """{name: [(date, close), ...]} -> (dates, {name: [close...] 정렬}).
    공통(교집합) 날짜만 사용."""
    if not series_map:
        return [], {}
    date_sets = [set(d for d, _ in s) for s in series_map.values()]
    common = set.intersection(*date_sets) if date_sets else set()
    dates = sorted(common)
    out = {}
    for name, s in series_map.items():
        m = dict(s)
        out[name] = [m[d] for d in dates]
    return dates, out
