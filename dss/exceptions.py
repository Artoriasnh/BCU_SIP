# Copyright (c) 2026 Hao Ni. All rights reserved.
"""Exceptions raised by the DSS engine; all derive from :class:`DSSError`."""

from __future__ import annotations

from typing import Final

EMPTY_CLUSTER_MESSAGE: Final[str] = (
    "Location is out of agricultural domain or contains no parcels "
    "within the 150m radius."
)


class DSSError(Exception):
    """Base class for all expected errors raised by the DSS engine."""


class DataValidationError(DSSError, ValueError):
    """Input data or arguments violate the expected schema or value ranges."""


class EmptyClusterError(DSSError):
    """No agricultural parcel intersects the command-zone buffer.

    Raised for urban centres, water bodies, locations outside Portugal or outside
    the coverage of the parcel layer. The default message is the brief's wording.
    """

    def __init__(self, message: str = EMPTY_CLUSTER_MESSAGE) -> None:
        """Initialise the exception.

        Args:
            message: Human-readable error message. Defaults to the wording
                required by the brief.
        """
        super().__init__(message)
