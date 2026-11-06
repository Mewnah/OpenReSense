// "Live datasets" strip: collection-level results from every live connector that covers the selected target.
import { getJSON } from './api.js';
import { h, getConnectors, gotoSearch, safeUrl } from './explore-util.js';

const LIMIT = 24;

export function createLive(el) {
  let abort;

  async function show(target, region) {
    abort?.abort();
    el.replaceChildren();
    if (!target) return;
    const ctl = (abort = new AbortController());
    const conns = (await getConnectors()).filter((c) => c.levels.includes('collection')
      && (c.targets.includes('*') || c.targets.includes(target)));
    if (ctl.signal.aborted) return;
    if (!conns.length) {
      el.append(h('div', { class: 'muted ex-live-none' }, 'No live-search connectors cover this target yet. The catalog entries below link to the archives directly.'));
      return;
    }
    el.append(h('div', { class: 'ex-live-title' }, 'Live datasets ', h('span', { class: 'muted' }, '— queried now from the archives')));
    for (const c of conns) el.append(row(c, target, region, ctl.signal));
  }

  function row(c, target, region, signal) {
    const status = h('span', { class: 'muted' });
    const strip = h('div', { class: 'ex-live-strip' });
    const wrap = h('div', { class: 'ex-live-row' },
      h('div', { class: 'row' }, h('a', { href: safeUrl(c.url), target: '_blank', rel: 'noopener noreferrer', title: c.description }, c.name), status));
    const load = () => {
      status.replaceChildren(h('span', { class: 'spinner' }));
      strip.replaceChildren();
      getJSON('/api/search', { connector: c.id, level: 'collection', target, region, limit: LIMIT }, { signal })
        .then((page) => {
          const total = page.total ?? page.results.length;
          status.textContent = `${total.toLocaleString()} dataset${total === 1 ? '' : 's'}${total > page.results.length ? ` (first ${page.results.length})` : ''}`;
          if (!page.results.length) strip.append(h('span', { class: 'muted' }, 'Nothing found.'));
          for (const r of page.results) strip.append(item(c, r, target, region));
          if (total > page.results.length) {
            strip.append(h('button', { class: 'btn small', onclick: () => gotoSearch({ connector: c.id, target, region, level: 'collection', run: true }) }, 'See all in Search →'));
          }
        })
        .catch((err) => {
          if (signal.aborted) return;
          status.replaceChildren(h('span', { class: 'error' }, err.message), ' ',
            h('button', { class: 'btn small', onclick: load }, 'Retry'));
        });
    };
    load();
    wrap.append(strip);
    return wrap;
  }

  function item(c, r, target, region) {
    const sub = [r.mission, r.instrument].filter(Boolean).join(' · ');
    return h('button', {
      class: 'ex-live-item', title: r.description || r.title,
      onclick: () => gotoSearch({ connector: c.id, collection: r.collection, collection_label: r.title, target, region, level: 'product', run: true }),
    }, h('b', null, r.title), sub && h('span', { class: 'muted' }, sub), r.description && h('span', { class: 'ex-clamp2' }, r.description));
  }

  return { show };
}
