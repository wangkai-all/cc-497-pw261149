"""修复建议：生成 → 审批 → 应用（只写新版本，不改原始余额）。

规则：
- 开放账期：ADJUSTMENT_ENTRY / REVERSAL_ENTRY，创建人可直接应用（全程审计）。
- 关账账期：只能走 CONTROLLED_CORRECTION（受控更正），必须 提交→他人审批→应用，
  双人控制（审批人 ≠ 创建人）。
- 应用时插入 balances 新版本行（version+1）与冲销分录，原始行保持不变。
- 应用后自动复核对账：格差归零 → 差异与建议标记 VERIFIED。
"""
import json

from .db import audit, now_iso, row, rows
from .reconcile import compute_gaps, current_amount, current_version

OPEN_PERIOD_TYPES = ("ADJUSTMENT_ENTRY", "REVERSAL_ENTRY")


def _period_status(conn, period: str) -> str:
    p = row(conn, "SELECT status FROM periods WHERE period=?", (period,))
    return p["status"] if p else "OPEN"


def _next_open_period(conn, period: str) -> str | None:
    r = row(conn, "SELECT period FROM periods WHERE status='OPEN' AND period>? ORDER BY period LIMIT 1",
            (period,))
    return r["period"] if r else None


# ---------------------------------------------------------------- 生成建议

def build_actions(conn, d: dict, cause: dict) -> list[dict]:
    """根据候选原因生成修复动作（不落库）。"""
    payload = json.loads(cause["payload"] or "{}")
    t = cause["cause_type"]
    key = {"org": d["org"], "currency": d["currency"], "account": d["account"]}

    if t == "DUPLICATE_ENTRY":
        src, extra = payload["source"], payload["amount_minor"]
        return [
            {"action_kind": "BALANCE_VERSION", "source": src, "period": d["period"],
             **key, "delta_minor": -extra,
             "rationale": f"冲回 {src} 表重复入账 {_fmt(extra)}（凭证 {payload['doc_no']}）"},
            {"action_kind": "ENTRY_REVERSAL", "source": src, "period": d["period"],
             **key, "delta_minor": -extra, "doc_no": payload["doc_no"],
             "rationale": f"生成凭证 {payload['doc_no']} 的红字冲销分录"},
        ]
    if t == "UNPOSTED_ADJUSTMENT":
        adj = d["amount_adjustment"]
        return [{"action_kind": "BALANCE_VERSION", "source": "GL", "period": d["period"],
                 **key, "delta_minor": adj,
                 "rationale": f"将调整表 {_fmt(adj)} 补过账到总账"}]
    if t == "MISSING_SETTLEMENT":
        missing = -d["stl_gap_minor"]
        return [{"action_kind": "BALANCE_VERSION", "source": "SETTLEMENT", "period": d["period"],
                 **key, "delta_minor": missing,
                 "rationale": f"补记缺失结算 {_fmt(missing)}"}]
    if t == "CROSS_PERIOD_REVERSAL":
        moved = payload["moved_minor"]
        other_periods = [p for p in payload["periods"] if p != d["period"]]
        # 以明细账入账期间为准，把总账错期金额平移到正确期间（跨期净额为 0）
        gl_period = other_periods[0] if other_periods else d["period"]
        correct_period = d["period"] if d["period"] != gl_period else other_periods[-1]
        return [
            {"action_kind": "BALANCE_VERSION", "source": "GL", "period": gl_period,
             **key, "delta_minor": -moved if gl_period != correct_period else moved,
             "rationale": f"冲回总账错记到 {gl_period} 的 {_fmt(moved)}"},
            {"action_kind": "BALANCE_VERSION", "source": "GL", "period": correct_period,
             **key, "delta_minor": moved if gl_period != correct_period else -moved,
             "rationale": f"将冲销金额 {_fmt(moved)} 平移到正确账期 {correct_period}"},
        ]
    if t == "FX_ROUNDING":
        return [{"action_kind": "BALANCE_VERSION", "source": "GL", "period": d["period"],
                 **key, "delta_minor": -d["gl_gap_minor"],
                 "rationale": f"汇率尾差调整 {_fmt(-d['gl_gap_minor'])}"}]
    raise ValueError(f"原因类型 {t} 暂不支持自动生成修复动作")


def _fmt(minor: int) -> str:
    return f"{minor / 100:,.2f}"


def create_proposal(conn, difference_id: int, cause_id: int, actor: str) -> dict:
    d = row(conn, "SELECT * FROM differences WHERE id=?", (difference_id,))
    if not d:
        raise ValueError("差异不存在")
    cause = row(conn, "SELECT * FROM difference_causes WHERE id=? AND difference_id=?",
                (cause_id, difference_id))
    if not cause:
        raise ValueError("候选原因不存在，请先诊断")

    actions = build_actions(conn, d, cause)
    # 任一动作落在关账期间 → 必须受控更正
    touches_closed = any(_period_status(conn, a["period"]) == "CLOSED" for a in actions)
    if touches_closed:
        ptype = "CONTROLLED_CORRECTION"
    elif cause["cause_type"] == "DUPLICATE_ENTRY":
        ptype = "REVERSAL_ENTRY"
    else:
        ptype = "ADJUSTMENT_ENTRY"

    title = f"[{cause['cause_type']}] {d['period']} {d['org']} {d['currency']} {d['account']}"
    cur = conn.execute("""
        INSERT INTO fix_proposals(difference_id, cause_id, proposal_type, title,
                                  description, status, created_by, created_at)
        VALUES (?,?,?,?,?, 'DRAFT', ?, ?)
    """, (difference_id, cause_id, ptype, title, cause["summary"], actor, now_iso()))
    pid = cur.lastrowid
    for a in actions:
        conn.execute("""
            INSERT INTO fix_actions(proposal_id, action_kind, source, period, org,
                                    currency, account, delta_minor, doc_no, rationale)
            VALUES (?,?,?,?,?,?,?,?,?,?)
        """, (pid, a["action_kind"], a["source"], a["period"], a["org"],
              a["currency"], a["account"], a["delta_minor"], a.get("doc_no"),
              a["rationale"]))

    if d["status"] in ("OPEN", "DIAGNOSED"):
        conn.execute("UPDATE differences SET status='FIX_PROPOSED', updated_at=? WHERE id=?",
                     (now_iso(), difference_id))
    audit(conn, actor, "PROPOSAL_CREATE", "fix_proposal", pid,
          {"type": ptype, "difference_id": difference_id, "actions": len(actions),
           "touches_closed_period": touches_closed})
    conn.commit()
    return get_proposal(conn, pid)


def get_proposal(conn, pid: int) -> dict:
    p = row(conn, "SELECT * FROM fix_proposals WHERE id=?", (pid,))
    if p:
        p["actions"] = rows(conn, "SELECT * FROM fix_actions WHERE proposal_id=?", (pid,))
    return p


# ---------------------------------------------------------------- 状态流转

def submit_proposal(conn, pid: int, actor: str) -> dict:
    p = get_proposal(conn, pid)
    if not p or p["status"] != "DRAFT":
        raise ValueError("仅草稿状态可提交")
    conn.execute("UPDATE fix_proposals SET status='SUBMITTED', submitted_by=?, submitted_at=? WHERE id=?",
                 (actor, now_iso(), pid))
    audit(conn, actor, "PROPOSAL_SUBMIT", "fix_proposal", pid, {})
    conn.commit()
    return get_proposal(conn, pid)


def approve_proposal(conn, pid: int, actor: str) -> dict:
    p = get_proposal(conn, pid)
    if not p or p["status"] != "SUBMITTED":
        raise ValueError("仅已提交状态可审批")
    if actor == p["created_by"]:
        raise PermissionError("受控更正要求双人控制：审批人不能是创建人")
    conn.execute("UPDATE fix_proposals SET status='APPROVED', approved_by=?, approved_at=? WHERE id=?",
                 (actor, now_iso(), pid))
    audit(conn, actor, "PROPOSAL_APPROVE", "fix_proposal", pid, {})
    conn.commit()
    return get_proposal(conn, pid)


def reject_proposal(conn, pid: int, actor: str, reason: str) -> dict:
    p = get_proposal(conn, pid)
    if not p or p["status"] in ("APPLIED", "VERIFIED"):
        raise ValueError("已应用的建议不能驳回")
    conn.execute("UPDATE fix_proposals SET status='REJECTED' WHERE id=?", (pid,))
    audit(conn, actor, "PROPOSAL_REJECT", "fix_proposal", pid, {"reason": reason})
    conn.commit()
    return get_proposal(conn, pid)


# ---------------------------------------------------------------- 应用前验证

def validate_proposal(conn, p: dict) -> list[str]:
    """应用前校验，返回错误列表（空 = 可应用）。含跨期冲销与重复入账专项验证。"""
    errors: list[str] = []
    actions = p["actions"]

    if p["proposal_type"] == "CONTROLLED_CORRECTION":
        if p["status"] != "APPROVED":
            errors.append("受控更正必须经提交并审批通过后才能应用")
    elif p["status"] not in ("DRAFT", "APPROVED"):
        errors.append(f"当前状态 {p['status']} 不可应用")

    for a in actions:
        status = _period_status(conn, a["period"])
        if status == "CLOSED" and p["proposal_type"] != "CONTROLLED_CORRECTION":
            errors.append(f"账期 {a['period']} 已关账，只能走受控更正")

    # 跨期冲销验证：同一 (source,org,currency,account) 跨期平移净额必须为 0
    nets: dict[tuple, int] = {}
    for a in actions:
        if a["action_kind"] != "BALANCE_VERSION":
            continue
        k = (a["source"], a["org"], a["currency"], a["account"])
        nets[k] = nets.get(k, 0) + a["delta_minor"]
    periods_involved = {a["period"] for a in actions}
    if len(periods_involved) > 1:
        for k, net in nets.items():
            if net != 0:
                errors.append(f"跨期冲销验证失败：{k[0]} 在多个账期间净额 {_fmt(net)} ≠ 0")

    # 重复入账验证：重复组仍然存在，且冲回金额恰好是一份
    cause = row(conn, "SELECT * FROM difference_causes WHERE id=?", (p["cause_id"],)) if p["cause_id"] else None
    if cause and cause["cause_type"] == "DUPLICATE_ENTRY":
        payload = json.loads(cause["payload"] or "{}")
        dup_entries = rows(conn, """
            SELECT * FROM entries WHERE source=? AND period=? AND doc_no=? AND amount_minor=?
        """, (payload["source"], p_period(conn, p), payload["doc_no"], payload["amount_minor"]))
        reversals = rows(conn, """
            SELECT * FROM entries WHERE source=? AND period=? AND doc_no=? AND amount_minor=?
        """, (payload["source"], p_period(conn, p), payload["doc_no"], -payload["amount_minor"]))
        remaining = len(dup_entries) - len(reversals)
        if remaining < 2:
            errors.append("重复入账验证失败：重复分录已不存在（可能已被冲回）")
        delta = next((a["delta_minor"] for a in actions if a["action_kind"] == "BALANCE_VERSION"), 0)
        if abs(delta) != abs(payload["amount_minor"]):
            errors.append("重复入账验证失败：冲回金额必须恰好等于一份重复分录")

    # 调整未过账验证：当前总账缺口仍等于调整额
    if cause and cause["cause_type"] == "UNPOSTED_ADJUSTMENT":
        d = row(conn, "SELECT * FROM differences WHERE id=?", (p["difference_id"],))
        amounts = {s: current_amount(conn, s, d["period"], d["org"], d["currency"], d["account"])
                   for s in ("GL", "DETAIL", "SETTLEMENT", "ADJUSTMENT")}
        gl_gap, _, _ = compute_gaps(amounts)
        if gl_gap != -amounts["ADJUSTMENT"]:
            errors.append("调整未过账验证失败：当前总账缺口已不等于调整额（账面已变化）")

    return errors


def p_period(conn, p: dict) -> str:
    d = row(conn, "SELECT period FROM differences WHERE id=?", (p["difference_id"],))
    return d["period"] if d else ""


# ---------------------------------------------------------------- 应用与复核

def apply_proposal(conn, pid: int, actor: str) -> dict:
    p = get_proposal(conn, pid)
    if not p:
        raise ValueError("修复建议不存在")
    errors = validate_proposal(conn, p)
    if errors:
        raise ValueError("；".join(errors))

    affected: set[tuple] = set()
    for a in p["actions"]:
        if a["action_kind"] == "BALANCE_VERSION":
            new_version = current_version(conn, a["source"], a["period"], a["org"],
                                          a["currency"], a["account"]) + 1
            current = current_amount(conn, a["source"], a["period"], a["org"],
                                     a["currency"], a["account"])
            conn.execute("""
                INSERT INTO balances(source, period, org, currency, account,
                                     amount_minor, version, proposal_id, note, created_by, created_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?)
            """, (a["source"], a["period"], a["org"], a["currency"], a["account"],
                  current + a["delta_minor"], new_version, pid, a["rationale"], actor, now_iso()))
            audit(conn, actor, "BALANCE_VERSION", "balance", None, {
                "source": a["source"], "period": a["period"], "org": a["org"],
                "currency": a["currency"], "account": a["account"],
                "delta_minor": a["delta_minor"], "new_version": new_version,
                "proposal_id": pid,
            })
            affected.add((a["period"], a["org"], a["currency"], a["account"]))
        elif a["action_kind"] == "ENTRY_REVERSAL":
            conn.execute("""
                INSERT INTO entries(source, period, org, currency, account, amount_minor,
                                    doc_no, entry_date, reversal_of, batch_id, created_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?)
            """, (a["source"], a["period"], a["org"], a["currency"], a["account"],
                  a["delta_minor"], a.get("doc_no") or f"REV-{pid}",
                  now_iso()[:10], a.get("entry_ref"), f"FIX-{pid}", now_iso()))
            affected.add((a["period"], a["org"], a["currency"], a["account"]))

    conn.execute("UPDATE fix_proposals SET status='APPLIED', applied_by=?, applied_at=? WHERE id=?",
                 (actor, now_iso(), pid))
    audit(conn, actor, "PROPOSAL_APPLY", "fix_proposal", pid,
          {"affected_keys": [list(k) for k in sorted(affected)]})

    # 复核：受影响键的未决差异全部重算
    verified = []
    for (period, org, currency, account) in affected:
        verified += recheck_key(conn, period, org, currency, account, pid)

    own = row(conn, "SELECT status FROM differences WHERE id=?", (p["difference_id"],))
    if own and own["status"] == "VERIFIED":
        conn.execute("UPDATE fix_proposals SET status='VERIFIED' WHERE id=?", (pid,))
        audit(conn, actor, "PROPOSAL_VERIFY", "fix_proposal", pid, {})
    conn.commit()
    return get_proposal(conn, pid)


def recheck_key(conn, period: str, org: str, currency: str, account: str,
                proposal_id: int) -> list[int]:
    """重算某键当前格差，归零则把相关未决差异标记为 VERIFIED。"""
    amounts = {s: current_amount(conn, s, period, org, currency, account)
               for s in ("GL", "DETAIL", "SETTLEMENT", "ADJUSTMENT")}
    gl_gap, stl_gap, diff = compute_gaps(amounts)
    verified = []
    for d in rows(conn, """
        SELECT * FROM differences
        WHERE period=? AND org=? AND currency=? AND account=?
          AND status NOT IN ('VERIFIED','WAIVED')
    """, (period, org, currency, account)):
        if diff == 0:
            conn.execute("""
                UPDATE differences SET status='VERIFIED', verified_via=?,
                    gl_gap_minor=0, stl_gap_minor=0, diff_minor=0, updated_at=? WHERE id=?
            """, (proposal_id, now_iso(), d["id"]))
            audit(conn, "system", "DIFF_VERIFIED", "difference", d["id"],
                  {"via_proposal": proposal_id})
            verified.append(d["id"])
        else:
            conn.execute("""
                UPDATE differences SET status='FIX_APPLIED', gl_gap_minor=?, stl_gap_minor=?,
                    diff_minor=?, updated_at=? WHERE id=?
            """, (gl_gap, stl_gap, diff, now_iso(), d["id"]))
    return verified
