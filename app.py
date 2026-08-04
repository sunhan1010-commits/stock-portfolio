"""
주식 포트폴리오 매니저 (Windows 데스크톱)
- 종목 분석 탭: 종목명/코드/티커 입력 -> 재무지표 자동 조회 + 초보자용 신호등 평가
- 포트폴리오 탭: 적금/예금/주식/ETF/현금 관리, 평가금액·비중 자동 계산
데이터: 한국=네이버금융, 미국=yfinance (지연 시세, 참고용).
"""
from __future__ import annotations
import certs  # noqa: F401  SSL 환경변수 먼저 설정
import os
import re
import sys
import json
import traceback

from PySide6.QtCore import (
    Qt, Signal, QObject, QStringListModel, QTimer, QThreadPool, QRunnable, Slot,
    QDateTime, QPointF, QMargins,
)
from PySide6.QtGui import QFont, QColor, QPainter
from PySide6.QtCharts import (
    QChart, QChartView, QLineSeries, QValueAxis, QDateTimeAxis,
    QCandlestickSeries, QCandlestickSet, QBarCategoryAxis, QAreaSeries,
)
from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QTabWidget, QVBoxLayout, QHBoxLayout,
    QGridLayout, QLabel, QLineEdit, QPushButton, QTableWidget, QTableWidgetItem,
    QHeaderView, QComboBox, QDoubleSpinBox, QSpinBox, QDialog, QDialogButtonBox,
    QFormLayout, QMessageBox, QFrame, QSizePolicy, QAbstractItemView, QPlainTextEdit,
    QCompleter, QTextBrowser, QCheckBox, QSplitter, QScrollArea,
    QToolButton, QMenu, QTabBar, QInputDialog, QStackedWidget,
)

import datasource
import metrics
import indicators
import db
import version
import updater


# ============================ 공통 유틸 ============================
def fmt_money(v, currency="KRW"):
    if v is None:
        return "-"
    if currency == "USD":
        return f"${v:,.2f}"
    return f"{v:,.0f}원"


def fmt_num(v, suffix="", digits=2):
    if v is None:
        return "-"
    return f"{v:,.{digits}f}{suffix}"


def fmt_metric_value(key, value):
    if value is None:
        return "-"
    if key in ("PER", "PBR"):
        return f"{value:,.2f}배"
    if key in ("ROE", "배당수익률", "52주 위치"):
        return f"{value:,.2f}%"
    return f"{value:,.2f}"


# ============================ 백그라운드 작업 ============================
class _AsyncSignals(QObject):
    """
    메인 스레드에서 생성되는 시그널 브릿지.
    done/fail 시그널을 자기 자신의 슬롯에 연결하면, 워커 스레드에서 emit해도
    (수신자 affinity가 메인 스레드라) Qt가 QueuedConnection으로 처리해
    콜백이 반드시 '메인 스레드'에서 실행된다. (GUI 조작 안전)
    """
    done = Signal(object)
    fail = Signal(str)

    def __init__(self, parent, on_done, on_fail):
        super().__init__(parent)          # parent(메인스레드 위젯) 소속 -> GC 안전
        self._on_done, self._on_fail = on_done, on_fail
        self.done.connect(self._deliver_done)
        self.fail.connect(self._deliver_fail)

    @Slot(object)
    def _deliver_done(self, res):
        try:
            self._on_done(res)
        finally:
            self.deleteLater()

    @Slot(str)
    def _deliver_fail(self, msg):
        try:
            if self._on_fail:
                self._on_fail(msg)
        finally:
            self.deleteLater()


class _AsyncTask(QRunnable):
    def __init__(self, fn, args, kwargs, signals):
        super().__init__()
        self.fn, self.args, self.kwargs, self.signals = fn, args, kwargs, signals

    def run(self):
        try:
            result = self.fn(*self.args, **self.kwargs)
        except Exception as e:
            try:
                self.signals.fail.emit(f"{e}")
            except RuntimeError:
                pass   # 종료 등으로 시그널 객체가 이미 삭제된 경우
            return
        try:
            self.signals.done.emit(result)
        except RuntimeError:
            pass


def run_async(parent, fn, on_done, on_fail=None, *args, **kwargs):
    """fn을 스레드풀에서 실행하고 결과 콜백을 '메인 스레드'에서 호출."""
    signals = _AsyncSignals(parent, on_done, on_fail)
    QThreadPool.globalInstance().start(_AsyncTask(fn, args, kwargs, signals))


# ============================ 줌 가능한 차트뷰 ============================
class ZoomableChartView(QChartView):
    """마우스 휠=확대/축소, 왼쪽 드래그=박스 확대, 오른쪽 드래그=이동, 더블클릭=원래대로."""
    def __init__(self, chart=None, parent=None):
        if chart is not None:
            super().__init__(chart, parent)
        else:
            super().__init__(parent)
        self.setRenderHint(QPainter.Antialiasing)
        self.setRubberBand(QChartView.RectangleRubberBand)
        self._pan_last = None

    def wheelEvent(self, e):
        ch = self.chart()
        if ch is not None:
            ch.zoom(1.25 if e.angleDelta().y() > 0 else 0.8)
            e.accept()
        else:
            super().wheelEvent(e)

    def mouseDoubleClickEvent(self, e):
        ch = self.chart()
        if ch is not None:
            ch.zoomReset()
        super().mouseDoubleClickEvent(e)

    def mousePressEvent(self, e):
        if e.button() == Qt.RightButton:
            self._pan_last = e.position()
            self.setCursor(Qt.ClosedHandCursor)
            e.accept(); return
        super().mousePressEvent(e)

    def mouseMoveEvent(self, e):
        if self._pan_last is not None and self.chart() is not None:
            d = e.position() - self._pan_last
            self._pan_last = e.position()
            self.chart().scroll(-d.x(), d.y())
            e.accept(); return
        super().mouseMoveEvent(e)

    def mouseReleaseEvent(self, e):
        if e.button() == Qt.RightButton and self._pan_last is not None:
            self._pan_last = None
            self.unsetCursor()
            e.accept(); return
        super().mouseReleaseEvent(e)


class ChartZoomDialog(QDialog):
    """차트를 큰 창으로 옮겨 크게 보기(닫으면 원래 위치로 되돌림)."""
    def __init__(self, parent, chart, restore_view, title="차트 크게 보기"):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.resize(1000, 640)
        self._chart = chart
        self._restore_view = restore_view
        lay = QVBoxLayout(self)
        hint = QLabel("휠=확대/축소 · 왼쪽 드래그=박스 확대 · 오른쪽 드래그=이동 · 더블클릭=원래대로")
        hint.setStyleSheet("color:#8b949e; font-size:11px;")
        lay.addWidget(hint)
        self.view = ZoomableChartView(chart)   # chart 를 이 뷰로 이동
        lay.addWidget(self.view, 1)
        bb = QDialogButtonBox(QDialogButtonBox.Close)
        bb.button(QDialogButtonBox.Close).clicked.connect(self.accept)
        lay.addWidget(bb)

    def closeEvent(self, e):
        # 차트를 원래 뷰로 되돌린다
        try:
            self._restore_view.setChart(self._chart)
        except Exception:
            pass
        super().closeEvent(e)


# ============================ 지표 배지 위젯 ============================
class MetricCard(QFrame):
    def __init__(self, ev: dict):
        super().__init__()
        self.setFrameShape(QFrame.StyledPanel)
        color = metrics.COLORS[ev["level"]]
        self.setStyleSheet(
            f"MetricCard {{ border:1px solid #d0d7de; border-radius:8px; }}")
        lay = QVBoxLayout(self)
        lay.setContentsMargins(12, 10, 12, 10)
        lay.setSpacing(4)

        top = QLabel(ev["key"])
        top.setStyleSheet("color:#57606a; font-size:12px;")
        val = QLabel(fmt_metric_value(ev["key"], ev["value"]))
        val.setStyleSheet("font-size:20px; font-weight:700;")
        badge = QLabel(f"  {metrics.LABELS_KO[ev['level']]}  ")
        badge.setStyleSheet(
            f"background:{color}; color:white; border-radius:9px;"
            f"padding:2px 8px; font-size:11px; font-weight:600;")
        badge.setAlignment(Qt.AlignCenter)
        badge.setSizePolicy(QSizePolicy.Maximum, QSizePolicy.Maximum)
        desc = QLabel(ev["text"])
        desc.setStyleSheet(f"color:{color}; font-size:11px;")
        desc.setWordWrap(True)

        lay.addWidget(top)
        lay.addWidget(val)
        row = QHBoxLayout()
        row.addWidget(badge)
        row.addStretch()
        lay.addLayout(row)
        lay.addWidget(desc)

        self.setToolTip(ev["help"])


# ============================ 지표 설명 다이얼로그 ============================
class MetricsHelpDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("지표 설명 — 초보자용")
        self.resize(560, 560)
        lay = QVBoxLayout(self)
        intro = QLabel("각 지표가 무엇을 의미하고, 어느 정도가 통상적인지 정리했어요. "
                       "아래 기준은 업종·성장성을 배제한 <b>단순 일반 가이드</b>입니다.")
        intro.setWordWrap(True)
        intro.setTextFormat(Qt.RichText)
        lay.addWidget(intro)

        view = QTextBrowser()
        html = ["<style>h3{margin:14px 0 2px;} p{margin:2px 0 2px;color:#333;} "
                ".g{color:#1f6feb;}</style>"]
        for item in metrics.GLOSSARY:
            html.append(f"<h3>{item['name']}</h3>")
            html.append(f"<p>{item['desc']}</p>")
            html.append(f"<p class='g'>▸ 기준: {item['guide']}</p>")
        view.setHtml("\n".join(html))
        view.setOpenExternalLinks(False)
        lay.addWidget(view, 1)

        note = QLabel("※ 지표는 참고용이며 투자 판단·매매 권유가 아닙니다. "
                      "카드에 마우스를 올려도 같은 설명이 뜹니다.")
        note.setWordWrap(True)
        note.setStyleSheet("color:#8b949e; font-size:11px;")
        lay.addWidget(note)

        bb = QDialogButtonBox(QDialogButtonBox.Close)
        bb.rejected.connect(self.reject)
        bb.accepted.connect(self.accept)
        bb.button(QDialogButtonBox.Close).clicked.connect(self.accept)
        lay.addWidget(bb)


# ============================ 상세 데이터셋 ============================
def _fmt_big(v, cur):
    """큰 금액을 조/억(원) 또는 T/B/M($)로 축약."""
    if v is None:
        return None
    if cur == "USD":
        a = abs(v)
        if a >= 1e12:
            return f"${v/1e12:.2f}T"
        if a >= 1e9:
            return f"${v/1e9:.2f}B"
        if a >= 1e6:
            return f"${v/1e6:.1f}M"
        return f"${v:,.0f}"
    a = abs(v)
    if a >= 1e12:
        return f"{v/1e12:,.2f}조원"
    if a >= 1e8:
        return f"{v/1e8:,.0f}억원"
    return f"{v:,.0f}원"


def _opinion_text(mean, scale):
    """증권가 투자의견 평균 -> 사람이 읽는 등급."""
    if mean is None:
        return None
    if scale == "us":   # 1=강력매수 ~ 5=매도
        label = ("강력매수" if mean <= 1.5 else "매수" if mean <= 2.5 else
                 "중립" if mean <= 3.5 else "매도" if mean <= 4.5 else "강력매도")
    else:               # kr: 5=매수 우위 ~ 1=매도
        label = ("강력매수" if mean >= 4.5 else "매수" if mean >= 3.5 else
                 "중립" if mean >= 2.5 else "매도" if mean >= 1.5 else "강력매도")
    return f"{label} ({mean:.2f})"


def detail_sections(q):
    """quote dict -> [(섹션명, [(라벨, 값HTML), ...]), ...]. 값 없는 행/섹션은 생략."""
    cur = q.get("currency", "USD")
    money = lambda v: fmt_money(v, cur) if v is not None else None
    pct = lambda v: f"{v:,.2f}%" if v is not None else None
    ratio = lambda v, s="배": f"{v:,.2f}{s}" if v is not None else None
    intc = lambda v: f"{v:,.0f}" if v is not None else None

    # 목표주가 + 상승여력
    tgt = q.get("target_price")
    price = q.get("price")
    tgt_html = None
    if tgt:
        tgt_html = money(tgt)
        if price:
            up = (tgt - price) / price * 100
            col = "#cf222e" if up > 0 else ("#1f6feb" if up < 0 else "#57606a")
            tgt_html += f"  <span style='color:{col}'>({up:+.1f}% 여력)</span>"

    sections = [
        ("가격·거래", [
            ("시가", money(q.get("open"))),
            ("고가", money(q.get("high"))),
            ("저가", money(q.get("low"))),
            ("거래량", intc(q.get("volume"))),
            ("거래대금", q.get("trading_value")),
            ("평균거래량", intc(q.get("avg_volume"))),
            ("52주 최고", money(q.get("wk52_high"))),
            ("52주 최저", money(q.get("wk52_low"))),
            ("50일 이동평균", money(q.get("ma50"))),
            ("200일 이동평균", money(q.get("ma200"))),
        ]),
        ("밸류에이션", [
            ("PER", ratio(q.get("per"))),
            ("추정 PER(Forward)", ratio(q.get("forward_per"))),
            ("PBR", ratio(q.get("pbr"))),
            ("PSR(주가매출비율)", ratio(q.get("psr"))),
            ("PEG(성장 대비 PER)", ratio(q.get("peg"), "")),
            ("EPS(주당순이익)", money(q.get("eps"))),
            ("추정 EPS", money(q.get("cns_eps"))),
            ("BPS(주당순자산)", money(q.get("bps"))),
            ("시가총액", _fmt_big(q.get("market_cap"), cur)),
            ("EV 대비 EBITDA(EBITDA)", _fmt_big(q.get("ebitda"), cur)),
        ]),
        ("수익성·성장", [
            ("ROE(자기자본이익률)", pct(q.get("roe"))),
            ("ROA(총자산이익률)", pct(q.get("roa"))),
            ("순이익률", pct(q.get("profit_margin"))),
            ("영업이익률", pct(q.get("operating_margin"))),
            ("매출 성장률(YoY)", pct(q.get("revenue_growth"))),
            ("이익 성장률(YoY)", pct(q.get("earnings_growth"))),
        ]),
        ("재무 안정성", [
            ("부채비율(D/E)", ratio(q.get("debt_to_equity"), "%")),
            ("유동비율", ratio(q.get("current_ratio"), "")),
            ("총현금", _fmt_big(q.get("total_cash"), cur)),
            ("총부채", _fmt_big(q.get("total_debt"), cur)),
            ("잉여현금흐름(FCF)", _fmt_big(q.get("fcf"), cur)),
            ("베타(시장 민감도)", ratio(q.get("beta"), "")),
        ]),
        ("배당", [
            ("배당수익률", pct(q.get("div_yield"))),
            ("주당배당금(DPS)", money(q.get("dps"))),
            ("배당성향", pct(q.get("payout_ratio"))),
        ]),
        ("증권가 컨센서스", [
            ("목표주가", tgt_html),
            ("투자의견", _opinion_text(q.get("recomm_mean"), q.get("recomm_scale"))),
            ("분석가 수", intc(q.get("n_analysts"))),
            ("외국인 보유율", q.get("foreign_rate")),
            ("기관 보유율", pct(q.get("inst_held"))),
        ]),
        ("기타", [
            ("업종/산업", q.get("sector") or q.get("industry")),
            ("상장주식수", intc(q.get("shares_out"))),
            ("임직원 수", intc(q.get("employees"))),
        ]),
    ]
    out = []
    for title, rows in sections:
        kept = [(lbl, val) for lbl, val in rows if val]
        if kept:
            out.append((title, kept))
    return out


def detail_html(q):
    parts = ["<style>h3{margin:14px 0 4px;color:#0969da;} "
             "table{border-collapse:collapse;width:100%;} "
             "td{padding:3px 6px;border-bottom:1px solid #eee;} "
             ".l{color:#57606a;width:52%;} .v{font-weight:600;}</style>"]
    secs = detail_sections(q)
    if not secs:
        return "<p>표시할 상세 데이터가 없습니다.</p>"
    for title, rows in secs:
        parts.append(f"<h3>{title}</h3><table>")
        for lbl, val in rows:
            parts.append(f"<tr><td class='l'>{lbl}</td>"
                         f"<td class='v'>{val}</td></tr>")
        parts.append("</table>")
    return "\n".join(parts)


class DetailDialog(QDialog):
    """종목의 확장 재무·거래·컨센서스 데이터셋을 표로 보여준다."""
    def __init__(self, parent, q):
        super().__init__(parent)
        name = q.get("name", ""); code = q.get("code", "")
        self.setWindowTitle(f"상세 데이터 · {name} ({code})")
        self.resize(560, 640)
        lay = QVBoxLayout(self)
        flag = "🇰🇷" if q.get("market") == "KR" else "🇺🇸"
        head = QLabel(f"{flag} <b>{name}</b> ({code}) — 현재가 "
                      f"{fmt_money(q.get('price'), q.get('currency','USD'))}")
        head.setTextFormat(Qt.RichText)
        lay.addWidget(head)
        view = QTextBrowser()
        view.setHtml(detail_html(q))
        view.setOpenExternalLinks(False)
        lay.addWidget(view, 1)
        note = QLabel("※ 데이터 제공 범위는 종목·시장에 따라 다릅니다(빈 항목은 제외). "
                      "국내는 네이버, 미국은 yfinance 기준. 참고용이며 투자권유가 아닙니다.")
        note.setWordWrap(True)
        note.setStyleSheet("color:#8b949e; font-size:11px;")
        lay.addWidget(note)
        bb = QDialogButtonBox(QDialogButtonBox.Close)
        bb.button(QDialogButtonBox.Close).clicked.connect(self.accept)
        lay.addWidget(bb)


# ============================ 종목 분석 탭 ============================
class AnalyzeTab(QWidget):
    def __init__(self, main):
        super().__init__()
        self.main = main
        self.quote = None
        lay = QVBoxLayout(self)

        # 검색바
        bar = QHBoxLayout()
        self.inp = QLineEdit()
        self.inp.setPlaceholderText("종목명·코드·티커 입력 (예: 삼성전자 / 005930 / AAPL)")
        self.inp.returnPressed.connect(self.search)
        btn = QPushButton("조회")
        btn.clicked.connect(self.search)
        self.add_btn = QPushButton("＋ 포트폴리오에 추가")
        self.add_btn.setEnabled(False)
        self.add_btn.clicked.connect(self.add_to_portfolio)
        help_btn = QPushButton("ⓘ 지표 설명")
        help_btn.clicked.connect(lambda: MetricsHelpDialog(self).exec())
        bar.addWidget(self.inp, 1)
        bar.addWidget(btn)
        bar.addWidget(self.add_btn)
        bar.addWidget(help_btn)
        lay.addLayout(bar)

        # 내 보유 종목 바로가기(현재 프로필의 주식·ETF)
        self.my_bar = QHBoxLayout()
        self.my_bar.setSpacing(4)
        self._my_label = QLabel("내 보유:")
        self._my_label.setStyleSheet("color:#57606a;")
        self.my_bar.addWidget(self._my_label)
        self.my_bar.addStretch()
        lay.addLayout(self.my_bar)

        # 헤더(종목명/가격)
        self.header = QLabel("종목을 조회하세요.")
        self.header.setStyleSheet("font-size:22px; font-weight:800; margin-top:6px;")
        lay.addWidget(self.header)
        self.subheader = QLabel("")
        self.subheader.setStyleSheet("color:#57606a;")
        lay.addWidget(self.subheader)

        # 총평 배너
        self.summary = QLabel("")
        self.summary.setWordWrap(True)
        self.summary.setStyleSheet("margin:6px 0;")
        lay.addWidget(self.summary)

        # 지표 카드 그리드
        self.grid_host = QWidget()
        self.grid = QGridLayout(self.grid_host)
        self.grid.setSpacing(10)
        lay.addWidget(self.grid_host)

        lay.addStretch()
        note = QLabel("※ 지표 기준은 업종·성장성을 배제한 단순 일반 가이드입니다. "
                      "무료 지연 시세이며 투자 판단·매매 권유가 아닙니다.")
        note.setWordWrap(True)
        note.setStyleSheet("color:#8b949e; font-size:11px;")
        lay.addWidget(note)
        self._my_buttons = []

    def showEvent(self, e):
        super().showEvent(e)
        self._load_my_holdings()

    def _load_my_holdings(self):
        """현재 프로필의 주식·ETF를 바로가기 버튼으로."""
        for b in getattr(self, "_my_buttons", []):
            b.setParent(None)
            b.deleteLater()
        self._my_buttons = []
        try:
            pid = self.main.active_profile_id()
            rows = db.list_holdings(pid)
        except Exception:
            rows = []
        secs = [r for r in rows if r["kind"] in ("주식", "ETF") and r.get("code")]
        self._my_label.setVisible(bool(secs))
        for r in secs:
            b = QPushButton(r["name"])
            b.setToolTip(f"{r['name']} ({r['code']}) 분석")
            b.setStyleSheet("padding:2px 8px;")
            code = r["code"]
            b.clicked.connect(lambda _=False, c=code: self._analyze_code(c))
            # 스트레치 앞에 삽입
            self.my_bar.insertWidget(self.my_bar.count() - 1, b)
            self._my_buttons.append(b)

    def _analyze_code(self, code):
        self.inp.setText(code)
        self.search()

    def install_completer(self, index):
        """KRX 종목목록으로 검색창 자동완성 설치(후보를 항상 소량으로 제한해 프리즈 방지)."""
        if not index:
            return
        self._code_map = {}
        labels = []
        for code, name in index:
            label = f"{name} ({code})"
            labels.append(label)
            self._code_map[label] = code
        # 정렬된 목록(소문자 기준) — 접두어 범위를 이진탐색으로 빠르게 찾기 위함
        pairs = sorted(((l.lower(), l) for l in labels), key=lambda x: x[0])
        self._sorted_lower = [p[0] for p in pairs]
        self._sorted_labels = [p[1] for p in pairs]

        # 표준 setCompleter 방식(팝업 표시·키 이동·IME·선택을 Qt가 안정적으로 처리).
        # 백스페이스 프리즈를 막기 위해 모델에는 '현재 접두어에 맞는 최대 50개'만 담는다.
        self._comp_model = QStringListModel([], self)
        comp = QCompleter(self._comp_model, self)
        comp.setCaseSensitivity(Qt.CaseInsensitive)
        comp.setFilterMode(Qt.MatchStartsWith)
        comp.setCompletionMode(QCompleter.PopupCompletion)
        comp.setMaxVisibleItems(12)
        comp.activated[str].connect(self._on_completer_pick)
        self._completer = comp
        self.inp.setCompleter(comp)
        # 입력이 잠깐 멈춘 뒤 모델 갱신(연타/IME 시 부담↓)
        self._pending_text = ""
        self._comp_timer = QTimer(self)
        self._comp_timer.setSingleShot(True)
        self._comp_timer.setInterval(120)
        self._comp_timer.timeout.connect(self._run_completion)
        self.inp.textEdited.connect(self._on_text_edited)

    def _on_text_edited(self, text):
        self._pending_text = text
        self._comp_timer.start()

    def _run_completion(self):
        import bisect
        comp = getattr(self, "_completer", None)
        if comp is None:
            return
        t = (self._pending_text or "").strip().lower()
        if len(t) < 1:
            self._comp_model.setStringList([])
            return
        lo = bisect.bisect_left(self._sorted_lower, t)
        out, i, n = [], lo, len(self._sorted_lower)
        while i < n and len(out) < 50 and self._sorted_lower[i].startswith(t):
            out.append(self._sorted_labels[i])
            i += 1
        self._comp_model.setStringList(out)  # ≤50개 -> 팝업 가벼움
        if out and self.inp.hasFocus():
            comp.complete()

    def _on_completer_pick(self, label):
        code = getattr(self, "_code_map", {}).get(label)
        if code:
            self.inp.setText(code)
        # 선택 직후 바로 조회 (텍스트 덮어쓰기 이후 실행되도록 지연)
        from PySide6.QtCore import QTimer
        QTimer.singleShot(0, self.search)

    def search(self):
        q = self.inp.text().strip()
        if not q:
            return
        # 자동완성 라벨 "이름 (005380)" 형태면 코드만 추출
        m = re.search(r"\((\d{6})\)\s*$", q)
        if m:
            q = m.group(1)
            self.inp.setText(q)
        self.header.setText("조회 중…")
        self.subheader.setText("")
        self.summary.setText("")
        self.add_btn.setEnabled(False)
        self._clear_grid()
        run_async(self, datasource.get_quote, self._on_quote, self._on_err, q)

    def _on_err(self, msg):
        self.header.setText("조회 실패")
        self.subheader.setText(msg)

    def _on_quote(self, q: dict):
        self.quote = q
        cur = q["currency"]
        flag = "🇰🇷" if q["market"] == "KR" else "🇺🇸"
        self.header.setText(f"{flag} {q['name']}  ({q['code']})")
        chg = ""
        if q.get("price") and q.get("prev_close"):
            d = q["price"] - q["prev_close"]
            pct = d / q["prev_close"] * 100 if q["prev_close"] else 0
            sign = "▲" if d > 0 else ("▼" if d < 0 else "-")
            col = "#cf222e" if d > 0 else ("#1f6feb" if d < 0 else "#57606a")
            chg = f"   <span style='color:{col}'>{sign} {abs(d):,.2f} ({pct:+.2f}%)</span>"
        mc = q.get("market_cap")
        mc_txt = fmt_money(mc, cur) if mc else "-"
        sector = f" · {q['sector']}" if q.get("sector") else ""
        self.subheader.setText(
            f"현재가 {fmt_money(q.get('price'), cur)}{chg}   |   시가총액 {mc_txt}{sector}")
        self.subheader.setTextFormat(Qt.RichText)

        evals = metrics.evaluate(q)
        ov = metrics.overall(evals)
        ov_color = metrics.COLORS[ov["level"]]
        self.summary.setText(
            f"<b>종합(참고):</b> <span style='color:{ov_color}; font-weight:700'>"
            f"{ov['text']}</span>")
        self.summary.setTextFormat(Qt.RichText)

        self._clear_grid()
        for i, ev in enumerate(evals):
            self.grid.addWidget(MetricCard(ev), i // 3, i % 3)
        self.add_btn.setEnabled(q["market"] in ("KR", "US"))

    def _clear_grid(self):
        while self.grid.count():
            item = self.grid.takeAt(0)
            w = item.widget()
            if w:
                w.deleteLater()

    def add_to_portfolio(self):
        if not self.quote:
            return
        q = self.quote
        pre = {
            "kind": "주식",
            "market": q["market"],
            "code": q["code"],
            "name": q["name"],
            "currency": q["currency"],
            "avg_price": q.get("price") or 0,
            "qty": 0,
        }
        dlg = HoldingDialog(self, pre)
        if dlg.exec() == QDialog.Accepted:
            pt = self.main.active_portfolio_tab()
            db.add_holding(dlg.result_data(), pt.profile_id)
            pt.reload()
            self.main.tabs.setCurrentWidget(pt)


# ============================ 보유내역 추가/수정 다이얼로그 ============================
class HoldingDialog(QDialog):
    def __init__(self, parent, data: dict | None = None, hid=None):
        super().__init__(parent)
        self.hid = hid
        self.setWindowTitle("보유 자산 " + ("수정" if hid else "추가"))
        self.setMinimumWidth(420)
        data = data or {}
        form = QFormLayout(self)

        self.kind = QComboBox()
        self.kind.addItems(db.KINDS)
        if data.get("kind"):
            self.kind.setCurrentText(data["kind"])
        self.kind.currentTextChanged.connect(self._sync)

        self.name = QLineEdit(data.get("name", ""))
        self.code = QLineEdit(data.get("code", "") or "")
        self.code.setPlaceholderText("종목코드/티커 (예: 005930, AAPL)")
        self.lookup_btn = QPushButton("조회로 채우기")
        self.lookup_btn.clicked.connect(self._lookup)
        code_row = QHBoxLayout()
        code_row.addWidget(self.code, 1)
        code_row.addWidget(self.lookup_btn)
        code_w = QWidget(); code_w.setLayout(code_row)

        self.currency = QComboBox()
        self.currency.addItems(datasource.CURRENCIES)
        self.currency.setCurrentText(data.get("currency", "KRW"))

        self.qty = QDoubleSpinBox()
        self.qty.setRange(0, 1e12); self.qty.setDecimals(6)
        self.qty.setValue(float(data.get("qty") or 0))
        self.avg = QDoubleSpinBox()
        self.avg.setRange(0, 1e12); self.avg.setDecimals(4); self.avg.setGroupSeparatorShown(True)
        self.avg.setValue(float(data.get("avg_price") or 0))
        self.amount = QDoubleSpinBox()
        self.amount.setRange(0, 1e15); self.amount.setDecimals(2); self.amount.setGroupSeparatorShown(True)
        self.amount.setValue(float(data.get("amount") or 0))
        self.principal = QDoubleSpinBox()
        self.principal.setRange(0, 1e15); self.principal.setDecimals(0)
        self.principal.setGroupSeparatorShown(True)
        self.principal.setValue(float(data.get("principal") or 0))
        self.memo = QLineEdit(data.get("memo", ""))
        self.included = QCheckBox("포트폴리오 계산(총액·비중)에 포함")
        self.included.setChecked(bool(data.get("included", 1)))

        form.addRow("종류", self.kind)
        form.addRow("이름", self.name)
        self.row_code = code_w
        form.addRow("코드/티커", self.row_code)
        form.addRow("통화", self.currency)
        form.addRow("수량", self.qty)
        form.addRow("평균 매입단가", self.avg)
        form.addRow("금액 (현재 잔액/평가금액)", self.amount)
        form.addRow("투입원금 (원화, 선택)", self.principal)
        form.addRow("메모", self.memo)
        form.addRow("", self.included)

        self.hint = QLabel("")
        self.hint.setStyleSheet("color:#8b949e; font-size:11px;")
        self.hint.setWordWrap(True)
        form.addRow(self.hint)

        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        form.addRow(bb)
        self._form = form
        self._sync()

    def _is_security(self):
        return self.kind.currentText() in ("주식", "ETF")

    def _sync(self):
        if not hasattr(self, "_form"):
            return
        sec = self._is_security()
        kind = self.kind.currentText()
        # 관련 없는 입력칸은 아예 숨긴다
        for w in (self.row_code, self.qty, self.avg):
            self._form.setRowVisible(w, sec)
        self._form.setRowVisible(self.amount, not sec)
        self._form.setRowVisible(self.principal, not sec)   # 투입원금(현금류만)
        self.amount.setEnabled(True)
        # 통화: 주식/ETF와 현금·예금·기타(외화 가능)는 선택 노출. 적금은 원화 고정.
        show_currency = sec or kind in ("현금", "예금", "기타")
        self._form.setRowVisible(self.currency, show_currency)
        if not show_currency:
            self.currency.setCurrentText("KRW")
        if sec:
            self.hint.setText("주식·ETF: 코드로 '조회로 채우기' 후 수량·매입단가를 입력하면 "
                              "현재가·평가금액·비중이 자동 계산됩니다.")
        elif show_currency:
            self.hint.setText("현금·예금·기타: '금액'에 현재 잔액(선택한 통화)을 입력하세요. "
                              "달러 등 외화는 통화를 USD로. '투입원금(원화)'을 넣으면 "
                              "현재 원화가치와 비교해 손익(환차익 등)을 보여줍니다.")
        else:
            self.hint.setText("적금: '금액'에 현재 잔액(원)을, 원하면 '투입원금'에 넣은 원금을 입력하세요.")
        self.adjustSize()

    def _lookup(self):
        q = self.code.text().strip() or self.name.text().strip()
        if not q:
            return
        self.lookup_btn.setText("조회 중…")
        self.lookup_btn.setEnabled(False)

        def done(res):
            self.lookup_btn.setText("조회로 채우기")
            self.lookup_btn.setEnabled(True)
            self.name.setText(res["name"])
            self.code.setText(res["code"])
            self.currency.setCurrentText(res["currency"])
            if res.get("price") and self.avg.value() == 0:
                self.avg.setValue(res["price"])
            self._market = res["market"]
            self.hint.setText(f"현재가 {fmt_money(res.get('price'), res['currency'])} "
                              f"· PER {fmt_num(res.get('per'))} · 조회 완료")

        def fail(msg):
            self.lookup_btn.setText("조회로 채우기")
            self.lookup_btn.setEnabled(True)
            QMessageBox.warning(self, "조회 실패", msg)

        run_async(self, datasource.get_quote, done, fail, q)

    def result_data(self) -> dict:
        sec = self._is_security()
        market = getattr(self, "_market", None)
        if sec and not market:
            # 코드로 시장 추정
            resolved = datasource.resolve(self.code.text().strip())
            market = resolved[0] if resolved else None
        return {
            "kind": self.kind.currentText(),
            "market": market if sec else None,
            "code": self.code.text().strip() if sec else None,
            "name": self.name.text().strip() or self.code.text().strip(),
            "qty": self.qty.value() if sec else 0,
            "avg_price": self.avg.value() if sec else 0,
            "amount": 0 if sec else self.amount.value(),
            "currency": self.currency.currentText(),
            "memo": self.memo.text().strip(),
            "sort": 0,
            "included": 1 if self.included.isChecked() else 0,
            "principal": 0 if sec else self.principal.value(),
        }


# ============================ 포트폴리오 탭 ============================
COLS = ["종류", "이름", "코드", "수량", "매입가", "현재가", "평가금액(KRW)",
        "손익", "비중"]


# ============================ 매도 다이얼로그 ============================
class SellDialog(QDialog):
    def __init__(self, parent, row, cur_price):
        super().__init__(parent)
        self.row = row
        self.setWindowTitle(f"매도 — {row['name']}")
        self.setMinimumWidth(360)
        cur = row.get("currency", "KRW")
        form = QFormLayout(self)
        held = row.get("qty") or 0
        form.addRow(QLabel(f"보유 {held:g}주 · 평균매입가 {fmt_money(row.get('avg_price'), cur)}"))
        self.qty = QDoubleSpinBox(); self.qty.setRange(0, held); self.qty.setDecimals(6)
        self.qty.setValue(held); self.qty.valueChanged.connect(self._preview)
        self.price = QDoubleSpinBox(); self.price.setRange(0, 1e12); self.price.setDecimals(2)
        self.price.setGroupSeparatorShown(True); self.price.setValue(cur_price or 0)
        self.price.valueChanged.connect(self._preview)
        self.ymd = QLineEdit(QDateTime.currentDateTime().toString("yyyy-MM-dd"))
        self.memo = QLineEdit()
        form.addRow("매도 수량", self.qty)
        form.addRow(f"매도 단가 ({cur})", self.price)
        form.addRow("매도일 (YYYY-MM-DD)", self.ymd)
        form.addRow("메모", self.memo)
        self.preview = QLabel(""); self.preview.setTextFormat(Qt.RichText)
        form.addRow(self.preview)
        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        bb.accepted.connect(self.accept); bb.rejected.connect(self.reject)
        form.addRow(bb)
        self._preview()

    def _preview(self):
        cur = self.row.get("currency", "KRW")
        gross = self.qty.value() * (self.price.value() - (self.row.get("avg_price") or 0))
        c = "#cf222e" if gross > 0 else ("#1f6feb" if gross < 0 else "#57606a")
        self.preview.setText(
            f"실현손익(해당 통화): <span style='color:{c}; font-weight:700'>"
            f"{gross:+,.2f} {cur}</span>")

    def result(self):
        return {"qty": self.qty.value(), "sell_price": self.price.value(),
                "ymd": self.ymd.text().strip() or
                QDateTime.currentDateTime().toString("yyyy-MM-dd"),
                "memo": self.memo.text().strip()}


# ============================ 거래내역(실현손익) 다이얼로그 ============================
class TransactionsDialog(QDialog):
    _COLS = ["날짜", "종목", "수량", "매입가", "매도가", "실현손익(원)"]

    def __init__(self, parent, profile_id):
        super().__init__(parent)
        self.profile_id = profile_id
        self.setWindowTitle("매도 거래내역 · 실현손익")
        self.resize(640, 460)
        lay = QVBoxLayout(self)
        self.total_lbl = QLabel(""); self.total_lbl.setTextFormat(Qt.RichText)
        self.total_lbl.setStyleSheet(
            "background:#f6f8fa; border:1px solid #d0d7de; border-radius:8px; padding:8px;")
        lay.addWidget(self.total_lbl)
        self.table = QTableWidget(0, len(self._COLS))
        self.table.setHorizontalHeaderLabels(self._COLS)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.verticalHeader().setVisible(False)
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.Stretch)
        lay.addWidget(self.table, 1)
        row = QHBoxLayout()
        dele = QPushButton("선택 기록 삭제"); dele.clicked.connect(self._delete)
        row.addStretch(); row.addWidget(dele)
        lay.addLayout(row)
        self._load()

    def _load(self):
        txns = db.list_transactions(self.profile_id)
        self._ids = [t["id"] for t in txns]
        self.table.setRowCount(len(txns))
        for i, t in enumerate(txns):
            cur = t.get("currency", "KRW")
            rz = t.get("realized") or 0
            cells = [t["ymd"], t["name"], f"{t['qty']:g}",
                     fmt_money(t["buy_price"], cur), fmt_money(t["sell_price"], cur),
                     f"{rz:+,.0f}"]
            for c, v in enumerate(cells):
                it = QTableWidgetItem(str(v))
                if c in (2, 3, 4, 5):
                    it.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
                if c == 5:
                    it.setForeground(QColor("#cf222e" if rz > 0 else
                                            ("#1f6feb" if rz < 0 else "#57606a")))
                self.table.setItem(i, c, it)
        total = db.realized_total(self.profile_id)
        tc = "#cf222e" if total > 0 else ("#1f6feb" if total < 0 else "#57606a")
        self.total_lbl.setText(
            f"누적 실현손익 <span style='font-size:16px; font-weight:800; color:{tc}'>"
            f"{total:+,.0f}원</span> · 거래 {len(txns)}건")

    def _delete(self):
        r = self.table.currentRow()
        if r < 0 or r >= len(self._ids):
            return
        db.delete_transaction(self._ids[r])
        self._load()
        pt = self.parent()
        if hasattr(pt, "render"):
            pt.render()


# ============================ 리밸런싱 제안 다이얼로그 ============================
class RebalanceDialog(QDialog):
    _COLS = ["자산", "현재금액", "현재%", "목표%", "조정(매수+/매도-)", "예상수량"]

    def __init__(self, pt):
        super().__init__(pt)
        self.pt = pt
        self.setWindowTitle("리밸런싱 제안")
        self.resize(720, 480)
        lay = QVBoxLayout(self)
        # 계산에 포함되는 자산만
        self.items = []
        for r in pt.rows:
            if not r.get("included", 1):
                continue
            krw, _ = pt._eval_amount_krw(r)
            if krw <= 0:
                continue
            self.items.append({"row": r, "krw": krw})
        self.total = sum(x["krw"] for x in self.items) or 1

        top = QHBoxLayout()
        top.addWidget(QLabel(f"총 평가금액 {self.total:,.0f}원 기준"))
        top.addStretch()
        eq = QPushButton("균등 분배"); eq.clicked.connect(self._preset_equal)
        keep = QPushButton("현재비중 유지"); keep.clicked.connect(self._preset_keep)
        top.addWidget(eq); top.addWidget(keep)
        lay.addLayout(top)

        self.table = QTableWidget(len(self.items), len(self._COLS))
        self.table.setHorizontalHeaderLabels(self._COLS)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.verticalHeader().setVisible(False)
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
        self.tgt_spins = []
        for i, x in enumerate(self.items):
            r = x["row"]
            self.table.setItem(i, 0, QTableWidgetItem(f"{r['name']}"))
            self.table.setItem(i, 1, QTableWidgetItem(f"{x['krw']:,.0f}원"))
            self.table.setItem(i, 2, QTableWidgetItem(f"{x['krw']/self.total*100:.1f}%"))
            sp = QDoubleSpinBox(); sp.setRange(0, 100); sp.setDecimals(1); sp.setSuffix(" %")
            sp.setValue(round(x["krw"] / self.total * 100, 1))
            sp.valueChanged.connect(self._recalc)
            self.table.setCellWidget(i, 3, sp); self.tgt_spins.append(sp)
            self.table.setItem(i, 4, QTableWidgetItem("-"))
            self.table.setItem(i, 5, QTableWidgetItem("-"))
            for c in (1, 2, 4, 5):
                self.table.item(i, c).setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
        lay.addWidget(self.table, 1)

        self.sum_lbl = QLabel(""); self.sum_lbl.setTextFormat(Qt.RichText)
        lay.addWidget(self.sum_lbl)
        note = QLabel("※ 목표 비중을 정하면 각 자산의 매수/매도 금액과 대략 수량을 제안합니다. "
                      "제안일 뿐 자동 매매는 하지 않습니다. (현금·예금 등도 포함)")
        note.setWordWrap(True); note.setStyleSheet("color:#8b949e; font-size:11px;")
        lay.addWidget(note)
        bb = QDialogButtonBox(QDialogButtonBox.Close)
        bb.rejected.connect(self.reject); bb.accepted.connect(self.accept)
        bb.button(QDialogButtonBox.Close).clicked.connect(self.accept)
        lay.addWidget(bb)
        self._recalc()

    def _preset_equal(self):
        n = len(self.items) or 1
        for sp in self.tgt_spins:
            sp.blockSignals(True); sp.setValue(round(100 / n, 1)); sp.blockSignals(False)
        self._recalc()

    def _preset_keep(self):
        for sp, x in zip(self.tgt_spins, self.items):
            sp.blockSignals(True); sp.setValue(round(x["krw"] / self.total * 100, 1))
            sp.blockSignals(False)
        self._recalc()

    def _recalc(self):
        tot_t = 0.0
        for i, x in enumerate(self.items):
            r = x["row"]
            t = self.tgt_spins[i].value(); tot_t += t
            target_v = self.total * t / 100
            diff = target_v - x["krw"]     # + 매수 / - 매도
            c = "#cf222e" if diff > 0 else ("#1f6feb" if diff < 0 else "#57606a")
            act = f"{diff:+,.0f}원"
            self.table.item(i, 4).setText(act)
            self.table.item(i, 4).setForeground(QColor(c))
            # 수량(주식·ETF): 조정금액 / 현재가(원화환산)
            qty_txt = "-"
            if r["kind"] in ("주식", "ETF"):
                price = self.pt.prices.get(r["id"]) or r.get("avg_price") or 0
                price_krw = price * self.pt._rate(r.get("currency"))
                if price_krw > 0:
                    q = diff / price_krw
                    qty_txt = f"{'+' if q>=0 else ''}{q:,.2f}주"
            self.table.item(i, 5).setText(qty_txt)
            self.table.item(i, 5).setForeground(QColor(c))
        warn = "" if abs(tot_t - 100) < 0.5 else \
            f" <span style='color:#cf222e'>(목표 합계 {tot_t:.1f}% — 100%로 맞추세요)</span>"
        self.sum_lbl.setText(f"목표 합계 <b>{tot_t:.1f}%</b>{warn}")


class PortfolioTab(QWidget):
    def __init__(self, main, profile_id):
        super().__init__()
        self.main = main
        self.rows: list[dict] = []
        self.prices: dict[int, float] = {}   # holding id -> 현재가(현지통화)
        self.rates = {}   # 통화 -> 원화 환율
        self._refreshing = False
        self.profile_id = profile_id
        lay = QVBoxLayout(self)

        # 프로필 바 (이 탭 = 이 프로필)
        pbar = QHBoxLayout()
        for text, fn in [("＋ 프로필 추가", self._new_profile),
                         ("이름변경", self._rename_profile),
                         ("이 프로필 삭제", self._delete_profile)]:
            b = QPushButton(text); b.clicked.connect(fn); pbar.addWidget(b)
        pbar.addStretch()
        pbar.addWidget(QLabel("투자성향"))
        self.style_combo = QComboBox()
        self.style_combo.addItems(list(metrics.STYLES.keys()))
        self.style_combo.currentTextChanged.connect(self._style_changed)
        pbar.addWidget(self.style_combo)
        pbar.addWidget(QLabel("나이"))
        self.age = QSpinBox()
        self.age.setRange(10, 100)
        self.age.setSuffix(" 세")
        self.age.valueChanged.connect(self._age_changed)
        pbar.addWidget(self.age)
        lay.addLayout(pbar)

        # 상단 버튼
        bar = QHBoxLayout()
        for text, fn in [("＋ 추가", self.add), ("수정", self.edit),
                         ("삭제", self.remove), ("💰 매도", self.sell),
                         ("📜 거래내역", self.show_transactions),
                         ("⚖️ 리밸런싱", self.rebalance),
                         ("🔄 새로고침", self.refresh)]:
            b = QPushButton(text); b.clicked.connect(fn); bar.addWidget(b)
        # 순서 직접 조정(수동)
        up = QPushButton("▲"); up.setToolTip("선택 항목을 위로")
        up.setFixedWidth(34); up.clicked.connect(lambda: self._move(-1))
        down = QPushButton("▼"); down.setToolTip("선택 항목을 아래로")
        down.setFixedWidth(34); down.clicked.connect(lambda: self._move(1))
        bar.addWidget(up); bar.addWidget(down)
        # 정렬 기준
        bar.addWidget(QLabel("정렬"))
        self.sort_combo = QComboBox()
        self.sort_mode = "수동"
        self.sort_combo.addItems([
            "수동", "평가금액 많은순", "평가금액 적은순", "비중 높은순",
            "손익 높은순", "손익 낮은순", "이름순", "종류순"])
        self.sort_combo.currentTextChanged.connect(self._sort_changed)
        bar.addWidget(self.sort_combo)
        bar.addStretch()
        self.fx_label = QLabel("")
        self.fx_label.setStyleSheet("color:#57606a; margin-left:12px;")
        bar.addWidget(self.fx_label)
        lay.addLayout(bar)

        # 요약
        self.summary = QLabel("")
        self.summary.setTextFormat(Qt.RichText)
        self.summary.setStyleSheet(
            "background:#f6f8fa; border:1px solid #d0d7de; border-radius:8px; padding:10px;")
        self.summary.setWordWrap(True)
        lay.addWidget(self.summary)

        # 초보자용 적정 주식비중 제안
        self.reco = QLabel("")
        self.reco.setTextFormat(Qt.RichText)
        self.reco.setWordWrap(True)
        self.reco.setStyleSheet(
            "border:1px solid #d0d7de; border-radius:8px; padding:10px;")
        lay.addWidget(self.reco)

        # 테이블
        self.table = QTableWidget(0, len(COLS))
        self.table.setHorizontalHeaderLabels(COLS)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.table.verticalHeader().setVisible(False)
        self.table.doubleClicked.connect(lambda *_: self.edit())
        hh = self.table.horizontalHeader()
        hh.setSectionResizeMode(1, QHeaderView.Stretch)
        for i in (0, 2, 3, 4, 5, 6, 7, 8):
            hh.setSectionResizeMode(i, QHeaderView.ResizeToContents)
        lay.addWidget(self.table, 1)

        self.status = QLabel("")
        self.status.setStyleSheet("color:#8b949e; font-size:11px;")
        lay.addWidget(self.status)

        self._sync_profile_meta()
        self.reload()

    # --- 프로필 메타(나이/성향) ---
    def _sync_profile_meta(self):
        prof = db.get_profile(self.profile_id) or {"age": 30, "style": "나이 기준"}
        self.age.blockSignals(True)
        self.age.setValue(int(prof.get("age", 30)))
        self.age.blockSignals(False)
        self.style_combo.blockSignals(True)
        st = prof.get("style") or "나이 기준"
        if st in metrics.STYLES:
            self.style_combo.setCurrentText(st)
        self.style_combo.blockSignals(False)

    def _style_changed(self, style):
        db.set_profile_style(self.profile_id, style)
        self.render()

    # --- 프로필 (탭 = 프로필) ---
    def _new_profile(self):
        from PySide6.QtWidgets import QInputDialog
        name, ok = QInputDialog.getText(self, "프로필 추가", "새 프로필 이름:")
        if not ok or not name.strip():
            return
        pid = db.add_profile(name.strip(), age=self.age.value())
        self.main.rebuild_portfolio_tabs(select_pid=pid)

    def _rename_profile(self):
        from PySide6.QtWidgets import QInputDialog
        cur = db.get_profile(self.profile_id)
        name, ok = QInputDialog.getText(self, "이름 변경", "새 이름:",
                                        text=cur["name"] if cur else "")
        if not ok or not name.strip():
            return
        db.rename_profile(self.profile_id, name.strip())
        self.main.rebuild_portfolio_tabs(select_pid=self.profile_id)

    def _delete_profile(self):
        cur = db.get_profile(self.profile_id)
        if QMessageBox.question(
                self, "프로필 삭제",
                f"'{cur['name']}' 프로필과 그 안의 모든 보유내역을 삭제할까요?") \
                != QMessageBox.Yes:
            return
        try:
            db.delete_profile(self.profile_id)
        except ValueError as e:
            QMessageBox.warning(self, "삭제 불가", str(e))
            return
        self.main.rebuild_portfolio_tabs()

    # --- 데이터 로드 ---
    def reload(self):
        self.rows = db.list_holdings(self.profile_id)
        self._apply_sort()
        self.render()

    def selected_id(self):
        r = self.table.currentRow()
        if r < 0 or r >= len(self.rows):
            return None
        return self.rows[r]["id"]

    # --- 정렬 / 순서 ---
    def _apply_sort(self):
        mode = getattr(self, "sort_mode", "수동")
        if mode == "수동":
            return   # db의 sort 순서 유지
        def krw(r):
            return self._eval_amount_krw(r)[0]
        def pnl(r):
            if r["kind"] in ("주식", "ETF"):
                return krw(r) - self._cost_krw(r)
            return 0.0
        keys = {
            "평가금액 많은순": (krw, True),
            "평가금액 적은순": (krw, False),
            "비중 높은순": (lambda r: krw(r) if r.get("included", 1) else -1, True),
            "손익 높은순": (pnl, True),
            "손익 낮은순": (pnl, False),
            "이름순": (lambda r: str(r["name"]), False),
            "종류순": (lambda r: str(r["kind"]), False),
        }
        if mode in keys:
            fn, desc = keys[mode]
            self.rows.sort(key=fn, reverse=desc)

    def _sort_changed(self, mode):
        self.sort_mode = mode
        self._apply_sort()
        self.render()

    def _move(self, delta):
        hid = self.selected_id()
        if hid is None:
            QMessageBox.information(self, "안내", "이동할 항목을 선택하세요.")
            return
        # 자동정렬 중이면 현재 표시 순서를 수동으로 고정
        if self.sort_mode != "수동":
            self.sort_mode = "수동"
            self.sort_combo.blockSignals(True)
            self.sort_combo.setCurrentText("수동")
            self.sort_combo.blockSignals(False)
            db.reorder([r["id"] for r in self.rows])
        idx = next((i for i, r in enumerate(self.rows) if r["id"] == hid), None)
        j = idx + delta
        if idx is None or j < 0 or j >= len(self.rows):
            return
        self.rows[idx], self.rows[j] = self.rows[j], self.rows[idx]
        db.reorder([r["id"] for r in self.rows])
        self.render()
        self.table.selectRow(j)

    def _age_changed(self, v):
        db.set_profile_age(self.profile_id, v)
        self.render()

    # --- CRUD ---
    def add(self):
        dlg = HoldingDialog(self)
        if dlg.exec() == QDialog.Accepted:
            db.add_holding(dlg.result_data(), self.profile_id)
            self.reload()

    def edit(self):
        hid = self.selected_id()
        if hid is None:
            QMessageBox.information(self, "안내", "수정할 항목을 선택하세요.")
            return
        row = next(r for r in self.rows if r["id"] == hid)
        dlg = HoldingDialog(self, dict(row), hid=hid)
        if dlg.exec() == QDialog.Accepted:
            db.update_holding(hid, dlg.result_data())
            self.reload()

    def remove(self):
        hid = self.selected_id()
        if hid is None:
            return
        row = next(r for r in self.rows if r["id"] == hid)
        if QMessageBox.question(self, "삭제", f"'{row['name']}' 항목을 삭제할까요?") \
                == QMessageBox.Yes:
            db.delete_holding(hid)
            self.prices.pop(hid, None)
            self.reload()

    # --- 매도(실현손익) ---
    def sell(self):
        hid = self.selected_id()
        if hid is None:
            QMessageBox.information(self, "안내", "매도할 종목(주식/ETF)을 선택하세요.")
            return
        row = next(r for r in self.rows if r["id"] == hid)
        if row["kind"] not in ("주식", "ETF"):
            QMessageBox.information(self, "안내", "주식·ETF만 매도 기록이 가능합니다.")
            return
        price = self.prices.get(hid) or row.get("avg_price") or 0
        dlg = SellDialog(self, row, price)
        if dlg.exec() != QDialog.Accepted:
            return
        d = dlg.result()
        rate = self._rate(row.get("currency"))
        realized = d["qty"] * (d["sell_price"] - (row.get("avg_price") or 0)) * rate
        db.add_transaction({
            "profile_id": self.profile_id, "ymd": d["ymd"],
            "market": row.get("market"), "code": row.get("code"), "name": row["name"],
            "qty": d["qty"], "buy_price": row.get("avg_price") or 0,
            "sell_price": d["sell_price"], "currency": row.get("currency", "KRW"),
            "realized": realized, "memo": d.get("memo", "")})
        # 수량 차감(전량이면 삭제)
        left = (row.get("qty") or 0) - d["qty"]
        if left <= 1e-9:
            db.delete_holding(hid)
            self.prices.pop(hid, None)
        else:
            nrow = dict(row); nrow["qty"] = left
            db.update_holding(hid, nrow)
        self.reload()
        QMessageBox.information(
            self, "매도 기록",
            f"{row['name']} {d['qty']:g}주 매도 · 실현손익 {realized:+,.0f}원")

    def show_transactions(self):
        TransactionsDialog(self, self.profile_id).exec()

    def rebalance(self):
        RebalanceDialog(self).exec()

    def showEvent(self, e):
        super().showEvent(e)
        self.main._active_pid = self.profile_id   # 이 포트폴리오를 활성으로
        self.refresh()

    # --- 시세 갱신 ---
    def refresh(self):
        if self._refreshing:
            return
        secs = [r for r in self.rows if r["kind"] in ("주식", "ETF") and r.get("code")]
        foreign = {r.get("currency") for r in self.rows
                   if r.get("currency") and r["currency"] != "KRW"}
        if not secs and not foreign:
            self.status.setText("갱신할 시세가 없습니다.")
            return
        self._refreshing = True
        self.status.setText("시세 조회 중…")

        def job():
            rates = datasource.fx_rates(foreign) if foreign else {}
            out = {}
            for r in secs:
                try:
                    m = r.get("market")
                    if m in ("KR", "US"):
                        # 저장된 시장 사용(코드 재추정 안 함 → 영숫자 ETF코드도 정상)
                        out[r["id"]] = datasource.live_price(m, r["code"]).get("price")
                    else:
                        out[r["id"]] = datasource.get_quote(r["code"]).get("price")
                except Exception:
                    out[r["id"]] = None
            return rates, out

        def done(res):
            self._refreshing = False
            rates, out = res
            self.rates = rates
            self.prices.update(out)
            if rates:
                self.fx_label.setText(
                    " · ".join(f"{c}/KRW {v:,.1f}" for c, v in sorted(rates.items())))
            else:
                self.fx_label.setText("")
            now = QDateTime.currentDateTime().toString("HH:mm:ss")
            self.status.setText(f"시세 갱신 완료 ({now})")
            self.render()

        def fail(msg):
            self._refreshing = False
            self.status.setText(f"시세 조회 실패: {msg}")

        run_async(self, job, done, fail)

    # --- 계산 ---
    def _rate(self, cur):
        if not cur or cur == "KRW":
            return 1.0
        return self.rates.get(cur) or datasource._FX_FALLBACK.get(cur, 1.0)

    def _eval_amount_krw(self, r):
        """평가금액을 KRW로 환산 -> (krw, local)."""
        if r["kind"] in ("주식", "ETF"):
            price = self.prices.get(r["id"])
            if price is None:
                price = r.get("avg_price") or 0
            local = (r.get("qty") or 0) * price
        else:
            local = r.get("amount") or 0
        return local * self._rate(r.get("currency")), local

    def _cost_krw(self, r):
        """투입원금(KRW). 주식·ETF=수량×매입가×환율, 현금류=투입원금(원화 직접입력)."""
        if r["kind"] in ("주식", "ETF"):
            local = (r.get("qty") or 0) * (r.get("avg_price") or 0)
            return local * self._rate(r.get("currency"))
        return r.get("principal") or 0

    # --- 렌더 ---
    def render(self):
        totals_krw = []
        for r in self.rows:
            krw, _ = self._eval_amount_krw(r)
            totals_krw.append(krw)
        # 총액·비중은 '계산 포함' 자산만
        total = sum(k for k, r in zip(totals_krw, self.rows)
                    if r.get("included", 1)) or 0

        self.table.setRowCount(len(self.rows))
        kind_sum: dict[str, float] = {}
        excluded_krw = 0
        inv_sum = 0.0      # 투입원금 합(원가 있는 자산만)
        inv_cur_sum = 0.0  # 그 자산들의 현재가치 합
        for i, r in enumerate(self.rows):
            cur = r.get("currency", "KRW")
            krw = totals_krw[i]
            included = bool(r.get("included", 1))
            price = self.prices.get(r["id"])
            is_sec = r["kind"] in ("주식", "ETF")

            if included:
                kind_sum[r["kind"]] = kind_sum.get(r["kind"], 0) + krw
            else:
                excluded_krw += krw

            # 손익 (주식·ETF: 시세 기준 / 현금류: 투입원금 대비 현재 원화가치)
            pnl_txt, pnl_color = "-", None
            cost = self._cost_krw(r)
            has_pnl = (is_sec and price and r.get("avg_price")) or \
                      (not is_sec and cost > 0)
            if has_pnl and cost > 0:
                pnl = krw - cost
                pct = (pnl / cost * 100) if cost else 0
                pnl_txt = f"{pnl:+,.0f}원 ({pct:+.1f}%)"
                pnl_color = "#cf222e" if pnl > 0 else ("#1f6feb" if pnl < 0 else None)
                if included:
                    inv_sum += cost
                    inv_cur_sum += krw

            weight = (krw / total * 100) if (total and included) else 0
            weight_txt = f"{weight:.1f}%" if included else "제외"
            cells = [
                r["kind"] + ("" if included else " ⛔"),
                r["name"],
                r.get("code") or "-",
                (f"{r['qty']:,.4f}".rstrip('0').rstrip('.') if is_sec and r.get("qty") else ("-" if is_sec else "")),
                (fmt_money(r.get("avg_price"), cur) if is_sec and r.get("avg_price") else "-"),
                (fmt_money(price, cur) if is_sec and price else ("-" if is_sec else "")),
                f"{krw:,.0f}원",
                pnl_txt,
                weight_txt,
            ]
            for c, text in enumerate(cells):
                it = QTableWidgetItem(str(text))
                it.setToolTip(str(text))          # 잘려도 마우스 올리면 전체 표시
                if c in (3, 4, 5, 6, 7, 8):
                    it.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
                if not included:
                    it.setForeground(QColor("#a0a6ac"))   # 제외 자산은 회색
                elif c == 7 and pnl_color:
                    it.setForeground(QColor(pnl_color))
                self.table.setItem(i, c, it)

        # 요약
        if total > 0:
            parts = []
            for k, v in sorted(kind_sum.items(), key=lambda x: -x[1]):
                parts.append(f"{k} {v/total*100:.1f}% ({v:,.0f}원)")
            asset_line = " · ".join(parts)
        else:
            asset_line = "보유 자산을 추가하세요."
        excl_txt = (f" &nbsp;<span style='color:#a0a6ac'>· 계산제외 {excluded_krw:,.0f}원</span>"
                    if excluded_krw else "")
        # 총 손익/수익률 (원가가 있는 자산 기준) — 수익=빨강, 손해=파랑
        pnl_line = ""
        if inv_sum > 0:
            tot_pnl = inv_cur_sum - inv_sum
            ret = tot_pnl / inv_sum * 100
            pc = "#cf222e" if tot_pnl > 0 else ("#1f6feb" if tot_pnl < 0 else "#57606a")
            sign = "▲" if tot_pnl > 0 else ("▼" if tot_pnl < 0 else "-")
            pnl_line = (
                f" &nbsp;&nbsp; <b>총 손익</b> "
                f"<span style='color:{pc}; font-size:16px; font-weight:800'>"
                f"{sign} {tot_pnl:+,.0f}원 ({ret:+.2f}%)</span>"
                f"<span style='color:#8b949e; font-size:11px'> · 투입원금 {inv_sum:,.0f}원</span>")
        # 실현손익(매도 누적)
        realized = db.realized_total(self.profile_id)
        rz_line = ""
        if realized:
            rc = "#cf222e" if realized > 0 else ("#1f6feb" if realized < 0 else "#57606a")
            rz_line = (f" &nbsp;&nbsp; <b>실현손익</b> "
                       f"<span style='color:{rc}; font-weight:700'>{realized:+,.0f}원</span>")
        self.summary.setText(
            f"<b>총 평가금액</b> &nbsp; <span style='font-size:18px; font-weight:800'>"
            f"{total:,.0f}원</span>{excl_txt}{pnl_line}{rz_line}<br>"
            f"<span style='color:#57606a'>자산배분 &nbsp; {asset_line}</span>")
        # 자산기록 스냅샷용 최근 값
        self._last_total = total
        self._last_invested = inv_sum
        self._last_pnl = inv_cur_sum - inv_sum
        self._last_breakdown = dict(kind_sum)

        # 투자성향 기반 권장 위험자산 비중
        risk_krw = kind_sum.get("주식", 0) + kind_sum.get("ETF", 0)
        current_risk = (risk_krw / total * 100) if total > 0 else None
        style = self.style_combo.currentText()
        rec = metrics.recommend_allocation(self.age.value(), current_risk, style)
        color = metrics.COLORS.get(rec["level"], "#57606a")
        cur_txt = (f"{current_risk:.1f}%" if current_risk is not None else "-")
        basis = "100 − 나이 기준" if style == "나이 기준" else f"{style} 기준"
        advice = f"<br><span style='color:{color}'>💡 {rec['advice']}</span>" \
            if rec.get("advice") else ""
        self.reco.setText(
            f"<b>권장 위험자산(주식+ETF) 비중</b> "
            f"<span style='color:#8b949e; font-size:11px'>· {basis}</span><br>"
            f"권장 <b>{rec['target']:.0f}%</b> "
            f"<span style='color:#57606a'>(범위 {rec['low']:.0f}~{rec['high']:.0f}%, "
            f"안전자산 {rec['safe_target']:.0f}%)</span><br>"
            f"현재 <b>{cur_txt}</b> → "
            f"<span style='color:{color}; font-weight:700'>{rec['status']}</span>"
            f"{advice}")
        self.reco.setToolTip(
            "고전적인 '100 − 나이 = 주식비중' 규칙을 초보자용으로 보수적으로 적용한 참고치입니다.\n"
            "위험자산=주식+ETF, 안전자산=적금·예금·현금. 개인 상황에 따라 조정하세요.")


# ============================ 실시간 종목 현황 탭 ============================
WATCH_COLS = ["종목", "현재가", "등락", "등락%"]


class RealtimeTab(QWidget):
    def __init__(self, main, profile_id):
        super().__init__()
        self.main = main
        self.profile_id = profile_id
        self.items = []       # [{market, code, name}]
        self.selected = None  # (market, code)
        self._busy = False
        lay = QVBoxLayout(self)

        # 상단 바
        bar = QHBoxLayout()
        self.inp = QLineEdit()
        self.inp.setPlaceholderText("종목 검색 (예: 삼성전자 / 005930 / 애플 / AAPL)")
        self.inp.returnPressed.connect(self._view_from_input)
        view_btn = QPushButton("조회")
        view_btn.clicked.connect(self._view_from_input)
        add_btn = QPushButton("⭐ 관심추가")
        add_btn.clicked.connect(self._add_symbol)
        del_btn = QPushButton("관심삭제")
        del_btn.clicked.connect(self._remove_symbol)
        self.auto_chk = QCheckBox("자동")
        self.auto_chk.setChecked(True)
        self.auto_chk.toggled.connect(self._toggle_auto)
        self.interval = QComboBox()
        self.interval.addItems(["15초", "30초", "60초"])
        self.interval.setCurrentText("30초")
        self.interval.currentTextChanged.connect(self._restart_timer)
        refresh_btn = QPushButton("🔄")
        refresh_btn.clicked.connect(self.refresh_all)
        bar.addWidget(self.inp, 1)
        bar.addWidget(view_btn)
        bar.addWidget(add_btn)
        bar.addWidget(del_btn)
        bar.addWidget(self.auto_chk)
        bar.addWidget(self.interval)
        bar.addWidget(refresh_btn)
        lay.addLayout(bar)

        # 내 보유 종목 바로가기
        self.my_bar = QHBoxLayout()
        self.my_bar.setSpacing(4)
        self._my_label = QLabel("내 보유:")
        self._my_label.setStyleSheet("color:#57606a;")
        self.my_bar.addWidget(self._my_label)
        self.my_bar.addStretch()
        self._my_buttons = []
        lay.addLayout(self.my_bar)

        legend = QLabel("💼 보유(자동) · ⭐ 관심종목(저장) · 목록/검색에서 종목 선택 → 오른쪽에 실시간 시세·차트·지표 분석")
        legend.setStyleSheet("color:#8b949e; font-size:11px;")
        lay.addWidget(legend)

        self.updated = QLabel("")
        self.updated.setStyleSheet("color:#8b949e; font-size:11px;")
        lay.addWidget(self.updated)

        # 좌: 관심종목 표 / 우: 차트
        split = QSplitter()
        self.table = QTableWidget(0, len(WATCH_COLS))
        self.table.setHorizontalHeaderLabels(WATCH_COLS)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.table.verticalHeader().setVisible(False)
        wh = self.table.horizontalHeader()
        wh.setSectionResizeMode(0, QHeaderView.Stretch)          # 종목명이 남는 공간 차지
        for _c in (1, 2, 3):
            wh.setSectionResizeMode(_c, QHeaderView.ResizeToContents)
        self.table.currentCellChanged.connect(self._on_select)
        split.addWidget(self.table)

        right = QWidget()
        rlay = QVBoxLayout(right)
        self.big = QLabel("관심종목을 선택하세요.")
        self.big.setTextFormat(Qt.RichText)
        self.big.setStyleSheet("font-size:16px;")
        rlay.addWidget(self.big)

        # 기간(차트 버전) 선택: 당일=분봉 라인 / 그 외=일봉 캔들(자세히)
        self.period = "당일"
        self._period_btns = {}
        prow = QHBoxLayout()
        prow.setSpacing(4)
        prow.addWidget(QLabel("기간"))
        for label in ("당일", "1주", "1개월", "3개월", "1년", "5년", "전체"):
            b = QPushButton(label)
            b.setCheckable(True)
            b.setChecked(label == "당일")
            b.clicked.connect(lambda _=False, l=label: self._set_period(l))
            self._period_btns[label] = b
            prow.addWidget(b)
        prow.addStretch()
        rlay.addLayout(prow)

        # 기술적 지표(선택) — 일봉 기간에서 가격에 오버레이 / RSI·MACD는 아래 보조창
        self._ind_checks = {}
        irow = QHBoxLayout(); irow.setSpacing(8)
        irow.addWidget(QLabel("지표"))
        for key in ("볼린저밴드", "이동평균", "일목균형표", "RSI", "MACD"):
            cb = QCheckBox(key)
            cb.stateChanged.connect(self._on_indicator_toggle)
            self._ind_checks[key] = cb
            irow.addWidget(cb)
        irow.addStretch()
        rlay.addLayout(irow)

        self.chart = QChart()
        self.chart.legend().hide()
        self.chart.setMargins(QMargins(4, 4, 4, 4))
        self.chart_view = ZoomableChartView(self.chart)
        self.chart_view.setMinimumHeight(300)
        rlay.addWidget(self.chart_view)
        zhint = QLabel("🔍 휠=확대/축소 · 드래그=구간확대 · 우클릭드래그=이동 · 더블클릭=원래대로")
        zhint.setStyleSheet("color:#8b949e; font-size:10px;")
        rlay.addWidget(zhint)

        # 보조지표(RSI/MACD) 창 — 필요할 때만 표시
        self.osc_chart = QChart()
        self.osc_chart.setMargins(QMargins(4, 0, 4, 0))
        self.osc_view = ZoomableChartView(self.osc_chart)
        self.osc_view.setMaximumHeight(160)
        self.osc_view.setVisible(False)
        rlay.addWidget(self.osc_view)

        # 액션 + 총평 + 지표 분석
        actrow = QHBoxLayout()
        self.add_port_btn = QPushButton("＋ 포트폴리오에 추가")
        self.add_port_btn.setEnabled(False)
        self.add_port_btn.clicked.connect(self.add_to_portfolio)
        self.detail_btn = QPushButton("📊 상세 데이터")
        self.detail_btn.setEnabled(False)
        self.detail_btn.setToolTip("목표주가·마진·성장률·재무안정성 등 확장 데이터셋 보기")
        self.detail_btn.clicked.connect(self._show_detail)
        big_btn = QPushButton("⤢ 크게 보기")
        big_btn.setToolTip("차트를 큰 창으로 열어 확대/이동해서 보기")
        big_btn.clicked.connect(self._open_big_chart)
        help_btn = QPushButton("ⓘ 지표 설명")
        help_btn.clicked.connect(lambda: MetricsHelpDialog(self).exec())
        actrow.addWidget(self.add_port_btn)
        actrow.addWidget(self.detail_btn)
        actrow.addWidget(big_btn)
        actrow.addWidget(help_btn)
        actrow.addStretch()
        rlay.addLayout(actrow)

        self.summary = QLabel("")
        self.summary.setTextFormat(Qt.RichText)
        self.summary.setWordWrap(True)
        rlay.addWidget(self.summary)

        self.grid_host = QWidget()
        self.grid = QGridLayout(self.grid_host)
        self.grid.setSpacing(8)
        rlay.addWidget(self.grid_host)
        rlay.addStretch()

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        scroll.setWidget(right)
        split.addWidget(scroll)
        split.setSizes([420, 640])
        lay.addWidget(split, 1)

        note = QLabel("※ 한국 종목은 사실상 실시간, 미국 종목은 약 15분 지연(무료 시세). "
                      "지표는 참고용이며 투자권유가 아닙니다.")
        note.setStyleSheet("color:#8b949e; font-size:11px;")
        lay.addWidget(note)

        self._timer = QTimer(self)
        self._timer.timeout.connect(self.refresh_all)
        self._restart_timer()

    def _interval_ms(self):
        return {"15초": 15000, "30초": 30000, "60초": 60000}.get(
            self.interval.currentText(), 30000)

    def _restart_timer(self, *_):
        self._timer.stop()
        if self.auto_chk.isChecked():
            self._timer.start(self._interval_ms())

    def _toggle_auto(self, on):
        self._restart_timer()

    def showEvent(self, e):
        super().showEvent(e)
        self.main._active_pid = self.profile_id   # 이 프로필을 활성으로
        self._load_my_holdings()
        self.reload_watchlist()
        self._restart_timer()

    def hideEvent(self, e):
        super().hideEvent(e)
        self._timer.stop()   # 안 보이는 탭은 자동갱신 중지

    # --- 내 보유 바로가기 ---
    def _load_my_holdings(self):
        for b in getattr(self, "_my_buttons", []):
            b.setParent(None); b.deleteLater()
        self._my_buttons = []
        try:
            rows = db.list_holdings(self.profile_id)
        except Exception:
            rows = []
        secs = [r for r in rows if r["kind"] in ("주식", "ETF") and r.get("code")]
        self._my_label.setVisible(bool(secs))
        for r in secs:
            b = QPushButton(r["name"])
            b.setStyleSheet("padding:2px 8px;")
            b.setToolTip(f"{r['name']} ({r['code']})")
            m, c = r.get("market") or "KR", r["code"]
            b.clicked.connect(lambda _=False, mm=m, cc=c: self._view_symbol(mm, cc))
            self.my_bar.insertWidget(self.my_bar.count() - 1, b)
            self._my_buttons.append(b)

    # --- 검색으로 조회 ---
    def _view_from_input(self):
        q = self.inp.text().strip()
        if not q:
            return
        m = re.search(r"\((\w{5,6})\)\s*$", q)   # "이름 (코드)" 형태면 코드 추출
        if m:
            q = m.group(1)
        self.summary.setText("조회 중…")
        self._clear_grid()

        def done(res):
            self._view_symbol(res["market"], res["code"], name=res["name"])

        def fail(msg):
            self.summary.setText(f"조회 실패: {msg}")
        run_async(self, datasource.get_quote, done, fail, q)

    def reload_watchlist(self):
        """이 프로필의 보유 주식·ETF(자동) + 저장된 관심종목으로 목록 구성."""
        pid = self.profile_id
        items, seen = [], set()
        # 1) 보유 종목(자동)
        for r in db.list_holdings(pid):
            if r["kind"] in ("주식", "ETF") and r.get("code") and r.get("market"):
                key = r["code"]
                if key not in seen:
                    seen.add(key)
                    items.append({"market": r["market"], "code": r["code"],
                                  "name": r["name"], "source": "held"})
        # 2) 저장된 관심종목
        for w in db.list_watchlist(pid):
            if w.get("code") and w["code"] not in seen:
                seen.add(w["code"])
                items.append({"market": w["market"], "code": w["code"],
                              "name": w["name"], "source": "watch"})
        self.items = items
        self._render_table()
        self.refresh_all()

    def _add_symbol(self):
        q = self.inp.text().strip()
        if not q:
            return
        self.inp.clear()
        pid = self.profile_id

        def done(res):
            db.add_watchlist(pid, res["market"], res["code"], res["name"])
            self.reload_watchlist()

        def fail(msg):
            QMessageBox.warning(self, "추가 실패", msg)
        run_async(self, datasource.get_quote, done, fail, q)

    def _remove_symbol(self):
        row = self.table.currentRow()
        if row < 0 or row >= len(self.items):
            return
        it = self.items[row]
        if it.get("source") == "held":
            QMessageBox.information(
                self, "안내",
                "보유 종목은 여기서 뺄 수 없습니다(포트폴리오에서 관리). "
                "저장한 관심종목만 삭제할 수 있어요.")
            return
        db.delete_watchlist(self.profile_id, it["code"])
        self.reload_watchlist()

    def _render_table(self):
        self.table.setRowCount(len(self.items))
        for i, it in enumerate(self.items):
            flag = "🇰🇷" if it["market"] == "KR" else "🇺🇸"
            mark = "⭐ " if it.get("source") == "watch" else "💼 "
            cell = QTableWidgetItem(f"{mark}{flag} {it['name']}")
            cell.setToolTip(f"{it['name']} ({it['code']}) · "
                            f"{'관심종목' if it.get('source') == 'watch' else '보유'}")
            self.table.setItem(i, 0, cell)
            for c in (1, 2, 3):
                if not self.table.item(i, c):
                    self.table.setItem(i, c, QTableWidgetItem("…"))

    def refresh_all(self):
        if self._busy or not self.items:
            return
        self._busy = True
        snapshot = list(self.items)

        def job():
            out = {}
            for it in snapshot:
                try:
                    out[(it["market"], it["code"])] = datasource.live_price(
                        it["market"], it["code"])
                except Exception:
                    out[(it["market"], it["code"])] = None
            return out

        def done(res):
            self._busy = False
            self._apply_prices(res)
            now = QDateTime.currentDateTime().toString("HH:mm:ss")
            self.updated.setText(f"마지막 갱신: {now}")
            if self.selected:
                self._load_chart(*self.selected)

        def fail(msg):
            self._busy = False
            self.updated.setText(f"갱신 실패: {msg}")
        run_async(self, job, done, fail)

    def _apply_prices(self, res: dict):
        for i, it in enumerate(self.items):
            lp = res.get((it["market"], it["code"]))
            if not lp or lp.get("price") is None:
                continue
            cur = lp["currency"]
            up = lp["change"] > 0
            color = QColor("#cf222e") if up else (
                QColor("#1f6feb") if lp["change"] < 0 else QColor("#57606a"))
            vals = [fmt_money(lp["price"], cur),
                    f"{lp['change']:+,.2f}",
                    f"{lp['change_pct']:+.2f}%"]
            for j, v in enumerate(vals, start=1):
                cell = QTableWidgetItem(v)
                cell.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
                cell.setForeground(color)
                self.table.setItem(i, j, cell)

    def _on_select(self, row, col, *_):
        if row < 0 or row >= len(self.items):
            return
        it = self.items[row]
        self._view_symbol(it["market"], it["code"], name=it["name"])

    def _view_symbol(self, market, code, name=None):
        self.selected = (market, code)
        if name:
            self._view_name = name
        self._load_chart(market, code)
        self._load_analysis(market, code)

    def _set_period(self, label):
        self.period = label
        for l, b in self._period_btns.items():
            b.setChecked(l == label)
        if self.selected:
            self._load_chart(*self.selected)

    # --- 지표 분석 ---
    def _load_analysis(self, market, code):
        def job():
            return datasource.quote_for(market, code)

        def done(q):
            self.quote = q
            self._render_analysis(q)
            self.add_port_btn.setEnabled(True)
            self.detail_btn.setEnabled(True)

        def fail(msg):
            self.summary.setText(f"지표 조회 실패: {msg}")
        run_async(self, job, done, fail)

    def _show_detail(self):
        q = getattr(self, "quote", None)
        if q:
            DetailDialog(self, q).exec()

    def _render_analysis(self, q):
        evals = metrics.evaluate(q)
        ov = metrics.overall(evals)
        ov_color = metrics.COLORS[ov["level"]]
        mc = q.get("market_cap")
        mc_txt = fmt_money(mc, q["currency"]) if mc else "-"
        sector = f" · {q['sector']}" if q.get("sector") else ""
        self.summary.setText(
            f"<b>종합(참고):</b> <span style='color:{ov_color}; font-weight:700'>"
            f"{ov['text']}</span>"
            f"<span style='color:#8b949e; font-size:11px'> · 시가총액 {mc_txt}{sector}</span>")
        self._clear_grid()
        for i, ev in enumerate(evals):
            self.grid.addWidget(MetricCard(ev), i // 3, i % 3)

    def _clear_grid(self):
        while self.grid.count():
            item = self.grid.takeAt(0)
            wdg = item.widget()
            if wdg:
                wdg.deleteLater()

    def add_to_portfolio(self):
        if not getattr(self, "quote", None):
            return
        q = self.quote
        pre = {"kind": "주식", "market": q["market"], "code": q["code"],
               "name": q["name"], "currency": q["currency"],
               "avg_price": q.get("price") or 0, "qty": 0}
        dlg = HoldingDialog(self, pre)
        if dlg.exec() == QDialog.Accepted:
            db.add_holding(dlg.result_data(), self.profile_id)
            pt = self.main.portfolio_tabs.get(self.profile_id)
            if pt:
                pt.reload()
            self._load_my_holdings()
            self.reload_watchlist()

    # --- 자동완성 ---
    def install_completer(self, index):
        if not index:
            return
        self._code_map = {}
        labels = []
        for code, name in index:
            label = f"{name} ({code})"
            labels.append(label)
            self._code_map[label] = code
        pairs = sorted(((l.lower(), l) for l in labels), key=lambda x: x[0])
        self._sorted_lower = [p[0] for p in pairs]
        self._sorted_labels = [p[1] for p in pairs]
        self._comp_model = QStringListModel([], self)
        comp = QCompleter(self._comp_model, self)
        comp.setCaseSensitivity(Qt.CaseInsensitive)
        comp.setFilterMode(Qt.MatchStartsWith)
        comp.setCompletionMode(QCompleter.PopupCompletion)
        comp.setMaxVisibleItems(12)
        comp.activated[str].connect(self._on_completer_pick)
        self._completer = comp
        self.inp.setCompleter(comp)
        self._pending_text = ""
        self._comp_timer = QTimer(self)
        self._comp_timer.setSingleShot(True)
        self._comp_timer.setInterval(120)
        self._comp_timer.timeout.connect(self._run_completion)
        self.inp.textEdited.connect(self._on_text_edited)

    def _on_text_edited(self, text):
        self._pending_text = text
        self._comp_timer.start()

    def _run_completion(self):
        import bisect
        comp = getattr(self, "_completer", None)
        if comp is None:
            return
        t = (self._pending_text or "").strip().lower()
        if len(t) < 1:
            self._comp_model.setStringList([])
            return
        lo = bisect.bisect_left(self._sorted_lower, t)
        out, i, n = [], lo, len(self._sorted_lower)
        while i < n and len(out) < 50 and self._sorted_lower[i].startswith(t):
            out.append(self._sorted_labels[i])
            i += 1
        self._comp_model.setStringList(out)
        if out and self.inp.hasFocus():
            comp.complete()

    def _on_completer_pick(self, label):
        code = getattr(self, "_code_map", {}).get(label)
        if code:
            self.inp.setText(code)
        QTimer.singleShot(0, self._view_from_input)

    def _load_chart(self, market, code):
        item = next((x for x in self.items
                     if x["market"] == market and x["code"] == code), None)
        name = (item["name"] if item else getattr(self, "_view_name", None)) or code
        period = self.period

        def job():
            lp = datasource.live_price(market, code)
            if period == "당일":
                data = datasource.intraday(market, code)
            else:
                data = datasource.history(market, code, period)
            return lp, data

        def done(res):
            lp, data = res
            cur = lp["currency"]
            up = lp["change"] > 0
            col = "#cf222e" if up else ("#1f6feb" if lp["change"] < 0 else "#57606a")
            flag = "🇰🇷" if market == "KR" else "🇺🇸"
            status = ""
            if lp.get("market_status"):
                status = " · 장중" if lp["market_status"] == "OPEN" else " · 장마감"
            self.big.setText(
                f"{flag} <b>{name}</b> ({code}){status} "
                f"<span style='color:#8b949e; font-size:12px'>· {period}</span><br>"
                f"<span style='font-size:22px; font-weight:800'>"
                f"{fmt_money(lp['price'], cur)}</span> "
                f"<span style='color:{col}; font-weight:700'>"
                f"{lp['change']:+,.2f} ({lp['change_pct']:+.2f}%)</span>")
            overlay = any(self._ind_checks[k].isChecked()
                          for k in ("볼린저밴드", "이동평균", "일목균형표"))
            show_osc = (self._ind_checks["RSI"].isChecked()
                        or self._ind_checks["MACD"].isChecked())
            is_intraday = (period == "당일")
            # 데이터를 (xs_ms, highs, lows, closes) 공통 형태로 변환
            if is_intraday:
                xs, closes = [], []
                for ts, pr in data:
                    ms = QDateTime.fromString(str(ts),
                                              "yyyyMMddHHmmss").toMSecsSinceEpoch()
                    xs.append(ms); closes.append(pr)
                highs = lows = closes
            else:
                xs = [QDateTime.fromString(d["date"],
                                           "yyyy-MM-dd").toMSecsSinceEpoch()
                      for d in data]
                closes = [d["close"] for d in data]
                highs = [d["high"] for d in data]
                lows = [d["low"] for d in data]

            try:
                if not xs:
                    self._clear_chart(); self._clear_osc()
                elif overlay:
                    self._draw_price_indicators(xs, highs, lows, closes, col,
                                                is_intraday,
                                                lp.get("prev_close") if is_intraday else None)
                elif is_intraday:
                    self._draw_line(data, lp.get("prev_close"), col)
                elif period in ("5년", "전체"):
                    self._draw_hist_line(data, col)
                else:
                    self._draw_candles(data)
                if xs and show_osc and len(closes) >= 30:
                    self._render_osc(xs, closes, is_intraday)
                else:
                    self._clear_osc()
            except Exception as e:
                # 지표 렌더 오류 시에도 앱이 죽지 않도록 기본 라인으로 폴백
                try:
                    if is_intraday:
                        self._draw_line(data, lp.get("prev_close"), col)
                    else:
                        self._draw_candles(data)
                    self._clear_osc()
                except Exception:
                    pass
                self.big.setText(self.big.text() +
                                 "<br><span style='color:#8b949e;font-size:11px'>"
                                 f"지표 표시 오류: {e}</span>")

        def fail(msg):
            self.big.setText(f"차트 조회 실패: {msg}")
        run_async(self, job, done, fail)

    def _on_indicator_toggle(self, *_):
        # 지표 체크가 바뀌면 현재 종목/기간으로 다시 그린다
        if getattr(self, "selected", None):
            self._load_chart(*self.selected)
        else:
            self._clear_osc()

    def _open_big_chart(self):
        if not self.chart.series():
            QMessageBox.information(self, "안내", "먼저 종목을 선택해 차트를 표시하세요.")
            return
        title = getattr(self, "_view_name", None) or "차트"
        dlg = ChartZoomDialog(self, self.chart, self.chart_view,
                              title=f"{title} · 크게 보기")
        dlg.exec()
        self.chart_view.setChart(self.chart)   # 닫힌 뒤 원래 뷰로 확실히 복귀

    def _clear_chart(self):
        self.chart.removeAllSeries()
        for ax in list(self.chart.axes()):
            self.chart.removeAxis(ax)
        self.chart.setTitle("")
        self.chart.legend().hide()   # 지표 오버레이가 켜질 때만 다시 표시

    def _draw_line(self, pts, prev_close, color_hex):
        """당일 분봉 라인(간단 버전)."""
        self._clear_chart()
        if not pts:
            return
        series = QLineSeries()
        ys = []
        for ts, price in pts:
            ms = QDateTime.fromString(ts, "yyyyMMddHHmmss").toMSecsSinceEpoch()
            series.append(ms, price)
            ys.append(price)
        pen = series.pen()
        pen.setColor(QColor(color_hex))
        pen.setWidth(2)
        series.setPen(pen)
        self.chart.addSeries(series)

        axx = QDateTimeAxis()
        axx.setFormat("HH:mm")
        axx.setTickCount(6)
        axx.setLabelsFont(QFont("Malgun Gothic", 8))
        self.chart.addAxis(axx, Qt.AlignBottom)
        series.attachAxis(axx)

        axy = QValueAxis()
        lo, hi = min(ys), max(ys)
        if prev_close:
            lo, hi = min(lo, prev_close), max(hi, prev_close)
        pad = (hi - lo) * 0.08 or (hi * 0.01 or 1)
        axy.setRange(lo - pad, hi + pad)
        axy.setLabelsFont(QFont("Malgun Gothic", 8))
        self.chart.addAxis(axy, Qt.AlignLeft)
        series.attachAxis(axy)

    def _draw_candles(self, data):
        """일봉 캔들차트(자세히 버전). 한국식: 상승 빨강 / 하락 파랑."""
        self._clear_chart()
        if not data:
            return
        series = QCandlestickSeries()
        series.setIncreasingColor(QColor("#cf222e"))
        series.setDecreasingColor(QColor("#1f6feb"))
        cats, lows, highs = [], [], []
        for d in data:
            s = QCandlestickSet(d["open"], d["high"], d["low"], d["close"])
            series.append(s)
            cats.append(d["date"])
            lows.append(d["low"]); highs.append(d["high"])
        self.chart.addSeries(series)

        axx = QBarCategoryAxis()
        axx.append(cats)
        axx.setLabelsFont(QFont("Malgun Gothic", 7))
        axx.setLabelsAngle(-60)
        # 라벨이 과밀하면 숨기고 제목에 기간 표시
        if len(cats) > 22:
            axx.setLabelsVisible(False)
        self.chart.addAxis(axx, Qt.AlignBottom)
        series.attachAxis(axx)

        axy = QValueAxis()
        lo, hi = min(lows), max(highs)
        pad = (hi - lo) * 0.08 or (hi * 0.01 or 1)
        axy.setRange(lo - pad, hi + pad)
        axy.setLabelsFont(QFont("Malgun Gothic", 8))
        self.chart.addAxis(axy, Qt.AlignLeft)
        series.attachAxis(axy)

        self.chart.setTitle(f"{cats[0]} ~ {cats[-1]}  (일봉 {len(cats)}개)")
        self.chart.setTitleFont(QFont("Malgun Gothic", 8))

    def _draw_hist_line(self, data, color_hex):
        """장기간(5년·전체) 종가 라인. 캔들은 개수가 많아 과밀하므로 라인+다운샘플."""
        self._clear_chart()
        if not data:
            return
        # 점이 너무 많으면 균등 다운샘플(마지막 점은 유지)
        step = max(1, len(data) // 800)
        pts = data[::step]
        if pts[-1] is not data[-1]:
            pts.append(data[-1])
        series = QLineSeries()
        ys = []
        for d in pts:
            ms = QDateTime.fromString(d["date"], "yyyy-MM-dd").toMSecsSinceEpoch()
            series.append(ms, d["close"]); ys.append(d["close"])
        pen = series.pen(); pen.setColor(QColor(color_hex)); pen.setWidth(2)
        series.setPen(pen)
        self.chart.addSeries(series)
        axx = QDateTimeAxis(); axx.setFormat("yy.MM"); axx.setTickCount(7)
        axx.setLabelsFont(QFont("Malgun Gothic", 8))
        self.chart.addAxis(axx, Qt.AlignBottom); series.attachAxis(axx)
        axy = QValueAxis()
        lo, hi = min(ys), max(ys); pad = (hi - lo) * 0.06 or 1
        axy.setRange(lo - pad, hi + pad); axy.setLabelsFont(QFont("Malgun Gothic", 8))
        self.chart.addAxis(axy, Qt.AlignLeft); series.attachAxis(axy)
        self.chart.setTitle(f"{data[0]['date']} ~ {data[-1]['date']}  (종가 {len(data)}일)")
        self.chart.setTitleFont(QFont("Malgun Gothic", 8))

    # ---- 기술적 지표 오버레이/보조창 ----
    def _draw_price_indicators(self, xs, highs, lows, closes, color_hex,
                               is_intraday=False, prev_close=None):
        """가격을 종가 라인으로 그리고 볼밴/이동평균/일목균형표를 안전하게 겹쳐 표시."""
        self._clear_chart()
        if not xs:
            return
        self.chart.legend().show()
        self.chart.legend().setAlignment(Qt.AlignBottom)
        self.chart.legend().setFont(QFont("Malgun Gothic", 7))
        allvals = [c for c in closes if c is not None]
        attach = []

        def add_line(name, ys, color, width=1, dashed=False, X=None):
            XX = X if X is not None else xs
            s = QLineSeries(); s.setName(name)
            n = 0
            for x, y in zip(XX, ys):
                if y is not None:
                    s.append(x, y); n += 1
            if n == 0:               # 빈 시리즈는 추가하지 않는다(크래시 방지)
                return None
            pen = s.pen(); pen.setColor(QColor(color)); pen.setWidth(width)
            if dashed:
                pen.setStyle(Qt.DashLine)
            s.setPen(pen)
            self.chart.addSeries(s); attach.append(s)
            return s

        add_line("종가", closes, color_hex, 2)
        if is_intraday and prev_close:
            add_line("전일종가", [prev_close] * len(xs), "#8b949e", 1, True)
            allvals.append(prev_close)
        axx = QDateTimeAxis()
        axx.setFormat("HH:mm" if is_intraday else "yy.MM")
        axx.setTickCount(7); axx.setLabelsFont(QFont("Malgun Gothic", 8))
        axy = QValueAxis(); axy.setLabelsFont(QFont("Malgun Gothic", 8))
        C = self._ind_checks
        x_hi = xs[-1]

        if C["볼린저밴드"].isChecked() and len(closes) >= 20:
            mid, up, lo = indicators.bollinger(closes)
            add_line("BB상단", up, "#8250df", 1, True)
            add_line("BB중심", mid, "#8250df", 1)
            add_line("BB하단", lo, "#8250df", 1, True)
            allvals += [v for v in up + lo if v is not None]
        if C["이동평균"].isChecked():
            add_line("MA20", indicators.sma(closes, 20), "#1a7f37", 1)
            add_line("MA60", indicators.sma(closes, 60), "#bf8700", 1)
        # 일목은 일봉(H/L 필요)에서만, 데이터가 충분할 때만.
        # 구름(선행스팬 A·B)은 QAreaSeries가 PySide6에서 페인트 중 크래시를 유발하므로
        # 경계선 2개(선행A/선행B)로 그린다.
        if C["일목균형표"].isChecked() and not is_intraday and len(closes) >= 52:
            ich = indicators.ichimoku(highs, lows, closes)
            shift = ich["shift"]
            gap = (xs[-1] - xs[0]) / max(1, len(xs) - 1)
            xs_ext = list(xs) + [xs[-1] + gap * (k + 1) for k in range(shift)]
            spanA = [None] * len(xs_ext)
            spanB = [None] * len(xs_ext)
            for i in range(len(closes)):
                j = i + shift
                if j < len(xs_ext):
                    spanA[j] = ich["spanA"][i]
                    spanB[j] = ich["spanB"][i]
            add_line("선행A", spanA, "#cf222e", 1, True, X=xs_ext)
            add_line("선행B", spanB, "#1f6feb", 1, True, X=xs_ext)
            add_line("전환선", ich["tenkan"], "#e16f24", 1)
            add_line("기준선", ich["kijun"], "#0969da", 1)
            allvals += [v for v in ich["spanA"] + ich["spanB"] if v is not None]
            valid_ext = [xs_ext[k] for k in range(len(xs_ext))
                         if spanA[k] is not None or spanB[k] is not None]
            if valid_ext:
                x_hi = max(x_hi, valid_ext[-1])

        if not attach or not allvals:
            self.chart.legend().hide()
            return
        self.chart.addAxis(axx, Qt.AlignBottom)
        axx.setRange(QDateTime.fromMSecsSinceEpoch(int(xs[0])),
                     QDateTime.fromMSecsSinceEpoch(int(x_hi)))
        lo_v, hi_v = min(allvals), max(allvals)
        pad = (hi_v - lo_v) * 0.08 or (abs(hi_v) * 0.01 or 1)
        axy.setRange(lo_v - pad, hi_v + pad)
        self.chart.addAxis(axy, Qt.AlignLeft)
        for s in attach:
            s.attachAxis(axx); s.attachAxis(axy)
        self.chart.setTitle("지표 오버레이" + (" · 당일" if is_intraday else ""))
        self.chart.setTitleFont(QFont("Malgun Gothic", 8))

    def _render_osc(self, xs, closes, is_intraday=False):
        """보조지표 창: RSI(우선) 또는 MACD."""
        self.osc_chart.removeAllSeries()
        for ax in list(self.osc_chart.axes()):
            self.osc_chart.removeAxis(ax)
        if not xs or len(closes) < 30:
            self.osc_view.setVisible(False); return
        C = self._ind_checks
        self.osc_chart.legend().setVisible(True)
        self.osc_chart.legend().setAlignment(Qt.AlignBottom)
        self.osc_chart.legend().setFont(QFont("Malgun Gothic", 7))
        axx = QDateTimeAxis()
        axx.setFormat("HH:mm" if is_intraday else "yy.MM"); axx.setTickCount(6)
        axx.setLabelsFont(QFont("Malgun Gothic", 7))

        def add(name, vals, color, width=1, dashed=False):
            s = QLineSeries(); s.setName(name)
            n = 0
            for x, y in zip(xs, vals):
                if y is not None:
                    s.append(x, y); n += 1
            if n == 0:
                return None
            pen = s.pen(); pen.setColor(QColor(color)); pen.setWidth(width)
            if dashed:
                pen.setStyle(Qt.DashLine)
            s.setPen(pen); self.osc_chart.addSeries(s); return s

        made = []
        if C["RSI"].isChecked():
            s = add("RSI(14)", indicators.rsi(closes), "#8250df", 1)
            g70 = add("70", [70.0] * len(closes), "#cf222e", 1, True)
            g30 = add("30", [30.0] * len(closes), "#1f6feb", 1, True)
            made = [x for x in (s, g70, g30) if x]
            if made:
                ay = QValueAxis(); ay.setRange(0, 100); ay.setTickCount(3)
                ay.setLabelsFont(QFont("Malgun Gothic", 7))
                self.osc_chart.addAxis(axx, Qt.AlignBottom)
                self.osc_chart.addAxis(ay, Qt.AlignLeft)
                for s in made:
                    s.attachAxis(axx); s.attachAxis(ay)
                self.osc_chart.setTitle("RSI(14) · 70↑ 과매수 / 30↓ 과매도")
        elif C["MACD"].isChecked():
            line, sig, _h = indicators.macd(closes)
            sl = add("MACD", line, "#0969da", 1)
            ss = add("시그널", sig, "#e16f24", 1)
            made = [x for x in (sl, ss) if x]
            if made:
                vv = [v for v in line + sig if v is not None]
                m = (max(abs(min(vv)), abs(max(vv))) if vv else 1) or 1
                ay = QValueAxis(); ay.setRange(-m, m)
                ay.setLabelsFont(QFont("Malgun Gothic", 7))
                self.osc_chart.addAxis(axx, Qt.AlignBottom)
                self.osc_chart.addAxis(ay, Qt.AlignLeft)
                for s in made:
                    s.attachAxis(axx); s.attachAxis(ay)
                self.osc_chart.setTitle("MACD(12,26,9)")
        if not made:
            self.osc_view.setVisible(False); return
        self.osc_chart.setTitleFont(QFont("Malgun Gothic", 8))
        self.osc_view.setVisible(True)

    def _clear_osc(self):
        self.osc_chart.removeAllSeries()
        for ax in list(self.osc_chart.axes()):
            self.osc_chart.removeAxis(ax)
        self.osc_view.setVisible(False)


# ============================ 종목 스크리너 탭 ============================
class _NumItem(QTableWidgetItem):
    """숫자 정렬용 테이블 아이템."""
    def __init__(self, text, value):
        super().__init__(text)
        self._v = value if value is not None else float("-inf")
        self.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)

    def __lt__(self, other):
        try:
            return self._v < other._v
        except Exception:
            return super().__lt__(other)


SCREEN_COLS = ["종목", "현재가", "PER", "PBR", "ROE", "배당", "시총(억)"]


class ScreenerTab(QWidget):
    def __init__(self, main):
        super().__init__()
        self.main = main
        self.data = []       # 불러온 재무 리스트
        self._busy = False
        self._sectors = []
        lay = QVBoxLayout(self)

        # 유니버스 선택
        u = QHBoxLayout()
        u.addWidget(QLabel("시장"))
        self.market = QComboBox()
        self.market.addItems(["전체(국내)", "KOSPI", "KOSDAQ",
                              "미국 주식", "미국 ETF", "미국 전체"])
        self.market.currentTextChanged.connect(self._on_market_changed)
        u.addWidget(self.market)
        u.addWidget(QLabel("개수"))
        self.count = QComboBox(); self.count.addItems(["50", "100", "200", "300"])
        self.count.setCurrentText("100")
        u.addWidget(self.count)
        u.addWidget(QLabel("섹터"))
        self.sector = QComboBox(); self.sector.addItem("전체(시총상위)", None)
        self.sector.setMinimumWidth(160)
        u.addWidget(self.sector)
        self.load_btn = QPushButton("📥 종목 불러오기")
        self.load_btn.clicked.connect(self._load)
        u.addWidget(self.load_btn)
        u.addStretch()
        self.watch_btn = QPushButton("⭐ 선택종목 관심추가")
        self.watch_btn.setToolTip("선택한 종목을 현재 포트폴리오의 관심종목으로 저장")
        self.watch_btn.clicked.connect(self._add_selected_watch)
        u.addWidget(self.watch_btn)
        lay.addLayout(u)

        # 조건 필터
        f = QHBoxLayout()
        def spin(suffix, mx, dec=1):
            s = QDoubleSpinBox(); s.setRange(0, mx); s.setDecimals(dec)
            s.setSpecialValueText("무시"); s.setSuffix(suffix)
            s.valueChanged.connect(self._apply); return s
        f.addWidget(QLabel("PER ≤")); self.f_per = spin("", 1000); f.addWidget(self.f_per)
        f.addWidget(QLabel("PBR ≤")); self.f_pbr = spin("", 100, 2); f.addWidget(self.f_pbr)
        f.addWidget(QLabel("ROE ≥")); self.f_roe = spin("%", 200); f.addWidget(self.f_roe)
        f.addWidget(QLabel("배당 ≥")); self.f_div = spin("%", 30, 2); f.addWidget(self.f_div)
        f.addWidget(QLabel("시총 ≥")); self.f_cap = spin("억", 1e7, 0); f.addWidget(self.f_cap)
        f.addStretch()
        self.count_label = QLabel("")
        self.count_label.setStyleSheet("color:#57606a;")
        f.addWidget(self.count_label)
        lay.addLayout(f)

        self.status = QLabel("‘종목 불러오기’로 조건 검색을 시작하세요. "
                             "(섹터를 고르면 그 업종만 빠르게, ‘전체’는 시총상위 N개)")
        self.status.setStyleSheet("color:#8b949e; font-size:11px;")
        lay.addWidget(self.status)

        # 결과 테이블
        self.table = QTableWidget(0, len(SCREEN_COLS))
        self.table.setHorizontalHeaderLabels(SCREEN_COLS)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSortingEnabled(True)
        self.table.verticalHeader().setVisible(False)
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
        self.table.doubleClicked.connect(self._open_selected)
        lay.addWidget(self.table, 1)

        hint = QLabel("※ 더블클릭=분석 탭으로 이동 · 여러 행 선택 후 ‘⭐ 선택종목 관심추가’로 저장. "
                      "국내는 시총상위/섹터, 미국은 주요 대형주·ETF를 조회합니다(미국은 시총을 원화로 환산). "
                      "ETF는 PER·PBR 등이 없을 수 있어요. 지표는 참고용이며 투자권유가 아닙니다.")
        hint.setStyleSheet("color:#8b949e; font-size:11px;")
        lay.addWidget(hint)

        # 섹터 목록 백그라운드 로딩
        run_async(self, datasource.kr_sectors, self._on_sectors, lambda *_: None)

    _US_KIND = {"미국 주식": "주식", "미국 ETF": "ETF", "미국 전체": "전체"}

    def _on_sectors(self, sectors):
        self._sectors = sectors or []
        for no, name, cnt in self._sectors:
            self.sector.addItem(f"{name} ({cnt})", no)

    def _on_market_changed(self, text):
        # 미국 시장은 섹터·개수 필터가 국내 전용이라 비활성화
        is_us = text in self._US_KIND
        self.sector.setEnabled(not is_us)
        self.count.setEnabled(not is_us)

    def _load(self):
        if self._busy:
            return
        self._busy = True
        self.load_btn.setEnabled(False)
        self.status.setText("종목·재무 불러오는 중… (수십 초 걸릴 수 있어요)")
        mtext = self.market.currentText()
        us_kind = self._US_KIND.get(mtext)
        market = {"전체(국내)": "ALL"}.get(mtext, mtext)
        n = int(self.count.currentText())
        sector_no = self.sector.currentData()

        def job():
            if us_kind:
                tickers = [t for t, _n, _e in datasource.us_universe(us_kind)]
                return datasource.screen_fetch_us(tickers)
            if sector_no is not None:
                codes = [c for c, _ in datasource.kr_sector_stocks(sector_no, 300)]
            else:
                codes = [c for c, _ in datasource.kr_market_leaders(market, n)]
            return datasource.screen_fetch(codes)

        def done(rows):
            self._busy = False
            self.load_btn.setEnabled(True)
            self.data = rows or []
            self.status.setText(f"{len(self.data)}개 종목 불러옴. 위 조건으로 자동 필터됩니다.")
            self._apply()

        def fail(msg):
            self._busy = False
            self.load_btn.setEnabled(True)
            self.status.setText(f"불러오기 실패: {msg}")
        run_async(self, job, done, fail)

    def _apply(self):
        per_max = self.f_per.value() or None
        pbr_max = self.f_pbr.value() or None
        roe_min = self.f_roe.value() or None
        div_min = self.f_div.value() or None
        cap_min = (self.f_cap.value() or 0) * 1e8  # 억 -> 원

        rows = []
        for d in self.data:
            if per_max and (d["per"] is None or d["per"] <= 0 or d["per"] > per_max):
                continue
            if pbr_max and (d["pbr"] is None or d["pbr"] > pbr_max):
                continue
            if roe_min and (d["roe"] is None or d["roe"] < roe_min):
                continue
            if div_min and ((d["div"] or 0) < div_min):
                continue
            if cap_min and ((d["market_cap"] or 0) < cap_min):
                continue
            rows.append(d)

        self.table.setSortingEnabled(False)
        self.table.setRowCount(len(rows))
        for i, d in enumerate(rows):
            cap_eok = (d["market_cap"] or 0) / 1e8
            mkt = d.get("market", "KR")
            flag = "🇺🇸" if mkt == "US" else "🇰🇷"
            cur = d.get("currency", "USD") if mkt == "US" else "KRW"
            tag = " · ETF" if d.get("is_etf") else ""
            cells = [
                (f"{flag} {d['name']}", None),
                (fmt_money(d["price"], cur) if d["price"] else "-", d["price"]),
                (f"{d['per']:.1f}" if d["per"] else "-", d["per"]),
                (f"{d['pbr']:.2f}" if d["pbr"] else "-", d["pbr"]),
                (f"{d['roe']:.1f}%" if d["roe"] is not None else "-", d["roe"]),
                (f"{d['div']:.2f}%" if d["div"] else "-", d["div"]),
                (f"{cap_eok:,.0f}", cap_eok),
            ]
            for c, (text, val) in enumerate(cells):
                if c == 0:
                    it = QTableWidgetItem(text)
                    it.setToolTip(f"{d['name']} ({d['code']}){tag}")
                    it.setData(Qt.UserRole, d["code"])
                    it.setData(Qt.UserRole + 1, mkt)
                else:
                    it = _NumItem(text, val)
                self.table.setItem(i, c, it)
        self.table.setSortingEnabled(True)
        self.count_label.setText(f"조건 충족 {len(rows)} / 불러온 {len(self.data)}")

    def _open_selected(self, *_):
        row = self.table.currentRow()
        if row < 0:
            return
        it0 = self.table.item(row, 0)
        code = it0.data(Qt.UserRole)
        mkt = it0.data(Qt.UserRole + 1) or "KR"
        if not code:
            return
        rt = self.main.active_realtime_tab() or \
            next(iter(self.main.realtime_tabs.values()), None)
        if not rt:   # 열린 실시간 탭이 없으면 활성 프로필을 연다
            self.main._open_profile(self.main.active_profile_id())
            rt = self.main.active_realtime_tab()
        if not rt:
            return
        rt._view_symbol(mkt, code)
        self.main.tabs.setCurrentWidget(rt)

    def _add_selected_watch(self):
        rows = sorted({idx.row() for idx in self.table.selectedIndexes()})
        if not rows:
            QMessageBox.information(self, "안내", "관심종목으로 추가할 종목을 선택하세요.")
            return
        pid = self.main.active_profile_id()
        by_code = {d["code"]: d["name"] for d in self.data}
        names, cnt = [], 0
        for r in rows:
            it = self.table.item(r, 0)
            if not it:
                continue
            code = it.data(Qt.UserRole)
            if not code:
                continue
            mkt = it.data(Qt.UserRole + 1) or "KR"
            db.add_watchlist(pid, mkt, code, by_code.get(code) or it.text())
            names.append(by_code.get(code) or code)
            cnt += 1
        # 해당 프로필의 실시간 탭 목록 즉시 반영
        try:
            rt = self.main.realtime_tabs.get(pid)
            if rt:
                rt.reload_watchlist()
        except Exception:
            pass
        prof = db.get_profile(pid)
        pname = prof["name"] if prof else ""
        self.status.setText(
            f"⭐ 관심종목 {cnt}개 추가됨 → '{pname}' ({', '.join(names[:5])}"
            f"{' 외' if cnt > 5 else ''}). ‘종목·실시간·분석’ 탭에서 확인하세요.")


# ============================ 시장 지표 탭 ============================
_ENERGY = {"CL=F", "BZ=F", "NG=F"}
_METAL = {"GC=F", "SI=F", "HG=F", "PL=F", "PA=F"}


def _market_catalog():
    items = []
    for name, key in datasource.MARKET_INDICES:
        items.append({"kind": "index", "key": key, "name": name, "cat": "지수"})
    for name, key in datasource.COMMODITIES:
        cat = "에너지" if key in _ENERGY else ("금속" if key in _METAL else "농산물")
        items.append({"kind": "commodity", "key": key, "name": name, "cat": cat})
    return items


class MiniChartCard(QFrame):
    """대시보드 카드: 이름·현재값·등락 + 종가 스파크라인. 클릭 시 단일 상세로."""
    def __init__(self, kind, key, name, on_click):
        super().__init__()
        self.kind, self.key, self.name, self._on_click = kind, key, name, on_click
        self.setObjectName("mini")
        self.setStyleSheet("QFrame#mini{border:1px solid #d0d7de; border-radius:8px;}")
        self.setMinimumSize(210, 140); self.setCursor(Qt.PointingHandCursor)
        v = QVBoxLayout(self); v.setContentsMargins(8, 6, 8, 6); v.setSpacing(2)
        self.head = QLabel(name); self.head.setTextFormat(Qt.RichText)
        self.head.setStyleSheet("font-size:12px;")
        v.addWidget(self.head)
        self.chart = QChart(); self.chart.legend().hide()
        self.chart.setMargins(QMargins(0, 0, 0, 0))
        self.chart.layout().setContentsMargins(0, 0, 0, 0)
        self.cv = QChartView(self.chart); self.cv.setRenderHint(QPainter.Antialiasing)
        self.cv.setMinimumHeight(84)
        v.addWidget(self.cv, 1)

    def mouseReleaseEvent(self, e):
        if self._on_click:
            self._on_click(self.kind, self.key)

    def load(self, period):
        self.head.setText(f"{self.name} · …")

        def job():
            lv = datasource.item_live(self.kind, self.key)
            if period == "당일":
                ys = [p for _t, p in datasource.item_intraday(self.kind, self.key)]
            else:
                ys = [c for _d, c in datasource.item_history(self.kind, self.key, period)]
            return lv, ys

        def done(res):
            lv, ys = res
            chg = lv.get("change") or 0
            col = "#cf222e" if chg > 0 else ("#1f6feb" if chg < 0 else "#57606a")
            price = lv.get("price") or (ys[-1] if ys else 0)
            self.head.setText(
                f"<b>{self.name}</b> "
                f"<span style='color:{col}; font-weight:700'>{price:,.2f} "
                f"({lv.get('change_pct', 0):+.2f}%)</span>")
            self._spark(ys, col)

        def fail(msg):
            self.head.setText(f"{self.name} · 실패")
        run_async(self, job, done, fail)

    def _spark(self, ys, color_hex):
        self.chart.removeAllSeries()
        for ax in list(self.chart.axes()):
            self.chart.removeAxis(ax)
        if not ys:
            return
        step = max(1, len(ys) // 300)
        ys = ys[::step]
        s = QLineSeries()
        for i, v in enumerate(ys):
            s.append(i, v)
        pen = s.pen(); pen.setColor(QColor(color_hex)); pen.setWidth(2); s.setPen(pen)
        self.chart.addSeries(s)
        axx = QValueAxis(); axx.setVisible(False)
        self.chart.addAxis(axx, Qt.AlignBottom); s.attachAxis(axx)
        ay = QValueAxis(); ay.setVisible(False)
        lo, hi = min(ys), max(ys); pad = (hi - lo) * 0.08 or 1
        ay.setRange(lo - pad, hi + pad)
        self.chart.addAxis(ay, Qt.AlignLeft); s.attachAxis(ay)


class MarketTab(QWidget):
    def __init__(self, main):
        super().__init__()
        self.main = main
        self.catalog = _market_catalog()
        self.cards = []
        lay = QVBoxLayout(self)
        bar = QHBoxLayout()
        self.mode = QComboBox(); self.mode.addItems(["그리드 대시보드", "단일 상세"])
        self.mode.currentTextChanged.connect(self._switch_mode)
        self.group = QComboBox()
        self.group.addItems(["전체", "지수", "에너지", "금속", "농산물"])
        self.group.currentTextChanged.connect(self._rebuild_grid)
        self.item = QComboBox()
        for it in self.catalog:
            self.item.addItem(it["name"], (it["kind"], it["key"]))
        self.item.currentIndexChanged.connect(lambda *_: self._load_single())
        self.period = QComboBox()
        self.period.addItems(["당일", "3개월", "1년", "3년", "5년", "전체"])
        self.period.setCurrentText("3개월")
        self.period.currentTextChanged.connect(self._reload)
        rf = QPushButton("🔄"); rf.clicked.connect(self._reload)
        bar.addWidget(QLabel("보기")); bar.addWidget(self.mode)
        self.lbl_group = QLabel("분류"); bar.addWidget(self.lbl_group); bar.addWidget(self.group)
        self.lbl_item = QLabel("종목"); bar.addWidget(self.lbl_item); bar.addWidget(self.item)
        bar.addWidget(QLabel("기간")); bar.addWidget(self.period)
        bar.addWidget(rf); bar.addStretch()
        lay.addLayout(bar)

        self.stack = QStackedWidget()
        self.grid_host = QWidget(); self.grid = QGridLayout(self.grid_host)
        self.grid.setSpacing(8)
        gscroll = QScrollArea(); gscroll.setWidgetResizable(True)
        gscroll.setFrameShape(QFrame.NoFrame); gscroll.setWidget(self.grid_host)
        self.stack.addWidget(gscroll)

        single = QWidget(); sv = QVBoxLayout(single)
        srow = QHBoxLayout()
        self.stat = QLabel("불러오는 중…"); self.stat.setTextFormat(Qt.RichText)
        srow.addWidget(self.stat, 1)
        big = QPushButton("⤢ 크게 보기"); big.clicked.connect(self._open_big_chart)
        srow.addWidget(big)
        sv.addLayout(srow)
        self.chart = QChart(); self.chart.legend().hide()
        self.chart.setMargins(QMargins(4, 4, 4, 4))
        self.cv = ZoomableChartView(self.chart)
        sv.addWidget(self.cv, 1)
        zh = QLabel("🔍 휠=확대/축소 · 드래그=구간확대 · 우클릭드래그=이동 · 더블클릭=원래대로")
        zh.setStyleSheet("color:#8b949e; font-size:10px;")
        sv.addWidget(zh)
        self.stack.addWidget(single)
        lay.addWidget(self.stack, 1)

        note = QLabel("※ 지수(코스피·코스닥·S&P500·나스닥·다우) + 주요 원자재(WTI·브렌트·천연가스·금·은·구리·"
                      "백금·팔라듐·곡물 등). ‘그리드’는 여러 종목을 한눈에(카드 클릭=상세), ‘당일’은 분봉. "
                      "두바이유는 무료 시세가 없어 브렌트로 대체합니다. (무료 지연 시세)")
        note.setWordWrap(True); note.setStyleSheet("color:#8b949e; font-size:11px;")
        lay.addWidget(note)
        self._loaded = False
        self._switch_mode(self.mode.currentText())

    def showEvent(self, e):
        super().showEvent(e)
        if not self._loaded:
            self._loaded = True
            self._reload()

    def _switch_mode(self, text):
        grid = (text == "그리드 대시보드")
        self.stack.setCurrentIndex(0 if grid else 1)
        self.lbl_group.setVisible(grid); self.group.setVisible(grid)
        self.lbl_item.setVisible(not grid); self.item.setVisible(not grid)
        if self._loaded:
            self._reload()

    def _reload(self, *_):
        if self.mode.currentText() == "그리드 대시보드":
            self._rebuild_grid()
        else:
            self._load_single()

    def _filtered_items(self):
        g = self.group.currentText()
        return [it for it in self.catalog if g == "전체" or it["cat"] == g]

    def _rebuild_grid(self, *_):
        for c in self.cards:
            c.setParent(None); c.deleteLater()
        self.cards = []
        while self.grid.count():
            w = self.grid.takeAt(0).widget()
            if w:
                w.setParent(None)
        period = self.period.currentText()
        cols = 4
        for i, it in enumerate(self._filtered_items()):
            card = MiniChartCard(it["kind"], it["key"], it["name"], self._open_from_grid)
            self.grid.addWidget(card, i // cols, i % cols)
            self.cards.append(card)
            card.load(period)

    def _open_from_grid(self, kind, key):
        idx = self.item.findData((kind, key))
        if idx >= 0:
            self.item.blockSignals(True)
            self.item.setCurrentIndex(idx)
            self.item.blockSignals(False)
        self.mode.setCurrentText("단일 상세")   # _switch_mode 가 _reload 호출

    def _load_single(self, *_):
        data = self.item.currentData()
        if not data:
            return
        kind, key = data
        name = self.item.currentText()
        period = self.period.currentText()
        self.stat.setText("불러오는 중…")
        if period == "당일":
            self._single_intraday(kind, key, name)
            return

        def job():
            hist = datasource.item_history(kind, key, period)
            val = datasource.index_valuation(key) if kind == "index" else None
            return hist, val

        def done(res):
            hist, val = res
            if not hist:
                self.stat.setText("데이터 없음"); return
            first, last = hist[0][1], hist[-1][1]
            chg = last - first
            pct = chg / first * 100 if first else 0
            col = "#cf222e" if chg > 0 else ("#1f6feb" if chg < 0 else "#57606a")
            extra = ""
            if val:
                if val.get("per"):
                    extra += f" · PER {val['per']:.1f}"
                if val.get("eps"):
                    extra += f" · 추정 EPS {val['eps']:,.1f}"
            self.stat.setText(
                f"<b>{name}</b> "
                f"<span style='font-size:20px; font-weight:800'>{last:,.2f}</span> "
                f"<span style='color:{col}; font-weight:700'>{chg:+,.2f} ({pct:+.2f}%)</span> "
                f"<span style='color:#8b949e; font-size:12px'>· {period} 기준{extra}</span>")
            self._draw(hist, col)

        def fail(msg):
            self.stat.setText(f"조회 실패: {msg}")
        run_async(self, job, done, fail)

    def _single_intraday(self, kind, key, name):
        def job():
            return datasource.item_intraday(kind, key), datasource.item_live(kind, key)

        def done(res):
            pts, lp = res
            if not pts:
                self.stat.setText("장 시작 전이거나 당일 데이터가 아직 없습니다."); return
            chg = lp.get("change") or 0
            col = "#cf222e" if chg > 0 else ("#1f6feb" if chg < 0 else "#57606a")
            price = lp.get("price") or pts[-1][1]
            self.stat.setText(
                f"<b>{name}</b> "
                f"<span style='font-size:20px; font-weight:800'>{price:,.2f}</span> "
                f"<span style='color:{col}; font-weight:700'>"
                f"{chg:+,.2f} ({lp.get('change_pct', 0):+.2f}%)</span> "
                f"<span style='color:#8b949e; font-size:12px'>· 당일 분봉(무료 지연)</span>")
            self._draw_intraday(pts, lp.get("prev_close"), col)

        def fail(msg):
            self.stat.setText(f"조회 실패: {msg}")
        run_async(self, job, done, fail)

    def _open_big_chart(self):
        if not self.chart.series():
            QMessageBox.information(self, "안내", "먼저 종목을 표시하세요."); return
        dlg = ChartZoomDialog(self, self.chart, self.cv,
                              title=f"{self.item.currentText()} · 크게 보기")
        dlg.exec()
        self.cv.setChart(self.chart)

    def _draw_intraday(self, pts, prev_close, color_hex):
        """당일 분봉 라인 + 전일종가 기준선."""
        self.chart.removeAllSeries()
        for ax in list(self.chart.axes()):
            self.chart.removeAxis(ax)
        s = QLineSeries(); ys = []
        for ts, v in pts:
            ms = QDateTime.fromString(ts, "yyyyMMddHHmmss").toMSecsSinceEpoch()
            s.append(ms, v); ys.append(v)
        pen = s.pen(); pen.setColor(QColor(color_hex)); pen.setWidth(2); s.setPen(pen)
        self.chart.addSeries(s)
        base = None
        if prev_close:
            base = QLineSeries()
            base.append(s.at(0).x(), prev_close)
            base.append(s.at(s.count() - 1).x(), prev_close)
            bp = base.pen(); bp.setColor(QColor("#8b949e")); bp.setWidth(1)
            bp.setStyle(Qt.DashLine); base.setPen(bp)
            self.chart.addSeries(base)
        ax = QDateTimeAxis(); ax.setFormat("HH:mm"); ax.setTickCount(7)
        ax.setLabelsFont(QFont("Malgun Gothic", 8))
        self.chart.addAxis(ax, Qt.AlignBottom); s.attachAxis(ax)
        if base:
            base.attachAxis(ax)
        ay = QValueAxis()
        lo, hi = min(ys), max(ys)
        if prev_close:
            lo, hi = min(lo, prev_close), max(hi, prev_close)
        pad = (hi - lo) * 0.08 or 1
        ay.setRange(lo - pad, hi + pad); ay.setLabelsFont(QFont("Malgun Gothic", 8))
        self.chart.addAxis(ay, Qt.AlignLeft); s.attachAxis(ay)
        if base:
            base.attachAxis(ay)

    def _draw(self, hist, color_hex):
        self.chart.removeAllSeries()
        for ax in list(self.chart.axes()):
            self.chart.removeAxis(ax)
        s = QLineSeries()
        ys = []
        step = max(1, len(hist) // 800)   # 전체 등 장기간 다운샘플
        pts = hist[::step]
        if hist and pts[-1] is not hist[-1]:
            pts.append(hist[-1])
        for ymd, v in pts:
            ms = QDateTime.fromString(ymd, "yyyy-MM-dd").toMSecsSinceEpoch()
            s.append(ms, v); ys.append(v)
        pen = s.pen(); pen.setColor(QColor(color_hex)); pen.setWidth(2); s.setPen(pen)
        self.chart.addSeries(s)
        ax = QDateTimeAxis(); ax.setFormat("yy.MM"); ax.setTickCount(7)
        ax.setLabelsFont(QFont("Malgun Gothic", 8))
        self.chart.addAxis(ax, Qt.AlignBottom); s.attachAxis(ax)
        ay = QValueAxis()
        lo, hi = min(ys), max(ys); pad = (hi - lo) * 0.06 or 1
        ay.setRange(lo - pad, hi + pad); ay.setLabelsFont(QFont("Malgun Gothic", 8))
        self.chart.addAxis(ay, Qt.AlignLeft); s.attachAxis(ay)


# ============================ 자산 기록 탭 ============================
class AssetHistoryTab(QWidget):
    _PALETTE = ["#cf222e", "#1f6feb", "#1a7f37", "#bf8700", "#8250df", "#e16f24"]

    def __init__(self, main):
        super().__init__()
        self.main = main
        self.checks = {}   # pid -> QCheckBox
        lay = QVBoxLayout(self)
        bar = QHBoxLayout()
        snap = QPushButton("📸 지금 자산 기록")
        snap.setToolTip("열려있는 각 포트폴리오의 현재 평가액을 오늘 날짜로 저장")
        snap.clicked.connect(self._snapshot)
        bar.addWidget(snap)
        bar.addWidget(QLabel("보기"))
        self.view = QComboBox()
        self.view.addItems(["총자산(규모)", "수익률(%)", "손익(원)", "자산군 비중"])
        self.view.currentTextChanged.connect(self._render)
        bar.addWidget(self.view)
        bar.addWidget(QLabel("집계"))
        self.agg = QComboBox(); self.agg.addItems(["일별", "분기별", "연도별"])
        self.agg.setCurrentText("분기별")
        self.agg.currentTextChanged.connect(self._render)
        bar.addWidget(self.agg)
        bar.addStretch()
        lay.addLayout(bar)

        self.checks_bar = QHBoxLayout()
        self.checks_bar.addWidget(QLabel("표시 프로필:"))
        self.checks_bar.addStretch()
        lay.addLayout(self.checks_bar)

        self.chart = QChart()
        self.chart.setMargins(QMargins(4, 4, 4, 4))
        self.cv = ZoomableChartView(self.chart)
        lay.addWidget(self.cv, 1)
        self.status = QLabel("‘📸 지금 자산 기록’으로 시점을 저장하면 추이가 그려집니다. "
                             "분기·연도별로 자산 변화를 비교하세요. (휠=확대, 우클릭드래그=이동, 더블클릭=원래대로)")
        self.status.setStyleSheet("color:#8b949e; font-size:11px;")
        lay.addWidget(self.status)

    def showEvent(self, e):
        super().showEvent(e)
        self._rebuild_checks()
        self._render()

    def _rebuild_checks(self):
        for c in self.checks.values():
            c.setParent(None); c.deleteLater()
        self.checks = {}
        for p in db.list_profiles():
            cb = QCheckBox(p["name"]); cb.setChecked(True)
            cb.stateChanged.connect(self._render)
            self.checks_bar.insertWidget(self.checks_bar.count() - 1, cb)
            self.checks[p["id"]] = cb

    def _snapshot(self):
        today = QDateTime.currentDateTime().toString("yyyy-MM-dd")
        n = 0
        for pid, pt in self.main.portfolio_tabs.items():
            total = getattr(pt, "_last_total", 0) or 0
            if total <= 0:
                continue
            inv = getattr(pt, "_last_invested", 0) or 0
            pnl = getattr(pt, "_last_pnl", 0) or 0
            ret = (pnl / inv * 100) if inv else 0
            bd = json.dumps(getattr(pt, "_last_breakdown", {}) or {}, ensure_ascii=False)
            db.add_snapshot(pid, today, total, inv, pnl, breakdown=bd, ret=ret)
            n += 1
        self.status.setText(f"{today} 기준 {n}개 포트폴리오 자산 기록 완료.")
        self._render()

    _FIELD = {"총자산(규모)": "total", "수익률(%)": "ret", "손익(원)": "pnl"}

    def _period_key(self, ymd, mode):
        if mode == "일별":
            return ymd
        y, m, _ = ymd.split("-")
        return f"{y}년" if mode == "연도별" else f"{y} {((int(m)-1)//3)+1}Q"

    def _checked_profiles(self):
        out = []
        for p in db.list_profiles():
            cb = self.checks.get(p["id"])
            if cb and not cb.isChecked():
                continue
            out.append(p)
        return out

    def _render(self, *_):
        self.chart.removeAllSeries()
        for ax in list(self.chart.axes()):
            self.chart.removeAxis(ax)
        from PySide6.QtCharts import QBarSeries, QBarSet
        mode = self.agg.currentText()
        view = self.view.currentText()

        if view == "자산군 비중":
            self._render_breakdown(mode)
            return

        field = self._FIELD.get(view, "total")
        cats, all_series = [], []
        ci = 0
        for p in self._checked_profiles():
            snaps = db.list_snapshots(p["id"])
            if not snaps:
                continue
            buckets = {}
            for s in snaps:
                buckets[self._period_key(s["ymd"], mode)] = s.get(field) or 0
            for k in buckets:
                if k not in cats:
                    cats.append(k)
            all_series.append((p["name"], buckets,
                               self._PALETTE[ci % len(self._PALETTE)]))
            ci += 1
        cats.sort()
        if not all_series:
            self.chart.legend().hide()
            self.status.setText("기록이 없습니다. ‘📸 지금 자산 기록’을 눌러 저장하세요.")
            return
        bs = QBarSeries()
        vmin, vmax = 0, 0
        for name, mp, color in all_series:
            bset = QBarSet(name)
            for c in cats:
                v = mp.get(c, 0)
                bset.append(v); vmax = max(vmax, v); vmin = min(vmin, v)
            bset.setColor(QColor(color))
            bs.append(bset)
        self.chart.addSeries(bs)
        axx = QBarCategoryAxis(); axx.append(cats)
        axx.setLabelsFont(QFont("Malgun Gothic", 8))
        self.chart.addAxis(axx, Qt.AlignBottom); bs.attachAxis(axx)
        axy = QValueAxis()
        axy.setRange(min(0, vmin) * 1.1, (vmax * 1.15) or 1)
        axy.setLabelFormat("%,.1f" if field == "ret" else "%,.0f")
        axy.setLabelsFont(QFont("Malgun Gothic", 8))
        self.chart.addAxis(axy, Qt.AlignLeft); bs.attachAxis(axy)
        self.chart.legend().setVisible(True)
        self.chart.legend().setAlignment(Qt.AlignBottom)
        unit = "%" if field == "ret" else "원"
        self.status.setText(f"{view} · {mode} · 프로필 {len(all_series)}개 비교 (단위: {unit})")

    def _render_breakdown(self, mode):
        """자산군 비중: 체크된 프로필 합산, 기간별 자산군 누적막대."""
        from PySide6.QtCharts import QStackedBarSeries, QBarSet
        KINDS = ["주식", "ETF", "적금", "예금", "현금", "기타"]
        colors = {"주식": "#cf222e", "ETF": "#e16f24", "적금": "#1a7f37",
                  "예금": "#1f6feb", "현금": "#8250df", "기타": "#8b949e"}
        # period -> kind -> sum(가장 최신 스냅샷 기준, 프로필 합산)
        per = {}
        for p in self._checked_profiles():
            latest = {}   # periodkey -> (ymd, breakdown dict)
            for s in db.list_snapshots(p["id"]):
                k = self._period_key(s["ymd"], mode)
                try:
                    bd = json.loads(s.get("breakdown") or "{}")
                except Exception:
                    bd = {}
                latest[k] = bd   # 정렬돼 있어 마지막이 최신
            for k, bd in latest.items():
                acc = per.setdefault(k, {})
                for kind, v in bd.items():
                    acc[kind] = acc.get(kind, 0) + (v or 0)
        cats = sorted(per.keys())
        if not cats:
            self.chart.legend().hide()
            self.status.setText("기록이 없습니다. ‘📸 지금 자산 기록’을 눌러 저장하세요.")
            return
        bs = QStackedBarSeries()
        ymax = 0
        for kind in KINDS:
            bset = QBarSet(kind); bset.setColor(QColor(colors[kind]))
            for c in cats:
                bset.append(per[c].get(kind, 0))
            bs.append(bset)
        for c in cats:
            ymax = max(ymax, sum(per[c].values()))
        bs.setLabelsVisible(False)
        self.chart.addSeries(bs)
        axx = QBarCategoryAxis(); axx.append(cats)
        axx.setLabelsFont(QFont("Malgun Gothic", 8))
        self.chart.addAxis(axx, Qt.AlignBottom); bs.attachAxis(axx)
        axy = QValueAxis(); axy.setRange(0, ymax * 1.15 or 1)
        axy.setLabelFormat("%,.0f"); axy.setLabelsFont(QFont("Malgun Gothic", 8))
        self.chart.addAxis(axy, Qt.AlignLeft); bs.attachAxis(axy)
        self.chart.legend().setVisible(True)
        self.chart.legend().setAlignment(Qt.AlignBottom)
        self.status.setText(f"자산군 비중 · {mode} · 누적막대(원). 오래된 기록엔 자산군 정보가 없을 수 있어요.")


# ============================ 월소득 분배 계산기 탭 ============================
class IncomeCalcTab(QWidget):
    CATS = ["저축·투자", "생활비", "주거·고정비", "여가·자기계발", "비상금"]
    PRESETS = {
        "균형형 (기본)": [20, 30, 25, 15, 10],
        "저축강화형": [40, 25, 20, 5, 10],
        "공격투자형": [50, 25, 15, 5, 5],
        "직접입력": None,
    }

    def __init__(self, main):
        super().__init__()
        self.main = main
        self.rows = []       # [(name_edit, pct_spin, amt_spin)]
        self._sync = False   # 재귀 갱신 방지
        self._loading = False  # 프로필 불러오는 중엔 저장 안 함
        lay = QVBoxLayout(self)
        pbar = QHBoxLayout()
        pbar.addWidget(QLabel("프로필"))
        self.profile = QComboBox(); self.profile.setMinimumWidth(160)
        self.profile.currentIndexChanged.connect(self._on_profile_changed)
        pbar.addWidget(self.profile)
        pinfo = QLabel("계획은 선택한 프로필에 자동 저장됩니다.")
        pinfo.setStyleSheet("color:#8b949e; font-size:11px;")
        pbar.addWidget(pinfo)
        pbar.addStretch()
        lay.addLayout(pbar)
        top = QHBoxLayout()
        top.addWidget(QLabel("월 소득(원)"))
        self.income = QDoubleSpinBox(); self.income.setRange(0, 1e12)
        self.income.setDecimals(0); self.income.setGroupSeparatorShown(True)
        self.income.setValue(3000000)
        self.income.valueChanged.connect(self._income_changed)
        top.addWidget(self.income)
        top.addWidget(QLabel("배분방식"))
        self.preset = QComboBox(); self.preset.addItems(list(self.PRESETS.keys()))
        self.preset.currentTextChanged.connect(self._apply_preset)
        top.addWidget(self.preset)
        top.addStretch()
        add_btn = QPushButton("＋ 항목 추가"); add_btn.clicked.connect(lambda: self._add_row("", 0))
        del_btn = QPushButton("－ 선택 삭제"); del_btn.clicked.connect(self._del_row)
        top.addWidget(add_btn); top.addWidget(del_btn)
        lay.addLayout(top)

        self.table = QTableWidget(0, 3)
        self.table.setHorizontalHeaderLabels(["항목(직접 수정)", "비율(%)", "월 금액(원, 직접입력)"])
        self.table.verticalHeader().setVisible(False)
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
        lay.addWidget(self.table)

        self.summary = QLabel("")
        self.summary.setTextFormat(Qt.RichText)
        self.summary.setStyleSheet(
            "background:#f6f8fa; border:1px solid #d0d7de; border-radius:8px; padding:10px;")
        lay.addWidget(self.summary)
        lay.addStretch()
        note = QLabel("※ 비율(%)이나 금액(원) 아무 쪽이나 입력하면 다른 쪽이 자동 계산됩니다. "
                      "‘＋ 항목 추가’로 항목을 넣고 이름도 직접 수정하세요. "
                      "이름에 ‘저축/투자’가 들어간 항목은 투자금으로 합산됩니다.")
        note.setWordWrap(True); note.setStyleSheet("color:#8b949e; font-size:11px;")
        lay.addWidget(note)

        self._rebuild_profiles()

    def showEvent(self, e):
        super().showEvent(e)
        self._rebuild_profiles()

    def _rebuild_profiles(self):
        """프로필 목록을 다시 채우고 현재 프로필 계획을 불러온다."""
        self._loading = True
        prev = self.profile.currentData()
        self.profile.blockSignals(True)
        self.profile.clear()
        profs = db.list_profiles()
        for p in profs:
            self.profile.addItem(p["name"], p["id"])
        # 이전 선택 유지, 없으면 활성 프로필
        target = prev if prev in [p["id"] for p in profs] else self.main.active_profile_id()
        idx = self.profile.findData(target)
        if idx >= 0:
            self.profile.setCurrentIndex(idx)
        self.profile.blockSignals(False)
        self._loading = False
        self._load_plan()

    def _current_pid(self):
        pid = self.profile.currentData()
        return pid if pid is not None else self.main.active_profile_id()

    def _on_profile_changed(self, *_):
        if not self._loading:
            self._load_plan()

    def _load_plan(self):
        """선택 프로필의 저장된 배분 계획을 불러온다(없으면 기본 프리셋)."""
        self._loading = True
        raw = db.get_setting(f"income_plan_{self._current_pid()}")
        plan = None
        if raw:
            try:
                plan = json.loads(raw)
            except Exception:
                plan = None
        if not plan or not plan.get("items"):
            self.profile.setToolTip("")
            self._loading = False
            self._apply_preset("균형형 (기본)")
            return
        self.income.blockSignals(True)
        self.income.setValue(plan.get("income", 3000000))
        self.income.blockSignals(False)
        self.preset.blockSignals(True)
        self.preset.setCurrentText(plan.get("preset", "직접입력"))
        self.preset.blockSignals(False)
        self.table.setRowCount(0)
        self.rows = []
        for it in plan["items"]:
            self._add_row(it.get("name", ""), it.get("pct", 0), mark_custom=False)
        self._loading = False
        self._refresh_summary()

    def _save_plan(self):
        if self._loading:
            return
        plan = {
            "income": self.income.value(),
            "preset": self.preset.currentText(),
            "items": [{"name": ne.text(), "pct": sp.value()}
                      for ne, sp, _amt in self.rows],
        }
        db.set_setting(f"income_plan_{self._current_pid()}",
                       json.dumps(plan, ensure_ascii=False))

    def _add_row(self, name, pct, mark_custom=True):
        r = self.table.rowCount()
        self.table.insertRow(r)
        ne = QLineEdit(name); ne.setPlaceholderText("항목 이름")
        ne.textChanged.connect(self._refresh_summary)
        self.table.setCellWidget(r, 0, ne)
        sp = QDoubleSpinBox(); sp.setRange(0, 100); sp.setDecimals(1); sp.setSuffix(" %")
        sp.setValue(pct)
        self.table.setCellWidget(r, 1, sp)
        amt = QDoubleSpinBox(); amt.setRange(0, 1e12); amt.setDecimals(0)
        amt.setGroupSeparatorShown(True); amt.setValue(self.income.value() * pct / 100)
        self.table.setCellWidget(r, 2, amt)
        # 양방향: %변경→금액, 금액변경→%
        sp.valueChanged.connect(lambda _v, s=sp, a=amt: self._pct_changed(s, a))
        amt.valueChanged.connect(lambda _v, s=sp, a=amt: self._amt_changed(s, a))
        self.rows.append((ne, sp, amt))
        if mark_custom and self.preset.currentText() != "직접입력":
            self.preset.blockSignals(True); self.preset.setCurrentText("직접입력")
            self.preset.blockSignals(False)
        self._refresh_summary()

    def _pct_changed(self, sp, amt):
        if self._sync:
            return
        self._sync = True
        amt.setValue(self.income.value() * sp.value() / 100)
        self._sync = False
        self._refresh_summary()

    def _amt_changed(self, sp, amt):
        if self._sync:
            return
        self._sync = True
        inc = self.income.value()
        sp.setValue((amt.value() / inc * 100) if inc else 0)
        self._sync = False
        self._refresh_summary()

    def _income_changed(self, *_):
        # 소득이 바뀌면 현재 비율(%) 유지하며 금액 재계산
        self._sync = True
        for _ne, sp, amt in self.rows:
            amt.setValue(self.income.value() * sp.value() / 100)
        self._sync = False
        self._refresh_summary()

    def _del_row(self):
        r = self.table.currentRow()
        if r < 0:
            return
        self.table.removeRow(r)
        del self.rows[r]
        self.preset.blockSignals(True); self.preset.setCurrentText("직접입력")
        self.preset.blockSignals(False)
        self._refresh_summary()

    def _apply_preset(self, name):
        vals = self.PRESETS.get(name)
        if not vals:
            return   # 직접입력: 현재 유지
        self.table.setRowCount(0)
        self.rows = []
        for cat, v in zip(self.CATS, vals):
            self._add_row(cat, v, mark_custom=False)
        self._refresh_summary()

    def _refresh_summary(self, *_):
        income = self.income.value()
        tot_pct = 0.0
        tot_amt = 0.0
        save = 0.0
        for ne, sp, amt in self.rows:
            tot_pct += sp.value()
            tot_amt += amt.value()
            if ("저축" in ne.text()) or ("투자" in ne.text()):
                save += amt.value()
        warn = "" if abs(tot_pct - 100) < 0.5 else \
            f" <span style='color:#cf222e'>(합계 {tot_pct:.0f}% / {tot_amt:,.0f}원 — 100%로 맞추세요)</span>"
        self.summary.setText(
            f"월 소득 <b>{income:,.0f}원</b> · 배분합계 <b>{tot_pct:.0f}%</b> "
            f"({tot_amt:,.0f}원){warn}<br>"
            f"<span style='color:#1a7f37; font-weight:700'>저축·투자 합계 {save:,.0f}원/월 "
            f"(연 {save*12:,.0f}원)</span>")
        self._save_plan()


# ============================ 메인 윈도우 ============================
_OPEN_WINDOWS = []   # 보조 창 참조 유지(GC 방지)


class MainWindow(QMainWindow):
    def __init__(self, is_secondary=False, only_pid=None):
        super().__init__()
        self.is_secondary = is_secondary
        self._only_pid = only_pid   # 보조 창: 이 프로필만 표시(공유설정 안 건드림)
        title = "주식 포트폴리오 매니저"
        self.setWindowTitle(title + ("  ·  새 창" if is_secondary else ""))
        self.resize(1080, 720)
        self.tabs = QTabWidget()
        self.tabs.setMovable(True)
        self.tabs.setTabsClosable(True)
        self.tabs.tabCloseRequested.connect(self._on_tab_close)
        self.portfolio_tabs = {}          # pid -> PortfolioTab
        self.realtime_tabs = {}           # pid -> RealtimeTab
        self._active_pid = only_pid or db.current_profile_id()
        self._comp_index = None
        if only_pid:
            self.open_pids = [only_pid]
        else:
            self._load_open_pids()
        self.screener_tab = ScreenerTab(self)
        self.market_tab = MarketTab(self)
        self.asset_tab = AssetHistoryTab(self)
        self.income_tab = IncomeCalcTab(self)
        # 항상 유지되는(닫기 불가) 탭들 — 프로필 탭 뒤에 배치
        self._persistent = [
            (self.screener_tab, "  스크리너  "),
            (self.market_tab, "  시장지표  "),
            (self.asset_tab, "  자산기록  "),
            (self.income_tab, "  월소득계산기  "),
        ]
        self.rebuild_profile_tabs()       # 열려있는 프로필의 포트폴리오+실시간 탭 생성
        self.setCentralWidget(self.tabs)
        # 우상단 '＋ 프로필 열기/추가' 버튼
        self._make_corner_button()
        # 종목목록(국내+미국)을 백그라운드로 미리 로딩 후 자동완성 설치
        run_async(self, datasource.completer_index,
                  self._on_completer_index, lambda *_: None)
        # 자동 업데이트 확인(백그라운드, 실패해도 무시) — 주 창에서만
        if not is_secondary:
            run_async(self, updater.check_latest_verbose,
                      self._on_update_startup, lambda *_: None)

    def _on_update_startup(self, info):
        """시작 시: 새 버전이 있을 때만 조용히 안내(그 외엔 아무 것도 안 함)."""
        if info and info.get("status") == "update":
            self._prompt_update(info)

    def _check_update_manual(self):
        """사용자가 '업데이트 확인'을 눌렀을 때: 모든 결과를 눈에 보이게 안내."""
        def done(info):
            st = (info or {}).get("status")
            if st == "update":
                self._prompt_update(info)
            elif st == "latest":
                QMessageBox.information(
                    self, "업데이트 확인",
                    f"이미 최신 버전입니다. (현재 {version.APP_VERSION})")
            else:
                QMessageBox.warning(
                    self, "업데이트 확인 실패",
                    "업데이트를 확인하지 못했습니다.\n"
                    f"사유: {(info or {}).get('error', '알 수 없음')}\n\n"
                    "인터넷 연결을 확인하거나 잠시 후 다시 시도하세요.")
        QMessageBox.information(self, "업데이트 확인", "최신 버전을 확인하는 중…")
        run_async(self, updater.check_latest_verbose, done, lambda *_: None)

    def _prompt_update(self, info):
        notes = ("\n\n" + info["notes"][:300]) if info.get("notes") else ""
        ret = QMessageBox.question(
            self, "업데이트 있음",
            f"새 버전 {info['version']} 이(가) 있습니다. 지금 업데이트할까요?\n"
            f"(현재 {version.APP_VERSION}){notes}",
            QMessageBox.Yes | QMessageBox.No)
        if ret == QMessageBox.Yes:
            if updater.download_and_launch(info["url"]):
                QMessageBox.information(
                    self, "업데이트", "설치 관리자를 실행합니다. 앱이 종료된 뒤 "
                    "설치를 진행하세요.")
                QApplication.quit()
            else:
                QMessageBox.warning(self, "업데이트 실패",
                                    "다운로드에 실패했습니다. 잠시 후 다시 시도하세요.")

    def _on_completer_index(self, index):
        self._comp_index = index
        for rt in self.realtime_tabs.values():
            rt.install_completer(index)

    # --- 열려있는 프로필 상태 ---
    def _load_open_pids(self):
        existing = [p["id"] for p in db.list_profiles()]
        val = db.get_setting("open_profiles")
        pids = []
        if val:
            pids = [int(x) for x in str(val).split(",")
                    if x.strip().isdigit() and int(x) in existing]
        self.open_pids = pids or existing[:]   # 저장값 없으면 전부 열기

    def _save_open_pids(self):
        if getattr(self, "is_secondary", False):
            return   # 보조 창은 공유 설정(마지막 열린 프로필)을 건드리지 않음
        db.set_setting("open_profiles", ",".join(str(p) for p in self.open_pids))

    def open_in_new_window(self, only_pid=None):
        """현재 상태와 독립된 새 창을 연다(같은 DB 공유). only_pid 지정 시 그 프로필만."""
        w = MainWindow(is_secondary=True, only_pid=only_pid)
        _OPEN_WINDOWS.append(w)
        w.show()
        w.raise_()
        w.activateWindow()
        return w

    def closeEvent(self, e):
        try:
            if self in _OPEN_WINDOWS:
                _OPEN_WINDOWS.remove(self)
        except Exception:
            pass
        super().closeEvent(e)

    def _make_corner_button(self):
        cont = QWidget()
        row = QHBoxLayout(cont)
        row.setContentsMargins(0, 0, 4, 0); row.setSpacing(4)
        btn = QToolButton()
        btn.setText("＋ 프로필")
        btn.setPopupMode(QToolButton.InstantPopup)
        menu = QMenu(btn)
        menu.aboutToShow.connect(lambda: self._fill_profile_menu(menu))
        btn.setMenu(menu)
        row.addWidget(btn)
        winbtn = QToolButton()
        winbtn.setText("🗗 새 창")
        winbtn.setToolTip("현재 창과 독립된 새 창 열기(프로필을 나란히 보기)")
        winbtn.clicked.connect(lambda: self.open_in_new_window())
        row.addWidget(winbtn)
        self.tabs.setCornerWidget(cont, Qt.TopRightCorner)

    def _fill_profile_menu(self, menu):
        menu.clear()
        closed = [p for p in db.list_profiles() if p["id"] not in self.open_pids]
        if closed:
            menu.addAction("● 프로필 열기").setEnabled(False)
            for p in closed:
                menu.addAction(f"    {p['name']}").triggered.connect(
                    lambda _=False, pid=p["id"]: self._open_profile(pid))
            menu.addSeparator()
        # 새 창으로 열기(모든 프로필 대상)
        allp = db.list_profiles()
        if allp:
            sub = menu.addMenu("🗗 새 창으로 열기")
            for p in allp:
                sub.addAction(p["name"]).triggered.connect(
                    lambda _=False, pid=p["id"]: self.open_in_new_window(only_pid=pid))
        menu.addAction("＋ 새 프로필…").triggered.connect(self._new_profile_dialog)
        menu.addSeparator()
        menu.addAction("⟳ 업데이트 확인").triggered.connect(self._check_update_manual)

    def _open_profile(self, pid):
        if pid not in self.open_pids:
            self.open_pids.append(pid)
            self._save_open_pids()
        self.rebuild_profile_tabs(select_pid=pid)

    def _new_profile_dialog(self):
        name, ok = QInputDialog.getText(self, "새 프로필", "프로필 이름:")
        if not ok or not name.strip():
            return
        pid = db.add_profile(name.strip(), age=30)
        self._open_profile(pid)

    def _on_tab_close(self, index):
        w = self.tabs.widget(index)
        pid = getattr(w, "profile_id", None)
        if pid is None:
            return   # 스크리너 등은 닫지 않음
        if pid in self.open_pids:
            self.open_pids.remove(pid)
            self._save_open_pids()
        self.rebuild_profile_tabs()

    # --- 다중 프로필 탭(포트폴리오 + 실시간 짝) ---
    def rebuild_profile_tabs(self, select_pid=None):
        # 삭제된 프로필 정리 + select_pid 자동 열기
        existing = {p["id"] for p in db.list_profiles()}
        self.open_pids = [p for p in self.open_pids if p in existing]
        if select_pid and select_pid not in self.open_pids and select_pid in existing:
            self.open_pids.append(select_pid)

        # 기존 프로필 탭 제거(스크리너는 유지)
        for d in (self.portfolio_tabs, self.realtime_tabs):
            for t in list(d.values()):
                idx = self.tabs.indexOf(t)
                if idx >= 0:
                    self.tabs.removeTab(idx)
                t.deleteLater()
        self.portfolio_tabs = {}
        self.realtime_tabs = {}

        # 상시 탭(스크리너·시장지표·자산기록·계산기)이 없으면 추가하고 닫기버튼 제거
        for w, title in getattr(self, "_persistent", []):
            if self.tabs.indexOf(w) < 0:
                self.tabs.addTab(w, title)
                self._disable_close(w)

        profs = [p for p in db.list_profiles() if p["id"] in self.open_pids]
        pos = 0
        for p in profs:
            pt = PortfolioTab(self, p["id"])
            self.portfolio_tabs[p["id"]] = pt
            self.tabs.insertTab(pos, pt, f"📁 {p['name']}"); pos += 1
            rt = RealtimeTab(self, p["id"])
            self.realtime_tabs[p["id"]] = rt
            if self._comp_index:
                rt.install_completer(self._comp_index)
            self.tabs.insertTab(pos, rt, f"📈 {p['name']}"); pos += 1
        if select_pid and select_pid in self.portfolio_tabs:
            self._active_pid = select_pid
            self.tabs.setCurrentWidget(self.portfolio_tabs[select_pid])
        elif profs and self._active_pid not in self.portfolio_tabs:
            self._active_pid = profs[0]["id"]

    def _disable_close(self, widget):
        idx = self.tabs.indexOf(widget)
        if idx >= 0:
            for side in (QTabBar.RightSide, QTabBar.LeftSide):
                b = self.tabs.tabBar().tabButton(idx, side)
                if b:
                    b.resize(0, 0)
                    b.hide()

    # 하위호환 별칭
    def rebuild_portfolio_tabs(self, select_pid=None):
        self.rebuild_profile_tabs(select_pid)

    def active_profile_id(self):
        if self._active_pid in self.portfolio_tabs:
            return self._active_pid
        return next(iter(self.portfolio_tabs), db.current_profile_id())

    def active_portfolio_tab(self):
        return self.portfolio_tabs.get(self.active_profile_id())

    def active_realtime_tab(self):
        return self.realtime_tabs.get(self.active_profile_id())


def main():
    db.init()
    app = QApplication(sys.argv)
    app.setFont(QFont("Malgun Gothic", 10))
    app.setStyleSheet("""
        QPushButton { padding:6px 12px; border:1px solid #d0d7de; border-radius:6px;
                      background:#f6f8fa; }
        QPushButton:hover { background:#eef1f4; }
        QPushButton:disabled { color:#a0a6ac; }
        QTableWidget { gridline-color:#eaeef2; }
        QHeaderView::section { background:#f6f8fa; padding:6px; border:none;
                               border-bottom:1px solid #d0d7de; font-weight:600; }
        QLineEdit, QComboBox, QDoubleSpinBox { padding:5px; border:1px solid #d0d7de;
                               border-radius:6px; }
        QTabBar::tab { padding:8px 4px; }
    """)
    w = MainWindow()
    _OPEN_WINDOWS.append(w)   # 주 창 참조 유지
    w.show()
    sys.exit(app.exec())


def _setup_crash_log():
    """pythonw(콘솔 없음) 실행에서도 크래시/예외를 로그 파일에 기록."""
    try:
        import faulthandler
        log_path = os.path.join(certs._APPDIR, "crash.log")
        os.makedirs(certs._APPDIR, exist_ok=True)
        f = open(log_path, "w", encoding="utf-8")
        faulthandler.enable(file=f)   # 세그폴트 등 네이티브 크래시도 기록

        def hook(t, v, tb):
            import traceback as _tb
            _tb.print_exception(t, v, tb, file=f); f.flush()
            _tb.print_exception(t, v, tb)
        sys.excepthook = hook
        return log_path
    except Exception:
        return None


if __name__ == "__main__":
    _log = _setup_crash_log()
    try:
        main()
    except Exception:
        traceback.print_exc()
        if _log:
            print(f"오류 기록: {_log}")
        input("오류가 발생했습니다. Enter를 눌러 종료...")
