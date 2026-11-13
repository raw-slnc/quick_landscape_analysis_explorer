# -*- coding: utf-8 -*-
"""Layer combo shared by the dialog and the analysis-management panel."""

from qgis.PyQt import sip
from qgis.PyQt.QtCore import pyqtSignal
from qgis.PyQt.QtWidgets import QComboBox, QHBoxLayout, QLabel, QToolButton, QWidget
from qgis.core import QgsApplication, QgsLayerTreeGroup, QgsMapLayerModel, QgsProject

from .i18n import tr


def add_layer_choice(row_layout, label_text, combo, parent):
    """Add "label, combo, on/off button" to a row of layer choices.

    The preview-layer row and the optional-layer row are built the same way,
    so that, with labels of one width (align_layer_choice_labels), their
    combos and buttons line up. Returns (label, button).
    """

    label = QLabel(label_text, parent)
    holder = QWidget(parent)
    holder_layout = QHBoxLayout(holder)
    holder_layout.setContentsMargins(0, 0, 0, 0)
    holder_layout.setSpacing(4)
    button = LayerVisibilityButton(combo, holder)
    button.setFixedHeight(combo.sizeHint().height())
    holder_layout.addWidget(combo, 1)
    holder_layout.addWidget(button)
    if row_layout.count():
        row_layout.addSpacing(8)
    row_layout.addWidget(label)
    row_layout.addWidget(holder, 1)
    return label, button


def align_layer_choice_labels(labels):
    """Give the labels of the layer-choice rows the width of the longest."""

    width = max(label.sizeHint().width() for label in labels)
    for label in labels:
        label.setFixedWidth(width)


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


class LayerVisibilityButton(QToolButton):
    """Turns a combo's layer on or off in the QGIS layer panel, for a while.

    Choosing a layer turns it on (the dialog gives each layer its choice,
    ``setWanted``, and keeps the user's clicks in the project). Like the
    preview's scale limits, this is a temporary change made by the plugin:
    the choice is put into the layer panel only while the plugin window is
    in front of the QGIS main window (turning a layer on also turns on the
    groups above it). When the main window comes in front, the check boxes
    this button changed get their earlier state back, and the choice is put
    in again when the plugin window comes back. The dialog decides which is
    in front (``setApplied``); the choice itself stays.

    The button shows the choice. While the choice is in the layer panel,
    turning the layer on or off there changes the choice too.
    """

    # The user clicked the button: True (on) or False (off).
    wantedChosen = pyqtSignal(bool)
    # Set while any of these buttons changes the layer panel: buttons can
    # share parent groups, and one button's change is not the user's.
    _changingNow = 0

    def __init__(self, combo, parent=None):
        super().__init__(parent)
        self._combo = combo
        # The choice for the current layer: None (no layer), True, False.
        self._wanted = None
        # Check boxes changed for the choice: [(layer-tree node, earlier)].
        self._changed = []
        self._applied = False
        self.setCheckable(True)
        self.setAutoRaise(True)
        self.setToolTip(
            tr(
                "Turns the layer on or off in the QGIS layer panel while this "
                "window is in front of the QGIS main window (turning it on also "
                "turns on its groups); back in the main window, the layer panel "
                "is as before. Going to another application changes nothing. "
                "A layer is on when chosen; the choice is kept in the project. "
                "Only layers that are on are drawn in the preview.",
                "このウィンドウがQGISのメインウィンドウより前にある間、レイヤーパネルで"
                "レイヤーの表示をオン／オフします（オンにすると親グループもオン）。"
                "メインウィンドウに移ると元に戻ります。他のアプリに移っても変わりません。"
                "レイヤーを選ぶとオンになり、オン／オフはプロジェクトに記録されます。"
                "オンのレイヤーだけプレビューに描きます。",
            )
        )
        self.clicked.connect(self._clicked)
        # Connected before the dialog's own slots: the earlier layer gets its
        # state back before the new layer is given its choice.
        combo.layerChanged.connect(self._layer_changed)
        self._root = QgsProject.instance().layerTreeRoot()
        self._root.visibilityChanged.connect(self._panel_changed)
        self.refresh()

    def detach(self):
        """Stop following the layer panel (before the window goes away)."""

        self.setApplied(False)
        if self._root is not None and not sip.isdeleted(self._root):
            try:
                self._root.visibilityChanged.disconnect(self._panel_changed)
            except (TypeError, RuntimeError):
                pass
        self._root = None

    def wanted(self):
        return self._wanted

    def setWanted(self, wanted):
        """Set the choice for the current layer (None without a layer)."""

        wanted = None if wanted is None else bool(wanted)
        if wanted == self._wanted:
            return
        self._revert()
        self._wanted = wanted
        if self._applied:
            self._apply()
        self.refresh()

    def setApplied(self, applied):
        """Put the choice into the layer panel (front) or take it out."""

        if applied and not self._applied:
            self._applied = True
            self._apply()
        elif not applied and self._applied:
            self._applied = False
            self._revert()
        if not sip.isdeleted(self):
            self.refresh()

    def _node(self):
        layer = self._combo.currentLayer()
        if layer is None or self._root is None or sip.isdeleted(self._root):
            return None
        return self._root.findLayer(layer.id())

    def refresh(self, *args):
        del args
        if sip.isdeleted(self):
            return
        node = self._node()
        on = self._wanted if self._wanted is not None else (
            node is not None and node.isVisible()
        )
        self.setEnabled(node is not None)
        self.blockSignals(True)
        self.setChecked(bool(on))
        self.blockSignals(False)
        self.setIcon(
            QgsApplication.getThemeIcon(
                "/mActionShowAllLayers.svg" if on else "/mActionHideAllLayers.svg"
            )
        )

    def _panel_changed(self, *args):
        """The layer panel changed: follow the user's own on/off there."""

        del args
        if sip.isdeleted(self):
            return
        if self._applied and not LayerVisibilityButton._changingNow and self._wanted is not None:
            node = self._node()
            if node is not None and node.isVisible() != self._wanted:
                # The user turned it on or off in the layer panel while the
                # choice was in: that is the new choice. What it was before
                # the plugin changed it is still given back later.
                self._wanted = node.isVisible()
                self.wantedChosen.emit(self._wanted)
        self.refresh()

    def _layer_changed(self, *args):
        del args
        # The earlier layer gets its state back; the dialog gives the new
        # layer its choice.
        self._revert()
        self._wanted = None
        if not sip.isdeleted(self):
            self.refresh()

    def _clicked(self, checked):
        if self._node() is None:
            self.refresh()
            return
        self.setWanted(bool(checked))
        self.wantedChosen.emit(bool(checked))

    def _apply(self):
        node = self._node()
        if node is None or self._wanted is None or self._changed:
            return
        LayerVisibilityButton._changingNow += 1
        try:
            if self._wanted:
                items = [node]
                parent = node.parent()
                # The root (the only node without a parent) is not a group to check.
                while isinstance(parent, QgsLayerTreeGroup) and parent.parent() is not None:
                    items.append(parent)
                    parent = parent.parent()
                for item in items:
                    if not item.itemVisibilityChecked():
                        self._changed.append((item, False))
                        item.setItemVisibilityChecked(True)
            elif node.isVisible():
                self._changed.append((node, True))
                node.setItemVisibilityChecked(False)
        finally:
            LayerVisibilityButton._changingNow -= 1

    def _revert(self):
        changed, self._changed = self._changed, []
        LayerVisibilityButton._changingNow += 1
        try:
            for item, earlier in reversed(changed):
                if not sip.isdeleted(item):
                    item.setItemVisibilityChecked(earlier)
        finally:
            LayerVisibilityButton._changingNow -= 1
