"""Tests for pb_fictif — PR #23 Bug C (PB public-domain enforcement)."""

from __future__ import annotations

import pytest
from shapely.geometry import Point, Polygon

from auvergne_pipeline.pb_fictif import _ensure_pb_on_public_domain


def test_pb_re_snapped_to_public_domain():
    """Bug C : a PB placed in private land must re-snap to public within 50 m."""
    pb = Point(15, 5)  # in private land
    parc_pub_union = Polygon(
        [(0, 0), (10, 0), (10, 10), (0, 10)]
    )  # public on the left
    ign_routes_buffered = Polygon()  # no IGN routes
    result, flag = _ensure_pb_on_public_domain(
        pb, parc_pub_union, ign_routes_buffered, re_snap_radius_m=50.0
    )
    assert flag is None  # re-snap succeeded
    assert result.x == pytest.approx(10.0, abs=0.5)  # snapped onto the boundary
