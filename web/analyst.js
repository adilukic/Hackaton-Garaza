'use strict';

// ── Constants (mirror engine thresholds) ──────────────────
const MATCH_T  = 0.97;
const REVIEW_T = 0.82;

// ── State ─────────────────────────────────────────────────
let queue       = [];
let activeId    = null;

// ── DOM refs ──────────────────────────────────────────────
const queueList      = document.getElementById('queue-list');
const queueEmpty     = document.getElementById('queue-empty');
const statTotal      = document.getElementById('stat-total');
const statHigh       = document.getElementById('stat-high');
const analystIdle    = document.getElementById('analyst-idle');
const analystDetail  = document.getElementById('analyst-detail');
const detailName     = document.getElementById('detail-name');
const detailSub      = document.getElementById('detail-sub');
const detailScore    = document.getElementById('detail-score');
const detailMeterFill= document.getElementById('detail-meter-fill');
const detailReason   = document.getElementById('detail-reason');
const detailGrid     = document.getElementById('detail-grid');
const detailVerdictBadge = document.getElementById('detail-verdict-badge');
const connectedList  = document.getElementById('connected-list');
const connectedLoading = document.getElementById('connected-loading');
const connectedEmpty = document.getElementById('connected-empty');
const connectedCount = document.getElementById('connected-count');
const clockEl        = document.getElementById('clock');

// ── Clock ─────────────────────────────────────────────────
function tickClock() {
  clockEl.textContent = new Date().toUTCString().slice(17, 25) + ' UTC';
}
tickClock();
setInterval(tickClock, 1000);

// ── Risk level helper ─────────────────────────────────────
function riskLevel(score) {
  if (score >= 0.90) return 'high';
  if (score >= 0.84) return 'med';
  return 'low';
}

// ── Verdict badge HTML ────────────────────────────────────
function verdictBadgeHTML(verdict) {
  const cfg = {
    'MATCH':    { cls: 'match',    label: 'MATCH'    },
    'REVIEW':   { cls: 'review',   label: 'REVIEW'   },
    'NO MATCH': { cls: 'no-match', label: 'NO MATCH' },
  };
  const c = cfg[verdict] || cfg['REVIEW'];
  return `<div class="verdict-badge ${c.cls}">
    <span class="verdict-dot"></span>
    <span>${c.label}</span>
  </div>`;
}

// ── Format amount ─────────────────────────────────────────
function fmtAmount(tx) {
  if (tx.rail === 'FIAT' && tx.amount)   return `$${Number(tx.amount).toLocaleString()}`;
  if (tx.rail === 'CRYPTO' && tx.amount) return `${tx.amount} ETH`;
  return '—';
}

// ── Load queue from API ───────────────────────────────────
async function loadQueue() {
  const data = await fetch('/api/queue').then(r => r.json()).catch(() => ({ total: 0, rows: [] }));
  queue = data.rows || data;
  const total = data.total || queue.length;
  queue.forEach(tx => knownIds.add(tx.screening_id));

  statTotal.textContent = total.toLocaleString();
  statHigh.textContent  = queue.filter(t => riskLevel(t.risk_score) === 'high').length;

  queueList.innerHTML = '';

  if (queue.length === 0) {
    queueEmpty.hidden = false;
    return;
  }
  queueEmpty.hidden = true;

  queue.forEach(tx => {
    const level = riskLevel(tx.risk_score);
    const name  = tx.recipient || tx.wallet_address || tx.sender || '—';
    const sub   = [tx.country, tx.rail, fmtAmount(tx)].filter(Boolean).join(' · ');

    const li = document.createElement('li');
    li.className = `queue-item risk-${level}`;
    li.dataset.id = tx.screening_id;
    li.innerHTML = `
      <span class="q-risk-dot"></span>
      <div class="q-body">
        <div class="q-name">${esc(name)}</div>
        <div class="q-sub">${esc(sub)}</div>
      </div>
      <span class="q-score">${tx.risk_score.toFixed(2)}</span>`;
    li.addEventListener('click', () => selectTransaction(tx));
    queueList.appendChild(li);
  });

  // Re-highlight active item if still in queue
  if (activeId) highlightActive(activeId);
}

// ── Select & render a transaction ────────────────────────
async function selectTransaction(tx) {
  activeId = tx.screening_id;
  highlightActive(activeId);

  // Show detail panel
  analystIdle.hidden   = true;
  analystDetail.hidden = false;

  // Header
  const name = tx.recipient || tx.wallet_address || tx.sender || '—';
  detailName.textContent = name;
  detailSub.textContent  = [tx.rail, tx.country, fmtAmount(tx)].filter(Boolean).join(' · ');
  detailVerdictBadge.innerHTML = verdictBadgeHTML(tx.verdict);

  // Score + meter
  const score = tx.risk_score;
  detailScore.textContent = score.toFixed(4);
  detailMeterFill.style.width = `${Math.min(score * 100, 100)}%`;
  detailMeterFill.className = `meter-fill ${tx.verdict === 'MATCH' ? 'match' : tx.verdict === 'REVIEW' ? 'review' : ''}`;

  // Meter ticks
  document.getElementById('detail-tick-review').style.left = `${REVIEW_T * 100}%`;
  document.getElementById('detail-tick-match').style.left  = `${MATCH_T  * 100}%`;

  // Reason
  detailReason.textContent = tx.reason || '—';

  // Info grid
  const cells = [
    { label: 'Screening ID', value: tx.screening_id, mono: true },
    { label: 'Rail',         value: tx.rail },
    { label: 'Verdict',      value: tx.verdict },
    { label: 'Sender',       value: tx.sender || '—' },
    { label: 'Recipient',    value: tx.recipient || '—' },
    { label: 'Country',      value: tx.country || '—' },
    { label: 'Amount',       value: fmtAmount(tx) },
    { label: 'Latency',      value: tx.latency_ms ? `${tx.latency_ms} ms` : '—', mono: true },
    { label: 'Screened at',  value: tx.timestamp_utc ? tx.timestamp_utc.slice(0, 19).replace('T', ' ') + ' UTC' : '—' },
  ];
  detailGrid.innerHTML = cells.map(c => `
    <div class="detail-cell">
      <div class="detail-cell-label">${esc(c.label)}</div>
      <div class="detail-cell-value${c.mono ? ' mono' : ''}">${esc(String(c.value))}</div>
    </div>`).join('');

  // Action buttons
  document.getElementById('btn-release').onclick  = () => decide(tx, 'release');
  document.getElementById('btn-block').onclick    = () => decide(tx, 'block');

  // Load connected
  loadConnected(tx);
}

// ── Load connected transactions ───────────────────────────
async function loadConnected(tx) {
  connectedList.innerHTML = '';
  connectedEmpty.hidden   = true;
  connectedCount.classList.remove('visible');
  connectedLoading.hidden = false;

  const related = await fetch(`/api/related/${tx.screening_id}`)
    .then(r => r.json()).catch(() => []);

  connectedLoading.hidden = true;

  if (!related.length) {
    connectedEmpty.hidden = false;
    return;
  }

  connectedCount.textContent = related.length;
  connectedCount.classList.add('visible');

  related.forEach(r => {
    const level = riskLevel(r.risk_score);
    const name  = r.recipient || r.wallet_address || r.sender || '—';
    const sub   = [r.country, r.rail, r.timestamp_utc ? r.timestamp_utc.slice(0,10) : ''].filter(Boolean).join(' · ');
    const tagCls = r.verdict === 'MATCH' ? 'match' : 'review';

    const li = document.createElement('li');
    li.className = `connected-item risk-${level}`;
    li.innerHTML = `
      <span class="q-risk-dot"></span>
      <div class="conn-body">
        <div class="conn-name">${esc(name)}</div>
        <div class="conn-sub">${esc(sub)}</div>
      </div>
      <span class="conn-tag ${tagCls}">${esc(r.verdict)}</span>
      <span class="conn-score">${r.risk_score.toFixed(2)}</span>`;
    li.addEventListener('click', () => selectTransaction(r));
    connectedList.appendChild(li);
  });
}

// ── Decision handler ──────────────────────────────────────
function decide(tx, action) {
  const labels = { release: 'Released', block: 'Blocked' };
  showToast(`${labels[action]}: ${tx.recipient || tx.wallet_address || tx.sender || '—'}`, action);

  // Remove from queue locally
  queue = queue.filter(t => t.screening_id !== tx.screening_id);
  const li = queueList.querySelector(`[data-id="${tx.screening_id}"]`);
  if (li) li.remove();

  statTotal.textContent = queue.length;
  statHigh.textContent  = queue.filter(t => riskLevel(t.risk_score) === 'high').length;

  if (!queue.length) queueEmpty.hidden = false;

  // Show next in queue or idle
  const next = queue[0];
  if (next) {
    selectTransaction(next);
  } else {
    activeId = null;
    analystIdle.hidden   = false;
    analystDetail.hidden = true;
  }
}

// ── Toast ─────────────────────────────────────────────────
function showToast(msg, cls) {
  const t = document.createElement('div');
  t.className = `decision-toast ${cls}`;
  t.textContent = msg;
  document.body.appendChild(t);
  setTimeout(() => t.remove(), 3000);
}

// ── Highlight active queue item ───────────────────────────
function highlightActive(id) {
  document.querySelectorAll('.queue-item').forEach(el => {
    el.classList.toggle('active', el.dataset.id === id);
  });
}

// ── Escape HTML ───────────────────────────────────────────
function esc(s) {
  return String(s ?? '')
    .replace(/&/g, '&amp;').replace(/</g, '&lt;')
    .replace(/>/g, '&gt;').replace(/"/g, '&quot;');
}

// ── Auto-poll for new transactions ────────────────────────
let knownIds = new Set();

async function pollQueue() {
  const data = await fetch('/api/queue').then(r => r.json()).catch(() => null);
  if (!data) return;

  const rows = data.rows || data;
  const total = data.total || rows.length;

  // Find truly new items (not yet in our known set)
  const newItems = rows.filter(tx => !knownIds.has(tx.screening_id));
  if (!newItems.length) return;

  // Update counts
  rows.forEach(tx => knownIds.add(tx.screening_id));
  queue = rows;
  statTotal.textContent = total.toLocaleString();
  statHigh.textContent  = rows.filter(t => riskLevel(t.risk_score) === 'high').length;
  queueEmpty.hidden = true;

  // Prepend new items at the top with a highlight flash
  newItems.reverse().forEach(tx => {
    const level = riskLevel(tx.risk_score);
    const name  = tx.recipient || tx.wallet_address || tx.sender || '—';
    const sub   = [tx.country, tx.rail, fmtAmount(tx)].filter(Boolean).join(' · ');

    const li = document.createElement('li');
    li.className = `queue-item risk-${level} queue-item-new`;
    li.dataset.id = tx.screening_id;
    li.innerHTML = `
      <span class="q-risk-dot"></span>
      <div class="q-body">
        <div class="q-name">${esc(name)}</div>
        <div class="q-sub">${esc(sub)}</div>
      </div>
      <span class="q-score">${tx.risk_score.toFixed(2)}</span>`;
    li.addEventListener('click', () => selectTransaction(tx));
    queueList.prepend(li);
    // Remove flash class after animation
    setTimeout(() => li.classList.remove('queue-item-new'), 1200);
  });

  if (activeId) highlightActive(activeId);
}

// ── Init ──────────────────────────────────────────────────
document.getElementById('refresh-btn').addEventListener('click', loadQueue);
loadQueue().then(() => {
  setInterval(pollQueue, 4000);
});
