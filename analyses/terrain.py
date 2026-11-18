# -*- coding: utf-8 -*-
"""DEM acquisition, terrain derivatives, and portable ZIP packaging."""

import contextlib
import datetime
import errno
import json
import math
import os
import shutil
import tempfile
import threading
import traceback
import uuid
import zipfile

import numpy as np
from osgeo import gdal, ogr, osr
from qgis.PyQt.QtCore import QThread, pyqtSignal
from qgis.core import Qgis, QgsMessageLog, QgsProcessingFeedback

from ..core import dem_store, memory
from ..core.memory import SURFACE_MARGIN_CELLS
from ..core.packages import MANIFEST_NAME
from ..terrain.analysis import compute_hillshade, compute_slope_deg
from ..terrain.cs_map import compute_cs_map
from ..terrain import (
    dem_sources,
    kept_tiles,
    sources,
    local_convert,
    nagano_rinmu,
    nagano_sabo,
    remote_dem,
    remote_zip,
    vs_lp,
)
from ..terrain.dem_loader import DEMLoader, GSITileDEMLoader, save_as_geotiff
from ..terrain.result_writer import (
    GEOPDF_DEM_NAME, save_geopdf_dem, save_raster, save_rgba_raster,
)
from ..terrain import contours, coverage, dem_gaps, watershed
from ..terrain.styles import write_catchment_qml, write_contour_qml, write_morizon_slope_qml


class TerrainJobCancelled(Exception):
    pass


class TerrainJobError(Exception):
    pass


class TerrainJobWorker(QThread):
    message = pyqtSignal(str)
    done = pyqtSignal(object, bool, str)
    # Bytes the next heavy step needs (for the memory meter).
    needs_memory = pyqtSignal(float)
    # (bytes needed, bytes free): the UI asks whether to go on and calls
    # answer_memory().
    short_of_memory = pyqtSignal(float, float)

    def __init__(self, specification, parent=None):
        super().__init__(parent)
        self.specification = dict(specification)
        self._cancelled = False
        self._answered = threading.Event()
        self._go_on = False
        # The last step reported, and a line from work running beside it
        # (the regional analysis's fetch): shown together.
        self._main_text = ""
        self._side_text = ""
        self._text_lock = threading.Lock()
        # Processing (GRASS) steps watch this feedback; cancelling the worker
        # cancels them too.
        self.processing_feedback = QgsProcessingFeedback()

    def cancel(self):
        self._cancelled = True
        self.processing_feedback.cancel()
        self._answered.set()

    def memory_need(self, need):
        self.needs_memory.emit(float(need))

    def confirm_memory(self, need, available):
        """Wait here (the analysis pauses) until the UI answers."""
        self._go_on = False
        self._answered.clear()
        self.short_of_memory.emit(float(need), float(available or 0))
        self._answered.wait()
        return self._go_on and not self._cancelled

    def answer_memory(self, go_on):
        self._go_on = bool(go_on)
        self._answered.set()

    def cancelled(self):
        return self._cancelled

    def check(self):
        if self._cancelled:
            raise TerrainJobCancelled()

    def report(self, text):
        with self._text_lock:
            self._main_text = text
            combined = text + ("\n" + self._side_text if self._side_text else "")
        self.message.emit(combined)

    def report_side(self, text):
        """A second line for work running beside the steps (from another
        thread); "" takes it away."""
        with self._text_lock:
            self._side_text = text or ""
            combined = self._main_text + ("\n" + self._side_text if self._side_text else "")
        self.message.emit(combined)

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
    ) as temporary, _out_of_space_as_error(
        temporary, os.path.dirname(package_path), specification.get("dem_store_dir")
    ), contextlib.ExitStack() as opened:
        data_dir = os.path.join(temporary, "data")
        output_dir = os.path.join(temporary, "outputs")
        style_dir = os.path.join(temporary, "styles")
        area_dir = os.path.join(temporary, "area")
        for directory in (data_dir, output_dir, style_dir, area_dir):
            os.makedirs(directory, exist_ok=True)
        # The regional analysis's fetching, beside the steps below (its
        # route's prefetch, in a thread of its own); stopped when the job
        # ends whichever way.
        prefetch = _RegionalPrefetch(specification, temporary, context)
        opened.callback(prefetch.stop)
        if prefetch.route is not None and not _offline(specification):
            # Where the route keeps what it reads by tiles (the DSM chunks):
            # the same store as the DEM's.
            try:
                kept = _tile_store(specification, context)
                prefetch.info["tile_store"] = (kept.root, kept.kind)
            except (OSError, TerrainJobError):
                pass
        prefetch.start()

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
        # Hillshade, slope and the CS Map on the frame with a margin, not on
        # the whole acquired DEM: with catchments that is three times wider
        # and, at 0.5 m, too large for the memory (the CS Map alone needs
        # some 360 bytes a cell).
        outer, inner = _margin_window(prepared.data, window, SURFACE_MARGIN_CELLS)
        surface_dem = prepared.data[outer[0]:outer[1], outer[2]:outer[3]]
        # The DEM is read: with its real size, ask before the heaviest step
        # when the free memory is short (other programs may have taken some
        # since the run started). Linux stops QGIS when memory runs out.
        _check_memory(
            context,
            memory.step_need(requested, surface_dem.size, prepared.data.size),
        )
        if "hillshade" in requested:
            context.report("Creating hillshade...")
            hillshade_full = compute_hillshade(surface_dem, prepared.cell_size)
            hillshade = _extract_window(hillshade_full, inner, np.nan)
            del hillshade_full
            hillshade[~mask] = np.nan
            context.check()

        if "slope" in requested:
            context.report("Creating slope...")
            slope_full = compute_slope_deg(surface_dem, prepared.cell_size)
            slope = _extract_window(slope_full, inner, np.nan)
            del slope_full
            slope[~mask] = np.nan
            context.check()

        if "cs_map" in requested:
            context.report("Creating CS Map...")
            cs_full = compute_cs_map(surface_dem, prepared.cell_size)
            cs_map = _extract_window(cs_full, inner, 0)
            del cs_full
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
        # The DEM in the preview's CRS (the GeoPDF's), uncompressed, for apps
        # that combine it with the GeoPDF. Not a layer; written out with the
        # files. Without it the analysis is complete all the same.
        preview_view = specification.get("preview_view") or {}
        preview_crs_wkt = preview_view.get("crs_wkt")
        # Why the DEM for the GeoPDF was not made, for the finish message:
        # "network", "too_wide" (dem_gaps.FillIncomplete) or "error".
        geopdf_dem_missing = None
        if preview_crs_wkt:
            try:
                context.report("Writing the DEM for the GeoPDF...")
                _write_geopdf_dem(
                    prepared, preview_view, os.path.join(data_dir, "dem.tif"),
                    os.path.join(data_dir, GEOPDF_DEM_NAME),
                    os.path.join(temporary, "_work"),
                    "gsi" if specification.get("geopdf_dem_japan") else "terrarium",
                    context.check,
                    fill_holes_first=_geopdf_fill_holes_first(specification),
                )
                output_members["dem_geopdf"] = "data/" + GEOPDF_DEM_NAME
            except dem_gaps.FillIncomplete as error:
                geopdf_dem_missing = error.reason
                QgsMessageLog.logMessage(
                    "The DEM for the GeoPDF was not made: its gaps could not be "
                    "filled ({}).".format(error.reason),
                    "Quick Landscape Analysis Explorer", Qgis.MessageLevel.Warning,
                )
            except (RuntimeError, OSError, ValueError, TerrainJobError) as error:
                geopdf_dem_missing = "error"
                QgsMessageLog.logMessage(
                    "The DEM for the GeoPDF could not be written: {}".format(error),
                    "Quick Landscape Analysis Explorer", Qgis.MessageLevel.Warning,
                )
            context.check()
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
            # Set when the DEM for the GeoPDF was not made (see above).
            "geopdf_dem_missing": geopdf_dem_missing,
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
                "acquisition": "A-series frame x{:g} (catchments and contours on the "
                "wider area, hillshade, slope and CS Map on the frame with a margin "
                "of {} cells, then cut to the frame)".format(
                    WATERSHED_WIDEN, SURFACE_MARGIN_CELLS),
                "catchments": len(catchments),
                "incomplete": sum(1 for item in catchments if not item["complete"]),
            } if catchments is not None else None,
            "styles": {
                "slope": "degrees, classes of 5 degrees from 20 to 45"
                if "slope" in requested
                else None
            },
        }
        # The country's regional analysis, after the basic one: its route
        # (terrain/sources/) writes into regional/<key>/ of the same ZIP.
        manifest["regional"] = _run_regional(
            specification, temporary, output_members, context, prefetch
        )
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

    loader, source = _acquire_source(specification, acquisition_frame, temporary, context)
    if _offline(specification):
        # From the QLAE folder: the source's own facts (provider, licence,
        # credit), read from the tiles kept there. The folder's path is not
        # recorded.
        source["from_qlae_folder"] = True
    return loader, source


def _acquire_source(specification, acquisition_frame, temporary, context):
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

    offline = _offline(specification)
    if offline:
        # The QLAE folder: the years kept there, newest first.
        store = _tile_store(specification, context)
        kept = kept_tiles.codes(store.root, "vs_lp")
        resolved = {code: kept[code] for code in codes if code in kept}
        if not resolved:
            raise TerrainJobError(NO_KEPT_TILES_ERROR)
    else:
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
        # Tiles kept from earlier analyses (this QGIS run, or the chosen
        # folder) are used as they are; the survey years above are still
        # checked, so a newer year is fetched.
        store = _tile_store(specification, context)
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
            path = store.find("vs_lp", year, code)
            if path is not None:
                store.reused += 1
            elif offline:
                continue
            else:
                incoming = store.incoming()
                try:
                    path = store.keep("vs_lp", year, code, vs_lp.download_grid_tif(
                        code, year, incoming, cancel_cb=context.cancelled
                    ))
                    store.downloaded += 1
                except remote_zip.Cancelled:
                    raise TerrainJobCancelled()
                except Exception as error:  # one tile failing should not stop the rest
                    errors.append("{} ({}): {}".format(code, year, error))
                    continue
                finally:
                    store.drop_incoming(incoming)
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
        "provider": sources.get("vs_lp")["facts"]["provider"],
        "dataset": sources.get("vs_lp")["facts"]["dataset"],
        "survey_years": years,
        "tiles": len({code for code in resolved}),
        "tile_files": len(fetched),
        # Kept tiles used again, and tiles downloaded (now kept too). The
        # store: "session" (this QGIS run) or "folder" (chosen by the user;
        # its path is not written here).
        "tiles_reused": store.reused,
        "tiles_downloaded": store.downloaded,
        "tile_store": store.kind,
        "licence": sources.get("vs_lp")["facts"]["licence"],
        "service": vs_lp.BUCKET_URL,
    }


def _offline(specification):
    """The DEM source is the QLAE folder: only the kept tiles are used,
    nothing is fetched; where none is kept there is no data."""
    return bool(specification.get("offline_store"))


def _tile_store(specification, context):
    """Where the source tiles are kept for reuse (core/dem_store.py)."""
    if _offline(specification):
        folder = specification.get("dem_store_dir") or ""
        if not folder or not os.path.isdir(folder):
            raise TerrainJobError("The folder of kept DEM tiles was not found.")
        return dem_store.TileStore(folder, "folder")
    package_path = os.path.abspath(specification["package_path"])
    # <project>/quick_landscape_analysis/analyses/<name>.zip
    project_directory = os.path.dirname(os.path.dirname(os.path.dirname(package_path)))
    context.report("Preparing the kept DEM tiles...")
    return dem_store.open_store(project_directory, specification.get("dem_store_dir") or "")


def _fetch_nagano_loader(key, specification, acquisition_frame, work_dir, context):
    """Nagano Prefecture 0.5 m DEM tiles read partly from their ZIP archives."""

    module = nagano_sabo if key == "nagano_sabo" else nagano_rinmu
    bbox = _source_envelope(acquisition_frame, specification, _epsg_wkt(6676), 0.5)
    remote_dem.check_size(bbox, 0.5)
    codes = module.tiles_for_extent(*bbox)
    if not codes:
        raise TerrainJobError("No Nagano Prefecture DEM tiles cover the analysis area.")
    codes = sorted(codes)
    # Kept tiles (core/dem_store.py): the tiles have no survey years to check.
    store = _tile_store(specification, context)
    paths = []
    missing = []
    total = len(codes)
    for index, code in enumerate(codes, start=1):
        context.check()
        kept = store.find(key, None, code)
        if kept is not None:
            store.reused += 1
            paths.append(kept)
            continue
        if _offline(specification):
            missing.append(code)
            continue
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
        incoming = store.incoming()
        try:
            path = module.download_tile_tif(
                code, incoming, cancel_cb=context.cancelled, progress_cb=progress
            )
            context.check()
            if path:
                path = store.keep(key, None, code, path)
                store.downloaded += 1
        finally:
            store.drop_incoming(incoming)
        if path:
            paths.append(path)
        else:
            missing.append(code)
    if not paths:
        if _offline(specification):
            raise TerrainJobError(NO_KEPT_TILES_ERROR)
        raise TerrainJobError("No Nagano Prefecture DEM tiles could be fetched for this area.")
    path = _merge_or_single(paths, work_dir, key)
    return _clip_fetched(path, acquisition_frame, specification), {
        "kind": key,
        "provider": sources.get(key)["facts"]["provider"],
        "dataset": sources.get(key)["facts"]["dataset"],
        "tiles": len(paths),
        "missing_tiles": missing,
        "tiles_reused": store.reused,
        "tiles_downloaded": store.downloaded,
        "tile_store": store.kind,
        "service": sources.get(key)["facts"]["service"],
    }


def _fetch_remote_loader(key, specification, acquisition_frame, work_dir, context):
    """Overseas DEM (NL AHN4, FR IGN LiDAR HD, NZ LINZ, German DGM1) for the area."""

    spec = remote_dem.REMOTE_SOURCES[key]
    bbox = _source_envelope(
        acquisition_frame, specification, _epsg_wkt(spec["epsg"]), spec["cell"]
    )
    # Kept tiles (core/dem_store.py), for the sources fetched by tiles.
    # Kept for reuse: the German tiles, and the chunks AHN and IGN are read
    # in (on a fixed grid). LINZ is read in parts of large files: not kept.
    store = _tile_store(specification, context) if spec["kind"] in ("de_tiles", "ogc") else None
    if _offline(specification) and store is None:
        raise TerrainJobError(NO_KEPT_TILES_ERROR)  # not kept: fetched by area
    try:
        path, extra = remote_dem.fetch_remote_dem(
            context, key, bbox, work_dir, store=store,
            kept=kept_tiles.codes(store.root, key) if _offline(specification) else None,
        )
    except remote_dem.RemoteDemError:
        if _offline(specification):
            raise TerrainJobError(NO_KEPT_TILES_ERROR)
        raise
    if store is not None:
        extra.update(
            tiles_reused=store.reused, tiles_downloaded=store.downloaded, tile_store=store.kind
        )
    _take_coverage(key, extra, specification)
    source = {
        "kind": key,
        "provider": spec["provider"],
        "dataset": spec["label"],
        "licence": spec["licence"],
        "service": spec["service"],
    }
    source.update(extra)  # credit line and survey years, where the source has them
    if spec.get("survey_years") and "survey_years" not in source:
        # Years of the whole dataset (AHN4), where tiles do not name theirs.
        source["survey_years"] = list(spec["survey_years"])
    return _clip_fetched(path, acquisition_frame, specification), source


def _take_coverage(key, extra, specification):
    """What the fetch learnt about where the source has tiles, into its
    coverage mesh (terrain/coverage.py): the whole list read anyway
    (Rhineland-Palatinate, New Zealand) or the tiles found (Bavaria). No
    more network; a failure here never stops the analysis."""
    listed = extra.pop("_coverage_list", None)
    names = extra.pop("_coverage_names", None)
    found = extra.pop("_coverage_found", None)
    if _offline(specification):
        return
    try:
        if key == "rlp_dgm1" and listed:
            coverage.take_list(key, [coverage._rlp_cell(e, n) for e, n in listed])
        elif key == "linz_dem" and names:
            cells = []
            for name in names:
                try:
                    cells.append(coverage._linz_cell(name))
                except (ValueError, IndexError):
                    continue
            coverage.take_list(key, cells)
        elif key == "by_dgm1" and found:
            coverage.take_found(key, [coverage._rlp_cell(e, n) for e, n in found])
    except Exception as error:  # noqa: BLE001 - only the mesh is left as it was
        QgsMessageLog.logMessage(
            "The coverage mesh was not updated: {}".format(error),
            "Quick Landscape Analysis Explorer", Qgis.MessageLevel.Info,
        )


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


class _RegionalPrefetch:
    """The route's prefetch(folder, info, progress, cancelled), when it has
    one, run beside the job's steps: for data that takes a while to fetch
    (the Netherlands' orthophoto). Its result is handed to run()."""

    def __init__(self, specification, temporary, context):
        self.key = specification.get("regional_option")
        self.context = context
        self.result = None
        self.error = None
        self._thread = None
        self._stop = threading.Event()
        option, self.route = sources.option_by_key(self.key) if self.key else (None, None)
        self.folder_member = "regional/" + (self.key or "")
        self.folder = os.path.join(temporary, *self.folder_member.split("/"))
        self.info = {"preview_view": specification.get("preview_view") or {},
                     "folder_member": self.folder_member,
                     # The QLAE folder, where large regional data is kept.
                     "kept_folder": specification.get("dem_store_dir") or ""}

    def start(self):
        if self.route is None or not hasattr(self.route, "prefetch"):
            return
        self._thread = threading.Thread(target=self._run, name="qlae-regional-prefetch", daemon=True)
        self._thread.start()

    def _run(self):
        report_side = getattr(self.context, "report_side", lambda _text: None)
        try:
            self.result = self.route.prefetch(
                self.folder, self.info, report_side,
                lambda: self._stop.is_set() or self.context.cancelled(),
            )
        except Exception as error:  # noqa: BLE001 - recorded; the basic analysis goes on
            self.error = error
        finally:
            # Done: its line goes (it stayed at its last step, as "100%",
            # until the whole analysis ended).
            report_side("")

    def wait(self):
        """Wait for the prefetch (the job can still be cancelled)."""
        while self._thread is not None and self._thread.is_alive():
            self.context.check()
            self._thread.join(0.2)

    def stop(self):
        self._stop.set()
        if self._thread is not None:
            self._thread.join(30)
            self.context.report_side("")


def _geopdf_fill_holes_first(specification):
    """Whether the chosen regional analysis's country fills the DEM's holes
    from around them before the DEM for the GeoPDF is made (its OPTION's
    "geopdf_fill_holes")."""
    key = specification.get("regional_option")
    option, _route = sources.option_by_key(key) if key else (None, None)
    return bool(option and option.get("geopdf_fill_holes"))


def _run_regional(specification, temporary, output_members, context, prefetch=None):
    """Run the chosen regional analysis (its route does the work); its
    record for the manifest, or None. A failure is recorded and logged,
    and the basic analysis is kept."""
    key = specification.get("regional_option")
    if not key:
        return None
    option, route = sources.option_by_key(key)
    if option is None or route is None:
        return {"key": key, "error": "not available"}
    folder_member = "regional/" + key
    context.check()
    context.report("Regional analysis...")
    if prefetch is not None:
        prefetch.wait()
    try:
        result = route.run(
            os.path.join(temporary, *folder_member.split("/")),
            {"outputs": dict(output_members), "folder_member": folder_member,
             # Where the basic analysis's files are (outputs[...] under it).
             "root": temporary,
             # The analysis area (area/aoi.wkt under root) is in this CRS.
             "area_crs_wkt": specification.get("aoi_crs_wkt") or "",
             "prefetched": prefetch.result if prefetch is not None else None,
             "prefetch_error": str(prefetch.error) if prefetch is not None and prefetch.error else None},
            context,
        ) or {}
    except (TerrainJobCancelled, TerrainJobError):
        raise
    except Exception as error:  # noqa: BLE001 - the basic analysis is kept
        QgsMessageLog.logMessage(
            "The regional analysis {} failed: {}".format(key, error),
            "Quick Landscape Analysis Explorer", Qgis.MessageLevel.Warning,
        )
        return {"key": key, "error": str(error)}
    return dict(result, key=key, name=option["label"])


def _facts(key):
    """The fixed facts of a source from its country file (terrain/sources/)."""
    return sources.get(key).get("facts", {})


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
    # The source's facts (terrain/sources/): the key it was chosen by.
    fact_key = ("terrarium:" if terrarium else "gsi:") + dataset
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
    # Kept tiles (core/dem_store.py), by zoom level; tiles that could not be
    # fetched (the sea, outside the coverage) are not kept.
    store = loader.tile_store = _tile_store(specification, context)
    loader.offline = _offline(specification)
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
        if loader.offline:
            raise TerrainJobError(NO_KEPT_TILES_ERROR)
        details = getattr(loader, "last_errors", [])
        raise TerrainJobError(details[0] if details else "{} are unavailable for this area.".format(label))
    work_dir = os.path.join(temporary, "_work")
    os.makedirs(work_dir, exist_ok=True)
    raw_path = os.path.join(work_dir, "tile_source.tif")
    save_as_geotiff(loader, raw_path)
    prepared_loader = DEMLoader().load(raw_path)
    kept = {
        "tiles_reused": store.reused,
        "tiles_downloaded": store.downloaded,
        "tile_store": store.kind,
    }
    if terrarium:
        return prepared_loader, dict(kept, **{
            "kind": "terrarium",
            "provider": _facts(fact_key)["provider"],
            "dataset": getattr(loader, "_used_source_label", "Terrarium"),
            "requested_dataset": dataset,
            "attribution": _facts(fact_key)["attribution"],
            "service": _facts(fact_key)["service"],
        })
    used = getattr(loader, "_used_source_label", "GSI DEM")
    return prepared_loader, dict(kept, **{
        "kind": "gsi",
        "provider": _facts(fact_key)["provider"],
        "dataset": used,
        "requested_dataset": dataset,
        "attribution": _facts(fact_key)["attribution"],
        "service": _facts(fact_key)["service"],
    })


def _transform_geometry(geometry, source_wkt, destination_wkt):
    source = _spatial_reference(source_wkt)
    destination = _spatial_reference(destination_wkt)
    result = geometry.Clone()
    if not source.IsSame(destination):
        transformation = osr.CoordinateTransformation(source, destination)
        if result.Transform(transformation) != 0:
            raise TerrainJobError("The analysis area could not be transformed.")
    return result


def _write_geopdf_dem(prepared, preview_view, dem_path, out_path, work_dir, fill, check,
                      fill_holes_first=False):
    """The DEM for use with the GeoPDF: the preview's area in its CRS.

    In stages: (1) the DEM it starts from: the region's step, when its
    country file asks (``fill_holes_first``, the Netherlands: AHN's holes
    under buildings, water and dense trees filled from the ground around
    them, at the DEM's own resolution); then for every region (2) cut to
    the preview and warped to its CRS, (3) small holes filled from around
    them and (4) what is left from the region's external data (``fill``).

    The GeoPDF shows the preview, which is often wider than the analysis
    frame (with a polygon area, the land around it). So the DEM is cut from
    the whole DEM acquired for the analysis (not masked to the polygon) over
    the preview's area; where nothing was acquired it has no data. Without a
    preview extent, the analysis DEM in the preview's CRS. fill: "gsi" in
    Japan, "terrarium" elsewhere: cells without a height get one from those
    tiles (result_writer.save_geopdf_dem).
    """

    crs_wkt = preview_view["crs_wkt"]
    extent = preview_view.get("extent")
    if not extent or len(extent) != 4:
        save_geopdf_dem(dem_path, out_path, crs_wkt, fill=fill, check=check)
        return
    xmin, ymin, xmax, ymax = (float(value) for value in extent)
    ring = ogr.Geometry(ogr.wkbLinearRing)
    for x, y in ((xmin, ymin), (xmax, ymin), (xmax, ymax), (xmin, ymax), (xmin, ymin)):
        ring.AddPoint_2D(x, y)
    area = ogr.Geometry(ogr.wkbPolygon)
    area.AddGeometry(ring)
    area.Segmentize(max(xmax - xmin, ymax - ymin) / 20.0)
    in_grid = _transform_geometry(area, crs_wkt, prepared.crs_wkt)
    gxmin, gxmax, gymin, gymax = in_grid.GetEnvelope()
    # The cells of the acquired DEM under the preview, with a margin.
    inverse = gdal.InvGeoTransform(prepared.gt)
    pixels = [gdal.ApplyGeoTransform(inverse, x, y) for x in (gxmin, gxmax) for y in (gymin, gymax)]
    pad = 2 + 4  # resampling, and the margin added in the preview's CRS
    rows, cols = prepared.data.shape
    c0 = max(0, int(math.floor(min(p[0] for p in pixels))) - pad)
    c1 = min(cols, int(math.ceil(max(p[0] for p in pixels))) + pad)
    r0 = max(0, int(math.floor(min(p[1] for p in pixels))) - pad)
    r1 = min(rows, int(math.ceil(max(p[1] for p in pixels))) + pad)
    if c0 >= c1 or r0 >= r1:
        # The preview is away from the acquired DEM: the analysis DEM only.
        save_geopdf_dem(dem_path, out_path, crs_wkt, bounds=(xmin, ymin, xmax, ymax),
                        cell=prepared.cell_size, fill=fill, check=check)
        return
    gt = prepared.gt
    cut_gt = (gt[0] + c0 * gt[1] + r0 * gt[2], gt[1], gt[2],
              gt[3] + c0 * gt[4] + r0 * gt[5], gt[4], gt[5])
    os.makedirs(work_dir, exist_ok=True)
    cut_path = save_raster(
        prepared.data[r0:r1, c0:c1], cut_gt, prepared.crs_wkt, work_dir,
        "dem_preview", overwrite=True,
    )
    if fill_holes_first:
        # Stage 1 for this region: holes not reaching the edge, from the
        # heights around them (as the regional "DEM (holes filled)").
        watershed.fill_enclosed_holes(cut_path, near_holes=True)
        check()
    save_geopdf_dem(cut_path, out_path, crs_wkt, bounds=(xmin, ymin, xmax, ymax),
                    cell=prepared.cell_size, fill=fill, check=check)


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


# The start of the error an analysis stops with when a disk ran out of space;
# the window words it (dialog._terrain_error_text). Followed by lines: the
# folder, its free bytes, its whole bytes.
DISK_FULL_ERROR = "disk_full"
# The QLAE folder holds no tile of the chosen data over the analysis area
# (the window words it).
NO_KEPT_TILES_ERROR = "no_kept_tiles"
# Less free space than this in a folder after a failure: the disk was full.
DISK_FULL_FREE_BYTES = 64 * 1024 * 1024


@contextlib.contextmanager
def _out_of_space_as_error(*folders):
    """Turn a failure from a full disk (the temporary folder or the ZIP's)
    into a TerrainJobError the window can word: otherwise it reads as a GDAL
    write error, or as failed downloads."""
    try:
        yield
    except TerrainJobCancelled:
        raise
    except Exception as error:
        folder = _full_folder(error, folders)
        if folder is None:
            raise
        usage = shutil.disk_usage(folder)
        raise TerrainJobError("{}\n{}\n{}\n{}".format(
            DISK_FULL_ERROR, _shown_folder(folder), usage.free, usage.total,
        )) from error


def _full_folder(error, folders):
    """The folder whose disk ran out of space in ``error``, or None."""
    existing = [folder for folder in folders if folder and os.path.isdir(folder)]
    text = str(error)
    said = (
        getattr(error, "errno", None) == errno.ENOSPC
        or "No space left" in text
        or os.strerror(errno.ENOSPC) in text
    )
    for folder in existing:
        if shutil.disk_usage(folder).free < DISK_FULL_FREE_BYTES:
            return folder
    if said and existing:
        return existing[0]
    return None


def _shown_folder(folder):
    """The folder to name: the temporary one's parent (it is removed)."""
    name = os.path.basename(os.path.normpath(folder))
    return os.path.dirname(os.path.normpath(folder)) if name.startswith("qlae_") else folder


def _check_memory(context, need):
    """Tell the context the bytes the next step needs and, when they do not
    fit in the free memory, ask it whether to go on (stops if not)."""
    tell = getattr(context, "memory_need", None)
    if tell is not None:
        tell(need)
    short, available = memory.too_much(need)
    ask = getattr(context, "confirm_memory", None)
    if short and ask is not None and not ask(need, available):
        raise TerrainJobCancelled()


def _margin_window(data, window, margin):
    """The window widened by ``margin`` cells and kept within ``data``, and
    the window relative to it (both as (r0, r1, c0, c1))."""
    r0, r1, c0, c1 = window
    rows, cols = data.shape[:2]
    outer_r0, outer_r1 = max(0, r0 - margin), min(rows, r1 + margin)
    outer_c0, outer_c1 = max(0, c0 - margin), min(cols, c1 + margin)
    inner = (r0 - outer_r0, r1 - outer_r0, c0 - outer_c0, c1 - outer_c0)
    return (outer_r0, outer_r1, outer_c0, outer_c1), inner


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
