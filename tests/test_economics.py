# Copyright (c) 2026 Hao Ni. All rights reserved.
"""Unit tests for PUE selection and payback (hand-calculated values)."""

from __future__ import annotations

import math

import pytest

from dss.config import EconomicConfig
from dss.economics import (
    InvestmentStatus,
    annual_harvest_kg,
    annual_uplift_usd,
    evaluate_pue,
    feasible_assets_by_month,
)
from dss.exceptions import DataValidationError
from dss.reference_tables import PUE_CATALOGUE, CropCategory

C = CropCategory
COLD, MILL, DRYER = PUE_CATALOGUE
DEMO = {C.CEREALS: 0.754, C.FALLBACK: 0.414}  # demonstration cluster, ha
MIXED = {C.VEGETABLES: 2.0, C.ORCHARDS: 1.0, C.CEREALS: 1.5}


class TestUplift:
    def test_harvest_converts_tonnes_to_kg(self):
        harvest = annual_harvest_kg({C.VEGETABLES: 2.0, C.PASTURES: 1.0})
        assert harvest == {C.VEGETABLES: 50_000.0, C.PASTURES: 4_000.0}

    def test_harvests_per_year(self):
        harvest = annual_harvest_kg({C.CEREALS: 1.0},
                                    config=EconomicConfig(harvests_per_year=2.0))
        assert harvest[C.CEREALS] == 14_000.0

    def test_cold_room_avoids_20_percent_spoilage_at_farmgate_price(self):
        # 2 ha x 25 t x 0.60 $/kg x 20 % + 1 ha x 12 t x 0.80 $/kg x 20 %
        assert annual_uplift_usd(COLD, annual_harvest_kg(MIXED)) == pytest.approx(
            6_000.0 + 1_920.0
        )

    def test_mill_charges_fee_on_whole_grain_harvest(self):
        assert annual_uplift_usd(MILL, annual_harvest_kg(MIXED)) == pytest.approx(
            10_500 * 0.05
        )

    def test_dryer_premium_on_15_percent_of_compatible_harvest(self):
        compatible_kg = 50_000 + 12_000 + 10_500
        assert annual_uplift_usd(DRYER, annual_harvest_kg(MIXED)) == pytest.approx(
            compatible_kg * 0.15 * 0.12
        )

    @pytest.mark.parametrize("asset", PUE_CATALOGUE, ids=lambda a: a.name)
    def test_pastures_and_fallback_earn_nothing(self, asset):
        harvest = annual_harvest_kg({C.PASTURES: 5.0, C.FALLBACK: 2.0})
        assert annual_uplift_usd(asset, harvest) == 0.0


class TestSelection:
    def test_demonstration_cluster_headline_is_no_investment(self):
        decision = evaluate_pue(0.0, DEMO)
        assert decision.status is InvestmentStatus.INSUFFICIENT_SURPLUS
        assert not decision.invest
        assert decision.payback_years is None
        assert "lowest 10 kWh/day" in decision.reason

    def test_demonstration_cluster_demand_based_surplus(self):
        decision = evaluate_pue(10.687, DEMO)
        assert decision.selected is not None
        assert decision.selected.asset is DRYER  # milling needs 25 kWh/day
        uplift = 0.754 * 7_000 * 0.15 * 0.12
        assert decision.selected.annual_net_uplift_usd == pytest.approx(uplift)
        assert decision.payback_years == pytest.approx(2_500 / uplift)

    def test_highest_net_uplift_is_selected(self):
        decision = evaluate_pue(30.0, MIXED)
        assert decision.selected is not None
        assert decision.selected.asset is COLD
        assert decision.payback_years == pytest.approx(6_000 / 7_920)
        assert {a.asset.name for a in decision.appraisals if a.is_candidate} == {
            COLD.name, MILL.name, DRYER.name
        }

    def test_more_surplus_unlocks_larger_loads(self):
        names = [
            getattr(evaluate_pue(s, MIXED).selected, "asset", None)
            for s in (9.9, 10.0, 20.0, 25.0)
        ]
        assert names == [None, DRYER, COLD, COLD]

    def test_load_boundary_is_inclusive(self):
        assert evaluate_pue(10.0, DEMO).selected is not None
        assert evaluate_pue(10.0 - 1e-12, DEMO).selected is not None  # round-off
        assert evaluate_pue(9.99, DEMO).selected is None

    def test_tie_prefers_lower_capex(self):
        # Equal uplift for milling (0.05 $/kg x 100 %) and drying (0.12 x 15 %)
        # is impossible with the brief's values, so build a tied catalogue.
        twin = type(MILL)("Cheap mill", MILL.compatible, MILL.daily_load_kwh,
                          1_000.0, MILL.harvest_share, MILL.value_usd_per_kg)
        decision = evaluate_pue(30.0, DEMO, catalogue=(MILL, twin))
        assert decision.selected is not None
        assert decision.selected.asset.name == "Cheap mill"


class TestNoInvestment:
    def test_no_compatible_crops(self):
        decision = evaluate_pue(100.0, {C.PASTURES: 5.0, C.FALLBACK: 1.0})
        assert decision.status is InvestmentStatus.NO_COMPATIBLE_CROPS
        assert decision.payback_years is None

    def test_compatible_crop_with_zero_area_does_not_count(self):
        decision = evaluate_pue(100.0, {C.VEGETABLES: 0.0, C.PASTURES: 1.0})
        assert decision.status is InvestmentStatus.NO_COMPATIBLE_CROPS

    def test_empty_cluster(self):
        assert evaluate_pue(50.0, {}).status is InvestmentStatus.NO_COMPATIBLE_CROPS

    def test_not_profitable_when_om_exceeds_uplift(self):
        tiny = {C.CEREALS: 0.01}  # dryer uplift 1.26 $/year < 4.5 % of 2,500
        decision = evaluate_pue(15.0, tiny,
                                config=EconomicConfig(annual_om_fraction=0.045))
        assert decision.status is InvestmentStatus.NOT_PROFITABLE
        assert decision.payback_years is None
        assert "positive annual net uplift" in decision.reason
        assert decision.appraisals[2].payback_years is None

    def test_structured_dictionary(self):
        result = evaluate_pue(0.0, DEMO).to_dict()
        assert result == {
            "status": "insufficient_surplus",
            "invest": False,
            "asset": None,
            "surplus_kwh_day": 0.0,
            "capex_usd": 0.0,
            "annual_net_uplift_usd": 0.0,
            "payback_years": None,
            "candidates": [],
            "reason": result["reason"],
        }

    def test_dictionary_when_investing(self):
        result = evaluate_pue(30.0, MIXED).to_dict()
        assert result["asset"] == COLD.name
        assert result["capex_usd"] == 6_000.0
        assert math.isclose(result["payback_years"], 6_000 / 7_920)


class TestOperationAndMaintenance:
    def test_om_reduces_net_uplift_and_lengthens_payback(self):
        base = evaluate_pue(30.0, MIXED).selected
        with_om = evaluate_pue(
            30.0, MIXED, config=EconomicConfig(annual_om_fraction=0.045)
        ).selected
        assert base is not None and with_om is not None
        assert with_om.annual_om_usd == pytest.approx(270.0)
        assert with_om.payback_years == pytest.approx(6_000 / (7_920 - 270))
        assert with_om.payback_years > base.payback_years

    @pytest.mark.parametrize(
        "kwargs", [{"harvests_per_year": 0.0}, {"harvests_per_year": math.nan},
                   {"annual_om_fraction": -0.1}, {"annual_om_fraction": 1.0}],
    )
    def test_invalid_config(self, kwargs):
        with pytest.raises(ValueError):
            EconomicConfig(**kwargs)


class TestSeasonalScreening:
    def test_monthly_candidates(self):
        months = feasible_assets_by_month(
            {"JAN": 5.0, "JUL": 21.9, "AUG": 26.0}, DEMO
        )
        assert months == {"JAN": (), "JUL": (DRYER.name,),
                          "AUG": (MILL.name, DRYER.name)}


class TestValidation:
    @pytest.mark.parametrize("surplus", [-1.0, math.nan, math.inf])
    def test_invalid_surplus(self, surplus):
        with pytest.raises(DataValidationError):
            evaluate_pue(surplus, DEMO)

    def test_invalid_area(self):
        with pytest.raises(DataValidationError, match="invalid"):
            evaluate_pue(10.0, {C.CEREALS: -1.0})

    def test_unknown_category(self):
        with pytest.raises(DataValidationError, match="No reference parameters"):
            evaluate_pue(10.0, {"99": 1.0})

    def test_string_keys_are_accepted(self):
        assert evaluate_pue(30.0, {"10": 0.754}).selected is not None

    def test_empty_catalogue(self):
        with pytest.raises(DataValidationError, match="empty"):
            evaluate_pue(10.0, DEMO, catalogue=())
