# WAGTales Tracker

A private investment tracker for funding pet-food resale deals. You fund the distributor price of an order, the customer pays the selling price, and you earn the difference as profit, minus logistics.

It runs on your own Mac as a small local web app, with no accounts or logins. Your data stays in one database file on your Mac, plus any backup copies you choose to keep. Amounts are shown as plain numbers (built with Indian rupees in mind) and dates as dd.mm.yyyy.

## What it does

- **Dashboard:** profit received, expected and projected, capital invested, return, financial-year totals, upcoming returns, and a list of what needs attention.
- **Customers:** totals for each customer, with a statement you can expand in place, print or save as a PDF.
- **Arrangements:** one-time or recurring agreements, with contract files you can upload, open and replace.
- **Deals:** search, filter and sort deals, and mark returns as received.
- **Reinvestments:** record profit rolled into another deal or elsewhere.
- **History:** see every change and restore any recent point. A restore is saved as a change too, so it can be undone.
- **Reminders:** Mac notifications for returns due or overdue, money not marked as sent, and a failing backup.
- **Backups:** an automatic daily copy in a folder you choose.
- **Dark and light themes:** dark by default, with a switch in the header.

## Requirements

- A Mac. The installer and notifications rely on macOS.
- Python 3.9 or later, with no packages to install. If macOS offers to install the Command Line Tools, accept and run the installer again.

## Install

```bash
git clone https://github.com/newrishav-debug/WAGTales.git
cd WAGTales
bash install.sh
```

The app opens at http://127.0.0.1:8765 and starts again each time you log in. There's nothing to configure. To update, pull the latest code and run `bash install.sh` again; your data is kept.

To stop and remove the app, run `bash install.sh uninstall`. Your data stays in `~/WAGTales/data`.

## Back up your data

**Backups are off until you choose a folder, and without one, losing your Mac means losing your data.**

1. Click **Backup off** in the app's header.
2. Enter the full path of a folder that isn't only on this Mac, such as iCloud Drive or an external disk. For iCloud Drive:
   ```
   ~/Library/Mobile Documents/com~apple~CloudDocs/WAGTales Backups
   ```
3. Click **Save**.

The app saves a `wagtales-YYYY-MM-DD.db` file after each change, one file per day, and keeps the last 30. Each file holds everything, including contracts and history. The files aren't encrypted, so keep them somewhere private.

To restore, your data folder must be empty, so move the existing `~/WAGTales/data` aside first. Then run this from the repo folder:

```bash
python3 app/server.py --restore "/path/to/wagtales-YYYY-MM-DD.db"
bash install.sh
```

## Where things are

| What | Where |
|---|---|
| Your data | `~/WAGTales/data/wagtales.db` |
| Settings | `~/WAGTales/data/settings.json` |
| Installed app | `~/WAGTales/app` |
| Log | `~/Library/Logs/wagtales-local.log` |

## Privacy

- The app only answers requests addressed to this Mac, so other devices can't reach it.
- Your data isn't stored in this repository. All names, amounts and other values under `test/fixtures/` are made up.
- There's no login, so anyone using your Mac account can open the app.
- The page loads its font from Google Fonts. No tracker data is sent to it.

## Development

The app is `app/server.py` (Python standard library and SQLite) and `app/index.html` (the whole interface). [CLAUDE.md](CLAUDE.md) has the data model, API and dashboard calculations.

```bash
python3 test/test_server.py                                    # server tests
npm install --no-save puppeteer-core && node test/ui.test.js   # interface tests, using Google Chrome
python3 app/server.py --data /tmp/wagtales-test --port 8799    # run a scratch copy
```

Run the scratch copy on its own data folder so it doesn't touch your real data.

One interface test, "needs attention shows the unsent deal", currently fails because it relies on dates that have passed.
