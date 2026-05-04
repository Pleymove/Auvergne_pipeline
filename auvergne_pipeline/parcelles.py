"""Public/private parcel classification + public-space geometry.

Public domain on a SRO = (parcels owned by the commune) UNION (residual
slice of the SRO that is not covered by any parcel = inter-parcel road
right-of-way).

The proprietaire test uses the PYARROW DEFENSIVE pattern to stay compatible
with the QGIS-embedded pyarrow that lacks ``match_substring_regex``:

    col = gdf["proprietaire"].astype("object").fillna("").str.lower()
    mask = col.str.contains("commune", regex=False, na=False)
"""

from __future__ import annotations

from typing import Tuple

import geopandas as gpd
from shapely.geometry.base import BaseGeometry
from shapely.ops import unary_union


def classify_parcelles(
    gdf_parcelle: gpd.GeoDataFrame,
    za_geometry: BaseGeometry,
) -> Tuple[gpd.GeoDataFrame, BaseGeometry]:
    """Tag each parcel ``public`` (commune-owned) and return the residual public domain.

    Parameters
    ----------
    gdf_parcelle:
        Parcels clipped to (or near) the SRO bounding box.
    za_geometry:
        SRO polygon -- used to compute ``za_geometry - union(parcelles)`` which
        is the inter-parcel public right-of-way (roads, public squares, etc.).

    Returns
    -------
    (gdf_classifie, domaine_public_hors_parcelle)
        ``gdf_classifie`` has an extra boolean column ``public``.
        ``domaine_public_hors_parcelle`` is a (Multi)Polygon (possibly empty).
    """
    out = gdf_parcelle.copy()

    if "proprietaire" in out.columns and not out.empty:
        col = out["proprietaire"].astype("object").fillna("").str.lower()
        out["public"] = col.str.contains("commune", regex=False, na=False)
    else:
        out["public"] = False

    if out.empty:
        return out, za_geometry

    parcelles_union = unary_union(out.geometry.tolist())
    domaine_public_hors_parcelle = za_geometry.difference(parcelles_union)
    return out, domaine_public_hors_parcelle


def public_space_geometry(
    parcelles_classifiees: gpd.GeoDataFrame,
    dom_pub_hors: BaseGeometry,
) -> BaseGeometry:
    """Geometry of the freely-routable public space (commune + inter-parcel)."""
    public_parcels = parcelles_classifiees[parcelles_classifiees.get("public", False)]
    public_union = (
        unary_union(public_parcels.geometry.tolist()) if not public_parcels.empty else None
    )

    if public_union is None and (dom_pub_hors is None or dom_pub_hors.is_empty):
        # Empty MultiPolygon as a safe default for downstream callers.
        return unary_union([])
    if public_union is None:
        return dom_pub_hors
    if dom_pub_hors is None or dom_pub_hors.is_empty:
        return public_union
    return unary_union([public_union, dom_pub_hors])
