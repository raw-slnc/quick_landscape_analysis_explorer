# -*- coding: utf-8 -*-

import configparser
import datetime
import math
import os
import sys

from qgis.PyQt.QtCore import QEvent, QRect, QRectF, QTimer, QUrl, Qt, pyqtSignal
from qgis.PyQt.QtGui import (
    QColor,
    QDesktopServices,
    QKeySequence,
    QPainter,
    QPen,
)
from qgis.PyQt.QtWidgets import (
    QButtonGroup,
    QCheckBox,
    QComboBox,
    QDialog,
    QFormLayout,
    QFrame,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QApplication,
    QPushButton,
    QRadioButton,
    QSizePolicy,
    QSplitter,
    QShortcut,
    QStyle,
    QTextEdit,
    QToolButton,
    QVBoxLayout,
    QWidget,
)
from qgis.core import (
    Qgis,
    QgsApplication,
    QgsColorRampShader,
    QgsCoordinateTransform,
    QgsCsException,
    QgsGeometry,
    QgsLayerTreeGroup,
    QgsCoordinateReferenceSystem,
    QgsMapLayerModel,
    QgsPointXY,
    QgsProject,
    QgsRasterLayer,
    QgsRasterShader,
    QgsRectangle,
    QgsSingleBandGrayRenderer,
    QgsSingleBandPseudoColorRenderer,
)
from qgis.gui import (
    QgsMapCanvas,
    QgsMapTool,
    QgsMapToolIdentify,
    QgsMapToolPan,
    QgsRubberBand,
)

from .analyses.terrain import TerrainJobWorker
from .core import packages
from .i18n import ENGLISH, JAPANESE, is_japanese, set_language, tr
from .plugin import PLUGIN_NAME
from .terrain.styles import SLOPE_COLORS


class PreviewAnalysisGuide(QWidget):
    """Screen-fixed guide for the central three-fifths analysis-safe area."""

    _FRAME_FRACTION = 3.0 / 5.0
    _LINE_WIDTH_POINTS = 0.3
    _UNDERLAY_WIDTH_POINTS = 0.7
    _UNDERLAY_COLOR = QColor(255, 255, 255)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WidgetAttribute.WA_NoSystemBackground, True)
        self.hide()

    def paintEvent(self, event):
        del event
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        dpi = (self.logicalDpiX() + self.logicalDpiY()) / 2.0
        line_width = self._LINE_WIDTH_POINTS * dpi / 72.0
        underlay_width = self._UNDERLAY_WIDTH_POINTS * dpi / 72.0
        painter.setBrush(Qt.BrushStyle.NoBrush)

        frame_width = self.width() * self._FRAME_FRACTION
        frame_height = self.height() * self._FRAME_FRACTION
        half_line = line_width / 2.0
        frame = QRectF(
            (self.width() - frame_width) / 2.0 + half_line,
            (self.height() - frame_height) / 2.0 + half_line,
            max(0.0, frame_width - line_width),
            max(0.0, frame_height - line_width),
        )

        # White underlay first, then the bluish grey line on top of it so the
        # frame stays visible over dark or busy backgrounds.
        underlay_pen = QPen(self._UNDERLAY_COLOR)
        underlay_pen.setWidthF(underlay_width)
        underlay_pen.setCosmetic(True)
        underlay_pen.setJoinStyle(Qt.PenJoinStyle.MiterJoin)
        painter.setPen(underlay_pen)
        painter.drawRect(frame)

        pen = QPen(QColor(49, 49, 126))
        pen.setWidthF(line_width)
        pen.setCosmetic(True)
        pen.setJoinStyle(Qt.PenJoinStyle.MiterJoin)
        painter.setPen(pen)
        painter.drawRect(frame)


class ASeriesPortraitPreview(QWidget):
    """An A-series map area whose portrait/landscape ratio can be switched."""

    canvasResizeStarted = pyqtSignal(object)
    canvasResized = pyqtSignal()
    _PORTRAIT_RATIO = 1.0 / math.sqrt(2.0)
    _LANDSCAPE_RATIO = math.sqrt(2.0)
    _MIN_WIDTH = 220
    _MIN_HEIGHT = 300

    def __init__(self, parent=None):
        super().__init__(parent)
        self._landscape = False
        self.setMinimumSize(self._MIN_WIDTH, self._MIN_HEIGHT)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.canvas = QgsMapCanvas(self)
        self.canvas.setCanvasColor(QColor("#ffffff"))
        self.canvas.enableAntiAliasing(True)
        self.canvas.setCachingEnabled(True)
        self.canvas.setParallelRenderingEnabled(True)
        self.canvas.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.canvas.setStyleSheet("border: 1px solid #777777;")
        # Apply this after the style sheet, which otherwise resets the
        # QGraphicsView viewport's background-fill property.
        self.canvas.viewport().setAutoFillBackground(True)
        self.analysisGuide = PreviewAnalysisGuide(self)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._fit_canvas()

    def aspectRatio(self):
        return self._LANDSCAPE_RATIO if self._landscape else self._PORTRAIT_RATIO

    def isLandscape(self):
        return self._landscape

    def setLandscape(self, landscape):
        landscape = bool(landscape)
        if self._landscape == landscape:
            return
        self._landscape = landscape
        self._fit_canvas()

    def _fit_canvas(self):
        extent = QgsRectangle(self.canvas.extent())
        available_width = self.width()
        available_height = self.height()
        ratio = self.aspectRatio()
        target_width = min(
            available_width,
            int(round(available_height * ratio)),
        )
        target_height = int(round(target_width / ratio))
        if target_height > available_height:
            target_height = available_height
            target_width = int(round(target_height * ratio))
        target_geometry = QRect(
            (available_width - target_width) // 2,
            # When an intermediate window width temporarily constrains the A
            # frame, keep the map and its controls stacked at the top. Any
            # unavoidable spare space belongs below, never above and below.
            0,
            target_width,
            target_height,
        )
        if self.canvas.geometry() != target_geometry:
            self.canvasResizeStarted.emit(extent)
            self.canvas.setGeometry(target_geometry)
            self.canvasResized.emit()
        self.analysisGuide.setGeometry(target_geometry)
        self.analysisGuide.raise_()

    def setAnalysisGuideVisible(self, visible):
        self.analysisGuide.setVisible(bool(visible))
        if visible:
            self.analysisGuide.raise_()


class LayerTreeOrderComboBox(QComboBox):
    """A layer combo whose items follow the layer-panel order."""

    layerChanged = pyqtSignal(object)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._allowEmptyLayer = False
        self.setSizeAdjustPolicy(
            QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon
        )
        self.setMinimumContentsLength(12)
        self.currentIndexChanged.connect(self._emit_layer_changed)

    def setAllowEmptyLayer(self, allow):
        self._allowEmptyLayer = bool(allow)

    def setLayers(self, layers):
        current_id = self.currentData()
        self.blockSignals(True)
        self.clear()
        if self._allowEmptyLayer:
            self.addItem("", None)
        for layer in layers:
            self.addItem(QgsMapLayerModel.iconForLayer(layer), layer.name(), layer.id())
        index = self.findData(current_id)
        if index >= 0:
            self.setCurrentIndex(index)
        elif self._allowEmptyLayer:
            self.setCurrentIndex(0)
        self.blockSignals(False)

    def currentLayer(self):
        layer_id = self.currentData()
        return QgsProject.instance().mapLayer(layer_id) if layer_id else None

    def setLayer(self, layer):
        layer_id = layer.id() if layer is not None else None
        index = self.findData(layer_id)
        if index >= 0:
            self.setCurrentIndex(index)
        elif self._allowEmptyLayer:
            self.setCurrentIndex(0)

    def _emit_layer_changed(self, *args):
        del args
        self.layerChanged.emit(self.currentLayer())


class PreviewPolygonSelectionTool(QgsMapToolIdentify):
    """QGIS-style click and rectangle selection for the preview canvas."""

    selectionRequested = pyqtSignal(object, object)

    def __init__(self, canvas):
        super().__init__(canvas)
        self._layer = None
        self._pressPosition = None
        self._dragging = False
        self._selectionRubber = QgsRubberBand(
            canvas, Qgis.GeometryType.Polygon
        )
        self._selectionRubber.setStrokeColor(QColor(40, 115, 220, 230))
        self._selectionRubber.setFillColor(QColor(40, 115, 220, 40))
        self._selectionRubber.setWidth(1)

    def setLayer(self, layer):
        self._layer = layer

    def flags(self):
        # QgsMapToolIdentify enables AllowZoomRect. QgsMapCanvas then consumes
        # Shift+mouse gestures for zooming before this selection tool sees
        # them, which made Shift-click require a second click. Selection mode
        # owns click and rectangle gestures, so it must not advertise that
        # zoom capability.
        return QgsMapTool.Flags()

    def canvasPressEvent(self, event):
        if event.button() != Qt.MouseButton.LeftButton:
            return
        self._pressPosition = event.pos()
        self._dragging = False
        self._selectionRubber.reset(Qgis.GeometryType.Polygon)

    def canvasMoveEvent(self, event):
        if self._pressPosition is None:
            return
        distance = (event.pos() - self._pressPosition).manhattanLength()
        if distance < QApplication.startDragDistance():
            return
        self._dragging = True
        rectangle = QRect(self._pressPosition, event.pos()).normalized()
        self._selectionRubber.setToCanvasRectangle(rectangle)

    def canvasReleaseEvent(self, event):
        if (
            event.button() != Qt.MouseButton.LeftButton
            or self._pressPosition is None
        ):
            return
        try:
            if self._layer is None:
                return
            if self._dragging:
                feature_ids = self._rectangle_feature_ids(event.pos())
            else:
                feature_ids = self._click_feature_ids(event.pos())
            self.selectionRequested.emit(feature_ids, event.modifiers())
        finally:
            self._pressPosition = None
            self._dragging = False
            self._selectionRubber.reset(Qgis.GeometryType.Polygon)

    def deactivate(self):
        self._pressPosition = None
        self._dragging = False
        self._selectionRubber.reset(Qgis.GeometryType.Polygon)
        super().deactivate()

    def _click_feature_ids(self, position):
        results = self.identify(
            position.x(),
            position.y(),
            [self._layer],
            QgsMapToolIdentify.IdentifyMode.TopDownStopAtFirst,
        )
        for result in results:
            if result.mLayer is self._layer:
                return [result.mFeature.id()]
        return []

    def _rectangle_feature_ids(self, position):
        rectangle = QRect(self._pressPosition, position).normalized()
        top_left = self.toMapCoordinates(rectangle.topLeft())
        bottom_right = self.toMapCoordinates(rectangle.bottomRight())
        map_rectangle = QgsRectangle(
            min(top_left.x(), bottom_right.x()),
            min(top_left.y(), bottom_right.y()),
            max(top_left.x(), bottom_right.x()),
            max(top_left.y(), bottom_right.y()),
        )
        geometry = QgsGeometry.fromRect(map_rectangle)
        results = self.identify(
            geometry,
            QgsMapToolIdentify.IdentifyMode.TopDownAll,
            [self._layer],
            QgsMapToolIdentify.Type.VectorLayer,
        )
        return list(dict.fromkeys(
            result.mFeature.id()
            for result in results
            if result.mLayer is self._layer
        ))


class LandscapeExplorerDialog(QDialog):
    languageChanged = pyqtSignal()

    def __init__(self, iface, parent=None):
        super().__init__(parent)
        self.iface = iface
        self.mainCanvas = iface.mapCanvas()
        self._previewAoiLayer = None
        self._previewOverlayLayer = None
        self._previewOverlaySourceId = None
        self._overlayOpacityValues = (100, 80, 60, 40, 20, 0)
        self._overlayOpacityIndex = 0
        self._previewBaseLayer = None
        self._previewBaseSourceId = None
        self._baseOpacityIndex = 0
        self._aoiChoicesInitialized = False
        self._previewInitialized = False
        self._previewLandscape = False
        self._previewVerticalChrome = None
        self._previewPanelMinimumWidth = None
        self._previewStackCompact = False
        self._compactPreviewHeight = None
        self._showLinuxRestoreControl = sys.platform.startswith("linux")
        self._pendingPreviewView = None
        self._pendingInspectionRefresh = False
        self._terrainWorker = None
        self._overwritePackagePath = None
        self._resizePreviewExtent = None
        self._stablePreviewExtent = None
        self._adjustingWindowWidth = False
        self._applyingASeriesLayout = False
        self._windowStateSettling = False
        self._finishingWindowInteraction = False
        self._previewFrozenForWindowChange = False
        self._previewResizeTimer = QTimer(self)
        self._previewResizeTimer.setSingleShot(True)
        self._previewResizeTimer.setInterval(140)
        self._previewResizeTimer.timeout.connect(
            lambda: self._restore_resized_preview_extent(final=True)
        )
        self._aSeriesLayoutTimer = QTimer(self)
        self._aSeriesLayoutTimer.setSingleShot(True)
        # A zero-delay pass runs after the current native resize/state event
        # queue has settled. This keeps normal-window constraints out of a
        # maximize/full-screen transition whose Resize event may arrive first.
        self._aSeriesLayoutTimer.setInterval(0)
        self._aSeriesLayoutTimer.timeout.connect(self._apply_a_series_layout)
        self._windowInteractionTimer = QTimer(self)
        self._windowInteractionTimer.setSingleShot(True)
        self._windowInteractionTimer.setInterval(140)
        self._windowInteractionTimer.timeout.connect(
            self._finish_window_interaction
        )
        self._windowStateTimer = QTimer(self)
        self._windowStateTimer.setSingleShot(True)
        self._windowStateTimer.setInterval(250)
        self._windowStateTimer.timeout.connect(
            self._finish_window_state_transition
        )

        self.setWindowTitle(PLUGIN_NAME)
        self.setWindowFlags(
            Qt.WindowType.Window
            | Qt.WindowType.WindowMinMaxButtonsHint
            | Qt.WindowType.WindowCloseButtonHint
        )
        # Use a roomy hidden staging width so Qt can resolve the right-panel
        # reservation before the A-series left width is calculated. The final
        # startup width is applied below, still before the first show().
        self.resize(1200, 590)

        main = QVBoxLayout(self)
        self.columnsSplitter = QSplitter(Qt.Orientation.Horizontal, self)
        self.columnsSplitter.setChildrenCollapsible(False)
        self.columnsSplitter.setHandleWidth(main.spacing())
        self.previewPanel = self._build_preview_panel()
        self.controlPanel = self._build_control_panel()
        self.controlPanel.setMinimumWidth(600)
        self.columnsSplitter.addWidget(self.previewPanel)
        self.columnsSplitter.addWidget(self.controlPanel)
        self.columnsSplitter.setStretchFactor(0, 0)
        self.columnsSplitter.setStretchFactor(1, 1)
        self.columnsSplitter.handle(1).setEnabled(False)
        main.addWidget(self.columnsSplitter, 1)

        self.demSourceCombo.currentIndexChanged.connect(self._dem_source_changed)
        self.runButton.clicked.connect(self._run_analysis)
        self.loadAnalysisButton.clicked.connect(self._load_selected_analysis)
        self.closeButton.clicked.connect(self._close_and_clear)
        self.languageButton.clicked.connect(self._switch_language)
        self.aoiLayerCombo.layerChanged.connect(self._aoi_layer_changed)
        self.selectedOnlyCheck.toggled.connect(self._refresh_preview_polygon)
        self.polygonAreaRadio.toggled.connect(self._area_mode_changed)
        self.mapExtentAreaRadio.toggled.connect(self._area_mode_changed)
        self.overlayLayerCombo.layerChanged.connect(self._update_preview_layers)
        self.baseMapCombo.layerChanged.connect(self._update_preview_layers)
        # Only explicit user choices are stored in the project, so automatic
        # defaults do not mark the project as modified.
        self.overlayLayerCombo.activated.connect(self._store_preview_layer_choices)
        self.baseMapCombo.activated.connect(self._store_preview_layer_choices)
        self.overlayOpacityButton.clicked.connect(self._cycle_overlay_opacity)
        self.baseOpacityButton.clicked.connect(self._cycle_base_opacity)
        self.previewFrame.canvasResizeStarted.connect(
            self._queue_preview_extent_restore
        )
        self.previewFrame.canvasResized.connect(self._queue_a_series_layout)
        self.previewFrame.canvasResized.connect(self._update_preview_status)
        self.previewCanvas.extentsChanged.connect(self._update_preview_status)
        self.previewCanvas.extentsChanged.connect(self._preview_extent_changed)
        self.mainViewButton.clicked.connect(self._sync_preview_from_main)
        QgsProject.instance().layersAdded.connect(self._refresh_preview_layer_choices)
        QgsProject.instance().layersRemoved.connect(self._refresh_preview_layer_choices)
        QgsProject.instance().layerTreeRoot().layerOrderChanged.connect(
            self._refresh_preview_layer_choices
        )
        QgsProject.instance().readProject.connect(
            self._apply_stored_preview_layer_choices
        )
        self._dem_source_changed()
        self._refresh_saved_analyses()
        self._refresh_preview_layer_choices()
        self._aoi_layer_changed(self.aoiLayerCombo.currentLayer())
        self._area_mode_changed()
        self._apply_startup_window_width()
        QTimer.singleShot(0, self._finish_initial_layout)

    def _build_preview_panel(self):
        panel = QWidget(self)
        panel.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Expanding)
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(0, 0, 0, 0)
        self.previewPanelLayout = layout

        preview_box = QGroupBox(tr("Map preview", "地図プレビュー"), panel)
        preview_layout = QVBoxLayout(preview_box)
        self.previewBox = preview_box
        self.previewBoxLayout = preview_layout

        self.previewFrame = ASeriesPortraitPreview(preview_box)
        self.previewCanvas = self.previewFrame.canvas
        self.previewPanTool = QgsMapToolPan(self.previewCanvas)
        self.previewSelectTool = PreviewPolygonSelectionTool(self.previewCanvas)
        self.previewSelectTool.selectionRequested.connect(
            self._select_preview_polygons
        )
        self.previewCanvas.setMapTool(self.previewPanTool)
        preview_layout.addWidget(self.previewFrame, 1)

        self.previewRubber = QgsRubberBand(self.previewCanvas, Qgis.GeometryType.Polygon)
        self.previewRubber.setStrokeColor(QColor(225, 82, 30, 230))
        self.previewRubber.setFillColor(QColor(255, 151, 54, 60))
        self.previewRubber.setWidth(2)

        status_row = QWidget(preview_box)
        status_layout = QHBoxLayout(status_row)
        status_layout.setContentsMargins(0, 0, 0, 0)
        status_layout.setSpacing(4)
        self.previewStatusLabel = QLabel(status_row)
        self.previewStatusLabel.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.previewStatusLabel.setStyleSheet("color: palette(text); font-size: 9pt;")
        self.previewOrientationButton = QToolButton(status_row)
        self.previewOrientationButton.setAutoRaise(True)
        self.previewOrientationButton.setFixedSize(30, 26)
        self.previewOrientationButton.clicked.connect(
            self._toggle_preview_orientation
        )
        self._update_preview_orientation_button()
        self.previewToolPanel = self._build_preview_tool_panel(status_row)
        self.mainViewButton = QToolButton(status_row)
        self.mainViewButton.setIcon(
            self.style().standardIcon(QStyle.StandardPixmap.SP_BrowserReload)
        )
        self.mainViewButton.setAutoRaise(True)
        self.mainViewButton.setFixedSize(26, 26)
        self.mainViewButton.setToolTip(
            tr(
                "Get the centre, CRS and scale from the QGIS main map.",
                "QGISメイン地図から中心・座標系・縮尺を取得します。",
            )
        )
        status_layout.addWidget(self.previewStatusLabel, 1)
        status_layout.addWidget(self.previewOrientationButton)
        status_layout.addWidget(
            self.previewToolPanel, 0, Qt.AlignmentFlag.AlignRight
        )
        status_layout.addWidget(self.mainViewButton)
        preview_layout.addWidget(status_row)

        layout.addWidget(preview_box, 1)
        self.exportBox = self._build_export_box()
        layout.addWidget(self.exportBox)
        layout.addStretch(0)
        return panel

    def _build_preview_tool_panel(self, parent):
        panel = QFrame(parent)
        panel.setObjectName("previewToolPanel")
        panel.setStyleSheet(
            "QFrame#previewToolPanel {"
            "background-color: palette(base);"
            "border: 1px solid palette(mid);"
            "border-radius: 3px;"
            "}"
        )
        tools = QHBoxLayout(panel)
        tools.setContentsMargins(2, 2, 2, 2)
        tools.setSpacing(1)

        self.mapNavigationButton = QToolButton(panel)
        self.mapNavigationButton.setIcon(
            QgsApplication.getThemeIcon("/mActionPan.svg")
        )
        self.mapNavigationButton.setCheckable(True)
        self.mapNavigationButton.setChecked(True)
        self.mapNavigationButton.setToolTip(
            tr("Map navigation (1)", "地図操作（1）")
        )

        self.polygonSelectButton = QToolButton(panel)
        self.polygonSelectButton.setIcon(
            QgsApplication.getThemeIcon("/mActionSelect.svg")
        )
        self.polygonSelectButton.setCheckable(True)
        self.polygonSelectButton.setToolTip(
            tr(
                "Select polygon (2). Drag selects a range; Shift adds; Ctrl selects or deselects.",
                "ポリゴン選択（2）。ドラッグで範囲選択、Shiftで追加、Ctrlで選択または解除します。",
            )
        )

        self.clearSelectionButton = QToolButton(panel)
        self.clearSelectionButton.setIcon(
            QgsApplication.getThemeIcon("/mActionDeselectAll.svg")
        )
        self.clearSelectionButton.setToolTip(
            tr("Clear polygon selection", "ポリゴン選択を解除")
        )

        for button in (
            self.mapNavigationButton,
            self.polygonSelectButton,
            self.clearSelectionButton,
        ):
            button.setAutoRaise(True)
            button.setFixedSize(26, 26)
            tools.addWidget(button)

        self.previewToolButtonGroup = QButtonGroup(panel)
        self.previewToolButtonGroup.setExclusive(True)
        self.previewToolButtonGroup.addButton(self.mapNavigationButton)
        self.previewToolButtonGroup.addButton(self.polygonSelectButton)

        self.mapNavigationButton.clicked.connect(self._activate_map_navigation)
        self.polygonSelectButton.clicked.connect(self._activate_polygon_selection)
        self.clearSelectionButton.clicked.connect(self._clear_polygon_selection)

        self.mapNavigationShortcut = QShortcut(QKeySequence("1"), self)
        self.mapNavigationShortcut.setContext(
            Qt.ShortcutContext.WidgetWithChildrenShortcut
        )
        self.mapNavigationShortcut.activated.connect(self._activate_map_navigation)
        self.polygonSelectShortcut = QShortcut(QKeySequence("2"), self)
        self.polygonSelectShortcut.setContext(
            Qt.ShortcutContext.WidgetWithChildrenShortcut
        )
        self.polygonSelectShortcut.activated.connect(
            self._activate_polygon_selection
        )

        panel.adjustSize()
        return panel

    def _build_export_box(self):
        box = QGroupBox(tr("Export (planned)", "エクスポート（予定）"))
        grid = QGridLayout(box)
        self.exportPdfButton = QPushButton(tr("PDF", "PDF"), box)
        self.exportFootprintPdfButton = QPushButton(
            tr("Footprint GeoPDF", "範囲形状GeoPDF"), box
        )
        self.exportLayersButton = QPushButton(tr("Layers", "レイヤー"), box)
        self.exportImageButton = QPushButton(tr("Image", "画像"), box)
        for index, button in enumerate((
            self.exportPdfButton,
            self.exportFootprintPdfButton,
            self.exportLayersButton,
            self.exportImageButton,
        )):
            button.setEnabled(False)
            button.setToolTip(
                tr(
                    "The export action is not connected in this UI stage.",
                    "このUI段階ではエクスポート処理へ接続していません。",
                )
            )
            grid.addWidget(button, index // 2, index % 2)
        return box

    def _build_control_panel(self):
        panel = QWidget(self)
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self._build_profile_box())
        layout.addWidget(self._build_area_box())
        terrain_row = QHBoxLayout()
        dem_column = QVBoxLayout()
        dem_column.addWidget(self._build_data_box(), 1)
        dem_column.addWidget(self._build_analysis_name_box())
        terrain_row.addLayout(dem_column, 1)
        analysis_column = QVBoxLayout()
        analysis_column.addWidget(self._build_analysis_box())
        analysis_column.addWidget(self._build_run_box())
        terrain_row.addLayout(analysis_column, 1)
        layout.addLayout(terrain_row)
        layout.addWidget(self._build_result_box(), 1)
        layout.addLayout(self._build_preview_layer_area(panel))
        layout.addLayout(self._build_buttons())
        return panel

    def _build_preview_layer_area(self, parent):
        """Stack the preview-layer row and the saved-analyses row."""

        area = QVBoxLayout()
        box = QGroupBox(tr("Preview layers", "プレビューレイヤー"), parent)
        row_layout = QHBoxLayout(box)

        overlay_row = QWidget(box)
        overlay_layout = QHBoxLayout(overlay_row)
        overlay_layout.setContentsMargins(0, 0, 0, 0)
        overlay_layout.setSpacing(4)
        self.overlayLayerCombo = LayerTreeOrderComboBox(box)
        self.overlayLayerCombo.setAllowEmptyLayer(True)
        self.overlayOpacityButton = QToolButton(overlay_row)
        self.overlayOpacityButton.setText("100%")
        overlay_height = self.overlayLayerCombo.sizeHint().height()
        self.overlayOpacityButton.setFixedHeight(overlay_height)
        self.overlayOpacityButton.setMinimumWidth(48)
        self.overlayOpacityButton.setToolTip(
            tr(
                "Cycle preview opacity: 100, 80, 60, 40, 20, 0 percent.",
                "プレビュー不透明度を100、80、60、40、20、0%の順で切り替えます。",
            )
        )
        overlay_layout.addWidget(self.overlayLayerCombo, 1)
        overlay_layout.addWidget(self.overlayOpacityButton)
        row_layout.addWidget(QLabel(tr("Overlay", "Overlay"), box))
        row_layout.addWidget(overlay_row, 1)
        row_layout.addSpacing(8)

        base_map_row = QWidget(box)
        base_map_layout = QHBoxLayout(base_map_row)
        base_map_layout.setContentsMargins(0, 0, 0, 0)
        base_map_layout.setSpacing(4)
        self.baseMapCombo = LayerTreeOrderComboBox(box)
        self.baseMapCombo.setAllowEmptyLayer(True)
        self.baseOpacityButton = QToolButton(base_map_row)
        self.baseOpacityButton.setText("100%")
        self.baseOpacityButton.setFixedHeight(overlay_height)
        self.baseOpacityButton.setMinimumWidth(48)
        self.baseOpacityButton.setToolTip(self.overlayOpacityButton.toolTip())
        base_map_layout.addWidget(self.baseMapCombo, 1)
        base_map_layout.addWidget(self.baseOpacityButton)
        row_layout.addWidget(QLabel(tr("Base map", "Base map"), box))
        row_layout.addWidget(base_map_row, 1)

        self.baseMapBox = box
        area.addWidget(box)
        area.addWidget(self._build_saved_analyses_box(parent))
        return area

    def showEvent(self, event):
        super().showEvent(event)
        QTimer.singleShot(0, self._finish_initial_layout)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if not self._adjustingWindowWidth and not self._finishingWindowInteraction:
            self._begin_window_interaction()
        # In a normal window the height determines the A-series width. Apply
        # that relationship in the same resize event so Qt and the window
        # manager always have a complete two-column layout before painting.
        # The top-level minimum is deferred until Qt has delivered any paired
        # WindowStateChange event.
        if not self._applyingASeriesLayout:
            self._apply_a_series_layout(
                event.size().height(), update_window_constraint=False
            )
            self._aSeriesLayoutTimer.start()

    def moveEvent(self, event):
        super().moveEvent(event)
        if not self._adjustingWindowWidth and not self._finishingWindowInteraction:
            self._begin_window_interaction()

    def changeEvent(self, event):
        super().changeEvent(event)
        if event.type() == QEvent.Type.WindowStateChange:
            self._windowStateSettling = True
            self._windowStateTimer.start()
            QTimer.singleShot(0, self._update_window_restore_button)

    def _update_window_restore_button(self):
        if not hasattr(self, "restoreWindowButton"):
            return
        show_restore = self._showLinuxRestoreControl and (
            self.isMaximized() or self.isFullScreen()
        )
        self.restoreWindowButton.setVisible(show_restore)

    def _restore_normal_window(self):
        if self.isMaximized() or self.isFullScreen():
            self.showNormal()
            self.raise_()
            self.activateWindow()

    def _close_and_clear(self):
        self._clear_session_analysis_layers()
        self.hide()

    def reject(self):
        """Esc closes the working view and removes session analysis layers."""

        self._close_and_clear()

    def closeEvent(self, event):
        self._clear_session_analysis_layers()
        super().closeEvent(event)

    def _finish_window_state_transition(self):
        self._windowStateSettling = False
        self._aSeriesLayoutTimer.stop()
        self._apply_a_series_layout()

    def _begin_window_interaction(self):
        if not self.isVisible():
            return
        if not self._previewFrozenForWindowChange:
            self.previewCanvas.freeze(True)
            self._previewFrozenForWindowChange = True
        self._windowInteractionTimer.start()

    def _finish_window_interaction(self):
        self._finishingWindowInteraction = True
        try:
            self._aSeriesLayoutTimer.stop()
            self._apply_a_series_layout()
            self._aSeriesLayoutTimer.stop()
            self._previewResizeTimer.stop()
            self._restore_resized_preview_extent(final=True)
        finally:
            if self._previewFrozenForWindowChange:
                self.previewCanvas.freeze(False)
                self._previewFrozenForWindowChange = False
                self.previewCanvas.refresh()
            self._finishingWindowInteraction = False

    def _finish_initial_layout(self):
        # QgsMapCanvas resets its viewport while being shown; restore the
        # opaque background once the visible viewport exists.
        self.previewCanvas.viewport().setAutoFillBackground(True)
        self._apply_a_series_layout()
        if self._pendingPreviewView is not None:
            saved = self._pendingPreviewView
            self._pendingPreviewView = None
            QTimer.singleShot(0, lambda state=saved: self._restore_preview_view(state))
        elif not self._previewInitialized:
            self._sync_preview_from_main()

    def _set_hidden_child_geometry(self):
        """Resolve nested child geometry while the top-level window is hidden."""

        self.layout().setGeometry(self.rect())
        splitter_height = self.columnsSplitter.height()
        available_width = (
            self.columnsSplitter.width() - self.columnsSplitter.handleWidth()
        )
        reserved_right_width = self.controlPanel.minimumWidth()
        staged_left_width = max(
            self._minimum_preview_panel_width(),
            available_width - reserved_right_width,
        )
        # A hidden QSplitter does not distribute newly available geometry
        # until its first show. Stage the two child sizes explicitly so the
        # nested preview layout can report its real height beforehand.
        self.previewPanel.resize(staged_left_width, splitter_height)
        self.controlPanel.resize(reserved_right_width, splitter_height)
        self.previewPanel.layout().setGeometry(self.previewPanel.rect())
        preview_box = self.previewFrame.parentWidget()
        preview_box.layout().setGeometry(preview_box.contentsRect())
        self.controlPanel.layout().setGeometry(self.controlPanel.rect())

    def _apply_a_series_layout(
        self, window_height=None, update_window_constraint=True
    ):
        """Apply the single height -> A width -> window-width relationship."""

        if (
            self._applyingASeriesLayout
            or not hasattr(self, "previewFrame")
            or self.previewFrame.height() <= 0
            or self._previewVerticalChrome is None
        ):
            return
        self._applyingASeriesLayout = True
        try:
            full_frame_height, desired_left_width, required_width = (
                self._a_series_dimensions_for_height(
                    self.height() if window_height is None else window_height
                )
            )
            blocked_window_states = (
                Qt.WindowState.WindowMinimized
                | Qt.WindowState.WindowMaximized
                | Qt.WindowState.WindowFullScreen
            )
            normal_window = (
                update_window_constraint
                and not self._windowStateSettling
                and not (self.windowState() & blocked_window_states)
                and not self._fills_available_screen()
            )
            if normal_window:
                bounded_required_width = min(
                    required_width, self._available_screen_width()
                )
                # This is the authoritative normal-window constraint. It runs
                # only after native resize/state events have been separated.
                if self.minimumWidth() != bounded_required_width:
                    self.setMinimumWidth(bounded_required_width)
                if self.width() < bounded_required_width:
                    self._adjustingWindowWidth = True
                    try:
                        self.resize(bounded_required_width, self.height())
                        self._keep_window_within_available_screen()
                    finally:
                        self._adjustingWindowWidth = False

            self.layout().activate()
            available_width = max(
                0,
                self.columnsSplitter.width()
                - self.columnsSplitter.handleWidth(),
            )
            reserved_right_width = self.controlPanel.minimumWidth()
            available_left_width = max(
                self._minimum_preview_panel_width(),
                available_width - reserved_right_width,
            )
            left_width = min(desired_left_width, available_left_width)

            # The splitter handle is intentionally disabled. Fixing the left
            # child to the calculated width makes that calculation the only
            # owner of the column boundary; the right child receives all
            # remaining space.
            if self.previewPanel.width() != left_width:
                self._adjustingWindowWidth = True
                try:
                    self.previewPanel.setFixedWidth(left_width)
                    self.columnsSplitter.setSizes(
                        [
                            left_width,
                            max(
                                reserved_right_width,
                                available_width - left_width,
                            ),
                        ]
                    )
                finally:
                    self._adjustingWindowWidth = False

            ratio = self.previewFrame.aspectRatio()
            chrome_width = self._preview_panel_chrome_width()
            available_frame_width = max(1, left_width - chrome_width)
            constrained = left_width < desired_left_width
            compact_height = min(
                full_frame_height,
                max(1, int(round(available_frame_width / ratio))),
            )
            # The same safe fallback applies to portrait and landscape: when
            # the screen cannot contain the requested width, keep the control
            # panel visible and place the spare space below the preview stack.
            self._set_preview_stack_compact(constrained, compact_height)
        finally:
            self._applyingASeriesLayout = False

    def _fills_available_screen(self):
        """Return whether the current client size is a WM screen-sized state."""

        screen = self.screen() or QApplication.primaryScreen()
        if screen is None:
            return False
        available = screen.availableGeometry()
        # Xfce can deliver the maximized Resize before WindowStateChange. Do
        # not treat that one event as a user-resized normal window.
        return (
            self.width() >= available.width() - 2
            and self.height() >= available.height() - 2
        )

    def _keep_window_within_available_screen(self):
        screen = self.screen() or QApplication.primaryScreen()
        if screen is None:
            return
        available = screen.availableGeometry()
        frame = self.frameGeometry()
        maximum_x = available.right() - frame.width() + 1
        target_x = max(available.left(), min(frame.left(), maximum_x))
        delta_x = target_x - frame.left()
        if delta_x:
            self.move(self.x() + delta_x, self.y())

    def _full_preview_height(self):
        if self._previewVerticalChrome is None:
            self._previewVerticalChrome = max(
                0, self.height() - self.previewFrame.height()
            )
        return max(
            ASeriesPortraitPreview._MIN_HEIGHT,
            self.height() - self._previewVerticalChrome,
        )

    def _minimum_preview_panel_width(self):
        """Return the intrinsic left-panel minimum, not its current fixed width."""

        if self._previewPanelMinimumWidth is not None:
            return self._previewPanelMinimumWidth
        return self.previewPanel.minimumSizeHint().width()

    def _a_series_dimensions_for_height(self, window_height):
        """Return frame height, left width and outer width for a window height."""

        full_frame_height = max(
            ASeriesPortraitPreview._MIN_HEIGHT,
            int(window_height) - self._previewVerticalChrome,
        )
        frame_width = max(
            ASeriesPortraitPreview._MIN_WIDTH,
            int(round(full_frame_height * self.previewFrame.aspectRatio())),
        )
        preview_width = self._preview_panel_width_for_frame(frame_width)
        return (
            full_frame_height,
            preview_width,
            self._required_window_width(preview_width),
        )

    def _preview_panel_chrome_width(self):
        chrome_width = self.previewPanel.width() - self.previewFrame.width()
        if chrome_width <= 0:
            margins = self.previewFrame.parentWidget().layout().contentsMargins()
            chrome_width = margins.left() + margins.right()
        return chrome_width

    def _set_preview_stack_compact(self, compact, frame_height=None):
        compact = bool(compact)
        target_height = int(frame_height or 0)
        if (
            self._previewStackCompact == compact
            and (not compact or self._compactPreviewHeight == target_height)
        ):
            return

        maximum_widget_size = 16777215
        self.previewBox.setMinimumHeight(0)
        self.previewBox.setMaximumHeight(maximum_widget_size)
        if compact:
            self.previewFrame.setFixedHeight(max(1, target_height))
            self.previewFrame.setSizePolicy(
                QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed
            )
            self.previewBox.setSizePolicy(
                QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed
            )
            self.previewBoxLayout.invalidate()
            self.previewBoxLayout.activate()
            self.previewBox.setFixedHeight(self.previewBox.sizeHint().height())
            self.previewPanelLayout.setStretch(0, 0)
            self.previewPanelLayout.setStretch(2, 1)
            self._compactPreviewHeight = target_height
        else:
            self.previewFrame.setMinimumHeight(ASeriesPortraitPreview._MIN_HEIGHT)
            self.previewFrame.setMaximumHeight(maximum_widget_size)
            self.previewFrame.setSizePolicy(
                QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding
            )
            self.previewBox.setSizePolicy(
                QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Expanding
            )
            self.previewPanelLayout.setStretch(0, 1)
            self.previewPanelLayout.setStretch(2, 0)
            self._compactPreviewHeight = None
        self._previewStackCompact = compact
        self.previewPanelLayout.invalidate()
        self.previewPanelLayout.activate()

    def _toggle_preview_orientation(self):
        self._set_preview_orientation(not self._previewLandscape)

    def _set_preview_orientation(self, landscape, adjust_window=True):
        landscape = bool(landscape)
        if self._previewLandscape == landscape:
            self._update_preview_orientation_button()
            return

        if self.isVisible():
            self._begin_window_interaction()
        self._previewLandscape = landscape
        self.previewFrame.setLandscape(landscape)
        self._update_preview_orientation_button()

        blocked_window_states = (
            Qt.WindowState.WindowMinimized
            | Qt.WindowState.WindowMaximized
            | Qt.WindowState.WindowFullScreen
        )
        if (
            adjust_window
            and self.isVisible()
            and not (self.windowState() & blocked_window_states)
        ):
            _frame_height, _preview_width, required_width = (
                self._a_series_dimensions_for_height(self.height())
            )
            target_width = min(required_width, self._available_screen_width())
            # Lower this first when switching landscape -> portrait; otherwise
            # the former landscape minimum prevents the window from shrinking.
            self.setMinimumWidth(target_width)
            self._adjustingWindowWidth = True
            try:
                self.resize(target_width, self.height())
                self._keep_window_within_available_screen()
            finally:
                self._adjustingWindowWidth = False
        self._aSeriesLayoutTimer.stop()
        self._apply_a_series_layout()

    def _update_preview_orientation_button(self):
        if not hasattr(self, "previewOrientationButton"):
            return
        if self._previewLandscape:
            self.previewOrientationButton.setText("A↔")
            self.previewOrientationButton.setToolTip(
                tr(
                    "Switch the preview to portrait A-series.",
                    "プレビューをA規格縦へ切り替えます。",
                )
            )
        else:
            self.previewOrientationButton.setText("A↕")
            self.previewOrientationButton.setToolTip(
                tr(
                    "Switch the preview to landscape A-series.",
                    "プレビューをA規格横へ切り替えます。",
                )
            )

    def _available_screen_width(self):
        screen = self.screen() or QApplication.primaryScreen()
        if screen is None:
            return self.width()
        return screen.availableGeometry().width()

    def _preview_panel_width_for_frame(self, frame_width):
        """Translate an A-frame width to the outer left-column width."""

        # QGroupBox adds style-dependent space outside its layout margins.
        # Measure the complete current difference instead of assuming a theme.
        return frame_width + self._preview_panel_chrome_width()

    def _required_window_width(self, preview_panel_width):
        """Return the stable normal-window width needed by both columns."""

        return self._window_width_for_columns(
            preview_panel_width, self.controlPanel.minimumWidth()
        )

    def _window_width_for_columns(self, preview_panel_width, control_width):
        margins = self.layout().contentsMargins()
        return (
            margins.left()
            + margins.right()
            + preview_panel_width
            + self.columnsSplitter.handleWidth()
            + control_width
        )

    def _apply_startup_window_width(self):
        """Reserve the right panel, then add the height-derived A width."""

        self._set_hidden_child_geometry()
        # Capture this before setFixedWidth() changes the widget's own minimum.
        self._previewPanelMinimumWidth = self.previewPanel.minimumSizeHint().width()
        self._previewVerticalChrome = max(
            0, self.height() - self.previewFrame.height()
        )
        _frame_height, preview_width, target_width = (
            self._a_series_dimensions_for_height(self.height())
        )
        self.setMinimumWidth(target_width)
        self.resize(target_width, self.height())
        self.previewPanel.setFixedWidth(preview_width)
        self._set_hidden_child_geometry()
        self.columnsSplitter.setSizes(
            [preview_width, self.controlPanel.minimumWidth()]
        )

    def _queue_a_series_layout(self):
        # The frame's final height is authoritative. Coalescing avoids a
        # recursive resize while QSplitter settles its child geometries.
        if not self._windowInteractionTimer.isActive():
            self._aSeriesLayoutTimer.start()

    def _queue_preview_extent_restore(self, extent):
        """Keep the geographic preview extent fixed while its frame resizes."""

        if (
            not self._previewInitialized
            or self._pendingPreviewView is not None
            or extent.isEmpty()
        ):
            return
        if self._resizePreviewExtent is None:
            stable_extent = self._stablePreviewExtent
            self._resizePreviewExtent = QgsRectangle(
                stable_extent if stable_extent is not None else extent
            )
        # Coalesce resize corrections. During a window move/resize the canvas
        # remains frozen on its last complete image and is rendered once when
        # the interaction timer finishes.
        self._previewResizeTimer.start()

    def _restore_resized_preview_extent(self, final=False):
        extent = self._resizePreviewExtent
        if extent is None:
            return
        self.previewCanvas.setExtent(extent)
        self.previewCanvas.refresh()
        self._update_preview_status()
        if final:
            self._resizePreviewExtent = None

    def _preview_extent_changed(self):
        """Remember user navigation, but not canvas-resize corrections."""

        if (
            not self._previewInitialized
            or self._pendingPreviewView is not None
            or self._resizePreviewExtent is not None
        ):
            return
        extent = self.previewCanvas.extent()
        if not extent.isEmpty():
            self._stablePreviewExtent = QgsRectangle(extent)

    def _build_profile_box(self):
        section = QWidget(self)
        section_layout = QVBoxLayout(section)
        section_layout.setContentsMargins(0, 0, 0, 0)
        section_layout.setSpacing(2)

        header = QHBoxLayout()
        header.setContentsMargins(0, 0, 0, 0)
        profile_title = QLabel(tr("Profile", "プロファイル"), section)
        title_font = profile_title.font()
        title_font.setBold(True)
        profile_title.setFont(title_font)
        self.restoreWindowButton = QPushButton(
            tr("Exit full screen", "全画面を終了"), section
        )
        self.restoreWindowButton.setIcon(
            self.style().standardIcon(QStyle.StandardPixmap.SP_TitleBarNormalButton)
        )
        self.restoreWindowButton.setVisible(False)
        self.restoreWindowButton.clicked.connect(self._restore_normal_window)
        header.addWidget(profile_title)
        header.addStretch(1)
        header.addWidget(self.restoreWindowButton)
        section_layout.addLayout(header)

        box = QGroupBox(section)
        form = QFormLayout(box)

        self.profileLabel = QLabel(tr("Japan — terrain", "日本 — 地形"))
        self.profileLabel.setToolTip(
            tr(
                "Terrain analysis from a local DEM or GSI elevation tiles.",
                "ローカルDEMまたは国土地理院標高タイルによる地形解析です。",
            )
        )
        form.addRow(self.profileLabel)
        section_layout.addWidget(box)
        return section

    def _build_area_box(self):
        box = QGroupBox(tr("1. Area", "1. 範囲"))
        form = QFormLayout(box)

        area_mode_row = QWidget(box)
        area_mode_layout = QHBoxLayout(area_mode_row)
        area_mode_layout.setContentsMargins(0, 0, 0, 0)
        area_mode_layout.setSpacing(12)
        self.polygonAreaRadio = QRadioButton(
            tr("Polygon layer", "ポリゴンレイヤー"), area_mode_row
        )
        self.mapExtentAreaRadio = QRadioButton(
            tr("Use preview map extent", "画面の範囲を使用"), area_mode_row
        )
        self.mapExtentAreaRadio.setToolTip(
            tr(
                "Uses the full visible preview extent. The grey central 3/5 "
                "frame is a guide for interpreting results away from edge effects.",
                "プレビューに見えている全範囲を使用します。中央3/5の灰色枠は、"
                "端部影響を避けて結果を見るための目安です。",
            )
        )
        self.areaModeButtonGroup = QButtonGroup(box)
        self.areaModeButtonGroup.setExclusive(True)
        self.areaModeButtonGroup.addButton(self.polygonAreaRadio)
        self.areaModeButtonGroup.addButton(self.mapExtentAreaRadio)
        self.polygonAreaRadio.setChecked(True)
        area_mode_layout.addWidget(self.polygonAreaRadio)
        area_mode_layout.addWidget(self.mapExtentAreaRadio)
        area_mode_layout.addStretch(1)
        form.addRow(tr("Area source", "範囲指定"), area_mode_row)

        self.aoiLayerCombo = LayerTreeOrderComboBox(box)
        self.aoiLayerCombo.setAllowEmptyLayer(True)
        form.addRow(tr("Polygon layer", "ポリゴンレイヤー"), self.aoiLayerCombo)

        self.selectedOnlyCheck = QCheckBox(
            tr("Use selected polygon features only", "選択したポリゴン地物のみを使用")
        )
        self.selectedOnlyCheck.setChecked(True)
        form.addRow("", self.selectedOnlyCheck)
        return box

    def _build_data_box(self):
        box = QGroupBox(tr("2. DEM", "2. DEM"))
        form = QFormLayout(box)

        self.demSourceCombo = QComboBox(box)
        self.demSourceCombo.addItem(tr("Local DEM", "ローカルDEM"), "local")
        self.demSourceCombo.addItem(tr("GSI DEM", "国土地理院DEM"), "gsi")
        self.demSourceCombo.setCurrentIndex(
            self.demSourceCombo.findData("gsi")
        )
        form.addRow(tr("Source", "取得元"), self.demSourceCombo)

        self.dataLayerCombo = LayerTreeOrderComboBox(box)
        self.dataLayerCombo.setAllowEmptyLayer(True)
        self.localDemLabel = QLabel(tr("DEM layer", "DEMレイヤー"), box)
        form.addRow(self.localDemLabel, self.dataLayerCombo)

        self.gsiDatasetCombo = QComboBox(box)
        self.gsiDatasetCombo.addItem(
            tr("Best available", "利用可能な最高解像度"),
            "best",
        )
        self.gsiDatasetCombo.addItem("DEM1A 1 m", "1m")
        self.gsiDatasetCombo.addItem("DEM5A 5 m", "5m")
        self.gsiDatasetCombo.addItem("DEM10B 10 m", "10m")
        self.gsiDatasetLabel = QLabel(tr("Dataset", "データセット"), box)
        form.addRow(self.gsiDatasetLabel, self.gsiDatasetCombo)
        return box

    def _build_run_box(self):
        box = QGroupBox(tr("4. Run", "4. 実行"))
        layout = QVBoxLayout(box)
        self.runButton = QPushButton(tr("Run", "実行"), box)
        self.runButton.setDefault(True)
        layout.addWidget(self.runButton)
        return box

    def _build_analysis_box(self):
        box = QGroupBox(tr("3. Analysis", "3. 解析"))
        layout = QVBoxLayout(box)
        self.hillshadeCheck = QCheckBox(tr("Hillshade", "陰影起伏"), box)
        self.slopeCheck = QCheckBox(tr("Slope", "傾斜"), box)
        self.csMapCheck = QCheckBox(tr("CS Map", "CS立体図"), box)
        for control in (self.hillshadeCheck, self.slopeCheck, self.csMapCheck):
            control.setChecked(True)
            layout.addWidget(control)
        self.slopeCheck.setToolTip(
            tr(
                "Uses the appearance of Morizon Next's 災害リスク/傾斜 layer.",
                "Morizon Nextの「災害リスク/傾斜」レイヤーと同じ見た目を使用します。",
            )
        )
        layout.addStretch(1)
        return box

    def _build_analysis_name_box(self):
        box = QGroupBox(tr("Analysis name", "解析名"))
        layout = QVBoxLayout(box)
        self.analysisNameEdit = QLineEdit(box)
        self.analysisNameEdit.setPlaceholderText(
            tr("Analysis name", "解析名")
        )
        layout.addWidget(self.analysisNameEdit)
        note = QLabel(
            tr(
                "Leave blank to use a timestamp.",
                "解析時空白はタイムスタンプで命名",
            ),
            box,
        )
        note.setWordWrap(True)
        note.setAlignment(Qt.AlignmentFlag.AlignRight)
        note.setStyleSheet("font-size: 9pt;")
        layout.addWidget(note)
        return box

    def _build_saved_analyses_box(self, parent):
        box = QGroupBox(tr("Load analysis results", "解析結果の読み込み"), parent)
        form = QFormLayout(box)

        saved_row = QWidget(box)
        saved_layout = QHBoxLayout(saved_row)
        saved_layout.setContentsMargins(0, 0, 0, 0)
        saved_layout.setSpacing(4)
        self.savedAnalysisCombo = QComboBox(saved_row)
        self.savedAnalysisCombo.setSizeAdjustPolicy(
            QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon
        )
        self.savedAnalysisCombo.setMinimumContentsLength(12)
        self.loadAnalysisButton = QPushButton(tr("Load", "読込"), saved_row)
        self.openAnalysisFolderButton = QPushButton(
            # Leading/trailing spaces give the longer label breathing room.
            tr("   Open folder   ", "   フォルダを開く   "), saved_row
        )
        self.openAnalysisFolderButton.setToolTip(
            tr(
                "Open the folder that holds the analysis ZIP files in the file manager.",
                "解析のZIPファイルを置いているフォルダをファイルマネージャで開きます。",
            )
        )
        self.openAnalysisFolderButton.clicked.connect(self._open_analysis_folder)
        combo_height = self.savedAnalysisCombo.sizeHint().height()
        self.loadAnalysisButton.setFixedHeight(combo_height)
        self.openAnalysisFolderButton.setFixedHeight(combo_height)
        saved_layout.addWidget(self.savedAnalysisCombo, 1)
        saved_layout.addWidget(self.loadAnalysisButton)
        saved_layout.addWidget(self.openAnalysisFolderButton)

        self.refreshAnalysesButton = QToolButton(saved_row)
        self.refreshAnalysesButton.setIcon(
            self.style().standardIcon(QStyle.StandardPixmap.SP_BrowserReload)
        )
        self.refreshAnalysesButton.setAutoRaise(True)
        self.refreshAnalysesButton.setFixedSize(combo_height, combo_height)
        self.refreshAnalysesButton.setToolTip(
            tr(
                "Rebuild the list from the analysis folder.",
                "解析フォルダから一覧を作り直します。",
            )
        )
        self.refreshAnalysesButton.clicked.connect(
            lambda: self._refresh_saved_analyses()
        )
        saved_layout.addWidget(self.refreshAnalysesButton)
        form.addRow(tr("Results", "解析結果"), saved_row)
        return box

    def _build_result_box(self):
        box = QGroupBox(tr("Status", "状態"))
        layout = QVBoxLayout(box)
        self.resultEdit = QTextEdit(box)
        self.resultEdit.setReadOnly(True)
        self.resultEdit.setPlaceholderText(
            tr(
                "Choose an area, DEM source and outputs, then run the analysis.",
                "範囲、DEM取得元、出力を選び、解析を実行します。",
            )
        )
        layout.addWidget(self.resultEdit)
        return box

    def _build_buttons(self):
        row = QHBoxLayout()
        self.languageButton = QPushButton(tr("Language: English", "言語：日本語"))
        self.languageButton.setToolTip(
            tr("Switch to Japanese.", "英語へ切り替えます。")
        )
        self.closeButton = QPushButton(tr("Close", "閉じる"))

        row.addWidget(self.languageButton)
        row.addStretch(1)
        row.addWidget(self.closeButton)
        return row

    def _refresh_preview_layer_choices(self, *args):
        del args
        current_aoi = self.aoiLayerCombo.currentLayer()
        current_data = self.dataLayerCombo.currentLayer()
        current_overlay = self.overlayLayerCombo.currentLayer()
        current_base = self.baseMapCombo.currentLayer()
        tree_layers = QgsProject.instance().layerTreeRoot().layerOrder()
        polygon_layers = [
            layer
            for layer in tree_layers
            if layer.type() == Qgis.LayerType.Vector
            and layer.geometryType() == Qgis.GeometryType.Polygon
        ]
        base_layers = [layer for layer in tree_layers if _is_base_preview_layer(layer)]
        local_rasters = [
            layer
            for layer in tree_layers
            if layer.type() == Qgis.LayerType.Raster and not _is_network_layer(layer)
        ]
        overlay_layers = [
            layer for layer in tree_layers if not _is_base_preview_layer(layer)
        ]
        self.aoiLayerCombo.setLayers(polygon_layers)
        self.dataLayerCombo.setLayers(local_rasters)
        self.overlayLayerCombo.setLayers(overlay_layers)
        self.baseMapCombo.setLayers(base_layers)

        if current_aoi is not None and current_aoi in polygon_layers:
            self.aoiLayerCombo.setLayer(current_aoi)
        elif not self._aoiChoicesInitialized and polygon_layers:
            active_layer = self.iface.activeLayer()
            if active_layer in polygon_layers:
                self.aoiLayerCombo.setLayer(active_layer)
            else:
                self.aoiLayerCombo.setLayer(polygon_layers[0])
        self._aoiChoicesInitialized = True
        if current_data is not None and current_data in local_rasters:
            self.dataLayerCombo.setLayer(current_data)
        if current_overlay is not None and current_overlay in overlay_layers:
            self.overlayLayerCombo.setLayer(current_overlay)
        else:
            stored, overlay = _stored_preview_layer(
                _PREVIEW_OVERLAY_KEY, overlay_layers
            )
            if stored:
                self.overlayLayerCombo.setLayer(overlay)
        stored_base, stored_base_layer = _stored_preview_layer(
            _PREVIEW_BASE_MAP_KEY, base_layers
        )
        if current_base is not None and current_base in base_layers:
            self.baseMapCombo.setLayer(current_base)
        elif stored_base:
            # A stored empty choice means "no base map" and is kept as is.
            self.baseMapCombo.setLayer(stored_base_layer)
        elif self.baseMapCombo.currentLayer() is None:
            # Prefer the lowest visible network layer, which is commonly the
            # base map in the QGIS layer stack.
            for layer in reversed(self.mainCanvas.layers()):
                if layer in base_layers:
                    self.baseMapCombo.setLayer(layer)
                    break
        self._update_preview_layers()

    def _store_preview_layer_choices(self, *args):
        del args
        project = QgsProject.instance()
        for key, combo in (
            (_PREVIEW_OVERLAY_KEY, self.overlayLayerCombo),
            (_PREVIEW_BASE_MAP_KEY, self.baseMapCombo),
        ):
            layer_id = _layer_id(combo.currentLayer()) or ""
            value, ok = project.readEntry(_PROJECT_SCOPE, key, "")
            if not ok or value != layer_id:
                project.writeEntry(_PROJECT_SCOPE, key, layer_id)

    def _apply_stored_preview_layer_choices(self, *args):
        """Apply the choices saved in a project that has just been read."""

        del args
        project = QgsProject.instance()
        for key, combo in (
            (_PREVIEW_OVERLAY_KEY, self.overlayLayerCombo),
            (_PREVIEW_BASE_MAP_KEY, self.baseMapCombo),
        ):
            layer_id = _stored_preview_layer_id(key)
            if layer_id is None:
                continue
            layer = project.mapLayer(layer_id) if layer_id else None
            if layer_id and layer is None:
                continue
            combo.setLayer(layer)
        self._update_preview_layers()

    def _update_preview_layers(self, *args):
        del args
        overlay_source = self.overlayLayerCombo.currentLayer()
        base_map = self.baseMapCombo.currentLayer()
        aoi_layer = (
            None
            if self.mapExtentAreaRadio.isChecked()
            else self.aoiLayerCombo.currentLayer()
        )
        old_overlay = self._set_preview_overlay_source(overlay_source)
        show_base = (
            base_map is not None
            and base_map is not aoi_layer
            and base_map is not overlay_source
        )
        old_base = self._set_preview_base_source(base_map if show_base else None)
        layers = []
        # Canvas order is top to bottom: AOI, optional overlay, then base.
        if aoi_layer is not None and aoi_layer is not overlay_source:
            layers.append(aoi_layer)
        if self._previewOverlayLayer is not None:
            layers.append(self._previewOverlayLayer)
        if self._previewBaseLayer is not None:
            layers.append(self._previewBaseLayer)
        self.previewCanvas.setLayers(layers)
        self.previewCanvas.refresh()
        for old_layer in (old_overlay, old_base):
            if old_layer is not None:
                old_layer.deleteLater()
        self._refresh_preview_polygon()

    def _set_preview_base_source(self, source_layer):
        """Use a preview-only copy so opacity never changes the main map."""

        source_id = source_layer.id() if source_layer is not None else None
        old_base = None
        if source_id != self._previewBaseSourceId:
            old_base = self._previewBaseLayer
            self._previewBaseLayer = (
                source_layer.clone() if source_layer is not None else None
            )
            self._previewBaseSourceId = source_id
        if self._previewBaseLayer is not None:
            opacity = self._overlayOpacityValues[self._baseOpacityIndex] / 100.0
            self._previewBaseLayer.setOpacity(opacity)
        return old_base

    def _cycle_base_opacity(self):
        self._baseOpacityIndex = (
            self._baseOpacityIndex + 1
        ) % len(self._overlayOpacityValues)
        opacity = self._overlayOpacityValues[self._baseOpacityIndex]
        self.baseOpacityButton.setText("{}%".format(opacity))
        if self._previewBaseLayer is not None:
            self._previewBaseLayer.setOpacity(opacity / 100.0)
            # The canvas keeps a cached image per layer; drop it so the new
            # opacity is drawn now instead of after the next pan or zoom.
            self._previewBaseLayer.triggerRepaint()
            self.previewCanvas.refresh()

    def _set_preview_overlay_source(self, source_layer):
        source_id = source_layer.id() if source_layer is not None else None
        old_overlay = None
        if source_id != self._previewOverlaySourceId:
            old_overlay = self._previewOverlayLayer
            self._previewOverlayLayer = (
                source_layer.clone() if source_layer is not None else None
            )
            self._previewOverlaySourceId = source_id
        if self._previewOverlayLayer is not None:
            opacity = self._overlayOpacityValues[self._overlayOpacityIndex] / 100.0
            self._previewOverlayLayer.setOpacity(opacity)
        return old_overlay

    def _cycle_overlay_opacity(self):
        self._overlayOpacityIndex = (
            self._overlayOpacityIndex + 1
        ) % len(self._overlayOpacityValues)
        opacity = self._overlayOpacityValues[self._overlayOpacityIndex]
        self.overlayOpacityButton.setText("{}%".format(opacity))
        if self._previewOverlayLayer is not None:
            self._previewOverlayLayer.setOpacity(opacity / 100.0)
            self._previewOverlayLayer.triggerRepaint()
            self.previewCanvas.refresh()

    def _sync_preview_from_main(self, *args):
        del args
        settings = self.mainCanvas.mapSettings()
        destination_crs = settings.destinationCrs()
        if destination_crs.isValid():
            self.previewCanvas.setDestinationCrs(destination_crs)
        # Preserve the main canvas map-units-per-pixel. Explicitly setting the
        # portrait extent is reliable even before a base map has rendered,
        # whereas zoomScale() can remain at the canvas default in that state.
        centre = self.mainCanvas.center()
        map_units_per_pixel = settings.mapUnitsPerPixel()
        half_width = map_units_per_pixel * max(1, self.previewCanvas.width()) / 2.0
        half_height = map_units_per_pixel * max(1, self.previewCanvas.height()) / 2.0
        scale = self.mainCanvas.scale()
        preview_extent = QgsRectangle(
            centre.x() - half_width,
            centre.y() - half_height,
            centre.x() + half_width,
            centre.y() + half_height,
        )
        self.previewCanvas.setExtent(preview_extent)
        # Canvas frame pixels can make the first estimate differ slightly.
        # Correct once using QGIS's own computed scale while keeping the centre.
        preview_scale = self.previewCanvas.scale()
        if scale > 0 and preview_scale > 0:
            correction = scale / preview_scale
            half_width *= correction
            half_height *= correction
            self.previewCanvas.setExtent(
                QgsRectangle(
                    centre.x() - half_width,
                    centre.y() - half_height,
                    centre.x() + half_width,
                    centre.y() + half_height,
                )
            )
        self._previewInitialized = True
        self._stablePreviewExtent = QgsRectangle(self.previewCanvas.extent())
        self.previewCanvas.refresh()
        self._refresh_preview_polygon()

    def _update_preview_status(self, *args):
        del args
        scale = self.previewCanvas.scale()
        self.previewStatusLabel.setText(
            tr(
                "Scale 1:{:,.0f}",
                "縮尺 1:{:,.0f}",
            ).format(scale)
        )

    def _restore_preview_view(self, state):
        crs = QgsCoordinateReferenceSystem(state.get("preview_crs", ""))
        if crs.isValid():
            self.previewCanvas.setDestinationCrs(crs)
        extent_values = state.get("preview_extent")
        if extent_values and len(extent_values) == 4:
            extent = QgsRectangle(*extent_values)
            if not extent.isEmpty():
                self.previewCanvas.setExtent(extent)
                self._stablePreviewExtent = QgsRectangle(extent)
                self.previewCanvas.refresh()
                self._refresh_preview_polygon()
        else:
            # Compatibility with state captured before extent preservation was
            # introduced.
            centre = state.get("preview_center")
            scale = state.get("preview_scale", 0)
            if centre and scale > 0:
                self.previewCanvas.setCenter(QgsPointXY(centre[0], centre[1]))
                self.previewCanvas.zoomScale(scale)
                self._stablePreviewExtent = QgsRectangle(
                    self.previewCanvas.extent()
                )
                self.previewCanvas.refresh()
                self._refresh_preview_polygon()
        self._previewInitialized = True

    def _preview_view_state(self):
        centre = self.previewCanvas.center()
        extent = self._stablePreviewExtent or self.previewCanvas.extent()
        crs = self.previewCanvas.mapSettings().destinationCrs()
        return {
            "preview_center": (centre.x(), centre.y()),
            "preview_scale": self.previewCanvas.scale(),
            "preview_extent": (
                extent.xMinimum(),
                extent.yMinimum(),
                extent.xMaximum(),
                extent.yMaximum(),
            ),
            "preview_crs": crs.authid() or crs.toWkt(),
        }

    def _aoi_layer_changed(self, layer):
        if self._previewAoiLayer is not None:
            try:
                self._previewAoiLayer.selectionChanged.disconnect(self._refresh_preview_polygon)
            except (TypeError, RuntimeError):
                pass
        self._previewAoiLayer = layer
        if self._previewAoiLayer is not None:
            self._previewAoiLayer.selectionChanged.connect(self._refresh_preview_polygon)
        self.previewSelectTool.setLayer(layer)
        if layer is None and self.previewCanvas.mapTool() is self.previewSelectTool:
            self._activate_map_navigation()
        self._update_preview_selection_controls()
        self._update_preview_layers()

    def _area_mode_changed(self, *args):
        del args
        use_polygon = self.polygonAreaRadio.isChecked()
        self.aoiLayerCombo.setEnabled(use_polygon)
        self.selectedOnlyCheck.setEnabled(use_polygon)
        self.previewFrame.setAnalysisGuideVisible(not use_polygon)
        if not use_polygon and self.previewCanvas.mapTool() is self.previewSelectTool:
            self._activate_map_navigation()
        self._update_preview_selection_controls()
        self._update_preview_layers()

    def _activate_map_navigation(self, *args):
        del args
        self.previewCanvas.setMapTool(self.previewPanTool)
        self.mapNavigationButton.setChecked(True)

    def _activate_polygon_selection(self, *args):
        del args
        if self.mapExtentAreaRadio.isChecked():
            self._activate_map_navigation()
            return
        layer = self.aoiLayerCombo.currentLayer()
        if layer is None:
            self._activate_map_navigation()
            return
        self.previewSelectTool.setLayer(layer)
        self.previewCanvas.setMapTool(self.previewSelectTool)
        self.polygonSelectButton.setChecked(True)

    def _select_preview_polygon(self, feature, modifiers=None):
        feature_id = feature.id() if hasattr(feature, "id") else int(feature)
        self._select_preview_polygons([feature_id], modifiers)

    def _select_preview_polygons(self, feature_ids, modifiers=None):
        layer = self.aoiLayerCombo.currentLayer()
        if layer is None:
            return
        feature_ids = set(feature_ids)
        if modifiers is None:
            modifiers = QApplication.keyboardModifiers()
        selected_ids = set(layer.selectedFeatureIds())
        if modifiers & Qt.KeyboardModifier.ControlModifier:
            selected_in_range = selected_ids & feature_ids
            if selected_in_range:
                # QGIS range-toggle semantics: if the range contains selected
                # features, remove those only. Never invert selected and
                # unselected features together.
                layer.selectByIds(sorted(selected_ids - selected_in_range))
            else:
                layer.selectByIds(sorted(selected_ids | feature_ids))
        elif modifiers & Qt.KeyboardModifier.ShiftModifier:
            layer.selectByIds(sorted(selected_ids | feature_ids))
        else:
            layer.selectByIds(sorted(feature_ids))

    def _clear_polygon_selection(self, *args):
        del args
        layer = self.aoiLayerCombo.currentLayer()
        if layer is not None:
            layer.removeSelection()

    def _update_preview_selection_controls(self):
        layer = self.aoiLayerCombo.currentLayer()
        has_layer = self.polygonAreaRadio.isChecked() and layer is not None
        self.polygonSelectButton.setEnabled(has_layer)
        self.clearSelectionButton.setEnabled(
            has_layer and layer.selectedFeatureCount() > 0
        )

    def _refresh_preview_polygon(self, *args):
        del args
        self.previewRubber.reset(Qgis.GeometryType.Polygon)
        if self.mapExtentAreaRadio.isChecked():
            self._update_preview_selection_controls()
            self.previewCanvas.refresh()
            return
        layer = self.aoiLayerCombo.currentLayer()
        if layer is None:
            self._update_preview_selection_controls()
            return

        if self.selectedOnlyCheck.isChecked():
            features = layer.getSelectedFeatures()
        else:
            features = layer.getFeatures()

        destination_crs = self.previewCanvas.mapSettings().destinationCrs()
        transform = None
        if (
            layer.crs().isValid()
            and destination_crs.isValid()
            and layer.crs() != destination_crs
        ):
            transform = QgsCoordinateTransform(
                layer.crs(), destination_crs, QgsProject.instance().transformContext()
            )

        for feature in features:
            geometry = feature.geometry()
            if geometry is None or geometry.isEmpty():
                continue
            geometry = QgsGeometry(geometry)
            if transform is not None:
                try:
                    geometry.transform(transform)
                except QgsCsException:
                    continue
            self.previewRubber.addGeometry(geometry, None)
        self.previewCanvas.refresh()
        self._update_preview_selection_controls()

    def _dem_source_changed(self, *args):
        del args
        local = self.demSourceCombo.currentData() == "local"
        self.localDemLabel.setVisible(local)
        self.dataLayerCombo.setVisible(local)
        self.gsiDatasetLabel.setVisible(not local)
        self.gsiDatasetCombo.setVisible(not local)

    def _run_analysis(self):
        if self._terrainWorker is not None:
            return
        try:
            specification = self._terrain_specification()
        except ValueError as error:
            self.resultEdit.setPlainText(str(error))
            return

        package_path = specification["package_path"]
        overwrite = os.path.exists(package_path)
        if overwrite:
            answer = QMessageBox.question(
                self,
                tr("Overwrite analysis", "解析を上書き"),
                tr(
                    "An analysis with this name already exists. Overwrite it?",
                    "同じ名前の解析があります。上書きしますか？",
                ),
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if answer != QMessageBox.StandardButton.Yes:
                return
            self._remove_package_layers(package_path)
            self._overwritePackagePath = package_path
        else:
            self._overwritePackagePath = None
        specification["overwrite"] = overwrite

        self.resultEdit.setPlainText(tr("Starting analysis...", "解析を開始します…"))
        self.runButton.setEnabled(False)
        self.languageButton.setEnabled(False)
        self.loadAnalysisButton.setEnabled(False)
        worker = TerrainJobWorker(specification, self)
        self._terrainWorker = worker
        worker.message.connect(self._show_terrain_message)
        worker.done.connect(self._terrain_finished)
        worker.start()

    def _terrain_specification(self):
        project_directory = QgsProject.instance().absolutePath()
        if not project_directory:
            raise ValueError(
                tr(
                    "Save the QGIS project before running an analysis.",
                    "解析を実行する前にQGISプロジェクトを保存してください。",
                )
            )
        analysis_name = self.analysisNameEdit.text().strip()
        if not analysis_name:
            # Blank name: use the local run time. The field stays blank so the
            # next run gets its own name instead of overwriting this one.
            analysis_name = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")

        geometry, crs, area_mode = self._analysis_area()
        outputs = []
        if self.hillshadeCheck.isChecked():
            outputs.append("hillshade")
        if self.slopeCheck.isChecked():
            outputs.append("slope")
        if self.csMapCheck.isChecked():
            outputs.append("cs_map")
        if not outputs:
            raise ValueError(
                tr("Choose at least one analysis output.", "解析出力を1つ以上選択してください。")
            )

        source_kind = self.demSourceCombo.currentData()
        source_path = ""
        source_name = ""
        if source_kind == "local":
            layer = self.dataLayerCombo.currentLayer()
            if not isinstance(layer, QgsRasterLayer):
                raise ValueError(tr("Choose a local DEM layer.", "ローカルDEMレイヤーを選択してください。"))
            source_path = layer.source().split("|", 1)[0]
            if not os.path.isfile(source_path):
                raise ValueError(
                    tr(
                        "The selected DEM must be a local raster file.",
                        "選択するDEMはローカルのラスタファイルである必要があります。",
                    )
                )
            source_name = layer.name()

        return {
            "analysis_name": analysis_name,
            "package_path": packages.package_path(project_directory, analysis_name),
            "plugin_version": _plugin_version(),
            "source_kind": source_kind,
            "source_path": source_path,
            "source_name": source_name,
            "gsi_dataset": self.gsiDatasetCombo.currentData() or "best",
            "outputs": outputs,
            "area_mode": area_mode,
            "preview_landscape": self._previewLandscape,
            "aoi_wkt": geometry.asWkt(12),
            "aoi_crs_wkt": crs.toWkt(),
        }

    def _analysis_area(self):
        if self.mapExtentAreaRadio.isChecked():
            extent = self._stablePreviewExtent or self.previewCanvas.extent()
            crs = self.previewCanvas.mapSettings().destinationCrs()
            if extent.isEmpty() or not crs.isValid():
                raise ValueError(tr("The preview extent is not ready.", "プレビュー範囲を取得できません。"))
            return QgsGeometry.fromRect(extent), crs, "map_extent"

        layer = self.aoiLayerCombo.currentLayer()
        if layer is None:
            raise ValueError(tr("Choose a polygon layer.", "ポリゴンレイヤーを選択してください。"))
        if self.selectedOnlyCheck.isChecked():
            if layer.selectedFeatureCount() == 0:
                raise ValueError(
                    tr(
                        "Select at least one polygon, or turn off selected features only.",
                        "1つ以上のポリゴンを選択するか、「選択地物のみ」を解除してください。",
                    )
                )
            features = layer.getSelectedFeatures()
        else:
            features = layer.getFeatures()
        geometries = [
            QgsGeometry(feature.geometry())
            for feature in features
            if feature.geometry() is not None and not feature.geometry().isEmpty()
        ]
        if not geometries:
            raise ValueError(tr("The area has no usable geometry.", "範囲に使用可能な形状がありません。"))
        geometry = QgsGeometry.unaryUnion(geometries)
        if geometry.isNull() or geometry.isEmpty():
            raise ValueError(tr("The area could not be combined.", "範囲を結合できませんでした。"))
        return geometry, layer.crs(), "polygon"

    def _show_terrain_message(self, message):
        translations = {
            "Preparing DEM...": "DEMを準備しています…",
            "Creating hillshade...": "陰影起伏を作成しています…",
            "Creating slope...": "傾斜を作成しています…",
            "Creating CS Map...": "CS立体図を作成しています…",
            "Writing analysis package...": "解析ZIPを保存しています…",
        }
        if is_japanese():
            if message.startswith("Fetching GSI DEM tiles..."):
                message = message.replace("Fetching GSI DEM tiles...", "国土地理院DEMを取得しています…", 1)
            else:
                message = translations.get(message, message)
        self.resultEdit.setPlainText(message)

    def _terrain_finished(self, result, cancelled, error):
        worker = self._terrainWorker
        self._terrainWorker = None
        if worker is not None:
            worker.deleteLater()
        self.runButton.setEnabled(True)
        self.languageButton.setEnabled(True)
        self.loadAnalysisButton.setEnabled(True)
        if cancelled:
            self.resultEdit.setPlainText(tr("Analysis cancelled.", "解析を中止しました。"))
            self._restore_overwritten_package()
            return
        if error:
            self.resultEdit.setPlainText(
                tr("Analysis failed: {}", "解析に失敗しました：{}").format(error)
            )
            self._refresh_saved_analyses()
            self._restore_overwritten_package()
            return

        package_path = result["package_path"]
        manifest = result["manifest"]
        self._refresh_saved_analyses(package_path)
        self._add_package_layers(package_path, manifest)
        self._overwritePackagePath = None
        grid = manifest["analysis_grid"]
        source = manifest["source"].get("dataset") or manifest["source"].get("name", "")
        self.resultEdit.setPlainText(
            tr(
                "Analysis saved.\nName: {}\nDEM: {}\nGrid: {:.3f} m / {} × {}\nValid coverage: {:.3f}%\nPackage: {}",
                "解析を保存しました。\n解析名：{}\nDEM：{}\nグリッド：{:.3f} m / {} × {}\n有効範囲：{:.3f}%\nZIP：{}",
            ).format(
                manifest["analysis_name"],
                source,
                grid["cell_size_m"],
                grid["columns"],
                grid["rows"],
                grid["valid_coverage_percent"],
                package_path,
            )
        )
        self._bring_to_front()

    def _restore_overwritten_package(self):
        path = self._overwritePackagePath
        self._overwritePackagePath = None
        if not path or not os.path.isfile(path):
            return
        try:
            self._add_package_layers(path, packages.read_manifest(path))
        except packages.PackageError:
            pass

    def _refresh_saved_analyses(self, selected_path=None):
        current = selected_path or self.savedAnalysisCombo.currentData()
        self.savedAnalysisCombo.blockSignals(True)
        self.savedAnalysisCombo.clear()
        for name, path, manifest in packages.list_packages(QgsProject.instance().absolutePath()):
            self.savedAnalysisCombo.addItem(name, path)
            self.savedAnalysisCombo.setItemData(
                self.savedAnalysisCombo.count() - 1, manifest, Qt.ItemDataRole.UserRole + 1
            )
        index = self.savedAnalysisCombo.findData(current)
        if index >= 0:
            self.savedAnalysisCombo.setCurrentIndex(index)
        self.savedAnalysisCombo.blockSignals(False)
        self.loadAnalysisButton.setEnabled(self.savedAnalysisCombo.count() > 0)

    def _open_analysis_folder(self):
        project_directory = QgsProject.instance().absolutePath()
        if not project_directory:
            self.resultEdit.setPlainText(
                tr(
                    "Save the QGIS project first. Analyses are stored next to it.",
                    "先にQGISプロジェクトを保存してください。解析はその隣に保存されます。",
                )
            )
            return
        folder = packages.storage_directory(project_directory)
        if not os.path.isdir(folder):
            # Do not create folders in the project directory just for viewing.
            self.resultEdit.setPlainText(
                tr(
                    "No analysis has been saved for this project yet.\n{}",
                    "このプロジェクトにはまだ解析が保存されていません。\n{}",
                ).format(folder)
            )
            return
        QDesktopServices.openUrl(QUrl.fromLocalFile(folder))

    def _load_selected_analysis(self):
        path = self.savedAnalysisCombo.currentData()
        if not path:
            return
        try:
            manifest = packages.read_manifest(path)
        except packages.PackageError:
            self.resultEdit.setPlainText(tr("The analysis results could not be loaded.", "解析結果を読み込めません。"))
            return
        self.analysisNameEdit.setText(manifest["analysis_name"])
        source = manifest.get("source", {})
        source_kind = source.get("kind", "local")
        source_index = self.demSourceCombo.findData(source_kind)
        if source_index >= 0:
            self.demSourceCombo.setCurrentIndex(source_index)
        if source_kind == "gsi":
            dataset_index = self.gsiDatasetCombo.findData(source.get("requested_dataset", "best"))
            if dataset_index >= 0:
                self.gsiDatasetCombo.setCurrentIndex(dataset_index)
        output_keys = manifest.get("outputs", {})
        self.hillshadeCheck.setChecked("hillshade" in output_keys)
        self.slopeCheck.setChecked("slope" in output_keys)
        self.csMapCheck.setChecked("cs_map" in output_keys)
        self._add_package_layers(path, manifest)
        self.resultEdit.setPlainText(
            tr("Loaded analysis: {}", "解析を読み込みました：{}").format(manifest["analysis_name"])
        )

    def _remove_package_layers(self, package_path):
        absolute = os.path.abspath(package_path)
        project = QgsProject.instance()
        remove_ids = [
            layer.id()
            for layer in project.mapLayers().values()
            if layer.customProperty("qlae/package_path", "") == absolute
            and _is_session_analysis_layer(layer)
        ]
        if remove_ids:
            project.removeMapLayers(remove_ids)

    def _clear_session_analysis_layers(self):
        project = QgsProject.instance()
        remove_ids = [
            layer.id()
            for layer in project.mapLayers().values()
            if _is_session_analysis_layer(layer)
        ]
        if remove_ids:
            # Stop a render that may still be drawing these layers; otherwise
            # its finished image can be shown after removal as a ghost.
            self.mainCanvas.stopRendering()
            project.removeMapLayers(remove_ids)
        _prune_empty_output_groups(project.layerTreeRoot())
        if remove_ids:
            _repaint_canvas_without_cache(self.mainCanvas)
            # Repeat once the removal and deleteLater() calls have been
            # processed. The canvas outlives this dialog (plugin unload).
            canvas = self.mainCanvas
            QTimer.singleShot(0, lambda: _repaint_canvas_without_cache(canvas))

    def _add_package_layers(self, package_path, manifest):
        self._remove_package_layers(package_path)
        project = QgsProject.instance()
        root = project.layerTreeRoot()
        plugin_group = _top_level_output_group(root)
        analysis_name = manifest["analysis_name"]
        existing = plugin_group.findGroup(analysis_name)
        if existing is not None:
            plugin_group.removeChildNode(existing)
        analysis_group = plugin_group.insertGroup(0, analysis_name)
        terrain_group = analysis_group.addGroup(tr("Terrain", "地形"))
        outputs = manifest.get("outputs", {})

        def add(member_key, name, group, visible=True, style=None):
            member = outputs.get(member_key)
            if not member:
                return None
            layer = QgsRasterLayer(packages.vsi_path(package_path, member), name)
            if not layer.isValid():
                return None
            layer.setCustomProperty("qlae/package_path", os.path.abspath(package_path))
            layer.setCustomProperty("qlae/session_managed", True)
            if style is not None:
                style(layer)
            project.addMapLayer(layer, False)
            node = group.addLayer(layer)
            node.setItemVisibilityChecked(visible)
            return layer

        add("dem", tr("DEM", "DEM"), terrain_group, False)
        add("hillshade", tr("Hillshade", "陰影起伏"), terrain_group, True, _style_hillshade)
        add("cs_map", tr("CS Map", "CS立体図"), terrain_group, True)
        if "slope" in outputs:
            risk_group = analysis_group.addGroup("災害リスク")
            slope_group = risk_group.addGroup("傾斜")
            slope_group.setExpanded(False)
            add("slope", "災害リスク/傾斜", slope_group, True, _style_morizon_slope)
        analysis_group.setExpanded(True)
        self.previewCanvas.refresh()

    def _bring_to_front(self):
        """Return attention to the analysis window when a result is ready.

        The window is deliberately not always-on-top: users can place it behind
        QGIS while working on the map. Analysis completion brings it back once,
        following the Morizon Next interaction pattern.
        """
        if self.isMinimized():
            self.showNormal()
        self.show()
        self.raise_()
        self.activateWindow()

    def _switch_language(self):
        set_language(ENGLISH if is_japanese() else JAPANESE)
        self.languageChanged.emit()

    def take_state(self):
        normal_geometry = (
            self.normalGeometry()
            if self.isMaximized() or self.isMinimized() or self.isFullScreen()
            else self.geometry()
        )
        state = {
            "normal_geometry": (
                normal_geometry.x(),
                normal_geometry.y(),
                normal_geometry.width(),
                normal_geometry.height(),
            ),
            "was_maximized": self.isMaximized(),
            "was_minimized": self.isMinimized(),
            "dem_source": self.demSourceCombo.currentData(),
            "gsi_dataset": self.gsiDatasetCombo.currentData(),
            "analysis_name": self.analysisNameEdit.text(),
            "saved_analysis_path": self.savedAnalysisCombo.currentData(),
            "outputs": {
                "hillshade": self.hillshadeCheck.isChecked(),
                "slope": self.slopeCheck.isChecked(),
                "cs_map": self.csMapCheck.isChecked(),
            },
            "aoi_layer_id": _layer_id(self.aoiLayerCombo.currentLayer()),
            "data_layer_id": _layer_id(self.dataLayerCombo.currentLayer()),
            "overlay_layer_id": _layer_id(self.overlayLayerCombo.currentLayer()),
            "overlay_opacity": self._overlayOpacityValues[
                self._overlayOpacityIndex
            ],
            "base_map_layer_id": _layer_id(self.baseMapCombo.currentLayer()),
            "base_map_opacity": self._overlayOpacityValues[
                self._baseOpacityIndex
            ],
            "area_mode": (
                "map_extent"
                if self.mapExtentAreaRadio.isChecked()
                else "polygon"
            ),
            "selected_only": self.selectedOnlyCheck.isChecked(),
            "preview_landscape": self._previewLandscape,
            "result_text": self.resultEdit.toPlainText(),
        }
        state.update(self._preview_view_state())
        return state

    def restore_state(self, state):
        # This runs while the replacement language window is still hidden.
        # Keep all top-level geometry changes before its first show().
        source_index = self.demSourceCombo.findData(state.get("dem_source"))
        if source_index >= 0:
            self.demSourceCombo.setCurrentIndex(source_index)
        dataset_index = self.gsiDatasetCombo.findData(state.get("gsi_dataset"))
        if dataset_index >= 0:
            self.gsiDatasetCombo.setCurrentIndex(dataset_index)
        self.analysisNameEdit.setText(state.get("analysis_name", ""))
        outputs = state.get("outputs", {})
        self.hillshadeCheck.setChecked(outputs.get("hillshade", True))
        self.slopeCheck.setChecked(outputs.get("slope", True))
        self.csMapCheck.setChecked(outputs.get("cs_map", True))
        self.selectedOnlyCheck.setChecked(state.get("selected_only", True))
        self.mapExtentAreaRadio.setChecked(
            state.get("area_mode", "polygon") == "map_extent"
        )
        opacity = state.get("overlay_opacity", 100)
        if opacity in self._overlayOpacityValues:
            self._overlayOpacityIndex = self._overlayOpacityValues.index(opacity)
        self.overlayOpacityButton.setText(
            "{}%".format(self._overlayOpacityValues[self._overlayOpacityIndex])
        )
        base_opacity = state.get("base_map_opacity", 100)
        if base_opacity in self._overlayOpacityValues:
            self._baseOpacityIndex = self._overlayOpacityValues.index(base_opacity)
        self.baseOpacityButton.setText(
            "{}%".format(self._overlayOpacityValues[self._baseOpacityIndex])
        )

        project = QgsProject.instance()
        aoi = project.mapLayer(state.get("aoi_layer_id", ""))
        data = project.mapLayer(state.get("data_layer_id", ""))
        overlay = project.mapLayer(state.get("overlay_layer_id", ""))
        base_map = project.mapLayer(state.get("base_map_layer_id", ""))
        if aoi is not None:
            self.aoiLayerCombo.setLayer(aoi)
        if data is not None:
            self.dataLayerCombo.setLayer(data)
        self._refresh_saved_analyses(state.get("saved_analysis_path"))
        self._refresh_preview_layer_choices()
        if overlay is not None:
            self.overlayLayerCombo.setLayer(overlay)
        if base_map is not None:
            self.baseMapCombo.setLayer(base_map)
        self._set_preview_orientation(
            state.get("preview_landscape", False), adjust_window=False
        )
        self._aSeriesLayoutTimer.stop()
        normal_geometry = state.get("normal_geometry")
        if normal_geometry and len(normal_geometry) == 4:
            self.setGeometry(*normal_geometry)
        self._set_hidden_child_geometry()
        self._apply_a_series_layout()
        if state.get("was_maximized"):
            self.setWindowState(
                self.windowState() | Qt.WindowState.WindowMaximized
            )
        elif state.get("was_minimized"):
            self.setWindowState(
                self.windowState() | Qt.WindowState.WindowMinimized
            )
        self.resultEdit.setPlainText(state.get("result_text", ""))
        self._pendingInspectionRefresh = False
        if state.get("preview_extent") or (
            state.get("preview_center") and state.get("preview_scale", 0) > 0
        ):
            # Keep resize events from capturing the canvas's temporary default
            # view before the rebuilt window has completed its first layout.
            self._previewInitialized = False
            self._pendingPreviewView = dict(state)

    def cleanup(self, clear_analysis_layers=False):
        if clear_analysis_layers:
            self._clear_session_analysis_layers()
        if self._terrainWorker is not None:
            self._terrainWorker.cancel()
            self._terrainWorker.wait(5000)
            self._terrainWorker = None
        self._aSeriesLayoutTimer.stop()
        self._windowInteractionTimer.stop()
        self._windowStateTimer.stop()
        self._previewResizeTimer.stop()
        if self._previewFrozenForWindowChange:
            self.previewCanvas.freeze(False)
            self._previewFrozenForWindowChange = False
        if self._previewAoiLayer is not None:
            try:
                self._previewAoiLayer.selectionChanged.disconnect(self._refresh_preview_polygon)
            except (TypeError, RuntimeError):
                pass
            self._previewAoiLayer = None
        for signal, slot in (
            (QgsProject.instance().layersAdded, self._refresh_preview_layer_choices),
            (QgsProject.instance().layersRemoved, self._refresh_preview_layer_choices),
            (
                QgsProject.instance().layerTreeRoot().layerOrderChanged,
                self._refresh_preview_layer_choices,
            ),
            (
                QgsProject.instance().readProject,
                self._apply_stored_preview_layer_choices,
            ),
        ):
            try:
                signal.disconnect(slot)
            except (TypeError, RuntimeError):
                pass
        self.previewRubber.reset(Qgis.GeometryType.Polygon)
        self.previewCanvas.setLayers([])
        if self._previewOverlayLayer is not None:
            self._previewOverlayLayer.deleteLater()
            self._previewOverlayLayer = None
            self._previewOverlaySourceId = None
        if self._previewBaseLayer is not None:
            self._previewBaseLayer.deleteLater()
            self._previewBaseLayer = None
            self._previewBaseSourceId = None


def _route_label(route_id):
    labels = {
        "terrain_raster": tr("Terrain raster (DEM / DSM)", "地形ラスター（DEM / DSM）"),
        "forest_polygons": tr("Forest resource polygons", "森林資源ポリゴン"),
        "reference_vector": tr("Other reference vector", "その他の参照ベクター"),
    }
    return labels[route_id]


def _error_text(code):
    messages = {
        "aoi_required": tr("Choose a polygon area layer.", "範囲となるポリゴンレイヤーを選択してください。"),
        "aoi_must_be_polygon": tr("The area layer must contain polygons.", "範囲レイヤーはポリゴンである必要があります。"),
        "selection_required": tr(
            "Select at least one polygon, or turn off 'selected features only'.",
            "1つ以上のポリゴンを選択するか、「選択地物のみ」を解除してください。",
        ),
        "aoi_has_no_geometry": tr("The chosen area has no usable geometry.", "選択した範囲に使用可能な形状がありません。"),
        "map_extent_required": tr(
            "The preview map extent is not ready.",
            "プレビュー画面の範囲を取得できません。",
        ),
        "data_required": tr("Choose the data layer to pair with the area.", "範囲と組み合わせるデータレイヤーを選択してください。"),
        "data_must_be_raster": tr("This entrance requires a raster layer.", "この入口にはラスターが必要です。"),
        "data_must_be_vector": tr("This entrance requires a vector layer.", "この入口にはベクターが必要です。"),
        "data_must_be_polygon": tr("This entrance requires polygon data.", "この入口にはポリゴンデータが必要です。"),
        "extent_transform_failed": tr(
            "The data extent could not be transformed to the area CRS.",
            "データの領域を範囲レイヤーの座標系へ変換できませんでした。",
        ),
    }
    return messages.get(code, tr("The pair could not be inspected.", "組み合わせを確認できませんでした。"))


def _layer_id(layer):
    return layer.id() if layer is not None else ""


def _repaint_canvas_without_cache(canvas):
    """Redraw a map canvas from scratch, dropping cached layer images."""

    try:
        canvas.stopRendering()
        clear_cache = getattr(canvas, "clearCache", None)
        if clear_cache is not None:
            clear_cache()
        canvas.refresh()
    except RuntimeError:
        # The canvas was deleted (QGIS is closing).
        pass


_PROJECT_SCOPE = "qlae"
_PREVIEW_OVERLAY_KEY = "preview/overlay_layer_id"
_PREVIEW_BASE_MAP_KEY = "preview/base_map_layer_id"


def _stored_preview_layer_id(key):
    """Return the stored layer ID, "" for a stored empty choice, or None."""

    value, ok = QgsProject.instance().readEntry(_PROJECT_SCOPE, key, "")
    return value if ok else None


def _stored_preview_layer(key, candidates):
    """Return (usable, layer) for a preview choice saved in the project."""

    layer_id = _stored_preview_layer_id(key)
    if layer_id is None:
        return False, None
    if not layer_id:
        return True, None
    layer = QgsProject.instance().mapLayer(layer_id)
    if layer is not None and layer in candidates:
        return True, layer
    return False, None


def _is_network_layer(layer):
    """WMS/WMTS/XYZ/WCS/ArcGIS services or URL-backed layers."""

    if layer.providerType().lower() in (
        "wms",
        "wcs",
        "wfs",
        "arcgismapserver",
        "arcgisfeatureserver",
        "vectortile",
    ):
        return True
    source = layer.source().lower()
    return source.startswith(("http://", "https://", "/vsicurl/")) or "url=http" in source


def _is_base_preview_layer(layer):
    """Network-derived layers and all raster layers belong to the lower row."""

    return _is_network_layer(layer) or layer.type() == Qgis.LayerType.Raster


def _plugin_version():
    parser = configparser.ConfigParser(interpolation=None)
    parser.read(os.path.join(os.path.dirname(__file__), "metadata.txt"), encoding="utf-8")
    return parser.get("general", "version", fallback="development")


def _top_level_output_group(root):
    """Return the QLAE group and keep it at the top of the layer panel."""

    plugin_group = next(
        (
            child
            for child in root.children()
            if isinstance(child, QgsLayerTreeGroup) and child.name() == PLUGIN_NAME
        ),
        None,
    )
    if plugin_group is None:
        return root.insertGroup(0, PLUGIN_NAME)
    if root.children() and root.children()[0] is plugin_group:
        return plugin_group

    moved_group = plugin_group.clone()
    root.insertChildNode(0, moved_group)
    root.removeChildNode(plugin_group)
    return moved_group


def _is_session_analysis_layer(layer):
    if bool(layer.customProperty("qlae/permanent", False)):
        return False
    if bool(layer.customProperty("qlae/session_managed", False)):
        return True
    # Compatibility with layers added before the explicit session marker.
    return bool(layer.customProperty("qlae/package_path", ""))


def _prune_empty_output_groups(root):
    plugin_group = next(
        (
            child
            for child in root.children()
            if isinstance(child, QgsLayerTreeGroup) and child.name() == PLUGIN_NAME
        ),
        None,
    )
    if plugin_group is None:
        return

    def prune(group):
        for child in list(group.children()):
            if isinstance(child, QgsLayerTreeGroup):
                prune(child)
                if not child.children():
                    group.removeChildNode(child)

    prune(plugin_group)
    if not plugin_group.children():
        root.removeChildNode(plugin_group)


def _style_hillshade(layer):
    renderer = QgsSingleBandGrayRenderer(layer.dataProvider(), 1)
    layer.setRenderer(renderer)
    layer.setOpacity(0.60)
    composition = getattr(QPainter, "CompositionMode", None)
    if composition is not None:
        layer.setBlendMode(composition.CompositionMode_Multiply)
    else:
        layer.setBlendMode(QPainter.CompositionMode_Multiply)
    layer.triggerRepaint()


def _style_morizon_slope(layer):
    values = (20.0, 25.0, 30.0, 35.0, 40.0, 45.0, float("inf"))
    labels = ("<= 20", "20 - 25", "25 - 30", "30 - 35", "35 - 40", "40 - 45", "> 45")
    items = [
        QgsColorRampShader.ColorRampItem(value, QColor(color), label)
        for value, color, label in zip(values, SLOPE_COLORS, labels)
    ]
    color_shader = QgsColorRampShader(0.0, 90.0)
    discrete = getattr(QgsColorRampShader, "Discrete", None)
    if discrete is None:
        discrete = QgsColorRampShader.Type.Discrete
    color_shader.setColorRampType(discrete)
    color_shader.setColorRampItemList(items)
    raster_shader = QgsRasterShader()
    raster_shader.setRasterShaderFunction(color_shader)
    renderer = QgsSingleBandPseudoColorRenderer(
        layer.dataProvider(), 1, raster_shader
    )
    layer.setRenderer(renderer)
    layer.setOpacity(0.65)
    layer.triggerRepaint()
