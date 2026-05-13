from __future__ import annotations

import geopandas as gpd
import pandas as pd
from shapely.geometry import LineString, Point, Polygon

from auvergne_pipeline import livrable_topology as lt

CRS = "EPSG:2154"
PUB = Polygon([(-100,-100),(100,-100),(100,100),(-100,100)])


def _base_df():
    return gpd.GeoDataFrame([
        {"sro":"S1","pa_id":"PA1","pb_id":"PB1","statut":"E","mode_pose":"1","src":"bt","infra_type":"bt","length_m":10.0,"geometry":LineString([(0,0),(10,0)])}
    ], geometry="geometry", crs=CRS)


def _pa_pb(pa_pt=(0,1), pb_pt=(10,1)):
    pa = gpd.GeoDataFrame(pd.DataFrame({"id_metier":["PA1"],"sro":["S1"],"geometry":[Point(*pa_pt)]}), geometry="geometry", crs=CRS)
    pb = gpd.GeoDataFrame(pd.DataFrame({"pb_id":["PB1"],"pa_id":["PA1"],"sro":["S1"],"geometry":[Point(*pb_pt)]}), geometry="geometry", crs=CRS)
    return pa, pb


def test_terminal_anchors_are_in_routing_and_final_graph():
    df = _base_df()
    pa, pb = _pa_pb()
    out, stats = lt._ensure_terminals_connected(df, pa, pb, delivery_public_area_safe=PUB)
    anchors = out[out.get("_terminal_anchor", False) == True]
    assert len(anchors) >= 2
    assert stats["pa_anchor_success"] >= 1
    assert stats["pb_anchor_success"] >= 1
    assert stats["terminal_anchor_edges_added"] >= 2


def test_existing_only_path_with_terminal_anchors_no_c0_spam():
    df = _base_df()
    pa, pb = _pa_pb(pa_pt=(0,0.05), pb_pt=(10,0.05))
    out, stats = lt._ensure_terminals_connected(df, pa, pb, delivery_public_area_safe=PUB)
    c0 = out[out.get("mode_pose", "") == "C0"]
    # very close terminals should snap via existing, not spam C0
    assert len(c0) == 0
    assert stats["terminals_connected_via_existing"] >= 2


def test_63149_like_no_infra_case_logs_rejection_reasons():
    df = _base_df()
    pa, pb = _pa_pb(pa_pt=(0,50), pb_pt=(10,50))
    _out, stats = lt._ensure_terminals_connected(df, pa, pb, delivery_public_area_safe=PUB)
    assert stats["pa_anchor_failed"] >= 1
    assert stats["pb_anchor_failed"] >= 1
    assert stats["pa_anchor_failed_too_far"] >= 1
    assert stats["pb_anchor_failed_too_far"] >= 1


from auvergne_pipeline import routing


def test_routing_logs_pr38_pass_counters(caplog):
    infra = _base_df()
    pa, pb = _pa_pb(pa_pt=(0, 0), pb_pt=(10, 0))
    ign = gpd.GeoDataFrame([], columns=["geometry"], geometry="geometry", crs=CRS)
    with caplog.at_level("INFO", logger="auvergne_pipeline.routing"):
        _ = routing.route_pa_to_pb(pa, pb, infra, ign, public_area=PUB, delivery_public_area=PUB)
    lines = [r.getMessage() for r in caplog.records if "[FINAL TOPO QA]" in r.getMessage()]
    assert lines
    line = lines[-1]
    assert "pass1_existing_reached=" in line
    assert "pass2_gc_reached=" in line
    assert "path_broken_after_postprocess=" in line


def test_final_graph_validation_catches_disconnected_metadata(monkeypatch, caplog):
    infra = _base_df()
    pa, pb = _pa_pb(pa_pt=(0, 0), pb_pt=(10, 0))
    ign = gpd.GeoDataFrame([], columns=["geometry"], geometry="geometry", crs=CRS)

    def _fake_finalize(df, *args, **kwargs):
        # Keep _used_by_paths metadata but force disconnection in geometry.
        out = df.copy()
        out["geometry"] = [
            LineString([(0, 0), (1, 0)]),
            LineString([(9, 0), (10, 0)]),
        ][:len(out)]
        stats = {"pa_pb_connected_count": 1, "pa_pb_disconnected_count": 0}
        return out, stats

    monkeypatch.setattr("auvergne_pipeline.livrable_topology.finalize_livrable_topology", _fake_finalize)
    with caplog.at_level("INFO", logger="auvergne_pipeline.routing"):
        _ = routing.route_pa_to_pb(pa, pb, infra, ign, public_area=PUB, delivery_public_area=PUB)
    lines = [r.getMessage() for r in caplog.records if "[FINAL TOPO QA]" in r.getMessage()]
    assert lines
    line = lines[-1]
    assert "path_metadata_present_but_graph_disconnected=" in line
    assert "committed_path_unreachable_final_graph=" in line
