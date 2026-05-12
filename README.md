# Auvergne avant-vente pipeline

Pipeline Python qui transforme le GPKG local `auvergne_local.gpkg` en livrables QGIS
(`livrable_pa`, `livrable_zapa`, `livrable_infra`) plus une liste de cas a verifier.

Document de design: page Notion **Phase 3 - Design pipeline Python (avant-vente Auvergne)**.

> Etat: **iteration 1** -- `main.py` + `loader.py` + `filters.py`. Les modules `d3.py`,
> `routing.py`, `writer.py`, `flags.py`, `reporter.py` arrivent dans les iterations suivantes,
> apres premiers retours de Pierre sur les 5 SRO pilotes.

## Lancement rapide (GUI)

Double-cliquez sur `start.bat`. Une fenetre s'ouvre :
- Choisissez le fichier GPKG
- Cochez les SRO a traiter
- Cliquez "Lancer"

Le mode CLI reste disponible via `run_pipeline.bat --all-pilots` ou
`run_pipeline.bat --sros <ids>`.

> Note technique : la GUI est implementee en **PyQt6** (framework GUI natif
> de QGIS, livre par OSGeo4W). **Aucune dependance supplementaire** n'est
> requise -- PyQt6 est deja present dans l'environnement charge par
> `o4w_env.bat`.

## Structure

```
auvergne_pipeline/
  __init__.py
  config.py           # chemins GPKG, seuils (D3=100m), constantes, SRO pilotes
  loader.py           # lit les couches utiles, clipees a l'emprise du SRO
  filters.py          # 4 filtres infra reutilisable -> 1 GeoDataFrame
  main.py             # CLI: --sro / --all-pilots / --list-sros
  tests/
    test_filters.py   # tests purs sur GeoDataFrames synthetiques
    test_loader.py    # tests d'integration (skip auto si GPKG absent)
run_pipeline.bat      # lanceur Windows (CRLF + UTF-8 sans BOM + ASCII)
README.md
```

## Prerequis

- Windows + QGIS 4.0.1 installe (Python embarque, pas besoin d'installer Python a part).
- GPKG genere par le script "Clone local d'un projet" :
  `~\Desktop\auvergne_local\auvergne_local.gpkg` (12 couches, EPSG:2154).

Le pipeline tourne **uniquement** dans l'environnement Python embarque QGIS via
`o4w_env.bat`. Aucune dependance externe a installer : GeoPandas, Shapely 2.x,
Fiona, NumPy et NetworkX sont deja livres avec QGIS.

## Lancement (Windows)

Depuis la racine du repo :

```bat
run_pipeline.bat --sro 63149/M06/PMZ/42478
run_pipeline.bat --all-pilots
run_pipeline.bat --list-sros
```

Variables d'environnement optionnelles :

| Variable           | Defaut                                                            | Effet                              |
| ------------------ | ----------------------------------------------------------------- | ---------------------------------- |
| `QGIS_ROOT`        | `C:\Program Files\QGIS 4.0.1`                                     | Racine QGIS (contient `bin\o4w_env.bat`) |
| `AUVERGNE_GPKG`    | `%USERPROFILE%\Desktop\auvergne_local\auvergne_local.gpkg`        | Chemin du GPKG local               |

Si `o4w_env.bat` n'est pas a l'emplacement par defaut, surcharger `QGIS_ROOT` avant l'appel :

```bat
set "QGIS_ROOT=D:\OSGeo4W"
run_pipeline.bat --all-pilots
```

## Lancement direct (sans .bat)

Quand le shell est deja dans l'env QGIS (ex: OSGeo4W Shell) :

```bash
python -m auvergne_pipeline.main --sro 63149/M06/PMZ/42478
```

## SRO pilotes (Puy-de-Dome)

Definis dans `auvergne_pipeline/config.PILOT_SROS` :

- `63149/M06/PMZ/42478` -- end-to-end basique
- `63257/QSB/PMZ/56934` -- ratio public/prive
- `63210/M06/PMZ/29655` -- routing / connexite
- `63048/QBO/PMZ/56826` -- BAT orphelin -> creation PA
- `63258/LLW/PMZ/24228` -- seuil D3 100 m

## Modules livres dans cette PR

### `loader.py`
Charge les 12 couches utiles, clipees a la `bbox` du `za_sro` (avec un buffer de
150 m pour les parcelles et 200 m pour l'infra existante, pour ne pas couper la
mesure D3 en bord de SRO). Verifie l'unicite du SRO et expose `list_available_sros`
pour le mode `--list-sros`.

### `filters.py`
Quatre filtres infra reutilisable, conformes au CDC + symbologie QGIS :
- ATHD: `dispopp_ar >= 1`
- BT ENEDIS: exclusion des cables enterres
- FT/PIT communal Orange: exclusion pleine-terre `(statut='E' AND mode_pose=4)`
- Cheminement RIP NGE: GC transport construit `(cm_avct+cm_typ_imp == 'C7')` et
  `cm_typelog in {TR, TD}`

`build_reusable_infra(layers)` agrege les 4 sources en une GeoDataFrame avec une
colonne `src` pour la tracabilite et les valeurs par defaut `mode_pose` / `statut`
attendues cote livrable.

### `main.py`
CLI minimal :
- `--sro CODE` (repetable) ou `--all-pilots` ou `--list-sros`
- `--gpkg PATH` pour pointer un autre GPKG
- Resume par SRO et recap final avec marqueurs `[OK]` / `[!]` / `[X]`.

## Tests

Les tests utilisent `pytest` (livre avec QGIS 4.0.1).

```bat
run_pipeline.bat -m pytest auvergne_pipeline\tests -v
```

Ou directement :

```bash
python -m pytest auvergne_pipeline/tests -v
```

- `test_filters.py` -- tests purs sur GeoDataFrames synthetiques (toujours executes).
- `test_loader.py` -- tests d'integration sur les 5 SRO pilotes (skip automatique
  si le GPKG local n'est pas present).

## Iterations a venir

D'apres la design page :

1. **Iteration 2 (apres retours Pierre)** : `parcelles.py`, `orphans.py`, `d3.py`
2. **Iteration 3** : `routing.py`, `writer.py`
3. **Iteration 4** : `flags.py`, `reporter.py` (export CSV / push Notion)

## Conventions

- CRS projet : **EPSG:2154** partout (metres natifs, aucune reprojection).
- Pas d'accent ni d'emoji dans les `.bat` / `.ps1` (ASCII pur, CRLF, UTF-8 sans BOM).
- Logs : marqueurs `[OK]` (succes), `[!]` (warning), `[X]` (erreur).
- Branches : `feature/auvergne-pipeline-XXX`. **Pas de merge direct** -- chaque
  iteration passe par une PR vers `main` que Pierre relit et merge depuis GitHub.
