"""기술적 지표 계산(순수 함수). 입력은 종가/고가/저가 리스트.

부족한 구간은 None으로 채워 원본과 길이를 맞춘다(차트에서 정렬 편하게).
초보자 참고용 — 매매신호가 아님.
"""
from collections import deque


def sma(vals, n):
    """단순이동평균."""
    out = [None] * len(vals)
    q = deque()
    s = 0.0
    for i, v in enumerate(vals):
        q.append(v); s += v
        if len(q) > n:
            s -= q.popleft()
        if len(q) == n:
            out[i] = s / n
    return out


def ema(vals, n):
    """지수이동평균."""
    out = [None] * len(vals)
    k = 2.0 / (n + 1)
    e = None
    for i, v in enumerate(vals):
        e = v if e is None else v * k + e * (1 - k)
        out[i] = e
    return out


def bollinger(closes, n=20, k=2.0):
    """볼린저밴드 -> (중심(SMA), 상단, 하단)."""
    mid = sma(closes, n)
    up = [None] * len(closes)
    lo = [None] * len(closes)
    for i in range(len(closes)):
        if mid[i] is not None:
            w = closes[i - n + 1:i + 1]
            m = mid[i]
            sd = (sum((x - m) ** 2 for x in w) / n) ** 0.5
            up[i] = m + k * sd
            lo[i] = m - k * sd
    return mid, up, lo


def rsi(closes, n=14):
    """RSI(Wilder). 70이상 과매수 / 30이하 과매도."""
    out = [None] * len(closes)
    if len(closes) <= n:
        return out
    gains = losses = 0.0
    for i in range(1, n + 1):
        d = closes[i] - closes[i - 1]
        gains += max(d, 0.0)
        losses += max(-d, 0.0)
    ag = gains / n
    al = losses / n
    out[n] = 100.0 - 100.0 / (1.0 + (ag / al if al else float("inf")))
    for i in range(n + 1, len(closes)):
        d = closes[i] - closes[i - 1]
        ag = (ag * (n - 1) + max(d, 0.0)) / n
        al = (al * (n - 1) + max(-d, 0.0)) / n
        rs = ag / al if al else float("inf")
        out[i] = 100.0 - 100.0 / (1.0 + rs)
    return out


def macd(closes, fast=12, slow=26, sig=9):
    """MACD -> (MACD선, 시그널, 히스토그램)."""
    ef = ema(closes, fast)
    es = ema(closes, slow)
    line = [(a - b) if (a is not None and b is not None) else None
            for a, b in zip(ef, es)]
    vals = [x for x in line if x is not None]
    esig = ema(vals, sig)
    sigl = [None] * len(line)
    j = 0
    for i in range(len(line)):
        if line[i] is not None:
            sigl[i] = esig[j]; j += 1
    hist = [(l - s) if (l is not None and s is not None) else None
            for l, s in zip(line, sigl)]
    return line, sigl, hist


def _period_hl_mid(highs, lows, i, period):
    if i - period + 1 < 0:
        return None
    return (max(highs[i - period + 1:i + 1]) + min(lows[i - period + 1:i + 1])) / 2


def ichimoku(highs, lows, closes, conv=9, base=26, span_b=52):
    """일목균형표 -> dict(tenkan, kijun, spanA, spanB, chikou, shift).
    spanA/B는 base(26)만큼 앞으로 이동해 그려야 하며(선행스팬=구름),
    chikou(후행스팬)는 base만큼 뒤로 이동. shift=base 로 반환."""
    n = len(closes)
    tenkan = [_period_hl_mid(highs, lows, i, conv) for i in range(n)]
    kijun = [_period_hl_mid(highs, lows, i, base) for i in range(n)]
    spanA = [((tenkan[i] + kijun[i]) / 2)
             if (tenkan[i] is not None and kijun[i] is not None) else None
             for i in range(n)]
    spanB = [_period_hl_mid(highs, lows, i, span_b) for i in range(n)]
    chikou = list(closes)
    return {"tenkan": tenkan, "kijun": kijun, "spanA": spanA,
            "spanB": spanB, "chikou": chikou, "shift": base}
