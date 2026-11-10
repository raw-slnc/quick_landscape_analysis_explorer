# -*- coding: utf-8 -*-

from qgis.core import (
    QgsCoordinateTransform,
    QgsCsException,
    QgsDistanceArea,
    QgsGeometry,
    QgsProject,
    QgsRasterLayer,
    QgsRectangle,
    QgsVectorLayer,
    QgsWkbTypes,
)

from ..core.contracts import AnalysisResult, PairInspectionError


def inspect_pair(route, context):
    """Inspect a polygon AOI and one QGIS data layer as a real working pair."""

    data = context.data_layer
    if data is None:
        raise PairInspectionError("data_required")
    _validate_source_kind(route.source_kind, data)

    if context.range_mode == "map_extent":
        if context.map_extent is None or context.map_crs is None:
            raise PairInspectionError("map_extent_required")
        aoi_crs = context.map_crs
        aoi_extent = QgsRectangle(context.map_extent)
        if (
            not aoi_crs.isValid()
            or aoi_extent.isEmpty()
        ):
            raise PairInspectionError("map_extent_required")
        measurement = QgsDistanceArea()
        measurement.setSourceCrs(
            aoi_crs, QgsProject.instance().transformContext()
        )
        measurement.setEllipsoid("WGS84")
        area_m2 = measurement.measureArea(QgsGeometry.fromRect(aoi_extent))
        aoi_layer_name = ""
        aoi_source = "preview_extent"
        feature_mode = "map_extent"
        feature_count = 1
        valid_geometries = 1
    else:
        aoi = context.aoi_layer
        if not isinstance(aoi, QgsVectorLayer):
            raise PairInspectionError("aoi_required")
        if aoi.geometryType() != QgsWkbTypes.PolygonGeometry:
            raise PairInspectionError("aoi_must_be_polygon")

        if context.selected_only:
            if aoi.selectedFeatureCount() == 0:
                raise PairInspectionError("selection_required")
            features = aoi.getSelectedFeatures()
            feature_mode = "selected"
            feature_count = aoi.selectedFeatureCount()
        else:
            features = aoi.getFeatures()
            feature_mode = "all"
            feature_count = aoi.featureCount()

        aoi_extent = QgsRectangle()
        measurement = QgsDistanceArea()
        measurement.setSourceCrs(
            aoi.crs(), QgsProject.instance().transformContext()
        )
        measurement.setEllipsoid("WGS84")
        area_m2 = 0.0
        valid_geometries = 0

        for feature in features:
            geometry = feature.geometry()
            if geometry is None or geometry.isEmpty():
                continue
            box = geometry.boundingBox()
            if aoi_extent.isNull():
                aoi_extent = QgsRectangle(box)
            else:
                aoi_extent.combineExtentWith(box)
            area_m2 += measurement.measureArea(geometry)
            valid_geometries += 1

        if valid_geometries == 0 or aoi_extent.isNull():
            raise PairInspectionError("aoi_has_no_geometry")
        aoi_crs = aoi.crs()
        aoi_layer_name = aoi.name()
        aoi_source = "polygon_layer"

    data_extent = _extent_in_aoi_crs(data, aoi_crs)
    overlaps = aoi_extent.intersects(data_extent)
    warnings = () if overlaps else ("bbox_no_overlap",)

    facts = {
        "aoi_source": aoi_source,
        "aoi_layer": aoi_layer_name,
        "aoi_feature_mode": feature_mode,
        "aoi_features": feature_count,
        "aoi_valid_geometries": valid_geometries,
        "aoi_area_km2": area_m2 / 1_000_000.0,
        "aoi_crs": _crs_label(aoi_crs),
        "data_layer": data.name(),
        "data_kind": _data_kind(data),
        "data_crs": _crs_label(data.crs()),
        "data_extent": _extent_text(data_extent),
        "bbox_overlap": overlaps,
    }

    if isinstance(data, QgsRasterLayer):
        facts.update(
            {
                "raster_bands": data.bandCount(),
                "raster_width": data.width(),
                "raster_height": data.height(),
                "pixel_size_x": abs(data.rasterUnitsPerPixelX()),
                "pixel_size_y": abs(data.rasterUnitsPerPixelY()),
            }
        )
    elif isinstance(data, QgsVectorLayer):
        facts.update(
            {
                "vector_features": data.featureCount(),
                "vector_geometry": QgsWkbTypes.displayString(data.wkbType()),
                "vector_fields": len(data.fields()),
            }
        )

    return AnalysisResult(route_id=route.route_id, facts=facts, warnings=warnings)


def _validate_source_kind(source_kind, layer):
    if source_kind == "raster" and not isinstance(layer, QgsRasterLayer):
        raise PairInspectionError("data_must_be_raster")
    if source_kind in ("vector", "polygon") and not isinstance(layer, QgsVectorLayer):
        raise PairInspectionError("data_must_be_vector")
    if source_kind == "polygon" and layer.geometryType() != QgsWkbTypes.PolygonGeometry:
        raise PairInspectionError("data_must_be_polygon")


def _extent_in_aoi_crs(layer, aoi_crs):
    extent = layer.extent()
    if not layer.crs().isValid() or not aoi_crs.isValid() or layer.crs() == aoi_crs:
        return QgsRectangle(extent)
    try:
        transform = QgsCoordinateTransform(
            layer.crs(), aoi_crs, QgsProject.instance().transformContext()
        )
        return transform.transformBoundingBox(extent)
    except QgsCsException as error:
        raise PairInspectionError("extent_transform_failed") from error


def _crs_label(crs):
    if not crs.isValid():
        return "Not defined"
    return crs.authid() or crs.description()


def _data_kind(layer):
    if isinstance(layer, QgsRasterLayer):
        return "raster"
    if isinstance(layer, QgsVectorLayer):
        return "vector"
    return "other"


def _extent_text(extent):
    return "{:.3f}, {:.3f} — {:.3f}, {:.3f}".format(
        extent.xMinimum(), extent.yMinimum(), extent.xMaximum(), extent.yMaximum()
    )
