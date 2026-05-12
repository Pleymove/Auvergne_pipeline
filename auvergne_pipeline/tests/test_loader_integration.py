"""Loader integration tests restored after PR #24 trim.

PR #24 replaced the loader integration tests with two unit tests for
``filter_bt_to_public_domain``. The previous coverage (load every pilot
SRO, list available SROs, raise on unknown SRO) was useful and skipped
automatically when the local GPKG was absent — restoring it here.
"""

from __future__ import annotations

import pytest

from auvergne_pipeline import config, loader


pytestmark = pytest.mark.skipif(
    not config.DEFAULT_GPKG.exists(),
    reason=f"GPKG local absent: {config.DEFAULT_GPKG}",
)


@pytest.mark.parametrize("sro_code", config.PILOT_SROS)
def test_load_pilot_sro(sro_code: str):
    """Each pilot SRO must load cleanly with all expected layer keys."""
    layers = loader.load_sro(config.DEFAULT_GPKG, sro_code)

    expected_keys = {
        "za_sro",
        "bal",
        "georeso_zapa",
        "georeso_pa",
        "parcelle",
        config.LAYER_ATHD,
        config.LAYER_BT,
        config.LAYER_FT_ARCITI,
        config.LAYER_CHEMINEMENT,
    }
    assert expected_keys.issubset(layers.keys())

    assert len(layers["za_sro"]) == 1
    # Sanity: at least one BAT in the SRO (otherwise the SRO has nothing to do).
    assert len(layers["bal"]) > 0, f"SRO pilote {sro_code} sans BAT"


def test_list_available_sros_contains_all_pilots():
    available = set(loader.list_available_sros(config.DEFAULT_GPKG))
    missing = set(config.PILOT_SROS) - available
    assert not missing, f"SRO pilotes absents du GPKG: {sorted(missing)}"


def test_unknown_sro_raises():
    with pytest.raises(loader.SroNotFoundError):
        loader.load_sro(config.DEFAULT_GPKG, "00000/AAA/PMZ/00000")
