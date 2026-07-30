# Bundled map data

## `us-states.json`

State boundaries for the topology page's geographic layout.

- **Source:** [Natural Earth](https://www.naturalearthdata.com/) 1:110m —
  admin-1 states and provinces (internal borders) plus admin-0 countries
  (the national outline), both filtered to the United States. The two are
  kept separate so a coastline can be drawn at a different weight from a
  state line.
- **Licence:** public domain. Natural Earth places no restrictions on use.
- **Processing:** simplified with Douglas-Peucker (epsilon 0.09°, roughly
  10 km — invisible at dashboard zoom levels), coordinates rounded to two
  decimal places, slivers and small offshore islands dropped, and Aleutian
  rings west of 170°W removed so nothing crosses the antimeridian. The
  result is ~22 KB.

It is bundled rather than fetched from a tile server on purpose: these
deployments are frequently air-gapped, and a basemap that only works with
internet access is a basemap that fails exactly when it matters.

### Regenerating

```bash
base=https://raw.githubusercontent.com/nvkelso/natural-earth-vector/master/geojson
curl -LO $base/ne_110m_admin_1_states_provinces.geojson
curl -LO $base/ne_110m_admin_0_countries.geojson
python3 scripts/build_basemap.py \
  ne_110m_admin_1_states_provinces.geojson \
  web/assets/data/us-states.json \
  ne_110m_admin_0_countries.geojson
```

To cover sites outside the United States, widen the country filter in the
script. The renderer only needs
`{"country": [[[lon, lat], ...]], "states": [{"n": name, "r": [...]}]}` —
`country` carries the fill and coastline, `states` the internal borders.
