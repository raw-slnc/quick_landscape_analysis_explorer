# -*- coding: utf-8 -*-
"""Layer combo shared by the dialog and the analysis-management panel."""

from qgis.PyQt import sip
from qgis.PyQt.QtCore import pyqtSignal
from qgis.PyQt.QtWidgets import QComboBox, QToolButton
from qgis.core import QgsApplication, QgsLayerTreeGroup, QgsMapLayerModel, QgsProject

from .i18n import tr


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

    Like the preview's scale limits, this is a temporary change made by the
    plugin: the chosen on/off state is put into the layer panel only while
    the plugin window is in front (turning a layer on also turns on the
    groups above it). When the window goes behind, the check boxes this
    button changed get their earlier state back, and the choice is put in
    again when the window comes to the front. Closing the window, choosing
    another layer or ``forget()`` ends the choice.

    The button shows whether the layer is drawn in QGIS now (its own check
    box and every parent group checked), so changes made in the layer panel
    show here too.
    """

    def __init__(self, combo, parent=None):
        super().__init__(parent)
        self._combo = combo
        # The plugin's choice for the current layer: None (none), True, False.
        self._wanted = None
        # Check boxes changed for the choice: [(layer-tree node, earlier)].
        self._changed = []
        self._applied = False
        self.setCheckable(True)
        self.setAutoRaise(True)
        self.setToolTip(
            tr(
                "Turns the layer on or off in the QGIS layer panel while this "
                "window is in front (turning it on also turns on its groups); "
                "behind it, the layer panel is as before. Only layers that are "
                "on are drawn in the preview.",
                "このウィンドウが前面にある間、QGISのレイヤーパネルでレイヤーの表示を"
                "オン／オフします（オンにすると親グループもオン）。背面に回ると元に"
                "戻ります。オンのレイヤーだけプレビューに描きます。",
            )
        )
        self.clicked.connect(self._clicked)
        combo.layerChanged.connect(self._layer_changed)
        self._root = QgsProject.instance().layerTreeRoot()
        self._root.visibilityChanged.connect(self.refresh)
        self.refresh()

    def detach(self):
        """Stop following the layer panel (before the window goes away)."""

        self.forget()
        if self._root is not None and not sip.isdeleted(self._root):
            try:
                self._root.visibilityChanged.disconnect(self.refresh)
            except (TypeError, RuntimeError):
                pass
        self._root = None

    def setApplied(self, applied):
        """Put the choice into the layer panel (front) or take it out."""

        if applied and not self._applied:
            self._applied = True
            self._apply()
        elif not applied and self._applied:
            self._applied = False
            self._revert()
        self.refresh()

    def forget(self):
        """Give the layer panel its earlier state back and drop the choice."""

        self._revert()
        self._wanted = None
        self._applied = False
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
        visible = node is not None and node.isVisible()
        self.setEnabled(node is not None)
        self.blockSignals(True)
        self.setChecked(visible)
        self.blockSignals(False)
        self.setIcon(
            QgsApplication.getThemeIcon(
                "/mActionShowAllLayers.svg" if visible else "/mActionHideAllLayers.svg"
            )
        )

    def _layer_changed(self, *args):
        del args
        self.forget()

    def _clicked(self, checked):
        if self._node() is None:
            self.refresh()
            return
        # A click comes from the window in front: start from the earlier
        # state and put the new choice in.
        self._revert()
        self._wanted = bool(checked)
        self._applied = True
        self._apply()
        self.refresh()

    def _apply(self):
        node = self._node()
        if node is None or self._wanted is None or self._changed:
            return
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

    def _revert(self):
        changed, self._changed = self._changed, []
        for item, earlier in reversed(changed):
            if not sip.isdeleted(item):
                item.setItemVisibilityChecked(earlier)
