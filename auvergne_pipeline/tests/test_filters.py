"""Pure-function tests for filters.py (no GPKG required)."""

from __future__ import annotations

import geopandas as gpd
import pandas as pd
from shapely.geometry import LineString

from auvergne_pipeline import config, filters


def _lines(n: int):
    return [LineString([(i, 0), (i, 1)]) for i in range(n)]


def _gdf(records: list[dict]) -> gpd.GeoDataFrame:
    df = pd.DataFrame(records)
    return gpd.GeoDataFrame(df, geometry="geometry", crs=config.PROJECT_CRS)


# ---------- ATHD ----------

def test_filter_athd_keeps_dispo_ge_1():
    gdf = _gdf([
        {"dispopp_ar": 0, "geometry": _lines(1)[0]},
        {"dispopp_ar": 1, "geometry": _lines(1)[0]},
        {"dispopp_ar": 5, "geometry": _lines(1)[0]},
        {"dispopp_ar": None, "geometry": _lines(1)[0]},
    ])
    out = filters.filter_athd(gdf)
    assert list(out["dispopp_ar"]) == [1, 5]


def test_filter_athd_empty_returns_empty():
    gdf = gpd.GeoDataFrame(geometry=[], crs=config.PROJECT_CRS)
    assert filters.filter_athd(gdf).empty


# ---------- BT ----------

def test_filter_bt_drops_buried_cables():
    gdf = _gdf([
        {"type_de_lien": "Aerien", "geometry": _lines(1)[0]},
        {"type_de_lien": "Cable enterre", "geometry": _lines(1)[0]},
        {"type_de_lien": "CABLE ENTERRE", "geometry": _lines(1)[0]},
        {"type_de_lien": "Facade", "geometry": _lines(1)[0]},
        {"type_de_lien": None, "geometry": _lines(1)[0]},
    ])
    out = filters.filter_bt(gdf)
    assert list(out["type_de_lien"]) == ["Aerien", "Facade", None]


# ---------- FT / PIT ----------

def test_filter_ft_arciti_excludes_e4_pleine_terre():
    gdf = _gdf([
        {"statut": "E", "mode_pose": 4, "geometry": _lines(1)[0]},   # excluded
        {"statut": "E", "mode_pose": 7, "geometry": _lines(1)[0]},   # kept
        {"statut": "C", "mode_pose": 4, "geometry": _lines(1)[0]},   # kept (not E)
        {"statut": "E", "mode_pose": "4", "geometry": _lines(1)[0]}, # excluded (string)
    ])
    out = filters.filter_ft_arciti(gdf)
    assert len(out) == 2
    assert {(r.statut, r.mode_pose) for _, r in out.iterrows()} == {("E", 7), ("C", 4)}


# ---------- Cheminement ----------

def test_filter_cheminement_keeps_C7_TR_TD_only():
    gdf = _gdf([
        {"cm_avct": "C", "cm_typ_imp": "7", "cm_typelog": "TR", "geometry": _lines(1)[0]},   # keep
        {"cm_avct": "C", "cm_typ_imp": "7", "cm_typelog": "TD", "geometry": _lines(1)[0]},   # keep
        {"cm_avct": "C", "cm_typ_imp": "7", "cm_typelog": "DI", "geometry": _lines(1)[0]},   # drop
        {"cm_avct": "P", "cm_typ_imp": "7", "cm_typelog": "TR", "geometry": _lines(1)[0]},   # drop (P7)
        {"cm_avct": None, "cm_typ_imp": None, "cm_typelog": "TR", "geometry": _lines(1)[0]}, # drop
    ])
    out = filters.filter_cheminement(gdf)
    assert sorted(out["cm_typelog"].tolist()) == ["TD", "TR"]


def test_filter_cheminement_missing_columns_returns_empty():
    gdf = _gdf([{"foo": 1, "geometry": _lines(1)[0]}])
    assert filters.filter_cheminement(gdf).empty


# ---------- Orchestrator ----------

def test_build_reusable_infra_concatenates_and_tags():
    layers = {
        config.LAYER_ATHD: _gdf([
            {"dispopp_ar": 2, "geometry": _lines(1)[0]},
        ]),
        config.LAYER_BT: _gdf([
            {"type_de_lien": "Aerien", "geometry": _lines(1)[0]},
            {"type_de_lien": "Cable enterre", "geometry": _lines(1)[0]},  # dropped
        ]),
        config.LAYER_FT_ARCITI: _gdf([
            {"statut": "E", "mode_pose": 7, "geometry": _lines(1)[0]},
        ]),
        config.LAYER_CHEMINEMENT: _gdf([
            {"cm_avct": "C", "cm_typ_imp": "7", "cm_typelog": "TR", "geometry": _lines(1)[0]},
        ]),
    }
    out = filters.build_reusable_infra(layers)
    assert len(out) == 4
    assert sorted(out["src"].unique().tolist()) == ["athd", "bt", "chem", "ft"]
    assert out.crs is not None


def test_build_reusable_infra_returns_empty_with_crs_when_no_input():
    out = filters.build_reusable_infra({})
    assert out.empty
    assert str(out.crs) == config.PROJECT_CRS
