# -*- coding: utf-8 -*-
"""Layer panel menu for exported groups: make the print layout.

A group exported with "Layers" holds ordinary layers in
exports/<analysis>/, with a record (qlae_layout.json) of what the print layout
needs. The plugin does not manage these groups; the menu only reads the
record. It is shown:

- enabled on a group whose layer files are in one folder with the record,
- disabled, with the reason, on a group whose files are in an exports folder
  of this plugin but the record is missing,
- not at all on other groups.
"""

import json
import os

from qgis.core import QgsCoordinateReferenceSystem, QgsLayerTreeGroup, QgsRectangle

from .core import packages
from .i18n import tr

_EXPORTS_PART = os.sep + os.path.join(packages.ROOT_FOLDER, packages.EXPORTS_FOLDER) + os.sep


def _file_path(layer):
    path = layer.source().split("|", 1)[0]
    return path if os.path.isfile(path) else ""


def group_record(group):
    """("ok", record path) / ("missing", None) / (None, None) for a group."""

    if not isinstance(group, QgsLayerTreeGroup):
        return None, None
    paths = [
        _file_path(node.layer()) for node in group.findLayers() if node.layer() is not None
    ]
    paths = [path for path in paths if path]
    if not paths:
        return None, None
    folders = {os.path.dirname(os.path.abspath(path)) for path in paths}
    if len(folders) == 1:
        record = os.path.join(folders.pop(), packages.LAYOUT_RECORD_NAME)
        if os.path.isfile(record):
            return "ok", record
    if any(_EXPORTS_PART in os.path.abspath(path) for path in paths):
        return "missing", None
    return None, None


class ExportedGroupMenu:
    """Adds "Make print layout" to the layer panel menu of exported groups."""

    def __init__(self, iface):
        self.iface = iface
        self.view = iface.layerTreeView() if iface is not None else None
        if self.view is not None and hasattr(self.view, "contextMenuAboutToShow"):
            self.view.contextMenuAboutToShow.connect(self._about_to_show)

    def unload(self):
        if self.view is not None and hasattr(self.view, "contextMenuAboutToShow"):
            try:
                self.view.contextMenuAboutToShow.disconnect(self._about_to_show)
            except (TypeError, RuntimeError):
                pass
        self.view = None

    def _about_to_show(self, menu):
        group = self.view.currentNode() if self.view is not None else None
        state, record = group_record(group)
        if state is None:
            return
        menu.addSeparator()
        if state == "ok":
            action = menu.addAction(tr("Make print layout", "印刷レイアウトを作成"))
            action.triggered.connect(
                lambda _checked=False, g=group, r=record: self._make(g, r)
            )
        else:
            action = menu.addAction(tr(
                "Make print layout (no record)", "印刷レイアウトを作成（記録がありません）"
            ))
            action.setEnabled(False)

    def _make(self, group, record_path):
        from qgis.core import Qgis

        from .dialog import make_print_layout

        bar = self.iface.messageBar()
        try:
            with open(record_path, encoding="utf-8") as stream:
                record = json.load(stream)
            extent = QgsRectangle(*record["extent"])
            crs = QgsCoordinateReferenceSystem.fromWkt(record["crs_wkt"])
        except (OSError, ValueError, KeyError, TypeError) as error:
            bar.pushMessage(
                tr("Print layout", "印刷レイアウト"),
                tr("The record could not be read: {}", "記録を読めませんでした：{}").format(error),
                Qgis.MessageLevel.Warning,
            )
            return
        # The layers of the group only, top to bottom as in the layer panel.
        layers = [node.layer() for node in group.findLayers() if node.layer() is not None]
        status = make_print_layout(
            self.iface.mainWindow(),
            self.iface,
            name=record.get("analysis_name") or group.name(),
            landscape=bool(record.get("landscape")),
            layers=layers,
            legend_layers=layers,
            extent=extent,
            crs=crs,
            description=record.get("description", ""),
            credits=record.get("credits", ""),
            summary=record.get("summary"),
        )
        if status:
            bar.pushMessage(
                tr("Print layout", "印刷レイアウト"), status.split("\n", 1)[0],
                Qgis.MessageLevel.Info,
            )
