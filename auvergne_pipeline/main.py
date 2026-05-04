"""CLI entry point for the Auvergne avant-vente pipeline.

Iteration 2 wires loader + filters + parcelles + orphans + d3 + flags into
``run_for_sro``. Iteration 3+ will plug routing / writer / reporter on top.

Usage examples (Windows, via run_pipeline.bat):
    run_pipeline.bat --sro 63149/M06/PMZ/42478
    run_pipeline.bat --all-pilots
    run_pipeline.bat --list-sros
"""

from __future__ import annotations

import argparse
import logging
import statistics
import sys
from pathlib import Path
from typing import Iterable

from . import (
    config,
    filters,
    flags as flags_mod,
    loader,
    orphans,
    parcelles,
)
from . import d3 as d3_mod

log = logging.getLogger("auvergne_pipeline")


def _configure_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )


def run_for_sro(gpkg_path: Path, sro_code: str) -> dict:
    """Execute iteration-1 steps for one SRO and return a small summary dict.

    The returned dict is intentionally lightweight (counts only) so it can be
    aggregated for the CLI summary without keeping every GeoDataFrame around.
    """
    log.info("=== SRO %s ===", sro_code)
    layers = loader.load_sro(gpkg_path, sro_code)
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
        "athd_out": by_src["athd"],
        "bt_out": by_src["bt"],
        "ft_out": by_src["ft"],
        "chem_out": by_src["chem"],
        "reusable_total": len(reusable),
    }

    log.info(
        "[INFO] %s reusable totals : athd=%d bt=%d ft=%d chem=%d -> total=%d",
        sro_code,
        by_src["athd"], by_src["bt"], by_src["ft"], by_src["chem"],
        summary["reusable_total"],
    )

    def _pct(out: int, in_: int) -> str:
        return f"{(100 * out / in_):5.1f}%" if in_ else "  n/a"

    log.info(
        "[INFO] %s filter retention : athd=%s bt=%s ft=%s chem=%s",
        sro_code,
        _pct(by_src["athd"], summary["athd_in"]),
        _pct(by_src["bt"], summary["bt_in"]),
        _pct(by_src["ft"], summary["ft_in"]),
        _pct(by_src["chem"], summary["chem_in"]),
    )

    # ---------- Iteration 2: parcelles / orphans / d3 / flags ----------
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
    log.info(
        "[INFO] %s parcelles : public=%d (%.1f%%) prive=%d (%.1f%%) total=%d",
        sro_code,
        n_pub, (100.0 * n_pub / n_parc_total) if n_parc_total else 0.0,
        n_priv, (100.0 * n_priv / n_parc_total) if n_parc_total else 0.0,
        n_parc_total,
    )

    orphan_bats = orphans.detect_orphans(layers["bal"], layers["georeso_zapa"])
    new_pas, new_zapas = orphans.create_pa_for_orphans(
        orphan_bats, sro_code, flag_collector=flag_collector
    )
    summary.update(
        {"orphan_bats": len(orphan_bats), "new_pa": len(new_pas), "new_zapa": len(new_zapas)}
    )
    log.info(
        "[INFO] %s orphans : bat_orphelins=%d new_pa_created=%d",
        sro_code, len(orphan_bats), len(new_pas),
    )

    sindex = reusable.sindex if not reusable.empty else None
    d3_results: list[dict] = []
    for _, bat in layers["bal"].iterrows():
        distance, infra_idx, parcel_url = d3_mod.measure_d3(
            bat, parcelles_class, public_geom, reusable, sindex, flag_collector
        )
        d3_results.append(
            {
                "bat_url": str(bat.get("id_metier", f"bat#{bat.name}")),
                "d3": distance,
                "cls": d3_mod.classify_bat(distance),
                "infra_idx": infra_idx,
                "parcel_url": parcel_url,
            }
        )

    n_bat = len(d3_results)
    auto_ok = sum(1 for r in d3_results if r["cls"] == "AUTO_OK")
    to_create = n_bat - auto_ok
    measurable = [r["d3"] for r in d3_results if r["d3"] is not None]
    median = statistics.median(measurable) if measurable else 0.0
    maxv = max(measurable) if measurable else 0.0
    counts = flag_collector.counts()
    summary.update(
        {
            "auto_ok": auto_ok,
            "to_create": to_create,
            "d3_median_m": median,
            "d3_max_m": maxv,
            "bat_hors_cadastre": counts.get("BAT_HORS_CADASTRE", 0),
            "bat_enclave": counts.get("BAT_ENCLAVE", 0),
        }
    )
    log.info(
        "[INFO] %s d3 : auto_ok=%d (%.1f%%) to_create=%d (%.1f%%) "
        "median=%.1fm max=%.1fm bat_hors_cadastre=%d bat_enclave=%d",
        sro_code,
        auto_ok, (100.0 * auto_ok / n_bat) if n_bat else 0.0,
        to_create, (100.0 * to_create / n_bat) if n_bat else 0.0,
        median, maxv,
        counts.get("BAT_HORS_CADASTRE", 0),
        counts.get("BAT_ENCLAVE", 0),
    )

    if not counts:
        log.info("[INFO] %s flags : aucun", sro_code)
    else:
        for ft, n in counts.most_common():
            log.info("[INFO] %s flags : %s=%d", sro_code, ft, n)

    log.info("[OK] SRO %s traite", sro_code)
    return summary


def run_for_sros(gpkg_path: Path, sro_codes: Iterable[str]) -> list[dict]:
    summaries: list[dict] = []
    failures: list[tuple[str, str]] = []
    for code in sro_codes:
        try:
            summaries.append(run_for_sro(gpkg_path, code))
        except Exception as exc:  # noqa: BLE001
            log.exception("[X] SRO %s : %s", code, exc)
            failures.append((code, str(exc)))

    log.info("--- Recap ---")
    for s in summaries:
        log.info(
            "[OK] %s : BAT=%d  ZAPA=%d  PA=%d  reusable=%d  auto_ok=%d/%d  "
            "orphans=%d  new_pa=%d",
            s["sro"], s["bal"], s["zapa"], s["pa"], s["reusable_total"],
            s.get("auto_ok", 0), s["bal"],
            s.get("orphan_bats", 0), s.get("new_pa", 0),
        )
    for code, err in failures:
        log.warning("[!] %s : %s", code, err)
    return summaries


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="auvergne_pipeline",
        description="Pipeline avant-vente Auvergne (Phase 3, iteration 2).",
    )
    p.add_argument(
        "--gpkg",
        type=Path,
        default=config.DEFAULT_GPKG,
        help=f"Chemin vers le GPKG local (defaut: {config.DEFAULT_GPKG}).",
    )
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--sro", action="append", help="Code SRO (peut etre repete).")
    g.add_argument(
        "--all-pilots",
        action="store_true",
        help="Traite les 5 SRO pilotes (config.PILOT_SROS).",
    )
    g.add_argument(
        "--list-sros",
        action="store_true",
        help="Liste les SRO disponibles dans le GPKG et sort.",
    )
    p.add_argument("-v", "--verbose", action="store_true", help="Logs DEBUG.")
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

    sros = list(config.PILOT_SROS) if args.all_pilots else list(args.sro or [])
    if not sros:
        log.error("[X] Aucun SRO a traiter.")
        return 2

    summaries = run_for_sros(gpkg, sros)
    return 0 if len(summaries) == len(sros) else 1


if __name__ == "__main__":
    sys.exit(main())
