// Checks app/apps-script.gs against a simulated spreadsheet and Drive.
//   node test/apps-script.test.js
const fs = require('fs');
const path = require('path');
const src = fs.readFileSync(path.join(__dirname, '..', 'app', 'apps-script.gs'), 'utf8');

function mkSheet(name, rows) {
  const g = rows.map(r => r.slice());
  const fmt = {}; const style = {};
  let maxRows = 50, maxCols = 20;
  const cell = (r, c) => (g[r - 1] && g[r - 1][c - 1] != null) ? g[r - 1][c - 1] : '';
  const put = (r, c, v) => { while (g.length < r) g.push([]); g[r - 1][c - 1] = v; };
  const range = (r, c, nr = 1, nc = 1) => {
    const each = f => { for (let i = 0; i < nr; i++) for (let j = 0; j < nc; j++) f(r + i, c + j, i, j); };
    const self = {
      getDisplayValues: () => Array.from({ length: nr }, (_, i) => Array.from({ length: nc }, (_, j) => String(cell(r + i, c + j)))),
      setValues: v => {
        if (v.length !== nr || v.some(row => row.length !== nc)) throw new Error('setValues size mismatch');
        if (r + nr - 1 > maxRows || c + nc - 1 > maxCols) throw new Error('range outside the sheet');
        each((rr, cc, i, j) => put(rr, cc, v[i][j])); return self;
      },
      clearContent: () => { each((rr, cc) => { if (g[rr - 1]) g[rr - 1][cc - 1] = ''; }); return self; },
      setNumberFormat: f => { each((rr, cc) => { fmt[rr + ',' + cc] = f; }); return self; },
      setNumberFormats: f => { each((rr, cc, i, j) => { fmt[rr + ',' + cc] = f[i][j]; }); return self; },
      setFontWeight: w => { each((rr, cc) => { style[rr + ',' + cc] = w; }); return self; },
      setFontSize: () => self, setFontColor: () => self
    };
    return self;
  };
  return {
    name, g, fmt, style,
    getRange: (r, c, nr, nc) => range(r, c, nr, nc),
    getLastRow: () => { for (let i = g.length - 1; i >= 0; i--) if ((g[i] || []).some(v => v !== '' && v != null)) return i + 1; return 0; },
    getMaxRows: () => maxRows, getMaxColumns: () => maxCols,
    insertRowsAfter: (_, n) => { maxRows += n; }, insertColumnsAfter: (_, n) => { maxCols += n; },
    clear: () => { g.length = 0; for (const k in fmt) delete fmt[k]; for (const k in style) delete style[k]; },
    autoResizeColumns: () => {}
  };
}

const sheets = {
  Deals: mkSheet('Deals', [['Deal #', 'Arrangement', 'Customer'], [1, 'ARR-01', '=ARRAYFORMULA(...)'], [2, 'ARR-02', ''], [3, 'ARR-01', '']]),
  Arrangements: mkSheet('Arrangements', [['ID'], ['ARR-01'], ['ARR-02']]),
  Reinvestments: mkSheet('Reinvestments', [['Date']]),
  Summary: mkSheet('Summary', [['Investment summary']])
};
const order = ['Summary', 'Arrangements', 'Deals', 'Reinvestments'];
const props = {}; const files = [];
const driveFiles = {
  '1FakeContractFileOneXXXXXXXXXXXXX': { name: 'ARR-01 contract - Sample Breeder.pdf', parent: '10zo3a3jNeNt2cZfHg0YgkCGwtOQgHbaE', bytes: Buffer.from('%PDF-1.4 one') },
  '1OtherFolderFileXXXXXXXXXXXXXXXX': { name: 'Private.pdf', parent: 'SOMEWHERE_ELSE', bytes: Buffer.from('secret') }
};
const book = {
  getSheetByName: n => sheets[n] || null,
  insertSheet: (n, i) => { sheets[n] = mkSheet(n, []); order.splice(i == null ? order.length : i, 0, n); return sheets[n]; }
};
const ctx = {
  SpreadsheetApp: { getActiveSpreadsheet: () => book, openById: () => book, flush: () => {} },
  PropertiesService: { getScriptProperties: () => ({ getProperty: k => props[k] || null, setProperty: (k, v) => { props[k] = v; } }) },
  LockService: { getScriptLock: () => ({ waitLock: () => {}, releaseLock: () => {} }) },
  ContentService: { createTextOutput: s => ({ s, setMimeType() { return this; } }), MimeType: { JSON: 'json' } },
  Utilities: {
    getUuid: () => require('crypto').randomUUID(),
    base64Decode: b => Array.from(Buffer.from(b, 'base64')),
    base64Encode: bytes => Buffer.from(bytes).toString('base64'),
    newBlob: (bytes, mime, name) => ({ bytes, mime, name }),
    formatDate: () => '21.10.2026 22:30'
  },
  Session: { getScriptTimeZone: () => 'Asia/Kolkata' },
  DriveApp: {
    getFolderById: id => ({
      createFile: blob => { const f = { folder: id, name: blob.name, size: blob.bytes.length, url: 'https://drive.google.com/file/d/F' + files.length + '/view' }; files.push(f); return { getUrl: () => f.url }; },
      getFilesByName: name => { const m = files.filter(f => f.name === name); let i = 0; return { hasNext: () => i < m.length, next: () => { const f = m[i++]; return { isTrashed: () => false, getSize: () => f.size, getUrl: () => f.url }; } }; }
    }),
    getRootFolder: () => ({}),
    getFileById: id => {
      const f = driveFiles[id]; if (!f) throw new Error('No item with the given ID could be found.');
      return { getName: () => f.name, getParents: () => { let i = 0; return { hasNext: () => i < 1, next: () => { i++; return { getId: () => f.parent }; } }; },
        getBlob: () => ({ getBytes: () => Array.from(f.bytes), getContentType: () => 'application/pdf' }) };
    }
  },
  Logger: { log: () => {} }
};
const api = new Function(...Object.keys(ctx), src + '; return {setup, doPost, doGet};')(...Object.values(ctx));
const post = body => JSON.parse(api.doPost({ postData: { contents: JSON.stringify(body) } }).s);
let pass = 0, fail = 0;
const ok = (c, m) => { if (c) pass++; else { fail++; console.log('FAIL:', m); } };

ok(post({ action: 'ping', key: 'x' }).error === 'Run setup in the Apps Script editor first.', 'no setup');
const key = api.setup(); ok(key.length === 64, 'key length');
ok(post({ action: 'ping', key: 'wrong' }).error === 'bad_key', 'bad key rejected');
const ping = post({ action: 'ping', key }); ok(ping.ok && ping.version === 5, 'ping answers with version 5');
ok(JSON.parse(api.doGet({ parameter: { action: 'ping', key } }).s).ok, 'GET ping works');
ok(post({ action: 'addDeal', key }).error === 'Unknown action.', 'old actions are gone');

const tab = (name, head, kinds, rows) => ({ tab: name, head, kinds, rows });
const payload = {
  action: 'sync', key,
  arr: tab('Arrangements', ['ID', 'Customer', 'Start Date'], ['t', 't', 'd'], [['ARR-01', 'Sample', '2026-08-28']]),
  deals: tab('Deals', ['Deal #', 'Customer', 'Investment Date', 'Profit', 'Transaction No.', 'Notes'], ['n', 't', 'd', 'n', 't', 't'],
    [[1, 'Sample', '2026-08-28', 70506, '00123456', '=HYPERLINK("x")'], [2, 'Jane', '', 8970.5, '', '']]),
  reinv: tab('Reinvestments', ['Date', 'Amount'], ['d', 'n'], [['2026-10-04', 65730]]),
  cust: tab('Customers', ['ID', 'Name', 'Phone'], ['t', 't', 't'], [['CUS-01', 'Sample', '98300 12345']]),
  report: { rows: [['Outlook', 'Amount'], ['Received', 225690], [], ['Month', 'Received'], ['Oct 2026', 159960]], bold: [0, 3] }
};
const sy = post(payload);
ok(sy.ok && sy.rows.deals === 2, 'sync ok: ' + JSON.stringify(sy));
const D = sheets.Deals.g;
ok(D[0].join() === 'Deal #,Customer,Investment Date,Profit,Transaction No.,Notes', 'header written');
ok(D[1][0] === 1 && D[1][3] === 70506, 'numbers stay numbers');
ok(D[1][2] instanceof Date && D[1][2].getFullYear() === 2026 && D[1][2].getMonth() === 7 && D[1][2].getDate() === 28, 'dates become dates');
ok(D[1][4] === '00123456' && sheets.Deals.fmt['2,5'] === '@', 'transaction numbers kept as text');
ok(D[1][5] === '\'=HYPERLINK("x")', 'text that looks like a formula is escaped');
ok(sheets.Deals.fmt['2,3'] === 'dd.mm.yyyy' && sheets.Deals.fmt['2,4'] === '#,##0', 'column formats');
ok(D[2][2] === '' && D[2][3] === 8970.5, 'blank date stays blank');
ok(!D[3] || D[3].every(v => v === ''), 'old third deal row cleared');
ok(!D.some(r => (r || []).some(v => String(v).startsWith('=ARRAYFORMULA'))), 'old array formulas cleared');
ok(sheets.Arrangements.g[2] === undefined || sheets.Arrangements.g[2].every(v => v === ''), 'removed arrangement cleared');
ok(sheets.Summary.g[0][0] === 'Investment summary', 'Summary tab untouched');
ok(sheets.Customers && sheets.Customers.g[1].join() === 'CUS-01,Sample,98300 12345' && sheets.Customers.fmt['2,3'] === '@', 'Customers tab written, phone as text');
const noCust = Object.assign({}, payload); delete noCust.cust;
ok(post(noCust).ok, 'sync without customers still works');
ok(/Unknown tab|incomplete/.test(post(Object.assign({}, payload, { cust: tab('Summary', ['A'], ['t'], []) })).error), 'customers cannot target another tab');

const R = sheets.Dashboard;
ok(R && order[0] === 'Dashboard', 'Dashboard tab created first');
ok(R.g[0][0] === 'WAGTales dashboard' && /^Copied from the WAGTales app on 21.10.2026 22:30/.test(R.g[1][0]), 'title and stamp');
ok(R.g[4][1] === 225690 && R.fmt['5,2'] === '#,##0' && R.fmt['7,1'] === '@', 'report values and formats');
ok(R.style['4,1'] === 'bold' && R.style['7,2'] === 'bold' && !R.style['5,1'], 'section headings bold');
post(Object.assign({}, payload, { report: { rows: [['Only row']], bold: [] } }));
ok(R.g.length === 4 && R.g[3][0] === 'Only row', 'report replaced on the next sync');

ok(/incomplete/.test(post({ action: 'sync', key, arr: payload.arr, deals: payload.deals }).error), 'missing tab rejected');
ok(/Unknown tab/.test(post(Object.assign({}, payload, { reinv: tab('Summary', ['A'], ['t'], []) })).error), 'only data tabs can be written');
ok(/wrong number of columns/.test(post(Object.assign({}, payload, { reinv: tab('Reinvestments', ['A', 'B'], ['t', 't'], [['x']]) })).error), 'ragged row rejected');

const data = Buffer.from('%PDF-1.4 test').toString('base64');
const c1 = post({ action: 'saveContract', key, name: 'ARR-03 contract - Sample (litter).pdf', mimeType: 'application/pdf', data });
ok(c1.ok && c1.url === 'https://drive.google.com/file/d/F0/view', 'contract saved: ' + JSON.stringify(c1));
ok(files[0].folder === '10zo3a3jNeNt2cZfHg0YgkCGwtOQgHbaE' && files[0].name === 'ARR-03 contract - Sample (litter).pdf', 'in the contracts folder');
const c2 = post({ action: 'saveContract', key, name: 'ARR-03 contract - Sample (litter).pdf', mimeType: 'application/pdf', data });
ok(c2.url === c1.url && files.length === 1, 'a retry reuses the same file');
ok(post({ action: 'saveContract', key, name: 'x.pdf', data: '' }).error === 'No file was received.', 'empty file rejected');

sheets.Deals.g.length = 0; sheets.Deals.g.push(['Deal #'], ['5']);
const rd = post({ action: 'read', key });
ok(rd.ok && rd.deals.length === 2 && rd.deals[1][0] === '5' && rd.arr.length === 2 && rd.reinv.length === 2 && rd.cust.length === 2, 'read returns the tabs');

const gc = post({ action: 'getContract', key, url: 'https://drive.google.com/file/d/1FakeContractFileOneXXXXXXXXXXXXX/view?usp=drivesdk' });
ok(gc.ok && gc.name === 'ARR-01 contract - Sample Breeder.pdf' && Buffer.from(gc.data, 'base64').toString() === '%PDF-1.4 one', 'contract sent back: ' + JSON.stringify(gc).slice(0, 80));
ok(/Only files in the WAGTales Contracts folder/.test(post({ action: 'getContract', key, url: 'https://drive.google.com/file/d/1OtherFolderFileXXXXXXXXXXXXXXXX/view' }).error), 'files elsewhere in Drive are refused');
ok(/not a Google Drive file/.test(post({ action: 'getContract', key, url: 'https://example.com/x' }).error), 'a non-Drive link is refused');

console.log('passed', pass, 'failed', fail);
process.exit(fail ? 1 : 0);
