# Bundled map data

## `us-states.json`

State boundaries for the topology page's geographic layout.

- **Source:** [Natural Earth](https://www.naturalearthdata.com/) 1:110m
  admin-1 states and provinces, filtered to the United States.
- **Licence:** public domain. Natural Earth places no restrictions on use.
- **Processing:** simplified with Douglas-Peucker (epsilon 0.09°, roughly
  10 km — invisible at dashboard zoom levels), coordinates rounded to two
  decimal places, slivers and small offshore islands dropped, and Aleutian
  rings west of 170°W removed so nothing crosses the antimeridian. The
  result is ~17 KB.

It is bundled rather than fetched from a tile server on purpose: these
deployments are frequently air-gapped, and a basemap that only works with
internet access is a basemap that fails exactly when it matters.

### Regenerating

```bash
curl -LO https://raw.githubusercontent.com/nvkelso/natural-earth-vector/master/geojson/ne_110m_admin_1_states_provinces.geojson
python3 scripts/build_basemap.py ne_110m_admin_1_states_provinces.geojson web/assets/data/us-states.json
```

To cover sites outside the United States, extend the script to read
`ne_110m_admin_0_countries.geojson` as well; the renderer only needs
`{"states": [{"n": name, "r": [[[lon, lat], ...]]}]}`.
