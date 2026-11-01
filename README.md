# Storm Tracker

**Every tropical cyclone on Earth, on one 3D globe.**

Live: **https://nwfella.github.io/storm-tracker/**

A single self-contained HTML file — no build step to run it, no CDN, no framework,
no API keys, and no network calls from your browser. The hurricane data is baked
into the page itself, refreshed hourly by a GitHub Action.

![globe](https://img.shields.io/badge/data%20refresh-hourly-4cc4ff) ![deps](https://img.shields.io/badge/dependencies-0-3fd39c) ![size](https://img.shields.io/badge/index.html-~132%20KB-8ea6bf)

## What it does

- **Real 3D globe** rendered with hand-written WebGL — a textured sphere, camera-following
  key light, fresnel rim and an additive atmosphere shell. No three.js.
- **Drag to spin, scroll to zoom, click to fly.** Double-click any ocean to zoom in.
- **Pick a storm and the camera flies to it** — from the list, from the marker, or by
  clicking a search result — then opens its detail panel.
- **Observed track, forecast track, uncertainty cone and gale-radius wind field** drawn
  on the sphere with correct horizon occlusion (far-side tracks never bleed across the face).
- **Search anything**: a city, a basin name, a storm name, or raw `lat,lon`.
- Hourly snapshots via GitHub Actions; the footer shows how old the data is and turns amber
  past 3 hours, red past 8.

## Data sources

| Source | Basins | Contribution |
| --- | --- | --- |
| [NOAA NHC](https://www.nhc.noaa.gov/) | North Atlantic, East & Central Pacific | Position, 1-min wind, pressure, movement, forecast track parsed from the TCM advisory text |
| [JMA](https://www.jma.go.jp/bosai/typhoon/) (RSMC Tokyo) | West Pacific | Position, 10-min wind, pressure, 5-day forecast with probability circles, official past track |
| [GDACS](https://www.gdacs.org/) (EU JRC) | Global catch-all (JTWC / BoM / IMD) | Position, wind radii, uncertainty cone, alert level |

Records are merged and de-duplicated, preferring the basin authority's numbers and
keeping GDACS attributes (alert level, cone, exposed countries) as a sidecar.

**Averaging matters.** US basins publish 1-minute sustained wind; JMA publishes
10-minute sustained wind. The page shows the reported value verbatim and derives the
Saffir–Simpson category from the 1-minute equivalent, converting WPac values at
1.12× (Harper et al. 2010). It says so in the panel. This is a visualisation, not a
warning service — always follow your national weather service.

## Architecture

```
index.html              the whole app: HTML + CSS + raw WebGL, data inlined
template.html           source with /*__DATA__*/ placeholder
data/storms.json        merged live snapshot (the collector's output)
data/world.json         127 Natural Earth land polygons, ~54 KB, baked
scripts/fetch_storms.py NHC + JMA + GDACS -> data/storms.json (stdlib only)
scripts/gen_world.py    Natural Earth GeoJSON -> compact flat-array world.json
scripts/build.py        inline data into template.html -> index.html
scripts/verify.js       gate: syntax check + headless-Chrome render assertions
.github/workflows/refresh.yml   hourly bake, gate, commit
```

The page paints its own equirectangular Earth texture at runtime from the baked
coastline polygons (shelf glow → land fill → bright coastline, plus graticule and
tropics). That is why there are no image downloads and why it works offline from a
`file://` open.

## Run it locally

```bash
python scripts/fetch_storms.py     # pull live data (no keys needed)
python scripts/build.py            # inline it into index.html
node scripts/verify.js             # syntax + render gate
open index.html                    # or: python -m http.server 8731
```

`scripts/gen_world.py` only needs re-running if you want to change the coastline
detail level; it expects `scripts/ne_110m_land.geojson`.

## Refreshing

The GitHub Action runs at `12 * * * *` (hourly, off the top of the hour), pulls all
three feeds, rebuilds `index.html`, gates it, and commits only if something changed.
If every upstream is unreachable the collector exits non-zero, so a total outage can
never overwrite good data with an empty globe.

## License

MIT — see [LICENSE](LICENSE).

## Donations

If this is useful to you: `0xe09e6c33b444d246F1005BdCd404ff33Cb709EcA` — any chain, any
token. Thank you.
