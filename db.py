"""
ScreenSmart - persistent storage layer (on-disk SQLite).

This module is the single source of truth for:
    * the watchlist REFERENCE DATA  (sanctioned entities & names, sanctioned
      wallets, adverse media, high-risk jurisdictions, the mock ledger), and
    * the LOG of every screened transaction.

Every tier of the engine (including the Tier-1 deterministic cache) reads from
this database. Storage is a real on-disk SQLite file (``screensmart.db``) -
**no in-memory database is used anywhere.** Each thread gets its own connection
and the file runs in WAL mode so the web server can read and write concurrently.

The engine passes its normalization functions into ``init_db`` so that names and
wallet addresses are stored in the same canonical form the lookups use.
"""

import os
import sqlite3
import threading
from datetime import datetime, timezone

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "screensmart.db")

# One SQLite connection per thread (sqlite3 connections are not thread-safe).
_local = threading.local()
_init_lock = threading.Lock()
_initialized = False


def _conn():
    """Return this thread's connection, opening it on first use."""
    conn = getattr(_local, "conn", None)
    if conn is None:
        conn = sqlite3.connect(DB_PATH, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        _local.conn = conn
    return conn


def _now():
    return datetime.now(timezone.utc).isoformat()


# =============================================================================
# SCHEMA
# =============================================================================

_SCHEMA = """
CREATE TABLE IF NOT EXISTS entities (
    entity_id        TEXT PRIMARY KEY,
    canonical_name   TEXT NOT NULL,
    entity_type      TEXT NOT NULL,
    program          TEXT,
    country          TEXT,
    adverse_modifier REAL DEFAULT 0,
    adverse_note     TEXT
);
CREATE TABLE IF NOT EXISTS entity_names (
    entity_id       TEXT NOT NULL,
    name            TEXT NOT NULL,
    normalized_name TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_entity_names_norm ON entity_names(normalized_name);

CREATE TABLE IF NOT EXISTS sanctioned_wallets (
    normalized_address TEXT PRIMARY KEY,
    address            TEXT NOT NULL,
    listing            TEXT
);
CREATE TABLE IF NOT EXISTS adverse_media_names (
    normalized_name TEXT PRIMARY KEY,
    modifier        REAL NOT NULL,
    note            TEXT
);
CREATE TABLE IF NOT EXISTS jurisdictions (
    name TEXT PRIMARY KEY
);
CREATE TABLE IF NOT EXISTS ledger (
    sender       TEXT,
    receiver     TEXT,
    norm_sender  TEXT,
    norm_receiver TEXT,
    amount       REAL,
    tx_hash      TEXT
);
CREATE INDEX IF NOT EXISTS idx_ledger_receiver ON ledger(norm_receiver);

CREATE TABLE IF NOT EXISTS transactions (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    screening_id  TEXT,
    rail          TEXT,
    verdict       TEXT,
    risk_score    REAL,
    reason        TEXT,
    sender        TEXT,
    recipient     TEXT,
    country       TEXT,
    wallet_address TEXT,
    amount        REAL,
    latency_ms    REAL,
    source        TEXT,
    timestamp_utc TEXT,
    created_at    TEXT
);
"""


# =============================================================================
# SEED DATA  (loaded into the DB once, on first run)
# =============================================================================
# (entity_id, canonical, type, program, country, [aliases], adverse_mod, adverse_note)
_SEED_ENTITIES = [
    ("E-001", "Sergey Petrov", "INDIVIDUAL", "OFAC SDN (2023)", "Russia",
     ["Sergei Petrov", "Sergej Petrov", "Serguei Petrov"], 0.05,
     "Adverse media: named in 2023 OCCRP money-laundering investigation."),
    ("E-002", "Aleksandr Ivanov", "INDIVIDUAL", "EU Consolidated List (2022)", "Russia",
     ["Alexander Ivanov", "Alexandr Ivanoff"], 0.0, None),
    ("C-001", "Hydra Holdings Ltd", "ENTITY", "OFAC SDN (2024)", "Cyprus",
     ["Hydra Holdings LLC", "Hydra Holding Group"], 0.0, None),
]
_SEED_WALLETS = [
    ("0xSANCTIONED_HYDRA_01", "OFAC SDN (2024) - ransomware proceeds wallet."),
    ("0xSANCTIONED_LAZARUS_7", "OFAC SDN (2022) - DPRK Lazarus Group wallet."),
]
_SEED_ADVERSE_NAMES = [
    ("ivan volkov", 0.18,
     "Adverse media: investigative report links subject to fraud network."),
]
_SEED_JURISDICTIONS = ["russia", "iran", "north korea", "syria", "cuba"]
_SEED_LEDGER = [
    ("0xSANCTIONED_HYDRA_01", "0xLAYER_1", 12.5, "0xtx_a1"),
    ("0xLAYER_1", "0xRECIPIENT_2HOP", 6.2, "0xtx_b2"),
    ("0xSANCTIONED_HYDRA_01", "0xMIXER_TORNADO", 40.0, "0xtx_c3"),
    ("0xMIXER_TORNADO", "0xINTERMEDIARY_A", 38.0, "0xtx_d4"),
    ("0xINTERMEDIARY_A", "0xCUSTOMER_X", 37.0, "0xtx_e5"),
    ("0xCLEAN_PAYROLL", "0xCLEAN_MERCHANT", 3.0, "0xtx_f6"),
]


def init_db(normalize_name, normalize_wallet):
    """Create the schema (idempotent) and seed reference data if empty.

    ``normalize_name`` / ``normalize_wallet`` are the engine's normalizers, so
    stored canonical forms match what the lookups will search for.
    """
    global _initialized
    with _init_lock:
        conn = _conn()
        conn.executescript(_SCHEMA)
        conn.commit()
        if conn.execute("SELECT COUNT(*) FROM entities").fetchone()[0] == 0:
            for eid, canon, etype, prog, country, aliases, adv_mod, adv_note in _SEED_ENTITIES:
                conn.execute(
                    "INSERT INTO entities VALUES (?,?,?,?,?,?,?)",
                    (eid, canon, etype, prog, country, adv_mod, adv_note))
                for nm in [canon, *aliases]:
                    conn.execute(
                        "INSERT INTO entity_names(entity_id, name, normalized_name) "
                        "VALUES (?,?,?)", (eid, nm, normalize_name(nm)))
            for addr, listing in _SEED_WALLETS:
                conn.execute(
                    "INSERT INTO sanctioned_wallets(normalized_address, address, listing) "
                    "VALUES (?,?,?)", (normalize_wallet(addr), addr, listing))
            for nm, mod, note in _SEED_ADVERSE_NAMES:
                conn.execute("INSERT INTO adverse_media_names VALUES (?,?,?)",
                             (nm, mod, note))
            for j in _SEED_JURISDICTIONS:
                conn.execute("INSERT INTO jurisdictions VALUES (?)", (j,))
            for s, r, amt, tx in _SEED_LEDGER:
                conn.execute(
                    "INSERT INTO ledger VALUES (?,?,?,?,?,?)",
                    (s, r, normalize_wallet(s), normalize_wallet(r), amt, tx))
            conn.commit()
        _initialized = True


# =============================================================================
# REFERENCE-DATA READS  (used by the engine's tiers)
# =============================================================================

def lookup_exact_name(normalized_name):
    """Tier 1: exact normalized name/alias hit -> a row, or None."""
    if not normalized_name:
        return None
    return _conn().execute(
        "SELECT en.entity_id, en.name AS matched_name, e.canonical_name, e.program "
        "FROM entity_names en JOIN entities e ON e.entity_id = en.entity_id "
        "WHERE en.normalized_name = ? LIMIT 1", (normalized_name,)).fetchone()


def fetch_all_names():
    """Tier 2: every listed name + its entity's metadata (for fuzzy matching)."""
    return _conn().execute(
        "SELECT en.entity_id, en.name, e.canonical_name, "
        "e.adverse_modifier, e.adverse_note "
        "FROM entity_names en JOIN entities e ON e.entity_id = en.entity_id"
    ).fetchall()


def lookup_wallet(normalized_address):
    """Tier 1 (crypto): listing string if the wallet is sanctioned, else None."""
    row = _conn().execute(
        "SELECT listing FROM sanctioned_wallets WHERE normalized_address = ?",
        (normalized_address,)).fetchone()
    return row["listing"] if row else None


def adverse_media_by_name(normalized_name):
    """Adverse-media hit on the screened name itself -> row, or None."""
    return _conn().execute(
        "SELECT modifier, note FROM adverse_media_names WHERE normalized_name = ?",
        (normalized_name,)).fetchone()


def is_high_risk(country_lower):
    """True if the (lowercased) jurisdiction is on the high-risk list."""
    return _conn().execute(
        "SELECT 1 FROM jurisdictions WHERE name = ? LIMIT 1",
        (country_lower,)).fetchone() is not None


def fetch_predecessors(norm_receiver):
    """Tier 3: who funded this wallet? -> rows of (norm_sender, amount, tx_hash)."""
    return _conn().execute(
        "SELECT norm_sender, amount, tx_hash FROM ledger WHERE norm_receiver = ?",
        (norm_receiver,)).fetchall()


# =============================================================================
# AGGREGATE READS  (for the UI header and the benchmark traffic generator)
# =============================================================================

def counts():
    c = _conn()
    ind = c.execute("SELECT COUNT(*) FROM entities WHERE entity_type='INDIVIDUAL'").fetchone()[0]
    ent = c.execute("SELECT COUNT(*) FROM entities WHERE entity_type='ENTITY'").fetchone()[0]
    wal = c.execute("SELECT COUNT(*) FROM sanctioned_wallets").fetchone()[0]
    led = c.execute("SELECT COUNT(*) FROM ledger").fetchone()[0]
    return {"individuals": ind, "entities": ent, "total_entities": ind + ent,
            "wallets": wal, "ledger_edges": led}


def all_sanctioned_names():
    return [r["name"] for r in _conn().execute("SELECT name FROM entity_names").fetchall()]


def all_sanctioned_wallets():
    return [r["address"] for r in
            _conn().execute("SELECT address FROM sanctioned_wallets").fetchall()]


def all_jurisdictions():
    return [r["name"] for r in _conn().execute("SELECT name FROM jurisdictions").fetchall()]


def ledger_receivers():
    """Distinct ledger receivers that are NOT themselves sanctioned.

    Used as crypto inputs in the benchmark - a mix that the engine resolves to
    REVIEW (traceable to a sanctioned source) or NO MATCH (clean).
    """
    out = []
    for r in _conn().execute(
            "SELECT DISTINCT receiver, norm_receiver FROM ledger").fetchall():
        if not lookup_wallet(r["norm_receiver"]):
            out.append(r["receiver"])
    return out


# =============================================================================
# TRANSACTION LOG  (write / count / clear)
# =============================================================================

_TX_COLS = ("screening_id", "rail", "verdict", "risk_score", "reason", "sender",
            "recipient", "country", "wallet_address", "amount", "latency_ms",
            "source", "timestamp_utc", "created_at")
_TX_INSERT = (f"INSERT INTO transactions ({', '.join(_TX_COLS)}) "
              f"VALUES ({', '.join(['?'] * len(_TX_COLS))})")


def _row_from_result(result, source):
    """Flatten an engine verdict payload into a transactions-table row."""
    ev = result.get("evidence", {}) or {}
    rail = result.get("rail")
    if rail == "FIAT":
        parties = ev.get("parties", {}) or {}
        sender = (parties.get("sender") or {}).get("input_name")
        recipient = (parties.get("recipient") or {}).get("input_name")
        country = ev.get("country")
        wallet, amount = None, None
    else:
        sender = recipient = country = None
        wallet = ev.get("wallet_address")
        amount = ev.get("amount")
    return (result.get("screening_id"), rail, result.get("verdict"),
            result.get("risk_score"), result.get("reason"), sender, recipient,
            country, wallet, amount, result.get("latency_ms"), source,
            result.get("timestamp_utc"), _now())


def insert_transaction(result, source="ui"):
    """Persist a single screened transaction."""
    conn = _conn()
    conn.execute(_TX_INSERT, _row_from_result(result, source))
    conn.commit()


def insert_transactions(results, source="benchmark"):
    """Persist many screened transactions in one batched write."""
    conn = _conn()
    conn.executemany(_TX_INSERT, [_row_from_result(r, source) for r in results])
    conn.commit()
    return len(results)


def count_transactions():
    return _conn().execute("SELECT COUNT(*) FROM transactions").fetchone()[0]


def get_transaction_by_id(screening_id):
    """Fetch a single transaction by screening_id."""
    return _conn().execute(
        "SELECT * FROM transactions WHERE screening_id = ? LIMIT 1",
        (screening_id,)).fetchone()


def get_review_queue(limit=100):
    """Most recent REVIEW transactions, newest first."""
    total = _conn().execute(
        "SELECT COUNT(*) FROM transactions WHERE verdict = 'REVIEW'"
    ).fetchone()[0]
    rows = _conn().execute(
        "SELECT * FROM transactions WHERE verdict = 'REVIEW' "
        "ORDER BY created_at DESC, risk_score DESC LIMIT ?", (limit,)
    ).fetchall()
    return {"total": total, "rows": [dict(r) for r in rows]}


def get_related_transactions(screening_id, country, recipient, sender, rail, reason):
    """Find suspicious transactions connected to the given one.

    Matches on: same country, same recipient name, same entity mentioned in reason.
    Returns REVIEW + MATCH verdicts only, excluding the source transaction.
    """
    conn = _conn()
    seen = set()
    results = []

    def _add(rows):
        for r in rows:
            sid = r["screening_id"]
            if sid and sid not in seen and sid != screening_id:
                seen.add(sid)
                results.append(r)

    # 1. Same recipient name (exact)
    if recipient:
        _add(conn.execute(
            "SELECT * FROM transactions WHERE recipient = ? AND screening_id != ? "
            "AND verdict IN ('REVIEW','MATCH') ORDER BY risk_score DESC LIMIT 20",
            (recipient, screening_id or "")).fetchall())

    # 2. Same entity ID in reason string (e.g. "E-001", "C-001")
    if reason:
        import re
        entity_ids = re.findall(r'\b[EC]-\d+\b', reason)
        for eid in entity_ids:
            _add(conn.execute(
                "SELECT * FROM transactions WHERE reason LIKE ? AND screening_id != ? "
                "AND verdict IN ('REVIEW','MATCH') ORDER BY risk_score DESC LIMIT 20",
                (f'%{eid}%', screening_id or "")).fetchall())

    # 3. Same country + same rail
    if country and rail:
        _add(conn.execute(
            "SELECT * FROM transactions WHERE country = ? AND rail = ? "
            "AND screening_id != ? AND verdict IN ('REVIEW','MATCH') "
            "ORDER BY risk_score DESC LIMIT 20",
            (country, rail, screening_id or "")).fetchall())

    return results[:30]


def clear_transactions():
    """Delete every stored transaction; returns how many were removed."""
    conn = _conn()
    removed = count_transactions()
    conn.execute("DELETE FROM transactions")
    conn.commit()
    return removed
