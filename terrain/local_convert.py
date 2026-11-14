# -*- coding: utf-8 -*-
"""DEMs without a position, converted with a coordinate system the user picks.

Two kinds of file come in here:

- CAD drawings (DXF, and DWG where GDAL can read it): lines, points and
  faces whose vertices carry heights. They are put on a grid by linear
  interpolation in triangles (GDAL's grid "linear" algorithm).
- Rasters without georeferencing or without a coordinate reference system.

The file's coordinates are taken as they are, as values of the coordinate
system chosen by the user (the project's by default): drawings of Japanese
surveys are usually in plane rectangular coordinates, which then give the
place, orientation and size at once. Nothing is placed by hand. The result
is written as an ordinary GeoTIFF (``write_converted``) and compared with a
network DEM (``compare_with_reference``), which tells whether the choice
fits: the agreement, the height ratio (unit) and the height datum difference.

Nothing here needs the GUI.
"""

import json
import math
import os
import tempfile

import numpy as np
from osgeo import gdal, ogr

CAD_EXTENSIONS = (".dxf", ".dwg")

# Folder (beside analyses/ and exports/) where a converted DEM waits for the
# analysis that takes it into its ZIP, and the end of its file name.
EDIT_FOLDER = "qlae_edit_dem"
EDIT_SUFFIX = "_qlae_edit"
# Units a file's coordinates and heights can be read in: metres per unit.
UNITS_M = {"m": 1.0, "cm": 0.01, "mm": 0.001}
# GeoTIFF metadata item that records how the DEM was converted and compared.
METADATA_KEY = "QLAE_CONVERSION"

# The CAD grid's cell comes from the data: this share of the typical
# distance between neighbouring lines and points (half: the grid then shows
# what lies between two contour lines). Bounded so the long side has at
# least CAD_MIN_CELLS and at most CAD_MAX_CELLS cells.
CAD_CELL_SHARE = 0.5
CAD_MIN_CELLS = 200
CAD_MAX_CELLS = 4000
# Vertices measured for that distance: a window around the centre holding
# about this many (a window keeps the density, thinning would not).
CAD_SPACING_SAMPLE = 50_000
# More points than this are thinned (densified lines and vertices together).
CAD_MAX_POINTS = 3_000_000
# A raster larger than this is not taken (the DEM is read whole into memory).
RASTER_MAX_CELLS = 60_000_000


class LocalSourceError(Exception):
    """A file that cannot become a local DEM; the message is for the user."""


def is_cad(path):
    return os.path.splitext(path or "")[1].lower() in CAD_EXTENSIONS


def raster_without_position(path):
    """True for a raster that could be a DEM but has no place on the earth.

    Only the missing position is checked here; the other checks (one band,
    not 8-bit, values present) are the dialog's usual ones.
    """

    try:
        dataset = gdal.Open(path)
    except RuntimeError:
        return False
    if dataset is None or dataset.RasterCount != 1:
        return False
    gt = dataset.GetGeoTransform(can_return_null=True)
    return gt is None or not dataset.GetProjection()


# --------------------------------------------------------------------------
# Local grid


class LocalGrid:
    """Heights (float64, NaN = no value) on a grid in the file's coordinates.

    ``gt`` is a GDAL geotransform in the file's own units, y up (north-up
    in the drawing's sense). ``info`` describes the source for the record.
    """

    def __init__(self, data, gt, info):
        self.data = data
        self.gt = tuple(float(value) for value in gt)
        self.info = dict(info)
        self.valid = np.isfinite(data)
        if not self.valid.any():
            raise LocalSourceError("no_values")

    @property
    def cell_area(self):
        gt = self.gt
        return abs(gt[1] * gt[5] - gt[2] * gt[4])

    @property
    def cell(self):
        return math.sqrt(self.cell_area)


def open_raster(path):
    """A raster without a position as a LocalGrid (file or pixel coordinates)."""

    try:
        dataset = gdal.Open(path)
    except RuntimeError:
        dataset = None
    if dataset is None:
        raise LocalSourceError("cannot_open")
    cols, rows = dataset.RasterXSize, dataset.RasterYSize
    if cols * rows > RASTER_MAX_CELLS:
        raise LocalSourceError("too_large")
    band = dataset.GetRasterBand(1)
    if band.DataType in (gdal.GDT_Byte, getattr(gdal, "GDT_Int8", -1)):
        # 256 levels: shaded relief, classes or a display image.
        raise LocalSourceError("eight_bit")
    data = band.ReadAsArray()
    if data is None:
        raise LocalSourceError("cannot_read")
    data = data.astype(np.float64)
    nodata = band.GetNoDataValue()
    if nodata is not None:
        data[data == nodata] = np.nan
    if band.GetMaskFlags() != gdal.GMF_ALL_VALID:
        data[band.GetMaskBand().ReadAsArray() == 0] = np.nan
    data[~np.isfinite(data) | (np.abs(data) >= 1e30)] = np.nan
    scale = band.GetScale()
    offset = band.GetOffset()
    data = data * (1.0 if scale is None else scale) + (0.0 if offset is None else offset)
    gt = dataset.GetGeoTransform(can_return_null=True)
    has_gt = gt is not None
    if gt is None:
        # Pixel coordinates, y up: column to the right, rows downwards.
        gt = (0.0, 1.0, 0.0, 0.0, 0.0, -1.0)
    dataset = None
    return LocalGrid(data, gt, {
        "kind": "raster",
        "file_name": os.path.basename(path),
        "coordinates": "file" if has_gt else "pixels",
    })


# --------------------------------------------------------------------------
# CAD drawings


class CadDrawing:
    """Vertices with heights from a CAD file, by CAD layer.

    ``parts[layer]`` is a list of (n, 3) arrays: one per line, ring or
    point group, in drawing order. Parts whose heights are all exactly 0 are
    counted apart: a flat (2D) drawing has them, and they would pull the
    terrain down to 0.
    """

    def __init__(self, path, progress=None, check=None):
        """``progress(fraction)`` is told how far the reading is; ``check()``
        is called now and then and may raise to stop."""

        self.path = path
        self.parts = {}
        self.flat_parts = {}
        self._read(progress, check)

    def layers(self):
        """[(CAD layer, parts with heights, parts without)], heights first."""

        names = set(self.parts) | set(self.flat_parts)
        found = [
            (name, len(self.parts.get(name, ())), len(self.flat_parts.get(name, ())))
            for name in names
        ]
        found.sort(key=lambda item: (item[1] == 0, item[0].lower()))
        return found

    def _read(self, progress, check):
        with _quiet_gdal():
            try:
                source = ogr.Open(self.path)
            except RuntimeError:
                source = None
        if source is None:
            raise LocalSourceError("cannot_open_cad")
        layers = [source.GetLayer(index) for index in range(source.GetLayerCount())]
        total = max(1, sum(max(0, layer.GetFeatureCount()) for layer in layers))
        done = 0
        for layer in layers:
            definition = layer.GetLayerDefn()
            layer_field = definition.GetFieldIndex("Layer")
            layer.ResetReading()
            for feature in layer:
                done += 1
                if done % 500 == 0:
                    if check is not None:
                        check()
                    if progress is not None:
                        progress(min(1.0, done / total))
                geometry = feature.GetGeometryRef()
                if geometry is None:
                    continue
                name = (
                    feature.GetField(layer_field) if layer_field >= 0 else None
                ) or layer.GetName() or "0"
                for part in _geometry_parts(geometry):
                    target = self.parts if np.any(part[:, 2] != 0.0) else self.flat_parts
                    target.setdefault(str(name), []).append(part)
        source = None
        if not self.parts and not self.flat_parts:
            raise LocalSourceError("cad_empty")


def _geometry_parts(geometry):
    """(n, 3) vertex arrays of a geometry: lines, rings and points."""

    if geometry.HasCurveGeometry():
        geometry = geometry.GetLinearGeometry()
    count = geometry.GetGeometryCount()
    if count:
        parts = []
        loose = []
        for index in range(count):
            child = geometry.GetGeometryRef(index)
            if child.GetGeometryCount() == 0 and child.GetPointCount() == 1:
                loose.extend(_vertices(child))
            else:
                parts.extend(_geometry_parts(child))
        if loose:
            parts.append(np.array(loose, dtype=np.float64))
        return parts
    vertices = _vertices(geometry)
    return [np.array(vertices, dtype=np.float64)] if vertices else []


def _vertices(geometry):
    points = []
    for index in range(geometry.GetPointCount()):
        x, y, z = geometry.GetPoint(index)
        if math.isfinite(x) and math.isfinite(y) and math.isfinite(z):
            points.append((x, y, z))
    return points


def grid_cad(drawing, layers=None, check=None, unit="m", swap=False, progress=None):
    """Interpolate the chosen CAD layers' heights onto a local grid.

    Lines are densified to the cell size first, so contour lines give the
    triangles their shape, not only their vertices. ``unit``: the unit the
    drawing's coordinates and heights are in (UNITS_M; the grid is in
    metres). ``swap``: X and Y read the other way round (survey X north,
    Y east drawn as x, y). ``check`` is called now and then and may raise
    to stop. ``progress(fraction)`` follows the gridding (GDAL's own steps).
    """

    # Sorted, so the same layers give the same DEM whatever order they were
    # chosen in (where two layers share a spot, the first one's height stays).
    chosen = sorted(layers if layers is not None else drawing.parts)
    parts = [
        _read_as(part, unit, swap) for name in chosen for part in drawing.parts.get(name, ())
    ]
    if not parts:
        raise LocalSourceError("cad_no_heights")
    stacked = np.concatenate(parts)
    xmin, ymin = stacked[:, 0].min(), stacked[:, 1].min()
    xmax, ymax = stacked[:, 0].max(), stacked[:, 1].max()
    long_side = max(xmax - xmin, ymax - ymin)
    if not long_side > 0:
        raise LocalSourceError("cad_no_extent")
    spacing = _feature_spacing(parts, check)
    # Rounded down, so the grid is never coarser than the data; then kept
    # within the bounds of cells along the long side.
    cell = _nice_floor((spacing or long_side / 1000) * CAD_CELL_SHARE)
    cell = min(cell, _nice_floor(long_side / CAD_MIN_CELLS))
    if long_side / cell > CAD_MAX_CELLS:
        cell = _nice_step(long_side / CAD_MAX_CELLS)
    while True:
        points = _densified(parts, cell, check)
        if len(points) <= CAD_MAX_POINTS:
            break
        cell = _nice_step(cell * 1.5)
    if check is not None:
        check()
    # One height per spot (Delaunay needs distinct points).
    keys = np.round(points[:, :2] / (cell * 0.01)).astype(np.int64)
    _unique, first = np.unique(keys, axis=0, return_index=True)
    points = points[np.sort(first)]
    if len(points) < 3:
        raise LocalSourceError("cad_no_heights")

    x0 = math.floor(xmin / cell) * cell
    y1 = math.ceil(ymax / cell) * cell
    cols = int(math.ceil((xmax - x0) / cell)) + 1
    rows = int(math.ceil((y1 - ymin) / cell)) + 1
    data = _linear_grid(points, x0, y1, cell, cols, rows, progress, check)
    if check is not None:
        check()
    return LocalGrid(data, (x0, cell, 0.0, y1, 0.0, -cell), {
        "kind": "cad",
        "file_name": os.path.basename(drawing.path),
        "cad_layers": sorted(chosen),
        "points": int(len(points)),
        "unit": unit,
        "swapped_xy": bool(swap),
        "feature_spacing": spacing,
        "method": "GDAL grid, linear interpolation in Delaunay triangles "
        "(lines densified to the cell size); cell = half the median distance "
        "between neighbouring lines and points, rounded down to 1, 2, 5 x 10^n",
    })


def _read_as(part, unit, swap):
    """Vertices (n, 3) in metres, X and Y swapped when asked."""

    factor = UNITS_M[unit]
    read = part * factor if factor != 1.0 else part.copy()
    if swap:
        read[:, [0, 1]] = read[:, [1, 0]]
    return read


def read_grid_as(grid, unit="m", swap=False):
    """A raster's LocalGrid read in another unit and/or with X and Y swapped."""

    factor = UNITS_M[unit]
    gt = tuple(value * factor for value in grid.gt)
    if swap:
        # x = gt0 + col*gt1 + row*gt2 and y = gt3 + col*gt4 + row*gt5 change places.
        gt = (gt[3], gt[4], gt[5], gt[0], gt[1], gt[2])
    return LocalGrid(grid.data * factor, gt, dict(grid.info, unit=unit, swapped_xy=bool(swap)))


def _feature_spacing(parts, check=None):
    """Median distance between neighbouring lines and points, or None.

    The vertices are joined into Delaunay triangles; only the edges that join
    two different lines or points count, so lines drawn with close vertices
    do not make the distance small. Measured in a window around the centre
    holding about CAD_SPACING_SAMPLE vertices.
    """

    ids = np.concatenate([np.full(len(part), index) for index, part in enumerate(parts)])
    xy = np.concatenate(parts)[:, :2]
    if len(xy) > CAD_SPACING_SAMPLE:
        xmin, ymin = xy.min(axis=0)
        xmax, ymax = xy.max(axis=0)
        share = math.sqrt(CAD_SPACING_SAMPLE / float(len(xy)))
        cx, cy = (xmin + xmax) / 2.0, (ymin + ymax) / 2.0
        hx, hy = (xmax - xmin) * share / 2.0, (ymax - ymin) * share / 2.0
        inside = (np.abs(xy[:, 0] - cx) <= hx) & (np.abs(xy[:, 1] - cy) <= hy)
        xy, ids = xy[inside], ids[inside]
    if check is not None:
        check()
    # One id per spot (a vertex shared by two lines keeps the first).
    keys = np.round(xy, 6)
    _unique, first = np.unique(keys, axis=0, return_index=True)
    xy, ids = xy[first], ids[first]
    if len(xy) < 3:
        return None
    owner = {(round(x, 6), round(y, 6)): part for (x, y), part in zip(xy.tolist(), ids.tolist())}
    points = ogr.Geometry(ogr.wkbMultiPoint)
    for x, y in xy.tolist():
        point = ogr.Geometry(ogr.wkbPoint)
        point.AddPoint_2D(x, y)
        points.AddGeometry(point)
    with _quiet_gdal():
        try:
            edges = points.DelaunayTriangulation(0.0, True)
        except RuntimeError:
            return None
    if edges is None:
        return None
    lengths = []
    for index in range(edges.GetGeometryCount()):
        edge = edges.GetGeometryRef(index)
        (x0, y0), (x1, y1) = edge.GetPoint_2D(0), edge.GetPoint_2D(1)
        a = owner.get((round(x0, 6), round(y0, 6)))
        b = owner.get((round(x1, 6), round(y1, 6)))
        if a is not None and b is not None and a != b:
            lengths.append(math.hypot(x1 - x0, y1 - y0))
    if check is not None:
        check()
    return float(np.median(lengths)) if lengths else None


def _densified(parts, step, check):
    pieces = []
    for index, part in enumerate(parts):
        if check is not None and index % 2000 == 0:
            check()
        if len(part) < 2:
            pieces.append(part)
            continue
        segment = np.diff(part, axis=0)
        lengths = np.hypot(segment[:, 0], segment[:, 1])
        counts = np.maximum(1, np.ceil(lengths / step).astype(np.int64))
        if counts.max() == 1:
            pieces.append(part)
            continue
        fractions = np.concatenate([np.arange(n) / n for n in counts.tolist()])
        starts = np.repeat(part[:-1], counts, axis=0)
        deltas = np.repeat(segment, counts, axis=0)
        pieces.append(starts + deltas * fractions[:, None])
        pieces.append(part[-1:])
    return np.concatenate(pieces)


def _linear_grid(points, x0, y1, cell, cols, rows, progress=None, check=None):
    """GDAL grid (linear) through a CSV file; NaN outside the triangles.

    The points are given to GDAL in grid units (origin at the grid's top
    left corner, one cell = 1): the triangulation then sees the same numbers
    whatever the file's unit and coordinates. With coordinates of about 100
    and points 0.001 apart (a drawing in metres read as mm) it did not
    finish.
    """

    folder = tempfile.mkdtemp(prefix="qlae_cad_")
    csv_path = os.path.join(folder, "points.csv")
    scaled = np.column_stack(
        ((points[:, 0] - x0) / cell, (points[:, 1] - y1) / cell, points[:, 2])
    )
    try:
        np.savetxt(
            csv_path, scaled, delimiter=",", header="x,y,z", comments="",
            fmt=("%.6f", "%.6f", "%.9g"),
        )
        with _quiet_gdal():
            source = gdal.OpenEx(
                csv_path,
                gdal.OF_VECTOR,
                open_options=[
                    "X_POSSIBLE_NAMES=x",
                    "Y_POSSIBLE_NAMES=y",
                    "Z_POSSIBLE_NAMES=z",
                    "AUTODETECT_TYPE=YES",
                ],
            )
            if source is None:
                raise LocalSourceError("cad_grid_failed")
            stop = {"now": False}

            def follow(complete, message, data):
                del message, data
                if check is not None:
                    try:
                        check()
                    except Exception:  # noqa: BLE001 - stop GDAL, raise below
                        stop["now"] = True
                        return 0
                if progress is not None:
                    progress(complete)
                return 1

            # Cell centres: the bounds are the outer edges of the grid.
            try:
                result = gdal.Grid(
                    "",
                    source,
                    format="MEM",
                    algorithm="linear:radius=-1:nodata=-1e38",
                    zfield="z",
                    outputBounds=[0.0, 0.0, float(cols), float(-rows)],
                    width=cols,
                    height=rows,
                    outputType=gdal.GDT_Float64,
                    callback=follow,
                )
            except RuntimeError:
                if stop["now"]:
                    check()
                raise
            source = None
        if result is None:
            raise LocalSourceError("cad_grid_failed")
        data = result.GetRasterBand(1).ReadAsArray().astype(np.float64)
        result = None
    finally:
        for name in os.listdir(folder):
            os.remove(os.path.join(folder, name))
        os.rmdir(folder)
    data[~np.isfinite(data) | (data <= -1e37)] = np.nan
    return data


def _nice_floor(value):
    """The last of 1, 2, 5 x 10^n at or below value."""

    if not value > 0:
        return 1.0
    power = 10.0 ** math.floor(math.log10(value))
    for factor in (5.0, 2.0, 1.0):
        if factor * power <= value * (1 + 1e-9):
            return factor * power
    return power


def _nice_step(value):
    """The next of 1, 2, 5 x 10^n at or above value."""

    if not value > 0:
        return 1.0
    power = 10.0 ** math.floor(math.log10(value))
    for factor in (1.0, 2.0, 5.0, 10.0):
        if value <= factor * power * (1 + 1e-9):
            return factor * power
    return 10.0 * power


class _quiet_gdal:
    """Use GDAL exceptions inside the block and leave the global mode as it was."""

    def __enter__(self):
        self._previous = gdal.GetUseExceptions()
        gdal.UseExceptions()
        return self

    def __exit__(self, *exc):
        if not self._previous:
            gdal.DontUseExceptions()
        return False


# --------------------------------------------------------------------------
# Writing and comparing


def write_converted(grid, crs_wkt, path, record):
    """Write the grid with the chosen CRS as a GeoTIFF with its record."""

    os.makedirs(os.path.dirname(path), exist_ok=True)
    if os.path.exists(path):
        # The same file converted again before its analysis.
        os.remove(path)
    rows, cols = grid.data.shape
    nodata = -9999.0
    options = ["COMPRESS=LZW", "TILED=YES", "BIGTIFF=IF_NEEDED"]
    gt = grid.gt
    north_up = gt[1] > 0 and gt[5] < 0 and gt[2] == 0 and gt[4] == 0
    with _quiet_gdal():
        driver = gdal.GetDriverByName("GTiff" if north_up else "MEM")
        dataset = (
            driver.Create(path, cols, rows, 1, gdal.GDT_Float32, options)
            if north_up
            else driver.Create("", cols, rows, 1, gdal.GDT_Float32)
        )
        if dataset is None:
            raise LocalSourceError("write_failed")
        dataset.SetGeoTransform(gt)
        dataset.SetProjection(crs_wkt)
        band = dataset.GetRasterBand(1)
        band.SetNoDataValue(nodata)
        band.WriteArray(np.where(np.isfinite(grid.data), grid.data, nodata).astype(np.float32))
        if not north_up:
            # Swapped (or mirrored) axes: written north-up, as the analysis
            # and other programs expect.
            cell = math.sqrt(abs(gt[1] * gt[5] - gt[2] * gt[4]))
            warped = gdal.Warp(
                path, dataset, format="GTiff", xRes=cell, yRes=cell,
                srcNodata=nodata, dstNodata=nodata, resampleAlg="near",
                creationOptions=options,
            )
            dataset = None
            if warped is None:
                raise LocalSourceError("write_failed")
            dataset = warped
        dataset.SetMetadataItem(METADATA_KEY, json.dumps(record, ensure_ascii=False))
        dataset.FlushCache()
        dataset = None
    return path


def update_record(path, record):
    """Replace the record of a converted DEM (after the comparison)."""

    with _quiet_gdal():
        dataset = gdal.Open(path, gdal.GA_Update)
        dataset.SetMetadataItem(METADATA_KEY, json.dumps(record, ensure_ascii=False))
        dataset.FlushCache()
        dataset = None


def read_record(path):
    """The placement record of an converted DEM, or None."""

    try:
        with _quiet_gdal():
            dataset = gdal.Open(path)
            text = dataset.GetMetadataItem(METADATA_KEY) if dataset is not None else None
            dataset = None
    except RuntimeError:
        return None
    if not text:
        return None
    try:
        return json.loads(text)
    except ValueError:
        return None


def edit_path(project_directory, root_folder, original_path):
    """Where the converted DEM of ``original_path`` waits for its analysis."""

    stem = os.path.splitext(os.path.basename(original_path))[0]
    return os.path.join(
        project_directory, root_folder, EDIT_FOLDER, stem + EDIT_SUFFIX + ".tif"
    )


def is_edit_file(path):
    return os.path.basename(os.path.dirname(os.path.abspath(path or ""))) == EDIT_FOLDER


# --------------------------------------------------------------------------
# Heights against a network DEM


def grid_extent(grid):
    """(xmin, ymin, xmax, ymax) of the grid in its coordinates."""

    rows, cols = grid.data.shape
    gt = grid.gt
    xs = [gt[0] + col * gt[1] + row * gt[2] for col in (0, cols) for row in (0, rows)]
    ys = [gt[3] + col * gt[4] + row * gt[5] for col in (0, cols) for row in (0, rows)]
    return (min(xs), min(ys), max(xs), max(ys))


def compare_with_reference(path, reference, check=None):
    """Fit converted = a * reference + b where both have values.

    ``reference``: (data, geotransform, CRS WKT) of a network DEM. The
    converted DEM is resampled onto the reference grid (``check()`` may
    stop it). Returns {"a", "b",
    "r", "n"}, or None with too few cells. r (agreement) says whether the
    DEM lies where the network DEM has the same land; a near 1 says the
    height unit is metres; b is the height datum difference (converted minus
    network, metres).
    """

    ref_data, ref_gt, ref_wkt = reference
    rows, cols = ref_data.shape
    target = gdal.GetDriverByName("MEM").Create("", cols, rows, 1, gdal.GDT_Float64)
    target.SetGeoTransform(ref_gt)
    target.SetProjection(ref_wkt)
    band = target.GetRasterBand(1)
    band.SetNoDataValue(float("nan"))
    band.Fill(float("nan"))
    stop = {"now": False}

    def follow(complete, message, data):
        del complete, message, data
        if check is not None:
            try:
                check()
            except Exception:  # noqa: BLE001 - stop GDAL, raise below
                stop["now"] = True
                return 0
        return 1

    with _quiet_gdal():
        source = gdal.Open(path)
        try:
            gdal.Warp(
                target,
                source,
                resampleAlg="bilinear",
                dstNodata=float("nan"),
                errorThreshold=0,
                warpOptions=["INIT_DEST=NO_DATA"],
                callback=follow,
            )
        except RuntimeError:
            if stop["now"]:
                check()
            raise
        source = None
    converted = band.ReadAsArray().astype(np.float64)
    target = None
    both = np.isfinite(converted) & np.isfinite(ref_data)
    count = int(np.count_nonzero(both))
    if count < 30:
        return None
    x = ref_data[both]
    y = converted[both]
    if np.std(x) <= 0 or np.std(y) <= 0:
        return None
    a, b = np.polyfit(x, y, 1)
    r = float(np.corrcoef(x, y)[0, 1])
    return {"a": float(a), "b": float(b), "r": r, "n": count}
