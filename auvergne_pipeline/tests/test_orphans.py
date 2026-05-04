"""Tests for orphans.detect_orphans + create_pa_for_orphans."""

from __future__ import annotations

import geopandas as gpd
import pandas as pd
from shapely.geometry import Point, Polygon

from auvergne_pipeline import config, flags as flags_mod, orphans


def _bal() -> gpd.GeoDataFrame:
    df = pd.DataFrame(
        {
            "id_metier": ["BAT_1", "BAT_2", "BAT_3", "BAT_4", "BAT_5"],
            "zapa": ["Z1", "Z2", "Z3", "Z_OLD", "Z_OLD"],
            "prises": [2, 3, 1, 4, 2],
            "geometry": [
                Point(1, 1), Point(2, 2), Point(3, 3),
                Point(10, 10), Point(11, 11),
            ],
        }
    )
    return gpd.GeoDataFrame(df, geometry="geometry", crs=config.PROJECT_CRS)


def _zapa_referential() -> gpd.GeoDataFrame:
    df = pd.DataFrame(
        {
            "id_metier": ["Z1", "Z2", "Z3"],
            "geometry": [
                Polygon([(0, 0), (5, 0), (5, 5), (0, 5)]),
                Polygon([(0, 0), (5, 0), (5, 5), (0, 5)]),
                Polygon([(0, 0), (5, 0), (5, 5), (0, 5)]),
            ],
        }
    )
    return gpd.GeoDataFrame(df, geometry="geometry", crs=config.PROJECT_CRS)


def test_detect_orphans_returns_bats_with_unknown_zapa():
    bal = _bal()
    zapa = _zapa_referential()
    out = orphans.detect_orphans(bal, zapa)
    assert sorted(out["id_metier"].tolist()) == ["BAT_4", "BAT_5"]


def test_detect_orphans_handles_empty_referential():
    bal = _bal()
    empty = gpd.GeoDataFrame(
        {"id_metier": [], "geometry": []}, geometry="geometry", crs=config.PROJECT_CRS
    )
    out = orphans.detect_orphans(bal, empty)
    assert len(out) == len(bal)


def test_create_pa_for_orphans_one_cluster_one_pa():
    bal = _bal()
    zapa = _zapa_referential()
    orphans_gdf = orphans.detect_orphans(bal, zapa)

    fc = flags_mod.FlagCollector("63149/M06/PMZ/42478")
    pa_rows, zapa_rows = orphans.create_pa_for_orphans(
        orphans_gdf, sro_code="63149/M06/PMZ/42478", flag_collector=fc
    )

    assert len(pa_rows) == 1
    assert len(zapa_rows) == 1
    assert pa_rows[0]["id_metier"] == "63149/M06/PA/99001"
    # Weighted barycentre: weights [4, 2] on (10,10) and (11,11)
    # cx = (4*10 + 2*11) / 6 = 62/6 ~= 10.333
    assert abs(pa_rows[0]["geometry"].x - 62 / 6) < 1e-6
    assert abs(pa_rows[0]["geometry"].y - 62 / 6) < 1e-6
    assert pa_rows[0]["n_bat"] == 2
    assert fc.counts().get("PA_ORPHELIN_CREE") == 1


def test_create_pa_for_orphans_empty_input_returns_nothing():
    empty = gpd.GeoDataFrame(
        {"id_metier": [], "zapa": [], "prises": [], "geometry": []},
        geometry="geometry", crs=config.PROJECT_CRS,
    )
    pa_rows, zapa_rows = orphans.create_pa_for_orphans(
        empty, sro_code="63149/M06/PMZ/42478"
    )
    assert pa_rows == [] and zapa_rows == []


def test_create_pa_for_orphans_spatial_fallback_for_null_zapa():
    df = pd.DataFrame(
        {
            "id_metier": ["BAT_A", "BAT_B", "BAT_FAR"],
            "zapa": [None, None, None],
            "prises": [1, 1, 1],
            "geometry": [Point(0, 0), Point(50, 0), Point(10000, 0)],
        }
    )
    bal = gpd.GeoDataFrame(df, geometry="geometry", crs=config.PROJECT_CRS)
    pa_rows, _ = orphans.create_pa_for_orphans(bal, sro_code="63149/M06/PMZ/42478")
    # BAT_A and BAT_B are 50 m apart (< 200 m eps) -> 1 cluster.
    # BAT_FAR is 9950 m away -> separate cluster.
    assert len(pa_rows) == 2
    assert {r["id_metier"] for r in pa_rows} == {
        "63149/M06/PA/99001",
        "63149/M06/PA/99002",
    }
