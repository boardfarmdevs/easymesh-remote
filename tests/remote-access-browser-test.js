'use strict';
// The lab's page in a browser, against the fixture's three origins (three ports of localhost):
// the welcome, the workspace with every view in its tile, input in a tile of another origin
// renewing the reservation, layouts that never reload a view, a second account locked out, an
// administrator releasing and maintaining, idle expiry, and sign-out.

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
    const contexts = {};
    const pages = {};
    const errors = [];
    for (const name of ['alice', 'bob', 'carol']) {
      contexts[name] = await browser.newContext({ignoreHTTPSErrors: true, viewport: {width: 1400, height: 900}});
      pages[name] = await contexts[name].newPage();
      pages[name].on('pageerror', error => errors.push(`${name}: ${error.message}`));
      pages[name].on('dialog', dialog => dialog.accept());
    }
    const {alice, bob, carol} = pages;
    const view = (page, name) => page.frameLocator(`.tile[data-view="${name}"] iframe`).frameLocator('#view').locator('#probe');
    async function login(page, username, origin = settings.origins.topology) {
      await page.goto(origin + '/_remote/');
      await page.locator('#availability-text', {hasText: 'Sign in to use this lab'}).waitFor();
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
    const loads = async () => (await (await fetch(settings.loads)).json()).loads;
    const status = async name => (await contexts[name].request.get(settings.origins.topology + '/_remote/status')).json();
    const visibleTiles = page => page.locator('.tile:not(.concealed)').evaluateAll(tiles => tiles.map(tile => tile.dataset.view));

    // The welcome, signed out and in; then the workspace with every view.
    await alice.goto(settings.origins.topology + '/_remote/');
    await alice.locator('#title', {hasText: 'Fixture lab'}).waitFor();
    assert.equal(await alice.locator('#facts').isHidden(), true);
    assert.equal(await alice.locator('#view-cards li').count(), 3);
    await login(alice, 'alice');
    await alice.locator('#availability-text', {hasText: 'Free'}).waitFor();
    assert.match(await alice.locator('#facts').textContent(), /fixture-1006/);
    await alice.getByRole('button', {name: 'Reserve and open the lab'}).click();
    await alice.locator('#workspace').waitFor();
    for (const name of ['room', 'topology', 'console']) await view(alice, name).waitFor();
    assert.equal(await loads(), 3);
    assert.deepEqual(await visibleTiles(alice), ['room', 'topology', 'console']);
    await alice.locator('#state', {hasText: 'Reserved by you'}).waitFor();

    // Input inside a tile of another origin than the page renews the idle timer.
    await advance(20);
    await view(alice, 'console').click();
    for (let attempt = 0; attempt < 100; attempt++) {
      if ((await status('alice')).idle_remaining === 30) break;
      assert.ok(attempt < 99, 'trusted input in a cross-origin tile renews idle');
      await new Promise(resolve => setTimeout(resolve, 50));
    }

    // Layouts, tabs and maximizing rearrange the tiles without reloading a view.
    await alice.getByRole('button', {name: 'One large'}).click();
    assert.match(alice.url(), /#layout=focus$/);
    await alice.getByRole('button', {name: 'One at a time'}).click();
    assert.deepEqual(await visibleTiles(alice), ['room']);
    await alice.getByRole('tab', {name: 'Console NG'}).click();
    assert.deepEqual(await visibleTiles(alice), ['console']);
    assert.match(alice.url(), /#layout=tabs&view=console$/);
    await alice.getByRole('button', {name: 'Side by side'}).click();
    await alice.locator('.tile[data-view="topology"] [data-icon="maximize"]').click();
    assert.deepEqual(await visibleTiles(alice), ['topology']);
    await alice.locator('.toolbar-hint').click();
    await alice.keyboard.press('Escape');
    assert.deepEqual(await visibleTiles(alice), ['room', 'topology', 'console']);
    const gutter = alice.locator('.gutter').first();
    const before = await alice.locator('.tile[data-view="room"]').boundingBox();
    const handle = await gutter.boundingBox();
    await alice.mouse.move(handle.x + handle.width / 2, handle.y + handle.height / 2);
    await alice.mouse.down();
    await alice.mouse.move(handle.x + 150, handle.y + handle.height / 2, {steps: 5});
    await alice.mouse.up();
    const after = await alice.locator('.tile[data-view="room"]').boundingBox();
    assert.ok(after.width > before.width + 100, 'dragging a divider resizes the tiles');
    assert.equal(await loads(), 3, 'no view reloaded');

    // A second account sees who holds the lab and reaches nothing.
    await login(bob, 'bob', settings.origins.room);
    await bob.locator('#state', {hasText: 'In use by alice'}).waitFor();
    assert.equal(await bob.locator('#acquire').isDisabled(), true);
    assert.equal(await bob.locator('#manage').isHidden(), true);
    assert.equal(await bob.evaluate(async () => (await fetch('/api/echo')).status), 423);

    // The administrator sees who is signed in and releases the lab; alice's workspace closes.
    await login(carol, 'carol');
    await carol.getByRole('button', {name: 'Manage'}).click();
    await carol.locator('#admin-users li', {hasText: 'alice'}).waitFor();
    assert.equal(await carol.locator('#admin-users li').count(), 3);
    await carol.getByRole('button', {name: 'Release now'}).click();
    await alice.locator('#workspace').waitFor({state: 'hidden', timeout: 10000});
    await alice.locator('#ended').waitFor();
    assert.equal(await alice.evaluate(async () => (await fetch('/api/echo')).status), 423);

    // After the handoff bob reserves; maintenance ends his reservation and refuses another.
    await advance(3);
    await bob.locator('#acquire:enabled').waitFor({timeout: 10000});
    await bob.getByRole('button', {name: 'Reserve and open the lab'}).click();
    await view(bob, 'room').waitFor();
    await carol.locator('#admin-reservation', {hasText: 'bob holds the lab'}).waitFor({timeout: 10000});
    await carol.getByRole('button', {name: 'Start maintenance'}).click();
    await bob.locator('#ended', {hasText: 'maintenance'}).waitFor({timeout: 10000});
    await bob.locator('#availability-text', {hasText: 'Under maintenance'}).waitFor();
    assert.equal(await bob.locator('#acquire').isDisabled(), true);
    await carol.getByRole('button', {name: 'End maintenance'}).click();

    // Idle expiry closes the workspace; signing out returns to the welcome.
    await advance(3);
    await bob.locator('#acquire:enabled').waitFor({timeout: 10000});
    await bob.getByRole('button', {name: 'Reserve and open the lab'}).click();
    await view(bob, 'console').waitFor();
    await advance(31);
    await bob.locator('#workspace').waitFor({state: 'hidden', timeout: 10000});
    await bob.locator('#ended', {hasText: 'has ended'}).waitFor();
    await bob.getByRole('button', {name: 'Sign out', exact: true}).click();
    await bob.locator('#login').waitFor({state: 'visible'});
    assert.deepEqual(errors, []);
    console.log('PASS lab page: welcome, workspace of three origins, tile activity, layouts without reloads, ' +
      'second account locked out, administrator release and maintenance, idle expiry, sign-out');
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
