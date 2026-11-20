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
    [{"name": "Doe John", "off": "1", "title": "President and CEO"}], [("P", 10000, 20.0, "A")], TODAY)
ATOM = f"""<?xml version="1.0" encoding="ISO-8859-1" ?>
<feed xmlns="http://www.w3.org/2005/Atom">
<title>Latest Filings</title>
<entry>
<title>4 - Fresh Co (0000555555) (Issuer)</title>
<link rel="alternate" type="text/html" href="https://www.sec.gov/Archives/edgar/data/555555/{TODAY_ACC.replace('-', '')}/{TODAY_ACC}-index.htm"/>
<summary type="html"> &lt;b&gt;Filed:&lt;/b&gt; {TODAY} </summary>
<updated>{TODAY}T16:05:11-05:00</updated>
<category scheme="https://www.sec.gov/" label="form type" term="4"/>
</entry>
<entry>
<title>4 - Doe John (0000777777) (Reporting)</title>
<link rel="alternate" type="text/html" href="https://www.sec.gov/Archives/edgar/data/777777/{TODAY_ACC.replace('-', '')}/{TODAY_ACC}-index.htm"/>
<updated>{TODAY}T16:05:11-05:00</updated>
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
check("feed picked up today's filing", any(r["ticker"] == "FRSH" and r["filing_date"] == str(TODAY) for r in rows))

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
RESP["https://query1.finance.yahoo.com/v8/finance/chart/SIGL?range=2y&interval=1d&includeAdjustedClose=true"] = yahoo_json(pts)
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

# 11. Cancel
q = queue.Queue(); ev = threading.Event(); ev.set()
F.Scanner(P(), q, ev).run()
kinds = [k for _, k, _ in list(q.queue)]
check("cancel posts 'cancelled'", "cancelled" in kinds)

print("\nFAILURES:", fails)
sys.exit(1 if fails else 0)
