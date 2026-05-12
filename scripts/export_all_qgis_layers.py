#!/usr/bin/env python3
"""
QGIS Batch Export — Export ALL vector layers from the current project to GeoPackage.

Usage dans QGIS :
  1. Ouvrir ton projet .qgz
  2. Plugins → Python Console
  3. Coller ce script et exécuter

Exporte toutes les couches vectorielles dans :
  ~/qgis_auvergne_automation/data/project_sample/
avec un nom de fichier propre (sans espaces, sans accents).
"""

import sys
import os
from pathlib import Path

# ── Chemin projet automation (à adapter si besoin) ──────────────────────────
AUTOMATION_DIR = Path.home() / "qgis_auvergne_automation"
OUTPUT_DIR = AUTOMATION_DIR / "data" / "project_sample"
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

print(f"=== QGIS Batch Export ===")
print(f"Projet QGIS : {QgsProject.instance().fileName()}")
print(f"Export vers : {OUTPUT_DIR}\n")

# ── Imports QGIS ────────────────────────────────────────────────────────────
try:
    from qgis.core import (
        QgsProject,
        QgsVectorLayer,
        QgsFeatureRequest,
    )
except ImportError:
    print("⚠  Ce script doit être exécuté DEPUIS QGIS (Console Python).")
    print("   Ouvre ton projet .qgz, puis Plugins → Python Console.")
    sys.exit(1)

# ── Nettoyage nom de fichier ────────────────────────────────────────────────
import re
def clean_filename(name: str) -> str:
    """Nettoie un nom de couche pour en faire un nom de fichier valide."""
    name = name.strip()
    # Remplacer espaces et caractères spéciaux par underscore
    name = re.sub(r'[^\w\-.]', '_', name)
    # Supprime underscores multiples
    name = re.sub(r'_+', '_', name)
    # Supprime underscores début/fin
    name = name.strip('_')
    # Limite longueur
    if len(name) > 60:
        name = name[:60]
    return name or "couche"

# ── Récupération toutes les couches ─────────────────────────────────────────
project = QgsProject.instance()
layers = list(project.mapLayers().values())

print(f"Couches trouvées : {len(layers)}\n")

exported = []
errors = []

for layer in layers:
    layer_name = layer.name()
    layer_id = layer.id()
    layer_type = layer.__class__.__name__

    # On n'exporte que les couches vectorielles (pas raster, pas groupe)
    if not isinstance(layer, QgsVectorLayer):
        print(f"  ◯ {layer_name} ({layer_type}) → ignoré (non-vectoriel)")
        continue

    if not layer.isValid():
        print(f"  ⚠ {layer_name} → couche invalide, ignorée")
        continue

    # Nom de fichier
    clean_name = clean_filename(layer_name)
    # Ajoute extension .gpkg si pas déjà présent
    if not clean_name.lower().endswith('.gpkg'):
        clean_name += '.gpkg'
    output_path = OUTPUT_DIR / clean_name

    # Si fichier existe, on l'écrase
    if output_path.exists():
        print(f"  ↻ {layer_name} → existe déjà, écrasement")

    # Export
    try:
        # Options : transformer en UTF-8, exporter tous les champs
        options = QgsVectorFileWriter.SaveVectorOptions()
        options.driverName = "GPKG"
        options.fileEncoding = "UTF-8"
        options.onlySelectedFeatures = False

        # Note : QgsVectorFileWriter.writeAsVectorFormatV3 est la méthode moderne
        # mais selon ta version QGIS, on peut utiliser writeAsVectorFormat
        # Testons les deux
        try:
            # QGIS 3.22+
            error = QgsVectorFileWriter.writeAsVectorFormatV3(
                layer,
                str(output_path),
                QgsProject.instance().transformContext(),
                options
            )
        except AttributeError:
            # QGIS < 3.22 : writeAsVectorFormat
            (res, err) = QgsVectorFileWriter.writeAsVectorFormat(
                layer,
                str(output_path),
                "utf-8",
                layer.crs(),
                "GPKG",
                onlySelected=False
            )
            error = err

        if error and error != QgsVectorFileWriter.NoError:
            raise Exception(str(error))

        print(f"  ✓ {layer_name} → {clean_name}")
        exported.append((layer_name, output_path))
    except Exception as e:
        print(f"  ✗ {layer_name} → ÉCHEC : {e}")
        errors.append((layer_name, str(e)))

# ── Rapport final ───────────────────────────────────────────────────────────
print(f"\n{'='*50}")
print(f"EXPORT TERMINÉ")
print(f"{'='*50}")
print(f"Exportés : {len(exported)}")
print(f"Erreurs  : {len(errors)}")

if exported:
    print("\n📁 Fichiers créés dans :")
    print(f"   {OUTPUT_DIR}/")
    for name, path in exported:
        print(f"   - {path.name}")

if errors:
    print("\n⚠  Erreurs :")
    for name, msg in errors:
        print(f"   - {name}: {msg}")

# Suggestion pour la suite
print(f"\n🎯 Prochaines étapes :")
print(f"  1. Vérifier les fichiers dans {OUTPUT_DIR}")
print(f"  2. Identifier les couches principales :")
print(f"     - PAA (points)")
print(f"     - ZAPA (polygones)")
print(f"     - pâtes / pastes / extrémités (points)")
print(f"  3. Renommer si besoin pour correspondre aux noms attendus :")
print(f"     PAA.gpkg, ZAPA.gpkg, pastes.gpkg")
print(f"\n  4. Lancer le pipeline :")
print(f"     python scripts/run_pipeline.py")
