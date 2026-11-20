#!/usr/bin/env python3
"""Underworld - Hogmanay in the Gardens ticket monitor.

Polls the official Edinburgh's Hogmanay box office feed (the same public JSON
endpoint the booking page at edwinterfest.com uses) and records, per ticket
tier, the price and how much is left. Writes TICKETS.md and prints ALERT lines
when a tier is on sale below its regular price, a sold-out tier comes back
(returns are re-sold at face value), or a tier is about to sell out.

Standard library only. Usage:
    python3 tickets.py                # check, store, write TICKETS.md
    python3 tickets.py --report-only  # rebuild TICKETS.md from stored data
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
UA = {"User-Agent": "hogmanay-ticket-monitor/1.0"}

SCHEMA = """
CREATE TABLE IF NOT EXISTS ticket_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS ticket_quotes (
    run_id INTEGER NOT NULL REFERENCES ticket_runs(id),
    performance_id TEXT NOT NULL,
    performance_at TEXT NOT NULL,
    performance_status TEXT NOT NULL,
    tier TEXT NOT NULL,
    price REAL NOT NULL,          -- all-in price per ticket (fees + £1 donation included)
    regular_price REAL,           -- from tickets.json
    pct_remaining INTEGER,        -- NULL when the box office doesn't publish it
    gates TEXT,                   -- JSON: per-gate allocation counters from the feed (not tickets left)
    on_sale INTEGER NOT NULL      -- 1 = can be bought right now
);
"""


def get_json(url: str, retries: int = 3):
    req = urllib.request.Request(url, headers=UA)
    for attempt in range(retries):
        try:
            with urllib.request.urlopen(req, timeout=40) as resp:
                return json.load(resp)
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
            if attempt == retries - 1:
                raise RuntimeError(f"{url}: {exc}") from exc
            time.sleep(3 * (attempt + 1))


def fetch(cfg: dict) -> list[dict]:
    """Return one row per tier per performance, using live (uncached) data."""
    base = f"{cfg['api_base']}/projects/{cfg['slug']}/performances"
    listing = get_json(base)
    perf_ids = [p["id"] for p in listing.get("cache", [])]
    if not perf_ids:
        raise RuntimeError("No performances listed; the event page may have changed.")
    rows = []
    for pid in perf_ids:
        perf = get_json(f"{base}/{pid}")
        live = perf.get("status") == "On Sale" and not perf.get("isCancelled")
        for band in perf.get("prices", []):
            tier = band["pricetype"]
            if tier not in cfg["tiers"]:
                continue  # e.g. "Access Platform", booked through the Access team
            pct = band.get("seatPercentageRemaining")
            gates = {c["title"]: c.get("remainingLimitValue") for c in band.get("concessions", [])
                     if c.get("limited")}
            # The booking page shows a tier as sold out when seatPercentageRemaining is 0.
            on_sale = live and (pct is None or pct > 0)
            rows.append({"performance_id": pid, "performance_at": perf.get("datetime", ""),
                         "performance_status": perf.get("status", "?"), "tier": tier,
                         "price": float(band.get("inclusivePrice") or band["price"]),
                         "regular_price": cfg["tiers"][tier], "pct_remaining": pct,
                         "gates": gates, "on_sale": on_sale})
    return rows


def store(db: sqlite3.Connection, rows: list[dict]) -> int:
    run_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    run_id = db.execute("INSERT INTO ticket_runs(run_at) VALUES (?)", (run_at,)).lastrowid
    db.executemany(
        "INSERT INTO ticket_quotes VALUES (?,?,?,?,?,?,?,?,?,?)",
        [(run_id, r["performance_id"], r["performance_at"], r["performance_status"], r["tier"],
          r["price"], r["regular_price"], r["pct_remaining"], json.dumps(r["gates"]), int(r["on_sale"]))
         for r in rows])
    db.commit()
    return run_id


def load_run(db: sqlite3.Connection, run_id: int) -> dict:
    out = {}
    for pid, at, status, tier, price, reg, pct, gates, on_sale in db.execute(
            "SELECT performance_id, performance_at, performance_status, tier, price, regular_price, "
            "pct_remaining, gates, on_sale FROM ticket_quotes WHERE run_id=?", (run_id,)):
        out[(pid, tier)] = {"at": at, "status": status, "tier": tier, "price": price, "regular": reg,
                            "pct": pct, "gates": json.loads(gates or "{}"), "on_sale": bool(on_sale)}
    return out


def build_report(cfg: dict, db: sqlite3.Connection) -> tuple[str, list[str]]:
    runs = db.execute("SELECT id, run_at FROM ticket_runs ORDER BY id DESC LIMIT 2").fetchall()
    if not runs:
        return "No data yet. Run `python3 tickets.py` first.\n", []
    (run_id, run_at), prev = runs[0], (runs[1] if len(runs) > 1 else None)
    now, before = load_run(db, run_id), (load_run(db, prev[0]) if prev else {})
    low_pct = cfg.get("alerts", {}).get("low_stock_percent", 10)
    alerts: list[str] = []

    lines = [f"# {cfg['event']} — tickets", "",
             f"Last checked **{run_at}**" + (f" (compared with {prev[1]})" if prev else " (first run)") + ".  ",
             f"Source: official box office feed. Book at {cfg['booking_url']}  ",
             "Prices are per ticket and include booking fees and the £1 charity donation.", ""]

    for (pid, tier), q in sorted(now.items(), key=lambda kv: kv[1]["regular"]):
        p = before.get((pid, tier))
        name = f"{tier} (£{q['regular']:.0f} regular)"
        if q["on_sale"] and q["price"] < q["regular"] - 0.005:
            if not p or not p["on_sale"] or p["price"] > q["price"]:
                alerts.append(f"BELOW REGULAR PRICE {tier}: £{q['price']:.2f} (regular £{q['regular']:.0f}) — {cfg['booking_url']}")
        if p and q["on_sale"] and not p["on_sale"]:
            alerts.append(f"BACK ON SALE {name} at £{q['price']:.2f} — returns go fast: {cfg['booking_url']}")
        if p and p["on_sale"] and not q["on_sale"]:
            alerts.append(f"SOLD OUT {name}")
        if (q["on_sale"] and q["pct"] is not None and q["pct"] <= low_pct
                and (not p or p["pct"] is None or p["pct"] > low_pct)):
            alerts.append(f"SELLING OUT {name}: only {q['pct']}% left")
        if p and p["price"] != q["price"]:
            alerts.append(f"PRICE CHANGE {tier}: £{p['price']:.2f} → £{q['price']:.2f}")

    by_perf: dict[str, list] = {}
    for (pid, _), q in now.items():
        by_perf.setdefault(pid, []).append(q)
    for pid, tiers in by_perf.items():
        when = datetime.strptime(tiers[0]["at"], "%Y-%m-%d %H:%M:%S").strftime("%a %d %b %Y, %H:%M")
        lines += [f"## {when} — box office status: {tiers[0]['status']}", "",
                  "| Tier | Price now | Regular | Status | Left |",
                  "|---|---:|---:|---|---:|"]
        for q in sorted(tiers, key=lambda t: t["regular"]):
            status = "**On sale**" if q["on_sale"] else "Sold out"
            if q["on_sale"] and q["price"] < q["regular"]:
                status = "**On sale, below regular**"
            left = f"{q['pct']}%" if q["pct"] is not None else "–"
            lines.append(f"| {q['tier']} | £{q['price']:.2f} | £{q['regular']:.2f} | {status} | {left} |")
        lines.append("")

    hist = db.execute(
        "SELECT r.run_at, q.tier, q.on_sale, q.pct_remaining FROM ticket_quotes q "
        "JOIN ticket_runs r ON r.id=q.run_id ORDER BY r.id DESC, q.tier LIMIT 60").fetchall()
    if hist:
        lines += ["## Recent checks", "", "| Checked | Tier | Status | Left |", "|---|---|---|---:|"]
        lines += [f"| {t} | {tier} | {'On sale' if s else 'Sold out'} | {'' if p is None else f'{p}%'} |"
                  for t, tier, s, p in hist]
        lines.append("")

    lines += ["## Resale", "",
              "This monitor reads the official box office only. Resale sites (Twickets, StubHub, viagogo) are "
              "not checked automatically. The event pages don't mention an official resale platform. Tickets are "
              "e-tickets with one QR code per order, and the entry gate can't be changed after purchase. "
              "A resale ticket has to be for the gate you plan to use.", ""]

    if alerts:
        lines[6:6] = ["## Alerts", ""] + [f"- {a}" for a in alerts] + [""]
    return "\n".join(lines), alerts


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", default=ROOT / "tickets.json", type=Path)
    ap.add_argument("--db", default=ROOT / "data" / "prices.db", type=Path)
    ap.add_argument("--report", default=ROOT / "TICKETS.md", type=Path)
    ap.add_argument("--report-only", action="store_true")
    args = ap.parse_args()

    cfg = json.loads(args.config.read_text())
    args.db.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(args.db)
    db.executescript(SCHEMA)
    if not args.report_only:
        rows = fetch(cfg)
        run_id = store(db, rows)
        print(f"Ticket run {run_id} stored: " + ", ".join(
            f"{r['tier']} £{r['price']:.0f} {'on sale' if r['on_sale'] else 'sold out'}" for r in rows))
    report, alerts = build_report(cfg, db)
    args.report.write_text(report)
    print(f"Report written to {args.report}")
    for a in alerts:
        print("ALERT:", a)
    return 0


if __name__ == "__main__":
    sys.exit(main())
