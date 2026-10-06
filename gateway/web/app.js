'use strict';
// The lab's page. Before a reservation: what the lab is, whether it is free, sign-in and reserve,
// and for an administrator the lab's management. During one: every view of the lab in one window,
// side by side, one large beside the others, or one at a time; any of them maximized or full
// screen. Each view keeps its own origin, framed there by a tile (/_remote/tile) that reports
// genuine input in it; this page reports its own. The gateway decides everything: this page shows.

const elements = Object.fromEntries([
  'bar-title', 'bar-meta', 'state', 'timers', 'idle', 'limit', 'keep', 'release', 'manage', 'who', 'logout',
  'error', 'welcome', 'title', 'summary', 'facts', 'availability-dot', 'availability-text',
  'availability-detail', 'ended', 'login', 'reserve', 'acquire', 'reserve-note', 'resume', 'open',
  'view-cards', 'rules', 'workspace', 'layouts', 'tabs', 'close-workspace', 'tiles', 'admin', 'admin-close',
  'admin-reservation', 'admin-release', 'admin-maintenance-state', 'admin-maintenance', 'admin-users',
  'admin-commands',
].map(id => [id.replace(/-(\w)/g, (_, letter) => letter.toUpperCase()), document.getElementById(id)]));

const LAYOUTS = [['columns', 'Side by side'], ['focus', 'One large'], ['tabs', 'One at a time']];
const PREFERRED = ['room', 'topology', 'console'];
const GUTTER = 8;
const ICONS = {
  columns: 'M3 5h18v14H3z M9 5v14 M15 5v14',
  focus: 'M3 5h18v14H3z M14 5v14 M14 12h7',
  tabs: 'M3 9h18v10H3z M3 9V5h7l1 4',
  maximize: 'M4 4h16v16H4z M4 8h16',
  restore: 'M8 8h12v12H8z M4 16V4h12',
  fullscreen: 'M4 9V4h5 M20 9V4h-5 M4 15v5h5 M20 15v5h-5',
  exit: 'M9 4v5H4 M15 4v5h5 M9 20v-5H4 M15 20v-5h5',
  open: 'M14 4h6v6 M20 4l-9 9 M18 14v6H4V6h6',
  reload: 'M20 12a8 8 0 1 1-2.3-5.7 M20 4v5h-5',
};

let state = null;
let receivedAt = 0;
let showing = 'welcome';
let firstStatus = true;
let wasMine = false;
let releasing = false;
let statusPending = false;
let refreshTimer = null;
let lastActivity = 0;
const layout = {name: 'columns', view: null, maximized: null, sizes: {}};
const narrow = window.matchMedia('(max-width: 860px)');

function el(tag, attributes = {}, ...children) {
  const node = document.createElement(tag);
  for (const [name, value] of Object.entries(attributes)) {
    if (name.startsWith('on')) node.addEventListener(name.slice(2), value);
    else if (value === true) node.setAttribute(name, '');
    else if (value !== false && value !== null && value !== undefined) node.setAttribute(name, value);
  }
  node.append(...children.filter(child => child !== null && child !== undefined && child !== false));
  return node;
}

function icon(name) {
  const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
  svg.setAttribute('viewBox', '0 0 24 24');
  svg.setAttribute('aria-hidden', 'true');
  for (const [key, value] of Object.entries({fill: 'none', stroke: 'currentColor', 'stroke-width': '2',
    'stroke-linecap': 'round', 'stroke-linejoin': 'round'})) svg.setAttribute(key, value);
  const path = document.createElementNS('http://www.w3.org/2000/svg', 'path');
  path.setAttribute('d', ICONS[name]);
  svg.append(path);
  return svg;
}

const store = {
  read(key, fallback) {
    try { return JSON.parse(localStorage.getItem(key)) ?? fallback; } catch (error) { return fallback; }
  },
  write(key, value) {
    try { localStorage.setItem(key, JSON.stringify(value)); } catch (error) { /* a private window: not kept */ }
  },
};

function showError(message) {
  elements.error.textContent = message || '';
  elements.error.hidden = !message;
}

async function request(operation, body) {
  const response = await fetch('/_remote/' + operation, {
    method: body === undefined ? 'GET' : 'POST',
    headers: body === undefined ? {} : {'Content-Type': 'application/json'},
    body: body === undefined ? undefined : JSON.stringify(body),
    cache: 'no-store', signal: AbortSignal.timeout(8000),
  });
  if (!response.ok) throw new Error((await response.text()).replace(/^\d{3}: [^\n]*\n\n/, ''));
  return response.json();
}

function remaining(key) {
  return Math.max(0, (state?.[key] || 0) - (Date.now() - receivedAt) / 1000);
}

function clock(value) {
  const total = Math.ceil(value);
  const hours = Math.floor(total / 3600);
  const minutes = Math.floor(total % 3600 / 60);
  const seconds = String(total % 60).padStart(2, '0');
  return hours ? `${hours}:${String(minutes).padStart(2, '0')}:${seconds}` : `${minutes}:${seconds}`;
}

function humane(value) {
  const minutes = Math.floor(value / 60);
  const seconds = value % 60;
  return [minutes && `${minutes} minute${minutes === 1 ? '' : 's'}`, seconds && `${seconds} second${seconds === 1 ? '' : 's'}`]
    .filter(Boolean).join(' ') || '0 seconds';
}

function phase() {
  if (!state) return 'down';
  if (!state.authenticated) return 'anonymous';
  if (state.mine) return 'mine';
  if (state.maintenance) return 'maintenance';
  if (state.busy) return 'busy';
  if (remaining('handoff_remaining') > 0) return 'settling';
  return 'free';
}

function viewOrder() {
  const names = Object.keys(state?.views || {});
  const rank = name => PREFERRED.includes(name) ? PREFERRED.indexOf(name) : PREFERRED.length + names.indexOf(name);
  return names.sort((first, second) => rank(first) - rank(second));
}

// The welcome and the bar

function renderCard() {
  const card = state.card || {};
  document.title = card.title || 'EasyMesh lab';
  elements.barTitle.textContent = elements.title.textContent = card.title || 'EasyMesh lab';
  elements.barMeta.textContent = [card.vm, card.host].filter(Boolean).join(' · ');
  elements.summary.textContent = card.summary || '';
  const facts = [['Lab', state.lab], ['VM', card.vm], ['Host', card.host], ['Built', card.built],
    ['Address', location.hostname]].filter(([, value]) => value);
  elements.facts.replaceChildren(...facts.flatMap(([name, value]) => [el('dt', {}, name), el('dd', {}, value)]));
  elements.facts.hidden = !state.authenticated;
  elements.viewCards.replaceChildren(...viewOrder().map(name => {
    const view = state.views[name];
    return el('li', {class: 'card'}, el('h3', {}, view.title), el('p', {}, view.summary),
      el('span', {class: 'address'}, new URL(view.origin).host));
  }));
  const rules = state.rules || {};
  elements.rules.replaceChildren(...[
    ['One person drives the lab at a time.', ' The reservation covers every view and its API, in every tab of that browser.'],
    ['Real input keeps it.', ` Clicks, keys, scrolling and touch renew it; after ${humane(rules.idle_seconds)} without them it ends. Watching, automatic updates and an unattended Play do not count.`],
    ['It has a hard limit.', ` ${humane(rules.maximum_seconds)} at most; activity does not extend it.`],
    ['Then the lab settles.', ` After a reservation ends the next one waits ${humane(rules.handoff_seconds)}, so the room's own operator lease can lapse.`],
    ['Release it when you are done.', ' Signing out releases it too.'],
  ].map(([lead, rest]) => el('li', {}, el('strong', {}, lead), rest)));
}

function renderBar() {
  const current = phase();
  const labels = {
    down: 'Gateway unreachable', anonymous: 'Signed out', mine: 'Reserved by you', maintenance: 'Maintenance',
    busy: `In use by ${state?.owner}`, settling: `Settling · ${clock(remaining('handoff_remaining'))}`, free: 'Free',
  };
  elements.state.className = 'pill ' + current;
  elements.state.textContent = labels[current];
  const mine = current === 'mine';
  elements.timers.hidden = !mine;
  if (mine) {
    elements.idle.textContent = clock(remaining('idle_remaining'));
    elements.limit.textContent = clock(remaining('maximum_remaining'));
    elements.idle.parentElement.classList.toggle('low', remaining('idle_remaining') < 60);
    elements.limit.parentElement.classList.toggle('low', remaining('maximum_remaining') < 300);
  }
  elements.keep.hidden = elements.release.hidden = !mine;
  elements.manage.hidden = state?.role !== 'admin';
  elements.who.hidden = elements.logout.hidden = !state?.authenticated;
  if (state?.authenticated) {
    elements.who.replaceChildren(el('strong', {}, state.username), el('span', {class: 'role'}, state.role || 'operator'));
  }
}

function renderDoor() {
  const current = phase();
  const rules = state?.rules || {};
  const text = {
    down: ['The gateway cannot be reached', 'The lab\'s page keeps trying every five seconds.'],
    anonymous: ['Sign in to use this lab', 'Accounts are given by the lab\'s administrator.'],
    free: ['Free', 'Nobody holds the lab. Reserve it to open every view.'],
    mine: ['Reserved by you', `Idle ends it in ${clock(remaining('idle_remaining'))} · the limit in ${clock(remaining('maximum_remaining'))}`],
    busy: [`In use by ${state?.owner}`, `Free within ${clock(Math.min(remaining('idle_remaining'), remaining('maximum_remaining')) + (rules.handoff_seconds || 0))} at the latest, or sooner when released`],
    settling: ['Settling after the last session', `Free in ${clock(remaining('handoff_remaining'))}`],
    maintenance: ['Under maintenance', 'An administrator is working on the lab or running its tests.'],
  }[current];
  elements.availabilityDot.className = 'dot ' + current;
  elements.availabilityText.textContent = text[0];
  elements.availabilityDetail.textContent = text[1];
  elements.login.hidden = Boolean(state?.authenticated) || current === 'down';
  elements.reserve.hidden = !state?.authenticated || current === 'mine';
  elements.acquire.disabled = current !== 'free';
  elements.reserveNote.textContent = current === 'free'
    ? `One reservation covers every view. It ends after ${humane(rules.idle_seconds)} without input, or after ${humane(rules.maximum_seconds)}.`
    : 'Reserving opens when the lab is free.';
  elements.resume.hidden = !(current === 'mine' && showing === 'welcome');
}

function renderAdmin() {
  const admin = state?.role === 'admin';
  if (!admin) {
    elements.admin.hidden = true;
    elements.manage.setAttribute('aria-expanded', 'false');
    return;
  }
  elements.adminReservation.textContent = state.busy
    ? `${state.owner} holds the lab · idle ends it in ${clock(remaining('idle_remaining'))}, the limit in ${clock(remaining('maximum_remaining'))}.`
    : 'Nobody holds the lab.';
  elements.adminRelease.disabled = !state.busy;
  elements.adminMaintenanceState.textContent = state.maintenance
    ? 'On: reservations are refused.' : 'Off: the lab can be reserved.';
  elements.adminMaintenance.textContent = state.maintenance ? 'End maintenance' : 'Start maintenance';
  elements.adminMaintenance.className = 'button' + (state.maintenance ? '' : ' danger');
  elements.adminUsers.replaceChildren(...(state.signed_in || []).map(entry => el('li', {},
    el('span', {}, entry.username, entry.username === state.owner ? el('span', {class: 'role'}, 'holds the lab') : null),
    el('span', {class: 'badge'}, `${entry.sessions} browser${entry.sessions === 1 ? '' : 's'}`))));
  const command = `sudo /opt/easymesh-remote/manage.py --lab ${state.lab}`;
  elements.adminCommands.textContent = [`${command} users`, `${command} add-user NAME [--role admin]`,
    `${command} role NAME admin|operator`, `${command} remove-user NAME`].join('\n\n');
}

function tick() {
  if (!state) return;
  renderBar();
  renderDoor();
  renderAdmin();
}

function render(next) {
  state = next;
  receivedAt = Date.now();
  const mine = Boolean(state.mine);
  if (wasMine && !mine) {
    endWorkspace();
    elements.ended.textContent = releasing ? 'You released the lab.' : state.maintenance
      ? 'Your reservation ended: an administrator started maintenance.'
      : 'Your reservation has ended: it was idle, reached its limit, or was released by an administrator.';
    elements.ended.hidden = false;
  } else if (mine) {
    elements.ended.hidden = true;
  }
  releasing = false;
  wasMine = mine;
  renderCard();
  if (mine && firstStatus) showWorkspace();
  firstStatus = false;
  tick();
}

async function refresh() {
  if (statusPending) return;
  statusPending = true;
  try {
    render(await request('status'));
    if (elements.error.dataset.source === 'status') showError('');
  } catch (error) {
    if (!state) {
      tick();
      renderDoor();
      elements.state.className = 'pill down';
      elements.state.textContent = 'Gateway unreachable';
    }
    showError('The gateway did not answer: ' + error.message);
    elements.error.dataset.source = 'status';
  } finally {
    statusPending = false;
  }
}

function refreshSoon() {
  clearTimeout(refreshTimer);
  refreshTimer = setTimeout(refresh, 300);
}

async function action(operation, body = {}) {
  try {
    showError('');
    render(await request(operation, body));
    return true;
  } catch (error) {
    showError(error.message);
    delete elements.error.dataset.source;
    await refresh();
    return false;
  }
}

// The workspace

function storageKey() {
  return 'easymesh-remote:' + state.lab + ':layout';
}

function loadLayout() {
  const saved = store.read(storageKey(), {});
  Object.assign(layout, {name: saved.name, view: saved.view, maximized: saved.maximized, sizes: saved.sizes || {}});
  const hash = new URLSearchParams(location.hash.slice(1));
  if (hash.has('layout')) Object.assign(layout, {name: hash.get('layout'), view: hash.get('view'), maximized: hash.get('max')});
  if (!LAYOUTS.some(([name]) => name === layout.name)) layout.name = 'columns';
}

function saveLayout() {
  store.write(storageKey(), layout);
  const hash = new URLSearchParams({layout: layout.name});
  if (layout.name === 'tabs' && layout.view) hash.set('view', layout.view);
  if (layout.maximized) hash.set('max', layout.maximized);
  history.replaceState(null, '', '#' + hash);
}

function sized(key, count, defaults) {
  const sizes = layout.sizes[key];
  if (!Array.isArray(sizes) || sizes.length !== count || !sizes.every(size => size > 0)) layout.sizes[key] = defaults;
  return layout.sizes[key];
}

function effectiveLayout() {
  return narrow.matches ? 'tabs' : layout.name;
}

function tiles() {
  return [...elements.tiles.querySelectorAll('.tile')];
}

function iconButton(name, title, onclick) {
  return el('button', {class: 'icon-button', type: 'button', title, 'aria-label': title, 'data-icon': name, onclick}, icon(name));
}

function setIcon(button, name, title) {
  if (button.dataset.icon === name) return;
  button.dataset.icon = name;
  button.title = title;
  button.setAttribute('aria-label', title);
  button.replaceChildren(icon(name));
}

function watchLoad(tile) {
  // A tile whose address does not answer (a certificate refused, a port taken) shows the
  // browser's own error page, which this page cannot see into: no 'loaded' message comes.
  clearTimeout(tile.loadTimer);
  tile.loadTimer = setTimeout(() => {
    tile.querySelector('.tile-note').textContent = `Not loaded: ${new URL(tile.dataset.origin).host} does not answer as this lab`;
  }, 20000);
}

function tileFor(name) {
  const view = state.views[name];
  const frame = el('iframe', {src: view.origin + '/_remote/tile', title: view.title, allow: 'fullscreen', allowfullscreen: true});
  const note = el('span', {class: 'tile-note'});
  const tile = el('article', {class: 'tile', 'data-view': name, 'data-origin': view.origin, 'aria-label': view.title},
    el('header', {class: 'tile-bar', ondblclick: event => { if (!event.target.closest('button, a')) toggleMaximize(name); }},
      el('span', {class: 'tile-title'}, view.title), note,
      el('span', {class: 'tile-tools'},
        iconButton('reload', 'Reload this view', () => {
          note.textContent = '';
          watchLoad(tile);
          frame.src = view.origin + '/_remote/tile';
        }),
        el('a', {class: 'icon-button', href: `${view.origin}/_remote/#layout=tabs&view=${name}`, target: '_blank',
          rel: 'noopener', title: 'Open alone in a new tab', 'aria-label': 'Open alone in a new tab'}, icon('open')),
        iconButton('maximize', 'Maximize in this window', () => toggleMaximize(name)),
        iconButton('fullscreen', 'Full screen', () => toggleFullscreen(tile)))),
    frame);
  watchLoad(tile);
  return tile;
}

function buildLayoutButtons() {
  elements.layouts.replaceChildren(...LAYOUTS.map(([name, label]) => el('button', {type: 'button', 'data-layout': name,
    onclick: () => { layout.name = name; layout.maximized = null; applyLayout(); }}, icon(name), label)));
}

function templates() {
  const count = tiles().length;
  const grid = elements.tiles.style;
  const name = effectiveLayout();
  const tracks = sizes => sizes.map(size => `minmax(0, ${size}fr)`).join(` ${GUTTER}px `);
  if (name === 'columns' && count > 1) {
    grid.gridTemplateColumns = tracks(sized('columns', count, [1.35, ...Array(count - 1).fill(1)]));
    grid.gridTemplateRows = 'minmax(0, 1fr)';
    grid.gridTemplateAreas = '"' + tiles().flatMap((tile, index) => index ? ['g' + index, 't' + index] : ['t0']).join(' ') + '"';
  } else if (name === 'focus' && count > 1) {
    grid.gridTemplateColumns = tracks(sized('focusX', 2, [1.6, 1]));
    grid.gridTemplateRows = tracks(sized('focusY', count - 1, Array(count - 1).fill(1)));
    const rows = [];
    for (let index = 1; index < count; index++) {
      if (index > 1) rows.push(`"t0 gv h${index - 1}"`);
      rows.push(`"t0 gv t${index}"`);
    }
    grid.gridTemplateAreas = rows.join(' ');
  } else {
    grid.gridTemplateColumns = grid.gridTemplateRows = 'minmax(0, 1fr)';
    grid.gridTemplateAreas = '"stack"';
  }
}

function addGutter(area, axis, key, index) {
  const gutter = el('div', {class: `gutter gutter-${axis}`, role: 'separator', tabindex: '0',
    'aria-orientation': axis === 'x' ? 'vertical' : 'horizontal', 'aria-label': 'Resize the views',
    title: 'Drag to resize · double-click to share evenly'});
  gutter.style.gridArea = area;
  gutter.addEventListener('pointerdown', event => drag(event, gutter, axis, key, index));
  gutter.addEventListener('dblclick', () => {
    layout.sizes[key] = layout.sizes[key].map(() => 1);
    templates();
    saveLayout();
  });
  gutter.addEventListener('keydown', event => {
    const step = {ArrowLeft: -1, ArrowUp: -1, ArrowRight: 1, ArrowDown: 1}[event.key];
    if (!step) return;
    event.preventDefault();
    const sizes = layout.sizes[key];
    const pair = sizes[index] + sizes[index + 1];
    sizes[index] = Math.min(pair * 0.9, Math.max(pair * 0.1, sizes[index] + step * pair * 0.04));
    sizes[index + 1] = pair - sizes[index];
    templates();
    saveLayout();
  });
  elements.tiles.append(gutter);
}

function drag(event, gutter, axis, key, index) {
  if (event.button !== 0) return;
  event.preventDefault();
  gutter.setPointerCapture(event.pointerId);
  const sizes = layout.sizes[key];
  const box = elements.tiles.getBoundingClientRect();
  const length = (axis === 'x' ? box.width : box.height) - 12 - GUTTER * (sizes.length - 1);
  const perUnit = length / sizes.reduce((sum, size) => sum + size, 0);
  const origin = axis === 'x' ? event.clientX : event.clientY;
  const start = sizes[index];
  const pair = sizes[index] + sizes[index + 1];
  const minimum = Math.min(pair / 2, 160 / perUnit);
  elements.tiles.classList.add('dragging');
  gutter.classList.add('active');
  const move = moving => {
    const delta = ((axis === 'x' ? moving.clientX : moving.clientY) - origin) / perUnit;
    sizes[index] = Math.min(pair - minimum, Math.max(minimum, start + delta));
    sizes[index + 1] = pair - sizes[index];
    templates();
  };
  const end = () => {
    gutter.removeEventListener('pointermove', move);
    elements.tiles.classList.remove('dragging');
    gutter.classList.remove('active');
    saveLayout();
  };
  gutter.addEventListener('pointermove', move);
  gutter.addEventListener('pointerup', end, {once: true});
  gutter.addEventListener('pointercancel', end, {once: true});
}

function applyLayout() {
  const all = tiles();
  const names = all.map(tile => tile.dataset.view);
  if (!names.includes(layout.view)) layout.view = names[0];
  if (!names.includes(layout.maximized)) layout.maximized = null;
  const name = effectiveLayout();
  elements.tiles.querySelectorAll('.gutter').forEach(gutter => gutter.remove());
  templates();
  if (name === 'columns') {
    for (let index = 1; index < all.length; index++) addGutter('g' + index, 'x', 'columns', index - 1);
  } else if (name === 'focus' && all.length > 1) {
    addGutter('gv', 'x', 'focusX', 0);
    for (let index = 1; index < all.length - 1; index++) addGutter('h' + index, 'y', 'focusY', index - 1);
  }
  all.forEach((tile, index) => {
    const view = tile.dataset.view;
    const maximized = layout.maximized === view;
    tile.style.gridArea = maximized ? '1 / 1 / -1 / -1' : name === 'tabs' || all.length === 1 ? 'stack' : 't' + index;
    tile.classList.toggle('maximized', maximized);
    tile.classList.toggle('concealed', layout.maximized ? !maximized : name === 'tabs' && view !== layout.view);
    setIcon(tile.querySelector('[data-icon="maximize"], [data-icon="restore"]'),
      maximized ? 'restore' : 'maximize', maximized ? 'Restore the layout' : 'Maximize in this window');
  });
  for (const button of elements.layouts.querySelectorAll('button')) {
    button.setAttribute('aria-pressed', String(button.dataset.layout === name && !layout.maximized));
    button.disabled = narrow.matches && button.dataset.layout !== 'tabs';
  }
  elements.tabs.hidden = name !== 'tabs' && !layout.maximized;
  elements.tabs.replaceChildren(...names.map(view => el('button', {class: 'tab', type: 'button', role: 'tab',
    'aria-selected': String(view === (layout.maximized || layout.view)),
    onclick: () => {
      if (layout.maximized) layout.maximized = view;
      else layout.view = view;
      applyLayout();
    }}, state.views[view].title)));
  saveLayout();
}

function toggleMaximize(name) {
  layout.maximized = layout.maximized === name ? null : name;
  applyLayout();
}

function toggleFullscreen(tile) {
  if (document.fullscreenElement === tile) document.exitFullscreen().catch(() => {});
  else tile.requestFullscreen().catch(error => showError('Full screen was refused: ' + error.message));
}

function ensureWorkspace() {
  if (tiles().length) return;
  loadLayout();
  elements.tiles.replaceChildren(...viewOrder().map(tileFor));
  buildLayoutButtons();
  applyLayout();
}

function showWorkspace() {
  showing = 'workspace';
  ensureWorkspace();
  elements.welcome.hidden = true;
  elements.workspace.hidden = false;
  tick();
}

function showWelcome() {
  showing = 'welcome';
  elements.workspace.hidden = true;
  elements.welcome.hidden = false;
  tick();
}

function endWorkspace() {
  if (document.fullscreenElement) document.exitFullscreen().catch(() => {});
  tiles().forEach(tile => clearTimeout(tile.loadTimer));
  elements.tiles.replaceChildren();
  showWelcome();
}

// Activity, messages and controls

function activity(event) {
  if (!state?.mine || !event.isTrusted || document.visibilityState !== 'visible' || !document.hasFocus()) return;
  if (event.type === 'pointermove' && !event.buttons) return;
  const now = Date.now();
  if (now - lastActivity < 15000) return;
  lastActivity = now;
  action('activity');
}

for (const name of ['pointerdown', 'pointerup', 'pointermove', 'keydown', 'wheel', 'touchstart']) {
  document.addEventListener(name, activity, {passive: true, capture: true});
}

window.addEventListener('message', event => {
  const tile = tiles().find(candidate => candidate.dataset.origin === event.origin);
  if (!tile || event.data?.source !== 'easymesh-remote-tile') return;
  const note = tile.querySelector('.tile-note');
  if (event.data.type === 'activity') refreshSoon();
  else if (event.data.type === 'loaded') {
    clearTimeout(tile.loadTimer);
    note.textContent = '';
  }
  else if (event.data.type === 'outside') note.textContent = 'This view left the lab\'s address: reload it';
});

document.addEventListener('fullscreenchange', () => {
  for (const tile of tiles()) {
    const button = tile.querySelector('[data-icon="fullscreen"], [data-icon="exit"]');
    const full = document.fullscreenElement === tile;
    setIcon(button, full ? 'exit' : 'fullscreen', full ? 'Leave full screen' : 'Full screen');
  }
});

document.addEventListener('keydown', event => {
  if (event.key === 'Escape' && layout.maximized && !document.fullscreenElement && showing === 'workspace') {
    toggleMaximize(layout.maximized);
  }
});

narrow.addEventListener('change', () => { if (tiles().length) applyLayout(); });

elements.login.addEventListener('submit', async event => {
  event.preventDefault();
  if (await action('login', Object.fromEntries(new FormData(elements.login)))) {
    elements.login.reset();
    if (state.mine) showWorkspace();
  }
});
elements.acquire.addEventListener('click', async () => {
  if (await action('acquire') && state.mine) showWorkspace();
});
elements.open.addEventListener('click', showWorkspace);
elements.closeWorkspace.addEventListener('click', showWelcome);
document.querySelector('.brand').addEventListener('click', event => {
  // Back to the lab's page without reloading the page, and with it every view.
  event.preventDefault();
  showWelcome();
});
elements.release.addEventListener('click', () => {
  releasing = true;
  action('release');
});
elements.logout.addEventListener('click', () => {
  releasing = true;
  action('logout');
});
elements.keep.addEventListener('click', event => {
  if (event.isTrusted) action('activity');
});
elements.manage.addEventListener('click', () => {
  elements.admin.hidden = !elements.admin.hidden;
  elements.manage.setAttribute('aria-expanded', String(!elements.admin.hidden));
});
elements.adminClose.addEventListener('click', () => {
  elements.admin.hidden = true;
  elements.manage.setAttribute('aria-expanded', 'false');
});
elements.adminRelease.addEventListener('click', () => {
  if (confirm(`Release the lab held by ${state.owner}? They lose it at once.`)) action('admin', {action: 'release'});
});
elements.adminMaintenance.addEventListener('click', () => {
  if (state.maintenance) action('admin', {action: 'maintenance-off'});
  else if (confirm('Start maintenance? It ends the current reservation and refuses new ones.')) {
    action('admin', {action: 'maintenance-on'});
  }
});

setInterval(refresh, 5000);
setInterval(tick, 1000);
refresh();
