# Copyright (c) 2026 Hao Ni. All rights reserved.
"""Agri-Energy Decision Support System (DSS) prototype engine.

Modules:
    taxonomy: HCAT crop codes to the brief's crop archetypes.
    spatial: Task 1, parcel ingestion and command-zone clustering.
    solar: Task 2, NASA POWER solar climatology.
    hydraulics: Task 2, water demand and required pump power.
    energy: Task 3, pump selection and PV energy balance.

Geospatial classes are imported lazily, so ``dss.taxonomy`` works without the
geospatial stack.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from dss.energy import (
    EnergyBalance,
    EnergyBasis,
    PumpSelection,
    PumpStrategy,
    energy_balance,
    monthly_energy_balance,
    select_pumps,
    surplus_threshold_psh,
)
from dss.exceptions import (
    DataValidationError,
    DSSError,
    EmptyClusterError,
    SolarDataError,
)
from dss.hydraulics import HydraulicDemand, size_hydraulics
from dss.reference_tables import CROP_ARCHETYPES, CropArchetype, CropCategory
from dss.solar import NasaPowerClient, SolarClimatology
from dss.taxonomy import HcatClassifier

if TYPE_CHECKING:  # pragma: no cover
    from dss.spatial import ParcelCluster, ParcelLayer

__all__ = [
    "CROP_ARCHETYPES",
    "CropArchetype",
    "CropCategory",
    "DSSError",
    "DataValidationError",
    "EmptyClusterError",
    "EnergyBalance",
    "EnergyBasis",
    "HcatClassifier",
    "HydraulicDemand",
    "NasaPowerClient",
    "ParcelCluster",
    "ParcelLayer",
    "PumpSelection",
    "PumpStrategy",
    "SolarClimatology",
    "SolarDataError",
    "energy_balance",
    "monthly_energy_balance",
    "select_pumps",
    "size_hydraulics",
    "surplus_threshold_psh",
]

__version__ = "0.1.0"

_LAZY_SPATIAL_EXPORTS = frozenset({"ParcelCluster", "ParcelLayer"})


def __getattr__(name: str) -> Any:
    """Import geospatial exports on first access.

    Args:
        name: Attribute requested from the package.

    Returns:
        The requested class from ``dss.spatial``.

    Raises:
        AttributeError: If ``name`` is not a public export of the package.
    """
    if name in _LAZY_SPATIAL_EXPORTS:
        from dss import spatial

        return getattr(spatial, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
