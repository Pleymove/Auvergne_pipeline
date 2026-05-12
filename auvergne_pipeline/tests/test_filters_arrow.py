"""Regression test: filter_bt must work with arrow-backed string dtypes.

The embedded QGIS pyarrow does not expose ``match_substring_regex``, so any
``str.contains(..., case=False)`` (or with a regex) on an arrow-backed string
column blows up at runtime. We reproduce that scenario with ``string[pyarrow]``
and assert the filter survives + returns the right rows.
"""

from __future__ import annotations

import geopandas as gpd
import pandas as pd
import pytest
from shapely.geometry import LineString

from auvergne_pipeline import config, filters


def _has_pyarrow() -> bool:
    try:
        import pyarrow  # noqa: F401
        return True
    except Exception:
        return False


pytestmark = pytest.mark.skipif(
    not _has_pyarrow(),
    reason="pyarrow non disponible (env de dev sans QGIS)",
)


def _line(i: int) -> LineString:
    return LineString([(i, 0), (i, 1)])


def _arrow_string_gdf() -> gpd.GeoDataFrame:
    """Build a GDF whose ``type_de_lien`` column uses the arrow-backed dtype."""
    df = pd.DataFrame(
        {
            "type_de_lien": pd.array(
                [
                    "Aerien",
                    "Cable enterre",
                    "CABLE ENTERRE",
                    "Facade",
                    None,
                    "câble enterré",
                ],
                dtype="string[pyarrow]",
            ),
            "geometry": [_line(i) for i in range(6)],
        }
    )
    return gpd.GeoDataFrame(df, geometry="geometry", crs=config.PROJECT_CRS)


def test_filter_bt_handles_arrow_string_without_match_substring_regex():
    """Reproduce the original AttributeError and confirm the fix avoids it."""
    gdf = _arrow_string_gdf()
    # Sanity: dtype really is arrow-backed.
    assert "pyarrow" in str(gdf["type_de_lien"].dtype).lower()

    # Should NOT raise AttributeError on match_substring_regex.
    out = filters.filter_bt(gdf)

    kept = out["type_de_lien"].astype("object").tolist()
    assert "Aerien" in kept
    assert "Facade" in kept
    assert None in kept
    # All buried-cable variants excluded (case-insensitive, with or without accents).
    for excluded in ("Cable enterre", "CABLE ENTERRE", "câble enterré"):
        assert excluded not in kept


def test_filter_bt_arrow_preserves_geometry_and_crs():
    gdf = _arrow_string_gdf()
    out = filters.filter_bt(gdf)
    assert out.crs == gdf.crs
    assert out.geometry.notna().all()
    assert len(out) == 3  # Aerien, Facade, None
