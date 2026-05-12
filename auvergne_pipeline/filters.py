"""Reusable infrastructure filters (4 sources -> 1 GeoDataFrame).

Rules consolidated from the CDC + QGIS symbology validation (Q1-Q16). Each
filter is small and pure so it can be tested in isolation; ``build_reusable_infra``
is the orchestrator the rest of the pipeline calls.
"""

from __future__ import annotations

from typing import Mapping

import geopandas as gpd
import pandas as pd

from . import config


def _empty_like(gdf: gpd.GeoDataFrame) -> gpd.GeoDataFrame:
    """Return an empty GeoDataFrame with the same columns / CRS as ``gdf``."""
    return gdf.iloc[0:0].copy()


def filter_athd(gdf: gpd.GeoDataFrame) -> gpd.GeoDataFrame:
    """ATHD arteres: keep rows whose available occupation count >= 1."""
    if gdf.empty or "dispopp_ar" not in gdf.columns:
        return _empty_like(gdf)
    dispo = pd.to_numeric(gdf["dispopp_ar"], errors="coerce").fillna(0)
    return gdf[dispo >= 1].copy()


def filter_bt(gdf: gpd.GeoDataFrame) -> gpd.GeoDataFrame:
    """ENEDIS BT: drop buried cables (only aerial / facade segments are reusable).

    Implementation note: the embedded QGIS pyarrow lacks ``match_substring_regex``,
    which pandas calls when ``str.contains`` is invoked with ``case=False`` (or
    any regex) on an arrow-backed string column. We force a Python-object dtype
    and lowercase the column ourselves, then use ``regex=False`` so pandas only
    needs ``match_substring`` (which is available).
    """
    if gdf.empty or "type_de_lien" not in gdf.columns:
        return _empty_like(gdf)
    tdl = gdf["type_de_lien"].astype("object").fillna("").str.lower()
    buried = (
        tdl.str.contains("cable enterre", regex=False, na=False)
        | tdl.str.contains("cable enterree", regex=False, na=False)
        | tdl.str.contains("câble enterré", regex=False, na=False)
    )
    return gdf[~buried].copy()


def filter_ft_arciti(gdf: gpd.GeoDataFrame) -> gpd.GeoDataFrame:
    """Orange PIT communal: drop pleine-terre (statut == 'E' AND mode_pose == 4)."""
    if gdf.empty:
        return _empty_like(gdf)
    if "statut" not in gdf.columns or "mode_pose" not in gdf.columns:
        return gdf.copy()
    mode = pd.to_numeric(gdf["mode_pose"], errors="coerce")
    excl = (gdf["statut"].astype(str) == "E") & (mode == 4)
    return gdf[~excl].copy()


def filter_cheminement(gdf: gpd.GeoDataFrame) -> gpd.GeoDataFrame:
    """RIP NGE cheminement: built transport GC (C7) with typelog in {TR, TD}."""
    if gdf.empty:
        return _empty_like(gdf)
    needed = {"cm_avct", "cm_typ_imp", "cm_typelog"}
    if not needed.issubset(gdf.columns):
        return _empty_like(gdf)
    code = gdf["cm_avct"].fillna("").astype(str) + gdf["cm_typ_imp"].fillna("").astype(str)
    mask = (code == "C7") & gdf["cm_typelog"].isin(["TR", "TD"])
    return gdf[mask].copy()


def _tag(gdf: gpd.GeoDataFrame, **assignments) -> gpd.GeoDataFrame:
    """Return ``gdf`` with extra columns; preserves CRS / geometry."""
    if gdf.empty:
        out = gdf.copy()
        for col, value in assignments.items():
            out[col] = pd.Series(dtype=object)
            del value  # keep mypy happy when branch unused
        return out
    return gdf.assign(**assignments)


def build_reusable_infra(
    layers: Mapping[str, gpd.GeoDataFrame],
) -> gpd.GeoDataFrame:
    """Concat the 4 filtered infra layers into a single GeoDataFrame.

    Each row is tagged with its provenance (``src``) plus the default
    ``mode_pose`` / ``statut`` expected on the livrable side, so the rest of
    the pipeline can stay source-agnostic.
    """
    pieces: list[gpd.GeoDataFrame] = []

    athd = layers.get(config.LAYER_ATHD)
    if athd is not None:
        pieces.append(_tag(filter_athd(athd), src="athd", mode_pose=7, statut="E"))

    bt = layers.get(config.LAYER_BT)
    if bt is not None:
        pieces.append(_tag(filter_bt(bt), src="bt", mode_pose=1, statut="E"))

    ft = layers.get(config.LAYER_FT_ARCITI)
    if ft is not None:
        pieces.append(_tag(filter_ft_arciti(ft), src="ft"))

    chem = layers.get(config.LAYER_CHEMINEMENT)
    if chem is not None:
        pieces.append(
            _tag(filter_cheminement(chem), src="chem", mode_pose="C7", statut="E")
        )

    pieces = [p for p in pieces if not p.empty]
    if not pieces:
        # Return an empty GeoDataFrame in the project CRS so downstream code
        # can call ``.sindex`` without special-casing.
        return gpd.GeoDataFrame(geometry=[], crs=config.PROJECT_CRS)

    target_crs = pieces[0].crs or config.PROJECT_CRS
    pieces = [p.to_crs(target_crs) if p.crs and p.crs != target_crs else p for p in pieces]
    return gpd.GeoDataFrame(
        pd.concat(pieces, ignore_index=True), geometry="geometry", crs=target_crs
    )
