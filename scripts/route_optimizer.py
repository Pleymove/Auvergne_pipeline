#!/usr/bin/env python3
"""
Route optimizer: compute shortest path from PAA to each pâte.
Detects obstacles and flags complex cases for manual review.
"""

import argparse
import pickle
from pathlib import Path
from typing import Dict, List, Tuple

import geopandas as gpd
import networkx as nx
import numpy as np
import pandas as pd
from shapely.geometry import Point, LineString
from shapely.ops import nearest_points

PROJECT_ROOT = Path(__file__).parent.parent
SCRIPTS = PROJECT_ROOT / "scripts"
CONSTRAINTS_DIR = PROJECT_ROOT / "data" / "contraints"
PROJECT_DIR = PROJECT_ROOT / "data" / "project_sample"
OUTPUT_DIR = PROJECT_ROOT / "output"

# Import topology engine for PR32 existing-first logic
from topology_engine import (
    enforce_crs,
    ensure_terminals_connected,
    split_livrableedges_at_endpoint_projections,
    reconnect_after_energy_removal,
    drop_c0_when_existing_equivalent,
    build_qa_report,
)

# Distance max to snap to road network (m)
SNAP_TOLERANCE = 50


def load_network() -> nx.MultiDiGraph:
    graph_file = OUTPUT_DIR / "road_network.pkl"
    if not graph_file.exists():
        raise FileNotFoundError(
            "Graphe routier manquant. Lance d'abord : python scripts/build_network.py"
        )
    with open(graph_file, 'rb') as f:
        return pickle.load(f)


def snap_to_network(point: Point, G: nx.MultiDiGraph) -> str:
    """Snap a point to the nearest network node within tolerance."""
    nearest_node = None
    min_dist = float('inf')

    for node, data in G.nodes(data=True):
        node_pt = Point(data['x'], data['y'])
        dist = point.distance(node_pt)
        if dist < min_dist and dist < SNAP_TOLERANCE:
            min_dist = dist
            nearest_node = node

    if nearest_node is None:
        # Créer un nœud virtuel si hors réseau
        node_id = f"virtual_{hash(point.wkt)}"
        G.add_node(node_id, x=point.x, y=point.y)
        return node_id

    return nearest_node


def load_obstacles() -> gpd.GeoDataFrame:
    """Charge toutes les couches obstacles et les fusionne."""
    obstacles = []

    # Eaux
    water_files = list(CONSTRAINTS_DIR.glob("water*.gpkg"))
    if water_files:
        water = gpd.read_file(water_files[0])
        if not water.empty:
            obstacles.append(water)

    # Building / private parcels (si disponible)
    building_files = list(CONSTRAINTS_DIR.glob("buildings*.gpkg"))
    if building_files:
        buildings = gpd.read_file(building_files[0])
        if not buildings.empty:
            obstacles.append(buildings)

    if obstacles:
        merged = gpd.GeoDataFrame(pd.concat(obstacles, ignore_index=True), crs=obstacles[0].crs)
        print(f"  ✓ Obstacles chargés : {len(merged)} polygones")
        return merged
    else:
        print("  ⚠ Aucune couche obstacle trouvée")
        return gpd.GeoDataFrame(columns=['geometry'], crs="EPSG:2154")


def load_project_data() -> Tuple[gpd.GeoDataFrame, gpd.GeoDataFrame, gpd.GeoDataFrame]:
    """
    Charge tes couches projet :
    - paa : points PAA (identifiant unique)
    - zapa : polygones ZAPA
    - pastes : points extrémités (pâtes)
    """
    paa_file = PROJECT_DIR / "PAA.gpkg"
    zapa_file = PROJECT_DIR / "ZAPA.gpkg"
    pastes_file = PROJECT_DIR / "pastes.gpkg"

    for f, name in [(paa_file, "PAA"), (zapa_file, "ZAPA"), (pastes_file, "pastes")]:
        if not f.exists():
            raise FileNotFoundError(f"Fichier manquant : {f}\n"
                                    f"Dépose tes couches dans {PROJECT_DIR}/")

    paa = gpd.read_file(paa_file)
    zapa = gpd.read_file(zapa_file)
    pastes = gpd.read_file(pastes_file)

    print(f"  PAA chargés : {len(paa)}")
    print(f"  ZAPA chargés : {len(zapa)}")
    print(f"  Pâtes chargés : {len(pastes)}")

    return paa, zapa, pastes


def compute_path(
    G: nx.MultiDiGraph,
    start_node: str,
    end_node: str,
    obstacles_gdf: gpd.GeoDataFrame
) -> Tuple[List[str], bool]:
    """
    Calcule plus court chemin et détecte obstacles.
    Retourne (liste nœuds, flag_complexe)
    """
    try:
        path = nx.shortest_path(G, source=start_node, target=end_node, weight='length')
        # Reconstruire la géométrie
        coords = [(G.nodes[n]['x'], G.nodes[n]['y']) for n in path]
        line = LineString(coords)
        complex_flag = False

        # Vérifier croisement obstacles (eaux, bâtiments)
        if not obstacles_gdf.empty:
            for _, obs in obstacles_gdf.iterrows():
                if line.intersects(obs.geometry):
                    complex_flag = True
                    break

        return path, complex_flag
    except nx.NetworkXNoPath:
        return [], True  # Aucun chemin trouvable → complexe


def generate_deliverable(
    paa_gdf: gpd.GeoDataFrame,
    zapa_gdf: gpd.GeoDataFrame,
    pastes_gdf: gpd.GeoDataFrame,
    G: nx.MultiDiGraph,
    obstacles_gdf: gpd.GeoDataFrame,
    output_path: Path,
):
    """
    Génère la couche livrable avec pipeline PR32 existing-first:

    1. CRS EPSG:2154 force sur toutes les couches
    2. Livrable initial (chemins reseau)
    3. ensure_terminals_connected() — PA/PB snap existant priorite
    4. split_livrableedges_at_endpoint_projections() — T-junctions
    5. drop_c0_when_existing_equivalent() — purge C0 superposes
    6. QA report
    """
    # 1. CRS force
    paa_gdf = enforce_crs(paa_gdf)
    zapa_gdf = enforce_crs(zapa_gdf)
    pastes_gdf = enforce_crs(pastes_gdf)

    # 2. Livrable initial (chemin reseau classique)
    results = []
    print("\n=== Calcul des chemins (PR31 baseline) ===")

    for idx, paa_row in paa_gdf.iterrows():
        paa_id = paa_row.get('id_ppa') or paa_row.get('PAA_ID') or idx
        paa_geom = paa_row.geometry
        zapa_match = zapa_gdf[zapa_gdf.contains(paa_geom)]
        if zapa_match.empty:
            print(f"  ⚠ PAA {paa_id} : aucune ZAPA → ignore")
            continue
        zapa_row = zapa_match.iloc[0]
        mask = pastes_gdf.within(zapa_row.geometry)
        zone_pastes = pastes_gdf[mask]
        if zone_pastes.empty:
            print(f"  ⚠ ZAPA {zapa_row.get('id')} : aucune pate → ignore")
            continue
        paa_node = snap_to_network(paa_geom, G)
        for _, paste_row in zone_pastes.iterrows():
            paste_id = paste_row.get('id_paste') or paste_row.get('PASTE_ID') or idx
            paste_geom = paste_row.geometry
            paste_node = snap_to_network(paste_geom, G)
            path_nodes = []
            try:
                path_nodes = nx.shortest_path(G, paa_node, paste_node, weight='length')
            except nx.NetworkXNoPath:
                pass
            status = "AUTO" if path_nodes else "MANUAL_REVIEW"
            if path_nodes:
                coords = [(G.nodes[n]['x'], G.nodes[n]['y']) for n in path_nodes]
                traceline = LineString(coords)
            else:
                traceline = LineString()
            results.append({
                'paa_id': paa_id, 'zapa_id': zapa_row.get('id'),
                'paste_id': paste_id, 'status': status,
                'traceline': traceline,
            })
        print(f"  PAA {paa_id} → {len(zone_pastes)} pâtes")

    if not results:
        print("✗ Aucun resultat — verifie tes donnees")
        return

    livrable = gpd.GeoDataFrame(results, crs=2154, geometry='traceline')
    livrable = livrable.rename_geometry('geometry')
    livrable = livrable[~(livrable.geometry.is_empty | livrable.geometry.isna())]

    # Load infra existante pour les comparaisons PR32
    infra_file = PROJECT_DIR / "infra_existante.gpkg"
    if infra_file.exists():
        infra_gdf = enforce_crs(gpd.read_file(infra_file))
    else:
        infra_gdf = gpd.GeoDataFrame(crs=2154)

    all_qa = {}

    # 3. Ensure terminals connected (existing-first)
    print("\n=== PR32 §B — ensure_terminals_connected ===")
    terminals = pastes_gdf.copy()  # Les pâtes = terminaux PA/PB
    terminal_results, qa_b = ensure_terminals_connected(terminals, infra_gdf)
    all_qa['B_ensure_terminals'] = qa_b
    print(f"  Connectes au reseau: {qa_b['connected_to_existing']}")
    print(f"  C0 crees: {qa_b['c0_created']}")
    print(f"  Deconnectes: {qa_b['disconnected']}")

    # 4. Split T-junctions
    print("\n=== PR32 §C — split T-junctions ===")
    if not terminal_results.empty:
        livrable, split_map, qa_c = split_livrableedges_at_endpoint_projections(
            livrable, terminals_gdf=terminal_results
        )
        all_qa['C_split_tjunction'] = qa_c
        print(f"  Splits effectues: {qa_c['splits']}")
        print(f"  Degeneres ignores: {qa_c['degenerate_skipped']}")

    # 5. Drop C0 when existing equivalent
    print("\n=== PR32 §E — drop_c0_superposes ===")
    livrable, qa_e = drop_c0_when_existing_equivalent(livrable, infra_gdf)
    all_qa['E_drop_c0'] = qa_e
    print(f"  C0 examines: {qa_e['c0_examined']}")
    print(f"  C0 supprimes: {qa_e['c0_dropped']}")
    print(f"  C0 conserves: {qa_e['c0_kept']}")

    # 6. CRS check final
    livrable = enforce_crs(livrable)

    # Save
    livrable.to_file(output_path, driver="GPKG")
    print(f"\n✓ Livrable PR32 genere : {output_path}")
    print(f"  Total tronçons : {len(livrable)}")

    # QA report
    qa_report = build_qa_report(**all_qa)
    qa_path = output_path.with_suffix('.qa.json')
    import json
    with open(qa_path, 'w') as f:
        json.dump(qa_report, f, indent=2, default=str)
    print(f"  QA report : {qa_path}")


def main():
    parser = argparse.ArgumentParser(description="Génère livrable routier")
    parser.add_argument("--output", type=str, default=str(OUTPUT_DIR / "livrable_final.gpkg"))
    args = parser.parse_args()

    print("=== ROUTE OPTIMIZER ===\n")

    # 1. Charger réseau
    G = load_network()
    print(f"Graphe chargé : {G.number_of_nodes()} nœuds\n")

    # 2. Charger obstacles
    print("Chargement obstacles...")
    obstacles = load_obstacles()

    # 3. Charger données projet
    print("Chargement données projet...")
    paa, zapa, pastes = load_project_data()

    # 4. Générer livrable
    output_path = Path(args.output)
    generate_deliverable(paa, zapa, pastes, G, obstacles, output_path)

    print("\nTerminé. Tu peux ouvrir le livrable dans QGIS.")


if __name__ == "__main__":
    main()
