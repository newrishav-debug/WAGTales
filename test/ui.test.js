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
    await p.emulateMediaFeatures([{ name: 'prefers-color-scheme', value: scheme }]);
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
  ok((await p.$$eval('#deals-box tbody tr', trs => trs.map(t => t.cells[5].textContent))).includes('100000000004, 100000000005'), 'deals table shows both transfers');
  ok(!(await p.$eval('#d-delete', e => e.hidden)), 'delete shown when editing');
  await p.click('#p-add-deal [data-goto="deals"]');

  // mark deal 3 received
  const rows = await p.$$('#deals-box tbody tr');
  for (const r of rows) {
    if ((await r.$eval('td', td => td.textContent)) === '3') { const b = await r.$$('td.acts .btn'); await b[1].click(); break; }
  }
  await p.type('#x-logi', '0'); await p.click('#x-submit'); await p.waitForNetworkIdle(); await sleep(300);
  ok(/Deal #3 marked received/.test(await text('#toast')), 'mark received');

  // delete deal 6
  await (await editBtn(6)).click();
  ok((await text('#deal-title')) === 'Edit deal #6', 'editing deal 6: ' + await text('#deal-title'));
  await p.click('#d-delete'); await p.waitForNetworkIdle(); await sleep(300);
  ok(/Deal #6 deleted/.test(await text('#toast')), 'delete toast: ' + await text('#toast'));
  ok(!(await dealNos()).includes('6'), 'deal 6 gone');

  // a deal that feeds a reinvestment cannot be deleted
  await (await editBtn(1)).click();
  await p.click('#d-delete'); await sleep(600);
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
  const custRows = await p.$$eval('#cust-box tbody tr', trs => trs.map(t => t.cells[1].textContent));
  ok(custRows.join('|') === 'Sample Breeder|Jane Doe|Alex Kumar Example', 'customers: ' + custRows.join('|'));
  await p.$eval('#cust-box tbody tr td.acts .btn', b => b.click());
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
  const dueTh = (await p.$$('#deals-box th'))[8];
  await dueTh.click(); await dueTh.evaluate(e => e); await sleep(100);
  await (await p.$$('#deals-box th'))[8].click(); await sleep(100);
  const firstDue = await p.$eval('#deals-box tbody tr td:nth-child(9)', e => e.textContent);
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

  ok(errs.length === 0, 'page errors: ' + errs.join(' | '));
  await browser.close();
  srv.kill();
  fs.rmSync(DATA, { recursive: true, force: true });
  console.log('passed', pass, 'failed', fail);
  process.exit(fail ? 1 : 0);
})().catch(e => { console.error(e); srv.kill(); process.exit(1); });
