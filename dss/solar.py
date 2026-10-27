# Copyright (c) 2026 Hao Ni. All rights reserved.
"""Task 2a: solar resource from the NASA POWER climatology API.

One request is made per cluster centroid. The response is validated and
converted to kWh/m2/day (numerically equal to peak sun hours, PSH) using the unit
string in the response metadata: with ``community=AG`` the API returns MJ/m2/day,
not kWh/m2/day as stated in the brief. Results are cached in memory and, if a
cache directory is given, on disk; the disk copy is also used as an offline
fallback for the same coordinates.

``ALLSKY_SFC_SW_DWN`` is irradiance on a horizontal surface; no tilt gain is
applied, which is conservative.
"""

from __future__ import annotations

import json
import logging
import math
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from pathlib import Path
from types import MappingProxyType
from typing import Any, Final, Protocol

import requests

from dss.config import MJ_PER_KWH, SolarApiConfig
from dss.exceptions import SolarDataError
from dss.validation import validate_coordinates

logger = logging.getLogger(__name__)

MONTHS: Final[tuple[str, ...]] = (
    "JAN", "FEB", "MAR", "APR", "MAY", "JUN",
    "JUL", "AUG", "SEP", "OCT", "NOV", "DEC",
)  # fmt: skip

# Multiply a value in the given unit by this factor to obtain kWh/m2/day.
KWH_PER_UNIT: Final[Mapping[str, float]] = MappingProxyType(
    {
        "MJ/m^2/day": 1.0 / MJ_PER_KWH,
        "kW-hr/m^2/day": 1.0,
        "kWh/m^2/day": 1.0,
    }
)

_DEFAULT_FILL_VALUE: Final[float] = -999.0


class HttpResponse(Protocol):
    """Subset of :class:`requests.Response` used by the client."""

    status_code: int

    def json(self) -> Any:
        """Decode the response body as JSON."""


class HttpSession(Protocol):
    """Subset of :class:`requests.Session` used by the client."""

    def get(
        self, url: str, *, params: Mapping[str, str | float], timeout: float
    ) -> HttpResponse:
        """Send a GET request."""


@dataclass(frozen=True, slots=True)
class SolarClimatology:
    """Long-term monthly solar resource at one location.

    Attributes:
        lat: Latitude of the query, decimal degrees.
        lon: Longitude of the query, decimal degrees.
        monthly_psh: Mean daily irradiation per month (``JAN`` ... ``DEC``),
            kWh/m2/day, i.e. peak sun hours.
        annual_psh: Annual mean daily irradiation, kWh/m2/day (PSH_avg).
        source: Origin of the data: ``api``, ``memory-cache``, ``disk-cache``
            or ``file``.
    """

    lat: float
    lon: float
    monthly_psh: Mapping[str, float]
    annual_psh: float
    source: str

    @property
    def design_month(self) -> str:
        """Month with the lowest irradiation (the design month)."""
        return min(MONTHS, key=lambda month: self.monthly_psh[month])

    @property
    def design_psh(self) -> float:
        """Peak sun hours of the design month, kWh/m2/day."""
        return self.monthly_psh[self.design_month]


class NasaPowerClient:
    """Client for the NASA POWER point climatology endpoint.

    Example:
        >>> client = NasaPowerClient()                  # doctest: +SKIP
        >>> solar = client.fetch(38.2285, -7.8211)      # doctest: +SKIP
        >>> solar.annual_psh, solar.design_month        # doctest: +SKIP
    """

    def __init__(
        self,
        config: SolarApiConfig | None = None,
        session: HttpSession | None = None,
        cache_dir: str | Path | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        """Create a client.

        Args:
            config: API parameters. Defaults to the brief's request.
            session: HTTP session; injectable for testing.
            cache_dir: Optional directory for raw responses (disk cache and
                offline fallback).
            sleep: Function used to wait between retries; injectable for testing.
        """
        self._config = config or SolarApiConfig()
        self._session: HttpSession = session or requests.Session()
        self._cache_dir = Path(cache_dir) if cache_dir is not None else None
        self._sleep = sleep
        self._memory: dict[tuple[float, float], SolarClimatology] = {}

    def fetch(self, lat: float, lon: float) -> SolarClimatology:
        """Return the solar climatology at a location.

        Args:
            lat: Latitude, decimal degrees (EPSG:4326).
            lon: Longitude, decimal degrees (EPSG:4326).

        Returns:
            Monthly and annual peak sun hours.

        Raises:
            DataValidationError: If the coordinates are invalid.
            SolarDataError: If the API fails after all retries, or its response
                is invalid, and no disk-cached response exists.
        """
        lat, lon = validate_coordinates(lat, lon)
        digits = self._config.coordinate_decimals
        key = (round(lat, digits), round(lon, digits))

        if key in self._memory:
            return replace(self._memory[key], source="memory-cache")

        try:
            payload = self._request(*key)
            climatology = parse_power_response(
                payload, self._config.parameter, lat=key[0], lon=key[1], source="api"
            )
        except SolarDataError as error:
            cached = self._read_disk_cache(key)
            if cached is None:
                raise
            logger.warning("NASA POWER unavailable (%s); using disk cache.", error)
            climatology = parse_power_response(
                cached,
                self._config.parameter,
                lat=key[0],
                lon=key[1],
                source="disk-cache",
            )
        else:
            self._write_disk_cache(key, payload)

        self._memory[key] = climatology
        return climatology

    def _request(self, lat: float, lon: float) -> Mapping[str, Any]:
        """GET the climatology, retrying transient failures with backoff.

        Args:
            lat: Rounded latitude, decimal degrees.
            lon: Rounded longitude, decimal degrees.

        Returns:
            The decoded JSON payload.

        Raises:
            SolarDataError: On a non-retryable error, invalid JSON, or when all
                retries are exhausted.
        """
        cfg = self._config
        params: dict[str, str | float] = {
            "parameters": cfg.parameter,
            "community": cfg.community,
            "longitude": lon,
            "latitude": lat,
            "format": "JSON",
        }
        attempts = cfg.max_retries + 1
        for attempt in range(1, attempts + 1):
            try:
                response = self._session.get(
                    cfg.url, params=params, timeout=cfg.timeout_s
                )
            except requests.RequestException as exc:
                error = SolarDataError(f"NASA POWER request failed: {exc}")
                retryable = isinstance(exc, requests.Timeout | requests.ConnectionError)
            else:
                if response.status_code == 200:
                    return _decode_json(response)
                error = SolarDataError(
                    f"NASA POWER returned HTTP {response.status_code}."
                )
                retryable = response.status_code == 429 or response.status_code >= 500

            if not retryable or attempt == attempts:
                raise error
            delay = cfg.backoff_s * 2 ** (attempt - 1)
            logger.warning(
                "%s Retrying in %.1f s (attempt %d of %d).",
                error,
                delay,
                attempt + 1,
                attempts,
            )
            self._sleep(delay)
        raise AssertionError("unreachable")  # pragma: no cover

    def _cache_path(self, key: tuple[float, float]) -> Path | None:
        """Disk-cache file for a coordinate key, or None without a cache dir."""
        if self._cache_dir is None:
            return None
        cfg = self._config
        name = f"power_{cfg.parameter}_{cfg.community}_{key[0]:+.4f}_{key[1]:+.4f}.json"
        return self._cache_dir / name

    def _read_disk_cache(self, key: tuple[float, float]) -> Mapping[str, Any] | None:
        """Load a cached raw response, or None if absent or unreadable."""
        path = self._cache_path(key)
        if path is None or not path.exists():
            return None
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            logger.warning("Ignoring unreadable cache file %s.", path)
            return None
        return payload if isinstance(payload, Mapping) else None

    def _write_disk_cache(
        self, key: tuple[float, float], payload: Mapping[str, Any]
    ) -> None:
        """Store a raw response; failures are logged, not raised."""
        path = self._cache_path(key)
        if path is None:
            return
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(payload), encoding="utf-8")
        except OSError as exc:
            logger.warning("Could not write cache file %s: %s", path, exc)


def parse_power_response(
    payload: Mapping[str, Any],
    parameter: str = "ALLSKY_SFC_SW_DWN",
    *,
    lat: float | None = None,
    lon: float | None = None,
    source: str = "api",
) -> SolarClimatology:
    """Validate a NASA POWER climatology response and convert it to PSH.

    Args:
        payload: Decoded JSON response.
        parameter: Name of the irradiance parameter in the response.
        lat: Latitude to report; defaults to the response geometry.
        lon: Longitude to report; defaults to the response geometry.
        source: Label describing where the payload came from.

    Returns:
        Monthly and annual peak sun hours in kWh/m2/day.

    Raises:
        SolarDataError: If a field is missing, a unit is unknown, or a value is
            missing (fill value), non-numeric, non-finite or negative.
    """
    values = _lookup(payload, "properties", "parameter", parameter)
    units = _lookup(payload, "parameters", parameter, "units")
    if units not in KWH_PER_UNIT:
        raise SolarDataError(
            f"Unsupported unit {units!r} for {parameter}; "
            f"expected one of {sorted(KWH_PER_UNIT)}."
        )
    factor = KWH_PER_UNIT[units]
    header = payload.get("header")
    fill_value = (
        header.get("fill_value", _DEFAULT_FILL_VALUE)
        if isinstance(header, Mapping)
        else _DEFAULT_FILL_VALUE
    )

    monthly = {
        month: _checked_value(values, month, fill_value) * factor for month in MONTHS
    }
    if "ANN" in values:
        annual = _checked_value(values, "ANN", fill_value) * factor
    else:
        annual = sum(monthly.values()) / len(MONTHS)

    if lat is None or lon is None:
        coordinates = _lookup(payload, "geometry", "coordinates")
        try:
            lon, lat = float(coordinates[0]), float(coordinates[1])
        except (TypeError, IndexError, ValueError) as exc:
            raise SolarDataError("NASA POWER geometry is malformed.") from exc

    return SolarClimatology(
        lat=lat,
        lon=lon,
        monthly_psh=MappingProxyType(monthly),
        annual_psh=annual,
        source=source,
    )


def load_power_json(
    path: str | Path, parameter: str = "ALLSKY_SFC_SW_DWN"
) -> SolarClimatology:
    """Read a saved NASA POWER response from disk.

    Args:
        path: JSON file saved from the API.
        parameter: Name of the irradiance parameter in the response.

    Returns:
        Monthly and annual peak sun hours in kWh/m2/day.

    Raises:
        SolarDataError: If the file cannot be read or fails validation.
    """
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise SolarDataError(f"Cannot read NASA POWER file {path}: {exc}") from exc
    if not isinstance(payload, Mapping):
        raise SolarDataError(f"{path} does not contain a JSON object.")
    return parse_power_response(payload, parameter, source="file")


def _decode_json(response: HttpResponse) -> Mapping[str, Any]:
    """Decode a response body, requiring a JSON object."""
    try:
        payload = response.json()
    except ValueError as exc:
        raise SolarDataError("NASA POWER returned invalid JSON.") from exc
    if not isinstance(payload, Mapping):
        raise SolarDataError("NASA POWER returned JSON that is not an object.")
    return payload


def _lookup(payload: Mapping[str, Any], *path: str) -> Any:
    """Follow a key path through nested mappings."""
    node: Any = payload
    for key in path:
        if not isinstance(node, Mapping) or key not in node:
            raise SolarDataError(
                f"NASA POWER response lacks field {'.'.join(path)!r}."
            )
        node = node[key]
    return node


def _checked_value(values: Mapping[str, Any], key: str, fill_value: float) -> float:
    """Return a validated non-negative irradiance value."""
    if key not in values:
        raise SolarDataError(f"NASA POWER response lacks the {key} value.")
    value = values[key]
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise SolarDataError(f"{key} value {value!r} is not numeric.")
    if value == fill_value:
        raise SolarDataError(f"{key} value is missing (fill value {fill_value}).")
    if not math.isfinite(value) or value < 0:
        raise SolarDataError(f"{key} value {value} is not a valid irradiance.")
    return float(value)
