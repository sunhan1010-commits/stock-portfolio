"""
초보자용 지표 평가.
각 재무지표를 '통상적인 기준'으로 4단계 신호로 분류한다.
  level: "good"(저평가/우수) / "ok"(적정) / "warn"(다소 높음/보통) / "bad"(고평가/주의) / "na"
주의: 아래 기준은 업종/성장성을 무시한 매우 단순한 일반 가이드다.
성장주(IT 등)는 PER/PBR이 높은 것이 정상일 수 있어 참고용으로만 사용할 것.
"""
from __future__ import annotations

COLORS = {
    "good": "#1a7f37",   # 초록
    "ok":   "#1f6feb",   # 파랑
    "warn": "#bf8700",   # 주황
    "bad":  "#cf222e",   # 빨강
    "na":   "#8b949e",   # 회색
}

LABELS_KO = {
    "good": "저평가/우수",
    "ok": "적정",
    "warn": "다소 높음",
    "bad": "주의",
    "na": "정보없음",
}


# 초보자용 지표 사전 (종목분석 탭 'ⓘ 지표 설명'에서 사용)
GLOSSARY = [
    {"name": "PER (주가수익비율)",
     "desc": "주가를 주당순이익(EPS)으로 나눈 값. '지금 이익 기준으로 원금 회수에 몇 년'인가를 뜻해요. 낮을수록 이익 대비 주가가 쌉니다.",
     "guide": "10 미만 저평가 · 10~20 적정 · 20~35 다소 높음 · 35 초과 주의. 단, 성장주는 높은 PER이 정상일 수 있어요. 적자면 PER은 의미가 없습니다."},
    {"name": "추정 PER (Forward PER)",
     "desc": "현재 주가를 '향후 12개월 예상 EPS'로 나눈 값. 현재(실적) PER이 과거 이익 기준이라면, 추정 PER은 앞으로의 예상 이익 기준이에요.",
     "guide": "추정 PER이 현재 PER보다 낮으면 이익 성장 기대(주가가 미래 이익 대비 싸짐), 높으면 이익 둔화 우려. 애널리스트 추정치라 실제와 다를 수 있어 참고용입니다."},
    {"name": "PBR (주가순자산비율)",
     "desc": "주가를 주당순자산(BPS, 회사가 가진 순재산)으로 나눈 값. 회사 장부상 가치 대비 주가 배수예요.",
     "guide": "1 미만이면 순자산보다 싸게 거래(저평가) · 1~2 적정 · 2~4 다소 높음 · 4 초과 주의."},
    {"name": "ROE (자기자본이익률)",
     "desc": "순이익을 자기자본으로 나눈 값(%). 주주 돈으로 얼마나 효율적으로 이익을 내는지를 봐요. 높을수록 좋습니다.",
     "guide": "15% 이상 우수 · 10~15% 양호 · 5~10% 보통 · 5% 미만 낮음. (한국 종목은 EPS/BPS로 추정한 값)"},
    {"name": "배당수익률",
     "desc": "1년간 받는 배당금을 주가로 나눈 값(%). 주식을 들고 있을 때 배당으로 얻는 연 수익률이에요.",
     "guide": "4% 이상 고배당 · 2~4% 양호 · 2% 미만 낮음 · 0% 무배당(성장 재투자형일 수 있음)."},
    {"name": "52주 위치",
     "desc": "최근 1년 최저가~최고가 사이에서 현재가가 어디쯤인지(%). 0%는 1년 최저, 100%는 1년 최고예요.",
     "guide": "낮을수록 상대적 저점. 다만 계속 하락 중인 종목도 낮게 나오니 이유를 함께 보세요."},
    {"name": "EPS / BPS",
     "desc": "EPS=주당순이익(1주가 버는 이익), BPS=주당순자산(1주에 담긴 순재산). PER·PBR을 계산하는 재료예요.",
     "guide": "EPS가 꾸준히 늘면 좋은 신호. 주가 ÷ EPS = PER, 주가 ÷ BPS = PBR."},
    {"name": "시가총액",
     "desc": "주가 × 총 주식수. 회사의 전체 몸값이에요. 클수록 대형·안정적, 작을수록 변동성이 큰 편입니다.",
     "guide": "초보자는 대형주(시총 상위)가 상대적으로 안전한 편입니다."},
]


def _band(value, bands, na_when_none=True):
    """
    bands: [(upper_bound, level, text), ...] 오름차순. 마지막은 upper_bound=None.
    value가 upper_bound 미만이면 해당 level 채택.
    """
    if value is None:
        return ("na", "정보 없음")
    for upper, level, text in bands:
        if upper is None or value < upper:
            return (level, text)
    return ("na", "정보 없음")


def eval_per(per):
    if per is not None and per <= 0:
        return {"key": "PER", "value": per, "level": "na",
                "text": "적자(EPS<0) — PER 무의미",
                "help": "순이익이 적자라 PER로 평가할 수 없습니다."}
    level, text = _band(per, [
        (10, "good", "저평가 구간 (10배 미만)"),
        (20, "ok",   "적정 구간 (10~20배)"),
        (35, "warn", "다소 높음 (20~35배)"),
        (None, "bad", "고평가 주의 (35배 초과)"),
    ])
    return {"key": "PER", "value": per, "level": level, "text": text,
            "help": ("PER = 주가 ÷ 주당순이익(EPS). 이익 대비 주가가 몇 배인지.\n"
                     "낮을수록 저렴. 통상 10 미만 저평가, 10~20 적정, 20↑ 부담.\n"
                     "단, 성장주는 높은 PER이 정상일 수 있습니다.")}


def eval_forward_per(fper, per=None):
    if fper is None:
        return {"key": "추정 PER", "value": None, "level": "na", "text": "정보 없음",
                "help": "추정 PER(Forward PER) = 주가 ÷ 향후 12개월 예상 EPS. "
                        "애널리스트 실적 추정 기반이라 종목에 따라 제공되지 않을 수 있습니다."}
    if fper <= 0:
        return {"key": "추정 PER", "value": fper, "level": "na",
                "text": "적자 예상 — 추정 PER 무의미",
                "help": "향후 예상 이익이 적자라 추정 PER로 평가하기 어렵습니다."}
    level, text = _band(fper, [
        (10, "good", "저평가 구간 (10배 미만)"),
        (20, "ok",   "적정 구간 (10~20배)"),
        (35, "warn", "다소 높음 (20~35배)"),
        (None, "bad", "고평가 주의 (35배 초과)"),
    ])
    trend = ""
    if per and per > 0:
        if fper < per * 0.97:
            trend = " · 향후 이익 개선 기대(현재 PER보다 낮음)"
        elif fper > per * 1.03:
            trend = " · 향후 이익 둔화 우려(현재 PER보다 높음)"
    return {"key": "추정 PER", "value": fper, "level": level, "text": text + trend,
            "help": ("추정 PER(Forward PER) = 주가 ÷ 향후 12개월 예상 EPS.\n"
                     "현재(실적) PER보다 낮으면 이익 성장 기대, 높으면 둔화 우려.\n"
                     "추정치라 실제와 다를 수 있으니 참고용으로만 보세요.")}


def eval_pbr(pbr):
    if pbr is not None and pbr <= 0:
        return {"key": "PBR", "value": pbr, "level": "na", "text": "자본잠식 등 이상치",
                "help": "PBR이 0 이하로 정상 평가가 어렵습니다."}
    level, text = _band(pbr, [
        (1, "good", "저평가 (순자산 이하, 1배 미만)"),
        (2, "ok",   "적정 (1~2배)"),
        (4, "warn", "다소 높음 (2~4배)"),
        (None, "bad", "고평가 주의 (4배 초과)"),
    ])
    return {"key": "PBR", "value": pbr, "level": level, "text": text,
            "help": ("PBR = 주가 ÷ 주당순자산(BPS). 회사 순자산 대비 주가 배수.\n"
                     "1배 미만이면 장부상 자산가치보다 싸게 거래. 통상 1~2 적정.")}


def eval_roe(roe):
    level, text = _band(roe, [
        (5,  "bad",  "낮음 (5% 미만)"),
        (10, "warn", "보통 (5~10%)"),
        (15, "ok",   "양호 (10~15%)"),
        (None, "good", "우수 (15% 이상)"),
    ])
    return {"key": "ROE", "value": roe, "level": level, "text": text,
            "help": ("ROE = 순이익 ÷ 자기자본. 자본을 얼마나 효율적으로 굴리는지.\n"
                     "높을수록 좋음. 통상 10% 이상 양호, 15% 이상 우수.\n"
                     "한국 종목은 EPS/BPS로 추정한 값입니다.")}


def eval_div(div):
    if div is None:
        return {"key": "배당수익률", "value": None, "level": "na", "text": "정보 없음",
                "help": "배당수익률 = 연간 배당금 ÷ 주가."}
    if div <= 0:
        return {"key": "배당수익률", "value": div, "level": "na", "text": "무배당",
                "help": "배당을 지급하지 않는 종목입니다(성장 재투자형일 수 있음)."}
    level, text = _band(div, [
        (2, "warn", "낮음 (2% 미만)"),
        (4, "ok",   "양호 (2~4%)"),
        (None, "good", "고배당 (4% 이상)"),
    ])
    return {"key": "배당수익률", "value": div, "level": level, "text": text,
            "help": ("배당수익률 = 연간 배당금 ÷ 주가.\n"
                     "통상 2~4% 양호, 4% 이상 고배당. 무배당 성장주도 많습니다.")}


def eval_52w(price, low, high):
    """현재가의 52주 밴드 내 위치(%)."""
    if not (price and low and high) or high <= low:
        return {"key": "52주 위치", "value": None, "level": "na", "text": "정보 없음",
                "help": "최근 1년 최고/최저 대비 현재가 위치."}
    pos = (price - low) / (high - low) * 100
    if pos < 25:
        level, text = "good", f"저점권 ({pos:.0f}%)"
    elif pos < 60:
        level, text = "ok", f"중간권 ({pos:.0f}%)"
    elif pos < 85:
        level, text = "warn", f"고점권 근접 ({pos:.0f}%)"
    else:
        level, text = "bad", f"52주 고점권 ({pos:.0f}%)"
    return {"key": "52주 위치", "value": pos, "level": level, "text": text,
            "help": ("최근 1년 최저~최고 사이에서 현재가의 위치.\n"
                     "0%=52주 최저, 100%=52주 최고. 낮을수록 상대적 저점.")}


def evaluate(quote: dict) -> list[dict]:
    """quote(datasource.get_quote 결과) -> 지표 평가 리스트."""
    return [
        eval_per(quote.get("per")),
        eval_forward_per(quote.get("forward_per"), quote.get("per")),
        eval_pbr(quote.get("pbr")),
        eval_roe(quote.get("roe")),
        eval_div(quote.get("div_yield")),
        eval_52w(quote.get("price"), quote.get("wk52_low"), quote.get("wk52_high")),
    ]


# 투자성향별 위험자산(주식+ETF) 목표 비중 (target, low, high)
STYLES = {
    "나이 기준": None,   # 100 − 나이 규칙 사용
    "공격형(위험선호)": (65, 55, 75),
    "중립형": (50, 45, 55),
    "보수형(위험회피)": (25, 10, 30),
}


def recommend_allocation(age: int, current_risk_pct: float | None,
                         style: str = "나이 기준") -> dict:
    """
    위험자산(주식+ETF) 적정 비중 제안.
    - 투자성향(style)을 고르면 그 목표 밴드를, '나이 기준'이면 '100 − 나이' 규칙을 사용.
    current_risk_pct: 현재 주식+ETF 비중(%). None이면 판정 생략.
    """
    band = STYLES.get(style)
    if band:
        target, low, high = band
    else:
        target = max(10, min(90, 100 - age))
        low = max(0, target - 10)
        high = min(100, target + 5)

    res = {"age": age, "style": style, "target": target, "low": low, "high": high,
           "safe_target": 100 - target, "current": current_risk_pct}
    if current_risk_pct is None:
        res.update(level="na",
                   status="자산을 추가하면 현재 비중과 비교해 드립니다.",
                   advice="")
        return res

    c = current_risk_pct
    if c < low:
        res.update(level="ok", status="위험자산 비중이 권장보다 낮음 (보수적)",
                   advice=f"안정적입니다. 여력이 되면 주식·ETF를 조금 늘려 "
                          f"{low:.0f}~{high:.0f}% 범위를 향해 가도 좋습니다.")
    elif c <= high:
        res.update(level="good", status="권장 범위 내 (적정)",
                   advice="현재 위험자산 비중이 나이에 맞는 적정 범위입니다. 잘 배분돼 있어요.")
    elif c <= high + 15:
        res.update(level="warn", status="위험자산 비중이 권장보다 다소 높음",
                   advice=f"변동성이 클 수 있습니다. 적금·예금 등 안전자산을 늘려 "
                          f"주식·ETF를 {high:.0f}% 근처로 낮추는 것을 고려하세요.")
    else:
        res.update(level="bad", status="위험자산 비중이 과다",
                   advice=f"초보자에게는 위험합니다. 주식·ETF를 {target:.0f}% 수준까지 "
                          f"줄이고 안전자산 비중을 높이길 권합니다.")
    return res


def overall(evals: list[dict]) -> dict:
    """지표들을 종합해 대략적인 한 줄 총평(참고용)."""
    score = {"good": 2, "ok": 1, "warn": -1, "bad": -2, "na": 0}
    # 밸류에이션 핵심(PER, PBR) 위주로 가중
    weight = {"PER": 2, "PBR": 2, "ROE": 1, "배당수익률": 1, "52주 위치": 1,
              "추정 PER": 0}
    total = 0
    wsum = 0
    for e in evals:
        w = weight.get(e["key"], 1)
        total += score[e["level"]] * w
        if e["level"] != "na":
            wsum += w
    if wsum == 0:
        return {"level": "na", "text": "평가할 지표가 부족합니다."}
    avg = total / wsum
    if avg >= 1.2:
        return {"level": "good", "text": "지표상 저평가·우량 경향 (참고용)"}
    if avg >= 0.2:
        return {"level": "ok", "text": "지표상 대체로 적정 수준 (참고용)"}
    if avg >= -0.8:
        return {"level": "warn", "text": "지표상 다소 부담 있는 수준 (참고용)"}
    return {"level": "bad", "text": "지표상 고평가·주의 경향 (참고용)"}
