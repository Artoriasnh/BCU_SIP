# Copyright (c) 2026 Hao Ni. All rights reserved.
"""Unit tests for the NASA POWER client; HTTP is replaced by a fake session."""

from __future__ import annotations

import copy
import json
import math
from pathlib import Path

import pytest
import requests

from dss.config import SolarApiConfig
from dss.exceptions import DataValidationError, SolarDataError
from dss.solar import MONTHS, NasaPowerClient, load_power_json, parse_power_response

FIXTURE = Path(__file__).parent / "fixtures" / "nasa_power_alentejo.json"
PARAM = "ALLSKY_SFC_SW_DWN"
LAT, LON = 38.015, -7.865


class FakeResponse:
    def __init__(self, status_code=200, payload=None, invalid_json=False):
        self.status_code = status_code
        self._payload = payload
        self._invalid_json = invalid_json

    def json(self):
        if self._invalid_json:
            raise ValueError("Expecting value")
        return self._payload


class FakeSession:
    """Returns (or raises) the queued outcomes in order and records each call."""

    def __init__(self, *outcomes):
        self._outcomes = list(outcomes)
        self.calls = []

    def get(self, url, *, params, timeout):
        self.calls.append({"url": url, "params": dict(params), "timeout": timeout})
        outcome = self._outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


@pytest.fixture
def payload():
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def _client(*outcomes, **kwargs):
    sleeps = []
    session = FakeSession(*outcomes)
    client = NasaPowerClient(session=session, sleep=sleeps.append, **kwargs)
    return client, session, sleeps


class TestParseResponse:
    def test_converts_mj_to_kwh(self, payload):
        solar = parse_power_response(payload, PARAM)
        raw = payload["properties"]["parameter"][PARAM]
        assert solar.annual_psh == pytest.approx(raw["ANN"] / 3.6)
        for month in MONTHS:
            assert solar.monthly_psh[month] == pytest.approx(raw[month] / 3.6)

    def test_reference_values_for_alentejo(self, payload):
        solar = parse_power_response(payload, PARAM)
        assert solar.annual_psh == pytest.approx(4.903, abs=1e-3)
        assert solar.design_month == "DEC"
        assert solar.design_psh == pytest.approx(2.106, abs=1e-3)

    def test_coordinates_default_to_response_geometry(self, payload):
        solar = parse_power_response(payload, PARAM)
        assert (solar.lat, solar.lon) == (LAT, LON)

    def test_kwh_units_are_not_rescaled(self, payload):
        payload["parameters"][PARAM]["units"] = "kW-hr/m^2/day"
        solar = parse_power_response(payload, PARAM)
        assert solar.annual_psh == payload["properties"]["parameter"][PARAM]["ANN"]

    def test_unknown_unit_rejected(self, payload):
        payload["parameters"][PARAM]["units"] = "W/m^2"
        with pytest.raises(SolarDataError, match="Unsupported unit"):
            parse_power_response(payload, PARAM)

    @pytest.mark.parametrize("bad", [-999.0, -1.0, math.nan, math.inf, "8.5", True])
    def test_invalid_monthly_value_rejected(self, payload, bad):
        payload["properties"]["parameter"][PARAM]["JUL"] = bad
        with pytest.raises(SolarDataError, match="JUL"):
            parse_power_response(payload, PARAM)

    def test_fill_value_taken_from_header(self, payload):
        payload["header"]["fill_value"] = -1.0
        payload["properties"]["parameter"][PARAM]["MAR"] = -1.0
        with pytest.raises(SolarDataError, match="fill value"):
            parse_power_response(payload, PARAM)

    def test_missing_month_rejected(self, payload):
        del payload["properties"]["parameter"][PARAM]["FEB"]
        with pytest.raises(SolarDataError, match="FEB"):
            parse_power_response(payload, PARAM)

    def test_missing_annual_value_is_monthly_mean(self, payload):
        del payload["properties"]["parameter"][PARAM]["ANN"]
        solar = parse_power_response(payload, PARAM)
        assert solar.annual_psh == pytest.approx(
            sum(solar.monthly_psh.values()) / 12
        )

    @pytest.mark.parametrize(
        "path", [("properties",), ("parameters",), ("properties", "parameter")]
    )
    def test_missing_block_rejected(self, payload, path):
        node = payload
        for key in path[:-1]:
            node = node[key]
        del node[path[-1]]
        with pytest.raises(SolarDataError, match="lacks field"):
            parse_power_response(payload, PARAM)

    def test_result_is_read_only(self, payload):
        solar = parse_power_response(payload, PARAM)
        with pytest.raises(TypeError):
            solar.monthly_psh["JAN"] = 0.0  # type: ignore[index]


class TestClient:
    def test_request_parameters(self, payload):
        client, session, _ = _client(FakeResponse(payload=payload))
        client.fetch(38.22851234, -7.82109876)
        call = session.calls[0]
        assert call["timeout"] == 10.0
        assert call["params"] == {
            "parameters": PARAM,
            "community": "AG",
            "longitude": -7.8211,
            "latitude": 38.2285,
            "format": "JSON",
        }

    def test_result_from_api(self, payload):
        client, _, _ = _client(FakeResponse(payload=payload))
        solar = client.fetch(LAT, LON)
        assert solar.source == "api"
        assert solar.annual_psh == pytest.approx(17.65 / 3.6)

    def test_memory_cache_avoids_second_request(self, payload):
        client, session, _ = _client(FakeResponse(payload=payload))
        first = client.fetch(LAT, LON)
        second = client.fetch(LAT + 1e-6, LON)  # same point at 4 decimals
        assert len(session.calls) == 1
        assert second.source == "memory-cache"
        assert second.monthly_psh == first.monthly_psh

    def test_retries_timeout_then_succeeds(self, payload):
        client, session, sleeps = _client(
            requests.Timeout("timed out"), FakeResponse(payload=payload)
        )
        assert client.fetch(LAT, LON).source == "api"
        assert len(session.calls) == 2
        assert sleeps == [1.0]

    def test_exponential_backoff_on_server_errors(self, payload):
        client, session, sleeps = _client(
            FakeResponse(503), FakeResponse(429), FakeResponse(payload=payload)
        )
        client.fetch(LAT, LON)
        assert len(session.calls) == 3
        assert sleeps == [1.0, 2.0]

    def test_gives_up_after_max_retries(self):
        client, session, _ = _client(
            FakeResponse(500), FakeResponse(500), FakeResponse(500)
        )
        with pytest.raises(SolarDataError, match="HTTP 500"):
            client.fetch(LAT, LON)
        assert len(session.calls) == 3  # 1 attempt + 2 retries

    def test_client_error_is_not_retried(self):
        client, session, sleeps = _client(FakeResponse(400))
        with pytest.raises(SolarDataError, match="HTTP 400"):
            client.fetch(LAT, LON)
        assert len(session.calls) == 1
        assert sleeps == []

    def test_invalid_json_rejected(self):
        client, _, _ = _client(FakeResponse(invalid_json=True))
        with pytest.raises(SolarDataError, match="invalid JSON"):
            client.fetch(LAT, LON)

    def test_non_object_json_rejected(self):
        client, _, _ = _client(FakeResponse(payload=[1, 2, 3]))
        with pytest.raises(SolarDataError, match="not an object"):
            client.fetch(LAT, LON)

    def test_invalid_coordinates_make_no_request(self):
        client, session, _ = _client()
        with pytest.raises(DataValidationError):
            client.fetch(120.0, LON)
        assert session.calls == []


class TestDiskCache:
    def test_response_is_cached_and_used_offline(self, payload, tmp_path):
        online, _, _ = _client(FakeResponse(payload=payload), cache_dir=tmp_path)
        online.fetch(LAT, LON)
        assert len(list(tmp_path.glob("*.json"))) == 1

        offline, _, _ = _client(
            requests.ConnectionError("offline"),
            cache_dir=tmp_path,
            config=SolarApiConfig(max_retries=0),
        )
        solar = offline.fetch(LAT, LON)
        assert solar.source == "disk-cache"
        assert solar.annual_psh == pytest.approx(17.65 / 3.6)

    def test_invalid_api_payload_falls_back_to_cache(self, payload, tmp_path):
        online, _, _ = _client(FakeResponse(payload=payload), cache_dir=tmp_path)
        online.fetch(LAT, LON)
        broken = copy.deepcopy(payload)
        broken["parameters"][PARAM]["units"] = "unknown"
        client, _, _ = _client(FakeResponse(payload=broken), cache_dir=tmp_path)
        assert client.fetch(LAT, LON).source == "disk-cache"

    def test_failure_without_cache_raises(self, tmp_path):
        client, _, _ = _client(
            requests.ConnectionError("offline"),
            cache_dir=tmp_path,
            config=SolarApiConfig(max_retries=0),
        )
        with pytest.raises(SolarDataError, match="request failed"):
            client.fetch(LAT, LON)


class TestLoadFile:
    def test_loads_saved_response(self):
        solar = load_power_json(FIXTURE)
        assert solar.source == "file"
        assert solar.design_month == "DEC"

    def test_missing_file(self, tmp_path):
        with pytest.raises(SolarDataError, match="Cannot read"):
            load_power_json(tmp_path / "missing.json")

    def test_non_object_file(self, tmp_path):
        path = tmp_path / "list.json"
        path.write_text("[]", encoding="utf-8")
        with pytest.raises(SolarDataError, match="JSON object"):
            load_power_json(path)


@pytest.mark.parametrize(
    "kwargs",
    [
        {"timeout_s": 0},
        {"max_retries": -1},
        {"backoff_s": -1},
        {"coordinate_decimals": 7},
    ],
)
def test_invalid_config(kwargs):
    with pytest.raises(ValueError):
        SolarApiConfig(**kwargs)
