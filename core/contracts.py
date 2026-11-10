# -*- coding: utf-8 -*-

from dataclasses import dataclass, field
from typing import Any, Dict, Tuple


@dataclass(frozen=True)
class DatasetRoute:
    """Connects one data entrance to its analysis and summary handlers."""

    route_id: str
    source_kind: str
    analysis_id: str
    summary_id: str


@dataclass(frozen=True)
class PairContext:
    aoi_layer: Any
    data_layer: Any
    selected_only: bool
    range_mode: str = "polygon"
    map_extent: Any = None
    map_crs: Any = None


@dataclass
class AnalysisResult:
    route_id: str
    facts: Dict[str, Any] = field(default_factory=dict)
    warnings: Tuple[str, ...] = ()


class PairInspectionError(Exception):
    def __init__(self, code):
        super().__init__(code)
        self.code = code
