// Footprint map for the Search tab: plain lon/lat plane of the target body (OpenLayers global `ol`).
const WORLD = [-180, -90, 180, 90];
const stroke = (color, width) => new ol.style.Stroke({ color, width });
const fill = (c) => new ol.style.Fill({ color: c });
const style = (c, f, w) => new ol.style.Style({ stroke: stroke(c, w), fill: fill(f), image: new ol.style.Circle({ radius: 4 + w, fill: fill(c), stroke: stroke('#000', 1) }) });
const STYLES = {
  base: style('#5aa9ff', 'rgba(90,169,255,.10)', 1.5),
  hover: style('#ffb454', 'rgba(255,180,84,.30)', 3),
  sel: style('#ffffff', 'rgba(255,255,255,.22)', 3),
};
const box = new ol.style.Style({ stroke: new ol.style.Stroke({ color: '#ffb454', width: 2, lineDash: [6, 4] }) });
const json = new ol.format.GeoJSON();
const wrapLon = (x) => ((((x + 180) % 360) + 360) % 360) - 180;

// Only lon/lat-aligned basemaps fit this view; others (polar stereographic...) are skipped.
function basemapLayer(bm) {
  const attributions = bm.attribution;
  if (bm.type === 'wms') {
    if (!bm.srs?.includes('EPSG:4326')) return null;
    return new ol.layer.Tile({ source: new ol.source.TileWMS({ url: bm.url, params: { LAYERS: bm.layers, VERSION: bm.version, FORMAT: bm.format }, projection: 'EPSG:4326', attributions, crossOrigin: 'anonymous' }) });
  }
  const ts = bm.tile_size || 256;
  if (bm.tiling === 'webmercator') {
    return new ol.layer.Tile({ source: new ol.source.XYZ({ url: bm.url, projection: 'EPSG:3857', maxZoom: bm.max_zoom, tileSize: ts, attributions, crossOrigin: 'anonymous' }) });
  }
  if (bm.crs !== 'lonlat') return null;
  const tileGrid = new ol.tilegrid.TileGrid({
    extent: WORLD, origin: [-180, 90], tileSize: bm.tiling === 'geographic-1x1' ? [ts * 2, ts] : ts,
    resolutions: Array.from({ length: bm.max_zoom + 1 }, (_, z) => 180 / ts / 2 ** z),
  });
  return new ol.layer.Tile({ source: new ol.source.XYZ({ url: bm.url, projection: 'EPSG:4326', tileGrid, attributions, crossOrigin: 'anonymous' }) });
}

export function createMap(el, { onHover, onSelect, onBox }) {
  const footprints = new ol.source.Vector();
  const boxes = new ol.source.Vector();
  const byKey = new Map();
  let baseLayer = null, hovered = null, selected = null, drawing = null;

  const map = new ol.Map({
    target: el,
    view: new ol.View({ projection: 'EPSG:4326', center: [0, 0], zoom: 1, maxZoom: 14, multiWorld: true }),
    layers: [
      new ol.layer.Graticule({ strokeStyle: stroke('rgba(255,255,255,.16)', 1), showLabels: true, wrapX: false, latLabelPosition: 0.02, lonLabelPosition: 0.98 }),
      new ol.layer.Vector({ source: boxes, style: box, zIndex: 5 }),
      new ol.layer.Vector({ source: footprints, zIndex: 6, style: (f) => STYLES[f.get('state') || 'base'] }),
    ],
  });
  const fitWorld = () => { const size = map.getSize(); if (size?.[0] && size[1]) map.getView().fit(WORLD, { size }); };

  const setState = (key, state) => { const f = byKey.get(key); if (f) f.set('state', state); };
  const keyAt = (pixel) => map.forEachFeatureAtPixel(pixel, (f) => f.get('key'), { hitTolerance: 3 }) ?? null;

  map.on('pointermove', (e) => {
    if (e.dragging || drawing) return;
    const k = keyAt(e.pixel);
    if (k !== hovered) { api.highlight(k); onHover(k); }
  });
  map.on('click', (e) => { if (!drawing) onSelect(keyAt(e.pixel)); });

  const api = {
    setBody(body) {
      if (baseLayer) map.removeLayer(baseLayer);
      baseLayer = null;
      for (const bm of body?.basemaps || []) if ((baseLayer = basemapLayer(bm))) break;
      if (baseLayer) map.getLayers().insertAt(0, baseLayer);
      if (!footprints.getFeatures().length) fitWorld();
    },
    add(results) {
      for (const r of results) {
        const key = `${r.connector}:${r.id}`;
        let g = null;
        if (r.footprint) g = json.readGeometry(r.footprint);
        else if (r.bbox) {
          const [w, s, e, n] = r.bbox;
          g = w === e && s === n ? new ol.geom.Point([w, s]) : ol.geom.Polygon.fromExtent([w, s, w > e ? e + 360 : e, n]);
        }
        if (!g || byKey.has(key)) continue;
        const f = new ol.Feature(g);
        f.set('key', key);
        byKey.set(key, f);
        footprints.addFeature(f);
      }
    },
    clear() { footprints.clear(); byKey.clear(); hovered = selected = null; },
    fit() {
      const ext = footprints.getExtent();
      if (footprints.getFeatures().length) map.getView().fit(ext, { padding: [30, 30, 30, 30], maxZoom: 8 });
    },
    highlight(key) {
      if (hovered !== selected) setState(hovered, '');
      hovered = key;
      if (key !== selected) setState(key, 'hover');
    },
    select(key, fit) {
      setState(selected, '');
      selected = key;
      setState(key, 'sel');
      const f = byKey.get(key);
      if (f && fit) map.getView().fit(f.getGeometry().getExtent(), { padding: [40, 40, 40, 40], maxZoom: 9 });
    },
    has: (key) => byKey.has(key),
    setBox(bbox) {
      boxes.clear();
      if (!bbox) return;
      const [w, s, e, n] = bbox;
      boxes.addFeature(new ol.Feature(ol.geom.Polygon.fromExtent([w, s, w > e ? e + 360 : e, n])));
    },
    drawBox(on) {
      if (drawing) { map.removeInteraction(drawing); drawing = null; }
      if (!on) return;
      drawing = new ol.interaction.Draw({ source: new ol.source.Vector(), type: 'Circle', geometryFunction: ol.interaction.Draw.createBox() });
      drawing.on('drawend', (e) => {
        const [x0, y0, x1, y1] = e.feature.getGeometry().getExtent();
        const round = (v) => Math.round(v * 1e4) / 1e4;
        const full = x1 - x0 >= 360;
        const bbox = [full ? -180 : wrapLon(x0), Math.max(-90, y0), full ? 180 : wrapLon(x1), Math.min(90, y1)].map(round);
        setTimeout(() => api.drawBox(false)); // after the event so OL finishes its own handling
        onBox(bbox);
      });
      map.addInteraction(drawing);
    },
    resize() { map.updateSize(); if (!footprints.getFeatures().length) fitWorld(); },
  };
  return api;
}
