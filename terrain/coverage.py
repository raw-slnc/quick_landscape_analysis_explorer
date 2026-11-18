# -*- coding: utf-8 -*-
"""Where a regional network DEM source has data, shown as a mesh on the
preview while the source is chosen.

The coverage is kept as a grid of the source's own tiles (1 km in France
and Rhineland-Palatinate, the Topo50 sheets in New Zealand): one bit for
each tile, have or not. A grid ships with the plugin (terrain/data/
coverage/, made by tools/build_coverage.py) and is brought up to date while
QGIS runs, at most once a run for each source, in a background thread: a
small question first (a count, or the source's list), and only when it has
changed, the new tiles (France: those published after the grid's date) or
the whole list again. The updated grid is kept in the QGIS profile, not in
the plugin's folder. The Netherlands is covered whole: no grid, QGIS's
world map is used.

Only the part of a grid under the preview is turned into a shape, so the
mesh stays light however large the coverage is. No QGIS API here except
the profile path, so the updates run in a worker thread.
"""

import base64
import datetime
import gzip
import json
import math
import os
import re
import time
import urllib.request
import zlib

import numpy as np
from osgeo import gdal, ogr

from . import remote_dem, sources

_DATA_DIR = os.path.join(os.path.dirname(__file__), "data", "coverage")
_USER_AGENT = "Mozilla/5.0 (compatible; QGIS plugin)"

IGN_WFS = "https://data.geopf.fr/wfs/ows?SERVICE=WFS&VERSION=2.0.0&REQUEST=GetFeature"
IGN_LAYER = "IGNF_LIDAR-HD_METADONNEE:metadata"
IGN_PAGE = 5000

# From the country files (terrain/sources/): source key -> its grid (EPSG,
# tile width and height in its units, the top-left corner of its tile
# numbering, the check interval in days, whether to ask before an update).
GRIDS = {
    key: source["coverage"] for key, source in sources.SOURCES.items()
    if "epsg" in source.get("coverage", {})
}
# Covered whole: the country's shape from QGIS's world map.
WHOLE_COUNTRY = {
    key: source["coverage"]["whole_country"] for key, source in sources.SOURCES.items()
    if "whole_country" in source.get("coverage", {})
}
KEYS = tuple(GRIDS) + tuple(WHOLE_COUNTRY)


class Coverage:
    """A grid of tiles: ``mask[row, col]`` is True where the source has a
    tile; row 0 is the top. ``x0``/``y_top`` are the grid's top-left corner."""

    def __init__(self, key, x0, y_top, mask, date="", count=0):
        grid = GRIDS[key]
        self.key = key
        self.epsg = grid["epsg"]
        self.cell_w = grid["cell_w"]
        self.cell_h = grid["cell_h"]
        self.x0 = float(x0)
        self.y_top = float(y_top)
        self.mask = mask.astype(bool)
        self.date = date  # newest tile date taken in (France), or the day made
        self.count = int(count)  # tiles the source listed when made

    # Storage: <key>.json with the grid's place and size, and <key>.mask
    # beside it, the mask packed (bits, zlib). Grids saved before kept the
    # mask in the JSON, in base64; they are still read (the base64 text set
    # off secret scanners, so it is no longer written).

    def to_json(self):
        rows, cols = self.mask.shape
        return {
            "key": self.key, "epsg": self.epsg, "x0": self.x0, "y_top": self.y_top,
            "rows": rows, "cols": cols, "date": self.date, "count": self.count,
            "made": datetime.date.today().isoformat(),
        }

    def packed_mask(self):
        return zlib.compress(np.packbits(self.mask, axis=None).tobytes(), 9)

    @classmethod
    def from_json(cls, data, packed=None):
        rows, cols = int(data["rows"]), int(data["cols"])
        if "mask" in data:  # saved before, in the JSON
            packed = base64.b64decode(data["mask"])
        if packed is None:
            raise ValueError("no mask")
        bits = np.frombuffer(zlib.decompress(packed), dtype=np.uint8)
        mask = np.unpackbits(bits)[: rows * cols].reshape(rows, cols).astype(bool)
        return cls(data["key"], data["x0"], data["y_top"], mask,
                   data.get("date", ""), data.get("count", 0))

    @classmethod
    def from_cells(cls, key, cells, date="", count=0):
        """From (col, row) indices on the source's own tile numbering, where
        row grows downwards (see _ign_cell and the others)."""
        cols = [c for c, _r in cells]
        rows = [r for _c, r in cells]
        c0, r0 = min(cols), min(rows)
        mask = np.zeros((max(rows) - r0 + 1, max(cols) - c0 + 1), dtype=bool)
        mask[np.array(rows) - r0, np.array(cols) - c0] = True
        x0, y_top = _cell_corner(key, c0, r0)
        return cls(key, x0, y_top, mask, date, count)

    def add_cells(self, cells):
        """Set more tiles, growing the grid when they lie outside it."""
        if not cells:
            return
        own_c0 = int(round((self.x0 - _ORIGINS[self.key][0]) / self.cell_w))
        own_r0 = int(round((_ORIGINS[self.key][1] - self.y_top) / self.cell_h))
        all_cells = [(own_c0 + c, own_r0 + r) for r, c in zip(*np.nonzero(self.mask))]
        merged = Coverage.from_cells(self.key, all_cells + list(cells), self.date, self.count)
        self.x0, self.y_top, self.mask = merged.x0, merged.y_top, merged.mask

    # The shape under a window.

    def shape_wkt(self, xmin, ymin, xmax, ymax):
        """The covered area within a box in the grid's CRS, as WKT (a
        polygon of whole tiles), or "" when none is there."""
        rows, cols = self.mask.shape
        c0 = max(0, int(math.floor((xmin - self.x0) / self.cell_w)))
        c1 = min(cols, int(math.ceil((xmax - self.x0) / self.cell_w)))
        r0 = max(0, int(math.floor((self.y_top - ymax) / self.cell_h)))
        r1 = min(rows, int(math.ceil((self.y_top - ymin) / self.cell_h)))
        if c0 >= c1 or r0 >= r1:
            return ""
        window = self.mask[r0:r1, c0:c1]
        if not window.any():
            return ""
        raster = gdal.GetDriverByName("MEM").Create("", c1 - c0, r1 - r0, 1, gdal.GDT_Byte)
        raster.SetGeoTransform((self.x0 + c0 * self.cell_w, self.cell_w, 0.0,
                                self.y_top - r0 * self.cell_h, 0.0, -self.cell_h))
        band = raster.GetRasterBand(1)
        band.WriteArray(window.astype(np.uint8))
        from .dem_gaps import memory_vector_datasource

        vectors = memory_vector_datasource()
        layer = vectors.CreateLayer("coverage", geom_type=ogr.wkbPolygon)
        gdal.Polygonize(band, band, layer, -1, [])
        shapes = ogr.Geometry(ogr.wkbMultiPolygon)
        for feature in layer:
            shapes.AddGeometry(feature.GetGeometryRef().Clone())
        return shapes.UnionCascaded().ExportToWkt() if shapes.GetGeometryCount() else ""


# The tile numbering of each source: (col, row), row growing downwards from
# an origin; _ORIGINS gives the origin's top-left corner.
_ORIGINS = {key: tuple(grid["origin"]) for key, grid in GRIDS.items()}
_TOPO50_LETTERS = "ABCDEFGHJKLMNPQRSTUVWXYZ"  # without I and O, as remote_dem  # pragma: allowlist secret


def _cell_corner(key, col, row):
    x, y = _ORIGINS[key]
    grid = GRIDS[key]
    return x + col * grid["cell_w"], y - row * grid["cell_h"]


# Mainland France in Lambert-93 (the plugin fetches only that layer): the
# list also holds the overseas departments, numbered in their own UTM zones
# (the Antilles below 3,000 km north, Réunion above 7,300 km).
_IGN_NORTH_KM = (6000, 7300)


def _ign_cell(name):
    """"0371-6389" (west km, north km) -> (col, row); None outside mainland
    France."""
    west, north = (int(part) for part in name.split("-"))
    if not _IGN_NORTH_KM[0] < north <= _IGN_NORTH_KM[1]:
        return None
    return west, 8000 - north


def _rlp_cell(east, north):
    """A German 1 km tile by its lower-left corner in km -> (col, row)
    (Rhineland-Palatinate and Bavaria alike)."""
    return east, 7000 - (north + 1)


def _linz_cell(name):
    """A Topo50 sheet name (e.g. "BP35") -> (col, row), as remote_dem."""
    a = _TOPO50_LETTERS.index(name[0])
    b = _TOPO50_LETTERS.index(name[1])
    base = _TOPO50_LETTERS.index("A") * 24 + _TOPO50_LETTERS.index("S")
    return int(name[2:4]), a * 24 + b - base


# Loading.

def _user_dir():
    from qgis.core import QgsApplication

    return os.path.join(QgsApplication.qgisSettingsDirPath(),
                        "quick_landscape_analysis_explorer", "coverage")


def _read_json(path):
    """A JSON file's content, or None."""
    try:
        with open(path, encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, ValueError):
        return None


def _read_grid(folder, key):
    """(the grid's JSON, its packed mask or None) in ``folder``, or None."""
    data = _read_json(os.path.join(folder, key + ".json"))
    if data is None:
        return None
    packed = None
    if "mask" not in data:
        try:
            with open(os.path.join(folder, key + ".mask"), "rb") as handle:
                packed = handle.read()
        except OSError:
            return None
    return data, packed


def load(key, user_dir=None):
    """The newest grid of a source: the one updated in the profile, else
    the one shipped with the plugin; None if neither."""
    best = None
    for found in (_read_grid(_DATA_DIR, key), _read_grid(user_dir or _user_dir(), key)):
        if not found:
            continue
        data = found[0]
        if best is None or (data.get("date", ""), data.get("count", 0)) >= (
                best[0].get("date", ""), best[0].get("count", 0)):
            best = found
    try:
        return Coverage.from_json(*best) if best else None
    except (KeyError, ValueError, zlib.error):
        return None


def save(coverage, folder):
    """Write the grid (<key>.mask, then <key>.json, each staged); the JSON's
    path."""
    os.makedirs(folder, exist_ok=True)
    mask_path = os.path.join(folder, coverage.key + ".mask")
    with open(mask_path + ".part", "wb") as handle:
        handle.write(coverage.packed_mask())
    os.replace(mask_path + ".part", mask_path)
    path = os.path.join(folder, coverage.key + ".json")
    staging = path + ".part"
    with open(staging, "w", encoding="utf-8") as handle:
        json.dump(coverage.to_json(), handle)
    os.replace(staging, path)
    return path


# Reading the sources (network).

def _get(url, timeout=120, gzip_ok=False):
    headers = {"User-Agent": _USER_AGENT}
    if gzip_ok:
        headers["Accept-Encoding"] = "gzip"
    request = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(request, timeout=timeout) as response:  # nosec B310
        data = response.read()
        if response.headers.get("Content-Encoding", "").lower() == "gzip":
            data = gzip.decompress(data)
    return data


def ign_count():
    data = _get(IGN_WFS + "&TYPENAMES={}&RESULTTYPE=hits".format(IGN_LAYER), timeout=60)
    found = re.search(rb'numberMatched="(\d+)"', data)
    return int(found.group(1)) if found else None


def _ign_page(url, tries=4):
    """One page of the IGN list. The service now and then answers a page
    with nothing (deep pages are slow): asked again after a pause."""
    for attempt in range(tries):
        try:
            return json.loads(_get(url))
        except (OSError, ValueError):
            if attempt == tries - 1:
                raise
            time.sleep(5 * (attempt + 1))


def ign_tiles(after="", check=None, progress=None):
    """[(col, row)] of the IGN tiles, only those published after the date
    ``after`` ("YYYY-MM-DD") when given, and the newest date seen."""
    cells = []
    newest = after
    start = 0
    query = "&TYPENAMES={}&OUTPUTFORMAT=application/json&COUNT={}&PROPERTYNAME=coordonnees_nw,date_edition&SORTBY=coordonnees_nw".format(IGN_LAYER, IGN_PAGE)
    if after:
        query += "&CQL_FILTER=" + urllib.request.quote("date_edition > '{}'".format(after))
    while True:
        if check is not None:
            check()
        page = _ign_page(IGN_WFS + query + "&STARTINDEX={}".format(start))
        features = page.get("features") or []
        for feature in features:
            properties = feature.get("properties") or {}
            name = properties.get("coordonnees_nw")
            cell = _ign_cell(name) if name else None
            if cell is not None:
                cells.append(cell)
            date = (properties.get("date_edition") or "")[:10]
            if date > newest:
                newest = date
        start += len(features)
        if progress is not None:
            progress(start)
        if len(features) < IGN_PAGE:
            break
    return cells, newest


def rlp_tiles():
    data = _get(remote_dem._RLP_DGM1, gzip_ok=True).decode("utf-8", "replace")
    keys = {(int(e), int(n)) for e, n, _y in
            re.findall(r"dgm1_32_(\d+)_(\d+)_1_rp_(\d{4})\.tif", data)}
    return [_rlp_cell(e, n) for e, n in keys]


# Bavaria's own list of its DGM1 files for download (Metalink), the whole
# state: about 22 MB, about 4 MB sent compressed.
BY_LIST = "https://geodaten.bayern.de/odd/a/dgm/dgm1/meta/metalink/09.meta4"


def by_list_date():
    """(date of Bavaria's list, its size in bytes), by a HEAD request (a few
    hundred bytes); (None, None) when not known."""
    request = urllib.request.Request(BY_LIST, method="HEAD", headers={"User-Agent": _USER_AGENT})
    with urllib.request.urlopen(request, timeout=60) as response:  # nosec B310
        modified = response.headers.get("Last-Modified")
        size = response.headers.get("Content-Length")
    if not modified:
        return None, None
    from email.utils import parsedate_to_datetime

    date = parsedate_to_datetime(modified).date().isoformat()
    return date, int(size) if size else None


def by_tiles():
    """[(col, row)] of Bavaria's tiles and the list's date."""
    date, _size = by_list_date()
    text = _get(BY_LIST, timeout=300, gzip_ok=True).decode("utf-8", "replace")
    keys = {(int(e), int(n)) for e, n in re.findall(r'<file name="(\d+)_(\d+)\.tif">', text)}
    return [_rlp_cell(e, n) for e, n in keys], date


def linz_tiles():
    collection = json.loads(_get(remote_dem._NZ_BASE + "collection.json", timeout=60))
    names = [link["href"][2:-5] for link in collection.get("links", [])
             if link.get("rel") == "item" and link.get("href", "").startswith("./")]
    cells = []
    for name in names:
        try:
            cells.append(_linz_cell(name))
        except (ValueError, IndexError):
            continue
    return cells


def build(key, check=None, progress=None):
    """A grid made from the source's whole list (tools/build_coverage.py)."""
    today = datetime.date.today().isoformat()
    if key == "ign_mnt":
        # The count is the service's own (the overseas tiles too), which
        # the updates compare with.
        count = ign_count()
        cells, newest = ign_tiles(check=check, progress=progress)
        return Coverage.from_cells(key, cells, newest, count or len(cells))
    if key == "by_dgm1":
        cells, date = by_tiles()
        return Coverage.from_cells(key, cells, date or today, len(cells))
    cells = rlp_tiles() if key == "rlp_dgm1" else linz_tiles()
    return Coverage.from_cells(key, cells, today, len(cells))


# How often a source is checked, in days, and whether the Status section
# asks before the update is fetched (it can take a while): from the
# country files.
CHECK_DAYS = {key: grid["check_days"] for key, grid in GRIDS.items()}
ASK_FIRST = tuple(key for key, grid in GRIDS.items() if grid.get("ask_first"))
_CHECKED_FILE = "checked.json"


def _checked(user_dir):
    return _read_json(os.path.join(user_dir, _CHECKED_FILE)) or {}


def due(key, user_dir=None, now=None):
    """Whether the source should be checked again (its interval has passed
    since the last check; kept in the profile, across QGIS runs)."""
    if key not in CHECK_DAYS:
        return False
    last = _checked(user_dir or _user_dir()).get(key)
    now = time.time() if now is None else now
    return last is None or now - float(last) >= CHECK_DAYS[key] * 86400


def mark_checked(key, user_dir=None):
    folder = user_dir or _user_dir()
    os.makedirs(folder, exist_ok=True)
    data = _checked(folder)
    data[key] = time.time()
    path = os.path.join(folder, _CHECKED_FILE)
    with open(path + ".part", "w", encoding="utf-8") as handle:
        json.dump(data, handle)
    os.replace(path + ".part", path)


def by_list_newer(user_dir=None):
    """Bavaria's list is newer than the grid: its size in bytes (sent
    compressed, about a fifth); 0 when not newer; None when not known."""
    coverage = load("by_dgm1", user_dir)
    date, size = by_list_date()
    if date is None or coverage is None:
        return None
    return (size or 1) if date > coverage.date else 0


def ign_new_tiles(user_dir=None):
    """How many tiles France has more than the grid (one small question),
    0 when none; None when the count could not be read."""
    coverage = load("ign_mnt", user_dir)
    count = ign_count()
    if count is None or coverage is None:
        return None
    return max(0, count - coverage.count)


def update(key, user_dir=None, check=None):
    """Bring a source's grid up to date (network); True when it changed.

    France: the count first (a few hundred bytes); when it has grown, only
    the tiles published after the grid's date. Rhineland-Palatinate and New
    Zealand: their list (about 400 KB and 85 KB), the grid made again when
    the number of tiles differs.
    """
    coverage = load(key, user_dir)
    if key == "ign_mnt":
        if coverage is None:
            return False  # the whole list is too large to read here
        count = ign_count()
        if count is None or count <= coverage.count:
            return False
        cells, newest = ign_tiles(after=coverage.date, check=check)
        coverage.add_cells(cells)
        coverage.date = newest
        coverage.count = count
    elif key == "by_dgm1":
        cells, date = by_tiles()
        if coverage is not None and date and date <= coverage.date:
            return False
        coverage = Coverage.from_cells(key, cells, date or datetime.date.today().isoformat(), len(cells))
    else:
        cells = rlp_tiles() if key == "rlp_dgm1" else linz_tiles()
        return take_list(key, cells, user_dir)
    save(coverage, user_dir or _user_dir())
    return True


# Taken from an analysis, which reads the same lists or fetches the tiles
# (no more network). Never stops the analysis.

def take_list(key, cells, user_dir=None):
    """The source's whole list, read by the analysis anyway (Rhineland-
    Palatinate, New Zealand): the grid made again when its count differs."""
    coverage = load(key, user_dir)
    if not cells or (coverage is not None and len(cells) == coverage.count):
        return False
    if coverage is not None and len(cells) < 0.9 * coverage.count:
        # Far fewer than before: more likely a list cut short than tiles
        # taken away; the grid is kept.
        return False
    save(Coverage.from_cells(key, cells, datetime.date.today().isoformat(), len(cells)),
         user_dir or _user_dir())
    return True


def take_found(key, cells, user_dir=None):
    """Tiles an analysis fetched (Bavaria): those not in the grid are added.
    Tiles not found are not taken out: a failed request is no proof."""
    coverage = load(key, user_dir)
    if coverage is None or not cells:
        return False
    rows, cols = coverage.mask.shape
    own_c0 = int(round((coverage.x0 - _ORIGINS[key][0]) / coverage.cell_w))
    own_r0 = int(round((_ORIGINS[key][1] - coverage.y_top) / coverage.cell_h))
    new = [(c, r) for c, r in cells
           if not (0 <= r - own_r0 < rows and 0 <= c - own_c0 < cols
                   and coverage.mask[r - own_r0, c - own_c0])]
    if not new:
        return False
    coverage.add_cells(new)
    save(coverage, user_dir or _user_dir())
    return True
