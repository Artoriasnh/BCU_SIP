# Copyright (c) 2026 Hao Ni. All rights reserved.
"""Live test against the NASA POWER API.

Skipped unless ``DSS_RUN_NETWORK_TESTS=1``, so the default test run never depends
on network access. Run with: ``DSS_RUN_NETWORK_TESTS=1 pytest -m network``.
"""

from __future__ import annotations

import os

import pytest

from dss.solar import MONTHS, NasaPowerClient

pytestmark = [
    pytest.mark.network,
    pytest.mark.skipif(
        os.environ.get("DSS_RUN_NETWORK_TESTS") != "1",
        reason="set DSS_RUN_NETWORK_TESTS=1 to call the NASA POWER API",
    ),
]


def test_live_climatology_is_plausible_for_alentejo():
    solar = NasaPowerClient().fetch(38.2285, -7.8211)
    assert solar.source == "api"
    assert set(solar.monthly_psh) == set(MONTHS)
    # Mainland Portugal: annual mean ~4.5-5.2 kWh/m2/day; winter minimum.
    assert 4.0 < solar.annual_psh < 6.0
    assert solar.design_month in {"NOV", "DEC", "JAN"}
