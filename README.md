# Agri-Energy Decision Support System: prototype engine

Copyright (c) 2026 Hao Ni. All rights reserved.

For a tube well anywhere in mainland Portugal, the engine

1. extracts the agricultural parcels inside the well's 150 m command zone
   (EuroCrops Portugal 2021, EPSG:3763);
2. queries NASA POWER at the cluster centroid and sizes the irrigation demand
   and pump power;
3. selects pumps from the IDCOL catalogue and computes the daily PV surplus;
4. selects the most profitable Productive Use of Energy (PUE) asset and its
   simple payback, or returns a structured "no investment" result.

All four tasks are implemented, tested and evaluated.

## Quick start

Developed and tested on Python 3.10.

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

python run_demo.py                                        # valid and invalid coordinates
python dss_pipeline.py --lat 38.07759 --lon -7.96330      # one well, Tasks 1-4
python dss_pipeline.py --lat 38.07759 --lon -7.96330 --demand-based --json

pytest --cov=dss --cov=dss_pipeline                       # tests with coverage (offline)
ruff check . && mypy dss dss_pipeline.py run_demo.py      # style and types
```

**Data.** Download `PT.zip` from [Zenodo record 10118572](https://zenodo.org/records/10118572/files/PT.zip)
(MD5 `3dba9c89c559b34d57acd286505bcb66`) and place it at `data/raw/PT.zip`.
It is read in place, without unzipping (GDAL `/vsizip/`). An unzipped
shapefile, or any layer with the same schema, can be passed with `--data`.
Tests that need the layer are skipped when it is absent.

## Result at the demonstration site

`python dss_pipeline.py --lat 38.07759 --lon -7.96330` prints:

```text
Well at (38.07759, -7.96330)
Task 1  Cluster: 3 parcels, 1.169 ha inside 150 m (Cereals & Grains 0.754 ha, Fallback Field Crop 0.415 ha)
Task 2  Solar: annual PSH 4.90 kWh/m2/day, design month DEC (2.11); source api
        Water demand 38.85 m3/day; required pump power 1.28 kW
Task 3  Pumps: 1 x Tier 1 (3.7 kW motor, 5 kWp PV, USD 7,500)
        E_PV 18.39 - E_pump 22.20 -> E_excess 0.00 kWh/day (rated consumption)
        Months with surplus: MAY, JUN, JUL, AUG
Task 4  insufficient_surplus: Surplus of 0.00 kWh/day is below the daily load of every compatible asset (lowest 10 kWh/day).
        Seasonal screening: no asset feasible in any month
```

The brief's example point (38.015, -7.865) has no parcel within 150 m in the
supplied sample and raises `EmptyClusterError`; the demonstration site is the
nearest location satisfying a fixed rule ([ASSUMPTIONS.md, A4](ASSUMPTIONS.md#a4-sparse-dataset-and-the-briefs-example-point)).

`python run_demo.py` runs every outcome the engine can return. The vegetable and
pasture sites are selected by fixed rules from the 300 random wells of the
[Task 4 evaluation](evaluation/results/task4/REPORT.md) and reproduce its rows.

| Case | Scenario | Outcome |
|---|---|---|
| Demonstration site: cereals and fallback crops | Brief's parameters | `insufficient_surplus` (no surplus) |
| Demonstration site | Demand-based consumption | Dryer, payback 26.3 years |
| Vegetable site, 0.50 ha (shortest payback in the batch) | Demand-based consumption | Dryer, payback 11.2 years; cold room feasible in June-August |
| Pasture-only site (nearest to the demonstration site) | Brief's parameters | `no_compatible_crops` |
| Brief's example point, central Lisbon, Atlantic Ocean | - | `EmptyClusterError` |
| Latitude 95 | - | `DataValidationError` |

## Three findings that shape the results

* **The brief's HCAT table matches no parcel.** Every code in the data starts
  with `33` (agriculture in the EAGLE matrix), so a mapping table was built
  from the HCAT hierarchy and audited code by code ([A1](ASSUMPTIONS.md#a1-hcat-codes-and-crop-archetypes)).
* **NASA POWER returns MJ/m²/day, not kWh/m²/day.** Units are converted from
  the response metadata; reading 17.65 as kWh would reverse every Task 3-4
  conclusion ([A5](ASSUMPTIONS.md#a5-nasa-power-units)).
* **With the brief's formulas the annual surplus is zero everywhere in
  mainland Portugal.** A surplus needs about 5.9 peak sun hours for every pump
  tier; Portugal receives 4.4-5.0. The headline result is therefore "no
  investment", reported as a structured outcome, and the engine adds a monthly
  screening and a demand-based consumption scenario
  ([A7](ASSUMPTIONS.md#a7-the-annual-surplus-is-zero-everywhere),
  [A13](ASSUMPTIONS.md#a13-demand-based-pump-consumption-sensitivity)). Even
  then the catalogue's PV arrays cannot power the cold room or the mill
  anywhere in mainland Portugal; only the dryer is reachable. This is a limit
  of the catalogue, not of the model: with 20 kWh/day of surplus, a 1 ha
  vegetable cluster pays back a cold room in 2.0 years
  ([Task 4 evaluation](evaluation/results/task4/REPORT.md), section 2).

## Where to find each rubric criterion

| Criterion | Where to look |
|---|---|
| **Code structure and modularity** | One module per concern under `dss/` (ETL `spatial`, API `solar`, sizing `hydraulics` and `energy`, economics `economics`); `dss_pipeline.py` only orchestrates. Reference tables are data (`reference_tables.py`), filtered programmatically rather than with nested conditionals. Configuration is frozen, validated dataclasses (`config.py`). Type annotations and Google-style docstrings throughout, enforced by ruff and mypy. |
| **Geospatial and dimensional rigour** | `PT.zip` read in place; reprojection to EPSG:3763 before buffering, R-tree spatial index, clipping to the buffer (`dss/spatial.py`); CRSs, indexing and units are documented in the `dss_pipeline.py` docstring. Power and energy are kept apart in every name (`kW`, `kWp`, `kWh/day`). Planar areas agree with declared areas to 0.009 % (median) and with geodesic areas to 0.04 % (P99). |
| **Defensive engineering** | `EmptyClusterError` with the exact required message, verified on valid and invalid coordinates (`run_demo.py`, `tests/test_spatial.py`, `tests/test_integration_spatial.py`). NASA POWER client with timeout, retries with back-off, in-memory and disk cache, unit and fill-value checks (`dss/solar.py`). Zero surplus and zero compatible crops return a status and no payback (`dss/economics.py`). |
| **Reasoning and assumptions** | [`ASSUMPTIONS.md`](ASSUMPTIONS.md): 13 decisions with evidence and effect, and two findings about the brief's reference data. Monte Carlo extension: `dss_pipeline.py` docstring. |

## Suggested reading order

1. `dss_pipeline.py`: the module docstring explains the architecture, CRSs,
   units, error handling and the Monte Carlo extension; the code shows the
   whole chain.
2. [`ASSUMPTIONS.md`](ASSUMPTIONS.md): the decisions behind the results.
3. `python run_demo.py`: the pipeline on valid and invalid coordinates.
4. The evaluation reports below, for the evidence behind each task.

## Evaluation

Each task has an evaluation script that writes a self-contained report with
figures and CSV tables. The checks compare the engine with independent hand
calculations, external data and physical bounds; the findings are written
from the data.

| Report | Checks | What it establishes |
|---|---|---|
| [Task 1](evaluation/results/task1/REPORT.md) | 13 / 13 | Classification audit, geometric accuracy, robustness over 1,000 random points, performance (3 ms per query), sensitivity to area accounting and radius |
| [Task 2](evaluation/results/task2/REPORT.md) | 7 / 7 | Live NASA POWER and unit checks, PVGIS cross-check (-5.1 % to +1.8 %), FAO-56 plausibility of the demand coefficients, pump catalogue consistency, live error handling |
| [Task 3](evaluation/results/task3/REPORT.md) | 5 / 5 | Hand recomputation, analytical surplus threshold, 300-well batch with invariants, multiple-pump strategies, sensitivity |
| [Task 4](evaluation/results/task4/REPORT.md) | 4 / 4 | Hand recomputation, decision logic across crop mixes, every no-investment path, 300-well batch, O&M sensitivity, cold-room capacity |

```bash
python -m evaluation.task1_spatial            # offline, about 1-2 min
python -m evaluation.task2_solar_hydraulics   # needs internet (NASA POWER, PVGIS)
python -m evaluation.task3_energy             # needs internet (NASA POWER)
python -m evaluation.task4_economics          # needs internet (NASA POWER)
```

## Repository layout

```text
dss_pipeline.py      End-to-end pipeline (Tasks 1-4), Python API and CLI
run_demo.py          Runner: valid and invalid coordinates
ASSUMPTIONS.md       Design decisions, evidence and effect on results
dss/                 Engine package, one module per concern
  config.py            Paths, CRSs and validated parameter dataclasses
  reference_tables.py  Tables 1-3 of the brief
  schema.py            Source and output column names
  exceptions.py        EmptyClusterError, SolarDataError, DataValidationError
  validation.py        Shared input validation
  taxonomy.py          HCAT codes -> crop archetypes
  spatial.py           Task 1: parcel ingestion, buffering, clustering
  solar.py             Task 2: NASA POWER client
  hydraulics.py        Task 2: water demand and pump power
  energy.py            Task 3: pump selection and energy balance
  economics.py         Task 4: PUE selection and payback
  resources/           HCAT mapping and label corrections (CSV, with rationale)
tests/               Unit, integration (needs the data) and live-API tests
evaluation/          One evaluation script per task and the generated reports
```

## Engineering practice

* Style: PEP 8 and the Google Python Style Guide, enforced by ruff (pydocstyle,
  annotations, bugbear, import order); mypy with `disallow_untyped_defs`.
* Tests: pytest with exact hand-calculated values, synthetic layers with known
  geometry, fake HTTP sessions for every failure mode, and integration tests on
  the real layer. Live NASA POWER tests run with
  `DSS_RUN_NETWORK_TESTS=1 pytest -m network`.
* Continuous integration: ruff, mypy and pytest on every push
  (`.github/workflows/ci.yml`).
* Reproducibility: pinned dependency ranges, a fixed random seed, and the
  software versions, git commit and input checksums recorded in every report.
