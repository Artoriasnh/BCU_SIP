# Copyright (c) 2026 Hao Ni. All rights reserved.
"""Reference parameters, transcribed verbatim from the assessment brief (Section 3).

Only these values are used for headline results.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Final


class CropCategory(str, Enum):
    """Macro crop archetypes of the brief's Reference Table 1.

    Values are the identifiers used in the brief; ``dss.taxonomy`` maps real
    HCAT codes onto them.
    """

    CEREALS = "10"
    ORCHARDS = "20"
    PASTURES = "30"
    VEGETABLES = "50"
    FALLBACK = "OTHER"


@dataclass(frozen=True, slots=True)
class CropArchetype:
    """Agronomic parameters of one crop archetype.

    Attributes:
        category: Archetype identifier.
        name: Macro category name as written in the brief.
        representative_crop: Representative crop(s) named in the brief.
        water_demand_m3_per_ha_day: Peak daily irrigation water demand,
            m3 ha^-1 day^-1 (1 m3/ha/day = 0.1 mm/day).
        fresh_yield_t_per_ha: Fresh harvested yield per season, t ha^-1.
        farmgate_price_usd_per_kg: Farmgate price, USD kg^-1.
    """

    category: CropCategory
    name: str
    representative_crop: str
    water_demand_m3_per_ha_day: float
    fresh_yield_t_per_ha: float
    farmgate_price_usd_per_kg: float


# Source: assessment brief, Section 3C "Reference Table 1: Agronomic Crop Demand".
CROP_ARCHETYPES: Final[dict[CropCategory, CropArchetype]] = {
    archetype.category: archetype
    for archetype in (
        CropArchetype(CropCategory.CEREALS, "Cereals & Grains",
                      "Maize / Wheat", 35.0, 7.0, 0.25),
        CropArchetype(CropCategory.ORCHARDS, "Orchards & Fruits",
                      "Citrus / Fruit Tree", 45.0, 12.0, 0.80),
        CropArchetype(CropCategory.PASTURES, "Pastures & Meadows",
                      "Fodder Grass", 15.0, 4.0, 0.10),
        CropArchetype(CropCategory.VEGETABLES, "Vegetables / High-Value",
                      "Tomato / Pepper", 55.0, 25.0, 0.60),
        CropArchetype(CropCategory.FALLBACK, "Fallback Field Crop",
                      "Mixed / Pulses", 30.0, 5.0, 0.30),
    )
}


@dataclass(frozen=True, slots=True)
class PumpTier:
    """One tier of the solar irrigation pump catalogue.

    Attributes:
        name: Tier label.
        motor_kw: Rated motor power, kW.
        pv_kwp: Dedicated PV capacity, kWp.
        capex_usd: Installed capital cost, USD.
        max_delivery_m3_day: Catalogue maximum delivery at 40 m head, m3/day.
            Not reproducible from the brief's own hydraulic formula (see the
            Task 2 evaluation); used for warnings only, never for selection.
    """

    name: str
    motor_kw: float
    pv_kwp: float
    capex_usd: float
    max_delivery_m3_day: float


# Source: assessment brief, Section 3D "Reference Table 2: IDCOL Solar Irrigation
# Pump Catalog". Ordered by motor power.
PUMP_CATALOGUE: Final[tuple[PumpTier, ...]] = (
    PumpTier("Tier 1", 3.7, 5.0, 7_500.0, 250.0),
    PumpTier("Tier 2", 5.5, 7.5, 10_500.0, 400.0),
    PumpTier("Tier 3", 7.5, 10.0, 14_000.0, 580.0),
    PumpTier("Tier 4", 11.0, 15.0, 19_500.0, 850.0),
)
