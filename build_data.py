#!/usr/bin/env python3
"""Fetch live prices from Stooq and write data.json + history.json.

Run locally:   python3 build_data.py
Run in CI:     same command — no dependencies beyond stdlib
"""
import csv, json, pathlib, subprocess, datetime, io

ROOT = pathlib.Path(__file__).parent

# ── PORTFOLIO DEFINITION ────────────────────────────────────────────────────
SPY_COST = 751.41  # SPY price at inception (29 May 2026)

HOLDINGS = [
    {"ticker": "RKLB",  "name": "Rocket Lab",           "shares": 16,  "cost": 138.80,    "bucket": "Launch & Systems"},
    {"ticker": "ASTS",  "name": "AST SpaceMobile",       "shares": 13,  "cost": 109.625,   "bucket": "Direct-to-Cell"},
    {"ticker": "PL",    "name": "Planet Labs",            "shares": 21,  "cost": 46.908,    "bucket": "Earth Observation"},
    {"ticker": "IRDM",  "name": "Iridium Communications", "shares": 17,  "cost": 48.57,     "bucket": "SatComms"},
    {"ticker": "VSAT",  "name": "Viasat",                 "shares": 10,  "cost": 81.54,     "bucket": "SatComms"},
    {"ticker": "HEI",   "name": "HEICO",                  "shares": 2,   "cost": 345.20,    "bucket": "Aerospace Components"},
    {"ticker": "HWM",   "name": "Howmet Aerospace",       "shares": 2,   "cost": 259.05,    "bucket": "Aerostructures"},
    {"ticker": "BKSY",  "name": "BlackSky Technology",    "shares": 10,  "cost": 44.83,     "bucket": "Earth Observation"},
    {"ticker": "SPIR",  "name": "Spire Global",           "shares": 22,  "cost": 22.60,     "bucket": "Space Data"},
    {"ticker": "MRCY",  "name": "Mercury Systems",        "shares": 4,   "cost": 108.725,   "bucket": "Space Electronics"},
    {"ticker": "HXL",   "name": "Hexcel",                 "shares": 5,   "cost": 90.96,     "bucket": "Composite Materials"},
    {"ticker": "MNTS",  "name": "Momentus",               "shares": 22,  "cost": 17.53,     "bucket": "In-Space Logistics"},
]

# ── FETCH PRICES VIA STOOQ ──────────────────────────────────────────────────
def _fetch_one(t: str, header: str):
    """Fetch a single ticker from Stooq; return price dict or None."""
    try:
        result = subprocess.run(
            ["curl", "-s", "-A", "Mozilla/5.0",
             f"https://stooq.com/q/l/?s={t.lower()}.us&f=sd2t2ohlcvp&e=csv"],
            capture_output=True, text=True, timeout=12
        )
        raw = result.stdout.strip()
        # Close is field index 6; require it present (prev-close may be N/D, handled below)
        if not raw or len(raw.split(",")) < 7 or raw.split(",")[6] in ("N/D", ""):
            return None
        reader = csv.DictReader(io.StringIO(header + raw))
        for row in reader:
            close_val = row.get("Close", "")
            open_val  = row.get("Open", "")
            prev_val  = row.get("PrevClose", "")
            if close_val not in ("N/D", "", None):
                close = float(close_val)
                opn   = float(open_val) if open_val not in ("N/D", "") else close
                prev  = float(prev_val) if prev_val not in ("N/D", "", None) else opn
                return {"close": close, "open": opn, "prev": prev, "date": row["Date"]}
    except Exception as e:
        print(f"  Warning: {t} — {e}")
    return None


def fetch_stooq(tickers: list[str]) -> dict:
    """Fetch Stooq EOD CSV one ticker at a time, with one retry on miss."""
    import time
    HEADER = "Symbol,Date,Time,Open,High,Low,Close,Volume,PrevClose\n"
    prices = {}
    for i, t in enumerate(tickers):
        if i > 0 and i % 10 == 0:
            time.sleep(1)
        p = _fetch_one(t, HEADER)
        if p is None:
            time.sleep(1.5)          # back off, then retry once (handles rate-limiting)
            p = _fetch_one(t, HEADER)
        if p is not None:
            prices[t] = p
    return prices


# ── MAIN ────────────────────────────────────────────────────────────────────
def main():
    all_tickers = [h["ticker"] for h in HOLDINGS] + ["SPY"]
    print(f"Fetching {len(all_tickers)} tickers from Stooq…")
    prices = fetch_stooq(all_tickers)
    print(f"  Got prices for: {sorted(prices.keys())}")

    # ── SAFETY GUARD: never overwrite good data with a broken/empty fetch ──
    # Require SPY + at least 60% of holdings, else keep the last good data.json.
    need = max(1, int(len(HOLDINGS) * 0.6))
    got_holdings = sum(1 for h in HOLDINGS if h["ticker"] in prices)
    if "SPY" not in prices or got_holdings < need:
        print(f"  ⚠ Insufficient prices ({got_holdings}/{len(HOLDINGS)} holdings, "
              f"SPY={'ok' if 'SPY' in prices else 'MISSING'}). "
              f"Keeping existing data.json — NOT overwriting.")
        return

    now   = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    as_of = next(iter(prices.values()))["date"] if prices else "—"

    # Compute SPY return
    spy_cur  = prices.get("SPY", {}).get("close")
    spy_ret  = round((spy_cur / SPY_COST - 1) * 100, 4) if spy_cur else None

    # Compute per-holding stats
    holdings_out = []
    total_cost = 0.0
    total_val  = 0.0

    for h in HOLDINGS:
        t      = h["ticker"]
        cur    = prices.get(t, {}).get("close")
        prev   = prices.get(t, {}).get("prev") or prices.get(t, {}).get("open") or cur
        cost_basis = round(h["shares"] * h["cost"], 4)
        cur_val    = round(h["shares"] * cur, 4)    if cur else None
        pnl        = round(cur_val - cost_basis, 4)  if cur_val is not None else None
        pnl_pct    = round((cur / h["cost"] - 1) * 100, 4) if cur else None
        day        = round((cur / prev - 1) * 100, 4)       if cur and prev else None

        total_cost += cost_basis
        if cur_val is not None:
            total_val += cur_val

        holdings_out.append({
            "ticker":     t,
            "name":       h["name"],
            "bucket":     h["bucket"],
            "shares":     h["shares"],
            "cost":       h["cost"],
            "cost_basis": cost_basis,
            "cur":        cur,
            "cur_val":    cur_val,
            "pnl":        pnl,
            "pnl_pct":    pnl_pct,
            "day":        day,
            "date":       prices.get(t, {}).get("date", ""),
        })

    total_pnl     = round(total_val - total_cost, 4)
    total_pnl_pct = round((total_val / total_cost - 1) * 100, 4) if total_cost else 0.0

    out = {
        "generated":      now,
        "as_of":          as_of,
        "total_cost":     round(total_cost, 2),
        "total_val":      round(total_val, 2),
        "total_pnl":      round(total_pnl, 2),
        "total_pnl_pct":  total_pnl_pct,
        "spy_ret":        spy_ret,
        "spy_price":      spy_cur,
        "holdings":       holdings_out,
    }

    path = ROOT / "data.json"
    path.write_text(json.dumps(out, indent=2))
    pnl_sign = "+" if total_pnl >= 0 else ""
    print(
        f"Wrote {path}  —  "
        f"Total value ${total_val:,.2f}  "
        f"P&L {pnl_sign}${total_pnl:,.2f} ({pnl_sign}{total_pnl_pct:.2f}%)  "
        f"SPY {spy_ret:+.2f}%" if spy_ret else ""
    )

    update_history(out, ROOT)


def update_history(out, root):
    """Append hourly P&L % snapshot to history.json for charting."""
    path = root / "history.json"
    try:
        entries = json.loads(path.read_text()) if path.exists() else []
    except Exception:
        entries = []

    point = {
        "ts":        out["generated"],
        "portfolio": out["total_pnl_pct"],
        "spy":       out["spy_ret"],
    }

    # Update existing entry if same hour, else append
    ts_hour = point["ts"][:13]
    if entries and entries[-1]["ts"][:13] == ts_hour:
        entries[-1] = point
    else:
        entries.append(point)

    entries = entries[-1000:]
    path.write_text(json.dumps(entries))
    print(f"Updated {path}  ({len(entries)} entries)")


if __name__ == "__main__":
    main()
