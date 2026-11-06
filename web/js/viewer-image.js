// Image view: the raster in its own pixel space (OL "pixels" projection, 1 map unit == 1 image px).
import { RasterLayer, dataSource, mapToPng } from './viewer-layer.js';
import { imageGeometry, pyramidResolutions, windowFromExtent, TILE } from './viewer-style.js';
import { qs } from './api.js';

const MAG_LEVELS = 5; // client-side magnification allowed past the last level: 1/32 image px per screen px

// cb: {onMove(pos|null), onPick(pos), onView()}; pos = {col, row, inside}
export function createImageView(el, colormaps, cb) {
  const raster = new RasterLayer(colormaps);
  let map, geo, projection, status, snap = true;

  const dpr = () => window.devicePixelRatio || 1;
  // View resolutions are multiplied by dpr so every snapped level maps image pixels to whole device pixels.
  const makeView = (center, resolution) => {
    const common = { projection, center, extent: geo.extent, constrainOnlyCenter: true, enableRotation: false };
    return snap
      ? new ol.View({ ...common, resolution, resolutions: pyramidResolutions(geo.maxZoom, MAG_LEVELS).map((r) => r * dpr()), constrainResolution: true })
      : new ol.View({ ...common, resolution, minResolution: dpr() / 2 ** MAG_LEVELS, maxResolution: 2 ** geo.maxZoom * dpr() * 2 });
  };

  function setView(view) {
    map.setView(view);
    view.on('change:resolution', () => cb.onView());
    view.on('change:center', () => cb.onView());
  }

  function ensureMap() {
    if (map) return;
    map = new ol.Map({
      target: el, controls: [],
      interactions: ol.interaction.defaults.defaults({ keyboard: false, altShiftDragRotate: false, pinchRotate: false }),
    });
    const pos = (c) => ({ col: Math.floor(c[0]), row: Math.floor(-c[1]), inside: c[0] >= 0 && c[1] <= 0 && c[0] < geo.width && -c[1] < geo.height });
    // At a power-of-two level an odd canvas size (or a wheel zoom) leaves image pixel edges on half device pixels;
    // NEAREST then picks neighbours at random, so nudge the centre until the edges are on whole device pixels.
    map.on('moveend', () => {
      const v = map.getView(), res = v.getResolution(), k = res / dpr(), centre = v.getCenter();
      if (!centre || Math.abs(Math.log2(k) % 1) > 1e-6 || v.getAnimating() || v.getInteracting()) return;
      const [px, py] = map.getPixelFromCoordinate([0, 0]);
      const fx = px * dpr() - Math.round(px * dpr()), fy = py * dpr() - Math.round(py * dpr());
      if (Math.abs(fx) > 0.01 || Math.abs(fy) > 0.01) v.setCenter([centre[0] + fx * k, centre[1] - fy * k]);
    });
    map.on('pointermove', (e) => !e.dragging && cb.onMove(pos(e.coordinate)));
    map.on('singleclick', (e) => cb.onPick(pos(e.coordinate)));
    el.addEventListener('mouseleave', () => cb.onMove(null));
    new ResizeObserver(() => map.updateSize()).observe(el);
  }

  const sourceFor = (s, info) => () => {
    const over = s.magnify === 'nearest' ? 0 : geo.overzoom;
    const base = `/api/datasets/${status.id}/tile/`;
    const query = qs({ bands: s.bands, resampling: s.resampling, magnify: s.magnify });
    const grid = new ol.tilegrid.TileGrid({ extent: geo.extent, origin: [0, 0], tileSize: TILE, resolutions: pyramidResolutions(geo.maxZoom, over) });
    return dataSource({ grid, projection, nb: s.bands.length, interpolate: s.interpolate, url: (z, x, y) => `${base}${z}/${x}/${y}${query}` });
  };

  const sync = (settings, bandStats) => raster.sync({
    sourceKey: JSON.stringify([status.id, settings.bands, settings.resampling, settings.magnify, settings.interpolate]),
    makeSource: sourceFor(settings, status.info), settings, bandStats, extent: geo.extent,
  });

  return {
    load(st, settings, bandStats) {
      ensureMap();
      status = st;
      geo = imageGeometry(st.info);
      projection = new ol.proj.Projection({ code: `pixels-${st.id}`, units: 'pixels', extent: geo.extent });
      raster.layer = null;
      map.setLayers([sync(settings, bandStats)]);
      setView(makeView([geo.width / 2, -geo.height / 2], 1));
      map.updateSize();
      this.fit();
    },
    update: sync,
    fit() { map.getView().fit(geo.extent, { size: map.getSize() }); },
    native() { map.getView().animate({ resolution: dpr(), duration: 120 }); },
    zoom(d) { const v = map.getView(); v.animate({ zoom: (snap ? Math.round(v.getZoom()) : v.getZoom()) + d, duration: 120 }); },
    setSnap(on) {
      if (snap === on) return;
      snap = on;
      if (!status) return;
      const v = map.getView();
      setView(makeView(v.getCenter(), v.getResolution()));
    },
    resize: () => map?.updateSize(),
    png: () => mapToPng(map),
    pixelWindow: () => windowFromExtent(map.getView().calculateExtent(map.getSize()), [1, 0, 0, 0, -1, 0], geo.width, geo.height),
    get map() { return map; }, // inspection hook for tests
    // image px per device px (>1 zoomed out)
    ratio: () => map.getView().getResolution() / dpr(),
  };
}
