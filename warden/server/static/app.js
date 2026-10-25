/* WARDEN dashboard — vanilla JS, no build step, no framework.
   A small hash-router, an SSE live feed, and one render function per page. */
"use strict";

const $ = (s, r = document) => r.querySelector(s);
const el = (tag, attrs = {}, kids = []) => {
  const n = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (k === "class") n.className = v;
    else if (k === "html") n.innerHTML = v;
    else if (k.startsWith("on")) n.addEventListener(k.slice(2), v);
    else if (v !== null && v !== undefined) n.setAttribute(k, v);
  }
  for (const kid of [].concat(kids)) if (kid !== null && kid !== undefined) n.append(kid.nodeType ? kid : document.createTextNode(kid));
  return n;
};
const api = async (path, opts) => {
  const r = await fetch(path, { headers: { "Content-Type": "application/json" }, ...opts });
  const t = await r.text();
  let j; try { j = t ? JSON.parse(t) : {}; } catch { j = { raw: t }; }
  if (!r.ok && !opts?.soft) throw Object.assign(new Error(j.detail || r.statusText), { status: r.status, body: j });
  return j;
};
const fmtTime = (ts) => new Date(ts * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" });
const esc = (s) => String(s ?? "").replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
const title = (s) => String(s).replace(/_/g, " ").replace(/\b\w/g, (c) => c.toUpperCase());

const ICONS = {
  overview: '<path d="M3 13h8V3H3zm0 8h8v-6H3zm10 0h8V11h-8zm0-18v6h8V3z"/>',
  feed: '<path d="M4 6h16M4 12h16M4 18h10"/>',
  agents: '<circle cx="9" cy="8" r="3"/><path d="M3 20c0-3.3 2.7-6 6-6s6 2.7 6 6"/><circle cx="17" cy="9" r="2"/><path d="M15.5 14.5c2.5.5 4.5 2.7 4.5 5.5"/>',
  approvals: '<path d="M9 11l3 3L22 4"/><path d="M21 12v7a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h11"/>',
  incidents: '<path d="M12 2 1 21h22z"/><path d="M12 9v5M12 17h.01"/>',
  policy: '<path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><path d="M14 2v6h6"/>',
  redteam: '<path d="M14.5 3.5 21 10 10 21l-6.5-6.5zM3 21l3.5-3.5"/><circle cx="17" cy="7" r="1.5"/>',
  trust: '<path d="M12 2 4 5v6c0 5 3.4 9.4 8 11 4.6-1.6 8-6 8-11V5z"/>',
  sandbox: '<rect x="3" y="3" width="18" height="18" rx="2"/><path d="M3 9h18M9 21V9"/>',
  compliance: '<path d="M9 11l3 3L22 4"/><path d="M12 3v4M3 12h4m10 0h4"/>',
  mcp: '<path d="M4 17V7l8-4 8 4v10l-8 4z"/><path d="M4 7l8 4 8-4M12 11v10"/>',
  bolt: '<path d="M13 2 3 14h7l-1 8 10-12h-7z"/>',
  play: '<path d="M6 4l14 8-14 8z"/>',
  skull: '<circle cx="9" cy="10" r="1.3"/><circle cx="15" cy="10" r="1.3"/><path d="M12 2a9 9 0 0 0-5 16v3h10v-3a9 9 0 0 0-5-16z"/>',
  shield: '<path d="M12 2 4 5v6c0 5 3.4 9.4 8 11 4.6-1.6 8-6 8-11V5z"/>',
};
const icon = (name, cls = "") => `<svg class="${cls}" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round">${ICONS[name] || ""}</svg>`;

const PAGES = [
  ["overview", "Overview", "overview"],
  ["feed", "Live Feed", "feed"],
  ["agents", "Agents", "agents"],
  ["approvals", "Approvals", "approvals"],
  ["incidents", "Incidents", "incidents"],
  ["SEP"],
  ["policy", "Policy Studio", "policy"],
  ["redteam", "Red Team", "redteam"],
  ["sandbox", "Blast Radius", "sandbox"],
  ["trust", "Trust Score", "trust"],
  ["compliance", "Compliance", "compliance"],
  ["connect", "Connect", "mcp"],
];

const state = { events: [], pendingApprovals: 0, openIncidents: 0 };

/* ---------------- toasts ---------------- */
let _muteToasts = 0;
function toast(title, body, kind = "") {
  if (_muteToasts > Date.now()) return;
  const host = $("#toasts");
  while (host.children.length >= 4) host.firstChild.remove();
  const t = el("div", { class: `toast ${kind}` }, [el("div", { class: "t" }, title), body ? el("div", { class: "b" }, body) : null]);
  host.append(t);
  setTimeout(() => { t.style.opacity = "0"; t.style.transform = "translateX(20px)"; setTimeout(() => t.remove(), 300); }, 4800);
}
const muteToastsFor = (ms) => { _muteToasts = Date.now() + ms; };

/* ---------------- nav + router ---------------- */
function buildNav() {
  const nav = $("#nav");
  nav.innerHTML = "";
  for (const p of PAGES) {
    if (p[0] === "SEP") { nav.append(el("div", { class: "sep" })); continue; }
    const [id, label, ic] = p;
    const b = el("button", { "data-page": id, onclick: () => (location.hash = id) },
      [el("span", { html: icon(ic) }), el("span", {}, label)]);
    if (id === "approvals") b.append(el("span", { class: "pill require_approval", id: "nav-apr", style: "margin-left:auto;display:none" }, "0"));
    if (id === "incidents") b.append(el("span", { class: "pill deny", id: "nav-inc", style: "margin-left:auto;display:none" }, "0"));
    nav.append(b);
  }
}
function setActive(page) {
  document.querySelectorAll(".nav button").forEach((b) => b.classList.toggle("active", b.dataset.page === page));
}

const ROUTES = {};
async function route() {
  const page = (location.hash.slice(1) || "overview").split("?")[0];
  setActive(page);
  const view = $("#view");
  view.innerHTML = "";
  const fn = ROUTES[page] || ROUTES.overview;
  try { await fn(view); } catch (e) { view.append(el("div", { class: "empty" }, "Error: " + e.message)); }
}

/* ---------------- reusable bits ---------------- */
const pill = (v) => el("span", { class: `pill ${v}` }, [el("span", { class: "pd" }), title(v)]);
const pageHead = (h, p, tools = []) => el("div", { class: "page-head" }, [
  el("div", {}, [el("h1", {}, h), p ? el("p", {}, p) : null]),
  tools.length ? el("div", { class: "toolbar" }, tools) : null,
]);
function card(titleText, bodyNodes, headExtra = null, flush = false) {
  return el("div", { class: "card" }, [
    titleText ? el("div", { class: "hd" }, [el("h3", {}, titleText), headExtra]) : null,
    el("div", { class: "bd" + (flush ? " flush" : "") }, [].concat(bodyNodes)),
  ]);
}
function statCard(k, v, s, ic, kind = "") {
  return el("div", { class: "card" }, el("div", { class: "stat" }, [
    el("div", { class: "k" }, [el("span", { html: icon(ic, "ic") }), k]),
    el("div", { class: "v", style: kind ? `color:var(--${kind})` : "" }, String(v)),
    s ? el("div", { class: "s" }, s) : null,
  ]));
}
function btn(label, ic, cls, onclick) {
  return el("button", { class: `btn ${cls || ""}`, onclick }, [ic ? el("span", { html: icon(ic) }) : null, label]);
}
async function withBusy(b, fn) {
  const old = b.innerHTML; b.disabled = true; b.innerHTML = '<span class="loading"></span> working…';
  try { return await fn(); } finally { b.disabled = false; b.innerHTML = old; }
}

/* ===================== OVERVIEW ===================== */
ROUTES.overview = async (view) => {
  view.append(pageHead("Overview", "Everything your AI agents did, and everything WARDEN stopped — in real time.",
    [btn("Run all scenarios", "play", "primary", (e) => withBusy(e.currentTarget, async () => {
      muteToastsFor(4000);
      const r = await api("/v1/scenarios/run-all", { method: "POST" });
      toast("Scenarios complete", `${r.passed}/${r.total} passed`, r.passed === r.total ? "" : "deny");
      route();
    }))]));
  const s = await api("/v1/stats");
  const v = s.verdicts, total = s.calls || 0;
  const blocked = v.deny + v.quarantine + v.require_approval;
  view.append(el("div", { class: "grid g-4", style: "margin-bottom:16px" }, [
    statCard("Governed calls", total.toLocaleString(), "since start", "bolt"),
    statCard("Allowed", v.allow, total ? Math.round(v.allow / total * 100) + "% of calls" : "", "trust", "allow"),
    statCard("Blocked / held", blocked, `${v.deny} denied · ${v.require_approval} held`, "shield", "deny"),
    statCard("Quarantined agents", s.agents_quarantined + s.agents_killed, `${s.agents_total} agents total`, "skull", "quarantine"),
  ]));
  // timeline + detectors
  const tl = s.timeline.slice(-48);
  const maxT = Math.max(1, ...tl.map((m) => m.allow + m.warn + m.require_approval + m.deny + m.quarantine));
  const colors = { allow: "--allow", warn: "--warn", require_approval: "--approval", deny: "--deny", quarantine: "--quar" };
  const spark = el("div", { class: "spark" }, tl.length ? tl.map((m) => {
    const col = el("div", { class: "col" });
    for (const k of ["allow", "warn", "require_approval", "deny", "quarantine"]) {
      const h = (m[k] / maxT) * 82;
      if (h > 0) col.append(el("i", { style: `height:${Math.max(2, h)}px;background:var(${colors[k]})` }));
    }
    col.title = `${m.allow} allow · ${m.deny} deny · ${m.quarantine} quar`;
    return col;
  }) : el("div", { class: "dim", style: "align-self:center;margin:auto" }, "no activity yet — run a scenario"));

  const det = Object.entries(s.detectors);
  const maxD = Math.max(1, ...det.map(([, n]) => n));
  const detBody = det.length ? det.map(([d, n]) => el("div", { class: "meter" }, [
    el("div", { class: "lbl" }, title(d)), el("div", { class: "bar" }, el("span", { style: `width:${n / maxD * 100}%` })),
    el("div", { class: "num" }, n),
  ])) : el("div", { class: "empty" }, "No detector activity yet.");

  view.append(el("div", { class: "grid g-2" }, [
    card("Decisions per minute", spark, el("div", { class: "tags" }, Object.keys(colors).map((k) =>
      el("span", { class: "pill " + k, style: "font-size:10.5px" }, title(k))))),
    card("What's doing the blocking", detBody),
  ]));

  view.append(el("div", { style: "height:16px" }));
  view.append(card("Latest activity", recentTable(state.events.slice(0, 8)), btn("Open live feed", "feed", "ghost sm", () => location.hash = "feed"), true));
  view.append(el("div", { style: "height:16px" }));
  view.append(el("div", { class: "grid g-3" }, [
    statCard("Gateway p50", (s.latency_ms.p50 ?? 0).toFixed(2) + " ms", "median decision time", "bolt"),
    statCard("Gateway p99", (s.latency_ms.p99 ?? 0).toFixed(2) + " ms", "99th percentile", "bolt"),
    statCard("Audit records", s.ledger_records.toLocaleString(), "hash-chained & verifiable", "compliance"),
  ]));
};

function recentTable(events) {
  if (!events.length) return el("div", { class: "empty" }, [el("div", { html: icon("feed") }), el("div", {}, "Waiting for agent activity…")]);
  const tb = el("tbody");
  for (const e of events) tb.append(feedRow(e, false));
  return el("table", {}, [el("thead", {}, el("tr", {}, ["Time", "Agent", "Tool", "Verdict", "Why"].map((h) => el("th", {}, h)))), tb]);
}
function feedRow(e, isNew) {
  return el("tr", { class: isNew ? "row-new" : "" }, [
    el("td", { class: "dim mono", style: "white-space:nowrap" }, fmtTime(e.ts)),
    el("td", {}, el("span", { class: "mono" }, e.agent_id)),
    el("td", {}, el("span", { class: "tool" }, e.tool)),
    el("td", {}, pill(e.verdict)),
    el("td", {}, el("div", { class: "expl" }, e.explanation || "—")),
  ]);
}

/* ===================== LIVE FEED ===================== */
ROUTES.feed = async (view) => {
  let filter = "all";
  const head = pageHead("Live Feed", "Every tool call, streamed from the gateway as it happens.", []);
  view.append(head);
  const filterBar = el("div", { class: "toolbar", style: "margin-bottom:14px" },
    ["all", "allow", "warn", "require_approval", "deny", "quarantine"].map((f) =>
      btn(title(f), null, "sm" + (f === "all" ? " primary" : ""), (ev) => {
        filter = f; document.querySelectorAll("#feedfilters .btn").forEach((b) => b.classList.remove("primary"));
        ev.currentTarget.classList.add("primary"); draw();
      })));
  filterBar.id = "feedfilters";
  view.append(filterBar);
  const wrap = el("div", { class: "card feed" });
  view.append(wrap);
  const draw = () => {
    const rows = state.events.filter((e) => filter === "all" || e.verdict === filter);
    wrap.innerHTML = "";
    wrap.append(rows.length ? (() => {
      const tb = el("tbody");
      rows.slice(0, 200).forEach((e) => tb.append(feedRow(e, false)));
      return el("table", {}, [el("thead", {}, el("tr", {}, ["Time", "Agent", "Tool", "Verdict", "Why"].map((h) => el("th", {}, h)))), tb]);
    })() : el("div", { class: "empty" }, "No matching calls yet."));
  };
  draw();
  view._onEvent = draw;
};

/* ===================== AGENTS ===================== */
ROUTES.agents = async (view) => {
  view.append(pageHead("Agents", "Every agent has an identity, an owner, scoped capability tokens, and a kill switch."));
  const { agents } = await api("/v1/agents");
  const tb = el("tbody");
  for (const a of agents) {
    const actions = el("div", { class: "toolbar" }, [
      btn("Blast radius", null, "sm ghost", () => location.hash = "sandbox?a=" + a.id),
      a.status === "active"
        ? btn("Kill", null, "sm danger", async (e) => {
            if (!confirm(`Kill ${a.id}? This revokes every token for it and its sub-agents.`)) return;
            await withBusy(e.currentTarget, () => api(`/v1/agents/${a.id}/kill`, { method: "POST", body: JSON.stringify({ actor: "dashboard" }) }));
            toast("Kill switch pulled", a.id + " and its sub-agents revoked", "deny"); route();
          })
        : (a.status === "quarantined"
            ? btn("Restore", null, "sm", async (e) => { await withBusy(e.currentTarget, () => api(`/v1/agents/${a.id}/restore`, { method: "POST", body: "{}", soft: true })); toast("Restored", a.id); route(); })
            : el("span", { class: "dim" }, "—")),
    ]);
    const c = a.counts || {};
    tb.append(el("tr", {}, [
      el("td", {}, [el("div", { style: "font-weight:600" }, a.name), el("div", { class: "dim mono", style: "font-size:11.5px" }, a.id + (a.parent ? " ← " + a.parent : ""))]),
      el("td", {}, el("span", { class: "pill info" }, a.role)),
      el("td", { class: "dim" }, a.owner),
      el("td", {}, pill(a.status)),
      el("td", { class: "mono", style: "font-size:12px" }, `${c.calls || 0} calls · ${(c.deny || 0) + (c.quarantine || 0)} blocked`),
      el("td", {}, actions),
    ]));
  }
  view.append(el("div", { class: "card", flush: true }, el("table", {}, [
    el("thead", {}, el("tr", {}, ["Agent", "Role", "Owner", "Status", "Activity", ""].map((h) => el("th", {}, h)))), tb])));
};

/* ===================== APPROVALS ===================== */
ROUTES.approvals = async (view) => {
  view.append(pageHead("Approvals", "High-risk actions pause here for a human. Risky ones need two different people, and no agent can approve itself."));
  const { approvals } = await api("/v1/approvals?all=true");
  if (!approvals.length) { view.append(el("div", { class: "card" }, el("div", { class: "empty" }, [el("div", { html: icon("approvals") }), el("div", {}, "No approval requests yet. Try the “$2,400 refund” scenario.")]))); return; }
  const grid = el("div", { class: "grid g-2" });
  for (const a of approvals) {
    const body = [];
    body.push(el("div", { class: "mono", style: "font-size:12.5px;margin-bottom:8px" }, `${a.call.agent_id} → ${a.call.tool}(${esc(JSON.stringify(a.call.args)).slice(0, 120)})`));
    body.push(el("div", { class: "callout", style: "margin-bottom:12px" }, a.reason));
    body.push(el("div", { class: "muted", style: "font-size:12.5px;margin-bottom:10px" }, `Needs ${a.required} approval(s) · ${a.approvals.length} so far` + (a.denials.length ? ` · ${a.denials.length} denial(s)` : "")));
    if (a.approvals.length) body.push(el("div", { class: "tags", style: "margin-bottom:10px" }, a.approvals.map((v) => el("span", { class: "pill allow" }, "✓ " + v.by))));
    if (a.status === "pending") {
      const who = el("input", { placeholder: "your name / email", style: "margin-bottom:8px" });
      body.push(who);
      body.push(el("div", { class: "toolbar" }, [
        btn(`Approve (${a.required - a.approvals.length} left)`, "approvals", "primary sm", async (e) => {
          if (!who.value.trim()) { who.focus(); return toast("Name required", "Enter who is approving", "deny"); }
          const r = await api(`/v1/approvals/${a.id}/decide`, { method: "POST", soft: true, body: JSON.stringify({ approver: who.value.trim(), approve: true }) });
          if (r.detail) return toast("Not allowed", r.detail, "deny");
          toast("Vote recorded", r.status === "approved" ? "Approved — action executed" : "1 of required", "approval"); route();
        }),
        btn("Deny", null, "sm danger", async () => {
          if (!who.value.trim()) { who.focus(); return toast("Name required", "", "deny"); }
          await api(`/v1/approvals/${a.id}/decide`, { method: "POST", soft: true, body: JSON.stringify({ approver: who.value.trim(), approve: false }) });
          toast("Denied", "", "deny"); route();
        }),
      ]));
    } else {
      body.push(pill(a.status === "approved" ? "allow" : a.status === "denied" ? "deny" : "info"));
      if (a.result_status) body.push(el("span", { class: "dim", style: "margin-left:8px" }, "→ " + a.result_status));
    }
    grid.append(card(`${title(a.call.tool)} · ${a.status}`, body, pill(a.status === "pending" ? "require_approval" : a.status === "approved" ? "allow" : "deny")));
  }
  view.append(grid);
};

/* ===================== INCIDENTS ===================== */
ROUTES.incidents = async (view) => {
  view.append(pageHead("Incidents", "Opened automatically when an agent is quarantined or a critical control fires."));
  const { incidents } = await api("/v1/incidents");
  if (!incidents.length) { view.append(el("div", { class: "card" }, el("div", { class: "empty" }, [el("div", { html: icon("incidents") }), el("div", {}, "No incidents. Run the exfiltration or laundering scenario to trigger one.")]))); return; }
  for (const i of incidents) {
    const body = [
      el("div", { style: "margin-bottom:10px" }, i.summary),
      el("div", { class: "tags", style: "margin-bottom:10px" }, (i.detectors || []).map((d) => el("span", { class: "pill high" }, title(d)))),
      el("div", { class: "muted", style: "font-size:12.5px" }, `Agent ${i.agent_id} · ${i.call_ids.length} call(s) · ${fmtTime(i.created_at)}`),
    ];
    if (i.actions?.length) body.push(el("ul", { style: "margin:10px 0 0;padding-left:18px;color:var(--ink-2);font-size:12.5px" }, i.actions.map((a) => el("li", {}, a))));
    view.append(el("div", { style: "margin-bottom:14px" }, card(i.title, body, el("div", { class: "tags" }, [pill("quarantine"), el("span", { class: "pill " + (i.severity === "critical" ? "crit" : "high") }, i.severity)]))));
  }
};

/* ===================== POLICY STUDIO ===================== */
ROUTES.policy = async (view) => {
  view.append(pageHead("Policy Studio", "Your agent constitution as code. Write rules in plain English, lint them, and back-test against real history before shipping."));
  const data = await api("/v1/policy");
  // NL compiler
  const nlIn = el("input", { placeholder: 'e.g. "Require two approvals for refunds over $500"', value: "Quarantine any agent that posts to pastebin.com" });
  const nlOut = el("div");
  const compile = async (e) => {
    const r = await api("/v1/policy/compile", { method: "POST", body: JSON.stringify({ text: nlIn.value }) });
    nlOut.innerHTML = "";
    nlOut.append(el("div", { class: "muted", style: "margin:10px 0 6px" }, `Confidence ${Math.round(r.confidence * 100)}%`));
    nlOut.append(el("div", { class: "tags", style: "margin-bottom:8px" }, (r.understood || []).map((u) => el("span", { class: "pill allow", style: "font-weight:500" }, u))));
    (r.warnings || []).forEach((w) => nlOut.append(el("div", { class: "pill warn", style: "margin:2px 4px 2px 0" }, "! " + w)));
    if (r.rule) nlOut.append(el("textarea", { rows: 10, readonly: true, style: "margin-top:8px" }, jsyaml(r.rule)));
  };
  view.append(card("Plain-English rule compiler", [
    el("div", { class: "toolbar", style: "margin-bottom:4px" }, [nlIn, btn("Compile", "bolt", "primary", compile)]), nlOut,
  ]));
  view.append(el("div", { style: "height:16px" }));

  // coverage + lint
  const cov = data.coverage;
  const covBody = el("div", {}, [
    el("div", { class: "meter" }, [el("div", { class: "lbl" }, "Tools governed"), el("div", { class: "bar" }, el("span", { style: `width:${cov.coverage_pct * 100}%` })), el("div", { class: "num" }, Math.round(cov.coverage_pct * 100) + "%")]),
    cov.sensitive_gaps.length ? el("div", { class: "pill warn", style: "margin-top:6px" }, "Gaps: " + cov.sensitive_gaps.join(", ")) : el("div", { class: "pill allow", style: "margin-top:6px" }, "All sensitive tools covered"),
  ]);
  const ls = data.lint_summary;
  const lintBody = el("div", {}, [
    el("div", { class: "tags", style: "margin-bottom:10px" }, [
      el("span", { class: "pill " + (ls.error ? "deny" : "allow") }, ls.error + " errors"),
      el("span", { class: "pill warn" }, ls.warning + " warnings"), el("span", { class: "pill info" }, ls.info + " info")]),
    ...(data.lint.slice(0, 6).map((i) => el("div", { style: "font-size:12.5px;margin:3px 0;color:var(--ink-2)" },
      [el("span", { class: "pill " + (i.level === "error" ? "deny" : i.level === "warning" ? "warn" : "info"), style: "margin-right:6px" }, i.code), i.message]))),
  ]);
  view.append(el("div", { class: "grid g-2" }, [card("Coverage", covBody), card("Linter", lintBody)]));
  view.append(el("div", { style: "height:16px" }));

  // editor + backtest
  const ta = el("textarea", { rows: 18 }, data.yaml);
  const out = el("div");
  const editorHead = el("div", { class: "toolbar" }, [
    btn("Back-test vs history", "redteam", "sm", async (e) => withBusy(e.currentTarget, async () => {
      const r = await api("/v1/policy/backtest", { method: "POST", soft: true, body: JSON.stringify({ yaml: ta.value }) });
      if (r.detail) { out.innerHTML = ""; out.append(el("div", { class: "pill deny" }, r.detail)); return; }
      out.innerHTML = "";
      out.append(el("div", { class: "grid g-3", style: "margin-top:12px" }, [
        statCard("Replayed", r.calls_replayed, "past calls", "feed"),
        statCard("Newly blocked", r.newly_blocked_or_flagged, "would now be caught", "shield", "deny"),
        statCard("Newly allowed", r.newly_allowed, "would now pass", "trust", "warn"),
      ]));
    })),
    btn("Lint", "policy", "sm", async () => {
      const r = await api("/v1/policy/lint", { method: "POST", body: JSON.stringify({ yaml: ta.value }) });
      out.innerHTML = ""; out.append(el("div", { class: "pill " + (r.ok ? "allow" : "deny"), style: "margin-top:10px" }, r.ok ? "No errors" : `${r.summary.error} errors`));
    }),
    btn("Apply policy", "bolt", "primary sm", async (e) => withBusy(e.currentTarget, async () => {
      const r = await api("/v1/policy/apply", { method: "POST", soft: true, body: JSON.stringify({ yaml: ta.value, actor: "dashboard" }) });
      if (r.detail) return toast("Not applied", r.detail, "deny");
      toast("Policy applied", `${r.rules} rules · ${r.fingerprint}`, "approval");
    })),
  ]);
  view.append(card("Constitution (" + data.fingerprint + ")", [ta, out], editorHead));
};

/* ===================== RED TEAM ===================== */
ROUTES.redteam = async (view) => {
  view.append(pageHead("Red Team", "An adversarial swarm attacks a sandbox copy of your setup — injection, exfiltration, privilege abuse, fraud, collusion, audit tampering. Nothing touches production.",
    [btn("Run campaign", "skull", "primary", (e) => run(e.currentTarget)),
     btn("Ablation study", "redteam", "", (e) => ablate(e.currentTarget))]));
  const target = el("div");
  view.append(target);
  target.append(el("div", { class: "empty" }, [el("div", { html: icon("redteam") }), el("div", {}, "Click “Run campaign” to launch the attacks.")]));

  async function run(b) {
    await withBusy(b, async () => {
      const r = await api("/v1/redteam/run", { method: "POST" });
      target.innerHTML = "";
      target.append(el("div", { class: "grid g-4", style: "margin-bottom:16px" }, [
        statCard("Attacks", r.attacks, "across " + Object.keys(r.categories).length + " categories", "skull"),
        statCard("Blocked", r.blocked, r.blocked === r.attacks ? "100% caught" : "", "shield", r.blocked === r.attacks ? "allow" : "deny"),
        statCard("Severity-weighted", Math.round(r.weighted_block_rate * 100) + "%", "critical attacks weighted 5×", "trust", "allow"),
        statCard("Duration", Math.round(r.duration_ms) + " ms", r.attacks + " isolated sandboxes", "bolt"),
      ]));
      const cats = Object.entries(r.categories).map(([c, v]) => el("div", { class: "meter" }, [
        el("div", { class: "lbl" }, title(c)), el("div", { class: "bar" }, el("span", { style: `width:${v.blocked / v.total * 100}%;background:var(--allow)` })),
        el("div", { class: "num" }, `${v.blocked}/${v.total}`)]));
      target.append(el("div", { class: "grid g-2" }, [
        card("Blocked by category", cats),
        card("Defence layers that fired", Object.entries(r.layers).map(([l, n]) => el("div", { class: "meter" }, [
          el("div", { class: "lbl mono", style: "font-size:11.5px" }, l), el("div", { class: "bar" }, el("span", { style: `width:${n / r.attacks * 100}%` })), el("div", { class: "num" }, n)]))),
      ]));
      target.append(el("div", { style: "height:16px" }));
      const list = el("div", {});
      for (const o of r.outcomes) {
        list.append(el("details", { class: "attack" }, [
          el("summary", {}, [
            el("span", { class: "pill " + (o.blocked ? "allow" : "deny") }, o.blocked ? "BLOCKED" : "GOT THROUGH"),
            el("span", { style: "font-weight:600" }, o.name),
            el("span", { class: "pill " + (o.severity === "critical" ? "crit" : o.severity) }, o.severity),
            el("span", { class: "chev", html: "›" }),
          ]),
          el("div", { class: "inner" }, [
            el("div", {}, el("b", {}, "Technique: ") ), o.technique,
            el("div", { style: "margin-top:6px" }, [el("b", {}, "Stopped by: "), (o.stopped_by.join(", ") || "—")]),
            o.detail ? el("div", { class: "dim", style: "margin-top:4px" }, o.detail) : null,
          ]),
        ]));
      }
      target.append(card("Every attack (" + r.attacks + ")", list, null, false));
    });
  }
  async function ablate(b) {
    await withBusy(b, async () => {
      const r = await api("/v1/redteam/ablation", { method: "POST" });
      target.innerHTML = "";
      const tb = el("tbody");
      for (const row of r.rows) {
        tb.append(el("tr", {}, [
          el("td", { style: "font-weight:600" }, row.config),
          el("td", {}, `${row.blocked}/${row.attacks}`),
          el("td", {}, el("div", { class: "bar", style: "width:140px" }, el("span", { style: `width:${row.weighted_block_rate * 100}%;background:${row.weighted_block_rate > .9 ? "var(--allow)" : row.weighted_block_rate > .5 ? "var(--warn)" : "var(--deny)"}` }))),
          el("td", { class: "mono" }, Math.round(row.weighted_block_rate * 100) + "%"),
          el("td", { class: "dim", style: "font-size:12px" }, row.missed.length ? row.missed.slice(0, 5).join(", ") + (row.missed.length > 5 ? "…" : "") : "—"),
        ]));
      }
      target.append(el("div", { class: "callout", style: "margin-bottom:14px" }, "Each row disables one defence layer and re-runs every attack. It shows which layer catches what — defence in depth means no single layer is load-bearing."));
      target.append(el("div", { class: "card", flush: true }, el("table", {}, [el("thead", {}, el("tr", {}, ["Configuration", "Blocked", "Resistance", "Weighted", "Attacks that get through"].map((h) => el("th", {}, h)))), tb])));
    });
  }
};

/* ===================== BLAST RADIUS ===================== */
ROUTES.sandbox = async (view) => {
  const pre = new URLSearchParams(location.hash.split("?")[1] || "").get("a");
  view.append(pageHead("Blast Radius", "If an agent were fully compromised right now, what could it actually reach? Computed statically from its tokens and the policy — nothing is executed."));
  const { agents } = await api("/v1/agents");
  const sel = el("select", { style: "max-width:260px", onchange: () => draw(sel.value) }, agents.map((a) => el("option", { value: a.id }, `${a.name} (${a.id})`)));
  if (pre) sel.value = pre;
  view.append(el("div", { class: "toolbar", style: "margin-bottom:16px" }, [el("label", { style: "margin:0 8px 0 0" }, "Agent:"), sel]));
  const out = el("div");
  view.append(out);
  async function draw(id) {
    out.innerHTML = '<div class="loading"></div>';
    const b = await api(`/v1/agents/${id}/blast-radius`);
    const tone = b.unattended_exfil_tools.length ? "deny" : b.worst_case_risk >= 15 ? "warn" : "allow";
    out.innerHTML = "";
    out.append(el("div", { class: "callout", style: `border-left:3px solid var(--${tone});margin-bottom:16px` }, [el("b", {}, b.assessment)]));
    out.append(el("div", { class: "grid g-3", style: "margin-bottom:16px" }, [
      statCard("Reachable unattended", b.reachable_count, "tools, no human needed", "bolt", tone),
      statCard("Needs approval", b.needs_approval.length, "held for a human", "approvals", "warn"),
      statCard("Blocked outright", b.blocked.length, "token / policy denies", "shield", "allow"),
    ]));
    const colTable = (title2, items, kind) => card(title2, items.length ? el("div", { class: "tags" }, items.map((t) =>
      el("span", { class: "pill " + kind, title: t.why || "", style: "font-weight:500" }, t.tool))) : el("span", { class: "dim" }, "none"));
    out.append(el("div", { class: "grid g-3" }, [
      colTable("Reachable", b.reachable, "deny"), colTable("Needs approval", b.needs_approval, "warn"), colTable("Blocked", b.blocked, "allow"),
    ]));
  }
  draw(sel.value);
};

/* ===================== TRUST SCORE ===================== */
ROUTES.trust = async (view) => {
  view.append(pageHead("Trust Score", "One explainable, signed score for how well-governed your agents are. Embed the badge to show customers your agents are safe.",
    [btn("Recompute", "trust", "", (e) => withBusy(e.currentTarget, () => route()))]));
  const r = await api("/v1/trust");
  const color = r.score >= 85 ? "--allow" : r.score >= 70 ? "--approval" : r.score >= 55 ? "--warn" : "--deny";
  const circ = 2 * Math.PI * 54;
  const ring = el("div", { class: "ring", html: `
    <svg viewBox="0 0 120 120" width="132" height="132">
      <circle cx="60" cy="60" r="54" fill="none" stroke="var(--line-2)" stroke-width="11"/>
      <circle cx="60" cy="60" r="54" fill="none" stroke="var(${color})" stroke-width="11" stroke-linecap="round"
        stroke-dasharray="${circ}" stroke-dashoffset="${circ * (1 - r.score / 100)}" transform="rotate(-90 60 60)"/>
    </svg>
    <div class="lbl"><div class="n">${r.score}</div><div class="g">grade ${r.grade}</div></div>`});
  const pillars = el("div", { style: "flex:1" }, Object.values(r.breakdown).map((p) => el("div", { class: "meter" }, [
    el("div", { class: "lbl", title: p.about }, p.label), el("div", { class: "bar" }, el("span", { style: `width:${p.score}%` })),
    el("div", { class: "num" }, Math.round(p.score)),
  ])));
  view.append(card("Overall", el("div", { class: "ring-wrap" }, [ring, pillars])));
  view.append(el("div", { style: "height:16px" }));
  const ev = r.evidence;
  view.append(el("div", { class: "grid g-4" }, [
    statCard("Attacks blocked", `${ev.attacks_blocked}/${ev.attacks_tested}`, "red-team", "skull"),
    statCard("Ledger", ev.ledger_intact ? "intact" : "BROKEN", `${ev.ledger_records} records`, "compliance", ev.ledger_intact ? "allow" : "deny"),
    statCard("Agents", ev.agents, "registered", "agents"),
    statCard("Coverage gaps", ev.sensitive_gaps.length, ev.sensitive_gaps.join(", ") || "none", "policy", ev.sensitive_gaps.length ? "warn" : "allow"),
  ]));
  view.append(el("div", { style: "height:16px" }));
  const badge = el("div", { style: "display:flex;gap:20px;align-items:center;flex-wrap:wrap" }, [
    el("img", { src: "/v1/trust/badge.svg?t=" + Date.now(), alt: "Secured by WARDEN", style: "height:52px" }),
    el("div", { style: "flex:1;min-width:240px" }, [
      el("div", { class: "muted", style: "margin-bottom:8px" }, "Signed attestation (verifiable with the org key):"),
      el("textarea", { rows: 3, readonly: true, class: "mono" }, `<img src="https://your-warden/v1/trust/badge.svg"\n     alt="Secured by WARDEN — Trust Score ${r.score}/100">`),
      el("div", { class: "dim mono", style: "margin-top:8px;word-break:break-all" }, "sig: " + (r.attestation?.signature || "").slice(0, 48) + "…"),
    ]),
  ]);
  view.append(card("Embeddable badge", badge));
};

/* ===================== COMPLIANCE ===================== */
ROUTES.compliance = async (view) => {
  view.append(pageHead("Compliance", "The live audit trail, packaged as evidence an auditor can read. Controls map loosely to NIST 800-53 families."));
  const r = await api("/v1/compliance");
  view.append(el("div", { class: "grid g-4", style: "margin-bottom:16px" }, [
    statCard("Controls", r.summary.controls, "assessed", "compliance"),
    statCard("Passing", r.summary.passing, "", "trust", "allow"),
    statCard("Warnings", r.summary.warnings, "", "incidents", "warn"),
    statCard("Failing", r.summary.failing, "", "shield", r.summary.failing ? "deny" : "allow"),
  ]));
  const tb = el("tbody");
  for (const c of r.controls) tb.append(el("tr", {}, [
    el("td", { class: "mono" }, c.id), el("td", { style: "font-weight:600" }, c.control),
    el("td", {}, pill(c.status === "pass" ? "allow" : c.status === "warn" ? "warn" : "deny")),
    el("td", { class: "muted", style: "font-size:12.5px" }, c.evidence),
  ]));
  view.append(el("div", { class: "card", flush: true }, el("table", {}, [el("thead", {}, el("tr", {}, ["ID", "Control", "Status", "Evidence"].map((h) => el("th", {}, h)))), tb])));
  view.append(el("div", { style: "height:16px" }));
  view.append(card("Audit ledger", el("div", {}, [
    el("div", { class: "toolbar", style: "margin-bottom:10px" }, [
      el("span", { class: "pill " + (r.ledger.ok ? "allow" : "deny") }, r.ledger.ok ? "Hash chain intact" : "BROKEN at #" + r.ledger.broken_at),
      el("span", { class: "dim" }, r.ledger.count + " records"),
      btn("Sign checkpoint", "compliance", "sm", async (e) => { const cp = await api("/v1/ledger/checkpoint", { method: "POST" }); toast("Checkpoint signed", "root " + cp.root.slice(0, 16) + "…", "approval"); }),
    ]),
    el("div", { class: "dim", style: "font-size:12.5px" }, r.note),
  ])));
};

/* ===================== CONNECT (MCP / SDK) ===================== */
ROUTES.connect = async (view) => {
  view.append(pageHead("Connect your agents", "WARDEN speaks three protocols so any agent framework can sit behind it in minutes."));
  const tools = await api("/v1/tools");
  const base = location.origin;
  const blocks = [
    ["Python SDK (2 lines)", `from warden.sdk import WardenClient

agent = WardenClient.remote("${base}", "support-bot", token)
agent = agent.session("Resolve support ticket #4821")

# every call is now governed, logged and (if needed) blocked
agent.call("send_email", to="olivia.martin@gmail.com",
           subject="Your refund", body="On its way!")`],
    ["REST", `curl -X POST ${base}/v1/call \\
  -H "Authorization: Bearer $TOKEN" \\
  -d '{"agent_id":"support-bot","tool":"db_query","args":{"order_id":"ord_4821"}}'`],
    ["MCP (Model Context Protocol)", `# point any MCP client at:
${base}/mcp

# tools/list returns ${tools.tools.length} governed tools;
# tools/call runs them through the WARDEN gateway.`],
  ];
  view.append(el("div", { class: "grid g-1" }, blocks.map(([t, code]) =>
    card(t, el("textarea", { rows: code.split("\n").length + 1, readonly: true, class: "mono" }, code)))));
  view.append(el("div", { style: "height:16px" }));
  const tb = el("tbody");
  for (const t of tools.tools) tb.append(el("tr", {}, [
    el("td", { class: "mono", style: "font-weight:600" }, t.name), el("td", { class: "muted" }, t.description),
    el("td", {}, el("span", { class: "pill info" }, t.category)),
    el("td", {}, t.sink === "external" ? el("span", { class: "pill warn" }, "external") : el("span", { class: "dim" }, t.sink || "—")),
    el("td", { class: "mono" }, t.risk),
  ]));
  view.append(card("Governed tools", el("table", {}, [el("thead", {}, el("tr", {}, ["Tool", "Description", "Category", "Sink", "Risk"].map((h) => el("th", {}, h)))), tb]), null, true));
};

/* tiny yaml emitter just for the NL preview */
function jsyaml(obj, indent = 0) {
  const pad = "  ".repeat(indent);
  if (Array.isArray(obj)) return obj.map((v) => `${pad}- ${typeof v === "object" ? "\n" + jsyaml(v, indent + 1) : v}`).join("\n");
  if (obj && typeof obj === "object") return Object.entries(obj).map(([k, v]) =>
    typeof v === "object" && v !== null ? `${pad}${k}:\n${jsyaml(v, indent + 1)}` : `${pad}${k}: ${v}`).join("\n");
  return pad + obj;
}

/* ---------------- live stream ---------------- */
function connectStream() {
  const es = new EventSource("/v1/stream");
  const dot = $("#livedot"), txt = $("#livetext");
  es.addEventListener("hello", (e) => {
    const d = JSON.parse(e.data);
    state.events = (d.recent || []).filter((x) => x.type === "call").reverse().concat(state.events).slice(0, 400);
    dot.classList.add("on"); txt.textContent = "live";
  });
  es.onopen = () => { dot.classList.add("on"); txt.textContent = "live"; };
  es.onerror = () => { dot.classList.remove("on"); txt.textContent = "reconnecting…"; };
  es.onmessage = (e) => {
    const ev = JSON.parse(e.data);
    if (ev.type === "call") {
      state.events.unshift(ev); state.events = state.events.slice(0, 400);
      const v = $("#view"); if (v._onEvent) v._onEvent(ev);
      const page = location.hash.slice(1) || "overview";
      if (page === "overview") route();
      if (["deny", "quarantine"].includes(ev.verdict)) toast(title(ev.verdict) + ": " + ev.tool, ev.explanation, ev.verdict);
    } else if (ev.type === "incident") {
      toast("Incident opened", ev.incident.title, "quarantine");
      refreshBadges();
    } else if (ev.type === "approval") {
      refreshBadges();
      if (ev.approval.status === "pending") toast("Approval needed", ev.approval.reason, "approval");
    } else if (ev.type === "agent") {
      if (["agents", "overview"].includes(location.hash.slice(1) || "overview")) route();
    }
  };
}
async function refreshBadges() {
  try {
    const s = await api("/v1/stats");
    const a = $("#nav-apr"), i = $("#nav-inc");
    if (a) { a.textContent = s.pending_approvals; a.style.display = s.pending_approvals ? "" : "none"; }
    if (i) { i.textContent = s.open_incidents; i.style.display = s.open_incidents ? "" : "none"; }
  } catch {}
}

/* ---------------- boot ---------------- */
function initTheme() {
  const saved = (() => { try { return localStorage.getItem("warden-theme"); } catch { return null; } })();
  if (saved) document.documentElement.dataset.theme = saved;
  $("#themebtn").addEventListener("click", (e) => {
    e.preventDefault();
    const cur = document.documentElement.dataset.theme;
    const next = cur === "dark" ? "light" : cur === "light" ? "dark" : (matchMedia("(prefers-color-scheme: dark)").matches ? "light" : "dark");
    document.documentElement.dataset.theme = next;
    try { localStorage.setItem("warden-theme", next); } catch {}
  });
}
window.addEventListener("hashchange", route);
document.addEventListener("DOMContentLoaded", () => {
  buildNav(); initTheme(); connectStream(); refreshBadges(); route();
  setInterval(refreshBadges, 8000);
});
