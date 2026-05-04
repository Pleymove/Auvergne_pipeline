"""CLI entry point for the Auvergne avant-vente pipeline.

Iteration 1 wires up the first half of the design page (loader + filters).
Later iterations will plug d3 / routing / writer / flags / reporter into the
same ``run_for_sro`` orchestrator.

Usage examples (Windows, via run_pipeline.bat):
    run_pipeline.bat --sro 63149/M06/PMZ/42478
    run_pipeline.bat --all-pilots
    run_pipeline.bat --list-sros
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path
from typing import Iterable

from . import config, filters, loader

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
        "reusable_total": len(reusable),
    }
    log.info(
        "[OK] SRO %s : %d trononcs reutilisables (athd=%d bt=%d ft=%d chem=%d)",
        sro_code,
        summary["reusable_total"],
        int((reusable.get("src") == "athd").sum()) if "src" in reusable.columns else 0,
        int((reusable.get("src") == "bt").sum()) if "src" in reusable.columns else 0,
        int((reusable.get("src") == "ft").sum()) if "src" in reusable.columns else 0,
        int((reusable.get("src") == "chem").sum()) if "src" in reusable.columns else 0,
    )
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
            "[OK] %s : BAT=%d  ZAPA=%d  PA=%d  reusable=%d",
            s["sro"], s["bal"], s["zapa"], s["pa"], s["reusable_total"],
        )
    for code, err in failures:
        log.warning("[!] %s : %s", code, err)
    return summaries


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="auvergne_pipeline",
        description="Pipeline avant-vente Auvergne (Phase 3, iteration 1).",
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
