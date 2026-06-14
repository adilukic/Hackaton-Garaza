"""
ScreenSmart - throughput & latency benchmark
============================================

Generates a realistic mix of synthetic payment instructions (fiat + crypto),
runs every one through the screening engine, and reports:

    * end-to-end throughput (transactions / second)
    * latency percentiles  (mean, p50, p90, p95, p99, max)
    * verdict distribution  (MATCH / REVIEW / NO MATCH)
    * how many resolved in the Tier-1 deterministic cache (the fast path)
    * a small latency histogram

The traffic mix mirrors reality: the vast majority of payments are clean, with
a minority of exact hits, fuzzy near-misses, and crypto exposure cases that
exercise the deeper (Tier 2 / Tier 3) code paths.

Run:
    python3 benchmark.py
    python3 benchmark.py --count 5000 --seed 7 --json results.json
"""

import argparse
import json
import math
import platform
import random
import statistics
import string
import sys
import time
from collections import Counter

import db
import screensmart as engine


# =============================================================================
# SYNTHETIC TRANSACTION GENERATION
# =============================================================================
# Watchlist inputs (sanctioned names, wallets, jurisdictions, ledger wallets)
# are pulled from the on-disk database at generate time - see generate_transactions.

# Clean (non-listed) name pools — deliberately disjoint from the watchlist.
_FIRST = ["James", "Maria", "Wei", "Priya", "Carlos", "Anna", "David", "Fatima",
          "Liam", "Sofia", "Noah", "Yuki", "Omar", "Elena", "Lucas", "Aisha",
          "Diego", "Ingrid", "Hassan", "Mei", "Grace", "Tomas", "Leila", "Kofi"]
_LAST = ["Smith", "Johnson", "Garcia", "Nguyen", "Kowalski", "Rossi", "Mueller",
         "Andersson", "Okafor", "Tanaka", "Silva", "Cohen", "Patel", "Ferraro",
         "Novak", "Haddad", "Larsen", "Costa", "Ito", "Khan", "Bauer", "Mwangi"]
_COMPANIES = ["Acme Logistics Inc", "Northwind Trading Ltd", "Blue Harbor LLC",
              "Meridian Exports", "Sunrise Freight Co", "Cedar Capital Partners",
              "Atlas Manufacturing", "Vega Imports GmbH", "Orion Shipping Ltd",
              "Delta Foods SA", "Lighthouse Retail", "Granite Holdings Inc"]
_COUNTRIES_LOW = ["United States", "Germany", "Japan", "Canada", "Brazil",
                  "France", "Australia", "Sweden", "Singapore", "Netherlands"]


def _fiat_amount():
    """Transfer value: mostly modest, occasionally large enough to trip EDD."""
    r = random.random()
    if r < 0.85:
        return random.randint(500, 9000)         # below the risk tiers
    if r < 0.96:
        return random.randint(10000, 90000)      # elevated / large
    return random.randint(250000, 2000000)       # enhanced due diligence


def _crypto_amount():
    """ETH value: mostly small, occasionally large enough to trip EDD."""
    r = random.random()
    if r < 0.85:
        return round(random.uniform(0.01, 9), 3)
    if r < 0.96:
        return round(random.uniform(10, 200), 3)
    return round(random.uniform(250, 1500), 3)


def _perturb(name):
    """Introduce a small typo/transliteration-style edit (drives fuzzy hits)."""
    chars = list(name)
    letters = [i for i, c in enumerate(chars) if c.isalpha()]
    if not letters:
        return name
    op = random.choice(["sub", "swap", "drop"])
    i = random.choice(letters)
    if op == "sub":
        chars[i] = random.choice(string.ascii_lowercase)
    elif op == "swap" and i + 1 < len(chars):
        chars[i], chars[i + 1] = chars[i + 1], chars[i]
    elif op == "drop" and len(letters) > 4:
        del chars[i]
    return "".join(chars)


def _random_wallet():
    """A plausible, unlisted hex wallet address."""
    return "0x" + "".join(random.choices("0123456789abcdef", k=24))


def _clean_party():
    """Either a person or a company (clean)."""
    if random.random() < 0.45:
        return random.choice(_COMPANIES)
    return f"{random.choice(_FIRST)} {random.choice(_LAST)}"


def generate_transactions(count):
    """Build `count` transactions with a realistic, labelled traffic mix.

    Watchlist inputs are read from the on-disk database. Each item is
    {"rail": "fiat"|"crypto", "expected_class": str, "args": (...)} where args
    match the engine's evaluate_* signatures.
    """
    names = db.all_sanctioned_names()
    wallets = db.all_sanctioned_wallets()
    ledger_wallets = db.ledger_receivers() or [_random_wallet()]
    high = [c.title() for c in db.all_jurisdictions()]

    def a_country():
        return random.choice(_COUNTRIES_LOW if random.random() < 0.8 else high)

    # Proportions (sum = 1.0). Clean traffic dominates, as in production.
    mix = [
        ("fiat_clean",    0.50),
        ("fiat_exact",    0.06),
        ("fiat_fuzzy",    0.12),
        ("crypto_clean",  0.16),
        ("crypto_exact",  0.05),
        ("crypto_ledger", 0.11),
    ]
    categories = random.choices(
        [c for c, _ in mix], weights=[w for _, w in mix], k=count
    )

    txs = []
    for cat in categories:
        if cat == "fiat_clean":
            txs.append({"rail": "fiat", "expected_class": cat,
                        "args": (_clean_party(), _clean_party(),
                                 a_country(), a_country(), _fiat_amount())})
        elif cat == "fiat_exact":
            txs.append({"rail": "fiat", "expected_class": cat,
                        "args": (_clean_party(), random.choice(names),
                                 a_country(), random.choice(high), _fiat_amount())})
        elif cat == "fiat_fuzzy":
            txs.append({"rail": "fiat", "expected_class": cat,
                        "args": (_clean_party(), _perturb(random.choice(names)),
                                 a_country(), random.choice(high), _fiat_amount())})
        elif cat == "crypto_clean":
            txs.append({"rail": "crypto", "expected_class": cat,
                        "args": (_random_wallet(), _crypto_amount())})
        elif cat == "crypto_exact":
            txs.append({"rail": "crypto", "expected_class": cat,
                        "args": (random.choice(wallets), _crypto_amount())})
        else:  # crypto_ledger: real ledger wallets (mix of exposed & clean)
            txs.append({"rail": "crypto", "expected_class": cat,
                        "args": (random.choice(ledger_wallets), _crypto_amount())})

    random.shuffle(txs)
    return txs


# =============================================================================
# BENCHMARK RUNNER
# =============================================================================

def _percentile(sorted_vals, pct):
    """Linear-interpolated percentile of an already-sorted list."""
    if not sorted_vals:
        return 0.0
    k = (len(sorted_vals) - 1) * pct
    lo, hi = math.floor(k), math.ceil(k)
    if lo == hi:
        return sorted_vals[int(k)]
    return sorted_vals[lo] * (hi - k) + sorted_vals[hi] * (k - lo)


def _tier1_hit(rail, result):
    """True if the verdict was resolved in the Tier-1 deterministic cache."""
    if rail == "crypto":
        return result.get("evidence", {}).get("distance_to_sanctioned") == 0
    for party in result.get("evidence", {}).get("parties", {}).values():
        if party.get("screening_tier") == "TIER_1_DETERMINISTIC":
            return True
    return False


def run_benchmark(transactions, warmup=50):
    """Execute every transaction, timing each call end-to-end."""
    evaluate = {"fiat": engine.evaluate_fiat_payment,
                "crypto": engine.evaluate_crypto_payment}

    # Warm-up to discount interpreter/first-call overhead.
    for tx in transactions[:warmup]:
        evaluate[tx["rail"]](*tx["args"])

    latencies = []
    by_rail = {"fiat": [], "crypto": []}
    verdicts = Counter()
    verdicts_by_rail = {"fiat": Counter(), "crypto": Counter()}
    tier1_hits = 0
    results_log = []  # every verdict, persisted to the DB after the run

    wall_start = time.perf_counter()
    for tx in transactions:
        rail = tx["rail"]
        t0 = time.perf_counter()
        result = evaluate[rail](*tx["args"])
        elapsed_ms = (time.perf_counter() - t0) * 1000.0

        latencies.append(elapsed_ms)
        by_rail[rail].append(elapsed_ms)
        verdicts[result["verdict"]] += 1
        verdicts_by_rail[rail][result["verdict"]] += 1
        results_log.append(result)
        if _tier1_hit(rail, result):
            tier1_hits += 1
    wall_total = time.perf_counter() - wall_start

    # Persist every screened transaction in one batched write.
    db.insert_transactions(results_log, source="benchmark")

    return {
        "count": len(transactions),
        "wall_total_s": wall_total,
        "throughput_tps": len(transactions) / wall_total if wall_total else 0,
        "latencies": latencies,
        "by_rail": by_rail,
        "verdicts": verdicts,
        "verdicts_by_rail": verdicts_by_rail,
        "tier1_hits": tier1_hits,
    }


def _latency_stats(latencies):
    s = sorted(latencies)
    return {
        "mean": statistics.fmean(s),
        "p50": _percentile(s, 0.50),
        "p90": _percentile(s, 0.90),
        "p95": _percentile(s, 0.95),
        "p99": _percentile(s, 0.99),
        "max": s[-1],
        "min": s[0],
    }


# =============================================================================
# REPORTING
# =============================================================================

_HIST_BUCKETS = [
    (0.00, 0.02), (0.02, 0.05), (0.05, 0.10), (0.10, 0.25),
    (0.25, 0.50), (0.50, 1.00), (1.00, float("inf")),
]


def _histogram(latencies, width=42):
    counts = [0] * len(_HIST_BUCKETS)
    for v in latencies:
        for i, (lo, hi) in enumerate(_HIST_BUCKETS):
            if lo <= v < hi:
                counts[i] += 1
                break
    peak = max(counts) or 1
    lines = []
    for (lo, hi), c in zip(_HIST_BUCKETS, counts):
        label = f"{lo:>4.2f}-{'inf ' if hi == float('inf') else f'{hi:>4.2f}'} ms"
        bar = "█" * round(width * c / peak)
        lines.append(f"   {label:>16} | {bar} {c}")
    return "\n".join(lines)


def print_report(stats):
    L = "=" * 70
    overall = _latency_stats(stats["latencies"])

    print("\n" + L)
    print("  ScreenSmart — Performance Benchmark")
    print(L)
    print(f"  Platform        : Python {platform.python_version()} · "
          f"{platform.system()} {platform.machine()}")
    print(f"  Transactions    : {stats['count']:,}")
    print(f"  Wall-clock time : {stats['wall_total_s'] * 1000:.1f} ms")
    print(f"  Throughput      : {stats['throughput_tps']:,.0f} tx/sec")
    cache_pct = 100 * stats["tier1_hits"] / stats["count"]
    print(f"  Tier-1 cache    : {stats['tier1_hits']:,} / {stats['count']:,} "
          f"resolved on the fast path ({cache_pct:.1f}%)")

    print("\n  Latency per transaction (end-to-end, ms)")
    print("  " + "-" * 66)
    print(f"   mean {overall['mean']:.4f}   p50 {overall['p50']:.4f}   "
          f"p90 {overall['p90']:.4f}   p95 {overall['p95']:.4f}")
    print(f"   p99  {overall['p99']:.4f}   max {overall['max']:.4f}   "
          f"min {overall['min']:.4f}")

    # Per-rail.
    print("\n  Latency by rail (ms)")
    print("  " + "-" * 66)
    print(f"   {'rail':<8}{'n':>7}{'mean':>10}{'p50':>10}{'p95':>10}{'p99':>10}{'max':>10}")
    for rail in ("fiat", "crypto"):
        vals = stats["by_rail"][rail]
        if not vals:
            continue
        st = _latency_stats(vals)
        print(f"   {rail:<8}{len(vals):>7}{st['mean']:>10.4f}{st['p50']:>10.4f}"
              f"{st['p95']:>10.4f}{st['p99']:>10.4f}{st['max']:>10.4f}")

    # Verdict distribution.
    print("\n  Verdict distribution")
    print("  " + "-" * 66)
    for verdict in ("MATCH", "REVIEW", "NO MATCH"):
        n = stats["verdicts"].get(verdict, 0)
        pct = 100 * n / stats["count"]
        bar = "█" * round(40 * n / stats["count"])
        print(f"   {verdict:<9}{n:>6} ({pct:>5.1f}%)  {bar}")
    f, c = stats["verdicts_by_rail"]["fiat"], stats["verdicts_by_rail"]["crypto"]
    print(f"   fiat  -> block {f['MATCH']}, review {f['REVIEW']}, release {f['NO MATCH']}")
    print(f"   crypto-> block {c['MATCH']}, review {c['REVIEW']}, release {c['NO MATCH']}")

    # Histogram.
    print("\n  Latency histogram")
    print("  " + "-" * 66)
    print(_histogram(stats["latencies"]))

    print(L)
    sla = overall["p99"]
    verdict_line = ("WELL within the sub-second SLA" if sla < 1000 else
                    "EXCEEDS the sub-second SLA")
    print(f"  p99 latency {sla:.4f} ms — {verdict_line}.")
    print(L + "\n")


def to_json(stats):
    overall = _latency_stats(stats["latencies"])
    return {
        "count": stats["count"],
        "wall_total_ms": round(stats["wall_total_s"] * 1000, 3),
        "throughput_tps": round(stats["throughput_tps"], 1),
        "tier1_hits": stats["tier1_hits"],
        "latency_ms": {k: round(v, 5) for k, v in overall.items()},
        "by_rail": {
            rail: {k: round(v, 5) for k, v in _latency_stats(vals).items()}
            for rail, vals in stats["by_rail"].items() if vals
        },
        "verdicts": dict(stats["verdicts"]),
    }


def histogram_data(latencies):
    """Latency histogram as a list of {label, count} buckets (for the UI)."""
    counts = [0] * len(_HIST_BUCKETS)
    for v in latencies:
        for i, (lo, hi) in enumerate(_HIST_BUCKETS):
            if lo <= v < hi:
                counts[i] += 1
                break
    out = []
    for (lo, hi), c in zip(_HIST_BUCKETS, counts):
        hi_label = "∞" if hi == float("inf") else f"{hi:.2f}"
        out.append({"label": f"{lo:.2f}–{hi_label} ms", "count": c})
    return out


def web_payload(stats, seed=None):
    """A rich, UI-friendly result payload consumed by the website."""
    overall = _latency_stats(stats["latencies"])
    count = stats["count"]
    by_rail = {}
    for rail, vals in stats["by_rail"].items():
        if not vals:
            continue
        st = _latency_stats(vals)
        by_rail[rail] = {"n": len(vals), **{k: round(v, 4) for k, v in st.items()}}
    verdicts = [
        {"verdict": v, "count": stats["verdicts"].get(v, 0),
         "pct": round(100 * stats["verdicts"].get(v, 0) / count, 1)}
        for v in ("MATCH", "REVIEW", "NO MATCH")
    ]
    return {
        "count": count,
        "seed": seed,
        "platform": f"Python {platform.python_version()} · "
                    f"{platform.system()} {platform.machine()}",
        "wall_total_ms": round(stats["wall_total_s"] * 1000, 2),
        "throughput_tps": round(stats["throughput_tps"], 0),
        "tier1_hits": stats["tier1_hits"],
        "tier1_pct": round(100 * stats["tier1_hits"] / count, 1),
        "latency_ms": {k: round(v, 4) for k, v in overall.items()},
        "by_rail": by_rail,
        "verdicts": verdicts,
        "histogram": histogram_data(stats["latencies"]),
    }


# =============================================================================
# ENTRY POINT
# =============================================================================

def main(argv=None):
    parser = argparse.ArgumentParser(description="ScreenSmart benchmark")
    parser.add_argument("--count", type=int, default=1000,
                        help="number of transactions to simulate (default 1000)")
    parser.add_argument("--seed", type=int, default=42,
                        help="RNG seed for reproducibility (default 42)")
    parser.add_argument("--json", metavar="PATH",
                        help="also write machine-readable results to PATH")
    args = parser.parse_args(argv)

    random.seed(args.seed)
    print(f"\n  Generating {args.count:,} synthetic transactions (seed={args.seed})…")
    transactions = generate_transactions(args.count)
    stats = run_benchmark(transactions)
    print_report(stats)

    if args.json:
        with open(args.json, "w") as fh:
            json.dump(to_json(stats), fh, indent=2)
        print(f"  Wrote machine-readable results to {args.json}\n")


if __name__ == "__main__":
    main()
