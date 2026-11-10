# -*- coding: utf-8 -*-
"""Layer combo shared by the dialog and the analysis-management panel."""

from qgis.PyQt.QtCore import pyqtSignal
from qgis.PyQt.QtWidgets import QComboBox
from qgis.core import QgsMapLayerModel, QgsProject


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
