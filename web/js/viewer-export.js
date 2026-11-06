// Export dialog: save what you see (rendered PNG/JPEG/GeoTIFF) or the raw data (GeoTIFF) via GET /api/datasets/{id}/export.
import { qs } from './api.js';
import { renderParams } from './render.js';
import { h, select, fmt } from './viewer-ui.js';

const LIMIT_MP = 400; // server refuses larger outputs (413)
const BYTES = { uint8: 1, int8: 1, uint16: 2, int16: 2, uint32: 4, int32: 4, float32: 4, float64: 8 };
const SCALES = [[1, '100 %'], [0.5, '50 %'], [0.25, '25 %'], [0.125, '12.5 %'], [0.0625, '6.25 %']];

// ctx(): {status, settings, viewWindow: [c0, r0, c1, r1] | null}
export function createExport(ctx) {
  const dlg = h('dialog', { class: 'dlg vw-export' });
  const f = { kind: 'rendered', format: 'png', scale: 1, win: 'full', bands: 'all' };
  const est = h('div', { class: 'vw-small' });
  const go = h('a', { class: 'btn primary', role: 'button', download: '' }, 'Download');

  function sizes(c) {
    const info = c.status.info;
    const win = f.win === 'view' && c.viewWindow ? c.viewWindow : [0, 0, info.width, info.height];
    const w = Math.max(1, Math.round((win[2] - win[0]) * f.scale)), hh = Math.max(1, Math.round((win[3] - win[1]) * f.scale));
    const nb = f.bands === 'all' ? info.count : c.settings.bands.length;
    const bytes = f.kind === 'raw' ? w * hh * nb * Math.max(...(info.dtypes ?? [info.dtype]).map((d) => BYTES[d] ?? 4)) : w * hh * (f.format === 'jpg' ? 3 : 4);
    return { win, w, h: hh, mp: (w * hh) / 1e6, planes: f.kind === 'raw' ? nb : 1, bytes };
  }

  function refresh() {
    const c = ctx();
    const { win, w, h: hh, mp, planes, bytes } = sizes(c);
    const tooBig = mp * planes > LIMIT_MP; // raw cubes count every band, like the server
    const fit = Math.sqrt(LIMIT_MP / ((win[2] - win[0]) * (win[3] - win[1]) * planes / 1e6));
    est.className = `vw-small${tooBig ? ' error' : ''}`;
    est.textContent = `${w} × ${hh} px${planes > 1 ? ` × ${planes} bands` : ''} · ${fmt(mp, 4)} MP · up to ${fmt(bytes / 1048576, 3)} MB uncompressed`
      + (tooBig ? ` — over the ${LIMIT_MP} million sample limit; use a scale of ${fmt(Math.floor(fit * 1000) / 1000, 3)} or less, the current view${planes > 1 ? ' or the displayed bands only' : ''}.` : '');
    const query = {
      kind: f.kind, format: f.format, scale: f.scale, window: win.join(','),
      ...(f.kind === 'rendered' ? renderParams(c.settings) : { bands: f.bands === 'all' ? undefined : c.settings.bands }),
      ...(f.kind === 'raw' ? { resampling: c.settings.resampling } : {}),
    };
    go.href = `/api/datasets/${c.status.id}/export${qs(query)}`;
    go.classList.toggle('disabled', tooBig);
    go.setAttribute('aria-disabled', String(tooBig));
  }

  function build() {
    const c = ctx();
    if (f.kind === 'raw') f.format = 'tif';
    const row = (label, el) => h('label', { class: 'vw-field' }, h('span', {}, label), el);
    const opt = (key, options, onchange) => select(options, f[key], (v) => { f[key] = typeof f[key] === 'number' ? Number(v) : v; onchange?.(); refresh(); });
    const formats = f.kind === 'raw' ? [['tif', 'GeoTIFF (original values)']] : [['png', 'PNG'], ['jpg', 'JPEG'], ['tif', 'GeoTIFF (RGBA)']];
    dlg.replaceChildren(
      h('h3', {}, `Export ${c.status.name}`),
      h('div', { class: 'col', style: 'gap:8px' },
        row('What', opt('kind', [['rendered', 'Rendered image (current stretch and colours)'], ['raw', 'Raw data (original values)']], () => { f.format = f.kind === 'raw' ? 'tif' : 'png'; build(); })),
        row('Format', opt('format', formats)),
        row('Area', opt('win', [['full', 'Full image'], ...(c.viewWindow ? [['view', 'Current view']] : [])])),
        row('Scale', opt('scale', SCALES)),
        f.kind === 'raw' && c.status.info.count > 1 ? row('Bands', opt('bands', [['all', `All ${c.status.info.count}`], ['shown', 'Displayed bands only']])) : null,
        est,
        h('div', { class: 'muted vw-small' }, f.kind === 'rendered'
          ? 'Uses the current band, stretch, tone, colormap and no-data settings. Downsampling uses the "Zoom out" method; never upsampled.'
          : 'Keeps data type, no-data and georeferencing. Downsampling uses the "Zoom out" method; never upsampled.')),
      h('div', { class: 'row end', style: 'margin-top:12px' },
        h('button', { class: 'btn', onclick: () => dlg.close() }, 'Close'),
        go));
    go.onclick = (e) => { if (go.classList.contains('disabled')) e.preventDefault(); };
    refresh();
  }

  return { open() { f.win = 'full'; build(); if (!dlg.open) dlg.showModal(); }, el: dlg };
}
