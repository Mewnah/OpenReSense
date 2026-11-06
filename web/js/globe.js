// 3D globe (CesiumJS) for any body: basemaps from catalog/bodies.json, server-rendered dataset drape,
// optional DEM terrain. API: CONTRACTS.md section 7. Call setBody() before setDataset() for another body.
import { qs } from './api.js';
import { renderParams } from './render.js';

let cesiumLoading;
function loadCesium() {
  cesiumLoading ??= new Promise((resolve, reject) => {
    window.CESIUM_BASE_URL ??= '/vendor/cesium/';
    const css = document.createElement('link');
    css.rel = 'stylesheet';
    css.href = '/vendor/cesium/Widgets/widgets.css';
    document.head.append(css);
    const js = document.createElement('script');
    js.src = '/vendor/cesium/Cesium.js';
    js.onload = () => resolve(window.Cesium);
    js.onerror = () => { cesiumLoading = null; reject(new Error('Could not load CesiumJS from /vendor/cesium/')); };
    document.head.append(js);
  });
  return cesiumLoading;
}

const splitAntimeridian = ([w, s, e, n]) => (w <= e ? [[w, s, e, n]] : [[w, s, 180, n], [-180, s, e, n]]);
const intersects = (a, b) => a[0] <= b[2] && b[0] <= a[2] && a[1] <= b[3] && b[1] <= a[3];
const sameRadii = (a, b) => a.every((v, i) => Math.abs(v - b[i]) < 1e-6 * v);
export const isDEM = (status) => {
  const i = status?.info;
  return !!i && i.count === 1 && i.lonlat_bounds
    && (/^(m|km|meters?|metres?)$/i.test(i.units?.[0] || '')
      || /dem|dtm|elev|topo|height|mola|lola|megdr|srtm|gebco/i.test(`${status.name} ${i.metadata?.['']?.DATA_SET_ID ?? ''}`));
};

const unitScale = (info) => (/^km$/i.test(info.units?.[0] || '') ? 1000 : 1);
// Radius products (e.g. LOLA LDEM: OFFSET = 1737400 m) store distance from the body centre, not height:
// the reference sphere radius must be subtracted.
export const heightBase = (info, radius) => (Math.abs((info.offsets?.[0] ?? 0) * unitScale(info)) > radius / 2 ? radius : 0);

export async function createGlobe(containerEl, { body }) {
  const C = await loadCesium();

  containerEl.classList.add('gl-root');
  containerEl.innerHTML = `
    <div class="gl-view"></div>
    <div class="gl-panel card">
      <label class="gl-field gl-basemap-row">Basemap <select class="select gl-basemap"></select></label>
      <label class="gl-check"><input type="checkbox" class="gl-light"> Sun lighting</label>
      <div class="gl-dem" hidden>
        <label class="gl-check"><input type="checkbox" class="gl-terrain"> 3D terrain</label>
        <label class="gl-field">Exaggeration <output class="gl-exag-out">1×</output>
          <input type="range" class="gl-exag" min="1" max="10" step="0.5" value="1"></label>
      </div>
      <button class="btn small gl-reset">Reset view</button>
      <div class="gl-note muted" role="status"></div>
    </div>
    <div class="gl-attr"></div>`;
  const $ = (sel) => containerEl.querySelector(sel);
  const viewEl = $('.gl-view');
  const noteEl = $('.gl-note');
  const baseSel = $('.gl-basemap');

  // S survives viewer rebuilds; everything else is per-viewer.
  const S = { body, radii: body.radii_m, basemapId: null, status: null, settings: null,
    lighting: false, terrain: null, exaggeration: 1 };
  let viewer, ellipsoid, geoScheme, basemapLayer, layers = [], stale = [], cancelWait;

  const note = (msg = '') => { noteEl.textContent = msg; };
  const render = () => viewer.scene.requestRender();
  const geographic = (extra) => new C.GeographicTilingScheme({ ellipsoid, ...extra });

  function tilingFor(bm) {
    if (bm.type === 'wms') {
      const srs = bm.srs || ['EPSG:4326'];
      if (srs.includes('EPSG:4326')) return geographic();
      if (srs.includes('EPSG:3857')) return new C.WebMercatorTilingScheme({ ellipsoid });
      return null;
    }
    if (bm.tiling === 'webmercator') return new C.WebMercatorTilingScheme({ ellipsoid });
    if (bm.tiling === 'geographic-2x1') return geographic();
    if (bm.tiling === 'geographic-1x1') return geographic({ numberOfLevelZeroTilesX: 1, numberOfLevelZeroTilesY: 1 });
    return null; // e.g. polar projections: not drawable as a globe imagery layer
  }

  const usableBasemaps = () => (S.body.basemaps || []).filter((bm) => tilingFor(bm));

  function basemapProvider(bm) {
    const common = {
      tilingScheme: tilingFor(bm), minimumLevel: bm.min_zoom ?? 0, maximumLevel: bm.max_zoom,
      tileWidth: bm.tile_size || 256, tileHeight: bm.tile_size || 256, credit: bm.attribution,
    };
    if (bm.type === 'wms') {
      return new C.WebMapServiceImageryProvider({ ...common, url: bm.url, layers: bm.layers,
        parameters: { format: bm.format || 'image/jpeg', version: bm.version || '1.1.1' } });
    }
    return new C.UrlTemplateImageryProvider({ ...common, url: bm.url });
  }

  function applyBasemap() {
    if (basemapLayer) viewer.imageryLayers.remove(basemapLayer);
    basemapLayer = null;
    const bm = usableBasemaps().find((b) => b.id === S.basemapId);
    if (!bm) return;
    const provider = basemapProvider(bm);
    let warned = false;
    provider.errorEvent.addEventListener((e) => {
      if (!warned) { warned = true; note(`Basemap tiles failing to load (${bm.name}): ${e.message || 'network error'}`); }
    });
    basemapLayer = new C.ImageryLayer(provider);
    viewer.imageryLayers.add(basemapLayer, 0);
    render();
  }

  function fillBasemapSelect() {
    const list = usableBasemaps();
    baseSel.innerHTML = list.map((b) => `<option value="${b.id}"></option>`).join('');
    list.forEach((b, i) => { baseSel.options[i].textContent = b.name + (b.provider ? ` — ${b.provider}` : ''); });
    S.basemapId = list.some((b) => b.id === S.basemapId) ? S.basemapId : list[0]?.id ?? null;
    baseSel.value = S.basemapId ?? '';
    $('.gl-basemap-row').hidden = list.length < 2;
    note(list.length ? '' : `No public basemap catalogued for ${S.body.name}.`);
  }

  const homeView = () => viewer.camera.setView({
    destination: C.Cartesian3.fromDegrees(0, 0, ellipsoid.maximumRadius * 3.5, ellipsoid),
  });

  function buildViewer(radii, keepCamera) {
    const prev = viewer && keepCamera ? viewer.camera.positionCartographic.clone() : null;
    teardown();
    S.radii = radii;
    ellipsoid = new C.Ellipsoid(...radii);
    C.Ellipsoid.default = ellipsoid;
    geoScheme = geographic();
    const earth = S.body.id === 'earth';
    viewer = new C.Viewer(viewEl, {
      ellipsoid, baseLayer: false, terrainProvider: new C.EllipsoidTerrainProvider({ ellipsoid }),
      baseLayerPicker: false, geocoder: false, homeButton: false, sceneModePicker: false,
      navigationHelpButton: false, animation: false, timeline: false, fullscreenButton: false,
      infoBox: false, selectionIndicator: false, creditContainer: $('.gl-attr'),
      requestRenderMode: true, maximumRenderTimeChange: Infinity,
    });
    const { scene } = viewer;
    scene.backgroundColor = C.Color.BLACK;
    scene.globe.baseColor = C.Color.fromCssColorString(S.body.color || '#888888');
    scene.globe.enableLighting = S.lighting;
    scene.globe.showGroundAtmosphere = earth;
    scene.globe.maximumScreenSpaceError = 1.5; // sharper than default 2: faithful detail matters more than speed
    if (scene.skyAtmosphere) scene.skyAtmosphere.show = earth;
    scene.fog.enabled = earth;
    for (const o of [scene.sun, scene.moon]) if (o) o.show = earth;
    if (scene.skyBox) scene.skyBox.show = false;
    if (prev) viewer.camera.setView({ destination: C.Cartesian3.fromRadians(prev.longitude, prev.latitude, prev.height, ellipsoid) });
    else homeView();
    layers = []; stale = []; basemapLayer = null;
    applyBasemap();
    applyDrape();
    applyTerrain();
  }

  function teardown() {
    cancelWait?.();
    if (viewer && !viewer.isDestroyed()) viewer.destroy();
    viewer = null;
  }

  // Dataset ellipsoid vs body ellipsoid differ in geodetic latitude (e.g. Mars sphere vs oblate), so
  // when the dataset is on this body, the globe adopts the dataset's ellipsoid.
  function wantedRadii() {
    const crs = S.status?.info?.crs;
    const [a] = S.body.radii_m;
    if (crs?.a && Math.abs(crs.a - a) / a < 0.005) return [crs.a, crs.a, crs.b ?? crs.a];
    return S.body.radii_m;
  }

  // Waits (bounded) until the new layers' tiles are in, then drops the previous ones: no blank flash.
  function dropStaleWhenLoaded() {
    cancelWait?.();
    if (!stale.length) return;
    const globe = viewer.scene.globe;
    let armed = false;
    const done = () => {
      cancelWait();
      for (const l of stale.splice(0)) viewer.imageryLayers.remove(l);
      render();
    };
    const armTimer = setTimeout(() => { armed = true; render(); }, 300);
    const failTimer = setTimeout(done, 10000);
    const off = globe.tileLoadProgressEvent.addEventListener((n) => { if (armed && n === 0) done(); });
    cancelWait = () => { clearTimeout(armTimer); clearTimeout(failTimer); off(); cancelWait = null; };
  }

  function applyDrape() {
    stale.push(...layers);
    layers = [];
    const info = S.status?.info;
    $('.gl-dem').hidden = !isDEM(S.status);
    if (info && !info.lonlat_bounds) note('This dataset has no georeferencing, so it cannot be shown on the globe.');
    else if (info && info.crs?.body && info.crs.body !== S.body.id) note(`Dataset is on ${info.crs.body}, globe shows ${S.body.name}.`);
    else if (!usableBasemaps().length) note(`No public basemap catalogued for ${S.body.name}.`);
    else note('');
    if (info?.lonlat_bounds && S.settings) {
      const [w, s, e, n] = info.lonlat_bounds;
      const lonSpan = w <= e ? e - w : e - w + 360;
      const degPerPx = Math.min(lonSpan / info.width, (n - s) / info.height);
      const maximumLevel = Math.max(0, Math.min(24, Math.ceil(Math.log2(180 / (256 * degPerPx)))));
      const url = `/api/datasets/${S.status.id}/warp?crs=lonlat&bbox={westDegrees},{southDegrees},{eastDegrees},{northDegrees}`
        + `&width=256&height=256&format=png&${qs(renderParams(S.settings)).slice(1)}`;
      const magnificationFilter = S.settings.interpolate ? C.TextureMagnificationFilter.LINEAR : C.TextureMagnificationFilter.NEAREST;
      for (const rect of splitAntimeridian(info.lonlat_bounds)) {
        const provider = new C.UrlTemplateImageryProvider({
          url, tilingScheme: geoScheme, maximumLevel, tileWidth: 256, tileHeight: 256,
          rectangle: C.Rectangle.fromDegrees(...rect),
        });
        provider.errorEvent.addEventListener((e) => note(`Dataset tiles failing to load: ${e.message || 'server error'}`));
        const layer = new C.ImageryLayer(provider, { magnificationFilter });
        viewer.imageryLayers.add(layer);
        layers.push(layer);
      }
    }
    dropStaleWhenLoaded();
    render();
  }

  const HEIGHTMAP = 65;
  function applyTerrain() {
    const { scene } = viewer;
    const status = S.terrain;
    $('.gl-terrain').checked = !!status;
    if (!status) {
      scene.terrainProvider = new C.EllipsoidTerrainProvider({ ellipsoid });
      scene.verticalExaggeration = 1;
      return render();
    }
    const { id, info } = status;
    const bounds = splitAntimeridian(info.lonlat_bounds);
    const [w0, s0, e0, n0] = info.lonlat_bounds;
    const lonSpan = w0 <= e0 ? e0 - w0 : e0 - w0 + 360;
    const degPerPx = Math.min(lonSpan / info.width, (n0 - s0) / info.height);
    const maxLevel = Math.max(0, Math.min(18, Math.ceil(Math.log2(180 / ((HEIGHTMAP - 1) * degPerPx)))));
    const scale = info.scales?.[0] ?? 1;
    const offset = info.offsets?.[0] ?? 0;
    const unit = unitScale(info);
    const base = heightBase(info, S.radii[0]);
    const provider = new C.CustomHeightmapTerrainProvider({
      width: HEIGHTMAP, height: HEIGHTMAP, tilingScheme: geoScheme,
      callback: async (x, y, level) => {
        const r = geoScheme.tileXYToRectangle(x, y, level);
        const [w, s, e, n] = [r.west, r.south, r.east, r.north].map(C.Math.toDegrees);
        const heights = new Float32Array(HEIGHTMAP * HEIGHTMAP);
        if (!bounds.some((b) => intersects(b, [w, s, e, n]))) return heights;
        // Grow the box by half a pixel so the edge samples land exactly on the tile edges (shared with neighbours).
        const dx = (e - w) / (HEIGHTMAP - 1) / 2;
        const dy = (n - s) / (HEIGHTMAP - 1) / 2;
        const res = await fetch(`/api/datasets/${id}/warp?crs=lonlat&bbox=${w - dx},${s - dy},${e + dx},${n + dy}`
          + `&width=${HEIGHTMAP}&height=${HEIGHTMAP}&bands=1&format=f32`);
        if (!res.ok) throw new Error(`terrain tile ${res.status}`);
        const px = new Float32Array(await res.arrayBuffer());
        const stride = +res.headers.get('X-Bands') || 2;
        for (let i = 0; i < heights.length; i++) {
          const v = px[i * stride];
          // nodata (alpha 0) and non-finite values become 0 m
          heights[i] = px[i * stride + stride - 1] > 0.5 && Number.isFinite(v) ? (v * scale + offset) * unit - base : 0;
        }
        return heights;
      },
    });
    provider.getTileDataAvailable = (x, y, level) => level <= maxLevel; // deeper levels are upsampled from the last one
    scene.terrainProvider = provider;
    scene.verticalExaggeration = S.exaggeration;
    render();
  }

  function setBody(newBody) {
    if (newBody.id === S.body.id && sameRadii(newBody.radii_m, S.body.radii_m)) return;
    S.body = newBody;
    S.status = S.settings = S.terrain = null;
    S.basemapId = null;
    fillBasemapSelect();
    buildViewer(newBody.radii_m, false);
  }

  const api = {
    setBody,

    setDataset(status, settings) {
      S.status = status?.info ? status : null;
      S.settings = settings;
      if (!S.status || !isDEM(S.status)) S.terrain = null;
      const radii = wantedRadii();
      if (sameRadii(radii, S.radii)) { applyDrape(); applyTerrain(); } else buildViewer(radii, true);
    },

    setRender(settings) {
      S.settings = settings;
      if (S.status) applyDrape();
    },

    flyToDataset() {
      const b = S.status?.info?.lonlat_bounds;
      if (!b) return;
      // requestRenderMode does not tick camera flights by itself
      const v = viewer;
      const pump = setInterval(() => (v.isDestroyed() ? stop() : v.scene.requestRender()), 16);
      const stop = () => clearInterval(pump);
      v.camera.flyTo({ destination: C.Rectangle.fromDegrees(...b), duration: 1.5, complete: stop, cancel: stop });
    },

    setTerrain(status, exaggeration = S.exaggeration) {
      S.terrain = status && isDEM(status) ? status : null;
      S.exaggeration = exaggeration;
      $('.gl-exag').value = exaggeration;
      $('.gl-exag-out').textContent = `${exaggeration}×`;
      applyTerrain();
    },

    resize() { viewer.resize(); render(); },

    destroy() {
      teardown();
      C.Ellipsoid.default = C.Ellipsoid.WGS84;
      containerEl.classList.remove('gl-root');
      containerEl.innerHTML = '';
    },
  };

  baseSel.onchange = () => { S.basemapId = baseSel.value; applyBasemap(); };
  $('.gl-light').onchange = (e) => { S.lighting = e.target.checked; viewer.scene.globe.enableLighting = S.lighting; render(); };
  $('.gl-reset').onclick = () => { homeView(); render(); };
  $('.gl-terrain').onchange = (e) => api.setTerrain(e.target.checked ? S.status : null);
  $('.gl-exag').oninput = (e) => api.setTerrain(S.terrain, +e.target.value);

  fillBasemapSelect();
  buildViewer(body.radii_m, false);
  return api;
}
