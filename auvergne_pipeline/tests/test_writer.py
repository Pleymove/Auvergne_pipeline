"""Tests for writer.write_sro_outputs — PR #14 (8 couches)."""

from __future__ import annotations

import tempfile
from pathlib import Path

import geopandas as gpd
import pandas as pd
from shapely.geometry import LineString, Point, Polygon

from auvergne_pipeline import config, flags as flags_mod, writer


def _bal():
    return gpd.GeoDataFrame(pd.DataFrame({
        "id_metier": ["BAT1", "BAT2"], "prises": [2, 3],
        "geometry": [Point(0, 0), Point(10, 0)],
    }), geometry="geometry", crs=config.PROJECT_CRS)


def _pa(): return gpd.GeoDataFrame(pd.DataFrame({
    "id_metier": ["PA1"], "geometry": [Point(5, 5)],
}), geometry="geometry", crs=config.PROJECT_CRS)


def _zapa(): return gpd.GeoDataFrame(pd.DataFrame({
    "id_metier": ["Z1"], "geometry": [Polygon([(0, 0), (10, 0), (10, 10), (0, 10)])],
}), geometry="geometry", crs=config.PROJECT_CRS)


def _pb(): return gpd.GeoDataFrame(pd.DataFrame({
    "pb_id": ["PB1"], "pa_id": ["PA1"], "sro": ["test"], "nb_prises": [5],
    "bat_count": [2], "geometry": [Point(3, 3)],
}), geometry="geometry", crs=config.PROJECT_CRS)


def _infra(): return gpd.GeoDataFrame(pd.DataFrame({
    "statut": ["E"], "mode_pose": ["7"], "src": ["athd"],
    "geometry": [LineString([(0, 0), (10, 10)])],
}), geometry="geometry", crs=config.PROJECT_CRS)


def _parcelles(): return gpd.GeoDataFrame(pd.DataFrame({
    "public": [True, False], "proprietaire": ["Commune", "Prive"],
    "geometry": [Point(0, 0).buffer(50), Point(100, 0).buffer(50)],
}), geometry="geometry", crs=config.PROJECT_CRS)


def test_writes_8_layers():
    fc = flags_mod.FlagCollector("test")
    fc.add("TEST_FLAG")
    with tempfile.TemporaryDirectory() as td:
        out = Path(td) / "t.gpkg"
        counts = writer.write_sro_outputs(
            "test", out, bal=_bal(), georeso_pa_existants=_pa(),
            georeso_zapa_existantes=_zapa(), new_pas=[], new_zapas=[],
            pb_fictifs=_pb(), livrable_infra=_infra(),
            parcelles=_parcelles(), flag_collector=fc,
        )
        assert set(counts.keys()) == {"livrable_pa", "livrable_zapa", "livrable_bat",
                                       "livrable_infra", "livrable_pb", "livrable_parcelles",
                                       "livrable_flags"}


def test_append_mode():
    fc = flags_mod.FlagCollector("S1")
    with tempfile.TemporaryDirectory() as td:
        out = Path(td) / "t.gpkg"
        writer.write_sro_outputs("S1", out, bal=_bal(), georeso_pa_existants=_pa(),
                                 georeso_zapa_existantes=_zapa(), new_pas=[], new_zapas=[],
                                 pb_fictifs=_pb(), livrable_infra=_infra(),
                                 parcelles=_parcelles(), flag_collector=fc)
        writer.write_sro_outputs("S2", out, bal=_bal(), georeso_pa_existants=_pa(),
                                 georeso_zapa_existantes=_zapa(), new_pas=[], new_zapas=[],
                                 pb_fictifs=_pb(), livrable_infra=_infra(),
                                 parcelles=_parcelles(), flag_collector=fc)
        bat = gpd.read_file(out, layer="livrable_bat")
        assert len(bat) == 4  # 2×2


def test_parcelles_has_all():
    fc = flags_mod.FlagCollector("t")
    parc = _parcelles()
    parc["is_public"] = parc["public"]
    with tempfile.TemporaryDirectory() as td:
        out = Path(td) / "t.gpkg"
        writer.write_sro_outputs("t", out, bal=_bal(), georeso_pa_existants=_pa(),
                                 georeso_zapa_existantes=_zapa(), new_pas=[], new_zapas=[],
                                 pb_fictifs=_pb(), livrable_infra=_infra(),
                                 parcelles=parc, flag_collector=fc)
        p = gpd.read_file(out, layer="livrable_parcelles")
        assert len(p) == 2
        assert set(p["is_public"]) == {True, False}