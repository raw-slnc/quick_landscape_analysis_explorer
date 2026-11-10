# -*- coding: utf-8 -*-
"""The analysis layers the plugin manages in the project.

The plugin keeps the layers of the analysis in use in its own layer group.
They stay in the project (also after the window is closed, and in the saved
project), so the print layout made from them keeps working. They read the
analysis ZIP directly; when a ZIP is gone, its layers are removed when a
project is opened.
"""

import os

from qgis.core import QgsLayerTreeGroup, QgsProject

PLUGIN_NAME = "Quick Landscape Analysis Explorer"


def is_session_layer(layer):
    """A layer of an analysis ZIP managed by the plugin."""

    if bool(layer.customProperty("qlae/permanent", False)):
        return False  # kept by an earlier version of the Layers export
    if bool(layer.customProperty("qlae/session_managed", False)):
        return True
    # Compatibility with layers added before the explicit session marker.
    return bool(layer.customProperty("qlae/package_path", ""))


def session_package_paths(project=None):
    """Package paths of the managed layers, in layer tree order."""

    project = project or QgsProject.instance()
    paths = []
    for layer in project.layerTreeRoot().layerOrder():
        if not is_session_layer(layer):
            continue
        path = layer.customProperty("qlae/package_path", "")
        if path and path not in paths:
            paths.append(path)
    return paths


def remove_layers_without_package(project=None):
    """Remove managed layers whose analysis ZIP no longer exists.

    Not counted as a change of the project (closing without doing anything
    does not ask to save), like Morizon Next.
    """

    project = project or QgsProject.instance()
    ids = [
        layer.id()
        for layer in project.mapLayers().values()
        if is_session_layer(layer)
        and not os.path.isfile(layer.customProperty("qlae/package_path", ""))
    ]
    if not ids:
        return 0
    was_dirty = project.isDirty()
    project.removeMapLayers(ids)
    prune_empty_groups(project.layerTreeRoot())
    if not was_dirty:
        project.setDirty(False)
    return len(ids)


def prune_empty_groups(root):
    """Remove empty groups in the plugin group, and the group when empty."""

    plugin_group = next(
        (
            child
            for child in root.children()
            if isinstance(child, QgsLayerTreeGroup) and child.name() == PLUGIN_NAME
        ),
        None,
    )
    if plugin_group is None:
        return

    def prune(group):
        for child in list(group.children()):
            if isinstance(child, QgsLayerTreeGroup):
                prune(child)
                if not child.children():
                    group.removeChildNode(child)

    prune(plugin_group)
    if not plugin_group.children():
        root.removeChildNode(plugin_group)
