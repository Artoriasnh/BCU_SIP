# Copyright (c) 2026 Hao Ni. All rights reserved.
"""Unit tests for pump selection and the energy balance (hand-calculated values)."""

from __future__ import annotations

import math
from pathlib import Path

import pytest

from dss.config import EnergyConfig, HydraulicConfig
from dss.energy import (
    EnergyBasis,
    PumpStrategy,
    energy_balance,
    monthly_energy_balance,
    select_pumps,
    surplus_threshold_psh,
)
from dss.exceptions import DataValidationError
from dss.reference_tables import PUMP_CATALOGUE
from dss.solar import MONTHS, load_power_json

FIXTURE = Path(__file__).parent / "fixtures" / "nasa_power_alentejo.json"
IDENTICAL, LEAST_COST = PumpStrategy.IDENTICAL, PumpStrategy.LEAST_COST


@pytest.fixture
def alentejo():
    return load_power_json(FIXTURE)


class TestSelectPumps:
    @pytest.mark.parametrize(
        ("power_kw", "expected"),
        [(0.0, "Tier 1"), (1.28, "Tier 1"), (3.7, "Tier 1"), (3.71, "Tier 2"),
         (7.5, "Tier 3"), (11.0, "Tier 4")],
    )
    def test_smallest_tier_covering_the_power(self, power_kw, expected):
        selection = select_pumps(power_kw, 100.0)
        assert selection.label == f"1 x {expected}"
        assert not selection.multiple_pumps_required

    def test_demonstration_cluster(self):
        selection = select_pumps(1.283, 38.85)
        assert (selection.motor_kw, selection.pv_kwp, selection.capex_usd) == (
            3.7, 5.0, 7_500.0
        )

    def test_identical_units_above_largest_motor(self):
        selection = select_pumps(13.0, 393.6, IDENTICAL)
        assert selection.label == "2 x Tier 3"  # 6.5 kW per unit
        assert selection.capex_usd == 28_000.0
        assert selection.multiple_pumps_required

    def test_least_cost_mix_above_largest_motor(self):
        selection = select_pumps(13.0, 393.6, LEAST_COST)
        assert selection.label == "Tier 3 + Tier 2"
        assert selection.capex_usd == 24_500.0
        assert selection.motor_kw >= 13.0

    def test_least_cost_never_dearer_than_identical(self):
        for power in (0.5, 4.0, 9.0, 11.5, 17.0, 25.0, 40.0):
            identical = select_pumps(power, 100.0, IDENTICAL)
            cheapest = select_pumps(power, 100.0, LEAST_COST)
            assert cheapest.capex_usd <= identical.capex_usd

    @pytest.mark.parametrize("power_kw", [0.5, 4.0, 8.0, 10.9, 11.0])
    def test_strategies_agree_when_one_pump_suffices(self, power_kw):
        # Two small pumps (Tier 2 + Tier 1, $18,000) would undercut one Tier 4,
        # but the brief prescribes a single pump up to 11 kW.
        identical = select_pumps(power_kw, 100.0, IDENTICAL)
        cheapest = select_pumps(power_kw, 100.0, LEAST_COST)
        assert cheapest.units == identical.units
        assert cheapest.n_units == 1

    def test_demand_above_largest_delivery_triggers_multiple_pumps(self):
        # The brief's second rule: Q above Tier 4 capacity (850 m3/day).
        selection = select_pumps(5.0, 900.0, IDENTICAL)
        assert selection.n_units == 2
        assert selection.label == "2 x Tier 3"  # 450 m3/day per unit
        assert not selection.exceeds_catalogue_delivery

    def test_unit_count_formula(self):
        # n = max(ceil(P / 11), ceil(Q / 850))
        assert select_pumps(30.0, 100.0).n_units == 3
        assert select_pumps(5.0, 2000.0).n_units == 3

    def test_catalogue_delivery_is_only_a_warning(self, caplog):
        selection = select_pumps(3.0, 300.0)  # Tier 1 lists 250 m3/day
        assert selection.label == "1 x Tier 1"
        assert selection.exceeds_catalogue_delivery
        assert "exceeds the catalogue delivery" in caplog.text

    @pytest.mark.parametrize(
        ("power", "demand"), [(-1.0, 10.0), (1.0, -10.0), (math.nan, 1.0),
                              (1.0, math.inf)],
    )
    def test_invalid_inputs(self, power, demand):
        with pytest.raises(DataValidationError):
            select_pumps(power, demand)

    def test_empty_catalogue(self):
        with pytest.raises(DataValidationError, match="empty"):
            select_pumps(1.0, 10.0, catalogue=())

    def test_unsorted_catalogue_is_handled(self):
        selection = select_pumps(5.0, 100.0, catalogue=tuple(reversed(PUMP_CATALOGUE)))
        assert selection.label == "1 x Tier 2"


class TestEnergyBalance:
    def test_brief_formula_for_tier_1(self, alentejo):
        balance = energy_balance(select_pumps(1.283, 38.85), alentejo.annual_psh)
        assert balance.pv_generation_kwh_day == pytest.approx(5.0 * 4.9028 * 0.75,
                                                              rel=1e-4)
        assert balance.pump_consumption_kwh_day == pytest.approx(3.7 * 6)
        assert balance.surplus_kwh_day == 0.0  # 18.39 < 22.2

    def test_demand_basis_uses_required_power(self, alentejo):
        selection = select_pumps(1.283, 38.85)
        balance = energy_balance(selection, alentejo.annual_psh, EnergyBasis.DEMAND)
        assert balance.pump_consumption_kwh_day == pytest.approx(1.283 * 6)
        assert balance.surplus_kwh_day == pytest.approx(18.3854 - 7.698, rel=1e-4)

    def test_surplus_is_bounded(self, alentejo):
        selection = select_pumps(1.283, 38.85)
        for month, balance in monthly_energy_balance(selection, alentejo).items():
            assert 0.0 <= balance.surplus_kwh_day <= balance.pv_generation_kwh_day
            assert balance.psh == alentejo.monthly_psh[month]

    def test_zero_irradiance_gives_no_surplus(self):
        balance = energy_balance(select_pumps(1.0, 10.0), 0.0)
        assert balance.pv_generation_kwh_day == 0.0
        assert balance.surplus_kwh_day == 0.0

    def test_parameters_are_configurable(self):
        selection = select_pumps(1.0, 10.0)
        balance = energy_balance(
            selection, 5.0,
            hydraulic=HydraulicConfig(pumping_hours_per_day=3.0),
            energy=EnergyConfig(pv_derate=1.0),
        )
        assert balance.pv_generation_kwh_day == pytest.approx(25.0)
        assert balance.pump_consumption_kwh_day == pytest.approx(11.1)

    @pytest.mark.parametrize("psh", [-0.1, math.nan, math.inf])
    def test_invalid_psh(self, psh):
        with pytest.raises(DataValidationError):
            energy_balance(select_pumps(1.0, 10.0), psh)

    @pytest.mark.parametrize("derate", [0.0, 1.1])
    def test_invalid_derate(self, derate):
        with pytest.raises(ValueError):
            EnergyConfig(pv_derate=derate)


class TestSeasonalAnalysis:
    def test_surplus_months_for_alentejo(self, alentejo):
        monthly = monthly_energy_balance(select_pumps(1.283, 38.85), alentejo)
        surplus_months = [m for m in MONTHS if monthly[m].surplus_kwh_day > 0]
        assert surplus_months == ["MAY", "JUN", "JUL", "AUG"]
        assert monthly["JUL"].surplus_kwh_day == pytest.approx(
            5.0 * 7.9 * 0.75 - 22.2, rel=1e-3
        )

    @pytest.mark.parametrize("tier", PUMP_CATALOGUE, ids=lambda t: t.name)
    def test_threshold_is_about_5_9_psh_for_every_tier(self, tier):
        selection = select_pumps(tier.motor_kw, 0.0)
        threshold = surplus_threshold_psh(selection)
        assert threshold == pytest.approx(8 * tier.motor_kw / tier.pv_kwp)
        assert 5.8 < threshold < 6.1

    def test_threshold_separates_surplus_from_deficit(self):
        selection = select_pumps(1.0, 10.0)
        threshold = surplus_threshold_psh(selection)
        assert energy_balance(selection, threshold * 0.99).surplus_kwh_day == 0.0
        assert energy_balance(selection, threshold * 1.01).surplus_kwh_day > 0.0

    def test_demand_threshold_is_lower(self):
        selection = select_pumps(1.283, 38.85)
        assert surplus_threshold_psh(selection, EnergyBasis.DEMAND) < (
            surplus_threshold_psh(selection, EnergyBasis.RATED)
        )
