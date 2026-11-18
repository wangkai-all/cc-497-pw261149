"""差异定位：候选原因自动诊断规则引擎。

每条候选原因都附带证据（分录、分录对、账期状态、阈值说明），
并给出可解释金额与置信度。规则：
  DUPLICATE_ENTRY        重复入账：同源同凭证号同金额出现多次
  CROSS_PERIOD_REVERSAL  跨期冲销：同一冲销凭证在不同账期入账 / 冲销链跨期
  MISSING_SETTLEMENT     结算缺失：明细分录在结算表中无对应记录
  UNPOSTED_ADJUSTMENT    调整未过账：调整表有金额但总账未反映
  TIMING_DIFFERENCE      时间性差异：业务日期与入账期间不一致
  FX_ROUNDING            汇率尾差：外币小额差异
"""
import json
from collections import defaultdict

from .db import audit, now_iso, row, rows

ROUNDING_THRESHOLD_MINOR = 50  # |差异| ≤ 0.50 个货币单位视为尾差候选
BASE_CURRENCY = "CNY"

CAUSE_LABELS = {
    "DUPLICATE_ENTRY": "重复入账",
    "CROSS_PERIOD_REVERSAL": "跨期冲销",
    "MISSING_SETTLEMENT": "结算缺失",
    "UNPOSTED_ADJUSTMENT": "调整未过账",
    "TIMING_DIFFERENCE": "时间性差异",
    "FX_ROUNDING": "汇率尾差",
}


def _entries_for_key(conn, d) -> list[dict]:
    """该差异键全部账期的分录（跨期分析需要相邻期间数据）。"""
    return rows(conn, """
        SELECT * FROM entries WHERE org=? AND currency=? AND account=?
        ORDER BY period, id
    """, (d["org"], d["currency"], d["account"]))


def _add_cause(conn, difference_id, cause_type, confidence, summary,
               explains_minor, payload, evidence) -> int:
    cur = conn.execute("""
        INSERT INTO difference_causes(difference_id, cause_type, confidence, summary,
                                      explains_minor, payload, status, created_at)
        VALUES (?,?,?,?,?,?, 'CANDIDATE', ?)
    """, (difference_id, cause_type, confidence, summary, explains_minor,
          json.dumps(payload, ensure_ascii=False), now_iso()))
    cause_id = cur.lastrowid
    for ev in evidence:
        conn.execute("""
            INSERT INTO cause_evidence(cause_id, evidence_type, ref_ids, note)
            VALUES (?,?,?,?)
        """, (cause_id, ev["type"], json.dumps(ev.get("ref_ids", [])), ev.get("note", "")))
    return cause_id


def _fmt(minor: int) -> str:
    return f"{minor / 100:,.2f}"


def detect_duplicates(conn, d, period_entries, gl_gap, stl_gap) -> list[dict]:
    """重复入账：同源同凭证号下，某金额的有效条数（正数 − 红冲）≥ 2，
    且多出的金额能解释格差。"""
    found = []
    by_source = defaultdict(list)
    for e in period_entries:
        if e["doc_no"]:
            by_source[e["source"]].append(e)
    for src, es in by_source.items():
        by_doc = defaultdict(list)
        for e in es:
            by_doc[e["doc_no"]].append(e)
        for doc, g in by_doc.items():
            counts = defaultdict(int)
            for e in g:
                counts[e["amount_minor"]] += 1
            for amt, cnt in counts.items():
                if amt <= 0:
                    continue
                effective = cnt - counts.get(-amt, 0)  # 扣除已红冲的份数
                if effective < 2:
                    continue
                extra = (effective - 1) * amt
                if src == "GL":
                    match = gl_gap == extra
                elif src == "SETTLEMENT":
                    match = stl_gap == extra
                else:  # DETAIL / ADJUSTMENT 虚增会压低 gl_gap / stl_gap
                    match = gl_gap == -extra or stl_gap == -extra
                dup_ids = [e["id"] for e in g if e["amount_minor"] == amt]
                found.append({
                    "type": "DUPLICATE_ENTRY",
                    "confidence": 0.95 if match else 0.5,
                    "summary": (f"{src} 表凭证 {doc} 有效重复 {effective} 条"
                                f"（每条 {_fmt(amt)}），多计 {_fmt(extra)}"),
                    "explains": extra,
                    "payload": {"source": src, "doc_no": doc, "amount_minor": amt,
                                "count": effective, "exact_match": match},
                    "evidence": [{
                        "type": "ENTRY",
                        "ref_ids": dup_ids,
                        "note": f"凭证号 {doc}、金额 {_fmt(amt)} 的重复分录 {effective} 条",
                    }],
                })
    return found


def detect_cross_period(conn, d, all_entries, gl_gap, stl_gap) -> list[dict]:
    """跨期冲销：同一凭证号出现在多个账期，或冲销链（reversal_of）跨期。"""
    found = []
    by_doc = defaultdict(list)
    for e in all_entries:
        if e["doc_no"]:
            by_doc[e["doc_no"]].append(e)

    for doc, es in by_doc.items():
        periods = {e["period"] for e in es}
        if len(periods) < 2:
            continue
        # 该凭证在差异期间缺失的源，即入账期间错位的源
        moved = sum(e["amount_minor"] for e in es if e["period"] == d["period"])
        other = [e for e in es if e["period"] != d["period"]]
        moved_amt = abs(other[0]["amount_minor"]) if other else abs(moved)
        if not moved_amt:
            continue
        match = abs(gl_gap) == moved_amt or abs(stl_gap) == moved_amt
        if not match:
            continue
        # 冲销链证据：任一相关分录的 reversal_of 指向其他期间
        chain_ids, chain_cross = [], False
        for e in es:
            if e["reversal_of"]:
                orig = row(conn, "SELECT * FROM entries WHERE id=?", (e["reversal_of"],))
                if orig:
                    chain_ids += [e["id"], orig["id"]]
                    if orig["period"] != e["period"]:
                        chain_cross = True
        other_periods = sorted(p for p in periods if p != d["period"])
        period_states = rows(conn,
                             f"SELECT period, status FROM periods WHERE period IN ({','.join('?' * len(periods))})",
                             tuple(sorted(periods)))
        evidence = [{
            "type": "ENTRY_PAIR",
            "ref_ids": [e["id"] for e in es],
            "note": f"凭证 {doc} 同时出现在账期 {', '.join(sorted(periods))}",
        }, {
            "type": "PERIOD_STATE",
            "ref_ids": [],
            "note": "；".join(f"{p['period']}={p['status']}" for p in period_states),
        }]
        if chain_cross:
            evidence.append({
                "type": "ENTRY_PAIR",
                "ref_ids": chain_ids,
                "note": "冲销分录与其原始分录分属不同账期",
            })
        found.append({
            "type": "CROSS_PERIOD_REVERSAL",
            "confidence": 0.95 if chain_cross else 0.9,
            "summary": (f"凭证 {doc}（{_fmt(moved_amt)}）在 {d['period']} 与 "
                        f"{'/'.join(other_periods)} 之间跨期入账，"
                        f"本期间{'总账' if gl_gap else '结算'}缺口 {_fmt(abs(gl_gap or stl_gap))}"),
            "explains": moved_amt,
            "payload": {"doc_no": doc, "moved_minor": moved_amt,
                        "periods": sorted(periods), "chain_cross": chain_cross},
            "evidence": evidence,
        })
    return found


def detect_missing_settlement(conn, d, period_entries, stl_gap) -> list[dict]:
    """结算缺失：明细分录按金额在结算表中找不到对应，且缺口正好等于格差。"""
    if stl_gap >= 0:
        return []
    detail_amts = defaultdict(int)
    for e in period_entries:
        if e["source"] == "DETAIL":
            detail_amts[e["amount_minor"]] += 1
    for e in period_entries:
        if e["source"] == "SETTLEMENT" and detail_amts.get(e["amount_minor"], 0) > 0:
            detail_amts[e["amount_minor"]] -= 1  # 按金额对消
    unmatched = [e for e in period_entries
                 if e["source"] == "DETAIL" and detail_amts.get(e["amount_minor"], 0) > 0]
    # 每条未匹配明细分录只计一次
    seen = defaultdict(int)
    unmatched = [e for e in unmatched
                 if (seen.__setitem__(e["amount_minor"], seen[e["amount_minor"]] + 1)
                     or seen[e["amount_minor"]] <= detail_amts[e["amount_minor"]])]
    missing = sum(e["amount_minor"] for e in unmatched)
    if not unmatched or missing != -stl_gap:
        return []
    return [{
        "type": "MISSING_SETTLEMENT",
        "confidence": 0.85,
        "summary": (f"{len(unmatched)} 笔明细分录（合计 {_fmt(missing)}）"
                    f"在结算表中无对应记录，结算缺口正好为该金额"),
        "explains": missing,
        "payload": {"unmatched_entry_ids": [e["id"] for e in unmatched]},
        "evidence": [{
            "type": "ENTRY",
            "ref_ids": [e["id"] for e in unmatched],
            "note": "未匹配到结算记录的明细分录",
        }],
    }]


def detect_unposted_adjustment(conn, d, period_entries, gl_gap) -> list[dict]:
    """调整未过账：调整表有发生额，且总账缺口正好等于调整额。"""
    adj = d["amount_adjustment"]
    if adj == 0 or gl_gap != -adj:
        return []
    adj_entries = [e for e in period_entries if e["source"] == "ADJUSTMENT"]
    return [{
        "type": "UNPOSTED_ADJUSTMENT",
        "confidence": 0.9,
        "summary": f"调整表存在 {_fmt(adj)} 调整未过账到总账，总账缺口正好等于该调整额",
        "explains": abs(adj),
        "payload": {"adjustment_minor": adj},
        "evidence": [{
            "type": "ENTRY",
            "ref_ids": [e["id"] for e in adj_entries],
            "note": "调整表分录（未反映到总账）",
        }],
    }]


def detect_timing(d, period_entries, gl_gap, stl_gap) -> list[dict]:
    """时间性差异：业务日期落在其他账期。"""
    found = []
    for e in period_entries:
        if not e["entry_date"]:
            continue
        date_period = e["entry_date"][:7]
        if date_period == e["period"]:
            continue
        gap = gl_gap if e["source"] in ("GL",) else stl_gap
        if abs(gap) != abs(e["amount_minor"]) and abs(gl_gap) != abs(e["amount_minor"]):
            continue
        found.append({
            "type": "TIMING_DIFFERENCE",
            "confidence": 0.7,
            "summary": (f"分录 {e['doc_no'] or e['id']} 业务日期 {e['entry_date']}"
                        f"（属 {date_period}）却入账在 {e['period']}"),
            "explains": abs(e["amount_minor"]),
            "payload": {"entry_id": e["id"], "date_period": date_period},
            "evidence": [{
                "type": "ENTRY",
                "ref_ids": [e["id"]],
                "note": "业务日期与入账期间不一致",
            }],
        })
    return found


def detect_rounding(d, gl_gap, stl_gap) -> list[dict]:
    """汇率尾差：外币且差异极小。"""
    peak = max(abs(gl_gap), abs(stl_gap))
    if d["currency"] == BASE_CURRENCY or peak == 0 or peak > ROUNDING_THRESHOLD_MINOR:
        return []
    return [{
        "type": "FX_ROUNDING",
        "confidence": 0.6,
        "summary": f"外币 {d['currency']} 差异仅 {_fmt(peak)}，疑似汇率折算尾差",
        "explains": peak,
        "payload": {"threshold_minor": ROUNDING_THRESHOLD_MINOR},
        "evidence": [{
            "type": "THRESHOLD",
            "ref_ids": [],
            "note": f"差异 {_fmt(peak)} ≤ 尾差阈值 {_fmt(ROUNDING_THRESHOLD_MINOR)}",
        }],
    }]


def diagnose(conn, difference_id: int, actor: str) -> list[dict]:
    """对一条差异运行全部诊断规则，落库候选原因（先清除旧候选）。"""
    d = row(conn, "SELECT * FROM differences WHERE id=?", (difference_id,))
    if not d:
        raise ValueError("差异不存在")
    conn.execute("DELETE FROM cause_evidence WHERE cause_id IN"
                 " (SELECT id FROM difference_causes WHERE difference_id=?)", (difference_id,))
    conn.execute("DELETE FROM difference_causes WHERE difference_id=?", (difference_id,))

    all_entries = _entries_for_key(conn, d)
    period_entries = [e for e in all_entries if e["period"] == d["period"]]
    gl_gap, stl_gap = d["gl_gap_minor"], d["stl_gap_minor"]

    candidates: list[dict] = []
    candidates += detect_duplicates(conn, d, period_entries, gl_gap, stl_gap)
    candidates += detect_cross_period(conn, d, all_entries, gl_gap, stl_gap)
    candidates += detect_missing_settlement(conn, d, period_entries, stl_gap)
    candidates += detect_unposted_adjustment(conn, d, period_entries, gl_gap)
    candidates += detect_timing(d, period_entries, gl_gap, stl_gap)
    if not candidates:  # 只有没有更具体解释时才报尾差
        candidates += detect_rounding(d, gl_gap, stl_gap)

    for c in candidates:
        _add_cause(conn, difference_id, c["type"], c["confidence"], c["summary"],
                   c["explains"], c["payload"], c["evidence"])

    if d["status"] == "OPEN":
        conn.execute("UPDATE differences SET status='DIAGNOSED', updated_at=? WHERE id=?",
                     (now_iso(), difference_id))
    audit(conn, actor, "DIAGNOSE", "difference", difference_id,
          {"candidates": len(candidates),
           "types": [c["type"] for c in candidates]})
    conn.commit()
    return rows(conn, "SELECT * FROM difference_causes WHERE difference_id=?", (difference_id,))
