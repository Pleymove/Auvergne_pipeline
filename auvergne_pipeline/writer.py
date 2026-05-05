"""Materialise pipeline results as QGIS-ready GPKG layers.

Each call to ``write_sro_outputs`` appends one SRO worth of features to
the output GeoPackage.  The caller is responsible for truncating the
output file once at the start of a pipeline run (see ``main.py``).

PR #14: expanded to 8 output layers (was 6 in PR #13).
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Dict, List

import geopandas as gpd
import pandas as pd

from . import config

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Layer names (8 layers for PR #14)
# ---------------------------------------------------------------------------
LAYER_PA = "livrable_pa"
LAYER_ZAPA = "livrable_zapa"
LAYER_BAT = "livrable_bat"
LAYER_INFRA = "livrable_infra"
LAYER_PB = "livrable_pb"
LAYER_PARCELLES = "livrable_parcelles"
LAYER_FLAGS = "livrable_flags"


def _ensure_layer_exists(
    gdf: gpd.GeoDataFrame, gpkg_path: Path, layer: str, crs: str
) -> None:
    """Write a GeoDataFrame to a GPKG layer (append, create if missing)."""
    if gdf is None or gdf.empty:
        return
    try:
        gdf.to_file(gpkg_path, layer=layer, driver="GPKG", mode="a")
    except ValueError:
        gdf.to_file(gpkg_path, layer=layer, driver="GPKG")


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def write_sro_outputs(
    sro_code: str,
    output_gpkg: Path,
    *,
    bal: gpd.GeoDataFrame,
    georeso_pa_existants: gpd.GeoDataFrame,
    georeso_zapa_existantes: gpd.GeoDataFrame,
    new_pas: List[dict],
    new_zapas: List[dict],
    pb_fictifs: gpd.GeoDataFrame,
    livrable_infra: gpd.GeoDataFrame,
    parcelles: gpd.GeoDataFrame,  # ALL parcels with is_public column
    flag_collector,  # : flags_mod.FlagCollector
    crs: str = config.PROJECT_CRS,
) -> Dict[str, int]:
    """Write all 8 livrable layers for one SRO into *output_gpkg* (append)."""
    output_gpkg.parent.mkdir(parents=True, exist_ok=True)
    counts: Dict[str, int] = {}

    # ---- 1. livrable_pa (existing + created) ----------------------------
    pa_rows: List[dict] = []

    if georeso_pa_existants is not None and not georeso_pa_existants.empty:
        for _, pa in georeso_pa_existants.iterrows():
            pa_id = pa.get("id_metier", f"pa#{pa.name}")
            pa_rows.append({
                "pa_id": pa_id, "sro": sro_code, "origine": "existant",
                "snap_source": "", "nb_bat": 0, "nb_prises": 0,
                "geometry": pa.geometry,
            })

    for pa in new_pas:
        pa_rows.append({
            "pa_id": pa.get("id_metier", f"pa#{pa.get('origine')}"),
            "sro": sro_code, "origine": "cree",
            "snap_source": pa.get("snap_source", ""),
            "nb_bat": pa.get("n_bat", 0),
            "nb_prises": pa.get("total_prises", 0),
            "geometry": pa["geometry"],
        })

    if pa_rows:
        pa_gdf = gpd.GeoDataFrame(pa_rows, geometry="geometry", crs=crs)
        _ensure_layer_exists(pa_gdf, output_gpkg, LAYER_PA, crs)
        counts[LAYER_PA] = len(pa_rows)

    # ---- 2. livrable_zapa (existing + created) --------------------------
    zapa_rows: List[dict] = []
    for _, z in georeso_zapa_existantes.iterrows():
        zapa_rows.append({
            "zapa_id": z.get("id_metier", f"zapa#{z.name}"),
            "sro": sro_code, "origine": "existant", "geometry": z.geometry,
        })
    for z in new_zapas:
        zapa_rows.append({
            "zapa_id": z.get("id_metier", f"zapa#{z.get('origine')}"),
            "sro": sro_code, "origine": "cree", "geometry": z["geometry"],
        })

    if zapa_rows:
        zapa_gdf = gpd.GeoDataFrame(zapa_rows, geometry="geometry", crs=crs)
        _ensure_layer_exists(zapa_gdf, output_gpkg, LAYER_ZAPA, crs)
        counts[LAYER_ZAPA] = len(zapa_rows)

    # ---- 3. livrable_bat ────────────────────────────────────────────
    bat_out = bal.copy()
    bat_out["sro_code"] = sro_code
    bat_out["pa_rattache"] = ""
    if not bat_out.empty:
        _ensure_layer_exists(bat_out, output_gpkg, LAYER_BAT, crs)
        counts[LAYER_BAT] = len(bat_out)

    # ---- 4. livrable_infra (existant + GC neuf) ─────────────────────
    if livrable_infra is not None and not livrable_infra.empty:
        infra_out = livrable_infra.copy()
        if "sro_code" not in infra_out.columns:
            infra_out["sro_code"] = sro_code
        _ensure_layer_exists(infra_out, output_gpkg, LAYER_INFRA, crs)
        counts[LAYER_INFRA] = len(infra_out)

    # ---- 5. livrable_pb ─────────────────────────────────────────────
    if pb_fictifs is not None and not pb_fictifs.empty:
        pb_out = pb_fictifs.copy()
        if "sro" not in pb_out.columns:
            pb_out["sro"] = sro_code
        _ensure_layer_exists(pb_out, output_gpkg, LAYER_PB, crs)
        counts[LAYER_PB] = len(pb_out)

    # ---- 6. livrable_parcelles (ALL, with is_public column) ─────────
    if parcelles is not None and not parcelles.empty:
        parc_out = parcelles.copy()
        if "sro_code" not in parc_out.columns:
            parc_out["sro_code"] = sro_code
        if "is_public" not in parc_out.columns and "public" in parc_out.columns:
            parc_out["is_public"] = parc_out["public"]
        _ensure_layer_exists(parc_out, output_gpkg, LAYER_PARCELLES, crs)
        counts[LAYER_PARCELLES] = len(parc_out)

    # ---- 7. livrable_flags (non-spatial table) ──────────────────────
    flags_df = flag_collector.to_dataframe()
    if not flags_df.empty:
        flags_df["sro_code"] = sro_code
        try:
            import sqlite3
            conn = sqlite3.connect(str(output_gpkg))
            flags_df.to_sql(LAYER_FLAGS, conn, if_exists="append", index=False)
            conn.close()
            counts[LAYER_FLAGS] = len(flags_df)
        except Exception:
            log.warning("Ecriture flags impossible, skip", exc_info=True)

    # ---- log recap ----------------------------------------------------
    log.info(
        "[INFO] %s writer : pa=%d zapa=%d bat=%d infra=%d pb=%d parcelles=%d flags=%d",
        sro_code,
        counts.get(LAYER_PA, 0), counts.get(LAYER_ZAPA, 0),
        counts.get(LAYER_BAT, 0), counts.get(LAYER_INFRA, 0),
        counts.get(LAYER_PB, 0), counts.get(LAYER_PARCELLES, 0),
        counts.get(LAYER_FLAGS, 0),
    )
    return counts