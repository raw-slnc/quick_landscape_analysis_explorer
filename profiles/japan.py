# -*- coding: utf-8 -*-
"""Initial Japan profile.

The first stage uses layers already loaded in QGIS. Later stages can replace a
route's source with a downloader without changing the AOI contract.
"""

from ..core.contracts import DatasetRoute


PROFILE_ID = "japan"

ROUTES = (
    DatasetRoute(
        route_id="terrain_raster",
        source_kind="raster",
        analysis_id="pair_inspection",
        summary_id="pair_text",
    ),
    DatasetRoute(
        route_id="forest_polygons",
        source_kind="polygon",
        analysis_id="pair_inspection",
        summary_id="pair_text",
    ),
    DatasetRoute(
        route_id="reference_vector",
        source_kind="vector",
        analysis_id="pair_inspection",
        summary_id="pair_text",
    ),
)
