# -*- coding: utf-8 -*-
"""Catchment (sub-basin) polygons with GRASS r.watershed, or with numpy.

GRASS: same tool and settings as Morizon Next's catchment aggregation (MFD,
convergence 5), so the boundaries agree. numpy (terrain/catchment_grid.py):
the same steps without GRASS, for a QGIS without it (the macOS disk image
has none); close to r.watershed, not the same cell for cell. Which one runs
is chosen by CATCHMENT_ENGINE.

The size of the smallest catchment is set as an area and the DEM is
resampled to a fixed cell size first, so the result does not depend on the
DEM source's resolution. The grid steps of the boundaries are then rounded a
little (each shared line once), so the boundaries move by under a cell.
"""

import gc
import os
import re
import shutil
import tempfile
import time

import numpy as np
from osgeo import gdal, ogr, osr

from . import catchment_grid

# Which computation makes the catchments (an internal switch, not in the UI):
#   "auto"  : GRASS r.watershed where QGIS has it, numpy otherwise
#   "numpy" : numpy always (the same result on every QGIS)
# CATCHMENT_ENGINE = "numpy"
CATCHMENT_ENGINE = "auto"

# Catchments are computed on this grid (m) ...
CELL_SIZE_M = 5.0
# ... and the smallest catchment is this large (ha). Morizon Next uses 500
# cells, which is 5 ha on a 10 m DEM.
MIN_AREA_HA = 5.0
# r.watershed settings shared with Morizon Next.
CONVERGENCE = 5
MEMORY_MB = 300
METHOD_TEXT = "GRASS r.watershed, MFD (convergence 5)"
# Boundaries are smoothed once per shared line: the grid steps are evened
# out (each point moved to the mean of the points within ROUND_AVERAGE_CELLS
# cells along the line), then rounded (Chaikin, ROUND_ITERATIONS passes).
# Compared on test DEMs (2026-11-13): Chaikin alone left the 5 m steps; this
# removes them, the lines moving one or two cells (more where a one-cell
# spike shrinks).
ROUND_AVERAGE_CELLS = 2
ROUND_ITERATIONS = 3
ROUND_OFFSET = 0.25
ROUNDING_TEXT = (
    "boundaries smoothed once per shared line (moving average over 2 cells "
    "each side, then Chaikin 3 passes); points where catchments meet are kept"
)


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


def catchment_engine():
    """"grass" or "numpy": what makes the catchments in this QGIS."""
    if CATCHMENT_ENGINE == "numpy":
        return "numpy"
    return "grass" if grass_watershed_algorithm_id() is not None else "numpy"


def method_text(engine):
    return METHOD_TEXT if engine == "grass" else catchment_grid.METHOD_TEXT


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

    Returns (catchments, engine, details): a list of dicts, wkt (clipped to
    the frame), area_ha (whole catchment), complete (False when it reaches
    the edge of the wide DEM); "grass" or "numpy", what made them; and
    {"timing": {"total_s", "flow_s", "cells"}, "grass_error": text or None}:
    seconds for the whole step and for the flow computation in it, the
    cells of the grid used, and why GRASS failed when numpy took over.

    Two branches: GRASS or numpy as CATCHMENT_ENGINE and QGIS choose
    (catchment_engine), and, when GRASS is chosen but does not run (for
    example the provider is listed but GRASS itself is not found), numpy
    again on the same grid. A cancelled job is not run again.
    """
    started = time.perf_counter()
    engine = catchment_engine()
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
        grid = gdal.Open(resampled)
        cells = grid.RasterXSize * grid.RasterYSize
        grid = None
        basin_path = os.path.join(work_dir, "basin.tif")
        grass_error = None
        if engine == "grass":
            flow_started = time.perf_counter()
            try:
                _grass_basins(resampled, basin_path, threshold, work_dir, context, feedback)
                if not os.path.exists(basin_path):
                    raise WatershedError("GRASS r.watershed did not produce catchments.")
            except Exception as error:  # noqa: BLE001 - any failure: numpy instead
                context.check()  # but a cancelled job stays cancelled
                grass_error = str(error) or type(error).__name__
                _log_grass_failure(grass_error)
                context.report("Catchments: GRASS could not run; computing in the plugin...")
                if os.path.exists(basin_path):
                    os.remove(basin_path)
                engine = "numpy"
        if engine == "numpy":
            flow_started = time.perf_counter()
            _numpy_basins(resampled, basin_path, threshold, context)
        flow_seconds = time.perf_counter() - flow_started
        context.check()
        if not os.path.exists(basin_path):
            raise WatershedError("The catchments could not be computed.")

        context.report("Catchments: making polygons...")
        catchments = _basin_polygons(basin_path, crs_wkt, frame_envelope, context)
        details = {
            "timing": {
                "total_s": round(time.perf_counter() - started, 2),
                "flow_s": round(flow_seconds, 2),
                "cells": cells,
            },
            "grass_error": grass_error,
        }
        return catchments, engine, details
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)


def _log_grass_failure(message):
    """Note in the QGIS log why GRASS did not run (the analysis goes on)."""
    try:
        from qgis.core import Qgis, QgsMessageLog

        QgsMessageLog.logMessage(
            "GRASS r.watershed could not run; catchments computed in the plugin "
            "(numpy) instead:\n" + message,
            "Quick Landscape Analysis Explorer",
            Qgis.MessageLevel.Warning,
        )
    except Exception:  # noqa: BLE001 - logging must not stop the analysis
        return


def _grass_basins(dem_path, basin_path, threshold, work_dir, context, feedback):
    """Catchment raster from GRASS r.watershed (as Morizon Next)."""
    import processing
    from processing.tools import dataobjects
    from qgis.core import QgsProcessingUtils

    algorithm_id = grass_watershed_algorithm_id()
    if algorithm_id is None:
        raise WatershedError(
            "GRASS (r.watershed) is not available in this QGIS. "
            "Enable the GRASS processing provider to create catchments."
        )
    processing_context = dataobjects.createContext(feedback)
    # GRASS asks for the temporary folder more than once; give it the
    # ASCII-safe one first, with "/" separators (see Morizon Next).
    processing_context.setTemporaryFolder(work_dir.replace("\\", "/"))
    QgsProcessingUtils.tempFolder(processing_context)
    context.report("Catchments: computing flow (GRASS r.watershed, MFD)...")
    processing.run(algorithm_id, {
        "elevation": dem_path,
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


def _numpy_basins(dem_path, basin_path, threshold, context):
    """Catchment raster from terrain/catchment_grid.py (no GRASS)."""
    dataset = gdal.Open(dem_path)
    band = dataset.GetRasterBand(1)
    data = band.ReadAsArray().astype(np.float64)
    nodata = band.GetNoDataValue()
    if nodata is not None:
        data[data == nodata] = np.nan
    gt, projection = dataset.GetGeoTransform(), dataset.GetProjection()
    band = None
    dataset = None
    labels = catchment_grid.basin_labels(data, threshold, CONVERGENCE, context)
    rows, cols = labels.shape
    out = gdal.GetDriverByName("GTiff").Create(
        basin_path, cols, rows, 1, gdal.GDT_Int32, options=["COMPRESS=LZW", "TILED=YES"]
    )
    out.SetGeoTransform(gt)
    out.SetProjection(projection)
    out_band = out.GetRasterBand(1)
    out_band.SetNoDataValue(0)  # no catchment: left out of the polygons
    out_band.WriteArray(labels)
    out_band = None
    out = None


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

    wholes = {}
    for value, geometries in sorted(parts.items()):
        collection = ogr.Geometry(ogr.wkbMultiPolygon)
        for geometry in geometries:
            collection.AddGeometry(geometry)
        whole = collection.UnionCascaded()
        if whole is None or whole.IsEmpty() or not whole.Intersects(frame):
            continue
        wholes[value] = whole
    complete = {value: bool(inner.Contains(whole)) for value, whole in wholes.items()}
    context.check()

    context.report("Catchments: rounding the boundaries...")
    cell = abs(basin_gt[1])
    shapes = _boundaries(wholes, complete, ROUND_ITERATIONS, cell, frame, context)
    rounded = shapes is not None
    if not rounded:
        shapes = _boundaries(wholes, complete, 0, cell, frame, context)

    catchments = []
    for value, whole in wholes.items():
        shape, edges = shapes[value] if shapes is not None else (whole, [])
        clipped = shape.Intersection(frame)
        if clipped is None or clipped.IsEmpty() or clipped.GetArea() <= 0:
            continue
        shown = ogr.Geometry(ogr.wkbMultiLineString)
        for edge in edges:
            piece = edge.Intersection(frame)
            if piece is None or piece.IsEmpty():
                continue
            for line in _lines(piece):
                shown.AddGeometry(line)
        catchments.append({
            "id": int(value),
            "wkt": clipped.ExportToWkt(),
            # The boundary lines this catchment draws (each line once).
            "edges_wkt": shown.ExportToWkt() if shown.GetGeometryCount() else None,
            # Area and completeness come from the grid, before rounding.
            "area_ha": round(whole.GetArea() / 10000.0, 3),
            # Reaching the edge of the wide DEM means the catchment goes on
            # beyond what was fetched.
            "complete": complete[value],
            "rounded": rounded,
        })
    return catchments


def _lines(geometry):
    """The line strings in an OGR geometry (line, multi-line or collection)."""

    name = geometry.GetGeometryName()
    if name == "LINESTRING":
        return [geometry.Clone()]
    if name in ("MULTILINESTRING", "GEOMETRYCOLLECTION"):
        found = []
        for index in range(geometry.GetGeometryCount()):
            found.extend(_lines(geometry.GetGeometryRef(index)))
        return found
    return []


def _boundaries(wholes, complete, iterations, cell, frame, context):
    """Catchment shapes rebuilt from shared boundary lines, and who draws them.

    The boundary lines between catchments (and between a catchment and no
    data) are taken once each, split where three or more catchments meet,
    smoothed (_averaged_line, then Chaikin, ``iterations`` passes; 0 keeps
    the grid steps) and the catchments are rebuilt from them, so neighbours
    keep meeting without gaps or overlaps and the meeting points stay where
    they are.

    Each line is drawn by one catchment only, so dashed lines on both sides
    of a boundary do not fall on top of each other: by the whole catchment
    (``complete``) when one side is whole, else by the lower number.

    Returns {value: (ogr shape, [ogr lines it draws])}, or None when the
    lines do not give the same catchments back inside the frame (the
    ogr ``frame``; outside it nothing is shown).
    """
    from qgis.core import QgsGeometry

    if not wholes:
        return None
    try:
        shapes = {
            value: QgsGeometry.fromWkt(whole.ExportToWkt())
            for value, whole in wholes.items()
        }
        boundaries = [QgsGeometry(shape.constGet().boundary()) for shape in shapes.values()]
        # Shared edges once, split where catchments meet, joined in between.
        network = QgsGeometry.unaryUnion(boundaries).mergeLines()
        context.check()
        lines = []
        for line in network.asGeometryCollection():
            if iterations:
                line = _averaged_line(line, cell, ROUND_AVERAGE_CELLS)
                line = line.smooth(iterations, ROUND_OFFSET)
            lines.append(line)
        context.check()
        faces = QgsGeometry.polygonize([QgsGeometry.unaryUnion(lines)])
        context.check()

        # Give each face to the catchment it covers most.
        pieces = {}
        for face in faces.asGeometryCollection():
            if face.area() <= 0:
                continue
            best, best_area = None, 0.0
            box = face.boundingBox()
            for value, shape in shapes.items():
                if not box.intersects(shape.boundingBox()):
                    continue
                area = face.intersection(shape).area()
                if area > best_area:
                    best, best_area = value, area
            if best is not None and best_area > face.area() * 0.5:
                pieces.setdefault(best, []).append(face)

        window = QgsGeometry.fromWkt(frame.ExportToWkt())
        rebuilt = {}
        for value, shape in shapes.items():
            if value not in pieces:
                return None
            joined = QgsGeometry.unaryUnion(pieces[value])
            # Rounding moves the lines by about a cell at most: anything
            # more means the catchments were not rebuilt as expected.
            before = shape.intersection(window).area()
            after = joined.intersection(window).area()
            if abs(after - before) > max(before * 0.02, 4 * cell * cell):
                return None
            rebuilt[value] = joined
        context.check()

        # Who draws each line: the catchments on its sides.
        edges = {value: [] for value in shapes}
        for line in lines:
            middle = line.interpolate(line.length() / 2.0)
            box = line.boundingBox()
            sides = [
                value for value, joined in rebuilt.items()
                if box.intersects(joined.boundingBox())
                and joined.distance(middle) < 1e-6
            ]
            if not sides:
                continue
            owner = min(sides, key=lambda value: (not complete[value], value))
            edges[owner].append(ogr.CreateGeometryFromWkt(line.asWkt()))
        return {
            value: (ogr.CreateGeometryFromWkt(joined.asWkt()), edges[value])
            for value, joined in rebuilt.items()
        }
    except Exception:  # noqa: BLE001 - any trouble: try again without rounding
        context.check()  # but a cancelled job stays cancelled
        return None


def _averaged_line(line, cell, reach):
    """The line with its grid steps evened out, ends kept.

    The line gets a point every cell; each point is moved to the mean of the
    points within ``reach`` cells along the line (fewer near the ends). Steps
    of a few cells become smooth slants and curves; the ends (where
    catchments meet) stay where they are.
    """
    from qgis.core import QgsGeometry, QgsPointXY

    dense = line.densifyByDistance(cell)
    parts = dense.asMultiPolyline() if dense.isMultipart() else [dense.asPolyline()]
    out = []
    for points in parts:
        last = len(points) - 1
        if last < 2:
            out.append(points)
            continue
        smoothed = [points[0]]
        for index in range(1, last):
            near = min(reach, index, last - index)
            window = points[index - near:index + near + 1]
            smoothed.append(QgsPointXY(
                sum(p.x() for p in window) / len(window),
                sum(p.y() for p in window) / len(window),
            ))
        smoothed.append(points[-1])
        out.append(smoothed)
    if len(out) == 1:
        return QgsGeometry.fromPolylineXY(out[0])
    return QgsGeometry.fromMultiPolylineXY(out)


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
                       ("complete", ogr.OFTInteger), ("edges_wkt", ogr.OFTString)):
        layer.CreateField(ogr.FieldDefn(name, kind))
    for item in catchments:
        feature = ogr.Feature(layer.GetLayerDefn())
        feature.SetField("basin_id", item["id"])
        feature.SetField("area_ha", item["area_ha"])
        feature.SetField("complete", 1 if item["complete"] else 0)
        if item.get("edges_wkt"):
            feature.SetField("edges_wkt", item["edges_wkt"])
        geometry = ogr.ForceToMultiPolygon(ogr.CreateGeometryFromWkt(item["wkt"]))
        feature.SetGeometry(geometry)
        layer.CreateFeature(feature)
        feature = None
    layer = None
    datasource = None
    return path
