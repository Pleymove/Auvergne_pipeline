#!/usr/bin/env python3
"""
Pipeline complet : téléchargement contraintes → réseau → livrable.
Usage :
  python scripts/run_pipeline.py --bbox "xmin,ymin,xmax,ymax"
"""

import argparse
import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent
SCRIPTS = PROJECT_ROOT / "scripts"

def run_step(step_name: str, command: list):
    print(f"\n{'='*50}")
    print(f"ÉTAPE {step_name}")
    print('='*50)
    result = subprocess.run(command, cwd=PROJECT_ROOT)
    if result.returncode != 0:
        print(f"✗ Étape '{step_name}' échouée")
        sys.exit(result.returncode)
    print(f"✓ Étape '{step_name}' terminée\n")

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--bbox", type=str, default=None)
    parser.add_argument("--skip-download", action="store_true")
    args = parser.parse_args()

    steps = []

    if not args.skip_download:
        steps.append(("1 - TÉLÉCHARGEMENT CONTRAINTES", [
            sys.executable, str(SCRIPTS / "download_constraints.py")
        ] + (["--bbox", args.bbox] if args.bbox else [])))

    steps.append(("2 - CONSTRUCTION RÉSEAU", [
        sys.executable, str(SCRIPTS / "build_network.py")
    ]))

    steps.append(("3 - GÉNÉRATION LIVRABLE", [
        sys.executable, str(SCRIPTS / "route_optimizer.py")
    ]))

    for name, cmd in steps:
        run_step(name, cmd)

    print("\n🎉 PIPELINE TERMINÉ")
    print("Livrable :", PROJECT_ROOT / "output" / "livrable_final.gpkg")
    print("Ouvre-le dans QGIS pour vérification.")

if __name__ == "__main__":
    main()
