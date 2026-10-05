/* Real served frontend -> real Nginx/API -> explicitly local fixture provider.
   No browser profile, real credential, PubMed query or paid model call is used. */
const assert = require('node:assert/strict');
const {spawn, spawnSync} = require('node:child_process');
const path = require('node:path');
const fs = require('node:fs');
const {chromium} = require(process.env.PLAYWRIGHT_MODULE || path.resolve(__dirname, '../test_tools/node_modules/playwright'));
const root = path.resolve(__dirname, '../..');
const sentinel = 'local-ui-fixture-not-a-real-key';

(async () => {
  let browser, fixture;
  try {
    const copy = spawnSync('docker', ['cp', 'local_extensions/tests/live_fixture.py', 'pubchat-local-search-server:/app/local_byok_tests/live_fixture.py'], {cwd:root, encoding:'utf8'});
    assert.equal(copy.status, 0, 'Could not install local test fixture');
    fixture = spawn('docker', ['exec', '-i', 'pubchat-local-search-server', 'python', '-u', '/app/local_byok_tests/live_fixture.py'], {cwd:root, stdio:['pipe','pipe','pipe']});
    let raw = '';
    const base = await new Promise((resolve, reject) => {
      const timer = setTimeout(() => reject(new Error('Local fixture readiness timeout')), 20000);
      fixture.once('exit', code => {clearTimeout(timer); reject(new Error('Fixture exited before readiness: ' + code));});
      fixture.stdout.on('data', chunk => {
        raw += chunk.toString();
        const match = raw.match(/READY (\{[^\n]+\})/);
        if (match) {clearTimeout(timer); resolve(JSON.parse(match[1]).base);}
      });
    });
    const windowsChrome = 'C:/Program Files/Google/Chrome/Application/chrome.exe';
    const executablePath = process.env.CHROME_PATH || (process.platform === 'win32' && fs.existsSync(windowsChrome) ? windowsChrome : undefined);
    browser = await chromium.launch({headless:true, executablePath});
    const context = await browser.newContext();
    const page = await context.newPage();
    const errors = [];
    page.on('pageerror', error => errors.push(error.message));
    await page.route('**/*', async route => {
      const url = new URL(route.request().url());
      if (!['localhost','127.0.0.1'].includes(url.hostname)) return route.abort();
      return route.continue();
    });
    await page.goto('http://localhost:8000/search/task');
    await page.waitForSelector('#pubchat-byok');
    await page.locator('#pubchat-byok summary').click();
    const field = name => page.locator(`#pubchat-byok [data-field="${name}"]`);
    await field('enabled').check();
    await field('main').fill(sentinel);
    await field('model_pro').fill('exact-fixture-model');
    await field('embedding_base_url').fill(base + '/v1');
    await field('embedding_model').fill('fixture-embedding');
    for (const protocol of ['openai','gemini','anthropic']) {
      await field('protocol').selectOption(protocol);
      await field('base_url').fill(base + (protocol === 'openai' ? '/v1' : ''));
      page.once('dialog', dialog => dialog.accept());
      await page.locator('[data-test]').click();
      await page.waitForFunction(() => document.querySelector('#pubchat-byok [data-status]').textContent.includes('Configured text and enabled embedding endpoints responded correctly'), null, {timeout:20000});
      console.log('PASS live frontend -> API -> local fixture:', protocol);
    }
    const status = await page.locator('[data-status]').textContent();
    assert.ok(!status.includes(sentinel));
    const storage = await page.evaluate(() => JSON.stringify({local:{...localStorage},session:{...sessionStorage}}));
    assert.ok(!storage.includes(sentinel));
    assert.deepEqual(errors, []);
    await page.reload();
    await page.waitForSelector('#pubchat-byok');
    assert.equal(await field('main').inputValue(), '');
    console.log('PASS real-served UI: no script errors, secret-free storage, reload clears key');
    await context.close();
  } finally {
    if (browser) await browser.close();
    if (fixture) {
      fixture.stdin.end('STOP\n');
      await new Promise(resolve => {
        if (fixture.exitCode !== null) return resolve();
        const timeout = setTimeout(() => {fixture.kill(); resolve();}, 10000);
        fixture.once('exit', () => {clearTimeout(timeout); resolve();});
      });
    }
  }
})().catch(error => {console.error(error.message); process.exitCode=1;});
