// Pure helpers for the viewer: OpenLayers WebGL style for CONTRACTS.md section 6, pixel tile-grid math.
// No DOM / no `ol` global here, so node can import and test this file.
import { effectiveSaturation } from './render.js';

export const TILE = 256;

// Pixel-space pyramid (CONTRACTS.md section 5): z == maxZoom is 1 tile px == 1 image px.
export function imageGeometry(info) {
  const { width, height } = info;
  const maxZoom = info.tiles?.max_zoom ?? Math.max(0, Math.ceil(Math.log2(Math.max(width, height) / TILE)));
  return { width, height, maxZoom, overzoom: info.tiles?.overzoom ?? 0, extent: [0, -height, width, 0] };
}

// Tile-grid resolutions, coarsest first; `levels` includes server overzoom levels.
export const pyramidResolutions = (maxZoom, extraLevels) =>
  Array.from({ length: maxZoom + extraLevels + 1 }, (_, i) => 2 ** (maxZoom - i));

// Piecewise-linear knots (domain 0..1 -> 0..1) of t = ln(1 + 255 t) / ln 256, geometrically spaced so the
// linear segments stay within ~0.1/255 of the exact curve. GLSL via OL has no log().
const LOG_KNOTS = 48;
export const logKnots = () => Array.from({ length: LOG_KNOTS + 1 }, (_, i) =>
  [i === 0 ? 0 : round6((256 ** (i / LOG_KNOTS) - 1) / 255), i / LOG_KNOTS]);

// Numbers go through JS toString into GLSL, which breaks on exponent notation; keep stops in plain decimals.
const round6 = (x) => Math.round(x * 1e6) / 1e6;

// Equalize knots from /stats quantiles (33 values at k/32) normalised to 0..1 over their own range.
// Ties collapse to one knot at the mean rank; returns null when the band is flat.
export function equalizeKnots(quantiles) {
  const lo = quantiles[0];
  const range = quantiles[quantiles.length - 1] - lo;
  if (!(range > 0)) return null;
  const last = quantiles.length - 1;
  const groups = new Map();
  quantiles.forEach((q, k) => {
    const x = round6((q - lo) / range);
    (groups.get(x) ?? groups.set(x, []).get(x)).push(k / last);
  });
  const knots = [...groups].map(([x, ys]) => [x, ys.reduce((a, b) => a + b) / ys.length]).sort((a, b) => a[0] - b[0]);
  return knots.length > 1 ? knots : null;
}

const interp = (input, knots, out = (y) => y) => ['interpolate', ['linear'], input, ...knots.flatMap(([x, y]) => [x, out(y)])];

// Per displayed band equalize knots (from /stats quantiles), or [] when not equalizing.
export const eqKnotsFor = (s, bandStats) =>
  s.stretch === 'equalize' ? s.bands.map((_, i) => bandStats[i]?.quantiles ? equalizeKnots(bandStats[i].quantiles) : null) : [];

// colormaps: {name: [[pos, r, g, b], ...]}; bandStats: /stats entries for the displayed bands (equalize needs them).
export function buildStyle(s, colormaps, bandStats = []) {
  const nb = s.bands.length;
  const eq = eqKnotsFor(s, bandStats);
  const v = (name) => ['var', name];
  // t = curve(x): x is the linearly stretched value (or the equalize rank input), curve a piecewise-linear knot list or null
  const curve = (i) => (s.stretch === 'equalize' ? eq[i] : s.stretch === 'log' ? logKnots() : null);
  const xExpr = (i) => {
    const band = ['band', i + 1];
    if (s.stretch === 'equalize' && eq[i]) return ['/', ['-', band, v(`eqlo${i}`)], v(`eqrange${i}`)];
    return ['case', ['!=', v(`max${i}`), v(`min${i}`)],
      ['clamp', ['/', ['-', band, v(`min${i}`)], ['-', v(`max${i}`), v(`min${i}`)]], 0, 1], 0];
  };
  const tExpr = (i) => {
    const t = curve(i) ? interp(xExpr(i), curve(i)) : xExpr(i);
    return ['+', v('invert'), ['*', ['-', 1, ['*', 2, v('invert')]], t]];
  };
  const alpha = ['band', nb + 1];
  const keep = ['+', ['*', v('black'), alpha], ['-', 1, v('black')]]; // black nodata: fade colour instead of alpha
  const calm = (name, neutral = 0) => ['+', neutral, ['*', ['-', v(name), neutral], keep]];
  let rgb;
  if (nb === 3) rgb = [0, 1, 2].map(tExpr);
  else if (s.colormap && s.colormap !== 'gray' && colormaps[s.colormap]) {
    const stops = colormaps[s.colormap];
    if (curve(0)) { // nesting the colormap in the curve would square the GLSL (log + viridis ~1 MB), so fold them into one curve
      const knots = foldColormap(curve(0), stops, s.invert);
      rgb = [1, 2, 3].map((c) => interp(xExpr(0), knots.map((k) => [k[0], k[c]]), (y) => y / 255));
    } else {
      const t = tExpr(0);
      rgb = [1, 2, 3].map((c) => interp(t, stops.map((st) => [st[0], st[c]]), (y) => y / 255));
    }
  } else rgb = [tExpr(0), tExpr(0), tExpr(0)];
  return {
    color: ['array', ...rgb.map((c) => ['*', c, keep]), ['+', ['*', alpha, ['-', 1, v('black')]], v('black')]],
    // black nodata must stay black: pull every adjustment toward its neutral value where `keep` is 0
    gamma: calm('gamma', 1), brightness: calm('brightness'), contrast: calm('contrast'),
    exposure: calm('exposure'), saturation: calm('saturation'),
    variables: styleVariables(s, bandStats),
  };
}

// Colormap(optionally inverted curve(x)) as one piecewise-linear function of x: [[x, r, g, b]] (r, g, b in 0..255).
// Breakpoints are the curve's knots plus the x where the curve reaches each colormap stop, so it is exact.
export function foldColormap(curveKnots, stops, invert) {
  const out = (u) => (invert ? 1 - u : u);
  const f = (x) => lerpKnots(curveKnots, x)[0];
  const xs = new Set(curveKnots.map(([x]) => x));
  for (const [p] of stops) {
    const y = out(p);
    for (let i = 0; i < curveKnots.length - 1; i += 1) {
      const [x0, y0] = curveKnots[i], [x1, y1] = curveKnots[i + 1];
      if (y1 !== y0 && y >= Math.min(y0, y1) && y <= Math.max(y0, y1)) xs.add(round6(x0 + ((y - y0) / (y1 - y0)) * (x1 - x0)));
    }
  }
  return [...xs].sort((p, q) => p - q).map((x) => [x, ...lerpKnots(stops, out(f(x)))]);
}

// Piecewise-linear value(s) at x of sorted knots [[x, ...ys]], clamped at both ends.
export function lerpKnots(knots, x) {
  const i = knots.findIndex((k) => k[0] >= x);
  if (i < 0) return knots.at(-1).slice(1);
  if (i === 0) return knots[0].slice(1);
  const [x0, ...ya] = knots[i - 1], [x1, ...yb] = knots[i];
  return ya.map((y, c) => y + ((x - x0) / (x1 - x0)) * (yb[c] - y));
}

// Anything that changes the GLSL (not just uniforms) goes here, so callers know when setStyle is needed.
export const styleKey = (s, bandStats = []) => JSON.stringify([s.bands.length, s.stretch,
  s.bands.length === 1 && s.colormap, eqKnotsFor(s, bandStats),
  s.bands.length === 1 && s.stretch !== 'linear' && s.colormap !== 'gray' && s.invert]); // invert is folded into the GLSL here

// eq vars need the raw quantile lo/range: pass bandStats entries (or null) per displayed band.
export function styleVariables(s, bandStats = []) {
  const vars = {
    gamma: s.gamma, brightness: s.brightness, contrast: s.contrast, exposure: s.exposure,
    saturation: effectiveSaturation(s), invert: s.invert ? 1 : 0, black: s.nodata === 'black' ? 1 : 0,
  };
  s.bands.forEach((_, i) => {
    vars[`min${i}`] = Number.isFinite(s.min[i]) ? s.min[i] : 0;
    vars[`max${i}`] = Number.isFinite(s.max[i]) ? s.max[i] : 1;
    const q = bandStats[i]?.quantiles;
    vars[`eqlo${i}`] = q ? q[0] : 0;
    vars[`eqrange${i}`] = q ? q[q.length - 1] - q[0] || 1 : 1;
  });
  return vars;
}

// Histogram axis: covers the data histogram and the current min/max handles.
export function histAxis(hist, lo, hi) {
  const a = Math.min(hist.min, lo, hi), b = Math.max(hist.max, lo, hi);
  return b > a ? [a, b] : [a, a + 1];
}
export const valueToFrac = (v, [a, b]) => (v - a) / (b - a);
export const fracToValue = (f, [a, b]) => a + f * (b - a);

// Pixel window [col0, row0, col1, row1] covered by a map extent for an unrotated affine transform, clipped to the image; null if empty.
export function windowFromExtent([x0, y0, x1, y1], [a, b, c, d, e, f], width, height) {
  if (b || d || !a || !e) return null;
  const cols = [(x0 - c) / a, (x1 - c) / a], rows = [(y0 - f) / e, (y1 - f) / e];
  const win = [Math.max(0, Math.floor(Math.min(...cols))), Math.max(0, Math.floor(Math.min(...rows))),
    Math.min(width, Math.ceil(Math.max(...cols))), Math.min(height, Math.ceil(Math.max(...rows)))];
  return win[2] > win[0] && win[3] > win[1] ? win : null;
}
