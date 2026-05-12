# 📤 Export depuis QGIS — Mode d'emploi

## ⚡ Méthode recommandée (le + simple)

### Étape 1 — Dans QGIS

1. Ouvre ton projet `.qgz` fourni par les collègues
2. Menu : **Plugins → Python Console** (ou `Ctrl+Alt+P`)
3. Clique sur l'onglet **"Editor"** (icône crayon)
4. Colle le script ci-dessous
5. Clic sur **▶ Run** (triangle vert)

```python
# ── COPIE CE BLOC DANS LA CONSOLE PYTHON QGIS ──────────────────────────────
import sys
from pathlib import Path

AUTOMATION_DIR = Path.home() / "qgis_auvergne_automation"
PROJECT_SAMPLE_DIR = AUTOMATION_DIR / "data" / "project_sample"
PROJECT_SAMPLE_DIR.mkdir(parents=True, exist_ok=True)

from qgis.core import QgsProject, QgsVectorFileWriter
import re

def clean_filename(name):
    name = name.strip()
    name = re.sub(r'[^\w\-.]', '_', name)
    name = re.sub(r'_+', '_', name)
    name = name.strip('_')
    return name[:60] or "couche"

print("=== Export toutes couches QGIS → GeoPackage ===\n")
project = QgsProject.instance()
layers = list(project.mapLayers().values())

exported = []
for layer in layers:
    if layer.type().value != 0:  # 0 = vector
        continue
    if not layer.isValid():
        continue

    clean_name = clean_filename(layer.name()) + '.gpkg'
    out_path = PROJECT_SAMPLE_DIR / clean_name

    options = QgsVectorFileWriter.SaveVectorOptions()
    options.driverName = "GPKG"
    options.fileEncoding = "UTF-8"

    err = QgsVectorFileWriter.writeAsVectorFormatV3(
        layer, str(out_path), project.transformContext(), options
    )

    if err == QgsVectorFileWriter.NoError:
        print(f"✓ {layer.name()} → {clean_name}")
        exported.append(layer.name())
    else:
        print(f"✗ {layer.name()} → ERREUR : {err}")

print(f"\n✅ {len(exported)} couches exportées vers {PROJECT_SAMPLE_DIR}")
print("\nProchaines étapes :")
print("1. Exécuter le mapping automatique :")
print("   python scripts/map_qgis_layers.py  (dans un terminal)")
print("2. Lancer le pipeline :")
print("   python scripts/run_pipeline.py")
```

### Étape 2 — Ferme QGIS (facultatif mais recommandé)
Pour éviter les verrous de fichiers, ferme QGIS après l'export.

### Étape 3 — Mapping automatique (terminal)

```bash
cd ~/qgis_auvergne_automation
source venv/bin/activate
python scripts/map_qgis_layers.py
```

Ce script lit les noms des couches exportées et les renomme selon les rôles attendus :
- `PAA.gpkg`
- `ZAPA.gpkg`
- `pastes.gpkg`
- `infra_existante.gpkg` (si trouvée)

### Étape 4 — Pipeline complet

```bash
python scripts/run_pipeline.py
python scripts/quality_flagger.py
```

Le livrable `output/livrable_final.gpkg` est prêt pour QGIS.

---

## 🔍 Vérification manuelle (si mapping automatique échoue)

Si le mapping automatique ne reconnaît pas certaines couches, voici comment vérifier :

1. Dans `data/project_sample/`, liste les fichiers exportés :
   ```bash
   ls -lh ~/qgis_auvergne_automation/data/project_sample/
   ```

2. Pour chaque fichier, regarde le contenu (type de géométrie) :
   ```bash
   ogrinfo -al -so ~/qgis_auvergne_automation/data/project_sample/NOM.gpkg
   ```
   Sortie attendue :
   - `PAA.gpkg` → `Point` (points)
   - `ZAPA.gpkg` → `Polygon` (polygones)
   - `pastes.gpkg` → `Point` (points)

3. Si les noms sont incorrects, renomme manuellement :
   ```bash
   cd ~/qgis_auvergne_automation/data/project_sample/
   mv "COUCHE_POURRIE.gpkg" PAA.gpkg   # si c'est bien les points PAA
   ```

**Astuce :** dans QGIS, regarde le **type de géométrie** en bas à droite
quand tu sélectionnes une couche : 📍 Point, ⬜ Polygon, 📐 Line.

---

## 🎯 Identification des rôles (critères)

| Rôle  | Géométrie | Nom QGIS typique | Champ clé |
|-------|-----------|------------------|-----------|
| PAA   | Point     | `PAA`, `PointAttache`, `PA`, `PointAmont` | `id_ppa`, `code_maa` |
| ZAPA  | Polygon   | `ZAPA`, `ZoneArriere`, `SecteurPAA` | `id_ppa`, `id_zapa` |
| Pâtes | Point     | `Pâte`, `Bâti`, `Terminal`, `Extrém`, `PT`, `PointFinal` | `id_paste`, `id_bati` |
| Infra | Line      | `Infra`, `Tracé`, `Câble`, `Fibre`, `Réseau` | (optionnel) |

Si une couche a un nom ambigu, garde-la dans `project_sample/` et on l'ajoutera manuellement dans le script `route_optimizer.py`.

---

## 🚨 Points de vigilance

1. **Projet QGIS avec connexion base** — Si le projet est lié à PostGIS/SQL Server, les couches peuvent être **verrouillées**. Dans ce cas :
   - Avant export, dans le panneau **Couches**, clic droit → **"Rendre la couche editable"** si nécessaire
   - Ou **"Sauvegarder la couche sous…"** vers un GeoPackage temporaire

2. **Filtres / sous-ensemble** — L'export avec `writeAsVectorFormatV3` exporte **toutes** les entités, pas seulement celles affichées. Si tu veux exporter uniquement les entités sélectionnées, change `onlySelectedFeatures` à `True` dans le script.

3. **Système de coordonnées** — Le script conserve le CRS d'origine. Idéalement toutes les couches doivent être en **Lambert 93 (EPSG:2154)**. Si une couche est en WGS84 (EPSG:4326), tu peux la reprojeter dans QGIS avant export :
   - Clic droit couche → **Exporter → Sauvegarder sous…**
   - Choisir CRS : `EPSG:2154 – RGF93 / Lambert-93`

4. **Champs sensibles** — Certaines couches peuvent contenir des champs confidentiels (propriétaires, adresses). Si c'est le cas, tu peux les supprimer dans QGIS avant export :
   - Table d'attributs → bouton **"Supprimer la colonne"**
   - Ou éditer la table et masquer les champs sensibles

---

## 📁 Structure finale attendue

```
qgis_auvergne_automation/
└── data/
    └── project_sample/
        ├── PAA.gpkg               ← points (OBLIGATOIRE)
        ├── ZAPA.gpkg              ← polygones (OBLIGATOIRE)
        ├── pastes.gpkg            ← points (OBLIGATOIRE)
        └── infra_existante.gpkg   ← lignes (optionnel)
```

Si un fichier manque, le script `route_optimizer.py` plantera avec un message clair.

---

## 🐛 Dépannage

**"Impossible d'écrire le GeoPackage"**
→ Vérifie que `project_sample/` est inscriptible :
```bash
mkdir -p ~/qgis_auvergne_automation/data/project_sample
chmod 755 ~/qgis_auvergne_automation/data/project_sample
```

**"Couche invalide" dans QGIS**
→ La couche n'est pas vectorielle ou la connexion base est cassée.
   Essaye de faire **"Sauvegarder sous…"** manuellement pour cette couche,
   puis ré-exporte via le script.

**"Aucun fichier .gpkg trouvé" après export**
→ L'a exporté dans un autre dossier ? Vérifie le chemin `AUTOMATION_DIR`
   dans le script. Si ton `qgis_auvergne_automation` n'est pas dans `~/`,
   ajuste le chemin :

```python
AUTOMATION_DIR = Path("/chemin/vers/qgis_auvergne_automation")
```

---

## 🆘 L'export échoue ? Solution de secours

En dernier recours, exporte manuellement depuis QGIS :

1. Panneau **Couches** → clic droit sur `PAA`
2. **Exporter → Sauvegarder les entités sous…**
3. Format : **GeoPackage**
4. Fichier : `PAA.gpkg` dans `~/qgis_auvergne_automation/data/project_sample/`
5. Décocher **"Seulement les entités sélectionnées"**
6. **OK**
7. Répéter pour ZAPA et pâtes

C'est plus long (3 min), mais ça marche à 100%.

---

**Une fois les fichiers prêts, lance le pipeline et dis-moi le résultat !** 🚀
