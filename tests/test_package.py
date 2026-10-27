# Copyright (c) 2026 Hao Ni. All rights reserved.
"""Tests for the package's public interface."""

from __future__ import annotations

import pytest

import dss


def test_spatial_classes_are_imported_lazily():
    from dss.spatial import ParcelCluster, ParcelLayer

    assert dss.ParcelLayer is ParcelLayer
    assert dss.ParcelCluster is ParcelCluster


def test_unknown_attribute_raises():
    with pytest.raises(AttributeError, match="no attribute"):
        _ = dss.does_not_exist


def test_public_names_resolve():
    for name in dss.__all__:
        assert getattr(dss, name) is not None
