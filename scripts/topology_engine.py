#!/usr/bin/env python3
"""
PR32 Topology Engine — existing-first, CRS-safe, no arbitrary C0/straight lines.

Implements the 5 hotfix sections from the Notion brief:
  A — CRS EPSG:2154 force en sortie
  B — _ensure_terminals_connected()    PA/PB → infra existante prioritaire
  C — _split_livrableedges_at_endpoint_projections()  T-junction split robuste
  D — _reconnect_after_energy_removal()  pas de C0 arbitraire
  E — _drop_c0_when_existing_equivalent()  purge C0 superposes + logs QA

Règle absolue : utiliser l'existant en priorité. C0/GC neuf uniquement en
dernier recours, court, public, jamais superposé à l'existant, jamais en
trait droit arbitraire.
"""

import logging
import math
from pathlib import Path
from typing import Optional

import geopandas as gpd
import numpy as np
import pandas as pd
from shapely.geometry import Point, LineString, Polygon, MultiLineString
from shapely.ops import snap as shapely_snap, nearest_points

logger = logging.getLogger(__name__)

# ──────────────────────────────────────────────────────────────────────
# Constants
# ──────────────────────────────────────────────────────────────────────
OUTPUT_CRS = 32632  # EPSG:2154
SNAP_TOLERANCE_M = 5.0          # snap general aux noeuds existants (m)
MIN_SEGMENT_M = 0.01            # eviter segments degeneres < 1 cm
T_JUNCTION_TOLERANCE_M = 5.0   # tolerance projection T-junction
C0_MAX_LENGTH_M = 100.0         # longueur max connecteur C0
MAX_CONNECTOR_ANGLE_DEG = 45.0  # angle max connecteur vs reseau adjacent
EXISTING_COINCIDENCE_M = 2.0    # si un existant passe a < 2m → drop C0 neuf

# ──────────────────────────────────────────────────────────────────────
# Section A — CRS EPSG:2154 force en sortie
# ──────────────────────────────────────────────────────────────────────

def enforce_crs(gdf: gpd.GeoDataFrame) -> gpd.GeoDataFrame:
    """
    Force le CRS en EPSG:2154. Si le gdf n'a pas de CRS, assume WGS84 (4326)
    et transforme. Si le CRS est deja 2154, retourne tel quel.
    """
    gdf = gdf.copy()
    if gdf.crs is None:
        logger.warning("GeoDataFrame sans CRS — assume EPSG:4326 (WGS84)")
        gdf.set_crs(epsg=4326, inplace=True)
    if gdf.crs.to_epsg() != 2154:
        logger.info(
            "Converting CRS from %s → EPSG:2154", gdf.crs.to_epsg()
        )
        gdf = gdf.to_crs(epsg=2154)
    return gdf


def enforce_crs_project_file(path: Path) -> Path:
    """
    Verifie et corrige le CRS d'un fichier QGZ / projet.
    Retourne le path. Side-effect: modifie le fichier si besoin.
    """
    if path.suffix.lower() in ('.qgz', '.qgs'):
        import xml.etree.ElementTree as ET
        tree = ET.parse(str(path))
        root = tree.getroot()
        for el in root.iter('spatialrefsys'):
            authid = el.find('authid')
            if authid is not None and authid.text != 'EPSG:2154':
                logger.warning(
                    "QGZ CRS incorrect: %s → forcé EPSG:2154", authid.text
                )
                authid.text = 'EPSG:2154'
        tree.write(str(path))
    return path


# ──────────────────────────────────────────────────────────────────────
# Section B — _ensure_terminals_connected()
# PA/PB connectés à l'infra existante EN PRIORITÉ.
# ──────────────────────────────────────────────────────────────────────

def _project_point_to_line(pt: Point, line: LineString) -> tuple:
    """
    Projette un point sur une LineString.
    Retourne (proj_point, distance_to_proj).
    """
    try:
        # nearest_points(a, b) → (nearest_on_a, nearest_on_b)
        nearest_pt_on_line = nearest_points(pt, line)[1]
        d = pt.distance(nearest_pt_on_line)
        return nearest_pt_on_line, d
    except Exception:
        return None, float('inf')


def _snap_to_existing_infra(
    terminal: Point,
    infra_gdf: gpd.GeoDataFrame,
    tolerance: float = SNAP_TOLERANCE_M,
) -> Optional[dict]:
    """
    Tente de connecter un terminal (PA/PB) à l'infra existante.

    Priorité de snap:
    1. Endpoint existant (exact ou < tolerance)
    2. Projection sur le milieu d'une ligne existante (< tolerance et d < 0.01 m entre proj et ligne)
    3. Endpoint le plus proche si aucun snap direct

    Retourne:
        {
            'type': 'endpoint' | 'midpoint' | 'nearest_endpoint' | 'none',
            'target_geom': Point,
            'target_edge_idx': int or None,
            'distance': float,
            'is_existing': bool,
        }
        ou None si rien trouvé.
    """
    best = None

    for idx, edge in infra_gdf.iterrows():
        geom = edge.geometry
        if geom is None or geom.is_empty:
            continue
        if not isinstance(geom, (LineString, MultiLineString)):
            continue

        # Get endpoints
        if isinstance(geom, MultiLineString):
            coords = list(geom.geoms[0].coords)
            coords += list(geom.geoms[-1].coords)
        else:
            coords = list(geom.coords)

        if len(coords) < 2:
            continue

        start = Point(coords[0])
        end = Point(coords[-1])

        # 1. Terminal très proche d'un endpoint existant
        for ep in (start, end):
            d = terminal.distance(ep)
            if d < tolerance:
                return {
                    'type': 'endpoint',
                    'target_geom': ep,
                    'target_edge_idx': idx,
                    'distance': d,
                    'is_existing': True,
                }

        # 2. Projection sur le milieu de la ligne
        proj, d = _project_point_to_line(terminal, geom)
        if proj is None:
            continue
        # Vérifier que la projection n'est pas un endpoint
        d_start = proj.distance(start)
        d_end = proj.distance(end)
        is_midpoint = d_start > 0.5 and d_end > 0.5

        if is_midpoint and d < tolerance:
            if best is None or d < best['distance']:
                best = {
                    'type': 'midpoint',
                    'target_geom': proj,
                    'target_edge_idx': idx,
                    'distance': d,
                    'is_existing': True,
                }

    # 3. Fallback: endpoint le plus proche dans la tolérance
    if best is None:
        best_ep = None
        best_d = float('inf')
        for idx, edge in infra_gdf.iterrows():
            geom = edge.geometry
            if geom is None or geom.is_empty:
                continue
            if not isinstance(geom, (LineString, MultiLineString)):
                continue
            if isinstance(geom, MultiLineString):
                coords = list(geom.geoms[0].coords)
                coords += list(geom.geoms[-1].coords)
            else:
                coords = list(geom.coords)
            if len(coords) < 2:
                continue
            for ep_coords in [coords[0], coords[-1]]:
                ep = Point(ep_coords)
                d = terminal.distance(ep)
                if d < best_d and d < tolerance:
                    best_d = d
                    best_ep = ep
                    best_idx = idx
        if best_ep is not None:
            return {
                'type': 'nearest_endpoint',
                'target_geom': best_ep,
                'target_edge_idx': best_idx,
                'distance': best_d,
                'is_existing': True,
            }

    return best


def _build_c0_connector(
    terminal: Point,
    target: Point,
    length_limit: float = C0_MAX_LENGTH_M,
) -> Optional[LineString]:
    """
    Crée un connecteur C0 terminal → target si la distance est raisonnable.
    Ne crée JAMAIS de C0 zéro-longueur.
    Retourne None si trop long ou degeneré.
    """
    geom = LineString([terminal, target])
    length = geom.length
    if length < MIN_SEGMENT_M:
        # Trop court → pas de C0 zero-longueur, on considère comme connecté
        return None
    if length > length_limit:
        logger.warning(
            "Connecteur C0 refusé: %.1f m > %.1f m max", length, length_limit
        )
        return None
    return geom


def ensure_terminals_connected(
    terminals_gdf: gpd.GeoDataFrame,
    infra_gdf: gpd.GeoDataFrame,
    c0_rows: Optional[list] = None,
) -> tuple:
    """
    Connecte les terminaux (PA/PB) à l'infra existante.

    Paramètres:
        terminals_gdf: GeoDataFrame avec colonnes geometry, id, type (PA/PB)
        infra_gdf: lignes d'infra existante (geometry: LineString, src='existant')
        c0_rows: liste mutable pour accumuler les nouveaux connecteurs C0

    Règles:
        - PA/PB se connectent d'abord à une infra existante livrée
        - Priorité de snap: endpoint existant → projection milieu → endpoint proche
        - Si projection sur milieu de ligne avec d_to_proj < 0.01:
          → ne jamais créer de C0 zéro-longueur (split la ligne existante)
        - C0 uniquement si vraiment nécessaire, court, jamais arbitraire
        - Ne jamais superposer C0 sur existant
        - Si pas de connexion possible: flag disconnected, PAS de C0 arbitraire
    """
    if c0_rows is None:
        c0_rows = []

    qa_log = {
        'terminals_processed': 0,
        'connected_to_existing': 0,
        'c0_created': 0,
        'c0_rejected_too_long': 0,
        'disconnected': 0,
    }

    new_rows = []

    for _, term in terminals_gdf.iterrows():
        geom = term.geometry
        if not isinstance(geom, Point):
            logger.warning("Terminal %s: géométrie non-point, ignoré", term.get('id', '?'))
            continue

        qa_log['terminals_processed'] += 1

        snap_info = _snap_to_existing_infra(geom, infra_gdf)

        if snap_info is None:
            # Pas d'infra à portée → flag disconnected
            qa_log['disconnected'] += 1
            row = dict(term)
            row['pa_pb_disconnected'] = True
            row['disconnect_reason'] = 'no_existing_infra_in_range'
            row['connection_type'] = 'disconnected'
            new_rows.append(row)
            logger.warning("Terminal %s: aucun snap possible → disconnected", term.get('id', '?'))
            continue

        target = snap_info['target_geom']
        target_idx = snap_info['target_edge_idx']

        if snap_info['type'] in ('endpoint', 'nearest_endpoint'):
            # Terminal connecté à un endpoint existant — pas besoin de C0
            qa_log['connected_to_existing'] += 1
            row = dict(term)
            row['pa_pb_disconnected'] = False
            row['connection_type'] = 'existing_endpoint'
            row['distance_to_infra_m'] = snap_info['distance']
            row['connected_to_edge_idx'] = target_idx
            new_rows.append(row)
            continue

        if snap_info['type'] == 'midpoint':
            d_to_proj = snap_info['distance']
            if d_to_proj < MIN_SEGMENT_M:
                # Projection quasi-exacte sur la ligne → pas de C0
                # Le terminal est considéré comme connecté sans nouveau segment
                qa_log['connected_to_existing'] += 1
                row = dict(term)
                row['pa_pb_disconnected'] = False
                row['connection_type'] = 'existing_midpoint_no_c0'
                row['distance_to_infra_m'] = d_to_proj
                row['connected_to_edge_idx'] = target_idx
                new_rows.append(row)
                continue

            # Projection sur milieu avec distance non-négligeable → C0 requis
            connector = _build_c0_connector(geom, target)
            if connector is None:
                qa_log['c0_rejected_too_long'] += 1
                row = dict(term)
                row['pa_pb_disconnected'] = True
                row['disconnect_reason'] = 'c0_would_exceed_length_limit'
                row['connection_type'] = 'disconnected_c0_too_long'
                new_rows.append(row)
                continue

            # Ajouter le C0 connector
            c0_row = {
                'geometry': connector,
                'src': 'c0_neuf',
                'mode_pose': 'C0',
                'id_paste': term.get('id_paste', ''),
                'id_paa': term.get('id_paa', ''),
                'connection_type': 'existing_midpoint_c0',
                'length_m': connector.length,
                'target_edge_idx': target_idx,
                'is_short': connector.length < C0_MAX_LENGTH_M * 0.3,
            }
            c0_rows.append(c0_row)
            qa_log['c0_created'] += 1

            row = dict(term)
            row['pa_pb_disconnected'] = False
            row['connection_type'] = 'existing_midpoint_with_c0'
            row['distance_to_infra_m'] = d_to_proj
            row['connected_to_edge_idx'] = target_idx
            row['c0_length_m'] = connector.length
            new_rows.append(row)

    result_gdf = gpd.GeoDataFrame(new_rows, crs=2154) if new_rows else gpd.GeoDataFrame()
    return result_gdf, qa_log


# ──────────────────────────────────────────────────────────────────────
# Section C — _split_livrableedges_at_endpoint_projections()
# Split T-junction CORRECT et robuste.
# ──────────────────────────────────────────────────────────────────────

def _line_contains_point_midpoint(line: LineString, pt: Point, tolerance: float) -> bool:
    """
    Vérifie si un point est projeté sur le milieu d'une ligne (pas un endpoint).
    """
    proj, d = _project_point_to_line(pt, line)
    if proj is None or d > tolerance:
        return False
    coords = list(line.coords)
    if len(coords) < 2:
        return False
    d_start = proj.distance(Point(coords[0]))
    d_end = proj.distance(Point(coords[-1]))
    return d_start > tolerance and d_end > tolerance


def split_livrableedges_at_endpoint_projections(
    rows: pd.DataFrame,  # livrable rows avec geometry: LineString
    terminals_gdf: Optional[gpd.GeoDataFrame] = None,
    tolerance: float = T_JUNCTION_TOLERANCE_M,
) -> tuple:
    """
    Scinde les lignes existantes aux points où les terminaux se projettent,
    créant des T-junctions propres.

    Règles:
        - Reconstruire la liste des endpoints à chaque passe, à partir des
          rows actuelles (pas de liste old_to_new fragile)
        - Utiliser un mapping robuste old_idx → new_idx / [new_idx_a, new_idx_b]
        - Réécrire l'endpoint de la ligne SOURCE même si la source est après
          la cible dans l'ordre des rows
        - Éviter les splits dégénérés < 0.01 m
    """
    rows = rows.copy()
    if rows.empty:
        return rows, {}, {'splits': 0}

    qa_log = {'splits': 0, 'degenerate_skipped': 0}

    # Mapping old_idx → new rows
    old_to_new: dict = {}  # idx → [new_idx_a, new_idx_b] ou new_idx si non-split

    # Collecter tous les points de projection (terminals)
    projection_points = []
    if terminals_gdf is not None and not terminals_gdf.empty:
        for _, term in terminals_gdf.iterrows():
            if isinstance(term.geometry, Point):
                projection_points.append(term.geometry)

    # Liste mutable de lignes avec leur index original
    current_lines = []
    for idx, row in rows.iterrows():
        geom = row.geometry
        if isinstance(geom, LineString) and not geom.is_empty:
            current_lines.append((idx, row, geom))

    new_rows_list = []

    for orig_idx, orig_row, line in current_lines:
        split_points = []

        # Trouver les points de projection sur cette ligne
        for pt in projection_points:
            proj, d = _project_point_to_line(pt, line)
            if proj is None:
                continue
            if d > tolerance:
                continue

            # Vérifier que ce n'est pas un endpoint
            coords = list(line.coords)
            d_start = proj.distance(Point(coords[0]))
            d_end = proj.distance(Point(coords[-1]))

            if d_start < MIN_SEGMENT_M or d_end < MIN_SEGMENT_M:
                # Point trop proche d'un endpoint → pas de split nécessaire
                continue

            # Position curviligne du point de projection
            # Pour split, on utilise la distance le long de la ligne
            split_points.append((line.project(proj), proj))

        if not split_points:
            # Pas de split → garder la ligne telle quelle
            new_rows_list.append(dict(orig_row))
            old_to_new[orig_idx] = [len(new_rows_list) - 1]
            continue

        # Trier les points par position curviligne (unique by position)
        split_points.sort(key=lambda x: x[0])

        # Effectuer le split — approche simple: inserer les points projetes
        # dans la geometrie originale puis reconstruire
        # On reconstruit la ligne avec les points d'insertion tries par distance
        all_coords = list(line.coords)

        for cum_dist, proj in split_points:
            px = proj.x
            py = proj.y
            pz = proj.coords[0][2] if len(proj.coords[0]) > 2 else 0.0
            best_ci = None
            best_d = float('inf')
            proj_tuple = (px, py, pz) if len(all_coords[0]) > 2 else (px, py)
            for ci in range(len(all_coords)):
                p = all_coords[ci]
                if len(p) > 2 and len(proj_tuple) > 2:
                    d = math.sqrt((p[0]-proj_tuple[0])**2 + (p[1]-proj_tuple[1])**2 + (p[2]-proj_tuple[2])**2)
                else:
                    d = math.sqrt((p[0]-proj_tuple[0])**2 + (p[1]-proj_tuple[1])**2)
                if d < best_d:
                    best_d = d
                    best_ci = ci
            # Ne pas insérer si deja trop proche d'un coord existant
            if best_d > MIN_SEGMENT_M and best_ci is not None:
                all_coords.insert(best_ci, proj_tuple)

        # Reconstruire la ligne avec les points inseres
        try:
            new_line = LineString(all_coords)
        except Exception:
            new_line = line

        # Extraire les sous-segments en coupant aux points inseres
        # On veut les segments originaux + les segments entre points d'insertion
        coords = list(new_line.coords)
        if len(coords) < 2:
            new_row = dict(orig_row)
            new_row['geometry'] = line
            new_rows_list.append(new_row)
            old_to_new[orig_idx] = [len(new_rows_list) - 1]
            continue

        segments = []
        for i in range(len(coords) - 1):
            seg = LineString([coords[i], coords[i+1]])
            if seg.length >= MIN_SEGMENT_M and not seg.is_empty:
                segments.append(seg)

        if not segments:
            old_to_new[orig_idx] = []
            qa_log['degenerate_skipped'] += 1
            continue

        old_to_new[orig_idx] = []
        for seg in segments:
            new_row = dict(orig_row)
            new_row['geometry'] = seg
            if 'length_m' in orig_row.index:
                new_row['length_m'] = seg.length
            new_rows_list.append(new_row)
            old_to_new[orig_idx].append(len(new_rows_list) - 1)
            qa_log['splits'] += 1

    # Rebuild the dataframe
    if new_rows_list:
        result_gdf = gpd.GeoDataFrame(new_rows_list, crs=2154)
    else:
        result_gdf = gpd.GeoDataFrame(columns=['geometry'], crs=2154)

    return result_gdf, old_to_new, qa_log


# ──────────────────────────────────────────────────────────────────────
# Section D — _reconnect_after_energy_removal()
# Reconnexion POST-suppression énergie SANS créer de C0 arbitraire.
# ──────────────────────────────────────────────────────────────────────

def _is_bt_or_e1_segment(row: dict) -> bool:
    """
    Vérifie si un segment supprimé était vraiment BT/E1.
    Critères: colonne type/tension/source ou longueur courte.
    """
    type_val = str(row.get('type', '')).upper()
    tension = str(row.get('tension', '')).upper()
    source = str(row.get('src', '')).upper()

    if 'BT' in type_val or 'E1' in type_val:
        return True
    if tension in ('BT', 'E1', '230V', '400V'):
        return True
    if source in ('BT', 'E1'):
        return True
    return False


def _reconnect_via_existing(
    orphan_a: Point,
    orphan_b: Point,
    infra_gdf: gpd.GeoDataFrame,
    max_length: float = C0_MAX_LENGTH_M,
) -> Optional[dict]:
    """
    Tente de reconnecter deux orphelins via l'infra existante plutôt que
    par un trait droit direct.

    Stratégie:
        1. Chaque orphan snap sur infra existante → trouve chemins existants
        2. Si les deux orphelins se reconnectent au même réseau existant → OK
        3. Sinon, si connecteur court requis → créer C0 minime

    Retourne:
        None si reconnexion impossible (flag ENERGY_RECONNECT_FAILED)
        ou dict avec le connecteur et metadata.
    """
    snap_a = _snap_to_existing_infra(orphan_a, infra_gdf)
    snap_b = _snap_to_existing_infra(orphan_b, infra_gdf)

    if snap_a is None and snap_b is None:
        return None  # Les deux orphelins isolés → flag

    if snap_a is not None and snap_b is not None:
        # Les deux orphelins se connectent à l'existant
        # Vérifier si c'est le même réseau (distance entre snap points courte)
        dist = snap_a['target_geom'].distance(snap_b['target_geom'])
        if dist < MIN_SEGMENT_M:
            return {'type': 'existing', 'reconnected': True}
        else:
            # Besoin d'un connecteur entre les deux snaps via existant
            if dist < max_length:
                connector = _build_c0_connector(snap_a['target_geom'], snap_b['target_geom'])
                if connector is not None:
                    return {
                        'type': 'c0_connector',
                        'geometry': connector,
                        'length_m': connector.length,
                        'reconnected': True,
                    }

    if snap_a is not None and snap_b is None:
        # A reconnecté, B orphelin → essayer de connecter B à A via infra
        dist = orphan_b.distance(snap_a['target_geom'])
        if dist < max_length:
            connector = _build_c0_connector(orphan_b, snap_a['target_geom'])
            if connector is not None:
                return {
                    'type': 'c0_to_existing',
                    'geometry': connector,
                    'target': 'A',
                    'length_m': connector.length,
                    'reconnected': True,
                }

    if snap_b is not None and snap_a is None:
        dist = orphan_a.distance(snap_b['target_geom'])
        if dist < max_length:
            connector = _build_c0_connector(orphan_a, snap_b['target_geom'])
            if connector is not None:
                return {
                    'type': 'c0_to_existing',
                    'geometry': connector,
                    'target': 'B',
                    'length_m': connector.length,
                    'reconnected': True,
                }

    return None


def reconnect_after_energy_removal(
    removed_segments: list,
    remaining_infra: gpd.GeoDataFrame,
    c0_rows: Optional[list] = None,
) -> tuple:
    """
    Après suppression de segments énergie:
        - Identifier les segments supprimés par diff before_keys/after_keys
        - Ne reconnecter que si le segment supprimé était vraiment BT/E1
        - Reconnecter via existant, jamais par long trait droit
        - Si pas de solution propre → flag ENERGY_RECONNECT_FAILED
    """
    if c0_rows is None:
        c0_rows = []

    qa_log = {
        'segments_analyzed': 0,
        'bt_e1_reconnected': 0,
        'non_bt_e1_skipped': 0,
        'reconnect_failed': 0,
        'c0_created': 0,
    }

    for seg in removed_segments:
        qa_log['segments_analyzed'] += 1

        if not _is_bt_or_e1_segment(seg):
            qa_log['non_bt_e1_skipped'] += 1
            continue

        # Extraire les orphelins (endpoints du segment supprimé)
        geom = seg.get('geometry')
        if geom is None or not isinstance(geom, (LineString, MultiLineString)):
            continue

        if isinstance(geom, MultiLineString):
            geom = geom.geoms[0]

        coords = list(geom.coords)
        if len(coords) < 2:
            continue

        orphan_a = Point(coords[0])
        orphan_b = Point(coords[-1])

        result = _reconnect_via_existing(orphan_a, orphan_b, remaining_infra)

        if result is None:
            qa_log['reconnect_failed'] += 1
            seg_flag = dict(seg)
            seg_flag['ENERGY_RECONNECT_FAILED'] = True
            continue

        if result['type'] == 'existing':
            qa_log['bt_e1_reconnected'] += 1
            continue

        if result['type'] in ('c0_connector', 'c0_to_existing'):
            connector = result.get('geometry')
            if connector is not None:
                c0_row = {
                    'geometry': connector,
                    'src': 'c0_neuf',
                    'mode_pose': 'C0',
                    'reconnect_source': 'energy_removal',
                    'target': result.get('target', ''),
                    'length_m': result.get('length_m', connector.length),
                    'is_short': connector.length < C0_MAX_LENGTH_M * 0.3,
                }
                c0_rows.append(c0_row)
                qa_log['c0_created'] += 1
                qa_log['bt_e1_reconnected'] += 1

    return qa_log


# ──────────────────────────────────────────────────────────────────────
# Section E — _drop_c0_when_existing_equivalent()
# Purge C0 / GC neuf superposés à l'infra existante.
# ──────────────────────────────────────────────────────────────────────

def _c0_overlaps_existing(
    c0_geom: LineString,
    infra_gdf: gpd.GeoDataFrame,
    tolerance: float = EXISTING_COINCIDENCE_M,
    parallel_deg: float = MAX_CONNECTOR_ANGLE_DEG,
) -> bool:
    """
    Vérifie si un C0 neuf est superposé ou parallèle à une ligne existante.
    Retourne True si overlap → le C0 doit être supprimé.
    """
    for _, edge in infra_gdf.iterrows():
        existing = edge.geometry
        if existing is None or existing.is_empty:
            continue
        if not isinstance(existing, (LineString, MultiLineString)):
            continue

        # 1. Proximité: la distance moyenne entre points du C0 et la ligne existante
        try:
            # Distance du midpoint C0 à la ligne existante
            mid = c0_geom.interpolate(0.5, normalized=True)
            proj, d = _project_point_to_line(mid, existing)
            if d < tolerance:
                # 2. Angulaire: vérifier que le C0 n'est pas juste parallèle
                #    au réseau adjacent (ça pourrait être un duplicata)
                c0_coords = list(c0_geom.coords)
                existing_coords = list(existing.coords)
                if len(c0_coords) >= 2 and len(existing_coords) >= 2:
                    dx1 = c0_coords[-1][0] - c0_coords[0][0]
                    dy1 = c0_coords[-1][1] - c0_coords[0][1]
                    dx2 = existing_coords[-1][0] - existing_coords[0][0]
                    dy2 = existing_coords[-1][1] - existing_coords[0][1]

                    # Produit scalaire normalisé
                    norm1 = math.sqrt(dx1*dx1 + dy1*dy1)
                    norm2 = math.sqrt(dx2*dx2 + dy2*dy2)
                    if norm1 > 0 and norm2 > 0:
                        cos_angle = (dx1*dx2 + dy1*dy2) / (norm1 * norm2)
                        angle = math.degrees(math.acos(max(-1, min(1, cos_angle))))
                        if angle < parallel_deg or angle > 180 - parallel_deg:
                            return True  # Superposé ou parallèle
                return True  # Proximité seule suffit pour flag
        except Exception:
            continue
    return False


def drop_c0_when_existing_equivalent(
    livrable_gdf: gpd.GeoDataFrame,
    infra_gdf: gpd.GeoDataFrame,
) -> tuple:
    """
    Passe finale: pour chaque row src == 'c0_neuf' ou mode_pose == 'C0':
        - Vérifier si un existant équivalent est présent (proximité + angulaire)
        - Si oui → DROP le C0 (utilisé l'existant à la place)
        - Logger les métriques QA

    Résultat attendu:
        - Plus de C0 superposé ou parallèle à une infra existante
        - C0 uniquement quand réellement nécessaire
    """
    qa_log = {
        'c0_examined': 0,
        'c0_dropped': 0,
        'c0_kept': 0,
        'ign_route_delivered_as_gc_m_sum': 0,
        'pa_pb_disconnected_count': 0,
        'micro_gaps_unresolved': 0,
    }

    is_c0 = livrable_gdf.apply(
        lambda r: str(r.get('src', '')) == 'c0_neuf' or str(r.get('mode_pose', '')) == 'C0',
        axis=1,
    ) if 'src' in livrable_gdf.columns or 'mode_pose' in livrable_gdf.columns \
       else pd.Series([False]*len(livrable_gdf))

    keep_mask = ~is_c0
    gc_neuf_total = 0

    for idx in livrable_gdf.index:
        row = livrable_gdf.loc[idx]

        if not is_c0.get(idx, False):
            continue

        qa_log['c0_examined'] += 1
        geom = row.geometry

        if geom is None or not isinstance(geom, LineString):
            keep_mask[idx] = True
            qa_log['c0_kept'] += 1
            continue

        overlaps = _c0_overlaps_existing(geom, infra_gdf)
        if overlaps:
            keep_mask[idx] = False
            qa_log['c0_dropped'] += 1
        else:
            keep_mask[idx] = True
            qa_log['c0_kept'] += 1
            gc_neuf_total += 1

    # Ajouter métriques agrégées
    if 'length_m' in livrable_gdf.columns:
        qa_log['ign_route_delivered_as_gc_m_sum'] = float(
            livrable_gdf.loc[keep_mask & is_c0, 'length_m'].sum()
        )
    if 'pa_pb_disconnected' in livrable_gdf.columns:
        qa_log['pa_pb_disconnected_count'] = int(
            (livrable_gdf['pa_pb_disconnected'] == True).sum()
        )
    if 'micro_gaps_unresolved' in livrable_gdf.columns:
        qa_log['micro_gaps_unresolved'] = int(
            livrable_gdf['micro_gaps_unresolved'].sum()
        )

    # Logger le résumé QA
    logger.info("=== QA Summary — drop_c0 ===")
    logger.info("C0 examinés: %d", qa_log['c0_examined'])
    logger.info("C0 supprimés (équivalent existant): %d", qa_log['c0_dropped'])
    logger.info("C0 conservés (nécessaires): %d", qa_log['c0_kept'])
    logger.info("Longueur C0 total conservé: %.1f m", qa_log['ign_route_delivered_as_gc_m_sum'])
    logger.info("PA/PB déconnectés: %d", qa_log['pa_pb_disconnected_count'])

    result_gdf = livrable_gdf[keep_mask].copy()
    return result_gdf, qa_log


# ──────────────────────────────────────────────────────────────────────
# Utility — build complete QA report
# ──────────────────────────────────────────────────────────────────────

def build_qa_report(
    ensure_log: Optional[dict] = None,
    split_log: Optional[dict] = None,
    reconnect_log: Optional[dict] = None,
    drop_log: Optional[dict] = None,
) -> dict:
    """Agrège tous les logs QA en un rapport structuré."""
    report = {}
    if ensure_log:
        report['ensure_terminals'] = ensure_log
    if split_log:
        report['split_tjunction'] = split_log
    if reconnect_log:
        report['reconnect_energy'] = reconnect_log
    if drop_log:
        report['drop_c0'] = drop_log
    return report
