// App shell: tab routing (#explore / #search / #viewer), module init, settings dialog.
import { emit, on } from './bus.js';
import { getJSON, putJSON } from './api.js';

const TABS = ['explore', 'search', 'viewer'];

function show(tab) {
  if (!TABS.includes(tab)) tab = 'explore';
  for (const t of TABS) {
    document.getElementById(`tab-${t}`).hidden = t !== tab;
    document.querySelector(`[data-tab="${t}"]`).classList.toggle('active', t === tab);
  }
  emit('tab:shown', { tab });
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
show(location.hash.slice(1) || 'explore');

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
