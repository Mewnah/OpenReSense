// Search tab: federated live search across connectors, grouped results, footprint map.
// Every search is a "view" (its form, results and scroll/map position) kept in memory and in the window history, so
// Back/Forward (the buttons, Alt+←/→, the mouse) show earlier results again instantly instead of searching again.
import { getJSON } from './api.js';
import { on } from './bus.js';
import { getBodies } from './bodies.js';
import { h, safeUrl } from './explore-util.js';
import { makeCard, openLightbox } from './search-card.js';
import { createMap } from './search-map.js';
import { createSplit } from './split.js';

const PAGE = 25;
const MAX_VIEWS = 20;  // searches kept in memory; going back to an older one runs it again
const NARROW = '(max-width: 900px)';  // the map becomes an overlay below this width (keep in step with explore.css)
const parseBox = (txt) => {
  const v = txt.split(/[\s,;]+/).filter(Boolean).map(Number);
  if (v.length !== 4 || v.some((x) => !Number.isFinite(x))) return null;
  const [w, s, e, n] = v;
  return Math.abs(w) <= 180 && Math.abs(e) <= 180 && s >= -90 && n <= 90 && s <= n ? v : null;
};
const cap = (t) => t.charAt(0).toUpperCase() + t.slice(1);
let viewSeq = 0;

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
  const formMsg = h('div', { class: 'error', role: 'alert', hidden: true });
  const form = h('form', { class: 'se-form', 'aria-label': 'Search' },
    h('label', null, 'Target', target), regionLabel,
    h('label', null, 'Text', text),
    h('div', { class: 'col', style: 'gap:4px' }, h('label', { for: 'se-bbox' }, 'Bounding box (lon/lat)'), bbox, h('div', { class: 'row', style: 'gap:6px' }, useRegion, drawBtn, clearBox)),
    h('div', { class: 'se-two' }, h('label', null, 'From', start), h('label', null, 'To', end)),
    h('fieldset', null, h('legend', { class: 'muted' }, 'Results are'), levelBox),
    collChip,
    h('fieldset', null, h('legend', { class: 'muted' }, 'Archives'), connBox),
    h('div', { class: 'row' }, go, mapBtn), formMsg);
  const resultsEl = h('div', { class: 'se-results', 'aria-live': 'polite' });
  const mapEl = h('div', { class: 'se-map', id: 'se-map' });
  const mapWrap = h('div', { class: 'se-mapwrap' }, mapEl);
  const splitEl = h('div', { class: 'se-split', role: 'separator', tabindex: 0, 'aria-orientation': 'vertical', 'aria-controls': 'se-map',
    'aria-label': 'Map width', 'aria-valuemin': 0, 'aria-valuemax': 100, title: 'Drag to resize the map · double-click to reset' });
  const grid = h('div', { class: 'se-root' }, form, resultsEl, splitEl, mapWrap);
  root.replaceChildren(grid);

  // ---- state
  const level = () => form.elements['se-level'].value;
  const setLevel = (v) => { form.elements['se-level'].value = v; renderConns(); };
  let coll = null;                 // {connector, id, label}: product search restricted to one collection
  let connList = [], checked = new Set(), seen = new Set(), connSeq = 0, shownBody;
  const views = new Map();         // id -> view (see makeView)
  let active = null;               // the view on screen
  let fromExplore = false;         // a search Explore asked for is running: its view replaces the tab's history entry
  let fitPending = false;          // the map was hidden when its view changed

  // ---- map (optional: search must work without it)
  let map = null, split = null;
  const narrow = matchMedia(NARROW);
  const hooks = {
    hover: (k) => { map?.highlight(k); },
    select: (k, fit) => { markSelected(k); map?.select(k, fit); },
    browse: (r) => browseProducts(r),
    preview: (r) => {  // ←/→ in the preview go through this search's results in screen order; the list and map follow
      const v = active;
      const list = [...v.el.querySelectorAll('.se-card')].map((el) => v.byKey.get(el.dataset.key)).filter(Boolean);
      openLightbox(r, list, (x) => {
        const k = `${x.connector}:${x.id}`;
        if (active !== v) return;
        markSelected(k);
        map?.select(k, false);
        v.cards.get(k)?.scrollIntoView({ block: 'nearest' });
      });
    },
  };
  try {
    map = createMap(mapEl, {
      onHover: (k) => { for (const [key, el] of active?.cards ?? []) el.classList.toggle('hover', key === k); },
      onSelect: (k) => { markSelected(k); if (k) active?.cards.get(k)?.scrollIntoView({ block: 'nearest', behavior: 'smooth' }); },
      onBox: (b) => { bbox.value = b.join(', '); drawBtn.classList.remove('primary'); syncBox(); },
    });
    split = createSplit(splitEl, { grid, other: resultsEl, pane: mapWrap, prop: '--se-map-w', key: 'search-map', minPane: 260, minOther: 320,
      onResize: () => map.size() });
  } catch (err) {
    console.error('[search] map unavailable', err);
    grid.classList.add('nomap');
    mapBtn.hidden = drawBtn.hidden = true;
  }
  function markSelected(k) {
    if (!active) return;
    active.selectedKey = k;
    for (const [key, el] of active.cards) el.classList.toggle('sel', key === k);
  }
  function syncBox() { map?.setBox(parseBox(bbox.value)); }

  mapBtn.onclick = () => {
    const cls = narrow.matches ? 'showmap' : 'nomap';
    const on_ = grid.classList.toggle(cls);
    mapBtn.setAttribute('aria-pressed', String(cls === 'showmap' ? on_ : !on_));
    setTimeout(() => {  // the width set while the map was hidden may not fit any more; results may have come meanwhile
      split?.apply();
      map?.resize();
      if (fitPending && active) syncMap();
    }, 0);
  };
  mapBtn.setAttribute('aria-pressed', String(!narrow.matches));
  drawBtn.onclick = () => {
    if (narrow.matches && !grid.classList.contains('showmap')) mapBtn.click();
    const active_ = !drawBtn.classList.contains('primary');
    drawBtn.classList.toggle('primary', active_);
    map.drawBox(active_);
  };

  // ---- form behaviour
  bbox.addEventListener('input', syncBox);
  clearBox.onclick = () => { bbox.value = ''; syncBox(); };
  region.addEventListener('change', () => { useRegion.disabled = !region.value; });
  useRegion.onclick = () => { bbox.value = regions[region.value].bbox.join(', '); syncBox(); };
  levelBox.addEventListener('change', renderConns);
  target.addEventListener('change', onTarget);

  function targetChanged() {  // the region row and the basemap follow the target
    const earth = target.value === 'earth';
    regionLabel.hidden = !earth;
    if (!earth) { region.value = ''; useRegion.disabled = true; }
    if (shownBody !== target.value) { shownBody = target.value; map?.setBody(bodyById.get(target.value)); }
  }
  async function onTarget() { targetChanged(); await loadConns(); }

  function setColl(c) {
    coll = c;
    collChip.hidden = !c;
    collChip.replaceChildren(...(c ? [h('span', { class: 'grow' }, `Collection: ${c.label}`),
      h('button', { type: 'button', class: 'btn small ghost', 'aria-label': 'Remove collection restriction', onclick: () => setColl(null) }, '✕')] : []));
  }

  async function loadConns() {  // archives for the current target; ones never seen before start ticked
    const my = ++connSeq;
    let list = [];
    try { list = await getJSON('/api/connectors', { target: target.value }); } catch (err) {
      if (my === connSeq) connBox.replaceChildren(h('span', { class: 'error' }, `Could not load archives: ${err.message}`));
      return false;
    }
    if (my !== connSeq) return false;
    connList = list;
    for (const c of list) if (!seen.has(c.id)) { seen.add(c.id); checked.add(c.id); }
    renderConns();
    return true;
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

  // ---- the form as data: what a view remembers, and putting it back
  const formState = () => ({ target: target.value, region: region.value, text: text.value.trim(), bbox: bbox.value.trim(),
    start: start.value, end: end.value, level: level(), coll, checked: [...checked].sort() });

  function showForm(st, conns) {  // conns: the archive list the view was made with ([] while it loads again)
    ++connSeq;  // a list still loading for another target must not replace this one
    target.value = bodyById.has(st.target) ? st.target : '';
    targetChanged();
    region.value = target.value === 'earth' && regions[st.region] ? st.region : '';
    useRegion.disabled = !region.value;
    text.value = st.text; bbox.value = st.bbox; start.value = st.start; end.value = st.end;
    syncBox();
    setColl(st.coll);
    checked = new Set(st.checked);
    for (const c of conns) seen.add(c.id);
    connList = conns;
    form.elements['se-level'].value = st.level;
    renderConns();
    formMsg.hidden = true;
  }

  async function restoreForm(st) {  // for a view no longer in memory (or from before a reload)
    showForm(st, []);
    connBox.replaceChildren(h('span', { class: 'spinner' }), ' loading archives…');
    const my = connSeq;
    try {
      const list = await getJSON('/api/connectors', { target: target.value });
      if (my !== connSeq) return false;
      for (const c of list) seen.add(c.id);
      connList = list;
      renderConns();
      return true;
    } catch (err) {
      if (my === connSeq) connBox.replaceChildren(h('span', { class: 'error' }, `Could not load archives: ${err.message}`));
      return false;
    }
  }

  const titleOf = (st) => {
    if (st.level === 'product' && st.coll) return `products in ${st.coll.label}`;
    const where = [bodyById.get(st.target)?.name, st.region && regions[st.region]?.name].filter(Boolean).join(': ');
    return [st.level === 'product' ? 'products' : 'datasets', st.text && `“${st.text}”`, where && `· ${where}`].filter(Boolean).join(' ');
  };

  // ---- running searches
  function fail(msg, el) { formMsg.textContent = msg; formMsg.hidden = false; el?.focus(); }

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

  // A new search from the form. It becomes a new history entry (Back returns to the results it replaces) unless
  // replace is set, or the entry is not a search yet (the Search tab was just opened), or it repeats the same search.
  // History entries remember (pv) what the entry before them shows: a view id, 'explore', or null when unknown.
  function run({ replace = false, backTo = null, backKey = null } = {}) {
    formMsg.hidden = true;
    if (bbox.value.trim() && !parseBox(bbox.value)) { fail('Bounding box must be four numbers W,S,E,N (lon -180..180, lat -90..90, S ≤ N).', bbox); return; }
    const conns = connList.filter((c) => c.levels.includes(level()) && (coll && level() === 'product' ? c.id === coll.connector : checked.has(c.id)));
    if (!conns.length) { fail(connList.length ? 'Tick at least one archive that supports this result level.' : 'No live-search connectors are available for this target.'); return; }
    const st = formState();
    const cur = history.state?.sv ? views.get(history.state.sv) : null;
    const same = !replace && cur && JSON.stringify(cur.state) === JSON.stringify(st);
    const push = !replace && !!history.state?.sv && !same;
    if (push) { backTo = cur?.title ?? null; backKey = cur?.id ?? null; } else if (same) { backTo = cur.backTo; backKey = cur.backKey; }
    const v = makeView(st, conns, backTo, backKey);
    // a repeat keeps what the entry knew about the one before it; other entries may still show the old view (kept)
    const entry = { ...(history.state || {}), sv: v.id, sf: st, sb: backTo, pv: push || replace ? backKey : history.state?.pv ?? null };
    if (push) history.pushState(entry, '', '#search'); else history.replaceState(entry, '', '#search');
    activate(v);
    v.sections.forEach((s) => fetchMore(s));
  }

  function makeView(st, conns, backTo, backKey) {
    const v = { id: `${Date.now().toString(36)}.${++viewSeq}`, state: st, connList, title: titleOf(st), backTo, backKey,
      cards: new Map(), byKey: new Map(), results: [], selectedKey: null, scroll: 0, mapView: null, ctl: new AbortController() };
    v.sections = conns.map((c) => makeSection(v, c));
    v.backBtn = backTo ? h('button', { type: 'button', class: 'btn small', title: `Back to ${backTo} (Alt+←)`, onclick: () => history.back() }, `← Back to ${backTo}`) : null;
    v.el = h('div', { class: 'se-view' },
      h('div', { class: 'se-crumb' }, v.backBtn, h('b', { class: 'grow', title: cap(v.title) }, cap(v.title))),
      v.sections.map((s) => s.el));
    views.set(v.id, v);
    for (const [id, old] of views) {  // oldest first; keep the one on screen
      if (views.size <= MAX_VIEWS) break;
      if (old !== active && old !== v) { old.ctl.abort(); views.delete(id); }
    }
    return v;
  }

  function activate(v) {
    if (active !== v) {
      if (active) { active.scroll = resultsEl.scrollTop; if (map?.shown()) active.mapView = map.getView(); }
      active = v;
      showForm(v.state, v.connList);
      resultsEl.replaceChildren(v.el);
      resultsEl.scrollTop = v.scroll;
      if (map) {
        map.clear();
        map.add(v.results);
        syncMap();
        if (v.selectedKey) { markSelected(v.selectedKey); map.select(v.selectedKey, false); }
      }
    }
    syncBack();
  }
  // The in-results Back button only when the history entry before this one is the one it names (a view reached
  // again through another tab has a different entry before it; the top bar's ← still steps back).
  function syncBack() {
    if (active?.backBtn) active.backBtn.hidden = !(history.state?.sv === active.id && (history.state.pv ?? null) === active.backKey);
  }
  function syncMap() {  // place the map for the view on screen; waits while the map is hidden
    fitPending = !map.shown();
    if (fitPending) return;
    if (active.mapView) map.setView(active.mapView);
    else if (active.results.length) map.fit();
  }

  function makeSection(v, c) {
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
    const sec = { v, c, q, el, status, notes, list, more, shown: 0, next: 0, total: null, busy: false };
    more.onclick = () => fetchMore(sec);
    return sec;
  }

  async function fetchMore(sec) {
    if (sec.busy) return;
    const { v } = sec;
    sec.busy = true;
    sec.more.hidden = true;
    sec.status.replaceChildren(h('span', { class: 'spinner' }), ' searching…');
    try {
      const page = await getJSON('/api/search', { ...sec.q, offset: sec.next }, { signal: v.ctl.signal });
      const tn = (r) => bodyById.get(r.target)?.name ?? r.target;
      for (const r of page.results) {
        const key = `${r.connector}:${r.id}`;
        if (v.cards.has(key)) continue;
        const el = makeCard(r, key, hooks, tn(r));
        v.cards.set(key, el);
        v.byKey.set(key, r);
        sec.list.append(el);
      }
      v.results.push(...page.results);
      if (v === active && map) {  // results of a view not on screen reach the map when it is shown again
        map.add(page.results);
        if (!sec.shown && !v.mapView) { if (map.shown()) map.fit(); else fitPending = true; }
      }
      sec.shown += page.results.length;
      sec.total = page.total;
      sec.next = page.next_offset;
      sec.status.textContent = page.total != null ? `${sec.shown.toLocaleString()} of ${page.total.toLocaleString()}` : `${sec.shown} shown`;
      sec.notes.replaceChildren(...page.notes.map((n) => h('div', null, n)));
      if (!sec.shown) sec.list.replaceChildren(h('div', { class: 'muted', style: 'padding:8px 0' }, 'No results. Try fewer constraints (text, box, dates).'));
      sec.more.hidden = page.next_offset == null;
      if (v === active && v.selectedKey) markSelected(v.selectedKey);
    } catch (err) {
      if (v.ctl.signal.aborted) return;
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
  }

  // ---- history: Back/Forward to a search shows its view again (or runs it again when it is no longer kept)
  addEventListener('popstate', async (e) => {
    const st = e.state;
    if (!st?.sv) return;
    const v = views.get(st.sv);
    if (v) { activate(v); return; }
    if (!st.sf || !await restoreForm(st.sf)) return;
    // the archive list loaded; Back/Forward may have moved on meanwhile: then that entry must not become this search
    if (history.state?.sv === st.sv) run({ replace: true, backTo: st.sb ?? null, backKey: st.pv ?? null });
    else if (active) showForm(active.state, active.connList);
  });

  // ---- prefill from Explore (or anywhere): a prefill is a fresh search
  on('search:prefill', async (p) => {
    fromExplore = true;  // set before the tab switch: that history entry is this search's, not the old results'
    try {
      target.value = bodyById.has(p.target) ? p.target : '';
      await onTarget();
      region.value = target.value === 'earth' && regions[p.region] ? p.region : '';
      useRegion.disabled = !region.value;
      text.value = p.text ?? '';
      bbox.value = ''; start.value = end.value = ''; syncBox();
      setColl(p.collection && p.connector ? { connector: p.connector, id: p.collection, label: p.collection_label || p.collection } : null);
      if (p.connector) { seen.add(p.connector); checked = new Set([p.connector]); } else checked = new Set(connList.map((c) => c.id));
      setLevel(p.level === 'product' ? 'product' : p.level === 'collection' ? 'collection' : level());
      // the tab switch made a new, untagged history entry: this search takes it, and Back leads to Explore
      if (p.run) run(history.state?.sv ? {} : { replace: true, backTo: 'Explore', backKey: 'explore' }); else text.focus();
    } finally {
      fromExplore = false;
    }
  });

  on('tab:shown', ({ tab }) => {
    if (tab !== 'search') return;
    setTimeout(() => {
      if (!map) return;
      map.resize();
      if (fitPending && active) syncMap();
    }, 0);
    // An entry made by clicking the Search tab shows the current results: tag it so Back/Forward return to them.
    // What comes before it is another tab, so it has no in-results Back button (pv null).
    if (active && !history.state?.sv && !fromExplore) {
      history.replaceState({ ...(history.state || {}), sv: active.id, sf: active.state, sb: active.backTo, pv: null }, '', '#search');
      syncBack();
    }
  });

  resultsEl.replaceChildren(h('div', { class: 'se-hint muted' }, 'Choose a target and press Search to query the live archives. Add a box, dates or keywords to narrow it down.'));
  map?.setBody(null);
  shownBody = '';
  await loadConns();
  // reopened on a search (a reload): run it again
  const st0 = history.state;
  if (st0?.sv && st0.sf && await restoreForm(st0.sf) && history.state?.sv === st0.sv) {
    run({ replace: true, backTo: st0.sb ?? null, backKey: st0.pv ?? null });
  }
}
