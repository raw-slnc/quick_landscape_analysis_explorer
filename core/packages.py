# -*- coding: utf-8 -*-
"""Named, portable terrain-analysis ZIP packages."""

import json
import os
import re
import zipfile


ROOT_FOLDER = "quick_landscape_analysis"
ANALYSES_FOLDER = "analyses"
EXPORTS_FOLDER = "exports"
# Written by the Layers export: what the layer panel menu needs to make the
# print layout of the exported group (no link to the ZIP is needed).
LAYOUT_RECORD_NAME = "qlae_layout.json"
MANIFEST_NAME = "analysis.json"


class PackageError(Exception):
    pass


def storage_directory(project_directory):
    if not project_directory:
        raise PackageError("project_not_saved")
    return os.path.join(project_directory, ROOT_FOLDER, ANALYSES_FOLDER)


def package_file_name(analysis_name):
    name = " ".join(str(analysis_name).strip().split())
    if not name:
        raise PackageError("analysis_name_required")
    safe = re.sub(r'[\\/:*?"<>|\x00-\x1f]', "_", name).strip(" .")
    if not safe:
        raise PackageError("analysis_name_required")
    return safe + ".zip"


def export_directory(package_path_value):
    """Where the Layers export writes an analysis: exports/<name>/ beside analyses/."""

    absolute = os.path.abspath(package_path_value)
    root = os.path.dirname(os.path.dirname(absolute))
    return os.path.join(root, EXPORTS_FOLDER, os.path.splitext(os.path.basename(absolute))[0])


def package_path(project_directory, analysis_name):
    return os.path.join(
        storage_directory(project_directory), package_file_name(analysis_name)
    )


def read_manifest(path):
    try:
        with zipfile.ZipFile(path, "r") as archive:
            payload = archive.read(MANIFEST_NAME)
        manifest = json.loads(payload.decode("utf-8"))
    except (OSError, KeyError, ValueError, zipfile.BadZipFile) as error:
        raise PackageError("invalid_package") from error
    if not isinstance(manifest, dict) or not manifest.get("analysis_name"):
        raise PackageError("invalid_package")
    return manifest


def list_packages(project_directory):
    try:
        folder = storage_directory(project_directory)
    except PackageError:
        return []
    if not os.path.isdir(folder):
        return []
    packages = []
    for file_name in sorted(os.listdir(folder), key=str.casefold):
        if not file_name.lower().endswith(".zip"):
            continue
        path = os.path.join(folder, file_name)
        try:
            manifest = read_manifest(path)
        except PackageError:
            continue
        packages.append((manifest["analysis_name"], path, manifest))
    return packages


def vsi_path(package_path_value, member):
    absolute = os.path.abspath(package_path_value).replace("\\", "/")
    return "/vsizip/{}{}".format(absolute, "/" + member.lstrip("/"))
