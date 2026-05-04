"""Tests for d3.measure_d3 + classify_bat.

Synthetic geometry layout (RGF93-like, in metres):

        y=30 +-----------------------------+
             |  pub (commune, x in [0,30]) |  <- top strip = public commune
        y=25 +--+-------------------+------+
             |  |  adj (private)    |      |  <- adj is adjacent to pub at y=25
        y=20 +--+--------+----------+------+
                |        |          |
                |  enc_outer (private)     |  <- ring around enc, y in [5,20]
        y=15    |   +---------+            |
                |   |  enc    |  hole      |  <- enc is private + ENCLAVED
        y=10    |   +---------+            |
                |        |                 |
        y=5     +--------+--------+--------+
              x=5      x=10     x=20     x=25

* za = box(0, 0, 30, 30)
* pub : x in [0, 30], y in [25, 30]               -> public commune
* adj : x in [0, 30], y in [20, 25]               -> private, mitoyenne du public
* enc_outer : x in [5, 25], y in [5, 20]
              avec un trou x in [10, 20], y in [10, 15]
                                                   -> private, entoure enc
* enc : x in [10, 20], y in [10, 15]               -> private, ENCLAVE
* dom_pub_hors = za - union(parcels)
                 ~ tout le pourtour vide non couvert (cotes droite/gauche
                   en bas, etc.). Petit, pas critique pour le test.

* reusable_infra : LineString proche de adj, distance ~ 2 m.
"""

from __future__ import annotations

import geopandas as gpd
import pandas as pd
import pytest
from shapely.geometry import LineString, Point, Polygon, box

from auvergne_pipeline import config, d3 as d3_mod, flags as flags_mod, parcelles


def _build_world():
    pub = box(0, 25, 30, 30)
    adj = box(0, 20, 30, 25)
    enc = box(10, 10, 20, 15)
    enc_outer = Polygon(
        shell=[(5, 5), (25, 5), (25, 20), (5, 20), (5, 5)],
        holes=[[(10, 10), (20, 10), (20, 15), (10, 15), (10, 10)]],
    )

    parcelles_gdf = gpd.GeoDataFrame(
        pd.DataFrame(
            {
                "id_metier": ["P_PUB", "P_ADJ", "P_OUTER", "P_ENC"],
                "proprietaire": [
                    "Commune de Test",
                    "M. Adj",
                    "M. Outer",
                    "M. Enc",
                ],
                "geometry": [pub, adj, enc_outer, enc],
            }
        ),
        geometry="geometry",
        crs=config.PROJECT_CRS,
    )

    za = box(0, 0, 30, 30)
    classified, dom_pub_hors = parcelles.classify_parcelles(parcelles_gdf, za)
    public_geom = parcelles.public_space_geometry(classified, dom_pub_hors)

    # Reusable infra: a horizontal line at y=27 inside pub area.
    infra_line = LineString([(2, 27), (28, 27)])
    infra = gpd.GeoDataFrame(
        pd.DataFrame({"src": ["athd"], "geometry": [infra_line]}),
        geometry="geometry",
        crs=config.PROJECT_CRS,
    )
    sindex = infra.sindex

    return classified, public_geom, infra, sindex


def _bat(point: Point, name: str = "BAT") -> pd.Series:
    """Build a Series with a 'name' attr and 'geometry' attr a-la iterrows()."""
    s = pd.Series(
        {"id_metier": name, "zapa": "Z1", "prises": 1, "geometry": point}
    )
    s.name = 0
    return s


def test_measure_d3_public_parcel_returns_zero():
    classified, public_geom, infra, sindex = _build_world()
    bat = _bat(Point(15, 27), "BAT_PUB")
    fc = flags_mod.FlagCollector("test")
    d, idx, purl = d3_mod.measure_d3(bat, classified, public_geom, infra, sindex, fc)
    assert d == 0.0
    assert idx is None
    assert purl == "P_PUB"
    assert fc.flags == []


def test_measure_d3_private_adjacent_returns_positive_distance():
    classified, public_geom, infra, sindex = _build_world()
    bat = _bat(Point(15, 22), "BAT_ADJ")
    fc = flags_mod.FlagCollector("test")
    d, idx, purl = d3_mod.measure_d3(bat, classified, public_geom, infra, sindex, fc)
    # adj boundary touches pub (shared edge y=25). Infra at y=27 -> d3 == 2 m.
    assert d is not None
    assert pytest.approx(d, abs=1e-6) == 2.0
    assert idx == 0
    assert purl == "P_ADJ"
    assert fc.counts().get("BAT_ENCLAVE", 0) == 0


def test_measure_d3_enclave_uses_neighbor_and_flags():
    classified, public_geom, infra, sindex = _build_world()
    bat = _bat(Point(15, 12), "BAT_ENC")
    fc = flags_mod.FlagCollector("test")
    d, idx, purl = d3_mod.measure_d3(bat, classified, public_geom, infra, sindex, fc)
    # enc is fully surrounded by enc_outer (level 1). enc_outer's outer
    # boundary touches adj/pub via the shared edge at y=20 -> public side.
    # Wait: enc_outer top is y=20, adj bottom is y=20, so enc_outer touches
    # adj (private). enc_outer doesn't touch pub directly (pub starts at y=25).
    # Level-2 search: from enc_outer, neighbour adj touches pub -> use adj.
    assert d is not None
    assert d >= 0.0
    assert purl == "P_ENC"
    assert fc.counts().get("BAT_ENCLAVE", 0) == 1


def test_measure_d3_outside_cadastre_flags_and_returns_none():
    classified, public_geom, infra, sindex = _build_world()
    bat = _bat(Point(1000, 1000), "BAT_OUT")
    fc = flags_mod.FlagCollector("test")
    d, idx, purl = d3_mod.measure_d3(bat, classified, public_geom, infra, sindex, fc)
    assert d is None
    assert idx is None
    assert purl is None
    assert fc.counts().get("BAT_HORS_CADASTRE", 0) == 1


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
