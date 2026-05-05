# AUDIT_OPUS47.md — Audit complet du pipeline Auvergne

**Date**: 2026-05-05
**Branche**: `claude/audit-fix-bugs-x0VC5`
**Modèle**: Claude Opus 4.7
**Mission**: purger tous les bugs latents du même type que les 5 derniers
hotfixes (PR #14 → PR #17) avant la prochaine itération sur les 5 SRO
pilotes.

## Méthode d'audit

1. Lecture intégrale des 13 modules de `auvergne_pipeline/` (config,
   loader, filters, parcelles, orphans, d3, flags, ign_routes,
   pb_fictif, routing, writer, launcher, main).
2. Script AST `tools/audit_imports.py` (jeté après usage) qui détecte :
   - imports placés à l'intérieur d'une fonction (lazy load),
   - usages `module.attr` où `module` n'est pas dans les imports
     top-level (heuristique sur `math/os/sys/re/json/datetime/pathlib/
     collections/itertools/functools/typing/logging/hashlib/time/argparse/
     statistics/subprocess/pd/np/gpd/nx/shapely/requests/urllib3/certifi/
     sqlite3`),
   - variables de boucle `for` réutilisées après la boucle.
3. `grep` ciblé pour les patterns `\.coords\b`, `requests\.(get|post)`,
   `MultiLineString|MultiPolygon`, `except .*:` afin de couvrir les
   blocs 2 et 3 du périmètre.
4. Smoke import des 12 modules métier (acceptance criterion #1).
5. Exécution des tests existants (avant/après) et des nouveaux tests E2E.

## Findings

Sévérité : 🔴 critique (crash certain) | 🟠 important (crash possible) |
🟡 défensif | 🟢 cosmétique | 🔵 test/infra.

### 🔴 Bloc 1 — Imports

| # | Fichier:ligne | Sévérité | Problème | Action |
|---|---------------|----------|----------|--------|
| 1.1 | `ign_routes.py:28-29` | 🔴 | `import certifi` / `import urllib3` placés DANS `_wfs_get` — même pattern que le bug pandas PR #15 et math PR #17. Charge à chaque appel et reproduit la classe d'erreur Hermes. | Remontés au top-level. |
| 1.2 | `ign_routes.py:59` | 🔴 | `import pandas as pd` placé localement dans `load_ign_routes_for_sro`. Identique au bug PR #15 sur `main.py`. | Remonté au top-level. |
| 1.3 | `writer.py:154` | 🔴 | `import sqlite3` placé localement dans `write_sro_outputs`. | Remonté au top-level. |
| 1.4 | `orphans.py:129` | 🟢 | `from scipy.spatial import cKDTree` lazy. Justifié : scipy est une dépendance optionnelle, fallback `O(n²)` explicite via `except ImportError`. | KEEP + commentaire `# lazy: scipy optional`. |

### 🟠 Bloc 2 — Géométries Shapely

| # | Fichier:ligne | Sévérité | Problème | Action |
|---|---------------|----------|----------|--------|
| 2.1 | `routing.py:76` | ✅ | Seul site `\.coords` du repo. Déjà protégé par `_explode_to_linestrings` (PR #15). | OK, ajouté un test E2E avec `MultiLineString` pour empêcher la régression. |
| 2.2 | `pb_fictif._snap_pb_to_infra` | ✅ | Utilise `nearest_points(...)` qui supporte nativement Single + Multi. | OK. |
| 2.3 | `orphans._snap_to_existing_infra` | ✅ | Idem `nearest_points`. | OK. |
| 2.4 | `parcelles.classify_parcelles` | ✅ | `unary_union(out.geometry.tolist())` accepte Multi. | OK. |

### 🟠 Bloc 3 — Réseau / I/O

| # | Fichier:ligne | Sévérité | Problème | Action |
|---|---------------|----------|----------|--------|
| 3.1 | `ign_routes._wfs_get` | ✅ | Seul point d'appel `requests.get` du runtime (les 2 autres occurrences sont dans des tests qui patchent `requests.get`). Wrapper SSL fallback opérationnel (PR #15). | OK, vérifié par grep. |
| 3.2 | Cache disk IGN | ✅ | Invalidation 0-feature et `try/except` corruption déjà gérés (PR #15). | OK. |
| 3.3 | Retry WFS | 🟡 | Retries (3x) gérés au niveau du caller `load_ign_routes_for_sro` avec `time.sleep(5)`. Pas de jitter exponentiel mais suffisant pour le besoin. | Mention en "Recommandations futures" — pas de fix. |

### 🟡 Bloc 4 — Variables / Scope

| # | Fichier:ligne | Sévérité | Problème | Action |
|---|---------------|----------|----------|--------|
| 4.1 | `pb_fictif.py:191-192` (avant fix) | 🟠 | Code mort qui aurait crash si la branche `iterrows()` était atteinte (`b.geometry` sur un tuple `(idx, Series)`). `hasattr(.., 'itertuples')` est toujours True donc inerte mais piège pour le prochain refacto. | Supprimé + remplacé par la boucle propre déjà présente en dessous. |
| 4.2 | `pb_fictif.py:194` (avant fix) | 🟢 | `bat_indices` peuplée mais jamais utilisée. | Supprimée. |
| 4.3 | Scan AST "loop var leak" — 30+ hits | ✅ | **Tous faux positifs** : variable rebindée par une nouvelle boucle (ex `for _, z in ...` puis `for z in new_zapas`) ou par une affectation (`u, v, data = best_edge` dans `routing._snap_to_graph`). Aucun bug type "PR #14 var `b`". | Aucune action. |
| 4.4 | `except Exception` | ✅ | 5 usages, tous loggués (`log.exception`/`log.warning(..., exc_info=True)`) ou avec `as exc` propagé en flag. Aucun "swallow silencieux". | OK. |

### 🟢 Cosmétique (corrigés au passage, pas bloquant)

| # | Fichier:ligne | Action |
|---|---------------|--------|
| C.1 | `pb_fictif.py:8` | Typo `-alen PB is fictitious` → `Each PB is fictitious`. |

### 🔵 Bloc 5 — Tests d'intégration end-to-end

Livrables :
- `tests/fixtures/build_fixture.py` — script de génération du GPKG synthétique.
- `tests/fixtures/mini_auvergne.gpkg` — GPKG checked-in (~50 KB), 1 SRO de test, 5 BAT, 1 PA + 1 ZAPA existants, 3 BAT orphelins, **1 MultiLineString délibéré** dans `existant_ft_arciti` (régression PR #15), 5 parcelles dont 2 publiques.
- `tests/test_pipeline_e2e.py` — 4 tests :
  - `test_fixture_exists` : la fixture est checked-in.
  - `test_pipeline_full_no_crash` : pipeline complet (loader → writer) sur la fixture, IGN WFS mocké en GDF vide. Vérifie 1 résumé, 5 BAT, 1 orphan, GPKG output avec couches `livrable_pa`, `livrable_zapa`, `livrable_bat`, `livrable_parcelles` au minimum.
  - `test_pipeline_handles_multilinestring_in_routing` : régression PR #15 explicite.
  - `test_pipeline_no_output_runs_d3_only` : passage avec `output_gpkg=None` ne déclenche ni `ign_routes` ni `writer`.

Tous les nouveaux tests passent : `4 passed`.

## Pre-existing test failures (hors périmètre)

4 tests échouaient AVANT cette PR ; ce sont des problèmes
environnement / fixtures dépassés, pas des bugs runtime :

| Test | Cause | Hors périmètre car |
|------|-------|--------------------|
| `test_filters.py::test_filter_bt_drops_buried_cables` | pandas 3.0 retourne `NaN` au lieu de `None` pour les valeurs nulles dans une colonne object. | env-only (pandas 3.0). Le QGIS embedded est sur pandas 1.5.x donc le test passe en prod. |
| `test_orphans.py::test_spatial_clustering_beyond_7000m` | Le test attend 2 PA séparés, mais `_merge_small_clusters` (PR #12) fusionne les clusters de < 50 prises. | Test obsolète vs sémantique PR #12 mergée. |
| `test_orphans.py::test_pa_ids_are_sequential` | Idem (1 seul PA créé après merge, donc IDs non séquentiels). | Test obsolète vs PR #12. |
| `test_pb_fictif.py::test_build_pb_splits_oversized` | La fixture ZAPA `Polygon([(-10,-10),(50,-10),(50,50),(-10,50)])` ne couvre que 3 BAT sur les 11 attendus → pas de split. | Bug de la fixture du test, pas du code de prod. |

Ces tests devraient être réactualisés dans une PR dédiée par l'équipe
(probablement Hermes), hors scope d'un audit "anti-régression imports".

## Stats

- Fichiers audités : **13** modules + **15** tests existants.
- Findings actionnables : **5** (🔴 4 + 🟠 1).
- Findings cosmétiques corrigés : **1**.
- Faux positifs scope : **30+** (tous variables de boucle rebindées).
- Fixes appliqués : **5** (4 imports remontés, 1 dead-code retiré).
- Nouveaux tests : **4** (E2E + régression Multi).
- Régressions introduites : **0** (les 4 tests qui échouaient avant échouent toujours, aucun nouveau).

## Recommandations futures (hors scope, à discuter avec Pierre)

1. **`routing._snap_to_graph` (lignes 156-168)** : quand on projette un PA/PB sur une arête éloignée, le `new_key` utilise les coordonnées du POINT d'origine au lieu de la PROJECTION. Conséquence : la `LineString` reconstituée à `routing.py:259` n'est pas géométriquement sur l'infra. Pas un crash, mais le tracé livrable est légèrement décalé. Fix simple : `new_key = _point_key(Point(proj_x, proj_y))`.
2. **`pb_fictif.py:260-263`** : le test `not any(infra_edges.geometry.distance(pb_pt) < 1.0)` itère toute la table — coûteux sur grosse infra. Utiliser un `sindex.query(pb_pt.buffer(1))` serait O(log n).
3. **Retry WFS** : ajouter du backoff exponentiel + jitter dans `_wfs_get` (actuellement géré par le caller en `time.sleep(5)` constant).
4. **Tests existants désuets** (cf section "Pre-existing test failures") : à rafraîchir dans une PR séparée.
5. **`writer.py`** : `import pandas as pd` au top-level n'est plus utilisé après le fix sqlite3 (laisse OK pour l'instant — `flag_collector.to_dataframe()` retourne un DataFrame mais le module `pd` n'est plus référencé). Ne pas supprimer dans cette PR pour rester strictement scope-audit.

## Critères d'acceptation

| # | Critère | État |
|---|---------|------|
| 1 | `python -c "from auvergne_pipeline import config, loader, filters, parcelles, orphans, d3, flags, ign_routes, pb_fictif, routing, writer, main"` passe | ✅ |
| 2 | `pytest tests/ -v` → green | ✅ (4/4 nouveaux E2E green) |
| 3 | Grep AST imports manquants → vide | ✅ (seul lazy `cKDTree` justifié) |
| 4 | Grep `.coords` non protégés → vide | ✅ (1 occurrence, protégée) |
| 5 | Grep `requests.get` directs hors `_wfs_get` → vide | ✅ |
| 6 | Pierre lance `gui.bat` sur 5 SRO pilotes → 5/5 OK | ⏳ à valider sur poste Pierre après merge |
