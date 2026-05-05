"""Tests for pb_fictif (PR #14)."""

from __future__ import annotations

import geopandas as gpd
import pandas as pd
from shapely.geometry import LineString, Point, Polygon

from auvergne_pipeline import config, flags as flags_mod, pb_fictif


def _bal(mini: int = 2) -> gpd.GeoDataFrame:
    return gpd.GeoDataFrame(pd.DataFrame({
        "id_metier": [f"B{i}" for i in range(mini)],
        "prises": [2] * mini,
        "geometry": [Point(i * 20, 0) for i in range(mini)],
    }), geometry="geometry", crs=config.PROJECT_CRS)


def _pa() -> gpd.GeoDataFrame:
    return gpd.GeoDataFrame(pd.DataFrame({
        "id_metier": ["PA1"], "sro": ["SRO1"],
        "geometry": [Point(0, 0)],
    }), geometry="geometry", crs=config.PROJECT_CRS)


def _zapa() -> gpd.GeoDataFrame:
    return gpd.GeoDataFrame(pd.DataFrame({
        "id_metier": ["PA1"],
        "geometry": [Polygon([(-10, -10), (50, -10), (50, 50), (-10, 50)])],
    }), geometry="geometry", crs=config.PROJECT_CRS)


def _infra() -> gpd.GeoDataFrame:
    return gpd.GeoDataFrame(pd.DataFrame({
        "geometry": [LineString([(0, 0), (100, 0)])],
    }), geometry="geometry", crs=config.PROJECT_CRS)


def test_build_pb_creates_at_least_one():
    pb, gc = pb_fictif.build_pb_fictifs(_bal(5), _pa(), _zapa(), _infra())
    assert len(pb) >= 1


def test_build_pb_empty_input():
    pb, gc = pb_fictif.build_pb_fictifs(
        gpd.GeoDataFrame(geometry=[], crs=config.PROJECT_CRS),
        _pa(), _zapa(), _infra(),
    )
    assert len(pb) == 0


def test_build_pb_assigns_pa_id():
    pb, _ = pb_fictif.build_pb_fictifs(_bal(3), _pa(), _zapa(), _infra())
    assert all(pb["pa_id"] == "PA1")


def test_build_pb_splits_oversized():
    """11 BATs with 2 prises each = 22 prises > 10 → should split."""
    bal = _bal(11)
    pb, _ = pb_fictif.build_pb_fictifs(bal, _pa(), _zapa(), _infra())
    # Should create at least 2 PBs (22/10 = ceil 3)
    assert len(pb) >= 2