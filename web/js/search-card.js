// Result card + lightbox for the Search tab.
import { emit } from './bus.js';
import { h, fmtBytes, safeUrl } from './explore-util.js';

const when = (t) => (t || '').replace('T', ' ').replace(/(:00)?(\.\d+)?Z?$/, '').replace(/ 00:00$/, '');
function timeText(r) {
  const a = when(r.start), b = when(r.end);
  return a && b && a !== b ? `${a} → ${b}` : a || b;
}

const ROLE_ORDER = ['data', 'label', 'browse', 'thumbnail', 'metadata', 'other'];

export function openInViewer(r, a) {
  if (!safeUrl(a.url)) return;
  const companions = r.assets.filter((x) => x !== a && (x.role === 'data' || x.role === 'label')).map((x) => safeUrl(x.url)).filter(Boolean);
  emit('viewer:open', { url: a.url, name: a.name, companions, meta: r });
  emit('nav', { tab: 'viewer' });
}

let dlg;
export function openLightbox(r) {
  const src = safeUrl(r.browse) || safeUrl(r.thumbnail);
  if (!src) return;
  if (!dlg) {
    dlg = h('dialog', { class: 'dlg se-lightbox', 'aria-label': 'Image preview' });
    dlg.addEventListener('click', (e) => { if (e.target === dlg) dlg.close(); });
    document.body.append(dlg);
  }
  const img = h('img', { src, alt: r.title });
  const err = h('div', { class: 'error', hidden: true }, 'Preview failed to load.');
  img.onerror = () => { img.hidden = true; err.hidden = false; };
  dlg.replaceChildren(img, err, h('div', { class: 'row' }, h('b', { class: 'grow' }, r.title),
    h('a', { class: 'btn small', href: src, target: '_blank', rel: 'noopener noreferrer' }, 'Open original ↗'),
    h('button', { class: 'btn small', autofocus: true, onclick: () => dlg.close() }, 'Close')));
  dlg.showModal();
}

// hooks: {browse(r), hover(key|null), select(key)}; returns the card element.
export function makeCard(r, key, hooks, targetName) {
  const preview = safeUrl(r.thumbnail) || safeUrl(r.browse);
  const thumb = preview
    ? h('button', { class: 'se-thumb', title: 'Preview', 'aria-label': `Preview ${r.title}`, onclick: () => openLightbox(r) },
      Object.assign(h('img', { src: preview, alt: '', loading: 'lazy' }), { onerror() { this.replaceWith(h('span', null, 'no preview')); } }))
    : h('div', { class: 'se-thumb none' }, 'no preview');
  const sub = [[r.mission, r.instrument].filter(Boolean).join(' · '), timeText(r), targetName,
    r.bbox && `[${r.bbox.map((v) => +v.toFixed(2)).join(', ')}]`].filter(Boolean).map((t) => h('span', null, t));
  const landing = safeUrl(r.landing);
  const assets = [...(r.assets || [])].sort((a, b) => ROLE_ORDER.indexOf(a.role) - ROLE_ORDER.indexOf(b.role));
  const extra = Object.entries(r.extra || {});

  const card = h('article', { class: 'se-card', 'data-key': key },
    thumb,
    h('div', { class: 'grow' },
      h('h3', { onclick: () => hooks.select(key, true) }, r.title),
      h('div', { class: 'se-sub' }, sub),
      r.description && h('p', { class: 'se-desc', title: r.description }, r.description),
      h('div', { class: 'row' },
        r.level === 'collection' && h('button', { class: 'btn small primary', onclick: () => hooks.browse(r) }, 'Browse products →'),
        landing && h('a', { class: 'btn small', href: landing, target: '_blank', rel: 'noopener noreferrer' }, 'Landing page ↗')),
      assets.length ? h('details', { class: 'se-assets' }, h('summary', null, `Files (${assets.length})`),
        assets.map((a) => h('div', { class: 'se-asset' },
          h('span', { class: 'badge' }, a.role), h('span', { class: 'grow', title: a.url }, a.name),
          h('span', { class: 'muted' }, fmtBytes(a.size)),
          a.openable && safeUrl(a.url) && h('button', { class: 'btn small primary', onclick: () => openInViewer(r, a) }, 'Open in viewer'),
          safeUrl(a.url) && h('a', { class: 'btn small', href: a.url, target: '_blank', rel: 'noopener noreferrer', download: a.name }, 'Download')))) : null,
      extra.length ? h('details', { class: 'se-extra' }, h('summary', null, 'Details'),
        h('table', { class: 'se-table' }, extra.map(([k, v]) => h('tr', null, h('th', null, k),
          h('td', null, v !== null && typeof v === 'object' ? JSON.stringify(v) : String(v)))))) : null));
  card.addEventListener('mouseenter', () => hooks.hover(key));
  card.addEventListener('mouseleave', () => hooks.hover(null));
  return card;
}
