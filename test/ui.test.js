// Drives the app page in Chrome against a server with a throwaway copy of the data.
//   npm install --no-save puppeteer-core   (once, uses the installed Google Chrome)
//   node test/ui.test.js                   screenshots go to test/shots/
const puppeteer = require('puppeteer-core');
const { spawn, execFileSync } = require('child_process');
const fs = require('fs'), os = require('os'), path = require('path');

const REPO = path.join(__dirname, '..');
const OUT = path.join(__dirname, 'shots');
fs.mkdirSync(OUT, { recursive: true });
const DATA = fs.mkdtempSync(path.join(os.tmpdir(), 'wagtales-ui-'));
execFileSync('python3', [path.join(REPO, 'app/server.py'), '--data', DATA, '--import', path.join(REPO, 'test/fixtures/sample-data.json')]);
const PORT = 8799, URL = 'http://127.0.0.1:' + PORT + '/';
const srv = spawn('python3', [path.join(REPO, 'app/server.py'), '--data', DATA, '--port', String(PORT)], { stdio: ['ignore', 'ignore', 'inherit'] });

let pass = 0, fail = 0;
const ok = (c, m) => { if (c) pass++; else { fail++; console.log('FAIL:', m); } };
const sleep = ms => new Promise(r => setTimeout(r, ms));

(async () => {
  for (let i = 0; i < 50; i++) {  // wait for the server to answer
    if (await fetch(URL + 'api/status').then(r => r.ok, () => false)) break;
    await sleep(200);
  }
  const browser = await puppeteer.launch({ executablePath: '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome', headless: true, args: ['--no-first-run'] });
  const errs = [];
  async function page(w, scheme) {
    const p = await browser.newPage();
    await p.setViewport({ width: w, height: 900 });
    await p.evaluateOnNewDocument(t => { try { localStorage.setItem('wagtales-theme', t); } catch (e) {} }, scheme);
    p.on('pageerror', e => errs.push(String(e)));
    p.on('console', m => { if (m.type() === 'error' && !/fonts/.test(m.text())) errs.push(m.text()); });
    p.on('dialog', d => d.accept());
    await p.goto(URL, { waitUntil: 'networkidle0' });
    await p.waitForSelector('.outlook .fig');
    return p;
  }
  const layout = p => p.evaluate(() => {
    const els = [...document.querySelectorAll('.panel:not([hidden]) input, .panel:not([hidden]) select, .panel:not([hidden]) textarea')].map(e => e.getBoundingClientRect());
    let bad = 0;
    for (let i = 0; i < els.length; i++) for (let j = i + 1; j < els.length; j++) { const a = els[i], b = els[j]; if (a.width && b.width && a.left < b.right - 0.5 && b.left < a.right - 0.5 && a.top < b.bottom - 0.5 && b.top < a.bottom - 0.5) bad++; }
    return { overlaps: bad, hscroll: document.documentElement.scrollWidth > document.documentElement.clientWidth + 1 };
  });

  // screenshots in both themes and widths
  for (const scheme of ['light', 'dark']) for (const w of [1280, 400]) {
    const p = await page(w, scheme);
    for (const t of ['overview', 'customers', 'arrangements', 'deals', 'history']) {
      await p.click(`.tab[data-tab="${t}"]`); await sleep(150);
      await p.screenshot({ path: path.join(OUT, `${t}-${scheme}-${w}.png`), fullPage: true });
      const l = await layout(p); ok(!l.overlaps && !l.hscroll, `${t} ${scheme} ${w} layout ${JSON.stringify(l)}`);
    }
    await p.close();
  }

  const p = await page(1280, 'light');
  const text = sel => p.$eval(sel, e => e.textContent);
  ok((await text('#sync-text')) === 'Backup off', 'header shows backup off');
  ok((await text('.outlook .fig')).replace(/,/g, '') > 0, 'outlook figure shown');

  // add a deal for ARR-01 from the Deals tab
  await p.click('.tab[data-tab="deals"]');
  await p.click('#p-deals [data-goto="add-deal"]');
  ok(!(await p.$eval('#d-delete', e => e.hidden)) === false, 'no delete button when adding');
  await p.select('#d-arr', 'ARR-01');
  ok((await p.$eval('#d-sell', e => e.value)) === '545355', 'prefilled from the last cycle');
  await p.click('#d-submit');
  await p.waitForFunction(() => !document.getElementById('p-overview').hidden);
  await sleep(300);
  ok(/Deal #6 added/.test(await text('#toast')), 'added toast: ' + await text('#toast'));
  const attn = await p.$$eval('.attn li', lis => lis.map(li => li.textContent));
  ok(attn.length === 1 && /Not sent/.test(attn[0]) && /Deal #6/.test(attn[0]), 'needs attention shows the unsent deal: ' + attn.join(' | '));
  ok(/FY 2026–27/.test(await p.$eval('#ov', e => e.textContent)), 'financial year card');
  ok(await p.$$eval('#ov table th', ths => ths.some(t => t.textContent === 'Per year')), 'per year column');
  const dealNos = async () => (await (await fetch(URL + 'api/data')).json()).deals.slice(1).map(r => r[0]);
  ok((await dealNos()).includes('6'), 'deal 6 saved');

  // edit deal 5: the two transaction numbers survive as one line
  await p.click('.tab[data-tab="deals"]');
  const editBtn = async n => { const rows = await p.$$('#deals-box tbody tr'); for (const r of rows) { if ((await r.$eval('td', td => td.textContent)) === String(n)) return r.$('td.acts .btn'); } };
  await (await editBtn(5)).click();
  ok((await p.$eval('#d-txn', e => e.value)) === '100000000004, 100000000005', 'txn shown comma-separated');
  ok((await p.$$eval('#deals-box tbody tr', trs => trs.map(t => t.cells[6].textContent))).includes('100000000004, 100000000005'), 'deals table shows both transfers');
  ok(!(await p.$eval('#d-delete', e => e.hidden)), 'delete shown when editing');
  await p.click('#p-add-deal [data-goto="deals"]');

  // mark deal 3 received
  const rows = await p.$$('#deals-box tbody tr');
  for (const r of rows) {
    if ((await r.$eval('td', td => td.textContent)) === '3') { await (await r.$('td.mark .btn')).click(); break; }
  }
  await p.type('#x-logi', '0'); await p.click('#x-submit'); await p.waitForNetworkIdle(); await sleep(300);
  ok(/Deal #3 marked received/.test(await text('#toast')), 'mark received');

  // delete deal 6
  await (await editBtn(6)).click();
  ok((await text('#deal-title')) === 'Edit deal #6', 'editing deal 6: ' + await text('#deal-title'));
  await p.click('#d-delete'); await p.click('#del-yes'); await p.waitForNetworkIdle(); await sleep(300);
  ok(/Deal #6 deleted/.test(await text('#toast')), 'delete toast: ' + await text('#toast'));
  ok(!(await dealNos()).includes('6'), 'deal 6 gone');

  // a deal that feeds a reinvestment cannot be deleted
  await (await editBtn(1)).click();
  await p.click('#d-delete'); await p.click('#del-yes'); await sleep(600);
  ok(/part of a reinvestment/.test(await text('#toast')), 'refusal shown');
  await p.click('#p-add-deal [data-goto="deals"]');

  // upload a contract for ARR-02
  await p.click('.tab[data-tab="arrangements"]');
  const pdf = path.join(DATA, 'test.pdf'); fs.writeFileSync(pdf, '%PDF-1.4 hello');
  const arrRows = await p.$$('#arr-box tbody tr');
  const [chooser] = await Promise.all([p.waitForFileChooser(), arrRows[1].$eval('td.acts .btn', b => b.click())]);
  await chooser.accept([pdf]); await sleep(900);
  ok(/Contract for ARR-02 saved/.test(await text('#toast')), 'contract toast');
  const href = await p.$eval('#arr-box tbody tr:nth-child(2) a.doclink', a => a.getAttribute('href'));
  ok(href === '/contracts/ARR-02%20contract%20-%20Jane%20Doe.pdf', 'contract link ' + href);
  const got = await p.evaluate(h => fetch(h).then(r => r.text()), href);
  ok(got === '%PDF-1.4 hello', 'contract served');
  const docBtns = await p.$eval('#arr-box tbody tr:nth-child(2) td.doc', c => [...c.querySelectorAll('.btn')].map(b => b.textContent).join('|'));
  ok(docBtns === 'Open|Replace', 'contract buttons: ' + docBtns);
  await p.screenshot({ path: path.join(OUT, 'arrangements-contract.png'), fullPage: true });
  const arrFit = await p.$eval('#arr-box', b => [b.scrollWidth, b.clientWidth]);
  ok(arrFit[0] <= arrFit[1], 'arrangements fit at 1280 without scrolling: ' + arrFit.join(' / '));

  // deleting from a row asks first, and Cancel or Escape change nothing
  const arrDel = '#arr-box tbody tr:first-child .btn.del';
  await p.click(arrDel);
  ok(!(await p.$eval('#del-overlay', e => e.hidden)) && /Delete arrangement ARR-01/.test(await text('#del-title')), 'delete asks first');
  await p.screenshot({ path: path.join(OUT, 'delete-dialog.png') });
  await p.click('#del-no');
  ok(await p.$eval('#del-overlay', e => e.hidden), 'Cancel closes the dialog');
  await p.click(arrDel); await p.keyboard.press('Escape');
  ok(await p.$eval('#del-overlay', e => e.hidden), 'Escape closes the dialog');
  ok((await (await fetch(URL + 'api/data')).json()).arr.some(r => r[0] === 'ARR-01'), 'nothing deleted by cancelling');
  await p.click(arrDel); await p.click('#del-yes'); await sleep(500);
  ok(/still has deals/.test(await text('#toast')), 'a record in use is refused: ' + await text('#toast'));

  // backup dialog
  await p.click('#conn-btn');
  ok(!(await p.$eval('#conn-overlay', e => e.hidden)) && (await p.$eval('#c-folder', e => e === document.activeElement)), 'backup dialog open on the folder');
  await p.screenshot({ path: path.join(OUT, 'backup-dialog.png') });
  await p.type('#c-folder', 'not/a/full/path');
  await p.click('#c-save'); await sleep(500);
  ok(/full path/.test(await text('#toast')), 'relative folder refused');
  const BK = fs.mkdtempSync(path.join(os.tmpdir(), 'wagtales-ui-backup-'));
  await p.$eval('#c-folder', e => { e.value = ''; }); await p.type('#c-folder', BK);
  await p.click('#c-save'); await sleep(500);
  ok(/Backup saved/.test(await text('#toast')) && fs.readdirSync(BK).length === 1, 'backup saved in the folder');
  ok(/^Backed up/.test(await text('#sync-text')), 'header shows backed up');
  await p.$eval('#c-folder', e => { e.value = ''; }); await p.click('#c-save'); await sleep(400);
  ok((await text('#sync-text')) === 'Backup off', 'backup can be turned off');
  fs.rmSync(BK, { recursive: true, force: true });
  await p.keyboard.press('Escape');
  ok(await p.$eval('#conn-overlay', e => e.hidden), 'dialog closes on Escape');

  // customers: statement for Sample covers both arrangements
  await p.click('.tab[data-tab="customers"]');
  const custMain = () => p.$$eval('#cust-box > table > tbody > tr:not(.cust-detail)', trs => trs.map(t => t.cells[2].textContent));
  ok((await custMain()).join('|') === 'Sample Breeder|Jane Doe|Alex Kumar Example', 'customers: ' + (await custMain()).join('|'));
  ok((await p.$$('#cust-box > table > thead th')).length === 10, 'customers table keeps all its columns');
  const openRows = () => p.$$eval('#cust-box tr.cust-detail', trs => trs.filter(t => t.offsetParent !== null).length);
  const expandBtn = '#cust-box > table > tbody > tr .btn.exp';
  const expanded = () => p.$eval(expandBtn, b => b.getAttribute('aria-expanded'));
  ok((await openRows()) === 0, 'customers start collapsed');
  await p.click(expandBtn);
  ok((await openRows()) === 1 && (await expanded()) === 'true', 'Expand opens the row');
  const inl = await p.$eval('#cust-box tr.cust-detail .statement', e => e.textContent);
  ok(/Statement of investments/.test(inl) && /ARR-01/.test(inl) && /100000000001/.test(inl) && /Outstanding/.test(inl), 'the row shows the statement');
  const widths = await p.$eval('#cust-box', b => [b.querySelector(':scope > table').offsetWidth, b.querySelector('.statement').offsetWidth]);
  ok(widths[0] === widths[1], 'statement uses the full table width: ' + widths.join(' / '));
  await p.screenshot({ path: path.join(OUT, 'customer-expanded.png'), fullPage: true });
  await p.$eval(expandBtn, b => b.click());
  ok((await openRows()) === 0 && (await expanded()) === 'false', 'Collapse closes it again');
  await p.click(expandBtn);
  await p.click('.tab[data-tab="deals"]'); await p.click('.tab[data-tab="customers"]');
  ok((await openRows()) === 1, 'stays expanded after changing tabs');
  await p.setViewport({ width: 400, height: 900 });
  await p.screenshot({ path: path.join(OUT, 'customer-expanded-400.png'), fullPage: true });
  await p.setViewport({ width: 1280, height: 900 });
  await p.click('#cust-box .cust-print .btn');
  await sleep(200);
  const st = await p.$eval('#st-box', e => e.textContent);
  ok(/ARR-01/.test(st) && /ARR-03 · 4-month recurring · litter/.test(st) && /Outstanding/.test(st), 'statement content');
  ok(/100000000001/.test(st) && /100000000003/.test(st), 'statement shows transaction numbers');
  await p.screenshot({ path: path.join(OUT, 'statement.png'), fullPage: true });
  await p.emulateMediaType('print');
  ok(await p.$eval('.tabs', e => getComputedStyle(e).display === 'none'), 'print hides the app chrome');
  await p.emulateMediaType('screen');

  // new customer leads to their first arrangement, with a fixed projection
  await p.click('#p-statement [data-goto="customers"]');
  await p.click('#p-customers [data-goto="add-cust"]');
  await p.type('#cu-name', 'Test Kennel'); await p.type('#cu-phone', '98300 00000');
  await p.click('#cu-submit'); await p.waitForNetworkIdle(); await sleep(300);
  ok(!(await p.$eval('#p-add-arr', e => e.hidden)) && (await p.$eval('#a-custsel', e => e.value)) === 'CUS-04', 'new arrangement for the new customer');
  await p.type('#a-term', '3'); await p.select('#a-proj', 'fixed');
  ok(!(await p.$eval('#a-cycle-w', e => e.hidden)), 'profit per cycle shown for a fixed projection');
  await p.type('#a-cycle', '20000');
  await p.screenshot({ path: path.join(OUT, 'arrangement-form.png'), fullPage: true });
  await p.click('#a-submit'); await p.waitForNetworkIdle(); await sleep(300);
  ok(!(await p.$eval('#p-add-deal', e => e.hidden)) && (await p.$eval('#d-arr', e => e.value)) === 'ARR-05', 'then its first deal');
  await p.click('#p-add-deal [data-goto="deals"]');
  await p.click('.tab[data-tab="arrangements"]');
  const arr5 = await p.$$eval('#arr-box tbody tr', trs => trs.map(t => t.textContent).filter(t => t.startsWith('ARR-05'))[0] || '');
  ok(/Test Kennel/.test(arr5) && /20,000 a cycle/.test(arr5), 'arrangement row: ' + arr5);

  // deals: search, filter and sort
  await p.click('.tab[data-tab="deals"]');
  await p.type('#f-q', 'alex'); await sleep(100);
  ok((await p.$$('#deals-box tbody tr')).length === 1 && (await text('#f-count')) === '1 of 5 deals', 'search: ' + await text('#f-count'));
  await p.$eval('#f-q', e => { e.value = ''; e.dispatchEvent(new Event('input')); });
  await p.select('#f-ret', 'received'); await sleep(100);
  ok((await p.$$('#deals-box tbody tr')).length === 3, 'received filter');
  await p.select('#f-ret', '');
  const dueTh = (await p.$$('#deals-box th'))[9];
  await dueTh.click(); await dueTh.evaluate(e => e); await sleep(100);
  await (await p.$$('#deals-box th'))[9].click(); await sleep(100);
  const firstDue = await p.$eval('#deals-box tbody tr td:nth-child(10)', e => e.textContent);
  ok(firstDue === '30.11.2026', 'sorted by due, latest first: ' + firstDue);

  // reinvestment into a deal
  await p.click('.tab[data-tab="reinvest"]');
  await p.click('#p-reinvest [data-goto="add-reinv"]');
  ok(!(await p.$eval('#r-dest-w', e => e.hidden)), 'free text shown for something else');
  await p.select('#r-destdeal', '5');
  ok(await p.$eval('#r-dest-w', e => e.hidden), 'free text hidden for a deal');
  await p.type('#r-amt', '5000'); await p.click('#r-submit'); await p.waitForNetworkIdle(); await sleep(300);
  const reinvRows = await p.$$eval('#reinv-box tbody tr', trs => trs.map(t => t.cells[3].textContent));
  ok(reinvRows[1] === 'Deal #5, Alex Kumar Example', 'reinvestment destination: ' + reinvRows.join('|'));

  // reminders switch
  await p.click('#conn-btn');
  ok(await p.$eval('#rem-on', e => e.checked), 'reminders on by default');
  await p.click('#rem-on'); await p.waitForNetworkIdle();
  ok(JSON.parse(fs.readFileSync(path.join(DATA, 'settings.json'))).notify === false, 'reminders saved as off');
  await p.keyboard.press('Escape');

  // history: undo the reinvestment
  await p.click('.tab[data-tab="history"]'); await p.waitForNetworkIdle(); await sleep(200);
  const hist = await p.$$eval('#hist-box tbody tr', trs => trs.map(t => t.cells[1].textContent));
  ok(hist[0] === 'Add a reinvestment from deal #1', 'latest change: ' + hist[0]);
  await p.$$eval('#hist-box tbody tr', trs => trs[1].querySelector('.btn').click());
  await p.waitForNetworkIdle(); await sleep(400);
  ok(/Restored/.test(await text('#toast')), 'restored toast');
  ok((await (await fetch(URL + 'api/data')).json()).reinv.length === 2, 'reinvestment undone');
  await p.screenshot({ path: path.join(OUT, 'history.png'), fullPage: true });

  const changes = (await (await fetch(URL + 'api/history')).json()).history.map(h => h.message);
  ok(/^Restore to how it was after/.test(changes[0]) && changes.includes('Save the contract for ARR-02') && changes.includes('Delete deal #6') && changes.includes('Add customer CUS-04 (Test Kennel)'), 'changes recorded: ' + changes.slice(0, 5).join(' | '));

  // the deal actions stay on screen even when the table has to scroll
  await p.click('.tab[data-tab="deals"]');
  const dealHeads = await p.$$eval('#deals-box th', ths => ths.map(t => t.textContent));
  ok(dealHeads[2] === 'Status' && dealHeads[3] === 'Mark as received', 'mark received sits beside status: ' + dealHeads.slice(2, 4).join(' | '));
  const dealFit = await p.$eval('#deals-box', b => { const r = b.getBoundingClientRect(), d = b.querySelector('tbody tr .btn.del').getBoundingClientRect(); return d.left >= r.left && d.right <= r.right; });
  ok(dealFit, 'deal icons visible without scrolling');

  // the page is dark until switched, and remembers the switch
  const ctx = await browser.createBrowserContext();
  const q = await ctx.newPage();
  await q.emulateMediaFeatures([{ name: 'prefers-color-scheme', value: 'light' }]);
  await q.goto(URL); await q.waitForNetworkIdle();
  const theme = () => q.$eval('html', e => e.getAttribute('data-theme'));
  const bg = () => q.$eval('body', e => getComputedStyle(e).backgroundColor);
  ok((await theme()) === 'dark' && (await bg()) === 'rgb(13, 17, 23)', 'dark by default: ' + await bg());
  await q.click('#theme-btn');
  ok((await theme()) === 'light' && (await bg()) === 'rgb(245, 246, 248)', 'switches to light: ' + await bg());
  await q.reload(); await q.waitForNetworkIdle();
  ok((await theme()) === 'light', 'remembers light after reloading');
  await q.click('#theme-btn');
  ok((await theme()) === 'dark', 'switches back to dark');
  await q.click('.tab[data-tab="customers"]');
  const col = sel => q.$eval(sel, e => getComputedStyle(e).color);
  ok((await col('#cust-box tbody td.c-total')) !== (await col('#cust-box tbody td.c-in')), 'amounts are coloured by meaning');
  ok((await q.$$('#cust-box td.c-zero')).length > 0, 'a zero amount is muted');
  await q.close(); await ctx.close();

  // a row delete that is allowed
  await p.click('.tab[data-tab="reinvest"]');
  const before = (await (await fetch(URL + 'api/data')).json()).reinv.length;
  await p.click('#reinv-box tbody tr:first-child .btn.del'); await p.click('#del-yes'); await p.waitForNetworkIdle(); await sleep(300);
  ok((await (await fetch(URL + 'api/data')).json()).reinv.length === before - 1 && /Reinvestment deleted/.test(await text('#toast')), 'reinvestment deleted from its row');
  for (const box of ['deals', 'arr', 'cust']) {
    await p.click('.tab[data-tab="' + { deals: 'deals', arr: 'arrangements', cust: 'customers' }[box] + '"]');
    ok((await p.$$('#' + box + '-box > table > tbody > tr:not(.cust-detail) .btn.edit')).length === (await p.$$('#' + box + '-box > table > tbody > tr:not(.cust-detail) .btn.del')).length && (await p.$$('#' + box + '-box .btn.del')).length > 0, box + ' rows have edit and delete icons');
  }

  ok(errs.length === 0, 'page errors: ' + errs.join(' | '));
  await browser.close();
  srv.kill();
  fs.rmSync(DATA, { recursive: true, force: true });
  console.log('passed', pass, 'failed', fail);
  process.exit(fail ? 1 : 0);
})().catch(e => { console.error(e); srv.kill(); process.exit(1); });
