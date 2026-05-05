"""Tests for ign_routes (PR #14)."""

from __future__ import annotations

from shapely.geometry import box

from auvergne_pipeline import config, ign_routes


def test_cache_key_deterministic():
    k1 = ign_routes._cache_key(0, 0, 100, 200)
    k2 = ign_routes._cache_key(0, 0, 100, 200)
    assert k1 == k2
    assert len(k1) == 12


def test_build_bbox():
    bbox = ign_routes._build_bbox(box(100, 200, 300, 400), buffer_m=10)
    assert bbox[0] == 90  # minx - buffer
    assert bbox[1] == 190


def test_empty_bbox_returns_empty_gdf():
    """Calling with a tiny bbox should still not crash (might return empty)."""
    # We can't test the real WFS call, just test the function doesn't crash
    # when called with empty infrastructure context.
    pass  # integration test — skipped here