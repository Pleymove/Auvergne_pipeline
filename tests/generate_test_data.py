#!/usr/bin/env python3
"""
Génère des données de test synthétiques pour valider le pipeline.
Crée :
  - PAA (points)
  - ZAPA (polygones)
  - Pâtes (points)
  - Infra existante (linestrings)
"""

import geopandas as gpd
from shapely.geometry import Point, Polygon, LineString
import random
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent
PROJECT_DIR = PROJECT_ROOT / "data" / "project_sample"
OUTPUT_DIR = PROJECT_ROOT / "output"

random.seed(42)


def create_synthetic_project(area_center=(500000, 300000), area_size=5000):
    """Génère un jeu de données projet simple."""
    print("=== GÉNÉRATION DONNÉES TEST ===\n")

    # 1. PAA (points sources)
    paa_points = []
    paa_data = []
    for i in range(3):  # 3 PAA
        x = area_center[0] + random.uniform(-area_size/2, area_size/2)
        y = area_center[1] + random.uniform(-area_size/2, area_size/2)
        paa_points.append(Point(x, y))
        paa_data.append({
            'id_ppa': f'PAA_{i+1:03d}',
            'nom': f'PAA Nord {i+1}',
        })

    paa_gdf = gpd.GeoDataFrame(paa_data, geometry=paa_points, crs="EPSG:2154")

    # 2. ZAPA (polygones autour de chaque PAA)
    zapa_polys = []
    zapa_data = []
    for idx, row in paa_gdf.iterrows():
        x, y = row.geometry.x, row.geometry.y
        size = random.uniform(800, 1500)
        poly = Polygon([
            (x-size/2, y-size/2),
            (x+size/2, y-size/2),
            (x+size/2, y+size/2),
            (x-size/2, y+size/2),
        ])
        zapa_polys.append(poly)
        zapa_data.append({
            'id': f'ZAPA_{row["id_ppa"]}',
            'id_ppa': row['id_ppa'],
            'surface_ha': size*size/10000,
        })

    zapa_gdf = gpd.GeoDataFrame(zapa_data, geometry=zapa_polys, crs="EPSG:2154")

    # 3. Pâtes (points dans chaque ZAPA)
    paste_points = []
    paste_data = []
    paste_id = 0
    for _, zapa_row in zapa_gdf.iterrows():
        poly = zapa_row.geometry
        bounds = poly.bounds
        n_pastes = random.randint(2, 5)
        for _ in range(n_pastes):
            x = random.uniform(bounds[0], bounds[2])
            y = random.uniform(bounds[1], bounds[3])
            p = Point(x, y)
            if poly.contains(p):
                paste_points.append(p)
                paste_data.append({
                    'id_paste': f'PASTE_{paste_id+1:04d}',
                    'zapa_id': zapa_row['id'],
                    'type': random.choice(['FTTH', 'FFT']),
                })
                paste_id += 1

    pastes_gdf = gpd.GeoDataFrame(paste_data, geometry=paste_points, crs="EPSG:2154")

    # 4. Infra existante (tracés PAA→première pâte + tronçons)
    infra_lines = []
    infra_data = []
    for _, paste_row in pastes_gdf.iterrows():
        paa_id = paste_row['zapa_id'].replace('ZAPA_', 'PAA_')
        paa_geom = paa_gdf[paa_gdf['id_ppa'] == paa_id].geometry.iloc[0]
        x1, y1 = paa_geom.x, paa_geom.y
        x2, y2 = paste_row.geometry.x, paste_row.geometry.y

        # Ligne simple (pouvait être remplacée par chemin réseau)
        line = LineString([(x1, y1), (x2, y2)])
        infra_lines.append(line)
        infra_data.append({
            'id_paste': paste_row['id_paste'],
            'source': 'infra_existante',
            'status': 'active',
        })

    infra_gdf = gpd.GeoDataFrame(infra_data, geometry=infra_lines, crs="EPSG:2154")

    # Sauvegarde
    PROJECT_DIR.mkdir(parents=True, exist_ok=True)
    paa_gdf.to_file(PROJECT_DIR / "PAA.gpkg", driver="GPKG")
    zapa_gdf.to_file(PROJECT_DIR / "ZAPA.gpkg", driver="GPKG")
    pastes_gdf.to_file(PROJECT_DIR / "pastes.gpkg", driver="GPKG")
    infra_gdf.to_file(PROJECT_DIR / "infra_existante.gpkg", driver="GPKG")

    print(f"  ✓ PAA     : {len(paa_gdf)} points")
    print(f"  ✓ ZAPA    : {len(zapa_gdf)} polygones")
    print(f"  ✓ Pâtes   : {len(pastes_gdf)} points")
    print(f"  ✓ Infra   : {len(infra_gdf)} tronçons")
    print(f"\nDonnées sauvegardées dans {PROJECT_DIR}/")

    return paa_gdf, zapa_gdf, pastes_gdf


def create_synthetic_obstacles(roads_gdf: gpd.GeoDataFrame):
    """
    Génère une couche obstacles synthétiques :
    - Fleuves (lignes)
    - Zones privées (polygones)
    """
    obstacles = []

    # Simulation fleuves (crossing lines)
    river_lines = []
    for _ in range(2):
        x = random.uniform(450000, 550000)
        y1 = random.uniform(200000, 400000)
        y2 = random.uniform(200000, 400000)
        line = LineString([(x, y1), (x, y2)])
        river_lines.append(line)

    river_gdf = gpd.GeoDataFrame(
        {'type': ['fleuve']*len(river_lines)},
        geometry=river_lines,
        crs="EPSG:2154"
    )
    obstacles.append(river_gdf)

    # Zones privées
    private_polys = []
    for _ in range(5):
        x = random.uniform(480000, 520000)
        y = random.uniform(280000, 320000)
        poly = Polygon([
            (x-200, y-200), (x+200, y-200),
            (x+200, y+200), (x-200, y+200)
        ])
        private_polys.append(poly)

    private_gdf = gpd.GeoDataFrame(
        {'type': ['prive']*len(private_polys)},
        geometry=private_polys,
        crs="EPSG:2154"
    )
    obstacles.append(private_gdf)

    if obstacles:
        merged = gpd.GeoDataFrame(pd.concat(obstacles, ignore_index=True), crs="EPSG:2154")
        merged.to_file(OUTPUT_DIR / "obstacles_synthetic.gpkg", driver="GPKG")
        print(f"  ✓ Obstacles synthétiques : {len(merged)} entités")
        return merged
    return gpd.GeoDataFrame()


def generate_mock_roads(area_center=(500000, 300000), area_size=5000):
    """
    Génère un petit réseau routier synthétique pour tester build_network.
    (Grillement orthogonal)
    """
    lines = []
    spacing = 500
    x_start = area_center[0] - area_size/2
    x_end = area_center[0] + area_size/2
    y_start = area_center[1] - area_size/2
    y_end = area_center[1] + area_size/2

    # Routes horizontales
    for y in [y_start + i*spacing for i in range(int(area_size/spacing)+1)]:
        lines.append(LineString([(x_start, y), (x_end, y)]))

    # Routes verticales
    for x in [x_start + i*spacing for i in range(int(area_size/spacing)+1)]:
        lines.append(LineString([(x, y_start), (x, y_end)]))

    roads_gdf = gpd.GeoDataFrame(
        {'highway': ['primary']*len(lines)},
        geometry=lines,
        crs="EPSG:2154"
    )
    roads_gdf.to_file(PROJECT_ROOT / "data" / "contraints" / "roads_synthetic.gpkg", driver="GPKG")
    print(f"  ✓ Réseau routier synthétique : {len(lines)} tronçons")
    return roads_gdf


if __name__ == "__main__":
    import pandas as pd

    print("=== TEST DATA GENERATOR ===\n")

    # 1. Générer données projet
    paa, zapa, pastes = create_synthetic_project()

    # 2. Générer réseau routier synthétique
    roads = generate_mock_roads()

    # 3. Générer obstacles
    create_synthetic_obstacles(roads)

    print("\n✅ Jeu de test complet.")
    print("Tu peux maintenant lancer le pipeline :")
    print("  python scripts/run_pipeline.py")
