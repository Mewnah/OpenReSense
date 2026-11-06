// Solar-system bodies + Earth regions (catalog/bodies.json + vocab regions via /api/bodies).
import { getJSON } from './api.js';

let cache;
export const getBodies = () => (cache ??= getJSON('/api/bodies')); // {bodies: [...], regions: {id: {...}}}

export async function bodyById(id) {
  const { bodies } = await getBodies();
  return bodies.find((b) => b.id === id) || null;
}

// Body for an opened dataset: info.crs.body when the backend recognised it, else Earth for geographic data.
export async function bodyForInfo(info) {
  return (await bodyById(info?.crs?.body || 'earth')) || null;
}
