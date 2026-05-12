"""PR #31 — Topology validation and normalization of livrable_infra.

After ``route_pa_to_pb`` produces an initial GeoDataFrame, this module
passes it through a series of normalisation steps so the delivered
network is a continuous, professional topology (Pierre's "bille
mentale" rule: a ball must be able to roll from PA to PB along the
delivered geometries, without any gap, with no zigzag of support).

Steps (call ``finalize_livrable_topology``):

1. ``_snap_endpoints_to_exact``    — round near-identical endpoints
2. ``_split_at_terminals``         — split lines where PA/PB project onto them
3. ``_ensure_terminals_connected`` — add a short public connector if needed
4. ``_remove_near_duplicates``     — drop quasi-parallel doublons via hierarchy
5. ``_filter_energy_private``      — drop E1/bt segments crossing private
6. ``_audit_continuity``           — build topology graph, check PA/PB reach
7. ``_audit_support_switches``     — count incoherent support alternations
8. ``_audit_mutualisation``        — compute trunk-reuse stats

All steps preserve QML-compatible columns and never invent codes RIP.
Returns the cleaned-up GeoDataFrame **plus** a ``stats`` dict consumed
by the ``[CONTINUITY QA]`` / ``[DEDUP QA]`` / ``[SUPPORT QA]`` /
``[ENERGY QA]`` / ``[SNAP QA]`` / ``[MUTUAL QA]`` log lines.

QGIS embedded compatibility:
- Uses ``geopandas`` / ``networkx`` / ``shapely`` / ``scipy`` only.
- No sklearn, no sklearn-derived clustering.
- Stable across pandas 1.5 → 2.x.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from typing import Optional

import geopandas as gpd
import networkx as nx
import numpy as np
import pandas as pd
import shapely
from shapely.geometry import LineString, Point
from shapely.ops import substring


log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Two endpoints within this distance are considered the same node and
# snapped to one another. Matches the weld radius used during routing.
ENDPOINT_SNAP_TOL_M = 0.5

# A gap shorter than this is "micro" and can be auto-fixed by snapping
# both endpoints to a shared coordinate, or by inserting a short
# connector if the gap lies inside the delivery public area.
MICRO_GAP_TOL_M = 1.0

# Maximum gap that we allow to auto-close with a public connector. Beyond
# this, we flag the gap as unresolved instead of inventing geometry.
MICRO_GAP_MAX_FIX_M = 3.0

# Considered "touching" a terminal (PA/PB) under this distance.
TERMINAL_TOUCH_TOL_M = 0.2

# Range within which we accept to split an existing edge at a terminal's
# projection. Anything further than this is flagged but not snapped.
TERMINAL_SNAP_RADIUS_M = 5.0

# Two parallel segments closer than this are considered "doublons".
NEAR_DUPLICATE_TOL_M = 0.5

# Hierarchy used to resolve near-duplicates and parallel conflicts.
# Lower index = higher priority kept. Tie-broken by length (shorter wins).
SUPPORT_HIERARCHY: dict[str, int] = {
    # Conduite Orange / FT / cheminement = E7 / C7 / chem family
    "ft": 0,
    "chem": 0,
    "athd": 1,
    # Aérien télécom (E0)
    # FT segments with mode_pose='0' fall here implicitly via their src.
    # Aérien énergie / BT (E1) — only public, lower priority.
    "bt": 3,
    # GC neuf C0 and IGN-derived gc_neuf — last resort.
    "gc_neuf": 4,
}

# Support family used to detect zigzags.
SUPPORT_FAMILY: dict[str, str] = {
    "ft": "underground_orange",
    "athd": "underground_orange",
    "chem": "underground_orange",
    "bt": "aerial_energy",
    "gc_neuf": "new_gc",
    # If src not present, fall back to "unknown" — never counted as switch.
}


def _support_family_of(row) -> str:
    """Map a delivered row to its support family (used by switch detection)."""
    src = (row.get("src") if hasattr(row, "get") else row["src"]) or ""
    mp = (row.get("mode_pose") if hasattr(row, "get") else row["mode_pose"]) or ""
    # Telecom aerien (E0) is identified by mode_pose='0' from FT sources.
    if src == "ft" and str(mp) == "0":
        return "aerial_telecom"
    return SUPPORT_FAMILY.get(str(src), "unknown")


# ---------------------------------------------------------------------------
# Step 1 — Snap near-identical endpoints to one exact coordinate
# ---------------------------------------------------------------------------


def _snap_endpoints_to_exact(
    df: gpd.GeoDataFrame, tol_m: float = ENDPOINT_SNAP_TOL_M
) -> tuple[gpd.GeoDataFrame, int]:
    """Round close endpoints so connected segments share exact coordinates.

    Builds a list of all endpoint coordinates, clusters them with a
    distance threshold ``tol_m`` (union-find on ``cKDTree.query_pairs``)
    and rewrites every LineString so its first/last vertex uses the
    cluster representative. Subsequent topology checks (``networkx``)
    therefore see one shared node where the previous version had two
    floating-point neighbours.

    Returns ``(new_df, n_endpoints_snapped)``.
    """
    if df is None or df.empty or "geometry" not in df.columns:
        return df, 0
    geoms = df.geometry.tolist()
    endpoints: list[tuple[float, float]] = []
    for g in geoms:
        if g is None or g.is_empty or not isinstance(g, LineString):
            endpoints.append(None)
            endpoints.append(None)
            continue
        cs = list(g.coords)
        endpoints.append((float(cs[0][0]), float(cs[0][1])))
        endpoints.append((float(cs[-1][0]), float(cs[-1][1])))

    # Index of real endpoints (skip None for degenerate rows).
    real_idx = [i for i, p in enumerate(endpoints) if p is not None]
    if len(real_idx) < 2:
        return df, 0
    coords = np.array([endpoints[i] for i in real_idx], dtype=float)

    from scipy.spatial import cKDTree
    tree = cKDTree(coords)
    pairs = tree.query_pairs(r=tol_m, output_type="ndarray")

    parent = list(range(len(real_idx)))

    def _find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def _union(a, b):
        ra, rb = _find(a), _find(b)
        if ra != rb:
            parent[ra] = rb

    for a, b in pairs:
        _union(int(a), int(b))

    # Cluster representatives = centroid rounded to 6 decimals.
    cluster_pts: dict[int, tuple[float, float]] = {}
    label_for: dict[int, int] = {}
    for i in range(len(real_idx)):
        label_for[i] = _find(i)
    for label in set(label_for.values()):
        members = [j for j, lab in label_for.items() if lab == label]
        cx = float(np.mean([coords[m, 0] for m in members]))
        cy = float(np.mean([coords[m, 1] for m in members]))
        cluster_pts[label] = (round(cx, 6), round(cy, 6))

    # Rewrite endpoints.
    n_changed = 0
    new_geoms: list = []
    real_pos = 0
    for i, g in enumerate(geoms):
        if g is None or g.is_empty or not isinstance(g, LineString):
            new_geoms.append(g)
            continue
        cs = [(float(c[0]), float(c[1])) for c in g.coords]
        if len(cs) < 2:
            new_geoms.append(g)
            continue
        # endpoints at real_idx positions 2*i (first) and 2*i+1 (last)
        # but we built real_idx skipping None rows, so map carefully.
        # In practice all rows we kept produce two endpoints in real_idx.
        # We track real_pos: how many "first/last" pairs we've consumed.
        first_label = label_for[real_pos]
        last_label = label_for[real_pos + 1]
        real_pos += 2
        new_first = cluster_pts[first_label]
        new_last = cluster_pts[last_label]
        changed = False
        if cs[0] != new_first:
            cs[0] = new_first
            changed = True
        if cs[-1] != new_last:
            cs[-1] = new_last
            changed = True
        if changed:
            try:
                # Avoid degenerate zero-length lines.
                if cs[0] == cs[-1] and len(cs) == 2:
                    new_geoms.append(g)  # keep original; downstream dedup will drop
                else:
                    new_geoms.append(LineString(cs))
                    n_changed += 1
            except (ValueError, TypeError):
                new_geoms.append(g)
        else:
            new_geoms.append(g)

    out = df.copy()
    out["geometry"] = new_geoms
    return out, n_changed


# ---------------------------------------------------------------------------
# Step 2 — Split lines at terminal projections (PA/PB)
# ---------------------------------------------------------------------------


def _split_line_at_distance(line: LineString, dist: float) -> tuple[LineString, LineString] | None:
    """Return ``(seg_before, seg_after)`` or ``None`` if the split is degenerate."""
    try:
        seg_a = substring(line, 0, dist)
        seg_b = substring(line, dist, line.length)
    except Exception:
        return None
    if not isinstance(seg_a, LineString) or not isinstance(seg_b, LineString):
        return None
    if seg_a.is_empty or seg_b.is_empty:
        return None
    return seg_a, seg_b


def _ensure_terminals_connected(
    df: gpd.GeoDataFrame,
    pa_sro: gpd.GeoDataFrame,
    pb_sro: gpd.GeoDataFrame,
    *,
    delivery_public_area_safe=None,
    touch_tol_m: float = TERMINAL_TOUCH_TOL_M,
    snap_radius_m: float = TERMINAL_SNAP_RADIUS_M,
    flag_collector=None,
) -> tuple[gpd.GeoDataFrame, dict]:
    """Make sure every PA and every PB touches a livrable line.

    For each terminal:
    1. If a livrable edge already passes within ``touch_tol_m`` of the
       terminal, do nothing (already connected after Step 1 snapping).
    2. Else, find the closest livrable edge within ``snap_radius_m``,
       split it at the projection point, then insert a short connector
       (``mode_pose=C0``, ``src=gc_neuf``) from the terminal to the
       projection point — provided the connector geometry is fully
       covered by ``delivery_public_area_safe``.
    3. Else flag (``PA_NOT_CONNECTED_TO_LIVRABLE`` / ``PB_NOT_CONNECTED_TO_LIVRABLE``)
       and leave the geometry alone.

    Existing PA / BAT / BAL geometries are NEVER moved (CDC). Only the
    livrable infra is reshaped.
    """
    stats = {
        "pa_connected": 0,
        "pb_connected": 0,
        "terminal_connectors_added": 0,
        "terminal_snap_failed": 0,
    }
    if df is None or df.empty:
        return df, stats

    rows = df.to_dict("records")

    def _connect(terminal_geom, label: str, flag_key: str, target_url: str) -> None:
        # 1) Already touching an endpoint?
        already_on_endpoint = False
        for r in rows:
            g = r.get("geometry")
            if g is None or g.is_empty:
                continue
            # Check exact endpoint touch (not just mid-line proximity)
            gs = list(g.coords)
            for ep in [gs[0], gs[-1]]:
                if Point(ep).distance(terminal_geom) <= touch_tol_m:
                    already_on_endpoint = True
                    break
            if already_on_endpoint:
                break
        if already_on_endpoint:
            if label == "pa":
                stats["pa_connected"] += 1
            else:
                stats["pb_connected"] += 1
            return

        # 2) Find best edge to split inside snap_radius_m.
        best_idx, best_proj, best_dist, best_proj_dist = None, None, float("inf"), 0.0
        for idx, r in enumerate(rows):
            g = r.get("geometry")
            if g is None or g.is_empty or not isinstance(g, LineString):
                continue
            d = g.distance(terminal_geom)
            if d > snap_radius_m or d > best_dist:
                continue
            proj_dist = g.project(terminal_geom)
            if proj_dist <= 0 or proj_dist >= g.length:
                # Terminal projects on an endpoint — handled by tolerance above.
                continue
            proj_pt = g.interpolate(proj_dist)
            best_idx, best_proj, best_dist, best_proj_dist = idx, proj_pt, d, proj_dist

        if best_idx is None:
            stats["terminal_snap_failed"] += 1
            if flag_collector is not None:
                flag_collector.add(
                    flag_key,
                    target_url=target_url,
                    message=(
                        f"{label.upper()} non connecté au livrable "
                        f"(aucune ligne livrée à moins de {snap_radius_m}m)"
                    ),
                )
            return

        # 3) Verify the connector lies in public domain.
        connector = LineString([(terminal_geom.x, terminal_geom.y),
                                (best_proj.x, best_proj.y)])
        if (
            delivery_public_area_safe is not None
            and not delivery_public_area_safe.covers(connector)
        ):
            stats["terminal_snap_failed"] += 1
            if flag_collector is not None:
                flag_collector.add(
                    flag_key,
                    target_url=target_url,
                    message=(
                        f"{label.upper()} non connecté au livrable "
                        f"(connecteur traverse domaine privé)"
                    ),
                )
            return

        # Split the target edge into two sub-segments.
        target_row = rows[best_idx]
        target_geom = target_row["geometry"]
        split = _split_line_at_distance(target_geom, best_proj_dist)
        if split is None:
            stats["terminal_snap_failed"] += 1
            if flag_collector is not None:
                flag_collector.add(
                    flag_key,
                    target_url=target_url,
                    message=f"{label.upper()} split impossible — geometry degenerate",
                )
            return
        seg_a, seg_b = split

        # Replace target_row with seg_a, then append seg_b + connector.
        row_a = dict(target_row)
        row_a["geometry"] = seg_a
        row_a["length_m"] = seg_a.length
        row_b = dict(target_row)
        row_b["geometry"] = seg_b
        row_b["length_m"] = seg_b.length
        connector_row = dict(target_row)
        connector_row["geometry"] = connector
        connector_row["length_m"] = connector.length
        connector_row["statut"] = ""
        connector_row["mode_pose"] = "C0"
        connector_row["src"] = "gc_neuf"
        connector_row["infra_type"] = "gc_neuf"
        # pb_id / pa_id: keep target_row's pa/pb so flag traceability is preserved.

        rows[best_idx] = row_a
        rows.append(row_b)
        rows.append(connector_row)
        stats["terminal_connectors_added"] += 1
        if label == "pa":
            stats["pa_connected"] += 1
        else:
            stats["pb_connected"] += 1

    if pa_sro is not None and not pa_sro.empty:
        for _, pa in pa_sro.iterrows():
            geom = pa.geometry
            if geom is None or geom.is_empty:
                continue
            _connect(
                geom, "pa", "PA_NOT_CONNECTED_TO_LIVRABLE",
                pa.get("id_metier", f"pa#{pa.name}"),
            )

    if pb_sro is not None and not pb_sro.empty:
        for _, pb in pb_sro.iterrows():
            geom = pb.geometry
            if geom is None or geom.is_empty:
                continue
            _connect(
                geom, "pb", "PB_NOT_CONNECTED_TO_LIVRABLE",
                pb.get("pb_id", f"pb#{pb.name}"),
            )

    out = gpd.GeoDataFrame(rows, geometry="geometry", crs=df.crs)
    return out, stats


# ---------------------------------------------------------------------------
# Step 2b — Generic T-junction split (PR #28 Point 2)
# ---------------------------------------------------------------------------


def _split_livrableedges_at_endpoint_projections(
    df: gpd.GeoDataFrame,
    *,
    tol_m: float = 0.5,
) -> tuple[gpd.GeoDataFrame, int]:
    """For each endpoint of each delivered LineString, check whether it
    lands on the middle of another line within ``tol_m``. If so, split
    the target line at the projection point and insert the two halves.

    This is the classic T-junction fix: endpoints must share exact
    coordinates, not just sit close to another edge.
    """
    if df is None or df.empty:
        return df, 0

    import enum
    rows = df.to_dict("records")
    # Collect all endpoints: (coord, source_index, is_first)
    endpoints: list[tuple[tuple[float, float], int, bool]] = []
    for idx, r in enumerate(rows):
        g = r.get("geometry")
        if g is None or g.is_empty or not isinstance(g, LineString):
            continue
        cs = list(g.coords)
        if len(cs) >= 2:
            endpoints.append(((round(cs[0][0], 3), round(cs[0][1], 3)), idx, True))
            endpoints.append(((round(cs[-1][0], 3), round(cs[-1][1], 3)), idx, False))

    splits_added = 0
    changed = True
    max_passes = 5
    while changed and max_passes > 0:
        changed = False
        max_passes -= 1
        # Rebuild rows list after splits
        new_rows: list[dict] = []
        ep_to_split: list[tuple[tuple, dict, tuple, float]] = []

        for idx, r in enumerate(rows):
            g = r.get("geometry")
            if g is None or g.is_empty or not isinstance(g, LineString):
                new_rows.append(r)
                continue
            cs = list(g.coords)
            first = (round(cs[0][0], 3), round(cs[0][1], 3))
            last = (round(cs[-1][0], 3), round(cs[-1][1], 3))
            is_split = False

            for ep_coord, src_idx, is_first in endpoints:
                if src_idx == idx:
                    continue  # same line
                g = LineString(cs)
                if g.length < tol_m * 2:
                    continue  # degenerate, skip
                proj_dist = g.project(Point(ep_coord[0], ep_coord[1]))
                if proj_dist <= 0 or proj_dist >= g.length:
                    continue  # endpoint near other line's endpoint, not middle
                ep_pt = Point(ep_coord[0], ep_coord[1])
                proj_pt = g.interpolate(proj_dist)
                if ep_pt.distance(proj_pt) > tol_m:
                    continue
                split = _split_line_at_distance(g, proj_dist)
                if split is None:
                    continue
                seg_a, seg_b = split

                # Check no degenerate tiny segments
                if seg_a.length < 0.01 or seg_b.length < 0.01:
                    continue

                # Update the endpoint to exact projection coordinate
                row_a = dict(r)
                row_a["geometry"] = seg_a
                row_a["length_m"] = seg_a.length
                row_b = dict(r)
                row_b["geometry"] = seg_b
                row_b["length_m"] = seg_b.length
                new_rows.append(row_a)
                new_rows.append(row_b)
                is_split = True
                splits_added += 1
                changed = True
                break

            if not is_split:
                new_rows.append(r)

        rows = new_rows
        # Rebuild endpoints from new rows
        new_endpoints: list[tuple] = []
        for idx, r in enumerate(rows):
            g = r.get("geometry")
            if g is None or g.is_empty or not isinstance(g, LineString):
                continue
            cs = list(g.coords)
            if len(cs) >= 2:
                new_endpoints.append(((round(cs[0][0], 3), round(cs[0][1], 3)), idx, True))
                new_endpoints.append(((round(cs[-1][0], 3), round(cs[-1][1], 3)), idx, False))
        endpoints = new_endpoints

    out = gpd.GeoDataFrame(rows, geometry="geometry", crs=df.crs)
    return out, splits_added


# ---------------------------------------------------------------------------
# Step 3 — Near-duplicate removal with metier hierarchy
# ---------------------------------------------------------------------------


def _hierarchy_score(row) -> tuple[int, float]:
    """Return ``(priority, length)`` used to sort kept-vs-dropped duplicates."""
    src = (row.get("src") or row.get("infra_type") or "").lower()
    prio = SUPPORT_HIERARCHY.get(src, 5)
    length = float(row.get("length_m") or 0.0)
    return (prio, length)


def _remove_near_duplicates(
    df: gpd.GeoDataFrame,
    *,
    parallel_tol_m: float = NEAR_DUPLICATE_TOL_M,
) -> tuple[gpd.GeoDataFrame, dict]:
    """Drop quasi-parallel duplicates keeping the highest-priority survivor.

    Two segments are considered duplicates of each other when their
    endpoints (regardless of direction) are within ``parallel_tol_m`` of
    each other. Hierarchy: ft/chem < athd < bt < gc_neuf. Equal-priority
    ties are resolved by keeping the SHORTER one (closer to the road,
    less likely to wander into private parcels).
    """
    stats = {
        "exact_duplicates_removed": 0,
        "near_duplicates_removed": 0,
        "parallel_conflicts_resolved": 0,
        "duplicate_parallel_length_m": 0.0,
    }
    if df is None or len(df) < 2:
        return df, stats

    # Build an index keyed by rounded endpoint pair (undirected).
    def _key(g):
        if g is None or g.is_empty or not isinstance(g, LineString):
            return None
        cs = list(g.coords)
        a = (round(cs[0][0], 3), round(cs[0][1], 3))
        b = (round(cs[-1][0], 3), round(cs[-1][1], 3))
        return tuple(sorted((a, b)))

    buckets: dict[tuple, list[int]] = defaultdict(list)
    for idx, g in enumerate(df.geometry):
        k = _key(g)
        if k is None:
            continue
        buckets[k].append(idx)

    keep_idx: set[int] = set(range(len(df)))
    for k, idxs in buckets.items():
        if len(idxs) < 2:
            continue
        # Choose best by hierarchy then length.
        ranked = sorted(idxs, key=lambda i: _hierarchy_score(df.iloc[i]))
        winner = ranked[0]
        for loser in ranked[1:]:
            stats["exact_duplicates_removed"] += 1
            keep_idx.discard(loser)
            stats["duplicate_parallel_length_m"] += float(
                df.iloc[loser].get("length_m") or 0.0
            )

    # Near-duplicates: walk per-bucket-pair where endpoints are within tol.
    # We approximate by scanning unmatched edges via STRtree.
    from shapely.strtree import STRtree
    remaining = sorted(keep_idx)
    geoms = [df.geometry.iloc[i] for i in remaining]
    if len(geoms) >= 2:
        tree = STRtree(geoms)
        # Build a mapping back from "index in geoms" to original df index.
        idx_map = {i: remaining[i] for i in range(len(remaining))}
        seen: set[int] = set()
        for i, gi in enumerate(geoms):
            if idx_map[i] in seen:
                continue
            buf = gi.buffer(parallel_tol_m)
            candidates = tree.query(buf)
            close_orig_idx: list[int] = []
            for j in candidates:
                if int(j) == i:
                    continue
                if idx_map[int(j)] in seen:
                    continue
                gj = geoms[int(j)]
                # Two parallel segments → high Hausdorff symmetry under tol.
                if gi.hausdorff_distance(gj) <= parallel_tol_m:
                    close_orig_idx.append(int(j))
            if not close_orig_idx:
                continue
            group = [i] + close_orig_idx
            # Pick best by hierarchy.
            ranked = sorted(
                group, key=lambda k: _hierarchy_score(df.iloc[idx_map[k]])
            )
            winner = ranked[0]
            for loser in ranked[1:]:
                seen.add(idx_map[loser])
                keep_idx.discard(idx_map[loser])
                stats["near_duplicates_removed"] += 1
                stats["parallel_conflicts_resolved"] += 1
                stats["duplicate_parallel_length_m"] += float(
                    df.iloc[idx_map[loser]].get("length_m") or 0.0
                )

    out = df.iloc[sorted(keep_idx)].reset_index(drop=True)
    return out, stats


# ---------------------------------------------------------------------------
# Step 4 — Strict private-crossing filter for aerial energy / BT
# ---------------------------------------------------------------------------


def _filter_energy_private(
    df: gpd.GeoDataFrame,
    *,
    delivery_public_area_safe=None,
    flag_collector=None,
) -> tuple[gpd.GeoDataFrame, dict]:
    """Drop BT / aerial-energy (E1) rows whose geometry crosses private land.

    ``delivery_public_area_safe`` MUST be the same strict reference as
    used by PR30's final-filter (``delivery_public_area.buffer(0.01)``).
    A BT row not covered by it touches private land somewhere along its
    polyline and must therefore be either removed (servitude énergie
    non transmissible aux télécoms) or flagged for manual review.

    Conservative choice: REMOVE the row and emit a per-SRO flag.
    """
    stats = {
        "energy_private_crossing_count": 0,
        "energy_private_crossing_length_m": 0.0,
        "energy_edges_removed_or_penalized": 0,
    }
    if df is None or df.empty or delivery_public_area_safe is None:
        return df, stats

    def _is_energy(row) -> bool:
        # BT family OR explicit E1 modepose with BT-like origin.
        src = str(row.get("src") or "").lower()
        mp = str(row.get("mode_pose") or "")
        return src == "bt" or (src == "" and mp == "1")

    drop_idx: list[int] = []
    for idx in range(len(df)):
        row = df.iloc[idx]
        if not _is_energy(row):
            continue
        g = row["geometry"]
        if g is None or g.is_empty:
            continue
        if not delivery_public_area_safe.covers(g):
            drop_idx.append(idx)
            stats["energy_private_crossing_count"] += 1
            stats["energy_private_crossing_length_m"] += float(row.get("length_m") or 0.0)
    if drop_idx and flag_collector is not None:
        flag_collector.add(
            "ENERGY_PRIVATE_CROSSING",
            target_url=str(df.iloc[0].get("sro", "?")),
            message=(
                f"Énergie/BT en privé supprimés : "
                f"{len(drop_idx)} arêtes / "
                f"{stats['energy_private_crossing_length_m']:.0f} m"
            ),
        )
    stats["energy_edges_removed_or_penalized"] = len(drop_idx)
    keep = [i for i in range(len(df)) if i not in set(drop_idx)]
    out = df.iloc[keep].reset_index(drop=True)
    return out, stats


# ---------------------------------------------------------------------------
# Step 5 — Continuity audit
# ---------------------------------------------------------------------------


def _build_livrable_topology_graph(df: gpd.GeoDataFrame) -> nx.Graph:
    """Build a NetworkX graph from the delivered geometries.

    Each LineString contributes one edge between its rounded endpoints
    (6 decimals). The graph is used to verify PA→PB reachability AFTER
    delivery. The intermediate vertices of polylines are NOT exploded
    into separate nodes — we only care about endpoint connectivity here.
    """
    G = nx.Graph()
    for _, row in df.iterrows():
        g = row.get("geometry")
        if g is None or g.is_empty or not isinstance(g, LineString):
            continue
        cs = list(g.coords)
        if len(cs) < 2:
            continue
        a = (round(cs[0][0], 6), round(cs[0][1], 6))
        b = (round(cs[-1][0], 6), round(cs[-1][1], 6))
        if a == b:
            continue
        G.add_edge(a, b)
    return G


def _audit_continuity(
    df: gpd.GeoDataFrame,
    pa_sro: gpd.GeoDataFrame,
    pb_sro: gpd.GeoDataFrame,
    *,
    micro_gap_tol_m: float = MICRO_GAP_TOL_M,
    flag_collector=None,
) -> dict:
    """Audit PA→PB reachability + count micro-gaps."""
    stats = {
        "pa_pb_connected_count": 0,
        "pa_pb_disconnected_count": 0,
        "micro_gaps_detected": 0,
        "micro_gaps_unresolved": 0,
        "max_gap_m": 0.0,
    }
    if df is None or df.empty:
        return stats

    G = _build_livrable_topology_graph(df)
    if G.number_of_nodes() == 0:
        return stats

    # Map PA id → terminal node coords (the nearest graph node).
    def _terminal_node(geom) -> Optional[tuple]:
        if geom is None or geom.is_empty:
            return None
        nodes = list(G.nodes())
        if not nodes:
            return None
        best, best_d = None, float("inf")
        for n in nodes:
            d = Point(n[0], n[1]).distance(geom)
            if d < best_d:
                best, best_d = n, d
        if best_d > MICRO_GAP_TOL_M:
            return None  # Terminal not on a delivered node.
        return best

    pa_node_for: dict[str, tuple] = {}
    if pa_sro is not None:
        for _, pa in pa_sro.iterrows():
            pid = pa.get("id_metier")
            if pid is None:
                continue
            n = _terminal_node(pa.geometry)
            if n is not None:
                pa_node_for[pid] = n

    if pb_sro is None or pb_sro.empty:
        return stats

    for _, pb in pb_sro.iterrows():
        pa_id = pb.get("pa_id")
        pb_node = _terminal_node(pb.geometry)
        pa_node = pa_node_for.get(pa_id)
        if pa_node is None or pb_node is None:
            stats["pa_pb_disconnected_count"] += 1
            continue
        try:
            if nx.has_path(G, pa_node, pb_node):
                stats["pa_pb_connected_count"] += 1
            else:
                stats["pa_pb_disconnected_count"] += 1
                if flag_collector is not None:
                    flag_collector.add(
                        "PB_NOT_CONNECTED_TO_LIVRABLE",
                        target_url=pb.get("pb_id", "?"),
                        message=f"Pas de chemin livré entre {pa_id} et PB",
                    )
        except nx.NodeNotFound:
            stats["pa_pb_disconnected_count"] += 1

    # Micro-gaps: pairs of endpoints in different connected components
    # but within ``micro_gap_tol_m`` of each other.
    nodes = list(G.nodes())
    if len(nodes) >= 2:
        from scipy.spatial import cKDTree
        coords = np.array(nodes, dtype=float)
        tree = cKDTree(coords)
        pairs = tree.query_pairs(r=micro_gap_tol_m, output_type="ndarray")
        cc = {n: i for i, comp in enumerate(nx.connected_components(G)) for n in comp}
        for a, b in pairs:
            na, nb = nodes[int(a)], nodes[int(b)]
            if cc.get(na) != cc.get(nb):
                stats["micro_gaps_detected"] += 1
                d = Point(na).distance(Point(nb))
                stats["max_gap_m"] = max(stats["max_gap_m"], d)
                # Unresolved by definition: we don't auto-fix here.
                stats["micro_gaps_unresolved"] += 1
                if flag_collector is not None:
                    flag_collector.add(
                        "MICRO_GAP_UNRESOLVED",
                        target_url=f"({na[0]:.0f},{na[1]:.0f})",
                        message=f"Micro-gap entre noeuds livrés, length={d:.2f}m",
                    )
    return stats


# ---------------------------------------------------------------------------
# Step 6 — Support switch audit (count + flags, no Dijkstra change)
# ---------------------------------------------------------------------------


def _audit_support_switches(df: gpd.GeoDataFrame) -> dict:
    """Count support-family alternations per (pa_id, pb_id) chain.

    A switch is "suspicious" when the same family alternates more than
    once in the same chain, e.g. underground_orange → aerial_energy →
    underground_orange. This is the zigzag pattern Pierre wants to flag.
    """
    stats = {
        "support_switch_count": 0,
        "suspicious_switch_count": 0,
        "support_switches_fixed": 0,  # heuristic dedup never re-routes here
    }
    if df is None or df.empty or "pa_id" not in df.columns:
        return stats

    df_sorted = df.sort_values(["pa_id", "pb_id"]).copy()
    grouped = df_sorted.groupby(["pa_id", "pb_id"], sort=False)
    for _, chain in grouped:
        families = [_support_family_of(r) for _, r in chain.iterrows()]
        # Filter unknowns (not actionable).
        families = [f for f in families if f != "unknown"]
        if len(families) < 2:
            continue
        switches = 0
        family_seen: list[str] = []
        for f in families:
            if family_seen and family_seen[-1] != f:
                switches += 1
            family_seen.append(f)
        stats["support_switch_count"] += switches
        # Suspicious: same family appears more than once non-consecutively.
        unique_fams = set(families)
        for fam in unique_fams:
            indices = [i for i, ff in enumerate(families) if ff == fam]
            for i in range(len(indices) - 1):
                if indices[i + 1] - indices[i] > 1:
                    stats["suspicious_switch_count"] += 1
                    break
    return stats


# ---------------------------------------------------------------------------
# Step 7 — Mutualisation audit
# ---------------------------------------------------------------------------


def _audit_mutualisation(df: gpd.GeoDataFrame) -> dict:
    """Trunk-reuse statistics: edges referenced by more than one PB."""
    stats = {
        "shared_edges_count": 0,
        "trunk_reuse_ratio": 0.0,
        "unshared_duplicate_paths_count": 0,
    }
    if df is None or df.empty or "pa_id" not in df.columns:
        return stats

    # Group by rounded (a, b) coords; count distinct pb_id per key.
    def _key(g):
        cs = list(g.coords)
        a = (round(cs[0][0], 3), round(cs[0][1], 3))
        b = (round(cs[-1][0], 3), round(cs[-1][1], 3))
        return tuple(sorted((a, b)))

    pb_per_key: dict[tuple, set] = defaultdict(set)
    for _, row in df.iterrows():
        g = row.get("geometry")
        if g is None or g.is_empty or not isinstance(g, LineString):
            continue
        k = _key(g)
        pid = row.get("pb_id")
        if pid is not None:
            pb_per_key[k].add(pid)

    shared = sum(1 for v in pb_per_key.values() if len(v) > 1)
    total = max(1, len(pb_per_key))
    stats["shared_edges_count"] = shared
    stats["trunk_reuse_ratio"] = round(shared / total, 3)
    return stats


# ---------------------------------------------------------------------------
# Step 3a — Repair micro-gaps (PR #28 Point 3)
# ---------------------------------------------------------------------------


def _repair_micro_gaps(
    df: gpd.GeoDataFrame,
    *,
    delivery_public_area_safe=None,
    flag_collector=None,
) -> tuple[gpd.GeoDataFrame, dict]:
    """Find endpoints from different connected components within
    ``MICRO_GAP_MAX_FIX_M`` and try to insert a public C0 connector.

    If the connector crosses private land, flag as MICRO_GAP_UNRESOLVED.
    """
    stats = {
        "micro_gaps_detected": 0,
        "micro_gaps_fixed": 0,
        "micro_gaps_unresolved": 0,
        "max_gap_m": 0.0,
    }
    if df is None or df.empty:
        return df, stats

    G = _build_livrable_topology_graph(df)
    if G.number_of_nodes() < 2:
        return df, stats

    nodes = list(G.nodes())
    from scipy.spatial import cKDTree
    coords = np.array(nodes, dtype=float)
    tree = cKDTree(coords)
    pairs = tree.query_pairs(r=MICRO_GAP_MAX_FIX_M, output_type="ndarray")
    cc = {n: i for i, comp in enumerate(nx.connected_components(G)) for n in comp}

    rows = df.to_dict("records")
    fixed_pairs: set = set()
    unresolved_pairs: set = set()

    for a, b in pairs:
        na, nb = nodes[int(a)], nodes[int(b)]
        if cc.get(na) == cc.get(nb):
            continue
        d = Point(na).distance(Point(nb))
        stats["max_gap_m"] = max(stats["max_gap_m"], d)
        stats["micro_gaps_detected"] += 1

        connector = LineString([na, nb])
        if (
            delivery_public_area_safe is not None
            and delivery_public_area_safe.covers(connector)
        ):
            # Add public connector
            row_template = dict(rows[0]) if rows else {}
            connector_row = {
                "sro": row_template.get("sro", "?"),
                "pa_id": row_template.get("pa_id", ""),
                "pb_id": row_template.get("pb_id", ""),
                "statut": "",
                "mode_pose": "C0",
                "src": "gc_neuf",
                "infra_type": "gc_neuf",
                "length_m": d,
                "geometry": connector,
            }
            rows.append(connector_row)
            stats["micro_gaps_fixed"] += 1
            fixed_pairs.add((na, nb))
        else:
            stats["micro_gaps_unresolved"] += 1
            unresolved_pairs.add((na, nb))
            if flag_collector is not None:
                flag_collector.add(
                    "MICRO_GAP_UNRESOLVED",
                    target_url=f"({na[0]:.0f},{na[1]:.0f})",
                    message=f"Micro-gap entre noeuds livrés, length={d:.2f}m",
                )

    out = gpd.GeoDataFrame(rows, geometry="geometry", crs=df.crs)
    return out, stats


# ---------------------------------------------------------------------------
# Step 5a — Smooth support switches (PR #28 Point 4)
# ---------------------------------------------------------------------------


def _smooth_support_switches(
    df: gpd.GeoDataFrame,
    *,
    delivery_public_area_safe=None,
) -> tuple[gpd.GeoDataFrame, dict]:
    """Detect and fix BT-sandwich zigzags: when a BT segment is
    surrounded by Orange/FT segments and a near-parallel Orange
    alternative exists, drop the BT segment.
    """
    stats = {
        "support_switches_fixed": 0,
    }
    if df is None or len(df) < 3:
        return df, stats

    # Build a lookup keyed by rounded endpoints (undirected).
    def _key(g):
        cs = list(g.coords)
        a = (round(cs[0][0], 3), round(cs[0][1], 3))
        b = (round(cs[-1][0], 3), round(cs[-1][1], 3))
        return tuple(sorted((a, b)))

    # Index edges by their key
    by_key: dict[tuple, list[int]] = defaultdict(list)
    for idx, row in df.iterrows():
        g = row.get("geometry")
        if g is None or g.is_empty:
            continue
        k = _key(g)
        by_key[k].append(idx)

    # Find BT sandwich: look for patterns where BT segment endpoints
    # are also endpoints of Orange/FT segments.
    drop_idx: set = set()
    for idx in range(len(df)):
        row = df.iloc[idx]
        src = str(row.get("src") or "").lower()
        if src != "bt":
            continue
        g = row.get("geometry")
        if g is None or g.is_empty or not isinstance(g, LineString):
            continue

        k = _key(g)
        # Check if there's an Orange/FT segment with same key
        has_orange_parallel = False
        for alt_idx in by_key.get(k, []):
            if alt_idx == idx:
                continue
            alt_row = df.iloc[alt_idx]
            alt_src = str(alt_row.get("src") or "").lower()
            if alt_src in ("ft", "chem", "athd"):
                has_orange_parallel = True
                break

        # Also check near-parallel (Hausdorff distance check)
        if not has_orange_parallel:
            for alt_idx in range(len(df)):
                if alt_idx == idx:
                    continue
                alt_row = df.iloc[alt_idx]
                alt_src = str(alt_row.get("src") or "").lower()
                if alt_src not in ("ft", "chem", "athd"):
                    continue
                alt_g = alt_row.get("geometry")
                if alt_g is None or alt_g.is_empty:
                    continue
                # Near-parallel: endpoints close AND hausdorff within tol
                if g.hausdorff_distance(alt_g) <= NEAR_DUPLICATE_TOL_M * 3:
                    # Endpoints also close
                    alt_k = _key(alt_g)
                    a_end_a = k[0]
                    a_end_b = k[1] if len(k) > 1 else k[0]
                    close_enough = (
                        Point(a_end_a[0], a_end_a[1]).distance(
                            Point(alt_k[0][0], alt_k[0][1])
                        ) <= NEAR_DUPLICATE_TOL_M * 3
                    )
                    if close_enough:
                        has_orange_parallel = True
                        break

        if has_orange_parallel:
            drop_idx.add(idx)
            stats["support_switches_fixed"] += 1

    keep = [i for i in range(len(df)) if i not in drop_idx]
    out = df.iloc[keep].reset_index(drop=True)
    return out, stats


# ---------------------------------------------------------------------------
# Step 6a — Reconnect after energy removal (PR #28 Point 5)
# ---------------------------------------------------------------------------


def _reconnect_after_energy_removal(
    df_before: gpd.GeoDataFrame,
    df_after: gpd.GeoDataFrame,
    *,
    delivery_public_area_safe=None,
    flag_collector=None,
) -> tuple[gpd.GeoDataFrame, dict]:
    """If removing BT/private edges disconnected parts of the network,
    try to reconnect endpoints with a short public C0 connector.
    """
    stats = {
        "energy_reconnectors_added": 0,
        "energy_reconnect_failed": 0,
    }
    if df_after is None or df_after.empty:
        return df_after, stats

    G = _build_livrable_topology_graph(df_after)
    if G.number_of_nodes() < 2:
        return df_after, stats

    nodes = list(G.nodes())
    from scipy.spatial import cKDTree
    coords = np.array(nodes, dtype=float)
    tree = cKDTree(coords)
    pairs = tree.query_pairs(r=MICRO_GAP_MAX_FIX_M * 2, output_type="ndarray")
    cc = {n: i for i, comp in enumerate(nx.connected_components(G)) for n in comp}

    rows = df_after.to_dict("records")
    fixed = 0
    failed = 0

    for a, b in pairs:
        na, nb = nodes[int(a)], nodes[int(b)]
        if cc.get(na) == cc.get(nb):
            continue
        d = Point(na).distance(Point(nb))
        connector = LineString([na, nb])

        if (
            delivery_public_area_safe is not None
            and delivery_public_area_safe.covers(connector)
        ):
            row_template = dict(rows[0]) if rows else {}
            connector_row = {
                "sro": row_template.get("sro", "?"),
                "pa_id": row_template.get("pa_id", ""),
                "pb_id": row_template.get("pb_id", ""),
                "statut": "",
                "mode_pose": "C0",
                "src": "gc_neuf",
                "infra_type": "gc_neuf",
                "length_m": d,
                "geometry": connector,
            }
            rows.append(connector_row)
            fixed += 1
        else:
            failed += 1
            if flag_collector is not None:
                flag_collector.add(
                    "ENERGY_RECONNECT_FAILED",
                    target_url=f"({na[0]:.0f},{na[1]:.0f})",
                    message=f"Reconnect impossible (privé/trop loin) après suppression énergie",
                )

    stats["energy_reconnectors_added"] = fixed
    stats["energy_reconnect_failed"] = failed

    out = gpd.GeoDataFrame(rows, geometry="geometry", crs=df_after.crs)
    return out, stats


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def finalize_livrable_topology(
    df: gpd.GeoDataFrame,
    pa_sro: gpd.GeoDataFrame,
    pb_sro: gpd.GeoDataFrame,
    sro_code: str,
    *,
    delivery_public_area_safe=None,
    flag_collector=None,
) -> tuple[gpd.GeoDataFrame, dict]:
    """Run the full PR31 topology validation pipeline.

    Returns ``(cleaned_df, stats)``. ``stats`` is a dict with all the
    metrics required by the ``[CONTINUITY QA]`` / ``[DEDUP QA]`` /
    ``[ENERGY QA]`` / ``[SUPPORT QA]`` / ``[SNAP QA]`` / ``[MUTUAL QA]``
    log lines.
    """
    stats: dict = {}
    if df is None or df.empty:
        return df, stats

    # 1) Snap endpoints to exact identical coordinates.
    df1, n_snapped = _snap_endpoints_to_exact(df)
    stats["endpoints_snapped"] = n_snapped

    # 2) Connect PA/PB to the livrable network.
    df2, snap_stats = _ensure_terminals_connected(
        df1, pa_sro, pb_sro,
        delivery_public_area_safe=delivery_public_area_safe,
        flag_collector=flag_collector,
    )
    stats.update(snap_stats)

    # 2b) Generic T-junction split (PR #28 Point 2).
    df2b, n_t_splits = _split_livrableedges_at_endpoint_projections(df2)
    stats["t_junction_splits"] = n_t_splits

    # 3) Repair micro-gaps (PR #28 Point 3).
    df3, gap_stats = _repair_micro_gaps(
        df2b, delivery_public_area_safe=delivery_public_area_safe,
        flag_collector=flag_collector,
    )
    stats.update(gap_stats)

    # 4) Remove near-duplicates.
    df4, dedup_stats = _remove_near_duplicates(df3)
    stats.update(dedup_stats)

    # 5) Smooth support switches (PR #28 Point 4).
    df5, sw_fix_stats = _smooth_support_switches(df4)
    stats.update(sw_fix_stats)

    # 6) Filter aerial energy / BT crossing private land.
    df6, energy_stats = _filter_energy_private(
        df5, delivery_public_area_safe=delivery_public_area_safe,
        flag_collector=flag_collector,
    )
    stats.update(energy_stats)

    # 7) Reconnect after energy removal (PR #28 Point 5).
    df7, reconnect_stats = _reconnect_after_energy_removal(
        df5, df6, delivery_public_area_safe=delivery_public_area_safe,
        flag_collector=flag_collector,
    )
    stats.update(reconnect_stats)

    # 8) Snap endpoints again (connectors may introduce new near-identical coords).
    df8, n_re_snap = _snap_endpoints_to_exact(df7)
    if n_re_snap:
        stats["endpoints_re_snapped_post_fix"] = n_re_snap
        # Re-do T-junction split after re-snap if new endpoints were created
        df8b, n_re_t = _split_livrableedges_at_endpoint_projections(df8)
        if n_re_t:
            stats["t_junction_splits_post_snap"] = n_re_t
            df8 = df8b
    else:
        df8b = df8

    # 9) Continuity audit (PA→PB reachability + micro-gaps).
    final_df = df8 if n_re_snap else (df8b if isinstance(df8b, gpd.GeoDataFrame) else df7)
    cont_stats = _audit_continuity(
        final_df, pa_sro, pb_sro,
        flag_collector=flag_collector,
    )
    stats.update(cont_stats)

    # 10) Support switch audit.
    sw_stats = _audit_support_switches(final_df)
    stats.update(sw_stats)

    # 11) Mutualisation audit.
    mut_stats = _audit_mutualisation(final_df)
    stats.update(mut_stats)

    # 12) Emit the QA log block.
    _log_pr31_block(sro_code, stats)
    return final_df, stats


# ---------------------------------------------------------------------------
# Logging helpers
# ---------------------------------------------------------------------------


def _log_pr31_block(sro_code: str, s: dict) -> None:
    """Emit the 6 PR31 grep-friendly QA lines."""
    log.info(
        "[CONTINUITY QA] sro=%s pa_pb_connected=%d pa_pb_disconnected=%d "
        "micro_gaps_detected=%d micro_gaps_fixed=%d micro_gaps_unresolved=%d max_gap_m=%.2f",
        sro_code,
        s.get("pa_pb_connected_count", 0),
        s.get("pa_pb_disconnected_count", 0),
        s.get("micro_gaps_detected", 0),
        s.get("micro_gaps_fixed", 0),
        s.get("micro_gaps_unresolved", 0),
        float(s.get("max_gap_m", 0.0) or 0.0),
    )
    log.info(
        "[SNAP QA] sro=%s pa_connected=%d pb_connected=%d "
        "terminal_connectors_added=%d terminal_snap_failed=%d "
        "t_junction_splits=%d",
        sro_code,
        s.get("pa_connected", 0),
        s.get("pb_connected", 0),
        s.get("terminal_connectors_added", 0),
        s.get("terminal_snap_failed", 0),
        s.get("t_junction_splits", 0),
    )
    log.info(
        "[DEDUP QA] sro=%s exact_duplicates_removed=%d near_duplicates_removed=%d "
        "parallel_conflicts_resolved=%d duplicate_parallel_length_m=%.0f",
        sro_code,
        s.get("exact_duplicates_removed", 0),
        s.get("near_duplicates_removed", 0),
        s.get("parallel_conflicts_resolved", 0),
        float(s.get("duplicate_parallel_length_m", 0.0) or 0.0),
    )
    log.info(
        "[SUPPORT QA] sro=%s support_switch_count=%d suspicious_switch_count=%d "
        "support_switches_fixed=%d",
        sro_code,
        s.get("support_switch_count", 0),
        s.get("suspicious_switch_count", 0),
        s.get("support_switches_fixed", 0),
    )
    log.info(
        "[ENERGY QA] sro=%s energy_private_crossing_count=%d "
        "energy_private_crossing_length_m=%.0f removed_or_penalized=%d "
        "energy_reconnectors_added=%d energy_reconnect_failed=%d",
        sro_code,
        s.get("energy_private_crossing_count", 0),
        float(s.get("energy_private_crossing_length_m", 0.0) or 0.0),
        s.get("energy_edges_removed_or_penalized", 0),
        s.get("energy_reconnectors_added", 0),
        s.get("energy_reconnect_failed", 0),
    )
    log.info(
        "[MUTUAL QA] sro=%s shared_edges_count=%d trunk_reuse_ratio=%.2f "
        "unshared_duplicate_paths_count=%d",
        sro_code,
        s.get("shared_edges_count", 0),
        float(s.get("trunk_reuse_ratio", 0.0) or 0.0),
        s.get("unshared_duplicate_paths_count", 0),
    )
