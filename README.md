# SEC Form 4 Insider Scanner

A Windows desktop app that scans SEC EDGAR Form 4 filings for insider buys and sells. It is designed to surface **true insider buying**: open-market purchases by several insiders at once that meaningfully increase their stakes and aren't pre-scheduled.

![icon](assets/app.png)

## Install (Windows)

1. Open the **[latest release](../../releases/latest)**.
2. Download **`Form4Scanner-Setup.exe`** and run it. It installs to your user folder, so no admin rights are needed, and adds a Start-menu shortcut (desktop shortcut optional).
   Prefer no installer? Download **`Form4Scanner.exe`** instead and double-click it.
3. The app isn't code-signed, so Windows SmartScreen may show "Windows protected your PC". Click **More info → Run anyway**.
4. The first time, type your **name and email** in the *SEC contact* box, for example `Jane Doe jane@example.com`. The SEC requires this on automated requests.

Every push to `main` rebuilds the app on GitHub Actions and replaces the *latest* release.

## Finding true insider buying

Click **★ True Insider Buying preset**, then **Start Scan**. The preset sets:

| Setting | Value | Why |
|---|---|---|
| Transactions | Open-market purchases (Code P) only | Insiders sell for many reasons but usually buy for one |
| Date range | Last 30 days | Gives clusters time to form |
| Roles | All insiders | Directors' buys are often as informative as executives' |
| Minimum $ | $100,000 | Filters token purchases |
| Exclude 10b5-1 | On | Pre-scheduled plan trades say little about current views |
| Clusters only | ≥ 2 different insiders | Several insiders buying together is a stronger signal |
| Min stake increase | +10% | The buy must meaningfully grow the insider's holding |
| Hide routine / regular buyers | On | Buys made on a schedule carry little information |
| Hide buyers who have sold since | On | A quick sale undercuts the signal |
| Exclude offering / private-placement buys | On | Buying in a company offering isn't an open-market bet |
| Exclude funds / companies as buyers | On | A fund's 10% stake is an investment bet, not inside knowledge |

Results are sorted by **Signal Score**, with the best first. The **Signal Filters** apply instantly to results you already have, so you can loosen or tighten them without re-scanning.

### Signal Score (0–100)
Every open-market buy gets a score. It starts at 40, then adds or subtracts points:

| Factor | Points |
|---|---|
| Cluster: 2 / 3 / 4+ different insiders buying | +12 / +18 / +22 |
| CEO or CFO is the buyer (or a director) | +8 (+3) |
| Purchase of $250k+ / $1M+ | +3 / +6 |
| Holding up 10% / 25% / 50% / new or doubled | +3 / +6 / +9 / +12 |
| First buy in 5+ yrs / Opportunistic / Regular / Routine | +10 / +5 / −12 / −20 |
| Insiders bought 0.05% / 0.25% / 1%+ of the company | +4 / +7 / +10 |
| Company under $300M / $2B / $10B; over $200B | +6 / +4 / +1; −3 |
| Bought 20% / 40%+ below the 52-week high | +4 / +7 |
| Stock fell 20%+ in the 3 months before the buy | +3 |
| Insiders net buyers over 90 days / heavy net sellers | +4 / −8 |
| Schedule 13D (5%+ activist stake) filed in last 90 days | +5 |
| Already up 30%+ since the purchase | −6 |
| Has sold since / 10b5-1 plan / offering buy / fund buyer | −10 to −25 / −15 / −25 / −15 |

Disqualifiers also **cap** the score: an offering buy is capped at 30, a 10b5-1 or routine buy at 45, and a buyer who has since sold 25%+ at 40. Right-click a row and choose **Why this score?** to see every point. The weights are a starting point based on published research and common practice, not a back-tested model. Use the Track Record to see how they hold up.

### Signal Track Record
*File → Signal Track Record* (Ctrl+T) lists every scored buy from your completed scans, each logged once with its score at the time. It shows the return from the insider's purchase price to today and **SPY over the same dates**, with a summary by score band: average return, average vs SPY, and % beating SPY. Clearing the download cache doesn't delete this history.

### Signal columns
* **Stake Δ %** shows how much the trade changed the insider's holding. It is calculated from *shares owned following transaction*, adding direct and each indirect holding (e.g. a trust) separately. **New** means a first-ever position.
* **Cluster** is the number of *different* insiders who made the same type of trade in that company during the scanned dates. It counts every size and role, not just the rows shown. With *Exclude 10b5-1* on, plan trades don't count toward it. Cluster buys are shown in **bold green**.
* **Flags**:
  * **10b5-1**: the filing ticks the Rule 10b5-1 checkbox, or a transaction footnote mentions a 10b5-1 plan (see below).
  * **Offering**: a footnote or the remarks say the shares were bought in an underwritten, public, registered-direct or rights offering, an IPO, or a private placement or PIPE.
  * **Fund**: the buyer's name looks like a fund or company (LP, LLC, Capital, Partners, Inc, etc.).
  * **13D**: someone filed a Schedule 13D (a 5%+ stake with intent to influence) on the company in the last 90 days.
* **10b5-1** shows *Yes* when the filing ticks the Rule 10b5-1 checkbox (added to Form 4 in 2023) or a footnote on the transaction says it was made under a 10b5-1 plan. Footnotes saying "*not* pursuant to a 10b5-1 plan" are ignored.

* **Pattern** describes the insider's earlier open-market buys in the same company over the last 5 years. It is found by matching the insider's Form 4 list against the company's on EDGAR.
  * *First buy in 5+ yrs*: no earlier purchases.
  * *Opportunistic*: has bought before, with no schedule.
  * *Regular*: bought in 3 or more of the last 4 quarters, or around the same time in 2 of the last 3 years.
  * *Routine*: bought around the same time of year (±3 weeks) in each of the last 3 years. This follows Cohen, Malloy & Pomorski's definition of routine traders, whose trades carried essentially no predictive power.
  * Buys within 30 days before the current one count as the same buying spree.
* **Sold Since** shows open-market sales (Code S) by the same insider in the same company on or after the purchase date, as a % of what they held right after buying. Shares withheld for taxes when stock vests (Code F) are labelled *tax w/h only* and are not counted as a sale. Rows where the buyer has since sold are shown in **orange**, and routine or regular buyers in grey. Later sales only appear once filed, so re-scan later to refresh.
* **% of Co.** shows the shares the whole cluster bought as a % of shares outstanding, from SEC XBRL data. **Mkt Cap** = shares outstanding × latest close.
* **Off 52w Hi** is how far below its 52-week high the stock was on the purchase date. **3M Before** is the 3-month move before the buy (hidden by default). **Since Buy** is the change from the insider's price to the latest close, adjusted for splits and dividends.
* **Insiders 90d** shows open-market buying minus selling by *all* of the company's insiders over the last 90 days (B = buyers, S = sellers).
* Prices come from Yahoo Finance's public chart data (no key needed). If Yahoo is unavailable, the price columns stay blank and the score simply leaves those points out.
* **Deep check buyers** runs after the main scan, for buy rows only. It covers Pattern, Sold Since, company data and prices. It adds a few minutes the first time and is cached afterwards. Untick it for a fast scan.
* Right-click a column header to show or hide columns.

This is a research filter, not investment advice. Insider buying has historically been followed by modestly positive returns *on average*, and plenty of individual cases lose money.

## Other features
* Filters for date range (7 / 14 / 30 days or custom), ticker watchlist or market-wide, role (CEO, CFO, Legal / General Counsel, Director / 10% Owner), transaction codes (P, S, A, M), minimum $ value, and 4/A amendments.
* Scans run in the background with a live progress bar and Cancel button.
* Click a column header to sort. Double-click a row to open the Form 4 on SEC.gov. Right-click a row for *Why this score?*, the Yahoo price chart, the filing index, *Show only this ticker*, or to copy the row or link.
* **Export CSV** saves the rows currently shown, with every signal column, the score and its reasons, trade dates and accession numbers.
* Requests are capped at 8 per second (SEC's limit is 10). Parsed filings are cached in `%LOCALAPPDATA%\Form4Scanner`, so re-scans are fast. A first market-wide 30-day scan downloads tens of thousands of filings and can take an hour or more.

## Run from source
Requires Python 3.9+ and nothing else (standard library only).
```
python form4_scanner.py                 # GUI
python form4_scanner.py --cli --ua "Jane Doe jane@example.com" --days 30 --codes P --min 100000 --exclude-plan --clusters 2 --min-stake 10 --hide-routine --hide-sold --exclude-offering --exclude-funds --min-score 60 --csv out.csv
python tests/test_scanner.py            # offline tests (mocked SEC responses)
```
On Windows you can also double-click `Run_Form4_Scanner.bat`, or `Build_EXE.bat` to build the exe locally.

## Project layout
```
form4_scanner.py                  the app (GUI + scanner + CLI)
assets/app.ico, app.png           icon
installer/Form4Scanner.iss        Inno Setup installer script
.github/workflows/build-windows.yml  CI: tests → exe → installer → release
tests/test_scanner.py             offline end-to-end tests
tests/gui_smoke.py                manual GUI smoke test (Linux/Xvfb)
```
