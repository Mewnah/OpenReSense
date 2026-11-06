// Explore tab: body picker, faceted catalog search, result cards, live-dataset strip.
import { getJSON } from './api.js';
import { on } from './bus.js';
import { getBodies } from './bodies.js';
import { h, debounce, getConnectors, gotoSearch, safeUrl } from './explore-util.js';
import { createLive } from './explore-live.js';

const PAGE = 60;
// [facet name (also the filter param), title]
const FACETS = [['agency', 'Agency'], ['kind', 'Kind'], ['data_types', 'Data type'], ['spectral', 'Spectral range'],
  ['levels', 'Processing level'], ['formats', 'Format'], ['access', 'Access']];
const CHIP_FIELDS = ['data_types', 'spectral', 'levels', 'formats'];
const link = (url, cls, ...kids) => h('a', { href: safeUrl(url), class: cls, target: '_blank', rel: 'noopener noreferrer' }, ...kids);

export async function init(root) {
  const [{ bodies, regions }, vocab, connectors] = await Promise.all([
    getBodies(), getJSON('/api/vocab').catch(() => ({})), getConnectors()]);
  const connById = new Map(connectors.map((c) => [c.id, c]));
  const liveIds = connectors.map((c) => c.id);
  const label = (facet, v) => vocab[facet]?.[v] ?? v;
  const bodyById = new Map(bodies.map((b) => [b.id, b]));

  const s = { q: '', target: '', region: '', live: false, f: Object.fromEntries(FACETS.map(([k]) => [k, new Set()])) };
  let facets = {}, total = 0, results = [], seq = 0, abort;
  const railClosed = new Set();

  const rail = h('nav', { class: 'ex-rail', 'aria-label': 'Targets' });
  const head = h('header', { class: 'ex-head' });
  const liveEl = h('section', { class: 'ex-live', 'aria-label': 'Live datasets' });
  const live = createLive(liveEl);
  const search = h('input', { class: 'input ex-q', type: 'search', placeholder: 'Search sources: titan radar, antarctic, HiRISE, JAXA…   ( / )', 'aria-label': 'Search catalog', autocomplete: 'off' });
  const summary = h('div', { class: 'ex-summary muted', 'aria-live': 'polite' });
  const list = h('div', { class: 'ex-list' });
  const more = h('button', { class: 'btn ex-more', hidden: true, onclick: () => fetchPage(true) }, 'Show more');
  const side = h('aside', { class: 'ex-facets', 'aria-label': 'Filters' });
  const main = h('div', { class: 'ex-main scroll' }, head, liveEl,
    h('div', { class: 'ex-body' }, h('div', { class: 'ex-results' }, search, summary, list, more), side));
  root.replaceChildren(h('div', { class: 'ex-root' }, rail, main));

  const params = () => ({
    q: s.q.trim(), target: s.target, region: s.region, connector: s.live ? liveIds : '',
    ...Object.fromEntries(FACETS.map(([k]) => [k, [...s.f[k]]])),
  });
  const anyFilter = () => FACETS.some(([k]) => s.f[k].size) || s.live || s.q.trim() || s.region;

  async function fetchPage(append = false) {
    const my = append ? seq : ++seq;
    abort?.abort();
    const ctl = (abort = new AbortController());
    if (!append) summary.replaceChildren(h('span', { class: 'spinner' }), ' Searching…');
    try {
      const r = await getJSON('/api/catalog', { ...params(), limit: PAGE, offset: append ? results.length : 0 }, { signal: ctl.signal });
      if (my !== seq) return;
      results = append ? results.concat(r.results) : r.results;
      ({ total, facets } = r);
      if (!append) { list.replaceChildren(); renderRail(); renderHead(); renderFacets(); }
      list.append(...r.results.map(card));
      more.hidden = results.length >= total;
      more.textContent = `Show more (${total - results.length} left)`;
      summary.textContent = total ? `${total.toLocaleString()} source${total === 1 ? '' : 's'}` : '';
      if (!total) list.replaceChildren(empty());
    } catch (err) {
      if (ctl.signal.aborted) return;
      summary.replaceChildren(h('span', { class: 'error' }, `Catalog request failed: ${err.message} `),
        h('button', { class: 'btn small', onclick: () => fetchPage() }, 'Retry'));
    }
  }
  const refresh = debounce(() => fetchPage(), 180);

  const targetName = () => bodyById.get(s.target)?.name;
  const searchLive = () => gotoSearch({ target: s.target, region: s.region, text: s.q.trim() || undefined, run: true });

  function empty() {
    return h('div', { class: 'ex-empty' },
      h('b', null, 'No catalog sources match.'),
      h('p', { class: 'muted' }, anyFilter() ? 'Loosen the filters or the search words.' : `The catalog has nothing listed${s.target ? ` for ${targetName()}` : ''} yet.`),
      h('div', { class: 'row' },
        anyFilter() && h('button', { class: 'btn', onclick: clearFilters }, 'Clear search and filters'),
        s.target && h('button', { class: 'btn primary', onclick: searchLive }, `Search live archives for ${targetName()}`)));
  }

  function clearFilters() {
    s.q = search.value = ''; s.region = ''; s.live = false;
    FACETS.forEach(([k]) => s.f[k].clear());
    fetchPage();
  }

  function select(target, region = '') {
    s.target = target; s.region = region;
    live.show(target, region);
    fetchPage();
    main.scrollTop = 0;
  }

  // ---- left rail
  function renderRail() {
    const counts = facets.targets || {};
    const rowFor = (b) => {
      const n = counts[b.id] || 0;
      const sw = b.thumb ? h('img', { class: 'ex-sw', src: b.thumb, alt: '', loading: 'lazy' })
        : h('span', { class: 'ex-sw', style: `background:${b.color || '#667'}` });
      return h('div', null,
        h('button', { class: `ex-body-btn${s.target === b.id ? ' sel' : ''}${n ? '' : ' zero'}`, 'aria-pressed': s.target === b.id, onclick: () => select(b.id) },
          sw, h('span', { class: 'grow' }, b.name), h('span', { class: 'ex-n' }, n)),
        b.id === 'earth' && s.target === 'earth' && regionList());
    };
    const groups = new Map();
    for (const b of bodies) (groups.get(b.group) ?? groups.set(b.group, []).get(b.group)).push(b);
    rail.replaceChildren(
      h('button', { class: `ex-body-btn${s.target ? '' : ' sel'}`, onclick: () => select('') },
        h('span', { class: 'ex-sw ex-all' }, '✦'), h('span', { class: 'grow' }, 'All targets')),
      ...[...groups].map(([g, bs]) => {
        const d = h('details', { class: 'ex-group' }, h('summary', null, g), bs.map(rowFor));
        d.open = !railClosed.has(g) || bs.some((b) => b.id === s.target);
        d.addEventListener('toggle', () => (d.open ? railClosed.delete(g) : railClosed.add(g)));
        return d;
      }));
  }

  function regionList() {
    const rc = facets.regions || {};
    return h('div', { class: 'ex-regions' }, Object.entries(regions).map(([id, r]) => h('button', {
      class: `ex-region${s.region === id ? ' sel' : ''}${rc[id] ? '' : ' zero'}`, 'aria-pressed': s.region === id,
      onclick: () => { s.region = s.region === id ? '' : id; live.show(s.target, s.region); fetchPage(); },
    }, h('span', { class: 'grow' }, r.name), h('span', { class: 'ex-n' }, rc[id] || 0))));
  }

  // ---- header for the selected target
  function renderHead() {
    const b = bodyById.get(s.target);
    if (!b) {
      head.replaceChildren(h('div', { class: 'ex-head-text' }, h('h1', null, 'Every remote-sensing archive, in one place'),
        h('p', { class: 'muted' }, 'Pick a body on the left, or just search: Titan, Antarctic, thermal, JAXA. Then search the live archives and open the raw data in the viewer.')));
      return;
    }
    const regionName = s.region && regions[s.region]?.name;
    const kind = [b.type, b.parent && `of ${bodyById.get(b.parent)?.name ?? b.parent}`].filter(Boolean).join(' ');
    head.replaceChildren(
      b.thumb ? h('img', { class: 'ex-head-img', src: b.thumb, alt: '' }) : h('span', { class: 'ex-head-img', style: `background:${b.color || '#667'}` }),
      h('div', { class: 'ex-head-text' },
        h('h1', null, regionName ? `${b.name}: ${regionName}` : b.name),
        h('div', { class: 'muted' }, kind, ' · ', `${(facets.targets?.[b.id] || 0).toLocaleString()} catalog sources`),
        b.summary && h('p', null, b.summary),
        h('div', { class: 'row' },
          h('button', { class: 'btn primary', onclick: searchLive }, `Search live archives for ${regionName || b.name}`),
          h('button', { class: 'btn ghost', onclick: () => select('') }, 'Clear target'))));
  }

  // ---- facets
  function renderFacets() {
    const conn = facets.connector || {};
    const nLive = liveIds.reduce((a, id) => a + (conn[id] || 0), 0);
    side.replaceChildren(...[
      h('div', { class: 'row' }, h('b', { class: 'grow' }, 'Filters'),
        anyFilter() && h('button', { class: 'btn small ghost', onclick: clearFilters }, 'Clear all')),
      liveIds.length ? h('label', { class: 'ex-check ex-live-check' },
        h('input', { type: 'checkbox', checked: s.live, onchange: (e) => { s.live = e.target.checked; fetchPage(); } }),
        h('span', { class: 'grow' }, 'Has live search'), h('span', { class: 'ex-n' }, nLive)) : null,
      ...FACETS.map(([k, title]) => facetBox(k, title))].filter(Boolean));
  }

  function facetBox(k, title) {
    const sel = s.f[k], counts = facets[k] || {};
    const rows = [...new Set([...sel, ...Object.keys(counts)])]
      .sort((a, b) => (sel.has(b) - sel.has(a)) || (counts[b] || 0) - (counts[a] || 0));
    if (!rows.length) return null;
    const d = h('details', { class: 'ex-facet' }, h('summary', null, title, sel.size ? h('span', { class: 'ex-badge' }, sel.size) : null));
    d.open = sel.size > 0 || ['agency', 'data_types', 'spectral'].includes(k);
    const box = h('div', { class: 'ex-facet-list' });
    const draw = (all) => box.replaceChildren(...rows.slice(0, all ? rows.length : 8).map((v) => h('label', { class: 'ex-check' },
      h('input', { type: 'checkbox', checked: sel.has(v), onchange: (e) => { e.target.checked ? sel.add(v) : sel.delete(v); fetchPage(); } }),
      h('span', { class: 'grow', title: v }, label(k, v)), h('span', { class: 'ex-n' }, counts[v] || 0))),
      ...(rows.length > 8 && !all ? [h('button', { class: 'btn small ghost', onclick: () => draw(true) }, `Show ${rows.length - 8} more`)] : []));
    draw(false);
    d.append(box);
    return d;
  }

  // ---- result card
  function card(e) {
    const toggle = (k, v) => () => { s.f[k].has(v) ? s.f[k].delete(v) : s.f[k].add(v); fetchPage(); };
    const chips = CHIP_FIELDS.flatMap((f) => (e[f] || []).map((v) => h('button', {
      class: `ex-tag ex-tag-${f}${s.f[f].has(v) ? ' on' : ''}`, title: `Filter: ${label(f, v)}`, onclick: toggle(f, v) }, label(f, v))));
    const conn = e.connector && connById.get(e.connector);
    const tg = (e.targets || []).map((t) => h('button', { class: 'ex-tag ex-tag-target', title: 'Show this target', onclick: () => select(t) },
      bodyById.get(t)?.name ?? vocab.targets?.[t] ?? t));
    const meta = [['Missions', e.missions], ['Instruments', e.instruments]].filter(([, v]) => v?.length)
      .map(([k, v]) => h('div', null, h('span', { class: 'muted' }, `${k}: `), v.join(', ')));
    const facts = [e.resolution && `Resolution ${e.resolution}`, e.temporal && `Time ${e.temporal}`].filter(Boolean).join('  ·  ');
    const desc = h('p', { class: 'ex-desc', tabindex: 0, title: 'Click to expand', onclick: () => desc.classList.toggle('open'),
      onkeydown: (ev) => { if (ev.key === 'Enter' || ev.key === ' ') { ev.preventDefault(); desc.classList.toggle('open'); } } }, e.description || '');
    return h('article', { class: 'ex-card' },
      h('div', { class: 'ex-card-top' },
        h('h3', null, link(e.url, null, e.name)),
        h('span', { class: 'badge' }, label('kind', e.kind)),
        h('span', { class: `badge ${e.access === 'open' ? 'ok' : e.access === 'restricted' || e.access === 'commercial' ? 'warn' : ''}` }, label('access', e.access))),
      h('div', { class: 'muted ex-prov' }, [e.provider, ...(e.agency || []).filter((a) => !e.provider?.includes(a))].filter(Boolean).join(' · ')),
      desc, h('div', { class: 'ex-tags' }, tg, chips), meta.length ? h('div', { class: 'ex-meta' }, meta) : null,
      facts && h('div', { class: 'muted ex-facts' }, facts),
      h('div', { class: 'row ex-actions' },
        link(e.url, 'btn small primary', 'Open ↗'),
        (e.links || []).map((l) => link(l.url, 'btn small', l.label, ' ↗')),
        conn && h('button', { class: 'btn small', title: conn.description, onclick: () => liveFor(e, conn) }, '⚡ Live search')));
  }

  function liveFor(e, conn) {
    const ok = (t) => conn.targets.includes('*') || conn.targets.includes(t);
    const own = e.targets || [];
    const target = own.includes(s.target) && ok(s.target) ? s.target : own.find(ok) ?? s.target;
    gotoSearch({ connector: conn.id, target, region: target === s.target ? s.region : undefined,
      level: conn.levels.includes('collection') ? 'collection' : 'product', run: true });
  }

  search.addEventListener('input', () => { s.q = search.value; refresh(); });
  document.addEventListener('keydown', (ev) => {
    if (ev.key === '/' && !root.hidden && !/^(INPUT|TEXTAREA|SELECT)$/.test(document.activeElement?.tagName)) {
      ev.preventDefault(); search.focus(); search.select();
    }
  });
  on('tab:shown', ({ tab }) => { if (tab === 'explore' && !seq) fetchPage(); });
  await fetchPage();
}
