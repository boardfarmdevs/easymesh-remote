'use strict';

const assert = require('assert').strict;
const path = require('path');
const {spawn} = require('child_process');
const readline = require('readline');
const {chromium} = require(process.env.PLAYWRIGHT_MODULE || 'playwright-core');

async function main() {
  const fixture = spawn('python3', [path.join(__dirname, 'remote-access-fixture.py')], {stdio: ['ignore', 'pipe', 'pipe']});
  let diagnostic = '';
  fixture.stderr.on('data', chunk => { diagnostic += chunk; });
  let browser;
  try {
    const settings = await new Promise((resolve, reject) => {
      const timer = setTimeout(() => reject(new Error('Fixture startup timeout: ' + diagnostic)), 15000);
      const lines = readline.createInterface({input: fixture.stdout});
      lines.once('line', line => {
        clearTimeout(timer);
        try { resolve(JSON.parse(line)); } catch (error) { reject(error); }
      });
      fixture.once('exit', code => { clearTimeout(timer); reject(new Error(`Fixture exited ${code}: ${diagnostic}`)); });
    });
    browser = await chromium.launch({headless: true,
      ...(process.env.CHROMIUM_PATH ? {executablePath: process.env.CHROMIUM_PATH} : {}), args: ['--no-sandbox']});
    const aliceContext = await browser.newContext({ignoreHTTPSErrors: true});
    const bobContext = await browser.newContext({ignoreHTTPSErrors: true});
    const alice = await aliceContext.newPage();
    const bob = await bobContext.newPage();
    const errors = [];
    for (const page of [alice, bob]) page.on('pageerror', error => errors.push(error.message));
    async function login(page, username) {
      await page.goto(settings.origins.topology + '/_remote/');
      await page.locator('[name=username]').fill(username);
      await page.locator('[name=password]').fill('remote fixture password');
      await page.getByRole('button', {name: 'Sign in', exact: true}).click();
      await page.locator('#login').waitFor({state: 'hidden'});
    }
    async function advance(seconds) {
      const response = await fetch(settings.control, {method: 'POST', headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({seconds})});
      assert.equal(response.status, 200);
    }
    await login(alice, 'alice');
    await alice.getByRole('button', {name: 'Reserve lab'}).click();
    await alice.frameLocator('#view').locator('#probe').waitFor();
    await login(bob, 'bob');
    await bob.locator('#status', {hasText: 'In use by alice'}).waitFor();
    assert.equal(await bob.locator('#acquire').isDisabled(), true);
    assert.equal(await bob.evaluate(async () => (await fetch('/api/echo')).status), 423);
    await alice.bringToFront();
    await advance(20);
    await alice.frameLocator('#view').locator('#probe').click();
    for (let attempt = 0; attempt < 100; attempt++) {
      const status = await (await aliceContext.request.get(settings.origins.topology + '/_remote/status')).json();
      if (status.idle_remaining === 30) break;
      assert.ok(attempt < 99, 'trusted iframe input renews idle');
      await new Promise(resolve => setTimeout(resolve, 50));
    }
    await alice.getByRole('link', {name: 'console', exact: true}).click();
    await alice.frameLocator('#view').locator('#probe').waitFor();
    assert.equal(await alice.locator('#login').isHidden(), true);
    assert.match(alice.url(), new RegExp(':' + new URL(settings.origins.console).port));
    await advance(31);
    await alice.locator('#view').waitFor({state: 'hidden', timeout: 10000});
    assert.equal(await alice.evaluate(async () => (await fetch('/api/echo')).status), 423);
    await advance(3);
    await bob.bringToFront();
    await bob.locator('#acquire:enabled').waitFor({timeout: 10000});
    await bob.getByRole('button', {name: 'Reserve lab'}).click();
    await bob.frameLocator('#view').locator('#probe').waitFor();
    await bob.getByRole('button', {name: 'Release lab'}).click();
    await bob.locator('#view').waitFor({state: 'hidden'});
    await bob.getByRole('button', {name: 'Sign out', exact: true}).click();
    await bob.locator('#login').waitFor({state: 'visible'});
    assert.deepEqual(errors, []);
    console.log('PASS remote portal: isolated accounts, cross-port cookie, iframe activity, idle expiry, handoff and release');
  } finally {
    if (browser) await browser.close();
    fixture.kill('SIGTERM');
    await new Promise(resolve => {
      if (fixture.exitCode !== null) return resolve();
      const timer = setTimeout(() => { fixture.kill('SIGKILL'); resolve(); }, 5000);
      fixture.once('exit', () => { clearTimeout(timer); resolve(); });
    });
  }
}

main().catch(error => { console.error(error); process.exitCode = 1; });
