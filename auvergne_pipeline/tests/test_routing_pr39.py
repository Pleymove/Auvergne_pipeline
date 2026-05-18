from __future__ import annotations

import re

import geopandas as gpd
import networkx as nx
import pandas as pd
from shapely.geometry import LineString, Point, Polygon

from auvergne_pipeline import livrable_topology as lt
from auvergne_pipeline import routing


CRS = "EPSG:2154"
PUB = Polygon([(-100, -100), (300, -100), (300, 100), (-100, 100)])


def _infra(coords=((0, 0), (20, 0)), *, mode_pose="1", src="bt"):
    return gpd.GeoDataFrame(
        [
            {
                "sro": "S1",
                "pa_id": "PA1",
                "pb_id": "PB1",
                "statut": "E",
                "mode_pose": mode_pose,
                "src": src,
                "infra_type": src,
                "length_m": LineString(coords).length,
                "geometry": LineString(coords),
            }
        ],
        geometry="geometry",
        crs=CRS,
    )


def _pa(point=(0, 0), pa_id="PA1"):
    return gpd.GeoDataFrame(
        pd.DataFrame({"id_metier": [pa_id], "sro": ["S1"], "geometry": [Point(*point)]}),
        geometry="geometry",
        crs=CRS,
    )


def _pb(points, pa_ids=None):
    pa_ids = pa_ids if pa_ids is not None else ["PA1"] * len(points)
    return gpd.GeoDataFrame(
        pd.DataFrame(
            {
                "pb_id": [f"PB{i + 1}" for i in range(len(points))],
                "pa_id": pa_ids,
                "sro": ["S1"] * len(points),
                "geometry": [Point(*p) for p in points],
            }
        ),
        geometry="geometry",
        crs=CRS,
    )


def _empty_ign():
    return gpd.GeoDataFrame([], columns=["geometry"], geometry="geometry", crs=CRS)


def test_pb_assignment_regression_route_does_not_see_pb_count_zero(caplog):
    pa = _pa()
    pb = _pb([(10, 0), (20, 0)], pa_ids=[None, None])
    infra = gpd.GeoDataFrame(
        [
            {**_infra(((0, 0), (10, 0))).iloc[0].to_dict(), "geometry": LineString([(0, 0), (10, 0)])},
            {**_infra(((10, 0), (20, 0))).iloc[0].to_dict(), "geometry": LineString([(10, 0), (20, 0)])},
        ],
        geometry="geometry",
        crs=CRS,
    )

    with caplog.at_level("INFO", logger="auvergne_pipeline.routing"):
        out = routing.route_pa_to_pb(pa, pb, infra, _empty_ign(), public_area=PUB, delivery_public_area=PUB)

    assert not out.empty
    qa = [r.getMessage() for r in caplog.records if "[PB ROUTING QA]" in r.getMessage()][-1]
    perf = [r.getMessage() for r in caplog.records if "step=dijkstra_total" in r.getMessage()][-1]
    assert "pb_total=2" in qa
    assert "pb_assigned=2" in qa
    assert "pb_attempted=2" in qa
    assert "pb_committed=2" in qa
    assert "pb_dropped=0" in qa
    assert "pb_count=2" in perf


def test_pb_pa_matching_fallback_routes_mismatched_pa_id(caplog):
    pa = _pa()
    pb = _pb([(20, 0)], pa_ids=["STALE_PA_ID"])

    with caplog.at_level("INFO", logger="auvergne_pipeline.routing"):
        out = routing.route_pa_to_pb(pa, pb, _infra(), _empty_ign(), public_area=PUB, delivery_public_area=PUB)

    assert not out.empty
    qa = [r.getMessage() for r in caplog.records if "[PB ROUTING QA]" in r.getMessage()][-1]
    assert "pb_assigned=1" in qa
    assert "pb_unassigned=0" in qa
    assert "pb_dropped=0" in qa


def test_logical_anchor_10m_has_no_visible_terminal_connector(caplog):
    pa = _pa(point=(0, 10))
    pb = _pb([(20, 10)])

    with caplog.at_level("INFO", logger="auvergne_pipeline.routing"):
        out = routing.route_pa_to_pb(pa, pb, _infra(), _empty_ign(), public_area=PUB, delivery_public_area=PUB)

    assert not out.empty
    assert out[out.get("infra_type", "") == "terminal_connector"].empty
    anchor = [r.getMessage() for r in caplog.records if "[ANCHOR QA]" in r.getMessage()][-1]
    final = [r.getMessage() for r in caplog.records if "[FINAL TOPO QA]" in r.getMessage()][-1]
    assert "terminal_logical_only=2" in anchor
    assert "path_metadata_present_but_graph_disconnected=0" in final


def test_terminal_anchor_edges_have_explicit_dijkstra_weights(monkeypatch):
    pa = _pa(point=(0, 10))
    pb = _pb([(20, 10)])
    original = routing.nx.single_source_dijkstra

    def _assert_weighted(graph, *args, **kwargs):
        for _, _, data in graph.edges(data=True):
            assert "_routing_weight" in data
            assert "_pass1_weight" in data
            assert "_is_existing" in data
            assert "_can_deliver" in data
        return original(graph, *args, **kwargs)

    monkeypatch.setattr(routing.nx, "single_source_dijkstra", _assert_weighted)

    routing.route_pa_to_pb(pa, pb, _infra(), _empty_ign(), public_area=PUB, delivery_public_area=PUB)


def test_path_preserving_postprocess_keeps_committed_path_connected(caplog):
    pa = _pa()
    pb = _pb([(20, 0)])

    with caplog.at_level("INFO", logger="auvergne_pipeline.routing"):
        out = routing.route_pa_to_pb(pa, pb, _infra(), _empty_ign(), public_area=PUB, delivery_public_area=PUB)

    assert not out.empty
    final = [r.getMessage() for r in caplog.records if "[FINAL TOPO QA]" in r.getMessage()][-1]
    assert "path_lost_between_routing_and_final_graph=0" in final
    assert "path_metadata_present_but_graph_disconnected=0" in final
    assert re.search(r"committed_path_reachable_final_graph=[1-9]", final)


def test_private_c0_path_is_marked_impossible_not_kept(monkeypatch, caplog):
    monkeypatch.setattr(routing, "ALLOW_IGN_ROAD_C0_WITHOUT_PARCEL_GATE", False)
    pa = _pa()
    pb = _pb([(20, 0)])
    empty_infra = gpd.GeoDataFrame([], columns=["geometry"], geometry="geometry", crs=CRS)
    gc_neuf = gpd.GeoDataFrame(
        [
            {
                "sro": "S1",
                "pa_id": "PA1",
                "pb_id": "PB1",
                "statut": None,
                "mode_pose": "C0",
                "src": "gc_neuf",
                "infra_type": "gc_neuf",
                "geometry": LineString([(0, 0), (20, 0)]),
            }
        ],
        geometry="geometry",
        crs=CRS,
    )
    strict_public = Polygon([(-1, -1), (5, -1), (5, 1), (-1, 1)])

    with caplog.at_level("INFO", logger="auvergne_pipeline.routing"):
        out = routing.route_pa_to_pb(
            pa,
            pb,
            empty_infra,
            _empty_ign(),
            gc_neuf=gc_neuf,
            public_area=PUB,
            delivery_public_area=strict_public,
        )

    assert out.empty
    qa = [r.getMessage() for r in caplog.records if "[PB ROUTING QA]" in r.getMessage()][-1]
    final = [r.getMessage() for r in caplog.records if "[FINAL TOPO QA]" in r.getMessage()][-1]
    assert "pb_committed=0" in qa
    assert "pb_impossible=1" in qa
    assert "pb_dropped=0" in qa
    assert "path_impossible_private_c0=1" in final
    assert "c0_private_crossing_kept=0" in final
    assert "pb_impossible_private_c0=1" in final


def test_shared_trunk_preserves_union_used_by_paths():
    pa = _pa()
    pb = _pb([(10, 0), (20, 0)])
    infra = gpd.GeoDataFrame(
        [
            {**_infra(((0, 0), (10, 0))).iloc[0].to_dict(), "geometry": LineString([(0, 0), (10, 0)]), "length_m": 10.0},
            {**_infra(((10, 0), (20, 0))).iloc[0].to_dict(), "geometry": LineString([(10, 0), (20, 0)]), "length_m": 10.0},
        ],
        geometry="geometry",
        crs=CRS,
    )

    out = routing.route_pa_to_pb(pa, pb, infra, _empty_ign(), public_area=PUB, delivery_public_area=PUB)

    trunk = out[out.geometry.apply(lambda g: g.equals(LineString([(0, 0), (10, 0)])))]
    assert not trunk.empty
    paths = trunk.iloc[0]["_used_by_paths"]
    assert "PA1->PB1" in paths
    assert "PA1->PB2" in paths


def test_final_graph_noding_endpoint_touches_middle_of_segment():
    df = gpd.GeoDataFrame(
        [
            {"geometry": LineString([(0, 0), (10, 0)]), "mode_pose": "1", "src": "bt", "infra_type": "bt"},
            {"geometry": LineString([(5, 0), (5, 5)]), "mode_pose": "1", "src": "bt", "infra_type": "bt"},
        ],
        geometry="geometry",
        crs=CRS,
    )

    graph = lt._build_livrable_topology_graph(df)

    assert (0.0, 0.0) in graph
    assert (5.0, 5.0) in graph
    assert nx.has_path(graph, (0.0, 0.0), (5.0, 5.0))


def test_c0_geom_qa_flags_long_unbacked_straight_chord(monkeypatch, caplog):
    pa = _pa()
    pb = _pb([(100, 0)])

    def _fake_finalize(df, *args, **kwargs):
        out = gpd.GeoDataFrame(
            [
                {
                    "sro": "S1",
                    "pa_id": "PA1",
                    "pb_id": "PB1",
                    "statut": "",
                    "mode_pose": "C0",
                    "src": "gc_neuf",
                    "infra_type": "gc_neuf",
                    "length_m": 100.0,
                    "_used_by_paths": "PA1->PB1",
                    "geometry": LineString([(0, 0), (100, 0)]),
                }
            ],
            geometry="geometry",
            crs=CRS,
        )
        return out, {"pa_pb_connected_count": 1, "pa_pb_disconnected_count": 0}

    monkeypatch.setattr(
        "auvergne_pipeline.livrable_topology.finalize_livrable_topology",
        _fake_finalize,
    )

    with caplog.at_level("INFO", logger="auvergne_pipeline.routing"):
        routing.route_pa_to_pb(
            pa,
            pb,
            _infra(((0, 0), (100, 0))),
            _empty_ign(),
            public_area=PUB,
            delivery_public_area=PUB,
        )

    c0 = [r.getMessage() for r in caplog.records if "[C0 GEOM QA]" in r.getMessage()][-1]
    assert "c0_suspicious_chord_count=1" in c0
    assert "c0_long_without_route_geometry_count=1" in c0
