/**
 * WAGTales Tracker - Google Sheet backup.
 *
 * The WAGTales app on your Mac keeps the data. After each change it sends a full copy here,
 * and this script writes it into the Customers, Arrangements, Deals and Reinvestments tabs
 * and a Dashboard tab with the reports. Contract files are copied into the WAGTales Contracts
 * folder in Google Drive, and back to the app when it has no copy. The sheet is a copy:
 * edits made in it are overwritten. The Summary and Margins tabs are left alone.
 *
 * One-time setup (inside this sheet: Extensions > Apps Script):
 *   1. Paste this whole file, replacing what is there, and save.
 *   2. No key yet? Choose "setup" in the function menu and click Run. Allow access, then copy
 *      the key printed in the execution log. If you have a key from before, keep it:
 *      running setup again replaces it.
 *   3. Choose "authorize" and click Run once, so contracts can be copied to Google Drive.
 *   4. Deploy > New deployment > Web app. Execute as: Me. Who has access: Anyone.
 *      Copy the web app URL (it ends in /exec). If it was deployed before, use
 *      Deploy > Manage deployments > Edit > Version: New version > Deploy; the URL stays the same.
 *   5. In the WAGTales app, click Backup and paste the URL and the key.
 *
 * Anyone with both the URL and the key can overwrite the sheet, so keep them private.
 */

var SCRIPT_VERSION = 5;
var SHEET_ID = '15KTdFGPKTQuRLtLUg4L3fFVz-boxhVVxLmm4Z5JZc9g';
var CONTRACTS_FOLDER_ID = '10zo3a3jNeNt2cZfHg0YgkCGwtOQgHbaE';
var DATA_TABS = ['Customers', 'Arrangements', 'Deals', 'Reinvestments'];

function setup() {
  var key = Utilities.getUuid().replace(/-/g, '') + Utilities.getUuid().replace(/-/g, '');
  PropertiesService.getScriptProperties().setProperty('WAGTALES_KEY', key);
  Logger.log('Your tracker key: ' + key);
  return key;
}

/* Asks Google for the sheet and Drive permissions. It does not change your key. */
function authorize() {
  book_();
  DriveApp.getRootFolder();
  Logger.log('Sheet and Drive access are allowed. You can close this.');
}

function doGet(e) {
  return handle_((e && e.parameter) || {});
}

function doPost(e) {
  var body = {};
  try { body = JSON.parse((e && e.postData && e.postData.contents) || '{}'); } catch (err) { body = {}; }
  return handle_(body);
}

function handle_(req) {
  var out;
  try {
    var key = PropertiesService.getScriptProperties().getProperty('WAGTALES_KEY');
    if (!key) throw new Error('Run setup in the Apps Script editor first.');
    if (!req || req.key !== key) return json_({ ok: false, error: 'bad_key' });
    var action = req.action;
    if (action === 'ping') out = {};
    else if (action === 'read') out = read_();
    else {
      var lock = LockService.getScriptLock();
      lock.waitLock(30000);
      try {
        if (action === 'sync') out = sync_(req);
        else if (action === 'saveContract') out = saveContract_(req);
        else if (action === 'getContract') out = getContract_(req);
        else throw new Error('Unknown action.');
        SpreadsheetApp.flush();
      } finally {
        lock.releaseLock();
      }
    }
    out.ok = true;
    out.version = SCRIPT_VERSION;
  } catch (err) {
    out = { ok: false, error: String((err && err.message) || err) };
  }
  return json_(out);
}

function json_(obj) {
  return ContentService.createTextOutput(JSON.stringify(obj)).setMimeType(ContentService.MimeType.JSON);
}

/* Works inside the sheet (Extensions > Apps Script) or as a separate project at script.google.com. */
function book_() {
  return SpreadsheetApp.getActiveSpreadsheet() || SpreadsheetApp.openById(SHEET_ID);
}

/* ---------- read: lets the app restore its data from this sheet ---------- */
function block_(name, cols) {
  var s = book_().getSheetByName(name);
  if (!s || s.getLastRow() < 1) return [];
  return s.getRange(1, 1, s.getLastRow(), cols).getDisplayValues();
}

function read_() {
  return { cust: block_('Customers', 6), arr: block_('Arrangements', 15), deals: block_('Deals', 21), reinv: block_('Reinvestments', 8) };
}

/* ---------- sync: replaces the data tabs and the Dashboard tab ---------- */
function sync_(p) {
  ['arr', 'deals', 'reinv'].forEach(function (k) {
    var t = p[k];
    if (!t || !Array.isArray(t.head) || !Array.isArray(t.kinds) || !Array.isArray(t.rows)) throw new Error('The backup data is incomplete.');
    if (DATA_TABS.indexOf(t.tab) < 0) throw new Error('Unknown tab ' + t.tab + '.');
  });
  if (p.cust) {
    if (!Array.isArray(p.cust.head) || !Array.isArray(p.cust.kinds) || !Array.isArray(p.cust.rows) || p.cust.tab !== 'Customers') throw new Error('The customer data is incomplete.');
    writeTab_(p.cust);
  }
  writeTab_(p.arr);
  writeTab_(p.deals);
  writeTab_(p.reinv);
  if (p.report && Array.isArray(p.report.rows)) writeReport_(p.report);
  return { rows: { arr: p.arr.rows.length, deals: p.deals.rows.length, reinv: p.reinv.rows.length } };
}

/* one cell as the sheet should hold it: dates as dates, numbers as numbers, text kept as typed */
function cell_(v, kind) {
  if (v === null || v === undefined || v === '') return '';
  if (kind === 'd') {
    var m = /^(\d{4})-(\d{2})-(\d{2})$/.exec(String(v));
    return m ? new Date(Number(m[1]), Number(m[2]) - 1, Number(m[3])) : String(v);
  }
  if (kind === 'n') {
    var x = Number(v);
    return isFinite(x) ? x : String(v);
  }
  v = String(v).slice(0, 5000);
  return /^[=+\-@]/.test(v) ? "'" + v : v;
}

function fit_(s, rows, cols) {
  if (s.getMaxRows() < rows) s.insertRowsAfter(s.getMaxRows(), rows - s.getMaxRows());
  if (s.getMaxColumns() < cols) s.insertColumnsAfter(s.getMaxColumns(), cols - s.getMaxColumns());
}

function writeTab_(t) {
  var book = book_(), s = book.getSheetByName(t.tab) || book.insertSheet(t.tab);
  var n = t.head.length;
  if (t.kinds.length !== n) throw new Error('The ' + t.tab + ' columns do not match.');
  var grid = [t.head.map(String)].concat(t.rows.map(function (r) {
    if (r.length !== n) throw new Error('A row in ' + t.tab + ' has the wrong number of columns.');
    return r.map(function (v, j) { return cell_(v, t.kinds[j]); });
  }));
  fit_(s, grid.length + 1, n);
  // clearing also removes the old ARRAYFORMULA columns; the app sends their values instead
  if (s.getLastRow() > 0) s.getRange(1, 1, s.getLastRow(), n).clearContent();
  if (grid.length > 1) {
    t.kinds.forEach(function (k, j) {
      s.getRange(2, j + 1, grid.length - 1, 1).setNumberFormat(k === 'd' ? 'dd.mm.yyyy' : k === 'n' ? '#,##0' : '@');
    });
  }
  s.getRange(1, 1, grid.length, n).setValues(grid);
}

function writeReport_(r) {
  var book = book_(), s = book.getSheetByName('Dashboard') || book.insertSheet('Dashboard', 0);
  var stamp = 'Copied from the WAGTales app on ' + Utilities.formatDate(new Date(), Session.getScriptTimeZone(), 'dd.MM.yyyy HH:mm') +
    '. This sheet is a copy: changes made here are overwritten.';
  var top = 3, body = [['WAGTales dashboard'], [stamp], []].concat(r.rows.slice(0, 2000));
  var w = body.reduce(function (m, row) { return Math.max(m, Array.isArray(row) ? row.length : 0); }, 1);
  var vals = [], fmts = [];
  body.forEach(function (row) {
    var v = [], f = [];
    for (var j = 0; j < w; j++) {
      var x = Array.isArray(row) ? row[j] : '';
      var isNum = typeof x === 'number' && isFinite(x);
      v.push(cell_(x, isNum ? 'n' : 't'));
      f.push(isNum ? '#,##0' : '@');
    }
    vals.push(v); fmts.push(f);
  });
  s.clear();
  fit_(s, vals.length, w);
  var range = s.getRange(1, 1, vals.length, w);
  range.setNumberFormats(fmts);
  range.setValues(vals);
  s.getRange(1, 1).setFontWeight('bold').setFontSize(14);
  s.getRange(2, 1).setFontColor('#6A7585');
  (r.bold || []).forEach(function (i) {
    if (typeof i === 'number' && i >= 0 && i + top < vals.length) s.getRange(i + top + 1, 1, 1, w).setFontWeight('bold');
  });
  s.autoResizeColumns(1, w);
}

/* ---------- contracts: copies a file into the WAGTales Contracts folder and returns its link ---------- */
function saveContract_(p) {
  var name = String(p.name || '').replace(/[\\/:*?"<>|]/g, ' ').trim();
  if (!name) throw new Error('The contract has no file name.');
  var bytes = Utilities.base64Decode(String(p.data || ''));
  if (!bytes.length) throw new Error('No file was received.');
  if (bytes.length > 15 * 1024 * 1024) throw new Error('Contract files must be 15 MB or smaller.');
  var folder;
  try { folder = DriveApp.getFolderById(CONTRACTS_FOLDER_ID); } catch (e) { folder = DriveApp.getRootFolder(); }
  // a retried backup finds the copy it already made instead of making another
  var same = folder.getFilesByName(name);
  while (same.hasNext()) {
    var f = same.next();
    if (!f.isTrashed() && f.getSize() === bytes.length) return { url: f.getUrl() };
  }
  var file = folder.createFile(Utilities.newBlob(bytes, String(p.mimeType || 'application/octet-stream'), name));
  return { url: file.getUrl() };
}

/* sends a contract back to the app, only from the WAGTales Contracts folder */
function getContract_(p) {
  var m = /[-\w]{25,}/.exec(String(p.url || ''));
  if (!m) throw new Error('That contract link is not a Google Drive file.');
  var file = DriveApp.getFileById(m[0]), parents = file.getParents(), inFolder = false;
  while (parents.hasNext()) if (parents.next().getId() === CONTRACTS_FOLDER_ID) inFolder = true;
  if (!inFolder) throw new Error('Only files in the WAGTales Contracts folder can be copied to the app.');
  var blob = file.getBlob();
  if (blob.getBytes().length > 15 * 1024 * 1024) throw new Error('Contract files must be 15 MB or smaller.');
  return { name: file.getName(), mimeType: blob.getContentType(), data: Utilities.base64Encode(blob.getBytes()) };
}
