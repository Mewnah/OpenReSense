// Draggable divider before the last column of a grid (e.g. the Search map). The pane's width is kept as a share of
// the space it and its neighbour have, so it follows window resizes. Drag, arrow keys (Shift: bigger steps), Home/End
// or double-click (back to the default) change it; the share is remembered.
const STORE = 'openresense.split.';

export function createSplit(handle, { grid, other, pane, prop, key, def = 0.45, minPane = 240, minOther = 260, onResize }) {
  let frac = def;
  try { const v = Number(localStorage.getItem(STORE + key)); if (v > 0 && v < 1) frac = v; } catch { /* storage off */ }
  const save = () => { try { localStorage.setItem(STORE + key, String(frac)); } catch { /* quota / private mode */ } };
  const avail = () => other.getBoundingClientRect().width + pane.getBoundingClientRect().width;
  let raf = 0;

  function apply() {  // returns the share actually shown (the minimum widths can override frac)
    const a = avail();
    if (!a || !pane.offsetParent) return null;  // pane hidden (map turned off, or the narrow-window overlay)
    const w = Math.round(Math.min(Math.max(a * frac, minPane), Math.max(minPane, a - minOther)));
    grid.style.setProperty(prop, `${w}px`);
    handle.setAttribute('aria-valuenow', String(Math.round((w / a) * 100)));
    cancelAnimationFrame(raf);
    raf = requestAnimationFrame(() => onResize?.());
    return w / a;
  }
  const set = (f) => { frac = Math.min(Math.max(f, 0.05), 0.95); frac = apply() ?? frac; };

  handle.addEventListener('pointerdown', (e) => {
    if (e.button !== 0) return;
    e.preventDefault();
    handle.setPointerCapture(e.pointerId);
    grid.classList.add('dragging');
    const right = pane.getBoundingClientRect().right;
    const move = (ev) => set((right - ev.clientX - handle.offsetWidth / 2) / avail());
    const up = () => {
      handle.removeEventListener('pointermove', move);
      handle.removeEventListener('pointerup', up);
      handle.removeEventListener('pointercancel', up);
      grid.classList.remove('dragging');
      save();
    };
    handle.addEventListener('pointermove', move);
    handle.addEventListener('pointerup', up);
    handle.addEventListener('pointercancel', up);
  });
  handle.addEventListener('keydown', (e) => {
    const step = e.shiftKey ? 0.1 : 0.02;
    const now = pane.getBoundingClientRect().width / (avail() || 1);  // from what is shown, not a share too big to fit
    const f = { ArrowLeft: now + step, ArrowRight: now - step, Home: 0.95, End: 0.05 }[e.key];
    if (f === undefined) return;
    e.preventDefault();
    set(f);
    save();
  });
  handle.addEventListener('dblclick', () => { set(def); save(); });
  new ResizeObserver(apply).observe(grid);
  return { apply };
}
