# Mode d'emploi — NJ Sylchon Projet Rades

## 🎯 Objectif

Automatiser à 80% la création de la couche **livrable** qui contient :
- PAA
- ZAPA
- Pâtes (points de terminaison)
- Tracés d'infra optimisés
- Flaggage des cas nécessitant la validation d'un chargé d'études

## 📦 Prérequis

1. **Données projet** à placer dans `data/project_sample/` :

   ```
   PAA.gpkg           → points avec champ "id_ppa" (identifiant unique)
   ZAPA.gpkg          → polygones avec champ "id_ppa" (jointure) OU "id"
   pastes.gpkg        → points avec champ "paste_id" et "zapa_id"
   infra_existante.gpkg (optionnel) → tracés déjà présents
   ```

2. **Alimentation contraintes** :

   Le script `download_constraints.py` télécharge automatiquement les couches
   nationales depuis OpenStreetMap (routes, cours d'eau, bâtiments).
   Si tu as des couches IGN (BD TOPO), tu peux les placer manuellement dans
   `data/contraints/` — le script les utilisera prioritairement.

## 🚀 Installation rapide

```bash
cd ~/qgis_auvergne_automation
bash install.sh
source venv/bin/activate
```

## 🔄 Workflow standard (2 semaines)

### Semaine 1 — Infrastructure

**Jour 1-2 — Test du pipeline (données synthétiques)**
```bash
python tests/generate_test_data.py        # recrée exemple de test
python scripts/run_pipeline.py            # lance tout
python scripts/quality_flagger.py         # vérifie résultats
```
Résultat : `output/livrable_final.gpkg` dans QGIS.

**Jour 3-4 — Adaptation à tes données réelles**
- Remplacer `data/project_sample/*.gpkg` par tes exports QGIS réels
- Vérifier que les champs `id_ppa`, `paste_id`, `zapa_id` sont cohérents
- Ajuster `SNAP_TOLERANCE` dans `route_optimizer.py` (distance max pour rattacher
  un point au réseau routier, par défaut 50 m)

**Jour 5-7 — Optimisation des contraintes**
- Ajouter couches complémentaires (parcelles privées, zones protégées) dans
  `data/contraints/`
- Si tu possèdes la BD TOPO IGN, extraire les shapes pertinents et les placer
  dans `data/contraints/` avec les noms :
  - `routes_ign.gpkg` (prioritaire sur OSM)
  - `hydro_ign.gpkg`
  - `batiments_ign.gpkg`

### Semaine 2 — Raffinement

**Jour 8-9 — Taux d'automatisation**
- Exécuter sur un échantillon réel (1-2 PAA)
- Mesurer le % MANUAL_REVIEW → doit être ≤ 20%
- Si >20%, ajuster heuristiques dans `compute_path()` :
  - Augmenter `SNAP_TOLERANCE` si PAA/pâtes trop éloignés du réseau
  - Ajouter des exceptions (ex : traversée fleuve autorisée si pont < 100 m)

**Jour 10-11 — Interface**
- Le livrable GeoPackage peut être chargé directement dans QGIS
- Style prédéfini (couleurs AUTO=green, MANUAL=orange) dans `output/styles/`

**Jour 12-14 — Documentation et livraison**
- `python scripts/quality_flagger.py` → rapport Excel pour les chargés d'études
- Rédaction procédure d'utilisation

## ⚙️ Configuration

### Paramètres principaux (éditer dans `scripts/route_optimizer.py`)

```python
SNAP_TOLERANCE = 50     # m —distance max pour rattacher PAA/pâte au réseau
DRIVABLE_HIGHWAYS = {   # types OSM routables
    'motorway', 'trunk', 'primary', 'secondary',
    'tertiary', 'unclassified', 'residential', 'service'
}
```

### Taille de la zone

Le téléchargement OSM par défaut couvre toute la France (bbox `(-5,41,9.5,51.5)`).
Pour cibler uniquement ta zone de travail :

```bash
python scripts/download_constraints.py --bbox "xmin,ymin,xmax,ymax"
```

Coordonnées en WGS84 (EPSG:4326). Tu peux obtenir la bbox depuis QGIS :
clic droit sur la zone → Copier → Bbox.

## 🧪 Vérification des résultats

- Ouvrir `output/livrable_final.gpkg` dans QGIS
- Filtrer `status = 'MANUAL_REVIEW'` → revoir ces tronçons manuellement
- Exporter un shapefile séparé pour les revues terrain

## 🐛 Dépannage

**Erreur : "Fichier manquant PAA.gpkg"**
→ Déposer tes couches dans `data/project_sample/` avant de lancer.

**Erreur : "Graphe routier non trouvé"**
→ Lancer `python scripts/build_network.py` après téléchargement contraintes.

**Trop de MANUAL_REVIEW (>30%)**
→ Vérifier que :
  - `SNAP_TOLERANCE` assez grand (50 → 100 m)
  - Couches obstacles complètes (eau, parcelles)
  - Pâtes bien dans leurs ZAPA

**PAA non rattachés au réseau**
→ Le script crée des nœuds virtuels (fichier `virtual_*` dans le graphe). Vérifier
que les PAA sont proches (< 50 m) d'une route OSM.

## 📞 Besoins d'adaptation ?

- Ajout contraintes métier personnalisées → modifier `compute_path()`
- Export format différent (Shapefile, CSV) → `generate_deliverable()`
- Intégration complète QGIS Processing → utiliser `qgis_integrate.py`

Bon courage pour les 500 BM ! 🚀
