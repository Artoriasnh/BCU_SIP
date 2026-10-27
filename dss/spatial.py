# Copyright (c) 2026 Hao Ni. All rights reserved.
"""Task 1: parcel ingestion and command-zone clustering.

The parcel layer is loaded once: reprojected to EPSG:3763 (metres), filtered to
agricultural parcels and spatially indexed. Each query then projects the input
point from EPSG:4326 to EPSG:3763, buffers it by 150 m, pre-selects candidate
parcels with the R-tree index and clips them to the buffer.

Each parcel reports its area inside the buffer (``area_clipped_ha``, used for
water demand; see ASSUMPTIONS A3) and its whole-parcel area (``area_full_ha``).
"""

from __future__ import annotations

import logging
import math
import numbers
from dataclasses import dataclass
from pathlib import Path

import geopandas as gpd
import pandas as pd
import shapely
from shapely.geometry import Point
from shapely.geometry.base import BaseGeometry

from dss.config import DEFAULT_PARCELS_PATH, M2_PER_HA, SpatialConfig
from dss.exceptions import DataValidationError, EmptyClusterError
from dss.reference_tables import CropCategory
from dss.schema import ParcelColumns, SourceColumns
from dss.taxonomy import HcatClassifier, is_agricultural

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True, eq=False)
class ParcelCluster:
    """Agricultural parcels inside one command-zone buffer.

    Attributes:
        query_lat: Latitude supplied by the user, decimal degrees (EPSG:4326).
        query_lon: Longitude supplied by the user, decimal degrees (EPSG:4326).
        centroid_lat: Latitude of the area-weighted centroid of the clipped
            parcels, decimal degrees (EPSG:4326).
        centroid_lon: Longitude of that centroid, decimal degrees (EPSG:4326).
        buffer: Command-zone polygon in the metric CRS.
        parcels: One row per parcel, geometry clipped to the buffer, in the
            metric CRS. Columns are listed in :class:`dss.schema.ParcelColumns`.
    """

    query_lat: float
    query_lon: float
    centroid_lat: float
    centroid_lon: float
    buffer: BaseGeometry
    parcels: gpd.GeoDataFrame

    @property
    def n_parcels(self) -> int:
        """Number of agricultural parcels in the cluster."""
        return len(self.parcels)

    @property
    def total_area_clipped_ha(self) -> float:
        """Agricultural area inside the buffer, ha."""
        return float(self.parcels[ParcelColumns.AREA_CLIPPED_HA].sum())

    @property
    def total_area_full_ha(self) -> float:
        """Whole-parcel area of all intersecting parcels, ha."""
        return float(self.parcels[ParcelColumns.AREA_FULL_HA].sum())

    @property
    def categories(self) -> frozenset[CropCategory]:
        """Crop archetypes present in the cluster."""
        return frozenset(self.parcels[ParcelColumns.CATEGORY])

    def area_by_category(self, *, clipped: bool = True) -> pd.Series:
        """Aggregate parcel area per crop archetype.

        Args:
            clipped: If True (default), use the area inside the buffer;
                otherwise use the whole-parcel area.

        Returns:
            Area in hectares indexed by :class:`CropCategory`, sorted by
            category, only for categories present in the cluster.
        """
        column = (
            ParcelColumns.AREA_CLIPPED_HA if clipped else ParcelColumns.AREA_FULL_HA
        )
        area: pd.Series = self.parcels.groupby(ParcelColumns.CATEGORY)[column].sum()
        # CropCategory is a str-Enum, so sorting orders by value: 10, 20, ..., OTHER.
        return area.sort_index()


class ParcelLayer:
    """National parcel layer, prepared once and queried per location.

    Example:
        >>> layer = ParcelLayer.from_file()          # doctest: +SKIP
        >>> cluster = layer.extract_cluster(38.2285, -7.8211)  # doctest: +SKIP
        >>> cluster.area_by_category()               # doctest: +SKIP
    """

    def __init__(
        self,
        parcels: gpd.GeoDataFrame,
        classifier: HcatClassifier | None = None,
        config: SpatialConfig | None = None,
    ) -> None:
        """Prepare a parcel layer for clustering queries.

        Args:
            parcels: Parcel polygons with the attributes listed in
                :attr:`dss.schema.SourceColumns.REQUIRED`, in any CRS.
            classifier: HCAT classifier. Defaults to the packaged mapping.
            config: Spatial parameters. Defaults to the brief's values.

        Raises:
            DataValidationError: If the layer has no CRS or lacks a required
                attribute.
        """
        self._config = config or SpatialConfig()
        self._classifier = classifier or HcatClassifier.from_csv()
        self._parcels = self._prepare(parcels)
        # Build the R-tree now rather than lazily on the first query.
        _ = self._parcels.sindex
        logger.info(
            "Parcel layer ready: %d agricultural parcels indexed in %s.",
            len(self._parcels),
            self._config.metric_crs,
        )

    @classmethod
    def from_file(
        cls,
        path: str | Path = DEFAULT_PARCELS_PATH,
        classifier: HcatClassifier | None = None,
        config: SpatialConfig | None = None,
    ) -> ParcelLayer:
        """Read a parcel layer from disk.

        Only the required attributes are read, to keep memory low.

        Args:
            path: Shapefile (or any OGR-readable vector file, including a
                ``.zip``) containing the parcels.
            classifier: HCAT classifier. Defaults to the packaged mapping.
            config: Spatial parameters. Defaults to the brief's values.

        Returns:
            A prepared, indexed parcel layer.

        Raises:
            FileNotFoundError: If ``path`` does not exist.
            DataValidationError: If the file lacks a CRS or required attributes.
        """
        path = Path(path)
        if not path.exists():
            raise FileNotFoundError(
                f"Parcel layer not found at {path}. Download PT.zip from "
                "https://zenodo.org/records/10118572 and unzip it into data/raw/."
            )
        logger.info("Reading parcel layer from %s ...", path)
        parcels = gpd.read_file(
            path, columns=list(SourceColumns.REQUIRED), engine="pyogrio"
        )
        return cls(parcels, classifier=classifier, config=config)

    @property
    def parcels(self) -> gpd.GeoDataFrame:
        """Prepared agricultural parcels (metric CRS)."""
        return self._parcels

    @property
    def config(self) -> SpatialConfig:
        """Spatial parameters in use."""
        return self._config

    def extract_cluster(self, lat: float, lon: float) -> ParcelCluster:
        """Extract the agricultural parcels inside the command-zone buffer.

        Args:
            lat: Latitude of the tube well, decimal degrees (EPSG:4326).
            lon: Longitude of the tube well, decimal degrees (EPSG:4326).

        Returns:
            The parcel cluster, with geometries clipped to the buffer.

        Raises:
            DataValidationError: If the coordinates are not finite or out of range.
            EmptyClusterError: If no agricultural parcel lies inside the buffer.
        """
        _validate_coordinates(lat, lon)
        lat, lon = float(lat), float(lon)  # accept numpy scalars, store floats
        cfg = self._config

        # Create the point in degrees, reproject to metres, and only then buffer.
        point_metric = (
            gpd.GeoSeries([Point(lon, lat)], crs=cfg.geographic_crs)
            .to_crs(cfg.metric_crs)
            .iloc[0]
        )
        buffer = point_metric.buffer(
            cfg.buffer_radius_m, quad_segs=cfg.buffer_quad_segs
        )

        # The R-tree pre-selects candidates; the exact test runs on those only.
        positions = self._parcels.sindex.query(buffer, predicate="intersects")
        if len(positions) == 0:
            raise EmptyClusterError()

        candidates = self._parcels.iloc[positions]
        clipped = candidates.geometry.intersection(buffer)
        area_clipped_ha = clipped.area / M2_PER_HA
        # Drop parcels that only touch the boundary (zero-area intersection).
        inside = area_clipped_ha > cfg.min_clipped_area_ha
        if not inside.any():
            raise EmptyClusterError()

        parcels = candidates.loc[inside].copy()
        parcels[ParcelColumns.AREA_CLIPPED_HA] = area_clipped_ha[inside]
        parcels[parcels.geometry.name] = clipped[inside]

        centroid_lat, centroid_lon = self._centroid_degrees(parcels.geometry)
        logger.debug(
            "Cluster at (%.5f, %.5f): %d parcels, %.3f ha inside the buffer.",
            lat,
            lon,
            len(parcels),
            parcels[ParcelColumns.AREA_CLIPPED_HA].sum(),
        )
        return ParcelCluster(
            query_lat=lat,
            query_lon=lon,
            centroid_lat=centroid_lat,
            centroid_lon=centroid_lon,
            buffer=buffer,
            parcels=parcels,
        )

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------
    def _prepare(self, raw: gpd.GeoDataFrame) -> gpd.GeoDataFrame:
        """Validate, clean, reproject, classify and filter the raw layer.

        Args:
            raw: Parcel layer as read from disk.

        Returns:
            Agricultural parcels in the metric CRS with standardised columns.

        Raises:
            DataValidationError: If the layer has no CRS or lacks attributes.
        """
        missing = set(SourceColumns.REQUIRED) - set(raw.columns)
        if missing:
            raise DataValidationError(
                f"Parcel layer lacks required attributes: {sorted(missing)}."
            )
        if raw.crs is None:
            raise DataValidationError(
                "Parcel layer has no CRS; cannot reproject to metres safely."
            )

        layer = raw.loc[:, [*SourceColumns.REQUIRED, raw.geometry.name]].copy()

        has_geometry = ~(layer.geometry.isna() | layer.geometry.is_empty)
        if not has_geometry.all():
            logger.warning(
                "Dropping %d parcels without geometry.", int((~has_geometry).sum())
            )
            layer = layer.loc[has_geometry]

        # Reproject first: validity is assessed in the CRS used for clipping.
        layer = layer.to_crs(self._config.metric_crs)
        invalid = ~layer.geometry.is_valid
        if invalid.any():
            logger.warning("Repairing %d invalid geometries.", int(invalid.sum()))
            layer.loc[invalid, layer.geometry.name] = shapely.make_valid(
                layer.geometry[invalid].values
            )

        layer = layer.rename(columns=ParcelColumns.FROM_SOURCE)
        layer[ParcelColumns.HCAT_CODE] = (
            layer[ParcelColumns.HCAT_CODE].astype("string").str.strip()
        )
        layer[ParcelColumns.CATEGORY] = self._classifier.classify_series(
            layer[ParcelColumns.HCAT_CODE]
        )
        layer[ParcelColumns.DECLARED_AREA_HA] = (
            pd.to_numeric(layer[SourceColumns.DECLARED_AREA_M2], errors="coerce")
            / M2_PER_HA
        )
        layer[ParcelColumns.AREA_FULL_HA] = layer.geometry.area / M2_PER_HA

        agricultural = is_agricultural(
            layer[ParcelColumns.LAND_COVER], layer[ParcelColumns.CATEGORY]
        )
        logger.info(
            "Kept %d of %d parcels as agricultural (land-cover and HCAT filters).",
            int(agricultural.sum()),
            len(layer),
        )
        columns = [
            ParcelColumns.HCAT_CODE,
            ParcelColumns.HCAT_NAME,
            ParcelColumns.LAND_COVER,
            ParcelColumns.CATEGORY,
            ParcelColumns.DECLARED_AREA_HA,
            ParcelColumns.AREA_FULL_HA,
            layer.geometry.name,
        ]
        return layer.loc[agricultural, columns].reset_index(drop=True)

    def _centroid_degrees(self, geometries: gpd.GeoSeries) -> tuple[float, float]:
        """Area-weighted centroid of geometries, returned as (lat, lon) degrees.

        Computed in the metric CRS, then converted to EPSG:4326.

        Args:
            geometries: Clipped parcel geometries in the metric CRS.

        Returns:
            Latitude and longitude of the centroid, decimal degrees.
        """
        centroid = shapely.union_all(geometries.values).centroid
        centroid_deg = (
            gpd.GeoSeries([centroid], crs=self._config.metric_crs)
            .to_crs(self._config.geographic_crs)
            .iloc[0]
        )
        return float(centroid_deg.y), float(centroid_deg.x)


def _validate_coordinates(lat: float, lon: float) -> None:
    """Check that coordinates are finite decimal degrees within range.

    Args:
        lat: Latitude, decimal degrees.
        lon: Longitude, decimal degrees.

    Raises:
        DataValidationError: If a value is not a finite number or out of range.
    """
    for name, value, bound in (("latitude", lat, 90.0), ("longitude", lon, 180.0)):
        if isinstance(value, bool) or not isinstance(value, numbers.Real):
            raise DataValidationError(
                f"{name} must be a number, got {type(value).__name__}."
            )
        if not math.isfinite(value) or abs(value) > bound:
            raise DataValidationError(
                f"{name} must be a finite value in [-{bound}, {bound}], got {value}."
            )
