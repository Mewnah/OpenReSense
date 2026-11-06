// Tiny app-wide event bus (CONTRACTS.md section 7 lists the events).
const bus = new EventTarget();

export const emit = (type, detail = {}) => bus.dispatchEvent(new CustomEvent(type, { detail }));

export function on(type, fn) {
  const h = (e) => fn(e.detail);
  bus.addEventListener(type, h);
  return () => bus.removeEventListener(type, h);
}
