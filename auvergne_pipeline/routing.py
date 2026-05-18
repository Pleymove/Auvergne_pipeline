"""Routing PA -> PB on combined graph (public infra + IGN routes).

Builds a NetworkX graph from the union of:
  - Filtered public infrastructure (``filters.build_reusable_infra``)
  - IGN BD TOPO routes (``ign_routes.load_ign_routes_for_sro``)
  - GC neuf C0 edges (``pb_fictif.build_pb_fictifs``)

Then snaps PA/PB endpoints onto the graph, runs Dijkstra for each
(PA, PB) pair, and returns the traversed edges tagged with
``statut`` / ``mode_pose``.
"""

from __future__ import annotations

import logging
import math
import time
from typing import List, Optional

import geopandas as gpd
import networkx as nx
import numpy as np
import pandas as pd
import shapely
from shapely.geometry import LineString, Point
from shapely import STRtree
import shapely.ops  # PR #26: substring for edge geometry splitting

from . import config

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
SNAP_TOLERANCE_M = 50.0         # nearest-node lookup radius (PR #21: was 0.5)
SNAP_PROJECTION_RADIUS_M = 200.0  # edge-projection fallback radius (PR #21)
WELD_RADIUS_M = 2.0             # node fusion radius for topology welding (PR #22)
SNAP_ENDPOINT_RADIUS_M = 3.0    # A/Z endpoint snap radius (PR #27: close gaps)
EDGE_KEY_SEP = "::"

# PR #28: sentinel to distinguish "not provided" from "explicitly None"
_SENTINEL = object()

# ---------------------------------------------------------------------------
# PR #29 — Routing weight hierarchy (CDC: prefer existing over IGN/GC neuf)
# ---------------------------------------------------------------------------
# Existing infrastructure (bt/ft/athd/chem) gets the natural length cost.
# GC neuf (artificial bridges) is heavily penalised so Dijkstra only takes
# them as a last resort when there is no existing path.
# IGN BD TOPO routes (raw road centrelines) are penalised even more, since
# any IGN traversal that ends up in livrable_infra is converted to C0 — we
# only want IGN as a last-resort topological connector.
WEIGHT_FACTOR_GC_NEUF = 10.0
WEIGHT_FACTOR_IGN_ROUTE = 30.0

# ---------------------------------------------------------------------------
# PR #30 — IGN-as-C0 delivery gate
# ---------------------------------------------------------------------------
# PR29 still allowed every IGN edge traversed by Dijkstra to be converted
# into a C0/gc_neuf row in livrable_infra. On dense rural SROs this
# produced long red C0 sections that visually replaced the existing aerial
# infra. PR30 introduces a strict gate:
#
#   IGN edges are delivered as C0 only if:
#     (a) length_m <= IGN_DELIVERY_MAX_LENGTH_M (short connector), AND
#     (b) the edge geometry lies entirely within ``delivery_public_area``
#         (parcelles publiques + inter-parcel road right-of-way, NOT the
#          permissive IGN buffer used for routing).
#
# IGN edges that fail either check are still TRAVERSABLE for routing
# purposes (Dijkstra can use them to reach a PB) but are silently
# dropped from the livrable. The blocked length is logged via
# [ROUTING QA] ign_route_blocked_m and a IGN_ROUTE_BLOCKED flag is added
# per SRO so Pierre can locate the gap on the QGIS flags layer.
IGN_DELIVERY_MAX_LENGTH_M = 50.0

# PR #31 H — cumulative IGN delivery limit per SRO. With the per-edge cap
# at 50 m, an SRO could still accumulate dozens of small IGN connectors
# that total kilometres of C0 in livrable_infra. We therefore introduce
# a per-SRO total cap: above this, further IGN edges are BLOCKED from
# delivery (still routable, just not visible) and counted in
# ign_route_blocked_m. The cap is generous on purpose — short legitimate
# connectors stay welcome; only the accumulation pattern is curbed.
MAX_IGN_DELIVERED_PER_SRO_M = 300.0

# PR #33 — No straight visible connectors. Any edge that is a direct
# LineString([A, B]) without a real source geometry (existing infra or
# IGN route) is treated as a CONNECTOR. Connectors are:
#   - ALLOWED as virtual topology helpers (deliverable=False)
#     for Dijkstra routing connectivity
#   - FORBIDDEN as visible segments in livrable_infra when > 3m
#   - Micro-snap (<= 3m) may be virtual and non-delivered
MAX_STRAIGHT_CONNECTOR_M = 3.0

# PR #39 — terminals may use an internal-only anchor for routing when they
# sit a little way off the delivered graph. This must never become a visible
# livrable_infra C0 row.
MAX_LOGICAL_TERMINAL_ANCHOR_M = 30.0

# PR #41 — Per-path IGN-as-C0 budget. PR #40's field run delivered
# >5 km of IGN-as-C0 on a single SRO (high_gc_ratio=0.96) producing the
# blue spaghetti Pierre flagged. These caps reject (don't truncate) any
# single PA→PB path whose IGN share exceeds either bound: an absolute
# ceiling, AND a ratio-of-total-length that only kicks in once the
# path is long enough to look like a real itinerary (otherwise a
# small purely-IGN fallback would be wrongly rejected).
PR41_MAX_IGN_PER_PATH_M = 800.0
PR41_MAX_IGN_RATIO_PER_PATH = 0.80
# Ratio rule only applies above this absolute path length; below, only
# the absolute cap matters. This keeps the synthetic unit tests with
# 20–50 m IGN-only paths intact while still catching real "PA→PB has
# 2 km of C0 instead of using nearby existing infra" spaghettis.
PR41_RATIO_MIN_TOTAL_M = 300.0

# PR #41 — When the global ``_snap`` fails to anchor an orphan PA
# (e.g. SRO 63258/LLW/PMZ/24228 on the 2026-05-18 field run), allow a
# longer logical anchor (still virtual, never delivered) so the rest
# of the PBs aren't dropped wholesale. Logical anchors do not become
# C0 in the livrable; their only role is to give Dijkstra an entry
# point onto the closest existing / IGN edge.
PR41_MAX_LOGICAL_ANCHOR_M_FOR_ORPHAN = 150.0

# PR #40 experimental mode: parcels remain visual context only. Routing and
# delivery are constrained by existing infrastructure and real IGN road
# geometries, not by public/private parcel ownership.
ALLOW_IGN_ROAD_C0_WITHOUT_PARCEL_GATE = True

# PR #31 H — soft warning when IGN-derived C0 dominates the livrable.
IGN_DELIVERED_TOTAL_RATIO_WARN = 0.10


def _routing_weight_for(data: dict) -> float:
    """Compute the Dijkstra weight for an edge based on its source type.

    Strict hierarchy: existing < gc_neuf < ign_route. The base unit is the
    geometric length so that a path through existing infra is always picked
    when one exists, even if a few extra metres longer than an IGN/C0 path.
    """
    base = float(data.get("length", 1.0))
    edge_type = data.get("type")
    if edge_type == "gc_neuf":
        return base * WEIGHT_FACTOR_GC_NEUF
    if edge_type == "ign_route":
        return base * WEIGHT_FACTOR_IGN_ROUTE
    return base


def _heal_existing_infra_topology(
    infra_filtered: gpd.GeoDataFrame,
) -> gpd.GeoDataFrame:
    """PR #37 — Repair SIG-level topology of EXISTING infra BEFORE routing.

    Pierre's field test after PR #36 still showed ``pa_pb_connected_ratio
    = 0.00`` on most pilote SROs, with infra rows being delivered but
    Dijkstra unable to chain them into a continuous PA→PB path. Root
    cause: the existing-infra layer in the input GPKG already has
    micro-gaps between endpoints, T-junctions where one segment ends on
    the middle of another, and unwelded close endpoints. PR #33/#36
    had moved these "heals" into the routing graph (mixing infra +
    IGN), so endpoints were being snapped onto IGN polylines instead
    of onto neighbouring existing infra — pushing the path through IGN
    and inflating C0.

    PR #37 fixes this BEFORE building the routing graph:
      1. Endpoints within ``ENDPOINT_SNAP_TOL_M`` are merged into a
         shared coordinate (no row added).
      2. Endpoints that land on the middle of another line within the
         same tolerance trigger a T-junction split: the target line is
         cut at the projection so the two segments share a vertex.
      3. A second snap-pass consolidates whatever the T-junction split
         exposed.

    No new geometry is invented and no C0 row is created. ``statut``,
    ``mode_pose``, ``infra_type``, ``src`` are preserved on every row.
    """
    if infra_filtered is None or infra_filtered.empty:
        return infra_filtered
    # Local import to avoid a circular import at module load time.
    from . import livrable_topology as _lt

    df = infra_filtered
    df, n_snap_1 = _lt._snap_endpoints_to_exact(
        df, tol_m=_lt.ENDPOINT_SNAP_TOL_M,
    )
    df, n_split = _lt._split_livrableedges_at_endpoint_projections(
        df, tol_m=_lt.ENDPOINT_SNAP_TOL_M,
    )
    df, n_snap_2 = _lt._snap_endpoints_to_exact(
        df, tol_m=_lt.ENDPOINT_SNAP_TOL_M,
    )
    if n_snap_1 or n_split or n_snap_2:
        log.info(
            "[INFRA HEAL] endpoints_snapped=%d t_junctions_split=%d "
            "endpoints_resnapped=%d",
            n_snap_1, n_split, n_snap_2,
        )
    return df


def _public_area_safe(public_area):
    """Pre-compute ``public_area.buffer(0.01)`` once per SRO (PR #29 B1).

    Returns ``None`` when *public_area* is missing/empty so callers can
    short-circuit without recomputing the buffer in inner loops.
    """
    if public_area is None or public_area is _SENTINEL:
        return None
    if hasattr(public_area, "is_empty") and public_area.is_empty:
        return None
    try:
        return public_area.buffer(0.01)
    except Exception:
        return None


def _explode_to_linestrings(geom):
    """Yield LineString parts from a (Multi)LineString geometry."""
    if geom is None or geom.is_empty:
        return
    if geom.geom_type == "LineString":
        yield geom
    elif geom.geom_type == "MultiLineString":
        for part in geom.geoms:
            if not part.is_empty:
                yield part


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _edge_key(u, v) -> str:
    """Deterministic key for an undirected edge."""
    a, b = (u, v) if u[0] < v[0] or (u[0] == v[0] and u[1] < v[1]) else (v, u)
    return f"{a[0]:.6f},{a[1]:.6f}{EDGE_KEY_SEP}{b[0]:.6f},{b[1]:.6f}"


def _point_key(pt) -> tuple[float, float]:
    """Rounded coordinate key for a node (accepts Point or (x,y) tuple)."""
    if isinstance(pt, Point):
        return (round(pt.x, 6), round(pt.y, 6))
    return (round(pt[0], 6), round(pt[1], 6))


def _coord2d(c) -> tuple[float, float]:
    """Project any coordinate to 2-D (drops Z/M dims)."""
    return (float(c[0]), float(c[1]))


def _path_membership_to_set(value) -> set[str]:
    """Normalize serialized/shared path metadata to a set of path ids."""
    if value is None:
        return set()
    try:
        if pd.isna(value):
            return set()
    except (TypeError, ValueError):
        pass
    if isinstance(value, set):
        return {str(v) for v in value if str(v)}
    if isinstance(value, (list, tuple)):
        return {str(v) for v in value if str(v)}
    text = str(value).strip()
    if not text:
        return set()
    parts: list[str] = []
    for chunk in text.replace(",", "|").split("|"):
        p = chunk.strip()
        if p:
            parts.append(p)
    return set(parts)


def _serialize_path_membership(paths: set[str]) -> str:
    """Serialize path membership deterministically for GeoPackage output."""
    return "|".join(sorted(str(p) for p in paths if str(p)))


# ---------------------------------------------------------------------------
# Topology welding (PR #22 / PR #22.5)
# ---------------------------------------------------------------------------

# Scipy is guaranteed present in QGIS embedded Python 4.0.1
# (already used elsewhere in the pipeline).

def _weld_close_nodes(
    G: nx.Graph, weld_radius_m: float = WELD_RADIUS_M
) -> nx.Graph:
    """Fuse nodes within ``weld_radius_m`` via scipy cKDTree + union-find.

    ATHD / BT / FT / cheminement edges sit at cm-level offsets from each
    other and from IGN routes, producing a graph of N disconnected islands.
    Welding merges close endpoints into a single node, reconnecting the
    graph so Dijkstra can find paths across all infrastructure layers.

    PR #22.5: replaced scikit-learn clustering with scipy.spatial.cKDTree
    (scikit-learn unavailable in QGIS 4.0.1 embedded Python on Pierre's box).
    """
    if G.number_of_nodes() < 2:
        return G

    nodes = list(G.nodes())
    coords = np.array(nodes, dtype=float)

    # 1) Build KDTree and find all node pairs within weld_radius_m
    from scipy.spatial import cKDTree
    tree = cKDTree(coords)
    pairs = tree.query_pairs(r=weld_radius_m, output_type='ndarray')

    # 2) Union-find with path compression to group pairs into clusters
    parent = list(range(len(nodes)))

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

    # 3) Compute cluster labels
    labels = np.array([_find(i) for i in range(len(nodes))])

    # 4) Representative coord per cluster (centroid, rounded to 6 decimals)
    cluster_to_centroid: dict[int, tuple[float, float]] = {}
    for label in np.unique(labels):
        mask = labels == label
        cx = round(float(coords[mask, 0].mean()), 6)
        cy = round(float(coords[mask, 1].mean()), 6)
        cluster_to_centroid[int(label)] = (cx, cy)

    old_to_new = {
        nodes[i]: cluster_to_centroid[int(labels[i])]
        for i in range(len(nodes))
    }

    # 5) Rebuild graph with fused nodes
    G_welded = nx.Graph()
    for u, v, data in G.edges(data=True):
        nu, nv = old_to_new[u], old_to_new[v]
        if nu == nv:
            continue  # self-loop after welding — skip
        if G_welded.has_edge(nu, nv):
            if data.get("length", 0) < G_welded[nu][nv].get("length", float("inf")):
                G_welded[nu][nv].update(data)
        else:
            G_welded.add_edge(nu, nv, **data)

    # 6) Log
    n_before = G.number_of_nodes()
    n_after = G_welded.number_of_nodes()
    n_cc_before = nx.number_connected_components(G)
    n_cc_after = nx.number_connected_components(G_welded)
    log.info(
        "[WELD] %d -> %d noeuds (-%d), %d -> %d composantes connexes",
        n_before, n_after, n_before - n_after, n_cc_before, n_cc_after,
    )
    return G_welded


# ---------------------------------------------------------------------------
# PR #27 Part B — A/Z endpoint topology snap
# ---------------------------------------------------------------------------


def _snap_endpoints_topology(
    G: nx.Graph,
    snap_radius_m: float = SNAP_ENDPOINT_RADIUS_M,
) -> dict:
    """Snap close A/Z endpoints (degree-1 nodes) into shared nodes.

    Extracts only true A/Z endpoints (nodes connected to exactly 1 edge),
    finds pairs within *snap_radius_m* via cKDTree, and merges them into
    the same graph node. Internal vertices (degree >= 2) are NEVER merged.

    After merging, stored geometries on affected edges are patched so their
    endpoint coordinates match the canonical representative node — this
    eliminates visible gaps in QGIS output.

    Returns a stats dict for QA logging.
    """
    if G.number_of_nodes() < 2:
        return {"endpoints_snapped": 0, "cc_before": 0, "cc_after": 0}

    from scipy.spatial import cKDTree

    # ── 1) Extract only degree-1 nodes (true A/Z endpoints) ──────────
    endpoint_indices = []
    endpoint_coords = []
    endpoint_nodes = []
    nodes = list(G.nodes())
    for i, n in enumerate(nodes):
        if G.degree(n) == 1:  # true A/Z endpoint
            endpoint_indices.append(i)
            endpoint_nodes.append(n)
            endpoint_coords.append((n[0], n[1]))

    if len(endpoint_nodes) < 2:
        cc = nx.number_connected_components(G)
        return {"endpoints_snapped": 0, "cc_before": cc, "cc_after": cc}

    coords_arr = np.array(endpoint_coords, dtype=float)
    cc_before = nx.number_connected_components(G)

    # ── 2) Find close endpoint pairs ──────────────────────────────────
    tree = cKDTree(coords_arr)
    pairs = tree.query_pairs(r=snap_radius_m, output_type="ndarray")

    if len(pairs) == 0:
        return {"endpoints_snapped": 0, "cc_before": cc_before, "cc_after": cc_before}

    # ── 3) Union-find to group close endpoints ────────────────────────
    parent = list(range(len(endpoint_nodes)))
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

    # Canonical representative per cluster
    rep = {}
    for i, n in enumerate(endpoint_nodes):
        root = _find(i)
        if root not in rep:
            rep[root] = n
    ep_old_to_new = {
        endpoint_nodes[i]: rep[_find(i)]
        for i in range(len(endpoint_nodes))
    }

    # ── 4) Build full mapping (internal nodes → identity) ────────────
    old_to_new = {n: n for n in nodes}
    old_to_new.update(ep_old_to_new)
    n_changed = sum(1 for o, n in old_to_new.items() if o != n)

    # ── 5) Rebuild graph with merged nodes + patch geometries ────────
    G2 = nx.Graph()
    for u, v, data in G.edges(data=True):
        nu, nv = old_to_new.get(u, u), old_to_new.get(v, v)
        if nu == nv:
            continue
        # PR #27 amend: patch geometry endpoints to match canonical nodes
        geom = data.get("geometry")
        if geom is not None and isinstance(geom, LineString) and not geom.is_empty:
            # PR #28 BLOQUANT 1: force all coords to 2-D (Shapely rejects mixed 2D/3D)
            coords_list = [_coord2d(c) for c in geom.coords]
            if len(coords_list) < 2:
                continue  # degenerate, skip
            changed = False
            # Use proximity to match coords[0]/coords[-1] to nu/nv,
            # since NetworkX may flip edge direction in undirected graphs.
            from shapely.geometry import Point as _Point
            p_first = _Point(coords_list[0])
            p_last = _Point(coords_list[-1])
            if u != nu and v != nv:
                # Both endpoints remapped — match by proximity
                d_first_nu = p_first.distance(_Point(nu))
                d_first_nv = p_first.distance(_Point(nv))
                if d_first_nu <= d_first_nv:
                    coords_list[0] = (float(nu[0]), float(nu[1]))
                    coords_list[-1] = (float(nv[0]), float(nv[1]))
                else:
                    coords_list[0] = (float(nv[0]), float(nv[1]))
                    coords_list[-1] = (float(nu[0]), float(nu[1]))
                changed = True
            elif u != nu:
                # Only u changed
                if p_first.distance(_Point(nu)) <= p_last.distance(_Point(nu)):
                    coords_list[0] = (float(nu[0]), float(nu[1]))
                else:
                    coords_list[-1] = (float(nu[0]), float(nu[1]))
                changed = True
            elif v != nv:
                # Only v changed
                if p_first.distance(_Point(nv)) <= p_last.distance(_Point(nv)):
                    coords_list[0] = (float(nv[0]), float(nv[1]))
                else:
                    coords_list[-1] = (float(nv[0]), float(nv[1]))
                changed = True
            if changed:
                # Validate before creating LineString (PR #28 BLOQUANT 1)
                if len(coords_list) < 2:
                    continue
                if coords_list[0] == coords_list[-1]:
                    continue  # degenerate zero-length line
                data = dict(data)
                try:
                    data["geometry"] = LineString(coords_list)
                except (ValueError, TypeError):
                    log.warning("[TOPO SNAP] Invalid geometry skipped for edge %s-%s", u, v)
                    continue
        if G2.has_edge(nu, nv):
            if data.get("length", 0) < G2[nu][nv].get("length", float("inf")):
                G2[nu][nv].update(data)
        else:
            G2.add_edge(nu, nv, **data)

    # Copy isolated nodes
    for n in G.nodes():
        if G.degree(n) == 0:
            nn = old_to_new.get(n, n)
            if nn not in G2:
                G2.add_node(nn)

    G.clear()
    G.add_nodes_from(G2.nodes())
    G.add_edges_from(G2.edges(data=True))

    cc_after = nx.number_connected_components(G)

    log.info(
        "[TOPO SNAP] %d endpoints snapés (-%d uniques), "
        "%d -> %d composantes connexes",
        len(endpoint_nodes), n_changed, cc_before, cc_after,
    )

    return {
        "endpoints_snapped": n_changed,
        "cc_before": cc_before,
        "cc_after": cc_after,
    }


# ---------------------------------------------------------------------------

def _build_graph(
    infra: gpd.GeoDataFrame,
    ign_routes: gpd.GeoDataFrame,
    snap_tol: float = SNAP_TOLERANCE_M,
) -> nx.Graph:
    """Build a weighted undirected graph from LineString edges."""
    G = nx.Graph()

    def _add_edges(gdf: gpd.GeoDataFrame, attrs: dict) -> None:
        if gdf is None or gdf.empty:
            return
        for row_idx, row in gdf.iterrows():
            for part_idx, line in enumerate(_explode_to_linestrings(row.geometry)):
                coords = list(line.coords)
                source_line_id = f"{attrs.get('type', 'edge')}:{row_idx}:{part_idx}"
                for i in range(len(coords) - 1):
                    a = coords[i]
                    b = coords[i + 1]
                    length = Point(a).distance(Point(b))
                    edge_attrs = {
                        k: row.get(k)
                        for k in ("statut", "mode_pose", "src", "sro_code")
                        if k in gdf.columns
                    }
                    # PR #23 Bug A: tag infra_type for QML coloring
                    edge_attrs.setdefault(
                        "infra_type",
                        edge_attrs.get("src") or attrs.get("type", "?"),
                    )
                    # PR #26: store actual source geometry on the edge
                    seg_geom = LineString([a, b])
                    G.add_edge(
                        _point_key(a),
                        _point_key(b),
                        length=length,
                        geometry=seg_geom,
                        _source_line_id=source_line_id,
                        **attrs,
                        **edge_attrs,
                    )

    _add_edges(infra, {"type": "infra"})
    _add_edges(ign_routes, {"type": "ign_route"})

    if G.number_of_nodes() == 0:
        log.warning("[ROUTING] Graphe vide — pas d'aretes ni infra ni IGN")

    log.info("[ROUTING] Graphe brut: %d noeuds, %d aretes", G.number_of_nodes(), G.number_of_edges())
    G = _weld_close_nodes(G, weld_radius_m=WELD_RADIUS_M)
    log.info("[ROUTING] Graphe welded: %d noeuds, %d aretes", G.number_of_nodes(), G.number_of_edges())
    return G


# ---------------------------------------------------------------------------


def _add_gc_neuf_to_graph(
    G: nx.Graph,
    gc_neuf: gpd.GeoDataFrame,
    snap_tol: float = SNAP_TOLERANCE_M,
    public_area: object = _SENTINEL,  # PR #28: spatial validation
    public_area_safe=None,            # PR #29 B1: pre-buffered for perf
    flag_collector=None,
) -> int:
    """Add GC neuf C0 edges into graph, snapping endpoints to nearest nodes.

    PR #21: endpoints are snapped to existing graph nodes first so that
    Dijkstra can traverse through GC neuf segments. If no node within
    snap_tol, the raw _point_key is added as a new isolated node.

    PR #28 BLOQUANT 2: edges crossing outside *public_area* are rejected
    with flag GC_NEUF_PRIVATE_CROSSING. Returns count of rejected edges.

    PR #29 B1: when ``public_area_safe`` is provided, it MUST equal
    ``public_area.buffer(0.01)`` and is used directly to avoid recomputing
    the buffer per edge (was N×buffer in inner loop).
    """
    if gc_neuf is None or gc_neuf.empty:
        return 0

    # Pre-compute buffered public area if not provided (B1).
    if public_area is not _SENTINEL and public_area_safe is None:
        public_area_safe = _public_area_safe(public_area)

    n_rejected = 0

    # Quick node lookup
    node_coords = np.array([(x, y) for x, y in G.nodes()])
    has_nodes = len(node_coords) > 0

    def _snap_endpoint(coord) -> tuple[float, float]:
        pk = _point_key(coord)
        if pk in G:
            return pk
        if has_nodes:
            dists = np.linalg.norm(node_coords - np.array([[coord[0], coord[1]]]), axis=1)
            i_min = int(dists.argmin())
            if dists[i_min] <= snap_tol:
                return _point_key(node_coords[i_min])
        return pk

    for _, row in gc_neuf.iterrows():
        line = row.geometry
        if line is None or line.is_empty:
            continue
        coords = [_coord2d(c) for c in line.coords]
        if len(coords) < 2:
            continue

        pk_a = _snap_endpoint(coords[0])
        pk_b = _snap_endpoint(coords[-1])

        # ── PR #28 BLOQUANT 2 / PR #29 B1: spatial check on GC neuf geometry ──
        gc_geom = LineString(coords)
        if public_area is not _SENTINEL:
            if public_area_safe is None:
                if flag_collector is not None:
                    flag_collector.add(
                        "GC_NEUF_ROUTING_IMPOSSIBLE",
                        target_url=row.get("pa_id", "?"),
                        message="GC neuf rejeté — domaine public inconnu",
                    )
                n_rejected += 1
                continue
            if not public_area_safe.covers(gc_geom):
                if flag_collector is not None:
                    flag_collector.add(
                        "GC_NEUF_PRIVATE_CROSSING",
                        target_url=row.get("pa_id", "?"),
                        message=f"GC neuf rejeté — traverse domaine privé, length={gc_geom.length:.0f}m",
                    )
                n_rejected += 1
                continue

        # Ensure both endpoints exist as nodes in G
        if pk_a not in G:
            G.add_node(pk_a)
        if pk_b not in G:
            G.add_node(pk_b)

        attrs = {
            "length": Point(pk_a[0], pk_a[1]).distance(Point(pk_b[0], pk_b[1])),
            "type": "gc_neuf",
            "statut": None,
            "mode_pose": "C0",
            "src": "gc_neuf",
            "infra_type": "gc_neuf",
            "geometry": gc_geom
            if not gc_geom.is_empty
            else LineString([(pk_a[0], pk_a[1]), (pk_b[0], pk_b[1])]),
            # PR #36: gc_neuf injected from ``pb_fictif`` represents REAL
            # planned GC neuf segments that already follow the public
            # domain (validated above by ``public_area_safe.covers``).
            # They are legitimate livrable infra, not virtual routing
            # helpers — keeping them virtual (PR #33's blanket rule)
            # caused PR #35 to reject every PA→PB path that needed any
            # gc_neuf, leaving SROs with no delivered infra on the field
            # test. They are now delivered like any other public C0 row.
            "virtual": False,
            "deliverable": True,
        }
        for col in ("sro_code", "pa_id", "pb_id"):
            if col in gc_neuf.columns:
                attrs[col] = row.get(col)
        G.add_edge(pk_a, pk_b, **attrs)

    return n_rejected


# ---------------------------------------------------------------------------



def _bridge_components_with_gc_neuf(
    G: nx.Graph,
    pa_node: tuple[float, float],
    pb_node: tuple[float, float],
    flag_collector=None,
    max_bridge_length_m: float = 50.0,
    public_area: object = _SENTINEL,  # PR #27 Part A: spatial validation
    public_area_safe=None,             # PR #29 B1: pre-buffered for perf
    diagnostics: dict | None = None,
) -> bool:
    """If pa_node and pb_node belong to different connected components,
    attempt to bridge them WITHOUT creating visible straight-line segments.

    PR #33 behaviour:
    - distance <= MAX_STRAIGHT_CONNECTOR_M: create a VIRTUAL edge 
      (routable but NOT deliverable) for micro-snap connectivity.
    - distance > MAX_STRAIGHT_CONNECTOR_M: flag disconnection, do NOT
      create any edge. The PB will be reported as disconnected.
    - This replaces the previous behaviour of creating visible C0
      diagonal bridges that showed up as straight red lines in QGIS.
    """
    try:
        cc_pa = nx.node_connected_component(G, pa_node)
        if pb_node in cc_pa:
            return False
    except (nx.NetworkXError, KeyError):
        return False

    direct_length = Point(pa_node[0], pa_node[1]).distance(
        Point(pb_node[0], pb_node[1])
    )

    # PR #33: only allow micro-snap bridges (<= 3m) as virtual edges
    if direct_length > MAX_STRAIGHT_CONNECTOR_M:
        if diagnostics is not None:
            diagnostics["direct_chord_blocked_count"] = (
                int(diagnostics.get("direct_chord_blocked_count", 0)) + 1
            )
        if flag_collector is not None:
            flag_collector.add(
                "COMPONENT_BRIDGE_REQUIRED_MANUAL_REVIEW",
                target_url=f"PA=({pa_node[0]:.0f},{pa_node[1]:.0f}) PB=({pb_node[0]:.0f},{pb_node[1]:.0f})",
                message=f"Bridge requis entre composants — distance {direct_length:.0f}m > seuil micro {MAX_STRAIGHT_CONNECTOR_M}m, review manuelle",
            )
        return False

    bridge_geom = LineString([
        (pa_node[0], pa_node[1]),
        (pb_node[0], pb_node[1]),
    ])

    # PR #36 — micro bridge can be DELIVERED when it stays inside the
    # public domain (Pierre's brief: "GC neuf seulement en dernier recours
    # … doit suivre la voirie publique"). A ≤3 m bridge across two welded
    # components in public area is the same as a small terminal connector:
    # short, justified, follows public domain — it counts as legitimate
    # GC neuf, not a virtual routing trick. If the bridge would cross
    # private land or no public_area was provided, fall back to the PR #33
    # virtual-only behaviour so Dijkstra still finds connectivity but the
    # edge will be filtered out at delivery time.
    bridge_is_public = False
    if ALLOW_IGN_ROAD_C0_WITHOUT_PARCEL_GATE:
        # PR #40 amend: disabling the parcel gate must not make synthetic
        # component-to-component chords deliverable. Only genuine
        # micro-bridges stay visible; longer gaps need existing/IGN geometry.
        bridge_is_public = direct_length <= MAX_STRAIGHT_CONNECTOR_M
    elif public_area is not _SENTINEL:
        if public_area_safe is None:
            public_area_safe = _public_area_safe(public_area)
        if public_area_safe is not None and public_area_safe.covers(bridge_geom):
            bridge_is_public = True

    bridge_attrs = {
        "length": direct_length,
        "type": "gc_neuf",
        "statut": None,
        "mode_pose": "C0",
        "src": "gc_neuf",
        "infra_type": "gc_neuf",
        "geometry": bridge_geom,
    }
    if bridge_is_public:
        bridge_attrs["virtual"] = False
        bridge_attrs["deliverable"] = True
    else:
        bridge_attrs["virtual"] = True
        bridge_attrs["deliverable"] = False
        bridge_attrs["virtual_reason"] = "micro_bridge"
    bridge_attrs["_routing_weight"] = _routing_weight_for(bridge_attrs)
    bridge_attrs["_pass1_weight"] = bridge_attrs["_routing_weight"] + 1e9
    bridge_attrs["_is_existing"] = False
    # PR #36 — keep the _can_deliver hint in sync so the path-walk in
    # ``route_pa_to_pb`` does not need to re-derive it for late-added
    # bridges (they are inserted AFTER the prepare-weights loop).
    bridge_attrs["_can_deliver"] = bridge_is_public
    G.add_edge(pa_node, pb_node, **bridge_attrs)
    if flag_collector is not None:
        flag_collector.add(
            "MICRO_BRIDGE_CREATED" if bridge_is_public else "MICRO_BRIDGE_CREATED_VIRTUAL",
            target_url=f"PA=({pa_node[0]:.0f},{pa_node[1]:.0f}) PB=({pb_node[0]:.0f},{pb_node[1]:.0f})",
            message=(
                f"Micro bridge {'C0 livré' if bridge_is_public else 'virtuel'} "
                f"créé, length={direct_length:.1f}m"
            ),
        )
    return True


# ---------------------------------------------------------------------------


def _rebuild_strtree_indices(
    G: nx.Graph,
) -> tuple:
    """Rebuild STRtree and edge_list after graph mutations (PR #27 Part C).

    Returns (node_tree, edge_tree, nodes_list, edge_list) — non-None
    when G has nodes/edges, otherwise (None, None, [], []).
    """
    nodes_list = list(G.nodes())
    node_geoms = [Point(n[0], n[1]) for n in nodes_list]
    node_tree = STRtree(node_geoms) if node_geoms else None

    edge_list: list[tuple] = []
    edge_geoms: list[LineString] = []
    for u, v, data in G.edges(data=True):
        line = LineString([(u[0], u[1]), (v[0], v[1])])
        edge_list.append((u, v, data))
        edge_geoms.append(line)
    edge_tree = STRtree(edge_geoms) if edge_geoms else None

    return node_tree, edge_tree, nodes_list, edge_list


def route_pa_to_pb(
    pa_sro: gpd.GeoDataFrame,
    pb_sro: gpd.GeoDataFrame,
    infra_filtered: gpd.GeoDataFrame,
    ign_routes: gpd.GeoDataFrame,
    flag_collector=None,
    gc_neuf: gpd.GeoDataFrame | None = None,
    public_area=None,           # PR #27 Part A — routing public domain
    delivery_public_area=None,  # PR #30 — strict delivery public domain
) -> gpd.GeoDataFrame:
    """Route all (PA, PB) pairs via Dijkstra and return traversed edges.

    *gc_neuf* (optional) — new C0 edges generated by pb_fictif, injected
    into the routing graph so they can be traversed by PA→PB paths.

    *public_area* (optional, PR #27) — *routing* public area: a permissive
    Shapely geometry (typically parcelles publiques ∪ IGN buffer 5 m)
    used to gate bridge creation and ign-buffer connectors during graph
    construction. This area is used for ROUTING decisions, not delivery.

    *delivery_public_area* (optional, PR #30) — *delivery* public area: a
    STRICT Shapely geometry (typically parcelles publiques only, no IGN
    buffer) used to gate every C0/gc_neuf row that ends up in the
    livrable. When omitted, falls back to ``public_area`` for backward
    compatibility — but main.py is expected to pass an explicitly
    stricter area so long IGN diagonals do not leak as C0.

    Returns a GeoDataFrame of LineStrings with columns:
    ``sro``, ``pa_id``, ``pb_id``, ``statut``, ``mode_pose``, ``infra_type``,
    ``src``, ``length_m``.

    Edges are deduplicated via _edge_key so a trunk shared by multiple PBs
    of the same PA appears only once in the output (PR #23 Feature D).
    """
    if pa_sro is None or pa_sro.empty or pb_sro is None or pb_sro.empty:
        return gpd.GeoDataFrame(geometry=[], crs=config.PROJECT_CRS)

    # PR #29 B4: per-step timing for [ROUTING PERF] logs
    sro_code_log = pa_sro.iloc[0].get("sro", "?") if len(pa_sro) > 0 else "?"
    t_total_start = time.perf_counter()
    perf: dict[str, float] = {}

    # PR #29 B1: pre-compute the buffered public area ONCE per SRO.
    public_area_safe = _public_area_safe(public_area)

    # PR #30 — strict delivery area, falls back to routing area if absent.
    # delivery_public_area_safe is what gates IGN→C0 conversion AND the
    # final C0/gc_neuf filter (replaces the previous lenient routing-area
    # final filter from PR28). Pre-computed once to avoid per-row buffers.
    if delivery_public_area is None:
        delivery_public_area_safe = public_area_safe  # backward compat
    else:
        delivery_public_area_safe = _public_area_safe(delivery_public_area)
    parcel_gate_reference_area = delivery_public_area_safe
    if ALLOW_IGN_ROAD_C0_WITHOUT_PARCEL_GATE:
        log.info(
            "[ROUTING MODE] parcel_gate=disabled network=existing+ign_roads "
            "existing_first=true direct_chords=false"
        )
        delivery_public_area_safe = None

    # PR #37 — heal existing infra topology BEFORE routing. The healed
    # GeoDataFrame is what enters the routing graph; the original
    # ``infra_filtered`` is no longer used past this point. The heal is
    # idempotent so calling it again later (e.g. from livrable_topology)
    # is harmless.
    t0 = time.perf_counter()
    infra_filtered = _heal_existing_infra_topology(infra_filtered)
    perf["heal_existing_infra"] = time.perf_counter() - t0

    # Build combined graph
    t0 = time.perf_counter()
    G = _build_graph(infra_filtered, ign_routes, snap_tol=SNAP_TOLERANCE_M)
    perf["build_graph"] = time.perf_counter() - t0

    # Inject GC neuf C0 edges (snaps endpoints to existing nodes first)
    t0 = time.perf_counter()
    n_rejected = 0
    if gc_neuf is not None and not gc_neuf.empty:
        n_rejected = _add_gc_neuf_to_graph(
            G, gc_neuf, snap_tol=SNAP_TOLERANCE_M,
            public_area=public_area,
            public_area_safe=public_area_safe,  # PR #29 B1
            flag_collector=flag_collector,
        )
    if n_rejected:
        log.info("[GC QA] %d GC neuf edges rejected (private crossing)", n_rejected)
    perf["add_gc_neuf"] = time.perf_counter() - t0

    if G.number_of_nodes() == 0:
        pb_total = int(len(pb_sro))
        log.info(
            "[PB ROUTING QA] sro=%s pb_total=%d pb_assigned=0 "
            "pb_unassigned=%d pb_attempted=0 pb_committed=0 "
            "pb_impossible=%d pb_dropped=0 reasons=no_path_in_existing_or_ign_graph:%d",
            sro_code_log,
            pb_total,
            pb_total,
            pb_total,
            pb_total,
        )
        log.info(
            "[ANCHOR QA] sro=%s terminal_visible_connectors=0 "
            "terminal_logical_only=0 terminal_anchor_missing=%d",
            sro_code_log,
            pb_total,
        )
        log.info(
            "[ROAD C0 QA] sro=%s road_c0_delivered_count=0 "
            "road_c0_delivered_m=0 parcel_gate_disabled_count=0 "
            "direct_chord_blocked_count=0 c0_without_route_geometry_count=0 "
            "c0_suspicious_chord_count=0",
            sro_code_log,
        )
        log.info(
            "[FINAL TOPO QA] sro=%s connected=0 disconnected=%d "
            "pa_pb_connected_ratio=0.00 straight_connectors=0 "
            "straight_connector_length_m=0 virtual_delivered=0 "
            "ign_cap_hit=0 c0_without_source_geometry=0 "
            "long_direct_c0_count=0 c0_without_ign_source=0 "
            "path_lost_between_routing_and_final_graph=0 "
            "path_broken_after_postprocess=0 "
            "committed_path_reachable_final_graph=0 "
            "committed_path_unreachable_final_graph=0 "
            "path_metadata_present_but_graph_disconnected=0",
            sro_code_log,
            pb_total,
        )
        return gpd.GeoDataFrame(geometry=[], crs=config.PROJECT_CRS)

    # PR #27 Part B: snap close A/Z endpoints before routing
    t0 = time.perf_counter()
    _topo_stats = _snap_endpoints_topology(G, snap_radius_m=SNAP_ENDPOINT_RADIUS_M)
    perf["snap_endpoints_topology"] = time.perf_counter() - t0

    # PR #28 BLOQUANT 4: snap degree-1 endpoints onto nearby lines
    t0 = time.perf_counter()
    _line_snap_stats = _snap_endpoints_to_lines(
        G, snap_radius_m=SNAP_ENDPOINT_RADIUS_M,
        public_area=public_area,
        public_area_safe=public_area_safe,  # PR #29 B1
        flag_collector=flag_collector,
    )
    perf["snap_endpoints_to_lines"] = time.perf_counter() - t0

    # PR #29 A1 + PR #36 — hierarchical routing weights AND per-edge
    # deliverability flag baked in. Dijkstra runs on the FULL graph but
    # non-deliverable edges carry an additive penalty large enough that
    # the algorithm will only ever route through one when no deliverable
    # alternative exists. The path walk later checks ``_can_deliver`` to
    # decide whether to commit or to flag the PB as disconnected — but
    # because the weight is already steering Dijkstra to deliverable
    # alternatives, those flags only fire when the geometry truly does
    # not allow a clean PA→PB livraison.
    #
    # Deliverability rules (PR #36):
    #   - virtual=True  or deliverable=False → non-deliverable
    #   - IGN edge longer than IGN_DELIVERY_MAX_LENGTH_M → non-deliverable
    #   - IGN edge whose geometry leaves ``delivery_public_area_safe`` →
    #     non-deliverable (private IGN must not leak into livrable_infra)
    # The cumulative IGN cap (MAX_IGN_DELIVERED_PER_SRO_M) is intentionally
    # NOT a deliverability blocker anymore. PR #35 made it a hard blocker
    # which turned full SROs into infra=0 livrables; PR #36 demotes it to
    # a telemetry warning so paths stay continuous, while Pierre still
    # sees ``ign_cap_hit`` rise in [FINAL TOPO QA] when an SRO over-uses
    # IGN-derived C0.
    t0 = time.perf_counter()
    HIGH_WEIGHT_PENALTY = 1e9
    for u, v, data in G.edges(data=True):
        base_w = _routing_weight_for(data)
        is_deliverable = True
        if data.get("virtual") or not data.get("deliverable", True):
            is_deliverable = False
        raw_type = data.get("type")
        raw_length = float(data.get("length", 0.0))
        if raw_type == "ign_route":
            if ALLOW_IGN_ROAD_C0_WITHOUT_PARCEL_GATE:
                is_deliverable = True
            elif raw_length > IGN_DELIVERY_MAX_LENGTH_M:
                is_deliverable = False
            elif delivery_public_area_safe is not None:
                stored = data.get("geometry")
                if (
                    stored is not None
                    and isinstance(stored, LineString)
                    and not stored.is_empty
                    and not delivery_public_area_safe.covers(stored)
                ):
                    is_deliverable = False
        data["_can_deliver"] = is_deliverable
        # PR #37 — existing-only flag for pass-1 Dijkstra. Existing =
        # ``type == "infra"`` (the SIG layer). gc_neuf injected by
        # pb_fictif is real planned GC but still counts as fallback for
        # pass 1; if pass 1 reaches the PB on existing only, the planned
        # gc_neuf isn't even visited. IGN and virtual stay out of pass 1
        # by design.
        data["_is_existing"] = (raw_type == "infra") and is_deliverable
        data["_routing_weight"] = base_w if is_deliverable else base_w + HIGH_WEIGHT_PENALTY
        # Pass-1 weight: only existing infra eligible.
        data["_pass1_weight"] = (
            base_w if data["_is_existing"]
            else base_w + HIGH_WEIGHT_PENALTY
        )
    perf["prepare_weights"] = time.perf_counter() - t0

    # Spec C (PR #22): diagnostic on connected components
    n_cc = nx.number_connected_components(G)
    if n_cc > 0:
        largest_cc = max(nx.connected_components(G), key=len)
        log.info(
            "[ROUTING] %d composantes connexes (plus grande = %d noeuds / %d total)",
            n_cc, len(largest_cc), G.number_of_nodes(),
        )

    edges_out: dict[str, dict] = {}  # PR #23 Feature D: deduplicate via edge key

    # PR #29 amend — raw-source telemetry collected DURING path collection,
    # BEFORE the output conversion (raw_type=="ign_route" → src="gc_neuf").
    # Reading these counters from the final GeoDataFrame would always
    # report 0 IGN since the conversion has happened by then. We therefore
    # accumulate per-edge length grouped by raw_type/raw_src here and pass
    # the result to ``_log_routing_qa`` at the end.
    raw_src_lengths: dict[str, float] = {}
    raw_src_counts: dict[str, int] = {}
    raw_type_lengths: dict[str, float] = {}
    converted_ign_to_gc_length_m = 0.0

    # PR #30 — IGN delivery telemetry (computed during path collection).
    ign_route_delivered_as_gc_m = 0.0  # IGN edges short+public, KEPT as C0
    ign_route_blocked_m = 0.0          # IGN edges too long or private, DROPPED
    ign_route_blocked_count = 0
    ign_cap_hit_count = 0
    # Per-SRO de-duplication: a single IGN_ROUTE_BLOCKED flag per SRO is
    # enough; we only track that we've already added it to avoid spam.
    _ign_blocked_flag_added = False

    # PR #33 — virtual edge telemetry
    virtual_edges_blocked_count = 0
    direct_chord_blocked_count = 0
    bridge_diagnostics = {"direct_chord_blocked_count": 0}
    straight_connector_count = 0
    straight_connector_length_m = 0.0
    anchor_stats = {
        "terminal_visible_connectors": 0,
        "terminal_logical_only": 0,
        "terminal_anchor_missing": 0,
    }
    pb_routing_stats = {
        "pb_total": int(len(pb_sro)),
        "pb_assigned": 0,
        "pb_unassigned": 0,
    }
    pb_attempted_ids: set[str] = set()
    pb_committed_ids: set[str] = set()
    pb_impossible_reasons: dict[str, str] = {}
    # PR38 diagnostics
    diag = {
        "pass1_existing_reached": 0,
        "pass1_existing_committed": 0,
        "pass1_existing_rejected": 0,
        "pass2_gc_reached": 0,
        "pass2_gc_committed": 0,
        "pass2_gc_rejected": 0,
        "pb_unreachable_after_pass1": 0,
        "pb_unreachable_after_pass2": 0,
        "path_rejected_missing_anchor": 0,
        "path_lost_between_routing_and_final_graph": 0,
        "path_broken_after_postprocess": 0,
        "committed_path_reachable_final_graph": 0,
        "committed_path_unreachable_final_graph": 0,
        "path_metadata_present_but_graph_disconnected": 0,
        "path_impossible_private_c0": 0,
        "c0_private_crossing_kept": 0,
        "pb_impossible_private_c0": 0,
    }
    committed_paths: dict[str, dict] = {}

    def _pb_id_for(pb) -> str:
        return str(pb.get("pb_id", f"pb#{pb.name}"))

    def _mark_pb_attempted(pb_id: str) -> None:
        pb_attempted_ids.add(str(pb_id))

    def _mark_pb_committed(pb_id: str) -> None:
        pb_committed_ids.add(str(pb_id))
        pb_impossible_reasons.pop(str(pb_id), None)

    def _mark_pb_impossible(pb_id: str, reason: str) -> None:
        pb_id = str(pb_id)
        pb_committed_ids.discard(pb_id)
        pb_impossible_reasons[pb_id] = reason

    # Build STRtree indices for fast PA/PB snapping (PR #21)
    # PR #27 Part C: use rebuild helper (avoids stale indices)
    node_tree, edge_tree, nodes_list, edge_list = _rebuild_strtree_indices(G)
    node_coords_to_geom = {n: Point(n[0], n[1]) for n in nodes_list}

    def _snap(_pt: Point) -> Optional[tuple[float, float]]:
        # 0) Exact key
        pk = _point_key(_pt)
        if pk in G:
            return pk

        # 1) Nearest node
        nonlocal node_tree, edge_tree, nodes_list, edge_list
        if node_tree is not None:
            buf = _pt.buffer(SNAP_TOLERANCE_M)
            candidates = node_tree.query(buf)
            best_n, best_d = None, SNAP_TOLERANCE_M
            for idx in candidates:
                n = nodes_list[idx]
                d = _pt.distance(Point(n[0], n[1]))
                if d < best_d:
                    best_d, best_n = d, n
            if best_n is not None:
                return best_n

        # 2) Project onto edge
        if edge_tree is not None and edge_list:
            buf = _pt.buffer(SNAP_PROJECTION_RADIUS_M)
            candidates = edge_tree.query(buf)
            best_e, best_d, best_proj = None, SNAP_PROJECTION_RADIUS_M, None
            for idx in candidates:
                u, v, edata = edge_list[idx]
                line = LineString([(u[0], u[1]), (v[0], v[1])])
                proj = line.interpolate(line.project(_pt))
                d = _pt.distance(proj)
                if d < best_d:
                    best_d, best_e, best_proj = d, (u, v, edata), proj
            if best_proj is not None:
                u, v, edata = best_e
                new_key = _point_key(best_proj)
                # Check edge still exists (may have been split by earlier snap)
                if new_key not in G and G.has_edge(u, v):
                    G.remove_edge(u, v)
                    d1 = Point(u[0], u[1]).distance(best_proj)
                    d2 = Point(v[0], v[1]).distance(best_proj)
                    extra = {k: edata[k] for k in edata if k not in ("length", "geometry")}
                    # PR #26: build proper sub-geometries from the original edge
                    orig_geom = edata.get("geometry")
                    if orig_geom is not None and isinstance(orig_geom, LineString):
                        proj_dist = orig_geom.project(best_proj)
                        seg_a = shapely.ops.substring(orig_geom, 0, proj_dist)
                        seg_b = shapely.ops.substring(orig_geom, proj_dist, orig_geom.length)
                    else:
                        seg_a = LineString([(u[0], u[1]), (best_proj.x, best_proj.y)])
                        seg_b = LineString([(best_proj.x, best_proj.y), (v[0], v[1])])
                    G.add_edge(u, new_key, length=d1, geometry=seg_a, **extra)
                    G.add_edge(new_key, v, length=d2, geometry=seg_b, **extra)
                    # PR #27 Part C: rebuild indices after edge split
                    node_tree, edge_tree, nodes_list, edge_list = _rebuild_strtree_indices(G)
                if new_key in G:
                    return new_key

        return None

    def _add_terminal_anchor(term_geom: Point, term_kind: str, term_id: str):
        snapped = _snap(term_geom)
        if snapped is None:
            anchor_stats["terminal_anchor_missing"] += 1
            return None
        tnode = _point_key(term_geom)
        if tnode not in G:
            G.add_node(tnode)
        if tnode != snapped and not G.has_edge(tnode, snapped):
            g = LineString([(tnode[0], tnode[1]), (snapped[0], snapped[1])])
            length_m = float(g.length)
            visible = length_m <= MAX_STRAIGHT_CONNECTOR_M
            if visible and delivery_public_area_safe is not None:
                visible = delivery_public_area_safe.covers(g)
            # PR #41 — relax the logical-anchor cap for orphan PAs
            # (those that have no infra within MAX_LOGICAL_TERMINAL_ANCHOR_M
            # but still sit within PR41_MAX_LOGICAL_ANCHOR_M_FOR_ORPHAN
            # of an exploitable edge). The relaxed anchor is virtual /
            # non-delivered — it only allows Dijkstra to find a starting
            # node, so the SRO does not lose all its PBs to a single
            # missing PA snap. Visible C0 connectors remain capped at
            # MAX_STRAIGHT_CONNECTOR_M.
            if length_m > MAX_LOGICAL_TERMINAL_ANCHOR_M:
                if visible or length_m > PR41_MAX_LOGICAL_ANCHOR_M_FOR_ORPHAN:
                    anchor_stats["terminal_anchor_missing"] += 1
                    return None
                # Long logical anchor accepted as a virtual non-delivered
                # edge. Keep ``visible=False`` so it is never emitted as
                # a real C0 row in livrable_infra.
            if visible:
                anchor_stats["terminal_visible_connectors"] += 1
            else:
                anchor_stats["terminal_logical_only"] += 1
            anchor_attrs = {
                "length": length_m,
                "geometry": g,
                "type": "gc_neuf" if visible else "terminal_anchor",
                "infra_type": "terminal_connector" if visible else "terminal_logical_anchor",
                "mode_pose": "C0" if visible else "",
                "src": "gc_neuf" if visible else "logical_anchor",
                "deliverable": visible,
                "virtual": not visible,
                "_can_deliver": visible,
                "_is_existing": False,
                "_terminal_anchor": True,
                "_logical_anchor": not visible,
                "_terminal_type": term_kind,
                "_terminal_id": term_id,
                "_c0_source": "terminal" if visible else None,
            }
            anchor_attrs["_routing_weight"] = _routing_weight_for(anchor_attrs)
            # Terminal anchors are the only allowed non-existing edges in
            # pass 1; they attach the PA/PB point to the existing graph and
            # are ignored by _path_is_existing_only below.
            anchor_attrs["_pass1_weight"] = length_m
            G.add_edge(
                tnode, snapped,
                **anchor_attrs,
            )
        return {
            "terminal_node": tnode,
            "graph_node": snapped,
            "final_anchor": tnode if G.has_edge(tnode, snapped) and not G[tnode][snapped].get("_logical_anchor") else snapped,
        }

    # PR #39 — assign every PB to a routable PA before the PA loop. Exact
    # id matching still wins, but missing/mismatched pa_id now falls back to
    # nearest PA inside this SRO batch instead of disappearing silently.
    pa_records = []
    pa_id_to_index: dict[str, int] = {}
    for pa_idx, pa in pa_sro.iterrows():
        pa_id_raw = pa.get("id_metier", f"pa#{pa.name}")
        pa_id = str(pa_id_raw)
        pa_records.append((pa_idx, pa_id, pa))
        pa_id_to_index[pa_id] = pa_idx

    pb_sro_routing = pb_sro.copy()
    if "pa_id" not in pb_sro_routing.columns:
        pb_sro_routing["pa_id"] = None
    assigned_pb_by_pa: dict[int, list[int]] = {pa_idx: [] for pa_idx, _, _ in pa_records}
    pb_unassigned_reasons: dict[str, int] = {}

    def _assign_pb_to_pa(pb) -> tuple[int | None, str]:
        raw_pa_id = pb.get("pa_id")
        if raw_pa_id is not None and str(raw_pa_id) in pa_id_to_index:
            return pa_id_to_index[str(raw_pa_id)], "pa_id"
        geom = pb.geometry
        if geom is None or getattr(geom, "is_empty", True):
            return None, "invalid_geometry"
        candidates = pa_records
        pb_sro_value = pb.get("sro")
        if pb_sro_value is not None:
            scoped = [
                rec for rec in pa_records
                if str(rec[2].get("sro", "")) == str(pb_sro_value)
            ]
            if scoped:
                candidates = scoped
        best_idx = None
        best_dist = float("inf")
        for pa_idx, _pa_id, pa in candidates:
            pgeom = pa.geometry
            if pgeom is None or getattr(pgeom, "is_empty", True):
                continue
            d = geom.distance(pgeom)
            if d < best_dist:
                best_dist = d
                best_idx = pa_idx
        if best_idx is None:
            return None, "no_valid_pa"
        return best_idx, "nearest_pa"

    for pb_idx, pb in pb_sro_routing.iterrows():
        assigned_idx, reason = _assign_pb_to_pa(pb)
        if assigned_idx is None:
            pb_routing_stats["pb_unassigned"] += 1
            pb_unassigned_reasons[reason] = pb_unassigned_reasons.get(reason, 0) + 1
            _mark_pb_impossible(_pb_id_for(pb), f"unassigned_{reason}")
            continue
        assigned_pb_by_pa.setdefault(assigned_idx, []).append(pb_idx)
        assigned_pa_id = str(pa_sro.loc[assigned_idx].get("id_metier", f"pa#{assigned_idx}"))
        pb_sro_routing.at[pb_idx, "pa_id"] = assigned_pa_id
        pb_routing_stats["pb_assigned"] += 1

    # ── For each PA, route all PA → PB (PR #23 Feature D: single-source Dijkstra per PA) ──
    t0_dijkstra = time.perf_counter()
    pa_count = 0
    pb_count = 0
    for pa_idx, pa in pa_sro.iterrows():
        pa_count += 1
        pa_id = pa.get("id_metier", f"pa#{pa.name}")
        sro = pa.get("sro", "?")
        pa_geom = pa.geometry

        pa_anchor = _add_terminal_anchor(pa_geom, "PA", str(pa_id))
        if pa_anchor is None:
            pb4pa = pb_sro_routing.loc[assigned_pb_by_pa.get(pa_idx, [])]
            pb_count += int(len(pb4pa))
            for _, pb in pb4pa.iterrows():
                pb_id = _pb_id_for(pb)
                _mark_pb_attempted(pb_id)
                _mark_pb_impossible(pb_id, "pa_anchor_missing")
            if flag_collector is not None:
                flag_collector.add(
                    "PA_PB_DECONNECTES",
                    target_url=pa_id,
                    message="PA non connectable au graphe",
                )
            continue
        pa_node = pa_anchor["terminal_node"]
        pa_final_anchor = pa_anchor["final_anchor"]

        pb4pa = pb_sro_routing.loc[assigned_pb_by_pa.get(pa_idx, [])]
        if pb4pa.empty:
            continue

        # ── Snap all PBs first (may mutate G via edge projection) ─────
        pb_snapped: list[tuple] = []  # (pb_row, pb_node, pb_final_anchor)
        for _, pb in pb4pa.iterrows():
            pb_count += 1
            pb_id = _pb_id_for(pb)
            _mark_pb_attempted(pb_id)
            pb_anchor = _add_terminal_anchor(pb.geometry, "PB", pb_id)
            if pb_anchor is None:
                _mark_pb_impossible(pb_id, "missing_anchor")
                diag["path_rejected_missing_anchor"] += 1
                # PB unreachable — flag immediately
                if flag_collector is not None:
                    flag_collector.add(
                        "PA_PB_DECONNECTES",
                        target_url=pb_id,
                        message="PB non connectable au graphe",
                    )
                continue
            pb_snapped.append((pb, pb_anchor["terminal_node"], pb_anchor["final_anchor"]))

        if not pb_snapped:
            continue

        # ── PR #37 — Two-pass Dijkstra per PA. Pass 1 explores ONLY
        # existing infra (``_is_existing == True``) so a path made of
        # plain SIG segments is found whenever one exists, completely
        # avoiding IGN / gc_neuf / virtual fallbacks. Pass 2 falls back
        # to the full deliverable graph (existing + IGN-as-C0 + planned
        # gc_neuf) for PBs that pass 1 could not reach.
        #
        # The pass-1 tree is computed eagerly; the pass-2 tree is built
        # lazily and only when at least one PB needs the fallback.
        def _dijkstra_tree_pass1():
            try:
                _, paths = nx.single_source_dijkstra(
                    G, source=pa_node, weight="_pass1_weight",
                )
                return paths
            except (nx.NetworkXError, KeyError):
                return {}

        def _dijkstra_tree_full():
            try:
                _, paths = nx.single_source_dijkstra(
                    G, source=pa_node, weight="_routing_weight",
                )
                return paths
            except (nx.NetworkXError, KeyError):
                return {}

        _paths_p1 = _dijkstra_tree_pass1()
        _paths_full = None  # lazy

        def _path_is_existing_only(p):
            """All edges along ``p`` are existing-infra and deliverable."""
            for i in range(len(p) - 1):
                data_e = G.get_edge_data(p[i], p[i + 1])
                if data_e is None:
                    return False
                if data_e.get("_terminal_anchor"):
                    continue
                if not data_e.get("_is_existing", False):
                    return False
            return True

        for pb, pb_node, pb_final_anchor in pb_snapped:
            pb_id = _pb_id_for(pb)
            path_id = f"{pa_id}->{pb_id}"

            path = None

            # ── Pass 1: existing-only ─────────────────────────────────
            if pb_node in _paths_p1:
                p1 = _paths_p1[pb_node]
                diag["pass1_existing_reached"] += 1
                if _path_is_existing_only(p1):
                    path = p1
                    diag["pass1_existing_committed"] += 1
                else:
                    diag["pass1_existing_rejected"] += 1
            else:
                diag["pb_unreachable_after_pass1"] += 1

            # ── Pass 2: fallback to IGN/C0 deliverable graph ──────────
            if path is None:
                if _paths_full is None:
                    _paths_full = _dijkstra_tree_full()
                if pb_node in _paths_full:
                    path = _paths_full[pb_node]
                    diag["pass2_gc_reached"] += 1
                    diag["pass2_gc_committed"] += 1
                else:
                    # PB still unreachable: try a last-resort micro-bridge
                    bridged = _bridge_components_with_gc_neuf(
                        G, pa_node, pb_node,
                        flag_collector=flag_collector,
                        public_area=public_area,
                        public_area_safe=public_area_safe,
                        diagnostics=bridge_diagnostics,
                    )
                    if bridged:
                        _paths_full = _dijkstra_tree_full()
                        if pb_node in _paths_full:
                            path = _paths_full[pb_node]
                            diag["pass2_gc_reached"] += 1
                            diag["pass2_gc_committed"] += 1

            if path is None:
                _mark_pb_impossible(str(pb_id), "unreachable_after_pass2")
                diag["pb_unreachable_after_pass2"] += 1
                if flag_collector is not None:
                    flag_collector.add(
                        "PA_PB_DECONNECTES",
                        target_url=pa_id,
                        message=f"Pas de chemin vers {pb_id} (pass 1 + fallback)",
                    )
                continue

            # ── PR #36 — DELIVERABILITY-AWARE Dijkstra path walk.
            #
            # PR #35 ran Dijkstra on the permissive graph and then rejected
            # any PA→PB whose chosen path contained a single non-deliverable
            # edge. On the field test this killed full SROs: gc_neuf
            # injected by ``pb_fictif`` was flagged virtual, micro-bridges
            # were flagged virtual, the cumulative IGN cap blocked legit
            # long-IGN paths — and Pierre ended up with infra=0 livrables.
            #
            # PR #36 instead steers Dijkstra: non-deliverable edges already
            # carry a huge ``_routing_weight`` penalty (see prepare_weights
            # above), so the algorithm naturally picks a fully-deliverable
            # alternative whenever one exists. The only case where the
            # returned path still contains a non-deliverable edge is when
            # there is *no* deliverable alternative at all — and that is
            # the situation we honestly flag here.
            #
            # The cumulative IGN cap (MAX_IGN_DELIVERED_PER_SRO_M) is no
            # longer a blocker: it's a soft warning surfaced in
            # [FINAL TOPO QA] via ``ign_cap_hit`` so Pierre still sees when
            # an SRO uses more IGN-derived C0 than the budget suggests.
            proposed_edges: list[tuple] = []
            path_ign_delivered_pending_m = 0.0
            path_deliverable = True
            path_rejection_reason: str | None = None

            for i in range(len(path) - 1):
                u, v = path[i], path[i + 1]
                edge_data = G.get_edge_data(u, v)
                if edge_data is None:
                    path_deliverable = False
                    path_rejection_reason = "missing_edge_data"
                    break

                if edge_data.get("_logical_anchor"):
                    continue

                if not edge_data.get("_can_deliver", True):
                    # Dijkstra had to traverse a non-deliverable edge — no
                    # deliverable alternative exists. Drop the PB cleanly.
                    virtual_edges_blocked_count += 1
                    if float(edge_data.get("length", 0.0) or 0.0) > MAX_STRAIGHT_CONNECTOR_M:
                        direct_chord_blocked_count += 1
                    path_deliverable = False
                    if edge_data.get("virtual"):
                        path_rejection_reason = "virtual_edge_no_alternative"
                    elif edge_data.get("type") == "ign_route":
                        path_rejection_reason = "no_path_in_existing_or_ign_graph"
                    else:
                        path_rejection_reason = "non_deliverable_no_alternative"
                    break

                raw_src = edge_data.get("src", edge_data.get("type", ""))
                raw_type = edge_data.get("type", "")
                raw_infra = edge_data.get("infra_type", "")
                raw_length = float(edge_data.get("length", 0.0))

                stored_geom = edge_data.get("geometry")
                if (
                    stored_geom is not None
                    and isinstance(stored_geom, LineString)
                    and not stored_geom.is_empty
                ):
                    out_geom = stored_geom
                    geom_is_real = True
                else:
                    out_geom = LineString([(u[0], u[1]), (v[0], v[1])])
                    geom_is_real = False

                # Track cumulative IGN-derived C0 length so the soft-cap
                # warning fires honestly at the end of the SRO.
                if raw_type == "ign_route" and raw_infra != "gc_neuf":
                    path_ign_delivered_pending_m += raw_length

                proposed_edges.append(
                    (u, v, edge_data, raw_src, raw_type, raw_infra,
                     raw_length, out_geom, geom_is_real)
                )

            if not path_deliverable:
                _mark_pb_impossible(str(pb_id), path_rejection_reason or "non_deliverable")
                if path is not None and pb_node in _paths_p1:
                    diag["pass1_existing_rejected"] += 1
                else:
                    diag["pass2_gc_rejected"] += 1
                if flag_collector is not None:
                    flag_collector.add(
                        "PA_PB_PATH_NON_DELIVERABLE",
                        target_url=f"{pa_id}->{pb_id}",
                        message=(
                            f"Aucun chemin livrable entre {pa_id} et {pb_id} "
                            f"({path_rejection_reason})."
                        ),
                    )
                continue

            # PR #36 — cumulative IGN soft-cap warning (no longer blocks).
            if (
                ign_route_delivered_as_gc_m + path_ign_delivered_pending_m
                > MAX_IGN_DELIVERED_PER_SRO_M
                and path_ign_delivered_pending_m > 0
            ):
                ign_cap_hit_count += 1
                if (
                    not _ign_blocked_flag_added
                    and flag_collector is not None
                ):
                    flag_collector.add(
                        "IGN_DELIVERED_BUDGET_EXCEEDED",
                        target_url=sro_code_log,
                        message=(
                            f"IGN-as-C0 cumulé > {MAX_IGN_DELIVERED_PER_SRO_M:.0f}m "
                            "sur ce SRO (livré en continuité — alerte budget)."
                        ),
                    )
                    _ign_blocked_flag_added = True

            # ── PR #41 — Per-path IGN budget. Reject paths whose IGN /
            # gc_neuf-as-C0 share would turn the livrable into a
            # spaghetti map. On the field test of 2026-05-18 we saw
            # paths made of >5 km of IGN per SRO with high_gc_ratio=0.96.
            # The rule: if the path's IGN length exceeds either an
            # absolute cap (PR41_MAX_IGN_PER_PATH_M) or a ratio of the
            # total path length (PR41_MAX_IGN_RATIO_PER_PATH), reject
            # the whole path rather than deliver a kilometric C0 chord.
            # The PB is reported as ``path_ign_budget_exceeded`` so the
            # operator can review it (rather than disappearing silently
            # behind a wall of C0).
            total_path_m = sum(p[6] for p in proposed_edges)
            ratio_violated = (
                total_path_m >= PR41_RATIO_MIN_TOTAL_M
                and (path_ign_delivered_pending_m / total_path_m)
                    > PR41_MAX_IGN_RATIO_PER_PATH
            )
            if (
                path_ign_delivered_pending_m > PR41_MAX_IGN_PER_PATH_M
                or ratio_violated
            ):
                ign_route_blocked_m += path_ign_delivered_pending_m
                ign_route_blocked_count += 1
                ign_cap_hit_count += 1
                _mark_pb_impossible(str(pb_id), "path_ign_budget_exceeded")
                diag["pass2_gc_rejected"] += 1
                if flag_collector is not None:
                    flag_collector.add(
                        "PATH_IGN_BUDGET_EXCEEDED",
                        target_url=f"{pa_id}->{pb_id}",
                        message=(
                            f"Chemin rejeté: IGN-as-C0={path_ign_delivered_pending_m:.0f}m "
                            f"sur {total_path_m:.0f}m total — au-delà du budget "
                            f"PR41 ({PR41_MAX_IGN_PER_PATH_M:.0f}m / "
                            f"ratio {PR41_MAX_IGN_RATIO_PER_PATH:.2f})."
                        ),
                    )
                continue

            # ── Commit path atomically ───────────────────────────────────
            for (u, v, edge_data, raw_src, raw_type, raw_infra,
                 raw_length, out_geom, geom_is_real) in proposed_edges:
                # PR #29 amend — accumulate raw-source telemetry BEFORE
                # any conversion. ign_route_length_used_m is computed
                # from raw_type, not from the converted output src.
                if edge_data.get("_logical_anchor"):
                    continue
                raw_src_norm = (
                    "gc_neuf" if raw_src == "gc_neuf_runtime" else raw_src
                ) or raw_type or ""
                raw_src_lengths[raw_src_norm] = (
                    raw_src_lengths.get(raw_src_norm, 0.0) + raw_length
                )
                raw_src_counts[raw_src_norm] = (
                    raw_src_counts.get(raw_src_norm, 0) + 1
                )
                raw_type_lengths[raw_type or ""] = (
                    raw_type_lengths.get(raw_type or "", 0.0) + raw_length
                )
                if raw_src == "gc_neuf_runtime":
                    raw_src = "gc_neuf"

                # PR #37 — track the upstream provenance of every C0 row
                # so post-routing audits can distinguish:
                #   - ``ign``: converted IGN segment (allowed at any length
                #     if short + public + within budget)
                #   - ``gc_neuf_planned``: pb_fictif injection (allowed)
                #   - ``micro_bridge``: short component bridge (≤3m public)
                #   - ``existing``: not a C0 at all
                c0_source: str | None = None
                if raw_type == "ign_route" and raw_infra != "gc_neuf":
                    mode_pose = "C0"
                    infra_type = "gc_neuf"
                    src = "gc_neuf"
                    converted_ign_to_gc_length_m += raw_length
                    ign_route_delivered_as_gc_m += raw_length
                    c0_source = "ign"
                elif raw_src in ("gc_neuf", "gc_neuf_runtime"):
                    mode_pose = "C0"
                    infra_type = "gc_neuf"
                    src = "gc_neuf"
                    # Distinguish planned gc_neuf (injected) from a
                    # micro-bridge that was promoted to deliverable.
                    if edge_data.get("virtual_reason") == "micro_bridge":
                        c0_source = "micro_bridge"
                    else:
                        c0_source = "gc_neuf_planned"
                else:
                    mode_pose = edge_data.get("mode_pose", "")
                    infra_type = raw_infra or raw_src or raw_type
                    src = raw_src or raw_type
                    c0_source = "existing"

                out_statut = edge_data.get("statut")
                if mode_pose == "C0":
                    out_statut = None
                elif out_statut is None:
                    out_statut = ""

                ekey = _edge_key(u, v)
                if ekey in edges_out:
                    paths = _path_membership_to_set(edges_out[ekey].get("_used_by_paths"))
                    paths.add(path_id)
                    edges_out[ekey]["_used_by_paths"] = _serialize_path_membership(paths)
                    continue
                edges_out[ekey] = {
                    "sro": sro,
                    "pa_id": pa_id,
                    "pb_id": pb_id,
                    "statut": out_statut,
                    "mode_pose": mode_pose,
                    "infra_type": infra_type,
                    "src": src,
                    "length_m": raw_length,
                    "geometry": out_geom,
                    "_c0_source": c0_source,
                    "_c0_source_line_id": (
                        edge_data.get("_source_line_id")
                        if c0_source == "ign"
                        else None
                    ),
                    "_used_by_paths": path_id,
                }

                # PR #34 amend (v3) — straight_connectors counts ONLY
                # truly synthetic chord segments: a 2-vertex line emitted
                # because no real source polyline geometry was available.
                # A 2-coord IGN polyline segment with a real ``geometry``
                # attribute is legitimate and must NOT be counted, otherwise
                # the metric inflates by O(IGN segments) and the livrable
                # always looks broken even when it isn't.
                if (
                    infra_type == "gc_neuf"
                    and not geom_is_real
                    and out_geom is not None
                ):
                    try:
                        if len(out_geom.coords) == 2:
                            straight_connector_count += 1
                            straight_connector_length_m += raw_length
                    except Exception:
                        pass
            committed_paths[path_id] = {
                "pa_id": pa_id,
                "pb_id": pb_id,
                "pa_anchor": pa_node,
                "pb_anchor": pb_node,
                "pa_final_anchor": pa_final_anchor,
                "pb_final_anchor": pb_final_anchor,
            }
            _mark_pb_committed(str(pb_id))

    perf["dijkstra_total"] = time.perf_counter() - t0_dijkstra

    def _log_pr39_routing_anchor_qa() -> None:
        pb_dropped = max(
            0,
            pb_routing_stats["pb_total"]
            - len(pb_committed_ids)
            - len(pb_impossible_reasons),
        )
        if pb_unassigned_reasons:
            reasons = ",".join(
                f"{k}:{v}" for k, v in sorted(pb_unassigned_reasons.items())
            )
        else:
            reasons = "none"
        impossible_reason_counts: dict[str, int] = {}
        for reason in pb_impossible_reasons.values():
            impossible_reason_counts[reason] = impossible_reason_counts.get(reason, 0) + 1
        impossible_reasons = (
            ",".join(f"{k}:{v}" for k, v in sorted(impossible_reason_counts.items()))
            if impossible_reason_counts else "none"
        )
        log.info(
            "[PB ROUTING QA] sro=%s pb_total=%d pb_assigned=%d "
            "pb_unassigned=%d pb_attempted=%d pb_committed=%d "
            "pb_impossible=%d pb_dropped=%d pb_unassigned_reasons=%s "
            "pb_impossible_reasons=%s",
            sro_code_log,
            pb_routing_stats["pb_total"],
            pb_routing_stats["pb_assigned"],
            pb_routing_stats["pb_unassigned"],
            len(pb_attempted_ids),
            len(pb_committed_ids),
            len(pb_impossible_reasons),
            pb_dropped,
            reasons,
            impossible_reasons,
        )
        log.info(
            "[ANCHOR QA] sro=%s terminal_visible_connectors=%d "
            "terminal_logical_only=%d terminal_anchor_missing=%d",
            sro_code_log,
            anchor_stats["terminal_visible_connectors"],
            anchor_stats["terminal_logical_only"],
            anchor_stats["terminal_anchor_missing"],
        )

    if not edges_out:
        _log_pr39_routing_anchor_qa()
        # PR #29 B4: log perf even on empty output so a slow empty SRO is visible.
        log.info("[ROUTING PERF] sro=%s step=dijkstra_total seconds=%.2f pa_count=%d pb_count=%d",
                 sro_code_log, perf.get("dijkstra_total", 0.0), pa_count, pb_count)
        # PR #34 amend v3: emit [FINAL TOPO QA] even when nothing was
        # delivered, so the cap-hit / straight-connector counters are
        # always observable in the run logs. An empty livrable is a
        # legitimate outcome (e.g., all paths rejected for cap) and
        # should still report its zero-row QA snapshot.
        log.info(
            "[FINAL TOPO QA] sro=%s connected=0 disconnected=%d "
            "pa_pb_connected_ratio=0.00 straight_connectors=%d "
            "straight_connector_length_m=%.0f virtual_delivered=0 "
            "ign_cap_hit=%d c0_without_source_geometry=0 "
            "long_direct_c0_count=0 c0_without_ign_source=0 "
            "path_impossible_private_c0=0 c0_private_crossing_kept=0 "
            "pb_impossible_private_c0=0",
            sro_code_log, pb_count,
            straight_connector_count, straight_connector_length_m,
            ign_cap_hit_count,
        )
        log.info(
            "[C0 GEOM QA] sro=%s c0_suspicious_chord_count=0 "
            "c0_long_without_route_geometry_count=0",
            sro_code_log,
        )
        log.info(
            "[ROAD C0 QA] sro=%s road_c0_delivered_count=0 "
            "road_c0_delivered_m=0 parcel_gate_disabled_count=0 "
            "direct_chord_blocked_count=%d c0_without_route_geometry_count=0 "
            "c0_suspicious_chord_count=0",
            sro_code_log,
            direct_chord_blocked_count
            + int(bridge_diagnostics.get("direct_chord_blocked_count", 0)),
        )
        return gpd.GeoDataFrame(geometry=[], crs=config.PROJECT_CRS)

    result = gpd.GeoDataFrame(list(edges_out.values()), geometry="geometry", crs=config.PROJECT_CRS)

    # ── PR #30: STRICT final filter on C0/gc_neuf — uses
    # delivery_public_area_safe (no IGN buffer) so any leftover delivered
    # C0/gc_neuf row crossing private land is removed. Existing infra
    # (bt/ft/athd/chem) is never touched here.
    t0 = time.perf_counter()
    final_removed_private_gc = 0
    private_crossing_final_length_m = 0.0
    private_c0_path_ids: set[str] = set()
    if (
        not ALLOW_IGN_ROAD_C0_WITHOUT_PARCEL_GATE
        and delivery_public_area_safe is not None
        and not result.empty
    ):
        mask_c0 = (result["mode_pose"] == "C0") | (result["infra_type"] == "gc_neuf")

        def _row_in_delivery(g):
            if g is None or getattr(g, "is_empty", False):
                return False
            return delivery_public_area_safe.covers(g)

        # Compute mask + collect length of removed rows for QA telemetry.
        c0_mask_idx = result.index[mask_c0]
        removed_idx: list[int] = []
        for idx in c0_mask_idx:
            geom = result.geometry.loc[idx]
            if not _row_in_delivery(geom):
                private_c0_path_ids.update(
                    _path_membership_to_set(result.loc[idx].get("_used_by_paths"))
                )
                removed_idx.append(idx)
                private_crossing_final_length_m += float(
                    result.loc[idx, "length_m"] or 0.0
                )
        if removed_idx:
            final_removed_private_gc = len(removed_idx)
            # Per-SRO flag (single entry, message lists the count + length).
            if flag_collector is not None:
                flag_collector.add(
                    "C0_PRIVATE_CROSSING_REMOVED",
                    target_url=sro_code_log,
                    message=(
                        f"C0/gc_neuf supprimés en final (traversent prive) : "
                        f"{final_removed_private_gc} arêtes / "
                        f"{private_crossing_final_length_m:.0f} m"
                    ),
                )
            result = result.drop(index=removed_idx).copy()
            if private_c0_path_ids and "_used_by_paths" in result.columns:
                keep_rows: list[int] = []
                for idx, row in result.iterrows():
                    paths = _path_membership_to_set(row.get("_used_by_paths"))
                    if paths:
                        paths.difference_update(private_c0_path_ids)
                        if not paths:
                            continue
                        result.at[idx, "_used_by_paths"] = _serialize_path_membership(paths)
                    keep_rows.append(idx)
                result = result.loc[keep_rows].copy()
            private_pb_ids = {
                str(committed_paths[pid].get("pb_id"))
                for pid in private_c0_path_ids
                if pid in committed_paths
            }
            for pid in sorted(private_c0_path_ids):
                rec = committed_paths.pop(pid, None)
                if rec is None:
                    continue
                _mark_pb_impossible(str(rec.get("pb_id")), "private_c0")
                diag["path_impossible_private_c0"] += 1
            diag["pb_impossible_private_c0"] = len(private_pb_ids)
            log.info(
                "[GC QA] Final pass removed %d C0/gc_neuf edges crossing "
                "private (length=%.0fm)",
                final_removed_private_gc, private_crossing_final_length_m,
            )
    perf["final_filter"] = time.perf_counter() - t0

    # ── PR #28 BLOQUANT 5: deduplicate near-identical geometries ──────
    t0 = time.perf_counter()
    n_before_dedup = len(result)
    result = _dedup_geometries(result)
    n_after_dedup = len(result)
    perf["dedup"] = time.perf_counter() - t0

    # ── PR #31 — Topology validation pipeline. Runs AFTER the existing
    # PR28/PR29/PR30 filters so it operates on geometries that are
    # already strict-public-only and exact-dedupped. The pipeline:
    #   1. snaps endpoints to exact identical coords
    #   2. splits livrable lines at PA/PB projections and adds public
    #      connectors so terminals visually touch the network
    #   3. removes near-duplicates by metier hierarchy
    #   4. drops aerial-energy (E1 / bt) segments crossing private land
    #   5. audits PA→PB reachability + micro-gaps
    #   6. counts support-switch zigzags (no rerouting)
    #   7. computes mutualisation stats
    t0 = time.perf_counter()
    from . import livrable_topology as _lt
    committed_pb_ids = {str(rec["pb_id"]) for rec in committed_paths.values()}
    committed_pa_ids = {str(rec["pa_id"]) for rec in committed_paths.values()}
    pb_for_topology = pb_sro_routing
    if committed_pb_ids and "pb_id" in pb_sro_routing.columns:
        pb_for_topology = pb_sro_routing[
            pb_sro_routing["pb_id"].astype(str).isin(committed_pb_ids)
        ].copy()
    pa_for_topology = pa_sro
    if committed_pa_ids and "id_metier" in pa_sro.columns:
        pa_for_topology = pa_sro[
            pa_sro["id_metier"].astype(str).isin(committed_pa_ids)
        ].copy()
    topology_delivery_public_area_safe = (
        None
        if ALLOW_IGN_ROAD_C0_WITHOUT_PARCEL_GATE
        else delivery_public_area_safe
    )
    result, pr31_stats = _lt.finalize_livrable_topology(
        result, pa_for_topology, pb_for_topology, sro_code_log,
        delivery_public_area_safe=topology_delivery_public_area_safe,
        flag_collector=flag_collector,
    )
    perf["pr31_topology"] = time.perf_counter() - t0
    # PR38: verify committed paths still exist in final graph rows
    if committed_paths:
        if "_used_by_paths" in result.columns:
            remaining: set[str] = set()
            for v in result["_used_by_paths"].dropna().unique():
                remaining.update(_path_membership_to_set(v))
            for pid in committed_paths:
                if pid not in remaining:
                    diag["path_lost_between_routing_and_final_graph"] += 1
                    diag["path_broken_after_postprocess"] += 1
        else:
            diag["path_lost_between_routing_and_final_graph"] += len(committed_paths)
            diag["path_broken_after_postprocess"] += len(committed_paths)

    def _build_final_graph(df: gpd.GeoDataFrame) -> nx.Graph:
        return _lt._build_livrable_topology_graph(df)

    # PR38 hard validation: actual final-graph reachability for committed paths.
    # PR41 fix — the PA / PB anchor coordinates were captured during the
    # routing stage, BEFORE ``finalize_livrable_topology`` ran. That stage
    # snaps near-by endpoints into shared centroids (``_snap_endpoints_to_-
    # exact``, T-junction splits, etc.), so the anchor (x, y) recorded in
    # ``committed_paths`` is no longer guaranteed to be a literal node of
    # the final graph. A literal ``pa_a in G_final`` lookup therefore
    # returned False for paths that were still genuinely reachable, which
    # is what produced ``committed_path_unreachable_final_graph=7/7`` on
    # 63149/M06/PMZ/42478 even though all 7 PBs had been committed.
    #
    # We now resolve each anchor to the NEAREST final-graph node within
    # a small tolerance before checking reachability. The tolerance must
    # be greater than ``ENDPOINT_SNAP_TOL_M`` (0.5 m) so a snap-to-exact
    # shift cannot fool the lookup, but small enough not to glue PA and
    # PB onto unrelated network branches. 2 m matches PR #36's
    # TERMINAL_TOUCH_TOL_M class of tolerance.
    PR41_ANCHOR_LOOKUP_TOL_M = 2.0
    G_final = _build_final_graph(result)
    _final_node_list = list(G_final.nodes())

    def _resolve_anchor(target):
        if target is None:
            return None
        if target in G_final:
            return target
        # Linear scan is fine here — typical SROs have < a few thousand
        # nodes, called once per committed path.
        try:
            tx, ty = float(target[0]), float(target[1])
        except (TypeError, ValueError, IndexError):
            return None
        best = None
        best_d2 = (PR41_ANCHOR_LOOKUP_TOL_M ** 2)
        for n in _final_node_list:
            try:
                dx = float(n[0]) - tx
                dy = float(n[1]) - ty
            except (TypeError, ValueError, IndexError):
                continue
            d2 = dx * dx + dy * dy
            if d2 <= best_d2:
                best_d2 = d2
                best = n
        return best

    unreachable_committed = 0
    unreachable_path_ids: set[str] = set()
    for pid, rec in committed_paths.items():
        pa_a_raw = rec.get("pa_final_anchor") or rec.get("pa_anchor")
        pb_a_raw = rec.get("pb_final_anchor") or rec.get("pb_anchor")
        pa_a = _resolve_anchor(pa_a_raw)
        pb_a = _resolve_anchor(pb_a_raw)
        metadata_present = (
            "_used_by_paths" in result.columns
            and any(
                pid in _path_membership_to_set(v)
                for v in result["_used_by_paths"].dropna().unique()
            )
        )
        reachable = (
            pa_a is not None and pb_a is not None
            and pa_a in G_final and pb_a in G_final
            and nx.has_path(G_final, pa_a, pb_a)
        )
        if reachable:
            diag["committed_path_reachable_final_graph"] += 1
        else:
            diag["committed_path_unreachable_final_graph"] += 1
            unreachable_committed += 1
            unreachable_path_ids.add(pid)
            if metadata_present:
                diag["path_metadata_present_but_graph_disconnected"] += 1
            diag["path_broken_after_postprocess"] += 1

    # ── PR #26 [INFRA QA] diagnostic logs ───────────────────────────────
    _log_infra_qa(result, pa_sro)

    # ── PR #27 Part D [GC QA] bridge diagnostics ─────────────────────────
    _log_gc_qa(result, pa_sro)

    # ── PR #28 [MUTUAL QA] mutualisation diagnostics ─────────────────────
    _log_mutual_qa(n_before_dedup, n_after_dedup, pa_sro)

    # PR #41 — honest pb_committed counter. A PB whose committed path
    # cannot be reached on the final livrable graph must NOT be counted
    # as committed in the QA log. Pierre's brief: "pb_committed doit
    # représenter des chemins livrés et connectés final graph, pas
    # juste trouvés avant postprocess." Demote unreachable PBs to
    # ``pb_impossible`` with reason ``path_broken_after_postprocess``
    # so the field log surfaces the real shape of the delivery.
    for pid in unreachable_path_ids:
        rec = committed_paths.get(pid)
        if rec is None:
            continue
        pb_id_str = str(rec.get("pb_id"))
        if pb_id_str in pb_committed_ids:
            pb_committed_ids.discard(pb_id_str)
            _mark_pb_impossible(pb_id_str, "path_broken_after_postprocess")

    _log_pr39_routing_anchor_qa()

    # ── PR #29 A2 / amend: [ROUTING QA] uses RAW counters collected during
    # path collection (BEFORE the ign_route → gc_neuf conversion), so
    # ign_route_length_used_m reflects what Dijkstra actually traversed.
    # PR #30: also surfaces delivery telemetry (delivered vs blocked,
    # final-filter removals, gaps_remaining_estimate).
    gaps_remaining_estimate = nx.number_connected_components(G)
    existing_connectors_added = 0
    if isinstance(_line_snap_stats, dict):
        existing_connectors_added = int(
            _line_snap_stats.get("existing_connectors_added", 0)
        )
    _log_routing_qa(
        result, sro_code_log,
        raw_src_lengths=raw_src_lengths,
        raw_src_counts=raw_src_counts,
        raw_type_lengths=raw_type_lengths,
        converted_ign_to_gc_length_m=converted_ign_to_gc_length_m,
        ign_route_delivered_as_gc_m=ign_route_delivered_as_gc_m,
        ign_route_blocked_m=ign_route_blocked_m,
        final_removed_private_gc=final_removed_private_gc,
        private_crossing_final_count=final_removed_private_gc,
        private_crossing_final_length_m=private_crossing_final_length_m,
        existing_connectors_added=existing_connectors_added,
        gaps_remaining_estimate=gaps_remaining_estimate,
    )

    # ── PR #33 — [FINAL TOPO QA] mandatory log ──────────────────────────
    # PR #34 amend (v3): count ONLY genuinely degenerate C0 rows (no
    # geometry at all, empty geometry, or a self-loop). A 2-vertex
    # LineString is legitimate for a real IGN-derived C0 segment — those
    # are the natural shape between consecutive polyline vertices — and
    # must not inflate this counter. The path-level deliverability check
    # (above) already ensures that no synthesized chord (virtual /
    # non-deliverable edge) reaches this stage.
    c0_without_source_geom = 0
    if not result.empty:
        c0_mask = result["mode_pose"] == "C0"
        for idx in result.index[c0_mask]:
            geom = result.loc[idx, "geometry"]
            if geom is None or not isinstance(geom, LineString) or geom.is_empty:
                c0_without_source_geom += 1
                continue
            coords = list(geom.coords)
            if len(coords) < 2:
                c0_without_source_geom += 1
                continue
            if coords[0] == coords[-1] and len(coords) == 2:
                # degenerate zero-length self-loop
                c0_without_source_geom += 1

    pa_pb_connected = pr31_stats.get("pa_pb_connected_count", 0) if isinstance(pr31_stats, dict) else 0
    pa_pb_disconnected = pr31_stats.get("pa_pb_disconnected_count", 0) if isinstance(pr31_stats, dict) else 0
    if committed_paths:
        pa_pb_connected = diag["committed_path_reachable_final_graph"]
        pa_pb_disconnected = diag["committed_path_unreachable_final_graph"]
    pa_pb_total = pa_pb_connected + pa_pb_disconnected
    pa_pb_ratio = pa_pb_connected / pa_pb_total if pa_pb_total > 0 else 0.0

    # PR #34 amend (point QA): compute a real ``virtual_delivered`` value
    # from the final GeoDataFrame instead of hard-coding 0. If neither
    # ``virtual`` nor ``deliverable`` columns are present (the routing path
    # never emits them by design), the counter stays 0 — but it is now an
    # observed 0 rather than a hard-coded reassurance.
    virtual_edges_delivered_count = 0
    if not result.empty:
        if "virtual" in result.columns:
            virtual_edges_delivered_count += int(
                result["virtual"].fillna(False).astype(bool).sum()
            )
        if "deliverable" in result.columns:
            virtual_edges_delivered_count += int(
                (result["deliverable"].fillna(True).astype(bool) == False).sum()
            )

    # ── PR #37 — strict C0 provenance audit. Every delivered C0 row
    # must trace back to a legitimate source:
    #   - ``ign``                  : IGN polyline segment converted to C0
    #   - ``gc_neuf_planned``      : pb_fictif injection
    #   - ``micro_bridge``         : public ≤3m component bridge
    #   - terminal connector emitted by livrable_topology._ensure_-
    #     terminals_connected (these are inserted AFTER routing and
    #     have no ``_c0_source`` column; we recognise them by length
    #     ≤ TERMINAL_CONNECTOR_MAX_LENGTH_M = 3m)
    # Any C0 row that fails this audit is a regression and counted by
    # ``long_direct_c0_count`` / ``c0_without_ign_source``.
    long_direct_c0_count = 0
    c0_without_ign_source = 0
    c0_suspicious_chord_count = 0
    c0_long_without_route_geometry_count = 0
    road_c0_delivered_count = 0
    road_c0_delivered_m = 0.0
    road_c0_source_line_ids: set[str] = set()
    parcel_gate_disabled_count = 0
    c0_without_route_geometry_count = 0
    if not result.empty:
        c0_mask = result["mode_pose"] == "C0"
        for idx in result.index[c0_mask]:
            length_m = float(result.loc[idx, "length_m"] or 0.0)
            infra_kind = str(result.loc[idx, "infra_type"] or "")
            src_tag = (
                result.loc[idx, "_c0_source"]
                if "_c0_source" in result.columns
                else None
            )
            if infra_kind == "terminal_connector" and length_m > MAX_STRAIGHT_CONNECTOR_M:
                long_direct_c0_count += 1
                c0_without_ign_source += 1
                continue
            # Terminal connectors (inserted by livrable_topology) are
            # short (<= 3 m) and tagged ``terminal``. Untagged rows of
            # the same length are tolerated for backwards compatibility.
            if src_tag == "terminal" and length_m <= 3.0:
                continue
            if src_tag is None and length_m <= 3.0:
                continue
            if src_tag in ("ign", "gc_neuf_planned", "micro_bridge"):
                if src_tag == "ign":
                    source_line_id = (
                        result.loc[idx, "_c0_source_line_id"]
                        if "_c0_source_line_id" in result.columns
                        else None
                    )
                    road_c0_source_line_ids.add(str(source_line_id or idx))
                    road_c0_delivered_m += length_m
                    geom = result.loc[idx, "geometry"]
                    if (
                        ALLOW_IGN_ROAD_C0_WITHOUT_PARCEL_GATE
                        and parcel_gate_reference_area is not None
                        and geom is not None
                        and not getattr(geom, "is_empty", False)
                        and not parcel_gate_reference_area.covers(geom)
                    ):
                        parcel_gate_disabled_count += 1
                continue
            # Anything else is suspect
            c0_without_ign_source += 1
            c0_without_route_geometry_count += 1
            if length_m > 3.0:
                long_direct_c0_count += 1

        for idx in result.index[c0_mask]:
            geom = result.loc[idx, "geometry"]
            if geom is None or not isinstance(geom, LineString) or geom.is_empty:
                continue
            length_m = float(result.loc[idx, "length_m"] or geom.length or 0.0)
            infra_kind = str(result.loc[idx, "infra_type"] or "")
            src_tag = result.loc[idx, "_c0_source"] if "_c0_source" in result.columns else None
            paths = _path_membership_to_set(result.loc[idx].get("_used_by_paths"))
            try:
                is_two_point = len(list(geom.coords)) == 2
            except Exception:
                is_two_point = False
            if infra_kind == "terminal_connector" and length_m > MAX_STRAIGHT_CONNECTOR_M:
                c0_suspicious_chord_count += 1
            elif length_m > IGN_DELIVERY_MAX_LENGTH_M and is_two_point and src_tag != "ign":
                c0_suspicious_chord_count += 1
                c0_long_without_route_geometry_count += 1
            elif paths & unreachable_path_ids:
                c0_suspicious_chord_count += 1
            if (
                delivery_public_area_safe is not None
                and not delivery_public_area_safe.covers(geom)
            ):
                c0_suspicious_chord_count += 1
                diag["c0_private_crossing_kept"] += 1

    road_c0_delivered_count = len(road_c0_source_line_ids)
    direct_chord_blocked_count += int(
        bridge_diagnostics.get("direct_chord_blocked_count", 0)
    )

    log.info(
        "[C0 GEOM QA] sro=%s c0_suspicious_chord_count=%d "
        "c0_long_without_route_geometry_count=%d",
        sro_code_log,
        c0_suspicious_chord_count,
        c0_long_without_route_geometry_count,
    )
    log.info(
        "[ROAD C0 QA] sro=%s road_c0_delivered_count=%d "
        "road_c0_delivered_m=%.0f parcel_gate_disabled_count=%d "
        "direct_chord_blocked_count=%d c0_without_route_geometry_count=%d "
        "c0_suspicious_chord_count=%d",
        sro_code_log,
        road_c0_delivered_count,
        road_c0_delivered_m,
        parcel_gate_disabled_count,
        direct_chord_blocked_count,
        c0_without_route_geometry_count,
        c0_suspicious_chord_count,
    )

    log.info(
        "[FINAL TOPO QA] sro=%s connected=%d disconnected=%d pa_pb_connected_ratio=%.2f "
        "straight_connectors=%d straight_connector_length_m=%.0f "
        "virtual_delivered=%d ign_cap_hit=%d c0_without_source_geometry=%d "
        "long_direct_c0_count=%d c0_without_ign_source=%d "
        "pass1_existing_reached=%d pass1_existing_committed=%d pass1_existing_rejected=%d "
        "pass2_gc_reached=%d pass2_gc_committed=%d pass2_gc_rejected=%d "
        "pb_unreachable_after_pass1=%d pb_unreachable_after_pass2=%d "
        "path_rejected_missing_anchor=%d path_lost_between_routing_and_final_graph=%d "
        "path_broken_after_postprocess=%d committed_path_reachable_final_graph=%d "
        "committed_path_unreachable_final_graph=%d "
        "path_metadata_present_but_graph_disconnected=%d "
        "path_impossible_private_c0=%d c0_private_crossing_kept=%d "
        "pb_impossible_private_c0=%d",
        sro_code_log, pa_pb_connected, pa_pb_disconnected, pa_pb_ratio,
        straight_connector_count, straight_connector_length_m,
        virtual_edges_delivered_count,
        ign_cap_hit_count,
        c0_without_source_geom,
        long_direct_c0_count,
        c0_without_ign_source,
        diag["pass1_existing_reached"],
        diag["pass1_existing_committed"],
        diag["pass1_existing_rejected"],
        diag["pass2_gc_reached"],
        diag["pass2_gc_committed"],
        diag["pass2_gc_rejected"],
        diag["pb_unreachable_after_pass1"],
        diag["pb_unreachable_after_pass2"],
        diag["path_rejected_missing_anchor"],
        diag["path_lost_between_routing_and_final_graph"],
        diag["path_broken_after_postprocess"],
        diag["committed_path_reachable_final_graph"],
        diag["committed_path_unreachable_final_graph"],
        diag["path_metadata_present_but_graph_disconnected"],
        diag["path_impossible_private_c0"],
        diag["c0_private_crossing_kept"],
        diag["pb_impossible_private_c0"],
    )

    # PR #37 — strip the internal ``_c0_source`` provenance column
    # before returning. Writer / GPKG output stays unchanged.
    if "_c0_source" in result.columns:
        result = result.drop(columns=["_c0_source"])
    if "_c0_source_line_id" in result.columns:
        result = result.drop(columns=["_c0_source_line_id"])

    # PR #29 B4: per-step + total perf logs ───────────────────────────
    perf["total_route_pa_to_pb"] = time.perf_counter() - t_total_start
    for step, sec in perf.items():
        if step == "dijkstra_total":
            log.info(
                "[ROUTING PERF] sro=%s step=%s seconds=%.2f pa_count=%d pb_count=%d",
                sro_code_log, step, sec, pa_count, pb_count,
            )
        else:
            log.info("[ROUTING PERF] sro=%s step=%s seconds=%.2f", sro_code_log, step, sec)

    return result


# ---------------------------------------------------------------------------
# PR #28 BLOQUANT 4 — Endpoint-to-line snap
# ---------------------------------------------------------------------------

_SNAP_TO_LINE_RADIUS_M = 3.0  # max distance for endpoint→line projection


def _snap_endpoints_to_lines(
    G: nx.Graph,
    snap_radius_m: float = _SNAP_TO_LINE_RADIUS_M,
    public_area: object = _SENTINEL,   # PR #28 amend B1
    public_area_safe=None,              # PR #29 B1: pre-buffered for perf
    flag_collector=None,
) -> dict:
    """Snap remaining degree-1 endpoints onto nearby edges (project+split).

    After endpoint→endpoint merge, some dangling endpoints may still sit
    close to a line without touching. This step projects each endpoint
    onto the nearest qualifying edge, validates the resulting connector,
    splits the line, and connects the graph.

    Conservative rules:
    - Only degree-1 nodes (true A/Z endpoints after earlier merges)
    - Edge must NOT be gc_neuf (don't split artificial bridges)
    - Distance must be <= snap_radius_m
    - Projection point must be between edge endpoints, not extension

    PR #29 B2: validate the public-domain check on the connector BEFORE
    splitting the target line. A rejected connector no longer leaves the
    graph in a half-split state, and the original line stays intact.

    PR #29 A3: when the original edge has a stored polyline geometry,
    project onto that real geometry to keep visual continuity (the
    connector + sub-segments use the actual road/infra shape).

    Returns stats dict for QA logging.
    """
    empty_stats = {
        "endpoints_to_lines": 0,
        "endpoints_rejected_private": 0,
        "existing_connectors_added": 0,
    }
    if G.number_of_nodes() < 2 or G.number_of_edges() < 1:
        return empty_stats

    from scipy.spatial import cKDTree  # noqa: F401  (used elsewhere; keeps import warm)
    from shapely.geometry import Point as _Point

    # PR #29 B1: pre-compute buffered public area once.
    if public_area is not _SENTINEL and public_area_safe is None:
        public_area_safe = _public_area_safe(public_area)

    # Collect degree-1 nodes and edges (PR #29 A3: prefer existing infra).
    endpoint_nodes = [n for n in G.nodes() if G.degree(n) == 1]
    if len(endpoint_nodes) == 0:
        return empty_stats

    edge_index: list[tuple] = []
    edge_lines: list[LineString] = []
    for u, v, data in G.edges(data=True):
        if data.get("type") == "gc_neuf":
            continue  # don't split artificial bridges
        # PR #29 A3: prefer the stored real geometry if available so that
        # the projection respects the actual line shape (curves, etc.).
        stored = data.get("geometry")
        if stored is not None and isinstance(stored, LineString) and not stored.is_empty:
            line = stored
        else:
            line = LineString([(u[0], u[1]), (v[0], v[1])])
        edge_index.append((u, v, data, line))
        edge_lines.append(line)

    if len(edge_lines) == 0:
        return empty_stats

    edge_tree = STRtree(edge_lines)
    n_snapped = 0
    n_rejected_private = 0
    n_existing_connectors = 0

    # PR #30 BLOQUANT 6 — priorité absolue à l'existant avant IGN comme
    # cible de snap. Tier 0 = type=="infra" (existant), Tier 1 = ign_route,
    # Tier 2 = autres. gc_neuf est déjà exclu plus haut.
    def _priority_tier(data: dict) -> int:
        t = data.get("type")
        if t == "infra":
            return 0
        if t == "ign_route":
            return 1
        return 2

    for ep in endpoint_nodes:
        ep_pt = _Point(ep[0], ep[1])
        buf = ep_pt.buffer(snap_radius_m)
        candidates = edge_tree.query(buf)

        # best tuple: (tier, u, v, data, line, proj_pt, proj_dist, seg_dist)
        best = None
        for idx in candidates:
            u, v, data, line = edge_index[idx]
            if ep == u or ep == v:
                continue
            proj_dist = line.project(ep_pt)
            proj_pt = line.interpolate(proj_dist)
            d = ep_pt.distance(proj_pt)
            if d > snap_radius_m:
                continue
            # ── Projection must be inside the segment (not at endpoint extension) ──
            if proj_dist <= 0 or proj_dist >= line.length:
                continue
            tier = _priority_tier(data)
            cand = (tier, u, v, data, line, proj_pt, proj_dist, d)
            # Lower tier wins. Within the same tier, the closer projection
            # wins. Pre-existing behaviour is recovered when only one
            # tier is present in the candidate set.
            if best is None or tier < best[0] or (
                tier == best[0] and d < best[7]
            ):
                best = cand

        if best is None:
            continue

        tier, u, v, data, line, proj_pt, proj_dist, _seg_d = best
        proj_key = _point_key(proj_pt)

        # ── Skip if projection already a node ──
        if proj_key in G:
            continue

        if not G.has_edge(u, v):
            continue

        # ── PR #29 B2: validate connector BEFORE splitting ──────────────
        connector = LineString([(ep[0], ep[1]), (proj_pt.x, proj_pt.y)])
        ep_dist = ep_pt.distance(proj_pt)

        if public_area is not _SENTINEL:
            if public_area_safe is None:
                if flag_collector is not None:
                    flag_collector.add(
                        "GC_NEUF_ROUTING_IMPOSSIBLE",
                        target_url=f"endpoint=({ep[0]:.0f},{ep[1]:.0f})",
                        message="Endpoint→line connector rejected — no public domain",
                    )
                n_rejected_private += 1
                continue
            if not public_area_safe.covers(connector):
                if flag_collector is not None:
                    flag_collector.add(
                        "GC_NEUF_PRIVATE_CROSSING",
                        target_url=f"endpoint=({ep[0]:.0f},{ep[1]:.0f})",
                        message=f"Endpoint→line connector rejected — traverses private, len={ep_dist:.0f}m",
                    )
                n_rejected_private += 1
                continue

        # ── Connector accepted: now split the line and add edges ────────
        G.remove_edge(u, v)
        extra = {k: data[k] for k in data if k not in ("length", "geometry")}
        d1 = _Point(u[0], u[1]).distance(proj_pt)
        d2 = _Point(v[0], v[1]).distance(proj_pt)

        # Build sub-geometries from the original edge geometry when possible.
        orig_geom = data.get("geometry")
        if orig_geom is not None and isinstance(orig_geom, LineString) and not orig_geom.is_empty:
            try:
                seg_a = shapely.ops.substring(orig_geom, 0, proj_dist)
                seg_b = shapely.ops.substring(orig_geom, proj_dist, orig_geom.length)
            except Exception:
                seg_a = LineString([(u[0], u[1]), (proj_pt.x, proj_pt.y)])
                seg_b = LineString([(proj_pt.x, proj_pt.y), (v[0], v[1])])
        else:
            seg_a = LineString([(u[0], u[1]), (proj_pt.x, proj_pt.y)])
            seg_b = LineString([(proj_pt.x, proj_pt.y), (v[0], v[1])])

        G.add_edge(u, proj_key, length=d1, geometry=seg_a, **extra)
        G.add_edge(proj_key, v, length=d2, geometry=seg_b, **extra)

        # PR #34 amend v3 — RELOCATE the dangling endpoint into the
        # projection node instead of adding a virtual perpendicular
        # connector. Pierre's brief item #5: "PA/PB doivent être
        # connectés par split/projection sur infra livrée proche, sans
        # connecteur droit visible." A virtual connector still works for
        # Dijkstra but fails the new path-level deliverability check,
        # so the PB ends up reported as disconnected.
        #
        # Relocation: rewrite each edge incident on ``ep`` so its
        # ``ep``-endpoint becomes ``proj_key``. The edge's stored
        # geometry is patched to match the new endpoint, and the edge's
        # ``length`` is recomputed from the new coordinates. This
        # introduces at most a ``snap_radius_m``-wide visual nudge on
        # the snapped segment near its tip, which is the well-known
        # trade-off Pierre accepts vs visible straight connectors.
        if ep != proj_key:
            for w in list(G.neighbors(ep)):
                e_data = G.get_edge_data(ep, w)
                if e_data is None:
                    continue
                G.remove_edge(ep, w)
                new_data = dict(e_data)
                geom_old = new_data.get("geometry")
                if (
                    geom_old is not None
                    and isinstance(geom_old, LineString)
                    and not geom_old.is_empty
                ):
                    coords_g = [_coord2d(c) for c in geom_old.coords]
                    if len(coords_g) >= 2:
                        p_first = _Point(coords_g[0])
                        p_last = _Point(coords_g[-1])
                        if p_first.distance(_Point(ep)) <= p_last.distance(_Point(ep)):
                            coords_g[0] = (float(proj_pt.x), float(proj_pt.y))
                        else:
                            coords_g[-1] = (float(proj_pt.x), float(proj_pt.y))
                        if coords_g[0] != coords_g[-1]:
                            try:
                                new_data["geometry"] = LineString(coords_g)
                            except (ValueError, TypeError):
                                pass
                new_data["length"] = _Point(proj_pt.x, proj_pt.y).distance(
                    _Point(w[0], w[1])
                )
                if proj_key != w:
                    G.add_edge(proj_key, w, **new_data)
            if G.degree(ep) == 0:
                G.remove_node(ep)
        n_snapped += 1
        # PR #30 — count as an "existing_connector" only when the snap
        # target was an existing infra line (tier 0). Snaps onto IGN
        # remain gc_neuf-class and are NOT counted here.
        if tier == 0:
            n_existing_connectors += 1

    if n_snapped or n_rejected_private:
        cc_after = nx.number_connected_components(G)
        log.info(
            "[TOPO SNAP] %d endpoints projected onto lines (existing=%d, rejected_private=%d), "
            "%d composantes connexes",
            n_snapped, n_existing_connectors, n_rejected_private, cc_after,
        )

    return {
        "endpoints_to_lines": n_snapped,
        "endpoints_rejected_private": n_rejected_private,
        "existing_connectors_added": n_existing_connectors,
    }


# ---------------------------------------------------------------------------
# PR #28 BLOQUANT 5 — Geometric dedup
# ---------------------------------------------------------------------------


def _dedup_geometries(df: gpd.GeoDataFrame) -> gpd.GeoDataFrame:
    """Remove near-duplicate geometries, keeping existing infra over gc_neuf.

    Rounds coordinates to 1 cm, normalizes, and hashes the WKT representation.
    When two geometries match, keeps non-gc_neuf edges first, then shortest.
    """
    if df.empty or len(df) < 2:
        return df

    import hashlib

    def _norm_hash(geom, precision: int = 2):
        """Normalize and hash WKT rounded to *precision* cm."""
        if geom is None or geom.is_empty:
            return None
        try:
            normalized = shapely.normalize(geom)
        except Exception:
            normalized = geom
        # Round coordinates
        fmt = f"{{:.{precision}f}}"
        coords = []
        for c in normalized.coords:
            # Support 2-D and 3-D coords
            parts = [fmt.format(float(c[i])) for i in range(min(len(c), 2))]
            coords.append(" ".join(parts))
        wkt = "LINESTRING (" + ", ".join(coords) + ")"
        return hashlib.md5(wkt.encode()).hexdigest()

    # Sort: existing infra first (not gc_neuf), then shorter length
    df["_is_gc"] = (df["infra_type"] == "gc_neuf") | (df["mode_pose"] == "C0")
    df["_sort_len"] = df["length_m"].fillna(0)
    df = df.sort_values(["_is_gc", "_sort_len"]).reset_index(drop=True)

    seen = set()
    owner_for_hash: dict[str, int] = {}
    keep_idx: list[int] = []
    for i in range(len(df)):
        h = _norm_hash(df.geometry.iloc[i])
        if h is None or h not in seen:
            seen.add(h)
            if h is not None:
                owner_for_hash[h] = i
            keep_idx.append(i)
        elif h is not None and "_used_by_paths" in df.columns:
            owner = owner_for_hash.get(h)
            if owner is not None:
                paths = _path_membership_to_set(df.at[owner, "_used_by_paths"])
                paths.update(_path_membership_to_set(df.at[i, "_used_by_paths"]))
                df.at[owner, "_used_by_paths"] = _serialize_path_membership(paths)

    result = df.iloc[keep_idx].drop(columns=["_is_gc", "_sort_len"]).copy()
    result.reset_index(drop=True, inplace=True)
    return result


# ---------------------------------------------------------------------------
# PR #28 — Mutualisation QA log
# ---------------------------------------------------------------------------


def _log_mutual_qa(before: int, after: int, pa_sro: gpd.GeoDataFrame) -> None:
    """Log mutualisation / dedup statistics (PR #28 BLOQUANT 5)."""
    sro_code = pa_sro.iloc[0].get("sro", "?") if len(pa_sro) > 0 else "?"
    removed = before - after
    log.info(
        "[MUTUAL QA] %s before=%d after=%d duplicates_removed=%d",
        sro_code, before, after, removed,
    )

def _log_gc_qa(df: gpd.GeoDataFrame, pa_sro: gpd.GeoDataFrame) -> None:
    """Log GC neuf bridge diagnostics (PR #27 Part D)."""
    if df.empty:
        return
    sro_code = df.iloc[0].get("sro", "?")
    n_total = len(df)
    n_gc = int((df["infra_type"] == "gc_neuf").sum())
    n_c0 = int((df["mode_pose"] == "C0").sum())
    n_gc_neuf_src = int((df["src"] == "gc_neuf").sum())
    log.info(
        "[GC QA] %s total=%d gc_neuf_infra=%d C0_mode_pose=%d gc_neuf_src=%d",
        sro_code, n_total, n_gc, n_c0, n_gc_neuf_src,
    )


def _log_routing_qa(
    df: gpd.GeoDataFrame,
    sro_code: str,
    *,
    raw_src_lengths: dict[str, float] | None = None,
    raw_src_counts: dict[str, int] | None = None,
    raw_type_lengths: dict[str, float] | None = None,
    converted_ign_to_gc_length_m: float = 0.0,
    # PR #30 — delivery telemetry (defaults preserve PR29 behaviour).
    ign_route_delivered_as_gc_m: float = 0.0,
    ign_route_blocked_m: float = 0.0,
    final_removed_private_gc: int = 0,
    private_crossing_final_count: int = 0,
    private_crossing_final_length_m: float = 0.0,
    existing_connectors_added: int = 0,
    gaps_remaining_estimate: int = 0,
) -> None:
    """PR #29 A2 / amend — log routed length per RAW source family.

    The breakdown is computed from the *raw* counters accumulated during
    path collection (i.e. BEFORE the conversion that maps every
    ``raw_type=="ign_route"`` edge to ``src="gc_neuf"``). Reading
    ``df["src"]`` would always show ``ign_route_length_used_m=0`` since
    by then every IGN edge has been rewritten — exactly the false-zero
    bug Pierre asked us to fix.

    Existing length stays based on the raw ``src`` family (bt/ft/athd/chem)
    so the metric matches the natural infra inventory shown in
    ``filters.build_reusable_infra``.

    A ``[ROUTING WARNING]`` is emitted whenever:
      * the gc_neuf share of total routed length exceeds 50 %, or
      * any ign_route length was actually traversed by Dijkstra
        (``ign_route_used_before_conversion``).

    Both warnings are diagnostics; the final livrable_infra format is
    unchanged (IGN→gc_neuf conversion stays in place per CDC).
    """
    if df is None or df.empty:
        return

    raw_src_lengths = raw_src_lengths or {}
    raw_src_counts = raw_src_counts or {}
    raw_type_lengths = raw_type_lengths or {}

    existing_keys = ("bt", "ft", "athd", "chem")
    existing_length = sum(raw_src_lengths.get(k, 0.0) for k in existing_keys)

    # "True" gc_neuf = pre-existing GC neuf injected via _add_gc_neuf_to_graph
    # OR generated as a runtime bridge. Both end up with raw_src=="gc_neuf"
    # AND raw_type=="gc_neuf"; we take the max so a column missing in one
    # of the two counters does not under-report the share. This is
    # explicitly independent of the IGN-to-C0 conversion.
    true_gc_length = max(
        raw_src_lengths.get("gc_neuf", 0.0),
        raw_type_lengths.get("gc_neuf", 0.0),
    )

    ign_length = raw_type_lengths.get("ign_route", 0.0)
    total = float(df["length_m"].sum()) if "length_m" in df.columns else 0.0

    log.info(
        "[ROUTING QA] sro=%s existing_length_m=%.0f true_gc_neuf_length_m=%.0f "
        "ign_route_length_used_m=%.0f converted_ign_to_gc_length_m=%.0f "
        "total_length_m=%.0f",
        sro_code, existing_length, true_gc_length, ign_length,
        converted_ign_to_gc_length_m, total,
    )

    # Per-raw-src counts: show every family present, including ign_route
    # so a leak is visible at a glance.
    if raw_src_counts:
        src_str = ", ".join(f"{k}={v}" for k, v in sorted(raw_src_counts.items()))
        log.info("[ROUTING QA] sro=%s raw_src_counts %s", sro_code, src_str)

    # PR #30 — delivery telemetry (single line so it greps cleanly).
    log.info(
        "[ROUTING QA] sro=%s ign_route_delivered_as_gc_m=%.0f "
        "ign_route_blocked_m=%.0f final_removed_private_gc=%d "
        "private_crossing_final_count=%d private_crossing_final_length_m=%.0f "
        "existing_connectors_added=%d gaps_remaining_estimate=%d",
        sro_code,
        ign_route_delivered_as_gc_m, ign_route_blocked_m,
        final_removed_private_gc, private_crossing_final_count,
        private_crossing_final_length_m,
        existing_connectors_added, gaps_remaining_estimate,
    )

    # ── WARNINGs ─────────────────────────────────────────────────────────
    # 1) gc_neuf share of the total routed length (CDC threshold = 50 %).
    if total > 0 and (true_gc_length + converted_ign_to_gc_length_m) / total > 0.50:
        log.warning(
            "[ROUTING WARNING] sro=%s high_gc_ratio=%.2f true_gc=%.0fm "
            "converted_ign=%.0fm total=%.0fm",
            sro_code,
            (true_gc_length + converted_ign_to_gc_length_m) / total,
            true_gc_length, converted_ign_to_gc_length_m, total,
        )
    # 2) Any IGN length actually traversed by Dijkstra is suspicious — even
    #    though the output converts it to C0/gc_neuf, a non-zero value here
    #    means the routing relied on IGN despite the ×30 weight.
    if ign_length > 0:
        log.warning(
            "[ROUTING WARNING] sro=%s ign_route_used_before_conversion=%.0fm",
            sro_code, ign_length,
        )


def _log_infra_qa(df: gpd.GeoDataFrame, pa_sro: gpd.GeoDataFrame) -> None:
    """Log quality-assurance breakdown for livrable_infra (PR #26)."""
    if df.empty:
        log.warning("[INFRA QA] livrable_infra vide")
        return

    sro_code = df.iloc[0].get("sro", "?") if len(df) > 0 else "?"
    n_total = len(df)

    # coalesce style_key = statut + mode_pose (PR #26 amend: replaces empty_statut)
    df_sk = df.copy()
    df_sk["statut_str"] = df_sk["statut"].fillna("").astype(str)
    df_sk["mp_str"] = df_sk["mode_pose"].fillna("").astype(str)
    df_sk["style_key"] = df_sk["statut_str"].str.cat(df_sk["mp_str"], sep="")

    n_empty_style = int((df_sk["style_key"] == "").sum())

    # style_key breakdown
    sk_counts = df_sk["style_key"].value_counts().to_dict()
    sk_str = ", ".join(f"{k}={v}" for k, v in sorted(sk_counts.items()))
    log.info("[INFRA QA] %s style_key: %s", sro_code, sk_str or "(vide)")

    # infra_type breakdown
    it_counts = df["infra_type"].value_counts().to_dict()
    it_str = ", ".join(f"{k}={v}" for k, v in sorted(it_counts.items()))
    log.info("[INFRA QA] %s infra_type: %s", sro_code, it_str or "(vide)")

    # Problematic attributes
    n_ign_route = int((df["src"] == "ign_route").sum())
    n_gc_neuf_runtime = int((df["src"] == "gc_neuf_runtime").sum())
    n_statut_none = int(df["statut"].isna().sum())

    total_length = float(df["length_m"].sum()) if "length_m" in df.columns else 0.0

    log.info(
        "[INFRA QA] %s total=%d features, %.0f m | "
        "empty_style_key=%d src_ign_route=%d src_gc_neuf_runtime=%d statut_null=%d",
        sro_code, n_total, total_length,
        n_empty_style, n_ign_route, n_gc_neuf_runtime, n_statut_none,
    )
