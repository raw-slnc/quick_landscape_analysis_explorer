# -*- coding: utf-8 -*-

import configparser
import datetime
import json
import math
import os
import re
import shutil
import sys
import zipfile

from qgis.PyQt import sip
from qgis.PyQt.QtXml import QDomDocument
from qgis.PyQt.QtCore import (
    QCoreApplication,
    QDir,
    QEvent,
    QProcess,
    QRect,
    QRectF,
    QSize,
    QTimer,
    QUrl,
    Qt,
    pyqtSignal,
)
from qgis.PyQt.QtGui import (
    QBrush,
    QColor,
    QCursor,
    QDesktopServices,
    QFont,
    QKeySequence,
    QPainter,
    QPalette,
    QPen,
    QStandardItem,
    QStandardItemModel,
)
from qgis.PyQt.QtWidgets import (
    QButtonGroup,
    QCheckBox,
    QComboBox,
    QDialog,
    QFormLayout,
    QFileDialog,
    QFrame,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QMessageBox,
    QApplication,
    QPushButton,
    QRadioButton,
    QSizePolicy,
    QSplitter,
    QStackedWidget,
    QShortcut,
    QStyle,
    QStyleOptionComboBox,
    QStylePainter,
    QTextEdit,
    QToolButton,
    QToolTip,
    QVBoxLayout,
    QWidget,
)
from qgis.core import (
    Qgis,
    QgsApplication,
    QgsColorRampShader,
    QgsCoordinateTransform,
    QgsCsException,
    QgsDistanceArea,
    QgsGeometry,
    QgsLayerTreeGroup,
    QgsMapLayer,
    QgsMessageLog,
    QgsCoordinateReferenceSystem,
    QgsPointXY,
    QgsProject,
    QgsRasterLayer,
    QgsVectorLayer,
    QgsRasterShader,
    QgsRectangle,
    QgsSettings,
    QgsSingleBandGrayRenderer,
    QgsSingleBandPseudoColorRenderer,
    QgsUnitTypes,
)
from qgis.gui import (
    QgsMapCanvas,
    QgsMapTool,
    QgsMapToolIdentify,
    QgsMapToolPan,
    QgsRubberBand,
)

from .analyses.terrain import WATERSHED_WIDEN, TerrainJobWorker, _a_series_envelope
from .core import packages
from .geopdf_task import GeoPdfTask
from .i18n import ENGLISH, JAPANESE, is_japanese, set_language, tr
from .layer_combo import (
    LayerTreeOrderComboBox,
    add_layer_choice,
    align_layer_choice_labels,
)
from .preview_scale import ScaleLimitSuspender
from . import preview_tiles
from .core import session
from .management_panel import AnalysisManagementPanel
from .order_arrows import LayerOrderArrows
from .plugin import PLUGIN_NAME
from .terrain import dem_sources, remote_dem, watershed
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


class StickyToolTipButton(QToolButton):
    """A tool button whose tooltip stays while the mouse is on it.

    Qt hides a tooltip after a while and when the button is pressed. This
    one shows it at once, for a long time, limited to the button's area (Qt
    hides it when the mouse leaves), and again after a click, so a tooltip
    that changes with each click is seen changing.
    """

    _SHOW_MS = 10 * 60 * 1000

    def event(self, event):
        if event.type() == QEvent.Type.ToolTip:
            self.showStickyToolTip()
            return True
        return super().event(event)

    def enterEvent(self, event):
        super().enterEvent(event)
        self.showStickyToolTip()

    def showStickyToolTip(self):
        if self.underMouse() and self.toolTip():
            QToolTip.showText(QCursor.pos(), self.toolTip(), self, self.rect(), self._SHOW_MS)


class DpiComboBox(QComboBox):
    """Resolution choices, with "Custom..." at the bottom to type one in.

    The list holds the fixed choices, the processing guide of the current
    paper (``setGuide``, marked with "*") and a typed-in value, in order. A
    typed-in value leaves the list once another one is chosen. dpiChosen is
    emitted for the user's choices only (for storing them).
    """

    dpiChosen = pyqtSignal(int)

    def __init__(self, choices, default, parent=None):
        super().__init__(parent)
        self._choices = tuple(choices)
        self._default = default
        self._guide = None
        self._lastIndex = 0
        self._fill([], default)
        self.activated.connect(self._activated)

    def _label(self, dpi):
        text = "{} dpi".format(dpi)
        if dpi == self._guide:
            text += "*"
        if dpi == self._default:
            text += tr(" (QLAE default)", " （QLAE既定値）")
        return text

    def _fill(self, extra, current):
        """Rebuild the list: the choices, the guide and ``extra``, in order."""

        before = self.dpi() if self.count() else None
        values = sorted(set(self._choices) | {v for v in [self._guide] + list(extra) if v})
        self.blockSignals(True)
        self.clear()
        for value in values:
            self.addItem(self._label(value), value)
        self.addItem(tr("Custom...", "自由設定…"), None)
        index = self.findData(current)
        self._lastIndex = index if index >= 0 else 0
        self.setCurrentIndex(self._lastIndex)
        self.blockSignals(False)
        if self.dpi() != before:
            # Only a change of the value is news (the list is rebuilt often).
            self.currentIndexChanged.emit(self._lastIndex)

    def dpi(self):
        value = self.currentData()
        if isinstance(value, int):
            return value
        # "Custom..." is current only while its dialog is open.
        return self.itemData(self._lastIndex)

    def setGuide(self, guide):
        """The processing guide of the paper: the finest resolution that is
        exported without the size warning (marked with "*")."""

        guide = int(guide) if guide else None
        if guide == self._guide:
            return
        current = self.dpi()
        self._guide = guide
        # The current value stays even when it was the earlier guide.
        self._fill([current], current)

    def setDpi(self, dpi):
        dpi = int(dpi)
        if not CUSTOM_DPI_RANGE[0] <= dpi <= CUSTOM_DPI_RANGE[1]:
            return
        self._fill([dpi], dpi)

    def _activated(self, index):
        if self.itemData(index) is not None:
            value = self.itemData(index)
            # A typed-in value leaves the list once another one is chosen.
            self._fill([value], value)
            self.dpiChosen.emit(value)
            return
        value, ok = QInputDialog.getInt(
            self,
            tr("Resolution", "解像度"),
            tr("Resolution (dpi):", "解像度（dpi）："),
            self.itemData(self._lastIndex),
            CUSTOM_DPI_RANGE[0],
            CUSTOM_DPI_RANGE[1],
        )
        if ok:
            self.setDpi(value)
            self.dpiChosen.emit(value)
        else:
            self.setCurrentIndex(self._lastIndex)


class GroupedSourceComboBox(QComboBox):
    """Combo whose list items are indented under group headers.

    The indent only helps in the open list; the closed combo shows the
    chosen item without it.
    """

    popupAboutToShow = pyqtSignal()

    def showPopup(self):
        # Lets the list be brought up to date (which sources can be used).
        self.popupAboutToShow.emit()
        super().showPopup()

    def paintEvent(self, event):
        del event
        painter = QStylePainter(self)
        option = QStyleOptionComboBox()
        self.initStyleOption(option)
        option.currentText = option.currentText.strip()
        painter.drawComplexControl(QStyle.ComplexControl.CC_ComboBox, option)
        painter.drawControl(QStyle.ControlElement.CE_ComboBoxLabel, option)


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
        # The preview draws the project layers themselves; while this window
        # is in front their scale limits are lifted (see preview_scale).
        self._scaleSuspender = ScaleLimitSuspender()
        # Layer-panel visibility changes come in bursts (a group and its
        # children); redraw the preview once after them.
        self._previewLayersTimer = QTimer(self)
        self._previewLayersTimer.setSingleShot(True)
        self._previewLayersTimer.setInterval(0)
        self._previewLayersTimer.timeout.connect(self._update_preview_layers)
        # Set when the layer panel changed while the window was closed.
        self._previewLayersPending = False
        # The project layers in the preview, top first. In analysis
        # management the canvas draws XYZ tile layers from copies fixed to the
        # export's zoom level (see preview_tiles); exports and the temporary
        # changes use these project layers.
        self._previewSourceLayers = []
        # source layer id -> (zoom, copy), and style watches on the sources
        self._previewTileCopies = {}
        self._tileCopyWatches = {}
        # source layer id -> (what decided the zoom, zoom), and the searches
        # under way: source layer id -> (what decides it, ExportZoomFinder)
        self._tileZooms = {}
        self._tileZoomFinders = {}
        # Analysis management: the package being managed, the explore view
        # to return to, and choices waiting for a rebuilt (language) window.
        self._managementPackagePath = None
        self._managementExtent = None
        self._managementMargin = False
        self._explorePreviewView = None
        self._pendingManagement = None
        # Set when this window replaces one in another language: the user
        # was already asked about the previous analysis.
        self._rebuiltForLanguage = False
        # True while an export takes the preview's layers (see _begin_export).
        self._exportStarting = False
        # Last known order of this window and the QGIS main window.
        self._lastInFront = False
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
        # Last normal (not maximised / full-screen) geometry, recorded by the
        # dialog itself. Qt's normalGeometry() can end up screen-sized when a
        # window manager delivers the maximised size before the state change,
        # and restoring to it then leaves the window full-screen sized.
        self._lastNormalGeometry = None
        self._restoringFromScreenState = False
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
        flags = (
            Qt.WindowType.Window
            | Qt.WindowType.WindowMinMaxButtonsHint
            | Qt.WindowType.WindowCloseButtonHint
        )
        if sys.platform == "darwin":
            # macOS: Qt makes a plain window a full-screen "primary" window
            # (NSWindowCollectionBehaviorFullScreenPrimary), so with QGIS in
            # full screen it opens in a full-screen Space of its own. With
            # CustomizeWindowHint (and no full-screen button hint) Qt makes it
            # an "auxiliary" window, shown in the same Space as full-screen
            # QGIS; the green button then zooms instead of going full screen.
            # Customized hints need the title bar hints given explicitly.
            flags |= (
                Qt.WindowType.CustomizeWindowHint
                | Qt.WindowType.WindowTitleHint
                | Qt.WindowType.WindowSystemMenuHint
            )
        self.setWindowFlags(flags)
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
        # Choosing a local DEM (by the user, not restored state) fits the
        # preview to the whole DEM.
        self.demSourceCombo.activated.connect(self._dem_source_activated)
        self.localDemBrowseButton.clicked.connect(self._choose_local_dem_file)
        self.previewCanvas.extentsChanged.connect(self._demAvailabilityTimer.start)
        self.runButton.clicked.connect(self._run_analysis)
        self.loadAnalysisButton.clicked.connect(self._load_selected_analysis)
        self.closeButton.clicked.connect(self._close_and_clear)
        self.languageButton.clicked.connect(self._switch_language)
        self.aoiLayerCombo.layerChanged.connect(self._aoi_layer_changed)
        self.selectedOnlyCheck.toggled.connect(self._refresh_preview_polygon)
        self.polygonAreaRadio.toggled.connect(self._area_mode_changed)
        self.mapExtentAreaRadio.toggled.connect(self._area_mode_changed)
        # Only a user switch clears the selection (not restoring a state).
        self.mapExtentAreaRadio.clicked.connect(self._clear_area_selection_for_extent)
        self.watershedCheck.toggled.connect(self._watershed_toggled)
        self.overlayLayerCombo.layerChanged.connect(self._update_preview_layers)
        self.baseMapCombo.layerChanged.connect(self._update_preview_layers)
        # Only explicit user choices are stored in the project, so automatic
        # defaults do not mark the project as modified.
        # Each combo stores only its own choice, so automatic defaults of the
        # others are not frozen into the project.
        for key, combo in self._stored_layer_combos():
            combo.activated.connect(
                lambda _index, key=key, combo=combo: self._store_layer_choice(key, combo)
            )
        # The on/off buttons: a layer is on when chosen; clicks are kept in
        # the project and given back to the layer (see LayerVisibilityButton).
        for button, combo, key in self._visibility_choices():
            combo.layerChanged.connect(
                lambda *_args, button=button, combo=combo, key=key: button.setWanted(
                    self._stored_on(key, combo)
                )
            )
            combo.activated.connect(
                lambda _index, button=button, combo=combo, key=key: self._layer_chosen(
                    button, combo, key
                )
            )
            button.wantedChosen.connect(lambda on, key=key: self._store_on(key, on))
        # Overlay, base map and optional layers are drawn only while they are
        # shown in the layer panel.
        QgsProject.instance().layerTreeRoot().visibilityChanged.connect(
            self._queue_preview_layers
        )
        # Coming back from another application to the QGIS main window does
        # not change this window's activation (it was inactive already), so
        # follow the application's focus too (see _in_front_of_main_window).
        QApplication.instance().focusWindowChanged.connect(self._focus_window_changed)
        stored_paper = QgsProject.instance().readEntry(_PROJECT_SCOPE, _PAPER_KEY, "")[0]
        if stored_paper in PAPER_SIZES_MM:
            self.paperSizeCombo.setCurrentIndex(self.paperSizeCombo.findData(stored_paper))
        stored_dpi, dpi_ok = QgsProject.instance().readNumEntry(
            _PROJECT_SCOPE, _EXPORT_DPI_KEY, DEFAULT_EXPORT_DPI
        )
        if dpi_ok:
            self.exportDpiCombo.setDpi(stored_dpi)
        stored_geopdf_dpi, geopdf_dpi_ok = QgsProject.instance().readNumEntry(
            _PROJECT_SCOPE, _GEOPDF_DPI_KEY, DEFAULT_GEOPDF_DPI
        )
        if geopdf_dpi_ok:
            self.geoPdfDpiCombo.setDpi(stored_geopdf_dpi)
        self._update_dpi_guides()
        self.paperSizeCombo.currentIndexChanged.connect(self._paper_changed)
        self.paperSizeCombo.activated.connect(
            lambda _i: QgsProject.instance().writeEntry(
                _PROJECT_SCOPE, _PAPER_KEY, self._paper_name()
            )
        )
        self.exportDpiCombo.dpiChosen.connect(
            lambda dpi: QgsProject.instance().writeEntry(_PROJECT_SCOPE, _EXPORT_DPI_KEY, dpi)
        )
        self.geoPdfDpiCombo.dpiChosen.connect(
            lambda dpi: QgsProject.instance().writeEntry(_PROJECT_SCOPE, _GEOPDF_DPI_KEY, dpi)
        )
        # The preview shows the paper output: its tile maps follow the
        # GeoPDF resolution.
        self.geoPdfDpiCombo.currentIndexChanged.connect(self._previewLayersTimer.start)
        self.managementPanel.exitRequested.connect(self._finish_management_by_user)
        self.managementPanel.renameRequested.connect(self._rename_analysis)
        self.managementPanel.layersChanged.connect(self._update_preview_layers)
        self.managementPanel.restoreWindowButton.clicked.connect(
            self._restore_normal_window
        )
        # Swallows pan/zoom input while the preview is fixed to an analysis.
        self.previewCanvas.viewport().installEventFilter(self)
        self.previewCanvas.installEventFilter(self)
        self.previewFrame.canvasResizeStarted.connect(
            self._queue_preview_extent_restore
        )
        self.previewFrame.canvasResized.connect(self._queue_a_series_layout)
        self.previewFrame.canvasResized.connect(self._update_preview_status)
        self.previewCanvas.extentsChanged.connect(self._update_preview_status)
        self.previewCanvas.extentsChanged.connect(self._preview_extent_changed)
        self.mainViewButton.clicked.connect(self._sync_preview_from_main)
        # Layer lists follow the project, once per burst of changes (adding
        # an analysis changes the layer tree many times in a row) and only
        # while the window is shown (see _queue_layer_choices).
        self._layerChoicesTimer = QTimer(self)
        self._layerChoicesTimer.setSingleShot(True)
        self._layerChoicesTimer.setInterval(50)
        self._layerChoicesTimer.timeout.connect(self._refresh_preview_layer_choices)
        self._layerChoicesPending = False
        QgsProject.instance().layersAdded.connect(self._queue_layer_choices)
        QgsProject.instance().layersRemoved.connect(self._queue_layer_choices)
        QgsProject.instance().layerTreeRoot().layerOrderChanged.connect(
            self._queue_layer_choices
        )
        QgsProject.instance().readProject.connect(self._project_was_read)
        self._dem_source_changed()
        self._refresh_saved_analyses()
        self._refresh_preview_layer_choices()
        self._aoi_layer_changed(self.aoiLayerCombo.currentLayer())
        self._area_mode_changed()
        self._update_management_controls()
        stored_landscape = self._stored_preview_landscape()
        if stored_landscape:
            # Before the first show, so the window width is computed for it.
            self._set_preview_orientation(True, adjust_window=False)
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

        # Where the chosen local DEM file holds values: thin line, light hatch.
        self.localDemRubber = QgsRubberBand(
            self.previewCanvas, Qgis.GeometryType.Polygon
        )
        self.localDemRubber.setStrokeColor(QColor(30, 90, 200, 170))
        self.localDemRubber.setFillColor(QColor(30, 90, 200, 110))
        self.localDemRubber.setBrushStyle(Qt.BrushStyle.DiagCrossPattern)
        self.localDemRubber.setWidth(1)

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
        # The pair of icons QGIS print layouts use to match a map item and
        # the main map: arrow into the frame here, out of it in analysis
        # management (toMainViewButton).
        self.mainViewButton.setIcon(
            QgsApplication.getThemeIcon("/mActionSetToCanvasExtent.svg")
        )
        self.mainViewButton.setAutoRaise(True)
        self.mainViewButton.setFixedSize(26, 26)
        self.mainViewButton.setToolTip(
            tr(
                "Get the centre, CRS and scale from the QGIS main map.",
                "QGISメイン地図から中心・座標系・縮尺を取得します。",
            )
        )
        # The preview is a reduced picture of this sheet: line widths and the
        # scale shown follow the paper, and the GeoPDF is made on it.
        self.paperSizeCombo = QComboBox(status_row)
        for name in PAPER_SIZES_MM:
            self.paperSizeCombo.addItem(name, name)
        self.paperSizeCombo.setCurrentIndex(self.paperSizeCombo.findData(DEFAULT_PAPER))
        self.paperSizeCombo.setToolTip(
            tr(
                "Paper size. The preview shows this sheet reduced; the GeoPDF and the "
                "image use it.",
                "紙の大きさ。プレビューはこの紙の縮小表示で、GeoPDFと画像もこの大きさで作ります。",
            )
        )
        status_layout.addWidget(self.previewStatusLabel, 1)
        status_layout.addWidget(self.paperSizeCombo)
        status_layout.addWidget(self.previewOrientationButton)
        status_layout.addWidget(
            self.previewToolPanel, 0, Qt.AlignmentFlag.AlignRight
        )
        status_layout.addWidget(self.mainViewButton)
        # Analysis management: the preview is fixed to the analysis, so the
        # sync goes the other way, from the preview to the main map.
        self.toMainViewButton = QToolButton(status_row)
        self.toMainViewButton.setIcon(
            QgsApplication.getThemeIcon("/mActionViewExtentInCanvas.svg")
        )
        self.toMainViewButton.setAutoRaise(True)
        self.toMainViewButton.setFixedSize(26, 26)
        self.toMainViewButton.setToolTip(
            tr(
                "Show the preview's area in the QGIS main map.",
                "QGISメイン地図をプレビューの範囲に合わせます。",
            )
        )
        self.toMainViewButton.setVisible(False)
        self.toMainViewButton.clicked.connect(self._sync_main_from_preview)
        status_layout.addWidget(self.toMainViewButton)
        preview_layout.addWidget(status_row)

        layout.addWidget(preview_box, 1)
        self.exportBox = self._build_export_box()
        layout.addWidget(self.exportBox)
        # The developer's name, as in Forestry Operations Lite (not translated).
        self.pluginCreditLabel = QLabel("Developed by Avid Tree Work", panel)
        self.pluginCreditLabel.setStyleSheet("color: #888; font-size: 8pt;")
        self.pluginCreditLabel.setAlignment(
            Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
        )
        layout.addWidget(self.pluginCreditLabel)
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
        box = QGroupBox(tr("Export", "エクスポート"))
        grid = QGridLayout(box)
        self.exportLayoutButton = QPushButton(tr("Print layout", "印刷レイアウト"), box)
        self.exportLayoutButton.setToolTip(
            tr(
                "Make a QGIS print layout of this analysis (map, legend, scale bar) "
                "and open it, for PDFs and printing.",
                "この解析のQGIS印刷レイアウト（地図・凡例・縮尺記号など）を作って開きます。"
                "PDFや印刷に使います。",
            )
        )
        self.exportLayoutButton.clicked.connect(self._export_print_layout)
        self.exportGeoPdfButton = QPushButton(tr("GeoPDF", "GeoPDF"), box)
        self.exportGeoPdfButton.setToolTip(
            tr(
                "Save what the preview shows as a GeoPDF on the chosen paper size "
                "(for Avid Maps Extra and similar apps).",
                "プレビューに見えているものを、選んだ紙の大きさのGeoPDFにします"
                "（Avid Maps Extraなど用）。",
            )
        )
        self.exportGeoPdfButton.clicked.connect(self._export_geopdf)
        self.exportLayersButton = QPushButton(tr("Layers", "レイヤー"), box)
        self.exportLayersButton.setToolTip(
            tr(
                "Write the files of this analysis, with their styles, to the exports "
                "folder and put them in the layer panel as ordinary layers.",
                "この解析のファイルをスタイルと一緒にexportsフォルダへ書き出し、"
                "普通のレイヤーとしてレイヤーパネルに置きます。",
            )
        )
        self.exportLayersButton.clicked.connect(self._export_layers)
        # The image button names its format; the button at its right switches
        # the format in a loop (PNG, JPG, TIF), remembered for the next time.
        self.exportImageButton = QPushButton(box)
        self.exportImageButton.setToolTip(
            tr(
                "Save what the preview shows as an image on the chosen paper size "
                "and resolution, with a world file. The button at the right "
                "switches the format.",
                "プレビューに見えているものを、選んだ紙の大きさと解像度の画像にします"
                "（位置情報のワールドファイル付き）。形式は右のボタンで切り替えます。",
            )
        )
        self.exportImageButton.clicked.connect(self._export_image)
        self.imageFormatButton = StickyToolTipButton(box)
        self.imageFormatButton.setIcon(QgsApplication.getThemeIcon("/mActionRefresh.svg"))
        # Its tooltip (with the current format's character) follows the format.
        self.imageFormatButton.clicked.connect(self._next_image_format)
        self.imageFormatButton.setFixedHeight(self.exportImageButton.sizeHint().height())
        self._imageFormat = _stored_image_format()
        self._update_image_button()
        image_cell = QHBoxLayout()
        image_cell.setSpacing(4)
        image_cell.addWidget(self.exportImageButton, 1)
        image_cell.addWidget(self.imageFormatButton)
        self._imageTask = None
        self._imageTaskLayers = []
        self._geoPdfTask = None
        grid.addWidget(self.exportGeoPdfButton, 0, 0)
        grid.addLayout(image_cell, 0, 1)
        grid.addWidget(self.exportLayersButton, 1, 0)
        grid.addWidget(self.exportLayoutButton, 1, 1)
        # One resolution each, in the columns of their buttons. A QGIS print
        # layout keeps its own export resolution (300 dpi unless changed).

        def dpi_column(label, default, tooltip):
            row = QHBoxLayout()
            row.addWidget(QLabel(label, box))
            combo = DpiComboBox(EXPORT_DPI_CHOICES, default, box)
            combo.setToolTip(tooltip + "\n" + tr(
                "*: the guideline processing limit: the finest resolution this paper is "
                "exported at without the size warning (it changes with the paper).",
                "*：処理上限の目安です。この用紙で、大きさの警告なしに書き出せる"
                "いちばん細かい解像度です（用紙で変わります）。",
            ))
            row.addWidget(combo, 1)
            return row, combo

        geopdf_row, self.geoPdfDpiCombo = dpi_column(
            tr("GeoPDF resolution", "GeoPDF 解像度"),
            DEFAULT_GEOPDF_DPI,
            tr(
                "Resolution of the GeoPDF's image parts (hillshade, base map, ...). "
                "The preview's tile maps follow it.",
                "GeoPDF の画像部分（陰影起伏・ベースマップなど）の解像度です。"
                "プレビューのタイル地図もこれに合わせます。",
            ),
        )
        image_row, self.exportDpiCombo = dpi_column(
            tr("Image resolution", "画像 解像度"),
            DEFAULT_EXPORT_DPI,
            tr("Resolution of the exported image.", "書き出す画像の解像度です。"),
        )
        grid.addLayout(geopdf_row, 2, 0)
        grid.addLayout(image_row, 2, 1)
        return box

    def _build_control_panel(self):
        panel = QWidget(self)
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(0, 0, 0, 0)

        # The upper part switches between the analysis inputs and the
        # analysis-management panel. Preview layers, loading and the bottom
        # buttons below it are shared by both.
        self.controlStack = QStackedWidget(panel)
        explore_page = QWidget(self.controlStack)
        explore_layout = QVBoxLayout(explore_page)
        explore_layout.setContentsMargins(0, 0, 0, 0)
        self._build_explore_page(explore_layout)
        self.controlStack.addWidget(explore_page)
        self.managementPanel = AnalysisManagementPanel(self.controlStack)
        self.controlStack.addWidget(self.managementPanel)
        layout.addWidget(self.controlStack)
        # The preview layers follow the screen's own layer choices directly.
        layout.addLayout(self._build_preview_layer_area(panel))
        # One label width for the optional-layer and preview-layer rows.
        align_layer_choice_labels(
            self.managementPanel.optionalLabels() + self._previewLayerLabels
        )
        # Shared status at the bottom: cleared on every screen switch so it
        # always describes the screen and data currently in use.
        layout.addWidget(self._build_result_box(), 1)
        layout.addLayout(self._build_buttons())
        return panel

    def _build_explore_page(self, layout):
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

    def _build_preview_layer_area(self, parent):
        """Stack the preview-layer row and the saved-analyses row."""

        area = QVBoxLayout()
        box = QGroupBox(tr("Preview layers", "プレビューレイヤー"), parent)
        row_layout = QHBoxLayout(box)

        # Built like the optional-layer row of analysis management, which
        # sits right above this row: the labels get one width (see
        # _build_right_panel), so the combos and buttons line up.
        self.overlayLayerCombo = LayerTreeOrderComboBox(box)
        self.overlayLayerCombo.setAllowEmptyLayer(True)
        overlay_label, self.overlayVisibilityButton = add_layer_choice(
            row_layout, tr("Overlay", "オーバーレイ"), self.overlayLayerCombo, box
        )
        self.baseMapCombo = LayerTreeOrderComboBox(box)
        self.baseMapCombo.setAllowEmptyLayer(True)
        base_map_label, self.baseVisibilityButton = add_layer_choice(
            row_layout, tr("Base map", "ベースマップ"), self.baseMapCombo, box
        )
        self._previewLayerLabels = (overlay_label, base_map_label)

        self.baseMapBox = box
        area.addWidget(box)
        area.addWidget(self._build_saved_analyses_box(parent))
        return area

    def showEvent(self, event):
        super().showEvent(event)
        QTimer.singleShot(0, self._finish_initial_layout)
        if self._layerChoicesPending:
            # The project's layers changed while the window was closed.
            self._layerChoicesPending = False
            self._layerChoicesTimer.start()
        if self._previewLayersPending:
            self._previewLayersPending = False
            self._previewLayersTimer.start()

    def _queue_preview_layers(self, *args):
        """Layer panel on/off changed: redraw the preview while it is shown."""

        del args
        if not self._alive():
            return
        if self.isVisible():
            self._previewLayersTimer.start()
        else:
            self._previewLayersPending = True

    def _queue_layer_choices(self, *args):
        """The project's layers changed: bring the layer lists up to date."""

        del args
        if not self._alive():
            return
        if self.isVisible():
            self._layerChoicesTimer.start()
        else:
            self._layerChoicesPending = True

    def resizeEvent(self, event):
        super().resizeEvent(event)
        # A window maximised by the window manager without a state change
        # still needs the restore button.
        QTimer.singleShot(0, self._update_window_restore_button)
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
        if event.type() == QEvent.Type.ActivationChange:
            # Decide after the switch has finished: the window that takes the
            # focus may be one of this window's own dialogs.
            QTimer.singleShot(0, self._update_scale_suspension)
        if event.type() == QEvent.Type.WindowStateChange:
            screen_states = (
                Qt.WindowState.WindowMaximized | Qt.WindowState.WindowFullScreen
            )
            if (event.oldState() & screen_states) and not (
                self.windowState() & screen_states
            ):
                # Leaving maximised / full screen: put the recorded normal
                # geometry back once the state has settled.
                self._restoringFromScreenState = True
            self._windowStateSettling = True
            self._windowStateTimer.start()
            QTimer.singleShot(0, self._update_window_restore_button)

    def _update_window_restore_button(self):
        if not hasattr(self, "restoreWindowButton"):
            return
        # Also when the window fills the screen while Qt reports "normal":
        # on Xfce a window maximised from the title bar or a shortcut can be
        # out of step with Qt's state, and then needs this button too.
        show_restore = self._showLinuxRestoreControl and (
            self.isMaximized() or self.isFullScreen() or self._fills_available_screen()
        )
        self.restoreWindowButton.setVisible(show_restore)
        self.managementPanel.restoreWindowButton.setVisible(show_restore)

    def _restore_normal_window(self):
        if self.isMaximized() or self.isFullScreen():
            # The state change then runs the restore check after settling.
            self.showNormal()
            self.raise_()
            self.activateWindow()
        elif self._fills_available_screen():
            # Qt already reports "normal" (out of step with the window
            # manager), so no state change will follow: restore directly.
            self._restore_recorded_normal_geometry()

    def _close_and_clear(self):
        # The analysis layers stay in the project (and in the print layout);
        # they are swapped only when another analysis is loaded or run.
        self.save_window_geometry()
        self.hide()
        # Closing ends the plugin's temporary changes to the project layers
        # (the on/off choices themselves are kept).
        for button, _in_use in self._visibility_buttons():
            button.setApplied(False)
        self._scaleSuspender.restore()

    def reject(self):
        """Esc closes the window; the analysis layers stay."""

        self._close_and_clear()

    def _finish_window_state_transition(self):
        self._windowStateSettling = False
        self._aSeriesLayoutTimer.stop()
        if self._restoringFromScreenState:
            self._restoringFromScreenState = False
            self._restore_recorded_normal_geometry()
        self._apply_a_series_layout()
        self._record_normal_geometry()
        if self.isVisible() and not self.isMinimized():
            # Minimising alone should not forget that it was maximised.
            self.save_window_geometry()

    def _in_screen_state(self):
        return bool(
            self.windowState()
            & (
                Qt.WindowState.WindowMinimized
                | Qt.WindowState.WindowMaximized
                | Qt.WindowState.WindowFullScreen
            )
        )

    def _record_normal_geometry(self):
        """Remember the geometry of a settled, normal, smaller-than-screen window."""

        if (
            self.isVisible()
            and not self._in_screen_state()
            and not self._windowStateSettling
            and not self._fills_available_screen()
        ):
            self._lastNormalGeometry = QRect(self.geometry())

    def _restore_recorded_normal_geometry(self):
        """After leaving maximised / full screen, return to the recorded size.

        Normally the window manager has already restored the window and this
        does nothing. It acts only when the window is still screen-sized.
        Measured on Xfce (xfwm4): Qt then reports a normal window while xfwm4
        still holds it maximised (a screen-sized window is maximised again by
        the window manager), and it refuses every resize. Showing the window
        again at the recorded geometry maps it afresh as a normal window and
        brings Qt and the window manager back in step (one brief flicker).
        """

        geometry = self._lastNormalGeometry
        if (
            geometry is None
            or self._in_screen_state()
            or not self._fills_available_screen()
        ):
            return
        _frame_height, _preview_width, required_width = (
            self._a_series_dimensions_for_height(geometry.height())
        )
        # Lower the width minimum first; the screen-sized minimum would
        # otherwise stop the window from shrinking.
        self.setMinimumWidth(min(required_width, self._available_screen_width()))
        self._adjustingWindowWidth = True
        try:
            self.hide()
            self.setWindowState(Qt.WindowState.WindowNoState)
            self.setGeometry(geometry)
            self.show()
            self.raise_()
            self.activateWindow()
        finally:
            self._adjustingWindowWidth = False

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
            self._record_normal_geometry()
            # Saved as soon as a move/resize settles, so the place is kept
            # even when QGIS quits with the window open.
            self.save_window_geometry()
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
        self._record_normal_geometry()
        if self._pendingPreviewView is not None:
            saved = self._pendingPreviewView
            self._pendingPreviewView = None
            QTimer.singleShot(0, lambda state=saved: self._restore_preview_view(state))
        elif not self._previewInitialized:
            self._sync_preview_from_main()
        if self._pendingManagement:
            # Queued after the explore view restore above.
            QTimer.singleShot(0, self._resume_pending_management)
        elif not self._rebuiltForLanguage:
            QTimer.singleShot(0, self._offer_previous_analysis)

    def _offer_previous_analysis(self):
        """Offer to open the analysis whose layers are in the project."""

        if (
            self._in_management()
            or self._terrainWorker is not None
            or getattr(self, "_offeringPrevious", False)
        ):
            return
        paths = [path for path in session.session_package_paths() if os.path.isfile(path)]
        if not paths:
            return
        path = paths[0]
        try:
            name = packages.read_manifest(path).get("analysis_name") or os.path.basename(path)
        except packages.PackageError:
            return
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Icon.Question)
        box.setWindowTitle(tr("Previous analysis", "前の解析"))
        box.setText(tr(
            "The layers of the previous analysis “{}” are in the project. Open it?",
            "前の解析「{}」のレイヤーがプロジェクトにあります。開きますか？",
        ).format(name))
        open_button = box.addButton(tr("Open", "開く"), QMessageBox.ButtonRole.AcceptRole)
        empty_button = box.addButton(
            tr("Start empty", "空で始める"), QMessageBox.ButtonRole.RejectRole
        )
        box.setDefaultButton(open_button)
        box.setEscapeButton(empty_button)
        self._offeringPrevious = True
        try:
            box.exec()
        finally:
            self._offeringPrevious = False
        if box.clickedButton() is open_button and self._enter_management(path):
            index = self.savedAnalysisCombo.findData(path)
            if index >= 0:
                self.savedAnalysisCombo.setCurrentIndex(index)
        elif box.clickedButton() is empty_button:
            # Starting empty, like finishing analysis management: take the
            # analysis layers out of the project (the ZIPs stay).
            self._remove_session_layers()

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

    # ----- Paper and export ------------------------------------------------

    def _paper_name(self):
        return self.paperSizeCombo.currentData() or DEFAULT_PAPER

    def _paper_size_mm(self):
        """(width, height) of the sheet in the current orientation."""

        short, long_ = PAPER_SIZES_MM[self._paper_name()]
        return (long_, short) if self._previewLandscape else (short, long_)

    def _sync_paper_magnification(self):
        """Draw the preview as the chosen sheet reduced to the frame.

        The canvas DPI is set so that its width stands for the paper width:
        line widths and text in mm then look as on the paper, and the scale
        QGIS reports is the scale on the paper. Callers set the extent next.
        """
        canvas = self.previewCanvas
        width_px = canvas.mapSettings().outputSize().width()
        if width_px <= 0:
            return
        device_dpi = canvas.mapSettings().outputDpi() / max(canvas.magnificationFactor(), 1e-9)
        if device_dpi <= 0:
            return
        paper_width_in = self._paper_size_mm()[0] / 25.4
        factor = (width_px / paper_width_in) / device_dpi
        # QGIS limits the magnification (0.1 at least); remember the wanted
        # value so the scale shown can still be the scale on the paper.
        self._desiredMagnification = factor
        if abs(factor - canvas.magnificationFactor()) > 1e-4:
            # The canvas reuses layer and label images while the extent and
            # the map units per pixel stay the same, whatever the DPI
            # (QgsMapRendererCache::updateParameters in 3.44): with the extent
            # fixed, labels kept the earlier sheet's size. Draw them anew.
            canvas.clearCache()
            canvas.setMagnificationFactor(factor)

    def _set_preview_extent(self, extent):
        """Show exactly this extent in the preview.

        With a magnification factor set, QgsMapCanvas widens an extent it is
        given by 1 / factor (measured with QGIS 3.44), so pass it pre-shrunk.
        """
        rectangle = QgsRectangle(extent)
        factor = self.previewCanvas.magnificationFactor()
        if factor > 0 and abs(factor - 1.0) > 1e-9:
            rectangle.scale(factor)
        self.previewCanvas.setExtent(rectangle)

    def _paper_changed(self, *args):
        del args
        extent = QgsRectangle(self._stablePreviewExtent or self.previewCanvas.extent())
        if self._in_management():
            self._fit_preview_to_management_extent()
        else:
            self._sync_paper_magnification()
            if not extent.isEmpty():
                self._set_preview_extent(extent)
        self.previewCanvas.refresh()
        self._update_preview_status()
        self._update_dpi_guides()
        # The tile layers' zoom follows the new scale.
        self._previewLayersTimer.start()

    def _update_dpi_guides(self):
        """The resolution lists mark the processing guide of the paper."""

        guide = self._processing_limit_dpi()
        for combo in (self.geoPdfDpiCombo, self.exportDpiCombo):
            combo.setGuide(guide)

    def _processing_limit_dpi(self):
        """The plugin's processing limit for the paper: the finest
        resolution, in 50 dpi steps, whose sheet stays within
        LARGE_EXPORT_PIXELS. A limit of size, not a quality advice."""

        width_mm, height_mm = self._paper_size_mm()
        square_inches = (width_mm / 25.4) * (height_mm / 25.4)
        finest = math.sqrt(LARGE_EXPORT_PIXELS / square_inches)
        return max(LIMIT_DPI_STEP, int(finest // LIMIT_DPI_STEP) * LIMIT_DPI_STEP)

    def _checked_export_dpi(self, combo, title, size_text, allow_as_is):
        """The resolution to export at, or None to stop.

        Above LARGE_EXPORT_PIXELS the user chooses: as it is (unless over
        the plugin's own limit, allow_as_is False), the processing limit
        (which then becomes the setting), or cancel.
        """

        dpi = combo.dpi()
        width_mm, height_mm = self._paper_size_mm()
        width_px = int(round(width_mm / 25.4 * dpi))
        height_px = int(round(height_mm / 25.4 * dpi))
        if allow_as_is and width_px * height_px <= LARGE_EXPORT_PIXELS:
            return dpi
        limit = self._processing_limit_dpi()
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Icon.Warning)
        box.setWindowTitle(title)
        box.setText(size_text.format(self._paper_name(), dpi, width_px, height_px))
        box.setInformativeText(
            tr(
                "This takes a long time and a lot of memory, so the plugin "
                "sets a guideline processing limit."
                if allow_as_is
                else "This takes a long time and a lot of memory, so the plugin "
                "limits one image to about 2 GB.",
                "時間とメモリを多く使うためプラグインで処理上限の目安を設けています。"
                if allow_as_is
                else "時間とメモリを多く使うためプラグインで1枚の画像を約2 GBまでに制限しています。",
            )
        )
        as_is = None
        if allow_as_is:
            as_is = box.addButton(
                tr("Export as it is", "このまま書き出す"), QMessageBox.ButtonRole.AcceptRole
            )
        use_limit = box.addButton(
            tr(
                "Export at the processing limit ({} dpi)",
                "処理上限（{} dpi）で書き出す",
            ).format(limit),
            QMessageBox.ButtonRole.AcceptRole,
        )
        box.addButton(QMessageBox.StandardButton.Cancel)
        box.setDefaultButton(use_limit)
        _keep_message_lines(box)
        box.exec()
        clicked = box.clickedButton()
        if as_is is not None and clicked is as_is:
            return dpi
        if clicked is use_limit:
            combo.setDpi(limit)
            combo.dpiChosen.emit(limit)
            return limit
        return None

    def _show_status(self, text):
        """Put text in the Status panel and draw it now.

        Exports replace the previous result as soon as they start, and a
        step that keeps QGIS busy (the GeoPDF) would otherwise leave the
        old text on screen until it ends.
        """

        self.resultEdit.setPlainText(text)
        self.resultEdit.viewport().repaint()

    def _export_geopdf(self, *args):
        """Save exactly what the preview shows as a GeoPDF on the paper size."""

        del args
        if self._geoPdfTask is not None:
            return
        self._show_status(tr("Exporting a GeoPDF...", "GeoPDFを書き出します…"))
        cancelled = tr("The GeoPDF export was cancelled.", "GeoPDFの書き出しを取り消しました。")
        if not self._confirm_wms_export():
            self._show_status(cancelled)
            return
        dpi = self._checked_export_dpi(
            self.geoPdfDpiCombo,
            tr("Large GeoPDF", "大きなGeoPDF"),
            tr(
                "{} at {} dpi makes images of {:,} × {:,} pixels.",
                "{}・{} dpiでは画像部分が{:,} × {:,}画素になります。",
            ),
            allow_as_is=True,
        )
        if dpi is None:
            self._show_status(cancelled)
            return
        width_mm, height_mm = self._paper_size_mm()
        width_px = int(round(width_mm / 25.4 * dpi))
        height_px = int(round(height_mm / 25.4 * dpi))
        settings = QgsSettings()
        name = self.managementPanel.analysisNameLabel.text() or "preview"
        start_dir = settings.value(
            "qlae/geopdf_dir", QgsProject.instance().absolutePath(), type=str
        )
        path, _filter = QFileDialog.getSaveFileName(
            self,
            tr("Save GeoPDF", "GeoPDFを保存"),
            os.path.join(start_dir or "", "{}_{}.pdf".format(name, self._paper_name())),
            "PDF (*.pdf)",
        )
        if not path:
            self._show_status(cancelled)
            return
        if not path.lower().endswith(".pdf"):
            path += ".pdf"
        settings.setValue("qlae/geopdf_dir", os.path.dirname(path))
        self._start_geopdf_task(path, dpi, width_px, height_px)

    def _start_geopdf_task(self, path, dpi, width_px, height_px):
        """Make the GeoPDF in the background (see geopdf_task)."""

        del width_px, height_px
        source_settings = self.previewCanvas.mapSettings()
        heading = tr(
            "Making GeoPDF ({}, {} dpi)", "GeoPDFを作成しています（{}・{} dpi）"
        ).format(self._paper_name(), dpi)
        self._show_status(heading + tr("...", "…"))
        # The layers are cloned with the temporary changes in them.
        self._begin_export()
        try:
            task = GeoPdfTask(
                path,
                self._paper_size_mm(),
                dpi,
                self._previewSourceLayers,
                self.previewCanvas.extent(),
                source_settings.destinationCrs(),
                source_settings.transformContext(),
                source_settings.flags(),
            )
        finally:
            self._end_export()

        def part_started(number, total, name, labels):
            if sip.isdeleted(self):
                return
            what = (
                tr("labels of “{}”", "「{}」のラベル") if labels else tr("“{}”", "「{}」")
            ).format(name)
            self._show_status(
                heading + "\n" + tr("Drawing {}/{}: {}", "描画 {}/{}：{}").format(number, total, what)
            )

        def composing():
            if not sip.isdeleted(self):
                self._show_status(heading + "\n" + tr(
                    "Putting the layers together into one PDF...",
                    "レイヤーを1つのPDFにまとめています…",
                ))

        task.partStarted.connect(part_started)
        task.composing.connect(composing)
        task.taskCompleted.connect(lambda: self._geopdf_task_finished(task, True))
        task.taskTerminated.connect(lambda: self._geopdf_task_finished(task, False))
        self._geoPdfTask = task
        self.exportGeoPdfButton.setEnabled(False)
        QgsApplication.taskManager().addTask(task)

    def _geopdf_task_finished(self, task, ok):
        self._geoPdfTask = None
        if sip.isdeleted(self):
            return
        self.exportGeoPdfButton.setEnabled(True)
        if ok:
            self._show_status(tr("GeoPDF saved: {}", "GeoPDFを保存しました：{}").format(task.path))
        elif task.isCanceled():
            self._show_status(tr("The GeoPDF export was cancelled.", "GeoPDFの書き出しを取り消しました。"))
        else:
            self._show_status(
                tr("The GeoPDF could not be made: {}", "GeoPDFを作成できませんでした：{}").format(
                    task.errorMessage
                )
            )

    def _export_layers(self, *args):
        """Write the analysis files out of the ZIP and place them as layers.

        The files go to exports/<analysis name>/ beside the analyses folder,
        each with its style (.qml of the same name, which QGIS also reads when
        the file is added by hand). The layers are ordinary layers, apart from
        the plugin's group: the plugin never removes or swaps them.
        """
        del args
        package_path = self._managementPackagePath
        if not package_path:
            return
        self._show_status(tr("Exporting the layers...", "レイヤーを書き出しています…"))
        project = QgsProject.instance()
        folder = packages.export_directory(package_path)
        folder_key = os.path.normcase(os.path.abspath(folder)) + os.sep
        placed = [
            layer for layer in project.mapLayers().values()
            if os.path.normcase(os.path.abspath(
                layer.source().split("|", 1)[0]
            )).startswith(folder_key)
        ]
        if placed:
            # Files in use cannot be replaced (Windows); one export at a time.
            self.resultEdit.setPlainText(tr(
                "Already exported. The layers are in the project; the files are in\n{}",
                "既にエクスポート済です。レイヤーはプロジェクトにあり、ファイルは次の場所です。\n{}",
            ).format(folder))
            return
        try:
            manifest = packages.read_manifest(package_path)
        except packages.PackageError as error:
            self.resultEdit.setPlainText(str(error))
            return
        name = manifest.get("analysis_name") or os.path.splitext(os.path.basename(package_path))[0]
        rewritten = os.path.isdir(folder) and bool(os.listdir(folder))
        try:
            written = _extract_analysis_files(package_path, manifest, folder)
        except (OSError, zipfile.BadZipFile) as error:
            self.resultEdit.setPlainText(tr(
                "The layers could not be exported: {}", "レイヤーを書き出せませんでした：{}"
            ).format(error))
            return
        # Same order, look and visibility as the analysis group.
        group_node = _package_layer_group(package_path)
        sources = []
        if group_node is not None:
            for node in group_node.findLayers():
                if node.layer() is not None:
                    parent = node.parent()
                    sub = parent.name() if parent is not group_node else ""
                    sources.append((node.layer(), node.isVisible(), sub))
        root = project.layerTreeRoot()
        plugin_first = bool(root.children()) and isinstance(
            root.children()[0], QgsLayerTreeGroup
        ) and root.children()[0].name() == PLUGIN_NAME
        group = root.insertGroup(1 if plugin_first else 0, name)
        sub_groups = {}
        count = 0
        for source, visible, sub in sources:
            member = _layer_member(source, package_path)
            path = written.get(member)
            if not path:
                continue
            style_path = os.path.splitext(path)[0] + ".qml"
            # Without the custom properties: they hold the plugin's markers,
            # which would make the plugin treat these files as its own.
            source.saveNamedStyle(style_path, categories=_style_without_custom_properties())
            if source.type() == Qgis.LayerType.Vector:
                layer = QgsVectorLayer(path, source.name(), "ogr")
            else:
                layer = QgsRasterLayer(path, source.name())
            if not layer.isValid():
                continue
            layer.loadNamedStyle(style_path)
            for key in ("qlae/package_path", "qlae/session_managed", "qlae/permanent"):
                layer.removeCustomProperty(key)
            project.addMapLayer(layer, False)
            # The same sub groups as the analysis group (地形, データ).
            target = group
            if sub:
                target = sub_groups.get(sub)
                if target is None:
                    target = sub_groups[sub] = group.addGroup(sub)
            node = target.addLayer(layer)
            node.setItemVisibilityChecked(visible)
            node.setExpanded(False)
            count += 1
        group.setExpanded(True)
        _write_layout_record(folder, {
            "analysis_name": name,
            "landscape": bool(self._previewLandscape),
            "extent": _rectangle_values(self.previewCanvas.extent()),
            "crs_wkt": self.previewCanvas.mapSettings().destinationCrs().toWkt(),
            "description": _layout_description(manifest),
            "credits": _layout_credits(manifest, [], []),
            "summary": _safe_area_summary(package_path, manifest),
        })
        self.resultEdit.setPlainText(
            (tr("Exported the layers again.", "レイヤーを書き出し直しました。") if rewritten
             else tr("Exported the layers.", "レイヤーを書き出しました。"))
            + "\n" + tr(
                "Place: {}\n"
                "The files of the analysis were written to this folder with their styles "
                "(.qml), and {} layer(s) were put in the layer panel group “{}”.\n"
                "These are ordinary layers: the plugin does not remove or replace them.",
                "場所：{}\n"
                "解析のファイルをスタイル（.qml）と一緒にこのフォルダに書き出し、"
                "{}個のレイヤーをレイヤーパネルのグループ「{}」に置きました。\n"
                "これらは普通のレイヤーで、プラグインは消したり入れ替えたりしません。",
            ).format(folder, count, name)
        )

    def _print_layout_layers(self):
        """Project layers behind the preview, top to bottom.

        (layers for the map, analysis layers for the legend)
        """
        project = QgsProject.instance()
        layers = []
        analysis = []
        # The preview's order: optional 1, optional 2, overlay, analysis,
        # base map.
        for combo in [c for _key, c in self._optional_layer_combos()] + [
            self.overlayLayerCombo
        ]:
            layer = combo.currentLayer()
            if layer is not None and layer not in layers:
                layers.append(layer)
        for layer_id in self.managementPanel.checkedLayerIds():
            layer = project.mapLayer(layer_id)
            if layer is not None and layer not in layers:
                layers.append(layer)
                analysis.append(layer)
        layer = self.baseMapCombo.currentLayer()
        if layer is not None and layer not in layers:
            layers.append(layer)
        return layers, analysis

    def _export_print_layout(self, *args):
        """Make a print layout of the analysis and open it in QGIS."""

        del args
        package_path = self._managementPackagePath
        if not package_path:
            return
        self._show_status(tr("Making the print layout...", "印刷レイアウトを作成します…"))
        try:
            manifest = packages.read_manifest(package_path)
        except packages.PackageError:
            manifest = {}
        name = self.managementPanel.analysisNameLabel.text() or manifest.get(
            "analysis_name", "QLAE"
        )
        layers, legend_layers = self._print_layout_layers()
        status = make_print_layout(
            self,
            self.iface,
            name=name,
            landscape=self._previewLandscape,
            layers=layers,
            legend_layers=legend_layers,
            extent=self.previewCanvas.extent(),
            crs=self.previewCanvas.mapSettings().destinationCrs(),
            description=_layout_description(manifest),
            credits=_layout_credits(manifest, layers, legend_layers),
            summary=_safe_area_summary(package_path, manifest),
        )
        self._show_status(status or tr(
            "The print layout was not made.", "印刷レイアウトの作成を取り消しました。"
        ))

    def _next_image_format(self, *args):
        """Switch the image format in a loop: PNG, JPG, TIF."""

        del args
        names = [item[0] for item in IMAGE_FORMATS]
        self._imageFormat = names[(names.index(self._imageFormat) + 1) % len(names)]
        QgsSettings().setValue(IMAGE_FORMAT_KEY, self._imageFormat)
        self._update_image_button()
        # Pressing hid the tooltip: show it again with the new format.
        self.imageFormatButton.showStickyToolTip()

    def _update_image_button(self):
        self.exportImageButton.setText(
            tr("Image: {}", "画像出力：{}").format(self._imageFormat)
        )
        character = {
            "PNG": tr("PNG: no loss", "PNG：劣化なし"),
            "JPG": tr("JPG: small, with some loss", "JPG：小さいが劣化あり"),
            "TIF": tr("TIF: large, no loss", "TIF：大きいが劣化なし"),
        }[self._imageFormat]
        self.imageFormatButton.setToolTip(
            tr(
                "Switch the image format: PNG → JPG → TIF",
                "画像の形式を切り替えます：PNG → JPG → TIF",
            )
            + "\n" + character
        )

    def _export_image(self, *args):
        """Save exactly what the preview shows as an image on the paper size."""

        del args
        if self._imageTask is not None:
            return
        self._show_status(tr("Exporting an image...", "画像を書き出します…"))
        cancelled = tr("The image export was cancelled.", "画像の書き出しを取り消しました。")
        if not self._confirm_wms_export():
            self._show_status(cancelled)
            return
        width_mm, height_mm = self._paper_size_mm()
        dpi = self.exportDpiCombo.dpi()
        pixels = round(width_mm / 25.4 * dpi) * round(height_mm / 25.4 * dpi)
        dpi = self._checked_export_dpi(
            self.exportDpiCombo,
            tr("Large image", "大きな画像"),
            tr(
                "{} at {} dpi makes an image of {:,} × {:,} pixels.",
                "{}・{} dpiでは{:,} × {:,}画素の画像になります。",
            ),
            # The plugin's own limit for one image (4 bytes a pixel); not a
            # limit of Qt or QGIS.
            allow_as_is=pixels * 4 < MAX_IMAGE_BYTES,
        )
        if dpi is None:
            self._show_status(cancelled)
            return
        width_px = int(round(width_mm / 25.4 * dpi))
        height_px = int(round(height_mm / 25.4 * dpi))
        settings = QgsSettings()
        name = self.managementPanel.analysisNameLabel.text() or "preview"
        start_dir = settings.value(
            "qlae/image_dir", QgsProject.instance().absolutePath(), type=str
        )
        # The format chosen on the image button: the save dialog offers only it.
        _name, file_filter, extension, file_format = next(
            item for item in IMAGE_FORMATS if item[0] == self._imageFormat
        )
        path, _chosen = QFileDialog.getSaveFileName(
            self,
            tr("Save image", "画像を保存"),
            os.path.join(
                start_dir or "",
                "{}_{}_{}dpi{}".format(name, self._paper_name(), dpi, extension),
            ),
            file_filter,
        )
        if not path:
            self._show_status(cancelled)
            return
        typed = os.path.splitext(path)[1].lower()
        if typed not in _IMAGE_EXTENSIONS[file_format]:
            path += extension
        settings.setValue("qlae/image_dir", os.path.dirname(path))
        self._start_image_task(path, file_format, dpi, width_px, height_px)

    def _start_image_task(self, path, file_format, dpi, width_px, height_px):
        from qgis.core import QgsApplication, QgsMapRendererTask, QgsMapSettings

        source_settings = self.previewCanvas.mapSettings()
        # Copies, so the task keeps drawing even if the preview changes; made
        # with the temporary changes in them.
        self._begin_export()
        try:
            self._imageTaskLayers = [layer.clone() for layer in self._previewSourceLayers]
        finally:
            self._end_export()
        settings = QgsMapSettings()
        settings.setLayers(self._imageTaskLayers)
        settings.setDestinationCrs(source_settings.destinationCrs())
        settings.setTransformContext(source_settings.transformContext())
        settings.setFlags(source_settings.flags())
        settings.setBackgroundColor(QColor("white"))
        settings.setOutputSize(QSize(width_px, height_px))
        settings.setOutputDpi(dpi)
        settings.setExtent(self.previewCanvas.extent())
        crs_wkt = source_settings.destinationCrs().toWkt()
        task = QgsMapRendererTask(settings, path, file_format)
        task.setSaveWorldFile(True)
        task.taskCompleted.connect(
            lambda: self._image_task_finished(path, crs_wkt, True)
        )
        task.taskTerminated.connect(
            lambda: self._image_task_finished(path, crs_wkt, False)
        )
        self._imageTask = task
        self.exportImageButton.setEnabled(False)
        self.imageFormatButton.setEnabled(False)
        self.resultEdit.setPlainText(
            tr("Making the image ({}, {} dpi, {:,} × {:,} pixels)...",
               "画像を作成しています（{}・{} dpi・{:,} × {:,}画素）…").format(
                self._paper_name(), dpi, width_px, height_px
            )
        )
        QgsApplication.taskManager().addTask(task)

    def _image_task_finished(self, path, crs_wkt, ok):
        self._imageTask = None
        layers, self._imageTaskLayers = self._imageTaskLayers, []
        for layer in layers:
            layer.deleteLater()
        if sip.isdeleted(self):
            return
        self.exportImageButton.setEnabled(True)
        self.imageFormatButton.setEnabled(True)
        if ok:
            _write_image_crs(path, crs_wkt)
            self.resultEdit.setPlainText(
                tr("Image saved: {}", "画像を保存しました：{}").format(path)
            )
        else:
            self.resultEdit.setPlainText(
                tr("The image could not be made: {}", "画像を作成できませんでした：{}").format(path)
            )

    def _toggle_preview_orientation(self):
        self._set_preview_orientation(not self._previewLandscape)
        if not self._in_management():
            # The input (profile) screen's orientation is kept in the project.
            # Analysis management takes its orientation from the analysis.
            project = QgsProject.instance()
            value = "landscape" if self._previewLandscape else "portrait"
            stored, ok = project.readEntry(_PROJECT_SCOPE, _PREVIEW_ORIENTATION_KEY, "")
            if not ok or stored != value:
                project.writeEntry(_PROJECT_SCOPE, _PREVIEW_ORIENTATION_KEY, value)

    def _stored_preview_landscape(self):
        """True/False from the project, or None when nothing was stored."""

        value, ok = QgsProject.instance().readEntry(
            _PROJECT_SCOPE, _PREVIEW_ORIENTATION_KEY, ""
        )
        return (value == "landscape") if ok and value in ("landscape", "portrait") else None

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
        if self._in_management():
            self._fit_preview_to_management_extent()
            if final:
                self._resizePreviewExtent = None
            return
        self._sync_paper_magnification()
        self._set_preview_extent(extent)
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
            or self._in_management()
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
                "Terrain analysis from a local DEM or a downloaded DEM "
                "(Japan, and some sources in other countries).",
                "ローカルDEMまたはダウンロードしたDEM（日本と、海外の一部の取得元）による地形解析です。",
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
                "frame is a guide for fitting the subject and its surroundings "
                "in the analysis.",
                "プレビューに見えている全範囲を使用します。中央3/5の灰色枠は、"
                "解析する対象と周辺の収まりを見るための目安です。",
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

        # One combo grouped by provider. Regional sources stay listed but are
        # disabled while the preview is outside their region, so the choice
        # does not jump when the map moves.
        self.demSourceCombo = GroupedSourceComboBox(box)
        self.demSourceCombo.setSizeAdjustPolicy(
            QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon
        )
        self.demSourceCombo.setMinimumContentsLength(14)
        model = QStandardItemModel(self.demSourceCombo)
        header_font = self.demSourceCombo.font()
        header_font.setBold(True)
        for group_label, region, items in dem_sources.GROUPS:
            header = QStandardItem(tr(*group_label))
            header.setFlags(Qt.ItemFlag.NoItemFlags)
            header.setFont(header_font)
            model.appendRow(header)
            for key, label in items:
                item = QStandardItem("    " + tr(*label))
                item.setData(key, Qt.ItemDataRole.UserRole)
                item.setData(tr(*label), _DEM_LABEL_ROLE)
                model.appendRow(item)
        self.demSourceCombo.setModel(model)
        self._set_dem_source(dem_sources.DEFAULT_KEY)
        self.demSourceCombo.popupAboutToShow.connect(self._refresh_dem_source_availability)
        form.addRow(tr("Source", "取得元"), self.demSourceCombo)

        # A local DEM is chosen as a file (not from the layer panel): the
        # file is checked when chosen and is not added to the project.
        self._localDemPath = ""
        self.localDemRow = QWidget(box)
        local_layout = QHBoxLayout(self.localDemRow)
        local_layout.setContentsMargins(0, 0, 0, 0)
        local_layout.setSpacing(4)
        self.localDemEdit = QLineEdit(self.localDemRow)
        self.localDemEdit.setReadOnly(True)
        self.localDemEdit.setPlaceholderText(tr("Choose a file", "ファイルを選択"))
        self.localDemBrowseButton = QToolButton(self.localDemRow)
        self.localDemBrowseButton.setText("…")
        self.localDemBrowseButton.setToolTip(
            tr("Choose a local DEM file.", "ローカルDEMファイルを選びます。")
        )
        local_layout.addWidget(self.localDemEdit, 1)
        local_layout.addWidget(self.localDemBrowseButton)
        self.localDemLabel = QLabel(tr("File", "ファイル"), box)
        form.addRow(self.localDemLabel, self.localDemRow)
        # Keep the row's space while hidden so the section does not change
        # height when switching between local and fetched sources.
        for widget in (self.localDemLabel, self.localDemRow):
            policy = widget.sizePolicy()
            policy.setRetainSizeWhenHidden(True)
            widget.setSizePolicy(policy)

        self.demSourceNote = QLabel(box)
        self.demSourceNote.setWordWrap(True)
        self.demSourceNote.setAlignment(
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop
        )
        note_font = self.demSourceNote.font()
        note_font.setPointSizeF(9.0)
        self.demSourceNote.setFont(note_font)
        # One character of left margin for every line, wrapped lines included.
        self.demSourceNote.setIndent(
            self.demSourceNote.fontMetrics().horizontalAdvance(tr("M", "あ"))
        )
        # Always two lines high, with or without a note, so the section keeps
        # its height whichever source is chosen.
        self.demSourceNote.setFixedHeight(
            2 * self.demSourceNote.fontMetrics().lineSpacing() + 2
        )
        form.addRow(self.demSourceNote)

        self._demAvailabilityTimer = QTimer(self)
        self._demAvailabilityTimer.setSingleShot(True)
        self._demAvailabilityTimer.setInterval(300)
        self._demAvailabilityTimer.timeout.connect(self._refresh_dem_source_availability)
        # Header colours from the start (refreshed when the preview moves).
        self._refresh_dem_source_availability()
        return box

    def _dem_source_key(self):
        return self.demSourceCombo.currentData(Qt.ItemDataRole.UserRole)

    def _set_dem_source(self, key):
        index = self.demSourceCombo.findData(key, Qt.ItemDataRole.UserRole)
        if index >= 0:
            self.demSourceCombo.setCurrentIndex(index)

    def _preview_bbox84(self):
        extent = self.previewCanvas.extent()
        crs = self.previewCanvas.mapSettings().destinationCrs()
        if extent.isEmpty() or not crs.isValid():
            return None
        try:
            box = QgsCoordinateTransform(
                crs,
                QgsCoordinateReferenceSystem("EPSG:4326"),
                QgsProject.instance().transformContext(),
            ).transformBoundingBox(extent)
        except QgsCsException:
            return None
        return (box.xMinimum(), box.yMinimum(), box.xMaximum(), box.yMaximum())

    def _refresh_dem_source_availability(self, *args):
        """Show only the regional sources the preview overlaps.

        Sources that cannot be used here are left out of the list (and stay
        disabled, so the wheel or keys cannot pick them); a group header is
        left out when none of its sources can be used.
        """

        del args
        bbox84 = self._preview_bbox84()
        model = self.demSourceCombo.model()
        lerc = remote_dem.gdal_has_lerc()
        # Group headers can never be chosen, so they are drawn as disabled.
        # Give them the normal text colour unless none of their sources can
        # be used here; then they stay grey with their sources.
        headers = []
        hidden_rows = []
        for row in range(model.rowCount()):
            item = model.item(row)
            key = item.data(Qt.ItemDataRole.UserRole)
            if not key:
                headers.append([item, False])
                continue
            available = dem_sources.overlaps(dem_sources.region_for(key), bbox84)
            tooltip = ""
            if not available:
                tooltip = tr(
                    "The preview is outside this source's area.",
                    "プレビューが提供範囲の外です。",
                )
            elif key == "linz_dem" and not lerc:
                available = False
                tooltip = tr(
                    "This QGIS cannot read LINZ data (LERC).",
                    "このQGISはLINZのデータ（LERC）を読めません。",
                )
            flags = Qt.ItemFlag.ItemIsSelectable
            if available:
                flags |= Qt.ItemFlag.ItemIsEnabled
                if headers:
                    headers[-1][1] = True
            item.setFlags(flags)
            item.setToolTip(tooltip)
            hidden_rows.append((row, not available))
        text_brush = QBrush(self.palette().color(QPalette.ColorRole.Text))
        for header, any_available in headers:
            header.setData(
                text_brush if any_available else None, Qt.ItemDataRole.ForegroundRole
            )
            hidden_rows.append((header.row(), not any_available))
        view = self.demSourceCombo.view()
        if hasattr(view, "setRowHidden"):
            current = self.demSourceCombo.currentIndex()
            for row, hidden in hidden_rows:
                # The chosen source stays listed so the combo can show it
                # (its note says why it cannot be used here).
                view.setRowHidden(row, hidden and row != current)
        self._dem_source_changed()

    def _build_run_box(self):
        box = QGroupBox(tr("4. Run", "4. 実行"))
        layout = QVBoxLayout(box)
        self.runButton = QPushButton(tr("Run", "実行"), box)
        self.runButton.setObjectName("qlaeRunButton")
        self.runButton.setDefault(True)
        self._style_run_button(running=False)
        layout.addWidget(self.runButton)
        return box

    def _style_run_button(self, running):
        """Blue Run, red Stop, white bold text; grey while it cannot be pressed."""

        base, hover, pressed = (
            ("#c0392b", "#a5281c", "#8e2217") if running else ("#2b6cb0", "#245a93", "#1e4c7d")
        )
        self.runButton.setStyleSheet(
            "QPushButton#qlaeRunButton {"
            "background-color: %s; color: white; font-weight: bold;"
            "border: 1px solid %s; border-radius: 3px; padding: 4px 12px;}"
            "QPushButton#qlaeRunButton:hover {background-color: %s;}"
            "QPushButton#qlaeRunButton:pressed {background-color: %s;}"
            "QPushButton#qlaeRunButton:disabled {background-color: #9aa5b1;"
            "border-color: #9aa5b1; color: white;}" % (base, pressed, hover, pressed)
        )

    def _build_analysis_box(self):
        box = QGroupBox(tr("3. Analysis", "3. 解析"))
        outer = QHBoxLayout(box)
        layout = QVBoxLayout()
        outer.addLayout(layout)
        self.hillshadeCheck = QCheckBox(tr("Hillshade", "陰影起伏"), box)
        self.slopeCheck = QCheckBox(tr("Slope", "傾斜"), box)
        self.csMapCheck = QCheckBox(tr("CS Map", "CS立体図"), box)
        self.contoursCheck = QCheckBox(tr("Contours", "等高線"), box)
        self.contoursCheck.setToolTip(
            tr(
                "Smoothed contours every 10 m (thicker every 20 m and 100 m), "
                "styled like the GSI basic-map contours.",
                "滑らかにした等高線です（10 m間隔、20 m・100 mごとに太く）。"
                "見た目は基盤地図情報の等高線に合わせています。",
            )
        )
        for control in (
            self.hillshadeCheck, self.slopeCheck, self.csMapCheck, self.contoursCheck
        ):
            control.setChecked(True)
            layout.addWidget(control)
        self.slopeCheck.setToolTip(
            tr(
                "Slope in degrees, coloured in classes of 5° from 20° to 45°.",
                "傾斜（度）です。20°から45°まで5°ごとに色分けします。",
            )
        )
        # Only with "Use preview map extent"; off by default. GRASS r.watershed
        # where QGIS has it, numpy otherwise (terrain/watershed.py).
        self.watershedCheck = QCheckBox(tr("Catchment map", "流域範囲図"), box)
        self.watershedCheck.setChecked(False)
        layout.addWidget(self.watershedCheck)
        # Empty rows under the checks keep room for the DEM and the base map
        # on the right.
        for _row in range(2):
            spacer_row = QWidget(box)
            spacer_row.setFixedHeight(self.watershedCheck.sizeHint().height())
            layout.addWidget(spacer_row)
        layout.addStretch(1)
        # Checks are in run order; the layers come out in drawing order.
        self.layerOrderArrows = LayerOrderArrows(
            [
                ("hillshade", self.hillshadeCheck),
                ("slope", self.slopeCheck),
                ("cs_map", self.csMapCheck),
                ("contours", self.contoursCheck),
                ("watershed", self.watershedCheck),
            ],
            [
                ("watershed", tr("Catchments", "流域範囲")),
                ("contours", tr("Contours", "等高線")),
                ("slope", tr("Slope", "傾斜")),
                ("hillshade", tr("Hillshade", "陰影起伏")),
                ("cs_map", tr("CS Map", "CS立体図")),
                ("dem", tr("DEM", "DEM")),
                # Not made by the analysis: drawn under its layers.
                ("base_map", tr("Base map", "ベースマップ")),
            ],
            box,
        )
        self.layerOrderArrows.setToolTip(tr(
            "Layers come out in this order (top is drawn on top), "
            "whatever the order of the checks.",
            "できるレイヤーはこの順に並びます（上ほど上に描かれます）。"
            "チェックの並びとは順番が変わります。",
        ))
        outer.addWidget(self.layerOrderArrows, 1)
        return box

    def _update_watershed_option(self, *args):
        """The catchment map is offered only with the preview extent."""

        del args
        extent_mode = self.mapExtentAreaRadio.isChecked()
        self.watershedCheck.setEnabled(extent_mode)
        if not extent_mode:
            tip = tr(
                "Available with “Use preview map extent”.",
                "「画面の範囲を使用」のときに使えます。",
            )
        elif watershed.catchment_engine() == "grass":
            tip = tr(
                "Catchments by valley (GRASS r.watershed, MFD). "
                "The DEM is fetched wider than the preview.",
                "谷ごとの流域を求めます（GRASS r.watershed・MFD）。"
                "プレビューより広くDEMを取得します。",
            )
        else:
            tip = tr(
                "Catchments by valley (computed in the plugin, MFD; close to "
                "GRASS r.watershed). The DEM is fetched wider than the preview.",
                "谷ごとの流域を求めます（プラグイン内の計算・MFD。GRASS r.watershedに近い結果）。"
                "プレビューより広くDEMを取得します。",
            )
        self.watershedCheck.setToolTip(tip)

    def _watershed_toggled(self, checked):
        if checked:
            self.resultEdit.setPlainText(_watershed_status_text())

    def _build_analysis_name_box(self):
        box = QGroupBox(tr("Analysis name", "解析名"))
        layout = QVBoxLayout(box)
        self.analysisNameEdit = QLineEdit(box)
        self.analysisNameEdit.setPlaceholderText(
            tr("Analysis name", "解析名")
        )
        layout.addWidget(self.analysisNameEdit)
        self.analysisNameEdit.textChanged.connect(self._analysis_name_changed)
        # Normally the timestamp note; a red warning while the name is taken.
        self.analysisNameNote = QLabel(_timestamp_note_text(), box)
        self.analysisNameNote.setWordWrap(True)
        self.analysisNameNote.setAlignment(Qt.AlignmentFlag.AlignRight)
        self.analysisNameNote.setStyleSheet("font-size: 9pt;")
        layout.addWidget(self.analysisNameNote)
        return box

    def _analysis_name_taken(self):
        name = self.analysisNameEdit.text().strip()
        directory = QgsProject.instance().absolutePath()
        if not name or not directory:
            return False
        return os.path.exists(packages.package_path(directory, name))

    def _analysis_name_changed(self, *args):
        del args
        self._update_run_button()

    def _update_run_button(self):
        """Run is unavailable while the name belongs to a saved analysis."""

        if not hasattr(self, "runButton") or not hasattr(self, "analysisNameNote"):
            return  # still building the window
        if getattr(self, "_terrainWorker", None) is not None:
            return  # the button is the Stop button now
        taken = self._analysis_name_taken()
        self.runButton.setEnabled(not taken)
        tip = _name_taken_text() if taken else ""
        self.analysisNameEdit.setToolTip(tip)
        note = self.analysisNameNote
        note.setText(_name_taken_short_text() if taken else _timestamp_note_text())
        note.setStyleSheet(
            "font-size: 9pt; color: #c0392b; font-weight: bold;" if taken else "font-size: 9pt;"
        )

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
                "Opens the folder that holds the analysis ZIP files.\n"
                "The ZIP chosen in the list is shown selected.",
                "解析のZIPファイルを置いているフォルダを開きます。\n"
                "一覧で選んでいるZIPは、選択された状態で表示されます。",
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
        layout.addWidget(self.resultEdit)
        return box

    def _build_buttons(self):
        row = QHBoxLayout()
        self.languageButton = QPushButton(tr("Language: English", "言語：日本語"))
        self.languageButton.setToolTip(
            tr("Switch to Japanese.", "英語へ切り替えます。")
        )
        self.closeButton = QPushButton(tr("Close", "閉じる"))
        self.avidMapsLink = QLabel(
            '<a href="{}">Avid Maps Extra</a>'.format(AVID_MAPS_URL)
        )
        self.avidMapsLink.setTextFormat(Qt.TextFormat.RichText)
        self.avidMapsLink.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextBrowserInteraction
        )
        # Not opened at once: a notice first says what the page is.
        self.avidMapsLink.setOpenExternalLinks(False)
        self.avidMapsLink.linkActivated.connect(self._open_avid_maps_page)
        self.avidMapsLink.setToolTip(tr(
            "Avid Maps Extra: a field map app (PWA) that opens the GeoPDF made here.\n"
            + AVID_MAPS_URL,
            "Avid Maps Extra：ここで作ったGeoPDFを開ける現場マップ（PWA）\n"
            + AVID_MAPS_URL,
        ))

        row.addWidget(self.languageButton)
        row.addStretch(1)
        row.addWidget(self.avidMapsLink)
        row.addSpacing(12)
        row.addWidget(self.closeButton)
        return row

    def _open_avid_maps_page(self, *args):
        """Say what Avid Maps Extra is, then open its overview page if wanted."""

        del args
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Icon.Information)
        box.setWindowTitle("Avid Maps Extra")
        box.setText(tr(
            "Avid Maps Extra is a field map app (PWA) that opens the GeoPDFs made "
            "here.\nIt is made by the author of this plugin (Avid Tree Work).",
            "Avid Maps Extra は、ここで作った GeoPDF を現場で開ける地図アプリ（PWA）です。\n"
            "このプラグインの作者（Avid Tree Work）が作っています。",
        ))
        box.setInformativeText(tr(
            "The overview page is in English; the app itself is in Japanese.\n"
            "Open the overview page in your browser?",
            "説明のページは英語、アプリは日本語です。\n"
            "説明のページをブラウザで開きますか？",
        ))
        open_button = box.addButton(tr("Open", "開く"), QMessageBox.ButtonRole.AcceptRole)
        box.addButton(QMessageBox.StandardButton.Cancel)
        box.setDefaultButton(open_button)
        _keep_message_lines(box)
        box.exec()
        if box.clickedButton() is open_button:
            QDesktopServices.openUrl(QUrl(AVID_MAPS_URL))

    def _refresh_preview_layer_choices(self, *args):
        del args
        if not self._alive():
            return
        if self._in_management():
            # After the current add/remove batch (e.g. a package reload).
            QTimer.singleShot(0, self._refresh_management_layers)
        current_aoi = self.aoiLayerCombo.currentLayer()
        current_optionals = [
            combo.currentLayer() for _key, combo in self._optional_layer_combos()
        ]
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
        overlay_layers = [
            layer for layer in tree_layers if not _is_base_preview_layer(layer)
        ]
        # Optional layers: any project layer except this plugin's own results
        # (those are chosen in the analysis list).
        optional_layers = [
            layer for layer in tree_layers
            if not layer.customProperty("qlae/package_path", "")
        ]
        self.aoiLayerCombo.setLayers(polygon_layers)
        self.overlayLayerCombo.setLayers(overlay_layers)
        self.baseMapCombo.setLayers(base_layers)
        for (key, combo), current in zip(self._optional_layer_combos(), current_optionals):
            combo.setLayers(optional_layers)
            if current is not None and current in optional_layers:
                combo.setLayer(current)
            else:
                stored, layer = _stored_preview_layer(key, optional_layers)
                if stored:
                    combo.setLayer(layer)

        stored_aoi_ok, stored_aoi = _stored_preview_layer(_AREA_POLYGON_KEY, polygon_layers)
        if current_aoi is not None and current_aoi in polygon_layers:
            self.aoiLayerCombo.setLayer(current_aoi)
        elif stored_aoi_ok and stored_aoi is not None:
            # The polygon layer chosen earlier in this project.
            self.aoiLayerCombo.setLayer(stored_aoi)
        elif not self._aoiChoicesInitialized and polygon_layers:
            active_layer = self.iface.activeLayer()
            if active_layer in polygon_layers:
                self.aoiLayerCombo.setLayer(active_layer)
            else:
                self.aoiLayerCombo.setLayer(polygon_layers[0])
        self._aoiChoicesInitialized = True
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
        self._restore_visibility_choices()
        self._update_preview_layers()

    def _stored_layer_combos(self):
        """Layer combos whose user choice is kept in the project."""

        return (
            (_AREA_POLYGON_KEY, self.aoiLayerCombo),
            (_PREVIEW_OVERLAY_KEY, self.overlayLayerCombo),
            (_PREVIEW_BASE_MAP_KEY, self.baseMapCombo),
        ) + self._optional_layer_combos()

    def _optional_layer_combos(self):
        return (
            (_OPTIONAL_LAYER_1_KEY, self.managementPanel.optionalLayerCombo1),
            (_OPTIONAL_LAYER_2_KEY, self.managementPanel.optionalLayerCombo2),
        )

    def _visibility_choices(self):
        """(on/off button, its layer combo, project key of its on/off)."""

        optional_1, optional_2 = self.managementPanel.optionalVisibilityButtons()
        return (
            (self.overlayVisibilityButton, self.overlayLayerCombo, _OVERLAY_ON_KEY),
            (self.baseVisibilityButton, self.baseMapCombo, _BASE_MAP_ON_KEY),
            (optional_1, self.managementPanel.optionalLayerCombo1, _OPTIONAL_LAYER_1_ON_KEY),
            (optional_2, self.managementPanel.optionalLayerCombo2, _OPTIONAL_LAYER_2_ON_KEY),
        )

    def _stored_on(self, key, combo):
        """On/off for the combo's layer: the stored click, else on."""

        if combo.currentLayer() is None:
            return None
        value, ok = QgsProject.instance().readBoolEntry(_PROJECT_SCOPE, key, True)
        return bool(value) if ok else True

    def _store_on(self, key, on):
        project = QgsProject.instance()
        value, ok = project.readBoolEntry(_PROJECT_SCOPE, key, True)
        if not ok or bool(value) != bool(on):
            project.writeEntry(_PROJECT_SCOPE, key, bool(on))

    def _layer_chosen(self, button, combo, key):
        """The user chose a layer: it is on (an earlier click is dropped)."""

        project = QgsProject.instance()
        _value, ok = project.readBoolEntry(_PROJECT_SCOPE, key, True)
        if ok:
            project.removeEntry(_PROJECT_SCOPE, key)
        button.setWanted(True if combo.currentLayer() is not None else None)

    def _restore_visibility_choices(self):
        for button, combo, key in self._visibility_choices():
            button.setWanted(self._stored_on(key, combo))

    def _store_layer_choice(self, key, combo):
        project = QgsProject.instance()
        layer_id = _layer_id(combo.currentLayer()) or ""
        value, ok = project.readEntry(_PROJECT_SCOPE, key, "")
        if not ok or value != layer_id:
            project.writeEntry(_PROJECT_SCOPE, key, layer_id)

    def _project_was_read(self, *args):
        """Restore the stored choices once the project has finished loading.

        While a project is read, the layer lists are rebuilt before the layer
        tree is complete, so the stored layers may not be in them yet.
        """

        del args

        def restore():
            # Another project: the analysis shown may not be in it.
            if self._in_management() and self._managementPackagePath not in (
                session.session_package_paths()
            ):
                self._exit_management()
            self._refresh_preview_layer_choices()
            self._apply_stored_preview_layer_choices()
            stored_landscape = self._stored_preview_landscape()
            if stored_landscape is not None and not self._in_management():
                self._set_preview_orientation(stored_landscape)
            self._refresh_saved_analyses()
            if self.isVisible():
                self._offer_previous_analysis()

        # After the plugin has removed layers whose ZIP is gone.
        QTimer.singleShot(50, restore)

    def _apply_stored_preview_layer_choices(self, *args):
        """Apply the choices saved in a project that has just been read."""

        del args
        project = QgsProject.instance()
        for key, combo in self._stored_layer_combos():
            layer_id = _stored_preview_layer_id(key)
            if layer_id is None:
                continue
            layer = project.mapLayer(layer_id) if layer_id else None
            if layer_id and layer is None:
                continue
            if layer is None and combo is self.aoiLayerCombo:
                # The area polygon layer has no "empty" choice.
                continue
            combo.setLayer(layer)
        self._restore_visibility_choices()
        self._update_preview_layers()

    # ----- Analysis management mode ---------------------------------------

    def _in_management(self):
        # getattr: events can reach eventFilter while the window is still
        # being built, before this attribute exists.
        return getattr(self, "_managementPackagePath", None) is not None

    def eventFilter(self, watched, event):
        # Events can come in while the window is still being built.
        if getattr(self, "previewCanvas", None) is None:
            return super().eventFilter(watched, event)
        if (
            event.type() == QEvent.Type.KeyPress
            and event.key() == Qt.Key.Key_Escape
            and watched in (self.previewCanvas, self.previewCanvas.viewport())
        ):
            # QgsMapCanvas marks Esc as handled though it does nothing with it
            # (no Esc action in the canvas or these map tools), so the dialog
            # never saw it. Close as Esc does everywhere else in the window.
            self.reject()
            return True
        # Events also come in while the window is still being built, before
        # the preview canvas exists.
        if (
            event.type() == QEvent.Type.Show
            and watched is getattr(self, "previewCanvas", None)
        ):
            self._prepare_preview_show()
        if (
            self._in_management()
            and event.type() in _PREVIEW_NAVIGATION_EVENTS
            and not (
                event.type() in (QEvent.Type.KeyPress, QEvent.Type.KeyRelease)
                and event.key() == Qt.Key.Key_Escape
            )
        ):
            # The preview is fixed to the analysis; the main map is for looking
            # around. Swallow wheel zoom, drag pan and keyboard navigation.
            return True
        return super().eventFilter(watched, event)

    def _prepare_preview_show(self):
        """Let the preview keep the paper's DPI when it is shown again.

        When the canvas is shown, QGIS sets the screen DPI but keeps the
        magnification (QgsMapCanvas::showEvent in 3.44), so the two no longer
        match and QGIS's lowest magnification (0.1) then stops the DPI from
        going down to the paper's: measured A0 at 96 dpi instead of 15 dpi,
        labels drawn at screen size. This runs before the canvas handles the
        event: back to magnification 1 here, and the paper's magnification
        and the extent are set again after (as after a resize).
        """

        canvas = self.previewCanvas
        if abs(canvas.magnificationFactor() - 1.0) < 1e-9:
            return
        self._queue_preview_extent_restore(QgsRectangle(canvas.extent()))
        canvas.setMagnificationFactor(1.0)

    def _enter_management(self, package_path, checked_ids=None):
        """Switch the right panel and fix the preview to one analysis."""

        package_path = os.path.abspath(package_path)
        layers = _package_layers(package_path)
        if not layers:
            return False
        try:
            manifest = packages.read_manifest(package_path)
        except packages.PackageError:
            return False
        # Show the preview view used when the analysis was run, so the
        # surroundings stay visible. Older packages fall back to the extent
        # of the analysis layers with a small margin.
        extent, landscape = self._saved_preview_view(manifest)
        self._managementMargin = extent is None
        if extent is None:
            extent = self._analysis_preview_extent(layers)
            if extent is None:
                return False
            landscape = extent.width() > extent.height()
        if not self._in_management():
            self._explorePreviewView = self._preview_view_state()
            self._explorePreviewView["preview_landscape"] = self._previewLandscape
        self._managementPackagePath = package_path
        self._managementExtent = extent
        self.resultEdit.clear()
        self.managementPanel.setAnalysis(
            package_path,
            manifest["analysis_name"],
            _analysis_summary(manifest, package_path, include_name=False),
        )
        self.managementPanel.setLayerGroup(
            _package_layer_group(package_path), checked_ids
        )
        self.controlStack.setCurrentWidget(self.managementPanel)
        self._activate_map_navigation()
        self._update_management_controls()
        # Orientation follows the analysis and cannot be changed here.
        self._stablePreviewExtent = QgsRectangle(extent)
        self._set_preview_orientation(landscape)
        self._fit_preview_to_management_extent()
        # The window layout settles after the orientation change; fit again.
        QTimer.singleShot(0, self._fit_preview_to_management_extent)
        return True

    def _finish_management_by_user(self):
        self._exit_management()
        # Nothing is managed any more: take the analysis layers out of the
        # project (the ZIP stays and can be loaded again). Layers written out
        # with the Layers export are ordinary layers and stay.
        self._remove_session_layers()
        self.resultEdit.setPlainText(
            tr(
                "Analysis management finished. The analysis layers were removed "
                "from the project; the analysis can be loaded again.",
                "解析管理を終了しました。解析のレイヤーはプロジェクトから片付けました"
                "（解析はいつでも読み込めます）。",
            )
        )

    def _exit_management(self, *args):
        del args
        if not self._in_management():
            return
        self._managementPackagePath = None
        self._managementExtent = None
        self.resultEdit.clear()
        self.managementPanel.clear()
        # The optional layers are used in analysis management only: their
        # on/off choices are taken out of the layer panel (and kept) when
        # the preview layers are updated below.
        self.controlStack.setCurrentIndex(0)
        self._update_management_controls()
        view = self._explorePreviewView
        self._explorePreviewView = None
        if view:
            self._set_preview_orientation(view.get("preview_landscape", False))
            self._restore_preview_view(view)
            # Re-apply after the orientation change has resized the window.
            QTimer.singleShot(0, lambda state=view: self._restore_preview_view(state))
        self._update_preview_layers()

    def _update_management_controls(self):
        managing = self._in_management()
        # Navigation tools, main-map sync and orientation are not used while
        # the preview is fixed to an analysis.
        for widget in (
            self.previewToolPanel,
            self.mainViewButton,
            self.previewOrientationButton,
        ):
            widget.setVisible(not managing)
        self.toMainViewButton.setVisible(managing)
        self._update_local_dem_footprint()
        if managing:
            self.previewFrame.setAnalysisGuideVisible(False)
            self.previewRubber.reset(Qgis.GeometryType.Polygon)
        else:
            self.previewFrame.setAnalysisGuideVisible(
                self.mapExtentAreaRadio.isChecked()
            )
            self._update_preview_selection_controls()
        # Export stays visible as the goal of the workflow, but it is only
        # usable while an analysis is being managed.
        self.exportBox.setEnabled(managing)
        # The shared status box hints at what this screen is for.
        self.resultEdit.setPlaceholderText(
            tr(
                "Choose what to show in the preview, then export.",
                "プレビューに表示するものを選び、エクスポートします。",
            )
            if managing
            else tr(
                "Choose an area, DEM source and outputs, then run the analysis.",
                "範囲、DEM取得元、出力を選び、解析を実行します。",
            )
        )

    def _saved_preview_view(self, manifest):
        """(extent in the preview CRS, landscape) saved at run time, or (None, None)."""

        view = manifest.get("preview_view") or {}
        values = view.get("extent")
        source_crs = QgsCoordinateReferenceSystem.fromWkt(view.get("crs_wkt", ""))
        if not values or len(values) != 4 or not source_crs.isValid():
            return None, None
        extent = QgsRectangle(*values)
        destination_crs = self.previewCanvas.mapSettings().destinationCrs()
        if destination_crs.isValid() and destination_crs != source_crs:
            try:
                extent = QgsCoordinateTransform(
                    source_crs, destination_crs, QgsProject.instance().transformContext()
                ).transformBoundingBox(extent)
            except QgsCsException:
                return None, None
        if extent.isEmpty():
            return None, None
        return extent, view.get("orientation") == "landscape"

    def _analysis_preview_extent(self, layers):
        """Combined extent of the analysis layers in the preview CRS."""

        destination_crs = self.previewCanvas.mapSettings().destinationCrs()
        combined = QgsRectangle()
        combined.setNull()
        for layer in layers:
            extent = layer.extent()
            if extent.isEmpty():
                continue
            if (
                destination_crs.isValid()
                and layer.crs().isValid()
                and layer.crs() != destination_crs
            ):
                try:
                    extent = QgsCoordinateTransform(
                        layer.crs(),
                        destination_crs,
                        QgsProject.instance().transformContext(),
                    ).transformBoundingBox(extent)
                except QgsCsException:
                    continue
            combined.combineExtentWith(extent)
        if combined.isNull() or combined.isEmpty():
            return None
        return combined

    def _fit_preview_to_management_extent(self):
        if self._managementExtent is None:
            return
        extent = QgsRectangle(self._managementExtent)
        if self._managementMargin:
            # Layer extent only: a small margin keeps its edge visible.
            extent.scale(1.02)
        self._stablePreviewExtent = QgsRectangle(self._managementExtent)
        self._sync_paper_magnification()
        self._set_preview_extent(extent)
        self.previewCanvas.refresh()
        self._update_preview_status()
        # The tile layers' zoom follows the fitted scale.
        self._previewLayersTimer.start()

    def _refresh_management_layers(self):
        """Rebuild the list after layers were removed or re-added."""

        if not self._in_management():
            return
        path = self._managementPackagePath
        layers = _package_layers(path)
        if not layers:
            self._exit_management()
            return
        # Reloading a package replaces its layers with new IDs; then fall back
        # to the visibility in the QGIS layer panel.
        current_ids = {layer.id() for layer in layers}
        known = current_ids & set(self.managementPanel.layerIds())
        checked_ids = (
            [
                layer_id
                for layer_id in self.managementPanel.checkedLayerIds()
                if layer_id in current_ids
            ]
            if known
            else None
        )
        self.managementPanel.setLayerGroup(_package_layer_group(path), checked_ids)

    def _rename_analysis(self):
        """Rename the analysis being managed (its ZIP and its stored name)."""

        path = self._managementPackagePath
        if not path:
            return
        if self._imageTask is not None:
            self.resultEdit.setPlainText(tr(
                "Wait until the image is finished.", "画像の作成が終わるまでお待ちください。"
            ))
            return
        if self._geoPdfTask is not None:
            self.resultEdit.setPlainText(tr(
                "Wait until the GeoPDF is finished.", "GeoPDFの作成が終わるまでお待ちください。"
            ))
            return
        try:
            old_name = packages.read_manifest(path)["analysis_name"]
        except packages.PackageError:
            self.resultEdit.setPlainText(
                tr("The analysis results could not be loaded.", "解析結果を読み込めません。")
            )
            return
        text, accepted = QInputDialog.getText(
            self,
            tr("Rename analysis", "解析名を変更"),
            tr("New analysis name:", "新しい解析名："),
            QLineEdit.EchoMode.Normal,
            old_name,
        )
        if not accepted:
            return
        new_name = packages.normalized_name(text)
        if new_name == old_name:
            return
        try:
            new_path = packages.renamed_package_path(path, new_name)
        except packages.PackageError:
            self.resultEdit.setPlainText(
                tr("Enter an analysis name.", "解析名を入力してください。")
            )
            return
        if not packages.is_same_file_path(new_path, path) and os.path.exists(new_path):
            self.resultEdit.setPlainText(_name_taken_text())
            return

        # The ZIP is read by the analysis layers and their preview copies:
        # let go of them all before the file is replaced (Windows locks it).
        project = QgsProject.instance()
        checked_members = {
            _layer_member(project.mapLayer(layer_id), path)
            for layer_id in self.managementPanel.checkedLayerIds()
            if project.mapLayer(layer_id) is not None
        }
        self._exit_management()
        self.previewCanvas.stopRendering()
        self.mainCanvas.stopRendering()
        self._remove_package_layers(path)
        _prune_empty_output_groups(project.layerTreeRoot())
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)

        error = None
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            new_path = packages.rename_package(path, new_name)
        except (packages.PackageError, OSError, zipfile.BadZipFile) as caught:
            error = caught
            new_path = path
        finally:
            QApplication.restoreOverrideCursor()

        # Back to the analysis (renamed, or unchanged after a failure).
        self._refresh_saved_analyses(new_path)
        try:
            manifest = packages.read_manifest(new_path)
        except packages.PackageError:
            manifest = None
        entered = False
        if manifest is not None:
            self._add_package_layers(new_path, manifest)
            checked_ids = [
                layer.id()
                for layer in _package_layers(new_path)
                if _layer_member(layer, new_path) in checked_members
            ]
            entered = self._enter_management(new_path, checked_ids)
        if error is not None:
            self.resultEdit.setPlainText(tr(
                "The analysis could not be renamed: {}\n"
                "The file may be in use. Nothing was changed.",
                "解析名を変更できませんでした：{}\n"
                "ファイルが使用中の可能性があります。変更していません。",
            ).format(error))
        elif entered:
            self.resultEdit.setPlainText(tr(
                "Renamed the analysis: {} → {}", "解析名を変更しました：{} → {}"
            ).format(old_name, new_name))

    def _resume_pending_management(self):
        pending = self._pendingManagement
        self._pendingManagement = None
        if pending:
            # A language rebuild is not a screen switch; keep the status.
            text = self.resultEdit.toPlainText()
            self._enter_management(pending["path"], pending.get("checked_ids"))
            self.resultEdit.setPlainText(text)

    # ----------------------------------------------------------------------

    def _update_preview_layers(self, *args):
        del args
        if not self._alive():
            return
        overlay = _shown_in_layer_panel(self.overlayLayerCombo.currentLayer())
        base_map = _shown_in_layer_panel(self.baseMapCombo.currentLayer())
        # The polygon layer is also part of the picture: it stays drawn with
        # "Use preview map extent" (only its selection is cleared then).
        aoi_layer = None if self._in_management() else self.aoiLayerCombo.currentLayer()
        # Canvas order is top to bottom. Analysis management: optional 1,
        # optional 2, the overlay, checked analysis layers, the base map.
        # Otherwise: the AOI, the overlay, the base map. These are the project
        # layers themselves, drawn with their own styles; a layer is listed
        # only once.
        layers = []

        def add(layer):
            if layer is not None and layer not in layers:
                layers.append(layer)

        if self._in_management():
            project = QgsProject.instance()
            for _key, combo in self._optional_layer_combos():
                layer = _shown_in_layer_panel(combo.currentLayer())
                if layer is not overlay and layer is not base_map:
                    add(layer)
            add(overlay)
            for layer_id in self.managementPanel.checkedLayerIds():
                add(project.mapLayer(layer_id))
        else:
            if aoi_layer is not overlay:
                add(aoi_layer)
            add(overlay)
        if base_map is not aoi_layer:
            add(base_map)
        self._previewSourceLayers = list(layers)
        drawn = [self._preview_tile_layer(layer) for layer in layers]
        self.previewCanvas.setLayers(drawn)
        self._drop_unused_tile_copies(drawn)
        self._update_scale_suspension()
        self.previewCanvas.refresh()
        self._refresh_preview_polygon()

    def _preview_tile_layer(self, layer):
        """The layer to draw: in analysis management an XYZ tile layer is
        drawn from a copy fixed to the zoom level of the export."""

        if not self._in_management() or not preview_tiles.is_xyz_layer(layer):
            return layer
        extent = self.previewCanvas.extent()
        crs = self.previewCanvas.mapSettings().destinationCrs()
        paper_width = self._paper_size_mm()[0]
        # The preview shows the paper output, the GeoPDF.
        dpi = self.geoPdfDpiCombo.dpi()
        # The zoom is found by drawing test squares in the background (the
        # tiles may have to be fetched): find it again only when what
        # decides it changed, and meanwhile draw what there is.
        key = (
            layer.source(),
            tuple(round(value, 3) for value in (
                extent.xMinimum(), extent.yMinimum(), extent.xMaximum(), extent.yMaximum()
            )),
            crs.authid() or crs.toWkt(),
            paper_width,
            dpi,
        )
        cached = self._previewTileCopies.get(layer.id())
        known = self._tileZooms.get(layer.id())
        if known is None or known[0] != key:
            self._find_tile_zoom(layer, key, extent, crs, paper_width, dpi)
            return cached[1] if cached is not None else layer
        zoom = known[1]
        if zoom is None:
            return layer
        if cached is not None and cached[0] == zoom:
            return cached[1]
        copy = preview_tiles.fixed_zoom_copy(layer, zoom)
        if copy is None:
            return layer
        if cached is not None:
            cached[1].deleteLater()
        self._previewTileCopies[layer.id()] = (zoom, copy)
        self._watch_tile_source(layer)
        return copy

    def _find_tile_zoom(self, layer, key, extent, crs, paper_width, dpi):
        """Start finding the export's zoom level of a tile layer; the
        preview is drawn again with its copy once it is known."""

        layer_id = layer.id()
        running = self._tileZoomFinders.get(layer_id)
        if running is not None:
            if running[0] == key:
                return
            running[1].cancel()
            running[1].deleteLater()
        finder = preview_tiles.ExportZoomFinder(layer, extent, crs, paper_width, dpi, self)
        self._tileZoomFinders[layer_id] = (key, finder)

        def found(zoom):
            if sip.isdeleted(self) or self._tileZoomFinders.get(layer_id, (None, None))[1] is not finder:
                return
            del self._tileZoomFinders[layer_id]
            finder.deleteLater()
            self._tileZooms[layer_id] = (key, zoom)
            self._previewLayersTimer.start()

        finder.found.connect(found)
        finder.start()

    def _drop_unused_tile_copies(self, drawn):
        for source_id, (_zoom, copy) in list(self._previewTileCopies.items()):
            if copy not in drawn:
                del self._previewTileCopies[source_id]
                copy.deleteLater()
                self._unwatch_tile_source(source_id)

    def _watch_tile_source(self, layer):
        """A style change of the source is copied on the next update."""

        if layer.id() in self._tileCopyWatches:
            return

        def changed(*_args, layer_id=layer.id()):
            entry = self._previewTileCopies.pop(layer_id, None)
            if entry is not None:
                QTimer.singleShot(0, entry[1].deleteLater)
            self._previewLayersTimer.start()

        layer.styleChanged.connect(changed)
        self._tileCopyWatches[layer.id()] = (layer, changed)

    def _unwatch_tile_source(self, layer_id):
        layer, slot = self._tileCopyWatches.pop(layer_id, (None, None))
        if layer is not None and not sip.isdeleted(layer):
            try:
                layer.styleChanged.disconnect(slot)
            except (TypeError, RuntimeError):
                pass

    def _update_scale_suspension(self):
        """Make the plugin's temporary changes to the project layers only
        while this window is in front of the QGIS main window: the
        layer-panel on/off choices of the preview-layer buttons and the
        lifted scale limits. Behind it, the project is as the user left it.
        Going to another application changes nothing."""

        if not self._alive():
            return
        in_front = self._exportStarting or self._in_front_of_main_window()
        managing = self._in_management()
        for button, in_use in self._visibility_buttons():
            button.setApplied(in_front and (in_use or managing))
        if in_front:
            self._scaleSuspender.suspend(self._previewSourceLayers)
        else:
            self._scaleSuspender.restore()

    def _focus_window_changed(self, *args):
        del args
        if self._alive():
            QTimer.singleShot(0, self._update_scale_suspension)

    def _alive(self):
        """False once this window, or its preview, has been deleted (a
        project signal can still reach a window that is going away)."""

        if sip.isdeleted(self):
            return False
        canvas = getattr(self, "previewCanvas", None)
        return canvas is not None and not sip.isdeleted(canvas)

    def _in_front_of_main_window(self):
        """Whether this window is in front of the QGIS main window.

        Only these two windows decide it: this window (or one of its own
        dialogs) being active puts it in front, the QGIS main window (or one
        of its windows and dialogs) being active puts it behind. Anything
        else keeps the last answer: another application, a native file
        dialog, or no active window at all.
        """

        if not self.isVisible() or self.isMinimized():
            self._lastInFront = False
            return False
        active = QApplication.activeWindow()
        if active is self or (active is not None and self.isAncestorOf(active)):
            self._lastInFront = True
        else:
            main = self.iface.mainWindow() if self.iface is not None else None
            if main is not None and active is not None and (
                active is main or main.isAncestorOf(active)
            ):
                self._lastInFront = False
        return self._lastInFront

    def _visibility_buttons(self):
        """(button, used outside analysis management)"""

        return (
            (self.overlayVisibilityButton, True),
            (self.baseVisibilityButton, True),
        ) + tuple(
            (button, False)
            for button in self.managementPanel.optionalVisibilityButtons()
        )

    def _begin_export(self):
        """Before an export takes the preview's layers.

        The save dialog may have just had the focus (a native file dialog is
        not one of this window's own), so the temporary on/off choices and
        scale lifting were taken out and, for example, a base map turned on
        only by its button had left the preview. Put them in again and take
        the layers now; ``_end_export`` goes back to following the focus.
        """

        self._exportStarting = True
        self._update_scale_suspension()
        self._previewLayersTimer.stop()
        self._update_preview_layers()

    def _end_export(self):
        self._exportStarting = False
        QTimer.singleShot(0, self._update_scale_suspension)

    def _sync_main_from_preview(self, *args):
        """Show the preview's area (the analysis) in the QGIS main map."""

        del args
        extent = QgsRectangle(self.previewCanvas.extent())
        if extent.isEmpty():
            return
        source_crs = self.previewCanvas.mapSettings().destinationCrs()
        destination_crs = self.mainCanvas.mapSettings().destinationCrs()
        if source_crs.isValid() and destination_crs.isValid() and source_crs != destination_crs:
            try:
                extent = QgsCoordinateTransform(
                    source_crs, destination_crs, QgsProject.instance().transformContext()
                ).transformBoundingBox(extent)
            except QgsCsException:
                return
        self.mainCanvas.setExtent(extent)
        self.mainCanvas.refresh()

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
        self._sync_paper_magnification()
        self._set_preview_extent(preview_extent)
        # Canvas frame pixels can make the first estimate differ slightly.
        # Correct once using QGIS's own computed scale while keeping the centre.
        preview_scale = self.previewCanvas.scale()
        if scale > 0 and preview_scale > 0:
            correction = scale / preview_scale
            half_width *= correction
            half_height *= correction
            self._set_preview_extent(
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
        desired = getattr(self, "_desiredMagnification", None)
        actual = self.previewCanvas.magnificationFactor()
        if desired and actual > 0:
            # The scale is proportional to the magnification; correct for a
            # magnification QGIS could not go down to.
            scale *= desired / actual
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
                self._sync_paper_magnification()
                self._set_preview_extent(extent)
                self._stablePreviewExtent = QgsRectangle(extent)
                self.previewCanvas.refresh()
                self._refresh_preview_polygon()
        else:
            # Compatibility with state captured before extent preservation was
            # introduced.
            centre = state.get("preview_center")
            scale = state.get("preview_scale", 0)
            if centre and scale > 0:
                self._sync_paper_magnification()
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
        self._update_watershed_option()
        self.previewFrame.setAnalysisGuideVisible(
            not use_polygon and not self._in_management()
        )
        if not use_polygon and self.previewCanvas.mapTool() is self.previewSelectTool:
            self._activate_map_navigation()
        self._update_preview_selection_controls()
        self._update_preview_layers()

    def _clear_area_selection_for_extent(self, *args):
        """Switching to the preview extent: keep the layer, drop its selection."""

        del args
        layer = self.aoiLayerCombo.currentLayer()
        if (
            self.mapExtentAreaRadio.isChecked()
            and layer is not None
            and layer.selectedFeatureCount() > 0
        ):
            layer.removeSelection()

    def _activate_map_navigation(self, *args):
        del args
        self.previewCanvas.setMapTool(self.previewPanTool)
        self.mapNavigationButton.setChecked(True)

    def _activate_polygon_selection(self, *args):
        del args
        if self.mapExtentAreaRadio.isChecked() or self._in_management():
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
        if self._in_management():
            # The analysis area is the whole fixed view; no AOI outline.
            self.previewCanvas.refresh()
            return
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
        key = self._dem_source_key()
        local = key == "local"
        self.localDemLabel.setVisible(local)
        self.localDemRow.setVisible(local)
        if hasattr(self, "localDemRubber"):
            self._update_local_dem_footprint()
        note = _dem_source_note(key)
        if not self._dem_source_available():
            # Why it is unavailable (outside the region, or no LERC support).
            item = self.demSourceCombo.model().item(self.demSourceCombo.currentIndex())
            note = item.toolTip() if item is not None else note
        self.demSourceNote.setText(note)
        # Remember the source before this change, to go back to it when the
        # Terrarium notice is not accepted.
        self._demSourceBeforeChange = getattr(self, "_demSourceCurrent", None)
        self._demSourceCurrent = key

    def _confirm_terrarium(self):
        """Ask the user to accept the Terrarium notice. Return whether accepted."""

        if QgsSettings().value(TERRARIUM_NOTICE_HIDDEN_KEY, False, type=bool):
            return True
        # The standard message box: icon, text, informative text, and the
        # "don't show again" check box at the left of the button row.
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Icon.Warning)
        box.setWindowTitle(tr("AWS Terrarium", "AWS Terrarium"))
        box.setText(tr(
            "The resolution of the source data differs by region.",
            "提供地域によって元データの解像度が異なります。",
        ))
        box.setInformativeText(tr(
            "In some regions it may not give the result you need, so use it with care.",
            "求める結果が得られない地域もあるため、注意して使用してください。",
        ))
        box.setStandardButtons(
            QMessageBox.StandardButton.Ok | QMessageBox.StandardButton.Cancel
        )
        box.setDefaultButton(QMessageBox.StandardButton.Ok)
        hide_next = QCheckBox(tr("Don't show this again", "次回から表示しない"), box)
        box.setCheckBox(hide_next)
        _keep_message_lines(box)
        if box.exec() != QMessageBox.StandardButton.Ok:
            return False
        if hide_next.isChecked():
            QgsSettings().setValue(TERRARIUM_NOTICE_HIDDEN_KEY, True)
        return True

    def _confirm_wms_export(self):
        """Before an export: warn about plain WMS layers in the preview.

        Some WMS servers limit the image size of one request (WIDTH/HEIGHT).
        QGIS splits large requests by the limit the server announces, or
        2000 pixels; a smaller limit the server does not announce makes the
        layer drop out of the output while the export still succeeds. QGIS's
        layout designer warns the same way. XYZ and WMTS tiles are fetched
        tile by tile and are not affected. Returns whether to go on.
        """

        names = [layer.name() for layer in self._previewSourceLayers if _is_plain_wms(layer)]
        if not names or QgsSettings().value(WMS_EXPORT_NOTICE_HIDDEN_KEY, False, type=bool):
            return True
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Icon.Warning)
        box.setWindowTitle(tr("WMS layers in the preview", "プレビューにWMSレイヤーがあります"))
        box.setText(tr(
            "Some WMS servers limit the image size of one request.",
            "WMSサーバーの中には、1回の要求で返せる画像の大きさに上限があるものがあります。",
        ))
        box.setInformativeText(tr(
            "Beyond the limit the layer is left out of the output:\n{}",
            "上限を超えると、そのレイヤーは出力に入りません：\n{}",
        ).format("\n".join(names)))
        box.setStandardButtons(
            QMessageBox.StandardButton.Ok | QMessageBox.StandardButton.Cancel
        )
        box.setDefaultButton(QMessageBox.StandardButton.Ok)
        hide_next = QCheckBox(tr("Don't show this again", "次回から表示しない"), box)
        box.setCheckBox(hide_next)
        _keep_message_lines(box)
        if box.exec() != QMessageBox.StandardButton.Ok:
            return False
        if hide_next.isChecked():
            QgsSettings().setValue(WMS_EXPORT_NOTICE_HIDDEN_KEY, True)
        return True

    def _dem_source_activated(self, *args):
        """The user chose a DEM source in the combo."""

        del args
        key = self._dem_source_key() or ""
        previous = getattr(self, "_demSourceBeforeChange", None) or ""
        # Asked when Terrarium is chosen, not when moving between its zooms.
        if (
            key.startswith("terrarium:")
            and not previous.startswith("terrarium:")
            and not self._confirm_terrarium()
        ):
            self._set_dem_source(previous or dem_sources.DEFAULT_KEY)
            return
        if self._dem_source_key() == "local":
            # The DEM section has no room for this under the layer combo, so
            # it goes to the shared status. The plugin cannot tell elevation
            # from other single-band rasters.
            self.resultEdit.setPlainText(
                tr(
                    "Local was chosen as the DEM source.\nChoose an elevation "
                    "raster file with \u2026; other single-band rasters such as slope "
                    "cannot be told apart.",
                    "データの取得元にローカルが選択されました。\n「…」から標高のラスタの"
                    "ファイルを選んでください。傾斜などの1バンドのラスタは見分けられません。",
                )
            )
        self._fit_preview_to_local_dem()

    def _choose_local_dem_file(self, *args):
        del args
        settings = QgsSettings()
        start = (
            os.path.dirname(self._localDemPath)
            if self._localDemPath
            else settings.value("qlae/local_dem_dir", "", type=str)
        )
        path, _filter = QFileDialog.getOpenFileName(
            self,
            tr("Choose a local DEM file", "ローカルDEMファイルを選択"),
            start,
            tr("Raster files", "ラスタファイル")
            + " (*.tif *.tiff *.img *.asc *.vrt *.bil *.dem);;"
            + tr("All files", "すべてのファイル")
            + " (*)",
        )
        if not path:
            return
        settings.setValue("qlae/local_dem_dir", os.path.dirname(path))
        info, reason = _inspect_local_dem(path)
        if info is None:
            # Keep the previous file; say why this one was not taken.
            self.resultEdit.setPlainText(
                tr(
                    "This file cannot be used as a DEM: {}\n{}",
                    "このファイルはDEMとして使えません：{}\n{}",
                ).format(os.path.basename(path), reason)
            )
            return
        self._set_local_dem(path, info)
        self.resultEdit.setPlainText(
            tr(
                "Local DEM file loaded: {}\nCheck that it is an elevation raster; "
                "other single-band rasters such as slope cannot be told apart.",
                "ローカルDEMファイルを読み込みました：{}\n標高のラスタか確認してください。"
                "傾斜などの1バンドのラスタは見分けられません。",
            ).format(os.path.basename(path))
        )
        self._fit_preview_to_local_dem()

    def _set_local_dem(self, path, info=None):
        self._localDemPath = path or ""
        self._localDemInfo = info
        self.localDemEdit.setText(os.path.basename(path) if path else "")
        self.localDemEdit.setToolTip(path or "")
        self._update_local_dem_footprint()

    def _update_local_dem_footprint(self):
        """Show the local DEM's value area while it is the chosen source."""

        self.localDemRubber.reset(Qgis.GeometryType.Polygon)
        info = getattr(self, "_localDemInfo", None)
        if (
            not info
            or not info.get("footprint_wkt")
            or self._dem_source_key() != "local"
            or self._in_management()
        ):
            return
        geometry = QgsGeometry.fromWkt(info["footprint_wkt"])
        crs = QgsCoordinateReferenceSystem.fromWkt(info["crs_wkt"])
        if geometry.isNull() or not crs.isValid():
            return
        self.localDemRubber.setToGeometry(geometry, crs)

    def _fit_preview_to_local_dem(self, *args):
        """Fit the area of the local DEM that holds values, centred.

        The whole valid area fits inside the A-series preview; the limiting
        side (width or height) touches the frame.
        """

        del args
        if self._in_management() or self._dem_source_key() != "local":
            return
        info = getattr(self, "_localDemInfo", None)
        if not info:
            return
        source_crs = QgsCoordinateReferenceSystem.fromWkt(info["crs_wkt"])
        extent = QgsRectangle(*info["valid_extent"])
        destination_crs = self.previewCanvas.mapSettings().destinationCrs()
        if (
            destination_crs.isValid()
            and source_crs.isValid()
            and source_crs != destination_crs
        ):
            try:
                extent = QgsCoordinateTransform(
                    source_crs, destination_crs, QgsProject.instance().transformContext()
                ).transformBoundingBox(extent)
            except QgsCsException:
                return
        if extent.isEmpty():
            return
        self._sync_paper_magnification()
        self._set_preview_extent(extent)
        self.previewCanvas.setCenter(extent.center())
        self._stablePreviewExtent = QgsRectangle(self.previewCanvas.extent())
        self.previewCanvas.refresh()

    def _dem_source_available(self):
        index = self.demSourceCombo.currentIndex()
        item = self.demSourceCombo.model().item(index) if index >= 0 else None
        return item is not None and bool(item.flags() & Qt.ItemFlag.ItemIsEnabled)

    def _run_analysis(self):
        if self._terrainWorker is not None:
            # While running, the Run button is the Stop button.
            self._terrainWorker.cancel()
            self.runButton.setEnabled(False)
            self.resultEdit.setPlainText(tr("Stopping analysis...", "解析を中止しています…"))
            return
        try:
            specification = self._terrain_specification()
        except ValueError as error:
            self.resultEdit.setPlainText(str(error))
            return

        package_path = specification["package_path"]
        # Analyses are never overwritten: their ZIP can be open in QGIS
        # (exported layers), and Windows cannot replace an open file.
        if os.path.exists(package_path):
            self.resultEdit.setPlainText(_name_taken_text())
            self._update_run_button()
            return
        self._overwritePackagePath = None
        specification["overwrite"] = False
        if not self._confirm_dem_size(specification):
            return

        if "watershed" in specification.get("outputs", ()):
            # The user should know the analysis covers more than they chose.
            self.resultEdit.setPlainText(_watershed_status_text())
        else:
            self.resultEdit.setPlainText(tr("Starting analysis...", "解析を開始します…"))
        self.runButton.setText(tr("Stop", "中止"))
        self._style_run_button(running=True)
        # Enter must not stop a running analysis.
        self.runButton.setDefault(False)
        self.runButton.setAutoDefault(False)
        self.languageButton.setEnabled(False)
        self.loadAnalysisButton.setEnabled(False)
        worker = TerrainJobWorker(specification, self)
        self._terrainWorker = worker
        worker.message.connect(self._show_terrain_message)
        worker.done.connect(self._terrain_finished)
        worker.start()
        # The area is in the specification now; drop the selection made for
        # it in the preview (it is also the selection of the project layer).
        aoi_layer = self.aoiLayerCombo.currentLayer()
        if aoi_layer is not None and aoi_layer.selectedFeatureCount() > 0:
            aoi_layer.removeSelection()

    def _confirm_dem_size(self, specification):
        """Before an analysis: ask when the DEM to read is large.

        The size is the DEM held in memory (the cells of the area that is
        fetched, as float64), which is known before anything is downloaded.
        The download itself is not: tile ZIPs differ about tenfold by survey
        year (VIRTUAL SHIZUOKA), and older years are fetched to fill gaps, so
        the notice says it can be larger. Returns whether to go on.
        """

        estimate = _dem_size_estimate(specification)
        if estimate is None:
            return True
        area_km2, cells = estimate
        # The thresholds are areas of a 0.5 m DEM, compared as cell counts.
        reference_cells = 1_000_000.0 / (DEM_NOTICE_REFERENCE_CELL_M ** 2)
        if cells < DEM_NOTICE_KM2 * reference_cells:
            return True
        strong = cells >= DEM_STRONG_NOTICE_KM2 * reference_cells
        size_gb = cells * DEM_BYTES_PER_CELL / 1e9

        box = QMessageBox(self)
        box.setIcon(QMessageBox.Icon.Critical if strong else QMessageBox.Icon.Warning)
        box.setWindowTitle(tr("Large DEM", "大きなDEM"))
        box.setText(tr(
            "The DEM to read is about {} GB (area about {} km²).",
            "読み込むDEMは約{} GBです（範囲 約{} km²）。",
        ).format(_format_gb(size_gb), "{:,.0f}".format(area_km2)))
        lines = []
        if strong:
            lines.append(tr(
                "Memory may run short and QGIS or the PC may become unstable.",
                "メモリが足りなくなり、QGISやPCが不安定になるおそれがあります。",
            ))
        if "watershed" in specification.get("outputs", ()):
            lines.append(tr(
                "For catchments, the area is read {:g} times wider and higher.",
                "流域範囲図のため、範囲を縦横{:g}倍に広げて読み込みます。",
            ).format(WATERSHED_WIDEN))
        if specification.get("source_kind") != "local":
            lines.append(tr(
                "Depending on the location, the download can be larger than this.",
                "ダウンロードする箇所によっては、サイズがこれより大きくなる場合があります。",
            ))
        lines.append(tr(
            "Making the area smaller is recommended. Continue?",
            "範囲を狭くすることをおすすめします。続けますか？",
        ) if strong else tr(
            "The analysis takes time. Continue?",
            "解析に時間がかかります。続けますか？",
        ))
        box.setInformativeText("\n".join(lines))
        box.setStandardButtons(
            QMessageBox.StandardButton.Ok | QMessageBox.StandardButton.Cancel
        )
        box.setDefaultButton(
            QMessageBox.StandardButton.Cancel if strong else QMessageBox.StandardButton.Ok
        )
        _keep_message_lines(box)
        return box.exec() == QMessageBox.StandardButton.Ok

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
        if self.contoursCheck.isChecked():
            outputs.append("contours")
        if self.watershedCheck.isEnabled() and self.watershedCheck.isChecked():
            outputs.append("watershed")
        if not outputs:
            raise ValueError(
                tr("Choose at least one analysis output.", "解析出力を1つ以上選択してください。")
            )

        source_key = self._dem_source_key() or dem_sources.DEFAULT_KEY
        source_kind = source_key.partition(":")[0]
        if not self._dem_source_available():
            raise ValueError(self.demSourceNote.text() or tr(
                "This DEM source cannot be used here.",
                "この取得元はここでは使えません。",
            ))
        source_path = ""
        source_name = ""
        if source_kind == "local":
            source_path = self._localDemPath
            if not source_path:
                raise ValueError(
                    tr("Choose a local DEM file.", "ローカルDEMファイルを選んでください。")
                )
            if not os.path.isfile(source_path):
                raise ValueError(
                    tr(
                        "The local DEM file was not found: {}",
                        "ローカルDEMファイルが見つかりません：{}",
                    ).format(source_path)
                )
            source_name = os.path.basename(source_path)

        return {
            "analysis_name": analysis_name,
            "package_path": packages.package_path(project_directory, analysis_name),
            "plugin_version": _plugin_version(),
            "source_key": source_key,
            "source_kind": source_kind,
            "source_path": source_path,
            "source_name": source_name,
            "outputs": outputs,
            "area_mode": area_mode,
            "preview_landscape": self._previewLandscape,
            # The view the user saw when running, including the surroundings.
            # Analysis management shows exactly this view again.
            "preview_view": self._current_preview_view(),
            "aoi_wkt": geometry.asWkt(12),
            "aoi_crs_wkt": crs.toWkt(),
        }

    def _current_preview_view(self):
        extent = self.previewCanvas.extent()
        crs = self.previewCanvas.mapSettings().destinationCrs()
        if extent.isEmpty() or not crs.isValid():
            return None
        return {
            "extent": [
                extent.xMinimum(),
                extent.yMinimum(),
                extent.xMaximum(),
                extent.yMaximum(),
            ],
            "crs_wkt": crs.toWkt(),
            "orientation": "landscape" if self._previewLandscape else "portrait",
        }

    def _analysis_area(self):
        if self.mapExtentAreaRadio.isChecked():
            # Analyse exactly what the preview shows. The stored stable extent
            # is only the rectangle kept across resizes; after a portrait /
            # landscape switch the visible extent is wider or taller than it.
            # Fall back to it only while a resize is still being settled.
            settling = (
                self._resizePreviewExtent is not None
                or self._previewFrozenForWindowChange
            )
            if settling and self._stablePreviewExtent is not None:
                extent = QgsRectangle(self._stablePreviewExtent)
            else:
                extent = self.previewCanvas.extent()
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
        if is_japanese():
            message = _japanese_terrain_message(message)
        self.resultEdit.setPlainText(message)

    def _terrain_finished(self, result, cancelled, error):
        worker = self._terrainWorker
        self._terrainWorker = None
        if worker is not None:
            worker.deleteLater()
        self.runButton.setText(tr("Run", "実行"))
        self._style_run_button(running=False)
        self.runButton.setDefault(True)
        self._update_run_button()
        self.languageButton.setEnabled(True)
        self.loadAnalysisButton.setEnabled(True)
        if cancelled:
            self.resultEdit.setPlainText(tr("Analysis cancelled.", "解析を中止しました。"))
            self._restore_overwritten_package()
            return
        if error:
            self.resultEdit.setPlainText(
                tr("Analysis stopped: {}", "解析を中止しました：{}").format(
                    _terrain_error_text(error)
                )
            )
            self._refresh_saved_analyses()
            self._restore_overwritten_package()
            return

        package_path = result["package_path"]
        manifest = result["manifest"]
        self._refresh_saved_analyses(package_path)
        self._add_package_layers(package_path, manifest)
        self._overwritePackagePath = None
        fallback = _grass_fallback_text(manifest)
        if self._enter_management(package_path):
            # The switch cleared the status; the details are in the summary.
            self.resultEdit.setPlainText(
                tr("Analysis saved: {}", "解析を保存しました：{}").format(
                    manifest["analysis_name"]
                )
                + fallback
            )
        else:
            self.resultEdit.setPlainText(
                tr("Analysis saved.", "解析を保存しました。")
                + fallback
                + "\n"
                + _analysis_summary(manifest, package_path)
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
        # The saved analyses decide which names are taken.
        self._update_run_button()

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
        path = self.savedAnalysisCombo.currentData()
        if path and os.path.isfile(path) and _show_file_in_file_manager(path):
            return
        # Nothing chosen, or the file manager could not select it.
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
        # Loading switches to analysis management; the analysis name field and
        # the status box on the input page are left untouched.
        source = manifest.get("source", {})
        self._set_dem_source(
            dem_sources.key_from_legacy(
                source.get("kind", "local"), source.get("requested_dataset")
            )
        )
        output_keys = manifest.get("outputs", {})
        self.hillshadeCheck.setChecked("hillshade" in output_keys)
        self.slopeCheck.setChecked("slope" in output_keys)
        self.csMapCheck.setChecked("cs_map" in output_keys)
        self.contoursCheck.setChecked("contours" in output_keys)
        self.watershedCheck.blockSignals(True)
        self.watershedCheck.setChecked("watershed" in output_keys)
        self.watershedCheck.blockSignals(False)
        self._add_package_layers(path, manifest)
        if self._enter_management(path):
            # Shown after the switch cleared the status. The analysis name
            # field on the input page is deliberately not filled in.
            self.resultEdit.setPlainText(
                tr("Loaded analysis: {}", "解析を読み込みました：{}").format(
                    manifest["analysis_name"]
                )
            )
        else:
            # Stayed on the input page, so say why there.
            self.resultEdit.setPlainText(
                tr(
                    "The analysis results could not be loaded.",
                    "解析結果を読み込めません。",
                )
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
        # The managed analysis layers are about to go; return to the inputs
        # so the window reopens in its normal state.
        self._exit_management()
        self._remove_session_layers()

    def _remove_session_layers(self, keep_package_path=None):
        """Remove session analysis layers from the main map.

        Layers made permanent (planned Layers export) are never touched.
        ``keep_package_path`` keeps the layers of the analysis in use.
        """

        keep = os.path.abspath(keep_package_path) if keep_package_path else None
        project = QgsProject.instance()
        remove_ids = [
            layer.id()
            for layer in project.mapLayers().values()
            if _is_session_analysis_layer(layer)
            and (
                keep is None
                or layer.customProperty("qlae/package_path", "") != keep
            )
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
        # The main map holds only the analysis in use: drop earlier analyses
        # (their ZIPs remain and can be loaded again) and this package's own
        # older copy before adding it.
        self._remove_session_layers(keep_package_path=package_path)
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
            # Keep the legend (symbology) collapsed in the layer panel.
            node.setExpanded(False)
            return layer

        # Top to bottom. Slope is a terrain derivative (only its colours come
        # from Morizon Next), so it belongs here, not under a risk group.
        # Hillshade is multiplied, so its position barely matters; the opaque
        # DEM goes last where it does not cover the others.
        def add_vector(member_key, style_key, name):
            member = outputs.get(member_key)
            if not member:
                return
            layer = QgsVectorLayer(packages.vsi_path(package_path, member), name, "ogr")
            if not layer.isValid():
                return
            style = outputs.get(style_key)
            if style:
                _load_packaged_style(layer, package_path, style)
            # After the style: loading a QML replaces the custom properties.
            layer.setCustomProperty("qlae/package_path", os.path.abspath(package_path))
            layer.setCustomProperty("qlae/session_managed", True)
            project.addMapLayer(layer, False)
            node = terrain_group.addLayer(layer)
            node.setItemVisibilityChecked(True)
            node.setExpanded(False)

        # Lines above the rasters: catchments, then contours.
        add_vector("watershed", "watershed_style", tr("Catchments", "流域範囲"))
        add_vector("contours", "contours_style", tr("Contours", "等高線"))
        add("slope", tr("Slope", "傾斜"), terrain_group, True, _style_morizon_slope)
        add("hillshade", tr("Hillshade", "陰影起伏"), terrain_group, True, _style_hillshade)
        add("cs_map", tr("CS Map", "CS立体図"), terrain_group, True)
        # The DEM is input data, not a result: its own group below.
        data_group = analysis_group.addGroup(tr("Data", "データ"))
        add("dem", tr("DEM", "DEM"), data_group, False)
        data_group.setExpanded(False)
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

    def _normal_geometry_for_saving(self):
        if self.isMaximized() or self.isMinimized() or self.isFullScreen():
            # Qt's normalGeometry() can be screen-sized; prefer our record.
            return self._lastNormalGeometry or self.normalGeometry()
        if self._fills_available_screen() and self._lastNormalGeometry is not None:
            return self._lastNormalGeometry
        return self.geometry()

    def save_window_geometry(self):
        """Remember the window position and size for the next opening."""

        geometry = self._normal_geometry_for_saving()
        if geometry.width() <= 0 or geometry.height() <= 0:
            return
        settings = QgsSettings()
        settings.setValue(
            WINDOW_GEOMETRY_KEY,
            [geometry.x(), geometry.y(), geometry.width(), geometry.height()],
        )
        settings.setValue(WINDOW_MAXIMIZED_KEY, self.isMaximized())

    def restore_saved_window_geometry(self):
        """Put the window where it was last time. Run before the first show().

        Return False when nothing usable is saved (then the caller centres it).
        """

        settings = QgsSettings()
        try:
            x, y, width, height = (
                int(value) for value in settings.value(WINDOW_GEOMETRY_KEY, [])
            )
        except (TypeError, ValueError):
            return False
        if width <= 0 or height <= 0:
            return False
        # The screen it was on may be gone (a monitor removed).
        screen = QApplication.screenAt(QRect(x, y, width, height).center())
        if screen is None:
            return False
        available = screen.availableGeometry()
        width = min(width, available.width())
        height = min(height, available.height())
        # Keep the whole window, including the title bar, on that screen.
        x = max(available.left(), min(x, available.right() - width + 1))
        y = max(available.top(), min(y, available.bottom() - height + 1))
        self._aSeriesLayoutTimer.stop()
        self.setGeometry(x, y, width, height)
        self._set_hidden_child_geometry()
        self._apply_a_series_layout()
        if settings.value(WINDOW_MAXIMIZED_KEY, False, type=bool):
            self.setWindowState(
                self.windowState() | Qt.WindowState.WindowMaximized
            )
        return True

    def take_state(self):
        normal_geometry = self._normal_geometry_for_saving()
        state = {
            "normal_geometry": (
                normal_geometry.x(),
                normal_geometry.y(),
                normal_geometry.width(),
                normal_geometry.height(),
            ),
            "was_maximized": self.isMaximized(),
            "was_minimized": self.isMinimized(),
            "dem_source": self._dem_source_key(),
            "analysis_name": self.analysisNameEdit.text(),
            "saved_analysis_path": self.savedAnalysisCombo.currentData(),
            "outputs": {
                "hillshade": self.hillshadeCheck.isChecked(),
                "slope": self.slopeCheck.isChecked(),
                "cs_map": self.csMapCheck.isChecked(),
                "contours": self.contoursCheck.isChecked(),
                "watershed": self.watershedCheck.isChecked(),
            },
            "aoi_layer_id": _layer_id(self.aoiLayerCombo.currentLayer()),
            "local_dem_path": self._localDemPath,
            "overlay_layer_id": _layer_id(self.overlayLayerCombo.currentLayer()),
            "base_map_layer_id": _layer_id(self.baseMapCombo.currentLayer()),
            "area_mode": (
                "map_extent"
                if self.mapExtentAreaRadio.isChecked()
                else "polygon"
            ),
            "selected_only": self.selectedOnlyCheck.isChecked(),
            "preview_landscape": self._previewLandscape,
            "result_text": self.resultEdit.toPlainText(),
        }
        if self._in_management() and self._explorePreviewView:
            # Save the explore view; the management view is rebuilt from the
            # analysis itself after the new window is shown.
            state.update(self._explorePreviewView)
            state["management"] = {
                "path": self._managementPackagePath,
                "checked_ids": self.managementPanel.checkedLayerIds(),
            }
        else:
            state.update(self._preview_view_state())
        return state

    def restore_state(self, state):
        # This runs while the replacement language window is still hidden.
        # Keep all top-level geometry changes before its first show().
        dem_source = state.get("dem_source")
        if dem_source and ":" not in dem_source:
            # State saved before the grouped source list.
            dem_source = dem_sources.key_from_legacy(dem_source, state.get("gsi_dataset"))
        self._set_dem_source(dem_source)
        self.analysisNameEdit.setText(state.get("analysis_name", ""))
        outputs = state.get("outputs", {})
        self.hillshadeCheck.setChecked(outputs.get("hillshade", True))
        self.slopeCheck.setChecked(outputs.get("slope", True))
        self.csMapCheck.setChecked(outputs.get("cs_map", True))
        self.contoursCheck.setChecked(outputs.get("contours", True))
        self.watershedCheck.blockSignals(True)
        self.watershedCheck.setChecked(outputs.get("watershed", False))
        self.watershedCheck.blockSignals(False)
        self.selectedOnlyCheck.setChecked(state.get("selected_only", True))
        self.mapExtentAreaRadio.setChecked(
            state.get("area_mode", "polygon") == "map_extent"
        )

        project = QgsProject.instance()
        aoi = project.mapLayer(state.get("aoi_layer_id", ""))
        overlay = project.mapLayer(state.get("overlay_layer_id", ""))
        base_map = project.mapLayer(state.get("base_map_layer_id", ""))
        if aoi is not None:
            self.aoiLayerCombo.setLayer(aoi)
        local_dem = state.get("local_dem_path", "")
        if local_dem and os.path.isfile(local_dem):
            info, _reason = _inspect_local_dem(local_dem)
            if info is not None:
                self._set_local_dem(local_dem, info)
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
        self._pendingManagement = state.get("management")
        self._rebuiltForLanguage = True

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
            (QgsProject.instance().layersAdded, self._queue_layer_choices),
            (QgsProject.instance().layersRemoved, self._queue_layer_choices),
            (
                QgsProject.instance().layerTreeRoot().layerOrderChanged,
                self._queue_layer_choices,
            ),
            (
                QgsProject.instance().readProject,
                self._project_was_read,
            ),
        ):
            try:
                signal.disconnect(slot)
            except (TypeError, RuntimeError):
                pass
        self.previewRubber.reset(Qgis.GeometryType.Polygon)
        self.localDemRubber.reset(Qgis.GeometryType.Polygon)
        self.previewCanvas.setLayers([])
        for source_id in list(self._tileCopyWatches):
            self._unwatch_tile_source(source_id)
        for _zoom, copy in self._previewTileCopies.values():
            copy.deleteLater()
        self._previewTileCopies = {}
        for _key, finder in self._tileZoomFinders.values():
            finder.cancel()
        self._tileZoomFinders = {}
        # Put the project layers' scale limits back and stop following the
        # layer panel.
        self._scaleSuspender.restore()
        try:
            QgsProject.instance().layerTreeRoot().visibilityChanged.disconnect(
                self._queue_preview_layers
            )
        except (TypeError, RuntimeError):
            pass
        try:
            QApplication.instance().focusWindowChanged.disconnect(self._focus_window_changed)
        except (TypeError, RuntimeError):
            pass
        self._previewLayersTimer.stop()
        self._layerChoicesTimer.stop()
        for button, _in_use in self._visibility_buttons():
            button.detach()


def _layer_id(layer):
    return layer.id() if layer is not None else ""


def _show_file_in_file_manager(path):
    """Open the file manager with ``path`` selected. Return False if not possible."""
    path = os.path.abspath(path)
    if sys.platform.startswith("win"):
        # "/select," and the path must be separate arguments for explorer.
        return QProcess.startDetached(
            "explorer.exe", ["/select,", QDir.toNativeSeparators(path)]
        )
    if sys.platform == "darwin":
        return QProcess.startDetached("open", ["-R", path])
    # Linux and others: the freedesktop FileManager1 D-Bus interface
    # (Nemo, Nautilus, Dolphin, Caja, ...).
    uri = QUrl.fromLocalFile(path).toString(QUrl.ComponentFormattingOption.FullyEncoded)
    process = QProcess()
    process.start(
        "dbus-send",
        [
            "--session",
            "--print-reply",
            "--dest=org.freedesktop.FileManager1",
            "--type=method_call",
            "/org/freedesktop/FileManager1",
            "org.freedesktop.FileManager1.ShowItems",
            "array:string:{}".format(uri),
            "string:",
        ],
    )
    if not process.waitForStarted(2000):
        return False
    if not process.waitForFinished(5000):
        # The file manager may still be starting; do not open a second window.
        return True
    return (
        process.exitStatus() == QProcess.ExitStatus.NormalExit
        and process.exitCode() == 0
    )


_DEM_LABEL_ROLE = Qt.ItemDataRole.UserRole + 2


def _inspect_local_dem(path, max_side=1024):
    """Check a file can be a DEM and find the area that holds values.

    Returns (info, None) or (None, reason). No raster is marked as
    "elevation", so this only rejects what cannot be one: unreadable files,
    no georeferencing, more than one band, and 8-bit integers (256 levels:
    shaded relief, classes or display images). The valid area is found on a
    reduced copy (at most ``max_side`` pixels a side), so it is quick even
    for large DEMs and accurate to about one reduced pixel.
    """
    import numpy as np
    from osgeo import gdal

    try:
        dataset = gdal.Open(path)
    except RuntimeError:
        dataset = None
    if dataset is None:
        return None, tr("GDAL cannot open it.", "GDALで開けません。")
    if dataset.RasterCount != 1:
        return None, tr(
            "It has {} bands; a DEM has one.", "{}バンドあります。DEMは1バンドです。"
        ).format(dataset.RasterCount)
    band = dataset.GetRasterBand(1)
    if band.DataType in (gdal.GDT_Byte, getattr(gdal, "GDT_Int8", -1)):
        return None, tr(
            "It is an 8-bit raster (256 levels), not an elevation raster.",
            "8ビット（256段階）のラスタで、標高のラスタではありません。",
        )
    gt = dataset.GetGeoTransform(can_return_null=True)
    crs_wkt = dataset.GetProjection()
    if gt is None or not crs_wkt:
        return None, tr(
            "It has no coordinate reference system or position.",
            "座標系または位置の情報がありません。",
        )
    cols, rows = dataset.RasterXSize, dataset.RasterYSize
    factor = max(1.0, max(cols, rows) / float(max_side))
    out_cols = max(1, int(round(cols / factor)))
    out_rows = max(1, int(round(rows / factor)))
    data = band.ReadAsArray(buf_xsize=out_cols, buf_ysize=out_rows)
    if data is None:
        return None, tr("Its values cannot be read.", "値を読めません。")
    data = data.astype("float64")
    valid = np.isfinite(data) & (np.abs(data) < 1e30)
    nodata = band.GetNoDataValue()
    if nodata is not None:
        valid &= data != nodata
    if not valid.any():
        return None, tr("It holds no values.", "値が入っていません。")
    valid_rows = np.where(valid.any(axis=1))[0]
    valid_cols = np.where(valid.any(axis=0))[0]
    # Reduced pixel edges back to full-resolution pixel positions.
    c0 = valid_cols[0] * cols / out_cols
    c1 = (valid_cols[-1] + 1) * cols / out_cols
    r0 = valid_rows[0] * rows / out_rows
    r1 = (valid_rows[-1] + 1) * rows / out_rows
    corners = [gdal.ApplyGeoTransform(gt, c, r) for c in (c0, c1) for r in (r0, r1)]
    xs = [point[0] for point in corners]
    ys = [point[1] for point in corners]
    dataset = None
    return {
        "crs_wkt": crs_wkt,
        "valid_extent": (min(xs), min(ys), max(xs), max(ys)),
        "footprint_wkt": _valid_footprint_wkt(valid, gt, cols / out_cols, rows / out_rows, crs_wkt),
    }, None


def _valid_footprint_wkt(valid, gt, scale_x, scale_y, crs_wkt):
    """Outline of the cells holding values, from the reduced mask."""
    from osgeo import gdal, ogr, osr

    rows, cols = valid.shape
    reduced_gt = (
        gt[0], gt[1] * scale_x, gt[2] * scale_y,
        gt[3], gt[4] * scale_x, gt[5] * scale_y,
    )
    raster = gdal.GetDriverByName("MEM").Create("", cols, rows, 1, gdal.GDT_Byte)
    raster.SetGeoTransform(reduced_gt)
    raster.SetProjection(crs_wkt)
    band = raster.GetRasterBand(1)
    band.WriteArray(valid.astype("uint8"))
    reference = osr.SpatialReference()
    reference.ImportFromWkt(crs_wkt)
    memory = ogr.GetDriverByName("Memory").CreateDataSource("")
    layer = memory.CreateLayer("valid", reference, ogr.wkbPolygon)
    layer.CreateField(ogr.FieldDefn("value", ogr.OFTInteger))
    # The band is its own mask, so only cells with values become polygons.
    gdal.Polygonize(band, band, layer, 0)
    collection = ogr.Geometry(ogr.wkbMultiPolygon)
    for feature in layer:
        geometry = feature.GetGeometryRef()
        if geometry is not None:
            collection.AddGeometry(geometry.Clone())
    if collection.IsEmpty():
        return ""
    merged = collection.UnionCascaded()
    # Stair steps of the reduced cells are smoothed a little.
    merged = merged.SimplifyPreserveTopology(abs(reduced_gt[1]) * 0.5)
    return merged.ExportToWkt()


# ISO 216 A sizes, (short side, long side) in mm.
PAPER_SIZES_MM = {
    "A5": (148, 210),
    "A4": (210, 297),
    "A3": (297, 420),
    "A2": (420, 594),
    "A1": (594, 841),
    "A0": (841, 1189),
}
DEFAULT_PAPER = "A4"
EXPORT_DPI_CHOICES = (96, 150, 200, 300, 600)
# Range for a resolution typed in with "Custom...": QGIS's own (map export
# dialog and layout export resolution).
CUSTOM_DPI_RANGE = (1, 3000)
# The processing limit (resolution) is rounded down to these steps.
LIMIT_DPI_STEP = 50
DEFAULT_EXPORT_DPI = 200
# The GeoPDF starts at the default export resolution of a QGIS print layout.
DEFAULT_GEOPDF_DPI = 300
# Above this many pixels per image the export asks first, offering the
# processing limit of the paper (at 300 dpi: A1 is about 70 million, A2 35
# million).
LARGE_EXPORT_PIXELS = 60_000_000
# Field map app (PWA) that opens the GeoPDF export; the overview page links
# on to the app itself (https://avid.mokuren.ne.jp/fieldmap/).
AVID_MAPS_URL = "https://avid.mokuren.ne.jp/avidmaps/system_overview.php"


def _style_without_custom_properties():
    """Style categories for the exported .qml files: all but custom properties
    (the plugin's markers live there)."""

    category = getattr(QgsMapLayer, "StyleCategory", QgsMapLayer)
    all_styles = category.AllStyleCategories
    custom = category.CustomProperties
    flags_type = getattr(QgsMapLayer, "StyleCategories", None)
    if isinstance(flags_type, type):  # QGIS 3 (PyQt5): QFlags
        return flags_type(all_styles) & ~flags_type(custom)
    # QGIS 4 (PyQt6): the operators give an int; turn it back into the type.
    return category(int(all_styles) & ~int(custom))


# The one print layout the plugin keeps (each analysis replaces it).
TERRARIUM_NOTICE_HIDDEN_KEY = "qlae/terrarium_notice_hidden"
WMS_EXPORT_NOTICE_HIDDEN_KEY = "qlae/wms_export_notice_hidden"
WINDOW_GEOMETRY_KEY = "qlae/window_geometry"
WINDOW_MAXIMIZED_KEY = "qlae/window_maximized"

PRINT_LAYOUT_NAME = "Quick Landscape Analysis Explorer"

# Image export: (file dialog filter, extension, QgsMapRendererTask format).
IMAGE_FORMATS = (
    # (name on the button, file dialog filter, extension, QgsMapRendererTask format)
    ("PNG", "PNG (*.png)", ".png", "PNG"),
    ("JPG", "JPEG (*.jpg *.jpeg)", ".jpg", "JPG"),
    ("TIF", "TIFF (*.tif *.tiff)", ".tif", "TIF"),
)
# Extensions a typed file name may already end with, for each format.
_IMAGE_EXTENSIONS = {
    "PNG": (".png",),
    "JPG": (".jpg", ".jpeg"),
    "TIF": (".tif", ".tiff"),
}
IMAGE_FORMAT_KEY = "qlae/image_format"


def _stored_image_format():
    """The image format chosen last time (PNG at first)."""

    name = QgsSettings().value(IMAGE_FORMAT_KEY, "PNG", type=str)
    return name if name in _IMAGE_EXTENSIONS else "PNG"


# The plugin's own limit for one exported image (4 bytes per pixel), about
# 2 GB. Qt itself (5.15 / 6.8) does not limit an image's total size; this
# keeps the memory one export takes within bounds.
MAX_IMAGE_BYTES = 2 ** 31 - 1


def _write_image_crs(path, crs_wkt):
    """Record the CRS next to the image (.aux.xml); the world file has none."""

    if not crs_wkt:
        return
    try:
        from osgeo import gdal

        dataset = gdal.Open(path)
        if dataset is not None:
            dataset.SetProjection(crs_wkt)
            dataset = None
    except Exception:  # noqa: BLE001 - the image itself is already saved
        return


def _timestamp_note_text():
    return tr("Leave blank to use a timestamp.", "解析時空白はタイムスタンプで命名")


def _name_taken_short_text():
    return tr("This name already exists", "同じ名称が存在します")


def _name_taken_text():
    return tr(
        "An analysis with this name already exists. Change the analysis name.",
        "同じ名称が存在します。解析名を変えてください。",
    )


def make_print_layout(parent, iface, name, landscape, layers, legend_layers, extent,
                      crs, description, credits, summary):
    """Ask once, then make the plugin's one print layout and open it.

    Used by the Print layout button and by the layer panel menu of exported
    groups. Returns the text for the status, or None when cancelled.
    """
    manager = QgsProject.instance().layoutManager()
    # The plugin keeps one print layout: each analysis replaces the last.
    existing = manager.layoutByName(PRINT_LAYOUT_NAME)
    guide = _print_layout_guide(name)
    question = guide
    if existing is not None:
        previous = existing.customProperty("qlae/analysis_name", "") or "?"
        question += "\n\n" + tr(
            "The print layout made before ({}) is replaced with this analysis.",
            "前に作った印刷レイアウト（{}）を、この解析のものに置き換えます。",
        ).format(previous)
    box = QMessageBox(parent)
    box.setIcon(QMessageBox.Icon.Question)
    box.setWindowTitle(tr("Print layout", "印刷レイアウト"))
    box.setText(question)
    make_button = box.addButton(
        tr("Replace and make", "置き換えて作る") if existing is not None
        else tr("Make", "作る"),
        QMessageBox.ButtonRole.AcceptRole,
    )
    # The standard Cancel, as in the plugin's other message boxes.
    cancel_button = box.addButton(QMessageBox.StandardButton.Cancel)
    box.setDefaultButton(make_button)
    box.setEscapeButton(cancel_button)
    box.exec()
    if box.clickedButton() is not make_button:
        return None
    try:
        layout = _build_print_layout(
            name, landscape, layers, legend_layers, extent, crs,
            description, credits, summary,
        )
    except Exception as error:  # noqa: BLE001 - keep the old layout
        return tr(
            "The print layout could not be made: {}", "印刷レイアウトを作れませんでした：{}"
        ).format(error)
    layout.setName(PRINT_LAYOUT_NAME)
    layout.setCustomProperty("qlae/analysis_name", name)
    # Replace the old layout only once the new one is ready.
    if existing is not None:
        manager.removeLayout(existing)
    manager.addLayout(layout)
    if iface is not None and hasattr(iface, "openLayoutDesigner"):
        iface.openLayoutDesigner(layout)
    # The same guide, with "made" for "make".
    return tr(
        "Made the print layout of “{}” (layout name: {}).",
        "解析「{}」の印刷レイアウトを作りました（レイアウト名：{}）。",
    ).format(name, PRINT_LAYOUT_NAME) + "\n" + guide.split("\n", 1)[1]


def _print_layout_guide(name):
    """What a print layout of the analysis shows, before it is made."""

    return tr(
        "Make the print layout of the analysis “{}”.\n"
        "A print layout draws the layers as they look in the main map.\n"
        "Use it as it is, or change the look with the layer settings in the main map.",
        "解析「{}」の印刷レイアウトを作ります。\n"
        "印刷レイアウトは、メイン画面のレイヤーと同じ見た目で描かれます。\n"
        "このまま使うことも、メイン画面のレイヤーの設定で見た目を変えることもできます。",
    ).format(name)


def _layout_description(manifest):
    """Lines describing the data and methods of an analysis (print layout)."""

    if not manifest:
        return ""
    # The lines list everything the analysis made, shown on the map or not:
    # say so first, so a listed layer missing from the map is not a mismatch.
    lines = [tr(
        "Analysis this document is based on (including layers not shown on the map)",
        "この書類の元になった解析（地図に表示していないものを含みます）",
    )]
    created = manifest.get("created_at", "")
    try:
        created = datetime.datetime.fromisoformat(created).astimezone().strftime("%Y-%m-%d")
    except (TypeError, ValueError):
        created = created[:10]
    if created:
        lines.append(tr("Made: {}", "作成日：{}").format(created))
    grid = manifest.get("analysis_grid", {}) or {}
    cell = float(grid.get("cell_size_m", 0.0) or 0.0)
    mode = (manifest.get("area", {}) or {}).get("mode", "")
    area_name = (
        tr("map view", "画面の範囲") if mode == "map_extent"
        else tr("polygon layer", "ポリゴンレイヤー")
    )
    if cell and grid.get("columns") and grid.get("rows"):
        lines.append(
            tr("Area: {} (about {:,.0f} m × {:,.0f} m)", "範囲：{}（約 {:,.0f} m × {:,.0f} m）").format(
                area_name, cell * grid["columns"], cell * grid["rows"]
            )
        )
    source = manifest.get("source", {}) or {}
    dataset = source.get("dataset") or source.get("name", "")
    # Survey years where the source names them per tile (VIRTUAL SHIZUOKA,
    # Rhineland-Palatinate): tiles of one area can be from different years.
    years = [str(year) for year in source.get("survey_years") or ()]
    if dataset and years:
        lines.append(
            tr(
                "DEM: {} (surveyed {}; analysed on a {:.2f} m grid)",
                "DEM：{}（{}年の測量、{:.2f} mのグリッドで解析）",
            ).format(dataset, tr(", ", "・").join(years), cell)
        )
    elif dataset:
        lines.append(
            tr("DEM: {} (analysed on a {:.2f} m grid)", "DEM：{}（{:.2f} mのグリッドで解析）").format(
                dataset, cell
            )
        )
    outputs = manifest.get("outputs", {}) or {}
    if "hillshade" in outputs:
        lines.append(tr("Hillshade: from the DEM", "陰影起伏：DEMから作成"))
    if "slope" in outputs:
        lines.append(tr(
            "Slope: degrees, classes of 5° from 20° to 45°",
            "傾斜：度。20°から45°まで5°ごとの区分",
        ))
    if "cs_map" in outputs:
        lines.append(tr(
            "CS Map: curvature and slope combined",
            "CS立体図：曲率と傾斜を重ねたもの",
        ))
    contours_info = manifest.get("contours") or {}
    if "contours" in outputs:
        lines.append(tr(
            "Contours: every {:g} m (thicker every 20 m and {:g} m), from a DEM averaged to 5 m "
            "and smoothed",
            "等高線：{:g} m間隔（20 m・{:g} mごとに太線）。DEMを5 mに平均してなめらかにしたもの",
        ).format(contours_info.get("interval_m", 10), contours_info.get("index_interval_m", 100)))
    watershed_info = manifest.get("watershed") or {}
    if "watershed" in outputs:
        # Analyses made before the numpy computation have no engine: GRASS.
        if watershed_info.get("engine", "grass") == "grass":
            text = tr(
                "Catchments: GRASS r.watershed (MFD), smallest {:g} ha",
                "流域範囲：GRASS r.watershed（MFD）、最小{:g} ha",
            )
        else:
            text = tr(
                "Catchments: computed in the plugin (MFD, as GRASS r.watershed), "
                "smallest {:g} ha",
                "流域範囲：プラグイン内の計算（MFD、GRASS r.watershedに準じる）、最小{:g} ha",
            )
        lines.append(text.format(watershed_info.get("min_area_ha", 5)))
    lines.append("Quick Landscape Analysis Explorer {}".format(
        manifest.get("plugin_version", "")
    ).strip())
    return "\n".join(lines)


def _dem_credit(source):
    """Credit line for the DEM of an analysis."""

    kind = source.get("kind", "")
    if kind == "gsi":
        return tr("GSI elevation tiles ({})", "国土地理院 標高タイル（{}）").format(
            source.get("dataset", "")
        )
    if kind == "local":
        return tr("Local file: {}", "ローカルファイル：{}").format(source.get("name", ""))
    if source.get("credit"):
        # The credit line in the form the provider asks for, kept as it is.
        credit = "{} — {}".format(source.get("dataset", ""), source["credit"])
        if is_japanese() and kind in _PROVIDER_COUNTRIES:
            credit = "{}：{}".format(_PROVIDER_COUNTRIES[kind], credit)
        return credit
    provider = source.get("provider", "")
    separator = ", "
    if is_japanese():
        separator = "、"
        if kind in _JAPANESE_PROVIDERS:
            provider = _JAPANESE_PROVIDERS[kind]
        elif kind in _PROVIDER_COUNTRIES and provider:
            # The provider's own wording is the attribution: keep it as it
            # is, with the country in Japanese.
            provider = "{}：{}".format(_PROVIDER_COUNTRIES[kind], provider)
    parts = [provider, source.get("dataset", "")]
    if source.get("licence"):
        parts.append(source["licence"])
    elif source.get("attribution"):
        parts.append(source["attribution"])
    return separator.join(part for part in parts if part)


# DEM providers by name in Japanese display (Japanese sources) ...
_JAPANESE_PROVIDERS = {
    "vs_lp": "静岡県（VIRTUAL SHIZUOKA）",
    "nagano_sabo": "長野県 砂防課",
    "nagano_rinmu": "長野県 林務部",
}
# ... and the country put before an overseas provider's own wording.
_PROVIDER_COUNTRIES = {
    "ahn_dtm": "オランダ",
    "ign_mnt": "フランス",
    "linz_dem": "ニュージーランド",
    "rlp_dgm1": "ドイツ",
    "by_dgm1": "ドイツ",
    "terrarium": "全世界",
}


def _layer_credit(layer):
    """Credit of a project layer: its metadata rights, or GSI tiles from the URL."""

    rights = [text for text in layer.metadata().rights() if text.strip()]
    if rights:
        return "{}：{}".format(layer.name(), " / ".join(rights)) if is_japanese() else (
            "{}: {}".format(layer.name(), " / ".join(rights))
        )
    if "cyberjapandata.gsi.go.jp" in layer.source():
        return tr("{}: GSI Tiles (Geospatial Information Authority of Japan)",
                  "{}：地理院タイル（国土地理院）").format(layer.name())
    return ""


def _layout_credits(manifest, layers, analysis_layers):
    lines = []
    source = (manifest or {}).get("source", {}) or {}
    if source:
        lines.append(tr("DEM: {}", "DEM：{}").format(_dem_credit(source)))
    analysis = set(analysis_layers)
    for layer in layers:
        if layer in analysis:
            continue
        credit = _layer_credit(layer)
        if credit and credit not in lines:
            lines.append(credit)
    return "\n".join(lines)


def _safe_area_summary(package_path, manifest):
    """The area summary for the layout; the layout is made without it on errors."""

    try:
        from .terrain.area_summary import area_summary

        return area_summary(package_path, manifest)
    except Exception as error:  # noqa: BLE001 - the summary is optional
        QgsMessageLog.logMessage(
            "Area summary skipped: {}".format(error), PLUGIN_NAME, Qgis.MessageLevel.Warning
        )
        return None


def _summary_text(summary, show_slope, location=""):
    """Numbers of the area. Slope numbers only when the slope is on the map
    (what is shown and what is summed up stay the same)."""

    lines = [
        tr("Area: {:,.2f} ha", "面積：{:,.2f} ha").format(summary["area_ha"]),
        tr(
            "Elevation: lowest {:,.0f} m / highest {:,.0f} m / mean {:,.0f} m (relief {:,.0f} m)",
            "標高：最低 {:,.0f} m／最高 {:,.0f} m／平均 {:,.0f} m（高低差 {:,.0f} m）",
        ).format(
            summary["elevation_min"], summary["elevation_max"], summary["elevation_mean"],
            summary["elevation_max"] - summary["elevation_min"],
        ),
    ]
    if show_slope:
        lines.append(tr("Mean slope: {:.1f}°", "平均傾斜：{:.1f}°").format(summary["slope_mean"]))
    if location:
        lines.append(location)
    # Catchments whenever the analysis has them, shown on the map or not.
    if summary.get("catchments") is not None:
        lines.append(tr(
            "Catchments: {} ({} reach the edge of the fetched DEM, not whole)",
            "流域：{}（取得したDEMの端に届き、全体がわからないもの {}）",
        ).format(summary["catchments"], summary.get("catchments_cut") or 0))
        largest = summary.get("largest_catchments") or []
        if largest:
            lines.append(tr(
                "Largest catchments (whole): {}", "大きい流域（流域全体）：{}"
            ).format(" / ".join("{:,.1f} ha".format(item["area_ha"]) for item in largest)))
    return "\n".join(lines)


def _location_text(map_item):
    """Centre (latitude, longitude) and the scale on the paper."""

    center = map_item.extent().center()
    try:
        transform = QgsCoordinateTransform(
            map_item.crs(), QgsCoordinateReferenceSystem("EPSG:4326"), QgsProject.instance()
        )
        point = transform.transform(center)
    except QgsCsException:
        return tr("Scale on the paper: 1:{:,.0f}", "紙の上の縮尺：1:{:,.0f}").format(map_item.scale())
    return tr(
        "Centre: {:.5f}° N, {:.5f}° E / scale on the paper 1:{:,.0f}",
        "中心：北緯 {:.5f}°、東経 {:.5f}°／紙の上の縮尺 1:{:,.0f}",
    ).format(point.y(), point.x(), map_item.scale())


# Locator map: an OpenStreetMap layer the plugin makes once in the project.
LOCATOR_URL = "type=xyz&url=https://tile.openstreetmap.org/{z}/{x}/{y}.png&zmax=19&zmin=0"
LOCATOR_CREDIT = "© OpenStreetMap contributors"
LOCATOR_SCALE = 5.0  # the locator shows this many times the map's width


LOCATOR_SIZE_MM = (72.0, 54.0)
LOCATOR_DPI = 200


def _locator_picture(main_extent, crs):
    """The locator map drawn once into a PNG (base64), or "" when it fails.

    Uses an OpenStreetMap layer that exists only for this drawing: nothing is
    added to the project, and the user's own layers are not touched. The
    picture is embedded in the layout, so it needs no file.
    """
    from qgis.PyQt.QtCore import QBuffer, QByteArray, QIODevice, QPointF
    from qgis.core import QgsMapRendererSequentialJob, QgsMapSettings

    layer = QgsRasterLayer(LOCATOR_URL, "OpenStreetMap", "wms")
    if not layer.isValid():
        return ""
    width_px = int(LOCATOR_SIZE_MM[0] / 25.4 * LOCATOR_DPI)
    height_px = int(LOCATOR_SIZE_MM[1] / 25.4 * LOCATOR_DPI)
    area = QgsRectangle(main_extent)
    area.scale(LOCATOR_SCALE)
    settings = QgsMapSettings()
    settings.setLayers([layer])
    settings.setDestinationCrs(crs)
    settings.setTransformContext(QgsProject.instance().transformContext())
    settings.setOutputSize(QSize(width_px, height_px))
    settings.setOutputDpi(LOCATOR_DPI)
    settings.setBackgroundColor(QColor("white"))
    settings.setExtent(area)
    job = QgsMapRendererSequentialJob(settings)
    job.start()
    job.waitForFinished()
    image = job.renderedImage()
    if image.isNull():
        return ""
    # Frame of the main map.
    to_pixel = settings.mapToPixel()
    top_left = to_pixel.transform(main_extent.xMinimum(), main_extent.yMaximum())
    bottom_right = to_pixel.transform(main_extent.xMaximum(), main_extent.yMinimum())
    painter = QPainter(image)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setPen(QPen(QColor(200, 0, 0), max(2.0, LOCATOR_DPI / 60.0)))
    painter.setBrush(QColor(255, 0, 0, 40))
    painter.drawRect(QRectF(
        QPointF(top_left.x(), top_left.y()), QPointF(bottom_right.x(), bottom_right.y())
    ))
    painter.end()
    data = QByteArray()
    buffer = QBuffer(data)
    buffer.open(QIODevice.OpenModeFlag.WriteOnly)
    image.save(buffer, "PNG")
    buffer.close()
    return bytes(data.toBase64()).decode("ascii")


def _build_print_layout(name, landscape, layers, legend_layers, extent, crs,
                        description, credits, summary=None):
    """A4 print layout: the A-series map with blocks of information.

    Portrait analysis: A4 landscape page, map on the left, the blocks on the
    right (title, description, legend, comment, scale bar and credits).
    Landscape analysis: A4 portrait page, map on top; below it the title,
    description and comment on the left, the legend on the right, and the
    scale bar and credits along the bottom. Positions are computed so the
    blocks never run into each other.
    """
    from qgis.core import (
        QgsLayoutItemLabel,
        QgsLayoutItemLegend,
        QgsLayoutItemMap,
        QgsLayoutItemPicture,
        QgsFillSymbol,
        QgsLayoutItem,
        QgsLayoutItemScaleBar,
        QgsLayoutItemShape,
        QgsLayoutMeasurement,
        QgsLayoutPoint,
        QgsLayoutSize,
        QgsLayoutUtils,
        QgsLegendRenderer,
        QgsPrintLayout,
    )

    margin = 10.0
    gap = 4.0
    ratio = math.sqrt(2.0)
    layout = QgsPrintLayout(QgsProject.instance())
    layout.initializeDefaults()
    layout.setName(name)
    page = layout.pageCollection().page(0)
    if landscape:
        page_w, page_h = 210.0, 297.0
        map_w = page_w - 2 * margin
        map_h = map_w / ratio
    else:
        page_w, page_h = 297.0, 210.0
        map_h = page_h - 2 * margin
        map_w = map_h / ratio
    page.setPageSize(QgsLayoutSize(page_w, page_h))

    map_item = QgsLayoutItemMap(layout)
    layout.addLayoutItem(map_item)
    map_item.attemptMove(QgsLayoutPoint(margin, margin))
    map_item.attemptResize(QgsLayoutSize(map_w, map_h))
    map_item.setCrs(crs)
    map_item.setLayers(list(layers))
    map_item.setKeepLayerSet(True)
    map_item.setFrameEnabled(True)
    map_item.zoomToExtent(extent)

    def text_label(text, size, x, y, width, bold=False, frame=False):
        """A label whose height fits its text, lines wrapped at ``width``."""
        item = QgsLayoutItemLabel(layout)
        item.setText(text)
        font = QFont()
        font.setPointSizeF(size)
        font.setBold(bold)
        try:
            from qgis.core import QgsTextFormat

            text_format = QgsTextFormat()
            text_format.setFont(font)
            text_format.setSize(size)
            item.setTextFormat(text_format)
        except (ImportError, AttributeError):
            item.setFont(font)
        inner = 0.0
        if frame:
            item.setFrameEnabled(True)
            item.setFrameStrokeWidth(QgsLayoutMeasurement(0.2))
            # Room inside the frame for the user's own text.
            inner = 3.0
        item.setMarginX(inner)
        item.setMarginY(inner)
        layout.addLayoutItem(item)
        height = _text_height_mm(text, size, width, bold=bold, inner=inner)
        item.attemptMove(QgsLayoutPoint(x, y))
        item.attemptResize(QgsLayoutSize(width, height))
        return item, height

    # --- the blocks -------------------------------------------------------
    if landscape:
        top = margin + map_h + gap
        left_x, left_w = margin, map_w * 0.55
        right_x = margin + left_w + 2 * gap
    else:
        top = margin
        left_x = margin + map_w + 8.0
        left_w = page_w - left_x - margin
        right_x = left_x
    bottom = page_h - margin

    # Scale bar inside the map, bottom right, on a white backing.
    scalebar = QgsLayoutItemScaleBar(layout)
    scalebar.setStyle("Single Box")
    scalebar.setLinkedMap(map_item)
    scalebar.applyDefaultSize()
    scalebar.setUnits(Qgis.DistanceUnit.Meters)
    scalebar.setUnitLabel("m")
    # Four segments right of 0 and two left of it, on every page.
    scalebar.setNumberOfSegments(4)
    scalebar.setNumberOfSegmentsLeft(2)
    scalebar.setHeight(1.5)
    line_symbol = scalebar.lineSymbol().clone() if hasattr(scalebar, "lineSymbol") else None
    if line_symbol is not None:
        line_symbol.setWidth(0.3)
        scalebar.setLineSymbol(line_symbol)
    scalebar.setBackgroundEnabled(True)
    scalebar.setBackgroundColor(QColor(255, 255, 255, 220))
    scalebar.setBoxContentSpace(1.5)
    layout.addLayoutItem(scalebar)
    scalebar.refreshItemSize()
    bar = scalebar.sizeWithUnits()
    inset = 4.0
    scalebar.attemptMove(QgsLayoutPoint(
        margin + map_w - inset - bar.width(), margin + map_h - inset - bar.height()
    ))

    # North arrow inside the map, top left, on a see-through white disc.
    arrow_x, arrow_y, arrow_size = margin + inset + 2.0, margin + 5.0, 10.0
    disc_size = 14.0
    disc = QgsLayoutItemShape(layout)
    disc.setShapeType(QgsLayoutItemShape.Shape.Ellipse)
    disc.setSymbol(QgsFillSymbol.createSimple({
        "color": "255,255,255,190",
        "outline_style": "no",
    }))
    layout.addLayoutItem(disc)  # added first, so it lies under the arrow
    disc.attemptResize(QgsLayoutSize(disc_size, disc_size))
    offset = (disc_size - arrow_size) / 2.0
    disc.attemptMove(QgsLayoutPoint(arrow_x - offset, arrow_y - offset))
    north = QgsLayoutItemPicture(layout)
    _set_north_arrow_picture(north)
    north.setLinkedMap(map_item)
    # The arrow is narrower than its box: centre it, so it sits on the disc.
    north.setPictureAnchor(QgsLayoutItem.ReferencePoint.Middle)
    layout.addLayoutItem(north)
    north.attemptResize(QgsLayoutSize(arrow_size, arrow_size))
    north.attemptMove(QgsLayoutPoint(arrow_x, arrow_y))

    # Slope on the map: its table. Otherwise the locator map (OSM), or the
    # elevation bands when there is no locator layer.
    show_slope = any("slope_deg" in layer.source() for layer in legend_layers)
    locator_png = ""
    if summary and not show_slope:
        locator_png = _locator_picture(map_item.extent(), map_item.crs())
    use_locator = bool(locator_png)
    if use_locator:
        credits = "\n".join(
            line for line in (credits, tr("Locator map: {}", "位置図：{}").format(LOCATOR_CREDIT))
            if line
        )

    # Credits along the bottom.
    credits_item = None
    if credits:
        credits_item, credits_h = text_label(
            tr("Sources\n", "出典\n") + credits, 7, left_x, 0, page_w - margin - left_x
        )
        credits_item.attemptMove(QgsLayoutPoint(left_x, bottom - credits_h))
        bottom -= credits_h + 2.0

    # Title, description and comment.
    cursor = top
    _title, title_h = text_label(name, 15, left_x, cursor, left_w, bold=True)
    cursor += title_h + 1.0
    if description:
        _desc, desc_h = text_label(description, 7.5, left_x, cursor, left_w)
        cursor += desc_h + gap
    comment_h = 22.0
    comment_w = left_w
    comment_y = bottom - comment_h
    comment, _h = text_label(
        tr("Comment", "コメント"), 8, left_x, comment_y, comment_w, frame=True
    )
    comment.attemptResize(QgsLayoutSize(comment_w, comment_h))
    comment.setId(tr("Comment", "コメント"))
    if landscape:
        legend_top, legend_bottom = top, bottom
    else:
        legend_top, legend_bottom = cursor, comment_y - gap

    # Legend in the space left; two columns when one is too tall.
    legend = QgsLayoutItemLegend(layout)
    legend.setTitle(tr("Legend", "凡例"))
    legend.setLinkedMap(map_item)
    legend.setAutoUpdateModel(False)
    root = legend.model().rootGroup()
    root.clear()
    for layer in legend_layers:
        # Hillshade, CS Map and DEM have no legend worth printing (bands).
        if layer.type() == Qgis.LayerType.Vector or "slope_deg" in layer.source():
            _add_legend_layer(legend, root.addLayer(layer), layer)
    has_legend = bool(root.findLayers())
    if has_legend:
        layout.addLayoutItem(legend)
        legend.attemptMove(QgsLayoutPoint(right_x, legend_top))

    def legend_height():
        # The item's own size is only known once drawn; measure it here.
        context = QgsLayoutUtils.createRenderContextForLayout(layout, None)
        return QgsLegendRenderer(legend.model(), legend.legendSettings()).minimumSize(
            context
        ).height()

    if has_legend:
        if legend_height() > legend_bottom - legend_top:
            legend.setColumnCount(2)
            legend.setSplitLayer(True)
        legend.adjustBoxSize()
    summary_bottom = comment_y - gap
    if landscape:
        # The comment goes under the legend when it fits there, leaving the
        # left column to the summary.
        right_w = page_w - margin - right_x
        if legend_top + (legend_height() if has_legend else 0.0) + gap + comment_h <= bottom:
            comment.attemptMove(QgsLayoutPoint(right_x, bottom - comment_h))
            comment.attemptResize(QgsLayoutSize(right_w, comment_h))
            summary_bottom = bottom

    # Summary of the area: right of the legend (A4 landscape page) or under
    # the description (A4 portrait page). Left out when it does not fit.
    if summary:
        context = QgsLayoutUtils.createRenderContextForLayout(layout, None)
        legend_w = QgsLegendRenderer(legend.model(), legend.legendSettings()).minimumSize(
            context
        ).width() if has_legend else -3 * gap
        if landscape:
            box_x, box_y, box_w = left_x, cursor, left_w
            box_bottom = summary_bottom
        else:
            box_x = right_x + legend_w + 3 * gap
            box_y, box_w = legend_top, page_w - margin - box_x
            box_bottom = legend_bottom
        placed_locator = False
        if box_w >= 55.0:
            placed_locator = _add_area_summary(
                layout, summary, box_x, box_y, box_w, box_bottom, text_label,
                show_slope, _location_text(map_item),
                locator_png if use_locator else None,
            )
        if use_locator and not placed_locator and credits_item is not None:
            # No locator on the page after all: drop its credit line.
            locator_credit = tr("Locator map: {}", "位置図：{}").format(LOCATOR_CREDIT)
            credits_item.setText(credits_item.text().replace("\n" + locator_credit, ""))
    return layout


def _add_area_summary(layout, summary, x, y, width, bottom, text_label,
                      show_slope=True, location="", locator=None):
    """Heading, numbers and a table or the locator map, if they fit.

    The slope class table when the slope is on the map; otherwise the
    locator map (``locator`` = PNG in base64), or the elevation band table
    when there is no locator.
    """
    from qgis.core import (
        QgsLayoutFrame,
        QgsLayoutItemManualTable,
        QgsLayoutPoint,
        QgsLayoutSize,
        QgsLayoutTableColumn,
        QgsLayoutUtils,
        QgsTableCell,
        QgsTextFormat,
    )

    def text_format(size, bold=False):
        font = QFont()
        font.setPointSizeF(size)
        font.setBold(bold)
        result = QgsTextFormat()
        result.setFont(font)
        result.setSize(size)
        return result

    rows = []
    if show_slope:
        first_header = tr("Slope (°)", "傾斜（°）")
        for color, item in zip(SLOPE_COLORS, summary["slope_classes"]):
            label_cell = QgsTableCell(item["label"])
            label_cell.setBackgroundColor(QColor(color))
            rows.append([
                label_cell,
                QgsTableCell("{:,.2f}".format(item["area_ha"])),
                QgsTableCell("{:.1f}".format(item["percent"])),
            ])
    else:
        first_header = tr("Elevation (m)", "標高（m）")
        for item in summary.get("elevation_bands") or []:
            rows.append([
                QgsTableCell(item["label"]),
                QgsTableCell("{:,.2f}".format(item["area_ha"])),
                QgsTableCell("{:.1f}".format(item["percent"])),
            ])
    # QGIS gives a table no height before a frame holds it; work it out from
    # the rows (header + classes) and the font.
    font = QFont()
    font.setPointSizeF(7.5)
    # Measured: a row is the font height plus the two cell margins.
    row_h = QgsLayoutUtils.fontHeightMM(font) + 2 * 0.8 + 0.1
    table_w = min(width, 80.0)
    table_h = row_h * (len(rows) + 1) + 1.0

    heading = tr("Summary of the area", "範囲の要約")
    numbers = _summary_text(summary, show_slope, location)
    heading_h = _text_height_mm(heading, 9, width, bold=True)
    numbers_h = _text_height_mm(numbers, 7.5, width)
    # The locator shrinks to the room left (not below 30 mm high); when it
    # does not fit, the elevation table is used instead.
    room = bottom - (y + heading_h + numbers_h + 1.0)
    locator_w = min(width, LOCATOR_SIZE_MM[0])
    locator_h = locator_w * LOCATOR_SIZE_MM[1] / LOCATOR_SIZE_MM[0]
    if locator is not None and room < locator_h:
        locator_h = room
        locator_w = locator_h * LOCATOR_SIZE_MM[0] / LOCATOR_SIZE_MM[1]
    if locator is not None and locator_h < 30.0:
        locator = None
    block_h = locator_h if locator is not None else (table_h if rows else 0.0)
    # Nothing is created unless it all fits: a table made for the layout but
    # never added to it crashes QGIS when the layout is deleted.
    if room < block_h:
        return False
    text_label(heading, 9, x, y, width, bold=True)
    y += heading_h
    text_label(numbers, 7.5, x, y, width)
    y += numbers_h + 1.0
    if locator is not None:
        _add_locator_picture(layout, locator, x, y, locator_w, locator_h)
        return True
    if not rows:
        return False

    table = QgsLayoutItemManualTable.create(layout)
    layout.addMultiFrame(table)
    table.setTableContents(rows)
    table.setIncludeTableHeader(True)
    table.setHeaders([
        QgsLayoutTableColumn(first_header),
        QgsLayoutTableColumn(tr("Area (ha)", "面積（ha）")),
        QgsLayoutTableColumn(tr("Share (%)", "割合（%）")),
    ])
    table.setHeaderTextFormat(text_format(7.5, bold=True))
    table.setContentTextFormat(text_format(7.5))
    table.setCellMargin(0.8)
    table.setColumnWidths([table_w * 0.36, table_w * 0.32, table_w * 0.32])
    frame = QgsLayoutFrame(layout, table)
    frame.attemptMove(QgsLayoutPoint(x, y))
    frame.attemptResize(QgsLayoutSize(table_w + 2.0, table_h))
    table.addFrame(frame)
    return False


def _set_north_arrow_picture(item):
    """Japanese display: the Japanese-style north mark drawn for the plugin,
    embedded in the layout (no file needed). Otherwise the QGIS default."""

    path = os.path.join(os.path.dirname(__file__), "images", "north_jp.svg")
    if is_japanese() and os.path.isfile(path):
        import base64

        with open(path, "rb") as stream:
            data = base64.b64encode(stream.read()).decode("ascii")
        picture_format = getattr(Qgis, "PictureFormat", None)
        if picture_format is not None:
            item.setPicturePath("base64:" + data, picture_format.SVG)
        else:
            item.setPicturePath("base64:" + data)
        return
    item.setPicturePath(":/images/north_arrows/layout_default_north_arrow.svg")


def _add_locator_picture(layout, png_base64, x, y, width, height):
    """The locator map, embedded in the layout as a picture."""
    from qgis.core import QgsLayoutItemPicture, QgsLayoutPoint, QgsLayoutSize

    item = QgsLayoutItemPicture(layout)
    picture_format = getattr(Qgis, "PictureFormat", None)
    if picture_format is not None:
        item.setPicturePath("base64:" + png_base64, picture_format.Raster)
    else:
        item.setPicturePath("base64:" + png_base64)
    item.setResizeMode(QgsLayoutItemPicture.ResizeMode.Zoom)
    item.setFrameEnabled(True)
    layout.addLayoutItem(item)
    item.attemptMove(QgsLayoutPoint(x, y))
    item.attemptResize(QgsLayoutSize(width, height))
    item.setId(tr("Locator map", "位置図"))


def _text_height_mm(text, size, width, bold=False, inner=0.0):
    """Height of a layout label for this text, lines wrapped at ``width``."""
    from qgis.core import QgsLayoutUtils

    font = QFont()
    font.setPointSizeF(size)
    font.setBold(bold)
    text_width = max(1.0, width - 2 * inner - 1.0)
    rows = sum(
        max(1, math.ceil(QgsLayoutUtils.textWidthMM(font, line) / text_width))
        for line in text.split("\n")
    )
    return QgsLayoutUtils.fontHeightMM(font) * 1.25 * rows + 2 * inner + 1.0


def _add_legend_layer(legend, node, layer):
    """Tidy one layer of the layout legend.

    Drops the band name a raster legend starts with, and draws line samples
    thicker so thin map lines (contours) can be seen in the legend.
    """
    from qgis.core import QgsMapLayerLegendUtils

    model = legend.model()
    nodes = model.layerLegendNodes(node)
    if layer.type() == Qgis.LayerType.Raster and len(nodes) > 1:
        # The band name is a plain text node before the colour entries.
        if type(nodes[0]).__name__ == "QgsSimpleLegendNode":
            QgsMapLayerLegendUtils.setLegendNodeOrder(node, list(range(1, len(nodes))))
    elif layer.type() == Qgis.LayerType.Vector:
        for index, legend_node in enumerate(nodes):
            symbol_node = getattr(legend_node, "symbol", None)
            symbol = symbol_node() if callable(symbol_node) else None
            if symbol is None or symbol.type() != Qgis.SymbolType.Line:
                continue
            thicker = symbol.clone()
            thicker.setWidth(max(0.5, thicker.width() * 3.0))
            thicker.setOpacity(1.0)
            QgsMapLayerLegendUtils.setLegendNodeCustomSymbol(node, index, thicker)
    model.refreshLayerLegend(node)


def _rectangle_values(rectangle):
    return [rectangle.xMinimum(), rectangle.yMinimum(), rectangle.xMaximum(), rectangle.yMaximum()]


def _write_layout_record(folder, values):
    """The record the layer panel menu reads to make the print layout."""

    record = {"format": "qlae-layout-record", "format_version": 1,
              "plugin_version": _plugin_version()}
    record.update(values)
    with open(os.path.join(folder, packages.LAYOUT_RECORD_NAME), "w", encoding="utf-8") as stream:
        json.dump(record, stream, ensure_ascii=False, indent=2)


def _layer_member(layer, package_path):
    """The ZIP member a managed layer reads (outputs/contours.gpkg, ...)."""

    prefix = packages.vsi_path(package_path, "")
    source = layer.source().split("|", 1)[0].replace("\\", "/")
    return source[len(prefix):].lstrip("/") if source.startswith(prefix.rstrip("/")) else ""


def _extract_analysis_files(package_path, manifest, folder):
    """Write the data files of the analysis to ``folder``; {member: path}."""

    members = [
        member for key, member in (manifest.get("outputs") or {}).items()
        if member and not key.endswith("_style")
    ]
    os.makedirs(folder, exist_ok=True)
    written = {}
    with zipfile.ZipFile(package_path) as archive:
        for member in members:
            path = os.path.join(folder, os.path.basename(member))
            with archive.open(member) as source, open(path, "wb") as target:
                shutil.copyfileobj(source, target)
            written[member] = path
    return written


def _keep_message_lines(box):
    """Keep each line of a QMessageBox's text on one line.

    QMessageBox picks its own width and wraps the text; its text labels get
    the width of their longest line. The labels are found by Qt's internal
    names; if a Qt version names them differently, the box simply keeps its
    standard wrapping.
    """

    labels = [
        label
        for label in (
            box.findChild(QLabel, "qt_msgbox_label"),
            box.findChild(QLabel, "qt_msgbox_informativelabel"),
        )
        if label is not None
    ]
    if labels:
        longest = max(
            label.fontMetrics().horizontalAdvance(line)
            for label in labels
            for line in label.text().split("\n")
        )
        for label in labels:
            label.setMinimumWidth(longest + 8)


def _is_plain_wms(layer):
    """A WMS layer fetched as whole images (not XYZ or WMTS tiles)."""

    if layer is None or layer.providerType() != "wms":
        return False
    source = layer.source()
    return "type=xyz" not in source and "tileMatrixSet=" not in source


def _shown_in_layer_panel(layer):
    """The layer if the QGIS layer panel shows it (parents included)."""

    if layer is None:
        return None
    node = QgsProject.instance().layerTreeRoot().findLayer(layer.id())
    return layer if node is not None and node.isVisible() else None


# Progress messages of the analysis job (English, from the worker thread) in
# Japanese: fixed messages, then messages with numbers or names in them.
_TERRAIN_MESSAGES_JA = {
    "Preparing DEM...": "DEMを準備しています…",
    "Creating hillshade...": "陰影起伏を作成しています…",
    "Creating slope...": "傾斜を作成しています…",
    "Creating CS Map...": "CS立体図を作成しています…",
    "Creating catchments...": "流域範囲を作成しています…",
    "Creating contours...": "等高線を作成しています…",
    "Writing analysis package...": "解析ZIPを保存しています…",
    "Contours: smoothing the DEM...": "等高線：DEMをなめらかにしています…",
    "Contours: tracing lines...": "等高線：線を引いています…",
    "Contours: rounding lines...": "等高線：線を丸めています…",
    "Catchments: computing flow (GRASS r.watershed, MFD)...":
        "流域範囲：流れを計算しています（GRASS r.watershed・MFD）…",
    "Catchments: making polygons...": "流域範囲：ポリゴンを作っています…",
    "Catchments: labelling the catchments…": "流域範囲：流域を分けています…",
    "Catchments: GRASS could not run; computing in the plugin...":
        "流域範囲：GRASSを実行できなかったため、プラグイン内で計算します…",
    "Catchments: rounding the boundaries...": "流域範囲：境界を丸めています…",
    "Combining…": "タイルをまとめています…",
    "Checking LINZ tile index…": "LINZのタイルの一覧を確認しています…",
    "Checking the Rhineland-Palatinate tile list…":
        "ラインラント＝プファルツ州のタイルの一覧を確認しています…",
}
_TERRAIN_PATTERNS_JA = (
    (r"Catchments: preparing the (\S+) m grid\.\.\.",
     r"流域範囲：\1 mのグリッドを準備しています…"),
    (r"Catchments: routing the flow… (\d+)%", r"流域範囲：流れの道筋を決めています… \1%"),
    (r"Catchments: accumulating the flow \(MFD\)… (\d+)%",
     r"流域範囲：流れの量を集めています（MFD）… \1%"),
    (r"Catchments: finding the streams… (\d+)%", r"流域範囲：谷を探しています… \1%"),
    (r"Checking VIRTUAL SHIZUOKA tiles\.\.\. (\d+)/(\d+)",
     r"VIRTUAL SHIZUOKAのタイルを確認しています… \1/\2"),
    (r"Downloading VIRTUAL SHIZUOKA tiles\.\.\. (\d+)/(\d+)",
     r"VIRTUAL SHIZUOKAのタイルをダウンロードしています… \1/\2"),
    (r"Fetching GSI DEM tiles\.\.\. (.*)", r"国土地理院DEMを取得しています… \1"),
    (r"Fetching Terrarium DEM tiles\.\.\. (.*)", r"Terrarium DEMを取得しています… \1"),
    (r"Nagano DEM tile (\d+)/(\d+) \((.+?)\): locating\.\.\.",
     r"長野県DEMタイル \1/\2（\3）：場所を確認しています…"),
    (r"Nagano DEM tile (\d+)/(\d+) \((.+?)\): checking (.+)\.\.\.",
     r"長野県DEMタイル \1/\2（\3）：\4を確認しています…"),
    (r"Nagano DEM tile (\d+)/(\d+) \((.+?)\): downloading (.+) MB\.\.\.",
     r"長野県DEMタイル \1/\2（\3）：ダウンロードしています \4 MB…"),
    (r"Fetching (.+)… (\d+)/(\d+)", r"\1を取得しています… \2/\3"),
    (r"Reading elevation data… (\d+)%", r"標高データを読み込んでいます… \1%"),
    (r"Reading (.+) \((\d+) tile\(s\)\)…", r"\1を読み込んでいます（\2タイル）…"),
)


def _japanese_terrain_message(message):
    """A progress message of the analysis job in Japanese (as is if unknown)."""

    if message in _TERRAIN_MESSAGES_JA:
        return _TERRAIN_MESSAGES_JA[message]
    for pattern, replacement in _TERRAIN_PATTERNS_JA:
        match = re.fullmatch(pattern, message)
        if match:
            return match.expand(replacement)
    return message


# Why an analysis stopped (English, from the analysis job) in Japanese.
# Messages meant for the developer only stay in English.
_TERRAIN_ERRORS_JA = {
    "The analysis area has no usable geometry.": "解析範囲に使用できる形状がありません。",
    "The analysis area does not have a usable extent.": "解析範囲に使用できる広がりがありません。",
    "The analysis area CRS is not defined.": "解析範囲の座標系が定義されていません。",
    "The analysis area could not be transformed.": "解析範囲の座標を変換できませんでした。",
    "The analysis area is empty.": "解析範囲が空です。",
    "The analysis area does not cover any DEM cells.": "解析範囲にDEMのセルがありません。",
    "DEM does not contain valid elevations in the analysis area.":
        "解析範囲のDEMに有効な標高がありません。",
    "DEM has no valid elevation values in the analysis range.":
        "解析範囲のDEMに有効な標高がありません。",
    "No VIRTUAL SHIZUOKA tiles cover the analysis area.":
        "解析範囲にかかるVIRTUAL SHIZUOKAのタイルがありません。",
    "No VIRTUAL SHIZUOKA tiles are available for the analysis area.":
        "解析範囲で取得できるVIRTUAL SHIZUOKAのタイルがありません。",
    "No Nagano Prefecture DEM tiles cover the analysis area.":
        "解析範囲にかかる長野県DEMのタイルがありません。",
    "No Nagano Prefecture DEM tiles could be fetched for this area.":
        "この範囲の長野県DEMのタイルを取得できませんでした。",
    "No elevation data for this area (outside the source coverage).":
        "この範囲の標高データがありません（取得元の提供範囲の外です）。",
    "Could not combine the downloaded tiles.": "ダウンロードしたタイルをまとめられませんでした。",
    "Could not write the elevation GeoTIFF.": "標高のGeoTIFFを書き出せませんでした。",
    remote_dem.LERC_MISSING_MESSAGE:
        "このQGISのGDALは、LINZのデータに使われているLERC圧縮のGeoTIFFを読めません。"
        "LERCに対応したGDALのQGISを使ってください。",
    "The local DEM file cannot be opened.": "ローカルDEMファイルを開けません。",
    "DEM/DSM CRS is unknown. Assign the correct source CRS before analysis.":
        "DEMの座標系がわかりません。解析の前に正しい座標系を設定してください。",
    "DEM/DSM CRS is invalid. Check the source CRS before analysis.":
        "DEMの座標系が正しくありません。解析の前に座標系を確認してください。",
    "DEM/DSM requires a projected or geographic horizontal CRS.":
        "DEMには投影座標系か地理座標系が必要です。",
    "Automatic UTM conversion supports 80 S to 84 N. Supply a local metric projected DEM.":
        "UTMへの自動変換は南緯80°から北緯84°までです。メートル単位の投影座標系のDEMを用意してください。",
    "Clip extent is outside DEM bounds": "解析範囲がDEMの範囲の外です。",
    "Could not determine the analysis DEM grid.": "解析用のDEMのグリッドを決められませんでした。",
    "DEM conversion failed.": "DEMを変換できませんでした。",
    "DEM conversion produced no valid elevation values.":
        "DEMを変換しましたが、有効な標高の値がありません。",
    "GRASS (r.watershed) is not available in this QGIS. Enable the GRASS processing "
    "provider to create catchments.":
        "このQGISではGRASS（r.watershed）を使えません。流域範囲図を作るには、"
        "GRASSのプロセシングを有効にしてください。",
    "Could not prepare the DEM for catchments.": "流域範囲のためのDEMを準備できませんでした。",
    "GRASS r.watershed did not produce catchments.": "GRASS r.watershedで流域ができませんでした。",
    "The catchments could not be computed.": "流域を計算できませんでした。",
}
_TERRAIN_ERROR_PATTERNS_JA = (
    (r"All tile downloads failed\.\n(.*)", r"タイルのダウンロードがすべて失敗しました。\n\1"),
    (r"GSI DEM tiles are unavailable for this area\.", r"この範囲の国土地理院DEMを取得できません。"),
    (r"Terrarium DEM tiles are unavailable for this area\.", r"この範囲のTerrarium DEMを取得できません。"),
    (r"The service returned an error instead of elevation data: (.*)",
     r"取得元のサービスが標高データの代わりにエラーを返しました：\1"),
    (r"Unsupported elevation unit: (.+)\. Supply elevations in metres\.",
     r"標高の単位（\1）に対応していません。メートル単位の標高を用意してください。"),
    (r"File not found: (.+)", r"ファイルが見つかりません：\1"),
    (r"Cannot open DEM: (.+)", r"DEMを開けません：\1"),
)


def _terrain_error_text(error):
    """Why an analysis stopped, in the plugin's language (as is if unknown)."""

    if error == "package_exists":
        # Normally caught before running; the job's own check, worded.
        return _name_taken_text()
    if not is_japanese():
        return error
    if error in _TERRAIN_ERRORS_JA:
        return _TERRAIN_ERRORS_JA[error]
    for pattern, replacement in _TERRAIN_ERROR_PATTERNS_JA:
        match = re.fullmatch(pattern, error, re.DOTALL)
        if match:
            return match.expand(replacement)
    return error


def _grass_fallback_text(manifest):
    """A line (with a leading newline) when numpy took over from GRASS, or ""."""

    info = (manifest or {}).get("watershed") or {}
    if not info.get("grass_error"):
        return ""
    return "\n" + tr(
        "Catchments: GRASS could not run, so they were computed in the plugin "
        "(the reason is in the QGIS log).",
        "流域範囲：GRASSを実行できなかったため、プラグイン内の計算で作りました"
        "（理由はQGISのログにあります）。",
    )


def _watershed_status_text():
    return tr(
        "To get catchments right, a wider area is fetched and every analysis is run on it,\n"
        "then cut to the needed area. The analysis covers more than the area you chose.",
        "流域を正確に出すために、範囲を広く取得して全解析を実行し、その後必要な範囲で切り取ります。\n"
        "ユーザーが指定した範囲より広く解析します。",
    )


# Size notice before an analysis. The thresholds are areas of a 0.5 m DEM
# (the finest sources); other sources are compared by the number of cells,
# so a coarser DEM can cover a wider area before the notice.
DEM_NOTICE_KM2 = 40.0
DEM_STRONG_NOTICE_KM2 = 100.0
DEM_NOTICE_REFERENCE_CELL_M = 0.5
# The DEM is read as float64 (terrain/dem_loader.py).
DEM_BYTES_PER_CELL = 8

# Cell size (m) of the sources with a fixed grid.
_DEM_SOURCE_CELL_M = {
    "vs_lp": 0.5,
    "nagano_sabo": 0.5,
    "nagano_rinmu": 0.5,
    "ahn_dtm": 0.5,
    "ign_mnt": 0.5,
    "linz_dem": 1.0,
    "rlp_dgm1": 1.0,
    "by_dgm1": 1.0,
}
# Tile sources: zoom level of the 256-pixel tiles (terrain/dem_loader.py).
# "GSI best" takes 1 m tiles where they exist, so it is counted as 1 m.
_DEM_TILE_ZOOM = {
    "gsi:best": 17,
    "gsi:1m": 17,
    "gsi:5m": 15,
    "gsi:10m": 14,
    "terrarium:2m": 14,
    "terrarium:5m": 13,
    "terrarium:10m": 12,
}
_EARTH_CIRCUMFERENCE_M = 40075016.686


def _dem_size_estimate(specification):
    """(area in km², DEM cells) of the area an analysis reads, or None.

    The area is the one analyses/terrain.py fetches: the A-series frame of
    the analysis area, widened for catchments.
    """

    crs = QgsCoordinateReferenceSystem.fromWkt(specification.get("aoi_crs_wkt") or "")
    geometry = QgsGeometry.fromWkt(specification.get("aoi_wkt") or "")
    if not crs.isValid() or geometry.isNull() or geometry.isEmpty():
        return None
    box = geometry.boundingBox()
    try:
        xmin, xmax, ymin, ymax = _a_series_envelope(
            (box.xMinimum(), box.xMaximum(), box.yMinimum(), box.yMaximum()),
            bool(specification.get("preview_landscape", False)),
        )
    except Exception:  # an unusable extent: the analysis itself reports it
        return None
    if "watershed" in specification.get("outputs", ()):
        half_w = (xmax - xmin) * WATERSHED_WIDEN / 2.0
        half_h = (ymax - ymin) * WATERSHED_WIDEN / 2.0
        cx = (xmin + xmax) / 2.0
        cy = (ymin + ymax) / 2.0
        xmin, xmax, ymin, ymax = cx - half_w, cx + half_w, cy - half_h, cy + half_h
    frame = QgsGeometry.fromRect(QgsRectangle(xmin, ymin, xmax, ymax))

    project = QgsProject.instance()
    measure = QgsDistanceArea()
    measure.setSourceCrs(crs, project.transformContext())
    if not measure.setEllipsoid(crs.ellipsoidAcronym() or "EPSG:7030"):
        measure.setEllipsoid("EPSG:7030")
    area_m2 = measure.measureArea(frame)
    try:
        center = QgsCoordinateTransform(
            crs, QgsCoordinateReferenceSystem("EPSG:4326"), project
        ).transform(frame.centroid().asPoint())
    except QgsCsException:
        return None
    cell_m2 = _dem_cell_area_m2(specification, center.y())
    if not area_m2 or not cell_m2 or not math.isfinite(area_m2):
        return None
    return area_m2 / 1e6, area_m2 / cell_m2


def _dem_cell_area_m2(specification, latitude):
    """Ground area (m²) of one DEM cell for the chosen source, or None."""

    key = specification.get("source_key") or ""
    if key in _DEM_SOURCE_CELL_M:
        return _DEM_SOURCE_CELL_M[key] ** 2
    cos_lat = math.cos(math.radians(latitude))
    if key in _DEM_TILE_ZOOM:
        # Web Mercator pixels shrink on the ground by cos(latitude).
        side = _EARTH_CIRCUMFERENCE_M / (256 * 2 ** _DEM_TILE_ZOOM[key]) * cos_lat
        return side * side
    if specification.get("source_kind") != "local":
        return None
    from osgeo import gdal

    dataset = gdal.Open(specification.get("source_path") or "")
    if dataset is None:
        return None
    gt = dataset.GetGeoTransform(can_return_null=True)
    source_crs = QgsCoordinateReferenceSystem.fromWkt(dataset.GetProjection() or "")
    dataset = None
    if not gt:
        return None
    width, height = abs(gt[1]), abs(gt[5])
    if source_crs.isGeographic():
        width *= 111320.0 * cos_lat
        height *= 110574.0
    elif source_crs.isValid():
        factor = QgsUnitTypes.fromUnitToUnitFactor(
            source_crs.mapUnits(), Qgis.DistanceUnit.Meters
        )
        width *= factor
        height *= factor
    return width * height


def _format_gb(size_gb):
    if size_gb < 1.0:
        return "{:.2f}".format(size_gb)
    if size_gb < 10.0:
        return "{:.1f}".format(size_gb)
    return "{:,.0f}".format(size_gb)


def _load_packaged_style(layer, package_path, member):
    """Apply a QML stored in the analysis ZIP."""

    import zipfile

    try:
        with zipfile.ZipFile(package_path) as archive:
            qml = archive.read(member).decode("utf-8")
    except (OSError, KeyError, zipfile.BadZipFile):
        return
    document = QDomDocument()
    result = document.setContent(qml)
    # PyQt5 returns (ok, message, line, column); PyQt6 may return a result object.
    ok = result[0] if isinstance(result, tuple) else bool(result)
    if ok:
        layer.importNamedStyle(document)


def _dem_source_note(key):
    """Short note under the DEM source (kept to two lines in the section)."""

    notes = {
        "vs_lp": ("0.5 m tiles; large areas take time.",
                  "0.5 mタイル。広い範囲は時間がかかります。"),
        "nagano_sabo": ("0.5 m tiles; large areas take time.",
                        "0.5 mタイル。広い範囲は時間がかかります。"),
        "nagano_rinmu": ("0.5 m tiles; large areas take time.",
                         "0.5 mタイル。広い範囲は時間がかかります。"),
        "ahn_dtm": ("0.5 m; the PDOK service can be slow.",
                    "0.5 m。PDOKのサービスは遅いことがあります。"),
        "ign_mnt": ("0.5 m; not all of France is covered yet.",
                    "0.5 m。フランス全土はまだ覆われていません。"),
        "linz_dem": ("1 m; the AWS copy may lag LINZ releases.",
                     "1 m。AWS上の複製は最新版より遅れることがあります。"),
        "rlp_dgm1": ("1 m tiles; the newest survey of each tile is used.",
                     "1 mタイル。タイルごとに最新の測量を使います。"),
        "by_dgm1": ("1 m tiles; large areas take time.",
                    "1 mタイル。広い範囲は時間がかかります。"),
        "terrarium:2m": ("Global tiles for small areas. Detail differs by region.",
                         "全球のタイル。狭い範囲向け。解像度は地域で差があります。"),
        "terrarium:5m": ("Global tiles. Detail differs by region.",
                         "全球のタイル。解像度は地域で差があります。"),
        "terrarium:10m": ("Global tiles for wide areas. Detail differs by region.",
                          "全球のタイル。広い範囲向け。解像度は地域で差があります。"),
        "gsi:1m": ("Laser survey areas only.",
                   "航空レーザ測量の範囲だけです。"),
    }
    note = notes.get(key)
    return tr(*note) if note else ""


_PREVIEW_NAVIGATION_EVENTS = (
    QEvent.Type.Wheel,
    QEvent.Type.MouseButtonPress,
    QEvent.Type.MouseButtonRelease,
    QEvent.Type.MouseButtonDblClick,
    QEvent.Type.MouseMove,
    QEvent.Type.KeyPress,
    QEvent.Type.KeyRelease,
)


def _package_layers(package_path):
    """Session layers that belong to one analysis package."""

    absolute = os.path.abspath(package_path)
    return [
        layer
        for layer in QgsProject.instance().mapLayers().values()
        if layer.customProperty("qlae/package_path", "") == absolute
        and _is_session_analysis_layer(layer)
    ]


def _package_layer_group(package_path):
    """The analysis group in the QGIS layer panel that holds the package."""

    root = QgsProject.instance().layerTreeRoot()
    for layer in _package_layers(package_path):
        node = root.findLayer(layer.id())
        if node is None:
            continue
        # Layers sit in <plugin group>/<analysis>/<sub groups...>.
        group = node.parent()
        while (
            group is not None
            and group.parent() is not None
            and not (
                isinstance(group.parent(), QgsLayerTreeGroup)
                and group.parent().name() == PLUGIN_NAME
            )
        ):
            group = group.parent()
        # Never fall back to the whole layer tree (layers moved by the user).
        if isinstance(group, QgsLayerTreeGroup) and group.parent() is not None:
            return group
    return None


def _analysis_summary(manifest, package_path, include_name=True):
    grid = manifest.get("analysis_grid", {})
    source = manifest.get("source", {})
    source_name = source.get("dataset") or source.get("name", "")
    lines = []
    if include_name:
        lines.append(
            tr("Name: {}", "解析名：{}").format(manifest.get("analysis_name", ""))
        )
    lines.append(tr("DEM: {}", "DEM：{}").format(source_name))
    if grid:
        lines.append(
            tr("Grid: {:.3f} m / {} × {}", "グリッド：{:.3f} m / {} × {}").format(
                grid.get("cell_size_m", 0.0),
                grid.get("columns", 0),
                grid.get("rows", 0),
            )
        )
        lines.append(
            tr("Valid coverage: {:.3f}%", "有効範囲：{:.3f}%").format(
                grid.get("valid_coverage_percent", 0.0)
            )
        )
    lines.append(tr("Package: {}", "ZIP：{}").format(package_path))
    return "\n".join(lines)


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
_AREA_POLYGON_KEY = "area/polygon_layer_id"
_PREVIEW_ORIENTATION_KEY = "preview/orientation"
_PAPER_KEY = "preview/paper"
_EXPORT_DPI_KEY = "export/dpi"
_GEOPDF_DPI_KEY = "export/geopdf_dpi"
_OPTIONAL_LAYER_1_KEY = "management/optional_layer_1_id"
_OPTIONAL_LAYER_2_KEY = "management/optional_layer_2_id"
# On/off of the layer of each on/off button (stored only when clicked).
_OVERLAY_ON_KEY = "preview/overlay_on"
_BASE_MAP_ON_KEY = "preview/base_map_on"
_OPTIONAL_LAYER_1_ON_KEY = "management/optional_layer_1_on"
_OPTIONAL_LAYER_2_ON_KEY = "management/optional_layer_2_on"


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
    return session.is_session_layer(layer)


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
