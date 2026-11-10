# -*- coding: utf-8 -*-
"""Smooth contour lines for map-style display (like a forest plan map).

Contours from a fine DEM (0.5-1 m) follow every small bump. Here the DEM is
first averaged to a coarser grid and blurred (Gaussian), so the lines come
from one smoothed surface and never cross; then the lines are rounded a little
(Chaikin) and tiny closed loops are dropped.
"""

import os

import numpy as np
from osgeo import gdal, ogr, osr

INTERVAL_M = 10.0          # minor contours
INDEX_INTERVAL_M = 100.0   # index (thickest) contours, as in the GSI style
SMOOTH_CELL_M = 5.0        # DEM is averaged to this grid first ...
GAUSSIAN_SIGMA_CELLS = 1.5  # ... then blurred (1.5 cells = 7.5 m)
CHAIKIN_ITERATIONS = 3
CHAIKIN_OFFSET = 0.25
MIN_LOOP_LENGTH_M = 60.0   # closed loops shorter than this are dropped
METHOD_TEXT = (
    "DEM averaged to 5 m, Gaussian blur (sigma 7.5 m), GDAL contours, "
    "Chaikin rounding (3 passes), closed loops under 60 m removed"
)


def _gaussian(array, sigma):
    """Gaussian blur that ignores NaN cells (separable, numpy only)."""

    radius = max(1, int(3 * sigma))
    x = np.arange(-radius, radius + 1)
    kernel = np.exp(-(x ** 2) / (2.0 * sigma ** 2))
    kernel /= kernel.sum()
    valid = np.isfinite(array)
    filled = np.where(valid, array, 0.0)
    weight = valid.astype("float64")

    def convolve(data):
        data = np.apply_along_axis(lambda v: np.convolve(v, kernel, mode="same"), 0, data)
        return np.apply_along_axis(lambda v: np.convolve(v, kernel, mode="same"), 1, data)

    result = convolve(filled) / np.maximum(convolve(weight), 1e-9)
    result[~valid] = np.nan
    return result


def _write(path, data, gt, crs_wkt):
    rows, cols = data.shape
    dataset = gdal.GetDriverByName("GTiff").Create(path, cols, rows, 1, gdal.GDT_Float32)
    dataset.SetGeoTransform(gt)
    dataset.SetProjection(crs_wkt)
    band = dataset.GetRasterBand(1)
    band.SetNoDataValue(-9999.0)
    band.WriteArray(np.where(np.isfinite(data), data, -9999.0).astype("float32"))
    dataset = None


def compute_contours(data, gt, crs_wkt, cell_size, area_wkt, work_dir, context):
    """Smoothed contour lines clipped to the analysis area.

    data, gt, crs_wkt : the prepared DEM (the whole acquired area, so lines
                        near the area edge are not distorted by the blur)
    area_wkt          : analysis area in the same CRS (lines are clipped to it)
    Returns a list of (wkt, elevation).
    """
    from qgis.core import QgsGeometry

    os.makedirs(work_dir, exist_ok=True)
    source = os.path.join(work_dir, "contour_dem.tif")
    _write(source, data, gt, crs_wkt)
    context.check()

    context.report("Contours: smoothing the DEM...")
    target = max(SMOOTH_CELL_M, float(cell_size))
    coarse_path = os.path.join(work_dir, "contour_dem_coarse.tif")
    warped = gdal.Warp(
        coarse_path, source, xRes=target, yRes=target,
        resampleAlg="average" if target > cell_size else "bilinear",
        srcNodata=-9999.0, dstNodata=-9999.0,
    )
    coarse_gt = warped.GetGeoTransform()
    coarse = warped.GetRasterBand(1).ReadAsArray().astype("float64")
    warped = None
    coarse[coarse <= -9998.0] = np.nan
    smooth_path = os.path.join(work_dir, "contour_dem_smooth.tif")
    _write(smooth_path, _gaussian(coarse, GAUSSIAN_SIGMA_CELLS), coarse_gt, crs_wkt)
    context.check()

    context.report("Contours: tracing lines...")
    dataset = gdal.Open(smooth_path)
    reference = osr.SpatialReference()
    reference.ImportFromWkt(crs_wkt)
    memory = ogr.GetDriverByName("Memory").CreateDataSource("")
    layer = memory.CreateLayer("contours", reference, ogr.wkbLineString)
    layer.CreateField(ogr.FieldDefn("id", ogr.OFTInteger))
    layer.CreateField(ogr.FieldDefn("elev", ogr.OFTReal))
    gdal.ContourGenerate(dataset.GetRasterBand(1), INTERVAL_M, 0, [], 1, -9999.0, layer, 0, 1)
    dataset = None
    context.check()

    context.report("Contours: rounding lines...")
    area = ogr.CreateGeometryFromWkt(area_wkt)
    lines = []
    for feature in layer:
        geometry = feature.GetGeometryRef()
        if geometry is None or geometry.IsEmpty():
            continue
        points = geometry.GetPointCount()
        closed = points > 2 and geometry.GetPoint_2D(0) == geometry.GetPoint_2D(points - 1)
        if closed and geometry.Length() < MIN_LOOP_LENGTH_M:
            continue
        rounded = QgsGeometry.fromWkt(geometry.ExportToWkt()).smooth(
            CHAIKIN_ITERATIONS, CHAIKIN_OFFSET
        )
        clipped = ogr.CreateGeometryFromWkt(rounded.asWkt()).Intersection(area)
        if clipped is None or clipped.IsEmpty() or clipped.Length() <= 0:
            continue
        lines.append((ogr.ForceToMultiLineString(clipped).ExportToWkt(), feature.GetField("elev")))
    feature = None
    layer = None
    memory = None
    return lines


def write_contours_gpkg(path, lines, crs_wkt):
    reference = osr.SpatialReference()
    reference.ImportFromWkt(crs_wkt)
    if os.path.exists(path):
        os.remove(path)
    datasource = ogr.GetDriverByName("GPKG").CreateDataSource(path)
    layer = datasource.CreateLayer("contours", reference, ogr.wkbMultiLineString)
    layer.CreateField(ogr.FieldDefn("elev", ogr.OFTReal))
    index_field = ogr.FieldDefn("index", ogr.OFTInteger)
    index_field.SetSubType(ogr.OFSTBoolean)
    layer.CreateField(index_field)
    for wkt, elevation in lines:
        feature = ogr.Feature(layer.GetLayerDefn())
        feature.SetField("elev", elevation)
        is_index = abs(elevation / INDEX_INTERVAL_M - round(elevation / INDEX_INTERVAL_M)) < 1e-6
        feature.SetField("index", 1 if is_index else 0)
        feature.SetGeometry(ogr.CreateGeometryFromWkt(wkt))
        layer.CreateFeature(feature)
        feature = None
    layer = None
    datasource = None
    return path
