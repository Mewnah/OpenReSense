// Left panel: open a dataset (URL / local path / upload), dataset list with live progress, subdataset picker.
import { getJSON, postJSON, delJSON } from './api.js';
import { h } from './viewer-ui.js';

const POLL_MS = 700;
const busy = (d) => d.status !== 'ready' && d.status !== 'error';

// onActive(status | null) fires when the user (or a finished open) selects a dataset; viewer.js decides what to show.
export function createOpen(host, onActive) {
  const datasets = new Map(); // id -> DatasetStatus
  const wantSelect = new Set(); // ids to select when they become ready
  let activeId = null, timer = null;

  const url = h('input', { class: 'input grow', placeholder: 'https://…/image.tif, .jp2, .IMG, .LBL, .xml', onkeydown: (e) => e.key === 'Enter' && openUrl() });
  const path = h('input', { class: 'input grow', placeholder: 'C:\\data\\scene.tif', onkeydown: (e) => e.key === 'Enter' && openPath() });
  const error = h('div', { class: 'error vw-small', hidden: true });
  const list = h('div', { class: 'vw-list' });
  const subs = h('div', { class: 'vw-subs', hidden: true });
  const file = h('input', { type: 'file', multiple: true, hidden: true, onchange: () => upload([...file.files]) });
  const drop = h('div', { class: 'vw-drop', tabIndex: 0, onclick: () => file.click() },
    'Drop files here, or click to choose', h('div', { class: 'muted vw-small' }, 'A PDS3 .LBL + .IMG pair? Drop both.'));
  const fail = (e) => { error.hidden = !e; error.textContent = e ? e.message || String(e) : ''; };

  async function open(body, select = true) {
    fail(null);
    try {
      const st = await postJSON('/api/datasets/open', body);
      track(st, select);
      return st;
    } catch (e) { fail(e); }
  }
  const openUrl = () => url.value.trim() && open({ url: url.value.trim() }).then(() => (url.value = ''));
  const openPath = () => path.value.trim() && open({ path: path.value.trim() }).then(() => (path.value = ''));

  async function upload(files) {
    if (!files.length) return;
    fail(null);
    const form = new FormData();
    files.forEach((f) => form.append('files', f));
    try {
      const st = await fetch('/api/datasets/upload', { method: 'POST', body: form }).then(async (r) => {
        if (!r.ok) throw new Error(`${r.status} ${(await r.json().catch(() => ({}))).detail ?? r.statusText}`);
        return r.json();
      });
      track(st, true);
    } catch (e) { fail(e); }
  }
  drop.addEventListener('dragover', (e) => { e.preventDefault(); drop.classList.add('over'); });
  drop.addEventListener('dragleave', () => drop.classList.remove('over'));
  drop.addEventListener('drop', (e) => { e.preventDefault(); drop.classList.remove('over'); upload([...e.dataTransfer.files]); });

  function track(st, select) {
    const older = [...datasets].filter(([id]) => id !== st.id);
    datasets.clear();
    for (const [id, d] of [[st.id, st], ...older]) datasets.set(id, d); // newest first
    if (select) wantSelect.add(st.id);
    settle(st);
    render();
    poll();
  }

  // A dataset reached a stable state: hand it to the viewer if the user is waiting for it.
  function settle(st) {
    if (st.status === 'ready' && wantSelect.delete(st.id)) select(st.id);
    if (st.status === 'error') wantSelect.delete(st.id);
  }

  function poll() {
    if (timer || ![...datasets.values()].some((d) => busy(d) || d.optimizing)) return;
    timer = setInterval(async () => {
      const live = [...datasets.values()].filter((d) => busy(d) || d.optimizing);
      if (!live.length) { clearInterval(timer); timer = null; return; }
      await Promise.all(live.map((d) => getJSON(`/api/datasets/${d.id}`).then((st) => {
        datasets.set(st.id, st);
        settle(st);
      }).catch((e) => { if (/^404/.test(e.message)) datasets.delete(d.id); })));
      render();
    }, POLL_MS);
  }

  async function remove(id, purge) {
    try { await delJSON(`/api/datasets/${id}?purge=${purge ? 1 : 0}`); } catch (e) { return fail(e); }
    datasets.delete(id);
    if (id === activeId) { activeId = null; onActive(null); }
    render();
  }

  function render() {
    list.replaceChildren(...(datasets.size ? [...datasets.values()].map((d) => {
      const badge = d.status === 'ready' ? (d.optimizing ? ['warn', 'optimizing'] : ['ok', 'ready']) : d.status === 'error' ? ['danger', 'error'] : ['warn', d.status];
      return h('div', { class: `vw-item${d.id === activeId ? ' active' : ''}${d.status === 'ready' ? ' ready' : ''}`, title: d.name,
        onclick: () => d.status === 'ready' && select(d.id) },
      h('div', { class: 'row vw-item-top' }, h('span', { class: 'grow vw-name' }, d.name),
        h('span', { class: `badge ${badge[0]}` }, badge[1]),
        h('button', { class: 'btn ghost small', title: 'Remove from list (keeps cached copies)', onclick: (e) => { e.stopPropagation(); remove(d.id); } }, '✕'),
        h('button', { class: 'btn ghost small', title: 'Remove and delete its cached downloads / conversions (your own files are never deleted)', onclick: (e) => { e.stopPropagation(); remove(d.id, true); } }, '🗑')),
      d.status === 'error' ? h('div', { class: 'error vw-small' }, d.message)
        : d.optimizing ? h('div', { class: 'muted vw-small' }, 'Building a fast copy in the background; viewing works meanwhile.') // progress is 1.0 here, not meaningful
          : busy(d) ? h('div', {}, h('div', { class: 'progress' }, h('i', { style: `width:${Math.round((d.progress ?? 0) * 100)}%` })),
            h('div', { class: 'muted vw-small' }, d.message || '')) : null);
    }) : [h('p', { class: 'muted vw-small' }, 'No datasets yet.')]));
    const a = datasets.get(activeId);
    subs.hidden = !a?.info?.subdatasets?.length;
    if (!subs.hidden) {
      const sel = h('select', { class: 'select grow' }, a.info.subdatasets.map((s) => h('option', { value: s.name }, s.description || s.name)));
      subs.replaceChildren(h('div', { class: 'muted vw-small' }, `${a.info.subdatasets.length} subdatasets`),
        h('div', { class: 'row' }, sel, h('button', { class: 'btn small primary', onclick: () => open({ path: sel.value, name: `${a.name} · ${sel.selectedOptions[0].textContent}` }) }, 'Open')));
    }
  }

  function select(id) {
    activeId = id;
    render();
    onActive(datasets.get(id));
  }

  // Server-backed file browser: pick a file, its path goes into the path field.
  const dlg = h('dialog', { class: 'dlg vw-fs' });
  async function browse(p = '') {
    const r = await getJSON('/api/fs', { path: p }).catch((e) => ({ error: e.message }));
    if (r.error) {
      dlg.replaceChildren(h('p', { class: 'error' }, r.error),
        h('div', { class: 'row end' }, h('button', { class: 'btn', onclick: () => browse('') }, 'Drives'), h('button', { class: 'btn', onclick: () => dlg.close() }, 'Close')));
      if (!dlg.open) dlg.showModal();
      return;
    }
    const pick = (e) => { path.value = e.path; dlg.close(); openPath(); };
    dlg.replaceChildren(
      h('h3', {}, 'Open a file'),
      h('div', { class: 'row' },
        h('button', { class: 'btn small', disabled: !r.parent && r.path === '', onclick: () => browse(r.parent ?? '') }, '↑ Up'),
        h('select', { class: 'select', onchange: (e) => browse(e.target.value) }, h('option', { value: '' }, 'Drives…'),
          r.drives.map((d) => h('option', { value: d }, d))),
        h('code', { class: 'grow vw-path' }, r.path || 'This PC')),
      h('div', { class: 'vw-fs-list' }, r.entries.map((e) => h('div', {
        class: `vw-fs-row${e.is_dir ? ' dir' : ''}${e.openable ? ' openable' : ''}`,
        onclick: () => (e.is_dir ? browse(e.path) : e.openable ? pick(e) : null),
      }, h('span', {}, e.is_dir ? '📁 ' : '🛰 ', e.name), h('span', { class: 'muted vw-small' }, e.size != null && !e.is_dir ? `${(e.size / 1048576).toFixed(1)} MB` : '')))),
      h('div', { class: 'row end' }, h('span', { class: 'muted vw-small grow' }, 'Click a data file to open it'), h('button', { class: 'btn', onclick: () => dlg.close() }, 'Close')));
    if (!dlg.open) dlg.showModal();
  }

  host.replaceChildren(
    h('section', { class: 'vw-sec' }, h('h4', {}, 'Open'),
      h('div', { class: 'row nowrap' }, url, h('button', { class: 'btn primary small', onclick: openUrl }, 'Open URL')),
      h('div', { class: 'row nowrap' }, path, h('button', { class: 'btn small', onclick: () => browse(path.value.trim().replace(/[^\\/]*$/, '') || '') }, 'Browse…'),
        h('button', { class: 'btn small', onclick: openPath }, 'Open')),
      drop, file, error),
    h('section', { class: 'vw-sec' }, h('h4', {}, 'Datasets'), list, subs),
    dlg);

  render();
  // Server lists newest first.
  const loaded = getJSON('/api/datasets').then((all) => { all.forEach((d) => datasets.set(d.id, d)); render(); poll(); }).catch((e) => console.error('[viewer] dataset list', e));

  return {
    loaded,
    open,
    get(id) { return datasets.get(id); },
    // Make a dataset the active one without notifying (viewer.js already knows).
    setActive(id) { activeId = id; render(); },
    update(st) { datasets.set(st.id, st); render(); },
    select,
  };
}
