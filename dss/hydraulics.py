# Copyright (c) 2026 Hao Ni. All rights reserved.
"""Task 2b: irrigation water demand and required pump power.

    Q_day = sum_i(A_i * WD_i)                         [m3/day]
    P     = rho * g * Q_day * H / (3.6e6 * t * eta)   [kW]

Units: A_i in ha and WD_i in m3/ha/day give m3/day. In the power formula,
rho * g * Q_day * H is the hydraulic work in J/day; dividing by 3.6e6 J/kWh gives
kWh/day, by the pumping time t (h/day) gives kW, and by the pump-and-motor
efficiency eta gives the electrical power the motor must deliver.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType

import pandas as pd

from dss.config import J_PER_KWH, HydraulicConfig
from dss.exceptions import DataValidationError
from dss.reference_tables import CROP_ARCHETYPES, CropArchetype, CropCategory


@dataclass(frozen=True, slots=True)
class HydraulicDemand:
    """Water demand and pump power of a cluster.

    Attributes:
        demand_by_category_m3_day: Peak daily water demand per crop archetype,
            m3/day.
        daily_demand_m3: Total peak daily water demand Q_day, m3/day.
        flow_rate_m3_h: Average flow over the pumping window, m3/h.
        required_power_kw: Electrical power the pump motor must deliver, kW.
    """

    demand_by_category_m3_day: Mapping[CropCategory, float]
    daily_demand_m3: float
    flow_rate_m3_h: float
    required_power_kw: float


def water_demand_by_category(
    area_by_category_ha: Mapping[CropCategory, float] | pd.Series,
    archetypes: Mapping[CropCategory, CropArchetype] = CROP_ARCHETYPES,
) -> dict[CropCategory, float]:
    """Peak daily water demand per crop archetype.

    Args:
        area_by_category_ha: Irrigated area per archetype, ha.
        archetypes: Agronomic parameters per archetype (brief, Table 1).

    Returns:
        Water demand per archetype, m3/day.

    Raises:
        DataValidationError: If an area is negative or not finite, or an
            archetype has no reference parameters.
    """
    demand: dict[CropCategory, float] = {}
    for key, area_ha in area_by_category_ha.items():
        category = _as_category(key, archetypes)
        area = float(area_ha)
        if not math.isfinite(area) or area < 0:
            raise DataValidationError(f"Area for {category!r} is invalid: {area_ha}.")
        demand[category] = area * archetypes[category].water_demand_m3_per_ha_day
    return demand


def required_pump_power_kw(
    daily_demand_m3: float, config: HydraulicConfig | None = None
) -> float:
    """Electrical power needed to lift the daily demand within the pumping window.

    Args:
        daily_demand_m3: Daily volume to pump, m3/day.
        config: Hydraulic parameters. Defaults to the brief's values.

    Returns:
        Required motor power, kW.

    Raises:
        DataValidationError: If the volume is negative or not finite.
    """
    if not math.isfinite(daily_demand_m3) or daily_demand_m3 < 0:
        raise DataValidationError(
            f"daily_demand_m3 must be finite and >= 0, got {daily_demand_m3}."
        )
    cfg = config or HydraulicConfig()
    work_j_per_day = (
        cfg.water_density_kg_m3
        * cfg.gravity_m_s2
        * daily_demand_m3
        * cfg.total_dynamic_head_m
    )
    return work_j_per_day / (
        J_PER_KWH * cfg.pumping_hours_per_day * cfg.pump_efficiency
    )


def size_hydraulics(
    area_by_category_ha: Mapping[CropCategory, float] | pd.Series,
    config: HydraulicConfig | None = None,
    archetypes: Mapping[CropCategory, CropArchetype] = CROP_ARCHETYPES,
) -> HydraulicDemand:
    """Compute the water demand and required pump power of a cluster.

    Args:
        area_by_category_ha: Irrigated area per archetype, ha, e.g.
            ``ParcelCluster.area_by_category()``.
        config: Hydraulic parameters. Defaults to the brief's values.
        archetypes: Agronomic parameters per archetype (brief, Table 1).

    Returns:
        Water demand and required pump power.
    """
    cfg = config or HydraulicConfig()
    by_category = water_demand_by_category(area_by_category_ha, archetypes)
    daily_demand = sum(by_category.values())
    return HydraulicDemand(
        demand_by_category_m3_day=MappingProxyType(by_category),
        daily_demand_m3=daily_demand,
        flow_rate_m3_h=daily_demand / cfg.pumping_hours_per_day,
        required_power_kw=required_pump_power_kw(daily_demand, cfg),
    )


def _as_category(
    key: object, archetypes: Mapping[CropCategory, CropArchetype]
) -> CropCategory:
    """Return ``key`` as a crop archetype that has reference parameters.

    Raises:
        DataValidationError: If ``key`` is not a known archetype.
    """
    try:
        category = CropCategory(key)
    except ValueError:
        category = None
    if category is None or category not in archetypes:
        raise DataValidationError(f"No reference parameters for {key!r}.")
    return category
