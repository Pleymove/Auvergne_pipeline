# QGIS Auvergne Automation — NJ Sylchon

Automation de livrables pour déploiement réseau PAA → ZAPA → pâtes.

## Structure

```
qgis_auvergne_automation/
├── data/
│   ├── contraints/     # Couches France entière (routes, hydro, cadastre)
│   └── project_sample/ # Échantillons de test
├── scripts/
│   ├── download_constraints.py  # Téléchargement OSM/IGN
│   ├── build_network.py         # Construction graphe routier
│   ├── route_optimizer.py       # Calcul chemin PAA→pâtes
│   ├── deliverable_generator.py # Couche livrable QGIS
│   └── quality_flagger.py       # Détection cas complexes
├── output/                     # Livrables générés (GeoPackage)
└── tests/                      # Tests unitaires
```

## Installation

```bash
cd ~/qgis_auvergne_automation
pip install -r requirements.txt
```

## Workflow

1. **Télécharger contraintes** : `python scripts/download_constraints.py --bbox <xmin,ymin,xmax,ymax>`
2. **Préparer données projet** : placer tes couches dans `data/project_sample/`
3. **Générer livrable** : `python scripts/deliverable_generator.py`
4. **Vérifier flags** : les cas `MANUAL_REVIEW` nécessitent validation humaine
