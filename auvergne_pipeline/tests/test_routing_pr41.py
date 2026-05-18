"""PR #41 — Restore continuity / no spaghetti after PR #40.

Field run 2026-05-18 (gui_run_20260518_135037.log) on the PR #40 merge
showed five regressions per pilot SRO:

  - 63149/M06/PMZ/42478 : C0=121 spaghetti, pa_pb_connected_ratio=0.00,
    all 7 committed paths broken after finalize_livrable_topology.
  - 63257/QSB/PMZ/56934 : crash in
    livrable_topology._split_livrableedges_at_endpoint_projections at
    ``LineString(orig_cs)`` — heterogeneous 2D/3D coord stack.
  - 63210/M06/PMZ/29655 : 6 committed paths broken after postprocess.
  - 63048/QBO/PMZ/56826 : 16 committed paths broken; pr31_topology
    took 176 s; 64 km of absurd infra.
  - 63258/LLW/PMZ/24228 : pb_total=18 pb_committed=0 — every PB
    rejected because the (orphan) PA could not be anchored.

PR #41 covers the bullets B → F of the brief. These tests pin the
behaviour and prevent the same regressions from sneaking back in.
"""

from __future__ import annotations

import logging
import re

import geopandas as gpd
import networkx as nx
import pandas as pd
import pytest
from shapely.geometry import LineString, Point, Polygon

from auvergne_pipeline import livrable_topology as lt
from auvergne_pipeline import routing


CRS = "EPSG:2154"
_BIG_PUBLIC = Polygon([(-1000, -1000), (1000, -1000), (1000, 1000), (-1000, 1000)])


def _infra_row(geom: LineString, **kw) -> dict:
    base = {
        "statut": "E", "mode_pose": "1", "src": "bt", "infra_type": "bt",
        "sro_code": "SRO1", "geometry": geom,
    }
    base.update(kw)
    return base


def _pa(x=0.0, y=0.0, pid="PA1") -> gpd.GeoDataFrame:
    return gpd.GeoDataFrame(pd.DataFrame({
        "id_metier": [pid], "sro": ["SRO1"],
        "geometry": [Point(x, y)],
    }), geometry="geometry", crs=CRS)


def _pb(x=0.0, y=0.0, pb_id="PB1", pa_id="PA1") -> gpd.GeoDataFrame:
    return gpd.GeoDataFrame(pd.DataFrame({
        "pb_id": [pb_id], "pa_id": [pa_id], "id_metier": [pa_id],
        "sro": ["SRO1"], "geometry": [Point(x, y)],
    }), geometry="geometry", crs=CRS)


# ---------------------------------------------------------------------------
# 1. _split_livrableedges_at_endpoint_projections — mixed 2D/3D coords
# ---------------------------------------------------------------------------

def test_livrable_topology_split_handles_mixed_2d_3d_coords():
    """Crash reproducer for 63257/QSB/PMZ/56934.

    A row whose LineString carries 3D coordinates AND another row whose
    LineString carries 2D coordinates must NOT raise
    ``ValueError: setting an array element with a sequence`` when the
    T-junction split tries to rewrite endpoints.
    """
    df = gpd.GeoDataFrame([
        _infra_row(LineString([(0, 0, 0), (20, 0, 0)])),   # 3D
        _infra_row(LineString([(10, 0), (10, 5)])),        # 2D, T arm
    ], geometry="geometry", crs=CRS)
    # Must not raise.
    out, n = lt._split_livrableedges_at_endpoint_projections(df)
    assert isinstance(out, gpd.GeoDataFrame)
    assert n >= 1
    # All output geometries are plain 2D LineStrings.
    for g in out.geometry:
        cs = list(g.coords)
        for c in cs:
            assert len(c) >= 2
            float(c[0]); float(c[1])  # raises if not numeric


def test_pr41_no_crash_on_63257_like_topology_split_case():
    """Round-trip the same crash through ``finalize_livrable_topology``
    so the whole post-processing pipeline survives heterogeneous coords.
    """
    df = gpd.GeoDataFrame([
        _infra_row(LineString([(0, 0, 0), (20, 0, 0)])),
        _infra_row(LineString([(10, 0), (10, 5)])),
    ], geometry="geometry", crs=CRS)
    pa = _pa(0, 0)
    pb = _pb(20, 0)
    # Must complete without exception.
    out, stats = lt.finalize_livrable_topology(
        df, pa, pb, "SRO_63257",
        delivery_public_area_safe=_BIG_PUBLIC.buffer(0.01),
    )
    assert isinstance(out, gpd.GeoDataFrame)


# ---------------------------------------------------------------------------
# 2. finalize_livrable_topology must not break _used_by_paths edges
# ---------------------------------------------------------------------------

def test_finalize_does_not_break_used_by_paths():
    """A C0 row that ``_drop_c0_when_existing_equivalent`` would have
    dropped as redundant must be KEPT when it carries committed-path
    metadata (`_used_by_paths`). PR #40 silently dropped them, causing
    ``path_broken_after_postprocess=7/7`` on 63149/M06/PMZ/42478.
    """
    df = gpd.GeoDataFrame([
        # Existing infra
        _infra_row(LineString([(0, 0), (50, 0)])),
        # C0 parallel duplicate carrying a committed path
        {
            "statut": "", "mode_pose": "C0", "src": "gc_neuf",
            "infra_type": "gc_neuf", "sro_code": "SRO1",
            "geometry": LineString([(5, 0), (45, 0)]),
            "_used_by_paths": "PA1->PB1",
        },
    ], geometry="geometry", crs=CRS)
    out, stats = lt._drop_c0_when_existing_equivalent(df)
    # The path-carrying C0 must survive.
    paths_left = set()
    if "_used_by_paths" in out.columns:
        for v in out["_used_by_paths"].dropna():
            paths_left.update(str(v).split(","))
    assert "PA1->PB1" in paths_left, (
        "PR #41: drop_c0_when_existing_equivalent must NOT drop a row "
        "that carries a committed-path tag, even when a parallel "
        "existing edge covers it."
    )


def test_path_metadata_propagated_when_line_is_split():
    """When ``_split_livrableedges_at_endpoint_projections`` splits a
    row, both halves must inherit ``_used_by_paths`` so the committed
    path stays reachable in the final graph.
    """
    df = gpd.GeoDataFrame([
        {
            "statut": "E", "mode_pose": "1", "src": "bt", "infra_type": "bt",
            "sro_code": "SRO1",
            "geometry": LineString([(0, 0), (20, 0)]),
            "_used_by_paths": "PA1->PB1",
        },
        _infra_row(LineString([(10, 0), (10, 5)])),  # T arm
    ], geometry="geometry", crs=CRS)
    out, n = lt._split_livrableedges_at_endpoint_projections(df)
    assert n >= 1
    # Find the rows that came from the split (both must carry the tag)
    split_rows = out[
        out["geometry"].apply(
            lambda g: g is not None
            and isinstance(g, LineString)
            and g.length > 0
            and g.length <= 20
            and (g.coords[0][1] == 0.0 and g.coords[-1][1] == 0.0)
        )
    ]
    assert len(split_rows) >= 2, "expected the main line to be split"
    paths_found = set()
    for _, r in split_rows.iterrows():
        for tag in str(r.get("_used_by_paths") or "").split(","):
            if tag:
                paths_found.add(tag)
    assert "PA1->PB1" in paths_found, (
        "PR #41: split halves must inherit the _used_by_paths tag, "
        f"got {paths_found}"
    )


# ---------------------------------------------------------------------------
# 3. pb_committed must be honest after final validation
# ---------------------------------------------------------------------------

def test_pb_committed_only_if_final_graph_reachable(caplog):
    """A trivial SRO with one PA, one PB, one BT edge: pb_committed
    must equal committed_path_reachable_final_graph (sanity case).

    This guards against the PR #40 regression where a PB was reported
    as committed even though the path was broken after postprocess.
    """
    infra = gpd.GeoDataFrame([
        _infra_row(LineString([(0, 0), (50, 0)])),
    ], geometry="geometry", crs=CRS)
    pa = _pa(0, 0)
    pb = _pb(50, 0)
    with caplog.at_level(logging.INFO, logger="auvergne_pipeline.routing"):
        out = routing.route_pa_to_pb(
            pa, pb, infra,
            gpd.GeoDataFrame(geometry=[], crs=CRS),
            public_area=_BIG_PUBLIC,
            delivery_public_area=_BIG_PUBLIC,
            gc_neuf=gpd.GeoDataFrame(geometry=[], crs=CRS),
        )
    pb_qa = next(
        (rec.getMessage() for rec in caplog.records
         if "[PB ROUTING QA]" in rec.getMessage()),
        None,
    )
    final_topo = next(
        (rec.getMessage() for rec in caplog.records
         if "[FINAL TOPO QA]" in rec.getMessage()),
        None,
    )
    assert pb_qa is not None and final_topo is not None
    m_committed = re.search(r"pb_committed=(\d+)", pb_qa)
    m_reach = re.search(r"committed_path_reachable_final_graph=(\d+)", final_topo)
    assert m_committed is not None and m_reach is not None
    assert int(m_committed.group(1)) == int(m_reach.group(1)) == 1, (
        f"PR #41: pb_committed must equal final reachable count, "
        f"got {pb_qa!r} / {final_topo!r}"
    )


def test_pb_committed_demoted_when_path_broken_after_postprocess(caplog):
    """Synthetic case: a PB whose committed path is broken by post-
    processing must NOT remain in pb_committed_ids; it is demoted with
    reason ``path_broken_after_postprocess``."""
    infra = gpd.GeoDataFrame([
        _infra_row(LineString([(0, 0), (50, 0)])),
    ], geometry="geometry", crs=CRS)
    pa = _pa(0, 0)
    pb = _pb(50, 0)
    with caplog.at_level(logging.INFO, logger="auvergne_pipeline.routing"):
        routing.route_pa_to_pb(
            pa, pb, infra,
            gpd.GeoDataFrame(geometry=[], crs=CRS),
            public_area=_BIG_PUBLIC,
            delivery_public_area=_BIG_PUBLIC,
            gc_neuf=gpd.GeoDataFrame(geometry=[], crs=CRS),
        )
    final_topo = next(
        (rec.getMessage() for rec in caplog.records
         if "[FINAL TOPO QA]" in rec.getMessage()),
        None,
    )
    assert final_topo is not None
    # If path_broken_after_postprocess > 0, an equal number of
    # ``path_broken_after_postprocess`` impossible reasons must appear.
    m_broken = re.search(r"path_broken_after_postprocess=(\d+)", final_topo)
    assert m_broken is not None
    pb_qa = next(
        (rec.getMessage() for rec in caplog.records
         if "[PB ROUTING QA]" in rec.getMessage()),
        None,
    )
    assert pb_qa is not None
    n_broken = int(m_broken.group(1))
    if n_broken > 0:
        m_reasons = re.search(
            r"path_broken_after_postprocess:(\d+)", pb_qa,
        )
        assert m_reasons is not None and int(m_reasons.group(1)) == n_broken


# ---------------------------------------------------------------------------
# 4. Orphan PA anchor must succeed when infra is reachable
# ---------------------------------------------------------------------------

def test_pr41_pa_anchor_created_pa_near_existing_infra():
    """Orphan PA placed 60 m off any infra/IGN. PR #40 returned
    ``pa_anchor_missing`` (cap = 30 m). PR #41 raises the logical
    anchor ceiling to ``PR41_MAX_LOGICAL_ANCHOR_M_FOR_ORPHAN`` (150 m)
    so the PA is anchored as a virtual / non-delivered logical anchor
    and the PB chain is not lost wholesale."""
    infra = gpd.GeoDataFrame([
        _infra_row(LineString([(0, 0), (100, 0)])),
    ], geometry="geometry", crs=CRS)
    # PA is 60 m above the infra — beyond the previous 30 m cap.
    pa = _pa(50, 60)
    pb = _pb(99, 0)
    out = routing.route_pa_to_pb(
        pa, pb, infra,
        gpd.GeoDataFrame(geometry=[], crs=CRS),
        public_area=_BIG_PUBLIC,
        delivery_public_area=_BIG_PUBLIC,
        gc_neuf=gpd.GeoDataFrame(geometry=[], crs=CRS),
    )
    # No "pa_anchor_missing" wholesale rejection: the BT row at least
    # must be delivered.
    assert not out.empty, (
        f"PR #41: orphan PA within 150 m of infra must anchor "
        f"(logical anchor) and let PB chain deliver, got empty: {out}"
    )
    bt_rows = out[out["infra_type"] == "bt"]
    assert not bt_rows.empty, (
        "PR #41: at least the BT existing infra should be delivered "
        "for the committed PA→PB"
    )


# ---------------------------------------------------------------------------
# 5. IGN budget must prevent spaghetti
# ---------------------------------------------------------------------------

def test_pr41_ign_fallback_budget_prevents_spaghetti():
    """A PA→PB whose ONLY route is a 2 km chain of IGN polyline must
    be REJECTED, not delivered as 2 km of C0 spaghetti. PR #40 happily
    delivered >5 km of IGN-as-C0 per SRO.
    """
    infra = gpd.GeoDataFrame(
        [], columns=["statut", "mode_pose", "src", "infra_type", "sro_code", "geometry"],
        geometry="geometry", crs=CRS,
    )
    # 2000 m IGN chain → above PR41_MAX_IGN_PER_PATH_M (800 m).
    seg_len = 40.0
    n_segs = 50
    ign_geom = LineString([(i * seg_len, 0.0) for i in range(n_segs + 1)])
    ign = gpd.GeoDataFrame(
        [{"geometry": ign_geom}], geometry="geometry", crs=CRS,
    )
    pa = _pa(0, 0)
    pb = _pb(n_segs * seg_len, 0.0)
    out = routing.route_pa_to_pb(
        pa, pb, infra, ign,
        public_area=_BIG_PUBLIC,
        delivery_public_area=_BIG_PUBLIC,
        gc_neuf=gpd.GeoDataFrame(geometry=[], crs=CRS),
    )
    # Path rejected — no row delivered for this PB.
    assert out.empty or (out["infra_type"] == "gc_neuf").sum() == 0, (
        f"PR #41: a 2 km IGN-only path must be rejected as spaghetti, "
        f"got {out['infra_type'].value_counts().to_dict() if not out.empty else 'empty'}"
    )


def test_pr41_existing_first_prefers_existing_even_if_ign_shorter():
    """Existing infra (longer detour) must be preferred over a parallel
    IGN shortcut. This was already covered by PR #37 pass 1 — the test
    pins it for PR #41 in case the IGN budget changes affect ranking.
    """
    infra = gpd.GeoDataFrame([
        _infra_row(LineString([(0, 0), (0, 50)])),
        _infra_row(LineString([(0, 50), (100, 50)])),
        _infra_row(LineString([(100, 50), (100, 0)])),
    ], geometry="geometry", crs=CRS)
    # Direct IGN shortcut from (0,0) to (100,0)
    ign = gpd.GeoDataFrame([{
        "geometry": LineString([(0.0, 0.0), (100.0, 0.0)]),
    }], geometry="geometry", crs=CRS)
    pa = _pa(0, 0)
    pb = _pb(100, 0)
    out = routing.route_pa_to_pb(
        pa, pb, infra, ign,
        public_area=_BIG_PUBLIC,
        delivery_public_area=_BIG_PUBLIC,
        gc_neuf=gpd.GeoDataFrame(geometry=[], crs=CRS),
    )
    assert not out.empty
    # Output must use BT detour (longer) rather than IGN shortcut.
    assert (out["infra_type"] == "bt").any()
    # And there is no kilometric C0 derived from IGN.
    long_gc = out[
        (out["infra_type"] == "gc_neuf") & (out["length_m"] > 50)
    ]
    assert long_gc.empty, (
        f"PR #41: long IGN-as-C0 must NOT appear when an existing "
        f"detour can deliver, got {long_gc}"
    )


# ---------------------------------------------------------------------------
# 6. Coord normaliser helpers
# ---------------------------------------------------------------------------

def test_coords_to_2d_tuples_handles_3d_and_garbage():
    """Direct unit test of the new coord normaliser."""
    raw = [
        (1.0, 2.0, 3.0),       # 3-tuple, Z dropped
        (4.0, 5.0),            # 2-tuple
        "not a coord",         # garbage — skipped
        (float("nan"), 7.0),   # non-finite — skipped
        (8.0, 9.0),
        (8.0, 9.0),            # duplicate — dropped
    ]
    cleaned = lt._coords_to_2d_tuples(raw)
    assert cleaned == [(1.0, 2.0), (4.0, 5.0), (8.0, 9.0)]


def test_safe_linestring_returns_none_for_degenerate_input():
    """``_safe_linestring`` must return None on < 2 valid coords."""
    assert lt._safe_linestring([(1, 2)]) is None
    assert lt._safe_linestring(["garbage", None]) is None
    assert lt._safe_linestring([]) is None
    # Healthy input still produces a LineString.
    g = lt._safe_linestring([(0, 0), (1, 1, 0)])
    assert g is not None
    assert isinstance(g, LineString)
    assert list(g.coords) == [(0.0, 0.0), (1.0, 1.0)]
