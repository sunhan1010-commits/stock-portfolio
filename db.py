"""
포트폴리오 로컬 저장 (SQLite). 인터넷이 없어도 보유내역은 유지된다.
- 여러 '프로필'(포트폴리오)을 저장할 수 있고, 각 프로필은 자체 보유내역과 나이를 가진다.
- 마지막에 사용한 프로필을 기억해 다음 실행 시 자동으로 불러온다.
"""
from __future__ import annotations
import os
import shutil
import sqlite3

_HERE = os.path.dirname(os.path.abspath(__file__))
# DB는 영구 위치(LOCALAPPDATA)에 저장 — exe로 묶여 소스가 임시폴더에 풀려도 데이터 유지.
_APPDIR = os.path.join(os.environ.get("LOCALAPPDATA") or _HERE, "StockPortfolioManager")
os.makedirs(_APPDIR, exist_ok=True)
DB_PATH = os.path.join(_APPDIR, "portfolio.db")

# 이 PC의 기존 데이터(프로젝트 폴더 portfolio.db)를 최초 1회 이전
_OLD_DB = os.path.join(_HERE, "portfolio.db")
if (not os.path.exists(DB_PATH) and os.path.exists(_OLD_DB)
        and os.path.abspath(_OLD_DB) != os.path.abspath(DB_PATH)):
    try:
        shutil.copy2(_OLD_DB, DB_PATH)
    except Exception:
        pass

# 자산 종류
KINDS = ["주식", "ETF", "적금", "예금", "현금", "기타"]

_HOLDING_COLS = ("kind", "market", "code", "name", "qty", "avg_price",
                 "amount", "currency", "memo", "sort", "included", "principal")


def _conn():
    c = sqlite3.connect(DB_PATH)
    c.row_factory = sqlite3.Row
    return c


def init():
    with _conn() as c:
        c.execute("""
            CREATE TABLE IF NOT EXISTS holdings (
                id        INTEGER PRIMARY KEY AUTOINCREMENT,
                kind      TEXT NOT NULL,
                market    TEXT,
                code      TEXT,
                name      TEXT NOT NULL,
                qty       REAL DEFAULT 0,
                avg_price REAL DEFAULT 0,
                amount    REAL DEFAULT 0,
                currency  TEXT DEFAULT 'KRW',
                memo      TEXT DEFAULT '',
                sort      INTEGER DEFAULT 0
            )
        """)
        c.execute("""
            CREATE TABLE IF NOT EXISTS profiles (
                id      INTEGER PRIMARY KEY AUTOINCREMENT,
                name    TEXT NOT NULL,
                age     INTEGER DEFAULT 30,
                sort    INTEGER DEFAULT 0
            )
        """)
        c.execute("CREATE TABLE IF NOT EXISTS settings "
                  "(key TEXT PRIMARY KEY, value TEXT)")
        c.execute("""
            CREATE TABLE IF NOT EXISTS asset_history (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                profile_id INTEGER,
                ymd        TEXT,          -- 'YYYY-MM-DD'
                total      REAL DEFAULT 0,
                invested   REAL DEFAULT 0,
                pnl        REAL DEFAULT 0
            )
        """)
        c.execute("""
            CREATE TABLE IF NOT EXISTS watchlist (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                profile_id INTEGER,
                market     TEXT,
                code       TEXT,
                name       TEXT,
                sort       INTEGER DEFAULT 0
            )
        """)
        c.execute("""
            CREATE TABLE IF NOT EXISTS transactions (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                profile_id INTEGER,
                ymd        TEXT,
                market     TEXT,
                code       TEXT,
                name       TEXT,
                qty        REAL,
                buy_price  REAL,       -- 매도 시점 평균 매입단가(현지통화)
                sell_price REAL,       -- 매도 단가(현지통화)
                currency   TEXT DEFAULT 'KRW',
                realized   REAL,       -- 실현손익(원화 환산)
                memo       TEXT DEFAULT ''
            )
        """)

        # --- 마이그레이션: 컬럼 추가 ---
        cols = [r["name"] for r in c.execute("PRAGMA table_info(holdings)")]
        if "profile_id" not in cols:
            c.execute("ALTER TABLE holdings ADD COLUMN profile_id INTEGER")
        if "included" not in cols:
            c.execute("ALTER TABLE holdings ADD COLUMN included INTEGER DEFAULT 1")
        if "principal" not in cols:
            c.execute("ALTER TABLE holdings ADD COLUMN principal REAL DEFAULT 0")
        pcols = [r["name"] for r in c.execute("PRAGMA table_info(profiles)")]
        if "style" not in pcols:
            c.execute("ALTER TABLE profiles ADD COLUMN style TEXT DEFAULT '나이 기준'")
        acols = [r["name"] for r in c.execute("PRAGMA table_info(asset_history)")]
        if "breakdown" not in acols:
            c.execute("ALTER TABLE asset_history ADD COLUMN breakdown TEXT DEFAULT ''")
        if "ret" not in acols:
            c.execute("ALTER TABLE asset_history ADD COLUMN ret REAL DEFAULT 0")

        # --- 기본 프로필 보장 + 기존(무소속) 보유내역 이전 ---
        cnt = c.execute("SELECT COUNT(*) AS n FROM profiles").fetchone()["n"]
        if cnt == 0:
            # 기존 age 설정이 있으면 승계
            row = c.execute("SELECT value FROM settings WHERE key='age'").fetchone()
            age = int(row["value"]) if row and str(row["value"]).isdigit() else 30
            cur = c.execute(
                "INSERT INTO profiles(name, age, sort) VALUES(?,?,0)",
                ("내 포트폴리오", age))
            default_id = cur.lastrowid
        else:
            default_id = c.execute(
                "SELECT id FROM profiles ORDER BY sort, id LIMIT 1").fetchone()["id"]

        # profile_id 가 없는 기존 보유내역을 기본 프로필로
        c.execute("UPDATE holdings SET profile_id=? WHERE profile_id IS NULL",
                  (default_id,))

        # 현재 프로필 설정 보장
        row = c.execute("SELECT value FROM settings WHERE key='current_profile_id'").fetchone()
        if not row or not c.execute(
                "SELECT 1 FROM profiles WHERE id=?", (row["value"],)).fetchone():
            c.execute("INSERT INTO settings(key,value) VALUES('current_profile_id',?) "
                      "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                      (str(default_id),))


# ----------------------------- 설정 -----------------------------
def get_setting(key: str, default=None):
    with _conn() as c:
        row = c.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return row["value"] if row else default


def set_setting(key: str, value):
    with _conn() as c:
        c.execute("INSERT INTO settings(key,value) VALUES(?,?) "
                  "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                  (key, str(value)))


# ----------------------------- 프로필 -----------------------------
def list_profiles() -> list[dict]:
    with _conn() as c:
        return [dict(r) for r in c.execute(
            "SELECT * FROM profiles ORDER BY sort, id")]


def add_profile(name: str, age: int = 30) -> int:
    with _conn() as c:
        cur = c.execute("INSERT INTO profiles(name, age, sort) "
                        "VALUES(?,?, (SELECT COALESCE(MAX(sort),0)+1 FROM profiles))",
                        (name, age))
        return cur.lastrowid


def rename_profile(pid: int, name: str):
    with _conn() as c:
        c.execute("UPDATE profiles SET name=? WHERE id=?", (name, pid))


def set_profile_age(pid: int, age: int):
    with _conn() as c:
        c.execute("UPDATE profiles SET age=? WHERE id=?", (age, pid))


def set_profile_style(pid: int, style: str):
    with _conn() as c:
        c.execute("UPDATE profiles SET style=? WHERE id=?", (style, pid))


def get_profile(pid: int) -> dict | None:
    with _conn() as c:
        row = c.execute("SELECT * FROM profiles WHERE id=?", (pid,)).fetchone()
        return dict(row) if row else None


def delete_profile(pid: int):
    """프로필과 그 보유내역 삭제. 마지막 하나는 삭제 불가."""
    with _conn() as c:
        n = c.execute("SELECT COUNT(*) AS n FROM profiles").fetchone()["n"]
        if n <= 1:
            raise ValueError("마지막 프로필은 삭제할 수 없습니다.")
        c.execute("DELETE FROM holdings WHERE profile_id=?", (pid,))
        c.execute("DELETE FROM profiles WHERE id=?", (pid,))


def current_profile_id() -> int:
    val = get_setting("current_profile_id")
    if val and str(val).isdigit():
        pid = int(val)
        if get_profile(pid):
            return pid
    # 폴백: 첫 프로필
    profs = list_profiles()
    return profs[0]["id"] if profs else add_profile("내 포트폴리오")


def set_current_profile(pid: int):
    set_setting("current_profile_id", pid)


# ----------------------------- 보유내역 -----------------------------
def list_holdings(profile_id: int) -> list[dict]:
    with _conn() as c:
        rows = c.execute(
            "SELECT * FROM holdings WHERE profile_id=? ORDER BY sort, id",
            (profile_id,)).fetchall()
        return [dict(r) for r in rows]


def add_holding(d: dict, profile_id: int) -> int:
    cols = _HOLDING_COLS + ("profile_id",)
    vals = [d.get(k) for k in _HOLDING_COLS] + [profile_id]
    with _conn() as c:
        cur = c.execute(
            f"INSERT INTO holdings ({','.join(cols)}) "
            f"VALUES ({','.join('?' * len(cols))})", vals)
        return cur.lastrowid


def update_holding(hid: int, d: dict):
    sets = ",".join(f"{k}=?" for k in _HOLDING_COLS)
    vals = [d.get(k) for k in _HOLDING_COLS] + [hid]
    with _conn() as c:
        c.execute(f"UPDATE holdings SET {sets} WHERE id=?", vals)


def delete_holding(hid: int):
    with _conn() as c:
        c.execute("DELETE FROM holdings WHERE id=?", (hid,))


def reorder(ordered_ids: list[int]):
    """주어진 id 순서대로 sort 값을 0,1,2… 로 저장(수동 정렬)."""
    with _conn() as c:
        for pos, hid in enumerate(ordered_ids):
            c.execute("UPDATE holdings SET sort=? WHERE id=?", (pos, hid))


# ----------------------------- 관심종목(저장) -----------------------------
def list_watchlist(profile_id: int) -> list[dict]:
    with _conn() as c:
        return [dict(r) for r in c.execute(
            "SELECT * FROM watchlist WHERE profile_id=? ORDER BY sort, id",
            (profile_id,))]


def add_watchlist(profile_id: int, market: str, code: str, name: str) -> int | None:
    with _conn() as c:
        # 중복 방지
        ex = c.execute("SELECT id FROM watchlist WHERE profile_id=? AND code=?",
                       (profile_id, code)).fetchone()
        if ex:
            return ex["id"]
        cur = c.execute(
            "INSERT INTO watchlist(profile_id, market, code, name, sort) "
            "VALUES(?,?,?,?, (SELECT COALESCE(MAX(sort),0)+1 FROM watchlist WHERE profile_id=?))",
            (profile_id, market, code, name, profile_id))
        return cur.lastrowid


def delete_watchlist(profile_id: int, code: str):
    with _conn() as c:
        c.execute("DELETE FROM watchlist WHERE profile_id=? AND code=?",
                  (profile_id, code))


# ----------------------------- 자산 기록(스냅샷) -----------------------------
def add_snapshot(profile_id: int, ymd: str, total: float,
                 invested: float = 0, pnl: float = 0,
                 breakdown: str = "", ret: float = 0):
    """같은 날짜는 덮어쓰기(하루 1건). breakdown=자산군별 금액 JSON, ret=수익률(%)."""
    with _conn() as c:
        c.execute("DELETE FROM asset_history WHERE profile_id=? AND ymd=?",
                  (profile_id, ymd))
        c.execute("INSERT INTO asset_history"
                  "(profile_id, ymd, total, invested, pnl, breakdown, ret) "
                  "VALUES(?,?,?,?,?,?,?)",
                  (profile_id, ymd, total, invested, pnl, breakdown, ret))


def list_snapshots(profile_id: int) -> list[dict]:
    with _conn() as c:
        return [dict(r) for r in c.execute(
            "SELECT * FROM asset_history WHERE profile_id=? ORDER BY ymd",
            (profile_id,))]


def delete_snapshot(sid: int):
    with _conn() as c:
        c.execute("DELETE FROM asset_history WHERE id=?", (sid,))


# ----------------------------- 매도 거래(실현손익) -----------------------------
def add_transaction(d: dict) -> int:
    cols = ("profile_id", "ymd", "market", "code", "name", "qty",
            "buy_price", "sell_price", "currency", "realized", "memo")
    with _conn() as c:
        cur = c.execute(
            f"INSERT INTO transactions ({','.join(cols)}) "
            f"VALUES ({','.join('?' * len(cols))})",
            [d.get(k) for k in cols])
        return cur.lastrowid


def list_transactions(profile_id: int) -> list[dict]:
    with _conn() as c:
        return [dict(r) for r in c.execute(
            "SELECT * FROM transactions WHERE profile_id=? ORDER BY ymd DESC, id DESC",
            (profile_id,))]


def realized_total(profile_id: int) -> float:
    with _conn() as c:
        row = c.execute("SELECT COALESCE(SUM(realized),0) AS s "
                        "FROM transactions WHERE profile_id=?", (profile_id,)).fetchone()
        return row["s"] or 0.0


def delete_transaction(tid: int):
    with _conn() as c:
        c.execute("DELETE FROM transactions WHERE id=?", (tid,))
