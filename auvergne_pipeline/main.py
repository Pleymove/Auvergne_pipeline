"""CLI entry point for the Auvergne avant-vente pipeline.

Full pipeline (PR #14):
  1. load GPKG         (loader.py)
  2. filter infra      (filters.py)
  3. classify parcelles(parcelles.py)
  4. orphan PA/ZAPA    (orphans.py)
  5. D3 distances      (d3.py)
  6. IGN routes        (ign_routes.py)
  7. PB fictifs        (pb_fictif.py)
  8. routing PA->PB    (routing.py)
  9. writer GPKG       (writer.py)

Usage:  run_pipeline.bat --all-pilots
"""

from __future__ import annotations

import argparse
import logging
import statistics
import sys
from pathlib import Path
from typing import Iterable

import geopandas as gpd

from . import (
    config,
    filters,
    flags as flags_mod,
    ign_routes,
    loader,
    orphans,
    parcelles,
    pb_fictif,
    routing,
    writer,
)
from . import d3 as d3_mod

log = logging.getLogger("auvergne_pipeline")


def _configure_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )


def run_for_sro(
    gpkg_path: Path, sro_code: str, *, output_gpkg: Path | None = None
) -> dict:
    log.info("=== SRO %s ===", sro_code)

    # ── 1. Load ─────────────────────────────────────────────────────
    layers = loader.load_sro(gpkg_path, sro_code)

    # ── 2. Filter infra ─────────────────────────────────────────────
    reusable = filters.build_reusable_infra(layers)

    by_src = {
        s: int((reusable["src"] == s).sum()) if "src" in reusable.columns else 0
        for s in ("athd", "bt", "ft", "chem")
    }

    summary = {
        "sro": sro_code,
        "bal": len(layers["bal"]),
        "zapa": len(layers["georeso_zapa"]),
        "pa": len(layers["georeso_pa"]),
        "parcelles": len(layers["parcelle"]),
        "athd_in": len(layers[config.LAYER_ATHD]),
        "bt_in": len(layers[config.LAYER_BT]),
        "ft_in": len(layers[config.LAYER_FT_ARCITI]),
        "chem_in": len(layers[config.LAYER_CHEMINEMENT]),
        "athd_out": by_src["athd"], "bt_out": by_src["bt"],
        "ft_out": by_src["ft"], "chem_out": by_src["chem"],
        "reusable_total": len(reusable),
    }

    log.info("[INFO] %s reusable totals : athd=%d bt=%d ft=%d chem=%d -> total=%d",
             sro_code, by_src["athd"], by_src["bt"], by_src["ft"], by_src["chem"],
             summary["reusable_total"])

    # ── 3. Classify parcelles ───────────────────────────────────────
    flag_collector = flags_mod.FlagCollector(sro_code)
    za_geom = layers["za_sro"].geometry.iloc[0]
    parcelles_class, dom_pub_hors = parcelles.classify_parcelles(
        layers["parcelle"], za_geom
    )
    public_geom = parcelles.public_space_geometry(parcelles_class, dom_pub_hors)

    n_parc_total = len(parcelles_class)
    n_pub = int(parcelles_class["public"].sum()) if "public" in parcelles_class.columns else 0
    n_priv = n_parc_total - n_pub
    summary.update({"parc_pub": n_pub, "parc_priv": n_priv})
    log.info("[INFO] %s parcelles : public=%d (%.1f%%) prive=%d (%.1f%%) total=%d",
             sro_code, n_pub, (100*n_pub/n_parc_total) if n_parc_total else 0.0,
             n_priv, (100*n_priv/n_parc_total) if n_parc_total else 0.0, n_parc_total)

    # ── 4. Orphans (PA/ZAPA creation) ──────────────────────────────
    orphan_bats = orphans.detect_orphans(layers["bal"], layers["georeso_zapa"])
    new_pas, new_zapas = orphans.create_pa_for_orphans(
        orphan_bats, sro_code,
        cheminement_lines=layers.get(config.LAYER_CHEMINEMENT),
        athd_lines=layers.get(config.LAYER_ATHD),
        parcelles_classifiees=parcelles_class,
        flag_collector=flag_collector,
    )
    summary.update({"orphan_bats": len(orphan_bats), "new_pa": len(new_pas),
                    "new_zapa": len(new_zapas)})
    log.info("[INFO] %s orphans : bat_orphelins=%d new_pa_created=%d",
             sro_code, len(orphan_bats), len(new_pas))

    # ── 5. D3 ──────────────────────────────────────────────────────
    sindex = reusable.sindex if not reusable.empty else None
    d3_results: list[dict] = []
    for _, bat in layers["bal"].iterrows():
        distance, infra_idx, parcel_url = d3_mod.measure_d3(
            bat, parcelles_class, public_geom, reusable, sindex, flag_collector
        )
        d3_results.append({
            "bat_url": str(bat.get("id_metier", f"bat#{bat.name}")),
            "d3": distance, "cls": d3_mod.classify_bat(distance),
            "infra_idx": infra_idx, "parcel_url": parcel_url,
        })

    n_bat = len(d3_results)
    auto_ok = sum(1 for r in d3_results if r["cls"] == "AUTO_OK")
    to_create = n_bat - auto_ok
    measurable = [r["d3"] for r in d3_results if r["d3"] is not None]
    median, maxv = (statistics.median(measurable), max(measurable)) if measurable else (0.0, 0.0)
    counts = flag_collector.counts()
    summary.update({
        "auto_ok": auto_ok, "to_create": to_create,
        "d3_median_m": median, "d3_max_m": maxv,
        "bat_hors_cadastre": counts.get("BAT_HORS_CADASTRE", 0),
        "bat_enclave": counts.get("BAT_ENCLAVE", 0),
    })

    # Enrich bal with D3 for writer
    bat_enriched = layers["bal"].copy()
    bat_enriched["d3_m"] = None
    bat_enriched["cls_d3"] = "TO_CREATE"
    bat_enriched["parcel_url"] = ""
    for i, d in enumerate(d3_results):
        if i < len(bat_enriched):
            bat_enriched.at[bat_enriched.index[i], "d3_m"] = d["d3"]
            bat_enriched.at[bat_enriched.index[i], "cls_d3"] = d["cls"]
            bat_enriched.at[bat_enriched.index[i], "parcel_url"] = d["parcel_url"]

    log.info("[INFO] %s d3 : auto_ok=%d (%.1f%%) to_create=%d (%.1f%%) median=%.1fm max=%.1fm",
             sro_code, auto_ok, (100*auto_ok/n_bat) if n_bat else 0.0,
             to_create, (100*to_create/n_bat) if n_bat else 0.0, median, maxv)

    if not counts:
        log.info("[INFO] %s flags : aucun", sro_code)
    else:
        for ft, n in counts.most_common():
            log.info("[INFO] %s flags : %s=%d", sro_code, ft, n)

    # ── 6-9. IGN + PB + Routing + Writer (only if output is requested) ──
    if output_gpkg is not None:
        # 6. IGN routes
        ign_roads = ign_routes.load_ign_routes_for_sro(za_geom)

        # 7. Build combined PA/ZAPA view (existing + created)
        pa_rows = [
            {"id_metier": pa.get("id_metier"), "sro": sro_code, "geometry": pa.get("geometry")}
            for _, pa in layers["georeso_pa"].iterrows()
        ]
        pa_rows.extend(
            {"id_metier": p["id_metier"], "sro": sro_code, "geometry": p["geometry"]}
            for p in new_pas
        )
        pa_all = gpd.GeoDataFrame(pa_rows, geometry="geometry", crs=config.PROJECT_CRS) if pa_rows \
            else gpd.GeoDataFrame(geometry=[], crs=config.PROJECT_CRS)

        zapa_rows = [
            {"id_metier": z.get("id_metier"), "sro": sro_code, "geometry": z.get("geometry")}
            for _, z in layers["georeso_zapa"].iterrows()
        ]
        zapa_rows.extend(
            {"id_metier": z["id_metier"], "sro": sro_code, "geometry": z["geometry"]}
            for z in new_zapas
        )
        zapa_all = gpd.GeoDataFrame(zapa_rows, geometry="geometry", crs=config.PROJECT_CRS) if zapa_rows \
            else gpd.GeoDataFrame(geometry=[], crs=config.PROJECT_CRS)

        # Combine infra + IGN for PB placement and routing
        combined_edges = gpd.GeoDataFrame(
            pd.concat([reusable, ign_roads], ignore_index=True),
            geometry="geometry", crs=config.PROJECT_CRS,
        ) if not reusable.empty and not ign_roads.empty else (
            reusable if not reusable.empty else ign_roads
        )

        import pandas as pd

        # 8. PB fictifs
        pb_gdf, gc_neuf = pb_fictif.build_pb_fictifs(
            layers["bal"], pa_all, zapa_all, combined_edges, flag_collector
        )

        # 9. Routing PA→PB on combined graph
        routed_infra = routing.route_pa_to_pb(
            pa_all, pb_gdf, reusable, ign_roads, flag_collector
        )

        # Merge GC neuf into the routed infra
        if gc_neuf is not None and not gc_neuf.empty:
            routed_infra = gpd.GeoDataFrame(
                pd.concat([routed_infra, gc_neuf], ignore_index=True),
                geometry="geometry", crs=config.PROJECT_CRS,
            )

        # 10. Writer
        writer.write_sro_outputs(
            sro_code, output_gpkg,
            bal=bat_enriched,
            georeso_pa_existants=layers["georeso_pa"],
            georeso_zapa_existantes=layers["georeso_zapa"],
            new_pas=new_pas, new_zapas=new_zapas,
            pb_fictifs=pb_gdf,
            livrable_infra=routed_infra,
            parcelles=parcelles_class,
            flag_collector=flag_collector,
        )

    log.info("[OK] SRO %s traite", sro_code)
    return summary


def run_for_sros(
    gpkg_path: Path, sro_codes: Iterable[str], *, output_gpkg: Path | None = None
) -> list[dict]:
    summaries, failures = [], []
    for code in sro_codes:
        try:
            summaries.append(run_for_sro(gpkg_path, code, output_gpkg=output_gpkg))
        except Exception as exc:
            log.exception("[X] SRO %s : %s", code, exc)
            failures.append((code, str(exc)))

    log.info("--- Recap ---")
    for s in summaries:
        log.info("[OK] %s : BAT=%d ZAPA=%d PA=%d reusable=%d auto_ok=%d/%d orphans=%d new_pa=%d",
                 s["sro"], s["bal"], s["zapa"], s["pa"], s["reusable_total"],
                 s.get("auto_ok", 0), s["bal"], s.get("orphan_bats", 0), s.get("new_pa", 0))
    if output_gpkg is not None and output_gpkg.exists():
        log.info("[OK] GPKG output : %s", output_gpkg.resolve())
    for code, err in failures:
        log.warning("[!] %s : %s", code, err)
    return summaries


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(prog="auvergne_pipeline",
                                description="Pipeline avant-vente Auvergne (PR #14).")
    p.add_argument("--gpkg", type=Path, default=config.DEFAULT_GPKG,
                   help=f"GPKG local (defaut: {config.DEFAULT_GPKG}).")
    p.add_argument("--output", type=Path, default=config.DEFAULT_OUTPUT_GPKG,
                   help="GPKG de sortie.")
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--sro", action="append", help="Code SRO.")
    g.add_argument("--sros", nargs="+", metavar="CODE", help="Liste de codes SRO.")
    g.add_argument("--all-pilots", action="store_true", help="5 SRO pilotes.")
    g.add_argument("--list-sros", action="store_true", help="Lister les SRO du GPKG.")
    p.add_argument("-v", "--verbose", action="store_true")
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    _configure_logging(args.verbose)

    gpkg = Path(args.gpkg)
    if not gpkg.exists():
        log.error("[X] GPKG introuvable: %s", gpkg)
        return 2

    if args.list_sros:
        for code in loader.list_available_sros(gpkg):
            print(code)
        return 0

    output_gpkg = Path(args.output).resolve()
    output_gpkg.parent.mkdir(parents=True, exist_ok=True)
    if output_gpkg.exists():
        output_gpkg.unlink()
    log.info("[OK] Output GPKG : %s", output_gpkg)

    sros = (list(config.PILOT_SROS) if args.all_pilots
            else list(args.sros) if args.sros
            else list(args.sro or []))
    if not sros:
        log.error("[X] Aucun SRO a traiter.")
        return 2

    summaries = run_for_sros(gpkg, sros, output_gpkg=output_gpkg)
    return 0 if len(summaries) == len(sros) else 1


if __name__ == "__main__":
    sys.exit(main())