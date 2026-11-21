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

## The main screen
* **Header toolbar:** ★ Insider Buying preset, ▼ Insider Selling preset, Track Record, Back-test, Export CSV and **Theme**. The themes are Light, Dark, Midnight Blue and Sepia (warm). Dark themes also darken the Windows title bar.
* **Show / hide settings** (Ctrl+H) collapses the scan settings so the results get the whole window.
* **Summary cards:** trades shown, open-market buys and sales ($), cluster-buy companies, the best buy score and the strongest sell warning.
* **View bar**, to reorganize the same results instantly:

| View | What it shows |
|---|---|
| All trades | Every row, sorted by score (click headers to re-sort) |
| Top buys / Top sells | Buys ranked by Signal Score / sales ranked by Sell Score |
| Cluster buys | One expandable group per company where 2+ insiders bought, with the buyers inside |
| By company | Every company with net insider $ (buys − sales), with its trades inside |
| By insider | The week's biggest buyers and sellers by total $, with their trades inside |
| Largest $ | Biggest trades first |

* **Search** filters by ticker, company, insider or title as you type.
* **Expand all / Collapse all** open or close every group in the grouped views.

## Finding true insider buying

Click **★ Insider Buying** (the True Insider Buying preset), then **Start Scan**. The preset sets:

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
| Hide duplicate filings | On | A fund and its GP (or a person and their trust) reporting the same purchase count once |
| Min price / min $ volume | $1 / $100K a day | Avoids penny stocks and shares you couldn't realistically trade |

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
| This insider's past buys beat / lagged SPY by 10%+ over 6 months (2+ buys) | +6 / −4 |
| Bought within 30 days of an earnings report that dropped the stock 10%+ | +4 |
| Book/market 0.5+ / trading below book | +2 / +4 |
| Burning cash with under a year left / negative equity | −5 / −3 |
| Form 4 filed 10+ business days late / thinly traded (under $250K a day) | −4 / −5 |

Disqualifiers also **cap** the score: an offering buy is capped at 30, a 10b5-1 or routine buy at 45, a buyer who has since sold 25%+ at 40, and a duplicate filing at 20. Right-click a row and choose **Why this score?** to see every point. The default weights are a starting point based on published research and common practice. **Back-test** them on real history (below) and apply tuned weights if they hold up.

### Back-test (File → Back-test Signal Score, Ctrl+B)
The back-test replays SEC's quarterly **Insider Transactions Data Sets**, the official bulk files of every Form 3/4/5 since 2006, for the quarters you choose. It works like this:
* Every qualifying buy is scored **as of its filing date**, using only information that was public then. A second buyer counts toward a cluster only once their Form 4 had been filed. Pattern, insider record, net flow, earnings and company data all come from the past.
* It measures the **1-, 3- and 6-month returns** from the filing date, and the same for SPY.
* It shows results by **score band**, then by **factor**: the average 6-month return vs SPY with and without each factor, and the points that difference suggests.
* **Test: Buy Signal Score / ▼ Sell Score** chooses which score to back-test. For sales, the suggested points reward factors that preceded *worse* returns.
* **Train / test check:** suggested weights are tuned on the first half of the period, then both the current and suggested weights are scored on the second half. Apply them only if they also ranked the second half better. A factor needs 30+ signals each way before any change is suggested. **Reset to default weights** at any time.
* Company size, book value and cash come from SEC XBRL *frames*, which return one figure for every company in a single request. They use figures from two quarters before each filing, so they had already been published. Prices come from Yahoo.
* **Caveats:**
  * Yahoo has no prices for delisted companies, so failed companies drop out (survivorship bias).
  * Factor comparisons are simple one-at-a-time differences.
  * The sample is random when there are more signals than the "Max signals" setting.
  * Downloads are kept in `%LOCALAPPDATA%\Form4Scanner\bulk`.

## Spotting significant insider selling
Most insider sales are noise: diversification, taxes, buying a house. Research finds that the sales worth noticing are **large relative to the insider's holding** and **discretionary**, meaning not pre-planned and not explained. Click **▼ Insider Selling** for a preset that:
* scans open-market sales of $250k+ over the last 30 days;
* hides 10b5-1 plan sales, offering sales, and sales by funds or companies;
* hides **explained** sales (a footnote cites taxes, estate planning, divorce, gifts, vesting and so on);
* hides **exercise-and-sell** sales (options exercised and sold the same day);
* hides routine sellers;
* keeps only sales of **25%+ of the holding**;
* opens the **Top sells** view.

Every sale gets a **Sell Score** (shown as ▼ 0–100; higher means a stronger warning). It starts at 30:

| Factor | Points |
|---|---|
| Sold the entire position / 50%+ / 25%+ of the holding; under 5% | +22 / +15 / +8; −8 |
| Discretionary (no plan, no explanation) | +8 |
| Explained in a footnote / 10b5-1 plan / exercise-and-sell | −10 / −12 / −12 |
| CEO or CFO seller (director) | +8 (+3) |
| Sale of $1M+ / $5M+ | +4 / +8 |
| First sale in 5+ years / opportunistic / regular / routine seller | +10 / +6 / −8 / −15 |
| 2 / 3+ insiders selling (a weak signal for sales) | +2 / +4 |
| Stock up 30%+ in the 3 months before / sold within 5% of the 52-week high | +5 / +4 |
| Sold late in the quarter, ahead of the next earnings report | +5 |
| Insiders heavy net sellers / net buyers over 90 days | +4 / −4 |
| Fund or company seller / sold in a registered offering | −8 / −15 |
| Form 4 filed 10+ business days late | +3 |

Offering sales are capped at 30. Exercise-and-sell is capped at 40. Plan, explained and routine sales are capped at 45. The deep check (seller history, prices and so on) runs for sales that look significant: 10%+ of the holding or $1M+, and not under a plan. The Track Record and back-test handle sales too. There, a *good* sell warning is one where the stock then **lagged** SPY. Use sell signals as a prompt to look harder at stocks you own or watch, not as a short-selling system.

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
* **Insider Record** shows how this insider's own earlier buys of the stock did over the following 6 months vs SPY: the average, and how many past buys were measured.
* **Since Earnings** shows the days since the company's last earnings release (8-K Item 2.02) and the stock's move on that report.
* **Book/Mkt** is shareholders' equity ÷ market value. **Avg $ Vol** is the average daily dollar volume over the 20 sessions before the buy (hidden by default).
* More **Flags**: *Dup* (same purchase already reported by a related owner), *Late Nd* (filed N business days after the 2-day deadline), *LowCash* (burning cash with under a year left), *NegEq* (negative equity).
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
python form4_scanner.py --backtest 2025Q2 2026Q1 --ua "Jane Doe jane@example.com" --csv backtest.csv
python form4_scanner.py --backtest 2025Q2 2026Q1 --sells --ua "Jane Doe jane@example.com"
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
