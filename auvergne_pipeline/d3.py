"""D3 distance: BAT parcel boundary -> nearest reusable infrastructure.

The "D3" distance drives the AUTO_OK / TO_CREATE classification per BAT.
For each BAT we:

1. Find the parcel that contains the BAT point (point-in-polygon).
2. If the parcel is public (commune): D3 = 0.
3. Else (private parcel): take its boundary, snap to the public side
   (intersect / 0.5 m tolerance with ``public_geom``).
   - If the parcel is enclaved (no boundary on public), walk up to two
     levels of adjacent parcels to find one that touches public; flag
     ``BAT_ENCLAVE``.
4. Measure the minimum distance from the (possibly neighbour) boundary to
   the nearest reusable infra segment via the spatial index.
"""

from __future__ import annotations

import math
from typing import Optional, Tuple

import geopandas as gpd

from . import flags as flags_mod


SEUIL_D3_M = 100.0
TOUCH_TOLERANCE_M = 0.5      # floating-point tolerance for "touches public"
NEIGHBOR_TOLERANCE_M = 1.0   # tolerance to find adjacent parcels (touching)
SEARCH_RADIUS_M = 500.0      # spatial-index search envelope around the boundary


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


def _enclave_target_boundary(
    parcel,
    parcelles: gpd.GeoDataFrame,
    public_geom,
    max_levels: int = 2,
):
    """Walk up to ``max_levels`` of neighbours to find one whose boundary touches public."""
    visited = {parcel.name}
    frontier = [parcel]
    for _ in range(max_levels):
        next_frontier = []
        for p in frontier:
            buf = p.geometry.buffer(NEIGHBOR_TOLERANCE_M)
            mask = parcelles.geometry.intersects(buf) & ~parcelles.index.isin(visited)
            for idx in parcelles[mask].index:
                visited.add(idx)
                neighbor = parcelles.loc[idx]
                nb_boundary = neighbor.geometry.boundary
                if _touches_public(nb_boundary, public_geom):
                    return nb_boundary
                next_frontier.append(neighbor)
        frontier = next_frontier
        if not frontier:
            break
    return None


def measure_d3(
    bat,
    parcelles_classifiees: gpd.GeoDataFrame,
    public_geom,
    reusable_infra: gpd.GeoDataFrame,
    sindex_infra,
    flag_collector: Optional["flags_mod.FlagCollector"] = None,
) -> Tuple[Optional[float], Optional[int], Optional[str]]:
    """Distance from BAT's parcel boundary (private side) to nearest reusable infra.

    Returns
    -------
    (distance_m, infra_idx, parcel_url)
        - ``distance_m`` in metres or ``None`` if not measurable.
        - ``infra_idx`` is the row index (label) of the closest reusable infra,
          or ``None`` if not applicable / not measurable.
        - ``parcel_url`` is a string identifier for the BAT's parcel, or
          ``None`` when the BAT is hors cadastre.
    """
    bat_geom = bat.geometry

    if parcelles_classifiees is None or parcelles_classifiees.empty:
        if flag_collector is not None:
            flag_collector.add("BAT_HORS_CADASTRE", target_url=_bat_url(bat))
        return None, None, None

    contains = parcelles_classifiees[parcelles_classifiees.geometry.contains(bat_geom)]
    if contains.empty:
        if flag_collector is not None:
            flag_collector.add("BAT_HORS_CADASTRE", target_url=_bat_url(bat))
        return None, None, None

    parcel = contains.iloc[0]
    purl = _parcel_url(parcel)

    if bool(parcel.get("public", False)):
        return 0.0, None, purl

    boundary = parcel.geometry.boundary

    if _touches_public(boundary, public_geom):
        target_boundary = boundary
    else:
        target_boundary = _enclave_target_boundary(
            parcel, parcelles_classifiees, public_geom, max_levels=2
        )
        if flag_collector is not None:
            flag_collector.add("BAT_ENCLAVE", target_url=purl)
        if target_boundary is None:
            return None, None, purl

    cand_pos = _candidates(sindex_infra, reusable_infra, target_boundary, SEARCH_RADIUS_M)
    if not cand_pos:
        return None, None, purl

    candidates = reusable_infra.iloc[cand_pos]
    distances = candidates.geometry.distance(target_boundary)
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
