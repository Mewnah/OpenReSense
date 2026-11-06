// Search tab: federated live search across connectors, grouped results, footprint map.
import { getJSON } from './api.js';
import { on } from './bus.js';
import { getBodies } from './bodies.js';
import { h, safeUrl } from './explore-util.js';
import { makeCard } from './search-card.js';
import { createMap } from './search-map.js';

const PAGE = 25;
const parseBox = (txt) => {
  const v = txt.split(/[\s,;]+/).filter(Boolean).map(Number);
  if (v.length !== 4 || v.some((x) => !Number.isFinite(x))) return null;
  const [w, s, e, n] = v;
  return Math.abs(w) <= 180 && Math.abs(e) <= 180 && s >= -90 && n <= 90 && s <= n ? v : null;
};

export async function init(root) {
  const { bodies, regions } = await getBodies();
  const bodyById = new Map(bodies.map((b) => [b.id, b]));

  // ---- form
  const target = h('select', { class: 'select', id: 'se-target' }, h('option', { value: '' }, 'Any target'));
  const groups = new Map();
  for (const b of bodies) (groups.get(b.group) ?? groups.set(b.group, h('optgroup', { label: b.group })).get(b.group)).append(h('option', { value: b.id }, b.name));
  target.append(...groups.values());
  const region = h('select', { class: 'select', id: 'se-region' }, h('option', { value: '' }, 'Any region'),
    Object.entries(regions).map(([id, r]) => h('option', { value: id }, r.name)));
  const regionLabel = h('label', { hidden: true }, 'Region', region);
  const text = h('input', { class: 'input', id: 'se-text', placeholder: 'keywords, product id, instrument…', autocomplete: 'off' });
  const bbox = h('input', { class: 'input mono', id: 'se-bbox', placeholder: 'W,S,E,N  e.g. -75,-80,-60,-70', autocomplete: 'off' });
  const useRegion = h('button', { type: 'button', class: 'btn small', disabled: true, title: 'Fill the box from the selected region' }, 'Use region');
  const drawBtn = h('button', { type: 'button', class: 'btn small', title: 'Drag a rectangle on the map' }, 'Draw on map');
  const clearBox = h('button', { type: 'button', class: 'btn small ghost' }, 'Clear');
  const start = h('input', { class: 'input', type: 'date', id: 'se-start' });
  const end = h('input', { class: 'input', type: 'date', id: 'se-end' });
  const levelRadio = (v, lbl) => h('label', null, h('input', { type: 'radio', name: 'se-level', value: v, checked: v === 'collection' }), lbl);
  const levelBox = h('div', { class: 'se-seg', role: 'radiogroup', 'aria-label': 'Result level' }, levelRadio('collection', 'Datasets'), levelRadio('product', 'Products'));
  const collChip = h('div', { class: 'se-chipline', hidden: true });
  const connBox = h('div', { class: 'se-conns' });
  const go = h('button', { class: 'btn primary', type: 'submit' }, 'Search');
  const mapBtn = h('button', { type: 'button', class: 'btn', 'aria-pressed': 'true' }, 'Map');
  const form = h('form', { class: 'se-form', 'aria-label': 'Search' },
    h('label', null, 'Target', target), regionLabel,
    h('label', null, 'Text', text),
    h('div', { class: 'col', style: 'gap:4px' }, h('label', { for: 'se-bbox' }, 'Bounding box (lon/lat)'), bbox, h('div', { class: 'row', style: 'gap:6px' }, useRegion, drawBtn, clearBox)),
    h('div', { class: 'se-two' }, h('label', null, 'From', start), h('label', null, 'To', end)),
    h('fieldset', null, h('legend', { class: 'muted' }, 'Results are'), levelBox),
    collChip,
    h('fieldset', null, h('legend', { class: 'muted' }, 'Archives'), connBox),
    h('div', { class: 'row' }, go, mapBtn));
  const resultsEl = h('div', { class: 'se-results', 'aria-live': 'polite' });
  const mapEl = h('div', { class: 'se-map' });
  const mapWrap = h('div', { class: 'se-mapwrap' }, mapEl);
  const grid = h('div', { class: 'se-root' }, form, resultsEl, mapWrap);
  root.replaceChildren(grid);

  // ---- state
  const level = () => form.elements['se-level'].value;
  const setLevel = (v) => { form.elements['se-level'].value = v; renderConns(); };
  let coll = null;                 // {connector, id, label}: product search restricted to one collection
  let connList = [], checked = new Set(), seen = new Set();
  let sections = [], cards = new Map(), runCtl, connSeq = 0, selectedKey = null;

  // ---- map (optional: search must work without it)
  let map = null;
  const narrow = matchMedia('(max-width: 1100px)');
  const hooks = {
    hover: (k) => { map?.highlight(k); },
    select: (k, fit) => { markSelected(k); map?.select(k, fit); },
    browse: (r) => browseProducts(r),
  };
  try {
    map = createMap(mapEl, {
      onHover: (k) => { for (const [key, el] of cards) el.classList.toggle('hover', key === k); },
      onSelect: (k) => { markSelected(k); if (k) cards.get(k)?.scrollIntoView({ block: 'nearest', behavior: 'smooth' }); },
      onBox: (b) => { bbox.value = b.join(', '); drawBtn.classList.remove('primary'); syncBox(); },
    });
  } catch (err) {
    console.error('[search] map unavailable', err);
    grid.classList.add('nomap');
    mapBtn.hidden = drawBtn.hidden = true;
  }
  function markSelected(k) {
    selectedKey = k;
    for (const [key, el] of cards) el.classList.toggle('sel', key === k);
  }
  function syncBox() { map?.setBox(parseBox(bbox.value)); }

  mapBtn.onclick = () => {
    const cls = narrow.matches ? 'showmap' : 'nomap';
    const on_ = grid.classList.toggle(cls);
    mapBtn.setAttribute('aria-pressed', String(cls === 'showmap' ? on_ : !on_));
    setTimeout(() => map?.resize(), 0);
  };
  mapBtn.setAttribute('aria-pressed', String(!narrow.matches));
  drawBtn.onclick = () => {
    if (narrow.matches && !grid.classList.contains('showmap')) mapBtn.click();
    const active = !drawBtn.classList.contains('primary');
    drawBtn.classList.toggle('primary', active);
    map.drawBox(active);
  };

  // ---- form behaviour
  bbox.addEventListener('input', syncBox);
  clearBox.onclick = () => { bbox.value = ''; syncBox(); };
  region.addEventListener('change', () => { useRegion.disabled = !region.value; });
  useRegion.onclick = () => { bbox.value = regions[region.value].bbox.join(', '); syncBox(); };
  levelBox.addEventListener('change', renderConns);
  target.addEventListener('change', onTarget);

  async function onTarget() {
    const earth = target.value === 'earth';
    regionLabel.hidden = !earth;
    if (!earth) { region.value = ''; useRegion.disabled = true; }
    map?.setBody(bodyById.get(target.value));
    await loadConns();
  }

  function setColl(c) {
    coll = c;
    collChip.hidden = !c;
    collChip.replaceChildren(...(c ? [h('span', { class: 'grow' }, `Collection: ${c.label}`),
      h('button', { type: 'button', class: 'btn small ghost', 'aria-label': 'Remove collection restriction', onclick: () => setColl(null) }, '✕')] : []));
  }

  async function loadConns() {
    const my = ++connSeq;
    let list = [];
    try { list = await getJSON('/api/connectors', { target: target.value }); } catch (err) { connBox.replaceChildren(h('span', { class: 'error' }, `Could not load archives: ${err.message}`)); return; }
    if (my !== connSeq) return;
    connList = list;
    for (const c of list) if (!seen.has(c.id)) { seen.add(c.id); checked.add(c.id); }
    renderConns();
  }

  function renderConns() {
    if (!connList.length) { connBox.replaceChildren(h('span', { class: 'muted' }, 'No live-search connectors for this target.')); return; }
    connBox.replaceChildren(...connList.map((c) => {
      const ok = c.levels.includes(level());
      return h('label', { class: `se-conn${ok ? '' : ' off'}`, title: c.description },
        h('input', { type: 'checkbox', checked: checked.has(c.id), disabled: !ok,
          onchange: (e) => (e.target.checked ? checked.add(c.id) : checked.delete(c.id)) }),
        h('span', null, c.name, ok ? null : h('small', null, `no ${level() === 'collection' ? 'dataset' : 'product'} search`)));
    }));
  }

  form.addEventListener('submit', (e) => { e.preventDefault(); run(); });

  // ---- running searches
  function hint(msg, cls = 'muted') { resultsEl.replaceChildren(h('div', { class: `se-hint ${cls}` }, msg)); }

  function queryFor(c) {
    const box = bbox.value.trim() ? parseBox(bbox.value) : null;
    const collection = coll && coll.connector === c.id && level() === 'product' ? coll.id : '';
    // product search without a collection would be a 400: list the collections to pick from instead
    const pick = level() === 'product' && c.needs_collection && !collection;
    const q = {
      connector: c.id, level: pick ? 'collection' : level(), text: text.value.trim(), target: target.value, region: region.value,
      bbox: box, start: start.value && `${start.value}T00:00:00Z`, end: end.value && `${end.value}T23:59:59Z`,
      collection, limit: PAGE,
    };
    const used = { text: q.text, bbox: box, time: q.start || q.end };
    const ignored = Object.keys(used).filter((k) => used[k] && !c.supports.includes(k));
    return { q, ignored, pick };
  }

  function run() {
    if (bbox.value.trim() && !parseBox(bbox.value)) { hint('Bounding box must be four numbers W,S,E,N (lon -180..180, lat -90..90, S ≤ N).', 'error'); bbox.focus(); return; }
    runCtl?.abort();
    const ctl = (runCtl = new AbortController());
    map?.clear(); cards = new Map(); selectedKey = null;
    const conns = connList.filter((c) => c.levels.includes(level()) && (coll && level() === 'product' ? c.id === coll.connector : checked.has(c.id)));
    if (!conns.length) { hint(connList.length ? 'Tick at least one archive that supports this result level.' : 'No live-search connectors are available for this target.'); return; }
    resultsEl.replaceChildren();
    sections = conns.map((c) => makeSection(c, ctl.signal));
    resultsEl.append(...sections.map((s) => s.el));
    sections.forEach((s) => fetchMore(s));
  }

  function makeSection(c, signal) {
    const { q, ignored, pick } = queryFor(c);
    const status = h('span', { class: 'muted' });
    const notes = h('div', { class: 'muted' });
    const list = h('div', { class: 'se-list' });
    const more = h('button', { class: 'btn', hidden: true }, 'Load more');
    const el = h('section', { class: 'se-sec', 'aria-label': c.name },
      h('div', { class: 'se-sec-head' }, h('h2', null, h('a', { href: safeUrl(c.url), target: '_blank', rel: 'noopener noreferrer', title: c.description }, c.name)), status,
        ignored.length ? h('span', { class: 'muted', title: 'This archive does not support these query fields' }, `(ignores ${ignored.join(', ')})`) : null),
      pick ? h('div', { class: 'muted' }, 'Pick a dataset to list its products.') : null,
      notes, list, h('div', { class: 'row', style: 'justify-content:center;margin-top:8px' }, more));
    const sec = { c, q, signal, el, status, notes, list, more, shown: 0, next: 0, total: null, busy: false };
    more.onclick = () => fetchMore(sec);
    return sec;
  }

  async function fetchMore(sec) {
    if (sec.busy) return;
    sec.busy = true;
    sec.more.hidden = true;
    sec.status.replaceChildren(h('span', { class: 'spinner' }), ' searching…');
    try {
      const page = await getJSON('/api/search', { ...sec.q, offset: sec.next }, { signal: sec.signal });
      const tn = (r) => bodyById.get(r.target)?.name ?? r.target;
      for (const r of page.results) {
        const key = `${r.connector}:${r.id}`;
        if (cards.has(key)) continue;
        const el = makeCard(r, key, hooks, tn(r));
        cards.set(key, el);
        sec.list.append(el);
      }
      map?.add(page.results);
      if (!sec.shown) map?.fit();
      sec.shown += page.results.length;
      sec.total = page.total;
      sec.next = page.next_offset;
      sec.status.textContent = page.total != null ? `${sec.shown.toLocaleString()} of ${page.total.toLocaleString()}` : `${sec.shown} shown`;
      sec.notes.replaceChildren(...page.notes.map((n) => h('div', null, n)));
      if (!sec.shown) sec.list.replaceChildren(h('div', { class: 'muted', style: 'padding:8px 0' }, 'No results. Try fewer constraints (text, box, dates).'));
      sec.more.hidden = page.next_offset == null;
      if (selectedKey) markSelected(selectedKey);
    } catch (err) {
      if (sec.signal.aborted) return;
      sec.status.replaceChildren(h('span', { class: 'error' }, err.message), ' ',
        h('button', { class: 'btn small', onclick: () => fetchMore(sec) }, 'Retry'));
    } finally {
      sec.busy = false;
    }
  }

  async function browseProducts(r) {
    const base = connList.find((c) => c.id === r.connector);
    if (!base?.levels.includes('product')) return;
    setColl({ connector: r.connector, id: r.collection || r.id, label: r.title });
    if (r.target && r.target !== target.value && bodyById.has(r.target)) { target.value = r.target; await onTarget(); }
    setLevel('product');
    run();
    resultsEl.scrollTop = 0;
  }

  // ---- prefill from Explore (or anywhere): a prefill is a fresh search
  on('search:prefill', async (p) => {
    target.value = bodyById.has(p.target) ? p.target : '';
    await onTarget();
    region.value = target.value === 'earth' && regions[p.region] ? p.region : '';
    useRegion.disabled = !region.value;
    text.value = p.text ?? '';
    bbox.value = ''; start.value = end.value = ''; syncBox();
    setColl(p.collection && p.connector ? { connector: p.connector, id: p.collection, label: p.collection_label || p.collection } : null);
    if (p.connector) { seen.add(p.connector); checked = new Set([p.connector]); } else checked = new Set(connList.map((c) => c.id));
    setLevel(p.level === 'product' ? 'product' : p.level === 'collection' ? 'collection' : level());
    if (p.run) run(); else text.focus();
  });

  on('tab:shown', ({ tab }) => { if (tab === 'search') setTimeout(() => map?.resize(), 0); });
  hint('Choose a target and press Search to query the live archives. Add a box, dates or keywords to narrow it down.');
  map?.setBody(null);
  await loadConns();
}
