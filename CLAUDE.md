# WAGTales Investment Tracker

A personal tracker for funding pet-food resale deals. The owner funds the distributor price of an order. The customer pays the selling price and the owner earns the difference as profit, minus logistics. Amounts are in Indian rupees; show plain numbers with no currency symbol and dates as dd.mm.yyyy.

## How it fits together

The **web app on the owner's Mac is the entry point and the source of truth**. It runs only on the Mac (owner's choice), at http://127.0.0.1:8765, started at login by a launchd agent (`com.wagtales.local`).

- `app/server.py`: Python 3.9 standard library only, because launchd runs `/usr/bin/python3`. It serves the page, a JSON API, and contract files, stores data in SQLite, and runs two background threads: the backup and the reminders.
- `app/index.html`: the whole UI, edited directly.
- `install.sh`: copies `app/` to `~/WAGTales/app` and (re)loads the launchd agent. It installs outside the repo because launchd agents can't read `~/Desktop`.

### Data: `~/WAGTales/data/wagtales.db`
One SQLite file (mode 600) holds everything. All record columns are text: ISO dates, plain numbers.
- `customers`, `arrangements`, `deals`, `reinvestments`: inputs only; worked-out columns are computed in `build()`. Columns are in `TABLES` in server.py.
- `contracts` (name to hash) and `contract_data` (hash to file contents): contract files. Contents are never removed, because the history can bring them back.
- `history`: a full copy of the records after every change, with a readable message (e.g. "Edit deal #4 (notes)"). The History tab lists them, and a restore is saved as a new change.
- `PRAGMA user_version` is `DB_VERSION`. Changing a table means raising it and adding a migration.

`settings.json` (mode 600: `backup_dir`, `notify`) and `reminders.json` (what was notified today) sit beside the database.

There is **no CSV and no Google Sheet or Drive code**. The move happened once, on 06.11.2026, with a script that isn't in the repo. The owner's Sheet still exists but nothing reads or writes it.

An arrangement's display name, used everywhere, is the customer's name plus "(label)" when there is a label.

### Backup folder
Set in the Backup dialog (`backup_dir`). After every change, and on start, `Backup` saves `wagtales-YYYY-MM-DD.db` in that folder using SQLite's backup API, checks it with `quick_check`, and keeps the last 30. It only ever removes files matching that name. Failures show in the header and are retried every 15 minutes. `python3 app/server.py --restore FILE.db` fills an empty data folder from a copy; restart the app afterwards with `bash install.sh`.

### Page columns (what `/api/data` returns)
The original columns keep their positions (the page reads `o.c[i]`, `o.row` = row number), and new columns go on the end:
- **Customers A:F**: ID · Name · Phone · Notes · Arrangements · Deals.
- **Arrangements A:O**: ID · Customer (display name) · Type · Term · Start (earliest deal) · End (EDATE start+term) · Deals · Agreed Return · Received · Outstanding · Contract · **Customer ID · Label · Projection · Profit per Cycle**.
- **Deals A:U**: Deal # · Arrangement · Customer · Status · Investment Date · MRP · Selling Price · Distributor Price · Profit (sell − dist) · Invested · Amount Sent · Transaction No. (text) · Return Type · Agreed Return (profit, or dist + profit for Profit + Principal) · Tentative Return Date · Logistics · Actual Return (agreed − logistics) · Return Received · Notes · Check · **Received Date**.
- **Reinvestments A:H**: Date · Source Deal # · Amount · Destination ("Deal #4, <name>" when linked) · Expected Return Date · Return Received · Notes · **Destination Deal #**.

`test_view_matches_the_expected_figures` checks the computed columns: `test/fixtures/sample-data.json` is the input and `expected-view.json` the result, both made up.

## API (`POST /api`, JSON, header `X-WagTales: 1` required)
- **Customers:** `addCustomer`, `updateCustomer`, `deleteCustomer`.
- **Arrangements:** `addArrangement`, `updateArrangement`, `deleteArrangement`, `uploadContract`. They take `custId`, or `newCust` to create the customer (or reuse one by name).
- **Deals:** `addDeal`, `updateDeal` (`recvDate`), `markReceived` (`date`), `deleteDeal`.
- **Reinvestments:** `addReinvestment`, `updateReinvestment`, `deleteReinvestment`. They take `destDeal` or `dest`.
- **App:** `restore` (`id`), `syncNow`, `saveSettings` (`folder`), `setReminders` (`on`).

Edits and deletes send `row` plus the original key value (`orig`), and are refused if the row changed. Deletes are refused when a record is still in use: a customer with arrangements, an arrangement with deals, or a deal in a reinvestment (source or destination). A deal number used by a reinvestment can't be changed.

GET endpoints: `/api/data`, `/api/status`, `/api/history`, `/contracts/<name>`. The server only answers to `Host` 127.0.0.1/localhost.

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
python3 test/test_server.py          # database, API, history, reminders, backup folder, restore
npm install --no-save puppeteer-core && node test/ui.test.js   # drives the page in Google Chrome; screenshots in test/shots/
bash install.sh                      # deploy app/ to ~/WAGTales/app and restart; data is upgraded if needed, never replaced
python3 app/server.py --data /tmp/x --port 8799   # run a scratch copy
```
- Logs: `~/Library/Logs/wagtales-local.log`.

## History and open items
- 21.10.2026: moved from "Google Sheet as database with two front ends" to this local app, with data imported from the sheet.
- 21.10.2026 later: added customers, received dates, linked reinvestments, projection overrides, per-year return, financial-year report, Needs attention + Mac reminders, deal filters and sorting, statements and History.
- 06.11.2026: moved from CSV files with a Google Sheet backup to one SQLite database with a backup folder. All Sheet, Drive and CSV code was removed.
