// Viewer tab: open datasets, view them (image / map / globe) with live render adjustments.
// Panels live in viewer-*.js; this file owns the active-dataset state and wires everything together.
import { getJSON } from './api.js';
import { emit, on } from './bus.js';
import { bodyForInfo } from './bodies.js';
import { applyPreset, defaultSettings, getColormaps } from './render.js';
import { createOpen } from './viewer-open.js';
import { createAdjust } from './viewer-adjust.js';
import { createInfo } from './viewer-info.js';
import { createImageView } from './viewer-image.js';
import { createMapView, projectionChoices } from './viewer-map.js';
import { createExport } from './viewer-export.js';
import { onTilesLoading } from './viewer-layer.js';
import { h, select, fmt, download } from './viewer-ui.js';

const KEYS = [
  ['0', 'Fit the picture to the window'], ['1', 'One image pixel per screen pixel'], ['+  −  or the wheel', 'Zoom in / out'],
  ['Drag', 'Pan'], ['Click', 'Values of that pixel (Esc closes them)'],
  ['i', 'Invert'], ['g', 'Black & white'], ['r', 'Reset the adjustments'],
  ['Double-click a slider name', 'Reset that slider'],
  ['h', 'Hide or show the side panels'], ['f', 'Fullscreen'], ['?', 'This list'],
  ['Alt + ←  /  Alt + →', 'Back / forward (every tab)'],
];

const STORE = 'ors.viewer.';
const stored = (k) => { try { return JSON.parse(localStorage.getItem(STORE + k)); } catch { return null; } };
const store = (k, v) => { try { localStorage.setItem(STORE + k, JSON.stringify(v)); } catch { /* quota / private mode */ } };

const rgbDefault = (info) => info.count >= 3 && ['red', 'green', 'blue'].every((c, i) => info.colorinterp?.[i] === c);
const defaultBands = (info) => (rgbDefault(info) ? [1, 2, 3] : [1]);
let webglOk = false; // a successful probe is cached; a failed one is retried (the browser may recover)
const webgl = () => webglOk ||= !!document.createElement('canvas').getContext('webgl2');
const fmtRatio =(n) => Number(n.toPrecision(3)).toString();

export async function init(section) {
  const colormaps = await getColormaps();

  // ---- DOM ----
  const leftEl = h('aside', { class: 'vw-left' });
  const adjustEl = h('div', {});
  const infoEl = h('div', {});
  const imgEl = h('div', { class: 'vw-pane' });
  const geoEl = h('div', { class: 'vw-pane', hidden: true });
  const globeEl = h('div', { class: 'vw-pane', hidden: true });
  const msgEl = h('div', { class: 'vw-msg' }, 'Open a dataset from the left panel, or send one here from Explore / Search.');
  const stage = h('div', { class: 'vw-stage' }, imgEl, geoEl, globeEl, msgEl);
  const viewBtns = Object.fromEntries([['image', 'Image'], ['map', 'Map'], ['globe', 'Globe']].map(([k, label]) =>
    [k, h('button', { class: 'btn small', onclick: () => showView(k) }, label)]));
  const projSel = h('span', {});
  const bmSel = h('span', {});
  const grat = h('input', { type: 'checkbox', onchange: () => maps.setGraticule(grat.checked) });
  const snap = h('input', { type: 'checkbox', onchange: () => { image.setSnap(snap.checked); store('snap', snap.checked); statusBar(); } });
  const tbtn = (label, title, fn) => h('button', { class: 'btn small', title, onclick: fn }, label);
  const geoTools = h('span', { class: 'row nowrap vw-geotools', hidden: true }, projSel, bmSel,
    h('label', { class: 'vw-check' }, grat, 'Graticule'));
  const imgTools = h('label', { class: 'vw-check', title: 'Zoom only in whole power-of-two steps, so every pixel always displays exactly (off: smooth zoom)' }, snap, 'Snap zoom');
  const zoomBtns = [tbtn('Fit', 'Fit to the window (0)', () => ctrl()?.fit()), tbtn('1:1', 'One image pixel per screen pixel (1)', () => ctrl()?.native()),
    tbtn('−', 'Zoom out (−)', () => ctrl()?.zoom(-1)), tbtn('+', 'Zoom in (+)', () => ctrl()?.zoom(1))];
  const snapshotBtn = tbtn('Snapshot', 'Save the current view as a PNG image', savePng);
  for (const b of [...zoomBtns, snapshotBtn]) b.dataset.title = b.title;  // their own tooltips, swapped out in the globe
  const fullscreen = () => (document.fullscreenElement ? document.exitFullscreen() : stage.requestFullscreen());
  const panelsBtn = tbtn('◧ Panels', 'Hide or show the side panels (h)', () => togglePanels());
  const keysDlg = h('dialog', { class: 'dlg vw-keys', 'aria-label': 'Keyboard shortcuts' },
    h('h3', {}, 'Viewer shortcuts'),
    h('table', { class: 'vw-meta' }, KEYS.map(([k, what]) => h('tr', {}, h('th', {}, h('kbd', {}, k)), h('td', {}, what)))),
    h('form', { method: 'dialog', class: 'row end' }, h('button', { class: 'btn' }, 'Close')));
  const toolbar = h('div', { class: 'vw-toolbar' },
    h('span', { class: 'vw-seg' }, Object.values(viewBtns)),
    h('span', { class: 'vw-seg' }, zoomBtns),
    imgTools, geoTools,
    h('span', { class: 'grow' }),
    tbtn('Export…', 'Export the rendered image or the raw data', () => st.status && exporter.open()),
    snapshotBtn, panelsBtn,
    tbtn('⛶', 'Fullscreen (f)', fullscreen),
    tbtn('?', 'Keyboard shortcuts (?)', () => keysDlg.showModal()));
  const sZoom = h('span', { class: 'vw-zoom mono' });
  const sLoading = h('span', { class: 'vw-loading', hidden: true, title: 'Tiles are still loading' }, h('span', { class: 'spinner' }), 'loading');
  const sResample = h('span', {});
  const sStretch = h('span', { class: 'muted' });
  const sCursor = h('span', { class: 'mono' });
  const sValues = h('span', { class: 'mono grow' });
  const statusEl = h('div', { class: 'vw-status' }, sZoom, sLoading, sResample, sStretch, h('span', { class: 'grow' }), sCursor, sValues);
  const vw = h('div', { class: 'vw' }, leftEl,
    h('div', { class: 'vw-center' }, toolbar, stage, statusEl),
    h('aside', { class: 'vw-right' }, adjustEl, infoEl));
  section.replaceChildren(vw, keysDlg);
  function togglePanels() {
    panelsBtn.classList.toggle('primary', vw.classList.toggle('nopanels'));
    setTimeout(() => { ctrl()?.resize(); globe?.resize(); }, 0);
  }
  let loadTimer;  // only say "loading" when it takes a moment, so quick updates do not flicker
  onTilesLoading((busy) => { clearTimeout(loadTimer); if (busy) loadTimer = setTimeout(() => { sLoading.hidden = false; }, 300); else sLoading.hidden = true; });

  // ---- state ----
  const st = { status: null, info: null, body: null, settings: null, view: 'image', proj: null, basemap: undefined,
    stats: new Map(), exact: false, loaded: { image: null, map: null, globe: null } };
  let globe = null, globeBody = null, hoverTimer = null, hoverAbort = null, persistTimer = null, renderFrame = 0, renderTimer = null;

  const getStats = (b) => st.stats.get(b);
  const bandStats = (s = st.settings) => s.bands.map(getStats);
  const statsObj = (bands) => ({ bands: bands.map(getStats) });
  const showing = () => section.getClientRects().length > 0;
  let msgTimer;
  // transient: an error about one action (export, probe...), which goes away by itself; any error goes away on a click
  const msg = (text, isError, transient) => {
    clearTimeout(msgTimer);
    msgEl.hidden = !text; msgEl.textContent = text || ''; msgEl.classList.toggle('error', !!isError);
    msgEl.title = isError ? 'Click to dismiss' : '';
    if (text && transient) msgTimer = setTimeout(() => msg(''), 8000);
  };
  msgEl.addEventListener('click', () => { if (msgEl.classList.contains('error')) msg(''); });

  const image = createImageView(imgEl, colormaps, { onMove: (p) => hover(p), onPick: (p) => pick(p), onView: () => statusBar() });
  const maps = createMapView(geoEl, colormaps, { onMove: (p) => hover(p), onPick: (p) => pick(p), onView: () => statusBar() });
  const ctrl = () => ({ image, map: maps }[st.view]);

  const exporter = createExport(() => ({ status: st.status, settings: st.settings, viewWindow: ctrl()?.pixelWindow?.() ?? null }));
  section.append(exporter.el);
  const adjust = createAdjust(adjustEl, {
    change: (patch, opts) => change(patch, opts),
    reset: () => reset(),
    setExact: (on) => setExact(on),
  });
  const info = createInfo(infoEl, { onClose: () => mark(null) });
  stage.append(info.probeEl);
  const open = createOpen(leftEl, (status) => activate(status));
  adjust.clear();

  // A small crosshair on the probed pixel, in the view where it was picked (one OL overlay per map).
  const markers = {};
  function mark(coord) {
    for (const [v, o] of Object.entries(markers)) o.setPosition(v === st.view ? coord ?? undefined : undefined);
    const map = ctrl()?.map;
    if (!coord || !map || markers[st.view]) return;
    markers[st.view] = new ol.Overlay({ element: h('div', { class: 'vw-marker' }), positioning: 'center-center', stopEvent: false });
    map.addOverlay(markers[st.view]);
    markers[st.view].setPosition(coord);
  }

  // ---- statistics ----
  // Bumped when the dataset or the exact mode changes: a reply requested before (an exact scan takes minutes) is stale.
  let statsGen = 0;
  async function ensureStats(bands) {
    const gen = statsGen;
    const missing = bands.filter((b) => !st.stats.has(b));
    if (!missing.length) return true;
    const r = await getJSON(`/api/datasets/${st.status.id}/stats`, { bands: missing, exact: st.exact ? 1 : 0 });
    if (gen !== statsGen) return false;
    r.bands.forEach((b) => st.stats.set(b.band, b));
    return true;
  }

  // ---- settings ----
  function change(patch, { live } = {}) {
    const needStats = patch.bands || (patch.preset && patch.preset !== 'manual');
    if (!needStats) return commit({ ...st.settings, ...patch }, live);
    return ensureStats(patch.bands ?? st.settings.bands).then((fresh) => {
      if (!fresh) return;
      const s = { ...st.settings, ...patch };
      if (patch.bands && s.preset === 'manual') s.preset = st.info.dtype === 'uint8' ? 'dtype' : 'p05';
      applyPreset(s, statsObj(s.bands), st.info);
      commit(s);
    }).catch((e) => msg(`Statistics failed: ${e.message}`, true, true));
  }

  function commit(s, live) {
    st.settings = s;
    adjust.sync(s, getStats);
    ctrl()?.update(s, bandStats(s));
    if (st.view === 'globe' && globe) { clearTimeout(renderTimer); renderTimer = setTimeout(() => globe.setRender(s), 250); }
    statusBar();
    if (!renderFrame) {
      renderFrame = requestAnimationFrame(() => { renderFrame = 0; emit('render:change', { id: st.status.id, settings: st.settings }); });
    }
    clearTimeout(persistTimer);
    persistTimer = setTimeout(save, live ? 400 : 0);
  }

  const save = () => st.status && store(st.status.id, { settings: st.settings, view: st.view, proj: st.proj, basemap: st.basemap });

  async function reset() {
    const bands = defaultBands(st.info);
    if (!(await ensureStats(bands))) return;
    commit(defaultSettings(st.info, statsObj(bands)));
  }

  async function setExact(on) {
    statsGen++;
    st.exact = on;
    st.stats = new Map();
    msg(on ? 'Scanning the full image for exact statistics…' : '');
    try {
      if (!(await ensureStats(st.settings.bands))) return;
      const s = { ...st.settings };
      if (s.preset !== 'manual') applyPreset(s, statsObj(s.bands), st.info);
      commit(s);
      msg('');
    } catch (e) { msg(`Statistics failed: ${e.message}`, true); }
  }

  // ---- activation ----
  let activation = 0;
  async function activate(status) {
    const mine = ++activation;
    statsGen++;
    info.clearProbe(); mark(null);  // the pixel shown belongs to the dataset being left
    if (!status || status.status !== 'ready' || !status.info) {
      st.status = null;
      st.loaded = { image: null, map: null, globe: null };
      globe?.setDataset(null, st.settings); // a removed dataset must not stay draped on the globe
      adjust.clear(); info.clear(); statusBar();
      msg('Open a dataset from the left panel, or send one here from Explore / Search.');
      return;
    }
    const inf = status.info;
    st.status = status; st.info = inf; st.stats = new Map(); st.exact = false;
    st.loaded = { image: null, map: null, globe: null };
    open.setActive(status.id);
    store('active', status.id);
    info.show(status);
    if (!inf.count) { adjust.clear(); msg('This file has no bands of its own: choose one of its subdatasets in the list on the left.'); return; }
    msg('Reading statistics…');
    try {
      st.body = await bodyForInfo(inf);
      const saved = stored(status.id);
      const ok = saved?.settings?.bands?.every((b) => b >= 1 && b <= inf.count) && saved.settings.min?.length >= saved.settings.bands.length;
      const bands = ok ? saved.settings.bands : defaultBands(inf);
      await ensureStats(bands);
      if (mine !== activation) return;
      st.settings = ok ? { ...defaultSettings(inf, statsObj(bands)), ...saved.settings } : defaultSettings(inf, statsObj(bands));
      st.proj = saved?.proj; st.basemap = saved?.basemap;
      msg('');
      await adjust.show(status, st.settings, getStats, false);
      if (mine !== activation) return;
      const georef = inf.georef !== 'none';
      viewBtns.map.disabled = viewBtns.globe.disabled = !georef;
      viewBtns.map.title = viewBtns.globe.title = georef ? '' : 'This dataset has no georeferencing';
      const choices = projectionChoices(inf, st.body);
      if (!choices.some((c) => c.id === st.proj)) st.proj = choices[0].id;
      projSel.replaceChildren(select(choices.map((c) => [c.id, c.label]), st.proj, (v) => {
        st.proj = v; st.loaded.map = null; save();
        info.clearProbe(); mark(null);  // the crosshair's coordinates belong to the old projection
        showView('map');
      }));
      emit('viewer:active', { id: status.id, status });
      emit('render:change', { id: status.id, settings: st.settings });
      info.clearProbe();
      mark(null);
      await showView(saved?.view && (georef || saved.view === 'image') ? saved.view : 'image');
    } catch (e) {
      if (mine === activation) msg(`Could not prepare the dataset: ${e.message}`, true);
    }
  }

  // ---- views ----
  async function showView(name) {
    if (!st.status?.info.count || !st.settings) return;
    st.view = name;
    imgEl.hidden = name !== 'image'; geoEl.hidden = name !== 'map'; globeEl.hidden = name !== 'globe';
    for (const [k, b] of Object.entries(viewBtns)) b.classList.toggle('primary', k === name);
    imgTools.hidden = name !== 'image'; geoTools.hidden = name !== 'map';
    for (const b of [...zoomBtns, snapshotBtn]) {  // the globe has its own camera controls (drag, wheel)
      b.disabled = name === 'globe';
      b.title = name === 'globe' ? 'Not in the globe view: drag and use the wheel there' : b.dataset.title;
    }
    save();
    if (!showing()) return; // loaded on tab:shown, when the containers have a size
    const status = st.status, s = st.settings, id = status.id;
    if (name !== 'globe' && !webgl()) return msg('WebGL is not available in this browser (graphics acceleration is off or its context limit is used up). Close other WebGL tabs or restart the browser.', true);
    if (name === 'image') {
      if (st.loaded.image !== id) { image.setSnap(snap.checked); image.load(status, s, bandStats()); st.loaded.image = id; } else image.update(s, bandStats());
      msg('');
    } else if (name === 'map') {
      const key = `${id}|${st.proj}`;
      if (st.loaded.map !== key) {
        const ctx = maps.load(status, st.body, st.proj, s, bandStats());
        st.loaded.map = key;
        msg(ctx.empty ? 'The dataset has no footprint in this projection. Pick another projection.' : '');
        const choices = maps.basemapChoices(st.body);
        if (!choices.some((c) => c.id === st.basemap) && st.basemap !== null) st.basemap = choices[0]?.id ?? null;
        maps.setBasemap(st.basemap);
        bmSel.replaceChildren(select([['', 'No basemap'], ...choices.map((c) => [c.id, c.name])], st.basemap ?? '',
          (v) => { st.basemap = v || null; maps.setBasemap(st.basemap); save(); }, { title: `Basemap (${st.body?.name ?? 'body'})` }));
      } else maps.update(s, bandStats());
    } else {
      msg('Loading globe…');
      try {
        if (!globe) {
          const { createGlobe } = await import('./globe.js');
          globe = await createGlobe(globeEl, { body: st.body });
          globeBody = st.body?.id;
        } else if (globeBody !== st.body?.id) { globe.setBody(st.body); globeBody = st.body?.id; }
        if (st.loaded.globe !== id) { globe.setDataset(status, s); globe.flyToDataset(); st.loaded.globe = id; } else globe.setRender(s);
        globe.resize();
        msg('');
      } catch (e) {
        msg(`The globe is unavailable: ${e.message}. Image and Map views still work.`, true);
        showView('image');
        return;
      }
    }
    ctrl()?.resize();
    statusBar();
  }

  async function savePng() {
    const name = `${st.status.name.replace(/\.[^.]+$/, '')}-view.png`;
    if (!ctrl()) return;
    try { download(await ctrl().png(), name); } catch (e) {
      // Basemap tiles are cross-origin images, which taint the canvas: retry with the dataset alone.
      if (st.view !== 'map' || !st.basemap) return msg(`Could not export the view: ${e.message}`, true, true);
      maps.setBasemap(null);
      try { download(await maps.png(), name); msg('Exported without the basemap (the tile server does not allow exporting its images).'); setTimeout(() => msg(''), 4000); }
      catch (e2) { msg(`Could not export the view: ${e2.message}`, true, true); }
      maps.setBasemap(st.basemap);
    }
  }

  // ---- status bar ----
  function statusBar() {
    const s = st.settings;
    if (!s || !ctrl() || st.view === 'globe') {
      sZoom.textContent = ''; sResample.textContent = st.view === 'globe' && s ? 'server-rendered tiles (PNG)' : '';
      sStretch.textContent = s ? stretchText(s) : '';
      return;
    }
    const r = ctrl().ratio();
    sZoom.textContent = Math.abs(r - 1) < 0.005 ? '1:1' : r > 1 ? `1:${fmtRatio(r)}` : `${fmtRatio(1 / r)}:1`;
    sResample.textContent = resampleText(r, s);
    sStretch.textContent = stretchText(s);
  }

  function resampleText(r, s) {
    const warped = st.view === 'map' ? ' · warped (server)' : '';
    const exact = Math.abs(Math.log2(r) % 1) < 0.01 || Math.abs(Math.log2(r) % 1 - 1) < 0.01;
    if (Math.abs(r - 1) < 0.005) return `native pixels, exact${warped}`;
    if (r > 1) return `${s.resampling} downsample${s.resampling === 'nearest' ? ' — decimated' : ''}${warped}${exact || st.view === 'map' ? '' : ' · GPU-scaled between levels'}`;
    const over = st.view === 'image' ? 2 ** (st.info.tiles?.overzoom ?? 0) : Infinity;
    if (s.magnify === 'nearest') return `${s.interpolate ? 'nearest + GPU smoothing' : 'nearest — true pixels'}${warped}`;
    return `${s.magnify} (server)${1 / r > over ? ` + ${s.interpolate ? 'smooth' : 'nearest'} beyond ${over}:1` : ''}${warped}`;
  }

  function stretchText(s) {
    const range = s.stretch === 'equalize' ? 'equalized' : s.bands.map((_, i) => `${fmt(s.min[i], 5)}–${fmt(s.max[i], 5)}`).join(' | ');
    return `${s.stretch} ${range}${s.preset !== 'manual' ? ` (${s.preset})` : ''}${s.gamma !== 1 ? ` · γ ${fmt(s.gamma, 3)}` : ''}`;
  }

  // ---- cursor + probe ----
  function pixelQuery(p) {
    if (st.view === 'image') return p.inside ? { col: p.col, row: p.row } : null;
    return p && { x: p.lon, y: p.lat, crs: 'lonlat' };
  }

  function hover(p) {
    clearTimeout(hoverTimer);
    hoverAbort?.abort();
    if (!p) { sCursor.textContent = sValues.textContent = ''; return; }
    sCursor.textContent = st.view === 'image' ? `col ${p.col}  row ${p.row}` : `lon ${p.lon.toFixed(5)}  lat ${p.lat.toFixed(5)}`;
    const q = pixelQuery(p);
    if (!q) { sValues.textContent = 'outside'; return; }
    hoverTimer = setTimeout(async () => {
      hoverAbort = new AbortController();
      try {
        const r = await getJSON(`/api/datasets/${st.status.id}/pixel`, q, { signal: hoverAbort.signal });
        if (!r.inside) { sValues.textContent = 'outside'; return; }
        const lonlat = r.lonlat && st.view === 'image' ? `  lon ${r.lonlat[0].toFixed(5)}  lat ${r.lonlat[1].toFixed(5)}` : '';
        const cols = st.view === 'map' ? `  col ${r.col}  row ${r.row}` : '';
        sValues.textContent = `${lonlat}${cols}  ${valuesText(r)}`;
      } catch (e) { if (e.name !== 'AbortError') sValues.textContent = ''; }
    }, 80);
  }

  function valuesText(r) {
    const bands = st.settings.bands.slice(0, 3);
    const scaled = st.info.scales?.some((x) => x !== 1) || st.info.offsets?.some((x) => x !== 0);
    return bands.map((b) => {
      if (r.nodata[b - 1]) return `b${b}: no data`;
      return `b${b}: ${fmt(r.values[b - 1], 8)}${scaled ? ` (${fmt(r.scaled[b - 1], 8)})` : ''}`;
    }).join('  ');
  }

  async function pick(p) {
    const q = p && pixelQuery(p);
    if (!q) return;
    const id = st.status.id, view = st.view, mine = activation;
    try {
      const r = await getJSON(`/api/datasets/${id}/pixel`, q);
      if (mine !== activation || st.status?.id !== id || st.view !== view) return;  // another dataset or view by now
      info.showProbe(r, st.info);
      mark(p.coord);
    } catch (e) { msg(`Probe failed: ${e.message}`, true, true); }
  }

  // ---- events ----
  on('viewer:open', async (d) => {
    emit('nav', { tab: 'viewer' });
    const { url, path, name, companions, meta } = d;
    await open.open({ url, path, name, companions, meta });
  });
  on('tab:shown', ({ tab }) => {
    if (tab !== 'viewer') return;
    if (st.status && st.settings && !st.loaded[st.view]) showView(st.view);
    else { ctrl()?.resize(); globe?.resize(); }
  });
  document.addEventListener('fullscreenchange', () => { ctrl()?.resize(); globe?.resize(); });
  // Shortcuts work unless you are typing: a slider, checkbox or button with the focus does not block them.
  const typing = (t) => t.isContentEditable || /^(TEXTAREA|SELECT)$/.test(t.tagName)
    || (t.tagName === 'INPUT' && !['range', 'checkbox', 'radio', 'button'].includes(t.type));
  document.addEventListener('keydown', (e) => {
    if (!showing() || e.ctrlKey || e.metaKey || e.altKey || typing(e.target) || document.querySelector('dialog[open]')) return;
    const c = st.view === 'globe' ? null : ctrl();
    const act = {
      '?': () => keysDlg.showModal(), h: togglePanels, f: fullscreen,
      ...(st.settings && {
        '+': () => c?.zoom(1), '=': () => c?.zoom(1), '-': () => c?.zoom(-1), 0: () => c?.fit(), 1: () => c?.native(),
        i: () => change({ invert: !st.settings.invert }), g: () => change({ grayscale: !st.settings.grayscale }), r: reset,
        Escape: () => { info.clearProbe(); mark(null); },
      }),
    }[e.key];
    if (act) { e.preventDefault(); act(); }
  });

  section.viewerDebug = { st, image, maps, change, showView, get globe() { return globe; } }; // inspection hook for tests
  snap.checked = stored('snap') ?? false;  // smooth wheel zoom unless the user asked for exact power-of-two steps
  // Resume the last dataset after a reload.
  open.loaded.then(() => {
    const last = stored('active'), d = last && open.get(last);
    if (d?.status === 'ready') open.select(last);
  });
}
