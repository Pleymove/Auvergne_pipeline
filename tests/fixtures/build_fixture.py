"""Build a tiny synthetic ``mini_auvergne.gpkg`` fixture for the E2E test.

Run once::

    python tests/fixtures/build_fixture.py

The output ``mini_auvergne.gpkg`` is committed to the repo so the E2E test
does not need to regenerate it on every run.

Layout (in EPSG:2154 - all coordinates in metres):

* 1 ZA_SRO ``99999/TST/PMZ/00001`` covering [0,1000]x[0,1000].
* 5 BAT inside the SRO with ``prises`` set; 2 share an existing zapa, the
  other 3 are orphans (their ``zapa`` value does not match any
  ``georeso_zapa`` row).
* 1 existing PA + 1 existing ZAPA (a small disc around the PA covering 2
  of the 5 BATs).
* 2 existant_ft_arciti rows -- 1 deliberate MultiLineString to exercise
  the regression path from PR #15 (routing._explode_to_linestrings).
* 1 existant_athd_artere row.
* 1 existant_bt row (kept, "Aerien").
* 1 existant_t_cheminement row (C7 / TR -- the only reusable one).
* 5 parcelles: 2 owned by the commune (public), 3 privately owned.
"""

from __future__ import annotations

from pathlib import Path

import geopandas as gpd
import pandas as pd
from shapely.geometry import (
    LineString,
    MultiLineString,
    Point,
    Polygon,
)

CRS = "EPSG:2154"
SRO_CODE = "99999/TST/PMZ/00001"
EXISTING_ZAPA_ID = "99999/TST/ZAPA/00001"
EXISTING_PA_ID = "99999/TST/PA/00001"

OUT = Path(__file__).resolve().parent / "mini_auvergne.gpkg"


def _gdf(rows: list[dict]) -> gpd.GeoDataFrame:
    return gpd.GeoDataFrame(pd.DataFrame(rows), geometry="geometry", crs=CRS)


def build() -> Path:
    if OUT.exists():
        OUT.unlink()

    # ------------------------------------------------------------------
    # ZA_SRO -- single 1 km square
    # ------------------------------------------------------------------
    za_polygon = Polygon([(0, 0), (1000, 0), (1000, 1000), (0, 1000)])
    za_sro = _gdf([
        {"sro": SRO_CODE, "geometry": za_polygon},
    ])
    za_sro.to_file(OUT, layer="za_sro", driver="GPKG")

    # ------------------------------------------------------------------
    # BAL -- 5 BATs. First 2 share EXISTING_ZAPA_ID, last 3 are orphans.
    # ------------------------------------------------------------------
    bal = _gdf([
        {"id_metier": "BAT/00001", "sro": SRO_CODE,
         "zapa": EXISTING_ZAPA_ID, "prises": 4,
         "geometry": Point(110, 110)},
        {"id_metier": "BAT/00002", "sro": SRO_CODE,
         "zapa": EXISTING_ZAPA_ID, "prises": 3,
         "geometry": Point(140, 130)},
        {"id_metier": "BAT/00003", "sro": SRO_CODE,
         "zapa": "ORPHAN_ZAPA_X", "prises": 60,
         "geometry": Point(600, 600)},
        {"id_metier": "BAT/00004", "sro": SRO_CODE,
         "zapa": "ORPHAN_ZAPA_X", "prises": 50,
         "geometry": Point(620, 610)},
        {"id_metier": "BAT/00005", "sro": SRO_CODE,
         "zapa": "ORPHAN_ZAPA_Y", "prises": 55,
         "geometry": Point(640, 580)},
    ])
    bal.to_file(OUT, layer="bal", driver="GPKG")

    # ------------------------------------------------------------------
    # georeso_pa + georeso_zapa -- 1 existing PA covering BAT/00001-2
    # ------------------------------------------------------------------
    georeso_pa = _gdf([
        {"id_metier": EXISTING_PA_ID, "sro": SRO_CODE,
         "geometry": Point(120, 120)},
    ])
    georeso_pa.to_file(OUT, layer="georeso_pa", driver="GPKG")

    georeso_zapa = _gdf([
        {"id_metier": EXISTING_ZAPA_ID, "sro": SRO_CODE,
         "geometry": Polygon([(80, 80), (200, 80), (200, 200), (80, 200)])},
    ])
    georeso_zapa.to_file(OUT, layer="georeso_zapa", driver="GPKG")

    # ------------------------------------------------------------------
    # FT arciti -- 2 segments. The 2nd is a MultiLineString to test
    # routing._explode_to_linestrings (regression PR #15).
    # ------------------------------------------------------------------
    multiline = MultiLineString([
        [(300, 100), (500, 100)],
        [(500, 100), (500, 300)],
    ])
    ft = _gdf([
        {"statut": "E", "mode_pose": 7,
         "geometry": LineString([(0, 100), (300, 100)])},
        {"statut": "E", "mode_pose": 7,
         "geometry": multiline},
    ])
    ft.to_file(OUT, layer="existant_ft_arciti", driver="GPKG")

    # ------------------------------------------------------------------
    # ATHD artere -- 1 segment, dispopp_ar >= 1 so it's reusable.
    # Goes through the orphan zone so D3 stays low.
    # ------------------------------------------------------------------
    athd = _gdf([
        {"dispopp_ar": 4,
         "geometry": LineString([(500, 600), (700, 600)])},
    ])
    athd.to_file(OUT, layer="existant_athd_artere", driver="GPKG")

    # ------------------------------------------------------------------
    # BT -- 1 aerial (kept).
    # ------------------------------------------------------------------
    bt = _gdf([
        {"type_de_lien": "Aerien",
         "geometry": LineString([(100, 200), (200, 200)])},
    ])
    bt.to_file(OUT, layer="existant_bt", driver="GPKG")

    # ------------------------------------------------------------------
    # Cheminement -- 1 reusable C7/TR.
    # ------------------------------------------------------------------
    chem = _gdf([
        {"cm_avct": "C", "cm_typ_imp": "7", "cm_typelog": "TR",
         "geometry": LineString([(200, 500), (800, 500)])},
    ])
    chem.to_file(OUT, layer="existant_t_cheminement", driver="GPKG")

    # ------------------------------------------------------------------
    # Parcelles -- 2 public (commune) + 3 private.
    # ------------------------------------------------------------------
    parcelle = _gdf([
        {"proprietaire": "Commune de Test",
         "geometry": Polygon([(80, 80), (200, 80), (200, 200), (80, 200)])},
        {"proprietaire": "COMMUNE DE TEST",
         "geometry": Polygon([(500, 500), (700, 500), (700, 700), (500, 700)])},
        {"proprietaire": "M Dupont",
         "geometry": Polygon([(300, 300), (400, 300), (400, 400), (300, 400)])},
        {"proprietaire": "Mme Martin",
         "geometry": Polygon([(700, 200), (800, 200), (800, 300), (700, 300)])},
        {"proprietaire": "SCI Test",
         "geometry": Polygon([(200, 700), (300, 700), (300, 800), (200, 800)])},
    ])
    parcelle.to_file(OUT, layer="parcelle", driver="GPKG")

    print(f"[OK] Built fixture: {OUT}")
    return OUT


if __name__ == "__main__":
    build()
