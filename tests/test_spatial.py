# Copyright (c) 2026 Hao Ni. All rights reserved.
"""Unit tests for Task 1 clustering, on a synthetic layer with exact answers.

Layout around the test well (metric CRS, r = 150 m)::

    A  40 m x 40 m cereal plot, fully inside, left of the well       -> kept
    B  1 km x 2 km olive grove whose left edge passes through the
       well: exactly half of the disc lies inside it                 -> kept
    C  water body inside the disc (non-agricultural land cover)       -> dropped
    D  cork-oak stand inside the disc (excluded HCAT branch)         -> dropped
    E  strip touching the disc at a single point                     -> dropped
    F  vineyard 5 km away                                            -> dropped
"""

from __future__ import annotations

import math

import geopandas as gpd
import pytest
from shapely.geometry import Polygon, box

from dss.config import GEOGRAPHIC_CRS, METRIC_CRS, SpatialConfig
from dss.exceptions import EMPTY_CLUSTER_MESSAGE, DataValidationError, EmptyClusterError
from dss.reference_tables import CropCategory
from dss.schema import ParcelColumns, SourceColumns
from dss.spatial import ParcelLayer
from dss.taxonomy import AGRICULTURAL_LAND_COVER

WELL_LAT, WELL_LON = 38.5, -8.0
RADIUS_M = 150.0
DISC_HA = math.pi * RADIUS_M**2 / 10_000
REL_TOL = 1e-3  # polygonal approximation of the disc (128 vertices) ~ 0.04 %

AGRI = AGRICULTURAL_LAND_COVER
OTHER_LAND = "Outras superfícies"


def _well_xy() -> tuple[float, float]:
    point = gpd.GeoSeries.from_xy([WELL_LON], [WELL_LAT], crs=GEOGRAPHIC_CRS)
    projected = point.to_crs(METRIC_CRS).iloc[0]
    return projected.x, projected.y


def _box(dx0: float, dy0: float, dx1: float, dy1: float) -> Polygon:
    """Rectangle defined by offsets in metres from the well."""
    x0, y0 = _well_xy()
    return box(x0 + dx0, y0 + dy0, x0 + dx1, y0 + dy1)


def _layer_frame(
    records: list[tuple[str, str, str, Polygon]], labels: list[str] | None = None
) -> gpd.GeoDataFrame:
    """Build a source-schema layer; the national label defaults to the name."""
    return gpd.GeoDataFrame(
        {
            SourceColumns.HCAT_CODE: [r[1] for r in records],
            SourceColumns.HCAT_NAME: [r[0] for r in records],
            SourceColumns.ORIGINAL_LABEL: labels or [r[0] for r in records],
            SourceColumns.LAND_COVER: [r[2] for r in records],
            SourceColumns.DECLARED_AREA_M2: [r[3].area for r in records],
        },
        geometry=[r[3] for r in records],
        crs=METRIC_CRS,
    )


@pytest.fixture(scope="module")
def synthetic_frame() -> gpd.GeoDataFrame:
    return _layer_frame(
        [
            ("A_cereal", "3301010600", AGRI, _box(-80, -20, -40, 20)),
            ("B_olive", "3303050000", AGRI, _box(0, -1000, 1000, 1000)),
            ("C_water", "3399000000", OTHER_LAND, _box(-30, 50, -10, 70)),
            ("D_cork", "3306060000", AGRI, _box(-30, -70, -10, -50)),
            ("E_touch", "3302000000", AGRI, _box(-350, -10, -150, 10)),
            ("F_far", "3303060000", AGRI, _box(5000, 0, 5100, 100)),
        ]
    )


@pytest.fixture(scope="module")
def layer(synthetic_frame, classifier) -> ParcelLayer:
    return ParcelLayer(synthetic_frame, classifier=classifier)


@pytest.fixture(scope="module")
def cluster(layer):
    return layer.extract_cluster(WELL_LAT, WELL_LON)


class TestClusterContent:
    def test_only_agricultural_parcels_inside_are_kept(self, cluster):
        names = sorted(cluster.parcels[ParcelColumns.HCAT_NAME])
        assert names == ["A_cereal", "B_olive"]

    def test_categories(self, cluster):
        assert cluster.categories == {CropCategory.CEREALS, CropCategory.ORCHARDS}

    def test_clipped_area_is_half_disc_for_crossing_parcel(self, cluster):
        olive = cluster.parcels.set_index(ParcelColumns.HCAT_NAME).loc["B_olive"]
        assert olive[ParcelColumns.AREA_CLIPPED_HA] == pytest.approx(
            DISC_HA / 2, rel=REL_TOL
        )

    def test_full_area_is_whole_parcel(self, cluster):
        olive = cluster.parcels.set_index(ParcelColumns.HCAT_NAME).loc["B_olive"]
        assert olive[ParcelColumns.AREA_FULL_HA] == pytest.approx(200.0)

    def test_area_by_category(self, cluster):
        clipped = cluster.area_by_category()
        assert clipped[CropCategory.CEREALS] == pytest.approx(0.16)
        assert clipped[CropCategory.ORCHARDS] == pytest.approx(DISC_HA / 2, rel=REL_TOL)
        assert cluster.area_by_category(clipped=False)[CropCategory.ORCHARDS] == (
            pytest.approx(200.0)
        )

    def test_clipped_total_never_exceeds_disc(self, cluster):
        assert cluster.total_area_clipped_ha <= DISC_HA

    def test_geometries_are_clipped_to_buffer(self, cluster):
        assert cluster.parcels.geometry.within(cluster.buffer.buffer(1e-6)).all()

    def test_buffer_approximates_circle(self, cluster):
        assert cluster.buffer.area / 10_000 == pytest.approx(DISC_HA, rel=REL_TOL)

    def test_centroid_is_area_weighted_towards_large_parcel(self, cluster):
        # Most clipped area lies east of the well, so the centroid shifts east.
        assert cluster.centroid_lon > WELL_LON
        assert cluster.centroid_lat == pytest.approx(WELL_LAT, abs=1e-3)


class TestEmptyCluster:
    def test_no_parcels_nearby(self, layer):
        with pytest.raises(EmptyClusterError) as excinfo:
            layer.extract_cluster(WELL_LAT + 0.05, WELL_LON - 0.05)  # ~5-6 km away
        assert str(excinfo.value) == EMPTY_CLUSTER_MESSAGE

    def test_outside_portugal(self, layer):
        with pytest.raises(EmptyClusterError):
            layer.extract_cluster(51.5074, -0.1278)  # London

    def test_only_non_agricultural_land(self, synthetic_frame, classifier):
        names = synthetic_frame[SourceColumns.HCAT_NAME]
        frame = synthetic_frame[names.isin(["C_water", "D_cork", "E_touch"])]
        layer = ParcelLayer(frame, classifier=classifier)
        with pytest.raises(EmptyClusterError, match="out of agricultural domain"):
            layer.extract_cluster(WELL_LAT, WELL_LON)


class TestInputValidation:
    @pytest.mark.parametrize(
        ("lat", "lon"),
        [
            (95.0, -8.0),
            (38.5, 200.0),
            (math.nan, -8.0),
            (38.5, math.inf),
            ("38.5", -8.0),
            (True, -8.0),
        ],
    )
    def test_invalid_coordinates(self, layer, lat, lon):
        with pytest.raises(DataValidationError):
            layer.extract_cluster(lat, lon)

    def test_missing_attribute(self, synthetic_frame, classifier):
        frame = synthetic_frame.drop(columns=[SourceColumns.LAND_COVER])
        with pytest.raises(DataValidationError, match="lacks required attributes"):
            ParcelLayer(frame, classifier=classifier)

    def test_missing_crs(self, synthetic_frame, classifier):
        attributes = synthetic_frame.drop(columns=synthetic_frame.geometry.name)
        frame = gpd.GeoDataFrame(attributes, geometry=list(synthetic_frame.geometry))
        assert frame.crs is None
        with pytest.raises(DataValidationError, match="no CRS"):
            ParcelLayer(frame, classifier=classifier)

    @pytest.mark.parametrize(
        "kwargs",
        [{"buffer_radius_m": 0}, {"buffer_quad_segs": 0}, {"min_clipped_area_ha": -1}],
    )
    def test_invalid_config(self, kwargs):
        with pytest.raises(ValueError):
            SpatialConfig(**kwargs)


class TestWithConfig:
    def test_shares_parcels_and_applies_new_radius(self, layer):
        wider = layer.with_config(SpatialConfig(buffer_radius_m=300.0))
        assert wider.parcels is layer.parcels
        cluster = wider.extract_cluster(WELL_LAT, WELL_LON)
        disc_300_ha = math.pi * 300.0**2 / 10_000
        olive = cluster.parcels.set_index(ParcelColumns.HCAT_NAME).loc["B_olive"]
        assert olive[ParcelColumns.AREA_CLIPPED_HA] == pytest.approx(
            disc_300_ha / 2, rel=REL_TOL
        )

    def test_rejects_different_metric_crs(self, layer):
        with pytest.raises(DataValidationError, match="metric CRS"):
            layer.with_config(SpatialConfig(metric_crs="EPSG:32629"))


class TestLabelOverrides:
    def test_override_matches_code_and_label(self, classifier):
        frame = _layer_frame(
            [
                ("stone_pine", "3303030000", AGRI, _box(-60, -20, -20, 20)),
                ("walnut", "3303030000", AGRI, _box(20, -20, 60, 20)),
            ],
            labels=["PINHÃO", "NOZ"],
        )
        cluster = ParcelLayer(frame, classifier=classifier).extract_cluster(
            WELL_LAT, WELL_LON
        )
        # Same HCAT code: stone-pine stands are excluded, walnut stays an orchard.
        assert cluster.parcels[ParcelColumns.HCAT_NAME].tolist() == ["walnut"]
        assert cluster.categories == {CropCategory.ORCHARDS}

    def test_overrides_can_be_disabled(self, classifier):
        from dss.taxonomy import LabelOverrides

        frame = _layer_frame(
            [("stone_pine", "3303030000", AGRI, _box(-60, -20, -20, 20))],
            labels=["PINHÃO"],
        )
        layer = ParcelLayer(frame, classifier=classifier, overrides=LabelOverrides())
        assert layer.extract_cluster(WELL_LAT, WELL_LON).n_parcels == 1


class TestEdgeCases:
    def test_config_is_exposed(self, layer):
        assert layer.config.buffer_radius_m == RADIUS_M

    def test_missing_file(self, tmp_path):
        with pytest.raises(FileNotFoundError, match="zenodo"):
            ParcelLayer.from_file(tmp_path / "missing.shp")

    def test_parcels_without_geometry_are_dropped(self, synthetic_frame, classifier):
        frame = synthetic_frame.copy()
        frame.loc[frame.index[0], frame.geometry.name] = None  # A_cereal
        cluster = ParcelLayer(frame, classifier=classifier).extract_cluster(
            WELL_LAT, WELL_LON
        )
        assert sorted(cluster.parcels[ParcelColumns.HCAT_NAME]) == ["B_olive"]

    def test_antipode_raises_empty_cluster(self, layer):
        with pytest.raises(EmptyClusterError):
            layer.extract_cluster(-38.5, 172.0)  # New Zealand


class TestRobustness:
    def test_geographic_input_layer_is_reprojected(self, synthetic_frame, classifier):
        layer_deg = ParcelLayer(synthetic_frame.to_crs(GEOGRAPHIC_CRS), classifier)
        cluster = layer_deg.extract_cluster(WELL_LAT, WELL_LON)
        assert layer_deg.parcels.crs.to_string() == METRIC_CRS
        assert cluster.n_parcels == 2
        assert cluster.total_area_clipped_ha == pytest.approx(
            DISC_HA / 2 + 0.16, rel=REL_TOL
        )

    def test_invalid_geometry_is_repaired(self, classifier):
        x0, y0 = _well_xy()
        corners = [(-20, -20), (20, 20), (20, -20), (-20, 20)]
        bow_tie = Polygon([(x0 + dx, y0 + dy) for dx, dy in corners])
        assert not bow_tie.is_valid
        frame = _layer_frame([("bow_tie", "3301070000", AGRI, bow_tie)])
        cluster = ParcelLayer(frame, classifier=classifier).extract_cluster(
            WELL_LAT, WELL_LON
        )
        assert cluster.n_parcels == 1
        assert cluster.total_area_clipped_ha == pytest.approx(0.08)  # two triangles

    def test_numpy_scalars_accepted(self, layer):
        import numpy as np

        cluster = layer.extract_cluster(np.float64(WELL_LAT), np.float32(WELL_LON))
        assert isinstance(cluster.query_lat, float)
