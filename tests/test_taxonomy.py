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
    LabelOverride,
    LabelOverrides,
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


@pytest.fixture
def overrides():
    return LabelOverrides.from_csv()


class TestLabelOverrides:
    def test_packaged_table_loads_with_rationales(self, overrides):
        assert len(overrides.overrides) >= 1
        assert all(o.rationale for o in overrides.overrides)

    def test_packaged_corrections(self, overrides, classifier):
        cases = [
            ("3303030000", "PINHÃO", None),
            ("3303030000", "NOZ", CropCategory.ORCHARDS),  # same code, untouched
            ("3302000000", "MEDRONHO", CropCategory.ORCHARDS),
            ("3301070000", "OUTRAS LEGUMINOSAS SECAS", CropCategory.FALLBACK),
            ("3303030000", "AMENDOIM", CropCategory.FALLBACK),
        ]
        codes = pd.Series([c for c, _, _ in cases], dtype="string")
        labels = pd.Series([lbl for _, lbl, _ in cases])
        result = overrides.apply(codes, labels, classifier.classify_series(codes))
        assert result.tolist() == [expected for _, _, expected in cases]

    def test_label_matching_ignores_case_whitespace_and_unicode_form(
        self, overrides, classifier
    ):
        decomposed = unicodedata.normalize("NFD", " pinhão ")
        codes = pd.Series(["3303030000"], dtype="string")
        result = overrides.apply(
            codes, pd.Series([decomposed]), classifier.classify_series(codes)
        )
        assert result.tolist() == [None]

    def test_missing_label_is_left_unchanged(self, overrides, classifier):
        codes = pd.Series(["3303030000"], dtype="string")
        result = overrides.apply(
            codes, pd.Series([None]), classifier.classify_series(codes)
        )
        assert result.tolist() == [CropCategory.ORCHARDS]

    def test_empty_table_changes_nothing(self, classifier):
        codes = pd.Series(["3303030000"], dtype="string")
        before = classifier.classify_series(codes)
        after = LabelOverrides().apply(codes, pd.Series(["PINHÃO"]), before)
        assert after.tolist() == before.tolist()

    def test_duplicate_key_rejected(self):
        override = LabelOverride("3303030000", "PINHÃO", None, "x")
        with pytest.raises(DataValidationError, match="Duplicate"):
            LabelOverrides([override, override])

    def test_malformed_code_rejected(self):
        with pytest.raises(DataValidationError, match="10 digits"):
            LabelOverrides([LabelOverride("3303", "PINHÃO", None, "x")])

    def test_unknown_category_rejected(self, tmp_path):
        path = tmp_path / "overrides.csv"
        path.write_text(
            "hcat_code,original_label,category,rationale\n3303030000,X,99,y\n",
            encoding="utf-8",
        )
        with pytest.raises(DataValidationError, match="Unknown category"):
            LabelOverrides.from_csv(path)
