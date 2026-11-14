# -*- coding: utf-8 -*-
"""The plugin's own text size.

The plugin's windows keep one text size (PLUGIN_PT) whatever QGIS's font
size setting is, so their layout does not break; the operating system's
display scaling (Windows 125 %, high-resolution screens) still enlarges
them as a whole. The typeface stays QGIS's (the system's). Qt does not pass
a window's font on to the windows it opens, so each of those (message boxes,
input dialogs, the CAD layer choice) is given the font here too.
"""

from qgis.PyQt.QtGui import QFont
from qgis.PyQt.QtWidgets import QApplication, QDialog, QInputDialog, QLineEdit

PLUGIN_PT = 9.0
# For style sheets that set a font property (bold): Qt then takes the text
# size from the application font, not from the window, unless it is given.
PLUGIN_SIZE_RULE = "font-size: {:g}pt;".format(PLUGIN_PT)


def plugin_font():
    font = QFont(QApplication.font())
    font.setPointSizeF(PLUGIN_PT)
    return font


# The size for everything in a plugin window, as a style sheet. Any style
# sheet on a window makes its widgets take the application font (QGIS's
# size) instead of the window's, so the size is given to all of them here,
# which also reaches what a font alone does not: combo lists, the windows a
# plugin window opens, and tooltips (QGIS's tooltip font is application-wide
# and stays as it is; a tooltip is a child of its widget). A widget's own
# style sheet still wins (the developer credit's 8 pt).
WINDOW_RULES = "QWidget {{ font-size: {0:g}pt; }} QToolTip {{ font-size: {0:g}pt; }}".format(
    PLUGIN_PT
)


def use_plugin_font(window):
    """Give a window (and through it, its contents, lists and tooltips) the
    plugin's text size."""

    window.setFont(plugin_font())
    if WINDOW_RULES not in window.styleSheet():
        window.setStyleSheet((window.styleSheet() + " " + WINDOW_RULES).strip())
    return window


def ask_int(parent, title, label, value, minimum, maximum):
    """QInputDialog.getInt in the plugin's text size: (value, accepted)."""

    dialog = use_plugin_font(QInputDialog(parent))
    dialog.setWindowTitle(title)
    dialog.setLabelText(label)
    dialog.setInputMode(QInputDialog.InputMode.IntInput)
    dialog.setIntRange(minimum, maximum)
    dialog.setIntValue(value)
    try:
        accepted = dialog.exec() == QDialog.DialogCode.Accepted
        return dialog.intValue(), accepted
    finally:
        dialog.deleteLater()


def ask_text(parent, title, label, text):
    """QInputDialog.getText in the plugin's text size: (text, accepted)."""

    dialog = use_plugin_font(QInputDialog(parent))
    dialog.setWindowTitle(title)
    dialog.setLabelText(label)
    dialog.setInputMode(QInputDialog.InputMode.TextInput)
    dialog.setTextEchoMode(QLineEdit.EchoMode.Normal)
    dialog.setTextValue(text)
    try:
        accepted = dialog.exec() == QDialog.DialogCode.Accepted
        return dialog.textValue(), accepted
    finally:
        dialog.deleteLater()
