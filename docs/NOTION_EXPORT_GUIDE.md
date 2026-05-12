# 📤 Export QGIS → Notion — Guide

## ⚡ Objectif

Exporter **toutes les couches vectorielles** de ton projet QGIS (`.qgz`) directement dans ta **base Notion Inbox** sous forme de fichiers GeoPackage.

---

## 🔐 Prérequis — Credentials Notion

### Option A — Variables d'environnement (recommandé)

Avant de lancer QGIS, exporte les deux variables :

```bash
export NOTION_TOKEN="secret_..."           # Ton token Notion Personnel
export NOTION_INBOX_DB_ID="xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx"  # ID de la base Inbox
```

Pour récupérer le token :
1. Notion → **Mes intégrations** (en bas à gauche)
2. **+ Nouvelle intégration**
3. Donner un nom (ex: "QGIS Export")
4. Copier le **Token interne**

Pour l'ID de la base Inbox :
1. Ouvre ta base Notion Inbox
2. Clic **⋮** → **Copier le lien**
3. L'ID est la partie après `/` dans l'URL
   Ex: `https://www.notion.so/xxxx/Database-Inbox-xxxxxxxxxxxxxxxxxxxxxxxxxxxx`
   → `xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx`

---

### Option B — Fichier de config automatique

Tu peux créer un fichier `~/.notion_env` qui sera lu automatiquement :

```bash
mkdir -p ~/.config/notion
cat > ~/.notion_env << EOF
NOTION_TOKEN=secret_xxxxxxxxxxxxxxxxxxxxxxxx
NOTION_INBOX_DB_ID=xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx
EOF
```

Le script `export_to_notion.py` lira ce fichier si les variables ne sont pas dans l'environnement.

---

## 🚀 Utilisation

### 1. Préparer l'environnement

```bash
# Si tu n'as pas encore défini les variables, crée le fichier :
nano ~/.notion_env  # et colle tes credentials

# Puis, depuis le même terminal, lance QGIS :
qgis /chemin/vers/ton_projet.qgz
```

**Important** : QGIS doit inheriter des variables d'environnement du terminal qui l'a lancé. Si tu lances QGIS via un launcher (icône), l'environnement peut être vide. Donc lance depuis le terminal.

---

### 2. Exécuter le script dans QGIS

1. Dans QGIS, ouvre ton projet `.qgz`
2. `Plugins → Python Console`
3. Onglet **Editor** (icône 📝)
4. Colle le contenu de `scripts/export_to_notion.py` (le script complet)
5. Clic sur **▶ Run**

**Sortie attendue :**
```
=== QGIS → Notion Export ===
Projet QGIS : /home/.../projet.qgz
Couches trouvées : 15

✓ PAA (Point, 125 entités)
  ↳ Notion: page créée
✓ ZAPA (Polygon, 42 entités)
  ↳ Notion: page créée
✓ Pâtes (Point, 450 entités)
  ↳ Notion: page créée

==================================================
EXPORT TERMINÉ
==================================================
Uploadées : 15
Erreurs    : 0

🔗 Vérifie ta base Notion 'Inbox' :
  • PAA
  • ZAPA
  • Pâtes
  ...
```

---

### 3. Vérifier Notion

Rends-toi dans ta base **Inbox** Notion. Tu devrais voir :
- Une nouvelle page pour chaque couche exportée
- Propriétés : Nom, Fichier (avec lien de téléchargement GeoPackage), Type, Entités, Date
- Le fichier GeoPackage attaché (cliquer pour télécharger)

---

## 📁 Structure des fichiers exportés

Chaque couche est sauvegardée en **GeoPackage** (`.gpkg`) avant upload. Le fichier est :
- Compressé en ZIP si > 20 Mo (limite Notion upload externe)
- Uploadé vers Notion
- Supprimé du disque temporaire après upload

Aucun fichier ne reste sur ton système (sauf si erreur).

---

## ⚠️ Erreurs courantes

| Erreur | Cause | Solution |
|--------|-------|----------|
| `NOTION_TOKEN non trouvée` | Variables d'env non définies | `export NOTION_TOKEN=...` dans le terminal avant de lancer QGIS, OU créer `~/.notion_env` |
| `403 Forbidden` | Token invalide ou DB ID incorrect | Vérifier token dans Notion → intégration, vérifier ID DB |
| `File > 20 Mo` | Fichier trop volumineux | Le script compresse automatiquement en ZIP |
| `Couche invalide` | Couche non-vectorielle ou corrompue | Ignorée automatiquement |

---

## 🔍 Vérification rapide du setup

Dans un terminal, teste tes credentials :

```bash
# Vérifier que le token marche
curl -H "Authorization: Bearer $NOTION_TOKEN" \
     -H "Notion-Version: 2022-06-28" \
     "https://api.notion.com/v1/databases/$NOTION_INBOX_DB_ID"
```

Tu dois obtenir une réponse JSON avec `"object": "database"`.

---

## 🎯 Étapes suivantes (après export Notion)

1. Télécharger les GeoPackages depuis Notion (ou les laisser en ligne)
2. Les placer dans `~/qgis_auvergne_automation/data/project_sample/`
3. Renommer si besoin en `PAA.gpkg`, `ZAPA.gpkg`, `pastes.gpkg`
4. Lancer le pipeline :
   ```bash
   cd ~/qgis_auvergne_automation
   source venv/bin/activate
   python scripts/run_pipeline.py
   ```

---

## 🆘 Support

**"Le script ne trouve pas NOTION_TOKEN"** :
- Tu as lancé QGIS depuis un launcher (bureau) → l'env n'est pas propagé
- Solution : lance QGIS depuis le terminal où tu as exporté les variables

**"Aucune couche exportée"** :
- Vérifie que ton projet QGIS a bien des couches vectorielles chargées (pas juste des groupes vides)

**Upload Notion lent** :
- Si beaucoup de couches (>10) ou fichiers lourds, l'api est limitée. Attendre ou compresser manuellement

---

**Prêt ? Ouvre QGIS, colle le script dans la Console Python, et go ! 🚀**
