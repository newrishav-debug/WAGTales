"""Tests for app/server.py: the database, the page's API, history, reminders and the backup folder.

    python3 test/test_server.py
"""
import base64
import datetime as dt
import http.client
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import threading
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, '..', 'app'))
import server  # noqa: E402

with open(os.path.join(HERE, 'fixtures', 'sample-data.json'), encoding='utf-8') as f:
    SAMPLE = json.load(f)
with open(os.path.join(HERE, 'fixtures', 'expected-view.json'), encoding='utf-8') as f:
    EXPECTED = json.load(f)  # the worked-out columns for the sample data
WIDTH = {'arr': 11, 'deals': 20, 'reinv': 7}  # the columns the expected view has


def padded(tabs):
    return {k: [r + [''] * (WIDTH[k] - len(r)) for r in v] for k, v in tabs.items()}


def original_columns(view):
    return {k: [r[:WIDTH[k]] for r in view[k]] for k in WIDTH}


DEAL = {'n': 6, 'arr': 'ARR-01', 'status': 'Active', 'date': '2026-11-08', 'mrp': 705060, 'sell': 545355,
        'dist': 474849, 'inv': 474849, 'sent': 'No', 'txn': '00123456', 'rtype': 'Profit Only', 'due': '2026-12-07',
        'notes': 'Cycle 3 of 6.'}
EDIT4 = dict(DEAL, row=5, orig='4', n=4, arr='ARR-03', date='2026-10-04', mrp=577200, sell=432900, dist=375180,
             inv=375180, sent='Yes', txn='100000000003', due='2026-11-04', logi='', recv='No', notes='Sent.')


class Base(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix='wagtales-test-')
        self.store = server.Store(self.dir)
        self.store.seed(SAMPLE, 'Import')
        self.backup = server.Backup(self.store, self.dir, delay=0)

    def tearDown(self):
        self.store.db.close()
        shutil.rmtree(self.dir, ignore_errors=True)

    def last_change(self):
        return self.store.history()[0]['message']

    def view(self):
        return server.display(server.build(self.store.snapshot()))


class StoreTests(Base):
    def test_view_matches_the_expected_figures(self):
        self.assertEqual(original_columns(self.view()), padded(EXPECTED))

    def test_customers_come_from_the_arrangement_names(self):
        self.assertEqual([(c['id'], c['name']) for c in self.store.load('cust')],
                         [('CUS-01', 'Sample Breeder'), ('CUS-02', 'Jane Doe'), ('CUS-03', 'Alex Kumar Example')])
        arr = self.store.load('arr')
        self.assertEqual([(a['customer_id'], a['label']) for a in arr],
                         [('CUS-01', ''), ('CUS-02', ''), ('CUS-01', 'litter'), ('CUS-03', '')])
        self.assertEqual(self.view()['cust'][1], ['CUS-01', 'Sample Breeder', '', '', '2', '3'])

    def test_received_dates_and_linked_reinvestments(self):
        deals = self.store.load('deals')
        self.assertEqual([d['received_date'] for d in deals], ['2026-10-03', '2026-10-07', '', '', ''])
        r = self.store.load('reinv')[0]
        self.assertEqual((r['destination_deal_no'], r['destination']), ('4', ''))
        self.assertEqual(self.view()['reinv'][1][3], 'Deal #4, Sample Breeder (litter)')

    def test_database_holds_inputs_only_with_iso_dates(self):
        rows = self.store.db.execute('SELECT * FROM deals ORDER BY rowid').fetchall()
        text = json.dumps(rows, ensure_ascii=False)
        self.assertIn('2026-08-28', text)
        self.assertNotIn('28.08.2026', text)
        self.assertNotIn('70,506', text)  # profit is worked out, not stored
        self.assertIn('100000000004\n100000000005', rows[4])

    def test_database_file_is_private(self):
        self.assertEqual(os.stat(self.store.path).st_mode & 0o777, 0o600)

    def test_edate_clamps_to_month_end(self):
        self.assertEqual(server.edate('2026-08-31', 6), '2027-02-28')
        self.assertEqual(server.edate('2026-10-18', 3), '2027-01-18')

    def test_import_refuses_a_folder_with_data(self):
        with self.assertRaises(SystemExit):
            self.store.seed(SAMPLE, 'Again')

    def test_unknown_database_version_stops_the_app(self):
        self.store.db.execute('PRAGMA user_version = 99')
        with self.assertRaises(SystemExit):
            server.Store(self.dir)
        self.store.db.execute('PRAGMA user_version = %d' % server.DB_VERSION)

    def test_every_change_is_recorded(self):
        before = len(self.store.history())
        self.store.act('markReceived', {'row': 4, 'deal': '3', 'logistics': 0, 'date': '2026-11-06'})
        self.assertEqual(len(self.store.history()), before + 1)
        self.assertEqual(self.last_change(), 'Mark deal #3 received')

    def test_a_refused_change_saves_nothing(self):
        before = self.store.snapshot()
        with self.assertRaises(server.Refused):
            self.store.act('addDeal', dict(DEAL, n=1))
        self.assertEqual(self.store.snapshot(), before)

    def test_a_save_that_changes_nothing_is_not_recorded(self):
        before = len(self.store.history())
        with self.store.lock, self.store.db:
            self.store._record('No change')
        self.assertEqual(len(self.store.history()), before)


class HttpTests(Base):
    def setUp(self):
        super().setUp()
        self.httpd = server.App(('127.0.0.1', 0), self.store, self.backup)
        self.port = self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        super().tearDown()

    def req(self, method, path, body=None, headers=None):
        c = http.client.HTTPConnection('127.0.0.1', self.port, timeout=10)
        h = {'Host': '127.0.0.1:%d' % self.port}
        h.update(headers or {})
        c.request(method, path, body=body, headers=h)
        r = c.getresponse()
        data = r.read()
        c.close()
        return r.status, data

    def post(self, action, **p):
        p['action'] = action
        status, data = self.req('POST', '/api', json.dumps(p), {'Content-Type': 'application/json', 'X-WagTales': '1'})
        self.assertEqual(status, 200, data)
        return json.loads(data)

    def data(self):
        status, body = self.req('GET', '/api/data')
        self.assertEqual(status, 200)
        return json.loads(body)

    def test_page_and_data(self):
        status, body = self.req('GET', '/')
        self.assertEqual(status, 200)
        self.assertIn(b'<title>WAGTales Tracker</title>', body)
        d = self.data()
        self.assertTrue(d['ok'])
        self.assertEqual((len(d['cust']), len(d['arr']), len(d['deals']), len(d['reinv'])), (4, 5, 6, 2))
        self.assertEqual((len(d['arr'][0]), len(d['deals'][0]), len(d['reinv'][0])), (15, 21, 8))
        self.assertEqual(d['sync']['state'], 'off')
        self.assertEqual(d['reminders'], {'on': True})
        self.assertEqual(self.req('GET', '/apps-script.gs')[0], 404)

    def test_other_sites_are_refused(self):
        self.assertEqual(self.req('POST', '/api', json.dumps({'action': 'addDeal'}))[0], 403)  # no header
        self.assertEqual(self.req('GET', '/api/data', headers={'Host': 'evil.example:8765'})[0], 403)
        status, _ = self.req('POST', '/api', '{}', {'X-WagTales': '1', 'Host': 'evil.example'})
        self.assertEqual(status, 403)
        self.assertEqual(self.req('GET', '/contracts/..%2Fwagtales.db')[0], 404)

    def test_add_deal(self):
        r = self.post('addDeal', **DEAL)
        self.assertEqual(r, {'row': 7, 'ok': True})
        row = self.data()['deals'][6]
        self.assertEqual(row[:5], ['6', 'ARR-01', 'Sample Breeder', 'Active', '08.11.2026'])
        self.assertEqual(row[8], '70,506')
        self.assertEqual(row[11], '00123456')  # transaction numbers stay text
        self.assertEqual((row[17], row[20]), ('No', ''))
        arr = self.data()['arr'][1]
        self.assertEqual(arr[6], '3')
        self.assertEqual(arr[9], '141,012')

    def test_deal_checks(self):
        self.assertEqual(self.post('addDeal', **dict(DEAL, n=5))['error'], 'Deal #5 already exists.')
        self.assertIn('Status must be', self.post('addDeal', **dict(DEAL, status='Maybe'))['error'])
        self.assertIn('MRP must be', self.post('addDeal', **dict(DEAL, mrp=''))['error'])
        self.assertIn('MRP must be', self.post('addDeal', **dict(DEAL, mrp=-1))['error'])
        self.assertEqual(self.post('addDeal', **dict(DEAL, arr='ARR-99'))['error'], 'Arrangement ARR-99 was not found.')
        self.assertIn('whole number', self.post('addDeal', **dict(DEAL, n=6.5))['error'])
        self.assertEqual(self.post('addDeal', **dict(DEAL, date='08.11.2026'))['error'], 'Dates must look like 2026-10-04.')
        self.assertEqual(len(self.data()['deals']), 6)

    def test_update_deal(self):
        self.assertEqual(self.post('updateDeal', **EDIT4)['row'], 5)
        row = self.data()['deals'][4]
        self.assertEqual((row[0], row[15], row[18]), ('4', '', 'Sent.'))
        self.assertEqual(self.last_change(), 'Edit deal #4 (notes)')
        self.assertEqual(self.post('updateDeal', **dict(EDIT4, orig='3'))['error'], server.CHANGED)
        self.assertEqual(self.post('updateDeal', **dict(EDIT4, n=1))['error'], 'Deal #1 already exists.')
        self.assertIn('part of a reinvestment', self.post('updateDeal', **dict(EDIT4, n=9))['error'])
        self.assertIn('Logistics must be', self.post('updateDeal', **dict(EDIT4, logi='abc'))['error'])
        self.assertEqual(self.post('updateDeal', **dict(EDIT4, row=99))['error'], 'That row was not found. Refresh and try again.')

    def test_received_date(self):
        self.post('updateDeal', **dict(EDIT4, recv='Yes', logi=600, recvDate='2026-11-05'))
        self.assertEqual(self.data()['deals'][4][20], '05.11.2026')
        self.post('updateDeal', **dict(EDIT4, recv='Yes', logi=600))  # no date sent: the saved one stays
        self.assertEqual(self.data()['deals'][4][20], '05.11.2026')
        self.post('updateDeal', **EDIT4)  # not received any more: the date goes
        self.assertEqual(self.data()['deals'][4][20], '')

    def test_mark_received(self):
        self.post('markReceived', row=5, deal='4', logistics=600, date='2026-11-04')
        row = self.data()['deals'][4]
        self.assertEqual((row[3], row[15], row[16], row[17], row[20]), ('Closed', '600', '57,120', 'Yes', '04.11.2026'))
        self.post('markReceived', row=4, deal='3', logistics=0)
        self.assertEqual(self.data()['deals'][3][20], server.fmt_date(dt.date.today().isoformat()))
        self.assertEqual(self.post('markReceived', row=5, deal='9', logistics=0)['error'], server.CHANGED)
        self.assertIn('Logistics cost must be', self.post('markReceived', row=4, deal='3', logistics='')['error'])

    def test_delete_deal(self):
        self.assertIn('part of a reinvestment', self.post('deleteDeal', row=2, orig='1')['error'])
        self.assertIn('part of a reinvestment', self.post('deleteDeal', row=5, orig='4')['error'])  # the destination
        self.assertTrue(self.post('deleteDeal', row=6, orig='5')['ok'])
        self.assertEqual([r[0] for r in self.data()['deals'][1:]], ['1', '2', '3', '4'])

    def test_customers(self):
        r = self.post('addCustomer', name='Test  Kennel', phone='98300 00000', notes='Breeder')
        self.assertEqual((r['row'], r['id']), (5, 'CUS-04'))
        self.assertEqual(self.data()['cust'][4], ['CUS-04', 'Test Kennel', '98300 00000', 'Breeder', '0', '0'])
        self.assertIn('already a customer called', self.post('addCustomer', name='sample breeder')['error'])
        self.assertEqual(self.post('addCustomer', name=' ')['error'], 'Enter the customer name.')
        self.post('updateCustomer', row=5, orig='CUS-04', name='Test Kennels', phone='', notes='')
        self.assertEqual(self.last_change(), 'Edit customer CUS-04 (name, phone, notes)')
        self.assertIn('already a customer called', self.post('updateCustomer', row=5, orig='CUS-04', name='Jane Doe')['error'])
        self.assertTrue(self.post('updateCustomer', row=5, orig='CUS-04', name='test kennels')['ok'])  # same customer, new case
        self.assertIn('still has arrangements', self.post('deleteCustomer', row=2, orig='CUS-01')['error'])
        self.assertTrue(self.post('deleteCustomer', row=5, orig='CUS-04')['ok'])
        self.assertEqual(len(self.data()['cust']), 4)

    def test_arrangements(self):
        r = self.post('addArrangement', id='ARR-05', newCust='Test Kennel', type='One-time', term='')
        self.assertEqual(r['row'], 6)
        d = self.data()
        self.assertEqual(d['cust'][4][:2], ['CUS-04', 'Test Kennel'])
        self.assertEqual(d['arr'][5][:2] + d['arr'][5][11:], ['ARR-05', 'Test Kennel', 'CUS-04', '', 'latest', ''])
        self.assertEqual(self.post('addArrangement', id='arr-05', custId='CUS-01', type='One-time')['error'], 'The ID arr-05 is already used.')
        self.assertEqual(self.post('addArrangement', id='ARR-06', type='One-time')['error'], 'Pick a customer.')
        self.assertEqual(self.post('addArrangement', id='ARR-06', custId='CUS-99')['error'], 'Customer CUS-99 was not found.')
        # a new customer with a known name joins that customer
        self.post('addArrangement', id='ARR-06', newCust='sample breeder', label='puppies', type='One-time')
        self.assertEqual(self.data()['arr'][6][1], 'Sample Breeder (puppies)')
        self.assertEqual(len(self.data()['cust']), 5)
        self.post('updateArrangement', row=6, orig='ARR-05', custId='CUS-01', label='kennel', type='2-month recurring', term=2,
                  projection='fixed', cycleProfit='20000')
        self.assertEqual(self.data()['arr'][5][:4] + self.data()['arr'][5][11:],
                         ['ARR-05', 'Sample Breeder (kennel)', '2-month recurring', '2', 'CUS-01', 'kennel', 'fixed', '20,000'])
        self.assertIn('Profit per cycle must be', self.post('updateArrangement', row=6, orig='ARR-05', custId='CUS-01', projection='fixed')['error'])
        self.assertIn('Projection must be', self.post('updateArrangement', row=6, orig='ARR-05', custId='CUS-01', projection='maybe')['error'])
        self.assertEqual(self.post('updateArrangement', row=6, orig='ARR-09', custId='CUS-01')['error'], server.CHANGED)
        self.assertIn('still has deals', self.post('deleteArrangement', row=2, orig='ARR-01')['error'])
        self.assertTrue(self.post('deleteArrangement', row=6, orig='ARR-05')['ok'])
        self.assertEqual(len(self.data()['arr']), 6)

    def test_customer_name_flows_to_deals(self):
        self.post('updateCustomer', row=2, orig='CUS-01', name='Sample B.')
        d = self.data()
        self.assertEqual([r[2] for r in d['deals'][1:]], ['Sample B.', 'Jane Doe', 'Sample B.', 'Sample B. (litter)', 'Alex Kumar Example'])
        self.assertEqual(d['reinv'][1][3], 'Deal #4, Sample B. (litter)')

    def test_reinvestments(self):
        base = dict(date='2026-11-08', deal=3, amount=10000, due='', recv='No', notes='')
        self.assertEqual(self.post('addReinvestment', destDeal='5', **base)['row'], 3)
        self.assertEqual(self.data()['reinv'][2][3], 'Deal #5, Alex Kumar Example')
        self.assertEqual(self.post('addReinvestment', destDeal='9', **base)['error'], 'Deal #9 was not found.')
        self.assertIn('describe where it went', self.post('addReinvestment', dest=' ', **base)['error'])
        orig = {'date': '04.10.2026', 'amount': '65,730'}
        self.post('updateReinvestment', row=2, orig=orig, date='2026-10-04', deal=1, amount=65730, dest='Fixed deposit',
                  due='2026-12-01', recv='No', notes='checked')
        self.assertEqual(self.data()['reinv'][1][3:8], ['Fixed deposit', '01.12.2026', 'No', 'checked', ''])
        stale = self.post('updateReinvestment', row=2, orig={'date': '01.01.2026', 'amount': '5'}, date='2026-10-04',
                          deal=1, amount=1, dest='x', recv='No')
        self.assertEqual(stale['error'], server.CHANGED)
        self.assertTrue(self.post('deleteReinvestment', row=3, orig={'date': '08.11.2026', 'amount': '10,000'})['ok'])
        self.assertEqual(len(self.data()['reinv']), 2)

    def test_contract_upload(self):
        pdf = b'%PDF-1.4 test'
        r = self.post('uploadContract', row=4, orig='ARR-03', cust='ignored', name='deal.PDF', mimeType='application/pdf',
                      data=base64.b64encode(pdf).decode())
        self.assertEqual(r['url'], '/contracts/ARR-03%20contract%20-%20Sample%20Breeder%20%28litter%29.pdf')
        self.assertEqual(self.req('GET', r['url']), (200, pdf))
        self.assertEqual(self.data()['arr'][3][10], r['url'])
        arr = self.store.load('arr')[2]
        self.assertEqual(arr['contract_file'], 'ARR-03 contract - Sample Breeder (litter).pdf')
        # replacing it with a photo takes the old file out of use
        self.post('uploadContract', row=4, orig='ARR-03', name='scan.jpg', data=base64.b64encode(b'jpg').decode())
        self.assertEqual(self.req('GET', r['url'])[0], 404)
        self.assertEqual(self.store.db.execute('SELECT name FROM contracts').fetchall(), [('ARR-03 contract - Sample Breeder (litter).jpg',)])
        self.assertIn('PDF, a Word file', self.post('uploadContract', row=4, orig='ARR-03', name='x.exe', data='AAAA')['error'])
        self.assertEqual(self.post('uploadContract', row=4, orig='ARR-03', name='x.pdf', data='')['error'], 'No file was received.')
        self.assertEqual(self.post('uploadContract', row=4, orig='ARR-09', name='x.pdf', data='AAAA')['error'], server.CHANGED)

    def test_history_and_restore(self):
        self.post('addDeal', **DEAL)
        self.post('uploadContract', row=2, orig='ARR-01', name='c.pdf', data=base64.b64encode(b'%PDF').decode())
        hist = json.loads(self.req('GET', '/api/history')[1])['history']
        self.assertEqual([h['message'] for h in hist[:3]], ['Save the contract for ARR-01', 'Add deal #6', 'Import'])
        self.assertEqual([h['current'] for h in hist[:2]], [True, False])
        self.assertTrue(hist[2]['restorable'])
        self.assertFalse(hist[-1]['restorable'])  # the empty start
        link = self.data()['arr'][1][10]
        self.assertEqual(self.req('GET', link), (200, b'%PDF'))
        self.assertTrue(self.post('restore', id=hist[2]['id'])['ok'])
        self.assertEqual(len(self.data()['deals']), 6)
        self.assertEqual(self.req('GET', link)[0], 404)  # the contract added later is gone too
        self.assertTrue(self.last_change().startswith('Restore to how it was after "Import" ('))
        # and the restore can itself be undone
        hist = self.store.history()
        self.post('restore', id=hist[1]['id'])
        self.assertEqual(len(self.data()['deals']), 7)
        self.assertEqual(self.req('GET', link), (200, b'%PDF'))  # and the contract is back
        self.assertEqual(self.post('restore', id=hist[-1]['id'])['error'], 'That point in the history cannot be restored.')
        self.assertEqual(self.post('restore', id='nope')['error'], 'That point in the history cannot be restored.')

    def test_reminder_setting(self):
        self.assertEqual(self.post('setReminders', on=False)['reminders'], {'on': False})
        self.assertEqual(self.data()['reminders'], {'on': False})
        self.post('setReminders', on=True)
        self.assertEqual(self.data()['reminders'], {'on': True})

    def test_backup_folder_setting(self):
        self.assertIn('full path', self.post('saveSettings', folder='relative/path')['error'])
        self.assertIn('outside the data folder', self.post('saveSettings', folder=os.path.join(self.dir, 'b'))['error'])
        folder = tempfile.mkdtemp(prefix='wagtales-backup-')
        self.addCleanup(shutil.rmtree, folder, True)
        r = self.post('saveSettings', folder=folder)
        self.assertEqual((r['sync']['state'], r['sync']['folder']), ('ok', folder))
        self.assertEqual(len(os.listdir(folder)), 1)
        self.assertEqual(self.post('saveSettings', folder='')['sync']['state'], 'off')


class ReminderTests(Base):
    def setUp(self):
        super().setUp()
        self.shown = []
        self.day = dt.date(2026, 11, 3)
        self.rem = server.Reminders(self.store, self.backup, self.dir, notify=self.shown.append, today=lambda: self.day)

    def test_due_tomorrow_today_and_overdue(self):
        self.assertEqual(self.rem.check(), 1)
        self.assertEqual(self.shown, ['Deal #4, Sample Breeder (litter): the return of 57,720 is due tomorrow.'])
        self.assertEqual(self.rem.check(), 0)  # once a day
        self.day = dt.date(2026, 11, 8)
        self.rem.check()
        self.assertEqual(self.shown[1:], ['Deal #3, Sample Breeder: the return of 70,506 was due on 07.11.2026.',
                                          'Deal #4, Sample Breeder (litter): the return of 57,720 was due on 04.11.2026.'])
        self.day = dt.date(2026, 11, 9)
        self.assertEqual(self.rem.check(), 2)  # overdue ones come back the next day

    def test_money_not_sent_and_failing_backup(self):
        self.store.act('addDeal', DEAL)
        self.backup.settings.update(backup_dir='/nowhere')
        self.backup.status = {'state': 'error', 'last': '', 'error': 'x'}
        self.day = dt.date(2026, 10, 22)
        self.rem.check()
        self.assertEqual(self.shown, ['Deal #6, Sample Breeder: the money is not marked as sent yet.',
                                      'The backup is failing. Open the tracker to see why.'])

    def test_off(self):
        self.backup.settings.update(notify=False)
        self.assertEqual(self.rem.check(), 0)
        self.assertEqual(self.shown, [])


class BackupTests(Base):
    def setUp(self):
        super().setUp()
        self.folder = tempfile.mkdtemp(prefix='wagtales-backup-')
        self.addCleanup(shutil.rmtree, self.folder, True)
        self.day = dt.date(2026, 11, 6)
        self.backup.today = lambda: self.day
        self.backup.settings.update(backup_dir=self.folder)

    def test_off_without_a_folder(self):
        self.backup.settings.update(backup_dir='')
        self.backup.run()
        self.assertEqual(self.backup.public()['state'], 'off')

    def test_copy_holds_everything(self):
        with self.store.lock, self.store.db:
            self.store.put_contract('c.pdf', b'%PDF')
        self.backup.run()
        self.assertEqual(self.backup.public()['state'], 'ok')
        self.assertEqual(os.listdir(self.folder), ['wagtales-2026-11-06.db'])
        path = os.path.join(self.folder, 'wagtales-2026-11-06.db')
        self.assertEqual(os.stat(path).st_mode & 0o777, 0o600)
        db = sqlite3.connect(path)
        self.addCleanup(db.close)
        self.assertEqual(db.execute('SELECT COUNT(*) FROM deals').fetchone()[0], 5)
        self.assertEqual(db.execute('SELECT COUNT(*) FROM contract_data').fetchone()[0], 1)

    def test_one_copy_a_day_and_the_oldest_are_removed(self):
        other = os.path.join(self.folder, 'my-notes.txt')
        open(other, 'w').close()
        for i in range(server.KEEP_BACKUPS + 2):
            self.day = dt.date(2026, 1, 1) + dt.timedelta(days=i)
            self.backup.run()
            self.backup.run()
        copies = sorted(f for f in os.listdir(self.folder) if f.endswith('.db'))
        self.assertEqual(len(copies), server.KEEP_BACKUPS)
        self.assertEqual(copies[0], 'wagtales-2026-01-03.db')
        self.assertTrue(os.path.exists(other))  # only its own files are removed

    def test_a_failure_is_shown_and_leaves_no_temporary_file(self):
        self.backup.settings.update(backup_dir=os.path.join(self.folder, 'file', 'below'))
        open(os.path.join(self.folder, 'file'), 'w').close()
        self.backup.run()
        self.assertEqual(self.backup.public()['state'], 'error')
        self.assertIn('Could not save a backup', self.backup.public()['error'])
        self.assertEqual(os.listdir(self.folder), ['file'])

    def test_restore_from_a_copy(self):
        with self.store.lock, self.store.db:
            self.store.put_contract('c.pdf', b'%PDF')
        self.backup.run()
        copy = os.path.join(self.folder, 'wagtales-2026-11-06.db')
        fresh = tempfile.mkdtemp(prefix='wagtales-restored-')
        self.addCleanup(shutil.rmtree, fresh, True)
        server.main(['--data', fresh, '--restore', copy])
        restored = server.Store(fresh)
        self.addCleanup(restored.db.close)
        self.assertEqual(restored.snapshot(), self.store.snapshot())
        self.assertEqual(restored.contract('c.pdf'), b'%PDF')
        self.assertEqual(restored.history()[0]['message'], self.store.history()[0]['message'])
        with self.assertRaises(SystemExit):  # it only fills an empty folder
            server.main(['--data', fresh, '--restore', copy])

    def test_restore_refuses_a_file_that_is_not_a_backup(self):
        bad = os.path.join(self.folder, 'bad.db')
        with open(bad, 'wb') as h:
            h.write(b'not a database at all')
        fresh = tempfile.mkdtemp(prefix='wagtales-restored-')
        self.addCleanup(shutil.rmtree, fresh, True)
        with self.assertRaises(SystemExit):
            server.main(['--data', fresh, '--restore', bad])


if __name__ == '__main__':
    unittest.main(verbosity=1)
