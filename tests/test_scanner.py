"""Offline end-to-end tests: mocks SEC.gov responses and runs the real Scanner."""
import datetime as dt
import json
import os
import queue
import sys
import tempfile
import threading

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
os.environ["LOCALAPPDATA"] = tempfile.mkdtemp()
import form4_scanner as F  # noqa: E402

TODAY = dt.date.today()


def last_weekday(offset):
    d = TODAY - dt.timedelta(days=offset)
    while d.weekday() >= 5:
        d -= dt.timedelta(days=1)
    return d


D1, D2 = last_weekday(2), last_weekday(5)
LASTWD = last_weekday(0)          # most recent weekday: its daily index "isn't published yet" in the mocks


def form4(acc, cik, name, ticker, owners, txns, filed, schema="X0508", fn="wk-form4_1.xml", plan=False):
    own_xml = ""
    from xml.sax.saxutils import escape
    owners = [{k: (escape(v) if isinstance(v, str) else v) for k, v in o.items()} for o in owners]
    name = escape(name)
    for o in owners:
        own_xml += f"""
  <reportingOwner>
    <reportingOwnerId>{('<rptOwnerCik>000' + o['cik'] + '</rptOwnerCik>') if o.get('cik') else ''}<rptOwnerName>{o['name']}</rptOwnerName></reportingOwnerId>
    <reportingOwnerRelationship>
      <isDirector>{o.get('dir','0')}</isDirector><isOfficer>{o.get('off','0')}</isOfficer>
      <isTenPercentOwner>{o.get('ten','false')}</isTenPercentOwner><isOther>0</isOther>
      {'<officerTitle>' + o['title'] + '</officerTitle>' if o.get('title') else ''}
      {'<otherText>' + o['other'] + '</otherText>' if o.get('other') else ''}
    </reportingOwnerRelationship>
  </reportingOwner>"""
    nd = ""
    for tx in txns:
        code, sh, px, ad = tx[:4]
        after = tx[4] if len(tx) > 4 else None
        fnote = tx[5] if len(tx) > 5 else ""
        own = tx[6] if len(tx) > 6 else "D"
        price = f"<value>{px}</value>" if px is not None else "<footnoteId id=\"F1\"/>"
        post = (f"<postTransactionAmounts><sharesOwnedFollowingTransaction><value>{after}</value>"
                f"</sharesOwnedFollowingTransaction></postTransactionAmounts>") if after is not None else ""
        nature = (f"<ownershipNature><directOrIndirectOwnership><value>{own}</value></directOrIndirectOwnership>"
                  + ("<natureOfOwnership><value>By Trust</value></natureOfOwnership>" if own == "I" else "")
                  + "</ownershipNature>")
        nd += f"""
    <nonDerivativeTransaction>
      <securityTitle><value>Common Stock</value></securityTitle>
      <transactionDate><value>{filed}</value></transactionDate>
      <transactionCoding><transactionFormType>4</transactionFormType><transactionCode>{code}</transactionCode><equitySwapInvolved>0</equitySwapInvolved></transactionCoding>
      <transactionAmounts>
        <transactionShares><value>{sh}</value></transactionShares>
        <transactionPricePerShare>{price}</transactionPricePerShare>
        <transactionAcquiredDisposedCode><value>{ad}</value></transactionAcquiredDisposedCode>
      </transactionAmounts>{post}{nature}{fnote}
    </nonDerivativeTransaction>"""
    deriv = """
  <derivativeTable><derivativeTransaction>
    <securityTitle><value>Stock Option (right to buy)</value></securityTitle>
    <transactionDate><value>2026-01-01</value></transactionDate>
    <transactionCoding><transactionCode>M</transactionCode></transactionCoding>
    <transactionAmounts><transactionShares><value>99999</value></transactionShares>
    <transactionPricePerShare><value>10</value></transactionPricePerShare>
    <transactionAcquiredDisposedCode><value>D</value></transactionAcquiredDisposedCode></transactionAmounts>
  </derivativeTransaction></derivativeTable>"""
    xml = f"""<?xml version="1.0"?>
<ownershipDocument>
  <schemaVersion>{schema}</schemaVersion>
  <documentType>4</documentType>
  {'<aff10b5One>1</aff10b5One>' if plan else '<aff10b5One>0</aff10b5One>'}
  <periodOfReport>{filed}</periodOfReport>
  <issuer><issuerCik>{cik:010d}</issuerCik><issuerName>{name}</issuerName><issuerTradingSymbol>{ticker}</issuerTradingSymbol></issuer>
  {own_xml}
  <nonDerivativeTable>{nd}
  </nonDerivativeTable>{deriv}
  <footnotes>
    <footnote id="F1">Weighted average price.</footnote>
    <footnote id="F2">The sales reported were effected pursuant to a Rule 10b5-1 trading plan adopted by the reporting person on May 1, 2026.</footnote>
    <footnote id="F3">These shares were not purchased pursuant to a Rule 10b5-1 trading plan.</footnote>
    <footnote id="F4">Purchased in the Issuer's underwritten public offering at the public offering price.</footnote>
    <footnote id="F5">Shares sold to cover tax withholding obligations in connection with the vesting of restricted stock units.</footnote>
  </footnotes>
</ownershipDocument>"""
    return f"""<SEC-DOCUMENT>{acc}.txt : {filed:%Y%m%d}
<SEC-HEADER>{acc}.hdr.sgml : {filed:%Y%m%d}
ACCESSION NUMBER:		{acc}
CONFORMED SUBMISSION TYPE:	4
PUBLIC DOCUMENT COUNT:		1
FILED AS OF DATE:		{filed:%Y%m%d}
</SEC-HEADER>
<DOCUMENT>
<TYPE>4
<SEQUENCE>1
<FILENAME>{fn}
<DESCRIPTION>FORM 4 SUBMISSION
<TEXT>
<XML>
{xml}
</XML>
</TEXT>
</DOCUMENT>
</SEC-DOCUMENT>
""".replace("{filed}", str(filed)).encode()


FILINGS = {
    # acc: (issuer cik, name, ticker, owners, txns, date)
    "0000320193-26-000101": (320193, "Apple Inc.", "AAPL",
                             [{"name": "Cook Timothy D", "off": "1", "dir": "1", "title": "Chief Executive Officer"}],
                             [("S", 50000, 230.10, "D", 3000000, '<footnoteId id="F2"/>'), ("S", 25000, 231.00, "D", 2975000, '<footnoteId id="F2"/>')], D1),
    "0000789019-26-000202": (789019, "MICROSOFT CORP", "MSFT",
                             [{"name": "Hood Amy", "off": "true", "title": "EVP, CFO"}],
                             [("P", 1000, 410.0, "A", 11000)], D1),
    "0000789019-26-000203": (789019, "MICROSOFT CORP", "MSFT",
                             [{"name": "Smith Bradford L", "off": "1", "title": "Vice Chair & President, General Counsel"}],
                             [("P", 100, 400.0, "A")], D2),      # $40k: below $50k min
    "0001045810-26-000303": (1045810, "NVIDIA CORP", "NVDA",
                             [{"name": "Director Jane", "dir": "1"}],
                             [("P", 300, 150.0, "A", 1300), ("P", 200, 160.0, "A", 1500), ("A", 5000, 0, "A", 6500)], D2),
    "0000999999-26-000404": (999999, "Tiny Bio Inc", "TBIO",
                             [{"name": "Big Fund LP", "ten": "true"}, {"name": "Big Fund GP LLC", "other": "10% owner group"}],
                             [("P", 100000, 2.5, "A", 100000), ("P", 1000, None, "A", 101000)], D2),
    "0000888888-26-000505": (888888, "Acme Corp", "ACME",
                             [{"name": "Lee Pat", "off": "1", "title": "Chief Legal Officer & Secretary"}],
                             [("M", 20000, 12.0, "A"), ("S", 20000, 55.0, "D")], D1),
    # BRK files a Form 4 as *reporting owner* of OXY - must not appear for ticker BRK.B
    "0000315090-26-000606": (797468, "OCCIDENTAL PETROLEUM CORP", "OXY",
                             [{"name": "BERKSHIRE HATHAWAY INC", "ten": "1", "cik": "1067983"}],
                             [("P", 1000000, 55.0, "A", 250000000)], D1),
    "0001067983-26-000707": (1067983, "BERKSHIRE HATHAWAY INC", "BRK.B",
                             [{"name": "Buffett Warren E", "off": "1", "dir": "1", "title": "Chairman & CEO"}],
                             [("P", 1000, 480.0, "A", 5000, '<footnoteId id="F3"/>')], D1),
    # second NVDA insider: small buy (below $50k) - still counts toward the cluster
    "0001045810-26-000310": (1045810, "NVIDIA CORP", "NVDA",
                             [{"name": "Other Director", "dir": "1", "cik": "222"}],
                             [("P", 100, 150.0, "A", 900)], D1),
    # third NVDA insider buying under a 10b5-1 plan (checkbox) - excluded from 'disc' cluster
    "0001045810-26-000311": (1045810, "NVIDIA CORP", "NVDA",
                             [{"name": "Planned Buyer", "off": "1", "title": "CTO", "cik": "333"}],
                             [("P", 1000, 150.0, "A", 2000)], D1, {"plan": True}),
    # CEO buy with direct + indirect (trust) lines: 1000 direct (->3000) and 1000 by trust (->1000 new)
    "0000666666-26-000101": (666666, "Split Lines Inc", "SPLT",
                             [{"name": "Two Line CEO", "off": "1", "title": "CEO", "cik": "444"}],
                             [("P", 1000, 50.0, "A", 3000, "", "D"), ("P", 1000, 50.0, "A", 1000, "", "I")], D2),
}

# Responses
RESP = {}
for acc, (cik, name, tic, owners, txns, d, *opt) in FILINGS.items():
    RESP[f"{F.SEC_WWW}/Archives/edgar/data/{cik}/{acc}.txt"] = form4(acc, cik, name, tic, owners, txns, d,
                                                                   **(opt[0] if opt else {}))


def master_idx(day):
    lines = ["Description:           Daily Index of EDGAR Dissemination Feed by Company Name",
             f"Last Data Received:    {day:%B %d, %Y}", "", "CIK|Company Name|Form Type|Date Filed|Filename",
             "-" * 80]
    for acc, (cik, name, _t, _o, _x, d, *_opt) in FILINGS.items():
        if d == day:
            lines.append(f"{cik}|{name}|4|{day:%Y%m%d}|edgar/data/{cik}/{acc}.txt")
            lines.append(f"111|SOME OWNER|4|{day:%Y%m%d}|edgar/data/111/{acc}.txt")   # duplicate (owner side)
    lines.append(f"1|X CORP|10-K|{day:%Y%m%d}|edgar/data/1/0000000001-26-000001.txt")
    lines.append(f"1|Y CORP|424B2|{day:%Y%m%d}|edgar/data/1/0000000001-26-000002.txt")
    return "\n".join(lines).encode()


for day in (D1, D2):
    q = (day.month - 1) // 3 + 1
    RESP[f"{F.SEC_WWW}/Archives/edgar/daily-index/{day.year}/QTR{q}/master.{day:%Y%m%d}.idx"] = master_idx(day)

RESP[f"{F.SEC_WWW}/files/company_tickers.json"] = json.dumps({
    "0": {"cik_str": 320193, "ticker": "AAPL", "title": "Apple Inc."},
    "1": {"cik_str": 789019, "ticker": "MSFT", "title": "MICROSOFT CORP"},
    "2": {"cik_str": 1045810, "ticker": "NVDA", "title": "NVIDIA CORP"},
    "3": {"cik_str": 1067983, "ticker": "BRK-B", "title": "BERKSHIRE HATHAWAY INC"},
}).encode()


def submissions(cik):
    accs = [a for a, v in FILINGS.items() if v[0] == cik or (cik == 1067983 and a.startswith("0000315090"))]
    return json.dumps({"cik": str(cik), "filings": {"recent": {
        "accessionNumber": accs + ["0000000000-26-999999"],
        "filingDate": [str(FILINGS[a][5]) for a in accs] + ["2020-01-01"],  # noqa
        "form": ["4"] * len(accs) + ["4"],
        "primaryDocument": ["xslF345X05/wk-form4_1.xml"] * (len(accs) + 1),
    }, "files": []}}).encode()


for c in (320193, 789019, 1045810, 1067983):
    RESP[f"{F.SEC_DATA}/submissions/CIK{c:010d}.json"] = submissions(c)

# Path used for OXY filing listed under BRK's submissions (issuer cik dir differs, fine for mock)
RESP[f"{F.SEC_WWW}/Archives/edgar/data/1067983/0000315090-26-000606.txt"] = RESP[
    f"{F.SEC_WWW}/Archives/edgar/data/797468/0000315090-26-000606.txt"]

# Today: latest-filings atom feed (daily index not yet published)
TODAY_ACC = "0000555555-26-000808"
RESP[f"{F.SEC_WWW}/Archives/edgar/data/555555/{TODAY_ACC}.txt"] = form4(
    TODAY_ACC, 555555, "Fresh Co", "FRSH",
    [{"name": "Doe John", "off": "1", "title": "President and CEO"}], [("P", 10000, 20.0, "A")], LASTWD)
ATOM = f"""<?xml version="1.0" encoding="ISO-8859-1" ?>
<feed xmlns="http://www.w3.org/2005/Atom">
<title>Latest Filings</title>
<entry>
<title>4 - Fresh Co (0000555555) (Issuer)</title>
<link rel="alternate" type="text/html" href="https://www.sec.gov/Archives/edgar/data/555555/{TODAY_ACC.replace('-', '')}/{TODAY_ACC}-index.htm"/>
<summary type="html"> &lt;b&gt;Filed:&lt;/b&gt; {LASTWD} </summary>
<updated>{LASTWD}T16:05:11-05:00</updated>
<category scheme="https://www.sec.gov/" label="form type" term="4"/>
</entry>
<entry>
<title>4 - Doe John (0000777777) (Reporting)</title>
<link rel="alternate" type="text/html" href="https://www.sec.gov/Archives/edgar/data/777777/{TODAY_ACC.replace('-', '')}/{TODAY_ACC}-index.htm"/>
<updated>{LASTWD}T16:05:11-05:00</updated>
</entry>
<entry>
<title>424B2 - Bank Notes (0000123) (Filer)</title>
<link rel="alternate" type="text/html" href="https://www.sec.gov/Archives/edgar/data/123/000012326000001/0000123-26-000001-index.htm"/>
<updated>{TODAY}T16:00:00-05:00</updated>
</entry>
<entry>
<title>4 - Old Co (0000444) (Issuer)</title>
<link rel="alternate" type="text/html" href="https://www.sec.gov/Archives/edgar/data/444/000044426000001/0000444-26-000001-index.htm"/>
<updated>{TODAY - dt.timedelta(days=10)}T09:00:00-05:00</updated>
</entry>
</feed>""".encode()

CALLS = []


def fake_get(self, url, retries=4):
    CALLS.append(url)
    if self.cancel.is_set():
        raise F.Cancelled()
    if "getcurrent" in url:
        return ATOM if "start=0&" in url else b'<feed xmlns="http://www.w3.org/2005/Atom"></feed>'
    import re as _re
    url = _re.sub(r"/data/(\d+)/\d{18}/", r"/data/\1/", url)   # both EDGAR path styles are valid
    if url in RESP:
        return RESP[url]
    if "daily-index" in url:
        raise F.NotFound(url)
    raise F.NotFound(url)


F.SecClient.get = fake_get


CLUSTERS = {}


def run(params):
    q = queue.Queue()
    F.Scanner(params, q, threading.Event()).run()
    rows, final = [], None
    CLUSTERS.clear()
    while not q.empty():
        _, kind, payload = q.get()
        if kind == "rows":
            rows.extend(payload)
        elif kind == "clusters":
            CLUSTERS.update(payload)
        elif kind in ("done", "error", "cancelled"):
            final = (kind, payload)
    return rows, final


def P(**kw):
    base = dict(start=TODAY - dt.timedelta(days=9), end=TODAY, user_agent="Test test@example.com")
    base.update(kw)
    return F.ScanParams(**base)


def show(rows):
    for r in rows:
        print(f"   {r['filing_date']} {r['ticker']:<6} {r['side']:<8} sh={r['shares']:>9,.0f} px={r['price']:>8.2f} "
              f"val={r['value']:>12,.0f} | {r['insider'][:34]:<34} | {r['role']:<30} | {r['title']}")


fails = 0


def check(name, cond):
    global fails
    print(("PASS " if cond else "FAIL ") + name)
    if not cond:
        fails += 1


# 1. Market-wide default (P only, $50k)
rows, final = run(P())
print("\n[1] market-wide, P only, min $50k ->", final)
show(rows)
tick = sorted(r["ticker"] for r in rows)
check("market-wide P >= 50k", tick == ["BRK.B", "FRSH", "MSFT", "NVDA", "NVDA", "OXY", "SPLT", "TBIO"])
check("dedupe: 11 index filings + 1 feed", final[1]["total"] == 12)
nv = next(r for r in rows if r["ticker"] == "NVDA" and r["insider"] == "Director Jane")
check("NVDA lots combined (300@150 + 200@160 = 77,000)", abs(nv["value"] - 77000) < 0.01 and nv["shares"] == 500)
check("NVDA weighted avg price 154", abs(nv["price"] - 154.0) < 1e-9)
tb = next(r for r in rows if r["ticker"] == "TBIO")
check("missing price treated as 0", abs(tb["value"] - 250000) < 0.01)
check("joint filers combined", tb["insider"] == "Big Fund LP; Big Fund GP LLC")
check("rendered link", rows[0]["link"].startswith("https://www.sec.gov/Archives/edgar/data/") and "/xslF345X05/wk-form4_1.xml" in rows[0]["link"])
check("feed picked up the not-yet-indexed day's filing", any(r["ticker"] == "FRSH" and r["filing_date"] == str(LASTWD) for r in rows))

# 2. Cached re-scan: no Archives fetches for filings
CALLS.clear()
rows2, final2 = run(P())
check("re-scan served from cache", final2[1]["cached"] == 12 and not any(u.endswith(".txt") for u in CALLS))
check("past daily indexes cached", not any("daily-index" in u and (f"{D1:%Y%m%d}" in u or f"{D2:%Y%m%d}" in u) for u in CALLS))

# 3. Role filters
rows, _ = run(P(codes={"P", "S"}, roles={F.ROLE_CEO}))
print("\n[3] CEO, P+S"); show(rows)
check("CEO filter", sorted(r["ticker"] for r in rows) == ["AAPL", "BRK.B", "FRSH", "SPLT"])
aapl = next(r for r in rows if r["ticker"] == "AAPL")
check("AAPL sale combined 75,000 sh", aapl["shares"] == 75000 and aapl["side"] == "Sell")

rows, _ = run(P(codes={"P", "S"}, roles={F.ROLE_CFO}))
check("CFO filter matches 'EVP, CFO' (410k)", [r["ticker"] for r in rows] == ["MSFT"])

rows, _ = run(P(codes={"P", "S"}, roles={F.ROLE_LEGAL}, min_value=0))
print("\n[3c] Legal, min 0"); show(rows)
check("Legal filter: GC + CLO", sorted(r["ticker"] for r in rows) == ["ACME", "MSFT"])

rows, _ = run(P(codes={"P"}, roles={F.ROLE_DIR}))
check("Director/10% filter", sorted(r["ticker"] for r in rows) == ["BRK.B", "NVDA", "OXY", "TBIO"])

# 4. Codes A/M toggles
rows, _ = run(P(codes={"M"}, min_value=0))
check("M from non-derivative table only (20,000 sh)", len(rows) == 1 and rows[0]["shares"] == 20000)
rows, _ = run(P(codes={"A"}, min_value=0))
check("A grant shows with min 0", len(rows) == 1 and rows[0]["ticker"] == "NVDA")
rows, _ = run(P(codes={"A"}))
check("A grant ($0) excluded with min 50k", len(rows) == 0)

# 5. Ticker mode
CALLS.clear()
rows, final = run(P(tickers=["AAPL", "MSFT", "BRK.B", "ZZZZ"], codes={"P", "S"}, min_value=0))
print("\n[5] tickers ->", final); show(rows)
check("ticker mode no daily index", not any("daily-index" in u for u in CALLS))
check("ticker mode results", sorted(r["ticker"] for r in rows) == ["AAPL", "BRK.B", "MSFT", "MSFT"])
check("BRK as reporting owner (OXY) excluded", not any(r["ticker"] == "OXY" for r in rows))

# 6. Uncombined lots
rows, _ = run(P(tickers=["NVDA"], combine_lots=False, min_value=0))
check("uncombined lots -> 4 rows (2 lots + 2 other insiders)", len(rows) == 4)

# 7. role matcher unit checks
R = F.owner_roles
check("'President & C.E.O.'", F.ROLE_CEO in R({"title": "President & C.E.O."}))
check("'Chief Executive Officer'", F.ROLE_CEO in R({"title": "chief executive officer"}))
check("'Director of Sales' not director", F.ROLE_DIR not in R({"title": "Director of Sales", "off": True}))
check("'Exec VP' no CEO false positive", F.ROLE_CEO not in R({"title": "Executive VP, Operations"}))
check("'Associate General Counsel'", F.ROLE_LEGAL in R({"title": "Associate General Counsel"}))
check("'CFO' token", F.ROLE_CFO in R({"title": "SVP & CFO"}))
check("'Chief Financial Officer'", F.ROLE_CFO in R({"title": "Chief Financial Officer"}))
check("parse_tickers", F.parse_tickers(" aapl, MSFT ;nvda $tsla aapl") == ["AAPL", "MSFT", "NVDA", "TSLA"])
check("UA validation", F.valid_user_agent("Jane Doe jane@ex.com") and not F.valid_user_agent("Jane"))

# 8. New signal features
rows, _ = run(P(codes={"P", "S"}, min_value=0))
by = {(r["ticker"], r["insider"]): r for r in rows}
print("\n[8] clusters:", {k: v[:2] for k, v in CLUSTERS.items() if not k.endswith("|A")})
check("NVDA buy cluster: 3 insiders, 2 discretionary", CLUSTERS.get("1045810|P")[:2] == (3, 2))
check("MSFT cluster = 2 (Hood + Smith)", CLUSTERS.get("789019|P")[:2] == (2, 2))
check("single-insider issuer cluster = 1", CLUSTERS.get("797468|P")[:2] == (1, 1))
check("stake: NVDA +50%", abs(by[("NVDA", "Director Jane")]["stake"] - 50.0) < 1e-9)
check("stake: TBIO new position", by[("TBIO", "Big Fund LP; Big Fund GP LLC")]["stake"] == float("inf"))
check("stake: MSFT +10%", abs(by[("MSFT", "Hood Amy")]["stake"] - 10.0) < 1e-9)
check("stake: direct + trust lines +100%", abs(by[("SPLT", "Two Line CEO")]["stake"] - 100.0) < 1e-9)
check("stake: AAPL sale -2.46%", abs(by[("AAPL", "Cook Timothy D")]["stake"] - (-75000 / 3050000 * 100)) < 1e-9)
check("10b5-1 via footnote", by[("AAPL", "Cook Timothy D")]["plan"] is True)
check("'not pursuant to 10b5-1' footnote ignored", by[("BRK.B", "Buffett Warren E")]["plan"] is False)
check("10b5-1 via Form 4 checkbox", by[("NVDA", "Planned Buyer")]["plan"] is True)
check("no plan by default", by[("MSFT", "Hood Amy")]["plan"] is False)
check("fmt_stake", (F.fmt_stake(float("inf")), F.fmt_stake(12.345), F.fmt_stake(-2.5), F.fmt_stake(None))
      == ("New", "+12.3%", "-2.5%", ""))

buys = [dict(r) for r in rows if r["code"] == "P"]
out = F.apply_signal_filters([dict(r) for r in buys], CLUSTERS, True, 2, 0)
check("clusters-only (excl. 10b5-1): NVDA x2 + MSFT x2, planned buyer dropped",
      sorted(r["insider"] for r in out) == ["Director Jane", "Hood Amy", "Other Director", "Smith Bradford L"])
out = F.apply_signal_filters([dict(r) for r in buys], CLUSTERS, False, 3, 0)
check("clusters >=3 incl. plans: all three NVDA buyers", sorted(r["insider"] for r in out) == ["Director Jane", "Other Director", "Planned Buyer"])
out = F.apply_signal_filters([dict(r) for r in buys], CLUSTERS, False, 0, 20)
print("   min-stake 20:", sorted((r["ticker"], F.fmt_stake(r["stake"])) for r in out))
check("min stake 20% (Other Director +12.5%, MSFT/FRSH +10%, OXY +0.4% dropped)",
      sorted(r["ticker"] for r in out) == ["BRK.B", "NVDA", "NVDA", "SPLT", "TBIO"])
out = F.apply_signal_filters([dict(r) for r in rows], CLUSTERS, True, 0, 0)
check("exclude plan drops AAPL sale + planned buy", not any(r["plan"] for r in out) and len(out) == len(rows) - 2)
csvrow = F.row_to_csv(by[("TBIO", "Big Fund LP; Big Fund GP LLC")])
H = F.CSV_HEADERS.index
check("CSV has stake/cluster/plan", csvrow[H("Stake Change %")] == "New position" and csvrow[H("10b5-1 Plan")] == "No"
      and len(csvrow) == len(F.CSV_HEADERS))

# 9. Insider history: buying pattern + later sales (ticker mode, company HIST)
HIST = 424242
H_FILINGS = {}   # acc -> (owner dict, txns, date, issuer cik)
_n = [0]


def hacc(issuer=HIST):
    _n[0] += 1
    return f"{issuer:010d}-2{_n[0] % 10}-{_n[0]:06d}"


def add_hist(owner, txns, d, issuer=HIST):
    a = hacc(issuer)
    H_FILINGS[a] = (owner, txns, d, issuer)
    return a


RITA = {"name": "Routine Rita", "off": "1", "title": "COO", "cik": "5001"}
QUINN = {"name": "Quarterly Quinn", "dir": "1", "cik": "5002"}
FRED = {"name": "Fresh Fred", "dir": "1", "cik": "5003"}
FLO = {"name": "Flipper Flo", "off": "1", "title": "CEO", "cik": "5004"}
TOM = {"name": "Taxed Tom", "off": "1", "title": "CFO", "cik": "5005"}
CUR = {}
for k in (1, 2, 3):                                   # Rita: same time every year for 3 years
    add_hist(RITA, [("P", 500, 20.0, "A", 10000 - 500 * (3 - k))], F._years_ago(D2, k) + dt.timedelta(days=5))
add_hist(RITA, [("P", 900, 21.0, "A", 9000)], F._years_ago(D2, 2) + dt.timedelta(days=5), issuer=999998)  # other company
CUR["rita"] = add_hist(RITA, [("P", 2000, 30.0, "A", 12000)], D2)
for days in (80, 170, 260, 350):                      # Quinn: roughly quarterly
    add_hist(QUINN, [("P", 300, 25.0, "A", 4000)], D2 - dt.timedelta(days=days))
CUR["quinn"] = add_hist(QUINN, [("P", 3000, 30.0, "A", 8000)], D2)
add_hist(FRED, [("S", 1000, 15.0, "D", 3000)], F._years_ago(D2, 2))      # an old SALE, no buys
CUR["fred"] = add_hist(FRED, [("P", 5000, 30.0, "A", 8000)], D2)
add_hist(FLO, [("P", 100, 12.0, "A", 4000)], F._years_ago(D2, 2) - dt.timedelta(days=100))
CUR["flo"] = add_hist(FLO, [("P", 1000, 30.0, "A", 5000)], D2)
add_hist(FLO, [("S", 2000, 33.0, "D", 3000)], D1)                         # sells 40% days later
CUR["tom"] = add_hist(TOM, [("P", 1500, 30.0, "A", 6000)], D2)
add_hist(TOM, [("F", 200, 31.0, "D", 5800)], D1)                          # tax withholding only

for a, (owner, txns, d, iss) in H_FILINGS.items():
    RESP[f"{F.SEC_WWW}/Archives/edgar/data/{iss}/{a}.txt"] = form4(
        a, iss, "Hist Co" if iss == HIST else "Other Co", "HIST" if iss == HIST else "OTHR", [owner], txns, d)


def subs_json(accs):
    accs = sorted(accs, key=lambda a: H_FILINGS[a][2], reverse=True)
    return json.dumps({"filings": {"recent": {
        "accessionNumber": accs, "filingDate": [str(H_FILINGS[a][2]) for a in accs],
        "form": ["4"] * len(accs), "primaryDocument": ["x.xml"] * len(accs)}, "files": []}}).encode()


RESP[f"{F.SEC_DATA}/submissions/CIK{HIST:010d}.json"] = subs_json([a for a, v in H_FILINGS.items() if v[3] == HIST])
for o in (RITA, QUINN, FRED, FLO, TOM):
    RESP[f"{F.SEC_DATA}/submissions/CIK{int(o['cik']):010d}.json"] = subs_json(
        [a for a, v in H_FILINGS.items() if v[0] is o])
tickers = json.loads(RESP[f"{F.SEC_WWW}/files/company_tickers.json"])
tickers["9"] = {"cik_str": HIST, "ticker": "HIST", "title": "Hist Co"}
RESP[f"{F.SEC_WWW}/files/company_tickers.json"] = json.dumps(tickers).encode()
F.Cache.clear_all()                                   # ticker list is cached - reload it

CALLS.clear()
q = queue.Queue()
F.Scanner(P(tickers=["HIST"], codes={"P"}, min_value=0), q, threading.Event()).run()
hrows, hist, kinds = [], {}, []
while not q.empty():
    _, kind, payload = q.get()
    kinds.append(kind)
    if kind == "rows":
        hrows.extend(payload)
    elif kind == "history":
        hist.update(payload)
for r in hrows:
    r.update(hist.get(r["acc"], {}))
hb = {r["insider"]: r for r in hrows}
print("\n[9] history:")
for r in hrows:
    print(f"   {r['insider']:<16} {r['pattern']:<40} {r['sold']}")
check("history rows for all 5 buyers", sorted(hb) == ["Flipper Flo", "Fresh Fred", "Quarterly Quinn", "Routine Rita", "Taxed Tom"])
check("history posted before done", "history" in kinds and kinds.index("history") < kinds.index("done"))
check("Rita = routine", hb["Routine Rita"]["pattern_kind"] == "routine")
check("Quinn = regular (quarterly)", hb["Quarterly Quinn"]["pattern_kind"] == "regular")
check("Fred = first buy (old sale ignored)", hb["Fresh Fred"]["pattern_kind"] == "first" and hb["Fresh Fred"]["pattern"] == "First buy in 5+ yrs")
check("Flo = opportunistic", hb["Flipper Flo"]["pattern_kind"] == "opp" and hb["Flipper Flo"]["pattern"].startswith("Opportunistic (last "))
check("Flo sold 40% since", hb["Flipper Flo"]["sold_kind"] == "sold" and abs(hb["Flipper Flo"]["sold_pct"] - 40.0) < 1e-9
      and hb["Flipper Flo"]["sold"].startswith("Sold 40% ("))
check("Tom: tax withholding is not a sale", hb["Taxed Tom"]["sold_kind"] == "tax")
check("Rita/Quinn/Fred not sold", all(hb[n]["sold_kind"] == "none" for n in ("Routine Rita", "Quarterly Quinn", "Fresh Fred")))
other = [a for a, v in H_FILINGS.items() if v[3] == 999998][0]
check("other-company filing never fetched", not any(other in u for u in CALLS))
check("routine buyer's other-company buy ignored", hb["Routine Rita"]["pattern_kind"] == "routine")
out = F.apply_signal_filters([dict(r) for r in hrows], {}, False, 0, 0, hide_routine=True)
check("hide routine/regular", sorted(r["insider"] for r in out) == ["Flipper Flo", "Fresh Fred", "Taxed Tom"])
out = F.apply_signal_filters([dict(r) for r in hrows], {}, False, 0, 0, hide_routine=True, hide_sold=True)
check("hide routine + sold", sorted(r["insider"] for r in out) == ["Fresh Fred", "Taxed Tom"])
csvrow = F.row_to_csv(hb["Flipper Flo"])
check("CSV pattern/sold columns", csvrow[F.CSV_HEADERS.index("Sold % of Holding")] == 40.0
      and csvrow[F.CSV_HEADERS.index("Pattern")].startswith("Opportunistic") and len(csvrow) == len(F.CSV_HEADERS))

# second run: history served from cache (no new filing downloads)
CALLS.clear()
q = queue.Queue()
F.Scanner(P(tickers=["HIST"], codes={"P"}, min_value=0), q, threading.Event()).run()
check("history re-run uses cache", not any(u.endswith(".txt") for u in CALLS)
      and not any(f"CIK{5001:010d}" in u for u in CALLS))

# history off -> no extra requests, rows not pending
CALLS.clear()
F.Cache.clear_all()
rows_nh, _ = run(P(tickers=["HIST"], codes={"P"}, min_value=0, check_history=False))
check("history off: no owner lookups", not any(f"CIK{5001:010d}" in u for u in CALLS)
      and all(r["pattern_kind"] == "na" for r in rows_nh))

# pattern unit checks
d0 = dt.date(2026, 11, 10)
check("buy 2 weeks earlier = same spree -> first", F.classify_pattern(d0, [d0 - dt.timedelta(days=14)])[0] == "first")
check("2 of 3 years -> regular", F.classify_pattern(d0, [dt.date(2025, 11, 1), dt.date(2024, 11, 20)])[0] == "regular")
check("Feb 29 safe", F._years_ago(dt.date(2024, 2, 29), 1) == dt.date(2023, 2, 28))
check("later sale before buy date ignored", F.classify_later_sales(
    d0, [{"txns": [{"t": "N", "code": "S", "sh": 100, "date": "2026-11-01"}]}], 1000)[0] == "none")
check("sale without holdings -> share count", F.classify_later_sales(
    d0, [{"txns": [{"t": "N", "code": "S", "sh": 100, "date": "2026-11-12"}]}], None)[1] == "Sold 100 sh (Nov 12)")

# 10. v1.3: offering / fund flags, company data, prices, Signal Score, track record
SIG = 777001
S_FIL = {}
F4N = '<footnoteId id="F4"/>'
CEO_A = {"name": "Able Alice", "off": "1", "title": "Chief Executive Officer", "cik": "6001"}
FUND_B = {"name": "Alpha Capital Partners LP", "ten": "1", "cik": "6002"}
DIR_C = {"name": "Offering Oscar", "dir": "1", "cik": "6003"}
SELL_D = {"name": "Seller Sam", "off": "1", "title": "President", "cik": "6004"}
_k = [0]


def sacc():
    _k[0] += 1
    return f"{SIG:010d}-26-{_k[0]:06d}"


def add_sig(owner, txns, d):
    a = sacc()
    S_FIL[a] = (owner, txns, d)
    return a


A_ACC = add_sig(CEO_A, [("P", 20000, 15.0, "A", 30000)], D2)
B_ACC = add_sig(FUND_B, [("P", 10000, 15.0, "A", 1010000)], D2)
C_ACC = add_sig(DIR_C, [("P", 50000, 12.0, "A", 60000, F4N)], D2)
D_ACC = add_sig(SELL_D, [("S", 300000, 20.0, "D", 100000)], TODAY - dt.timedelta(days=60))
for a, (owner, txns, d) in S_FIL.items():
    RESP[f"{F.SEC_WWW}/Archives/edgar/data/{SIG}/{a}.txt"] = form4(a, SIG, "Signal Corp", "SIGL", [owner], txns, d)


def sig_subs(accs, extra=()):
    accs = sorted(accs, key=lambda a: S_FIL[a][2], reverse=True)
    acc_list = accs + [e[2] for e in extra]
    return json.dumps({"filings": {"recent": {
        "accessionNumber": acc_list,
        "filingDate": [str(S_FIL[a][2]) for a in accs] + [e[0] for e in extra],
        "form": ["4"] * len(accs) + [e[1] for e in extra],
        "primaryDocument": ["x.xml"] * len(acc_list)}, "files": []}}).encode()


D13 = (str(TODAY - dt.timedelta(days=20)), "SCHEDULE 13D", "0009999999-26-000001")
OLD13 = (str(TODAY - dt.timedelta(days=200)), "SC 13D", "0009999999-25-000002")
RESP[f"{F.SEC_DATA}/submissions/CIK{SIG:010d}.json"] = sig_subs(list(S_FIL), extra=[D13, OLD13])
for o in (CEO_A, FUND_B, DIR_C, SELL_D):
    RESP[f"{F.SEC_DATA}/submissions/CIK{int(o['cik']):010d}.json"] = sig_subs([a for a, v in S_FIL.items() if v[0] is o])
RESP[f"{F.SEC_DATA}/api/xbrl/companyconcept/CIK{SIG:010d}/dei/EntityCommonStockSharesOutstanding.json"] = json.dumps(
    {"units": {"shares": [{"end": "2025-06-30", "val": 9000000, "filed": "2025-08-01"},
                          {"end": "2026-06-30", "val": 10000000, "filed": "2026-08-01"}]}}).encode()


def yahoo_json(points):
    ts = [int(dt.datetime(d.year, d.month, d.day, 14, 30, tzinfo=dt.timezone.utc).timestamp()) for d, _ in points]
    closes = [c for _, c in points]
    return json.dumps({"chart": {"result": [{"meta": {"gmtoffset": -18000, "symbol": "SIGL"}, "timestamp": ts,
                       "indicators": {"quote": [{"close": closes}], "adjclose": [{"adjclose": closes}]}}],
                       "error": None}}).encode()


pts, d = [], TODAY - dt.timedelta(days=700)
while d <= TODAY:
    if d.weekday() < 5:
        age = (D2 - d).days
        if d > D2:
            px = 18.0                                   # after the buy: 18
        elif age <= 60:
            px = 15.0                                   # 2 months before the buy: fell to 15
        elif age <= 240:
            px = 30.0                                   # high of 30 within the past year
        else:
            px = 20.0
        pts.append((d, px))
    d += dt.timedelta(days=1)
pts[-3] = (pts[-3][0], None)                            # a null bar must be skipped
RESP["https://query1.finance.yahoo.com/v8/finance/chart/SIGL?range=10y&interval=1d&includeAdjustedClose=true"] = yahoo_json(pts)
tick = json.loads(RESP[f"{F.SEC_WWW}/files/company_tickers.json"])
tick["10"] = {"cik_str": SIG, "ticker": "SIGL", "title": "Signal Corp"}
RESP[f"{F.SEC_WWW}/files/company_tickers.json"] = json.dumps(tick).encode()
F.Cache.clear_all()

q = queue.Queue()
F.Scanner(P(tickers=["SIGL"], codes={"P", "S"}, min_value=0), q, threading.Event()).run()
srows, extra, sclusters, kinds = [], {}, {}, []
while not q.empty():
    _, kind, payload = q.get()
    kinds.append(kind)
    if kind == "rows":
        srows.extend(payload)
    elif kind in ("history", "context"):
        for acc, info in payload.items():
            extra.setdefault(acc, {}).update(info)
    elif kind == "clusters":
        sclusters.update(payload)
for r in srows:
    r.update(extra.get(r["acc"], {}))
F.apply_signal_filters(srows, sclusters, False, 0, 0)
sb = {r["insider"]: r for r in srows if r["code"] == "P"}
A, B, C = sb["Able Alice"], sb["Alpha Capital Partners LP"], sb["Offering Oscar"]
print("\n[10] v1.3:")
for r in (A, B, C):
    print(f"   {r['insider']:<26} score={r['score']:<3} flags={'offer ' if r['offering'] else ''}{'fund' if r['entity'] else ''}"
          f" pct_co={r.get('cluster_pct_co')} mcap={r.get('mcap')} off_high={r.get('off_high')} since={r.get('since')}")
print("   " + "\n   ".join(A["score_why"]))
check("context posted before done", "context" in kinds and kinds.index("context") < kinds.index("done"))
check("offering footnote flagged", C["offering"] and not A["offering"] and not B["offering"])
check("fund buyer flagged", B["entity"] and not A["entity"] and not C["entity"])
check("shares outstanding = latest filing", A["shares_out"] == 10_000_000)
check("this buy % of company", abs(A["pct_co"] - 0.2) < 1e-9)
check("discretionary cluster excludes offering buy", sclusters[f"{SIG}|P"][:2] == (3, 2))
check("cluster % of company (all 3 buys)", abs(A["cluster_pct_co"] - 0.8) < 1e-9)
check("market cap = shares x last close", abs(A["mcap"] - 180_000_000) < 1)
check("50% off 52-week high at buy", abs(A["off_high"] - (-50.0)) < 1e-6)
check("3-month change before buy", A["chg3m"] is not None and abs(A["chg3m"] - (-50.0)) < 1e-6)
check("+20% since insider's price", abs(A["since"] - 20.0) < 1e-6)
check("offering buyer since-buy uses own price ($12 -> $18 = +50%)", abs(C["since"] - 50.0) < 1e-6)
check("net insider flow 90d", A["net90"]["sell"] == 6_000_000 and A["net90"]["buyers"] == 3 and A["net90"]["sellers"] == 1)
check("13D in last 90 days (old one ignored)", A["d13"] == 1 and A["d13_last"] == D13[0])
check("Yahoo + XBRL requested", any("finance.yahoo.com" in u for u in CALLS) and any("companyconcept" in u for u in CALLS))
check("score: CEO buy ranks above fund and offering buys", A["score"] > B["score"] and A["score"] > C["score"])
check("score reasons mention CEO, 13D, heavy sellers",
      any("CEO" in w for w in A["score_why"]) and any("13D" in w for w in A["score_why"])
      and any("net sellers" in w for w in A["score_why"]))
check("score penalises offering", any("offering" in w for w in C["score_why"]))
check("offering buy capped at 30", C["score"] <= 30 and any(w.startswith("capped at 30") for w in C["score_why"]))

out = F.apply_signal_filters([dict(r) for r in srows if r["code"] == "P"], sclusters, False, 0, 0,
                             exclude_offering=True, exclude_entity=True)
check("exclude offering + funds", [r["insider"] for r in out] == ["Able Alice"])
out = F.apply_signal_filters([dict(r) for r in srows if r["code"] == "P"], sclusters, False, 0, 0, min_score=A["score"])
check("min score filter", [r["insider"] for r in out] == ["Able Alice"])
csvrow = F.row_to_csv(A)
check("CSV has score / mcap / 13D / reasons", csvrow[H("Signal Score")] == A["score"] and csvrow[H("13D Filings 90d")] == 1
      and "CEO" in csvrow[H("Score Reasons")] and len(csvrow) == len(F.CSV_HEADERS))

# unit checks
check("yahoo symbols", (F.yahoo_symbol("BRK.B"), F.yahoo_symbol("none"), F.yahoo_symbol("")) == ("BRK-B", None, None))
check("two share classes summed", F.latest_shares_outstanding({"units": {"shares": [
    {"end": "2026-06-30", "val": 100, "filed": "2026-08-01"}, {"end": "2026-06-30", "val": 40, "filed": "2026-08-01"},
    {"end": "2025-06-30", "val": 999, "filed": "2025-08-01"}]}}) == 140)
check("no shares data", F.latest_shares_outstanding({}) is None)
split = [("2026-01-02", 100.0, 50.0), ("2026-03-02", 60.0, 60.0)]          # 2:1 split after the buy
check("since-buy is split-safe", abs(F.price_metrics(split, dt.date(2026, 1, 2), 100.0)["since_buy"] - 20.0) < 1e-9)
check("bad Yahoo JSON -> None", F.parse_yahoo_chart(b"<html>") is None and F.parse_yahoo_chart(b'{"chart":{"result":[]}}') is None)
xml = (b'<ownershipDocument><schemaVersion>X0508</schemaVersion><issuer><issuerCik>1</issuerCik></issuer>'
       b'<reportingOwner><reportingOwnerId><rptOwnerCik>2</rptOwnerCik><rptOwnerName>X Y</rptOwnerName></reportingOwnerId>'
       b'</reportingOwner><remarks>Shares were acquired in a private placement.</remarks></ownershipDocument>')
check("remarks mark whole filing as offering", F.parse_ownership_xml(xml, {"acc": "x"})["offering_doc"] is True)
base = {"code": "P", "cluster": 1, "value": 50_000}
check("plain buy scores 40", F.signal_score(base)[0] == 40)
check("routine buy capped at 45", F.signal_score({**base, "cluster": 4, "roles_all": [F.ROLE_CEO], "value": 2e6,
                                                   "pattern_kind": "routine"})[0] == 45)
check("sells have no score", F.signal_score({"code": "S"})[0] is None)
check("score clamps 0..100", F.signal_score({**base, "offering": True, "plan": True, "pattern_kind": "routine",
                                              "sold_kind": "sold", "entity": True})[0] == 0
      and F.signal_score({**base, "cluster": 5, "roles_all": [F.ROLE_CEO], "value": 2e6, "stake": float("inf"),
                          "pattern_kind": "first", "cluster_pct_co": 2, "mcap": 1e8, "off_high": -60, "d13": 1})[0] == 100)
import tempfile as _tf
log = F.SignalLog(os.path.join(_tf.mkdtemp(), "t.sqlite3"))
n1 = log.log([dict(A, trade_start=str(D2)), dict(C, trade_start=str(D2)), {"code": "S", "acc": "z"}])
n2 = log.log([dict(A, score=1)])
items = log.all()
check("track record logs buys once (first score kept)", n1 == 2 and n2 == 0 and
      next(i for i in items if i["acc"] == A["acc"])["score"] == A["score"])
summ = F.track_record_summary([{"score": 80, "ret": 10.0, "excess": 4.0}, {"score": 75, "ret": -2.0, "excess": -5.0},
                               {"score": 40, "ret": 3.0, "excess": None}])
check("track record summary by band", summ["70+"]["n"] == 2 and abs(summ["70+"]["avg"] - 4.0) < 1e-9
      and summ["70+"]["beat"] == 50.0 and summ["50-69"] is None and summ["under 50"]["avg_excess"] is None)
log.clear()
check("track record clear", log.all() == [])
log.close()

# 11. v1.4 live signals: related-owner duplicates, insider record, earnings, fundamentals, liquidity, late
ERN = 888101
E_FIL = {}
_e = [0]


def add_ern(owner, txns, d):
    _e[0] += 1
    a = f"{ERN:010d}-26-{_e[0]:06d}"
    E_FIL[a] = (owner, txns, d)
    return a


XO = {"name": "Xavier Owens", "off": "1", "title": "CEO", "cik": "7001"}
FLP = {"name": "Big Fund LP", "ten": "1", "cik": "7003"}
FGP = {"name": "Big Fund GP LLC", "ten": "1", "cik": "7004"}
DIR1 = {"name": "Dana One", "dir": "1", "cik": "7005"}
DIR2 = {"name": "Evan Two", "dir": "1", "cik": "7006"}
add_ern(XO, [("P", 1000, 10.0, "A", 1000)], D2 - dt.timedelta(days=400))
add_ern(XO, [("P", 1000, 12.0, "A", 2000)], D2 - dt.timedelta(days=300))
XO_CUR = add_ern(XO, [("P", 10000, 12.0, "A", 12000)], D2)
FLP_ACC = add_ern(FLP, [("P", 50000, 12.0, "A", 950000), ("P", 25000, 12.1, "A", 975000)], D2)
FGP_ACC = add_ern(FGP, [("P", 50000, 12.0, "A", 950000), ("P", 25000, 12.1, "A", 975000)], D2)
D1_ACC = add_ern(DIR1, [("P", 1000, 25.0, "A", 5000)], D1)
D2_ACC = add_ern(DIR2, [("P", 1000, 25.0, "A", 3000)], D1)
for a_, (owner, txns, d) in E_FIL.items():
    RESP[f"{F.SEC_WWW}/Archives/edgar/data/{ERN}/{a_}.txt"] = form4(a_, ERN, "Earnings Co", "ERNC", [owner], txns, d)
EARN_DATE = D2 - dt.timedelta(days=10)


def ern_subs(accs, eight_ks=()):
    accs = sorted(accs, key=lambda a: E_FIL[a][2], reverse=True)
    return json.dumps({"filings": {"recent": {
        "accessionNumber": accs + [k[1] for k in eight_ks],
        "filingDate": [str(E_FIL[a][2]) for a in accs] + [k[0] for k in eight_ks],
        "form": ["4"] * len(accs) + ["8-K"] * len(eight_ks),
        "items": [""] * len(accs) + [k[2] for k in eight_ks],
        "primaryDocument": ["x"] * (len(accs) + len(eight_ks))}, "files": []}}).encode()


RESP[f"{F.SEC_DATA}/submissions/CIK{ERN:010d}.json"] = ern_subs(
    list(E_FIL), [(str(EARN_DATE), "0000888101-26-900001", "2.02,9.01"),
                  (str(D2 - dt.timedelta(days=40)), "0000888101-26-900002", "5.02")])
for o in (XO, FLP, FGP, DIR1, DIR2):
    RESP[f"{F.SEC_DATA}/submissions/CIK{int(o['cik']):010d}.json"] = ern_subs([a_ for a_, v in E_FIL.items() if v[0] is o])
CC = f"{F.SEC_DATA}/api/xbrl/companyconcept/CIK{ERN:010d}"
RESP[f"{CC}/dei/EntityCommonStockSharesOutstanding.json"] = json.dumps(
    {"units": {"shares": [{"end": "2026-09-30", "val": 10_000_000, "filed": "2026-10-30"}]}}).encode()
RESP[f"{CC}/us-gaap/StockholdersEquity.json"] = json.dumps(
    {"units": {"USD": [{"end": "2026-09-30", "val": 60_000_000, "filed": "2026-10-30"}]}}).encode()
RESP[f"{CC}/us-gaap/CashAndCashEquivalentsAtCarryingValue.json"] = json.dumps(
    {"units": {"USD": [{"end": "2026-09-30", "val": 5_000_000, "filed": "2026-10-30"}]}}).encode()
RESP[f"{CC}/us-gaap/NetCashProvidedByUsedInOperatingActivities.json"] = json.dumps(
    {"units": {"USD": [{"start": "2025-01-01", "end": "2025-12-31", "val": -8_000_000, "filed": "2026-02-20"},
                       {"start": "2026-01-01", "end": "2026-09-30", "val": -1_000_000, "filed": "2026-10-30"}]}}).encode()


def yjson(points, vol=10_000):
    ts = [int(dt.datetime(d.year, d.month, d.day, 14, 30, tzinfo=dt.timezone.utc).timestamp()) for d, _ in points]
    cl = [c for _, c in points]
    return json.dumps({"chart": {"result": [{"meta": {"gmtoffset": -18000}, "timestamp": ts,
                       "indicators": {"quote": [{"close": cl, "volume": [vol] * len(cl)}], "adjclose": [{"adjclose": cl}]}}],
                       "error": None}}).encode()


def series(fn, days=900):
    out, d = [], TODAY - dt.timedelta(days=days)
    while d <= TODAY:
        if d.weekday() < 5:
            out.append((d, fn(d)))
        d += dt.timedelta(days=1)
    return out


ern_px = series(lambda d: 10.0 if d < D2 - dt.timedelta(days=350) else (15.0 if d < EARN_DATE else 12.0))
YURL = "https://query1.finance.yahoo.com/v8/finance/chart/{}?range=10y&interval=1d&includeAdjustedClose=true"
RESP[YURL.format("ERNC")] = yjson(ern_px)
RESP[YURL.format("SPY")] = yjson(series(lambda d: 400.0), vol=1_000_000)
tk_ = json.loads(RESP[f"{F.SEC_WWW}/files/company_tickers.json"])
tk_["11"] = {"cik_str": ERN, "ticker": "ERNC", "title": "Earnings Co"}
RESP[f"{F.SEC_WWW}/files/company_tickers.json"] = json.dumps(tk_).encode()
F.Cache.clear_all()

q = queue.Queue()
F.Scanner(P(tickers=["ERNC"], codes={"P"}, min_value=0), q, threading.Event()).run()
erows, einfo, eclu, edups = [], {}, {}, {}
while not q.empty():
    _, kind, payload = q.get()
    if kind == "rows":
        erows.extend(payload)
    elif kind in ("history", "context"):
        for acc, info in payload.items():
            einfo.setdefault(acc, {}).update(info)
    elif kind == "clusters":
        eclu.update(payload)
    elif kind == "dups":
        edups.update(payload)
for r in erows:
    r.update(einfo.get(r["acc"], {}))
    r["dup_of"] = edups.get(r["acc"], "")
F.apply_signal_filters(erows, eclu, False, 0, 0)
eb = {r["insider"]: r for r in erows}
X = eb["Xavier Owens"]
print("\n[11] v1.4 live:")
print("   dups:", edups, " cluster:", eclu.get(f"{ERN}|P", ())[:2])
print(f"   Xavier: ins_rec={X.get('ins_rec')} earn={X.get('earn_days')}d {X.get('earn_move')} bm={X.get('bm')} "
      f"low_cash={X.get('low_cash')} dvol={X.get('dollar_vol')} score={X['score']}")
print("   " + "\n   ".join(X["score_why"]))
check("related fund GP filing marked duplicate", set(edups) <= {FLP_ACC, FGP_ACC} and len(edups) == 1)
check("unrelated directors with identical lots NOT merged", D1_ACC not in edups and D2_ACC not in edups)
check("cluster counts the fund purchase once", eclu[f"{ERN}|P"][:2] == (4, 4))
dup_row = eb["Big Fund GP LLC"] if FGP_ACC in edups else eb["Big Fund LP"]
check("duplicate row capped at 20", dup_row["score"] <= 20 and any("related owner" in w for w in dup_row["score_why"]))
check("insider record: 2 past buys, +37.5% vs SPY", X["ins_rec"] and X["ins_rec"]["n"] == 2
      and abs(X["ins_rec"]["avg_ex"] - 37.5) < 1e-6)
check("insider record factor", any("past buys beat SPY" in w for w in X["score_why"]))
check("earnings: 10 days after a -20% report", X["earn_days"] == 10 and abs(X["earn_move"] - (-20.0)) < 1e-6
      and any("after earnings" in w for w in X["score_why"]))
check("non-earnings 8-K ignored", X["earn_date"] == str(EARN_DATE))
check("book/market 0.5", abs(X["bm"] - 0.5) < 1e-9 and any("book/market" in w for w in X["score_why"]))
check("low cash flagged (annual burn > cash)", X["low_cash"] is True and any("cash" in w for w in X["score_why"]))
check("thin trading flagged (20-day avg $ volume)", 120_000 < X["dollar_vol"] < 150_000
      and any("thinly traded" in w for w in X["score_why"]))
check("late filing: none", X["late_days"] == 0)
check("late business days", (F.late_business_days("2026-11-02", "2026-11-04"), F.late_business_days("2026-11-02", "2026-11-20"),
                             F.late_business_days("2026-11-06", "2026-11-10")) == (0, 12, 0))
check("late factor", any("late" in w for w in F.signal_score({"code": "P", "cluster": 1, "late_days": 12})[1]))
check("weights adjustable", (F.set_weights({"ceo_cfo": 20, "bogus": 5}), F.signal_score({"code": "P", "cluster": 1,
      "roles_all": [F.ROLE_CEO]})[0])[1] == 60 and F.WEIGHTS["ceo_cfo"] == 20)
F.set_weights(None)
check("weights reset", F.WEIGHTS == F.DEFAULT_WEIGHTS)
check("forward return needs elapsed time", F.forward_return([("2026-01-02", 10, 10, 0)], dt.date(2026, 1, 2), 30) is None)
check("fact_value annual picks 12-month period",
      F.fact_value(json.loads(RESP[f"{CC}/us-gaap/NetCashProvidedByUsedInOperatingActivities.json"])["units"]["USD"],
                   annual=True) == -8_000_000)
csvrow = F.row_to_csv(X)
check("CSV has insider record / earnings / late", csvrow[H("Insider Past Buys Measured")] == 2
      and csvrow[H("Days Since Earnings")] == 10 and len(csvrow) == len(F.CSV_HEADERS))

# 12. Back-test on synthetic SEC quarterly data sets
import io as _io
import zipfile as _zip

BT_COLS = {
    "SUBMISSION": ["ACCESSION_NUMBER", "FILING_DATE", "PERIOD_OF_REPORT", "DATE_OF_ORIG_SUB", "NO_SECURITIES_OWNED",
                   "NOT_SUBJECT_SEC16", "FORM3_HOLDING_REPORTED", "FORM4_TRANS_REPORTED", "DOCUMENT_TYPE", "ISSUERCIK",
                   "ISSUERNAME", "ISSUERTRADINGSYMBOL", "REMARKS", "AFF10B5ONE"],
    "REPORTINGOWNER": ["ACCESSION_NUMBER", "RPTOWNERCIK", "RPTOWNERNAME", "RPTOWNER_RELATIONSHIP", "RPTOWNER_TITLE",
                       "RPTOWNER_TXT", "RPTOWNER_STREET1", "RPTOWNER_STREET2", "RPTOWNER_CITY", "RPTOWNER_STATE",
                       "RPTOWNER_ZIPCODE", "RPTOWNER_STATE_DESC", "FILE_NUMBER"],
    "NONDERIV_TRANS": ["ACCESSION_NUMBER", "NONDERIV_TRANS_SK", "SECURITY_TITLE", "SECURITY_TITLE_FN", "TRANS_DATE",
                       "TRANS_DATE_FN", "DEEMED_EXECUTION_DATE", "DEEMED_EXECUTION_DATE_FN", "TRANS_FORM_TYPE", "TRANS_CODE",
                       "EQUITY_SWAP_INVOLVED", "EQUITY_SWAP_TRANS_CD_FN", "TRANS_TIMELINESS", "TRANS_TIMELINESS_FN",
                       "TRANS_SHARES", "TRANS_SHARES_FN", "TRANS_PRICEPERSHARE", "TRANS_PRICEPERSHARE_FN",
                       "TRANS_ACQUIRED_DISP_CD", "TRANS_ACQUIRED_DISP_CD_FN", "SHRS_OWND_FOLWNG_TRANS",
                       "SHRS_OWND_FOLWNG_TRANS_FN", "VALU_OWND_FOLWNG_TRANS", "VALU_OWND_FOLWNG_TRANS_FN",
                       "DIRECT_INDIRECT_OWNERSHIP", "DIRECT_INDIRECT_OWNERSHIP_FN", "NATURE_OF_OWNERSHIP",
                       "NATURE_OF_OWNERSHIP_FN"],
    "FOOTNOTES": ["ACCESSION_NUMBER", "FOOTNOTE_ID", "FOOTNOTE_TXT"],
}


def sec_date(d):
    return d.strftime("%d-%b-%Y").upper()


class BulkQ:
    def __init__(self):
        self.t = {k: [] for k in BT_COLS}
        self.sk = 0

    def filing(self, acc, cik, ticker, owner, rel, title, lots, filed, fn=None, form="4", remarks=""):
        self.t["SUBMISSION"].append({"ACCESSION_NUMBER": acc, "FILING_DATE": sec_date(filed), "DOCUMENT_TYPE": form,
                                     "ISSUERCIK": f"{cik:010d}", "ISSUERNAME": f"Co {ticker}",
                                     "ISSUERTRADINGSYMBOL": ticker, "REMARKS": remarks, "AFF10B5ONE": "0"})
        self.t["REPORTINGOWNER"].append({"ACCESSION_NUMBER": acc, "RPTOWNERCIK": owner[1], "RPTOWNERNAME": owner[0],
                                         "RPTOWNER_RELATIONSHIP": rel, "RPTOWNER_TITLE": title})
        for code, sh, px, td, after in lots:
            self.sk += 1
            self.t["NONDERIV_TRANS"].append({"ACCESSION_NUMBER": acc, "NONDERIV_TRANS_SK": str(self.sk),
                                             "SECURITY_TITLE": "Common Stock", "TRANS_DATE": sec_date(td),
                                             "TRANS_CODE": code, "TRANS_SHARES": str(sh), "TRANS_PRICEPERSHARE": str(px),
                                             "TRANS_SHARES_FN": fn or "", "TRANS_ACQUIRED_DISP_CD": "A" if code == "P" else "D",
                                             "SHRS_OWND_FOLWNG_TRANS": str(after), "DIRECT_INDIRECT_OWNERSHIP": "D"})
        if fn:
            self.t["FOOTNOTES"].append({"ACCESSION_NUMBER": acc, "FOOTNOTE_ID": fn,
                                        "FOOTNOTE_TXT": "Purchased in the underwritten public offering."})

    def zip(self):
        buf = _io.BytesIO()
        with _zip.ZipFile(buf, "w") as z:
            for name, cols in BT_COLS.items():
                lines = ["\t".join(cols)] + ["\t".join(r.get(c, "") for c in cols) for r in self.t[name]]
                z.writestr(f"{name}.tsv", "\n".join(lines) + "\n")
            z.writestr("FORM345_metadata.json", "{}")
        return buf.getvalue()


BQ = {q: BulkQ() for q in ("2024q3", "2024q4", "2025q1", "2025q2")}
n_acc = [0]


def bacc():
    n_acc[0] += 1
    return f"0009{n_acc[0]:06d}-25-{n_acc[0]:06d}"


EXPECT = {"signals": 0, "cluster_rows": 0}
for i in range(60):
    q_ = "2025q1" if i < 30 else "2025q2"
    base = dt.date(2025, 1, 6) if i < 30 else dt.date(2025, 4, 7)
    d = base + dt.timedelta(days=(i % 30) * 2)
    cik, tic = 900000 + i, f"T{i:03d}"
    BQ[q_].filing(bacc(), cik, tic, (f"Person{i} Alpha", f"{50000 + i}"), "Officer", "CEO",
                  [("P", 10000, 10.0, d, 20000)], d)
    EXPECT["signals"] += 1
    if i % 2 == 0:            # a second insider joins within days -> cluster; these stocks go up 20%
        BQ[q_].filing(bacc(), cik, tic, (f"Other{i} Beta", f"{60000 + i}"), "Director", "",
                      [("P", 10000, 10.0, d + dt.timedelta(days=2), 15000)], d + dt.timedelta(days=2))
        EXPECT["signals"] += 1
    if i == 7:                # a sale in the warm-up period by another insider
        BQ["2024q4"].filing(bacc(), cik, tic, ("Seller Seven", "70007"), "Officer", "CFO",
                            [("S", 5000, 9.0, dt.date(2024, 11, 4), 1000)], dt.date(2024, 11, 5))
# fund + GP report the same purchase separately (the GP's copy must be dropped)
BQ["2025q1"].filing(bacc(), 900100, "T100", ("Delta Fund LP", "80001"), "TenPercentOwner", "",
                    [("P", 40000, 10.0, dt.date(2025, 2, 3), 400000), ("P", 10000, 10.2, dt.date(2025, 2, 4), 410000)],
                    dt.date(2025, 2, 5))
BQ["2025q1"].filing(bacc(), 900100, "T100", ("Delta Fund GP LLC", "80002"), "TenPercentOwner", "",
                    [("P", 40000, 10.0, dt.date(2025, 2, 3), 400000), ("P", 10000, 10.2, dt.date(2025, 2, 4), 410000)],
                    dt.date(2025, 2, 5))
EXPECT["signals"] += 1
# an offering buy (footnote) and a Form 3 that must be ignored
BQ["2025q2"].filing(bacc(), 900101, "T101", ("Olga Offer", "80003"), "Director", "",
                    [("P", 20000, 10.0, dt.date(2025, 5, 5), 20000)], dt.date(2025, 5, 6), fn="F1")
EXPECT["signals"] += 1
BQ["2025q2"].filing(bacc(), 900102, "T102", ("New Guy", "80004"), "Officer", "CEO",
                    [("P", 99999, 10.0, dt.date(2025, 5, 5), 99999)], dt.date(2025, 5, 6), form="3")
# warm-up: an earlier buy by Person1 (pattern = opportunistic)
BQ["2024q3"].filing(bacc(), 900001, "T001", ("Person1 Alpha", "50001"), "Officer", "CEO",
                    [("P", 1000, 8.0, dt.date(2024, 8, 1), 10000)], dt.date(2024, 8, 2))
# look-ahead guard: T005's second buyer trades in the window but files 40 days later
BQ["2025q1"].filing(bacc(), 900005, "T005", ("Late Larry", "80005"), "Director", "",
                    [("P", 10000, 10.0, dt.date(2025, 1, 17), 10000)], dt.date(2025, 2, 28))
EXPECT["signals"] += 1

# sales for the Sell Score back-test: big sales (60% of holding) precede a 20% drop, small ones don't
for i in range(40):
    q_ = "2025q1" if i < 20 else "2025q2"
    base = dt.date(2025, 1, 6) if i < 20 else dt.date(2025, 4, 7)
    d = base + dt.timedelta(days=(i % 20) * 2)
    big = i % 2 == 0
    BQ[q_].filing(bacc(), 900200 + i, f"T{200 + i}", (f"Seller{i} Gamma", f"{70100 + i}"), "Officer", "CEO",
                  [("S", 30000 if big else 1000, 10.0, d, 20000 if big else 49000)], d)

for q_, bq in BQ.items():
    url = F.BULK_URLS[0 if q_ >= "2025q1" else 1].format(q=q_)      # older quarters only at the second address
    RESP[url] = bq.zip()
for i in list(range(60)) + [100, 101]:
    tic, up = f"T{i:03d}", (i % 2 == 0 and i < 60)
    RESP[YURL.format(tic)] = yjson(series(lambda d, up=up: 12.0 if (up and d > dt.date(2025, 6, 30) or
                                                                    up and d.year == 2025 and d.month in (2, 3, 5, 6)
                                                                    and False) else 10.0, days=700),
                                   vol=100_000)
# rising stocks: +20% three months after each filing
for i in range(0, 60, 2):
    tic = f"T{i:03d}"
    base = dt.date(2025, 1, 6) if i < 30 else dt.date(2025, 4, 7)
    jump = base + dt.timedelta(days=(i % 30) * 2 + 20)
    RESP[YURL.format(tic)] = yjson(series(lambda d, j=jump: 12.0 if d >= j else 10.0, days=700), vol=100_000)
for i in range(40):
    base = dt.date(2025, 1, 6) if i < 20 else dt.date(2025, 4, 7)
    drop = base + dt.timedelta(days=(i % 20) * 2 + 20)
    RESP[YURL.format(f"T{200 + i}")] = yjson(series(lambda d, j=drop, big=(i % 2 == 0): 8.0 if (big and d >= j) else 10.0,
                                                    days=700), vol=100_000)
for per in {F.frame_period_instant(dt.date(2025, m, 15)) for m in range(1, 13)}:
    RESP[f"{F.SEC_DATA}/api/xbrl/frames/dei/EntityCommonStockSharesOutstanding/shares/{per}.json"] = json.dumps(
        {"data": [{"cik": 900000 + i, "val": 1_000_000} for i in range(103)]}).encode()

CALLS.clear()
bq_ = queue.Queue()
F.Backtester(F.BacktestParams("2025q1", "2025q2", "Test test@example.com", warmup_quarters=2, min_value=50_000,
                              max_signals=0, company_data=True), bq_, threading.Event()).run()
res, berr = None, None
while not bq_.empty():
    _, kind, payload = bq_.get()
    if kind == "bt_done":
        res = payload
    elif kind == "error":
        berr = payload
print("\n[12] back-test:", berr or {k: res[k] for k in ("filings", "candidates", "signals", "priced6", "dups", "missing_quarters")})
check("back-test ran", res is not None)
rows_bt = {(r["ticker"], r["insider"]): r for r in res["rows"]}
cl = next(f for f in res["factors"] if f["key"] == "cluster2")
print(f"   cluster2: n={cl['n_with']} with={cl['with']:.1f} without={cl['without']:.1f} suggested={cl['suggested']}")
print("   bands:", {b: (v["n"], None if v["ex6M"] is None else round(v["ex6M"], 1)) for b, v in res["bands"].items()})
check("bulk files: both SEC addresses tried, none missing", res["missing_quarters"] == [])
check("Form 3 ignored; all expected signals", res["signals"] == EXPECT["signals"])
check("fund GP duplicate removed", res["dups"] == 1 and ("T100", "Delta Fund GP LLC") not in rows_bt)
check("offering footnote parsed", rows_bt[("T101", "Olga Offer")]["offering"] is True)
check("6-month results for all priced", res["priced6"] == res["signals"])
check("cluster rows +20% vs SPY", abs(rows_bt[("T000", "Person0 Alpha")]["ex6M"] - 20.0) < 1e-6)
# first buyers in rising stocks weren't a cluster yet when they filed (no look-ahead), so uplift ~ +10
check("cluster factor uplift measured -> suggested points", cl["uplift"] > 5 and cl["suggested"] == round(cl["uplift"]))
check("weights only suggested where the training half has 30+ signals each way",
      "cluster2" not in res["suggested"] and all(f["n_with"] >= 30 for f in F.factor_stats(
          [r for r in res["rows"] if r.get("ex6M") is not None][:res["priced6"] // 2]) if f["enough"]))
check("no look-ahead: late-filed buyer not in earlier cluster", rows_bt[("T005", "Person5 Alpha")]["cluster"] == 1)
check("pattern from warm-up history", rows_bt[("T001", "Person1 Alpha")]["pattern_kind"] == "opp")
check("net flow includes a warm-up-period sale within 90 days", rows_bt[("T007", "Person7 Alpha")]["net90"]["sell"] == 45000
      and rows_bt[("T009", "Person9 Alpha")]["net90"]["sell"] == 0)
check("market cap from XBRL frame", abs(rows_bt[("T002", "Person2 Alpha")]["mcap"] - 10_000_000) < 1)
check("train/test results present", res["test_current"]["All"]["n"] > 0 and res["split_date"])
check("frames requested", any("/api/xbrl/frames/" in u for u in CALLS))
check("bulk zip cached on disk", os.path.exists(os.path.join(F.app_data_dir(), "bulk", "2025q1_form345.zip")))
CALLS.clear()
bq_ = queue.Queue()
F.Backtester(F.BacktestParams("2025q1", "2025q2", "Test test@example.com", warmup_quarters=2, min_value=50_000,
                              max_signals=10, company_data=False), bq_, threading.Event()).run()
res2 = next(p_ for _, k, p_ in list(bq_.queue) if k == "bt_done")
check("re-run uses cached zips & prices; sample size respected",
      not any("insider-transactions" in u or "finance.yahoo" in u for u in CALLS) and res2["signals"] == 10)
check("bt CSV row", len(F.bt_row_csv(res["rows"][0])) == len(F.BT_CSV_HEADERS))
check("quarter helpers", (F.shift_quarter("2025q1", -2), F.quarter_range("2024q4", "2025q2"),
                          F.quarter_bounds("2024q4")[1], F.bulk_date("05-JAN-2024")) ==
      ("2024q3", ["2024q4", "2025q1", "2025q2"], dt.date(2024, 12, 31), "2024-01-05"))
check("default back-test period", F.default_backtest_quarters(dt.date(2026, 11, 20)) == ("2025q2", "2026q1"))

# 13. v1.5 Sell Score: live scan of company SELC + back-test of sales
SLC = 555777
L_FIL = {}
_l = [0]


def add_sl(owner, txns, d, plan=False):
    _l[0] += 1
    a = f"{SLC:010d}-26-{_l[0]:06d}"
    L_FIL[a] = (owner, txns, d, plan)
    return a


F5N = '<footnoteId id="F5"/>'
SAM = {"name": "Ceo Sam", "off": "1", "title": "Chief Executive Officer", "cik": "8001"}
RIT = {"name": "Dir Rita", "dir": "1", "cik": "8002"}
TAX = {"name": "Cfo Tax", "off": "1", "title": "CFO", "cik": "8003"}
EXE = {"name": "Exer Ed", "off": "1", "title": "COO", "cik": "8004"}
PAT = {"name": "Plan Pat", "off": "1", "title": "President", "cik": "8005"}
for k in (1, 2, 3):
    add_sl(RIT, [("S", 5000, 30.0, "D", 100000 + 5000 * (4 - k))], F._years_ago(D2, k) + dt.timedelta(days=3))
SAM_ACC = add_sl(SAM, [("S", 60000, 50.0, "D", 40000)], D2)
add_sl(RIT, [("S", 10000, 50.0, "D", 90000)], D2)
add_sl(TAX, [("S", 20000, 50.0, "D", 10000, F5N)], D2)
add_sl(EXE, [("M", 30000, 10.0, "A", 30000), ("S", 30000, 50.0, "D", 0)], D2)
add_sl(PAT, [("S", 8000, 50.0, "D", 2000)], D2, plan=True)
for a_, (owner, txns, d, plan) in L_FIL.items():
    RESP[f"{F.SEC_WWW}/Archives/edgar/data/{SLC}/{a_}.txt"] = form4(a_, SLC, "Sell Co", "SELC", [owner], txns, d, plan=plan)


def sl_subs(accs):
    accs = sorted(accs, key=lambda a: L_FIL[a][2], reverse=True)
    return json.dumps({"filings": {"recent": {"accessionNumber": accs, "filingDate": [str(L_FIL[a][2]) for a in accs],
                                              "form": ["4"] * len(accs), "primaryDocument": ["x"] * len(accs)},
                                   "files": []}}).encode()


RESP[f"{F.SEC_DATA}/submissions/CIK{SLC:010d}.json"] = sl_subs(list(L_FIL))
for o in (SAM, RIT, TAX, EXE, PAT):
    RESP[f"{F.SEC_DATA}/submissions/CIK{int(o['cik']):010d}.json"] = sl_subs([a_ for a_, v in L_FIL.items() if v[0] is o])
RESP[YURL.format("SELC")] = yjson(series(lambda d: 30.0 if d < D2 - dt.timedelta(days=85) else 50.0), vol=500_000)
tk2 = json.loads(RESP[f"{F.SEC_WWW}/files/company_tickers.json"])
tk2["12"] = {"cik_str": SLC, "ticker": "SELC", "title": "Sell Co"}
RESP[f"{F.SEC_WWW}/files/company_tickers.json"] = json.dumps(tk2).encode()
F.Cache.clear_all()

CALLS.clear()
q = queue.Queue()
F.Scanner(P(tickers=["SELC"], codes={"S"}, min_value=0), q, threading.Event()).run()
lrows, linfo, lclu = [], {}, {}
while not q.empty():
    _, kind, payload = q.get()
    if kind == "rows":
        lrows.extend(payload)
    elif kind in ("history", "context"):
        for k_, info in payload.items():
            linfo.setdefault(k_, {}).update(info)
    elif kind == "clusters":
        lclu.update(payload)
for r in lrows:
    r.update(linfo.get(r["hkey"], {}))
F.apply_signal_filters(lrows, lclu, False, 0, 0)
cur = {r["insider"]: r for r in lrows if r["trade_start"] == str(D2)}
print("\n[13] sells:")
for n_, r in sorted(cur.items(), key=lambda x: -(x[1]["score"] or 0)):
    print(f"   {n_:<10} score={r['score']:<3} stake={F.fmt_stake(r['stake']):>7} deep={r['deep']} pattern={r.get('pattern')!s:<34}"
          f" flags={'expl ' if r['explained'] else ''}{'ex&sell ' if r['exercise_sell'] else ''}{'plan' if r['plan'] else ''}")
SA = cur["Ceo Sam"]
print("   " + "\n   ".join(SA["score_why"]))
check("sale rows keyed separately (hkey)", SA["hkey"] == f"{SAM_ACC}|S")
check("big discretionary CEO sale after a run-up scores highest", SA["score"] >= 80
      and SA["score"] == max(r["score"] for r in cur.values()))
check("first sale in 5+ years (seller history)", SA["pattern"] == "First sale in 5+ yrs" and SA["pattern_kind"] == "first")
check("run-up + near 52w high factors", any("run-up" in w or "3 months before the sale" in w for w in SA["score_why"])
      and any("52-week high" in w for w in SA["score_why"]))
check("routine yearly seller capped at 45", cur["Dir Rita"]["pattern_kind"] == "routine" and cur["Dir Rita"]["score"] <= 45)
check("tax-withholding sale explained, capped", cur["Cfo Tax"]["explained"] and cur["Cfo Tax"]["score"] <= 45)
check("exercise-and-sell detected, capped at 40", cur["Exer Ed"]["exercise_sell"] and cur["Exer Ed"]["score"] <= 40)
check("10b5-1 plan sale: no deep check, capped", cur["Plan Pat"]["plan"] and not cur["Plan Pat"]["deep"]
      and cur["Plan Pat"]["score"] <= 45)
check("sell rows never show 'Sold Since'", all(r["sold_kind"] == "na" for r in lrows))
check("seller history fetched for significant sales", any(f"CIK{8001:010d}" in u for u in CALLS)
      and not any(f"CIK{8005:010d}" in u for u in CALLS))
check("explained-sale wording", [bool(F.EXPLAINED_RE.search(t)) for t in (
    "Shares withheld to satisfy tax withholding obligations upon vesting of RSUs.",
    "Sale effected for estate planning purposes.", "Weighted average price; prices ranged from $50.01 to $50.40.",
    "Shares sold to fund the purchase of a residence.")] == [True, True, False, True])
check("sell score helpers", F.sell_score({"code": "P"})[0] is None and F.rate_row({"code": "S", "stake": -100})[0] > 30
      and F.sell_score({"code": "S", "stake": -2.0})[0] < 30 + 8 + 1)
log = F.SignalLog(os.path.join(_tf.mkdtemp(), "t2.sqlite3"))
nl = log.log([SA, cur["Dir Rita"]])
items = log.all()
check("track record logs sales with side S", nl == 2 and all(i["side"] == "S" for i in items)
      and items[0]["acc"].endswith("|S"))
summ = F.track_record_summary([{"side": "S", "score": 80, "ret": -12.0, "excess": -15.0},
                               {"side": "S", "score": 75, "ret": 5.0, "excess": 2.0},
                               {"side": "P", "score": 90, "ret": 9.0, "excess": 3.0}], "S")
check("sell summary counts lagging SPY as success", summ["70+"]["n"] == 2 and summ["70+"]["beat"] == 50.0)
log.close()
csvrow = F.row_to_csv(SA)
check("CSV for sales", csvrow[H("Signal Score")] == SA["score"] and len(csvrow) == len(F.CSV_HEADERS))

bq_ = queue.Queue()
F.Backtester(F.BacktestParams("2025q1", "2025q2", "Test test@example.com", warmup_quarters=2, min_value=5_000,
                              max_signals=0, company_data=True, side="S"), bq_, threading.Event()).run()
rs = next((p_ for _, k, p_ in list(bq_.queue) if k == "bt_done"), None)
err = next((p_ for _, k, p_ in list(bq_.queue) if k == "error"), None)
print("   sell back-test:", err or {b: (v["n"], None if v["ex6M"] is None else round(v["ex6M"], 1), v["hit6"])
                                   for b, v in rs["bands"].items()})
check("sell back-test ran", rs is not None and rs["params"]["side"] == "S" and rs["signals"] >= 40)
big = next(f for f in rs["factors"] if f["key"] == "s_pct50")
check("big-sale factor: -20% vs SPY difference", abs(big["uplift"] - (-20.0)) < 1.0)
check("sell bands: high scores lagged SPY", rs["bands"]["70+"]["ex6M"] is not None and rs["bands"]["70+"]["ex6M"] < -15
      and rs["bands"]["70+"]["hit6"] == 100.0)
check("sell suggestion flips sign (worse return -> more points)",
      F.factor_stats([{"ex6M": -20.0, "factors": ["s_pct50"]}] * 30 + [{"ex6M": 0.0, "factors": []}] * 30,
                     side="S")[0]["suggested"] == 20)

# 14b. Sign-in (each user's own name and email)
check("sign-in: real name + email accepted", F.identity_problem("Pat Smith", "pat.smith@gmail.com") == "")
check("sign-in: blank name rejected", F.identity_problem("", "pat@gmail.com") != "")
check("sign-in: blank email rejected", F.identity_problem("Pat Smith", "") != "")
check("sign-in: bad email rejected", F.identity_problem("Pat Smith", "pat@gmail") != ""
      and F.identity_problem("Pat Smith", "pat smith@gmail.com") != "")
check("sign-in: example placeholders rejected", F.identity_problem("Jane Doe", "jd@gmail.com") != ""
      and F.identity_problem("Pat Smith", "jane@example.com") != "")
check("sign-in: email in name box rejected", F.identity_problem("pat@gmail.com", "pat@gmail.com") != "")
check("sign-in: User-Agent is 'Name email'", F.sec_user_agent("  Pat   Smith ", " pat@gmail.com ") == "Pat Smith pat@gmail.com")
check("sign-in: accents folded for the HTTP header", F.sec_user_agent("Zoë Łukasz", "z@gmail.com").isascii()
      and F.valid_user_agent(F.sec_user_agent("Zoë Łukasz", "z@gmail.com")))

# 14. Cancel
q = queue.Queue(); ev = threading.Event(); ev.set()
F.Scanner(P(), q, ev).run()
kinds = [k for _, k, _ in list(q.queue)]
check("cancel posts 'cancelled'", "cancelled" in kinds)

print("\nFAILURES:", fails)
sys.exit(1 if fails else 0)
