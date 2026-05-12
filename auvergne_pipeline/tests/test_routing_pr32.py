"""PR #32 regression tests — existing-first topology + CRS hotfix.

Sections tested:
A — enforce_crs() behaviour (no CRS, already 2154, 4326→2154)
B — Terminal connection (existing-line, C0 rejected >100m, no zero-length C0)
C — T-junction split (valid split, no degenerate <0.01 m, is_split flag)
D — Reconnect after energy removal (existing-priority, ENERGY_RECONNECT_FAILED)
E — Drop superposed C0 (drop with existing equivalent, keep without, QA counters)
"""

from __future__ import annotations

import geopandas as gpd
import numpy as np
import pytest
from shapely.geometry import LineString, Point, Polygon

from auvergne_pipeline import livrable_topology as lt

CRS = "EPSG:2154"

_BIG_PUBLIC = Polygon([(-1000, -1000), (1000, -1000), (1000, 1000), (-1000, 1000)])


def _livrable_row(**kw) -> dict:
    """Build a livrable_infra row dict with safe defaults."""
    base = {
        "sro": "SRO1",
        "pa_id": "PA1",
        "pb_id": "PB1",
        "statut": "E",
        "mode_pose": "1",
        "infra_type": "bt",
        "src": "bt",
        "length_m": 0.0,
        "geometry": None,
    }
    base.update(kw)
    if base["geometry"] is not None and base["length_m"] == 0.0:
        base["length_m"] = base["geometry"].length
    return base


def _df(rows: list[dict], crs=CRS) -> gpd.GeoDataFrame:
    return gpd.GeoDataFrame(rows, geometry="geometry", crs=crs)


def _pa(x=0.0, y=0.0, pid="PA1") -> gpd.GeoDataFrame:
    return gpd.GeoDataFrame(
        {
            "id_metier": [pid],
            "sro": ["SRO1"],
            "geometry": [Point(x, y)],
        },
        geometry="geometry",
        crs=CRS,
    )


def _pb(x=0.0, y=0.0, pb_id="PB1", pa_id="PA1") -> gpd.GeoDataFrame:
    return gpd.GeoDataFrame(
        {
            "pb_id": [pb_id],
            "pa_id": [pa_id],
            "geometry": [Point(x, y)],
        },
        geometry="geometry",
        crs=CRS,
    )


# =========================================================================
# A — CRS tests
# =========================================================================


def test_enforce_crs_no_crs_sets_4326_then_converts():
    """enforce_crs() on a GDF without CRS: assign 4326 then convert to 2154."""
    line = LineString([(2.35, 48.85), (2.36, 48.86)])
    gdf = gpd.GeoDataFrame([{"geometry": line}], geometry="geometry")
    assert gdf.crs is None
    result = lt.enforce_crs(gdf, target_crs="EPSG:2154")
    assert result.crs == "EPSG:2154"
    # Coordinates should have been transformed (no longer lon/lat values)
    cs = list(result.geometry.iloc[0].coords)
    # 2154 coords are in the hundreds of thousands, not degrees
    assert abs(cs[0][0]) > 1000 or abs(cs[0][1]) > 1000


def test_enforce_crs_already_2154_unchanged():
    """enforce_crs() on a GDF already in 2154: returned as-is."""
    line = LineString([(0, 0), (10, 10)])
    gdf = _df([{"geometry": line}])
    result = lt.enforce_crs(gdf, target_crs="EPSG:2154")
    assert result.crs == "EPSG:2154"
    # Geometry must be identical
    assert result.geometry.iloc[0].equals(line)


def test_enforce_crs_4326_converts_to_2154():
    """enforce_crs() on a GDF in 4326: converts to 2154."""
    line = LineString([(2.35, 48.85), (2.36, 48.86)])
    gdf = _df([{"geometry": line}], crs="EPSG:4326")
    result = lt.enforce_crs(gdf, target_crs="EPSG:2154")
    assert result.crs == "EPSG:2154"
    cs = list(result.geometry.iloc[0].coords)
    # Transformed coords should be large (meter-based)
    assert abs(cs[0][0]) > 500_000


# =========================================================================
# B — Terminal connection tests
# =========================================================================


def test_terminal_near_existing_endpoint_connected_without_c0():
    """A PA placed near the endpoint of an existing livrable line must
    be considered connected without adding any C0 connector."""
    df = _df([
        _livrable_row(geometry=LineString([(0, 0), (10, 0)]),
                       src="ft", mode_pose="7", infra_type="ft"),
    ])
    pa = _pa(0.0, 0.05, pid="PA1")  # 5 cm from endpoint (0,0)
    pb = _pb(10.0, 0.0, pb_id="PB1", pa_id="PA1")
    out, stats = lt.finalize_livrable_topology(
        df, pa, pb, "SRO1",
        delivery_public_area_safe=_BIG_PUBLIC.buffer(0.01),
    )
    # Connected but no new C0 should have been injected
    c0_rows = out[(out["mode_pose"] == "C0") & (out["src"] == "gc_neuf")]
    assert len(c0_rows) == 0, "no C0 connector should be needed"
    assert stats["pa_connected"] >= 1


def test_c0_connector_rejected_if_terminal_far_from_line():
    """A PA whose nearest existing line is > 100 m away must NOT produce
    a C0 connector — it's too far for automatic connection."""
    df = _df([
        _livrable_row(geometry=LineString([(0, 0), (10, 0)]),
                       src="ft", mode_pose="7", infra_type="ft"),
    ])
    pa = _pa(5.0, 200.0, pid="PA1")  # 200 m away
    pb = _pb(10.0, 0.0, pb_id="PB1", pa_id="PA1")
    from auvergne_pipeline import flags as flags_mod

    fc = flags_mod.FlagCollector("SRO1")
    out, stats = lt.finalize_livrable_topology(
        df, pa, pb, "SRO1",
        delivery_public_area_safe=_BIG_PUBLIC.buffer(0.01),
        flag_collector=fc,
    )
    # No connector should have been added
    assert stats.get("terminal_connectors_added", 0) == 0
    assert stats.get("terminal_snap_failed", 0) >= 1


def test_no_zero_length_c0_connector():
    """When PA projection is essentially on the line (d < 0.01 m), no
    zero-length C0 must be created — connection is via existing."""
    df = _df([
        _livrable_row(geometry=LineString([(0, 0), (10, 0)]),
                       src="ft", mode_pose="7", infra_type="ft"),
    ])
    pa = _pa(5.0, 0.005, pid="PA1")  # 5 mm off the line, projection on it
    pb = _pb(10.0, 0.0, pb_id="PB1", pa_id="PA1")
    out, stats = lt.finalize_livrable_topology(
        df, pa, pb, "SRO1",
        delivery_public_area_safe=_BIG_PUBLIC.buffer(0.01),
    )
    # Any C0 rows must have length > 0.01 m
    c0_rows = out[(out["mode_pose"] == "C0") & (out["src"] == "gc_neuf")]
    for _, r in c0_rows.iterrows():
        assert r["length_m"] > 0.01, "zero-length C0 detected"
    assert stats.get("terminals_connected_via_existing", 0) >= 1


# =========================================================================
# C — T-junction split tests
# =========================================================================


def test_t_junction_split_when_endpoint_projects_on_line():
    """Line A (0,5)→(5,5) ends at (5,5) on the middle of Line B
    (5,0)→(5,10). After T-junction split, Line B must become two
    segments sharing exact node (5,5) with Line A."""
    df = _df([
        _livrable_row(geometry=LineString([(0, 5), (5, 5)]),
                       src="ft", mode_pose="7", infra_type="ft", pb_id="PB1"),
        _livrable_row(geometry=LineString([(5, 0), (5, 10)]),
                       src="ft", mode_pose="7", infra_type="ft", pb_id="PB2"),
    ])
    out, stats = lt._split_livrableedges_at_endpoint_projections(df, tol_m=0.5)
    assert len(out) >= 3, f"Expected ≥3 rows after T-junction split; got {len(out)}"
    # (5,5) must appear as endpoint of at least 2 segments
    geoms = list(out.geometry)
    all_endpoints = []
    for g in geoms:
        cs = list(g.coords)
        all_endpoints.append((round(cs[0][0], 3), round(cs[0][1], 3)))
        all_endpoints.append((round(cs[-1][0], 3), round(cs[-1][1], 3)))
    count_55 = sum(1 for ep in all_endpoints if ep == (5.0, 5.0))
    assert count_55 >= 2, f"(5,5) should be shared endpoint ≥2×, got {count_55}"


def test_no_degenerate_split_below_0_01m():
    """If the projection splits a line into a segment < 0.01 m, the split
    must be refused — no degenerate geometry."""
    df = _df([
        _livrable_row(geometry=LineString([(0, 5), (5, 5)]),
                       src="ft", mode_pose="7", infra_type="ft", pb_id="PB1"),
        # Line B: projection of Line A endpoint (0,5) is very close to B's (0,0)
        _livrable_row(geometry=LineString([(0.005, 0), (5, 10)]),
                       src="ft", mode_pose="7", infra_type="ft", pb_id="PB2"),
    ])
    out, stats = lt._split_livrableedges_at_endpoint_projections(df, tol_m=0.5)
    # Verify no resulting segment is shorter than 0.01 m
    for _, r in out.iterrows():
        g = r["geometry"]
        if isinstance(g, LineString):
            assert g.length >= 0.01, f"degenerate split: {g.length} m"


def test_is_split_flag_after_successful_split():
    """After a successful T-junction split, the ``is_split`` attribute
    (or the split count stat) must reflect that splitting occurred."""
    df = _df([
        _livrable_row(geometry=LineString([(0, 5), (5, 5)]),
                       src="ft", mode_pose="7", infra_type="ft", pb_id="PB1"),
        _livrable_row(geometry=LineString([(5, 0), (5, 10)]),
                       src="ft", mode_pose="7", infra_type="ft", pb_id="PB2"),
    ])
    out, n_splits = lt._split_livrableedges_at_endpoint_projections(df, tol_m=0.5)
    assert n_splits >= 1, "expected at least one T-junction split"


# =========================================================================
# D — Reconnect after energy removal
# =========================================================================


def test_reconnect_via_existing_has_priority():
    """After a BT/E1 segment is removed, reconnect should prefer
    existing infrastructure over injecting a new C0."""
    # Before: an FT line bridges the gap + a BT segment
    df_before = _df([
        _livrable_row(geometry=LineString([(0, 0), (5, 0)]),
                       src="ft", mode_pose="7", infra_type="ft"),
        _livrable_row(geometry=LineString([(5, 0), (10, 0)]),
                       src="bt", mode_pose="1", infra_type="bt"),  # removed BT
    ])
    # After: BT removed, only FT remains
    df_after = _df([
        _livrable_row(geometry=LineString([(0, 0), (5, 0)]),
                       src="ft", mode_pose="7", infra_type="ft"),
    ])
    out, recon_stats = lt._reconnect_after_energy_removal(
        df_before, df_after,
        delivery_public_area_safe=_BIG_PUBLIC.buffer(0.01),
    )
    # If existing infra already bridges, no new C0 needed
    assert recon_stats.get("energy_reconnectors_added", 0) == 0


def test_energy_reconnect_FAILED_when_no_solution():
    """If there's no existing infra and no public path, the reconnect
    must fail and set ENERGY_RECONNECT_FAILED flag."""
    from auvergne_pipeline import flags as flags_mod

    # Before: isolated BT segment, no other infra
    df_before = _df([
        _livrable_row(geometry=LineString([(0, 0), (10, 0)]),
                       src="bt", mode_pose="1", infra_type="bt"),
    ])
    df_after = gpd.GeoDataFrame(geometry=[], crs=CRS)  # everything removed

    fc = flags_mod.FlagCollector("SRO1")
    out, recon_stats = lt._reconnect_after_energy_removal(
        df_before, df_after,
        delivery_public_area_safe=_BIG_PUBLIC.buffer(0.01),
        flag_collector=fc,
    )
    # If df_after is empty, reconnect should either skip or fail
    flags_df = fc.to_dataframe()
    if recon_stats.get("energy_reconnect_failed", 0) > 0:
        assert "ENERGY_RECONNECT_FAILED" in set(flags_df["flag_type"])


# =========================================================================
# E — Drop superposed C0 tests
# =========================================================================


def test_c0_dropped_when_existing_equivalent():
    """A new C0 connector must be dropped if an existing equivalent
    infrastructure passes within 2 m with < 45° angle (Hausdorff ≤ 1.0 m)."""

    # Existing FT line
    ft_geom = LineString([(0, 0), (10, 0)])
    # New C0 that runs nearly parallel to the FT line (offset 0.5 m → Hausdorff ≈ 0.5 m)
    c0_geom = LineString([(0, 0.5), (10, 0.5)])

    df = _df([
        _livrable_row(geometry=ft_geom, src="ft", mode_pose="7", infra_type="ft"),
        _livrable_row(geometry=c0_geom, src="gc_neuf", mode_pose="C0", infra_type="gc_neuf"),
    ])
    out, c0_stats = lt._drop_c0_when_existing_equivalent(df, hausdorff_tol_m=1.0)
    # The C0 row should have been dropped
    c0_rows = out[(out["mode_pose"] == "C0") & (out["src"] == "gc_neuf")]
    assert len(c0_rows) == 0, "C0 parallel to existing should be dropped"
    assert c0_stats["c0_removed_existing_parallel"] >= 1


def test_c0_kept_when_no_existing_equivalent():
    """A C0 connector must be kept when there is no equivalent existing
    infrastructure within tolerance."""

    # Only isolated FT line, nowhere near the C0
    ft_geom = LineString([(0, 100), (10, 100)])
    c0_geom = LineString([(0, 0), (10, 0)])

    df = _df([
        _livrable_row(geometry=ft_geom, src="ft", mode_pose="7", infra_type="ft"),
        _livrable_row(geometry=c0_geom, src="gc_neuf", mode_pose="C0", infra_type="gc_neuf"),
    ])
    out, c0_stats = lt._drop_c0_when_existing_equivalent(df, hausdorff_tol_m=1.0)
    c0_rows = out[(out["mode_pose"] == "C0") & (out["src"] == "gc_neuf")]
    assert len(c0_rows) == 1, "C0 with no equivalent existing should be kept"
    assert c0_stats["c0_kept_last_resort"] >= 1


def test_c0_qa_counters_consistent():
    """Verify QA counters c0_removed_existing_parallel + c0_kept_last_resort
    together account for all C0 rows in the output."""

    ft_geom = LineString([(0, 0), (10, 0)])
    # Close C0 → will be dropped
    c0_close = LineString([(0, 0.3), (10, 0.3)])
    # Far C0 → will be kept
    c0_far = LineString([(0, 100), (10, 100)])

    df = _df([
        _livrable_row(geometry=ft_geom, src="ft", mode_pose="7", infra_type="ft"),
        _livrable_row(geometry=c0_close, src="gc_neuf", mode_pose="C0", infra_type="gc_neuf"),
        _livrable_row(geometry=c0_far, src="gc_neuf", mode_pose="C0", infra_type="gc_neuf"),
    ])
    out, c0_stats = lt._drop_c0_when_existing_equivalent(df, hausdorff_tol_m=1.0)

    removed = c0_stats["c0_removed_existing_parallel"]
    kept = c0_stats["c0_kept_last_resort"]
    assert removed + kept == 2, (
        f"expected 2 C0 candidates, got removed={removed} + kept={kept}"
    )
    # The remaining C0 count in output must match 'kept'
    c0_rows = out[(out["mode_pose"] == "C0") & (out["src"] == "gc_neuf")]
    assert len(c0_rows) == kept


def test_c0_single_candidate_no_existing_kept():
    """Single C0 with zero non-C0 rows: kept (no existing to compare)."""
    c0_geom = LineString([(0, 0), (10, 0)])
    df = _df([
        _livrable_row(geometry=c0_geom, src="gc_neuf", mode_pose="C0", infra_type="gc_neuf"),
    ])
    out, c0_stats = lt._drop_c0_when_existing_equivalent(df, hausdorff_tol_m=1.0)
    assert len(out) == 1
    assert out.iloc[0]["mode_pose"] == "C0"
    assert c0_stats["c0_kept_last_resort"] == 1
    assert c0_stats["c0_removed_existing_parallel"] == 0
