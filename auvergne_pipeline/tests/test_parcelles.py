"""Tests for parcelles.classify_parcelles + public_space_geometry."""

from __future__ import annotations

import geopandas as gpd
import pandas as pd
import pytest
from shapely.geometry import Polygon, box

from auvergne_pipeline import config, parcelles


def _square(x0: float, y0: float, side: float = 10.0) -> Polygon:
    return Polygon(
        [(x0, y0), (x0 + side, y0), (x0 + side, y0 + side), (x0, y0 + side), (x0, y0)]
    )


def _three_parcels(proprio_dtype: str = "object") -> gpd.GeoDataFrame:
    df = pd.DataFrame(
        {
            "proprietaire": pd.array(
                ["Commune de Maringues", "Section A", "M. Dupont"],
                dtype=proprio_dtype,
            ),
            "geometry": [_square(0, 0), _square(10, 0), _square(20, 0)],
        }
    )
    return gpd.GeoDataFrame(df, geometry="geometry", crs=config.PROJECT_CRS)


def test_classify_parcelles_object_dtype_marks_commune_as_public():
    gdf = _three_parcels("object")
    za = box(-5, -5, 35, 15)
    out, dom_pub_hors = parcelles.classify_parcelles(gdf, za)
    assert out["public"].tolist() == [True, False, False]
    # za is wider/taller than parcels -> some residual public domain.
    assert not dom_pub_hors.is_empty


def _has_pyarrow() -> bool:
    try:
        import pyarrow  # noqa: F401
        return True
    except Exception:
        return False


@pytest.mark.skipif(not _has_pyarrow(), reason="pyarrow indisponible")
def test_classify_parcelles_arrow_string_does_not_crash():
    gdf = _three_parcels("string[pyarrow]")
    za = box(-5, -5, 35, 15)
    out, _ = parcelles.classify_parcelles(gdf, za)
    assert out["public"].tolist() == [True, False, False]


def test_classify_parcelles_no_proprietaire_column_marks_all_private():
    df = pd.DataFrame({"geometry": [_square(0, 0)]})
    gdf = gpd.GeoDataFrame(df, geometry="geometry", crs=config.PROJECT_CRS)
    out, _ = parcelles.classify_parcelles(gdf, box(-5, -5, 15, 15))
    assert out["public"].tolist() == [False]


def test_public_space_geometry_unions_commune_and_residual():
    gdf = _three_parcels("object")
    za = box(-5, -5, 35, 15)
    classified, dom_pub_hors = parcelles.classify_parcelles(gdf, za)
    public_geom = parcelles.public_space_geometry(classified, dom_pub_hors)
    assert not public_geom.is_empty
    # Public commune parcel sits within public_geom.
    assert public_geom.contains(_square(0, 0).buffer(-0.1))
    # Private parcels are NOT in public_geom.
    assert not public_geom.contains(_square(10, 0).buffer(-0.1))


def test_public_space_geometry_handles_no_public_parcels():
    df = pd.DataFrame({
        "proprietaire": ["M. Dupont", "Section B"],
        "public": [False, False],
        "geometry": [_square(0, 0), _square(10, 0)],
    })
    gdf = gpd.GeoDataFrame(df, geometry="geometry", crs=config.PROJECT_CRS)
    dom_pub = box(0, 10, 20, 15)  # strip above the parcels
    out = parcelles.public_space_geometry(gdf, dom_pub)
    assert not out.is_empty
    assert out.contains(box(2, 11, 18, 14))
