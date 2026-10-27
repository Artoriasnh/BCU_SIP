# Copyright (c) 2026 Hao Ni. All rights reserved.
"""Task 4: Productive Use of Energy (PUE) selection and simple payback.

An asset is a candidate when both requirements of the brief hold:

    energy feasibility:  E_excess >= daily load                 [kWh/day]
    crop relevance:      the cluster grows a compatible archetype (area > 0)

Among candidates the highest annual net uplift is selected, and

    payback = CAPEX / annual net uplift                         [years]

with ``net uplift = uplift - O&M`` (O&M is 0 in headline results). Harvest is
``area * yield * 1000 kg/t * harvests per year`` [kg/year]. When no asset
qualifies, the decision carries a status explaining why, and no payback is
computed.

The engine accepts any surplus: the brief's annual value (headline), the
demand-based value (sensitivity) or monthly values (seasonal screening, which
reports feasibility only and is not annualised).
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import Enum
from types import MappingProxyType
from typing import Any

from dss.config import EconomicConfig
from dss.exceptions import DataValidationError
from dss.reference_tables import (
    CROP_ARCHETYPES,
    PUE_CATALOGUE,
    CropArchetype,
    CropCategory,
    PueAsset,
)
from dss.validation import AreaTable, validate_areas

KG_PER_TONNE = 1_000.0
_REL_TOL = 1e-9  # tolerance when comparing surplus and load (floating point)


class InvestmentStatus(str, Enum):
    """Outcome of the PUE appraisal."""

    INVEST = "invest"
    NO_COMPATIBLE_CROPS = "no_compatible_crops"
    INSUFFICIENT_SURPLUS = "insufficient_surplus"
    NOT_PROFITABLE = "not_profitable"


@dataclass(frozen=True, slots=True)
class PueAppraisal:
    """Appraisal of one asset for one cluster.

    Attributes:
        asset: The PUE asset.
        crop_compatible: True if the cluster grows a compatible archetype.
        energy_feasible: True if the surplus covers the daily load.
        annual_uplift_usd: Annual gross value uplift, USD/year.
        annual_om_usd: Annual operation and maintenance cost, USD/year.
    """

    asset: PueAsset
    crop_compatible: bool
    energy_feasible: bool
    annual_uplift_usd: float
    annual_om_usd: float

    @property
    def is_candidate(self) -> bool:
        """True if both requirements of the brief are met."""
        return self.crop_compatible and self.energy_feasible

    @property
    def annual_net_uplift_usd(self) -> float:
        """Annual net uplift (uplift minus O&M), USD/year."""
        return self.annual_uplift_usd - self.annual_om_usd

    @property
    def payback_years(self) -> float | None:
        """Simple payback, years; ``None`` if the net uplift is not positive."""
        net = self.annual_net_uplift_usd
        return self.asset.capex_usd / net if net > 0 else None


@dataclass(frozen=True, slots=True)
class PueDecision:
    """Investment decision for one cluster.

    Attributes:
        status: Outcome of the appraisal.
        surplus_kwh_day: Daily surplus energy used, kWh/day.
        selected: The selected asset, or ``None`` if there is no investment.
        appraisals: Appraisal of every asset in the catalogue.
    """

    status: InvestmentStatus
    surplus_kwh_day: float
    selected: PueAppraisal | None
    appraisals: tuple[PueAppraisal, ...]

    @property
    def invest(self) -> bool:
        """True if an asset is selected."""
        return self.selected is not None

    @property
    def payback_years(self) -> float | None:
        """Payback of the selected asset, years, or ``None``."""
        return self.selected.payback_years if self.selected else None

    @property
    def reason(self) -> str:
        """One-sentence explanation of the decision."""
        if self.selected is not None:
            return (
                f"{self.selected.asset.name} selected: net uplift "
                f"{self.selected.annual_net_uplift_usd:,.0f} USD/year, payback "
                f"{self.payback_years:.1f} years."
            )
        if self.status is InvestmentStatus.NO_COMPATIBLE_CROPS:
            return "The cluster grows no crop compatible with any PUE asset."
        if self.status is InvestmentStatus.INSUFFICIENT_SURPLUS:
            lowest = min(
                a.asset.daily_load_kwh for a in self.appraisals if a.crop_compatible
            )
            return (
                f"Surplus of {self.surplus_kwh_day:.2f} kWh/day is below the daily "
                f"load of every compatible asset (lowest {lowest:g} kWh/day)."
            )
        return "No feasible asset has a positive annual net uplift."

    def to_dict(self) -> dict[str, Any]:
        """Structured summary, e.g. for tables or JSON."""
        selected = self.selected
        return {
            "status": self.status.value,
            "invest": self.invest,
            "asset": selected.asset.name if selected else None,
            "surplus_kwh_day": self.surplus_kwh_day,
            "capex_usd": selected.asset.capex_usd if selected else 0.0,
            "annual_net_uplift_usd": (
                selected.annual_net_uplift_usd if selected else 0.0
            ),
            "payback_years": self.payback_years,
            "candidates": [a.asset.name for a in self.appraisals if a.is_candidate],
            "reason": self.reason,
        }


def annual_harvest_kg(
    area_by_category_ha: AreaTable,
    archetypes: Mapping[CropCategory, CropArchetype] = CROP_ARCHETYPES,
    config: EconomicConfig | None = None,
) -> dict[CropCategory, float]:
    """Fresh harvest per archetype.

    Args:
        area_by_category_ha: Area per archetype, ha.
        archetypes: Agronomic parameters (brief, Table 1).
        config: Financial conventions (harvests per year).

    Returns:
        Harvest per archetype, kg/year.
    """
    harvests = (config or EconomicConfig()).harvests_per_year
    return {
        category: area
        * archetypes[category].fresh_yield_t_per_ha
        * KG_PER_TONNE
        * harvests
        for category, area in validate_areas(area_by_category_ha, archetypes).items()
    }


def annual_uplift_usd(
    asset: PueAsset,
    harvest_kg: Mapping[CropCategory, float],
    archetypes: Mapping[CropCategory, CropArchetype] = CROP_ARCHETYPES,
) -> float:
    """Annual gross value uplift of an asset (brief, Table 3 and Task 4 tips).

    Args:
        asset: The PUE asset.
        harvest_kg: Harvest per archetype, kg/year.
        archetypes: Agronomic parameters (farmgate prices).

    Returns:
        Uplift, USD/year.
    """
    total = 0.0
    for category, kg in harvest_kg.items():
        if category not in asset.compatible:
            continue
        value = (
            archetypes[category].farmgate_price_usd_per_kg
            if asset.value_usd_per_kg is None
            else asset.value_usd_per_kg
        )
        total += kg * asset.harvest_share * value
    return total


def evaluate_pue(
    surplus_kwh_day: float,
    area_by_category_ha: AreaTable,
    catalogue: Sequence[PueAsset] = PUE_CATALOGUE,
    archetypes: Mapping[CropCategory, CropArchetype] = CROP_ARCHETYPES,
    config: EconomicConfig | None = None,
) -> PueDecision:
    """Select the PUE asset with the highest annual net uplift.

    Args:
        surplus_kwh_day: Daily surplus energy E_excess, kWh/day.
        area_by_category_ha: Area per archetype, ha, e.g.
            ``ParcelCluster.area_by_category()``.
        catalogue: Available PUE assets (brief, Table 3).
        archetypes: Agronomic parameters (brief, Table 1).
        config: Financial conventions. Defaults to the headline values.

    Returns:
        The decision, with a status if there is no investment.

    Raises:
        DataValidationError: If the surplus is negative or not finite, an area
            is invalid, or the catalogue is empty.
    """
    if not math.isfinite(surplus_kwh_day) or surplus_kwh_day < 0:
        raise DataValidationError(
            f"surplus_kwh_day must be finite and >= 0, got {surplus_kwh_day}."
        )
    if not catalogue:
        raise DataValidationError("The PUE catalogue is empty.")
    cfg = config or EconomicConfig()
    areas = validate_areas(area_by_category_ha, archetypes)
    harvest = annual_harvest_kg(areas, archetypes, cfg)
    grown = {category for category, area in areas.items() if area > 0}

    appraisals = tuple(
        PueAppraisal(
            asset=asset,
            crop_compatible=bool(asset.compatible & grown),
            energy_feasible=_covers(surplus_kwh_day, asset.daily_load_kwh),
            annual_uplift_usd=annual_uplift_usd(asset, harvest, archetypes),
            annual_om_usd=asset.capex_usd * cfg.annual_om_fraction,
        )
        for asset in catalogue
    )
    candidates = [a for a in appraisals if a.is_candidate]
    profitable = [a for a in candidates if a.annual_net_uplift_usd > 0]

    if not any(a.crop_compatible for a in appraisals):
        status = InvestmentStatus.NO_COMPATIBLE_CROPS
    elif not candidates:
        status = InvestmentStatus.INSUFFICIENT_SURPLUS
    elif not profitable:
        status = InvestmentStatus.NOT_PROFITABLE
    else:
        status = InvestmentStatus.INVEST
    # Ties: lower CAPEX first, then catalogue order (max keeps the first).
    selected = (
        max(profitable, key=lambda a: (a.annual_net_uplift_usd, -a.asset.capex_usd))
        if profitable
        else None
    )
    return PueDecision(status, surplus_kwh_day, selected, appraisals)


def feasible_assets_by_month(
    surplus_by_month: Mapping[str, float],
    area_by_category_ha: AreaTable,
    catalogue: Sequence[PueAsset] = PUE_CATALOGUE,
    archetypes: Mapping[CropCategory, CropArchetype] = CROP_ARCHETYPES,
) -> Mapping[str, tuple[str, ...]]:
    """Seasonal screening: assets meeting both requirements in each month.

    Monthly results are not annualised into revenue or payback.

    Args:
        surplus_by_month: Daily surplus per month, kWh/day.
        area_by_category_ha: Area per archetype, ha.
        catalogue: Available PUE assets.
        archetypes: Agronomic parameters.

    Returns:
        Read-only mapping from month to the names of candidate assets.
    """
    return MappingProxyType(
        {
            month: tuple(
                a.asset.name
                for a in evaluate_pue(
                    surplus, area_by_category_ha, catalogue, archetypes
                ).appraisals
                if a.is_candidate
            )
            for month, surplus in surplus_by_month.items()
        }
    )


def _covers(surplus_kwh_day: float, load_kwh_day: float) -> bool:
    """``surplus >= load``, tolerant of floating-point round-off."""
    return surplus_kwh_day >= load_kwh_day or math.isclose(
        surplus_kwh_day, load_kwh_day, rel_tol=_REL_TOL
    )
