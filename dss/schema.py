# Copyright (c) 2026 Hao Ni. All rights reserved.
"""Column names of the source parcel layer and of the engine's parcel table.

Using another parcel layer with a different schema only requires changes here.
"""

from __future__ import annotations

from typing import Final


class SourceColumns:
    """Attribute names in the EuroCrops Portugal 2021 shapefile."""

    HCAT_CODE: Final[str] = "EC_hcat_c"  # 10-digit hierarchical HCAT code
    HCAT_NAME: Final[str] = "EC_hcat_n"  # English HCAT class name
    LAND_COVER: Final[str] = "CAT_OCU_DE"  # national land-cover category
    DECLARED_AREA_M2: Final[str] = "OSA_AREA"  # declared area, square metres

    REQUIRED: Final[tuple[str, ...]] = (
        HCAT_CODE,
        HCAT_NAME,
        LAND_COVER,
        DECLARED_AREA_M2,
    )


class ParcelColumns:
    """Attribute names of the standardised parcel table used inside the engine."""

    HCAT_CODE: Final[str] = "hcat_code"
    HCAT_NAME: Final[str] = "hcat_name"
    LAND_COVER: Final[str] = "land_cover"
    CATEGORY: Final[str] = "category"  # dss.reference_tables.CropCategory
    DECLARED_AREA_HA: Final[str] = "declared_area_ha"  # from the source, ha
    AREA_FULL_HA: Final[str] = "area_full_ha"  # whole parcel, ha (EPSG:3763)
    AREA_CLIPPED_HA: Final[str] = "area_clipped_ha"  # inside the buffer, ha

    # Mapping from source attribute names to engine attribute names.
    FROM_SOURCE: Final[dict[str, str]] = {
        SourceColumns.HCAT_CODE: HCAT_CODE,
        SourceColumns.HCAT_NAME: HCAT_NAME,
        SourceColumns.LAND_COVER: LAND_COVER,
    }
