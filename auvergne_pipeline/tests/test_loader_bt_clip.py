"""Extra coverage for ``loader.filter_bt_to_public_domain`` (PR #23 Bug B).

The PR #24 commit added two unit tests in ``test_loader.py`` (entirely
private BT, half-public-half-private). This file rounds out the corner
cases: empty input, missing public domain, IGN-buffer-only public, and
sub-0.5 m residual-segment drop.
"""

from __future__ import annotations

import geopandas as gpd
from shapely.geometry import LineString, Polygon

from auvergne_pipeline import config, loader


def _gdf(geoms: list, crs: str = config.PROJECT_CRS) -> gpd.GeoDataFrame:
    return gpd.GeoDataFrame({"geometry": geoms}, crs=crs)


def test_empty_bt_input_returns_empty():
    """An empty BT GeoDataFrame must short-circuit and return empty."""
    bt = gpd.GeoDataFrame(geometry=[], crs=config.PROJECT_CRS)
    parc = _gdf([Polygon([(0, 0), (10, 0), (10, 10), (0, 10)])])
    ign = gpd.GeoDataFrame(geometry=[], crs=config.PROJECT_CRS)
    out = loader.filter_bt_to_public_domain(bt, parc, ign)
    assert out.empty


def test_no_public_domain_returns_empty():
    """If neither parcelles publiques nor IGN routes, BT is fully dropped."""
    bt = _gdf([LineString([(0, 0), (100, 0)])])
    parc = gpd.GeoDataFrame(geometry=[], crs=config.PROJECT_CRS)
    ign = gpd.GeoDataFrame(geometry=[], crs=config.PROJECT_CRS)
    out = loader.filter_bt_to_public_domain(bt, parc, ign)
    assert out.empty


def test_ign_route_buffer_keeps_bt():
    """A BT segment running along an IGN route must be kept (within buffer)."""
    # BT runs along Y=2; IGN route along Y=0. Buffer 5 m -> includes Y=2.
    bt = _gdf([LineString([(0, 2), (50, 2)])])
    parc = gpd.GeoDataFrame(geometry=[], crs=config.PROJECT_CRS)
    ign = _gdf([LineString([(0, 0), (50, 0)])])
    out = loader.filter_bt_to_public_domain(bt, parc, ign, buffer_m=5.0)
    assert not out.empty
    # Length preserved: should be close to original 50 m
    assert abs(out.geometry.iloc[0].length - 50.0) < 0.5


def test_residual_under_0_5m_dropped():
    """Tiny BT segments left behind by the clip (< 0.5 m) must be dropped."""
    # BT crosses public/private boundary at x=10; segment fully inside
    # public is (0,0)->(10,0) (length 10). The leftover segment after clip
    # against public would be exactly 0 m (boundary), but make the public
    # polygon slip 0.3m past the edge — clipped residuals shorter than 0.5m
    # must be removed.
    bt = _gdf([LineString([(0, 0), (10.3, 0)])])
    parc = _gdf([Polygon([(0, -1), (10, -1), (10, 1), (0, 1)])])
    ign = gpd.GeoDataFrame(geometry=[], crs=config.PROJECT_CRS)
    out = loader.filter_bt_to_public_domain(bt, parc, ign)
    # The one kept segment is the public part (~10 m). No tiny residual.
    assert all(out.geometry.length > 0.5)
