# -*- coding: utf-8 -*-
"""GeoPDF of the preview, made as a background task.

QGIS writes a GeoPDF of a print layout only in the main thread (layouts are
not thread safe; QGIS's own "Export print layout as PDF" algorithm runs
without threading), so QGIS stops until the file is written. The background
GeoPDF of "Save map as PDF" (QgsMapRendererTask) is not available in PyQGIS.

So the plugin puts the GeoPDF together the way QGIS does inside: each layer
is drawn into its own PDF with the QGIS map renderer, and GDAL's PDF driver
composes them into one GeoPDF (layer tree, opacity and blend mode of each
layer, georeferencing, and the features with their attributes). The parts
are drawn one by one in the task, so the progress can be shown as
"part n of m" while QGIS stays usable.

Labels are drawn after all layers (as in the map), each labelled layer as a
part of its own; a layer's labels are placed apart from its own labels only,
not from other layers' labels.
"""

import os
import shutil
import tempfile
import threading
# Only builds the GeoPDF composition XML for GDAL; no XML is read or parsed.
import xml.etree.ElementTree as ElementTree  # nosec B405

from qgis.PyQt.QtCore import QMarginsF, QSize, QSizeF, pyqtSignal
from qgis.PyQt.QtGui import QColor, QPageLayout, QPageSize, QPainter, QPdfWriter, QTransform
from qgis.core import (
    Qgis,
    QgsCoordinateReferenceSystem,
    QgsCoordinateTransformContext,
    QgsFeature,
    QgsFeatureRequest,
    QgsFeatureSink,
    QgsGeometry,
    QgsMapRendererCustomPainterJob,
    QgsMapSettings,
    QgsRenderedFeatureHandlerInterface,
    QgsTask,
    QgsVectorFileWriter,
    QgsVectorLayer,
)

_CompositionMode = QPainter.CompositionMode
# Blend modes a PDF can do (as QgsAbstractGeospatialPdfExporter), by the
# names GDAL's composition file uses.
_PDF_BLEND_MODES = {
    _CompositionMode.CompositionMode_SourceOver: "Normal",
    _CompositionMode.CompositionMode_Multiply: "Multiply",
    _CompositionMode.CompositionMode_Screen: "Screen",
    _CompositionMode.CompositionMode_Overlay: "Overlay",
    _CompositionMode.CompositionMode_Darken: "Darken",
    _CompositionMode.CompositionMode_Lighten: "Lighten",
    _CompositionMode.CompositionMode_ColorDodge: "ColorDodge",
    _CompositionMode.CompositionMode_ColorBurn: "ColorBurn",
    _CompositionMode.CompositionMode_HardLight: "HardLight",
    _CompositionMode.CompositionMode_SoftLight: "SoftLight",
    _CompositionMode.CompositionMode_Difference: "Difference",
    _CompositionMode.CompositionMode_Exclusion: "Exclusion",
}


class _FeatureCollector(QgsRenderedFeatureHandlerInterface):
    """Keeps the drawn features of one layer, in PDF page units.

    QGIS calls this while drawing (in the task's thread) with each feature
    and the area it covers on the page in pixels; like QGIS, the features go
    into the GeoPDF with that area as their geometry (72 units an inch,
    measured up from the bottom of the page).
    """

    def __init__(self, dpi, page_height_px):
        super().__init__()
        scale = 72.0 / dpi
        self._transform = QTransform.fromTranslate(0, page_height_px * scale).scale(scale, -scale)
        self._lock = threading.Lock()
        self.features = []

    def handleRenderedFeature(self, feature, renderedBounds, context):
        del context
        geometry = QgsGeometry(renderedBounds)
        geometry.transform(self._transform)
        geometry.convertToMultiType()
        kept = QgsFeature(feature)
        kept.setGeometry(geometry)
        with self._lock:
            self.features.append(kept)

    def usedAttributes(self, layer, context):
        del layer, context
        return {QgsFeatureRequest.ALL_ATTRIBUTES}


class _Part:
    """One PDF of the composition: a layer, or a layer's labels."""

    def __init__(self, layer, labels, path):
        self.layer = layer
        self.labels = labels
        self.path = path
        self.writer = None
        self.painter = None
        self.job = None
        self.collector = None


class GeoPdfTask(QgsTask):
    """Writes the GeoPDF in the background.

    ``layers`` are the preview's layers from top to bottom. They are cloned
    here (in the main thread): the task draws the clones, so the project's
    layers can change meanwhile. ``partStarted(n, m, name, labels)`` and
    ``composing()`` tell how far it is; ``errorMessage`` says why it failed.
    """

    partStarted = pyqtSignal(int, int, str, bool)
    composing = pyqtSignal()

    def __init__(self, path, paper_mm, dpi, layers, extent, crs, transform_context, flags):
        super().__init__("GeoPDF", QgsTask.Flag.CanCancel)
        self.path = path
        self.errorMessage = ""
        self._dpi = dpi
        self._lock = threading.Lock()
        self._currentJob = None
        self._folder = tempfile.mkdtemp(prefix="qlae_geopdf_")
        width_px = int(round(paper_mm[0] / 25.4 * dpi))
        height_px = int(round(paper_mm[1] / 25.4 * dpi))
        self._size = QSize(width_px, height_px)

        base = QgsMapSettings()
        base.setDestinationCrs(crs)
        base.setTransformContext(transform_context)
        base.setOutputSize(self._size)
        base.setOutputDpi(dpi)
        base.setExtent(extent)
        base.setFlags(flags)
        # Every part is laid over the ones below: no background (the
        # renderer would fill each part white and hide the parts below it).
        base.setBackgroundColor(QColor(0, 0, 0, 0))
        # Each layer is its own PDF and its opacity and blend mode are applied
        # when the PDFs are put together, so nothing has to be drawn as an
        # image: keep everything vector (a page-sized image at A0 and high
        # dpi can fail and leave the page empty).
        base.setFlag(Qgis.MapSettingsFlag.ForceVectorOutput, True)
        base.setFlag(Qgis.MapSettingsFlag.RenderPreviewJob, False)
        self._mapToPixel = base.mapToPixel()
        self._crs = crs

        # Clones, top to bottom, with what the composition applies.
        self._layers = []
        for layer in layers:
            clone = layer.clone()
            if clone is None:
                continue
            opacity = layer.opacity()
            mode = layer.blendMode()
            if mode in _PDF_BLEND_MODES:
                clone.setOpacity(1.0)
                clone.setBlendMode(_CompositionMode.CompositionMode_SourceOver)
            else:
                # A blend mode a PDF cannot do stays in the drawing.
                opacity, mode = 1.0, _CompositionMode.CompositionMode_SourceOver
            labelled = isinstance(clone, QgsVectorLayer) and clone.labelsEnabled()
            display = clone.displayField() if isinstance(clone, QgsVectorLayer) else ""
            self._layers.append({
                "layer": clone, "id": layer.id(), "name": layer.name(),
                "opacity": opacity, "mode": mode, "labelled": labelled,
                "display": display,
            })

        # Parts in drawing order: the layers from the bottom up, then the
        # labels (drawn above every layer, as in the map).
        self._parts = []
        self._partCount = 0
        for entry in reversed(self._layers):
            self._parts.append(_Part(entry, False, self._part_path()))
        for entry in reversed(self._layers):
            if entry["labelled"]:
                self._parts.append(_Part(entry, True, self._part_path()))
        for part in self._parts:
            self._prepare(part, base)

    def _part_path(self):
        self._partCount += 1
        return os.path.join(self._folder, "part_{}.pdf".format(self._partCount))

    def _prepare(self, part, base):
        """Writer, painter and render job of a part (main thread, as QGIS)."""

        settings = QgsMapSettings(base)
        settings.setLayers([part.layer["layer"]])
        if part.labels:
            settings.setFlag(Qgis.MapSettingsFlag.DrawLabeling, True)
            settings.setFlag(Qgis.MapSettingsFlag.SkipSymbolRendering, True)
        else:
            settings.setFlag(Qgis.MapSettingsFlag.DrawLabeling, not part.layer["labelled"])
            if isinstance(part.layer["layer"], QgsVectorLayer):
                part.collector = _FeatureCollector(self._dpi, self._size.height())
                settings.addRenderedFeatureHandler(part.collector)
        part.writer = QPdfWriter(part.path)
        part.writer.setPageOrientation(QPageLayout.Orientation.Portrait)
        part.writer.setPageSize(QPageSize(
            QSizeF(self._size.width() * 25.4 / self._dpi, self._size.height() * 25.4 / self._dpi),
            QPageSize.Unit.Millimeter,
        ))
        part.writer.setPageMargins(QMarginsF(0, 0, 0, 0))
        part.writer.setResolution(int(self._dpi))
        part.painter = QPainter(part.writer)
        part.job = QgsMapRendererCustomPainterJob(settings, part.painter)
        part.job.prepare()

    # --- task -------------------------------------------------------------

    def cancel(self):
        with self._lock:
            job = self._currentJob
        if job is not None:
            job.cancelWithoutBlocking()
        super().cancel()

    def run(self):
        try:
            return self._run()
        except Exception as error:  # noqa: BLE001 - reported to the user
            self.errorMessage = str(error)
            return False

    def _run(self):
        total = len(self._parts)
        for index, part in enumerate(self._parts, start=1):
            if self.isCanceled():
                return False
            self.partStarted.emit(index, total, part.layer["name"], part.labels)
            self.setProgress(100.0 * (index - 1) / (total + 1))
            with self._lock:
                self._currentJob = part.job
            part.job.renderPrepared()
            with self._lock:
                self._currentJob = None
            part.painter.end()
            part.painter = None
            part.writer = None
        if self.isCanceled():
            return False
        self.composing.emit()
        self.setProgress(100.0 * total / (total + 1))
        vectors = self._write_features()
        composition = os.path.join(self._folder, "composition.xml")
        ElementTree.ElementTree(self._composition(vectors)).write(
            composition, encoding="utf-8", xml_declaration=True
        )
        return self._compose(composition)

    def _write_features(self):
        """GeoPackage of each layer's drawn features: {layer id: (path, name)}."""

        written = {}
        for part in self._parts:
            if part.collector is None or not part.collector.features:
                continue
            features = part.collector.features
            path = os.path.join(self._folder, "features_{}.gpkg".format(len(written) + 1))
            options = QgsVectorFileWriter.SaveVectorOptions()
            options.driverName = "GPKG"
            options.layerName = "features"
            writer = QgsVectorFileWriter.create(
                path,
                features[0].fields(),
                features[0].geometry().wkbType(),
                QgsCoordinateReferenceSystem(),
                QgsCoordinateTransformContext(),
                options,
                QgsFeatureSink.SinkFlag.RegeneratePrimaryKey,
            )
            if writer is None or writer.hasError() != QgsVectorFileWriter.WriterError.NoError:
                # The map is still worth having without these attributes.
                continue
            for feature in features:
                writer.addFeature(feature, QgsFeatureSink.Flag.FastInsert)
            del writer
            written[part.layer["id"]] = (path, "features")
        return written

    def _composition(self, vectors):
        """GDAL's composition file, as QgsAbstractGeospatialPdfExporter."""

        page_w = self._size.width() * 72.0 / self._dpi
        page_h = self._size.height() * 72.0 / self._dpi
        root = ElementTree.Element("PDFComposition")
        metadata = ElementTree.SubElement(root, "Metadata")
        ElementTree.SubElement(metadata, "Creator").text = "Quick Landscape Analysis Explorer"

        # Layer tree: every layer drawn in the preview is on when it opens.
        tree = ElementTree.SubElement(root, "LayerTree")
        for entry in self._layers:
            ElementTree.SubElement(tree, "Layer", {
                "id": entry["id"], "name": entry["name"], "initiallyVisible": "true",
            })

        page = ElementTree.SubElement(root, "Page")
        ElementTree.SubElement(page, "DPI").text = "72"
        ElementTree.SubElement(page, "Width").text = repr(page_w)
        ElementTree.SubElement(page, "Height").text = repr(page_h)

        georeferencing = ElementTree.SubElement(page, "Georeferencing", {
            "id": "georeferenced_0", "ISO32000ExtensionFormat": "true",
        })
        srs = self._crs.authid()
        if not srs or srs.lower().startswith("user"):
            srs = self._crs.toWkt(Qgis.CrsWktVariant.PreferredGdal)
        ElementTree.SubElement(georeferencing, "SRS").text = srs
        ElementTree.SubElement(georeferencing, "BoundingBox", {
            "x1": "0", "y1": "0", "x2": repr(page_w), "y2": repr(page_h),
        })
        width, height = self._size.width(), self._size.height()
        for px, py in ((0, 0), (width, 0), (width, height), (0, height)):
            point = self._mapToPixel.toMapCoordinates(px, py)
            ElementTree.SubElement(georeferencing, "ControlPoint", {
                "x": repr(px * 72.0 / self._dpi),
                "y": repr((height - py) * 72.0 / self._dpi),
                "GeoX": repr(point.x()),
                "GeoY": repr(point.y()),
            })

        content = ElementTree.SubElement(page, "Content")
        for part in self._parts:
            switch = ElementTree.SubElement(content, "IfLayerOn", {"layerId": part.layer["id"]})
            pdf = ElementTree.SubElement(switch, "PDF", {"dataset": part.path})
            if not part.labels and (
                part.layer["opacity"] != 1.0
                or part.layer["mode"] != _CompositionMode.CompositionMode_SourceOver
            ):
                ElementTree.SubElement(pdf, "Blending", {
                    "opacity": repr(float(part.layer["opacity"])),
                    "function": _PDF_BLEND_MODES[part.layer["mode"]],
                })
        # The features (with attributes) are not drawn; they are there to be
        # identified in a PDF viewer, as in QGIS's GeoPDF.
        for entry in reversed(self._layers):
            if entry["id"] not in vectors:
                continue
            path, layer_name = vectors[entry["id"]]
            switch = ElementTree.SubElement(content, "IfLayerOn", {"layerId": entry["id"]})
            vector = ElementTree.SubElement(switch, "Vector", {
                "dataset": path, "layer": layer_name, "visible": "false",
            })
            structure = {"displayLayerName": entry["name"]}
            if entry["display"]:
                structure["fieldToDisplay"] = entry["display"]
            ElementTree.SubElement(vector, "LogicalStructure", structure)
        return root

    def _compose(self, composition):
        from osgeo import gdal

        errors = []

        def collect(_level, _number, message):
            errors.append(message)

        driver = gdal.GetDriverByName("PDF")
        if driver is None:
            self.errorMessage = "GDAL PDF driver is not available"
            return False
        gdal.PushErrorHandler(collect)
        try:
            dataset = driver.Create(
                self.path, 0, 0, 0, gdal.GDT_Unknown,
                options=["COMPOSITION_FILE={}".format(composition)],
            )
            ok = dataset is not None
            dataset = None
        finally:
            gdal.PopErrorHandler()
        if not ok:
            self.errorMessage = "\n".join(errors) or "GDAL could not write the PDF"
        return ok

    def finished(self, result):
        # Main thread: let go of the clones and the parts.
        del result
        for part in self._parts:
            if part.painter is not None and part.painter.isActive():
                part.painter.end()
            part.painter = None
            part.writer = None
            part.job = None
        for entry in self._layers:
            entry["layer"].deleteLater()
        self._layers = []
        shutil.rmtree(self._folder, ignore_errors=True)
