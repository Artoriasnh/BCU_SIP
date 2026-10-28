# Copyright (c) 2026 Hao Ni. All rights reserved.
"""Evaluation of Task 3: pump selection and PV energy balance.

Run with ``python -m evaluation.task3_energy`` after the Task 1 evaluation (it
reuses the demonstration site). The report is written to
``evaluation/results/task3/REPORT.md``. Internet access is needed for NASA POWER;
without it the demonstration uses the stored response and network sections are
reported as skipped.
"""

from __future__ import annotations

import argparse
import dataclasses
import logging
import math
from collections.abc import Mapping, Sequence
from typing import Any, Final

import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

from dss.config import (
    DEFAULT_PARCELS_PATH,
    J_PER_KWH,
    M2_PER_HA,
    EnergyConfig,
    HydraulicConfig,
    SpatialConfig,
)
from dss.energy import (
    EnergyBalance,
    EnergyBasis,
    PumpSelection,
    PumpStrategy,
    energy_balance,
    monthly_energy_balance,
    select_pumps,
    surplus_threshold_psh,
)
from dss.exceptions import SolarDataError
from dss.hydraulics import size_hydraulics
from dss.reference_tables import CROP_ARCHETYPES, PUMP_CATALOGUE, CropCategory
from dss.solar import MONTHS, NasaPowerClient, SolarClimatology
from dss.spatial import ParcelCluster, ParcelLayer
from evaluation.common import (
    FIXTURE,
    SEED,
    TRANSECT,
    Report,
    SampledWell,
    demo_site,
    demo_solar,
    markdown_table,
    sample_wells,
    solar_by_grid_cell,
)

logger = logging.getLogger(__name__)

RATED, DEMAND = EnergyBasis.RATED, EnergyBasis.DEMAND
IDENTICAL, LEAST_COST = PumpStrategy.IDENTICAL, PumpStrategy.LEAST_COST
SURPLUS_COLOUR: Final[str] = "#55a868"


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
def main(argv: Sequence[str] | None = None) -> None:
    """Run the Task 3 evaluation and write the report."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--lat", type=float, help="well latitude (default: demo site)")
    parser.add_argument("--lon", type=float, help="well longitude (default: demo site)")
    parser.add_argument("--sample-size", type=int, default=300,
                        help="number of random wells for the batch run")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    logging.getLogger("dss.energy").setLevel(logging.ERROR)  # summarised instead

    lat, lon = (args.lat, args.lon) if args.lat is not None else demo_site()
    report = Report(
        "task3",
        "Task 3 evaluation: pump selection and energy balance",
        "This report verifies pump selection and the daily energy balance "
        "(`dss.energy`): an independent recomputation for the demonstration "
        "cluster, the analytical surplus threshold, a batch run over randomly "
        "placed wells, the multiple-pump strategies, and parameter sensitivity. "
        "Headline results use the brief's formula (rated motor power); the "
        "demand-based consumption and the monthly analysis are reported "
        "alongside as sensitivity and seasonal analyses.",
    )
    report.register_data_file(FIXTURE)

    layer = ParcelLayer.from_file(DEFAULT_PARCELS_PATH)
    cluster = layer.extract_cluster(lat, lon)
    client = NasaPowerClient()
    solar = demo_solar(client, cluster)

    evaluate_demo(report, cluster, solar)
    evaluate_threshold(report, client)
    evaluate_batch(report, layer, client, args.sample_size)
    evaluate_strategies(report)
    evaluate_sensitivity(report, cluster, solar)

    print(f"Report written to {report.write()}")


# ---------------------------------------------------------------------------
# 1. Demonstration cluster
# ---------------------------------------------------------------------------
def evaluate_demo(
    report: Report, cluster: ParcelCluster, solar: SolarClimatology
) -> None:
    """Recompute selection and energy balance by hand; show the monthly balance."""
    hyd, eng = HydraulicConfig(), EnergyConfig()
    demand = size_hydraulics(_areas(cluster))
    power, q_day = demand.required_power_kw, demand.daily_demand_m3
    selection = select_pumps(power, q_day)

    # Hand calculation, written out independently of dss.energy.
    largest = max(PUMP_CATALOGUE, key=lambda t: t.motor_kw)
    n = max(1, math.ceil(power / largest.motor_kw),
            math.ceil(q_day / largest.max_delivery_m3_day))
    tier = min((t for t in PUMP_CATALOGUE if t.motor_kw >= power / n),
               key=lambda t: t.motor_kw)
    e_pv = n * tier.pv_kwp * solar.annual_psh * eng.pv_derate
    e_pump = {RATED: n * tier.motor_kw * hyd.pumping_hours_per_day,
              DEMAND: power * hyd.pumping_hours_per_day}
    hand = {basis: max(0.0, e_pv - e_pump[basis]) for basis in EnergyBasis}
    engine = {basis: energy_balance(selection, solar.annual_psh, basis)
              for basis in EnergyBasis}

    report.check(
        "Engine reproduces the hand calculation (annual PSH)",
        selection.label == f"{n} x {tier.name}"
        and all(math.isclose(engine[b].surplus_kwh_day, hand[b], abs_tol=1e-9)
                and math.isclose(engine[b].pv_generation_kwh_day, e_pv)
                for b in EnergyBasis),
        f"{selection.label}; E_PV {e_pv:.2f}, surplus rated "
        f"{hand[RATED]:.2f} / demand {hand[DEMAND]:.2f} kWh/day",
    )
    report.check(
        "Selected motor power covers the required power",
        selection.motor_kw >= power,
        f"{selection.motor_kw:.1f} kW installed for {power:.2f} kW required",
    )

    summary = pd.DataFrame(
        {
            "Quantity": ["Daily demand Q_day", "Required power P_req",
                         "Selected pumps", "Motor power", "PV capacity", "CAPEX",
                         "Annual PSH", "E_PV = PV x PSH x derate",
                         "E_pump, rated (brief)", "E_excess, rated (brief)",
                         "E_pump, demand (sensitivity)",
                         "E_excess, demand (sensitivity)"],
            "Value": [f"{q_day:.2f}", f"{power:.3f}", selection.label,
                      f"{selection.motor_kw:g}", f"{selection.pv_kwp:g}",
                      f"{selection.capex_usd:,.0f}", f"{solar.annual_psh:.3f}",
                      f"{e_pv:.3f}", f"{e_pump[RATED]:.3f}", f"{hand[RATED]:.3f}",
                      f"{e_pump[DEMAND]:.3f}", f"{hand[DEMAND]:.3f}"],
            "Unit": ["m3/day", "kW", "", "kW", "kWp", "USD", "kWh/m2/day",
                     "kWh/day", "kWh/day", "kWh/day", "kWh/day", "kWh/day"],
        }
    )
    monthly = {b: monthly_energy_balance(selection, solar, b) for b in EnergyBasis}
    table = pd.DataFrame(
        {
            "Month": list(MONTHS),
            "PSH": [solar.monthly_psh[m] for m in MONTHS],
            "E_PV (kWh/day)": [monthly[RATED][m].pv_generation_kwh_day
                               for m in MONTHS],
            "Surplus, rated": [monthly[RATED][m].surplus_kwh_day for m in MONTHS],
            "Surplus, demand": [monthly[DEMAND][m].surplus_kwh_day for m in MONTHS],
        }
    )
    surplus_months = [m for m in MONTHS if monthly[RATED][m].surplus_kwh_day > 0]
    figure = _monthly_figure(report, solar, monthly, e_pump)
    threshold = surplus_threshold_psh(selection)

    report.add_section(
        "1. Demonstration cluster",
        f"""
Cluster centroid ({cluster.centroid_lat:.5f}, {cluster.centroid_lon:.5f});
irradiance source: {solar.source}.

{markdown_table(summary)}

**Headline (brief).** With the annual PSH of {solar.annual_psh:.2f}, PV
generation ({e_pv:.1f} kWh/day) is below the rated consumption
({e_pump[RATED]:.1f} kWh/day), so the surplus is
**{hand[RATED]:.1f} kWh/day**.

**Seasonal analysis.** A surplus exists when the monthly PSH exceeds
{threshold:.2f}: in {len(surplus_months)} months
({", ".join(surplus_months) or "none"}).

**Demand-based consumption (sensitivity).** The pump runs at the rated power
only if it must; lifting the actual demand needs {e_pump[DEMAND]:.1f} kWh/day,
leaving {hand[DEMAND]:.1f} kWh/day on an annual basis.

{markdown_table(table)}

{figure}
""",
    )


# ---------------------------------------------------------------------------
# 2. Analytical surplus threshold
# ---------------------------------------------------------------------------
def evaluate_threshold(report: Report, client: NasaPowerClient) -> None:
    """Threshold PSH per tier and its position relative to Portuguese irradiance."""
    factor = HydraulicConfig().pumping_hours_per_day / EnergyConfig().pv_derate
    rows: list[dict[str, Any]] = []
    separates = True
    for tier in PUMP_CATALOGUE:
        selection = select_pumps(tier.motor_kw, 0.0)
        threshold = surplus_threshold_psh(selection)
        below = energy_balance(selection, threshold * (1 - 1e-6)).surplus_kwh_day
        above = energy_balance(selection, threshold * (1 + 1e-6)).surplus_kwh_day
        separates &= below == 0.0 and above > 0.0
        rows.append({"Tier": tier.name, "Motor (kW)": tier.motor_kw,
                     "PV (kWp)": tier.pv_kwp,
                     "PV / motor (kWp/kW)": tier.pv_kwp / tier.motor_kw,
                     "Threshold PSH, rated": threshold})
    tiers = pd.DataFrame(rows)
    low, high = tiers["Threshold PSH, rated"].min(), tiers["Threshold PSH, rated"].max()
    report.check(
        "Analytical threshold separates deficit from surplus",
        separates,
        f"threshold {low:.2f}-{high:.2f} PSH for all tiers",
    )

    cities: list[dict[str, Any]] = []
    for name, lat, lon in TRANSECT:
        try:
            solar = client.fetch(lat, lon)
        except SolarDataError as exc:
            logger.warning("NASA POWER failed for %s: %s", name, exc)
            continue
        cities.append({"City": name, "Annual PSH": solar.annual_psh,
                       "Max monthly PSH": max(solar.monthly_psh.values()),
                       f"Months above {low:.2f}": sum(
                           v > low for v in solar.monthly_psh.values())})
    transect = (
        markdown_table(pd.DataFrame(cities)) if cities else "Skipped: NASA unavailable."
    )
    max_annual = max((c["Annual PSH"] for c in cities), default=math.nan)
    verdict = (
        f"the highest annual mean is {max_annual:.2f} PSH, below the lowest "
        "threshold. **Under the brief's formula and annual PSH, no location on "
        "the transect has a surplus**; surpluses appear only in the summer months "
        "(seasonal analysis) or with demand-based consumption."
        if max_annual < low
        else f"the highest annual mean is {max_annual:.2f} PSH, so some locations "
        "exceed the threshold."
    )

    report.add_section(
        "2. Analytical surplus threshold",
        f"""
With the rated consumption, `E_PV > E_pump` exactly when

    PSH > (t_pump / derate) x (P_motor / PV_kWp) = {factor:g} x P_motor / PV_kWp

The threshold therefore depends only on the catalogue's PV-to-motor ratio, not on
the crops or the location: it is {low:.2f}-{high:.2f} PSH for every tier.

{markdown_table(tiers)}

Along the north-south transect {verdict}

{transect}
""",
    )


# ---------------------------------------------------------------------------
# 3. Batch run over random wells
# ---------------------------------------------------------------------------
def evaluate_batch(
    report: Report, layer: ParcelLayer, client: NasaPowerClient, sample_size: int
) -> None:
    """Run the full Task 1-3 chain for random wells and test invariants."""
    wells = sample_wells(layer, sample_size, np.random.default_rng(SEED))
    try:
        solar_by_cluster, grouping = solar_by_grid_cell(
            client, [w.cluster for w in wells]
        )
    except SolarDataError as exc:
        report.add_section("3. Batch run over random wells", f"Skipped: {exc}")
        return

    rows, violations = [], 0
    for well, solar in zip(wells, solar_by_cluster, strict=True):
        row, ok = _batch_row(well, solar)
        rows.append(row)
        violations += not ok
    frame = pd.DataFrame(rows)
    csv_link = report.save_csv(frame, "batch_clusters")

    report.check(
        "Invariants hold for every cluster and month",
        violations == 0,
        f"{len(frame)} clusters x 12 months x 2 bases; {violations} violations",
    )

    n = len(frame)
    stats = pd.DataFrame(
        {
            "Statistic": [
                "Clusters", "Median parcels per cluster",
                "Median clipped area (ha)",
                "Median required power (kW)", "Max required power (kW)",
                "Multiple pumps required",
                "Demand above catalogue delivery (warning)",
                "Annual surplus > 0, rated (headline)",
                "Annual surplus > 0, demand (sensitivity)",
                "Surplus months, rated (min-max)",
                "Median annual surplus, demand (kWh/day)",
            ],
            "Value": [
                f"{n:,}", f"{frame['n_parcels'].median():.0f}",
                f"{frame['area_ha'].median():.2f}",
                f"{frame['P_req_kW'].median():.2f}",
                f"{frame['P_req_kW'].max():.2f}",
                _share(frame["n_pumps"] > 1), _share(frame["exceeds_delivery"]),
                _share(frame["surplus_rated_kwh"] > 0),
                _share(frame["surplus_demand_kwh"] > 0),
                f"{frame['surplus_months_rated'].min()}-"
                f"{frame['surplus_months_rated'].max()}",
                f"{frame['surplus_demand_kwh'].median():.1f}",
            ],
        }
    )
    selections = (
        frame.groupby("selection").size().rename("Clusters").reset_index()
        .sort_values("Clusters", ascending=False)
    )
    figure = _batch_figure(report, frame)
    max_area = math.pi * SpatialConfig().buffer_radius_m**2 / M2_PER_HA
    max_coeff, top = max(
        (a.water_demand_m3_per_ha_day, k) for k, a in CROP_ARCHETYPES.items()
    )
    max_power = size_hydraulics({top: max_area}).required_power_kw
    n_multi = int((frame["n_pumps"] > 1).sum())
    pump_note = (
        "No cluster in this sample needs more than one pump: clusters in this "
        f"dataset are small (median {frame['area_ha'].median():.2f} ha)."
        if n_multi == 0
        else f"{n_multi} clusters in this sample need more than one pump."
    )
    months = frame["surplus_months_rated"]
    months_note = (
        f"Every cluster has {months.min()} surplus months under the rated "
        "consumption: the threshold (about 5.9 PSH) is set by the catalogue, and "
        "the monthly irradiance varies little across the sampled grid cells."
        if months.min() == months.max()
        else f"Clusters have {months.min()}-{months.max()} surplus months under "
        "the rated consumption; as the threshold is set by the catalogue, the "
        "differences come from irradiance alone."
    )
    zero = frame[frame["surplus_demand_kwh"] <= 0]
    load = zero["P_req_kW"] / zero["motor_kW"]
    zero_note = (
        f"The demand-based surplus is zero for {len(zero)} clusters, whose "
        f"required power is {load.min():.0%}-{load.max():.0%} of the installed "
        "motor power: when the pump is sized close to the demand, even "
        "demand-based pumping uses all the PV output."
        if len(zero)
        else "The demand-based surplus is positive for every cluster."
    )
    report.add_section(
        "3. Batch run over random wells",
        f"""
Wells were placed at {sample_size} random agricultural parcels (seed {SEED}) and
the full chain was run: cluster, demand and power, NASA POWER at the centroid,
pump selection, monthly energy balance. Because the solar data come from a
1-degree grid, requests were grouped by grid cell; each group was verified by
fetching its extreme members (north, south, east, west) and fell back to
per-centroid requests if they differed ({grouping['cells']} cells,
{grouping['inconsistent']} with differing extremes, {grouping['requests']}
requests in total). Per-cluster results: {csv_link}.

Invariants tested for every cluster, month and consumption basis: installed motor
power >= required power; `0 <= surplus <= E_PV`; least-cost CAPEX <= identical
CAPEX; demand-based surplus >= rated surplus.

{markdown_table(stats)}

{markdown_table(selections)}

{figure}

A 150 m radius encloses at most {max_area:.2f} ha; even fully irrigated at the
highest demand coefficient ({max_coeff:g} m3/ha/day) a cluster needs
{max_power:.1f} kW, so several pumps are possible only for clusters dominated by
that archetype. {pump_note} {months_note} {zero_note} The catalogue's delivery
column is flagged rather than used (Task 2 report, section 6).
""",
    )


# ---------------------------------------------------------------------------
# 4. Multiple-pump strategies
# ---------------------------------------------------------------------------
def evaluate_strategies(report: Report) -> None:
    """Compare CAPEX of identical, least-cost and all-Tier-4 combinations."""
    hyd = HydraulicConfig()
    kwh_per_m3 = (hyd.water_density_kg_m3 * hyd.gravity_m_s2
                  * hyd.total_dynamic_head_m / J_PER_KWH)
    m3_per_kw = hyd.pumping_hours_per_day * hyd.pump_efficiency / kwh_per_m3
    largest = max(PUMP_CATALOGUE, key=lambda t: t.motor_kw)
    p_delivery = largest.max_delivery_m3_day / m3_per_kw  # P at which Q = 850

    powers = np.round(np.arange(0.25, 45.01, 0.25), 2)
    rows = []
    for power in powers:
        q_day = power * m3_per_kw  # demand consistent with the brief's formula
        identical = select_pumps(power, q_day, IDENTICAL)
        cheapest = select_pumps(power, q_day, LEAST_COST)
        rows.append({
            "P_req (kW)": power, "Identical": identical.label,
            "CAPEX identical": identical.capex_usd, "Least cost": cheapest.label,
            "CAPEX least cost": cheapest.capex_usd,
            "CAPEX all Tier 4": identical.n_units * largest.capex_usd,
            "Delivery rule binds": math.ceil(q_day / largest.max_delivery_m3_day)
            > math.ceil(power / largest.motor_kw),
        })
    frame = pd.DataFrame(rows)
    csv_link = report.save_csv(frame, "strategy_sweep")
    report.check(
        "CAPEX ordering: least cost <= identical <= all Tier 4",
        bool((frame["CAPEX least cost"] <= frame["CAPEX identical"]).all()
             and (frame["CAPEX identical"] <= frame["CAPEX all Tier 4"]).all()),
        f"{len(frame)} power levels, 0.25-45 kW",
    )

    saving = 1 - frame["CAPEX least cost"] / frame["CAPEX identical"]
    multi = frame["P_req (kW)"] > largest.motor_kw
    changes = frame[frame["Identical"] != frame["Identical"].shift()]
    examples = frame[frame["P_req (kW)"].isin([12.0, 13.0, 16.0, 20.0, 25.0, 34.0])]

    fig, ax = plt.subplots(figsize=(8, 3.5))
    for column, style in (("CAPEX all Tier 4", ":"), ("CAPEX identical", "-"),
                          ("CAPEX least cost", "--")):
        ax.step(frame["P_req (kW)"], frame[column] / 1000, style, where="post",
                label=column.replace("CAPEX ", ""))
    ax.axvline(largest.motor_kw, color="grey", lw=0.8)
    ax.set(xlabel="Required power (kW)", ylabel="Pump CAPEX (kUSD)",
           title="Pump CAPEX by combination strategy")
    ax.legend()
    figure = report.save_figure(fig, "strategies", "Pump CAPEX by strategy.")

    report.add_section(
        "4. Multiple-pump strategies",
        f"""
Above {largest.motor_kw:g} kW the brief requires several pumps but does not say
how to combine them. Three options were compared over a sweep of required power
(demand consistent with the brief's formula, {m3_per_kw:.2f} m3/day per kW):

* **Identical (headline):** `n` identical units, each the smallest tier covering
  `P / n`. Parallel pumps are normally identical so that they share the load
  at the same operating point.
* **Least cost (sensitivity):** the cheapest mix of tiers.
* **All Tier 4:** `n` units of the largest tier; never cheaper than identical,
  so not used.

For multiple-pump cases least cost saves {saving[multi].mean():.0%} on average
(maximum {saving[multi].max():.0%}) relative to identical units.

**Delivery rule.** With the brief's head, efficiency and pumping hours, the
demand reaches the Tier 4 delivery ({largest.max_delivery_m3_day:g} m3/day) only
at {p_delivery:.1f} kW, while the power rule already requires
{math.ceil(p_delivery / largest.motor_kw)} pumps there. The delivery rule binds
at {int(frame["Delivery rule binds"].sum())} of {len(frame)} power levels, a
consequence of the inflated catalogue delivery figures.

Selection changes along the sweep:

{markdown_table(changes[["P_req (kW)", "Identical", "CAPEX identical"]])}

Examples:

{markdown_table(examples[["P_req (kW)", "Identical", "CAPEX identical",
                          "Least cost", "CAPEX least cost", "CAPEX all Tier 4"]])}

{figure}

Full sweep: {csv_link}.
""",
    )


# ---------------------------------------------------------------------------
# 5. Parameter sensitivity
# ---------------------------------------------------------------------------
def evaluate_sensitivity(
    report: Report, cluster: ParcelCluster, solar: SolarClimatology
) -> None:
    """One-at-a-time +/-20 % perturbations of derate, pumping hours and PSH."""
    base_hyd, base_eng = HydraulicConfig(), EnergyConfig()
    variants: list[tuple[str, HydraulicConfig, EnergyConfig, float]] = [
        ("Brief (base)", base_hyd, base_eng, 1.0),
        ("Derate 0.60 (-20 %)", base_hyd, EnergyConfig(pv_derate=0.60), 1.0),
        ("Derate 0.90 (+20 %)", base_hyd, EnergyConfig(pv_derate=0.90), 1.0),
        ("Pumping 4.8 h (-20 %)",
         dataclasses.replace(base_hyd, pumping_hours_per_day=4.8), base_eng, 1.0),
        ("Pumping 7.2 h (+20 %)",
         dataclasses.replace(base_hyd, pumping_hours_per_day=7.2), base_eng, 1.0),
        ("PSH -20 %", base_hyd, base_eng, 0.8),
        ("PSH +20 %", base_hyd, base_eng, 1.2),
    ]
    rows = []
    for name, hyd, eng, psh_scale in variants:
        demand = size_hydraulics(_areas(cluster), hyd)
        selection = select_pumps(demand.required_power_kw, demand.daily_demand_m3)
        monthly = {m: solar.monthly_psh[m] * psh_scale for m in MONTHS}
        annual = solar.annual_psh * psh_scale

        rows.append({
            "Scenario": name, "Pumps": selection.label,
            "Threshold PSH": surplus_threshold_psh(selection, RATED, hyd, eng),
            "Annual surplus, rated": _surplus(selection, annual, RATED, hyd, eng),
            "Surplus months, rated": sum(
                _surplus(selection, v, RATED, hyd, eng) > 0 for v in monthly.values()
            ),
            "July surplus, rated": _surplus(selection, monthly["JUL"], RATED, hyd, eng),
            "Annual surplus, demand": _surplus(selection, annual, DEMAND, hyd, eng),
        })
    frame = pd.DataFrame(rows)
    flips = frame.loc[
        (frame["Annual surplus, rated"] > 0) & (frame["Scenario"] != "Brief (base)"),
        "Scenario",
    ].tolist()
    verdict = (
        "No single change produces an annual surplus under the rated consumption."
        if not flips
        else "An annual surplus under the rated consumption appears only for: "
        + ", ".join(flips)
        + ". Fewer pumping hours lower the rated consumption (motor power x hours) "
        "faster than they raise the required power."
    )
    report.add_section(
        "5. Parameter sensitivity (demonstration cluster)",
        f"""
Each input is moved by +/-20 % with the others at the brief's values. Changing
the pumping hours also changes the required power and can change the selected
pump. Surpluses are in kWh/day.

{markdown_table(frame)}

{verdict} The number of surplus months and the demand-based surplus respond
roughly in proportion to the PV yield. The demand-based consumption does not
depend on the pumping hours, because `P_req x t_pump = rho g Q H / eta` is fixed
by the daily demand.
""",
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _areas(cluster: ParcelCluster) -> dict[CropCategory, float]:
    return {k: float(v) for k, v in cluster.area_by_category().items()}


def _surplus(
    selection: PumpSelection,
    psh: float,
    basis: EnergyBasis,
    hyd: HydraulicConfig,
    eng: EnergyConfig,
) -> float:
    return energy_balance(selection, psh, basis, hyd, eng).surplus_kwh_day


def _batch_row(
    well: SampledWell, solar: SolarClimatology
) -> tuple[dict[str, Any], bool]:
    """Results for one well's cluster and whether all invariants hold."""
    cluster = well.cluster
    demand = size_hydraulics(_areas(cluster))
    power, q_day = demand.required_power_kw, demand.daily_demand_m3
    identical = select_pumps(power, q_day, IDENTICAL)
    cheapest = select_pumps(power, q_day, LEAST_COST)
    monthly = {b: monthly_energy_balance(identical, solar, b) for b in EnergyBasis}
    annual = {b: energy_balance(identical, solar.annual_psh, b) for b in EnergyBasis}

    ok = (
        identical.motor_kw >= power
        and cheapest.motor_kw >= power
        and cheapest.capex_usd <= identical.capex_usd
        and all(0.0 <= bal.surplus_kwh_day <= bal.pv_generation_kwh_day
                for by_month in monthly.values() for bal in by_month.values())
        and all(monthly[DEMAND][m].surplus_kwh_day
                >= monthly[RATED][m].surplus_kwh_day for m in MONTHS)
    )
    row = {
        "well_lat": round(well.lat, 6), "well_lon": round(well.lon, 6),
        "centroid_lat": round(cluster.centroid_lat, 5),
        "centroid_lon": round(cluster.centroid_lon, 5),
        "n_parcels": cluster.n_parcels, "area_ha": cluster.total_area_clipped_ha,
        "Q_m3_day": q_day, "P_req_kW": power, "selection": identical.label,
        "motor_kW": identical.motor_kw,
        "n_pumps": identical.n_units, "capex_identical": identical.capex_usd,
        "capex_least_cost": cheapest.capex_usd,
        "exceeds_delivery": identical.exceeds_catalogue_delivery,
        "annual_psh": solar.annual_psh,
        "surplus_rated_kwh": annual[RATED].surplus_kwh_day,
        "surplus_demand_kwh": annual[DEMAND].surplus_kwh_day,
        "surplus_months_rated": sum(b.surplus_kwh_day > 0
                                    for b in monthly[RATED].values()),
    }
    return row, ok


def _monthly_figure(
    report: Report,
    solar: SolarClimatology,
    monthly: Mapping[EnergyBasis, Mapping[str, EnergyBalance]],
    e_pump: Mapping[EnergyBasis, float],
) -> str:
    pv = [monthly[RATED][m].pv_generation_kwh_day for m in MONTHS]
    fig, ax = plt.subplots(figsize=(8, 3.5))
    ax.bar(MONTHS, pv, color="#4c72b0", alpha=0.8, label="E_PV")
    ax.axhline(e_pump[RATED], color="k", lw=1.5, label="E_pump, rated (brief)")
    ax.axhline(e_pump[DEMAND], color="k", lw=1, ls="--",
               label="E_pump, demand (sensitivity)")
    excess = [max(0.0, v - e_pump[RATED]) for v in pv]
    ax.bar(MONTHS, excess, bottom=[min(v, e_pump[RATED]) for v in pv],
           color=SURPLUS_COLOUR, label="Surplus, rated")
    ax.set(ylabel="kWh/day", ylim=(0, max(pv) * 1.35),
           title=f"Monthly energy balance (annual PSH {solar.annual_psh:.2f})")
    ax.legend(fontsize=8, ncol=2, loc="upper left")
    return report.save_figure(fig, "monthly_balance",
                              "Monthly PV generation against pump consumption.")


def _batch_figure(report: Report, frame: pd.DataFrame) -> str:
    fig, (left, right) = plt.subplots(1, 2, figsize=(10, 3.5))
    left.hist(frame["P_req_kW"], bins=30, color="#4c72b0")
    for tier in PUMP_CATALOGUE:
        left.axvline(tier.motor_kw, color="grey", lw=0.8, ls=":")
        left.text(tier.motor_kw, left.get_ylim()[1] * 0.95, tier.name, rotation=90,
                  fontsize=7, ha="right", va="top")
    x_max = 1.05 * max(frame["P_req_kW"].max(),
                       max(t.motor_kw for t in PUMP_CATALOGUE))
    left.set(xlabel="Required power (kW)", ylabel="Clusters", xlim=(0, x_max),
             title="Required power and tier limits")
    right.scatter(frame["P_req_kW"], frame["surplus_demand_kwh"], s=10,
                  color=SURPLUS_COLOUR, alpha=0.7)
    right.axhline(0, color="k", lw=0.8)
    right.set(xlabel="Required power (kW)", ylabel="kWh/day", xlim=(0, x_max),
              title="Annual surplus, demand basis (sensitivity)")
    return report.save_figure(fig, "batch", "Batch run over random wells.")


def _share(mask: pd.Series) -> str:
    return f"{int(mask.sum()):,} ({mask.mean():.0%})"


if __name__ == "__main__":
    main()
