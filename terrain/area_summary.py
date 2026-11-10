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
