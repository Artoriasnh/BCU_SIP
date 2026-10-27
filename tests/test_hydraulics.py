# Copyright (c) 2026 Hao Ni. All rights reserved.
"""Unit tests for water demand and pump power, checked against hand calculations."""

from __future__ import annotations

import math

import pandas as pd
import pytest

from dss.config import HydraulicConfig
from dss.exceptions import DataValidationError
from dss.hydraulics import (
    required_pump_power_kw,
    size_hydraulics,
    water_demand_by_category,
)
from dss.reference_tables import CROP_ARCHETYPES, CropCategory

BRIEF = HydraulicConfig()


class TestPumpPower:
    def test_hand_calculation(self):
        # 1000 * 9.81 * 100 * 40 / (3.6e6 * 6 * 0.55) = 39.24e6 / 11.88e6
        assert required_pump_power_kw(100.0) == pytest.approx(3.303030, rel=1e-6)

    def test_dimensional_consistency(self):
        q = 250.0
        power_kw = required_pump_power_kw(q)
        hydraulic_work_j = 1000 * 9.81 * q * 40
        electrical_energy_j = power_kw * 3.6e6 * 6 * 0.55
        assert electrical_energy_j == pytest.approx(hydraulic_work_j)

    def test_proportionality(self):
        base = required_pump_power_kw(200.0)
        assert required_pump_power_kw(400.0) == pytest.approx(2 * base)
        double_head = HydraulicConfig(total_dynamic_head_m=80.0)
        assert required_pump_power_kw(200.0, double_head) == pytest.approx(2 * base)
        double_hours = HydraulicConfig(pumping_hours_per_day=12.0)
        assert required_pump_power_kw(200.0, double_hours) == pytest.approx(base / 2)

    def test_zero_demand_needs_no_power(self):
        assert required_pump_power_kw(0.0) == 0.0

    @pytest.mark.parametrize("bad", [-1.0, math.nan, math.inf])
    def test_invalid_demand_rejected(self, bad):
        with pytest.raises(DataValidationError):
            required_pump_power_kw(bad)


class TestWaterDemand:
    def test_one_hectare_matches_reference_table(self):
        demand = water_demand_by_category({category: 1.0 for category in CropCategory})
        for category, archetype in CROP_ARCHETYPES.items():
            assert demand[category] == archetype.water_demand_m3_per_ha_day

    def test_accepts_series(self):
        areas = pd.Series({CropCategory.CEREALS: 2.0, CropCategory.VEGETABLES: 1.0})
        assert water_demand_by_category(areas) == {
            CropCategory.CEREALS: 70.0,
            CropCategory.VEGETABLES: 55.0,
        }

    @pytest.mark.parametrize("bad", [-0.1, math.nan, math.inf])
    def test_invalid_area_rejected(self, bad):
        with pytest.raises(DataValidationError, match="invalid"):
            water_demand_by_category({CropCategory.CEREALS: bad})

    def test_unknown_category_rejected(self):
        with pytest.raises(DataValidationError, match="No reference parameters"):
            water_demand_by_category({"99": 1.0})


class TestSizeHydraulics:
    def test_example_cluster(self):
        # Pasture 15.862 ha and olives 0.5677 ha (whole-parcel areas, Alentejo).
        result = size_hydraulics(
            {CropCategory.PASTURES: 15.862, CropCategory.ORCHARDS: 0.5677}
        )
        assert result.daily_demand_m3 == pytest.approx(15.862 * 15 + 0.5677 * 45)
        assert result.flow_rate_m3_h == pytest.approx(result.daily_demand_m3 / 6)
        assert result.required_power_kw == pytest.approx(8.7027, abs=1e-4)

    def test_largest_possible_clipped_cluster(self):
        # A buffer fully planted with vegetables is the worst case: pi * 150 m^2.
        disc_ha = math.pi * 150**2 / 10_000
        result = size_hydraulics({CropCategory.VEGETABLES: disc_ha})
        assert result.daily_demand_m3 == pytest.approx(388.77, abs=0.01)
        assert result.required_power_kw == pytest.approx(12.84, abs=0.01)

    def test_empty_cluster_has_zero_demand(self):
        result = size_hydraulics({})
        assert result.daily_demand_m3 == 0.0
        assert result.required_power_kw == 0.0


@pytest.mark.parametrize(
    "kwargs",
    [
        {"pump_efficiency": 0.0},
        {"pump_efficiency": 1.2},
        {"pumping_hours_per_day": 0.0},
        {"pumping_hours_per_day": 25.0},
        {"total_dynamic_head_m": 0.0},
        {"water_density_kg_m3": -1.0},
    ],
)
def test_invalid_config(kwargs):
    with pytest.raises(ValueError):
        HydraulicConfig(**kwargs)


def test_brief_defaults():
    assert BRIEF.total_dynamic_head_m == 40.0
    assert BRIEF.pump_efficiency == 0.55
    assert BRIEF.pumping_hours_per_day == 6.0
