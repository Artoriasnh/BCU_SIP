# Copyright (c) 2026 Hao Ni. All rights reserved.
"""Map EuroCrops HCAT codes to the brief's crop archetypes.

All EuroCrops codes start with "33" (the position of agriculture in the EAGLE
land-cover matrix), so the brief's two-digit prefixes (10/20/30/50) match no
parcel. Instead, the rules in ``resources/hcat_mapping.csv`` map HCAT branches
(code prefixes) to archetypes. The longest matching prefix wins, so a specific
rule can override its parent; unmatched or missing codes fall back to the brief's
"Other / NaN" archetype. See ASSUMPTIONS A1.
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
        if path is None:
            resource = resources.files("dss") / "resources" / _MAPPING_RESOURCE
            text = resource.read_text(encoding="utf-8")
        else:
            text = Path(path).read_text(encoding="utf-8")
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
    required = {"hcat_prefix", "category", "label", "rationale"}
    missing = required - set(reader.fieldnames or ())
    if missing:
        raise DataValidationError(f"Mapping CSV lacks columns: {sorted(missing)}.")
    valid = {member.value for member in CropCategory} | {EXCLUDED_TOKEN}
    for row in reader:
        token = row["category"].strip()
        if token not in valid:
            raise DataValidationError(
                f"Unknown category {token!r} for prefix {row['hcat_prefix']!r}; "
                f"expected one of {sorted(valid)}."
            )
        yield HcatRule(
            prefix=row["hcat_prefix"].strip(),
            category=None if token == EXCLUDED_TOKEN else CropCategory(token),
            label=row["label"].strip(),
            rationale=row["rationale"].strip(),
        )


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
