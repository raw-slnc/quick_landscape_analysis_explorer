# -*- coding: utf-8 -*-
"""The mesh of where the chosen network DEM source has data (terrain/
coverage.py), for the preview: the shape under the preview, and keeping a
source's grid up to date, in background threads.

When a source is chosen and its interval has passed (coverage.CHECK_DAYS),
it is checked. Rhineland-Palatinate and New Zealand are updated at once
(a few seconds). France and Bavaria are asked first: only France's count,
or the date of Bavaria's list, is read, and when it has changed the window
asks in the Status section whether to fetch the new tiles (France) or the
new list (Bavaria, about 4 MB), which can take a while on a slow line."""

import json
import os

from qgis.core import (
    QgsCoordinateReferenceSystem,
    QgsCoordinateTransform,
    QgsCsException,
    QgsGeometry,
    QgsProject,
    QgsRectangle,
)
from qgis.PyQt.QtCore import QThread, pyqtSignal

from .terrain import coverage, sources, world_country

# The region a source is offered for, drawn as an outline beside the mesh
# (the country files' "outline"): German states from BKG's VG2500
# (terrain/data/regions/, made by tools/build_regions.py), countries from
# QGIS's world map, with only the parts in the source's own area (mainland
# France, the main islands of New Zealand).
_REGIONS_DIR = os.path.join(
    os.path.dirname(__file__), "terrain", "data", "regions"
)
REGION_KEYS = tuple(
    key for key, source in sources.SOURCES.items() if source.get("outline")
)

# Update threads still running when their window went away.
_RUNNING = set()


class _Job(QThread):
    """``kind``: "check" (France: count the new tiles; others: update) or
    "update" (fetch and take in the new tiles)."""

    changed = pyqtSignal(str, int)  # key, tiles taken in (-1: not known)
    found_new = pyqtSignal(str, int)  # key, new tiles (France)
    failed = pyqtSignal(str)

    def __init__(self, key, kind, parent=None):
        super().__init__(parent)
        self.key = key
        self.kind = kind

    def run(self):
        try:
            if self.kind == "check" and self.key in coverage.ASK_FIRST:
                # France: the count of new tiles; Bavaria: whether its list
                # is newer (its size). Fetched only when the user says so.
                news = (
                    coverage.ign_new_tiles()
                    if self.key == "ign_mnt"
                    else coverage.by_list_newer()
                )
                if news is None:
                    return
                coverage.mark_checked(self.key)
                if news:
                    self.found_new.emit(self.key, news)
                return
            news = coverage.ign_new_tiles() if self.key == "ign_mnt" else -1
            if coverage.update(self.key):
                self.changed.emit(self.key, news if news is not None else -1)
            coverage.mark_checked(self.key)
        # no network, a changed service: the grid stays
        except Exception:  # noqa: BLE001
            if self.kind == "update":
                self.failed.emit(self.key)


class CoverageView:
    def __init__(self, owner, on_changed, on_new_tiles, on_failed):
        self._owner = owner
        self._on_changed = on_changed
        self._on_new_tiles = on_new_tiles
        self._on_failed = on_failed
        self._grids = {}
        self._threads = []

    def shape(self, key, canvas):
        """(QgsGeometry, CRS) of the coverage under the preview, or None."""
        extent = canvas.extent()
        crs = canvas.mapSettings().destinationCrs()
        if extent.isEmpty() or not crs.isValid():
            return None
        if key in coverage.WHOLE_COUNTRY:
            # The whole country has data: its outline (the region's own file
            # when there is one, else QGIS's world map).
            region = self.region(key)
            if region is not None:
                return region
            if key not in self._grids:
                self._grids[key] = world_country.country_wkt(
                    coverage.WHOLE_COUNTRY[key]
                )
            wkt = self._grids[key]
            return (
                (
                    QgsGeometry.fromWkt(wkt),
                    QgsCoordinateReferenceSystem("EPSG:4326"),
                )
                if wkt
                else None
            )
        if key not in self._grids:
            self._grids[key] = coverage.load(key)
        grid = self._grids[key]
        if grid is None:
            return None
        source_crs = QgsCoordinateReferenceSystem("EPSG:{}".format(grid.epsg))
        try:
            box = QgsCoordinateTransform(
                crs, source_crs, QgsProject.instance().transformContext()
            ).transformBoundingBox(extent)
        except QgsCsException:
            return None
        # One tile of margin, so the mesh reaches past the preview's edges.
        wkt = grid.shape_wkt(
            box.xMinimum() - grid.cell_w,
            box.yMinimum() - grid.cell_h,
            box.xMaximum() + grid.cell_w,
            box.yMaximum() + grid.cell_h,
        )
        return (QgsGeometry.fromWkt(wkt), source_crs) if wkt else None

    def region(self, key):
        """(QgsGeometry, CRS) of the source's region, or None."""
        cache_key = "region:" + key
        if cache_key not in self._grids:
            self._grids[cache_key] = _region(key)
        return self._grids[cache_key]

    def check_if_due(self, key):
        """Check the source when its interval has passed."""
        if key not in coverage.GRIDS or self._busy(key):
            return
        try:
            if not coverage.due(key):
                return
        except OSError:
            return
        self._start(key, "check")

    def update(self, key):
        """Fetch the new tiles (France, after the user said yes)."""
        if not self._busy(key):
            self._start(key, "update")

    def _busy(self, key):
        return any(
            thread.key == key and thread.isRunning()
            for thread in self._threads
        )

    def _start(self, key, kind):
        thread = _Job(key, kind, self._owner)
        thread.changed.connect(self._changed)
        thread.found_new.connect(self._on_new_tiles)
        thread.failed.connect(self._on_failed)
        thread.finished.connect(
            lambda t=thread: (
                self._threads.remove(t) if t in self._threads else None
            )
        )
        self._threads.append(thread)
        thread.start()

    def forget(self):
        ('Read the grids again (an analysis may have brought them up to '
         'date).')
        self._grids = {
            key: value
            for key, value in self._grids.items()
            if key.startswith("region:")
        }

    def _changed(self, key, taken):
        self._grids.pop(key, None)
        self._on_changed(key, taken)

    def cleanup(self):
        """The window is closing: running jobs finish on their own."""
        for thread in list(self._threads):
            for signal in (thread.changed, thread.found_new, thread.failed):
                try:
                    signal.disconnect()
                except TypeError:
                    pass
            if thread.isRunning():
                thread.setParent(None)
                _RUNNING.add(thread)
                thread.finished.connect(lambda t=thread: _RUNNING.discard(t))
        self._threads = []


def _region(key):
    outline = sources.get(key).get("outline")
    if not outline:
        return None
    if outline[0] == "file":
        try:
            with open(
                os.path.join(_REGIONS_DIR, key + ".json"), encoding="utf-8"
            ) as handle:
                data = json.load(handle)
        except (OSError, ValueError):
            return None
        return QgsGeometry.fromWkt(data["wkt"]), QgsCoordinateReferenceSystem(
            "EPSG:{}".format(data["epsg"])
        )
    _kind, code, (xmin, ymin, xmax, ymax) = outline
    wkt = world_country.country_wkt(code)
    if not wkt:
        return None
    area = QgsGeometry.fromWkt(wkt)
    # Only the parts within the source's own area (not overseas parts).
    parts = [
        part
        for part in area.asGeometryCollection()
        if part.boundingBox().intersects(QgsRectangle(xmin, ymin, xmax, ymax))
    ]
    if not parts:
        return None
    return QgsGeometry.collectGeometry(parts), QgsCoordinateReferenceSystem(
        "EPSG:4326"
    )


def country_by_outline(lon, lat):
    """The ISO code of a country whose own region outline (a file in
    terrain/data/regions/, finer than QGIS's world map) holds the WGS84
    point, or None."""
    from qgis.core import QgsPointXY

    point = QgsGeometry.fromPointXY(QgsPointXY(lon, lat))
    for key, source in sources.SOURCES.items():
        if source.get("outline") != ("file",) or not source.get("country"):
            continue
        region = _region_cached(key)
        if region is None:
            continue
        area, crs = region
        try:
            local = QgsGeometry(point)
            local.transform(
                QgsCoordinateTransform(
                    QgsCoordinateReferenceSystem("EPSG:4326"),
                    crs,
                    QgsProject.instance().transformContext(),
                )
            )
        except QgsCsException:
            continue
        if area.contains(local):
            return source["country"]
    return None


_REGION_CACHE = {}


def _region_cached(key):
    if key not in _REGION_CACHE:
        _REGION_CACHE[key] = _region(key)
    return _REGION_CACHE[key]
