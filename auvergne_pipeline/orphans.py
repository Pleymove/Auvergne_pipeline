"""Orphan-BAT detection + new PA creation at cluster barycenter.

A BAT is "orphan" when its ``zapa`` code is no longer present in the ZAPA
referential (``georeso_zapa.id_metier``). Orphans are clustered, then a new
PA is created at each cluster's weighted (by ``prises``) barycenter.

Clustering strategy:
1. Primary: group by ``bal['zapa']`` (orphans sharing a former ZAPA share a PA).
2. Fallback: BAT with no/null ZAPA are clustered spatially via a simple
   union-find using a 200 m distance threshold (DBSCAN with min_samples=1).

The convex hull of each cluster + 20 m buffer becomes the new ZAPA polygon.
"""

from __future__ import annotations

import math
from typing import Iterable, List, Optional, Tuple

import geopandas as gpd
import numpy as np
from shapely.geometry import MultiPoint, Point

from . import flags as flags_mod


CLUSTER_EPS_M = 200.0
ZAPA_BUFFER_M = 20.0


def detect_orphans(
    bal: gpd.GeoDataFrame,
    georeso_zapa: gpd.GeoDataFrame,
) -> gpd.GeoDataFrame:
    """Return BAT whose ``zapa`` is not present in the ZAPA referential."""
    if bal is None or bal.empty or "zapa" not in bal.columns:
        return bal.iloc[0:0].copy() if bal is not None else gpd.GeoDataFrame()

    if (
        georeso_zapa is None
        or georeso_zapa.empty
        or "id_metier" not in georeso_zapa.columns
    ):
        zapa_ids: set = set()
    else:
        zapa_ids = set(georeso_zapa["id_metier"].dropna().astype(str).tolist())

    return bal[~bal["zapa"].astype(str).isin(zapa_ids)].copy()


def _id_prefix(sro_code: str) -> str:
    """Extract the ``DDDDD/CCC`` (department / NRO) prefix of an SRO code."""
    parts = sro_code.split("/")
    if len(parts) < 2:
        raise ValueError(f"sro_code mal forme: {sro_code!r}")
    return "/".join(parts[:2])


def _prise_weight(bat) -> float:
    """Robust prise count for centroid weighting (NaN / 0 / missing -> 1)."""
    val = bat.get("prises", 1) if hasattr(bat, "get") else 1
    try:
        v = float(val)
    except (TypeError, ValueError):
        return 1.0
    if math.isnan(v) or v <= 0:
        return 1.0
    return v


def _spatial_clusters(points: List[Point], eps_m: float = CLUSTER_EPS_M) -> List[List[int]]:
    """Union-find clustering: two points belong together if d(.,.) <= eps_m (transitive)."""
    n = len(points)
    parent = list(range(n))

    def find(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a: int, b: int) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[ra] = rb

    for i in range(n):
        for j in range(i + 1, n):
            if points[i].distance(points[j]) <= eps_m:
                union(i, j)

    clusters: dict[int, List[int]] = {}
    for i in range(n):
        clusters.setdefault(find(i), []).append(i)
    return list(clusters.values())


def _weighted_centroid(geoms: Iterable[Point], weights: Iterable[float]) -> Point:
    coords = np.array([[g.x, g.y] for g in geoms], dtype=float)
    w = np.asarray(list(weights), dtype=float)
    if (w <= 0).all() or len(w) == 0:
        w = np.ones(len(coords))
    cx, cy = np.average(coords, axis=0, weights=w)
    return Point(float(cx), float(cy))


def _cluster_zapa_geom(geoms: List[Point]) -> "MultiPoint | object":
    """Convex hull of cluster (or buffer of single point) + 20 m buffer."""
    if len(geoms) >= 3:
        return MultiPoint(geoms).convex_hull.buffer(ZAPA_BUFFER_M)
    return MultiPoint(geoms).buffer(ZAPA_BUFFER_M)


def create_pa_for_orphans(
    orphan_bats: gpd.GeoDataFrame,
    sro_code: str,
    start_id: int = 99001,
    flag_collector: Optional["flags_mod.FlagCollector"] = None,
) -> Tuple[List[dict], List[dict]]:
    """Cluster orphan BAT and create one PA + ZAPA per cluster.

    Returns lists of dicts compatible with ``geopandas.GeoDataFrame(rows)``
    later in writer.py.
    """
    if orphan_bats is None or orphan_bats.empty:
        return [], []

    prefix = _id_prefix(sro_code)
    pa_rows: List[dict] = []
    zapa_rows: List[dict] = []

    # Step 1 -- group orphans sharing the same former ZAPA code.
    zapa_groups: List[List] = []
    unknown: List = []
    if "zapa" in orphan_bats.columns:
        groups: dict[str, list] = {}
        for _, bat in orphan_bats.iterrows():
            z = bat.get("zapa")
            if z is None or (isinstance(z, float) and math.isnan(z)) or str(z).strip() == "":
                unknown.append(bat)
            else:
                groups.setdefault(str(z), []).append(bat)
        zapa_groups = list(groups.values())
    else:
        unknown = [bat for _, bat in orphan_bats.iterrows()]

    # Step 2 -- spatial fallback for orphans without (or with unique) ZAPA.
    if unknown:
        pts = [b.geometry for b in unknown]
        for cluster in _spatial_clusters(pts, eps_m=CLUSTER_EPS_M):
            zapa_groups.append([unknown[i] for i in cluster])

    next_id = start_id
    for cluster in zapa_groups:
        bats_geom = [b.geometry for b in cluster]
        weights = [_prise_weight(b) for b in cluster]
        centroid = _weighted_centroid(bats_geom, weights)
        zapa_geom = _cluster_zapa_geom(bats_geom)

        pa_id = f"{prefix}/PA/{next_id}"
        next_id += 1

        pa_rows.append(
            {
                "id_metier": pa_id,
                "sro": sro_code,
                "geometry": centroid,
                "origine": "orphan_barycentre",
                "n_bat": len(cluster),
            }
        )
        zapa_rows.append(
            {
                "id_metier": pa_id,
                "sro": sro_code,
                "geometry": zapa_geom,
                "origine": "orphan_convex_hull",
            }
        )

        if flag_collector is not None:
            flag_collector.add(
                "PA_ORPHELIN_CREE",
                target_url=pa_id,
                message=f"Cluster de {len(cluster)} BAT orphelin(s)",
            )

    return pa_rows, zapa_rows
