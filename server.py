"""
ScreenSmart - local web server (standard library only, zero dependencies).

Wraps the `screensmart` screening engine in a tiny JSON API and serves the
single-page "Screening Console" UI from ./web.

Run:
    python3 server.py
then open http://127.0.0.1:8000

Endpoints
    GET  /                       -> the console UI (web/index.html)
    GET  /api/meta               -> watchlist stats, thresholds, demo scenarios
    GET  /api/transactions/count -> number of stored transactions
    POST /api/screen/fiat        -> verdict payload (persisted)
    POST /api/screen/crypto      -> verdict payload (persisted)
    POST /api/benchmark          -> throughput/latency report (persisted)
    POST /api/transactions/clear -> delete all stored transactions

Reference data and the transaction log live in an on-disk SQLite database
(see db.py / screensmart.db). No in-memory database is used.
"""

import json
import os
import random
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

import benchmark as bench
import db
import screensmart as engine

# Upper bound so a browser request can't ask the engine to run forever.
MAX_BENCHMARK_COUNT = 50000

HOST = "127.0.0.1"
PORT = 8000
WEB_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "web")

_CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".svg": "image/svg+xml",
    ".ico": "image/x-icon",
    ".json": "application/json; charset=utf-8",
}

# A handful of real-world situations the UI offers as worked examples. Each
# mirrors a scenario from the engine's own test suite, written in plain language
# so a person (not just an analyst) can understand what they're looking at.
DEMO_SCENARIOS = [
    {"id": 1, "rail": "fiat", "expected": "NO MATCH",
     "title": "A routine supplier payment",
     "blurb": "A US logistics firm pays John Smith in Canada. Small amount, "
              "low-risk countries, nothing raises a flag.",
     "fields": {"sender": "Acme Logistics Inc", "recipient": "John Smith",
                "sender_country": "United States", "recipient_country": "Canada",
                "amount": 4500}},
    {"id": 2, "rail": "fiat", "expected": "MATCH",
     "title": "A name hiding behind a spelling",
     "blurb": "The recipient is written “Sergei Petrov” — a transliteration of "
              "the sanctioned “Sergey Petrov.” Same person, different alphabet.",
     "fields": {"sender": "Global Trade LLC", "recipient": "Sergei Petrov",
                "sender_country": "United States", "recipient_country": "Russia",
                "amount": 8000}},
    {"id": 3, "rail": "fiat", "expected": "REVIEW",
     "title": "Close, but not certain",
     "blurb": "“Sergey Petrenko” resembles a sanctioned name, sits in a "
              "high-risk country, and has adverse press — worth a human's eyes.",
     "fields": {"sender": "Maria Lopez", "recipient": "Sergey Petrenko",
                "sender_country": "Spain", "recipient_country": "Russia",
                "amount": 12000}},
    {"id": 4, "rail": "fiat", "expected": "REVIEW",
     "title": "A very large transfer",
     "blurb": "Two clean parties, but the size of the transfer alone triggers "
              "enhanced due diligence — more money, more risk.",
     "fields": {"sender": "Acme Logistics Inc", "recipient": "John Smith",
                "sender_country": "United States", "recipient_country": "United States",
                "amount": 500000}},
    {"id": 5, "rail": "crypto", "expected": "MATCH",
     "title": "A wallet on the blocklist",
     "blurb": "The counterparty address is itself on the OFAC sanctions list — a "
              "direct, unambiguous hit.",
     "fields": {"wallet_address": "0xSANCTIONED_LAZARUS_7", "amount": 5.0}},
    {"id": 6, "rail": "crypto", "expected": "REVIEW",
     "title": "Two hops from trouble",
     "blurb": "The wallet itself is clean, but its funds trace back to a "
              "sanctioned source just two transfers ago.",
     "fields": {"wallet_address": "0xRECIPIENT_2HOP", "amount": 6.2}},
    {"id": 7, "rail": "crypto", "expected": "NO MATCH",
     "title": "A clean counterparty",
     "blurb": "No connection to any sanctioned address anywhere within the "
              "trace depth. Safe to release.",
     "fields": {"wallet_address": "0xCLEAN_MERCHANT", "amount": 3.0}},
]


def _meta_payload():
    """Live watchlist statistics + policy config for the UI header & meter."""
    counts = db.counts()
    counts["max_hops"] = engine.MAX_HOPS
    return {
        "counts": counts,
        "thresholds": {
            "match": engine.MATCH_THRESHOLD,
            "review": engine.REVIEW_THRESHOLD,
        },
        "scenarios": DEMO_SCENARIOS,
    }


class Handler(BaseHTTPRequestHandler):
    """Routes API calls to the engine and serves static files for everything else."""

    server_version = "ScreenSmart/1.0"

    # -- helpers --------------------------------------------------------------
    def _send_json(self, payload, status=200):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _send_file(self, rel_path):
        # Resolve safely inside WEB_DIR (block path traversal).
        safe = os.path.normpath(os.path.join(WEB_DIR, rel_path.lstrip("/")))
        if not safe.startswith(WEB_DIR) or not os.path.isfile(safe):
            self._send_json({"error": "not found"}, status=404)
            return
        ext = os.path.splitext(safe)[1].lower()
        with open(safe, "rb") as fh:
            data = fh.read()
        self.send_response(200)
        self.send_header("Content-Type", _CONTENT_TYPES.get(ext, "application/octet-stream"))
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _read_json_body(self):
        length = int(self.headers.get("Content-Length", 0))
        if not length:
            return {}
        return json.loads(self.rfile.read(length).decode("utf-8") or "{}")

    # -- routing --------------------------------------------------------------
    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/api/meta":
            self._send_json(_meta_payload())
        elif path == "/api/transactions/count":
            self._send_json({"count": db.count_transactions()})
        elif path == "/api/queue":
            self._send_json(db.get_review_queue())
        elif path.startswith("/api/related/"):
            sid = path[len("/api/related/"):]
            row = db.get_transaction_by_id(sid) if sid else None
            if not row:
                self._send_json([])
            else:
                related = db.get_related_transactions(
                    screening_id=row["screening_id"],
                    country=row["country"],
                    recipient=row["recipient"],
                    sender=row["sender"],
                    rail=row["rail"],
                    reason=row["reason"],
                )
                self._send_json([dict(r) for r in related])
        elif path in ("/", ""):
            self._send_file("index.html")
        elif path == "/analyst":
            self._send_file("analyst.html")
        else:
            self._send_file(path)

    def do_POST(self):
        path = urlparse(self.path).path
        try:
            data = self._read_json_body()
            if path == "/api/screen/fiat":
                # `country` is accepted as a fallback for both sides.
                fallback = str(data.get("country", ""))
                result = engine.evaluate_fiat_payment(
                    sender=str(data.get("sender", "")),
                    recipient=str(data.get("recipient", "")),
                    sender_country=str(data.get("sender_country", fallback)),
                    recipient_country=str(data.get("recipient_country", fallback)),
                    amount=float(data.get("amount") or 0),
                )
                db.insert_transaction(result, source="ui")
                self._send_json(result)
            elif path == "/api/screen/crypto":
                result = engine.evaluate_crypto_payment(
                    wallet_address=str(data.get("wallet_address", "")),
                    amount=float(data.get("amount") or 0),
                )
                db.insert_transaction(result, source="ui")
                self._send_json(result)
            elif path == "/api/benchmark":
                count = max(1, min(int(data.get("count") or 1000), MAX_BENCHMARK_COUNT))
                seed = int(data.get("seed") or 42)
                random.seed(seed)
                transactions = bench.generate_transactions(count)
                stats = bench.run_benchmark(transactions)  # persists to DB
                self._send_json(bench.web_payload(stats, seed=seed))
            elif path == "/api/transactions/clear":
                self._send_json({"cleared": db.clear_transactions()})
            else:
                self._send_json({"error": "unknown endpoint"}, status=404)
        except (ValueError, json.JSONDecodeError) as exc:
            self._send_json({"error": f"bad request: {exc}"}, status=400)

    # Quieter, single-line access log.
    def log_message(self, fmt, *args):
        print(f"  [{self.log_date_time_string()}] {fmt % args}")


def main():
    httpd = ThreadingHTTPServer((HOST, PORT), Handler)
    print("\n  ScreenSmart console running")
    print(f"  -> http://{HOST}:{PORT}\n  (Ctrl+C to stop)\n")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n  Stopped.\n")
        httpd.server_close()


if __name__ == "__main__":
    main()
