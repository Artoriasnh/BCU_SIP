# Copyright (c) 2026 Hao Ni. All rights reserved.
"""Evaluation of Task 2: solar resource, water demand and required pump power.

Run with ``python -m evaluation.task2_solar_hydraulics`` after the Task 1
evaluation (it reuses the demonstration site). The report is written to
``evaluation/results/task2/REPORT.md``. Internet access is needed for NASA POWER
and PVGIS; a section whose service is unreachable is reported as skipped.
"""

from __future__ import annotations

import argparse
import calendar
import dataclasses
import logging
import math
import tempfile
from collections.abc import Callable, Mapping, Sequence
from typing import Any, Final

import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
import requests

from dss.config import (
    DEFAULT_PARCELS_PATH,
    J_PER_KWH,
    MJ_PER_KWH,
    HydraulicConfig,
    SolarApiConfig,
)
from dss.exceptions import DataValidationError, SolarDataError
from dss.hydraulics import size_hydraulics
from dss.reference_tables import CROP_ARCHETYPES, PUMP_CATALOGUE, CropCategory
from dss.solar import MONTHS, NasaPowerClient, SolarClimatology, load_power_json
from dss.spatial import ParcelLayer
from evaluation.common import (
    BRIEF_ALENTEJO,
    RESULTS_ROOT,
    Report,
    demo_site,
    markdown_table,
)

logger = logging.getLogger(__name__)

FIXTURE = RESULTS_ROOT.parent.parent / "tests" / "fixtures" / "nasa_power_alentejo.json"
PVGIS_URL: Final[str] = "https://re.jrc.ec.europa.eu/api/v5_3/MRcalc"
TIMEOUT_S: Final[float] = 30.0

# North-to-south transect of mainland Portugal (city centres, decimal degrees).
TRANSECT: Final[tuple[tuple[str, float, float], ...]] = (
    ("Braganca", 41.806, -6.757),
    ("Porto", 41.150, -8.611),
    ("Coimbra", 40.203, -8.410),
    ("Lisbon", 38.722, -9.139),
    ("Evora", 38.571, -7.909),
    ("Faro", 37.019, -7.930),
)

# Mid-season crop coefficients, FAO Irrigation and Drainage Paper 56, Table 12
# (indicative values for a representative crop of each archetype).
FAO56_KC_MID: Final[dict[CropCategory, tuple[float, str]]] = {
    CropCategory.CEREALS: (1.20, "maize (grain)"),
    CropCategory.ORCHARDS: (0.70, "olives, 40-60 % ground cover"),
    CropCategory.PASTURES: (0.95, "rotated grazing pasture (0.85-1.05)"),
    CropCategory.VEGETABLES: (1.15, "tomato"),
    CropCategory.FALLBACK: (1.15, "dry beans"),
}

# July FAO Penman-Monteith ET0 at Evora (mm/day): Rodrigues & Braga (2021),
# Agriculture 11(2):124, Table 2.
EVORA_JULY_ET0_PM: Final[float] = 6.1
EVORA_STATION: Final[tuple[float, float]] = (38.57, -7.91)

# Period of NASA POWER daily data used for the Hargreaves estimate (inclusive).
DAILY_PERIOD: Final[tuple[str, str]] = ("20010101", "20201231")
NASA_DAILY_URL: Final[str] = "https://power.larc.nasa.gov/api/temporal/daily/point"
SOLAR_CONSTANT_MJ_M2_MIN: Final[float] = 0.0820  # FAO-56, eq. 21

# SECI (India) minimum daily output of DC solar pumps, litres per Wp, at
# 7.15 kWh/m2/day on the array plane with tracking.
SECI_L_PER_WP: Final[dict[float, float]] = {30.0: 38.0, 50.0: 23.0}


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
def main(argv: Sequence[str] | None = None) -> None:
    """Run the Task 2 evaluation and write the report."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--lat", type=float, help="well latitude (default: demo site)")
    parser.add_argument("--lon", type=float, help="well longitude (default: demo site)")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    lat, lon = (args.lat, args.lon) if args.lat is not None else demo_site()
    report = Report(
        "task2",
        "Task 2 evaluation: solar resource, water demand and pump power",
        "This report verifies the NASA POWER client (`dss.solar`) and the "
        "hydraulic sizing (`dss.hydraulics`): live data retrieval and unit "
        "handling, spatial plausibility, an independent cross-check against "
        "PVGIS, agronomic plausibility of the brief's water-demand coefficients, "
        "an independent recomputation of the pump power, the consistency of the "
        "brief's pump catalogue, parameter sensitivity, and error handling.",
    )
    report.register_data_file(FIXTURE)

    layer = ParcelLayer.from_file(DEFAULT_PARCELS_PATH)
    cluster = layer.extract_cluster(lat, lon)
    areas = {k: float(v) for k, v in cluster.area_by_category().items()}
    client = NasaPowerClient()

    evaluate_live_nasa(client, report, cluster.centroid_lat, cluster.centroid_lon)
    transect = evaluate_transect(client, report)
    evaluate_pvgis(transect, report)
    evaluate_water_demand(report, cluster.centroid_lat, cluster.centroid_lon)
    evaluate_hydraulics(report, areas)
    evaluate_pump_catalogue(report)
    evaluate_sensitivity(report, areas)
    evaluate_error_handling(report, cluster.centroid_lat, cluster.centroid_lon)

    print(f"Report written to {report.write()}")


# ---------------------------------------------------------------------------
# 1. Live NASA POWER retrieval
# ---------------------------------------------------------------------------
def evaluate_live_nasa(
    client: NasaPowerClient, report: Report, lat: float, lon: float
) -> None:
    """Fetch the demo centroid and the brief's point; check units and values."""
    try:
        demo = client.fetch(lat, lon)
        brief = client.fetch(*BRIEF_ALENTEJO)
        raw = _raw_power(lat, lon, ["ALLSKY_SFC_SW_DWN"])
    except (SolarDataError, requests.RequestException) as exc:
        report.add_section("1. Live NASA POWER retrieval", f"Skipped: {exc}")
        return

    units = raw["parameters"]["ALLSKY_SFC_SW_DWN"]["units"]
    same_cell_note = (
        "The demonstration site and the brief's point fall in the same cell, which "
        "is why their values below are identical; both were fetched live."
        if dict(demo.monthly_psh) == dict(brief.monthly_psh)
        else "The demonstration site and the brief's point fall in different cells."
    )
    sources = ", ".join(raw.get("header", {}).get("sources", [])) or "not reported"
    raw_ann = raw["properties"]["parameter"]["ALLSKY_SFC_SW_DWN"]["ANN"]
    stored = load_power_json(FIXTURE)
    max_diff = max(abs(brief.monthly_psh[m] - stored.monthly_psh[m]) for m in MONTHS)

    table = pd.DataFrame(
        {
            "Month": [*MONTHS, "ANN"],
            "Demo centroid PSH (kWh/m2/day)": [
                *(demo.monthly_psh[m] for m in MONTHS), demo.annual_psh
            ],
            "Brief point PSH, live": [
                *(brief.monthly_psh[m] for m in MONTHS), brief.annual_psh
            ],
            "Brief point PSH, stored fixture": [
                *(stored.monthly_psh[m] for m in MONTHS), stored.annual_psh
            ],
        }
    )
    report.check(
        "Live data come from the API, not a cache or file",
        demo.source == "api" and brief.source == "api",
        f"sources: {demo.source}, {brief.source}",
    )
    report.check(
        "Unit conversion follows the response metadata",
        math.isclose(demo.annual_psh, raw_ann / MJ_PER_KWH)
        if units == "MJ/m^2/day" else math.isclose(demo.annual_psh, raw_ann),
        f"API units '{units}'; raw ANN {raw_ann} -> {demo.annual_psh:.3f} kWh/m2/day",
    )
    report.check(
        "Live API reproduces the stored response",
        max_diff < 0.05,
        f"max monthly difference {max_diff:.4f} kWh/m2/day",
    )
    report.add_section(
        "1. Live NASA POWER retrieval",
        f"""
Each cluster is queried at its own centroid ({lat:.5f}, {lon:.5f} for the
demonstration site). The API reports units of **{units}** for `community=AG`, so
values are divided by 3.6 to obtain kWh/m2/day (peak sun hours). The design
month is the minimum month at each location: **{demo.design_month}**
({demo.design_psh:.2f} PSH) at the demonstration site.

The irradiance source reported by the API is **{sources}**, a 1-degree global
grid, so points in the same grid cell receive identical values. {same_cell_note}

{markdown_table(table)}
""",
    )


# ---------------------------------------------------------------------------
# 2. Spatial plausibility: north-south transect
# ---------------------------------------------------------------------------
def evaluate_transect(
    client: NasaPowerClient, report: Report
) -> dict[str, SolarClimatology]:
    """Irradiance should increase from north to south."""
    results: dict[str, SolarClimatology] = {}
    for name, lat, lon in TRANSECT:
        try:
            results[name] = client.fetch(lat, lon)
        except SolarDataError as exc:
            logger.warning("NASA POWER failed for %s: %s", name, exc)
    if len(results) < 3:
        report.add_section("2. North-south transect", "Skipped: NASA unavailable.")
        return results

    frame = pd.DataFrame(
        [
            {"City": name, "Latitude": lat, "Annual PSH": results[name].annual_psh,
             "Design month": results[name].design_month,
             "Design PSH": results[name].design_psh,
             "July PSH": results[name].monthly_psh["JUL"]}
            for name, lat, _ in TRANSECT if name in results
        ]
    )
    rho = frame["Latitude"].corr(frame["Annual PSH"], method="spearman")

    fig, ax = plt.subplots(figsize=(8, 4))
    for name, solar in results.items():
        ax.plot(MONTHS, [solar.monthly_psh[m] for m in MONTHS], marker=".", label=name)
    ax.set(ylabel="PSH (kWh/m2/day)", title="NASA POWER monthly climatology")
    ax.legend(ncol=3, fontsize=8)
    figure = report.save_figure(fig, "transect", "Monthly PSH, north to south.")

    report.check(
        "Irradiance increases towards the south",
        rho < 0,
        f"Spearman rank correlation (latitude, annual PSH) = {rho:.2f}",
    )
    report.add_section(
        "2. Spatial plausibility: north-south transect",
        f"""
{markdown_table(frame)}

{figure}

Annual irradiance rises from north to south, with one expected exception:
inland Braganca receives more than coastal Porto, where Atlantic cloud cover is
more frequent.
""",
    )
    return results


# ---------------------------------------------------------------------------
# 3. Independent cross-check against PVGIS
# ---------------------------------------------------------------------------
def evaluate_pvgis(nasa: Mapping[str, SolarClimatology], report: Report) -> None:
    """Compare NASA POWER with PVGIS horizontal irradiation at the same points."""
    rows, pairs = [], []
    for name, lat, lon in TRANSECT:
        if name not in nasa:
            continue
        try:
            monthly, annual = _pvgis_climatology(lat, lon)
        except (requests.RequestException, KeyError, TypeError, ValueError) as exc:
            logger.warning("PVGIS failed for %s: %s", name, exc)
            continue
        n_ann = nasa[name].annual_psh
        errors = [nasa[name].monthly_psh[m] / monthly[m] - 1 for m in MONTHS]
        rows.append(
            {"City": name, "NASA annual PSH": n_ann, "PVGIS annual PSH": annual,
             "Annual bias (NASA/PVGIS - 1)": f"{n_ann / annual - 1:+.1%}",
             "Monthly MAPE": f"{np.mean(np.abs(errors)):.1%}"}
        )
        pairs += [(monthly[m], nasa[name].monthly_psh[m]) for m in MONTHS]
    if not rows:
        report.add_section("3. Cross-check against PVGIS", "Skipped: no PVGIS data.")
        return

    frame = pd.DataFrame(rows)
    biases = [nasa[r["City"]].annual_psh / r["PVGIS annual PSH"] - 1 for r in rows]
    fig, ax = plt.subplots(figsize=(4.5, 4.5))
    x, y = zip(*pairs, strict=True)
    ax.plot(x, y, ".", alpha=0.7)
    ax.plot([0, 9], [0, 9], "k--", lw=0.8)
    ax.set(xlabel="PVGIS PSH (kWh/m2/day)", ylabel="NASA POWER PSH (kWh/m2/day)",
           title="Monthly means, transect cities", aspect="equal")
    figure = report.save_figure(fig, "pvgis_vs_nasa", "NASA POWER vs PVGIS.")
    report.check(
        "NASA POWER agrees with PVGIS within 10 % (annual)",
        max(abs(b) for b in biases) < 0.10,
        f"annual biases between {min(biases):+.1%} and {max(biases):+.1%}",
    )
    report.add_section(
        "3. Independent cross-check against PVGIS",
        f"""
PVGIS (EU Joint Research Centre, satellite-derived) is an independent source of
global horizontal irradiation. Its monthly totals (`MRcalc`, `H(h)_m`, kWh/m2 per
month) are converted to mean daily values and averaged over all available years.
Satellite products typically differ from ground measurements by a few per cent
on an annual basis, so agreement within 10 % is the acceptance threshold.

{markdown_table(frame)}

{figure}
""",
    )


# ---------------------------------------------------------------------------
# 4. Agronomic plausibility of the brief's water demand
# ---------------------------------------------------------------------------
def evaluate_water_demand(report: Report, lat: float, lon: float) -> None:
    """Compare the brief's coefficients with FAO-56 peak crop evapotranspiration.

    The primary benchmark is the FAO Penman-Monteith ET0 published for the nearest
    weather station (Evora, July). A Hargreaves estimate is reported alongside,
    computed day by day from NASA POWER daily temperatures and FAO-56
    extraterrestrial radiation, with all inputs listed.
    """
    try:
        daily = _daily_temperatures(lat, lon, DAILY_PERIOD)
    except (requests.RequestException, KeyError, TypeError, ValueError) as exc:
        report.add_section("4. Plausibility of water demand", f"Skipped: {exc}")
        return

    daily["ra_mj"] = [_extraterrestrial_radiation(lat, day) for day in daily.index]
    daily["et0"] = (
        0.0023
        * (daily["T2M"] + 17.8)
        * np.sqrt((daily["T2M_MAX"] - daily["T2M_MIN"]).clip(lower=0))
        * 0.408
        * daily["ra_mj"]
    )
    monthly = daily.groupby(daily.index.month).mean()
    monthly.index = [MONTHS[m - 1] for m in monthly.index]
    et0 = monthly["et0"].to_dict()
    peak_month = max(et0, key=et0.__getitem__)
    hargreaves_peak = et0[peak_month]
    daily_tmax_jul = float(monthly.loc["JUL", "T2M_MAX"])
    climatology_note = _climatology_tmax_note(lat, lon, daily_tmax_jul)
    inputs = pd.DataFrame(
        {
            "Month": monthly.index,
            "Mean daily Tmax (C)": monthly["T2M_MAX"].round(1),
            "Mean daily Tmin (C)": monthly["T2M_MIN"].round(1),
            "Mean T (C)": monthly["T2M"].round(1),
            "Ra, FAO-56 (MJ/m2/day)": monthly["ra_mj"].round(1),
            "ET0 Hargreaves (mm/day)": monthly["et0"].round(2),
        }
    )

    rows, ratios = [], {}
    for category, archetype in CROP_ARCHETYPES.items():
        kc, crop = FAO56_KC_MID[category]
        etc_station = kc * EVORA_JULY_ET0_PM
        brief_mm = archetype.water_demand_m3_per_ha_day / 10  # 10 m3/ha = 1 mm
        ratios[category.value] = brief_mm / etc_station
        rows.append(
            {"Archetype": category.value, "FAO-56 reference crop": crop, "Kc mid": kc,
             "Peak ETc, Evora PM (mm/day)": round(etc_station, 2),
             "Peak ETc, NASA Hargreaves (mm/day)": round(kc * hargreaves_peak, 2),
             "Brief demand (mm/day)": brief_mm,
             "Brief / ETc (Evora PM)": f"{brief_mm / etc_station:.0%}"}
        )
    low = ", ".join(k for k, r in ratios.items() if r < 0.7)
    high = ", ".join(k for k, r in ratios.items() if r >= 0.7)
    excess = hargreaves_peak / EVORA_JULY_ET0_PM - 1
    station_km = _haversine_km(lat, lon, *EVORA_STATION)
    gap_note = (
        "which lies within that documented range."
        if -0.05 <= excess <= 0.23
        else "which lies outside that range, so the estimate is not used for the "
        "finding."
    )

    fig, ax = plt.subplots(figsize=(7, 3.5))
    ax.plot(MONTHS, [et0[m] for m in MONTHS], marker="o",
            label="Hargreaves, NASA daily inputs")
    ax.plot("JUL", EVORA_JULY_ET0_PM, "r*", ms=12, label="Penman-Monteith, Evora")
    ax.set(ylabel="ET0 (mm/day)", title="Reference evapotranspiration")
    ax.legend()
    figure = report.save_figure(fig, "et0", "Monthly ET0 at the demonstration site.")
    first, last = DAILY_PERIOD
    report.add_section(
        "4. Agronomic plausibility of the brief's water demand (finding)",
        f"""
**Benchmark.** Peak (July) reference evapotranspiration computed with FAO
Penman-Monteith from station data at Evora, {station_km:.0f} km from the
demonstration site, is {EVORA_JULY_ET0_PM} mm/day (Rodrigues & Braga, 2021,
*Agriculture* 11(2), 124; 5.7-7.3 mm/day across 14 Alentejo stations).
Peak crop evapotranspiration is `ETc = Kc_mid x ET0` with FAO-56 mid-season
coefficients. July rainfall is negligible, so ETc approximates the net irrigation
requirement; the gross requirement is larger once application losses are
included.

{markdown_table(pd.DataFrame(rows))}

**Finding.** The brief's coefficients are below 70 % of FAO-56 peak demand for
archetypes {low or "none"} and at or above 70 % for {high or "none"}; low values
are better read as season-average or deficit-irrigation figures. Headline results
keep the brief's values (comparability across candidates); this comparison bounds
the uncertainty and motivates the Monte Carlo extension.

**Secondary estimate.** ET0 is computed for every day of {first[:4]}-{last[:4]}
with the Hargreaves equation (FAO-56, eq. 52) from NASA POWER *daily*
`T2M_MAX`, `T2M_MIN` and `T2M`, and extraterrestrial radiation from FAO-56
eq. 21, then averaged by month. It gives {hargreaves_peak:.1f} mm/day in
{peak_month}, {excess:+.0%} relative to the station benchmark. Hargreaves is known
to over-estimate Penman-Monteith in Alentejo by 0-23 % (regional slope 1.08; same
source), {gap_note}

Why daily data: {climatology_note}

{markdown_table(inputs)}

{figure}
""",
    )


# ---------------------------------------------------------------------------
# 5. Independent recomputation of the pump power
# ---------------------------------------------------------------------------
def evaluate_hydraulics(report: Report, areas: Mapping[CropCategory, float]) -> None:
    """Recompute demand and power by hand, showing every unit conversion."""
    cfg = HydraulicConfig()
    engine = size_hydraulics(areas, cfg)

    q_day = sum(
        area * CROP_ARCHETYPES[category].water_demand_m3_per_ha_day
        for category, area in areas.items()
    )
    rho_g = cfg.water_density_kg_m3 * cfg.gravity_m_s2
    work_j = rho_g * q_day * cfg.total_dynamic_head_m
    energy_kwh = work_j / J_PER_KWH
    hydraulic_kw = energy_kwh / cfg.pumping_hours_per_day
    electrical_kw = hydraulic_kw / cfg.pump_efficiency

    trail = pd.DataFrame(
        {
            "Quantity": ["Irrigated area", "Daily demand Q_day", "Flow over 6 h",
                         "Hydraulic work rho*g*Q*H", "Hydraulic energy",
                         "Hydraulic (output) power", "Electrical (input) power / eta"],
            "Value": [sum(areas.values()), q_day, q_day / cfg.pumping_hours_per_day,
                      work_j, energy_kwh, hydraulic_kw, electrical_kw],
            "Unit": ["ha", "m3/day", "m3/h", "J/day", "kWh/day", "kW", "kW"],
        }
    )
    by_category = pd.DataFrame(
        {
            "Archetype": [k.value for k in areas],
            "Area (ha)": list(areas.values()),
            "Demand coefficient (m3/ha/day)": [
                CROP_ARCHETYPES[k].water_demand_m3_per_ha_day for k in areas
            ],
            "Demand (m3/day)": [engine.demand_by_category_m3_day[k] for k in areas],
        }
    )
    report.check(
        "Engine reproduces the hand calculation",
        math.isclose(engine.daily_demand_m3, q_day, rel_tol=1e-12)
        and math.isclose(engine.required_power_kw, electrical_kw, rel_tol=1e-12),
        f"Q = {engine.daily_demand_m3:.3f} m3/day, "
        f"P = {engine.required_power_kw:.3f} kW",
    )
    report.add_section(
        "5. Independent recomputation for the demonstration cluster",
        f"""
{markdown_table(by_category)}

Unit trail (the brief's "hydraulic power" includes the efficiency, so it is the
electrical input power the motor must deliver):

{markdown_table(trail)}
""",
    )


# ---------------------------------------------------------------------------
# 6. Consistency of the brief's pump catalogue
# ---------------------------------------------------------------------------
def evaluate_pump_catalogue(report: Report) -> None:
    """Back-calculate the catalogue's delivery column from first principles."""
    cfg = HydraulicConfig()
    rho_g = cfg.water_density_kg_m3 * cfg.gravity_m_s2
    kwh_per_m3 = rho_g * cfg.total_dynamic_head_m / J_PER_KWH
    seci_40m = np.interp(40.0, list(SECI_L_PER_WP), list(SECI_L_PER_WP.values()))
    rows = []
    for tier in PUMP_CATALOGUE:
        formula_q = (
            tier.motor_kw * cfg.pumping_hours_per_day * cfg.pump_efficiency / kwh_per_m3
        )
        energy_needed = tier.max_delivery_m3_day * kwh_per_m3  # kWh/day, hydraulic
        rows.append(
            {
                "Tier": tier.name,
                "Motor (kW)": tier.motor_kw,
                "PV (kWp)": tier.pv_kwp,
                "Catalogue (m3/day)": tier.max_delivery_m3_day,
                "Brief formula (m3/day)": round(formula_q, 1),
                "Ratio": round(tier.max_delivery_m3_day / formula_q, 2),
                "Implied head at eta 0.55, 6 h (m)": round(
                    cfg.total_dynamic_head_m * formula_q / tier.max_delivery_m3_day, 1
                ),
                "PSH needed, 100 % conversion": round(energy_needed / tier.pv_kwp, 2),
                "PSH needed, eta 0.55 and derate 0.75": round(
                    energy_needed / (tier.pv_kwp * cfg.pump_efficiency * 0.75), 1
                ),
                "SECI benchmark at 40 m (m3/day)": round(seci_40m * tier.pv_kwp, 0),
            }
        )
    report.add_section(
        "6. Consistency of the brief's pump catalogue (finding)",
        f"""
Lifting 1 m3 by 40 m requires {kwh_per_m3:.4f} kWh of hydraulic energy. The
catalogue's "max delivery at 40 m" is 2.2-2.6 times what the brief's own formula
gives for the rated motor power, and would require 13-15 kWh/m2/day of
irradiation with the brief's efficiency and derate, more than the clear-sky
maximum at the Earth's surface. The figures are consistent with a head of only
about 16-18 m, and exceed the SECI minimum-output benchmark (interpolated to
{seci_40m:.1f} L/Wp/day at 40 m, under 7.15 kWh/m2/day with tracking) by a factor
of about 1.7-1.9.

{markdown_table(pd.DataFrame(rows))}

**Consequence.** The column is not used for selection (the brief selects by
motor power); it is used only to flag clusters whose demand exceeds the
catalogue figure.
""",
    )


# ---------------------------------------------------------------------------
# 7. Sensitivity of the required power
# ---------------------------------------------------------------------------
def evaluate_sensitivity(report: Report, areas: Mapping[CropCategory, float]) -> None:
    """One-at-a-time +/-20 % perturbations of the hydraulic inputs (tornado)."""
    base_cfg = HydraulicConfig()
    base = size_hydraulics(areas, base_cfg).required_power_kw

    def with_demand(scale: float) -> float:
        archetypes = {
            k: dataclasses.replace(
                v, water_demand_m3_per_ha_day=v.water_demand_m3_per_ha_day * scale
            )
            for k, v in CROP_ARCHETYPES.items()
        }
        return size_hydraulics(areas, base_cfg, archetypes).required_power_kw

    def with_config(field: str) -> Callable[[float], float]:
        def run(scale: float) -> float:
            value = getattr(base_cfg, field) * scale
            if field == "pump_efficiency":
                value = min(value, 1.0)  # efficiency cannot exceed 1
            cfg = dataclasses.replace(base_cfg, **{field: value})
            return size_hydraulics(areas, cfg).required_power_kw

        return run

    inputs: dict[str, Callable[[float], float]] = {
        "Water demand coefficients": with_demand,
        "Total dynamic head": with_config("total_dynamic_head_m"),
        "Pump efficiency": with_config("pump_efficiency"),
        "Pumping hours": with_config("pumping_hours_per_day"),
    }
    rows = [
        {"Input": name, "-20 %": run(0.8), "+20 %": run(1.2)}
        for name, run in inputs.items()
    ]
    frame = pd.DataFrame(rows)
    frame["Span (kW)"] = (frame["+20 %"] - frame["-20 %"]).abs()
    frame = frame.sort_values("Span (kW)")

    fig, ax = plt.subplots(figsize=(7, 3))
    for i, row in enumerate(frame.itertuples()):
        low, high = sorted((row[2] / base - 1, row[3] / base - 1))
        ax.barh(i, (high - low) * 100, left=low * 100, color="#4c72b0")
    ax.axvline(0, color="k", lw=1)
    ax.set_yticks(range(len(frame)), frame["Input"])
    ax.set(xlabel="Change in required power (%)",
           title=f"One-at-a-time +/-20 % (base {base:.2f} kW)")
    figure = report.save_figure(fig, "tornado", "Sensitivity of the required power.")
    report.add_section(
        "7. Sensitivity of the required power",
        f"""
Required power is proportional to head and demand and inversely proportional to
efficiency and pumping hours, so +/-20 % on any input moves it by -17 % to +25 %.
Whether a cluster crosses the single-pump limit (11 kW) can therefore depend on
input uncertainty; this is the natural target of a Monte Carlo analysis.

{markdown_table(frame)}

{figure}
""",
    )


# ---------------------------------------------------------------------------
# 8. Error handling against the live service
# ---------------------------------------------------------------------------
def evaluate_error_handling(report: Report, lat: float, lon: float) -> None:
    """Exercise failure modes end to end and record the observed behaviour."""
    fast = {"backoff_s": 0.0}
    cache_dir = tempfile.mkdtemp(prefix="dss-power-cache-")
    down = "https://power.invalid/api"

    def observe(func: Callable[[], SolarClimatology]) -> str:
        try:
            return f"returned (source: {func().source})"
        except DataValidationError:
            return "DataValidationError"
        except SolarDataError as exc:
            return f"SolarDataError: {exc}"

    cases: list[tuple[str, str, Callable[[], SolarClimatology]]] = [
        ("Latitude 95 (invalid)", "DataValidationError",
         lambda: NasaPowerClient().fetch(95.0, lon)),
        ("Unknown endpoint (HTTP 4xx, not retried)", "SolarDataError",
         lambda: NasaPowerClient(SolarApiConfig(
             url="https://power.larc.nasa.gov/api/temporal/climatology/nope",
             max_retries=0)).fetch(lat, lon)),
        ("Unreachable host (retried once)", "SolarDataError",
         lambda: NasaPowerClient(
             SolarApiConfig(url=down, max_retries=1, **fast)).fetch(lat, lon)),
        ("1 ms timeout (retried once)", "SolarDataError",
         lambda: NasaPowerClient(
             SolarApiConfig(timeout_s=0.001, max_retries=1, **fast)).fetch(lat, lon)),
        ("Populate disk cache", "returned (source: api)",
         lambda: NasaPowerClient(cache_dir=cache_dir).fetch(lat, lon)),
        ("Service down, disk cache present", "returned (source: disk-cache)",
         lambda: NasaPowerClient(SolarApiConfig(url=down, max_retries=0),
                                 cache_dir=cache_dir).fetch(lat, lon)),
    ]  # fmt: skip
    rows = []
    for name, expected, func in cases:
        observed = observe(func)
        rows.append(
            {"Scenario": name, "Expected": expected, "Observed": observed[:100],
             "Result": "PASS" if observed.startswith(expected) else "FAIL"}
        )
    frame = pd.DataFrame(rows)
    report.check(
        "Failure modes handled as designed (live)",
        bool((frame["Result"] == "PASS").all()),
        f"{(frame['Result'] == 'PASS').sum()} / {len(frame)} scenarios",
    )
    report.add_section(
        "8. Error handling against the live service",
        f"""
Unit tests cover these paths with a fake HTTP session; here they are exercised
against the real network stack.

{markdown_table(frame)}
""",
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _raw_power(lat: float, lon: float, parameters: Sequence[str]) -> dict[str, Any]:
    """Raw NASA POWER climatology response for arbitrary parameters."""
    cfg = SolarApiConfig()
    response = requests.get(
        cfg.url,
        params={"parameters": ",".join(parameters), "community": cfg.community,
                "longitude": lon, "latitude": lat, "format": "JSON"},
        timeout=TIMEOUT_S,
    )
    response.raise_for_status()
    payload: dict[str, Any] = response.json()
    return payload


def _pvgis_climatology(lat: float, lon: float) -> tuple[dict[str, float], float]:
    """Mean daily horizontal irradiation per month and per year from PVGIS."""
    response = requests.get(
        PVGIS_URL,
        params={"lat": lat, "lon": lon, "horirrad": 1, "outputformat": "json"},
        timeout=TIMEOUT_S,
    )
    response.raise_for_status()
    records = response.json()["outputs"]["monthly"]
    frame = pd.DataFrame(records)
    frame["days"] = [calendar.monthrange(int(y), int(m))[1]
                     for y, m in zip(frame["year"], frame["month"], strict=True)]
    frame["daily"] = frame["H(h)_m"] / frame["days"]
    by_month = frame.groupby("month")["daily"].mean()
    monthly = {MONTHS[int(m) - 1]: float(v) for m, v in by_month.items()}
    yearly = frame.groupby("year").agg(total=("H(h)_m", "sum"), days=("days", "sum"))
    annual = float((yearly["total"] / yearly["days"]).mean())
    return monthly, annual


def _daily_temperatures(
    lat: float, lon: float, period: tuple[str, str]
) -> pd.DataFrame:
    """NASA POWER daily 2 m temperatures (C), indexed by date; fill values dropped."""
    cfg = SolarApiConfig()
    response = requests.get(
        NASA_DAILY_URL,
        params={"parameters": "T2M_MAX,T2M_MIN,T2M", "community": cfg.community,
                "longitude": lon, "latitude": lat, "start": period[0],
                "end": period[1], "format": "JSON"},
        timeout=120.0,
    )
    response.raise_for_status()
    payload = response.json()
    fill = payload.get("header", {}).get("fill_value", -999.0)
    frame = pd.DataFrame(payload["properties"]["parameter"])
    frame.index = pd.to_datetime(frame.index, format="%Y%m%d")
    frame = frame.replace(fill, np.nan).dropna()
    if frame.empty:
        raise ValueError("NASA POWER returned no valid daily temperatures.")
    return frame


def _climatology_tmax_note(lat: float, lon: float, daily_tmax_jul: float) -> str:
    """Compare climatology July maxima with the mean of the daily maxima."""
    first, last = DAILY_PERIOD
    try:
        raw = _raw_power(lat, lon, ["T2M_MAX_AVG", "T2M_MAX"])
        values = raw["properties"]["parameter"]
        avg, extreme = values["T2M_MAX_AVG"]["JUL"], values["T2M_MAX"]["JUL"]
    except (requests.RequestException, KeyError, TypeError, ValueError):
        return (
            "Hargreaves requires mean daily temperature extremes, which are "
            "computed here directly from the daily series."
        )
    return (
        "NASA documents `T2M_MAX_AVG` as the average of the daily maximum "
        f"temperature. At this site, however, the climatology endpoint returns "
        f"{avg:.1f} C for July (`T2M_MAX`: {extreme:.1f} C), whereas the mean of "
        f"NASA's own daily maxima for {first[:4]}-{last[:4]} is "
        f"{daily_tmax_jul:.1f} C. The cause of this difference is not documented, "
        "so ET0 is computed from the daily series, which matches the definition "
        "required by the Hargreaves equation."
    )


def _extraterrestrial_radiation(lat: float, day: pd.Timestamp) -> float:
    """Daily extraterrestrial radiation Ra (MJ/m2/day), FAO-56 eqs. 21-25."""
    phi = math.radians(lat)
    j = day.dayofyear
    dr = 1 + 0.033 * math.cos(2 * math.pi * j / 365)
    delta = 0.409 * math.sin(2 * math.pi * j / 365 - 1.39)
    omega = math.acos(max(-1.0, min(1.0, -math.tan(phi) * math.tan(delta))))
    return (
        24 * 60 / math.pi
        * SOLAR_CONSTANT_MJ_M2_MIN
        * dr
        * (omega * math.sin(phi) * math.sin(delta)
           + math.cos(phi) * math.cos(delta) * math.sin(omega))
    )


def _haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance in kilometres (mean Earth radius 6371 km)."""
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * 6371.0 * math.asin(math.sqrt(a))


if __name__ == "__main__":
    main()
