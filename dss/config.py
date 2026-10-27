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
J_PER_KWH: Final[float] = 3.6e6  # 3600 s/h x 1000 W/kW
MJ_PER_KWH: Final[float] = 3.6

# ---------------------------------------------------------------------------
# External services
# ---------------------------------------------------------------------------
# NASA POWER climatology endpoint (brief, Section 3B; no API key required).
NASA_POWER_URL: Final[str] = (
    "https://power.larc.nasa.gov/api/temporal/climatology/point"
)


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


@dataclass(frozen=True, slots=True)
class SolarApiConfig:
    """Parameters of the NASA POWER client (Task 2).

    Attributes:
        url: Climatology endpoint.
        parameter: Irradiance parameter (all-sky surface shortwave downward).
        community: POWER user community; it determines the returned units.
        timeout_s: Timeout per HTTP request, seconds.
        max_retries: Retries after a timeout, connection error, HTTP 429 or 5xx.
        backoff_s: Initial retry delay, seconds; doubled after each retry.
        coordinate_decimals: Rounding of request coordinates; also the cache key
            resolution (4 decimals ~ 11 m).
    """

    url: str = NASA_POWER_URL
    parameter: str = "ALLSKY_SFC_SW_DWN"
    community: str = "AG"
    timeout_s: float = 10.0
    max_retries: int = 2
    backoff_s: float = 1.0
    coordinate_decimals: int = 4

    def __post_init__(self) -> None:
        """Validate parameter ranges.

        Raises:
            ValueError: If a parameter is outside its meaningful range.
        """
        if self.timeout_s <= 0:
            raise ValueError(f"timeout_s must be positive, got {self.timeout_s}.")
        if self.max_retries < 0:
            raise ValueError(f"max_retries must be >= 0, got {self.max_retries}.")
        if self.backoff_s < 0:
            raise ValueError(f"backoff_s must be >= 0, got {self.backoff_s}.")
        if not 0 <= self.coordinate_decimals <= 6:
            raise ValueError(
                "coordinate_decimals must be in [0, 6], "
                f"got {self.coordinate_decimals}."
            )


@dataclass(frozen=True, slots=True)
class HydraulicConfig:
    """Hydraulic design parameters (brief, Task 2).

    Attributes:
        total_dynamic_head_m: Static water depth plus friction losses, m.
        pump_efficiency: Combined pump and motor efficiency, dimensionless.
        pumping_hours_per_day: Daily pumping duration, h/day.
        water_density_kg_m3: Density of water, kg/m3.
        gravity_m_s2: Gravitational acceleration, m/s2.
    """

    total_dynamic_head_m: float = 40.0
    pump_efficiency: float = 0.55
    pumping_hours_per_day: float = 6.0
    water_density_kg_m3: float = 1000.0
    gravity_m_s2: float = 9.81

    def __post_init__(self) -> None:
        """Validate parameter ranges.

        Raises:
            ValueError: If a parameter is outside its physical range.
        """
        positive = {
            "total_dynamic_head_m": self.total_dynamic_head_m,
            "water_density_kg_m3": self.water_density_kg_m3,
            "gravity_m_s2": self.gravity_m_s2,
        }
        for name, value in positive.items():
            if value <= 0:
                raise ValueError(f"{name} must be positive, got {value}.")
        if not 0 < self.pump_efficiency <= 1:
            raise ValueError(
                f"pump_efficiency must be in (0, 1], got {self.pump_efficiency}."
            )
        if not 0 < self.pumping_hours_per_day <= 24:
            raise ValueError(
                "pumping_hours_per_day must be in (0, 24], "
                f"got {self.pumping_hours_per_day}."
            )


@dataclass(frozen=True, slots=True)
class EnergyConfig:
    """Energy-balance parameters (brief, Task 3).

    Attributes:
        pv_derate: System derate factor applied to PV output (temperature,
            soiling, wiring, inverter and mismatch losses), dimensionless.
    """

    pv_derate: float = 0.75

    def __post_init__(self) -> None:
        """Validate parameter ranges.

        Raises:
            ValueError: If the derate factor is outside (0, 1].
        """
        if not 0 < self.pv_derate <= 1:
            raise ValueError(f"pv_derate must be in (0, 1], got {self.pv_derate}.")
