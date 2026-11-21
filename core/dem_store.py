# -*- coding: utf-8 -*-
"""DEM tiles kept for reuse: the tiles of a source as fetched,
by year and code.

Two places:
- the session store: in the project's quick_landscape_analysis/dem_session/,
  one folder for each run of QGIS. It is used while that QGIS runs and is
  removed when the project is analysed in a later run (a folder whose QGIS
  no longer holds its lock).
- a folder the user chooses (advanced settings): kept until they remove it.
  When it is set, it is used instead of the session store.

A tile is written under a temporary name and then renamed, so another QGIS
using the same folder never reads a half-written tile.
"""

import os
import shutil
import sys
import time
import uuid

from . import packages

SESSION_FOLDER = "dem_session"
LOCK_NAME = ".lock"
# Settings (QgsSettings) of the chosen folder.
SETTING_ENABLED = "qlae/dem_store_enabled"
SETTING_DIR = "qlae/dem_store_dir"
# Name of the attribute holding this QGIS run's session on the application
# object: it outlives a reload of the plugin, which is the same run.
_APP_ATTRIBUTE = "_qlae_dem_session"
# A download folder left in a chosen folder this long is removed.
STALE_INCOMING_SECONDS = 24 * 3600


class TileStore:
    """Tiles under ``root``/<source>/<year>/<code><ext>; a source whose tiles
    have no year keeps them in ``root``/<source>/<code><ext>. For map tiles
    (GSI, Terrarium) the zoom level takes the place of the year."""

    def __init__(self, root, kind):
        self.root = root
        self.kind = kind  # "session" or "folder"
        self.reused = 0
        self.downloaded = 0

    def path(self, source, year, code, ext=".tif"):
        folder = os.path.join(self.root, source)
        if year is not None:
            folder = os.path.join(folder, str(year))
        return os.path.join(folder, "{}{}".format(code, ext))

    def find(self, source, year, code, ext=".tif"):
        """The kept tile, or None."""
        path = self.path(source, year, code, ext)
        return path if os.path.isfile(path) else None

    def keep_bytes(self, source, year, code, data, ext=".tif"):
        """Write a fetched tile held in memory into the store."""
        target = self.path(source, year, code, ext)
        os.makedirs(os.path.dirname(target), exist_ok=True)
        staging = target + ".part-" + uuid.uuid4().hex
        try:
            with open(staging, "wb") as handle:
                handle.write(data)
            os.replace(staging, target)
        except BaseException:
            try:
                os.remove(staging)
            except OSError:
                pass
            raise
        return target

    def incoming(self):
        """A new empty folder on the store's disk to download into."""
        folder = os.path.join(self.root, ".incoming", uuid.uuid4().hex)
        os.makedirs(folder, exist_ok=True)
        return folder

    def keep(self, source, year, code, fetched_path, ext=".tif"):
        """Move a fetched tile into the store; returns its path there."""
        target = self.path(source, year, code, ext)
        os.makedirs(os.path.dirname(target), exist_ok=True)
        staging = target + ".part-" + uuid.uuid4().hex
        shutil.move(fetched_path, staging)
        os.replace(staging, target)
        return target

    def drop_incoming(self, folder):
        shutil.rmtree(folder, ignore_errors=True)


def chosen_folder():
    """The folder chosen in the advanced settings, or "" when not in use."""
    from qgis.core import QgsSettings

    settings = QgsSettings()
    if not settings.value(SETTING_ENABLED, False, type=bool):
        return ""
    return settings.value(SETTING_DIR, "", type=str) or ""


def open_store(project_directory, chosen=""):
    """The store an analysis uses: the chosen folder, else this run's
    session folder in the project (old session folders removed first)."""
    if chosen:
        os.makedirs(chosen, exist_ok=True)
        _remove_stale_incoming(chosen)
        return TileStore(chosen, "folder")
    root = os.path.join(
        project_directory, packages.ROOT_FOLDER, SESSION_FOLDER
    )
    os.makedirs(root, exist_ok=True)
    session = _session()
    remove_old_sessions(root, session["id"])
    folder = os.path.join(root, session["id"])
    os.makedirs(folder, exist_ok=True)
    _hold_lock(session, folder)
    return TileStore(folder, "session")


def remove_old_sessions(root, own_id=None):
    """Remove the session folders whose QGIS has ended (its lock is free)."""
    if not os.path.isdir(root):
        return
    for name in os.listdir(root):
        folder = os.path.join(root, name)
        if name == own_id or not os.path.isdir(folder):
            continue
        if _lock_is_free(os.path.join(folder, LOCK_NAME)):
            shutil.rmtree(folder, ignore_errors=True)


def _remove_stale_incoming(root):
    """Downloads left behind (QGIS closed mid-download) in a chosen folder:
    removed after a day, so a download still running elsewhere is kept."""
    incoming = os.path.join(root, ".incoming")
    if not os.path.isdir(incoming):
        return
    now = time.time()
    for name in os.listdir(incoming):
        folder = os.path.join(incoming, name)
        try:
            if now - os.path.getmtime(folder) > STALE_INCOMING_SECONDS:
                shutil.rmtree(folder, ignore_errors=True)
        except OSError:
            pass


def folder_size(root):
    """Bytes of the files under ``root`` (0 when missing)."""
    total = 0
    for directory, _dirs, files in os.walk(root):
        for name in files:
            try:
                total += os.path.getsize(os.path.join(directory, name))
            except OSError:
                pass
    return total


# This run of QGIS and its lock.


def _session():
    from qgis.PyQt.QtCore import QCoreApplication

    app = QCoreApplication.instance()
    session = getattr(app, _APP_ATTRIBUTE, None) if app is not None else None
    if session is None:
        session = {
            "id": "{}_{}".format(os.getpid(), uuid.uuid4().hex[:8]),
            "locks": {},
        }
        if app is not None:
            setattr(app, _APP_ATTRIBUTE, session)
    return session


def _hold_lock(session, folder):
    """Keep the folder's lock file open and locked while QGIS runs."""
    if folder in session["locks"]:
        return
    handle = open(os.path.join(folder, LOCK_NAME), "a+b")
    if not _try_lock(handle):
        handle.close()
        return
    session["locks"][folder] = handle


def _lock_is_free(path):
    """True when no running QGIS holds the lock (or there is none)."""
    if not os.path.exists(path):
        return True
    try:
        handle = open(path, "a+b")
    except OSError:
        return False  # in use (Windows) or not ours to touch
    try:
        if not _try_lock(handle):
            return False
        _unlock(handle)
        return True
    finally:
        handle.close()


def _try_lock(handle):
    try:
        if sys.platform == "win32":
            import msvcrt

            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except OSError:
        return False


def _unlock(handle):
    try:
        if sys.platform == "win32":
            import msvcrt

            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    except OSError:
        pass
