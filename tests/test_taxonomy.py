# Copyright (c) 2026 Hao Ni. All rights reserved.
"""Unit tests for the HCAT taxonomy mapping (pandas only, no geospatial stack)."""

from __future__ import annotations

import csv
import unicodedata
from pathlib import Path

import pandas as pd
import pytest

from dss.exceptions import DataValidationError
from dss.reference_tables import CROP_ARCHETYPES, CropCategory
from dss.taxonomy import (
    AGRICULTURAL_LAND_COVER,
    HcatClassifier,
    HcatRule,
    is_agricultural,
)

FIXTURES_DIR = Path(__file__).parent / "fixtures"


def _observed_codes() -> list[tuple[str, str, str]]:
    """All 79 HCAT codes found in the Portuguese layer with expected categories."""
    with (FIXTURES_DIR / "observed_hcat_codes.csv").open(encoding="utf-8") as fh:
        return [
            (row["hcat_code"], row["hcat_name"], row["expected_category"])
            for row in csv.DictReader(fh)
        ]


def _expected(token: str) -> CropCategory | None:
    return None if token == "EXCLUDED" else CropCategory(token)


class TestPackagedMapping:
    def test_loads_with_unique_prefixes(self, classifier):
        prefixes = [rule.prefix for rule in classifier.rules]
        assert len(prefixes) == len(set(prefixes)) > 0

    def test_rules_are_ordered_most_specific_first(self, classifier):
        lengths = [len(rule.prefix) for rule in classifier.rules]
        assert lengths == sorted(lengths, reverse=True)

    def test_every_rule_has_a_rationale(self, classifier):
        assert all(rule.rationale for rule in classifier.rules)

    def test_every_archetype_has_reference_parameters(self):
        assert set(CROP_ARCHETYPES) == set(CropCategory)

    @pytest.mark.parametrize(("code", "name", "expected"), _observed_codes())
    def test_observed_codes_regression(self, classifier, code, name, expected):
        """Locks the reviewed assignment of every code present in the dataset."""
        assert classifier.classify(code) == _expected(expected), name

    def test_brief_prefixes_never_occur_in_real_codes(self):
        """Documents why the brief's two-digit prefix table cannot be used."""
        codes = [code for code, _, _ in _observed_codes()]
        assert all(code.startswith("33") for code in codes)
        assert not any(code[:2] in {"10", "20", "30", "50"} for code in codes)


class TestLongestPrefixMatching:
    def test_specific_rule_overrides_parent(self, classifier):
        assert classifier.classify("3301061200") == CropCategory.VEGETABLES  # herbs
        assert classifier.classify("3301060500") == CropCategory.FALLBACK  # sunflower

    def test_excluded_child_of_permanent_crops(self, classifier):
        assert classifier.classify("3303080700") is None  # non-grazable shrubland
        assert classifier.classify("3303050000") == CropCategory.ORCHARDS  # olives

    def test_unseen_code_inherits_branch(self, classifier):
        # A cereal code absent from the 2021 layer still maps via its branch.
        assert classifier.classify("3301010200") == CropCategory.CEREALS

    def test_unmatched_code_falls_back(self, classifier):
        assert classifier.classify("3305000000") == CropCategory.FALLBACK

    @pytest.mark.parametrize("missing", [None, float("nan"), pd.NA, "", "   "])
    def test_missing_code_falls_back(self, classifier, missing):
        assert classifier.classify(missing) == CropCategory.FALLBACK

    def test_series_keeps_missing_distinct_from_excluded(self, classifier):
        codes = pd.Series(["3301010600", None, "3306060000"], dtype="string")
        result = classifier.classify_series(codes)
        assert result.tolist() == [CropCategory.CEREALS, CropCategory.FALLBACK, None]


class TestRuleValidation:
    def test_empty_rules_rejected(self):
        with pytest.raises(DataValidationError):
            HcatClassifier([])

    @pytest.mark.parametrize("prefix", ["", "33a", "33010101010"])
    def test_malformed_prefix_rejected(self, prefix):
        with pytest.raises(DataValidationError):
            HcatClassifier([HcatRule(prefix, CropCategory.CEREALS, "x", "y")])

    def test_duplicate_prefix_rejected(self):
        rule = HcatRule("3301", CropCategory.CEREALS, "x", "y")
        with pytest.raises(DataValidationError):
            HcatClassifier([rule, rule])

    def test_unknown_category_in_csv_rejected(self, tmp_path):
        path = tmp_path / "mapping.csv"
        path.write_text(
            "hcat_prefix,category,label,rationale\n3301,99,x,y\n", encoding="utf-8"
        )
        with pytest.raises(DataValidationError, match="Unknown category"):
            HcatClassifier.from_csv(path)

    def test_missing_csv_column_rejected(self, tmp_path):
        path = tmp_path / "mapping.csv"
        path.write_text("hcat_prefix,category\n3301,10\n", encoding="utf-8")
        with pytest.raises(DataValidationError, match="lacks columns"):
            HcatClassifier.from_csv(path)


class TestAgriculturalFilter:
    def test_land_cover_and_category_both_required(self):
        land_cover = pd.Series(
            [AGRICULTURAL_LAND_COVER, "Superfície florestal", AGRICULTURAL_LAND_COVER]
        )
        categories = pd.Series([CropCategory.PASTURES, CropCategory.PASTURES, None])
        assert is_agricultural(land_cover, categories).tolist() == [True, False, False]

    def test_unicode_normalisation_and_whitespace(self):
        decomposed = " " + unicodedata.normalize("NFD", AGRICULTURAL_LAND_COVER) + " "
        result = is_agricultural(pd.Series([decomposed]), pd.Series(["10"]))
        assert result.tolist() == [True]

    def test_missing_land_cover_is_not_agricultural(self):
        result = is_agricultural(pd.Series([None]), pd.Series([CropCategory.CEREALS]))
        assert result.tolist() == [False]
