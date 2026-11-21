# -*- coding: utf-8 -*-

import os

from qgis.PyQt.QtCore import QTimer, QUrl
from qgis.PyQt.QtGui import QDesktopServices, QIcon
from qgis.PyQt.QtWidgets import QAction, QMessageBox
from qgis.core import QgsProject

from .core import session

from .i18n import tr


PLUGIN_NAME = "Quick Landscape Analysis Explorer"


class QuickLandscapeAnalysisExplorer:
    """QGIS entry point for QLAE."""

    MENU = "Quick Landscape Analysis Explorer"

    def __init__(self, iface):
        self.iface = iface
        self.plugin_dir = os.path.dirname(__file__)
        self.action = None
        self.maps_action = None
        self.dialog = None
        self.group_menu = None

    def _action_text(self):
        # 予察: the desk study before field work, which is what the plugin is
        # for.
        return tr(PLUGIN_NAME, PLUGIN_NAME + "（地形予察）")

    def initGui(self):
        icon = QIcon(os.path.join(self.plugin_dir, "icon.svg"))
        self.action = QAction(
            icon, self._action_text(), self.iface.mainWindow()
        )
        self.action.triggered.connect(self.run)
        self.iface.addPluginToMenu(self.MENU, self.action)
        self.iface.addToolBarIcon(self.action)
        self.maps_action = QAction(
            self._maps_action_text(), self.iface.mainWindow()
        )
        self.maps_action.triggered.connect(self.open_maps4qgis)
        self.iface.addPluginToMenu(self.MENU, self.maps_action)
        # Analysis layers whose ZIP is gone are removed when a project opens.
        QgsProject.instance().readProject.connect(self._project_read)
        self._project_read()
        # Layer panel menu of exported groups (works with the window closed).
        from .layer_menu import ExportedGroupMenu

        self.group_menu = ExportedGroupMenu(self.iface)

    def _project_read(self, *args):
        del args
        # After the project has finished loading.
        QTimer.singleShot(0, session.remove_layers_without_package)

    def unload(self):
        if self.maps_action is not None:
            self.iface.removePluginMenu(self.MENU, self.maps_action)
            self.maps_action = None
        if self.action is not None:
            self.iface.removePluginMenu(self.MENU, self.action)
            self.iface.removeToolBarIcon(self.action)
            self.action = None
        try:
            QgsProject.instance().readProject.disconnect(self._project_read)
        except (TypeError, RuntimeError):
            pass
        if self.group_menu is not None:
            self.group_menu.unload()
            self.group_menu = None
        # The analysis layers belong to the project now; they stay.
        self._close_dialog(clear_analysis_layers=False)
        from .maps4qgis_server import stop_server

        stop_server()

    def run(self):
        if self.dialog is None:
            self._create_dialog()
            if not self.dialog.restore_saved_window_geometry():
                _center_on(self.dialog, self.iface.mainWindow())
        if self.dialog.isMinimized():
            self.dialog.showNormal()
        self.dialog.show()
        self.dialog.raise_()
        self.dialog.activateWindow()

    def _maps_action_text(self):
        return tr("Maps4QGIS from QLAE", "Maps4QGIS（QLAEから起動）")

    def open_maps4qgis(self):
        from .maps4qgis_server import get_server

        try:
            url = get_server().desktop_url()
            opened = QDesktopServices.openUrl(QUrl(url))
        except OSError as error:
            QMessageBox.warning(
                self.iface.mainWindow(), "Maps4QGIS", str(error)
            )
            return
        if not opened:
            QMessageBox.warning(
                self.iface.mainWindow(),
                "Maps4QGIS",
                tr(
                    "Could not open the browser.",
                    "ブラウザを開けませんでした。",
                ),
            )

    def _create_dialog(self):
        from .dialog import LandscapeExplorerDialog

        # No parent: this is an independent normal window. It can be minimized,
        # reached with Alt+Tab, and placed behind the QGIS main window.
        self.dialog = LandscapeExplorerDialog(self.iface, None)
        self.dialog.setWindowIcon(
            QIcon(os.path.join(self.plugin_dir, "icon.svg"))
        )
        self.dialog.languageChanged.connect(self._rebuild_dialog)

    def _close_dialog(self, clear_analysis_layers=False):
        if self.dialog is None:
            return
        self.dialog.save_window_geometry()
        self.dialog.cleanup(clear_analysis_layers=clear_analysis_layers)
        self.dialog.hide()
        self.dialog.deleteLater()
        self.dialog = None

    def _rebuild_dialog(self):
        state = self.dialog.take_state()
        self._close_dialog(clear_analysis_layers=False)
        if self.action is not None:
            self.action.setText(self._action_text())
        if self.maps_action is not None:
            self.maps_action.setText(self._maps_action_text())
        self._create_dialog()
        self.dialog.restore_state(state)
        self.run()


def _center_on(window, main_window):
    if main_window is None:
        return
    geometry = window.frameGeometry()
    geometry.moveCenter(main_window.frameGeometry().center())
    window.move(geometry.topLeft())
