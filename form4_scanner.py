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
APP_VERSION = "1.5.0"

SEC_WWW = "https://www.sec.gov"
SEC_DATA = "https://data.sec.gov"
MAX_REQUESTS_PER_SECOND = 8          # SEC's published limit is 10/sec
FETCH_WORKERS = 4
HISTORY_YEARS = 5                    # how far back to look for an insider's earlier buys
CONTEXT_DAYS = 90                    # window for "what other insiders did" and recent 13D filings
PRICE_RPS = 3                        # Yahoo Finance requests per second
BROWSER_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130 Safari/537.36"
CACHE_VERSION = 4                    # bump if the parsed-record format changes

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


# Footnotes that explain a sale as non-discretionary (taxes, estate planning, divorce, gifts ...)
EXPLAINED_RE = re.compile(
    r"\btax(?:es)?\b|withh?olding|sell[- ]to[- ]cover|to cover (?:the )?(?:exercise|strike|tax)|exercise price|"
    r"estate[- ]planning|divorce|domestic relations|charit|donat|\bgift|tuition|mortgage|"
    r"purchase (?:of )?(?:a |his |her |their )?(?:home|house|residence)|personal (?:financial |liquidity )?(?:planning|needs)|"
    r"diversif|margin (?:call|loan)|pledge|settlement of (?:restricted|performance|RSU)|"
    r"in connection with the (?:vesting|settlement)|upon (?:the )?vesting|vesting of (?:restricted|performance|RSU)",
    re.I)


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
    plan_notes, offer_notes, explain_notes = [], [], []
    for fn in root.findall("footnotes/footnote"):
        text = " ".join("".join(fn.itertext()).split())
        if PLAN_RE.search(text) and not NOT_PLAN_RE.search(text):
            plan_notes.append(fn.get("id", ""))
        if OFFERING_RE.search(text):
            offer_notes.append(fn.get("id", ""))
        if EXPLAINED_RE.search(text):
            explain_notes.append(fn.get("id", ""))
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
                "explained": bool(set(_footnote_ids(t)) & set(explain_notes)),
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
    quote = (ind.get("quote") or [{}])[0]
    closes = quote.get("close") or []
    vols = quote.get("volume") or [0] * len(closes)
    adj = ((ind.get("adjclose") or [{}])[0].get("adjclose")) or closes
    offset = (r.get("meta") or {}).get("gmtoffset") or 0
    bars = []
    for t, c, a, v in zip(ts, closes, adj, vols):
        if c is None or t is None:
            continue
        d = dt.datetime.fromtimestamp(t + offset, dt.timezone.utc).date().isoformat()
        bars.append((d, float(c), float(a if a is not None else c), float(v or 0)))
    return bars or None


def price_metrics(bars: list, buy_date: dt.date, paid: float | None) -> dict:
    """Split/dividend-safe price context around an insider purchase."""
    if not bars or buy_date is None:
        return {}
    iso = buy_date.isoformat()
    upto = [b for b in bars if b[0] <= iso]
    last = bars[-1]
    out = {"last_close": last[1], "last_date": last[0], "dollar_vol": dollar_volume(bars, iso)}
    if not upto:
        return out
    close_buy, adj_buy = upto[-1][1], upto[-1][2]
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


def business_days_between(d1: dt.date, d2: dt.date) -> int:
    """Weekdays after d1 up to and including d2 (0 if d2 <= d1)."""
    if not d1 or not d2 or d2 <= d1:
        return 0
    days, n = (d2 - d1).days, 0
    full, rem = divmod(days, 7)
    n = full * 5
    d = d1 + dt.timedelta(days=full * 7)
    for _ in range(rem):
        d += dt.timedelta(days=1)
        if d.weekday() < 5:
            n += 1
    return n


def late_business_days(trade_iso: str, filed_iso: str) -> int:
    """Business days a Form 4 was filed after its 2-business-day deadline."""
    t, f = _to_date(trade_iso), _to_date(filed_iso)
    if not t or not f:
        return 0
    return max(0, business_days_between(t, f) - 2)


def bar_on_or_before(bars: list, iso: str):
    lo, hi = 0, len(bars)
    while lo < hi:
        mid = (lo + hi) // 2
        if bars[mid][0] <= iso:
            lo = mid + 1
        else:
            hi = mid
    return bars[lo - 1] if lo else None


def forward_return(bars: list, start: dt.date, days: int, paid: float | None = None) -> float | None:
    """% return from the start date (or insider's price) to `days` calendar days later; None if not elapsed."""
    if not bars or start is None:
        return None
    end = start + dt.timedelta(days=days)
    if bars[-1][0] < end.isoformat():
        return None
    b0, b1 = bar_on_or_before(bars, start.isoformat()), bar_on_or_before(bars, end.isoformat())
    if not b0 or not b1 or not b0[2]:
        return None
    basis = (b0[1] / paid) if paid and paid > 0 else 1.0
    return (b1[2] / b0[2] * basis - 1.0) * 100.0


def dollar_volume(bars: list, iso: str, n: int = 20) -> float | None:
    """Average daily $ volume over the n sessions up to a date."""
    idx = None
    lo, hi = 0, len(bars)
    while lo < hi:
        mid = (lo + hi) // 2
        if bars[mid][0] <= iso:
            lo = mid + 1
        else:
            hi = mid
    idx = lo
    window = [b for b in bars[max(0, idx - n):idx] if len(b) > 3 and b[3]]
    if not window:
        return None
    return sum(b[1] * b[3] for b in window) / len(window)


def earnings_metrics(bars: list, earn_dates: list, buy_date: dt.date) -> dict:
    """Days since the last earnings release (8-K Item 2.02) before the buy, and the stock's move on it."""
    if not earn_dates or buy_date is None:
        return {}
    iso = buy_date.isoformat()
    prior = [d for d in earn_dates if d <= iso]
    if not prior:
        return {}
    e = max(prior)
    out = {"earn_date": e, "earn_days": (buy_date - dt.date.fromisoformat(e)).days}
    if bars:
        before = bar_on_or_before(bars, (dt.date.fromisoformat(e) - dt.timedelta(days=1)).isoformat())
        after = [b for b in bars if b[0] >= e][:2]
        if before and after and before[2]:
            out["earn_move"] = (after[-1][2] / before[2] - 1.0) * 100.0
    return out


def insider_record(prior_buys: list, bars: list, spy: list, asof: dt.date, horizon: int = 182) -> dict | None:
    """How this insider's earlier buys did over 6 months vs SPY (only buys at least 6 months old)."""
    if not prior_buys or not bars or not spy:
        return None
    by_day: dict[str, list] = {}
    for d_iso, sh, px in prior_buys:
        by_day.setdefault(d_iso, []).append((sh or 0.0, px or 0.0))
    ex = []
    for d_iso, lots in sorted(by_day.items()):
        d = _to_date(d_iso)
        if not d or d + dt.timedelta(days=horizon) > asof:
            continue
        sh = sum(x[0] for x in lots)
        px = (sum(x[0] * x[1] for x in lots) / sh) if sh else None
        r, m = forward_return(bars, d, horizon, px), forward_return(spy, d, horizon)
        if r is not None and m is not None:
            ex.append(r - m)
    if not ex:
        return {"n": 0, "avg_ex": None}
    return {"n": len(ex), "avg_ex": sum(ex) / len(ex)}


def fmt_ins_rec(rec: dict | None) -> str:
    if not rec:
        return ""
    if not rec.get("n"):
        return "no past buys"
    return f"{rec['avg_ex']:+.0f}% vs SPY ({rec['n']})"


def fact_value(facts: list, asof_iso: str | None = None, annual: bool = False) -> float | None:
    """Latest XBRL fact value (optionally only facts filed by a date; annual = ~12-month periods)."""
    sel = [f for f in facts if f.get("val") is not None and (asof_iso is None or f.get("filed", "") <= asof_iso)]
    if annual:
        def yearly(f):
            a, b = _to_date(f.get("start", "")), _to_date(f.get("end", ""))
            return bool(a and b and 330 <= (b - a).days <= 380)
        sel = [f for f in sel if yearly(f)]
    if not sel:
        return None
    best = max(sel, key=lambda f: (f.get("end", ""), f.get("filed", "")))
    return float(best["val"])


def health_flags(equity: float | None, cash: float | None, ocf: float | None, mcap: float | None) -> dict:
    out = {"bm": None, "low_cash": False}
    if equity is not None and mcap:
        out["bm"] = equity / mcap
    if ocf is not None and ocf < 0 and cash is not None and cash < -ocf:
        out["low_cash"] = True
    return out


def related_signature(rec: dict, code: str) -> tuple | None:
    """Same issuer, same lots (date, shares, price) = the same purchase reported by related owners."""
    lots = sorted((t.get("date", ""), round(t.get("sh") or 0.0), round(t.get("px") or 0.0, 4))
                  for t in rec.get("txns", []) if t.get("t") == "N" and t.get("code") == code)
    if not lots or not sum(x[1] for x in lots):
        return None
    return (str(rec.get("issuer_cik") or rec.get("ticker")), code, tuple(lots))


def looks_related(name1: str, name2: str, n_lots: int) -> bool:
    """Is an identical purchase by two owners plausibly the same economic purchase?"""
    if n_lots >= 2 or is_entity(name1) or is_entity(name2):
        return True
    stop = {"the", "and", "trust", "family", "revocable", "living", "irrevocable", "jr", "sr", "ii", "iii", "iv"}
    t1 = {w for w in re.findall(r"[a-z]{3,}", (name1 or "").lower()) if w not in stop}
    t2 = {w for w in re.findall(r"[a-z]{3,}", (name2 or "").lower()) if w not in stop}
    return bool(t1 & t2)


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
# Each factor: key -> (default points, description). The back-test can suggest new points.
FACTORS = {
    "cluster2": (12, "2 different insiders buying (cluster)"),
    "cluster3": (18, "3 different insiders buying (cluster)"),
    "cluster4": (22, "4+ different insiders buying (cluster)"),
    "ceo_cfo": (8, "CEO or CFO is the buyer"),
    "director": (3, "director is the buyer"),
    "val250k": (3, "purchase of $250k or more"),
    "val1m": (6, "purchase of $1M or more"),
    "stake10": (3, "holding up 10%+"),
    "stake25": (6, "holding up 25%+"),
    "stake50": (9, "holding up 50%+"),
    "stake100": (12, "new position or more than doubled holding"),
    "first": (10, "first open-market buy in 5+ years"),
    "opp": (5, "opportunistic (no buying schedule)"),
    "regular": (-12, "regular buyer (on a schedule)"),
    "routine": (-20, "routine buyer (same time every year)"),
    "sold_small": (-10, "has sold some shares since buying"),
    "sold_big": (-25, "has sold 25%+ since buying"),
    "plan": (-15, "made under a 10b5-1 plan"),
    "offering": (-25, "bought in a company offering / private placement"),
    "entity": (-15, "buyer is a fund or company, not a person"),
    "pct005": (4, "insiders bought 0.05%+ of the company"),
    "pct025": (7, "insiders bought 0.25%+ of the company"),
    "pct1": (10, "insiders bought 1%+ of the company"),
    "cap300m": (6, "micro/small cap (under $300M)"),
    "cap2b": (4, "small cap (under $2B)"),
    "cap10b": (1, "mid cap (under $10B)"),
    "mega": (-3, "mega cap ($200B+)"),
    "off20": (4, "bought 20%+ below 52-week high"),
    "off40": (7, "bought 40%+ below 52-week high"),
    "fell3m": (3, "stock fell 20%+ in the 3 months before"),
    "up30": (-6, "already up 30%+ since the purchase"),
    "net_buy": (4, "insiders net buyers over 90 days"),
    "net_sell": (-8, "insiders were heavy net sellers over 90 days"),
    "d13": (5, "Schedule 13D (5%+ stake) filed in last 90 days"),
    "ins_good": (6, "this insider's past buys beat SPY by 10%+ (6 months)"),
    "ins_bad": (-4, "this insider's past buys lagged SPY by 10%+ (6 months)"),
    "earn_drop": (4, "bought within 30 days of earnings that sent the stock down 10%+"),
    "bm05": (2, "book value 50%+ of market value"),
    "bm1": (4, "trading below book value"),
    "low_cash": (-5, "burning cash with under a year of cash left"),
    "neg_equity": (-3, "negative shareholders' equity"),
    "late": (-4, "Form 4 filed more than 10 business days late"),
    "thin": (-5, "thinly traded (under $250k a day)"),
}
# Sell Score: higher = stronger warning. Research: only large sales relative to holdings, and
# unexplained / discretionary ones, have carried negative information on average.
SELL_FACTORS = {
    "s_all": (22, "sold the entire position"),
    "s_pct50": (15, "sold 50%+ of the holding"),
    "s_pct25": (8, "sold 25%+ of the holding"),
    "s_small": (-8, "sold under 5% of the holding"),
    "s_discretionary": (8, "discretionary: no plan and no explanation given"),
    "s_explained": (-10, "explained in a footnote (taxes, estate planning, gifts ...)"),
    "s_plan": (-12, "made under a 10b5-1 plan"),
    "s_exercise": (-12, "exercise-and-sell of options the same day"),
    "s_ceo_cfo": (8, "CEO or CFO is the seller"),
    "s_director": (3, "director is the seller"),
    "s_val1m": (4, "sale of $1M or more"),
    "s_val5m": (8, "sale of $5M or more"),
    "s_first": (10, "first open-market sale in 5+ years"),
    "s_opp": (6, "opportunistic seller (no selling schedule)"),
    "s_regular": (-8, "regular seller (on a schedule)"),
    "s_routine": (-15, "routine seller (same time every year)"),
    "s_cluster2": (2, "2 different insiders selling"),
    "s_cluster3": (4, "3+ different insiders selling"),
    "s_runup": (5, "stock up 30%+ in the 3 months before the sale"),
    "s_near_high": (4, "sold within 5% of the 52-week high"),
    "s_late_qtr": (5, "sold late in the quarter, ahead of the next earnings report"),
    "s_net_sell": (4, "insiders were heavy net sellers over 90 days"),
    "s_net_buy": (-4, "insiders net buyers over 90 days"),
    "s_entity": (-8, "seller is a fund or company, not a person"),
    "s_offering": (-15, "sold in a registered / underwritten offering"),
    "s_late": (3, "Form 4 filed more than 10 business days late"),
}
ALL_FACTORS = {**FACTORS, **SELL_FACTORS}
DEFAULT_WEIGHTS = {k: v[0] for k, v in ALL_FACTORS.items()}
WEIGHTS = dict(DEFAULT_WEIGHTS)
SCORE_BASE = 40
SELL_BASE = 30


def set_weights(custom: dict | None) -> None:
    """Use back-test-suggested points (unknown keys ignored); None/{} = defaults."""
    WEIGHTS.clear()
    WEIGHTS.update(DEFAULT_WEIGHTS)
    for k, v in (custom or {}).items():
        if k in WEIGHTS:
            try:
                WEIGHTS[k] = int(v)
            except (TypeError, ValueError):
                pass


def score_factors(r: dict) -> list[tuple[str, str]]:
    """Which factors apply to a buy row -> [(key, readable label)]."""
    f: list[tuple[str, str]] = []

    def on(key: str, label: str | None = None) -> None:
        f.append((key, label or FACTORS[key][1]))

    n = int(r.get("cluster", 1) or 1)
    if n >= 4:
        on("cluster4", f"{n} different insiders buying (cluster)")
    elif n == 3:
        on("cluster3")
    elif n == 2:
        on("cluster2")
    roles = set(r.get("roles_all") or [])
    if ROLE_CEO in roles or ROLE_CFO in roles:
        on("ceo_cfo")
    elif ROLE_DIR in roles and not r.get("entity"):
        on("director")
    v = r.get("value") or 0
    if v >= 1_000_000:
        on("val1m")
    elif v >= 250_000:
        on("val250k")
    st = r.get("stake")
    if st is not None:
        if st == float("inf") or st >= 100:
            on("stake100")
        elif st >= 50:
            on("stake50", f"holding up {st:.0f}%")
        elif st >= 25:
            on("stake25", f"holding up {st:.0f}%")
        elif st >= 10:
            on("stake10", f"holding up {st:.0f}%")
    pk = r.get("pattern_kind")
    if pk in ("first", "opp", "regular", "routine"):
        on(pk)
    if r.get("sold_kind") == "sold":
        pct = r.get("sold_pct")
        on("sold_big" if (pct is None or pct >= 25) else "sold_small")
    if r.get("plan"):
        on("plan")
    if r.get("offering"):
        on("offering")
    if r.get("entity"):
        on("entity")
    pc = r.get("cluster_pct_co")
    if pc is not None:
        if pc >= 1:
            on("pct1", f"insiders bought {pc:.2f}% of the company")
        elif pc >= 0.25:
            on("pct025", f"insiders bought {pc:.2f}% of the company")
        elif pc >= 0.05:
            on("pct005", f"insiders bought {pc:.2f}% of the company")
    mc = r.get("mcap")
    if mc:
        if mc < 300e6:
            on("cap300m")
        elif mc < 2e9:
            on("cap2b")
        elif mc < 10e9:
            on("cap10b")
        elif mc >= 200e9:
            on("mega")
    oh = r.get("off_high")
    if oh is not None:
        if oh <= -40:
            on("off40", f"bought {abs(oh):.0f}% below 52-week high")
        elif oh <= -20:
            on("off20", f"bought {abs(oh):.0f}% below 52-week high")
    c3 = r.get("chg3m")
    if c3 is not None and c3 <= -20:
        on("fell3m", f"stock fell {abs(c3):.0f}% in the 3 months before")
    sb = r.get("since")
    if sb is not None and sb >= 30:
        on("up30", f"already up {sb:.0f}% since the purchase")
    fl = r.get("net90")
    if fl:
        if fl["sell"] >= 250_000 and fl["sell"] > 3 * max(fl["buy"], 1.0):
            on("net_sell")
        elif fl["buy"] > fl["sell"]:
            on("net_buy")
    if r.get("d13"):
        on("d13")
    ir = r.get("ins_rec")
    if ir and ir.get("n", 0) >= 2 and ir.get("avg_ex") is not None:
        if ir["avg_ex"] >= 10:
            on("ins_good", f"this insider's {ir['n']} past buys beat SPY by {ir['avg_ex']:.0f}% on average (6 months)")
        elif ir["avg_ex"] <= -10:
            on("ins_bad", f"this insider's {ir['n']} past buys lagged SPY by {abs(ir['avg_ex']):.0f}% (6 months)")
    ds, em = r.get("earn_days"), r.get("earn_move")
    if ds is not None and em is not None and 0 <= ds <= 30 and em <= -10:
        on("earn_drop", f"bought {ds} days after earnings that moved the stock {em:.0f}%")
    bm = r.get("bm")
    if bm is not None:
        if bm < 0:
            on("neg_equity")
        elif bm >= 1:
            on("bm1", f"trading below book value (book/market {bm:.2f})")
        elif bm >= 0.5:
            on("bm05", f"book/market {bm:.2f}")
    if r.get("low_cash"):
        on("low_cash")
    if (r.get("late_days") or 0) > 10:
        on("late", f"Form 4 filed {r['late_days']} business days late")
    dv = r.get("dollar_vol")
    if dv is not None and dv < 250_000:
        on("thin", f"thinly traded (about {fmt_compact(dv)} a day)")
    return f


def signal_score(r: dict, weights: dict | None = None) -> tuple[int | None, list]:
    """0-100 score for an open-market buy, with the reasons behind it.

    Starts at 40 and adds/subtracts each factor's points (adjustable via the back-test).
    Missing data simply adds nothing. Disqualifiers cap the score.
    """
    if r.get("code") != "P":
        return None, []
    w = WEIGHTS if weights is None else weights
    pts, why = SCORE_BASE, []
    for key, label in score_factors(r):
        n = w.get(key, 0)
        if n:
            pts += n
            why.append(f"{n:+d}  {label}")
    caps = []
    if r.get("offering"):
        caps.append((30, "offering / placement purchase"))
    if r.get("plan"):
        caps.append((45, "10b5-1 plan purchase"))
    if r.get("pattern_kind") == "routine":
        caps.append((45, "routine buyer"))
    if r.get("sold_kind") == "sold" and (r.get("sold_pct") is None or r.get("sold_pct") >= 25):
        caps.append((40, "sold 25%+ since"))
    if r.get("dup_of"):
        caps.append((20, "same purchase already reported by a related owner"))
    for cap, text in sorted(caps):
        if pts > cap:
            why.append(f"capped at {cap}: {text}")
            pts = cap
            break
    return max(0, min(100, int(round(pts)))), why


def sell_factors(r: dict) -> list[tuple[str, str]]:
    """Which sell factors apply to a sale row -> [(key, readable label)]."""
    f: list[tuple[str, str]] = []

    def on(key: str, label: str | None = None) -> None:
        f.append((key, label or SELL_FACTORS[key][1]))

    st = r.get("stake")
    if st is not None:
        if st <= -99.5:
            on("s_all")
        elif st <= -50:
            on("s_pct50", f"sold {abs(st):.0f}% of the holding")
        elif st <= -25:
            on("s_pct25", f"sold {abs(st):.0f}% of the holding")
        elif st > -5:
            on("s_small", f"sold {abs(st):.1f}% of the holding")
    if r.get("plan"):
        on("s_plan")
    if r.get("explained"):
        on("s_explained")
    if r.get("exercise_sell"):
        on("s_exercise")
    if not (r.get("plan") or r.get("explained") or r.get("exercise_sell") or r.get("offering")):
        on("s_discretionary")
    roles = set(r.get("roles_all") or [])
    if ROLE_CEO in roles or ROLE_CFO in roles:
        on("s_ceo_cfo")
    elif ROLE_DIR in roles and not r.get("entity"):
        on("s_director")
    v = r.get("value") or 0
    if v >= 5_000_000:
        on("s_val5m")
    elif v >= 1_000_000:
        on("s_val1m")
    pk = r.get("pattern_kind")
    if pk in ("first", "opp", "regular", "routine"):
        on("s_" + pk)
    n = int(r.get("cluster", 1) or 1)
    if n >= 3:
        on("s_cluster3", f"{n} different insiders selling")
    elif n == 2:
        on("s_cluster2")
    c3 = r.get("chg3m")
    if c3 is not None and c3 >= 30:
        on("s_runup", f"stock up {c3:.0f}% in the 3 months before the sale")
    oh = r.get("off_high")
    if oh is not None and oh >= -5:
        on("s_near_high")
    ds = r.get("earn_days")
    if ds is not None and 60 <= ds <= 100:
        on("s_late_qtr", f"sold {ds} days after the last earnings report (late in the quarter)")
    fl = r.get("net90")
    if fl:
        if fl["sell"] >= 250_000 and fl["sell"] > 3 * max(fl["buy"], 1.0):
            on("s_net_sell")
        elif fl["buy"] > fl["sell"]:
            on("s_net_buy")
    if r.get("entity"):
        on("s_entity")
    if r.get("offering"):
        on("s_offering")
    if (r.get("late_days") or 0) > 10:
        on("s_late", f"Form 4 filed {r['late_days']} business days late")
    return f


def sell_score(r: dict, weights: dict | None = None) -> tuple[int | None, list]:
    """0-100 warning score for an open-market sale (higher = more likely informative), with reasons."""
    if r.get("code") != "S":
        return None, []
    w = WEIGHTS if weights is None else weights
    pts, why = SELL_BASE, []
    for key, label in sell_factors(r):
        n = w.get(key, 0)
        if n:
            pts += n
            why.append(f"{n:+d}  {label}")
    caps = []
    if r.get("offering"):
        caps.append((30, "sold in an offering"))
    if r.get("exercise_sell"):
        caps.append((40, "exercise-and-sell"))
    if r.get("plan"):
        caps.append((45, "10b5-1 plan sale"))
    if r.get("explained"):
        caps.append((45, "explained sale"))
    if r.get("pattern_kind") == "routine":
        caps.append((45, "routine seller"))
    if r.get("dup_of"):
        caps.append((20, "same sale already reported by a related owner"))
    for cap, text in sorted(caps):
        if pts > cap:
            why.append(f"capped at {cap}: {text}")
            pts = cap
            break
    return max(0, min(100, int(round(pts)))), why


def rate_row(r: dict, weights: dict | None = None) -> tuple[int | None, list]:
    """Signal Score for buys, Sell Score for sales, nothing for other codes."""
    if r.get("code") == "P":
        return signal_score(r, weights)
    if r.get("code") == "S":
        return sell_score(r, weights)
    return None, []


# ----------------------------------------------------------------------------
# Track record of signals (kept even if the download cache is cleared)
# ----------------------------------------------------------------------------
class SignalLog:
    def __init__(self, path: str | None = None):
        self.conn = sqlite3.connect(path or cache_path(), timeout=30)
        self.conn.execute("""CREATE TABLE IF NOT EXISTS signals (
            acc TEXT PRIMARY KEY, ticker TEXT, company TEXT, insider TEXT, buy_date TEXT,
            price REAL, value REAL, score INTEGER, first_seen TEXT, pattern TEXT, cluster INTEGER)""")
        cols = {r[1] for r in self.conn.execute("PRAGMA table_info(signals)")}
        if "side" not in cols:                       # v1.5: sales are logged too
            self.conn.execute("ALTER TABLE signals ADD COLUMN side TEXT DEFAULT 'P'")
        self.conn.commit()

    def log(self, rows: list) -> int:
        n = 0
        today = dt.date.today().isoformat()
        for r in rows:
            if r.get("code") not in ("P", "S") or r.get("score") is None or not r.get("ticker") or r.get("dup_of"):
                continue
            key = r["acc"] if r["code"] == "P" else f"{r['acc']}|S"
            cur = self.conn.execute(
                "INSERT OR IGNORE INTO signals (acc, ticker, company, insider, buy_date, price, value, score, "
                "first_seen, pattern, cluster, side) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (key, r["ticker"], r.get("company", ""), r.get("insider", ""),
                 r.get("trade_start") or r.get("filing_date"), r.get("price"), r.get("value"),
                 r["score"], today, r.get("pattern", ""), r.get("cluster", 1), r["code"]))
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


def track_record_summary(items: list, side: str = "P") -> dict:
    """items: dicts with score, ret, excess -> per score band: count, avg return, avg vs SPY, % beating SPY.

    For sales (side "S") a good warning is one where the stock then lagged SPY, so 'beat' is the % that lagged.
    """
    out = {}
    items = [x for x in items if (x.get("side") or "P") == side]
    for band in ("70+", "50-69", "under 50"):
        sel = [x for x in items if score_band(x.get("score")) == band and x.get("ret") is not None]
        if not sel:
            out[band] = None
            continue
        ex = [x["excess"] for x in sel if x.get("excess") is not None]
        good = (lambda e: e > 0) if side == "P" else (lambda e: e < 0)
        out[band] = {"n": len(sel), "avg": sum(x["ret"] for x in sel) / len(sel),
                     "avg_excess": (sum(ex) / len(ex)) if ex else None,
                     "beat": (sum(1 for e in ex if good(e)) / len(ex) * 100) if ex else None}
    return out


class PriceClient(SecClient):
    """Daily prices from Yahoo Finance's public chart endpoint (no key needed)."""

    def __init__(self, cancel_event: threading.Event):
        super().__init__(BROWSER_UA, cancel_event, rps=PRICE_RPS)

    def bars(self, sym: str) -> list | None:
        q = urllib.parse.quote(sym)
        for host in ("query1", "query2"):
            url = f"https://{host}.finance.yahoo.com/v8/finance/chart/{q}?range=10y&interval=1d&includeAdjustedClose=true"
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
            stake = stake_change_pct(chunk)
            explained = any(x.get("explained") for x in chunk)
            exercise_sell = code == "S" and any(
                t.get("code") == "M" and t.get("date") in dates for t in record.get("txns", []))
            # deep check: every buy; sales only when they look significant (saves requests)
            deep = bool(params.check_history and (
                code == "P" or (code == "S" and not plan and not offering
                                and ((stake is not None and stake <= -10) or value >= 1_000_000))))
            pend = "\u2026" if deep else ""
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
                "stake": stake,
                "plan": plan,
                "cluster_key": cluster_key(record, code),
                "cluster": 1,
                "cluster_disc": 0 if plan else 1,
                "owner_cik": record["owners"][0].get("cik", ""),
                "issuer_cik": record.get("issuer_cik", "") or cand.get("issuer_cik") or "",
                "trade_start": dates[0] if dates else (record.get("filing_date") or ""),
                "held_after": holdings_after(chunk),
                "pattern": pend,
                "pattern_kind": "pending" if deep else "na",
                "sold": pend if code == "P" else "",
                "sold_kind": "pending" if (deep and code == "P") else "na",
                "sold_pct": None,
                "offering": offering,
                "entity": is_entity(record["owners"][0].get("name", "")),
                "roles_all": sorted(all_roles),
                "deep": deep,
                "late_days": late_business_days(dates[0] if dates else "", record.get("filing_date") or ""),
                "dup_of": "",
                "explained": explained,
                "exercise_sell": exercise_sell,
                "hkey": record["acc"] if code == "P" else f"{record['acc']}|{code}",
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
        self._lists: dict[str, dict | None] = {}   # CIK -> {"f4": {...}, "d13": [...], "earn": [...]}
        self._sigs: dict[tuple, tuple] = {}         # related-filer signature -> (accession, insider key)
        self.dups: dict[str, str] = {}              # duplicate accession -> first accession
        self._dups_new: dict[str, str] = {}

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
            if not (r["code"] == "P" or (r["code"] == "S" and r.get("deep"))):
                continue
            b = self.buys.get(r["hkey"])            # buys and significant sales, keyed by hkey
            if b is None:
                self.buys[r["hkey"]] = {k: r[k] for k in ("owner_cik", "issuer_cik", "trade_start", "ticker", "acc",
                                                           "code", "filing_date", "held_after", "value", "shares",
                                                           "price")}
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
        earn: list = []

        def add(block: dict) -> None:
            accs = block.get("accessionNumber", [])
            items = block.get("items") or [""] * len(accs)
            for acc, fdate, form, item in zip(accs, block.get("filingDate", []), block.get("form", []), items):
                if fdate < since:
                    continue
                if form == "4":
                    out[acc] = fdate
                elif re.match(r"(?:SC |SCHEDULE )13D", form or ""):
                    d13.append([fdate, form, acc])
                elif form in ("8-K", "8-K/A") and "2.02" in (item or ""):
                    earn.append(fdate)
        data = json.loads(client.get(f"{SEC_DATA}/submissions/CIK{cik:010d}.json"))
        filings = data.get("filings", {})
        recent = filings.get("recent", {})
        add(recent)
        dates = recent.get("filingDate", [])
        if dates and min(dates) > since:
            for f in filings.get("files", []):
                if f.get("filingTo", "") >= since:
                    add(json.loads(client.get(f"{SEC_DATA}/submissions/{f['name']}")))
        return {"f4": out, "d13": d13, "earn": sorted(set(earn))}

    @staticmethod
    def _unknown(reason: str = "Unavailable") -> dict:
        return {"pattern": reason, "pattern_kind": "unknown", "sold": reason, "sold_kind": "unknown", "sold_pct": None}

    def _classify(self, acc: str, buy: dict, accs: set | None, cache: Cache, owner: str, issuer: str) -> dict:
        trade = _to_date(buy["trade_start"]) or _to_date(buy["filing_date"])
        buy["prior_buys"] = []
        code = buy.get("code", "P")
        acc = buy.get("acc", acc)
        if accs is None or trade is None:
            out = self._unknown()
            if code != "P":
                out.update({"sold": "", "sold_kind": "na"})
            return out
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
                if t.get("t") == "N" and t.get("code") == code:
                    d = _to_date(t.get("date", ""))
                    if d and d < trade:
                        prior.append(d)
                        if code == "P" and not (rec.get("offering_doc") or t.get("offer")):
                            buy["prior_buys"].append((d.isoformat(), t.get("sh"), t.get("px")))
        p_kind, p_label = classify_pattern(trade, prior)
        if code != "P":
            return {"pattern": p_label.replace("First buy", "First sale"), "pattern_kind": p_kind,
                    "sold": "", "sold_kind": "na", "sold_pct": None}
        s_kind, s_label, s_pct = classify_later_sales(trade, records, buy.get("held_after"))
        return {"pattern": p_label, "pattern_kind": p_kind, "sold": s_label, "sold_kind": s_kind, "sold_pct": s_pct}

    def history_phase(self, client: SecClient, cache: Cache) -> None:
        since = (_years_ago(dt.date.today(), HISTORY_YEARS) - dt.timedelta(days=120)).isoformat()
        pairs: dict[tuple, list] = {}
        for hk, b in self.buys.items():
            if b["owner_cik"] and b["issuer_cik"]:
                pairs.setdefault((b["owner_cik"], str(b["issuer_cik"])), []).append(hk)
            else:
                self.post("history", {hk: self._unknown()})
        order = sorted(pairs, key=lambda k: -max(self.buys[a]["value"] for a in pairs[k]))
        total, done = len(order), 0
        if not total:
            return
        self.post("phase", f"Checking trading history for {total:,} insiders (buy pattern, later sales, seller pattern)...")
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
                    accs = (set(o_f4) & set(i_f4)) | {self.buys[h]["acc"] for h in pairs[pair]}
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

    @staticmethod
    def _fundamentals_task(client: SecClient, cik: int) -> dict:
        """Equity, cash and operating cash flow facts from SEC XBRL (most recent ~40 of each)."""
        def facts(concepts: list) -> list:
            for c in concepts:
                try:
                    data = json.loads(client.get(f"{SEC_DATA}/api/xbrl/companyconcept/CIK{cik:010d}/us-gaap/{c}.json",
                                                 retries=2))
                except NotFound:
                    continue
                f = (data.get("units") or {}).get("USD") or []
                if f:
                    f = sorted(f, key=lambda x: (x.get("end", ""), x.get("filed", "")))[-40:]
                    return [{k: x.get(k) for k in ("start", "end", "val", "filed")} for x in f]
            return []
        return {"equity": facts(["StockholdersEquity",
                                 "StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest"]),
                "cash": facts(["CashAndCashEquivalentsAtCarryingValue",
                               "CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents"]),
                "ocf": facts(["NetCashProvidedByUsedInOperatingActivities"])}

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
        spy_raw = cache.get_blob(f"px4:SPY:{today}")
        spy = json.loads(spy_raw) if spy_raw is not None else None
        if spy is None:
            try:
                spy = prices.bars("SPY") or []
                if spy:
                    cache.put_blob(f"px4:SPY:{today}", json.dumps(spy).encode())
            except Cancelled:
                raise
            except Exception:
                spy = []
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
                # 2b) equity / cash / operating cash flow (cached a week)
                funds: dict[str, dict | None] = {}
                futs = {}
                for cik in batch:
                    raw = cache.get_blob(f"fund:{cik}", max_age=7 * 86400)
                    if raw is not None:
                        funds[cik] = json.loads(raw)
                    else:
                        futs[ex.submit(self._fundamentals_task, client, int(cik))] = cik

                def ok_funds(cik, v):
                    funds[cik] = v
                    cache.put_blob(f"fund:{cik}", json.dumps(v).encode())
                collect(futs, ok_funds, lambda cik: funds.__setitem__(cik, None))
                # 3) daily prices (cached for the day)
                bars: dict[str, list | None] = {}
                futs = {}
                for cik in batch:
                    for acc in issuers[cik]:
                        sym = yahoo_symbol(self.buys[acc]["ticker"])
                        if not sym or sym in bars or sym in futs.values():
                            continue
                        raw = cache.get_blob(f"px4:{sym}:{today}")
                        if raw is not None:
                            bars[sym] = json.loads(raw)
                        else:
                            futs[ex.submit(prices.bars, sym)] = sym

                def ok_bars(sym, v):
                    bars[sym] = v
                    if v:
                        cache.put_blob(f"px4:{sym}:{today}", json.dumps(v).encode())
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
                    fd = funds.get(cik) or {}
                    equity = fact_value(fd.get("equity") or [])
                    cash = fact_value(fd.get("cash") or [])
                    ocf = fact_value(fd.get("ocf") or [], annual=True)
                    for acc in issuers[cik]:
                        b = self.buys[acc]
                        sym = yahoo_symbol(b["ticker"])
                        bdate = _to_date(b["trade_start"]) or _to_date(b["filing_date"])
                        sb = bars.get(sym) or [] if sym else []
                        pm = price_metrics(sb, bdate, b.get("price")) if sym else {}
                        mcap = (so * pm["last_close"]) if (so and pm.get("last_close")) else None
                        em = earnings_metrics(sb, (lst or {}).get("earn", []), bdate)
                        results[acc] = {
                            "shares_out": so,
                            "pct_co": ((b["shares"] / so * 100.0) if so else None) if b.get("code", "P") == "P" else None,
                            "mcap": mcap,
                            "off_high": pm.get("off_high"), "chg3m": pm.get("chg3m"),
                            "since": pm.get("since_buy"), "last_close": pm.get("last_close"),
                            "dollar_vol": pm.get("dollar_vol"),
                            "net90": flow, "d13": len(d13), "d13_last": d13[0][0] if d13 else "",
                            "earn_date": em.get("earn_date"), "earn_days": em.get("earn_days"),
                            "earn_move": em.get("earn_move"),
                            "equity": equity, "cash": cash, "ocf": ocf,
                            **health_flags(equity, cash, ocf, mcap),
                            "ins_rec": insider_record(b.get("prior_buys") or [], sb, spy or [], today)
                            if b.get("code", "P") == "P" else None,
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
            raw = cache.get_blob(f"f4list3:{cik}", max_age=6 * 3600)
            if raw is not None:
                self._lists[cik] = json.loads(raw)
            else:
                futs[ex.submit(self._form4_list_task, client, int(cik), since)] = cik

        def ok(cik, data):
            self._lists[cik] = data
            cache.put_blob(f"f4list3:{cik}", json.dumps(data).encode())
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
            sig = related_signature(rec, code)
            if sig is not None:
                first = self._sigs.get(sig)
                name = rec["owners"][0].get("name", "")
                if first is None:
                    self._sigs[sig] = (rec["acc"], who, name)
                elif first[1] != who and looks_related(first[2], name, len(sig[2])):
                    self.dups[rec["acc"]] = first[0]
                    self._dups_new[rec["acc"]] = first[0]
                    continue
            entry["accs"][rec["acc"]] = (sh_all, v_all, sh_disc, v_disc)
            entry["all"].add(who)
            if disc:
                entry["disc"].add(who)
            self._changed.add(key)

    def flush_clusters(self, force: bool = False) -> None:  # noqa: ARG002
        if self._dups_new:
            self.post("dups", dict(self._dups_new))
            self._dups_new = {}
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
            for acc in self.dups:                       # don't spend requests on duplicates
                self.buys.pop(acc, None)
                self.buys.pop(f"{acc}|S", None)
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
               "Insider Record (avg 6M vs SPY %)", "Insider Past Buys Measured", "Days Since Earnings",
               "Earnings Move %", "Book/Market", "Low Cash", "Avg Daily $ Volume", "Business Days Late", "Duplicate Of",
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
            rnd((r.get("ins_rec") or {}).get("avg_ex")), (r.get("ins_rec") or {}).get("n", ""),
            "" if r.get("earn_days") is None else r["earn_days"], rnd(r.get("earn_move")), rnd(r.get("bm"), 3),
            "Yes" if r.get("low_cash") else "", rnd(r.get("dollar_vol"), 0), r.get("late_days") or "",
            r.get("dup_of", ""),
            "" if r.get("sold_pct") is None else round(r["sold_pct"], 1),
            "; ".join(x.strip() for x in r.get("score_why", [])),
            r["trade_dates"], r["lots"], r["form"], r["acc"], r["link"], r["index_link"]]


# ============================================================================
# Back-test: SEC Insider Transactions Data Sets + Yahoo prices + XBRL frames
# ============================================================================
BULK_URLS = (
    "https://www.sec.gov/files/datastandardsinnovation/data/insider-transactions-data-sets/{q}_form345.zip",
    "https://www.sec.gov/files/structureddata/data/insider-transactions-data-sets/{q}_form345.zip",
)
HORIZONS = (("1M", 30), ("3M", 91), ("6M", 182))


def quarter_of(d: dt.date) -> str:
    return f"{d.year}q{(d.month - 1) // 3 + 1}"


def quarter_bounds(q: str) -> tuple[dt.date, dt.date]:
    y, n = int(q[:4]), int(q[-1])
    start = dt.date(y, 3 * (n - 1) + 1, 1)
    end = (dt.date(y + (n == 4), (3 * n) % 12 + 1, 1) - dt.timedelta(days=1))
    return start, end


def shift_quarter(q: str, k: int) -> str:
    y, n = int(q[:4]), int(q[-1]) - 1 + k
    return f"{y + n // 4}q{n % 4 + 1}"


def quarter_range(a: str, b: str) -> list[str]:
    out, q = [], a
    while q <= b and len(out) < 200:
        out.append(q)
        q = shift_quarter(q, 1)
    return out


def default_backtest_quarters(today: dt.date | None = None) -> tuple[str, str]:
    """Most recent four quarters whose signals have had ~6 months to play out."""
    end = quarter_of((today or dt.date.today()) - dt.timedelta(days=270))
    return shift_quarter(end, -3), end


def bulk_date(text: str) -> str:
    text = (text or "").strip()
    if not text:
        return ""
    for fmt in ("%d-%b-%Y", "%Y-%m-%d", "%m/%d/%Y"):
        try:
            return dt.datetime.strptime(text.title() if "-" in text and fmt == "%d-%b-%Y" else text, fmt).date().isoformat()
        except ValueError:
            continue
    return ""


def parse_bulk_zip(data: bytes) -> dict:
    """SEC Form 3/4/5 data set (one quarter) -> {accession: record} for Form 4s with P or S trades.

    Records use the same layout as parse_ownership_xml, so the live logic can be reused.
    """
    import io
    import zipfile
    zf = zipfile.ZipFile(io.BytesIO(data))
    names = {n.upper().rsplit("/", 1)[-1]: n for n in zf.namelist()}
    csv.field_size_limit(10_000_000)

    def rows(table: str):
        name = names.get(f"{table}.TSV") or names.get(f"{table}.TXT")
        if not name:
            return
        with zf.open(name) as fh:
            text = io.TextIOWrapper(fh, encoding="utf-8", errors="replace", newline="")
            yield from csv.DictReader(text, delimiter="\t", quoting=csv.QUOTE_NONE)

    recs: dict[str, dict] = {}
    for r in rows("SUBMISSION"):
        if (r.get("DOCUMENT_TYPE") or "").strip() != "4":
            continue
        acc = (r.get("ACCESSION_NUMBER") or "").strip()
        remarks = " ".join((r.get("REMARKS") or "").split())
        recs[acc] = {
            "acc": acc, "filing_date": bulk_date(r.get("FILING_DATE", "")), "form": "4",
            "issuer_cik": (r.get("ISSUERCIK") or "").strip().lstrip("0"),
            "issuer_name": (r.get("ISSUERNAME") or "").strip(),
            "ticker": (r.get("ISSUERTRADINGSYMBOL") or "").strip().upper(),
            "schema": "", "xml_file": "", "owners": [], "txns": [], "error": "",
            "plan10b5": (r.get("AFF10B5ONE") or "").strip().lower() in ("1", "true", "y", "yes"),
            "offering_doc": bool(OFFERING_RE.search(remarks)),
        }
    for r in rows("REPORTINGOWNER"):
        rec = recs.get((r.get("ACCESSION_NUMBER") or "").strip())
        if rec is None:
            continue
        rel = (r.get("RPTOWNER_RELATIONSHIP") or "").upper()
        rec["owners"].append({
            "name": (r.get("RPTOWNERNAME") or "").strip(), "cik": (r.get("RPTOWNERCIK") or "").strip().lstrip("0"),
            "dir": "DIRECTOR" in rel, "off": "OFFICER" in rel, "ten": "TENPERCENT" in rel.replace(" ", "").replace("%", "PERCENT"),
            "oth": "OTHER" in rel, "title": (r.get("RPTOWNER_TITLE") or "").strip(),
            "other": (r.get("RPTOWNER_TXT") or "").strip(),
        })
    plan_fn: dict[str, set] = {}
    offer_fn: dict[str, set] = {}
    expl_fn: dict[str, set] = {}
    for r in rows("FOOTNOTES"):
        acc = (r.get("ACCESSION_NUMBER") or "").strip()
        if acc not in recs:
            continue
        text = " ".join((r.get("FOOTNOTE_TXT") or "").split())
        fid = (r.get("FOOTNOTE_ID") or "").strip()
        if PLAN_RE.search(text) and not NOT_PLAN_RE.search(text):
            plan_fn.setdefault(acc, set()).add(fid)
        if OFFERING_RE.search(text):
            offer_fn.setdefault(acc, set()).add(fid)
        if EXPLAINED_RE.search(text):
            expl_fn.setdefault(acc, set()).add(fid)
    for r in rows("NONDERIV_TRANS"):
        acc = (r.get("ACCESSION_NUMBER") or "").strip()
        rec = recs.get(acc)
        code = (r.get("TRANS_CODE") or "").strip().upper()
        if rec is None or code not in ("P", "S", "M"):
            continue
        fns = set()
        for k, v in r.items():
            if k and k.endswith("_FN") and v:
                fns.update(x.strip() for x in v.split(",") if x.strip())
        try:
            sk = int(r.get("NONDERIV_TRANS_SK") or 0)
        except ValueError:
            sk = 0
        rec["txns"].append({
            "t": "N", "code": code, "sk": sk,
            "sh": _num(r.get("TRANS_SHARES", "")), "px": _num(r.get("TRANS_PRICEPERSHARE", "")),
            "ad": (r.get("TRANS_ACQUIRED_DISP_CD") or "").strip().upper(),
            "date": bulk_date(r.get("TRANS_DATE", "")), "sec": (r.get("SECURITY_TITLE") or "").strip(),
            "after": _num(r.get("SHRS_OWND_FOLWNG_TRANS", "")),
            "own": (r.get("DIRECT_INDIRECT_OWNERSHIP") or "D").strip().upper() or "D",
            "nat": (r.get("NATURE_OF_OWNERSHIP") or "").strip(),
            "plan": bool(fns & plan_fn.get(acc, set())), "offer": bool(fns & offer_fn.get(acc, set())),
            "explained": bool(fns & expl_fn.get(acc, set())),
        })
    out = {}
    for acc, rec in recs.items():
        if any(t["code"] in ("P", "S") for t in rec["txns"]) and rec["owners"] and rec["filing_date"]:
            rec["txns"].sort(key=lambda t: t.pop("sk", 0))
            out[acc] = rec
    return out


def frame_period_instant(d: dt.date) -> str:
    """Balance-sheet frame two quarters before a date (so the figure was already public)."""
    q = shift_quarter(quarter_of(d), -2)
    return f"CY{q[:4]}Q{q[-1]}I"


def frame_period_annual(d: dt.date) -> str:
    return f"CY{d.year - 1 if d.month >= 5 else d.year - 2}"


def parse_frame(raw: bytes) -> dict:
    try:
        data = json.loads(raw).get("data") or []
    except (ValueError, AttributeError):
        return {}
    return {str(x.get("cik")): float(x["val"]) for x in data if x.get("val") is not None and x.get("cik") is not None}


@dataclass
class BacktestParams:
    start_q: str
    end_q: str
    user_agent: str
    warmup_quarters: int = 8
    min_value: float = 100_000.0
    max_signals: int = 2000
    company_data: bool = True
    side: str = "P"                        # "P" = test the buy Signal Score, "S" = the Sell Score


def _mean(xs: list) -> float | None:
    xs = [x for x in xs if x is not None]
    return (sum(xs) / len(xs)) if xs else None


def band_stats(rows: list, score_key: str = "score", side: str = "P") -> dict:
    out = {}
    for band in ("70+", "50-69", "under 50", "All"):
        sel = rows if band == "All" else [r for r in rows if score_band(r.get(score_key)) == band]
        st = {"n": len(sel)}
        for h, _days in HORIZONS:
            st[f"ret{h}"] = _mean([r.get(f"ret{h}") for r in sel])
            st[f"ex{h}"] = _mean([r.get(f"ex{h}") for r in sel])
        ex6 = [r["ex6M"] for r in sel if r.get("ex6M") is not None]
        st["n6"] = len(ex6)
        good = (lambda x: x > 0) if side == "P" else (lambda x: x < 0)
        st["hit6"] = (sum(1 for x in ex6 if good(x)) / len(ex6) * 100.0) if ex6 else None
        out[band] = st
    return out


def factor_stats(rows: list, min_n: int = 30, side: str = "P") -> list[dict]:
    """One-factor comparison of 6-month excess return with vs without each factor.

    Buys: points follow the difference. Sales: a higher Sell Score should mean a WORSE return,
    so points follow minus the difference.
    """
    priced = [r for r in rows if r.get("ex6M") is not None]
    out = []
    sign = 1 if side == "P" else -1
    for key, (default, label) in (FACTORS if side == "P" else SELL_FACTORS).items():
        w = [r["ex6M"] for r in priced if key in r.get("factors", ())]
        wo = [r["ex6M"] for r in priced if key not in r.get("factors", ())]
        if not w:
            continue
        a, b = _mean(w), _mean(wo)
        uplift = (a - b) if (a is not None and b is not None) else None
        ok = len(w) >= min_n and len(wo) >= min_n and uplift is not None
        sugg = max(-25, min(25, int(round(sign * uplift)))) if ok else WEIGHTS.get(key, default)
        out.append({"key": key, "label": label, "current": WEIGHTS.get(key, default), "n_with": len(w),
                    "n_without": len(wo), "with": a, "without": b, "uplift": uplift, "suggested": sugg,
                    "enough": ok})
    return out


class Backtester:
    """Runs a back-test on a background thread, posting (kind, payload) to a queue."""

    def __init__(self, params: BacktestParams, out_q: queue.Queue, cancel: threading.Event):
        self.p = params
        self.q = out_q
        self.cancel = cancel

    def post(self, kind: str, payload=None) -> None:
        self.q.put((0, kind, payload))

    def check_cancel(self) -> None:
        if self.cancel.is_set():
            raise Cancelled()

    # -- data ------------------------------------------------------------------
    def _bulk_quarter(self, client: SecClient, q: str) -> bytes | None:
        folder = os.path.join(app_data_dir(), "bulk")
        os.makedirs(folder, exist_ok=True)
        path = os.path.join(folder, f"{q}_form345.zip")
        if os.path.exists(path) and os.path.getsize(path) > 1000:
            with open(path, "rb") as fh:
                return fh.read()
        for url in BULK_URLS:
            try:
                data = client.get(url.format(q=q))
            except NotFound:
                continue
            if data[:2] == b"PK":
                with open(path + ".part", "wb") as fh:
                    fh.write(data)
                os.replace(path + ".part", path)
                return data
        return None

    def _frame(self, client: SecClient, cache: Cache, tax: str, concept: str, unit: str, period: str) -> dict:
        key = f"frame:{tax}:{concept}:{period}"
        raw = cache.get_blob(key, max_age=30 * 86400)
        if raw is None:
            try:
                raw = client.get(f"{SEC_DATA}/api/xbrl/frames/{tax}/{concept}/{unit}/{period}.json", retries=2)
            except NotFound:
                raw = b"{}"
            cache.put_blob(key, raw)
        return parse_frame(raw)

    def run(self) -> None:
        cache = None
        try:
            cache = Cache(cache_path())
            self._run(cache)
        except Cancelled:
            self.post("cancelled", None)
        except SecAccessDenied as exc:
            self.post("error", str(exc))
        except Exception as exc:
            self.post("error", f"{type(exc).__name__}: {exc}\n\n{traceback.format_exc(limit=5)}")
        finally:
            if cache:
                cache.close()

    def _run(self, cache: Cache) -> None:
        p = self.p
        client = SecClient(p.user_agent, self.cancel)
        prices = PriceClient(self.cancel)
        eval_start, _ = quarter_bounds(p.start_q)
        _, eval_end = quarter_bounds(p.end_q)
        quarters = quarter_range(shift_quarter(p.start_q, -max(0, p.warmup_quarters)), p.end_q)

        # 1) SEC quarterly data sets
        records: dict[str, dict] = {}
        missing = []
        for i, q in enumerate(quarters):
            self.check_cancel()
            self.post("phase", f"Loading SEC insider data set {q.upper()} ({i + 1} of {len(quarters)})...")
            self.post("bt_progress", (i, len(quarters)))
            data = self._bulk_quarter(client, q)
            if data is None:
                missing.append(q.upper())
                continue
            records.update(parse_bulk_zip(data))
        if not records:
            raise RuntimeError("No SEC insider data could be loaded for that period.")

        # 2) indexes (as reported, with filing dates so nothing is used before it was public)
        self.post("phase", f"Indexing {len(records):,} Form 4 filings...")
        by_issuer: dict[str, list] = {}
        buys_by_pair: dict[tuple, list] = {}
        sells_by_pair: dict[tuple, list] = {}
        side = p.side if p.side in ("P", "S") else "P"
        sigs: dict[tuple, tuple] = {}
        dups: set = set()
        for rec in sorted(records.values(), key=lambda r: (r["filing_date"], r["acc"])):
            iss = rec["issuer_cik"] or rec["ticker"]
            who = owner_key(rec)
            for code in ("P", "S"):
                sig = related_signature(rec, code)
                if sig is None:
                    continue
                first = sigs.get(sig)
                name = rec["owners"][0].get("name", "")
                if first is None:
                    sigs[sig] = (rec["acc"], who, name)
                elif first[1] != who and looks_related(first[2], name, len(sig[2])):
                    dups.add(rec["acc"])
            if rec["acc"] in dups:
                continue
            for t in rec["txns"]:
                if not t.get("date"):
                    continue
                disc = not (rec.get("plan10b5") or t.get("plan") or t.get("offer") or rec.get("offering_doc"))
                by_issuer.setdefault(iss, []).append(
                    (t["date"], rec["filing_date"], who, t["code"], t.get("sh") or 0.0, t.get("px") or 0.0, disc))
                if t["code"] == "P" and not (t.get("offer") or rec.get("offering_doc")):
                    buys_by_pair.setdefault((who, iss), []).append((t["date"], rec["filing_date"], t.get("sh"), t.get("px")))
                elif t["code"] == "S":
                    sells_by_pair.setdefault((who, iss), []).append((t["date"], rec["filing_date"], t.get("sh"), t.get("px")))

        # 3) candidate signals in the evaluation period
        sp = ScanParams(start=eval_start, end=eval_end, user_agent=p.user_agent, codes={side},
                        min_value=p.min_value, combine_lots=True, check_history=False)
        cands = []
        for rec in records.values():
            if not (eval_start.isoformat() <= rec["filing_date"] <= eval_end.isoformat()) or rec["acc"] in dups:
                continue
            if not yahoo_symbol(rec.get("ticker", "")):
                continue
            cands.extend(evaluate_record(rec, {}, sp))
        total_cands = len(cands)
        if p.max_signals and len(cands) > p.max_signals:
            import random
            cands = random.Random(7).sample(cands, p.max_signals)
        cands.sort(key=lambda r: r["filing_date"])
        if not cands:
            raise RuntimeError("No qualifying purchases found in that period.")

        # 4) prices (cached a week) for every ticker + SPY
        syms = sorted({yahoo_symbol(r["ticker"]) for r in cands} | {"SPY"})
        bars: dict[str, list] = {}
        ex = ThreadPoolExecutor(max_workers=FETCH_WORKERS)
        try:
            todo = {}
            for s_ in syms:
                raw = cache.get_blob(f"pxbt:{s_}", max_age=7 * 86400)
                if raw is not None:
                    bars[s_] = json.loads(raw)
                else:
                    todo[s_] = None
            futs = {ex.submit(prices.bars, s_): s_ for s_ in todo}
            done = 0
            for fut in as_completed(futs):
                s_ = futs[fut]
                try:
                    b = fut.result() or []
                except Cancelled:
                    raise
                except Exception:
                    b = []
                bars[s_] = b
                cache.put_blob(f"pxbt:{s_}", json.dumps(b).encode())
                done += 1
                if done % 10 == 0 or done == len(futs):
                    self.post("phase", f"Downloading price history: {done:,} of {len(futs):,} tickers "
                                       f"({len(syms) - len(futs):,} cached)...")
                    self.post("bt_progress", (done, len(futs)))
                self.check_cancel()

            # 5) company data: XBRL frames (all companies per request) + 13D / earnings dates per company
            frames: dict[str, dict] = {}
            lists: dict[str, dict] = {}
            if p.company_data:
                periods_i = sorted({frame_period_instant(_to_date(r["filing_date"])) for r in cands})
                periods_a = sorted({frame_period_annual(_to_date(r["filing_date"])) for r in cands})
                self.post("phase", "Downloading company data from SEC XBRL frames...")
                for per in periods_i:
                    self.check_cancel()
                    frames[f"shares:{per}"] = (self._frame(client, cache, "dei", "EntityCommonStockSharesOutstanding", "shares", per)
                                               or self._frame(client, cache, "us-gaap", "CommonStockSharesOutstanding", "shares", per))
                    frames[f"equity:{per}"] = self._frame(client, cache, "us-gaap", "StockholdersEquity", "USD", per)
                    frames[f"cash:{per}"] = self._frame(client, cache, "us-gaap", "CashAndCashEquivalentsAtCarryingValue", "USD", per)
                for per in periods_a:
                    self.check_cancel()
                    frames[f"ocf:{per}"] = self._frame(client, cache, "us-gaap", "NetCashProvidedByUsedInOperatingActivities", "USD", per)
                ciks = sorted({r["issuer_cik"] for r in cands if r.get("issuer_cik")})
                since = quarter_bounds(quarters[0])[0].isoformat()
                futs = {}
                for cik in ciks:
                    raw = cache.get_blob(f"btlist:{cik}", max_age=7 * 86400)
                    if raw is not None:
                        lists[cik] = json.loads(raw)
                    else:
                        futs[ex.submit(Scanner._form4_list_task, client, int(cik), since)] = cik
                done = 0
                for fut in as_completed(futs):
                    cik = futs[fut]
                    try:
                        lists[cik] = fut.result()
                        cache.put_blob(f"btlist:{cik}", json.dumps(lists[cik]).encode())
                    except Cancelled:
                        raise
                    except Exception:
                        lists[cik] = {}
                    done += 1
                    if done % 20 == 0 or done == len(futs):
                        self.post("phase", f"Checking 13D & earnings dates: {done:,} of {len(futs):,} companies...")
                        self.post("bt_progress", (done, len(futs)))
                    self.check_cancel()
        finally:
            ex.shutdown(wait=False, cancel_futures=True)

        # 6) features as of each filing date, forward returns, scores
        self.post("phase", "Scoring signals and measuring returns...")
        spy = bars.get("SPY") or []
        out = []
        for r in cands:
            fdate = _to_date(r["filing_date"])
            tdate = _to_date(r["trade_start"]) or fdate
            iss = r["issuer_cik"] or r["ticker"]
            who = r["owner_cik"] or r["insider"].lower()
            hist = by_issuer.get(iss, [])
            fiso = r["filing_date"]
            # cluster: different discretionary buyers in the 30 days to this trade, already reported
            lo = (tdate - dt.timedelta(days=30)).isoformat()
            win = [h for h in hist if h[3] == side and h[6] and lo <= h[0] <= fiso and h[1] <= fiso]
            r["cluster"] = max(1, len({h[2] for h in win} | ({who} if not (r.get("plan") or r.get("offering")) else set())))
            cl_sh = sum(h[4] for h in win)
            # pattern from earlier buys by the same insider
            same = buys_by_pair if side == "P" else sells_by_pair
            prior = [_to_date(b[0]) for b in same.get((who, iss), []) if b[1] <= fiso and b[0] < tdate.isoformat()]
            r["pattern_kind"], r["pattern"] = classify_pattern(tdate, [d for d in prior if d])
            # net insider flow, 90 days to the filing
            lo90 = (fdate - dt.timedelta(days=90)).isoformat()
            buy = sum(h[4] * h[5] for h in hist if h[3] == "P" and lo90 <= h[0] <= fiso and h[1] <= fiso)
            sell = sum(h[4] * h[5] for h in hist if h[3] == "S" and lo90 <= h[0] <= fiso and h[1] <= fiso)
            r["net90"] = {"buy": buy, "sell": sell,
                          "buyers": len({h[2] for h in hist if h[3] == "P" and lo90 <= h[0] <= fiso and h[1] <= fiso}),
                          "sellers": len({h[2] for h in hist if h[3] == "S" and lo90 <= h[0] <= fiso and h[1] <= fiso})}
            # prices as of the filing date (when you could have acted)
            sym = yahoo_symbol(r["ticker"])
            b = bars.get(sym) or []
            pm = price_metrics([x for x in b if x[0] <= fiso], tdate, r.get("price"))
            r["off_high"], r["chg3m"], r["since"] = pm.get("off_high"), pm.get("chg3m"), pm.get("since_buy")
            r["dollar_vol"] = pm.get("dollar_vol")
            close_f = pm.get("last_close")
            # insider's own record from buys at least 6 months before this filing
            pb = [(x[0], x[2], x[3]) for x in buys_by_pair.get((who, iss), []) if x[1] <= fiso and x[0] < tdate.isoformat()]
            r["ins_rec"] = insider_record(pb, b, spy, fdate) if side == "P" else None
            # company data
            if p.company_data:
                per_i, per_a = frame_period_instant(fdate), frame_period_annual(fdate)
                cik = str(r.get("issuer_cik") or "")
                so = frames.get(f"shares:{per_i}", {}).get(cik)
                r["shares_out"] = so
                r["mcap"] = (so * close_f) if (so and close_f) else None
                r["cluster_pct_co"] = (cl_sh / so * 100.0) if (so and side == "P") else None
                eq = frames.get(f"equity:{per_i}", {}).get(cik)
                cash = frames.get(f"cash:{per_i}", {}).get(cik)
                ocf = frames.get(f"ocf:{per_a}", {}).get(cik)
                r.update(health_flags(eq, cash, ocf, r["mcap"]))
                lst = lists.get(cik) or {}
                r["d13"] = sum(1 for d in lst.get("d13", []) if lo90 <= d[0] <= fiso)
                em = earnings_metrics([x for x in b if x[0] <= fiso], [d for d in lst.get("earn", []) if d <= fiso], tdate)
                r["earn_days"], r["earn_move"] = em.get("earn_days"), em.get("earn_move")
            # forward returns from the filing date
            for h, days in HORIZONS:
                rv, mv = forward_return(b, fdate, days), forward_return(spy, fdate, days)
                r[f"ret{h}"] = rv
                r[f"ex{h}"] = (rv - mv) if (rv is not None and mv is not None) else None
            r["factors"] = [k for k, _ in (score_factors(r) if side == "P" else sell_factors(r))]
            r["score"], r["score_why"] = rate_row(r)
            out.append(r)

        # 7) statistics, plus weights tuned on the first half and checked on the second
        priced = [r for r in out if r.get("ex6M") is not None]
        half = len(priced) // 2
        train, test = priced[:half], priced[half:]
        fs_all = factor_stats(out, side=side)
        fs_train = factor_stats(train, side=side)
        suggested = {f["key"]: f["suggested"] for f in fs_train if f["enough"]}
        tuned_w = dict(WEIGHTS)
        tuned_w.update(suggested)
        for r in test:
            r["score_tuned"], _ = rate_row(r, tuned_w)
        res = {
            "params": {"start": p.start_q.upper(), "end": p.end_q.upper(), "warmup": p.warmup_quarters,
                       "min_value": p.min_value, "max_signals": p.max_signals, "side": side},
            "filings": len(records), "candidates": total_cands, "signals": len(out),
            "priced": sum(1 for r in out if r.get("ret1M") is not None), "priced6": len(priced),
            "dups": len(dups), "missing_quarters": missing,
            "bands": band_stats(out, side=side), "factors": fs_all, "suggested": suggested,
            "split_date": priced[half]["filing_date"] if priced and half < len(priced) else "",
            "test_current": band_stats(test, "score", side), "test_tuned": band_stats(test, "score_tuned", side),
            "rows": out,
        }
        self.post("bt_done", res)


BT_CSV_HEADERS = ["Filing Date", "Trade Date", "Ticker", "Company", "Insider", "Value ($)", "Score", "Factors",
                  "Return 1M %", "Return 3M %", "Return 6M %", "vs SPY 1M %", "vs SPY 3M %", "vs SPY 6M %",
                  "Cluster", "Pattern", "Mkt Cap", "Off 52w High %", "Accession"]


def bt_row_csv(r: dict) -> list:
    def rnd(v):
        return "" if v is None else round(v, 2)
    return [r["filing_date"], r.get("trade_start", ""), r["ticker"], r.get("company", ""), r.get("insider", ""),
            round(r.get("value") or 0, 2), r.get("score"), " ".join(r.get("factors", [])),
            rnd(r.get("ret1M")), rnd(r.get("ret3M")), rnd(r.get("ret6M")),
            rnd(r.get("ex1M")), rnd(r.get("ex3M")), rnd(r.get("ex6M")),
            r.get("cluster", 1), r.get("pattern", ""), rnd(r.get("mcap")), rnd(r.get("off_high")), r["acc"]]


# ============================================================================
# GUI
# ============================================================================
THEMES = {
    "Light": {
        "bg": "#f3f5f8", "panel": "#ffffff", "fg": "#1f2933", "muted": "#6b7480", "border": "#d5dbe3",
        "accent": "#2563eb", "accent_fg": "#ffffff", "accent_hover": "#1d4ed8", "header": "#1e3a5f",
        "header_fg": "#ffffff", "header_btn": "#2b4c78", "header_btn_hover": "#355c90", "entry": "#ffffff",
        "select": "#cfe0ff", "select_fg": "#0b1220", "tree": "#ffffff", "heading": "#e8edf4",
        "buy_bg": "#e8f6ec", "buy_fg": "#14532d", "sell_bg": "#fbeaea", "sell_fg": "#7f1d1d",
        "hot_bg": "#c4ecd1", "hot_fg": "#0b3d20", "hotsell_bg": "#f5c6c6", "hotsell_fg": "#6b0f0f",
        "flip_bg": "#fdebd3", "flip_fg": "#8a4b08", "routine_bg": "#eeeeee", "routine_fg": "#5f6670",
        "grant_bg": "#eef2fb", "exercise_bg": "#f6f1e7", "group_bg": "#dde5f0", "group_fg": "#102a43",
        "card": "#ffffff", "good": "#15803d", "bad": "#b91c1c", "tip": "#ffffe1", "dark": False},
    "Dark": {
        "bg": "#15171c", "panel": "#1e2229", "fg": "#e5e7eb", "muted": "#9aa3af", "border": "#323844",
        "accent": "#3b82f6", "accent_fg": "#ffffff", "accent_hover": "#2563eb", "header": "#0d0f13",
        "header_fg": "#f3f4f6", "header_btn": "#23272f", "header_btn_hover": "#2e3440", "entry": "#272c35",
        "select": "#2f4a73", "select_fg": "#ffffff", "tree": "#1a1d23", "heading": "#252a33",
        "buy_bg": "#16301f", "buy_fg": "#a7f3c0", "sell_bg": "#371b1e", "sell_fg": "#fca5a5",
        "hot_bg": "#1d4a31", "hot_fg": "#d1fae5", "hotsell_bg": "#5a1d23", "hotsell_fg": "#ffe1e1",
        "flip_bg": "#45301a", "flip_fg": "#fcd9a8", "routine_bg": "#22252b", "routine_fg": "#878f9c",
        "grant_bg": "#1c2638", "exercise_bg": "#332e20", "group_bg": "#2a313d", "group_fg": "#f3f4f6",
        "card": "#21262e", "good": "#4ade80", "bad": "#f87171", "tip": "#2a2f39", "dark": True},
    "Midnight Blue": {
        "bg": "#1a2232", "panel": "#212b3d", "fg": "#dbe4f0", "muted": "#93a3ba", "border": "#34425c",
        "accent": "#5b9cf6", "accent_fg": "#0d1726", "accent_hover": "#7ab0f8", "header": "#121a28",
        "header_fg": "#e6eefb", "header_btn": "#24314a", "header_btn_hover": "#2e3e5d", "entry": "#28344b",
        "select": "#34507d", "select_fg": "#ffffff", "tree": "#1e2738", "heading": "#29354d",
        "buy_bg": "#1c3a35", "buy_fg": "#b5f0d3", "sell_bg": "#3b2433", "sell_fg": "#f5b3c4",
        "hot_bg": "#21523f", "hot_fg": "#dcfce7", "hotsell_bg": "#5a2638", "hotsell_fg": "#ffe4ec",
        "flip_bg": "#4a3a22", "flip_fg": "#fde2b8", "routine_bg": "#253043", "routine_fg": "#8797ae",
        "grant_bg": "#22304a", "exercise_bg": "#373424", "group_bg": "#2d3a55", "group_fg": "#f1f5fb",
        "card": "#24304a", "good": "#6ee7a8", "bad": "#fb8a9a", "tip": "#2a3650", "dark": True},
    "Sepia (warm)": {
        "bg": "#f2eadb", "panel": "#fbf6ec", "fg": "#3b2f24", "muted": "#7a6a58", "border": "#d9cbb3",
        "accent": "#9a5b2e", "accent_fg": "#fffaf1", "accent_hover": "#82491f", "header": "#5a3e2b",
        "header_fg": "#fdf6ea", "header_btn": "#6d4c36", "header_btn_hover": "#7d5a41", "entry": "#fffaf1",
        "select": "#e8d5b5", "select_fg": "#2b2018", "tree": "#fbf6ec", "heading": "#eee2cd",
        "buy_bg": "#e3efd9", "buy_fg": "#2f5320", "sell_bg": "#f3dcd5", "sell_fg": "#7a2a1b",
        "hot_bg": "#cfe5bf", "hot_fg": "#22401a", "hotsell_bg": "#ebc3b6", "hotsell_fg": "#5e1d10",
        "flip_bg": "#f2dcc0", "flip_fg": "#7a4a14", "routine_bg": "#ece4d6", "routine_fg": "#857865",
        "grant_bg": "#ebe6dc", "exercise_bg": "#efe5cf", "group_bg": "#e6d9c2", "group_fg": "#2f241a",
        "card": "#fffaf1", "good": "#4d7c2f", "bad": "#a3361f", "tip": "#fffaf1", "dark": False},
}
VIEWS = [("all", "All trades"), ("top_buys", "Top buys"), ("top_sells", "Top sells"),
         ("clusters", "Cluster buys"), ("company", "By company"), ("insider", "By insider"),
         ("largest", "Largest $")]


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
        ("ins_rec", "Insider Record", 132, "w", True),
        ("earn", "Since Earnings", 112, "w", True),
        ("bm", "Book/Mkt", 72, "e", True),
        ("dvol", "Avg $ Vol", 84, "e", True),
        ("flags", "Flags", 110, "w", False),
        ("pattern", "Pattern", 215, "w", False),
        ("sold", "Sold Since", 145, "w", False),
        ("link", "SEC Link", 260, "w", False),
    ]
    HEADINGS = {c[0]: c[1] for c in COLUMNS}
    COL_KEYS = [c[0] for c in COLUMNS]
    TOOLTIPS = {
        "stake": "How much this trade grew (or shrank) the insider's holding. 'New' = first shares.",
        "cluster": "Number of different insiders making the same kind of trade in this company during the scanned dates. "
                   "With 'Exclude pre-planned 10b5-1 trades' on, plan and offering purchases don't count.",
        "flags": "10b5-1 = pre-scheduled trading plan  •  Offering = bought in a company offering / private "
                 "placement  •  Fund = buyer is a fund or company  •  13D = a 5%+ holder filed Schedule 13D in "
                 "the last 90 days  •  Dup = same purchase already reported by a related owner  •  Late = Form 4 "
                 "filed N business days after the deadline  •  LowCash = burning cash with under a year left  •  "
                 "NegEq = negative shareholders' equity.",
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
        "ins_rec": "How this insider's own earlier buys of this stock did over the next 6 months vs SPY "
                   "(average, number of past buys measured).",
        "earn": "Days from the company's last earnings release (8-K Item 2.02) to the purchase, and the stock's "
                "move on that report.",
        "bm": "Book value (shareholders' equity, SEC XBRL) divided by market value. Above 1 = trading below book.",
        "dvol": "Average daily dollar volume over the 20 sessions before the purchase.",
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
            set_weights(self.settings.get("score_weights"))
            self.q: queue.Queue = queue.Queue()
            self.cancel_event: threading.Event | None = None
            self.worker: threading.Thread | None = None
            self.scan_id = 0
            self.rows: dict[str, dict] = {}       # iid -> row
            self.order: list[str] = []            # all iids in current sort order
            self.clusters: dict[str, tuple] = {}  # cluster_key -> (all, discretionary)
            self.by_cluster: dict[str, list] = {} # cluster_key -> [iid]
            self.by_acc: dict[str, list] = {}     # accession -> [iid] (buy rows, for history updates)
            self.by_acc_all: dict[str, list] = {} # accession -> [iid] (all rows)
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
            self._groups: list[str] = []
            self._user_sorted = False
            self._init_style()
            self._menu()
            self._header()
            self._controls()
            self._cards()
            self._viewbar()
            self._table()
            self._footer()
            self.apply_theme(self.theme_name)
            self._on_range_change()
            root.protocol("WM_DELETE_WINDOW", self.on_close)
            root.after(100, self._poll)

        # ---- look & feel ------------------------------------------------
        def _init_style(self):
            s = ttk.Style(self.root)
            s.theme_use("clam")                       # fully recolourable on every platform
            self.style = s
            base = ("Segoe UI", 10) if sys.platform == "win32" else ("DejaVu Sans", 10)
            self.base_font = base
            for name in ("TkDefaultFont", "TkTextFont", "TkMenuFont", "TkHeadingFont"):
                try:
                    tkfont.nametofont(name).configure(family=base[0], size=base[1])
                except tk.TclError:
                    pass
            linespace = tkfont.Font(family=base[0], size=base[1]).metrics("linespace")
            self.row_height = max(24, int(linespace * 1.6))
            self.bold_font = tkfont.Font(family=base[0], size=base[1], weight="bold")
            self.title_font = tkfont.Font(family=base[0], size=17, weight="bold")
            self.card_value_font = tkfont.Font(family=base[0], size=16, weight="bold")
            self.card_label_font = tkfont.Font(family=base[0], size=9)
            self._theme_listeners: list = []
            self._menus: list = []
            name = self.settings.get("theme", "Light")
            self.theme_name = name if name in THEMES else "Light"
            self.T = THEMES[self.theme_name]

        def apply_theme(self, name: str):
            if name not in THEMES:
                name = "Light"
            self.theme_name, T, s, base = name, THEMES[name], self.style, self.base_font
            self.T = T
            if hasattr(self, "theme_var"):
                self.theme_var.set(name)
            s.configure(".", background=T["bg"], foreground=T["fg"], fieldbackground=T["entry"],
                        bordercolor=T["border"], lightcolor=T["bg"], darkcolor=T["bg"], troughcolor=T["border"],
                        selectbackground=T["select"], selectforeground=T["select_fg"], insertcolor=T["fg"],
                        focuscolor=T["accent"], font=base)
            s.map(".", foreground=[("disabled", T["muted"])])
            s.configure("TFrame", background=T["bg"])
            s.configure("TLabel", background=T["bg"], foreground=T["fg"])
            s.configure("Title.TLabel", background=T["bg"], foreground=T["fg"], font=(base[0], 15, "bold"))
            s.configure("Sub.TLabel", background=T["bg"], foreground=T["muted"])
            s.configure("Hint.TLabel", background=T["bg"], foreground=T["muted"], font=(base[0], 9))
            s.configure("TLabelframe", background=T["bg"], bordercolor=T["border"], lightcolor=T["bg"], darkcolor=T["bg"])
            s.configure("TLabelframe.Label", background=T["bg"], foreground=T["accent"], font=(base[0], 10, "bold"))
            s.configure("TButton", background=T["panel"], foreground=T["fg"], bordercolor=T["border"],
                        lightcolor=T["panel"], darkcolor=T["panel"], padding=(10, 4))
            s.map("TButton", background=[("disabled", T["bg"]), ("pressed", T["select"]), ("active", T["heading"])],
                  foreground=[("disabled", T["muted"])])
            s.configure("Big.TButton", font=(base[0], 10, "bold"), padding=(14, 6))
            s.configure("Accent.TButton", background=T["accent"], foreground=T["accent_fg"], bordercolor=T["accent"],
                        lightcolor=T["accent"], darkcolor=T["accent"], font=(base[0], 10, "bold"), padding=(16, 6))
            s.map("Accent.TButton", background=[("disabled", T["border"]), ("pressed", T["accent_hover"]),
                                                ("active", T["accent_hover"])],
                  foreground=[("disabled", T["muted"])])
            for st_ in ("Header.TButton", "Header.TMenubutton"):
                s.configure(st_, background=T["header_btn"], foreground=T["header_fg"], bordercolor=T["header_btn"],
                            lightcolor=T["header_btn"], darkcolor=T["header_btn"], arrowcolor=T["header_fg"],
                            padding=(12, 5), font=(base[0], 10, "bold"))
                s.map(st_, background=[("pressed", T["header_btn_hover"]), ("active", T["header_btn_hover"])])
            s.configure("HeaderAccent.TButton", background=T["accent"], foreground=T["accent_fg"], bordercolor=T["accent"],
                        lightcolor=T["accent"], darkcolor=T["accent"], padding=(12, 5), font=(base[0], 10, "bold"))
            s.map("HeaderAccent.TButton", background=[("pressed", T["accent_hover"]), ("active", T["accent_hover"])])
            for st_ in ("TCheckbutton", "TRadiobutton"):
                s.configure(st_, background=T["bg"], foreground=T["fg"], indicatorbackground=T["entry"],
                            indicatorforeground=T["accent"], upperbordercolor=T["border"], lowerbordercolor=T["border"])
                s.map(st_, background=[("active", T["bg"])],
                      indicatorbackground=[("pressed", T["select"]), ("selected", T["entry"])])
            s.configure("Seg.Toolbutton", background=T["panel"], foreground=T["fg"], bordercolor=T["border"],
                        lightcolor=T["panel"], darkcolor=T["panel"], padding=(12, 4), anchor="center")
            s.map("Seg.Toolbutton", background=[("selected", T["accent"]), ("active", T["heading"])],
                  foreground=[("selected", T["accent_fg"])])
            for st_ in ("TEntry", "TCombobox", "TSpinbox"):
                s.configure(st_, fieldbackground=T["entry"], foreground=T["fg"], background=T["panel"],
                            bordercolor=T["border"], lightcolor=T["entry"], darkcolor=T["entry"], arrowcolor=T["fg"],
                            insertcolor=T["fg"])
                s.map(st_, fieldbackground=[("readonly", T["entry"]), ("disabled", T["bg"])],
                      foreground=[("disabled", T["muted"])], bordercolor=[("focus", T["accent"])])
            s.configure("TProgressbar", background=T["accent"], troughcolor=T["border"], bordercolor=T["border"],
                        lightcolor=T["accent"], darkcolor=T["accent"])
            s.configure("Treeview", background=T["tree"], fieldbackground=T["tree"], foreground=T["fg"],
                        bordercolor=T["border"], lightcolor=T["tree"], darkcolor=T["tree"], font=base,
                        rowheight=self.row_height)
            s.map("Treeview", background=[("selected", T["select"])], foreground=[("selected", T["select_fg"])])
            s.configure("Treeview.Heading", background=T["heading"], foreground=T["fg"], bordercolor=T["border"],
                        lightcolor=T["heading"], darkcolor=T["heading"], relief="flat", font=(base[0], base[1], "bold"))
            s.map("Treeview.Heading", background=[("active", T["select"])])
            s.configure("TNotebook", background=T["bg"], bordercolor=T["border"], lightcolor=T["bg"], darkcolor=T["bg"])
            s.configure("TNotebook.Tab", background=T["panel"], foreground=T["fg"], padding=(12, 4),
                        bordercolor=T["border"], lightcolor=T["panel"], darkcolor=T["panel"])
            s.map("TNotebook.Tab", background=[("selected", T["accent"])], foreground=[("selected", T["accent_fg"])])
            s.configure("TScrollbar", background=T["panel"], troughcolor=T["bg"], bordercolor=T["border"],
                        arrowcolor=T["fg"], lightcolor=T["panel"], darkcolor=T["panel"])
            s.map("TScrollbar", background=[("active", T["heading"])])
            self.root.configure(bg=T["bg"])
            for opt, key in (("background", "entry"), ("foreground", "fg"), ("selectBackground", "select"),
                             ("selectForeground", "select_fg")):
                self.root.option_add(f"*TCombobox*Listbox.{opt}", T[key])
            for m in self._menus:
                try:
                    m.configure(bg=T["panel"], fg=T["fg"], activebackground=T["select"],
                                activeforeground=T["select_fg"], selectcolor=T["accent"], relief="flat")
                except tk.TclError:
                    pass
            self._theme_tree_tags(self.tree)
            self.header.configure(bg=T["header"])
            for w in self._header_labels:
                if isinstance(w, tk.Label) and w.cget("text"):
                    w.configure(bg=T["header"], fg=T["header_fg"])
                else:
                    w.configure(bg=T["header"])
            self.sub_lbl.configure(fg="#c9d6e8" if not T["dark"] else "#9aa3af")
            for card in self._cards_w.values():
                card["frame"].configure(bg=T["card"], highlightbackground=T["border"])
                card["value"].configure(bg=T["card"], fg=T[card["color"]] if card["color"] else T["fg"])
                card["label"].configure(bg=T["card"], fg=T["muted"])
                card["detail"].configure(bg=T["card"], fg=T["muted"])
            self.dark_titlebar(self.root, T["dark"])
            for fn in list(self._theme_listeners):
                try:
                    fn(T)
                except tk.TclError:
                    self._theme_listeners.remove(fn)
            if hasattr(self, "rows"):
                self._update_summary()

        def _theme_tree_tags(self, tree):
            T = self.T
            tree.tag_configure("Buy", background=T["buy_bg"], foreground=T["buy_fg"])
            tree.tag_configure("Sell", background=T["sell_bg"], foreground=T["sell_fg"])
            tree.tag_configure("Grant", background=T["grant_bg"], foreground=T["fg"])
            tree.tag_configure("Exercise", background=T["exercise_bg"], foreground=T["fg"])
            tree.tag_configure("hot", background=T["hot_bg"], foreground=T["hot_fg"], font=self.bold_font)
            tree.tag_configure("hotsell", background=T["hotsell_bg"], foreground=T["hotsell_fg"], font=self.bold_font)
            tree.tag_configure("flip", background=T["flip_bg"], foreground=T["flip_fg"])
            tree.tag_configure("routine", background=T["routine_bg"], foreground=T["routine_fg"])
            tree.tag_configure("group", background=T["group_bg"], foreground=T["group_fg"], font=self.bold_font)
            tree.tag_configure("good", foreground=T["good"])
            tree.tag_configure("bad", foreground=T["bad"])
            tree.tag_configure("dim", foreground=T["muted"])
            tree.tag_configure("up", foreground=T["good"])
            tree.tag_configure("down", foreground=T["bad"])

        @staticmethod
        def dark_titlebar(win, dark: bool):
            """Dark Windows title bar for dark themes (Windows 10 1809+ / 11)."""
            if sys.platform != "win32":
                return
            try:
                import ctypes
                win.update_idletasks()
                hwnd = ctypes.windll.user32.GetParent(win.winfo_id())
                val = ctypes.c_int(1 if dark else 0)
                for attr in (20, 19):
                    if ctypes.windll.dwmapi.DwmSetWindowAttribute(hwnd, attr, ctypes.byref(val),
                                                                  ctypes.sizeof(val)) == 0:
                        break
            except Exception:
                pass

        def theme_window(self, win, trees=()):
            """Apply the current theme to another window now and whenever the theme changes."""
            def apply(T):
                win.configure(bg=T["bg"])
                self.dark_titlebar(win, T["dark"])
                for t in trees:
                    self._theme_tree_tags(t)
            apply(self.T)
            self._theme_listeners.append(apply)

        def _menu(self):
            m = tk.Menu(self.root)
            fm = tk.Menu(m, tearoff=0)
            self._menus += [m, fm]
            fm.add_command(label="Export Results to CSV...", command=self.export_csv, accelerator="Ctrl+E")
            fm.add_separator()
            fm.add_command(label="Signal Track Record...", command=self.show_track_record, accelerator="Ctrl+T")
            fm.add_command(label="Back-test Signal Score...", command=self.show_backtest, accelerator="Ctrl+B")
            fm.add_separator()
            fm.add_command(label="Open Data Folder", command=self.open_data_folder)
            fm.add_command(label="Clear Download Cache", command=self.clear_cache)
            fm.add_separator()
            fm.add_command(label="Exit", command=self.on_close)
            m.add_cascade(label="File", menu=fm)
            pm = tk.Menu(m, tearoff=0)
            pm.add_command(label="★  True Insider Buying", command=self.apply_preset)
            pm.add_command(label="▼  Significant Insider Selling", command=self.apply_sell_preset)
            m.add_cascade(label="Presets", menu=pm)
            vm = tk.Menu(m, tearoff=0)
            self.view_var = tk.StringVar(value=self.settings.get("view", "all"))
            for key, label in VIEWS:
                vm.add_radiobutton(label=label, value=key, variable=self.view_var, command=self._view_mode_changed)
            vm.add_separator()
            self.theme_var = tk.StringVar(value=self.theme_name)
            tm = tk.Menu(vm, tearoff=0)
            for name in THEMES:
                tm.add_radiobutton(label=name, value=name, variable=self.theme_var,
                                   command=lambda n=name: self.set_theme(n))
            vm.add_cascade(label="Theme", menu=tm)
            vm.add_command(label="Show / hide scan settings", command=self.toggle_settings, accelerator="Ctrl+H")
            m.add_cascade(label="View", menu=vm)
            hm = tk.Menu(m, tearoff=0)
            self._menus += [pm, vm, tm, hm]
            hm.add_command(label="How it works", command=self.show_help)
            hm.add_command(label="Finding true insider buying", command=self.show_signal_help)
            hm.add_command(label="SEC EDGAR Form 4 search", command=lambda: webbrowser.open(
                "https://www.sec.gov/edgar/search/#/forms=4"))
            m.add_cascade(label="Help", menu=hm)
            self.root.config(menu=m)
            self.root.bind_all("<Control-e>", lambda e: self.export_csv())
            self.root.bind_all("<Control-t>", lambda e: self.show_track_record())
            self.root.bind_all("<Control-b>", lambda e: self.show_backtest())
            self.root.bind_all("<Control-h>", lambda e: self.toggle_settings())

        def set_theme(self, name: str):
            self.apply_theme(name)
            self._save()

        # ---- header bar ----------------------------------------------------
        def _header(self):
            T = self.T
            self.header = tk.Frame(self.root, bg=T["header"], padx=16, pady=10)
            self.header.pack(fill="x")
            self._header_labels = []
            try:
                img = tk.PhotoImage(file=resource_path(os.path.join("assets", "app.png")))
                self._hdr_icon = img.subsample(max(1, img.width() // 40))
                lbl = tk.Label(self.header, image=self._hdr_icon, bg=T["header"], bd=0)
                lbl.pack(side="left", padx=(0, 10))
                self._header_labels.append(lbl)
            except Exception:
                pass
            box = tk.Frame(self.header, bg=T["header"])
            box.pack(side="left")
            self._header_labels.append(box)
            t = tk.Label(box, text="Form 4 Insider Scanner", font=self.title_font, bg=T["header"], fg=T["header_fg"])
            t.pack(anchor="w")
            self.sub_lbl = tk.Label(box, text="Insider buys & sells from SEC EDGAR, scored and organized",
                                    font=(self.base_font[0], 9), bg=T["header"], fg="#c9d6e8")
            self.sub_lbl.pack(anchor="w")
            self._header_labels += [t, self.sub_lbl]
            right = tk.Frame(self.header, bg=T["header"])
            right.pack(side="right")
            self._header_labels.append(right)
            tm = tk.Menu(self.root, tearoff=0)
            self._menus.append(tm)
            for name in THEMES:
                tm.add_radiobutton(label=name, value=name, variable=self.theme_var, command=lambda n=name: self.set_theme(n))
            ttk.Menubutton(right, text="◐  Theme", style="Header.TMenubutton", menu=tm).pack(side="right", padx=(6, 0))
            ttk.Button(right, text="Export CSV", style="Header.TButton", command=self.export_csv).pack(side="right", padx=(6, 0))
            ttk.Button(right, text="Back-test", style="Header.TButton", command=self.show_backtest).pack(side="right", padx=(6, 0))
            ttk.Button(right, text="Track Record", style="Header.TButton", command=self.show_track_record).pack(side="right", padx=(6, 0))
            ttk.Button(right, text="▼  Insider Selling", style="Header.TButton", command=self.apply_sell_preset).pack(side="right", padx=(6, 0))
            ttk.Button(right, text="★  Insider Buying", style="HeaderAccent.TButton", command=self.apply_preset).pack(side="right", padx=(6, 0))

        # ---- settings panel --------------------------------------------
        def _controls(self):
            st = self.settings
            outer = ttk.Frame(self.root, padding=(14, 6, 14, 2))
            outer.pack(fill="x")
            self.settings_frame = ttk.Frame(outer)
            self.settings_frame.pack(fill="x")
            grid = ttk.Frame(self.settings_frame)
            grid.pack(fill="x", pady=(2, 0))
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
            ttk.Label(f, text="% (buys)").pack(side="left", padx=(0, 22))
            ttk.Label(f, text="Min % of holding sold").pack(side="left")
            self.min_sold_var = tk.StringVar(value=str(st.get("min_sold", "0")))
            e_s = ttk.Entry(f, textvariable=self.min_sold_var, width=5, validate="key", validatecommand=vcmd2)
            e_s.pack(side="left", padx=4)
            e_s.bind("<KeyRelease>", lambda ev: self._view_changed())
            ttk.Label(f, text="% (sales)").pack(side="left", padx=(0, 22))
            self.excl_explained_var = tk.BooleanVar(value=st.get("exclude_explained", False))
            ttk.Checkbutton(f, text="Exclude explained / exercise-and-sell sales", variable=self.excl_explained_var,
                            command=self._view_changed).pack(side="left")
            self.cluster_min_var.trace_add("write", lambda *a: self._view_changed())

            f2 = ttk.Frame(box)
            f2.pack(fill="x", pady=(4, 0))
            self.history_var = tk.BooleanVar(value=st.get("check_history", True))
            ttk.Checkbutton(f2, text="Deep check (history, later sales, company size, prices)",
                            variable=self.history_var).pack(side="left", padx=(0, 22))
            self.hide_routine_var = tk.BooleanVar(value=st.get("hide_routine", False))
            ttk.Checkbutton(f2, text="Hide routine / regular traders", variable=self.hide_routine_var,
                            command=self._view_changed).pack(side="left", padx=(0, 22))
            self.hide_sold_var = tk.BooleanVar(value=st.get("hide_sold", False))
            ttk.Checkbutton(f2, text="Hide buyers who have sold since", variable=self.hide_sold_var,
                            command=self._view_changed).pack(side="left", padx=(0, 22))


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
            ttk.Label(f3, text="(blank = any)", style="Hint.TLabel").pack(side="left", padx=(0, 22))
            vdec = (self.root.register(lambda s: re.fullmatch(r"\d{0,6}(\.\d{0,2})?", s) is not None), "%P")
            ttk.Label(f3, text="Min price $").pack(side="left")
            self.min_price_var = tk.StringVar(value=str(st.get("min_price", "")))
            e5 = ttk.Entry(f3, textvariable=self.min_price_var, width=6, validate="key", validatecommand=vdec)
            e5.pack(side="left", padx=(4, 22))
            e5.bind("<KeyRelease>", lambda ev: self._view_changed())
            ttk.Label(f3, text="Min $ volume/day ($K)").pack(side="left")
            self.min_dvol_var = tk.StringVar(value=str(st.get("min_dvol", "")))
            e6 = ttk.Entry(f3, textvariable=self.min_dvol_var, width=7, validate="key", validatecommand=vnum)
            e6.pack(side="left", padx=4)
            e6.bind("<KeyRelease>", lambda ev: self._view_changed())
            self.hide_dups_var = tk.BooleanVar(value=st.get("hide_dups", True))
            ttk.Checkbutton(f2, text="Hide duplicate filings (related owners)", variable=self.hide_dups_var,
                            command=self._view_changed).pack(side="left", padx=(22, 0))

            # SEC contact ---------------------------------------------------
            f = ttk.Frame(self.settings_frame)
            f.pack(fill="x", pady=(6, 2))
            ttk.Label(f, text="SEC contact (User-Agent):").pack(side="left")
            self.ua_var = tk.StringVar(value=st.get("user_agent", ""))
            ttk.Entry(f, textvariable=self.ua_var, width=42).pack(side="left", padx=6)
            ttk.Label(f, text="SEC requires your name + email on automated requests, e.g. \"Jane Doe jane@example.com\"",
                      style="Hint.TLabel").pack(side="left")

            # Action row -----------------------------------------------------
            f = ttk.Frame(outer)
            f.pack(fill="x", pady=(8, 2))
            self.action_row = f
            self.start_btn = ttk.Button(f, text="▶  Start Scan", style="Accent.TButton", command=self.start_scan)
            self.start_btn.pack(side="left")
            self.cancel_btn = ttk.Button(f, text="■  Cancel", style="Big.TButton", command=self.cancel_scan, state="disabled")
            self.cancel_btn.pack(side="left", padx=(8, 0))
            ttk.Button(f, text="Clear", command=self.clear_results).pack(side="left", padx=(8, 0))
            self.settings_btn = ttk.Button(f, text="⚙  Hide settings", command=self.toggle_settings)
            self.settings_btn.pack(side="left", padx=(8, 0))
            self.progress = ttk.Progressbar(f, mode="determinate", length=300)
            self.progress.pack(side="left", padx=(20, 10))
            self.status_var = tk.StringVar(value="Ready.")
            ttk.Label(f, textvariable=self.status_var).pack(side="left", fill="x", expand=True)
            if self.settings.get("settings_hidden"):
                self.root.after(10, self.toggle_settings)

        def toggle_settings(self):
            if self.settings_frame.winfo_ismapped():
                self.settings_frame.pack_forget()
                self.settings_btn.config(text="⚙  Show settings")
            else:
                self.settings_frame.pack(fill="x", before=self.action_row)
                self.settings_btn.config(text="⚙  Hide settings")

        # ---- summary cards + view bar ------------------------------------------
        def _cards(self):
            T = self.T
            wrap = ttk.Frame(self.root, padding=(14, 6, 14, 2))
            wrap.pack(fill="x")
            specs = [("shown", "Trades shown", None), ("buys", "Open-market buys", "good"),
                     ("sells", "Open-market sales", "bad"), ("clusters", "Cluster-buy companies", None),
                     ("best", "Best buy score", "good"), ("warn", "Strongest sell warning", "bad")]
            self._cards_w = {}
            for i, (key, label, color) in enumerate(specs):
                fr = tk.Frame(wrap, bg=T["card"], highlightthickness=1, highlightbackground=T["border"],
                              padx=14, pady=7)
                fr.grid(row=0, column=i, sticky="nsew", padx=(0 if i == 0 else 8, 0))
                wrap.columnconfigure(i, weight=1, uniform="cards")
                lb = tk.Label(fr, text=label.upper(), font=self.card_label_font, bg=T["card"], fg=T["muted"])
                lb.pack(anchor="w")
                val = tk.Label(fr, text="—", font=self.card_value_font, bg=T["card"], fg=T["fg"])
                val.pack(anchor="w")
                det = tk.Label(fr, text=" ", font=self.card_label_font, bg=T["card"], fg=T["muted"])
                det.pack(anchor="w")
                self._cards_w[key] = {"frame": fr, "value": val, "label": lb, "detail": det, "color": color}

        def _viewbar(self):
            f = ttk.Frame(self.root, padding=(14, 6, 14, 2))
            f.pack(fill="x")
            ttk.Label(f, text="View", style="Sub.TLabel").pack(side="left", padx=(0, 8))
            for key, label in VIEWS:
                ttk.Radiobutton(f, text=label, value=key, variable=self.view_var, style="Seg.Toolbutton",
                                command=self._view_mode_changed).pack(side="left", padx=(0, 2))
            ttk.Button(f, text="Collapse all", command=lambda: self._expand_all(False)).pack(side="right")
            ttk.Button(f, text="Expand all", command=lambda: self._expand_all(True)).pack(side="right", padx=(0, 4))
            self.search_var = tk.StringVar(value="")
            e = ttk.Entry(f, textvariable=self.search_var, width=22)
            e.pack(side="right", padx=(0, 14))
            ttk.Label(f, text="Search", style="Sub.TLabel").pack(side="right", padx=(0, 6))
            self.search_var.trace_add("write", lambda *a: self._schedule_view())

        def _view_mode_changed(self):
            self._user_sorted = False
            self._apply_view()

        def _expand_all(self, open_: bool):
            for g in self._groups:
                self.tree.item(g, open=open_)

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

        def apply_sell_preset(self):
            """Settings that isolate the kind of insider selling research links to weak returns."""
            self.range_var.set("Last 30 Days")
            self._on_range_change()
            for v in self.role_vars.values():
                v.set(False)
            for c, v in self.code_vars.items():
                v.set(c == "S")
            self.min_var.set("250000")
            self.combine_var.set(True)
            self.amend_var.set(False)
            self.excl_plan_var.set(True)
            self.cluster_only_var.set(False)
            self.min_stake_var.set("0")
            self.min_sold_var.set("25")
            self.excl_explained_var.set(True)
            self.history_var.set(True)
            self.hide_routine_var.set(True)
            self.hide_sold_var.set(False)
            self.excl_offer_var.set(True)
            self.excl_entity_var.set(True)
            self.hide_dups_var.set(True)
            self.min_score_var.set("0")
            self.min_price_var.set("1")
            self.min_dvol_var.set("100")
            self.view_var.set("top_sells")
            self._user_sorted = False
            self._view_changed()
            self.status_var.set("Selling preset: open-market sales ≥ $250k, 30 days, 25%+ of the holding, no 10b5-1 "
                                "plans, no explained or exercise-and-sell sales, no routine sellers. Click Start Scan.")

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
            self.hide_dups_var.set(True)
            self.min_price_var.set("1")
            self.min_dvol_var.set("100")
            self.min_sold_var.set("0")
            self.excl_explained_var.set(False)
            self.view_var.set("top_buys")
            self._user_sorted = False
            self._view_changed()
            self.status_var.set("Preset applied: open-market buys ≥ $100k, 30 days, ≥ 2 insiders, no 10b5-1 plans, "
                                "stake +10%, no routine buyers or later sellers, price ≥ $1, ≥ $100K/day traded. "
                                "Click Start Scan.")

        # ---- table ------------------------------------------------------
        def _table(self):
            wrap = ttk.Frame(self.root, padding=(14, 2, 14, 0))
            wrap.pack(fill="both", expand=True)
            cols = [c[0] for c in COLUMNS]
            self.tree = ttk.Treeview(wrap, columns=cols, show="headings", selectmode="extended")
            self.tree.column("#0", width=30, minwidth=30, stretch=False)
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
            self.tree.bind("<Double-1>", self._on_double)
            self.tree.bind("<Return>", lambda e: self.open_selected())
            self.tree.bind("<Button-3>", self._on_right_click)
            self.hidden_cols = set(self.settings.get("hidden_cols", ["chg3m", "dvol"])) & {c[0] for c in COLUMNS}
            self._apply_columns()
            self.tree.bind("<Control-c>", lambda e: self.copy_selected())
            self.tree.bind("<Motion>", self._on_motion)
            self.tree.bind("<Leave>", lambda e: self._hide_tip())
            self._tip = None

            self.ctx = tk.Menu(self.root, tearoff=0)
            self._menus.append(self.ctx)
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
            ttk.Label(f, text="Bold green = cluster buy  •  Bold red = strong sell warning  •  Orange = sold since / "
                             "offering  •  Grey = routine  •  Double-click = open filing  •  Right-click = more",
                      style="Hint.TLabel").pack(side="right")

        # header tooltips ---------------------------------------------------
        def _on_motion(self, event):
            if self.tree.identify_region(event.x, event.y) != "heading":
                self._hide_tip()
                return
            col = self.tree.identify_column(event.x)
            shown = list(self.tree["displaycolumns"])
            if not shown or shown == ["#all"]:
                shown = list(self.tree["columns"])
            try:
                idx = int(col[1:]) - 1
                if idx < 0:
                    self._hide_tip()
                    return
                key = shown[idx]
            except (ValueError, IndexError):
                return
            text = TOOLTIPS.get(key)
            if not text:
                self._hide_tip()
                return
            if self._tip is None:
                self._tip = tk.Toplevel(self.root)
                self._tip.wm_overrideredirect(True)
                self._tip_label = tk.Label(self._tip, bg=self.T["tip"], fg=self.T["fg"], relief="solid", borderwidth=1,
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
                "min_price": self.min_price_var.get(), "min_dvol": self.min_dvol_var.get(),
                "hide_dups": self.hide_dups_var.get(),
                "min_sold": self.min_sold_var.get(), "exclude_explained": self.excl_explained_var.get(),
                "theme": self.theme_name, "view": self.view_var.get(),
                "settings_hidden": not self.settings_frame.winfo_ismapped(),
                "score_weights": {k: v for k, v in WEIGHTS.items() if v != DEFAULT_WEIGHTS.get(k)},
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
            elif kind == "dups":
                for acc, first in payload.items():
                    for iid in self.by_acc_all.get(acc, []):
                        self.rows[iid]["dup_of"] = first
                        self._refresh_row(iid)
                self._schedule_view()
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
                        label = "Duplicate filing" if r.get("dup_of") else "Not checked"
                        r.update({"pattern": label, "pattern_kind": "unknown",
                                  "sold": label, "sold_kind": "unknown"})
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
            score_txt = "" if score is None else (f"▼ {score}" if r["code"] == "S" else score)
            return (r["filing_date"], r["ticker"], score_txt,
                    r["company"], r["insider"], r["role"], r["title"],
                    r["side"], f"{r['shares']:,.0f}", fmt_money(r["price"], 2), fmt_money(r["value"]),
                    fmt_stake(r.get("stake")), r["cluster"],
                    fmt_pct(r.get("cluster_pct_co"), plus=False, digits=2),
                    fmt_compact(r["mcap"]) if r.get("mcap") else "",
                    fmt_pct(r.get("off_high"), plus=False, digits=0), fmt_pct(r.get("chg3m"), digits=0),
                    fmt_pct(r.get("since"), digits=0), fmt_net90(r.get("net90")),
                    fmt_ins_rec(r.get("ins_rec")), self._earn_text(r),
                    "" if r.get("bm") is None else f"{r['bm']:.2f}",
                    fmt_compact(r["dollar_vol"]) if r.get("dollar_vol") else "", self._flags(r),
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
            if r.get("dup_of"):
                f.append("Dup")
            if (r.get("late_days") or 0) > 0:
                f.append(f"Late {r['late_days']}d")
            if r.get("low_cash"):
                f.append("LowCash")
            if r.get("bm") is not None and r["bm"] < 0:
                f.append("NegEq")
            if r.get("explained"):
                f.append("Explained")
            if r.get("exercise_sell"):
                f.append("Ex&Sell")
            return ", ".join(f)

        @staticmethod
        def _earn_text(r: dict) -> str:
            if r.get("earn_days") is None:
                return ""
            mv = r.get("earn_move")
            return f"{r['earn_days']}d" + ("" if mv is None else f" ({mv:+.0f}%)")

        def _tags(self, r: dict) -> tuple:
            if r["code"] == "P" and (r.get("sold_kind") == "sold" or r.get("offering")):
                return ("flip",)
            if r["code"] == "P" and r.get("pattern_kind") in ("routine", "regular"):
                return ("routine",)
            if r["code"] == "P" and r["cluster"] >= 2 and not r.get("plan"):
                return ("hot",)
            if r["code"] == "S":
                if r.get("pattern_kind") in ("routine", "regular"):
                    return ("routine",)
                if (r.get("score") or 0) >= 70:
                    return ("hotsell",)
            return (r["side"],)

        def _insert(self, r: dict):
            self._compute(r)
            iid = self.tree.insert("", "end", values=self._values(r), tags=self._tags(r))
            self.rows[iid] = r
            self.order.append(iid)
            self.by_cluster.setdefault(r["cluster_key"], []).append(iid)
            if r["code"] in ("P", "S"):
                self.by_acc.setdefault(r.get("hkey", r["acc"]), []).append(iid)
            self.by_acc_all.setdefault(r["acc"], []).append(iid)
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
            if r["code"] in ("P", "S"):
                r["score"], r["score_why"] = rate_row(r)

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
            if min_score and r["code"] in ("P", "S") and (r.get("score") or 0) < min_score:
                return False
            if self.max_mcap_var.get().isdigit() and r.get("mcap") and r["mcap"] > int(self.max_mcap_var.get()) * 1e6:
                return False
            if self.hide_dups_var.get() and r.get("dup_of"):
                return False
            if r["code"] == "S":
                ms = self.min_sold_var.get()
                if ms.isdigit() and int(ms) > 0 and (r.get("stake") is None or r["stake"] > -int(ms)):
                    return False
                if self.excl_explained_var.get() and (r.get("explained") or r.get("exercise_sell")):
                    return False
            q = (self.search_var.get() or "").strip().lower() if hasattr(self, "search_var") else ""
            if q and q not in f"{r['ticker']} {r['company']} {r['insider']} {r['title']}".lower():
                return False
            try:
                mp = float(self.min_price_var.get() or 0)
            except ValueError:
                mp = 0
            if mp and r.get("last_close") is not None and r["last_close"] < mp:
                return False
            if self.min_dvol_var.get().isdigit() and r.get("dollar_vol") is not None \
                    and r["dollar_vol"] < int(self.min_dvol_var.get()) * 1000:
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
            tree, R = self.tree, self.rows
            for g in self._groups:                       # take rows out of old groups, then drop the groups
                kids = tree.get_children(g)
                if kids:
                    tree.detach(*kids)
            if self._groups:
                tree.delete(*self._groups)
            self._groups = []
            top = tree.get_children()
            if top:
                tree.detach(*top)
            mode = self.view_var.get()
            vis = [i for i in self.order if self._visible(R[i])]

            def score(i):
                v = R[i].get("score")
                return -1 if v is None else v
            grouped = mode in ("clusters", "company", "insider")
            tree.configure(show=("tree headings" if grouped else "headings"))
            if mode == "top_buys":
                vis = [i for i in vis if R[i]["code"] == "P"]
                if not self._user_sorted:
                    vis.sort(key=lambda i: (score(i), R[i]["value"]), reverse=True)
            elif mode == "top_sells":
                vis = [i for i in vis if R[i]["code"] == "S"]
                if not self._user_sorted:
                    vis.sort(key=lambda i: (score(i), R[i]["value"]), reverse=True)
            elif mode == "largest" and not self._user_sorted:
                vis.sort(key=lambda i: R[i]["value"], reverse=True)
            if not grouped:
                for idx, i in enumerate(vis):
                    tree.move(i, "", idx)
            else:
                groups: dict = {}
                for i in vis:
                    r = R[i]
                    if mode == "clusters":
                        if r["code"] != "P" or r["cluster"] < 2:
                            continue
                        k = r["cluster_key"]
                    elif mode == "company":
                        k = r.get("issuer_cik") or r["ticker"]
                    else:
                        k = r.get("owner_cik") or r["insider"].lower()
                    groups.setdefault(k, []).append(i)
                summ = sorted(((k, self._group_summary(mode, v)) for k, v in groups.items()),
                              key=lambda x: x[1]["sort"], reverse=True)
                for n, (k, sm) in enumerate(summ):
                    gid = f"g:{n}"
                    tree.insert("", "end", iid=gid, text="", values=sm["values"], tags=("group",),
                                open=(mode == "clusters" or n < 10))
                    self._groups.append(gid)
                    kids = groups[k] if self._user_sorted else sorted(
                        groups[k], key=lambda i: (score(i), R[i]["value"]), reverse=True)
                    for j, i in enumerate(kids):
                        tree.move(i, gid, j)
            self._update_summary()

        def _group_summary(self, mode: str, iids: list) -> dict:
            rows = [self.rows[i] for i in iids]
            buys = [r for r in rows if r["code"] == "P"]
            sells = [r for r in rows if r["code"] == "S"]
            bv, sv = sum(r["value"] for r in buys), sum(r["value"] for r in sells)
            best_b = max((r.get("score") or 0 for r in buys), default=None)
            best_s = max((r.get("score") or 0 for r in sells), default=None)
            first = max(rows, key=lambda r: (r.get("score") or 0, r["value"]))
            insiders = {r.get("owner_cik") or r["insider"] for r in rows}
            tickers = sorted({r["ticker"] for r in rows if r["ticker"]})
            d = {k: "" for k in COL_KEYS}
            d["filing_date"] = max(r["filing_date"] for r in rows)
            d["shares"] = f"{sum(r['shares'] for r in rows):,.0f}"
            if mode == "clusters":
                d.update(ticker=first["ticker"], company=first["company"], score=best_b if best_b is not None else "",
                         insider=(f"{first['cluster']} insiders buying" + (f" ({len(insiders)} shown)"
                                                                            if len(insiders) != first["cluster"] else "")),
                         side="Cluster",
                         value=fmt_money(bv), cluster=first["cluster"],
                         pct_co=fmt_pct(first.get("cluster_pct_co"), plus=False, digits=2),
                         mcap=fmt_compact(first["mcap"]) if first.get("mcap") else "",
                         net90=fmt_net90(first.get("net90")), role=", ".join(sorted({r["role"] for r in buys}))[:60])
                return {"values": tuple(d[k] for k in COL_KEYS), "sort": (best_b or 0, bv)}
            if mode == "company":
                net = bv - sv
                d.update(ticker=first["ticker"], company=first["company"],
                         insider=f"{len(insiders)} insider{'s' if len(insiders) != 1 else ''}, {len(rows)} trade{'s' if len(rows) != 1 else ''}",
                         side="Net buy" if net >= 0 else "Net sell",
                         value=("+" if net >= 0 else "−") + fmt_money(abs(net)),
                         score=best_b if best_b is not None else (f"▼ {best_s}" if best_s is not None else ""),
                         mcap=fmt_compact(first["mcap"]) if first.get("mcap") else "",
                         net90=fmt_net90(first.get("net90")), cluster=max(r["cluster"] for r in rows))
                return {"values": tuple(d[k] for k in COL_KEYS), "sort": (net, best_b or 0)}
            side = "Buyer" if buys and not sells else ("Seller" if sells and not buys else "Bought & sold")
            d.update(insider=first["insider"], role=first["role"], title=first["title"],
                     ticker=", ".join(tickers)[:24], side=side,
                     company=f"{len(rows)} trade{'s' if len(rows) != 1 else ''} in {len(tickers)} compan{'ies' if len(tickers) != 1 else 'y'}",
                     value=fmt_money(bv + sv),
                     score=best_b if best_b is not None else (f"▼ {best_s}" if best_s is not None else ""))
            return {"values": tuple(d[k] for k in COL_KEYS), "sort": (bv + sv,)}

        def _displayed(self) -> list[str]:
            """Row iids in on-screen order (inside groups too)."""
            out = []
            for i in self.tree.get_children(""):
                if i in self.rows:
                    out.append(i)
                else:
                    out.extend(self.tree.get_children(i))
            return out

        def _sel(self) -> list[str]:
            """Selected rows; a selected group counts as all its rows."""
            out = []
            for i in self.tree.selection():
                if i in self.rows:
                    out.append(i)
                else:
                    out.extend(self.tree.get_children(i))
            return list(dict.fromkeys(out))

        def _visible_rows(self) -> list[dict]:
            return [self.rows[i] for i in self._displayed()]

        def _set_card(self, key: str, value: str, detail: str = " "):
            c = self._cards_w.get(key)
            if c:
                c["value"].configure(text=value)
                c["detail"].configure(text=detail)

        def _update_summary(self):
            vis = self._visible_rows()
            total = len(self.rows)
            buys_c = [r for r in vis if r["code"] == "P"]
            sells_c = [r for r in vis if r["code"] == "S"]
            self._set_card("shown", f"{len(vis):,}", f"of {total:,} found" if total else "run a scan")
            self._set_card("buys", f"{len(buys_c):,}", fmt_compact(sum(r["value"] for r in buys_c)) if buys_c else " ")
            self._set_card("sells", f"{len(sells_c):,}", fmt_compact(sum(r["value"] for r in sells_c)) if sells_c else " ")
            cl = {r["cluster_key"] for r in buys_c if r["cluster"] >= 2}
            self._set_card("clusters", f"{len(cl):,}", "2+ insiders buying" if cl else " ")
            bb = max(buys_c, key=lambda r: r.get("score") or -1, default=None)
            self._set_card("best", "—" if not bb or bb.get("score") is None else str(bb["score"]),
                           f"{bb['ticker']} · {bb['insider'][:22]}" if bb and bb.get("score") is not None else " ")
            ws = max(sells_c, key=lambda r: r.get("score") or -1, default=None)
            self._set_card("warn", "—" if not ws or ws.get("score") is None else f"▼ {ws['score']}",
                           f"{ws['ticker']} · {ws['insider'][:22]}" if ws and ws.get("score") is not None else " ")
            if not total:
                self.summary_var.set("No matching trades yet.")
                return
            buys = [r for r in vis if r["code"] == "P"]
            sells = [r for r in vis if r["code"] == "S"]
            clusters = {r["ticker"] for r in buys if r["cluster"] >= 2}
            txt = f"{dict(VIEWS).get(self.view_var.get(), '')}: showing {len(vis):,} of {total:,} trades"
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
            self._groups = []
            for iid in list(self.rows):
                if self.tree.exists(iid):
                    self.tree.delete(iid)
            self.rows.clear()
            self.order.clear()
            self.clusters.clear()
            self.by_cluster.clear()
            self.by_acc.clear()
            self.by_acc_all.clear()
            self.only_ticker = None
            self.sort_key, self.sort_desc = None, False
            for key, head, *_ in COLUMNS:
                self.tree.heading(key, text=head)
            self._update_summary()

        # ---- sorting -------------------------------------------------------
        def sort_by(self, key: str):
            self._user_sorted = True
            numeric = next(c[4] for c in COLUMNS if c[0] == key)
            if self.sort_key == key:
                self.sort_desc = not self.sort_desc
            else:
                self.sort_key, self.sort_desc = key, numeric   # numbers default high-to-low
            if key == "net90":
                def kf(iid):
                    f = self.rows[iid].get("net90")
                    return ((f["buy"] - f["sell"]) if f else -INF, self.rows[iid]["value"])
            elif key in ("ins_rec", "earn", "dvol"):
                def kf(iid):
                    r = self.rows[iid]
                    v = {"ins_rec": (r.get("ins_rec") or {}).get("avg_ex"), "earn": r.get("earn_days"),
                         "dvol": r.get("dollar_vol")}[key]
                    return (-INF if v is None else v, r["value"])
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
            if iid and iid in self.rows:
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
            sel = self._sel()
            if not sel:
                return
            r = self.rows[sel[0]]
            if r.get("score") is None:
                messagebox.showinfo(APP_NAME, "Scores are calculated for open-market purchases (Code P) and "
                                              "sales (Code S) only.")
                return
            if r["code"] == "S":
                lines = [f"{r['ticker']} - {r['insider']} - sold {fmt_money(r['value'])}", "",
                         f"Sell Score (warning strength): {r['score']} / 100", "", f"+{SELL_BASE}  starting point"]
            else:
                lines = [f"{r['ticker']} - {r['insider']} - {fmt_money(r['value'])}", "",
                         f"Signal Score: {r['score']} / 100", "", f"+{SCORE_BASE}  starting point"]
            lines += r.get("score_why") or []
            if not r.get("prices_ok") and r.get("deep"):
                lines += ["", "(No price data - price-based points not included.)"]
            if not r.get("deep"):
                lines += ["", "(Deep check was off - history, company and price points not included.)"]
            messagebox.showinfo(APP_NAME, "\n".join(lines))

        def open_chart(self):
            sel = self._sel()
            if sel:
                sym = yahoo_symbol(self.rows[sel[0]]["ticker"])
                if sym:
                    webbrowser.open(f"https://finance.yahoo.com/quote/{sym}/chart")

        def show_track_record(self):
            TrackRecordWindow(self)

        def show_backtest(self):
            if getattr(self, "_bt_win", None) is not None and self._bt_win.win.winfo_exists():
                self._bt_win.win.lift()
                return
            self._bt_win = BacktestWindow(self)

        def weights_changed(self):
            self._save()
            self._view_changed()

        def filter_to_ticker(self):
            sel = self._sel()
            if sel:
                self.only_ticker = self.rows[sel[0]]["ticker"]
                self._apply_view()

        def clear_ticker_filter(self):
            self.only_ticker = None
            self._apply_view()

        def open_selected(self, index: bool = False):
            for iid in self._sel()[:10]:
                webbrowser.open(self.rows[iid]["index_link" if index else "link"])

        def copy_link(self):
            sel = self._sel()
            if sel:
                self.root.clipboard_clear()
                self.root.clipboard_append("\n".join(self.rows[i]["link"] for i in sel))

        def copy_selected(self):
            sel = self._sel()
            if not sel:
                return
            lines = ["\t".join(CSV_HEADERS)]
            lines += ["\t".join(str(x) for x in row_to_csv(self.rows[i])) for i in sel]
            self.root.clipboard_clear()
            self.root.clipboard_append("\n".join(lines))

        def export_csv(self):
            vis = self._displayed()
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
                "• File > Back-test Signal Score replays years of SEC insider data, measures 1/3/6-month returns "
                "vs SPY by score band and by factor, and can apply weights tuned on the first half of the period "
                "(checked on the second half).\n\n"
                "• Sales get a Sell Score (▼, higher = stronger warning): large share of the holding sold, "
                "discretionary (no plan, no explanation like taxes or estate planning), not exercise-and-sell, "
                "non-routine seller, CEO/CFO, after a run-up or near the 52-week high, late in the quarter. "
                "Use the ▼ Insider Selling preset and the 'Top sells' view.\n\n"
                "• Also scored: the insider's own past record, buys after an earnings drop, book/market, low cash, "
                "late filings and thin trading. Duplicate filings by related owners count once.\n\n"
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

    class BacktestWindow:
        """Replay SEC insider data to see how the Signal Score would have done."""

        def __init__(self, app):
            self.app = app
            self.q: queue.Queue = queue.Queue()
            self.cancel_event: threading.Event | None = None
            self.result: dict | None = None
            st = app.settings
            self.win = tk.Toplevel(app.root)
            self.win.title(f"{APP_NAME} - Back-test Signal Score")
            self.win.geometry("1260x780")
            top = ttk.Frame(self.win, padding=(12, 10, 12, 4))
            top.pack(fill="x")
            ttk.Label(top, text="Back-test the Signal Score", style="Title.TLabel").pack(side="left")
            ttk.Label(top, text="   replays SEC insider data and measures what happened next", style="Sub.TLabel").pack(side="left", pady=(5, 0))

            ctl = ttk.LabelFrame(self.win, text="Settings", padding=(10, 6))
            ctl.pack(fill="x", padx=12, pady=4)
            d_start, d_end = default_backtest_quarters()
            last_q = quarter_of(dt.date.today() - dt.timedelta(days=45))
            qs = [q.upper() for q in quarter_range("2006q3", last_q)][::-1]
            ttk.Label(ctl, text="Signals from").grid(row=0, column=0, sticky="w")
            self.start_var = tk.StringVar(value=st.get("bt_start", d_start.upper()))
            ttk.Combobox(ctl, textvariable=self.start_var, values=qs, width=8, state="readonly").grid(row=0, column=1, padx=4)
            ttk.Label(ctl, text="to").grid(row=0, column=2)
            self.end_var = tk.StringVar(value=st.get("bt_end", d_end.upper()))
            ttk.Combobox(ctl, textvariable=self.end_var, values=qs, width=8, state="readonly").grid(row=0, column=3, padx=(4, 18))
            ttk.Label(ctl, text="History warm-up (quarters)").grid(row=0, column=4)
            self.warm_var = tk.IntVar(value=int(st.get("bt_warmup", 8)))
            ttk.Spinbox(ctl, from_=0, to=20, width=4, textvariable=self.warm_var).grid(row=0, column=5, padx=(4, 18))
            ttk.Label(ctl, text="Min purchase $").grid(row=0, column=6)
            self.min_var = tk.StringVar(value=str(st.get("bt_min", "100000")))
            ttk.Entry(ctl, textvariable=self.min_var, width=10).grid(row=0, column=7, padx=(4, 18))
            ttk.Label(ctl, text="Max signals (sample)").grid(row=0, column=8)
            self.max_var = tk.StringVar(value=str(st.get("bt_max", "2000")))
            ttk.Entry(ctl, textvariable=self.max_var, width=7).grid(row=0, column=9, padx=(4, 18))
            self.company_var = tk.BooleanVar(value=st.get("bt_company", True))
            ttk.Checkbutton(ctl, text="Include company data (size, value, cash, 13D, earnings timing)",
                            variable=self.company_var).grid(row=1, column=0, columnspan=6, sticky="w", pady=(6, 0))
            side_f = ttk.Frame(ctl)
            side_f.grid(row=1, column=6, columnspan=5, sticky="w", pady=(6, 0))
            ttk.Label(side_f, text="Test:").pack(side="left", padx=(0, 6))
            self.side_var = tk.StringVar(value=st.get("bt_side", "P"))
            ttk.Radiobutton(side_f, text="Buy Signal Score", value="P", variable=self.side_var,
                            style="Seg.Toolbutton").pack(side="left")
            ttk.Radiobutton(side_f, text="▼ Sell Score", value="S", variable=self.side_var,
                            style="Seg.Toolbutton").pack(side="left", padx=(2, 0))
            ttk.Label(ctl, style="Hint.TLabel", wraplength=1180, justify="left", text=(
                "Downloads SEC's quarterly insider data sets (~10-40 MB each, kept for next time) and daily prices "
                "for each ticker. A first run with 2,000 signals takes roughly 10-20 minutes; later runs reuse the "
                "downloads. Returns are measured from the filing date (when you could have acted) against SPY.")
                      ).grid(row=2, column=0, columnspan=11, sticky="w", pady=(4, 0))

            act = ttk.Frame(self.win, padding=(12, 4))
            act.pack(fill="x")
            self.run_btn = ttk.Button(act, text="▶  Run back-test", style="Accent.TButton", command=self.run)
            self.run_btn.pack(side="left")
            self.cancel_btn = ttk.Button(act, text="■  Cancel", style="Big.TButton", command=self.cancel, state="disabled")
            self.cancel_btn.pack(side="left", padx=8)
            self.progress = ttk.Progressbar(act, mode="determinate", length=260)
            self.progress.pack(side="left", padx=(12, 10))
            self.status = tk.StringVar(value="Choose a period and click Run.")
            ttk.Label(act, textvariable=self.status).pack(side="left", fill="x", expand=True)

            self.summary = tk.StringVar(value="")
            ttk.Label(self.win, textvariable=self.summary, padding=(12, 2), justify="left").pack(fill="x")

            nb = ttk.Notebook(self.win)
            nb.pack(fill="both", expand=True, padx=12, pady=4)
            self.t_bands = self._table(nb, "By score band", [
                ("band", "Score band", 110, "w"), ("n", "Signals", 70, "e"), ("ret1M", "1M return", 90, "e"),
                ("ret3M", "3M return", 90, "e"), ("ret6M", "6M return", 90, "e"), ("ex1M", "1M vs SPY", 90, "e"),
                ("ex3M", "3M vs SPY", 90, "e"), ("ex6M", "6M vs SPY", 90, "e"), ("hit6", "Beat SPY (6M)", 160, "e")])
            self.t_factors = self._table(nb, "By factor", [
                ("label", "Factor", 340, "w"), ("current", "Points now", 96, "e"), ("n_with", "Signals with", 104, "e"),
                ("with", "6M vs SPY with", 128, "e"), ("without", "without", 90, "e"),
                ("uplift", "Difference", 96, "e"), ("suggested", "Suggested points", 140, "e")])
            self.t_test = self._table(nb, "Train / test check", [
                ("band", "Score band (test half)", 200, "w"), ("n_cur", "Signals (current)", 150, "e"),
                ("ex_cur", "6M vs SPY (current weights)", 250, "e"), ("n_new", "Signals (suggested)", 170, "e"),
                ("ex_new", "6M vs SPY (suggested weights)", 260, "e")])
            self.t_rows = self._table(nb, "Signals", [
                ("filing_date", "Filed", 90, "w"), ("ticker", "Ticker", 64, "w"), ("insider", "Insider", 180, "w"),
                ("value", "Value", 90, "e"), ("score", "Score", 56, "center"), ("ret1M", "1M", 70, "e"),
                ("ret3M", "3M", 70, "e"), ("ret6M", "6M", 70, "e"), ("ex6M", "6M vs SPY", 84, "e"),
                ("factors", "Factors", 420, "w")])

            bot = ttk.Frame(self.win, padding=(12, 4, 12, 10))
            bot.pack(fill="x")
            app.theme_window(self.win, [self.t_bands, self.t_factors, self.t_test, self.t_rows])
            self.apply_btn = ttk.Button(bot, text="Apply suggested weights", style="Accent.TButton",
                                        command=self.apply, state="disabled")
            self.apply_btn.pack(side="left")
            ttk.Button(bot, text="Reset to default weights", command=self.reset).pack(side="left", padx=8)
            ttk.Button(bot, text="Export signals CSV...", command=self.export).pack(side="left")
            self.weights_lbl = tk.StringVar()
            ttk.Label(bot, textvariable=self.weights_lbl, style="Hint.TLabel").pack(side="left", padx=12)
            ttk.Label(bot, style="Hint.TLabel", wraplength=560, justify="left", text=(
                "Caveats: Yahoo has no prices for delisted companies, so failures drop out (survivorship bias). "
                "Factor differences are simple one-at-a-time comparisons. Past results don't guarantee future ones."
            )).pack(side="right")
            self._show_weights()

        def _table(self, nb, title, cols):
            frame = ttk.Frame(nb)
            nb.add(frame, text=title)
            tree = ttk.Treeview(frame, columns=[c[0] for c in cols], show="headings")
            for key, head, w, anchor in cols:
                tree.heading(key, text=head)
                tree.column(key, width=w, anchor=anchor, stretch=key in ("label", "factors", "insider"))
            vs = ttk.Scrollbar(frame, orient="vertical", command=tree.yview)
            tree.configure(yscrollcommand=vs.set)
            tree.pack(side="left", fill="both", expand=True)
            vs.pack(side="right", fill="y")
            tree.tag_configure("good", foreground="#14532d")
            tree.tag_configure("bad", foreground="#7f1d1d")
            tree.tag_configure("dim", foreground="#7a828c")
            return tree

        def _show_weights(self):
            changed = sum(1 for k, v in WEIGHTS.items() if v != DEFAULT_WEIGHTS[k])
            self.weights_lbl.set(f"Weights in use: {'default' if not changed else f'{changed} adjusted by back-test'}")

        def run(self):
            ua = self.app.ua_var.get().strip()
            if not valid_user_agent(ua):
                messagebox.showwarning(APP_NAME, "Enter your name and email in 'SEC contact' in the main window first.",
                                       parent=self.win)
                return
            a, b = self.start_var.get().lower(), self.end_var.get().lower()
            if a > b:
                a, b = b, a
            try:
                params = BacktestParams(start_q=a, end_q=b, user_agent=ua, warmup_quarters=int(self.warm_var.get()),
                                        min_value=float(self.min_var.get().replace(",", "") or 0),
                                        max_signals=int(self.max_var.get().replace(",", "") or 0),
                                        company_data=self.company_var.get(), side=self.side_var.get())
            except (ValueError, tk.TclError):
                messagebox.showwarning(APP_NAME, "Please check the numbers in Settings.", parent=self.win)
                return
            self.app.settings.update({"bt_start": a.upper(), "bt_end": b.upper(), "bt_warmup": params.warmup_quarters,
                                      "bt_min": self.min_var.get(), "bt_max": self.max_var.get(),
                                      "bt_company": params.company_data, "bt_side": params.side})
            self.cancel_event = threading.Event()
            self.run_btn.config(state="disabled")
            self.cancel_btn.config(state="normal")
            self.apply_btn.config(state="disabled")
            self.status.set("Starting...")
            threading.Thread(target=Backtester(params, self.q, self.cancel_event).run, daemon=True).start()
            self.win.after(150, self._poll)

        def cancel(self):
            if self.cancel_event:
                self.cancel_event.set()
                self.status.set("Cancelling...")

        def _poll(self):
            if not self.win.winfo_exists():
                return
            finished = False
            try:
                for _ in range(200):
                    _, kind, payload = self.q.get_nowait()
                    if kind == "phase":
                        self.status.set(payload)
                    elif kind == "bt_progress":
                        d, t = payload
                        self.progress.config(maximum=max(1, t), value=d)
                    elif kind == "bt_done":
                        self.result = payload
                        self._render(payload)
                        finished = True
                    elif kind == "cancelled":
                        self.status.set("Back-test cancelled.")
                        finished = True
                    elif kind == "error":
                        self.status.set("Back-test stopped with an error.")
                        messagebox.showerror(APP_NAME, payload, parent=self.win)
                        finished = True
            except queue.Empty:
                pass
            if finished:
                self.run_btn.config(state="normal")
                self.cancel_btn.config(state="disabled")
            else:
                self.win.after(150, self._poll)

        @staticmethod
        def _pct(v, digits=1):
            return "" if v is None else f"{v:+.{digits}f}%"

        def _render(self, res):
            p = res["params"]
            sells = p.get("side") == "S"
            self.t_bands.heading("hit6", text="Lagged SPY (6M)" if sells else "Beat SPY (6M)")
            miss = f"  •  missing data sets: {', '.join(res['missing_quarters'])}" if res["missing_quarters"] else ""
            self.status.set(f"Done: {res['signals']:,} {'sale' if sells else 'buy'} signals {p['start']}-{p['end']} "
                            f"(of {res['candidates']:,} {'sales' if sells else 'purchases'} ≥ ${p['min_value']:,.0f}), "
                            f"{res['priced6']:,} with 6-month results{miss}.")
            self.progress.config(value=self.progress.cget("maximum"))
            allb = res["bands"]["All"]
            self.summary.set(
                ("Sell Score: a good warning is followed by LOWER returns, so look for higher bands lagging SPY more.  "
                 if sells else "") +
                f"All signals: 6-month return {self._pct(allb['ret6M'])}, vs SPY {self._pct(allb['ex6M'])}, "
                f"{'lagged' if sells else 'beat'} SPY "
                f"{'' if allb['hit6'] is None else format(allb['hit6'], '.0f') + '%'} of the time.  "
                f"Related-owner duplicates removed: {res['dups']:,}.  Train/test split at {res['split_date'] or 'n/a'}.")
            t = self.t_bands
            t.delete(*t.get_children())
            for band in ("70+", "50-69", "under 50", "All"):
                b = res["bands"][band]
                good = (b["ex6M"] or 0) < 0 if sells else (b["ex6M"] or 0) > 0
                tag = ("good",) if good and b["ex6M"] is not None else (("bad",) if b["ex6M"] is not None else ())
                t.insert("", "end", tags=tag, values=(band, f"{b['n']:,}", *(self._pct(b[k]) for k in
                         ("ret1M", "ret3M", "ret6M", "ex1M", "ex3M", "ex6M")),
                         "" if b["hit6"] is None else f"{b['hit6']:.0f}%  (n={b['n6']})"))
            t = self.t_factors
            t.delete(*t.get_children())
            sugg = res["suggested"]
            sgn = -1 if sells else 1
            for f in sorted(res["factors"], key=lambda x: -(sgn * x["uplift"] if x["uplift"] is not None else -999)):
                s_ = sugg.get(f["key"])
                tag = ("dim",) if not f["enough"] else (("good",) if sgn * (f["uplift"] or 0) > 0 else ("bad",))
                t.insert("", "end", tags=tag, values=(
                    f["label"], f"{f['current']:+d}", f"{f['n_with']:,}", self._pct(f["with"]), self._pct(f["without"]),
                    self._pct(f["uplift"]), "too few signals" if s_ is None else f"{s_:+d}"))
            t = self.t_test
            t.delete(*t.get_children())
            for band in ("70+", "50-69", "under 50"):
                a, b = res["test_current"][band], res["test_tuned"][band]
                t.insert("", "end", values=(band, f"{a['n']:,}", self._pct(a["ex6M"]), f"{b['n']:,}", self._pct(b["ex6M"])))
            t = self.t_rows
            t.delete(*t.get_children())
            for r in sorted(res["rows"], key=lambda r: -(r.get("score") or 0))[:5000]:
                ex6 = r.get("ex6M")
                ok = (ex6 or 0) < 0 if sells else (ex6 or 0) > 0
                tag = ("good",) if (ok and ex6 is not None) else (("bad",) if ex6 is not None else ())
                t.insert("", "end", tags=tag, values=(
                    r["filing_date"], r["ticker"], r.get("insider", ""), fmt_money(r.get("value") or 0), r.get("score"),
                    self._pct(r.get("ret1M"), 0), self._pct(r.get("ret3M"), 0), self._pct(r.get("ret6M"), 0),
                    self._pct(ex6, 0), " ".join(r.get("factors", []))))
            self.apply_btn.config(state="normal" if sugg else "disabled")

        def apply(self):
            if not self.result or not self.result["suggested"]:
                return
            sugg = self.result["suggested"]
            lines = [f"{ALL_FACTORS[k][1]}: {WEIGHTS[k]:+d} → {v:+d}" for k, v in sugg.items() if v != WEIGHTS.get(k)]
            if not lines:
                messagebox.showinfo(APP_NAME, "The suggested weights match the ones in use.", parent=self.win)
                return
            msg = ("Apply these changes to the Signal Score?\n\n" + "\n".join(lines[:25]) +
                   ("\n..." if len(lines) > 25 else "") +
                   "\n\nThey were tuned on the first half of the period; check the 'Train / test check' tab to see "
                   "whether they also ranked the second half better. You can reset to defaults at any time.")
            if messagebox.askyesno(APP_NAME, msg, parent=self.win):
                new = dict(WEIGHTS)
                new.update(sugg)
                set_weights(new)
                self.app.weights_changed()
                self._show_weights()

        def reset(self):
            set_weights(None)
            self.app.weights_changed()
            self._show_weights()

        def export(self):
            if not self.result:
                return
            path = filedialog.asksaveasfilename(parent=self.win, defaultextension=".csv",
                                                initialfile=f"backtest_{self.result['params']['start']}_{self.result['params']['end']}.csv",
                                                filetypes=[("CSV (Excel)", "*.csv")])
            if path:
                with open(path, "w", newline="", encoding="utf-8-sig") as fh:
                    w = csv.writer(fh)
                    w.writerow(BT_CSV_HEADERS)
                    w.writerows(bt_row_csv(r) for r in self.result["rows"])

    class TrackRecordWindow:
        """How past signals have done since the insider bought, vs SPY, grouped by score band."""
        COLS = [("first_seen", "Logged", 90, "w"), ("side", "Side", 56, "center"), ("ticker", "Ticker", 64, "w"),
                ("insider", "Insider", 170, "w"),
                ("buy_date", "Trade Date", 90, "w"), ("price", "Price", 76, "e"), ("score", "Score", 56, "center"),
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
                      text="Every open-market buy and sale that received a score in a completed scan is logged once, "
                           "with its score at the time. Return = from the insider's price to the latest close "
                           "(split/dividend adjusted). SPY = the S&P 500 ETF over the same dates. For sales, a good "
                           "warning is one where the stock then lagged SPY. Double-click a row for its price chart."
                      ).pack(fill="x")
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
            app.theme_window(self.win, [self.tree])
            self.tree.bind("<Double-1>", self._open)
            self.sort_key, self.sort_desc = "buy_date", True
            log = SignalLog()
            self.items = log.all()
            log.close()
            for x in self.items:
                x["side"] = x.get("side") or "P"
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
                ret, exc = x.get("ret"), x.get("excess")
                sell = (x.get("side") or "P") == "S"
                ok = ((exc if exc is not None else ret) or 0) < 0 if sell else (ret or 0) > 0
                tag = ("up",) if (ok and ret is not None) else (("down",) if ret is not None else ())
                self.tree.insert("", "end", iid=x["acc"], tags=tag, values=(
                    x["first_seen"], "▼ Sell" if sell else "Buy", x["ticker"], x["insider"], x["buy_date"],
                    fmt_money(x["price"] or 0, 2), x["score"], x.get("pattern", ""),
                    fmt_money(x["now"], 2) if x.get("now") else "", fmt_pct(ret, digits=1),
                    fmt_pct(x.get("spy"), digits=1), fmt_pct(x.get("excess"), digits=1),
                    x.get("days", "")))
            self._summarise()

        def _summarise(self):
            if not self.items:
                return
            lines = []
            for side, title, verb in (("P", "Buys", "beat"), ("S", "Sells (▼ a good warning lags SPY)", "lagged")):
                summ = track_record_summary(self.items, side)
                if not any(summ.values()):
                    continue
                lines.append(title)
                for band in ("70+", "50-69", "under 50"):
                    b = summ.get(band)
                    if not b:
                        continue
                    vs = "" if b["avg_excess"] is None else f",  vs SPY {b['avg_excess']:+.1f}%,  {verb} SPY {b['beat']:.0f}% of the time"
                    lines.append(f"    Score {band}:  {b['n']} signals,  average return {b['avg']:+.1f}%{vs}")
            self.summary.set("\n".join(lines) or "Prices not available yet.")

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
                    raw = cache.get_blob(f"px4:{sym}:{today}")
                    if raw is not None:
                        return json.loads(raw)
                    b = client.bars(sym)
                    if b:
                        cache.put_blob(f"px4:{sym}:{today}", json.dumps(b).encode())
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
                w.writerow(["Logged", "Side", "Ticker", "Company", "Insider", "Trade Date", "Price", "Value", "Score",
                            "Pattern", "Cluster", "Last", "Return %", "SPY %", "vs SPY %", "Days", "Accession"])
                for x in self.items:
                    w.writerow([x["first_seen"], "Sell" if (x.get("side") or "P") == "S" else "Buy", x["ticker"],
                                x["company"], x["insider"], x["buy_date"], x["price"],
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
        r["score"], r["score_why"] = rate_row(r)
        if exclude_offering and r.get("offering"):
            continue
        if exclude_entity and r.get("entity") and r["code"] == "P":
            continue
        if min_score and r["code"] in ("P", "S") and (r.get("score") or 0) < min_score:
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


def run_backtest_cli(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="form4_scanner.py --backtest", description="Back-test the Signal Score.")
    ap.add_argument("start", help="first quarter, e.g. 2025Q2")
    ap.add_argument("end", help="last quarter, e.g. 2026Q1")
    ap.add_argument("--ua", required=True)
    ap.add_argument("--warmup", type=int, default=8, help="quarters of history loaded before the start")
    ap.add_argument("--min", type=float, default=100000)
    ap.add_argument("--max-signals", type=int, default=2000)
    ap.add_argument("--no-company-data", action="store_true")
    ap.add_argument("--sells", action="store_true", help="test the Sell Score instead of the buy Signal Score")
    ap.add_argument("--csv")
    a = ap.parse_args(argv)
    set_weights(load_settings().get("score_weights"))
    q: queue.Queue = queue.Queue()
    bt = Backtester(BacktestParams(a.start.lower(), a.end.lower(), a.ua, a.warmup, a.min, a.max_signals,
                                   not a.no_company_data, "S" if a.sells else "P"), q, threading.Event())
    t = threading.Thread(target=bt.run, daemon=True)
    t.start()
    res = None
    while t.is_alive() or not q.empty():
        try:
            _, kind, payload = q.get(timeout=0.5)
        except queue.Empty:
            continue
        if kind == "phase":
            print(f"[phase] {payload}", file=sys.stderr)
        elif kind == "error":
            print(f"[error] {payload}", file=sys.stderr)
            return 1
        elif kind == "bt_done":
            res = payload
    if not res:
        return 1

    def pct(v):
        return "     -" if v is None else f"{v:+6.1f}%"
    print(f"\nBack-test {res['params']['start']}-{res['params']['end']}: {res['signals']} signals "
          f"({res['priced6']} with 6-month results), {res['dups']} duplicate filings removed")
    print(f"{'Band':<10}{'n':>6} {'1M':>8}{'3M':>8}{'6M':>8} {'1M vs':>8}{'3M vs':>8}{'6M vs':>8}  beat SPY")
    for band, b in res["bands"].items():
        hit = "" if b["hit6"] is None else f"{b['hit6']:.0f}%"
        print(f"{band:<10}{b['n']:>6} {pct(b['ret1M'])}{pct(b['ret3M'])}{pct(b['ret6M'])} "
              f"{pct(b['ex1M'])}{pct(b['ex3M'])}{pct(b['ex6M'])}  {hit}")
    print("\nFactors (6-month return vs SPY, with vs without):")
    for f in sorted(res["factors"], key=lambda x: -(x["uplift"] or -999)):
        print(f"  {f['label'][:58]:<58} n={f['n_with']:<5} {pct(f['with'])} vs {pct(f['without'])}  "
              f"now {f['current']:+d} -> {'?' if not f['enough'] else format(f['suggested'], '+d')}")
    if a.csv:
        with open(a.csv, "w", newline="", encoding="utf-8-sig") as fh:
            w = csv.writer(fh)
            w.writerow(BT_CSV_HEADERS)
            w.writerows(bt_row_csv(r) for r in res["rows"])
    return 0


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
        if r.get("hkey") in history:
            r.update(history[r["hkey"]])
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
    if len(sys.argv) > 1 and sys.argv[1] == "--backtest":
        sys.exit(run_backtest_cli(sys.argv[2:]))
    run_gui()


if __name__ == "__main__":
    main()
