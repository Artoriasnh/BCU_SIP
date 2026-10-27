# Copyright (c) 2026 Hao Ni. All rights reserved.
"""Task 3: pump selection and daily energy balance.

Pump selection (ASSUMPTIONS.md, A8)
-------------------------------
The brief selects the smallest catalogue tier whose rated motor power meets the
required power, and flags multiple pumps when the daily demand exceeds Tier 4
capacity. Both rules are applied: several pumps are needed when the required
power exceeds the largest motor *or* the demand exceeds the largest catalogue
delivery, with ``n = max(ceil(P / P_max), ceil(Q / Q_max))``.

* ``IDENTICAL`` (headline): ``n`` identical units of the smallest tier covering
  ``P / n``. Pumps operating in parallel should be identical.
* ``LEAST_COST`` (sensitivity): the cheapest combination of tiers, mixed or not.
  Below the multiple-pump threshold both strategies select the same single pump.

Energy balance (ASSUMPTIONS.md, A7, A13)
------------------------------------
    E_PV     = PV_kWp * PSH * derate                 [kWh/day]
    E_pump   = P * t_pump                            [kWh/day]
    E_excess = max(0, E_PV - E_pump)                 [kWh/day]

``RATED`` (headline, as in the brief) uses the rated motor power for ``P``.
``DEMAND`` (sensitivity) uses the power actually required to lift the daily
demand, i.e. the pump consumes only the energy the water demand calls for.
Power (kW, kWp) and energy (kWh/day) are kept distinct in all names.
"""

from __future__ import annotations

import itertools
import logging
import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import Enum
from types import MappingProxyType

from dss.config import EnergyConfig, HydraulicConfig
from dss.exceptions import DataValidationError
from dss.reference_tables import PUMP_CATALOGUE, PumpTier
from dss.solar import MONTHS, SolarClimatology

logger = logging.getLogger(__name__)


class PumpStrategy(str, Enum):
    """How several pump units are combined."""

    IDENTICAL = "identical"
    LEAST_COST = "least_cost"


class EnergyBasis(str, Enum):
    """Which power drives the daily pump consumption."""

    RATED = "rated"  # brief: rated motor power x pumping hours
    DEMAND = "demand"  # sensitivity: required power x pumping hours


@dataclass(frozen=True, slots=True)
class PumpSelection:
    """Pump units chosen for a cluster.

    Attributes:
        units: Selected catalogue tiers (one entry per installed pump).
        required_power_kw: Power required by the cluster's demand, kW.
        daily_demand_m3: Peak daily water demand, m3/day.
        strategy: Combination strategy used.
    """

    units: tuple[PumpTier, ...]
    required_power_kw: float
    daily_demand_m3: float
    strategy: PumpStrategy

    @property
    def n_units(self) -> int:
        """Number of pumps."""
        return len(self.units)

    @property
    def multiple_pumps_required(self) -> bool:
        """True if one pump of the largest tier is not enough."""
        return self.n_units > 1

    @property
    def motor_kw(self) -> float:
        """Total rated motor power, kW."""
        return sum(unit.motor_kw for unit in self.units)

    @property
    def pv_kwp(self) -> float:
        """Total dedicated PV capacity, kWp."""
        return sum(unit.pv_kwp for unit in self.units)

    @property
    def capex_usd(self) -> float:
        """Total installed capital cost, USD."""
        return sum(unit.capex_usd for unit in self.units)

    @property
    def catalogue_delivery_m3_day(self) -> float:
        """Total catalogue delivery at 40 m head, m3/day (indicative only)."""
        return sum(unit.max_delivery_m3_day for unit in self.units)

    @property
    def exceeds_catalogue_delivery(self) -> bool:
        """True if the demand exceeds the catalogue delivery (warning only)."""
        return self.daily_demand_m3 > self.catalogue_delivery_m3_day

    @property
    def label(self) -> str:
        """Readable description, e.g. ``2 x Tier 3`` or ``Tier 3 + Tier 2``."""
        names = [unit.name for unit in self.units]
        if len(set(names)) == 1:
            return f"{len(names)} x {names[0]}"
        return " + ".join(names)


@dataclass(frozen=True, slots=True)
class EnergyBalance:
    """Daily energy balance for one irradiance level.

    Attributes:
        basis: Power used for the pump consumption.
        psh: Peak sun hours, kWh/m2/day.
        pv_generation_kwh_day: Daily PV generation E_PV, kWh/day.
        pump_consumption_kwh_day: Daily pump consumption E_pump, kWh/day.
        surplus_kwh_day: Daily surplus E_excess, kWh/day.
    """

    basis: EnergyBasis
    psh: float
    pv_generation_kwh_day: float
    pump_consumption_kwh_day: float
    surplus_kwh_day: float


def select_pumps(
    required_power_kw: float,
    daily_demand_m3: float,
    strategy: PumpStrategy = PumpStrategy.IDENTICAL,
    catalogue: Sequence[PumpTier] = PUMP_CATALOGUE,
) -> PumpSelection:
    """Choose the pump units for a cluster.

    Args:
        required_power_kw: Power required to lift the daily demand, kW
            (``HydraulicDemand.required_power_kw``).
        daily_demand_m3: Peak daily water demand, m3/day.
        strategy: How several units are combined.
        catalogue: Available pump tiers.

    Returns:
        The selected pump units.

    Raises:
        DataValidationError: If an input is negative or not finite, or the
            catalogue is empty.
    """
    inputs = {
        "required_power_kw": required_power_kw,
        "daily_demand_m3": daily_demand_m3,
    }
    for name, value in inputs.items():
        if not math.isfinite(value) or value < 0:
            raise DataValidationError(f"{name} must be finite and >= 0, got {value}.")
    if not catalogue:
        raise DataValidationError("The pump catalogue is empty.")
    tiers = sorted(catalogue, key=lambda tier: tier.motor_kw)
    largest = tiers[-1]

    n_units = max(
        1,
        math.ceil(required_power_kw / largest.motor_kw),
        math.ceil(daily_demand_m3 / largest.max_delivery_m3_day),
    )
    # The delivery rule applies only when it triggered the multiple-pump case.
    needs_delivery = daily_demand_m3 > largest.max_delivery_m3_day
    if strategy is PumpStrategy.IDENTICAL:
        power_per_unit = required_power_kw / n_units
        demand_per_unit = daily_demand_m3 / n_units
        tier = next(
            t
            for t in tiers
            if t.motor_kw >= power_per_unit
            and (not needs_delivery or t.max_delivery_m3_day >= demand_per_unit)
        )
        units: tuple[PumpTier, ...] = (tier,) * n_units
    else:
        units = _least_cost_units(tiers, required_power_kw, daily_demand_m3, n_units)

    selection = PumpSelection(units, required_power_kw, daily_demand_m3, strategy)
    if selection.exceeds_catalogue_delivery:
        logger.warning(
            "Demand %.1f m3/day exceeds the catalogue delivery of %s (%.0f m3/day).",
            daily_demand_m3,
            selection.label,
            selection.catalogue_delivery_m3_day,
        )
    return selection


def energy_balance(
    selection: PumpSelection,
    psh: float,
    basis: EnergyBasis = EnergyBasis.RATED,
    hydraulic: HydraulicConfig | None = None,
    energy: EnergyConfig | None = None,
) -> EnergyBalance:
    """Daily PV generation, pump consumption and surplus.

    Args:
        selection: Installed pump units.
        psh: Peak sun hours, kWh/m2/day (e.g. ``SolarClimatology.annual_psh``).
        basis: Power used for the pump consumption.
        hydraulic: Hydraulic parameters (pumping hours). Defaults to the brief.
        energy: Energy parameters (PV derate). Defaults to the brief.

    Returns:
        The daily energy balance.

    Raises:
        DataValidationError: If ``psh`` is negative or not finite.
    """
    if not math.isfinite(psh) or psh < 0:
        raise DataValidationError(f"psh must be finite and >= 0, got {psh}.")
    hours = (hydraulic or HydraulicConfig()).pumping_hours_per_day
    derate = (energy or EnergyConfig()).pv_derate

    pv_kwh = selection.pv_kwp * psh * derate
    power_kw = (
        selection.motor_kw
        if basis is EnergyBasis.RATED
        else selection.required_power_kw
    )
    pump_kwh = power_kw * hours
    surplus_kwh = max(0.0, pv_kwh - pump_kwh)

    if not 0.0 <= surplus_kwh <= pv_kwh:  # pragma: no cover - defensive invariant
        raise RuntimeError("Energy balance invariant violated.")
    return EnergyBalance(basis, psh, pv_kwh, pump_kwh, surplus_kwh)


def monthly_energy_balance(
    selection: PumpSelection,
    solar: SolarClimatology,
    basis: EnergyBasis = EnergyBasis.RATED,
    hydraulic: HydraulicConfig | None = None,
    energy: EnergyConfig | None = None,
) -> Mapping[str, EnergyBalance]:
    """Energy balance for each calendar month (seasonal analysis).

    Args:
        selection: Installed pump units.
        solar: Monthly climatology at the cluster centroid.
        basis: Power used for the pump consumption.
        hydraulic: Hydraulic parameters. Defaults to the brief.
        energy: Energy parameters. Defaults to the brief.

    Returns:
        Read-only mapping from month (``JAN`` ... ``DEC``) to energy balance.
    """
    return MappingProxyType(
        {
            month: energy_balance(
                selection, solar.monthly_psh[month], basis, hydraulic, energy
            )
            for month in MONTHS
        }
    )


def surplus_threshold_psh(
    selection: PumpSelection,
    basis: EnergyBasis = EnergyBasis.RATED,
    hydraulic: HydraulicConfig | None = None,
    energy: EnergyConfig | None = None,
) -> float:
    """Peak sun hours above which the system produces a surplus.

    ``E_PV > E_pump``  <=>  ``PSH > P * t_pump / (PV_kWp * derate)``.

    Args:
        selection: Installed pump units.
        basis: Power used for the pump consumption.
        hydraulic: Hydraulic parameters. Defaults to the brief.
        energy: Energy parameters. Defaults to the brief.

    Returns:
        Threshold PSH, kWh/m2/day.
    """
    hours = (hydraulic or HydraulicConfig()).pumping_hours_per_day
    derate = (energy or EnergyConfig()).pv_derate
    power_kw = (
        selection.motor_kw
        if basis is EnergyBasis.RATED
        else selection.required_power_kw
    )
    return power_kw * hours / (selection.pv_kwp * derate)


def _least_cost_units(
    tiers: Sequence[PumpTier],
    required_power_kw: float,
    daily_demand_m3: float,
    min_units: int,
) -> tuple[PumpTier, ...]:
    """Cheapest multiset of tiers meeting the power and delivery requirements.

    A single pump is kept whenever one suffices (the brief's rule); otherwise
    ``min_units`` to ``min_units + 2`` units are considered. The delivery
    constraint applies only when the demand exceeds the largest tier's
    catalogue delivery. Ties are broken by fewer units, then lower motor power.
    """
    needs_delivery = daily_demand_m3 > tiers[-1].max_delivery_m3_day
    counts = range(1, 2) if min_units == 1 else range(min_units, min_units + 3)
    best: tuple[PumpTier, ...] | None = None
    for count in counts:
        for combo in itertools.combinations_with_replacement(tiers, count):
            if sum(t.motor_kw for t in combo) < required_power_kw:
                continue
            if needs_delivery and sum(
                t.max_delivery_m3_day for t in combo
            ) < daily_demand_m3:
                continue
            if best is None or _cost_key(combo) < _cost_key(best):
                best = combo
    if best is None:  # pragma: no cover - min_units largest units suffice
        raise RuntimeError("No pump combination satisfies the requirements.")
    return tuple(sorted(best, key=lambda t: t.motor_kw, reverse=True))


def _cost_key(combo: Sequence[PumpTier]) -> tuple[float, int, float]:
    return (
        sum(t.capex_usd for t in combo),
        len(combo),
        sum(t.motor_kw for t in combo),
    )
