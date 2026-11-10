# -*- coding: utf-8 -*-


def classFactory(iface):
    from .plugin import QuickLandscapeAnalysisExplorer

    return QuickLandscapeAnalysisExplorer(iface)
