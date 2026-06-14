/* =========================================================================
   ScreenSmart — Screening Console (front-end controller)

   Responsibilities:
     - Load /api/meta (watchlist stats, policy thresholds, demo scenarios).
     - Manage the FIAT/CRYPTO rail toggle and form state.
     - Choreograph the "scanning" tier pipeline, call the engine API, and
       render the verdict + full audit trail (fiat party cards / crypto chain).
   ========================================================================= */

(() => {
  "use strict";

  const $ = (sel, root = document) => root.querySelector(sel);
  const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];

  // App state -------------------------------------------------------------
  const state = {
    rail: "fiat",
    thresholds: { match: 0.97, review: 0.82 },
    busy: false,
  };

  // Verdict -> CSS custom-property colours (kept in sync with styles.css).
  const VERDICT_THEME = {
    "MATCH":    { color: "var(--match)",   bg: "var(--match-bg)",   border: "var(--match-border)",   cls: "match",   action: "Block payment & file alert" },
    "REVIEW":   { color: "var(--review)",  bg: "var(--review-bg)",  border: "var(--review-border)",  cls: "review",  action: "Hold — route to a human analyst" },
    "NO MATCH": { color: "var(--nomatch)", bg: "var(--nomatch-bg)", border: "var(--nomatch-border)", cls: "nomatch", action: "Release payment" },
  };

  // The tier pipeline shown during the scan, per rail.
  const PIPELINES = {
    fiat: [
      { name: "Normalize input",     tier: "Layer 0" },
      { name: "Deterministic cache", tier: "Tier 1" },
      { name: "Fuzzy match · ML",    tier: "Tier 2" },
    ],
    crypto: [
      { name: "Normalize address",   tier: "Layer 0" },
      { name: "Sanctioned wallet",   tier: "Tier 1" },
      { name: "Graph trace",         tier: "Tier 3" },
    ],
  };

  // ---------------------------------------------------------------- clock
  function tickClock() {
    const el = $("#clock");
    if (!el) return;
    const now = new Date();
    const hh = String(now.getUTCHours()).padStart(2, "0");
    const mm = String(now.getUTCMinutes()).padStart(2, "0");
    const ss = String(now.getUTCSeconds()).padStart(2, "0");
    el.textContent = `${hh}:${mm}:${ss} UTC`;
  }

  // ------------------------------------------------------------ bootstrap
  async function loadMeta() {
    try {
      const res = await fetch("/api/meta");
      const meta = await res.json();
      $("#stat-entities").textContent = meta.counts.total_entities;
      $("#stat-wallets").textContent = meta.counts.wallets;
      $("#stat-hops").textContent = meta.counts.max_hops;
      state.thresholds = meta.thresholds;
      positionMeterTicks();
      renderScenarios(meta.scenarios);
    } catch (err) {
      console.error("meta load failed", err);
    }
  }

  function positionMeterTicks() {
    const r = state.thresholds.review * 100;
    const m = state.thresholds.match * 100;
    $("#tick-review").style.left = `${r}%`;
    $("#tick-match").style.left = `${m}%`;
    $("#legend-review").style.left = `${r}%`;
    $("#legend-match").style.left = `${m}%`;
  }

  // Human-readable copy for the expected outcome (no terse chips).
  const OUTCOME_COPY = {
    "NO MATCH": { cls: "nomatch", text: "Likely cleared" },
    "MATCH":    { cls: "match",   text: "Likely blocked" },
    "REVIEW":   { cls: "review",  text: "Needs a human" },
  };
  const KIND_COPY = {
    fiat: { text: "Bank transfer",
      icon: `<svg viewBox="0 0 24 24"><path d="M3 9l9-5 9 5M4 9v9m16-9v9M3 18h18M8 11v5m4-5v5m4-5v5" stroke-linecap="round" stroke-linejoin="round"/></svg>` },
    crypto: { text: "Crypto transfer",
      icon: `<svg viewBox="0 0 24 24"><rect x="3" y="6" width="18" height="13" rx="2"/><path d="M3 10h18M16 14h2" stroke-linecap="round"/></svg>` },
  };

  function renderScenarios(scenarios) {
    const list = $("#scenario-list");
    list.innerHTML = "";
    scenarios.forEach((sc) => {
      const outcome = OUTCOME_COPY[sc.expected] || OUTCOME_COPY["REVIEW"];
      const kind = KIND_COPY[sc.rail] || KIND_COPY.fiat;
      const card = document.createElement("button");
      card.className = "example";
      card.type = "button";
      card.innerHTML = `
        <div class="example-top">
          <span class="example-kind">${kind.icon}${kind.text}</span>
          <span class="example-outcome outcome-${outcome.cls}"><i class="dot"></i>${outcome.text}</span>
        </div>
        <p class="example-title">${escapeHtml(sc.title)}</p>
        <p class="example-blurb">${escapeHtml(sc.blurb)}</p>`;
      card.addEventListener("click", () => loadScenario(sc));
      list.appendChild(card);
    });
  }

  function loadScenario(sc) {
    setRail(sc.rail);
    if (sc.rail === "fiat") {
      const f = sc.fields;
      $("#form-fiat [name=sender]").value = f.sender || "";
      $("#form-fiat [name=recipient]").value = f.recipient || "";
      $("#form-fiat [name=sender_country]").value = f.sender_country || f.country || "";
      $("#form-fiat [name=recipient_country]").value = f.recipient_country || f.country || "";
      $("#form-fiat [name=amount]").value = f.amount ?? "";
    } else {
      $("#form-crypto [name=wallet_address]").value = sc.fields.wallet_address || "";
      $("#form-crypto [name=amount]").value = sc.fields.amount ?? "";
    }
    // Auto-run for a smooth demo flow.
    execute();
  }

  // ----------------------------------------------------------- rail toggle
  function setRail(rail) {
    state.rail = rail;
    $("#rail-toggle").dataset.rail = rail;
    $$(".rail-opt").forEach((b) =>
      b.setAttribute("aria-selected", String(b.dataset.rail === rail))
    );
    $("#form-fiat").classList.toggle("is-active", rail === "fiat");
    $("#form-crypto").classList.toggle("is-active", rail === "crypto");
  }

  // --------------------------------------------------------- stage control
  function showStage(name) {
    ["idle", "scan", "result"].forEach((s) =>
      $(`#stage-${s}`).classList.toggle("is-active", s === name)
    );
  }

  // Sleep helper for the choreographed scan.
  const wait = (ms) => new Promise((r) => setTimeout(r, ms));

  // ------------------------------------------------------------- execute
  async function execute() {
    if (state.busy) return;

    // Build request from the active form.
    let endpoint, payload, valid;
    if (state.rail === "fiat") {
      const sender = $("#form-fiat [name=sender]").value.trim();
      const recipient = $("#form-fiat [name=recipient]").value.trim();
      const sender_country = $("#form-fiat [name=sender_country]").value.trim();
      const recipient_country = $("#form-fiat [name=recipient_country]").value.trim();
      const amount = $("#form-fiat [name=amount]").value;
      valid = sender || recipient;
      endpoint = "/api/screen/fiat";
      payload = { sender, recipient, sender_country, recipient_country, amount };
    } else {
      const wallet = $("#form-crypto [name=wallet_address]").value.trim();
      const amount = $("#form-crypto [name=amount]").value;
      valid = !!wallet;
      endpoint = "/api/screen/crypto";
      payload = { wallet_address: wallet, amount };
    }

    if (!valid) {
      flashInvalid();
      return;
    }

    state.busy = true;
    $("#execute").disabled = true;

    // Kick off the engine call and the scan animation in parallel.
    const fetchPromise = fetch(endpoint, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    }).then((r) => r.json());

    await runScanAnimation();
    let result;
    try {
      result = await fetchPromise;
    } catch (err) {
      result = { verdict: "REVIEW", reason: "Engine unreachable: " + err, evidence: {} };
    }

    renderResult(result);
    refreshTxCount();  // this screening was persisted to the DB
    state.busy = false;
    $("#execute").disabled = false;
  }

  function flashInvalid() {
    const exec = $("#execute");
    exec.animate(
      [{ transform: "translateX(0)" }, { transform: "translateX(-6px)" },
       { transform: "translateX(6px)" }, { transform: "translateX(0)" }],
      { duration: 280 }
    );
  }

  // Light each tier in sequence — the signature "command center" moment.
  async function runScanAnimation() {
    const steps = PIPELINES[state.rail];
    const ol = $("#pipeline");
    ol.innerHTML = "";
    steps.forEach((s, i) => {
      const li = document.createElement("li");
      li.className = "pl-step";
      li.id = `pl-${i}`;
      li.innerHTML = `
        <span class="pl-dot">${i + 1}</span>
        <span class="pl-name">${s.name}</span>
        <span class="pl-tier">${s.tier}</span>`;
      ol.appendChild(li);
    });

    showStage("scan");
    await wait(180);
    for (let i = 0; i < steps.length; i++) {
      const li = $(`#pl-${i}`);
      li.classList.add("active");
      await wait(230);
      li.classList.remove("active");
      li.classList.add("done");
      li.querySelector(".pl-dot").textContent = "✓";
    }
    await wait(220);
  }

  // -------------------------------------------------------------- render
  function renderResult(result) {
    const verdict = result.verdict || "REVIEW";
    const theme = VERDICT_THEME[verdict] || VERDICT_THEME["REVIEW"];
    const stage = $("#stage-result");

    // Theme the result via CSS custom properties.
    stage.style.setProperty("--v-color", theme.color);
    stage.style.setProperty("--v-bg", theme.bg);
    stage.style.setProperty("--v-border", theme.border);

    $("#verdict-word").textContent = verdict;
    $("#verdict-action").innerHTML = `→ <b>${theme.action}</b>`;

    // Rail tag in the panel header.
    const railTag = $("#output-rail-tag");
    railTag.hidden = false;
    railTag.textContent = (result.rail || state.rail).toUpperCase();

    // Reason.
    $("#reason").textContent = result.reason || "—";

    // Meta grid.
    renderMeta(result);

    // Animated score + meter.
    showStage("result");
    animateScore(result.risk_score || 0, theme);

    // Audit detail.
    const audit = $("#audit");
    audit.innerHTML = "";
    if ((result.rail || state.rail) === "FIAT" || state.rail === "fiat") {
      if (result.evidence && result.evidence.parties) renderFiatAudit(audit, result, verdict);
    } else {
      renderCryptoAudit(audit, result, verdict);
    }
  }

  function renderMeta(result) {
    const ev = result.evidence || {};
    const cells = [
      ["Rail", result.rail || state.rail.toUpperCase(), false],
      ["Engine latency", `${result.latency_ms ?? "—"} ms`, true],
      ["Tiers run", (result.tiers_evaluated || []).length, false],
    ];
    if (ev.amount !== undefined && ev.amount !== null && ev.amount !== "") {
      const unit = (result.rail === "CRYPTO") ? " ETH" : "";
      cells.push(["Amount", `${Number(ev.amount).toLocaleString()}${unit}`, false]);
    }
    if ((result.rail) === "CRYPTO") {
      cells.push(["Distance", ev.distance_to_sanctioned === null || ev.distance_to_sanctioned === undefined
        ? "n/a" : `${ev.distance_to_sanctioned} hop(s)`, false]);
    }
    cells.push(["Screening ID", (result.screening_id || "—").slice(0, 8), false]);
    if (result.timestamp_utc) {
      cells.push(["Timestamp", new Date(result.timestamp_utc).toISOString().slice(11, 19) + " UTC", false]);
    }
    $("#meta-grid").innerHTML = cells.map(([k, v, accent]) => `
      <div class="meta-cell">
        <div class="meta-k">${k}</div>
        <div class="meta-v ${accent ? "accent" : ""}">${v}</div>
      </div>`).join("");
  }

  function animateScore(target, theme) {
    const valEl = $("#score-value");
    const fill = $("#meter-fill");
    fill.style.background = theme.color;
    // Fill bar.
    requestAnimationFrame(() => { fill.style.width = `${Math.min(target, 1) * 100}%`; });
    // Count up the number.
    const dur = 900, start = performance.now();
    function frame(now) {
      const p = Math.min((now - start) / dur, 1);
      const eased = 1 - Math.pow(1 - p, 3);
      valEl.textContent = (target * eased).toFixed(2);
      if (p < 1) requestAnimationFrame(frame);
      else valEl.textContent = target.toFixed(2);
    }
    requestAnimationFrame(frame);
  }

  // FIAT: two party cards (sender / recipient) with the scoring breakdown.
  function renderFiatAudit(root, result, verdict) {
    const ev = result.evidence;
    const decisive = ev.decisive_party;
    const order = ["sender", "recipient"];
    const cards = order.filter((r) => ev.parties[r]).map((role) => {
      const p = ev.parties[role];
      const isDecisive = role === decisive && verdict !== "NO MATCH";
      const pTheme = VERDICT_THEME[p.party_verdict] || VERDICT_THEME["NO MATCH"];
      const matched = p.matched_entity_id
        ? `<div class="kv"><span class="kv-k">Matched</span><span class="kv-v">${p.matched_entity_id} · ${escapeHtml(p.matched_name || "")}</span></div>`
        : "";
      const note = p.adverse_media_note
        ? `<div class="party-note">⚑ ${escapeHtml(p.adverse_media_note)}</div>` : "";
      const countryTxt = escapeHtml(p.country || "—") +
        (p.country_high_risk ? ' <span class="kv-flag">high-risk</span>' : "");
      return `
        <div class="party ${isDecisive ? "decisive" : ""}"${isDecisive ? ` style="--v-color:${pTheme.color};--v-bg:${pTheme.bg}"` : ""}>
          <div class="party-head">
            <span class="party-role">${role}</span>
            ${isDecisive ? `<span class="party-flag">decisive</span>` : ""}
          </div>
          <div class="party-name">${escapeHtml(p.input_name || "—")}</div>
          <div class="party-norm"><span>norm:</span> ${escapeHtml(p.normalized_name || "—")}</div>
          <span class="party-verdict-badge vb-${pTheme.cls}">${p.party_verdict}</span>
          <div class="kv-list">
            <div class="kv"><span class="kv-k">Country</span><span class="kv-v">${countryTxt}</span></div>
            <div class="kv"><span class="kv-k">Name similarity</span><span class="kv-v">${fmt(p.base_similarity)}</span></div>
            <div class="kv"><span class="kv-k">Adverse media</span><span class="kv-v">+${fmt(p.adverse_media_modifier)}</span></div>
            <div class="kv"><span class="kv-k">Jurisdiction</span><span class="kv-v">+${fmt(p.jurisdiction_modifier)}</span></div>
            <div class="kv"><span class="kv-k">Money size</span><span class="kv-v">+${fmt(p.amount_modifier)}</span></div>
            ${matched}
          </div>
          ${note}
        </div>`;
    }).join("");

    root.innerHTML = `<p class="audit-title">Screening evidence</p><div class="parties">${cards}</div>`;
  }

  // CRYPTO: render the exposure chain (or a clean confirmation).
  function renderCryptoAudit(root, result, verdict) {
    const ev = result.evidence || {};

    if (verdict === "NO MATCH") {
      root.innerHTML = `
        <p class="audit-title">Provenance trace</p>
        <div class="clean-block">
          <span class="clean-icon"><svg viewBox="0 0 24 24" width="22" height="22"><path d="M4 12.5l5 5L20 6.5" stroke-linecap="round" stroke-linejoin="round"/></svg></span>
          <div class="clean-text">
            <strong>No sanctioned exposure found</strong>
            <span>Traced all inbound paths; no listed address within depth limit.</span>
          </div>
        </div>`;
      return;
    }

    if (verdict === "MATCH" || (ev.distance_to_sanctioned === 0)) {
      root.innerHTML = `
        <p class="audit-title">Provenance trace</p>
        <div class="chain-wrap"><div class="chain">
          <div class="chain-node is-sanctioned is-target">
            <span class="chain-pill">${escapeHtml(ev.wallet_address || "")}</span>
            <span class="chain-tag">Listed wallet · distance 0</span>
          </div>
        </div></div>
        <div class="reason" style="margin-top:14px">${escapeHtml(ev.listing || "")}</div>`;
      return;
    }

    // REVIEW: build node chain from exposure_path [{from,to,amount,tx_hash}, ...]
    const hops = ev.exposure_path || [];
    const nodes = [];
    if (hops.length) {
      nodes.push({ id: hops[0].from, sanctioned: true });
      hops.forEach((h) => nodes.push({ id: h.to, sanctioned: false }));
    }
    const lastIdx = nodes.length - 1;

    let html = "";
    nodes.forEach((n, i) => {
      const cls = [n.sanctioned ? "is-sanctioned" : "", i === lastIdx ? "is-target" : ""].join(" ").trim();
      const tag = n.sanctioned ? "Sanctioned source" : (i === lastIdx ? "Counterparty" : `Intermediary ${i}`);
      html += `
        <div class="chain-node ${cls}">
          <span class="chain-pill">${escapeHtml(n.id)}</span>
          <span class="chain-tag">${tag}</span>
        </div>`;
      if (i < lastIdx) {
        const hop = hops[i];
        html += `
          <div class="chain-link">
            <span class="chain-arrow"></span>
            <span class="chain-meta"><b>${hop.amount} ETH</b><br>${escapeHtml(hop.tx_hash)}</span>
          </div>`;
      }
    });

    root.innerHTML = `
      <p class="audit-title">Provenance trace · ${ev.distance_to_sanctioned} hop(s)</p>
      <div class="chain-wrap"><div class="chain">${html}</div></div>`;
  }

  // ============================ BENCHMARK ============================
  function showBenchStage(name) {
    ["idle", "running", "results"].forEach((s) =>
      $(`#bench-${s}`).classList.toggle("is-active", s === name)
    );
  }

  async function runBenchmark() {
    const count = parseInt($("#bench-count").value, 10) || 1000;
    const btn = $("#bench-run");
    btn.disabled = true;
    $("#bench-running-text").textContent =
      `Running ${count.toLocaleString()} transactions through the engine…`;
    showBenchStage("running");

    let data;
    try {
      const res = await fetch("/api/benchmark", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ count }),
      });
      data = await res.json();
    } catch (err) {
      $("#bench-results").innerHTML =
        `<p class="bench-caption">Benchmark failed: ${escapeHtml(String(err))}</p>`;
      showBenchStage("results");
      btn.disabled = false;
      return;
    }

    renderBenchmark(data);
    showBenchStage("results");
    refreshTxCount();  // the benchmark persisted every transaction
    btn.disabled = false;
  }

  // ===================== TRANSACTION DATABASE =====================
  async function refreshTxCount() {
    try {
      const res = await fetch("/api/transactions/count");
      const { count } = await res.json();
      $("#tx-count").textContent = Number(count).toLocaleString();
    } catch (_) { /* leave the last known value */ }
  }

  async function clearTransactions() {
    const btn = $("#tx-clear");
    btn.disabled = true;
    const original = btn.textContent;
    try {
      const res = await fetch("/api/transactions/clear", { method: "POST" });
      const { cleared } = await res.json();
      btn.textContent = `Cleared ${Number(cleared).toLocaleString()}`;
      await refreshTxCount();
      setTimeout(() => { btn.textContent = original; }, 1600);
    } catch (_) {
      btn.textContent = original;
    }
    btn.disabled = false;
  }

  const ms = (x) => `${Number(x).toFixed(3)} ms`;

  function renderBenchmark(d) {
    const lat = d.latency_ms;

    // Headline stat cards.
    const headline = [
      { lead: true, k: Math.round(d.throughput_tps).toLocaleString(), unit: "tx/sec",
        l: "Throughput (single-threaded)" },
      { k: lat.p50.toFixed(3), unit: "ms", l: "Median latency (p50)" },
      { k: lat.p99.toFixed(3), unit: "ms", l: "p99 latency" },
      { k: d.tier1_pct.toFixed(1), unit: "%", l: "Resolved in Tier-1 cache" },
    ].map((s) => `
      <div class="bench-stat ${s.lead ? "lead" : ""}">
        <div class="k">${s.k}<small>${s.unit}</small></div>
        <div class="l">${s.l}</div>
      </div>`).join("");

    // Latency percentile rows.
    const latRows = [
      ["Mean", lat.mean], ["p50 (median)", lat.p50], ["p90", lat.p90],
      ["p95", lat.p95], ["p99", lat.p99], ["Max", lat.max],
    ].map(([k, v]) => `
      <div class="lat-row"><span class="lat-k">${k}</span><span class="lat-v">${ms(v)}</span></div>`
    ).join("");

    // Latency histogram.
    const hmax = Math.max(...d.histogram.map((b) => b.count), 1);
    const hist = d.histogram.map((b) => `
      <div class="bar-row">
        <span class="bar-label mono">${escapeHtml(b.label)}</span>
        <span class="bar-track"><span class="bar-fill" style="width:${100 * b.count / hmax}%"></span></span>
        <span class="bar-val">${b.count.toLocaleString()}</span>
      </div>`).join("");

    // Verdict distribution.
    const vClass = { "MATCH": "match", "REVIEW": "review", "NO MATCH": "nomatch" };
    const verdicts = d.verdicts.map((v) => `
      <div class="bar-row">
        <span class="bar-label">${v.verdict}</span>
        <span class="bar-track"><span class="bar-fill ${vClass[v.verdict]}" style="width:${v.pct}%"></span></span>
        <span class="bar-val">${v.count.toLocaleString()} · ${v.pct}%</span>
      </div>`).join("");

    // Per-rail table.
    const railName = { fiat: "Bank transfer", crypto: "Crypto" };
    const railRows = Object.entries(d.by_rail).map(([rail, st]) => `
      <tr>
        <td>${railName[rail] || rail}</td>
        <td>${st.n.toLocaleString()}</td>
        <td>${st.mean.toFixed(3)}</td>
        <td>${st.p95.toFixed(3)}</td>
        <td>${st.p99.toFixed(3)}</td>
      </tr>`).join("");

    $("#bench-results").innerHTML = `
      <p class="bench-caption">${escapeHtml(String(d.count.toLocaleString()))} transactions · ${escapeHtml(d.platform)} · seed ${d.seed} · ${d.wall_total_ms} ms total</p>
      <div class="bench-headline">${headline}</div>
      <div class="bench-grid">
        <div class="bench-card">
          <h3>Latency per transaction</h3>
          ${latRows}
          <h3 style="margin-top:18px">Distribution</h3>
          ${hist}
        </div>
        <div class="bench-card">
          <h3>Verdict distribution</h3>
          ${verdicts}
          <h3 style="margin-top:18px">By rail (ms)</h3>
          <table class="bench-rail-table">
            <tr><th>Rail</th><th>n</th><th>Mean</th><th>p95</th><th>p99</th></tr>
            ${railRows}
          </table>
        </div>
      </div>`;
  }

  // ------------------------------------------------------------- helpers
  const fmt = (n) => (n === null || n === undefined ? "0.00" : Number(n).toFixed(2));
  function escapeHtml(s) {
    return String(s).replace(/[&<>"']/g, (c) =>
      ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  }

  // --------------------------------------------------------------- wiring
  function init() {
    $$(".rail-opt").forEach((b) =>
      b.addEventListener("click", () => setRail(b.dataset.rail))
    );
    $("#execute").addEventListener("click", execute);
    $("#bench-run").addEventListener("click", runBenchmark);
    $("#tx-clear").addEventListener("click", clearTransactions);
    document.addEventListener("keydown", (e) => {
      if (e.key === "Enter" && !e.shiftKey && (e.target.tagName === "INPUT")) {
        e.preventDefault();
        execute();
      }
    });

    setRail("fiat");
    showStage("idle");
    tickClock();
    setInterval(tickClock, 1000);
    loadMeta();
    refreshTxCount();
  }

  document.addEventListener("DOMContentLoaded", init);
})();
