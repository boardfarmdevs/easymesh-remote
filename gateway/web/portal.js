'use strict';

const elements = Object.fromEntries(['title', 'links', 'status', 'keep', 'release', 'logout', 'lobby',
  'login', 'acquire', 'detail', 'error', 'view'].map(name => [name, document.getElementById(name)]));
let mine = false;
let mounted = false;
let lastActivity = 0;
let statusPending = false;

function showError(message) {
  elements.error.textContent = message || '';
  elements.error.hidden = !message;
}

async function request(operation, body) {
  const response = await fetch('/_remote/' + operation, {
    method: body === undefined ? 'GET' : 'POST',
    headers: body === undefined ? {} : {'Content-Type': 'application/json'},
    body: body === undefined ? undefined : JSON.stringify(body),
    cache: 'no-store', signal: AbortSignal.timeout(8000)
  });
  if (!response.ok) throw new Error(await response.text());
  return response.json();
}

function seconds(value) {
  const remaining = Math.max(0, Math.ceil(value));
  return `${Math.floor(remaining / 60)}m ${remaining % 60}s`;
}

function unmount() {
  if (document.fullscreenElement) document.exitFullscreen().catch(() => {});
  elements.view.hidden = true;
  elements.view.removeAttribute('src');
  mounted = false;
}

function render(state) {
  mine = Boolean(state.mine);
  elements.title.textContent = `${state.lab} · ${state.service}`;
  elements.links.replaceChildren(...Object.entries(state.links).map(([name, url]) => {
    const anchor = document.createElement('a');
    anchor.textContent = name;
    anchor.href = url;
    return anchor;
  }));
  elements.login.hidden = state.authenticated;
  elements.logout.hidden = !state.authenticated;
  elements.release.hidden = !mine;
  elements.keep.hidden = !mine;
  elements.lobby.hidden = mine;
  elements.acquire.hidden = !state.authenticated || mine;
  elements.acquire.disabled = Boolean(state.busy || state.handoff_remaining || state.maintenance);
  if (mine) {
    elements.status.textContent = `Reserved by you · idle ${seconds(state.idle_remaining)} · limit ${seconds(state.maximum_remaining)}`;
    if (!mounted) {
      elements.view.src = '/';
      elements.view.hidden = false;
      mounted = true;
    }
  } else {
    if (mounted) unmount();
    const message = !state.authenticated ? 'Sign in to see availability' : state.maintenance
      ? 'Reserved by administrator for maintenance or local testing' : state.busy
      ? `In use by ${state.owner} · idle ${seconds(state.idle_remaining)} · limit ${seconds(state.maximum_remaining)}`
      : state.handoff_remaining ? `Previous room lease settling · ${seconds(state.handoff_remaining)}` : 'Available';
    elements.status.textContent = message;
    elements.detail.textContent = message;
  }
}

async function refresh() {
  if (statusPending) return;
  statusPending = true;
  try {
    render(await request('status'));
  } catch (error) {
    mine = false;
    unmount();
    elements.lobby.hidden = false;
    elements.status.textContent = 'Gateway unavailable; controls closed';
    showError(error.message);
  } finally {
    statusPending = false;
  }
}

async function action(operation, body = {}) {
  try {
    showError('');
    render(await request(operation, body));
    return true;
  } catch (error) {
    showError(error.message);
    await refresh();
    return false;
  }
}

function activity(event) {
  if (!mine || !event.isTrusted || document.visibilityState !== 'visible' || !document.hasFocus()) return;
  if (event.type === 'pointermove' && !event.buttons) return;
  const now = Date.now();
  if (now - lastActivity < 15000) return;
  lastActivity = now;
  action('activity');
}

function watchActivity(target) {
  for (const name of ['pointerdown', 'pointermove', 'keydown', 'wheel', 'touchstart']) {
    target.addEventListener(name, activity, {passive: true, capture: true});
  }
}

elements.view.addEventListener('load', () => {
  try {
    watchActivity(elements.view.contentWindow.document);
  } catch (error) {
    showError('This view navigated outside the gateway. Use the view links above.');
  }
});
elements.login.addEventListener('submit', async event => {
  event.preventDefault();
  const fields = new FormData(elements.login);
  if (await action('login', Object.fromEntries(fields))) elements.login.reset();
});
elements.acquire.addEventListener('click', () => action('acquire'));
elements.release.addEventListener('click', () => action('release'));
elements.logout.addEventListener('click', () => action('logout'));
elements.keep.addEventListener('click', event => {
  if (event.isTrusted) action('activity');
});
watchActivity(document);
setInterval(refresh, 5000);
refresh();
