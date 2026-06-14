# ScreenSmart

A dual-layer sanctions & risk screening engine for payments, with an analyst review workspace.

---

## What it does

Every payment — fiat or crypto — is checked against sanctions lists before it clears. ScreenSmart takes a payment instruction and returns one of three verdicts:

| Verdict | Action | When |
|---------|--------|------|
| **MATCH** | Block the payment | Exact or near-exact identity hit |
| **REVIEW** | Hold for a human analyst | Fuzzy or indirect risk |
| **NO MATCH** | Release the payment | Clean |

Every verdict carries a full audit trail: score, reason, evidence, timestamp, latency. Everything is persisted to an on-disk SQLite database. Pure Python standard library — no dependencies, no `pip install`.

---

## Run it

```bash
python3 server.py
```

Then open:
- **http://127.0.0.1:8000** — Screening console (submit a payment, get a verdict)
- **http://127.0.0.1:8000/analyst** — Analyst queue (review all flagged transactions)

---

## The two interfaces

### 1. Screening Console (`/`)

Submit a payment and get an instant verdict.

- Switch between **Bank transfer** (fiat) and **Crypto transfer** rails
- Fill in sender, recipient, countries, amount — or click a worked example
- See the verdict, risk score meter, and full audit trail
- Run a **performance benchmark** to measure throughput and latency on your machine

**Fiat fields:** sender name, sender country, recipient name, recipient country, amount  
**Crypto fields:** counterparty wallet address, amount (ETH)

### 2. Analyst Queue (`/analyst`)

The workspace for a human analyst clearing flagged transactions.

- See every REVIEW transaction sorted by risk score, highest first
- Header shows total pending (e.g. "5,637 pending review") and how many are high-risk
- Click any transaction to open the full detail view:
  - Verdict badge, risk score meter, reason in plain English
  - All transaction metadata (screening ID, rail, parties, country, amount, latency, timestamp)
  - **Connected suspicious transactions** — other flagged payments sharing the same entity, name pattern, or country corridor, loaded automatically
- Three action buttons: **Release**, **Block**, **Escalate**
  - Clicking a decision removes the item from the queue and moves to the next one

---

## How the engine works

Screening runs in tiers, cheapest first. Most traffic resolves in microseconds.

```
Payment instruction
        │
        ▼
┌─────────────────────┐
│  Normalization      │  Lowercase, strip accents, drop Ltd/Inc/LLC
└────────┬────────────┘
         │
         ▼
┌─────────────────────┐
│  Tier 1             │  Exact name/alias match or sanctioned wallet → MATCH
│  Deterministic      │  Single indexed DB lookup, O(1)
└────────┬────────────┘
         │ no exact hit
         ▼
┌─────────────────────┐   (fiat)
│  Tier 2             │  Jaro-Winkler fuzzy similarity across all listed names
│  Probabilistic      │  + adverse media modifier
│                     │  + jurisdiction risk modifier
│                     │  + money-size risk modifier
│                     │  → MATCH / REVIEW / NO MATCH
└────────┬────────────┘
         │
         ▼
┌─────────────────────┐   (crypto only)
│  Tier 3             │  BFS graph trace up to 3 hops backward through the ledger
│  Graph trace        │  → REVIEW if sanctioned ancestor found, NO MATCH if clean
└─────────────────────┘
```

**Key design principle:** MATCH (auto-block) requires identity certainty — exact or near-exact match. Risk modifiers (country, adverse media, amount) can only escalate a case to REVIEW, never manufacture a block from a fuzzy match. This prevents freezing innocent customers.

---

## Scoring model (Tier 2 — fiat)

The composite risk score is a transparent linear model, auditable by hand:

```
composite = name_similarity + adverse_media_modifier + jurisdiction_modifier + amount_modifier
```

Clamped to [0, 1].

| Component | Value | Trigger |
|-----------|-------|---------|
| Name similarity | 0.0 – 1.0 | Jaro-Winkler across all listed names |
| Adverse media | +0.05 – +0.18 | Entity appears in financial crime reporting |
| Jurisdiction risk | +0.05 | Sender or recipient country on high-risk list |
| Amount — large (≥ 10,000) | +0.03 | Large transfer |
| Amount — very large (≥ 50,000) | +0.06 | |
| Amount — EDD threshold (≥ 250,000) | +0.12 + EDD flag | Triggers Enhanced Due Diligence regardless of names |

**Thresholds:**
- `composite ≥ 0.97` → MATCH (near-exact identity)
- `composite ≥ 0.82` → REVIEW (analyst)
- `composite < 0.82` → NO MATCH (release)

---

## Connected transactions (analyst queue)

When an analyst opens a flagged transaction, the system automatically surfaces related suspicious transactions from the history by looking for:

1. **Same recipient name** — exact match across all stored transactions
2. **Same entity ID** — other transactions that matched the same sanctioned entity (e.g. E-001 "Sergey Petrov")
3. **Same country + rail** — other flagged payments on the same corridor

This lets the analyst see patterns that are invisible when reviewing one transaction at a time — e.g. six payments to slightly different spellings of the same name across different dates.

---

## API

| Method | Endpoint | Body | Returns |
|--------|----------|------|---------|
| GET | `/api/meta` | — | Watchlist stats, thresholds, demo scenarios |
| GET | `/api/transactions/count` | — | Number of stored transactions |
| GET | `/api/queue` | — | Top 100 REVIEW transactions by risk score + total count |
| GET | `/api/related/{screening_id}` | — | Connected suspicious transactions |
| POST | `/api/screen/fiat` | `{sender, recipient, sender_country, recipient_country, amount}` | Verdict + audit trail |
| POST | `/api/screen/crypto` | `{wallet_address, amount}` | Verdict + audit trail |
| POST | `/api/benchmark` | `{count, seed}` | Throughput & latency report |
| POST | `/api/transactions/clear` | — | Deletes all stored transactions |

---

## Files

```
screensmart.py      Engine: normalization, scoring, fiat & crypto evaluation
db.py               SQLite storage: reference data + transaction log
benchmark.py        Simulates N transactions, reports throughput + latency
server.py           Stdlib HTTP server: JSON API + static file serving

web/
  index.html        Screening console UI
  app.js            Screening console logic
  styles.css        Shared styles (both UIs)
  analyst.html      Analyst queue UI
  analyst.js        Analyst queue logic
  analyst.css       Analyst queue styles

screensmart.db      SQLite database (created and seeded on first run)
```

---

## Performance

~3,000 transactions/sec single-threaded on a modern laptop. p99 latency well under 1 ms. Crypto (hash + small graph) resolves ~70× faster than fiat (fuzzy matching). Run the benchmark for live numbers on your machine.

---

## Reference data

The mock watchlist contains:

- **Individuals:** Sergey Petrov (OFAC SDN), Aleksandr Ivanov (EU list)
- **Entities:** Hydra Holdings Ltd (OFAC SDN)
- **Sanctioned wallets:** 2 addresses (Lazarus Group, Hydra ransomware)
- **Adverse media:** Ivan Volkov (fraud network)
- **High-risk jurisdictions:** Russia, Iran, North Korea, Syria, Cuba
- **Ledger:** 6 edges of mock blockchain transaction history

In production: replace with OFAC SDN, OFSI, EU Consolidated List, UN list, and a blockchain analytics provider. The tiered architecture stays the same — only the data loaders change.

---

> Prototype built for a fintech hackathon. Not production compliance software.
