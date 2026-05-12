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

import geopandas as gpd
import pytest
from pyogrio import list_layers as pyogrio_list_layers

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
    layers = set(pyogrio_list_layers(str(output))[:, 0])

    expected_minimum = {
        "livrable_pa",
        "livrable_zapa",
        "livrable_bal",
        "livrable_parcelles",
        "livrable_zasro",
        "livrable_sro",
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
    pb_fictif / routing / writer.

    PR #23 (CDC): if reusable infra contains BT segments, IGN routes ARE
    loaded once (in section 3.5) so the BT clip can include road buffers
    in the public domain. This is the correct CDC behavior — D3 distances
    must reference clipped BT, not unclipped BT.
    """
    from auvergne_pipeline.main import run_for_sros

    summaries = run_for_sros(FIXTURE, [SRO_CODE], output_gpkg=None)
    assert len(summaries) == 1
    assert summaries[0]["bal"] == 5
    # ign_routes is loaded at most once (BT clip), never twice (no routing).
    assert empty_ign_routes.call_count <= 1


# ---------------------------------------------------------------------------
# PR #19 regression E2E tests
# ---------------------------------------------------------------------------


def test_livrable_infra_is_routed_only(tmp_path, empty_ign_routes):
    """PR #20 Bug #2: livrable_infra = strict PA→PB paths, not all reusable."""
    from auvergne_pipeline.main import run_for_sros

    output = tmp_path / "test_infra.gpkg"
    summaries = run_for_sros(FIXTURE, [SRO_CODE], output_gpkg=output)

    assert len(summaries) == 1
    reusable_total = summaries[0]["reusable_total"]

    # Load livrable_infra from the output GPKG (may be absent if no routes)
    from pyogrio import list_layers
    layers = list_layers(str(output))[:, 0]
    if "livrable_infra" in layers:
        infra_gdf = gpd.read_file(output, layer="livrable_infra")
        # PR #20: livrable_infra should be STRICTLY less than full reusable
        assert len(infra_gdf) < reusable_total, (
            f"PR #20: livrable_infra should contain only routed edges, "
            f"but has {len(infra_gdf)} >= reusable_total={reusable_total}"
        )
    else:
        # No routed edges = valid (empty infra on disconnected fixture)
        pass


def test_layer_styles_after_e2e(tmp_path, empty_ign_routes):
    """PR #19 Bug #3: layer_styles table filled after E2E pipeline run."""
    from auvergne_pipeline.main import run_for_sros

    output = tmp_path / "test_styles.gpkg"
    run_for_sros(FIXTURE, [SRO_CODE], output_gpkg=output)

    import sqlite3
    conn = sqlite3.connect(str(output))
    n = conn.execute("SELECT COUNT(*) FROM layer_styles").fetchone()[0]
    conn.close()
    assert n >= 6, f"Expected >=6 QML styles in layer_styles, found {n}"


def test_qml_sidecars_after_e2e(tmp_path, empty_ign_routes):
    """PR #21: sidecars deprecated — GPKG + .qgz only, no .qml sidecars."""
    from auvergne_pipeline.main import run_for_sros

    output = tmp_path / "test_sidecars.gpkg"
    run_for_sros(FIXTURE, [SRO_CODE], output_gpkg=output)

    # PR #21: sidecars deprecated — only GPKG + .qgz should exist
    sidecars_exist = False
    for layer_name in ("livrable_pa", "livrable_bal", "livrable_infra",
                       "livrable_zapa", "livrable_parcelles", "livrable_zasro"):
        sidecar = tmp_path / f"test_sidecars_{layer_name}.qml"
        if sidecar.exists():
            sidecars_exist = True
    assert not sidecars_exist, "PR #21: sidecars .qml are deprecated, should not exist"


# ---------------------------------------------------------------------------
# PR #23 regression E2E tests
# ---------------------------------------------------------------------------


def test_cdc_log_emitted_when_bt_present(tmp_path, empty_ign_routes, caplog):
    """PR #23 Bug B: a `[CDC]` log line is emitted when BT clip happens."""
    import logging

    from auvergne_pipeline.main import run_for_sros

    output = tmp_path / "test_cdc.gpkg"
    with caplog.at_level(logging.INFO, logger="auvergne_pipeline"):
        run_for_sros(FIXTURE, [SRO_CODE], output_gpkg=output)
    cdc_lines = [r for r in caplog.records if "[CDC]" in r.getMessage()]
    assert cdc_lines, "Expected at least one [CDC] log line during BT clip"
    msg = cdc_lines[0].getMessage()
    assert "BT clip public" in msg
    assert SRO_CODE in msg


def test_livrable_infra_carries_infra_type_column(tmp_path, empty_ign_routes):
    """PR #23 Bug A: livrable_infra rows must carry an ``infra_type`` column."""
    from auvergne_pipeline.main import run_for_sros

    output = tmp_path / "test_infra_type.gpkg"
    run_for_sros(FIXTURE, [SRO_CODE], output_gpkg=output)

    from pyogrio import list_layers
    layers = list_layers(str(output))[:, 0]
    if "livrable_infra" not in layers:
        pytest.skip("No routed edges on this fixture; livrable_infra absent")
    infra = gpd.read_file(output, layer="livrable_infra")
    assert "infra_type" in infra.columns, (
        "livrable_infra must carry infra_type for the QML to colour edges"
    )
