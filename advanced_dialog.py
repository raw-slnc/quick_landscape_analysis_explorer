# -*- coding: utf-8 -*-
"""Advanced settings (the gear beside the Region section): where the DEM
tiles fetched for analyses are kept for reuse (core/dem_store.py)."""

import os

from qgis.core import QgsProject, QgsSettings
from qgis.PyQt.QtCore import Qt, QUrl
from qgis.PyQt.QtGui import QDesktopServices
from qgis.PyQt.QtWidgets import (
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QGroupBox,
    QGridLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QVBoxLayout,
)

from .core import dem_store, packages
from .i18n import tr
from .ui_fonts import use_plugin_font


def _gb(size):
    return "{:.2f}".format(size / 1e9) if size < 1e10 else "{:,.0f}".format(size / 1e9)


class AdvancedSettingsDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        use_plugin_font(self)
        self.setWindowTitle(tr("Advanced settings", "詳細設定"))
        self.setModal(True)
        layout = QVBoxLayout(self)

        box = QGroupBox(tr("DEM tiles kept for reuse", "取得したDEMの保存"), self)
        box_layout = QVBoxLayout(box)
        note = QLabel(tr(
            "DEM tiles fetched for an analysis are kept while QGIS runs, in the "
            "project's quick_landscape_analysis/dem_session folder, and used again "
            "when an analysis covers the same place; only the missing tiles are "
            "fetched. The survey years are checked every time, so a newer year is "
            "fetched. They are removed when the project is analysed after QGIS is "
            "started again.\n"
            "To keep them longer, choose a folder: the tiles are then kept there "
            "(not in the project) until you remove them. They can take much space. "
            "They can then be analysed without the network, with \"QLAE folder\" "
            "as the DEM source.\n"
            "Kept: VIRTUAL SHIZUOKA, Nagano, the German states, GSI and Terrarium "
            "tiles.",
            "解析で取得したDEMのタイルは、QGISを起動している間、プロジェクトの "
            "quick_landscape_analysis/dem_session フォルダーに一時保存し、同じ場所を"
            "解析するときに使い回します。足りないタイルだけを取得します。"
            "測量年度は毎回確認するので、新しい年度があれば取得します。"
            "QGISを起動し直してからこのプロジェクトで解析すると、消えます。\n"
            "長く残したいときは、保存場所を選んでください。タイルはプロジェクトではなく"
            "そこに残り、自分で消すまで残ります。容量を多く使うことがあります。"
            "DEMの取得元に「QLAEフォルダ」を選ぶと、ネットワークを使わずに解析できます。\n"
            "対象：VIRTUAL SHIZUOKA・長野県・ドイツの州・国土地理院・Terrarium のタイル。",
        ), box)
        note.setWordWrap(True)
        box_layout.addWidget(note)

        settings = QgsSettings()
        self.keepCheck = QCheckBox(
            tr("Keep the tiles in this folder", "タイルをこの保存場所に残す"), box
        )
        self.keepCheck.setChecked(settings.value(dem_store.SETTING_ENABLED, False, type=bool))
        box_layout.addWidget(self.keepCheck)
        row = QGridLayout()
        self.folderEdit = QLineEdit(settings.value(dem_store.SETTING_DIR, "", type=str), box)
        self.folderEdit.setPlaceholderText(tr("Folder", "保存場所のフォルダー"))
        choose = QPushButton(tr("Choose…", "選択…"), box)
        choose.clicked.connect(self._choose_folder)
        self.openFolderButton = QPushButton(tr("Open Folder", "フォルダを開く"), box)
        self.openFolderButton.clicked.connect(self._open_folder)
        button_width = max(choose.sizeHint().width(), self.openFolderButton.sizeHint().width()) + 24
        choose.setFixedWidth(button_width)
        self.openFolderButton.setFixedWidth(button_width)
        self.sizeLabel = QLabel(box)
        self.sizeLabel.setWordWrap(True)
        self.sizeLabel.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)
        row.addWidget(self.folderEdit, 0, 0)
        row.addWidget(choose, 0, 1)
        row.addWidget(self.sizeLabel, 1, 0, Qt.AlignmentFlag.AlignTop)
        row.addWidget(self.openFolderButton, 1, 1)
        row.setColumnStretch(0, 1)
        box_layout.addLayout(row)
        box_layout.addStretch(1)
        layout.addWidget(box)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel, self
        )
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText(tr("Cancel", "キャンセル"))
        buttons.accepted.connect(self._accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

        self.keepCheck.toggled.connect(self._update_state)
        self.folderEdit.textChanged.connect(self._update_state)
        self._update_state()
        self.resize(560, self.sizeHint().height())

    def _choose_folder(self):
        folder = QFileDialog.getExistingDirectory(
            self, tr("Folder to keep the DEM tiles", "DEMのタイルの保存場所"),
            self.folderEdit.text() or os.path.expanduser("~"),
        )
        if folder:
            self.folderEdit.setText(folder)
            self.keepCheck.setChecked(True)

    def _open_folder(self):
        folder = self.folderEdit.text().strip()
        if os.path.isdir(folder):
            QDesktopServices.openUrl(QUrl.fromLocalFile(folder))

    def _update_state(self, *args):
        del args
        self.folderEdit.setEnabled(self.keepCheck.isChecked())
        lines = []
        project = QgsProject.instance().absolutePath()
        if project:
            session = os.path.join(project, packages.ROOT_FOLDER, dem_store.SESSION_FOLDER)
            lines.append(tr(
                "Kept while QGIS runs (this project): {} GB",
                "QGISの起動中の一時保存（このプロジェクト）：{} GB",
            ).format(_gb(dem_store.folder_size(session))))
        folder = self.folderEdit.text().strip()
        self.openFolderButton.setEnabled(os.path.isdir(folder))
        if self.keepCheck.isChecked() and folder and os.path.isdir(folder):
            lines.append(tr("In the chosen folder: {} GB", "保存場所：{} GB").format(
                _gb(dem_store.folder_size(folder))))
        self.sizeLabel.setText("\n".join(lines))

    def _accept(self):
        folder = self.folderEdit.text().strip()
        keep = self.keepCheck.isChecked() and bool(folder)
        settings = QgsSettings()
        settings.setValue(dem_store.SETTING_ENABLED, keep)
        settings.setValue(dem_store.SETTING_DIR, folder)
        self.accept()
