# -*- coding: utf-8 -*-
"""Right-hand panel shown while an analysis is being managed.

The panel only lists what to show and asks to leave; the dialog owns the
preview canvas and decides how the checked layers are drawn.
"""

from qgis.PyQt.QtCore import Qt, pyqtSignal
from qgis.PyQt.QtWidgets import (
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QStyle,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)
from qgis.core import QgsLayerTreeGroup, QgsLayerTreeLayer

from .i18n import tr
from .layer_combo import LayerTreeOrderComboBox, LayerVisibilityButton


_LAYER_ID_ROLE = Qt.ItemDataRole.UserRole + 1


class AnalysisManagementPanel(QWidget):
    """Layer-panel-like choice of analysis layers for the preview."""

    exitRequested = pyqtSignal()
    renameRequested = pyqtSignal()
    layersChanged = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._packagePath = None
        self._updating = False

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)

        header = QHBoxLayout()
        header.setContentsMargins(0, 0, 0, 0)
        title = QLabel(tr("Analysis management", "解析管理"), self)
        title_font = title.font()
        title_font.setBold(True)
        title.setFont(title_font)
        self.restoreWindowButton = QPushButton(
            tr("Exit full screen", "全画面を終了"), self
        )
        self.restoreWindowButton.setIcon(
            self.style().standardIcon(QStyle.StandardPixmap.SP_TitleBarNormalButton)
        )
        self.restoreWindowButton.setVisible(False)
        header.addWidget(title)
        header.addStretch(1)
        header.addWidget(self.restoreWindowButton)
        layout.addLayout(header)

        analysis_box = QGroupBox(tr("Summary", "サマリー"), self)
        analysis_layout = QVBoxLayout(analysis_box)
        self.analysisNameLabel = QLabel(analysis_box)
        self.analysisNameLabel.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )
        name_font = self.analysisNameLabel.font()
        name_font.setBold(True)
        self.analysisNameLabel.setFont(name_font)
        # The name and, at the top right of the box, the button that renames it.
        name_row = QHBoxLayout()
        name_row.setContentsMargins(0, 0, 0, 0)
        name_row.addWidget(self.analysisNameLabel, 1)
        self.renameButton = QPushButton(
            # Leading/trailing spaces give the label breathing room, like the
            # Open folder button.
            tr("   Rename analysis   ", "   解析名を変更   "), analysis_box
        )
        self.renameButton.clicked.connect(self.renameRequested)
        name_row.addWidget(
            self.renameButton, 0, Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignTop
        )
        analysis_layout.addLayout(name_row)
        self.detailsLabel = QLabel(analysis_box)
        self.detailsLabel.setWordWrap(True)
        self.detailsLabel.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )
        self.detailsLabel.setStyleSheet("font-size: 9pt;")
        analysis_layout.addWidget(self.detailsLabel)
        layout.addWidget(analysis_box)

        layers_box = QGroupBox(tr("Shown in preview", "プレビューに表示"), self)
        layers_layout = QVBoxLayout(layers_box)
        self.layerTree = QTreeWidget(layers_box)
        self.layerTree.setHeaderHidden(True)
        self.layerTree.setRootIsDecorated(True)
        self.layerTree.itemChanged.connect(self._item_changed)
        layers_layout.addWidget(self.layerTree)
        note = QLabel(
            tr(
                "Checked layers are drawn below the optional layers and the "
                "overlay and above the base map, in this list's order.",
                "チェックしたレイヤーは、オプショナルレイヤーとオーバーレイの下、"
                "ベースマップの上に、この一覧の順で描きます。",
            ),
            layers_box,
        )
        note.setWordWrap(True)
        note.setStyleSheet("font-size: 9pt;")
        layers_layout.addWidget(note)
        layout.addWidget(layers_box, 1)

        # Two extra project layers drawn on top of everything (e.g. forest
        # compartments or roads for the export). Drawing order, top first:
        # optional 1, optional 2, overlay, analysis, base map.
        optional_box = QGroupBox(tr("Optional layers", "オプショナルレイヤー"), self)
        # Both on one row, like the preview-layer row below.
        optional_row = QHBoxLayout(optional_box)
        self.optionalLayerCombo1 = LayerTreeOrderComboBox(optional_box)
        self.optionalLayerCombo2 = LayerTreeOrderComboBox(optional_box)
        # Like the preview-layer row: each combo has the layer-panel on/off
        # button of its layer.
        self._optionalVisibilityButtons = []
        for index, combo in enumerate(
            (self.optionalLayerCombo1, self.optionalLayerCombo2), start=1
        ):
            combo.setAllowEmptyLayer(True)
            combo.layerChanged.connect(lambda *_args: self.layersChanged.emit())
            button = LayerVisibilityButton(combo, optional_box)
            button.setFixedHeight(combo.sizeHint().height())
            self._optionalVisibilityButtons.append(button)
            if index > 1:
                optional_row.addSpacing(8)
            optional_row.addWidget(
                QLabel(tr("Optional {}", "オプショナル{}").format(index), optional_box)
            )
            combo_row = QHBoxLayout()
            combo_row.setSpacing(4)
            combo_row.addWidget(combo, 1)
            combo_row.addWidget(button)
            optional_row.addLayout(combo_row, 1)

        # Below the analysis layers, so that the optional layers sit right
        # above the shared preview-layer row (all non-analysis layers together).
        footer = QHBoxLayout()
        footer.addStretch(1)
        self.exitButton = QPushButton(
            tr("Finish analysis management", "解析管理を終了"), self
        )
        # Blue, like the Run button: it switches back to the inputs.
        self.exitButton.setObjectName("qlaeExitButton")
        self.exitButton.setStyleSheet(
            "QPushButton#qlaeExitButton {background-color: #2b6cb0; color: white;"
            "font-weight: bold; border: 1px solid #1e4c7d; border-radius: 3px;"
            "padding: 4px 12px;}"
            "QPushButton#qlaeExitButton:hover {background-color: #245a93;}"
            "QPushButton#qlaeExitButton:pressed {background-color: #1e4c7d;}"
            "QPushButton#qlaeExitButton:disabled {background-color: #9aa5b1;"
            "border-color: #9aa5b1; color: white;}"
        )
        self.exitButton.clicked.connect(self.exitRequested)
        footer.addWidget(self.exitButton)
        layout.addLayout(footer)
        layout.addWidget(optional_box)

    def optionalVisibilityButtons(self):
        return tuple(self._optionalVisibilityButtons)

    def packagePath(self):
        return self._packagePath

    def setAnalysis(self, package_path, analysis_name, details=""):
        self._packagePath = package_path
        self.analysisNameLabel.setText(analysis_name)
        self.detailsLabel.setText(details)
        self.detailsLabel.setVisible(bool(details))

    def setLayerGroup(self, analysis_group, checked_ids=None):
        """Mirror the analysis group of the QGIS layer panel.

        ``checked_ids`` keeps earlier choices; without it the visibility in the
        QGIS layer panel is used as the starting state.
        """

        self._updating = True
        try:
            self.layerTree.clear()
            if analysis_group is not None:
                self._add_children(
                    self.layerTree.invisibleRootItem(), analysis_group, checked_ids
                )
            self.layerTree.expandAll()
        finally:
            self._updating = False
        self.layersChanged.emit()

    def clear(self):
        self._packagePath = None
        self.analysisNameLabel.clear()
        self.detailsLabel.clear()
        self._updating = True
        try:
            self.layerTree.clear()
        finally:
            self._updating = False

    def checkedLayerIds(self):
        """Checked layer IDs from top to bottom."""

        return self._layer_ids(checked_only=True)

    def layerIds(self):
        return self._layer_ids(checked_only=False)

    def _layer_ids(self, checked_only):
        ids = []

        def walk(item):
            for index in range(item.childCount()):
                child = item.child(index)
                layer_id = child.data(0, _LAYER_ID_ROLE)
                if layer_id:
                    if (
                        not checked_only
                        or child.checkState(0) == Qt.CheckState.Checked
                    ):
                        ids.append(layer_id)
                else:
                    walk(child)

        walk(self.layerTree.invisibleRootItem())
        return ids

    def _add_children(self, parent_item, group, checked_ids):
        for node in group.children():
            if isinstance(node, QgsLayerTreeGroup):
                item = QTreeWidgetItem(parent_item, [node.name()])
                self._add_children(item, node, checked_ids)
                if item.childCount() == 0:
                    parent_item.removeChild(item)
            elif isinstance(node, QgsLayerTreeLayer) and node.layer() is not None:
                layer = node.layer()
                item = QTreeWidgetItem(parent_item, [layer.name()])
                item.setData(0, _LAYER_ID_ROLE, layer.id())
                item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
                if checked_ids is None:
                    checked = node.isVisible()
                else:
                    checked = layer.id() in checked_ids
                item.setCheckState(
                    0, Qt.CheckState.Checked if checked else Qt.CheckState.Unchecked
                )

    def _item_changed(self, item, column):
        del column
        if self._updating or not item.data(0, _LAYER_ID_ROLE):
            return
        self.layersChanged.emit()
