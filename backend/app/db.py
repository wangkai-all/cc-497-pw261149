"""SQLite 存储层：追加式（append-only）版本化余额 + 分录 + 对账/修复/审计表。

核心约束：
- balances 表只 INSERT 不 UPDATE/DELETE，原始余额（version=1）永远不可改写；
  当前有效余额 = 同一 (source, period, org, currency, account) 下 version 最大的行。
- 修复建议通过插入新版本行生效，形成完整版本链。
"""
import json
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

DEFAULT_DB_PATH = Path(__file__).resolve().parent.parent / "recon.db"

SOURCES = ("GL", "DETAIL", "SETTLEMENT", "ADJUSTMENT")
SOURCE_LABELS = {
    "GL": "总账",
    "DETAIL": "明细",
    "SETTLEMENT": "结算",
    "ADJUSTMENT": "调整",
}

SCHEMA = """
CREATE TABLE IF NOT EXISTS periods (
  period     TEXT PRIMARY KEY,            -- 例如 '2026-11'
  status     TEXT NOT NULL DEFAULT 'OPEN',-- OPEN / CLOSED
  closed_at  TEXT,
  closed_by  TEXT
);

CREATE TABLE IF NOT EXISTS balances (      -- 追加式版本化余额
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  source      TEXT NOT NULL,               -- GL/DETAIL/SETTLEMENT/ADJUSTMENT
  period      TEXT NOT NULL,
  org         TEXT NOT NULL,
  currency    TEXT NOT NULL,
  account     TEXT NOT NULL,
  amount_minor INTEGER NOT NULL,           -- 金额（最小货币单位，分）
  version     INTEGER NOT NULL,
  proposal_id INTEGER,                     -- 产生该版本的修复建议（原始装载为 NULL）
  note        TEXT,
  created_by  TEXT NOT NULL,
  created_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_bal_key ON balances(source, period, org, currency, account, version);

CREATE TABLE IF NOT EXISTS entries (       -- 分录级数据（证据链，同样只增不改）
  id           INTEGER PRIMARY KEY AUTOINCREMENT,
  source       TEXT NOT NULL,
  period       TEXT NOT NULL,              -- 入账期间
  org          TEXT NOT NULL,
  currency     TEXT NOT NULL,
  account      TEXT NOT NULL,
  amount_minor INTEGER NOT NULL,
  doc_no       TEXT,                       -- 原始凭证号
  entry_date   TEXT,                       -- 业务发生日期
  reversal_of  INTEGER,                    -- 冲销对象分录 id
  batch_id     TEXT,
  created_at   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_entries_key ON entries(org, currency, account, period);
CREATE INDEX IF NOT EXISTS idx_entries_doc ON entries(doc_no);

CREATE TABLE IF NOT EXISTS recon_runs (
  id              INTEGER PRIMARY KEY AUTOINCREMENT,
  period          TEXT NOT NULL,
  status          TEXT NOT NULL,           -- RUNNING / COMPLETED
  total_keys      INTEGER DEFAULT 0,
  total_diffs     INTEGER DEFAULT 0,
  total_abs_minor INTEGER DEFAULT 0,
  started_by      TEXT,
  started_at      TEXT,
  finished_at     TEXT
);

CREATE TABLE IF NOT EXISTS differences (
  id                INTEGER PRIMARY KEY AUTOINCREMENT,
  run_id            INTEGER NOT NULL,
  period            TEXT NOT NULL,
  org               TEXT NOT NULL,
  currency          TEXT NOT NULL,
  account           TEXT NOT NULL,
  amount_gl         INTEGER NOT NULL DEFAULT 0,
  amount_detail     INTEGER NOT NULL DEFAULT 0,
  amount_settlement INTEGER NOT NULL DEFAULT 0,
  amount_adjustment INTEGER NOT NULL DEFAULT 0,
  gl_gap_minor      INTEGER NOT NULL DEFAULT 0,   -- 总账 - (明细+调整)
  stl_gap_minor     INTEGER NOT NULL DEFAULT 0,   -- 结算 - (明细+调整)
  diff_minor        INTEGER NOT NULL,             -- max(|gl_gap|, |stl_gap|)
  status            TEXT NOT NULL DEFAULT 'OPEN', -- OPEN/DIAGNOSED/FIX_PROPOSED/FIX_APPLIED/VERIFIED/WAIVED
  verified_via      INTEGER,                      -- 使其验证通过的修复建议 id
  note              TEXT,
  created_at        TEXT NOT NULL,
  updated_at        TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_diff_key ON differences(period, org, currency, account);

CREATE TABLE IF NOT EXISTS difference_causes (
  id             INTEGER PRIMARY KEY AUTOINCREMENT,
  difference_id  INTEGER NOT NULL,
  cause_type     TEXT NOT NULL,   -- DUPLICATE_ENTRY/CROSS_PERIOD_REVERSAL/MISSING_SETTLEMENT/UNPOSTED_ADJUSTMENT/FX_ROUNDING/TIMING_DIFFERENCE
  confidence     REAL NOT NULL,
  summary        TEXT NOT NULL,
  explains_minor INTEGER NOT NULL DEFAULT 0,  -- 该原因可解释的金额
  payload        TEXT,                        -- JSON：规则命中细节
  status         TEXT NOT NULL DEFAULT 'CANDIDATE', -- CANDIDATE/CONFIRMED/REJECTED
  created_at     TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS cause_evidence (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  cause_id      INTEGER NOT NULL,
  evidence_type TEXT NOT NULL,    -- ENTRY / ENTRY_PAIR / PERIOD_STATE / THRESHOLD / BALANCE
  ref_ids       TEXT,             -- JSON 数组：关联分录/余额 id
  note          TEXT
);

CREATE TABLE IF NOT EXISTS fix_proposals (
  id             INTEGER PRIMARY KEY AUTOINCREMENT,
  difference_id  INTEGER NOT NULL,
  cause_id       INTEGER,
  proposal_type  TEXT NOT NULL,   -- ADJUSTMENT_ENTRY / REVERSAL_ENTRY / CONTROLLED_CORRECTION
  title          TEXT NOT NULL,
  description    TEXT,
  status         TEXT NOT NULL DEFAULT 'DRAFT', -- DRAFT/SUBMITTED/APPROVED/REJECTED/APPLIED/VERIFIED
  created_by     TEXT NOT NULL,
  submitted_by   TEXT, approved_by TEXT, applied_by TEXT,
  created_at     TEXT NOT NULL,
  submitted_at   TEXT, approved_at TEXT, applied_at TEXT
);

CREATE TABLE IF NOT EXISTS fix_actions (
  id           INTEGER PRIMARY KEY AUTOINCREMENT,
  proposal_id  INTEGER NOT NULL,
  action_kind  TEXT NOT NULL,     -- BALANCE_VERSION / ENTRY_REVERSAL
  source       TEXT NOT NULL,
  period       TEXT NOT NULL,
  org          TEXT NOT NULL,
  currency     TEXT NOT NULL,
  account      TEXT NOT NULL,
  delta_minor  INTEGER NOT NULL,
  entry_ref    INTEGER,           -- ENTRY_REVERSAL 冲销的分录 id
  doc_no       TEXT,              -- ENTRY_REVERSAL 沿用的原凭证号
  rationale    TEXT
);

CREATE TABLE IF NOT EXISTS audit_log (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  ts          TEXT NOT NULL,
  actor       TEXT NOT NULL,
  action      TEXT NOT NULL,
  entity_type TEXT NOT NULL,
  entity_id   INTEGER,
  payload     TEXT
);
"""


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def db_path() -> str:
    return os.environ.get("RECON_DB", str(DEFAULT_DB_PATH))


def connect(path: str | None = None) -> sqlite3.Connection:
    conn = sqlite3.connect(path or db_path())
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    conn.commit()


def audit(conn, actor: str, action: str, entity_type: str,
          entity_id: int | None = None, payload: dict | None = None) -> None:
    """所有关键操作写入审计日志（同事务提交）。"""
    conn.execute(
        "INSERT INTO audit_log(ts, actor, action, entity_type, entity_id, payload)"
        " VALUES (?,?,?,?,?,?)",
        (now_iso(), actor, action, entity_type, entity_id,
         json.dumps(payload or {}, ensure_ascii=False)),
    )


def rows(conn, sql: str, params: tuple = ()) -> list[dict]:
    return [dict(r) for r in conn.execute(sql, params).fetchall()]


def row(conn, sql: str, params: tuple = ()) -> dict | None:
    r = conn.execute(sql, params).fetchone()
    return dict(r) if r else None
