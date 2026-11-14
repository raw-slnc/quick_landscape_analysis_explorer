# -*- coding: utf-8 -*-
"""DEM acquisition, terrain derivatives, and portable ZIP packaging."""

import contextlib
import datetime
import json
import math
import os
import shutil
import tempfile
import traceback
import uuid
import zipfile

import numpy as np
from osgeo import gdal, ogr, osr
from qgis.PyQt.QtCore import QThread, pyqtSignal
from qgis.core import Qgis, QgsMessageLog, QgsProcessingFeedback

from ..core.packages import MANIFEST_NAME
from ..terrain.analysis import compute_hillshade, compute_slope_deg
from ..terrain.cs_map import compute_cs_map
from ..terrain import (
    dem_sources,
    local_convert,
    nagano_rinmu,
    nagano_sabo,
    remote_dem,
    remote_zip,
    vs_lp,
)
from ..terrain.dem_loader import DEMLoader, GSITileDEMLoader, save_as_geotiff
from ..terrain.result_writer import save_raster, save_rgba_raster
from ..terrain import contours, watershed
from ..terrain.styles import write_catchment_qml, write_contour_qml, write_morizon_slope_qml


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
        # Processing (GRASS) steps watch this feedback; cancelling the worker
        # cancels them too.
        self.processing_feedback = QgsProcessingFeedback()

    def cancel(self):
        self._cancelled = True
        self.processing_feedback.cancel()

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
    if not specification.get("aoi_wkt"):
        # Polygon area: the polygons are combined here, not in the UI thread.
        parts = specification.pop("aoi_parts_wkb", None) or []
        specification["aoi_wkt"] = _combine_area(
            parts, context, specification.get("aoi_clip")
        )

    # A converted DEM (qlae_edit_dem/) goes into the ZIP and is
    # removed from its folder once the ZIP is written.
    edited_path = None
    # The DEMs opened below are closed (ExitStack) before the temporary folder
    # is removed: Windows cannot delete a file GDAL still holds open.
    with tempfile.TemporaryDirectory(
        prefix="qlae_", ignore_cleanup_errors=True
    ) as temporary, contextlib.ExitStack() as opened:
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
        requested = set(specification.get("outputs") or ())
        if "watershed" in requested:
            # Catchments need the land outside the view: fetch a wider DEM
            # and run every analysis on it, then cut to the A-series frame.
            acquisition_frame = _scaled_frame(acquisition_frame, WATERSHED_WIDEN)
        loader, source = _acquire_loader(
            specification, acquisition_frame, temporary, context
        )
        opened.callback(loader.close)
        if source.get("kind") == "local" and local_convert.is_edit_file(
            specification.get("source_path")
        ):
            edited_path = specification["source_path"]
            member = "data/qlae_edit/" + os.path.basename(edited_path)
            os.makedirs(os.path.join(temporary, "data", "qlae_edit"), exist_ok=True)
            shutil.copy2(edited_path, os.path.join(temporary, *member.split("/")))
            source["edited_dem"] = member
            source["conversion"] = local_convert.read_record(edited_path)

        context.check()
        prepared = loader.prepare_for_analysis()
        opened.callback(prepared.close)
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
        if specification.get("area_mode") == "map_extent":
            # The view is the area: the frame is the view aligned outward to
            # whole cells (and to the A-series ratio, which can add a row or
            # column wholly outside the view). Fill the whole frame.
            mask[:] = True
        rows, cols = mask.shape
        final_dem = _extract_window(prepared.data, window, np.nan)
        final_dem[~mask] = np.nan
        valid_inside = mask & np.isfinite(final_dem)
        if not np.any(valid_inside):
            raise TerrainJobError("DEM does not contain valid elevations in the analysis area.")

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

        catchments = None
        catchment_engine = None
        catchment_details = None
        if "watershed" in requested:
            context.report("Creating catchments...")
            try:
                catchments, catchment_engine, catchment_details = watershed.compute_catchments(
                    prepared.data,
                    prepared.gt,
                    prepared.crs_wkt,
                    _window_envelope(prepared, window),
                    context,
                    getattr(context, "processing_feedback", None),
                    prepared.cell_size,
                )
            except watershed.WatershedError as error:
                raise TerrainJobError(str(error))
            context.check()

        contour_lines = None
        if "contours" in requested:
            context.report("Creating contours...")
            # From the whole acquired DEM, so the blur near the edge of the
            # analysis area still sees the land around it.
            contour_lines = contours.compute_contours(
                prepared.data,
                prepared.gt,
                prepared.crs_wkt,
                prepared.cell_size,
                analysis_aoi.ExportToWkt(),
                os.path.join(temporary, "_work"),
                context,
            )
            context.check()

        context.report("Writing analysis package...")
        save_raster(
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

        if contour_lines is not None:
            contours.write_contours_gpkg(
                os.path.join(output_dir, "contours.gpkg"), contour_lines, prepared.crs_wkt
            )
            write_contour_qml(os.path.join(style_dir, "contours.qml"))
            output_members["contours"] = "outputs/contours.gpkg"
            output_members["contours_style"] = "styles/contours.qml"
        if catchments is not None:
            watershed.write_catchments_gpkg(
                os.path.join(output_dir, "catchments.gpkg"), catchments, prepared.crs_wkt
            )
            write_catchment_qml(os.path.join(style_dir, "catchments.qml"))
            output_members["watershed"] = "outputs/catchments.gpkg"
            output_members["watershed_style"] = "styles/catchments.qml"

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
            # Preview view at run time; analysis management shows this view.
            "preview_view": specification.get("preview_view"),
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
            "contours": {
                "method": contours.METHOD_TEXT,
                "interval_m": contours.INTERVAL_M,
                "index_interval_m": contours.INDEX_INTERVAL_M,
                "lines": len(contour_lines),
            } if contour_lines is not None else None,
            "watershed": {
                "method": watershed.method_text(catchment_engine),
                # "grass" (r.watershed) or "numpy" (no GRASS); see watershed.py.
                "engine": catchment_engine,
                # Seconds (whole step / flow computation) and grid cells, to
                # compare GRASS and numpy on one PC.
                "timing": catchment_details["timing"],
                # Why GRASS did not run when numpy took over (else None).
                "grass_error": catchment_details["grass_error"],
                "boundaries": watershed.ROUNDING_TEXT
                if catchments and all(item.get("rounded") for item in catchments)
                else "grid cell edges",
                "cell_size_m": max(watershed.CELL_SIZE_M, float(prepared.cell_size)),
                "min_area_ha": watershed.MIN_AREA_HA,
                # Catchments under min_area_ha (between close confluences)
                # joined to the catchment they drain into.
                "small_joined": catchment_details.get("joined_small"),
                # Cells of holes inside the DEM (water, survey gaps) filled
                # for the catchments only; pieces under fragment_cells cells
                # put into the largest catchment next to them.
                "holes_filled_cells": catchment_details.get("filled_cells"),
                "fragment_cells": watershed.FRAGMENT_CELLS,
                "acquisition": "A-series frame x{:g} (all analyses on the wider area, "
                "then cut to the frame)".format(WATERSHED_WIDEN),
                "catchments": len(catchments),
                "incomplete": sum(1 for item in catchments if not item["complete"]),
            } if catchments is not None else None,
            "styles": {
                "slope": "degrees, classes of 5 degrees from 20 to 45"
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

    removed = None
    if edited_path:
        # Closed now (the ExitStack above): the ZIP holds it from here on.
        try:
            os.remove(edited_path)
            removed = edited_path
        except OSError:
            pass
    return {"package_path": package_path, "manifest": manifest, "edited_dem_removed": removed}


# Polygons combined per batch, then the batches together (progress, and a
# smaller union each time).
_COMBINE_BATCH = 1000


def _combine_area(parts_wkb, context, clip=None):
    """WKT of the polygon area: the polygons combined into one.

    clip: (xmin, ymin, xmax, ymax) the area is cut to (the preview, when
    all polygons in view are used), or None.

    A forest plan of 146,920 polygons could not be combined at once in the
    UI thread (one broken polygon makes the whole union fail). Here: in
    batches with progress; if the union fails, again with the polygons
    repaired (makeValid); if that fails too, the polygons are kept as one
    collection without combining (the frame and the area mask come out the
    same).
    """
    from qgis.core import QgsGeometry

    geometries = []
    for wkb in parts_wkb:
        geometry = QgsGeometry()
        geometry.fromWkb(wkb)
        if not geometry.isNull() and not geometry.isEmpty():
            geometries.append(geometry)
    if not geometries:
        raise TerrainJobError("The analysis area has no usable geometry.")

    def union(items, label):
        batches = [items[i:i + _COMBINE_BATCH] for i in range(0, len(items), _COMBINE_BATCH)]
        merged = []
        for done, batch in enumerate(batches, start=1):
            context.check()
            context.report("{}… {}%".format(label, 100 * done // (len(batches) + 1)))
            part = QgsGeometry.unaryUnion(batch)
            if part.isNull() or part.isEmpty():
                return None
            merged.append(part)
        context.check()
        whole = merged[0] if len(merged) == 1 else QgsGeometry.unaryUnion(merged)
        return None if whole.isNull() or whole.isEmpty() else whole

    whole = union(geometries, "Combining the area")
    if whole is None:
        repaired = []
        for geometry in geometries:
            context.check()
            fixed = geometry.makeValid()
            if not fixed.isNull() and not fixed.isEmpty():
                repaired.append(fixed)
        whole = union(repaired, "Combining the area (repaired polygons)") if repaired else None
        if whole is None:
            whole = QgsGeometry.collectGeometry(repaired or geometries)
    if clip:
        from qgis.core import QgsRectangle

        cut = whole.intersection(QgsGeometry.fromRect(QgsRectangle(*clip)))
        if cut.isNull() or cut.isEmpty():
            raise TerrainJobError("The analysis area has no usable geometry.")
        whole = cut
    return whole.asWkt(12)


def _geometry_from_specification(specification):
    geometry = ogr.CreateGeometryFromWkt(specification["aoi_wkt"])
    if geometry is None or geometry.IsEmpty():
        raise TerrainJobError("The analysis area has no usable geometry.")
    return geometry


def _acquire_loader(specification, acquisition_frame, temporary, context):
    """Open or fetch the DEM chosen in the DEM section."""

    key = specification.get("source_key") or dem_sources.key_from_legacy(
        specification.get("source_kind"), specification.get("gsi_dataset")
    )
    if key == "local":
        return _open_local_loader(specification, acquisition_frame)
    if key.startswith("gsi:") or key.startswith("terrarium:"):
        return _fetch_tile_loader(key, specification, acquisition_frame, temporary, context)
    work_dir = os.path.join(temporary, "_work")
    os.makedirs(work_dir, exist_ok=True)
    try:
        if key == "vs_lp":
            return _fetch_vs_loader(specification, acquisition_frame, work_dir, context)
        if key in ("nagano_sabo", "nagano_rinmu"):
            return _fetch_nagano_loader(
                key, specification, acquisition_frame, work_dir, context
            )
        if key in remote_dem.REMOTE_SOURCES:
            return _fetch_remote_loader(
                key, specification, acquisition_frame, work_dir, context
            )
    except remote_dem.RemoteDemError as error:
        raise TerrainJobError(str(error))
    except (remote_zip.Cancelled,):
        raise TerrainJobCancelled()
    raise TerrainJobError("Unknown DEM source: {}".format(key))


def _source_envelope(acquisition_frame, specification, crs_wkt, cell):
    """Acquisition frame in the source CRS, padded like a local DEM."""

    frame = _transform_geometry(acquisition_frame, specification["aoi_crs_wkt"], crs_wkt)
    xmin, xmax, ymin, ymax = frame.GetEnvelope()
    pad_x = max((xmax - xmin) * 0.08, cell * 16.0)
    pad_y = max((ymax - ymin) * 0.08, cell * 16.0)
    return xmin - pad_x, ymin - pad_y, xmax + pad_x, ymax + pad_y


def _epsg_wkt(epsg):
    reference = osr.SpatialReference()
    reference.ImportFromEPSG(epsg)
    return reference.ExportToWkt()


def _clip_fetched(path, acquisition_frame, specification):
    loader = DEMLoader().open_metadata(path)
    xmin, ymin, xmax, ymax = _source_envelope(
        acquisition_frame, specification, loader.crs_wkt, abs(loader.gt[1])
    )
    return loader.clip_to_extent(xmin, ymin, xmax, ymax)


def _merge_or_single(paths, work_dir, prefix):
    if len(paths) == 1:
        return paths[0]
    merged = os.path.join(work_dir, prefix + "_merged.tif")
    vs_lp.merge_tifs(paths, merged)
    return merged


def _fetch_vs_loader(specification, acquisition_frame, work_dir, context):
    """VIRTUAL SHIZUOKA LP/Grid 0.5 m DTM tiles from its S3 bucket."""

    bbox = _source_envelope(acquisition_frame, specification, _epsg_wkt(6676), 0.5)
    remote_dem.check_size(bbox, 0.5)
    codes = vs_lp.tiles_for_extent(*bbox)
    if not codes:
        raise TerrainJobError("No VIRTUAL SHIZUOKA tiles cover the analysis area.")

    def checking(done, total):
        context.check()
        context.report("Checking VIRTUAL SHIZUOKA tiles... {}/{}".format(done, total))

    try:
        resolved = vs_lp.resolve_all_years(codes, progress_cb=checking)
    except vs_lp.VsConnectionError as error:
        # Not "no tiles": the bucket did not answer.
        raise TerrainJobError(
            "VIRTUAL SHIZUOKA could not be reached (no answer from the server). "
            "Check the network and run again.\n" + str(error)
        )
    if not resolved:
        raise TerrainJobError("No VIRTUAL SHIZUOKA tiles are available for the analysis area.")
    # The newest survey of a tile may cover only part of it (08OE8859: 2020
    # a corner, 2019 nearly all): fill what it leaves from older surveys.
    fetched = []  # (year, path)
    errors = []
    total = len(resolved)
    for index, (code, years) in enumerate(sorted(resolved.items()), start=1):
        txmin, tymin, txmax, tymax = vs_lp.tile_bbox(code)
        part = (max(txmin, bbox[0]), max(tymin, bbox[1]), min(txmax, bbox[2]), min(tymax, bbox[3]))
        for year in years:
            context.check()
            context.report("Downloading VIRTUAL SHIZUOKA tiles... {}/{}".format(index, total))
            # One folder a year: a tile's files are named by its code only.
            year_dir = os.path.join(work_dir, "vs_{}".format(year))
            os.makedirs(year_dir, exist_ok=True)
            try:
                path = vs_lp.download_grid_tif(
                    code, year, year_dir, cancel_cb=context.cancelled
                )
            except remote_zip.Cancelled:
                raise TerrainJobCancelled()
            except Exception as error:  # one tile failing should not stop the rest
                errors.append("{} ({}): {}".format(code, year, error))
                continue
            fetched.append((year, path))
            if vs_lp.missing_share(path, *part) < 0.0005:
                break
    if not fetched:
        raise TerrainJobError("All tile downloads failed.\n" + "\n".join(errors[:3]))
    context.check()
    # Older surveys first: the newer ones are laid over them.
    fetched.sort(key=lambda item: item[0])
    path = _merge_or_single([path for _year, path in fetched], work_dir, "vs_grid")
    years = sorted({year for year, _path in fetched})
    return _clip_fetched(path, acquisition_frame, specification), {
        "kind": "vs_lp",
        "provider": "Shizuoka Prefecture (VIRTUAL SHIZUOKA)",
        "dataset": "VS LP/Grid 0.5m DTM",
        "survey_years": years,
        "tiles": len({code for code in resolved}),
        "tile_files": len(fetched),
        "licence": "CC BY 4.0",
        "service": vs_lp.BUCKET_URL,
    }


def _fetch_nagano_loader(key, specification, acquisition_frame, work_dir, context):
    """Nagano Prefecture 0.5 m DEM tiles read partly from their ZIP archives."""

    module = nagano_sabo if key == "nagano_sabo" else nagano_rinmu
    bbox = _source_envelope(acquisition_frame, specification, _epsg_wkt(6676), 0.5)
    remote_dem.check_size(bbox, 0.5)
    codes = module.tiles_for_extent(*bbox)
    if not codes:
        raise TerrainJobError("No Nagano Prefecture DEM tiles cover the analysis area.")
    codes = sorted(codes)
    paths = []
    missing = []
    total = len(codes)
    for index, code in enumerate(codes, start=1):
        context.check()
        prefix = "Nagano DEM tile {}/{} ({})".format(index, total, code)

        def progress(phase, place, detail, prefix=prefix):
            context.report(
                "{}: checking {}...".format(prefix, place)
                if phase == "checking"
                else "{}: downloading {:.0f}/{:.0f} MB...".format(
                    prefix, detail[0] / 1e6, detail[1] / 1e6
                )
            )

        context.report("{}: locating...".format(prefix))
        path = module.download_tile_tif(
            code, work_dir, cancel_cb=context.cancelled, progress_cb=progress
        )
        context.check()
        if path:
            paths.append(path)
        else:
            missing.append(code)
    if not paths:
        raise TerrainJobError("No Nagano Prefecture DEM tiles could be fetched for this area.")
    path = _merge_or_single(paths, work_dir, key)
    sabo = key == "nagano_sabo"
    return _clip_fetched(path, acquisition_frame, specification), {
        "kind": key,
        "provider": "Nagano Prefecture ({})".format("Sabo Division" if sabo else "Forestry Department"),
        "dataset": "Nagano {} 0.5m DEM".format("Sabo R3-4" if sabo else "Rinmu"),
        "tiles": len(paths),
        "missing_tiles": missing,
        "service": "https://www.geospatial.jp/ckan/dataset/{}".format(
            "r3-4-50cmdem" if sabo else "nagano-dem"
        ),
    }


def _fetch_remote_loader(key, specification, acquisition_frame, work_dir, context):
    """Overseas DEM (NL AHN4, FR IGN LiDAR HD, NZ LINZ, German DGM1) for the area."""

    spec = remote_dem.REMOTE_SOURCES[key]
    bbox = _source_envelope(
        acquisition_frame, specification, _epsg_wkt(spec["epsg"]), spec["cell"]
    )
    path, extra = remote_dem.fetch_remote_dem(context, key, bbox, work_dir)
    source = {
        "kind": key,
        "provider": spec["provider"],
        "dataset": spec["label"],
        "licence": spec["licence"],
        "service": spec["service"],
    }
    source.update(extra)  # credit line and survey years, where the source has them
    return _clip_fetched(path, acquisition_frame, specification), source


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


def _fetch_tile_loader(key, specification, acquisition_frame, temporary, context):
    """GSI (Japan) or AWS Terrarium (global) elevation tiles."""

    kind, _sep, dataset = key.partition(":")
    if kind == "terrarium":
        sources = {
            "2m": GSITileDEMLoader.TERRARIUM_SOURCES[0],
            "5m": GSITileDEMLoader.TERRARIUM_SOURCES[1],
            "10m": GSITileDEMLoader.TERRARIUM_SOURCES[2],
        }
        if dataset not in sources:
            raise TerrainJobError("Unknown Terrarium resolution: {}".format(dataset))
        return _fetch_gsi_loader(
            specification, acquisition_frame, temporary, context,
            dataset=dataset, sources=[sources[dataset]], terrarium=True,
        )
    return _fetch_gsi_loader(
        specification, acquisition_frame, temporary, context, dataset=dataset or "best"
    )


def _fetch_gsi_loader(
    specification, acquisition_frame, temporary, context,
    dataset="best", sources=None, terrarium=False,
):
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
    if sources is None:
        source_map = {
            "1m": [GSITileDEMLoader.TILE_SOURCES[0]],
            "5m": [GSITileDEMLoader.TILE_SOURCES[1]],
            "10m": [GSITileDEMLoader.TILE_SOURCES[2]],
        }
        sources = source_map.get(dataset)
    label = "Terrarium DEM tiles" if terrarium else "GSI DEM tiles"

    def progress(done, total):
        context.check()
        context.report("Fetching {}... {}/{}".format(label, done, total))

    loader.fetch_for_extent(
        xmin - pad_x,
        ymin - pad_y,
        xmax + pad_x,
        ymax + pad_y,
        sources=sources,
        cancel_cb=context.cancelled,
        progress_cb=progress,
    )
    context.check()
    if loader.data is None or not np.isfinite(loader.data).any():
        details = getattr(loader, "last_errors", [])
        raise TerrainJobError(details[0] if details else "{} are unavailable for this area.".format(label))
    work_dir = os.path.join(temporary, "_work")
    os.makedirs(work_dir, exist_ok=True)
    raw_path = os.path.join(work_dir, "tile_source.tif")
    save_as_geotiff(loader, raw_path)
    prepared_loader = DEMLoader().load(raw_path)
    if terrarium:
        return prepared_loader, {
            "kind": "terrarium",
            "provider": "AWS Terrain Tiles (Mapzen Terrarium)",
            "dataset": getattr(loader, "_used_source_label", "Terrarium"),
            "requested_dataset": dataset,
            "attribution": "Terrain Tiles: Mapzen / AWS Open Data and their data sources",
            "service": "https://registry.opendata.aws/terrain-tiles/",
        }
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


# The DEM for catchments is fetched this many times wider (and higher) than
# the A-series frame: one frame added on every side.
WATERSHED_WIDEN = 3.0


def _scaled_frame(frame, factor):
    xmin, xmax, ymin, ymax = frame.GetEnvelope()
    cx = (xmin + xmax) / 2.0
    cy = (ymin + ymax) / 2.0
    half_w = (xmax - xmin) * factor / 2.0
    half_h = (ymax - ymin) * factor / 2.0
    return _rectangle_geometry((cx - half_w, cx + half_w, cy - half_h, cy + half_h))


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
    # Every cell the area touches. The window is aligned outward to whole
    # cells, so the cells along its edges are only partly in the area; with
    # cell centres only, those edge rows and columns came out empty.
    gdal.RasterizeLayer(raster, [1], layer, burn_values=[1], options=["ALL_TOUCHED=TRUE"])
    mask = band.ReadAsArray().astype(bool)
    feature = None
    layer = None
    memory = None
    band = None
    raster = None
    if not np.any(mask):
        raise TerrainJobError("The analysis area does not cover any DEM cells.")
    return mask, gt
