"""种子数据：三个账期（2026-09/10 已关账，2026-11 开放），植入典型差异场景。

场景清单：
  1. 2026-10 CN01/CNY/1001  重复入账（关账期间 → 只能受控更正）
  2. 2026-10+11 CN02/CNY/2202 跨期冲销（总账入 10 月、明细入 11 月，同一凭证 REV-1001）
  3. 2026-11 CN01/CNY/6001  重复入账（开放期间）
  4. 2026-11 CN01/CNY/2202  调整未过账
  5. 2026-11 US01/USD/1122  结算缺失
  6. 2026-11 US01/USD/6001  汇率尾差（0.03）
其余键全部平衡，用于对照。
"""
from .db import audit, init_db, now_iso


def Y(v: float) -> int:
    """元 → 分。"""
    return int(round(v * 100))


def _bal(conn, source, period, org, currency, account, amount, note="期初装载"):
    conn.execute("""
        INSERT INTO balances(source, period, org, currency, account,
                             amount_minor, version, proposal_id, note, created_by, created_at)
        VALUES (?,?,?,?,?,?, 1, NULL, ?, 'seed', ?)
    """, (source, period, org, currency, account, amount, note, now_iso()))


def _entry(conn, source, period, org, currency, account, amount,
           doc_no=None, entry_date=None, reversal_of=None, batch_id=None) -> int:
    cur = conn.execute("""
        INSERT INTO entries(source, period, org, currency, account, amount_minor,
                            doc_no, entry_date, reversal_of, batch_id, created_at)
        VALUES (?,?,?,?,?,?,?,?,?,?,?)
    """, (source, period, org, currency, account, amount,
          doc_no, entry_date, reversal_of, batch_id, now_iso()))
    return cur.lastrowid


def _four(conn, period, org, currency, account, gl, detail, settlement, adjustment=0):
    _bal(conn, "GL", period, org, currency, account, gl)
    _bal(conn, "DETAIL", period, org, currency, account, detail)
    _bal(conn, "SETTLEMENT", period, org, currency, account, settlement)
    _bal(conn, "ADJUSTMENT", period, org, currency, account, adjustment)


def seed(conn) -> None:
    init_db(conn)
    for table in ("audit_log", "fix_actions", "fix_proposals", "cause_evidence",
                  "difference_causes", "differences", "recon_runs",
                  "entries", "balances", "periods"):
        conn.execute(f"DELETE FROM {table}")

    # ---- 账期 ------------------------------------------------------------
    conn.execute("INSERT INTO periods VALUES ('2026-09','CLOSED','2026-10-05T10:00:00+00:00','manager.liu')")
    conn.execute("INSERT INTO periods VALUES ('2026-10','CLOSED','2026-11-05T10:00:00+00:00','manager.liu')")
    conn.execute("INSERT INTO periods VALUES ('2026-11','OPEN',NULL,NULL)")

    # ---- 2026-09（全部平衡） ----------------------------------------------
    _four(conn, "2026-09", "CN01", "CNY", "1001", Y(1_000_000), Y(1_000_000), Y(1_000_000))
    _four(conn, "2026-09", "CN01", "CNY", "6001", Y(900_000), Y(900_000), Y(900_000))
    # 跨期冲销的源头：9 月计提应计费用 -80 万（总账与明细一致）
    _four(conn, "2026-09", "CN02", "CNY", "2202", Y(-800_000), Y(-800_000), Y(-800_000))
    acc_gl = _entry(conn, "GL", "2026-09", "CN02", "CNY", "2202", Y(-800_000),
                    doc_no="ACC-0901", entry_date="2026-09-30")
    acc_dt = _entry(conn, "DETAIL", "2026-09", "CN02", "CNY", "2202", Y(-800_000),
                    doc_no="ACC-0901", entry_date="2026-09-30")
    _four(conn, "2026-09", "US01", "USD", "1122", Y(30_000), Y(30_000), Y(30_000))
    _four(conn, "2026-09", "US01", "USD", "6001", Y(50_000), Y(50_000), Y(50_000))

    # ---- 2026-10 -----------------------------------------------------------
    # 场景 1：CN01/CNY/1001 重复入账（关账期间）——明细多记一笔 2 万押金
    _four(conn, "2026-10", "CN01", "CNY", "1001", Y(520_000), Y(540_000), Y(520_000))
    _entry(conn, "GL", "2026-10", "CN01", "CNY", "1001", Y(500_000), doc_no="DEP-1000", entry_date="2026-10-08")
    _entry(conn, "GL", "2026-10", "CN01", "CNY", "1001", Y(20_000), doc_no="DEP-1001", entry_date="2026-10-15")
    _entry(conn, "DETAIL", "2026-10", "CN01", "CNY", "1001", Y(500_000), doc_no="DEP-1000", entry_date="2026-10-08")
    _entry(conn, "DETAIL", "2026-10", "CN01", "CNY", "1001", Y(20_000), doc_no="DEP-1001", entry_date="2026-10-15", batch_id="B-1015")
    _entry(conn, "DETAIL", "2026-10", "CN01", "CNY", "1001", Y(20_000), doc_no="DEP-1001", entry_date="2026-10-15", batch_id="B-1015")
    _entry(conn, "SETTLEMENT", "2026-10", "CN01", "CNY", "1001", Y(520_000), doc_no="STL-1000", entry_date="2026-10-31")

    # 场景 2a：跨期冲销（10 月侧）——总账把 9 月计提的冲销入在 10 月，明细未入
    _four(conn, "2026-10", "CN02", "CNY", "2202", Y(800_000), 0, 0)
    _entry(conn, "GL", "2026-10", "CN02", "CNY", "2202", Y(800_000),
           doc_no="REV-1001", entry_date="2026-10-31", reversal_of=acc_gl)

    # 平衡对照
    _four(conn, "2026-10", "CN01", "CNY", "6001", Y(1_100_000), Y(1_100_000), Y(1_100_000))
    _four(conn, "2026-10", "US01", "USD", "1122", Y(32_000), Y(32_000), Y(32_000))
    _four(conn, "2026-10", "US01", "USD", "6001", Y(55_000), Y(55_000), Y(55_000))

    # ---- 2026-11 -----------------------------------------------------------
    # 场景 2b：跨期冲销（11 月侧）——明细与结算把同一笔冲销入在 11 月，总账未入
    _four(conn, "2026-11", "CN02", "CNY", "2202", 0, Y(800_000), Y(800_000))
    _entry(conn, "DETAIL", "2026-11", "CN02", "CNY", "2202", Y(800_000),
           doc_no="REV-1001", entry_date="2026-10-31", reversal_of=acc_dt)
    _entry(conn, "SETTLEMENT", "2026-11", "CN02", "CNY", "2202", Y(800_000),
           doc_no="STL-REV1", entry_date="2026-11-02")

    # 场景 3：CN01/CNY/6001 重复入账（开放期间）——发票 INV-1102 在明细入账两次
    _four(conn, "2026-11", "CN01", "CNY", "6001", Y(1_200_000), Y(1_700_000), Y(1_200_000))
    _entry(conn, "GL", "2026-11", "CN01", "CNY", "6001", Y(700_000), doc_no="INV-1101", entry_date="2026-11-06")
    _entry(conn, "GL", "2026-11", "CN01", "CNY", "6001", Y(500_000), doc_no="INV-1102", entry_date="2026-11-12")
    _entry(conn, "DETAIL", "2026-11", "CN01", "CNY", "6001", Y(700_000), doc_no="INV-1101", entry_date="2026-11-06")
    _entry(conn, "DETAIL", "2026-11", "CN01", "CNY", "6001", Y(500_000), doc_no="INV-1102", entry_date="2026-11-12", batch_id="B-1112")
    _entry(conn, "DETAIL", "2026-11", "CN01", "CNY", "6001", Y(500_000), doc_no="INV-1102", entry_date="2026-11-12", batch_id="B-1112")
    _entry(conn, "SETTLEMENT", "2026-11", "CN01", "CNY", "6001", Y(700_000), doc_no="STL-1101", entry_date="2026-11-08")
    _entry(conn, "SETTLEMENT", "2026-11", "CN01", "CNY", "6001", Y(500_000), doc_no="STL-1102", entry_date="2026-11-13")

    # 场景 4：CN01/CNY/2202 调整未过账——7,500 供应商价格调整未进总账
    _four(conn, "2026-11", "CN01", "CNY", "2202", Y(300_000), Y(300_000), Y(307_500), Y(7_500))
    _entry(conn, "DETAIL", "2026-11", "CN01", "CNY", "2202", Y(300_000), doc_no="PO-2201", entry_date="2026-11-05")
    _entry(conn, "GL", "2026-11", "CN01", "CNY", "2202", Y(300_000), doc_no="VCH-2201", entry_date="2026-11-05")
    _entry(conn, "SETTLEMENT", "2026-11", "CN01", "CNY", "2202", Y(307_500), doc_no="STL-2201", entry_date="2026-11-14")
    _entry(conn, "ADJUSTMENT", "2026-11", "CN01", "CNY", "2202", Y(7_500), doc_no="ADJ-1101", entry_date="2026-11-12")

    # 场景 5：US01/USD/1122 结算缺失——发票 INV-U202 尚未结算
    _four(conn, "2026-11", "US01", "USD", "1122", Y(45_000), Y(45_000), Y(20_000))
    _entry(conn, "DETAIL", "2026-11", "US01", "USD", "1122", Y(20_000), doc_no="INV-U201", entry_date="2026-11-03")
    _entry(conn, "DETAIL", "2026-11", "US01", "USD", "1122", Y(25_000), doc_no="INV-U202", entry_date="2026-11-10")
    _entry(conn, "GL", "2026-11", "US01", "USD", "1122", Y(45_000), doc_no="VCH-U201", entry_date="2026-11-10")
    _entry(conn, "SETTLEMENT", "2026-11", "US01", "USD", "1122", Y(20_000), doc_no="STL-U201", entry_date="2026-11-09")

    # 场景 6：US01/USD/6001 汇率尾差 0.03
    _four(conn, "2026-11", "US01", "USD", "6001", Y(88_000), Y(88_000) + 3, Y(88_000))
    _entry(conn, "GL", "2026-11", "US01", "USD", "6001", Y(88_000), doc_no="INV-U301", entry_date="2026-11-15")
    _entry(conn, "DETAIL", "2026-11", "US01", "USD", "6001", Y(88_000) + 3, doc_no="INV-U301", entry_date="2026-11-15")

    # 平衡对照
    _four(conn, "2026-11", "CN01", "CNY", "1001", Y(600_000), Y(600_000), Y(600_000))
    _four(conn, "2026-11", "CN02", "CNY", "6001", Y(450_000), Y(450_000), Y(450_000))
    _four(conn, "2026-11", "CN02", "CNY", "1122", Y(66_000), Y(66_000), Y(66_000))
    _four(conn, "2026-11", "US01", "USD", "2202", Y(15_000), Y(15_000), Y(15_000))

    audit(conn, "system", "SEED", "database", None, {"note": "种子数据装载完成"})
    conn.commit()
