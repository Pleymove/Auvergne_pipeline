"""PR #36 regression tests — deliverable routing graph + terminal connectivity.

PR #35 made the path-level deliverability check too strict: gc_neuf
injected by ``pb_fictif`` was marked virtual, micro-bridges across
public domain were virtual, and the cumulative IGN cap was a hard
blocker. On the field test of 2026-05-13, this produced infra=0
livrables on full SROs and ``pa_pb_connected_ratio=0`` across all
pilote SROs. PR #36 restores honest connectivity while keeping the
anti-straight-line guards in place.
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


def _row(**kw) -> dict:
    base = {
        "sro": "SRO1", "pa_id": "PA1", "pb_id": "PB1",
        "statut": "E", "mode_pose": "1",
        "infra_type": "bt", "src": "bt",
        "length_m": 0.0, "geometry": None,
    }
    base.update(kw)
    if base["geometry"] is not None and base["length_m"] == 0.0:
        base["length_m"] = base["geometry"].length
    return base


def _df(rows) -> gpd.GeoDataFrame:
    return gpd.GeoDataFrame(rows, geometry="geometry", crs=CRS)


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


class _Flags:
    def __init__(self):
        self.entries: list[dict] = []

    def add(self, flag_type, target_url, message):
        self.entries.append({"type": flag_type, "target": target_url, "msg": message})


# ---------------------------------------------------------------------------
# 1. Terminal connectivity — short C0 connector restored
# ---------------------------------------------------------------------------

def test_pa_and_pb_off_line_get_short_terminal_connector():
    """A PA ~1 m off the livrable line and a PB ~0.5 m off must end up
    both connected. PR #35 silently flagged them as disconnected; PR #36
    restores a short, public C0 terminal connector for each.
    """
    df = _df([_row(geometry=LineString([(0, 0), (10, 0)]))])
    pa = _pa(3.0, 1.0, pid="PA1")
    pb = _pb(7.0, 0.5, pb_id="PB1", pa_id="PA1")

    out, stats = lt.finalize_livrable_topology(
        df, pa, pb, "SRO1",
        delivery_public_area_safe=_BIG_PUBLIC.buffer(0.01),
    )
    assert stats["pa_connected"] >= 1
    assert stats["pb_connected"] >= 1
    assert stats["terminal_connectors_added"] >= 2, (
        f"expected one connector per terminal, got stats={stats}"
    )
    assert stats["terminal_snap_failed"] == 0
    # Both connectors are short
    for g in out.geometry:
        cs = list(g.coords)
        if len(cs) == 2:
            d = Point(cs[0]).distance(Point(cs[-1]))
            assert d <= 5.0  # well under TERMINAL_CONNECTOR_MAX_LENGTH_M + line length


def test_terminal_connector_rejected_when_too_far():
    """A PA at 4 m of the only line exceeds
    ``TERMINAL_CONNECTOR_MAX_LENGTH_M`` (3 m). It is flagged, no
    connector is emitted — PR #36 keeps the anti-straight-line guard.
    """
    df = _df([_row(geometry=LineString([(0, 0), (10, 0)]))])
    pa = _pa(5.0, 4.0, pid="PA1")
    pb = _pb(10.0, 0.0, pb_id="PB1", pa_id="PA1")

    out, stats = lt.finalize_livrable_topology(
        df, pa, pb, "SRO1",
        delivery_public_area_safe=_BIG_PUBLIC.buffer(0.01),
    )
    assert stats["pa_connected"] == 0
    assert stats["terminal_snap_failed"] >= 1
    # And no C0 connector was emitted
    c0_rows = out[(out["mode_pose"] == "C0") & (out["src"] == "gc_neuf")]
    # Only allowed gc_neuf rows are those genuinely produced upstream by
    # pb_fictif; in this synthetic test there are none.
    assert c0_rows.empty


def test_terminal_connector_rejected_when_crosses_private():
    """A PA close enough but the connector would cross private land.
    PR #36 flags and does not emit the C0.
    """
    # Public area only covers y >= 1.5
    public = Polygon([(-10, 1.5), (20, 1.5), (20, 10), (-10, 10)]).buffer(0.01)
    df = _df([_row(geometry=LineString([(0, 2.0), (10, 2.0)]))])
    pa = _pa(5.0, 0.5, pid="PA1")  # 1.5 m below the line, crosses y<1.5
    pb = _pb(9.0, 2.0, pb_id="PB1", pa_id="PA1")

    out, stats = lt.finalize_livrable_topology(
        df, pa, pb, "SRO1",
        delivery_public_area_safe=public,
    )
    assert stats["pa_connected"] == 0
    assert stats["terminal_snap_failed"] >= 1


# ---------------------------------------------------------------------------
# 2. Deliverable-graph routing — alternative path preferred
# ---------------------------------------------------------------------------

def test_existing_path_preferred_over_virtual_when_alternative_exists(monkeypatch):
    """If a path can be made fully deliverable via existing infra,
    Dijkstra must pick it instead of routing through a virtual edge.

    The graph has two disjoint routes between PA and PB: a longer real
    BT detour, and a shorter virtual gc_neuf shortcut. PR #36's
    delivery-weighted Dijkstra MUST take the long deliverable detour,
    not the short virtual shortcut — and the livrable_infra must
    contain the BT edges, not the virtual one.
    """
    G = nx.Graph()
    # Existing BT detour: (0,0) → (25, 10) → (50, 0)
    G.add_edge((0.0, 0.0), (25.0, 10.0),
               length=27, type="infra", src="bt", infra_type="bt",
               statut="E", mode_pose="1",
               geometry=LineString([(0, 0), (25, 10)]))
    G.add_edge((25.0, 10.0), (50.0, 0.0),
               length=27, type="infra", src="bt", infra_type="bt",
               statut="E", mode_pose="1",
               geometry=LineString([(25, 10), (50, 0)]))
    # Virtual gc_neuf shortcut via an intermediate node
    G.add_edge((0.0, 0.0), (25.0, 0.0),
               length=25, type="gc_neuf", src="gc_neuf", infra_type="gc_neuf",
               mode_pose="C0", statut="",
               geometry=LineString([(0, 0), (25, 0)]),
               virtual=True, deliverable=False)
    G.add_edge((25.0, 0.0), (50.0, 0.0),
               length=25, type="gc_neuf", src="gc_neuf", infra_type="gc_neuf",
               mode_pose="C0", statut="",
               geometry=LineString([(25, 0), (50, 0)]),
               virtual=True, deliverable=False)
    monkeypatch.setattr(routing, "_build_graph", lambda *a, **k: G)

    pa = _pa(0, 0)
    pb = _pb(50, 0)
    out = routing.route_pa_to_pb(
        pa, pb,
        gpd.GeoDataFrame(geometry=[], crs=CRS),
        gpd.GeoDataFrame(geometry=[], crs=CRS),
        public_area=_BIG_PUBLIC,
        delivery_public_area=_BIG_PUBLIC,
    )
    assert not out.empty, "expected a delivered path via the existing BT detour"
    # The output must be the BT detour, not the virtual shortcut.
    assert (out["infra_type"] == "bt").any(), (
        f"expected BT in delivered path, got {out['infra_type'].tolist()}"
    )
    # No virtual edge slipped into the livrable
    virtual_rows = out[
        (out["mode_pose"] == "C0") & (out["src"] == "gc_neuf")
    ]
    assert virtual_rows.empty, (
        f"virtual gc_neuf shortcut must not be delivered, got {virtual_rows}"
    )


# ---------------------------------------------------------------------------
# 3. SRO-like — infra not 0 when PBs exist and infra is reachable
# ---------------------------------------------------------------------------

def test_sro_with_existing_infra_close_to_pbs_delivers_non_empty():
    """An SRO with delivered infra near several PBs must NOT come out
    with ``writer infra=0``. PR #35's strict path rejection caused this
    on the field test (4/5 SROs with infra=0 or near-empty).
    """
    # Existing infra spans 100 m
    infra = gpd.GeoDataFrame([{
        "statut": "E", "mode_pose": "1", "src": "bt",
        "infra_type": "bt",
        "geometry": LineString([(0, 0), (100, 0)]),
        "sro_code": "SRO1",
    }], geometry="geometry", crs=CRS)

    # 3 PBs near the infra, each 1 m off
    pa = _pa(5.0, 0.0, pid="PA1")
    pb = gpd.GeoDataFrame([
        {"pb_id": f"PB{i}", "pa_id": "PA1", "id_metier": "PA1",
         "sro": "SRO1", "geometry": Point(xi, 1.0)}
        for i, xi in enumerate([30.0, 60.0, 90.0])
    ], geometry="geometry", crs=CRS)

    out = routing.route_pa_to_pb(
        pa, pb, infra,
        gpd.GeoDataFrame(geometry=[], crs=CRS),
        public_area=_BIG_PUBLIC,
        delivery_public_area=_BIG_PUBLIC,
        gc_neuf=gpd.GeoDataFrame(geometry=[], crs=CRS),
    )
    assert not out.empty, "PR #36: SRO with reachable infra must deliver edges"
    assert float(out["length_m"].sum()) > 0


# ---------------------------------------------------------------------------
# 4. Non-regression: no straight connectors, no virtual delivered,
#    no c0_without_source — AND at least one PA→PB connected
# ---------------------------------------------------------------------------

def test_no_artefacts_and_at_least_one_pa_pb_connected(caplog):
    infra = gpd.GeoDataFrame([{
        "statut": "E", "mode_pose": "1", "src": "bt", "infra_type": "bt",
        "geometry": LineString([(0, 0), (100, 0)]),
        "sro_code": "SRO1",
    }], geometry="geometry", crs=CRS)

    pa = _pa(5.0, 0.0, pid="PA1")
    pb = _pb(95.0, 0.0, pb_id="PB1", pa_id="PA1")

    with caplog.at_level(logging.INFO, logger="auvergne_pipeline.routing"):
        out = routing.route_pa_to_pb(
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
    assert "straight_connectors=0" in line, line
    assert "virtual_delivered=0" in line, line
    assert "c0_without_source_geometry=0" in line, line
    ratio = re.search(r"pa_pb_connected_ratio=([0-9.]+)", line)
    assert ratio is not None
    assert float(ratio.group(1)) > 0.0, (
        f"PR #36: at least one PA→PB must be connected, got: {line}"
    )


# ---------------------------------------------------------------------------
# 5. PA / BAT positions stay immobile
# ---------------------------------------------------------------------------

def test_pa_and_bat_terminals_are_not_moved():
    """``_ensure_terminals_connected`` may split lines and add C0
    connectors, but it must NEVER move the original PA/PB terminal
    geometries themselves.
    """
    df = _df([_row(geometry=LineString([(0, 0), (10, 0)]))])
    pa = _pa(5.0, 1.0, pid="PA1")
    pb = _pb(7.0, 0.5, pb_id="PB1", pa_id="PA1")
    pa_pt_before = (float(pa.iloc[0].geometry.x), float(pa.iloc[0].geometry.y))
    pb_pt_before = (float(pb.iloc[0].geometry.x), float(pb.iloc[0].geometry.y))

    lt.finalize_livrable_topology(
        df, pa, pb, "SRO1",
        delivery_public_area_safe=_BIG_PUBLIC.buffer(0.01),
    )
    pa_pt_after = (float(pa.iloc[0].geometry.x), float(pa.iloc[0].geometry.y))
    pb_pt_after = (float(pb.iloc[0].geometry.x), float(pb.iloc[0].geometry.y))
    assert pa_pt_before == pa_pt_after
    assert pb_pt_before == pb_pt_after


# ---------------------------------------------------------------------------
# 6. Micro-bridge across public domain is DELIVERABLE
# ---------------------------------------------------------------------------

def test_public_micro_bridge_is_deliverable_not_virtual():
    """A ≤3 m micro-bridge whose geometry stays inside the public
    domain becomes a real C0 delivered row (Pierre's brief: "GC neuf
    doit suivre la voirie publique"). The previous PR #33/#35 design
    flagged every micro-bridge as virtual, which made PR #35 reject
    every PA→PB that needed one.
    """
    G = nx.Graph()
    G.add_edge((0.0, 0.0), (5.0, 0.0),
               length=5.0, type="infra", src="ft", infra_type="ft",
               geometry=LineString([(0, 0), (5, 0)]))
    G.add_edge((7.0, 0.0), (12.0, 0.0),
               length=5.0, type="infra", src="ft", infra_type="ft",
               geometry=LineString([(7, 0), (12, 0)]))
    # 2 m gap between components, in public area
    public = Polygon([(-5, -5), (20, -5), (20, 5), (-5, 5)])
    bridged = routing._bridge_components_with_gc_neuf(
        G, (5.0, 0.0), (7.0, 0.0),
        public_area=public,
    )
    assert bridged
    e = G.get_edge_data((5.0, 0.0), (7.0, 0.0))
    assert e is not None
    assert e.get("deliverable") is True
    assert e.get("virtual") is False
    assert e.get("_can_deliver") is True


def test_private_micro_bridge_stays_virtual(monkeypatch):
    """A ≤3 m micro-bridge whose geometry would cross private parcels
    stays virtual: Dijkstra can still use it for connectivity, but the
    path will be rejected by the deliverable-graph check rather than
    drawing a diagonal across a private parcel.
    """
    monkeypatch.setattr(routing, "ALLOW_IGN_ROAD_C0_WITHOUT_PARCEL_GATE", False)
    G = nx.Graph()
    G.add_edge((0.0, 0.0), (5.0, 0.0),
               length=5.0, type="infra", src="ft", infra_type="ft",
               geometry=LineString([(0, 0), (5, 0)]))
    G.add_edge((7.0, 0.0), (12.0, 0.0),
               length=5.0, type="infra", src="ft", infra_type="ft",
               geometry=LineString([(7, 0), (12, 0)]))
    private = Polygon([(-100, -100), (-50, -100), (-50, -50), (-100, -50)])
    bridged = routing._bridge_components_with_gc_neuf(
        G, (5.0, 0.0), (7.0, 0.0),
        public_area=private,
    )
    assert bridged
    e = G.get_edge_data((5.0, 0.0), (7.0, 0.0))
    assert e.get("virtual") is True
    assert e.get("deliverable") is False
    assert e.get("_can_deliver") is False


# ---------------------------------------------------------------------------
# 7. IGN-derived C0 above the cap is delivered with a soft warning
# ---------------------------------------------------------------------------

def test_ign_over_cap_path_rejected_by_per_path_budget(caplog):
    """PR #41 — a path whose IGN-only ratio exceeds
    ``PR41_MAX_IGN_RATIO_PER_PATH`` AND whose total length is above
    ``PR41_RATIO_MIN_TOTAL_M`` is rejected. 500 m of IGN ⇒ ratio = 1.0
    ⇒ rejected with flag ``PATH_IGN_BUDGET_EXCEEDED``. PR #36's soft-
    warning policy is intentionally reversed: Pierre's brief says
    "Interdire ou pénaliser très fortement les détours IGN massifs".
    """
    infra = gpd.GeoDataFrame(
        [], columns=["statut", "mode_pose", "src", "geometry", "sro_code"],
        geometry="geometry", crs=CRS,
    )
    seg_len = 20.0
    n_segs = 25  # 500 m → above PR41_RATIO_MIN_TOTAL_M (300 m), ratio = 1.0
    ign_geom = LineString([(i * seg_len, 0.0) for i in range(n_segs + 1)])
    ign = gpd.GeoDataFrame(
        [{"geometry": ign_geom}], geometry="geometry", crs=CRS,
    )
    public = Polygon([
        (-10.0, -10.0), (n_segs * seg_len + 10.0, -10.0),
        (n_segs * seg_len + 10.0, 10.0), (-10.0, 10.0),
    ])

    pa = _pa(0.0, 0.0, pid="PA1")
    pb = _pb(n_segs * seg_len, 0.0, pb_id="PB1", pa_id="PA1")

    flags = _Flags()
    with caplog.at_level(logging.INFO, logger="auvergne_pipeline.routing"):
        result = routing.route_pa_to_pb(
            pa, pb, infra, ign,
            flag_collector=flags,
            gc_neuf=gpd.GeoDataFrame(geometry=[], crs=CRS),
            public_area=public,
            delivery_public_area=public,
        )
    # Path rejected.
    assert result.empty or (result["infra_type"] == "gc_neuf").sum() == 0
    # PR #42 — SRO hard cap may fire before per-path cap. Accept either.
    budget_flag = [
        f for f in flags.entries
        if f["type"] in (
            "PATH_IGN_BUDGET_EXCEEDED",
            "IGN_DELIVERED_BUDGET_EXCEEDED",
        )
    ]
    assert budget_flag, (
        f"PR #41/#42: a budget rejection flag must be raised, got "
        f"{[f['type'] for f in flags.entries]}"
    )


# ---------------------------------------------------------------------------
# 8. SRO-like with PA off-line and existing infra — terminal connector
#    delivers + does not inflate straight_connector counter
# ---------------------------------------------------------------------------

def test_pa_off_line_terminal_connector_does_not_inflate_metrics(caplog):
    """A PA 1 m off the line gets attached via a short terminal C0.
    [FINAL TOPO QA] must report ``straight_connectors=0`` because the
    counter only fires on synthesised chord fallbacks, not on the
    short-and-justified terminal connectors emitted by
    ``_ensure_terminals_connected``.
    """
    infra = gpd.GeoDataFrame([{
        "statut": "E", "mode_pose": "1", "src": "bt", "infra_type": "bt",
        "geometry": LineString([(0, 0), (50, 0)]),
        "sro_code": "SRO1",
    }], geometry="geometry", crs=CRS)
    pa = _pa(25.0, 1.0, pid="PA1")  # 1 m off the line
    pb = _pb(50.0, 0.0, pb_id="PB1", pa_id="PA1")
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
    assert "straight_connectors=0" in line, line
    assert "virtual_delivered=0" in line, line
