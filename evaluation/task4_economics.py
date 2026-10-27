# Copyright (c) 2026 Hao Ni. All rights reserved.
"""Evaluation of Task 4: PUE selection and simple payback.

Run with ``python -m evaluation.task4_economics`` after the Task 1 evaluation (it
reuses the demonstration site). The report is written to
``evaluation/results/task4/REPORT.md``. Internet access is needed for NASA POWER;
without it the demonstration uses the stored response and the batch run is
skipped.
"""

from __future__ import annotations

import argparse
import logging
import math
from collections.abc import Mapping, Sequence
from typing import Any, Final

import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
from matplotlib.ticker import NullFormatter

from dss.config import (
    DEFAULT_PARCELS_PATH,
    M2_PER_HA,
    EconomicConfig,
    EnergyConfig,
    SpatialConfig,
)
from dss.economics import (
    InvestmentStatus,
    PueDecision,
    annual_harvest_kg,
    evaluate_pue,
    feasible_assets_by_month,
)
from dss.energy import EnergyBasis, energy_balance, monthly_energy_balance, select_pumps
from dss.exceptions import EmptyClusterError, SolarDataError
from dss.hydraulics import size_hydraulics
from dss.reference_tables import CROP_ARCHETYPES, PUE_CATALOGUE, CropCategory
from dss.solar import MONTHS, NasaPowerClient, SolarClimatology
from dss.spatial import ParcelCluster, ParcelLayer
from evaluation.common import SEED, Report, demo_site, markdown_table
from evaluation.task1_spatial import parcel_sample_points
from evaluation.task2_solar_hydraulics import FIXTURE
from evaluation.task3_energy import demo_solar, solar_by_grid_cell

logger = logging.getLogger(__name__)

RATED, DEMAND = EnergyBasis.RATED, EnergyBasis.DEMAND
C = CropCategory

# Sensitivity value for PUE O&M (not in the brief): solar cold rooms in Nigeria,
# O&M USD 1,704-1,904/year for about USD 40,000 of construction cost.
# Takeshima et al. (2021), IFPRI Discussion Paper 02047, Table 13.
OM_FRACTION: Final[float] = 0.045
WITH_OM: Final[EconomicConfig] = EconomicConfig(annual_om_fraction=OM_FRACTION)
COLD_ROOM_CAPACITY_T: Final[float] = 5.0  # brief, Table 3 ("5 MT")

# Scenarios: (label, energy basis, financial conventions).
SCENARIOS: Final[tuple[tuple[str, EnergyBasis, EconomicConfig], ...]] = (
    ("Headline: rated consumption (brief)", RATED, EconomicConfig()),
    ("Sensitivity: demand-based consumption", DEMAND, EconomicConfig()),
    (f"Sensitivity: demand-based, O&M {OM_FRACTION:.1%}", DEMAND, WITH_OM),
)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
def main(argv: Sequence[str] | None = None) -> None:
    """Run the Task 4 evaluation and write the report."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--lat", type=float, help="well latitude (default: demo site)")
    parser.add_argument("--lon", type=float, help="well longitude (default: demo site)")
    parser.add_argument("--sample-size", type=int, default=300,
                        help="number of random wells for the batch run")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    logging.getLogger("dss.energy").setLevel(logging.ERROR)

    lat, lon = (args.lat, args.lon) if args.lat is not None else demo_site()
    report = Report(
        "task4",
        "Task 4 evaluation: PUE selection and simple payback",
        "This report verifies the PUE appraisal (`dss.economics`): an independent "
        "recomputation for the demonstration cluster, the decision logic across "
        "crop mixes and surplus levels, the structured no-investment results, a "
        "batch run over randomly placed wells, the sensitivity to operation and "
        "maintenance costs, and the cold-room capacity. Headline results use the "
        "brief's parameters; other scenarios are labelled as sensitivity runs.",
    )
    report.register_data_file(FIXTURE)

    layer = ParcelLayer.from_file(DEFAULT_PARCELS_PATH)
    cluster = layer.extract_cluster(lat, lon)
    client = NasaPowerClient()
    solar = demo_solar(client, cluster)

    evaluate_demo(report, cluster, solar)
    evaluate_decision_logic(report)
    evaluate_edge_cases(report)
    batch = evaluate_batch(report, layer, client, args.sample_size)
    evaluate_cold_room(report, batch)

    print(f"Report written to {report.write()}")


# ---------------------------------------------------------------------------
# 1. Demonstration cluster
# ---------------------------------------------------------------------------
def evaluate_demo(
    report: Report, cluster: ParcelCluster, solar: SolarClimatology
) -> None:
    """Recompute uplifts by hand; decisions under each scenario; seasonal view."""
    areas = _areas(cluster)
    demand = size_hydraulics(areas)
    pumps = select_pumps(demand.required_power_kw, demand.daily_demand_m3)
    surplus = {b: energy_balance(pumps, solar.annual_psh, b).surplus_kwh_day
               for b in EnergyBasis}

    # Hand calculation, written out independently of dss.economics.
    harvest_kg = {k: a * CROP_ARCHETYPES[k].fresh_yield_t_per_ha * 1000
                  for k, a in areas.items()}
    hand = {
        "Cold Room (5 MT)": 0.20 * sum(
            harvest_kg.get(k, 0.0) * CROP_ARCHETYPES[k].farmgate_price_usd_per_kg
            for k in (C.ORCHARDS, C.VEGETABLES)),
        "Grain Milling Unit": 0.05 * harvest_kg.get(C.CEREALS, 0.0),
        "Solar Hybrid Dryer": 0.12 * 0.15 * sum(
            harvest_kg.get(k, 0.0) for k in (C.CEREALS, C.ORCHARDS, C.VEGETABLES)),
    }
    decisions = {label: evaluate_pue(surplus[basis], areas, config=cfg)
                 for label, basis, cfg in SCENARIOS}
    engine = {a.asset.name: a.annual_uplift_usd
              for a in decisions[SCENARIOS[0][0]].appraisals}
    report.check(
        "Engine reproduces the hand-calculated uplifts",
        all(math.isclose(engine[name], value, abs_tol=1e-9)
            for name, value in hand.items()),
        "; ".join(f"{name}: {value:,.2f} USD/year" for name, value in hand.items()),
    )
    headline = decisions[SCENARIOS[0][0]]
    report.check(
        "Headline returns a structured decision without division by zero",
        isinstance(headline.to_dict(), dict)
        and (headline.invest or headline.payback_years is None),
        f"status '{headline.status.value}', payback {headline.payback_years}",
    )

    appraisal = pd.DataFrame(
        [{"Asset": a.asset.name, "Daily load (kWh/day)": a.asset.daily_load_kwh,
          "CAPEX (USD)": a.asset.capex_usd, "Crop compatible": a.crop_compatible,
          "Annual uplift (USD/year)": a.annual_uplift_usd,
          "Payback if feasible (years)": _fmt(a.payback_years)}
         for a in headline.appraisals]
    )
    scenarios = pd.DataFrame(
        [{"Scenario": label, **_decision_row(decisions[label])}
         for label, _, _ in SCENARIOS]
    )
    monthly = {b: monthly_energy_balance(pumps, solar, b) for b in EnergyBasis}
    by_month = {
        b: feasible_assets_by_month(
            {m: monthly[b][m].surplus_kwh_day for m in MONTHS}, areas)
        for b in EnergyBasis
    }
    seasonal = pd.DataFrame(
        {"Month": list(MONTHS),
         "Surplus, rated": [monthly[RATED][m].surplus_kwh_day for m in MONTHS],
         "Feasible, rated": [", ".join(by_month[RATED][m]) or "-" for m in MONTHS],
         "Surplus, demand": [monthly[DEMAND][m].surplus_kwh_day for m in MONTHS],
         "Feasible, demand": [", ".join(by_month[DEMAND][m]) or "-"
                              for m in MONTHS]}
    )
    demand_case, om_case = (decisions[label] for label, _, _ in SCENARIOS[1:])
    if demand_case.selected is None:
        sensitivity_text = f"With demand-based consumption: {demand_case.reason}"
    else:
        om_usd = demand_case.selected.asset.capex_usd * OM_FRACTION
        sensitivity_text = (
            f"With demand-based consumption: {demand_case.reason} Adding O&M of "
            f"{OM_FRACTION:.1%} of CAPEX ({om_usd:,.1f} USD/year): {om_case.reason}"
        )
    harvest_text = ", ".join(
        f"{k.value}: {areas[k]:.3f} ha -> {kg:,.0f} kg" for k, kg in harvest_kg.items()
    )

    report.add_section(
        "1. Demonstration cluster",
        f"""
Harvest (area x yield x 1000 kg/t, one season per year): {harvest_text}.
Surplus: {surplus[RATED]:.2f} kWh/day (rated, headline) and
{surplus[DEMAND]:.2f} kWh/day (demand-based).

{markdown_table(appraisal)}

{markdown_table(scenarios)}

**Headline.** {headline.reason}

**Sensitivity.** {sensitivity_text}

**Seasonal screening** (feasibility only; monthly results are not annualised
into revenue):

{markdown_table(seasonal)}
""",
    )


# ---------------------------------------------------------------------------
# 2. Decision logic across crop mixes and surplus levels
# ---------------------------------------------------------------------------
def evaluate_decision_logic(report: Report) -> None:
    """Selected asset and payback for 1 ha clusters over a range of surpluses."""
    mixes: dict[str, dict[CropCategory, float]] = {
        f"{CROP_ARCHETYPES[k].name} (1 ha)": {k: 1.0} for k in CROP_ARCHETYPES
    }
    mixes["Cereals + vegetables (0.5 ha each)"] = {C.CEREALS: 0.5, C.VEGETABLES: 0.5}
    loads = sorted({a.daily_load_kwh for a in PUE_CATALOGUE})
    levels = [0.0, *loads, 2 * max(loads)]
    rows = []
    for name, areas in mixes.items():
        row: dict[str, Any] = {"Cluster": name}
        for level in levels:
            decision = evaluate_pue(level, areas)
            row[f"{level:g} kWh/day"] = (
                f"{_short(decision.selected.asset.name)} "
                f"({decision.payback_years:.1f} y)"
                if decision.selected
                else decision.status.value.replace("_", " ")
            )
        rows.append(row)
    max_area = math.pi * SpatialConfig().buffer_radius_m**2 / M2_PER_HA
    report.add_section(
        "2. Decision logic across crop mixes and surplus levels",
        f"""
Selected asset and simple payback (years) for illustrative clusters, at surplus
levels equal to the assets' daily loads. The selection changes only when the
surplus crosses a daily load. Uplift is proportional to the harvested area, so
payback scales with 1/area: a cluster filling the whole buffer ({max_area:.2f} ha)
pays back {max_area:.2f} times faster than the 1 ha values shown.

{markdown_table(pd.DataFrame(rows))}
""",
    )


# ---------------------------------------------------------------------------
# 3. Structured no-investment results
# ---------------------------------------------------------------------------
def evaluate_edge_cases(report: Report) -> None:
    """Every no-investment path returns a status and no payback."""
    cases: list[tuple[str, float, Mapping[Any, float], EconomicConfig, str]] = [
        ("Zero surplus", 0.0, {C.VEGETABLES: 2.0}, EconomicConfig(),
         "insufficient_surplus"),
        ("Surplus just below the lowest load", 9.99, {C.CEREALS: 1.0},
         EconomicConfig(), "insufficient_surplus"),
        ("Surplus equal to the lowest load", 10.0, {C.CEREALS: 1.0},
         EconomicConfig(), "invest"),
        ("Pastures only, large surplus", 100.0, {C.PASTURES: 7.0},
         EconomicConfig(), "no_compatible_crops"),
        ("Fallback crops only", 100.0, {C.FALLBACK: 7.0}, EconomicConfig(),
         "no_compatible_crops"),
        ("No parcels", 100.0, {}, EconomicConfig(), "no_compatible_crops"),
        ("Compatible crop with zero area", 100.0, {C.VEGETABLES: 0.0},
         EconomicConfig(), "no_compatible_crops"),
        (f"Tiny cereal plot, O&M {OM_FRACTION:.1%}", 30.0, {C.CEREALS: 0.01},
         WITH_OM, "not_profitable"),
    ]  # fmt: skip
    rows = []
    for name, surplus, areas, cfg, expected in cases:
        try:
            result = evaluate_pue(surplus, areas, config=cfg).to_dict()
            observed, payback = result["status"], result["payback_years"]
        except Exception as exc:  # report any failure as a row
            observed, payback = f"{type(exc).__name__}: {exc}", None
        consistent = (observed == "invest") == (payback is not None)
        rows.append({"Scenario": name, "Expected": expected, "Observed": observed,
                     "Payback (years)": _fmt(payback),
                     "Result": "PASS" if observed == expected and consistent
                     else "FAIL"})
    frame = pd.DataFrame(rows)
    passed = int((frame["Result"] == "PASS").sum())
    report.check(
        "Every no-investment path returns a status and no payback",
        passed == len(frame),
        f"{passed} / {len(frame)} scenarios",
    )
    example = evaluate_pue(0.0, {C.VEGETABLES: 2.0}).to_dict()
    report.add_section(
        "3. Structured no-investment results",
        f"""
{markdown_table(frame)}

Example of the structured result returned when there is no investment:

```python
{example}
```
""",
    )


# ---------------------------------------------------------------------------
# 4. Batch run over random wells
# ---------------------------------------------------------------------------
def evaluate_batch(
    report: Report, layer: ParcelLayer, client: NasaPowerClient, sample_size: int
) -> pd.DataFrame | None:
    """Full Task 1-4 chain for random wells; invariants and distributions."""
    rng = np.random.default_rng(SEED)
    clusters = []
    for lat, lon in parcel_sample_points(layer, sample_size, rng):
        try:
            clusters.append(layer.extract_cluster(lat, lon))
        except EmptyClusterError:
            continue
    try:
        solar_by_cluster, _ = solar_by_grid_cell(client, clusters)
    except SolarDataError as exc:
        report.add_section("4. Batch run over random wells", f"Skipped: {exc}")
        return None

    rows, violations = [], 0
    for cluster, solar in zip(clusters, solar_by_cluster, strict=True):
        row, ok = _batch_row(cluster, solar)
        rows.append(row)
        violations += not ok
    frame = pd.DataFrame(rows)
    csv_link = report.save_csv(frame, "batch_decisions")
    report.check(
        "Invariants hold for every cluster and scenario",
        violations == 0,
        f"{len(frame)} clusters x {len(SCENARIOS)} scenarios; {violations} violations",
    )

    statuses = pd.DataFrame(
        {label: frame[f"status_{i}"].value_counts()
         for i, (label, _, _) in enumerate(SCENARIOS)}
    ).fillna(0).astype(int).rename_axis("Status").reset_index()
    assets = pd.DataFrame(
        {label: frame[f"asset_{i}"].fillna("no investment").value_counts()
         for i, (label, _, _) in enumerate(SCENARIOS)}
    ).fillna(0).astype(int).rename_axis("Selected asset").reset_index()
    paybacks = pd.DataFrame(
        [{"Scenario": label, "Investing clusters": int(frame[f"payback_{i}"].notna()
                                                       .sum()),
          **{f"Payback P{q}": _fmt(frame[f"payback_{i}"].quantile(q / 100))
             for q in (10, 50, 90)}}
         for i, (label, _, _) in enumerate(SCENARIOS)]
    )
    seasonal = pd.DataFrame(
        {"Months with a feasible asset": range(13),
         "Clusters, rated": frame["months_rated"].value_counts()
         .reindex(range(13), fill_value=0).to_numpy(),
         "Clusters, demand": frame["months_demand"].value_counts()
         .reindex(range(13), fill_value=0).to_numpy()}
    )
    seasonal = seasonal[(seasonal.iloc[:, 1:] > 0).any(axis=1)]
    figure = _batch_figure(report, frame)
    max_psh = float(frame["annual_psh"].max())
    derate = EnergyConfig().pv_derate
    reach = (
        frame.groupby(["pumps", "pv_kwp"]).size().rename("Clusters").reset_index()
        .sort_values("pv_kwp")
    )
    reach["Max E_PV (kWh/day)"] = reach["pv_kwp"] * max_psh * derate
    reach["Assets within reach"] = [
        ", ".join(_short(a.name) for a in PUE_CATALOGUE if a.daily_load_kwh <= ceiling)
        or "none"
        for ceiling in reach["Max E_PV (kWh/day)"]
    ]
    reach = reach.rename(columns={"pumps": "Pumps", "pv_kwp": "PV (kWp)"})
    dryer_only = reach.loc[reach["Assets within reach"] == "Dryer", "Clusters"].sum()
    reach_note = (
        f"For {dryer_only} of {len(frame)} clusters the dryer is the only asset the "
        "PV array could ever power on an annual basis, whatever the consumption "
        "assumption; the cold room and the mill need a larger array."
        if dryer_only
        else "Every cluster's PV array could in principle power more than the dryer."
    )
    invest = [int(frame[f"payback_{i}"].notna().sum()) for i in range(len(SCENARIOS))]
    summary = (
        f"Clusters with an investment: {invest[0]} of {len(frame)} under the "
        f"headline, {invest[1]} with demand-based consumption and {invest[2]} when "
        f"O&M of {OM_FRACTION:.1%} is added."
    )
    report.add_section(
        "4. Batch run over random wells",
        f"""
The full chain (cluster, demand, pump, energy balance, PUE) was run for
{len(frame)} wells at random agricultural parcels (seed {SEED}); NASA POWER
requests are grouped by 1-degree grid cell as in the Task 3 evaluation.
Per-cluster results: {csv_link}.

{summary}

Invariants tested: the selected asset is a candidate with the highest net uplift;
payback equals CAPEX / net uplift; no investment implies no payback; a larger
(demand-based) surplus never yields a lower net uplift than the rated surplus.

{markdown_table(statuses)}

{markdown_table(assets)}

{markdown_table(paybacks)}

Seasonal screening (number of months in which at least one asset meets both
requirements):

{markdown_table(seasonal)}

{figure}

**Which assets are reachable.** Even with no pump consumption, the surplus
cannot exceed the PV generation `PV_kWp x PSH x derate`. At the sunniest sampled
grid cell (annual PSH {max_psh:.2f}) this ceiling is:

{markdown_table(reach)}

{reach_note}
""",
    )
    return frame


# ---------------------------------------------------------------------------
# 5. Cold-room capacity
# ---------------------------------------------------------------------------
def evaluate_cold_room(report: Report, batch: pd.DataFrame | None) -> None:
    """Compare the cold-room-compatible harvest with the 5 t nameplate capacity."""
    max_area = math.pi * SpatialConfig().buffer_radius_m**2 / M2_PER_HA
    rows = []
    for category in (C.VEGETABLES, C.ORCHARDS):
        tonnes = annual_harvest_kg({category: max_area})[category] / 1000
        rows.append({"Case": f"Whole buffer of {CROP_ARCHETYPES[category].name}",
                     "Compatible harvest (t/year)": tonnes})
    if batch is not None and len(batch):
        rows.append({"Case": "Largest cluster in the batch run",
                     "Compatible harvest (t/year)": batch["cold_room_t"].max()})
        rows.append({"Case": "Median cluster in the batch run",
                     "Compatible harvest (t/year)": batch["cold_room_t"].median()})
    frame = pd.DataFrame(rows)
    frame["Full 5 t loads per year"] = (
        frame["Compatible harvest (t/year)"] / COLD_ROOM_CAPACITY_T
    )
    frame["Days per load if spread over a year"] = [
        _fmt(365 / n) if n >= 1 else "less than one load per year"
        for n in frame["Full 5 t loads per year"]
    ]
    report.add_section(
        "5. Cold-room capacity",
        f"""
Following the brief's tip, the cold-room uplift is applied to the whole
compatible harvest; its {COLD_ROOM_CAPACITY_T:g} t capacity is not imposed as a
cap. The table shows how many full loads that harvest represents. Produce is
held for a limited period rather than a whole season, so the capacity binds
only if the harvest arrives faster than one load per storage period.

{markdown_table(frame)}
""",
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _areas(cluster: ParcelCluster) -> dict[CropCategory, float]:
    return {k: float(v) for k, v in cluster.area_by_category().items()}


def _batch_row(
    cluster: ParcelCluster, solar: SolarClimatology
) -> tuple[dict[str, Any], bool]:
    """Decisions for one cluster under each scenario, and the invariant check."""
    areas = _areas(cluster)
    demand = size_hydraulics(areas)
    pumps = select_pumps(demand.required_power_kw, demand.daily_demand_m3)
    surplus = {b: energy_balance(pumps, solar.annual_psh, b).surplus_kwh_day
               for b in EnergyBasis}
    decisions = [evaluate_pue(surplus[basis], areas, config=cfg)
                 for _, basis, cfg in SCENARIOS]
    months = {
        b: sum(bool(v) for v in feasible_assets_by_month(
            {m: bal.surplus_kwh_day
             for m, bal in monthly_energy_balance(pumps, solar, b).items()},
            areas).values())
        for b in EnergyBasis
    }
    ok = all(_consistent(d) for d in decisions) and (
        _net(decisions[1]) >= _net(decisions[0])
    )
    harvest = annual_harvest_kg(areas)
    row: dict[str, Any] = {
        "lat": round(cluster.centroid_lat, 5), "lon": round(cluster.centroid_lon, 5),
        "area_ha": sum(areas.values()), "pumps": pumps.label,
        "pv_kwp": pumps.pv_kwp, "annual_psh": solar.annual_psh,
        "surplus_rated": surplus[RATED], "surplus_demand": surplus[DEMAND],
        "months_rated": months[RATED], "months_demand": months[DEMAND],
        "cold_room_t": sum(harvest.get(k, 0.0)
                           for k in PUE_CATALOGUE[0].compatible) / 1000,
    }
    for i, decision in enumerate(decisions):
        row[f"status_{i}"] = decision.status.value
        row[f"asset_{i}"] = decision.selected.asset.name if decision.selected else None
        row[f"payback_{i}"] = decision.payback_years
    return row, ok


def _consistent(decision: PueDecision) -> bool:
    """The decision satisfies the selection and payback rules."""
    profitable = [a for a in decision.appraisals
                  if a.is_candidate and a.annual_net_uplift_usd > 0]
    if decision.selected is None:
        return decision.status is not InvestmentStatus.INVEST and not profitable
    best = max(a.annual_net_uplift_usd for a in profitable)
    selected = decision.selected
    return (
        decision.status is InvestmentStatus.INVEST
        and selected.is_candidate
        and math.isclose(selected.annual_net_uplift_usd, best)
        and decision.payback_years is not None
        and math.isclose(decision.payback_years,
                         selected.asset.capex_usd / selected.annual_net_uplift_usd)
    )


def _net(decision: PueDecision) -> float:
    return decision.selected.annual_net_uplift_usd if decision.selected else 0.0


def _decision_row(decision: PueDecision) -> dict[str, Any]:
    result = decision.to_dict()
    return {
        "Surplus (kWh/day)": result["surplus_kwh_day"],
        "Status": result["status"],
        "Asset": result["asset"] or "-",
        "Net uplift (USD/year)": result["annual_net_uplift_usd"],
        "Payback (years)": _fmt(result["payback_years"]),
    }


def _batch_figure(report: Report, frame: pd.DataFrame) -> str:
    fig, (left, right) = plt.subplots(1, 2, figsize=(10, 3.5))
    labels = ["Headline", "Demand", f"Demand + O&M {OM_FRACTION:.1%}"]
    invest = [int(frame[f"payback_{i}"].notna().sum()) for i in range(len(labels))]
    left.bar(labels, invest, color="#4c72b0")
    left.set(ylabel="Clusters with an investment", ylim=(0, len(frame)),
             title=f"Investing clusters (of {len(frame)})")
    left.tick_params(axis="x", labelsize=8)
    paybacks = frame["payback_1"].dropna()
    if len(paybacks):
        bins = np.logspace(np.log10(paybacks.min()), np.log10(paybacks.max()), 25)
        right.hist(paybacks, bins=bins, color="#55a868")
        right.set_xscale("log")
        ticks = [t for t in (1, 2, 5, 10, 20, 50, 100, 200, 500, 1000, 2000)
                 if paybacks.min() / 2 <= t <= paybacks.max() * 2]
        right.set_xticks(ticks, [f"{t:g}" for t in ticks])
        right.xaxis.set_minor_formatter(NullFormatter())
    right.set(xlabel="Simple payback (years, log scale)", ylabel="Clusters",
              title="Payback, demand-based surplus")
    return report.save_figure(fig, "batch", "Batch run over random wells.")


def _short(name: str) -> str:
    return name.split(" (")[0].replace("Grain Milling Unit", "Mill").replace(
        "Solar Hybrid Dryer", "Dryer")


def _fmt(value: float | None) -> str:
    if value is None or (isinstance(value, float) and math.isnan(value)):
        return "-"
    return f"{value:,.1f}"


if __name__ == "__main__":
    main()
