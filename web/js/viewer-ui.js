// Tiny DOM helpers shared by the viewer panels.

// h('div', {class: 'x', onclick: fn, hidden: bool}, ...children)
export function h(tag, props = {}, ...kids) {
  const e = document.createElement(tag);
  for (const [k, v] of Object.entries(props)) {
    if (v == null || v === false) continue;
    if (k === 'class') e.className = v;
    else if (k.startsWith('on')) e.addEventListener(k.slice(2), v);
    else if (k in e && k !== 'list') e[k] = v;
    else e.setAttribute(k, v === true ? '' : v);
  }
  e.append(...kids.flat().filter((x) => x != null && x !== false));
  return e;
}

export const select = (options, value, onchange, props = {}) => {
  const s = h('select', { class: 'select', ...props, onchange: () => onchange(s.value) },
    options.map(([v, label]) => h('option', { value: v, selected: v === value }, label)));
  return s;
};

export const fmt = (v, digits = 6) => (v == null || Number.isNaN(v) ? '–' : Number(Number(v).toPrecision(digits)).toString());

export function download(blob, name) {
  const a = h('a', { href: URL.createObjectURL(blob), download: name });
  a.click();
  setTimeout(() => URL.revokeObjectURL(a.href), 5000);
}

// Labelled slider + number box. map: {to(p) -> value, from(value) -> p} for non-linear sliders (p in [0, 1]).
export function slider({ label, min, max, step, value, log, onInput }) {
  const map = log
    ? { to: (p) => Math.exp(Math.log(min) + p * (Math.log(max) - Math.log(min))), from: (v) => (Math.log(v) - Math.log(min)) / (Math.log(max) - Math.log(min)) }
    : { to: (p) => min + p * (max - min), from: (v) => (v - min) / (max - min) };
  const range = h('input', { type: 'range', min: 0, max: 1000, step: 1, class: 'vw-range',
    oninput: () => { const v = round(map.to(range.value / 1000)); num.value = v; onInput(v); } });
  const round = (v) => Number(v.toFixed(3));
  const num = h('input', { type: 'number', class: 'input vw-num', min, max, step,
    onchange: () => { const v = Math.min(max, Math.max(min, Number(num.value))); if (Number.isFinite(v)) { set(v); onInput(v); } } });
  const set = (v) => { range.value = Math.round(map.from(Math.min(max, Math.max(min, v))) * 1000); num.value = round(v); };
  set(value);
  return { el: h('label', { class: 'vw-slider' }, h('span', {}, label), range, num), set };
}
