"""Tests for routing (PR #14)."""

from __future__ import annotations

import geopandas as gpd
import pandas as pd
from shapely.geometry import LineString, Point

from auvergne_pipeline import config, routing


def _straight_infra() -> gpd.GeoDataFrame:
    return gpd.GeoDataFrame(pd.DataFrame({
        "statut": ["E"], "mode_pose": ["1"], "src": ["bt"],
        "geometry": [LineString([(0, 0), (100, 0)])],
    }), geometry="geometry", crs=config.PROJECT_CRS)


def _pa_at_pt(x, y, pid="PA1"):
    return gpd.GeoDataFrame(pd.DataFrame({
        "id_metier": [pid], "sro": ["SRO1"],
        "geometry": [Point(x, y)],
    }), geometry="geometry", crs=config.PROJECT_CRS)


def _pb_at_pt(x, y, pid="PB1"):
    return gpd.GeoDataFrame(pd.DataFrame({
        "pb_id": [pid], "pa_id": ["PA1"],
        "geometry": [Point(x, y)],
    }), geometry="geometry", crs=config.PROJECT_CRS)


def test_route_simple_straight_line():
    """PA at (0,0), PB at (100,0), straight infra → 1 edge."""
    routed = routing.route_pa_to_pb(
        _pa_at_pt(0, 0), _pb_at_pt(100, 0),
        _straight_infra(),
        gpd.GeoDataFrame(geometry=[], crs=config.PROJECT_CRS),
    )
    assert len(routed) >= 1


def test_route_empty_pa_returns_empty():
    routed = routing.route_pa_to_pb(
        gpd.GeoDataFrame(geometry=[], crs=config.PROJECT_CRS),
        _pb_at_pt(0, 0), _straight_infra(),
        gpd.GeoDataFrame(geometry=[], crs=config.PROJECT_CRS),
    )
    assert len(routed) == 0


def test_route_disconnected_pa_no_path():
    """PA far from any infra → flag but no crash."""
    import networkx as nx
    routed = routing.route_pa_to_pb(
        _pa_at_pt(1000, 1000, "PA_FAR"), _pb_at_pt(0, 0),
        _straight_infra(),
        gpd.GeoDataFrame(geometry=[], crs=config.PROJECT_CRS),
    )
    # PA is too far to snap, should return empty
    assert len(routed) == 0


def test_routed_edges_have_statut_mode_pose():
    routed = routing.route_pa_to_pb(
        _pa_at_pt(0, 0), _pb_at_pt(100, 0),
        _straight_infra(),
        gpd.GeoDataFrame(geometry=[], crs=config.PROJECT_CRS),
    )
    if len(routed) > 0:
        assert "statut" in routed.columns
        assert "mode_pose" in routed.columns