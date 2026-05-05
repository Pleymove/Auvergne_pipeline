"""Materialise pipeline results as QGIS-ready GPKG layers.

Each call to ``write_sro_outputs`` appends one SRO worth of features to
the output GeoPackage.  The caller is responsible for truncating the
output file once at the start of a pipeline run (see ``main.py``).
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
# Layer names (exposed for tests / external consumers)
# ---------------------------------------------------------------------------
LAYER_PA = "livrable_pa"
LAYER_ZAPA = "livrable_zapa"
LAYER_BAT = "livrable_bat"
LAYER_INFRA_REUTILISABLE = "livrable_infra_reutilisable"
LAYER_PARCELLES_PUBLIQUES = "livrable_parcelles_publiques"
LAYER_FLAGS = "livrable_flags"


def _ensure_layer_exists(
    gdf: gpd.GeoDataFrame,
    gpkg_path: Path,
    layer: str,
    crs: str,
) -> None:
    """Write a GeoDataFrame to a GPKG layer (append, create if missing)."""
    if gdf is None or gdf.empty:
        return
    try:
        gdf.to_file(gpkg_path, layer=layer, driver="GPKG", mode="a")
    except ValueError:
        # Layer might not exist yet — create it
        gdf.to_file(gpkg_path, layer=layer, driver="GPKG")


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def write_sro_outputs(
    sro_code: str,
    output_gpkg: Path,
    *,
    bal: gpd.GeoDataFrame,
    d3_results: List[dict],
    georeso_pa_existants: gpd.GeoDataFrame,
    georeso_zapa_existantes: gpd.GeoDataFrame,
    new_pas: List[dict],
    new_zapas: List[dict],
    reusable_infra: gpd.GeoDataFrame,
    parcelles_classifiees: gpd.GeoDataFrame,
    flag_collector,  # : flags_mod.FlagCollector
    crs: str = config.PROJECT_CRS,
) -> Dict[str, int]:
    """Write all livrable layers for one SRO into *output_gpkg* (append).

    Returns a dict ``{layer_name: feature_count}``.
    """
    output_gpkg.parent.mkdir(parents=True, exist_ok=True)
    counts: Dict[str, int] = {}

    # ---- 1. livrable_pa (existing + created) ----------------------------
    pa_rows: List[dict] = []

    # a) Existing PAs (from georeso_pa)
    if georeso_pa_existants is not None and not georeso_pa_existants.empty:
        # Count BAT per PA
        bat_zapa = bal.get("zapa", pd.Series(dtype=str))
        # georeso_zapa maps id_metier -> PA id_metier
        pa_ids: set[str] = set()
        for _, pa in georeso_pa_existants.iterrows():
            pa_id = pa.get("id_metier", f"pa#{pa.name}")
            pa_ids.add(pa_id)
            pa_rows.append(
                {
                    "pa_id": pa_id,
                    "sro": sro_code,
                    "origine": "existant",
                    "snap_source": "",
                    "nb_bat": 0,  # filled below
                    "nb_prises": 0,  # filled below
                    "geometry": pa.geometry,
                }
            )

    # b) Created PAs (orphans)
    for pa in new_pas:
        pa_rows.append(
            {
                "pa_id": pa.get("id_metier", f"pa#{pa.get('origine')}"),
                "sro": sro_code,
                "origine": "cree",
                "snap_source": pa.get("snap_source", ""),
                "nb_bat": pa.get("n_bat", 0),
                "nb_prises": pa.get("total_prises", 0),
                "geometry": pa["geometry"],
            }
        )

    if pa_rows:
        pa_gdf = gpd.GeoDataFrame(pa_rows, geometry="geometry", crs=crs)
        _ensure_layer_exists(pa_gdf, output_gpkg, LAYER_PA, crs)
        counts[LAYER_PA] = len(pa_rows)

    # ---- 2. livrable_zapa (existing + created) --------------------------
    zapa_rows: List[dict] = []
    for _, z in georeso_zapa_existantes.iterrows():
        zapa_rows.append(
            {
                "zapa_id": z.get("id_metier", f"zapa#{z.name}"),
                "sro": sro_code,
                "origine": "existant",
                "geometry": z.geometry,
            }
        )
    for z in new_zapas:
        zapa_rows.append(
            {
                "zapa_id": z.get("id_metier", f"zapa#{z.get('origine')}"),
                "sro": sro_code,
                "origine": "cree",
                "geometry": z["geometry"],
            }
        )

    if zapa_rows:
        zapa_gdf = gpd.GeoDataFrame(zapa_rows, geometry="geometry", crs=crs)
        _ensure_layer_exists(zapa_gdf, output_gpkg, LAYER_ZAPA, crs)
        counts[LAYER_ZAPA] = len(zapa_rows)

    # ---- 3. livrable_bat (enriched with D3 results) ---------------------
    bat_out = bal.copy()
    bat_out["sro_code"] = sro_code

    # Join D3 results by bat_url (id_metier)
    d3_map: dict[str, dict] = {}
    for d in d3_results:
        bu = d.get("bat_url", "")
        if bu:
            d3_map[bu] = d

    bat_out["d3_m"] = None
    bat_out["cls"] = "TO_CREATE"
    bat_out["parcel_url"] = ""

    for i, (_, bat) in enumerate(bal.iterrows()):
        bu = bat.get("id_metier", f"bat#{i}")
        if bu in d3_map:
            d = d3_map[bu]
            bat_out.at[bat.name, "d3_m"] = d.get("d3")
            bat_out.at[bat.name, "cls"] = d.get("cls", "TO_CREATE")
            bat_out.at[bat.name, "parcel_url"] = d.get("parcel_url", "")

    bat_out["pa_rattache"] = ""  # placeholder for PR #14 (routing)

    if not bat_out.empty:
        _ensure_layer_exists(bat_out, output_gpkg, LAYER_BAT, crs)
        counts[LAYER_BAT] = len(bat_out)

    # ---- 4. livrable_infra_reutilisable ---------------------------------
    if reusable_infra is not None and not reusable_infra.empty:
        infra_out = reusable_infra.copy()
        infra_out["sro_code"] = sro_code
        _ensure_layer_exists(infra_out, output_gpkg, LAYER_INFRA_REUTILISABLE, crs)
        counts[LAYER_INFRA_REUTILISABLE] = len(infra_out)

    # ---- 5. livrable_parcelles_publiques ---------------------------------
    if parcelles_classifiees is not None and not parcelles_classifiees.empty:
        is_pub = parcelles_classifiees.get("public", pd.Series(dtype=bool))
        pub = parcelles_classifiees[is_pub].copy()
        if not pub.empty:
            pub["sro_code"] = sro_code
            _ensure_layer_exists(pub, output_gpkg, LAYER_PARCELLES_PUBLIQUES, crs)
            counts[LAYER_PARCELLES_PUBLIQUES] = len(pub)

    # ---- 6. livrable_flags (non-spatial table) ---------------------------
    flags_df = flag_collector.to_dataframe()
    if not flags_df.empty:
        flags_df["sro_code"] = sro_code
        # Non-spatial DataFrame: write as table
        try:
            import sqlite3

            conn = sqlite3.connect(str(output_gpkg))
            flags_df.to_sql(LAYER_FLAGS, conn, if_exists="append", index=False)
            conn.close()
            counts[LAYER_FLAGS] = len(flags_df)
        except Exception:
            log.warning("Ecriture flags impossible, skip", exc_info=True)

    # ---- log recap -------------------------------------------------------
    log.info(
        "[INFO] %s writer : pa=%d zapa=%d bat=%d infra=%d parcelles_pub=%d flags=%d",
        sro_code,
        counts.get(LAYER_PA, 0),
        counts.get(LAYER_ZAPA, 0),
        counts.get(LAYER_BAT, 0),
        counts.get(LAYER_INFRA_REUTILISABLE, 0),
        counts.get(LAYER_PARCELLES_PUBLIQUES, 0),
        counts.get(LAYER_FLAGS, 0),
    )

    return counts
