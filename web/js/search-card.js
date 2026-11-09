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

export const previewOf = (r) => safeUrl(r.browse) || safeUrl(r.thumbnail);
const MAX_ZOOM = 32;  // preview zoom limit: each image pixel 32 screen pixels wide

// Image preview: the browse image (or thumbnail), zoomed with the wheel at the cursor and dragged to pan; enlarged past
// 100 % it shows the image's own pixels. list: the results to step through with ←/→; onShow(r) follows along.
let lb;
export function openLightbox(r, list = [r], onShow = null) {
  if (!previewOf(r)) return;
  lb ??= makeLightbox();
  const items = list.filter(previewOf);
  lb.open(items.includes(r) ? items : [r], r, onShow);
}

function makeLightbox() {
  const img = h('img', { alt: '', draggable: 'false' });
  const msg = h('div', { class: 'lb-msg' });
  const stage = h('div', { class: 'lb-stage', tabindex: -1 }, img, msg);
  const title = h('b', { class: 'grow lb-title' });
  const pos = h('span', { class: 'muted lb-pos' });
  const zoomTxt = h('span', { class: 'lb-zoom', title: "Zoom: 100 % shows the image's own pixels" });
  const btn = (label, tip, fn) => h('button', { type: 'button', class: 'btn small', title: tip, 'aria-label': tip.replace(/ \(.*\)$/, ''), onclick: fn }, label);
  const prev = btn('◀', 'Previous image (←)', () => step(-1));
  const next = btn('▶', 'Next image (→)', () => step(1));
  const orig = h('a', { class: 'btn small', target: '_blank', rel: 'noopener noreferrer', title: 'Open the image in your web browser' }, 'Open original ↗');
  const dlg = h('dialog', { class: 'dlg lb', 'aria-label': 'Image preview' },
    h('div', { class: 'lb-bar' }, prev, next, pos, title, zoomTxt,
      btn('−', 'Zoom out (−)', () => zoomBy(1 / 1.5)), btn('+', 'Zoom in (+)', () => zoomBy(1.5)),
      btn('Fit', 'Fit to the window (0)', () => view(fitScale())), btn('1:1', "The image's own pixels (1)", () => view(1)),
      orig, btn('✕', 'Close (Esc)', () => dlg.close())),
    stage,
    h('div', { class: 'lb-hint muted' }, 'Scroll to zoom · drag to pan · double-click: 1:1 / fit · ← → other results'));
  let down = null;  // close on a click on the backdrop only (not a text selection dragged out onto it)
  dlg.addEventListener('pointerdown', (e) => { down = e.target; });
  dlg.addEventListener('click', (e) => { if (e.target === dlg && down === dlg) dlg.close(); });
  document.body.append(dlg);

  let list = [], i = 0, onShow = null, seq = 0;
  let s = 1, tx = 0, ty = 0, nw = 0, nh = 0;  // scale, image position in the stage, natural size
  const fitScale = () => (nw ? Math.min(stage.clientWidth / nw, stage.clientHeight / nh) : 1);
  const startScale = () => Math.min(fitScale(), 1);  // like before: never enlarged at first

  function draw() {
    if (!nw) return;
    const w = nw * s, hh = nh * s, sw = stage.clientWidth, sh = stage.clientHeight;
    tx = w <= sw ? (sw - w) / 2 : Math.min(0, Math.max(sw - w, tx));  // smaller than the window: centered
    ty = hh <= sh ? (sh - hh) / 2 : Math.min(0, Math.max(sh - hh, ty));  // larger: no empty margin
    Object.assign(img.style, { width: `${w}px`, height: `${hh}px`, transform: `translate(${tx}px, ${ty}px)` });
    img.classList.toggle('px', s > 1);
    zoomTxt.textContent = `${Math.round(s * 100)} %`;
  }
  function zoomAt(f, cx, cy) {  // keep the image point under (cx, cy) in place
    const ns = Math.min(Math.max(s * f, startScale() / 2), MAX_ZOOM);
    tx = cx - ((cx - tx) / s) * ns;
    ty = cy - ((cy - ty) / s) * ns;
    s = ns;
    draw();
  }
  const zoomBy = (f) => zoomAt(f, stage.clientWidth / 2, stage.clientHeight / 2);
  const view = (scale) => zoomBy(scale / s);

  stage.addEventListener('wheel', (e) => {
    e.preventDefault();
    const r = stage.getBoundingClientRect();
    zoomAt(Math.exp(-e.deltaY * (e.deltaMode === 1 ? 0.05 : 0.0015)), e.clientX - r.left, e.clientY - r.top);
  }, { passive: false });
  stage.addEventListener('pointerdown', (e) => {
    if (e.button !== 0 || !nw || stage.classList.contains('dragging')) return;  // one pointer pans (a second finger is ignored)
    stage.setPointerCapture(e.pointerId);
    stage.classList.add('dragging');
    let x = e.clientX, y = e.clientY;
    const move = (ev) => { if (ev.pointerId !== e.pointerId) return; tx += ev.clientX - x; ty += ev.clientY - y; x = ev.clientX; y = ev.clientY; draw(); };
    const up = (ev) => {
      if (ev.pointerId !== e.pointerId) return;
      stage.classList.remove('dragging');
      stage.removeEventListener('pointermove', move); stage.removeEventListener('pointerup', up); stage.removeEventListener('pointercancel', up);
    };
    stage.addEventListener('pointermove', move);
    stage.addEventListener('pointerup', up);
    stage.addEventListener('pointercancel', up);
  });
  stage.addEventListener('dblclick', (e) => {  // 1:1 at the cursor, then (small images) fit, then back to the start
    const r = stage.getBoundingClientRect(), fit = Math.min(fitScale(), MAX_ZOOM);
    if (s < 0.99) zoomAt(1 / s, e.clientX - r.left, e.clientY - r.top);
    else view(s < fit * 0.99 ? fit : startScale());
  });
  dlg.addEventListener('keydown', (e) => {
    if (e.ctrlKey || e.metaKey || e.altKey) return;  // browser zoom (Ctrl +/−/0) and Back (Alt+←) stay the browser's
    if (e.target.closest?.('a, button') && (e.key === 'Enter' || e.key === ' ')) return;
    const act = { ArrowLeft: () => step(-1), ArrowRight: () => step(1), '+': () => zoomBy(1.5), '=': () => zoomBy(1.5),
      '-': () => zoomBy(1 / 1.5), 0: () => view(fitScale()), 1: () => view(1) }[e.key];
    if (act) { e.preventDefault(); act(); }
  });
  new ResizeObserver(draw).observe(stage);

  function step(d) { if (list.length > 1) show((i + d + list.length) % list.length); }
  function show(k) {
    i = k;
    const r = list[i], src = previewOf(r), my = ++seq;
    title.textContent = r.title;
    title.title = r.title;
    dlg.setAttribute('aria-label', `Image preview: ${r.title}`);
    pos.textContent = list.length > 1 ? `${i + 1} / ${list.length}` : '';
    prev.hidden = next.hidden = list.length < 2;
    orig.href = src;
    nw = nh = 0;
    img.hidden = true;
    zoomTxt.textContent = '';
    msg.textContent = 'Loading…';
    msg.hidden = false;
    img.onload = () => {
      if (my !== seq) return;
      nw = img.naturalWidth; nh = img.naturalHeight;
      msg.hidden = true; img.hidden = false;
      s = startScale();
      draw();
    };
    img.onerror = () => { if (my === seq) msg.textContent = 'This preview could not be loaded. "Open original" tries it in your browser.'; };
    img.removeAttribute('src');  // the same image again still fires load
    img.src = src;
    onShow?.(r);
  }
  return {
    open(items, r, cb) {
      list = items; onShow = cb;
      if (!dlg.open) dlg.showModal();
      show(Math.max(0, list.indexOf(r)));
      stage.focus();
    },
  };
}

// hooks: {browse(r), hover(key|null), select(key), preview(r)}; returns the card element.
export function makeCard(r, key, hooks, targetName) {
  const preview = safeUrl(r.thumbnail) || safeUrl(r.browse);
  const thumb = preview
    ? h('button', { class: 'se-thumb', title: 'Preview (zoom with the wheel)', 'aria-label': `Preview ${r.title}`, onclick: () => hooks.preview(r) },
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
