// fetch helpers. Errors throw Error("<status> <detail>") using FastAPI's {detail} body when present.

export function qs(params = {}) {
  const u = new URLSearchParams();
  for (const [k, v] of Object.entries(params)) {
    if (v === undefined || v === null || v === '' || (Array.isArray(v) && !v.length)) continue;
    u.set(k, Array.isArray(v) ? v.join(',') : String(v));
  }
  const s = u.toString();
  return s ? `?${s}` : '';
}

async function handle(r) {
  if (!r.ok) {
    let msg = r.statusText;
    try {
      const j = await r.json();
      msg = typeof j.detail === 'string' ? j.detail : JSON.stringify(j.detail ?? j);
    } catch { /* non-JSON error body */ }
    throw new Error(`${r.status} ${msg}`);
  }
  return r.status === 204 ? null : r.json();
}

export const getJSON = (path, params, opts = {}) => fetch(path + qs(params), opts).then(handle);

const send = (method) => (path, body, opts = {}) => fetch(path, {
  method,
  headers: { 'Content-Type': 'application/json' },
  body: body === undefined ? undefined : JSON.stringify(body),
  ...opts,
}).then(handle);

export const postJSON = send('POST');
export const putJSON = send('PUT');
export const delJSON = send('DELETE');
