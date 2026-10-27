# Copyright (c) 2026 Hao Ni. All rights reserved.
"""Configuration: file paths, coordinate reference systems and parameters.

Tunable values live here rather than inline in formulas, so the engine can be
re-parameterised (e.g. in a Monte Carlo loop). Parameter sets are frozen,
validated dataclasses.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Final

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
PROJECT_ROOT: Final[Path] = Path(__file__).resolve().parent.parent
DEFAULT_PARCELS_PATH: Final[Path] = (
    PROJECT_ROOT / "data" / "raw" / "PT" / "PT_2021_EC21.shp"
)

# ---------------------------------------------------------------------------
# Coordinate reference systems
# ---------------------------------------------------------------------------
# WGS 84, decimal degrees: user input and NASA POWER queries.
GEOGRAPHIC_CRS: Final[str] = "EPSG:4326"

# ETRS89 / Portugal TM06, metres: all buffering, clipping and area calculations.
METRIC_CRS: Final[str] = "EPSG:3763"

# ---------------------------------------------------------------------------
# Unit conversion factors
# ---------------------------------------------------------------------------
M2_PER_HA: Final[float] = 10_000.0


@dataclass(frozen=True, slots=True)
class SpatialConfig:
    """Parameters of the command-zone clustering (Task 1).

    Attributes:
        buffer_radius_m: Radius of the communal tube-well command zone, in metres.
            The brief specifies 150 m (pi * 150**2 ~= 7.07 ha).
        buffer_quad_segs: Segments per quarter circle of the buffer polygon
            (32 keeps the area error below 0.05 %).
        min_clipped_area_ha: Parcels with a smaller clipped area only touch the
            buffer and are discarded.
        metric_crs: Projected CRS (metres) used for geometric operations.
        geographic_crs: Geographic CRS (degrees) of user-supplied coordinates.
    """

    buffer_radius_m: float = 150.0
    buffer_quad_segs: int = 32
    min_clipped_area_ha: float = 1e-6
    metric_crs: str = METRIC_CRS
    geographic_crs: str = GEOGRAPHIC_CRS

    def __post_init__(self) -> None:
        """Validate parameter ranges.

        Raises:
            ValueError: If a parameter is outside its physically meaningful range.
        """
        if self.buffer_radius_m <= 0:
            raise ValueError(
                f"buffer_radius_m must be positive, got {self.buffer_radius_m}."
            )
        if self.buffer_quad_segs < 1:
            raise ValueError(
                f"buffer_quad_segs must be >= 1, got {self.buffer_quad_segs}."
            )
        if self.min_clipped_area_ha < 0:
            raise ValueError(
                "min_clipped_area_ha must be non-negative, "
                f"got {self.min_clipped_area_ha}."
            )
