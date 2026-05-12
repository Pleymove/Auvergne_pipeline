# 🏗️ Projet QGIS Automatisé — NJ Sylchon

**Statut :** Prêt à l'emploi (données test incluses)
**Objectif :** Générer automatiquement 80% des tracés d'infra PAA→pâtes, flagger 20% pour validation manuelle
**Délai cible :** 2 semaines pour 500 BM

---

## 📁 Structure du projet

```
~/qgis_auvergne_automation/
├── data/
│   ├── contraints/        # Couches nationales (OSM/IGN)
│   │   ├── roads*.gpkg    # Réseau routier
│   │   ├── water*.gpkg    # Cours d'eau, lacs
│   │   └── buildings*.gpkg # Bâtiments
│   └── project_sample/    # Tes données (à déposer)
│       ├── PAA.gpkg       # Points PAA + id_ppa
│       ├── ZAPA.gpkg      # Polygones ZAPA + id_ppa
│       └── pastes.gpkg    # Points pâtes + paste_id + zapad_id
├── scripts/
│   ├── download_constraints.py  # Télécharge OSM France
│   ├── build_network.py         # Construit graphe NetworkX
│   ├── route_optimizer.py       # Calcule chemins + flags
│   ├── quality_flagger.py       # Rapport qualité
│   ├── run_pipeline.py          # Pipeline complet
│   └── qgis_integrate.py        # Intégration QGIS Processing
├── output/
│   ├── livrable_final.gpkg      # résultat principal
│   ├── road_network.pkl         # graphe sérialisé
│   └── reports/qualite_livrable.csv
└── tests/
    └── generate_test_data.py    # Données synthétiques
```

---

## 🚀 Mise en route (5 min)

```bash
cd ~/qgis_auvergne_automation

# 1. Installer dépendances (one-time)
bash install.sh
source venv/bin/activate

# 2. Tester avec données synthétiques
python tests/generate_test_data.py

# 3. Lancer pipeline complet
python scripts/run_pipeline.py

# 4. Vérifier résultats
python scripts/quality_flagger.py
```

Le livrable `output/livrable_final.gpkg` est prêt pour QGIS.

---

## 🗺️ Architecture technique

```
┌─────────────────────────────────────────────────────────────┐
│  DONNÉES PROJET (NJ Sylchon)                               │
│  ├─ PAA (points)      ──┐                                  │
│  ├─ ZAPA (polygones)   ──┼─ Jointures attributaires ──────►│
│  └─ Pâtes (points)     ──┘                                  │
└─────────────────────────┬───────────────────────────────────┘
                          │
┌─────────────────────────▼───────────────────────────────────┐
│  CONTRAINTES NATIONALES (auto-téléchargées)                │
│  ├─ Routes OSM/IGN  ──────┐                                │
│  ├─ Cours d'eau      ──────┼─ Analyse obstacles ──────────►│
│  └─ Bâtiments        ──────┘                                │
└─────────────────────────┬───────────────────────────────────┘
                          │
┌─────────────────────────▼───────────────────────────────────┐
│  RÉSEAU ROUTIER (NetworkX graph)                           │
│  Nœuds = intersections Routières                           │
│  Arêtes = tronçons (longueur, highway type)                │
└─────────────────────────┬───────────────────────────────────┘
                          │
                 [Snap PAA/pâtes au réseau]
                          │
┌─────────────────────────▼───────────────────────────────────┐
│  CALCUL PLUS COURT CHEMIN (Dijkstra)                       │
│  Source = nœud PAA                                         │
│  Cible  = nœud pâte                                        │
│  Évitement = eaux + zones privées                          │
└─────────────────────────┬───────────────────────────────────┘
                          │
              ┌───────────┴────────────┐
              │                        │
              ▼                        ▼
    ┌─────────────────┐    ┌────────────────────┐
    │  CHEMIN CLAIR   │    │  OBSTACLE DÉTECTÉ │
    │  → status=AUTO  │    │  → status=MANUAL  │
    └─────────────────┘    └────────────────────┘
              │                        │
              └───────────┬────────────┘
                          ▼
              ┌──────────────────────────┐
              │  LIVRABLE (GeoPackage)   │
              │  ├─ tracé LINESTRING     │
              │  ├─ attribut status      │
              │  └─ métadonnées PAA      │
              └──────────────────────────┘
```

---

## 📊 Résultats attendus (sur 1 zone test)

```
Total tronçons          : 150
Automatisés (80%)        : 120 (80.0%)
À vérifier manuellement  :  30 (20.0%)
```

**Cas AUTO :**
- Infra existante déjà en place (PAA→pâte dans données)
- Chemin routier sans obstacle
- Distance directe < snap tolerance

**Cas MANUAL_REVIEW :**
- Traversée fleuve sans pont à proximité
- Parcelle privée clôturée
- PAA trop éloigné du réseau (>50 m)
- Aucun chemin trouvé (zone isolée)

---

## 🛠️ Adaptation à tes données réelles

### Étape 1 — Copier tes couches

```bash
# Depuis QGIS → exporter en GeoPackage
# Ou récupérer tes fichiers existants
cp /chemin/vers/tes/fichiers/PAA.gpkg      ~/qgis_auvergne_automation/data/project_sample/
cp /chemin/vers/tes/fichiers/ZAPA.gpkg     ~/qgis_auvergne_automation/data/project_sample/
cp /chegon/vers/tes/fichiers/pastes.gpkg   ~/qgis_auvergne_automation/data/project_sample/
```

**Champs obligatoires :**
- `PAA.gpkg` → `id_ppa` (unique)
- `ZAPA.gpkg` → `id_ppa` (pour jointure) OU `id` (si pas de jointure)
- `pastes.gpkg` → `id_paste`, `zapa_id` (référence ZAPA)

### Étape 2 — Vérifier la zone

Si ta zone n'est pas en France entière, limiter téléchargement OSM :

```bash
python scripts/download_constraints.py --bbox "xmin,ymin,xmax,ymax"
```

Recuperer bbox dans QGIS :clic droit couche → Propriétés → Infos → Emprise.

### Étape 3 — Ajuster paramètres

Modifier `SNAP_TOLERANCE` dans `scripts/route_optimizer.py` selon densité réseau :
- Milieu urbain : 30 m (réseau dense)
- Milieu rural : 100 m (chemins moins précis)

### Étape 4 — Tester

```bash
python scripts/run_pipeline.py
python scripts/quality_flagger.py
```

Si >20% MANUAL, inspecter les flags :
```bash
ogrinfo output/livrable_final.gpkg -sql "SELECT * FROM livrable WHERE status='MANUAL_REVIEW' LIMIT 10"
```

---

## 🎯 Points de vigilance

1. **Snap to network** — Si PAA/pâtes trop éloignés d'une route OSM, création de
   nœuds virtuels. Cela force un trajet direct (ligne droite) qui peut traverser
   obstacles. Solution : augmenter `SNAP_TOLERANCE` ou ajouter des routes manquantes
   dans OSM (contribution Communauté).

2. **ZAPA incomplètes** — Si une pâte n'est dans aucune ZAPA, elle est ignorée.
   Vérifier jointures spatiales.

3. **Routes privées** — OSM peut manquer des cheminements privés. Ajouter une
   couche `routes_privees.gpkg` dans `data/contraints/` (prioritaire sur OSM).

4. **Fleuves sans pont** — L'algorithme détecte traversée eau → flag MANUAL.
   Si pont existe mais non dans OSM, ajouter ligne ponctuelle `ponts.gpkg`.

---

## 📈 Monitoring & debug

### Logs d'exécution

Chaque script affiche :
- Nombre d'entités chargées
- Nombre de nœuds/arêtes du graphe
- Comptage AUTO vs MANUAL

### Debug visuel dans QGIS

Charger `livrable_final.gpkg` :
- Style automatique :
  - Vert : AUTO
  - Orange : MANUAL_REVIEW
- Superposer couches contraintes (eau, parcelles) pour comprendre flags

### Export rapports

```bash
python scripts/quality_flagger.py --livrable output/livrable_final.gpkg
```

Génère :
- `output/reports/qualite_livrable.csv` (résumé)
- `output/reports/manual_cases.geojson` (cibles chargés d'études)

---

## 🔄 Intégration continue

Pour mettre à jour automatiquement chaque semaine (nouveaux PAA) :

```bash
# 1. Récupérer nouveaux PAA
# 2. Relancer pipeline
python scripts/run_pipeline.py

# 3. Notifier équipe
python scripts/quality_flagger.py | mail -s "Livrable weekly" etude@nj-sylchon.fr
```

Cron possible si données projet sur serveur partagé :
```
0 6 * * 1 cd ~/qgis_auvergne_automation && source venv/bin/activate && python scripts/run_pipeline.py
```

---

## 🆘 Support

Problèmes courants :

| Symptôme                                    | Cause probable                    | Solution                              |
|--------------------------------------------|------------------------------------|---------------------------------------|
| `FileNotFoundError: PAA.gpkg`              | Fichiers projet manquants         | Copier tes couches dans `project_sample/` |
| Trop de MANUAL (>30%)                      | Snap tolerance trop faible        | Augmenter à 100 m dans `route_optimizer.py` |
| Aucun tronçon généré                       | Pâtes hors ZAPA                   | Vérifier jointure spatiale ZAPA→pâtes |
| Erreur téléchargement OSM                  | Pas de réseau / rate limiting    | Relancer plus tard, ou utiliser .gpkg pré-téléchargés |

---

## 📞 Prochaines étapes (chez NJ Sylchon)

1. **Accès aux données projet** — récupérer exports QGIS des PAA/ZAPA/pâtes
2. **Définition bbox de travail** — zone Auvergne précise à traiter
3. **Validation test** — lancer pipeline sur un secteur pilote (10 PAA)
4. **Ajustement** — calibrer `SNAP_TOLERANCE`, ajouter couches locales
5. **Production** — batch 500 BM → livraison GeoPackage + rapport MANUAL

---

**Prêt à démarrer. J'attends tes fichiers projet pour lancer la vraie analyse. 🚀**
