"""FastAPI 入口：差异分析全部 API + 前端静态页。"""
import json
from pathlib import Path

from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import causes, fixes, reconcile
from .db import audit, connect, init_db, now_iso, row, rows
from .seed import seed

FRONTEND_DIR = Path(__file__).resolve().parent.parent.parent / "frontend"

app = FastAPI(title="财务余额差异分析", version="1.0.0")

ORGS = {"CN01": "北京总部", "CN02": "上海分公司", "US01": "旧金山子公司"}
ACCOUNTS = {"1001": "库存现金", "1122": "应收账款", "2202": "应付账款", "6001": "主营业务收入"}
USERS = {"analyst.chen": "分析员·陈", "manager.liu": "财务经理·刘"}


def actor_of(x_user: str | None) -> str:
    return x_user if x_user in USERS else "analyst.chen"


@app.on_event("startup")
def startup():
    conn = connect()
    init_db(conn)
    if not row(conn, "SELECT period FROM periods LIMIT 1"):
        seed(conn)
    conn.close()


# ---------------------------------------------------------------- 元数据

@app.get("/api/meta")
def meta():
    conn = connect()
    periods = rows(conn, "SELECT * FROM periods ORDER BY period")
    conn.close()
    return {"orgs": ORGS, "accounts": ACCOUNTS, "users": USERS, "periods": periods,
            "cause_labels": causes.CAUSE_LABELS}


class RunRequest(BaseModel):
    period: str


@app.post("/api/seed")
def reseed(x_user: str | None = Header(default=None)):
    conn = connect()
    seed(conn)
    conn.close()
    return {"ok": True}


# ---------------------------------------------------------------- 账期

@app.get("/api/periods")
def list_periods():
    conn = connect()
    out = rows(conn, "SELECT * FROM periods ORDER BY period")
    conn.close()
    return out


@app.post("/api/periods/{period}/close")
def close_period(period: str, x_user: str | None = Header(default=None)):
    actor = actor_of(x_user)
    conn = connect()
    p = row(conn, "SELECT * FROM periods WHERE period=?", (period,))
    if not p:
        conn.close()
        raise HTTPException(404, "账期不存在")
    if p["status"] == "CLOSED":
        conn.close()
        raise HTTPException(409, "账期已关账")
    open_diffs = row(conn, """
        SELECT COUNT(*) AS c FROM differences
        WHERE period=? AND status NOT IN ('VERIFIED','WAIVED')
    """, (period,))["c"]
    conn.execute("UPDATE periods SET status='CLOSED', closed_at=?, closed_by=? WHERE period=?",
                 (now_iso(), actor, period))
    audit(conn, actor, "PERIOD_CLOSE", "period", None,
          {"period": period, "open_differences": open_diffs})
    conn.commit()
    conn.close()
    return {"period": period, "status": "CLOSED", "open_differences": open_diffs}


# ---------------------------------------------------------------- 对账运行

@app.post("/api/runs")
def start_run(req: RunRequest, x_user: str | None = Header(default=None)):
    conn = connect()
    try:
        run = reconcile.run_reconciliation(conn, req.period, actor_of(x_user))
    except ValueError as e:
        conn.close()
        raise HTTPException(404, str(e))
    conn.close()
    return run


@app.get("/api/runs")
def list_runs(period: str | None = None):
    conn = connect()
    if period:
        out = rows(conn, "SELECT * FROM recon_runs WHERE period=? ORDER BY id DESC", (period,))
    else:
        out = rows(conn, "SELECT * FROM recon_runs ORDER BY id DESC LIMIT 50")
    conn.close()
    return out


# ---------------------------------------------------------------- 差异树与差异

@app.get("/api/tree")
def tree(period: str, autorun: bool = True, x_user: str | None = Header(default=None)):
    conn = connect()
    if autorun and not reconcile.latest_run(conn, period):
        reconcile.run_reconciliation(conn, period, actor_of(x_user))
    out = reconcile.build_tree(conn, period)
    conn.close()
    return out


@app.get("/api/differences")
def list_differences(period: str | None = None, status: str | None = None,
                     org: str | None = None, currency: str | None = None):
    conn = connect()
    sql = """SELECT d.* FROM differences d
             JOIN (SELECT period, org, currency, account, MAX(id) AS mid
                   FROM differences GROUP BY period, org, currency, account) t
               ON d.id = t.mid WHERE 1=1"""
    params: list = []
    if period:
        sql += " AND d.period=?"
        params.append(period)
    if status:
        sql += " AND d.status=?"
        params.append(status)
    if org:
        sql += " AND d.org=?"
        params.append(org)
    if currency:
        sql += " AND d.currency=?"
        params.append(currency)
    out = rows(conn, sql + " ORDER BY d.period, d.org, d.account", tuple(params))
    conn.close()
    return out


@app.get("/api/differences/{diff_id}")
def difference_detail(diff_id: int):
    conn = connect()
    d = row(conn, "SELECT * FROM differences WHERE id=?", (diff_id,))
    if not d:
        conn.close()
        raise HTTPException(404, "差异不存在")
    cause_list = rows(conn, "SELECT * FROM difference_causes WHERE difference_id=?", (diff_id,))
    for c in cause_list:
        c["payload"] = json.loads(c["payload"] or "{}")
        c["evidence"] = rows(conn, "SELECT * FROM cause_evidence WHERE cause_id=?", (c["id"],))
        for ev in c["evidence"]:
            ev["ref_ids"] = json.loads(ev["ref_ids"] or "[]")
            if ev["ref_ids"]:
                marks = ",".join("?" * len(ev["ref_ids"]))
                ev["entries"] = rows(conn, f"SELECT * FROM entries WHERE id IN ({marks})",
                                     tuple(ev["ref_ids"]))
    proposals = rows(conn, "SELECT * FROM fix_proposals WHERE difference_id=? ORDER BY id DESC",
                     (diff_id,))
    for p in proposals:
        p["actions"] = rows(conn, "SELECT * FROM fix_actions WHERE proposal_id=?", (p["id"],))
    versions = rows(conn, """
        SELECT * FROM balances WHERE period=? AND org=? AND currency=? AND account=?
        ORDER BY source, version
    """, (d["period"], d["org"], d["currency"], d["account"]))
    conn.close()
    return {"difference": d, "causes": cause_list, "proposals": proposals,
            "balance_versions": versions}


@app.post("/api/differences/{diff_id}/diagnose")
def diagnose(diff_id: int, x_user: str | None = Header(default=None)):
    conn = connect()
    try:
        out = causes.diagnose(conn, diff_id, actor_of(x_user))
    except ValueError as e:
        conn.close()
        raise HTTPException(404, str(e))
    conn.close()
    return out


class WaiveRequest(BaseModel):
    reason: str


@app.post("/api/differences/{diff_id}/waive")
def waive(diff_id: int, req: WaiveRequest, x_user: str | None = Header(default=None)):
    actor = actor_of(x_user)
    if len(req.reason.strip()) < 5:
        raise HTTPException(422, "豁免必须填写原因（至少 5 个字符）")
    conn = connect()
    d = row(conn, "SELECT * FROM differences WHERE id=?", (diff_id,))
    if not d:
        conn.close()
        raise HTTPException(404, "差异不存在")
    conn.execute("UPDATE differences SET status='WAIVED', note=?, updated_at=? WHERE id=?",
                 (req.reason, now_iso(), diff_id))
    audit(conn, actor, "DIFF_WAIVE", "difference", diff_id, {"reason": req.reason})
    conn.commit()
    conn.close()
    return {"ok": True}


@app.post("/api/causes/{cause_id}/confirm")
def confirm_cause(cause_id: int, x_user: str | None = Header(default=None)):
    conn = connect()
    conn.execute("UPDATE difference_causes SET status='CONFIRMED' WHERE id=?", (cause_id,))
    audit(conn, actor_of(x_user), "CAUSE_CONFIRM", "difference_cause", cause_id, {})
    conn.commit()
    conn.close()
    return {"ok": True}


@app.post("/api/causes/{cause_id}/reject")
def reject_cause(cause_id: int, x_user: str | None = Header(default=None)):
    conn = connect()
    conn.execute("UPDATE difference_causes SET status='REJECTED' WHERE id=?", (cause_id,))
    audit(conn, actor_of(x_user), "CAUSE_REJECT", "difference_cause", cause_id, {})
    conn.commit()
    conn.close()
    return {"ok": True}


# ---------------------------------------------------------------- 修复建议

class ProposalRequest(BaseModel):
    cause_id: int


@app.post("/api/differences/{diff_id}/proposals")
def create_proposal(diff_id: int, req: ProposalRequest,
                    x_user: str | None = Header(default=None)):
    conn = connect()
    try:
        p = fixes.create_proposal(conn, diff_id, req.cause_id, actor_of(x_user))
    except ValueError as e:
        conn.close()
        raise HTTPException(422, str(e))
    conn.close()
    return p


@app.get("/api/proposals")
def list_proposals(status: str | None = None):
    conn = connect()
    sql = """SELECT p.*, d.period, d.org, d.currency, d.account, d.diff_minor
             FROM fix_proposals p JOIN differences d ON d.id = p.difference_id"""
    params: list = []
    if status:
        sql += " WHERE p.status=?"
        params.append(status)
    out = rows(conn, sql + " ORDER BY p.id DESC", tuple(params))
    for p in out:
        p["actions"] = rows(conn, "SELECT * FROM fix_actions WHERE proposal_id=?", (p["id"],))
    conn.close()
    return out


@app.get("/api/proposals/{pid}")
def proposal_detail(pid: int):
    conn = connect()
    p = fixes.get_proposal(conn, pid)
    if not p:
        conn.close()
        raise HTTPException(404, "修复建议不存在")
    p["validation_errors"] = fixes.validate_proposal(conn, p) if p["status"] in ("DRAFT", "SUBMITTED", "APPROVED") else []
    conn.close()
    return p


@app.post("/api/proposals/{pid}/submit")
def submit(pid: int, x_user: str | None = Header(default=None)):
    conn = connect()
    try:
        p = fixes.submit_proposal(conn, pid, actor_of(x_user))
    except ValueError as e:
        conn.close()
        raise HTTPException(409, str(e))
    conn.close()
    return p


@app.post("/api/proposals/{pid}/approve")
def approve(pid: int, x_user: str | None = Header(default=None)):
    conn = connect()
    try:
        p = fixes.approve_proposal(conn, pid, actor_of(x_user))
    except PermissionError as e:
        conn.close()
        raise HTTPException(403, str(e))
    except ValueError as e:
        conn.close()
        raise HTTPException(409, str(e))
    conn.close()
    return p


class RejectRequest(BaseModel):
    reason: str = ""


@app.post("/api/proposals/{pid}/reject")
def reject(pid: int, req: RejectRequest, x_user: str | None = Header(default=None)):
    conn = connect()
    try:
        p = fixes.reject_proposal(conn, pid, actor_of(x_user), req.reason)
    except ValueError as e:
        conn.close()
        raise HTTPException(409, str(e))
    conn.close()
    return p


@app.post("/api/proposals/{pid}/apply")
def apply(pid: int, x_user: str | None = Header(default=None)):
    conn = connect()
    try:
        p = fixes.apply_proposal(conn, pid, actor_of(x_user))
    except ValueError as e:
        conn.close()
        raise HTTPException(409, str(e))
    conn.close()
    return p


# ---------------------------------------------------------------- 版本链与审计

@app.get("/api/versions")
def versions(source: str, period: str, org: str, currency: str, account: str):
    conn = connect()
    out = rows(conn, """
        SELECT b.*, p.title AS proposal_title
        FROM balances b LEFT JOIN fix_proposals p ON p.id = b.proposal_id
        WHERE b.source=? AND b.period=? AND b.org=? AND b.currency=? AND b.account=?
        ORDER BY b.version
    """, (source, period, org, currency, account))
    conn.close()
    return out


@app.get("/api/audit")
def audit_log(entity_type: str | None = None, entity_id: int | None = None,
              limit: int = 200):
    conn = connect()
    sql = "SELECT * FROM audit_log WHERE 1=1"
    params: list = []
    if entity_type:
        sql += " AND entity_type=?"
        params.append(entity_type)
    if entity_id is not None:
        sql += " AND entity_id=?"
        params.append(entity_id)
    out = rows(conn, sql + " ORDER BY id DESC LIMIT ?", tuple(params + [limit]))
    for r in out:
        r["payload"] = json.loads(r["payload"] or "{}")
    conn.close()
    return out


# ---------------------------------------------------------------- 前端

@app.get("/")
def index():
    return FileResponse(FRONTEND_DIR / "index.html")


app.mount("/static", StaticFiles(directory=FRONTEND_DIR), name="static")
