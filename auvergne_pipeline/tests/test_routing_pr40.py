from __future__ import annotations

import re

import geopandas as gpd
import pandas as pd
from shapely.geometry import LineString, Point, Polygon

from auvergne_pipeline import routing


CRS = "EPSG:2154"
STRICT_PUBLIC_ELSEWHERE = Polygon([(1000, 1000), (1010, 1000), (1010, 1010), (1000, 1010)])
PUBLIC_AROUND_TEST = Polygon([(-100, -100), (200, -100), (200, 100), (-100, 100)])


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


def _infra(coords):
    line = LineString(coords)
    return gpd.GeoDataFrame(
        [
            {
                "sro": "S1",
                "statut": "E",
                "mode_pose": "1",
                "src": "bt",
                "infra_type": "bt",
                "length_m": line.length,
                "geometry": line,
            }
        ],
        geometry="geometry",
        crs=CRS,
    )


def _ign(coords):
    line = LineString(coords)
    return gpd.GeoDataFrame(
        [
            {
                "sro": "S1",
                "src": "ign",
                "infra_type": "route",
                "length_m": line.length,
                "geometry": line,
            }
        ],
        geometry="geometry",
        crs=CRS,
    )


def _empty_edges():
    return gpd.GeoDataFrame([], columns=["geometry"], geometry="geometry", crs=CRS)


def test_ign_road_outside_delivery_area_still_delivers_c0(caplog):
    with caplog.at_level("INFO", logger="auvergne_pipeline.routing"):
        out = routing.route_pa_to_pb(
            _pa((0, 0)),
            _pb([(50, 0)]),
            _empty_edges(),
            _ign([(0, 0), (50, 0)]),
            public_area=PUBLIC_AROUND_TEST,
            delivery_public_area=STRICT_PUBLIC_ELSEWHERE,
        )

    assert not out.empty
    assert (out["mode_pose"] == "C0").any()
    assert (out["src"] == "gc_neuf").any()
    assert any(out.geometry.apply(lambda g: g.equals(LineString([(0, 0), (50, 0)]))))
    qa = [r.getMessage() for r in caplog.records if "[PB ROUTING QA]" in r.getMessage()][-1]
    road = [r.getMessage() for r in caplog.records if "[ROAD C0 QA]" in r.getMessage()][-1]
    assert "pb_committed=1" in qa
    assert "pb_dropped=0" in qa
    assert "road_c0_delivered_count=1" in road
    assert "parcel_gate_disabled_count=1" in road


def test_direct_pa_pb_chord_still_forbidden(caplog):
    with caplog.at_level("INFO", logger="auvergne_pipeline.routing"):
        out = routing.route_pa_to_pb(
            _pa((0, 0)),
            _pb([(50, 0)]),
            _empty_edges(),
            _empty_edges(),
            public_area=PUBLIC_AROUND_TEST,
            delivery_public_area=STRICT_PUBLIC_ELSEWHERE,
        )

    assert out.empty
    qa = [r.getMessage() for r in caplog.records if "[PB ROUTING QA]" in r.getMessage()][-1]
    assert "pb_committed=0" in qa
    assert "pb_impossible=1" in qa
    assert "pb_dropped=0" in qa


def test_existing_infra_preferred_over_shorter_ign():
    out = routing.route_pa_to_pb(
        _pa((0, 0)),
        _pb([(50, 0)]),
        _infra([(0, 0), (0, 30), (50, 0)]),
        _ign([(0, 0), (50, 0)]),
        public_area=PUBLIC_AROUND_TEST,
        delivery_public_area=STRICT_PUBLIC_ELSEWHERE,
    )

    assert not out.empty
    assert not (out["mode_pose"] == "C0").any()
    assert set(out["src"]) == {"bt"}


def test_final_graph_preserves_mixed_existing_and_ign_path(caplog):
    infra = _infra([(0, 0), (10, 0)])
    ign = _ign([(10, 0), (50, 0)])

    with caplog.at_level("INFO", logger="auvergne_pipeline.routing"):
        out = routing.route_pa_to_pb(
            _pa((0, 0)),
            _pb([(50, 0)]),
            infra,
            ign,
            public_area=PUBLIC_AROUND_TEST,
            delivery_public_area=STRICT_PUBLIC_ELSEWHERE,
        )

    assert not out.empty
    assert (out["mode_pose"] == "C0").any()
    final = [r.getMessage() for r in caplog.records if "[FINAL TOPO QA]" in r.getMessage()][-1]
    assert "path_lost_between_routing_and_final_graph=0" in final
    assert "path_metadata_present_but_graph_disconnected=0" in final
    assert "committed_path_unreachable_final_graph=0" in final
    assert re.search(r"committed_path_reachable_final_graph=1\b", final)


def test_parcel_gate_disabled_mode_and_road_c0_log(caplog):
    with caplog.at_level("INFO", logger="auvergne_pipeline.routing"):
        routing.route_pa_to_pb(
            _pa((0, 0)),
            _pb([(50, 0)]),
            _empty_edges(),
            _ign([(0, 0), (50, 0)]),
            public_area=PUBLIC_AROUND_TEST,
            delivery_public_area=STRICT_PUBLIC_ELSEWHERE,
        )

    mode = [r.getMessage() for r in caplog.records if "[ROUTING MODE]" in r.getMessage()][-1]
    road = [r.getMessage() for r in caplog.records if "[ROAD C0 QA]" in r.getMessage()][-1]
    assert "parcel_gate=disabled" in mode
    assert "network=existing+ign_roads" in mode
    assert "direct_chords=false" in mode
    assert "road_c0_delivered_count=1" in road
