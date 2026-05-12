"""PR #29 A4 — PB classification 4 statuses.

Covers the new ``_classify_pb_placement`` helper and verifies the
backward-compatible ``_ensure_pb_on_public_domain`` wrapper still passes
the PR23 unit-test contract.
"""

from __future__ import annotations

import pytest
from shapely.geometry import Point, Polygon

from auvergne_pipeline import pb_fictif
from auvergne_pipeline.pb_fictif import (
    PB_PLACEMENT_INCERTAIN,
    PB_PLACEMENT_PRIVE,
    PB_PLACEMENT_PUBLIC_PARCELLE,
    PB_PLACEMENT_PUBLIC_ROUTE_BUFFER,
    _classify_pb_placement,
    _ensure_pb_on_public_domain,
)


def test_pb_directly_on_parcelle_publique_is_PUBLIC_PARCELLE():
    pb = Point(5, 5)
    parc_pub = Polygon([(0, 0), (10, 0), (10, 10), (0, 10)])
    out, status = _classify_pb_placement(pb, parc_pub, None)
    assert status == PB_PLACEMENT_PUBLIC_PARCELLE
    assert out.equals(pb)  # geometry unchanged


def test_pb_on_route_buffer_is_snapped_to_public_geometry():
    """PR #29 A4: a PB only accepted via the IGN buffer must be snapped to
    public geometry (parcelle publique or buffer boundary), never left
    floating in private land inside an arbitrary buffer.
    """
    pb = Point(15, 5)
    parc_pub = Polygon([(0, 0), (10, 0), (10, 10), (0, 10)])  # left half
    # IGN buffer wraps the road around y=5, x in [12, 30] — a thick band
    ign_buf = Polygon([(12, 0), (30, 0), (30, 10), (12, 10)])
    out, status = _classify_pb_placement(pb, parc_pub, ign_buf)
    assert status == PB_PLACEMENT_PUBLIC_ROUTE_BUFFER, status
    # PB must have been snapped to a real public reference (boundary or parcelle).
    # The original PB sat in the middle of the buffer at x=15, y=5; after snap
    # the result must lie on the buffer boundary or parcelle publique.
    on_parc = parc_pub.buffer(0.5).covers(out)
    on_boundary = ign_buf.boundary.buffer(0.5).covers(out)
    assert on_parc or on_boundary, (
        f"snapped PB should be on parcelle publique or IGN boundary, got {out.wkt}"
    )


def test_pb_in_private_land_is_PRIVE_when_resnap_fails():
    pb = Point(1000, 1000)
    parc_pub = Polygon([(0, 0), (10, 0), (10, 10), (0, 10)])
    out, status = _classify_pb_placement(pb, parc_pub, None, re_snap_radius_m=10.0)
    assert status == PB_PLACEMENT_PRIVE
    assert out.equals(pb)


def test_pb_no_public_reference_is_INCERTAIN():
    pb = Point(0, 0)
    out, status = _classify_pb_placement(pb, None, None)
    assert status == PB_PLACEMENT_INCERTAIN
    assert out.equals(pb)


def test_pb_resnap_close_to_parcelle_publique_promotes_to_PARCELLE():
    """PB just outside the parcel but within 50 m re-snap → snapped onto
    the parcelle publique boundary, classified PUBLIC_PARCELLE.
    """
    pb = Point(15, 5)  # 5 m east of the parcel below
    parc_pub = Polygon([(0, 0), (10, 0), (10, 10), (0, 10)])
    out, status = _classify_pb_placement(pb, parc_pub, None, re_snap_radius_m=50.0)
    assert status == PB_PLACEMENT_PUBLIC_PARCELLE
    assert parc_pub.buffer(0.5).covers(out)


# ---------------------------------------------------------------------------
# Backwards-compatible wrapper still satisfies the PR23 contract
# ---------------------------------------------------------------------------


def test_ensure_pb_on_public_domain_wrapper_still_works():
    """The historical wrapper used by older tests must keep returning
    (geom, None) for a successful re-snap and (geom, "PB_PLACEMENT_PRIVE")
    on failure.
    """
    # Successful re-snap (5 m away from a parcelle publique)
    pb = Point(15, 5)
    parc_pub = Polygon([(0, 0), (10, 0), (10, 10), (0, 10)])
    out, flag = _ensure_pb_on_public_domain(pb, parc_pub, Polygon(), re_snap_radius_m=50.0)
    assert flag is None
    assert out.x == pytest.approx(10.0, abs=0.5)

    # Failed re-snap → PB_PLACEMENT_PRIVE
    pb_far = Point(1000, 1000)
    out2, flag2 = _ensure_pb_on_public_domain(
        pb_far, parc_pub, Polygon(), re_snap_radius_m=50.0,
    )
    assert flag2 == PB_PLACEMENT_PRIVE
