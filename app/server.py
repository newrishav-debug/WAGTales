#!/usr/bin/env python3
"""WAGTales tracker: the app that runs on this Mac.

Serves the tracker page at http://127.0.0.1:8765 and keeps the data in CSV files in
the data folder (default ~/WAGTales/data). Every change is committed to a git
repository in that folder, so the page can show the history and restore any point.

When the Google Sheet backup is set up, the whole dataset and a dashboard report
are copied to the WAGTales Investment sheet after each change, through the Apps
Script web app in apps-script.gs. The sheet is a copy; this app is the source.
The app also shows Mac notifications for returns that are due and money not sent.

    python3 server.py [--data DIR] [--port 8765]
    python3 server.py --import FILE.json   fill empty CSVs from sheet values
    python3 server.py --restore            fill empty CSVs from the Google Sheet backup

Standard library only, compatible with the /usr/bin/python3 (3.9) that launchd runs.
"""
import argparse
import base64
import calendar
import csv
import datetime as dt
import hashlib
import json
import math
import mimetypes
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
import traceback
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import quote, unquote, urlparse

APP_DIR = os.path.dirname(os.path.abspath(__file__))
SCRIPT_VERSION = 4   # the oldest apps-script.gs that still works
LATEST_SCRIPT = 5    # the version in this folder: adds the Customers tab and contract downloads
MAX_CONTRACT = 15 * 1024 * 1024
MAX_BODY = 25 * 1024 * 1024
CONTRACT_EXTS = ('.pdf', '.doc', '.docx', '.jpg', '.jpeg', '.png')
CHANGED = 'The data changed since the page loaded. Refresh and try again.'
DATA_FORMAT = 'Data format: '  # commits that change the CSV columns; history cannot go back past them

# ---------- CSV files: input columns only, ISO dates, plain numbers ----------
FILES = {
    'cust': ('customers.csv', ['id', 'name', 'phone', 'notes']),
    'arr': ('arrangements.csv', ['id', 'customer_id', 'label', 'type', 'term_months', 'projection', 'cycle_profit',
                                 'contract_file', 'contract_url']),
    'deals': ('deals.csv', ['deal_no', 'arrangement_id', 'status', 'investment_date', 'mrp', 'selling_price',
                            'distributor_price', 'invested_amount', 'amount_sent', 'transaction_no', 'return_type',
                            'tentative_return_date', 'logistics', 'return_received', 'notes', 'received_date']),
    'reinv': ('reinvestments.csv', ['date', 'source_deal_no', 'amount', 'destination_deal_no', 'destination',
                                    'expected_return_date', 'return_received', 'notes']),
}
# the first format (21.10.2026), upgraded on start
OLD_COLS = {
    'arr': ['id', 'customer', 'type', 'term_months', 'contract_file', 'contract_url'],
    'deals': FILES['deals'][1][:-1],
    'reinv': ['date', 'source_deal_no', 'amount', 'destination', 'expected_return_date', 'return_received', 'notes'],
}
PROJECTIONS = ['latest', 'fixed', 'none']
FIELD_NAMES = {'deal_no': 'deal #', 'arrangement_id': 'arrangement', 'investment_date': 'date', 'mrp': 'MRP',
               'tentative_return_date': 'return date', 'customer_id': 'customer', 'term_months': 'term',
               'cycle_profit': 'profit per cycle', 'contract_file': 'contract', 'source_deal_no': 'source deal',
               'destination_deal_no': 'destination', 'expected_return_date': 'expected return date'}

# ---------- the sheet-shaped view: tabs and columns as in the Google Sheet. Kinds: t text, n number, d date ----------
# The first columns of each tab keep the original sheet layout; later additions go on the end.
SHEET = {
    'cust': ('Customers', [('ID', 't'), ('Name', 't'), ('Phone', 't'), ('Notes', 't'), ('Arrangements', 'n'), ('Deals', 'n')]),
    'arr': ('Arrangements', [('ID', 't'), ('Customer', 't'), ('Type', 't'), ('Term (months)', 'n'), ('Start Date', 'd'),
                             ('End Date', 'd'), ('Deals', 'n'), ('Agreed Return', 'n'), ('Received (net of logistics)', 'n'),
                             ('Outstanding (agreed)', 'n'), ('Contract', 't'), ('Customer ID', 't'), ('Label', 't'),
                             ('Projection', 't'), ('Profit per Cycle', 'n')]),
    'deals': ('Deals', [('Deal #', 'n'), ('Arrangement', 't'), ('Customer', 't'), ('Status', 't'), ('Investment Date', 'd'),
                        ('MRP', 'n'), ('Selling Price', 'n'), ('Distributor Price', 'n'), ('Profit', 'n'),
                        ('Invested Amount', 'n'), ('Amount Sent', 't'), ('Transaction No.', 't'), ('Return Type', 't'),
                        ('Agreed Return', 'n'), ('Tentative Return Date', 'd'), ('Logistics', 'n'), ('Actual Return', 'n'),
                        ('Return Received', 't'), ('Notes', 't'), ('Check', 't'), ('Received Date', 'd')]),
    'reinv': ('Reinvestments', [('Date', 'd'), ('Source Deal #', 'n'), ('Amount Rolled Over', 'n'), ('Destination', 't'),
                                ('Expected Return Date', 'd'), ('Return Received', 't'), ('Notes', 't'),
                                ('Destination Deal #', 'n')]),
}


class Refused(Exception):
    """A change the app will not make. The message is shown to the user."""


class BackupError(Exception):
    """The Google Sheet backup did not work. The message is shown to the user."""

    def __init__(self, message, retry=False):
        super().__init__(message)
        self.retry = retry  # Google sometimes fails one request and answers the next


# ---------- values ----------
def num(v):
    if v is None or isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        n = float(v)
    else:
        s = str(v).replace(',', '').strip()
        if not s:
            return None
        try:
            n = float(s)
        except ValueError:
            return None
    return n if math.isfinite(n) else None


def canon(n):
    """a number as the CSV stores it: 12345, 0.5"""
    if n is None:
        return ''
    if float(n).is_integer():
        return str(int(n))
    return ('%.6f' % n).rstrip('0').rstrip('.')


def typed(s):
    """CSV number text -> int or float, or '' when empty"""
    n = num(s)
    if n is None:
        return ''
    return int(n) if n.is_integer() else n


def fmt_num(v):
    if v == '' or v is None:
        return ''
    if float(v).is_integer():
        return '{:,}'.format(int(v))
    return '{:,.2f}'.format(v).rstrip('0').rstrip('.')


def fmt_date(iso):
    m = re.match(r'^(\d{4})-(\d{2})-(\d{2})$', iso or '')
    return '%s.%s.%s' % (m.group(3), m.group(2), m.group(1)) if m else (iso or '')


def parse_date(s):
    """dd.mm.yyyy, M/D/YYYY, yyyy-mm-dd or a sheet day number -> ISO date, or None"""
    s = str(s if s is not None else '').strip()
    try:
        m = re.match(r'^(\d{1,2})\.(\d{1,2})\.(\d{4})$', s)
        if m:
            return dt.date(int(m.group(3)), int(m.group(2)), int(m.group(1))).isoformat()
        m = re.match(r'^(\d{1,2})/(\d{1,2})/(\d{4})$', s)
        if m:
            return dt.date(int(m.group(3)), int(m.group(1)), int(m.group(2))).isoformat()
        m = re.match(r'^(\d{4})-(\d{1,2})-(\d{1,2})$', s)
        if m:
            return dt.date(int(m.group(1)), int(m.group(2)), int(m.group(3))).isoformat()
        if re.match(r'^\d{5}$', s):
            return (dt.date(1899, 12, 30) + dt.timedelta(days=int(s))).isoformat()
    except ValueError:
        return None
    return None


def edate(iso, months):
    """the sheet's EDATE: same day k months later, clamped to the end of the month"""
    d = dt.date.fromisoformat(iso)
    k = d.month - 1 + months
    y, m = d.year + k // 12, k % 12 + 1
    return dt.date(y, m, min(d.day, calendar.monthrange(y, m)[1])).isoformat()


def same(stored, shown):
    """does a stored value still match what the page showed?"""
    a, b = num(stored), num(shown)
    if a is not None and b is not None:
        return a == b
    return str(stored).strip() == str(shown if shown is not None else '').strip()


def next_id(prefix, rows):
    n = 0
    for r in rows:
        m = re.search(r'(\d+)\s*$', r.get('id', ''))
        if m:
            n = max(n, int(m.group(1)))
    return '%s-%02d' % (prefix, n + 1)


def split_name(text):
    """'Sample Breeder (litter)' -> ('Sample Breeder', 'litter')"""
    m = re.match(r'^(.*\S)\s*\(([^()]*)\)\s*$', text or '')
    return (m.group(1).strip(), m.group(2).strip()) if m else ((text or '').strip(), '')


def attach_customers(arrs, customers):
    """turn each arrangement's customer text into a customer record plus a label, reusing customers by name"""
    by_name = {c['name'].lower(): c for c in customers}
    for a in arrs:
        if 'customer' not in a:
            continue
        name, label = split_name(a.pop('customer'))
        a.setdefault('label', label)
        if not a['label']:
            a['label'] = label
        c = by_name.get(name.lower())
        if not c:
            c = {'id': next_id('CUS', customers), 'name': name, 'phone': '', 'notes': ''}
            customers.append(c)
            by_name[name.lower()] = c
        a['customer_id'] = c['id']


def dest_deal_of(text):
    """'Deal #4, Sample Breeder (litter)' -> '4'"""
    m = re.match(r'^\s*deal\s*#\s*(\d+)\b', text or '', re.I)
    return m.group(1) if m else ''


# ---------- checks on what the page sends ----------
def v_text(v):
    return '' if v is None else str(v)[:2000]


def v_num(v, label):
    n = num(v)
    if n is None or n < 0:
        raise Refused(label + ' must be a number of 0 or more.')
    return canon(n)


def v_date(v, label, required=True):
    if v in (None, ''):
        if required:
            raise Refused('Enter the ' + label + '.')
        return ''
    try:
        if not re.match(r'^\d{4}-\d{2}-\d{2}$', str(v)):
            raise ValueError
        return dt.date.fromisoformat(str(v)).isoformat()
    except ValueError:
        raise Refused('Dates must look like 2026-10-04.')


def v_one(v, options, label):
    if v not in options:
        raise Refused(label + ' must be one of: ' + ', '.join(options) + '.')
    return v


def at_row(rows, row, key, orig):
    """index of the row the page edited; refuses if it no longer holds what the page showed"""
    try:
        i = int(row) - 2
    except (TypeError, ValueError):
        i = -1
    if i < 0 or i >= len(rows):
        raise Refused('That row was not found. Refresh and try again.')
    if not same(rows[i][key], orig):
        raise Refused(CHANGED)
    return i


def what_changed(name, old, new):
    diff = [FIELD_NAMES.get(k, k.replace('_', ' ')) for k in FILES[name][1] if old.get(k, '') != new.get(k, '')]
    return ' (%s)' % ', '.join(diff) if diff else ' (nothing changed)'


def read_csv(path):
    with open(path, newline='', encoding='utf-8') as f:
        r = csv.reader(f)
        head = next(r, [])
        return head, [dict(zip(head, row)) for row in r]


# ---------- the data folder ----------
class Store:
    def __init__(self, root):
        self.root = root
        self.contracts = os.path.join(root, 'contracts')
        self.lock = threading.RLock()
        os.makedirs(self.contracts, exist_ok=True)
        upgraded = self._upgrade()
        for name, (fn, cols) in FILES.items():
            p = self.path(name)
            if not os.path.exists(p):
                self.save(name, [])
                continue
            head, _ = read_csv(p)
            if head != cols:
                raise SystemExit('%s has the columns %s, but the app expects %s.' % (p, head, cols))
        self._git_init()
        if upgraded:
            self.commit(DATA_FORMAT + 'customers, received dates and linked reinvestments')

    def _upgrade(self):
        """moves CSV files from the first format to this one"""
        old = {}
        for name in OLD_COLS:
            p = self.path(name)
            if os.path.exists(p):
                head, rows = read_csv(p)
                if head == OLD_COLS[name]:
                    old[name] = rows
        if not old:
            return False
        if 'arr' in old:
            cust = self.load('cust') if os.path.exists(self.path('cust')) else []
            for a in old['arr']:
                a.update(label='', projection='latest', cycle_profit='')
            attach_customers(old['arr'], cust)
            self.save('cust', cust)
        for d in old.get('deals', []):
            d['received_date'] = d['tentative_return_date'] if d['return_received'] == 'Yes' else ''
        for r in old.get('reinv', []):
            r['destination_deal_no'] = dest_deal_of(r['destination'])
            if r['destination_deal_no']:
                r['destination'] = ''
        for name, rows in old.items():
            self.save(name, rows)
        return True

    def path(self, name):
        return os.path.join(self.root, FILES[name][0])

    def load(self, name):
        cols = FILES[name][1]
        with open(self.path(name), newline='', encoding='utf-8') as f:
            return [{k: (r.get(k) or '') for k in cols} for r in csv.DictReader(f)]

    def snapshot(self):
        with self.lock:
            return {n: self.load(n) for n in FILES}

    def save(self, name, rows):
        # write a temporary file and rename it over the old one, so a crash never leaves half a file
        cols = FILES[name][1]
        fd, tmp = tempfile.mkstemp(dir=self.root, prefix='.' + name + '-', suffix='.tmp')
        try:
            with os.fdopen(fd, 'w', newline='', encoding='utf-8') as f:
                w = csv.DictWriter(f, fieldnames=cols)
                w.writeheader()
                for r in rows:
                    w.writerow({k: r.get(k, '') for k in cols})
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, self.path(name))
        except BaseException:
            if os.path.exists(tmp):
                os.unlink(tmp)
            raise

    # ---------- history ----------
    def _git(self, *args):
        try:
            return subprocess.run(['git', '-C', self.root] + list(args), capture_output=True, text=True, timeout=30)
        except (OSError, subprocess.SubprocessError):
            return None

    def _git_init(self):
        with open(os.path.join(self.root, '.gitignore'), 'w') as f:
            f.write('settings.json\nreport.json\nreminders.json\n.*.tmp\n')
        if os.path.isdir(os.path.join(self.root, '.git')):
            return
        r = self._git('init', '-q')
        if r is None or r.returncode:
            sys.stderr.write('git is not available, so changes are saved without history.\n')
            return
        self._git('config', 'user.name', 'WAGTales app')
        self._git('config', 'user.email', 'wagtales@localhost')
        self.commit('Start the WAGTales data history')

    def commit(self, message):
        self._git('add', '-A')
        self._git('commit', '-q', '-m', message)

    def history(self, limit=200):
        r = self._git('log', '-n', str(limit), '--format=%H%x1f%cI%x1f%s')
        if r is None or r.returncode:
            return []
        out, ok = [], True
        for i, line in enumerate(r.stdout.splitlines()):
            h, when, msg = line.split('\x1f', 2)
            start = msg.startswith('Start the')
            out.append({'hash': h, 'when': when, 'message': msg, 'current': i == 0,
                        'restorable': ok and i > 0 and not start})
            if msg.startswith(DATA_FORMAT) or start:
                ok = False  # older states use other columns
        return out

    def restore(self, h):
        with self.lock:
            entry = next((e for e in self.history() if e['hash'] == h), None)
            if not entry or not entry['restorable']:
                raise Refused('That point in the history cannot be restored.')
            added = self._git('diff', '--name-only', '-z', '--diff-filter=A', h, 'HEAD')
            for f in (added.stdout.split('\0') if added else []):
                p = os.path.join(self.root, f)
                if f and os.path.isfile(p):
                    os.remove(p)
            r = self._git('checkout', h, '--', '.')
            if r is None or r.returncode:
                raise Refused('The history could not be read: %s' % (r.stderr.strip() if r else 'git is missing'))
            when = dt.datetime.fromisoformat(entry['when']).strftime('%d.%m.%Y %H:%M')
            self.commit('Restore to how it was after "%s" (%s)' % (entry['message'], when))
        return {}

    # ---------- changes ----------
    def act(self, action, p):
        fn = ACTIONS.get(action)
        if not fn:
            raise Refused('Unknown action.')
        with self.lock:
            snap = {n: self.load(n) for n in FILES}
            out, changed, message = fn(self, snap, p)
            for n in changed:
                self.save(n, snap[n])
            if changed:
                self.commit(message)
        return out

    def seed(self, tabs, message):
        with self.lock:
            if any(self.load(n) for n in FILES):
                raise SystemExit('The data folder already has records. Import only fills an empty one.')
            for n in FILES:
                self.save(n, tabs[n])
            self.commit(message)

    def set_contract(self, arr_id, **fields):
        """records a contract copy made by the backup: its Drive link, or a local file downloaded from Drive"""
        with self.lock:
            rows = self.load('arr')
            for a in rows:
                if a['id'] == arr_id:
                    a.update(fields)
                    self.save('arr', rows)
                    self.commit(('Link the contract for %s in Google Drive' if 'contract_url' in fields
                                 else 'Download the contract for %s from Google Drive') % arr_id)
                    return


# ---------- changes the page can make ----------
def deal_taken(snap, n):
    return any(same(d['deal_no'], n) for d in snap['deals'])


def deal_fields(snap, p, editing):
    arr = v_text(p.get('arr')).strip()
    if not arr:
        raise Refused('Pick an arrangement.')
    if not any(a['id'] == arr for a in snap['arr']):
        raise Refused('Arrangement %s was not found.' % arr)
    n = num(p.get('n'))
    if n is None or n < 1 or not n.is_integer():
        raise Refused('Deal # must be a whole number of 1 or more.')
    f = {
        'deal_no': canon(n), 'arrangement_id': arr,
        'status': v_one(p.get('status'), ['Active', 'Closed'], 'Status'),
        'investment_date': v_date(p.get('date'), 'investment date'),
        'mrp': v_num(p.get('mrp'), 'MRP'),
        'selling_price': v_num(p.get('sell'), 'Selling price'),
        'distributor_price': v_num(p.get('dist'), 'Distributor price'),
        'invested_amount': v_num(p.get('inv'), 'Invested amount'),
        'amount_sent': v_one(p.get('sent'), ['Yes', 'No'], 'Amount sent'),
        'transaction_no': v_text(p.get('txn')).strip(),
        'return_type': v_one(p.get('rtype'), ['Profit Only', 'Profit + Principal'], 'Return type'),
        'tentative_return_date': v_date(p.get('due'), 'return date', required=False),
        'notes': v_text(p.get('notes')).strip(),
        'logistics': '', 'return_received': 'No', 'received_date': '',
    }
    if editing:
        lg = p.get('logi')
        f['logistics'] = '' if lg in (None, '') else v_num(lg, 'Logistics')
        f['return_received'] = v_one(p.get('recv'), ['Yes', 'No'], 'Return received')
        if f['return_received'] == 'Yes':
            f['received_date'] = v_date(p.get('recvDate'), 'received date', required=False)
    return f


def deal_in_reinvestment(snap, n):
    return any(same(r['source_deal_no'], n) or same(r['destination_deal_no'], n) for r in snap['reinv'])


def add_deal(store, snap, p):
    f = deal_fields(snap, p, False)
    if deal_taken(snap, f['deal_no']):
        raise Refused('Deal #%s already exists.' % f['deal_no'])
    snap['deals'].append(f)
    return {'row': len(snap['deals']) + 1}, ['deals'], 'Add deal #' + f['deal_no']


def update_deal(store, snap, p):
    i = at_row(snap['deals'], p.get('row'), 'deal_no', p.get('orig'))
    old = snap['deals'][i]
    f = deal_fields(snap, p, True)
    if not same(f['deal_no'], old['deal_no']):
        if deal_taken(snap, f['deal_no']):
            raise Refused('Deal #%s already exists.' % f['deal_no'])
        if deal_in_reinvestment(snap, old['deal_no']):
            raise Refused('Deal #%s is part of a reinvestment, so its number cannot change.' % old['deal_no'])
    if f['return_received'] == 'Yes' and not f['received_date']:
        f['received_date'] = old['received_date'] or dt.date.today().isoformat()
    snap['deals'][i] = f
    return {'row': i + 2}, ['deals'], 'Edit deal #%s%s' % (f['deal_no'], what_changed('deals', old, f))


def mark_received(store, snap, p):
    i = at_row(snap['deals'], p.get('row'), 'deal_no', p.get('deal'))
    d = snap['deals'][i]
    d.update(status='Closed', logistics=v_num(p.get('logistics'), 'Logistics cost'), return_received='Yes',
             received_date=v_date(p.get('date'), 'received date', required=False) or dt.date.today().isoformat())
    return {'row': i + 2}, ['deals'], 'Mark deal #%s received' % d['deal_no']


def delete_deal(store, snap, p):
    i = at_row(snap['deals'], p.get('row'), 'deal_no', p.get('orig'))
    n = snap['deals'][i]['deal_no']
    if deal_in_reinvestment(snap, n):
        raise Refused('Deal #%s is part of a reinvestment. Change or delete that reinvestment first.' % n)
    del snap['deals'][i]
    return {}, ['deals'], 'Delete deal #' + n


def cust_fields(snap, p, skip=None):
    name = ' '.join(v_text(p.get('name')).split())
    if not name:
        raise Refused('Enter the customer name.')
    if any(c['name'].lower() == name.lower() and c is not skip for c in snap['cust']):
        raise Refused('There is already a customer called %s.' % name)
    return {'name': name, 'phone': v_text(p.get('phone')).strip()[:100], 'notes': v_text(p.get('notes')).strip()}


def add_customer(store, snap, p):
    f = cust_fields(snap, p)
    f['id'] = next_id('CUS', snap['cust'])
    snap['cust'].append(f)
    return {'row': len(snap['cust']) + 1, 'id': f['id']}, ['cust'], 'Add customer %s (%s)' % (f['id'], f['name'])


def update_customer(store, snap, p):
    i = at_row(snap['cust'], p.get('row'), 'id', p.get('orig'))
    c = snap['cust'][i]
    old = dict(c)
    c.update(cust_fields(snap, p, skip=c))
    return {'row': i + 2, 'id': c['id']}, ['cust'], 'Edit customer %s%s' % (c['id'], what_changed('cust', old, c))


def delete_customer(store, snap, p):
    i = at_row(snap['cust'], p.get('row'), 'id', p.get('orig'))
    c = snap['cust'][i]
    if any(a['customer_id'] == c['id'] for a in snap['arr']):
        raise Refused('%s still has arrangements. Delete them or move them to another customer first.' % c['name'])
    del snap['cust'][i]
    return {}, ['cust'], 'Delete customer %s (%s)' % (c['id'], c['name'])


def arr_fields(snap, p):
    """the arrangement's fields, and whether a new customer was added for it"""
    cid, new = v_text(p.get('custId')).strip(), ' '.join(v_text(p.get('newCust')).split())
    added = []
    if cid:
        if not any(c['id'] == cid for c in snap['cust']):
            raise Refused('Customer %s was not found.' % cid)
    elif new:
        c = next((c for c in snap['cust'] if c['name'].lower() == new.lower()), None)
        if not c:
            c = {'id': next_id('CUS', snap['cust']), 'name': new, 'phone': '', 'notes': ''}
            snap['cust'].append(c)
            added = ['cust']
        cid = c['id']
    else:
        raise Refused('Pick a customer.')
    term = p.get('term')
    proj = v_one(p.get('projection') or 'latest', PROJECTIONS, 'Projection')
    return {
        'customer_id': cid, 'label': ' '.join(v_text(p.get('label')).split())[:100],
        'type': v_text(p.get('type')).strip() or 'One-time',
        'term_months': '' if term in (None, '') else v_num(term, 'Term'),
        'projection': proj,
        'cycle_profit': v_num(p.get('cycleProfit'), 'Profit per cycle') if proj == 'fixed' else '',
    }, added


def add_arrangement(store, snap, p):
    aid = v_text(p.get('id')).strip()
    if not aid:
        raise Refused('Enter an ID.')
    if any(a['id'].lower() == aid.lower() for a in snap['arr']):
        raise Refused('The ID %s is already used.' % aid)
    f, added = arr_fields(snap, p)
    f.update(id=aid, contract_file='', contract_url='')
    snap['arr'].append(f)
    return {'row': len(snap['arr']) + 1}, ['arr'] + added, 'Add arrangement ' + aid


def update_arrangement(store, snap, p):
    i = at_row(snap['arr'], p.get('row'), 'id', p.get('orig'))
    a = snap['arr'][i]
    old = dict(a)
    f, added = arr_fields(snap, p)
    a.update(f)
    return {'row': i + 2}, ['arr'] + added, 'Edit arrangement %s%s' % (a['id'], what_changed('arr', old, a))


def delete_arrangement(store, snap, p):
    i = at_row(snap['arr'], p.get('row'), 'id', p.get('orig'))
    aid = snap['arr'][i]['id']
    if any(d['arrangement_id'] == aid for d in snap['deals']):
        raise Refused('%s still has deals. Delete them or move them to another arrangement first.' % aid)
    del snap['arr'][i]
    return {}, ['arr'], 'Delete arrangement ' + aid


def contract_name(a, cust, ext):
    name = '%s contract - %s' % (a['id'], cust)
    return re.sub(r'[\\/:*?"<>|\x00-\x1f]', ' ', name).strip().lstrip('.') + ext


def upload_contract(store, snap, p):
    i = at_row(snap['arr'], p.get('row'), 'id', p.get('orig'))
    a = snap['arr'][i]
    try:
        data = base64.b64decode(str(p.get('data') or ''), validate=True)
    except ValueError:
        raise Refused('The file could not be read.')
    if not data:
        raise Refused('No file was received.')
    if len(data) > MAX_CONTRACT:
        raise Refused('Contract files must be 15 MB or smaller.')
    ext = os.path.splitext(str(p.get('name') or ''))[1].lower()
    if ext not in CONTRACT_EXTS:
        raise Refused('Contracts must be a PDF, a Word file or a photo (JPG or PNG).')
    name = contract_name(a, arr_names(snap)[a['id']], ext)
    write_contract(store, name, data)
    old = a['contract_file']
    if old and old != name and os.path.isfile(os.path.join(store.contracts, old)):
        os.remove(os.path.join(store.contracts, old))
    a.update(contract_file=name, contract_url='')  # the backup copies it to Drive and fills the link
    return {'row': i + 2, 'url': '/contracts/' + quote(name)}, ['arr'], 'Save the contract for ' + a['id']


def write_contract(store, name, data):
    fd, tmp = tempfile.mkstemp(dir=store.contracts, prefix='.upload-', suffix='.tmp')
    with os.fdopen(fd, 'wb') as f:
        f.write(data)
    os.replace(tmp, os.path.join(store.contracts, name))


def reinv_fields(snap, p):
    f = {
        'date': v_date(p.get('date'), 'date'),
        'source_deal_no': v_num(p.get('deal'), 'Source deal #'),
        'amount': v_num(p.get('amount'), 'Amount'),
        'destination_deal_no': '', 'destination': '',
        'expected_return_date': v_date(p.get('due'), 'expected return date', required=False),
        'return_received': v_one(p.get('recv'), ['Yes', 'No'], 'Return received'),
        'notes': v_text(p.get('notes')).strip(),
    }
    if p.get('destDeal') not in (None, ''):
        f['destination_deal_no'] = v_num(p.get('destDeal'), 'Destination deal #')
        if not deal_taken(snap, f['destination_deal_no']):
            raise Refused('Deal #%s was not found.' % f['destination_deal_no'])
    else:
        f['destination'] = v_text(p.get('dest')).strip()
        if not f['destination']:
            raise Refused('Pick the deal it went into, or describe where it went.')
    return f


def reinv_row(snap, p):
    o = p.get('orig') or {}
    try:
        i = int(p.get('row')) - 2
    except (TypeError, ValueError):
        i = -1
    if i < 0 or i >= len(snap['reinv']):
        raise Refused('That row was not found. Refresh and try again.')
    r = snap['reinv'][i]
    if fmt_date(r['date']) != str(o.get('date') or '').strip() or not same(r['amount'], o.get('amount')):
        raise Refused(CHANGED)
    return i


def add_reinvestment(store, snap, p):
    snap['reinv'].append(reinv_fields(snap, p))
    return {'row': len(snap['reinv']) + 1}, ['reinv'], 'Add a reinvestment from deal #' + snap['reinv'][-1]['source_deal_no']


def update_reinvestment(store, snap, p):
    i = reinv_row(snap, p)
    old, f = snap['reinv'][i], reinv_fields(snap, p)
    snap['reinv'][i] = f
    return {'row': i + 2}, ['reinv'], 'Edit a reinvestment from deal #%s%s' % (f['source_deal_no'], what_changed('reinv', old, f))


def delete_reinvestment(store, snap, p):
    r = snap['reinv'].pop(reinv_row(snap, p))
    return {}, ['reinv'], 'Delete a reinvestment from deal #' + r['source_deal_no']


ACTIONS = {
    'addCustomer': add_customer, 'updateCustomer': update_customer, 'deleteCustomer': delete_customer,
    'addArrangement': add_arrangement, 'updateArrangement': update_arrangement, 'deleteArrangement': delete_arrangement,
    'uploadContract': upload_contract,
    'addDeal': add_deal, 'updateDeal': update_deal, 'markReceived': mark_received, 'deleteDeal': delete_deal,
    'addReinvestment': add_reinvestment, 'updateReinvestment': update_reinvestment, 'deleteReinvestment': delete_reinvestment,
}


# ---------- the sheet's tabs, with its formula columns worked out here ----------
def arr_names(snap):
    """arrangement ID -> the name shown for it: the customer, with the label in brackets"""
    custs = {c['id']: c['name'] for c in snap['cust']}
    out = {}
    for a in snap['arr']:
        name = custs.get(a['customer_id'], '')
        out[a['id']] = '%s (%s)' % (name, a['label']) if a['label'] else name
    return out


def build(snap, for_sheet=False):
    names = arr_names(snap)
    deals = []
    for d in snap['deals']:
        sell, dist, inv, logi = (typed(d[k]) for k in ('selling_price', 'distributor_price', 'invested_amount', 'logistics'))
        profit = (sell or 0) - (dist or 0)
        agreed = (dist or 0) + profit if d['return_type'] == 'Profit + Principal' else profit
        deals.append([
            typed(d['deal_no']), d['arrangement_id'], names.get(d['arrangement_id'], ''), d['status'], d['investment_date'],
            typed(d['mrp']), sell, dist, profit, inv, d['amount_sent'], d['transaction_no'], d['return_type'], agreed,
            d['tentative_return_date'], logi, agreed - logi if logi != '' else '', d['return_received'], d['notes'],
            'Invested differs from distributor price' if inv != '' and inv != dist else '', d['received_date'],
        ])
    arrs = []
    for a in snap['arr']:
        mine = [r for r in deals if r[1] == a['id']]
        dates = [r[4] for r in mine if r[4]]
        start = min(dates) if dates else ''
        term = typed(a['term_months'])
        if for_sheet:
            link = a['contract_url'] or a['contract_file']
        else:
            link = '/contracts/' + quote(a['contract_file']) if a['contract_file'] else a['contract_url']
        arrs.append([
            a['id'], names[a['id']], a['type'], term, start, edate(start, int(term)) if start and term else '',
            len(mine), sum(r[13] for r in mine), sum(r[16] or 0 for r in mine if r[17] == 'Yes'),
            sum(r[13] for r in mine if r[17] == 'No'), link,
            a['customer_id'], a['label'], a['projection'] or 'latest', typed(a['cycle_profit']),
        ])
    by_no = {canon(num(r[0])): r for r in deals}
    reinv = []
    for r in snap['reinv']:
        dest = r['destination']
        target = by_no.get(r['destination_deal_no'])
        if r['destination_deal_no']:
            dest = 'Deal #%s, %s' % (r['destination_deal_no'], target[2] if target else 'not found')
        reinv.append([r['date'], typed(r['source_deal_no']), typed(r['amount']), dest, r['expected_return_date'],
                      r['return_received'], r['notes'], typed(r['destination_deal_no'])])
    cust = []
    for c in snap['cust']:
        ids = [a['id'] for a in snap['arr'] if a['customer_id'] == c['id']]
        cust.append([c['id'], c['name'], c['phone'], c['notes'], len(ids), sum(1 for d in deals if d[1] in ids)])
    return {'cust': cust, 'arr': arrs, 'deals': deals, 'reinv': reinv}


def display(tabs):
    """rows as the page shows them: a header row, then dd.mm.yyyy dates and 1,234 numbers"""
    out = {}
    for name, rows in tabs.items():
        cols = SHEET[name][1]
        out[name] = [[c[0] for c in cols]] + [
            [fmt_date(v) if k == 'd' else fmt_num(v) if k == 'n' else v for v, (_, k) in zip(r, cols)] for r in rows]
    return out


def mirror(snap):
    """what the backup writes into each tab of the Google Sheet"""
    tabs = build(snap, for_sheet=True)
    return {name: {'tab': SHEET[name][0], 'head': [c[0] for c in SHEET[name][1]],
                   'kinds': [c[1] for c in SHEET[name][1]], 'rows': rows} for name, rows in tabs.items()}


def from_sheet(values):
    """sheet display values ({cust, arr, deals, reinv}, each with its header row) -> CSV rows.
    Reads the current layout and the first one (no Customers tab, fewer columns)."""
    def rows(v, ok):
        out = []
        for r in (v or [])[1:]:
            r = [str(x if x is not None else '').strip() for x in r] + [''] * 24
            if ok(r):
                out.append(r)
        return out

    def n(s):
        return canon(num(s))

    def d(s):
        return parse_date(s) or ''

    cust = [{'id': r[0], 'name': r[1], 'phone': r[2], 'notes': r[3]} for r in rows(values.get('cust'), lambda r: r[0])]
    known = {c['id'] for c in cust}
    arr = []
    for r in rows(values.get('arr'), lambda r: r[0]):
        a = {'id': r[0], 'type': r[2], 'term_months': n(r[3]), 'projection': r[13] if r[13] in PROJECTIONS else 'latest',
             'cycle_profit': n(r[14]), 'contract_file': '' if r[10].startswith('http') else r[10],
             'contract_url': r[10] if r[10].startswith('http') else ''}
        if r[11] in known:
            a.update(customer_id=r[11], label=r[12])
        else:
            a['customer'] = r[1]
        arr.append(a)
    attach_customers(arr, cust)
    deals = [{'deal_no': n(r[0]), 'arrangement_id': r[1], 'status': r[3] or 'Active', 'investment_date': d(r[4]),
              'mrp': n(r[5]), 'selling_price': n(r[6]), 'distributor_price': n(r[7]), 'invested_amount': n(r[9]),
              'amount_sent': r[10] or 'No', 'transaction_no': r[11], 'return_type': r[12] or 'Profit Only',
              'tentative_return_date': d(r[14]), 'logistics': n(r[15]), 'return_received': r[17] or 'No',
              'notes': r[18], 'received_date': d(r[20]) or (d(r[14]) if r[17] == 'Yes' else '')}
             for r in rows(values.get('deals'), lambda r: r[0])]
    reinv = []
    for r in rows(values.get('reinv'), lambda r: r[0] or r[2]):
        dest_no = n(r[7]) or dest_deal_of(r[3])
        reinv.append({'date': d(r[0]), 'source_deal_no': n(r[1]), 'amount': n(r[2]), 'destination_deal_no': dest_no,
                      'destination': '' if dest_no else r[3], 'expected_return_date': d(r[4]),
                      'return_received': r[5] or 'No', 'notes': r[6]})
    return {'cust': cust, 'arr': arr, 'deals': deals, 'reinv': reinv}


# ---------- settings: backup connection and reminders ----------
class Settings:
    def __init__(self, root):
        self.path = os.path.join(root, 'settings.json')
        self.lock = threading.Lock()

    def get(self):
        try:
            with open(self.path, encoding='utf-8') as f:
                s = json.load(f)
            return s if isinstance(s, dict) else {}
        except (OSError, ValueError):
            return {}

    def update(self, **changes):
        with self.lock:
            s = self.get()
            s.update(changes)
            fd, tmp = tempfile.mkstemp(dir=os.path.dirname(self.path), prefix='.settings-', suffix='.tmp')
            with os.fdopen(fd, 'w') as f:
                json.dump(s, f)
            os.chmod(tmp, 0o600)
            os.replace(tmp, self.path)
            return s


def connected(s):
    return bool(s.get('url') and s.get('key'))


# ---------- Google Sheet backup ----------
def now_text():
    return dt.datetime.now().replace(microsecond=0).isoformat()


class Backup:
    def __init__(self, store, root, delay=3.0, settings=None):
        self.store = store
        self.settings = settings or Settings(root)
        self.report_path = os.path.join(root, 'report.json')
        self.delay = delay
        self.retry_wait = 3.0
        self.cond = threading.Condition()
        self.wanted = False
        self.force = False
        self.sent = None
        self.script = None  # the version the sheet script last reported
        self.status = {'state': 'idle', 'last': '', 'error': ''}
        try:
            with open(self.report_path, encoding='utf-8') as f:
                self.report = json.load(f)
        except (OSError, ValueError):
            self.report = None

    def public(self):
        s = self.settings.get()
        out = dict(self.status, configured=connected(s), url=s.get('url', ''), notice='')
        if not connected(s):
            out['state'] = 'off'
        elif self.script and self.script < LATEST_SCRIPT:
            out['notice'] = ('The sheet script is one version behind, so the Customers tab is not copied and Drive '
                             'contracts are not downloaded. Copy the script again, paste it into Apps Script, save, '
                             'and deploy a new version. The key stays the same.')
        return out

    def call(self, s, action, payload=None):
        try:
            return self._call(s, action, payload)
        except BackupError as e:
            if not e.retry:
                raise
            time.sleep(self.retry_wait)
            return self._call(s, action, payload)

    def _call(self, s, action, payload):
        body = dict(payload or {}, action=action, key=s['key'])
        req = urllib.request.Request(s['url'], data=json.dumps(body).encode('utf-8'), method='POST',
                                     headers={'Content-Type': 'text/plain;charset=utf-8'})
        try:
            # Apps Script answers with a redirect; urllib follows it with a GET, which is what Google expects
            with urllib.request.urlopen(req, timeout=120) as r:
                text = r.read().decode('utf-8', 'replace')
        except urllib.error.HTTPError as e:
            raise BackupError('Google answered with error %d. Check the web app URL, and that it is deployed '
                              'with access for Anyone.' % e.code, retry=e.code >= 500)
        except (urllib.error.URLError, OSError):
            raise BackupError('Could not reach Google. Check the internet connection.', retry=True)
        try:
            j = json.loads(text)
        except ValueError:
            raise BackupError('That web app URL did not answer like the backup script. Check it is the /exec URL '
                              'from Deploy, with access set to Anyone.')
        if not j.get('ok'):
            err = str(j.get('error') or 'an error')
            if err == 'bad_key':
                raise BackupError('The key does not match the one in the sheet script. Paste the key from setup.', retry=True)
            if err == 'Unknown action.':
                raise BackupError('The sheet script is out of date. Paste the new script into Apps Script and '
                                  'deploy a new version.')
            raise BackupError('The sheet script reported: ' + err)
        if (j.get('version') or 0) < SCRIPT_VERSION:
            raise BackupError('The sheet script is out of date. Paste the new script into Apps Script and deploy '
                              'a new version.')
        self.script = j.get('version')
        return j

    def save_settings(self, url, key):
        url, key = v_text(url).strip(), v_text(key).strip()
        if not re.match(r'^https://script\.google(usercontent)?\.com/', url):
            raise Refused('Paste the web app URL from Deploy. It starts with https://script.google.com/')
        key = key or self.settings.get().get('key', '')
        if len(key) < 16:
            raise Refused('Paste the key from the execution log after running setup.')
        try:
            self.call({'url': url, 'key': key}, 'ping')
        except BackupError as e:
            raise Refused(str(e))
        self.settings.update(url=url, key=key)
        self.request(force=True)
        return {'sync': self.public()}

    def set_report(self, report):
        if not isinstance(report, dict) or not isinstance(report.get('rows'), list):
            raise Refused('The report was not readable.')
        if report != self.report:
            self.report = report
            with open(self.report_path, 'w', encoding='utf-8') as f:
                json.dump(report, f)
            self.request()
        return {'sync': self.public()}

    def request(self, force=False):
        with self.cond:
            self.wanted = True
            self.force = self.force or force
            if connected(self.settings.get()):
                self.status = dict(self.status, state='pending')
            self.cond.notify()

    def start(self):
        threading.Thread(target=self._loop, name='backup', daemon=True).start()

    def _loop(self):
        while True:
            with self.cond:
                while not self.wanted:
                    # a failed backup is tried again every 15 minutes
                    if not self.cond.wait(timeout=900) and self.status['state'] == 'error':
                        self.wanted = True
                self.cond.wait(timeout=self.delay)  # let a burst of changes settle into one backup
                self.wanted, force, self.force = False, self.force, False
            try:
                self.run(force)
            except Exception as e:  # keep the backup thread alive whatever happens
                traceback.print_exc()
                self.status = dict(self.status, state='error', error='Unexpected error: %s' % e)

    def run(self, force=False):
        s = self.settings.get()
        if not connected(s):
            self.status = dict(self.status, state='off')
            return
        try:
            if self.script is None:
                self.call(s, 'ping')  # learn which script version is deployed
            self._upload_contracts(s)
            payload = mirror(self.store.snapshot())
            payload['report'] = self.report
            digest = hashlib.sha256(json.dumps(payload, sort_keys=True).encode('utf-8')).hexdigest()
            if force or digest != self.sent:
                self.call(s, 'sync', payload)
                self.sent = digest
            if self.script >= LATEST_SCRIPT:
                self._download_contracts(s)
            self.status = {'state': 'ok', 'last': now_text(), 'error': ''}
        except BackupError as e:
            self.status = dict(self.status, state='error', error=str(e))

    def _upload_contracts(self, s):
        """copies contracts added in the app to the WAGTales Contracts folder in Drive"""
        for a in self.store.snapshot()['arr']:
            local = os.path.join(self.store.contracts, a['contract_file']) if a['contract_file'] else ''
            if local and os.path.isfile(local) and not a['contract_url']:
                with open(local, 'rb') as f:
                    data = base64.b64encode(f.read()).decode('ascii')
                j = self.call(s, 'saveContract', {'name': a['contract_file'], 'data': data,
                                                  'mimeType': mimetypes.guess_type(local)[0] or 'application/octet-stream'})
                if j.get('url'):
                    self.store.set_contract(a['id'], contract_url=j['url'])

    def _download_contracts(self, s):
        """keeps a copy on this Mac of contracts that are only in Drive"""
        snap = self.store.snapshot()
        names = arr_names(snap)
        for a in snap['arr']:
            local = os.path.join(self.store.contracts, a['contract_file']) if a['contract_file'] else ''
            if a['contract_url'] and not (local and os.path.isfile(local)):
                j = self.call(s, 'getContract', {'url': a['contract_url']})
                ext = os.path.splitext(j.get('name') or '')[1].lower()
                if j.get('data') and ext in CONTRACT_EXTS:
                    name = contract_name(a, names[a['id']], ext)
                    write_contract(self.store, name, base64.b64decode(j['data']))
                    self.store.set_contract(a['id'], contract_file=name)


# ---------- Mac notifications ----------
def mac_notify(message):
    quoted = '"%s"' % message.replace('\\', '\\\\').replace('"', '\\"')
    subprocess.run(['osascript', '-e', 'display notification %s with title "WAGTales"' % quoted],
                   capture_output=True, timeout=20)


class Reminders:
    """once a day per item: returns due tomorrow or today, overdue returns, money not sent, a failing backup"""

    def __init__(self, store, backup, root, notify=mac_notify, today=dt.date.today):
        self.store, self.backup = store, backup
        self.path = os.path.join(root, 'reminders.json')
        self.notify, self.today = notify, today

    def on(self):
        return self.backup.settings.get().get('notify', True) is not False

    def items(self, today):
        out = []
        for r in build(self.store.snapshot())['deals']:
            n, name, amount = r[0], r[2], fmt_num(r[13])
            if r[17] != 'Yes' and r[14]:
                days = (dt.date.fromisoformat(r[14]) - today).days
                if days == 1:
                    out.append(('due:%s:%s' % (n, r[14]), 'Deal #%s, %s: the return of %s is due tomorrow.' % (n, name, amount)))
                elif days == 0:
                    out.append(('due:%s:%s' % (n, r[14]), 'Deal #%s, %s: the return of %s is due today.' % (n, name, amount)))
                elif days < 0:
                    out.append(('late:%s' % n, 'Deal #%s, %s: the return of %s was due on %s.' % (n, name, amount, fmt_date(r[14]))))
            if r[10] == 'No' and r[3] == 'Active':
                out.append(('sent:%s' % n, 'Deal #%s, %s: the money is not marked as sent yet.' % (n, name)))
        b = self.backup.public()
        if b['configured'] and b['state'] == 'error':
            out.append(('backup', 'The Google Sheet backup is failing. Open the tracker to see why.'))
        return out

    def check(self):
        if not self.on():
            return 0
        today = self.today().isoformat()
        try:
            with open(self.path, encoding='utf-8') as f:
                sent = json.load(f)
        except (OSError, ValueError):
            sent = {}
        shown = 0
        for key, message in self.items(self.today()):
            if sent.get(key) != today:
                self.notify(message)
                sent[key] = today
                shown += 1
        sent = {k: v for k, v in sent.items() if v == today}  # forget older days
        with open(self.path, 'w', encoding='utf-8') as f:
            json.dump(sent, f)
        return shown

    def start(self):
        def loop():
            time.sleep(60)
            while True:
                try:
                    self.check()
                except Exception:
                    traceback.print_exc()
                time.sleep(3600)
        threading.Thread(target=loop, name='reminders', daemon=True).start()


# ---------- web server ----------
class App(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, addr, store, backup, reminders=None):
        super().__init__(addr, Handler)
        self.store, self.backup = store, backup
        self.reminders = reminders or Reminders(store, backup, store.root, notify=lambda m: None)


class Handler(BaseHTTPRequestHandler):
    server_version = 'WAGTales'

    def log_message(self, fmt, *args):
        pass

    def send(self, code, body, ctype='application/json; charset=utf-8'):
        if not isinstance(body, bytes):
            body = json.dumps(body).encode('utf-8')
        self.send_response(code)
        self.send_header('Content-Type', ctype)
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.end_headers()
        self.wfile.write(body)

    def send_file(self, path, ctype):
        try:
            with open(path, 'rb') as f:
                body = f.read()
        except OSError:
            return self.send(404, {'ok': False, 'error': 'Not found.'})
        self.send(200, body, ctype)

    def local(self):
        # only answer to this Mac by name, so other websites cannot reach the app through DNS tricks
        host = (self.headers.get('Host') or '').lower()
        host = host.rsplit(':', 1)[0] if not host.endswith(']') else host
        return host.strip('[]') in ('127.0.0.1', 'localhost', '::1')

    def do_GET(self):
        if not self.local():
            return self.send(403, {'ok': False, 'error': 'Forbidden.'})
        store, backup = self.server.store, self.server.backup
        path = urlparse(self.path).path
        if path in ('/', '/index.html'):
            return self.send_file(os.path.join(APP_DIR, 'index.html'), 'text/html; charset=utf-8')
        if path == '/apps-script.gs':
            return self.send_file(os.path.join(APP_DIR, 'apps-script.gs'), 'text/plain; charset=utf-8')
        if path == '/api/data':
            out = display(build(store.snapshot()))
            out.update(ok=True, sync=backup.public(), reminders={'on': self.server.reminders.on()})
            return self.send(200, out)
        if path == '/api/status':
            return self.send(200, {'ok': True, 'sync': backup.public()})
        if path == '/api/history':
            return self.send(200, {'ok': True, 'history': store.history()})
        if path.startswith('/contracts/'):
            name = unquote(path[len('/contracts/'):])
            if name and name == os.path.basename(name) and not name.startswith('.'):
                p = os.path.join(store.contracts, name)
                if os.path.isfile(p):
                    return self.send_file(p, mimetypes.guess_type(name)[0] or 'application/octet-stream')
        self.send(404, {'ok': False, 'error': 'Not found.'})

    def do_POST(self):
        # the custom header makes browsers refuse cross-site requests, which cannot send it without permission
        if not self.local() or urlparse(self.path).path != '/api' or self.headers.get('X-WagTales') != '1':
            return self.send(403, {'ok': False, 'error': 'Forbidden.'})
        try:
            n = int(self.headers.get('Content-Length') or 0)
        except ValueError:
            n = -1
        if n < 0 or n > MAX_BODY:
            return self.send(413, {'ok': False, 'error': 'That request is too large.'})
        try:
            p = json.loads(self.rfile.read(n).decode('utf-8') or '{}')
            if not isinstance(p, dict):
                raise ValueError
        except ValueError:
            return self.send(400, {'ok': False, 'error': 'The request was not readable.'})
        store, backup = self.server.store, self.server.backup
        action = p.get('action')
        try:
            if action == 'report':
                out = backup.set_report(p.get('report'))
            elif action == 'syncNow':
                backup.request(force=True)
                out = {'sync': backup.public()}
            elif action == 'saveSettings':
                out = backup.save_settings(p.get('url'), p.get('key'))
            elif action == 'setReminders':
                backup.settings.update(notify=bool(p.get('on')))
                out = {'reminders': {'on': self.server.reminders.on()}}
            elif action == 'restore':
                out = store.restore(str(p.get('hash') or ''))
                backup.request()
            else:
                out = store.act(action, p)
                backup.request()
            out['ok'] = True
            self.send(200, out)
        except Refused as e:
            self.send(200, {'ok': False, 'error': str(e)})
        except Exception as e:
            traceback.print_exc()
            self.send(500, {'ok': False, 'error': 'The app hit an unexpected error: %s' % e})


def main(argv=None):
    ap = argparse.ArgumentParser(description='WAGTales tracker')
    ap.add_argument('--data', default=os.path.expanduser('~/WAGTales/data'), help='folder for the CSV files')
    ap.add_argument('--host', default='127.0.0.1')
    ap.add_argument('--port', type=int, default=8765)
    ap.add_argument('--import', dest='import_file', help='fill an empty data folder from a JSON file of sheet values')
    ap.add_argument('--restore', action='store_true', help='fill an empty data folder from the Google Sheet backup')
    args = ap.parse_args(argv)
    os.makedirs(args.data, exist_ok=True)
    store = Store(args.data)
    backup = Backup(store, args.data)
    if args.import_file:
        with open(args.import_file, encoding='utf-8') as f:
            store.seed(from_sheet(json.load(f)), 'Import from the Google Sheet')
        print('Imported into', args.data)
        return
    if args.restore:
        s = backup.settings.get()
        if not connected(s):
            raise SystemExit('The backup is not set up in this data folder (settings.json is missing).')
        store.seed(from_sheet(backup.call(s, 'read')), 'Restore from the Google Sheet backup')
        print('Restored into', args.data)
        return
    reminders = Reminders(store, backup, args.data)
    httpd = App((args.host, args.port), store, backup, reminders)
    backup.start()
    backup.request()
    reminders.start()
    print('WAGTales is running at http://%s:%d with data in %s' % (args.host, args.port, args.data), flush=True)
    httpd.serve_forever()


if __name__ == '__main__':
    main()
