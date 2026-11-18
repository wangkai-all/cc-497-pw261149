/* 财务余额差异分析 — 前端逻辑 */
"use strict";

const state = {
  meta: null,
  period: null,
  user: localStorage.getItem("recon.user") || "analyst.chen",
  tree: null,
  collapsed: new Set(),
  currentDiffId: null,
};

const SOURCE_LABEL = { GL: "总账", DETAIL: "明细", SETTLEMENT: "结算", ADJUSTMENT: "调整" };
const SOURCE_VAR = { GL: "--series-1", DETAIL: "--series-2", SETTLEMENT: "--series-3", ADJUSTMENT: "--series-4" };
const DIFF_STATUS = {
  OPEN: "待处理", DIAGNOSED: "已诊断", FIX_PROPOSED: "建议已生成",
  FIX_APPLIED: "已应用待复核", VERIFIED: "已验证", WAIVED: "已豁免", OK: "平衡",
};
const PROPOSAL_STATUS = {
  DRAFT: "草稿", SUBMITTED: "已提交", APPROVED: "已审批",
  REJECTED: "已驳回", APPLIED: "已应用", VERIFIED: "已验证",
};
const PROPOSAL_TYPE = {
  ADJUSTMENT_ENTRY: "调整分录", REVERSAL_ENTRY: "红字冲销", CONTROLLED_CORRECTION: "受控更正",
};
const ACTION_LABEL = {
  RUN_START: "开始对账", RUN_DONE: "对账完成", DIAGNOSE: "差异诊断",
  CAUSE_CONFIRM: "确认原因", CAUSE_REJECT: "排除原因",
  PROPOSAL_CREATE: "创建建议", PROPOSAL_SUBMIT: "提交建议", PROPOSAL_APPROVE: "审批通过",
  PROPOSAL_REJECT: "驳回建议", PROPOSAL_APPLY: "应用修复", PROPOSAL_VERIFY: "修复验证",
  DIFF_WAIVE: "差异豁免", DIFF_VERIFIED: "差异验证通过", PERIOD_CLOSE: "账期关账",
  BALANCE_VERSION: "生成余额新版本", SEED: "装载种子数据",
};

/* ---------------- 工具 ---------------- */
const $ = (sel) => document.querySelector(sel);
const fmt = (minor) => (minor / 100).toLocaleString("zh-CN", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
const esc = (s) => String(s ?? "").replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));

async function api(path, opts = {}) {
  const res = await fetch(path, {
    ...opts,
    headers: { "Content-Type": "application/json", "X-User": state.user, ...(opts.headers || {}) },
  });
  if (!res.ok) {
    let msg = res.statusText;
    try { msg = (await res.json()).detail || msg; } catch { /* ignore */ }
    throw new Error(msg);
  }
  return res.json();
}

function toast(msg, isErr = false) {
  const t = $("#toast");
  t.textContent = msg;
  t.style.background = isErr ? "var(--critical)" : "var(--text-primary)";
  t.style.opacity = 1;
  clearTimeout(t._timer);
  t._timer = setTimeout(() => (t.style.opacity = 0), 3200);
}

async function guard(fn) {
  try { await fn(); } catch (e) { toast(e.message, true); }
}

/* ---------------- 初始化 ---------------- */
async function init() {
  state.meta = await api("/api/meta");
  const userSel = $("#userSelect");
  userSel.innerHTML = Object.entries(state.meta.users)
    .map(([k, v]) => `<option value="${k}" ${k === state.user ? "selected" : ""}>${v}（${k}）</option>`).join("");
  userSel.onchange = () => { state.user = userSel.value; localStorage.setItem("recon.user", state.user); renderProgress(); };

  const periodSel = $("#periodSelect");
  periodSel.innerHTML = state.meta.periods
    .map((p) => `<option value="${p.period}">${p.period}${p.status === "CLOSED" ? "（已关账）" : ""}</option>`).join("");
  state.period = state.meta.periods[state.meta.periods.length - 1].period;
  periodSel.value = state.period;
  periodSel.onchange = () => { state.period = periodSel.value; loadTree(); };

  $("#runBtn").onclick = () => guard(async () => {
    const run = await api("/api/runs", { method: "POST", body: JSON.stringify({ period: state.period }) });
    toast(`对账完成：${run.total_keys} 个键，发现 ${run.total_diffs} 项差异`);
    loadTree();
  });
  $("#closePeriodBtn").onclick = () => guard(async () => {
    if (!confirm(`确定对账期 ${state.period} 关账？关账后只能走受控更正。`)) return;
    const r = await api(`/api/periods/${state.period}/close`, { method: "POST" });
    toast(`已关账（尚有 ${r.open_differences} 项未决差异）`);
    await reloadMeta(); loadTree();
  });
  $("#reseedBtn").onclick = () => guard(async () => {
    if (!confirm("重置为初始演示数据？所有修复记录将清空。")) return;
    await api("/api/seed", { method: "POST" });
    await reloadMeta(); loadTree(); loadAudit(); renderProgress();
    toast("已重置演示数据");
  });
  $("#themeBtn").onclick = () => {
    const root = document.documentElement;
    root.dataset.theme = root.dataset.theme === "dark" ? "light" : "dark";
  };

  document.querySelectorAll(".tabs button").forEach((b) => {
    b.onclick = () => {
      document.querySelectorAll(".tabs button").forEach((x) => x.classList.toggle("active", x === b));
      ["tree", "progress", "audit"].forEach((t) => ($(`#tab-${t}`).style.display = t === b.dataset.tab ? "" : "none"));
      if (b.dataset.tab === "progress") renderProgress();
      if (b.dataset.tab === "audit") loadAudit();
    };
  });
  $("#drawerClose").onclick = closeDrawer;
  $("#drawerMask").onclick = closeDrawer;
  $("#auditFilter").onchange = loadAudit;

  await loadTree();
}

async function reloadMeta() {
  state.meta = await api("/api/meta");
  const periodSel = $("#periodSelect");
  periodSel.innerHTML = state.meta.periods
    .map((p) => `<option value="${p.period}">${p.period}${p.status === "CLOSED" ? "（已关账）" : ""}</option>`).join("");
  periodSel.value = state.period;
}

/* ---------------- 差异树 ---------------- */
async function loadTree() {
  state.tree = await api(`/api/tree?period=${state.period}`);
  renderPeriodStatus();
  renderTiles();
  renderTree();
}

function renderPeriodStatus() {
  const p = state.meta.periods.find((x) => x.period === state.period);
  const closed = p && p.status === "CLOSED";
  $("#periodStatus").innerHTML = closed
    ? `<span class="badge st-REJECTED">已关账 · 仅受控更正</span>`
    : `<span class="badge st-OK">开放中</span>`;
  $("#closePeriodBtn").disabled = closed;
}

function renderTiles() {
  const t = state.tree;
  $("#tiles").innerHTML = `
    <div class="tile"><div class="label">未决差异</div><div class="value num">${t.diff_count}</div>
      <div class="sub">共 ${t.account_count} 个科目键</div></div>
    <div class="tile"><div class="label">未决差异金额</div><div class="value num">${fmt(t.total_abs_diff)}</div>
      <div class="sub">按最大格差汇总</div></div>
    <div class="tile"><div class="label">已验证修复</div><div class="value num">${t.verified_count}</div>
      <div class="sub">复核格差归零</div></div>
    <div class="tile"><div class="label">最近对账</div><div class="value" style="font-size:15px">${t.run ? "#" + t.run.id : "—"}</div>
      <div class="sub">${t.run ? esc(t.run.finished_at).replace("T", " ").slice(0, 19) : "尚未运行"}</div></div>`;
}

function statusBadge(status) {
  return `<span class="badge st-${status}">${DIFF_STATUS[status] || status}</span>`;
}

function gapCell(v) {
  const cls = v === 0 ? "gap-zero" : v > 0 ? "gap-pos" : "gap-neg";
  return `<td class="num ${cls}">${v === 0 ? "—" : fmt(v)}</td>`;
}

function renderTree() {
  const t = state.tree;
  const rows = [];
  rows.push(`<tr class="org-row"><td><span class="caret" data-node="root">${state.collapsed.has("root") ? "▸" : "▾"}</span>账期 ${t.period}</td>
    <td colspan="8"></td><td>${t.diff_count ? `<span class="badge st-OPEN">${t.diff_count} 项差异</span>` : statusBadge("OK")}</td></tr>`);
  if (!state.collapsed.has("root")) {
    for (const org of t.orgs) {
      const oKey = `o:${org.org}`;
      rows.push(`<tr class="org-row"><td class="indent-1"><span class="caret" data-node="${oKey}">${state.collapsed.has(oKey) ? "▸" : "▾"}</span>${org.org} ${esc(state.meta.orgs[org.org] || "")}</td>
        <td colspan="6"></td><td class="num">${org.total_abs_diff ? fmt(org.total_abs_diff) : "—"}</td>
        <td>${org.diff_count ? `<span class="badge st-OPEN">${org.diff_count} 项</span>` : statusBadge("OK")}</td></tr>`);
      if (state.collapsed.has(oKey)) continue;
      for (const cur of org.currencies) {
        const cKey = `c:${org.org}:${cur.currency}`;
        rows.push(`<tr class="currency-row"><td class="indent-2"><span class="caret" data-node="${cKey}">${state.collapsed.has(cKey) ? "▸" : "▾"}</span>${cur.currency}</td>
          <td colspan="6"></td><td class="num">${cur.total_abs_diff ? fmt(cur.total_abs_diff) : "—"}</td>
          <td>${cur.diff_count ? `<span class="badge st-OPEN">${cur.diff_count} 项</span>` : statusBadge("OK")}</td></tr>`);
        if (state.collapsed.has(cKey)) continue;
        for (const a of cur.accounts) {
          const hasDiff = a.diff_minor > 0 && a.status !== "VERIFIED" && a.status !== "WAIVED";
          rows.push(`<tr class="account-row ${hasDiff ? "has-diff" : ""}" data-diff="${a.difference_id ?? ""}">
            <td class="indent-2" style="padding-left:70px">${a.account} ${esc(state.meta.accounts[a.account] || "")}</td>
            <td class="num">${fmt(a.amounts.GL)}</td><td class="num">${fmt(a.amounts.DETAIL)}</td>
            <td class="num">${fmt(a.amounts.SETTLEMENT)}</td><td class="num">${fmt(a.amounts.ADJUSTMENT)}</td>
            <td class="num">${fmt(a.expected)}</td>
            ${gapCell(a.gl_gap)}${gapCell(a.stl_gap)}
            <td class="num">${a.diff_minor ? fmt(a.diff_minor) : "—"}</td>
            <td>${statusBadge(a.status)}</td></tr>`);
        }
      }
    }
  }
  $("#treeTable tbody").innerHTML = rows.join("");
  $("#treeTable").querySelectorAll(".caret").forEach((c) => {
    c.onclick = (e) => {
      e.stopPropagation();
      const k = c.dataset.node;
      state.collapsed.has(k) ? state.collapsed.delete(k) : state.collapsed.add(k);
      renderTree();
    };
  });
  $("#treeTable").querySelectorAll("tr.account-row").forEach((tr) => {
    tr.onclick = () => tr.dataset.diff && openDrawer(Number(tr.dataset.diff));
  });
}

/* ---------------- 差异详情抽屉 ---------------- */
async function openDrawer(diffId) {
  state.currentDiffId = diffId;
  const data = await api(`/api/differences/${diffId}`);
  const d = data.difference;
  const body = $("#drawerBody");
  body.innerHTML = `
    <h2>差异 #${d.id} ${statusBadge(d.status)}</h2>
    <div class="muted">${d.period} · ${d.org} ${esc(state.meta.orgs[d.org] || "")} · ${d.currency} · ${d.account} ${esc(state.meta.accounts[d.account] || "")}</div>
    <div class="kv">
      <div><span class="k">总账差（总账−应平）</span><span class="num">${fmt(d.gl_gap_minor)}</span></div>
      <div><span class="k">结算差（结算−应平）</span><span class="num">${fmt(d.stl_gap_minor)}</span></div>
      <div><span class="k">差异额</span><span class="num"><b>${fmt(d.diff_minor)}</b></span></div>
      <div><span class="k">最近更新</span><span>${esc(d.updated_at).replace("T", " ").slice(0, 19)}</span></div>
    </div>
    <h3>四表金额对比</h3>
    <div class="chart" id="compareChart"></div>
    <div class="actions" style="margin:10px 0">
      <button class="primary small" id="diagnoseBtn">运行诊断</button>
      <button class="small" id="waiveBtn">豁免此差异</button>
    </div>
    <h3>候选原因与证据（${data.causes.length}）</h3>
    <div id="causeList">${data.causes.length ? "" : '<div class="muted">尚未诊断，点击“运行诊断”。</div>'}</div>
    <h3>修复建议（${data.proposals.length}）</h3>
    <div id="diffProposals">${data.proposals.length ? "" : '<div class="muted">暂无。确认候选原因后可生成修复建议。</div>'}</div>
    <h3>余额版本链（原始余额不可改写，修复只追加新版本）</h3>
    <div class="versions" id="versionChain"></div>`;

  renderCompareChart(d);
  renderCauses(data.causes);
  renderDiffProposals(data.proposals);
  renderVersionChain(data.balance_versions);

  $("#diagnoseBtn").onclick = () => guard(async () => {
    await api(`/api/differences/${diffId}/diagnose`, { method: "POST" });
    toast("诊断完成"); openDrawer(diffId); loadTree();
  });
  $("#waiveBtn").onclick = () => guard(async () => {
    const reason = prompt("豁免原因（至少 5 个字符，将记入审计）：");
    if (!reason) return;
    await api(`/api/differences/${diffId}/waive`, { method: "POST", body: JSON.stringify({ reason }) });
    toast("已豁免"); openDrawer(diffId); loadTree();
  });

  $("#drawer").classList.add("open");
  $("#drawerMask").classList.add("open");
}

function closeDrawer() {
  $("#drawer").classList.remove("open");
  $("#drawerMask").classList.remove("open");
}

/* 四表对比：横向发散条形图（零轴居中，负值向左）。
   4 个系列使用分类色板槽位 1-4；每条柱直接标注数值（缓解浅色的对比度告警）。 */
function renderCompareChart(d) {
  const el = $("#compareChart");
  const vals = [
    { key: "GL", v: d.amount_gl }, { key: "DETAIL", v: d.amount_detail },
    { key: "SETTLEMENT", v: d.amount_settlement }, { key: "ADJUSTMENT", v: d.amount_adjustment },
  ];
  const maxAbs = Math.max(...vals.map((x) => Math.abs(x.v)), 1);
  const legend = `<div class="legend">${vals.map((x) =>
    `<span class="item"><span class="swatch" style="background:var(${SOURCE_VAR[x.key]})"></span>${SOURCE_LABEL[x.key]}</span>`).join("")}
    <span class="item muted">应平金额（明细+调整）：${fmt(d.amount_detail + d.amount_adjustment)}</span></div>`;
  const rowsHtml = vals.map((x) => {
    const pct = (Math.abs(x.v) / maxAbs) * 50;
    const neg = x.v < 0;
    const style = neg
      ? `right:50%;width:${pct}%;background:var(${SOURCE_VAR[x.key]})`
      : `left:50%;width:${pct}%;background:var(${SOURCE_VAR[x.key]})`;
    return `<div class="bar-row">
      <div class="name">${SOURCE_LABEL[x.key]}</div>
      <div class="bar-track">
        <div class="bar-zero" style="left:50%"></div>
        <div class="bar ${neg ? "neg" : ""}" style="${style}" data-tip="${SOURCE_LABEL[x.key]}：${fmt(x.v)}"></div>
      </div>
      <div class="bar-val num ${x.v !== vals[1].v + vals[3].v ? "strong" : ""}">${fmt(x.v)}</div>
    </div>`;
  }).join("");
  el.innerHTML = legend + rowsHtml;
  el.querySelectorAll(".bar").forEach((bar) => {
    bar.addEventListener("mousemove", (e) => showTip(e, bar.dataset.tip));
    bar.addEventListener("mouseleave", hideTip);
  });
}

function showTip(e, text) {
  const tip = $("#tooltip");
  tip.textContent = text;
  tip.style.opacity = 1;
  tip.style.left = e.clientX + 12 + "px";
  tip.style.top = e.clientY + 12 + "px";
}
function hideTip() { $("#tooltip").style.opacity = 0; }

function renderCauses(causes) {
  const el = $("#causeList");
  if (!causes.length) return;
  el.innerHTML = causes.map((c) => `
    <div class="cause ${c.status === "REJECTED" ? "rejected" : ""}">
      <div class="head">
        <span class="type">${esc(state.meta.cause_labels[c.cause_type] || c.cause_type)}</span>
        <span class="muted">置信度 ${(c.confidence * 100).toFixed(0)}%</span>
        <div class="conf"><div style="width:${c.confidence * 100}%"></div></div>
        <span class="badge st-${c.status === "CONFIRMED" ? "VERIFIED" : c.status === "REJECTED" ? "REJECTED" : "DIAGNOSED"}">
          ${{ CANDIDATE: "候选", CONFIRMED: "已确认", REJECTED: "已排除" }[c.status]}</span>
      </div>
      <div class="summary">${esc(c.summary)}</div>
      ${c.evidence.map((ev) => `
        <div class="evidence">
          <div class="ev-note">证据 · ${esc(ev.note)}</div>
          ${ev.entries && ev.entries.length ? `<table><thead><tr>
            <th>ID</th><th>来源</th><th>账期</th><th>凭证号</th><th>业务日期</th><th>金额</th></tr></thead>
            <tbody>${ev.entries.map((en) => `<tr>
              <td class="num">${en.id}</td><td>${SOURCE_LABEL[en.source]}</td><td>${en.period}</td>
              <td>${esc(en.doc_no || "—")}</td><td>${esc(en.entry_date || "—")}</td>
              <td class="num">${fmt(en.amount_minor)}</td></tr>`).join("")}</tbody></table>` : ""}
        </div>`).join("")}
      <div class="actions" style="margin-top:8px">
        ${c.status === "CANDIDATE" ? `
          <button class="small" data-act="confirm" data-id="${c.id}">确认原因</button>
          <button class="small" data-act="reject" data-id="${c.id}">排除</button>
          <button class="small primary" data-act="propose" data-id="${c.id}">生成修复建议</button>` : ""}
        ${c.status === "CONFIRMED" ? `<button class="small primary" data-act="propose" data-id="${c.id}">生成修复建议</button>` : ""}
      </div>
    </div>`).join("");
  el.querySelectorAll("button").forEach((b) => {
    b.onclick = () => guard(async () => {
      const id = b.dataset.id;
      if (b.dataset.act === "confirm") await api(`/api/causes/${id}/confirm`, { method: "POST" });
      if (b.dataset.act === "reject") await api(`/api/causes/${id}/reject`, { method: "POST" });
      if (b.dataset.act === "propose") {
        const p = await api(`/api/differences/${state.currentDiffId}/proposals`, {
          method: "POST", body: JSON.stringify({ cause_id: Number(id) }),
        });
        toast(`已生成${PROPOSAL_TYPE[p.proposal_type]}建议 #${p.id}${p.proposal_type === "CONTROLLED_CORRECTION" ? "（需提交审批）" : ""}`);
      }
      openDrawer(state.currentDiffId); loadTree();
    });
  });
}

function renderDiffProposals(proposals) {
  const el = $("#diffProposals");
  if (!proposals.length) return;
  el.innerHTML = proposals.map((p) => `
    <div class="cause">
      <div class="head">
        <span class="badge type-${p.proposal_type}">${PROPOSAL_TYPE[p.proposal_type]}</span>
        <b>#${p.id}</b><span class="muted">${esc(p.title)}</span>
        <span class="badge st-${p.status}">${PROPOSAL_STATUS[p.status]}</span>
      </div>
      <div class="summary muted">${esc(p.description || "")}</div>
      ${p.actions.map((a) => `<div class="muted" style="font-size:12px">
        ▸ ${a.action_kind === "BALANCE_VERSION" ? "余额新版本" : "红字分录"}：
        ${SOURCE_LABEL[a.source]} ${a.period} ${a.delta_minor >= 0 ? "+" : ""}${fmt(a.delta_minor)}
        — ${esc(a.rationale || "")}</div>`).join("")}
    </div>`).join("");
}

function renderVersionChain(versions) {
  const byKey = {};
  for (const v of versions) {
    (byKey[v.source] = byKey[v.source] || []).push(v);
  }
  $("#versionChain").innerHTML = Object.entries(byKey).map(([src, vs]) => `
    <div><b>${SOURCE_LABEL[src]}</b>
      ${vs.map((v) => `<div class="version-item ${v.version === 1 ? "original" : "fix"}">
        <span class="v-tag">v${v.version}${v.version === 1 ? "（原始）" : ""}</span>
        <span class="num">${fmt(v.amount_minor)}</span>
        <span class="v-tag">${esc(v.created_by)} · ${esc(v.created_at).slice(0, 19).replace("T", " ")}</span>
        ${v.proposal_id ? `<span class="v-tag">来自建议 #${v.proposal_id}</span>` : ""}
        ${v.note ? `<span class="v-tag">${esc(v.note)}</span>` : ""}
      </div>`).join("")}
    </div>`).join("");
}

/* ---------------- 修复进度 ---------------- */
async function renderProgress() {
  const proposals = await api("/api/proposals");
  const stages = ["DRAFT", "SUBMITTED", "APPROVED", "APPLIED", "VERIFIED", "REJECTED"];
  $("#pipeline").innerHTML = stages.map((s) => {
    const n = proposals.filter((p) => p.status === s).length;
    return `<div class="stage"><div class="n num">${n}</div><div class="t">${PROPOSAL_STATUS[s]}</div></div>`;
  }).join("");

  const rows = [];
  for (const p of proposals) {
    const detail = await api(`/api/proposals/${p.id}`);
    const errs = detail.validation_errors || [];
    const canSubmit = p.status === "DRAFT";
    const canApprove = p.status === "SUBMITTED" && p.created_by !== state.user;
    const canApply = p.proposal_type === "CONTROLLED_CORRECTION" ? p.status === "APPROVED" : ["DRAFT", "APPROVED"].includes(p.status);
    const canReject = !["APPLIED", "VERIFIED", "REJECTED"].includes(p.status);
    rows.push(`<tr>
      <td class="num">#${p.id}</td>
      <td><span class="badge type-${p.proposal_type}">${PROPOSAL_TYPE[p.proposal_type]}</span></td>
      <td>${p.period} · ${p.org} · ${p.currency} · ${p.account}<br><span class="muted num">差异 ${fmt(p.diff_minor)}</span></td>
      <td>${esc(p.title)}</td>
      <td>${p.actions.map((a) => `<div class="num" style="font-size:12px">${SOURCE_LABEL[a.source]} ${a.period} ${a.delta_minor >= 0 ? "+" : ""}${fmt(a.delta_minor)}</div>`).join("")}</td>
      <td><span class="badge st-${p.status}">${PROPOSAL_STATUS[p.status]}</span></td>
      <td style="font-size:12px">${esc(p.created_by)}<br>${p.approved_by ? "审批：" + esc(p.approved_by) : ""}</td>
      <td>${errs.length ? `<div class="err">${errs.map(esc).join("<br>")}</div>` : '<span class="muted">通过</span>'}</td>
      <td><div class="actions">
        ${canSubmit ? `<button class="small" data-act="submit" data-id="${p.id}">提交</button>` : ""}
        ${p.status === "SUBMITTED" ? `<button class="small primary" data-act="approve" data-id="${p.id}" ${canApprove ? "" : "disabled"}>审批</button>` : ""}
        ${canApply ? `<button class="small primary" data-act="apply" data-id="${p.id}">应用</button>` : ""}
        ${canReject ? `<button class="small danger" data-act="reject" data-id="${p.id}">驳回</button>` : ""}
      </div>${p.status === "SUBMITTED" && !canApprove ? '<div class="muted" style="font-size:11px">需非创建人审批</div>' : ""}</td>
    </tr>`);
  }
  $("#proposalTable tbody").innerHTML = rows.join("") || `<tr><td colspan="9" class="muted">暂无修复建议</td></tr>`;
  $("#proposalTable").querySelectorAll("button").forEach((b) => {
    b.onclick = () => guard(async () => {
      const id = b.dataset.id;
      if (b.dataset.act === "reject") {
        const reason = prompt("驳回原因：") || "";
        await api(`/api/proposals/${id}/reject`, { method: "POST", body: JSON.stringify({ reason }) });
      } else {
        await api(`/api/proposals/${id}/${b.dataset.act}`, { method: "POST" });
      }
      toast("操作完成");
      renderProgress(); loadTree();
      if (state.currentDiffId && $("#drawer").classList.contains("open")) openDrawer(state.currentDiffId);
    });
  });
}

/* ---------------- 审计记录 ---------------- */
async function loadAudit() {
  const type = $("#auditFilter").value;
  const logs = await api(`/api/audit${type ? "?entity_type=" + type : ""}`);
  $("#auditTable tbody").innerHTML = logs.map((l) => `<tr>
    <td class="num" style="white-space:nowrap">${esc(l.ts).replace("T", " ").slice(0, 19)}</td>
    <td>${esc(l.actor)}</td>
    <td>${ACTION_LABEL[l.action] || esc(l.action)}</td>
    <td>${esc(l.entity_type)}${l.entity_id ? " #" + l.entity_id : ""}</td>
    <td style="font-size:12px" class="muted">${esc(JSON.stringify(l.payload))}</td>
  </tr>`).join("");
}

init().catch((e) => toast(e.message, true));
