# -*- coding: utf-8 -*-
"""Preview copies of XYZ tile layers that use the export's zoom level.

QGIS picks the zoom level of a tile layer from how fine the drawing is: the
map canvas draws at screen resolution, the GeoPDF and image exports at the
export dpi, so at the same scale the two can show different maps (e.g. the
GSI map with or without its symbols). Analysis management previews the
output, so its tile layers are drawn from copies fixed to the zoom level the
export will use; the canvas scales those tiles down. Exports use the project
layers themselves.

QGIS itself decides the zoom level: a small square at the middle of the
area is drawn at the export resolution, once with the layer and once with
copies fixed to nearby zoom levels, and the copy that draws the same picture
gives the level. (Working it out from the scale alone was off by one level
near the switch-over scales: with reprojection QGIS derives the resolution
from the projected cells.)

The test squares are drawn in the background (the tiles may have to be
fetched first): the preview draws the layer itself meanwhile and switches to
the copy once the level is known (ExportZoomFinder).
"""

import re

from qgis.core import (
    QgsCoordinateReferenceSystem,
    QgsCoordinateTransform,
    QgsMapLayerStyle,
    QgsMapRendererParallelJob,
    QgsMapSettings,
    QgsProject,
    QgsRasterLayer,
    QgsRectangle,
)
from qgis.PyQt.QtCore import QObject, QSize, pyqtSignal

_PROBE_PIXELS = 256

_WEB_MERCATOR = QgsCoordinateReferenceSystem("EPSG:3857")
_WEB_MERCATOR_WIDTH = 2 * 20037508.342789244
_TILE_SIZE = 256


def is_xyz_layer(layer):
    return (
        isinstance(layer, QgsRasterLayer)
        and layer.providerType() == "wms"
        and "type=xyz" in layer.source()
    )


def _param(source, key):
    match = re.search(r"(?:^|&){}=([^&]*)".format(re.escape(key)), source)
    return match.group(1) if match else None


def _estimated_zoom(extent, crs, pixels, zmin, zmax):
    """A first guess from the scale (QGIS picks the nearest resolution)."""

    to_tiles = QgsCoordinateTransform(crs, _WEB_MERCATOR, QgsProject.instance())
    resolution = to_tiles.transformBoundingBox(extent).width() / pixels
    return min(
        range(zmin, zmax + 1),
        key=lambda zoom: abs(_WEB_MERCATOR_WIDTH / (_TILE_SIZE * 2 ** zoom) - resolution),
    )


def _probe_job(layer, probe, crs, dpi):
    """A background drawing of the test square (started, not waited for)."""

    settings = QgsMapSettings()
    settings.setLayers([layer])
    settings.setDestinationCrs(crs)
    settings.setTransformContext(QgsProject.instance().transformContext())
    settings.setOutputSize(QSize(_PROBE_PIXELS, _PROBE_PIXELS))
    settings.setOutputDpi(dpi)
    settings.setExtent(probe)
    job = QgsMapRendererParallelJob(settings)
    job.start()
    return job


def _difference(image_a, image_b):
    """Share of pixels that differ (0 = the same picture)."""

    if image_a.size() != image_b.size():
        return 1.0
    differing = 0
    step = 4  # every 4th pixel each way is plenty to tell maps apart
    total = 0
    for y in range(0, image_a.height(), step):
        for x in range(0, image_a.width(), step):
            total += 1
            if image_a.pixel(x, y) != image_b.pixel(x, y):
                differing += 1
    return differing / total if total else 1.0


class ExportZoomFinder(QObject):
    """Finds the zoom level the export of an area will use, in the background.

    ``found(zoom)`` is emitted once (``None`` when it cannot be told). The
    test squares are drawn one after another without stopping QGIS.
    """

    found = pyqtSignal(object)

    def __init__(self, layer, extent, crs, paper_width_mm, dpi, parent=None):
        super().__init__(parent)
        self._layer = layer
        self._crs = crs
        self._dpi = dpi
        self._job = None
        self._copy = None
        self._reference = None
        self._best = None
        self._candidates = []
        self._done = False
        source = layer.source()
        try:
            if layer.crs() != _WEB_MERCATOR or extent.isEmpty() or paper_width_mm <= 0 or dpi <= 0:
                raise ValueError("not a tile layer in Web Mercator")
            zmin = int(_param(source, "zmin") or 0)
            zmax = int(_param(source, "zmax") or 18)
            pixels = paper_width_mm / 25.4 * dpi
            guess = _estimated_zoom(extent, crs, pixels, zmin, zmax)
        except Exception:  # noqa: BLE001 - any failure: draw the layer as it is
            self._candidates = None
            return
        # QGIS lands at or a little below the guess (see the module notes).
        self._candidates = list(range(min(zmax, guess + 1), max(zmin, guess - 2) - 1, -1))
        # The square has the export's metres per pixel.
        half = _PROBE_PIXELS / 2 * extent.width() / pixels
        centre = extent.center()
        self._probe = QgsRectangle(
            centre.x() - half, centre.y() - half, centre.x() + half, centre.y() + half
        )

    def start(self):
        if self._candidates is None:
            self._finish(None)
            return
        self._run(self._layer, self._reference_drawn)

    def cancel(self):
        self._done = True
        job, self._job = self._job, None
        if job is not None:
            job.cancelWithoutBlocking()

    def _run(self, layer, then):
        job = _probe_job(layer, self._probe, self._crs, self._dpi)
        self._job = job
        job.finished.connect(lambda: self._drawn(job, then))

    def _drawn(self, job, then):
        if self._done or job is not self._job:
            return
        self._job = None
        then(job.renderedImage())

    def _reference_drawn(self, image):
        self._reference = image
        self._next()

    def _next(self):
        while self._candidates:
            zoom = self._candidates.pop(0)
            copy = fixed_zoom_copy(self._layer, zoom)
            if copy is None:
                continue
            self._copy = copy
            self._run(copy, lambda image, zoom=zoom: self._candidate_drawn(zoom, image))
            return
        self._finish(self._best[1] if self._best else None)

    def _candidate_drawn(self, zoom, image):
        difference = _difference(self._reference, image)
        if self._best is None or difference < self._best[0]:
            self._best = (difference, zoom)
        if difference == 0:
            self._finish(zoom)
        else:
            self._next()

    def _finish(self, zoom):
        if self._done:
            return
        self._done = True
        self.found.emit(zoom)


def fixed_zoom_copy(layer, zoom):
    """A copy of an XYZ layer that only uses this zoom level (preview only)."""

    source = layer.source()
    for key in ("zmin", "zmax"):
        if _param(source, key) is None:
            source += "&{}={}".format(key, zoom)
        else:
            source = re.sub(
                r"(^|&){}=[^&]*".format(key), r"\g<1>{}={}".format(key, zoom), source
            )
    copy = QgsRasterLayer(source, layer.name(), "wms")
    if not copy.isValid():
        return None
    style = QgsMapLayerStyle()
    style.readFromLayer(layer)
    style.writeToLayer(copy)
    # The preview draws at any scale (see preview_scale).
    copy.setScaleBasedVisibility(False)
    return copy
