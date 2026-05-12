"""Materialise pipeline results as QGIS-ready GPKG layers.

Each call to ``write_sro_outputs`` appends one SRO worth of features to
the output GeoPackage.  The caller is responsible for truncating the
output file once at the start of a pipeline run (see ``main.py``).

PR #14: expanded to 8 output layers (was 6 in PR #13).
"""

from __future__ import annotations

import logging
import sqlite3
from pathlib import Path
from typing import Dict, List, Optional

import geopandas as gpd
import pandas as pd

from . import config

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Layer names (8 layers for PR #14)
# ---------------------------------------------------------------------------
LAYER_PA = "livrable_pa"
LAYER_ZAPA = "livrable_zapa"
LAYER_BAL = "livrable_bal"
LAYER_INFRA = "livrable_infra"
LAYER_PB = "livrable_pb"
LAYER_PARCELLES = "livrable_parcelles"
LAYER_FLAGS = "livrable_flags"
LAYER_ZASRO = "livrable_zasro"
LAYER_SRO = "livrable_sro"

# QML styles directory (official Pierre from Notion "QML Auvergne")
QML_DIR = Path(__file__).parent / "qml" / "officiel"

QML_MAPPING = {
    LAYER_PA: "livrable_pa_style.qml",
    LAYER_INFRA: "livrable_infra_style.qml",
    LAYER_ZAPA: "livrable_zapa_style.qml",
    LAYER_BAL: "bal_style.qml",
    LAYER_PARCELLES: "parcelle_style.qml",
    LAYER_ZASRO: "za_sro_style.qml",
}


# ---------------------------------------------------------------------------
# CRS enforcement helper (PR #32-hotfix)
# ---------------------------------------------------------------------------

def _ensure_crs(
    gdf: gpd.GeoDataFrame, target_crs: str
) -> Optional[gpd.GeoDataFrame]:
    """Force a GeoDataFrame to the target CRS before GPKG write."""
    if gdf is None or gdf.empty:
        return gdf
    if gdf.crs is None:
        return gdf.set_crs(target_crs)
    target_epsg = int(
        str(target_crs).split(":")[1] if ":" in str(target_crs) else target_crs
    )
    if gdf.crs.to_epsg() != target_epsg:
        return gdf.to_crs(target_crs)
    return gdf


# ---------------------------------------------------------------------------
# Layer CRS enforcement before write (PR #32-hotfix)
# ---------------------------------------------------------------------------

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
    za_sro: gpd.GeoDataFrame | None = None,  # Polygon ZASRO for the SRO
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

    # ---- 3. livrable_bal (BAL filtered by ZASRO) ────────────────────
    bal_out = bal.copy()
    bal_out = _ensure_crs(bal_out, crs)
    bal_out["sro_code"] = sro_code
    if not bal_out.empty:
        _ensure_layer_exists(bal_out, output_gpkg, LAYER_BAL, crs)
        counts[LAYER_BAL] = len(bal_out)

    # ---- 4. livrable_infra (existant + GC neuf) ─────────────────────
    if livrable_infra is not None and not livrable_infra.empty:
        infra_out = livrable_infra.copy()
        infra_out = _ensure_crs(infra_out, crs)
        if "sro_code" not in infra_out.columns:
            infra_out["sro_code"] = sro_code
        _ensure_layer_exists(infra_out, output_gpkg, LAYER_INFRA, crs)
        counts[LAYER_INFRA] = len(infra_out)

    # ---- 5. livrable_pb ─────────────────────────────────────────────
    if pb_fictifs is not None and not pb_fictifs.empty:
        pb_out = pb_fictifs.copy()
        pb_out = _ensure_crs(pb_out, crs)
        if "sro" not in pb_out.columns:
            pb_out["sro"] = sro_code
        _ensure_layer_exists(pb_out, output_gpkg, LAYER_PB, crs)
        counts[LAYER_PB] = len(pb_out)

    # ---- 6. livrable_parcelles (ALL, with is_public column) ─────────
    if parcelles is not None and not parcelles.empty:
        parc_out = parcelles.copy()
        parc_out = _ensure_crs(parc_out, crs)
        if "sro_code" not in parc_out.columns:
            parc_out["sro_code"] = sro_code
        if "is_public" not in parc_out.columns and "public" in parc_out.columns:
            parc_out["is_public"] = parc_out["public"]
        _ensure_layer_exists(parc_out, output_gpkg, LAYER_PARCELLES, crs)
        counts[LAYER_PARCELLES] = len(parc_out)

    # ---- 7. livrable_zasro (Polygon, 1 ligne par SRO) ──────────────
    if za_sro is not None and not za_sro.empty:
        zasro_out = za_sro.copy()
        zasro_out = _ensure_crs(zasro_out, crs)
        if "sro_code" not in zasro_out.columns:
            zasro_out["sro_code"] = sro_code
        _ensure_layer_exists(zasro_out, output_gpkg, LAYER_ZASRO, crs)
        counts[LAYER_ZASRO] = len(zasro_out)

    # ---- 7bis. livrable_sro (Point, 1 ligne par SRO) ───────────────
    # representative_point = toujours dans le polygone, robuste aux ZASRO
    # non-convexes (meilleur que centroid).
    if za_sro is not None and not za_sro.empty:
        sro_pt_geom = za_sro.geometry.iloc[0].representative_point()
        sro_attrs = {
            "sro_code": sro_code,
            "nom_nro": za_sro.iloc[0].get("nom_nro", ""),
            "geometry": sro_pt_geom,
        }
        sro_gdf = gpd.GeoDataFrame([sro_attrs], geometry="geometry", crs=crs)
        _ensure_layer_exists(sro_gdf, output_gpkg, LAYER_SRO, crs)
        counts[LAYER_SRO] = 1

    # ---- 8. livrable_flags (non-spatial table) ──────────────────────
    flags_df = flag_collector.to_dataframe()
    if not flags_df.empty:
        flags_df["sro_code"] = sro_code
        try:
            conn = sqlite3.connect(str(output_gpkg))
            flags_df.to_sql(LAYER_FLAGS, conn, if_exists="append", index=False)
            conn.close()
            counts[LAYER_FLAGS] = len(flags_df)
        except Exception:
            log.warning("Ecriture flags impossible, skip", exc_info=True)

    # ---- log recap ----------------------------------------------------
    log.info(
        "[INFO] %s writer : pa=%d zapa=%d bal=%d infra=%d pb=%d parcelles=%d zasro=%d sro=%d flags=%d",
        sro_code,
        counts.get(LAYER_PA, 0), counts.get(LAYER_ZAPA, 0),
        counts.get(LAYER_BAL, 0), counts.get(LAYER_INFRA, 0),
        counts.get(LAYER_PB, 0), counts.get(LAYER_PARCELLES, 0),
        counts.get(LAYER_ZASRO, 0), counts.get(LAYER_SRO, 0),
        counts.get(LAYER_FLAGS, 0),
    )
    return counts


# ---------------------------------------------------------------------------
# QML style injection into GPKG layer_styles table
# ---------------------------------------------------------------------------


def apply_qml_styles_to_gpkg(gpkg_path: Path) -> None:
    """Inject QML styles into the layer_styles table of the GPKG.

    QGIS reads this table on layer open and applies the style automatically.
    Schema: https://docs.qgis.org/latest/en/docs/user_manual/managing_data_source\
/create_layers.html#geopackage-styles
    """
    conn = sqlite3.connect(str(gpkg_path))
    cur = conn.cursor()
    # Create layer_styles table if missing (QGIS schema)
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS layer_styles (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            f_table_catalog TEXT,
            f_table_schema TEXT,
            f_table_name TEXT,
            f_geometry_column TEXT,
            styleName TEXT,
            styleQML TEXT,
            styleSLD TEXT,
            useAsDefault BOOLEAN,
            description TEXT,
            owner TEXT,
            ui TEXT,
            update_time DATETIME DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    applied = 0
    for layer_name, qml_filename in QML_MAPPING.items():
        qml_path = QML_DIR / qml_filename
        if not qml_path.exists():
            log.warning(
                "[QML] Fichier introuvable: %s — style non applique", qml_path
            )
            continue
        qml_content = qml_path.read_text(encoding="utf-8")
        # Upsert: remove previous style for this layer if present
        # (idempotent — safe to call multiple times / multi-SRO runs)
        cur.execute(
            "DELETE FROM layer_styles WHERE f_table_name = ? AND styleName = ?",
            (layer_name, layer_name + "_style"),
        )
        cur.execute(
            """
            INSERT INTO layer_styles
              (f_table_name, f_geometry_column, styleName, styleQML, useAsDefault)
            VALUES (?, 'geom', ?, ?, 1)
            """,
            (layer_name, layer_name + "_style", qml_content),
        )
        applied += 1
    conn.commit()
    conn.close()
    log.info("[QML] %d styles officiels appliques au GPKG", applied)


# ---------------------------------------------------------------------------
# QML sidecars — .qml files alongside the GPKG for QGIS auto-load
# ---------------------------------------------------------------------------


def write_qml_sidecars(gpkg_path: Path) -> int:
    """DEPRECATED (PR #21) — sidecars .qml ne fonctionnent pas pour GPKG multi-layer.

    La convention QGIS <gpkg>_<layer>.qml ne s'applique qu'aux Shapefiles.
    Pour un GPKG multi-couches, utiliser :
    1. La table interne layer_styles avec useAsDefault=1
    2. Un projet .qgz (``write_qgis_project``) qui charge toutes les couches
    """
    log.debug("[QML sidecar] Deprecated — sidecars inutiles sur GPKG, utiliser .qgz")
    return 0


# ---------------------------------------------------------------------------
# Pre-styled .qgz project for auto-loading all layers at once
# ---------------------------------------------------------------------------


def write_qgis_project(gpkg_path: Path) -> Path | None:
    """Generate a .qgz alongside the GPKG that pre-loads all 9 livrable
    layers with official QML styles in correct stacking order.

    Pierre double-clicks the .qgz → QGIS opens with everything styled.
    """
    try:
        from qgis.core import QgsProject, QgsVectorLayer, QgsApplication
    except ImportError:
        log.warning("[QGZ] qgis.core non disponible — projet .qgz non genere")
        return None

    # Init QGIS app if not already running
    if not QgsApplication.instance():
        qgs = QgsApplication([], False)
        qgs.initQgis()

    project = QgsProject.instance()
    project.clear()

    qgz_path = gpkg_path.with_suffix(".qgz")
    project.setFileName(str(qgz_path))

    # Stacking order: background → foreground
    LAYER_ORDER = [
        ("livrable_parcelles", "parcelle_style.qml"),
        ("livrable_zasro", "za_sro_style.qml"),
        ("livrable_zapa", "livrable_zapa_style.qml"),
        ("livrable_infra", "livrable_infra_style.qml"),
        ("livrable_bal", "bal_style.qml"),
        ("livrable_pb", None),   # no official QML for PB fictif
        ("livrable_pa", "livrable_pa_style.qml"),
        ("livrable_sro", None),   # no official QML for SRO point
        ("livrable_flags", None),
    ]

    gpkg_uri = str(gpkg_path.resolve())
    loaded = 0
    for layer_name, qml_name in LAYER_ORDER:
        uri = f"{gpkg_uri}|layername={layer_name}"
        layer = QgsVectorLayer(uri, layer_name, "ogr")
        if not layer.isValid():
            log.warning("[QGZ] Layer invalide: %s", layer_name)
            continue
        if qml_name:
            qml_path = QML_DIR / qml_name
            if qml_path.exists():
                layer.loadNamedStyle(str(qml_path))
        project.addMapLayer(layer)
        loaded += 1

    project.write()
    log.info("[QGZ] Projet QGIS pre-style ecrit: %s (%d couches)", qgz_path.name, loaded)
    return qgz_path