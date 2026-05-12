"""Tests for orphans.detect_orphans + create_pa_for_orphans (PR #12 algorithm).

Key changes from iteration 2:
  - PA snapping to existing infra (cheminement / ATHD).
  - Fallback to public parcels when no infra nearby.
  - Cluster gap 7000 m (was 200 m).
  - Small cluster merging (< 50 prises).
  - K-means subdivision when cluster > 120 prises.
"""

from __future__ import annotations

import geopandas as gpd
import numpy as np
import pandas as pd
from shapely.geometry import LineString, Point, Polygon

from auvergne_pipeline import config, flags as flags_mod, orphans


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

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


def _cheminement_near(center: Point) -> gpd.GeoDataFrame:
    """Single cheminement line near the given point."""
    line = LineString([
        (center.x + 10, center.y + 10),
        (center.x + 110, center.y + 110),
    ])
    df = pd.DataFrame({"src": ["chem"], "geometry": [line]})
    return gpd.GeoDataFrame(df, geometry="geometry", crs=config.PROJECT_CRS)


def _public_parcels(center: Point) -> gpd.GeoDataFrame:
    """Single public parcel near the given point."""
    poly = center.buffer(300)
    df = pd.DataFrame(
        {"id_metier": ["P_PUB"], "proprietaire": ["Commune de Test"], "public": [True],
         "geometry": [poly]}
    )
    return gpd.GeoDataFrame(df, geometry="geometry", crs=config.PROJECT_CRS)


# ---------------------------------------------------------------------------
# detect_orphans (unchanged logic)
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# create_pa_for_orphans — basic cases
# ---------------------------------------------------------------------------

def test_create_pa_empty_returns_nothing():
    empty = gpd.GeoDataFrame(
        {"id_metier": [], "zapa": [], "prises": [], "geometry": []},
        geometry="geometry", crs=config.PROJECT_CRS,
    )
    pa_rows, zapa_rows = orphans.create_pa_for_orphans(
        empty, sro_code="63149/M06/PMZ/42478"
    )
    assert pa_rows == [] and zapa_rows == []


def test_create_pa_single_cluster_single_pa():
    """2 orphan BATs close together (< 7000 m) -> 1 PA."""
    bal = _bal()
    zapa = _zapa_referential()
    orphans_gdf = orphans.detect_orphans(bal, zapa)

    # Cheminement near the centroid
    centroid = orphans._weighted_centroid(
        [Point(10, 10), Point(11, 11)], [4, 2]
    )
    chem = _cheminement_near(centroid)

    fc = flags_mod.FlagCollector("63149/M06/PMZ/42478")
    pa_rows, zapa_rows = orphans.create_pa_for_orphans(
        orphans_gdf,
        sro_code="63149/M06/PMZ/42478",
        cheminement_lines=chem,
        flag_collector=fc,
    )

    assert len(pa_rows) == 1
    assert len(zapa_rows) == 1
    assert pa_rows[0]["id_metier"] == "63149/M06/PA/99001"
    assert pa_rows[0]["snap_source"] == "cheminement"
    assert pa_rows[0]["n_bat"] == 2
    assert pa_rows[0]["total_prises"] == 6
    # PA should be snapped to the cheminement, NOT at raw centroid
    assert pa_rows[0]["geometry"].distance(centroid) > 0.1
    assert fc.counts().get("PA_ORPHELIN_CREE") == 1


def test_create_pa_fallback_to_public_parcel_when_no_infra():
    """No cheminement/ATHD nearby -> snap to public parcel."""
    orphans_gdf = gpd.GeoDataFrame(
        pd.DataFrame({
            "id_metier": ["BAT_A"],
            "zapa": ["Z_UNKNOWN"],
            "prises": [1],
            "geometry": [Point(100, 100)],
        }),
        geometry="geometry", crs=config.PROJECT_CRS,
    )

    pub = _public_parcels(Point(100, 100))
    fc = flags_mod.FlagCollector("test/SRO/PMZ/1")
    pa_rows, _ = orphans.create_pa_for_orphans(
        orphans_gdf,
        sro_code="test/SRO/PMZ/1",
        parcelles_classifiees=pub,
        flag_collector=fc,
    )

    assert len(pa_rows) == 1
    assert pa_rows[0]["snap_source"] == "parcelle_publique"
    assert fc.counts().get("PA_PLACEMENT_INCERTAIN", 0) == 1


def test_create_pa_impossible_when_no_infra_no_public_parcel():
    """No infra and no public parcel within radius -> placement impossible."""
    orphans_gdf = gpd.GeoDataFrame(
        pd.DataFrame({
            "id_metier": ["BAT_X"],
            "zapa": ["Z_UNKNOWN"],
            "prises": [1],
            "geometry": [Point(5000, 5000)],
        }),
        geometry="geometry", crs=config.PROJECT_CRS,
    )

    fc = flags_mod.FlagCollector("test/SRO/PMZ/1")
    pa_rows, _ = orphans.create_pa_for_orphans(
        orphans_gdf,
        sro_code="test/SRO/PMZ/1",
        flag_collector=fc,
    )

    assert len(pa_rows) == 1
    assert pa_rows[0]["snap_source"] == "aucune_infra"
    assert fc.counts().get("PA_PLACEMENT_IMPOSSIBLE", 0) == 1


# ---------------------------------------------------------------------------
# Spatial clustering (7000 m gap)
# ---------------------------------------------------------------------------

def test_spatial_clustering_7000m_gap():
    """BATs within 7000 m are merged; beyond are separate."""
    df = pd.DataFrame({
        "id_metier": ["A", "B", "C"],
        "zapa": [None, None, None],
        "prises": [1, 1, 1],
        "geometry": [Point(0, 0), Point(3000, 0), Point(8000, 0)],
    })
    bat = gpd.GeoDataFrame(df, geometry="geometry", crs=config.PROJECT_CRS)

    pa_rows, _ = orphans.create_pa_for_orphans(
        bat, sro_code="63149/M06/PMZ/42478"
    )
    # A and B are 3000 m apart -> same cluster (1 PA).
    # C is 5000 m from B, 8000 m from A -> falls under 7000 m gap with B -> same cluster.
    # So 1 PA total.
    assert len(pa_rows) == 1


def test_spatial_clustering_beyond_7000m():
    """BATs > 7000 m apart -> separate clusters."""
    df = pd.DataFrame({
        "id_metier": ["A", "B"],
        "zapa": [None, None],
        "prises": [1, 1],
        "geometry": [Point(0, 0), Point(8000, 0)],
    })
    bat = gpd.GeoDataFrame(df, geometry="geometry", crs=config.PROJECT_CRS)

    pa_rows, _ = orphans.create_pa_for_orphans(
        bat, sro_code="63149/M06/PMZ/42478"
    )
    # 8000 m > 7000 m -> 2 separate clusters
    assert len(pa_rows) == 2


# ---------------------------------------------------------------------------
# Small cluster merging (< 50 prises)
# ---------------------------------------------------------------------------

def test_merge_small_cluster_into_nearest():
    """A cluster with < 50 prises merges into the nearest larger cluster."""
    df = pd.DataFrame({
        "id_metier": ["BIG"] + [f"T{i}" for i in range(60)],
        "zapa": [None] * 61,
        # BIG: 51 prises. 60 small BATs: 1 prise each = 60 prises total but in 2 clusters
        "prises": [51] + [1] * 60,
        "geometry": [Point(0, 0)] + [Point(8000 + i * 10, 0) for i in range(60)],
    })
    bat = gpd.GeoDataFrame(df, geometry="geometry", crs=config.PROJECT_CRS)

    # The 60 small BATs at x=8000+ are in one cluster (< 7000m apart from each other).
    # That cluster has 60 prises -> >= 50, so it stays.
    # The BIG cluster has 51 prises -> >= 50.
    # Gap between BIG (0,0) and small cluster (8000,0) is 8000m > 7000m.
    # So we should get 2 PAs, both with >= 50 prises.
    pa_rows, _ = orphans.create_pa_for_orphans(
        bat, sro_code="63149/M06/PMZ/42478"
    )
    assert len(pa_rows) == 2


# ---------------------------------------------------------------------------
# K-means subdivision (> 120 prises)
# ---------------------------------------------------------------------------

def test_kmeans_subdivision_when_over_120_prises():
    """A cluster with > 120 prises is split into multiple PAs."""
    n_bat = 125  # 125 BATs, 1 prise each = 125 total > 120
    df = pd.DataFrame({
        "id_metier": [f"B{i}" for i in range(n_bat)],
        "zapa": [None] * n_bat,
        "prises": [1] * n_bat,
        "geometry": [Point(i * 10, 0) for i in range(n_bat)],  # spread 1250 m
    })
    bat = gpd.GeoDataFrame(df, geometry="geometry", crs=config.PROJECT_CRS)

    pa_rows, _ = orphans.create_pa_for_orphans(
        bat, sro_code="63149/M06/PMZ/42478"
    )
    # 125 > 120 -> ceil(125/120) = 2 PAs minimum
    assert len(pa_rows) >= 2


# ---------------------------------------------------------------------------
# PA IDs are sequential
# ---------------------------------------------------------------------------

def test_pa_ids_are_sequential():
    df = pd.DataFrame({
        "id_metier": ["A", "B"],
        "zapa": [None, None],
        "prises": [1, 1],
        "geometry": [Point(0, 0), Point(8000, 0)],
    })
    bat = gpd.GeoDataFrame(df, geometry="geometry", crs=config.PROJECT_CRS)

    pa_rows, _ = orphans.create_pa_for_orphans(
        bat, sro_code="63149/M06/PMZ/42478", start_id=99010
    )
    ids = {r["id_metier"] for r in pa_rows}
    assert "63149/M06/PA/99010" in ids
    assert "63149/M06/PA/99011" in ids


# ---------------------------------------------------------------------------
# Edge case: None inputs don't crash
# ---------------------------------------------------------------------------

def test_create_pa_handles_none_infra():
    orphan = gpd.GeoDataFrame(
        pd.DataFrame({
            "id_metier": ["X"],
            "zapa": ["XX"],
            "prises": [1],
            "geometry": [Point(50, 50)],
        }),
        geometry="geometry", crs=config.PROJECT_CRS,
    )
    # All optional params = None -> no crash, PA_PLACEMENT_IMPOSSIBLE
    fc = flags_mod.FlagCollector("test/SRO/PMZ/1")
    pa_rows, _ = orphans.create_pa_for_orphans(
        orphan, sro_code="test/SRO/PMZ/1",
        cheminement_lines=None,
        athd_lines=None,
        parcelles_classifiees=None,
        flag_collector=fc,
    )
    assert len(pa_rows) == 1
    assert pa_rows[0]["snap_source"] == "aucune_infra"