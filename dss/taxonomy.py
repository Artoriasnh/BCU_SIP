# Copyright (c) 2026 Hao Ni. All rights reserved.
"""Map EuroCrops HCAT codes to the brief's crop archetypes.

All EuroCrops codes start with "33" (the position of agriculture in the EAGLE
land-cover matrix), so the brief's two-digit prefixes (10/20/30/50) match no
parcel. Instead, the rules in ``resources/hcat_mapping.csv`` map HCAT branches
(code prefixes) to archetypes. The longest matching prefix wins, so a specific
rule can override its parent; unmatched or missing codes fall back to the brief's
"Other / NaN" archetype. See ASSUMPTIONS.md, A1.

A small override table (``resources/label_overrides.csv``) corrects documented
EuroCrops harmonisation errors. Each override matches both the HCAT code and the
national crop label, so it cannot affect other parcels with the same code.
"""

from __future__ import annotations

import csv
import math
import re
import unicodedata
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from importlib import resources
from pathlib import Path
from typing import Final

import pandas as pd

from dss.exceptions import DataValidationError
from dss.reference_tables import CropCategory

# National land-cover category of agricultural land in the source layer.
AGRICULTURAL_LAND_COVER: Final[str] = "Superfície agrícola"

# Token used in the mapping CSV for HCAT branches that are not irrigated
# agriculture (forestry, shrubland). Such parcels are removed from the cluster.
EXCLUDED_TOKEN: Final[str] = "EXCLUDED"

_PREFIX_PATTERN: Final[re.Pattern[str]] = re.compile(r"^\d{1,10}$")
_MAPPING_RESOURCE: Final[str] = "hcat_mapping.csv"
_OVERRIDES_RESOURCE: Final[str] = "label_overrides.csv"
_CODE_PATTERN: Final[re.Pattern[str]] = re.compile(r"^\d{10}$")


@dataclass(frozen=True, slots=True)
class HcatRule:
    """One row of the HCAT mapping table.

    Attributes:
        prefix: HCAT code prefix (1 to 10 digits) identifying a taxonomy branch.
        category: Target crop archetype, or ``None`` if the branch is excluded
            from irrigated agriculture.
        label: Short human-readable name of the branch.
        rationale: Justification of the assignment.
    """

    prefix: str
    category: CropCategory | None
    label: str
    rationale: str


class HcatClassifier:
    """Classify 10-digit HCAT codes into crop archetypes by longest prefix.

    Example:
        >>> classifier = HcatClassifier.from_csv()
        >>> classifier.classify("3301010600")  # grain maize
        <CropCategory.CEREALS: '10'>
    """

    def __init__(self, rules: Sequence[HcatRule]) -> None:
        """Build a classifier from mapping rules.

        Args:
            rules: Mapping rules. Prefixes must be unique digit strings.

        Raises:
            DataValidationError: If ``rules`` is empty, or a prefix is malformed
                or duplicated.
        """
        if not rules:
            raise DataValidationError("At least one HCAT mapping rule is required.")
        seen: set[str] = set()
        for rule in rules:
            if not _PREFIX_PATTERN.fullmatch(rule.prefix):
                raise DataValidationError(
                    f"Invalid HCAT prefix {rule.prefix!r}: expected 1-10 digits."
                )
            if rule.prefix in seen:
                raise DataValidationError(f"Duplicate HCAT prefix {rule.prefix!r}.")
            seen.add(rule.prefix)
        # Longest prefix first, so the first match is the most specific rule.
        self._rules: tuple[HcatRule, ...] = tuple(
            sorted(rules, key=lambda rule: len(rule.prefix), reverse=True)
        )

    @classmethod
    def from_csv(cls, path: str | Path | None = None) -> HcatClassifier:
        """Load mapping rules from a CSV file.

        Args:
            path: CSV with columns ``hcat_prefix``, ``category``, ``label`` and
                ``rationale``. Defaults to the packaged ``hcat_mapping.csv``.

        Returns:
            A classifier built from the file.

        Raises:
            DataValidationError: If a column is missing or a category is unknown.
        """
        text = _read_text(path, _MAPPING_RESOURCE)
        return cls(list(_parse_rules(text.splitlines())))

    @property
    def rules(self) -> tuple[HcatRule, ...]:
        """Mapping rules, ordered from the most to the least specific."""
        return self._rules

    def classify(self, code: object) -> CropCategory | None:
        """Classify a single HCAT code.

        Args:
            code: HCAT code. Missing values (``None``, ``NaN``, blank strings)
                are accepted and fall back, as per the brief's "Other / NaN" row.

        Returns:
            The crop archetype, or ``None`` if the code belongs to an excluded
            (non-agricultural) branch.
        """
        normalised = _normalise_code(code)
        if normalised is None:
            return CropCategory.FALLBACK
        for rule in self._rules:
            if normalised.startswith(rule.prefix):
                return rule.category
        return CropCategory.FALLBACK

    def classify_series(self, codes: pd.Series) -> pd.Series:
        """Classify a column of HCAT codes.

        Each unique code is classified once and the result is broadcast.

        Args:
            codes: Series of HCAT codes.

        Returns:
            Series aligned with ``codes`` holding ``CropCategory`` members, or
            ``None`` for excluded branches.
        """
        # Missing codes become "" (which falls back) so that they are not
        # confused with excluded branches, which are encoded as None.
        keys = codes.astype("string").fillna("")
        lookup = {key: self.classify(key) for key in pd.unique(keys)}
        # Built explicitly: Series.map would silently turn None into NaN.
        return pd.Series(
            [lookup[key] for key in keys], index=codes.index, dtype=object
        )


@dataclass(frozen=True, slots=True)
class LabelOverride:
    """Correction of one EuroCrops harmonisation error.

    Attributes:
        hcat_code: Full 10-digit HCAT code assigned by EuroCrops.
        original_label: National crop label (``EC_org_n``) the override applies to.
        category: Corrected crop archetype, or ``None`` to exclude the parcels.
        rationale: Justification of the correction.
    """

    hcat_code: str
    original_label: str
    category: CropCategory | None
    rationale: str


class LabelOverrides:
    """Apply label-specific corrections on top of the code-based classification."""

    def __init__(self, overrides: Sequence[LabelOverride] = ()) -> None:
        """Build the override table.

        Args:
            overrides: Corrections; each (code, label) pair must be unique.

        Raises:
            DataValidationError: If a code is malformed or a key is duplicated.
        """
        keys: set[tuple[str, str]] = set()
        for override in overrides:
            if not _CODE_PATTERN.fullmatch(override.hcat_code):
                raise DataValidationError(
                    f"Invalid HCAT code {override.hcat_code!r}: expected 10 digits."
                )
            key = (override.hcat_code, _normalise_label(override.original_label))
            if key in keys:
                raise DataValidationError(f"Duplicate override for {key}.")
            keys.add(key)
        self._overrides: tuple[LabelOverride, ...] = tuple(overrides)

    @classmethod
    def from_csv(cls, path: str | Path | None = None) -> LabelOverrides:
        """Load overrides from a CSV file.

        Args:
            path: CSV with columns ``hcat_code``, ``original_label``, ``category``
                and ``rationale``. Defaults to the packaged ``label_overrides.csv``.

        Returns:
            The override table.

        Raises:
            DataValidationError: If a column is missing or a category is unknown.
        """
        reader = csv.DictReader(_read_text(path, _OVERRIDES_RESOURCE).splitlines())
        _require_columns(
            reader, {"hcat_code", "original_label", "category", "rationale"}
        )
        return cls(
            [
                LabelOverride(
                    hcat_code=row["hcat_code"].strip(),
                    original_label=row["original_label"].strip(),
                    category=_parse_category(row["category"], row["hcat_code"]),
                    rationale=row["rationale"].strip(),
                )
                for row in reader
            ]
        )

    @property
    def overrides(self) -> tuple[LabelOverride, ...]:
        """The corrections, in file order."""
        return self._overrides

    def apply(
        self, codes: pd.Series, labels: pd.Series, categories: pd.Series
    ) -> pd.Series:
        """Return ``categories`` with the corrections applied.

        Args:
            codes: HCAT code per parcel.
            labels: National crop label per parcel (``EC_org_n``).
            categories: Code-based classification of the same parcels.

        Returns:
            Corrected categories, aligned with the inputs.
        """
        result = categories.to_numpy(dtype=object, copy=True)
        clean_codes = codes.astype("string").str.strip()
        clean_labels = labels.astype("string").map(
            _normalise_label, na_action="ignore"
        )
        for override in self._overrides:
            mask = (clean_codes == override.hcat_code) & (
                clean_labels == _normalise_label(override.original_label)
            )
            result[mask.fillna(False).to_numpy(dtype=bool)] = override.category
        return pd.Series(result, index=categories.index, dtype=object)


def is_agricultural(land_cover: pd.Series, categories: pd.Series) -> pd.Series:
    """Flag parcels that count as irrigable agricultural land.

    A parcel qualifies when its national land-cover category is agricultural land
    (removing forest, built-up land and water) and its HCAT branch is not excluded.

    Args:
        land_cover: National land-cover category per parcel (``CAT_OCU_DE``).
        categories: Output of :meth:`HcatClassifier.classify_series`.

    Returns:
        Boolean Series aligned with the inputs.
    """
    target = unicodedata.normalize("NFC", AGRICULTURAL_LAND_COVER)
    normalised = land_cover.astype("string").str.strip().str.normalize("NFC")
    return (normalised == target).fillna(False).astype(bool) & categories.notna()


def _parse_rules(lines: Iterable[str]) -> Iterable[HcatRule]:
    """Parse mapping rules from CSV lines.

    Args:
        lines: Lines of the mapping CSV, header included.

    Yields:
        One :class:`HcatRule` per data row.

    Raises:
        DataValidationError: If a column is missing or a category is unknown.
    """
    reader = csv.DictReader(lines)
    _require_columns(reader, {"hcat_prefix", "category", "label", "rationale"})
    for row in reader:
        yield HcatRule(
            prefix=row["hcat_prefix"].strip(),
            category=_parse_category(row["category"], row["hcat_prefix"]),
            label=row["label"].strip(),
            rationale=row["rationale"].strip(),
        )


def _read_text(path: str | Path | None, resource: str) -> str:
    """Read a CSV from ``path``, or from the packaged resource if ``path`` is None.

    Args:
        path: Explicit file path, or None for the packaged resource.
        resource: File name inside ``dss/resources``.

    Returns:
        The file content.
    """
    if path is None:
        return (resources.files("dss") / "resources" / resource).read_text(
            encoding="utf-8"
        )
    return Path(path).read_text(encoding="utf-8")


def _require_columns(reader: csv.DictReader[str], required: set[str]) -> None:
    """Raise if a CSV lacks required columns.

    Raises:
        DataValidationError: If a required column is missing.
    """
    missing = required - set(reader.fieldnames or ())
    if missing:
        raise DataValidationError(f"CSV lacks columns: {sorted(missing)}.")


def _parse_category(token: str, context: str) -> CropCategory | None:
    """Parse a category token from a CSV row.

    Args:
        token: Archetype value (e.g. ``"10"``) or ``EXCLUDED``.
        context: Row identifier used in the error message.

    Returns:
        The archetype, or ``None`` for ``EXCLUDED``.

    Raises:
        DataValidationError: If the token is unknown.
    """
    token = token.strip()
    if token == EXCLUDED_TOKEN:
        return None
    try:
        return CropCategory(token)
    except ValueError:
        valid = sorted({member.value for member in CropCategory} | {EXCLUDED_TOKEN})
        raise DataValidationError(
            f"Unknown category {token!r} for {context!r}; expected one of {valid}."
        ) from None


def _normalise_label(label: str) -> str:
    """Normalise a national crop label for matching (NFC, trimmed, upper case)."""
    return unicodedata.normalize("NFC", label).strip().upper()


def _normalise_code(code: object) -> str | None:
    """Return a clean digit string, or ``None`` for a missing code.

    Args:
        code: Raw HCAT code as read from the source layer.

    Returns:
        The stripped code, or ``None`` if it is missing or blank.
    """
    if code is None or code is pd.NA:
        return None
    if isinstance(code, float) and math.isnan(code):
        return None
    text = str(code).strip()
    return text or None
