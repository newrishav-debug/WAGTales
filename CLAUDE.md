# WAGTales Investment Tracker

A personal tracker for funding pet-food resale deals. The owner funds the distributor price of an order. The customer pays the selling price and the owner earns the difference as profit, minus logistics. Amounts are in Indian rupees; show plain numbers with no currency symbol and dates as dd.mm.yyyy.

## How it fits together

The **web app on the owner's Mac is the entry point and the source of truth**. It runs only on the Mac (owner's choice), at http://127.0.0.1:8765, started at login by a launchd agent (`com.wagtales.local`).

- `app/server.py`: Python 3.9 standard library only, because launchd runs `/usr/bin/python3`. It serves the page, a JSON API, and contract files, stores data as CSV, and runs two background threads: the backup and the reminders.
- `app/index.html`: the whole UI, edited directly. It came from the earlier claude.ai artifact (`wagtales.html`, now deleted; it's in git history).
- `app/apps-script.gs`: the Google Sheet backup endpoint (`SCRIPT_VERSION = 5`).
- `install.sh`: copies `app/` to `~/WAGTales/app` and (re)loads the launchd agent. It installs outside the repo because launchd agents can't read `~/Desktop`.

### Data: `~/WAGTales/data`
Not in this repo. `data-template/` holds the CSV columns with no rows, and everything in `test/fixtures/` is made up.
- `customers.csv`: `id` (CUS-01…), name, phone, notes.
- `arrangements.csv`: `id`, `customer_id`, `label` (e.g. "litter"), type, term_months, `projection` (`latest`/`fixed`/`none`), `cycle_profit`, `contract_file`, `contract_url`.
- `deals.csv`: inputs only, plus `received_date`.
- `reinvestments.csv`: `destination_deal_no` (a linked deal) or free-text `destination`.
- `contracts/`: local contract files, named `ARR-xx contract - <customer>.<ext>`.
- A git repo: every change is committed with a readable message (e.g. "Edit deal #4 (notes)"). The History tab lists the commits and restores a point by checking those files out as a new commit. Commits starting `Data format:` change the CSV columns, and history can't be restored past them.
- Git-ignored: `settings.json` (mode 600: backup URL, key, and `notify` for reminders), `report.json` (the latest dashboard report), `reminders.json` (what was notified today).

Formats are upgraded on start. `Store._upgrade` moves the first format (21.10.2026: customer as text on arrangements, no received dates) to the current one. It splits "Name (label)" into a customer plus a label and merges customers by name. Fixture: `test/fixtures/v1-data`. The owner doesn't edit the CSVs by hand; all changes go through the app.

An arrangement's display name, used everywhere (Deals "Customer", the sheet), is the customer's name plus "(label)" when there is a label.

### Google Sheet: backup and reports only
WAGTales Investment, ID `15KTdFGPKTQuRLtLUg4L3fFVz-boxhVVxLmm4Z5JZc9g`. After every change, the backup thread works through these steps:
1. On the first run, it pings to learn the script version.
2. It copies new local contracts to the Drive folder **WAGTales Contracts** (`10zo3a3jNeNt2cZfHg0YgkCGwtOQgHbaE`).
3. It sends a `sync` with every tab plus a report. The call is skipped when the content hash is unchanged.
4. With script v5, it downloads contracts that exist only in Drive (`getContract`, limited to that folder).

A failed request is retried once, because Google occasionally fails a single request, even with a bad_key reply. Failures are retried every 15 minutes.

The script rewrites the Customers (v5 only), Arrangements, Deals and Reinvestments tabs as values, and writes a **Dashboard** tab (first tab) from the report the page builds out of `model()`. **Summary** and **Margins** are untouched; Summary's formulas still work because the original columns keep their positions. With a v4 script the backup still works, but `public()` shows a notice asking for the script update. `python3 app/server.py --restore` refills an empty data folder from the sheet; `from_sheet` reads both the current layout and the first one.

### Sheet-shaped columns (what `/api/data` returns and the backup writes)
The original columns keep their positions (the page reads `o.c[i]`, `o.row` = row number), and new columns go on the end:
- **Customers A:F**: ID · Name · Phone · Notes · Arrangements · Deals.
- **Arrangements A:O**: ID · Customer (display name) · Type · Term · Start (earliest deal) · End (EDATE start+term) · Deals · Agreed Return · Received · Outstanding · Contract · **Customer ID · Label · Projection · Profit per Cycle**.
- **Deals A:U**: Deal # · Arrangement · Customer · Status · Investment Date · MRP · Selling Price · Distributor Price · Profit (sell − dist) · Invested · Amount Sent · Transaction No. (text) · Return Type · Agreed Return (profit, or dist + profit for Profit + Principal) · Tentative Return Date · Logistics · Actual Return (agreed − logistics) · Return Received · Notes · Check · **Received Date**.
- **Reinvestments A:H**: Date · Source Deal # · Amount · Destination ("Deal #4, <name>" when linked) · Expected Return Date · Return Received · Notes · **Destination Deal #**.

`test_view_matches_the_sheet_formulas` checks the computed columns against a sample sheet (`test/fixtures/`, made-up data).

## API (`POST /api`, JSON, header `X-WagTales: 1` required)
- **Customers:** `addCustomer`, `updateCustomer`, `deleteCustomer`.
- **Arrangements:** `addArrangement`, `updateArrangement`, `deleteArrangement`, `uploadContract`. They take `custId`, or `newCust` to create the customer (or reuse one by name).
- **Deals:** `addDeal`, `updateDeal` (`recvDate`), `markReceived` (`date`), `deleteDeal`.
- **Reinvestments:** `addReinvestment`, `updateReinvestment`, `deleteReinvestment`. They take `destDeal` or `dest`.
- **App:** `restore` (`hash`), `report`, `syncNow`, `saveSettings`, `setReminders` (`on`).

Edits and deletes send `row` plus the original key value (`orig`), and are refused if the row changed. Deletes are refused when a record is still in use: a customer with arrangements, an arrangement with deals, or a deal in a reinvestment (source or destination). A deal number used by a reinvestment can't be changed.

GET endpoints: `/api/data`, `/api/status`, `/api/history`, `/contracts/<name>`, `/apps-script.gs`. The server only answers to `Host` 127.0.0.1/localhost.

## Reminders
`Reminders` checks hourly, starting 60 s after launch, and posts a Mac notification via `osascript` at most once a day per item:
- a return due tomorrow or today
- an overdue return (repeats daily)
- an Active deal with Amount Sent = No
- a backup that is failing

It can be switched on and off in the Backup dialog (`settings.json` `notify`). The dashboard's **Needs attention** card shows the same items, plus returns due within 7 days.

## Dashboard logic (`model()` in app/index.html)
- **Received:** realised profit on deals with Return Received = Yes. That is Actual Return, minus the distributor price for Profit + Principal deals. Received deals are dated by Received Date, falling back to the due date and then the investment date.
- **Logistics rate:** total logistics ÷ total profit on paid deals. It is applied to every amount still to come.
- **Expected:** profit on unpaid deals × (1 − logistics rate).
- **Projected:** recurring arrangements get one cycle per month of the term; missing cycles are projected monthly after the latest deal's return date.
  - `latest`: each projected cycle repeats the latest deal's profit.
  - `fixed`: `cycle_profit` is used instead.
  - `none`: the arrangement isn't projected.

  Logistics comes off all of them. An arrangement with no deals has no projection.
- **Capital invested:** the largest single Invested Amount per arrangement, because principal circles from cycle to cycle. Fresh capital = capital − reinvestments linked to deals.
- **Return:** total profit ÷ capital. **Per year:** return × 365 ÷ days from first investment to last payout (simple, not compounded; n/a under 28 days).
- **By financial year:** Indian FY (April–March), by paid/due date.
- **Customers:** the sums of their arrangements.

## UX decisions the owner asked for
- Tabs in workflow order: **Dashboard › Customers › Arrangements › Deals › Reinvestments › History**; each tab has its own add button.
- New customer → New arrangement with them preselected → Add deal with it preselected → Dashboard.
- Mark received requires a typed logistics amount (0 allowed) and a received date (defaults to today).
- Edit and Delete exist for all record types.
- Deals table: search (customer, deal #, notes, transaction), status and return filters, sortable columns (#, customer, date, invested, agreed, due).
- Customers have a printable **Statement** (print CSS hides the app chrome; save as PDF from the print window).
- Projected cycles in Upcoming returns have an **Add deal** button that prefills from the last cycle.
- The page auto-refreshes on focus and every 2 minutes, never while a form or dialog is open.
- The header shows backup status and opens the Backup and reminders dialog.
- Toasts don't block clicks (`pointer-events: none`).
- Calm finance look: Public Sans, navy accent, received/expected/projected as dark/mid/light blue, light and dark themes, max width 1440px.

## Build, test, deploy
```bash
python3 test/test_server.py          # store, upgrade, API, history, reminders, backup (fake Apps Script), restore
node test/apps-script.test.js        # apps-script.gs against a simulated sheet and Drive
npm install --no-save puppeteer-core && node test/ui.test.js   # drives the page in Google Chrome; screenshots in test/shots/
bash install.sh                      # deploy app/ to ~/WAGTales/app and restart; data is upgraded if needed, never replaced
python3 app/server.py --data /tmp/x --port 8799   # run a scratch copy
```
- **Apps Script changes:** paste `app/apps-script.gs` into the sheet's Extensions › Apps Script and save. Then Deploy › Manage deployments › Edit › **New version** › Deploy. **Never rerun `setup` once a key is in use**, because it replaces the key. When the script version rises, raise `LATEST_SCRIPT` in server.py; `SCRIPT_VERSION` there is the oldest script that still works.
- Logs: `~/Library/Logs/wagtales-local.log`.

## History and open items
- 21.10.2026: moved from "Google Sheet as database with two front ends" to this local app, with data imported from the live sheet. The backup was connected the same evening (script v4).
- 21.10.2026 later: added customers, received dates, linked reinvestments, projection overrides, per-year return, financial-year report, Needs attention + Mac reminders, deal filters and sorting, statements and History.
- **Waiting on the owner:** paste the v5 script and deploy a new version. Until then there's no Customers tab in the sheet, and the ARR-01/ARR-03 contracts stay Drive-only (they download automatically after the update).
- The claude.ai artifact (https://claude.ai/artifact/65ScVZUaQTgv8YjVKErmtR) still writes to the sheet directly and should no longer be used.
