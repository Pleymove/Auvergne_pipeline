"""Orphan-BAT detection + smart PA creation with infrastructure snapping.

Business rule (Pierre, NGE Energie & Solutions, 04/05/2026):
  - 1 PA = up to 24 micro-modules x 5 prises = **120 prises max**.
  - Default: **1 single PA per SRO** at the weighted barycentre of orphan
    prises, snapped to existing public infrastructure.
  - Multiple PAs only if:
      * Total prises > 120  (split via weighted k-means)
      * Spatial gap > 7000 m between clusters
      * Isolated cluster with >= 50 prises gets its own PA
"""

from __future__ import annotations

import math
from typing import List, Optional, Tuple

import geopandas as gpd
import numpy as np
from shapely.geometry import MultiPoint, Point
from shapely.ops import nearest_points

from . import flags as flags_mod

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

PA_MAX_PRISES = 120
CLUSTER_GAP_M = 7000.0
PA_MIN_PRISES_CLUSTER = 50
SNAP_RADIUS_M = 200.0
FALLBACK_PUBLIC_RADIUS_M = 500.0
ZAPA_BUFFER_M = 20.0
KMEANS_MAX_ITER = 50


# ---------------------------------------------------------------------------
# detect_orphans  (unchanged from iteration 2)
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _id_prefix(sro_code: str) -> str:
    """Extract the ``DDDDD/CCC`` prefix of an SRO code."""
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


def _weighted_centroid(
    pts: List[Point], weights: List[float]
) -> Point:
    """Weighted average of point coordinates."""
    pts_arr = np.array([[p.x, p.y] for p in pts], dtype=float)
    w = np.asarray(weights, dtype=float)
    if len(w) == 0 or (w <= 0).all():
        w = np.ones(len(pts_arr))
    cx, cy = np.average(pts_arr, axis=0, weights=w)
    return Point(float(cx), float(cy))


# ---------------------------------------------------------------------------
# Union-find spatial clustering (gap-based)
# ---------------------------------------------------------------------------

def _spatial_clusters(
    pts: List[Point], eps_m: float = CLUSTER_GAP_M
) -> List[List[int]]:
    """Union-find: two points are in the same cluster if distance <= eps_m.

    Uses transitive closure: if A near B and B near C, {A,B,C} is one cluster.
    """
    n = len(pts)
    if n == 0:
        return []

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

    # Use cKDTree for efficiency if scipy is available
    try:
        from scipy.spatial import cKDTree

        coords = np.array([[p.x, p.y] for p in pts], dtype=float)
        tree = cKDTree(coords)
        pairs = tree.query_pairs(r=eps_m)
        for i, j in pairs:
            union(i, j)
    except ImportError:
        # Fallback O(n^2) for environments without scipy
        for i in range(n):
            for j in range(i + 1, n):
                if pts[i].distance(pts[j]) <= eps_m:
                    union(i, j)

    clusters: dict[int, List[int]] = {}
    for i in range(n):
        clusters.setdefault(find(i), []).append(i)
    return list(clusters.values())


def _merge_small_clusters(
    clusters: List[List[int]],
    pts: List[Point],
    weights: List[float],
    min_prises: float = PA_MIN_PRISES_CLUSTER,
) -> List[List[int]]:
    """Merge clusters whose total prises < min_prises into the nearest cluster."""
    if len(clusters) <= 1:
        return clusters

    # Compute barycentres and total prises per cluster
    centroids: List[Point] = []
    totals: List[float] = []
    for cl in clusters:
        c_pts = [pts[i] for i in cl]
        c_w = [weights[i] for i in cl]
        centroids.append(_weighted_centroid(c_pts, c_w))
        totals.append(sum(c_w))

    merged = [True] * len(clusters)
    result: List[List[int]] = []

    for idx, (cl, total) in enumerate(zip(clusters, totals)):
        if total >= min_prises:
            result.append(list(cl))
            continue

        # Find nearest cluster (by centroid distance) with total >= min_prises
        best_dist = float("inf")
        best_j = -1
        for j in range(len(clusters)):
            if j == idx:
                continue
            d = centroids[idx].distance(centroids[j])
            if d < best_dist:
                best_dist = d
                best_j = j

        if best_j >= 0 and best_j < len(result):
            # The target cluster might already be in result — find it
            target = clusters[best_j]
            existing = None
            for r in result:
                if set(r) == set(target):
                    existing = r
                    break
            if existing is not None:
                existing.extend(cl)
            elif best_j > idx:
                # Target hasn't been processed yet — defer
                clusters[best_j].extend(cl)
            else:
                # Fallback: keep as standalone (shouldn't happen)
                result.append(list(cl))
        else:
            # No suitable target — keep as standalone
            result.append(list(cl))

    return result


# ---------------------------------------------------------------------------
# Weighted k-means (vanilla Lloyd, no scipy dependency)
# ---------------------------------------------------------------------------

def _kmeans_weighted(
    pts: List[Point],
    weights: List[float],
    k: int,
    max_iter: int = KMEANS_MAX_ITER,
) -> List[List[int]]:
    """Weighted k-means clustering. Each point votes with its weight.

    Returns list of k index-lists (indices into the original pts list).
    """
    n = len(pts)
    if n <= k:
        return [[i] for i in range(n)]

    coords = np.array([[p.x, p.y] for p in pts], dtype=float)
    w = np.asarray(weights, dtype=float)

    # Initialisation: k-means++ style (pick first centroid at weighted random,
    # then pick remaining farthest from existing centroids)
    centroids = np.zeros((k, 2), dtype=float)

    # First centroid: weighted random
    probs = w / w.sum()
    idx0 = np.random.choice(n, p=probs)
    centroids[0] = coords[idx0]

    for c in range(1, k):
        # Distance to nearest existing centroid
        dists = np.min(
            np.linalg.norm(coords[:, None] - centroids[:c][None], axis=2),
            axis=1,
        )
        dists += 1e-10  # avoid div by zero
        probs = dists / dists.sum()
        centroids[c] = coords[np.random.choice(n, p=probs)]

    for _ in range(max_iter):
        # Assign points to nearest centroid
        dists = np.linalg.norm(coords[:, None] - centroids[None], axis=2)
        labels = np.argmin(dists, axis=1)

        # Update centroids (weighted mean)
        new_centroids = np.zeros_like(centroids)
        for c in range(k):
            mask = labels == c
            if mask.sum() == 0:
                # Dead cluster: re-seed at random point
                new_centroids[c] = coords[np.random.randint(n)]
            else:
                new_centroids[c] = np.average(
                    coords[mask], axis=0, weights=w[mask]
                )

        if np.allclose(centroids, new_centroids, rtol=1e-6):
            break
        centroids = new_centroids

    # Build index lists
    result: List[List[int]] = [[] for _ in range(k)]
    for i, lbl in enumerate(labels):
        result[lbl].append(i)

    return [r for r in result if r]  # filter empty clusters


# ---------------------------------------------------------------------------
# Snap to existing infrastructure
# ---------------------------------------------------------------------------

def _snap_to_existing_infra(
    centroid: Point,
    cheminement_lines: gpd.GeoDataFrame,
    athd_lines: gpd.GeoDataFrame,
    radius_m: float = SNAP_RADIUS_M,
) -> Tuple[Optional[Point], str]:
    """Find the nearest point on cheminement / ATHD within radius_m.

    Returns (snapped_point, source) or (None, "") if nothing found.
    """
    candidates: List[Tuple[Point, str]] = []

    # Collect candidate segments within the search radius
    search_area = centroid.buffer(radius_m)

    for df, source in (
        (cheminement_lines, "cheminement"),
        (athd_lines, "athd"),
    ):
        if df is None or df.empty:
            continue
        mask = df.geometry.intersects(search_area)
        if not mask.any():
            continue
        for _, row in df[mask].iterrows():
            # Find the closest point on each candidate segment
            _, snap_pt = nearest_points(centroid, row.geometry)
            candidates.append((snap_pt, source))

    if not candidates:
        return None, ""

    # Pick the closest
    distances = [centroid.distance(pt) for pt, _ in candidates]
    best = int(np.argmin(distances))
    return candidates[best]


def _snap_to_public_parcel(
    centroid: Point,
    parcelles_classifiees: gpd.GeoDataFrame,
    radius_m: float = FALLBACK_PUBLIC_RADIUS_M,
) -> Tuple[Optional[Point], str]:
    """Fallback: snap to nearest point inside a public parcel."""
    import pandas as pd

    if parcelles_classifiees is None or parcelles_classifiees.empty:
        return None, ""

    search_area = centroid.buffer(radius_m)

    is_public = parcelles_classifiees.get("public", pd.Series(dtype=bool))
    pub = parcelles_classifiees[is_public]
    if pub.empty:
        return None, ""

    mask = pub.geometry.intersects(search_area)
    if not mask.any():
        return None, ""

    candidates = pub[mask]
    # For each candidate parcel, snap to its nearest boundary point
    best_pt = None
    best_dist = float("inf")
    for _, row in candidates.iterrows():
        _, snap_pt = nearest_points(centroid, row.geometry)
        d = centroid.distance(snap_pt)
        if d < best_dist:
            best_dist = d
            best_pt = snap_pt

    return (best_pt, "parcelle_publique") if best_pt is not None else (None, "")


# =====================================================================
# Main entry point
# =====================================================================

def create_pa_for_orphans(
    orphan_bats: gpd.GeoDataFrame,
    sro_code: str,
    cheminement_lines: Optional[gpd.GeoDataFrame] = None,
    athd_lines: Optional[gpd.GeoDataFrame] = None,
    parcelles_classifiees: Optional[gpd.GeoDataFrame] = None,
    start_id: int = 99001,
    flag_collector: Optional["flags_mod.FlagCollector"] = None,
) -> Tuple[List[dict], List[dict]]:
    """Cluster orphan BATs and create smart PA(s) with infrastructure snapping.

    Parameters
    ----------
    orphan_bats:
        GeoDataFrame of orphan BATs (must have ``geometry`` Point + ``prises``).
    sro_code:
        Canonical SRO code ``DDDDD/CCC/PMZ/NNNNN``.
    cheminement_lines:
        Cheminement infrastructure (public by nature).  May be None.
    athd_lines:
        ATHD artere lines.  May be None.
    parcelles_classifiees:
        Pre-classified parcels with a boolean ``public`` column.  May be None.
    start_id:
        First PA id suffix (99001, 99002, ...).
    flag_collector:
        In-memory flag accumulator (optional).

    Returns
    -------
    (pa_rows, zapa_rows)
        Lists of dicts ready for ``geopandas.GeoDataFrame(rows)``.
    """
    if orphan_bats is None or orphan_bats.empty:
        return [], []

    import pandas as pd  # local import, always available

    prefix = _id_prefix(sro_code)
    pa_rows: List[dict] = []
    zapa_rows: List[dict] = []

    # Extract points and weights
    pts: List[Point] = []
    weights: List[float] = []
    for _, bat in orphan_bats.iterrows():
        pts.append(bat.geometry)
        weights.append(_prise_weight(bat))

    total_prises = sum(weights)

    # ---- Step 1: cluster by spatial gap ----
    clusters = _spatial_clusters(pts, CLUSTER_GAP_M)

    # ---- Step 2: merge small clusters ----
    clusters = _merge_small_clusters(clusters, pts, weights, PA_MIN_PRISES_CLUSTER)

    # ---- Step 3: sub-divide large clusters (> 120 prises) ----
    final_clusters: List[List[int]] = []
    for cl in clusters:
        cl_total = sum(weights[i] for i in cl)
        if cl_total > PA_MAX_PRISES:
            # How many sub-PAs do we need?
            k = max(2, int(math.ceil(cl_total / PA_MAX_PRISES)))
            cl_pts = [pts[i] for i in cl]
            cl_w = [weights[i] for i in cl]
            sub = _kmeans_weighted(cl_pts, cl_w, k)
            final_clusters.extend([[cl[i] for i in s] for s in sub])
        else:
            final_clusters.append(cl)

    # ---- Step 4: create PA + ZAPA for each cluster ----
    next_id = start_id

    for cluster in final_clusters:
        cl_pts = [pts[i] for i in cluster]
        cl_w = [weights[i] for i in cluster]
        centroid = _weighted_centroid(cl_pts, cl_w)
        cl_prises = int(sum(cl_w))

        # 4a. Snap to existing infrastructure
        snapped_pt, snap_source = _snap_to_existing_infra(
            centroid, cheminement_lines, athd_lines, SNAP_RADIUS_M
        )

        placement_flag = None

        if snapped_pt is None:
            # 4b. Fallback: snap to public parcel
            snapped_pt, snap_source = _snap_to_public_parcel(
                centroid, parcelles_classifiees, FALLBACK_PUBLIC_RADIUS_M
            )
            if snapped_pt is not None:
                placement_flag = "PA_PLACEMENT_INCERTAIN"
            else:
                # 4c. Last resort: raw centroid (flagged)
                snapped_pt = centroid
                snap_source = "aucune_infra"
                placement_flag = "PA_PLACEMENT_IMPOSSIBLE"

        # Create PA id
        pa_id = f"{prefix}/PA/{next_id}"
        next_id += 1

        # ZAPA geometry: convex hull + buffer (or buffer around point if < 3)
        pts_for_hull = [pts[i] for i in cluster]
        if len(pts_for_hull) >= 3:
            zapa_geom = MultiPoint(pts_for_hull).convex_hull.buffer(ZAPA_BUFFER_M)
        else:
            zapa_geom = MultiPoint(pts_for_hull).buffer(ZAPA_BUFFER_M)

        pa_rows.append(
            {
                "id_metier": pa_id,
                "sro": sro_code,
                "geometry": snapped_pt,
                "origine": f"orphan_barycentre_snapped",
                "snap_source": snap_source,
                "n_bat": len(cluster),
                "total_prises": cl_prises,
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

        # Flags
        if flag_collector is not None:
            flag_collector.add(
                "PA_ORPHELIN_CREE",
                target_url=pa_id,
                message=(
                    f"{len(cluster)} BAT, {cl_prises} prises, "
                    f"snapped on: {snap_source}"
                ),
            )
            if placement_flag is not None:
                flag_collector.add(
                    placement_flag,
                    target_url=pa_id,
                    message=(
                        f"Snap echoue (source={snap_source}), "
                        f"PA place au centroid brut"
                    ),
                )

    return pa_rows, zapa_rows