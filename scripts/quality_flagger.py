#!/usr/bin/env python3
"""
Analyse qualité du livrable :
  - Taux d'automatisation
  - Répartition des obstacles
  - Liste des ZAPA problématiques
  - Rapport pour chargés d'études
"""

import argparse
import geopandas as gpd
import pandas as pd
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent
OUTPUT_DIR = PROJECT_ROOT / "output"
REPORT_DIR = PROJECT_ROOT / "output" / "reports"

def analyze(livrable_path: Path):
    print(f"Analyse : {livrable_path.name}\n")

    gdf = gpd.read_file(livrable_path)
    total = len(gdf)
    auto = (gdf['status'] == 'AUTO').sum()
    manual = total - auto

    print(f"Total tronçons          : {total}")
    print(f"Automatisés (80% target): {auto} ({100*auto/total:.1f}%)")
    print(f"À vérifier manuellement : {manual} ({100*manual/total:.1f}%)")

    if manual > total * 0.2:
        print(f"\n⚠  ATTENTION : {manual} tronçons dépassent la cible 20% revue humaine")
        print("   Causes probables :")
        print("   - Intersection eaux sans pont nearby")
        print("   - Parcelles privées non-accessibles")
        print("   - PAA trop éloignés du réseau")
    else:
        print(f"\n✅ Taux de complexité dans la cible (< 20%)")

    # Détail obstacles
    print("\n--- Détail des FLAGS ---")
    manual_rows = gdf[gdf['status'] == 'MANUAL_REVIEW']
    if not manual_rows.empty:
        # Regrouper par ZAPA
        by_zapa = manual_rows.groupby('zapa_id').size().sort_values(ascending=False)
        print("\nZAPA les plus problématiques :")
        for zapa_id, count in by_zapa.head(10).items():
            print(f"  {zapa_id} : {count} pâtes à revoir")
    else:
        print("Aucun cas complexe détecté — tout est AUTO ! 🎉")

    # Export CSV pour chargés d'études
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    csv_out = REPORT_DIR / "qualite_livrable.csv"
    summary = {
        'total_troncons': [total],
        'automatises': [auto],
        'a_verifier': [manual],
        'taux_auto': [f"{100*auto/total:.1f}%"],
    }
    pd.DataFrame(summary).to_csv(csv_out, index=False)
    print(f"\n📊 Rapport sauvegardé : {csv_out}")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--livrable", type=str, default=str(OUTPUT_DIR / "livrable_final.gpkg"))
    args = parser.parse_args()

    analyze(Path(args.livrable))


if __name__ == "__main__":
    main()
