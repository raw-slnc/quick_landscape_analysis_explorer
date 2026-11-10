# -*- coding: utf-8 -*-
"""Catchment (sub-basin) polygons with GRASS r.watershed.

Same tool and settings as Morizon Next's catchment aggregation (MFD,
convergence 5), so the boundaries agree. The size of the smallest catchment
is set as an area and the DEM is resampled to a fixed cell size first, so
the result does not depend on the DEM source's resolution.
"""

import gc
import os
import re
import shutil
import tempfile

import numpy as np
from osgeo import gdal, ogr, osr

# Catchments are computed on this grid (m) ...
CELL_SIZE_M = 5.0
# ... and the smallest catchment is this large (ha). Morizon Next uses 500
# cells, which is 5 ha on a 10 m DEM.
MIN_AREA_HA = 5.0
# r.watershed settings shared with Morizon Next.
CONVERGENCE = 5
MEMORY_MB = 300
METHOD_TEXT = "GRASS r.watershed, MFD (convergence 5)"


class WatershedError(Exception):
    """A message for the user."""


def grass_watershed_algorithm_id():
    """The registered r.watershed id (provider name differs by QGIS), or None."""
    from qgis.core import QgsApplication

    registry = QgsApplication.processingRegistry()
    for algorithm_id in ("grass:r.watershed", "grass7:r.watershed"):
        if registry.algorithmById(algorithm_id) is not None:
            return algorithm_id
    return None


def _ascii_safe_work_dir():
    """A work folder whose path has no full-width characters.

    GRASS fails on paths with non-ASCII characters (for example a Japanese
    Windows user name in the temp folder). Like Morizon Next, use a folder at
    the root of the temp drive on Windows.
    """
    base = tempfile.gettempdir()
    drive = os.path.splitdrive(base)[0]
    root = os.path.join(drive + os.sep, "qlae_tmp") if drive else os.path.join(base, "qlae_tmp")
    if re.search("[^\x01-\x7E]", root):
        root = os.path.join(drive + os.sep if drive else os.sep, "qlae_tmp")
    os.makedirs(root, exist_ok=True)
    return tempfile.mkdtemp(prefix="ws_", dir=root)


def _write_dem(path, data, gt, crs_wkt):
    rows, cols = data.shape
    dataset = gdal.GetDriverByName("GTiff").Create(
        path, cols, rows, 1, gdal.GDT_Float32, options=["COMPRESS=LZW", "TILED=YES"]
    )
    dataset.SetGeoTransform(gt)
    dataset.SetProjection(crs_wkt)
    band = dataset.GetRasterBand(1)
    band.SetNoDataValue(-9999.0)
    band.WriteArray(np.where(np.isfinite(data), data, -9999.0).astype("float32"))
    dataset = None


def compute_catchments(data, gt, crs_wkt, frame_envelope, context, feedback, cell_size):
    """Catchment polygons for the frame, computed on the whole (wide) DEM.

    data, gt, crs_wkt : the prepared DEM covering the wide acquisition area
    frame_envelope    : (xmin, xmax, ymin, ymax) of the output A-series frame
    context           : has report(text) and check()
    feedback          : QgsProcessingFeedback (cancelled with the worker)

    Returns a list of dicts: wkt (clipped to the frame), area_ha (whole
    catchment), complete (False when it reaches the edge of the wide DEM).
    """
    import processing
    from processing.tools import dataobjects
    from qgis.core import QgsProcessingUtils

    algorithm_id = grass_watershed_algorithm_id()
    if algorithm_id is None:
        raise WatershedError(
            "GRASS (r.watershed) is not available in this QGIS. "
            "Enable the GRASS processing provider to create catchments."
        )
    work_dir = _ascii_safe_work_dir()
    try:
        dem_path = os.path.join(work_dir, "dem.tif")
        _write_dem(dem_path, data, gt, crs_wkt)
        context.check()

        # Resample to the fixed catchment grid (averaging finer DEMs).
        target = max(CELL_SIZE_M, float(cell_size))
        resampled = os.path.join(work_dir, "dem_ws.tif")
        context.report("Catchments: preparing the {:.0f} m grid...".format(target))
        warped = gdal.Warp(
            resampled, dem_path, xRes=target, yRes=target,
            resampleAlg="average" if target > cell_size else "bilinear",
            srcNodata=-9999.0, dstNodata=-9999.0, format="GTiff",
        )
        if warped is None:
            raise WatershedError("Could not prepare the DEM for catchments.")
        warped = None
        context.check()

        threshold = max(1, int(round(MIN_AREA_HA * 10000.0 / (target * target))))
        basin_path = os.path.join(work_dir, "basin.tif")
        processing_context = dataobjects.createContext(feedback)
        # GRASS asks for the temporary folder more than once; give it the
        # ASCII-safe one first, with "/" separators (see Morizon Next).
        processing_context.setTemporaryFolder(work_dir.replace("\\", "/"))
        QgsProcessingUtils.tempFolder(processing_context)
        context.report("Catchments: computing flow (GRASS r.watershed, MFD)...")
        processing.run(algorithm_id, {
            "elevation": resampled,
            "-4": False, "-a": False, "-b": False, "-m": False, "-s": False,
            "GRASS_RASTER_FORMAT_META": "", "GRASS_RASTER_FORMAT_OPT": "",
            "GRASS_REGION_CELLSIZE_PARAMETER": 0, "GRASS_REGION_PARAMETER": None,
            "accumulation": None, "basin": basin_path, "blocking": None,
            "convergence": CONVERGENCE, "depression": None, "disturbed_land": None,
            "drainage": None, "flow": None, "half_basin": None, "length_slope": None,
            "max_slope_length": None, "memory": MEMORY_MB, "slope_steepness": None,
            "spi": None, "stream": None, "tci": None, "threshold": threshold,
        }, feedback=feedback, context=processing_context)
        processing_context = None
        gc.collect()
        context.check()
        if not os.path.exists(basin_path):
            raise WatershedError("GRASS r.watershed did not produce catchments.")

        context.report("Catchments: making polygons...")
        return _basin_polygons(basin_path, crs_wkt, frame_envelope, context)
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)


def _basin_polygons(basin_path, crs_wkt, frame_envelope, context):
    dataset = gdal.Open(basin_path)
    band = dataset.GetRasterBand(1)
    basin_gt = dataset.GetGeoTransform()
    cols, rows = dataset.RasterXSize, dataset.RasterYSize
    reference = osr.SpatialReference()
    reference.ImportFromWkt(crs_wkt)
    memory = ogr.GetDriverByName("Memory").CreateDataSource("")
    layer = memory.CreateLayer("basins", reference, ogr.wkbPolygon)
    layer.CreateField(ogr.FieldDefn("DN", ogr.OFTInteger))
    gdal.Polygonize(band, band.GetMaskBand(), layer, 0)
    context.check()

    # One (multi)polygon per catchment value.
    parts = {}
    for feature in layer:
        value = feature.GetField("DN")
        geometry = feature.GetGeometryRef()
        if value is None or value <= 0 or geometry is None:
            continue
        parts.setdefault(value, []).append(geometry.Clone())
    feature = None
    layer = None
    memory = None
    band = None
    dataset = None

    xmin, ymax = basin_gt[0], basin_gt[3]
    xmax = xmin + cols * basin_gt[1]
    ymin = ymax + rows * basin_gt[5]
    tolerance = abs(basin_gt[1]) * 0.5
    inner = _rectangle(xmin + tolerance, xmax - tolerance, ymin + tolerance, ymax - tolerance)
    frame = _rectangle(*frame_envelope)

    catchments = []
    for value, geometries in sorted(parts.items()):
        collection = ogr.Geometry(ogr.wkbMultiPolygon)
        for geometry in geometries:
            collection.AddGeometry(geometry)
        whole = collection.UnionCascaded()
        if whole is None or whole.IsEmpty() or not whole.Intersects(frame):
            continue
        clipped = whole.Intersection(frame)
        if clipped is None or clipped.IsEmpty() or clipped.GetArea() <= 0:
            continue
        catchments.append({
            "id": int(value),
            "wkt": clipped.ExportToWkt(),
            "area_ha": round(whole.GetArea() / 10000.0, 3),
            # Reaching the edge of the wide DEM means the catchment goes on
            # beyond what was fetched.
            "complete": bool(inner.Contains(whole)),
        })
    return catchments


def _rectangle(xmin, xmax, ymin, ymax):
    ring = ogr.Geometry(ogr.wkbLinearRing)
    for x, y in ((xmin, ymax), (xmax, ymax), (xmax, ymin), (xmin, ymin), (xmin, ymax)):
        ring.AddPoint_2D(x, y)
    polygon = ogr.Geometry(ogr.wkbPolygon)
    polygon.AddGeometry(ring)
    return polygon


def write_catchments_gpkg(path, catchments, crs_wkt):
    reference = osr.SpatialReference()
    reference.ImportFromWkt(crs_wkt)
    if os.path.exists(path):
        os.remove(path)
    datasource = ogr.GetDriverByName("GPKG").CreateDataSource(path)
    layer = datasource.CreateLayer("catchments", reference, ogr.wkbMultiPolygon)
    for name, kind in (("basin_id", ogr.OFTInteger), ("area_ha", ogr.OFTReal),
                       ("complete", ogr.OFTInteger)):
        layer.CreateField(ogr.FieldDefn(name, kind))
    for item in catchments:
        feature = ogr.Feature(layer.GetLayerDefn())
        feature.SetField("basin_id", item["id"])
        feature.SetField("area_ha", item["area_ha"])
        feature.SetField("complete", 1 if item["complete"] else 0)
        geometry = ogr.ForceToMultiPolygon(ogr.CreateGeometryFromWkt(item["wkt"]))
        feature.SetGeometry(geometry)
        layer.CreateFeature(feature)
        feature = None
    layer = None
    datasource = None
    return path
