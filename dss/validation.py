# Copyright (c) 2026 Hao Ni. All rights reserved.
"""Shared input validation."""

from __future__ import annotations

import math
import numbers
from collections.abc import Iterable, Mapping
from typing import Any, Protocol

from dss.exceptions import DataValidationError
from dss.reference_tables import CROP_ARCHETYPES, CropArchetype, CropCategory


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


class AreaTable(Protocol):
    """Area per crop archetype, ha: a mapping or a pandas Series.

    Any object whose ``items()`` yields ``(archetype, area)`` pairs qualifies.
    """

    def items(self) -> Iterable[tuple[Any, Any]]:
        """Return ``(archetype, area)`` pairs."""
        ...


def validate_areas(
    area_by_category_ha: AreaTable,
    archetypes: Mapping[CropCategory, CropArchetype] = CROP_ARCHETYPES,
) -> dict[CropCategory, float]:
    """Check a mapping of crop archetype to area.

    Args:
        area_by_category_ha: Area per archetype, ha (a mapping or pandas
            Series keyed by ``CropCategory`` or its value, e.g. ``"10"``).
        archetypes: Archetypes with reference parameters.

    Returns:
        Areas as floats keyed by ``CropCategory``.

    Raises:
        DataValidationError: If a key has no reference parameters, or an area
            is negative or not finite.
    """
    areas: dict[CropCategory, float] = {}
    for key, value in area_by_category_ha.items():
        try:
            category = CropCategory(key)
        except ValueError:
            category = None
        if category is None or category not in archetypes:
            raise DataValidationError(f"No reference parameters for {key!r}.")
        try:
            area = float(value)
        except (TypeError, ValueError):
            area = math.nan
        if not math.isfinite(area) or area < 0:
            raise DataValidationError(f"Area for {category!r} is invalid: {value}.")
        areas[category] = area
    return areas
