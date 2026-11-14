/* Relay dashboard: vanilla JS, hash router, no build step. */
const $ = (s, el = document) => el.querySelector(s);
const esc = (v) => String(v ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const pct = (x) => (x == null ? "—" : `${Math.round(x * 100)}%`);
const DEPTS = ["Billing", "Technical Support", "Account Security", "Customer Success", "Returns & Refunds", "General"];
const main = $("#main");
let pollTimer = null;
const charts = [];

async function api(path, opts = {}) {
  const res = await fetch(path, { headers: { "Content-Type": "application/json" }, ...opts,
    body: opts.body ? JSON.stringify(opts.body) : undefined });
  let data = null;
  try { data = await res.json(); } catch { /* non-JSON */ }
  if (!res.ok) {
    const d = data?.detail;
    const msg = typeof d === "string" ? d : d?.error || (Array.isArray(d) ? d.map((x) => `${x.loc?.slice(-1)[0]}: ${x.msg}`).join("; ") : null) || `HTTP ${res.status}`;
    throw new Error(msg);
  }
  return data;
}
function toast(msg, err = false) {
  const t = $("#toast"); t.textContent = msg; t.className = "toast" + (err ? " err" : "");
  clearTimeout(t._h); t._h = setTimeout(() => t.classList.add("hidden"), 4200);
}
const loading = (label = "Loading…") => `<div class="loading-block"><span class="spinner"></span>${esc(label)}</div>`;
const errorBox = (e) => `<div class="callout err">${esc(e.message || e)}</div>`;
function ago(iso) {
  if (!iso) return "";
  const s = (Date.now() - new Date(iso).getTime()) / 1000;
  if (s < 60) return "just now"; if (s < 3600) return `${Math.floor(s / 60)}m ago`;
  if (s < 86400) return `${Math.floor(s / 3600)}h ago`; return new Date(iso).toLocaleDateString();
}
const fmtTime = (iso) => (iso ? new Date(iso).toLocaleString([], { dateStyle: "medium", timeStyle: "short" }) : "");
const label = (s) => String(s ?? "").replace(/_/g, " ");
function clearPoll() { if (pollTimer) clearTimeout(pollTimer); pollTimer = null; }
function destroyCharts() { while (charts.length) charts.pop().destroy(); }

/* ---------- health ---------- */
async function refreshHealth() {
  try {
    const h = await api("/api/health");
    const sq = h.subq, g = h.generator;
    $("#h-subq").className = "dot " + (sq.status === "ok" ? "ok" : sq.status === "missing_key" ? "warn" : "err");
    $("#h-subq-t").textContent = sq.status === "ok" ? `${sq.latency_ms} ms` : sq.status === "missing_key" ? "key missing" : "error";
    $("#h-gen").className = "dot " + (g.ready ? "ok" : g.loading ? "warn" : "err");
    $("#h-gen-t").textContent = g.ready ? g.provider.replace(/_/g, " ") : g.loading ? "loading model…" : "unavailable";
    const msgs = [];
    if (sq.status !== "ok") msgs.push(sq.message || "SubQ unreachable");
    if (!g.ready && g.error) msgs.push(`Generator: ${g.error}`);
    $("#h-msg").textContent = msgs.join(" · ");
    if (!g.ready && !g.error) setTimeout(refreshHealth, 8000);
  } catch (e) { $("#h-msg").textContent = "Health check failed: " + e.message; }
}
async function refreshCounts() {
  try {
    const all = await api("/api/conversations");
    $("#nav-inbox-count").textContent = all.items.filter((c) => c.status !== "resolved").length || "";
    $("#nav-esc-count").textContent = all.items.filter((c) => c.status === "escalated").length || "";
  } catch { /* ignore */ }
}

/* ---------- chips ---------- */
function chips(c) {
  const out = [];
  if (c.category) out.push(`<span class="chip cat">${esc(label(c.category))}</span>`);
  if (c.priority_label) out.push(`<span class="chip ${esc(c.priority_label)}">${esc(c.priority_label)}</span>`);
  out.push(`<span class="chip ${esc(c.status)}">${esc(c.status)}</span>`);
  if (c.department) out.push(`<span class="chip">${esc(c.department)}</span>`);
  if (c.frustrated >= 0.5) out.push(`<span class="chip high">frustrated</span>`);
  if (c.draft_status) out.push(`<span class="chip ${esc(c.draft_status)}">draft: ${esc(label(c.draft_status))}</span>`);
  return out.join("");
}

/* ---------- inbox / escalations ---------- */
async function viewInbox(escalationsOnly = false) {
  const params = new URLSearchParams(location.hash.split("?")[1] || "");
  const status = escalationsOnly ? "escalated" : params.get("status") || "";
  const dept = params.get("department") || "";
  main.innerHTML = `
    <div class="page-head">
      <div><h1>${escalationsOnly ? "Escalations" : "Inbox"}</h1>
        <div class="sub">${escalationsOnly ? "Cases routed to the human queue, with the reason for each." : "Every message is classified, routed and drafted by the live pipeline."}</div></div>
      <div class="toolbar">
        <select id="f-dept"><option value="">All departments</option>${DEPTS.map((d) => `<option ${d === dept ? "selected" : ""}>${esc(d)}</option>`).join("")}</select>
        ${escalationsOnly ? "" : `<select id="f-status"><option value="">All statuses</option>${["open", "escalated", "resolved"].map((s) => `<option ${s === status ? "selected" : ""}>${s}</option>`).join("")}</select>`}
        ${escalationsOnly ? "" : `<button class="btn" id="seed">Load sample inbox</button>`}
      </div>
    </div>
    <div id="seed-status"></div>
    <div id="list">${loading()}</div>`;
  const apply = () => {
    const p = new URLSearchParams();
    const d = $("#f-dept").value; if (d) p.set("department", d);
    const s = $("#f-status")?.value; if (s) p.set("status", s);
    location.hash = `#/${escalationsOnly ? "escalations" : "inbox"}${p.toString() ? "?" + p : ""}`;
  };
  $("#f-dept").onchange = apply; if ($("#f-status")) $("#f-status").onchange = apply;
  if ($("#seed")) $("#seed").onclick = startSeed;
  await loadList(status, dept, escalationsOnly);
  pollSeed(status, dept, escalationsOnly);
}
async function loadList(status, dept, escalationsOnly) {
  const q = new URLSearchParams(); if (status) q.set("status", status); if (dept) q.set("department", dept);
  try {
    const r = await api("/api/conversations?" + q);
    const el = $("#list"); if (!el) return;
    if (!r.items.length) {
      el.innerHTML = `<div class="card empty">${escalationsOnly ? "No escalated conversations. 🎉" : "No conversations yet. Use <b>Load sample inbox</b> or <a href='#/simulate' style='color:var(--accent)'>simulate a message</a>."}</div>`;
      return;
    }
    el.innerHTML = `<div class="list-wrap"><div class="list">${r.items.map((c) => `
      <a class="row" href="#/c/${esc(c.id)}">
        <div style="min-width:0"><div class="who">${esc(c.customer_name)} <span class="muted small">· ${esc(c.channel)}</span></div>
          <div class="subj">${esc(c.subject)}</div>
          ${escalationsOnly && c.escalation_reasons?.length ? `<div class="small" style="color:var(--red);margin-top:6px">${esc(c.escalation_reasons.join(" · "))}</div>` : ""}
          <div class="chips">${chips(c)}</div></div>
        <div class="right"><span>${ago(c.updated_at)}</span>${c.assignee ? `<span class="chip">@${esc(c.assignee)}</span>` : ""}</div>
      </a>`).join("")}</div></div>`;
  } catch (e) { const el = $("#list"); if (el) el.innerHTML = errorBox(e); }
}
async function startSeed() {
  try { await api("/api/seed", { method: "POST" }); toast("Sending 12 sample messages through the live pipeline…"); viewInbox(); }
  catch (e) { toast(e.message, true); }
}
async function pollSeed(status, dept, escalationsOnly) {
  clearPoll();
  try {
    const s = await api("/api/seed");
    const box = $("#seed-status"); if (!box) return;
    const busy = s.seed.running || s.draft_queue > 0;
    box.innerHTML = s.seed.running
      ? `<div class="callout info" style="margin-bottom:14px"><span class="spinner"></span> Classifying sample messages ${s.seed.done}/${s.seed.total}… drafts queue behind them (${s.draft_queue} waiting).</div>`
      : s.draft_queue > 0 ? `<div class="callout info" style="margin-bottom:14px"><span class="spinner"></span> Generating AI drafts on CPU: ${s.draft_queue} waiting.</div>` : "";
    if (s.seed.errors?.length) box.innerHTML += `<div class="callout err" style="margin-bottom:14px">${esc(s.seed.errors.join("; "))}</div>`;
    if (busy) { await loadList(status, dept, escalationsOnly); refreshCounts(); pollTimer = setTimeout(() => pollSeed(status, dept, escalationsOnly), 3000); }
  } catch { /* ignore */ }
}

/* ---------- conversation detail ---------- */
function bars(probs, top) {
  const entries = Object.entries(probs || {}).sort((a, b) => b[1] - a[1]).slice(0, 5);
  if (!entries.length) return `<div class="muted small">No probabilities (fallback classifier)</div>`;
  return entries.map(([k, v]) => `<div class="bar ${k === top ? "top" : ""}"><span class="label" title="${esc(k)}">${esc(label(k))}</span>
    <span class="track"><span class="fill" style="width:${Math.max(1, v * 100)}%"></span></span><span class="val">${pct(v)}</span></div>`).join("");
}
function gauge(name, v, warnAt = 0.5) {
  return `<div class="bar ${v >= warnAt ? "top" : ""}"><span class="label">${esc(name)}</span>
    <span class="track"><span class="fill" style="width:${Math.max(1, v * 100)}%;background:${v >= warnAt ? "var(--red)" : "var(--accent)"}"></span></span><span class="val">${pct(v)}</span></div>`;
}
function analysisPanel(a) {
  if (!a) return `<div class="card">${loading("Analyzing…")}</div>`;
  const c = a.classification, x = a.extraction;
  const extracted = { order_reference: x.order_reference, product_name: x.product_name, amount_in_dispute: x.amount_in_dispute, emails: x.emails?.value, dates: x.dates?.value };
  return `<div class="card">
    <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:10px"><h2 style="margin:0">Analysis</h2>
      <span class="chip ${a.source === "fallback" ? "fallback" : "cat"}">${esc(a.source)} · ${esc(a.model)} · ${a.latency_ms} ms</span></div>
    ${a.error ? `<div class="callout warn" style="margin-bottom:10px">SubQ failed, keyword fallback used: ${esc(a.error)}</div>` : ""}
    <h3>Category · ${pct(c.category.confidence)} confidence</h3>${bars(c.category.probabilities, c.category.value)}
    <h3 style="margin-top:14px">Intent · ${pct(c.intent.confidence)}</h3>${bars(c.intent.probabilities, c.intent.value)}
    <h3 style="margin-top:14px">Priority · ${esc(c.priority.label)} (score ${c.priority.score.toFixed(2)} / 3)</h3>${bars(c.priority.probabilities, c.priority.label)}
    <h3 style="margin-top:14px">Signals</h3>
    ${gauge("Frustrated", c.frustrated)}${gauge("Time-sensitive", c.time_sensitive)}${gauge("Needs human", c.needs_human)}
    <h3 style="margin-top:14px">Requested action · ${pct(c.requested_action.confidence)}</h3>${bars(c.requested_action.probabilities, c.requested_action.value)}
    <h3 style="margin-top:14px">Extracted fields</h3><pre class="json">${esc(JSON.stringify(extracted, null, 2))}</pre>
    <details style="margin-top:8px"><summary>Regex candidates sent to SubQ</summary><pre class="json">${esc(JSON.stringify(x.candidates, null, 2))}</pre></details>
  </div>`;
}
function draftPanel(conv) {
  const d = conv.draft, st = conv.draft_status;
  if (st === "pending" || st === "generating") return `<div class="card"><h2>AI draft</h2>${loading(st === "pending" ? "Queued for the reply generator…" : "Generating draft on CPU (≈20–40 s)…")}</div>`;
  if (!d) return `<div class="card"><h2>Reply</h2><textarea id="draft" rows="7" placeholder="Write a reply…"></textarea>
    <div class="toolbar" style="margin-top:10px"><button class="btn primary" id="send">Send reply</button><button class="btn" id="regen">Generate AI draft</button></div></div>`;
  const g = d.guardrails;
  const guardRows = g?.scores ? [
    ["Promises refund/compensation", g.scores.promises_refund_or_compensation, false],
    ["States policy not in KB", g.scores.states_policy_not_in_kb, false],
    ["Tone professional", g.scores.tone_professional, true]].map(([n, v, good]) => {
      const ok = good ? v >= 0.5 : v < 0.5;
      return `<div class="guard"><span>${n}</span><span class="${ok ? "pass" : "fail"}">${ok ? "pass" : "fail"} · ${pct(v)}</span></div>`; }).join("") : "";
  return `<div class="card">
    <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:10px"><h2 style="margin:0">AI draft</h2>
      <span class="chip ${esc(d.status)}">${esc(label(d.status))}</span></div>
    ${d.status === "failed" ? `<div class="callout err" style="margin-bottom:10px">No draft: ${esc(d.error)}. Case flagged for a human.</div>` : ""}
    ${d.status === "needs_review" ? `<div class="callout warn" style="margin-bottom:10px">Guardrails failed twice (${esc((g?.failed || []).join(", "))}). Review before sending.</div>` : ""}
    <textarea id="draft" rows="9">${esc(d.text || "")}</textarea>
    <div class="toolbar" style="margin-top:10px">
      <button class="btn primary" id="send">Send</button><button class="btn" id="save">Save edit</button><button class="btn" id="regen">Regenerate</button></div>
    <div class="muted small" style="margin-top:10px">${esc(d.source)} · ${esc(d.model)} · ${d.latency_ms ?? "—"} ms total · ${d.attempts?.length || 0} attempt(s)</div>
    ${g ? `<h3 style="margin-top:14px">Guardrails ${g.source === "subq" ? "(SubQ)" : "(unavailable)"}</h3>${guardRows || `<div class="callout warn">${esc(g.error || "not checked")}</div>`}` : ""}
    ${d.attempts?.length > 1 ? `<details style="margin-top:8px"><summary>First attempt (rejected)</summary><pre class="json">${esc(d.attempts[0].text || d.attempts[0].error)}</pre></details>` : ""}
  </div>`;
}
async function viewConversation(id) {
  main.innerHTML = loading();
  let r;
  try { r = await api(`/api/conversations/${encodeURIComponent(id)}`); } catch (e) { main.innerHTML = errorBox(e); return; }
  const c = r.conversation;
  const lastAnalysis = [...r.messages].reverse().find((m) => m.role === "customer")?.analysis;
  const routing = c.routing;
  main.innerHTML = `
    <div class="page-head"><div><a href="#/inbox" class="muted small">← Inbox</a><h1 style="margin-top:6px">${esc(c.customer_name)}</h1>
      <div class="sub">${esc(c.email || "no email")} · ${esc(c.channel)} · opened ${fmtTime(c.created_at)}</div><div class="chips" style="display:flex;gap:6px;margin-top:10px;flex-wrap:wrap">${chips(c)}</div></div>
      <div class="toolbar">
        <input type="text" id="assignee" placeholder="Assign to…" value="${esc(c.assignee || "")}" style="width:140px"><button class="btn" id="assign">Assign</button>
        <button class="btn danger" id="escalate">Escalate</button><button class="btn" id="resolve" ${c.status === "resolved" ? "disabled" : ""}>Resolve</button></div></div>
    <div class="detail">
      <div class="stack">
        <div class="thread">${r.messages.map((m) => `<div class="msg ${m.role}"><div class="meta"><span><b>${esc(m.role === "customer" ? m.author || "Customer" : m.author || "Agent")}</b> · ${esc(m.role)}</span><span>${fmtTime(m.created_at)}</span></div><div class="body">${esc(m.text)}</div></div>`).join("")}</div>
        <div id="draft-slot">${draftPanel(c)}</div>
        <div class="card"><h2>Customer follow-up</h2><div class="muted small" style="margin-bottom:8px">Simulate the customer replying in this conversation.</div>
          <textarea id="followup" rows="3" placeholder="Customer's next message…"></textarea><div class="toolbar" style="margin-top:8px"><button class="btn" id="followup-send">Add customer message</button></div></div>
      </div>
      <div class="stack">
        <div class="card"><h2>Routing</h2><div class="kv">
          <span class="k">Department</span><span>${esc(c.department || "—")}</span>
          <span class="k">Status</span><span>${esc(c.status)}</span>
          <span class="k">Assignee</span><span>${esc(c.assignee || "unassigned")}</span>
          <span class="k">First response</span><span>${c.first_response_at ? fmtTime(c.first_response_at) : "not yet"}</span></div>
          ${c.escalation_reasons?.length ? `<h3 style="margin-top:14px">Escalation reasons</h3><ul class="reasons">${c.escalation_reasons.map((x) => `<li>${esc(x)}</li>`).join("")}</ul>`
            : `<div class="callout ok" style="margin-top:12px">No escalation triggers. Safe for an agent to send from the draft.</div>`}
          ${routing && !routing.escalate && c.escalated ? `<div class="muted small" style="margin-top:8px">Latest message alone wouldn't escalate; earlier triggers still apply.</div>` : ""}
        </div>
        ${analysisPanel(lastAnalysis)}
      </div>
    </div>`;
  const act = async (btn, fn) => { btn.disabled = true; const t = btn.innerHTML; btn.innerHTML = `<span class="spinner"></span> ${t}`;
    try { await fn(); } catch (e) { toast(e.message, true); btn.disabled = false; btn.innerHTML = t; } };
  $("#assign").onclick = (e) => act(e.target, async () => { await api(`/api/conversations/${id}/assign`, { method: "POST", body: { assignee: $("#assignee").value || null } }); toast("Assigned"); viewConversation(id); });
  $("#escalate").onclick = (e) => { const reason = prompt("Reason for escalation", "Escalated manually by agent"); if (reason === null) return;
    act(e.target, async () => { await api(`/api/conversations/${id}/escalate`, { method: "POST", body: { reason } }); toast("Escalated"); viewConversation(id); refreshCounts(); }); };
  $("#resolve").onclick = (e) => act(e.target, async () => { await api(`/api/conversations/${id}/resolve`, { method: "POST" }); toast("Resolved"); viewConversation(id); refreshCounts(); });
  const send = $("#send"); if (send) send.onclick = (e) => { const text = $("#draft").value.trim(); if (!text) return toast("Reply is empty", true);
    act(e.target, async () => { await api(`/api/conversations/${id}/reply`, { method: "POST", body: { text, send: true } }); toast("Reply sent"); viewConversation(id); }); };
  const save = $("#save"); if (save) save.onclick = (e) => act(e.target, async () => { await api(`/api/conversations/${id}/reply`, { method: "POST", body: { text: $("#draft").value, send: false } }); toast("Draft saved"); e.target.disabled = false; e.target.textContent = "Save edit"; });
  const regen = $("#regen"); if (regen) regen.onclick = (e) => act(e.target, async () => { await api(`/api/conversations/${id}/regenerate?wait=false`, { method: "POST" }); viewConversation(id); });
  $("#followup-send").onclick = (e) => { const text = $("#followup").value.trim(); if (!text) return;
    act(e.target, async () => { await api(`/api/messages?wait_draft=false`, { method: "POST", body: { customer_name: c.customer_name, email: c.email, channel: c.channel, text, conversation_id: id } }); viewConversation(id); refreshCounts(); }); };
  if (c.draft_status === "pending" || c.draft_status === "generating") {
    clearPoll();
    const poll = async () => { try { const n = await api(`/api/conversations/${id}`); if (!location.hash.startsWith(`#/c/${id}`)) return;
      if (n.conversation.draft_status !== "pending" && n.conversation.draft_status !== "generating") return viewConversation(id);
      $("#draft-slot").innerHTML = draftPanel(n.conversation); } catch { /* keep polling */ } pollTimer = setTimeout(poll, 2500); };
    pollTimer = setTimeout(poll, 2500);
  }
}

/* ---------- simulate ---------- */
function viewSimulate() {
  main.innerHTML = `<div class="page-head"><div><h1>Simulate incoming message</h1><div class="sub">Runs the real pipeline: one SubQ call for classification + extraction, routing rules, then a grounded draft and a SubQ guardrail check.</div></div></div>
    <div class="card"><form class="form" id="sim">
      <div class="form-row"><label>Customer name<input type="text" name="customer_name" required value="Ngozi Eze"></label>
        <label>Email<input type="email" name="email" value="ngozi.eze@gmail.com"></label></div>
      <label>Channel<select name="channel"><option>email</option><option>chat</option><option>web_form</option><option>social</option><option>phone_transcript</option></select></label>
      <label>Message<textarea name="text" rows="6" required>Hi, my order ORD-60231 says delivered but I never got the Lumen Hub. I paid ₦185,000. Please help, I need it before my parents arrive on Saturday.</textarea></label>
      <div class="toolbar"><button class="btn primary" type="submit">Send through pipeline</button><span class="muted small">Draft is generated in the background; you'll land on the conversation.</span></div>
    </form><div id="sim-out" style="margin-top:14px"></div></div>`;
  $("#sim").onsubmit = async (ev) => {
    ev.preventDefault();
    const f = Object.fromEntries(new FormData(ev.target));
    const btn = ev.target.querySelector("button[type=submit]"); btn.disabled = true;
    $("#sim-out").innerHTML = loading("Classifying with SubQ…");
    try {
      const r = await api("/api/messages?wait_draft=false", { method: "POST", body: f });
      toast(`Classified as ${label(r.analysis.classification.category.value)} in ${r.analysis.latency_ms} ms → ${r.routing.department}${r.escalated ? " (escalated)" : ""}`);
      refreshCounts(); location.hash = `#/c/${r.conversation_id}`;
    } catch (e) { $("#sim-out").innerHTML = errorBox(e); btn.disabled = false; }
  };
}

/* ---------- analytics ---------- */
async function viewAnalytics() {
  main.innerHTML = loading("Loading analytics…");
  let a; try { a = await api("/api/analytics"); } catch (e) { main.innerHTML = errorBox(e); return; }
  const fr = a.avg_first_response_seconds;
  const frt = fr == null ? "—" : fr < 60 ? `${Math.round(fr)}s` : fr < 3600 ? `${Math.round(fr / 60)}m` : `${(fr / 3600).toFixed(1)}h`;
  main.innerHTML = `<div class="page-head"><div><h1>Analytics</h1><div class="sub">${a.totals.conversations} conversations · ${a.totals.messages} customer messages</div></div>
    <button class="btn" onclick="viewAnalytics()">Refresh</button></div>
    <div class="grid4" style="margin-bottom:14px">
      ${[["Escalation rate", pct(a.escalation_rate)], ["Resolution rate", pct(a.resolution_rate)], ["Avg first response", frt], ["Frustration rate", pct(a.frustration_rate)]]
        .map(([l, v]) => `<div class="card stat"><div class="l">${l}</div><div class="v">${v}</div></div>`).join("")}
    </div>
    <div class="grid4" style="margin-bottom:14px">
      ${[["Open", a.totals.open], ["Escalated", a.totals.escalated], ["Resolved", a.totals.resolved],
        ["Avg SubQ / draft", `${a.avg_classification_latency_ms ?? "—"} ms / ${a.avg_draft_latency_ms ? (a.avg_draft_latency_ms / 1000).toFixed(1) + " s" : "—"}`]]
        .map(([l, v]) => `<div class="card stat"><div class="l">${l}</div><div class="v">${v}</div></div>`).join("")}
    </div>
    <div class="grid2">
      <div class="card"><h2>Volume (last 24h, hourly)</h2><div class="chart-box"><canvas id="ch-hour"></canvas></div></div>
      <div class="card"><h2>Volume (last 14 days)</h2><div class="chart-box"><canvas id="ch-day"></canvas></div></div>
      <div class="card"><h2>Category distribution</h2><div class="chart-box"><canvas id="ch-cat"></canvas></div></div>
      <div class="card"><h2>Priority levels</h2><div class="chart-box"><canvas id="ch-pri"></canvas></div></div>
    </div>`;
  const draw = () => {
    if (!window.Chart) return setTimeout(draw, 200);
    destroyCharts();
    Chart.defaults.font.family = "Inter, system-ui, sans-serif"; Chart.defaults.color = "#71717a";
    const accent = "#5b5bd6", grid = { color: "#f1f1f3" };
    const opts = { responsive: true, maintainAspectRatio: false, plugins: { legend: { display: false } }, scales: { x: { grid: { display: false } }, y: { beginAtZero: true, grid, ticks: { precision: 0 } } } };
    charts.push(new Chart($("#ch-hour"), { type: "bar", data: { labels: a.volume_by_hour.map((h) => new Date(h.hour).toLocaleTimeString([], { hour: "2-digit" })), datasets: [{ data: a.volume_by_hour.map((h) => h.count), backgroundColor: accent, borderRadius: 4 }] }, options: opts }));
    charts.push(new Chart($("#ch-day"), { type: "line", data: { labels: a.volume_by_day.map((d) => d.date.slice(5)), datasets: [{ data: a.volume_by_day.map((d) => d.count), borderColor: accent, backgroundColor: "rgba(91,91,214,.08)", fill: true, tension: .3, pointRadius: 2 }] }, options: opts }));
    const cats = Object.entries(a.category_distribution).sort((x, y) => y[1] - x[1]);
    const pal = ["#5b5bd6", "#8b8bf0", "#22a06b", "#e2a03f", "#dc4c4c", "#3b82f6", "#a1a1aa", "#c084fc"];
    charts.push(new Chart($("#ch-cat"), { type: "doughnut", data: { labels: cats.map((c) => label(c[0])), datasets: [{ data: cats.map((c) => c[1]), backgroundColor: pal, borderWidth: 0 }] }, options: { responsive: true, maintainAspectRatio: false, cutout: "62%", plugins: { legend: { position: "right" } } } }));
    const pr = a.priority_levels;
    charts.push(new Chart($("#ch-pri"), { type: "bar", data: { labels: Object.keys(pr), datasets: [{ data: Object.values(pr), backgroundColor: ["#a1a1aa", "#3b82f6", "#e2a03f", "#dc4c4c"], borderRadius: 4 }] }, options: opts }));
  };
  draw();
}

/* ---------- settings ---------- */
async function viewSettings() {
  main.innerHTML = loading();
  let s; try { s = (await api("/api/settings")).settings; } catch (e) { main.innerHTML = errorBox(e); return; }
  const num = (k, l, hint, step = "0.01", min = 0, max = 1) => `<label>${l}<input type="number" name="${k}" step="${step}" min="${min}" max="${max}" value="${esc(s[k])}"><span class="hint">${hint}</span></label>`;
  main.innerHTML = `<div class="page-head"><div><h1>Settings</h1><div class="sub">The knowledge base grounds every draft and every guardrail check. Thresholds drive routing in code.</div></div></div>
    <div class="card"><form class="form" id="set" style="max-width:860px">
      <label>Company name<input type="text" name="company_name" value="${esc(s.company_name)}"></label>
      <label>Policy / knowledge base<textarea name="kb_text" rows="18">${esc(s.kb_text)}</textarea><span class="hint">Drafts may only state what is written here; guardrails flag anything else.</span></label>
      <div class="form-row">${num("needs_human_threshold", "Needs-human threshold", "Escalate when P(needs human) ≥ this")}${num("category_confidence_min", "Min category confidence", "Escalate when category confidence is below this")}</div>
      <div class="form-row">${num("frustrated_threshold", "Frustration threshold", "P(frustrated) that counts as frustrated")}${num("guardrail_threshold", "Guardrail threshold", "Draft fails a check at this probability")}</div>
      <div class="form-row">${num("high_priority_score", "High priority score", "0 low · 1 medium · 2 high · 3 urgent", "0.1", 0, 3)}${num("urgent_priority_score", "Urgent priority score", "Escalate at or above this score", "0.1", 0, 3)}</div>
      <div class="form-row">${num("max_unresolved_messages", "Repeated-contact limit", "Escalate on the Nth unresolved customer message", "1", 1, 50)}<span></span></div>
      <div class="toolbar"><button class="btn primary" type="submit">Save settings</button></div>
    </form></div>`;
  $("#set").onsubmit = async (ev) => {
    ev.preventDefault();
    const f = Object.fromEntries(new FormData(ev.target));
    for (const k of Object.keys(f)) if (!["company_name", "kb_text"].includes(k)) f[k] = Number(f[k]);
    try { await api("/api/settings", { method: "PUT", body: f }); toast("Settings saved"); } catch (e) { toast(e.message, true); }
  };
}

/* ---------- router ---------- */
function route() {
  clearPoll(); destroyCharts();
  const h = location.hash || "#/inbox";
  const view = h.slice(2).split(/[/?]/)[0];
  document.querySelectorAll("nav a").forEach((a) => a.classList.toggle("active", a.dataset.view === view || (view === "c" && a.dataset.view === "inbox")));
  if (view === "c") return viewConversation(h.slice(4).split("?")[0]);
  if (view === "escalations") return viewInbox(true);
  if (view === "analytics") return viewAnalytics();
  if (view === "simulate") return viewSimulate();
  if (view === "settings") return viewSettings();
  return viewInbox(false);
}
window.addEventListener("hashchange", route);
route(); refreshHealth(); refreshCounts();
setInterval(refreshCounts, 20000);
