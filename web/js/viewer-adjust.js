// Right panel: band mode, stretch + histograms, tonal sliders, colormap, resampling.
import { PRESETS, coreShare, getColormaps } from './render.js';
import { createHistogram } from './viewer-hist.js';
import { h, select, slider, fmt, section } from './viewer-ui.js';

const STRETCH = [['linear', 'Linear'], ['equalize', 'Histogram equalize'], ['log', 'Logarithmic']];
const DOWN = [['average', 'Average (box)'], ['nearest', 'Nearest (decimate)'], ['bilinear', 'Bilinear'], ['cubic', 'Cubic'],
  ['cubic_spline', 'Cubic spline'], ['lanczos', 'Lanczos'], ['mode', 'Mode'], ['rms', 'RMS']];
const UP = [['nearest', 'Nearest (true pixels)'], ['bilinear', 'Bilinear'], ['cubic', 'Cubic'], ['cubic_spline', 'Cubic spline'], ['lanczos', 'Lanczos']];
const SLIDERS = [['gamma', 'Gamma', 0.1, 5, 0.01, true], ['brightness', 'Brightness', -1, 1, 0.01], ['contrast', 'Contrast', -1, 1, 0.01],
  ['exposure', 'Exposure', -1, 1, 0.01], ['saturation', 'Saturation', -1, 1, 0.01]];
const RGB_NM = [640, 550, 470];
const OUTLIER_PRESETS = new Set(['minmax', 'dtype', 'sigma2', 'sigma3']);  // ranges a few extreme values can blow up

// Natural-colour band triple: the bands nearest red/green/blue wavelengths when known, else 1,2,3.
export function pickRgb(info) {
  const wl = info.wavelengths || [];
  if (info.count < 3) return [1];
  if (wl.filter((w) => w != null).length < 3) return [1, 2, 3];
  return RGB_NM.map((t) => 1 + wl.reduce((best, w, i) => (w != null && (wl[best] == null || Math.abs(w - t) < Math.abs(wl[best] - t)) ? i : best), 0));
}

export const bandLabel = (info, b) => `${b}: ${info.band_names?.[b - 1] ?? 'Band'}${info.wavelengths?.[b - 1] != null ? ` · ${fmt(info.wavelengths[b - 1], 5)} nm` : ''}`;

// api: {change(patch, {live}), reset(), setExact(bool)}
export function createAdjust(host, api) {
  let info, ui = null;
  const gradient = (stops) => `linear-gradient(90deg, ${stops.filter((_, i) => i % 4 === 0).map((c) => `rgb(${c[1]},${c[2]},${c[3]}) ${c[0] * 100}%`).join(',')})`;
  const check = (input, text, hint) => h('label', { class: 'vw-check', title: hint }, input, text);

  async function build(status) {
    const colormaps = await getColormaps();
    info = status.info;
    ui = { hists: [], mins: [], maxs: [], sliders: {}, histBands: 0, built: status.id };
    const bandOptions = Array.from({ length: info.count }, (_, i) => [i + 1, bandLabel(info, i + 1)]);
    ui.mode = select([['gray', 'Single band'], ['rgb', 'RGB composite']], 'gray', (m) => api.change({ bands: m === 'rgb' ? pickRgb(info) : [1] }), { disabled: info.count < 3 });
    ui.bandSel = [0, 1, 2].map((i) => select(bandOptions, 1, (v) => api.change({ bands: ui.cur.bands.map((b, j) => (j === i ? Number(v) : b)) })));
    ui.preset = select(Object.entries(PRESETS), 'p05', (preset) => api.change({ preset }), { title: 'How the display range is chosen from the statistics' });
    ui.stretch = select(STRETCH, 'linear', (stretch) => api.change({ stretch }), { title: 'How values inside the range map to brightness' });
    ui.warn = h('div', { class: 'vw-warn', hidden: true, role: 'status' });
    ui.histBox = h('div', { class: 'vw-hists' });
    for (const [key, label, min, max, step, log] of SLIDERS) {
      ui.sliders[key] = slider({ label, min, max, step, log, value: key === 'gamma' ? 1 : 0, onInput: (v) => api.change({ [key]: v }, { live: true }) });
    }
    ui.invert = h('input', { type: 'checkbox', onchange: () => api.change({ invert: ui.invert.checked }) });
    ui.gray = h('input', { type: 'checkbox', onchange: () => api.change({ grayscale: ui.gray.checked }) });
    ui.cmaps = h('div', { class: 'vw-cmaps' }, Object.entries(colormaps).map(([name, stops]) =>
      h('button', { class: 'vw-cmap', title: name, 'data-name': name, onclick: () => api.change({ colormap: name }) },
        h('i', { style: `background:${gradient(stops)}` }), h('span', {}, name))));
    ui.down = select(DOWN, 'average', (resampling) => api.change({ resampling }));
    ui.up = select(UP, 'nearest', (magnify) => api.change({ magnify }));
    ui.smooth = h('input', { type: 'checkbox', onchange: () => api.change({ interpolate: ui.smooth.checked }) });
    ui.nodata = select([['transparent', 'Transparent'], ['black', 'Black']], 'transparent', (nodata) => api.change({ nodata }));
    ui.exact = h('input', { type: 'checkbox', onchange: () => api.setExact(ui.exact.checked) });
    ui.bands = section('bands', 'Bands', true, ui.mode, h('div', { class: 'vw-bandsels' }, ui.bandSel));
    ui.bands.hidden = info.count < 2;  // one band: nothing to choose
    ui.single = section('colormap', 'Colormap', true, ui.cmaps);
    host.replaceChildren(
      h('div', { class: 'vw-adjhead' }, h('b', { class: 'grow' }, 'Adjustments'),
        h('button', { class: 'btn small', title: 'Reset every adjustment (r)', onclick: () => api.reset() }, 'Reset')),
      ui.bands,
      section('stretch', 'Stretch', true, h('div', { class: 'row nowrap' }, ui.preset, ui.stretch), ui.warn, ui.histBox,
        check(ui.exact, 'Exact statistics (full scan)', 'Scans every pixel (minutes on huge files). Makes the percentile and σ ranges exact; '
          + 'Min – max can only get wider, as rare extreme values are found too.')),
      section('tone', 'Tone', true, Object.values(ui.sliders).map((s) => s.el),
        h('div', { class: 'row' }, check(ui.invert, 'Invert (i)'), check(ui.gray, 'Black & white (g)'))),
      ui.single,
      section('display', 'Display', false,
        h('label', { class: 'vw-field' }, h('span', {}, 'Zoom out'), ui.down),
        h('label', { class: 'vw-field' }, h('span', {}, 'Zoom in'), ui.up),
        check(ui.smooth, 'Smooth interpolation (GPU)', 'Bilinear filtering of the raw values between pixels; off shows true pixels'),
        h('label', { class: 'vw-field' }, h('span', {}, 'No-data'), ui.nodata)),
    );
  }

  // One histogram block per displayed band; rebuilt only when the band count changes.
  function buildHists(s) {
    ui.hists = []; ui.mins = []; ui.maxs = [];
    ui.histBox.replaceChildren(...s.bands.map((b, i) => {
      const range = (lo, hi, live) => {
        const min = [...ui.cur.min], max = [...ui.cur.max];
        min[i] = lo; max[i] = hi;
        api.change({ min, max, preset: 'manual' }, { live });
      };
      const hist = createHistogram(range);
      const sc = info.scales?.[b - 1] ?? 1, off = info.offsets?.[b - 1] ?? 0;
      const edit = (isMin) => h('input', {
        type: 'number', class: 'input vw-num', step: 'any', title: isMin ? 'Lowest value shown (black)' : 'Highest value shown (white)',
        onchange: (e) => {
          const v = (Number(e.target.value) - off) / sc;
          if (Number.isFinite(v)) range(isMin ? v : ui.cur.min[i], isMin ? ui.cur.max[i] : v, false);
        },
      });
      const lo = edit(true), hi = edit(false);
      const log = h('input', { type: 'checkbox', onchange: () => hist.setLog(log.checked) });
      ui.hists.push(hist); ui.mins.push(lo); ui.maxs.push(hi);
      return h('div', { class: 'vw-histblk' },
        s.bands.length > 1 ? h('div', { class: 'muted' }, `${['R', 'G', 'B'][i]} · ${bandLabel(info, b)}`) : null,
        hist.el,
        h('div', { class: 'row vw-minmax' }, lo, '–', hi),
        h('div', { class: 'row' }, h('label', { class: 'vw-check vw-small', title: 'Bar heights on a log scale: shows rare values' }, log, 'log counts'),
          sc !== 1 || off !== 0 ? h('span', { class: 'muted vw-small' }, `scaled = raw × ${fmt(sc)} + ${fmt(off)}${info.units?.[b - 1] ? ` (${info.units[b - 1]})` : ''}`) : null));
    }));
    ui.histBands = s.bands.length;
  }

  // Warn when most pixels sit in a sliver of the range (the usual reason for a black or flat picture): a few extreme values
  // set it (Min – max, σ), or the data uses little of its data type (full data-type range).
  function warn(s, getStats) {
    const share = OUTLIER_PRESETS.has(s.preset) ? Math.min(...s.bands.map((b, i) => coreShare(getStats(b), s.min[i], s.max[i]))) : 1;
    ui.warn.hidden = share >= 0.1;
    if (ui.warn.hidden) return;
    const pct = share < 0.01 ? 'under 1' : Math.round(share * 100);
    const text = s.preset === 'dtype'
      ? `Most pixels use only ${pct} % of the full data-type range, so the picture looks dark or flat.`
      : `Most pixels use only ${pct} % of this range: a few extreme values (fill values, hot or saturated pixels) set it, `
        + 'so the picture looks dark or flat.';
    if (ui.warnText === text) return;  // unchanged: keep the element (screen readers announce changes only)
    ui.warnText = text;
    ui.warn.replaceChildren(h('span', {}, text),
      h('button', { class: 'btn small primary', onclick: () => api.change({ preset: 'p05' }) }, 'Use 0.5 – 99.5 %'));
  }

  // Push settings into the controls without rebuilding (safe while dragging).
  function sync(s, getStats) {
    ui.cur = s;
    if (ui.histBands !== s.bands.length) buildHists(s);
    ui.mode.value = s.bands.length === 3 ? 'rgb' : 'gray';
    ui.bandSel.forEach((sel, i) => { sel.value = s.bands[i] ?? 1; sel.hidden = i >= s.bands.length; });
    ui.preset.value = s.preset;
    ui.stretch.value = s.stretch;
    for (const [key] of SLIDERS) ui.sliders[key].set(s[key]);
    ui.invert.checked = s.invert;
    ui.gray.checked = s.grayscale;
    ui.down.value = s.resampling; ui.up.value = s.magnify; ui.smooth.checked = s.interpolate; ui.nodata.value = s.nodata;
    ui.single.hidden = s.bands.length !== 1;
    ui.cmaps.querySelectorAll('.vw-cmap').forEach((b) => b.classList.toggle('active', b.dataset.name === s.colormap));
    s.bands.forEach((b, i) => {
      const sc = info.scales?.[b - 1] ?? 1, off = info.offsets?.[b - 1] ?? 0;
      ui.hists[i].set(getStats(b), s.min[i], s.max[i]);
      for (const [input, v] of [[ui.mins[i], s.min[i]], [ui.maxs[i], s.max[i]]]) {
        if (document.activeElement !== input) input.value = fmt(v * sc + off, 8);
      }
    });
    warn(s, getStats);
  }

  return {
    async show(status, settings, getStats, exact) {
      if (ui?.built !== status.id) await build(status);
      sync(settings, getStats);
      ui.exact.checked = exact;
    },
    sync(settings, getStats) { if (ui) sync(settings, getStats); },
    clear() { ui = null; host.replaceChildren(h('p', { class: 'muted pad' }, 'Open a dataset to adjust it.')); },
  };
}
