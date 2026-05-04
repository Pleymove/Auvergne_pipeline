"""Tests for d3.measure_d3 + classify_bat after the BAT-point fix.

D3 is measured from the **BAT point** (building hookup at the facade) to
the closest reusable-infrastructure segment, not from the parcel boundary.
The boundary is essentially always on the street where the infra runs, so
the boundary-based measurement collapsed to ~0 m and over-reported AUTO_OK.

Synthetic world (RGF93-like, in metres):

* za = box(-1000, -1000, 11000, 11000)
* pub       : box(0, 0, 100, 100)              -- public commune
* adj       : box(150, 150, 250, 250)          -- private, touches public via
                                                  the residual public domain
* far       : box(350, 350, 450, 450)          -- private, idem
* enc_outer : box(600, 600, 800, 800) WITH HOLE box(680, 680, 720, 720)
                                                -- private ring around enc
* enc       : box(680, 680, 720, 720)          -- private, ENCLAVE inside the
                                                  hole (no boundary on public)
"""

from __future__ import annotations

import geopandas as gpd
import pandas as pd
import pytest
from shapely.geometry import LineString, Point, Polygon, box

from auvergne_pipeline import config, d3 as d3_mod, flags as flags_mod, parcelles


def _build_world(infra_lines):
    pub = box(0, 0, 100, 100)
    adj = box(150, 150, 250, 250)
    far = box(350, 350, 450, 450)
    enc_outer = Polygon(
        shell=[(600, 600), (800, 600), (800, 800), (600, 800), (600, 600)],
        holes=[[(680, 680), (720, 680), (720, 720), (680, 720), (680, 680)]],
    )
    enc = box(680, 680, 720, 720)

    parcs = gpd.GeoDataFrame(
        pd.DataFrame(
            {
                "id_metier": ["P_PUB", "P_ADJ", "P_FAR", "P_OUTER", "P_ENC"],
                "proprietaire": [
                    "Commune de Test",
                    "M. Adj",
                    "M. Far",
                    "M. Outer",
                    "M. Enc",
                ],
                "geometry": [pub, adj, far, enc_outer, enc],
            }
        ),
        geometry="geometry",
        crs=config.PROJECT_CRS,
    )
    za = box(-1000, -1000, 11000, 11000)
    classified, dom_pub_hors = parcelles.classify_parcelles(parcs, za)
    public_geom = parcelles.public_space_geometry(classified, dom_pub_hors)

    infra = gpd.GeoDataFrame(
        pd.DataFrame(
            {
                "src": ["athd"] * len(infra_lines),
                "geometry": list(infra_lines),
            }
        ),
        geometry="geometry",
        crs=config.PROJECT_CRS,
    )
    sindex = infra.sindex if not infra.empty else None
    return classified, public_geom, infra, sindex


def _bat(point: Point, name: str = "BAT") -> pd.Series:
    s = pd.Series(
        {"id_metier": name, "zapa": "Z1", "prises": 1, "geometry": point}
    )
    s.name = 0
    return s


# ------------------------------------------------------------------
# 1. BAT on a public parcel -> D3 = 0
# ------------------------------------------------------------------

def test_measure_d3_public_parcel_returns_zero():
    classified, public_geom, infra, sindex = _build_world(
        [LineString([(300, 300), (310, 310)])]
    )
    bat = _bat(Point(50, 50), "BAT_PUB")
    fc = flags_mod.FlagCollector("test")

    d, idx, purl = d3_mod.measure_d3(bat, classified, public_geom, infra, sindex, fc)

    assert d == 0.0
    assert idx is None
    assert purl == "P_PUB"
    assert fc.flags == []
    assert d3_mod.classify_bat(d) == "AUTO_OK"


# ------------------------------------------------------------------
# 2. BAT in private parcel, 30 m from infra -> D3 == 30, AUTO_OK
# ------------------------------------------------------------------

def test_measure_d3_private_30m_is_auto_ok_no_flag():
    # Vertical infra at x=170, y in [200, 300]. BAT at (200, 200).
    # Closest point on segment is (170, 200) -> distance == 30 m exact.
    classified, public_geom, infra, sindex = _build_world(
        [LineString([(170, 200), (170, 300)])]
    )
    bat = _bat(Point(200, 200), "BAT_30M")
    fc = flags_mod.FlagCollector("test")

    d, idx, purl = d3_mod.measure_d3(bat, classified, public_geom, infra, sindex, fc)

    assert d is not None
    assert pytest.approx(d, abs=1e-6) == 30.0
    assert idx == 0
    assert purl == "P_ADJ"
    assert fc.flags == []
    assert d3_mod.classify_bat(d) == "AUTO_OK"


# ------------------------------------------------------------------
# 3. BAT in private parcel, 150 m from infra -> D3 == 150, TO_CREATE
# ------------------------------------------------------------------

def test_measure_d3_private_150m_is_to_create_no_flag():
    # Vertical infra at x=250, y in [400, 500]. BAT at (400, 400).
    # Closest point is (250, 400) -> distance == 150 m exact.
    classified, public_geom, infra, sindex = _build_world(
        [LineString([(250, 400), (250, 500)])]
    )
    bat = _bat(Point(400, 400), "BAT_150M")
    fc = flags_mod.FlagCollector("test")

    d, idx, purl = d3_mod.measure_d3(bat, classified, public_geom, infra, sindex, fc)

    assert d is not None
    assert pytest.approx(d, abs=1e-6) == 150.0
    assert idx == 0
    assert purl == "P_FAR"
    assert fc.flags == []
    assert d3_mod.classify_bat(d) == "TO_CREATE"


# ------------------------------------------------------------------
# 4. BAT in enclaved parcel, 50 m from infra -> D3 == 50, BAT_ENCLAVE flag
# ------------------------------------------------------------------

def test_measure_d3_enclave_measures_bat_distance_and_flags():
    # Vertical infra at x=650, y in [700, 800]. BAT at (700, 700).
    # Closest point is (650, 700) -> distance == 50 m exact.
    classified, public_geom, infra, sindex = _build_world(
        [LineString([(650, 700), (650, 800)])]
    )
    bat = _bat(Point(700, 700), "BAT_ENC")
    fc = flags_mod.FlagCollector("test")

    d, idx, purl = d3_mod.measure_d3(bat, classified, public_geom, infra, sindex, fc)

    assert d is not None
    assert pytest.approx(d, abs=1e-6) == 50.0
    assert idx == 0
    assert purl == "P_ENC"
    assert fc.counts().get("BAT_ENCLAVE", 0) == 1
    assert fc.counts().get("BAT_HORS_CADASTRE", 0) == 0
    assert d3_mod.classify_bat(d) == "AUTO_OK"


# ------------------------------------------------------------------
# 5. BAT outside any parcel -> BAT_HORS_CADASTRE, distance still measured
# ------------------------------------------------------------------

def test_measure_d3_outside_cadastre_still_measures_distance():
    # Infra near a BAT placed in the SRO but outside any parcel polygon.
    classified, public_geom, infra, sindex = _build_world(
        [LineString([(5050, 5000), (5050, 5100)])]
    )
    bat = _bat(Point(5000, 5000), "BAT_OUT")
    fc = flags_mod.FlagCollector("test")

    d, idx, purl = d3_mod.measure_d3(bat, classified, public_geom, infra, sindex, fc)

    assert d is not None
    assert pytest.approx(d, abs=1e-6) == 50.0
    assert idx == 0
    assert purl is None
    assert fc.counts().get("BAT_HORS_CADASTRE", 0) == 1
    assert d3_mod.classify_bat(d) == "AUTO_OK"


# ------------------------------------------------------------------
# 6. No infra within SEARCH_RADIUS_M -> (None, None, purl)
# ------------------------------------------------------------------

def test_measure_d3_no_infra_in_range_returns_none():
    # Infra at (50_000, 50_000) -> several tens of km from the BAT.
    classified, public_geom, infra, sindex = _build_world(
        [LineString([(50_000, 50_000), (50_050, 50_100)])]
    )
    bat = _bat(Point(200, 200), "BAT_NO_INFRA")
    fc = flags_mod.FlagCollector("test")

    d, idx, purl = d3_mod.measure_d3(bat, classified, public_geom, infra, sindex, fc)

    assert d is None
    assert idx is None
    assert purl == "P_ADJ"
    assert fc.counts().get("BAT_ENCLAVE", 0) == 0
    assert fc.counts().get("BAT_HORS_CADASTRE", 0) == 0
    assert d3_mod.classify_bat(d) == "TO_CREATE"


# ------------------------------------------------------------------
# classify_bat (unchanged): seuil 100 m inclusif
# ------------------------------------------------------------------

@pytest.mark.parametrize(
    "distance,expected",
    [
        (0.0, "AUTO_OK"),
        (50.0, "AUTO_OK"),
        (100.0, "AUTO_OK"),
        (100.01, "TO_CREATE"),
        (None, "TO_CREATE"),
    ],
)
def test_classify_bat(distance, expected):
    assert d3_mod.classify_bat(distance) == expected
