// Right panel (bottom): pixel probe, metadata table, label text.
import { h, fmt } from './viewer-ui.js';

const W = 300, H = 90;

function spectrum(info, probe) {
  const canvas = h('canvas', { class: 'vw-spectrum', width: W * 2, height: H * 2 });
  canvas.style.width = `${W}px`; canvas.style.height = `${H}px`;
  const ctx = canvas.getContext('2d');
  ctx.scale(2, 2);
  const wl = info.wavelengths || [];
  const useWl = wl.filter((w) => w != null).length > 1;
  const pts = probe.scaled.map((y, i) => ({ x: useWl ? wl[i] : i + 1, y, bad: probe.nodata[i] })).filter((p) => p.x != null && Number.isFinite(p.y));
  const good = pts.filter((p) => !p.bad);
  if (!good.length) return canvas;
  const [x0, x1] = [Math.min(...pts.map((p) => p.x)), Math.max(...pts.map((p) => p.x))];
  const [y0, y1] = [Math.min(...good.map((p) => p.y)), Math.max(...good.map((p) => p.y))];
  const px = (x) => 28 + ((x - x0) / (x1 - x0 || 1)) * (W - 36), py = (y) => H - 16 - ((y - y0) / (y1 - y0 || 1)) * (H - 28);
  ctx.strokeStyle = '#5aa9ff'; ctx.fillStyle = '#5aa9ff'; ctx.beginPath();
  good.forEach((p, i) => (i ? ctx.lineTo(px(p.x), py(p.y)) : ctx.moveTo(px(p.x), py(p.y))));
  ctx.stroke();
  good.forEach((p) => ctx.fillRect(px(p.x) - 1.5, py(p.y) - 1.5, 3, 3));
  ctx.fillStyle = '#8a94a8'; ctx.font = '10px system-ui';
  ctx.fillText(fmt(y1, 4), 2, 10); ctx.fillText(fmt(y0, 4), 2, H - 16);
  ctx.fillText(`${fmt(x0, 5)}`, 28, H - 3); ctx.textAlign = 'right'; ctx.fillText(`${fmt(x1, 5)} ${useWl ? 'nm' : '(band)'}`, W - 4, H - 3);
  return canvas;
}

export function createInfo(host) {
  const probeBox = h('div', {});
  const metaBox = h('div', {});
  const labelBox = h('div', {});
  host.replaceChildren(
    h('section', { class: 'vw-sec' }, h('h4', {}, 'Pixel'), probeBox),
    h('section', { class: 'vw-sec' }, h('h4', {}, 'Dataset'), metaBox),
    labelBox,
  );
  const clearProbe = () => probeBox.replaceChildren(h('p', { class: 'muted vw-small' }, 'Click the image to probe a pixel.'));
  clearProbe();

  const row = (k, v, mono) => (v == null || v === '' ? null : h('tr', {}, h('th', {}, k), h('td', { class: mono ? 'mono' : '' }, v)));

  return {
    clearProbe,
    clear() { clearProbe(); metaBox.replaceChildren(); labelBox.replaceChildren(); },
    show(status) {
      const i = status.info, c = i.crs;
      const src = status.source?.url || status.source?.path;
      metaBox.replaceChildren(h('table', { class: 'vw-meta' },
        row('Name', status.name),
        row('Source', src ? h('a', { href: src.startsWith('http') ? src : undefined, title: src, target: '_blank', rel: 'noopener' }, src.length > 48 ? `…${src.slice(-46)}` : src) : null),
        row('Driver', i.driver + (i.converted ? ` (converted from ${i.converted.from})` : '')),
        row('Size', `${i.width} × ${i.height} px × ${i.count} band${i.count > 1 ? 's' : ''}`),
        row('Data type', i.dtypes && new Set(i.dtypes).size > 1 ? [...new Set(i.dtypes)].join(', ') : i.dtype),
        row('No-data', i.nodata?.some((n) => n != null) ? [...new Set(i.nodata)].join(', ') : 'none (mask only)'),
        row('CRS', c ? c.name : 'not georeferenced'),
        row('proj4', c?.proj4, true),
        row('Body', c?.body),
        row('Resolution', i.resolution && `${fmt(i.resolution[0])} × ${fmt(i.resolution[1])} ${c?.units ?? ''}/px`),
        row('Bounds', i.bounds && i.bounds.map((v) => fmt(v, 9)).join(', '), true),
        row('Lon/lat bounds', i.lonlat_bounds && `W ${fmt(i.lonlat_bounds[0], 7)}  S ${fmt(i.lonlat_bounds[1], 7)}  E ${fmt(i.lonlat_bounds[2], 7)}  N ${fmt(i.lonlat_bounds[3], 7)}`),
        row('Georeferencing', i.georef),
        row('Overviews', i.overviews?.length ? i.overviews.map((o) => `1/${o}`).join(' ') : null),
        row('Files', i.files?.length ? i.files.join('\n') : null, true)));
      labelBox.replaceChildren(...(i.label ? [h('details', { class: 'vw-sec' },
        h('summary', {}, 'Label / header'),
        h('button', { class: 'btn small', onclick: (e) => { navigator.clipboard.writeText(i.label); e.target.textContent = 'Copied'; setTimeout(() => (e.target.textContent = 'Copy'), 1200); } }, 'Copy'),
        h('pre', { class: 'vw-label' }, i.label))] : []));
    },
    // probe: /pixel response
    showProbe(probe, info) {
      if (!probe.inside) return probeBox.replaceChildren(h('p', { class: 'muted vw-small' }, 'Outside the dataset.'));
      const scaled = info.scales?.some((s) => s !== 1) || info.offsets?.some((o) => o !== 0);
      probeBox.replaceChildren(
        h('div', { class: 'vw-small' }, `col ${probe.col}, row ${probe.row}`, probe.lonlat ? ` · lon ${fmt(probe.lonlat[0], 8)}, lat ${fmt(probe.lonlat[1], 8)}` : ''),
        h('div', { class: 'vw-probe-scroll' }, h('table', { class: 'vw-meta' },
          h('tr', {}, h('th', {}, 'Band'), h('th', {}, 'Raw'), scaled ? h('th', {}, 'Scaled') : null),
          probe.values.map((v, b) => h('tr', {},
            h('td', {}, `${b + 1}${info.band_names?.[b] && info.band_names[b] !== `Band ${b + 1}` ? ` ${info.band_names[b]}` : ''}`),
            h('td', { class: 'mono' }, probe.nodata[b] ? 'no data' : fmt(v, 9)),
            scaled ? h('td', { class: 'mono' }, probe.nodata[b] ? '' : fmt(probe.scaled[b], 9)) : null)))),
        ...(info.count > 3 ? [spectrum(info, probe)] : []));
    },
  };
}
