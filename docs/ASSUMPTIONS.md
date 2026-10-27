# Assumption Register

Copyright (c) 2026 Hao Ni. All rights reserved.

This register records every place where the brief, the supplied data and the
external API disagree or leave a gap, together with the decision taken and its
evidence. **Guiding principle:** headline results use only the parameters given
in the brief, so that results are comparable across candidates. Any parameter
not given in the brief appears only in clearly labelled sensitivity runs, with
its source cited.

| ID | Topic | Status |
|----|-------|--------|
| A1 | HCAT code mapping | Implemented (Task 1) |
| A2 | Definition of an agricultural parcel | Implemented (Task 1) |
| A3 | Parcels partly inside the buffer | Implemented (Task 1) |
| A4 | Sparse dataset and the brief's example location | Implemented (Task 1) |
| A5 | NASA POWER units | Planned (Task 2) |
| A6 | NASA POWER query location | Planned (Task 2) |
| A7 | Annual surplus energy is always zero | Planned (Tasks 3-4) |
| A8 | Pump selection and multiple pumps | Planned (Task 3) |
| A9 | Cold-room capacity | Planned (Task 4) |
| A10 | Operating costs and "net" uplift | Planned (Task 4) |
| A11 | Other uplift conventions | Planned (Task 4) |
| A12 | Horizontal irradiance used as PSH | Planned (Task 2) |

---

## A1 — HCAT code mapping

**Issue.** The brief maps "the first two digits of `EC_hcat_c`" to archetypes
10/20/30/50. All 100,000 codes in the supplied layer start with `33`, so the
brief's table matches no parcel. An exhaustive scan of every two-digit window of
the 10-digit code found no position reproducing the brief's semantics.

**Evidence.** The HCAT-ID starts with the position of agriculture in the EAGLE
land-cover matrix (`33`), followed by hierarchical levels (Schneider et al.,
EuroCrops, arXiv:2106.08151). A code's parent is obtained by zeroing its
right-most non-zero digit.

**Decision.** A versioned mapping table, `dss/resources/hcat_mapping.csv`, assigns
HCAT *branches* (prefixes) to archetypes by longest-prefix match, each with a
written rationale. Codes matching no rule, and missing codes, fall back to the
brief's "Other / NaN" archetype. Fallow land (`330111`) and generic "other arable
crops" (`330199`) are kept as fallback, following the brief's rule literally;
this slightly overestimates water demand. All 79 codes present in the data are
locked by a regression test (`tests/fixtures/observed_hcat_codes.csv`).

## A2 — Definition of an agricultural parcel

**Issue.** About 32 % of polygons are not farmland (forest, built-up land, water
bodies, shrubland). Counting them would let a buffer over a lake "contain
parcels" and defeat `EmptyClusterError`.

**Decision.** A parcel is agricultural if its national land-cover category
`CAT_OCU_DE` is *Superfície agrícola* **and** its HCAT branch is not excluded
(forestry `3306*`, including cork-oak stands; non-grazable shrubland
`3303080700`). Only agricultural parcels enter the cluster and the empty-cluster
test, consistent with the required message "out of agricultural domain".

## A3 — Parcels partly inside the buffer

**Issue.** The brief asks to "identify all intersecting parcels" and calculate
their areas, but also defines the buffer as a ~7 ha tube-well command zone. A
15.5 ha parcel crossing a 7.07 ha disc illustrates the ambiguity.

**Decision.** Headline results use the area **inside** the buffer (areal
weighting): demand is an extensive quantity proportional to area and crops are
assumed uniform within a parcel (Goodchild & Lam, 1980; Lam, 1983). Whole-parcel
areas are reported alongside for comparison.

## A4 — Sparse dataset and the brief's example location

**Issue.** The brief's valid example (38.015, -7.865) has no parcel within 150 m
in the supplied layer; the nearest parcel is ~1.4 km away.

**Evidence.** The supplied file is intact (header lengths match file sizes; md5
matches Zenodo record 10118572, EuroCrops v10). It holds exactly 100,000 polygons
sorted by `OSA_ID`, whose values reach ~2.08 million, so it is a sparse (~5 %)
subset of the national layer. Every candidate receives the same file.

**Decision.** Use the supplied data unchanged. The notebook shows the brief's
point raising `EmptyClusterError` (with this explanation) and demonstrates the
pipeline at the nearest well-populated location, (38.2285, -7.8211). The parcel
path is configurable, so the engine runs unchanged on the full IFAP layer.

## A5 — NASA POWER units *(Task 2)*

The API returns `ALLSKY_SFC_SW_DWN` in **MJ/m²/day** for `community=AG` (see
`parameters.ALLSKY_SFC_SW_DWN.units`), not kWh/m²/day as the brief states. Values
are converted using the unit string in the response metadata
(MJ → ÷ 3.6; kWh → × 1). Unknown units raise an error; the fill value (-999) is
treated as missing.

## A6 — NASA POWER query location *(Task 2)*

One query per cluster, at the area-weighted centroid of the clipped parcels
(computed in EPSG:3763, then converted to EPSG:4326). Results are cached in
memory by coordinate. The design month is the minimum monthly value at that
location, not a fixed month.

## A7 — Annual surplus energy is always zero *(Tasks 3-4)*

`E_excess > 0` requires `PSH > 8 · P_motor / PV_kW` ≈ 5.9 kWh/m²/day for every
catalogue tier, a threshold set by the catalogue alone. The annual mean GHI in
mainland Portugal is ~4.7–5.1 kWh/m²/day, so the strict annual formula always
yields zero surplus and no PUE investment. Headline results keep the strict
formula; a monthly (seasonal) analysis shows the months in which surplus is
positive. (Real IDCOL systems use a PV/motor ratio of ~2.3 versus ~1.36 in the
catalogue.)

## A8 — Pump selection and multiple pumps *(Task 3)*

Table 2 selects the smallest tier whose motor power is at least the required
power. Task 3.1 flags multiple pumps when `Q_day` exceeds Tier 4 capacity. Both
are honoured: multiple pumps are needed when `P_req > 11 kW` or
`Q_day > 850 m³/day`, with `n = max(⌈P_req/11⌉, ⌈Q_day/850⌉)`. Headline
configuration: *n identical units of the smallest tier covering `P_req/n`*
(pumps in parallel should be identical). A least-cost mixed combination is
reported as a sensitivity case. The delivery column is used only as a warning.

## A9 — Cold-room capacity *(Task 4)*

No cap: the brief's tip defines the uplift on the total harvested fresh weight
(Area × Yield) of compatible crops. "5 MT" is treated as a model name; the ratio
of harvest to capacity is reported. Evidence that capacity rarely binds: a ~2 t
solar cold room cools 200 kg/day at 94 % utilisation (~69 t/yr, ~34 turnovers).

## A10 — Operating costs and "net" uplift *(Task 4)*

The brief gives no O&M costs and defines payback as CAPEX / annual uplift, so the
headline O&M is zero. Sensitivity run: PUE O&M at 3 % of CAPEX per year (ColdHubs
case: USD 835/yr on USD 27,000 CAPEX).

## A11 — Other uplift conventions *(Task 4)*

One harvest per year; the milling fee applies to the whole cereal harvest; the
dryer premium applies to 15 % of the harvest of compatible crops (10, 20, 50);
pastures (30) and fallback crops are compatible with no PUE asset.

## A12 — Horizontal irradiance used as PSH *(Task 2)*

`ALLSKY_SFC_SW_DWN` is global horizontal irradiance; no tilt gain is applied,
which is conservative.

---

## References

- Schneider, M. et al. EuroCrops: a pan-European dataset for time series crop
  type classification. arXiv:2106.08151.
- Schneider, M. et al. (2023). EuroCrops: the largest harmonized open crop
  dataset across the European Union. *Scientific Data* 10. doi:10.1038/s41597-023-02517-0
- EuroCrops v10, Zenodo record 10118572 (PT.zip).
- Goodchild, M. F. & Lam, N. S.-N. (1980). Areal interpolation: a variant of the
  traditional spatial problem. *Geo-Processing* 1, 297–312.
- Lam, N. S.-N. (1983). Spatial interpolation methods: a review. *The American
  Cartographer* 10(2), 129–150.
- NASA POWER, Communities documentation: <https://power.larc.nasa.gov/docs/methodology/communities/>
- ESMAP, IDCOL Solar Irrigation Program (F. Rahman).
- REEEP / SOARING fact sheet: Solar cooling (ColdHubs case study).
