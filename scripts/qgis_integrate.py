#!/usr/bin/env python3
"""
Intégration QGIS : script exécutable depuis le Modeler ou en batch.
Charge les couches projet depuis un dossier et génère le livrable.
"""

import sys
import os
from pathlib import Path

# Ajout QGIS si présent
try:
    from qgis.core import (
        QgsApplication,
        QgsProcessingFeedback,
        QgsVectorLayer,
    )
    QGIS_AVAILABLE = True
except ImportError:
    QGIS_AVAILABLE = False
    print("⚠ QGIS non détecté — exécution en mode standalone")

import geopandas as gpd
import networkx as nx
from shapely.geometry import Point, LineString

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def run_standalone():
    """Exécution sans QGIS (Python pur)."""
    sys.path.insert(0, str(PROJECT_ROOT / "scripts"))
    from route_optimizer import load_network, load_project_data, load_obstacles, snap_to_network, compute_path, generate_deliverable

    print("=== QGIS INTEGRATION (standalone mode) ===\n")

    # Utiliser les chemins par défaut
    G = load_network()
    obstacles = load_obstacles()
    paa, zapa, pastes = load_project_data()

    output = PROJECT_ROOT / "output" / "livrable_qgis_integration.gpkg"
    generate_deliverable(paa, zapa, pastes, G, obstacles, output)

    return str(output)


def run_qgis_processing(project_folder: str, output_file: str):
    """
    Exécution depuis QGIS Processing :
    - Charge layers via QgsVectorLayer
    - Calcule avec algorithmes QGIS
    - Retourne livrable
    """
    print(f"Traitement QGIS : {project_folder} → {output_file}")

    # 1. Layers
    paa_layer = QgsVectorLayer(f"{project_folder}/PAA.gpkg", "PAA", "ogr")
    zapa_layer = QgsVectorLayer(f"{project_folder}/ZAPA.gpkg", "ZAPA", "ogr")
    pastes_layer = QgsVectorLayer(f"{project_folder}/pastes.gpkg", "pâtes", "ogr")

    if not paa_layer.isValid():
        raise RuntimeError("Couche PAA invalide")

    # 2. Jointure attributaire PAA → ZAPA par ID
    # 3. Jointure ZAPA → pâtes par ID
    # 4. Construction du graphe avec QgsNetworkAnalysis
    # 5. Calcul des plus courts chemins
    # 6. Marquage obstacles
    # 7. Export livrable

    print("✓ Mode QGIS pas encore implémenté — utilise standalone")
    return run_standalone()


if __name__ == "__main__":
    if len(sys.argv) > 1:
        # Called with arguments → QGIS processing mode
        run_qgis_processing(sys.argv[1], sys.argv[2] if len(sys.argv) > 2 else "output.gpkg")
    else:
        # Standalone
        out = run_standalone()
        print(f"\n📁 Livrable : {out}")
