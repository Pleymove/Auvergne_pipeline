"""Pure-Python stdout progress parser used by the launcher GUI (PR #29 C).

The parser is intentionally PyQt-free so it can be unit-tested on a CI
machine that does not ship PyQt6 (the launcher itself requires PyQt6 and
is therefore not importable in CI). The GUI feeds every stdout line to
:class:`ProgressState.update` and reads back the current SRO / step /
percent for the QProgressBar / labels.
"""

from __future__ import annotations

import re
from typing import Optional

# Sub-step ordering inside a single SRO. Each step gets an equal weight
# slice of the SRO's progress slot. Updates only ratchet forward, so
# progress never moves backwards within an SRO.
SUBSTEP_ORDER = [
    "load",
    "ign",
    "cdc",
    "pb",
    "graph",
    "weld",
    "snap",
    "routing",
    "infra_qa",
    "writer",
    "qml",
    "qgz",
]
_SUBSTEP_INDEX = {s: i for i, s in enumerate(SUBSTEP_ORDER)}


_SRO_HEADER_RE = re.compile(r"===\s*SRO\s+(?P<sro>\S+)\s*===")
_SRO_DONE_RE = re.compile(r"\[OK\]\s+SRO\s+(?P<sro>\S+)\s+traite")


# Order matters: more-specific markers first. ``[ROUTING] Graphe brut``
# must match before the more general ``[ROUTING] `` substring.
LINE_TO_STEP: list[tuple[str, str, str]] = [
    ("[IGN]", "ign", "Chargement IGN"),
    ("[CDC]", "cdc", "Filtrage domaine public"),
    ("[PB QA]", "pb", "Creation PB"),
    ("[ROUTING] Graphe brut", "graph", "Construction graphe"),
    ("[WELD]", "weld", "Welding topologique"),
    ("[TOPO SNAP]", "snap", "Snap topologique"),
    ("[ROUTING] ", "routing", "Calcul routing / Dijkstra"),
    ("[INFRA QA]", "infra_qa", "Controle livrable infra"),
    ("writer :", "writer", "Ecriture GPKG"),
    ("[QML]", "qml", "Application styles QML"),
    ("[QGZ]", "qgz", "Ecriture projet QGIS"),
]


class ProgressState:
    """Track pipeline progress from stdout lines.

    Public attributes (read-only from the GUI):

    * ``total_sros``           — number of SROs scheduled in this run
    * ``sro_index``            — 1-based index of the SRO currently running
                                 (0 before the first ``=== SRO ... ===``)
    * ``current_sro``          — the SRO code from the header marker
    * ``current_step_id``      — id from :data:`SUBSTEP_ORDER`
    * ``current_step_label``   — French human label
    * ``completed_sros``       — number of SROs that already emitted ``[OK] traite``
    """

    def __init__(self, total_sros: int) -> None:
        self.total_sros = max(1, int(total_sros))
        self.sro_index = 0
        self.current_sro: str = ""
        self.current_step_id: str = "load"
        self.current_step_label: str = "Chargement SRO"
        self._max_substep_idx = 0
        self.completed_sros = 0

    @staticmethod
    def _line_to_substep(line: str) -> Optional[tuple[str, str]]:
        for marker, sid, label in LINE_TO_STEP:
            if marker in line:
                return sid, label
        return None

    def update(self, line: str) -> None:
        m = _SRO_HEADER_RE.search(line)
        if m:
            self.current_sro = m.group("sro")
            self.sro_index = min(self.sro_index + 1, self.total_sros)
            self.current_step_id = "load"
            self.current_step_label = "Chargement SRO"
            self._max_substep_idx = 0
            return

        m = _SRO_DONE_RE.search(line)
        if m:
            self.completed_sros = max(self.completed_sros, self.sro_index)
            return

        substep = self._line_to_substep(line)
        if substep is None:
            return
        sid, label = substep
        idx = _SUBSTEP_INDEX.get(sid, self._max_substep_idx)
        if idx >= self._max_substep_idx:
            self._max_substep_idx = idx
            self.current_step_id = sid
            self.current_step_label = label

    def progress_percent(self) -> int:
        """Return the global progress in [0, 100]."""
        if self.sro_index == 0:
            return 0
        per_sro = 100.0 / self.total_sros
        substep_frac = self._max_substep_idx / max(1, len(SUBSTEP_ORDER) - 1)
        completed = (self.sro_index - 1) * per_sro
        if self.completed_sros >= self.sro_index:
            return int(min(100.0, self.sro_index * per_sro))
        return int(min(100.0, completed + per_sro * substep_frac))
