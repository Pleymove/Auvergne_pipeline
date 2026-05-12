"""Tests for routing._explode_to_linestrings (PR #15 regression)."""

from __future__ import annotations

from shapely.geometry import LineString, MultiLineString

from auvergne_pipeline.routing import _explode_to_linestrings


def test_explode_handles_linestring():
    line = LineString([(0, 0), (1, 1)])
    parts = list(_explode_to_linestrings(line))
    assert len(parts) == 1
    assert parts[0].equals(line)


def test_explode_handles_multilinestring():
    """Regression: NotImplementedError on MultiLineString .coords (PR #15)."""
    multi = MultiLineString([[(0, 0), (1, 1)], [(2, 2), (3, 3)]])
    parts = list(_explode_to_linestrings(multi))
    assert len(parts) == 2


def test_explode_empty_and_none():
    assert list(_explode_to_linestrings(None)) == []
    empty = LineString()
    assert list(_explode_to_linestrings(empty)) == []
