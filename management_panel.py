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
from .ui_fonts import PLUGIN_SIZE_RULE
from .layer_combo import LayerTreeOrderComboBox, add_layer_choice


_LAYER_ID_ROLE = Qt.ItemDataRole.UserRole + 1
# A group in the analysis's layer group made by a regional analysis (the
# route of a country file, terrain/sources/) carries this custom property:
# it is listed in the right-hand column.
REGIONAL_GROUP_PROPERTY = "qlae/regional"
# A regional layer that is there for the base map (an orthophoto): in the
# layer panel in the basic Data group, not drawn; in analysis management in
# the right-hand column under Data, without a tick box.
BASE_MAP_ONLY_PROPERTY = "qlae/base_map_only"
# A regional layer kept in the analysis's Data group (the DSM, the
# orthophotos): listed in the right-hand column under Data.
REGIONAL_DATA_PROPERTY = "qlae/regional_data"
# The output a basic layer is (hillshade, slope, ...), and the basic output
# a regional layer is shown instead of: the two are ticked one at a time.
OUTPUT_KEY_PROPERTY = "qlae/output"
EXCLUDES_PROPERTY = "qlae/excludes"
# Outputs of which only one is ticked at a time: their colours mix (the CS
# Valley, a regional output of Japan, is a cut-out of the CS Map).
EXCLUSIVE_OUTPUTS = (
    frozenset(("cs_map", "slope")),
    frozenset(("cs_map", "cs_valley")),
)
# When both of such a pair are shown in the layer panel, analysis management
# starts with these unticked (the slope is kept, not the CS Map).
UNTICKED_FIRST = ("cs_map",)
# Widths of the basic and the regional columns.
BASIC_STRETCH, REGIONAL_STRETCH = 4, 5


def _role(layer):
    """What a layer is shown as: its basic output, or the basic output a
    regional layer is shown instead of (its "excludes")."""
    return (
        layer.customProperty(OUTPUT_KEY_PROPERTY)
        or layer.customProperty(EXCLUDES_PROPERTY)
        or ""
    )


def exclusive(layer, other):
    """Whether only one of the two layers can be ticked: the same role (the
    basic slope and the regional fitted slope; the DEM and the DEM with its
    holes filled), or roles whose colours mix (the CS Map and a slope)."""
    first, second = _role(layer), _role(other)
    if not first or not second or layer is other:
        return False
    return first == second or frozenset((first, second)) in EXCLUSIVE_OUTPUTS


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
            self.style().standardIcon(
                QStyle.StandardPixmap.SP_TitleBarNormalButton
            )
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
        # The name and, at the top right of the box, the button that renames
        # it.
        name_row = QHBoxLayout()
        name_row.setContentsMargins(0, 0, 0, 0)
        name_row.addWidget(self.analysisNameLabel, 1)
        self.renameButton = QPushButton(
            # Leading/trailing spaces give the label breathing room, like the
            # Open folder button.
            tr("   Rename analysis   ", "   解析名を変更   "),
            analysis_box,
        )
        self.renameButton.clicked.connect(self.renameRequested)
        name_row.addWidget(
            self.renameButton,
            0,
            Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignTop,
        )
        analysis_layout.addLayout(name_row)
        self.detailsLabel = QLabel(analysis_box)
        self.detailsLabel.setWordWrap(True)
        self.detailsLabel.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )
        analysis_layout.addWidget(self.detailsLabel)
        layout.addWidget(analysis_box)

        layers_box = QGroupBox(
            tr("Shown in preview", "プレビューに表示"), self
        )
        layers_layout = QVBoxLayout(layers_box)
        # Two columns: the basic analysis, and a regional analysis when the
        # analysis has one (the basic column takes the whole width otherwise).
        columns = QHBoxLayout()
        columns.setContentsMargins(0, 0, 0, 0)
        self._columns = columns
        self.layerTree = QTreeWidget(layers_box)
        self.regionalTree = QTreeWidget(layers_box)
        # Each column under its name, shown while there are two.
        self.basicHeading = QLabel(tr("Basic", "基本"), layers_box)
        self.regionalHeading = QLabel(tr("Regional", "地域"), layers_box)
        for tree, heading, stretch in (
            (self.layerTree, self.basicHeading, BASIC_STRETCH),
            (self.regionalTree, self.regionalHeading, REGIONAL_STRETCH),
        ):
            tree.setHeaderHidden(True)
            tree.setRootIsDecorated(True)
            tree.itemChanged.connect(self._item_changed)
            column = QVBoxLayout()
            column.setContentsMargins(0, 0, 0, 0)
            column.setSpacing(2)
            column.addWidget(heading)
            column.addWidget(tree, 1)
            columns.addLayout(column, stretch)
        self._show_regional(False)
        layers_layout.addLayout(columns, 1)
        note = QLabel(
            tr(
                "Checked layers are shown in the preview.",
                "チェックしたレイヤーをプレビューに表示します。",
            ),
            layers_box,
        )
        layers_layout.addWidget(note)
        layout.addWidget(layers_box, 1)

        # Two extra project layers drawn on top of everything (e.g. forest
        # compartments or roads for the export). Drawing order, top first:
        # optional 1, optional 2, overlay, analysis, base map.
        optional_box = QGroupBox(
            tr("Optional layers", "オプショナルレイヤー"), self
        )
        # Both on one row, built like the preview-layer row below (the dialog
        # gives all their labels one width), each combo with the layer-panel
        # on/off button of its layer.
        optional_row = QHBoxLayout(optional_box)
        self.optionalLayerCombo1 = LayerTreeOrderComboBox(optional_box)
        self.optionalLayerCombo2 = LayerTreeOrderComboBox(optional_box)
        self._optionalVisibilityButtons = []
        self._optionalLabels = []
        for index, combo in enumerate(
            (self.optionalLayerCombo1, self.optionalLayerCombo2), start=1
        ):
            combo.setAllowEmptyLayer(True)
            combo.layerChanged.connect(
                lambda *_args: self.layersChanged.emit()
            )
            label, button = add_layer_choice(
                optional_row,
                tr("Optional {}", "オプショナル{}").format(index),
                combo,
                optional_box,
            )
            self._optionalLabels.append(label)
            self._optionalVisibilityButtons.append(button)

        # Below the analysis layers, so that the optional layers sit right
        # above the shared preview-layer row (all non-analysis layers
        # together).
        footer = QHBoxLayout()
        footer.addStretch(1)
        self.exitButton = QPushButton(
            tr("Finish analysis management", "解析管理を終了"), self
        )
        # Blue, like the Run button: it switches back to the inputs.
        self.exitButton.setObjectName("qlaeExitButton")
        self.exitButton.setStyleSheet(
            'QPushButton#qlaeExitButton {background-color: #2b6cb0; '
            'color: white;'
            "font-weight: bold; "
            + PLUGIN_SIZE_RULE
            + " border: 1px solid #1e4c7d; border-radius: 3px;"
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

    def optionalLabels(self):
        return tuple(self._optionalLabels)

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
            self.regionalTree.clear()
            if analysis_group is not None:
                self._add_children(
                    self.layerTree.invisibleRootItem(),
                    analysis_group,
                    checked_ids,
                )
            self.layerTree.expandAll()
            self.regionalTree.expandAll()
            if checked_ids is None:
                # Starting from the layer panel (which is not exclusive):
                # one of each exclusive pair ticked.
                self._untick_exclusive_starts()
            self._mark_exclusive_pairs()
            self._show_regional(self.regionalTree.topLevelItemCount() > 0)
        finally:
            self._updating = False
        self.layersChanged.emit()

    def _show_regional(self, shown):
        """The regional column and the column names, or the basic list alone
        across the whole width."""
        for widget in (
            self.regionalTree,
            self.regionalHeading,
            self.basicHeading,
        ):
            widget.setVisible(shown)
        # A hidden column keeps its share of the width unless its stretch
        # goes too: the basic list then takes it all.
        self._columns.setStretch(1, REGIONAL_STRETCH if shown else 0)

    def clear(self):
        self._packagePath = None
        self.analysisNameLabel.clear()
        self.detailsLabel.clear()
        self._updating = True
        try:
            self.layerTree.clear()
            self.regionalTree.clear()
            self._show_regional(False)
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

        # The regional analysis is drawn over the basic one.
        walk(self.regionalTree.invisibleRootItem())
        walk(self.layerTree.invisibleRootItem())
        return ids

    def _add_children(self, parent_item, group, checked_ids):
        for node in group.children():
            if isinstance(node, QgsLayerTreeGroup) and node.customProperty(
                REGIONAL_GROUP_PROPERTY
            ):
                # The regional group ("Regional analysis"): in the right-hand
                # column, named there Analysis (the column is named Regional).
                item = QTreeWidgetItem(
                    self.regionalTree.invisibleRootItem(),
                    [tr("Analysis", "解析")],
                )
                self._add_children(item, node, checked_ids)
                if item.childCount() == 0:
                    self.regionalTree.invisibleRootItem().removeChild(item)
            elif isinstance(node, QgsLayerTreeGroup):
                item = QTreeWidgetItem(parent_item, [node.name()])
                self._add_children(item, node, checked_ids)
                if item.childCount() == 0:
                    parent_item.removeChild(item)
            elif (
                isinstance(node, QgsLayerTreeLayer)
                and node.layer() is not None
            ):
                layer = node.layer()
                if layer.customProperty(
                    REGIONAL_DATA_PROPERTY
                ) or layer.customProperty(BASE_MAP_ONLY_PROPERTY):
                    # Regional data (the DSM, the orthophotos): in the layer
                    # panel in the basic Data group, but it belongs to the
                    # regional analysis: listed in the right-hand column
                    # under Data. The orthophotos without a tick box (they
                    # are there for the base map).
                    item = QTreeWidgetItem(
                        self._regional_data_item(), [layer.name()]
                    )
                    item.setData(0, _LAYER_ID_ROLE, layer.id())
                    if layer.customProperty(BASE_MAP_ONLY_PROPERTY):
                        item.setFlags(
                            item.flags() & ~Qt.ItemFlag.ItemIsUserCheckable
                        )
                        continue
                    item.setFlags(
                        item.flags() | Qt.ItemFlag.ItemIsUserCheckable
                    )
                    checked = (
                        node.isVisible()
                        if checked_ids is None
                        else layer.id() in checked_ids
                    )
                    item.setCheckState(
                        0,
                        Qt.CheckState.Checked
                        if checked
                        else Qt.CheckState.Unchecked,
                    )
                    continue
                item = QTreeWidgetItem(parent_item, [layer.name()])
                item.setData(0, _LAYER_ID_ROLE, layer.id())
                item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
                if checked_ids is None:
                    checked = node.isVisible()
                else:
                    checked = layer.id() in checked_ids
                item.setCheckState(
                    0,
                    Qt.CheckState.Checked
                    if checked
                    else Qt.CheckState.Unchecked,
                )

    def _regional_data_item(self):
        """The Data heading of the right-hand column (made when first needed,
        after its Analysis heading)."""
        root = self.regionalTree.invisibleRootItem()
        title = tr("Data", "データ")
        for index in range(root.childCount()):
            if root.child(index).text(0) == title and not root.child(
                index
            ).data(0, _LAYER_ID_ROLE):
                return root.child(index)
        return QTreeWidgetItem(root, [title])

    def _item_changed(self, item, column):
        del column
        if self._updating or not item.data(0, _LAYER_ID_ROLE):
            return
        if item.checkState(0) == Qt.CheckState.Checked:
            self._untick_excluded(item.data(0, _LAYER_ID_ROLE))
        self.layersChanged.emit()

    def _mark_exclusive_pairs(self):
        """Layers of which only one can be ticked (see exclusive): "⇄" after
        their names, and the others named in the tooltip (as in G.S.I. and
        Fundamental GeoPackage Builder)."""
        from qgis.core import QgsProject

        project = QgsProject.instance()
        items = []
        for tree in (self.layerTree, self.regionalTree):
            for item in self._layer_items(tree.invisibleRootItem()):
                layer = project.mapLayer(item.data(0, _LAYER_ID_ROLE))
                if layer is not None:
                    items.append((item, layer))
        for item, layer in items:
            partners = [
                other_item.text(0).replace(" ⇄", "")
                for other_item, other in items
                if exclusive(layer, other)
            ]
            if not partners:
                continue
            if not item.text(0).endswith(" ⇄"):
                item.setText(0, item.text(0) + " ⇄")
            item.setToolTip(
                0,
                tr(
                    "Exclusive with “{}”: only one can be ticked.",
                    "「{}」と排他です。どちらか一方だけをチェックできます。",
                ).format(tr("”, “", "」「").join(partners)),
            )

    def _untick_exclusive_starts(self):
        """Of ticked layers exclusive with each other, untick those whose role
        is in UNTICKED_FIRST (the CS Map, when the slope is ticked too)."""
        from qgis.core import QgsProject

        project = QgsProject.instance()
        ticked = []
        for tree in (self.layerTree, self.regionalTree):
            for item in self._layer_items(tree.invisibleRootItem()):
                layer = project.mapLayer(item.data(0, _LAYER_ID_ROLE))
                if (
                    layer is not None
                    and item.checkState(0) == Qt.CheckState.Checked
                ):
                    ticked.append((item, layer))
        for item, layer in ticked:
            if item.checkState(0) != Qt.CheckState.Checked:
                continue
            if any(
                exclusive(layer, other)
                and other_item.checkState(0) == Qt.CheckState.Checked
                for other_item, other in ticked
            ):
                if _role(layer) in UNTICKED_FIRST:
                    item.setCheckState(0, Qt.CheckState.Unchecked)

    def _untick_excluded(self, layer_id):
        ('Ticking a layer unticks those it is exclusive with (see '
         'exclusive).')
        from qgis.core import QgsProject

        project = QgsProject.instance()
        ticked = project.mapLayer(layer_id)
        if ticked is None:
            return
        self._updating = True
        try:
            for tree in (self.layerTree, self.regionalTree):
                for item in self._layer_items(tree.invisibleRootItem()):
                    other = project.mapLayer(item.data(0, _LAYER_ID_ROLE))
                    if other is not None and exclusive(ticked, other):
                        item.setCheckState(0, Qt.CheckState.Unchecked)
        finally:
            self._updating = False

    def _layer_items(self, parent):
        for index in range(parent.childCount()):
            child = parent.child(index)
            if child.data(0, _LAYER_ID_ROLE):
                yield child
            else:
                yield from self._layer_items(child)
