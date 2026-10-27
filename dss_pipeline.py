# Copyright (c) 2026 Hao Ni. All rights reserved.
"""Agri-Energy Decision Support System: end-to-end pipeline for one tube well.

Usage::

    python dss_pipeline.py --lat 38.07759 --lon -7.96330
    python dss_pipeline.py --lat 38.07759 --lon -7.96330 --demand-based --json

or from Python::

    pipeline = DssPipeline.from_file()          # load and index parcels once
    result = pipeline.run(38.07759, -7.96330)   # Tasks 1-4 for one well
    print(result.summary())

``run_demo.py`` runs the pipeline on valid and invalid coordinates. Design
decisions and their evidence are listed in ``ASSUMPTIONS.md`` (A1-A13).

Architecture
------------
One module per concern; this file only orchestrates them::

    Task 1   dss.spatial     parcels -> 150 m command-zone cluster
    Task 2   dss.solar       NASA POWER climatology at the cluster centroid
             dss.hydraulics  water demand and required pump power
    Task 3   dss.energy      pump selection and daily energy balance
    Task 4   dss.economics   PUE selection and simple payback

The pipeline has two stages. ``DssPipeline.locate`` performs the
location-dependent, I/O-bound steps (cluster extraction and the NASA POWER
request). ``appraise`` performs the parameter-dependent steps as pure
functions of a ``Site`` and a ``Scenario``. Reference parameters live in
``dss.reference_tables`` (transcribed from the brief) and tunable parameters
in frozen, validated dataclasses in ``dss.config``.

Coordinate reference systems and spatial indexing
--------------------------------------------------
Inputs are WGS84 decimal degrees (EPSG:4326). Parcels are reprojected once to
ETRS89 / Portugal TM06 (EPSG:3763), a metric projection, so that the 150 m
buffer and all areas are planar computations in metres (area error against
geodesic areas below 0.05 %, see the Task 1 evaluation). The well is
projected to EPSG:3763 before buffering; buffering in degrees would give an
ellipse whose size depends on latitude. Parcels are indexed once in an
R-tree (``GeoDataFrame.sindex``); each query retrieves candidates whose
bounding boxes intersect the buffer and then applies the exact
intersection, so a query takes milliseconds on 68,000 parcels. Parcels
crossing the buffer contribute only their area inside it (ASSUMPTIONS.md, A3).
The cluster centroid is computed in EPSG:3763 and converted back to EPSG:4326
for the NASA POWER request.

Units
-----
====================  ===========  ==============================================
Quantity              Unit         Note
====================  ===========  ==============================================
Area                  ha           m2 / 10,000, computed in EPSG:3763
Water demand Q_day    m3/day       area (ha) x coefficient (m3/ha/day)
Irradiance, PSH       kWh/m2/day   API returns MJ/m2/day for community AG: / 3.6
Pump power P          kW           rho g Q H / (3.6e6 J/kWh x t_pump x eta)
PV capacity           kWp          power, not energy
E_PV, E_pump, E_ex    kWh/day      energy per day
Harvest               kg/year      area x yield (t/ha) x 1000 kg/t
Uplift, CAPEX         USD/year,    payback = CAPEX / annual net uplift (years)
                      USD
====================  ===========  ==============================================

Power (kW, kWp) and energy (kWh/day) are never mixed in a name. The brief's
"hydraulic power" includes the efficiency, so it is the electrical power the
motor must deliver.

Error handling
--------------
* ``DataValidationError``: coordinates or parameters out of range.
* ``EmptyClusterError``: no agricultural parcel within 150 m, with the exact
  message required by the brief.
* ``SolarDataError``: NASA POWER unavailable after retries (10 s timeout,
  exponential back-off on timeouts, connection errors, HTTP 429 and 5xx) and
  no cached response; malformed or fill-value responses are rejected.
* No PUE investment is a valid outcome, not an error: ``PueDecision`` carries
  a status (``insufficient_surplus``, ``no_compatible_crops`` or
  ``not_profitable``) and payback is ``None``; it is never divided by zero.

Key assumptions
---------------
Headline results use only the brief's parameters. Where the brief and the
data disagree the choice is documented in ``ASSUMPTIONS.md``; the most
consequential are the HCAT mapping (A1: all codes in the data start with
``33``, so the brief's two-digit table matches nothing), the NASA POWER unit
conversion (A5), and the finding that the brief's annual formula gives zero
surplus everywhere in mainland Portugal (A7), which is why a monthly
screening and a demand-based consumption scenario (A13) are reported.

Monte Carlo extension for regional policy analysis
--------------------------------------------------
The engine is a deterministic function of a location and a ``Scenario``,
which makes a Monte Carlo wrapper straightforward::

    rng = np.random.default_rng(seed)
    sites = [pipeline.locate(lat, lon) for lat, lon in wells]   # once per well
    for draw in range(n_draws):
        scenario = sample_scenario(rng, policy)                 # e.g. +/- head,
        for site in sites:                                      # eta, derate,
            results.append(appraise(site, scenario))            # prices, CAPEX

1. *Sample locations* across the region of interest, e.g. wells at random
   agricultural parcels, and call ``locate`` once per well. Solar data are
   on a 1-degree grid, so requests can be shared per grid cell.
2. *Sample uncertain inputs* per draw from documented distributions: water
   demand coefficients, head, pump efficiency, PV derate, irradiance bias,
   yields and prices. Each draw is a ``Scenario`` built with
   ``dataclasses.replace``, so the configuration is validated and immutable.
3. *Encode policy incentives* as scenario changes: a CAPEX subsidy scales
   ``capex_usd`` in the pump or PUE catalogue, PV oversizing scales
   ``pv_kwp``, and an O&M support scheme changes ``annual_om_fraction``.
4. *Aggregate* the structured results: probability of investment, payback
   distribution, adopted asset mix and surplus energy, per policy and
   region. ``appraise`` takes microseconds and has no side effects, so
   draws parallelise trivially and a fixed seed makes the study
   reproducible.

The deterministic engine already shows which policy levers such a study should
compare. Under the brief's parameters the annual surplus is zero
(ASSUMPTIONS.md, A7), so a subsidy on PUE CAPEX shortens the payback of an
asset that is not feasible and produces no investment: the binding constraint
is energy, not capital. Scaling the PV array by a factor ``k`` divides the
surplus threshold (5.87-6.00 PSH) by ``k``. With ``k`` of about 1.4 every
mainland site has an annual surplus (annual PSH 4.4-5.0), and at the
demonstration site the dryer (10 kWh/day) becomes feasible at ``k`` of about
1.75 (5 kWp x k x 4.90 PSH x 0.75 - 22.2 kWh/day >= 10 kWh/day). A regional
study would therefore compare PV oversizing with CAPEX subsidies, sampling the
uncertain inputs around each policy.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import TYPE_CHECKING, Any

from dss.config import (
    DEFAULT_PARCELS_PATH,
    EconomicConfig,
    EnergyConfig,
    HydraulicConfig,
)
from dss.economics import PueDecision, evaluate_pue, feasible_assets_by_month
from dss.energy import (
    EnergyBalance,
    EnergyBasis,
    PumpSelection,
    PumpStrategy,
    energy_balance,
    monthly_energy_balance,
    select_pumps,
)
from dss.exceptions import DataValidationError, EmptyClusterError, SolarDataError
from dss.hydraulics import HydraulicDemand, size_hydraulics
from dss.reference_tables import (
    CROP_ARCHETYPES,
    PUE_CATALOGUE,
    PUMP_CATALOGUE,
    CropArchetype,
    CropCategory,
    PueAsset,
    PumpTier,
)
from dss.solar import MONTHS, NasaPowerClient, SolarClimatology
from dss.validation import validate_areas

if TYPE_CHECKING:  # pragma: no cover - the geospatial stack is imported lazily
    from dss.spatial import ParcelCluster, ParcelLayer

EXIT_OK, EXIT_BAD_INPUT, EXIT_NO_SOLAR, EXIT_NO_DATA = 0, 2, 3, 4
DATA_HINT = (
    "Download PT.zip from https://zenodo.org/records/10118572/files/PT.zip and "
    "place it in data/raw/ (no need to unzip), or pass another layer with --data."
)


@dataclass(frozen=True, slots=True)
class Scenario:
    """Parameters of one appraisal. Defaults reproduce the brief.

    Attributes:
        hydraulic: Head, efficiency, pumping hours (Task 2).
        energy: PV derate (Task 3).
        economic: Harvests per year and PUE O&M (Task 4).
        energy_basis: Pump consumption: rated motor power (brief) or the
            power the demand requires (sensitivity, ASSUMPTIONS.md, A13).
        pump_strategy: Combination of several pumps (ASSUMPTIONS.md, A8).
        archetypes: Crop parameters (brief, Table 1).
        pump_catalogue: Pump tiers (brief, Table 2).
        pue_catalogue: PUE assets (brief, Table 3).
    """

    hydraulic: HydraulicConfig = field(default_factory=HydraulicConfig)
    energy: EnergyConfig = field(default_factory=EnergyConfig)
    economic: EconomicConfig = field(default_factory=EconomicConfig)
    energy_basis: EnergyBasis = EnergyBasis.RATED
    pump_strategy: PumpStrategy = PumpStrategy.IDENTICAL
    archetypes: Mapping[CropCategory, CropArchetype] = field(
        default_factory=lambda: CROP_ARCHETYPES
    )
    pump_catalogue: tuple[PumpTier, ...] = PUMP_CATALOGUE
    pue_catalogue: tuple[PueAsset, ...] = PUE_CATALOGUE


@dataclass(frozen=True, slots=True)
class Site:
    """Location-dependent inputs of one tube well (Tasks 1 and 2a).

    Attributes:
        lat: Well latitude, decimal degrees (EPSG:4326).
        lon: Well longitude, decimal degrees (EPSG:4326).
        area_by_category_ha: Clipped area per crop archetype, ha.
        solar: Solar climatology at the cluster centroid.
        cluster: The parcel cluster, if the areas come from ``ParcelLayer``.
    """

    lat: float
    lon: float
    area_by_category_ha: Mapping[CropCategory, float]
    solar: SolarClimatology
    cluster: ParcelCluster | None = None

    def __post_init__(self) -> None:
        """Validate and freeze the areas."""
        areas = validate_areas(self.area_by_category_ha)
        object.__setattr__(self, "area_by_category_ha", MappingProxyType(areas))


@dataclass(frozen=True, slots=True)
class PipelineResult:
    """Outputs of Tasks 2-4 for one site and scenario.

    Attributes:
        site: Location-dependent inputs.
        scenario: Parameters used.
        hydraulics: Water demand and required pump power.
        pumps: Selected pump units.
        energy: Daily energy balance at the annual mean PSH (the brief's
            ``PSH_avg``); its surplus drives the PUE decision.
        monthly_energy: Daily energy balance for each month.
        decision: PUE investment decision.
        seasonal_assets: PUE assets meeting both requirements in each month
            (screening only, not annualised).
    """

    site: Site
    scenario: Scenario
    hydraulics: HydraulicDemand
    pumps: PumpSelection
    energy: EnergyBalance
    monthly_energy: Mapping[str, EnergyBalance]
    decision: PueDecision
    seasonal_assets: Mapping[str, tuple[str, ...]]

    @property
    def surplus_months(self) -> tuple[str, ...]:
        """Months with a positive daily surplus."""
        return tuple(m for m in MONTHS if self.monthly_energy[m].surplus_kwh_day > 0)

    def to_dict(self) -> dict[str, Any]:
        """JSON-serialisable summary of every task's output."""
        site, solar, pumps, energy = self.site, self.site.solar, self.pumps, self.energy
        return {
            "location": {"lat": site.lat, "lon": site.lon},
            "task1_cluster": {
                "n_parcels": site.cluster.n_parcels if site.cluster else None,
                "centroid": (
                    {"lat": site.cluster.centroid_lat, "lon": site.cluster.centroid_lon}
                    if site.cluster else None
                ),
                "area_by_category_ha": {
                    k.value: v for k, v in site.area_by_category_ha.items()
                },
                "total_area_ha": sum(site.area_by_category_ha.values()),
            },
            "task2_solar_hydraulics": {
                "annual_psh_kwh_m2_day": solar.annual_psh,
                "design_month": solar.design_month,
                "design_psh_kwh_m2_day": solar.design_psh,
                "solar_source": solar.source,
                "daily_demand_m3": self.hydraulics.daily_demand_m3,
                "required_power_kw": self.hydraulics.required_power_kw,
            },
            "task3_pumps_energy": {
                "pumps": pumps.label,
                "n_pumps": pumps.n_units,
                "multiple_pumps_required": pumps.multiple_pumps_required,
                "motor_kw": pumps.motor_kw,
                "pv_kwp": pumps.pv_kwp,
                "pump_capex_usd": pumps.capex_usd,
                "demand_exceeds_catalogue_delivery": pumps.exceeds_catalogue_delivery,
                "energy_basis": energy.basis.value,
                "e_pv_kwh_day": energy.pv_generation_kwh_day,
                "e_pump_kwh_day": energy.pump_consumption_kwh_day,
                "e_excess_kwh_day": energy.surplus_kwh_day,
                "surplus_months": list(self.surplus_months),
            },
            "task4_pue": {
                **self.decision.to_dict(),
                "seasonal_candidates": {
                    m: list(assets) for m, assets in self.seasonal_assets.items()
                },
            },
        }

    def summary(self) -> str:
        """Readable multi-line report."""
        site, solar, h, pumps, e = (
            self.site, self.site.solar, self.hydraulics, self.pumps, self.energy
        )
        crops = ", ".join(
            f"{CROP_ARCHETYPES[k].name} {v:.3f} ha"
            for k, v in site.area_by_category_ha.items()
        )
        parcels = f"{site.cluster.n_parcels} parcels, " if site.cluster else ""
        months_by_asset: dict[str, list[str]] = {}
        for month, assets in self.seasonal_assets.items():
            for asset in assets:
                months_by_asset.setdefault(asset, []).append(month)
        lines = [
            f"Well at ({site.lat:.5f}, {site.lon:.5f})",
            f"Task 1  Cluster: {parcels}{sum(site.area_by_category_ha.values()):.3f}"
            f" ha inside 150 m ({crops})",
            f"Task 2  Solar: annual PSH {solar.annual_psh:.2f} kWh/m2/day, design "
            f"month {solar.design_month} ({solar.design_psh:.2f}); source "
            f"{solar.source}",
            f"        Water demand {h.daily_demand_m3:.2f} m3/day; required pump "
            f"power {h.required_power_kw:.2f} kW",
            f"Task 3  Pumps: {pumps.label} ({pumps.motor_kw:g} kW motor, "
            f"{pumps.pv_kwp:g} kWp PV, USD {pumps.capex_usd:,.0f})"
            + (" - multiple pumps required" if pumps.multiple_pumps_required else ""),
            f"        E_PV {e.pv_generation_kwh_day:.2f} - E_pump "
            f"{e.pump_consumption_kwh_day:.2f} -> E_excess {e.surplus_kwh_day:.2f}"
            f" kWh/day ({e.basis.value} consumption)",
            f"        Months with surplus: {', '.join(self.surplus_months) or 'none'}",
            f"Task 4  {self.decision.status.value}: {self.decision.reason}",
            "        Seasonal screening: "
            + ("; ".join(f"{a} in {', '.join(m)}" for a, m in months_by_asset.items())
               or "no asset feasible in any month"),
        ]
        return "\n".join(lines)


def appraise(site: Site, scenario: Scenario | None = None) -> PipelineResult:
    """Tasks 2b-4 for one site: a pure function of its inputs.

    Args:
        site: Location-dependent inputs (``DssPipeline.locate``).
        scenario: Parameters. Defaults to the brief's values.

    Returns:
        Hydraulic sizing, pump selection, energy balance and PUE decision.
    """
    s = scenario or Scenario()
    areas = site.area_by_category_ha
    hydraulics = size_hydraulics(areas, s.hydraulic, s.archetypes)
    pumps = select_pumps(
        hydraulics.required_power_kw,
        hydraulics.daily_demand_m3,
        s.pump_strategy,
        s.pump_catalogue,
    )
    energy = energy_balance(
        pumps, site.solar.annual_psh, s.energy_basis, s.hydraulic, s.energy
    )
    monthly = monthly_energy_balance(
        pumps, site.solar, s.energy_basis, s.hydraulic, s.energy
    )
    decision = evaluate_pue(
        energy.surplus_kwh_day, areas, s.pue_catalogue, s.archetypes, s.economic
    )
    seasonal = feasible_assets_by_month(
        {m: b.surplus_kwh_day for m, b in monthly.items()},
        areas,
        s.pue_catalogue,
        s.archetypes,
    )
    return PipelineResult(site, s, hydraulics, pumps, energy, monthly, decision,
                          seasonal)


class DssPipeline:
    """Runs Tasks 1-4 for any number of wells on one parcel layer."""

    def __init__(
        self, layer: ParcelLayer, client: NasaPowerClient | None = None
    ) -> None:
        """Create a pipeline.

        Args:
            layer: Prepared, indexed parcel layer (Task 1).
            client: NASA POWER client (Task 2). Defaults to a client with the
                brief's settings and an in-memory cache.
        """
        self._layer = layer
        self._client = client or NasaPowerClient()

    @classmethod
    def from_file(
        cls,
        path: str | Path = DEFAULT_PARCELS_PATH,
        client: NasaPowerClient | None = None,
    ) -> DssPipeline:
        """Load, classify and index a parcel layer, then create a pipeline.

        Args:
            path: Parcel layer in the EuroCrops schema.
            client: NASA POWER client.

        Returns:
            A pipeline ready to run.

        Raises:
            FileNotFoundError: If the parcel layer does not exist.
        """
        if not Path(path).exists():
            raise FileNotFoundError(f"Parcel layer not found: {path}. {DATA_HINT}")
        from dss.spatial import ParcelLayer  # geospatial stack loaded on demand

        return cls(ParcelLayer.from_file(path), client)

    def locate(self, lat: float, lon: float) -> Site:
        """Task 1 and the solar part of Task 2 for one well.

        Args:
            lat: Well latitude, decimal degrees (EPSG:4326).
            lon: Well longitude, decimal degrees (EPSG:4326).

        Returns:
            The site: clipped crop areas and solar climatology.

        Raises:
            DataValidationError: If the coordinates are invalid.
            EmptyClusterError: If no agricultural parcel lies within 150 m.
            SolarDataError: If no solar data can be obtained.
        """
        cluster = self._layer.extract_cluster(lat, lon)
        solar = self._client.fetch(cluster.centroid_lat, cluster.centroid_lon)
        areas = validate_areas(cluster.area_by_category())
        return Site(float(lat), float(lon), areas, solar, cluster)

    def run(
        self, lat: float, lon: float, scenario: Scenario | None = None
    ) -> PipelineResult:
        """Tasks 1-4 for one well.

        Args:
            lat: Well latitude, decimal degrees (EPSG:4326).
            lon: Well longitude, decimal degrees (EPSG:4326).
            scenario: Parameters. Defaults to the brief's values.

        Returns:
            Outputs of every task.

        Raises:
            DataValidationError: If the coordinates are invalid.
            EmptyClusterError: If no agricultural parcel lies within 150 m.
            SolarDataError: If no solar data can be obtained.
        """
        return appraise(self.locate(lat, lon), scenario)


def main(argv: Sequence[str] | None = None, pipeline: DssPipeline | None = None) -> int:
    """Command-line entry point.

    Args:
        argv: Command-line arguments (defaults to ``sys.argv``).
        pipeline: Pipeline to use; loaded from ``--data`` if omitted.

    Returns:
        Exit status: 0 on success, 2 for invalid input or an empty cluster,
        3 if no solar data are available, 4 if the parcel layer is missing.
    """
    parser = argparse.ArgumentParser(
        description="Run the agri-energy DSS (Tasks 1-4) for one tube well."
    )
    parser.add_argument("--lat", type=float, required=True, help="decimal degrees")
    parser.add_argument("--lon", type=float, required=True, help="decimal degrees")
    parser.add_argument("--data", type=Path, default=DEFAULT_PARCELS_PATH,
                        help="parcel layer (default: %(default)s)")
    parser.add_argument("--demand-based", action="store_true",
                        help="pump consumption from the required power "
                             "(sensitivity, ASSUMPTIONS.md, A13)")
    parser.add_argument("--json", action="store_true", help="print JSON")
    args = parser.parse_args(argv)

    scenario = Scenario(
        energy_basis=EnergyBasis.DEMAND if args.demand_based else EnergyBasis.RATED
    )
    try:
        engine = pipeline or DssPipeline.from_file(args.data)
        result = engine.run(args.lat, args.lon, scenario)
    except (DataValidationError, EmptyClusterError) as exc:
        print(f"{type(exc).__name__}: {exc}", file=sys.stderr)
        return EXIT_BAD_INPUT
    except SolarDataError as exc:
        print(f"SolarDataError: {exc}", file=sys.stderr)
        return EXIT_NO_SOLAR
    except FileNotFoundError as exc:
        print(f"FileNotFoundError: {exc}", file=sys.stderr)
        return EXIT_NO_DATA
    print(json.dumps(result.to_dict(), indent=2) if args.json else result.summary())
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
