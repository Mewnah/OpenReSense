// Interactive histogram with draggable min / max handles (values in raw units).
import { h } from './viewer-ui.js';
import { histAxis, valueToFrac, fracToValue } from './viewer-style.js';

const W = 300, H = 84, PAD = 6;

// onRange(lo, hi, done): live while dragging (done=false), once more on release (done=true).
export function createHistogram(onRange) {
  const canvas = h('canvas', { class: 'vw-hist', width: W * 2, height: H * 2 });
  canvas.style.width = `${W}px`; canvas.style.height = `${H}px`;
  const ctx = canvas.getContext('2d');
  ctx.scale(2, 2);
  let hist = null, lo = 0, hi = 1, log = false, axis = [0, 1], drag = null;

  const xOf = (v) => PAD + valueToFrac(v, axis) * (W - 2 * PAD);
  const valueAt = (clientX) => {  // the canvas may be drawn narrower than W (a narrow panel): scale to its drawn width
    const r = canvas.getBoundingClientRect();
    const f = ((clientX - r.left) * (W / (r.width || W)) - PAD) / (W - 2 * PAD);
    return fracToValue(Math.min(1, Math.max(0, f)), axis);
  };

  function draw() {
    ctx.clearRect(0, 0, W, H);
    if (!hist) return;
    const { counts } = hist, n = counts.length;
    const height = (c) => (log ? Math.log1p(c) : c);
    const top = Math.max(...counts.map(height)) || 1;
    const span = hist.max > hist.min ? (hist.max - hist.min) / n : 0;
    ctx.fillStyle = '#6b7a99';
    counts.forEach((c, i) => {
      const x0 = xOf(hist.min + i * span), x1 = span ? xOf(hist.min + (i + 1) * span) : x0 + 2;
      const bar = (height(c) / top) * (H - 14);
      ctx.fillRect(x0, H - 4 - bar, Math.max(1, x1 - x0 - 0.3), bar);
    });
    ctx.fillStyle = 'rgba(0,0,0,.45)'; // clipped ranges
    ctx.fillRect(0, 0, xOf(lo), H);
    ctx.fillRect(xOf(hi), 0, W - xOf(hi), H);
    for (const [v, color] of [[lo, '#5aa9ff'], [hi, '#ffb454']]) {
      ctx.fillStyle = ctx.strokeStyle = color;
      ctx.beginPath(); ctx.moveTo(xOf(v), 0); ctx.lineTo(xOf(v), H - 2); ctx.stroke();
      ctx.beginPath(); ctx.moveTo(xOf(v) - 5, 0); ctx.lineTo(xOf(v) + 5, 0); ctx.lineTo(xOf(v), 8); ctx.fill();
    }
  }

  canvas.addEventListener('pointerdown', (e) => {
    if (!hist) return;
    const v = valueAt(e.clientX);
    drag = Math.abs(v - lo) <= Math.abs(v - hi) ? 'lo' : 'hi';
    canvas.setPointerCapture(e.pointerId);
    move(e);
  });
  const move = (e) => {
    if (!drag) return;
    const v = valueAt(e.clientX);
    if (drag === 'lo') lo = Math.min(v, hi); else hi = Math.max(v, lo);
    draw();
    onRange(lo, hi, false);
  };
  canvas.addEventListener('pointermove', move);
  canvas.addEventListener('pointerup', () => { if (drag) { drag = null; onRange(lo, hi, true); } });

  return {
    el: canvas,
    // bandStats histograms + current range; the histogram and axis are frozen while dragging so handles don't run away.
    // The middle-of-the-data histogram (core) when the range lies near it: a few far-off values squeeze the full one.
    set(bandStats, newLo, newHi) {
      if (!drag) {
        lo = newLo; hi = newHi;
        const full = bandStats?.histogram ?? null, core = bandStats?.core;
        const span = core ? core.max - core.min : 0;
        hist = core && span > 0 && lo >= core.min - span && hi <= core.max + span ? core : full;
        if (hist) axis = histAxis(hist, lo, hi);
      }
      draw();
    },
    setLog(on) { log = on; draw(); },
  };
}
