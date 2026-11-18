"""端到端测试：对账 → 诊断 → 修复（含受控更正、跨期冲销、重复入账验证、不可变性）。"""
import os
import sys

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


@pytest.fixture()
def client(tmp_path, monkeypatch):
    db = tmp_path / "test.db"
    monkeypatch.setenv("RECON_DB", str(db))
    from app.db import connect, init_db
    from app.seed import seed
    conn = connect(str(db))
    init_db(conn)
    seed(conn)
    conn.close()
    from app.main import app
    return TestClient(app)


ANALYST = {"X-User": "analyst.chen"}
MANAGER = {"X-User": "manager.liu"}


def run_recon(client, period):
    r = client.post("/api/runs", json={"period": period}, headers=ANALYST)
    assert r.status_code == 200, r.text
    return r.json()


def diffs(client, period):
    return client.get("/api/differences", params={"period": period}).json()


def find_diff(client, period, org, account, currency="CNY"):
    for d in diffs(client, period):
        if d["org"] == org and d["account"] == account and d["currency"] == currency:
            return d
    raise AssertionError(f"未找到差异 {period} {org} {account}")


def diagnose(client, diff_id):
    r = client.post(f"/api/differences/{diff_id}/diagnose", headers=ANALYST)
    assert r.status_code == 200, r.text
    return r.json()


# ---------------------------------------------------------------- 对账

def test_recon_finds_expected_differences(client):
    run11 = run_recon(client, "2026-11")
    assert run11["total_diffs"] == 5  # 重复/调整未过账/跨期/结算缺失/尾差
    run10 = run_recon(client, "2026-10")
    assert run10["total_diffs"] == 2  # 重复(关账) + 跨期(关账侧)
    run09 = run_recon(client, "2026-09")
    assert run09["total_diffs"] == 0


def test_gap_amounts(client):
    run_recon(client, "2026-11")
    dup = find_diff(client, "2026-11", "CN01", "6001")
    assert dup["gl_gap_minor"] == -50_000_000  # 明细多记 500,000.00
    adj = find_diff(client, "2026-11", "CN01", "2202")
    assert adj["gl_gap_minor"] == -750_000     # 调整 7,500.00 未过账
    stl = find_diff(client, "2026-11", "US01", "1122", "USD")
    assert stl["stl_gap_minor"] == -2_500_000  # 结算缺 25,000.00
    fx = find_diff(client, "2026-11", "US01", "6001", "USD")
    assert fx["diff_minor"] == 3               # 尾差 0.03


# ---------------------------------------------------------------- 诊断

def test_diagnose_duplicate(client):
    run_recon(client, "2026-11")
    d = find_diff(client, "2026-11", "CN01", "6001")
    causes = diagnose(client, d["id"])
    dup = next(c for c in causes if c["cause_type"] == "DUPLICATE_ENTRY")
    assert dup["confidence"] >= 0.9
    assert dup["explains_minor"] == 50_000_000


def test_diagnose_cross_period(client):
    run_recon(client, "2026-11")
    d = find_diff(client, "2026-11", "CN02", "2202")
    causes = diagnose(client, d["id"])
    cp = next(c for c in causes if c["cause_type"] == "CROSS_PERIOD_REVERSAL")
    assert cp["explains_minor"] == 80_000_000
    detail = client.get(f"/api/differences/{d['id']}").json()
    ev = [e for c in detail["causes"] for e in c["evidence"]]
    assert any(e["evidence_type"] == "PERIOD_STATE" for e in ev)  # 证据含账期状态


def test_diagnose_missing_settlement_and_unposted_adjustment(client):
    run_recon(client, "2026-11")
    stl = find_diff(client, "2026-11", "US01", "1122", "USD")
    types = {c["cause_type"] for c in diagnose(client, stl["id"])}
    assert "MISSING_SETTLEMENT" in types
    adj = find_diff(client, "2026-11", "CN01", "2202")
    types = {c["cause_type"] for c in diagnose(client, adj["id"])}
    assert "UNPOSTED_ADJUSTMENT" in types


def test_diagnose_rounding_only_when_no_specific_cause(client):
    run_recon(client, "2026-11")
    fx = find_diff(client, "2026-11", "US01", "6001", "USD")
    causes = diagnose(client, fx["id"])
    assert [c["cause_type"] for c in causes] == ["FX_ROUNDING"]


# ---------------------------------------------------------------- 修复（开放期间）

def _make_proposal(client, diff_id, cause_type):
    causes = diagnose(client, diff_id)
    cause = next(c for c in causes if c["cause_type"] == cause_type)
    r = client.post(f"/api/differences/{diff_id}/proposals",
                    json={"cause_id": cause["id"]}, headers=ANALYST)
    assert r.status_code == 200, r.text
    return r.json()


def test_open_period_duplicate_fix_flow(client):
    run_recon(client, "2026-11")
    d = find_diff(client, "2026-11", "CN01", "6001")
    p = _make_proposal(client, d["id"], "DUPLICATE_ENTRY")
    assert p["proposal_type"] == "REVERSAL_ENTRY"

    r = client.post(f"/api/proposals/{p['id']}/apply", headers=ANALYST)
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "VERIFIED"

    after = client.get(f"/api/differences/{d['id']}").json()["difference"]
    assert after["status"] == "VERIFIED"
    assert after["diff_minor"] == 0

    # 原始余额（version=1）未被改写；新版本为 version=2
    versions = client.get("/api/versions", params={
        "source": "DETAIL", "period": "2026-11", "org": "CN01",
        "currency": "CNY", "account": "6001"}).json()
    assert [v["version"] for v in versions] == [1, 2]
    assert versions[0]["amount_minor"] == 170_000_000   # 原始值原封不动
    assert versions[1]["amount_minor"] == 120_000_000   # 新版本 = 修正后


def test_unposted_adjustment_fix(client):
    run_recon(client, "2026-11")
    d = find_diff(client, "2026-11", "CN01", "2202")
    p = _make_proposal(client, d["id"], "UNPOSTED_ADJUSTMENT")
    r = client.post(f"/api/proposals/{p['id']}/apply", headers=ANALYST)
    assert r.json()["status"] == "VERIFIED"


def test_missing_settlement_fix(client):
    run_recon(client, "2026-11")
    d = find_diff(client, "2026-11", "US01", "1122", "USD")
    p = _make_proposal(client, d["id"], "MISSING_SETTLEMENT")
    r = client.post(f"/api/proposals/{p['id']}/apply", headers=ANALYST)
    assert r.json()["status"] == "VERIFIED"


# ---------------------------------------------------------------- 受控更正（关账期间）

def test_closed_period_requires_controlled_correction(client):
    run_recon(client, "2026-10")
    d = find_diff(client, "2026-10", "CN01", "1001")
    p = _make_proposal(client, d["id"], "DUPLICATE_ENTRY")
    assert p["proposal_type"] == "CONTROLLED_CORRECTION"

    # 未审批直接应用 → 拒绝
    r = client.post(f"/api/proposals/{p['id']}/apply", headers=ANALYST)
    assert r.status_code == 409

    # 提交后创建人自审 → 拒绝（双人控制）
    client.post(f"/api/proposals/{p['id']}/submit", headers=ANALYST)
    r = client.post(f"/api/proposals/{p['id']}/approve", headers=ANALYST)
    assert r.status_code == 403

    # 经理审批 → 应用 → 验证通过
    r = client.post(f"/api/proposals/{p['id']}/approve", headers=MANAGER)
    assert r.status_code == 200
    r = client.post(f"/api/proposals/{p['id']}/apply", headers=MANAGER)
    assert r.json()["status"] == "VERIFIED"
    after = client.get(f"/api/differences/{d['id']}").json()["difference"]
    assert after["status"] == "VERIFIED"


def test_cross_period_reversal_fix_nets_zero_and_verifies_both_periods(client):
    run_recon(client, "2026-10")
    run_recon(client, "2026-11")
    d11 = find_diff(client, "2026-11", "CN02", "2202")
    d10 = find_diff(client, "2026-10", "CN02", "2202")

    p = _make_proposal(client, d11["id"], "CROSS_PERIOD_REVERSAL")
    assert p["proposal_type"] == "CONTROLLED_CORRECTION"  # 动作触及已关账的 2026-10
    periods = {a["period"] for a in p["actions"]}
    assert periods == {"2026-10", "2026-11"}
    net = sum(a["delta_minor"] for a in p["actions"] if a["action_kind"] == "BALANCE_VERSION")
    assert net == 0  # 跨期平移净额为 0

    client.post(f"/api/proposals/{p['id']}/submit", headers=ANALYST)
    client.post(f"/api/proposals/{p['id']}/approve", headers=MANAGER)
    r = client.post(f"/api/proposals/{p['id']}/apply", headers=MANAGER)
    assert r.json()["status"] == "VERIFIED"

    # 两个账期的差异都被复核通过
    assert client.get(f"/api/differences/{d11['id']}").json()["difference"]["status"] == "VERIFIED"
    assert client.get(f"/api/differences/{d10['id']}").json()["difference"]["status"] == "VERIFIED"


# ---------------------------------------------------------------- 验证规则

def test_duplicate_fix_cannot_be_applied_twice(client):
    run_recon(client, "2026-11")
    d = find_diff(client, "2026-11", "CN01", "6001")
    p1 = _make_proposal(client, d["id"], "DUPLICATE_ENTRY")
    client.post(f"/api/proposals/{p1['id']}/apply", headers=ANALYST)

    # 第二次针对同一重复组的建议：应用时重复入账验证应失败
    causes = diagnose(client, d["id"])
    dup = next((c for c in causes if c["cause_type"] == "DUPLICATE_ENTRY"), None)
    if dup:  # 差异已平，诊断可能不再报重复；若仍报则应用必须失败
        p2 = client.post(f"/api/differences/{d['id']}/proposals",
                         json={"cause_id": dup["id"]}, headers=ANALYST).json()
        r = client.post(f"/api/proposals/{p2['id']}/apply", headers=ANALYST)
        assert r.status_code == 409
        assert "重复入账验证失败" in r.json()["detail"]


def test_original_balances_immutable_after_fixes(client):
    run_recon(client, "2026-11")
    d = find_diff(client, "2026-11", "CN01", "6001")
    p = _make_proposal(client, d["id"], "DUPLICATE_ENTRY")
    client.post(f"/api/proposals/{p['id']}/apply", headers=ANALYST)
    # 版本链完整：v1 原始行未变
    versions = client.get("/api/versions", params={
        "source": "DETAIL", "period": "2026-11", "org": "CN01",
        "currency": "CNY", "account": "6001"}).json()
    v1 = next(v for v in versions if v["version"] == 1)
    assert v1["amount_minor"] == 170_000_000
    assert v1["proposal_id"] is None


def test_waive_requires_reason_and_is_audited(client):
    run_recon(client, "2026-11")
    fx = find_diff(client, "2026-11", "US01", "6001", "USD")
    r = client.post(f"/api/differences/{fx['id']}/waive", json={"reason": "短"}, headers=ANALYST)
    assert r.status_code == 422
    r = client.post(f"/api/differences/{fx['id']}/waive",
                    json={"reason": "汇率折算尾差，金额不重大，予以豁免"}, headers=MANAGER)
    assert r.status_code == 200
    logs = client.get("/api/audit", params={"entity_type": "difference"}).json()
    assert any(l["action"] == "DIFF_WAIVE" for l in logs)


def test_close_period_blocks_normal_fix(client):
    run_recon(client, "2026-11")
    d = find_diff(client, "2026-11", "CN01", "6001")
    r = client.post("/api/periods/2026-11/close", headers=MANAGER)
    assert r.status_code == 200
    # 关账后新建议自动变为受控更正
    p = _make_proposal(client, d["id"], "DUPLICATE_ENTRY")
    assert p["proposal_type"] == "CONTROLLED_CORRECTION"


def test_audit_trail_covers_full_lifecycle(client):
    run_recon(client, "2026-11")
    d = find_diff(client, "2026-11", "CN01", "6001")
    p = _make_proposal(client, d["id"], "DUPLICATE_ENTRY")
    client.post(f"/api/proposals/{p['id']}/apply", headers=ANALYST)
    actions = {l["action"] for l in client.get("/api/audit").json()}
    assert {"RUN_START", "RUN_DONE", "DIAGNOSE", "PROPOSAL_CREATE",
            "BALANCE_VERSION", "PROPOSAL_APPLY", "DIFF_VERIFIED"} <= actions


def test_tree_drilldown(client):
    run_recon(client, "2026-11")
    tree = client.get("/api/tree", params={"period": "2026-11"}).json()
    assert tree["diff_count"] == 5
    cn01 = next(o for o in tree["orgs"] if o["org"] == "CN01")
    cny = next(c for c in cn01["currencies"] if c["currency"] == "CNY")
    acc6001 = next(a for a in cny["accounts"] if a["account"] == "6001")
    assert acc6001["status"] == "OPEN"
    assert acc6001["amounts"]["DETAIL"] == 170_000_000
