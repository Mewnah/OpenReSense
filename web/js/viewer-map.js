// Map view: georeferenced 2D map in a selectable projection on the dataset's own body.
// Raster tiles are warped by the server (/warp) into the view projection; basemaps are reprojected by OL.
import { RasterLayer, dataSource, mapToPng } from './viewer-layer.js';
import { TILE, windowFromExtent } from './viewer-style.js';
import { qs } from './api.js';

proj4.defs('EPSG:3413', '+proj=stere +lat_0=90 +lat_ts=70 +lon_0=-45 +k=1 +x_0=0 +y_0=0 +datum=WGS84 +units=m +no_defs');
proj4.defs('EPSG:3031', '+proj=stere +lat_0=-90 +lat_ts=-71 +lon_0=0 +k=1 +x_0=0 +y_0=0 +datum=WGS84 +units=m +no_defs');
ol.proj.proj4.register(proj4);
const POLAR_EXTENT = [-12.5e6, -12.5e6, 12.5e6, 12.5e6]; // whole hemisphere down to the equator
const RAD = Math.PI / 180;

const radii = (info, body) => [
  info.crs?.a ?? body?.radii_m?.[0] ?? 6378137,
  info.crs?.b ?? body?.radii_m?.[2] ?? body?.radii_m?.[0] ?? 6356752.314,
];

// Projection menu for a dataset: [{id, label}]
export function projectionChoices(info, body) {
  const earth = body?.id === 'earth';
  return [
    ...(info.crs?.proj4 && info.bounds ? [{ id: 'native', label: `Native (${info.crs.name || 'dataset CRS'})` }] : []),
    { id: 'lonlat', label: 'Lon / Lat (equirectangular)' },
    { id: 'north', label: earth ? 'North polar (EPSG:3413)' : 'North polar stereographic' },
    { id: 'south', label: earth ? 'South polar (EPSG:3031)' : 'South polar stereographic' },
    // shortcut: OL draws no raster tiles east of 180 in Web Mercator (they work in lon/lat), so skip it for antimeridian footprints
    ...(earth && !(info.lonlat_bounds?.[0] > info.lonlat_bounds?.[2]) ? [{ id: 'mercator', label: 'Web Mercator' }] : []),
  ];
}

function register(code, def) {
  if (!ol.proj.get(code)) { proj4.defs(code, def); ol.proj.proj4.register(proj4); }
  return ol.proj.get(code);
}

const haversine = ([lon1, lat1], [lon2, lat2], R) => {
  const a = Math.sin((lat2 - lat1) * RAD / 2) ** 2
    + Math.cos(lat1 * RAD) * Math.cos(lat2 * RAD) * Math.sin((lon2 - lon1) * RAD / 2) ** 2;
  return 2 * R * Math.asin(Math.sqrt(a));
};

// OL projection of the dataset body's geographic CRS (degrees, east-positive).
export function bodyLonLat(info, body) {
  const [a, b] = radii(info, body);
  const p = register(`ors:ll:${a}:${b}`, `+proj=longlat +a=${a} +b=${b} +no_defs`);
  p.setExtent([-180, -90, 180, 90]);
  p.setGlobal(true); // tiles and basemaps repeat past +-180, so a footprint crossing the antimeridian is contiguous
  return p;
}

// OL assumes Earth's sphere for the scale line; other bodies need their own metric.
function bodyScale(p, ll, R) {
  const toLL = ol.proj.getTransform(p, ll), mpu = p.getMetersPerUnit() || 1;
  p.setGetPointResolution((res, pt) => haversine(toLL([pt[0] - res / 2, pt[1]]), toLL([pt[0] + res / 2, pt[1]]), R) / mpu || res);
}

// z0 = 2 x 1 tiles of 180 degrees (or 1 tile of 360 x 180 when `wide`), y = 0 at the north.
const geoGrid = (ts, maxZoom, wide) => new ol.tilegrid.TileGrid({
  extent: [-180, -90, 180, 90], origin: [-180, 90], tileSize: wide ? [2 * ts, ts] : ts,
  resolutions: Array.from({ length: maxZoom + 1 }, (_, i) => 180 / ts / 2 ** i),
});

// Everything the view needs for one projection choice; {empty: true} when the dataset isn't visible in it.
function projectionContext(id, status, body) {
  const { info } = status;
  const ll = bodyLonLat(info, body);
  const [a, b] = radii(info, body);
  const earth = body?.id === 'earth';
  let p, crs, extent;
  switch (id) {
    case 'native': p = register(`ors:native:${status.id}`, info.crs.proj4); crs = 'native'; extent = info.bounds; break;
    case 'lonlat': p = ll; crs = 'lonlat'; extent = ll.getExtent(); break;
    case 'north': case 'south': {
      const pole = id === 'north' ? 90 : -90;
      if (earth) { p = ol.proj.get(pole > 0 ? 'EPSG:3413' : 'EPSG:3031'); crs = p.getCode(); extent = POLAR_EXTENT; } else {
        crs = `+proj=stere +lat_0=${pole} +lat_ts=${pole} +lon_0=0 +k=1 +x_0=0 +y_0=0 +a=${a} +b=${b} +units=m +no_defs`;
        p = register(`ors:${id}:${a}:${b}`, crs);
        extent = [-2 * a, -2 * a, 2 * a, 2 * a];
      }
      break;
    }
    default: p = ol.proj.get('EPSG:3857'); crs = 'EPSG:3857'; extent = p.getExtent();
  }
  p.setExtent(extent);
  if (!earth) bodyScale(p, ll, (2 * a + b) / 3);

  // Dataset footprint in this projection; W > E (antimeridian) continues east of 180. Projections that wrap in x
  // (lon/lat, Mercator) keep the part beyond their extent, the others are clipped to it.
  const lb = info.lonlat_bounds;
  if (lb && (id === 'north' ? lb[3] <= 0 : id === 'south' && lb[1] >= 0)) return { empty: true }; // other hemisphere
  const wraps = p.canWrapX();
  let layerExtent = id === 'native' ? info.bounds : null;
  if (!layerExtent) {
    let [w, s, e, n] = info.lonlat_bounds ?? [-180, -90, 180, 90];
    if (w > e) e += 360;
    layerExtent = ol.extent.getIntersection(ol.proj.transformExtent([w, s, e, n], ll, p, 40),
      wraps ? [-Infinity, extent[1], Infinity, extent[3]] : extent);
  }
  if (!layerExtent || ol.extent.isEmpty(layerExtent) || !layerExtent.every(Number.isFinite)) return { empty: true };

  const res0 = id === 'lonlat' ? 180 / TILE : Math.max(extent[2] - extent[0], extent[3] - extent[1]) / TILE;
  const pxSize = id === 'native' && info.resolution ? Math.abs(info.resolution[0])
    : Math.sqrt(ol.extent.getArea(layerExtent) / (info.width * info.height));
  const maxZoom = Math.min(24, Math.max(0, Math.ceil(Math.log2(res0 / pxSize)) + 1));
  const grid = id === 'lonlat' ? geoGrid(TILE, maxZoom) : ol.tilegrid.createXYZ({ extent, tileSize: TILE, maxZoom });
  const w = extent[2] - extent[0];
  const viewExtent = wraps && id !== 'native' ? [extent[0] - w, extent[1], extent[2] + w, extent[3]] : extent;
  return { id, p, ll, crs, extent, viewExtent, layerExtent, grid, pxSize };
}

// null when the basemap uses a CRS we cannot draw.
function basemapLayer(bm, llProj) {
  const crs = !bm.crs || bm.crs === 'lonlat' ? llProj : ol.proj.get(bm.crs);
  if (!crs) return null;
  const ts = bm.tile_size || 256, maxZoom = bm.max_zoom ?? 12;
  const geographic = bm.tiling?.startsWith('geographic') || crs === llProj;
  const grid = geographic ? geoGrid(ts, maxZoom, bm.tiling === 'geographic-1x1')
    : ol.tilegrid.createXYZ({ extent: crs.getExtent(), tileSize: ts, maxZoom });
  const common = { projection: crs, tileGrid: grid, attributions: bm.attribution, interpolate: true, crossOrigin: 'anonymous' }; // every basemap host sends CORS headers; without this Save PNG fails
  if (bm.type !== 'wms') return new ol.layer.Tile({ source: new ol.source.XYZ({ ...common, url: bm.url }) });
  const v13 = /^1\.3/.test(bm.version || '1.1.1');
  const srs = bm.srs?.[0] ?? (crs === llProj ? 'EPSG:4326' : crs.getCode());
  const flip = v13 && srs === 'EPSG:4326'; // WMS 1.3.0 uses lat,lon axis order for EPSG:4326
  const base = bm.url + (bm.url.includes('?') ? '&' : '?') + qs({
    SERVICE: 'WMS', REQUEST: 'GetMap', VERSION: bm.version || '1.1.1', LAYERS: bm.layers, STYLES: '',
    FORMAT: bm.format || 'image/jpeg', WIDTH: ts, HEIGHT: ts, [v13 ? 'CRS' : 'SRS']: srs,
  });
  return new ol.layer.Tile({
    source: new ol.source.TileImage({
      ...common,
      tileUrlFunction: (tc) => {
        const [x0, y0, x1, y1] = grid.getTileCoordExtent(tc);
        return `${base}&BBOX=${(flip ? [y0, x0, y1, x1] : [x0, y0, x1, y1]).join(',')}`;
      },
    }),
  });
}

// cb: {onMove({lon, lat}|null), onPick({lon, lat}), onView()}
export function createMapView(el, colormaps, cb) {
  const raster = new RasterLayer(colormaps);
  const basemaps = new Map(); // basemap id -> OL layer, or null when unsupported
  let map, status, ctx, graticule = null, bmId = null;

  function ensureMap() {
    if (map) return;
    map = new ol.Map({
      target: el,
      controls: [new ol.control.ScaleLine({ units: 'metric' }), new ol.control.Attribution({ collapsible: true })],
      interactions: ol.interaction.defaults.defaults({ keyboard: false, altShiftDragRotate: false, pinchRotate: false }),
    });
    const pos = (c) => {
      const [lon, lat] = ol.proj.transform(c, ctx.p, ctx.ll);
      return Math.abs(lat) <= 90 && Math.abs(lon) <= 900 ? { lon: ((lon + 540) % 360 + 360) % 360 - 180, lat } : null;
    };
    map.on('pointermove', (e) => !e.dragging && !ctx?.empty && cb.onMove(pos(e.coordinate)));
    map.on('singleclick', (e) => { const p = !ctx?.empty && pos(e.coordinate); if (p) cb.onPick({ ...p, coord: e.coordinate }); });
    el.addEventListener('mouseleave', () => cb.onMove(null));
    new ResizeObserver(() => map.updateSize()).observe(el);
  }

  const layers = () => map.setLayers([basemaps.get(bmId), raster.layer, graticule].filter(Boolean));

  const sync = (s, bandStats) => raster.sync({
    sourceKey: JSON.stringify([status.id, ctx.id, s.bands, s.resampling, s.magnify, s.interpolate]),
    settings: s, bandStats, extent: ctx.layerExtent,
    makeSource: () => {
      const query = qs({ crs: ctx.crs, width: TILE, height: TILE, format: 'f32', bands: s.bands, resampling: s.resampling, magnify: s.magnify });
      return dataSource({
        grid: ctx.grid, projection: ctx.p, nb: s.bands.length, interpolate: s.interpolate, wrapX: ctx.p.canWrapX(),
        url: (z, x, y) => `/api/datasets/${status.id}/warp${query}&bbox=${ctx.grid.getTileCoordExtent([z, x, y]).join(',')}`,
      });
    },
  });

  return {
    load(st, body, projId, settings, bandStats) {
      ensureMap();
      status = st;
      ctx = projectionContext(projId, st, body);
      raster.layer = null;
      if (ctx.empty) { map.setLayers([]); return ctx; }
      for (const bm of body?.basemaps ?? []) if (!basemaps.has(bm.id)) basemaps.set(bm.id, basemapLayer(bm, ctx.ll));
      sync(settings, bandStats);
      layers();
      map.setView(new ol.View({
        projection: ctx.p, center: ol.extent.getCenter(ctx.layerExtent), zoom: 0,
        minResolution: ctx.grid.getResolution(ctx.grid.getMaxZoom()) / 16,
        extent: ctx.viewExtent, constrainOnlyCenter: true, enableRotation: false,
      }));
      map.getView().on(['change:resolution', 'change:center'], () => cb.onView());
      map.updateSize();
      this.fit();
      return ctx;
    },
    update(settings, bandStats) { if (!ctx?.empty) sync(settings, bandStats); },
    basemapChoices: (body) => (body?.basemaps ?? []).filter((bm) => basemaps.get(bm.id)).map((bm) => ({ id: bm.id, name: bm.name })),
    setBasemap(id) { bmId = id; if (!ctx?.empty && map) layers(); },
    setGraticule(on) {
      graticule = on ? new ol.layer.Graticule({
        showLabels: true, wrapX: false,
        strokeStyle: new ol.style.Stroke({ color: 'rgba(255,255,255,.4)', width: 1, lineDash: [2, 4] }),
      }) : null;
      if (!ctx?.empty && map) layers();
    },
    fit() { if (!ctx || ctx.empty) return; map.getView().fit(ctx.layerExtent, { size: map.getSize(), padding: [20, 20, 20, 20] }); },
    native() { if (!ctx || ctx.empty) return; map.getView().animate({ resolution: ctx.pxSize * (window.devicePixelRatio || 1), duration: 120 }); },
    zoom(d) { map.getView().animate({ zoom: map.getView().getZoom() + d, duration: 120 }); },
    resize: () => map?.updateSize(),
    png: () => mapToPng(map),
    // Visible part of the image in pixels; only for the native projection, where the view is the pixel grid.
    pixelWindow: () => ctx?.id === 'native' && status.info.transform
      ? windowFromExtent(map.getView().calculateExtent(map.getSize()), status.info.transform, status.info.width, status.info.height) : null,
    get map() { return map; }, // inspection hook for tests
    // dataset pixels per device pixel (>1 zoomed out); approximate away from the projection's true-scale line
    ratio: () => (!ctx || ctx.empty ? 1 : map.getView().getResolution() / ctx.pxSize / (window.devicePixelRatio || 1)),
    get empty() { return !ctx || ctx.empty; },
  };
}
