# -*- coding: utf-8 -*-
"""Named, portable terrain-analysis ZIP packages."""

import json
import os
import re
import shutil
import uuid
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


def normalized_name(analysis_name):
    """The analysis name as it is stored: runs of spaces become one."""

    return " ".join(str(analysis_name).strip().split())


def renamed_package_path(path, new_name):
    """Where ``path`` goes when renamed to ``new_name`` (same folder)."""

    return os.path.join(os.path.dirname(os.path.abspath(path)), package_file_name(new_name))


def is_same_file_path(first, second):
    return os.path.normcase(os.path.abspath(first)) == os.path.normcase(os.path.abspath(second))


def rename_package(path, new_name):
    """Rename an analysis: the ZIP file and the name in its manifest.

    The ZIP is written anew beside the old one (a ZIP member cannot be
    replaced in place), then swapped in. When the old ZIP cannot be removed
    (in use, on Windows), the new one is removed again and nothing changes.
    Returns the new path.
    """

    path = os.path.abspath(path)
    name = normalized_name(new_name)
    new_path = renamed_package_path(path, name)
    same_file = is_same_file_path(new_path, path)
    if not same_file and os.path.exists(new_path):
        raise PackageError("analysis_name_taken")
    manifest = read_manifest(path)
    manifest["analysis_name"] = name
    payload = json.dumps(manifest, ensure_ascii=False, indent=2).encode("utf-8")
    temporary = new_path + ".{}.tmp".format(uuid.uuid4().hex)
    try:
        with zipfile.ZipFile(path, "r") as source, zipfile.ZipFile(
            temporary, "w", compression=zipfile.ZIP_STORED, allowZip64=True
        ) as target:
            for info in source.infolist():
                copied = zipfile.ZipInfo(info.filename, info.date_time)
                copied.compress_type = zipfile.ZIP_STORED
                copied.external_attr = info.external_attr
                if info.filename == MANIFEST_NAME:
                    target.writestr(copied, payload)
                    continue
                # The size lets zipfile choose ZIP64 for large members.
                copied.file_size = info.file_size
                with source.open(info) as reader, target.open(copied, "w") as writer:
                    shutil.copyfileobj(reader, writer, 1024 * 1024)
        if same_file:
            os.replace(temporary, path)
        else:
            os.replace(temporary, new_path)
            try:
                os.remove(path)
            except OSError:
                os.remove(new_path)
                raise
    finally:
        if os.path.exists(temporary):
            os.remove(temporary)
    return new_path


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
