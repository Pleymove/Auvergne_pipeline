#!/usr/bin/env python3
"""
QGIS → Notion Export (Version credentials auto-détection)

Avant exécution : assure-toi d'avoir défini :
  export NOTION_TOKEN="secret_..."
  export NOTION_INBOX_DB_ID="xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx"

Ou place un fichier ~/.notion_env avec :
  NOTION_TOKEN=secret_...
  NOTION_INBOX_DB_ID=...
"""

import sys, os, tempfile, zipfile, json, subprocess
from pathlib import Path

try:
    import requests
except ImportError:
    print("⚠  'requests' manquant. Installe : pip install requests")
    sys.exit(1)

try:
    from qgis.core import QgsProject, QgsVectorFileWriter
except ImportError:
    print("⚠  Exécuter depuis la Console Python de QGIS.")
    sys.exit(1)

# ── Credentials Notion (auto-détection) ─────────────────────────────────────
def load_env_vars():
    """Charge NOTION_TOKEN et NOTION_INBOX_DB_ID depuis l'env ou ~/.notion_env."""
    token = os.environ.get("NOTION_TOKEN")
    db_id = os.environ.get("NOTION_INBOX_DB_ID")

    if not token or not db_id:
        env_file = Path.home() / ".notion_env"
        if env_file.exists():
            print(f"Lecture credentials depuis {env_file}")
            with open(env_file) as f:
                for line in f:
                    line = line.strip()
                    if '=' in line and not line.startswith('#'):
                        k, v = line.split('=', 1)
                        os.environ[k] = v
            token = os.environ.get("NOTION_TOKEN")
            db_id = os.environ.get("NOTION_INBOX_DB_ID")

    return token, db_id

NOTION_TOKEN, NOTION_DB_ID = load_env_vars()

if not NOTION_TOKEN:
    print("⚠  NOTION_TOKEN non trouvé.")
    print("   Définis-le : export NOTION_TOKEN='secret_...'")
    print("   OU crée ~/.notion_env avec : NOTION_TOKEN=secret_...")
    sys.exit(1)

if not NOTION_DB_ID:
    print("⚠  NOTION_INBOX_DB_ID non trouvé.")
    print("   Définis-le : export NOTION_INBOX_DB_ID='base_id'")
    print("   OU dans ~/.notion_env : NOTION_INBOX_DB_ID=...")
    sys.exit(1)

HEADERS = {
    "Authorization": f"Bearer {NOTION_TOKEN}",
    "Notion-Version": "2022-06-28",
    "Content-Type": "application/json",
}

# ── Helpers ──────────────────────────────────────────────────────────────────
def clean_filename(name: str) -> str:
    import re
    name = name.strip()
    name = re.sub(r'[^\w\-.]', '_', name)
    name = re.sub(r'_+', '_', name)
    return name[:60].strip('_') or "couche"

def upload_file_to_notion(file_path: Path) -> str:
    """Upload un fichier vers Notion, retourne l'URL publique."""
    if not file_path.exists():
        raise FileNotFoundError(file_path)

    filesize = file_path.stat().st_size
    filename = file_path.name

    # Si > 20 Mo, compresser
    if filesize > 20 * 1024 * 1024:
        print(f"    ⚠ {filename} > 20 Mo → compression ZIP")
        zip_path = file_path.with_suffix('.zip')
        with zipfile.ZipFile(zip_path, 'w', zipfile.ZIP_DEFLATED) as zf:
            zf.write(file_path, arcname=filename)
        upload_path = zip_path
        filename = zip_path.name
    else:
        upload_path = file_path

    url = "https://api.notion.com/v1/file_uploads"
    with open(upload_path, 'rb') as f:
        files = {'file': (filename, f, 'application/octet-stream')}
        resp = requests.post(
            url,
            headers={
                "Authorization": f"Bearer {NOTION_TOKEN}",
                "Notion-Version": "2022-06-28",
            },
            files=files
        )

    if resp.status_code not in (200, 201):
        raise Exception(f"Notion upload failed {resp.status_code}: {resp.text[:200]}")

    result = resp.json()
    return result.get('url')

def create_notion_page(layer_name: str, file_url: str, geometry_type: str, feature_count: int):
    """Crée une page dans la DB Inbox Notion."""
    data = {
        "parent": {"database_id": NOTION_DB_ID},
        "properties": {
            "Nom": {
                "title": [{"text": {"content": f"QGIS — {layer_name}"}}]
            },
            "Fichier": {
                "files": [{"name": Path(file_url).name, "external": {"url": file_url}}]
            },
            "Type": {
                "select": {"name": geometry_type}
            },
            "Entités": {
                "number": feature_count
            },
            "Exporté le": {
                "date": {"now": True}
            }
        }
    }
    resp = requests.post("https://api.notion.com/v1/pages", headers=HEADERS, json=data)
    if resp.status_code not in (200, 201):
        print(f"    ✗ Page Notion: {resp.status_code} {resp.text[:200]}")
        return None
    page = resp.json()
    return page.get('id')

# ── Main Export ──────────────────────────────────────────────────────────────
TEMP_DIR = Path(tempfile.mkdtemp(prefix="qgis_notion_"))
print(f"=== QGIS → Notion Export ===")
print(f"Répertoire temporaire : {TEMP_DIR}\n")

project = QgsProject.instance()
layers = list(project.mapLayers().values())

print(f"Projet : {project.fileName()}")
print(f"Couches : {len(layers)}\n")

exported = []
errors = []

for layer in layers:
    if layer.type().value != 0 or not layer.isValid():
        continue  # vector only

    name = layer.name()
    geom = {0: "Point", 1: "Line", 2: "Polygon"}.get(layer.geometryType(), "Autre")
    count = layer.featureCount()

    safe = clean_filename(name)
    tmp_file = TEMP_DIR / f"{safe}.gpkg"

    # Export
    err = QgsVectorFileWriter.writeAsVectorFormat(
        layer, str(tmp_file), "utf-8", layer.crs(), "GPKG", onlySelected=False
    )
    if err != QgsVectorFileWriter.NoError:
        print(f"✗ {name} → export échoué")
        errors.append(name)
        continue

    print(f"✓ {name} ({geom}, {count} entités)")

    # Upload
    try:
        url = upload_file_to_notion(tmp_file)
        page_id = create_notion_page(name, url, geom, count)
        if page_id:
            exported.append(name)
            print(f"  ↳ Notion: page créée")
        else:
            errors.append(name)
    except Exception as e:
        print(f"  ✗ Erreur Notion : {e}")
        errors.append(name)

# ── Résumé ───────────────────────────────────────────────────────────────────
print(f"\n{'='*50}")
print(f"EXPORT TERMINÉ")
print(f"{'='*50}")
print(f"Uploadées : {len(exported)}")
print(f"Erreurs    : {len(errors)}")

if exported:
    print(f"\n🔗 Vérifie ta base Notion 'Inbox' :")
    for n in exported:
        print(f"   • {n}")

if errors:
    print(f"\n⚠ Échecs : {', '.join(errors)}")

# Cleanup
try:
    import shutil
    shutil.rmtree(TEMP_DIR)
except:
    pass
