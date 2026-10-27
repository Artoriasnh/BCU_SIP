# Copyright (c) 2026 Hao Ni. All rights reserved.
"""Shared input validation."""

from __future__ import annotations

import math
import numbers

from dss.exceptions import DataValidationError


def validate_coordinates(lat: object, lon: object) -> tuple[float, float]:
    """Check that coordinates are finite decimal degrees within range.

    Args:
        lat: Latitude, decimal degrees (EPSG:4326).
        lon: Longitude, decimal degrees (EPSG:4326).

    Returns:
        The coordinates as Python floats (numpy scalars are accepted).

    Raises:
        DataValidationError: If a value is not a finite number or out of range.
    """
    checked = []
    for name, value, bound in (("latitude", lat, 90.0), ("longitude", lon, 180.0)):
        if isinstance(value, bool) or not isinstance(value, numbers.Real):
            raise DataValidationError(
                f"{name} must be a number, got {type(value).__name__}."
            )
        number = float(value)
        if not math.isfinite(number) or abs(number) > bound:
            raise DataValidationError(
                f"{name} must be a finite value in [-{bound}, {bound}], got {value}."
            )
        checked.append(number)
    return checked[0], checked[1]
