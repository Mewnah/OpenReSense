// Shared by the image and map views: Float32 DataTile source + WebGL layer whose style follows the render settings.
import { buildStyle, styleKey, styleVariables, TILE } from './viewer-style.js';

async function fetchF32(url, signal, expected) {
  const r = await fetch(url, { signal });
  if (!r.ok) throw new Error(`${r.status} ${url}`);
  const data = new Float32Array(await r.arrayBuffer());
  if (data.length !== expected) throw new Error(`tile has ${data.length} values, expected ${expected}`);
  return data;
}

// nb displayed bands -> tiles carry nb + 1 channels (alpha last).
export function dataSource({ grid, projection, nb, interpolate, url, wrapX }) {
  return new ol.source.DataTile({
    tileGrid: grid, projection, bandCount: nb + 1, interpolate, transition: 0, wrapX,
    loader: (z, x, y, { signal }) => fetchF32(url(z, x, y), signal, TILE * TILE * (nb + 1)),
  });
}

export class RasterLayer {
  constructor(colormaps) { this.colormaps = colormaps; this.layer = null; this.sourceKey = null; this.styleKey = null; }

  // Cheapest update that satisfies the change: uniforms only, new GLSL, or new source (tiles refetched).
  sync({ sourceKey, makeSource, settings, bandStats, extent }) {
    const sk = styleKey(settings, bandStats);
    if (!this.layer) {
      this.layer = new ol.layer.WebGLTile({ source: makeSource(), style: buildStyle(settings, this.colormaps, bandStats), extent });
    } else {
      if (sourceKey !== this.sourceKey) this.layer.setSource(makeSource());
      if (sk !== this.styleKey) this.layer.setStyle(buildStyle(settings, this.colormaps, bandStats));
      else this.layer.updateStyleVariables(styleVariables(settings, bandStats));
    }
    this.sourceKey = sourceKey;
    this.styleKey = sk;
    return this.layer;
  }
}

// Composite every layer canvas of an OL map into one PNG blob (OL's documented export recipe).
export function mapToPng(map) {
  return new Promise((resolve, reject) => {
    map.once('rendercomplete', () => {
      const [w, h] = map.getSize();
      const out = Object.assign(document.createElement('canvas'), { width: w, height: h });
      const ctx = out.getContext('2d');
      for (const c of map.getViewport().querySelectorAll('.ol-layer canvas, canvas.ol-layer')) {
        if (!c.width) continue;
        ctx.globalAlpha = Number(c.parentNode.style.opacity || c.style.opacity || 1);
        ctx.setTransform(...(c.style.transform.match(/^matrix\(([^)]*)\)/)?.[1].split(',').map(Number)
          ?? [c.width / w, 0, 0, c.height / h, 0, 0]));
        ctx.drawImage(c, 0, 0);
      }
      try { out.toBlob((b) => (b ? resolve(b) : reject(new Error('empty canvas'))), 'image/png'); }
      catch (e) { reject(e); }
    });
    map.renderSync();
  });
}
