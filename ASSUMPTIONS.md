# Assumptions and design decisions

Copyright (c) 2026 Hao Ni. All rights reserved.

This register lists every point where the brief, the supplied data and the
external API disagree or leave a gap, the decision taken, the evidence for it,
and its effect on the results. Each entry points to the code that implements it
and to the evaluation that checks it.

**Principle.** Headline results use only the brief's parameters, so they are
comparable across candidates. A parameter the brief does not give appears only
in a labelled sensitivity run, with its source cited.

| ID | Topic | Decision | Effect on headline results |
|---|---|---|---|
| [A1](#a1-hcat-codes-and-crop-archetypes) | HCAT codes | Prefix mapping table built from the HCAT hierarchy; 4 label corrections | Makes classification possible at all |
| [A2](#a2-what-counts-as-an-agricultural-parcel) | Agricultural parcels | Land cover *and* HCAT branch filter | 67,726 of 100,000 polygons kept |
| [A3](#a3-parcels-crossing-the-buffer) | Parcels crossing the buffer | Area inside the buffer | Demand bounded by the 7.07 ha disc |
| [A4](#a4-sparse-dataset-and-the-briefs-example-point) | Sparse dataset | Data used unchanged; demonstration site chosen by rule | Brief's point raises `EmptyClusterError` |
| [A5](#a5-nasa-power-units) | NASA POWER units | Convert using the unit in the response | PSH 4.90, not 17.65 |
| [A6](#a6-where-and-how-nasa-power-is-queried) | NASA POWER query | Cluster centroid, cached | None (1-degree grid) |
| [A7](#a7-the-annual-surplus-is-zero-everywhere) | Zero annual surplus | Brief's formula kept; monthly screening added | Headline PUE decision: no investment |
| [A8](#a8-pump-selection-and-multiple-pumps) | Multiple pumps | Identical units; least cost as sensitivity | None in a 300-well sample |
| [A9](#a9-cold-room-capacity) | Cold-room capacity | No cap, as the brief's tip implies | None |
| [A10](#a10-operation-and-maintenance-costs) | O&M costs | Zero; 4.5 % of CAPEX as sensitivity | None |
| [A11](#a11-uplift-conventions) | Uplift conventions | One harvest a year; shares as in Table 3 | Defines the uplift |
| [A12](#a12-horizontal-irradiance-as-peak-sun-hours) | Irradiance plane | Horizontal irradiance used as PSH | Conservative |
| [A13](#a13-demand-based-pump-consumption-sensitivity) | Pump consumption | Rated power (brief); demand-based as sensitivity | None |

Two inconsistencies in the brief's reference data are reported as findings
rather than corrected: see [F1](#f1-pump-catalogue-delivery) and
[F2](#f2-water-demand-coefficients).

---

## A1. HCAT codes and crop archetypes

**Issue.** The brief classifies parcels by "the first two digits of
`EC_hcat_c`" (10, 20, 30, 50). Every one of the 100,000 codes in the supplied
layer starts with `33`, so the table matches no parcel. No other two-digit
window of the 10-digit code reproduces the brief's categories.

**Evidence.** HCAT codes are hierarchical: the first two digits give the
position of agriculture in the EAGLE land-cover matrix and are constant by
design; the following digits encode crop group, crop and variety (Schneider et
al., 2021, 2023).

**Decision.** `dss/resources/hcat_mapping.csv` maps HCAT branches (prefixes) to
the brief's archetypes, matched by longest prefix, with a written rationale per
rule (24 rules). Unmatched or missing codes fall back to the brief's
"Other / NaN" archetype. Where EuroCrops' harmonisation is wrong for Portugal,
`dss/resources/label_overrides.csv` corrects it, keyed on the HCAT code *and*
the national crop label: stone-pine nut stands (*pinhão*) are excluded as
largely rainfed agroforestry, like cork oak; strawberry tree (*medronho*)
becomes an orchard; dry pulses and peanuts become fallback crops.

**Effect.** Without the table no parcel can be classified. The overrides change
308 parcels, 279 of them stone-pine stands (3,190 ha).

**Where.** `dss/taxonomy.py`. All 79 codes present in the data are locked by a
regression test; every code, label combination and override is audited in the
[Task 1 evaluation](evaluation/results/task1/REPORT.md), section 1.

## A2. What counts as an agricultural parcel

**Issue.** About a third of the polygons are not farmland (forest, built-up
land, water, shrubland). Counting them would let a buffer over a lake "contain
parcels" and would defeat `EmptyClusterError`.

**Decision.** A parcel is agricultural if its national land-cover class
(`CAT_OCU_DE`) is *Superfície agrícola* and its HCAT branch is not excluded
(forestry `3306*`, including cork oak; non-grazable shrubland `3303080700`).

**Effect.** 67,726 of 100,000 polygons are kept. Only these enter the cluster
and the empty-cluster test, matching the required message "out of agricultural
domain".

**Where.** `dss/taxonomy.py` (`is_agricultural`); Task 1 evaluation, section 1.

## A3. Parcels crossing the buffer

**Issue.** The brief asks for "all intersecting parcels" but defines the buffer
as a tube well's command zone. A 15 ha parcel crossing the 7.07 ha disc would
otherwise contribute twice the disc's area.

**Decision.** Parcels are clipped to the buffer, and the area inside it drives
the water demand (areal weighting: demand is proportional to area and the crop
is assumed uniform within a parcel; Goodchild & Lam, 1980). Whole-parcel areas
are reported alongside.

**Effect.** Demand can never exceed that of the disc fully planted with the
thirstiest crop (12.8 kW). Whole parcels would add land far outside the command
zone: for one cluster in ten the whole-parcel area is at least 2.4 times the
clipped area (Task 1 evaluation, section 5).

**Where.** `dss/spatial.py`; Task 1 evaluation, sections 5-6.

## A4. Sparse dataset and the brief's example point

**Issue.** The brief's valid example (38.015, -7.865) has no parcel within
150 m in the supplied layer; the nearest is about 1.4 km away.

**Evidence.** The file is intact (MD5 matches Zenodo record 10118572). It holds
exactly 100,000 polygons whose identifiers reach about 2.08 million, i.e. a
sparse sample (about 5 %) of the national layer. All candidates receive the
same file.

**Decision.** The data are used unchanged; the brief's point raises
`EmptyClusterError`. The pipeline is demonstrated at (38.07759, -7.96330),
chosen by a fixed rule rather than by hand: the nearest location to the brief's
point with at least three parcels of at least two archetypes inside the
buffer. It lies 11.1 km away and holds cereals and fallback crops. The data
path is configurable, so the engine runs unchanged on the full national layer.

**Where.** `run_demo.py`; Task 1 evaluation, section 7.

## A5. NASA POWER units

**Issue.** The brief states that `ALLSKY_SFC_SW_DWN` is in kWh/m²/day. For
`community=AG` the API returns MJ/m²/day.

**Decision.** Values are converted using the unit string in the response
metadata (MJ: divide by 3.6; kWh: unchanged). An unknown unit raises
`SolarDataError`; the fill value -999 is rejected.

**Effect.** Annual PSH at the demonstration site is 4.90 kWh/m²/day. Reading
the raw 17.65 as kWh would inflate PV generation 3.6-fold and reverse every
Task 3-4 conclusion.

**Where.** `dss/solar.py`; Task 2 evaluation, section 1 (live check against
the metadata).

## A6. Where and how NASA POWER is queried

**Decision.** One request per cluster at the area-weighted centroid of the
clipped parcels (computed in EPSG:3763, converted to EPSG:4326), with a 10 s
timeout, retries with exponential back-off, an in-memory cache and an optional
disk cache used as an offline fallback. The design month is the lowest month at
that location.

**Evidence.** The irradiance source is the 1-degree SYN1DEG grid, so all points
in a cell receive the same values; NASA POWER agrees with PVGIS within
-5.1 % to +1.8 % along a north-south transect (Task 2 evaluation, sections 2-3).

**Where.** `dss/solar.py`, `dss_pipeline.py`.

## A7. The annual surplus is zero everywhere

**Issue.** With the brief's formulas, `E_excess > 0` requires

    PSH > (t_pump / derate) x (P_motor / PV_kWp) = 8 x P_motor / PV_kWp,

which is 5.87-6.00 kWh/m²/day for every catalogue tier. The threshold is set by
the catalogue alone. Annual means in mainland Portugal are 4.4-5.0, so the
annual surplus is always zero and Task 4 always returns "no investment".

**Decision.** Headline results keep the brief's formula and annual PSH, and
report the structured "no investment" result. Because the brief's overview asks
to "model seasonal energy generation", the energy balance and the PUE screening
are also reported for each month (feasibility only; monthly results are not
annualised into revenue, which would need parameters the brief does not give).

**Effect.** At the demonstration site a surplus appears in May-August. The
root cause is the catalogue's PV-to-motor ratio of about 1.36 kWp/kW: the
array is too small to run the motor at full power for six hours on an average
day.

**Where.** `dss/energy.py` (`surplus_threshold_psh`, `monthly_energy_balance`),
`dss/economics.py` (`feasible_assets_by_month`); Task 3 evaluation, section 2.

## A8. Pump selection and multiple pumps

**Issue.** Table 2 selects the smallest tier whose motor power covers the
required power; Task 3.1 flags multiple pumps when `Q_day` exceeds Tier 4
capacity. Neither says how several pumps are combined.

**Decision.** Both rules apply: several pumps are needed when
`P_req > 11 kW` or `Q_day > 850 m³/day`, with
`n = max(ceil(P_req / 11), ceil(Q_day / 850))`. Headline: `n` identical units
of the smallest tier covering `P_req / n`, since pumps in parallel are normally
identical so that they share the load at the same operating point. Sensitivity:
the cheapest mix of tiers. Below 11 kW both give the same single pump.

**Effect.** A 150 m buffer needs at most 12.8 kW, so multiple pumps are rare:
none of 300 randomly placed wells needed more than one. With the brief's
hydraulics the delivery rule never binds before the power rule (Task 3
evaluation, sections 3-4).

**Where.** `dss/energy.py` (`select_pumps`).

## A9. Cold-room capacity

**Decision.** No cap. The brief's tip defines the uplift on the total harvested
fresh weight of compatible crops, and "5 MT" is a storage capacity, not an
annual throughput: produce is held for a limited period and the room is
reloaded through the season.

**Effect.** None on the headline. The Task 4 evaluation (section 5) reports the
number of full loads a year the harvest represents, so the reader can judge
when capacity would bind.

## A10. Operation and maintenance costs

**Issue.** The brief asks for the highest "net" uplift but gives no operating
costs, and defines payback as CAPEX / annual uplift.

**Decision.** Headline O&M is zero. Sensitivity: 4.5 % of CAPEX per year, the
midpoint of solar cold rooms in Nigeria (O&M USD 1,704-1,904 per year on about
USD 40,000 of construction cost; Takeshima et al., 2021, Table 13).

**Effect.** At the demonstration site the demand-based investment (a dryer
earning USD 95 a year) becomes unprofitable (O&M USD 112.5 a year).

**Where.** `dss/config.py` (`EconomicConfig`); Task 4 evaluation, sections 1
and 4.

## A11. Uplift conventions

**Decision.** One harvest per year; the milling fee applies to the whole cereal
harvest; the dryer premium applies to 15 % of the harvest of crops 10, 20 and
50; the cold room avoids 20 % spoilage of crops 20 and 50 at the farmgate
price; pastures and fallback crops are compatible with no asset. Harvest is
area x yield x 1,000 kg/t. Ties in uplift are broken by lower CAPEX.

**Where.** `dss/reference_tables.py` (`PUE_CATALOGUE`), `dss/economics.py`.

## A12. Horizontal irradiance as peak sun hours

**Decision.** `ALLSKY_SFC_SW_DWN` is global horizontal irradiance and is used
directly as PSH, as the brief states. No tilt gain is applied, which
under-estimates the output of a tilted array and is therefore conservative.

## A13. Demand-based pump consumption (sensitivity)

**Issue.** The brief's `E_pump = P_motor x t_pump` assumes the pump runs at its
rated power for the full pumping window even when the demand needs far less;
at the demonstration site the motor is rated 3.7 kW for a required 1.28 kW.

**Decision.** Headline: the brief's formula. Sensitivity:
`E_pump = P_req x t_pump`, the energy needed to lift the actual demand
(`rho g Q H / eta`, independent of the pumping hours). Solar irrigation
sizing methods derive the PV energy from the crop water demand (e.g. the
worst-month and critical-period methods compared by Đurin et al., 2018), and
the surplus that arises when irrigation demand is below pump capacity is the
quantity of interest for productive use.

**Effect.** At the demonstration site the annual surplus rises from 0 to
10.7 kWh/day and a dryer becomes feasible (payback 26.3 years).

**Where.** `dss/energy.py` (`EnergyBasis.DEMAND`); `python dss_pipeline.py
--demand-based`; Task 3 evaluation, section 1.

---

## F1. Pump catalogue delivery

The catalogue's "max delivery at 40 m head" is 2.2-2.6 times what the brief's
own hydraulic formula gives for the rated motor power. Delivering it would need
13-15 kWh/m²/day of irradiation with the brief's efficiency and derate, more
than reaches the Earth's surface; the figures match a head of about 16-18 m.
Selection therefore uses motor power, as Table 2 states, and the delivery
column is used only to flag clusters whose demand exceeds it (Task 2
evaluation, section 6).

## F2. Water-demand coefficients

Compared with FAO-56 peak crop evapotranspiration at Évora (Penman-Monteith
ET0 of 6.1 mm/day in July; Rodrigues & Braga, 2021), the brief's coefficients
are 26-48 % of peak demand for cereals, pastures and fallback crops, and
78-105 % for vegetables and orchards. They read best as season-average or
deficit-irrigation figures. Headline results keep them; the comparison bounds
the uncertainty a Monte Carlo study should sample (Task 2 evaluation,
section 4).

---

## References

- Allen, R. G., Pereira, L. S., Raes, D. & Smith, M. (1998). *Crop
  evapotranspiration.* FAO Irrigation and Drainage Paper 56.
- Đurin, B., Lajqi, S. & Plantak, L. (2018). 'Worst month' and 'critical
  period' methods for the sizing of solar irrigation systems: a comparison.
  *Revista Facultad de Ingeniería Universidad de Antioquia* 88, 100-109.
  doi:10.17533/udea.redin.n88a11
- Goodchild, M. F. & Lam, N. S.-N. (1980). Areal interpolation: a variant of
  the traditional spatial problem. *Geo-Processing* 1, 297-312.
- NASA POWER. Communities and parameters documentation.
  <https://power.larc.nasa.gov/docs/methodology/>
- Rodrigues, G. C. & Braga, R. P. (2021). Evaluation of NASA POWER reanalysis
  products to estimate daily weather variables in a hot summer Mediterranean
  climate. *Agriculture* 11(2), 124.
- Schneider, M., Broszeit, A. & Körner, M. (2021). EuroCrops: a pan-European
  dataset for time series crop type classification. arXiv:2106.08151.
- Schneider, M. et al. (2023). EuroCrops: the largest harmonized open crop
  dataset across the European Union. *Scientific Data* 10.
  doi:10.1038/s41597-023-02517-0
- Takeshima, H., Yamauchi, F., Bawa, D., Kamaldeen, S. O., Edeh, H. O. &
  Hernandez, M. (2021). *Solar-powered cold-storages and sustainable food
  system transformation: evidence from horticulture markets interventions in
  northeast Nigeria.* IFPRI Discussion Paper 02047.
- EuroCrops v10, Portugal. Zenodo record 10118572.
  <https://zenodo.org/records/10118572>
