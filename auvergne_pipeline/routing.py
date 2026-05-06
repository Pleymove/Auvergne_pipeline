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
from typing import List, Optional

import geopandas as gpd
import networkx as nx
import numpy as np
import pandas as pd
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
EDGE_KEY_SEP = "::"

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
) -> None:
    """Add GC neuf C0 edges into graph, snapping endpoints to nearest nodes.

    PR #21: endpoints are snapped to existing graph nodes first so that
    Dijkstra can traverse through GC neuf segments. If no node within
    snap_tol, the raw _point_key is added as a new isolated node.
    """
    if gc_neuf is None or gc_neuf.empty:
        return

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
        coords = list(line.coords)
        if len(coords) < 2:
            continue

        pk_a = _snap_endpoint(coords[0])
        pk_b = _snap_endpoint(coords[-1])

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
            "geometry": row.geometry
            if row.geometry is not None and not row.geometry.is_empty
            else LineString([(pk_a[0], pk_a[1]), (pk_b[0], pk_b[1])]),
        }
        for col in ("sro_code", "pa_id", "pb_id"):
            if col in gc_neuf.columns:
                attrs[col] = row.get(col)
        G.add_edge(pk_a, pk_b, **attrs)


# ---------------------------------------------------------------------------


def _bridge_components_with_gc_neuf(
    G: nx.Graph,
    pa_node: tuple[float, float],
    pb_node: tuple[float, float],
    flag_collector=None,
    max_bridge_length_m: float = 50.0,
) -> bool:
    """If pa_node and pb_node belong to different connected components
    AND the direct distance is within *max_bridge_length_m*, add a short
    GC neuf C0 bridge and return True.

    Otherwise only flag the disconnection — NEVER create a long diagonal
    across private parcels (PR #26: CDC compliance).

    PR #22: in rural areas the welded graph may still be disconnected
    by short gaps. Bridging resolves those gaps ONLY when the direct
    distance is small enough to be a plausible public-domain connector.
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

    if direct_length > max_bridge_length_m:
        # Too far — flag instead of inventing a diagonal
        if flag_collector is not None:
            flag_collector.add(
                "GC_NEUF_ROUTING_IMPOSSIBLE",
                target_url=f"PA=({pa_node[0]:.0f},{pa_node[1]:.0f}) PB=({pb_node[0]:.0f},{pb_node[1]:.0f})",
                message=f"Pont GC neuf impossible — distance {direct_length:.0f}m > seuil {max_bridge_length_m}m",
            )
        return False

    # Short connector, geometrically plausible as a public-domain link
    bridge_geom = LineString([
        (pa_node[0], pa_node[1]),
        (pb_node[0], pb_node[1]),
    ])
    G.add_edge(
        pa_node, pb_node,
        length=direct_length,
        type="gc_neuf",
        statut="",
        mode_pose="C0",
        src="gc_neuf",
        infra_type="gc_neuf",
        geometry=bridge_geom,
    )
    if flag_collector is not None:
        flag_collector.add(
            "GC_NEUF_GENERE_DIJKSTRA",
            target_url=f"PA=({pa_node[0]:.0f},{pa_node[1]:.0f}) PB=({pb_node[0]:.0f},{pb_node[1]:.0f})",
            message=f"Pont GC neuf C0 short-range, length={direct_length:.0f}m",
        )
    return True


# ---------------------------------------------------------------------------


def route_pa_to_pb(
    pa_sro: gpd.GeoDataFrame,
    pb_sro: gpd.GeoDataFrame,
    infra_filtered: gpd.GeoDataFrame,
    ign_routes: gpd.GeoDataFrame,
    flag_collector=None,
    gc_neuf: gpd.GeoDataFrame | None = None,
) -> gpd.GeoDataFrame:
    """Route all (PA, PB) pairs via Dijkstra and return traversed edges.

    *gc_neuf* (optional) — new C0 edges generated by pb_fictif, injected
    into the routing graph so they can be traversed by PA→PB paths.

    Returns a GeoDataFrame of LineStrings with columns:
    ``sro``, ``pa_id``, ``pb_id``, ``statut``, ``mode_pose``, ``infra_type``,
    ``src``, ``length_m``.

    Edges are deduplicated via _edge_key so a trunk shared by multiple PBs
    of the same PA appears only once in the output (PR #23 Feature D).
    """
    if pa_sro is None or pa_sro.empty or pb_sro is None or pb_sro.empty:
        return gpd.GeoDataFrame(geometry=[], crs=config.PROJECT_CRS)

    # Build combined graph
    G = _build_graph(infra_filtered, ign_routes, snap_tol=SNAP_TOLERANCE_M)

    # Inject GC neuf C0 edges (snaps endpoints to existing nodes first)
    if gc_neuf is not None and not gc_neuf.empty:
        _add_gc_neuf_to_graph(G, gc_neuf, snap_tol=SNAP_TOLERANCE_M)

    if G.number_of_nodes() == 0:
        return gpd.GeoDataFrame(geometry=[], crs=config.PROJECT_CRS)

    # Spec C (PR #22): diagnostic on connected components
    n_cc = nx.number_connected_components(G)
    if n_cc > 0:
        largest_cc = max(nx.connected_components(G), key=len)
        log.info(
            "[ROUTING] %d composantes connexes (plus grande = %d noeuds / %d total)",
            n_cc, len(largest_cc), G.number_of_nodes(),
        )

    edges_out: dict[str, dict] = {}  # PR #23 Feature D: deduplicate via edge key

    # Build STRtree indices for fast PA/PB snapping (PR #21)
    node_tree = None
    edge_tree = None
    nodes_list = list(G.nodes())
    node_coords_to_geom = {n: Point(n[0], n[1]) for n in nodes_list}
    node_geoms = [node_coords_to_geom[n] for n in nodes_list]
    if node_geoms:
        node_tree = STRtree(node_geoms)

    # Build edge list + STRtree for edge projection
    edge_list: list[tuple] = []
    edge_geoms: list = []
    for u, v, data in G.edges(data=True):
        line = LineString([(u[0], u[1]), (v[0], v[1])])
        edge_list.append((u, v, data))
        edge_geoms.append(line)
    if edge_geoms:
        edge_tree = STRtree(edge_geoms)

    def _snap(_pt: Point) -> Optional[tuple[float, float]]:
        # 0) Exact key
        pk = _point_key(_pt)
        if pk in G:
            return pk

        # 1) Nearest node
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
                        # Split the original LineString at the projection point
                        proj_dist = orig_geom.project(best_proj)
                        seg_a = shapely.ops.substring(orig_geom, 0, proj_dist)
                        seg_b = shapely.ops.substring(orig_geom, proj_dist, orig_geom.length)
                    else:
                        seg_a = LineString([(u[0], u[1]), (best_proj.x, best_proj.y)])
                        seg_b = LineString([(best_proj.x, best_proj.y), (v[0], v[1])])
                    G.add_edge(u, new_key, length=d1, geometry=seg_a, **extra)
                    G.add_edge(new_key, v, length=d2, geometry=seg_b, **extra)
                if new_key in G:
                    return new_key

        return None

    # ── For each PA, route all PA → PB (PR #23 Feature D: single-source Dijkstra per PA) ──
    for _, pa in pa_sro.iterrows():
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
                    G, source=pa_node, weight="length"
                )
                return paths
            except (nx.NetworkXError, KeyError):
                return {}

        _paths = _dijkstra_tree()

        for pb, pb_node in pb_snapped:
            pb_id = pb.get("pb_id", f"pb#{pb.name}")

            # Check if PB reachable from PA in current tree
            if pb_node in _paths:
                path = _paths[pb_node]
            else:
                # Spec B (PR #22): bridge components with GC neuf C0
                bridged = _bridge_components_with_gc_neuf(
                    G, pa_node, pb_node, flag_collector=flag_collector
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

            # Collect edges along the path (PR #26: use stored geometry, fix attribs)
            for i in range(len(path) - 1):
                u, v = path[i], path[i + 1]
                edge_data = G.get_edge_data(u, v)
                if edge_data is None:
                    continue
                ekey = _edge_key(u, v)
                if ekey not in edges_out:
                    # ── Resolve attributes with QML / CDC compliance ──────
                    raw_src = edge_data.get("src", edge_data.get("type", ""))
                    raw_type = edge_data.get("type", "")
                    raw_infra = edge_data.get("infra_type", "")

                    # PR #26: gc_neuf_runtime → gc_neuf (must not leak into GPKG)
                    if raw_src == "gc_neuf_runtime":
                        raw_src = "gc_neuf"

                    # PR #26: pure IGN edges delivered as infra must become C0
                    if raw_type == "ign_route" and raw_infra != "gc_neuf":
                        mode_pose = "C0"
                        infra_type = "gc_neuf"
                        src = "gc_neuf"
                    elif raw_src in ("gc_neuf", "gc_neuf_runtime"):
                        mode_pose = "C0"
                        infra_type = "gc_neuf"
                        src = "gc_neuf"
                    else:
                        mode_pose = edge_data.get("mode_pose", "")
                        infra_type = raw_infra or raw_src or raw_type
                        src = raw_src or raw_type

                    # ── Use stored geometry, fallback to naive reconstruction ──
                    stored_geom = edge_data.get("geometry")
                    if stored_geom is not None and isinstance(stored_geom, LineString) and not stored_geom.is_empty:
                        out_geom = stored_geom
                    else:
                        out_geom = LineString([(u[0], u[1]), (v[0], v[1])])

                    # ── Normalize statut: never None in the GPKG (PR #26 amend)
                    out_statut = edge_data.get("statut")
                    if out_statut is None:
                        out_statut = ""

                    edges_out[ekey] = {
                        "sro": sro,
                        "pa_id": pa_id,
                        "pb_id": pb_id,
                        "statut": out_statut,
                        "mode_pose": mode_pose,
                        "infra_type": infra_type,
                        "src": src,
                        "length_m": edge_data.get("length", 0.0),
                        "geometry": out_geom,
                    }

    if not edges_out:
        return gpd.GeoDataFrame(geometry=[], crs=config.PROJECT_CRS)

    result = gpd.GeoDataFrame(list(edges_out.values()), geometry="geometry", crs=config.PROJECT_CRS)

    # ── PR #26 [INFRA QA] diagnostic logs ───────────────────────────────
    _log_infra_qa(result, pa_sro)

    return result


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
