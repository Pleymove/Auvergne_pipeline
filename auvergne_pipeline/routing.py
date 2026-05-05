"""Routing PA -> PB on combined graph (public infra + IGN routes).

Builds a NetworkX graph from the union of:
  - Filtered public infrastructure (``filters.build_reusable_infra``)
  - IGN BD TOPO routes (``ign_routes.load_ign_routes_for_sro``)

Then snaps PA/PB endpoints onto the graph, runs Dijkstra for each
(PA, PB) pair, and returns the traversed edges tagged with
``statut`` / ``mode_pose``.
"""

from __future__ import annotations

import logging
from typing import List, Optional

import geopandas as gpd
import networkx as nx
import numpy as np
import pandas as pd
from shapely.geometry import LineString, Point
from shapely.ops import nearest_points, split, snap

from . import config

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
SNAP_TOLERANCE_M = 0.5
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
# Helpers ────────────────────────────────────────────────────────────────
# ---------------------------------------------------------------------------

def _edge_key(u, v) -> str:
    """Deterministic key for an undirected edge."""
    a, b = (u, v) if u[0] < v[0] or (u[0] == v[0] and u[1] < v[1]) else (v, u)
    return f"{a[0]:.6f},{a[1]:.6f}{EDGE_KEY_SEP}{b[0]:.6f},{b[1]:.6f}"


def _point_key(pt: Point) -> tuple[float, float]:
    """Rounded coordinate key for a node."""
    return (round(pt.x, 6), round(pt.y, 6))


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
                    G.add_edge(
                        _point_key(Point(a)),
                        _point_key(Point(b)),
                        length=length,
                        **attrs,
                        **{k: row.get(k) for k in ("statut", "mode_pose", "src", "sro_code")
                           if k in gdf.columns},
                    )

    _add_edges(infra, {"type": "infra"})
    _add_edges(ign_routes, {"type": "ign_route"})

    if G.number_of_nodes() == 0:
        log.warning("[ROUTING] Graphe vide — pas d'aretes ni infra ni IGN")

    log.info("[ROUTING] Graphe: %d noeuds, %d aretes", G.number_of_nodes(), G.number_of_edges())
    return G


def _snap_to_graph(
    pt: Point,
    G: nx.Graph,
    snap_tol: float = SNAP_TOLERANCE_M,
) -> Optional[tuple[float, float]]:
    """Snap a point to the nearest graph node within tolerance.

    If no node is within tolerance, project onto the nearest edge, split it,
    and return the coords of the new intermediate node.
    """
    # 1) Exact node lookup
    pk = _point_key(pt)
    if pk in G:
        return pk

    # 2) Nearest node within tolerance
    nodes = np.array([(x, y) for x, y in G.nodes()])
    if len(nodes) == 0:
        return None

    dists = np.linalg.norm(nodes - np.array([[pt.x, pt.y]]), axis=1)
    i_min = int(dists.argmin())
    if dists[i_min] <= snap_tol:
        return (float(nodes[i_min, 0]), float(nodes[i_min, 1]))

    # 3) Project onto nearest edge
    best_edge = None
    best_param = 0.0
    best_dist = float("inf")

    for u, v, data in G.edges(data=True):
        ux, uy = u
        vx, vy = v
        dx, dy = vx - ux, vy - uy
        edge_len_sq = dx * dx + dy * dy
        if edge_len_sq < 1e-18:
            # degenerate — skip
            proj_x, proj_y = ux, uy
        else:
            t = max(0.0, min(1.0, ((pt.x - ux) * dx + (pt.y - uy) * dy) / edge_len_sq))
            proj_x, proj_y = ux + t * dx, uy + t * dy

        d = Point(pt.x, pt.y).distance(Point(proj_x, proj_y))
        if d < best_dist:
            best_dist = d
            best_edge = (u, v, data)
            best_param = (
                Point(proj_x, proj_y).distance(Point(u[0], u[1]))
                / math.sqrt(edge_len_sq)
                if edge_len_sq > 1e-18
                else 0.0
            )

    if best_edge is None or best_dist > snap_tol * 100:
        return None

    # Split the edge at the projected point
    u, v, data = best_edge
    new_key = _point_key(Point(pt.x, pt.y))

    # Remove old edge, add two new ones
    G.remove_edge(u, v)
    d1 = Point(u[0], u[1]).distance(pt)
    d2 = Point(v[0], v[1]).distance(pt)
    total = d1 + d2 or 1.0
    G.add_edge(u, new_key, length=d1, **{k: data[k] for k in data if k != "length"})
    G.add_edge(new_key, v, length=d2, **{k: data[k] for k in data if k != "length"})

    return new_key


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def route_pa_to_pb(
    pa_sro: gpd.GeoDataFrame,
    pb_sro: gpd.GeoDataFrame,
    infra_filtered: gpd.GeoDataFrame,
    ign_routes: gpd.GeoDataFrame,
    flag_collector=None,
) -> gpd.GeoDataFrame:
    """Route all (PA, PB) pairs via Dijkstra and return traversed edges.

    Returns a GeoDataFrame of LineStrings with columns:
    ``sro``, ``pa_id``, ``pb_id``, ``statut``, ``mode_pose``, ``src``, ``length_m``.
    """
    import math

    if pa_sro is None or pa_sro.empty or pb_sro is None or pb_sro.empty:
        return gpd.GeoDataFrame(geometry=[], crs=config.PROJECT_CRS)

    # Build combined graph
    G = _build_graph(infra_filtered, ign_routes, snap_tol=SNAP_TOLERANCE_M)
    if G.number_of_nodes() == 0:
        return gpd.GeoDataFrame(geometry=[], crs=config.PROJECT_CRS)

    edges_out: List[dict] = []

    # ── For each SRO, route all PA → PB ──────────────────────────────
    for _, pa in pa_sro.iterrows():
        pa_id = pa.get("id_metier", f"pa#{pa.name}")
        sro = pa.get("sro", "?")
        pa_geom = pa.geometry

        # Snap PA to graph
        pa_node = _snap_to_graph(pa_geom, G)
        if pa_node is None:
            if flag_collector is not None:
                flag_collector.add(
                    "PA_PB_DECONNECTES",
                    target_url=pa_id,
                    message="PA non connectable au graphe",
                )
            continue

        # PBs for this PA
        pb4pa = pb_sro[pb_sro.get("pa_id", pd.Series(dtype=str)) == pa_id]
        if pb4pa.empty:
            continue

        for _, pb in pb4pa.iterrows():
            pb_id = pb.get("pb_id", f"pb#{pb.name}")
            pb_geom = pb.geometry

            pb_node = _snap_to_graph(pb_geom, G)
            if pb_node is None:
                if flag_collector is not None:
                    flag_collector.add(
                        "PA_PB_DECONNECTES",
                        target_url=pb_id,
                        message="PB non connectable au graphe",
                    )
                continue

            # Dijkstra
            try:
                path = nx.shortest_path(G, source=pa_node, target=pb_node, weight="length")
            except nx.NetworkXNoPath:
                if flag_collector is not None:
                    flag_collector.add(
                        "PA_PB_DECONNECTES",
                        target_url=pa_id,
                        message=f"Pas de chemin vers {pb_id}",
                    )
                continue

            # Collect edges along the path
            for i in range(len(path) - 1):
                u, v = path[i], path[i + 1]
                edge_data = G.get_edge_data(u, v)
                if edge_data is None:
                    continue
                edges_out.append({
                    "sro": sro,
                    "pa_id": pa_id,
                    "pb_id": pb_id,
                    "statut": edge_data.get("statut", ""),
                    "mode_pose": edge_data.get("mode_pose", ""),
                    "src": edge_data.get("src", edge_data.get("type", "")),
                    "length_m": edge_data.get("length", 0.0),
                    "geometry": LineString([Point(u[0], u[1]), Point(v[0], v[1])]),
                })

    if not edges_out:
        return gpd.GeoDataFrame(geometry=[], crs=config.PROJECT_CRS)

    return gpd.GeoDataFrame(edges_out, geometry="geometry", crs=config.PROJECT_CRS)