#!/usr/bin/env python3
"""
Build routable network graph from OSM roads.
Creates a NetworkX graph saved as pickle for fast reuse.
"""

import argparse
import pickle
from pathlib import Path

try:
    import geopandas as gpd
    import networkx as nx
    from shapely.geometry import LineString, Point
    import osmnx as ox
except ImportError as e:
    print(f"ERROR: {e}")
    print("pip install -r requirements.txt")
    exit(1)

PROJECT_ROOT = Path(__file__).parent.parent
CONSTRAINTS_DIR = PROJECT_ROOT / "data" / "contraints"
OUTPUT_DIR = PROJECT_ROOT / "output"

# Highway types that are routable (ordered by priority)
DRIVABLE_HIGHWAYS = {
    'motorway', 'trunk', 'primary', 'secondary', 'tertiary',
    'unclassified', 'residential', 'service',
    'living_street', 'pedestrian',  # inclus pour accès piéton/urgence
}


def load_roads() -> gpd.GeoDataFrame:
    """Charge toutes les routes depuis OSM."""
    files = list(CONSTRAINTS_DIR.glob("roads*.gpkg"))
    if not files:
        raise FileNotFoundError(f"Aucun fichier routes trouvé dans {CONSTRAINTS_DIR}")
    gdf = gpd.read_file(files[0])
    if gdf.crs is None:
        gdf.set_crs(epsg=4326, inplace=True)  # OSM default
    return gdf.to_crs(epsg=2154)  # Lambert 93


def create_graph(roads_gdf: gpd.GeoDataFrame) -> nx.MultiDiGraph:
    """Construit un graphe NetworkX à partir des tronçons routiers."""
    print("Construction du graphe routier...")

    # Filtrer routes non routables
    if "highway" in roads_gdf.columns:
        mask = roads_gdf["highway"].isin(DRIVABLE_HIGHWAYS) | roads_gdf["highway"].isna()
        roads = roads_gdf[mask].copy()
    else:
        roads = roads_gdf.copy()

    # Créer un graphe
    G = nx.MultiDiGraph(crs="EPSG:2154")

    for idx, row in roads.iterrows():
        geom = row.geometry
        if not isinstance(geom, LineString):
            continue

        # Extraire points extrêmes comme nœuds
        coords = list(geom.coords)
        if len(coords) < 2:
            continue

        start_pt = Point(coords[0])
        end_pt = Point(coords[-1])

        # Générer IDs uniques (hash de coordonnées)
        start_id = f"node_{hash((round(coords[0][0], 6), round(coords[0][1], 6)))}"
        end_id = f"node_{hash((round(coords[-1][0], 6), round(coords[-1][1], 6)))}"

        # Ajouter nœuds
        G.add_node(start_id, x=coords[0][0], y=coords[0][1])
        G.add_node(end_id, x=coords[-1][0], y=coords[-1][1])

        # Ajouter edge avec métadonnées
        length = geom.length  # en mètres (Lambert 93)
        edge_attrs = {
            'length': length,
            'geometry': geom.wkt,
            'highway': row.get('highway', 'unclassified'),
            'osmid': row.get('osmid', idx),
        }
        G.add_edge(start_id, end_id, **edge_attrs)
        # Graphe bidirectionnel si la route est à double sens
        if row.get('oneway', 'no') != 'yes':
            G.add_edge(end_id, start_id, **edge_attrs)

    print(f"  ✅ Graphe créé : {G.number_of_nodes()} nœuds, {G.number_of_edges()} arêtes")
    return G


def save_graph(G: nx.MultiDiGraph, path: Path):
    """Sauvegarde le graphe en pickle."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, 'wb') as f:
        pickle.dump(G, f)
    print(f"  ✓ Graphe sauvegardé : {path}")


def main():
    parser = argparse.ArgumentParser(description="Build road network graph")
    parser.add_argument("--force", action="store_true", help="Rebuild even if exists")
    args = parser.parse_args()

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    graph_file = OUTPUT_DIR / "road_network.pkl"

    if graph_file.exists() and not args.force:
        print(f"Graphe existe déjà : {graph_file}")
        print("  (utilise --force pour reconstructeur)")
        return

    print("=== BUILD NETWORK ===\n")

    roads_gdf = load_roads()
    print(f"Routes chargées : {len(roads_gdf)} tronçons")

    G = create_graph(roads_gdf)
    save_graph(G, graph_file)

    print("\nTerminé. Prochaine étape: python scripts/route_optimizer.py")


if __name__ == "__main__":
    main()
