// Render settings shared by the image/map views (OpenLayers WebGL style) and server-rendered PNGs
// (globe, exports). The per-pixel pipeline is CONTRACTS.md section 6; server/raster implements the same math.

export const PRESETS = {
  dtype: 'Full data-type range',
  minmax: 'Min – max',
  p01: '0.1 – 99.9 %',
  p05: '0.5 – 99.5 %',
  p1: '1 – 99 %',
  p2: '2 – 98 %',
  sigma2: 'Mean ± 2σ',
  sigma3: 'Mean ± 3σ',
  manual: 'Manual',
};

const DTYPE_RANGE = {
  uint8: [0, 255], int8: [-128, 127], uint16: [0, 65535], int16: [-32768, 32767],
  uint32: [0, 4294967295], int32: [-2147483648, 2147483647],
};

// bandStats: one entry of /stats "bands". Returns [min, max] or null for 'manual'.
export function presetRange(bandStats, preset, dtype) {
  const s = bandStats;
  const p = s.percentiles;
  switch (preset) {
    case 'dtype': return DTYPE_RANGE[dtype] ? [...DTYPE_RANGE[dtype]] : [s.min, s.max];
    case 'minmax': return [s.min, s.max];
    case 'p01': return [p['0.1'], p['99.9']];
    case 'p05': return [p['0.5'], p['99.5']];
    case 'p1': return [p['1'], p['99']];
    case 'p2': return [p['2'], p['98']];
    case 'sigma2': return [s.mean - 2 * s.std, s.mean + 2 * s.std];
    case 'sigma3': return [s.mean - 3 * s.std, s.mean + 3 * s.std];
    default: return null;
  }
}

// Fills s.min/s.max for s.bands from stats (must include those bands) according to s.preset.
export function applyPreset(s, stats, info) {
  s.bands.forEach((b, i) => {
    const bs = stats.bands.find((x) => x.band === b);
    const r = bs && presetRange(bs, s.preset, info.dtypes?.[b - 1] ?? info.dtype);
    if (r) [s.min[i], s.max[i]] = r;
  });
  s.min.length = s.max.length = s.bands.length;
  return s;
}

export function defaultSettings(info, stats) {
  const ci = info.colorinterp || [];
  const rgb = info.count >= 3 && ci[0] === 'red' && ci[1] === 'green' && ci[2] === 'blue';
  const s = {
    bands: rgb ? [1, 2, 3] : [1],
    stretch: 'linear', // 'linear' | 'equalize' | 'log'
    preset: info.dtype === 'uint8' ? 'dtype' : 'p05',
    min: [], max: [],
    gamma: 1, brightness: 0, contrast: 0, exposure: 0, saturation: 0,
    invert: false,
    grayscale: false, // B&W: forces saturation -1
    colormap: 'gray', // single-band only
    resampling: 'average', // server downsampling when zoomed out
    magnify: 'nearest', // server upsampling when zoomed past native resolution
    interpolate: false, // client texture filtering between zoom levels
    nodata: 'transparent', // or 'black'
  };
  return applyPreset(s, stats, info);
}

export const effectiveSaturation = (s) => (s.grayscale ? -1 : s.saturation);

// Query params for server-side rendering (/api/datasets/{id}/warp?format=png ...), see CONTRACTS.md 5-6.
export function renderParams(s) {
  return {
    bands: s.bands.join(','),
    stretch: s.stretch,
    min: s.min.join(','),
    max: s.max.join(','),
    gamma: s.gamma,
    brightness: s.brightness,
    contrast: s.contrast,
    exposure: s.exposure,
    saturation: effectiveSaturation(s),
    invert: s.invert ? 1 : 0,
    colormap: s.bands.length === 1 ? s.colormap : undefined,
    resampling: s.resampling,
    magnify: s.magnify,
    nodata: s.nodata,
  };
}

let colormaps;
// {name: [[pos, r, g, b], ...]} — 33 stops each, linear interpolation between stops.
export const getColormaps = () => (colormaps ??= fetch('/colormaps.json').then((r) => r.json()).then((j) => j.maps));
