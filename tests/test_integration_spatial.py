# Copyright (c) 2026 Hao Ni. All rights reserved.
"""Integration tests for Task 1 against the EuroCrops Portugal 2021 layer.

Skipped automatically when ``data/raw/PT.zip`` is not present.
Run only these with ``pytest -m integration``.
"""

from __future__ import annotations

import math
import time

import pytest

from dss.config import DEFAULT_PARCELS_PATH
from dss.exceptions import EMPTY_CLUSTER_MESSAGE, EmptyClusterError
from dss.reference_tables import CropCategory
from dss.schema import ParcelColumns
from dss.spatial import ParcelLayer

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not DEFAULT_PARCELS_PATH.exists(),
        reason=f"EuroCrops PT layer not found at {DEFAULT_PARCELS_PATH}",
    ),
]

# Locations used in the brief and in run_demo.py.
LISBON = (38.722, -9.139)  # urban centre: brief expects EmptyClusterError
BRIEF_ALENTEJO = (38.015, -7.865)  # brief's "valid" example; see test below
# Demonstration site selected by rule in evaluation/task1_spatial.py.
DEMO_SITE = (38.07759, -7.96330)
DISC_HA = math.pi * 150.0**2 / 10_000


@pytest.fixture(scope="module")
def layer() -> ParcelLayer:
    return ParcelLayer.from_file()


def test_layer_contains_only_classified_agricultural_parcels(layer):
    categories = layer.parcels[ParcelColumns.CATEGORY]
    assert categories.notna().all()
    assert set(categories) <= set(CropCategory)
    assert layer.parcels.crs.to_epsg() == 3763


def test_computed_area_agrees_with_declared_area(layer):
    """Sanity check of the reprojection: EPSG:3763 areas vs IFAP declared areas."""
    declared = layer.parcels[ParcelColumns.DECLARED_AREA_HA]
    computed = layer.parcels[ParcelColumns.AREA_FULL_HA]
    relative_error = ((computed - declared).abs() / declared).median()
    assert relative_error < 0.01


def test_lisbon_raises_empty_cluster(layer):
    with pytest.raises(EmptyClusterError) as excinfo:
        layer.extract_cluster(*LISBON)
    assert str(excinfo.value) == EMPTY_CLUSTER_MESSAGE


def test_brief_alentejo_point_is_empty_in_this_sample(layer):
    """The brief's example point has no parcel within 150 m (ASSUMPTIONS.md, A4)."""
    with pytest.raises(EmptyClusterError):
        layer.extract_cluster(*BRIEF_ALENTEJO)


def test_demonstration_site_succeeds(layer):
    cluster = layer.extract_cluster(*DEMO_SITE)
    assert cluster.n_parcels == 3
    assert cluster.categories == {CropCategory.CEREALS, CropCategory.FALLBACK}
    assert 0 < cluster.total_area_clipped_ha <= DISC_HA
    assert cluster.total_area_full_ha >= cluster.total_area_clipped_ha


def test_query_is_fast_once_layer_is_loaded(layer):
    start = time.perf_counter()
    for _ in range(10):
        layer.extract_cluster(*DEMO_SITE)
    assert (time.perf_counter() - start) / 10 < 1.0  # seconds per query
