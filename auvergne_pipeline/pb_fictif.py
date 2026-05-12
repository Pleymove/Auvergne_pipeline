"""Fictitious PB (Point de Branchement) generation.

Business rules (Pierre, 05/05/2026):
  - 1 PB per BAT cluster where every BAT is within 100 m of the PB.
  - Default capacity: 5 prises.  Max: 10 prises (2 micro-modules).
  - Placement: nearest point on public infrastructure to the farthest BAT
    in the cluster (minimises max D3).
  - Each PB is fictitious — for debug only, may be stripped for the client.
"""

from __future__ import annotations

import logging
import math
from typing import List, Optional

import geopandas as gpd
import numpy as np
import pandas as pd
from shapely.geometry import LineString, MultiPoint, Point
from shapely.ops import nearest_points

from . import config, flags as flags_mod

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
PB_D3_MAX_M = 100.0
PB_PRISES_DEFAUT = 5
PB_PRISES_MAX = 10
CLUSTER_EPS_M = 100.0  # max distance between BATs in same cluster
GC_NEUF_BUFFER_M = 2.0


# ---------------------------------------------------------------------------
# Helpers ────────────────────────────────────────────────────────────────
# ---------------------------------------------------------------------------

def _spatial_clusters(pts: List[Point], eps_m: float = CLUSTER_EPS_M) -> List[List[int]]:
    """Union-find clustering by distance threshold."""
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

    for i in range(n):
        for j in range(i + 1, n):
            if pts[i].distance(pts[j]) <= eps_m:
                union(i, j)

    clusters: dict[int, List[int]] = {}
    for i in range(n):
        clusters.setdefault(find(i), []).append(i)
    return list(clusters.values())


def _prise_weight(bat) -> float:
    val = bat.get("prises", 1) if hasattr(bat, "get") else 1
    try:
        v = float(val)
    except (TypeError, ValueError):
        return 1.0
    if math.isnan(v) or v <= 0:
        return 1.0
    return v


def _split_oversized_cluster(
    cluster_indices: List[int],
    pts: List[Point],
    weights: List[float],
    max_prises: int = PB_PRISES_MAX,
) -> List[List[int]]:
    """Greedy split: fill buckets up to max_prises, nearest-neighbour."""
    remaining = list(cluster_indices)
    result: List[List[int]] = []

    while remaining:
        bucket: List[int] = [remaining.pop(0)]
        bucket_w = weights[bucket[0]]

        while remaining and bucket_w < max_prises:
            # Find nearest remaining BAT to the bucket's centroid
            bucket_pts = [pts[i] for i in bucket]
            centroid = MultiPoint(bucket_pts).centroid

            best_i = 0
            best_d = float("inf")
            for idx, r in enumerate(remaining):
                d = centroid.distance(pts[r])
                if d < best_d:
                    best_d = d
                    best_i = idx

            if bucket_w + weights[remaining[best_i]] > max_prises:
                break

            bucket.append(remaining.pop(best_i))
            bucket_w += weights[bucket[-1]]  # last added

        result.append(bucket)

    return result


def _snap_pb_to_infra(
    target_bat_geom: Point,
    infra_edges: gpd.GeoDataFrame,
    search_radius: float = 500.0,
) -> Optional[Point]:
    """Place PB at the nearest point on infra to the target BAT."""
    if infra_edges is None or infra_edges.empty:
        return None

    envelope = target_bat_geom.buffer(search_radius)
    candidates = infra_edges[infra_edges.geometry.intersects(envelope)]
    if candidates.empty:
        return None

    best_pt = None
    best_dist = float("inf")
    for _, row in candidates.iterrows():
        _, snap = nearest_points(target_bat_geom, row.geometry)
        d = target_bat_geom.distance(snap)
        if d < best_dist:
            best_dist = d
            best_pt = snap

    return best_pt


# ---------------------------------------------------------------------------
# PR #23 Bug C — PB public-domain enforcement
# ---------------------------------------------------------------------------


# PR #29 A4 — explicit PB placement statuses
PB_PLACEMENT_PUBLIC_PARCELLE = "PB_PLACEMENT_PUBLIC_PARCELLE"
PB_PLACEMENT_PUBLIC_ROUTE_BUFFER = "PB_PLACEMENT_PUBLIC_ROUTE_BUFFER"
PB_PLACEMENT_PRIVE = "PB_PLACEMENT_PRIVE"
PB_PLACEMENT_INCERTAIN = "PB_PLACEMENT_INCERTAIN"

# Status families that allow GC neuf creation. Private / uncertain MUST NOT
# emit a GC neuf row — those PBs are flagged for manual review only.
PB_PLACEMENT_PUBLIC_FAMILY = (
    PB_PLACEMENT_PUBLIC_PARCELLE,
    PB_PLACEMENT_PUBLIC_ROUTE_BUFFER,
)


def _classify_pb_placement(
    pb_geom: Point,
    parcelle_publique_union,
    ign_routes_buffered,
    re_snap_radius_m: float = 50.0,
) -> tuple[Point, str]:
    """Classify a PB placement and snap onto public geometry if needed.

    PR #29 A4: distinguish four explicit statuses instead of merging
    "PB on parcelle publique" with "PB merely inside the IGN buffer".

    Returns
    -------
    (pb_geom_corrected, status)

    status is one of :
    - PB_PLACEMENT_PUBLIC_PARCELLE     — PB lies on a communal parcel
    - PB_PLACEMENT_PUBLIC_ROUTE_BUFFER — PB only via the IGN road buffer,
                                         snapped to the nearest public point
    - PB_PLACEMENT_PRIVE               — re-snap failed within
                                         ``re_snap_radius_m``
    - PB_PLACEMENT_INCERTAIN           — no public reference available

    The status is computed AFTER any required re-snap, so an initially
    private PB that lands on a parcelle publique after the re-snap is
    classified as PUBLIC_PARCELLE (not PRIVE).
    """
    has_parc = (
        parcelle_publique_union is not None
        and not parcelle_publique_union.is_empty
    )
    has_ign = (
        ign_routes_buffered is not None
        and not ign_routes_buffered.is_empty
    )

    if not has_parc and not has_ign:
        return pb_geom, PB_PLACEMENT_INCERTAIN

    # Step 1 — already on a parcelle publique?
    if has_parc and pb_geom.intersects(parcelle_publique_union.buffer(2)):
        return pb_geom, PB_PLACEMENT_PUBLIC_PARCELLE

    # Step 2 — accepted via IGN buffer but NOT on a parcelle publique:
    # snap to the nearest exact public geometry (parcelle publique edge or
    # IGN buffer edge) so the PB sits on a real public reference, not in
    # the middle of a buffered void.
    if has_ign and pb_geom.intersects(ign_routes_buffered):
        # Snap target = union(parcelle_publique, ign_routes_buffered.boundary)
        # so we land either on a public parcel or on a road centre/edge,
        # never floating in the middle of an arbitrary buffer.
        boundary = ign_routes_buffered.boundary
        if has_parc:
            try:
                snap_target = parcelle_publique_union.union(boundary)
            except Exception:
                snap_target = boundary
        else:
            snap_target = boundary
        if snap_target is None or snap_target.is_empty:
            return pb_geom, PB_PLACEMENT_PRIVE
        _, nearest = nearest_points(pb_geom, snap_target)
        # If the snap result happens to land on a parcelle publique,
        # promote the status accordingly.
        if has_parc and nearest.intersects(parcelle_publique_union.buffer(0.5)):
            return nearest, PB_PLACEMENT_PUBLIC_PARCELLE
        return nearest, PB_PLACEMENT_PUBLIC_ROUTE_BUFFER

    # Step 3 — outside both references: try a 50 m re-snap onto the public
    # union. If it lands close enough, prefer the parcelle publique side.
    parts = []
    if has_parc:
        parts.append(parcelle_publique_union)
    if has_ign:
        parts.append(ign_routes_buffered)
    public_union = parts[0] if len(parts) == 1 else parts[0].union(parts[1])
    if public_union is None or public_union.is_empty:
        return pb_geom, PB_PLACEMENT_PRIVE
    _, nearest = nearest_points(pb_geom, public_union)
    if pb_geom.distance(nearest) <= re_snap_radius_m:
        if has_parc and nearest.intersects(parcelle_publique_union.buffer(0.5)):
            return nearest, PB_PLACEMENT_PUBLIC_PARCELLE
        return nearest, PB_PLACEMENT_PUBLIC_ROUTE_BUFFER

    # Re-snap failed — the PB stays in private land for manual review.
    return pb_geom, PB_PLACEMENT_PRIVE


def _ensure_pb_on_public_domain(
    pb_geom: Point,
    parcelle_publique_union,
    ign_routes_buffered,
    re_snap_radius_m: float = 50.0,
):
    """Backwards-compatible wrapper around :func:`_classify_pb_placement`.

    Returns ``(pb_geom_corrected, flag)`` where *flag* is ``None`` when the
    PB ends up on the public domain (any flavour) and
    ``"PB_PLACEMENT_PRIVE"`` when the re-snap fails.

    PR #29 A4 keeps this function for the existing PR23 unit tests; the
    new four-statut classifier ``_classify_pb_placement`` is what the
    pipeline calls.
    """
    pb_geom_out, status = _classify_pb_placement(
        pb_geom, parcelle_publique_union, ign_routes_buffered, re_snap_radius_m,
    )
    if status in PB_PLACEMENT_PUBLIC_FAMILY:
        return pb_geom_out, None
    if status == PB_PLACEMENT_INCERTAIN:
        # Historical behaviour was to flag PRIVE on missing public references.
        return pb_geom, "PB_PLACEMENT_PRIVE"
    return pb_geom, status  # PB_PLACEMENT_PRIVE


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def build_pb_fictifs(
    bal_sro: gpd.GeoDataFrame,
    pa_sro: gpd.GeoDataFrame,
    zapa_sro: gpd.GeoDataFrame,
    infra_edges: gpd.GeoDataFrame,
    flag_collector: Optional["flags_mod.FlagCollector"] = None,
    parcelle_publique_union=None,     # PR #23 Bug C
    ign_routes_buffered=None,         # PR #23 Bug C
) -> tuple[gpd.GeoDataFrame, gpd.GeoDataFrame]:
    """Generate fictitious PBs and any required GC neuf (C0) segments.

    Returns
    -------
    (pb_fictifs, gc_neuf)
        ``pb_fictifs`` — GeoDataFrame of PB Points.
        ``gc_neuf``     — GeoDataFrame of GC neuf LineStrings (mode_pose='C0').
    """
    pb_rows: List[dict] = []
    gc_rows: List[dict] = []

    if bal_sro is None or bal_sro.empty or pa_sro is None or pa_sro.empty:
        return (
            gpd.GeoDataFrame(geometry=[], crs=config.PROJECT_CRS),
            gpd.GeoDataFrame(geometry=[], crs=config.PROJECT_CRS),
        )

    pb_counter = 0

    # ── For each PA, find BATs in its ZAPA ────────────────────────────
    for _, pa in pa_sro.iterrows():
        pa_id = pa.get("id_metier", f"pa#{pa.name}")
        pa_geom = pa.geometry

        # Find the ZAPA for this PA
        zapa_match = zapa_sro[zapa_sro.get("id_metier", pd.Series(dtype=str)) == pa_id]
        if zapa_match.empty:
            continue
        zapa_geom = zapa_match.iloc[0].geometry

        # BATs inside the ZAPA
        mask = bal_sro.geometry.within(zapa_geom.buffer(10))
        bats_in_zapa = bal_sro[mask]
        if bats_in_zapa.empty:
            continue

        # ── Cluster BATs by 100 m proximity ──────────────────────────
        _pts: List[Point] = []
        _w: List[float] = []
        for _, bat in bats_in_zapa.iterrows():
            _pts.append(bat.geometry)
            _w.append(_prise_weight(bat))

        clusters = _spatial_clusters(_pts, CLUSTER_EPS_M)

        # ── Handle oversized clusters (>PB_PRISES_MAX prises) ─────────
        final_clusters: List[List[int]] = []
        for cl in clusters:
            cl_w = sum(_w[i] for i in cl)
            if cl_w > PB_PRISES_MAX:
                sub = _split_oversized_cluster(cl, _pts, _w, PB_PRISES_MAX)
                final_clusters.extend(sub)
            else:
                final_clusters.append(cl)

        # ── Create PB for each cluster ───────────────────────────────
        sro_code = (
            pa.get("sro", "?") if hasattr(pa, "get") else "?"
        )

        for cl in final_clusters:
            cl_pts = [_pts[i] for i in cl]
            cl_w = sum(_w[i] for i in cl)

            # Find the farthest BAT from PA (target for PB placement)
            farthest_bat_idx = cl[0]
            farthest_dist = pa_geom.distance(cl_pts[0])
            for i in cl:
                d = pa_geom.distance(_pts[i])
                if d > farthest_dist:
                    farthest_dist = d
                    farthest_bat_idx = i

            farthest_pt = _pts[farthest_bat_idx]

            # Snap PB to nearest point on infrastructure
            pb_pt = _snap_pb_to_infra(farthest_pt, infra_edges)
            if pb_pt is None:
                pb_pt = farthest_pt  # fallback to BAT point
                if flag_collector is not None:
                    flag_collector.add(
                        "PB_PLACEMENT_INCERTAIN",
                        target_url=pa_id,
                        message=f"PB non snapable sur infra (cluster {len(cl)} BAT)",
                    )

            pb_counter += 1
            pb_id = f"PB_{sro_code}_{pb_counter}"

            # PR #29 A4: 4-statut classification (PUBLIC_PARCELLE /
            # PUBLIC_ROUTE_BUFFER / PRIVE / INCERTAIN). PUBLIC_ROUTE_BUFFER
            # PBs are snapped onto exact public geometry (parcelle publique
            # boundary or IGN road), they no longer float inside a buffer.
            placement_status = PB_PLACEMENT_PUBLIC_PARCELLE
            if pb_pt is not None and (
                (parcelle_publique_union is not None
                 and not parcelle_publique_union.is_empty)
                or (ign_routes_buffered is not None
                    and not ign_routes_buffered.is_empty)
            ):
                pb_pt_corrected, placement_status = _classify_pb_placement(
                    pb_pt, parcelle_publique_union, ign_routes_buffered,
                )
                if placement_status in PB_PLACEMENT_PUBLIC_FAMILY:
                    pb_pt = pb_pt_corrected
                else:
                    # PRIVE / INCERTAIN — flag for manual review and keep
                    # the original PB geometry (do NOT use a phantom snap).
                    if flag_collector is not None:
                        flag_collector.add(
                            placement_status,
                            target_url=pa_id,
                            message=(
                                f"PB place en parcelle privee pour cluster "
                                f"{len(cl)} BAT"
                                if placement_status == PB_PLACEMENT_PRIVE
                                else (
                                    f"PB placement incertain pour cluster "
                                    f"{len(cl)} BAT (domaine public inconnu)"
                                )
                            ),
                        )
            elif pb_pt is not None:
                placement_status = PB_PLACEMENT_INCERTAIN

            pb_rows.append({
                "pb_id": pb_id,
                "pa_id": pa_id,
                "sro": sro_code,
                "nb_prises": int(cl_w),
                "bat_count": len(cl),
                "farthest_bat_d3_m": round(farthest_dist, 1),
                "placement_status": placement_status,  # PR #28 BLOQUANT 3 + PR #29 A4
                "geometry": pb_pt,
            })

            # PR #28 BLOQUANT 3 / PR #29 A4: only PUBLIC_* PBs trigger gc_line.
            if placement_status not in PB_PLACEMENT_PUBLIC_FAMILY:
                continue

            # If PB is not on existing infra, create GC neuf (C0) from
            # the PA to the PB
            if infra_edges is None or infra_edges.empty or not any(
                infra_edges.geometry.distance(pb_pt) < 1.0
            ):
                gc_line = LineString([pa_geom, pb_pt])
                gc_rows.append({
                    "sro": sro_code,
                    "pa_id": pa_id,
                    "pb_id": pb_id,
                    "statut": "C",
                    "mode_pose": config.GC_NEUF_MODE_POSE,
                    "src": "gc_neuf",
                    "geometry": gc_line,
                })

    # ── Build output GeoDataFrames ────────────────────────────────────
    pb_gdf = gpd.GeoDataFrame(
        pb_rows, geometry="geometry", crs=config.PROJECT_CRS
    ) if pb_rows else gpd.GeoDataFrame(geometry=[], crs=config.PROJECT_CRS)

    gc_gdf = gpd.GeoDataFrame(
        gc_rows, geometry="geometry", crs=config.PROJECT_CRS
    ) if gc_rows else gpd.GeoDataFrame(geometry=[], crs=config.PROJECT_CRS)

    # ── PR #28 BLOQUANT 3 / PR #29 A4 [PB QA] diagnostics ──────────────
    if not pb_gdf.empty:
        n_total = len(pb_gdf)
        statuses = pb_gdf["placement_status"]
        n_parc = int((statuses == PB_PLACEMENT_PUBLIC_PARCELLE).sum())
        n_buf = int((statuses == PB_PLACEMENT_PUBLIC_ROUTE_BUFFER).sum())
        n_priv = int((statuses == PB_PLACEMENT_PRIVE).sum())
        n_unc = int((statuses == PB_PLACEMENT_INCERTAIN).sum())
        # Backwards-compat: any leftover legacy "PB_PLACEMENT_PUBLIC" still
        # counts as parcelle publique for the breakdown total.
        n_legacy_public = int((statuses == "PB_PLACEMENT_PUBLIC").sum())
        n_parc += n_legacy_public
        log.info(
            "[PB QA] total=%d public_parcelle=%d public_route_buffer=%d "
            "private=%d uncertain=%d",
            n_total, n_parc, n_buf, n_priv, n_unc,
        )

    return pb_gdf, gc_gdf