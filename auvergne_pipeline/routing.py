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
        for _, row in gdf.iterrows():
            for line in _explode_to_linestrings(row.geometry):
                coords = list(line.coords)
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
            "statut": "",
            "mode_pose": "C0",
            "src": "gc_neuf",
            "infra_type": "gc_neuf",
            "geometry": gc_geom
            if not gc_geom.is_empty
            else LineString([(pk_a[0], pk_a[1]), (pk_b[0], pk_b[1])]),
            # PR #33: gc_neuf edges are VIRTUAL — routable but NOT delivered
            "virtual": True,
            "deliverable": False,
            "virtual_reason": "gc_neuf_c0",
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

    # PR #33: micro bridge accepted but VIRTUAL (not delivered).
    # PR #34 amend (Bloqueur 1): this edge is added AFTER the prepare_weights
    # loop, so we must set ``_routing_weight`` here ourselves. Otherwise
    # NetworkX falls back to its default behaviour (None / implicit weight)
    # which can bias Dijkstra paths through this virtual bridge.
    bridge_attrs = {
        "length": direct_length,
        "type": "gc_neuf",
        "statut": "",
        "mode_pose": "C0",
        "src": "gc_neuf",
        "infra_type": "gc_neuf",
        "geometry": bridge_geom,
        "virtual": True,
        "deliverable": False,
        "virtual_reason": "micro_bridge",
    }
    bridge_attrs["_routing_weight"] = _routing_weight_for(bridge_attrs)
    G.add_edge(pa_node, pb_node, **bridge_attrs)
    if flag_collector is not None:
        flag_collector.add(
            "MICRO_BRIDGE_CREATED_VIRTUAL",
            target_url=f"PA=({pa_node[0]:.0f},{pa_node[1]:.0f}) PB=({pb_node[0]:.0f},{pb_node[1]:.0f})",
            message=f"Micro bridge virtuel cree, length={direct_length:.1f}m (non livre)",
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

    # PR #29 A1: hierarchical routing weights — prefer existing infra over
    # gc_neuf, and gc_neuf over IGN routes. The factors are documented
    # at module top (WEIGHT_FACTOR_*).
    t0 = time.perf_counter()
    for u, v, data in G.edges(data=True):
        data["_routing_weight"] = _routing_weight_for(data)
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
    straight_connector_count = 0
    straight_connector_length_m = 0.0

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

    # ── For each PA, route all PA → PB (PR #23 Feature D: single-source Dijkstra per PA) ──
    t0_dijkstra = time.perf_counter()
    pa_count = 0
    pb_count = 0
    for _, pa in pa_sro.iterrows():
        pa_count += 1
        pa_id = pa.get("id_metier", f"pa#{pa.name}")
        sro = pa.get("sro", "?")
        pa_geom = pa.geometry

        pa_node = _snap(pa_geom)
        if pa_node is None:
            if flag_collector is not None:
                flag_collector.add(
                    "PA_PB_DECONNECTES",
                    target_url=pa_id,
                    message="PA non connectable au graphe",
                )
            continue

        pb4pa = pb_sro[pb_sro.get("pa_id", pd.Series(dtype=str)) == pa_id]
        if pb4pa.empty:
            continue

        # ── Snap all PBs first (may mutate G via edge projection) ─────
        pb_snapped: list[tuple] = []  # (pb_row, pb_node, pb_id, pb_geom)
        for _, pb in pb4pa.iterrows():
            pb_node = _snap(pb.geometry)
            if pb_node is None:
                # PB unreachable — flag immediately
                if flag_collector is not None:
                    flag_collector.add(
                        "PA_PB_DECONNECTES",
                        target_url=pb.get("pb_id", f"pb#{pb.name}"),
                        message="PB non connectable au graphe",
                    )
                continue
            pb_snapped.append((pb, pb_node))

        if not pb_snapped:
            continue

        # ── One Dijkstra tree per PA (after all PB snaps) ──────────────
        def _dijkstra_tree():
            try:
                _, paths = nx.single_source_dijkstra(
                    G, source=pa_node, weight="_routing_weight"  # PR #28 BLOQUANT 5
                )
                return paths
            except (nx.NetworkXError, KeyError):
                return {}

        _paths = _dijkstra_tree()

        for pb, pb_node in pb_snapped:
            pb_count += 1
            pb_id = pb.get("pb_id", f"pb#{pb.name}")

            # Check if PB reachable from PA in current tree
            if pb_node in _paths:
                path = _paths[pb_node]
            else:
                # Spec B (PR #22): bridge components with GC neuf C0
                bridged = _bridge_components_with_gc_neuf(
                    G, pa_node, pb_node,
                    flag_collector=flag_collector,
                    public_area=public_area,             # PR #27
                    public_area_safe=public_area_safe,   # PR #29 B1
                )
                if bridged:
                    # Recompute tree after bridge insertion
                    _paths = _dijkstra_tree()
                    if pb_node in _paths:
                        path = _paths[pb_node]
                    else:
                        if flag_collector is not None:
                            flag_collector.add(
                                "PA_PB_DECONNECTES",
                                target_url=pa_id,
                                message=f"Pas de chemin vers {pb_id} meme apres pont GC neuf",
                            )
                        continue
                else:
                    if flag_collector is not None:
                        flag_collector.add(
                            "PA_PB_DECONNECTES",
                            target_url=pa_id,
                            message=f"Pas de chemin vers {pb_id}",
                        )
                    continue

            # ── PR #34 amend (v3) — PATH-LEVEL deliverability validation.
            #
            # Previous behaviour: walk the path edge-by-edge, and for each
            # non-deliverable edge (virtual=True, IGN over per-edge or
            # cumulative cap, IGN outside public domain) silently ``continue``
            # — but commit the other edges of the same PA→PB. The output
            # ended up structurally discontinuous: the routing log saw the
            # PA→PB as connected (a Dijkstra path existed), yet the livrable
            # had visible holes where blocked edges had been dropped.
            #
            # New rule (brief Notion 2026-05): the delivered graph MUST equal
            # the routed graph for a given PA→PB pair. If any edge along the
            # path is not deliverable, the WHOLE PB path is rejected — no
            # partial geometry is emitted — and the pair is flagged as
            # disconnected. This trades raw connectivity for honest livrables
            # without straight-line patches.
            proposed_edges: list[tuple] = []
            path_ign_delivered_pending_m = 0.0
            path_deliverable = True
            path_rejection_reason: str | None = None

            for i in range(len(path) - 1):
                u, v = path[i], path[i + 1]
                edge_data = G.get_edge_data(u, v)
                if edge_data is None:
                    # Should not happen for a Dijkstra path — be defensive.
                    path_deliverable = False
                    path_rejection_reason = "missing_edge_data"
                    break

                # ── Virtual / explicitly-non-deliverable edges abort the path.
                # PR #33 introduced these to keep Dijkstra connectivity (gc_neuf
                # injected, endpoint→line micro-snap, component bridges) without
                # delivering them. If any sit on the chosen path, the PA→PB is
                # only routable through a virtual hop and therefore cannot be
                # delivered as a continuous chain of real infra.
                if edge_data.get("virtual") or not edge_data.get("deliverable", True):
                    virtual_edges_blocked_count += 1
                    path_deliverable = False
                    path_rejection_reason = "virtual_edge_in_path"
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

                # ── IGN-as-C0 path-level gate (PR #30 + PR #34 amend v3).
                # Per-edge checks (length, public area) AND the SRO-wide
                # cumulative cap must ALL pass for the path to be eligible.
                # The cap considers already-committed deliveries plus what
                # this path would add — so paths that would push past the
                # budget are dropped before any of their IGN edges leak in.
                if raw_type == "ign_route" and raw_infra != "gc_neuf":
                    is_short = raw_length <= IGN_DELIVERY_MAX_LENGTH_M
                    if delivery_public_area_safe is not None:
                        is_in_public = delivery_public_area_safe.covers(out_geom)
                    else:
                        is_in_public = True
                    within_budget = (
                        ign_route_delivered_as_gc_m
                        + path_ign_delivered_pending_m
                        + raw_length
                        <= MAX_IGN_DELIVERED_PER_SRO_M
                    )

                    if not (is_short and is_in_public and within_budget):
                        ign_route_blocked_m += raw_length
                        ign_route_blocked_count += 1
                        if is_short and is_in_public and not within_budget:
                            ign_cap_hit_count += 1
                            path_rejection_reason = "ign_cap_exceeded"
                        elif not is_short:
                            path_rejection_reason = "ign_segment_too_long"
                        else:
                            path_rejection_reason = "ign_outside_public"
                        if (
                            not _ign_blocked_flag_added
                            and flag_collector is not None
                        ):
                            flag_collector.add(
                                "IGN_ROUTE_BLOCKED",
                                target_url=sro_code_log,
                                message=(
                                    "Tronçon IGN routé non livré (trop long, "
                                    "hors domaine public strict ou budget "
                                    "cumulatif dépassé)"
                                ),
                            )
                            _ign_blocked_flag_added = True
                        path_deliverable = False
                        break

                    path_ign_delivered_pending_m += raw_length

                proposed_edges.append(
                    (u, v, edge_data, raw_src, raw_type, raw_infra,
                     raw_length, out_geom, geom_is_real)
                )

            if not path_deliverable:
                if flag_collector is not None:
                    flag_collector.add(
                        "PA_PB_PATH_NON_DELIVERABLE",
                        target_url=f"{pa_id}->{pb_id}",
                        message=(
                            f"Chemin Dijkstra {pa_id}->{pb_id} non livrable "
                            f"({path_rejection_reason}). PB compté comme "
                            "déconnecté pour livrable_infra."
                        ),
                    )
                # PA→PB stays out of edges_out entirely; pr31_stats will
                # later see no delivered path between this PA and PB.
                continue

            # ── Commit path atomically ───────────────────────────────────
            for (u, v, edge_data, raw_src, raw_type, raw_infra,
                 raw_length, out_geom, geom_is_real) in proposed_edges:
                # PR #29 amend — accumulate raw-source telemetry BEFORE
                # any conversion. ign_route_length_used_m is computed
                # from raw_type, not from the converted output src.
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

                if raw_type == "ign_route" and raw_infra != "gc_neuf":
                    mode_pose = "C0"
                    infra_type = "gc_neuf"
                    src = "gc_neuf"
                    converted_ign_to_gc_length_m += raw_length
                    ign_route_delivered_as_gc_m += raw_length
                elif raw_src in ("gc_neuf", "gc_neuf_runtime"):
                    mode_pose = "C0"
                    infra_type = "gc_neuf"
                    src = "gc_neuf"
                else:
                    mode_pose = edge_data.get("mode_pose", "")
                    infra_type = raw_infra or raw_src or raw_type
                    src = raw_src or raw_type

                out_statut = edge_data.get("statut")
                if out_statut is None:
                    out_statut = ""

                ekey = _edge_key(u, v)
                if ekey in edges_out:
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

    perf["dijkstra_total"] = time.perf_counter() - t0_dijkstra

    if not edges_out:
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
            "ign_cap_hit=%d c0_without_source_geometry=0",
            sro_code_log, pb_count,
            straight_connector_count, straight_connector_length_m,
            ign_cap_hit_count,
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
    if delivery_public_area_safe is not None and not result.empty:
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
    result, pr31_stats = _lt.finalize_livrable_topology(
        result, pa_sro, pb_sro, sro_code_log,
        delivery_public_area_safe=delivery_public_area_safe,
        flag_collector=flag_collector,
    )
    perf["pr31_topology"] = time.perf_counter() - t0

    # ── PR #26 [INFRA QA] diagnostic logs ───────────────────────────────
    _log_infra_qa(result, pa_sro)

    # ── PR #27 Part D [GC QA] bridge diagnostics ─────────────────────────
    _log_gc_qa(result, pa_sro)

    # ── PR #28 [MUTUAL QA] mutualisation diagnostics ─────────────────────
    _log_mutual_qa(n_before_dedup, n_after_dedup, pa_sro)

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
        c0_mask = result["infra_type"] == "gc_neuf"
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

    log.info(
        "[FINAL TOPO QA] sro=%s connected=%d disconnected=%d pa_pb_connected_ratio=%.2f "
        "straight_connectors=%d straight_connector_length_m=%.0f "
        "virtual_delivered=%d ign_cap_hit=%d c0_without_source_geometry=%d",
        sro_code_log, pa_pb_connected, pa_pb_disconnected, pa_pb_ratio,
        straight_connector_count, straight_connector_length_m,
        virtual_edges_delivered_count,
        ign_cap_hit_count,
        c0_without_source_geom,
    )

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
    keep_idx: list[int] = []
    for i in range(len(df)):
        h = _norm_hash(df.geometry.iloc[i])
        if h is None or h not in seen:
            seen.add(h)
            keep_idx.append(i)

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
