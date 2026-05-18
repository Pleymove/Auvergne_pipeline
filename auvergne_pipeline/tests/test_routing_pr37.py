"""PR #37 — Existing-infra gap healing + two-pass Dijkstra + strict C0 provenance.

The PR #36 field test still showed ``pa_pb_connected_ratio=0`` on most
pilote SROs because:
  1. Existing infra has SIG-level micro-gaps / T-junctions / unwelded
     endpoints that Dijkstra cannot cross.
  2. When those gaps existed, the path detoured through IGN or
     gc_neuf, producing C0 spam (``infra=509`` of which ``C0=128`` etc.)
     even though existing infra was right there.
  3. PR #36's QA reported ``connected/disconnected`` from the routing
     stage, not from the final ``livrable_infra``.

PR #37 fixes:
  - ``_heal_existing_infra_topology`` is run BEFORE routing. It snaps
    near-by endpoints into shared coordinates and splits T-junctions
    on EXISTING infra only. No row is added; no C0 is created.
  - Dijkstra runs in TWO passes: pass 1 explores only existing-infra
    edges; pass 2 falls back to IGN / planned gc_neuf only for PBs
    that pass 1 could not reach. Long C0 only appear when truly
    needed.
  - Every C0 row carries a ``_c0_source`` provenance tag (stripped
    before return). [FINAL TOPO QA] exposes two new counters,
    ``long_direct_c0_count`` and ``c0_without_ign_source``, that must
    stay at 0 — they fire when a C0 row > 3 m has no IGN / planned
    upstream.
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
# 1. heal step — micro-gaps in existing infra are repaired without C0
# ---------------------------------------------------------------------------

def test_heal_snaps_close_endpoints_no_c0():
    """Two BT segments separated by 0.3 m must end up sharing a vertex
    after ``_heal_existing_infra_topology``. No new row appended; no
    C0 created."""
    infra = gpd.GeoDataFrame([
        _infra_row(LineString([(0.0, 0.0), (5.0, 0.0)])),
        _infra_row(LineString([(5.3, 0.0), (10.0, 0.0)])),
    ], geometry="geometry", crs=CRS)
    healed = routing._heal_existing_infra_topology(infra)
    # No row added
    assert len(healed) == 2
    # No C0 row inserted
    c0_rows = healed[
        (healed.get("mode_pose", pd.Series(["", ""])) == "C0")
        & (healed.get("src", pd.Series(["", ""])) == "gc_neuf")
    ]
    assert c0_rows.empty
    # The two segments now share an endpoint near x=5
    endpoints: set[tuple[float, float]] = set()
    for g in healed.geometry:
        cs = list(g.coords)
        endpoints.add((round(cs[0][0], 2), round(cs[0][1], 2)))
        endpoints.add((round(cs[-1][0], 2), round(cs[-1][1], 2)))
    # Not both (5.0, 0.0) and (5.3, 0.0) — they were collapsed
    assert not ((5.0, 0.0) in endpoints and (5.3, 0.0) in endpoints), endpoints


def test_heal_splits_t_junction_no_c0():
    """A line ending on the MIDDLE of another (T-junction) must be
    split at the projection. No C0 is produced.
    """
    infra = gpd.GeoDataFrame([
        _infra_row(LineString([(0.0, 0.0), (20.0, 0.0)])),    # main
        _infra_row(LineString([(10.0, 0.0), (10.0, 5.0)])),   # T arm
    ], geometry="geometry", crs=CRS)
    healed = routing._heal_existing_infra_topology(infra)
    # The main line was split at (10, 0)
    endpoints: set[tuple[float, float]] = set()
    for g in healed.geometry:
        cs = list(g.coords)
        endpoints.add((round(cs[0][0], 2), round(cs[0][1], 2)))
        endpoints.add((round(cs[-1][0], 2), round(cs[-1][1], 2)))
    assert (10.0, 0.0) in endpoints
    # No C0 row was inserted
    c0_rows = healed[
        (healed.get("mode_pose", pd.Series(["", "", ""])) == "C0")
        & (healed.get("src", pd.Series(["", "", ""])) == "gc_neuf")
    ]
    assert c0_rows.empty


# ---------------------------------------------------------------------------
# 2. Two-pass Dijkstra — existing path wins when reachable after heal
# ---------------------------------------------------------------------------

def test_existing_path_chosen_when_reachable_after_heal(caplog):
    """An SRO where existing infra has a 0.4 m micro-gap between two
    segments and a parallel IGN exists.

    Without heal, Dijkstra would detour through IGN (cheaper than
    crossing the gap). With PR #37's heal + two-pass, the gap is
    snapped closed and pass 1 finds a fully-existing path. The output
    must contain only BT rows; ``c0_without_ign_source`` and
    ``long_direct_c0_count`` stay at 0.
    """
    infra = gpd.GeoDataFrame([
        _infra_row(LineString([(0.0, 0.0), (5.0, 0.0)])),
        _infra_row(LineString([(5.3, 0.0), (10.0, 0.0)])),
    ], geometry="geometry", crs=CRS)
    # Parallel IGN that Dijkstra would have used to bridge the 0.3 m gap.
    ign = gpd.GeoDataFrame([{
        "geometry": LineString([(0.0, 0.1), (10.0, 0.1)]),
    }], geometry="geometry", crs=CRS)
    pa = _pa(0.0, 0.0, pid="PA1")
    pb = _pb(10.0, 0.0, pb_id="PB1", pa_id="PA1")

    with caplog.at_level(logging.INFO, logger="auvergne_pipeline.routing"):
        out = routing.route_pa_to_pb(
            pa, pb, infra, ign,
            public_area=_BIG_PUBLIC,
            delivery_public_area=_BIG_PUBLIC,
            gc_neuf=gpd.GeoDataFrame(geometry=[], crs=CRS),
        )

    assert not out.empty, "PR #37: PA→PB must deliver an existing-only path"
    # PR #41 — heal closes the gap inside the infra layer, but the
    # subsequent ``_weld_close_nodes`` merges infra and IGN endpoints
    # together when they sit within WELD_RADIUS_M. PA / PB therefore
    # need micro terminal connectors to anchor onto the welded node.
    # The strict assertion becomes: NO **long** gc_neuf row exists.
    gc_rows = out[out["infra_type"] == "gc_neuf"]
    long_gc_rows = gc_rows[gc_rows["length_m"] > 1.0]
    assert long_gc_rows.empty, (
        f"PR #37: heal must avoid long gc_neuf rows on a pure existing "
        f"path, got {long_gc_rows}"
    )
    # And the QA log
    final_topo = [
        rec.getMessage() for rec in caplog.records
        if "[FINAL TOPO QA]" in rec.getMessage()
    ]
    assert final_topo
    line = final_topo[-1]
    ld = re.search(r"long_direct_c0_count=(\d+)", line)
    assert ld is not None and int(ld.group(1)) == 0, line
    cw = re.search(r"c0_without_ign_source=(\d+)", line)
    assert cw is not None and int(cw.group(1)) == 0, line


# ---------------------------------------------------------------------------
# 3. IGN fallback only when truly needed (no existing path)
# ---------------------------------------------------------------------------

def test_ign_fallback_taken_when_no_existing_path():
    """No existing infra at all between PA and PB — pass 1 fails, pass 2
    delivers an IGN-converted-to-C0 path. The output rows are tagged as
    coming from IGN; the QA counters remain healthy.
    """
    infra = gpd.GeoDataFrame(
        [], columns=["statut", "mode_pose", "src", "infra_type", "sro_code", "geometry"],
        geometry="geometry", crs=CRS,
    )
    ign = gpd.GeoDataFrame([{
        "geometry": LineString([(i * 10.0, 0.0) for i in range(6)]),
    }], geometry="geometry", crs=CRS)
    pa = _pa(0.0, 0.0, pid="PA1")
    pb = _pb(50.0, 0.0, pb_id="PB1", pa_id="PA1")

    out = routing.route_pa_to_pb(
        pa, pb, infra, ign,
        public_area=_BIG_PUBLIC,
        delivery_public_area=_BIG_PUBLIC,
        gc_neuf=gpd.GeoDataFrame(geometry=[], crs=CRS),
    )
    assert not out.empty
    # Every delivered row is gc_neuf (IGN-as-C0)
    assert (out["infra_type"] == "gc_neuf").all()


# ---------------------------------------------------------------------------
# 4. Long C0 without IGN source is impossible in the new pipeline
# ---------------------------------------------------------------------------

def test_long_c0_count_and_c0_without_ign_source_stay_zero(caplog):
    """Synthetic SRO: PA → BT(0,0)-(50,0) → PB at (50, 1).

    Standard healthy case. There must be ZERO C0 rows > 3 m without an
    IGN source after PR #37 (everything is either existing infra or a
    short terminal connector ≤ 3 m).
    """
    infra = gpd.GeoDataFrame([
        _infra_row(LineString([(0.0, 0.0), (50.0, 0.0)])),
    ], geometry="geometry", crs=CRS)
    pa = _pa(0.0, 0.0, pid="PA1")
    pb = _pb(50.0, 1.0, pb_id="PB1", pa_id="PA1")
    with caplog.at_level(logging.INFO, logger="auvergne_pipeline.routing"):
        out = routing.route_pa_to_pb(
            pa, pb, infra,
            gpd.GeoDataFrame(geometry=[], crs=CRS),
            public_area=_BIG_PUBLIC,
            delivery_public_area=_BIG_PUBLIC,
            gc_neuf=gpd.GeoDataFrame(geometry=[], crs=CRS),
        )
    assert not out.empty
    final_topo = [
        rec.getMessage() for rec in caplog.records
        if "[FINAL TOPO QA]" in rec.getMessage()
    ]
    assert final_topo
    line = final_topo[-1]
    ld = re.search(r"long_direct_c0_count=(\d+)", line)
    cw = re.search(r"c0_without_ign_source=(\d+)", line)
    assert ld is not None and int(ld.group(1)) == 0, line
    assert cw is not None and int(cw.group(1)) == 0, line


# ---------------------------------------------------------------------------
# 5. _c0_source is not leaked in the GeoDataFrame returned to the writer
# ---------------------------------------------------------------------------

def test_c0_source_column_is_stripped_from_output():
    """The provenance column ``_c0_source`` is for internal QA only;
    it MUST NOT survive in the returned GeoDataFrame (writer / GPKG
    output stays unchanged).
    """
    infra = gpd.GeoDataFrame([
        _infra_row(LineString([(0.0, 0.0), (10.0, 0.0)])),
    ], geometry="geometry", crs=CRS)
    pa = _pa(0.0, 0.0)
    pb = _pb(10.0, 0.0)
    out = routing.route_pa_to_pb(
        pa, pb, infra,
        gpd.GeoDataFrame(geometry=[], crs=CRS),
        public_area=_BIG_PUBLIC,
        delivery_public_area=_BIG_PUBLIC,
        gc_neuf=gpd.GeoDataFrame(geometry=[], crs=CRS),
    )
    assert "_c0_source" not in out.columns


# ---------------------------------------------------------------------------
# 6. Final QA `pa_pb_connected_ratio` reflects the FINAL livrable_infra
# ---------------------------------------------------------------------------

def test_pa_pb_connected_ratio_in_qa_reflects_final_graph(caplog):
    """``[FINAL TOPO QA]`` reports a non-zero ratio when at least one
    PA→PB can be traversed on the FINAL ``livrable_infra`` (the audit
    inside ``finalize_livrable_topology`` rebuilds the graph from the
    delivered rows, not from the upstream routing intermediate).
    """
    infra = gpd.GeoDataFrame([
        _infra_row(LineString([(0.0, 0.0), (50.0, 0.0)])),
    ], geometry="geometry", crs=CRS)
    pa = _pa(0.0, 0.0)
    pb = _pb(50.0, 0.0)
    with caplog.at_level(logging.INFO, logger="auvergne_pipeline.routing"):
        out = routing.route_pa_to_pb(
            pa, pb, infra,
            gpd.GeoDataFrame(geometry=[], crs=CRS),
            public_area=_BIG_PUBLIC,
            delivery_public_area=_BIG_PUBLIC,
            gc_neuf=gpd.GeoDataFrame(geometry=[], crs=CRS),
        )
    assert not out.empty
    final_topo = [
        rec.getMessage() for rec in caplog.records
        if "[FINAL TOPO QA]" in rec.getMessage()
    ]
    assert final_topo
    line = final_topo[-1]
    ratio = re.search(r"pa_pb_connected_ratio=([0-9.]+)", line)
    assert ratio is not None
    assert float(ratio.group(1)) >= 0.99, (
        f"PR #37: a trivial single-PA-PB scenario must report a "
        f"connected ratio close to 1.0, got: {line}"
    )


# ---------------------------------------------------------------------------
# 7. Pass-1 short-circuit: gc_neuf injected is NOT touched when existing
#    infra covers the path
# ---------------------------------------------------------------------------

def test_pass1_avoids_gc_neuf_when_existing_covers(monkeypatch):
    """If a planned gc_neuf is injected but a fully existing path also
    exists, pass 1 must pick the existing path — the gc_neuf row is
    never delivered."""
    G = nx.Graph()
    # Existing detour through real BT
    G.add_edge((0.0, 0.0), (10.0, 0.0),
               length=10, type="infra", src="bt", infra_type="bt",
               statut="E", mode_pose="1",
               geometry=LineString([(0, 0), (10, 0)]))
    G.add_edge((10.0, 0.0), (50.0, 0.0),
               length=40, type="infra", src="bt", infra_type="bt",
               statut="E", mode_pose="1",
               geometry=LineString([(10, 0), (50, 0)]))
    # Planned gc_neuf shortcut (deliverable after PR #36)
    G.add_edge((0.0, 0.0), (25.0, -1.0),
               length=25, type="gc_neuf", src="gc_neuf", infra_type="gc_neuf",
               mode_pose="C0", statut="",
               geometry=LineString([(0, 0), (25, -1)]),
               virtual=False, deliverable=True)
    G.add_edge((25.0, -1.0), (50.0, 0.0),
               length=25, type="gc_neuf", src="gc_neuf", infra_type="gc_neuf",
               mode_pose="C0", statut="",
               geometry=LineString([(25, -1), (50, 0)]),
               virtual=False, deliverable=True)
    monkeypatch.setattr(routing, "_build_graph", lambda *a, **k: G)

    pa = _pa(0.0, 0.0)
    pb = _pb(50.0, 0.0)
    out = routing.route_pa_to_pb(
        pa, pb,
        gpd.GeoDataFrame(geometry=[], crs=CRS),
        gpd.GeoDataFrame(geometry=[], crs=CRS),
        public_area=_BIG_PUBLIC,
        delivery_public_area=_BIG_PUBLIC,
        gc_neuf=gpd.GeoDataFrame(geometry=[], crs=CRS),
    )
    assert not out.empty
    # Output is BT only — gc_neuf was bypassed by pass 1
    gc_rows = out[out["infra_type"] == "gc_neuf"]
    assert gc_rows.empty, (
        f"PR #37: pass 1 must avoid gc_neuf when existing path exists, "
        f"got delivered gc_neuf rows: {gc_rows}"
    )


# ---------------------------------------------------------------------------
# 8. Pass-2 fallback is taken when existing infra alone cannot reach PB
# ---------------------------------------------------------------------------

def test_pass2_taken_when_existing_unreachable(monkeypatch):
    """Existing infra exists but does not reach the PB; the only way
    to reach PB is via a planned gc_neuf. Pass 1 fails, pass 2
    succeeds. The gc_neuf row appears in the output, tagged as
    ``gc_neuf_planned`` upstream (no long_direct_c0 violation).
    """
    G = nx.Graph()
    # Existing BT spans (0,0)-(10,0) only
    G.add_edge((0.0, 0.0), (10.0, 0.0),
               length=10, type="infra", src="bt", infra_type="bt",
               statut="E", mode_pose="1",
               geometry=LineString([(0, 0), (10, 0)]))
    # Planned gc_neuf 10→50 — Dijkstra MUST use it
    G.add_edge((10.0, 0.0), (50.0, 0.0),
               length=40, type="gc_neuf", src="gc_neuf", infra_type="gc_neuf",
               mode_pose="C0", statut="",
               geometry=LineString([(10, 0), (50, 0)]),
               virtual=False, deliverable=True)
    monkeypatch.setattr(routing, "_build_graph", lambda *a, **k: G)

    pa = _pa(0.0, 0.0)
    pb = _pb(50.0, 0.0)
    out = routing.route_pa_to_pb(
        pa, pb,
        gpd.GeoDataFrame(geometry=[], crs=CRS),
        gpd.GeoDataFrame(geometry=[], crs=CRS),
        public_area=_BIG_PUBLIC,
        delivery_public_area=_BIG_PUBLIC,
        gc_neuf=gpd.GeoDataFrame(geometry=[], crs=CRS),
    )
    assert not out.empty
    # Both BT and gc_neuf are present
    assert (out["infra_type"] == "bt").any()
    assert (out["infra_type"] == "gc_neuf").any()


# ---------------------------------------------------------------------------
# 9. Terminal connector ≤ 3 m does not trip the long_direct_c0 audit
# ---------------------------------------------------------------------------

def test_short_terminal_connector_not_counted_as_long_c0(caplog):
    """``_ensure_terminals_connected`` emits a short C0 connector for
    a PA 1 m off the line. The PR #37 audit must NOT count this as a
    ``long_direct_c0`` violation (length ≤ 3 m AND no _c0_source tag —
    treated as legitimate terminal connector)."""
    infra = gpd.GeoDataFrame([
        _infra_row(LineString([(0.0, 0.0), (50.0, 0.0)])),
    ], geometry="geometry", crs=CRS)
    pa = _pa(25.0, 1.0)
    pb = _pb(50.0, 0.0)
    with caplog.at_level(logging.INFO, logger="auvergne_pipeline.routing"):
        routing.route_pa_to_pb(
            pa, pb, infra,
            gpd.GeoDataFrame(geometry=[], crs=CRS),
            public_area=_BIG_PUBLIC,
            delivery_public_area=_BIG_PUBLIC,
            gc_neuf=gpd.GeoDataFrame(geometry=[], crs=CRS),
        )
    final_topo = [
        rec.getMessage() for rec in caplog.records
        if "[FINAL TOPO QA]" in rec.getMessage()
    ]
    assert final_topo
    line = final_topo[-1]
    ld = re.search(r"long_direct_c0_count=(\d+)", line)
    cw = re.search(r"c0_without_ign_source=(\d+)", line)
    assert ld is not None and int(ld.group(1)) == 0, line
    assert cw is not None and int(cw.group(1)) == 0, line
