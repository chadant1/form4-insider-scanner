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

The **Signal Filters** row applies instantly to results you already have, so you can loosen or tighten it without re-scanning.

### Signal columns
* **Stake Δ %** shows how much the trade changed the insider's holding. It is calculated from *shares owned following transaction*, adding direct and each indirect holding (e.g. a trust) separately. **New** means a first-ever position.
* **Cluster** is the number of *different* insiders who made the same type of trade in that company during the scanned dates. It counts every size and role, not just the rows shown. With *Exclude 10b5-1* on, plan trades don't count toward it. Cluster buys are shown in **bold green**.
* **10b5-1** shows *Yes* when the filing ticks the Rule 10b5-1 checkbox (added to Form 4 in 2023) or a footnote on the transaction says it was made under a 10b5-1 plan. Footnotes saying "*not* pursuant to a 10b5-1 plan" are ignored.

This is a research filter, not investment advice. Insider buying has historically been followed by modestly positive returns *on average*, and plenty of individual cases lose money.

## Other features
* Filters for date range (7 / 14 / 30 days or custom), ticker watchlist or market-wide, role (CEO, CFO, Legal / General Counsel, Director / 10% Owner), transaction codes (P, S, A, M), minimum $ value, and 4/A amendments.
* Scans run in the background with a live progress bar and Cancel button.
* Click a column header to sort. Double-click a row to open the Form 4 on SEC.gov. Right-click a row to open the filing index, show only that ticker, or copy the row or link.
* **Export CSV** saves the rows currently shown, including stake %, cluster counts, the 10b5-1 flag, trade dates and accession numbers.
* Requests are capped at 8 per second (SEC's limit is 10). Parsed filings are cached in `%LOCALAPPDATA%\Form4Scanner`, so re-scans are fast. A first market-wide 30-day scan downloads tens of thousands of filings and can take an hour or more.

## Run from source
Requires Python 3.9+ and nothing else (standard library only).
```
python form4_scanner.py                 # GUI
python form4_scanner.py --cli --ua "Jane Doe jane@example.com" --days 30 --codes P --min 100000 --exclude-plan --clusters 2 --min-stake 10 --csv out.csv
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
