# -*- coding: utf-8 -*-
"""Numbers that sum up the analysis area (for the print layout).

Read from the analysis package itself: the DEM (cells outside the analysis
area are NoData there) and the slope raster when it was made, so the numbers
match the map. Without a slope raster the slope is computed from the DEM in
the same way.
"""

import math

import numpy as np
from osgeo import gdal, ogr

from ..core import packages
from .analysis import compute_slope_deg

# Same classes as the slope legend: (upper bound, label), upper bound included.
SLOPE_CLASSES = (
    (20.0, "<= 20"),
    (25.0, "20 - 25"),
    (30.0, "25 - 30"),
    (35.0, "30 - 35"),
    (40.0, "35 - 40"),
    (45.0, "40 - 45"),
    (float("inf"), "> 45"),
)


def _read(path):
    dataset = gdal.Open(path)
    if dataset is None:
        return None, None
    band = dataset.GetRasterBand(1)
    data = band.ReadAsArray().astype("float64")
    nodata = band.GetNoDataValue()
    if nodata is not None:
        data[data == nodata] = np.nan
    gt = dataset.GetGeoTransform()
    dataset = None
    return data, gt


# Elevation bands: the smallest of these steps that gives at most this many.
BAND_STEPS = (5, 10, 20, 25, 50, 100, 200, 250, 500)
MAX_BANDS = 8
# Largest catchments listed.
LARGEST_CATCHMENTS = 5


def _elevation_bands(values, cell_area):
    low, high = float(values.min()), float(values.max())
    step = next(
        (step for step in BAND_STEPS
         if math.ceil(high / step) - math.floor(low / step) <= MAX_BANDS),
        BAND_STEPS[-1],
    )
    bands = []
    start = math.floor(low / step) * step
    while start < high:
        end = start + step
        cells = int(np.count_nonzero((values >= start) & (values < end)))
        if end >= high:
            cells = int(np.count_nonzero(values >= start))
        bands.append({
            "label": "{:g} - {:g}".format(start, end),
            "area_ha": cells * cell_area / 10000.0,
            "percent": 100.0 * cells / max(1, values.size),
        })
        start = end
    return bands


def _largest_catchments(package_path, member):
    dataset = ogr.Open(packages.vsi_path(package_path, member))
    if dataset is None:
        return []
    layer = dataset.GetLayer(0)
    areas = []
    for feature in layer:
        area = feature.GetField("area_ha")
        if area is not None:
            areas.append({"area_ha": float(area), "complete": bool(feature.GetField("complete"))})
    feature = None
    layer = None
    dataset = None
    areas.sort(key=lambda item: item["area_ha"], reverse=True)
    return areas[:LARGEST_CATCHMENTS]


def _area_mask(package_path, manifest, gt, shape, projection):
    """The analysis area (area/aoi.wkt of the package) on a raster's grid, as
    a boolean array; None when it cannot be made."""
    import zipfile

    from osgeo import osr

    area = manifest.get("area") or {}
    try:
        with zipfile.ZipFile(package_path) as package:
            wkt = package.read(area.get("geometry") or "area/aoi.wkt").decode("utf-8")
    except (OSError, KeyError, zipfile.BadZipFile):
        return None
    geometry = ogr.CreateGeometryFromWkt(wkt)
    if geometry is None or not area.get("crs_wkt") or not projection:
        return None
    source, target = osr.SpatialReference(), osr.SpatialReference()
    source.ImportFromWkt(area["crs_wkt"])
    target.ImportFromWkt(projection)
    for srs in (source, target):
        srs.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
    geometry.Transform(osr.CoordinateTransformation(source, target))
    raster = gdal.GetDriverByName("MEM").Create("", shape[1], shape[0], 1, gdal.GDT_Byte)
    raster.SetGeoTransform(gt)
    raster.SetProjection(projection)
    vectors = ogr.GetDriverByName("Memory").CreateDataSource("")
    layer = vectors.CreateLayer("area", target, ogr.wkbUnknown)
    feature = ogr.Feature(layer.GetLayerDefn())
    feature.SetGeometry(geometry)
    layer.CreateFeature(feature)
    gdal.RasterizeLayer(raster, [1], layer, burn_values=[1])
    mask = raster.GetRasterBand(1).ReadAsArray() == 1
    raster = layer = vectors = None
    return mask


def slope_classes(raster_path, items, package_path=None, manifest=None):
    """The areas of a slope raster in the classes of its own style (the map's
    classes): ``items`` are (upper value, label, colour), lowest first, the
    last upper value infinite. Counted within the analysis area when the
    package is known (the regional slope has values under trees and past
    it), else over the raster's values. (classes, mean slope), or None."""
    dataset = gdal.Open(raster_path)
    if dataset is None:
        return None
    projection = dataset.GetProjection()
    dataset = None
    slope, gt = _read(raster_path)
    if slope is None:
        return None
    mask = np.isfinite(slope)
    if package_path and manifest:
        area = _area_mask(package_path, manifest, gt, slope.shape, projection)
        if area is not None:
            mask &= area
    values = slope[mask]
    cell_area = abs(gt[1] * gt[5])
    classes = []
    lower = -np.inf
    for upper, label, color in items:
        cells = int(np.count_nonzero((values > lower) & (values <= upper)))
        classes.append({
            "label": label,
            "color": color,
            "area_ha": cells * cell_area / 10000.0,
            "percent": 100.0 * cells / max(1, values.size),
        })
        lower = upper
    return classes, (float(values.mean()) if values.size else float("nan"))


def area_summary(package_path, manifest):
    """Return a dict of numbers, or None when the DEM cannot be read."""

    outputs = manifest.get("outputs", {}) or {}
    dem_member = outputs.get("dem")
    if not dem_member:
        return None
    dem, gt = _read(packages.vsi_path(package_path, dem_member))
    if dem is None:
        return None
    valid = np.isfinite(dem)
    count = int(np.count_nonzero(valid))
    if count == 0:
        return None
    cell_area = abs(gt[1] * gt[5])
    slope = None
    if outputs.get("slope"):
        slope, _gt = _read(packages.vsi_path(package_path, outputs["slope"]))
        if slope is not None and slope.shape != dem.shape:
            slope = None
    if slope is None:
        slope = compute_slope_deg(dem, abs(gt[1]))
    values = dem[valid]
    slope_values = slope[valid & np.isfinite(slope)]
    classes = []
    lower = -np.inf
    for upper, label in SLOPE_CLASSES:
        cells = int(np.count_nonzero((slope_values > lower) & (slope_values <= upper)))
        classes.append({
            "label": label,
            "area_ha": cells * cell_area / 10000.0,
            "percent": 100.0 * cells / max(1, slope_values.size),
        })
        lower = upper
    watershed = manifest.get("watershed") or {}
    largest = []
    if outputs.get("watershed"):
        largest = _largest_catchments(package_path, outputs["watershed"])
    return {
        "area_ha": count * cell_area / 10000.0,
        "elevation_min": float(values.min()),
        "elevation_max": float(values.max()),
        "elevation_mean": float(values.mean()),
        "slope_mean": float(slope_values.mean()) if slope_values.size else float("nan"),
        "slope_classes": classes,
        "catchments": watershed.get("catchments") if watershed else None,
        "catchments_cut": watershed.get("incomplete") if watershed else None,
        "largest_catchments": largest,
        "elevation_bands": _elevation_bands(values, cell_area),
    }
