#!/usr/bin/env python3
"""WAGTales tracker: the app that runs on this Mac.

Serves the tracker page at http://127.0.0.1:8765 and keeps everything, contract files
included, in one SQLite database in the data folder (default ~/WAGTales/data). Every
change is recorded in that database, so the page can show the history and restore any
point. When a backup folder is set, a copy of the database is saved there after each
change. The app also shows Mac notifications for returns that are due and money not sent.

    python3 server.py [--data DIR] [--port 8765]
    python3 server.py --import FILE.json   fill an empty database from a JSON file of records
    python3 server.py --restore FILE.db    fill an empty data folder from a backup copy

Standard library only, compatible with the /usr/bin/python3 (3.9) that launchd runs.
"""
import argparse
import base64
import calendar
import datetime as dt
import hashlib
import json
import math
import mimetypes
import os
import re
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import quote, unquote, urlparse

APP_DIR = os.path.dirname(os.path.abspath(__file__))
MAX_CONTRACT = 15 * 1024 * 1024
MAX_BODY = 25 * 1024 * 1024
CONTRACT_EXTS = ('.pdf', '.doc', '.docx', '.jpg', '.jpeg', '.png')
CHANGED = 'The data changed since the page loaded. Refresh and try again.'
DB_NAME = 'wagtales.db'
DB_VERSION = 1
KEEP_BACKUPS = 30  # daily copies kept in the backup folder
BACKUP_FILE = re.compile(r'^wagtales-\d{4}-\d\d-\d\d\.db$')

# ---------- database tables: input columns only, ISO dates, plain numbers, all kept as text ----------
TABLES = {
    'cust': ('customers', ['id', 'name', 'phone', 'notes']),
    'arr': ('arrangements', ['id', 'customer_id', 'label', 'type', 'term_months', 'projection', 'cycle_profit',
                                 'contract_file']),
    'deals': ('deals', ['deal_no', 'arrangement_id', 'status', 'investment_date', 'mrp', 'selling_price',
                            'distributor_price', 'invested_amount', 'amount_sent', 'transaction_no', 'return_type',
                            'tentative_return_date', 'logistics', 'return_received', 'notes', 'received_date']),
    'reinv': ('reinvestments', ['date', 'source_deal_no', 'amount', 'destination_deal_no', 'destination',
                                    'expected_return_date', 'return_received', 'notes']),
}
PROJECTIONS = ['latest', 'fixed', 'none']
FIELD_NAMES = {'deal_no': 'deal #', 'arrangement_id': 'arrangement', 'investment_date': 'date', 'mrp': 'MRP',
               'tentative_return_date': 'return date', 'customer_id': 'customer', 'term_months': 'term',
               'cycle_profit': 'profit per cycle', 'contract_file': 'contract', 'source_deal_no': 'source deal',
               'destination_deal_no': 'destination', 'expected_return_date': 'expected return date'}

# ---------- the columns the page reads by position. Kinds: t text, n number, d date ----------
# Later additions go on the end.
COLUMNS = {
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
    """a number as the database stores it: 12345, 0.5"""
    if n is None:
        return ''
    if float(n).is_integer():
        return str(int(n))
    return ('%.6f' % n).rstrip('0').rstrip('.')


def typed(s):
    """stored number text -> int or float, or '' when empty"""
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
    """dd.mm.yyyy, M/D/YYYY, yyyy-mm-dd -> ISO date, or None"""
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
    except ValueError:
        return None
    return None


def edate(iso, months):
    """same day k months later, clamped to the end of the month"""
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
    diff = [FIELD_NAMES.get(k, k.replace('_', ' ')) for k in TABLES[name][1] if old.get(k, '') != new.get(k, '')]
    return ' (%s)' % ', '.join(diff) if diff else ' (nothing changed)'


# ---------- the database ----------
class Store:
    def __init__(self, root):
        self.root = root
        self.path = os.path.join(root, DB_NAME)
        self.lock = threading.RLock()
        if not os.path.isfile(self.path):
            self._create()
        self.db = sqlite3.connect(self.path, check_same_thread=False)
        version = self.db.execute('PRAGMA user_version').fetchone()[0]
        if version != DB_VERSION:
            raise SystemExit('%s is version %s, but the app expects %s.' % (self.path, version, DB_VERSION))

    def _create(self):
        # built under another name and renamed, so a failed start never leaves half a database
        tmp = self.path + '.new'
        if os.path.exists(tmp):
            os.remove(tmp)
        self.db = sqlite3.connect(tmp)
        try:
            with self.db:
                for table, cols in TABLES.values():
                    self.db.execute('CREATE TABLE %s (%s)' % (table, ', '.join("%s TEXT NOT NULL DEFAULT \'\'" % c for c in cols)))
                self.db.execute('CREATE TABLE contracts (name TEXT PRIMARY KEY, sha TEXT NOT NULL)')
                # file contents are never removed, because the history can bring them back
                self.db.execute('CREATE TABLE contract_data (sha TEXT PRIMARY KEY, data BLOB NOT NULL)')
                self.db.execute('CREATE TABLE history (id INTEGER PRIMARY KEY AUTOINCREMENT, at TEXT NOT NULL, '
                                'message TEXT NOT NULL, state TEXT NOT NULL)')
                self.db.execute('PRAGMA user_version = %d' % DB_VERSION)
                self._record('Start the WAGTales data history')
            self.db.close()
            os.chmod(tmp, 0o600)
            os.replace(tmp, self.path)
        except BaseException:
            self.db.close()
            if os.path.exists(tmp):
                os.remove(tmp)
            raise

    def load(self, name):
        table, cols = TABLES[name]
        cur = self.db.execute('SELECT %s FROM %s ORDER BY rowid' % (', '.join(cols), table))
        return [dict(zip(cols, r)) for r in cur]

    def snapshot(self):
        with self.lock:
            return {n: self.load(n) for n in TABLES}

    def save(self, name, rows):
        table, cols = TABLES[name]
        self.db.execute('DELETE FROM %s' % table)
        self.db.executemany('INSERT INTO %s (%s) VALUES (%s)' % (table, ', '.join(cols), ', '.join('?' * len(cols))),
                            [[r.get(k) or '' for k in cols] for r in rows])

    # ---------- contract files ----------
    def put_contract(self, name, data):
        sha = hashlib.sha256(data).hexdigest()
        self.db.execute('INSERT OR IGNORE INTO contract_data (sha, data) VALUES (?, ?)', (sha, data))
        self.db.execute('INSERT OR REPLACE INTO contracts (name, sha) VALUES (?, ?)', (name, sha))

    def drop_contract(self, name):
        self.db.execute('DELETE FROM contracts WHERE name = ?', (name,))

    def contract(self, name):
        with self.lock:
            r = self.db.execute('SELECT d.data FROM contracts c JOIN contract_data d ON d.sha = c.sha WHERE c.name = ?',
                                (name,)).fetchone()
        return bytes(r[0]) if r else None

    # ---------- history ----------
    def _record(self, message):
        state = dict((n, self.load(n)) for n in TABLES)
        state['contracts'] = dict(self.db.execute('SELECT name, sha FROM contracts').fetchall())
        state = json.dumps(state, sort_keys=True)
        last = self.db.execute('SELECT state FROM history ORDER BY id DESC LIMIT 1').fetchone()
        if not last or last[0] != state:  # a save that changed nothing is not a change
            at = dt.datetime.now().astimezone().replace(microsecond=0).isoformat()
            self.db.execute('INSERT INTO history (at, message, state) VALUES (?, ?, ?)', (at, message, state))

    def history(self, limit=200):
        with self.lock:
            rows = self.db.execute('SELECT id, at, message FROM history ORDER BY id DESC LIMIT ?', (limit,)).fetchall()
        return [{'id': i, 'when': at, 'message': msg, 'current': n == 0,
                 'restorable': n > 0 and not msg.startswith('Start the')} for n, (i, at, msg) in enumerate(rows)]

    def restore(self, h):
        with self.lock:
            entry = next((e for e in self.history() if str(e['id']) == str(h)), None)
            if not entry or not entry['restorable']:
                raise Refused('That point in the history cannot be restored.')
            state = json.loads(self.db.execute('SELECT state FROM history WHERE id = ?', (entry['id'],)).fetchone()[0])
            when = dt.datetime.fromisoformat(entry['when']).strftime('%d.%m.%Y %H:%M')
            with self.db:
                for n in TABLES:
                    self.save(n, state[n])
                self.db.execute('DELETE FROM contracts')
                self.db.executemany('INSERT INTO contracts (name, sha) VALUES (?, ?)', state['contracts'].items())
                self._record('Restore to how it was after "%s" (%s)' % (entry['message'], when))
        return {}

    # ---------- changes ----------
    def act(self, action, p):
        fn = ACTIONS.get(action)
        if not fn:
            raise Refused('Unknown action.')
        with self.lock, self.db:  # all of a change is saved, or none of it
            snap = {n: self.load(n) for n in TABLES}
            out, changed, message = fn(self, snap, p)
            for n in changed:
                self.save(n, snap[n])
            if changed:
                self._record(message)
        return out

    def seed(self, tabs, message):
        with self.lock, self.db:
            if self.has_records():
                raise SystemExit('The database already has records. Import only fills an empty one.')
            for n in TABLES:
                self.save(n, tabs[n])
            self._record(message)

    def has_records(self):
        with self.lock:
            return any(self.load(n) for n in TABLES)

    def backup_to(self, path):
        with self.lock:
            dest = sqlite3.connect(path)
            try:
                self.db.backup(dest)
                if dest.execute('PRAGMA quick_check').fetchone()[0] != 'ok':
                    raise sqlite3.DatabaseError('the copy failed its check')
            finally:
                dest.close()


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
    f.update(id=aid, contract_file='')
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
    store.put_contract(name, data)
    if a['contract_file'] and a['contract_file'] != name:
        store.drop_contract(a['contract_file'])
    a['contract_file'] = name
    return {'row': i + 2, 'url': '/contracts/' + quote(name)}, ['arr'], 'Save the contract for ' + a['id']


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


# ---------- the tables, with their worked-out columns ----------
def arr_names(snap):
    """arrangement ID -> the name shown for it: the customer, with the label in brackets"""
    custs = {c['id']: c['name'] for c in snap['cust']}
    out = {}
    for a in snap['arr']:
        name = custs.get(a['customer_id'], '')
        out[a['id']] = '%s (%s)' % (name, a['label']) if a['label'] else name
    return out


def build(snap):
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
        link = '/contracts/' + quote(a['contract_file']) if a['contract_file'] else ''
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
        cols = COLUMNS[name][1]
        out[name] = [[c[0] for c in cols]] + [
            [fmt_date(v) if k == 'd' else fmt_num(v) if k == 'n' else v for v, (_, k) in zip(r, cols)] for r in rows]
    return out


# ---------- settings: backup folder and reminders ----------
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


# ---------- backup folder ----------
def now_text():
    return dt.datetime.now().replace(microsecond=0).isoformat()


class Backup:
    def __init__(self, store, root, delay=3.0, settings=None, today=dt.date.today):
        self.store = store
        self.root = root
        self.settings = settings or Settings(root)
        self.delay = delay
        self.today = today
        self.cond = threading.Condition()
        self.wanted = False
        self.status = {'state': 'idle', 'last': '', 'error': ''}

    def folder(self):
        return self.settings.get().get('backup_dir', '')

    def public(self):
        folder = self.folder()
        out = dict(self.status, configured=bool(folder), folder=folder)
        if not folder:
            out['state'] = 'off'
        return out

    def write(self, folder):
        """saves today's copy in the folder, replacing the one from earlier today, and removes the oldest"""
        os.makedirs(folder, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=folder, prefix='.wagtales-', suffix='.tmp')
        os.close(fd)
        try:
            self.store.backup_to(tmp)
            os.chmod(tmp, 0o600)
            os.replace(tmp, os.path.join(folder, 'wagtales-%s.db' % self.today().isoformat()))
        except BaseException:
            if os.path.exists(tmp):
                os.remove(tmp)
            raise
        copies = sorted(f for f in os.listdir(folder) if BACKUP_FILE.match(f))
        for f in copies[:-KEEP_BACKUPS]:
            os.remove(os.path.join(folder, f))

    def save_settings(self, folder):
        folder = os.path.expanduser(v_text(folder).strip())
        if not folder:
            self.settings.update(backup_dir='')
            return {'sync': self.public()}
        if not os.path.isabs(folder):
            raise Refused('Enter the full path of the folder, starting with / or ~/')
        folder = os.path.abspath(folder)
        if os.path.commonpath([folder, os.path.abspath(self.root)]) == os.path.abspath(self.root):
            raise Refused('Choose a folder outside the data folder, or the backup is lost along with the data.')
        try:
            self.write(folder)
        except (OSError, sqlite3.Error) as e:
            raise Refused('Could not save a backup there: %s' % e)
        self.settings.update(backup_dir=folder)
        self.status = {'state': 'ok', 'last': now_text(), 'error': ''}
        return {'sync': self.public()}

    def request(self):
        with self.cond:
            self.wanted = True
            if self.folder():
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
                self.wanted = False
            try:
                self.run()
            except Exception as e:  # keep the backup thread alive whatever happens
                traceback.print_exc()
                self.status = dict(self.status, state='error', error='Unexpected error: %s' % e)

    def run(self):
        folder = self.folder()
        if not folder:
            self.status = dict(self.status, state='off')
            return
        try:
            self.write(folder)
            self.status = {'state': 'ok', 'last': now_text(), 'error': ''}
        except (OSError, sqlite3.Error) as e:
            self.status = dict(self.status, state='error', error='Could not save a backup in %s: %s' % (folder, e))


def restore_backup(src, root):
    """fills an empty data folder from a backup copy"""
    try:
        db = sqlite3.connect('file:%s?mode=ro' % quote(os.path.abspath(src)), uri=True)
        ok = db.execute('PRAGMA quick_check').fetchone()[0] == 'ok' and db.execute('PRAGMA user_version').fetchone()[0] == DB_VERSION
    except sqlite3.Error as e:
        raise SystemExit('%s is not a WAGTales backup: %s' % (src, e))
    if not ok:
        raise SystemExit('%s is not a WAGTales backup this version can read.' % src)
    store = Store(root)
    if store.has_records():
        raise SystemExit('The data folder already has records. Restore only fills an empty one.')
    store.db.close()
    tmp = store.path + '.new'
    dest = sqlite3.connect(tmp)
    db.backup(dest)
    dest.close()
    db.close()
    os.chmod(tmp, 0o600)
    os.replace(tmp, store.path)


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
            out.append(('backup', 'The backup is failing. Open the tracker to see why.'))
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
            body = store.contract(name)
            if body is not None:
                return self.send(200, body, mimetypes.guess_type(name)[0] or 'application/octet-stream')
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
            if action == 'syncNow':
                backup.request()
                out = {'sync': backup.public()}
            elif action == 'saveSettings':
                out = backup.save_settings(p.get('folder'))
            elif action == 'setReminders':
                backup.settings.update(notify=bool(p.get('on')))
                out = {'reminders': {'on': self.server.reminders.on()}}
            elif action == 'restore':
                out = store.restore(str(p.get('id') or ''))
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
    ap.add_argument('--data', default=os.path.expanduser('~/WAGTales/data'), help='folder for the database')
    ap.add_argument('--host', default='127.0.0.1')
    ap.add_argument('--port', type=int, default=8765)
    ap.add_argument('--import', dest='import_file', help='fill an empty database from a JSON file of records')
    ap.add_argument('--restore', help='fill an empty data folder from a backup copy')
    args = ap.parse_args(argv)
    os.makedirs(args.data, exist_ok=True)
    if args.restore:
        restore_backup(args.restore, args.data)
        print('Restored into', args.data, '- if the app is running, restart it with: bash install.sh')
        return
    store = Store(args.data)
    backup = Backup(store, args.data)
    if args.import_file:
        with open(args.import_file, encoding='utf-8') as f:
            store.seed(json.load(f), 'Import')
        print('Imported into', args.data)
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
