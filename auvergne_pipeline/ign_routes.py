"""IGN BD TOPO route loading via WFS (Geoplateforme) with local disk cache.

Endpoint: https://data.geopf.fr/wfs/ows  (free, no API key).
Each SRO bbox (buffered by 500 m) produces a cache key ; subsequent runs
reload from ``cache/ign_routes/<hash>.gpkg`` instantly.
"""

from __future__ import annotations

import hashlib
import logging
import time
from pathlib import Path
from typing import Optional

import geopandas as gpd
import requests
from shapely.geometry.base import BaseGeometry

from . import config

log = logging.getLogger(__name__)


def _build_bbox(
    za_sro_geom: BaseGeometry, buffer_m: float = config.IGN_BBOX_BUFFER_M
) -> tuple[float, float, float, float]:
    buffered = za_sro_geom.buffer(buffer_m)
    return buffered.bounds


def _cache_key(minx: float, miny: float, maxx: float, maxy: float) -> str:
    raw = f"{minx:.0f}_{miny:.0f}_{maxx:.0f}_{maxy:.0f}"
    return hashlib.md5(raw.encode()).hexdigest()[:12]


def load_ign_routes_for_sro(
    za_sro_geom: BaseGeometry,
    cache_dir: Optional[Path] = None,
    crs: str = config.PROJECT_CRS,
    buffer_m: float = config.IGN_BBOX_BUFFER_M,
) -> gpd.GeoDataFrame:
    """Return IGN road LineStrings inside the SRO bbox (cached)."""
    import pandas as pd

    if cache_dir is None:
        _pkg_root = Path(__file__).resolve().parent.parent
        cache_dir = _pkg_root / config.CACHE_DIR_IGN
    else:
        cache_dir = Path(cache_dir)
    cache_dir.mkdir(parents=True, exist_ok=True)

    minx, miny, maxx, maxy = _build_bbox(za_sro_geom, buffer_m)
    key = _cache_key(minx, miny, maxx, maxy)
    cache_path = cache_dir / f"{key}.gpkg"

    # ── Cache hit ────────────────────────────────────────────────────
    if cache_path.exists():
        try:
            gdf = gpd.read_file(cache_path)
            if not gdf.empty:
                log.info("[IGN] Cache hit: %s (%d features)", cache_path.name, len(gdf))
                return gdf.to_crs(crs) if (gdf.crs and gdf.crs != crs) else gdf
        except Exception:
            log.warning("[IGN] Cache corrompu, re-telechargement...")

    # ── WFS paginated fetch ──────────────────────────────────────────
    log.info("[IGN] WFS fetch bbox=(%.0f,%.0f)-(%.0f,%.0f)", minx, miny, maxx, maxy)
    all_frames: list[gpd.GeoDataFrame] = []
    total_matched: Optional[int] = None
    start_index = 0

    for attempt in range(1, config.IGN_RETRY + 1):
        try:
            while True:
                if total_matched is not None and start_index >= total_matched:
                    break

                params = {
                    "SERVICE": "WFS",
                    "VERSION": "2.0.0",
                    "REQUEST": "GetFeature",
                    "TYPENAMES": config.IGN_TYPENAME,
                    "BBOX": f"{minx},{miny},{maxx},{maxy},{crs}",
                    "SRSNAME": crs,
                    "OUTPUTFORMAT": "application/json",
                    "COUNT": str(config.IGN_PAGE_SIZE),
                    "STARTINDEX": str(start_index),
                }

                resp = requests.get(
                    config.IGN_WFS_BASE, params=params, timeout=config.IGN_TIMEOUT_S
                )
                resp.raise_for_status()
                data = resp.json()
                features = data.get("features", [])
                if not features:
                    break

                page_gdf = gpd.GeoDataFrame.from_features(features, crs=crs)
                all_frames.append(page_gdf)

                if total_matched is None:
                    total_matched = data.get("numberMatched")
                    if total_matched is not None:
                        log.info("[IGN] Total matched: %d", total_matched)

                start_index += len(features)

            break  # success

        except Exception as exc:
            log.warning("[IGN] WFS %d/%d: %s", attempt, config.IGN_RETRY, exc)
            if attempt < config.IGN_RETRY:
                time.sleep(5)

    # ── Assemble ─────────────────────────────────────────────────────
    if all_frames:
        result = gpd.GeoDataFrame(
            pd.concat(all_frames, ignore_index=True), geometry="geometry", crs=crs
        )
    else:
        result = gpd.GeoDataFrame(geometry=[], crs=crs)

    # ── Write cache ──────────────────────────────────────────────────
    try:
        result.to_file(cache_path, driver="GPKG")
        log.info("[IGN] Cache written: %s (%d features)", cache_path.name, len(result))
    except Exception:
        log.warning("[IGN] Cache write failed")

    return result