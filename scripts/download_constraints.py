#!/usr/bin/env python3
"""
Download national constraint layers for France.
Two modes:
  - Full France (pre-defined bbox)
  - Custom bounding box (xmin,ymin,xmax,ymax) in Lambert 93
"""

import argparse
import os
import sys
from pathlib import Path

try:
    import osmnx as ox
    import geopandas as gpd
    from shapely.geometry import box
except ImportError:
    print("ERROR: Missing dependencies. Run: pip install -r requirements.txt")
    sys.exit(1)

# ── France bounds (Lambert 93 approximate) ──────────────────────────────────
FRANCE_BBOX = (-5.0, 41.0, 9.5, 51.5)  # (xmin, ymin, xmax, ymax) lon/lat WGS84

# ── OSM tags to extract ──────────────────────────────────────────────────────
TAGS = {
    "roads": [("highway", True)],          # toutes les voies
    "water": [("waterway", True), ("natural", "water")],
    "buildings": [("building", True)],
    "barriers": [("barrier", True)],       # clôtures, bornes
}

OUTPUT_DIR = Path(__file__).parent.parent / "data" / "contraints"


def download_layer(name: str, tags: list, bbox: tuple, filepath: Path):
    """Download a single OSM layer."""
    print(f"  ↓ Téléchargement {name}...")
    try:
        gdf = ox.features_from_bbox(bbox=bbox, tags=dict(tags))
        if gdf.empty:
            print(f"    ⚠ Aucun éléments trouvés pour {name}")
            return False
        gdf.to_file(filepath, driver="GPKG")
        print(f"    ✓ {len(gdf)} entités → {filepath.name}")
        return True
    except Exception as e:
        print(f"    ✗ Erreur: {e}")
        return False


def main():
    parser = argparse.ArgumentParser(description="Télécharger couches contraintes")
    parser.add_argument(
        "--bbox",
        type=str,
        help="Bounding box custom: xmin,ymin,xmax,ymax (WGS84 lon/lat)",
    )
    parser.add_argument(
        "--france",
        action="store_true",
        default=True,
        help="Télécharger toute la France (défaut)",
    )
    args = parser.parse_args()

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    if args.bbox:
        bbox = tuple(map(float, args.bbox.split(",")))
        print(f"Téléchargement zone custom : {bbox}")
    else:
        bbox = FRANCE_BBOX
        print(f"Téléchargement France entière (bbox: {bbox})")

    print(f"Destination : {OUTPUT_DIR}\n")

    for layer_name, tag_list in TAGS.items():
        success = True
        for tag_key, tag_val in tag_list:
            filename = f"{layer_name}.gpkg"
            if tag_val is True:  # tag existence only
                filename = f"{layer_name}_all.gpkg"
            filepath = OUTPUT_DIR / filename
            ok = download_layer(layer_name, [(tag_key, tag_val)], bbox, filepath)
            success = success and ok
        if success:
            print(f"  ✅ {layer_name} OK\n")
        else:
            print(f"  ⚠ {layer_name} partiel\n")

    print("Téléchargement terminé.\n")
    print("Prochaines étapes :")
    print("  1. python scripts/build_network.py  (construire graphe routier)")
    print("  2. Déposer tes couches projet dans data/project_sample/")
    print("  3. python scripts/deliverable_generator.py")


if __name__ == "__main__":
    main()
