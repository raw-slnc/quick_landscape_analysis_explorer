# -*- coding: utf-8 -*-
"""DEM acquisition, terrain derivatives, and portable ZIP packaging."""

import datetime
import json
import math
import os
import tempfile
import traceback
import uuid
import zipfile

import numpy as np
from osgeo import gdal, ogr, osr
from qgis.PyQt.QtCore import QThread, pyqtSignal
from qgis.core import Qgis, QgsMessageLog

from ..core.packages import MANIFEST_NAME
from ..terrain.analysis import compute_hillshade, compute_slope_deg
from ..terrain.cs_map import compute_cs_map
from ..terrain.dem_loader import DEMLoader, GSITileDEMLoader, save_as_geotiff
from ..terrain.result_writer import save_raster, save_rgba_raster
from ..terrain.styles import write_morizon_slope_qml


class TerrainJobCancelled(Exception):
    pass


class TerrainJobError(Exception):
    pass


class TerrainJobWorker(QThread):
    message = pyqtSignal(str)
    done = pyqtSignal(object, bool, str)

    def __init__(self, specification, parent=None):
        super().__init__(parent)
        self.specification = dict(specification)
        self._cancelled = False

    def cancel(self):
        self._cancelled = True

    def cancelled(self):
        return self._cancelled

    def check(self):
        if self._cancelled:
            raise TerrainJobCancelled()

    def report(self, text):
        self.message.emit(text)

    def run(self):
        try:
            result = create_terrain_package(self.specification, self)
            self.check()
        except TerrainJobCancelled:
            self.done.emit(None, True, "")
            return
        except TerrainJobError as error:
            self.done.emit(None, False, str(error))
            return
        except Exception as error:  # keep worker exceptions out of the Qt event loop
            QgsMessageLog.logMessage(
                "Terrain analysis failed:\n" + traceback.format_exc(),
                "Quick Landscape Analysis Explorer",
                Qgis.MessageLevel.Warning,
            )
            self.done.emit(None, False, str(error))
            return
        self.done.emit(result, False, "")


def create_terrain_package(specification, context):
    package_path = specification["package_path"]
    if os.path.exists(package_path) and not specification.get("overwrite"):
        raise TerrainJobError("package_exists")

    with tempfile.TemporaryDirectory(prefix="qlae_") as temporary:
        data_dir = os.path.join(temporary, "data")
        output_dir = os.path.join(temporary, "outputs")
        style_dir = os.path.join(temporary, "styles")
        area_dir = os.path.join(temporary, "area")
        for directory in (data_dir, output_dir, style_dir, area_dir):
            os.makedirs(directory, exist_ok=True)

        context.report("Preparing DEM...")
        context.check()
        aoi = _geometry_from_specification(specification)
        landscape = bool(specification.get("preview_landscape", False))
        acquisition_frame = _a_series_frame(aoi, landscape)
        if specification["source_kind"] == "gsi":
            loader, source = _fetch_gsi_loader(
                specification, acquisition_frame, temporary, context
            )
        else:
            loader, source = _open_local_loader(specification, acquisition_frame)

        context.check()
        prepared = loader.prepare_for_analysis()
        analysis_aoi = _transform_geometry(
            aoi,
            specification["aoi_crs_wkt"],
            prepared.crs_wkt,
        )
        analysis_frame = _a_series_frame(analysis_aoi, landscape)
        window = _a_series_pixel_window(
            prepared, analysis_frame.GetEnvelope(), landscape
        )
        mask, final_gt = _area_mask(prepared, analysis_aoi, window)
        rows, cols = mask.shape
        final_dem = _extract_window(prepared.data, window, np.nan)
        final_dem[~mask] = np.nan
        valid_inside = mask & np.isfinite(final_dem)
        if not np.any(valid_inside):
            raise TerrainJobError("DEM does not contain valid elevations in the analysis area.")

        requested = set(specification.get("outputs") or ())
        hillshade = None
        slope = None
        cs_map = None
        if "hillshade" in requested:
            context.report("Creating hillshade...")
            hillshade_full = compute_hillshade(prepared.data, prepared.cell_size)
            hillshade = _extract_window(hillshade_full, window, np.nan)
            hillshade[~mask] = np.nan
            context.check()

        if "slope" in requested:
            context.report("Creating slope...")
            slope_full = compute_slope_deg(prepared.data, prepared.cell_size)
            slope = _extract_window(slope_full, window, np.nan)
            slope[~mask] = np.nan
            context.check()

        if "cs_map" in requested:
            context.report("Creating CS Map...")
            cs_full = compute_cs_map(prepared.data, prepared.cell_size)
            cs_map = _extract_window(cs_full, window, 0)
            if cs_map.shape[2] == 4:
                cs_map[:, :, 3][~valid_inside] = 0
            else:
                cs_map[~valid_inside] = 0
            context.check()

        context.report("Writing analysis package...")
        dem_path = save_raster(
            final_dem, final_gt, prepared.crs_wkt, data_dir, "dem", overwrite=True
        )
        output_members = {"dem": "data/dem.tif"}
        if "hillshade" in requested:
            save_raster(
                hillshade,
                final_gt,
                prepared.crs_wkt,
                output_dir,
                "hillshade",
                overwrite=True,
            )
            output_members["hillshade"] = "outputs/hillshade.tif"
        if "slope" in requested:
            save_raster(
                slope,
                final_gt,
                prepared.crs_wkt,
                output_dir,
                "slope_deg",
                overwrite=True,
            )
            write_morizon_slope_qml(os.path.join(style_dir, "slope.qml"))
            output_members["slope"] = "outputs/slope_deg.tif"
            output_members["slope_style"] = "styles/slope.qml"
        if "cs_map" in requested:
            save_rgba_raster(
                cs_map,
                final_gt,
                prepared.crs_wkt,
                output_dir,
                "cs_map",
                overwrite=True,
            )
            output_members["cs_map"] = "outputs/cs_map.tif"

        with open(os.path.join(area_dir, "aoi.wkt"), "w", encoding="utf-8") as stream:
            stream.write(specification["aoi_wkt"])
        frame_envelope = _window_envelope(prepared, window)
        final_frame = _rectangle_geometry(frame_envelope)
        with open(os.path.join(area_dir, "frame.wkt"), "w", encoding="utf-8") as stream:
            stream.write(final_frame.ExportToWkt())

        inside_count = int(np.count_nonzero(mask))
        valid_count = int(np.count_nonzero(valid_inside))
        manifest = {
            "format": "qlae-terrain-package",
            "format_version": 1,
            "analysis_name": specification["analysis_name"],
            "created_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            "plugin_version": specification.get("plugin_version", "development"),
            "profile": "japan",
            "area": {
                "mode": specification["area_mode"],
                "crs_wkt": specification["aoi_crs_wkt"],
                "geometry": "area/aoi.wkt",
            },
            "output_frame": {
                "standard": "ISO 216 A-series aspect ratio",
                "orientation": "landscape" if landscape else "portrait",
                "target_width_height_ratio": (
                    math.sqrt(2.0) if landscape else 1.0 / math.sqrt(2.0)
                ),
                "actual_width_height_ratio": cols / rows,
                "crs_wkt": prepared.crs_wkt,
                "geometry": "area/frame.wkt",
            },
            "source": source,
            "analysis_grid": {
                "crs_wkt": prepared.crs_wkt,
                "cell_size_m": float(prepared.cell_size),
                "columns": cols,
                "rows": rows,
                "valid_coverage_percent": round(100.0 * valid_count / inside_count, 3),
            },
            "outputs": output_members,
            "styles": {
                "slope": "Morizon Next 災害リスク/傾斜 (non-scoring)"
                if "slope" in requested
                else None
            },
        }
        with open(os.path.join(temporary, MANIFEST_NAME), "w", encoding="utf-8") as stream:
            json.dump(manifest, stream, ensure_ascii=False, indent=2)

        context.check()
        os.makedirs(os.path.dirname(package_path), exist_ok=True)
        temporary_zip = package_path + ".{}.tmp".format(uuid.uuid4().hex)
        try:
            with zipfile.ZipFile(
                temporary_zip, "w", compression=zipfile.ZIP_STORED, allowZip64=True
            ) as archive:
                for root, directories, files in os.walk(temporary):
                    directories[:] = sorted(
                        directory for directory in directories
                        if not directory.startswith("_")
                    )
                    for file_name in sorted(files):
                        path = os.path.join(root, file_name)
                        if path == temporary_zip:
                            continue
                        member = os.path.relpath(path, temporary).replace(os.sep, "/")
                        archive.write(path, member)
            context.check()
            os.replace(temporary_zip, package_path)
        finally:
            if os.path.exists(temporary_zip):
                os.remove(temporary_zip)

    return {"package_path": package_path, "manifest": manifest}


def _geometry_from_specification(specification):
    geometry = ogr.CreateGeometryFromWkt(specification["aoi_wkt"])
    if geometry is None or geometry.IsEmpty():
        raise TerrainJobError("The analysis area has no usable geometry.")
    return geometry


def _open_local_loader(specification, acquisition_frame):
    path = specification.get("source_path", "")
    if not path or not os.path.isfile(path):
        raise TerrainJobError("The local DEM file cannot be opened.")
    loader = DEMLoader().open_metadata(path)
    source_frame = _transform_geometry(
        acquisition_frame, specification["aoi_crs_wkt"], loader.crs_wkt
    )
    xmin, xmax, ymin, ymax = source_frame.GetEnvelope()
    pad_x = max((xmax - xmin) * 0.08, abs(loader.gt[1]) * 16.0)
    pad_y = max((ymax - ymin) * 0.08, abs(loader.gt[5]) * 16.0)
    clipped = loader.clip_to_extent(
        xmin - pad_x, ymin - pad_y, xmax + pad_x, ymax + pad_y
    )
    return clipped, {
        "kind": "local",
        "name": specification.get("source_name") or os.path.basename(path),
        "file_name": os.path.basename(path),
    }


def _fetch_gsi_loader(specification, acquisition_frame, temporary, context):
    wgs84 = osr.SpatialReference()
    wgs84.ImportFromEPSG(4326)
    wgs84.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
    area84 = _transform_geometry(
        acquisition_frame, specification["aoi_crs_wkt"], wgs84.ExportToWkt()
    )
    xmin, xmax, ymin, ymax = area84.GetEnvelope()
    pad_x = max((xmax - xmin) * 0.08, 0.0002)
    pad_y = max((ymax - ymin) * 0.08, 0.0002)
    loader = GSITileDEMLoader()
    dataset = specification.get("gsi_dataset", "best")
    source_map = {
        "1m": [GSITileDEMLoader.TILE_SOURCES[0]],
        "5m": [GSITileDEMLoader.TILE_SOURCES[1]],
        "10m": [GSITileDEMLoader.TILE_SOURCES[2]],
    }

    def progress(done, total):
        context.check()
        context.report("Fetching GSI DEM tiles... {}/{}".format(done, total))

    loader.fetch_for_extent(
        xmin - pad_x,
        ymin - pad_y,
        xmax + pad_x,
        ymax + pad_y,
        sources=source_map.get(dataset),
        cancel_cb=context.cancelled,
        progress_cb=progress,
    )
    context.check()
    if loader.data is None or not np.isfinite(loader.data).any():
        details = getattr(loader, "last_errors", [])
        raise TerrainJobError(details[0] if details else "GSI DEM tiles are unavailable for this area.")
    work_dir = os.path.join(temporary, "_work")
    os.makedirs(work_dir, exist_ok=True)
    raw_path = os.path.join(work_dir, "gsi_source.tif")
    save_as_geotiff(loader, raw_path)
    prepared_loader = DEMLoader().load(raw_path)
    used = getattr(loader, "_used_source_label", "GSI DEM")
    return prepared_loader, {
        "kind": "gsi",
        "provider": "Geospatial Information Authority of Japan",
        "dataset": used,
        "requested_dataset": dataset,
        "attribution": "国土地理院標高タイル",
        "service": "https://maps.gsi.go.jp/development/ichiran.html",
    }


def _transform_geometry(geometry, source_wkt, destination_wkt):
    source = _spatial_reference(source_wkt)
    destination = _spatial_reference(destination_wkt)
    result = geometry.Clone()
    if not source.IsSame(destination):
        transformation = osr.CoordinateTransformation(source, destination)
        if result.Transform(transformation) != 0:
            raise TerrainJobError("The analysis area could not be transformed.")
    return result


def _spatial_reference(wkt):
    reference = osr.SpatialReference()
    if not wkt or reference.ImportFromWkt(wkt) != 0:
        raise TerrainJobError("The analysis area CRS is not defined.")
    reference.StripVertical()
    reference.DemoteTo2D()
    reference.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
    return reference


def _a_series_frame(geometry, landscape):
    """Return the smallest centred A-series rectangle containing geometry."""
    return _rectangle_geometry(_a_series_envelope(geometry.GetEnvelope(), landscape))


def _a_series_envelope(envelope, landscape):
    xmin, xmax, ymin, ymax = envelope
    width = xmax - xmin
    height = ymax - ymin
    if not all(math.isfinite(value) for value in envelope) or width <= 0 or height <= 0:
        raise TerrainJobError("The analysis area does not have a usable extent.")
    target = math.sqrt(2.0) if landscape else 1.0 / math.sqrt(2.0)
    if width / height < target:
        width = height * target
    else:
        height = width / target
    center_x = (xmin + xmax) / 2.0
    center_y = (ymin + ymax) / 2.0
    return (
        center_x - width / 2.0,
        center_x + width / 2.0,
        center_y - height / 2.0,
        center_y + height / 2.0,
    )


def _rectangle_geometry(envelope):
    xmin, xmax, ymin, ymax = envelope
    ring = ogr.Geometry(ogr.wkbLinearRing)
    ring.AddPoint_2D(xmin, ymax)
    ring.AddPoint_2D(xmax, ymax)
    ring.AddPoint_2D(xmax, ymin)
    ring.AddPoint_2D(xmin, ymin)
    ring.AddPoint_2D(xmin, ymax)
    geometry = ogr.Geometry(ogr.wkbPolygon)
    geometry.AddGeometry(ring)
    return geometry


def _a_series_pixel_window(loader, envelope, landscape):
    """Align an A-series frame to the square analysis grid without clipping it."""
    xmin, xmax, ymin, ymax = envelope
    inverse = gdal.InvGeoTransform(loader.gt)
    pixels = [
        gdal.ApplyGeoTransform(inverse, x, y)
        for x in (xmin, xmax)
        for y in (ymin, ymax)
    ]
    c0 = math.floor(min(pixel[0] for pixel in pixels))
    c1 = math.ceil(max(pixel[0] for pixel in pixels))
    r0 = math.floor(min(pixel[1] for pixel in pixels))
    r1 = math.ceil(max(pixel[1] for pixel in pixels))
    width = c1 - c0
    height = r1 - r0
    target_map_ratio = math.sqrt(2.0) if landscape else 1.0 / math.sqrt(2.0)
    pixel_width = math.hypot(loader.gt[1], loader.gt[4])
    pixel_height = math.hypot(loader.gt[2], loader.gt[5])
    target_pixel_ratio = target_map_ratio * pixel_height / pixel_width
    if width / height < target_pixel_ratio:
        required_width = math.ceil(height * target_pixel_ratio)
        extra = required_width - width
        c0 -= extra // 2
        c1 += extra - extra // 2
    else:
        required_height = math.ceil(width / target_pixel_ratio)
        extra = required_height - height
        r0 -= extra // 2
        r1 += extra - extra // 2
    if (r1 - r0) * (c1 - c0) > 100_000_000:
        raise TerrainJobError("The A-series output grid exceeds 100 million cells.")
    return r0, r1, c0, c1


def _extract_window(data, window, fill_value):
    """Extract a 2D/3D window and pad portions outside the acquired DEM."""
    r0, r1, c0, c1 = window
    shape = (r1 - r0, c1 - c0) + data.shape[2:]
    result = np.full(shape, fill_value, dtype=data.dtype)
    source_r0 = max(0, r0)
    source_r1 = min(data.shape[0], r1)
    source_c0 = max(0, c0)
    source_c1 = min(data.shape[1], c1)
    if source_r1 <= source_r0 or source_c1 <= source_c0:
        return result
    target_r0 = source_r0 - r0
    target_r1 = target_r0 + source_r1 - source_r0
    target_c0 = source_c0 - c0
    target_c1 = target_c0 + source_c1 - source_c0
    result[target_r0:target_r1, target_c0:target_c1, ...] = data[
        source_r0:source_r1, source_c0:source_c1, ...
    ]
    return result


def _window_envelope(loader, window):
    r0, r1, c0, c1 = window
    corners = [
        gdal.ApplyGeoTransform(loader.gt, col, row)
        for col in (c0, c1)
        for row in (r0, r1)
    ]
    return (
        min(point[0] for point in corners),
        max(point[0] for point in corners),
        min(point[1] for point in corners),
        max(point[1] for point in corners),
    )


def _area_mask(loader, geometry, window):
    r0, r1, c0, c1 = window
    x0, y0 = gdal.ApplyGeoTransform(loader.gt, c0, r0)
    gt = (x0, loader.gt[1], loader.gt[2], y0, loader.gt[4], loader.gt[5])
    rows = r1 - r0
    cols = c1 - c0
    raster = gdal.GetDriverByName("MEM").Create("", cols, rows, 1, gdal.GDT_Byte)
    raster.SetGeoTransform(gt)
    raster.SetProjection(loader.crs_wkt)
    band = raster.GetRasterBand(1)
    band.Fill(0)

    memory = ogr.GetDriverByName("Memory").CreateDataSource("")
    layer = memory.CreateLayer("area", _spatial_reference(loader.crs_wkt), ogr.wkbUnknown)
    feature = ogr.Feature(layer.GetLayerDefn())
    feature.SetGeometry(geometry)
    layer.CreateFeature(feature)
    gdal.RasterizeLayer(raster, [1], layer, burn_values=[1])
    mask = band.ReadAsArray().astype(bool)
    feature = None
    layer = None
    memory = None
    band = None
    raster = None
    if not np.any(mask):
        raise TerrainJobError("The analysis area does not cover any DEM cells.")
    return mask, gt
