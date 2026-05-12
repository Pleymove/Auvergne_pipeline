"""Pipeline configuration: paths, thresholds, layer names, SRO pilots.

Single source of truth for tunable values referenced across modules. Paths
default to the local environment described in the Notion design page; they can
be overridden via environment variables when running on another machine.
"""

from __future__ import annotations

import os
from pathlib import Path

# Project CRS (RGF93 Lambert-93). Everything is computed in metres.
PROJECT_CRS = "EPSG:2154"

# Default location of the local GPKG produced by the QGIS clone script.
DEFAULT_GPKG = Path(
    os.environ.get(
        "AUVERGNE_GPKG",
        str(Path.home() / "Desktop" / "auvergne_local" / "auvergne_local.gpkg"),
    )
)

# Layers expected inside the GPKG. The pipeline only reads the ones it needs.
LAYER_ZA_SRO = "za_sro"
LAYER_BAL = "bal"
LAYER_GEORESO_ZAPA = "georeso_zapa"
LAYER_GEORESO_PA = "georeso_pa"
LAYER_PARCELLE = "parcelle"

LAYER_ATHD = "existant_athd_artere"
LAYER_BT = "existant_bt"
LAYER_FT_ARCITI = "existant_ft_arciti"
LAYER_CHEMINEMENT = "existant_t_cheminement"

# Output (livrable) layers, written by writer.py in later iterations.
LAYER_LIVRABLE_PA = "livrable_pa"
LAYER_LIVRABLE_ZAPA = "livrable_zapa"
LAYER_LIVRABLE_INFRA = "livrable_infra"

# Output GPKG path (default, overridable via --output CLI)
DEFAULT_OUTPUT_GPKG = Path("output/auvergne_outputs.gpkg")

INFRA_LAYERS = (LAYER_ATHD, LAYER_BT, LAYER_FT_ARCITI, LAYER_CHEMINEMENT)

# Spatial buffers (metres) applied around the SRO bounding box when clipping.
PARCELLE_BBOX_BUFFER_M = 150
INFRA_BBOX_BUFFER_M = 200

# D3 threshold: a BAT is auto-eligible if its parcel boundary is within this
# distance of an existing reusable infrastructure segment.
SEUIL_D3_M = 100

# Pilot SRO codes used for end-to-end smoke testing (Puy-de-Dome).
PILOT_SROS = (
    "63149/M06/PMZ/42478",
    "63257/QSB/PMZ/56934",
    "63210/M06/PMZ/29655",
    "63048/QBO/PMZ/56826",
    "63258/LLW/PMZ/24228",
)

# ---------------------------------------------------------------------------
# IGN BD TOPO routes (PR #14)
# ---------------------------------------------------------------------------
IGN_WFS_BASE = "https://data.geopf.fr/wfs/ows"
IGN_TYPENAME = "BDTOPO_V3:troncon_de_route"
IGN_BBOX_BUFFER_M = 500.0
IGN_PAGE_SIZE = 5000
IGN_TIMEOUT_S = 60
IGN_RETRY = 3
CACHE_DIR_IGN = Path("cache/ign_routes")

# ---------------------------------------------------------------------------
# PB fictif (PR #14)
# ---------------------------------------------------------------------------
PB_D3_MAX_M = 100.0
PB_PRISES_DEFAUT = 5
PB_PRISES_MAX = 10

# ---------------------------------------------------------------------------
# Routing (PR #14)
# ---------------------------------------------------------------------------
ROUTING_SNAP_TOL_M = 0.5
GC_NEUF_MODE_POSE = "C0"
