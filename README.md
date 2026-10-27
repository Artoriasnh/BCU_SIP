# Agri-Energy Decision Support System (DSS) — Prototype Engine

Copyright (c) 2026 Hao Ni. All rights reserved.

Prototype computational engine that, for a given location in Portugal:

1. extracts the agricultural parcels inside a 150 m communal tube-well command zone
   (EuroCrops Portugal 2021);
2. sizes the irrigation water demand and the solar pumping system using NASA POWER
   solar climatology;
3. estimates surplus PV energy; and
4. appraises the most profitable Productive Use of Energy (PUE) asset.

> Work in progress — this README is completed as each task is delivered.

## Data

The parcel layer is **not** stored in this repository (the `.dbf` alone is ~320 MB).

1. Download `PT.zip` from Zenodo record 10118572 (EuroCrops v10):
   <https://zenodo.org/records/10118572/files/PT.zip>
   (expected md5: `3dba9c89c559b34d57acd286505bcb66`)
2. Unzip it so that the shapefile sits at `data/raw/PT/PT_2021_EC21.shp`.

The engine accepts any parcel layer with the same schema (e.g. the full IFAP layer);
pass its path to `ParcelLayer.from_file`.

## Quick start

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
pytest                 # unit tests (integration tests auto-skip if data are missing)
ruff check . && mypy dss
```

## Repository layout

```
dss/                 Engine package (one module per concern)
  config.py          Paths, CRSs and tunable parameters
  schema.py          Source and output column names
  exceptions.py      Domain exceptions (EmptyClusterError, ...)
  reference_tables.py  Reference parameters from the assessment brief
  validation.py      Shared input validation
  taxonomy.py        HCAT -> brief crop archetype classification
  spatial.py         Task 1: parcel ingestion, buffering and clustering
  solar.py           Task 2: NASA POWER client (timeouts, retries, caching, units)
  hydraulics.py      Task 2: water demand and required pump power
  resources/hcat_mapping.csv   Versioned HCAT mapping (single source of truth)
tests/               pytest suite (unit, integration and optional live-API tests)
```

Live NASA POWER tests are skipped by default; run them with
`DSS_RUN_NETWORK_TESTS=1 pytest -m network`.
