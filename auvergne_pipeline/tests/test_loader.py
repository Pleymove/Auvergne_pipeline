"""Tests for loader — PR #23 Bug B (BT clip to public domain)."""

from __future__ import annotations

import geopandas as gpd
import pytest
from shapely.geometry import LineString, Polygon

from auvergne_pipeline import config, loader


def test_bt_clipped_to_public_domain():
    """Bug B : a BT segment entirely inside a private parcel must be dropped."""
    bt = gpd.GeoDataFrame(
        {"geometry": [LineString([(50, 50), (60, 60)])]},
        crs=config.PROJECT_CRS,
    )
    parc_pub = gpd.GeoDataFrame(
        {"geometry": [Polygon([(0, 0), (10, 0), (10, 10), (0, 10)])]},
        crs=config.PROJECT_CRS,
    )
    ign = gpd.GeoDataFrame(geometry=[], crs=config.PROJECT_CRS)
    result = loader.filter_bt_to_public_domain(bt, parc_pub, ign)
    assert result.empty


def test_bt_clipped_at_public_private_boundary():
    """Bug B : a BT segment half public half private must be clipped."""
    bt = gpd.GeoDataFrame(
        {"geometry": [LineString([(0, 5), (20, 5)])]},
        crs=config.PROJECT_CRS,
    )
    parc_pub = gpd.GeoDataFrame(
        {"geometry": [Polygon([(0, 0), (10, 0), (10, 10), (0, 10)])]},
        crs=config.PROJECT_CRS,
    )
    ign = gpd.GeoDataFrame(geometry=[], crs=config.PROJECT_CRS)
    result = loader.filter_bt_to_public_domain(bt, parc_pub, ign)
    assert not result.empty
    assert result.geometry.iloc[0].length == pytest.approx(10.0, abs=0.5)
