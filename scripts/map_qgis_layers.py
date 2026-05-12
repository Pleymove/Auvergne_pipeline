#!/usr/bin/env python3
"""
QGIS — Aide au mapping des couches exportées vers les rôles attendus.

Après avoir exporté toutes les couches (export_all_qgis_layers.py),
ce script t'aide à identifier quelle couche correspond à PAA, ZAPA, pâtes,
etc., et à les renommer automatiquement pour le pipeline.

Usage :
  1. Exécuter export_all_qgis_layers.py dans QGIS
  2. Exécuter ce script (toujours dans Console Python) pour mapping auto
  3. Le script renomme les fichiers vers les noms attendus par le pipeline
"""

import sys, os, shutil
from pathlib import Path

try:
    from qgis.core import QgsProject
except ImportError:
    print("⚠  Exécuter depuis la Console Python de QGIS.")
    sys.exit(1)

AUTOMATION_DIR = Path.home() / "qgis_auvergne_automation"
PROJECT_SAMPLE_DIR = AUTOMATION_DIR / "data" / "project_sample"

print("=== Mapping automatique des couches vers rôles attendus ===\n")

# 1. Lister tous les gpkg exportés
gpkg_files = list(PROJECT_SAMPLE_DIR.glob("*.gpkg"))
if not gpkg_files:
    print(f"⚠  Aucun GeoPackage trouvé dans {PROJECT_SAMPLE_DIR}")
    print("   Exécute d'abord export_all_qgis_layers.py")
    sys.exit(1)

print(f"Fichiers trouvés : {len(gpkg_files)}\n")

# 2. Stratégie de reconnaissance par nom
# On va chercher des motifs dans les noms de couche QGIS.
# Pour ça, il faut relire le projet pour obtenir les vrais noms QGIS,
# car les fichiers exportés ont été nettoyés.

project = QgsProject.instance()
layers_dict = {layer.name(): layer for layer in project.mapLayers().values()}

# Règles de mapping
RULES = {
    'PAA':      ['paa', 'PAA', 'point_attache', 'point_attache_amont', 'PA', 'P A', ' paa '],
    'ZAPA':     ['zapa', 'ZAPA', 'zones arriere', 'zone arriere', 'secteur', 'secteur_paa'],
    'pastes':   ['pate', 'pâte', 'paste', 'pates', 'extrém', 'bâti', 'batiment', 'batîment', 'terminal', 'point_terminal', 'pt', 'point_final'],
    'infra':    ['infra', 'tracé', 'trace', 'infrastructure', 'cable', 'fibre', 'ligne', 'troncon', 'tronçon', 'réseau_existant', 'existante'],
}

# 3. Pour chaque fichier GeoPackage, essayer de retrouver la couche QGIS source
assignments = {}  # role -> fichier source
ambiguities = []

for gpkg in gpkg_files:
    # Le nom du fichier (sans extension) est censé évoquer le nom original
    filename_stem = gpkg.stem
    # On essaie de matcher avec les noms de couches QGIS
    matched_role = None
    matched_layer = None

    for layer_name, layer in layers_dict.items():
        clean_layer = clean_filename(layer_name)
        if clean_layer.lower() == filename_stem.lower():
            # C'est probablement la même couche
            for role, patterns in RULES.items():
                for pattern in patterns:
                    if pattern.lower() in layer_name.lower():
                        if matched_role is None:
                            matched_role = role
                            matched_layer = layer_name
                        else:
                            # Déjà un rôle assigné → ambigu, on garde le plus long overlap
                            pass
            break

    if matched_role:
        if matched_role in assignments:
            ambiguities.append((matched_role, matched_layer, assignments[matched_role]))
            print(f"⚠ Conflit pour rôle '{matched_role}':")
            print(f"   - {matched_layer}")
            print(f"   - {assignments[matched_role]}")
        else:
            assignments[matched_role] = (matched_layer, gpkg)
            print(f"✓ {matched_layer} → rôle : {matched_role}  ({gpkg.name})")
    else:
        print(f"○ {gpkg.name} → rôle non identifié (on garde tel quel)")

# 4. Effectuer les renommer vers les noms attendus par le pipeline
TARGET_NAMES = {
    'PAA':     'PAA.gpkg',
    'ZAPA':    'ZAPA.gpkg',
    'pastes':  'pastes.gpkg',
    'infra':   'infra_existante.gpkg',  # optionnel
}

print(f"\n{'='*50}")
print("RENOMMAGE pour le pipeline")
print(f"{'='*50}")

renamed = []
for role, target_name in TARGET_NAMES.items():
    if role in assignments:
        layer_name, src_path = assignments[role]
        dst_path = PROJECT_SAMPLE_DIR / target_name
        # Si le fichier cible existe déjà, on le supprime
        if dst_path.exists():
            dst_path.unlink()
        shutil.copy2(src_path, dst_path)
        print(f"  {layer_name}  →  {target_name}")
        renamed.append(role)
    else:
        print(f"  (non trouvé) {target_name}")

# 5. Nettoyage : supprimer les fichiers anonymes exportés si on les a mappés
# (optionnel — on peut les garder aussi)
# for role in renamed:
#     src_path = assignments[role][1]
#     if src_path.exists() and src_path.name not in TARGET_NAMES.values():
#         src_path.unlink()

print(f"\n✅ Mapping terminé.")
print(f"\n📁 Dossier prêt pour le pipeline : {PROJECT_SAMPLE_DIR}")
print("\nFichiers à utiliser par le pipeline :")
for role in ['PAA', 'ZAPA', 'pastes']:
    f = PROJECT_SAMPLE_DIR / TARGET_NAMES[role]
    if f.exists():
        print(f"  ✓ {f.name}")
    else:
        print(f"  ✗ {f.name}  MANQUANT")

if (PROJECT_SAMPLE_DIR / TARGET_NAMES['infra']).exists():
    print(f"  ✓ infra_existante.gpkg  (optionnel)")

if ambiguities:
    print("\n⚠  Ambiguïtés détectées — vérifie manuellement :")
    for role, l1, l2 in ambiguities:
        print(f"   Rôle '{role}' : '{l1}' vs '{l2}'")
