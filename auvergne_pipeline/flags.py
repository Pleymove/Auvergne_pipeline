"""Lightweight flag collector for cas-a-verifier (iteration 2).

The Notion exporter (reporter.py) lands in iteration 3+. For now we only
collect flags in memory and log per-SRO recaps in main.py.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from typing import Optional

import pandas as pd


FLAG_TYPES: dict[str, str] = {
    "BAT_HORS_CADASTRE": "BAT non contenu dans aucune parcelle",
    "BAT_ENCLAVE": "BAT enclave, mesure D3 sur parcelle adjacente",
    "PA_ORPHELIN_CREE": "Nouveau PA cree au barycentre (a valider)",
    "PA_PB_DECONNECTES": "Pas de chemin entre PA et PB sur infra reutilisable",
    "PRIVE_TRAVERSE": "Trace cree traverse parcelle privee (hors ENEDIS)",
    "HYDRO_TRAVERSE": "Trace cree traverse cours d eau / fleuve / lac",
    "TRACE_LONG": "Trace cree > seuil",
    "S7_DETECTE": "Code S7 detecte (non documente CDC)",
}


@dataclass
class FlagCollector:
    """In-memory accumulator of per-SRO cas-a-verifier."""

    sro_code: str
    flags: list[dict] = field(default_factory=list)

    def add(
        self,
        flag_type: str,
        target_url: Optional[str] = None,
        message: Optional[str] = None,
    ) -> None:
        description = FLAG_TYPES.get(flag_type, flag_type)
        self.flags.append(
            {
                "flag_type": flag_type,
                "sro": self.sro_code,
                "target": target_url,
                "message": message or description,
            }
        )

    def counts(self) -> Counter:
        return Counter(f["flag_type"] for f in self.flags)

    def to_dataframe(self) -> pd.DataFrame:
        cols = ["flag_type", "sro", "target", "message"]
        if not self.flags:
            return pd.DataFrame(columns=cols)
        return pd.DataFrame(self.flags, columns=cols)
