// node --test tests/globe_helpers.test.mjs  (run through tests/test_globe_js.py)
import test from 'node:test';
import assert from 'node:assert/strict';
import { heightBase, isDEM } from '../web/js/globe.js';

test('radius-valued DEMs (LOLA LDEM) get the reference radius subtracted', () => {
  assert.equal(heightBase({ offsets: [1737400], scales: [0.5], units: [''] }, 1737400), 1737400);
  assert.equal(heightBase({ offsets: [1737.4], units: ['km'] }, 1737400), 1737400);
});

test('plain height DEMs keep their heights', () => {
  assert.equal(heightBase({ offsets: [0], units: ['m'] }, 3396000), 0);
  assert.equal(heightBase({ offsets: [-100], units: ['m'] }, 3396000), 0);
  assert.equal(heightBase({}, 3396000), 0);
});

test('PDS DEMs without units are recognised by dataset id', () => {
  const info = { count: 1, lonlat_bounds: [-180, -90, 180, 90], units: [''], metadata: { '': { DATA_SET_ID: '"MGS-M-MOLA-5-MEGDR-L3-V1.0"' } } };
  assert.ok(isDEM({ name: 'megt90n000eb.img', info }));
  assert.ok(!isDEM({ name: 'm01.img', info: { ...info, metadata: {} } }));
});
