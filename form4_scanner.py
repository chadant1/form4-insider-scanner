#!/usr/bin/env python3
"""
SEC Form 4 Insider Trading Scanner
==================================

A standalone desktop app (Tkinter) that scans SEC EDGAR Form 4 filings for
insider purchases/sales, filters them by date range, ticker, insider role,
transaction code and dollar value, and shows the matches in a sortable table.

* Pure Python standard library - no pip installs required (Python 3.9+).
* All SEC traffic runs on a background thread; the window never freezes.
* Polite to SEC: declared User-Agent, <= 8 requests/second, retry with backoff.
* Parsed filings are cached locally, so re-scanning with different filters is fast.

Run:   python form4_scanner.py            (GUI)
       python form4_scanner.py --cli -h   (command-line mode, for scripting)
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import gzip
import json
import os
import queue
import re
import sqlite3
import sys
import threading
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
import xml.etree.ElementTree as ET
import zlib
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field

APP_NAME = "SEC Form 4 Insider Scanner"
APP_VERSION = "1.3.0"

SEC_WWW = "https://www.sec.gov"
SEC_DATA = "https://data.sec.gov"
MAX_REQUESTS_PER_SECOND = 8          # SEC's published limit is 10/sec
FETCH_WORKERS = 4
HISTORY_YEARS = 5                    # how far back to look for an insider's earlier buys
CONTEXT_DAYS = 90                    # window for "what other insiders did" and recent 13D filings
PRICE_RPS = 3                        # Yahoo Finance requests per second
BROWSER_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130 Safari/537.36"
CACHE_VERSION = 3                    # bump if the parsed-record format changes

# ----------------------------------------------------------------------------
# Insider-role matching (case-insensitive, on officer title + relationship text)
# ----------------------------------------------------------------------------
ROLE_CEO = "CEO"
ROLE_CFO = "CFO"
ROLE_LEGAL = "Legal / General Counsel"
ROLE_DIR = "Director / 10% Owner"
ROLE_ORDER = [ROLE_CEO, ROLE_CFO, ROLE_LEGAL, ROLE_DIR]

ROLE_PATTERNS = {
    ROLE_CEO: re.compile(r"\bC\.?\s?E\.?\s?O\b|chief\s+exec(?:utive|\.)?\s+officer", re.I),
    ROLE_CFO: re.compile(r"\bC\.?\s?F\.?\s?O\b|chief\s+financial\s+officer", re.I),
    ROLE_LEGAL: re.compile(r"general\s+counsel|chief\s+legal\s+officer|\bCLO\b|counsel|legal", re.I),
    ROLE_DIR: re.compile(r"^\s*(?:board\s+)?director\s*$|10\s*%\s*owner|ten\s+percent\s+owner", re.I),
}

# Transaction codes offered in the UI
TXN_CODES = {
    "P": ("Open-Market Purchases (Code P)", "Buy"),
    "S": ("Open-Market Sales (Code S)", "Sell"),
    "A": ("Grants / Awards (Code A)", "Grant"),
    "M": ("Option Exercises (Code M)", "Exercise"),
}

RANGE_CHOICES = {"Last 7 Days": 7, "Last 14 Days": 14, "Last 30 Days": 30, "Custom Range": None}


# ----------------------------------------------------------------------------
# Paths / settings
# ----------------------------------------------------------------------------
def app_data_dir() -> str:
    base = (os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA")
            or os.path.join(os.path.expanduser("~"), ".local", "share"))
    path = os.path.join(base, "Form4Scanner")
    os.makedirs(path, exist_ok=True)
    return path


def cache_path() -> str:
    return os.path.join(app_data_dir(), "cache.sqlite3")


def settings_path() -> str:
    return os.path.join(app_data_dir(), "settings.json")


def load_settings() -> dict:
    try:
        with open(settings_path(), "r", encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:
        return {}


def save_settings(data: dict) -> None:
    try:
        with open(settings_path(), "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2)
    except Exception:
        pass


# ----------------------------------------------------------------------------
# Errors
# ----------------------------------------------------------------------------
class Cancelled(Exception):
    pass


class NotFound(Exception):
    pass


class SecAccessDenied(Exception):
    pass


# ----------------------------------------------------------------------------
# HTTP client with rate limiting, gzip and retry
# ----------------------------------------------------------------------------
class SecClient:
    def __init__(self, user_agent: str, cancel_event: threading.Event,
                 rps: float = MAX_REQUESTS_PER_SECOND):
        self.user_agent = user_agent.strip()
        self.cancel = cancel_event
        self._interval = 1.0 / rps
        self._lock = threading.Lock()
        self._next_slot = 0.0
        self.request_count = 0

    def _sleep(self, seconds: float) -> None:
        if seconds > 0 and self.cancel.wait(seconds):
            raise Cancelled()
        if self.cancel.is_set():
            raise Cancelled()

    def _throttle(self) -> None:
        with self._lock:
            now = time.monotonic()
            slot = max(now, self._next_slot)
            self._next_slot = slot + self._interval
        self._sleep(slot - now)

    @staticmethod
    def _decode(data: bytes, encoding: str) -> bytes:
        encoding = (encoding or "").lower()
        if encoding == "gzip" or data[:2] == b"\x1f\x8b":
            return gzip.decompress(data)
        if encoding == "deflate":
            try:
                return zlib.decompress(data)
            except zlib.error:
                return zlib.decompress(data, -zlib.MAX_WBITS)
        return data

    def get(self, url: str, retries: int = 4) -> bytes:
        last_error: Exception | None = None
        for attempt in range(retries + 1):
            if self.cancel.is_set():
                raise Cancelled()
            self._throttle()
            req = urllib.request.Request(url, headers={
                "User-Agent": self.user_agent,
                "Accept-Encoding": "gzip, deflate",
            })
            try:
                with urllib.request.urlopen(req, timeout=30) as resp:
                    data = resp.read()
                    enc = resp.headers.get("Content-Encoding", "")
                self.request_count += 1
                return self._decode(data, enc)
            except urllib.error.HTTPError as exc:
                self.request_count += 1
                if exc.code == 404:
                    raise NotFound(url) from None
                try:
                    body = exc.read()[:4000]
                except Exception:
                    body = b""
                if exc.code == 403 and (b"Undeclared Automated Tool" in body
                                        or b"User-Agent" in body
                                        or b"user agent" in body.lower()):
                    raise SecAccessDenied(
                        "SEC rejected the request because of the User-Agent. Enter your real "
                        "name and email in the 'SEC contact' box (e.g. 'Jane Doe jane@example.com')."
                    ) from None
                if exc.code in (403, 429, 500, 502, 503, 504):
                    last_error = exc
                    if exc.code == 403 and attempt >= 2:
                        break
                    wait = min(60.0, (2 ** attempt) * (5.0 if exc.code in (403, 429) else 1.5))
                    self._sleep(wait)
                    continue
                raise
            except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as exc:
                last_error = exc
                self._sleep(min(30.0, 2 ** attempt))
        if isinstance(last_error, urllib.error.HTTPError) and last_error.code == 403:
            raise SecAccessDenied(f"SEC returned 403 Forbidden for {url}")
        raise last_error if last_error else RuntimeError(f"Failed to fetch {url}")


# ----------------------------------------------------------------------------
# Local cache (sqlite, stdlib)
# ----------------------------------------------------------------------------
class Cache:
    def __init__(self, path: str):
        self.conn = sqlite3.connect(path, timeout=30, check_same_thread=False)
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=NORMAL")
        self.conn.execute("CREATE TABLE IF NOT EXISTS filings (acc TEXT PRIMARY KEY, ver INTEGER, data TEXT)")
        self.conn.execute("CREATE TABLE IF NOT EXISTS blobs (key TEXT PRIMARY KEY, ts REAL, data BLOB)")
        self.conn.commit()
        self._pending = 0

    def get_filing(self, acc: str) -> dict | None:
        row = self.conn.execute("SELECT data FROM filings WHERE acc=? AND ver=?",
                                (acc, CACHE_VERSION)).fetchone()
        return json.loads(row[0]) if row else None

    def put_filing(self, acc: str, record: dict) -> None:
        self.conn.execute("INSERT OR REPLACE INTO filings (acc, ver, data) VALUES (?,?,?)",
                          (acc, CACHE_VERSION, json.dumps(record, separators=(",", ":"))))
        self._pending += 1
        if self._pending >= 100:
            self.flush()

    def get_blob(self, key: str, max_age: float | None = None) -> bytes | None:
        row = self.conn.execute("SELECT ts, data FROM blobs WHERE key=?", (key,)).fetchone()
        if not row:
            return None
        if max_age is not None and time.time() - row[0] > max_age:
            return None
        return row[1]

    def put_blob(self, key: str, data: bytes) -> None:
        self.conn.execute("INSERT OR REPLACE INTO blobs (key, ts, data) VALUES (?,?,?)",
                          (key, time.time(), data))
        self.flush()

    def flush(self) -> None:
        self.conn.commit()
        self._pending = 0

    def close(self) -> None:
        try:
            self.flush()
            self.conn.close()
        except Exception:
            pass

    @staticmethod
    def clear_all() -> None:
        conn = sqlite3.connect(cache_path(), timeout=30)
        conn.execute("DROP TABLE IF EXISTS filings")
        conn.execute("DROP TABLE IF EXISTS blobs")
        conn.commit()
        conn.execute("VACUUM")
        conn.close()


# ----------------------------------------------------------------------------
# Form 4 parsing
# ----------------------------------------------------------------------------
def _strip_ns(root: ET.Element) -> None:
    for el in root.iter():
        if isinstance(el.tag, str) and "}" in el.tag:
            el.tag = el.tag.split("}", 1)[1]


def _txt(el: ET.Element | None, path: str) -> str:
    if el is None:
        return ""
    node = el.find(path)
    if node is None or node.text is None:
        return ""
    return node.text.strip()


def _val(el: ET.Element | None, path: str) -> str:
    return _txt(el, path + "/value") or _txt(el, path)


def _flag(el: ET.Element | None, path: str) -> bool:
    return _txt(el, path).lower() in ("1", "true", "yes")


def _num(text: str) -> float | None:
    text = (text or "").replace(",", "").replace("$", "").strip()
    if not text:
        return None
    try:
        return float(text)
    except ValueError:
        return None


# Purchases made in a company offering / private placement rather than on the open market
OFFERING_RE = re.compile(
    r"(?:underwritten|registered\s+direct|public|secondary|follow[- ]on|initial\s+public|best[- ]efforts|"
    r"at[- ]the[- ]market|concurrent)\s+offering|\bIPO\b|private\s+placement|rights\s+offering|"
    r"subscription\s+agreement|securities\s+purchase\s+agreement|\bPIPE\b|directed\s+share\s+program|"
    r"\bin\s+(?:the\s+)?(?:[\w'-]+\s+){0,3}offering\b|\bpurchased\s+from\s+the\s+(?:issuer|company)\b", re.I)
# Reporting owners that are funds / companies rather than people
ENTITY_RE = re.compile(
    r"\b(?:L\.?P\.?|LLC|L\.L\.C\.?|LLP|Ltd\.?|Limited|Inc\.?|Incorporated|Corp\.?|Corporation|Fund|Funds|"
    r"Capital|Partners|Partnership|Holdings?|Management|Advisors|Advisers|Investments?|Ventures|Group|"
    r"Company|Co\.|AG|GmbH|S\.A\.|N\.V\.|PLC|Foundation|Endowment|Bank|Trust\s+Company)(?=\W|$)", re.I)


def is_entity(name: str) -> bool:
    return bool(ENTITY_RE.search(name or ""))


PLAN_RE = re.compile(r"10b5\s*-?\s*1", re.I)
NOT_PLAN_RE = re.compile(r"\bnot\b[^.;]{0,80}10b5\s*-?\s*1|10b5\s*-?\s*1[^.;]{0,40}\b(?:terminated|cancell?ed)\b", re.I)


def _footnote_ids(el: ET.Element) -> list:
    return [f.get("id", "") for f in el.iter("footnoteId") if f.get("id")]


def parse_ownership_xml(xml_bytes: bytes, record: dict) -> dict:
    root = ET.fromstring(xml_bytes)
    _strip_ns(root)
    record["schema"] = _txt(root, "schemaVersion")
    # Form 4 checkbox (since 2023): "transaction made pursuant to a 10b5-1 plan"
    record["plan10b5"] = _flag(root, "aff10b5One")
    plan_notes, offer_notes = [], []
    for fn in root.findall("footnotes/footnote"):
        text = " ".join("".join(fn.itertext()).split())
        if PLAN_RE.search(text) and not NOT_PLAN_RE.search(text):
            plan_notes.append(fn.get("id", ""))
        if OFFERING_RE.search(text):
            offer_notes.append(fn.get("id", ""))
    remarks = " ".join(_txt(root, "remarks").split())
    record["offering_doc"] = bool(OFFERING_RE.search(remarks))
    if not record.get("form"):
        record["form"] = _txt(root, "documentType")
    issuer = root.find("issuer")
    record["issuer_cik"] = _txt(issuer, "issuerCik").lstrip("0")
    record["issuer_name"] = _txt(issuer, "issuerName")
    record["ticker"] = _txt(issuer, "issuerTradingSymbol").upper()
    owners = []
    for ro in root.findall("reportingOwner"):
        rid = ro.find("reportingOwnerId")
        rel = ro.find("reportingOwnerRelationship")
        owners.append({
            "name": _txt(rid, "rptOwnerName"),
            "cik": _txt(rid, "rptOwnerCik").lstrip("0"),
            "dir": _flag(rel, "isDirector"),
            "off": _flag(rel, "isOfficer"),
            "ten": _flag(rel, "isTenPercentOwner"),
            "oth": _flag(rel, "isOther"),
            "title": _txt(rel, "officerTitle"),
            "other": _txt(rel, "otherText"),
        })
    record["owners"] = owners
    txns = []
    for table, tag in (("N", "nonDerivativeTable/nonDerivativeTransaction"),
                       ("D", "derivativeTable/derivativeTransaction")):
        for t in root.findall(tag):
            txns.append({
                "t": table,
                "code": _txt(t, "transactionCoding/transactionCode").upper(),
                "sh": _num(_val(t, "transactionAmounts/transactionShares")),
                "px": _num(_val(t, "transactionAmounts/transactionPricePerShare")),
                "ad": _val(t, "transactionAmounts/transactionAcquiredDisposedCode").upper(),
                "date": _val(t, "transactionDate")[:10],
                "sec": _val(t, "securityTitle"),
                "after": _num(_val(t, "postTransactionAmounts/sharesOwnedFollowingTransaction")),
                "own": _val(t, "ownershipNature/directOrIndirectOwnership").upper() or "D",
                "nat": _val(t, "ownershipNature/natureOfOwnership"),
                "plan": bool(set(_footnote_ids(t)) & set(plan_notes)),
                "offer": bool(set(_footnote_ids(t)) & set(offer_notes)),
            })
    record["txns"] = txns
    return record


def parse_submission(raw: bytes, accession: str, fallback_date: str = "") -> dict:
    """Parse a full EDGAR submission (.txt) that contains a Form 4 XML document."""
    record = {"acc": accession, "filing_date": fallback_date, "form": "", "issuer_cik": "",
              "issuer_name": "", "ticker": "", "schema": "", "xml_file": "",
              "owners": [], "txns": [], "plan10b5": False, "error": ""}
    m = re.search(rb"FILED AS OF DATE:\s*(\d{8})", raw)
    if m:
        d = m.group(1).decode()
        record["filing_date"] = f"{d[:4]}-{d[4:6]}-{d[6:]}"
    m = re.search(rb"CONFORMED SUBMISSION TYPE:\s*(\S+)", raw)
    if m:
        record["form"] = m.group(1).decode("latin-1")

    xml_bytes = None
    for doc in re.finditer(rb"<DOCUMENT>(.*?)</DOCUMENT>", raw, re.S | re.I):
        body = doc.group(1)
        xm = re.search(rb"<XML>\s*(.*?)\s*</XML>", body, re.S | re.I)
        if xm and b"ownershipDocument" in xm.group(1):
            xml_bytes = xm.group(1)
            fn = re.search(rb"<FILENAME>([^\r\n<]+)", body, re.I)
            if fn:
                record["xml_file"] = fn.group(1).strip().decode("latin-1")
            break
    if xml_bytes is None and b"<ownershipDocument" in raw:      # bare XML document
        xml_bytes = raw[raw.find(b"<"):]
    if xml_bytes is None:
        record["error"] = "No ownershipDocument XML found"
        return record
    try:
        parse_ownership_xml(xml_bytes, record)
    except ET.ParseError as exc:
        record["error"] = f"XML parse error: {exc}"
    return record


def owner_roles(owner: dict) -> set:
    text = " | ".join(x for x in (owner.get("title", ""), owner.get("other", "")) if x)
    roles = set()
    for role in (ROLE_CEO, ROLE_CFO, ROLE_LEGAL):
        if ROLE_PATTERNS[role].search(text):
            roles.add(role)
    if owner.get("dir") or owner.get("ten"):
        roles.add(ROLE_DIR)
    else:
        for part in (owner.get("title", ""), owner.get("other", "")):
            if part and ROLE_PATTERNS[ROLE_DIR].search(part):
                roles.add(ROLE_DIR)
    return roles


def owner_title_text(owner: dict) -> str:
    parts = []
    if owner.get("title"):
        parts.append(owner["title"])
    elif owner.get("off"):
        parts.append("Officer")
    if owner.get("dir"):
        parts.append("Director")
    if owner.get("ten"):
        parts.append("10% Owner")
    if owner.get("other"):
        parts.append(owner["other"])
    elif owner.get("oth") and not parts:
        parts.append("Other")
    seen, out = set(), []
    for p in parts:
        if p.lower() not in seen:
            seen.add(p.lower())
            out.append(p)
    return ", ".join(out)


def stake_change_pct(lots: list) -> float | None:
    """% change in the insider's holding caused by these lots.

    Uses 'shares owned following transaction' per ownership line (direct, or each
    indirect holder). Returns +inf for a brand-new position, None if unknown.
    """
    lines: dict = {}
    for x in lots:
        lines.setdefault((x.get("own") or "D", x.get("nat", "")), []).append(x)
    before_total = after_total = 0.0
    for group in lines.values():
        after = group[-1].get("after")
        if after is None:
            return None
        signed = sum((g.get("sh") or 0.0) * (-1 if g.get("ad") == "D" else 1) for g in group)
        before_total += max(0.0, after - signed)
        after_total += after
    if before_total <= 0:
        return float("inf") if after_total > 0 else None
    return (after_total - before_total) / before_total * 100.0


def holdings_after(lots: list) -> float | None:
    """Total shares held (across the traded ownership lines) right after these lots."""
    lines: dict = {}
    for x in lots:
        lines.setdefault((x.get("own") or "D", x.get("nat", "")), []).append(x)
    total = 0.0
    for group in lines.values():
        after = group[-1].get("after")
        if after is None:
            return None
        total += after
    return total


def _years_ago(d: dt.date, years: int) -> dt.date:
    try:
        return d.replace(year=d.year - years)
    except ValueError:                       # Feb 29
        return d.replace(year=d.year - years, day=28)


def _to_date(text: str) -> dt.date | None:
    try:
        return dt.date.fromisoformat((text or "")[:10])
    except ValueError:
        return None


def classify_pattern(trade_date: dt.date, prior_buys: list, years: int = HISTORY_YEARS) -> tuple[str, str]:
    """Classify a purchase by the insider's earlier open-market buys in the same company.

    routine  - bought around the same time of year (+/-3 weeks) in each of the previous 3 years
               (Cohen, Malloy & Pomorski's definition of a routine trader, with a small date window)
    regular  - bought in 3+ of the previous 4 calendar quarters, or same time of year in 2 of 3 years
    first    - no earlier open-market buys in the look-back window
    opp      - has bought before, but not on a schedule ("opportunistic")
    Buys within 30 days before this one are treated as part of the same buying spree.
    """
    cutoff = trade_date - dt.timedelta(days=30)
    prior = sorted(d for d in prior_buys if d and d < cutoff)
    if not prior:
        return "first", f"First buy in {years}+ yrs"
    hits = [k for k in (1, 2, 3)
            if any(abs((d - _years_ago(trade_date, k)).days) <= 21 for d in prior)]
    if len(hits) == 3:
        return "routine", f"Routine (yearly since {trade_date.year - 3})"

    def qtr(d: dt.date) -> int:
        return d.year * 4 + (d.month - 1) // 3
    cq = qtr(trade_date)
    quarters = {qtr(d) for d in prior if cq - 4 <= qtr(d) < cq}
    if len(quarters) >= 3:
        return "regular", f"Regular ({len(quarters)} of last 4 qtrs)"
    if len(hits) == 2:
        return "regular", "Regular (2 of last 3 yrs)"
    return "opp", f"Opportunistic (last {prior[-1]:%b %Y})"


def classify_later_sales(trade_start: dt.date, records: list, held_after: float | None) -> tuple[str, str, float | None]:
    """Did the insider sell (Code S) on or after the purchase date? Tax withholding (F) is reported separately."""
    sold, tax, first = 0.0, 0.0, None
    for rec in records:
        for t in rec.get("txns", []):
            if t.get("t") != "N":
                continue
            d = _to_date(t.get("date", "")) or _to_date(rec.get("filing_date", ""))
            if not d or d < trade_start:
                continue
            if t.get("code") == "S":
                sold += t.get("sh") or 0.0
                first = d if first is None else min(first, d)
            elif t.get("code") == "F":
                tax += t.get("sh") or 0.0
    if sold > 0:
        if held_after and held_after > 0:
            pct = min(100.0, sold / held_after * 100.0)
            return "sold", f"Sold {pct:.0f}% ({first:%b %d})", pct
        return "sold", f"Sold {sold:,.0f} sh ({first:%b %d})", None
    if tax > 0:
        return "tax", "No (tax w/h only)", 0.0
    return "none", "No", 0.0


# ----------------------------------------------------------------------------
# Company & price context
# ----------------------------------------------------------------------------
def latest_shares_outstanding(data: dict) -> float | None:
    """Most recent shares-outstanding figure from an XBRL companyconcept response."""
    facts = [f for f in ((data.get("units") or {}).get("shares") or []) if f.get("val")]
    if not facts:
        return None
    latest_filed = max(f.get("filed", "") for f in facts)
    same_filing = [f for f in facts if f.get("filed", "") == latest_filed]
    latest_end = max(f.get("end", "") for f in same_filing)
    vals = {float(f["val"]) for f in same_filing if f.get("end", "") == latest_end}
    return sum(vals) if vals else None        # several distinct values = several share classes


def yahoo_symbol(ticker: str) -> str | None:
    sym = (ticker or "").strip().upper().replace(".", "-").replace("/", "-")
    if not sym or sym in ("NONE", "N/A", "NA", "-") or not re.fullmatch(r"[A-Z0-9\-]{1,10}", sym):
        return None
    return sym


def parse_yahoo_chart(raw: bytes) -> list | None:
    """Yahoo v8 chart JSON -> [(iso date, close, adjusted close), ...] oldest first."""
    try:
        res = json.loads(raw)["chart"]["result"]
    except (ValueError, KeyError, TypeError):
        return None
    if not res:
        return None
    r = res[0]
    ts = r.get("timestamp") or []
    ind = r.get("indicators") or {}
    closes = ((ind.get("quote") or [{}])[0].get("close")) or []
    adj = ((ind.get("adjclose") or [{}])[0].get("adjclose")) or closes
    offset = (r.get("meta") or {}).get("gmtoffset") or 0
    bars = []
    for t, c, a in zip(ts, closes, adj):
        if c is None or t is None:
            continue
        d = dt.datetime.fromtimestamp(t + offset, dt.timezone.utc).date().isoformat()
        bars.append((d, float(c), float(a if a is not None else c)))
    return bars or None


def price_metrics(bars: list, buy_date: dt.date, paid: float | None) -> dict:
    """Split/dividend-safe price context around an insider purchase."""
    if not bars or buy_date is None:
        return {}
    iso = buy_date.isoformat()
    upto = [b for b in bars if b[0] <= iso]
    last = bars[-1]
    out = {"last_close": last[1], "last_date": last[0]}
    if not upto:
        return out
    _, close_buy, adj_buy = upto[-1]
    year_ago = (buy_date - dt.timedelta(days=365)).isoformat()
    window = [b[2] for b in upto if b[0] >= year_ago]
    if window:
        out["off_high"] = (adj_buy / max(window) - 1.0) * 100.0
    before = [b for b in upto if b[0] <= (buy_date - dt.timedelta(days=91)).isoformat()]
    if before:
        out["chg3m"] = (adj_buy / before[-1][2] - 1.0) * 100.0
    ratio = last[2] / adj_buy if adj_buy else None
    if ratio is not None:
        basis = (close_buy / paid) if paid and paid > 0 else 1.0
        out["since_buy"] = (ratio * basis - 1.0) * 100.0
    return out


def insider_net_flow(records: list, start: dt.date, end: dt.date) -> dict:
    """Open-market buying vs selling by all insiders of a company between two dates."""
    buy = sell = 0.0
    buyers, sellers = set(), set()
    for rec in records:
        if rec.get("error") or not rec.get("owners"):
            continue
        who = owner_key(rec)
        for t in rec.get("txns", []):
            if t.get("t") != "N" or t.get("code") not in ("P", "S"):
                continue
            d = _to_date(t.get("date", ""))
            if not d or d < start or d > end:
                continue
            v = (t.get("sh") or 0.0) * (t.get("px") or 0.0)
            if t["code"] == "P":
                buy += v
                buyers.add(who)
            else:
                sell += v
                sellers.add(who)
    return {"buy": buy, "sell": sell, "buyers": len(buyers), "sellers": len(sellers)}


def fmt_net90(flow: dict | None) -> str:
    if not flow:
        return ""
    net = flow["buy"] - flow["sell"]
    if not flow["buy"] and not flow["sell"]:
        return "$0"
    sign = "+" if net >= 0 else "-"
    return f"{sign}{fmt_compact(abs(net))} ({flow['buyers']}B/{flow['sellers']}S)"


def fmt_pct(v: float | None, plus: bool = True, digits: int = 1) -> str:
    if v is None:
        return ""
    return f"{v:+.{digits}f}%" if plus else f"{v:.{digits}f}%"


# ----------------------------------------------------------------------------
# Signal Score
# ----------------------------------------------------------------------------
def signal_score(r: dict) -> tuple[int | None, list]:
    """0-100 score for an open-market buy, with the reasons behind it.

    Starts at 40 and adds/subtracts points for what research and practice associate with
    informative insider buying. Missing data simply adds nothing.
    """
    if r.get("code") != "P":
        return None, []
    pts, why = 40, []

    def add(n: int, text: str) -> None:
        nonlocal pts
        pts += n
        why.append(f"{n:+d}  {text}")

    n = int(r.get("cluster", 1) or 1)
    if n >= 4:
        add(22, f"{n} different insiders buying (cluster)")
    elif n == 3:
        add(18, "3 different insiders buying (cluster)")
    elif n == 2:
        add(12, "2 different insiders buying (cluster)")
    roles = set(r.get("roles_all") or [])
    if ROLE_CEO in roles or ROLE_CFO in roles:
        add(8, "CEO or CFO is the buyer")
    elif ROLE_DIR in roles and not r.get("entity"):
        add(3, "director is the buyer")
    v = r.get("value") or 0
    if v >= 1_000_000:
        add(6, "purchase of $1M or more")
    elif v >= 250_000:
        add(3, "purchase of $250k or more")
    st = r.get("stake")
    if st is not None:
        if st == float("inf") or st >= 100:
            add(12, "new position or more than doubled holding")
        elif st >= 50:
            add(9, f"holding up {st:.0f}%")
        elif st >= 25:
            add(6, f"holding up {st:.0f}%")
        elif st >= 10:
            add(3, f"holding up {st:.0f}%")
    pk = r.get("pattern_kind")
    if pk == "first":
        add(10, "first open-market buy in 5+ years")
    elif pk == "opp":
        add(5, "opportunistic (no buying schedule)")
    elif pk == "regular":
        add(-12, "regular buyer (on a schedule)")
    elif pk == "routine":
        add(-20, "routine buyer (same time every year)")
    if r.get("sold_kind") == "sold":
        pct = r.get("sold_pct")
        add(-25 if (pct is None or pct >= 25) else -10, "has sold shares since buying")
    if r.get("plan"):
        add(-15, "made under a 10b5-1 plan")
    if r.get("offering"):
        add(-25, "bought in a company offering / private placement")
    if r.get("entity"):
        add(-15, "buyer is a fund or company, not a person")
    pc = r.get("cluster_pct_co")
    if pc is not None:
        if pc >= 1:
            add(10, f"insiders bought {pc:.2f}% of the company")
        elif pc >= 0.25:
            add(7, f"insiders bought {pc:.2f}% of the company")
        elif pc >= 0.05:
            add(4, f"insiders bought {pc:.2f}% of the company")
    mc = r.get("mcap")
    if mc:
        if mc < 300e6:
            add(6, "micro/small cap (under $300M)")
        elif mc < 2e9:
            add(4, "small cap (under $2B)")
        elif mc < 10e9:
            add(1, "mid cap (under $10B)")
        elif mc >= 200e9:
            add(-3, "mega cap ($200B+)")
    oh = r.get("off_high")
    if oh is not None:
        if oh <= -40:
            add(7, f"bought {abs(oh):.0f}% below 52-week high")
        elif oh <= -20:
            add(4, f"bought {abs(oh):.0f}% below 52-week high")
    c3 = r.get("chg3m")
    if c3 is not None and c3 <= -20:
        add(3, f"stock fell {abs(c3):.0f}% in the 3 months before")
    sb = r.get("since")
    if sb is not None and sb >= 30:
        add(-6, f"already up {sb:.0f}% since the purchase")
    f = r.get("net90")
    if f:
        if f["sell"] >= 250_000 and f["sell"] > 3 * max(f["buy"], 1.0):
            add(-8, "insiders were heavy net sellers over 90 days")
        elif f["buy"] > f["sell"]:
            add(4, "insiders net buyers over 90 days")
    if r.get("d13"):
        add(5, "Schedule 13D (5%+ stake) filed in last 90 days")
    # Disqualifiers cap the score however good everything else looks
    caps = []
    if r.get("offering"):
        caps.append((30, "offering / placement purchase"))
    if r.get("plan"):
        caps.append((45, "10b5-1 plan purchase"))
    if pk == "routine":
        caps.append((45, "routine buyer"))
    if r.get("sold_kind") == "sold" and (r.get("sold_pct") is None or r.get("sold_pct") >= 25):
        caps.append((40, "sold 25%+ since"))
    for cap, text in sorted(caps):
        if pts > cap:
            why.append(f"capped at {cap}: {text}")
            pts = cap
            break
    return max(0, min(100, int(round(pts)))), why


# ----------------------------------------------------------------------------
# Track record of signals (kept even if the download cache is cleared)
# ----------------------------------------------------------------------------
class SignalLog:
    def __init__(self, path: str | None = None):
        self.conn = sqlite3.connect(path or cache_path(), timeout=30)
        self.conn.execute("""CREATE TABLE IF NOT EXISTS signals (
            acc TEXT PRIMARY KEY, ticker TEXT, company TEXT, insider TEXT, buy_date TEXT,
            price REAL, value REAL, score INTEGER, first_seen TEXT, pattern TEXT, cluster INTEGER)""")
        self.conn.commit()

    def log(self, rows: list) -> int:
        n = 0
        today = dt.date.today().isoformat()
        for r in rows:
            if r.get("code") != "P" or r.get("score") is None or not r.get("ticker"):
                continue
            cur = self.conn.execute(
                "INSERT OR IGNORE INTO signals VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (r["acc"], r["ticker"], r.get("company", ""), r.get("insider", ""),
                 r.get("trade_start") or r.get("filing_date"), r.get("price"), r.get("value"),
                 r["score"], today, r.get("pattern", ""), r.get("cluster", 1)))
            n += cur.rowcount
        self.conn.commit()
        return n

    def all(self) -> list[dict]:
        cur = self.conn.execute("SELECT * FROM signals ORDER BY buy_date DESC")
        cols = [c[0] for c in cur.description]
        return [dict(zip(cols, row)) for row in cur.fetchall()]

    def clear(self) -> None:
        self.conn.execute("DELETE FROM signals")
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()


def score_band(score: int | None) -> str:
    if score is None:
        return "?"
    return "70+" if score >= 70 else ("50-69" if score >= 50 else "under 50")


def track_record_summary(items: list) -> dict:
    """items: dicts with score, ret, excess -> per score band: count, avg return, avg vs SPY, % beating SPY."""
    out = {}
    for band in ("70+", "50-69", "under 50"):
        sel = [x for x in items if score_band(x.get("score")) == band and x.get("ret") is not None]
        if not sel:
            out[band] = None
            continue
        ex = [x["excess"] for x in sel if x.get("excess") is not None]
        out[band] = {"n": len(sel), "avg": sum(x["ret"] for x in sel) / len(sel),
                     "avg_excess": (sum(ex) / len(ex)) if ex else None,
                     "beat": (sum(1 for e in ex if e > 0) / len(ex) * 100) if ex else None}
    return out


class PriceClient(SecClient):
    """Daily prices from Yahoo Finance's public chart endpoint (no key needed)."""

    def __init__(self, cancel_event: threading.Event):
        super().__init__(BROWSER_UA, cancel_event, rps=PRICE_RPS)

    def bars(self, sym: str) -> list | None:
        q = urllib.parse.quote(sym)
        for host in ("query1", "query2"):
            url = f"https://{host}.finance.yahoo.com/v8/finance/chart/{q}?range=2y&interval=1d&includeAdjustedClose=true"
            try:
                raw = self.get(url, retries=2)
            except NotFound:
                return None
            except Cancelled:
                raise
            except Exception:
                continue
            b = parse_yahoo_chart(raw)
            if b:
                return b
        return None


def fmt_stake(pct: float | None) -> str:
    if pct is None:
        return ""
    if pct == float("inf"):
        return "New"
    return f"{pct:+,.1f}%"


def owner_key(record: dict) -> str:
    o = record["owners"][0]
    return o.get("cik") or o.get("name", "").lower()


def sec_links(record: dict, fallback_cik: str = "") -> tuple[str, str]:
    """Return (rendered Form 4 URL, filing index URL)."""
    acc = record["acc"]
    nodash = acc.replace("-", "")
    cik = record.get("issuer_cik") or fallback_cik
    base = f"{SEC_WWW}/Archives/edgar/data/{cik}/{nodash}"
    index_url = f"{base}/{acc}-index.htm"
    schema, xml_file = record.get("schema", ""), record.get("xml_file", "")
    if xml_file and re.fullmatch(r"X0\d{3}", schema or ""):
        return f"{base}/xslF345{schema[:3]}/{xml_file}", index_url
    return index_url, index_url


# ----------------------------------------------------------------------------
# Scan parameters and filtering
# ----------------------------------------------------------------------------
@dataclass
class ScanParams:
    start: dt.date
    end: dt.date
    user_agent: str
    tickers: list = field(default_factory=list)
    roles: set = field(default_factory=set)          # empty = any insider
    codes: set = field(default_factory=lambda: {"P"})
    min_value: float = 50000.0
    include_amendments: bool = False
    combine_lots: bool = True
    check_history: bool = True                      # pattern + later-sales lookup for buyers


def cluster_key(record: dict, code: str) -> str:
    return f"{record.get('issuer_cik') or record.get('ticker', '')}|{code}"


def issuer_matches(record: dict, cand: dict) -> bool:
    want = cand.get("issuer_cik")
    return not (want and record.get("issuer_cik") and str(want) != str(record["issuer_cik"]))


def evaluate_record(record: dict, cand: dict, params: ScanParams) -> list[dict]:
    """Apply role / code / value filters to one parsed filing; return result rows."""
    if record.get("error") or not record.get("owners"):
        return []
    if not issuer_matches(record, cand):
        return []          # company is the *reporting owner* here, not the issuer

    all_roles: set = set()
    for o in record["owners"]:
        all_roles |= owner_roles(o)
    if params.roles:
        matched = all_roles & params.roles
        if not matched:
            return []
    else:
        matched = all_roles
    role_text = ", ".join(r for r in ROLE_ORDER if r in matched)
    if not role_text:
        role_text = "Officer" if any(o.get("off") for o in record["owners"]) else "Other"

    names = [o["name"] for o in record["owners"] if o.get("name")]
    titles = [owner_title_text(o) for o in record["owners"]]
    insider = "; ".join(names)
    title = "; ".join(t for t in titles if t)

    groups: dict[str, list] = {}
    for t in record.get("txns", []):
        # Common-stock (non-derivative) table only: avoids double counting option
        # exercises and meaningless "values" on option grants.
        if t.get("t") != "N" or t.get("code") not in params.codes:
            continue
        groups.setdefault(t["code"], []).append(t)

    link, index_link = sec_links(record, cand.get("path_cik", ""))
    rows = []
    for code in sorted(groups):
        items = groups[code]
        chunks = [items] if params.combine_lots else [[x] for x in items]
        for chunk in chunks:
            shares = sum((x.get("sh") or 0.0) for x in chunk)
            value = sum((x.get("sh") or 0.0) * (x.get("px") or 0.0) for x in chunk)
            price = (value / shares) if shares else (chunk[0].get("px") or 0.0)
            if value < params.min_value:
                continue
            dates = sorted({x.get("date", "") for x in chunk if x.get("date")})
            plan = bool(record.get("plan10b5")) or any(x.get("plan") for x in chunk)
            offering = bool(record.get("offering_doc")) or any(x.get("offer") for x in chunk)
            rows.append({
                "filing_date": record.get("filing_date") or cand.get("date", ""),
                "ticker": record.get("ticker") or cand.get("ticker", "") or "",
                "company": record.get("issuer_name", ""),
                "insider": insider,
                "role": role_text,
                "title": title,
                "side": TXN_CODES.get(code, ("", code))[1],
                "code": code,
                "shares": shares,
                "price": price,
                "value": value,
                "link": link,
                "index_link": index_link,
                "trade_dates": ", ".join(dates),
                "lots": len(chunk),
                "form": record.get("form", ""),
                "acc": record["acc"],
                "stake": stake_change_pct(chunk),
                "plan": plan,
                "cluster_key": cluster_key(record, code),
                "cluster": 1,
                "cluster_disc": 0 if plan else 1,
                "owner_cik": record["owners"][0].get("cik", ""),
                "issuer_cik": record.get("issuer_cik", "") or cand.get("issuer_cik") or "",
                "trade_start": dates[0] if dates else (record.get("filing_date") or ""),
                "held_after": holdings_after(chunk),
                "pattern": "\u2026" if (code == "P" and params.check_history) else "",
                "pattern_kind": "pending" if (code == "P" and params.check_history) else "na",
                "sold": "\u2026" if (code == "P" and params.check_history) else "",
                "sold_kind": "pending" if (code == "P" and params.check_history) else "na",
                "sold_pct": None,
                "offering": offering,
                "entity": is_entity(record["owners"][0].get("name", "")),
                "roles_all": sorted(all_roles),
                "deep": bool(code == "P" and params.check_history),
            })
    return rows


# ----------------------------------------------------------------------------
# EDGAR discovery
# ----------------------------------------------------------------------------
def load_ticker_map(client: SecClient, cache: Cache) -> dict:
    raw = cache.get_blob("company_tickers", max_age=3 * 86400)
    if raw is None:
        raw = client.get(f"{SEC_WWW}/files/company_tickers.json")
        cache.put_blob("company_tickers", raw)
    data = json.loads(raw)
    out = {}
    for v in data.values():
        out[str(v["ticker"]).upper()] = (int(v["cik_str"]), v.get("title", ""))
    return out


def daily_index_entries(client: SecClient, cache: Cache, day: dt.date) -> list | None:
    """Form 4 / 4/A entries from EDGAR's daily master index. None = no index for that day."""
    key = f"dailyidx:{day:%Y%m%d}"
    cached = cache.get_blob(key)
    if cached is not None:
        return json.loads(cached)
    qtr = (day.month - 1) // 3 + 1
    url = f"{SEC_WWW}/Archives/edgar/daily-index/{day.year}/QTR{qtr}/master.{day:%Y%m%d}.idx"
    try:
        raw = client.get(url)
    except NotFound:
        return None
    entries = []
    for line in raw.decode("latin-1").splitlines():
        parts = line.split("|")
        if len(parts) != 5:
            continue
        cik, _name, form, date_filed, fname = (p.strip() for p in parts)
        if form not in ("4", "4/A") or not fname.startswith("edgar/"):
            continue
        if re.fullmatch(r"\d{8}", date_filed):
            date_filed = f"{date_filed[:4]}-{date_filed[4:6]}-{date_filed[6:]}"
        entries.append([form, fname, date_filed, cik])
    if day < dt.date.today():
        cache.put_blob(key, json.dumps(entries).encode())
    return entries


# ----------------------------------------------------------------------------
# Scanner (runs on a background thread)
# ----------------------------------------------------------------------------
class Scanner:
    def __init__(self, params: ScanParams, out_q: queue.Queue, cancel: threading.Event, scan_id: int = 0):
        self.p = params
        self.q = out_q
        self.cancel = cancel
        self.scan_id = scan_id
        # cluster_key -> {"all": set(insiders), "disc": set(insiders not trading under a 10b5-1 plan)}
        self.clusters: dict[str, dict] = {}
        self._changed: set = set()
        self.buys: dict[str, dict] = {}       # accession -> purchase info for the history check
        self._lists: dict[str, dict | None] = {}   # CIK -> {"f4": {...}, "d13": [...]}

    def post(self, kind: str, payload=None) -> None:
        self.q.put((self.scan_id, kind, payload))

    def check_cancel(self) -> None:
        if self.cancel.is_set():
            raise Cancelled()

    # -- discovery ---------------------------------------------------------
    def _forms(self) -> set:
        return {"4", "4/A"} if self.p.include_amendments else {"4"}

    def _company_filings(self, client: SecClient, cik: int):
        start, end = self.p.start.isoformat(), self.p.end.isoformat()
        data = json.loads(client.get(f"{SEC_DATA}/submissions/CIK{cik:010d}.json"))
        filings = data.get("filings", {})
        blocks = [filings.get("recent", {})]
        recent_dates = blocks[0].get("filingDate", [])
        oldest = min(recent_dates) if recent_dates else None
        if oldest and oldest > start:
            for f in filings.get("files", []):
                if f.get("filingTo", "") >= start and f.get("filingFrom", "9999") <= end:
                    self.check_cancel()
                    blocks.append(json.loads(client.get(f"{SEC_DATA}/submissions/{f['name']}")))
        for b in blocks:
            for acc, fdate, form in zip(b.get("accessionNumber", []), b.get("filingDate", []), b.get("form", [])):
                if start <= fdate <= end:
                    yield acc, fdate, form

    def _latest_feed(self, client: SecClient, want_days: set, forms: set, cands: dict) -> int:
        """Today's filings aren't in the daily index until evening - use the 'latest filings' feed."""
        added, start = 0, 0
        min_day = min(want_days)
        for _page in range(40):
            self.check_cancel()
            url = (f"{SEC_WWW}/cgi-bin/browse-edgar?action=getcurrent&type=4&company=&dateb="
                   f"&owner=include&start={start}&count=100&output=atom")
            root = ET.fromstring(client.get(url))
            _strip_ns(root)
            entries = root.findall("entry")
            if not entries:
                break
            oldest = None
            for e in entries:
                form = _txt(e, "title").split(" - ", 1)[0].strip()
                updated = _txt(e, "updated")[:10]
                link = e.find("link")
                href = link.get("href", "") if link is not None else ""
                m = re.search(r"/data/(\d+)/(\d{18})/(\d{10}-\d{2}-\d{6})-index", href)
                try:
                    day = dt.date.fromisoformat(updated)
                except ValueError:
                    continue
                oldest = day if oldest is None else min(oldest, day)
                if m and form in forms and day in want_days:
                    cik, nodash, acc = m.groups()
                    if acc not in cands:
                        cands[acc] = {"acc": acc, "path": f"edgar/data/{cik}/{nodash}/{acc}.txt",
                                      "date": updated, "issuer_cik": None, "path_cik": cik}
                        added += 1
            if oldest is not None and oldest < min_day:
                break
            start += 100
        return added

    def gather(self, client: SecClient, cache: Cache) -> dict:
        p, forms, cands = self.p, self._forms(), {}
        if p.tickers:
            self.post("phase", "Loading SEC ticker list...")
            tmap = load_ticker_map(client, cache)
            unknown = []
            for t in p.tickers:
                self.check_cancel()
                hit = tmap.get(t.upper().replace(".", "-")) or tmap.get(t.upper())
                if not hit:
                    unknown.append(t)
                    continue
                cik, _title = hit
                self.post("phase", f"Listing Form 4 filings for {t}...")
                for acc, fdate, form in self._company_filings(client, cik):
                    if form in forms and acc not in cands:
                        cands[acc] = {"acc": acc, "date": fdate, "ticker": t.upper(),
                                      "path": f"edgar/data/{cik}/{acc.replace('-', '')}/{acc}.txt",
                                      "issuer_cik": str(cik), "path_cik": str(cik)}
            if unknown:
                self.post("warn", "Ticker(s) not found in SEC list and skipped: " + ", ".join(unknown))
            return cands

        day, missing, weekdays, denied = p.start, [], 0, 0
        while day <= p.end:
            self.check_cancel()
            if day.weekday() < 5:
                weekdays += 1
                self.post("phase", f"Reading EDGAR daily index for {day:%a %b %d, %Y}...")
                try:
                    entries = daily_index_entries(client, cache, day)
                except SecAccessDenied as exc:
                    if "User-Agent" in str(exc):
                        raise
                    entries, denied = None, denied + 1
                if entries is None:
                    missing.append(day)
                else:
                    for form, fname, fdate, cik in entries:
                        if form not in forms:
                            continue
                        acc = fname.rsplit("/", 1)[-1].replace(".txt", "")
                        if acc not in cands:
                            cands[acc] = {"acc": acc, "path": fname, "date": fdate,
                                          "issuer_cik": None, "path_cik": cik}
            day += dt.timedelta(days=1)
        if weekdays and denied == weekdays:
            raise SecAccessDenied("SEC refused every index request (403). Check your SEC contact / network.")
        recent = {d for d in missing if d >= dt.date.today() - dt.timedelta(days=4)}
        if recent:
            self.post("phase", "Today's index isn't published yet - checking EDGAR latest-filings feed...")
            try:
                self._latest_feed(client, recent, forms, cands)
            except Cancelled:
                raise
            except Exception as exc:  # best effort
                self.post("warn", f"Could not read EDGAR latest-filings feed: {exc}")
        return cands

    def _emit(self, rows: list) -> None:
        self.post("rows", rows)
        for r in rows:
            if r["code"] != "P":
                continue
            b = self.buys.get(r["acc"])
            if b is None:
                self.buys[r["acc"]] = {k: r[k] for k in ("owner_cik", "issuer_cik", "trade_start", "ticker",
                                                          "filing_date", "held_after", "value", "shares", "price")}
            else:
                b["trade_start"] = min(b["trade_start"], r["trade_start"])
                b["value"] += r["value"]
                b["shares"] += r["shares"]
                b["price"] = b["value"] / b["shares"] if b["shares"] else b["price"]
                if r["held_after"] is not None:
                    b["held_after"] = max(b["held_after"] or 0.0, r["held_after"])

    # -- insider history: buying pattern + later sales -------------------------
    @staticmethod
    def _form4_list_task(client: SecClient, cik: int, since: str) -> dict:
        """{"f4": {accession: filing date}, "d13": [[date, form, accession]]} for a CIK since a date."""
        out: dict = {}
        d13: list = []

        def add(block: dict) -> None:
            for acc, fdate, form in zip(block.get("accessionNumber", []), block.get("filingDate", []),
                                        block.get("form", [])):
                if fdate < since:
                    continue
                if form == "4":
                    out[acc] = fdate
                elif re.match(r"(?:SC |SCHEDULE )13D", form or ""):
                    d13.append([fdate, form, acc])
        data = json.loads(client.get(f"{SEC_DATA}/submissions/CIK{cik:010d}.json"))
        filings = data.get("filings", {})
        recent = filings.get("recent", {})
        add(recent)
        dates = recent.get("filingDate", [])
        if dates and min(dates) > since:
            for f in filings.get("files", []):
                if f.get("filingTo", "") >= since:
                    add(json.loads(client.get(f"{SEC_DATA}/submissions/{f['name']}")))
        return {"f4": out, "d13": d13}

    @staticmethod
    def _unknown(reason: str = "Unavailable") -> dict:
        return {"pattern": reason, "pattern_kind": "unknown", "sold": reason, "sold_kind": "unknown", "sold_pct": None}

    def _classify(self, acc: str, buy: dict, accs: set | None, cache: Cache, owner: str, issuer: str) -> dict:
        trade = _to_date(buy["trade_start"]) or _to_date(buy["filing_date"])
        if accs is None or trade is None:
            return self._unknown()
        prior, records = [], []
        for a in accs:
            rec = cache.get_filing(a)
            if not rec or rec.get("error"):
                continue
            if rec.get("issuer_cik") and str(rec["issuer_cik"]) != str(issuer):
                continue
            if not any(o.get("cik") == owner for o in rec.get("owners", [])):
                continue
            records.append(rec)
            if a == acc:
                continue
            for t in rec.get("txns", []):
                if t.get("t") == "N" and t.get("code") == "P":
                    d = _to_date(t.get("date", ""))
                    if d and d < trade:
                        prior.append(d)
        p_kind, p_label = classify_pattern(trade, prior)
        s_kind, s_label, s_pct = classify_later_sales(trade, records, buy.get("held_after"))
        return {"pattern": p_label, "pattern_kind": p_kind, "sold": s_label, "sold_kind": s_kind, "sold_pct": s_pct}

    def history_phase(self, client: SecClient, cache: Cache) -> None:
        since = (_years_ago(dt.date.today(), HISTORY_YEARS) - dt.timedelta(days=120)).isoformat()
        pairs: dict[tuple, list] = {}
        for acc, b in self.buys.items():
            if b["owner_cik"] and b["issuer_cik"]:
                pairs.setdefault((b["owner_cik"], str(b["issuer_cik"])), []).append(acc)
            else:
                self.post("history", {acc: self._unknown()})
        order = sorted(pairs, key=lambda k: -max(self.buys[a]["value"] for a in pairs[k]))
        total, done = len(order), 0
        if not total:
            return
        self.post("phase", f"Checking buying history & later sales for {total:,} insiders...")
        self.post("history_progress", (0, total))
        lists = self._lists
        ex = ThreadPoolExecutor(max_workers=FETCH_WORKERS)
        collect = self._collector()
        try:
            for i in range(0, total, 20):
                batch = order[i:i + 20]
                # 1) Form 4 lists for each insider and company (cached for 6 hours)
                self._load_lists(client, cache, ex, {c for pair in batch for c in pair}, since)

                # 2) filings about this insider AND this company = intersection of the two lists
                pair_accs: dict[tuple, set | None] = {}
                todo: dict[str, dict] = {}
                for pair in batch:
                    o_list, i_list = lists.get(pair[0]), lists.get(pair[1])
                    if o_list is None or i_list is None:
                        pair_accs[pair] = None
                        continue
                    o_f4, i_f4 = o_list["f4"], i_list["f4"]
                    accs = (set(o_f4) & set(i_f4)) | set(pairs[pair])
                    pair_accs[pair] = accs
                    for a in accs:
                        if a not in todo and cache.get_filing(a) is None:
                            todo[a] = {"acc": a, "date": o_f4.get(a) or i_f4.get(a) or "",
                                       "path": f"edgar/data/{pair[1]}/{a.replace('-', '')}/{a}.txt"}
                futs = {ex.submit(self._fetch_one, client, c): a for a, c in todo.items()}
                collect(futs, lambda a, rec: cache.put_filing(a, rec), lambda a: None)

                # 3) classify and send to the window
                results = {}
                for pair in batch:
                    for acc in pairs[pair]:
                        results[acc] = self._classify(acc, self.buys[acc], pair_accs[pair], cache, pair[0], pair[1])
                    done += 1
                cache.flush()
                self.post("history", results)
                self.post("history_progress", (done, total))
        finally:
            ex.shutdown(wait=False, cancel_futures=True)

    @staticmethod
    def _shares_task(client: SecClient, cik: int) -> float | None:
        for concept in ("dei/EntityCommonStockSharesOutstanding", "us-gaap/CommonStockSharesOutstanding"):
            try:
                data = json.loads(client.get(f"{SEC_DATA}/api/xbrl/companyconcept/CIK{cik:010d}/{concept}.json",
                                             retries=2))
            except NotFound:
                continue
            v = latest_shares_outstanding(data)
            if v:
                return v
        return None

    def context_phase(self, client: SecClient, cache: Cache) -> None:
        """Company size, recent insider flow, 13D filings and price context for every buy row."""
        issuers: dict[str, list] = {}
        for acc, b in self.buys.items():
            if b["issuer_cik"]:
                issuers.setdefault(str(b["issuer_cik"]), []).append(acc)
        if not issuers:
            return
        today = dt.date.today()
        start90 = today - dt.timedelta(days=CONTEXT_DAYS)
        since_hist = (_years_ago(today, HISTORY_YEARS) - dt.timedelta(days=120)).isoformat()
        recent_since = (start90 - dt.timedelta(days=3)).isoformat()
        order = sorted(issuers, key=lambda c: -max(self.buys[a]["value"] for a in issuers[c]))
        total, done = len(order), 0
        self.post("phase", f"Checking company size, recent insider activity & prices for {total:,} companies...")
        self.post("context_progress", (0, total))
        prices = PriceClient(self.cancel)
        ex = ThreadPoolExecutor(max_workers=FETCH_WORKERS)
        collect = self._collector()
        try:
            for i in range(0, total, 15):
                batch = order[i:i + 15]
                self._load_lists(client, cache, ex, set(batch), since_hist)
                # 1) the company's Form 4s from the last 90 days (most are cached already)
                todo: dict[str, dict] = {}
                for cik in batch:
                    lst = self._lists.get(cik)
                    for a, fd in ((lst or {}).get("f4") or {}).items():
                        if fd >= recent_since and a not in todo and cache.get_filing(a) is None:
                            todo[a] = {"acc": a, "date": fd, "path": f"edgar/data/{cik}/{a.replace('-', '')}/{a}.txt"}
                futs = {ex.submit(self._fetch_one, client, c): a for a, c in todo.items()}
                collect(futs, lambda a, rec: cache.put_filing(a, rec), lambda a: None)
                # 2) shares outstanding (cached a week)
                shares: dict[str, float | None] = {}
                futs = {}
                for cik in batch:
                    raw = cache.get_blob(f"shares:{cik}", max_age=7 * 86400)
                    if raw is not None:
                        shares[cik] = json.loads(raw)
                    else:
                        futs[ex.submit(self._shares_task, client, int(cik))] = cik

                def ok_shares(cik, v):
                    shares[cik] = v
                    cache.put_blob(f"shares:{cik}", json.dumps(v).encode())
                collect(futs, ok_shares, lambda cik: shares.__setitem__(cik, None))
                # 3) daily prices (cached for the day)
                bars: dict[str, list | None] = {}
                futs = {}
                for cik in batch:
                    for acc in issuers[cik]:
                        sym = yahoo_symbol(self.buys[acc]["ticker"])
                        if not sym or sym in bars or sym in futs.values():
                            continue
                        raw = cache.get_blob(f"px:{sym}:{today}")
                        if raw is not None:
                            bars[sym] = json.loads(raw)
                        else:
                            futs[ex.submit(prices.bars, sym)] = sym

                def ok_bars(sym, v):
                    bars[sym] = v
                    if v:
                        cache.put_blob(f"px:{sym}:{today}", json.dumps(v).encode())
                collect(futs, ok_bars, lambda sym: bars.__setitem__(sym, None))
                # 4) combine
                results = {}
                for cik in batch:
                    lst = self._lists.get(cik)
                    recs = []
                    if lst:
                        for a, fd in lst["f4"].items():
                            if fd >= recent_since:
                                rec = cache.get_filing(a)
                                if rec and not rec.get("error") and str(rec.get("issuer_cik")) == cik:
                                    recs.append(rec)
                    flow = insider_net_flow(recs, start90, today) if lst else None
                    d13 = sorted((d for d in (lst or {}).get("d13", []) if d[0] >= start90.isoformat()), reverse=True)
                    so = shares.get(cik)
                    for acc in issuers[cik]:
                        b = self.buys[acc]
                        sym = yahoo_symbol(b["ticker"])
                        pm = price_metrics(bars.get(sym) or [], _to_date(b["trade_start"]) or _to_date(b["filing_date"]),
                                           b.get("price")) if sym else {}
                        results[acc] = {
                            "shares_out": so,
                            "pct_co": (b["shares"] / so * 100.0) if so else None,
                            "mcap": (so * pm["last_close"]) if (so and pm.get("last_close")) else None,
                            "off_high": pm.get("off_high"), "chg3m": pm.get("chg3m"),
                            "since": pm.get("since_buy"), "last_close": pm.get("last_close"),
                            "net90": flow, "d13": len(d13), "d13_last": d13[0][0] if d13 else "",
                            "prices_ok": bool(pm),
                        }
                    done += 1
                cache.flush()
                self.post("context", results)
                self.post("context_progress", (done, total))
        finally:
            ex.shutdown(wait=False, cancel_futures=True)

    def _collector(self):
        def collect(futures: dict, on_ok, on_fail) -> None:
            for fut in as_completed(futures):
                key = futures[fut]
                try:
                    on_ok(key, fut.result())
                except Cancelled:
                    raise
                except SecAccessDenied as exc:
                    if "User-Agent" in str(exc):
                        raise
                    on_fail(key)
                except Exception:
                    on_fail(key)
            self.check_cancel()
        return collect

    def _load_lists(self, client: SecClient, cache: Cache, ex: ThreadPoolExecutor, ciks: set, since: str) -> None:
        """Fill self._lists for these CIKs (Form 4 + 13D lists; cached 6 hours)."""
        futs = {}
        for cik in ciks:
            if cik in self._lists:
                continue
            raw = cache.get_blob(f"f4list2:{cik}", max_age=6 * 3600)
            if raw is not None:
                self._lists[cik] = json.loads(raw)
            else:
                futs[ex.submit(self._form4_list_task, client, int(cik), since)] = cik

        def ok(cik, data):
            self._lists[cik] = data
            cache.put_blob(f"f4list2:{cik}", json.dumps(data).encode())
        self._collector()(futs, ok, lambda cik: self._lists.__setitem__(cik, None))

    # -- clusters ------------------------------------------------------------
    def track(self, rec: dict, cand: dict) -> None:
        """Record which insiders traded each issuer, by code (any size / role)."""
        if rec.get("error") or not rec.get("owners") or not issuer_matches(rec, cand):
            return
        who = owner_key(rec)
        by_code: dict[str, list] = {}       # code -> [discretionary?, shares all, $ all, shares disc, $ disc]
        for t in rec.get("txns", []):
            if t.get("t") == "N" and t.get("code") in TXN_CODES:
                disc = not (rec.get("plan10b5") or t.get("plan") or t.get("offer") or rec.get("offering_doc"))
                sh, val = (t.get("sh") or 0.0), (t.get("sh") or 0.0) * (t.get("px") or 0.0)
                agg = by_code.setdefault(t["code"], [False, 0.0, 0.0, 0.0, 0.0])
                agg[0] = agg[0] or disc
                agg[1] += sh
                agg[2] += val
                if disc:
                    agg[3] += sh
                    agg[4] += val
        for code, (disc, sh_all, v_all, sh_disc, v_disc) in by_code.items():
            key = cluster_key(rec, code)
            entry = self.clusters.setdefault(key, {"all": set(), "disc": set(), "accs": {}})
            if rec["acc"] in entry["accs"]:
                continue
            entry["accs"][rec["acc"]] = (sh_all, v_all, sh_disc, v_disc)
            entry["all"].add(who)
            if disc:
                entry["disc"].add(who)
            self._changed.add(key)

    def flush_clusters(self, force: bool = False) -> None:  # noqa: ARG002
        if self._changed:
            out = {}
            for k in self._changed:
                e = self.clusters[k]
                tot = [sum(v[i] for v in e["accs"].values()) for i in range(4)]
                # (insiders, insiders excl. 10b5-1, shares, $, shares excl. 10b5-1, $ excl. 10b5-1)
                out[k] = (len(e["all"]), len(e["disc"]), tot[0], tot[1], tot[2], tot[3])
            self.post("clusters", out)
            self._changed = set()

    # -- main loop ---------------------------------------------------------
    @staticmethod
    def _fetch_one(client: SecClient, cand: dict) -> dict:
        raw = client.get(f"{SEC_WWW}/Archives/{cand['path']}")
        return parse_submission(raw, cand["acc"], cand.get("date", ""))

    def run(self) -> None:
        stats = {"total": 0, "done": 0, "failed": 0, "matched": 0, "cached": 0, "requests": 0}
        cache = None
        client = SecClient(self.p.user_agent, self.cancel)
        try:
            cache = Cache(cache_path())
            self.post("phase", "Collecting list of Form 4 filings...")
            cands = self.gather(client, cache)
            stats["total"] = len(cands)
            self.post("total", len(cands))
            if not cands:
                stats["requests"] = client.request_count
                self.post("done", stats)
                return

            todo = []
            for i, (acc, cand) in enumerate(cands.items()):
                if i % 200 == 0:
                    self.check_cancel()
                rec = cache.get_filing(acc)
                if rec is None:
                    todo.append(cand)
                    continue
                stats["cached"] += 1
                stats["done"] += 1
                self.track(rec, cand)
                rows = evaluate_record(rec, cand, self.p)
                if rows:
                    stats["matched"] += len(rows)
                    self._emit(rows)
                if stats["done"] % 250 == 0:
                    self.flush_clusters()
                    self.post("progress", dict(stats))
            self.flush_clusters()
            self.post("progress", dict(stats))

            if todo:
                mins = len(todo) / MAX_REQUESTS_PER_SECOND / 60
                eta = f"~{mins:.0f} min" if mins >= 1 else "< 1 min"
                self.post("phase", f"Downloading {len(todo):,} filings from SEC ({eta}; "
                                   f"{stats['cached']:,} already cached)...")
                ex = ThreadPoolExecutor(max_workers=FETCH_WORKERS)
                try:
                    futures = {ex.submit(self._fetch_one, client, c): c for c in todo}
                    for fut in as_completed(futures):
                        cand = futures[fut]
                        try:
                            rec = fut.result()
                        except Cancelled:
                            raise
                        except SecAccessDenied as exc:
                            if "User-Agent" in str(exc):
                                raise
                            rec = None
                        except Exception:
                            rec = None
                        if rec is None:
                            stats["failed"] += 1
                        else:
                            cache.put_filing(cand["acc"], rec)
                            self.track(rec, cand)
                            rows = evaluate_record(rec, cand, self.p)
                            if rows:
                                stats["matched"] += len(rows)
                                self._emit(rows)
                        stats["done"] += 1
                        if stats["done"] % 25 == 0:
                            self.flush_clusters()
                        self.post("progress", dict(stats))
                        self.check_cancel()
                finally:
                    ex.shutdown(wait=False, cancel_futures=True)
            self.flush_clusters(force=True)
            if self.p.check_history and self.buys:
                self.history_phase(client, cache)
                self.context_phase(client, cache)
            stats["requests"] = client.request_count
            self.post("done", stats)
        except Cancelled:
            self.flush_clusters(force=True)
            stats["requests"] = client.request_count
            self.post("cancelled", stats)
        except SecAccessDenied as exc:
            self.post("error", str(exc))
        except Exception as exc:
            self.post("error", f"{type(exc).__name__}: {exc}\n\n{traceback.format_exc(limit=4)}")
        finally:
            if cache:
                cache.close()


# ----------------------------------------------------------------------------
# Helpers shared by GUI and CLI
# ----------------------------------------------------------------------------
def parse_tickers(text: str) -> list[str]:
    out = []
    for tok in re.split(r"[,\s;]+", text or ""):
        tok = tok.strip().lstrip("$").upper()
        if tok and tok not in out:
            out.append(tok)
    return out


def valid_user_agent(ua: str) -> bool:
    return bool(re.search(r"\S+@\S+\.\S+", ua or ""))


def fmt_money(v: float, decimals: int = 0) -> str:
    return f"${v:,.{decimals}f}"


def fmt_compact(v: float) -> str:
    for div, suf in ((1e9, "B"), (1e6, "M"), (1e3, "K")):
        if abs(v) >= div:
            return f"${v / div:.1f}{suf}"
    return f"${v:,.0f}"


CSV_HEADERS = ["Filing Date", "Ticker", "Signal Score", "Company", "Insider Name", "Matched Role", "Exact Title",
               "Buy/Sell", "Transaction Code", "Shares", "Price", "Total Value ($)", "Stake Change %",
               "Cluster (insiders)", "Cluster excl. 10b5-1", "Cluster % of Company", "% of Company (this buy)",
               "Market Cap ($)", "% Off 52-Week High at Buy", "3-Month Change Before Buy %", "Change Since Buy %",
               "Insider Net $ 90d", "Insider Buyers 90d", "Insider Sellers 90d", "13D Filings 90d",
               "10b5-1 Plan", "Offering / Placement", "Fund / Company Buyer", "Pattern", "Sold Since",
               "Sold % of Holding", "Score Reasons", "Trade Date(s)",
               "Lots", "Form", "Accession", "SEC Link", "Filing Index"]


def row_to_csv(r: dict) -> list:
    stake = r.get("stake")
    stake_out = "" if stake is None else ("New position" if stake == float("inf") else round(stake, 2))
    def rnd(v, n=2):
        return "" if v is None else round(v, n)
    flow = r.get("net90") or {}
    return [r["filing_date"], r["ticker"], "" if r.get("score") is None else r["score"],
            r["company"], r["insider"], r["role"], r["title"],
            r["side"], r["code"], round(r["shares"], 4), round(r["price"], 4), round(r["value"], 2),
            stake_out, r.get("cluster", 1), r.get("cluster_disc", 1), rnd(r.get("cluster_pct_co"), 4),
            rnd(r.get("pct_co"), 4), rnd(r.get("mcap"), 0), rnd(r.get("off_high")), rnd(r.get("chg3m")),
            rnd(r.get("since")), rnd((flow["buy"] - flow["sell"]) if flow else None, 0),
            flow.get("buyers", ""), flow.get("sellers", ""), r.get("d13", ""),
            "Yes" if r.get("plan") else "No", "Yes" if r.get("offering") else "No",
            "Yes" if r.get("entity") else "No",
            r.get("pattern", ""), r.get("sold", ""),
            "" if r.get("sold_pct") is None else round(r["sold_pct"], 1),
            "; ".join(x.strip() for x in r.get("score_why", [])),
            r["trade_dates"], r["lots"], r["form"], r["acc"], r["link"], r["index_link"]]


# ============================================================================
# GUI
# ============================================================================
def resource_path(name: str) -> str:
    base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, name)


def run_gui() -> None:
    import tkinter as tk
    import tkinter.font as tkfont
    from tkinter import filedialog, messagebox, ttk

    if sys.platform == "win32":
        try:
            import ctypes
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("Form4Scanner.InsiderScanner")
        except Exception:
            pass

    INF = float("inf")
    COLUMNS = [
        # key, heading, width, anchor, numeric
        ("filing_date", "Filing Date", 100, "w", False),
        ("ticker", "Ticker", 64, "w", False),
        ("score", "Score", 58, "center", True),
        ("company", "Company", 150, "w", False),
        ("insider", "Insider Name", 150, "w", False),
        ("role", "Matched Role", 120, "w", False),
        ("title", "Exact Title", 140, "w", False),
        ("side", "Buy/Sell", 80, "center", False),
        ("shares", "Shares", 84, "e", True),
        ("price", "Price", 76, "e", True),
        ("value", "Total Value ($)", 128, "e", True),
        ("stake", "Stake Δ %", 88, "e", True),
        ("cluster", "Cluster", 66, "center", True),
        ("pct_co", "% of Co.", 74, "e", True),
        ("mcap", "Mkt Cap", 78, "e", True),
        ("off_high", "Off 52w Hi", 92, "e", True),
        ("chg3m", "3M Before", 80, "e", True),
        ("since", "Since Buy", 86, "e", True),
        ("net90", "Insiders 90d", 150, "w", True),
        ("flags", "Flags", 110, "w", False),
        ("pattern", "Pattern", 215, "w", False),
        ("sold", "Sold Since", 145, "w", False),
        ("link", "SEC Link", 260, "w", False),
    ]
    HEADINGS = {c[0]: c[1] for c in COLUMNS}
    TOOLTIPS = {
        "stake": "How much this trade grew (or shrank) the insider's holding. 'New' = first shares.",
        "cluster": "Number of different insiders making the same kind of trade in this company during the scanned dates. "
                   "With 'Exclude pre-planned 10b5-1 trades' on, plan and offering purchases don't count.",
        "flags": "10b5-1 = pre-scheduled trading plan  •  Offering = bought in a company offering / private "
                 "placement  •  Fund = buyer is a fund or company  •  13D = a 5%+ holder filed Schedule 13D in "
                 "the last 90 days.",
        "score": "Signal Score 0-100: starts at 40 and adds or subtracts points for cluster size, CEO/CFO, "
                 "stake increase, first buy vs routine, later sales, 10b5-1/offering/fund flags, % of company, "
                 "company size, price drop before the buy, rise since, insider net flow and 13D filings. "
                 "Right-click a row > 'Why this score?' for the breakdown.",
        "pct_co": "Shares the whole cluster of insiders bought, as a % of shares outstanding (SEC XBRL data).",
        "mcap": "Market value: latest shares outstanding (SEC) x latest close (Yahoo Finance).",
        "off_high": "How far below its 52-week high the stock was on the purchase date.",
        "chg3m": "Stock price change over the 3 months before the purchase.",
        "since": "Price change from what the insider paid to the latest close (split/dividend adjusted).",
        "net90": "Open-market buying minus selling by ALL insiders of the company over the last 90 days "
                 "(B = buyers, S = sellers).",
        "pattern": "The insider's earlier open-market buys in this company (last 5 years): First buy, "
                   "Opportunistic (no schedule), Regular (most recent quarters / 2 of 3 years) or "
                   "Routine (same time every year for 3 years).",
        "sold": "Open-market sales (Code S) by this insider in this company on or after the purchase date, "
                "as a % of what they held right after buying. Tax withholding on vesting is not counted as a sale.",
    }
    PATTERN_ORDER = {"first": 0, "opp": 1, "regular": 2, "routine": 3, "unknown": 4, "pending": 5, "na": 6}

    class App:
        def __init__(self, root: tk.Tk):
            self.root = root
            self.settings = load_settings()
            self.q: queue.Queue = queue.Queue()
            self.cancel_event: threading.Event | None = None
            self.worker: threading.Thread | None = None
            self.scan_id = 0
            self.rows: dict[str, dict] = {}       # iid -> row
            self.order: list[str] = []            # all iids in current sort order
            self.clusters: dict[str, tuple] = {}  # cluster_key -> (all, discretionary)
            self.by_cluster: dict[str, list] = {} # cluster_key -> [iid]
            self.by_acc: dict[str, list] = {}     # accession -> [iid] (buy rows, for history updates)
            self.sort_key, self.sort_desc = None, False
            self.scan_started = 0.0
            self._view_job = None

            root.title(APP_NAME)
            root.geometry("1500x900")
            if sys.platform == "win32":
                try:
                    root.state("zoomed")
                except tk.TclError:
                    pass
            root.minsize(1100, 660)
            try:
                ico = resource_path(os.path.join("assets", "app.ico"))
                if sys.platform == "win32" and os.path.exists(ico):
                    root.iconbitmap(default=ico)
                else:
                    png = resource_path(os.path.join("assets", "app.png"))
                    if os.path.exists(png):
                        self._icon_img = tk.PhotoImage(file=png)
                        root.iconphoto(True, self._icon_img)
            except Exception:
                pass
            self._style()
            self._menu()
            self._controls()
            self._table()
            self._footer()
            self._on_range_change()
            root.protocol("WM_DELETE_WINDOW", self.on_close)
            root.after(100, self._poll)

        # ---- look & feel ------------------------------------------------
        def _style(self):
            s = ttk.Style(self.root)
            for theme in ("vista", "clam"):
                if theme in s.theme_names():
                    s.theme_use(theme)
                    break
            base = ("Segoe UI", 10) if sys.platform == "win32" else ("DejaVu Sans", 10)
            self.base_font = base
            for name in ("TkDefaultFont", "TkTextFont", "TkMenuFont", "TkHeadingFont"):
                try:
                    tkfont.nametofont(name).configure(family=base[0], size=base[1])
                except tk.TclError:
                    pass
            linespace = tkfont.Font(family=base[0], size=base[1]).metrics("linespace")
            s.configure(".", font=base)
            s.configure("Treeview", font=base, rowheight=max(24, int(linespace * 1.55)))
            s.configure("Treeview.Heading", font=(base[0], base[1], "bold"))
            s.configure("Title.TLabel", font=(base[0], 15, "bold"))
            s.configure("Sub.TLabel", foreground="#5b6470")
            s.configure("Hint.TLabel", foreground="#6b7480", font=(base[0], 9))
            s.configure("Big.TButton", font=(base[0], 10, "bold"), padding=(14, 6))
            s.configure("TLabelframe.Label", font=(base[0], 10, "bold"))
            self.bold_font = tkfont.Font(family=base[0], size=base[1], weight="bold")

            def fixed_map(option):   # Tk 8.6.9 Treeview tag-colour bug
                return [e for e in s.map("Treeview", query_opt=option) if e[:2] != ("!disabled", "!selected")]
            s.map("Treeview", foreground=fixed_map("foreground"), background=fixed_map("background"))

        def _menu(self):
            m = tk.Menu(self.root)
            fm = tk.Menu(m, tearoff=0)
            fm.add_command(label="Export Results to CSV...", command=self.export_csv, accelerator="Ctrl+E")
            fm.add_separator()
            fm.add_command(label="Signal Track Record...", command=self.show_track_record, accelerator="Ctrl+T")
            fm.add_separator()
            fm.add_command(label="Open Data Folder", command=self.open_data_folder)
            fm.add_command(label="Clear Download Cache", command=self.clear_cache)
            fm.add_separator()
            fm.add_command(label="Exit", command=self.on_close)
            m.add_cascade(label="File", menu=fm)
            hm = tk.Menu(m, tearoff=0)
            hm.add_command(label="How it works", command=self.show_help)
            hm.add_command(label="Finding true insider buying", command=self.show_signal_help)
            hm.add_command(label="SEC EDGAR Form 4 search", command=lambda: webbrowser.open(
                "https://www.sec.gov/edgar/search/#/forms=4"))
            m.add_cascade(label="Help", menu=hm)
            self.root.config(menu=m)
            self.root.bind_all("<Control-e>", lambda e: self.export_csv())
            self.root.bind_all("<Control-t>", lambda e: self.show_track_record())

        # ---- settings panel --------------------------------------------
        def _controls(self):
            st = self.settings
            outer = ttk.Frame(self.root, padding=(14, 10, 14, 4))
            outer.pack(fill="x")
            head = ttk.Frame(outer)
            head.pack(fill="x")
            ttk.Label(head, text="SEC Form 4 Insider Trading Scanner", style="Title.TLabel").pack(side="left")
            ttk.Label(head, text="   Insider buys & sells from SEC EDGAR", style="Sub.TLabel").pack(side="left", pady=(5, 0))
            ttk.Button(head, text="★  True Insider Buying preset", command=self.apply_preset).pack(side="right")

            grid = ttk.Frame(outer)
            grid.pack(fill="x", pady=(8, 0))
            grid.columnconfigure(0, weight=1)
            grid.columnconfigure(1, weight=1)

            # Date range ---------------------------------------------------
            f = ttk.LabelFrame(grid, text="Date Range (filing date)", padding=(10, 6))
            f.grid(row=0, column=0, sticky="nsew", padx=(0, 6), pady=3)
            self.range_var = tk.StringVar(value=st.get("range", "Last 7 Days"))
            cb = ttk.Combobox(f, textvariable=self.range_var, values=list(RANGE_CHOICES), state="readonly", width=16)
            cb.grid(row=0, column=0, padx=(0, 12))
            cb.bind("<<ComboboxSelected>>", lambda e: self._on_range_change())
            ttk.Label(f, text="From").grid(row=0, column=1)
            self.from_var = tk.StringVar(value=st.get("from", ""))
            self.from_entry = ttk.Entry(f, textvariable=self.from_var, width=12)
            self.from_entry.grid(row=0, column=2, padx=(4, 10))
            ttk.Label(f, text="To").grid(row=0, column=3)
            self.to_var = tk.StringVar(value=st.get("to", ""))
            self.to_entry = ttk.Entry(f, textvariable=self.to_var, width=12)
            self.to_entry.grid(row=0, column=4, padx=(4, 6))
            ttk.Label(f, text="YYYY-MM-DD", style="Hint.TLabel").grid(row=0, column=5, sticky="w")

            # Tickers ------------------------------------------------------
            f = ttk.LabelFrame(grid, text="Ticker Watchlist (optional)", padding=(10, 6))
            f.grid(row=0, column=1, sticky="nsew", padx=(6, 0), pady=3)
            f.columnconfigure(0, weight=1)
            self.tickers_var = tk.StringVar(value=st.get("tickers", ""))
            ttk.Entry(f, textvariable=self.tickers_var).grid(row=0, column=0, sticky="ew")
            ttk.Label(f, text="e.g. AAPL, MSFT, NVDA  -  leave blank to scan market-wide",
                      style="Hint.TLabel").grid(row=1, column=0, sticky="w", pady=(2, 0))

            # Roles --------------------------------------------------------
            f = ttk.LabelFrame(grid, text="Insider Role", padding=(10, 6))
            f.grid(row=1, column=0, sticky="nsew", padx=(0, 6), pady=3)
            saved_roles = set(st.get("roles", []))
            self.role_vars = {}
            for i, role in enumerate(ROLE_ORDER):
                v = tk.BooleanVar(value=role in saved_roles)
                self.role_vars[role] = v
                ttk.Checkbutton(f, text=role, variable=v).grid(row=0, column=i, sticky="w", padx=(0, 14))
            ttk.Label(f, text="None checked = all insiders. Matches officer title / relationship text "
                             "(case-insensitive).", style="Hint.TLabel").grid(row=1, column=0, columnspan=4, sticky="w", pady=(2, 0))

            # Transactions -------------------------------------------------
            f = ttk.LabelFrame(grid, text="Transactions", padding=(10, 6))
            f.grid(row=1, column=1, sticky="nsew", padx=(6, 0), pady=3)
            saved_codes = set(st.get("codes", ["P"]))
            self.code_vars = {}
            for i, (code, (label, _)) in enumerate(TXN_CODES.items()):
                v = tk.BooleanVar(value=code in saved_codes)
                self.code_vars[code] = v
                ttk.Checkbutton(f, text=label, variable=v).grid(row=i // 2, column=i % 2, sticky="w", padx=(0, 14))
            sub = ttk.Frame(f)
            sub.grid(row=2, column=0, columnspan=2, sticky="w", pady=(4, 0))
            ttk.Label(sub, text="Minimum $ value").pack(side="left")
            self.min_var = tk.StringVar(value=str(st.get("min_value", "50000")))
            vcmd = (self.root.register(lambda s: re.fullmatch(r"[\d,]*\.?\d*", s) is not None), "%P")
            ttk.Entry(sub, textvariable=self.min_var, width=12, validate="key", validatecommand=vcmd).pack(side="left", padx=(6, 14))
            self.amend_var = tk.BooleanVar(value=st.get("amendments", False))
            ttk.Checkbutton(sub, text="Include 4/A amendments", variable=self.amend_var).pack(side="left", padx=(0, 12))
            self.combine_var = tk.BooleanVar(value=st.get("combine", True))
            ttk.Checkbutton(sub, text="Combine lots per filing", variable=self.combine_var).pack(side="left")

            # Signal filters (instant, no re-scan) --------------------------
            box = ttk.LabelFrame(grid, text="Signal Filters  (apply instantly to the results - no re-scan needed)", padding=(10, 6))
            box.grid(row=2, column=0, columnspan=2, sticky="nsew", pady=3)
            f = ttk.Frame(box)
            f.pack(fill="x")
            self.excl_plan_var = tk.BooleanVar(value=st.get("exclude_plan", True))
            ttk.Checkbutton(f, text="Exclude pre-planned 10b5-1 trades", variable=self.excl_plan_var,
                            command=self._view_changed).pack(side="left", padx=(0, 22))
            self.cluster_only_var = tk.BooleanVar(value=st.get("clusters_only", False))
            ttk.Checkbutton(f, text="Clusters only - at least", variable=self.cluster_only_var,
                            command=self._view_changed).pack(side="left")
            self.cluster_min_var = tk.IntVar(value=int(st.get("cluster_min", 2)))
            ttk.Spinbox(f, from_=2, to=20, width=4, textvariable=self.cluster_min_var,
                        command=self._view_changed).pack(side="left", padx=4)
            ttk.Label(f, text="different insiders").pack(side="left", padx=(0, 22))
            ttk.Label(f, text="Min stake increase").pack(side="left")
            self.min_stake_var = tk.StringVar(value=str(st.get("min_stake", "0")))
            vcmd2 = (self.root.register(lambda s: re.fullmatch(r"\d{0,5}", s) is not None), "%P")
            e = ttk.Entry(f, textvariable=self.min_stake_var, width=6, validate="key", validatecommand=vcmd2)
            e.pack(side="left", padx=4)
            e.bind("<KeyRelease>", lambda ev: self._view_changed())
            ttk.Label(f, text="%  (0 = off; buys only)").pack(side="left")
            self.cluster_min_var.trace_add("write", lambda *a: self._view_changed())

            f2 = ttk.Frame(box)
            f2.pack(fill="x", pady=(4, 0))
            self.history_var = tk.BooleanVar(value=st.get("check_history", True))
            ttk.Checkbutton(f2, text="Deep check buyers (history, later sales, company size, prices)",
                            variable=self.history_var).pack(side="left", padx=(0, 22))
            self.hide_routine_var = tk.BooleanVar(value=st.get("hide_routine", False))
            ttk.Checkbutton(f2, text="Hide routine / regular buyers", variable=self.hide_routine_var,
                            command=self._view_changed).pack(side="left", padx=(0, 22))
            self.hide_sold_var = tk.BooleanVar(value=st.get("hide_sold", False))
            ttk.Checkbutton(f2, text="Hide buyers who have sold since", variable=self.hide_sold_var,
                            command=self._view_changed).pack(side="left", padx=(0, 22))
            ttk.Label(f2, text="Deep check adds a few minutes the first time; results are cached.",
                      style="Hint.TLabel").pack(side="left")

            f3 = ttk.Frame(box)
            f3.pack(fill="x", pady=(4, 0))
            self.excl_offer_var = tk.BooleanVar(value=st.get("exclude_offering", True))
            ttk.Checkbutton(f3, text="Exclude offering / private-placement buys", variable=self.excl_offer_var,
                            command=self._view_changed).pack(side="left", padx=(0, 22))
            self.excl_entity_var = tk.BooleanVar(value=st.get("exclude_entity", False))
            ttk.Checkbutton(f3, text="Exclude funds / companies as buyers", variable=self.excl_entity_var,
                            command=self._view_changed).pack(side="left", padx=(0, 22))
            vnum = (self.root.register(lambda s: re.fullmatch(r"\d{0,7}", s) is not None), "%P")
            ttk.Label(f3, text="Min score").pack(side="left")
            self.min_score_var = tk.StringVar(value=str(st.get("min_score", "0")))
            e3 = ttk.Entry(f3, textvariable=self.min_score_var, width=5, validate="key", validatecommand=vnum)
            e3.pack(side="left", padx=(4, 22))
            e3.bind("<KeyRelease>", lambda ev: self._view_changed())
            ttk.Label(f3, text="Max market cap $M").pack(side="left")
            self.max_mcap_var = tk.StringVar(value=str(st.get("max_mcap", "")))
            e4 = ttk.Entry(f3, textvariable=self.max_mcap_var, width=8, validate="key", validatecommand=vnum)
            e4.pack(side="left", padx=4)
            e4.bind("<KeyRelease>", lambda ev: self._view_changed())
            ttk.Label(f3, text="(blank = any)", style="Hint.TLabel").pack(side="left")

            # SEC contact ---------------------------------------------------
            f = ttk.Frame(outer)
            f.pack(fill="x", pady=(6, 2))
            ttk.Label(f, text="SEC contact (User-Agent):").pack(side="left")
            self.ua_var = tk.StringVar(value=st.get("user_agent", ""))
            ttk.Entry(f, textvariable=self.ua_var, width=42).pack(side="left", padx=6)
            ttk.Label(f, text="SEC requires your name + email on automated requests, e.g. \"Jane Doe jane@example.com\"",
                      style="Hint.TLabel").pack(side="left")

            # Action row -----------------------------------------------------
            f = ttk.Frame(outer)
            f.pack(fill="x", pady=(8, 4))
            self.start_btn = ttk.Button(f, text="▶  Start Scan", style="Big.TButton", command=self.start_scan)
            self.start_btn.pack(side="left")
            self.cancel_btn = ttk.Button(f, text="■  Cancel", style="Big.TButton", command=self.cancel_scan, state="disabled")
            self.cancel_btn.pack(side="left", padx=(8, 0))
            ttk.Button(f, text="Export CSV...", command=self.export_csv).pack(side="left", padx=(16, 0))
            ttk.Button(f, text="Clear", command=self.clear_results).pack(side="left", padx=(6, 0))
            self.progress = ttk.Progressbar(f, mode="determinate", length=300)
            self.progress.pack(side="left", padx=(20, 10))
            self.status_var = tk.StringVar(value="Ready.")
            ttk.Label(f, textvariable=self.status_var).pack(side="left", fill="x", expand=True)

        def _on_range_change(self):
            days = RANGE_CHOICES.get(self.range_var.get())
            if days is None:
                self.from_entry.config(state="normal")
                self.to_entry.config(state="normal")
                if not self.from_var.get():
                    self.from_var.set((dt.date.today() - dt.timedelta(days=7)).isoformat())
                if not self.to_var.get():
                    self.to_var.set(dt.date.today().isoformat())
            else:
                today = dt.date.today()
                self.from_var.set((today - dt.timedelta(days=days)).isoformat())
                self.to_var.set(today.isoformat())
                self.from_entry.config(state="disabled")
                self.to_entry.config(state="disabled")

        def apply_preset(self):
            """Settings that isolate the kind of insider buying research links to returns."""
            self.range_var.set("Last 30 Days")
            self._on_range_change()
            for v in self.role_vars.values():
                v.set(False)
            for c, v in self.code_vars.items():
                v.set(c == "P")
            self.min_var.set("100000")
            self.combine_var.set(True)
            self.amend_var.set(False)
            self.excl_plan_var.set(True)
            self.cluster_only_var.set(True)
            self.cluster_min_var.set(2)
            self.min_stake_var.set("10")
            self.history_var.set(True)
            self.hide_routine_var.set(True)
            self.hide_sold_var.set(True)
            self.excl_offer_var.set(True)
            self.excl_entity_var.set(True)
            self.min_score_var.set("0")
            self._view_changed()
            self.status_var.set("Preset applied: open-market buys ≥ $100k, 30 days, ≥ 2 insiders, no 10b5-1 "
                                "plans, stake +10%, no routine buyers, no later sellers. Click Start Scan.")

        # ---- table ------------------------------------------------------
        def _table(self):
            wrap = ttk.Frame(self.root, padding=(14, 2, 14, 0))
            wrap.pack(fill="both", expand=True)
            cols = [c[0] for c in COLUMNS]
            self.tree = ttk.Treeview(wrap, columns=cols, show="headings", selectmode="extended")
            for key, head, width, anchor, _n in COLUMNS:
                self.tree.heading(key, text=head, command=lambda k=key: self.sort_by(k))
                self.tree.column(key, width=width, minwidth=44, anchor=anchor,
                                 stretch=(key in ("company", "title", "link")))
            vs = ttk.Scrollbar(wrap, orient="vertical", command=self.tree.yview)
            hs = ttk.Scrollbar(wrap, orient="horizontal", command=self.tree.xview)
            self.tree.configure(yscrollcommand=vs.set, xscrollcommand=hs.set)
            self.tree.grid(row=0, column=0, sticky="nsew")
            vs.grid(row=0, column=1, sticky="ns")
            hs.grid(row=1, column=0, sticky="ew")
            wrap.rowconfigure(0, weight=1)
            wrap.columnconfigure(0, weight=1)
            self.tree.tag_configure("Buy", background="#e8f6ec", foreground="#14532d")
            self.tree.tag_configure("Sell", background="#fbeaea", foreground="#7f1d1d")
            self.tree.tag_configure("Grant", background="#eef2fb")
            self.tree.tag_configure("Exercise", background="#f6f1e7")
            self.tree.tag_configure("hot", background="#c4ecd1", foreground="#0b3d20", font=self.bold_font)
            self.tree.tag_configure("flip", background="#fdebd3", foreground="#8a4b08")
            self.tree.tag_configure("routine", background="#eeeeee", foreground="#5f6670")
            self.tree.bind("<Double-1>", self._on_double)
            self.tree.bind("<Return>", lambda e: self.open_selected())
            self.tree.bind("<Button-3>", self._on_right_click)
            self.hidden_cols = set(self.settings.get("hidden_cols", ["chg3m"])) & {c[0] for c in COLUMNS}
            self._apply_columns()
            self.tree.bind("<Control-c>", lambda e: self.copy_selected())
            self.tree.bind("<Motion>", self._on_motion)
            self.tree.bind("<Leave>", lambda e: self._hide_tip())
            self._tip = None

            self.ctx = tk.Menu(self.root, tearoff=0)
            self.ctx.add_command(label="Why this score?", command=self.explain_score)
            self.ctx.add_command(label="Open price chart (Yahoo Finance)", command=self.open_chart)
            self.ctx.add_separator()
            self.ctx.add_command(label="Open Form 4 on SEC.gov", command=self.open_selected)
            self.ctx.add_command(label="Open filing index page", command=lambda: self.open_selected(index=True))
            self.ctx.add_separator()
            self.ctx.add_command(label="Show only this ticker", command=self.filter_to_ticker)
            self.ctx.add_command(label="Show all tickers", command=self.clear_ticker_filter)
            self.ctx.add_separator()
            self.ctx.add_command(label="Copy link", command=self.copy_link)
            self.ctx.add_command(label="Copy row(s)", command=self.copy_selected)
            self.only_ticker: str | None = None

        def _footer(self):
            f = ttk.Frame(self.root, padding=(14, 4, 14, 8))
            f.pack(fill="x")
            self.summary_var = tk.StringVar(value="No results yet.")
            ttk.Label(f, textvariable=self.summary_var).pack(side="left")
            ttk.Label(f, text="Bold green = cluster buy  •  Orange = sold since  •  Grey = routine  •  "
                             "Double-click = open filing",
                      style="Hint.TLabel").pack(side="right")

        # header tooltips ---------------------------------------------------
        def _on_motion(self, event):
            if self.tree.identify_region(event.x, event.y) != "heading":
                self._hide_tip()
                return
            col = self.tree.identify_column(event.x)
            try:
                key = self.tree["columns"][int(col[1:]) - 1]
            except (ValueError, IndexError):
                return
            text = TOOLTIPS.get(key)
            if not text:
                self._hide_tip()
                return
            if self._tip is None:
                self._tip = tk.Toplevel(self.root)
                self._tip.wm_overrideredirect(True)
                self._tip_label = tk.Label(self._tip, bg="#ffffe1", relief="solid", borderwidth=1,
                                           padx=6, pady=3, wraplength=320, justify="left")
                self._tip_label.pack()
            self._tip_label.config(text=text)
            self._tip.wm_geometry(f"+{event.x_root + 12}+{event.y_root + 18}")

        def _hide_tip(self):
            if self._tip is not None:
                self._tip.destroy()
                self._tip = None

        # ---- params ------------------------------------------------------
        def _collect_params(self) -> ScanParams | None:
            ua = self.ua_var.get().strip()
            if not valid_user_agent(ua):
                messagebox.showwarning(APP_NAME, "SEC requires a contact on automated requests.\n\n"
                                       "Enter your name and email in 'SEC contact', e.g.\n    Jane Doe jane@example.com")
                return None
            try:
                start = dt.date.fromisoformat(self.from_var.get().strip())
                end = dt.date.fromisoformat(self.to_var.get().strip())
            except ValueError:
                messagebox.showwarning(APP_NAME, "Dates must be in YYYY-MM-DD format.")
                return None
            end = min(end, dt.date.today())
            if start > end:
                messagebox.showwarning(APP_NAME, "'From' date must be on or before 'To' date.")
                return None
            codes = {c for c, v in self.code_vars.items() if v.get()}
            if not codes:
                messagebox.showwarning(APP_NAME, "Select at least one transaction type.")
                return None
            try:
                min_value = float((self.min_var.get() or "0").replace(",", ""))
            except ValueError:
                messagebox.showwarning(APP_NAME, "Minimum $ value must be a number.")
                return None
            return ScanParams(
                start=start, end=end, user_agent=ua,
                tickers=parse_tickers(self.tickers_var.get()),
                roles={r for r, v in self.role_vars.items() if v.get()},
                codes=codes, min_value=min_value,
                include_amendments=self.amend_var.get(),
                combine_lots=self.combine_var.get(),
                check_history=self.history_var.get(),
            )

        def _save(self):
            save_settings({
                "range": self.range_var.get(), "from": self.from_var.get(), "to": self.to_var.get(),
                "tickers": self.tickers_var.get(),
                "roles": [r for r, v in self.role_vars.items() if v.get()],
                "codes": [c for c, v in self.code_vars.items() if v.get()],
                "min_value": self.min_var.get(), "amendments": self.amend_var.get(),
                "combine": self.combine_var.get(), "user_agent": self.ua_var.get().strip(),
                "exclude_plan": self.excl_plan_var.get(), "clusters_only": self.cluster_only_var.get(),
                "cluster_min": self._cluster_min(), "min_stake": self.min_stake_var.get(),
                "check_history": self.history_var.get(), "hide_routine": self.hide_routine_var.get(),
                "hide_sold": self.hide_sold_var.get(),
                "exclude_offering": self.excl_offer_var.get(), "exclude_entity": self.excl_entity_var.get(),
                "min_score": self.min_score_var.get(), "max_mcap": self.max_mcap_var.get(),
                "hidden_cols": sorted(self.hidden_cols),
            })

        # ---- scan control -------------------------------------------------
        def start_scan(self):
            if self.worker and self.worker.is_alive():
                return
            if RANGE_CHOICES.get(self.range_var.get()):
                self._on_range_change()          # refresh "last N days" to today
            p = self._collect_params()
            if not p:
                return
            span = (p.end - p.start).days + 1
            if not p.tickers and span > 31:
                if not messagebox.askyesno(APP_NAME, f"A market-wide scan of {span} days can mean tens of "
                                           "thousands of filings and take an hour or more the first time.\n\nContinue?"):
                    return
            self._save()
            self.clear_results()
            self.scan_id += 1
            self.cancel_event = threading.Event()
            scanner = Scanner(p, self.q, self.cancel_event, self.scan_id)
            self.worker = threading.Thread(target=scanner.run, name="form4-scan", daemon=True)
            self.scan_started = time.time()
            self.progress.config(mode="indeterminate")
            self.progress.start(12)
            scope = ", ".join(p.tickers) if p.tickers else "market-wide"
            self.status_var.set(f"Starting scan ({scope}, {p.start} to {p.end})...")
            self.start_btn.config(state="disabled")
            self.cancel_btn.config(state="normal")
            self.worker.start()

        def cancel_scan(self):
            if self.cancel_event:
                self.cancel_event.set()
                self.status_var.set("Cancelling...")
                self.cancel_btn.config(state="disabled")

        def _finish(self, done: int | None = None):
            if str(self.progress.cget("mode")) == "indeterminate":
                self.progress.stop()
                self.progress.config(mode="determinate", value=0)
            if done is not None:
                self.progress.config(value=done)
            self.start_btn.config(state="normal")
            self.cancel_btn.config(state="disabled")
            if self.rows and self.sort_key is None:
                if any(r.get("score") is not None for r in self.rows.values()):
                    self.sort_by("score")            # best signals first
                else:
                    self.sort_key, self.sort_desc = "filing_date", False
                    self.sort_by("filing_date")      # toggles to descending
            else:
                self._apply_view()

        def _poll(self):
            try:
                for _ in range(2000):
                    sid, kind, payload = self.q.get_nowait()
                    if sid != self.scan_id:
                        continue
                    self._handle(kind, payload)
            except queue.Empty:
                pass
            self.root.after(100, self._poll)

        def _handle(self, kind, payload):
            if kind == "phase":
                self.status_var.set(payload)
            elif kind == "warn":
                self.status_var.set(payload)
                messagebox.showinfo(APP_NAME, payload)
            elif kind == "total":
                self.progress.stop()
                self.progress.config(mode="determinate", maximum=max(1, payload), value=0)
            elif kind == "progress":
                self.progress.config(value=payload["done"])
                extra = f", {payload['failed']} failed" if payload["failed"] else ""
                self.status_var.set(f"Scanned {payload['done']:,} of {payload['total']:,} filings  •  "
                                    f"{payload['matched']:,} matches{extra}")
            elif kind == "rows":
                for r in payload:
                    self._insert(r)
                self._update_summary()
            elif kind == "history":
                for acc, info in payload.items():
                    for iid in self.by_acc.get(acc, []):
                        self.rows[iid].update(info)
                        self._refresh_row(iid)
                if self.hide_routine_var.get() or self.hide_sold_var.get():
                    self._schedule_view()
            elif kind == "history_progress":
                done, total = payload
                self.progress.config(mode="determinate", maximum=max(1, total), value=done)
                self.status_var.set(f"Checking buyers' history & later sales: {done:,} of {total:,} insiders...")
            elif kind == "context":
                for acc, info in payload.items():
                    for iid in self.by_acc.get(acc, []):
                        self.rows[iid].update(info)
                        self._refresh_row(iid)
                self._schedule_view()
            elif kind == "context_progress":
                done, total = payload
                self.progress.config(mode="determinate", maximum=max(1, total), value=done)
                self.status_var.set(f"Checking company size, insider activity & prices: {done:,} of {total:,} companies...")
            elif kind == "clusters":
                self.clusters.update(payload)
                for key in payload:
                    for iid in self.by_cluster.get(key, []):
                        self._refresh_row(iid)
                if self.cluster_only_var.get():
                    self._schedule_view()
            elif kind in ("done", "cancelled"):
                for r in self.rows.values():          # anything the history check didn't reach
                    if r.get("pattern_kind") == "pending":
                        r.update({"pattern": "Not checked", "pattern_kind": "unknown",
                                  "sold": "Not checked", "sold_kind": "unknown"})
                for iid in self.rows:
                    self._refresh_row(iid)
                secs = time.time() - self.scan_started
                verb = "Scan complete" if kind == "done" else "Scan cancelled"
                extra = f"  •  {payload['failed']} could not be downloaded" if payload["failed"] else ""
                if kind == "done" and payload["total"] == 0:
                    self.status_var.set("No Form 4 filings found for that date range / ticker list.")
                else:
                    self.status_var.set(f"{verb}: {payload['done']:,} of {payload['total']:,} filings scanned, "
                                        f"{payload['matched']:,} matches in {secs:,.0f}s{extra}")
                self._finish(int(float(self.progress.cget("maximum"))))
                self._update_summary()
                if kind == "done":
                    try:
                        log = SignalLog()
                        log.log(list(self.rows.values()))
                        log.close()
                    except Exception:
                        pass
            elif kind == "error":
                self.status_var.set("Scan stopped with an error.")
                self._finish()
                messagebox.showerror(APP_NAME, payload)

        # ---- rows / view ----------------------------------------------------
        def _cluster_count(self, r: dict) -> int:
            all_n, disc_n = self.clusters.get(r["cluster_key"], (1, 0 if r.get("plan") else 1))[:2]
            n = disc_n if self.excl_plan_var.get() else all_n
            return max(n, 0 if (self.excl_plan_var.get() and r.get("plan")) else 1)

        def _cluster_shares(self, r: dict) -> float:
            c = self.clusters.get(r["cluster_key"])
            if not c or len(c) < 6:
                return r["shares"]
            return c[4] if self.excl_plan_var.get() else c[2]

        def _cluster_min(self) -> int:
            try:
                return max(2, int(self.cluster_min_var.get()))
            except (tk.TclError, ValueError):
                return 2

        def _values(self, r: dict) -> tuple:
            score = r.get("score")
            return (r["filing_date"], r["ticker"], "" if score is None else score,
                    r["company"], r["insider"], r["role"], r["title"],
                    r["side"], f"{r['shares']:,.0f}", fmt_money(r["price"], 2), fmt_money(r["value"]),
                    fmt_stake(r.get("stake")), r["cluster"],
                    fmt_pct(r.get("cluster_pct_co"), plus=False, digits=2),
                    fmt_compact(r["mcap"]) if r.get("mcap") else "",
                    fmt_pct(r.get("off_high"), plus=False, digits=0), fmt_pct(r.get("chg3m"), digits=0),
                    fmt_pct(r.get("since"), digits=0), fmt_net90(r.get("net90")), self._flags(r),
                    r.get("pattern", ""), r.get("sold", ""), r["link"])

        @staticmethod
        def _flags(r: dict) -> str:
            f = []
            if r.get("plan"):
                f.append("10b5-1")
            if r.get("offering"):
                f.append("Offering")
            if r.get("entity"):
                f.append("Fund")
            if r.get("d13"):
                f.append("13D")
            return ", ".join(f)

        def _tags(self, r: dict) -> tuple:
            if r["code"] == "P" and (r.get("sold_kind") == "sold" or r.get("offering")):
                return ("flip",)
            if r["code"] == "P" and r.get("pattern_kind") in ("routine", "regular"):
                return ("routine",)
            if r["code"] == "P" and r["cluster"] >= 2 and not r.get("plan"):
                return ("hot",)
            return (r["side"],)

        def _insert(self, r: dict):
            self._compute(r)
            iid = self.tree.insert("", "end", values=self._values(r), tags=self._tags(r))
            self.rows[iid] = r
            self.order.append(iid)
            self.by_cluster.setdefault(r["cluster_key"], []).append(iid)
            if r["code"] == "P":
                self.by_acc.setdefault(r["acc"], []).append(iid)
            if not self._visible(r):
                self.tree.detach(iid)

        def _refresh_row(self, iid: str):
            r = self.rows[iid]
            self._compute(r)
            self.tree.item(iid, values=self._values(r), tags=self._tags(r))

        def _compute(self, r: dict) -> None:
            r["cluster"] = self._cluster_count(r)
            r["cluster_disc"] = self.clusters.get(r["cluster_key"], (1, 1))[1]
            so = r.get("shares_out")
            r["cluster_pct_co"] = (self._cluster_shares(r) / so * 100.0) if so else None
            if r["code"] == "P":
                r["score"], r["score_why"] = signal_score(r)

        def _visible(self, r: dict) -> bool:
            if self.only_ticker and r["ticker"] != self.only_ticker:
                return False
            if self.excl_plan_var.get() and r.get("plan"):
                return False
            if self.cluster_only_var.get() and r["cluster"] < self._cluster_min():
                return False
            if self.hide_routine_var.get() and r.get("pattern_kind") in ("routine", "regular"):
                return False
            if self.hide_sold_var.get() and r.get("sold_kind") == "sold":
                return False
            if self.excl_offer_var.get() and r.get("offering"):
                return False
            if self.excl_entity_var.get() and r.get("entity") and r["code"] == "P":
                return False
            min_score = int(self.min_score_var.get() or 0) if self.min_score_var.get().isdigit() else 0
            if min_score and r["code"] == "P" and (r.get("score") or 0) < min_score:
                return False
            if self.max_mcap_var.get().isdigit() and r.get("mcap") and r["mcap"] > int(self.max_mcap_var.get()) * 1e6:
                return False
            try:
                min_stake = float(self.min_stake_var.get() or 0)
            except ValueError:
                min_stake = 0
            if min_stake > 0 and r["code"] == "P":
                stake = r.get("stake")
                if stake is None or stake < min_stake:
                    return False
            return True

        def _view_changed(self):
            for iid in self.rows:
                self._refresh_row(iid)
            self._schedule_view()

        def _schedule_view(self):
            if self._view_job is None:
                self._view_job = self.root.after(150, self._apply_view)

        def _apply_view(self):
            self._view_job = None
            attached = self.tree.get_children()
            if attached:
                self.tree.detach(*attached)
            idx = 0
            for iid in self.order:
                if self._visible(self.rows[iid]):
                    self.tree.move(iid, "", idx)
                    idx += 1
            self._update_summary()

        def _visible_rows(self) -> list[dict]:
            return [self.rows[i] for i in self.tree.get_children()]

        def _update_summary(self):
            vis = self._visible_rows()
            total = len(self.rows)
            if not total:
                self.summary_var.set("No matching trades yet.")
                return
            buys = [r for r in vis if r["code"] == "P"]
            sells = [r for r in vis if r["code"] == "S"]
            clusters = {r["ticker"] for r in buys if r["cluster"] >= 2}
            txt = f"Showing {len(vis):,} of {total:,} trades"
            strong = sum(1 for r in buys if (r.get("score") or 0) >= 70)
            if strong:
                txt += f"   •   Score 70+: {strong:,}"
            txt += (f"   •   Buys: {len(buys):,} ({fmt_compact(sum(r['value'] for r in buys))})"
                    f"   •   Sells: {len(sells):,} ({fmt_compact(sum(r['value'] for r in sells))})"
                    f"   •   Cluster-buy tickers: {len(clusters):,}")
            if self.only_ticker:
                txt += f"   •   Filtered to {self.only_ticker}"
            self.summary_var.set(txt)

        def clear_results(self):
            self.tree.delete(*self.tree.get_children())
            for iid in list(self.rows):
                if self.tree.exists(iid):
                    self.tree.delete(iid)
            self.rows.clear()
            self.order.clear()
            self.clusters.clear()
            self.by_cluster.clear()
            self.by_acc.clear()
            self.only_ticker = None
            self.sort_key, self.sort_desc = None, False
            for key, head, *_ in COLUMNS:
                self.tree.heading(key, text=head)
            self._update_summary()

        # ---- sorting -------------------------------------------------------
        def sort_by(self, key: str):
            numeric = next(c[4] for c in COLUMNS if c[0] == key)
            if self.sort_key == key:
                self.sort_desc = not self.sort_desc
            else:
                self.sort_key, self.sort_desc = key, numeric   # numbers default high-to-low
            if key == "net90":
                def kf(iid):
                    f = self.rows[iid].get("net90")
                    return ((f["buy"] - f["sell"]) if f else -INF, self.rows[iid]["value"])
            elif key == "pct_co":
                def kf(iid):
                    v = self.rows[iid].get("cluster_pct_co")
                    return (-INF if v is None else v, self.rows[iid]["value"])
            elif numeric:
                def kf(iid):
                    v = self.rows[iid].get(key)
                    return (-INF if v is None else v, self.rows[iid]["value"])
            elif key == "flags":
                kf = lambda iid: (self._flags(self.rows[iid]), self.rows[iid]["value"])
            elif key == "pattern":
                kf = lambda iid: (-PATTERN_ORDER.get(self.rows[iid].get("pattern_kind", "na"), 9), self.rows[iid]["value"])
            elif key == "sold":
                def kf(iid):
                    r = self.rows[iid]
                    pct = r.get("sold_pct")
                    rank = {"sold": 2, "tax": 1, "none": 0}.get(r.get("sold_kind"), -1)
                    return (rank, pct if pct is not None else 0.0, r["value"])
            else:
                kf = lambda iid: (str(self.rows[iid][key]).lower(), -self.rows[iid]["value"])
            self.order.sort(key=kf, reverse=self.sort_desc)
            for k in HEADINGS:
                arrow = (" ▼" if self.sort_desc else " ▲") if k == key else ""
                self.tree.heading(k, text=HEADINGS[k] + arrow)
            self._apply_view()

        # ---- row actions ---------------------------------------------------
        def _on_double(self, event):
            if self.tree.identify_region(event.x, event.y) not in ("cell", "tree"):
                return
            iid = self.tree.identify_row(event.y)
            if iid:
                webbrowser.open(self.rows[iid]["link"])

        def _apply_columns(self):
            self.tree["displaycolumns"] = [c[0] for c in COLUMNS if c[0] not in self.hidden_cols]

        def _column_menu(self, event):
            menu = tk.Menu(self.root, tearoff=0)
            for key, head, *_ in COLUMNS:
                if key in ("filing_date", "ticker"):
                    continue
                var = tk.BooleanVar(value=key not in self.hidden_cols)
                menu.add_checkbutton(label=head, variable=var,
                                     command=lambda k=key, v=var: self._toggle_column(k, v.get()))
                menu._vars = getattr(menu, "_vars", []) + [var]   # keep references alive
            menu.add_separator()
            menu.add_command(label="Show all columns", command=lambda: self._toggle_column(None, True))
            menu.tk_popup(event.x_root, event.y_root)

        def _toggle_column(self, key, show):
            if key is None:
                self.hidden_cols.clear()
            elif show:
                self.hidden_cols.discard(key)
            else:
                self.hidden_cols.add(key)
            self._apply_columns()

        def _on_right_click(self, event):
            if self.tree.identify_region(event.x, event.y) == "heading":
                self._column_menu(event)
                return
            iid = self.tree.identify_row(event.y)
            if iid:
                if iid not in self.tree.selection():
                    self.tree.selection_set(iid)
                self.ctx.tk_popup(event.x_root, event.y_root)

        def explain_score(self):
            sel = self.tree.selection()
            if not sel:
                return
            r = self.rows[sel[0]]
            if r.get("score") is None:
                messagebox.showinfo(APP_NAME, "Scores are calculated for open-market purchases (Code P) only.")
                return
            lines = [f"{r['ticker']} - {r['insider']} - {fmt_money(r['value'])}", "",
                     f"Signal Score: {r['score']} / 100", "", "+40  starting point"]
            lines += r.get("score_why") or []
            if not r.get("prices_ok") and r.get("deep"):
                lines += ["", "(No price data - price-based points not included.)"]
            if not r.get("deep"):
                lines += ["", "(Deep check was off - history, company and price points not included.)"]
            messagebox.showinfo(APP_NAME, "\n".join(lines))

        def open_chart(self):
            sel = self.tree.selection()
            if sel:
                sym = yahoo_symbol(self.rows[sel[0]]["ticker"])
                if sym:
                    webbrowser.open(f"https://finance.yahoo.com/quote/{sym}/chart")

        def show_track_record(self):
            TrackRecordWindow(self)

        def filter_to_ticker(self):
            sel = self.tree.selection()
            if sel:
                self.only_ticker = self.rows[sel[0]]["ticker"]
                self._apply_view()

        def clear_ticker_filter(self):
            self.only_ticker = None
            self._apply_view()

        def open_selected(self, index: bool = False):
            for iid in self.tree.selection()[:10]:
                webbrowser.open(self.rows[iid]["index_link" if index else "link"])

        def copy_link(self):
            sel = self.tree.selection()
            if sel:
                self.root.clipboard_clear()
                self.root.clipboard_append("\n".join(self.rows[i]["link"] for i in sel))

        def copy_selected(self):
            sel = self.tree.selection()
            if not sel:
                return
            lines = ["\t".join(CSV_HEADERS)]
            lines += ["\t".join(str(x) for x in row_to_csv(self.rows[i])) for i in sel]
            self.root.clipboard_clear()
            self.root.clipboard_append("\n".join(lines))

        def export_csv(self):
            vis = self.tree.get_children()
            if not vis:
                messagebox.showinfo(APP_NAME, "There are no results to export yet.")
                return
            path = filedialog.asksaveasfilename(
                title="Export results", defaultextension=".csv",
                initialfile=f"form4_insider_trades_{dt.date.today():%Y%m%d}.csv",
                filetypes=[("CSV (Excel)", "*.csv"), ("All files", "*.*")])
            if not path:
                return
            try:
                with open(path, "w", newline="", encoding="utf-8-sig") as fh:
                    w = csv.writer(fh)
                    w.writerow(CSV_HEADERS)
                    for iid in vis:
                        w.writerow(row_to_csv(self.rows[iid]))
            except OSError as exc:
                messagebox.showerror(APP_NAME, f"Could not write file:\n{exc}")
                return
            self.status_var.set(f"Exported {len(vis):,} rows to {path}")
            if messagebox.askyesno(APP_NAME, f"Exported {len(vis):,} rows (the ones currently shown).\n\nOpen the file now?"):
                try:
                    os.startfile(path)  # type: ignore[attr-defined]
                except Exception:
                    webbrowser.open("file:///" + path.replace("\\", "/"))

        # ---- misc ------------------------------------------------------------
        def open_data_folder(self):
            path = app_data_dir()
            try:
                os.startfile(path)  # type: ignore[attr-defined]
            except Exception:
                messagebox.showinfo(APP_NAME, path)

        def clear_cache(self):
            if self.worker and self.worker.is_alive():
                messagebox.showinfo(APP_NAME, "Please wait for the current scan to finish or cancel it first.")
                return
            if messagebox.askyesno(APP_NAME, "Delete all downloaded/cached filings? The next scan will re-download them."):
                try:
                    Cache.clear_all()
                    self.status_var.set("Cache cleared.")
                except Exception as exc:
                    messagebox.showerror(APP_NAME, str(exc))

        def show_help(self):
            messagebox.showinfo(APP_NAME, (
                f"{APP_NAME} v{APP_VERSION}\n\n"
                "Market-wide: reads EDGAR's daily index of every Form 4 filed on each weekday in the range "
                "(plus EDGAR's latest-filings feed for today), then downloads and parses each filing.\n\n"
                "Watchlist: looks up each ticker's CIK and lists only that company's Form 4 filings.\n\n"
                "Values = shares x price for common-stock (non-derivative) transactions. With 'Combine lots' "
                "on, all lots of the same code in one filing become one row with a weighted-average price.\n\n"
                "Requests are limited to 8/second per SEC policy. Parsed filings are cached, so re-scans "
                "with different filters are fast.\n\nData folder:\n" + app_data_dir()))

        def show_signal_help(self):
            messagebox.showinfo(APP_NAME, (
                "Finding true insider buying\n\n"
                "• Cluster = how many DIFFERENT insiders made the same kind of trade (e.g. open-market "
                "buys) in that company during the scanned dates - counted across all sizes and roles, not just "
                "the rows shown. Several insiders buying together is a stronger signal than one.\n\n"
                "• Stake Δ % = how much the trade grew the insider's holding, from the 'shares owned "
                "following transaction' on the Form 4. 'New' = their first shares.\n\n"
                "• 10b5-1 = the trade was made under a pre-scheduled trading plan (the Form 4 checkbox, or "
                "a footnote saying so). Planned trades say less about the insider's current view.\n\n"
                "• Pattern = the insider's earlier open-market buys in the same company over the last 5 years. "
                "Routine = around the same time of year (±3 weeks) in each of the last 3 years; Regular = 3 of "
                "the last 4 quarters, or the same time in 2 of the last 3 years; First buy = none in 5 years; "
                "Opportunistic = has bought before, with no schedule.\n\n"
                "• Sold Since = open-market sales (Code S) in the same company on or after the purchase date, as "
                "a % of the shares held right after buying. Shares withheld for taxes on vesting (Code F) are "
                "not counted. Re-scan later to refresh.\n\n"
                "• Signal Score (0-100) adds up all of the above plus company size, % of the company bought, "
                "price drop before the buy, any rise since, net insider buying/selling over 90 days and "
                "recent 13D filings. Right-click a row > 'Why this score?' to see every point.\n\n"
                "• File > Signal Track Record shows how logged signals have done since, versus SPY.\n\n"
                "The ★ preset sets: open-market buys only, ≥ $100k, last 30 days, all roles, "
                "≥ 2 insiders, no 10b5-1 plans, stake +10% or more, and hides routine/regular buyers and "
                "buyers who have sold since.\n\n"
                "This is a research filter, not investment advice."))

        def on_close(self):
            try:
                self._save()
            except Exception:
                pass
            if self.cancel_event:
                self.cancel_event.set()
            self.root.destroy()

    class TrackRecordWindow:
        """How past signals have done since the insider bought, vs SPY, grouped by score band."""
        COLS = [("first_seen", "Logged", 90, "w"), ("ticker", "Ticker", 64, "w"), ("insider", "Insider", 170, "w"),
                ("buy_date", "Buy Date", 90, "w"), ("price", "Paid", 76, "e"), ("score", "Score", 56, "center"),
                ("pattern", "Pattern", 200, "w"), ("now", "Last", 76, "e"), ("ret", "Return", 76, "e"),
                ("spy", "SPY", 70, "e"), ("excess", "vs SPY", 76, "e"), ("days", "Days", 54, "e")]

        def __init__(self, app):
            self.app = app
            self.q: queue.Queue = queue.Queue()
            self.win = tk.Toplevel(app.root)
            self.win.title(f"{APP_NAME} - Signal Track Record")
            self.win.geometry("1180x660")
            top = ttk.Frame(self.win, padding=(12, 10, 12, 4))
            top.pack(fill="x")
            ttk.Label(top, text="Signal Track Record", style="Title.TLabel").pack(side="left")
            ttk.Button(top, text="Clear history", command=self.clear).pack(side="right")
            ttk.Button(top, text="Export CSV...", command=self.export).pack(side="right", padx=6)
            self.refresh_btn = ttk.Button(top, text="Refresh prices", command=self.refresh)
            self.refresh_btn.pack(side="right")
            self.summary = tk.StringVar(value="Loading...")
            ttk.Label(self.win, textvariable=self.summary, padding=(12, 2), justify="left",
                      font=(app.base_font[0], app.base_font[1])).pack(fill="x")
            ttk.Label(self.win, padding=(12, 0, 12, 6), style="Hint.TLabel", wraplength=1120, justify="left",
                      text="Every open-market buy that received a score in a completed scan is logged once, with its "
                           "score at the time. Return = from the insider's purchase price to the latest close "
                           "(split/dividend adjusted). SPY = the S&P 500 ETF over the same dates. Double-click a row "
                           "for its price chart.").pack(fill="x")
            wrap = ttk.Frame(self.win, padding=(12, 0, 12, 10))
            wrap.pack(fill="both", expand=True)
            self.tree = ttk.Treeview(wrap, columns=[c[0] for c in self.COLS], show="headings")
            for key, head, w, anchor in self.COLS:
                self.tree.heading(key, text=head, command=lambda k=key: self.sort(k))
                self.tree.column(key, width=w, anchor=anchor, stretch=key in ("insider", "pattern"))
            vs = ttk.Scrollbar(wrap, orient="vertical", command=self.tree.yview)
            self.tree.configure(yscrollcommand=vs.set)
            self.tree.pack(side="left", fill="both", expand=True)
            vs.pack(side="right", fill="y")
            self.tree.tag_configure("up", foreground="#14532d")
            self.tree.tag_configure("down", foreground="#7f1d1d")
            self.tree.bind("<Double-1>", self._open)
            self.sort_key, self.sort_desc = "buy_date", True
            log = SignalLog()
            self.items = log.all()
            log.close()
            self.render()
            if self.items:
                self.refresh()
            else:
                self.summary.set("No signals logged yet - run a scan with 'Deep check buyers' on.")

        def render(self):
            self.tree.delete(*self.tree.get_children())

            def key(x):
                v = x.get(self.sort_key)
                return (v is None, v if v is not None else 0)
            for x in sorted(self.items, key=key, reverse=self.sort_desc):
                ret = x.get("ret")
                tag = ("up",) if (ret or 0) > 0 else (("down",) if ret is not None else ())
                self.tree.insert("", "end", iid=x["acc"], tags=tag, values=(
                    x["first_seen"], x["ticker"], x["insider"], x["buy_date"],
                    fmt_money(x["price"] or 0, 2), x["score"], x.get("pattern", ""),
                    fmt_money(x["now"], 2) if x.get("now") else "", fmt_pct(ret, digits=1),
                    fmt_pct(x.get("spy"), digits=1), fmt_pct(x.get("excess"), digits=1),
                    x.get("days", "")))
            self._summarise()

        def _summarise(self):
            if not self.items:
                return
            summ = track_record_summary(self.items)
            lines = []
            for band in ("70+", "50-69", "under 50"):
                b = summ.get(band)
                if not b:
                    lines.append(f"Score {band}:  no priced signals yet")
                    continue
                vs = "" if b["avg_excess"] is None else f",  vs SPY {b['avg_excess']:+.1f}%,  beat SPY {b['beat']:.0f}% of the time"
                lines.append(f"Score {band}:  {b['n']} signals,  average return {b['avg']:+.1f}%{vs}")
            self.summary.set("\n".join(lines))

        def sort(self, key):
            self.sort_desc = (not self.sort_desc) if self.sort_key == key else True
            self.sort_key = key
            self.render()

        def refresh(self):
            self.refresh_btn.config(state="disabled")
            self.summary.set("Fetching prices from Yahoo Finance...")
            items = [dict(x) for x in self.items]
            threading.Thread(target=self._fetch, args=(items,), daemon=True).start()
            self.win.after(200, self._poll)

        def _fetch(self, items):
            try:
                cache = Cache(cache_path())
                client = PriceClient(threading.Event())
                today = dt.date.today()

                def bars_for(sym):
                    raw = cache.get_blob(f"px:{sym}:{today}")
                    if raw is not None:
                        return json.loads(raw)
                    b = client.bars(sym)
                    if b:
                        cache.put_blob(f"px:{sym}:{today}", json.dumps(b).encode())
                    return b
                spy = bars_for("SPY") or []
                done = {}
                for x in items:
                    sym = yahoo_symbol(x["ticker"])
                    if sym and sym not in done:
                        done[sym] = bars_for(sym) or []
                    d = _to_date(x["buy_date"] or "")
                    pm = price_metrics(done.get(sym) or [], d, x["price"]) if (sym and d) else {}
                    sp = price_metrics(spy, d, None) if (spy and d) else {}
                    x["ret"] = pm.get("since_buy")
                    x["now"] = pm.get("last_close")
                    x["spy"] = sp.get("since_buy")
                    x["excess"] = (x["ret"] - x["spy"]) if (x["ret"] is not None and x["spy"] is not None) else None
                    x["days"] = (today - d).days if d else ""
                cache.close()
                self.q.put(items)
            except Exception as exc:
                self.q.put(exc)

        def _poll(self):
            try:
                res = self.q.get_nowait()
            except queue.Empty:
                if self.win.winfo_exists():
                    self.win.after(200, self._poll)
                return
            if not self.win.winfo_exists():
                return
            self.refresh_btn.config(state="normal")
            if isinstance(res, Exception):
                self.summary.set(f"Could not fetch prices: {res}")
                return
            self.items = res
            self.render()

        def _open(self, event):
            iid = self.tree.identify_row(event.y)
            x = next((i for i in self.items if i["acc"] == iid), None)
            sym = yahoo_symbol(x["ticker"]) if x else None
            if sym:
                webbrowser.open(f"https://finance.yahoo.com/quote/{sym}/chart")

        def export(self):
            path = filedialog.asksaveasfilename(parent=self.win, defaultextension=".csv",
                                                initialfile=f"signal_track_record_{dt.date.today():%Y%m%d}.csv",
                                                filetypes=[("CSV (Excel)", "*.csv")])
            if not path:
                return
            with open(path, "w", newline="", encoding="utf-8-sig") as fh:
                w = csv.writer(fh)
                w.writerow(["Logged", "Ticker", "Company", "Insider", "Buy Date", "Paid", "Value", "Score",
                            "Pattern", "Cluster", "Last", "Return %", "SPY %", "vs SPY %", "Days", "Accession"])
                for x in self.items:
                    w.writerow([x["first_seen"], x["ticker"], x["company"], x["insider"], x["buy_date"], x["price"],
                                x["value"], x["score"], x.get("pattern", ""), x.get("cluster", ""), x.get("now", ""),
                                "" if x.get("ret") is None else round(x["ret"], 2),
                                "" if x.get("spy") is None else round(x["spy"], 2),
                                "" if x.get("excess") is None else round(x["excess"], 2), x.get("days", ""), x["acc"]])

        def clear(self):
            if messagebox.askyesno(APP_NAME, "Delete the whole signal history? This cannot be undone.", parent=self.win):
                log = SignalLog()
                log.clear()
                log.close()
                self.items = []
                self.render()
                self.summary.set("History cleared.")

    root = tk.Tk()
    App(root)
    root.mainloop()


# ============================================================================
# CLI (handy for scripting / troubleshooting)
# ============================================================================
def apply_signal_filters(rows: list, clusters: dict, exclude_plan: bool, min_cluster: int,
                         min_stake: float, hide_routine: bool = False, hide_sold: bool = False,
                         exclude_offering: bool = False, exclude_entity: bool = False,
                         min_score: int = 0) -> list:
    """Same logic as the GUI's instant filters (used by the CLI and tests). Also computes scores."""
    out = []
    for r in rows:
        c = clusters.get(r["cluster_key"], (1, 0 if r.get("plan") else 1))
        all_n, disc_n = c[:2]
        r["cluster"], r["cluster_disc"] = (disc_n if exclude_plan else all_n), disc_n
        if exclude_plan and r.get("plan"):
            r["cluster"] = disc_n
        elif r["cluster"] < 1:
            r["cluster"] = 1
        so = r.get("shares_out")
        csh = (c[4] if exclude_plan else c[2]) if len(c) >= 6 else r["shares"]
        r["cluster_pct_co"] = (csh / so * 100.0) if so else None
        if r["code"] == "P":
            r["score"], r["score_why"] = signal_score(r)
        if exclude_offering and r.get("offering"):
            continue
        if exclude_entity and r.get("entity") and r["code"] == "P":
            continue
        if min_score and r["code"] == "P" and (r.get("score") or 0) < min_score:
            continue
        if exclude_plan and r.get("plan"):
            continue
        if min_cluster and r["cluster"] < min_cluster:
            continue
        if min_stake > 0 and r["code"] == "P":
            if r.get("stake") is None or r["stake"] < min_stake:
                continue
        if hide_routine and r.get("pattern_kind") in ("routine", "regular"):
            continue
        if hide_sold and r.get("sold_kind") == "sold":
            continue
        out.append(r)
    return out


def run_cli(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="form4_scanner.py --cli", description="Scan SEC Form 4 filings from the command line.")
    ap.add_argument("--ua", required=True, help='SEC contact, e.g. "Jane Doe jane@example.com"')
    ap.add_argument("--days", type=int, default=7)
    ap.add_argument("--start")
    ap.add_argument("--end")
    ap.add_argument("--tickers", default="")
    ap.add_argument("--roles", default="", help="comma list of: ceo,cfo,legal,director")
    ap.add_argument("--codes", default="P", help="comma list of P,S,A,M")
    ap.add_argument("--min", type=float, default=50000)
    ap.add_argument("--amendments", action="store_true")
    ap.add_argument("--no-combine", action="store_true")
    ap.add_argument("--exclude-plan", action="store_true", help="drop 10b5-1 plan trades")
    ap.add_argument("--clusters", type=int, default=0, help="only show trades with >= N different insiders")
    ap.add_argument("--min-stake", type=float, default=0, help="buys must grow the holding by >= this %%")
    ap.add_argument("--no-history", action="store_true", help="skip the Pattern / Sold Since lookup")
    ap.add_argument("--hide-routine", action="store_true", help="drop routine / regular buyers")
    ap.add_argument("--hide-sold", action="store_true", help="drop buyers who have sold since")
    ap.add_argument("--exclude-offering", action="store_true", help="drop offering / private-placement buys")
    ap.add_argument("--exclude-funds", action="store_true", help="drop buys by funds / companies")
    ap.add_argument("--min-score", type=int, default=0, help="only buys with Signal Score >= N")
    ap.add_argument("--csv", help="write results to this CSV file")
    a = ap.parse_args(argv)

    end = dt.date.fromisoformat(a.end) if a.end else dt.date.today()
    start = dt.date.fromisoformat(a.start) if a.start else end - dt.timedelta(days=a.days)
    role_map = {"ceo": ROLE_CEO, "cfo": ROLE_CFO, "legal": ROLE_LEGAL, "director": ROLE_DIR}
    params = ScanParams(start=start, end=end, user_agent=a.ua, tickers=parse_tickers(a.tickers),
                        roles={role_map[r.strip().lower()] for r in a.roles.split(",") if r.strip()},
                        codes={c.strip().upper() for c in a.codes.split(",") if c.strip()},
                        min_value=a.min, include_amendments=a.amendments, combine_lots=not a.no_combine,
                        check_history=not a.no_history)
    q: queue.Queue = queue.Queue()
    cancel = threading.Event()
    t = threading.Thread(target=Scanner(params, q, cancel).run, daemon=True)
    t.start()
    rows, last_print, clusters, history = [], 0.0, {}, {}
    try:
        while True:
            try:
                _sid, kind, payload = q.get(timeout=0.5)
            except queue.Empty:
                if not t.is_alive():
                    break
                continue
            if kind == "rows":
                rows.extend(payload)
            elif kind == "clusters":
                clusters.update(payload)
            elif kind in ("history", "context"):
                for acc, info in payload.items():
                    history.setdefault(acc, {}).update(info)
            elif kind in ("phase", "warn"):
                print(f"[{kind}] {payload}", file=sys.stderr)
            elif kind == "progress" and time.time() - last_print > 2:
                last_print = time.time()
                print(f"  scanned {payload['done']}/{payload['total']}  matches {payload['matched']}", file=sys.stderr)
            elif kind in ("done", "cancelled"):
                print(f"[{kind}] {payload}", file=sys.stderr)
                break
            elif kind == "error":
                print(f"[error] {payload}", file=sys.stderr)
                return 1
    except KeyboardInterrupt:
        cancel.set()
    for r in rows:
        if r["code"] == "P" and r["acc"] in history:
            r.update(history[r["acc"]])
    rows = apply_signal_filters(rows, clusters, a.exclude_plan, a.clusters, a.min_stake,
                                a.hide_routine, a.hide_sold, a.exclude_offering, a.exclude_funds, a.min_score)
    rows.sort(key=lambda r: (r.get("score") or -1, r["value"]), reverse=True)
    for r in rows:
        sc = "" if r.get("score") is None else r["score"]
        print(f"{r['filing_date']}  {r['ticker']:<6} score={sc!s:<3} {r['side']:<5} {fmt_money(r['value']):>15}  "
              f"{fmt_stake(r.get('stake')):>9}  cl={r['cluster']:<2} plan={'Y' if r.get('plan') else 'N'}  "
              f"{r['insider'][:26]:<26} {r.get('pattern', '')[:34]:<34} {r.get('sold', '')[:18]:<18} {r['link']}")
    if a.csv:
        with open(a.csv, "w", newline="", encoding="utf-8-sig") as fh:
            w = csv.writer(fh)
            w.writerow(CSV_HEADERS)
            w.writerows(row_to_csv(r) for r in rows)
        print(f"Wrote {len(rows)} rows to {a.csv}", file=sys.stderr)
    return 0


def main() -> None:
    if len(sys.argv) > 1 and sys.argv[1] == "--cli":
        sys.exit(run_cli(sys.argv[2:]))
    run_gui()


if __name__ == "__main__":
    main()
