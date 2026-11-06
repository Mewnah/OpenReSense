// Small helpers shared by the Explore and Search tabs.
import { getJSON } from './api.js';
import { emit } from './bus.js';

export function h(tag, attrs, ...kids) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v == null || v === false) continue;
    if (k === 'class') el.className = v;
    else if (k.startsWith('on')) el.addEventListener(k.slice(2), v);
    else el.setAttribute(k, v === true ? '' : v);
  }
  el.append(...kids.flat().filter((k) => k != null && k !== false));
  return el;
}

export function debounce(fn, ms) {
  let t;
  return (...a) => { clearTimeout(t); t = setTimeout(() => fn(...a), ms); };
}

export const fmtBytes = (n) => {
  if (n == null) return '';
  const u = ['B', 'KB', 'MB', 'GB', 'TB'];
  let i = 0;
  while (n >= 1024 && i < u.length - 1) { n /= 1024; i++; }
  return `${n >= 100 || i === 0 ? Math.round(n) : n.toFixed(1)} ${u[i]}`;
};

// Not cached on failure, so connectors that appear later (or a flaky first call) are picked up.
let connectors;
export function getConnectors() {
  connectors ??= getJSON('/api/connectors').catch(() => { connectors = null; return []; });
  return connectors;
}

export function gotoSearch(prefill) {
  emit('search:prefill', prefill);
  emit('nav', { tab: 'search' });
}

// Remote/user-supplied URLs: only ever link http(s) (never javascript:, data:, ...).
export const safeUrl = (u) => (/^https?:\/\//i.test(u || '') ? u : null);

export const ext = (url, label) => h('a', { class: 'btn small', href: safeUrl(url), target: '_blank', rel: 'noopener noreferrer' }, label, ' ↗');
