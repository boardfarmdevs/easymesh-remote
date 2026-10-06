'use strict';
// One view of the lab inside the lab's page. The page cannot see input in another origin's frame,
// so this tile, on the view's own origin, watches the view and renews the reservation itself:
// genuine clicks, keys, scrolling and touch in a visible, focused window, at most every 15 seconds.

const view = document.getElementById('view');
let lastActivity = 0;

function tell(type) {
  // The lab's page, on one of the lab's origins, listens; the message carries nothing else.
  if (window.parent !== window) window.parent.postMessage({source: 'easymesh-remote-tile', type}, '*');
}

function activity(event) {
  if (!event.isTrusted || document.visibilityState !== 'visible' || !document.hasFocus()) return;
  if (event.type === 'pointermove' && !event.buttons) return;
  const now = Date.now();
  if (now - lastActivity < 15000) return;
  lastActivity = now;
  fetch('/_remote/activity', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: '{}',
    cache: 'no-store'}).then(response => { if (response.ok) tell('activity'); }).catch(() => {});
}

function watchActivity(target) {
  // pointerup too: a first click into a tile fires pointerdown before focus moves into its frame.
  for (const name of ['pointerdown', 'pointerup', 'pointermove', 'keydown', 'wheel', 'touchstart']) {
    target.addEventListener(name, activity, {passive: true, capture: true});
  }
}

view.addEventListener('load', () => {
  try {
    watchActivity(view.contentWindow.document);
    tell('loaded');
  } catch (error) {
    tell('outside');
  }
});
watchActivity(document);
// Loaded only now, so that its load is never missed.
view.src = '/';
