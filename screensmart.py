"""
ScreenSmart - Dual-Layer Sanctions & Risk Screening Engine
==========================================================

A production-shaped *prototype* of a real-time payment screening engine for a
fintech hackathon. ScreenSmart evaluates a payment instruction (FIAT or CRYPTO)
and returns one of three regulatory verdicts:

    * MATCH    -> BLOCK the payment (deterministic / high-confidence hit).
    * REVIEW   -> Route to a HUMAN ANALYST queue (probabilistic / partial hit).
    * NO MATCH -> RELEASE the payment (clean).

Design goals (what the judges should take away):
    1. Sub-second latency  -> a tiered architecture that does the cheapest,
       most certain work first and only escalates when needed.
    2. Uncompromised audit trail -> EVERY verdict carries a self-contained,
       human-readable explanation of *why* the decision was made, with the
       exact evidence (entity ids, scores, hop paths, tx hashes, timestamps).
    3. Explainability -> the risk score is a transparent, linear scoring model
       (not a black box). Regulators can reproduce every number by hand.

-------------------------------------------------------------------------------
TIERED ARCHITECTURE
-------------------------------------------------------------------------------
    Data Normalization Layer
        Standardize text: lowercase, strip accents/transliteration noise,
        drop corporate suffixes (Ltd/Inc/LLC...), collapse whitespace.

    Tier 1 - Deterministic Cache  (O(1) hash lookups)
        Exact normalized-name / alias hits and exact sanctioned wallet hits.
        This is where ~95% of real traffic resolves, in microseconds.

    Tier 2 - Probabilistic Matching / Lightweight ML  (fuzzy + scoring model)
        Jaro-Winkler similarity catches transliteration & typo variants
        (Sergey vs Sergei). A transparent linear scoring model blends name
        similarity with adverse-media and jurisdiction risk modifiers.

    Tier 3 - Graph Tracing  (BFS over the blockchain ledger)
        For crypto, trace up to N hops *backward* from the counterparty wallet
        to detect indirect exposure to a sanctioned address.

Run it:   python3 screensmart.py
No third-party dependencies required (pure standard library).
"""

from __future__ import annotations

import hashlib
import math
import re
import time
import uuid
import unicodedata
from collections import deque
from datetime import datetime, timezone
from enum import Enum
from typing import Dict, List, Optional

import db


# =============================================================================
# SECTION 0 - SHARED VOCABULARY (Verdicts & tunable thresholds)
# =============================================================================

class Verdict(str, Enum):
    """The three terminal regulatory outcomes. `str` mixin -> JSON-friendly."""

    MATCH = "MATCH"        # Block.   Deterministic or high-confidence hit.
    REVIEW = "REVIEW"      # Escalate. Partial / probabilistic hit.
    NO_MATCH = "NO MATCH"  # Release.  Clean.


# Severity ranking so we can pick the *worst* outcome across multiple parties.
_VERDICT_SEVERITY = {Verdict.NO_MATCH: 0, Verdict.REVIEW: 1, Verdict.MATCH: 2}


# --- Scoring thresholds (the "policy knobs" a compliance officer would tune) --
# Two separate cut-offs reflect a deliberate compliance principle:
#
#   * MATCH (auto-BLOCK) requires IDENTITY CERTAINTY -> an exact hit (Tier 1)
#     or a near-exact fuzzy score. We do NOT auto-block on a merely-similar
#     name; that is how real systems wrongly freeze innocent customers.
#   * REVIEW uses the full COMPOSITE risk (fuzzy similarity + contextual risk
#     modifiers). Risk modifiers can only escalate attention toward REVIEW,
#     never manufacture a block out of a probabilistic match.
MATCH_THRESHOLD = 0.97     # base name similarity >= this -> MATCH (near-exact).
REVIEW_THRESHOLD = 0.82    # composite >= this -> REVIEW (analyst); else NO MATCH.

# Below this similarity, the closest sanctioned entity is treated as noise:
# we do NOT attach its adverse-media flag to an unrelated party.
FUZZY_CANDIDATE_THRESHOLD = 0.70

# --- Tier 2 linear scoring-model weights (transparent "ML" heuristic) --------
# composite = name_similarity + adverse_media_modifier + jurisdiction_modifier
# (clamped to [0, 1]). Kept additive & small so the math is auditable by hand.
JURISDICTION_RISK_MODIFIER = 0.05   # bump when a party sits in a high-risk country.

# --- Tier 3 graph-tracing policy ---------------------------------------------
MAX_HOPS = 3                        # how deep to trace crypto provenance.
EXPOSURE_DECAY_PER_HOP = 0.15       # risk attenuates the further from the source.

# --- Money-size (transaction value) risk -------------------------------------
# Larger transfers carry more risk. Each tier adds a risk modifier; crossing the
# top tier triggers Enhanced Due Diligence (EDD) -> a human review even when no
# other signal fires. Bands are per-rail because the units differ (currency vs
# ETH). Tiers are listed largest-first; the first one the amount clears wins.
#   (threshold, modifier, label, triggers_edd)
FIAT_AMOUNT_TIERS = [
    (250000, 0.12, "≥ 250,000", True),
    (50000, 0.06, "≥ 50,000", False),
    (10000, 0.03, "≥ 10,000", False),
]
CRYPTO_AMOUNT_TIERS = [
    (250, 0.12, "≥ 250 ETH", True),
    (50, 0.06, "≥ 50 ETH", False),
    (10, 0.03, "≥ 10 ETH", False),
]


def _amount_risk(amount, tiers):
    """Map a transaction value to (modifier, human_label, triggers_edd)."""
    try:
        amt = float(amount or 0)
    except (TypeError, ValueError):
        amt = 0.0
    for threshold, modifier, label, edd in tiers:
        if amt >= threshold:
            return modifier, f"large transfer ({label})", edd
    return 0.0, None, False


# --- Graduated review scoring -------------------------------------------------
# A REVIEW score must reflect *how much* risk, not just *that* there is some.
# The amount tiers above are coarse (a flat modifier per band) and were only
# ever meant to nudge the composite; reusing them verbatim for the final score
# made every size-/proximity-driven review collapse onto REVIEW_THRESHOLD. The
# helpers below spread those reviews across the upper REVIEW band instead, so a
# borderline case scores near 0.82 and an extreme one approaches the cap. These
# only change the SCORE we report — never which verdict a transaction gets.
EDD_SCORE_CAP = 0.95        # amount-only (EDD) review: never enters MATCH territory.
EXPOSURE_SCORE_CAP = 0.96   # indirect graph-exposure review ceiling.
REVIEW_BAND_CEILING = 0.94  # all review scores are compressed into
                            # [REVIEW_THRESHOLD, this] so even near-match reviews
                            # keep headroom below MATCH to spread into (see below).
FLOOR_REVIEW_CAP = 0.84     # mandatory "forced" reviews (e.g. adverse-media) sit
                            # at the very bottom of the band, ordered but lowest.

# Reviews that share a scoring bucket (same hop-distance, a repeated counterparty
# name, a sub-anchor amount that contributes nothing) would otherwise show an
# identical score, so the queue stacks up on a handful of values. We add a small
# offset derived deterministically from the transaction's OWN identifying fields:
# the same transaction always gets the same score (reproducible & auditable — no
# RNG), but otherwise-identical-looking reviews spread organically across the band.
REVIEW_SCORE_SPREAD = 0.045   # width of the deterministic per-transaction spread.


def _edd_threshold(tiers):
    """Smallest transfer value that triggers Enhanced Due Diligence for a rail."""
    edd = [t[0] for t in tiers if t[3]]
    return min(edd) if edd else float("inf")


FIAT_EDD_THRESHOLD = _edd_threshold(FIAT_AMOUNT_TIERS)
CRYPTO_EDD_THRESHOLD = _edd_threshold(CRYPTO_AMOUNT_TIERS)


def _magnitude_fraction(amount, anchor):
    """How large ``amount`` is relative to ``anchor``, log-scaled to [0, 1].

    At the anchor (or below) -> 0.0; at 100x the anchor (or more) -> 1.0.
    Log-scaled because transfer values span orders of magnitude.
    """
    try:
        amt = float(amount or 0)
    except (TypeError, ValueError):
        amt = 0.0
    if anchor <= 0 or amt <= anchor:
        return 0.0
    return min(1.0, math.log10(amt / anchor) / 2.0)


def _edd_review_score(amount, edd_threshold):
    """Graduated REVIEW score for an amount-only (EDD) hit.

    Maps the transfer's magnitude onto [REVIEW_THRESHOLD, EDD_SCORE_CAP]: just
    over the EDD line scores near the floor, an extreme transfer near the cap.
    """
    frac = _magnitude_fraction(amount, edd_threshold)
    return round(REVIEW_THRESHOLD + (EDD_SCORE_CAP - REVIEW_THRESHOLD) * frac, 4)


def _floor_review_score(raw_composite):
    """Score a sub-threshold composite that is *forced* into REVIEW (e.g. by the
    adverse-media floor). Maps the true composite from
    [FUZZY_CANDIDATE_THRESHOLD, REVIEW_THRESHOLD] into
    [REVIEW_THRESHOLD, FLOOR_REVIEW_CAP] so these mandatory reviews keep their
    relative ordering instead of all collapsing onto the floor, while staying
    the lowest-priority band.
    """
    lo = FUZZY_CANDIDATE_THRESHOLD
    span = REVIEW_THRESHOLD - lo
    frac = (raw_composite - lo) / span if span > 0 else 0.0
    frac = min(1.0, max(0.0, frac))
    return round(REVIEW_THRESHOLD + (FLOOR_REVIEW_CAP - REVIEW_THRESHOLD) * frac, 4)


def _review_spread(evidence):
    """Deterministic offset in [0, REVIEW_SCORE_SPREAD) from a transaction's own
    identifying fields. A hash (not RNG) so the same transaction always maps to
    the same offset — it spreads look-alike reviews without breaking auditability.
    """
    parties = evidence.get("parties") or {}
    parts = [
        evidence.get("wallet_address"),
        evidence.get("amount"),
        evidence.get("distance_to_sanctioned"),
        (parties.get("sender") or {}).get("input_name"),
        (parties.get("recipient") or {}).get("input_name"),
    ]
    key = "|".join("" if p is None else str(p) for p in parts)
    digest = int(hashlib.sha256(key.encode()).hexdigest(), 16)
    return (digest % 100000) / 100000 * REVIEW_SCORE_SPREAD


# =============================================================================
# SECTION 1 - REFERENCE DATA (persisted in the on-disk SQLite database)
# =============================================================================
# The watchlist - sanctioned entities & names (with transliteration aliases like
# Sergey/Sergei), adverse media, high-risk jurisdictions, sanctioned wallets and
# the mock blockchain ledger - lives in `db.py` / `screensmart.db`, a real
# on-disk database (no in-memory structures). Every tier below reads from it.
# `db.init_db(...)` is called once the normalization functions are defined and
# seeds the database on first run. To inspect or replace the data, see db.py.


# =============================================================================
# SECTION 2 - DATA NORMALIZATION LAYER
# =============================================================================

# Corporate suffixes stripped during normalization (handles "Ltd"/"Inc"/...).
_CORPORATE_SUFFIXES = {
    "ltd", "limited", "inc", "incorporated", "llc", "llp", "plc", "corp",
    "corporation", "co", "company", "gmbh", "ag", "sa", "sas", "srl", "bv",
    "nv", "oy", "ab", "pte", "group", "holdings", "holding",
}


def normalize_name(name: str) -> str:
    """Standardize a name/string for matching.

    Steps (each one collapses a common source of false positives/negatives):
        1. Unicode NFKD + strip accents   -> "Pétrov" == "Petrov".
        2. Lowercase.
        3. Replace non-alphanumerics with spaces (drops punctuation/&/.).
        4. Drop trailing corporate suffixes (Ltd, Inc, LLC, GmbH ...).
        5. Collapse repeated whitespace.

    Returns a clean, lowercase token string ready for exact or fuzzy matching.
    """
    if not name:
        return ""

    # 1. Decompose accented characters and discard the combining marks.
    decomposed = unicodedata.normalize("NFKD", name)
    ascii_text = "".join(c for c in decomposed if not unicodedata.combining(c))

    # 2 + 3. Lowercase and replace any non-alphanumeric run with a single space.
    ascii_text = re.sub(r"[^a-z0-9]+", " ", ascii_text.lower()).strip()

    # 4. Remove corporate suffix tokens (e.g. "hydra holdings ltd" -> "hydra").
    tokens = [t for t in ascii_text.split() if t not in _CORPORATE_SUFFIXES]

    # 5. Re-join (whitespace already collapsed by the split/join).
    return " ".join(tokens)


def normalize_wallet(address: str) -> str:
    """Canonicalize a wallet address (trim + lowercase the hex)."""
    return (address or "").strip().lower()


# Open/seed the on-disk database now that the normalizers it needs exist.
# Idempotent: creates the schema if missing and seeds reference data once.
db.init_db(normalize_name, normalize_wallet)


# =============================================================================
# SECTION 3 - PROBABILISTIC MATCHING (Jaro-Winkler, from scratch)
# =============================================================================

def _jaro_similarity(s1: str, s2: str) -> float:
    """Raw Jaro similarity in [0, 1] (no prefix bonus)."""
    if s1 == s2:
        return 1.0
    len1, len2 = len(s1), len(s2)
    if len1 == 0 or len2 == 0:
        return 0.0

    # Two chars match only if equal and within this sliding window.
    match_distance = max(len1, len2) // 2 - 1
    if match_distance < 0:
        match_distance = 0

    s1_matches = [False] * len1
    s2_matches = [False] * len2
    matches = 0

    for i in range(len1):
        start = max(0, i - match_distance)
        end = min(i + match_distance + 1, len2)
        for j in range(start, end):
            if s2_matches[j] or s1[i] != s2[j]:
                continue
            s1_matches[i] = True
            s2_matches[j] = True
            matches += 1
            break

    if matches == 0:
        return 0.0

    # Count transpositions (matched chars that are out of order).
    transpositions = 0
    k = 0
    for i in range(len1):
        if not s1_matches[i]:
            continue
        while not s2_matches[k]:
            k += 1
        if s1[i] != s2[k]:
            transpositions += 1
        k += 1
    transpositions //= 2

    return (
        matches / len1
        + matches / len2
        + (matches - transpositions) / matches
    ) / 3.0


def _jaro_winkler(s1: str, s2: str, prefix_weight: float = 0.1) -> float:
    """Jaro-Winkler: Jaro score boosted for a shared prefix (max 4 chars).

    The prefix bonus is what makes Jaro-Winkler so good on names: people and
    transliterations usually share a leading stem (Serg-ey / Serg-ei).
    """
    jaro = _jaro_similarity(s1, s2)
    prefix = 0
    for i in range(min(len(s1), len(s2), 4)):
        if s1[i] == s2[i]:
            prefix += 1
        else:
            break
    return jaro + prefix * prefix_weight * (1 - jaro)


def calculate_similarity_score(name_a: str, name_b: str) -> float:
    """Order-tolerant fuzzy similarity between two names, in [0, 1].

    We take the *max* of:
        * Jaro-Winkler on the names as written, and
        * Jaro-Winkler on the token-sorted names.
    The second term makes "Petrov Sergey" match "Sergey Petrov" (a common
    first-name/last-name ordering difference across data sources).

    Inputs are normalized defensively so the function is safe to call directly.
    """
    a = normalize_name(name_a)
    b = normalize_name(name_b)
    if not a or not b:
        return 0.0

    direct = _jaro_winkler(a, b)
    sorted_a = " ".join(sorted(a.split()))
    sorted_b = " ".join(sorted(b.split()))
    token_sorted = _jaro_winkler(sorted_a, sorted_b)

    return round(max(direct, token_sorted), 4)


# =============================================================================
# SECTION 4 - AUDIT TRAIL (standardized verdict envelope)
# =============================================================================

def _build_verdict(
    *,
    rail: str,
    verdict: Verdict,
    risk_score: float,
    reason: str,
    tiers_evaluated: List[str],
    evidence: Dict,
    started_at: float,
) -> Dict:
    """Wrap a decision in a self-contained, regulator-grade audit payload.

    Every field here exists so an analyst (or auditor, months later) can fully
    reconstruct the decision without access to any other system.
    """
    # Spread look-alike reviews across the band. First compress the raw review
    # score into [REVIEW_THRESHOLD, REVIEW_BAND_CEILING] so even near-match
    # reviews keep headroom below the MATCH line, then add a deterministic offset
    # centered on that value (so it stacks on neither boundary). MATCH / NO MATCH
    # scores are left exactly as computed.
    if verdict == Verdict.REVIEW:
        raw = min(max(risk_score, REVIEW_THRESHOLD), MATCH_THRESHOLD)
        frac = (raw - REVIEW_THRESHOLD) / (MATCH_THRESHOLD - REVIEW_THRESHOLD)
        compressed = REVIEW_THRESHOLD + frac * (REVIEW_BAND_CEILING - REVIEW_THRESHOLD)
        centered = compressed + _review_spread(evidence) - REVIEW_SCORE_SPREAD / 2
        risk_score = min(max(centered, REVIEW_THRESHOLD), MATCH_THRESHOLD - 0.001)

    return {
        "screening_id": str(uuid.uuid4()),            # unique, traceable id.
        "timestamp_utc": datetime.now(timezone.utc).isoformat(),
        "rail": rail,                                 # "FIAT" or "CRYPTO".
        "verdict": verdict.value,                     # MATCH / REVIEW / NO MATCH.
        "action": _ACTION_FOR_VERDICT[verdict],       # plain-English next step.
        "risk_score": round(risk_score, 4),           # composite [0, 1].
        "reason": reason,                             # one-line human summary.
        "tiers_evaluated": tiers_evaluated,           # which tiers ran.
        "evidence": evidence,                         # structured proof.
        "latency_ms": round((time.perf_counter() - started_at) * 1000, 3),
    }


_ACTION_FOR_VERDICT = {
    Verdict.MATCH: "BLOCK payment and file alert.",
    Verdict.REVIEW: "HOLD payment; route to human analyst queue.",
    Verdict.NO_MATCH: "RELEASE payment.",
}


# =============================================================================
# SECTION 5 - FIAT SCREENING  (Tier 1 deterministic + Tier 2 probabilistic)
# =============================================================================

def _screen_party_against_sanctions(name: str, country: str,
                                    amount_modifier: float = 0.0) -> Dict:
    """Screen ONE party (sender or recipient) and return a scored result.

    All reference data is read from the on-disk database (db.py):
        Tier 1  -> exact normalized hit on a canonical name OR alias -> MATCH
                   (a single indexed DB lookup).
        Tier 2  -> best Jaro-Winkler similarity across all listed names, then a
                   transparent linear model adds adverse-media, this party's
                   own jurisdiction risk, and the shared money-size modifier.
    """
    normalized = normalize_name(name)
    jurisdiction_high_risk = db.is_high_risk(country.strip().lower())

    # --- Tier 1: deterministic exact-match cache (indexed DB lookup) ----------
    hit = db.lookup_exact_name(normalized)
    if hit:
        return {
            "tier": "TIER_1_DETERMINISTIC",
            "verdict": Verdict.MATCH,
            "score": 1.0,
            "matched_entity_id": hit["entity_id"],
            "matched_canonical": hit["canonical_name"],
            "matched_name": hit["matched_name"],
            "base_similarity": 1.0,
            "adverse_modifier": 0.0,
            "jurisdiction_modifier": 0.0,
            "amount_modifier": amount_modifier,
            "country": country,
            "adverse_note": None,
            "jurisdiction_high_risk": jurisdiction_high_risk,
            "detail": f"Exact match to {hit['entity_id']} "
                      f"('{hit['matched_name']}') on {hit['program']}.",
        }

    # --- Tier 2: probabilistic fuzzy match + linear risk-scoring model --------
    best = None              # best-matching name row from the DB
    best_similarity = 0.0
    for row in db.fetch_all_names():
        sim = calculate_similarity_score(name, row["name"])
        if sim > best_similarity:
            best_similarity, best = sim, row

    # Adverse-media modifier: from the matched entity AND/OR the screened name.
    adverse_modifier = 0.0
    adverse_notes: List[str] = []
    # Entity-level adverse media only counts if the entity is a genuine fuzzy
    # candidate (otherwise an unrelated clean name would inherit its flag).
    if (best and best_similarity >= FUZZY_CANDIDATE_THRESHOLD
            and best["adverse_modifier"]):
        adverse_modifier += best["adverse_modifier"]
        if best["adverse_note"]:
            adverse_notes.append(best["adverse_note"])
    # Name-level adverse media is a direct hit on the screened party itself and
    # always applies (e.g. a PEP / investigation subject not on the SDN list).
    name_adverse = db.adverse_media_by_name(normalized)
    if name_adverse:
        adverse_modifier += name_adverse["modifier"]
        if name_adverse["note"]:
            adverse_notes.append(name_adverse["note"])

    jurisdiction_modifier = JURISDICTION_RISK_MODIFIER if jurisdiction_high_risk else 0.0

    # Composite linear score (name + adverse media + jurisdiction + money size),
    # clamped to [0, 1].
    composite = min(1.0, best_similarity + adverse_modifier
                    + jurisdiction_modifier + amount_modifier)

    # Verdict policy:
    #   MATCH  <- identity certainty (near-exact name) ONLY.
    #   REVIEW <- composite risk crosses the review line.
    if best_similarity >= MATCH_THRESHOLD:
        verdict = Verdict.MATCH
    elif composite >= REVIEW_THRESHOLD:
        verdict = Verdict.REVIEW
    else:
        verdict = Verdict.NO_MATCH

    # Adverse-media floor: a party flagged in adverse media is never silently
    # released, even if it doesn't resemble any sanctioned name.
    if adverse_modifier > 0.0 and verdict == Verdict.NO_MATCH:
        verdict = Verdict.REVIEW
        composite = _floor_review_score(composite)

    return {
        "tier": "TIER_2_PROBABILISTIC",
        "verdict": verdict,
        "score": composite,
        "matched_entity_id": best["entity_id"] if best else None,
        "matched_canonical": best["canonical_name"] if best else None,
        "matched_name": best["name"] if best else "",
        "base_similarity": round(best_similarity, 4),
        "adverse_modifier": round(adverse_modifier, 4),
        "jurisdiction_modifier": jurisdiction_modifier,
        "amount_modifier": amount_modifier,
        "country": country,
        "adverse_note": " ".join(adverse_notes) if adverse_notes else None,
        "jurisdiction_high_risk": jurisdiction_high_risk,
        "detail": _describe_tier2(
            best["entity_id"] if best else None,
            best["canonical_name"] if best else None,
            best_similarity, adverse_modifier, jurisdiction_modifier,
            amount_modifier, composite),
    }


def _describe_tier2(entity_id, canonical_name, sim, adverse, jur,
                    amount_mod, composite) -> str:
    """Human-readable one-liner for a Tier-2 result (used in the audit trail)."""
    if not entity_id:
        return "No comparable sanctioned entity found."
    parts = [f"Fuzzy match score of {sim:.2f} with "
             f"{entity_id} ({canonical_name})"]
    if adverse > 0:
        parts.append(f"adverse media risk modifier applied (+{adverse:.2f})")
    if jur > 0:
        parts.append(f"high-risk jurisdiction modifier applied (+{jur:.2f})")
    if amount_mod > 0:
        parts.append(f"money-size risk modifier applied (+{amount_mod:.2f})")
    parts.append(f"composite risk {composite:.2f}")
    return " + ".join(parts) + "."


def evaluate_fiat_payment(sender: str, recipient: str, sender_country: str,
                          recipient_country: str = None, amount: float = 0.0) -> dict:
    """Evaluate a FIAT payment instruction and return a verdict + audit trail.

    The sender and recipient are each screened against the watchlist using
    *their own* jurisdiction risk, plus a shared money-size risk derived from
    ``amount``. The *worst* party outcome governs the payment.

    ``recipient_country`` defaults to ``sender_country`` if omitted (so the old
    single-country call still works). A very large transfer triggers Enhanced
    Due Diligence: a human REVIEW even when nothing else fires.
    """
    started_at = time.perf_counter()
    if recipient_country is None:
        recipient_country = sender_country

    amount_modifier, amount_label, amount_edd = _amount_risk(amount, FIAT_AMOUNT_TIERS)

    results = {
        "sender": _screen_party_against_sanctions(sender, sender_country, amount_modifier),
        "recipient": _screen_party_against_sanctions(recipient, recipient_country, amount_modifier),
    }

    # Pick the party with the most severe verdict (ties -> higher score).
    worst_role, worst = max(
        results.items(),
        key=lambda kv: (_VERDICT_SEVERITY[kv[1]["verdict"]], kv[1]["score"]),
    )
    verdict: Verdict = worst["verdict"]
    decisive_input = sender if worst_role == "sender" else recipient

    # Enhanced Due Diligence floor: a very large transfer always gets a human
    # review, even with two otherwise-clean parties ("more money, more risk").
    edd_triggered = False
    risk_score = worst["score"]
    if amount_edd and verdict == Verdict.NO_MATCH:
        verdict = Verdict.REVIEW
        risk_score = max(risk_score, _edd_review_score(amount, FIAT_EDD_THRESHOLD))
        edd_triggered = True

    # Build the human-readable reason string.
    if verdict == Verdict.NO_MATCH:
        reason = "No sanctions or adverse-media exposure detected for either party."
    elif edd_triggered:
        reason = (f"REVIEW - enhanced due diligence: {amount_label} "
                  f"({amount:,.0f}) requires a human check.")
    else:
        reason = f"{verdict.value} on {worst_role.upper()} '{decisive_input}' - {worst['detail']}"

    tiers = ["NORMALIZATION", "TIER_1_DETERMINISTIC", "TIER_2_PROBABILISTIC"]

    evidence = {
        "amount": amount,
        "amount_modifier": amount_modifier,
        "amount_risk": amount_label,
        "edd_triggered": edd_triggered,
        "decisive_party": worst_role,
        "parties": {
            role: {
                "input_name": (sender if role == "sender" else recipient),
                "normalized_name": normalize_name(
                    sender if role == "sender" else recipient),
                "country": r["country"],
                "country_high_risk": r["jurisdiction_high_risk"],
                "screening_tier": r["tier"],
                "matched_entity_id": r["matched_entity_id"],
                "matched_name": r["matched_name"] or None,
                "base_similarity": r["base_similarity"],
                "adverse_media_modifier": r["adverse_modifier"],
                "jurisdiction_modifier": r["jurisdiction_modifier"],
                "amount_modifier": r["amount_modifier"],
                "adverse_media_note": r["adverse_note"],
                "party_verdict": r["verdict"].value,
                "party_score": round(r["score"], 4),
            }
            for role, r in results.items()
        },
    }

    return _build_verdict(
        rail="FIAT",
        verdict=verdict,
        risk_score=risk_score,
        reason=reason,
        tiers_evaluated=tiers,
        evidence=evidence,
        started_at=started_at,
    )


# =============================================================================
# SECTION 6 - CRYPTO SCREENING  (Tier 1 wallet cache + Tier 3 graph tracing)
# =============================================================================

def _trace_to_sanctioned(wallet: str, max_hops: int = MAX_HOPS) -> Optional[Dict]:
    """Breadth-first search backward (over the DB ledger) for the NEAREST
    sanctioned ancestor.

    Returns the shortest-path exposure (distance + reconstructed flow path with
    tx hashes), or ``None`` if no sanctioned wallet is reachable within
    ``max_hops``. BFS guarantees we report the *closest* exposure first.
    """
    start = normalize_wallet(wallet)
    visited = {start}
    # Queue items: (current_wallet, distance, path_of_edges_from_source_side)
    queue: deque = deque([(start, 0, [])])

    while queue:
        current, distance, path = queue.popleft()
        if distance >= max_hops:
            continue
        for row in db.fetch_predecessors(current):  # "who funded this wallet?"
            sender = row["norm_sender"]
            hop = {
                "from": sender,
                "to": current,
                "amount": row["amount"],
                "tx_hash": row["tx_hash"],
            }
            new_path = [hop, *path]  # prepend -> path reads source -> target.
            listing = db.lookup_wallet(sender)
            if listing:
                return {
                    "distance": distance + 1,
                    "sanctioned_wallet": sender,
                    "listing": listing,
                    "path": new_path,
                }
            if sender not in visited:
                visited.add(sender)
                queue.append((sender, distance + 1, new_path))
    return None


def evaluate_crypto_payment(wallet_address: str, amount: float) -> dict:
    """Evaluate a CRYPTO payment by screening the counterparty wallet.

    Logic:
        Tier 1 -> wallet is itself on the sanctioned list -> MATCH (distance 0).
        Tier 3 -> graph-trace backward up to ``MAX_HOPS``; any sanctioned
                  ancestor -> REVIEW (graduated risk: closer = higher score).
        else   -> NO MATCH.

    Returns the standard audit envelope plus ``distance_to_sanctioned`` and the
    full hop-by-hop exposure path (with tx hashes) for the analyst.
    """
    started_at = time.perf_counter()
    normalized = normalize_wallet(wallet_address)
    amount_modifier, amount_label, amount_edd = _amount_risk(amount, CRYPTO_AMOUNT_TIERS)

    # --- Tier 1: deterministic sanctioned-wallet cache (indexed DB lookup) ---
    listing = db.lookup_wallet(normalized)
    if listing:
        return _build_verdict(
            rail="CRYPTO",
            verdict=Verdict.MATCH,
            risk_score=1.0,
            reason=f"MATCH - exact sanctioned wallet on list ({listing})",
            tiers_evaluated=["NORMALIZATION", "TIER_1_DETERMINISTIC"],
            evidence={
                "wallet_address": wallet_address,
                "amount": amount,
                "amount_modifier": amount_modifier,
                "amount_risk": amount_label,
                "distance_to_sanctioned": 0,
                "sanctioned_wallet": wallet_address,
                "listing": listing,
                "exposure_path": [],
            },
            started_at=started_at,
        )

    # --- Tier 3: backward graph tracing for indirect exposure ----------------
    trace = _trace_to_sanctioned(normalized, MAX_HOPS)
    if trace:
        distance = trace["distance"]
        # Risk attenuates with distance (closer = higher). Map the attenuated
        # proximity across the REVIEW band so each hop-distance gets a distinct
        # score instead of collapsing onto the floor, then let the transfer's
        # magnitude nudge it further up. Clamped to the exposure ceiling.
        proximity = max(0.0, 1.0 - EXPOSURE_DECAY_PER_HOP * distance)
        amount_frac = _magnitude_fraction(amount, CRYPTO_EDD_THRESHOLD)
        band = EXPOSURE_SCORE_CAP - REVIEW_THRESHOLD
        exposure_score = round(min(
            EXPOSURE_SCORE_CAP,
            REVIEW_THRESHOLD + band * proximity + band * 0.4 * amount_frac), 4)
        path_str = " -> ".join(
            [trace["path"][0]["from"]] + [h["to"] for h in trace["path"]]
        )
        reason = (f"REVIEW - indirect exposure: {distance} hop(s) from sanctioned "
                  f"wallet {trace['sanctioned_wallet']} ({trace['listing']})")
        if amount_modifier > 0:
            reason += f"; {amount_label} adds risk (+{amount_modifier:.2f})"
        return _build_verdict(
            rail="CRYPTO",
            verdict=Verdict.REVIEW,
            risk_score=exposure_score,
            reason=reason,
            tiers_evaluated=["NORMALIZATION", "TIER_1_DETERMINISTIC", "TIER_3_GRAPH"],
            evidence={
                "wallet_address": wallet_address,
                "amount": amount,
                "amount_modifier": amount_modifier,
                "amount_risk": amount_label,
                "distance_to_sanctioned": distance,
                "sanctioned_wallet": trace["sanctioned_wallet"],
                "listing": trace["listing"],
                "flow_path": path_str,
                "exposure_path": trace["path"],
            },
            started_at=started_at,
        )

    # --- No sanctioned exposure --------------------------------------------
    # Clean wallet, but a very large transfer still gets Enhanced Due Diligence.
    if amount_edd:
        return _build_verdict(
            rail="CRYPTO",
            verdict=Verdict.REVIEW,
            risk_score=_edd_review_score(amount, CRYPTO_EDD_THRESHOLD),
            reason=(f"REVIEW - enhanced due diligence: {amount_label} "
                    f"({amount} ETH) with no sanctioned exposure."),
            tiers_evaluated=["NORMALIZATION", "TIER_1_DETERMINISTIC", "TIER_3_GRAPH"],
            evidence={
                "wallet_address": wallet_address,
                "amount": amount,
                "amount_modifier": amount_modifier,
                "amount_risk": amount_label,
                "edd_triggered": True,
                "distance_to_sanctioned": None,
                "exposure_path": [],
            },
            started_at=started_at,
        )

    # --- Clean and below the EDD threshold -> release ------------------------
    reason = f"NO MATCH - no sanctioned exposure within {MAX_HOPS} hops."
    if amount_modifier > 0:
        reason += f" Money-size risk noted ({amount_label}, +{amount_modifier:.2f})."
    return _build_verdict(
        rail="CRYPTO",
        verdict=Verdict.NO_MATCH,
        risk_score=amount_modifier,
        reason=reason,
        tiers_evaluated=["NORMALIZATION", "TIER_1_DETERMINISTIC", "TIER_3_GRAPH"],
        evidence={
            "wallet_address": wallet_address,
            "amount": amount,
            "amount_modifier": amount_modifier,
            "amount_risk": amount_label,
            "distance_to_sanctioned": None,
            "exposure_path": [],
        },
        started_at=started_at,
    )


# =============================================================================
# SECTION 7 - PRESENTATION HELPERS (clean output for the demo / slides)
# =============================================================================

_VERDICT_BADGE = {
    "MATCH": "[ MATCH  -> BLOCK   ]",
    "REVIEW": "[ REVIEW -> ANALYST ]",
    "NO MATCH": "[ NO MATCH -> RELEASE ]",
}


def print_verdict(title: str, result: Dict) -> None:
    """Pretty-print one screening result as a tidy, slide-ready block."""
    line = "=" * 78
    print(line)
    print(f" {title}")
    print(line)
    print(f"  VERDICT       : {_VERDICT_BADGE.get(result['verdict'], result['verdict'])}")
    print(f"  RAIL          : {result['rail']}")
    print(f"  RISK SCORE    : {result['risk_score']}")
    print(f"  ACTION        : {result['action']}")
    print(f"  REASON        : {result['reason']}")
    print(f"  TIERS RUN     : {' -> '.join(result['tiers_evaluated'])}")
    print(f"  LATENCY       : {result['latency_ms']} ms")
    print(f"  SCREENING ID  : {result['screening_id']}")
    print(f"  TIMESTAMP     : {result['timestamp_utc']}")

    # Rail-specific evidence highlights (the bits a judge will look for).
    ev = result["evidence"]
    if result["rail"] == "CRYPTO":
        print(f"  DISTANCE      : {ev.get('distance_to_sanctioned')}")
        if ev.get("flow_path"):
            print(f"  FLOW PATH     : {ev['flow_path']}")
        for hop in ev.get("exposure_path", []):
            print(f"      hop  {hop['from']} -> {hop['to']}  "
                  f"({hop['amount']} ETH, tx {hop['tx_hash']})")
    else:
        for role, p in ev.get("parties", {}).items():
            tag = "  <-- decisive" if role == ev.get("decisive_party") else ""
            print(f"  {role.upper():9} : '{p['input_name']}' -> "
                  f"{p['party_verdict']} (sim={p['base_similarity']}, "
                  f"adverse={p['adverse_media_modifier']}, "
                  f"jur={p['jurisdiction_modifier']}){tag}")
            if p["matched_entity_id"]:
                print(f"              matched {p['matched_entity_id']} "
                      f"('{p['matched_name']}')")
            if p["adverse_media_note"]:
                print(f"              {p['adverse_media_note']}")
    print()


# =============================================================================
# SECTION 8 - TEST VERIFICATION SUITE
# =============================================================================
# Six distinct scenarios -> all three verdicts across BOTH rails (fiat + crypto).
# Output is intentionally clean so it can be pasted straight into a slide.

if __name__ == "__main__":
    print("\n" + "#" * 78)
    print("#  ScreenSmart - Live Verdict Demonstration")
    print("#  Dual-layer (FIAT + CRYPTO) sanctions & risk screening")
    print("#" * 78 + "\n")

    # ---- FIAT scenarios -----------------------------------------------------

    # 1. FIAT / NO MATCH: two clean parties, low-risk jurisdictions.
    print_verdict(
        "SCENARIO 1  -  FIAT  -  clean payment",
        evaluate_fiat_payment(
            sender="Acme Logistics Inc",
            recipient="John Smith",
            sender_country="United States",
            recipient_country="Canada",
            amount=4500,
        ),
    )

    # 2. FIAT / MATCH: recipient is a known transliteration alias (Sergei) of
    #    the listed "Sergey Petrov" -> Tier 1 deterministic exact-alias hit.
    print_verdict(
        "SCENARIO 2  -  FIAT  -  transliteration alias (Sergei vs Sergey)",
        evaluate_fiat_payment(
            sender="Global Trade LLC",
            recipient="Sergei Petrov",
            sender_country="United States",
            recipient_country="Russia",
            amount=8000,
        ),
    )

    # 3. FIAT / REVIEW: "Sergey Petrenko" is a real, common name that fuzzy-
    #    matches the sanctioned "Sergey Petrov". Identity is NOT certain, so we
    #    do not auto-block; adverse media + high-risk jurisdiction escalate it
    #    to a human analyst rather than freezing an innocent customer.
    print_verdict(
        "SCENARIO 3  -  FIAT  -  fuzzy near-miss + adverse media",
        evaluate_fiat_payment(
            sender="Maria Lopez",
            recipient="Sergey Petrenko",
            sender_country="Spain",
            recipient_country="Russia",
            amount=12000,
        ),
    )

    # 3b. FIAT / REVIEW: two clean parties, but a very large transfer triggers
    #     Enhanced Due Diligence ("more money, more risk").
    print_verdict(
        "SCENARIO 3b -  FIAT  -  large transfer -> enhanced due diligence",
        evaluate_fiat_payment(
            sender="Acme Logistics Inc",
            recipient="John Smith",
            sender_country="United States",
            recipient_country="United States",
            amount=500000,
        ),
    )

    # ---- CRYPTO scenarios ---------------------------------------------------

    # 4. CRYPTO / MATCH: the counterparty wallet is itself on the OFAC list.
    print_verdict(
        "SCENARIO 4  -  CRYPTO  -  exact sanctioned wallet",
        evaluate_crypto_payment("0xSANCTIONED_LAZARUS_7", amount=5.0),
    )

    # 5. CRYPTO / REVIEW: wallet sits 2 hops downstream of a sanctioned source.
    print_verdict(
        "SCENARIO 5  -  CRYPTO  -  indirect exposure (graph trace, 2 hops)",
        evaluate_crypto_payment("0xRECIPIENT_2HOP", amount=6.2),
    )

    # 6. CRYPTO / NO MATCH: wallet has no sanctioned ancestor within 3 hops.
    print_verdict(
        "SCENARIO 6  -  CRYPTO  -  clean wallet",
        evaluate_crypto_payment("0xCLEAN_MERCHANT", amount=3.0),
    )

    print("#" * 78)
    print("#  Demo complete - 3 verdicts x 2 rails shown above.")
    print("#" * 78 + "\n")
