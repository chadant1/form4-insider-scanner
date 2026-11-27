"""Nightly cloud scan for the phone page.

Runs one market-wide scan of SEC Form 4 filings (open-market buys and sales), applies the same
"True Insider Buying" and "Significant Insider Selling" presets as the desktop app, ranks the
week's biggest buyers and sellers, and writes a static web site (index.html + data.json) that
GitHub Pages serves to your phone.

    SEC_CONTACT="Pat Smith pat@gmail.com" python cloud/publish_site.py --out _site

The SEC requires a real name and email on automated requests; the workflow reads them from the
repository secret SEC_CONTACT so they never appear in the code or on the page.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import shutil
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
import form4_scanner as F  # noqa: E402

try:
    from zoneinfo import ZoneInfo
    CENTRAL = ZoneInfo("America/Chicago")
except Exception:                                   # pragma: no cover
    CENTRAL = dt.timezone(dt.timedelta(hours=-6))

SCAN_DAYS = 30          # both desktop presets look back 30 days
WEEK_DAYS = 7
MIN_VALUE = 100_000     # buying preset minimum; the selling preset adds its own $250k floor
SELL_MIN_VALUE = 250_000
SELL_MIN_PCT = 25       # selling preset: sold 25%+ of the holding
TOP_PER_SIDE = 20       # "This week": biggest buyers / sellers shown


def _tradable(r: dict) -> bool:
    """Desktop preset liquidity filters: price >= $1 and >= $100K traded per day (when known)."""
    if r.get("last_close") is not None and r["last_close"] < 1:
        return False
    if r.get("dollar_vol") is not None and r["dollar_vol"] < 100_000:
        return False
    return True


def is_preset_buy(r: dict) -> bool:
    return (r["code"] == "P" and not r.get("plan") and not r.get("dup_of") and r["value"] >= MIN_VALUE
            and r.get("cluster", 1) >= 2 and r.get("stake") is not None and r["stake"] >= 10
            and r.get("pattern_kind") not in ("routine", "regular") and r.get("sold_kind") != "sold"
            and not r.get("offering") and not r.get("entity") and _tradable(r))


def is_preset_sell(r: dict) -> bool:
    return (r["code"] == "S" and not r.get("plan") and not r.get("dup_of") and r["value"] >= SELL_MIN_VALUE
            and r.get("stake") is not None and r["stake"] <= -SELL_MIN_PCT
            and not r.get("explained") and not r.get("exercise_sell")
            and r.get("pattern_kind") not in ("routine", "regular")
            and not r.get("offering") and _tradable(r))


def is_week_trade(r: dict, week_start: str) -> bool:
    return (r["code"] in ("P", "S") and r["filing_date"] >= week_start and not r.get("plan")
            and not r.get("dup_of") and not r.get("offering") and not r.get("entity") and r["value"] >= MIN_VALUE)


def flags(r: dict) -> list[str]:
    out = []
    if r.get("d13"):
        out.append("13D filer")
    if (r.get("late_days") or 0) > 0:
        out.append(f"Filed {r['late_days']} days late")
    if r.get("low_cash"):
        out.append("Low cash")
    if r.get("bm") is not None and r["bm"] < 0:
        out.append("Negative equity")
    if r.get("explained"):
        out.append("Explained sale")
    if r.get("exercise_sell"):
        out.append("Exercise and sell")
    return out


def _num(v, digits=1):
    if v is None or v == float("inf") or v == float("-inf"):
        return None
    return round(float(v), digits)


def export_row(r: dict) -> dict:
    return {
        "id": r.get("hkey", r["acc"]), "code": r["code"], "ticker": r.get("ticker", ""),
        "company": r.get("company", ""), "insider": r.get("insider", ""), "role": r.get("role", ""),
        "title": r.get("title", ""), "filed": r["filing_date"], "traded": r.get("trade_start") or "",
        "shares": round(r.get("shares") or 0), "price": _num(r.get("price"), 2), "value": round(r["value"]),
        "stake": _num(r.get("stake")), "stake_new": r.get("stake") == float("inf"),
        "cluster": r.get("cluster", 1), "score": r.get("score"), "why": list(r.get("score_why") or []),
        "pattern": r.get("pattern", ""), "sold": r.get("sold", ""), "flags": flags(r),
        "mcap": F.fmt_compact(r["mcap"]) if r.get("mcap") else "",
        "pct_co": _num(r.get("cluster_pct_co"), 2), "off_high": _num(r.get("off_high"), 0),
        "chg3m": _num(r.get("chg3m"), 0), "since": _num(r.get("since"), 0),
        "net90": F.fmt_net90(r.get("net90")), "link": r.get("link", ""),
    }


def week_leaders(rows: list, side: str) -> list[dict]:
    by: dict = {}
    for r in rows:
        if r["code"] != side:
            continue
        key = r.get("owner_cik") or r["insider"].lower()
        g = by.setdefault(key, {"insider": r["insider"], "role": r.get("role", ""), "total": 0.0, "trades": 0,
                                "tickers": [], "top_score": None, "ids": []})
        g["total"] += r["value"]
        g["trades"] += 1
        if r.get("ticker") and r["ticker"] not in g["tickers"]:
            g["tickers"].append(r["ticker"])
        if r.get("score") is not None and (g["top_score"] is None or r["score"] > g["top_score"]):
            g["top_score"] = r["score"]
        g["ids"].append(r.get("hkey", r["acc"]))
    leaders = sorted(by.values(), key=lambda g: -g["total"])[:TOP_PER_SIDE]
    for g in leaders:
        g["total"] = round(g["total"])
    return leaders


def build_data(rows: list, clusters: dict, end: dt.date, now: dt.datetime, stats: dict) -> dict:
    rows = F.apply_signal_filters(rows, clusters, exclude_plan=True, min_cluster=0, min_stake=0)
    week_start = (end - dt.timedelta(days=WEEK_DAYS - 1)).isoformat()
    buys = sorted((r for r in rows if is_preset_buy(r)), key=lambda r: (-(r["score"] or 0), -r["value"]))
    sells = sorted((r for r in rows if is_preset_sell(r)), key=lambda r: (-(r["score"] or 0), -r["value"]))
    week = [r for r in rows if is_week_trade(r, week_start)]
    week_buyers, week_sellers = week_leaders(week, "P"), week_leaders(week, "S")
    keep_ids = {r.get("hkey", r["acc"]) for r in buys + sells}
    keep_ids |= {i for g in week_buyers + week_sellers for i in g["ids"]}
    out_rows = [export_row(r) for r in rows if r.get("hkey", r["acc"]) in keep_ids]
    local = now.astimezone(CENTRAL)
    return {
        "app_version": F.APP_VERSION,
        "generated_utc": now.astimezone(dt.timezone.utc).isoformat(timespec="seconds"),
        "generated_local": (f"{local:%a %b} {local.day}, {local.hour % 12 or 12}:{local:%M %p} "
                            + (local.tzname() or "CT")),
        "start": (end - dt.timedelta(days=SCAN_DAYS)).isoformat(), "end": end.isoformat(),
        "week_start": week_start,
        "filings_scanned": stats.get("done", 0),
        "buys": [r.get("hkey", r["acc"]) for r in buys],
        "sells": [r.get("hkey", r["acc"]) for r in sells],
        "week_buyers": week_buyers, "week_sellers": week_sellers,
        "rows": out_rows,
    }


def contact_from_env() -> str:
    ua = (os.environ.get("SEC_CONTACT") or "").strip()
    if not ua:
        name, email = os.environ.get("SEC_NAME", ""), os.environ.get("SEC_EMAIL", "")
        if name or email:
            problem = F.identity_problem(name, email)
            if problem:
                raise SystemExit(f"SEC_NAME / SEC_EMAIL: {problem}")
            ua = F.sec_user_agent(name, email)
    if not F.valid_user_agent(ua):
        raise SystemExit("Set the repository secret SEC_CONTACT to your name and email, "
                         "e.g. 'Pat Smith pat@gmail.com' (the SEC requires it).")
    return ua


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--out", default="_site", help="folder to write the web site into")
    ap.add_argument("--end", help="last filing date to scan (YYYY-MM-DD); default today in Central time")
    a = ap.parse_args(argv)
    ua = contact_from_env()
    now = dt.datetime.now(dt.timezone.utc)
    end = dt.date.fromisoformat(a.end) if a.end else now.astimezone(CENTRAL).date()
    params = F.ScanParams(start=end - dt.timedelta(days=SCAN_DAYS), end=end, user_agent=ua, codes={"P", "S"},
                          min_value=MIN_VALUE, include_amendments=False, combine_lots=True, check_history=True)
    t0 = time.time()
    print(f"Scanning {params.start} to {params.end} (open-market buys and sales >= ${MIN_VALUE:,})", flush=True)
    rows, clusters, stats = F.collect_scan(params, log=lambda m: print(m, flush=True))
    data = build_data(rows, clusters, end, now, stats)
    data["scan_minutes"] = round((time.time() - t0) / 60, 1)
    os.makedirs(a.out, exist_ok=True)
    for name in os.listdir(os.path.join(HERE, "site")):
        shutil.copy2(os.path.join(HERE, "site", name), os.path.join(a.out, name))
    with open(os.path.join(a.out, "data.json"), "w", encoding="utf-8") as fh:
        json.dump(data, fh, separators=(",", ":"))
    print(f"Done in {data['scan_minutes']} min: {len(data['buys'])} preset buys, {len(data['sells'])} preset sells, "
          f"{len(data['week_buyers'])} top buyers and {len(data['week_sellers'])} top sellers this week, "
          f"{len(data['rows'])} trades written to {a.out}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
