// node tests/data/viewer/style.test.mjs  -- pure-helper tests for web/js/viewer-style.js
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { ColorType } from '../../../node_modules/ol/expr/expression.js';
import { newCompilationContext } from '../../../node_modules/ol/expr/gpu.js';
import { expressionToGlsl } from '../../../node_modules/ol/render/webgl/compileUtil.js';
import { NumberType } from '../../../node_modules/ol/expr/expression.js';
import { buildStyle, foldColormap, lerpKnots, windowFromExtent, equalizeKnots, imageGeometry, logKnots, pyramidResolutions, histAxis } from '../../../web/js/viewer-style.js';
import { defaultSettings } from '../../../web/js/render.js';

const colormaps = JSON.parse(readFileSync('web/colormaps.json', 'utf8')).maps;
const stats = (b) => ({ bands: [1, 2, 3].map((band) => ({ band, min: 0, max: 100, mean: 50, std: 10,
  percentiles: { 0.1: 1, 0.5: 2, 1: 3, 2: 4, 5: 5, 95: 95, 98: 96, 99: 97, 99.5: 98, 99.9: 99 } })) });
const compile = (style, nb) => {
  const ctx = { ...newCompilationContext(), bandCount: nb + 1 };
  const glsl = expressionToGlsl(ctx, style.color, ColorType);
  for (const name of ctx.variables.keys()) assert.ok(name in style.variables, `missing var ${name}`);
  assert.ok(!/\de[-+]?\d/.test(glsl), 'exponent notation in GLSL');
  return glsl;
};

// geometry
const g = imageGeometry({ width: 8000, height: 4000 });
assert.equal(g.maxZoom, 5); assert.deepEqual(g.extent, [0, -4000, 8000, 0]);
assert.equal(imageGeometry({ width: 200, height: 100 }).maxZoom, 0);
assert.deepEqual(pyramidResolutions(2, 2), [4, 2, 1, 0.5, 0.25]);

// log knots hit the exact curve at nodes and stay close in between
const knots = logKnots();
for (const [x, y] of knots) assert.ok(Math.abs(Math.log(1 + 255 * x) / Math.log(256) - y) < 1e-4);
let worst = 0;
for (let t = 0; t <= 1; t += 0.001) {
  const i = knots.findIndex(([x]) => x >= t), [x1, y1] = knots[Math.max(i, 1)], [x0, y0] = knots[Math.max(i, 1) - 1];
  worst = Math.max(worst, Math.abs(y0 + (y1 - y0) * (t - x0) / (x1 - x0) - Math.log(1 + 255 * t) / Math.log(256)));
}
assert.ok(worst * 255 < 0.3, `log knots error ${worst * 255} LSB`);

// equalize knots: ascending, ties merged, flat -> null
const q = Array.from({ length: 33 }, (_, k) => k * 2);
const ek = equalizeKnots(q);
assert.equal(ek.length, 33); assert.equal(ek[0][1], 0); assert.equal(ek[32][1], 1);
assert.equal(equalizeKnots(Array(33).fill(5)), null);
const tied = equalizeKnots([...Array(10).fill(0), ...Array.from({ length: 23 }, (_, k) => 1 + k)]);
assert.ok(tied.every((k, i) => i === 0 || k[0] > tied[i - 1][0]));
assert.ok(equalizeKnots(q.map((x) => x * 1e-9)).length === 33, 'range-normalised so tiny units survive');

// styles compile to GLSL for every mode
const info = { dtype: 'uint16', count: 3, colorinterp: ['red', 'green', 'blue'] };
for (const stretch of ['linear', 'log', 'equalize']) {
  for (const [bands, colormap] of [[[1], 'gray'], [[1], 'viridis'], [[1, 2, 3], 'gray']]) {
    const s = { ...defaultSettings(info, stats()), bands, stretch, colormap, min: [0, 0, 0], max: [1, 1, 1] };
    compile(buildStyle(s, colormaps, bands.map(() => ({ quantiles: q }))), bands.length);
  }
}
const s0 = defaultSettings(info, stats());
assert.deepEqual(s0.bands, [1, 2, 3]);
assert.equal(buildStyle({ ...s0, invert: true }, colormaps).variables.invert, 1);
assert.deepEqual(histAxis({ min: 5, max: 10 }, 0, 20), [0, 20]);
// Black no-data must survive the adjustments: every one is an expression of the alpha band (neutral where alpha is 0)
{
  const style = buildStyle({ ...s0, nodata: 'black', brightness: 0.3 }, colormaps);
  const ctx = { ...newCompilationContext(), bandCount: 4 };
  for (const k of ['gamma', 'brightness', 'contrast', 'exposure', 'saturation']) {
    assert.match(expressionToGlsl(ctx, style[k], NumberType), /getBandValue\(4/, `${k} must depend on the alpha band`);
  }
}
// pixel window of a map extent: unrotated affine, clipped to the image, null when disjoint or rotated
assert.deepEqual(windowFromExtent([10.5, -50, 700, -3.2], [1, 0, 0, 0, -1, 0], 600, 40), [10, 3, 600, 40]);
assert.deepEqual(windowFromExtent([100, 40, 200, 80], [10, 0, 0, 0, -10, 100], 50, 50), [10, 2, 20, 6]); // north-up geotransform
assert.equal(windowFromExtent([700, -50, 800, -3], [1, 0, 0, 0, -1, 0], 600, 40), null);
assert.equal(windowFromExtent([0, 0, 5, 5], [1, 0.1, 0, 0, -1, 0], 600, 40), null);
// A colormap on a log/equalize stretch is folded into one curve: exact, and the GLSL stays small
// (nesting them made a ~1 MB shader that lost the WebGL context)
for (const invert of [false, true]) {
  const curve = logKnots();
  const folded = foldColormap(curve, colormaps.viridis, invert);
  for (let x = 0; x <= 1; x += 1 / 997) {
    const y = lerpKnots(curve, x)[0];
    const want = lerpKnots(colormaps.viridis, invert ? 1 - y : y), got = lerpKnots(folded, x);
    want.forEach((w, c) => assert.ok(Math.abs(w - got[c]) < 0.05, `folded viridis differs at x=${x}: ${w} vs ${got[c]}`));
  }
}
for (const stretch of ['log', 'equalize']) {
  const q = Array.from({ length: 33 }, (_, k) => k * k);
  const sOne = { ...defaultSettings({ count: 1, dtype: 'uint16', colorinterp: ['gray'] }, stats()), stretch, colormap: 'viridis' };
  const glsl = compile(buildStyle(sOne, colormaps, [{ quantiles: q }]), 1);
  assert.ok(glsl.length < 100000, `${stretch} + colormap GLSL is ${glsl.length} chars`);
}
console.log('viewer-style: all tests passed');
