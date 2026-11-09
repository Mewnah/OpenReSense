// App shell: tab routing (#explore / #search / #viewer), module init, settings dialog.
import { emit, on } from './bus.js';
import { getJSON, putJSON } from './api.js';

const TABS = ['explore', 'search', 'viewer'];
const LAST_TAB = 'ors.lastTab';  // the app (window) reopens on the tab you used last

function show(tab) {
  if (!TABS.includes(tab)) tab = 'explore';
  for (const t of TABS) {
    document.getElementById(`tab-${t}`).hidden = t !== tab;
    document.querySelector(`[data-tab="${t}"]`).classList.toggle('active', t === tab);
  }
  try { localStorage.setItem(LAST_TAB, tab); } catch { /* storage off */ }
  emit('tab:shown', { tab });
}

// Back / Forward in the top bar (the app window has no browser buttons; Alt+←/→ and the mouse buttons work too).
// Tabs, Explore targets and Search results are history entries. The Navigation API, where present, tells whether
// there is somewhere to go (and only counts this app's own entries); without it the buttons stay enabled.
const backBtn = document.getElementById('nav-back'), fwdBtn = document.getElementById('nav-fwd');
backBtn.addEventListener('click', () => history.back());
fwdBtn.addEventListener('click', () => history.forward());
if (window.navigation) {
  const sync = () => { backBtn.disabled = !navigation.canGoBack; fwdBtn.disabled = !navigation.canGoForward; };
  navigation.addEventListener('currententrychange', sync);
  sync();
}

on('nav', ({ tab }) => {
  if (location.hash !== `#${tab}`) location.hash = tab; // hashchange calls show()
  else show(tab);
});
window.addEventListener('hashchange', () => show(location.hash.slice(1)));

// Init every module up front (hidden tabs included) so cross-tab events like viewer:open are never lost.
// A failing module must not break the others.
for (const name of TABS) {
  try {
    const mod = await import(`./${name}.js`);
    await mod.init(document.getElementById(`tab-${name}`));
  } catch (err) {
    console.error(`[${name}] init failed`, err);
    document.getElementById(`tab-${name}`).innerHTML = `<p class="error pad">Failed to load ${name}: ${err.message}</p>`;
  }
}
let first = location.hash.slice(1);
if (!first) {
  try { first = localStorage.getItem(LAST_TAB); } catch { /* storage off */ }
  first = TABS.includes(first) ? first : 'explore';
  history.replaceState(history.state, '', `#${first}`);
}
show(first);

// Settings dialog
const dlg = document.getElementById('settings-dlg');
document.getElementById('settings-btn').addEventListener('click', async () => {
  const s = await getJSON('/api/settings').catch(() => ({}));
  dlg.querySelector('[name=earthdata_token]').value = s.earthdata_token || '';
  dlg.showModal();
});
dlg.addEventListener('close', async () => {
  if (dlg.returnValue !== 'save') return;
  await putJSON('/api/settings', { earthdata_token: dlg.querySelector('[name=earthdata_token]').value.trim() });
});
