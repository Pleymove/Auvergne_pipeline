"""Load the layers needed for one SRO from the local GPKG.

The loader reads only what is needed inside the SRO bounding box (plus a
safety buffer for parcelles and existing infra), so a single SRO run stays
well under the memory budget even on dense rural areas.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Dict

import geopandas as gpd
from shapely.geometry import box

from . import config

log = logging.getLogger(__name__)


class SroNotFoundError(LookupError):
    """Raised when the requested SRO code does not match exactly one ZA_SRO row."""


def _read_layer(gpkg_path: Path, layer: str, **kwargs) -> gpd.GeoDataFrame:
    """Thin wrapper around `geopandas.read_file` with consistent logging."""
    log.debug("Reading layer %s from %s", layer, gpkg_path)
    return gpd.read_file(gpkg_path, layer=layer, **kwargs)


def load_sro(gpkg_path: str | Path, sro_code: str) -> Dict[str, gpd.GeoDataFrame]:
    """Load every layer needed by the pipeline, clipped to the SRO footprint.

    Parameters
    ----------
    gpkg_path:
        Path to ``auvergne_local.gpkg``.
    sro_code:
        SRO code in the canonical ``DEPT/NRO/PMZ/NUM`` form.

    Returns
    -------
    dict of str -> GeoDataFrame
        Keys: ``za_sro``, ``bal``, ``georeso_zapa``, ``georeso_pa``,
        ``parcelle``, plus the four infra layers from ``config.INFRA_LAYERS``.
    """
    gpkg_path = Path(gpkg_path)
    if not gpkg_path.exists():
        raise FileNotFoundError(f"GPKG introuvable: {gpkg_path}")

    layers: Dict[str, gpd.GeoDataFrame] = {}

    za_all = _read_layer(gpkg_path, config.LAYER_ZA_SRO)
    za = za_all[za_all["sro"] == sro_code]
    if len(za) != 1:
        raise SroNotFoundError(
            f"SRO {sro_code!r} attendu unique, trouve {len(za)} ligne(s) dans "
            f"{config.LAYER_ZA_SRO}"
        )
    layers["za_sro"] = za.reset_index(drop=True)

    za_geom = za.geometry.iloc[0]
    bbox = za.total_bounds  # xmin, ymin, xmax, ymax
    bbox_parcelle = box(*bbox).buffer(config.PARCELLE_BBOX_BUFFER_M).bounds
    bbox_infra = box(*bbox).buffer(config.INFRA_BBOX_BUFFER_M).bounds

    bal = _read_layer(gpkg_path, config.LAYER_BAL)
    layers["bal"] = bal[bal["sro"] == sro_code].reset_index(drop=True)

    zapa = _read_layer(gpkg_path, config.LAYER_GEORESO_ZAPA, bbox=tuple(bbox))
    layers["georeso_zapa"] = zapa[zapa.intersects(za_geom)].reset_index(drop=True)

    pa = _read_layer(gpkg_path, config.LAYER_GEORESO_PA, bbox=tuple(bbox))
    layers["georeso_pa"] = pa[pa.intersects(za_geom)].reset_index(drop=True)

    layers["parcelle"] = _read_layer(
        gpkg_path, config.LAYER_PARCELLE, bbox=bbox_parcelle
    ).reset_index(drop=True)

    for name in config.INFRA_LAYERS:
        layers[name] = _read_layer(gpkg_path, name, bbox=bbox_infra).reset_index(
            drop=True
        )

    log.info(
        "SRO %s charge: %d BAT / %d ZAPA / %d PA / %d parcelles / "
        "%d ATHD / %d BT / %d FT / %d cheminement",
        sro_code,
        len(layers["bal"]),
        len(layers["georeso_zapa"]),
        len(layers["georeso_pa"]),
        len(layers["parcelle"]),
        len(layers[config.LAYER_ATHD]),
        len(layers[config.LAYER_BT]),
        len(layers[config.LAYER_FT_ARCITI]),
        len(layers[config.LAYER_CHEMINEMENT]),
    )
    return layers


def list_available_sros(gpkg_path: str | Path) -> list[str]:
    """Return the distinct SRO codes present in ``za_sro`` (sorted)."""
    za = _read_layer(Path(gpkg_path), config.LAYER_ZA_SRO)
    return sorted(s for s in za["sro"].dropna().unique())
