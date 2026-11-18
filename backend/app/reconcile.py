"""对账引擎：计算四表当前有效余额，按平衡式 总账 == 明细+调整 == 结算 找差异。

余额语义：各源当期发生额（本期净额）。平衡式：
    expected = DETAIL + ADJUSTMENT   （调整表金额应已反映在总账与结算中）
    gl_gap   = GL - expected
    stl_gap  = SETTLEMENT - expected
任一格差非零即构成差异。
"""
from .db import SOURCES, audit, now_iso, row, rows

TOLERANCE_MINOR = 0  # 任何非零格差都记为差异（尾差由诊断规则归类）


def current_balances(conn, period: str) -> dict[tuple, dict[str, int]]:
    """返回 {(org, currency, account): {source: amount_minor}}，取每个键的最新版本。"""
    out: dict[tuple, dict[str, int]] = {}
    for r in rows(conn, """
        SELECT b.source, b.org, b.currency, b.account, b.amount_minor
        FROM balances b
        JOIN (SELECT source, org, currency, account, MAX(version) AS mv
              FROM balances WHERE period = ?
              GROUP BY source, org, currency, account) t
          ON b.source=t.source AND b.org=t.org AND b.currency=t.currency
         AND b.account=t.account AND b.version=t.mv
        WHERE b.period = ?
    """, (period, period)):
        key = (r["org"], r["currency"], r["account"])
        out.setdefault(key, {s: 0 for s in SOURCES})[r["source"]] = r["amount_minor"]
    return out


def current_amount(conn, source: str, period: str, org: str,
                   currency: str, account: str) -> int:
    r = row(conn, """
        SELECT amount_minor FROM balances
        WHERE source=? AND period=? AND org=? AND currency=? AND account=?
        ORDER BY version DESC LIMIT 1
    """, (source, period, org, currency, account))
    return r["amount_minor"] if r else 0


def current_version(conn, source: str, period: str, org: str,
                    currency: str, account: str) -> int:
    r = row(conn, """
        SELECT MAX(version) AS v FROM balances
        WHERE source=? AND period=? AND org=? AND currency=? AND account=?
    """, (source, period, org, currency, account))
    return (r["v"] or 0) if r else 0


def compute_gaps(amounts: dict[str, int]) -> tuple[int, int, int]:
    expected = amounts["DETAIL"] + amounts["ADJUSTMENT"]
    gl_gap = amounts["GL"] - expected
    stl_gap = amounts["SETTLEMENT"] - expected
    diff = max(abs(gl_gap), abs(stl_gap))
    return gl_gap, stl_gap, diff


def run_reconciliation(conn, period: str, actor: str) -> dict:
    """对指定账期执行全量对账，落库 differences，返回 run 摘要。"""
    p = row(conn, "SELECT * FROM periods WHERE period=?", (period,))
    if not p:
        raise ValueError(f"账期 {period} 不存在")

    audit(conn, actor, "RUN_START", "recon_run", None, {"period": period})
    cur = conn.execute(
        "INSERT INTO recon_runs(period, status, started_by, started_at)"
        " VALUES (?,?,?,?)", (period, "RUNNING", actor, now_iso()))
    run_id = cur.lastrowid

    balances = current_balances(conn, period)
    total_diffs = 0
    total_abs = 0
    for (org, currency, account), amounts in sorted(balances.items()):
        gl_gap, stl_gap, diff = compute_gaps(amounts)
        if diff <= TOLERANCE_MINOR:
            continue
        total_diffs += 1
        total_abs += diff
        conn.execute("""
            INSERT INTO differences(run_id, period, org, currency, account,
                amount_gl, amount_detail, amount_settlement, amount_adjustment,
                gl_gap_minor, stl_gap_minor, diff_minor, status, created_at, updated_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?, 'OPEN', ?, ?)
        """, (run_id, period, org, currency, account,
              amounts["GL"], amounts["DETAIL"], amounts["SETTLEMENT"], amounts["ADJUSTMENT"],
              gl_gap, stl_gap, diff, now_iso(), now_iso()))

    conn.execute("""
        UPDATE recon_runs SET status='COMPLETED', total_keys=?, total_diffs=?,
               total_abs_minor=?, finished_at=? WHERE id=?
    """, (len(balances), total_diffs, total_abs, now_iso(), run_id))
    audit(conn, actor, "RUN_DONE", "recon_run", run_id,
          {"period": period, "total_keys": len(balances), "total_diffs": total_diffs})
    conn.commit()
    return row(conn, "SELECT * FROM recon_runs WHERE id=?", (run_id,))


def latest_run(conn, period: str) -> dict | None:
    return row(conn, """
        SELECT * FROM recon_runs WHERE period=? AND status='COMPLETED'
        ORDER BY id DESC LIMIT 1
    """, (period,))


def difference_for_key(conn, period: str, org: str, currency: str,
                       account: str) -> dict | None:
    """该键最近一次未关闭（未豁免）的差异记录。"""
    return row(conn, """
        SELECT * FROM differences
        WHERE period=? AND org=? AND currency=? AND account=? AND status != 'WAIVED'
        ORDER BY id DESC LIMIT 1
    """, (period, org, currency, account))


def build_tree(conn, period: str) -> dict:
    """差异树：账期 → 组织 → 币种 → 科目，携带四表金额与差异状态。"""
    run = latest_run(conn, period)
    balances = current_balances(conn, period)
    diffs = {}
    if run:
        for d in rows(conn, "SELECT * FROM differences WHERE run_id=?", (run["id"],)):
            diffs[(d["org"], d["currency"], d["account"])] = d

    orgs: dict[str, dict] = {}
    for (org, currency, account), amounts in sorted(balances.items()):
        gl_gap, stl_gap, diff = compute_gaps(amounts)
        d = diffs.get((org, currency, account))
        if d:
            status = d["status"]
        elif diff > 0:
            status = "OPEN"  # 账面在对账后发生变化，尚未复跑
        else:
            status = "OK"
        leaf = {
            "account": account,
            "amounts": {s: amounts[s] for s in SOURCES},
            "expected": amounts["DETAIL"] + amounts["ADJUSTMENT"],
            "gl_gap": gl_gap,
            "stl_gap": stl_gap,
            "diff_minor": diff,
            "status": status,
            "difference_id": d["id"] if d else None,
        }
        cur_node = orgs.setdefault(org, {"org": org, "currencies": {}})
        cur_node["currencies"].setdefault(currency, {"currency": currency, "accounts": []})["accounts"].append(leaf)

    def agg(nodes):
        return {
            "diff_count": sum(1 for n in nodes if n["diff_minor"] > 0 and n["status"] not in ("VERIFIED", "WAIVED")),
            "verified_count": sum(1 for n in nodes if n["status"] == "VERIFIED"),
            "total_abs_diff": sum(n["diff_minor"] for n in nodes if n["status"] not in ("VERIFIED", "WAIVED")),
            "account_count": len(nodes),
        }

    org_list = []
    for org, onode in orgs.items():
        cur_list = []
        for currency, cnode in onode["currencies"].items():
            cur_list.append({"currency": currency, "accounts": cnode["accounts"], **agg(cnode["accounts"])})
        all_accounts = [a for c in cur_list for a in c["accounts"]]
        org_list.append({"org": org, "currencies": cur_list, **agg(all_accounts)})
    all_accounts = [a for o in org_list for c in o["currencies"] for a in c["accounts"]]
    return {
        "period": period,
        "run": run,
        "period_status": row(conn, "SELECT status FROM periods WHERE period=?", (period,))["status"],
        "orgs": org_list,
        **agg(all_accounts),
    }
