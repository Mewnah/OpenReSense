# OpenReSense catalog report (2026-11-05)

Total entries: **878** in 10 files. Cross-file merge removed 161 duplicate entries (same id or same URL/dataset); one entry (KASA KPLO mission site) was added.

## Per file
| file | entries |
|---|---|
| earth-agencies | 160 |
| nasa-pds-astro | 101 |
| polar | 96 |
| earth-platforms | 91 |
| sun-telescopes | 89 |
| outer-solar-system | 85 |
| mars | 80 |
| inner-small-bodies | 66 |
| moon | 60 |
| international-planetary | 50 |

## Per kind
| kind | entries |
|---|---|
| archive | 252 |
| dataset | 232 |
| portal | 200 |
| mission-site | 61 |
| catalog | 51 |
| tool | 36 |
| api | 21 |
| map-service | 16 |
| raw-feed | 9 |

## Per target (all 75 vocab targets; entries may list several)
| target | entries |
|---|---|
| sun | 77 |
| mercury | 83 |
| venus | 94 |
| earth | 360 |
| moon | 172 |
| mars | 208 |
| phobos | 65 |
| deimos | 54 |
| ceres | 75 |
| vesta | 61 |
| psyche | 3 |
| lutetia | 10 |
| steins | 10 |
| gaspra | 11 |
| ida | 11 |
| mathilde | 11 |
| annefrank | 2 |
| braille | 2 |
| dinkinesh | 3 |
| donaldjohanson | 3 |
| eros | 13 |
| itokawa | 12 |
| ryugu | 15 |
| bennu | 14 |
| didymos | 12 |
| dimorphos | 11 |
| toutatis | 5 |
| asteroids | 111 |
| halley | 13 |
| borrelly | 4 |
| wild-2 | 9 |
| tempel-1 | 10 |
| hartley-2 | 9 |
| 67p | 18 |
| 3i-atlas | 10 |
| comets | 104 |
| jupiter | 115 |
| io | 86 |
| europa | 88 |
| ganymede | 80 |
| callisto | 73 |
| amalthea | 3 |
| himalia | 2 |
| saturn | 104 |
| mimas | 53 |
| enceladus | 68 |
| tethys | 54 |
| dione | 55 |
| rhea | 55 |
| titan | 99 |
| hyperion | 12 |
| iapetus | 55 |
| phoebe | 12 |
| janus | 4 |
| epimetheus | 4 |
| prometheus | 4 |
| pandora | 4 |
| pan | 4 |
| uranus | 78 |
| miranda | 8 |
| ariel | 6 |
| umbriel | 6 |
| titania | 6 |
| oberon | 6 |
| neptune | 76 |
| triton | 56 |
| proteus | 4 |
| nereid | 4 |
| pluto | 69 |
| charon | 47 |
| nix | 3 |
| hydra | 3 |
| arrokoth | 13 |
| kbos | 26 |
| rings | 19 |

## Top 40 agencies
| agency | entries |
|---|---|
| NASA | 359 |
| USGS | 108 |
| ESA | 82 |
| JPL | 81 |
| PDS | 72 |
| JAXA | 42 |
| NSIDC | 34 |
| NOAA | 25 |
| NSF | 23 |
| Europlanet | 23 |
| ASU | 17 |
| EC | 16 |
| DLR | 15 |
| ISRO | 15 |
| CNSA | 14 |
| GSFC | 14 |
| LPI | 14 |
| USRA | 13 |
| UMD | 10 |
| NAOC | 10 |
| IPAC | 10 |
| STScI | 10 |
| JHUAPL | 10 |
| SETI | 10 |
| ASI | 8 |
| EUMETSAT | 7 |
| ISAS | 7 |
| CNRS | 7 |
| NAOJ | 6 |
| NMSU | 6 |
| PGC | 6 |
| CSA | 5 |
| CNES | 5 |
| Google | 5 |
| UArizona | 5 |
| NRL | 5 |
| LMSAL | 5 |
| ECMWF | 4 |
| NRCan | 4 |
| JRC | 4 |

## Link check (scripts/check_links.py -> cache/link_report.json)
Checked 1349 URLs; 49 failures in the final run. Remaining failures are mostly bot protection
(HTTP 403/418/429: LPI, ASF, CRESDA, Tianditu, Quantarctica, Gemini, NCAP, Historic England, BlackSky, Firefly),
local TLS-chain errors on hosts that load with verification off (ICGC, Theia, GISTDA, SBN OSIRIS-REx, DLR HRSC team,
Caltech Murray lab, CLEP, NISAR ASF, NAIC), or transient outages (ESA ESASky/Rosetta image archive 503, Flickr 502,
PDS Rings holdings and LASP MAVEN timeouts, SBN 429). Fixed this pass: SWOT, Brazil Data Cube (now data.inpe.br/bdc),
FABDEM (DOI), DMI ice charts, EMIT dead link. Unconfirmed from here: inaf-cassis-stereo-archive, fabdem.

## Remaining gaps
- Thin targets (<3 sources): annefrank, braille, himalia, janus, epimetheus, prometheus, pandora, pan, ariel, umbriel, titania, oberon, proteus, nereid (covered only through Voyager/Cassini/New Horizons mission archives).
- Not catalogued: Russian/Soviet planetary archives, Pioneer Venus, Ranger, Chang'e-6/7 releases, many individual VESPA services, many national Earth mapping agencies.
- Descriptions, resolutions and formats are curator knowledge; only URL reachability was machine-checked. Astropedia entries cannot be machine-verified (SPA returns 200 for any path).

## Gap-fill round (2026-11-05)
Total entries: **1014** in 15 files. Five curator files were added (vespa-services 23, national-eo 58, heritage-new-missions 21, airborne-campaigns 26, teams-communities 8 after dedup). Two duplicates were merged into existing entries and removed from the new files: ru-planeta into ru-vniiem-electro, planetarypy-community into planetarypy. Validator: 0 errors. Link check of the new entries (225 URLs): 4 failures, all local TLS-chain errors on hosts that load with verification off (vnsc.org.vn, sparrso.gov.bd, narss.sci.eg, rds.icimod.org); kept.

### Per file
| file | entries |
|---|---|
| earth-agencies | 160 |
| nasa-pds-astro | 101 |
| polar | 96 |
| earth-platforms | 91 |
| sun-telescopes | 89 |
| outer-solar-system | 85 |
| mars | 80 |
| inner-small-bodies | 66 |
| moon | 60 |
| national-eo | 58 |
| international-planetary | 50 |
| airborne-campaigns | 26 |
| vespa-services | 23 |
| heritage-new-missions | 21 |
| teams-communities | 8 |

### Per kind
| kind | entries |
|---|---|
| archive | 287 |
| dataset | 253 |
| portal | 245 |
| mission-site | 81 |
| catalog | 58 |
| tool | 39 |
| api | 26 |
| map-service | 16 |
| raw-feed | 9 |

### Per target (entries may list several)
| target | entries |
|---|---|
| earth | 450 |
| mars | 222 |
| moon | 182 |
| jupiter | 123 |
| saturn | 111 |
| comets | 108 |
| asteroids | 104 |
| titan | 103 |
| venus | 102 |
| europa | 91 |
| sun | 89 |
| io | 89 |
| mercury | 87 |
| uranus | 84 |
| neptune | 83 |
| ganymede | 83 |
| ceres | 80 |
| callisto | 76 |
| pluto | 70 |
| enceladus | 70 |
| phobos | 67 |
| vesta | 63 |
| triton | 59 |
| rhea | 59 |
| dione | 58 |
| iapetus | 57 |
| tethys | 56 |
| mimas | 55 |
| deimos | 55 |
| charon | 47 |
| kbos | 27 |
| rings | 22 |
| 67p | 19 |
| hyperion | 15 |
| halley | 15 |
| ryugu | 15 |
| bennu | 14 |
| phoebe | 14 |
| eros | 13 |
| arrokoth | 13 |
| didymos | 12 |
| itokawa | 12 |
| miranda | 11 |
| 3i-atlas | 11 |
| dimorphos | 11 |
| ida | 11 |
| gaspra | 11 |
| mathilde | 11 |
| lutetia | 10 |
| steins | 10 |
| tempel-1 | 10 |
| titania | 9 |
| umbriel | 9 |
| hartley-2 | 9 |
| wild-2 | 9 |
| ariel | 8 |
| oberon | 8 |
| pan | 7 |
| pandora | 7 |
| janus | 7 |
| prometheus | 7 |
| epimetheus | 7 |
| proteus | 6 |
| nereid | 6 |
| toutatis | 5 |
| borrelly | 4 |
| himalia | 3 |
| dinkinesh | 3 |
| donaldjohanson | 3 |
| psyche | 3 |
| amalthea | 3 |
| nix | 3 |
| hydra | 3 |
| braille | 2 |
| annefrank | 2 |
