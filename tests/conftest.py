# Copyright (c) 2026 Hao Ni. All rights reserved.
"""Shared pytest fixtures."""

from __future__ import annotations

import pytest

from dss.taxonomy import HcatClassifier


@pytest.fixture(scope="session")
def classifier() -> HcatClassifier:
    """Classifier built from the packaged HCAT mapping."""
    return HcatClassifier.from_csv()
