#!/bin/bash
# Installation du projet QGIS Auvergne Automation

set -e

echo "=== Installation QGIS Automation ==="

# 1. Vérifier Python
python3 --version || { echo "Python 3 requis"; exit 1; }

# 2. Créer venv
if [ ! -d "venv" ]; then
    echo "Création venv..."
    python3 -m venv venv
fi

# 3. Activer venv et installer
echo "Installation dépendances..."
source venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt

echo "✓ Installation terminée"
echo ""
echo "Utilisation :"
echo "  source venv/bin/activate"
echo "  python scripts/run_pipeline.py"
