/* Inbox review UI (bundled to public/app.js by esbuild). Plain DOM, no framework. */

type Json = Record<string, any>; // eslint-disable-line @typescript-eslint/no-explicit-any

const $ = <T extends Element = HTMLElement>(sel: string, root: ParentNode = document) => root.querySelector(sel) as T;
const $$ = <T extends Element = HTMLElement>(sel: string, root: ParentNode = document) => [...root.querySelectorAll(sel)] as T[];
const esc = (s: unknown) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]!);

const CAT: Record<string, string> = {
  how_to: "How-to", troubleshooting: "Troubleshooting", project_request: "Project request", refund_cancellation: "Refund / cancellation",
  complaint: "Complaint", quote_request: "Quote request", follow_up: "Follow-up", other: "Other",
};
const STATUS: Record<string, string> = { new: "New", processing: "Drafting", pending_review: "Needs review", sent: "Sent", rejected: "Closed", escalated: "With expert", error: "Failed" };
const KB: Record<string, string> = { answered: "Knowledge: answered", partial: "Knowledge: partly answered", insufficient_evidence: "Knowledge: not found", unavailable: "Knowledge: unavailable", not_needed: "No product lookup", error: "Knowledge: error" };

let filter = "pending_review";
let selected: string | null = null;
let current: Json | null = null;

async function api(path: string, opts: RequestInit = {}): Promise<Json> {
  const res = await fetch(path, { headers: { "Content-Type": "application/json" }, ...opts });
  const body = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(body?.error?.message ?? `HTTP ${res.status}`);
  return body;
}
const post = (path: string, body: unknown = {}) => api(path, { method: "POST", body: JSON.stringify(body) });

function reviewer(): string | null {
  const v = ($("#reviewer") as HTMLInputElement).value.trim();
  if (!v) {
    ($("#reviewer") as HTMLInputElement).focus();
    toast("Add your name in “Reviewing as” first — every decision is recorded against a person.", true);
    return null;
  }
  try { localStorage.setItem("reviewer", v); } catch { /* storage may be unavailable */ }
  return v;
}

function toast(msg: string, bad = false) {
  const b = $("#banners");
  b.innerHTML = `<div class="banner ${bad ? "bad" : ""}" role="status">${esc(msg)}</div>`;
  setTimeout(() => { if (b.textContent?.includes(msg.slice(0, 20))) b.innerHTML = ""; }, 6000);
}

const ago = (iso: string) => {
  const d = new Date(iso.endsWith("Z") ? iso : iso + "Z");
  return d.toLocaleString(undefined, { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" });
};

// ------------------------------------------------------------------ health
async function loadHealth() {
  try {
    const h = await api("/api/health");
    const llm = h.llm_provider === "fake" ? `<span class="pill warn">Demo model (fake)</span>` : h.llm_provider === "unconfigured" ? `<span class="pill bad">No model key</span>` : `<span class="pill ok">Claude ${esc(h.models.draft)}</span>`;
    const kb = h.kb.ok ? `<span class="pill ok">Knowledge Brain connected</span>` : `<span class="pill bad">Knowledge Brain unreachable</span>`;
    $("#health").innerHTML = `${kb}${llm}<span class="pill">Prompt ${esc(h.prompts.draft)}</span>${h.batch?.running ? `<span class="pill"><span class="spinner"></span> Drafting ${h.batch.done}/${h.batch.total}</span>` : ""}`;
    return h;
  } catch {
    $("#health").innerHTML = `<span class="pill bad">Inbox service not responding</span>`;
    return null;
  }
}

// ------------------------------------------------------------------- inbox
async function renderInbox() {
  const view = $("#view");
  if (!$(".inbox", view)) {
    view.innerHTML = `<div class="inbox">
      <section class="list" aria-label="Emails">
        <div class="list-tools">
          <div class="filters" role="group" aria-label="Filter">
            ${[["pending_review", "Needs review"], ["new", "New"], ["sent", "Sent"], ["escalated", "With expert"], ["error", "Failed"], ["", "All"]]
              .map(([v, l]) => `<button type="button" data-f="${v}" aria-pressed="${v === filter}">${l}</button>`).join("")}
          </div>
          <div style="display:flex;gap:8px">
            <button class="btn small" id="draftAll">Draft all new emails</button>
            <button class="btn quiet small" id="addEmail">Add an email</button>
          </div>
        </div>
        <div class="rows" id="rows"></div>
      </section>
      <section id="detail"><div class="empty">Choose an email to review its draft.</div></section>
    </div>`;
    $$("[data-f]", view).forEach((b) => b.addEventListener("click", () => {
      filter = b.dataset.f!;
      $$("[data-f]", view).forEach((x) => x.setAttribute("aria-pressed", String(x === b)));
      loadList();
    }));
    $("#draftAll").addEventListener("click", async () => {
      try {
        const r = await post("/api/process-new");
        toast(`Drafting ${r.total} email(s) in the background…`);
        pollBatch();
      } catch (e) { toast((e as Error).message, true); }
    });
    $("#addEmail").addEventListener("click", openNewEmail);
  }
  await loadList();
  if (selected) await openEmail(selected);
}

async function pollBatch() {
  const h = await loadHealth();
  await loadList();
  if (h?.batch?.running) setTimeout(pollBatch, 2500);
  else if (h) toast(`Drafting finished: ${h.batch.done} processed${h.batch.errors ? `, ${h.batch.errors} failed` : ""}.`, !!h.batch.errors);
}

async function loadList() {
  const { emails } = await api(`/api/emails${filter ? `?status=${filter}` : ""}`);
  const rows = $("#rows");
  if (!rows) return;
  rows.innerHTML = emails.length
    ? emails.map((e: Json) => `<button class="row" data-id="${esc(e.id)}" aria-current="${e.id === selected}">
        <div class="top"><b>${esc(e.from_name)}</b><time>${ago(e.received_at)}</time></div>
        <div class="subj">${esc(e.subject)}</div>
        <div class="chips">
          <span class="chip st-${esc(e.status)}">${STATUS[e.status] ?? esc(e.status)}</span>
          ${e.category ? `<span class="chip">${CAT[e.category] ?? esc(e.category)}</span>` : ""}
          ${e.risk ? `<span class="risk ${esc(e.risk)}">${esc(e.risk)} risk</span>` : ""}
          ${e.blocks ? `<span class="chip st-error">${e.blocks} blocking</span>` : ""}
          ${e.draft_status === "auto_sent" ? `<span class="chip st-sent">Sent automatically</span>` : ""}
        </div></button>`).join("")
    : `<div class="empty" style="margin:12px">${filter === "pending_review" ? "Nothing waiting for review. Draft the new emails to fill this queue." : "No emails here."}</div>`;
  $$(".row", rows).forEach((r) => r.addEventListener("click", () => openEmail(r.dataset.id!)));
}

async function openEmail(id: string) {
  selected = id;
  $$(".row").forEach((r) => r.setAttribute("aria-current", String(r.dataset.id === id)));
  const data = await api(`/api/emails/${encodeURIComponent(id)}`);
  current = data;
  const { email, draft, sent, history } = data;
  const detail = $("#detail");
  const emailCard = `<article class="card">
    <div class="email-head"><b>${esc(email.from_name)}</b><span>${esc(email.from_email)}</span><span>${ago(email.received_at)}</span><span class="chip st-${esc(email.status)}">${STATUS[email.status] ?? esc(email.status)}</span></div>
    <div class="email-subj">${esc(email.subject)}</div>
    <div class="email-body">${esc(email.body)}</div>
    ${history && !history.startsWith("(no earlier") ? `<details style="margin-top:12px"><summary>Earlier messages in this thread</summary><pre class="hist">${esc(history)}</pre></details>` : ""}
    ${email.last_error ? `<div class="banner bad" style="margin-top:12px">Drafting failed: ${esc(email.last_error)}</div>` : ""}
  </article>`;
  if (!draft) {
    detail.innerHTML = `<div class="detail"><div>${emailCard}<div class="card"><h2>No draft yet</h2><p>Classify the email, look up product facts in the Knowledge Brain and write a reply for review.</p><button class="btn" id="process">Draft a reply</button></div></div><div></div></div>`;
    $("#process").addEventListener("click", () => processEmail(id));
    return;
  }
  detail.innerHTML = `<div class="detail"><div>${emailCard}${draftCard(draft, sent)}</div><div>${evidenceCard(draft)}</div></div>`;
  wireDraft(draft);
}

async function processEmail(id: string) {
  const btn = $("#process") as HTMLButtonElement | null;
  if (btn) { btn.disabled = true; btn.innerHTML = `<span class="spinner"></span> Drafting — classifying, checking knowledge, writing`; }
  try {
    await post(`/api/emails/${encodeURIComponent(id)}/process`);
  } catch (e) {
    toast(`Drafting failed: ${(e as Error).message}`, true);
  }
  await loadList();
  await openEmail(id);
}

function draftCard(d: Json, sent: Json | null): string {
  const reviewable = d.status === "pending_review";
  const issues = (d.guards as Json[]).map((g) => `<li class="${g.severity}"><code>${esc(g.code)}</code> ${esc(g.message)}</li>`).join("");
  const blocks = (d.guards as Json[]).some((g) => g.severity === "block");
  const auto = d.status === "auto_sent";
  return `<article class="card">
    <h2>${d.holding_reply ? "Holding reply (no product facts)" : "Draft reply"}</h2>
    <div class="draft-meta">
      <span class="chip">${CAT[d.category] ?? esc(d.category)}</span>
      <span class="risk ${esc(d.risk)}">${esc(d.risk)} risk</span>
      <span class="chip">${KB[d.kb_status] ?? esc(d.kb_status)}</span>
      <span class="chip">${esc(d.models.draft)}, prompt ${esc(d.prompt_versions.draft)}</span>
      <span class="chip">${(d.latency_ms / 1000).toFixed(1)} s, $${Number(d.cost_usd).toFixed(4)}</span>
      ${d.models.draft === "fake" ? `<span class="chip st-error">Demo model</span>` : ""}
    </div>
    ${sent ? `<div class="sent-box">Sent ${auto ? "automatically" : `by ${esc(sent.sent_by)}`} at ${ago(sent.sent_at)}.${d.audit_status === "pending" ? " Queued for a post-send audit." : ""}</div>` : ""}
    ${d.status === "escalated" ? `<div class="banner" style="margin-bottom:12px">Handed to an expert by ${esc(d.reviewer)}.</div>` : ""}
    ${reviewable && d.approval_reasons.length ? `<div class="reasons"><b>Why a person needs to check this</b><ul>${(d.approval_reasons as string[]).map((r) => `<li>${esc(r)}</li>`).join("")}</ul></div>` : ""}
    ${issues ? `<ul class="issues">${issues}</ul>` : ""}
    <label class="f" for="subj">Subject</label>
    <input class="subj" id="subj" value="${esc(sent?.subject ?? d.reply_subject)}" ${reviewable ? "" : "readonly"}>
    <label class="f" for="body">Reply</label>
    <textarea class="body" id="body" ${reviewable ? "" : "readonly"}>${esc(sent?.body ?? d.reply_body)}</textarea>
    <div class="edit-note" id="editNote"></div>
    ${reviewable ? `
      ${blocks ? `<label class="ack"><input type="checkbox" id="ack"> I've checked the blocking issues above and this reply is OK to send.</label>` : ""}
      <div class="actions">
        <button class="btn" id="approve">Send as drafted</button>
        <button class="btn" id="sendEdited" disabled>Send my edited version</button>
        <select id="reason" aria-label="Why did you edit?" hidden>
          <option value="">Why did you edit?</option>
          <option>Factual correction</option><option>Missing information</option><option>Too long / wordy</option>
          <option>Tone</option><option>Removed a commitment</option><option>Formatting</option><option>Other</option>
        </select>
        <span style="flex:1"></span>
        <button class="btn quiet" id="redraft">Redraft</button>
        <button class="btn quiet" id="escalate">Needs an expert</button>
        <button class="btn danger" id="reject">Reject</button>
      </div>` : ""}
    ${d.audit_status === "pending" ? `<div class="actions"><button class="btn small" id="auditOk">Audit: reply was fine</button><button class="btn danger small" id="auditFlag">Audit: flag a problem</button></div>` : ""}
  </article>`;
}

function evidenceCard(d: Json): string {
  const lookups = (d.knowledge as Json[]).map((l) => `<div class="lookup">
      <div class="q">${esc(l.question)}</div>
      <span class="chip ${l.status === "answered" ? "st-sent" : l.status === "unavailable" || l.status === "error" ? "st-error" : "st-pending_review"}">${KB[l.status] ?? esc(l.status)}</span>
      ${l.escalation_id ? `<span class="chip">Expert task #${l.escalation_id}</span>` : ""}
      ${l.answer ? `<div class="a">${esc(l.answer.replace(/\[S\d+\]/g, ""))}</div>` : ""}
      ${l.error ? `<div class="banner bad">${esc(l.error)}</div>` : ""}
      ${(l.sources as Json[]).map((s) => `<div class="src"><span class="kid">${esc(s.id)}</span><a href="${esc(s.url)}" target="_blank" rel="noopener">${esc(s.title)}</a>${s.kind === "image" ? " (screenshot)" : ""}</div>`).join("")}
    </div>`).join("");
  const claims = (d.draft.claims as Json[]).map((c) => `<div class="claim ${c.source_ids.length ? "" : "nosrc"}">${c.source_ids.map((k: string) => `<span class="kid">${esc(k)}</span>`).join(" ")} ${esc(c.text)}${c.source_ids.length ? "" : " (no source)"}</div>`).join("");
  const cl = d.classification;
  return `<article class="card"><h2>Evidence</h2>${lookups || `<p class="empty" style="padding:12px">No product lookup was needed for this email.</p>`}</article>
    ${claims ? `<article class="card"><h2>Facts stated in the reply</h2>${claims}</article>` : ""}
    <article class="card"><h2>How it was read</h2>
      <div style="font-size:14px">${esc(cl.summary)}</div>
      <div class="chips" style="margin-top:8px"><span class="chip">urgency ${esc(cl.urgency)}</span><span class="chip">${esc(cl.sentiment)}</span>${(cl.flags as string[]).map((f) => `<span class="chip st-error">${esc(f.replace(/_/g, " "))}</span>`).join("")}</div>
      ${d.draft.commitments?.length ? `<h3>Promises in the reply</h3><ul style="margin:0;padding-left:18px;font-size:14px">${(d.draft.commitments as string[]).map((c) => `<li>${esc(c)}</li>`).join("")}</ul>` : ""}
      <h3>Next action for the team</h3><div style="font-size:14px">${esc(d.draft.next_action)}${d.draft.follow_up_date ? ` (follow up ${esc(d.draft.follow_up_date)})` : ""}</div>
    </article>`;
}

function wireDraft(d: Json) {
  const body = $("#body") as HTMLTextAreaElement;
  const subj = $("#subj") as HTMLInputElement;
  const original = d.reply_body as string;
  const norm = (s: string) => s.replace(/\r\n?/g, "\n").split("\n").map((l) => l.trimEnd()).join("\n").replace(/\n{3,}/g, "\n\n").trim();
  const update = () => {
    const edited = norm(body.value) !== norm(original) || subj.value !== d.reply_subject;
    body.classList.toggle("edited", edited);
    const a = $("#approve") as HTMLButtonElement | null, s = $("#sendEdited") as HTMLButtonElement | null, r = $("#reason") as HTMLSelectElement | null;
    if (a) a.disabled = edited;
    if (s) s.disabled = !edited;
    if (r) r.hidden = !edited;
    $("#editNote").textContent = edited ? "Edited. This send will reset the autonomy streak for this category and be saved as feedback." : "";
  };
  body?.addEventListener("input", update);
  subj?.addEventListener("input", update);
  const ack = () => ($("#ack") as HTMLInputElement | null)?.checked ?? false;
  const act = async (fn: () => Promise<Json>, done: string) => {
    try {
      const r = await fn();
      const a = r.autonomy;
      toast(`${done}${a ? ` ${CAT[a.category]} streak: ${a.consecutive_unedited}/${a.threshold}${a.eligible ? " — eligible for autonomy" : ""}.` : ""}`);
      await loadList();
      await openEmail(d.email_id);
    } catch (e) { toast((e as Error).message, true); }
  };
  $("#approve")?.addEventListener("click", () => { const who = reviewer(); if (who) act(() => post(`/api/drafts/${d.id}/approve`, { reviewer: who, acknowledged: ack() }), "Sent as drafted."); });
  $("#sendEdited")?.addEventListener("click", () => {
    const who = reviewer(); if (!who) return;
    const reason = ($("#reason") as HTMLSelectElement).value || null;
    act(() => post(`/api/drafts/${d.id}/send-edited`, { reviewer: who, body: body.value, subject: subj.value, reason, acknowledged: ack() }), "Sent with your edits.");
  });
  $("#redraft")?.addEventListener("click", () => processEmail(d.email_id));
  $("#escalate")?.addEventListener("click", () => ask("Hand this to an expert", "What does the expert need to know?", false, async (reason) => {
    const who = reviewer(); if (!who) return;
    await act(() => post(`/api/drafts/${d.id}/escalate`, { reviewer: who, reason }), "Handed to an expert (also added to the Knowledge Brain expert queue).");
  }));
  $("#reject")?.addEventListener("click", () => ask("Reject this draft", "Why is it wrong? (required)", true, async (reason, replacement) => {
    const who = reviewer(); if (!who) return;
    await act(() => post(`/api/drafts/${d.id}/reject`, { reviewer: who, reason, replacement: replacement || null }), replacement ? "Draft rejected; your reply was sent." : "Draft rejected.");
  }));
  $("#auditOk")?.addEventListener("click", () => { const who = reviewer(); if (who) act(() => post(`/api/drafts/${d.id}/audit`, { reviewer: who, ok: true }), "Audit recorded."); });
  $("#auditFlag")?.addEventListener("click", () => ask("Flag an automatic reply", "What was wrong with it?", false, async (note) => {
    const who = reviewer(); if (!who) return;
    await act(() => post(`/api/drafts/${d.id}/audit`, { reviewer: who, ok: false, note }), "Flagged. Autonomy for this category is now off.");
  }));
}

function ask(title: string, prompt: string, withReplacement: boolean, onOk: (text: string, replacement?: string) => void) {
  const dlg = document.createElement("dialog");
  dlg.innerHTML = `<form method="dialog"><h2 style="margin-top:0;font-size:18px">${esc(title)}</h2>
    <label class="f">${esc(prompt)}</label><textarea name="t" rows="3" required></textarea>
    ${withReplacement ? `<label class="f">Your replacement reply (optional — sent instead of the draft)</label><textarea name="r" rows="6"></textarea>` : ""}
    <div class="actions"><button class="btn" value="ok">Confirm</button><button class="btn quiet" value="cancel" formnovalidate>Cancel</button></div></form>`;
  document.body.append(dlg);
  dlg.showModal();
  dlg.addEventListener("close", () => {
    const f = new FormData(dlg.querySelector("form")!);
    if (dlg.returnValue === "ok") onOk(String(f.get("t") ?? ""), String(f.get("r") ?? ""));
    dlg.remove();
  });
}

function openNewEmail() {
  const dlg = document.createElement("dialog");
  dlg.innerHTML = `<form method="dialog"><h2 style="margin-top:0;font-size:18px">Add an email to the inbox</h2>
    <label class="f">From name</label><input name="from_name" required value="Test Customer">
    <label class="f">From email</label><input name="from_email" type="email" required value="test@example.com">
    <label class="f">Subject</label><input name="subject" required>
    <label class="f">Body</label><textarea name="body" rows="7" required></textarea>
    <div class="actions"><button class="btn" value="ok">Add email</button><button class="btn quiet" value="cancel" formnovalidate>Cancel</button></div></form>`;
  document.body.append(dlg);
  dlg.showModal();
  dlg.addEventListener("close", async () => {
    if (dlg.returnValue === "ok") {
      const f = Object.fromEntries(new FormData(dlg.querySelector("form")!));
      try {
        const e = await post("/api/emails", f);
        filter = "new";
        $$("[data-f]").forEach((x) => x.setAttribute("aria-pressed", String(x.dataset.f === "new")));
        selected = e.id;
        await loadList();
        await openEmail(e.id);
      } catch (err) { toast((err as Error).message, true); }
    }
    dlg.remove();
  });
}

// --------------------------------------------------------------- autonomy
async function renderAutonomy() {
  const a = await api("/api/autonomy");
  const view = $("#view");
  view.innerHTML = `<div class="grid2"><div>
    <article class="card"><h2>Autonomous sending by category</h2>
    <p style="margin-top:0;color:var(--muted);max-width:70ch">A category becomes eligible after ${a.threshold} drafts in a row are sent with no changes at all (the text that went out is compared with the draft — clicking approve isn't enough). A named person still has to switch it on, and any edit, rejection or flagged audit switches it off and restarts the count. Money, complaint, quote and project emails always go to a person.</p>
    <table><thead><tr><th>Category</th><th>Unchanged in a row</th><th class="num">Best</th><th class="num">Sent automatically</th><th>State</th><th></th></tr></thead><tbody>
    ${(a.categories as Json[]).map((c) => {
      const pct = Math.min(100, (c.consecutive_unedited / c.threshold) * 100);
      const state = c.never_autonomous ? "Always a person" : c.enabled ? "On" : c.eligible ? "Eligible, off" : "Earning";
      return `<tr><td>${esc(c.label)}</td>
        <td><div class="streak"><div class="track"><div class="fill ${pct >= 100 ? "full" : ""}" style="width:${pct}%"></div></div><span>${c.consecutive_unedited}/${c.threshold}</span></div></td>
        <td class="num">${c.best_streak}</td><td class="num">${c.auto_sent}</td>
        <td>${esc(state)}${c.disabled_reason ? `<br><small style="color:var(--muted)">${esc(c.disabled_reason)}</small>` : ""}</td>
        <td>${c.never_autonomous ? "" : c.enabled ? `<button class="btn danger small" data-off="${c.category}">Turn off</button>` : `<button class="btn small" data-on="${c.category}" ${c.eligible ? "" : "disabled"}>Turn on</button>`}</td></tr>`;
    }).join("")}
    </tbody></table></article>
    <article class="card"><h2>Post-send audit queue</h2>
    ${(a.audit_queue as Json[]).length ? (a.audit_queue as Json[]).map((q) => `<div class="lookup"><div class="q">${esc(q.reply_subject)} <span class="chip">${CAT[q.category]}</span></div><div class="a">${esc(q.reply_body)}</div>
      <button class="btn small" data-audit-ok="${q.id}">Reply was fine</button> <button class="btn danger small" data-audit-flag="${q.id}">Flag a problem</button></div>`).join("") : `<div class="empty">No automatic replies waiting for audit.</div>`}
    </article></div>
    <article class="card"><h2>Recent autonomy events</h2>
    <table><tbody>${(a.events as Json[]).map((e) => `<tr><td>${ago(e.at)}</td><td>${esc(CAT[e.category] ?? e.category)}</td><td>${esc(e.event.replace(/_/g, " "))}${e.streak_after !== null ? ` (${e.streak_after})` : ""}</td><td>${esc(e.actor ?? "")}${e.detail ? `<br><small>${esc(e.detail)}</small>` : ""}</td></tr>`).join("") || `<tr><td>No events yet.</td></tr>`}</tbody></table></article></div>`;
  $$("[data-on]", view).forEach((b) => b.addEventListener("click", async () => { const who = reviewer(); if (!who) return; try { await post(`/api/autonomy/${b.dataset.on}/enable`, { actor: who }); renderAutonomy(); } catch (e) { toast((e as Error).message, true); } }));
  $$("[data-off]", view).forEach((b) => b.addEventListener("click", async () => { const who = reviewer(); if (!who) return; await post(`/api/autonomy/${b.dataset.off}/disable`, { actor: who, reason: `turned off by ${who}` }); renderAutonomy(); }));
  $$("[data-audit-ok]", view).forEach((b) => b.addEventListener("click", async () => { const who = reviewer(); if (!who) return; await post(`/api/drafts/${b.dataset.auditOk}/audit`, { reviewer: who, ok: true }); renderAutonomy(); }));
  $$("[data-audit-flag]", view).forEach((b) => b.addEventListener("click", async () => { const who = reviewer(); if (!who) return; await post(`/api/drafts/${b.dataset.auditFlag}/audit`, { reviewer: who, ok: false, note: "flagged from audit queue" }); renderAutonomy(); }));
}

// --------------------------------------------------------------- learning
function wordDiff(a: string, b: string): string {
  const x = a.split(/(\s+)/), y = b.split(/(\s+)/);
  const m = x.length, n = y.length;
  if (m * n > 400000) return `<del>${esc(a)}</del>\n<ins>${esc(b)}</ins>`;
  const dp = Array.from({ length: m + 1 }, () => new Uint16Array(n + 1));
  for (let i = m - 1; i >= 0; i--) for (let j = n - 1; j >= 0; j--) dp[i][j] = x[i] === y[j] ? dp[i + 1][j + 1] + 1 : Math.max(dp[i + 1][j], dp[i][j + 1]);
  let i = 0, j = 0, out = "";
  while (i < m && j < n) {
    if (x[i] === y[j]) { out += esc(x[i]); i++; j++; }
    else if (dp[i + 1][j] >= dp[i][j + 1]) out += `<del>${esc(x[i++])}</del>`;
    else out += `<ins>${esc(y[j++])}</ins>`;
  }
  while (i < m) out += `<del>${esc(x[i++])}</del>`;
  while (j < n) out += `<ins>${esc(y[j++])}</ins>`;
  return out;
}

async function renderLearning() {
  const [{ stats, recent }, { versions }] = await Promise.all([api("/api/feedback"), api("/api/prompts")]);
  const view = $("#view");
  view.innerHTML = `<div class="grid2"><div>
    <article class="card"><h2>Reviewer decisions by category and prompt version</h2>
    ${(stats as Json[]).length ? `<table><thead><tr><th>Category</th><th>Prompt</th><th class="num">Reviewed</th><th class="num">Sent unchanged</th><th class="num">Edited</th><th class="num">Rejected</th><th class="num">To expert</th><th class="num">Avg. edit size</th></tr></thead><tbody>
      ${(stats as Json[]).map((s) => `<tr><td>${esc(CAT[s.category] ?? s.category)}</td><td>${esc(s.prompt_version)}</td><td class="num">${s.reviewed}</td><td class="num">${s.sent_unchanged}</td><td class="num">${s.edited}</td><td class="num">${s.rejected}</td><td class="num">${s.escalated}</td><td class="num">${Math.round(s.avg_edit_ratio * 100)}%</td></tr>`).join("")}
    </tbody></table>` : `<div class="empty">No reviews yet. Every send, edit, rejection and escalation shows up here.</div>`}
    </article>
    <article class="card"><h2>What reviewers changed</h2>
    ${(recent as Json[]).filter((r) => !r.unchanged && r.final_body).slice(0, 12).map((r) => `<details class="lookup"><summary>${esc(CAT[r.category] ?? r.category)}: ${esc(r.decision)} by ${esc(r.reviewer)}${r.reason ? `, “${esc(r.reason)}”` : ""} (${Math.round(r.edit_ratio * 100)}% changed)</summary><div class="diff">${wordDiff(r.draft_body, r.final_body)}</div></details>`).join("") || `<div class="empty">No edits recorded yet.</div>`}
    </article></div>
    <article class="card"><h2>Drafting prompt versions</h2>
    <p style="margin-top:0;color:var(--muted)">Run <code>npm run learn</code> to turn recurring edits into a candidate prompt, then <code>npm run eval -- --prompt vN</code> to compare it with the active one on the held-out emails before switching. Switching back is a rollback.</p>
    <table><thead><tr><th>Prompt</th><th>Status</th><th>Notes</th><th></th></tr></thead><tbody>
    ${(versions as Json[]).map((v) => `<tr><td>${esc(v.name)}.${esc(v.version)}</td><td>${esc(v.status)}</td><td>${esc(v.notes ?? "")}</td><td>${v.status === "active" ? "" : `<button class="btn quiet small" data-act="${esc(v.name)}:${esc(v.version)}">Make active</button>`}</td></tr>`).join("")}
    </tbody></table></article></div>`;
  $$("[data-act]", view).forEach((b) => b.addEventListener("click", async () => {
    const who = reviewer(); if (!who) return;
    const [name, version] = b.dataset.act!.split(":");
    try { await post("/api/prompts/activate", { name, version, actor: who }); toast(`${name}.${version} is now active.`); renderLearning(); loadHealth(); } catch (e) { toast((e as Error).message, true); }
  }));
}

// ------------------------------------------------------------------ audit
async function renderAudit() {
  const { entries } = await api("/api/audit");
  $("#view").innerHTML = `<article class="card"><h2>Audit log</h2><p style="margin-top:0;color:var(--muted)">Every draft, send, review, autonomy change and prompt switch. Email addresses and phone numbers are masked.</p>
    <table><thead><tr><th>When</th><th>Who</th><th>Action</th><th>Item</th><th>Detail</th></tr></thead><tbody>
    ${(entries as Json[]).map((e) => `<tr><td>${ago(e.at)}</td><td>${esc(e.actor)}</td><td>${esc(e.action.replace(/_/g, " "))}</td><td>${esc(e.entity)} ${esc(e.entity_id ?? "")}</td><td><small>${esc(e.detail ?? "")}</small></td></tr>`).join("")}
    </tbody></table></article>`;
}

// ----------------------------------------------------------------- routing
const views: Record<string, () => Promise<void>> = { inbox: renderInbox, autonomy: renderAutonomy, learning: renderLearning, audit: renderAudit };
$$(".subnav [data-view]").forEach((b) => b.addEventListener("click", () => {
  $$(".subnav [data-view]").forEach((x) => x.setAttribute("aria-selected", String(x === b)));
  $("#view").innerHTML = "";
  views[b.dataset.view!]().catch((e) => toast((e as Error).message, true));
}));
try { ($("#reviewer") as HTMLInputElement).value = localStorage.getItem("reviewer") ?? ""; } catch { /* ignore */ }
loadHealth().then((h) => {
  if (h && !h.kb.ok) toast("The Knowledge Brain isn't reachable. Drafts that need product facts will be holding replies until it's back.", true);
  if (h?.llm_provider === "unconfigured") toast("ANTHROPIC_API_KEY is not set, so drafting is off. Add it to .env and restart.", true);
});
renderInbox().catch((e) => toast((e as Error).message, true));
setInterval(loadHealth, 15000);
