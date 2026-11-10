# -*- coding: utf-8 -*-
"""Small runtime language switcher.

English is the source UI. Japanese is kept beside each English string so the
first development stages do not depend on compiled translation catalogs.
"""

from qgis.PyQt.QtCore import QSettings


SETTING = "quick_landscape_analysis_explorer/language"
ENGLISH = "en"
JAPANESE = "ja"

_language = None


def language():
    global _language
    if _language is None:
        stored = QSettings().value(SETTING, ENGLISH)
        _language = JAPANESE if stored == JAPANESE else ENGLISH
    return _language


def set_language(value):
    global _language
    _language = JAPANESE if value == JAPANESE else ENGLISH
    QSettings().setValue(SETTING, _language)


def is_japanese():
    return language() == JAPANESE


def tr(english, japanese):
    return japanese if is_japanese() else english
