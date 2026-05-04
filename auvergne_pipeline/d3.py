"""D3 distance: BAT point -> nearest reusable infrastructure.

The "D3" distance drives the AUTO_OK / TO_CREATE classification per BAT.

Important (fix vs iteration 2): D3 is measured from the **BAT point**
(building hookup at the facade) to the closest reusable-infrastructure
segment, not from the parcel boundary. The boundary is essentially always
glued to the street where the infra runs, so the boundary-based measurement
collapsed to ~0 m and over-reported AUTO_OK.

Per-BAT logic:

1. Point-in-polygon to identify the BAT's parcel (used for the
   ``BAT_HORS_CADASTRE`` / ``BAT_ENCLAVE`` flags and for the public
   shortcut). When the BAT is hors cadastre, the flag is raised but the
   distance measurement still proceeds (best-effort).
2. If the parcel is public (commune): D3 = 0 (BAT already on the public
   domain, no cordon to pull).
3. If the parcel is private and has no boundary on the public domain
   (parcel enclavee): flag ``BAT_ENCLAVE``. The measurement still runs.
4. Distance = min over reusable infra in a search envelope around the BAT
   of ``infra.distance(bat_point)``.
"""

from __future__ import annotations

import math
from typing import Optional, Tuple

import geopandas as gpd

from . import flags as flags_mod


SEUIL_D3_M = 100.0
TOUCH_TOLERANCE_M = 0.5      # floating-point tolerance for "touches public"
NEIGHBOR_TOLERANCE_M = 1.0   # tolerance to find adjacent parcels (touching)
SEARCH_RADIUS_M = 500.0      # spatial-index search envelope around the BAT


def _bat_url(bat) -> str:
    if hasattr(bat, "get"):
        val = bat.get("id_metier", None)
        if val is not None:
            try:
                if not (isinstance(val, float) and math.isnan(val)):
                    return str(val)
            except TypeError:
                return str(val)
    return f"bat#{getattr(bat, 'name', '?')}"


def _parcel_url(parcel) -> str:
    for col in ("id_metier", "idu", "parcel_id"):
        if hasattr(parcel, "get"):
            val = parcel.get(col, None)
        else:
            val = getattr(parcel, col, None)
        if val is None:
            continue
        try:
            if isinstance(val, float) and math.isnan(val):
                continue
        except TypeError:
            pass
        return str(val)
    return f"parcel#{getattr(parcel, 'name', '?')}"


def _candidates(sindex, infra: gpd.GeoDataFrame, geom, search_radius_m: float = SEARCH_RADIUS_M):
    """Return integer positions of infra rows whose envelope is near geom."""
    if sindex is None or infra is None or infra.empty:
        return []
    envelope = geom.buffer(search_radius_m)
    try:
        return list(sindex.query(envelope))
    except (TypeError, AttributeError):
        return list(sindex.intersection(envelope.bounds))


def _touches_public(boundary, public_geom) -> bool:
    if public_geom is None or public_geom.is_empty:
        return False
    if boundary.intersects(public_geom):
        return True
    return boundary.distance(public_geom) <= TOUCH_TOLERANCE_M


def measure_d3(
    bat,
    parcelles_classifiees: gpd.GeoDataFrame,
    public_geom,
    reusable_infra: gpd.GeoDataFrame,
    sindex_infra,
    flag_collector: Optional["flags_mod.FlagCollector"] = None,
) -> Tuple[Optional[float], Optional[int], Optional[str]]:
    """Distance from the BAT point to the nearest reusable infra segment.

    Returns
    -------
    (distance_m, infra_idx, parcel_url)
        - ``distance_m`` in metres or ``None`` if no infra is within
          ``SEARCH_RADIUS_M`` of the BAT.
        - ``infra_idx`` is the row index (label) of the closest reusable
          infra, or ``None`` when not measurable / on a public parcel.
        - ``parcel_url`` is a string identifier for the BAT's parcel, or
          ``None`` when the BAT is hors cadastre.
    """
    bat_geom = bat.geometry

    # ---- Step 1: identify the BAT's parcel (for flags + public shortcut) --
    purl: Optional[str] = None
    if parcelles_classifiees is None or parcelles_classifiees.empty:
        if flag_collector is not None:
            flag_collector.add("BAT_HORS_CADASTRE", target_url=_bat_url(bat))
    else:
        contains = parcelles_classifiees[
            parcelles_classifiees.geometry.contains(bat_geom)
        ]
        if contains.empty:
            if flag_collector is not None:
                flag_collector.add("BAT_HORS_CADASTRE", target_url=_bat_url(bat))
        else:
            parcel = contains.iloc[0]
            purl = _parcel_url(parcel)

            if bool(parcel.get("public", False)):
                return 0.0, None, purl

            boundary = parcel.geometry.boundary
            if not _touches_public(boundary, public_geom):
                if flag_collector is not None:
                    flag_collector.add("BAT_ENCLAVE", target_url=purl)

    # ---- Step 2: distance BAT (point) -> nearest reusable infra ----------
    cand_pos = _candidates(sindex_infra, reusable_infra, bat_geom, SEARCH_RADIUS_M)
    if not cand_pos:
        return None, None, purl

    candidates = reusable_infra.iloc[cand_pos]
    distances = candidates.geometry.distance(bat_geom)
    if distances.empty:
        return None, None, purl

    i_min_pos = int(distances.values.argmin())
    return float(distances.iloc[i_min_pos]), int(candidates.index[i_min_pos]), purl


def classify_bat(distance: Optional[float]) -> str:
    """AUTO_OK if distance <= seuil D3, else TO_CREATE (None counts as TO_CREATE)."""
    if distance is None:
        return "TO_CREATE"
    if distance <= SEUIL_D3_M:
        return "AUTO_OK"
    return "TO_CREATE"
