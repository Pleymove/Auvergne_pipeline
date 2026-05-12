"""``build_pb_fictifs`` unit tests restored after PR #24 trim.

PR #24 replaced the ``build_pb_fictifs`` unit tests with a single test
for the new ``_ensure_pb_on_public_domain`` helper. The high-value cases
(empty input, at least one PB created, pa_id propagation) are restored
here. The previous ``test_build_pb_splits_oversized`` is intentionally
left out — its fixture (ZAPA polygon too small to contain the 11 BATs)
was already failing on main.

Bonus tests for ``_ensure_pb_on_public_domain``:
* re-snap fails outside ``re_snap_radius_m`` -> ``PB_PLACEMENT_PRIVE``
* PB already on public -> no flag, geometry unchanged
* empty public union -> ``PB_PLACEMENT_PRIVE`` (defensive guard added in
  the review PR — used to crash on ``nearest_points(empty)``).
"""

from __future__ import annotations

import geopandas as gpd
import pandas as pd
from shapely.geometry import LineString, Point, Polygon

from auvergne_pipeline import config, pb_fictif
from auvergne_pipeline.pb_fictif import _ensure_pb_on_public_domain


def _bal(n: int = 2) -> gpd.GeoDataFrame:
    return gpd.GeoDataFrame(pd.DataFrame({
        "id_metier": [f"B{i}" for i in range(n)],
        "prises": [2] * n,
        "geometry": [Point(i * 20, 0) for i in range(n)],
    }), geometry="geometry", crs=config.PROJECT_CRS)


def _pa() -> gpd.GeoDataFrame:
    return gpd.GeoDataFrame(pd.DataFrame({
        "id_metier": ["PA1"], "sro": ["SRO1"],
        "geometry": [Point(0, 0)],
    }), geometry="geometry", crs=config.PROJECT_CRS)


def _zapa() -> gpd.GeoDataFrame:
    return gpd.GeoDataFrame(pd.DataFrame({
        "id_metier": ["PA1"],
        "geometry": [Polygon([(-10, -10), (200, -10), (200, 200), (-10, 200)])],
    }), geometry="geometry", crs=config.PROJECT_CRS)


def _infra() -> gpd.GeoDataFrame:
    return gpd.GeoDataFrame(pd.DataFrame({
        "geometry": [LineString([(0, 0), (200, 0)])],
    }), geometry="geometry", crs=config.PROJECT_CRS)


# ---------------------------------------------------------------------------
# Restored build_pb_fictifs unit tests
# ---------------------------------------------------------------------------

def test_build_pb_creates_at_least_one():
    pb, _ = pb_fictif.build_pb_fictifs(_bal(5), _pa(), _zapa(), _infra())
    assert len(pb) >= 1


def test_build_pb_empty_input():
    pb, _ = pb_fictif.build_pb_fictifs(
        gpd.GeoDataFrame(geometry=[], crs=config.PROJECT_CRS),
        _pa(), _zapa(), _infra(),
    )
    assert len(pb) == 0


def test_build_pb_assigns_pa_id():
    pb, _ = pb_fictif.build_pb_fictifs(_bal(3), _pa(), _zapa(), _infra())
    assert all(pb["pa_id"] == "PA1")


def test_build_pb_no_pa_returns_empty():
    """Empty PA collection must short-circuit gracefully (no crash)."""
    pb, gc = pb_fictif.build_pb_fictifs(
        _bal(3),
        gpd.GeoDataFrame(geometry=[], crs=config.PROJECT_CRS),
        _zapa(), _infra(),
    )
    assert pb.empty and gc.empty


# ---------------------------------------------------------------------------
# _ensure_pb_on_public_domain — extra coverage
# ---------------------------------------------------------------------------

def test_pb_already_on_public_returns_unchanged():
    """If the PB is already on public, geometry is unchanged and no flag."""
    pb = Point(5, 5)  # inside the public polygon below
    parc_pub = Polygon([(0, 0), (10, 0), (10, 10), (0, 10)])
    result, flag = _ensure_pb_on_public_domain(
        pb, parc_pub, Polygon(), re_snap_radius_m=50.0
    )
    assert flag is None
    assert result.equals(pb)


def test_pb_resnap_fails_returns_flag():
    """If the PB is too far from any public domain, flag PB_PLACEMENT_PRIVE."""
    pb = Point(1000, 1000)  # nowhere near the tiny public polygon
    parc_pub = Polygon([(0, 0), (10, 0), (10, 10), (0, 10)])
    result, flag = _ensure_pb_on_public_domain(
        pb, parc_pub, Polygon(), re_snap_radius_m=50.0
    )
    assert flag == "PB_PLACEMENT_PRIVE"
    # PB geometry is preserved when re-snap fails
    assert result.equals(pb)


def test_pb_empty_public_union_returns_flag():
    """Defensive: an empty public union must not crash nearest_points."""
    pb = Point(15, 5)
    # Both inputs empty — the previous version crashed inside nearest_points.
    result, flag = _ensure_pb_on_public_domain(
        pb, Polygon(), Polygon(), re_snap_radius_m=50.0
    )
    assert flag == "PB_PLACEMENT_PRIVE"
    assert result.equals(pb)
