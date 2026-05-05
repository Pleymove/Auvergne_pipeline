"""End-to-end pipeline test against a tiny synthetic fixture.

This test is the regression net asked for by Pierre after the it.5/6/7
chain of "imports oublies" bugs. It runs the whole pipeline (loader ->
filters -> parcelles -> orphans -> d3 -> pb_fictif -> routing -> writer)
on a 5-BAT fixture and asserts:

* the pipeline does not crash;
* a livrable GPKG is produced with the expected layers.

IGN WFS is mocked (returns an empty routes GeoDataFrame) -- the test must
not depend on network access.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import fiona
import geopandas as gpd
import pytest

from auvergne_pipeline import config


FIXTURE = Path(__file__).parent / "fixtures" / "mini_auvergne.gpkg"
SRO_CODE = "99999/TST/PMZ/00001"


@pytest.fixture
def empty_ign_routes():
    """Patch ign_routes.load_ign_routes_for_sro to return an empty GDF."""
    empty = gpd.GeoDataFrame(geometry=[], crs=config.PROJECT_CRS)
    with patch(
        "auvergne_pipeline.main.ign_routes.load_ign_routes_for_sro",
        return_value=empty,
    ) as p:
        yield p


def test_fixture_exists():
    assert FIXTURE.exists(), (
        f"Fixture missing: {FIXTURE}. "
        "Run `python tests/fixtures/build_fixture.py` to regenerate."
    )


def test_pipeline_full_no_crash(tmp_path, empty_ign_routes):
    """Run the full pipeline on the synthetic fixture without crashing."""
    from auvergne_pipeline.main import run_for_sros

    output = tmp_path / "test_output.gpkg"
    summaries = run_for_sros(FIXTURE, [SRO_CODE], output_gpkg=output)

    assert len(summaries) == 1, "Expected exactly one SRO summary"
    s = summaries[0]
    assert s["sro"] == SRO_CODE
    assert s["bal"] == 5
    assert s["pa"] == 1
    assert s["zapa"] == 1
    # 3 orphan BATs (BAT/00003-5) -> at least 1 created PA
    assert s["orphan_bats"] >= 1
    assert s["new_pa"] >= 1

    assert output.exists(), "Output GPKG was not written"
    layers = set(fiona.listlayers(str(output)))

    expected_minimum = {
        "livrable_pa",
        "livrable_zapa",
        "livrable_bat",
        "livrable_parcelles",
    }
    missing = expected_minimum - layers
    assert not missing, f"Missing livrable layers: {missing}. Got: {layers}"


def test_pipeline_handles_multilinestring_in_routing(tmp_path, empty_ign_routes):
    """Regression for PR #15: a MultiLineString in existant_ft_arciti must
    not crash the routing graph builder.
    """
    from auvergne_pipeline.main import run_for_sros

    output = tmp_path / "regression_output.gpkg"
    # If MultiLineString handling is broken, run_for_sros logs the exception
    # and returns an empty summary list. We assert the SRO ran end-to-end.
    summaries = run_for_sros(FIXTURE, [SRO_CODE], output_gpkg=output)
    assert len(summaries) == 1, (
        "Pipeline crashed on a fixture containing a MultiLineString -- "
        "check routing._explode_to_linestrings (PR #15 regression)."
    )


def test_pipeline_no_output_runs_d3_only(empty_ign_routes):
    """Without output_gpkg, the pipeline stops at D3 and does not call
    ign_routes / pb_fictif / routing / writer.
    """
    from auvergne_pipeline.main import run_for_sros

    summaries = run_for_sros(FIXTURE, [SRO_CODE], output_gpkg=None)
    assert len(summaries) == 1
    assert summaries[0]["bal"] == 5
    # ign_routes must NOT have been called (output_gpkg is None)
    empty_ign_routes.assert_not_called()
