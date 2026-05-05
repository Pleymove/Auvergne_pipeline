"""Tests for writer.write_sro_outputs (PR #13)."""

from __future__ import annotations

import tempfile
from pathlib import Path

import geopandas as gpd
import pandas as pd
import pytest
from shapely.geometry import Point, Polygon

from auvergne_pipeline import config, flags as flags_mod, writer


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _bal_gdf() -> gpd.GeoDataFrame:
    return gpd.GeoDataFrame(
        pd.DataFrame({
            "id_metier": ["BAT_A", "BAT_B"],
            "zapa": ["Z1", "Z1"],
            "prises": [2, 3],
            "geometry": [Point(0, 0), Point(10, 0)],
        }),
        geometry="geometry", crs=config.PROJECT_CRS,
    )


def _pa_gdf() -> gpd.GeoDataFrame:
    return gpd.GeoDataFrame(
        pd.DataFrame({
            "id_metier": ["PA_EXIST"],
            "geometry": [Point(5, 5)],
        }),
        geometry="geometry", crs=config.PROJECT_CRS,
    )


def _zapa_gdf() -> gpd.GeoDataFrame:
    return gpd.GeoDataFrame(
        pd.DataFrame({
            "id_metier": ["ZAPA_EXIST"],
            "geometry": [Polygon([(0, 0), (10, 0), (10, 10), (0, 10)])],
        }),
        geometry="geometry", crs=config.PROJECT_CRS,
    )


def _d3_results() -> list[dict]:
    return [
        {"bat_url": "BAT_A", "d3": 42.0, "cls": "AUTO_OK", "parcel_url": "P1"},
        {"bat_url": "BAT_B", "d3": None, "cls": "TO_CREATE", "parcel_url": ""},
    ]


def _new_pas() -> list[dict]:
    return [
        {
            "id_metier": "63/M06/PA/99001",
            "origine": "cree",
            "snap_source": "cheminement",
            "n_bat": 5,
            "total_prises": 20,
            "geometry": Point(3, 3),
        }
    ]


def _new_zapas() -> list[dict]:
    return [
        {
            "id_metier": "63/M06/PA/99001",
            "origine": "cree",
            "geometry": Point(3, 3).buffer(20),
        }
    ]


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_write_sro_outputs_creates_expected_layers():
    """Smoke test: all 6 layers are written to the output GPKG."""
    bal = _bal_gdf()
    with tempfile.TemporaryDirectory() as td:
        out = Path(td) / "test.gpkg"
        fc = flags_mod.FlagCollector("test/SRO/PMZ/1")
        fc.add("PA_ORPHELIN_CREE", target_url="63/M06/PA/99001")

        counts = writer.write_sro_outputs(
            "test/SRO/PMZ/1",
            out,
            bal=bal,
            d3_results=_d3_results(),
            georeso_pa_existants=_pa_gdf(),
            georeso_zapa_existantes=_zapa_gdf(),
            new_pas=_new_pas(),
            new_zapas=_new_zapas(),
            reusable_infra=gpd.GeoDataFrame(
                pd.DataFrame({"src": ["athd"], "geometry": [Point(0, 0)]}),
                geometry="geometry", crs=config.PROJECT_CRS,
            ),
            parcelles_classifiees=gpd.GeoDataFrame(
                pd.DataFrame({
                    "public": [True],
                    "geometry": [Point(0, 0).buffer(100)],
                }),
                geometry="geometry", crs=config.PROJECT_CRS,
            ),
            flag_collector=fc,
        )

        assert out.exists()
        assert set(counts.keys()) == {
            "livrable_pa",
            "livrable_zapa",
            "livrable_bat",
            "livrable_infra_reutilisable",
            "livrable_parcelles_publiques",
            "livrable_flags",
        }
        # Verify layers can be read back
        for layer in ["livrable_pa", "livrable_bat", "livrable_zapa"]:
            gdf = gpd.read_file(out, layer=layer)
            assert len(gdf) > 0


def test_write_sro_outputs_append_mode():
    """Calling write_sro_outputs twice appends to the same layers."""
    bal = _bal_gdf()
    with tempfile.TemporaryDirectory() as td:
        out = Path(td) / "test.gpkg"

        for sro in ("SRO1", "SRO2"):
            fc = flags_mod.FlagCollector(sro)
            writer.write_sro_outputs(
                sro,
                out,
                bal=bal,
                d3_results=_d3_results(),
                georeso_pa_existants=_pa_gdf(),
                georeso_zapa_existantes=_zapa_gdf(),
                new_pas=_new_pas(),
                new_zapas=_new_zapas(),
                reusable_infra=gpd.GeoDataFrame(
                    geometry=[], crs=config.PROJECT_CRS,
                ),
                parcelles_classifiees=gpd.GeoDataFrame(
                    geometry=[], crs=config.PROJECT_CRS,
                ),
                flag_collector=fc,
            )

        # bat layer should have 2 * 2 = 4 features
        bat = gpd.read_file(out, layer="livrable_bat")
        assert len(bat) == 4


def test_write_sro_outputs_bat_enriched_with_d3():
    """BAT layer contains d3_m, cls, parcel_url from d3_results."""
    bal = _bal_gdf()
    with tempfile.TemporaryDirectory() as td:
        out = Path(td) / "test.gpkg"
        fc = flags_mod.FlagCollector("test/SRO/PMZ/1")

        writer.write_sro_outputs(
            "test/SRO/PMZ/1",
            out,
            bal=bal,
            d3_results=_d3_results(),
            georeso_pa_existants=_pa_gdf(),
            georeso_zapa_existantes=_zapa_gdf(),
            new_pas=[],
            new_zapas=[],
            reusable_infra=gpd.GeoDataFrame(
                geometry=[], crs=config.PROJECT_CRS,
            ),
            parcelles_classifiees=gpd.GeoDataFrame(
                geometry=[], crs=config.PROJECT_CRS,
            ),
            flag_collector=fc,
        )

        bat = gpd.read_file(out, layer="livrable_bat")
        # BAT_A should be AUTO_OK with d3=42
        row_a = bat[bat["id_metier"] == "BAT_A"].iloc[0]
        assert row_a["cls"] == "AUTO_OK"
        assert row_a["d3_m"] == 42.0
        assert row_a["parcel_url"] == "P1"

        # BAT_B should be TO_CREATE with d3=None
        row_b = bat[bat["id_metier"] == "BAT_B"].iloc[0]
        assert row_b["cls"] == "TO_CREATE"
        assert pd.isna(row_b["d3_m"])


def test_write_sro_outputs_pa_has_existing_and_created():
    """PA layer contains both existing and created entries."""
    bal = _bal_gdf()
    with tempfile.TemporaryDirectory() as td:
        out = Path(td) / "test.gpkg"
        fc = flags_mod.FlagCollector("test/SRO/PMZ/1")

        writer.write_sro_outputs(
            "test/SRO/PMZ/1",
            out,
            bal=bal,
            d3_results=_d3_results(),
            georeso_pa_existants=_pa_gdf(),
            georeso_zapa_existantes=_zapa_gdf(),
            new_pas=_new_pas(),
            new_zapas=_new_zapas(),
            reusable_infra=gpd.GeoDataFrame(
                geometry=[], crs=config.PROJECT_CRS,
            ),
            parcelles_classifiees=gpd.GeoDataFrame(
                geometry=[], crs=config.PROJECT_CRS,
            ),
            flag_collector=fc,
        )

        pa = gpd.read_file(out, layer="livrable_pa")
        origines = set(pa["origine"])
        assert "existant" in origines
        assert "cree" in origines


def test_write_sro_outputs_public_parcels_only():
    """Only public parcels are written to the output."""
    bal = _bal_gdf()
    parc = gpd.GeoDataFrame(
        pd.DataFrame({
            "public": [True, False, True],
            "proprietaire": ["Commune A", "M. Dupont", "Commune B"],
            "geometry": [
                Point(0, 0).buffer(50),
                Point(100, 100).buffer(50),
                Point(200, 200).buffer(50),
            ],
        }),
        geometry="geometry", crs=config.PROJECT_CRS,
    )

    with tempfile.TemporaryDirectory() as td:
        out = Path(td) / "test.gpkg"
        fc = flags_mod.FlagCollector("test/SRO/PMZ/1")

        writer.write_sro_outputs(
            "test/SRO/PMZ/1",
            out,
            bal=bal,
            d3_results=_d3_results(),
            georeso_pa_existants=_pa_gdf(),
            georeso_zapa_existantes=_zapa_gdf(),
            new_pas=[],
            new_zapas=[],
            reusable_infra=gpd.GeoDataFrame(
                geometry=[], crs=config.PROJECT_CRS,
            ),
            parcelles_classifiees=parc,
            flag_collector=fc,
        )

        pub = gpd.read_file(out, layer="livrable_parcelles_publiques")
        assert len(pub) == 2
        assert all("Commune" in p for p in pub["proprietaire"])
