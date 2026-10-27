# Copyright (c) 2026 Hao Ni. All rights reserved.
"""Tests for the end-to-end pipeline (dss_pipeline.py).

Unit tests use a fake parcel layer and solar client; the integration test runs
the real EuroCrops layer with the stored NASA POWER response (no network).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, replace
from pathlib import Path
from types import MappingProxyType

import pandas as pd
import pytest

import dss_pipeline
from dss.config import DEFAULT_PARCELS_PATH, EconomicConfig, HydraulicConfig
from dss.economics import InvestmentStatus
from dss.energy import EnergyBasis, PumpStrategy
from dss.exceptions import DataValidationError, EmptyClusterError, SolarDataError
from dss.reference_tables import CropCategory
from dss.solar import load_power_json
from dss_pipeline import DssPipeline, Scenario, Site, appraise

FIXTURE = Path(__file__).parent / "fixtures" / "nasa_power_alentejo.json"
DEMO_SITE = (38.07759, -7.96330)
DEMO_AREAS = {CropCategory.CEREALS: 0.7545, CropCategory.FALLBACK: 0.4146}
C = CropCategory


@pytest.fixture(scope="module")
def solar():
    return load_power_json(FIXTURE)


@pytest.fixture(scope="module")
def demo(solar):
    return Site(*DEMO_SITE, DEMO_AREAS, solar)


@dataclass
class FakeCluster:
    centroid_lat: float
    centroid_lon: float
    n_parcels: int = 3

    def area_by_category(self) -> pd.Series:
        return pd.Series(DEMO_AREAS)


class FakeLayer:
    """Returns the demonstration cluster, or raises like the real layer."""

    def extract_cluster(self, lat, lon):
        if abs(lat) > 90:
            raise DataValidationError("latitude out of range")
        if (lat, lon) != DEMO_SITE:
            raise EmptyClusterError()
        return FakeCluster(lat, lon)


class FakeClient:
    def __init__(self, solar=None, error=False):
        self.solar, self.error, self.calls = solar, error, []

    def fetch(self, lat, lon):
        self.calls.append((lat, lon))
        if self.error:
            raise SolarDataError("service unavailable")
        return self.solar


class TestAppraise:
    def test_headline_matches_task_results(self, demo):
        result = appraise(demo)
        assert result.hydraulics.daily_demand_m3 == pytest.approx(38.846, rel=1e-3)
        assert result.pumps.label == "1 x Tier 1"
        assert result.energy.surplus_kwh_day == 0.0
        assert result.decision.status is InvestmentStatus.INSUFFICIENT_SURPLUS
        assert result.surplus_months == ("MAY", "JUN", "JUL", "AUG")
        assert all(not assets for assets in result.seasonal_assets.values())

    def test_demand_based_scenario(self, demo):
        result = appraise(demo, Scenario(energy_basis=EnergyBasis.DEMAND))
        assert result.energy.basis is EnergyBasis.DEMAND
        assert result.decision.selected is not None
        assert result.decision.selected.asset.name == "Solar Hybrid Dryer"
        assert result.seasonal_assets["JUL"] == ("Solar Hybrid Dryer",)
        assert result.seasonal_assets["JAN"] == ()

    def test_scenario_parameters_are_used(self, solar):
        site = Site(*DEMO_SITE, {C.VEGETABLES: 7.0}, solar)  # about 12.7 kW
        identical = appraise(site)
        cheapest = appraise(site, Scenario(pump_strategy=PumpStrategy.LEAST_COST))
        assert identical.pumps.label == "2 x Tier 3"
        assert cheapest.pumps.label == "Tier 3 + Tier 2"
        longer = appraise(site, Scenario(
            hydraulic=replace(HydraulicConfig(), pumping_hours_per_day=12.0)))
        assert longer.hydraulics.required_power_kw == pytest.approx(
            identical.hydraulics.required_power_kw / 2)

    def test_om_scenario_can_make_investment_unprofitable(self, demo):
        result = appraise(demo, Scenario(energy_basis=EnergyBasis.DEMAND,
                                         economic=EconomicConfig(
                                             annual_om_fraction=0.045)))
        assert result.decision.status is InvestmentStatus.NOT_PROFITABLE

    def test_appraise_is_pure(self, demo):
        assert appraise(demo).to_dict() == appraise(demo).to_dict()


class TestSite:
    def test_areas_are_validated_and_frozen(self, solar):
        site = Site(0.0, 0.0, {"10": 1.0}, solar)
        assert isinstance(site.area_by_category_ha, MappingProxyType)
        assert site.area_by_category_ha == {C.CEREALS: 1.0}

    def test_invalid_area(self, solar):
        with pytest.raises(DataValidationError):
            Site(0.0, 0.0, {C.CEREALS: -1.0}, solar)


class TestOutputs:
    def test_to_dict_is_json_serialisable(self, demo):
        data = json.loads(json.dumps(appraise(demo).to_dict()))
        assert set(data) == {"location", "task1_cluster", "task2_solar_hydraulics",
                             "task3_pumps_energy", "task4_pue"}
        assert data["task4_pue"]["payback_years"] is None
        assert data["task4_pue"]["status"] == "insufficient_surplus"
        assert data["task1_cluster"]["area_by_category_ha"]["10"] == 0.7545

    def test_summary_reports_every_task(self, demo):
        text = appraise(demo).summary()
        for marker in ("Task 1", "Task 2", "Task 3", "Task 4", "1 x Tier 1",
                       "insufficient_surplus", "no asset feasible"):
            assert marker in text


class TestDssPipeline:
    def test_run_queries_solar_at_the_centroid(self, solar):
        client = FakeClient(solar)
        result = DssPipeline(FakeLayer(), client).run(*DEMO_SITE)
        assert client.calls == [DEMO_SITE]
        assert result.site.cluster is not None
        assert result.to_dict()["task1_cluster"]["n_parcels"] == 3

    def test_empty_cluster_propagates(self, solar):
        with pytest.raises(EmptyClusterError):
            DssPipeline(FakeLayer(), FakeClient(solar)).run(38.722, -9.139)

    def test_solar_failure_propagates(self):
        with pytest.raises(SolarDataError):
            DssPipeline(FakeLayer(), FakeClient(error=True)).run(*DEMO_SITE)


class TestMissingData:
    def test_from_file_explains_how_to_get_the_data(self, tmp_path):
        with pytest.raises(FileNotFoundError, match=r"zenodo\.org"):
            DssPipeline.from_file(tmp_path / "missing.shp")

    def test_command_line_exit_code(self, tmp_path, capsys):
        code = dss_pipeline.main(["--lat", "38.07759", "--lon", "-7.96330",
                                  "--data", str(tmp_path / "missing.shp")])
        assert code == dss_pipeline.EXIT_NO_DATA
        assert "Parcel layer not found" in capsys.readouterr().err


class TestCommandLine:
    def _main(self, args, client):
        pipeline = DssPipeline(FakeLayer(), client)
        return dss_pipeline.main(args, pipeline=pipeline)

    def test_success_prints_summary(self, solar, capsys):
        code = self._main(["--lat", "38.07759", "--lon", "-7.96330"],
                          FakeClient(solar))
        assert code == dss_pipeline.EXIT_OK
        assert "Task 4" in capsys.readouterr().out

    def test_json_output(self, solar, capsys):
        code = self._main(["--lat", "38.07759", "--lon", "-7.96330", "--json",
                           "--demand-based"], FakeClient(solar))
        assert code == dss_pipeline.EXIT_OK
        data = json.loads(capsys.readouterr().out)
        assert data["task3_pumps_energy"]["energy_basis"] == "demand"

    def test_empty_cluster_exit_code(self, solar, capsys):
        code = self._main(["--lat", "38.722", "--lon", "-9.139"], FakeClient(solar))
        assert code == dss_pipeline.EXIT_BAD_INPUT
        assert "EmptyClusterError" in capsys.readouterr().err

    def test_invalid_coordinate_exit_code(self, solar, capsys):
        code = self._main(["--lat", "95", "--lon", "-8"], FakeClient(solar))
        assert code == dss_pipeline.EXIT_BAD_INPUT
        assert "DataValidationError" in capsys.readouterr().err

    def test_solar_failure_exit_code(self, capsys):
        code = self._main(["--lat", "38.07759", "--lon", "-7.96330"],
                          FakeClient(error=True))
        assert code == dss_pipeline.EXIT_NO_SOLAR
        assert "SolarDataError" in capsys.readouterr().err


@pytest.mark.integration
@pytest.mark.skipif(not DEFAULT_PARCELS_PATH.exists(),
                    reason=f"EuroCrops PT layer not found at {DEFAULT_PARCELS_PATH}")
def test_end_to_end_on_the_supplied_layer(solar):
    pipeline = DssPipeline.from_file(client=FakeClient(solar))
    result = pipeline.run(*DEMO_SITE)
    assert result.site.cluster is not None
    assert result.site.cluster.n_parcels == 3
    assert result.pumps.label == "1 x Tier 1"
    assert result.decision.status is InvestmentStatus.INSUFFICIENT_SURPLUS
    with pytest.raises(EmptyClusterError):
        pipeline.run(38.015, -7.865)  # the brief's example point (A4)
